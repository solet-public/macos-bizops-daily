#!/usr/bin/env python3
"""Offline smoke for the per-session ``(sequence, id)`` event walk (no pytest).

Read-cap sweep, 2026-08-15 PDT / 2026-08-16 UTC (lane-ak). ``read.py``'s
per-session event read was an unbounded ``query_state`` over a 2,077,082-row
table followed by a Python ``rows.sort()``; ``dcb1722c7`` lowered the default row
bound from 10,000 to 100 and it became a refusal for any session past its first
hundred events. It is now :func:`iter_events_by_sequence` — a paged keyset walk
whose ordering lives in the read rather than in a post-hoc sort.

This smoke pins that walk. Its companion measurement is
``workbench/2026-08-16_event_walk_live_reconciliation.py``, which verifies the
same MECHANISM live at real scale (22 pages, 2,174 rows, totals reconciled
against ``count``) but cannot verify THIS function — driving it needs a
``query_ordered`` seam, and ``state_service::query_ordered`` is not a registered
bridge process while in-process access needs a database password only the state
service holds. So: pattern verified live, function verified here, its own live
path unexercised. That split is deliberate and should not be blurred.

WHY THE FIXTURE LOOKS NOTHING LIKE PRODUCTION
=============================================
Measured on the live ledger, the busiest session is **benign in every dimension
this walk can get wrong**: 2,174 events, sequences dense ``1..2,174``, and 2,174
DISTINCT ``created_at`` values — largest tie group of size 1. A fixture modelled
on that data would exercise nothing, because every cursor variant, correct or
broken, passes on dense untied rows.

So the fixture is deliberately adversarial exactly where production happens not
to be:

* **``sequence`` ties** — so the ``id`` tie-break is *exercised* rather than
  merely present. A single-column cursor splits a tie group across a page
  boundary and silently drops the remainder;
  :func:`test_single_column_cursor_drops_rows` is the mutation proving this
  fixture can detect that.
* **``sequence`` gaps** — so nothing may assume density. Production is dense
  today; one soft-deleted event breaks that, and a walk that quietly assumes
  contiguity is a bug waiting for the first non-dense session.
* **``created_at`` ties** — see the warning below. Do not delete these.

THE ``created_at``-TIES GROUP IS LOAD-BEARING — READ BEFORE REMOVING IT
=======================================================================
It pins a divergence the LIVE DATA DOES NOT EXHIBIT, and it is the reason this
site does not use the shared ``bounded_read.iter_table_rows``.

That helper's cursor is ``(created_at, id)``; this caller's contract is
``sequence`` order. On live data the two coincide exactly — a property of how
those sessions were ingested (one linear pass, monotonically increasing
``created_at``), not a guarantee. A re-ingest or backfill writes ``created_at``
at import wall-clock while ``sequence`` stays the vendor ordinal, and then they
part company.

A future reader who measures production, finds no ``created_at`` ties and
deletes this group as unrealistic would remove the only executable evidence for
the rule in ``iter_table_rows``'s own docstring — after which "simplifying" this
site onto the shared helper would pass review and silently reorder events.
:func:`test_helper_cursor_would_reorder_events` is what makes that rule
self-enforcing instead of a comment someone has to remember. A prose rule
degrades; a rule with a fixture behind it does not.

Run:
    .venv/bin/python3 ananta/tests/llm/session_ledger/event_sequence_walk_smoke.py
"""

from __future__ import annotations

import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast

REPO_ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(REPO_ROOT / "ananta" / "src"))

from ananta.llm.session_ledger import read as read_module  # noqa: E402
from ananta.llm.session_ledger.read import iter_events_by_sequence  # noqa: E402
from ananta.llm.session_ledger.schema import TABLE_EVENT  # noqa: E402

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


# A small page so a modest fixture straddles SEVERAL boundaries. The production
# constant is 100; testing at 100 would need a 250-row fixture to cross two
# boundaries, and the tie/gap groups would be lost in the noise. 10 gives the
# same coverage at a size a reader can hold in their head.
_PAGE = 10

_SESSION = "les_fixture"
_OTHER_SESSION = "les_other"
_BASE = datetime(2026, 8, 16, 0, 0, 0, tzinfo=UTC)


def _sort_key(row: dict[str, object], columns: list[str]) -> tuple[Any, ...]:
    """The row's value under the ``order_by`` columns, as one comparable tuple.

    A TUPLE, because that is what makes the cursor a row-value comparison rather
    than a per-column AND — the distinction the walk's correctness rests on.
    """
    return tuple(cast("Any", row[column]) for column in columns)


def _assert_query_ordered_contract(
    *,
    table: str,
    order_by: list[list[str]],
    limit: int,
    after: tuple[object, ...] | None,
    max_limit: int,
) -> list[str]:
    """Refuse anything the real primitive would refuse; return the sort columns.

    Each guard corresponds to a real refusal. A fake that tolerates any of them
    greens a walk the provider rejects, which is worse than no fake at all.
    """
    if table != TABLE_EVENT:
        raise AssertionError(f"unexpected table {table!r}")
    if limit > max_limit:
        raise AssertionError(
            f"limit {limit} exceeds the primitive's {max_limit}-row ceiling; the "
            f"real query_ordered refuses rather than clamping (Gap-C fail-loud)"
        )
    if after is not None and len(after) != len(order_by):
        raise AssertionError(
            f"cursor arity {len(after)} != order_by arity {len(order_by)}; the real "
            f"primitive compiles `after` as ONE row-value comparison and refuses a "
            f"mismatch"
        )
    if any(pair[1] != "asc" for pair in order_by):
        raise AssertionError("this fake only implements ascending order_by")
    return [pair[0] for pair in order_by]


def _select_page(
    rows: list[dict[str, object]],
    *,
    filters: dict[str, object],
    columns: list[str],
    after: tuple[object, ...] | None,
    limit: int,
) -> list[dict[str, object]]:
    """Filter, order, seek past the cursor, and take one page.

    ``is_deleted == 0`` is applied unconditionally — that is ``query_ordered``'s
    ``include_deleted`` default, and note it does NOT match ``None``, which is
    the asymmetry that makes moving a site off ``query_state`` a silent
    semantics change unless the filter is adjusted in the same edit.
    """
    matched = [
        row
        for row in rows
        if all(row.get(key) == value for key, value in filters.items())
        and row.get("is_deleted") == 0
    ]
    matched.sort(key=lambda row: _sort_key(row, columns))
    if after is not None:
        cursor = tuple(after)
        matched = [row for row in matched if _sort_key(row, columns) > cursor]
    return [dict(row) for row in matched[:limit]]


class _FakeOrderedState:
    """A ``query_ordered`` seam honouring the parts of the contract this walk uses.

    Faithful on purpose about four things a looser fake would paper over, each of
    which would turn a real defect green:

    * ``after`` is a ROW-VALUE comparison over the ``order_by`` columns, not a
      per-column AND. A per-column fake accepts a broken cursor.
    * an ``after`` whose arity differs from ``order_by`` RAISES, as the real
      primitive does — a fake that tolerates a mismatch greens a walk the
      provider would refuse.
    * ``include_deleted`` defaults to False, i.e. ``is_deleted == 0``, which does
      NOT match ``None`` — the same asymmetry that makes moving a site from
      ``query_state`` to ``query_ordered`` a silent semantics change.
    * ``limit`` above the primitive's 100-row ceiling RAISES rather than being
      clamped (the Gap-C fail-loud).
    """

    MAX_ORDERED_LIMIT = 100

    def __init__(self, rows: list[dict[str, object]]) -> None:
        self._rows = rows
        self.calls = 0
        self.pages_served: list[int] = []

    def __call__(
        self,
        table: str,
        *,
        filters: dict[str, object],
        order_by: list[list[str]],
        limit: int,
        after: tuple[object, ...] | None = None,
    ) -> list[dict[str, object]]:
        columns = _assert_query_ordered_contract(
            table=table,
            order_by=order_by,
            limit=limit,
            after=after,
            max_limit=self.MAX_ORDERED_LIMIT,
        )
        self.calls += 1
        page = _select_page(
            self._rows, filters=filters, columns=columns, after=after, limit=limit
        )
        self.pages_served.append(len(page))
        return page


def _event(
    *,
    sequence: int,
    ordinal: int,
    created_offset_seconds: float,
    session_id: str = _SESSION,
    is_deleted: int = 0,
) -> dict[str, object]:
    return {
        "id": f"evt_{ordinal:04d}",
        "session_id": session_id,
        "sequence": sequence,
        "created_at": _BASE + timedelta(seconds=created_offset_seconds),
        "is_deleted": is_deleted,
    }


def _fixture() -> list[dict[str, object]]:
    """34 live rows for the session under test, adversarial in three dimensions.

    Layout, chosen so each hazard STRADDLES a page boundary at ``_PAGE = 10``
    rather than sitting comfortably inside one page — a hazard entirely within a
    page is not tested by a paging walk:

    * rows 1-9    sequences 1..9,   distinct ``created_at``   (the benign case)
    * rows 10-12  sequence 10 ×3    — a TIE GROUP across the page-1/2 boundary
    * rows 13-20  sequences 30..37  — a GAP (10 -> 30), nothing is contiguous
    * rows 21-24  identical ``created_at`` — a CREATED_AT TIE group whose
                  ``sequence`` order is the REVERSE of its ``id`` order, so a
                  ``(created_at, id)`` cursor and a ``(sequence, id)`` cursor
                  disagree and the disagreement is detectable
    * rows 25-34  sequences 60..69  — a clean tail past the last hazard

    Plus two rows that must NEVER appear: one soft-deleted, one belonging to a
    different session. Both are the cheap mistakes — a walk that forgets
    ``include_deleted``, or one that drops the ``session_id`` filter when it
    starts paging.
    """
    rows: list[dict[str, object]] = []
    ordinal = 0

    for seq in range(1, 10):  # 9 rows, distinct created_at
        ordinal += 1
        rows.append(_event(sequence=seq, ordinal=ordinal, created_offset_seconds=ordinal))

    for _ in range(3):  # sequence TIE group of 3, straddling a page boundary
        ordinal += 1
        rows.append(_event(sequence=10, ordinal=ordinal, created_offset_seconds=ordinal))

    for seq in range(30, 38):  # GAP: 10 -> 30
        ordinal += 1
        rows.append(_event(sequence=seq, ordinal=ordinal, created_offset_seconds=ordinal))

    # created_at TIE group: one shared timestamp, and sequence DESCENDING as the
    # id ascends — so ordering by (created_at, id) yields these four in exactly
    # the reverse of their sequence order.
    shared = float(ordinal + 1)
    for seq in (53, 52, 51, 50):
        ordinal += 1
        rows.append(_event(sequence=seq, ordinal=ordinal, created_offset_seconds=shared))

    for seq in range(60, 70):  # clean tail
        ordinal += 1
        rows.append(_event(sequence=seq, ordinal=ordinal, created_offset_seconds=ordinal))

    ordinal += 1
    rows.append(_event(sequence=999, ordinal=ordinal, created_offset_seconds=ordinal,
                       is_deleted=1))
    ordinal += 1
    rows.append(_event(sequence=5, ordinal=ordinal, created_offset_seconds=ordinal,
                       session_id=_OTHER_SESSION))
    return rows


def _live_rows() -> list[dict[str, object]]:
    return [
        row
        for row in _fixture()
        if row["is_deleted"] == 0 and row["session_id"] == _SESSION
    ]


def _walk(state: _FakeOrderedState) -> list[dict[str, object]]:
    original = read_module._EVENT_PAGE_ROWS  # noqa: SLF001
    read_module._EVENT_PAGE_ROWS = _PAGE  # noqa: SLF001
    try:
        return list(iter_events_by_sequence(state, session_id=_SESSION))
    finally:
        read_module._EVENT_PAGE_ROWS = original  # noqa: SLF001


# ---------------------------------------------------------------------------
# The fixture must be capable of failing before any green from it means anything
# ---------------------------------------------------------------------------


def test_fixture_straddles_multiple_page_boundaries() -> None:
    """A fixture that cannot reach the page boundary cannot test the cursor.

    Asserted with both numbers named so a future shrink reddens here rather than
    silently voiding every other test in this file.
    """
    live = len(_live_rows())
    _check(
        live > 2 * _PAGE,
        f"fixture spans multiple pages: {live} live rows vs page size {_PAGE} "
        f"({-(-live // _PAGE)} pages)",
    )


def test_fixture_is_adversarial_in_all_three_dimensions() -> None:
    """Live data is benign in all three; a fixture modelled on it tests nothing."""
    rows = _live_rows()
    seqs = [int(cast("int", row["sequence"])) for row in rows]
    created = [row["created_at"] for row in rows]
    _check(len(seqs) != len(set(seqs)), "fixture contains SEQUENCE TIES (tie-break exercised)")
    _check(
        sorted(set(seqs)) != list(range(min(seqs), max(seqs) + 1)),
        "fixture contains SEQUENCE GAPS (no density assumption)",
    )
    _check(
        len(created) != len(set(created)),
        "fixture contains CREATED_AT TIES — the state where the shared helper's "
        "cursor diverges; live data has none, which is why this must",
    )


# ---------------------------------------------------------------------------
# The walk itself
# ---------------------------------------------------------------------------


def test_walk_yields_every_row_exactly_once() -> None:
    state = _FakeOrderedState(_fixture())
    walked = _walk(state)
    ids = [str(row["id"]) for row in walked]
    expected = {str(row["id"]) for row in _live_rows()}
    _check(len(ids) == len(expected), f"walk yielded {len(ids)} rows, expected {len(expected)}")
    _check(len(ids) == len(set(ids)), "walk yielded no duplicate rows")
    _check(set(ids) == expected, "walk yielded exactly the live rows for this session")


def test_walk_is_sequence_ordered() -> None:
    state = _FakeOrderedState(_fixture())
    seqs = [int(cast("int", row["sequence"])) for row in _walk(state)]
    _check(
        seqs == sorted(seqs),
        "walk is sequence-ascending — the order comes from query_ordered, not a "
        "post-hoc Python sort",
    )


def test_tie_group_survives_a_page_boundary() -> None:
    state = _FakeOrderedState(_fixture())
    walked = _walk(state)
    ties = [row for row in walked if int(cast("int", row["sequence"])) == 10]
    _check(len(ties) == 3, f"all 3 rows of the sequence-10 tie group survived (got {len(ties)})")


def test_gaps_do_not_terminate_the_walk() -> None:
    state = _FakeOrderedState(_fixture())
    seqs = [int(cast("int", row["sequence"])) for row in _walk(state)]
    _check(
        max(seqs) == 69,
        f"walk crossed the 10->30 gap and reached the tail (last sequence {max(seqs)})",
    )


def test_soft_deleted_and_foreign_rows_are_excluded() -> None:
    state = _FakeOrderedState(_fixture())
    walked = _walk(state)
    _check(
        all(int(cast("int", row["sequence"])) != 999 for row in walked),
        "soft-deleted row excluded (query_ordered's include_deleted default)",
    )
    _check(
        all(str(row["session_id"]) == _SESSION for row in walked),
        "the session_id filter is applied on EVERY page, not just the first",
    )


def test_short_final_page_terminates() -> None:
    state = _FakeOrderedState(_fixture())
    _walk(state)
    live = len(_live_rows())
    _check(
        state.pages_served[-1] < _PAGE,
        f"final page was short ({state.pages_served[-1]} < {_PAGE}), which is what "
        f"ends the walk",
    )
    _check(
        state.calls == -(-live // _PAGE),
        f"walk issued {state.calls} calls for {live} rows at page {_PAGE} — no "
        f"wasted round-trip after a short page",
    )


def test_exact_multiple_of_page_size_terminates() -> None:
    """The off-by-one a short final page structurally cannot catch.

    When the row count is an exact multiple of the page size, the last full page
    is indistinguishable from a mid-walk one. The walk must ask once more, get an
    empty page, and stop — a walk that assumes a full page means "more" loops
    forever, and one that assumes it means "done" is correct here only by luck.
    """
    rows = [
        _event(sequence=seq, ordinal=seq, created_offset_seconds=seq)
        for seq in range(1, 2 * _PAGE + 1)
    ]
    state = _FakeOrderedState(rows)
    walked = _walk(state)
    _check(len(walked) == 2 * _PAGE, f"exact-multiple fixture walked in full ({len(walked)})")
    _check(
        state.calls == 3,
        f"exact multiple issued {state.calls} calls: 2 full pages + 1 empty probe",
    )


def test_helper_cursor_would_reorder_events() -> None:
    """Executable form of the rule in ``bounded_read.iter_table_rows``'s docstring.

    Ordering the SAME rows by ``(created_at, id)`` — the shared helper's fixed
    cursor — does NOT reproduce sequence order once ``created_at`` ties exist.
    Live data has no such ties, so this is the only place the divergence is
    demonstrated rather than argued. If this test ever passes trivially, the
    created_at-tie group has been removed and the rule has lost its enforcement.
    """
    rows = _live_rows()
    by_helper = [
        int(cast("int", row["sequence"]))
        for row in sorted(rows, key=lambda r: (cast("Any", r["created_at"]), str(r["id"])))
    ]
    _check(
        by_helper != sorted(by_helper),
        "a (created_at, id) cursor REORDERS these events — so iter_table_rows "
        "cannot serve a sequence-ordered contract, and this site's bespoke walk "
        "is not redundant with it",
    )


def test_single_column_cursor_drops_rows() -> None:
    """The mutation that proves the fixture can detect a missing tie-break.

    A cursor on ``sequence`` alone is strictly greater than the last sequence of
    the previous page, so any tie group split by a page boundary loses its
    remainder — silently, with no short page and no error.
    """
    state = _FakeOrderedState(_fixture())
    correct = len(_walk(state))

    after: object | None = None
    seen: list[dict[str, object]] = []
    single = _FakeOrderedState(_fixture())
    while True:
        page = single(
            TABLE_EVENT,
            filters={"session_id": _SESSION},
            order_by=[["sequence", "asc"]],
            limit=_PAGE,
            after=(after,) if after is not None else None,
        )
        if not page:
            break
        seen.extend(page)
        after = page[-1]["sequence"]
        if len(page) < _PAGE:
            break

    _check(
        len(seen) < correct,
        f"single-column (sequence,) cursor lost rows: {len(seen)} vs {correct} — "
        f"this is the failure the id tie-break prevents, and the fixture can see it",
    )


def main() -> int:
    print("=== event_sequence_walk_smoke ===")
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
