"""Stateless helper functions for ACT-R memory plugin."""

import datetime
from datetime import UTC
from typing import Any

from ananta.error_handling import FrameworkError


def acting_session_id(
    params: dict[str, Any],
    state: dict[str, Any],
) -> str:
    """Resolve the ACTING session for a focus-buffer operation (JOS-02 §5.2).

    The server-built ``state`` dict is authoritative (the platform stamps the
    action's own session into it at injection time); the engine-merged
    ``params`` value is the fallback for paths that thread context through
    params only. A caller-supplied override never wins over ``state``, and a
    session-less action cannot reach the focus buffer at all.
    """
    session_id = str(state.get("session_id") or params.get("session_id") or "")
    if not session_id:
        raise FrameworkError(
            message=(
                "focus-buffer operation requires an acting session; neither "
                "state nor params carried a session_id"
            ),
            error_code="memory.session_required",
        )
    return session_id


def build_response(
    status: str,
    data: dict[str, Any],
    error: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build standardized plugin response."""
    return {
        "action_status": status,
        "timestamp": datetime.datetime.now(UTC).isoformat(),
        "data": data,
        "actions": [],
        "error": error,
    }
