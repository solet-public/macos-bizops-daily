"""Cross-record operation-attempt consistency rules.

Structural journal decoding belongs in :mod:`journal_validation`; this module
owns the narrow semantic comparison between an operation's current checkpoint
and its most recent recorded attempt.
"""

from __future__ import annotations

from typing import Protocol

from .errors import OperationAttemptMismatch, StateError
from .models import CheckpointStatus, JsonValue


class OperationAttemptState(Protocol):
    """The journal fields needed for operation-attempt semantic validation."""

    @property
    def operation_statuses(self) -> dict[str, CheckpointStatus]: ...

    @property
    def result_kind(self) -> str | None: ...


def validate_operation_attempt_coverage(
    transaction: OperationAttemptState,
    latest: dict[str, dict[str, JsonValue]],
) -> OperationAttemptMismatch | None:
    """Return the one recoverable mismatch, while rejecting all other drift."""

    statuses = transaction.operation_statuses
    disagreements = [
        (operation_id, attempt)
        for operation_id, attempt in latest.items()
        if attempt.get("checkpoint_status") != statuses[operation_id].value
    ]
    mismatch = _recoverable_mismatch(transaction, disagreements)
    no_attempt = {CheckpointStatus.PENDING, CheckpointStatus.NOT_APPLICABLE}
    for operation_id, current in statuses.items():
        if current not in no_attempt and operation_id not in latest:
            raise StateError(f"operation status lacks an attempt record: {operation_id!r}")
    return mismatch


def _recoverable_mismatch(
    transaction: OperationAttemptState,
    disagreements: list[tuple[str, dict[str, JsonValue]]],
) -> OperationAttemptMismatch | None:
    if not disagreements or _is_unrecorded_transition(transaction, disagreements):
        return None
    if len(disagreements) != 1:
        operation_id, _attempt = disagreements[0]
        raise StateError(f"operation status disagrees with latest attempt: {operation_id!r}")
    operation_id, attempt = disagreements[0]
    return OperationAttemptMismatch(
        operation=operation_id,
        current=transaction.operation_statuses[operation_id],
        latest=attempt,
    )


def _is_unrecorded_transition(
    transaction: OperationAttemptState,
    disagreements: list[tuple[str, dict[str, JsonValue]]],
) -> bool:
    if _is_contract_reconciliation_reset(transaction, disagreements):
        return True
    if len(disagreements) != 1:
        return False
    operation_id, latest = disagreements[0]
    if latest.get("phase") != "pre_probe":
        return False
    current = transaction.operation_statuses[operation_id]
    if current is CheckpointStatus.AWAITING_USER:
        return transaction.result_kind == "probe_drift"
    return (
        current is CheckpointStatus.APPLYING
        and transaction.result_kind is None
        and latest.get("checkpoint_status") == CheckpointStatus.AWAITING_USER.value
    )


def _is_contract_reconciliation_reset(
    transaction: OperationAttemptState,
    disagreements: list[tuple[str, dict[str, JsonValue]]],
) -> bool:
    marker = transaction.result_kind
    if not isinstance(marker, str) or not marker.startswith("contract_reconciliation_reset:"):
        return False
    migration_id = marker.removeprefix("contract_reconciliation_reset:")
    return bool(migration_id) and all(
        transaction.operation_statuses[operation_id] is CheckpointStatus.PENDING
        and attempt.get("checkpoint_status") == CheckpointStatus.VERIFIED.value
        for operation_id, attempt in disagreements
    )
