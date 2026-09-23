"""GAP-2 one-time backfill: project ``metadata.important`` → the ``important`` column.

The SQL-lockdown migration promoted the silent-bucket discriminator from a raw
``metadata->>'important'`` JSONB predicate (which the state interface cannot
express) to a first-class boolean column on ``core__agent_message`` (schema.py).
The additive column lands with ``default=False``, so EXISTING rows whose
``metadata.important`` is true would default to ``important=False`` and wrongly
re-surface in the silent peer-inbox (``list_peer_messages_for(silent_only=True)``).
This backfill copies the JSONB flag onto the column for those pre-migration rows.

**ONE-SHOT, durable-marker-gated**: runs to
completion EXACTLY ONCE, gated by a durable key-value marker set ONLY after a
SUCCESSFUL full pass. Set-on-success-only is self-healing — a fault before the
marker leaves it unset → the next boot re-runs. Idempotent regardless: it flips
ONLY rows where the JSONB flag is true and the column is still false, so a re-run
(or a lost marker) is a no-op.

State-interface ONLY (no raw SQL/DDL): the whole table is walked as ``query_ordered``
pages via ``iter_table_rows``, then ``update_state`` flips the column on the
matching rows (a small set; IMPORTANT messages are a fraction). New rows get the
column from ``AgentMessagingRepository._insert_message`` directly, so this
backfill is purely for pre-migration history.

**This read used to be deliberately UNCAPPED**, on the reasoning that
``query_ordered``'s 100-row page cap "would silently truncate this unbounded
table". That was correct against the old 10,000-row default and is now exactly
inverted: the default bound is 100 (``read_bounds.MAX_READ_ROWS``), so the
uncapped read is the one that gets refused, and a sibling read on this same boot
path took ``start_interface`` down on 2026-08-15 for precisely that reason. A
page cap only truncates if you stop after one page — ``iter_table_rows`` pages
until a short page proves the end, on a tie-safe ``(created_at, id)`` cursor, so
the walk is complete AND bounded per page. Flipping ``important`` mid-walk is
safe: both cursor columns are immutable after insert.

Nothing fired here only because the durable marker is set on this profile. **A
one-shot marker is not a bound, it is a delay** — any database with pre-migration
history and no marker (a rebuild, a restore) would have booted straight into the
refusal.
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING

from ananta.llm.agent_messaging.schema import NAMESPACE, TABLE_AGENT_MESSAGE
from ananta.llm.agent_messaging.state_results import (
    require_completed,
    require_updated,
)
from ananta.services.state_service.bounded_read import iter_table_rows

if TYPE_CHECKING:
    from ananta.interfaces.state_management_interface import StateManagementInterface

logger = logging.getLogger(__name__)

_COL_ID = "id"
_COL_IMPORTANT = "important"
_COL_METADATA = "metadata"

# Measured 14,270 rows on 2026-08-15, growing with every message sent. The
# ceiling sits BELOW 109,393 on purpose: that is the size of the unbounded read
# that froze the platform for 3h20m and started this whole programme, so a
# one-shot walk here refuses before it can become that same read again. The
# remedy at that point is not a bigger number — it is expressing the projection
# as a set-based statement in the database, which the state interface cannot do
# today because the source predicate is a JSONB field.
_AGENT_MESSAGE_CEILING = 100_000
_CEILING_REASON = (
    "one row per agent message ever sent (measured 14,270 on 2026-08-15); this "
    "is a ONE-SHOT pre-migration projection, so the walk happens at most once "
    "per database, and the ceiling stays below the 109,393-row read that caused "
    "the 2026-08-15 stall."
)

# Durable one-shot marker (set ONLY after a successful full pass → self-healing).
_BACKFILL_MARKER_KEY = "agent_message_important_backfill_v1_complete"
_BACKFILL_MARKER_VALUE = "true"

# Returned ``status`` discriminators.
STATUS_COMPLETED = "completed"
STATUS_ALREADY_DONE = "already_done"


def backfill_message_important(state: StateManagementInterface) -> dict[str, object]:
    """ONE-SHOT projection of ``metadata.important`` onto the ``important`` column.

    Returns ``{"updated": [...ids], "status": ...}``. Gated by a durable marker
    (``status="already_done"`` on every boot after the first successful run). The
    marker is set ONLY after the full pass succeeds, so any fault before it leaves
    the marker unset → the next boot re-runs (self-healing).
    """
    if _backfill_already_complete(state):
        return {"updated": [], "status": STATUS_ALREADY_DONE}
    updated: list[str] = []
    rows = iter_table_rows(
        state,
        namespace=NAMESPACE,
        table=TABLE_AGENT_MESSAGE,
        filters={},
        ceiling=_AGENT_MESSAGE_CEILING,
        reason=_CEILING_REASON,
    )
    for row in rows:
        # `filters` stays empty rather than pushing `important=False` down. The
        # column landed additively with default=False, and whether pre-migration
        # rows carry False or NULL is not something this site has measured — an
        # equality filter would silently skip the NULL ones, which are exactly
        # the rows the backfill exists for. Pagination fixes the bound without
        # betting on that; narrowing the predicate is a separate change that
        # needs its own measurement first.
        if not _needs_backfill(row):
            continue
        message_id = str(row[_COL_ID])
        require_updated(
            state.update_state(
                NAMESPACE,
                {"table": TABLE_AGENT_MESSAGE, "filters": {_COL_ID: message_id}},
                {_COL_IMPORTANT: True},
            ),
        )
        updated.append(message_id)
    # SUCCESS — full pass completed without raising. Set the marker ONLY now so a
    # partial/failed run re-runs next boot.
    _mark_backfill_complete(state)
    if updated:
        logger.info(
            "agent_message important backfill flipped %d pre-migration row(s)",
            len(updated),
        )
    return {"updated": updated, "status": STATUS_COMPLETED}


def _needs_backfill(row: dict[str, object]) -> bool:
    """True iff ``metadata.important`` is truthy but the column is still false."""
    if bool(row.get(_COL_IMPORTANT, False)):
        return False
    return _metadata_important(row.get(_COL_METADATA))


def _metadata_important(value: object) -> bool:
    """Truthy ``metadata.important`` from a dict or JSON-string cell (dual-shape)."""
    if isinstance(value, str):
        value = json.loads(value) if value else {}
    if isinstance(value, dict):
        return bool(value.get("important", False))
    return False


def _backfill_already_complete(state: StateManagementInterface) -> bool:
    """True once the one-shot marker has been durably set by a successful run."""
    data = require_completed(
        state.get_key_value(NAMESPACE, _BACKFILL_MARKER_KEY),
        "get message-important backfill marker",
    )
    return bool(data.get("found"))


def _mark_backfill_complete(state: StateManagementInterface) -> None:
    """Durably record completion (set on success only → exactly-once + self-healing)."""
    require_completed(
        state.set_key_value(NAMESPACE, _BACKFILL_MARKER_KEY, _BACKFILL_MARKER_VALUE),
        "set message-important backfill marker",
    )


__all__ = ["backfill_message_important"]
