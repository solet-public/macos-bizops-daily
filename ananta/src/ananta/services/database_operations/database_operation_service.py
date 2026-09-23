"""Database Operation Service.

Provides focused database operation functionality extracted from StateService.
Implements Strategy pattern for bootstrap vs plugin mode CRUD operations.
"""

import logging

from ananta.core.domain.types import ActionResult
from ananta.services.schema_management import NamespaceValidator

from .database_storage_strategy import DatabaseStorageStrategy

logger = logging.getLogger(__name__)


class DatabaseOperationService:
    """Manages database CRUD operations with proper separation of concerns.

    Uses Strategy pattern to handle bootstrap vs plugin mode operations.
    Implements dependency injection for clean architecture.
    """

    def __init__(
        self,
        storage_strategy: DatabaseStorageStrategy,
        namespace_validator: NamespaceValidator | None = None,
    ) -> None:
        """Initialize database operation service.

        Args:
            storage_strategy: Strategy for database storage operations
            namespace_validator: Validator for namespace strings (created if None)
        """
        self._storage_strategy = storage_strategy
        self._namespace_validator = namespace_validator or NamespaceValidator()

        logger.debug("DatabaseOperationService initialized with dependency injection")

    def write_state(
        self,
        namespace: str,
        enhanced_data: dict[str, object],
        calling_service: str | None = None,
        calling_namespace: str | None = None,
    ) -> ActionResult:
        """Write data with namespace validation and proper delegation.

        Args:
            namespace: Target namespace for data writing
            enhanced_data: Data with standard fields already populated
            calling_service: Optional calling service identifier
            calling_namespace: Optional calling namespace

        Returns:
            ActionResult with write status and details

        Raises:
            FrameworkError: If validation fails or write operation fails
        """
        self._namespace_validator.validate_namespace(namespace)

        logger.debug(f"Writing data to namespace: {namespace}")

        # Delegate to storage strategy
        return self._storage_strategy.write_state(
            namespace, enhanced_data, calling_service, calling_namespace
        )

    def read_state(self, namespace: str, query: dict[str, object]) -> ActionResult:
        """Read data with namespace validation and proper delegation.

        Args:
            namespace: Target namespace to read from
            query: Query parameters for data retrieval

        Returns:
            ActionResult with retrieved data or error details

        Raises:
            FrameworkError: If validation fails or read operation fails
        """
        self._namespace_validator.validate_namespace(namespace)

        return self._storage_strategy.read_state(namespace, query)

    def update_state(
        self, namespace: str, query: dict[str, object], updates: dict[str, object]
    ) -> ActionResult:
        """Update data with namespace validation and proper delegation.

        Args:
            namespace: Target namespace for updates
            query: Query to identify records to update
            updates: Update operations to apply

        Returns:
            ActionResult with update status and details

        Raises:
            FrameworkError: If validation fails or update operation fails
        """
        self._namespace_validator.validate_namespace(namespace)

        logger.debug(f"Updating data in namespace: {namespace}")

        # Delegate to storage strategy
        return self._storage_strategy.update_state(namespace, query, updates)

    def upsert_state(self, namespace: str, data: dict[str, object]) -> ActionResult:
        """Upsert data with namespace validation and proper delegation.

        Args:
            namespace: Target namespace for upsert
            data: Data containing table, record, and conflict_columns

        Returns:
            ActionResult with upsert status and details

        Raises:
            FrameworkError: If validation fails or upsert operation fails
        """
        self._namespace_validator.validate_namespace(namespace)

        logger.debug(f"Upserting data in namespace: {namespace}")

        # Inject namespace into record (required by schema standardizer)
        record = data.get("record")
        if isinstance(record, dict) and "namespace" not in record:
            record["namespace"] = namespace

        # Delegate to storage strategy
        return self._storage_strategy.upsert_state(namespace, data)

    def delete_records(self, namespace: str, query: dict[str, object]) -> ActionResult:
        """Delete data with namespace validation and proper delegation.

        Args:
            namespace: Target namespace for deletions
            query: Query to identify records to delete

        Returns:
            ActionResult with deletion status and details

        Raises:
            FrameworkError: If validation fails or delete operation fails
        """
        self._namespace_validator.validate_namespace(namespace)

        logger.debug(f"Deleting data from namespace: {namespace}")

        # Delegate to storage strategy
        return self._storage_strategy.delete_records(namespace, query)

    def query_state(self, namespace: str, filters: dict[str, object]) -> ActionResult:
        """DEPRECATED alias for :meth:`read_state` — prefer ``read_state``.

        The ``filters`` dict is passed through unchanged and becomes
        ``read_state``'s ``query``, so it is the whole ``{table, filters,
        limit?, unbounded?}`` envelope rather than just a filter mapping —
        ``limit`` and ``unbounded`` are honoured here. See
        :meth:`ananta.services.state_service.StateService.query_state` for the
        full contract and the deprecation rationale.

        Args:
            namespace: Target namespace to query
            filters: The ``{table, filters, limit?, unbounded?}`` query envelope.

        Returns:
            ActionResult with the records, or error details

        Raises:
            FrameworkError: If validation fails or query operation fails
        """
        self._namespace_validator.validate_namespace(namespace)

        return self._storage_strategy.query_state(namespace, filters)

    def query_ordered(self, namespace: str, data: dict[str, object]) -> ActionResult:
        """Ordered, bounded, tie-safe query with namespace validation.

        Args:
            namespace: Target namespace to query
            data: ``{table, filters, order_by, limit, after?, include_deleted?}``

        Returns:
            ActionResult with the ordered page in ``data.records``

        Raises:
            FrameworkError: If validation fails or query operation fails
        """
        self._namespace_validator.validate_namespace(namespace)

        return self._storage_strategy.query_ordered(namespace, data)

    def execute_sql(self, sql_query: str, sql_params: list[object] | None = None) -> ActionResult:
        """Execute SQL query with proper delegation.

        Args:
            sql_query: SQL query string to execute
            sql_params: Optional parameters for the SQL query

        Returns:
            ActionResult with query results or error details

        Raises:
            FrameworkError: If SQL execution fails
        """
        logger.debug("Executing SQL query via storage strategy")

        # Delegate to storage strategy
        return self._storage_strategy.execute_sql(sql_query, sql_params)
