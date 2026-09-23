#!/usr/bin/env python3
"""Offline smoke for ``walk_sessions_page`` — the ``list_sessions`` page walk (no pytest).

Read-cap sweep, 2026-08-16 (lane-ak). ``list_sessions``' default path read
``__session`` whole — **14,412 rows to return 50** — then applied its windows,
sort and limit in Python. Measured against the serving release
``rel-20260816T042114Z-41eed531f`` the verb is **dead**:

    code: query.unbounded_read_over_cap
    read_state on table 'session' returned more than the 100-row cap ...
    details: {namespace: session_ledger, table: session, cap_rows: 100}

It is now a keyset walk that pages in the caller's sort order and post-filters
each page. This smoke pins that walk.

WHY THE FIXTURE LOOKS NOTHING LIKE PRODUCTION, AND WHY SIZE ALONE IS NOT IT
===========================================================================
The pre-existing ``list_sessions_m17_filters_smoke.py`` already passes 24/24
against this code — but its fixtures are a handful of rows, so the walk completes
in ONE SHORT PAGE and the cursor is never used. In production the default path
always fills a page and always advances. **That green is evidence about a
different program.**

The obvious correction — "use a big fixture" — is not enough either, and the
distinction is the whole design of this file:

    A 250-row fixture whose qualifying rows all sit in page 1 tests exactly what
    a 5-row fixture tests: the walk still returns on its first iteration.

So the defining property is not size, it is POSITION:

    >>> Qualifying rows must lie BEYOND the first page. The cursor only engages
    >>> when page 1 yields fewer than `limit` survivors.

Hence **band A**: 90 rows at the HEAD of the sort order that FAIL the window.
Band A is load-bearing twice — it forces the cursor to advance, and it is the
only reason mutation M3 is detectable (see ``test_pushing_limit_down_under_returns``).

Run:
    .venv/bin/python3 ananta/tests/llm/session_ledger/session_page_walk_smoke.py
"""

from __future__ import annotations

import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast

REPO_ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(REPO_ROOT / "ananta" / "src"))

from ananta.llm.session_ledger import read_support  # noqa: E402
from ananta.llm.session_ledger.read_support import (  # noqa: E402
    SessionWindow,
    walk_sessions_page,
)
from ananta.llm.session_ledger.schema import TABLE_SESSION  # noqa: E402
from ananta.llm.session_ledger.types import SessionsOrderBy  # noqa: E402

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


#: Page size the walk is driven at. The production constant is 100; shrinking it
#: keeps the fixture small enough to reason about while still crossing several
#: boundaries. Every band size below is expressed in multiples of this.
_PAGE = 10
_LIMIT = 5

_BASE = datetime(2026, 8, 16, 12, 0, 0, tzinfo=UTC)

# Window: [WINDOW_SINCE, WINDOW_UNTIL] on last_event_at. TWO-SIDED on purpose —
# that is what the flat filter grammar cannot express (one condition per column),
# so only ONE side is pushed down and both are re-checked in Python. A fixture
# with a one-sided window would never exercise that split.
_WINDOW_SINCE = _BASE - timedelta(minutes=400)
_WINDOW_UNTIL = _BASE - timedelta(minutes=100)


def _session(
    *,
    ordinal: int,
    minutes_ago: float,
    first_minutes_ago: float | None = None,
    vendor: str = "claude_code",
    is_deleted: int = 0,
) -> dict[str, object]:
    last = _BASE - timedelta(minutes=minutes_ago)
    first = _BASE - timedelta(minutes=first_minutes_ago if first_minutes_ago is not None
                              else minutes_ago + 1)
    return {
        "id": f"les_{ordinal:04d}",
        "source_id": "src_a",
        "external_session_id": f"ext_{ordinal:04d}",
        "vendor": vendor,
        "vendor_session_label": None,
        "project_path": "/p",
        "first_event_at": first,
        "last_event_at": last,
        "event_count": 1,
        "canonical_external_session_id": None,
        "is_deleted": is_deleted,
    }


def _fixture() -> list[dict[str, object]]:
    """Rows in LAST_EVENT_AT_DESC order — index order == scan order.

    | band | idx     | in window? | why it exists                                   |
    |------|---------|------------|-------------------------------------------------|
    | A    | 0-89    | NO (too new)| 9 pages of non-qualifying rows AT THE HEAD.     |
    |      |         |            | Forces the cursor past page 1, and is the ONLY   |
    |      |         |            | reason M3 (limit-pushdown) is detectable.        |
    | B    | 90-99   | YES        | survivors ending exactly ON the page-9 boundary  |
    | C    | 100-109 | YES        | survivors just AFTER it — a page-1-only walk     |
    |      |         |            | loses these                                      |
    | D    | 110-129 | YES (tie)  | 20 rows sharing ONE last_event_at, straddling    |
    |      |         |            | the idx-120 page boundary -> the id tie-break    |
    | E    | 130-139 | mixed      | poison rows: soft-deleted, wrong vendor, and     |
    |      |         |            | first_event_at out of ITS window while           |
    |      |         |            | last_event_at is inside                          |
    | F    | 140-159 | NO (too old)| tail past the window's lower bound              |
    """
    rows: list[dict[str, object]] = []
    idx = 0

    for _ in range(90):  # band A — newer than WINDOW_UNTIL, so OUT of window
        rows.append(_session(ordinal=idx, minutes_ago=10 + idx * 0.5))
        idx += 1
    for _ in range(20):  # bands B + C — inside the window
        rows.append(_session(ordinal=idx, minutes_ago=150 + (idx - 90) * 0.5))
        idx += 1
    tie_minutes = 200.0
    for _ in range(20):  # band D — one shared last_event_at across a boundary
        rows.append(_session(ordinal=idx, minutes_ago=tie_minutes))
        idx += 1
    # band E — poison rows, all inside the last_event_at window
    rows.append(_session(ordinal=idx, minutes_ago=250, is_deleted=1))
    idx += 1
    rows.append(_session(ordinal=idx, minutes_ago=251, vendor="codex"))
    idx += 1
    # last_event_at inside its window, first_event_at OUTSIDE its own window:
    # only a post-filter that checks BOTH windows drops this one.
    rows.append(_session(ordinal=idx, minutes_ago=252, first_minutes_ago=5000))
    idx += 1
    for _ in range(7):
        rows.append(_session(ordinal=idx, minutes_ago=253 + idx * 0.1))
        idx += 1
    for _ in range(20):  # band F — older than WINDOW_SINCE, OUT of window
        rows.append(_session(ordinal=idx, minutes_ago=500 + idx))
        idx += 1
    return rows


class _FakeOrderedState:
    """``query_ordered`` seam honouring the parts of the contract the walk uses.

    Faithful on purpose about the things a looser fake would paper over, each of
    which would turn a real defect green:

    * ``after`` is a ROW-VALUE comparison over the ``order_by`` columns, not a
      per-column AND — and it is direction-aware, so a DESC walk is compared with
      ``<`` rather than ``>``. The event-walk fake is ascending-only; reusing it
      unchanged here would silently pass a desc walk on asc semantics.
    * an ``after`` whose arity differs from ``order_by`` RAISES, as the real
      primitive does.
    * ``include_deleted`` defaults to False, i.e. ``is_deleted == 0`` — the
      predicate that REPLACED the caller's explicit ``{"is_deleted": 0}``, and
      the only thing now excluding soft-deleted rows.
    * ``limit`` above the primitive's 100-row ceiling RAISES rather than clamping.
    * the Gap-A ``gte``/``lte`` comparators, because the walk pushes ONE side of
      each window down through them.
    """

    MAX_ORDERED_LIMIT = 100

    def __init__(self, rows: list[dict[str, object]]) -> None:
        self._rows = rows
        self.calls = 0
        self.pages_served: list[int] = []
        self.limits_seen: list[int] = []

    def __call__(
        self,
        table: str,
        *,
        filters: dict[str, object],
        order_by: list[list[str]],
        limit: int,
        after: tuple[object, ...] | None = None,
    ) -> list[dict[str, object]]:
        columns, descending = _validate(table, order_by, limit, after, self.MAX_ORDERED_LIMIT)
        self.calls += 1
        self.limits_seen.append(limit)
        page = _select(self._rows, filters, columns, descending, after, limit)
        self.pages_served.append(len(page))
        return page


def _validate(
    table: str,
    order_by: list[list[str]],
    limit: int,
    after: tuple[object, ...] | None,
    max_limit: int,
) -> tuple[list[str], bool]:
    if table != TABLE_SESSION:
        raise AssertionError(f"unexpected table {table!r}")
    if limit > max_limit:
        raise AssertionError(
            f"limit {limit} exceeds the primitive's {max_limit}-row ceiling; the "
            f"real query_ordered refuses rather than clamping (Gap-C fail-loud)"
        )
    if after is not None and len(after) != len(order_by):
        raise AssertionError(
            f"cursor arity {len(after)} != order_by arity {len(order_by)}; the real "
            f"primitive compiles `after` as ONE row-value comparison"
        )
    directions = {pair[1] for pair in order_by}
    if len(directions) != 1:
        raise AssertionError("order_by columns must share one direction")
    return [pair[0] for pair in order_by], directions == {"desc"}


def _op_matches(cell: object, spec: dict[str, object]) -> bool:
    """One structured filter spec, mirroring ``ordered_query._filter_matches``.

    A NULL cell on the left of a comparison is UNKNOWN and therefore excluded —
    SQL three-valued logic, and the same convention the real primitive uses.
    Fails loud on an op the walk should never emit, rather than treating it as a
    match: a fake that quietly accepts an unknown op turns a malformed filter
    into a passing test.
    """
    op = spec.get("op")
    if op == "is_null":
        return cell is None
    if op == "gte":
        return cell is not None and cast("Any", cell) >= spec["value"]
    if op == "lte":
        return cell is not None and cast("Any", cell) <= spec["value"]
    raise AssertionError(f"fake does not implement op {op!r}")


def _matches(row: dict[str, object], filters: dict[str, object]) -> bool:
    for key, spec in filters.items():
        cell = row.get(key)
        ok = _op_matches(cell, spec) if isinstance(spec, dict) else cell == spec
        if not ok:
            return False
    return True


def _select(
    rows: list[dict[str, object]],
    filters: dict[str, object],
    columns: list[str],
    descending: bool,
    after: tuple[object, ...] | None,
    limit: int,
) -> list[dict[str, object]]:
    def key(row: dict[str, object]) -> tuple[Any, ...]:
        return tuple(cast("Any", row[column]) for column in columns)

    matched = [r for r in rows if _matches(r, filters) and r.get("is_deleted") == 0]
    matched.sort(key=key, reverse=descending)
    if after is not None:
        cursor = tuple(after)
        matched = [r for r in matched if (key(r) < cursor if descending else key(r) > cursor)]
    return [dict(r) for r in matched[:limit]]


def _window() -> SessionWindow:
    return SessionWindow(
        since=_WINDOW_SINCE.replace(tzinfo=None),
        until=_WINDOW_UNTIL.replace(tzinfo=None),
        first_event_since=(_BASE - timedelta(minutes=600)).replace(tzinfo=None),
        first_event_until=None,
    )


def _naive(rows: list[dict[str, object]]) -> list[dict[str, object]]:
    """Fixture datetimes as the provider serves them: naive UTC (the F1 seam)."""
    out = []
    for row in rows:
        copy = dict(row)
        for column in ("first_event_at", "last_event_at"):
            value = cast("datetime", copy[column])
            copy[column] = value.replace(tzinfo=None)
        out.append(copy)
    return out


def _walk(state: _FakeOrderedState, *, limit: int = _LIMIT) -> list[dict[str, object]]:
    original = read_support._SESSION_PAGE_ROWS  # noqa: SLF001
    read_support._SESSION_PAGE_ROWS = _PAGE  # noqa: SLF001
    try:
        return walk_sessions_page(
            state,
            filters={"vendor": "claude_code", "canonical_external_session_id": {"op": "is_null"}},
            window=_window(),
            order_by=SessionsOrderBy.LAST_EVENT_AT_DESC,
            limit=limit,
        )
    finally:
        read_support._SESSION_PAGE_ROWS = original  # noqa: SLF001


def _state() -> _FakeOrderedState:
    return _FakeOrderedState(_naive(_fixture()))


# ---------------------------------------------------------------------------
# The fixture must be able to fail before any green from it means anything
# ---------------------------------------------------------------------------


def test_band_a_forces_the_cursor_to_advance() -> None:
    """The property the whole file rests on: survivors lie BEYOND page 1.

    If band A is ever weakened so that qualifying rows reach page 1, this walk
    returns on its first iteration, the cursor is never exercised, and every
    other green in this file becomes evidence about a different program.
    """
    state = _state()
    _walk(state)
    _check(
        state.calls >= 2,
        f"walk needed {state.calls} pages at page-size {_PAGE} — the cursor "
        f"ADVANCED (band A holds {90 // _PAGE} pages of non-qualifying rows)",
    )


def test_fixture_is_adversarial_where_production_is_not() -> None:
    rows = _naive(_fixture())
    lasts = [r["last_event_at"] for r in rows]
    _check(len(lasts) != len(set(lasts)), "fixture has last_event_at TIES (band D)")
    _check(any(r["is_deleted"] == 1 for r in rows), "fixture has a SOFT-DELETED row")
    _check(any(r["vendor"] != "claude_code" for r in rows), "fixture has a WRONG-VENDOR row")
    _check(len(rows) > 3 * _PAGE, f"fixture spans many pages ({len(rows)} rows / {_PAGE})")


# ---------------------------------------------------------------------------
# The walk
# ---------------------------------------------------------------------------


def test_walk_fills_the_limit_from_beyond_page_one() -> None:
    state = _state()
    got = _walk(state)
    _check(len(got) == _LIMIT, f"walk returned a FULL page of {_LIMIT} (got {len(got)})")
    _check(len({r["id"] for r in got}) == len(got), "no duplicate rows across page boundaries")


def test_walk_returns_only_in_window_rows() -> None:
    state = _state()
    got = _walk(state)
    ok = all(
        _WINDOW_SINCE.replace(tzinfo=None)
        <= cast("datetime", r["last_event_at"])
        <= _WINDOW_UNTIL.replace(tzinfo=None)
        for r in got
    )
    _check(ok, "every returned row satisfies BOTH sides of the two-sided window")


def test_walk_is_sort_ordered() -> None:
    state = _state()
    lasts = [cast("datetime", r["last_event_at"]) for r in _walk(state)]
    _check(lasts == sorted(lasts, reverse=True), "result is last_event_at DESC")


def test_poison_rows_never_appear() -> None:
    state = _state()
    got = _walk(state, limit=50)
    ids = {r["id"] for r in got}
    rows = {r["id"]: r for r in _naive(_fixture())}
    deleted = {i for i, r in rows.items() if r["is_deleted"] == 1}
    wrong_vendor = {i for i, r in rows.items() if r["vendor"] != "claude_code"}
    bad_first = {
        i for i, r in rows.items()
        if cast("datetime", r["first_event_at"]) < (_BASE - timedelta(minutes=600)).replace(tzinfo=None)
    }
    _check(not (ids & deleted), "soft-deleted row excluded (query_ordered include_deleted default)")
    _check(not (ids & wrong_vendor), "wrong-vendor row excluded on EVERY page, not just the first")
    _check(
        not (ids & bad_first),
        "row failing the FIRST_event_at window excluded — both windows are "
        "post-filtered, not just the one whose side was pushed down",
    )


def test_tie_group_survives_a_page_boundary() -> None:
    """Band D — 20 rows sharing ONE ``last_event_at``, straddling a page edge.

    Reached only at a limit large enough to walk past bands B+C, which is why it
    takes its own test: at the default ``_LIMIT`` the walk fills up and returns
    before band D is ever paged. The FIRST version of this file had the tie group
    in the fixture but no test that reached it, so mutation M1 (dropping the
    ``id`` tie-break) passed 17/17 — the group was present and unexercised.

    A sort column alone is not a total order. With a ``(last_event_at,)`` cursor,
    ``after`` is strictly-less-than the last row of the page, so every remaining
    member of a tie group split by the boundary is skipped — silently, with no
    short page and no error.
    """
    state = _state()
    got = _walk(state, limit=50)
    tie_ids = {
        str(r["id"])
        for r in _naive(_fixture())
        if cast("datetime", r["last_event_at"])
        == (_BASE - timedelta(minutes=200)).replace(tzinfo=None)
    }
    returned = {str(r["id"]) for r in got} & tie_ids
    _check(
        len(tie_ids) == 20,
        f"fixture's tie group is {len(tie_ids)} rows (must straddle a "
        f"{_PAGE}-row page boundary to be worth testing)",
    )
    _check(
        returned == tie_ids,
        f"ALL {len(tie_ids)} tie-group rows survived the page boundary "
        f"(got {len(returned)}) — this is what the id tie-break buys",
    )


def test_walk_never_requests_more_than_the_page_size() -> None:
    state = _state()
    _walk(state, limit=50)
    _check(
        all(limit == _PAGE for limit in state.limits_seen),
        f"every provider call asked for exactly {_PAGE} rows, never the caller's "
        f"limit — that distinction IS the fix (see M3)",
    )


def test_exhaustion_terminates_without_filling_the_limit() -> None:
    """A limit larger than the qualifying set must end, not spin."""
    state = _state()
    got = _walk(state, limit=50)
    _check(0 < len(got) < 50, f"walk exhausted the source and returned {len(got)} < 50")
    _check(state.pages_served[-1] < _PAGE, "walk ended on a short page")


# ---------------------------------------------------------------------------
# M3, inlined — the mutation a reviewer would never ask for
# ---------------------------------------------------------------------------


def test_pushing_limit_down_under_returns() -> None:
    """The rejected design, run against this fixture, to prove it IS rejected.

    The tempting repair is to push ``order_by`` AND ``limit`` into the provider
    and drop the post-filter. That returns the top ``limit`` rows in sort order,
    of which — because of band A — almost none qualify. The caller gets fewer
    rows than exist, with no error: **a silent under-return**, strictly worse
    than the unbounded read it replaced.

    This is inlined rather than left as a manual mutation because it is the
    single most valuable check in the file and the one most likely to be
    "simplified" away by someone who has not read the history.

    **If this test ever passes trivially, band A has been weakened — repair the
    fixture, do not delete the test.**
    """
    state = _state()
    correct = _walk(state, limit=_LIMIT)

    wrong = _select(
        _naive(_fixture()),
        {"vendor": "claude_code", "canonical_external_session_id": {"op": "is_null"}},
        ["last_event_at", "id"],
        True,
        None,
        _LIMIT,
    )
    survivors = [
        r for r in wrong
        if _WINDOW_SINCE.replace(tzinfo=None)
        <= cast("datetime", r["last_event_at"])
        <= _WINDOW_UNTIL.replace(tzinfo=None)
    ]
    _check(
        len(correct) == _LIMIT,
        f"the paged walk returns a full {_LIMIT} rows",
    )
    _check(
        len(survivors) < len(correct),
        f"limit-pushdown WITHOUT post-filtering returns {len(survivors)} qualifying "
        f"rows vs the walk's {len(correct)} — the silent under-return this design "
        f"exists to prevent, and band A is what makes it visible",
    )


def main() -> int:
    print("=== session_page_walk_smoke ===")
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            print(name)
            fn()
    print(f"\n{_passed} passed, {len(_failed)} failed")
    for label in _failed:
        print(f"  FAILED: {label}")
    return 1 if _failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
