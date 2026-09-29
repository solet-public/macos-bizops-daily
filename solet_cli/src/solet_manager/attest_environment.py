"""Live environment facts and the running-process probe for ``solet attest``.

Every fact here is QUERIED, never inferred: the OS build from ``sw_vers``,
Homebrew's own version and the installed versions of the manager Formula's
declared dependency closure from ``brew``, the interpreter the manager's own
venv actually runs on from the running process, the model identifiers actually
served from the local inference server's live listing, and the running
solet's code identity from its own ``attest_runtime_code`` verb over the
target's bridge CLI.  A probe that cannot run records WHY (``error``) and a
``null`` value -- an attestation must never print a plausible-looking fact
it did not measure.
"""

from __future__ import annotations

import json
import platform
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import cast

from .models import JsonValue
from .release_identity import CommandRunner, run_command

# The manager Formula's ``depends_on`` closure (solet.rb.template). The NAMES
# are the Formula's declaration; only their installed VERSIONS are queried.
# python@3.13 left it in r61: the Formula builds on any existing Python 3.13, so
# which one it used is a fact of the running venv (``manager_python``), not of brew.
FORMULA_DEPENDENCY_CLOSURE = ("git",)
_BREW_CANDIDATES = (Path("/opt/homebrew/bin/brew"), Path("/usr/local/bin/brew"))
_MODELS_ENDPOINT = "http://127.0.0.1:1234/api/v0/models"
_PROBE_TIMEOUT_S = 20
_BRIDGE_TIMEOUT_S = 45
_ATTEST_PROCESS = "plugin::macos_self_deployment_plugin::attest_runtime_code"
_MAX_BODY = 1_048_576


def environment_facts(runner: CommandRunner = run_command, models_endpoint: str = _MODELS_ENDPOINT) -> dict[str, JsonValue]:
    """The host facts a customer report needs, each with its own source and error."""

    return {
        "os": _os_build(runner),
        "homebrew": _homebrew_versions(runner),
        "manager_python": _manager_python(),
        "models_served": _models_served(models_endpoint),
    }


def _manager_python() -> dict[str, JsonValue]:
    """The interpreter this manager process runs on: its venv launcher and what that resolves to."""

    executable = Path(sys.executable)
    return {
        "source": "sys.executable realpath",
        "executable": str(executable),
        "resolved": str(executable.resolve()),
        "version": platform.python_version(),
        "error": None,
    }


def _os_build(runner: CommandRunner) -> dict[str, JsonValue]:
    fact: dict[str, JsonValue] = {"source": "sw_vers", "product_name": None, "product_version": None, "build_version": None, "error": None}
    outcome = runner(("sw_vers",), None, _PROBE_TIMEOUT_S)
    if outcome.returncode != 0:
        fact["error"] = outcome.stderr.strip() or f"sw_vers exited {outcome.returncode}"
        return fact
    values = {key.strip(): value.strip() for key, _, value in (line.partition(":") for line in outcome.stdout.splitlines()) if value}
    fact["product_name"] = values.get("ProductName")
    fact["product_version"] = values.get("ProductVersion")
    fact["build_version"] = values.get("BuildVersion")
    return fact


def _homebrew_versions(runner: CommandRunner) -> dict[str, JsonValue]:
    fact: dict[str, JsonValue] = {
        "source": None,
        "brew_version": None,
        "closure": cast(list[JsonValue], list(FORMULA_DEPENDENCY_CLOSURE)),
        "formulae": None,
        "error": None,
    }
    brew = next((candidate for candidate in _BREW_CANDIDATES if candidate.is_file()), None)
    if brew is None:
        fact["error"] = "brew_absent"
        return fact
    fact["source"] = f"{brew} list --versions {' '.join(FORMULA_DEPENDENCY_CLOSURE)}"
    version = runner((str(brew), "--version"), None, _PROBE_TIMEOUT_S)
    fact["brew_version"] = version.stdout.strip().splitlines()[0] if version.returncode == 0 and version.stdout.strip() else None
    listed = runner((str(brew), "list", "--versions", *FORMULA_DEPENDENCY_CLOSURE), None, _PROBE_TIMEOUT_S)
    if listed.returncode != 0 and not listed.stdout.strip():
        fact["error"] = listed.stderr.strip() or f"brew list exited {listed.returncode}"
        return fact
    fact["formulae"] = _parse_brew_versions(listed.stdout)
    return fact


def _parse_brew_versions(listing: str) -> dict[str, JsonValue]:
    formulae: dict[str, JsonValue] = dict.fromkeys(FORMULA_DEPENDENCY_CLOSURE)
    for line in listing.splitlines():
        name, _, versions = line.strip().partition(" ")
        if name in formulae and versions:
            formulae[name] = versions
    return formulae


def _models_served(endpoint: str) -> dict[str, JsonValue]:
    fact: dict[str, JsonValue] = {"source": endpoint, "models": None, "error": None}
    try:
        with urllib.request.urlopen(endpoint, timeout=5) as response:  # noqa: S310 - fixed loopback URL
            body = response.read(_MAX_BODY + 1)
        if len(body) > _MAX_BODY:
            fact["error"] = "model_listing_too_large"
            return fact
        payload: object = json.loads(body)
    except (OSError, ValueError, urllib.error.URLError) as exc:
        fact["error"] = f"inference_server_unreachable: {exc}"
        return fact
    rows = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(rows, list):
        fact["error"] = "model_listing_malformed"
        return fact
    models: list[JsonValue] = []
    for row in cast(list[object], rows):
        if isinstance(row, dict):
            entry = cast(dict[str, object], row)
            models.append({"id": str(entry.get("id") or ""), "state": str(entry.get("state") or "")})
    fact["models"] = models
    return fact


def probe_runtime(target: Path, runner: CommandRunner = run_command) -> tuple[dict[str, JsonValue] | None, str | None]:
    """Ask the RUNNING solet what code it serves, through the target's own bridge CLI.

    Returns ``(observed, None)`` when the process answered, else
    ``(None, reason)``: ``bridge_cli_absent`` when the target has no bridge
    binary, ``solet_not_running`` when the bridge found no live port, and
    ``runtime_attestation_malformed`` when the answer lacked the digest.
    """

    bridge = target / ".venv" / "bin" / "solet-bridge"
    if not bridge.is_file():
        return None, "bridge_cli_absent"
    arguments = json.dumps({"reconciliation_id": "solet-attest", "verification_modules": []})
    outcome = runner((str(bridge), "call", _ATTEST_PROCESS, arguments, "--timeout", "30"), target, _BRIDGE_TIMEOUT_S)
    if outcome.returncode != 0:
        detail = (outcome.stderr or outcome.stdout).strip().replace("\n", " ")[-300:]
        return None, f"solet_not_running: {detail}" if detail else "solet_not_running"
    try:
        payload: object = json.loads(outcome.stdout)
    except ValueError:
        return None, "runtime_attestation_malformed"
    data = _attestation_data(payload)
    if data is None or not isinstance(data.get("release_surface_sha256"), str):
        return None, "runtime_attestation_malformed"
    return data, None


def _attestation_data(payload: object) -> dict[str, JsonValue] | None:
    if not isinstance(payload, dict):
        return None
    result = cast(dict[str, JsonValue], payload).get("result")
    if not isinstance(result, dict):
        return None
    data = result.get("data")
    return data if isinstance(data, dict) else None


__all__ = ["FORMULA_DEPENDENCY_CLOSURE", "environment_facts", "probe_runtime"]
