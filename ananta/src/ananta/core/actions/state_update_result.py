"""Read the row count out of a ``StateManagementInterface.update_state`` result.

The contract (``ananta/src/ananta/core/state/flow_runtime_graph.py`` module
docstring, built by the postgres state plugin's ``create_success_result``) is
``{action_status: "completed", data: {namespace, result: {updated: <int>}}}``.
The count sits one layer below ``data``; reading ``data.updated`` instead finds
nothing on the live seam, which is how ``fail_action_event`` shipped to review
reporting every successful resolution as an error (review uev_60f9a739, B1).
Every guarded ``action_events`` write in ``ananta.core.actions`` reads its count
through this one function so the shape is decided once.
"""

from __future__ import annotations

from ananta.core.domain.enums import ActionStatus
from ananta.core.domain.types import ActionResult

_COMPLETED_STATUSES: frozenset[str] = frozenset({ActionStatus.COMPLETED.value})


class StateUpdateResultError(RuntimeError):
    """The update did not complete, or its result envelope is malformed."""


def updated_row_count(result: ActionResult, *, what: str) -> int:
    """Return ``data.result.updated`` after checking ``action_status``.

    ``what`` names the write for the error message. Raises
    ``StateUpdateResultError`` when the state layer reported anything but a
    completed update or the envelope lacks an integer count -- never guesses.
    """
    status = result.get("action_status")
    if str(status) not in _COMPLETED_STATUSES:
        raise StateUpdateResultError(
            f"{what}: update_state did not complete (action_status={status!r}, "
            f"error={result.get('error')!r})",
        )
    data = result.get("data")
    inner = data.get("result") if isinstance(data, dict) else None
    updated = inner.get("updated") if isinstance(inner, dict) else None
    if isinstance(updated, bool) or not isinstance(updated, int):
        raise StateUpdateResultError(
            f"{what}: update_state result has no integer data.result.updated: {result!r}",
        )
    return updated


__all__ = ["StateUpdateResultError", "updated_row_count"]
