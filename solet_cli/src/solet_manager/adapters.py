"""Fail-closed target-local adapter registry and subprocess transport."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

from .adapter_protocol import (
    DiscoveredCandidate,
    OperationRequest,
    OperationResult,
    PlannedAction,
)
from .adapter_validation import FORMULA_MARKER, bounded_redacted
from .errors import AdapterProtocolError, StateConflictError
from .models import CheckpointStatus

__all__ = [
    "AdapterRegistry",
    "DiscoveredCandidate",
    "OperationRequest",
    "OperationResult",
    "PlannedAction",
    "invoke_adapter",
    "resolve_long_lived_python",
]


class AdapterRegistry:
    """Closed mapping from reviewed flow runners to target-local vectors."""

    def __init__(self, *, target: Path, base_python: Path | None = None) -> None:
        self.target = target
        self.base_python = base_python

    def command_for(self, runner: str) -> tuple[str, ...] | None:
        if runner == "bootstrap":
            bootstrap = self.target / "bootstrap.py"
            if self.base_python is not None and bootstrap.is_file():
                return (str(self.base_python), str(bootstrap), "--operation-adapter")
            return None
        adapter_module = self.target / "plugins" / "github_midwife_plugin" / "src" / "github_midwife_plugin" / "setup_adapter.py"
        target_python = self.target / ".venv" / "bin" / "python3"
        target_runners = {
            "external_cli",
            "genesis",
            "hydration",
            "platform_process",
            "system",
            "system_settings",
        }
        if runner in target_runners and target_python.is_file() and adapter_module.is_file():
            return (
                str(target_python),
                "-m",
                "github_midwife_plugin.setup_adapter",
            )
        return None

    def refresh_base_python(self) -> Path | None:
        """Retain a bootstrap-selected interpreter or resolve one when absent."""
        if self.base_python is None:
            self.base_python = resolve_long_lived_python()
        return self.base_python


_PYTHON_RUNTIME_OPERATION_ID = "install_python_runtime"
_PYTHON_RUNTIME_OPERATION_REF = "setup::python.install_313"
_CAPTURE_ROOT_ENV = "SOLET_ADAPTER_ENVELOPE_CAPTURE_ROOT"
_HOMEBREW_CANDIDATES = (
    "/opt/homebrew/bin/brew",
    "/usr/local/bin/brew",
)
_PYTHON_RUNTIME_INSTALLER_ARGS = ("install", "python@3.13")
_PYTHON_RUNTIME_PREFIX_ARGS = ("--prefix", "python@3.13")
_HOMEBREW_GUARD_ENV = {
    "HOMEBREW_NO_AUTO_UPDATE": "1",
    "HOMEBREW_NO_INSTALLED_DEPENDENTS_CHECK": "1",
    "HOMEBREW_NO_INSTALL_UPGRADE": "1",
}


def _homebrew_environment() -> dict[str, str]:
    """Preserve adopter Homebrew policy while enforcing shipped safeguards."""

    environment = {name: value for name, value in os.environ.items() if name.startswith("HOMEBREW_")}
    environment.update(_HOMEBREW_GUARD_ENV)
    return environment


def _capture_path(request: OperationRequest, kind: str) -> Path | None:
    """Return the explicit receipt path for one adapter envelope.

    Capture is disabled unless a guest harness supplies an absolute root.  The
    regular create path therefore retains no new artifact or filesystem side
    effect.
    """

    root_text = os.environ.get(_CAPTURE_ROOT_ENV)
    if root_text is None:
        return None
    root = Path(root_text)
    if not root.is_absolute() or kind not in {"request", "result"}:
        raise StateConflictError("adapter envelope capture root or kind is invalid")
    return root / f"{request.request_id}-{kind}.json"


def _capture_envelope(request: OperationRequest, kind: str, envelope: object) -> None:
    """Atomically write a guest-requested public adapter envelope."""

    path = _capture_path(request, kind)
    if path is None:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(envelope, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)


def _homebrew_install_items(lines: list[str], *, kind: str, name: str, approved_closure: tuple[str, ...] = ()) -> list[str] | None:
    headers = [index for index, line in enumerate(lines) if line.startswith("Would install")]
    expected_count = 1 + len(approved_closure)
    allowed_headers = {
        f"Would install {expected_count} {kind}:",
        f"Would install {expected_count} {kind}s:",
    }
    if kind == "formula":
        allowed_headers.add(f"Would install {expected_count} formulae:")
    if len(headers) != 1 or lines[headers[0]] not in allowed_headers:
        return None
    items: list[str] = []
    for line in lines[headers[0] + 1 :]:
        if line.startswith("Would "):
            break
        if line and not line.startswith("==>"):
            items.append(line)
    return items


def _homebrew_install_plan_allowed(
    output: str,
    name: str,
    *,
    kind: str = "formula",
    approved_closure: tuple[str, ...] = (),
) -> bool:
    """Accept only the declared Homebrew kind, root, and dependency closure."""

    if not _valid_homebrew_acquisition(kind, name, approved_closure):
        return False
    approved = {name, *approved_closure}
    lines = [line.strip() for line in output.splitlines()]
    items = _homebrew_install_items(lines, kind=kind, name=name, approved_closure=approved_closure)
    return not _has_prohibited_homebrew_mutation(lines) and items is not None and len(items) == len(approved) and set(items) == approved


def _valid_homebrew_acquisition(kind: str, name: str, approved_closure: tuple[str, ...]) -> bool:
    """Validate a declared package root and its duplicate-free closure."""

    return kind in {"formula", "cask"} and bool(name) and all(approved_closure) and len({name, *approved_closure}) == 1 + len(approved_closure)


def _has_prohibited_homebrew_mutation(lines: list[str]) -> bool:
    """Return whether a dry-run includes a non-install package mutation."""

    prohibited = ("Would upgrade", "Would reinstall", "Would remove", "Would unlink")
    return any(line.startswith(prohibited) for line in lines)


def _python_install_plan_failure(request: OperationRequest, brew: str, environment: dict[str, str]) -> OperationResult | None:
    """Return a public failed result when the read-only package plan is unsafe."""

    try:
        dry_run = subprocess.run(  # noqa: S603 - fixed reviewed command vector
            [brew, "install", "--dry-run", "python@3.13"],
            capture_output=True,
            check=False,
            text=True,
            timeout=request.timeout_seconds,
            env=environment,
        )
    except subprocess.TimeoutExpired:
        return _python_runtime_result(
            request,
            status=CheckpointStatus.FAILED,
            error_kind="python_runtime_install_plan_timeout",
            retry_safe=True,
            exit_code=None,
            repair="Retry the Homebrew Python installation dry-run after inspecting package state.",
        )
    except OSError:
        return _python_runtime_result(
            request,
            status=CheckpointStatus.FAILED,
            error_kind="python_runtime_install_plan_unavailable",
            retry_safe=True,
            exit_code=None,
            repair="Repair the Homebrew executable before retrying the installation dry-run.",
        )
    if dry_run.returncode == 0 and _homebrew_install_plan_allowed(f"{dry_run.stdout}\n{dry_run.stderr}", "python@3.13"):
        return None
    return _python_runtime_result(
        request,
        status=CheckpointStatus.FAILED,
        error_kind="python_runtime_install_plan_rejected",
        retry_safe=True,
        exit_code=dry_run.returncode,
        repair="Homebrew proposed a mutation outside the approved Python formula action.",
    )


def _is_python_runtime_bootstrap(request: OperationRequest, runner: str) -> bool:
    return runner == "bootstrap" and request.operation_id == _PYTHON_RUNTIME_OPERATION_ID and request.operation_ref == _PYTHON_RUNTIME_OPERATION_REF


def _python_runtime_result(
    request: OperationRequest,
    *,
    status: CheckpointStatus,
    error_kind: str | None = None,
    retry_safe: bool = True,
    exit_code: int | None = 0,
    planned_actions: tuple[PlannedAction, ...] = (),
    repair: str | None = None,
) -> OperationResult:
    return OperationResult(
        request.request_id,
        request.operation_id,
        request.phase,
        request.probe_purpose,
        status,
        error_kind,
        retry_safe,
        exit_code,
        False,
        0,
        "",
        "",
        planned_actions,
        (),
        (),
        repair,
    )


def _brew_candidate_works(candidate: str, *, timeout_seconds: int) -> bool:
    if not Path(candidate).is_absolute():
        return False
    try:
        completed = subprocess.run(  # noqa: S603 - candidate is closed and absolute
            [candidate, "--version"],
            capture_output=True,
            check=False,
            text=True,
            timeout=timeout_seconds,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return completed.returncode == 0


def resolve_brew_executable(*, timeout_seconds: int) -> str | None:
    """Resolve and validate Homebrew through PATH and standard keg locations."""
    candidates = (shutil.which("brew"), *_HOMEBREW_CANDIDATES)
    for candidate in dict.fromkeys(item for item in candidates if item is not None):
        if _brew_candidate_works(candidate, timeout_seconds=timeout_seconds):
            return candidate
    return None


def _select_installed_homebrew_python(
    request: OperationRequest,
    brew: str,
) -> Path | None:
    """Select and validate only the reviewed Python formula path after install."""
    try:
        completed = subprocess.run(  # noqa: S603 - fixed reviewed command vector
            [brew, *_PYTHON_RUNTIME_PREFIX_ARGS],
            capture_output=True,
            check=False,
            text=True,
            timeout=request.timeout_seconds,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if completed.returncode != 0:
        return None
    prefix = Path(completed.stdout.strip())
    if not prefix.is_absolute():
        return None
    candidate = prefix / "bin" / "python3.13"
    manager_prefix = Path(sys.prefix).resolve(strict=False)
    if not _eligible_python(candidate, candidate.resolve(strict=False), manager_prefix):
        return None
    return candidate if _is_python_313(candidate) else None


def _bootstrap_python_runtime(
    registry: AdapterRegistry,
    request: OperationRequest,
) -> OperationResult:
    """Run the one reviewed pre-interpreter installer, then enter the closed adapter."""
    selected = resolve_long_lived_python()
    if selected is not None:
        registry.base_python = selected
        return invoke_adapter(registry, runner="bootstrap", request=request)
    brew = resolve_brew_executable(timeout_seconds=request.timeout_seconds)
    if brew is None:
        return OperationResult.blocked(
            request,
            error_kind="homebrew_missing",
            repair="Install Homebrew from its reviewed official distribution path, then resume.",
        )
    if request.phase == "probe":
        action = PlannedAction(
            "python.install_homebrew_formula",
            "Install Homebrew Python 3.13",
            "package_install",
            "homebrew:python@3.13",
            True,
            "python_runtime_resolution_required",
        )
        return _python_runtime_result(
            request,
            status=CheckpointStatus.PENDING,
            planned_actions=(action,),
            repair="Approve the reviewed Homebrew Python 3.13 installation action.",
        )
    environment = _homebrew_environment()
    plan_failure = _python_install_plan_failure(request, brew, environment)
    if plan_failure is not None:
        return plan_failure
    try:
        completed = subprocess.run(  # noqa: S603 - fixed reviewed command vector
            [brew, *_PYTHON_RUNTIME_INSTALLER_ARGS],
            capture_output=True,
            check=False,
            text=True,
            timeout=request.timeout_seconds,
            env=environment,
        )
    except subprocess.TimeoutExpired:
        return _python_runtime_result(
            request,
            status=CheckpointStatus.FAILED,
            error_kind="python_runtime_install_timeout",
            retry_safe=True,
            exit_code=None,
            repair="Retry the approved Homebrew Python 3.13 installation action.",
        )
    except OSError:
        return _python_runtime_result(
            request,
            status=CheckpointStatus.FAILED,
            error_kind="python_runtime_install_unavailable",
            retry_safe=True,
            exit_code=None,
            repair="Repair the Homebrew executable and retry the approved Python installation action.",
        )
    if completed.returncode != 0:
        return _python_runtime_result(
            request,
            status=CheckpointStatus.FAILED,
            error_kind="python_runtime_install_failed",
            retry_safe=True,
            exit_code=completed.returncode,
            repair="Inspect the Homebrew Python 3.13 installation failure and retry the approved action.",
        )
    selected = _select_installed_homebrew_python(request, brew)
    if selected is None:
        return _python_runtime_result(
            request,
            status=CheckpointStatus.FAILED,
            error_kind="python_runtime_resolution_failed",
            retry_safe=True,
            exit_code=0,
            repair="Verify Homebrew installed an eligible Python 3.13 outside the manager formula keg, then resume.",
        )
    registry.base_python = selected
    return invoke_adapter(registry, runner="bootstrap", request=request)


def invoke_adapter(
    registry: AdapterRegistry,
    *,
    runner: str,
    request: OperationRequest,
) -> OperationResult:
    request.validate()
    request_payload = request.to_dict()
    _capture_envelope(request, "request", request_payload)
    command = registry.command_for(runner)
    if command is None:
        if registry.base_python is None and _is_python_runtime_bootstrap(request, runner):
            result = _bootstrap_python_runtime(registry, request)
            _capture_envelope(request, "result", result.to_dict())
            return result
        result = OperationResult.blocked(
            request,
            error_kind="adapter_missing",
            repair=f"Install the reviewed {runner!r} target-local adapter and resume.",
        )
        _capture_envelope(request, "result", result.to_dict())
        return result
    started = time.monotonic()
    try:
        completed = subprocess.run(  # noqa: S603 - vector comes from closed registry
            list(command),
            input=json.dumps(request.to_dict(), sort_keys=True, separators=(",", ":")),
            capture_output=True,
            check=False,
            text=True,
            timeout=request.timeout_seconds,
        )
    except subprocess.TimeoutExpired:
        elapsed = int((time.monotonic() - started) * 1000)
        result = _timeout_result(request, elapsed)
        _capture_envelope(request, "result", result.to_dict())
        return result
    elapsed = int((time.monotonic() - started) * 1000)
    if completed.returncode != 0:
        result = _exit_result(request, completed, elapsed)
        _capture_envelope(request, "result", result.to_dict())
        return result
    try:
        raw: object = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise AdapterProtocolError(f"adapter stdout is not one JSON result: {exc}") from exc
    if not isinstance(raw, dict) or not all(isinstance(key, str) for key in raw):
        raise AdapterProtocolError("adapter stdout must be one JSON object")
    result = OperationResult.from_dict(raw, request)
    _capture_envelope(request, "result", raw)
    return result


def _timeout_result(request: OperationRequest, elapsed: int) -> OperationResult:
    return OperationResult(
        request.request_id,
        request.operation_id,
        request.phase,
        request.probe_purpose,
        CheckpointStatus.FAILED,
        "adapter_timeout",
        False,
        None,
        True,
        elapsed,
        "",
        "",
        (),
        (),
        (),
        "Inspect the target-local adapter and retry after resolving the timeout.",
    )


def _exit_result(
    request: OperationRequest,
    completed: subprocess.CompletedProcess[str],
    elapsed: int,
) -> OperationResult:
    return OperationResult(
        request.request_id,
        request.operation_id,
        request.phase,
        request.probe_purpose,
        CheckpointStatus.FAILED,
        "adapter_exit_error",
        False,
        completed.returncode,
        False,
        elapsed,
        bounded_redacted(completed.stdout),
        bounded_redacted(completed.stderr),
        (),
        (),
        (),
        "Inspect the redacted adapter diagnostics and repair the target-local operation.",
    )


def resolve_long_lived_python() -> Path | None:
    """Resolve Python 3.13 outside the manager formula keg and execute it."""

    candidates = [
        Path("/opt/homebrew/bin/python3.13"),
        Path("/usr/local/bin/python3.13"),
    ]
    discovered = shutil.which("python3.13")
    if discovered:
        candidates.append(Path(discovered))
    manager_prefix = Path(sys.prefix).resolve(strict=False)
    for candidate in candidates:
        resolved = candidate.resolve(strict=False)
        if not _eligible_python(candidate, resolved, manager_prefix):
            continue
        if _is_python_313(candidate):
            return candidate
    return None


def _eligible_python(candidate: Path, resolved: Path, manager_prefix: Path) -> bool:
    return candidate.is_file() and FORMULA_MARKER not in str(candidate) and manager_prefix not in resolved.parents


def _is_python_313(candidate: Path) -> bool:
    try:
        result = subprocess.run(
            [str(candidate), "--version"],
            capture_output=True,
            check=False,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    output = f"{result.stdout} {result.stderr}".strip()
    return result.returncode == 0 and output.startswith("Python 3.13")
