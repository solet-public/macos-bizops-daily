import logging

from ananta.core.domain.enums import ErrorSeverity
from ananta.core.domain.error_codes import ErrorCode
from ananta.error_handling import FrameworkError

logger = logging.getLogger(__name__)


class TemplateResolutionError(FrameworkError):
    def __init__(
        self,
        message: str,
        template_context: dict[str, object] | None = None,
        error_code: str = ErrorCode.ACTION_INVALID_FORMAT,
        original_error: Exception | None = None,
        severity: str | ErrorSeverity = ErrorSeverity.ERROR,
    ) -> None:
        super().__init__(
            message=f"Template Resolution Failed: {message}",
            error_code=error_code,
            details=template_context or {},
            original_error=original_error,
            severity=severity,
        )
        self.template_context = template_context or {}


class UnresolvedTemplateVariablesError(TemplateResolutionError):
    def __init__(
        self,
        unresolved_variables: list[str],
        action_name: str = "unknown",
        error_code: str = ErrorCode.ACTION_INVALID_FORMAT,
        original_error: Exception | None = None,
        severity: str | ErrorSeverity = ErrorSeverity.ERROR,
    ) -> None:
        variables_str = ", ".join(f"<<<{var}>>>" for var in unresolved_variables)
        super().__init__(
            f"Unresolved template variables in action '{action_name}': {variables_str}. "
            "All template variables must be resolved before validation.",
            template_context={
                "action_name": action_name,
                "unresolved_variables": unresolved_variables,
                "variable_count": len(unresolved_variables),
            },
            error_code=error_code,
            original_error=original_error,
            severity=severity,
        )
        self.unresolved_variables = unresolved_variables


class UnknownTemplateFunctionError(TemplateResolutionError):
    def __init__(
        self,
        function_name: str,
        available_functions: list[str] | None = None,
        error_code: str = ErrorCode.ACTION_INVALID_FORMAT,
        original_error: Exception | None = None,
        severity: str | ErrorSeverity = ErrorSeverity.ERROR,
    ) -> None:
        message = f"Unknown template function: {function_name}"
        if available_functions:
            message += f". Available functions: {', '.join(available_functions)}"

        super().__init__(
            message,
            template_context={
                "function_name": function_name,
                "available_functions": available_functions or [],
            },
            error_code=error_code,
            original_error=original_error,
            severity=severity,
        )
        self.function_name = function_name


class TemplateFileNotFoundError(TemplateResolutionError):
    def __init__(
        self,
        file_path: str,
        searched_paths: list[str] | None = None,
        error_code: str = ErrorCode.ACTION_INVALID_FORMAT,
        original_error: Exception | None = None,
        severity: str | ErrorSeverity = ErrorSeverity.ERROR,
    ) -> None:
        message = f"Template file not found: {file_path}"
        if searched_paths:
            message += f". Searched in: {', '.join(searched_paths)}"

        super().__init__(
            message,
            template_context={"file_path": file_path, "searched_paths": searched_paths or []},
            error_code=error_code,
            original_error=original_error,
            severity=severity,
        )
        self.file_path = file_path


class TemplateFunctionError(TemplateResolutionError):
    def __init__(
        self,
        function_name: str,
        function_args: str,
        error_message: str,
        error_code: str = ErrorCode.ACTION_INVALID_FORMAT,
        original_error: Exception | None = None,
        severity: str | ErrorSeverity = ErrorSeverity.ERROR,
    ) -> None:
        super().__init__(
            f"Template function '{function_name}' failed: {error_message}",
            template_context={
                "function_name": function_name,
                "function_args": function_args,
                "error_message": error_message,
            },
            error_code=error_code,
            original_error=original_error,
            severity=severity,
        )
        self.function_name = function_name
        self.function_args = function_args


class TemplateVariableError(TemplateResolutionError):
    def __init__(
        self,
        variable_name: str,
        error_message: str,
        error_code: str = ErrorCode.ACTION_INVALID_FORMAT,
        original_error: Exception | None = None,
        severity: str | ErrorSeverity = ErrorSeverity.ERROR,
    ) -> None:
        super().__init__(
            f"Template variable '{variable_name}' resolution failed: {error_message}",
            template_context={"variable_name": variable_name, "error_message": error_message},
            error_code=error_code,
            original_error=original_error,
            severity=severity,
        )
        self.variable_name = variable_name


class TemplateValidationError(TemplateResolutionError):
    def __init__(
        self,
        message: str,
        template_pattern: str | None = None,
        error_code: str = ErrorCode.ACTION_INVALID_FORMAT,
        original_error: Exception | None = None,
        severity: str | ErrorSeverity = ErrorSeverity.ERROR,
    ) -> None:
        super().__init__(
            f"Template validation failed: {message}",
            template_context={"template_pattern": template_pattern, "validation_message": message},
            error_code=error_code,
            original_error=original_error,
            severity=severity,
        )
        self.template_pattern = template_pattern
