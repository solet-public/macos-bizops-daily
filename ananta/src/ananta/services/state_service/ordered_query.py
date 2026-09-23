"""Shared contract + hardening for the ``query_ordered`` state primitive.

``query_ordered`` is the one operator-approved widening of the
``StateManagementInterface`` surface (2026-06-19): an ordered, bounded,
tie-safe read. Raw SQL stays *inside* the postgres/rds provider
``select_ordered`` implementations; every caller goes through the
high-level primitive. This module is the single audited home for the
Codex-required surface hardening so the postgres provider, the rds
provider, and the in-memory bootstrap storage all agree:

* **Identifier safety** — ``table`` and ``order_by`` column names must
  match :data:`_IDENTIFIER_RE` (lowercase snake_case); the providers
  additionally quote them via ``psycopg.sql.Identifier`` (never string
  interpolation). A non-matching name is rejected here, before any SQL.
* **Direction enum** — every ``order_by`` direction is a strict
  ``asc``/``desc`` and they must all match (a single direction is
  required for the row-value ``after`` cursor comparison).
* **Composite-only ordering** — at least two order columns, so the final
  column can act as a total-order tie-break (a single-column order is not
  tie-safe; equal leading values would page non-deterministically).
* **Capped positive limit, fail-loud over the cap (Gap-C)** — a request
  at or under ``_MAX_ORDERED_LIMIT`` is used as-is; a request OVER the cap
  is REFUSED (``OrderedQueryError``) unless the caller passes
  ``unbounded=True`` to consciously opt into the larger page. The old
  silent clamp-to-cap is gone: a caller asking for more rows than the cap
  can no longer lose the overflow off the end without consenting to the
  scan.
* **AND-range comparison filters (Gap-A)** — alongside scalar equality,
  list ``= ANY``, and ``is_null``/``is_not_null``, a filter value may be
  ``{"op": "lt"|"lte"|"gt"|"gte", "value": X}`` for a half-open range
  predicate. The SQL providers compile it in ``_build_filter_clauses``;
  the in-memory matcher below mirrors it.
* **Default ``is_deleted = 0``** — soft-deleted rows are excluded unless
  the caller opts in with ``include_deleted=True`` (avoids the
  delete-to-prune re-emit trap for every future caller).

The canonical sort representation is a UTC ISO-8601 string: the postgres
provider already serializes ``created_at`` via ``isoformat()`` on read,
ISO-8601 strings sort lexicographically in timestamp order, and the
opaque inbox cursor is encoded from the same ISO string — so the SQL
path, the in-memory path, and the cursor encoder all compare like for
like (no created_at type mismatch across backends).
"""

from __future__ import annotations

import operator
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

# Same cap discipline as ``_MAX_LIST_LIMIT`` in
# ``ananta/llm/session_ledger`` / ``agent_messaging`` repositories.
_MAX_ORDERED_LIMIT = 100

# Lowercase snake_case identifier — the platform's column-naming
# convention. Rejecting anything else is the first injection defense
# (the providers quote via ``sql.Identifier`` as defense in depth).
_IDENTIFIER_RE = re.compile(r"^[a-z_][a-z0-9_]*$")

_ASC = "asc"
_DESC = "desc"
_VALID_DIRECTIONS = frozenset({_ASC, _DESC})


class OrderedQueryError(ValueError):
    """Raised when a ``query_ordered`` payload violates the contract."""


@dataclass(frozen=True, slots=True)
class OrderedQuerySpec:
    """A validated, hardened ``query_ordered`` request.

    Every field is post-validation: ``table`` / ``order_columns`` are
    identifier-safe, ``direction`` is a strict enum value shared by all
    order columns, ``limit`` is capped positive, and ``after`` (when
    present) is a tuple whose arity matches ``order_columns``.
    """

    table: str
    filters: dict[str, object]
    order_columns: tuple[str, ...]
    direction: str
    limit: int
    after: tuple[object, ...] | None
    include_deleted: bool


def normalize_sort_value(value: object) -> str:
    """Canonicalize a sort/cursor value to a comparable string.

    A ``datetime`` becomes its ISO-8601 rendering (matching the postgres
    provider's on-read serialization); everything else is stringified.
    This is what makes a raw-``datetime`` in-memory row and an ISO-string
    cursor compare consistently.
    """
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


def _natural_key_value(value: object) -> Any:
    """Type-faithful comparable for the in-memory sort/cursor comparison.

    Distinct from :func:`normalize_sort_value` (which canonicalizes to a
    STRING for opaque cursor encoding + the role-inbox merge): here a NUMERIC
    value stays numeric, so multi-digit integers order by VALUE (``10 > 9``),
    NOT lexically (``'10' < '9'`` — the bug Codex caught: a ``cursor``-ordered
    page silently lost rows at the 9→10 digit boundary on the in-memory /
    bootstrap backend). A ``datetime`` still renders to its ISO-8601 form, so a
    raw-``datetime`` in-memory row and an ISO-string cursor for the SAME column
    compare like-for-like; datetime/ISO + string ordering is preserved EXACTLY
    (only the numeric branch is new). Within one order column every row + the
    cursor share the column's type, so each position compares like-vs-like.
    """
    if isinstance(value, (int, float)):  # bool is an int subclass — numeric
        return value
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


def _validate_identifier(name: object, *, role: str) -> str:
    if not isinstance(name, str) or not _IDENTIFIER_RE.match(name):
        raise OrderedQueryError(
            f"query_ordered: {role} {name!r} is not a valid lowercase "
            "snake_case identifier",
        )
    return name


def _parse_order_by(raw: object) -> tuple[tuple[str, ...], str]:
    if not isinstance(raw, (list, tuple)) or len(raw) < 2:
        raise OrderedQueryError(
            "query_ordered: order_by must be a composite of at least two "
            "(column, direction) pairs (a single column is not tie-safe)",
        )
    columns: list[str] = []
    directions: set[str] = set()
    for pair in raw:
        if not isinstance(pair, (list, tuple)) or len(pair) != 2:
            raise OrderedQueryError(
                f"query_ordered: order_by entry {pair!r} must be a "
                "(column, direction) pair",
            )
        column, direction = pair
        columns.append(_validate_identifier(column, role="order_by column"))
        if direction not in _VALID_DIRECTIONS:
            raise OrderedQueryError(
                f"query_ordered: order_by direction {direction!r} must be "
                "'asc' or 'desc'",
            )
        directions.add(direction)
    if len(directions) != 1:
        raise OrderedQueryError(
            "query_ordered: all order_by directions must match (the "
            "row-value 'after' cursor compares a single direction)",
        )
    return tuple(columns), next(iter(directions))


def parse_ordered_query(data: dict[str, object]) -> OrderedQuerySpec:
    """Validate + harden a raw ``query_ordered`` payload into a spec.

    Raises :class:`OrderedQueryError` on any contract violation so a
    malformed request fails fast and loud rather than degrading into an
    arbitrary or injectable query.
    """
    table = _validate_identifier(data.get("table"), role="table")

    filters_raw = data.get("filters", {})
    if not isinstance(filters_raw, dict):
        raise OrderedQueryError("query_ordered: filters must be a dict")
    filters: dict[str, object] = {
        _validate_identifier(key, role="filter column"): value
        for key, value in filters_raw.items()
    }

    order_columns, direction = _parse_order_by(data.get("order_by"))

    limit_raw = data.get("limit")
    if not isinstance(limit_raw, int) or isinstance(limit_raw, bool):
        raise OrderedQueryError("query_ordered: limit must be an int")
    # Gap-C: fail loud over the cap rather than silently clamping; a caller
    # must pass unbounded=True to consciously opt into a larger page. This
    # closes the silent-truncation hole (e.g. a 1..500 verb whose request was
    # quietly cut to 100). The lower bound still floors at 1.
    unbounded = bool(data.get("unbounded", False))
    if limit_raw > _MAX_ORDERED_LIMIT and not unbounded:
        raise OrderedQueryError(
            f"query_ordered: limit {limit_raw} exceeds the cap "
            f"{_MAX_ORDERED_LIMIT}; pass unbounded=True to opt into a larger "
            "page (silent truncation is refused — the caller must consent to "
            "the larger scan).",
        )
    limit = max(1, limit_raw)

    after = _parse_after(data.get("after"), arity=len(order_columns))

    include_deleted = bool(data.get("include_deleted", False))

    return OrderedQuerySpec(
        table=table,
        filters=filters,
        order_columns=order_columns,
        direction=direction,
        limit=limit,
        after=after,
        include_deleted=include_deleted,
    )


def _to_naive_utc(value: object) -> object:
    """Convert a tz-aware ``datetime`` to naive UTC; pass everything else.

    The platform stores timestamps as ``timestamp without time zone`` in
    naive UTC (the TZ-storage seam). Normalizing the cursor's timestamp
    value here — once, in the shared parse path — means both the postgres
    and rds providers bind a type-matched naive timestamp for the
    row-value comparison without each plugin needing its own tz strip, so
    the two provider impls stay byte-for-byte lockstep.
    """
    if isinstance(value, datetime) and value.tzinfo is not None:
        return value.astimezone(UTC).replace(tzinfo=None)
    return value


def _parse_after(raw: object, *, arity: int) -> tuple[object, ...] | None:
    # Preserve each value's natural Python type (a ``datetime`` for a
    # timestamp column) so the SQL providers bind a type-correct param for
    # the row-value comparison; tz-aware timestamps are normalized to naive
    # UTC (the storage seam). The in-memory comparator normalizes both
    # sides to the canonical string form at compare time.
    if raw is None:
        return None
    if not isinstance(raw, (list, tuple)) or len(raw) != arity:
        raise OrderedQueryError(
            f"query_ordered: after cursor must be a {arity}-tuple matching "
            "order_by arity",
        )
    return tuple(_to_naive_utc(value) for value in raw)


def _row_is_live(record: dict[str, object], *, include_deleted: bool) -> bool:
    if include_deleted:
        return True
    flag = record.get("is_deleted", 0)
    return not flag


# AND-range comparison ops for the in-memory matcher — mirrors the SQL
# providers' ``_COMPARISON_OPERATORS``. Keyed by the structured filter op.
_COMPARISON_FUNCS: dict[str, Callable[[Any, Any], bool]] = {
    "lt": operator.lt,
    "lte": operator.le,
    "gt": operator.gt,
    "gte": operator.ge,
}


def _filter_matches(cell: object, spec: object) -> bool:
    """Mirror the SQL ``_build_filter_clauses`` grammar for one column in memory.

    Keeps the in-memory bootstrap backend in agreement with the postgres/rds
    providers (the module invariant): scalar → equality; list/tuple →
    ``= ANY`` membership; ``{"op": "is_null"|"is_not_null"}`` → NULL test;
    ``{"op": "lt"|"lte"|"gt"|"gte", "value": X}`` → AND-range compare (Gap-A).
    A NULL cell on the left of a comparison is UNKNOWN → excluded (SQL
    three-valued logic). Comparison operands normalize via
    :func:`_natural_key_value` so numerics stay numeric (value order, not
    lexical) and a ``datetime`` vs an ISO-string compare like-for-like (the
    cursor-path convention). Fails loud on an unknown op — parity with the
    providers' ``ValueError``.
    """
    if isinstance(spec, dict):
        op = spec.get("op")
        if op == "is_null":
            return cell is None
        if op == "is_not_null":
            return cell is not None
        if op in _COMPARISON_FUNCS:
            if "value" not in spec:
                raise OrderedQueryError(
                    f"query_ordered: filter op {op!r} requires a 'value'",
                )
            if cell is None:
                return False
            return _COMPARISON_FUNCS[op](
                _natural_key_value(cell), _natural_key_value(spec["value"]),
            )
        raise OrderedQueryError(
            f"query_ordered: unsupported filter op {op!r}; expected 'is_null', "
            "'is_not_null', 'lt', 'lte', 'gt', or 'gte'",
        )
    if isinstance(spec, (list, tuple)):
        return cell in spec
    return cell == spec


def _matches_filters(
    record: dict[str, object], filters: dict[str, object],
) -> bool:
    return all(
        _filter_matches(record.get(key), value) for key, value in filters.items()
    )


def _sort_key(
    record: dict[str, object], order_columns: tuple[str, ...],
) -> tuple[Any, ...]:
    return tuple(
        _natural_key_value(record.get(column)) for column in order_columns
    )


def _after_keeps(
    record: dict[str, object],
    order_columns: tuple[str, ...],
    cursor: tuple[Any, ...],
    *,
    descending: bool,
) -> bool:
    key = _sort_key(record, order_columns)
    return key < cursor if descending else key > cursor


def apply_ordered_query_in_memory(
    records: list[dict[str, object]], spec: OrderedQuerySpec,
) -> list[dict[str, object]]:
    """Apply an :class:`OrderedQuerySpec` to in-memory rows.

    Mirrors the SQL semantics for the bootstrap storage strategy: filter
    by equality (+ ``is_deleted`` default), order by the composite key in
    the requested direction with the canonical string normalization,
    apply the tie-safe ``after`` cursor, then take ``limit``. Both the row
    keys and the cursor go through :func:`_natural_key_value` (numeric stays
    numeric so an int cursor orders by VALUE; ``datetime``→ISO so a datetime
    cursor and an ISO-string row compare like for like).
    """
    descending = spec.direction == _DESC

    selected = [
        record
        for record in records
        if _row_is_live(record, include_deleted=spec.include_deleted)
        and _matches_filters(record, spec.filters)
    ]
    selected.sort(
        key=lambda record: _sort_key(record, spec.order_columns),
        reverse=descending,
    )

    if spec.after is not None:
        cursor = tuple(_natural_key_value(value) for value in spec.after)
        selected = [
            record
            for record in selected
            if _after_keeps(
                record, spec.order_columns, cursor, descending=descending,
            )
        ]

    return selected[: spec.limit]


__all__ = [
    "OrderedQueryError",
    "OrderedQuerySpec",
    "apply_ordered_query_in_memory",
    "normalize_sort_value",
    "parse_ordered_query",
]
