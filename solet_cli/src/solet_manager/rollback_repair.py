"""Three-phase orchestration for the one allowlisted rollback transaction."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from .models import CommandResult, ExitCode, JsonValue
from .paths import ManagerPaths
from .rollback_repair_contract import (
    RECIPE_ID,
    RECIPE_VERSION,
    RepairPlan,
    RepairRefusedError,
    artifact_identity,
    build_plan,
    load_and_pin_record,
    load_private_object,
    preflight_refusal,
    recheck_at_action,
    sha256_file,
)
from .rollback_repair_storage import (
    RollbackPostconditionError,
    append_event,
    atomic_restore,
    has_event,
    has_terminal,
    journal_path,
    latest_receipt,
    pointer,
    read_journal,
    reconstruct_action_receipt,
    recovery_path,
    retain_recovery_generation,
    verify_postconditions,
)
from .state_io import instance_lock

Mutator = Callable[[RepairPlan, Path, Path], dict[str, JsonValue]]
ActTimeHook = Callable[[RepairPlan], None]

# The focused smoke imports these to inject discriminating mutator controls.
_atomic_restore = atomic_restore


class RollbackRepairExecutor:
    """Verify, journal, roll back, and independently prove one artifact."""

    def __init__(
        self,
        paths: ManagerPaths,
        *,
        mutator: Mutator | None = None,
        act_time_hook: ActTimeHook | None = None,
    ) -> None:
        self._paths = paths
        self._mutator = atomic_restore if mutator is None else mutator
        self._act_time_hook = act_time_hook

    def run(self, failure_record: Path, supplied_digest: str) -> CommandResult:
        """Run the closed recipe and return a stable typed terminal state."""
        try:
            raw, record_path, record_digest = load_and_pin_record(
                failure_record,
                supplied_digest,
            )
            name, _ = preflight_refusal(raw, record_path, self._paths)
            with instance_lock(self._paths.lock_path(name)):
                plan = build_plan(raw, record_path, record_digest, self._paths)
                journal = journal_path(self._paths, plan.failure_id)
                recovery = recovery_path(self._paths, plan.failure_id)
                events = read_journal(journal, plan)
                if self._act_time_hook is not None:
                    self._act_time_hook(plan)
                recheck_at_action(plan, raw, self._paths)
                current = artifact_identity(plan.artifact.path)
                if current.digest == plan.expected_digest:
                    return self._finish_reentry(plan, journal, recovery, events)
                if current.digest != plan.fault_digest:
                    raise RepairRefusedError(
                        "changed_failure_state",
                        "the recorded failing artifact is no longer present",
                        evidence=str(plan.artifact.path),
                    )
                return self._perform(plan, journal, recovery, events)
        except RepairRefusedError as exc:
            return refusal_result(
                failure_record,
                exc,
                supplied_digest=supplied_digest,
            )

    def _perform(
        self,
        plan: RepairPlan,
        journal: Path,
        recovery: Path,
        events: list[dict[str, JsonValue]],
    ) -> CommandResult:
        if has_terminal(events, "repair_worked"):
            return self._finish_reentry(plan, journal, recovery, events)
        record_preconditions(plan, journal, events)
        retain_recovery_generation(plan, recovery)
        record_recovery(plan, journal, recovery, events)
        raw = load_private_object(plan.record_path)
        recheck_at_action(plan, raw, self._paths)
        append_event(
            journal,
            plan,
            phase="action",
            status="mutation_started",
            evidence={
                "inverse_operation": "atomic_restore_authenticated_before_image",
                "touched_set": list(plan.touched_set),
                "recovery": pointer(recovery, plan.fault_digest),
            },
            next_action="complete_or_recover_action",
        )
        stage = plan.artifact.path.parent / (f".{plan.artifact.path.name}.{plan.failure_id}.repair")
        try:
            receipt = self._mutator(plan, stage, recovery)
        except Exception as exc:
            return self._finish_action_exception(plan, journal, recovery, exc)
        append_event(
            journal,
            plan,
            phase="action",
            status="action_receipt_recorded",
            evidence=receipt,
            next_action="verify_postconditions",
        )
        return self._verify(
            plan,
            journal,
            recovery,
            receipt,
            replay="first_invocation",
        )

    def _finish_action_exception(
        self,
        plan: RepairPlan,
        journal: Path,
        recovery: Path,
        exc: Exception,
    ) -> CommandResult:
        receipt: dict[str, JsonValue] | None = None
        mutation_count = 0
        try:
            observed = artifact_identity(plan.artifact.path)
        except RepairRefusedError:
            mutation_count = 1
        else:
            if observed.digest == plan.expected_digest:
                mutation_count = 1
                receipt = reconstruct_action_receipt(plan, recovery)
                append_event(
                    journal,
                    plan,
                    phase="action",
                    status="action_receipt_recorded",
                    evidence=receipt,
                    next_action="record_interrupted_action_then_reverify",
                )
            elif observed.digest != plan.fault_digest:
                mutation_count = 1
        return action_failed_result(
            plan,
            journal,
            exc,
            receipt=receipt,
            mutation_count=mutation_count,
            recovery=recovery,
        )

    def _finish_reentry(
        self,
        plan: RepairPlan,
        journal: Path,
        recovery: Path,
        events: list[dict[str, JsonValue]],
    ) -> CommandResult:
        if not recovery.is_file() or sha256_file(recovery) != plan.fault_digest:
            raise RepairRefusedError(
                "recovery_generation",
                "re-entry found no authenticated retained prior generation",
                evidence=str(recovery),
            )
        receipt = latest_receipt(events)
        if receipt is None:
            if not has_event(events, "mutation_started"):
                raise RepairRefusedError(
                    "action_receipt",
                    "re-entry has no durable action start or authenticated receipt",
                    evidence=str(journal),
                )
            receipt = reconstruct_action_receipt(plan, recovery)
            append_event(
                journal,
                plan,
                phase="action",
                status="action_receipt_recorded",
                evidence=receipt,
                next_action="verify_postconditions",
            )
        verified = self._verify(
            plan,
            journal,
            recovery,
            receipt,
            replay="already_repaired",
        )
        if verified.exit_code != ExitCode.OK:
            return verified
        return result(
            plan,
            status="already_repaired",
            exit_code=ExitCode.OK,
            repair_worked=True,
            action={"status": "no_op", "receipt": receipt},
            postcondition=verified.data["postcondition"],
            mutation_count=0,
            reason=None,
            next_action="none",
            replay="independently_reverified_no_op",
            recovery=recovery,
        )

    def _verify(
        self,
        plan: RepairPlan,
        journal: Path,
        recovery: Path,
        receipt: dict[str, JsonValue] | None,
        *,
        replay: str,
    ) -> CommandResult:
        append_event(
            journal,
            plan,
            phase="postcondition",
            status="postconditions_started",
            evidence={"oracle": "independent_filesystem_reopen_v1"},
            next_action="complete_independent_verification",
        )
        try:
            observed = verify_postconditions(plan)
        except RollbackPostconditionError as exc:
            return postcondition_failed_result(
                plan,
                journal,
                receipt,
                exc,
                replay=replay,
            )
        append_event(
            journal,
            plan,
            phase="postcondition",
            status="repair_worked",
            evidence=observed,
            next_action="none",
        )
        return result(
            plan,
            status="repaired",
            exit_code=ExitCode.OK,
            repair_worked=True,
            action={"status": "completed", "receipt": receipt},
            postcondition={"status": "verified", "evidence": observed},
            mutation_count=0 if replay == "already_repaired" else 1,
            reason=None,
            next_action="none",
            replay=replay,
            recovery=recovery,
        )


def record_preconditions(
    plan: RepairPlan,
    journal: Path,
    events: list[dict[str, JsonValue]],
) -> None:
    if has_event(events, "preconditions_verified"):
        return
    append_event(
        journal,
        plan,
        phase="precondition",
        status="preconditions_verified",
        evidence={
            "failure_record": pointer(plan.record_path, plan.record_digest),
            "artifact_before": plan.artifact.public(),
            "restore_source": plan.restore_source.public(),
            "collateral_census": plan.collateral_before,
        },
        next_action="retain_prior_generation",
    )


def record_recovery(
    plan: RepairPlan,
    journal: Path,
    recovery: Path,
    events: list[dict[str, JsonValue]],
) -> None:
    if has_event(events, "recovery_generation_durable"):
        return
    append_event(
        journal,
        plan,
        phase="precondition",
        status="recovery_generation_durable",
        evidence={"recovery": pointer(recovery, plan.fault_digest)},
        next_action="dispatch_declared_inverse",
    )


def action_failed_result(
    plan: RepairPlan,
    journal: Path,
    exc: Exception,
    *,
    receipt: dict[str, JsonValue] | None,
    mutation_count: int,
    recovery: Path,
) -> CommandResult:
    append_event(
        journal,
        plan,
        phase="action",
        status="action_failed",
        evidence={
            "exception_type": type(exc).__name__,
            "mutation_count": mutation_count,
            "action_receipt": receipt,
        },
        next_action="inspect_journal_then_retry_same_recipe",
    )
    return result(
        plan,
        status="action_failed",
        exit_code=ExitCode.FAILED,
        repair_worked=False,
        action={"status": "action_failed", "receipt": receipt},
        postcondition={"status": "not_started"},
        mutation_count=mutation_count,
        reason="declared inverse action failed",
        next_action="inspect the repair journal before retrying this exact recipe",
        error_kind="rollback_action_failed",
        recovery=recovery,
    )


def postcondition_failed_result(
    plan: RepairPlan,
    journal: Path,
    receipt: dict[str, JsonValue] | None,
    exc: RollbackPostconditionError,
    *,
    replay: str,
) -> CommandResult:
    evidence: dict[str, JsonValue] = {
        "reason": str(exc),
        "oracle": "independent_filesystem_reopen_v1",
    }
    append_event(
        journal,
        plan,
        phase="postcondition",
        status="postcondition_failed",
        evidence=evidence,
        next_action="preserve_journal_and_escalate",
    )
    return result(
        plan,
        status="postcondition_failed",
        exit_code=ExitCode.FAILED,
        repair_worked=False,
        action={"status": "completed", "receipt": receipt},
        postcondition={"status": "postcondition_failed", "evidence": evidence},
        mutation_count=0 if replay == "already_repaired" else 1,
        reason=str(exc),
        next_action="preserve the journal and escalate; do not rerun a forward action",
        error_kind="rollback_postcondition_failed",
        replay=replay,
    )


def result(
    plan: RepairPlan,
    *,
    status: str,
    exit_code: ExitCode,
    repair_worked: bool,
    action: dict[str, JsonValue],
    postcondition: JsonValue,
    mutation_count: int,
    reason: str | None,
    next_action: str,
    error_kind: str | None = None,
    replay: str = "not_replayed",
    recovery: Path | None = None,
) -> CommandResult:
    return CommandResult(
        kind="rollback_repair",
        status=status,
        message=(
            "Rollback independently verified."
            if repair_worked
            else reason or "Rollback did not verify."
        ),
        exit_code=exit_code,
        error_kind=error_kind,
        data={
            "subject": {"name": plan.instance_name, "target": str(plan.target)},
            "transaction": {
                "path": str(plan.manager_transaction_path),
                "operation_id": plan.manager_operation_id,
                "sha256": plan.transaction_digest,
                "failure_id": plan.failure_id,
            },
            "recipe": {"id": RECIPE_ID, "version": RECIPE_VERSION},
            "input_fingerprint": plan.input_fingerprint,
            "touched_set": list(plan.touched_set),
            "precondition": {
                "status": "verified",
                "failure_record": pointer(plan.record_path, plan.record_digest),
            },
            "action": action,
            "postcondition": postcondition,
            "mutation_count": mutation_count,
            "before": (pointer(recovery, plan.fault_digest) if recovery is not None else None),
            "after": (
                pointer(plan.artifact.path, plan.expected_digest)
                if repair_worked
                or (mutation_count > 0 and action.get("receipt") is not None)
                else None
            ),
            "recovery": (pointer(recovery, plan.fault_digest) if recovery is not None else None),
            "repair_worked": repair_worked,
            "reason": reason,
            "next_action": next_action,
            "replay": replay,
        },
    )


def refusal_result(
    record: Path,
    exc: RepairRefusedError,
    *,
    supplied_digest: str,
) -> CommandResult:
    protected = exc.guard.startswith("protected")
    return CommandResult(
        kind="rollback_repair",
        status="refused",
        message=exc.reason,
        exit_code=ExitCode.HUMAN_ACTION,
        error_kind=("repair_refused_protected_subject" if protected else "rollback_repair_refused"),
        data={
            "subject": {"failure_record": str(record)},
            "transaction": None,
            "recipe": {"id": RECIPE_ID, "version": RECIPE_VERSION},
            "input_fingerprint": supplied_digest,
            "touched_set": [],
            "precondition": {
                "status": "refused",
                "guard": exc.guard,
                "evidence": exc.evidence,
                "reason": exc.reason,
            },
            "action": {"status": "not_started", "receipt": None},
            "postcondition": {"status": "not_started"},
            "mutation_count": 0,
            "before": None,
            "after": None,
            "recovery": None,
            "repair_worked": False,
            "reason": exc.reason,
            "next_action": "preserve evidence and obtain an operator decision",
            "replay": "refused_before_action",
        },
    )
