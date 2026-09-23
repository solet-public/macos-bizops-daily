import logging
import traceback
from datetime import UTC, datetime
from types import TracebackType
from typing import TypeVar

from ananta.core.domain.enums import ActionStatus, ErrorSeverity
from ananta.core.domain.error_codes import ErrorCode

T = TypeVar("T", bound=dict[str, object])


class AnantaError(Exception):
    def __init__(
        self,
        message: str,
        error_code: str,
        details: dict[str, object] | None = None,
        original_error: Exception | None = None,
        severity: str | ErrorSeverity = ErrorSeverity.ERROR,
    ):
        super().__init__(message)
        self.message = message
        self.error_code = error_code
        self.details = details or {}
        self.original_error = original_error
        self.severity = severity.value if isinstance(severity, ErrorSeverity) else severity
        self.timestamp = datetime.now(UTC).isoformat()

    @property
    def error_type(self) -> str:
        if self.__class__.__name__ == "FrameworkError":
            return "framework_error"
        elif self.__class__.__name__ == "PluginError":
            return "plugin_error"
        elif self.__class__.__name__ == "ExternalError":
            return "external_error"
        else:
            return "unknown_error"

    def to_dict(self) -> dict[str, object]:
        result: dict[str, object] = {
            "type": self.error_type,
            "code": self.error_code,
            "message": self.message,
            "details": self.details,
            "severity": self.severity,
            "timestamp": self.timestamp,
        }
        if self.original_error:
            result["original_error"] = str(self.original_error)
        return result

    def to_response(self, action_name: str = "unknown") -> dict[str, object]:
        return create_error_response(
            action_name=action_name,
            message=self.message,
            error_code=self.error_code,
            details=self.details,
            severity=self.severity,
            timestamp=self.timestamp,
        )

    def to_action_state(
        self, action_name: str, parameters: dict[str, object] | None = None
    ) -> dict[str, object]:
        return {
            "name": action_name,
            "parameters": parameters or {},
            "action_status": ActionStatus.ERROR.value,
            "error": self.to_dict(),
            "timestamp": self.timestamp,
        }


class FrameworkError(AnantaError):
    def __init__(
        self,
        message: str,
        error_code: str = ErrorCode.FRAMEWORK_GENERIC,
        details: dict[str, object] | None = None,
        original_error: Exception | None = None,
        severity: str | ErrorSeverity = ErrorSeverity.ERROR,
    ):
        super().__init__(
            message=message,
            error_code=error_code,
            details=details,
            original_error=original_error,
            severity=severity,
        )


class ValidationError(FrameworkError):
    def __init__(
        self,
        message: str,
        error_code: str = ErrorCode.VALIDATION_ERROR,
        details: dict[str, object] | None = None,
        original_error: Exception | None = None,
        severity: str | ErrorSeverity = ErrorSeverity.ERROR,
    ):
        super().__init__(
            message=message,
            error_code=error_code,
            details=details,
            original_error=original_error,
            severity=severity,
        )


class ActionValidationError(ValidationError):
    def __init__(
        self,
        message: str,
        action_name: str,
        error_code: str = ErrorCode.ACTION_INVALID_PARAMS,
        details: dict[str, object] | None = None,
        original_error: Exception | None = None,
        severity: str | ErrorSeverity = ErrorSeverity.ERROR,
    ):
        enhanced_details: dict[str, object] = {
            "action": action_name,
            "error_type": "validation",
        }
        if details:
            enhanced_details.update(details)

        if details and "expected" in details and "received" in details:
            enhanced_message = (
                f"{message}. Expected: {details['expected']}, Received: {details['received']}"
            )
        else:
            enhanced_message = message

        super().__init__(
            message=enhanced_message,
            error_code=error_code,
            details=enhanced_details,
            original_error=original_error,
            severity=severity,
        )


class ConfigurationError(FrameworkError):
    def __init__(
        self,
        message: str,
        error_code: str = ErrorCode.CONFIGURATION_ERROR,
        details: dict[str, object] | None = None,
        original_error: Exception | None = None,
        plugin_name: str | None = None,
        param_name: str | None = None,
        severity: str | ErrorSeverity = ErrorSeverity.ERROR,
    ):
        if plugin_name or param_name:
            details = details or {}
            if plugin_name:
                details["plugin_name"] = plugin_name
            if param_name:
                details["param_name"] = param_name

        super().__init__(
            message=message,
            error_code=error_code,
            details=details,
            original_error=original_error,
            severity=severity,
        )


class MissingConfigurationError(ConfigurationError):
    def __init__(
        self,
        message: str,
        plugin_name: str,
        param_name: str | None = None,
        error_code: str = "ananta.missing_configuration",
        details: dict[str, object] | None = None,
        original_error: Exception | None = None,
        severity: str | ErrorSeverity = ErrorSeverity.ERROR,
    ):
        config_details: dict[str, object] = {"plugin_name": plugin_name}
        if param_name:
            config_details["param_name"] = param_name
            if not message:
                message = f"Missing required operational configuration parameter: {param_name}"
        else:
            if not message:
                message = f"Missing required operational configuration for plugin: {plugin_name}"

        if details:
            config_details.update(details)

        super().__init__(
            message=message,
            error_code=error_code,
            details=config_details,
            original_error=original_error,
            severity=severity,
        )


class InvalidConfigurationFormatError(ConfigurationError):
    def __init__(
        self,
        message: str,
        plugin_name: str,
        param_name: str | None = None,
        expected_type: str | None = None,
        received_type: str | None = None,
        error_code: str = "ananta.invalid_configuration_format",
        details: dict[str, object] | None = None,
        original_error: Exception | None = None,
        severity: str | ErrorSeverity = ErrorSeverity.ERROR,
    ):
        config_details: dict[str, object] = {"plugin_name": plugin_name}
        if param_name:
            config_details["param_name"] = param_name
        if expected_type:
            config_details["expected_type"] = expected_type
        if received_type:
            config_details["received_type"] = received_type

        if details:
            config_details.update(details)

        super().__init__(
            message=message,
            error_code=error_code,
            details=config_details,
            original_error=original_error,
            severity=severity,
        )


class ConfigValidationError(ConfigurationError):
    def __init__(
        self,
        message: str,
        plugin_name: str,
        param_name: str | None = None,
        validation_errors: list[str] | None = None,
        error_code: str = "ananta.config_validation_error",
        details: dict[str, object] | None = None,
        original_error: Exception | None = None,
        severity: str | ErrorSeverity = ErrorSeverity.ERROR,
    ):
        config_details: dict[str, object] = {"plugin_name": plugin_name}
        if param_name:
            config_details["param_name"] = param_name
        if validation_errors:
            config_details["validation_errors"] = validation_errors

        if details:
            config_details.update(details)

        super().__init__(
            message=message,
            error_code=error_code,
            details=config_details,
            original_error=original_error,
            severity=severity,
        )


class ResourceError(FrameworkError):
    def __init__(
        self,
        message: str,
        error_code: str = ErrorCode.FILE_ACCESS_ERROR,
        details: dict[str, object] | None = None,
        original_error: Exception | None = None,
        severity: str | ErrorSeverity = ErrorSeverity.ERROR,
    ):
        super().__init__(
            message=message,
            error_code=error_code,
            details=details,
            original_error=original_error,
            severity=severity,
        )


class PermissionError(FrameworkError):
    def __init__(
        self,
        message: str,
        error_code: str = ErrorCode.PERMISSION_ERROR,
        details: dict[str, object] | None = None,
        original_error: Exception | None = None,
        severity: str | ErrorSeverity = ErrorSeverity.ERROR,
    ):
        super().__init__(
            message=message,
            error_code=error_code,
            details=details,
            original_error=original_error,
            severity=severity,
        )


class SystemError(FrameworkError):
    def __init__(
        self,
        message: str,
        error_code: str = ErrorCode.UNKNOWN_ERROR,
        details: dict[str, object] | None = None,
        original_error: Exception | None = None,
        severity: str | ErrorSeverity = ErrorSeverity.CRITICAL,
    ):
        super().__init__(
            message=message,
            error_code=error_code,
            details=details,
            original_error=original_error,
            severity=severity,
        )


class PluginError(AnantaError):
    def __init__(
        self,
        message: str,
        error_code: str = ErrorCode.PLUGIN_GENERIC,
        details: dict[str, object] | None = None,
        original_error: Exception | None = None,
        plugin_name: str | None = None,
        severity: str | ErrorSeverity = ErrorSeverity.ERROR,
    ):
        if plugin_name:
            details = details or {}
            details["plugin"] = plugin_name

        super().__init__(
            message=message,
            error_code=error_code,
            details=details,
            original_error=original_error,
            severity=severity,
        )


class PluginConfigError(PluginError):
    def __init__(
        self,
        message: str,
        plugin_name: str,
        error_code: str = "ananta.plugin_config_error",
        details: dict[str, object] | None = None,
        original_error: Exception | None = None,
        severity: str | ErrorSeverity = ErrorSeverity.ERROR,
    ):
        super().__init__(
            message=message,
            error_code=error_code,
            details=details,
            original_error=original_error,
            plugin_name=plugin_name,
            severity=severity,
        )


class PluginContractError(PluginError):
    """Raised when a plugin violates its interface contract."""

    def __init__(
        self,
        message: str,
        plugin_name: str,
        error_code: str = "ananta.plugin_contract_error",
        details: dict[str, object] | None = None,
        original_error: Exception | None = None,
        severity: str | ErrorSeverity = ErrorSeverity.ERROR,
    ):
        super().__init__(
            message=message,
            error_code=error_code,
            details=details,
            original_error=original_error,
            plugin_name=plugin_name,
            severity=severity,
        )


class ExternalError(AnantaError):
    def __init__(
        self,
        message: str,
        error_code: str = ErrorCode.EXTERNAL_GENERIC,
        details: dict[str, object] | None = None,
        original_error: Exception | None = None,
        service_name: str | None = None,
        severity: str | ErrorSeverity = ErrorSeverity.ERROR,
    ):
        if service_name:
            details = details or {}
            details["service"] = service_name

        super().__init__(
            message=message,
            error_code=error_code,
            details=details,
            original_error=original_error,
            severity=severity,
        )


class NetworkError(ExternalError):
    def __init__(
        self,
        message: str,
        error_code: str = ErrorCode.NETWORK_GENERIC,
        details: dict[str, object] | None = None,
        original_error: Exception | None = None,
        service_name: str | None = None,
        severity: str | ErrorSeverity = ErrorSeverity.ERROR,
    ):
        super().__init__(
            message=message,
            error_code=error_code,
            details=details,
            original_error=original_error,
            service_name=service_name,
            severity=severity,
        )


class TemplateReferenceError(FrameworkError):
    def __init__(
        self,
        template_pattern: str,
        context: dict[str, object],
        details: str = "",
        original_error: Exception | None = None,
        severity: str | ErrorSeverity = ErrorSeverity.ERROR,
    ):
        super().__init__(
            message=f"Template reference '{template_pattern}' failed to resolve: {details}",
            error_code="template.301",
            details={"template_pattern": template_pattern, "context": context, "details": details},
            original_error=original_error,
            severity=severity,
        )


class ActionNotFoundError(TemplateReferenceError):
    def __init__(
        self,
        template_pattern: str,
        context: dict[str, object],
        details: str = "",
        original_error: Exception | None = None,
        severity: str | ErrorSeverity = ErrorSeverity.ERROR,
    ):
        super().__init__(
            template_pattern=template_pattern,
            context=context,
            details=details,
            original_error=original_error,
            severity=severity,
        )
        self.error_code = "template.302"


class ActionNameCollisionError(FrameworkError):
    def __init__(
        self,
        action_name: str,
        flow_id: str,
        details: str = "",
        original_error: Exception | None = None,
        severity: str | ErrorSeverity = ErrorSeverity.ERROR,
    ):
        super().__init__(
            message=f"Action name '{action_name}' already exists in flow {flow_id}",
            error_code="orchestrator.401",
            details={"action_name": action_name, "flow_id": flow_id, "details": details},
            original_error=original_error,
            severity=severity,
        )


class ContextPropagationError(FrameworkError):
    def __init__(
        self,
        message: str,
        context: dict[str, object] | None = None,
        details: str = "",
        original_error: Exception | None = None,
        severity: str | ErrorSeverity = ErrorSeverity.ERROR,
    ):
        super().__init__(
            message=message,
            error_code="orchestrator.402",
            details={"context": context, "details": details},
            original_error=original_error,
            severity=severity,
        )


def create_success_response(
    data: dict[str, object] | None = None,
    actions: list[dict[str, object] | str] | None = None,
) -> dict[str, object]:
    timestamp = datetime.now(UTC).isoformat()
    return {
        "status": ActionStatus.COMPLETED.value,
        "action_status": ActionStatus.COMPLETED.value,
        "data": data or {},
        "actions": actions or [],
        "error": None,
        "timestamp": timestamp,
    }


def create_error_response(
    action_name: str,
    message: str,
    error_code: str,
    details: dict[str, object] | None = None,
    data: dict[str, object] | None = None,
    actions: list[dict[str, object] | str] | None = None,
    severity: str | ErrorSeverity = ErrorSeverity.ERROR,
    timestamp: str | None = None,
) -> dict[str, object]:
    severity_value = severity.value if isinstance(severity, ErrorSeverity) else severity
    current_timestamp = timestamp or datetime.now(UTC).isoformat()

    return {
        "status": ActionStatus.ERROR.value,
        "action_status": ActionStatus.ERROR.value,
        "data": data or {},
        "actions": actions or [],
        "error": {
            "type": "error",
            "code": error_code,
            "message": f"[{action_name}] {message}",
            "details": details or {},
            "severity": severity_value,
            "timestamp": current_timestamp,
        },
        "timestamp": current_timestamp,
    }


def create_error_response_from_exception(
    exception: Exception,
    action_name: str = "unknown",
    additional_details: dict[str, object] | None = None,
) -> dict[str, object]:
    if isinstance(exception, AnantaError):
        error_dict = exception.to_dict()
        error_details = error_dict.get("details", {})
        details: dict[str, object] = dict(error_details) if isinstance(error_details, dict) else {}

        if additional_details:
            details.update(additional_details)

        message = error_dict.get("message", str(exception))
        error_code = error_dict.get("code", ErrorCode.UNKNOWN_ERROR)
        timestamp = error_dict.get("timestamp")

        return create_error_response(
            action_name=action_name,
            message=str(message) if message is not None else str(exception),
            error_code=str(error_code) if error_code is not None else ErrorCode.UNKNOWN_ERROR,
            details=details,
            timestamp=str(timestamp) if timestamp is not None else None,
        )
    else:
        details = {"traceback": traceback.format_exc()}
        if additional_details:
            details.update(additional_details)

        return create_error_response(
            action_name=action_name,
            message=str(exception),
            error_code=ErrorCode.UNKNOWN_ERROR,
            details=details,
        )


def error_to_action_state(
    exception: Exception,
    action_name: str,
    parameters: dict[str, object] | None = None,
) -> dict[str, object]:
    if isinstance(exception, AnantaError):
        return exception.to_action_state(action_name, parameters)

    timestamp = datetime.now(UTC).isoformat()
    return {
        "name": action_name,
        "parameters": parameters or {},
        "action_status": ActionStatus.ERROR.value,
        "error": {
            "code": ErrorCode.UNKNOWN_ERROR,
            "message": str(exception),
            "details": {"traceback": traceback.format_exc()},
            "severity": ErrorSeverity.ERROR.value,
            "timestamp": timestamp,
        },
        "timestamp": timestamp,
    }


def log_exception(exception: Exception, logger: logging.Logger, level: int = logging.ERROR) -> None:
    if isinstance(exception, AnantaError):
        error_dict = exception.to_dict()
        logger.log(
            level,
            f"[{exception.__class__.__name__}] {error_dict['code']} - {error_dict['message']}",
            exc_info=True,
        )
    else:
        logger.log(level, f"{str(exception)}", exc_info=True)


class ErrorContext:
    def __init__(
        self,
        context: str,
        logger: logging.Logger,
        error_class: type[AnantaError] = SystemError,
        error_code: str = ErrorCode.UNKNOWN_ERROR,
        action_name: str | None = None,
    ):
        self.context = context
        self.logger = logger
        self.error_class = error_class
        self.error_code = error_code
        self.action_name = action_name

    def __enter__(self) -> "ErrorContext":
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exception: BaseException | None,
        _exc_tb: TracebackType | None,
    ) -> bool:
        if exc_type is not None and exception is not None:
            if isinstance(exception, AnantaError):
                log_exception(exception, self.logger)
                return False

            error = self.error_class(
                message=f"{self.context}: {str(exception)}",
                error_code=self.error_code,
                original_error=Exception(str(exception)),
            )
            log_exception(error, self.logger)
            return False
        return True


def format_error_details(error: Exception, include_traceback: bool = False) -> dict[str, object]:
    result: dict[str, object]
    if isinstance(error, AnantaError):
        result = error.to_dict()
    else:
        result = {
            "code": ErrorCode.UNKNOWN_ERROR,
            "message": str(error),
            "details": {},
            "severity": ErrorSeverity.ERROR.value,
            "timestamp": datetime.now(UTC).isoformat(),
        }

    if include_traceback:
        tb = traceback.format_exception(type(error), error, error.__traceback__)
        result["traceback"] = "".join(tb)

    return result


def format_config_error_message(
    plugin_name: str, param_name: str | None = None, description: str | None = None
) -> str:
    if param_name and description:
        return f"Operational configuration error in plugin '{plugin_name}': {param_name} - {description}"
    elif param_name:
        return f"Operational configuration error in plugin '{plugin_name}': {param_name}"
    elif description:
        return f"Operational configuration error in plugin '{plugin_name}': {description}"
    else:
        return f"Operational configuration error in plugin '{plugin_name}'"
