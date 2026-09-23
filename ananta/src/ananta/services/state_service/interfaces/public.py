"""State Service Public API - AI-discoverable operations."""

from abc import ABC, abstractmethod
from typing import Any

from ananta.core.actions.action_metadata import (
    MergeErrorProcessorCustomizations,
    ParameterMetadata,
    ParameterType,
    ReturnValueSchema,
)
from ananta.core.domain.types import ActionResult
from ananta.core.services.service_interface_decorator import service_interface_process


class StateManagementAPI(ABC):
    """Public state management operations - AI-discoverable via vector search."""

    @service_interface_process(
        name="write_state",
        provider="state_service",
        parameters={
            "namespace": ParameterMetadata(
                description="Target namespace to write to", required=True, type=ParameterType.STRING
            ),
            "data": ParameterMetadata(
                description="Data to write including table name and record(s)",
                required=True,
                type=ParameterType.OBJECT,
            ),
        },
        return_value_schema=ReturnValueSchema(
            type=ParameterType.OBJECT,
            description="Write result with generated IDs and insertion count",
            properties={
                "action_status": ParameterMetadata(
                    type=ParameterType.STRING,
                    description="Status: completed or failed",
                    required=False,
                ),
                "data": ParameterMetadata(
                    type=ParameterType.OBJECT,
                    description="Write result containing namespace and result with generated_id/inserted count",
                    required=False,
                ),
            },
            usage_patterns=[
                "Insert new records into the database",
                "Batch insert multiple records",
            ],
        ),
        is_enabled=True,
        error_processor_customizations=MergeErrorProcessorCustomizations()
    )
    @abstractmethod
    def write_state(self, namespace: str, data: dict[str, Any]) -> ActionResult:
        """Write data to state database."""
        ...

    @service_interface_process(
        name="read_state",
        provider="state_service",
        parameters={
            "namespace": ParameterMetadata(
                description="Target namespace to query", required=True, type=ParameterType.STRING
            ),
            "query": ParameterMetadata(
                description="Query parameters including table, filters, joins, aggregations",
                required=True,
                type=ParameterType.OBJECT,
            ),
        },
        return_value_schema=ReturnValueSchema(
            type=ParameterType.OBJECT,
            description="Query results with records and metadata",
            properties={
                "action_status": ParameterMetadata(
                    type=ParameterType.STRING,
                    description="Status: completed or failed",
                    required=False,
                ),
                "data": ParameterMetadata(
                    type=ParameterType.OBJECT, description="Query results", required=False
                ),
            },
            usage_patterns=[
                "Query state database for records",
                "Filter and retrieve application data",
            ],
        ),
        is_enabled=True,
        error_processor_customizations=MergeErrorProcessorCustomizations()
    )
    @abstractmethod
    def read_state(self, namespace: str, query: dict[str, Any]) -> ActionResult:
        """Read data from state database."""
        ...

    @service_interface_process(
        name="count",
        provider="state_service",
        is_discoverable=True,
        parameters={
            "namespace": ParameterMetadata(
                description="Namespace that owns the table",
                required=True,
                type=ParameterType.STRING,
            ),
            "data": ParameterMetadata(
                description=(
                    "Count query: {table, filters}. 'filters' uses the same "
                    "grammar as query_state (column=scalar / list = ANY / "
                    "{op: is_null|is_not_null} / {op: lt|lte|gt|gte, value}). A "
                    "'column' key is rejected. No automatic is_deleted "
                    "exclusion -- pass is_deleted in filters to scope to live "
                    "rows."
                ),
                required=True,
                type=ParameterType.OBJECT,
            ),
        },
        return_value_schema=ReturnValueSchema(
            type=ParameterType.OBJECT,
            description="Row count surfaced at data.result.value (integer >= 0; empty set -> 0)",
            properties={
                "action_status": ParameterMetadata(
                    type=ParameterType.STRING,
                    description="Status: completed or failed",
                    required=False,
                ),
                "data": ParameterMetadata(
                    type=ParameterType.OBJECT,
                    description="Envelope; the scalar count is at data.result.value",
                    required=False,
                ),
            },
            usage_patterns=[
                "Count matching rows without materializing them",
                "Index-backed COUNT over a filtered set",
            ],
        ),
        is_enabled=True,
        error_processor_customizations=MergeErrorProcessorCustomizations()
    )
    @abstractmethod
    def count(self, namespace: str, data: dict[str, Any]) -> ActionResult:
        """Count rows matching a filtered set; the scalar is at data.result.value."""
        ...

    @service_interface_process(
        name="max_value",
        provider="state_service",
        is_discoverable=True,
        parameters={
            "namespace": ParameterMetadata(
                description="Namespace that owns the table",
                required=True,
                type=ParameterType.STRING,
            ),
            "data": ParameterMetadata(
                description=(
                    "Max query: {table, column, filters}. 'column' is REQUIRED "
                    "(the column to aggregate). 'filters' uses the same grammar "
                    "as query_state. No automatic is_deleted exclusion."
                ),
                required=True,
                type=ParameterType.OBJECT,
            ),
        },
        return_value_schema=ReturnValueSchema(
            type=ParameterType.OBJECT,
            description=(
                "Column maximum surfaced at data.result.value (column-typed "
                "scalar, or null over an empty set; a TIMESTAMP column yields a "
                "naive datetime the caller normalizes)"
            ),
            properties={
                "action_status": ParameterMetadata(
                    type=ParameterType.STRING,
                    description="Status: completed or failed",
                    required=False,
                ),
                "data": ParameterMetadata(
                    type=ParameterType.OBJECT,
                    description="Envelope; the scalar maximum is at data.result.value",
                    required=False,
                ),
            },
            usage_patterns=[
                "Find the largest value of a column without shipping rows",
                "Get the most recent timestamp in a filtered set",
            ],
        ),
        is_enabled=True,
        error_processor_customizations=MergeErrorProcessorCustomizations()
    )
    @abstractmethod
    def max_value(self, namespace: str, data: dict[str, Any]) -> ActionResult:
        """Largest value of a column over a filtered set; scalar at data.result.value."""
        ...

    @service_interface_process(
        name="min_value",
        provider="state_service",
        is_discoverable=True,
        parameters={
            "namespace": ParameterMetadata(
                description="Namespace that owns the table",
                required=True,
                type=ParameterType.STRING,
            ),
            "data": ParameterMetadata(
                description=(
                    "Min query: {table, column, filters}. 'column' is REQUIRED "
                    "(the column to aggregate). 'filters' uses the same grammar "
                    "as query_state. No automatic is_deleted exclusion."
                ),
                required=True,
                type=ParameterType.OBJECT,
            ),
        },
        return_value_schema=ReturnValueSchema(
            type=ParameterType.OBJECT,
            description=(
                "Column minimum surfaced at data.result.value (column-typed "
                "scalar, or null over an empty set; a TIMESTAMP column yields a "
                "naive datetime the caller normalizes)"
            ),
            properties={
                "action_status": ParameterMetadata(
                    type=ParameterType.STRING,
                    description="Status: completed or failed",
                    required=False,
                ),
                "data": ParameterMetadata(
                    type=ParameterType.OBJECT,
                    description="Envelope; the scalar minimum is at data.result.value",
                    required=False,
                ),
            },
            usage_patterns=[
                "Find the smallest value of a column without shipping rows",
                "Get the earliest timestamp in a filtered set",
            ],
        ),
        is_enabled=True,
        error_processor_customizations=MergeErrorProcessorCustomizations()
    )
    @abstractmethod
    def min_value(self, namespace: str, data: dict[str, Any]) -> ActionResult:
        """Smallest value of a column over a filtered set; scalar at data.result.value."""
        ...

    @service_interface_process(
        name="delete_records",
        provider="state_service",
        parameters={
            "namespace": ParameterMetadata(
                description="Namespace containing the records to delete",
                required=True,
                type=ParameterType.STRING,
            ),
            "query": ParameterMetadata(
                description="Deletion query including table, filters, and optional soft_delete flag",
                required=True,
                type=ParameterType.OBJECT,
            ),
        },
        return_value_schema=ReturnValueSchema(
            type=ParameterType.OBJECT,
            description="Deletion status with metadata about removed records",
            properties={
                "action_status": ParameterMetadata(
                    type=ParameterType.STRING,
                    description="Status: completed or failed",
                    required=False,
                ),
                "data": ParameterMetadata(
                    type=ParameterType.OBJECT,
                    description="Deletion result payload with result.deleted count",
                    required=False,
                ),
            },
            usage_patterns=[
                "Clean up deterministic test data",
                "Purge stale records before reloading fixtures",
            ],
        ),
        is_enabled=True,
        error_processor_customizations=MergeErrorProcessorCustomizations()
    )
    @abstractmethod
    def delete_records(self, namespace: str, query: dict[str, Any]) -> ActionResult:
        """Delete records with optional soft_delete (default) or hard delete when explicitly disabled."""
        ...

    @service_interface_process(
        name="describe_schema",
        provider="state_service",
        parameters={
            "namespace": ParameterMetadata(
                description="Namespace to describe", required=True, type=ParameterType.STRING
            ),
        },
        return_value_schema=ReturnValueSchema(
            type=ParameterType.OBJECT,
            description="Schema definition with table structures",
            properties={
                "action_status": ParameterMetadata(
                    type=ParameterType.STRING,
                    description="Status: completed or failed",
                    required=False,
                ),
                "data": ParameterMetadata(
                    type=ParameterType.OBJECT, description="Schema definition", required=False
                ),
            },
            usage_patterns=[
                "Inspect database schema",
                "Understand data structure",
            ],
        ),
        is_enabled=True,
        error_processor_customizations=MergeErrorProcessorCustomizations()
    )
    @abstractmethod
    def describe_schema(self, namespace: str) -> ActionResult:
        """Get schema definition for namespace."""
        ...

    @service_interface_process(
        name="list_namespaces",
        provider="state_service",
        parameters={},
        return_value_schema=ReturnValueSchema(
            type=ParameterType.OBJECT,
            description="List of all namespaces",
            properties={
                "action_status": ParameterMetadata(
                    type=ParameterType.STRING,
                    description="Status: completed or failed",
                    required=False,
                ),
                "data": ParameterMetadata(
                    type=ParameterType.OBJECT, description="Namespace list", required=False
                ),
            },
            usage_patterns=[
                "Discover available namespaces",
                "List data partitions",
            ],
        ),
        is_enabled=True,
        error_processor_customizations=MergeErrorProcessorCustomizations(
        )
    )
    @abstractmethod
    def list_namespaces(self) -> ActionResult:
        """List all namespaces."""
        ...

    @service_interface_process(
        name="execute_sql",
        provider="state_service",
        parameters={
            "sql_query": ParameterMetadata(
                description="SQL query to execute", required=True, type=ParameterType.STRING
            ),
            "sql_params": ParameterMetadata(
                description="Query parameters for safe SQL parameterization",
                required=False,
                type=ParameterType.LIST,
            ),
        },
        return_value_schema=ReturnValueSchema(
            type=ParameterType.OBJECT,
            description="SQL query results",
            properties={
                "action_status": ParameterMetadata(
                    type=ParameterType.STRING,
                    description="Status: completed or failed",
                    required=False,
                ),
                "data": ParameterMetadata(
                    type=ParameterType.OBJECT, description="Query results", required=False
                ),
            },
            usage_patterns=[
                "Execute advanced SQL queries",
                "Perform complex database operations",
            ],
        ),
        is_enabled=True,
        error_processor_customizations=MergeErrorProcessorCustomizations()
    )
    @abstractmethod
    def execute_sql(
        self,
        sql_query: str,
        sql_params: list[Any] | None = None,
        calling_service: str = "StateService",
        calling_namespace: str = "ananta.services.state_service",
    ) -> ActionResult:
        """Execute raw SQL query."""
        ...
