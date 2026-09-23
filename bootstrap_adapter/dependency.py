"""Deterministic dependency-closure probe and repair route."""

from __future__ import annotations

import enum
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

from .models import (
    FORMULA_KEG_MARKER,
    INSTALL_TIMEOUT_SECONDS,
    PROBE_TIMEOUT_SECONDS,
    AdapterError,
    AdapterRuntime,
    Runner,
)
from .protocol import EMPTY_ACTION_PURPOSES, Request, evidence, planned_action, result

REQUIRED_DISTRIBUTIONS: tuple[tuple[str, str], ...] = (
    ("solet-setup-contracts", "solet_setup_contracts"),
    ("ananta", "ananta"),
    ("macos-vault-plugin", "plugins/macos_vault_plugin"),
    ("github_midwife_plugin", "plugins/github_midwife_plugin"),
    ("agent_messaging_plugin", "plugins/agent_messaging_plugin"),
)
Closure = tuple[tuple[str, str], ...]
EXISTING_DEPENDENCIES_REF = "existing::dependencies.reconcile"
_DECLARED_RELATIVE = re.compile(r"^(plugins/[a-z][a-z0-9_]*|ananta|solet_setup_contracts)$")
_DECLARED_DISTRIBUTION = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


class ClosureState(enum.Enum):
    """Closed states produced by executing the target interpreter."""

    ABSENT = "absent"
    INCOMPLETE = "incomplete"
    INTERPRETER_DANGLING = "interpreter_dangling"
    PRESENT_CLOSED = "present_closed"


def declared_closure(request: Request) -> Closure:
    """The closure this request declares: the fixed set for the create flow, the Manager's list for existing-install.

    The existing-install route's ``declared_closure`` public input is a sorted
    list of ``<distribution>=<relative path>`` rows the Manager computed from
    the fixed set, the preserved plugin roster and the release's declared
    additions (existing-install design section 4.1).  It must contain the fixed
    set: the adapter never installs less than the birth closure.
    """
    if request["operation_ref"] != EXISTING_DEPENDENCIES_REF:
        return REQUIRED_DISTRIBUTIONS
    raw = request["public_inputs"].get("declared_closure")
    if not isinstance(raw, list) or not raw:
        raise AdapterError("existing-install dependency reconcile requires a declared_closure list")
    rows = [_closure_row(item) for item in raw]
    declared = {relative for _, relative in rows}
    if any(relative not in declared for _, relative in REQUIRED_DISTRIBUTIONS):
        raise AdapterError("declared_closure omits a required seed distribution")
    if len(declared) != len(rows):
        raise AdapterError("declared_closure repeats a relative path")
    return tuple(rows)


def _closure_row(item: object) -> tuple[str, str]:
    if not isinstance(item, str) or "=" not in item:
        raise AdapterError("declared_closure rows must be <distribution>=<relative path>")
    distribution, _, relative = item.partition("=")
    if _DECLARED_DISTRIBUTION.fullmatch(distribution) is None or _DECLARED_RELATIVE.fullmatch(relative) is None:
        raise AdapterError(f"declared_closure row is outside the closed grammar: {item!r}")
    return distribution, relative


def _closure_probe_script(closure: Closure = REQUIRED_DISTRIBUTIONS) -> str:
    distributions = [name for name, _relative in closure]
    return f"""
import importlib.metadata as metadata
import importlib.util
import json

def present(name):
    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, AttributeError, ValueError):
        return False

packages = {{}}
for name in {distributions!r}:
    try:
        distribution = metadata.distribution(name)
    except metadata.PackageNotFoundError:
        packages[name] = None
    else:
        packages[name] = {{
            "version": distribution.version,
            "direct_url": distribution.read_text("direct_url.json") or "",
        }}
print(json.dumps({{
    "pip": present("pip"),
    "build_backend": present("setuptools.build_meta"),
    "wheel": present("wheel"),
    "packages": packages,
}}, sort_keys=True))
"""


def _editable_root_matches(raw_direct_url: object, expected: Path) -> bool:
    if not isinstance(raw_direct_url, str) or not raw_direct_url:
        return False
    try:
        parsed: object = json.loads(raw_direct_url)
    except json.JSONDecodeError:
        return False
    if not isinstance(parsed, dict):
        return False
    url = parsed.get("url")
    directory_info = parsed.get("dir_info")
    if not isinstance(url, str) or not url.startswith("file://"):
        return False
    if not isinstance(directory_info, dict) or directory_info.get("editable") is not True:
        return False
    installed = Path(unquote(urlparse(url).path)).resolve(strict=False)
    return installed == expected.resolve(strict=False)


def _venv_records_formula_keg(venv_dir: Path) -> bool:
    candidates = [venv_dir / "pyvenv.cfg"]
    candidates.extend((venv_dir / "bin").glob("python*"))
    candidates.extend((venv_dir / "bin").glob("solet-bridge"))
    for candidate in candidates:
        try:
            if candidate.is_symlink() and FORMULA_KEG_MARKER in os.readlink(candidate):
                return True
            if candidate.is_file() and FORMULA_KEG_MARKER in candidate.read_text(
                encoding="utf-8",
                errors="replace",
            ):
                return True
        except OSError:
            return True
    return False


def _run_probe(runner: Runner, command: list[str]) -> subprocess.CompletedProcess[str] | None:
    try:
        return runner(
            command,
            capture_output=True,
            text=True,
            timeout=PROBE_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None


def _version_state(runner: Runner, venv_python: Path) -> tuple[ClosureState | None, bool]:
    completed = _run_probe(
        runner,
        [
            str(venv_python),
            "-I",
            "-c",
            "import sys; print('.'.join(map(str, sys.version_info[:3])))",
        ],
    )
    if completed is None or completed.returncode != 0:
        return ClosureState.INTERPRETER_DANGLING, False
    return None, completed.stdout.strip().startswith("3.13.")


def _closure_payload(runner: Runner, venv_python: Path, closure: Closure = REQUIRED_DISTRIBUTIONS) -> dict[str, object] | None:
    completed = _run_probe(
        runner,
        [str(venv_python), "-I", "-c", _closure_probe_script(closure)],
    )
    if completed is None or completed.returncode != 0:
        return None
    try:
        parsed: object = json.loads(completed.stdout)
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


def _package_closure_matches(payload: dict[str, object], target: Path, closure: Closure = REQUIRED_DISTRIBUTIONS) -> bool:
    packages = payload.get("packages")
    if not isinstance(packages, dict):
        return False
    for distribution, relative in closure:
        package = packages.get(distribution)
        if not isinstance(package, dict) or not isinstance(package.get("version"), str):
            return False
        if not _editable_root_matches(package.get("direct_url"), target / relative):
            return False
    return True


def _missing_seed_distributions(payload: dict[str, object] | None, target: Path, closure: Closure = REQUIRED_DISTRIBUTIONS) -> tuple[str, ...]:
    """Return only editable distributions whose installed identity is not compliant."""

    if payload is None or not isinstance(payload.get("packages"), dict):
        return tuple(relative for _distribution, relative in closure)
    packages = payload.get("packages")
    if not isinstance(packages, dict):
        return tuple(relative for _distribution, relative in closure)
    return tuple(
        relative
        for distribution, relative in closure
        if not isinstance(packages.get(distribution), dict)
        or not _editable_root_matches(packages[distribution].get("direct_url"), target / relative)
    )


def _private_cli_works(runner: Runner, private_solet: Path) -> bool:
    if not private_solet.is_file():
        return False
    completed = _run_probe(runner, [str(private_solet), "--version"])
    return bool(
        completed is not None and completed.returncode == 0 and (completed.stdout or completed.stderr).strip(),
    )


def probe_dependency_closure(
    target: Path,
    runner: Runner,
    closure: Closure = REQUIRED_DISTRIBUTIONS,
) -> tuple[ClosureState, dict[str, bool]]:
    venv_dir = target / ".venv"
    venv_python = venv_dir / "bin/python3"
    facts = {
        "python_exists": venv_python.is_file(),
        "python_313": False,
        "pip": False,
        "build_backend": False,
        "wheel": False,
        "package_closure": False,
        "private_solet": False,
        "formula_keg_free": not _venv_records_formula_keg(venv_dir),
    }
    if not venv_python.is_file():
        return ClosureState.ABSENT, facts
    terminal, facts["python_313"] = _version_state(runner, venv_python)
    if terminal is not None:
        return terminal, facts
    payload = _closure_payload(runner, venv_python, closure)
    if payload is None:
        return ClosureState.INCOMPLETE, facts
    facts["pip"] = payload.get("pip") is True
    facts["build_backend"] = payload.get("build_backend") is True
    facts["wheel"] = payload.get("wheel") is True
    facts["package_closure"] = _package_closure_matches(payload, target, closure)
    facts["private_solet"] = _private_cli_works(runner, venv_dir / "bin/solet-bridge")
    closed = all(value is True for key, value in facts.items() if key != "python_exists")
    return (ClosureState.PRESENT_CLOSED if closed else ClosureState.INCOMPLETE), facts


def _python_candidate_works(runtime: AdapterRuntime, candidate: str) -> bool:
    if FORMULA_KEG_MARKER in candidate or not Path(candidate).is_file():
        return False
    completed = _run_probe(runtime.run, [candidate, "--version"])
    if completed is None or completed.returncode != 0:
        return False
    return f"{completed.stdout} {completed.stderr}".strip().startswith("Python 3.13")


def _resolve_long_lived_python(runtime: AdapterRuntime) -> str:
    discovered = shutil.which("python3.13")
    candidates = [
        runtime.base_python,
        discovered,
        "/opt/homebrew/bin/python3.13",
        "/usr/local/bin/python3.13",
        sys.executable if sys.version_info[:2] == (3, 13) else None,
    ]
    for candidate in dict.fromkeys(item for item in candidates if item is not None):
        if _python_candidate_works(runtime, candidate):
            return candidate
    raise AdapterError("no long-lived Python 3.13 outside the Solet formula keg is available")


def _run_required(runtime: AdapterRuntime, command: list[str], label: str) -> None:
    try:
        completed = runtime.run(
            command,
            capture_output=True,
            text=True,
            timeout=INSTALL_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise AdapterError(f"{label} could not execute") from exc
    if completed.returncode != 0:
        stderr_tail = completed.stderr[-1024:].strip()
        raise AdapterError(
            f"{label} failed (exit {completed.returncode}; stderr tail: {stderr_tail!r})"
        )


def _repair_venv_if_needed(
    runtime: AdapterRuntime, state: ClosureState, facts: dict[str, bool], venv_dir: Path, closure: Closure = REQUIRED_DISTRIBUTIONS
) -> tuple[ClosureState, dict[str, bool]]:
    if state not in {ClosureState.ABSENT, ClosureState.INTERPRETER_DANGLING} and facts["python_313"]:
        return state, facts
    interpreter = _resolve_long_lived_python(runtime)
    venv_command = [interpreter, "-m", "venv"]
    if venv_dir.exists():
        venv_command.append("--upgrade")
    _run_required(runtime, [*venv_command, str(venv_dir)], "venv construction")
    return probe_dependency_closure(runtime.target, runtime.run, closure)


def _repair_build_backend_if_needed(runtime: AdapterRuntime, facts: dict[str, bool], venv_python: Path) -> None:
    if all(facts[name] for name in ("pip", "build_backend", "wheel")):
        return
    _run_required(
        runtime,
        [str(venv_python), "-m", "pip", "install", "setuptools", "wheel"],
        "build-backend installation",
    )


def _repair_seed_packages_if_needed(
    runtime: AdapterRuntime, facts: dict[str, bool], venv_python: Path, closure: Closure = REQUIRED_DISTRIBUTIONS
) -> None:
    missing = _missing_seed_distributions(_closure_payload(runtime.run, venv_python, closure), runtime.target, closure)
    if not facts["private_solet"] and not missing:
        missing = tuple(relative for _distribution, relative in closure)
    for relative in missing:
        package_dir = runtime.target / relative
        if not package_dir.is_dir():
            raise AdapterError(f"required seed package directory is missing: {relative}")
        _run_required(
            runtime,
            [
                str(venv_python),
                "-m",
                "pip",
                "install",
                "--no-build-isolation",
                "-e",
                str(package_dir),
            ],
            f"seed install for {relative}",
        )


def apply_dependency_closure(runtime: AdapterRuntime, closure: Closure = REQUIRED_DISTRIBUTIONS) -> None:
    venv_dir = runtime.target / ".venv"
    venv_python = venv_dir / "bin/python3"
    state, facts = probe_dependency_closure(runtime.target, runtime.run, closure)
    if state is ClosureState.PRESENT_CLOSED:
        return
    _state, facts = _repair_venv_if_needed(runtime, state, facts, venv_dir, closure)
    _repair_build_backend_if_needed(runtime, facts, venv_python)
    _repair_seed_packages_if_needed(runtime, facts, venv_python, closure)


def _closure_evidence(
    runtime: AdapterRuntime,
    facts: dict[str, bool],
) -> list[dict[str, Any]]:
    return [
        evidence(
            runtime,
            evidence_id=f"environment.{key}",
            kind="dependency_closure",
            status="verified" if value else "pending",
            summary=f"Dependency-closure fact {key} was checked by the target interpreter.",
            observed=value,
            expected=True,
            source="$TARGET/.venv",
        )
        for key, value in sorted(facts.items())
    ]


def _declared_actions(runtime: AdapterRuntime, state: ClosureState, facts: dict[str, bool], closure: Closure, venv_python: Path) -> list[dict[str, Any]]:
    """Existing-install planned actions: exactly the missing declared pieces, or one venv rebuild (section 4.2)."""
    if state in {ClosureState.ABSENT, ClosureState.INTERPRETER_DANGLING} or not facts["python_313"]:
        return [planned_action("rebuild_venv", "Rebuild the instance virtual environment with the long-lived Python 3.13", "environment_rebuild", "$TARGET/.venv", state.value)]
    actions: list[dict[str, Any]] = []
    if not all(facts[name] for name in ("pip", "build_backend", "wheel")):
        actions.append(planned_action("pip.install_build_backend", "Install pip, setuptools, and wheel in the instance environment", "package_install", "$TARGET/.venv", "build_backend_incomplete"))
    for relative in _missing_seed_distributions(_closure_payload(runtime.run, venv_python, closure), runtime.target, closure):
        # ``pip.install_editable.<relative>`` (dots: the closed planned-action id
        # grammar admits no colon); the exact path is the action's target.
        actions.append(planned_action("pip.install_editable." + relative.replace("/", "."), f"Editable-install {relative} into the instance environment", "package_install", f"$TARGET/{relative}", "seed_package_closure_incomplete"))
    if not facts["private_solet"] and not actions:
        actions.append(planned_action("pip.install_editable.plugins.agent_messaging_plugin", "Reinstall the messaging plugin so the private bridge CLI works", "package_install", "$TARGET/plugins/agent_messaging_plugin", "private_cli_missing"))
    return actions


def _existing_probe_result(request: Request, runtime: AdapterRuntime, state: ClosureState, facts: dict[str, bool], closure: Closure, evidence_items: list[dict[str, Any]]) -> dict[str, Any]:
    """Existing-install probe: a not-yet-verified closure is ``pending``, never ``blocked`` (section 4.3).

    Only a formula-keg venv is a real refusal; a missing piece or a dangling
    interpreter is repairable and the executor reapplies a retry-safe
    operation on ``pending``.
    """
    if not facts["formula_keg_free"]:
        return result(request, status="blocked", error_kind="instance_formula_keg_detected", retry_safe=False, evidence_items=evidence_items, repair="Rebuild the instance environment outside the manager formula keg.")
    if request["probe_purpose"] in EMPTY_ACTION_PURPOSES:
        return result(request, status="pending", evidence_items=evidence_items, repair="Reapply the declared dependency closure.")
    return result(
        request,
        status="pending",
        planned_actions=_declared_actions(runtime, state, facts, closure, runtime.target / ".venv/bin/python3"),
        evidence_items=evidence_items,
        repair="Approve exactly the missing declared pieces.",
    )


def dependency_closure_route(request: Request, runtime: AdapterRuntime) -> dict[str, Any]:
    try:
        closure = declared_closure(request)
    except AdapterError as exc:
        return result(request, status="blocked", error_kind="adapter_protocol_error", retry_safe=False, repair=f"{exc}. Send the exact declared closure the Manager computed.")
    existing = request["operation_ref"] == EXISTING_DEPENDENCIES_REF
    if request["phase"] == "apply":
        try:
            apply_dependency_closure(runtime, closure)
        except AdapterError as exc:
            return result(
                request,
                status="failed",
                error_kind="dependency_closure_apply_failed",
                retry_safe=True,
                repair=(
                    f"{exc}. Repair the target package or interpreter condition and resume."
                ),
            )
        return result(request, status="applied")
    state, facts = probe_dependency_closure(runtime.target, runtime.run, closure)
    evidence_items = _closure_evidence(runtime, facts)
    if state is ClosureState.PRESENT_CLOSED:
        return result(request, status="verified", evidence_items=evidence_items)
    if existing:
        return _existing_probe_result(request, runtime, state, facts, closure, evidence_items)
    error_kind = "instance_interpreter_dangling" if state is ClosureState.INTERPRETER_DANGLING else "dependency_closure_incomplete"
    if request["probe_purpose"] in EMPTY_ACTION_PURPOSES:
        return result(
            request,
            status="blocked",
            error_kind=error_kind,
            evidence_items=evidence_items,
            repair="Re-run the approved dependency-closure repair operation.",
        )
    actions = [
        planned_action(
            "environment.create_or_repair_venv",
            "Create or deterministically repair the instance dependency closure",
            "environment_rebuild",
            "$TARGET/.venv",
            error_kind,
        ),
        planned_action(
            "environment.install_build_backend",
            "Install pip, setuptools, and wheel in the instance environment",
            "package_install",
            "$TARGET/.venv",
            "build_backend_incomplete",
        ),
        planned_action(
            "environment.install_seed_package_closure",
            "Install the locked editable seed closure and private solet CLI",
            "package_install",
            "$TARGET",
            "seed_package_closure_incomplete",
        ),
    ]
    return result(
        request,
        status="pending",
        planned_actions=actions,
        evidence_items=evidence_items,
        repair="Approve the complete dependency-closure action set.",
    )
