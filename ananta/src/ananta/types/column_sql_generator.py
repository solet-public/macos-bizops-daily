"""
Column SQL Generator Service

Responsibility: Handle all column SQL generation operations for schema types
Dependencies: ColumnType, logging
Complexity: Medium-High - focused on SQL generation with comprehensive formatting logic

Extracted from ColumnDefinition god class (C11 complexity method)
"""

import logging
from typing import Literal, Protocol, runtime_checkable

from ananta.types.column_types import ColumnType

logger = logging.getLogger(__name__)

# Per W5.P §3.2: PG-native referential actions for column-level FK
# declarations. Matches ColumnDefinition.ReferentialAction.
_ReferentialAction = Literal[
    "NO ACTION", "RESTRICT", "CASCADE", "SET NULL", "SET DEFAULT",
]


@runtime_checkable
class SqlFunctionDetector(Protocol):
    """Protocol for SQL function detection services."""

    def is_sql_function(self, value: str) -> bool:
        """Check if a value is a SQL function."""
        ...


class ColumnSqlGenerator:
    """
    Service for generating SQL column definitions from ColumnDefinition objects.

    ARCHITECTURAL ROLE: Supporting service that extracts SQL generation logic
    from ColumnDefinition while maintaining data class integrity.

    This service handles:
    - Converting ColumnDefinition objects to SQL column definition strings
    - Managing SQL formatting for PRIMARY KEY, NOT NULL, UNIQUE constraints
    - Handling DEFAULT value formatting with proper quoting logic
    - Processing CHECK constraints and other column attributes
    - Delegating SQL function detection to specialized service
    """

    def __init__(self, sql_function_detector: SqlFunctionDetector | None = None) -> None:
        """Initialize ColumnSqlGenerator with optional function detector."""
        self.sql_function_detector = sql_function_detector

    def generate_column_sql(
        self,
        column_name: str,
        column_type: ColumnType,
        primary_key: bool = False,
        not_null: bool = False,
        default: object | None = None,
        unique: bool = False,
        check: str | None = None,
        type_params: dict[str, object] | None = None,
        foreign_key: tuple[str, str] | None = None,
        on_delete: _ReferentialAction = "NO ACTION",
        on_update: _ReferentialAction = "NO ACTION",
    ) -> str:
        """
        Generate SQL column definition from column properties.

        EXTRACTED FROM: ColumnDefinition.to_sql() - C(11) complexity

        Args:
            column_name: Name of the database column
            column_type: Type of the column (TEXT, INTEGER, etc.)
            primary_key: Whether column is a primary key
            not_null: Whether column has NOT NULL constraint
            default: Default value for the column
            unique: Whether column has UNIQUE constraint
            check: CHECK constraint expression
            type_params: Type-specific parameters (e.g., {"dimension": 384} for VECTOR)
            foreign_key: Optional (target_table, target_column) FK declaration
                (W5.P §3.2). When set, emits ``REFERENCES <target>(<col>)``
                with the configured ON DELETE / ON UPDATE actions.
            on_delete: Referential action when the referenced row is
                deleted. Ignored if ``foreign_key`` is None.
            on_update: Referential action when the referenced row's key
                is updated. Ignored if ``foreign_key`` is None.

        Returns:
            Complete SQL column definition string

        Example:
            "user_id INTEGER PRIMARY KEY NOT NULL"
            "created_at DATETIME DEFAULT (NOW() AT TIME ZONE 'UTC')"
            "embedding vector(384) NOT NULL"
            "memory_id TEXT NOT NULL REFERENCES actr_memory_plugin__memory(id) ON DELETE CASCADE"
        """
        # Generate base type string with parameters
        type_str = self._format_column_type(column_type, type_params)
        parts = [column_name, type_str]

        # Add constraints in standard SQL order
        if primary_key:
            parts.append("PRIMARY KEY")
        if not_null and not primary_key:
            parts.append("NOT NULL")
        if unique and not primary_key:
            parts.append("UNIQUE")

        # Handle default value formatting
        if default is not None:
            default_clause = self._format_default_value(default, column_type)
            if default_clause:
                parts.append(default_clause)

        # Add CHECK constraint if specified
        if check:
            parts.append(f"CHECK ({check})")

        # Per W5.P §3.2: append REFERENCES clause when a FK is declared.
        if foreign_key is not None:
            parts.append(self._format_foreign_key(foreign_key, on_delete, on_update))

        return " ".join(parts)

    @staticmethod
    def _format_foreign_key(
        foreign_key: tuple[str, str],
        on_delete: _ReferentialAction,
        on_update: _ReferentialAction,
    ) -> str:
        """Format a PG-native REFERENCES clause.

        ``foreign_key`` is ``(target_table, target_column)`` where the
        target_table is the already-namespace-prefixed table name (matches
        the ``namespace__table`` convention emitted by the Postgres-native
        DDL renderer / ``adoption.plan_fk_reconciliation``).
        """
        target_table, target_column = foreign_key
        return (
            f"REFERENCES {target_table}({target_column}) "
            f"ON DELETE {on_delete} ON UPDATE {on_update}"
        )

    def _format_column_type(
        self, column_type: ColumnType, type_params: dict[str, object] | None
    ) -> str:
        """
        Format column type with optional parameters.

        Args:
            column_type: The column type enum
            type_params: Optional type-specific parameters

        Returns:
            Formatted type string (e.g., "VECTOR" or "vector(384)")
        """
        base_type = column_type.value

        # Handle VECTOR type with dimension parameter
        if column_type == ColumnType.VECTOR and type_params:
            dimension = type_params.get("dimension")
            if dimension:
                # Use lowercase 'vector' for PostgreSQL pgvector extension
                return f"vector({dimension})"
            else:
                logger.error(
                    f"VECTOR type missing dimension parameter in type_params: {type_params}"
                )
                return "vector"  # Dimension-less vector (not recommended)

        return base_type

    def _format_default_value(self, default: object, column_type: ColumnType) -> str:
        """Format DEFAULT value with proper quoting, function detection, and type-aware coercion.

        Order: type-specific coercion (BOOLEAN 0/1 → FALSE/TRUE) → non-string
        literal → contract placeholder → quoted string default.
        """
        if column_type == ColumnType.BOOLEAN:
            coerced = self._coerce_boolean_default(default)
            if coerced is not None:
                return coerced

        if not isinstance(default, str):
            return f"DEFAULT {default}"

        contract_result = self._handle_contract_placeholder(default)
        if contract_result is not None:
            return contract_result

        return self._format_string_default(default)

    @staticmethod
    def _coerce_boolean_default(default: object) -> str | None:
        """Coerce 0/1/bool to ``DEFAULT FALSE``/``DEFAULT TRUE``; return ``None``
        for values that don't match the boolean-coercion rule.

        Plugin schemas declare boolean defaults as integer 0/1 historically; no DB
        accepts integer literals as boolean defaults, so coerce at emit time so the
        same declaration works on every backend.
        """
        if isinstance(default, bool):
            return "DEFAULT TRUE" if default else "DEFAULT FALSE"
        if isinstance(default, int) and default in (0, 1):
            return "DEFAULT TRUE" if default == 1 else "DEFAULT FALSE"
        return None

    def _handle_contract_placeholder(self, default: str) -> str | None:
        """
        Handle contract placeholders that shouldn't become literal SQL.

        Args:
            default: The default value string to check

        Returns:
            SQL DEFAULT clause if contract handled, None if not a contract
        """
        contract_mappings: dict[str, str] = {
            "__CONTRACT:auto_id_with_prefix__": "",
            "__CONTRACT:auto_timestamp_on_insert__": "DEFAULT (NOW() AT TIME ZONE 'UTC')",
            "__CONTRACT:auto_timestamp_on_update__": "DEFAULT (NOW() AT TIME ZONE 'UTC')",
        }

        if default in contract_mappings:
            return contract_mappings[default]

        if default.startswith("__CONTRACT:") and default.endswith("__"):
            logger.error(f"Unhandled contract placeholder: {default}")
            return ""

        return None

    def _format_string_default(self, default: str) -> str:
        """
        Format a string default value with proper quoting.

        Args:
            default: The string default value to format

        Returns:
            SQL DEFAULT clause with appropriate quoting
        """
        if default.startswith('"') and default.endswith('"'):
            return f"DEFAULT {default}"

        if self._is_sql_function(default):
            return f"DEFAULT {default}"

        return f"DEFAULT '{default}'"

    def _is_sql_function(self, value: str) -> bool:
        """Check if a value is a SQL function using the detector service."""
        if self.sql_function_detector is None:
            return False
        return self.sql_function_detector.is_sql_function(value)
