"""maintenance-verbs M1 (workbench
2026-08-09_maintenance_verbs_m0_design_mverbs-impl.md §2.3, shape (a)) — the
state-layer primitives over `session_context_status` (schema.py). ONE row per
`agent_instance_id`, always overwritten by the latest report — the reporting
hook's own cadence is the freshness bound, this table just holds the newest
value. Same decoupled-from-managed_session posture as
`session_claude_mapping_store.py`: no FK, keyed on a bare `agent_instance_id`
string, so a `host=operator` row (never present in `managed_session`) is
still representable here.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from ananta.llm.agent_messaging.role_binding import AGENT_ROLE_BINDING_NAMESPACE
from ananta.llm.agent_messaging.state_results import require_completed, require_records
from ananta.services.state_service.bounded_read import iter_table_rows

from .schema import (
    PEER_BINDING_NAMESPACE,
    PEER_BINDING_TABLE,
    TABLE_SESSION_CONTEXT_STATUS,
    TABLE_SESSION_CONTEXT_STATUS_HISTORY,
)

if TYPE_CHECKING:
    from ananta.interfaces.state_management_interface import StateManagementInterface

GAUGE_HISTORY_RETENTION = 64
"""How many history rows to keep per ``agent_instance_id`` (GAU-15, ratified
2026-08-19). At the reporting hook's 120s throttle that is >=2h of series per
session — longer than the 85-minute freeze that motivated the table, which is
the bound that matters: a retention shorter than the incident it must explain
would discard the evidence before anyone reads it."""

_COL_AGENT_INSTANCE_ID = "agent_instance_id"
_COL_AGENT_SESSION_ID = "agent_session_id"
_COL_IS_DELETED = "is_deleted"
_COL_MEASURED_AT = "measured_at"
_COL_RECORDED_AT = "recorded_at"
_CONFLICT_COLUMNS = ["agent_instance_id"]

_CONTEXT_STATUS_CEILING = 1_000_000
_CONTEXT_STATUS_CEILING_REASON = (
    "self-notice needs every fresh gauge plus every current live or peer-bound "
    "identity; the walk stays paged while this remains a bounded fleet projection"
)


def _as_flag(value: bool | None) -> int | None:
    """``True``/``False`` -> 1/0, and ``None`` STAYS ``None``.

    Deliberately not ``int(bool(value))``: that maps ``None`` to 0, collapsing
    "not reported" into "cache is warm" — the exact distinction these columns
    exist to preserve.
    """
    return None if value is None else int(value)


class GaugeHistoryAppendError(Exception):
    """The cache write COMMITTED and its history append did not (GAU-15).

    ★ THIS ERROR EXISTS TO NAME A SPLIT STATE, not merely to fail. The two
    writes are deliberately NOT in one transaction (see
    :func:`upsert_session_context_status`), so by the time an append can fail
    the operational row is already durable. A blanket "upsert failed" would
    therefore report as failed a write that half-succeeded — and a caller that
    believed it would re-send a cache write that already landed. Naming the
    state is what makes the right response derivable: the gauge reading IS
    stored, nothing needs re-sending, and the next throttled tick is the retry.

    Raising at all (rather than warning) is the ruled behaviour: a history that
    degrades silently reproduces GAU-15 one layer down, and this module's whole
    reason to exist is that a silent gap in a series is indistinguishable from
    a healthy series until someone needs it.
    """

    error_token = "cache_written_history_append_failed"

    def __init__(self, agent_instance_id: str, detail: str) -> None:
        self.agent_instance_id = agent_instance_id
        self.detail = detail
        super().__init__(
            f"{self.error_token}: the session_context_status CACHE row for "
            f"{agent_instance_id!r} was written and its history append was NOT "
            f"({detail}). The reading is stored; do NOT re-send the cache "
            "write. The next reporting tick appends the following row.",
        )


class StaleContextReadingError(Exception):
    """A reporter attempted to replay or regress one session's reading."""


def _incoming_reading_timestamp(raw: object, *, field: str) -> datetime:
    """Parse a reporter-supplied clock without guessing its timezone."""
    if not isinstance(raw, str) or not raw.strip():
        raise StaleContextReadingError(f"{field} must be a non-empty timestamp")
    try:
        value = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as exc:
        raise StaleContextReadingError(f"{field} is invalid: {raw!r}") from exc
    if value.tzinfo is None:
        raise StaleContextReadingError(f"{field} must be timezone-aware")
    return value


def _stored_reading_timestamp(raw: object, *, field: str) -> datetime:
    """Parse a persisted UTC clock, restoring the offset stripped by DATETIME.

    ``reading_at`` enters this module as an explicit reporter timestamp, but
    the state schema stores it in a naive ``DATETIME`` cell. The two directions
    deliberately have different contracts: accepting a naive incoming clock
    would guess what a reporter meant, while accepting a naive value returned
    by this column restores the schema's UTC representation before comparison.
    """
    if isinstance(raw, datetime):
        value = raw
    elif isinstance(raw, str) and raw.strip():
        try:
            value = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError as exc:
            raise StaleContextReadingError(f"{field} is invalid: {raw!r}") from exc
    else:
        raise StaleContextReadingError(f"{field} must be a non-empty timestamp")
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value


def _reading_is_new(
    state: StateManagementInterface,
    *,
    agent_instance_id: str,
    claude_session_id: str,
    reading_at: str | None,
) -> bool:
    incoming = None if reading_at is None else _incoming_reading_timestamp(
        reading_at,
        field="incoming reading_at",
    )
    if incoming is None:
        return True
    existing = read_session_context_status(state, agent_instance_id)
    if existing is None or str(existing.get("claude_session_id") or "") != claude_session_id:
        return True
    prior_raw = existing.get("reading_at")
    if prior_raw is None:
        return True
    prior = _stored_reading_timestamp(prior_raw, field="stored reading_at")
    if incoming == prior:
        return False
    if incoming < prior:
        raise StaleContextReadingError(
            f"reading_at regressed for {agent_instance_id!r}/{claude_session_id!r}: "
            f"{incoming.isoformat()} < {prior.isoformat()}",
        )
    return True


def _history_record(
    record: dict[str, Any], *, recorded_at: str,
) -> dict[str, Any]:
    """The history row for an accepted cache write.

    Built by COPYING the very dict handed to the cache upsert, rather than by
    re-reading the arguments: a second construction path is a second thing to
    keep in step, and the series would silently stop matching the cache the
    first time one of them gained a column.
    """
    return {**record, _COL_RECORDED_AT: recorded_at}


def _prune_history(
    state: StateManagementInterface, *, agent_instance_id: str,
) -> None:
    """Hard-delete this instance's history rows beyond the newest
    :data:`GAUGE_HISTORY_RETENTION`.

    THE WRITER BOUNDS ITS OWN TABLE. No reaper, no schedule, nothing to arm and
    forget — an unbounded append-only table with a cleanup job that "will be
    added" is how a bounded design becomes an unbounded one in production.

    HARD delete (``soft_delete=False``) is deliberate: a soft delete would keep
    every row forever behind an ``is_deleted`` flag, and this platform has no
    reaper that ever clears one, so the bound would be cosmetic while the table
    grew exactly as fast as before.

    The cutoff is read, not computed: the ``recorded_at`` of the OLDEST row we
    intend to keep, taken from a bounded ordered page. Rows strictly older than
    that go. Ties on ``recorded_at`` are kept rather than deleted — deleting a
    tie could drop a row we just wrote, and keeping one costs a row.
    """
    rows = require_records(
        state.query_ordered(
            AGENT_ROLE_BINDING_NAMESPACE,
            {
                "table": TABLE_SESSION_CONTEXT_STATUS_HISTORY,
                "filters": {_COL_AGENT_INSTANCE_ID: agent_instance_id},
                "order_by": [[_COL_RECORDED_AT, "desc"], ["id", "desc"]],
                "limit": GAUGE_HISTORY_RETENTION,
            },
        ),
    )
    if len(rows) < GAUGE_HISTORY_RETENTION:
        return
    cutoff = rows[-1].get(_COL_RECORDED_AT)
    if not cutoff:
        return
    require_completed(
        state.delete_records(
            AGENT_ROLE_BINDING_NAMESPACE,
            {
                "table": TABLE_SESSION_CONTEXT_STATUS_HISTORY,
                "filters": {
                    _COL_AGENT_INSTANCE_ID: agent_instance_id,
                    _COL_RECORDED_AT: {"op": "lt", "value": cutoff},
                },
                "soft_delete": False,
            },
        ),
        "prune session_context_status_history",
    )


def _append_history(
    state: StateManagementInterface,
    *,
    agent_instance_id: str,
    record: dict[str, Any],
) -> None:
    """Append one history row for an ALREADY-COMMITTED cache write, then prune.

    Failure of either step raises :class:`GaugeHistoryAppendError`, which names
    the split state rather than pretending the cache write failed.
    """
    row = _history_record(record, recorded_at=datetime.now(UTC).isoformat())
    try:
        require_completed(
            state.write_state(
                AGENT_ROLE_BINDING_NAMESPACE,
                {"table": TABLE_SESSION_CONTEXT_STATUS_HISTORY, "record": row},
            ),
            "append session_context_status_history",
        )
        _prune_history(state, agent_instance_id=agent_instance_id)
    except GaugeHistoryAppendError:
        raise
    except Exception as exc:
        raise GaugeHistoryAppendError(agent_instance_id, str(exc)) from exc


def upsert_session_context_status(
    state: StateManagementInterface,
    *,
    agent_instance_id: str,
    claude_session_id: str,
    model: str,
    current_tokens: int,
    ceiling: int,
    measured_at: str,
    reading_at: str | None = None,
    cache_read_tokens: int | None = None,
    cache_cold: bool | None = None,
    cache_overage_signature: bool | None = None,
    reporter_surface: str | None = None,
    reporter_generation: int | None = None,
    agent_session_id: str | None = None,
    provider: str | None = None,
    runtime: str | None = None,
    effort: str | None = None,
    cache_write_tokens: int | None = None,
) -> bool:
    """Overwrite the single latest snapshot for `agent_instance_id`. Conflicts
    on `agent_instance_id` alone (unlike `session_claude_mapping`'s
    per-firing history triple) — this table is a cache, not a log.

    The three cache fields default to ``None`` = NOT REPORTED, which is a
    third state and never a synonym for "warm". A reporter predating the
    2026-08-16 cache-state widening sends none of them, and a reader must be
    able to tell that apart from a reporter that looked and found the cache
    live — otherwise every un-upgraded hook silently asserts a warm cache.

    ``reporter_surface``/``reporter_generation`` say WHICH COPY of the
    reporting hook produced this row, because more than one copy can be
    registered on the same event at once. Latest write wins here — that is
    what "a cache, not a log" means — so without them a row cannot be
    attributed to a reporter at all, and a field missing because a STALE
    copy served the tick is indistinguishable from one missing because the
    verbs are undeployed. Both default to ``None`` = pre-attribution
    reporter, which is itself a positive finding about that row.

    ★ TWO WRITES, DELIBERATELY NOT ONE TRANSACTION (GAU-15, 2026-08-19). The
    cache upsert commits first and the history append follows as a SEPARATE
    top-level state call — they do not share a ``transactional()`` context, and
    that is a design choice rather than an oversight. The cache is on the
    rotation-decision path; wrapping it in a transaction with bookkeeping would
    let a history failure roll back an operational reading that was perfectly
    good. The cost of the choice is a real split state, which is why the
    failure has its own named error (:class:`GaugeHistoryAppendError`) instead
    of a generic raise: a caller must be able to tell "your reading is stored,
    the series is not" from "nothing was stored".

    ``reading_at`` is the SECOND CLOCK (GAU-14 D3, 2026-08-19). ``measured_at``
    is when the reporter LOOKED; this is when the reading it carries was
    PRODUCED -- the transcript line's own timestamp. The two differ by the age
    of that line at observation time, measured at ~34s on the sweep path and
    ~4 minutes on the prompt-surfaced hook path, and a notice reporting only
    the first reads as though the second were the same number. ``None`` is NOT
    REPORTED and must never be defaulted to ``measured_at``: that would
    fabricate a zero lag precisely where the lag is unknown -- the same
    tri-state discipline ``_as_flag`` enforces above, for the same reason.
    Within one runtime session it is also the consumption identity: an equal
    timestamp is an idempotent no-op and an older timestamp fails loud. A new
    runtime session resets that monotone series, which preserves `/clear`.

    ``agent_session_id`` is the ROUTING JOIN (2026-08-18). This table keys on
    the reporting session's LEDGER id, while a watcher-held worker's live
    binding keys on its WATCH id, so a consumer holding a row here could not
    find that session's bridge at all. Storing the stable session id lets
    ``peer_registry.resolve_by_agent_session_id`` reverse-resolve the binding
    whichever id that binding is keyed on. ``None`` = NOT REPORTED (a reporter
    predating the column), and a caller must not read it as `this session has
    no bridge` -- the same tri-state discipline ``_as_flag`` enforces above,
    for the same reason: those are different facts with different fixes.
    """
    if not _reading_is_new(
        state,
        agent_instance_id=agent_instance_id,
        claude_session_id=claude_session_id,
        reading_at=reading_at,
    ):
        return False
    record: dict[str, Any] = {
        _COL_AGENT_INSTANCE_ID: agent_instance_id,
        "claude_session_id": claude_session_id,
        "model": model,
        "current_tokens": current_tokens,
        "ceiling": ceiling,
        "measured_at": measured_at,
        "reading_at": reading_at,
        "cache_read_tokens": cache_read_tokens,
        "cache_cold": _as_flag(cache_cold),
        "cache_overage_signature": _as_flag(cache_overage_signature),
        "reporter_surface": reporter_surface,
        "reporter_generation": reporter_generation,
        "agent_session_id": agent_session_id,
        "provider": provider,
        "runtime": runtime,
        "effort": effort,
        "cache_write_tokens": cache_write_tokens,
    }
    require_completed(
        state.upsert_state(
            AGENT_ROLE_BINDING_NAMESPACE,
            {
                "table": TABLE_SESSION_CONTEXT_STATUS,
                "record": record,
                "conflict_columns": _CONFLICT_COLUMNS,
            },
        ),
        "upsert session_context_status",
    )
    _append_history(state, agent_instance_id=agent_instance_id, record=record)
    return True


def read_session_context_status(
    state: StateManagementInterface, agent_instance_id: str,
) -> dict[str, Any] | None:
    """The latest snapshot row for `agent_instance_id`, or `None` when no
    report has ever landed for it (a fresh session pre-first-tick, or a
    `host=operator` seat this pass has not been wired to report yet — see
    the M0 design doc's seat-wiring open item). Never raises on absence;
    callers translate `None` into their own fail-loud contract."""
    result = state.query_state(
        AGENT_ROLE_BINDING_NAMESPACE,
        {
            "table": TABLE_SESSION_CONTEXT_STATUS,
            "filters": {_COL_AGENT_INSTANCE_ID: agent_instance_id, _COL_IS_DELETED: 0},
        },
    )
    records = require_records(result)
    return records[0] if records else None


class AmbiguousAgentSessionIdError(Exception):
    """More than one gauge row claims the same ``agent_session_id``.

    FAIL-LOUD BY NECESSITY, not by taste. The gauge table's ONLY unique index
    is on ``agent_instance_id``; ``agent_session_id`` is nullable, unindexed
    and unconstrained, so single-valuedness is something the schema does not
    provide and the code must therefore not assume. Picking a row would answer
    a routing question with a coin flip -- and answer it CONFIDENTLY, which is
    the failure mode the GAU-07 fix exists to remove rather than relocate.
    Mirrors ``peer_registry.PeerSessionAmbiguousError``'s posture on the very
    same key.
    """

    def __init__(self, agent_session_id: str, agent_instance_ids: list[str]) -> None:
        self.agent_session_id = agent_session_id
        self.agent_instance_ids = agent_instance_ids
        super().__init__(
            f"{len(agent_instance_ids)} session_context_status rows share "
            f"agent_session_id {agent_session_id!r} ({', '.join(agent_instance_ids)}) — "
            "refusing to guess which one a caller meant.",
        )


def read_agent_session_id_for_binding(
    state: StateManagementInterface, agent_instance_id: str,
) -> str:
    """The stable ``agent_session_id`` bound to ``agent_instance_id`` in
    ``peer_binding``, or ``""`` when that instance has no binding.

    HOP 1 OF THE GAU-07 JOIN, and the reason no id is ever derived from
    another: a watcher registers under ``agi-watch-<digest>``, a ONE-WAY
    sha256 of the session id, so the reverse direction exists only as STORED
    data. ``agent_instance_id`` is UNIQUE on this table, so the hop is
    unambiguous by schema constraint rather than by convention.
    """
    if not agent_instance_id.strip():
        return ""
    result = state.query_state(
        PEER_BINDING_NAMESPACE,
        {
            "table": PEER_BINDING_TABLE,
            "filters": {_COL_AGENT_INSTANCE_ID: agent_instance_id, _COL_IS_DELETED: 0},
        },
    )
    records = require_records(result)
    if not records:
        return ""
    return str(records[0].get(_COL_AGENT_SESSION_ID) or "")


def read_session_context_status_by_agent_session_id(
    state: StateManagementInterface, agent_session_id: str,
) -> dict[str, Any] | None:
    """The gauge row whose stored ``agent_session_id`` matches, or ``None``.

    HOP 2 OF THE GAU-07 JOIN. Two guards, both load-bearing:

    * AN EMPTY KEY SHORT-CIRCUITS BEFORE ANY QUERY RUNS. ``agent_session_id``
      is nullable -- every reporter of generation <= 2 wrote NULL -- so a
      blank key reaching the filter would match the whole population of
      pre-attribution rows and return a confident WRONG row, which is worse
      than the missing-row defect this join was added to fix. (It would also
      violate this repo's rule against bare ``None`` filter values, which
      require an explicit ``{"op": "is_null"}``.)
    * MORE THAN ONE MATCH RAISES rather than returning ``records[0]`` -- see
      :class:`AmbiguousAgentSessionIdError`.
    """
    if not agent_session_id.strip():
        return None
    result = state.query_state(
        AGENT_ROLE_BINDING_NAMESPACE,
        {
            "table": TABLE_SESSION_CONTEXT_STATUS,
            "filters": {_COL_AGENT_SESSION_ID: agent_session_id, _COL_IS_DELETED: 0},
        },
    )
    records = require_records(result)
    if not records:
        return None
    if len(records) > 1:
        raise AmbiguousAgentSessionIdError(
            agent_session_id,
            [str(row.get(_COL_AGENT_INSTANCE_ID) or "") for row in records],
        )
    return dict(records[0])


def list_session_context_statuses(
    state: StateManagementInterface,
    *,
    measured_since: datetime,
    active_agent_instance_ids: set[str],
) -> list[dict[str, Any]]:
    """Return self-notice candidates, not the historical gauge table.

    THE SCAN THE L4c SELF-NOTICE LEG NEEDS, and the reason it can reach a seat
    at all. Its two sibling legs enumerate `managed_session` and then look the
    gauge up per row, which structurally excludes every `host=operator` session
    -- a ledger a seat is never in. This table has no FK to that ledger (see
    the module docstring), so scanning it directly is the only enumeration that
    includes the sessions whose rotation decision is the expensive one.

    The candidate union is deliberately narrower than every historical gauge:
    fresh rows remain eligible for notice, while stale rows remain eligible only
    when their lifecycle row or current peer binding says the instance is live.
    The caller retains the in-Python age and lifecycle recheck that decides
    whether a candidate is actually notable.
    """
    rows: list[dict[str, Any]] = []
    seen_ids: set[str] = set()

    def add(rows_to_add: Iterable[dict[str, object]]) -> None:
        for row in rows_to_add:
            row_id = str(row["id"])
            if row_id not in seen_ids:
                seen_ids.add(row_id)
                rows.append(dict(row))

    add(iter_table_rows(
        state,
        namespace=AGENT_ROLE_BINDING_NAMESPACE,
        table=TABLE_SESSION_CONTEXT_STATUS,
        filters={_COL_MEASURED_AT: {"op": "gte", "value": measured_since}},
        ceiling=_CONTEXT_STATUS_CEILING,
        reason=_CONTEXT_STATUS_CEILING_REASON,
    ))
    if active_agent_instance_ids:
        add(iter_table_rows(
            state,
            namespace=AGENT_ROLE_BINDING_NAMESPACE,
            table=TABLE_SESSION_CONTEXT_STATUS,
            filters={_COL_AGENT_INSTANCE_ID: sorted(active_agent_instance_ids)},
            ceiling=_CONTEXT_STATUS_CEILING,
            reason=_CONTEXT_STATUS_CEILING_REASON,
        ))
    return rows


def read_session_context_status_history(
    state: StateManagementInterface,
    agent_instance_id: str,
    *,
    limit: int = GAUGE_HISTORY_RETENTION,
) -> tuple[list[dict[str, Any]], bool]:
    """The newest ``limit`` history rows for ``agent_instance_id``, newest
    first, plus whether the page was TRUNCATED.

    The bool is the whole point of the return shape. A caller reading a series
    to decide "did this gauge stop" is asking a question about the OLDEST row
    it can see, and a silently truncated page answers that question with a
    boundary the caller chose by accident. Returning truncation explicitly
    means a reader can tell "the series starts here" from "the page ends here".

    Reads are bounded by construction: the page is capped, ordered on the
    tie-safe ``(recorded_at, id)`` composite, and never unbounded.
    """
    if not agent_instance_id.strip():
        return ([], False)
    capped = max(1, min(limit, GAUGE_HISTORY_RETENTION))
    rows = require_records(
        state.query_ordered(
            AGENT_ROLE_BINDING_NAMESPACE,
            {
                "table": TABLE_SESSION_CONTEXT_STATUS_HISTORY,
                "filters": {_COL_AGENT_INSTANCE_ID: agent_instance_id},
                "order_by": [[_COL_RECORDED_AT, "desc"], ["id", "desc"]],
                "limit": capped + 1,
            },
        ),
    )
    return (rows[:capped], len(rows) > capped)


__all__ = [
    "GAUGE_HISTORY_RETENTION",
    "AmbiguousAgentSessionIdError",
    "GaugeHistoryAppendError",
    "StaleContextReadingError",
    "list_session_context_statuses",
    "read_agent_session_id_for_binding",
    "read_session_context_status_by_agent_session_id",
    "read_session_context_status",
    "read_session_context_status_history",
    "upsert_session_context_status",
]
