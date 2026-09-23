"""Frozen stage and transaction roll-up rules for installation journals."""

from __future__ import annotations

from .errors import StateError
from .journal_migrations import activation_site_key
from .models import CheckpointStatus, TransactionStatus

StageProbeStatuses = dict[str, dict[str, dict[str, CheckpointStatus]]]

_FINAL_STAGE_STATUSES = {
    CheckpointStatus.VERIFIED,
    CheckpointStatus.DECLINED,
    CheckpointStatus.NOT_APPLICABLE,
}


def roll_up_transaction(
    stages: dict[str, CheckpointStatus],
    completion: dict[str, CheckpointStatus],
) -> TransactionStatus:
    """Apply the frozen roll-up precedence exactly."""

    stage_values = tuple(stages.values())
    completion_values = tuple(completion.values())
    if _is_verified(stage_values, completion_values):
        return TransactionStatus.VERIFIED
    combined = (*stage_values, *completion_values)
    precedence = (
        (CheckpointStatus.FAILED, TransactionStatus.FAILED),
        (CheckpointStatus.BLOCKED, TransactionStatus.BLOCKED),
        (CheckpointStatus.AWAITING_USER, TransactionStatus.AWAITING_USER),
        (CheckpointStatus.APPLYING, TransactionStatus.APPLYING),
    )
    for checkpoint, transaction in precedence:
        if checkpoint in combined:
            return transaction
    return TransactionStatus.PENDING


def _is_verified(
    stages: tuple[CheckpointStatus, ...],
    completion: tuple[CheckpointStatus, ...],
) -> bool:
    return (
        bool(stages)
        and bool(completion)
        and all(status in _FINAL_STAGE_STATUSES for status in stages)
        and all(status is CheckpointStatus.VERIFIED for status in completion)
    )


def derive_stage_statuses(
    existing: dict[str, CheckpointStatus],
    stage_probe_statuses: StageProbeStatuses,
    operation_stages: dict[str, str],
    operation_statuses: dict[str, CheckpointStatus],
    probe_activations: dict[str, dict[str, str]] | None = None,
) -> dict[str, CheckpointStatus]:
    by_stage = _statuses_by_stage(
        existing,
        stage_probe_statuses,
        operation_stages,
        operation_statuses,
        probe_activations,
    )
    return {
        stage_id: _derive_stage_status(existing[stage_id], statuses)
        for stage_id, statuses in by_stage.items()
    }


def _statuses_by_stage(
    existing: dict[str, CheckpointStatus],
    stage_probe_statuses: StageProbeStatuses,
    operation_stages: dict[str, str],
    operation_statuses: dict[str, CheckpointStatus],
    probe_activations: dict[str, dict[str, str]] | None,
) -> dict[str, list[CheckpointStatus]]:
    by_stage: dict[str, list[CheckpointStatus]] = {stage_id: [] for stage_id in existing}
    for stage_id, boundaries in stage_probe_statuses.items():
        if stage_id not in by_stage:
            raise StateError(f"stage probe map references unknown stage {stage_id!r}")
        for boundary, probes in boundaries.items():
            by_stage[stage_id].extend(
                (
                    status
                    if probe_activations is None
                    or probe_activations[activation_site_key(stage_id, boundary, probe_id)]["state"]
                    == "active"
                    else CheckpointStatus.NOT_APPLICABLE
                )
                for probe_id, status in probes.items()
            )
    for operation_id, stage_id in operation_stages.items():
        if stage_id not in by_stage:
            raise StateError(f"operation map references unknown stage {stage_id!r}")
        by_stage[stage_id].append(operation_statuses[operation_id])
    return by_stage


def _derive_stage_status(
    existing: CheckpointStatus,
    statuses: list[CheckpointStatus],
) -> CheckpointStatus:
    if not statuses:
        return existing
    precedence = (
        CheckpointStatus.FAILED,
        CheckpointStatus.BLOCKED,
        CheckpointStatus.AWAITING_USER,
        CheckpointStatus.APPLYING,
    )
    for status in precedence:
        if status in statuses:
            return status
    if all(status is CheckpointStatus.NOT_APPLICABLE for status in statuses):
        return CheckpointStatus.NOT_APPLICABLE
    if all(status in _FINAL_STAGE_STATUSES for status in statuses):
        return CheckpointStatus.VERIFIED
    return CheckpointStatus.PENDING
