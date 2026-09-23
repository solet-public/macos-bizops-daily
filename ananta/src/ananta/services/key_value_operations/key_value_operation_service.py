"""Key-Value Operation Service.

Provides focused key-value operation functionality extracted from StateService.
Implements Strategy pattern for bootstrap vs plugin mode key-value operations.
"""

import logging

from ananta.core.domain.types import ActionResult

from .key_value_storage_strategy import KeyValueStorageStrategy
from .key_value_validator import KeyValueValidator

logger = logging.getLogger(__name__)


class KeyValueOperationService:
    """Manages key-value CRUD operations with proper separation of concerns.

    Uses Strategy pattern to handle bootstrap vs plugin mode operations.
    Implements dependency injection for clean architecture.
    """

    def __init__(
        self,
        storage_strategy: KeyValueStorageStrategy,
        validator: KeyValueValidator | None = None,
    ) -> None:
        """Initialize key-value operation service.

        Args:
            storage_strategy: Strategy for key-value storage operations
            validator: Validator for key-value parameters (created if None)
        """
        self._storage_strategy = storage_strategy
        self._validator = validator or KeyValueValidator()
        logger.debug("KeyValueOperationService initialized with dependency injection")

    def set_key_value(
        self,
        namespace: str,
        key: str,
        value: str | int | float | bool | dict[str, object] | list[object] | None,
        scope: str = "GLOBAL",
        ttl: int | None = None,
    ) -> ActionResult:
        """Set key-value pair with validation and proper delegation.

        Args:
            namespace: Target namespace for the key-value pair
            key: Key identifier for the value
            value: The value to store (will be JSON-serialized if complex)
            scope: Scope of the value ("GLOBAL", "SESSION", "FLOW")
            ttl: Time-to-live in seconds (None = permanent)

        Returns:
            ActionResult with operation status and details

        Raises:
            FrameworkError: If validation fails or set operation fails
        """
        # Validate parameters
        self._validator.validate_key_value_parameters(namespace, key, scope, ttl)

        logger.debug(f"Setting key-value: {namespace}.{key}.{scope}")

        # Delegate to storage strategy
        return self._storage_strategy.set_key_value(namespace, key, value, scope, ttl)

    def get_key_value(self, namespace: str, key: str, scope: str = "GLOBAL") -> ActionResult:
        """Get key-value pair with proper delegation.

        Args:
            namespace: Target namespace to query
            key: Key identifier to retrieve
            scope: Scope of the value ("GLOBAL", "SESSION", "FLOW")

        Returns:
            ActionResult with retrieved value or error details

        Raises:
            FrameworkError: If get operation fails
        """
        return self._storage_strategy.get_key_value(namespace, key, scope)

    def delete_key_value(self, namespace: str, key: str, scope: str = "GLOBAL") -> ActionResult:
        """Delete key-value pair with proper delegation.

        Args:
            namespace: Target namespace for deletion
            key: Key identifier to delete
            scope: Scope of the value ("GLOBAL", "SESSION", "FLOW")

        Returns:
            ActionResult with operation status and details

        Raises:
            FrameworkError: If delete operation fails
        """
        logger.debug(f"Deleting key-value: {namespace}.{key}.{scope}")

        # Delegate to storage strategy
        return self._storage_strategy.delete_key_value(namespace, key, scope)

    def clear_key_values(
        self, namespace: str | None = None, scope: str | None = None
    ) -> ActionResult:
        """Clear key-value pairs with validation and proper delegation.

        Args:
            namespace: Optional namespace filter (None = all namespaces)
            scope: Optional scope filter (None = all scopes)

        Returns:
            ActionResult with operation status and details

        Raises:
            FrameworkError: If validation fails or clear operation fails
        """
        # Validate parameters
        self._validator.validate_clear_key_values_parameters(namespace, scope)

        logger.debug(f"Clearing key-values: namespace={namespace}, scope={scope}")

        # Delegate to storage strategy
        return self._storage_strategy.clear_key_values(namespace, scope)

    def list_key_values(
        self,
        namespace: str | None = None,
        scope: str | None = None,
        pattern: str | None = None,
    ) -> ActionResult:
        """List key-value pairs with validation and proper delegation.

        Args:
            namespace: Optional namespace filter (None = all namespaces)
            scope: Optional scope filter (None = all scopes)
            pattern: Optional key pattern filter (None = no pattern filtering)

        Returns:
            ActionResult with matching key-value pairs

        Raises:
            FrameworkError: If validation fails or list operation fails
        """
        # Validate parameters
        self._validator.validate_list_key_values_parameters(namespace, scope, pattern)

        # Delegate to storage strategy
        return self._storage_strategy.list_key_values(namespace, scope, pattern)
