"""Address Book service database schema definition.

Defines relational tables for address registry storage,
allowing agents to register and resolve named addresses.
"""

from ananta.types.column_types import ColumnType
from ananta.types.schema_types import (
    ColumnDefinition,
    IndexDefinition,
    SchemaDefinition,
    TableSchema,
)

# Namespace for address book tables
# Tables will be: default_address_book_plugin__address, default_address_book_plugin__address_entry
NAMESPACE = "default_address_book_plugin"


def get_address_book_schema() -> SchemaDefinition:
    """Address Book schema.

    Tables:
    - address: Registry entries with name, type, description, tags
    - address_entry: Individual fields for each address (url, port, host, etc.)
    """
    return SchemaDefinition(
        namespace=NAMESPACE,
        tables={
            "address": TableSchema(
                table_name="address",
                id_prefix="adr",
                description="Address registry entries with metadata",
                columns={
                    "name": ColumnDefinition(
                        type=ColumnType.TEXT,
                        not_null=True,
                        description=(
                            "Address name for resolution (lookup key). Uniqueness "
                            "among LIVE rows is enforced in application logic "
                            "(register upserts by name), NOT a DB UNIQUE constraint: "
                            "the surrogate id is the real key, and a natural-key "
                            "UNIQUE constraint traps soft-deletes and blocks "
                            "re-registration (see the idx_address_name lookup index)."
                        ),
                    ),
                    "address_type": ColumnDefinition(
                        type=ColumnType.TEXT,
                        not_null=True,
                        description="Address classification type (free-form; the address book is a generic key-value registry — closed enums on classification strings are the wrong abstraction)",
                    ),
                    "description": ColumnDefinition(
                        type=ColumnType.TEXT,
                        not_null=True,
                        description="Human-readable description of the address",
                    ),
                    "tags": ColumnDefinition(
                        type=ColumnType.JSON,
                        default="[]",
                        description="JSON array of tags for organization",
                    ),
                    "memory_id": ColumnDefinition(
                        type=ColumnType.TEXT,
                        description="FK to memory service entry for semantic search",
                    ),
                    # Standard fields auto-provided: id, external_id, name, created_at, updated_at
                },
                indexes=[
                    IndexDefinition("idx_address_name", ["name"]),
                    IndexDefinition("idx_address_type", ["address_type"]),
                    IndexDefinition("idx_address_memory_id", ["memory_id"]),
                ],
            ),
            "address_entry": TableSchema(
                table_name="address_entry",
                id_prefix="ade",
                description="Individual fields for each address entry",
                columns={
                    "default_address_book_plugin__address_id": ColumnDefinition(
                        type=ColumnType.TEXT,
                        not_null=True,
                        description="FK to address table",
                    ),
                    "field_type": ColumnDefinition(
                        type=ColumnType.TEXT,
                        not_null=True,
                        description="Entry field type (free-form; plugin consumers index entries by field_type as a string key, so any value the consumer plugin understands is valid)",
                    ),
                    "description": ColumnDefinition(
                        type=ColumnType.TEXT,
                        not_null=True,
                        description="Human-readable field description",
                    ),
                    "value": ColumnDefinition(
                        type=ColumnType.TEXT,
                        not_null=True,
                        description="Field value (can be vault reference like vault://secret_name)",
                    ),
                    "sort_order": ColumnDefinition(
                        type=ColumnType.INTEGER,
                        default=0,
                        description="Display order for entries",
                    ),
                    # Standard fields auto-provided: id, external_id, name, created_at, updated_at
                },
                indexes=[
                    IndexDefinition(
                        "idx_entry_address_id", ["default_address_book_plugin__address_id"]
                    ),
                    IndexDefinition("idx_entry_field_type", ["field_type"]),
                ],
            ),
        },
    )
