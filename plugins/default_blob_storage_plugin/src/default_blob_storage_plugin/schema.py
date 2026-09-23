"""Blob storage plugin database schema definition.

Defines the metadata table for tracking stored blobs.
"""

from ananta.types.column_types import ColumnType
from ananta.types.schema_types import (
    ColumnDefinition,
    IndexDefinition,
    SchemaDefinition,
    TableSchema,
)

from .constants import PLUGIN_NAME

TABLE_METADATA = "metadata"


def get_blob_storage_schema() -> SchemaDefinition:
    """Blob storage metadata schema.

    Tables:
    - metadata: Blob metadata for stored files
    """
    return SchemaDefinition(
        namespace=PLUGIN_NAME,
        tables={
            TABLE_METADATA: TableSchema(
                table_name=TABLE_METADATA,
                description="Metadata for stored blobs",
                id_prefix="bmd",
                columns={
                    "blob_id": ColumnDefinition(
                        type=ColumnType.TEXT,
                        not_null=False,  # Must be nullable: INSERT first, then UPDATE with generated ID
                        description="Unique blob identifier (set after ID generation)",
                    ),
                    "name": ColumnDefinition(
                        type=ColumnType.TEXT,
                        description="User-specified name (no extension)",
                    ),
                    "extension": ColumnDefinition(
                        type=ColumnType.TEXT,
                        description="File extension (e.g., mp3, wav, png)",
                    ),
                    "filename": ColumnDefinition(
                        type=ColumnType.TEXT,
                        description="Complete filename (external_id + extension)",
                    ),
                    "original_name": ColumnDefinition(
                        type=ColumnType.TEXT,
                        description="Original filename for UPLOADED files only",
                    ),
                    "mime_type": ColumnDefinition(
                        type=ColumnType.TEXT,
                        description="MIME type of the blob",
                    ),
                    "size": ColumnDefinition(
                        type=ColumnType.INTEGER,
                        description="Size in bytes",
                    ),
                    "preview": ColumnDefinition(
                        type=ColumnType.TEXT,
                        description="Preview text or thumbnail reference",
                    ),
                    "description": ColumnDefinition(
                        type=ColumnType.TEXT,
                        description="Human-readable description",
                    ),
                    "plugin_namespace": ColumnDefinition(
                        type=ColumnType.TEXT,
                        description="Namespace of plugin that stored the blob",
                    ),
                    "file_hash": ColumnDefinition(
                        type=ColumnType.TEXT,
                        description="Content hash for deduplication",
                    ),
                    "saved_at": ColumnDefinition(
                        type=ColumnType.TEXT,
                        description="Timestamp when blob was saved",
                    ),
                    "plugin_metadata": ColumnDefinition(
                        type=ColumnType.JSON,
                        default="{}",
                        description="Plugin-specific metadata attributes",
                    ),
                },
                indexes=[
                    IndexDefinition("idx_blob_id", ["blob_id"]),
                    IndexDefinition("idx_filename", ["filename"]),
                    IndexDefinition("idx_plugin_namespace", ["plugin_namespace"]),
                    IndexDefinition("idx_mime_type", ["mime_type"]),
                    IndexDefinition("idx_file_hash", ["file_hash"]),
                ],
            ),
        },
    )
