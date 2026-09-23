"""Plugin Key-Value Storage Implementation.

Provides key-value operations through plugin delegation for plugin mode operations.
Implements delegation to state management plugins for all key-value operations.
"""

import logging

from ananta.core.domain.types import ActionResult
from ananta.interfaces.state_management_interface import StateManagementInterface

logger = logging.getLogger(__name__)


class PluginKeyValueStorage:
    """Key-value operations through plugin delegation.

    Delegates all key-value operations to state management plugins.
    Used in plugin mode after system initialization is complete.
    """

    def __init__(self, plugin: StateManagementInterface) -> None:
        """Initialize plugin key-value storage.

        Args:
            plugin: State management plugin instance implementing StateManagementInterface

        Raises:
            TypeError: If plugin does not implement StateManagementInterface
        """
        self._plugin = plugin
        logger.debug("PluginKeyValueStorage initialized with typed plugin interface")

    def set_key_value(
        self,
        namespace: str,
        key: str,
        value: str | int | float | bool | dict[str, object] | list[object] | None,
        scope: str = "GLOBAL",
        ttl: int | None = None,
    ) -> ActionResult:
        """Set key-value pair via plugin delegation.

        Args:
            namespace: Target namespace for the key-value pair
            key: Key identifier for the value
            value: The value to store
            scope: Scope of the value ("GLOBAL", "SESSION", "FLOW")
            ttl: Time-to-live in seconds (None = permanent)

        Returns:
            ActionResult from plugin execution

        Raises:
            FrameworkError: If plugin execution fails
        """
        logger.debug("Plugin mode - delegating set_key_value operation to plugin")

        return self._plugin.set_key_value(namespace, key, value, scope, ttl)

    def get_key_value(self, namespace: str, key: str, scope: str = "GLOBAL") -> ActionResult:
        """Get key-value pair via plugin delegation.

        Args:
            namespace: Target namespace to query
            key: Key identifier to retrieve
            scope: Scope of the value ("GLOBAL", "SESSION", "FLOW")

        Returns:
            ActionResult from plugin execution

        Raises:
            FrameworkError: If plugin execution fails
        """
        return self._plugin.get_key_value(namespace, key, scope)

    def delete_key_value(self, namespace: str, key: str, scope: str = "GLOBAL") -> ActionResult:
        """Delete key-value pair via plugin delegation.

        Args:
            namespace: Target namespace for deletion
            key: Key identifier to delete
            scope: Scope of the value ("GLOBAL", "SESSION", "FLOW")

        Returns:
            ActionResult from plugin execution

        Raises:
            FrameworkError: If plugin execution fails
        """
        return self._plugin.delete_key_value(namespace, key, scope)

    def clear_key_values(
        self, namespace: str | None = None, scope: str | None = None
    ) -> ActionResult:
        """Clear key-value pairs via plugin delegation.

        Args:
            namespace: Optional namespace filter (None = all namespaces)
            scope: Optional scope filter (None = all scopes)

        Returns:
            ActionResult from plugin execution

        Raises:
            FrameworkError: If plugin execution fails
        """
        logger.debug("Plugin mode - delegating clear_key_values operation to plugin")

        return self._plugin.clear_key_values(namespace, scope)

    def list_key_values(
        self,
        namespace: str | None = None,
        scope: str | None = None,
        pattern: str | None = None,
    ) -> ActionResult:
        """List key-value pairs via plugin delegation.

        Args:
            namespace: Optional namespace filter (None = all namespaces)
            scope: Optional scope filter (None = all scopes)
            pattern: Optional key pattern filter (None = no pattern filtering)

        Returns:
            ActionResult from plugin execution

        Raises:
            FrameworkError: If plugin execution fails
        """
        logger.debug("Plugin mode - delegating list_key_values operation to plugin")

        return self._plugin.list_key_values(namespace, scope, pattern)
