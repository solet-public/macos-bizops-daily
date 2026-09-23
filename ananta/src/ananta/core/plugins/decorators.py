"""Decorators for plugin lifecycle management.

This module provides decorators for ServicePlugin lifecycle methods to ensure
proper error handling, ActionResult formatting, and error routing metadata.
"""

from collections.abc import Callable
from datetime import UTC, datetime
from functools import wraps
from typing import Any, Literal, TypeVar

from ananta.core.domain.enums import ActionStatus
from ananta.core.domain.types import ActionResult

F = TypeVar("F", bound=Callable[..., Any])


def service_lifecycle(operation: Literal["start", "stop"]) -> Callable[[F], F]:
    """Decorator for service lifecycle methods.

    Ensures:
    - Returns ActionResult
    - Errors formatted properly
    - Error routing metadata attached
    - Timestamps added
    - action_status set correctly

    THIS decorator documentation is for developers.
    Use @platform_process for inference-facing process definitions.

    Args:
        operation: Lifecycle operation type ("start" or "stop")

    Returns:
        Decorated function that returns ActionResult

    Example:
        ```python
        @service_lifecycle(operation="start")
        async def start_services(self) -> ActionResult:
            # Setup logic...
            return {
                "action_status": ActionStatus.COMPLETED.value,
                "data": {"message": "Service started"},
                "actions": [],
                "error": None,
            }
        ```
    """

    def decorator(func: F) -> F:
        @wraps(func)
        async def wrapper(self: Any, *args: Any, **kwargs: Any) -> ActionResult:
            try:
                result: ActionResult = await func(self, *args, **kwargs)

                # Add timestamp if not present
                if "timestamp" not in result:
                    result["timestamp"] = datetime.now(UTC).isoformat()

                # Ensure required fields exist
                if "action_status" not in result:
                    result["action_status"] = ActionStatus.COMPLETED.value
                if "data" not in result:
                    result["data"] = {}
                if "actions" not in result:
                    result["actions"] = []
                if "error" not in result:
                    result["error"] = None

                # Add error routing metadata if error status
                if result.get("action_status") == "error":
                    error = result.get("error")
                    if error:
                        # Add routing metadata as separate fields in result data
                        # (ErrorDetail is a strict TypedDict and cannot have extra fields)
                        if "data" not in result:
                            result["data"] = {}
                        data = result["data"]
                        data["_route_to"] = "process_error"
                        data["_lifecycle_operation"] = operation
                        # Ensure error has timestamp
                        if "timestamp" not in error:
                            error["timestamp"] = datetime.now(UTC).isoformat()

                return result

            except Exception as e:
                # Unhandled exception - format as ActionResult error
                return {
                    "action_status": "error",
                    "data": {
                        "_route_to": "process_error",
                        "_lifecycle_operation": operation,
                    },
                    "actions": [],
                    "error": {
                        "type": "ServicePluginError",
                        "code": f"{self.name}.{operation}_exception",
                        "message": f"Unhandled exception in {operation}: {e}",
                        "details": {"exception_type": type(e).__name__},
                        "severity": "ERROR",
                        "timestamp": datetime.now(UTC).isoformat(),
                    },
                    "timestamp": datetime.now(UTC).isoformat(),
                }

        return wrapper  # type: ignore[return-value]

    return decorator
