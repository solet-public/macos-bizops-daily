"""Reap actions stuck in ``processing`` forever (D8).

At the time of the 2026-08-15 incident, 60 rows sat in ``processing``, the
oldest from 2026-05-29. An action claimed by a poller that then died is never
returned to ``queued`` and never failed — it simply stops, and nothing in the
platform notices. INCIDENT.md §2b records that a restarted poller has not
re-claimed the June rows in seven weeks, which independently proves
``processing`` rows are not recovered on restart.

## Fail, do not requeue — this is the load-bearing decision

The intuitive reap is "return it to ``queued`` so it gets another try". For an
oversized payload that is **exactly how the platform re-wedges**: D13 in
INCIDENT.md records that neutralising the two originally-stuck deliveries was
not enough, because the underlying oversized results were still in the queue
and generated fresh oversized deliveries that froze the poller a SECOND time
within four minutes of recovery. A requeued oversized row is a scheduled
outage.

So the reaper is size-aware: a stale row whose payload exceeds
``MAX_ACTION_PARAMETERS_BYTES`` is FAILED with a legible reason and never
retried; only a row under the bound returns to ``queued``. This is why item 1's
byte bound must land with or before this reaper — without it, "reap" and
"re-wedge" are the same operation.

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


def reap_orphaned_processing_actions(
    state_service: _OrderedReader,
    *,
    orphan_age_seconds: float = DEFAULT_ORPHAN_AGE_SECONDS,
    page_limit: int = DEFAULT_REAP_PAGE_LIMIT,
    bound_bytes: int = MAX_ACTION_PARAMETERS_BYTES,
) -> dict[str, int]:
    """Return abandoned ``processing`` actions to ``queued``, or fail them.

    Returns:
        Counts of ``{"examined", "requeued", "failed"}`` for logging. A pass
        that finds nothing returns zeros and logs nothing — this runs
        periodically and must stay silent when there is no work.
    """
    cutoff = _naive_utc_now() - timedelta(seconds=orphan_age_seconds)

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
    requeued = 0
    failed = 0

    for row in rows:
        action_id = row.get("id")
        if not isinstance(action_id, str):
            continue
        process_key = row.get("process_key")
        process_key_str = process_key if isinstance(process_key, str) else "<unknown>"
        raw_parameters = row.get("parameters")
        parameters_str = raw_parameters if isinstance(raw_parameters, str) else "{}"

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
            # D13: requeueing this would re-wedge the poller on the next claim.
            logger.error(
                "ORPHAN_REAP_FAILED: action %s (%s) is %d bytes, over the %d "
                "bound — failing instead of requeueing so it cannot re-wedge "
                "the poller",
                action_id,
                process_key_str,
                exc.size,
                exc.bound,
            )
            state_service.update_state(
                namespace="core",
                query={"table": "action_events", "filters": {"id": action_id}},
                updates={"status": "failed", "error_message": str(exc)},
            )
            failed += 1
            continue

        logger.warning(
            "ORPHAN_REAP_REQUEUED: action %s (%s) was claimed and abandoned "
            "(no progress for >%.0fs); returning it to queued",
            action_id,
            process_key_str,
            orphan_age_seconds,
        )
        state_service.update_state(
            namespace="core",
            query={"table": "action_events", "filters": {"id": action_id}},
            updates={"status": "queued"},
        )
        requeued += 1

    if rows:
        logger.info(
            "ORPHAN_REAP: examined=%d requeued=%d failed=%d (cutoff=%s UTC)",
            len(rows),
            requeued,
            failed,
            cutoff.isoformat(),
        )

    return {"examined": len(rows), "requeued": requeued, "failed": failed}


__all__ = [
    "DEFAULT_ORPHAN_AGE_SECONDS",
    "DEFAULT_REAP_PAGE_LIMIT",
    "EVIDENCE_FLOOR_CREATED_AT",
    "reap_orphaned_processing_actions",
]
