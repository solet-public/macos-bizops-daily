"""Plugin Schema Storage Implementation.

Provides schema storage through plugin delegation for plugin mode operations.
Implements delegation to state management plugins.
"""

import logging

from ananta.core.domain.types import ActionResult
from ananta.interfaces.state_management_interface import StateManagementInterface

logger = logging.getLogger(__name__)


class PluginSchemaStorage:
    """Schema storage through plugin delegation.

    Delegates schema operations to state management plugins.
    Used in plugin mode after system initialization is complete.
    """

    def __init__(self, plugin: StateManagementInterface) -> None:
        """Initialize plugin storage.

        Args:
            plugin: State management plugin instance implementing StateManagementInterface

        Raises:
            TypeError: If plugin does not implement StateManagementInterface
        """
        self._plugin = plugin
        logger.debug("PluginSchemaStorage initialized with typed plugin interface")

    def create_schema(self, namespace: str, standardized_schema: dict[str, object]) -> ActionResult:
        """Create schema via plugin delegation.

        Args:
            namespace: Target namespace for schema
            standardized_schema: Schema with standard fields already added

        Returns:
            ActionResult from plugin execution

        Raises:
            FrameworkError: If plugin execution fails
        """
        logger.debug("Plugin mode - delegating schema creation to plugin")

        return self._plugin.create_schema(namespace, standardized_schema)

    def describe_schema(self, namespace: str) -> ActionResult:
        """Describe schema via plugin delegation.

        Args:
            namespace: Target namespace to describe

        Returns:
            ActionResult from plugin execution

        Raises:
            FrameworkError: If plugin execution fails
        """
        return self._plugin.describe_schema(namespace)

    def initialize_database(self) -> ActionResult:
        """Initialize database via plugin delegation.

        Returns:
            ActionResult from plugin execution

        Raises:
            FrameworkError: If plugin execution fails
        """
        logger.debug("Plugin mode - delegating database initialization to plugin")

        return self._plugin.initialize_database()
