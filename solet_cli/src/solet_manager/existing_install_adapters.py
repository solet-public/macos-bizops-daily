"""Closed existing-install adapter registry and its subprocess vectors.

An adapter is one ``existing::`` operation reference bound to one closed
subprocess vector that executes code from the refreshed target tree (design
section 3).  This registry is not ``AdapterRegistry`` and does not import the
create registry: ``genesis``, ``system_settings`` and ``platform_process``
runners do not exist here, so every create-only edge is structurally
unreachable rather than merely unused.  The seed's dispatcher enumerates the
same ``existing::`` vocabulary; a smoke proves the seed-side subset is
byte-equal to the table in ``existing_install_bundle``.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import cast

from .adapter_protocol import (
    EXISTING_INSTALL_FLOW_ID,
    OperationRequest,
    OperationResult,
)
from .adapter_validation import bounded_redacted
from .errors import AdapterError, AdapterMissingError, AdapterProtocolError, StateConflictError
from .existing_install_bundle import EXISTING_OPERATIONS, ExistingOperation
from .models import CheckpointStatus, JsonValue
from .reconciliation_request import ReconciliationOutcome, parse_reconciliation_response

__all__ = [
    "ATTEST_PROCESS_KEY",
    "BRIDGE_PROCESS_ALLOWLIST",
    "BRIDGE_READ_KINDS",
    "BRIDGE_WRITE_KINDS",
    "KNOWLEDGE_SEARCH_PROCESS_KEY",
    "PS_VECTOR",
    "ExistingInstallAdapterRegistry",
    "bridge_health",
    "invoke_existing_adapter",
    "invoke_instance_bridge",
    "invoke_reconciliation",
    "run_launchctl",
    "run_ps",
    "run_security_metadata",
]

ATTEST_PROCESS_KEY = "service_interface::local_self_deployment_service::attest_runtime_code"
KNOWLEDGE_SEARCH_PROCESS_KEY = "service_interface::knowledge_service::search"
_MIGRATION_PATTERN = re.compile(r"^(service_interface|plugin)::[a-z][a-z0-9_]*::[a-z0-9_]*migrat[a-z0-9_]*$")
#: Manager-side allowlist of platform process KINDS an ``instance_bridge``
#: operation may call (section 3.1).  A bundle names the exact key in its
#: ``public_inputs``; the key must also match one of these closed patterns.
#: The ``*.read`` kinds (Step 6 section 3.6) are the doctor's read-only
#: vocabulary: a migration's own ``dry_run`` postcondition and knowledge search.
BRIDGE_PROCESS_ALLOWLIST: dict[str, re.Pattern[str]] = {
    "migration": _MIGRATION_PATTERN,
    "migration.read": _MIGRATION_PATTERN,
    "knowledge": re.compile(r"^service_interface::knowledge_service::(install|reinstall|remove|refresh)[a-z0-9_]*$"),
    "knowledge.read": re.compile(re.escape(KNOWLEDGE_SEARCH_PROCESS_KEY)),
    "self_deployment.attest": re.compile(re.escape(ATTEST_PROCESS_KEY)),
}
#: Kinds that may mutate platform state; the doctor never invokes one (Step 6 F-DOC-2).
BRIDGE_WRITE_KINDS = frozenset({"migration", "knowledge"})
#: Kinds the doctor may invoke; ``health`` is the bridge's own subcommand (``bridge_health``).
BRIDGE_READ_KINDS = frozenset({"migration.read", "knowledge.read", "self_deployment.attest", "health"})
PS_VECTOR = ("/bin/ps", "-axo", "pid=,lstart=,command=")
_SECURITY = "/usr/bin/security"
_SECURITY_TIMEOUT_SECONDS = 10
_OPERATIONS_BY_REF = {item.operation_ref: item for item in EXISTING_OPERATIONS}
_BRIDGE_TIMEOUT_SECONDS = 120
_LAUNCHCTL = "/bin/launchctl"
_LAUNCHCTL_VERBS = frozenset({"print", "bootout", "bootstrap", "kickstart"})
_SETUP_ADAPTER_MODULE = "github_midwife_plugin.setup_adapter"


@dataclass(frozen=True, slots=True)
class ExistingInstallAdapterRegistry:
    """Closed runner -> vector map for the existing-install flow (section 3.1)."""

    name: str
    target: Path
    bridge_cli_path: Path
    base_python: Path | None

    def operation(self, operation_ref: str) -> ExistingOperation:
        member = _OPERATIONS_BY_REF.get(operation_ref)
        if member is None:
            raise AdapterMissingError(f"{operation_ref!r} is not a member of the closed existing-install registry")
        return member

    def command_for(self, runner: str) -> tuple[str, ...] | None:
        vectors = {
            "bootstrap": self._bootstrap_vector,
            "target_adapter": self._target_adapter_vector,
            "reconciliation": self._target_adapter_vector,
            "instance_bridge": self._bridge_vector,
            "launchctl": lambda: (_LAUNCHCTL,),
        }
        resolver = vectors.get(runner)
        return None if resolver is None else resolver()

    def _bootstrap_vector(self) -> tuple[str, ...] | None:
        bootstrap = self.target / "bootstrap.py"
        if self.base_python is not None and bootstrap.is_file():
            return (str(self.base_python), str(bootstrap), "--operation-adapter")
        return None

    def _target_adapter_vector(self) -> tuple[str, ...] | None:
        target_python = self.target / ".venv" / "bin" / "python3"
        adapter_module = self.target / "plugins" / "github_midwife_plugin" / "src" / "github_midwife_plugin" / "setup_adapter.py"
        if target_python.is_file() and adapter_module.is_file():
            return (str(target_python), "-m", _SETUP_ADAPTER_MODULE)
        return None

    def _bridge_vector(self) -> tuple[str, ...] | None:
        # Always the BRIDGE (``solet-bridge``: call/health), never the manager
        # entry point, which has no ``call`` subcommand.
        if self.bridge_cli_path.name == "solet-bridge" and self.bridge_cli_path.is_file():
            return (str(self.bridge_cli_path),)
        return None


def invoke_existing_adapter(registry: ExistingInstallAdapterRegistry, request: OperationRequest) -> OperationResult:
    """Run one ``existing::`` operation through its bound vector and parse the closed result."""
    request.validate()
    if request.flow_id != EXISTING_INSTALL_FLOW_ID:
        raise AdapterProtocolError("existing-install adapters only speak the existing-install flow")
    member = registry.operation(request.operation_ref)
    if member.side != "seed":
        raise AdapterMissingError(f"{request.operation_ref!r} is a Manager-side operation, not a target adapter")
    command = registry.command_for(member.runner)
    if command is None:
        return OperationResult.blocked(
            request,
            error_kind="adapter_missing",
            repair=f"The target does not provide the {member.runner!r} vector for {request.operation_ref}.",
        )
    started = time.monotonic()
    payload = json.dumps(request.to_dict(), sort_keys=True, separators=(",", ":"))
    try:
        completed = subprocess.run(  # noqa: S603 - vector comes from the closed registry
            list(command),
            input=payload,
            capture_output=True,
            check=False,
            text=True,
            timeout=request.timeout_seconds,
            cwd=registry.target,
            env=_adapter_environment(registry),
        )
    except subprocess.TimeoutExpired:
        return _synthetic_failure(request, "adapter_timeout", int((time.monotonic() - started) * 1000), timed_out=True)
    elapsed = int((time.monotonic() - started) * 1000)
    if completed.returncode != 0:
        return _synthetic_failure(request, "adapter_exit_error", elapsed, exit_code=completed.returncode, stdout=completed.stdout, stderr=completed.stderr)
    try:
        raw: object = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise AdapterProtocolError(f"adapter stdout is not one JSON result: {exc}") from exc
    if not isinstance(raw, dict) or not all(isinstance(key, str) for key in raw):
        raise AdapterProtocolError("adapter stdout must be one JSON object")
    return OperationResult.from_dict(cast(dict[str, JsonValue], raw), request)


def invoke_reconciliation(
    registry: ExistingInstallAdapterRegistry, envelope: dict[str, JsonValue], *, timeout_seconds: int
) -> ReconciliationOutcome:
    """Feed the fixed reconciliation envelope to the refreshed target's adapter executable."""
    command = registry.command_for("reconciliation")
    if command is None:
        raise AdapterMissingError("the target does not provide the reconciliation vector")
    try:
        completed = subprocess.run(  # noqa: S603 - vector comes from the closed registry
            list(command),
            input=json.dumps(envelope, sort_keys=True, separators=(",", ":")),
            capture_output=True,
            check=False,
            text=True,
            timeout=timeout_seconds,
            cwd=registry.target,
            env=_adapter_environment(registry),
        )
    except subprocess.TimeoutExpired as exc:
        raise AdapterError("reconciliation adapter timed out before answering") from exc
    if completed.returncode != 0:
        raise AdapterError(f"reconciliation adapter exited {completed.returncode}: {bounded_redacted(completed.stderr)[:512]}")
    return parse_reconciliation_response(completed.stdout)


def invoke_instance_bridge(
    registry: ExistingInstallAdapterRegistry,
    process_key: str,
    arguments: dict[str, JsonValue],
    *,
    kind: str,
    timeout_seconds: int = _BRIDGE_TIMEOUT_SECONDS,
) -> dict[str, JsonValue]:
    """Call one allowlisted platform process over the instance's bridge CLI.

    Returns the process result's ``data`` object.  A refused or failed call is
    an ``AdapterError``; the caller classifies it.
    """
    pattern = BRIDGE_PROCESS_ALLOWLIST.get(kind)
    if pattern is None or pattern.fullmatch(process_key) is None:
        raise StateConflictError(f"process key {process_key!r} is not allowlisted under kind {kind!r}")
    command = registry.command_for("instance_bridge")
    if command is None:
        raise AdapterMissingError("the target does not provide the bridge CLI vector")
    argv = (*command, "call", "--timeout", str(timeout_seconds), process_key, json.dumps(arguments, sort_keys=True, separators=(",", ":")))
    envelope = _run_bridge(argv, registry, timeout_seconds)
    result = envelope.get("result")
    if not isinstance(result, dict) or result.get("success") is not True:
        detail = envelope.get("error_message") if envelope.get("error_message") else (result.get("error") if isinstance(result, dict) else None)
        raise AdapterError(f"bridge call {process_key} did not succeed: {bounded_redacted(str(detail))[:512]}")
    data = result.get("data")
    if not isinstance(data, dict):
        raise AdapterProtocolError(f"bridge call {process_key} returned no data object")
    return cast(dict[str, JsonValue], data)


def bridge_health(registry: ExistingInstallAdapterRegistry, *, timeout_seconds: int = 30) -> dict[str, JsonValue]:
    """Read the bridge's own health envelope; the readiness signal is top-level ``status``."""
    command = registry.command_for("instance_bridge")
    if command is None:
        raise AdapterMissingError("the target does not provide the bridge CLI vector")
    return _run_bridge((*command, "health"), registry, timeout_seconds)


def run_launchctl(
    registry: ExistingInstallAdapterRegistry, verb: str, *arguments: str, timeout_seconds: int = 60
) -> subprocess.CompletedProcess[str]:
    """Run one closed ``launchctl`` vector; the verb set is fixed and the label is the caller's."""
    if verb not in _LAUNCHCTL_VERBS:
        raise StateConflictError(f"launchctl verb {verb!r} is outside the closed vector set")
    command = registry.command_for("launchctl")
    if command is None:
        raise AdapterMissingError("launchctl vector is unavailable")
    return subprocess.run(  # noqa: S603 - closed verb set, inventory label
        (*command, verb, *arguments),
        capture_output=True,
        check=False,
        text=True,
        timeout=timeout_seconds,
        stdin=subprocess.DEVNULL,
    )


def run_ps(timeout_seconds: int = 30) -> subprocess.CompletedProcess[str]:
    """The one closed process-table vector the doctor reads (Step 6 section 3.3 section 11)."""
    return subprocess.run(  # noqa: S603 - fixed vector, no caller input
        PS_VECTOR,
        capture_output=True,
        check=False,
        text=True,
        timeout=timeout_seconds,
        stdin=subprocess.DEVNULL,
    )


def run_security_metadata(service: str, account: str) -> subprocess.CompletedProcess[str]:
    """Keychain metadata only -- never ``-w`` -- so no secret value is read (doctor_credential_copy_census precedent)."""
    if not service or not account or service.startswith("-") or account.startswith("-"):
        raise StateConflictError("keychain metadata probe requires a plain service and account")
    return subprocess.run(  # noqa: S603 - closed vector; the two arguments are inventory identities
        (_SECURITY, "find-generic-password", "-s", service, "-a", account),
        capture_output=True,
        check=False,
        text=True,
        timeout=_SECURITY_TIMEOUT_SECONDS,
        stdin=subprocess.DEVNULL,
    )


def _run_bridge(argv: tuple[str, ...], registry: ExistingInstallAdapterRegistry, timeout_seconds: int) -> dict[str, JsonValue]:
    try:
        completed = subprocess.run(  # noqa: S603 - bridge path comes from the inventory
            list(argv),
            capture_output=True,
            check=False,
            text=True,
            timeout=timeout_seconds + 30,
            stdin=subprocess.DEVNULL,
            env=_adapter_environment(registry),
        )
    except subprocess.TimeoutExpired as exc:
        raise AdapterError("bridge CLI timed out") from exc
    if completed.returncode != 0:
        raise AdapterError(f"bridge CLI exited {completed.returncode}: {bounded_redacted(completed.stderr)[:512]}")
    try:
        raw: object = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise AdapterProtocolError(f"bridge CLI stdout is not JSON: {exc}") from exc
    if not isinstance(raw, dict):
        raise AdapterProtocolError("bridge CLI stdout must be one JSON object")
    return cast(dict[str, JsonValue], raw)


def _adapter_environment(registry: ExistingInstallAdapterRegistry) -> dict[str, str]:
    environment = {name: value for name, value in os.environ.items() if name in {"HOME", "PATH", "LANG", "TMPDIR", "USER"} or name.startswith("HOMEBREW_")}
    environment["SOLET_NAME"] = registry.name
    environment["GIT_TERMINAL_PROMPT"] = "0"
    return environment


def _synthetic_failure(
    request: OperationRequest,
    error_kind: str,
    elapsed: int,
    *,
    timed_out: bool = False,
    exit_code: int | None = None,
    stdout: str = "",
    stderr: str = "",
) -> OperationResult:
    return OperationResult(
        request.request_id,
        request.operation_id,
        request.phase,
        request.probe_purpose,
        CheckpointStatus.FAILED,
        error_kind,
        False,
        exit_code,
        timed_out,
        elapsed,
        bounded_redacted(stdout),
        bounded_redacted(stderr),
        (),
        (),
        (),
        "Inspect the redacted adapter diagnostics and repair the target-local operation.",
    )
