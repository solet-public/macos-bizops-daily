import re
from typing import Any

from .constants import DEFAULT_MAX_FILE_SIZE
from .errors import BlobStorageErrorCode, BlobValidationError


def validate_blob_id_format(blob_id: str) -> None:
    """Validate blob_id is a valid state-service generated ID.

    State service generates IDs in format: {prefix}-{base36_suffix}
    where prefix comes from the table schema's id_prefix and suffix is 12-13 chars.
    We validate structure only, not the specific prefix (which varies by table).
    """
    if not blob_id:
        raise BlobValidationError(
            "Blob ID cannot be empty", BlobStorageErrorCode.INVALID_BLOB_ID.value
        )

    # State service IDs have format: {prefix}-{base36_suffix}
    # The prefix varies by table schema, suffix is 12-13 alphanumeric chars
    if "-" not in blob_id:
        raise BlobValidationError(
            "Blob ID must contain a hyphen separator",
            BlobStorageErrorCode.INVALID_BLOB_ID.value,
        )

    parts = blob_id.split("-", 1)
    if len(parts) != 2 or not parts[0] or not parts[1]:
        raise BlobValidationError(
            "Blob ID must be in format '{prefix}-{suffix}'",
            BlobStorageErrorCode.INVALID_BLOB_ID.value,
        )

    suffix = parts[1]
    # State service base36 encoding produces 12-13 character suffixes
    if not re.match(r"^[a-z0-9]{12,13}$", suffix):
        raise BlobValidationError(
            f"Blob ID suffix must be 12-13 alphanumeric characters (base36), got '{suffix}'",
            BlobStorageErrorCode.INVALID_BLOB_ID.value,
        )


def validate_file_content(content: bytes, max_size: int = DEFAULT_MAX_FILE_SIZE) -> None:
    if len(content) == 0:
        raise BlobValidationError(
            "File content cannot be empty",
            BlobStorageErrorCode.INVALID_CONTENT.value,
        )

    if len(content) > max_size:
        raise BlobValidationError(
            f"File size ({len(content)} bytes) exceeds maximum allowed size ({max_size} bytes)",
            BlobStorageErrorCode.BLOB_TOO_LARGE.value,
        )


def _validate_name(metadata: dict[str, Any]) -> None:
    """Validate name field if present (user-specified name, no extension)."""
    if "name" in metadata:
        name = metadata["name"]
        if not isinstance(name, str) or not name.strip():
            raise BlobValidationError(
                "name must be a non-empty string",
                BlobStorageErrorCode.INVALID_METADATA.value,
            )


def _validate_extension(metadata: dict[str, Any]) -> None:
    """Validate extension field if present."""
    if "extension" in metadata:
        extension = metadata["extension"]
        if not isinstance(extension, str) or not extension.strip():
            raise BlobValidationError(
                "extension must be a non-empty string",
                BlobStorageErrorCode.INVALID_METADATA.value,
            )
        # Extension should not have a leading dot
        if extension.startswith("."):
            raise BlobValidationError(
                "extension should not include leading dot",
                BlobStorageErrorCode.INVALID_METADATA.value,
            )


def _validate_filename(metadata: dict[str, Any]) -> None:
    """Validate filename field if present (calculated: external_id + extension)."""
    if "filename" in metadata:
        filename = metadata["filename"]
        if not isinstance(filename, str) or not filename.strip():
            raise BlobValidationError(
                "filename must be a non-empty string",
                BlobStorageErrorCode.INVALID_METADATA.value,
            )


def _validate_original_name(metadata: dict[str, Any]) -> None:
    """Validate original_name field if present (for uploaded files only)."""
    if "original_name" in metadata:
        original_name = metadata["original_name"]
        if not isinstance(original_name, str) or not original_name.strip():
            raise BlobValidationError(
                "original_name must be a non-empty string",
                BlobStorageErrorCode.INVALID_METADATA.value,
            )


def _validate_mime_type(metadata: dict[str, Any]) -> None:
    """Validate mime_type field if present."""
    if "mime_type" in metadata:
        mime_type = metadata["mime_type"]
        if not isinstance(mime_type, str) or not mime_type.strip():
            raise BlobValidationError(
                "mime_type must be a non-empty string",
                BlobStorageErrorCode.INVALID_METADATA.value,
            )

        if "/" not in mime_type:
            raise BlobValidationError(
                "mime_type must be in format 'type/subtype'",
                BlobStorageErrorCode.INVALID_METADATA.value,
            )


def _validate_size(metadata: dict[str, Any]) -> None:
    """Validate size field if present."""
    if "size" in metadata:
        size = metadata["size"]
        if not isinstance(size, int) or size < 0:
            raise BlobValidationError(
                "size must be a non-negative integer",
                BlobStorageErrorCode.INVALID_METADATA.value,
            )


def _validate_and_normalize_tags(metadata: dict[str, Any]) -> None:
    """Validate and normalize tags field if present."""
    if "tags" in metadata:
        tags = metadata["tags"]
        if isinstance(tags, str):
            metadata["tags"] = [tag.strip() for tag in tags.split(",") if tag.strip()]
        elif isinstance(tags, list):
            if not all(isinstance(tag, str) for tag in tags):
                raise BlobValidationError(
                    "tags must be a list of strings or comma-separated string",
                    BlobStorageErrorCode.INVALID_METADATA.value,
                )
        else:
            raise BlobValidationError(
                "tags must be a list of strings or comma-separated string",
                BlobStorageErrorCode.INVALID_METADATA.value,
            )


def _validate_description(metadata: dict[str, Any]) -> None:
    """Validate description field if present."""
    if "description" in metadata:
        description = metadata["description"]
        if not isinstance(description, str):
            raise BlobValidationError(
                "description must be a string",
                BlobStorageErrorCode.INVALID_METADATA.value,
            )


def validate_file_metadata(metadata: dict[str, Any]) -> None:
    """Validate file metadata structure and fields."""
    _validate_name(metadata)
    _validate_extension(metadata)
    _validate_filename(metadata)
    _validate_original_name(metadata)
    _validate_mime_type(metadata)
    _validate_size(metadata)
    _validate_and_normalize_tags(metadata)
    _validate_description(metadata)


def validate_search_filters(filters: dict[str, Any]) -> None:
    if "limit" in filters:
        limit = filters["limit"]
        if not isinstance(limit, int) or limit <= 0:
            raise BlobValidationError(
                "limit must be a positive integer",
                BlobStorageErrorCode.VALIDATION_ERROR.value,
            )

        if limit > 1000:
            raise BlobValidationError(
                "limit cannot exceed 1000",
                BlobStorageErrorCode.VALIDATION_ERROR.value,
            )

    if "offset" in filters:
        offset = filters["offset"]
        if not isinstance(offset, int) or offset < 0:
            raise BlobValidationError(
                "offset must be a non-negative integer",
                BlobStorageErrorCode.VALIDATION_ERROR.value,
            )


def normalize_metadata(metadata: dict[str, Any]) -> dict[str, Any]:
    normalized = dict(metadata)

    if "tags" in normalized and isinstance(normalized["tags"], list):
        normalized["tags"] = ",".join(normalized["tags"])

    for key, value in normalized.items():
        if isinstance(value, str):
            normalized[key] = value.strip()

    return normalized
