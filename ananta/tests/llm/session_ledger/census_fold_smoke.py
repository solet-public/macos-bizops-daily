#!/usr/bin/env python3
"""Offline fold smoke for the SQL-lockdown census migration (GAP-1, no pytest).

The census ``census_source_rows`` retires the multi-CTE
``bit_xor(hashtextextended(...))`` aggregate onto a Python fold over the typed
``query_state`` / ``query_ordered`` primitives (operator D1, 2026-06-20 —
Python-fold + re-baseline, NO aggregate primitive). The fold logic lives in
``read_support`` as a DB-free :class:`_CensusAggregator` plus the
keyset-paging :func:`fold_census_events` / :func:`build_census`, which take the
read seam as an INJECTED callback — so the whole composition is unit-testable
here with a fake reader, no live DB.

This smoke proves the parts the live-schema smoke cannot make cheap or
deterministic:

* per-source session counts + canonical/sibling split;
* event ``event_count`` + the two-seed fingerprint, attributed to the event's
  *session's* source, and DROPPING events/tool_calls whose session is not live
  (the retired ``JOIN __session … AND s.is_deleted = 0``);
* fingerprint DETERMINISM (build twice → byte-identical) — the whole reason for
  ``blake2b`` over the salted builtin ``hash()``;
* fingerprint ORDER-INDEPENDENCE (the XOR) — pages folded in any order, and at
  any page size, yield the identical result, so the ``id``-keyset paging is
  faithful to the retired set-wise aggregate;
* a source with NO live events carries ``None`` fingerprints (the old LEFT JOIN
  NULL), distinct from a present-but-zero XOR;
* import-batch health (owned-running vs unclaimed-route) + oldest-running age;
* output rows ordered ``(source_kind, source_id)`` with exactly the keys the
  ``service.census`` consumer reads.

Run:
    .venv/bin/python3 ananta/tests/llm/session_ledger/census_fold_smoke.py
"""

from __future__ import annotations

import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast

REPO_ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(REPO_ROOT / "ananta" / "src"))

from ananta.llm.session_ledger.read_support import (  # noqa: E402
    _CensusAggregator,
    build_census,
)
from ananta.llm.session_ledger.repository import (  # noqa: E402
    SessionLedgerRepository,
)
from ananta.llm.session_ledger.schema import (  # noqa: E402
    TABLE_EVENT,
    TABLE_IMPORT_BATCH,
    TABLE_SESSION,
    TABLE_SOURCE,
    TABLE_TOOL_CALL,
)
from ananta.services.state_service.read_bounds import MAX_READ_ROWS  # noqa: E402

_passed = 0
_failed: list[str] = []


def _check(condition: object, label: str) -> None:
    global _passed
    if condition:
        _passed += 1
        print(f"  PASS  {label}")
    else:
        _failed.append(label)
        print(f"  FAIL  {label}")


_NOW = datetime(2026, 6, 15, 12, 0, 0)  # naive UTC — what the mixin passes
_SEEDS = (0, 527612190)


def _iso(seconds_before: int) -> str:
    # started_at reads back as a naive-UTC ISO string (the live read path).
    return (_NOW - timedelta(seconds=seconds_before)).isoformat()


# --- Fixtures -------------------------------------------------------------
# Two live sources + a third with sessions but zero events. Sessions s1/s2 ->
# src-A (one canonical, one sibling), s3 -> src-B (canonical), s4 -> src-C
# (canonical, no events). Events/tool_calls for the non-live session "s-del"
# must be DROPPED (its row is absent from the live `sessions` read).
#: Sessions on src-E. Above MAX_READ_ROWS so the per-source tool_call count must
#: chunk; the fake refuses an over-cap `= ANY` exactly as the provider does, so an
#: unchunked count fails rather than passing quietly.
_BULK_SESSIONS = 150

_SOURCES: list[dict[str, object]] = [
    {"id": "src-A", "source_kind": "claude_code", "root_uri": "/a"},
    {"id": "src-B", "source_kind": "codex", "root_uri": "/b"},
    {"id": "src-C", "source_kind": "zzz_other", "root_uri": "/c"},
    # src-D has RUNNING BATCHES AND ZERO SESSIONS. It exists to pin a regression
    # that a session-derived source list reintroduces: the row-walking code keyed
    # batches off import_batch.source_id directly, so a source with batches but
    # no sessions still got a tally. Deriving the source list from the sessions
    # index silently drops it — and without src-D that mutation stays GREEN.
    {"id": "src-D", "source_kind": "batches_only", "root_uri": "/d"},
    # src-E holds MORE THAN MAX_READ_ROWS sessions so the tool_call count must
    # CHUNK. Below the cap, an unchunked `= ANY` passes identically and the
    # chunking is untested.
    {"id": "src-E", "source_kind": "bulk", "root_uri": "/e"},
]
# NOTE `created_at`: the paged session walk cursors on ``(created_at, id)``, so
# every session row needs it. The pre-2026-08-16 fixture held FOUR sessions and
# therefore never crossed a page boundary — the cursor was never used and the
# missing column never noticed. Growing the fixture past the page size surfaced
# it immediately, which is its own small argument for fixtures that page.
_SESSIONS: list[dict[str, object]] = [
    {"id": "s1", "source_id": "src-A", "canonical_external_session_id": None,
     "created_at": "2026-06-15T00:00:00.000001"},
    {"id": "s2", "source_id": "src-A", "canonical_external_session_id": "ext-x",
     "created_at": "2026-06-15T00:00:00.000002"},
    {"id": "s3", "source_id": "src-B", "canonical_external_session_id": None,
     "created_at": "2026-06-15T00:00:00.000003"},
    {"id": "s4", "source_id": "src-C", "canonical_external_session_id": None,
     "created_at": "2026-06-15T00:00:00.000004"},
    *(
        {
            "id": f"sE{i:04d}",
            "source_id": "src-E",
            "canonical_external_session_id": None,
            "created_at": f"2026-06-15T00:00:01.{i:06d}",
        }
        for i in range(_BULK_SESSIONS)
    ),
]
_EVENTS: list[dict[str, object]] = [
    {"id": "e1", "session_id": "s1", "content_blob_id": None},
    {"id": "e2", "session_id": "s1", "content_blob_id": "b2"},
    {"id": "e3", "session_id": "s2", "content_blob_id": None},
    {"id": "e4", "session_id": "s3", "content_blob_id": "b4"},
    {"id": "e5", "session_id": "s-del", "content_blob_id": "b5"},  # dropped
]
_TOOL_CALLS: list[dict[str, object]] = [
    {"id": "t1", "session_id": "s1"},
    {"id": "t2", "session_id": "s3"},
    {"id": "t3", "session_id": "s-del"},  # dropped
    *({"id": f"tE{i:04d}", "session_id": f"sE{i:04d}"} for i in range(_BULK_SESSIONS)),
]
_IMPORT_BATCHES: list[dict[str, object]] = [
    {"id": "ib1", "source_id": "src-A", "status": "running",
     "polling_lease_token": "tok", "started_at": _iso(100)},
    {"id": "ib2", "source_id": "src-A", "status": "running",
     "polling_lease_token": None, "started_at": _iso(50)},
    {"id": "ib3", "source_id": "src-B", "status": "completed",
     "polling_lease_token": None, "started_at": _iso(999)},
    {"id": "ib5", "source_id": "src-D", "status": "running",
     "polling_lease_token": "tokD", "started_at": _iso(300)},
    {"id": "ib4", "source_id": "src-A", "status": "running",
     "polling_lease_token": "tok2", "started_at": _iso(200)},  # oldest
]


def _op_matches(cell: object, spec: dict[str, object]) -> bool:
    """One structured filter spec. ``gt`` is accepted and deferred: the event-fold
    keyset passes ``{id: {op: gt}}`` and the caller applies it itself."""
    op = spec.get("op")
    if op == "is_null":
        return cell is None
    if op == "is_not_null":
        return cell is not None
    if op == "gt":
        return True
    raise AssertionError(f"fake does not implement op {op!r}")


def _spec_matches(cell: object, spec: object) -> bool:
    if isinstance(spec, dict):
        return _op_matches(cell, spec)
    if isinstance(spec, (list, tuple)):
        return cell in spec
    return cell == spec


def _row_matches(row: dict[str, Any], filters: dict[str, object]) -> bool:
    """One row against the census's filter grammar; fails loud on an unknown op."""
    return all(
        _spec_matches(row.get(key, 0 if key == "is_deleted" else None), spec)
        for key, spec in filters.items()
    )


def _sort_key(row: dict[str, Any], cols: list[str]) -> tuple[str, ...]:
    return tuple(str(row.get(col, "")) for col in cols)


def _ordered_rows(
    rows: list[dict[str, Any]],
    filters: dict[str, object],
    cols: list[str],
    *,
    include_deleted: bool,
) -> list[dict[str, Any]]:
    """Filtered + ordered rows, with the event-keyset ``{id: {op: gt}}`` applied.

    That op is deferred out of ``_row_matches`` and applied here because it is a
    CURSOR, not a predicate on the row's own value — treating it as an ordinary
    filter would make every row match.
    """
    matched = sorted(
        (
            r for r in rows
            if (include_deleted or int(cast("int", r.get("is_deleted", 0))) == 0)
            and _row_matches(r, filters)
        ),
        key=lambda r: _sort_key(r, cols),
    )
    op = filters.get("id")
    if isinstance(op, dict) and op.get("op") == "gt":
        last = str(op["value"])
        matched = [r for r in matched if str(r["id"]) > last]
    return matched


class _FakeReader:
    """In-memory stand-in for the mixin's bound ``_query`` / ``_query_ordered``.

    ``query`` returns the planted list for a table verbatim (the bounded reads);
    ``query_ordered`` FAITHFULLY implements the ``id``-keyset cursor — sort by
    ``id`` ASC, apply the Gap-A ``id > last_id`` lower bound, slice to ``limit``
    — so the paging loop in ``fold_census_events`` is exercised for real (a
    skip/dup would corrupt the counts).
    """

    def __init__(self, tables: dict[str, list[dict[str, Any]]]) -> None:
        self._tables = tables

    def query(
        self, table: str, filters: dict[str, object],
    ) -> list[dict[str, object]]:
        """Honour the FULL filter grammar the census uses, not just is_deleted.

        It used to honour ``is_deleted`` alone and pass everything else through,
        which was harmless while every read was a whole-table walk. It stopped
        being harmless the moment the census started COUNTING with predicates
        (2026-08-16): a fake that ignores ``{"session_id": [...]}`` returns the
        whole table for every count, so per-source tallies come back identical
        and wrong — and every count-related assertion would have been meaningless
        while passing. Equality, ``= ANY`` and the is_null/is_not_null ops now
        all apply.
        """
        for spec in filters.values():
            if isinstance(spec, (list, tuple)) and len(spec) > MAX_READ_ROWS:
                raise AssertionError(
                    f"query.unbounded_read_over_cap: `= ANY` filter carried "
                    f"{len(spec)} values against the {MAX_READ_ROWS}-row cap. "
                    f"A membership read is bounded by the CALLER's list, so it "
                    f"must be chunked — without this refusal an unchunked count "
                    f"passes quietly and the chunking is untested."
                )
        return [
            dict(row)
            for row in self._tables.get(table, [])
            if _row_matches(row, filters)
        ]

    def query_ordered(
        self,
        table: str,
        *,
        filters: dict[str, object],
        order_by: list[list[str]],
        limit: int,
        after: tuple[object, ...] | None = None,
        include_deleted: bool = False,
    ) -> list[dict[str, object]]:
        """Ordered page, honouring the ROW-VALUE ``after`` cursor.

        ``after`` support was added 2026-08-16: without it a paged walk re-reads
        page one forever and only stops when the ceiling fires. That went
        unnoticed while the fixture held four sessions — nothing ever paged, so
        the cursor was never used. Growing the fixture past the page size turned
        a silently-unexercised code path into an immediate infinite loop, which
        is the better failure of the two.
        """
        cols = [pair[0] for pair in order_by]
        rows = _ordered_rows(
            self._tables.get(table, []), filters, cols, include_deleted=include_deleted,
        )
        if after is not None:
            cursor = tuple(str(v) for v in after)
            rows = [r for r in rows if _sort_key(r, cols) > cursor]
        return [dict(row) for row in rows[:limit]]


class _StateServiceShim:
    """Filter-honoring StateManagementInterface stand-in over ``_FakeReader``.

    Lets the REAL :meth:`SessionLedgerRepository.census_source_rows` run offline
    — exercising the public method + the base ``_query`` / ``_query_ordered``
    envelope extraction + the delegator's ``_naive_utc(self._clock())`` clock
    normalization + the batch-age subtraction TOGETHER (the other tests call
    ``build_census`` with an already-naive ``now``, so they bypass exactly the
    tz-aware-clock-vs-naive-``started_at`` path). census never opens a
    transaction, so ``transactional`` is intentionally absent.
    """

    def __init__(self, reader: _FakeReader) -> None:
        self._reader = reader

    def query_state(self, namespace: str, query: dict[str, Any]) -> dict[str, Any]:
        filters = query.get("filters") or {}
        rows = self._reader.query(
            str(query["table"]),
            cast("dict[str, object]", filters) if isinstance(filters, dict) else {},
        )
        return {"action_status": "completed", "data": {"records": rows},
                "actions": [], "error": None}

    def count(self, namespace: str, data: dict[str, Any]) -> dict[str, Any]:
        """Scalar COUNT(*) — what the census now uses instead of walking
        __tool_call and __import_batch (2026-08-16 count-not-walk).

        Present on the shim so ``test_real_method_clock_normalization`` still
        drives the REAL ``census_source_rows`` through its real bindings; a shim
        missing it would force that test onto a hand-wired path and stop
        exercising ``read._census_count`` / ``_census_min_value`` at all.
        """
        rows = self._reader.query(
            str(data["table"]), cast("dict[str, object]", data.get("filters") or {}),
        )
        return {"action_status": "completed", "data": {"result": {"value": len(rows)}},
                "actions": [], "error": None}

    def min_value(self, namespace: str, data: dict[str, Any]) -> dict[str, Any]:
        column = str(data["column"])
        rows = self._reader.query(
            str(data["table"]), cast("dict[str, object]", data.get("filters") or {}),
        )
        values = [r[column] for r in rows if r.get(column) is not None]
        return {"action_status": "completed",
                "data": {"result": {"value": min(values) if values else None}},
                "actions": [], "error": None}

    def query_ordered(self, namespace: str, data: dict[str, Any]) -> dict[str, Any]:
        after_raw = data.get("after")
        rows = self._reader.query_ordered(
            str(data["table"]),
            filters=cast("dict[str, object]", data["filters"]),
            order_by=cast("list[list[str]]", data["order_by"]),
            limit=int(cast("int", data["limit"])),
            after=tuple(cast("list[Any]", after_raw)) if after_raw is not None else None,
            include_deleted=bool(data.get("include_deleted")),
        )
        return {"action_status": "completed", "data": {"records": rows},
                "actions": [], "error": None}


def _reader() -> _FakeReader:
    return _FakeReader(
        {
            TABLE_SOURCE: _SOURCES,
            TABLE_SESSION: _SESSIONS,
            TABLE_EVENT: _EVENTS,
            TABLE_TOOL_CALL: _TOOL_CALLS,
            TABLE_IMPORT_BATCH: _IMPORT_BATCHES,
        }
    )


def _walker(reader: _FakeReader) -> Any:
    """A ``walk`` seam over the fake that yields rows ONE AT A TIME.

    A GENERATOR on purpose. ``build_census`` streams its three large tables now
    (read-cap sweep, 2026-08-16), and a fake that handed back a list would let a
    double-consumption bug pass unnoticed — which is exactly the defect this
    change had to fix: ``_CensusAggregator`` used to walk ``sessions`` twice, once
    to build the id->source index and once to tally. With lists that is merely
    wasteful; with generators the second pass sees an exhausted iterator and every
    session tally silently comes back EMPTY.

    ``counts`` records how many times each table was walked, so
    ``test_census_streams_each_table_exactly_once`` can assert the property
    directly rather than inferring it from the tallies being right.
    """

    class _Walk:
        def __init__(self) -> None:
            self.counts: dict[str, int] = {}
            self.drained = False

        def __call__(
            self, table: str, filters: dict[str, Any], *, ceiling: int, reason: str,
        ) -> Any:
            self.counts[table] = self.counts.get(table, 0) + 1
            yield from (dict(row) for row in reader.query(table, {**filters, "is_deleted": 0}))
            self.drained = True

    return _Walk()


def _aggregates(reader: _FakeReader, walker: Any) -> tuple[Any, Any]:
    """``count`` / ``min_value`` seams over the fake — and an ORDERING TRIPWIRE.

    Read-cap sweep, 2026-08-16 (lane-ak). ``build_census`` no longer READS
    ``__tool_call`` (637,496 rows) or ``__import_batch`` (284,759); it counts
    them. These seams stand in for the scalar aggregates.

    The counter ALSO records whether any count happened before the session walk
    had been drained. Counting a tool_call before the ``session_id -> source_id``
    index exists silently attributes nothing and every per-source count comes
    back zero — the same silent-empty-tallies failure the double-consumption bug
    produced. ``build_census`` now makes that structurally impossible (the
    counting methods belong to the object that owns the index), and
    ``test_counts_cannot_precede_the_session_index`` asserts it rather than
    trusting the structure to stay that way.
    """

    class _Aggregates:
        def __init__(self) -> None:
            self.count_calls: list[str] = []
            self.min_calls: list[str] = []
            self.counted_before_walk_drained = False

        def count(self, table: str, filters: dict[str, Any]) -> int:
            if not walker.drained:
                self.counted_before_walk_drained = True
            self.count_calls.append(table)
            return len(reader.query(table, {**filters}))

        def min_value(self, table: str, column: str, filters: dict[str, Any]) -> Any:
            self.min_calls.append(table)
            values = [
                row[column]
                for row in reader.query(table, {**filters})
                if row.get(column) is not None
            ]
            return min(values) if values else None

    agg = _Aggregates()
    return agg.count, agg.min_value


def _census_with(reader: _FakeReader, *, page_size: int = 100) -> list[dict[str, object]]:
    """One census over an explicit reader, with all four seams wired."""
    walker = _walker(reader)
    count, min_value = _aggregates(reader, walker)
    return build_census(
        query=reader.query,
        query_ordered=reader.query_ordered,
        walk=walker,
        count=count,
        min_value=min_value,
        now=_NOW,
        page_size=page_size,
        fingerprint_seeds=_SEEDS,
    )


def _census(*, page_size: int) -> list[dict[str, object]]:
    reader = _reader()
    walker = _walker(reader)
    count, min_value = _aggregates(reader, walker)
    return build_census(
        query=reader.query,
        query_ordered=reader.query_ordered,
        walk=walker,
        count=count,
        min_value=min_value,
        now=_NOW,
        page_size=page_size,
        fingerprint_seeds=_SEEDS,
    )


def _by_source(rows: list[dict[str, object]]) -> dict[str, dict[str, object]]:
    return {str(r["source_id"]): r for r in rows}


def test_per_source_tallies() -> None:
    rows = _census(page_size=100)
    by = _by_source(rows)

    # Five sources since 2026-08-16: src-D (batches, no sessions) and src-E
    # (150 sessions) were added to make two mutations detectable — see
    # _SOURCES. src-D in particular proves a source with NO sessions still
    # gets a census row, which a session-derived source list would drop.
    _check(len(rows) == 5, f"one census row per live source (got {len(rows)})")
    _check(
        [str(r["source_id"]) for r in rows]
        == ["src-D", "src-E", "src-A", "src-B", "src-C"],
        "rows ordered (source_kind, source_id): batches_only, bulk, claude_code, "
        "codex, zzz_other",
    )

    a = by["src-A"]
    _check(
        a["session_count"] == 2
        and a["canonical_count"] == 1
        and a["sibling_count"] == 1,
        "src-A session counts: 2 total, 1 canonical, 1 sibling",
    )
    _check(a["event_count"] == 3, "src-A event_count=3 (e1,e2,e3; e5/s-del dropped)")
    _check(a["tool_call_count"] == 1, "src-A tool_call_count=1 (t1; t3/s-del dropped)")
    _check(
        a["owned_running_batches"] == 2 and a["unclaimed_route_batches"] == 1,
        "src-A batch health: 2 owned-running, 1 unclaimed-route (completed ignored)",
    )
    _check(
        a["oldest_running_batch_age_seconds"] == 200,
        "src-A oldest-running age = now - min(started_at over running) = 200s",
    )
    _check(
        isinstance(a["fingerprint_a"], int) and isinstance(a["fingerprint_b"], int),
        "src-A has present (non-None) two-seed fingerprints",
    )

    b = by["src-B"]
    _check(
        b["session_count"] == 1 and b["canonical_count"] == 1
        and b["sibling_count"] == 0 and b["event_count"] == 1
        and b["tool_call_count"] == 1,
        "src-B: 1 canonical session, 1 event, 1 tool_call",
    )
    _check(
        b["owned_running_batches"] == 0
        and b["unclaimed_route_batches"] == 0
        and b["oldest_running_batch_age_seconds"] is None,
        "src-B has no running batches → 0/0/None",
    )

    c = by["src-C"]
    _check(
        c["session_count"] == 1 and c["event_count"] == 0
        and c["tool_call_count"] == 0,
        "src-C: 1 session, no events, no tool_calls",
    )
    _check(
        c["fingerprint_a"] is None and c["fingerprint_b"] is None,
        "src-C (no live events) carries None fingerprints (old LEFT JOIN NULL)",
    )


def test_expected_output_keys() -> None:
    rows = _census(page_size=100)
    expected = {
        "source_id", "source_kind", "root_uri",
        "session_count", "canonical_count", "sibling_count",
        "event_count", "fingerprint_a", "fingerprint_b", "tool_call_count",
        "owned_running_batches", "unclaimed_route_batches",
        "oldest_running_batch_age_seconds",
    }
    _check(
        all(set(r.keys()) == expected for r in rows),
        "every census row has exactly the service.census-consumed keys",
    )


def test_determinism_and_paging_invariance() -> None:
    # Build twice — fingerprints must be byte-identical (blake2b, not salted hash()).
    first = _by_source(_census(page_size=100))
    second = _by_source(_census(page_size=100))
    _check(
        first["src-A"]["fingerprint_a"] == second["src-A"]["fingerprint_a"]
        and first["src-A"]["fingerprint_b"] == second["src-A"]["fingerprint_b"],
        "fingerprint is deterministic across runs (re-baseline-safe)",
    )
    # Page size must not change the result — exercises multi-page keyset paging
    # (2+2+1) and proves no row is skipped or double-counted at a page boundary.
    paged = _by_source(_census(page_size=2))
    single = first
    _check(
        all(
            paged[s]["event_count"] == single[s]["event_count"]
            and paged[s]["fingerprint_a"] == single[s]["fingerprint_a"]
            and paged[s]["fingerprint_b"] == single[s]["fingerprint_b"]
            for s in ("src-A", "src-B", "src-C")
        ),
        "page_size=2 (multi-page) == page_size=100 (single page): paging is faithful",
    )


def test_source_with_batches_but_no_sessions_still_gets_a_tally() -> None:
    """src-D has RUNNING BATCHES and ZERO SESSIONS.

    The row-walking code keyed batches off ``import_batch.source_id`` directly,
    so such a source got a tally. Deriving the source list from the sessions
    index instead silently drops it — the census still renders, one source is
    just missing its batch health.

    The fixture alone does not catch that: src-D's DATA existing is not the same
    as src-D's data being ASSERTED. This is the assertion.
    """
    by = _by_source(_census(page_size=100))
    _check("src-D" in by, "src-D (no sessions) still has a census row")
    d = by.get("src-D", {})
    _check(
        d.get("owned_running_batches") == 1,
        f"src-D's running batch is tallied (got {d.get('owned_running_batches')}) "
        f"— 0 here means the source list came from the sessions index",
    )
    _check(
        d.get("session_count") == 0 and d.get("tool_call_count") == 0,
        "and it correctly reports zero sessions / tool calls",
    )


def test_tool_call_count_chunks_past_the_cap() -> None:
    """src-E holds more sessions than the cap, so the count MUST chunk.

    The fake refuses an over-cap ``= ANY`` exactly as the provider does, so an
    unchunked count raises rather than passing. Below the cap the two are
    indistinguishable — which is why the fixture carries 150 sessions for one
    source and not four.
    """
    by = _by_source(_census(page_size=100))
    e = by.get("src-E", {})
    _check(
        e.get("session_count") == _BULK_SESSIONS,
        f"src-E has all {_BULK_SESSIONS} sessions (got {e.get('session_count')})",
    )
    _check(
        e.get("tool_call_count") == _BULK_SESSIONS,
        f"src-E's {_BULK_SESSIONS} tool calls counted across chunk boundaries "
        f"(got {e.get('tool_call_count')})",
    )


def test_fingerprint_order_independence() -> None:
    # The XOR fold must be order-independent: fold the SAME events split into
    # pages in one order vs the reverse → identical fingerprints.
    def fold(pages: list[list[dict[str, object]]]) -> _CensusAggregator:
        # tool_calls / import_batches are no longer constructor inputs: they are
        # counted, not read (2026-08-16 count-not-walk). The fingerprint fold
        # never depended on them — only on the session index — so this test is
        # unaffected beyond the signature.
        agg = _CensusAggregator(
            sources=_SOURCES, sessions=_SESSIONS, fingerprint_seeds=_SEEDS,
        )
        for page in pages:
            agg.fold_event_page(page)
        return agg

    forward = _by_source(fold([_EVENTS[:2], _EVENTS[2:]]).result(now=_NOW))
    reverse = _by_source(
        fold([list(reversed(_EVENTS[2:])), list(reversed(_EVENTS[:2]))]).result(
            now=_NOW,
        )
    )
    _check(
        forward["src-A"]["fingerprint_a"] == reverse["src-A"]["fingerprint_a"]
        and forward["src-A"]["fingerprint_b"] == reverse["src-A"]["fingerprint_b"]
        and forward["src-B"]["fingerprint_a"] == reverse["src-B"]["fingerprint_a"],
        "fingerprint is order-independent (XOR) across re-ordered pages",
    )


def test_fingerprint_distinguishes_content() -> None:
    # Two corpora differing only in one event's content_blob_id must produce
    # different fingerprints (the fold actually folds the blob identity).
    base = _by_source(_census(page_size=100))["src-A"]
    reader = _FakeReader(
        {
            TABLE_SOURCE: _SOURCES,
            TABLE_SESSION: _SESSIONS,
            TABLE_EVENT: [
                {"id": "e1", "session_id": "s1", "content_blob_id": "CHANGED"},
                *_EVENTS[1:],
            ],
            TABLE_TOOL_CALL: _TOOL_CALLS,
            TABLE_IMPORT_BATCH: _IMPORT_BATCHES,
        }
    )
    mutated = _by_source(
        _census_with(reader)
    )["src-A"]
    _check(
        base["fingerprint_a"] != mutated["fingerprint_a"],
        "changing one event's content_blob_id changes the source fingerprint",
    )


def test_real_method_clock_normalization() -> None:
    # Run the REAL public census_source_rows() with a TZ-AWARE production-shape
    # clock (datetime.now(UTC) form) so the delegator's _naive_utc normalization
    # meets the naive started_at subtraction. A tz-aware − naive bug would
    # TypeError ONLY when a running batch exists — invisible to every other
    # (naive-now) test here. The clock is fixed-tz-aware (12:00:00 UTC) so the
    # oldest running batch (started 200s earlier) yields a deterministic age.
    repo = SessionLedgerRepository(
        state_service=cast("Any", _StateServiceShim(_reader())),
        clock=lambda: datetime(2026, 6, 15, 12, 0, 0, tzinfo=UTC),
    )
    rows = repo.census_source_rows()
    by = _by_source(rows)
    a = by["src-A"]
    _check(
        a["oldest_running_batch_age_seconds"] == 200,
        "real census_source_rows(): tz-aware clock normalized to naive UTC → "
        "batch age = 200s (no tz-aware − naive TypeError)",
    )
    _check(
        a["event_count"] == 3 and a["fingerprint_a"] is not None,
        "real census_source_rows() folds events through the base _query_ordered "
        "envelope seam",
    )


def test_session_walk_streams_exactly_once_and_the_big_tables_are_not_read() -> None:
    """``__session`` is walked ONCE; ``__tool_call`` / ``__import_batch`` are COUNTED.

    Two repairs, pinned together because the second removed what the first fixed.

    **Streaming (earlier wave).** ``_CensusAggregator`` consumed ``sessions``
    TWICE — once to build the id->source index, once to tally. Harmless for a
    list; with a generator the second pass sees an exhausted iterator and every
    session tally silently comes back EMPTY, with no error. The two passes are
    now one, asserted directly as walk-count == 1 rather than inferred from the
    tallies being right.

    **Count-not-walk (2026-08-16).** ``__tool_call`` (637,496 rows / 6,375 pages)
    and ``__import_batch`` (284,759 / 2,848) were walked ONLY to be counted, so
    they are no longer read at all — hence walk-count == 0 for both. Asserting
    ZERO, not "fewer": a regression that reintroduced a walk would otherwise
    hide behind correct-looking tallies.
    """
    reader = _reader()
    walker = _walker(reader)
    count, min_value = _aggregates(reader, walker)
    rows = build_census(
        query=reader.query,
        query_ordered=reader.query_ordered,
        walk=walker,
        count=count,
        min_value=min_value,
        now=_NOW,
        page_size=100,
        fingerprint_seeds=_SEEDS,
    )
    _check(
        walker.counts.get(TABLE_SESSION) == 1,
        f"__session walked exactly once (got {walker.counts.get(TABLE_SESSION)})",
    )
    for table in (TABLE_TOOL_CALL, TABLE_IMPORT_BATCH):
        _check(
            walker.counts.get(table) is None,
            f"{table} is NOT walked at all (got {walker.counts.get(table)}) — "
            f"it is counted; a reintroduced walk must fail here, not hide behind "
            f"a correct tally",
        )
    by = _by_source(rows)
    _check(
        by["src-A"]["session_count"] > 0,
        f"session tallies survived streaming (src-A session_count="
        f"{by['src-A']['session_count']}) — 0 here is the exhausted-iterator bug",
    )
    _check(
        by["src-A"]["tool_call_count"] > 0,
        "tool_call counts are attributed per source — they depend on the session "
        "index being COMPLETE before any counting runs",
    )


def test_counts_cannot_precede_the_session_index() -> None:
    """No count is issued before the session walk has been drained.

    The ordering is now structural — the counting methods live on the object
    that owns the ``session_id -> source_id`` index, so they cannot run before
    it exists. This asserts it anyway, because "structural" is a property of
    today's arrangement and a future refactor could hand the index in from
    outside and restore the hazard.

    Counting before the index is built attributes nothing: every per-source
    count returns zero, the census still renders, and nothing raises. Same
    silent-empty-tallies signature as the double-consumption bug.
    """
    reader = _reader()
    walker = _walker(reader)

    class _Recorder:
        def __init__(self) -> None:
            self.early = False

        def count(self, table: str, filters: dict[str, Any]) -> int:
            if not walker.drained:
                self.early = True
            return len(reader.query(table, {**filters}))

        def min_value(self, table: str, column: str, filters: dict[str, Any]) -> Any:
            values = [
                r[column] for r in reader.query(table, {**filters})
                if r.get(column) is not None
            ]
            return min(values) if values else None

    rec = _Recorder()
    build_census(
        query=reader.query,
        query_ordered=reader.query_ordered,
        walk=walker,
        count=rec.count,
        min_value=rec.min_value,
        now=_NOW,
        page_size=100,
        fingerprint_seeds=_SEEDS,
    )
    _check(
        not rec.early,
        "no count ran before the session walk was drained — the index is "
        "complete before anything is attributed to a source",
    )


def main() -> int:
    print("=== census_fold_smoke ===")
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            print(name)
            fn()
    print(f"\n{_passed} passed, {len(_failed)} failed")
    for label in _failed:
        print(f"  FAILED: {label}")
    return 1 if _failed else 0


if __name__ == "__main__":
    sys.exit(main())
