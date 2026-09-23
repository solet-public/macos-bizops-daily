"""Adapter request construction and typed operation-attempt journal records."""

from __future__ import annotations

import uuid

from .adapters import OperationRequest, OperationResult
from .contracts import ContractBundle
from .errors import StateConflictError
from .flow import PlannedOperation
from .models import CheckpointStatus, JsonValue
from .probe_input_projection import probe_public_inputs
from .transaction import Transaction, utc_now


def operation_request(
    transaction: Transaction,
    bundle: ContractBundle,
    operation: PlannedOperation,
    *,
    phase: str,
    probe_purpose: str | None,
    approval: str | None,
    attempt: int,
    answers_fingerprint: str | None = None,
) -> OperationRequest:
    return OperationRequest(
        request_id=str(uuid.uuid4()),
        operation_id=operation.operation_id,
        operation_ref=operation.operation_ref,
        phase=phase,
        probe_purpose=probe_purpose,
        attempt=attempt,
        name=transaction.name,
        target=transaction.target,
        flow_id=bundle.flow_id,
        flow_source_revision=bundle.source_revision,
        answers_fingerprint=answers_fingerprint or transaction.answers_fingerprint,
        approval_fingerprint=approval,
        dry_run=phase == "probe",
        timeout_seconds=operation.apply_timeout_seconds if phase == "apply" else 30,
        public_inputs=operation.public_inputs,
    )


def operation_probe_request(
    transaction: Transaction,
    bundle: ContractBundle,
    operation: PlannedOperation,
    *,
    probe_id: str,
    purpose: str,
    attempt: int,
) -> tuple[str, OperationRequest]:
    """Build one operation-owned request for its declared probe definition."""

    try:
        definition = bundle.probes[probe_id]
    except KeyError as exc:
        raise StateConflictError(
            f"operation {operation.operation_id!r} declares unknown probe {probe_id!r}"
        ) from exc
    probe_ref = definition.get("probe_ref")
    runner = definition.get("runner")
    if not isinstance(probe_ref, str) or not isinstance(runner, str):
        raise StateConflictError(f"probe {probe_id!r} lacks an executable definition")
    return runner, OperationRequest(
        request_id=str(uuid.uuid4()),
        operation_id=probe_id,
        operation_ref=probe_ref,
        phase="probe",
        probe_purpose=purpose,
        attempt=attempt,
        name=transaction.name,
        target=transaction.target,
        flow_id=bundle.flow_id,
        flow_source_revision=bundle.source_revision,
        answers_fingerprint=transaction.answers_fingerprint,
        approval_fingerprint=None,
        dry_run=True,
        timeout_seconds=30,
        public_inputs=probe_public_inputs(transaction, probe_ref),
    )


def attempt_record(
    result: OperationResult,
    *,
    stage_id: str,
    phase: str,
    attempt: int,
    owner_operation_id: str,
) -> dict[str, JsonValue]:
    return {
        "operation_id": owner_operation_id,
        "stage_id": stage_id,
        "phase": phase,
        "attempt": attempt,
        "request_id": result.request_id,
        "checkpoint_status": result.checkpoint_status.value,
        "error_kind": result.error_kind,
        "retry_safe": result.retry_safe,
        "exit_code": result.exit_code,
        "timed_out": result.timed_out,
        "duration_ms": result.duration_ms,
        "planned_actions": [action.to_dict() for action in result.planned_actions],
        "evidence": list(result.evidence),
        "reason": result.reason,
        "repair": result.repair,
        "recorded_at": utc_now(),
    }


def next_attempt(transaction: Transaction, operation_id: str) -> int:
    values = [
        value
        for recorded in transaction.operation_attempts
        if recorded.get("operation_id") == operation_id
        and isinstance((value := recorded.get("attempt")), int)
        and not isinstance(value, bool)
    ]
    return 1 + max(values, default=0)


def normalize_apply_result(result: OperationResult) -> OperationResult:
    """Preserve transport failures while rejecting invalid successful states."""

    if result.checkpoint_status is CheckpointStatus.APPLIED:
        return result
    if (
        result.checkpoint_status is CheckpointStatus.PENDING
        and result.timed_out
        and result.retry_safe
        and result.error_kind is not None
    ):
        return result
    transport_statuses = {
        CheckpointStatus.AWAITING_USER,
        CheckpointStatus.BLOCKED,
        CheckpointStatus.FAILED,
    }
    if result.checkpoint_status in transport_statuses and result.error_kind is not None:
        return result
    return OperationResult(
        request_id=result.request_id,
        operation_id=result.operation_id,
        phase=result.phase,
        probe_purpose=result.probe_purpose,
        checkpoint_status=CheckpointStatus.FAILED,
        error_kind="adapter_protocol_error",
        retry_safe=False,
        exit_code=result.exit_code,
        timed_out=result.timed_out,
        duration_ms=result.duration_ms,
        stdout=result.stdout,
        stderr=result.stderr,
        planned_actions=(),
        discovered_candidates=(),
        evidence=result.evidence,
        repair="Apply must report applied; only a post-apply probe can verify.",
        reason=result.reason,
    )


def actions_by_operation(value: JsonValue) -> dict[str, list[JsonValue]]:
    if not isinstance(value, list):
        raise StateConflictError("approved planned_actions is not an array")
    grouped: dict[str, list[JsonValue]] = {}
    for item in value:
        if not isinstance(item, dict) or not isinstance(item.get("operation_id"), str):
            raise StateConflictError(
                "approved planned action lacks operation identity"
            )
        grouped.setdefault(str(item["operation_id"]), []).append(item)
    return grouped
