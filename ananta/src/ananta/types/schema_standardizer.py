from ananta.types.column_types import ColumnType
from ananta.types.schema_types import (
    ColumnDefinition,
    SchemaDefinition,
    TableSchema,
)


class SystemFieldContract:
    """Database-agnostic contract definition for system field behavior"""

    def __init__(self, contract_type: str, responsibility: str, **kwargs: object) -> None:
        self.contract_type = contract_type
        self.responsibility = responsibility  # 'database_plugin' | 'state_interface'
        self.metadata = kwargs


class StandardFieldDefinitions:
    @staticmethod
    def get_standard_fields() -> dict[str, ColumnDefinition]:
        """Get standard fields with database-agnostic contract definitions.

        Data sensitivity values (0.0=public, 1.0=restricted):
        - System identifiers: 0.3 (low, needed for references)
        - Timestamps: 0.2 (low, general metadata)
        - User attribution: 0.6 (medium-high, could reveal PII)
        - Internal flags: 0.1 (very low, operational metadata)
        """
        return {
            "id": ColumnDefinition(
                type=ColumnType.TEXT,
                primary_key=True,
                # Database-agnostic contract - plugin interprets this
                default="__CONTRACT:auto_id_with_prefix__",
                data_sensitivity=0.3,  # Low - needed for references
            ),
            "external_id": ColumnDefinition(
                type=ColumnType.TEXT,
                unique=True,
                data_sensitivity=0.5,  # Medium - could reveal external integrations
            ),
            "namespace": ColumnDefinition(
                type=ColumnType.TEXT,
                not_null=True,
                data_sensitivity=0.2,  # Low - internal routing
            ),
            "created_at": ColumnDefinition(
                type=ColumnType.DATETIME,
                # Database-agnostic contract - plugin interprets this
                default="__CONTRACT:auto_timestamp_on_insert__",
                data_sensitivity=0.2,  # Low - general metadata
            ),
            "updated_at": ColumnDefinition(
                type=ColumnType.DATETIME,
                # Database-agnostic contract - plugin interprets this
                default="__CONTRACT:auto_timestamp_on_update__",
                data_sensitivity=0.2,  # Low - general metadata
            ),
            "created_by": ColumnDefinition(
                type=ColumnType.TEXT,
                data_sensitivity=0.6,  # Medium-high - could reveal user info
            ),
            "updated_by": ColumnDefinition(
                type=ColumnType.TEXT,
                data_sensitivity=0.6,  # Medium-high - could reveal user info
            ),
            "name": ColumnDefinition(
                type=ColumnType.TEXT,
                data_sensitivity=0.3,  # Low - business field, context-dependent
            ),
            "is_deleted": ColumnDefinition(
                type=ColumnType.INTEGER,
                default="0",
                data_sensitivity=0.1,  # Very low - operational flag
            ),
        }

    @staticmethod
    def get_system_field_contracts() -> dict[str, SystemFieldContract]:
        """Get database-agnostic contracts for system field behaviors"""
        return {
            "id": SystemFieldContract(
                contract_type="auto_id_with_prefix",
                responsibility="database_plugin",
                prefix_source="table_metadata",
                validation="must_be_unique_per_table",
            ),
            "created_at": SystemFieldContract(
                contract_type="auto_timestamp_on_insert",
                responsibility="database_plugin",
                format="ISO8601_UTC",
                immutable=True,
            ),
            "updated_at": SystemFieldContract(
                contract_type="auto_timestamp_on_update",
                responsibility="database_plugin",
                format="ISO8601_UTC",
                trigger_required=True,
            ),
            "created_by": SystemFieldContract(
                contract_type="interface_execution_context",
                responsibility="state_interface",
                default="ananta.services.state_service",
                required=True,
            ),
            "updated_by": SystemFieldContract(
                contract_type="interface_execution_context",
                responsibility="state_interface",
                source="current_operation_context",
                required=True,
            ),
            "namespace": SystemFieldContract(
                contract_type="interface_namespace_injection",
                responsibility="state_interface",
                source="calling_namespace",
                required=True,
            ),
            "external_id": SystemFieldContract(
                contract_type="interface_optional_field",
                responsibility="state_interface",
                nullable=True,
            ),
            "name": SystemFieldContract(
                contract_type="interface_optional_field",
                responsibility="state_interface",
                nullable=True,
            ),
            "is_deleted": SystemFieldContract(
                contract_type="soft_delete_flag",
                responsibility="state_interface",
                default=0,
                required=True,
            ),
        }

    @staticmethod
    def get_reserved_field_names() -> set[str]:
        # Fields managed by state service interface (abstraction layer)
        # Reserved for current and future standardization
        return {
            "id",
            "external_id",
            "namespace",
            "created_at",
            "updated_at",
            "last_read_at",
            "created_by",
            "updated_by",
            "name",
            "is_deleted",
        }


class SchemaStandardizer:
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

    # Fields that allow specific constraint additions/relaxations:
    # - name: can add unique=True and/or not_null=True
    # - external_id: can add not_null=True, OR declare unique=False to opt
    #   out of the platform-wide standalone uniqueness for a table whose true
    #   identity is composite and expressed via its own indexes instead (2a,
    #   2026-08-06 — schema-debt-external-id lane; see
    #   workbench/2026-08-06_schema_debt_external_id_findings_schema-debt-impl.md).
    #   unique=True needs no override at all — it's the platform default.
    CONSTRAINABLE_FIELDS: frozenset[str] = frozenset({"name", "external_id"})

    def __init__(self) -> None:
        self.standard_fields = StandardFieldDefinitions.get_standard_fields()
        self.reserved_names = StandardFieldDefinitions.get_reserved_field_names()

    def standardize_schema(self, schema_def: SchemaDefinition) -> SchemaDefinition:
        standardized_tables = {}

        for table_name, table_schema in schema_def.tables.items():
            standardized_tables[table_name] = self._standardize_table(table_name, table_schema)

        return SchemaDefinition(
            namespace=schema_def.namespace,
            version=schema_def.version,
            description=schema_def.description,
            tables=standardized_tables,
        )

    def _standardize_table(
        self, table_name: str, table_schema: TableSchema
    ) -> TableSchema:
        """Standardize table by adding platform fields and validating overrides.

        FAIL-FAST: Rejects schemas that override protected standard fields.
        Protected fields have critical constraints (e.g., external_id UNIQUE)
        that must not be silently dropped.

        Customizable fields (name, namespace, etc.) can still be overridden
        to add stricter constraints.
        """
        # FAIL-FAST: Validate no protected field overrides
        self._validate_no_protected_field_overrides(table_name, table_schema)

        standardized_columns: dict[str, ColumnDefinition] = {}

        # Start with standard fields as base
        for field_name, field_def in self.standard_fields.items():
            standardized_columns[field_name] = field_def

        # Add/override with business-specific fields
        # Only CUSTOMIZABLE fields can be overridden (name, namespace, etc.)
        for col_name, col_def in table_schema.columns.items():
            standardized_columns[col_name] = col_def

        return TableSchema(
            table_name=table_schema.table_name,
            columns=standardized_columns,
            indexes=table_schema.indexes,
            check_constraints=table_schema.check_constraints,
            with_history=table_schema.with_history,
            description=table_schema.description,
            id_prefix=table_schema.id_prefix,
            data_sensitivity=table_schema.data_sensitivity,
        )

    def _validate_no_protected_field_overrides(
        self, table_name: str, table_schema: TableSchema
    ) -> None:
        """Validate standard field overrides.

        Rules:
        - PROTECTED_STANDARD_FIELDS: No overrides allowed
        - CONSTRAINABLE_FIELDS (name, external_id): Can add constraints, and
          external_id can additionally relax to unique=False
          - name: can add unique=True and/or not_null=True
          - external_id: can add not_null=True; unique=True needs no
            override; unique=False opts a table out of the platform-wide
            standalone uniqueness when its true identity is composite and
            expressed via the table's own indexes instead

        Args:
            table_name: Name of table being validated
            table_schema: Table schema to validate

        Raises:
            ValueError: If validation fails
        """
        # Check fully protected fields
        overridden_protected = set(table_schema.columns.keys()) & self.PROTECTED_STANDARD_FIELDS
        if overridden_protected:
            fields_list = ", ".join(sorted(overridden_protected))
            raise ValueError(
                f"SCHEMA VALIDATION FAILED: Table '{table_name}' attempts to override "
                f"protected standard field(s): [{fields_list}]. "
                f"These fields are platform-managed and cannot be redefined."
            )

        # Validate constrainable field overrides
        for field_name in self.CONSTRAINABLE_FIELDS:
            if field_name in table_schema.columns:
                self._validate_constrainable_field(
                    table_name, field_name, table_schema.columns[field_name]
                )

    def _validate_constrainable_field(
        self, table_name: str, field_name: str, column_def: ColumnDefinition
    ) -> None:
        """Validate that a constrainable field override only adds allowed constraints.

        Args:
            table_name: Name of table being validated
            field_name: Name of field being validated
            column_def: The override column definition

        Raises:
            ValueError: If override removes or changes disallowed properties
        """
        platform_def = self.standard_fields.get(field_name)
        if not platform_def:
            return

        # external_id: unique=True (the platform default) and unique=False
        # (the declared composite-identity opt-out, 2a 2026-08-06) are both
        # legal table-level choices — nothing left to reject here. See
        # workbench/2026-08-06_schema_debt_external_id_findings_schema-debt-impl.md
        # for why a table would opt out.
