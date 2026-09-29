"""Resumable operation execution after a preview fingerprint is approved."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path

from .adapters import AdapterRegistry, OperationResult, invoke_adapter, resolve_long_lived_python
from .completion_verifier import rebind_completion_probes, run_completion_probes
from .config import CreateConfig
from .contracts import ContractBundle
from .create_applied_edits import record_applied_edits, tracked_edit_digests
from .errors import OperationAttemptMismatch, StateConflictError
from .flow import (
    PlannedOperation,
    SetupPlan,
    build_setup_plan,
    current_frontier_stage_ids,
    reconcile_stage_probe_activation,
)
from .inference_probe_policy import advisory_inference_probe_result
from .models import CheckpointStatus, CommandResult, ExitCode, InstanceRecord, JsonValue
from .operation_records import (
    attempt_record,
    next_attempt,
    normalize_apply_result,
    operation_probe_request,
    operation_request,
)
from .paths import ManagerPaths
from .registry import InstanceRegistry
from .resume_rules import (
    assert_decision_revision_allowed,
    read_only_auto_advance_is_pure,
)
from .stage_boundaries import BoundaryFailure, run_stage_boundaries, stage_stop_result
from .transaction import Transaction, write_transaction


@dataclass(frozen=True)
class OperationOutcome:
    """Updated durable state and an optional terminal command result."""

    transaction: Transaction
    terminal_result: CommandResult | None


@dataclass(frozen=True)
class OperationAttemptReconciliation:
    """One probe-backed resolution of an interrupted apply-state record."""

    transaction: Transaction
    probe: OperationResult


def run_operations(
    *,
    bundle: ContractBundle,
    plan: SetupPlan,
    transaction: Transaction,
    approved_actions: dict[str, list[JsonValue]],
    config: CreateConfig,
    paths: ManagerPaths,
    instance_registry: InstanceRegistry,
    refresh_preview: Callable[[], CommandResult],
    stop_after_stage: str | None = None,
) -> CommandResult:
    registry = AdapterRegistry(
        target=Path(transaction.target),
        base_python=resolve_long_lived_python(),
    )
    current = transaction
    for operation in plan.operations:
        outcome = _run_operation(
            bundle=bundle,
            operation=operation,
            transaction=current,
            approved_actions=approved_actions,
            registry=registry,
            paths=paths,
            refresh_preview=refresh_preview,
        )
        current = outcome.transaction
        if outcome.terminal_result is not None:
            return outcome.terminal_result
    boundary_outcome = _finish_operation_stages(
        bundle=bundle,
        plan=plan,
        transaction=current,
        registry=registry,
        paths=paths,
        refresh_preview=refresh_preview,
        stage_ids=(stop_after_stage,) if stop_after_stage is not None else None,
    )
    if boundary_outcome.terminal_result is not None:
        return boundary_outcome.terminal_result
    if stop_after_stage is not None:
        return _stage_limited_result(stop_after_stage, boundary_outcome.transaction, paths)
    frontier_outcome = _advance_read_only_frontiers(
        bundle=bundle,
        transaction=boundary_outcome.transaction,
        registry=registry,
        config=config,
        paths=paths,
        refresh_preview=refresh_preview,
    )
    if frontier_outcome.terminal_result is not None:
        return frontier_outcome.terminal_result
    return _complete_create(
        bundle=bundle,
        transaction=frontier_outcome.transaction,
        registry=registry,
        paths=paths,
        instance_registry=instance_registry,
    )


def _run_operation(
    *,
    bundle: ContractBundle,
    operation: PlannedOperation,
    transaction: Transaction,
    approved_actions: dict[str, list[JsonValue]],
    registry: AdapterRegistry,
    paths: ManagerPaths,
    refresh_preview: Callable[[], CommandResult],
) -> OperationOutcome:
    status = transaction.operation_statuses.get(operation.operation_id)
    if status is CheckpointStatus.VERIFIED:
        return OperationOutcome(transaction, None)
    if status is CheckpointStatus.APPLIED:
        return _resume_applied_operation(
            bundle=bundle,
            operation=operation,
            transaction=transaction,
            registry=registry,
            paths=paths,
        )
    return _run_pending_operation(
        bundle=bundle,
        operation=operation,
        transaction=transaction,
        approved_actions=approved_actions,
        registry=registry,
        paths=paths,
        refresh_preview=refresh_preview,
    )


def _resume_applied_operation(
    *,
    bundle: ContractBundle,
    operation: PlannedOperation,
    transaction: Transaction,
    registry: AdapterRegistry,
    paths: ManagerPaths,
) -> OperationOutcome:
    attempt = next_attempt(transaction, operation.operation_id)
    result = _invoke_operation_probe(
        bundle=bundle,
        operation=operation,
        transaction=transaction,
        registry=registry,
        purpose="post_apply",
        attempt=attempt,
    )
    updated = _record_operation_result(
        transaction,
        operation,
        result,
        phase="post_probe",
        attempt=attempt,
        paths=paths,
    )
    terminal = None
    if result.checkpoint_status is not CheckpointStatus.VERIFIED:
        terminal = adapter_stop_result(operation.operation_id, result.to_dict(), updated)
    return OperationOutcome(updated, terminal)


def _run_pending_operation(
    *,
    bundle: ContractBundle,
    operation: PlannedOperation,
    transaction: Transaction,
    approved_actions: dict[str, list[JsonValue]],
    registry: AdapterRegistry,
    paths: ManagerPaths,
    refresh_preview: Callable[[], CommandResult],
) -> OperationOutcome:
    attempt = next_attempt(transaction, operation.operation_id)
    probe = _invoke_operation_probe(
        bundle=bundle,
        operation=operation,
        transaction=transaction,
        registry=registry,
        purpose="pre_apply",
        attempt=attempt,
    )
    updated = _record_operation_result(
        transaction,
        operation,
        probe,
        phase="pre_probe",
        attempt=attempt,
        paths=paths,
    )
    inventory_request = operation_request(
        transaction,
        bundle,
        operation,
        phase="probe",
        probe_purpose="pre_apply",
        approval=None,
        attempt=attempt,
    )
    inventory = advisory_inference_probe_result(
        transaction.answers,
        inventory_request,
        invoke_adapter(
            registry,
            runner=operation.runner,
            request=inventory_request,
        ),
    )
    drift = _planned_action_drift(
        operation,
        inventory,
        approved_actions.get(operation.operation_id, []),
    )
    if drift:
        return _probe_drift_outcome(
            operation,
            updated,
            paths,
            refresh_preview,
        )
    stop = _pre_apply_stop(bundle, operation, probe, updated)
    inventory_has_approved_pending_action = (
        transaction.operation_statuses.get(operation.operation_id)
        in {CheckpointStatus.PENDING, CheckpointStatus.AWAITING_USER}
        and bool(inventory.planned_actions)
    )
    if stop is not None or (
        not inventory_has_approved_pending_action
        and probe.checkpoint_status
        in {
            CheckpointStatus.VERIFIED,
            CheckpointStatus.DECLINED,
            CheckpointStatus.NOT_APPLICABLE,
        }
    ):
        return OperationOutcome(updated, stop)
    return _apply_operation(
        bundle=bundle,
        operation=operation,
        transaction=updated,
        registry=registry,
        paths=paths,
        attempt=attempt,
    )


def _invoke_operation_probe(
    *,
    bundle: ContractBundle,
    operation: PlannedOperation,
    transaction: Transaction,
    registry: AdapterRegistry,
    purpose: str,
    attempt: int,
) -> OperationResult:
    declared_probe_ids = (
        operation.precondition_probe_ids
        if purpose == "pre_apply"
        else operation.postcondition_probe_ids
    )
    if declared_probe_ids:
        result: OperationResult | None = None
        for probe_id in declared_probe_ids:
            runner, request = operation_probe_request(
                transaction,
                bundle,
                operation,
                probe_id=probe_id,
                purpose=purpose,
                attempt=attempt,
            )
            result = advisory_inference_probe_result(
                transaction.answers,
                request,
                invoke_adapter(registry, runner=runner, request=request),
            )
            if result.checkpoint_status is not CheckpointStatus.VERIFIED:
                return result
        if result is None:
            raise StateConflictError("declared operation probes resolved to no probe request")
        return result
    request = operation_request(
        transaction,
        bundle,
        operation,
        phase="probe",
        probe_purpose=purpose,
        approval=None,
        attempt=attempt,
    )
    return advisory_inference_probe_result(
        transaction.answers,
        request,
        invoke_adapter(registry, runner=operation.runner, request=request),
    )


def reconcile_interrupted_operation_attempt(
    *,
    bundle: ContractBundle,
    operation: PlannedOperation,
    transaction: Transaction,
    mismatch: OperationAttemptMismatch,
    registry: AdapterRegistry,
    paths: ManagerPaths | None,
) -> OperationAttemptReconciliation:
    """Re-probe the sole safe crash window without weakening journal validation.

    ``paths=None`` is the preview form: it returns an in-memory journal only.
    Execution supplies ``paths`` while holding the instance lock, making the
    new attempt and recovered status one normal atomic journal write.
    """

    _require_reconcilable_operation_attempt(
        bundle=bundle,
        operation=operation,
        transaction=transaction,
        mismatch=mismatch,
    )
    attempt = next_attempt(transaction, operation.operation_id)
    probe = _invoke_operation_probe(
        bundle=bundle,
        operation=operation,
        transaction=transaction,
        registry=registry,
        purpose="pre_apply",
        attempt=attempt,
    )
    if probe.checkpoint_status is not CheckpointStatus.VERIFIED and probe.repair is None:
        probe = replace(
            probe,
            repair=(
                "Re-run the approved operation after resolving the declared "
                "pre-apply probe result."
            ),
        )
    updated = transaction.with_operation_status(
        operation.operation_id,
        probe.checkpoint_status,
        attempt=attempt_record(
            probe,
            stage_id=operation.stage_id,
            phase="pre_probe",
            attempt=attempt,
            owner_operation_id=operation.operation_id,
        ),
    )
    if paths is not None:
        write_transaction(paths.transaction_path(updated.name), updated)
    return OperationAttemptReconciliation(updated, probe)


def _require_reconcilable_operation_attempt(
    *,
    bundle: ContractBundle,
    operation: PlannedOperation,
    transaction: Transaction,
    mismatch: OperationAttemptMismatch,
) -> None:
    definition = bundle.operations.get(operation.operation_id)
    idempotency = None if definition is None else definition.get("idempotency")
    if (
        not _is_pinned_probe_then_apply_operation(
            bundle,
            operation,
            transaction,
            idempotency,
        )
        or not _is_retry_safe_pre_probe_mismatch(operation, mismatch)
    ):
        raise mismatch


def _is_pinned_probe_then_apply_operation(
    bundle: ContractBundle,
    operation: PlannedOperation,
    transaction: Transaction,
    idempotency: JsonValue | None,
) -> bool:
    return (
        transaction.flow_contract_digest == bundle.contract_digest
        and isinstance(idempotency, dict)
        and idempotency.get("mode") == "probe_then_apply"
        and bool(operation.precondition_probe_ids)
    )


def _is_retry_safe_pre_probe_mismatch(
    operation: PlannedOperation,
    mismatch: OperationAttemptMismatch,
) -> bool:
    return (
        mismatch.operation == operation.operation_id
        and mismatch.current is CheckpointStatus.APPLYING
        and mismatch.latest.get("phase") == "pre_probe"
        and mismatch.latest.get("checkpoint_status")
        in {
            CheckpointStatus.PENDING.value,
            CheckpointStatus.AWAITING_USER.value,
        }
        and mismatch.latest.get("retry_safe") is True
    )


def _record_operation_result(
    transaction: Transaction,
    operation: PlannedOperation,
    result: OperationResult,
    *,
    phase: str,
    attempt: int,
    paths: ManagerPaths,
) -> Transaction:
    updated = transaction.with_operation_status(
        operation.operation_id,
        result.checkpoint_status,
        attempt=attempt_record(
            result,
            stage_id=operation.stage_id,
            phase=phase,
            attempt=attempt,
            owner_operation_id=operation.operation_id,
        ),
    )
    write_transaction(paths.transaction_path(updated.name), updated)
    return updated


def _planned_action_drift(
    operation: PlannedOperation,
    probe: OperationResult,
    approved: list[JsonValue],
) -> bool:
    observed: list[JsonValue] = [
        {"operation_id": operation.operation_id, **action.to_dict()}
        for action in probe.planned_actions
    ]
    return observed != approved


def _probe_drift_outcome(
    operation: PlannedOperation,
    transaction: Transaction,
    paths: ManagerPaths,
    refresh_preview: Callable[[], CommandResult],
) -> OperationOutcome:
    preview = refresh_preview()
    refreshed_fingerprint = preview.data.get("approval_fingerprint")
    if (
        transaction.approval_fingerprint is not None
        and refreshed_fingerprint == transaction.approval_fingerprint
    ):
        raise StateConflictError(
            "planned action drift regenerated the rejected approval fingerprint"
        )
    updated = transaction.with_operation_status(
        operation.operation_id,
        CheckpointStatus.AWAITING_USER,
    ).with_result_kind("probe_drift")
    write_transaction(paths.transaction_path(updated.name), updated)
    terminal = probe_drift_preview(
        preview,
        f"Host actions changed before {operation.operation_id!r}; no action ran.",
    )
    return OperationOutcome(updated, terminal)


def _pre_apply_stop(
    bundle: ContractBundle,
    operation: PlannedOperation,
    probe: OperationResult,
    transaction: Transaction,
) -> CommandResult | None:
    if (
        probe.checkpoint_status is CheckpointStatus.AWAITING_USER
        and probe.planned_actions
    ):
        return None
    incomplete = {
        CheckpointStatus.AWAITING_USER,
        CheckpointStatus.BLOCKED,
        CheckpointStatus.FAILED,
    }
    if probe.checkpoint_status not in incomplete:
        return None
    if (
        probe.checkpoint_status is CheckpointStatus.BLOCKED
        and probe.operation_id in operation.precondition_probe_ids
        and _probe_remediates_operation(bundle, probe.operation_id, operation.operation_id)
    ):
        return None
    return adapter_stop_result(operation.operation_id, probe.to_dict(), transaction)


def _probe_remediates_operation(
    bundle: ContractBundle,
    probe_id: str,
    operation_id: str,
) -> bool:
    definition = bundle.probes.get(probe_id)
    if definition is None:
        raise StateConflictError(f"declared precondition probe {probe_id!r} is unavailable")
    if "remediation_operation_refs" not in definition:
        return False
    remediation_refs = definition["remediation_operation_refs"]
    if not isinstance(remediation_refs, list) or not all(
        isinstance(item, str) for item in remediation_refs
    ):
        raise StateConflictError(
            f"declared precondition probe {probe_id!r} has invalid remediation references"
        )
    return operation_id in remediation_refs


def _apply_operation(
    *,
    bundle: ContractBundle,
    operation: PlannedOperation,
    transaction: Transaction,
    registry: AdapterRegistry,
    paths: ManagerPaths,
    attempt: int,
) -> OperationOutcome:
    applying = transaction.with_operation_status(
        operation.operation_id,
        CheckpointStatus.APPLYING,
    )
    write_transaction(paths.transaction_path(applying.name), applying)
    request = operation_request(
        applying,
        bundle,
        operation,
        phase="apply",
        probe_purpose=None,
        approval=applying.approval_fingerprint,
        attempt=attempt,
    )
    target = Path(applying.target)
    before = tracked_edit_digests(target, applying.seed.tree_hash)
    result = normalize_apply_result(
        invoke_adapter(registry, runner=operation.runner, request=request)
    )
    if result.checkpoint_status is CheckpointStatus.APPLIED:
        # iss_9cd4359a: the doctor accepts exactly the tracked bytes an approved apply left behind.
        record_applied_edits(
            paths,
            name=applying.name,
            target=applying.target,
            create_operation_id=applying.operation_id,
            operation_id=operation.operation_id,
            before=before,
            after=tracked_edit_digests(target, applying.seed.tree_hash),
        )
    applied = _record_operation_result(
        applying,
        operation,
        result,
        phase="apply",
        attempt=attempt,
        paths=paths,
    )
    if (
        operation.operation_id == "install_python_runtime"
        and result.checkpoint_status is CheckpointStatus.APPLIED
    ):
        registry.refresh_base_python()
    if result.checkpoint_status is not CheckpointStatus.APPLIED:
        return OperationOutcome(
            applied,
            adapter_stop_result(operation.operation_id, result.to_dict(), applied),
        )
    return _post_apply_probe(
        bundle=bundle,
        operation=operation,
        transaction=applied,
        registry=registry,
        paths=paths,
        attempt=attempt,
    )


def _post_apply_probe(
    *,
    bundle: ContractBundle,
    operation: PlannedOperation,
    transaction: Transaction,
    registry: AdapterRegistry,
    paths: ManagerPaths,
    attempt: int,
) -> OperationOutcome:
    result = _invoke_operation_probe(
        bundle=bundle,
        operation=operation,
        transaction=transaction,
        registry=registry,
        purpose="post_apply",
        attempt=attempt,
    )
    updated = _record_operation_result(
        transaction,
        operation,
        result,
        phase="post_probe",
        attempt=attempt,
        paths=paths,
    )
    terminal = None
    if result.checkpoint_status is not CheckpointStatus.VERIFIED:
        terminal = adapter_stop_result(operation.operation_id, result.to_dict(), updated)
    return OperationOutcome(updated, terminal)


def _finish_operation_stages(
    *,
    bundle: ContractBundle,
    plan: SetupPlan,
    transaction: Transaction,
    registry: AdapterRegistry,
    paths: ManagerPaths,
    refresh_preview: Callable[[], CommandResult],
    stage_ids: tuple[str, ...] | None = None,
) -> OperationOutcome:
    selected_stage_ids = (
        stage_ids
        if stage_ids is not None
        else tuple(dict.fromkeys(item.stage_id for item in plan.operations))
    )
    updated, _observations, failures = run_stage_boundaries(
        bundle=bundle,
        transaction=transaction,
        registry=registry,
        stage_ids=selected_stage_ids,
        boundary="exit",
        answers=transaction.answers,
        persist_path=paths.transaction_path(transaction.name),
    )
    if not failures:
        return OperationOutcome(updated, None)
    return stage_remediation_preview(
        failure=failures[0],
        transaction=updated,
        paths=paths,
        refresh_preview=refresh_preview,
    )


def _stage_limited_result(
    stage_id: str,
    transaction: Transaction,
    paths: ManagerPaths,
) -> CommandResult:
    """Persist and report one verified stage without advancing its successor."""

    stage_status = transaction.stages.get(stage_id)
    if stage_status not in {CheckpointStatus.VERIFIED, CheckpointStatus.NOT_APPLICABLE}:
        raise StateConflictError(
            f"stage-limited execution ended without a completed stage: {stage_id!r}"
        )
    updated = transaction.with_result_kind("stage_resume_completed")
    write_transaction(paths.transaction_path(updated.name), updated)
    return CommandResult(
        kind="stage_resume",
        status="stage_completed",
        message=f"Named stage {stage_id!r} completed; successor stages were not advanced.",
        exit_code=ExitCode.OK,
        data={"stage_id": stage_id, "transaction": updated.to_dict()},
    )


def _advance_read_only_frontiers(
    *,
    bundle: ContractBundle,
    transaction: Transaction,
    registry: AdapterRegistry,
    config: CreateConfig,
    paths: ManagerPaths,
    refresh_preview: Callable[[], CommandResult],
) -> OperationOutcome:
    current = transaction
    for _round in range(len(bundle.stages) + 1):
        frontier = current_frontier_stage_ids(bundle, current, current.answers)
        if not frontier:
            return OperationOutcome(current, None)
        plan = _read_only_frontier_plan(bundle, current, config, paths, frontier)
        if not read_only_auto_advance_is_pure(current.answers, plan):
            return _next_stage_preview_outcome(current, frontier, paths)
        outcome = _cross_read_only_frontier(
            bundle=bundle,
            transaction=current,
            plan=plan,
            frontier=frontier,
            registry=registry,
            paths=paths,
            refresh_preview=refresh_preview,
        )
        current = outcome.transaction
        if outcome.terminal_result is not None:
            return outcome
    raise StateConflictError("read-only stage frontier did not converge")


def _read_only_frontier_plan(
    bundle: ContractBundle,
    transaction: Transaction,
    config: CreateConfig,
    paths: ManagerPaths,
    frontier: tuple[str, ...],
) -> SetupPlan:
    frontier_set = set(frontier)
    return build_setup_plan(
        bundle=bundle,
        config=config,
        seed=transaction.seed,
        journal_path=paths.transaction_path(transaction.name),
        prospective_consents=False,
        decision_selections=config.decisions,
        recorded_answers=transaction.answers,
        decision_sources=config.decision_sources,
        resolution_stage_ids=frontier_set,
        operation_stage_ids=frontier_set,
    )


def _next_stage_preview_outcome(
    transaction: Transaction,
    frontier: tuple[str, ...],
    paths: ManagerPaths,
) -> OperationOutcome:
    updated = transaction.with_result_kind("next_stage_preview_required")
    write_transaction(paths.transaction_path(updated.name), updated)
    result = CommandResult(
        kind="create",
        status="awaiting_user",
        message="The current frontier verified; the next frontier requires a fresh preview.",
        exit_code=ExitCode.HUMAN_ACTION,
        error_kind="next_stage_preview_required",
        repair="Re-run create, review the newly reachable actions, and approve its new fingerprint.",
        data={"frontier": list(frontier), "transaction": updated.to_dict()},
    )
    return OperationOutcome(updated, result)


def _cross_read_only_frontier(
    *,
    bundle: ContractBundle,
    transaction: Transaction,
    plan: SetupPlan,
    frontier: tuple[str, ...],
    registry: AdapterRegistry,
    paths: ManagerPaths,
    refresh_preview: Callable[[], CommandResult],
) -> OperationOutcome:
    assert_decision_revision_allowed(transaction, plan.answers, bundle)
    updated = reconcile_stage_probe_activation(
        bundle,
        rebind_completion_probes(
            bundle,
            transaction.with_answers(plan.answers),
            plan.answers,
        ),
        plan.answers,
    )
    for boundary in ("entry", "exit"):
        updated, _observations, failures = run_stage_boundaries(
            bundle=bundle,
            transaction=updated,
            registry=registry,
            stage_ids=frontier,
            boundary=boundary,
            answers=updated.answers,
            persist_path=paths.transaction_path(updated.name),
        )
        if failures:
            return stage_remediation_preview(
                failure=failures[0],
                transaction=updated,
                paths=paths,
                refresh_preview=refresh_preview,
            )
    return OperationOutcome(updated, None)


def stage_remediation_preview(
    *,
    failure: BoundaryFailure,
    transaction: Transaction,
    paths: ManagerPaths,
    refresh_preview: Callable[[], CommandResult],
) -> OperationOutcome:
    """Render a new approval-required preview for a remediable boundary failure."""

    if not failure.remediation_operation_ids:
        return OperationOutcome(
            transaction,
            stage_stop_result(failure.identity, transaction),
        )
    preview = refresh_preview()
    if preview.status != "preview_ready":
        return OperationOutcome(transaction, preview)
    updated = transaction.with_result_kind("stage_boundary_remediation_required")
    write_transaction(paths.transaction_path(updated.name), updated)
    return OperationOutcome(
        updated,
        CommandResult(
            kind=preview.kind,
            status="awaiting_user",
            message=(
                f"Setup requires remediation for stage boundary {failure.identity}."
            ),
            exit_code=ExitCode.HUMAN_ACTION,
            error_kind="stage_boundary_remediation_required",
            repair=(
                "Review the newly rendered remediation preview and rerun with its "
                "approval fingerprint."
            ),
            data=preview.data,
        ),
    )


def _complete_create(
    *,
    bundle: ContractBundle,
    transaction: Transaction,
    registry: AdapterRegistry,
    paths: ManagerPaths,
    instance_registry: InstanceRegistry,
) -> CommandResult:
    updated, checks = run_completion_probes(bundle, transaction, registry, paths)
    verified = updated.status.value == "verified"
    updated = updated.with_result_kind(
        "created" if verified else "created_needs_attention"
    )
    write_transaction(paths.transaction_path(updated.name), updated)
    if verified:
        ensure_registry(instance_registry, updated)
    return CommandResult(
        kind="create",
        status=updated.status.value,
        message=(
            "Installation and every required completion probe verified."
            if verified
            else "Manager-owned setup operations completed, but required completion remains unresolved."
        ),
        exit_code=ExitCode.OK if verified else ExitCode.HUMAN_ACTION,
        error_kind=None if verified else "created_needs_attention",
        repair=(
            None
            if verified
            else "Repair the listed completion checks and resume."
        ),
        data={"transaction": updated.to_dict(), "completion_checks": checks},
    )


def ensure_registry(registry: InstanceRegistry, transaction: Transaction) -> None:
    record = InstanceRecord(
        name=transaction.name,
        target=transaction.target,
        launcher=str(Path(transaction.target) / "client" / "bin" / transaction.name),
        seed_repository=transaction.seed.repository,
        seed_tag=transaction.seed.release_tag,
        seed_commit=transaction.seed.commit,
        seed_tree_hash=transaction.seed.tree_hash,
        profile=transaction.seed.profile,
        flow_id=transaction.flow_id,
        flow_source_revision=transaction.flow_source_revision,
        flow_contract_digest=transaction.flow_contract_digest,
        created_at=transaction.created_at,
        updated_at=transaction.created_at,
        lifecycle_state="verified",
        input_fingerprint=transaction.input_fingerprint,
        expected_router_name=transaction.name,
        expected_router_socket=str(
            Path.home() / ".ananta/runtime" / f"{transaction.name}.router.sock"
        ),
        expected_router_port_range="8800-8999",
    )
    registry.add(record)


def adapter_stop_result(
    operation_id: str,
    adapter_result: dict[str, JsonValue],
    transaction: Transaction,
) -> CommandResult:
    status = str(adapter_result["checkpoint_status"])
    return CommandResult(
        kind="create",
        status=transaction.status.value,
        message=f"Setup stopped at operation {operation_id!r}.",
        exit_code=ExitCode.FAILED if status == "failed" else ExitCode.HUMAN_ACTION,
        error_kind=str(adapter_result.get("error_kind") or "adapter_incomplete"),
        repair=str(
            adapter_result.get("repair")
            or "Repair the target-local adapter result and resume."
        ),
        data={
            "operation_id": operation_id,
            "adapter_result": adapter_result,
            "transaction": transaction.to_dict(),
        },
    )


def probe_drift_preview(preview: CommandResult, message: str) -> CommandResult:
    return CommandResult(
        kind=preview.kind,
        status="awaiting_user",
        message=message,
        exit_code=ExitCode.HUMAN_ACTION,
        error_kind="probe_drift",
        repair="Review the newly rendered preview and rerun with its new approval fingerprint.",
        data=preview.data,
    )


def approval_stale_preview(preview: CommandResult) -> CommandResult:
    """Refuse a token made against the pre-permission-preflight preimage."""

    return CommandResult(
        kind=preview.kind,
        status="awaiting_user",
        message=(
            "Approval is stale: the fingerprint preimage now includes permission "
            "preflight semantic content."
        ),
        exit_code=ExitCode.HUMAN_ACTION,
        error_kind="approval_stale",
        repair="Run a fresh dry-run, review permission preflight, and re-approve.",
        data=preview.data,
    )
