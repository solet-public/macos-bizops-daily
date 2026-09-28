"""Deferred release migrations: a retry-safe row the adapter left ``pending`` (iss_6d26db73 design section 5).

A release-declared migration whose precondition is not yet true on this host
(e.g. a plugin transition whose replacement is not ready) returns ``pending``
with ``retry_safe`` and a repair, and promises it left the target coherent --
nothing half-done.  The executor journals such a row ``deferred`` instead of
failing the update; the row is settled for this update and never re-driven by
it.  Promotion then publishes ``needs_attention`` (before the pointer
releases, so a crash cannot leave a ``verified`` row that hides a deferral),
and the next ``solet-manager update`` retries it in ``verify`` mode at the
same release.

Only a ``BACKED_UP_ARTIFACT`` row may defer: its declared targets were backed
up before the apply, and its writes are re-baselined like any apply's.  A
pending apply from any other row, or one without an error kind and repair, is
``deferral_not_permitted`` -- never a silent pass.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

from .errors import UpdateFailedError
from .models import CheckpointStatus, JsonValue, OperationType

if TYPE_CHECKING:
    from .adapter_protocol import OperationResult
    from .existing_install_bundle import RuntimeOperation
    from .update_runtime_execution import RuntimeExecution

DEFERRED = "deferred"


def apply_deferral(execution: RuntimeExecution, operation: RuntimeOperation, applied: OperationResult) -> bool:
    """Journal ``operation`` as deferred when its apply stayed pending; ``False`` when it did not."""
    if applied.checkpoint_status is not CheckpointStatus.PENDING or not applied.retry_safe:
        return False
    if execution.operation_type(operation.operation_id) != OperationType.BACKED_UP_ARTIFACT.value or not applied.error_kind or not applied.repair:
        raise UpdateFailedError("deferral_not_permitted", f"{operation.operation_id} apply stayed pending but the row cannot defer", repair=f"Retain all evidence; {execution._reconcile_repair()}")  # noqa: SLF001
    execution._rebaseline(operation)  # noqa: SLF001
    execution._record(operation.operation_id, "manager", None, status=DEFERRED, note={DEFERRED: {"error_kind": applied.error_kind, "repair": applied.repair}})  # noqa: SLF001
    return True


def deferred_rows(journal: dict[str, JsonValue]) -> tuple[tuple[str, str, str], ...]:
    """``(operation_id, error_kind, repair)`` for every row the update deferred, in journal order."""
    rows: list[tuple[str, str, str]] = []
    for item in cast(list[JsonValue], journal["runtime_operations"]):
        row = cast(dict[str, JsonValue], item)
        if row["status"] != DEFERRED:
            continue
        for attempt in reversed(cast(list[JsonValue], row["attempts"])):
            note = cast(dict[str, JsonValue], cast(dict[str, JsonValue], attempt)["evidence"]).get(DEFERRED)
            if isinstance(note, dict):
                deferred = cast(dict[str, JsonValue], note)
                rows.append((cast(str, row["operation_id"]), cast(str, deferred["error_kind"]), cast(str, deferred["repair"])))
                break
    return tuple(rows)


__all__ = ["DEFERRED", "apply_deferral", "deferred_rows"]
