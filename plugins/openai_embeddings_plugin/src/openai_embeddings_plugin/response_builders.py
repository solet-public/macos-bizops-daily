"""Response builder helpers for the OpenAI embeddings plugin."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from ananta.core.domain.enums import ActionStatus, ErrorSeverity

if TYPE_CHECKING:
    from ananta.core.domain.types import ActionResult, ErrorDetail

    from .constants import ErrorCode


def now() -> str:
    """Return current timestamp as ISO string."""
    return datetime.now(UTC).isoformat()


def error_result(
    code: ErrorCode, message: str, details: dict[str, Any] | None = None
) -> ActionResult:
    """Build error ActionResult."""
    error: ErrorDetail = {
        "type": "OpenAIEmbeddingsError",
        "code": code.value,
        "message": message,
        "details": details or {},
        "severity": ErrorSeverity.ERROR.value,
        "timestamp": now(),
    }
    return {
        "action_status": ActionStatus.ERROR.value,
        "error": error,
        "timestamp": now(),
    }


def success_result(data: dict[str, Any]) -> ActionResult:
    """Build success ActionResult."""
    return {
        "action_status": ActionStatus.COMPLETED.value,
        "data": {"result": data},
        "actions": [],
        "timestamp": now(),
    }
