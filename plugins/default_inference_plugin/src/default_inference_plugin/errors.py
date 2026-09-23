import traceback
from datetime import UTC, datetime
from typing import Any

from ananta.core.plugins.plugin_contracts import ActionStatus, ErrorSeverity
from ananta.error_handling import (
    AnantaError,
    PluginError,
)
from ananta.error_handling import create_error_response as core_create_error_response
from ananta.error_handling import (
    create_success_response as core_create_success_response,
)


class InferenceErrorCode:
    UNKNOWN_ERROR = "default_inference_plugin.unknown_error"
    PARAMETER_ERROR = "default_inference_plugin.parameter_error"
    PARSING_ERROR = "default_inference_plugin.parsing_error"
    INVALID_RESPONSE = "default_inference_plugin.invalid_response"
    UNSUPPORTED_MODEL = "default_inference_plugin.unsupported_model"
    AUTHENTICATION_ERROR = "default_inference_plugin.authentication_error"
    NETWORK_ERROR = "default_inference_plugin.network_error"
    TIMEOUT_ERROR = "default_inference_plugin.timeout_error"
    RATE_LIMIT_EXCEEDED = "default_inference_plugin.rate_limit_exceeded"
    SERVER_ERROR = "default_inference_plugin.server_error"


class ValidationError(PluginError):
    def __init__(self, message: str, details: dict[str, Any] | None = None):
        super().__init__(
            message=message,
            error_code=InferenceErrorCode.PARAMETER_ERROR,
            details=details,
            plugin_name="default_inference_plugin",
        )


def create_error_response(
    action_name: str = "unknown",
    message: str = "An error occurred",
    error_code: str = InferenceErrorCode.UNKNOWN_ERROR,
    details: dict[str, Any] | None = None,
    data: dict[str, Any] | None = None,
    actions: list[dict[str, object] | str] | None = None,
    severity: str = ErrorSeverity.ERROR.value,
) -> dict[str, Any]:
    timestamp = datetime.now(UTC).isoformat()
    response = core_create_error_response(
        action_name=action_name,
        message=message,
        error_code=error_code,
        details=details,
        data=data or {},
        actions=actions or [],
        severity=severity,
    )
    response["action_status"] = ActionStatus.ERROR.value
    response["timestamp"] = timestamp
    return response


def create_error_response_from_exception(
    exception: Exception,
    action_name: str = "unknown",
    additional_details: dict[str, Any] | None = None,
) -> dict[str, Any]:
    timestamp = datetime.now(UTC).isoformat()

    if isinstance(exception, AnantaError):
        error_dict = exception.to_dict()
        error_details = error_dict.get("details", {})
        # Type narrow error_dict components
        details: dict[str, Any] = dict(error_details) if isinstance(error_details, dict) else {}

        if additional_details:
            details.update(additional_details)

        # Extract message with type narrowing
        error_message = error_dict.get("message")
        message_str = error_message if isinstance(error_message, str) else str(exception)

        # Extract error code with type narrowing
        error_code_raw = error_dict.get("code")
        error_code_str = (
            error_code_raw if isinstance(error_code_raw, str) else InferenceErrorCode.UNKNOWN_ERROR
        )

        # Extract severity with type narrowing
        error_severity = error_dict.get("severity")
        severity_str = (
            error_severity if isinstance(error_severity, str) else ErrorSeverity.ERROR.value
        )

        response = create_error_response(
            action_name=action_name,
            message=message_str,
            error_code=error_code_str,
            details=details,
            severity=severity_str,
        )

        if "timestamp" in error_dict:
            response["error"]["timestamp"] = error_dict["timestamp"]
            response["timestamp"] = error_dict["timestamp"]
        else:
            response["error"]["timestamp"] = timestamp
            response["timestamp"] = timestamp

        return response
    else:
        details = {"traceback": traceback.format_exc()}
        if additional_details:
            details.update(additional_details)

        response = create_error_response(
            action_name=action_name,
            message=str(exception),
            error_code=InferenceErrorCode.UNKNOWN_ERROR,
            details=details,
        )

        response["timestamp"] = timestamp
        response["error"]["timestamp"] = timestamp
        return response


def create_success_response(
    data: Any = None, actions: list[dict[str, object] | str] | None = None
) -> dict[str, Any]:
    timestamp = datetime.now(UTC).isoformat()
    response = core_create_success_response(data=data or {}, actions=actions or [])
    response["action_status"] = ActionStatus.COMPLETED.value
    response["timestamp"] = timestamp
    return response
