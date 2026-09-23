"""CDX-06 part C (2026-08-24) — the state-layer primitives over
``inbox_consumption_status`` (schema.py). ONE row per ``agent_instance_id``,
always overwritten by the latest report — same single-row-latest posture as
``session_context_status_store.py``, deliberately a SEPARATE table rather
than new columns on that one (see the table's own schema.py comment for why).

Reads first use the exact ``agent_instance_id`` and then, for a watcher-held
session, use the stored stable ``agent_session_id`` supplied by its peer
binding. This is the GAU-07 ledger/watch-id join; the write path has captured
that stable id since this table was introduced, so it needs no backfill.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from ananta.llm.agent_messaging.role_binding import AGENT_ROLE_BINDING_NAMESPACE
from ananta.llm.agent_messaging.state_results import require_completed, require_records

from .schema import TABLE_INBOX_CONSUMPTION_STATUS

if TYPE_CHECKING:
    from ananta.interfaces.state_management_interface import StateManagementInterface

_COL_AGENT_INSTANCE_ID = "agent_instance_id"
_COL_AGENT_SESSION_ID = "agent_session_id"
_COL_IS_DELETED = "is_deleted"
_CONFLICT_COLUMNS = ["agent_instance_id"]


class AmbiguousInboxConsumptionAgentSessionIdError(Exception):
    """More than one inbox-consumption row claims an agent session id.

    The table's unique key is ``agent_instance_id``, not
    ``agent_session_id``. A read through the latter therefore must fail loud
    rather than select an arbitrary report and falsely claim that it belongs
    to the watcher-held session being resolved.
    """

    def __init__(self, agent_session_id: str, agent_instance_ids: list[str]) -> None:
        super().__init__(
            f"{len(agent_instance_ids)} inbox-consumption rows share agent_session_id "
            f"{agent_session_id!r} ({', '.join(agent_instance_ids)}) — refusing to guess "
            "which report belongs to this session."
        )


def upsert_inbox_consumption_status(
    state: StateManagementInterface,
    *,
    agent_instance_id: str,
    runtime: str,
    checked_at: str,
    pending_found_at: str | None,
    pending_reason: str | None,
    reporter_surface: str | None,
    agent_session_id: str | None,
) -> None:
    """Overwrite the caller's own latest inbox-consumption-check row.
    Conflicts on ``agent_instance_id`` alone, so a retry after a transient
    fault re-sends the SAME latest state rather than duplicating a row."""
    record: dict[str, Any] = {
        _COL_AGENT_INSTANCE_ID: agent_instance_id,
        "runtime": runtime,
        "checked_at": checked_at,
        "pending_found_at": pending_found_at,
        "pending_reason": pending_reason,
        "reporter_surface": reporter_surface,
        "agent_session_id": agent_session_id,
    }
    require_completed(
        state.upsert_state(
            AGENT_ROLE_BINDING_NAMESPACE,
            {
                "table": TABLE_INBOX_CONSUMPTION_STATUS,
                "record": record,
                "conflict_columns": _CONFLICT_COLUMNS,
            },
        ),
        "upsert inbox_consumption_status",
    )


def read_inbox_consumption_status(
    state: StateManagementInterface, agent_instance_id: str,
) -> dict[str, Any] | None:
    """The latest row for ``agent_instance_id``, or ``None`` when this
    session's consumer hook has never reported — a fresh session, a
    `host=operator` seat, or a runtime (Claude) that uses its own separate
    honest wake mechanism rather than this table. Never raises on absence;
    callers translate ``None`` into their own ``resolved=False`` contract."""
    result = state.query_state(
        AGENT_ROLE_BINDING_NAMESPACE,
        {
            "table": TABLE_INBOX_CONSUMPTION_STATUS,
            "filters": {_COL_AGENT_INSTANCE_ID: agent_instance_id, _COL_IS_DELETED: 0},
        },
    )
    records = require_records(result)
    return records[0] if records else None


def read_inbox_consumption_status_by_agent_session_id(
    state: StateManagementInterface, agent_session_id: str,
) -> dict[str, Any] | None:
    """The row carrying ``agent_session_id``, or ``None`` when no unique row
    can be reached through that stable identity.

    An empty id must short-circuit: it denotes a pre-join reporter, not a
    value that may be passed as a bare null-like filter. Multiple matches fail
    loud because this table has no uniqueness guarantee on the join column.
    """
    if not agent_session_id.strip():
        return None
    result = state.query_state(
        AGENT_ROLE_BINDING_NAMESPACE,
        {
            "table": TABLE_INBOX_CONSUMPTION_STATUS,
            "filters": {_COL_AGENT_SESSION_ID: agent_session_id, _COL_IS_DELETED: 0},
        },
    )
    records = require_records(result)
    if not records:
        return None
    if len(records) > 1:
        raise AmbiguousInboxConsumptionAgentSessionIdError(
            agent_session_id,
            [str(row.get(_COL_AGENT_INSTANCE_ID) or "") for row in records],
        )
    return dict(records[0])


__all__ = [
    "AmbiguousInboxConsumptionAgentSessionIdError",
    "read_inbox_consumption_status",
    "read_inbox_consumption_status_by_agent_session_id",
    "upsert_inbox_consumption_status",
]
