"""Database Storage Strategy Protocol.

Defines the interface for different database storage implementations.
Implements Strategy pattern for bootstrap vs plugin mode CRUD operations.
"""

from typing import Protocol

from ananta.core.domain.types import ActionResult


class DatabaseStorageStrategy(Protocol):
    """Protocol defining database CRUD operations.

    Implementations handle bootstrap mode (in-memory) vs plugin mode (delegation).
    """

    def write_state(
        self,
        namespace: str,
        enhanced_data: dict[str, object],
        calling_service: str | None = None,
        calling_namespace: str | None = None,
    ) -> ActionResult:
        """Write data to the storage backend.

        Args:
            namespace: Target namespace for data
            enhanced_data: Data with standard fields already populated
            calling_service: Optional calling service identifier
            calling_namespace: Optional calling namespace

        Returns:
            ActionResult with operation status and details

        Raises:
            FrameworkError: If write operation fails
        """
        ...

    def read_state(self, namespace: str, query: dict[str, object]) -> ActionResult:
        """Read data from the storage backend.

        Args:
            namespace: Target namespace to query
            query: Query parameters for data retrieval

        Returns:
            ActionResult with retrieved data or error details

        Raises:
            FrameworkError: If read operation fails
        """
        ...

    def update_state(
        self, namespace: str, query: dict[str, object], updates: dict[str, object]
    ) -> ActionResult:
        """Update data in the storage backend.

        Args:
            namespace: Target namespace for updates
            query: Query to identify records to update
            updates: Update operations to apply

        Returns:
            ActionResult with operation status and details

        Raises:
            FrameworkError: If update operation fails
        """
        ...

    def delete_records(self, namespace: str, query: dict[str, object]) -> ActionResult:
        """Delete data from the storage backend.

        Args:
            namespace: Target namespace for deletions
            query: Query to identify records to delete

        Returns:
            ActionResult with operation status and details

        Raises:
            FrameworkError: If delete operation fails
        """
        ...

    def query_state(self, namespace: str, filters: dict[str, object]) -> ActionResult:
        """Query data with filtering from storage backend.

        Args:
            namespace: Target namespace to query
            filters: Filter criteria for data retrieval

        Returns:
            ActionResult with filtered data or error details

        Raises:
            FrameworkError: If query operation fails
        """
        ...

    def query_ordered(self, namespace: str, data: dict[str, object]) -> ActionResult:
        """Ordered, bounded, tie-safe query from the storage backend.

        Args:
            namespace: Target namespace to query
            data: ``{table, filters, order_by, limit, after?, include_deleted?}``

        Returns:
            ActionResult with the ordered page in ``data.records``

        Raises:
            FrameworkError: If query operation fails
        """
        ...

    def upsert_state(self, namespace: str, data: dict[str, object]) -> ActionResult:
        """Insert or update a record based on conflict columns.

        Args:
            namespace: Target namespace for upsert
            data: Must contain:
                - table: Target table name
                - record: Record data to insert/update
                - conflict_columns: List of columns to check for conflicts (e.g., ["id"])

        Returns:
            ActionResult with the record ID (generated or existing)

        Raises:
            FrameworkError: If upsert operation fails
        """
        ...

    def execute_sql(self, sql_query: str, sql_params: list[object] | None = None) -> ActionResult:
        """Execute raw SQL query against storage backend.

        Args:
            sql_query: SQL query string to execute
            sql_params: Optional parameters for the SQL query

        Returns:
            ActionResult with query results or error details

        Raises:
            FrameworkError: If SQL execution fails
        """
        ...
