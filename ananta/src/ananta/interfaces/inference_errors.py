"""Error types for inference service interface."""

from ananta.core.domain.enums import ErrorSeverity
from ananta.error_handling import FrameworkError


class InferenceError(FrameworkError):
    """Base error for all inference issues."""

    def __init__(
        self,
        message: str,
        error_code: str = "inference.error",
        severity: ErrorSeverity = ErrorSeverity.ERROR,
        details: dict[str, object] | None = None,
    ):
        super().__init__(
            message=message,
            error_code=error_code,
            severity=severity,
            details=details,
        )


class InferenceServiceUnavailableError(InferenceError):
    """Provider unreachable or unhealthy."""

    def __init__(self, message: str, details: dict[str, object] | None = None):
        super().__init__(
            message=message,
            error_code="inference.service_unavailable",
            severity=ErrorSeverity.CRITICAL,
            details=details,
        )


class InferenceTimeoutError(InferenceError):
    """Request timed out."""

    def __init__(self, message: str, details: dict[str, object] | None = None):
        super().__init__(
            message=message,
            error_code="inference.timeout",
            severity=ErrorSeverity.WARNING,
            details=details,
        )


class InferenceValidationError(InferenceError):
    """Invalid input or output validation failed."""

    def __init__(self, message: str, details: dict[str, object] | None = None):
        super().__init__(
            message=message,
            error_code="inference.validation_error",
            severity=ErrorSeverity.ERROR,
            details=details,
        )
