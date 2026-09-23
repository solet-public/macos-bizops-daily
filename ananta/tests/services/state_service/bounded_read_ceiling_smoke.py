#!/usr/bin/env python3
"""Smoke: a declared read ceiling refuses at the boundary instead of truncating.

Pins the contract of `ananta.services.state_service.bounded_read`, which exists
because an explicit `limit` is NOT on its own a safe repair for an unbounded
whole-table read.

The asymmetry it guards, straight from `read_bounds.resolve_read_limit`:

    no limit       -> (cap + 1, overflow_is_error=True)   loud refusal past cap
    explicit limit -> (limit,   overflow_is_error=False)  SILENT PREFIX past limit

That is correct for a top-N caller and wrong for a load-the-whole-table caller.
The same parameter answers a completeness question for one and asks one of the
other. So a site that adds `limit: 500` to a whole-table read and stops there has
DOWNGRADED itself: it used to fail loudly at 10,000 and now returns 500 of 600
without saying so. `assert_within_ceiling` restores the loud half.

The boundary is `>=`, not `>`, and that is the whole point: at exactly `ceiling`
rows a result is indistinguishable from a truncated one, so it is treated as
truncated. A `>` comparison here would be the bug this module exists to prevent,
which is why the calibration below mutates precisely that.

Sections G onward pin the module's OTHER answer, `iter_table_rows` — the walk for
a table that genuinely grows, where no ceiling can honestly claim smallness. Its
whole value is that the bound applies per PAGE while the caller still sees every
row, so the checks that matter are the ones a broken walk would fail while still
looking like it works: no row seen twice, no row dropped at a page boundary, a
same-`created_at` group not split by the boundary, and a page that does not
complete refused rather than read as "end of table". Its ceiling boundary is `>`,
NOT the `>=` above; that difference is deliberate and is pinned in J.

The pages are served by the REAL ordered-query primitives (`parse_ordered_query`
+ `apply_ordered_query_in_memory`), the same ones the in-memory backend and the
postgres providers run, rather than a hand-rolled matcher. A hand-rolled fake
here could not fail on an over-cap page size or a non-composite order_by, so it
would green a walk the real provider refuses.

PURE UNIT: no DB, no platform, no state service.

Run:
    .venv/bin/python3 ananta/tests/services/state_service/bounded_read_ceiling_smoke.py

Project policy: no pytest. Exits 0 on success, 1 on failure.
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT / "ananta" / "src"))

from ananta.services.state_service.bounded_read import (  # noqa: E402
    PagedReadError,
    ReadCeilingError,
    assert_within_ceiling,
    iter_table_rows,
)
from ananta.services.state_service.ordered_query import (  # noqa: E402
    apply_ordered_query_in_memory,
    parse_ordered_query,
)

_passed = 0
_failed: list[str] = []

_NS = "widgets_ns"
_TABLE = "widgets"
_REASON = "bounded by X."


def _check(condition: object, label: str) -> None:
    global _passed
    if condition:
        _passed += 1
        print(f"  PASS  {label}")
    else:
        _failed.append(label)
        print(f"  FAIL  {label}")


class _PagingState:
    """Serves ``query_ordered`` pages through the REAL ordered-query primitives.

    Not a hand-rolled matcher: ``parse_ordered_query`` enforces the composite
    order_by, the identifier grammar and the page cap, and
    ``apply_ordered_query_in_memory`` applies the filter grammar, the type-faithful
    sort, the ``after`` cursor and the limit. So a walk that asks for an over-cap
    page, or a single-column cursor, fails here exactly as it would in production.
    """

    def __init__(self, rows: list[dict[str, object]]) -> None:
        self.rows = rows
        self.queries: list[dict[str, object]] = []
        self.fail_on_call: int | None = None

    def query_ordered(self, namespace: str, query: dict[str, object]) -> object:  # noqa: ARG002
        self.queries.append(dict(query))
        if self.fail_on_call is not None and len(self.queries) == self.fail_on_call:
            return {"action_status": "failed", "error": {"code": "provider.boom"}}
        spec = parse_ordered_query(query)
        selected = apply_ordered_query_in_memory(list(self.rows), spec)
        return {
            "action_status": "completed",
            "data": {"records": [dict(r) for r in selected], "count": len(selected)},
        }


def _row(n: int, *, created_at: str | None = None, **extra: object) -> dict[str, object]:
    """A standardizer-shaped row.

    Both cursor columns are zero-padded so lexical order matches numeric order —
    otherwise the fixture's own ordering, not the walk's, decides whether an
    order assertion passes, and the check stops measuring the code.
    """
    return {
        "id": f"wid-{n:05d}",
        "created_at": created_at or f"2026-08-15T00:00:00.{n:06d}",
        "is_deleted": 0,
        **extra,
    }


def _walk(state: _PagingState, *, ceiling: int = 10_000, **kwargs: object) -> list[dict[str, object]]:
    return list(
        iter_table_rows(
            state,  # pyright: ignore[reportArgumentType]  # duck-typed fake
            namespace=_NS,
            table=_TABLE,
            filters=kwargs.pop("filters", {}),  # pyright: ignore[reportArgumentType]
            ceiling=ceiling,
            reason=_REASON,
            **kwargs,  # pyright: ignore[reportArgumentType]
        ),
    )


def _paging_completeness() -> None:
    print("\nG. a result set larger than one page is walked WHOLE")
    # 250 rows = 3 pages at the 100-row page size. A walk that read one page and
    # stopped would return 100 and look entirely healthy.
    rows = [_row(n) for n in range(250)]
    state = _PagingState(rows)
    walked = _walk(state)
    _check(len(walked) == 250, f"every row is yielded across pages (got {len(walked)})")
    ids = [str(r["id"]) for r in walked]
    _check(ids == sorted(ids), "rows arrive in (created_at, id) ascending order")
    _check(len(set(ids)) == len(ids), "no row is yielded TWICE — the cursor advances")
    _check(
        ids == [str(r["id"]) for r in rows],
        "the walked set is exactly the table, no row dropped at a page boundary",
    )
    _check(
        len(state.queries) == 3,
        f"three pages issued for 250 rows, then a short page ends it "
        f"(got {len(state.queries)})",
    )
    _check(
        state.queries[0].get("after") is None
        and state.queries[1].get("after") is not None,
        "the first page carries no cursor; later pages carry the previous page's last row",
    )

    print("\nH. an EXACT multiple of the page size still terminates")
    # 200 rows: the third query returns zero records. A walk that only stopped on
    # a short page and never on an empty one would spin forever here.
    exact = _PagingState([_row(n) for n in range(200)])
    _check(len(_walk(exact)) == 200, "200 rows (2 full pages) are walked whole")
    _check(len(exact.queries) == 3, "a third, empty page ends the walk")

    print("\nI. a same-created_at group spanning a page boundary is not split")
    # 150 rows sharing ONE created_at. With a created_at-only cursor, page 2 would
    # ask for rows strictly after that timestamp and silently drop the other 50.
    tied = _PagingState([_row(n, created_at="2026-08-15T12:00:00.000000") for n in range(150)])
    tied_walk = _walk(tied)
    _check(
        len(tied_walk) == 150,
        f"all 150 same-timestamp rows survive the boundary (got {len(tied_walk)}) "
        f"— this is what the (created_at, id) composite buys",
    )
    _check(
        len({r["id"] for r in tied_walk}) == 150,
        "and none of them is repeated",
    )

def _paging_refusals() -> None:
    print("\nJ. the ceiling refuses on EXCEEDING it — '>', not the '>=' above")
    at = _PagingState([_row(n) for n in range(150)])
    _check(
        len(_walk(at, ceiling=150)) == 150,
        "a walk of EXACTLY `ceiling` rows completes — paging knows the count "
        "exactly, so the boundary is not ambiguous the way a single read's is",
    )
    over = _PagingState([_row(n) for n in range(151)])
    raised = False
    message = ""
    try:
        _walk(over, ceiling=150)
    except ReadCeilingError as exc:
        raised = True
        message = str(exc)
    _check(raised, "one row past the ceiling RAISES")
    _check(_TABLE in message, "the refusal names the table")
    _check(_REASON in message, "the refusal repeats the call site's WRITTEN justification")
    _check("150" in message, "the refusal names the ceiling")

    print("\nK. a page that does not COMPLETE is fatal, never read as end-of-table")
    # The dangerous failure: a provider error envelope treated as an empty page
    # would end the walk early and report a PREFIX as the whole table — silently.
    broken = _PagingState([_row(n) for n in range(250)])
    broken.fail_on_call = 2
    short = False
    paged_error = False
    try:
        _walk(broken)
    except PagedReadError:
        paged_error = True
    except ReadCeilingError:
        short = True
    _check(paged_error, "a non-completed envelope raises PagedReadError")
    _check(not short, "and is NOT mistaken for a ceiling problem")

def _paging_mechanics() -> None:
    print("\nL. the walk STREAMS — one page in flight, not the whole table")
    lazy = _PagingState([_row(n) for n in range(250)])
    gen = iter_table_rows(
        lazy,  # pyright: ignore[reportArgumentType]  # duck-typed fake
        namespace=_NS,
        table=_TABLE,
        filters={},
        ceiling=10_000,
        reason=_REASON,
    )
    _check(len(lazy.queries) == 0, "constructing the iterator issues NO query")
    next(gen)
    _check(
        len(lazy.queries) == 1,
        f"taking the first row reads exactly one page, not the table "
        f"(got {len(lazy.queries)} queries)",
    )
    gen.close()

    print("\nM. filters and include_deleted reach the provider")
    mixed = _PagingState(
        [_row(n, kind="a") for n in range(5)] + [_row(100 + n, kind="b") for n in range(5)],
    )
    _check(
        [r["kind"] for r in _walk(mixed, filters={"kind": "a"})] == ["a"] * 5,
        "an equality filter is applied by the provider, not in Python afterwards",
    )
    deleted = _PagingState(
        [_row(n) for n in range(3)] + [_row(50 + n, is_deleted=1) for n in range(3)],
    )
    _check(
        len(_walk(deleted)) == 3,
        "include_deleted defaults to False — soft-deleted rows are excluded",
    )
    _check(
        len(_walk(deleted, include_deleted=True)) == 6,
        "include_deleted=True returns them",
    )

    print("\nN. a row without the cursor columns is refused, not skipped")
    # A cursor column that is absent from the page's last row leaves the walk with
    # nowhere to resume from. Refusing is the only safe answer: guessing a cursor
    # restarts the walk or skips a span, and both are wrong WITHOUT an error.
    # Served by a bespoke stand-in rather than the real matcher, because the real
    # one would sort a cursor-less row somewhere harmless and never expose the
    # branch — the fixture has to place the fault where the code meets it.
    class _HeadlessLastRow:
        def __init__(self) -> None:
            self.calls = 0

        def query_ordered(self, namespace: str, query: dict[str, object]) -> object:  # noqa: ARG002
            self.calls += 1
            page = [_row(n) for n in range(100)]
            del page[-1]["created_at"]
            return {"action_status": "completed", "data": {"records": page}}

    headless = _HeadlessLastRow()
    cursor_error = False
    message_n = ""
    try:
        list(
            iter_table_rows(
                headless,  # pyright: ignore[reportArgumentType]  # duck-typed fake
                namespace=_NS,
                table=_TABLE,
                filters={},
                ceiling=10_000,
                reason=_REASON,
            ),
        )
    except PagedReadError as exc:
        cursor_error = True
        message_n = str(exc)
    _check(cursor_error, "a missing cursor column raises PagedReadError")
    _check("created_at" in message_n, "the refusal names the missing cursor column")
    _check(
        headless.calls == 1,
        f"and it refuses BEFORE issuing another page — no blind re-read "
        f"(got {headless.calls} calls)",
    )


def main() -> int:
    print("A. under the ceiling — a complete result passes through untouched")
    rows = [{"id": n} for n in range(9)]
    returned = assert_within_ceiling(rows, table="widgets", ceiling=10, reason="bounded by X.")
    _check(returned == rows, "returns the SAME rows (not a copy, not a prefix)")
    _check(returned is rows, "returns the identical list object")

    print("\nB. empty is complete, not suspicious")
    _check(
        assert_within_ceiling([], table="widgets", ceiling=10, reason="bounded by X.") == [],
        "an empty result is returned, never treated as a truncation",
    )

    print("\nC. AT the ceiling — refused, because it cannot be told from a prefix")
    at_ceiling = [{"id": n} for n in range(10)]
    raised = False
    message = ""
    try:
        assert_within_ceiling(at_ceiling, table="widgets", ceiling=10, reason="bounded by X.")
    except ReadCeilingError as exc:
        raised = True
        message = str(exc)
    _check(raised, "len(records) == ceiling RAISES (the >= boundary, not >)")

    print("\nD. the refusal is instructive, not just loud")
    _check("widgets" in message, "names the table")
    _check("10" in message, "names the ceiling")
    _check("bounded by X." in message, "repeats the call site's WRITTEN justification")
    _check(
        "PREFIX" in message or "prefix" in message,
        "says the result may be a prefix — the reason it is refused",
    )
    _check(
        "query_ordered" in message,
        "names a sanctioned way forward (paginate) rather than only refusing",
    )

    print("\nE. over the ceiling — also refused")
    over = [{"id": n} for n in range(25)]
    raised_over = False
    try:
        assert_within_ceiling(over, table="widgets", ceiling=10, reason="bounded by X.")
    except ReadCeilingError:
        raised_over = True
    _check(raised_over, "len(records) > ceiling RAISES")

    print("\nF. it REFUSES, it never truncates")
    # The failure mode this module exists to prevent is returning rows[:ceiling].
    # If assert_within_ceiling ever "helpfully" trimmed instead of raising, C and
    # E would still pass while the caller silently lost its tail.
    trimmed = False
    try:
        result = assert_within_ceiling(over, table="widgets", ceiling=10, reason="bounded by X.")
        trimmed = isinstance(result, list)
    except ReadCeilingError:
        trimmed = False
    _check(not trimmed, "an over-ceiling call returns NOTHING at all — no silent prefix")

    # Split three ways rather than one long function: the CC gate reads the
    # combined form as D(21), and the three groups are genuinely different
    # questions — is the walk COMPLETE, does it REFUSE correctly, and does the
    # machinery (streaming, filters, cursor) behave.
    _paging_completeness()
    _paging_refusals()
    _paging_mechanics()

    print(f"\n{_passed} passed, {len(_failed)} failed")
    for label in _failed:
        print(f"  FAILED: {label}")
    return 1 if _failed else 0


if __name__ == "__main__":
    sys.exit(main())
