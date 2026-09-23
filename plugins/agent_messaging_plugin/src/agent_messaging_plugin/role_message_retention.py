"""MSG-03 retention rider: age-based GC for terminal ``core__agent_role_message``
rows.

Operator ruling (2026-08-20): 90 days, flat — an age, not a class-tagged
policy. Mirrors :func:`ananta.services.inference_service.completion_request_queue.gc_terminal_completion_requests`
(INF-08) exactly in shape, on this table instead:

* Scoped to TERMINAL rows only (``consumed=true OR escalated=true``) — a row
  still owed (neither flag set) is live work and is never reaped here, same
  as INF-08 leaves ``pending`` alone.
* Aged by ``updated_at`` (the standardizer-injected terminal-flip timestamp,
  same convention INF-08 and ``forwarded_vertex_reconcile.gc_terminal_rows``
  both use), compared by VALUE via :func:`to_naive_utc`, never a lexical
  spelling compare.
* ``never-delete-on-unknown-age``: a row whose ``updated_at`` this function
  cannot read is skipped, not reaped. A destructive GC must never guess an
  age.

Measured before this was written (2026-08-20, INF-10's stopped-queue check):
5,829 live rows total, 3,264 terminal (``consumed`` or ``escalated``), 353
created in the preceding 24h, oldest live row 2026-06-20 (61 days old at
measurement time) — a GROWING table, actively accumulating debris as work
completes, not a stopped queue. At a 90-day cutoff there are currently ZERO
eligible rows; this GC is a standing bound for when that changes, exactly as
INF-08 shipped as a standing bound rather than reaping anything on day one.

Deliberately NOT wired into a scheduled sweep tick, matching INF-08's own
shipped shape: this module has no sweeper-tick composition seam of its own,
and inventing one was not part of the ruling. The wiring lives with whichever
caller schedules it.

Deliberately NOT a class-tagged TTL (the other half MSG-03 originally
recommended): the operator ruled a single 90-day age, not per-class values
nobody has set — inventing one would put unruled policy in code, the same
reasoning that held INF-08's own (a) leg refused.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from ananta.core.domain.timestamps import to_naive_utc
from ananta.llm.agent_messaging.schema import (
    COL_CONSUMED,
    COL_ESCALATED,
    NAMESPACE,
    TABLE_AGENT_ROLE_MESSAGE,
)
from ananta.llm.agent_messaging.state_results import require_completed, require_records

if TYPE_CHECKING:
    from ananta.interfaces.state_management_interface import StateManagementInterface

logger = logging.getLogger(__name__)

_COL_ID = "id"
_COL_IS_DELETED = "is_deleted"
_COL_UPDATED_AT = "updated_at"

# Operator ruling, 2026-08-20: "90 days retention for inter-agent messages is Ok."
ROLE_MESSAGE_TERMINAL_GC_AFTER_SECONDS = 90 * 24 * 60 * 60


def _terminal_row_aged(row: dict[str, Any], *, cutoff_iso: str) -> bool:
    """Is this terminal (``consumed``/``escalated``) row older than ``cutoff_iso``?

    A missing/empty ``updated_at`` returns ``False`` (never-delete-on-unknown-
    age): a destructive GC must never reap a row whose age it cannot read. A
    present-but-unparseable stamp is a genuine corruption — ``to_naive_utc``
    raises (fail loud); the caller sees the exception rather than a silent skip.
    """
    updated = row.get(_COL_UPDATED_AT)
    if not (isinstance(updated, str) and updated):
        return False
    return to_naive_utc(updated) < to_naive_utc(cutoff_iso)


def gc_terminal_role_messages(
    state: StateManagementInterface,
    *,
    terminal_gc_after_seconds: int = ROLE_MESSAGE_TERMINAL_GC_AFTER_SECONDS,
) -> int:
    """Hard-delete aged terminal (``consumed``/``escalated``) role-message rows.

    Scoped to the two terminal predicates only — a row that is neither
    ``consumed`` nor ``escalated`` is still-owed work and is never reaped
    here. Queried as two separate passes (the state-interface filter grammar
    is AND-only; there is no OR), ids de-duplicated before delete since a row
    can satisfy both. Returns the number of rows reaped.
    """
    cutoff = (
        datetime.now(UTC) - timedelta(seconds=terminal_gc_after_seconds)
    ).isoformat()
    reaped: set[str] = set()
    for terminal_column in (COL_CONSUMED, COL_ESCALATED):
        rows = require_records(
            state.query_state(
                NAMESPACE,
                {
                    "table": TABLE_AGENT_ROLE_MESSAGE,
                    "filters": {terminal_column: True, _COL_IS_DELETED: 0},
                },
            ),
        )
        for row in rows:
            if not _terminal_row_aged(row, cutoff_iso=cutoff):
                continue
            row_id = str(row.get(_COL_ID) or "")
            if row_id:
                reaped.add(row_id)
    for row_id in reaped:
        require_completed(
            state.delete_records(
                NAMESPACE,
                {
                    "table": TABLE_AGENT_ROLE_MESSAGE,
                    "filters": {_COL_ID: row_id},
                    "soft_delete": False,
                },
            ),
            "hard-delete role message",
        )
    if reaped:
        logger.info(
            "MSG-03 terminal-row GC: reaped %d aged agent_role_message rows",
            len(reaped),
        )
    return len(reaped)


__all__ = [
    "ROLE_MESSAGE_TERMINAL_GC_AFTER_SECONDS",
    "gc_terminal_role_messages",
]
