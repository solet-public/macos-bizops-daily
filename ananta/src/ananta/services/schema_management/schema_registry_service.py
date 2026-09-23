"""Schema Registry Service.

Platform-level service for persisting schema metadata using CRUD interface.
Database-agnostic implementation works with any StateManagementInterface provider.
"""

import logging
import uuid
from dataclasses import dataclass

from ananta.core.domain.enums import ActionStatus
from ananta.interfaces.state_management_interface import StateManagementInterface
from ananta.types.schema_standardizer import StandardFieldDefinitions
from ananta.types.schema_types import ColumnDefinition, SchemaDefinition, TableSchema

logger = logging.getLogger(__name__)

# Error message constants
_ERR_TABLE_NOT_FOUND = "Table '%s' not found in schema definition. Available tables: %s"
_ERR_SCHEMA_PERSIST_FAILED = "Failed to persist schema for %s.%s: %s"
_ERR_COLUMN_RETRIEVAL_FAILED = "Error retrieving column names for %s: %s"


@dataclass
class ColumnRecordParams:
    """Parameters for building column record."""

    namespace: str
    table_name: str
    full_table_name: str
    col_name: str
    col_def: ColumnDefinition
    position: int


class SchemaRegistryService:
    """Database-agnostic schema registry using StateManagementInterface.

    Persists schema metadata to core__schema_registry table using CRUD operations.
    Works with any provider implementing StateManagementInterface (PostgreSQL today).
    """

    def __init__(self, state_plugin: StateManagementInterface) -> None:
        """Initialize with state management plugin.

        Args:
            state_plugin: Any plugin implementing StateManagementInterface
        """
        self._state_plugin = state_plugin
        self._standard_fields = StandardFieldDefinitions.get_reserved_field_names()
        logger.debug("SchemaRegistryService initialized with state management plugin")

    def persist_schema(self, namespace: str, table_name: str, schema_def: SchemaDefinition) -> None:
        """Persist table schema to registry using CRUD interface.

        Uses delete_records() to clear prior rows and write_state() for inserts.
        Works with any database provider - no custom SQL required.

        Args:
            namespace: Table namespace (e.g., 'core', 'plugin__my_plugin')
            table_name: Table name without namespace
            schema_def: Schema definition with column metadata

        Raises:
            ValueError: If table_name not found in schema_def
        """
        full_table_name = f"{namespace}__{table_name}"

        logger.debug(
            "Persisting schema for %s to registry using CRUD interface",
            full_table_name,
        )

        # Clear any prior registry rows for this table, then re-insert the current
        # columns below. The delete is HARD (soft_delete=False) to preserve the
        # original semantics: a removed column must physically drop, and the
        # (full_table_name, column_name) UNIQUE index would otherwise collide with
        # the re-insert if a soft-deleted row lingered. A 0-row clear is valid
        # (first-time registration), so only the envelope is checked.
        delete_result = self._state_plugin.delete_records(
            namespace="core",
            query={
                "table": "schema_registry",
                "filters": {"full_table_name": full_table_name},
                "soft_delete": False,
            },
        )
        if delete_result.get("action_status") != ActionStatus.COMPLETED:
            error_detail = delete_result.get("error", "Unknown error")
            raise RuntimeError(f"Failed to clear schema registry for {full_table_name}: {error_detail}")

        # Get the table schema
        if table_name not in schema_def.tables:
            error_msg = _ERR_TABLE_NOT_FOUND % (
                table_name,
                list(schema_def.tables.keys()),
            )
            raise ValueError(error_msg)

        table_schema: TableSchema = schema_def.tables[table_name]

        # Insert column definitions using write_state (CORRECT API)
        columns_persisted = 0
        for position, (col_name, col_def) in enumerate(table_schema.columns.items()):
            params = ColumnRecordParams(
                namespace=namespace,
                table_name=table_name,
                full_table_name=full_table_name,
                col_name=col_name,
                col_def=col_def,
                position=position,
            )
            record = self._build_column_record(params)

            # CORRECT: write_state with data dict containing table and record
            result = self._state_plugin.write_state(
                namespace="core", data={"table": "schema_registry", "record": record}
            )

            if result.get("action_status") != ActionStatus.COMPLETED:
                error_detail = result.get("error", "Unknown error")
                raise RuntimeError(
                    _ERR_SCHEMA_PERSIST_FAILED % (full_table_name, col_name, error_detail)
                )
            columns_persisted += 1

        logger.debug(
            "Successfully persisted %s/%s column definitions for %s",
            columns_persisted,
            len(table_schema.columns),
            full_table_name,
        )

    def get_column_names(self, full_table_name: str) -> list[str]:
        """Get ordered column names for a table from schema registry.

        Args:
            full_table_name: Full table name (namespace__table)

        Returns:
            List of column names in order, or None if not found
        """
        # CORRECT: read_state with query dict containing table and filters
        # Note: "filters" is the query-key the Postgres provider expects.
        result = self._state_plugin.read_state(
            namespace="core",
            query={
                "table": "schema_registry",
                "filters": {"full_table_name": full_table_name},
            },
        )

        # CORRECT: dict access, enum comparison
        if result.get("action_status") != ActionStatus.COMPLETED:
            error_detail = result.get("error", "Unknown error")
            raise RuntimeError(_ERR_COLUMN_RETRIEVAL_FAILED % (full_table_name, error_detail))

        data = result.get("data", {})
        records = data.get("records", [])
        if not isinstance(records, list) or not records:
            raise RuntimeError(_ERR_COLUMN_RETRIEVAL_FAILED % (full_table_name, "No records"))

        # Sort by column_position and return column names
        sorted_records = sorted(
            records, key=lambda r: r.get("column_position", 0) if isinstance(r, dict) else 0
        )
        column_names = [
            r["column_name"] for r in sorted_records if isinstance(r, dict) and "column_name" in r
        ]

        if not column_names:
            raise RuntimeError(_ERR_COLUMN_RETRIEVAL_FAILED % (full_table_name, "No column names"))

        return column_names

    def _build_column_record(self, params: ColumnRecordParams) -> dict[str, object]:
        """Build schema registry record for a column.

        Args:
            params: Column record parameters

        Returns:
            Dictionary record ready for write_state()
        """
        return {
            "id": str(uuid.uuid4()),
            "namespace": "core",  # schema_registry is always in core namespace
            "created_by": "ananta.services.schema_management.schema_registry_service",
            "updated_by": "ananta.services.schema_management.schema_registry_service",
            "external_id": None,
            "table_namespace": params.namespace,
            "table_name": params.table_name,
            "full_table_name": params.full_table_name,
            "column_name": params.col_name,
            "column_type": str(params.col_def.type.value),
            "column_position": params.position,
            "is_primary_key": int(params.col_def.primary_key),
            "is_not_null": int(params.col_def.not_null),
            "default_value": (
                str(params.col_def.default) if params.col_def.default is not None else None
            ),
            "is_unique": int(params.col_def.unique),
            "check_constraint": params.col_def.check,
            "is_standard_field": int(params.col_name in self._standard_fields),
            "column_description": params.col_def.description,
            "data_sensitivity": params.col_def.data_sensitivity,
        }
