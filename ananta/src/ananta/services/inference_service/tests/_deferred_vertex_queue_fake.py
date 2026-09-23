"""Shared ordered-read behavior for isolated deferred-vertex smoke fakes."""

from __future__ import annotations

from typing import cast


def query_ordered_rows(
    rows: list[dict[str, object]], data: dict[str, object],
) -> dict[str, object]:
    """Return one bounded, cursor-respecting page of live queue rows."""
    filters = data.get("filters")
    expected: dict[str, object] = {}
    if isinstance(filters, dict):
        for key, value in cast(dict[object, object], filters).items():
            if isinstance(key, str):
                expected[key] = value
    selected = _matching_live_rows(rows, expected, data.get("include_deleted") is True)
    selected.sort(key=_cursor)
    after = data.get("after")
    if isinstance(after, list):
        after_row = cast(list[object], after)
        if len(after_row) == 2:
            selected = [
                row for row in selected
                if _cursor(row) > (str(after_row[0]), str(after_row[1]))
            ]
    limit = data.get("limit")
    page = selected[:limit] if isinstance(limit, int) else selected
    return {"action_status": "completed", "data": {"records": page}}


def _matching_live_rows(
    rows: list[dict[str, object]], expected: dict[str, object], include_deleted: bool,
) -> list[dict[str, object]]:
    return [
        dict(row) for row in rows
        if (include_deleted or row.get("is_deleted") != 1)
        and all(row.get(key) == value for key, value in expected.items())
    ]


def _cursor(row: dict[str, object]) -> tuple[str, str]:
    return str(row["created_at"]), str(row["id"])
