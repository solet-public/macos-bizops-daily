"""REL-05 F2 one-time backfill: grandfather role-message history for the
consumption-gated re-emit predicate.

REL-05 (Q5) changes the role drain's stop condition from ``delivered=false`` to
``consumed=false AND emit_count < cap``. Every historical role row is
``delivered=true, consumed=false, emit_count=0``, so the NEW predicate would
RE-OWE the entire role-message history and flood re-emits at the first
post-land drain. This backfill GRANDFATHERS that history: for every
``delivered=true`` row it sets ``consumed=true``, ``consumed_at=<migration-
time>``, ``emit_count=1`` — so only rows created (or emitted) after the land
participate in consumption-gated re-emit. Pre-migration ``delivered=false``
rows are left owed (``consumed=false``): they were genuinely undelivered and
SHOULD still be delivered, exactly as the old repair loop was already doing.

Same ONE-SHOT, durable-marker-gated, self-healing, idempotent shape as
:func:`~agent_messaging_plugin.message_important_backfill.backfill_message_important`:
gated by a durable key-value marker set ONLY after a successful full pass (a
fault before the marker → next boot re-runs); idempotent because it flips ONLY
rows where ``delivered=true`` and ``consumed=false``, so a re-run (or a lost
marker) is a no-op.

State-interface ONLY (no raw SQL/DDL): the whole table is walked as
``query_ordered`` pages via ``iter_table_rows``, then ``update_state`` on each
grandfathered row by id. It rides the INJECTED :class:`StateManagementInterface`
(the platform's
authenticated pool) exactly like ``message_important_backfill``, so it CANNOT
hit the JOS-02 migration-credential failure mode: that migration built its OWN
``PostgresConfig`` from plugin JSON → defaulted ``password='change_me'`` → auth
failure, and its only smoke used a STUB state service so the live-auth path was
never exercised. This backfill never opens a connection; the live path IS the
injected authenticated service, and
``plugins/agent_messaging_plugin/tests/role_message_consumed_backfill_smoke.py``
exercises it against a real (non-stub) state fake.

(That last sentence used to name "S7" and was FALSE — no such test existed
anywhere in the tree, and none had since this module shipped. A coverage claim in
a docstring is worse than silence: it stops the next reader from checking. The
smoke it now names was written on 2026-08-15 and is registered in
``quality_gates/gate_smokes.txt``, which is an allowlist — an unregistered smoke
silently never runs.)

**This read used to be deliberately UNCAPPED**, on the reasoning that
``query_ordered``'s 100-row page cap "would silently truncate this unbounded
table". That was correct against the old 10,000-row default and is now exactly
inverted: the default bound is 100 (``read_bounds.MAX_READ_ROWS``), so the
uncapped read is the one that gets refused, and a sibling read on this same boot
path took ``start_interface`` down on 2026-08-15 for precisely that reason. A
page cap only truncates if you stop after one page — ``iter_table_rows`` pages
until a short page proves the end, on a tie-safe ``(created_at, id)`` cursor.
Grandfathering rows mid-walk is safe: both cursor columns are immutable after
insert.

Nothing fired here only because the durable marker is set on this profile. **A
one-shot marker is not a bound, it is a delay** — any database with pre-migration
history and no marker (a rebuild, a restore) would have booted straight into the
refusal.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from ananta.llm.agent_messaging.schema import (
    COL_CONSUMED,
    COL_CONSUMED_AT,
    COL_EMIT_COUNT,
    NAMESPACE,
    TABLE_AGENT_ROLE_MESSAGE,
)
from ananta.llm.agent_messaging.state_results import (
    require_completed,
    require_updated,
)
from ananta.services.state_service.bounded_read import iter_table_rows

if TYPE_CHECKING:
    from ananta.interfaces.state_management_interface import StateManagementInterface

logger = logging.getLogger(__name__)

_COL_ID = "id"
_COL_DELIVERED = "delivered"

# Measured 4,346 rows on 2026-08-15, growing with every role message. Same
# reasoning as ``message_important_backfill``'s ceiling: below the 109,393-row
# read that caused the 2026-08-15 stall, so a one-shot walk refuses before it can
# become that read again.
_ROLE_MESSAGE_CEILING = 100_000
_CEILING_REASON = (
    "one row per role message ever routed (measured 4,346 on 2026-08-15); this "
    "is a ONE-SHOT grandfathering pass, so the walk happens at most once per "
    "database, and the ceiling stays below the 109,393-row read that caused the "
    "2026-08-15 stall."
)

# Durable one-shot marker (set ONLY after a successful full pass → self-healing).
_BACKFILL_MARKER_KEY = "agent_role_message_consumed_backfill_v1_complete"
_BACKFILL_MARKER_VALUE = "true"

# Returned ``status`` discriminators.
STATUS_COMPLETED = "completed"
STATUS_ALREADY_DONE = "already_done"


def backfill_role_message_consumed(
    state: StateManagementInterface,
) -> dict[str, object]:
    """Grandfather ``delivered=true`` role-message history for the consumed drain.

    Returns ``{"updated": [...ids], "status": ...}``. Gated by a durable marker
    (``status="already_done"`` on every boot after the first successful run). The
    marker is set ONLY after the full pass succeeds, so any fault before it leaves
    the marker unset → the next boot re-runs (self-healing).
    """
    if _backfill_already_complete(state):
        return {"updated": [], "status": STATUS_ALREADY_DONE}
    migration_time = datetime.now(UTC).isoformat()
    updated: list[str] = []
    rows = iter_table_rows(
        state,
        namespace=NAMESPACE,
        table=TABLE_AGENT_ROLE_MESSAGE,
        filters={},
        ceiling=_ROLE_MESSAGE_CEILING,
        reason=_CEILING_REASON,
    )
    for row in rows:
        # `filters` stays empty rather than pushing {consumed: False,
        # delivered: True} down, even though both halves ARE columns and the
        # grammar could express it. Whether pre-migration rows carry False or
        # NULL in those columns is not something this site has measured, and an
        # equality filter would silently skip the NULL ones — which are exactly
        # the rows to grandfather. Pagination fixes the bound without betting on
        # that; pushing the predicate down is a worthwhile follow-up that needs
        # its own measurement first.
        if not _needs_backfill(row):
            continue
        row_id = str(row[_COL_ID])
        require_updated(
            state.update_state(
                NAMESPACE,
                {"table": TABLE_AGENT_ROLE_MESSAGE, "filters": {_COL_ID: row_id}},
                {
                    COL_CONSUMED: True,
                    COL_CONSUMED_AT: migration_time,
                    COL_EMIT_COUNT: 1,
                },
            ),
        )
        updated.append(row_id)
    # SUCCESS — full pass completed without raising. Set the marker ONLY now so a
    # partial/failed run re-runs next boot.
    _mark_backfill_complete(state)
    if updated:
        logger.info(
            "agent_role_message consumed backfill grandfathered %d pre-migration "
            "delivered row(s)",
            len(updated),
        )
    return {"updated": updated, "status": STATUS_COMPLETED}


def _needs_backfill(row: dict[str, object]) -> bool:
    """True iff a delivered row has not yet been grandfathered (consumed=false)."""
    if bool(row.get(COL_CONSUMED, False)):
        return False
    return bool(row.get(_COL_DELIVERED, False))


def _backfill_already_complete(state: StateManagementInterface) -> bool:
    """True once the one-shot marker has been durably set by a successful run."""
    data = require_completed(
        state.get_key_value(NAMESPACE, _BACKFILL_MARKER_KEY),
        "get role-message consumed backfill marker",
    )
    return bool(data.get("found"))


def _mark_backfill_complete(state: StateManagementInterface) -> None:
    """Durably record completion (set on success only → exactly-once + self-healing)."""
    require_completed(
        state.set_key_value(NAMESPACE, _BACKFILL_MARKER_KEY, _BACKFILL_MARKER_VALUE),
        "set role-message consumed backfill marker",
    )


__all__ = ["backfill_role_message_consumed"]
