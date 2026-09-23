"""Completeness check for a call site that declares its own ``read_state`` bound.

Companion to :mod:`ananta.services.state_service.read_bounds`, which owns the
PROVIDER-side cap. This module owns the other half: what a CALL SITE must do
when it passes an explicit ``limit`` in order to read a whole small table.

## Why an explicit limit is not, by itself, safe

``resolve_read_limit`` deliberately treats the two cases differently:

* **no limit** → the provider fetches ``cap + 1`` rows and REFUSES if that many
  come back. Loud. A caller can never receive a silent prefix.
* **explicit limit** → the provider fetches exactly ``limit`` rows and
  ``overflow_is_error`` is ``False``. Its docstring states the reasoning: "the
  caller asked for exactly N rows and N rows is a complete answer to that
  question, not a truncation."

That reasoning is correct for a top-N caller, which is asking a *ranking*
question. It is exactly wrong for a load-the-whole-registry caller, which is
asking a *completeness* question — for that caller, receiving exactly ``limit``
rows is the one outcome that means "you probably did not get everything."

So "add an explicit limit" is not on its own a fix for an unbounded whole-table
read: applied naively it DOWNGRADES the site from a loud refusal at the cap to a
silent prefix at the limit. It moves the failure closer and makes it quieter —
the opposite of the intent. The bound must be paired with a check that the
ceiling was not reached, which is what :func:`assert_within_ceiling` is for.

## What a ceiling means

A ceiling is a written-down claim about a table: *this table cannot reach N rows
in normal operation, and here is why.* The call site states the reason in a
comment; this function makes the claim self-enforcing. If the table ever does
reach the ceiling, the caller gets an error naming the table, the ceiling, and
the fact that the result would have been a prefix — rather than proceeding on
data that is quietly incomplete.

An unwritten assumption about table size is what produced the 2026-08-15
outage. A written assumption that fails loudly is the remedy; a written
assumption that fails silently is the same bug with better documentation.

## The other half: when the table is NOT small by construction

:func:`assert_within_ceiling` serves a call site that can honestly claim its
table stays small. A call site that must read every row of a table which
genuinely grows cannot make that claim, and no ceiling will make it true.
:func:`iter_table_rows` is that site's answer: it walks the whole result set as
tie-safe ``query_ordered`` pages, so the row bound applies per page instead of
to the whole read, and the caller still sees every row.

The three answers to "my read is over the cap" are therefore:

* the table is small by construction → explicit ``limit`` +
  :func:`assert_within_ceiling`;
* the table grows → :func:`iter_table_rows`;
* the table cannot grow AND the whole scan is genuinely wanted →
  ``unbounded=True``, which is CONSENT TO A FULL SCAN and not a bound. It is
  also misnamed: it means "my declared bound exceeds the default", not "no
  bound". Reach for it last.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol, TypeVar

if TYPE_CHECKING:
    from collections.abc import Iterator

_RecordT = TypeVar("_RecordT")


class OrderedReader(Protocol):
    """The one method :func:`iter_table_rows` actually calls.

    Structural on purpose. Typing the walk against the full
    ``StateManagementInterface`` was an over-constraint that blocked a legitimate
    caller on 2026-08-15 PDT / 2026-08-16 UTC: ``FlowRuntimeGraph`` holds a
    ``StateServiceProtocol``, whose ``query_ordered`` is signature-identical, and
    pyright correctly refused it. A helper should ask for what it uses, not for
    the largest interface that happens to provide it.

    The return is ``object`` rather than ``ActionResult`` because this module
    validates the envelope itself (see :func:`_page_records`) instead of trusting
    a declared type — and that keeps a state-service helper free of a dependency
    on core's domain types.
    """

    def query_ordered(self, namespace: str, data: dict[str, object]) -> object: ...

# One page. Deliberately equal to ``read_bounds.MAX_READ_ROWS`` in value but NOT
# derived from it: the binding constraint here is ``ordered_query``'s
# ``_MAX_ORDERED_LIMIT``, which REFUSES a larger page unless ``unbounded=True``
# is passed. They are independent constants that happen to agree today; deriving
# one from the other would silently break this walk the day either moves.
_PAGE_ROWS = 100

# The tie-safe composite the walk pages on. Two columns, not one: a
# ``created_at``-only cursor splits a same-timestamp group at a page boundary and
# silently drops the rest of it, which is the exact failure this whole module
# exists to prevent. Both columns are added to every table by the standardizer,
# and both are immutable after insert — so a caller that WRITES to rows as it
# walks (the one-shot backfills do) cannot move a row across the cursor.
_ORDER_BY = [["created_at", "asc"], ["id", "asc"]]

_COL_CREATED_AT = "created_at"
_COL_ID = "id"
_COMPLETED = "completed"


class ReadCeilingError(RuntimeError):
    """Raised when a whole-table read returned as many rows as its ceiling.

    Mirrors :class:`ananta.services.state_service.read_bounds.ReadBoundError` in
    spirit — a bound was violated — but names the CALL SITE's declared ceiling
    rather than the provider's global cap.
    """


class PagedReadError(RuntimeError):
    """Raised when a page of :func:`iter_table_rows` could not be read.

    Separate from :class:`ReadCeilingError` because the two mean opposite
    things: a ceiling error says the walk worked and the table is bigger than
    the call site declared, this says the walk itself did not complete. Merging
    them would let a provider fault be read as a size problem and "fixed" by
    raising a number.
    """


def assert_within_ceiling(
    records: list[_RecordT],
    *,
    table: str,
    ceiling: int,
    reason: str,
) -> list[_RecordT]:
    """Return ``records`` unchanged, or raise if the read hit its ceiling.

    Call this at every site that reads a whole table with an explicit ``limit``
    because the table is small by construction. Pass the same integer as the
    query's ``limit``.

    Args:
        records: The rows the read returned.
        table: Table name, for the error message.
        ceiling: The ``limit`` the call site passed. Reaching it is the failure
            condition — at exactly this many rows the result is indistinguishable
            from a truncated one, so it is treated as truncated.
        reason: The call site's written justification for the ceiling (why the
            table cannot legitimately grow this large). Repeated back in the
            error so whoever hits it sees the assumption that just broke, not
            merely the number that bounded it.

    Returns:
        ``records``, unchanged, when the read was complete.

    Raises:
        ReadCeilingError: If ``len(records) >= ceiling``.
    """
    if len(records) >= ceiling:
        raise ReadCeilingError(
            f"read_state on table {table!r} returned {len(records)} rows against a "
            f"declared ceiling of {ceiling} — the result may be a PREFIX, not the "
            f"whole table, so it is refused rather than used. The ceiling was "
            f"justified as: {reason} That assumption no longer holds. Either the "
            f"table grew past what this call site was designed for (raise the "
            f"ceiling and re-justify it, or stop reading the table whole and "
            f"filter/paginate with 'query_ordered' instead), or rows are "
            f"accumulating that something else should have been deleting."
        )
    return records


def _page_records(result: object, *, table: str) -> list[dict[str, object]]:
    """Rows from a COMPLETED ``query_ordered`` envelope, or raise.

    A state op does NOT raise on a provider error — it returns an
    ``ActionResult`` whose ``action_status`` is not ``completed``. Reading that
    as an empty page would end the walk early and report a PREFIX as the whole
    table, which is the failure this module exists to prevent, so a non-completed
    envelope is fatal here.

    The rows live at ``data.records``. That path is checked, not assumed: three
    repaired call sites in the same programme navigated ``data["result"]``, a key
    ``read_state`` has never returned, and so discarded the rows they had just
    fetched while still looking healthy.
    """
    if not isinstance(result, dict):
        raise PagedReadError(
            f"paged read of table {table!r}: state result is not a dict: {result!r}",
        )
    status = str(result.get("action_status", ""))
    if status != _COMPLETED:
        raise PagedReadError(
            f"paged read of table {table!r} did not complete "
            f"(action_status={status!r}): {result!r}",
        )
    data = result.get("data")
    records = data.get("records") if isinstance(data, dict) else None
    if records is None:
        return []
    if not isinstance(records, list):
        raise PagedReadError(
            f"paged read of table {table!r}: data.records is not a list: {records!r}",
        )
    return [row for row in records if isinstance(row, dict)]


def iter_table_rows(
    state: OrderedReader,
    *,
    namespace: str,
    table: str,
    filters: dict[str, object],
    ceiling: int,
    reason: str,
    include_deleted: bool = False,
) -> Iterator[dict[str, object]]:
    """Yield every row matching ``filters``, walked as tie-safe ordered pages.

    The repair for a call site that needs EVERY row of a table which genuinely
    grows. The provider's row bound applies per page rather than to the whole
    read, so the walk is complete at any table size without consenting to a full
    scan — and rows are yielded as they arrive, so only one page is ever in
    memory. Materialising the whole result with ``list()`` throws that second
    property away; do it only when the caller genuinely needs random access.

    Safe to WRITE to rows while iterating. The cursor is ``(created_at, id)``,
    both immutable after insert, so an update cannot move a row across it — and
    because the cursor is a row value rather than an offset, rows dropping OUT of
    ``filters`` behind the cursor cannot shift the rows ahead of it either. That
    is what makes the one-shot backfills' read-and-flip loop correct here.

    **WHEN THIS IS THE WRONG TOOL.** Not the right tool when the caller's
    contract is an order other than ``(created_at, id)`` — ``sequence`` on an
    event log, a rank, a score. The reason is NOT that this yields a different
    order; on the ledger's ``event`` table, measured 2026-08-15, ``(created_at,
    id)`` and ``sequence`` order coincide exactly (2,174 distinct ``created_at``
    values over 2,174 rows, largest tie group 1). The reason is that it cannot
    **promise** that order. It orders by insertion; where the two coincide, the
    coincidence is an unstated invariant about how rows were inserted — one
    linear ingest pass with monotonically increasing ``created_at`` — and a
    re-ingest or backfill breaks it by writing ``created_at`` at import
    wall-clock while ``sequence`` stays the vendor ordinal. Satisfying a
    different order through this helper means materialising the walk and
    re-sorting, which defeats it outright; that reason needs no invariant at all.

    Such a caller wants its own keyset walk on ``(its_column, id)``. Keep the
    ``id`` tie-break: a single-column cursor splits an equal-valued group at a
    page boundary and silently drops the remainder, which is the failure this
    module exists to prevent, and it is no less true outside this function.

    (Parameterising the leading cursor column here would collapse those two
    implementations into one and is worth doing — deliberately not done in the
    2026-08-15 boot repair, which was on a deploy's critical path with a second
    lane already mid-edit against this signature.)

    Args:
        state: The state interface to page through.
        namespace: Owning namespace of ``table``.
        table: Table to walk.
        filters: Equality/op filter grammar, same as ``query_state``. Applied by
            the provider on every page — push the predicate down here rather than
            filtering in Python whenever the grammar can express it.
        ceiling: Refuse once the walk EXCEEDS this many rows. This is a written
            claim about how large a whole-table walk this call site is designed
            to perform — not a claim that the table is small.
        reason: Why that ceiling holds, repeated back in the error so whoever
            hits it sees the assumption that broke rather than a number to raise.
        include_deleted: When ``False`` (default), soft-deleted rows are
            excluded — the same predicate as a ``{"is_deleted": 0}`` filter,
            spelled the way ``query_ordered`` expects. Do not pass both.

    Yields:
        Each matching row, in ``(created_at, id)`` ascending order.

    Raises:
        PagedReadError: A page did not complete, or a row lacks a cursor column.
        ReadCeilingError: The walk exceeded ``ceiling`` rows.
    """
    after: list[object] | None = None
    seen = 0
    while True:
        query: dict[str, object] = {
            "table": table,
            "filters": filters,
            "order_by": _ORDER_BY,
            "limit": _PAGE_ROWS,
            "include_deleted": include_deleted,
        }
        if after is not None:
            query["after"] = after
        records = _page_records(state.query_ordered(namespace, query), table=table)
        if not records:
            return
        for record in records:
            seen += 1
            # STRICTLY GREATER, where assert_within_ceiling uses >=. The
            # difference is not an oversight and matters: there, reaching the
            # limit makes a single read indistinguishable from a truncated one,
            # so the boundary itself is suspect. Here the walk pages until a
            # short page proves the end, so the count is known exactly and a
            # table of exactly `ceiling` rows was read in full.
            if seen > ceiling:
                raise ReadCeilingError(
                    f"paged walk of table {table!r} passed its declared ceiling "
                    f"of {ceiling} rows, so it was refused rather than run to "
                    f"completion. The ceiling was justified as: {reason} That "
                    f"assumption no longer holds. Either raise it and "
                    f"re-justify it, narrow 'filters' so the provider ships "
                    f"fewer rows, or stop walking the table whole — at this "
                    f"size the work probably belongs in a set-based statement "
                    f"evaluated in the database rather than a row loop.",
                )
            yield record
        if len(records) < _PAGE_ROWS:
            return
        last = records[-1]
        if _COL_CREATED_AT not in last or _COL_ID not in last:
            raise PagedReadError(
                f"paged read of table {table!r}: a row is missing the cursor "
                f"columns {_COL_CREATED_AT!r}/{_COL_ID!r}, so the walk cannot "
                f"advance without risking skipped or repeated rows: {last!r}",
            )
        after = [last[_COL_CREATED_AT], last[_COL_ID]]


__all__ = [
    "OrderedReader",
    "PagedReadError",
    "ReadCeilingError",
    "assert_within_ceiling",
    "iter_table_rows",
]
