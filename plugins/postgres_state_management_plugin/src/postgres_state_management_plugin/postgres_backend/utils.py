"""
PostgreSQL State Plugin Utilities

Type mappings and helper functions for PostgreSQL database operations.
"""

from typing import Final

from ananta.types.column_types import ColumnType

# ColumnType to PostgreSQL type mapping
COLUMN_TYPE_MAP: Final[dict[ColumnType, str]] = {
    ColumnType.TEXT: "TEXT",
    ColumnType.INTEGER: "INTEGER",
    ColumnType.REAL: "REAL",
    ColumnType.BLOB: "BYTEA",
    ColumnType.DATETIME: "TIMESTAMP",
    ColumnType.BOOLEAN: "BOOLEAN",
    ColumnType.VECTOR: "vector",  # pgvector extension type, dimension specified via type_params
    ColumnType.JSON: "JSONB",
}


def get_postgres_type(column_type: ColumnType) -> str:
    """
    Convert ColumnType enum to PostgreSQL type string.

    Args:
        column_type: The ColumnType enum value

    Returns:
        PostgreSQL type string

    Raises:
        ValueError: If column_type is not supported
    """
    if column_type not in COLUMN_TYPE_MAP:
        raise ValueError(f"Unsupported column type: {column_type}")
    return COLUMN_TYPE_MAP[column_type]


def build_table_name(namespace: str, table: str) -> str:
    """
    Build fully qualified table name with namespace.

    Pattern: {namespace}__{table}

    Args:
        namespace: The namespace identifier
        table: The table name

    Returns:
        Fully qualified table name
    """
    return f"{namespace}__{table}"


def escape_identifier(identifier: str) -> str:
    """
    Escape SQL identifier for PostgreSQL.

    Args:
        identifier: The identifier to escape

    Returns:
        Escaped identifier safe for SQL queries
    """
    # PostgreSQL uses double quotes for identifiers
    # Escape any existing double quotes by doubling them
    escaped = identifier.replace('"', '""')
    return f'"{escaped}"'
