"""Namespace Validator.

Provides namespace validation logic extracted from StateService.
Implements fail-fast validation without fallback patterns.
"""

from ananta.core.domain.enums import ErrorSeverity
from ananta.error_handling import FrameworkError


class NamespaceValidator:
    """Validates namespace strings according to Ananta standards.

    Enforces strict validation rules without fallback behavior.
    """

    def validate_namespace(self, namespace: str) -> None:
        """Validate namespace string format and content.

        Args:
            namespace: Namespace string to validate

        Raises:
            FrameworkError: If namespace is invalid (no fallback behavior)
        """
        if not namespace:
            raise FrameworkError(
                message="Namespace must be a non-empty string",
                error_code="state_service.invalid_namespace",
                details={"provided_namespace": namespace},
                severity=ErrorSeverity.ERROR,
            )

        if not namespace.replace("_", "").replace("-", "").isalnum():
            raise FrameworkError(
                message="Namespace must contain only alphanumeric characters, hyphens, and underscores",
                error_code="state_service.invalid_namespace_format",
                details={"provided_namespace": namespace},
                severity=ErrorSeverity.ERROR,
            )
