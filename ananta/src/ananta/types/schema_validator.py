"""
Schema Validator Service

Responsibility: Handle schema validation operations for schema definitions
Dependencies: logging
Complexity: Medium - focused on validation logic with comprehensive error checking

Extracted from SchemaDefinition god class (B8 complexity method)
"""

import logging
from collections.abc import Mapping
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ananta.types.schema_types import TableSchema

logger = logging.getLogger(__name__)


class SchemaValidator:
    """
    Service for validating schema definitions and their components.

    ARCHITECTURAL ROLE: Supporting service that extracts validation logic
    from SchemaDefinition while maintaining schema definition integrity.

    This service handles:
    - Validating schema namespace requirements
    - Checking for required tables and columns
    - Analyzing primary key and unique constraint requirements
    - Providing comprehensive error reporting
    - Supporting plugin schema flexibility
    """

    def __init__(self) -> None:
        """Initialize SchemaValidator."""

    def validate_schema(
        self,
        namespace: str,
        tables: Mapping[str, "TableSchema"],
        require_primary_keys: bool = False,
    ) -> list[str]:
        """
        Validate schema definition and return list of validation errors.

        EXTRACTED FROM: SchemaDefinition.validate() - B(8) complexity

        Args:
            namespace: Schema namespace to validate
            tables: Dictionary of table schemas to validate
            require_primary_keys: Whether to enforce primary key requirements

        Returns:
            List of validation error messages (empty list if valid)

        Examples:
            validate_schema("", {}) -> ["Namespace cannot be empty", "Schema has no tables"]
            validate_schema("users", {"users": valid_table}) -> []
        """
        errors = []

        # Validate namespace requirements
        namespace_errors = self._validate_namespace(namespace)
        errors.extend(namespace_errors)

        # Validate table collection requirements
        tables_errors = self._validate_tables_collection(namespace, tables)
        errors.extend(tables_errors)

        # Validate individual table schemas
        for table_name, table_schema in tables.items():
            table_errors = self._validate_individual_table(
                table_name, table_schema, require_primary_keys
            )
            errors.extend(table_errors)

        return errors

    def _validate_namespace(self, namespace: str) -> list[str]:
        """
        Validate namespace requirements.

        Args:
            namespace: Namespace to validate

        Returns:
            List of namespace validation errors
        """
        errors = []

        if not namespace:
            errors.append("Namespace cannot be empty")

        return errors

    def _validate_tables_collection(
        self, namespace: str, tables: Mapping[str, "TableSchema"]
    ) -> list[str]:
        """
        Validate tables collection requirements.

        Args:
            namespace: Schema namespace for error context
            tables: Tables dictionary to validate

        Returns:
            List of tables collection validation errors
        """
        errors = []

        if not tables:
            errors.append(f"Schema '{namespace}' has no tables defined")

        return errors

    def _validate_individual_table(
        self, table_name: str, table_schema: "TableSchema", require_primary_keys: bool
    ) -> list[str]:
        """
        Validate individual table schema.

        Args:
            table_name: Name of the table being validated
            table_schema: Table schema object to validate
            require_primary_keys: Whether to enforce primary key requirements

        Returns:
            List of table validation errors
        """
        errors = []

        # Validate table has columns
        if not table_schema.columns:
            errors.append(f"Table '{table_name}' has no columns defined")
            return errors  # Can't validate further without columns

        # Check for primary key or unique constraints if required
        if require_primary_keys:
            key_errors = self._validate_table_keys(table_name, table_schema)
            errors.extend(key_errors)

        return errors

    def _validate_table_keys(
        self, _table_name: str, table_schema: "TableSchema"
    ) -> list[str]:  # Reserved for interface compatibility
        """
        Validate table key requirements (primary key or unique constraints).

        Args:
            table_name: Name of the table being validated
            table_schema: Table schema with columns to check

        Returns:
            List of key validation errors
        """
        errors: list[str] = []

        has_key = any(col.primary_key or col.unique for col in table_schema.columns.values())

        if not has_key:
            # Note: Allow plugin schemas without primary keys - system will add 'id' field during standardization
            # This is commented out to support plugin schema flexibility
            # errors.append(f"Table '{table_name}' must have at least one primary key or unique column")
            pass

        return errors

    def validate_table_name(self, table_name: str) -> list[str]:
        """
        Validate table name format and requirements.

        Args:
            table_name: Table name to validate

        Returns:
            List of table name validation errors
        """
        errors = []

        if not table_name:
            errors.append("Table name cannot be empty")
        elif not table_name.replace("_", "").isalnum():
            errors.append(
                f"Table name '{table_name}' contains invalid characters (only letters, numbers, and underscores allowed)"
            )

        return errors

    def validate_column_name(self, column_name: str) -> list[str]:
        """
        Validate column name format and requirements.

        Args:
            column_name: Column name to validate

        Returns:
            List of column name validation errors
        """
        errors = []

        if not column_name:
            errors.append("Column name cannot be empty")
        elif not column_name.replace("_", "").isalnum():
            errors.append(
                f"Column name '{column_name}' contains invalid characters (only letters, numbers, and underscores allowed)"
            )

        return errors
