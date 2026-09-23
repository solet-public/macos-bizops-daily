"""Key-Value Parameter Validator.

Provides validation for key-value operation parameters.
Extracted from StateService validation methods.
"""

import logging

from ananta.core.domain.enums import ErrorSeverity
from ananta.error_handling import FrameworkError

logger = logging.getLogger(__name__)


class KeyValueValidator:
    """Validates parameters for key-value operations.

    Provides centralized validation logic extracted from StateService.
    """

    def validate_key_value_parameters(
        self, namespace: str, key: str, scope: str, ttl: int | None
    ) -> None:
        """Validate all parameters for set_key_value method.

        Args:
            namespace: Target namespace for the key-value pair
            key: Key identifier for the value
            scope: Scope of the value ("GLOBAL", "SESSION", "FLOW")
            ttl: Time-to-live in seconds (None = permanent)

        Raises:
            FrameworkError: If any parameter is invalid
        """
        self.validate_key_value_namespace(namespace)
        self.validate_key_value_key(key)
        self.validate_key_value_scope(scope)
        self.validate_key_value_ttl(ttl)

    def validate_key_value_namespace(self, namespace: str) -> None:
        """Validate namespace parameter for key-value operations.

        Args:
            namespace: Namespace to validate

        Raises:
            FrameworkError: If namespace is invalid
        """
        if not namespace:
            logger.error(f"Invalid namespace parameter: {namespace}")
            raise FrameworkError(
                message="Namespace must be a non-empty string",
                error_code="key_value_service.invalid_runtime_namespace",
                details={"provided_namespace": namespace},
                severity=ErrorSeverity.ERROR,
            )

    def validate_key_value_key(self, key: str) -> None:
        """Validate key parameter for key-value operations.

        Args:
            key: Key to validate

        Raises:
            FrameworkError: If key is invalid
        """
        if not key:
            logger.error(f"Invalid key parameter: {key}")
            raise FrameworkError(
                message="Key must be a non-empty string",
                error_code="key_value_service.invalid_runtime_key",
                details={"provided_key": key},
                severity=ErrorSeverity.ERROR,
            )

    def validate_key_value_scope(self, scope: str) -> None:
        """Validate scope parameter for key-value operations.

        Args:
            scope: Scope to validate

        Raises:
            FrameworkError: If scope is invalid
        """
        if scope not in ["GLOBAL", "SESSION", "FLOW"]:
            logger.error(f"Invalid scope parameter: {scope}")
            raise FrameworkError(
                message="Scope must be one of: GLOBAL, SESSION, FLOW",
                error_code="key_value_service.invalid_runtime_scope",
                details={
                    "provided_scope": scope,
                    "valid_scopes": ["GLOBAL", "SESSION", "FLOW"],
                },
                severity=ErrorSeverity.ERROR,
            )

    def validate_key_value_ttl(self, ttl: int | None) -> None:
        """Validate TTL parameter for key-value operations.

        Args:
            ttl: TTL to validate

        Raises:
            FrameworkError: If TTL is invalid
        """
        if ttl is not None and ttl <= 0:
            logger.error(f"Invalid TTL parameter: {ttl}")
            raise FrameworkError(
                message="TTL must be a positive integer or None",
                error_code="key_value_service.invalid_runtime_ttl",
                details={"provided_ttl": ttl},
                severity=ErrorSeverity.ERROR,
            )

    def validate_clear_key_values_parameters(
        self, namespace: str | None, scope: str | None
    ) -> None:
        """Validate parameters for clear_key_values operation.

        Args:
            namespace: Optional namespace filter (None = all namespaces)
            scope: Optional scope filter (None = all scopes)

        Raises:
            FrameworkError: If any parameter is invalid
        """
        if namespace is not None and not namespace:
            logger.error(f"Invalid namespace parameter: {namespace}")
            raise FrameworkError(
                message="Namespace must be a non-empty string or None",
                error_code="key_value_service.invalid_runtime_namespace",
                details={"provided_namespace": namespace},
                severity=ErrorSeverity.ERROR,
            )

        if scope is not None and scope not in ["GLOBAL", "SESSION", "FLOW"]:
            logger.error(f"Invalid scope parameter: {scope}")
            raise FrameworkError(
                message="Scope must be one of: GLOBAL, SESSION, FLOW, or None",
                error_code="key_value_service.invalid_runtime_scope",
                details={
                    "provided_scope": scope,
                    "valid_scopes": ["GLOBAL", "SESSION", "FLOW"],
                },
                severity=ErrorSeverity.ERROR,
            )

    def validate_list_key_values_parameters(
        self, namespace: str | None, scope: str | None, pattern: str | None
    ) -> None:
        """Validate parameters for list_key_values operation.

        Args:
            namespace: Optional namespace filter (None = all namespaces)
            scope: Optional scope filter (None = all scopes)
            pattern: Optional key pattern filter (None = no pattern filtering)

        Raises:
            FrameworkError: If any parameter is invalid
        """
        if namespace is not None and not namespace:
            logger.error(f"Invalid namespace parameter: {namespace}")
            raise FrameworkError(
                message="Namespace must be a non-empty string or None",
                error_code="key_value_service.invalid_runtime_namespace",
                details={"provided_namespace": namespace},
                severity=ErrorSeverity.ERROR,
            )

        if scope is not None and scope not in ["GLOBAL", "SESSION", "FLOW"]:
            logger.error(f"Invalid scope parameter: {scope}")
            raise FrameworkError(
                message="Scope must be one of: GLOBAL, SESSION, FLOW, or None",
                error_code="key_value_service.invalid_runtime_scope",
                details={
                    "provided_scope": scope,
                    "valid_scopes": ["GLOBAL", "SESSION", "FLOW"],
                },
                severity=ErrorSeverity.ERROR,
            )

        # TODO: Add pattern validation if regex/glob pattern syntax needs to be enforced
        # Pattern validation: type is already enforced by type hints (str | None)
        # No additional runtime validation needed beyond type checking
        _ = pattern  # Acknowledge parameter is part of public API
