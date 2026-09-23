"""GAU-21 — state-layer primitives over `gauge_notice_record` (schema.py).

The durable counterpart to the sweep's in-memory gauge notices. The emit site
writes one row per notice it DECIDES to fire, before and independently of the
steward binding, so a notice that reached nobody still leaves a trace; readers
get a by-type, by-subject, time-windowed page that does NOT consume what it
reads.

★ THE READ IS NON-DESTRUCTIVE, AND THAT IS THE POINT. The bridge event queue
this replaces hands a reader the pending events and then rebinds the queue to
exclude them, so a verifier polling it races the steward and can swallow the
very notice the steward needed. A verifier built on that queue would damage
the operational path it is supposed to be checking. Ordinary bounded SELECTs
have no such edge.

★ THE WRITER BOUNDS ITS OWN TABLE, exactly as `session_context_status_history`
does — pruned at write time, hard-deleted, no reaper to arm and forget and no
`is_deleted` flag that nothing in this platform ever clears.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import TYPE_CHECKING, Any

from ananta.llm.agent_messaging.role_binding import AGENT_ROLE_BINDING_NAMESPACE
from ananta.llm.agent_messaging.state_results import require_completed, require_records

from .release_identity import running_release_id
from .schema import TABLE_GAUGE_NOTICE_RECORD

if TYPE_CHECKING:
    from ananta.interfaces.state_management_interface import StateManagementInterface

logger = logging.getLogger(__name__)

GAUGE_NOTICE_RETENTION = 64
"""How many notice rows to keep per (``agent_instance_id``, ``notice_type``).

★ BOUNDED BY THE STATE LAYER, NOT BY TASTE. ``query_ordered`` refuses a limit
above 100 unless the caller passes ``unbounded=True`` to consent to the larger
scan, and the prune reads a page of exactly this size on every write. A larger
retention would therefore put an opted-in oversized scan on the sweep's own
emit path — paid on every notice, to bound a table whose rows are small. 64
keeps every read inside the sanctioned default, and matches the gauge series'
own retention rather than inventing a second number to keep true.

Within that ceiling it is chosen against how these notices actually arrive,
which is TWO different rates depending on an outcome this table is the first
thing to record:

* DELIVERED notices are latched — the sweep records a send and suppresses the
  key until the condition clears — so a dark or arrested session produces one
  row per EPISODE, and 64 is a long history of distinct episodes.
* UNDELIVERABLE notices are not. The latch is armed only on a successful send,
  deliberately, so that a steward who binds later still gets the notice; an
  unbound steward therefore re-fires every sweep tick. At the 300s cadence that
  is ~12 rows/hour for that subject, so 64 is ~5h of history rather than 64
  episodes — still comfortably longer than the 85-minute freeze that motivated
  this family, which is the bound that has to hold.

Both are bounded, which is what the number has to guarantee, and the second
rate is stated here rather than discovered later because a retention figure
justified by an episode count would silently be a day's worth in the exact case
this table was built to make visible. Latching the record instead was rejected:
it would either change delivery semantics (suppressing a notice a late-binding
steward should still receive) or hide a genuinely repeating fault behind one
stale row.
"""

MAX_READ_ROWS = 64
"""Hard ceiling on one read, stated rather than silent.

Equal to the retention bound so a caller asking for everything a subject could
have retained gets it in one page, and paired with an explicit ``truncated``
flag so a reader can never mistake a capped page for a complete one. The read
asks for one row MORE than the cap to derive ``truncated``, so this must stay
below the state layer's own 100-row ceiling as well.
"""

_COL_AGENT_INSTANCE_ID = "agent_instance_id"
_COL_EMITTED_AT = "emitted_at"
_COL_NOTICE_TYPE = "notice_type"


def _prune_notice_records(
    state: StateManagementInterface, *, agent_instance_id: str, notice_type: str,
) -> None:
    """Hard-delete this (subject, type)'s rows beyond the newest
    :data:`GAUGE_NOTICE_RETENTION`.

    Pruned per (subject, type) rather than per subject: the two legs answer
    different questions, and a session that goes dark often enough would
    otherwise evict its own staleness history with coverage rows, silently
    turning one leg's retention into a function of the other leg's volume.

    The cutoff is READ from a bounded ordered page, never computed from a
    clock, and ties on ``emitted_at`` are kept rather than deleted — deleting a
    tie could drop the row just written, and keeping one costs a row.
    """
    rows = require_records(
        state.query_ordered(
            AGENT_ROLE_BINDING_NAMESPACE,
            {
                "table": TABLE_GAUGE_NOTICE_RECORD,
                "filters": {
                    _COL_AGENT_INSTANCE_ID: agent_instance_id,
                    _COL_NOTICE_TYPE: notice_type,
                },
                "order_by": [[_COL_EMITTED_AT, "desc"], ["id", "desc"]],
                "limit": GAUGE_NOTICE_RETENTION,
            },
        ),
    )
    if len(rows) < GAUGE_NOTICE_RETENTION:
        return
    cutoff = rows[-1].get(_COL_EMITTED_AT)
    if not cutoff:
        return
    require_completed(
        state.delete_records(
            AGENT_ROLE_BINDING_NAMESPACE,
            {
                "table": TABLE_GAUGE_NOTICE_RECORD,
                "filters": {
                    _COL_AGENT_INSTANCE_ID: agent_instance_id,
                    _COL_NOTICE_TYPE: notice_type,
                    _COL_EMITTED_AT: {"op": "lt", "value": cutoff},
                },
                "soft_delete": False,
            },
        ),
        "prune gauge_notice_record",
    )


def record_gauge_notice(
    state: StateManagementInterface,
    *,
    notice_type: str,
    agent_instance_id: str,
    emitted_at: str,
    delivery_outcome: str,
    steward_instance_id: str | None = None,
    release_id: str | None = None,
    threshold_s: float | None = None,
    observed_s: float | None = None,
    last_report_alive_at: str | None = None,
    gauge_measured_at: str | None = None,
) -> None:
    """Append one durable notice record, then prune this (subject, type).

    RAISES on failure rather than swallowing. The best-effort posture GAU-21
    rules for this path belongs at the EMIT SITE, where a record fault must not
    cost the other rows their notice — but it belongs there VISIBLY, in a
    caller that catches and warns. A store that silently returned False would
    push that decision somewhere no reader of the sweep can see it, and a
    bookkeeping table that fails invisibly is the defect this table exists to
    end, one layer down.

    Every optional column is genuinely optional and stays ``None`` when it does
    not apply, because the two legs measure different things: a coverage notice
    has no gauge row to timestamp and no lifecycle clock it diverged from, and
    writing zeros there would make "there was no gauge row" indistinguishable
    from "the gauge read the epoch".
    """
    record: dict[str, Any] = {
        _COL_NOTICE_TYPE: notice_type,
        _COL_AGENT_INSTANCE_ID: agent_instance_id,
        _COL_EMITTED_AT: emitted_at,
        "delivery_outcome": delivery_outcome,
        "steward_instance_id": steward_instance_id,
        "release_id": release_id,
        "threshold_s": threshold_s,
        "observed_s": observed_s,
        "last_report_alive_at": last_report_alive_at,
        "gauge_measured_at": gauge_measured_at,
    }
    require_completed(
        state.write_state(
            AGENT_ROLE_BINDING_NAMESPACE,
            {"table": TABLE_GAUGE_NOTICE_RECORD, "record": record},
        ),
        "append gauge_notice_record",
    )
    _prune_notice_records(
        state, agent_instance_id=agent_instance_id, notice_type=notice_type,
    )


def read_gauge_notice_records(
    state: StateManagementInterface,
    *,
    notice_type: str | None = None,
    agent_instance_id: str | None = None,
    since: str | None = None,
    limit: int = MAX_READ_ROWS,
) -> tuple[list[dict[str, Any]], bool]:
    """``(rows_newest_first, truncated)`` for this filter.

    Each filter is OMITTED when it is ``None`` rather than matched against
    NULL: ``None`` here means "do not narrow on this", which is a different
    question from "rows whose column IS NULL", and conflating them would make
    an unfiltered read silently return only the rows with no steward.

    ``truncated`` is derived by asking for one row more than the cap and
    reporting whether it came back — a count the caller cannot get wrong,
    rather than a flag set beside a slice that a later edit can desynchronise.
    """
    capped = max(1, min(limit, MAX_READ_ROWS))
    filters: dict[str, Any] = {}
    if notice_type is not None:
        filters[_COL_NOTICE_TYPE] = notice_type
    if agent_instance_id is not None:
        filters[_COL_AGENT_INSTANCE_ID] = agent_instance_id
    if since is not None:
        filters[_COL_EMITTED_AT] = {"op": "gte", "value": since}
    rows = require_records(
        state.query_ordered(
            AGENT_ROLE_BINDING_NAMESPACE,
            {
                "table": TABLE_GAUGE_NOTICE_RECORD,
                "filters": filters,
                "order_by": [[_COL_EMITTED_AT, "desc"], ["id", "desc"]],
                "limit": capped + 1,
            },
        ),
    )
    return rows[:capped], len(rows) > capped



def record_notice_best_effort(
    state: StateManagementInterface,
    *,
    notice_type: str,
    agent_instance_id: str,
    delivery_outcome: str,
    steward_instance_id: str | None,
    clock: datetime,
    threshold_s: float,
    observed_s: float,
    last_report_alive_at: datetime | None = None,
    gauge_measured_at: datetime | None = None,
) -> None:
    """GAU-21 — durably record that a gauge notice fired, whatever delivery did.

    ★ WHY THIS IS NOT GATED ON THE STEWARD BINDING. Every notify path in this
    module resolves the steward FIRST and returns early when it is ``None``, so
    an alarm about an unbound session reaches nobody AND leaves nothing behind:
    from outside, a detector that fired into the void and a detector that never
    fired are the same silence. That is the whole count-4 defect, and the fix is
    structural — this runs on every decided notice, and ``no_steward_binding``
    is a RECORDED OUTCOME rather than an early return.

    ★ BEST-EFFORT, AND VISIBLY SO — WHICH IS WHY IT IS ITS OWN FUNCTION. The
    record is bookkeeping riding a loop whose job is operational: a record fault
    must never cost the OTHER rows in that tick their notice, exactly as the
    sibling ``append_event`` faults never do. So this warns and returns rather
    than raising.

    That posture lives in a SEPARATE, PLAINLY-NAMED function rather than inside
    :func:`record_gauge_notice`, which still raises. A single store call that
    quietly swallowed would hide the decision from every caller, including the
    ones for which a lost record is not acceptable — and a bookkeeping table
    that fails invisibly is the defect this table exists to end, one layer
    down. Two contracts, both stated, is what keeps the swallow honest.

    The thresholds are recorded AS MEASURED AT EMIT TIME rather than left for a
    reader to re-derive, because a reader's copy of them has already been wrong:
    the running release evaluated coverage at 300s while master's source read
    600s, so a stored row dated by master's constants would misreport by a
    factor of two in a direction nothing announced.
    """
    try:
        record_gauge_notice(
            state,
            notice_type=notice_type,
            agent_instance_id=agent_instance_id,
            emitted_at=clock.isoformat(),
            delivery_outcome=delivery_outcome,
            steward_instance_id=steward_instance_id,
            release_id=running_release_id(),
            threshold_s=threshold_s,
            observed_s=observed_s,
            last_report_alive_at=(
                None if last_report_alive_at is None else last_report_alive_at.isoformat()
            ),
            gauge_measured_at=(
                None if gauge_measured_at is None else gauge_measured_at.isoformat()
            ),
        )
    except Exception:  # noqa: BLE001 — bookkeeping never fails the sweep loop
        logger.warning(
            "session %s %s durable record failed",
            agent_instance_id, notice_type, exc_info=True,
        )

__all__ = [
    "GAUGE_NOTICE_RETENTION",
    "MAX_READ_ROWS",
    "read_gauge_notice_records",
    "record_gauge_notice",
    "record_notice_best_effort",
]
