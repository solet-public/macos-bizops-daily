"""Schema creation helpers for PostgreSQL state plugin."""

import logging

from postgres_state_management_plugin.postgres_backend.provider import PostgresProvider

from .column_definition import normalize_columns

logger = logging.getLogger(__name__)


def extract_id_prefix_from_table_def(table_def: object, table_name: str) -> str:
    """Extract id_prefix from table definition.

    FAIL-FAST: Requires id_prefix to be explicitly defined in schema.
    No fallback patterns - every table MUST have an id_prefix defined.
    """
    if not isinstance(table_def, dict):
        raise ValueError(
            f"Table '{table_name}': table_def must be a dict, got {type(table_def).__name__}"
        )
    id_prefix = table_def.get("id_prefix")
    if not isinstance(id_prefix, str) or not id_prefix:
        raise ValueError(
            f"Table '{table_name}': id_prefix must be defined in TableSchema. "
            f"Every table requires an explicit id_prefix for ID generation."
        )
    return id_prefix


def extract_columns_from_table_def(table_def: object) -> dict[str, object] | None:
    """Extract columns dictionary from table definition."""
    if not isinstance(table_def, dict):
        return None
    if "columns" not in table_def:
        raise ValueError(
            "Table definition must have 'columns' key. "
            "Use SchemaDefinition/TableSchema types instead of flat dicts."
        )
    cols = table_def.get("columns")
    return cols if isinstance(cols, dict) else None


def create_tables_from_schema(
    provider: PostgresProvider, namespace: str, tables: dict[str, object]
) -> list[str]:
    created = []
    for table_name, table_def in tables.items():
        columns = extract_columns_from_table_def(table_def)
        if columns is None:
            continue
        id_prefix = extract_id_prefix_from_table_def(table_def, table_name)
        normalized = normalize_columns(columns)
        if normalized:
            provider.create_table(
                namespace=namespace,
                table=table_name,
                columns=normalized,
                table_prefix=id_prefix,
            )
            created.append(table_name)
    return created
