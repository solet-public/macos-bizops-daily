"""Key-Value Storage Strategy Protocol.

Defines the interface for different key-value storage implementations.
Implements Strategy pattern for bootstrap vs plugin mode key-value operations.
"""

from typing import Protocol

from ananta.core.domain.types import ActionResult


class KeyValueStorageStrategy(Protocol):
    """Protocol defining key-value CRUD operations.

    Implementations handle bootstrap mode (in-memory) vs plugin mode (delegation).
    """

    def set_key_value(
        self,
        namespace: str,
        key: str,
        value: str | int | float | bool | dict[str, object] | list[object] | None,
        scope: str = "GLOBAL",
        ttl: int | None = None,
    ) -> ActionResult:
        """Set a key-value pair with optional TTL.

        Args:
            namespace: Target namespace for the key-value pair
            key: Key identifier for the value
            value: The value to store (will be JSON-serialized if complex)
            scope: Scope of the value ("GLOBAL", "SESSION", "FLOW")
            ttl: Time-to-live in seconds (None = permanent)

        Returns:
            ActionResult with operation status and details

        Raises:
            FrameworkError: If set operation fails
        """
        ...

    def get_key_value(self, namespace: str, key: str, scope: str = "GLOBAL") -> ActionResult:
        """Get a key-value pair from storage.

        Args:
            namespace: Target namespace to query
            key: Key identifier to retrieve
            scope: Scope of the value ("GLOBAL", "SESSION", "FLOW")

        Returns:
            ActionResult with retrieved value or error details

        Raises:
            FrameworkError: If get operation fails
        """
        ...

    def delete_key_value(self, namespace: str, key: str, scope: str = "GLOBAL") -> ActionResult:
        """Delete a key-value pair from storage.

        Args:
            namespace: Target namespace for deletion
            key: Key identifier to delete
            scope: Scope of the value ("GLOBAL", "SESSION", "FLOW")

        Returns:
            ActionResult with operation status and details

        Raises:
            FrameworkError: If delete operation fails
        """
        ...

    def clear_key_values(
        self, namespace: str | None = None, scope: str | None = None
    ) -> ActionResult:
        """Clear key-value pairs by namespace and/or scope.

        Args:
            namespace: Optional namespace filter (None = all namespaces)
            scope: Optional scope filter (None = all scopes)

        Returns:
            ActionResult with operation status and details

        Raises:
            FrameworkError: If clear operation fails
        """
        ...

    def list_key_values(
        self,
        namespace: str | None = None,
        scope: str | None = None,
        pattern: str | None = None,
    ) -> ActionResult:
        """List key-value pairs with optional filtering.

        Args:
            namespace: Optional namespace filter (None = all namespaces)
            scope: Optional scope filter (None = all scopes)
            pattern: Optional key pattern filter (None = no pattern filtering)

        Returns:
            ActionResult with matching key-value pairs

        Raises:
            FrameworkError: If list operation fails
        """
        ...
