from typing import Final

PLUGIN_NAME: Final[str] = "default_blob_storage_plugin"
PLUGIN_VERSION: Final[str] = "0.1.0"

BLOBS_DIRECTORY_NAME: Final[str] = "blobs"
DEFAULT_MAX_FILE_SIZE: Final[int] = 1024 * 1024 * 1024  # 1GB
DEFAULT_CLEANUP_ON_STARTUP: Final[bool] = True


SUPPORTED_MIME_TYPES: Final[tuple[str, ...]] = (
    "text/plain",
    "text/markdown",
    "text/html",
    "text/csv",
    "application/json",
    "application/xml",
    "application/pdf",
    "application/msword",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "image/jpeg",
    "image/png",
    "image/gif",
    "image/webp",
    "image/svg+xml",
    "audio/mpeg",
    "audio/wav",
    "audio/ogg",
    "video/mp4",
    "video/avi",
    "video/quicktime",
    "application/zip",
    "application/tar",
    "application/gzip",
    "application/octet-stream",  # Fallback for unknown binary files
)

STANDARD_METADATA_KEYS: Final[tuple[str, ...]] = (
    "name",  # User-specified name (no extension)
    "extension",  # File extension (e.g., mp3, wav, png)
    "external_id",  # Normalized lookup key derived from name
    "filename",  # Complete filename (external_id + "." + extension)
    "original_name",  # Original filename for UPLOADED files only
    "mime_type",
    "size",
    "preview",
    "description",
    "tags",
    "created_at",
    "updated_at",
    "plugin_namespace",
)


# File-source ingestion: error codes (used by store_blob_from_file)
class IngestionErrorCode:
    """Error codes for file-source blob ingestion (store_blob_from_file)."""

    FILE_PATH_NOT_ABSOLUTE: Final[str] = "blob_storage.file_path_not_absolute"
    FILE_NOT_FOUND: Final[str] = "blob_storage.file_not_found"
    FILE_NOT_REGULAR: Final[str] = "blob_storage.file_not_regular"
    FILE_UNREADABLE: Final[str] = "blob_storage.file_unreadable"
    FILE_EMPTY: Final[str] = "blob_storage.file_empty"
    MIME_TYPE_UNKNOWN: Final[str] = "blob_storage.mime_type_unknown"


# File-source ingestion: auto-derived metadata field names
INGESTION_META_FILENAME: Final[str] = "filename"
INGESTION_META_ORIGINAL_NAME: Final[str] = "original_name"
INGESTION_META_MIME_TYPE: Final[str] = "mime_type"
INGESTION_META_SOURCE_PATH: Final[str] = "source_path"
INGESTION_META_BYTE_COUNT: Final[str] = "byte_count"
INGESTION_META_ARTIFACT_TYPE: Final[str] = "artifact_type"

# File-source ingestion: derive artifact_type from mime_type primary class
MIME_PRIMARY_CLASS_SEPARATOR: Final[str] = "/"
