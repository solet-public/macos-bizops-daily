"""Database schema for default_knowledge_plugin."""

from ananta.types.column_types import ColumnType
from ananta.types.schema_types import (
    ColumnDefinition,
    IndexDefinition,
    SchemaDefinition,
    TableSchema,
)

from .constants import PLUGIN_NAME, TABLE_KNOWLEDGE_INSTALL


def get_knowledge_schema() -> SchemaDefinition:
    """Schema definition for knowledge base installs."""
    return SchemaDefinition(
        namespace=PLUGIN_NAME,
        tables={
            TABLE_KNOWLEDGE_INSTALL: TableSchema(
                table_name=TABLE_KNOWLEDGE_INSTALL,
                id_prefix="kin",
                description="Installed knowledge base metadata and activation state",
                columns={
                    "name": ColumnDefinition(
                        type=ColumnType.TEXT,
                        not_null=True,
                        unique=True,
                        description="Directory name (unique key)",
                    ),
                    "source": ColumnDefinition(
                        type=ColumnType.TEXT,
                        description="Clean git URL if cloned (no credentials), NULL otherwise",
                    ),
                    "source_type": ColumnDefinition(
                        type=ColumnType.TEXT,
                        not_null=True,
                        check="source_type IN ('local', 'symlink', 'git')",
                        description="Source classification",
                    ),
                    "resolved_path": ColumnDefinition(
                        type=ColumnType.TEXT,
                        not_null=True,
                        description="Absolute filesystem path",
                    ),
                    "manifest_name": ColumnDefinition(
                        type=ColumnType.TEXT,
                        description="Display name from manifest",
                    ),
                    "manifest_tags": ColumnDefinition(
                        type=ColumnType.JSON,
                        default="[]",
                        description="Tags from manifest",
                    ),
                    "process_keys": ColumnDefinition(
                        type=ColumnType.JSON,
                        default="[]",
                        description="Process keys from manifest",
                    ),
                    "chunk_count": ColumnDefinition(
                        type=ColumnType.INTEGER,
                        not_null=True,
                        default=0,
                        description="Number of indexed chunks",
                    ),
                    "memory_ids": ColumnDefinition(
                        type=ColumnType.JSON,
                        not_null=True,
                        default="[]",
                        description="List of created memory IDs",
                    ),
                    "branch": ColumnDefinition(
                        type=ColumnType.TEXT,
                        description="Managed branch name (e.g. 'example' for git KBs, NULL for local)",
                    ),
                    "last_indexed_commit": ColumnDefinition(
                        type=ColumnType.TEXT,
                        description="Git commit SHA at last index (NULL for non-git)",
                    ),
                    "is_active": ColumnDefinition(
                        type=ColumnType.INTEGER,
                        not_null=True,
                        default=1,
                        check="is_active IN (0, 1)",
                        description="Activation flag",
                    ),
                    "indexed_at": ColumnDefinition(
                        type=ColumnType.TEXT,
                        not_null=True,
                        description="Last content indexing timestamp",
                    ),
                },
                indexes=[
                    IndexDefinition("idx_knowledge_install_active", ["is_active"]),
                ],
            ),
        },
    )
