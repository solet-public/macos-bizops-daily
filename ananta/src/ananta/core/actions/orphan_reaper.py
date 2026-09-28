"""Reap actions stuck in ``processing`` forever (D8) by failing them.

At the time of the 2026-08-15 incident, 60 rows sat in ``processing``, the
oldest from 2026-05-29. An action claimed by a poller that then died is never
returned to ``queued`` and never failed — it simply stops, and nothing in the
platform notices. INCIDENT.md §2b records that a restarted poller has not
re-claimed the June rows in seven weeks, which independently proves
``processing`` rows are not recovered on restart.

## Fail, do not requeue — this is the load-bearing decision

The intuitive reap is "return it to ``queued`` so it gets another try". Every
stale row is FAILED instead, with a legible ``error_message`` naming its orphan
age, and is never re-run. Two incidents each proved requeueing wrong:

- **Oversized payloads (D13, 2026-08-15).** Neutralising the two
  originally-stuck deliveries was not enough, because the underlying oversized
  results were still in the queue and generated fresh oversized deliveries
  that froze the poller a SECOND time within four minutes of recovery. A
  requeued oversized row is a scheduled outage.
- **Long inline handlers (iss_30fb08fd, 2026-09-28).** A 17-20 minute inline
  corpus audit was SIGKILLed mid-run, leaving its row ``processing``. An hour
  later this reaper requeued it, the next poll re-ran the audit and stalled the
  serial dispatch path again, and a second SIGKILL re-armed the same loop. A
  requeue re-runs whatever killed the last process.

And a requeue helps nobody: by the time a row is an hour stale its caller has
long since timed out (the CLI waits 120 s), so the re-run's result is delivered
to no one. A caller that still wants the work resubmits it. An oversized row
keeps its size-specific reason (the payload bound is still measured pre-parse,
so classifying it never pays the cost that made it an orphan). A single row
can also be resolved deliberately, without waiting for the age threshold, with
``fail_action_event`` in ``action_event_resolution``.

## Two hazards this module handles explicitly

**The seven-hour clock hazard (D11).** ``core__action_events.updated_at`` is
``timestamp WITHOUT time zone`` holding UTC, while Postgres ``now()`` returns
local time. A naive comparison is wrong by seven hours *in the direction that
reaps live rows* — it would treat actions claimed moments ago as long-dead and
fail work that is running fine. Every timestamp this module binds is an
explicit naive-UTC value, never a database ``now()``.

**No lease column, so no "dead poller" predicate.** ``action_events`` carries
no claimant id and no lease (``_mark_action_processing`` writes ``status``
only), so "claimed by a poller that no longer exists" is not expressible. A
boot-time sweep would be the usual substitute, but it is unsafe here: under
blue-green a second colour's poller may legitimately hold rows, and a sweep
would fail its live work. The conservative substitute is an age threshold set
far beyond any legitimate action runtime — it reaps strictly less than a
correct lease would, which is the right direction to be wrong in.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import Protocol

from ananta.core.actions.payload_bounds import (
    MAX_ACTION_PARAMETERS_BYTES,
    OversizedActionPayloadError,
    check_claimed_parameters_size,
)
from ananta.core.actions.state_update_result import updated_row_count
from ananta.core.domain.types import ActionResult

logger = logging.getLogger(__name__)

# An action still ``processing`` after this long is not running; it is
# abandoned. Deliberately generous — legitimate handlers must return promptly
# (the action-queue fast-return contract), so an hour is already two orders of
# magnitude beyond a well-behaved dispatch, and reaping too little is safe
# while reaping too much fails live work.
DEFAULT_ORPHAN_AGE_SECONDS = 3600.0

# Rows examined per pass. Small on purpose: the state grammar has no column
# projection (``select`` is ``SELECT *``), so every row read carries its full
# ``parameters`` payload over the wire. A small page bounds how much a single
# pass can pull if an oversized orphan is present.
DEFAULT_REAP_PAGE_LIMIT = 25

# Rows created at or before this instant are the preserved June evidence
# (``ae-2m3q4msgmiekh``, 601 MB, and ``ae-2m3r7oyi13vv4``, 21 MB, both
# 2026-06-28), which are cited in the incident record and must not be touched.
# Expressed as a ``created_at`` floor rather than an id exclusion for two
# reasons: the filter grammar has no NOT-IN, and — more importantly — a floor
# is applied IN SQL, so the 601 MB payload is never transferred at all. An
# id-based exclusion would have to read the row in order to skip it.
EVIDENCE_FLOOR_CREATED_AT = datetime(2026, 7, 1, 0, 0, 0)  # noqa: DTZ001 — naive UTC by design


class _OrderedReader(Protocol):
    """The slice of the state interface this module needs."""

    def query_ordered(self, namespace: str, data: dict[str, object]) -> ActionResult: ...

    def update_state(
        self, namespace: str, query: dict[str, object], updates: dict[str, object],
    ) -> ActionResult: ...


def _naive_utc_now() -> datetime:
    """Current time as a NAIVE UTC datetime, matching the column's basis.

    The explicit ``replace(tzinfo=None)`` on a UTC-aware value is the whole
    point: it produces the same wall-clock reading the column stores. Using
    ``datetime.now()`` (local) or a database ``now()`` here would introduce the
    seven-hour D11 skew in the direction that reaps live rows.
    """
    return datetime.now(UTC).replace(tzinfo=None)


def _extract_records(result: ActionResult) -> list[dict[str, object]]:
    data = result.get("data")
    if not isinstance(data, dict):
        return []
    records = data.get("records")
    if not isinstance(records, list):
        return []
    return [row for row in records if isinstance(row, dict)]


def _orphan_age_seconds(updated_at: object, now: datetime) -> float:
    """Seconds since a row's naive-UTC ``updated_at``, which the query bound.

    The state layer returns the column as a ``datetime`` or, through some
    serialisers, an ISO-8601 string; anything else is a contract break and
    raises rather than guessing an age for the error message.
    """
    if isinstance(updated_at, str):
        updated_at = datetime.fromisoformat(updated_at)
    if not isinstance(updated_at, datetime):
        raise TypeError(f"action_events.updated_at is not a timestamp: {updated_at!r}")
    return (now - updated_at.replace(tzinfo=None)).total_seconds()


def abandoned_error_message(age_seconds: float, orphan_age_seconds: float) -> str:
    """The legible reason written to a reaped row's ``error_message``."""
    return (
        f"abandoned by a dead poller: still processing after {age_seconds:.0f}s "
        f"with no progress (orphan threshold {orphan_age_seconds:.0f}s); failed, "
        "not re-run — resubmit if the work is still wanted"
    )


def reap_orphaned_processing_actions(
    state_service: _OrderedReader,
    *,
    orphan_age_seconds: float = DEFAULT_ORPHAN_AGE_SECONDS,
    page_limit: int = DEFAULT_REAP_PAGE_LIMIT,
    bound_bytes: int = MAX_ACTION_PARAMETERS_BYTES,
) -> dict[str, int]:
    """Fail abandoned ``processing`` actions; never return them to ``queued``.

    Returns:
        Counts of ``{"examined", "failed", "oversized"}`` for logging
        (``oversized`` is the subset of ``failed`` over the payload bound). A
        pass that finds nothing returns zeros and logs nothing — this runs
        periodically and must stay silent when there is no work.
    """
    now = _naive_utc_now()
    cutoff = now - timedelta(seconds=orphan_age_seconds)

    # Two predicates on two DIFFERENT columns, because the filter grammar
    # allows one op per column: staleness on ``updated_at``, evidence
    # preservation on ``created_at``. Both bound in SQL, so the preserved June
    # rows are excluded before any byte of their payload is transferred.
    result = state_service.query_ordered(
        "core",
        {
            "table": "action_events",
            "filters": {
                "status": "processing",
                "updated_at": {"op": "lt", "value": cutoff},
                "created_at": {"op": "gt", "value": EVIDENCE_FLOOR_CREATED_AT},
            },
            "order_by": [["updated_at", "asc"], ["id", "asc"]],
            "limit": page_limit,
            "include_deleted": True,
        },
    )

    rows = _extract_records(result)
    failed = 0
    oversized = 0

    for row in rows:
        action_id = row.get("id")
        if not isinstance(action_id, str):
            continue
        process_key = row.get("process_key")
        process_key_str = process_key if isinstance(process_key, str) else "<unknown>"
        raw_parameters = row.get("parameters")
        parameters_str = raw_parameters if isinstance(raw_parameters, str) else "{}"
        age_seconds = _orphan_age_seconds(row.get("updated_at"), now)

        try:
            # Pre-parse byte check, same guard the dispatch path uses. Nothing
            # here parses the payload — an oversized orphan must be classified
            # WITHOUT paying the cost that made it an orphan in the first place.
            check_claimed_parameters_size(
                parameters_str,
                action_id=action_id,
                process_key=process_key_str,
                bound=bound_bytes,
            )
        except OversizedActionPayloadError as exc:
            logger.error(
                "ORPHAN_REAP_FAILED: action %s (%s) is %d bytes, over the %d "
                "bound, and was abandoned %.0fs ago — failing it so it cannot "
                "re-wedge the poller",
                action_id,
                process_key_str,
                exc.size,
                exc.bound,
                age_seconds,
            )
            error_message = f"{exc}; {abandoned_error_message(age_seconds, orphan_age_seconds)}"
            is_oversized = True
        else:
            logger.error(
                "ORPHAN_REAP_FAILED: action %s (%s) was claimed and abandoned "
                "(no progress for %.0fs, threshold %.0fs); failing it, not "
                "requeueing — a re-run would repeat whatever killed its poller",
                action_id,
                process_key_str,
                age_seconds,
                orphan_age_seconds,
            )
            error_message = abandoned_error_message(age_seconds, orphan_age_seconds)
            is_oversized = False

        # Guarded on ``status='processing'`` (review N1): if the row completed
        # or was failed by someone else between the read above and this write,
        # the write touches nothing and this pass leaves it alone.
        updated = updated_row_count(
            state_service.update_state(
                namespace="core",
                query={
                    "table": "action_events",
                    "filters": {"id": action_id, "status": "processing"},
                },
                updates={"status": "failed", "error_message": error_message},
            ),
            what=f"orphan reap of {action_id}",
        )
        if updated == 0:
            logger.warning(
                "ORPHAN_REAP_SKIPPED: action %s left processing before it could be "
                "failed; leaving it as it is",
                action_id,
            )
            continue
        failed += 1
        oversized += int(is_oversized)

    if rows:
        logger.info(
            "ORPHAN_REAP: examined=%d failed=%d oversized=%d (cutoff=%s UTC)",
            len(rows),
            failed,
            oversized,
            cutoff.isoformat(),
        )

    return {"examined": len(rows), "failed": failed, "oversized": oversized}


__all__ = [
    "DEFAULT_ORPHAN_AGE_SECONDS",
    "DEFAULT_REAP_PAGE_LIMIT",
    "EVIDENCE_FLOOR_CREATED_AT",
    "abandoned_error_message",
    "reap_orphaned_processing_actions",
]
