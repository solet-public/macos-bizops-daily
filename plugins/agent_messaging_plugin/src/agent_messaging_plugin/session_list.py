"""Filter normalization and hard result bounds for the public fleet roster."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Final

from .schema import (
    LIFECYCLE_IDLE,
    LIFECYCLE_LIVE,
    LIFECYCLE_OVERDUE,
    LIFECYCLE_PARKED,
    LIFECYCLE_SPAWNING,
)
from .session_lifecycle_store import read_managed_sessions_page

if TYPE_CHECKING:
    from ananta.interfaces.state_management_interface import StateManagementInterface

# A fleet roster is an operator-facing context payload, not an export surface.
# Fifty rows is enough for ordinary lane/host/state coordination while staying
# well under the state provider's 100-row page cap. The 250 hard maximum permits
# a deliberately broader filtered survey without allowing a caller to turn this
# verb back into a whole append-mostly-ledger dump.
LIST_SESSIONS_DEFAULT_LIMIT: Final = 50
LIST_SESSIONS_MAX_LIMIT: Final = 250

_FILTER_KEYS: Final = ("lane_id", "work_class", "host", "lifecycle_state")
_LIVE_STATES: Final = (
    LIFECYCLE_SPAWNING,
    LIFECYCLE_LIVE,
    LIFECYCLE_IDLE,
    LIFECYCLE_OVERDUE,
    LIFECYCLE_PARKED,
)


class SessionListError(ValueError):
    """A public list contract violation with its stable processor code."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(message)


def _selected_filters(
    filters: dict[str, Any] | None, *, live_only: bool,
) -> dict[str, Any]:
    selected = {
        key: value
        for key in _FILTER_KEYS
        if filters is not None and (value := filters.get(key)) not in (None, "")
    }
    if not selected and not live_only:
        accepted = ", ".join((*_FILTER_KEYS, "live_only=true"))
        raise SessionListError(
            "filter_required",
            f"list_sessions requires at least one fleet filter ({accepted}); "
            "to list the live fleet pass {\"live_only\": true}.",
        )
    if live_only:
        selected["lifecycle_state"] = _live_lifecycle_filter(
            selected.get("lifecycle_state")
        )
    return selected


def _live_lifecycle_filter(current_filter: object) -> object:
    """Intersect an optional lifecycle predicate with the non-terminal set."""
    if current_filter is None:
        return list(_LIVE_STATES)
    if isinstance(current_filter, (list, tuple)):
        return [value for value in current_filter if value in _LIVE_STATES]
    return current_filter if current_filter in _LIVE_STATES else []


def _validated_limit(limit: object) -> int:
    if (
        isinstance(limit, bool)
        or not isinstance(limit, int)
        or not 1 <= limit <= LIST_SESSIONS_MAX_LIMIT
    ):
        raise SessionListError(
            "invalid_limit",
            f"list_sessions limit must be an integer from 1 through {LIST_SESSIONS_MAX_LIMIT}.",
        )
    return limit


def _validated_cursor(
    after_created_at: object,
    after_id: object,
) -> list[str] | None:
    if after_created_at is None and after_id is None:
        return None
    if (
        not isinstance(after_created_at, str)
        or not after_created_at.strip()
        or not isinstance(after_id, str)
        or not after_id.strip()
    ):
        raise SessionListError(
            "invalid_cursor",
            "list_sessions cursor requires non-empty after_created_at and after_id.",
        )
    return [after_created_at, after_id]


def list_session_rows(
    state: StateManagementInterface,
    filters: dict[str, Any] | None,
    *,
    live_only: bool,
    limit: object,
    after_created_at: object = None,
    after_id: object = None,
) -> dict[str, Any]:
    """Return one bounded roster page and an honest continuation cursor."""
    selected_filters = _selected_filters(filters, live_only=live_only)
    selected_limit = _validated_limit(limit)
    cursor = _validated_cursor(after_created_at, after_id)
    rows, truncated = read_managed_sessions_page(
        state, selected_filters, limit=selected_limit, after=cursor,
    )
    next_cursor = None
    if truncated:
        if not rows:
            raise SessionListError(
                "pagination_stalled",
                "list_sessions found a follow-up page without a row to advance from.",
            )
        last = rows[-1]
        created_at = last.get("created_at")
        row_id = last.get("id")
        if not isinstance(created_at, str) or not created_at or not isinstance(row_id, str) or not row_id:
            raise SessionListError(
                "pagination_stalled",
                "list_sessions cannot advance because a row lacks created_at or id.",
            )
        next_cursor = {"created_at": created_at, "id": row_id}
    return {
        "sessions": rows,
        "returned": len(rows),
        "truncated": truncated,
        "next_cursor": next_cursor,
    }


__all__ = [
    "LIST_SESSIONS_DEFAULT_LIMIT",
    "LIST_SESSIONS_MAX_LIMIT",
    "SessionListError",
    "list_session_rows",
]
