"""Plugin Database Storage Implementation.

Provides database operations through plugin delegation for plugin mode operations.
Implements delegation to state management plugins for all CRUD operations.
"""

import logging

from ananta.core.domain.types import ActionResult
from ananta.interfaces.state_management_interface import StateManagementInterface

logger = logging.getLogger(__name__)


class PluginDatabaseStorage:
    """Database operations through plugin delegation.

    Delegates all CRUD operations to state management plugins.
    Used in plugin mode after system initialization is complete.
    """

    def __init__(self, plugin: StateManagementInterface) -> None:
        """Initialize plugin database storage.

        Args:
            plugin: State management plugin instance implementing StateManagementInterface

        Raises:
            TypeError: If plugin does not implement StateManagementInterface
        """
        self._plugin = plugin
        logger.debug("PluginDatabaseStorage initialized with typed plugin interface")

    def write_state(
        self,
        namespace: str,
        enhanced_data: dict[str, object],
        calling_service: str | None = None,
        calling_namespace: str | None = None,
    ) -> ActionResult:
        """Write data via plugin delegation.

        Args:
            namespace: Target namespace for data
            enhanced_data: Data with standard fields already populated
            calling_service: Reserved interface parameter, unused in this implementation
            calling_namespace: Reserved interface parameter, unused in this implementation

        Returns:
            ActionResult from plugin execution

        Raises:
            FrameworkError: If plugin execution fails
        """
        # Note: calling_service and calling_namespace are not passed to plugin
        _ = calling_service, calling_namespace
        logger.debug("Plugin mode - delegating write operation to plugin")

        # Call typed interface method directly
        return self._plugin.write_state(namespace, enhanced_data)

    def read_state(self, namespace: str, query: dict[str, object]) -> ActionResult:
        """Read data via plugin delegation.

        Args:
            namespace: Target namespace to query
            query: Query parameters for data retrieval

        Returns:
            ActionResult from plugin execution

        Raises:
            FrameworkError: If plugin execution fails
        """
        return self._plugin.read_state(namespace, query)

    def update_state(
        self, namespace: str, query: dict[str, object], updates: dict[str, object]
    ) -> ActionResult:
        """Update data via plugin delegation.

        Args:
            namespace: Target namespace for updates
            query: Query to identify records to update
            updates: Update operations to apply

        Returns:
            ActionResult from plugin execution

        Raises:
            FrameworkError: If plugin execution fails
        """
        return self._plugin.update_state(namespace, query, updates)

    def upsert_state(self, namespace: str, data: dict[str, object]) -> ActionResult:
        """Upsert data via plugin delegation.

        Args:
            namespace: Target namespace for upsert
            data: Data containing table, record, and conflict_columns

        Returns:
            ActionResult from plugin execution

        Raises:
            FrameworkError: If plugin execution fails
        """
        return self._plugin.upsert_state(namespace, data)

    def delete_records(self, namespace: str, query: dict[str, object]) -> ActionResult:
        """Delete data via plugin delegation.

        Args:
            namespace: Target namespace for deletions
            query: Query to identify records to delete

        Returns:
            ActionResult from plugin execution

        Raises:
            FrameworkError: If plugin execution fails
        """
        return self._plugin.delete_records(namespace, query)

    def query_state(self, namespace: str, filters: dict[str, object]) -> ActionResult:
        """Query data via plugin delegation — DEPRECATED alias for ``read_state``.

        The dict is forwarded to the plugin unchanged, and both state plugins
        implement ``query_state`` as ``return self.read_state(...)``, so it is
        the whole ``{table, filters, limit?, unbounded?}`` query envelope and
        ``limit`` / ``unbounded`` are honoured. See
        :meth:`ananta.services.state_service.StateService.query_state`.

        Args:
            namespace: Target namespace to query
            filters: The ``{table, filters, limit?, unbounded?}`` query envelope.

        Returns:
            ActionResult from plugin execution

        Raises:
            FrameworkError: If plugin execution fails
        """
        return self._plugin.query_state(namespace, filters)

    def query_ordered(self, namespace: str, data: dict[str, object]) -> ActionResult:
        """Ordered, bounded, tie-safe query via plugin delegation.

        Args:
            namespace: Target namespace to query
            data: ``{table, filters, order_by, limit, after?, include_deleted?}``

        Returns:
            ActionResult from plugin execution

        Raises:
            FrameworkError: If plugin execution fails
        """
        return self._plugin.query_ordered(namespace, data)

    def execute_sql(self, sql_query: str, sql_params: list[object] | None = None) -> ActionResult:
        """Execute SQL query via plugin delegation.

        Args:
            sql_query: SQL query string to execute
            sql_params: Optional parameters for the SQL query

        Returns:
            ActionResult from plugin execution

        Raises:
            FrameworkError: If plugin execution fails
        """
        logger.debug("Plugin mode - delegating SQL execution to plugin")

        return self._plugin.execute_sql(sql_query, sql_params)
