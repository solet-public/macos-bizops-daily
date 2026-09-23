from datetime import UTC, datetime
from enum import Enum

from ananta.core.domain.enums import ActionStatus


class AsyncJobStatus(Enum):
    """Status values for async jobs. Matches JobStatus and core__job table constraint.

    Database CHECK constraint: status IN ('queued', 'processing', 'completed', 'cancelled', 'error')
    """

    QUEUED = "queued"
    PENDING = "queued"  # Alias for backwards compatibility
    PROCESSING = "processing"
    COMPLETED = "completed"
    ERROR = "error"
    FAILED = "error"  # Alias for backwards compatibility
    CANCELLED = "cancelled"


def normalize_status(status_value: object) -> str:
    value = getattr(status_value, "value", status_value)
    return str(value).lower()


def is_status_match(actual_status: object, expected_status: object) -> bool:
    return normalize_status(actual_status) == normalize_status(expected_status)


def create_success_response(
    data: dict[str, object] | None = None, actions: list[dict[str, object]] | None = None
) -> dict[str, object]:
    return {
        "status": ActionStatus.COMPLETED.value,
        "action_status": ActionStatus.COMPLETED.value,
        "data": data or {},
        "actions": actions or [],
        "error": None,
        "timestamp": datetime.now(UTC).isoformat(),
    }


def create_error_response(
    error_message: str, error_code: str = "unknown", data: dict[str, object] | None = None
) -> dict[str, object]:
    return {
        "status": ActionStatus.ERROR.value,
        "action_status": ActionStatus.ERROR.value,
        "data": data or {},
        "actions": [],
        "error": {"message": error_message, "code": error_code},
        "timestamp": datetime.now(UTC).isoformat(),
    }
