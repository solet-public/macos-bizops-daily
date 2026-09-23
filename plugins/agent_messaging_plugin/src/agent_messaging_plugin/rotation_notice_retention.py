"""GAU-06 retention: the rotation self-notice writer bounds its OWN thread.

★ WHY THIS EXISTS AT ALL. GAU-06 made the self-notice durable so a watcher's
drain can no longer consume a session's only copy of its own context reading.
Durability without a bound is just a slower failure: measured on 2026-08-18
across 153 consecutive deployed rider ticks, the fleet produced 117 self-notices
in 12h40m -- ~222/day at 752 chars each, ~81,000 rows and ~61MB/year at current
size, into a table with no retention. The rate is band-EDGE gated, not
per-tick: the leg's :class:`~.rotation_self_notice.BandEdgeLatch` already
suppresses repeats inside one band, so this bound counts crossings.

★ HONEST LIMIT, carried forward rather than buried: that projection is a RATE
from an UNMEASURED starting point. ``core__agent_message``'s absolute row count
is not readable through any sanctioned verb, and this lane did not reach for raw
SQL to find out. If the table is already large for unrelated reasons, that
changes retention's urgency -- not this mechanism.

★ THE WRITER BOUNDS ITS OWN TABLE -- no reaper, no schedule, nothing to arm and
forget. Adopted deliberately from GAU-15's ratified gauge-history prune
(:func:`~.session_context_status_store._prune_history`), whose rationale applies
here word for word: an unbounded append-only table with a cleanup job that "will
be added" is how a bounded design becomes an unbounded one in production.

★ AND THE EXCEPTION IT SPENDS. ``core__agent_message`` declares itself
APPEND-ONLY. Deleting from it is a change to that declared semantics, ruled by
the seat on 2026-08-19 as: append-only, EXCEPT event-type-scoped retention on
machine-generated notice threads. The exception is NARROW BY CONSTRUCTION, and
the construction is the sentinel sender rather than a convention anyone must
remember: peer threads key on ``(sender_bridge_id, peer_instance)``, so the
``system:rotation-notice`` sentinel gives these notices a thread of their own per
recipient. This module is handed THAT thread id, by the writer that just wrote
to it. It has no way to name a coordination thread and no path that could reach
one -- the append-only guarantee for every other thread in the table is
unchanged, and the schema's own description now says so.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Final

from ananta.llm.agent_messaging.schema import NAMESPACE, TABLE_AGENT_MESSAGE
from ananta.llm.agent_messaging.state_results import require_deleted, require_records

if TYPE_CHECKING:
    from ananta.interfaces.state_management_interface import StateManagementInterface

ROTATION_NOTICE_RETENTION: Final[int] = 50
"""Newest self-notices kept per recipient thread. INTERIM, seat-set 2026-08-19.

★ THIS NUMBER IS NOT THIS LANE'S TO CHOOSE and is recorded as borrowed: the
retention WINDOW belongs to MSG-03's owner, who re-tunes it against the measured
rate above. 50 band-edge crossings is a long history for one session -- a lane
that crossed a band every hour would hold two days of them -- and the mechanism
is indifferent to the value, so re-tuning is this one line.

A bound that must exist for the mechanism to run at all is not the same thing as
a policy choice about how much history is worth keeping. This constant is the
first; MSG-03 owns the second.
"""

_COL_THREAD_ID = "thread_id"
_COL_CURSOR = "cursor"


def prune_rotation_notices(
    state: StateManagementInterface,
    *,
    thread_id: str,
    keep: int = ROTATION_NOTICE_RETENTION,
) -> int:
    """Hard-delete this thread's notices beyond the newest ``keep``.

    Returns how many rows were deleted, so a caller can log a number rather than
    an assurance.

    ★ ORDERED ON ``cursor``, NOT A TIMESTAMP. The cursor is per-thread
    monotonic and allocated atomically by the repository, and the schema
    declares it unique within a thread. A timestamp ordering would have to
    reason about ties and about a clock that can be wrong; this one cannot tie.

    ★ THE CUTOFF IS READ, NEVER COMPUTED -- the ``cursor`` of the OLDEST row we
    intend to KEEP, taken from a bounded ordered page, with rows strictly below
    it deleted. Computing it (``newest - keep``) would be wrong the moment a
    cursor sequence has a gap, and gaps are ordinary: the repository allocates a
    cursor per append to the thread, and this table's history predates this
    module. Reading it cannot be wrong about a gap it never assumes away.

    ★ HARD DELETE, for GAU-15's stated reason: a soft delete keeps every row
    forever behind an ``is_deleted`` flag, and nothing on this platform ever
    reaps one -- the bound would be cosmetic while the table grew at exactly the
    old rate.

    ★ SHORT PAGE MEANS NOTHING TO DO. Fewer than ``keep`` rows is the normal
    case for most sessions and returns before any delete is issued, so the
    common path costs one bounded read.
    """
    if keep <= 0:
        # A keep of zero would delete the notice this prune was triggered BY.
        # Refusing is the safe direction: the caller's bug becomes an unbounded
        # table, never a session told nothing at all.
        raise ValueError(f"keep must be positive, got {keep}")
    rows = require_records(
        state.query_ordered(
            NAMESPACE,
            {
                "table": TABLE_AGENT_MESSAGE,
                "filters": {_COL_THREAD_ID: thread_id},
                "order_by": [[_COL_CURSOR, "desc"]],
                "limit": keep,
            },
        ),
    )
    if not rows or len(rows) < keep:
        # ``not rows`` is stated SEPARATELY from the short-page test even though
        # an empty page is already shorter than any positive keep. The two are
        # different claims -- "nothing to prune" and "not enough to prune" --
        # and the empty case is the one that would index past the end of the
        # page if a future edit loosened the comparison. A guard that only
        # holds while another guard also holds is not a guard.
        return 0
    cutoff = rows[-1].get(_COL_CURSOR)
    if not isinstance(cutoff, int):
        # An unreadable cutoff means DELETE NOTHING. The filter would otherwise
        # be built from a value the store did not vouch for, and the operation
        # it feeds is a hard delete -- the one direction that cannot be undone
        # by trying again.
        return 0
    return require_deleted(
        state.delete_records(
            NAMESPACE,
            {
                "table": TABLE_AGENT_MESSAGE,
                "filters": {
                    _COL_THREAD_ID: thread_id,
                    _COL_CURSOR: {"op": "lt", "value": cutoff},
                },
                "soft_delete": False,
            },
        ),
    )


__all__ = ["ROTATION_NOTICE_RETENTION", "prune_rotation_notices"]
