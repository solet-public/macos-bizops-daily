"""Fail one orphaned ``action_events`` row by id (iss_6069cf22).

Before this module no sanctioned verb could resolve a stuck row. After the
2026-09-28 stall (iss_30fb08fd) a SIGKILL left the audit's row ``processing``,
and the only compliant options were to wait an hour for the orphan reaper or
to let it be re-run.

**Its one real use: a row orphaned by a process that is gone.** A row left
``processing`` by a process that died or was replaced (a SIGKILL, a crash, a
blue-green swap) can be failed from the live process at once, with a reason,
instead of waiting out the reaper's age threshold.

**What it cannot do.** It is dispatched as an action on the same serial,
FIFO action queue it would relieve, so:

- it cannot interrupt a handler running in this process: the call waits
  behind that handler and, once it runs, finds the row finished and refuses;
- it cannot overtake a queued row ahead of it: that row is claimed first.

Failing a row does not stop work still running in another process -- a
Python thread cannot be killed -- and it does not run the row's error
processor, terminate its flow token, or send a bridge delivery; a caller
polling ``process_result`` sees ``failed`` with the reason. The old
process's own failure write is guarded on ``processing`` and cannot overwrite
the row; its completion write is not guarded (see
``ActionQueuePoller._update_action_status_to_completed``), so a handler that
does return in the old process marks the row completed.

Refusals, all loud (``ActionEventResolutionError``):

- an empty ``reason``;
- a row that is not ``queued`` or ``processing`` -- a finished row's outcome
  is history and is not rewritten;
- a row at or below ``EVIDENCE_FLOOR_CREATED_AT`` -- the preserved incident
  evidence. The floor is applied IN SQL on the read, exactly as the orphan
  reaper applies it, so the 601 MB evidence payload is never transferred; such
  a row is therefore indistinguishable from a missing one, and the refusal
  says so;
- a row whose status changed between the read and the write -- the update is
  guarded on the observed status and must touch exactly one row.
"""

from __future__ import annotations

import logging
from typing import Protocol

from ananta.core.actions.orphan_reaper import EVIDENCE_FLOOR_CREATED_AT
from ananta.core.actions.state_update_result import updated_row_count
from ananta.core.domain.enums import ActionStatus
from ananta.core.domain.types import ActionResult

logger = logging.getLogger(__name__)

RESOLVABLE_STATUSES: frozenset[str] = frozenset(
    {ActionStatus.QUEUED.value, ActionStatus.PROCESSING.value},
)


class ActionEventResolutionError(ValueError):
    """A refused resolution; the message says exactly why."""


class _ActionEventStore(Protocol):
    """The slice of ``StateManagementInterface`` this module needs."""

    def query_ordered(self, namespace: str, data: dict[str, object]) -> ActionResult: ...

    def update_state(
        self, namespace: str, query: dict[str, object], updates: dict[str, object],
    ) -> ActionResult: ...


def _records(result: ActionResult) -> list[dict[str, object]]:
    if str(result.get("action_status")) != ActionStatus.COMPLETED.value:
        raise ActionEventResolutionError(f"action_events read did not complete: {result!r}")
    data = result.get("data")
    if not isinstance(data, dict):
        raise ActionEventResolutionError(f"action_events read returned no data: {result!r}")
    records = data.get("records")
    if not isinstance(records, list):
        raise ActionEventResolutionError(f"action_events read returned no records: {result!r}")
    return [row for row in records if isinstance(row, dict)]


def fail_action_event(
    state_service: _ActionEventStore,
    *,
    action_id: str,
    reason: str,
) -> dict[str, str]:
    """Mark one ``queued``/``processing`` action ``failed`` with ``reason``.

    Returns ``{"action_id", "process_key", "previous_status", "status",
    "error_message"}``. Raises ``ActionEventResolutionError`` on every refusal
    listed in the module docstring.
    """
    if not action_id.strip():
        raise ActionEventResolutionError("action_id is required")
    if not reason.strip():
        raise ActionEventResolutionError(
            "reason is required: say why this action is being failed",
        )

    evidence_floor = {"op": "gt", "value": EVIDENCE_FLOOR_CREATED_AT}
    rows = _records(
        state_service.query_ordered(
            "core",
            {
                "table": "action_events",
                "filters": {"id": action_id, "created_at": evidence_floor},
                "order_by": [["created_at", "asc"], ["id", "asc"]],
                "limit": 1,
                "include_deleted": True,
            },
        ),
    )
    if not rows:
        raise ActionEventResolutionError(
            f"action {action_id} not found above the evidence floor "
            f"({EVIDENCE_FLOOR_CREATED_AT.isoformat()} UTC): it does not exist, or "
            "it is a preserved incident-evidence row, which this verb never touches",
        )
    row = rows[0]
    previous_status = str(row.get("status"))
    if previous_status not in RESOLVABLE_STATUSES:
        raise ActionEventResolutionError(
            f"action {action_id} is '{previous_status}', not queued or processing; "
            "a finished action's outcome is not rewritten",
        )

    process_key = str(row.get("process_key") or "<unknown>")
    error_message = f"failed by fail_action_event (was {previous_status}): {reason.strip()}"
    updated = updated_row_count(
        state_service.update_state(
            namespace="core",
            query={
                "table": "action_events",
                "filters": {
                    "id": action_id,
                    "status": previous_status,
                    "created_at": evidence_floor,
                },
            },
            updates={"status": ActionStatus.FAILED.value, "error_message": error_message},
        ),
        what=f"fail_action_event({action_id})",
    )
    if updated != 1:
        raise ActionEventResolutionError(
            f"action {action_id} changed state after it was read as "
            f"'{previous_status}' ({updated} rows updated); re-read it and retry",
        )

    logger.warning(
        "ACTION_EVENT_FAILED_BY_VERB: action %s (%s) was %s; failed: %s",
        action_id,
        process_key,
        previous_status,
        reason.strip(),
    )
    return {
        "action_id": action_id,
        "process_key": process_key,
        "previous_status": previous_status,
        "status": ActionStatus.FAILED.value,
        "error_message": error_message,
    }


__all__ = [
    "RESOLVABLE_STATUSES",
    "ActionEventResolutionError",
    "fail_action_event",
]
