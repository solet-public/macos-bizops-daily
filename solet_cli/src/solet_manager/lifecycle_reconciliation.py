"""Read-only reconciliation of manager registration and target runtime health."""

from __future__ import annotations

import json
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from .models import JsonValue

_HEALTH_TIMEOUT_SECONDS = 10

type RuntimeState = Literal["healthy", "dead", "absent", "indeterminate"]


@dataclass(frozen=True)
class RuntimeObservation:
    """One bounded observation from the target-local health CLI."""

    state: RuntimeState
    target: str | None
    executable: str | None
    exit_code: int | None
    reported_status: str | None
    detail: str

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "state": self.state,
            "target": self.target,
            "executable": self.executable,
            "exit_code": self.exit_code,
            "reported_status": self.reported_status,
            "detail": self.detail,
        }


@dataclass(frozen=True)
class LifecycleReconciliation:
    """Comparison of the manager's durable registration with runtime reality."""

    classification: str
    agreement: bool
    manager_recorded_state: str
    runtime: RuntimeObservation
    wrong_side: str | None
    safe_next_action: str

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "classification": self.classification,
            "agreement": self.agreement,
            "manager_recorded_state": self.manager_recorded_state,
            "runtime_state": self.runtime.state,
            "wrong_side": self.wrong_side,
            "safe_next_action": self.safe_next_action,
            "runtime_observation": self.runtime.to_dict(),
        }


@dataclass(frozen=True)
class _ClassificationRule:
    classification: str
    agreement: bool
    wrong_side: str | None
    safe_next_action: str


_CLASSIFICATION_RULES: dict[tuple[bool, RuntimeState], _ClassificationRule] = {
    (False, "absent"): _ClassificationRule(
        "absent",
        True,
        None,
        "No manager record or target exists. Review `solet create {name} --dry-run`.",
    ),
    (False, "healthy"): _ClassificationRule(
        "unregistered_runtime_healthy",
        False,
        "manager",
        "Preserve the healthy runtime and reconcile the missing manager record through "
        "reviewed adoption. Do not restart, recreate, or delete the target.",
    ),
    (True, "healthy"): _ClassificationRule(
        "registered_runtime_healthy",
        True,
        None,
        "No runtime reconciliation action is required; registration and health agree.",
    ),
    (True, "dead"): _ClassificationRule(
        "registered_runtime_dead",
        False,
        "runtime",
        "Preserve the manager record. Inspect target-local health and startup logs, then "
        "use the governed start or repair path; do not unregister or recreate.",
    ),
    (False, "dead"): _ClassificationRule(
        "unregistered_runtime_dead",
        False,
        "manager_and_runtime",
        "Do not adopt, start, recreate, or delete this unmanaged unhealthy target. Inspect "
        "runtime health and target identity, then choose a reviewed recovery.",
    ),
    (True, "absent"): _ClassificationRule(
        "registered_target_absent",
        False,
        "manager",
        "Preserve the manager record as evidence and review its stale target before any "
        "create, unregister, or recreation action.",
    ),
    (True, "indeterminate"): _ClassificationRule(
        "registered_runtime_indeterminate",
        False,
        "runtime_observation",
        "Do not mutate the manager record or target. Inspect the target-local health executable "
        "identity and output, then repeat this read-only status check.",
    ),
    (False, "indeterminate"): _ClassificationRule(
        "unregistered_runtime_indeterminate",
        False,
        "runtime_observation",
        "Do not mutate the manager record or target. Inspect the target-local health executable "
        "identity and output, then repeat this read-only status check.",
    ),
}


def reconcile_lifecycle(
    *,
    name: str,
    registered: bool,
    target: Path | None,
) -> LifecycleReconciliation:
    """Compare registry presence with a bounded, read-only target health probe."""

    runtime = observe_runtime(name=name, target=target)
    manager_state = "registered" if registered else "unregistered"
    classification, agreement, wrong_side, next_action = _classify(
        name=name,
        registered=registered,
        runtime=runtime,
    )
    return LifecycleReconciliation(
        classification=classification,
        agreement=agreement,
        manager_recorded_state=manager_state,
        runtime=runtime,
        wrong_side=wrong_side,
        safe_next_action=next_action,
    )


def observe_runtime(*, name: str, target: Path | None) -> RuntimeObservation:
    """Observe runtime health without consulting or changing manager-owned state."""

    if target is None:
        return _indeterminate_observation(
            target=None,
            executable=None,
            detail="No target path was supplied for runtime observation.",
        )
    target_text = str(target)
    if not target.exists():
        return RuntimeObservation(
            state="absent",
            target=target_text,
            executable=None,
            exit_code=None,
            reported_status=None,
            detail="The target path does not exist.",
        )
    executable = _validated_health_executable(target)
    if executable is None:
        return _indeterminate_observation(
            target=target_text,
            executable=str(target / ".venv/bin/solet-bridge"),
            detail="Target or target-local health executable identity is invalid.",
        )
    return _run_health_probe(name=name, target=target, executable=executable)


def _validated_health_executable(target: Path) -> Path | None:
    executable = target / ".venv/bin/solet-bridge"
    try:
        resolved_target = target.resolve(strict=True)
        resolved_executable = executable.resolve(strict=True)
    except OSError:
        return None
    if not _target_identity_valid(target):
        return None
    if not _executable_identity_valid(
        target=target,
        executable=executable,
        resolved_target=resolved_target,
        resolved_executable=resolved_executable,
    ):
        return None
    return executable


def _target_identity_valid(target: Path) -> bool:
    return target.is_absolute() and target.is_dir() and not target.is_symlink()


def _executable_identity_valid(
    *,
    target: Path,
    executable: Path,
    resolved_target: Path,
    resolved_executable: Path,
) -> bool:
    return (
        executable.is_file()
        and not executable.is_symlink()
        and not (target / ".venv").is_symlink()
        and not (target / ".venv/bin").is_symlink()
        and resolved_executable.is_relative_to(resolved_target)
        and os.access(executable, os.X_OK)
    )


def _run_health_probe(*, name: str, target: Path, executable: Path) -> RuntimeObservation:
    executable_text = str(executable)

    env = dict(os.environ)
    env.update({"PYTHONDONTWRITEBYTECODE": "1", "SOLET_NAME": name})
    try:
        completed = subprocess.run(  # noqa: S603 - exact target-local executable is validated
            [executable_text, "health"],
            check=False,
            cwd=target,
            env=env,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=_HEALTH_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired:
        return _indeterminate_observation(
            target=str(target),
            executable=executable_text,
            detail=f"Target-local health probe exceeded {_HEALTH_TIMEOUT_SECONDS} seconds.",
        )
    except OSError as exc:
        return _indeterminate_observation(
            target=str(target),
            executable=executable_text,
            detail=f"Target-local health probe could not execute: {type(exc).__name__}.",
        )
    return _completed_observation(
        target=target,
        executable=executable,
        completed=completed,
    )


def _completed_observation(
    *,
    target: Path,
    executable: Path,
    completed: subprocess.CompletedProcess[str],
) -> RuntimeObservation:
    reported_status = _reported_status(completed.stdout)
    state, detail = _completed_state(completed.returncode, reported_status)
    return RuntimeObservation(
        state=state,
        target=str(target),
        executable=str(executable),
        exit_code=completed.returncode,
        reported_status=reported_status,
        detail=detail,
    )


def _completed_state(
    return_code: int,
    reported_status: str | None,
) -> tuple[RuntimeState, str]:
    if return_code == 0 and reported_status == "healthy":
        return "healthy", "Target-local health probe reported healthy."
    if return_code != 0:
        return "dead", "Target-local health probe exited nonzero."
    if reported_status is not None:
        return "dead", f"Target-local health probe reported {reported_status!r}, not 'healthy'."
    return "indeterminate", "Target-local health probe returned no valid JSON status."


def _indeterminate_observation(
    *,
    target: str | None,
    executable: str | None,
    detail: str,
) -> RuntimeObservation:
    return RuntimeObservation(
        state="indeterminate",
        target=target,
        executable=executable,
        exit_code=None,
        reported_status=None,
        detail=detail,
    )


def _reported_status(stdout: str) -> str | None:
    try:
        payload = json.loads(stdout)
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, dict):
        return None
    status = payload.get("status")
    return status if isinstance(status, str) and status else None


def _classify(
    *,
    name: str,
    registered: bool,
    runtime: RuntimeObservation,
) -> tuple[str, bool, str | None, str]:
    rule = _CLASSIFICATION_RULES[(registered, runtime.state)]
    return (
        rule.classification,
        rule.agreement,
        rule.wrong_side,
        rule.safe_next_action.format(name=name),
    )
