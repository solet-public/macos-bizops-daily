"""Schema Management Service.

Provides focused schema management functionality extracted from StateService.
Implements Strategy pattern for bootstrap vs plugin mode operations.
"""

import logging
from typing import TypedDict

from ananta.core.domain.types import ActionResult
from ananta.types.schema_standardizer import (
    SchemaStandardizer,
    StandardFieldDefinitions,
)
from ananta.types.schema_types import ColumnDefinition

from .namespace_validator import NamespaceValidator
from .schema_storage_strategy import SchemaStorageStrategy

logger = logging.getLogger(__name__)


class TablesDict(TypedDict):
    """Type for the 'tables' entry in schema dictionaries."""

    pass


class SchemaDict(TypedDict, total=False):
    """Type for schema dictionaries with tables."""

    tables: dict[str, dict[str, object]]


class SchemaManagementService:
    """Manages schema operations with proper separation of concerns.

    Uses Strategy pattern to handle bootstrap vs plugin mode operations.
    Implements dependency injection for clean architecture.
    """

    def __init__(
        self,
        storage_strategy: SchemaStorageStrategy,
        namespace_validator: NamespaceValidator | None = None,
        schema_standardizer: SchemaStandardizer | None = None,
    ) -> None:
        """Initialize schema management service.

        Args:
            storage_strategy: Strategy for schema storage operations
            namespace_validator: Validator for namespace strings (created if None)
            schema_standardizer: Standardizer for schema fields (created if None)
        """
        self._storage_strategy = storage_strategy
        self._namespace_validator = namespace_validator or NamespaceValidator()
        self._schema_standardizer = schema_standardizer or SchemaStandardizer()

        logger.debug("SchemaManagementService initialized with dependency injection")

    def create_schema(self, namespace: str, schema: dict[str, object]) -> ActionResult:
        """Create schema with standard field enhancement.

        Args:
            namespace: Target namespace for schema creation
            schema: Raw schema definition

        Returns:
            ActionResult with creation status and details

        Raises:
            FrameworkError: If validation or creation fails
        """
        self._namespace_validator.validate_namespace(namespace)

        logger.debug(f"Creating schema for namespace: {namespace}")

        # Type narrow the tables value
        tables_value = schema.get("tables", {})
        if isinstance(tables_value, dict):
            logger.debug(f"Raw schema tables: {list(tables_value.keys())}")

        # Add standard fields before storage (core business logic)
        standardized_schema = self._add_standard_fields_to_schema(schema)

        # Type narrow the standardized tables value
        standardized_tables = standardized_schema.get("tables", {})
        if isinstance(standardized_tables, dict):
            logger.debug(f"Standardized schema tables: {list(standardized_tables.keys())}")

        # Delegate to storage strategy
        return self._storage_strategy.create_schema(namespace, standardized_schema)

    def describe_schema(self, namespace: str) -> ActionResult:
        """Retrieve schema definition for namespace.

        Args:
            namespace: Target namespace to describe

        Returns:
            ActionResult with schema definition or error details

        Raises:
            FrameworkError: If validation or retrieval fails
        """
        self._namespace_validator.validate_namespace(namespace)

        return self._storage_strategy.describe_schema(namespace)

    def initialize_database(self) -> ActionResult:
        """Initialize database backend.

        Returns:
            ActionResult indicating initialization success/failure

        Raises:
            FrameworkError: If initialization fails
        """
        logger.debug("Initializing database via storage strategy")
        return self._storage_strategy.initialize_database()

    def _add_standard_fields_to_schema(self, schema: dict[str, object]) -> dict[str, object]:
        """Add standard fields to all tables in schema.

        Core business logic for schema standardization.

        Args:
            schema: Raw schema definition

        Returns:
            Schema with standard fields added to all tables
        """
        if "tables" not in schema:
            return schema

        # Type narrow the tables value
        tables_obj = schema["tables"]
        if not isinstance(tables_obj, dict):
            return schema

        # Create a typed dict for tables to ensure proper typing
        standardized_tables: dict[str, dict[str, object]] = {}
        standard_fields = StandardFieldDefinitions.get_standard_fields()

        logger.debug(f"Adding standard fields: {list(standard_fields.keys())}")

        for table_name, table_columns_obj in tables_obj.items():
            # Type narrow table columns
            if not isinstance(table_columns_obj, dict):
                continue

            # Convert to dict[str, object] explicitly
            table_columns: dict[str, object] = dict(table_columns_obj)
            enhanced_columns = self._enhance_table_with_standard_fields(
                table_name, table_columns, standard_fields
            )
            standardized_tables[table_name] = enhanced_columns

        # Return as dict[str, object] with properly typed tables
        standardized_schema: dict[str, object] = {"tables": standardized_tables}
        return standardized_schema

    def _enhance_table_with_standard_fields(
        self,
        table_name: str,
        table_columns: dict[str, object],
        standard_fields: dict[str, ColumnDefinition],
    ) -> dict[str, object]:
        """Enhance a single table with standard fields.

        Args:
            table_name: Name of table being enhanced
            table_columns: Existing column definitions
            standard_fields: Standard fields to add if missing

        Returns:
            Enhanced column definitions with standard fields

        Raises:
            ValueError: If schema attempts to override a reserved standard field
        """
        logger.debug(f"Processing table {table_name} with {len(table_columns)} columns")

        # FAIL-FAST: Reject schemas that override reserved standard fields
        # Standard fields have specific constraints (e.g., external_id UNIQUE) that
        # must not be accidentally removed by business schemas
        self._validate_no_standard_field_overrides(table_name, table_columns, standard_fields)

        enhanced_columns = dict(table_columns)

        for field_name, column_def in standard_fields.items():
            self._add_standard_field_if_missing(
                enhanced_columns, field_name, column_def, table_name
            )

        logger.debug(f"Table {table_name} now has {len(enhanced_columns)} columns")
        return enhanced_columns

    # Standard fields that are fully protected - no overrides allowed
    PROTECTED_STANDARD_FIELDS: frozenset[str] = frozenset({
        "id",           # Primary key - platform managed
        "namespace",    # Routing key - platform managed
        "created_at",   # Auto-timestamp - platform managed
        "updated_at",   # Auto-timestamp - platform managed
        "created_by",   # Attribution - platform managed
        "updated_by",   # Attribution - platform managed
        "is_deleted",   # Soft delete flag - platform managed
    })

    # Fields that allow specific constraint additions (not removals)
    # - name: can add unique=True and/or not_null=True
    # - external_id: can add not_null=True (unique is already platform-enforced)
    CONSTRAINABLE_FIELDS: frozenset[str] = frozenset({"name", "external_id"})

    def _validate_no_standard_field_overrides(
        self,
        table_name: str,
        table_columns: dict[str, object],
        _standard_fields: dict[str, ColumnDefinition],
    ) -> None:
        """Validate standard field overrides.

        Rules:
        - PROTECTED_STANDARD_FIELDS: No overrides allowed
        - CONSTRAINABLE_FIELDS (name, external_id): Can add constraints only
          - name: can add unique=True and/or not_null=True
          - external_id: can add not_null=True (unique is platform-enforced)

        Args:
            table_name: Name of table being validated
            table_columns: Column definitions from business schema
            _standard_fields: Platform standard field definitions (unused, for API compat)

        Raises:
            ValueError: If validation fails
        """
        # Check fully protected fields
        overridden_protected = set(table_columns.keys()) & self.PROTECTED_STANDARD_FIELDS
        if overridden_protected:
            fields_list = ", ".join(sorted(overridden_protected))
            raise ValueError(
                f"SCHEMA VALIDATION FAILED: Table '{table_name}' attempts to override "
                f"protected standard field(s): [{fields_list}]. "
                f"These fields are platform-managed and cannot be redefined."
            )

        # Validate external_id override preserves UNIQUE constraint
        #
        # DELIBERATE ASYMMETRY (schema-debt-external-id lane, 2a, 2026-08-06):
        # the ColumnDefinition-based path (SchemaStandardizer._validate_
        # constrainable_field, ananta/types/schema_standardizer.py) now lets
        # a table opt OUT of this via an explicit unique=False override —
        # this path (the raw-dict/bootstrap dialect, reached from
        # StateService.create_schema for the framework/generic-plugin/
        # discovery_service namespaces, never session_ledger) still hard-
        # rejects it. No caller on THIS path has hit the composite-identity
        # need yet, so relaxing it here would be speculative capability with
        # no red-first leg to prove it against. Path-2 parity + unifying
        # both validators into one implementation so they can't drift again
        # is a tracked follow-on, not done in 2a — see the "P2 2a follow-on"
        # section of
        # workbench/2026-08-06_schema_debt_external_id_findings_schema-debt-impl.md
        # before relaxing this. A pin-leg
        # (ananta/tests/services/state_service/schema_management_service_external_id_pin_smoke.py)
        # reds if this rule changes out from under that record.
        if "external_id" in table_columns:
            col_def = table_columns["external_id"]
            # Column definitions here are SQL strings like "TEXT UNIQUE NOT NULL"
            if isinstance(col_def, str) and "UNIQUE" not in col_def.upper():
                raise ValueError(
                    f"SCHEMA VALIDATION FAILED: Table '{table_name}' overrides 'external_id' "
                    f"without UNIQUE constraint. The platform requires external_id to be unique. "
                    f"Either remove the override or include UNIQUE."
                )

    def _add_standard_field_if_missing(
        self,
        enhanced_columns: dict[str, object],
        field_name: str,
        column_def: ColumnDefinition,
        table_name: str,
    ) -> None:
        """Add a standard field if it doesn't already exist.

        Args:
            enhanced_columns: Column definitions being modified
            field_name: Name of standard field to add
            column_def: Standard field definition
            table_name: Name of table (for logging)
        """
        if field_name not in enhanced_columns:
            field_definition = self._convert_column_def_to_sql_format(field_name, column_def)
            enhanced_columns[field_name] = field_definition
            logger.debug(
                f"Added standard field {field_name} to table {table_name}: {field_definition}"
            )

    def _convert_column_def_to_sql_format(
        self, field_name: str, column_def: ColumnDefinition
    ) -> str:
        """Convert column definition to SQL format string.

        Args:
            field_name: Name of the field
            column_def: Column definition to convert

        Returns:
            SQL string representation of the column (e.g., "TEXT PRIMARY KEY", "TEXT UNIQUE")
        """
        # Use ColumnDefinition's to_sql() method and extract the type/constraints part
        # to_sql() returns "field_name TYPE CONSTRAINTS", we need "TYPE CONSTRAINTS"
        full_sql = column_def.to_sql(field_name)

        # Split and remove the field name part
        parts = full_sql.split(" ", 1)
        if len(parts) > 1:
            return parts[1]  # Everything after the field name
        else:
            return column_def.type.value  # Fallback to just the type
