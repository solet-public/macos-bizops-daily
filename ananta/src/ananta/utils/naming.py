"""Shared naming utilities for artifact naming and external_id generation.

This module provides the single source of truth for name normalization
and validation across all plugins and services. All systems requiring
unique names should use these utilities.

Design principles:
- name: Display-only (case-sensitive, user-visible)
- external_id: Uniqueness key (normalized, case-insensitive)
- Fail fast on invalid names
"""

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, cast

from ananta.core.domain.enums import NamingErrorCode
from ananta.core.domain.types import ActionResult

if TYPE_CHECKING:
    from ananta.interfaces.blob_storage_service_interface import (
        BlobStorageServiceInterface,
    )

# Re-export for convenience
__all__ = [
    "NamingErrorCode",
    "NamingError",
    "NormalizedName",
    "normalize_name",
    "validate_display_name",
    "build_external_id",
    "parse_filename",
    "build_filename",
    "normalize_with_extension",
    "resolve_file_by_name",
]


class NamingError(Exception):
    """Exception for naming validation failures."""

    def __init__(self, code: NamingErrorCode, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(message)


# Maximum display name length (reasonable limit for filenames)
MAX_DISPLAY_NAME_LENGTH = 255

# Minimum display name length
MIN_DISPLAY_NAME_LENGTH = 1

# Known file extensions - only these are recognized as extensions by parse_filename
# This prevents decimal numbers like "0.8depth" from being misinterpreted as extensions
KNOWN_EXTENSIONS: frozenset[str] = frozenset({
    # Audio
    "wav", "mp3", "m4a", "flac", "ogg", "aac", "wma", "aiff", "opus",
    # MIDI
    "mid", "midi",
    # SSML/XML
    "ssml", "xml",
    # Documents
    "txt", "md", "json", "yaml", "yml", "csv", "tsv",
    # Images
    "png", "jpg", "jpeg", "gif", "webp", "svg", "bmp", "ico",
    # Video
    "mp4", "webm", "mkv", "avi", "mov",
    # Archives
    "zip", "tar", "gz", "7z",
    # Other
    "pdf", "html", "htm",
})


@dataclass(frozen=True)
class NormalizedName:
    """Result of name normalization."""

    display_name: str  # Original user-visible name (case-preserved)
    external_id: str  # Normalized uniqueness key (lowercase)
    extension: str | None = None  # File extension if applicable


def normalize_name(display_name: str) -> str:
    """Normalize display name to external_id.

    This function applies normalization rules to generate a case-insensitive
    uniqueness key from a user-provided display name.

    Rules applied:
    - Trim leading/trailing whitespace
    - Strip non-ASCII characters
    - Convert to lowercase
    - Replace internal whitespace with underscores
    - Keep only a-z, 0-9, _, -
    - Do NOT collapse multiple underscores (preserves user intent)
    - Reject empty results

    Args:
        display_name: User-visible name (case-sensitive)

    Returns:
        Normalized external_id (lowercase, ASCII-safe)

    Raises:
        NamingError: If result is empty after normalization
    """
    # Trim whitespace
    result = display_name.strip()

    # Strip non-ASCII characters
    result = result.encode("ascii", errors="ignore").decode("ascii")

    # Convert to lowercase
    result = result.lower()

    # Replace internal whitespace with underscores
    result = re.sub(r"\s+", "_", result)

    # Keep only alphanumeric, underscore, and hyphen
    result = re.sub(r"[^a-z0-9_\-]", "", result)

    # Strip leading/trailing underscores
    result = result.strip("_")

    # Validate non-empty
    if not result:
        raise NamingError(
            NamingErrorCode.NAME_INVALID,
            f"Name '{display_name}' contains no valid characters after normalization",
        )

    return result


def validate_display_name(name: str) -> None:
    """Validate display name format.

    Checks that the display name meets basic requirements:
    - Non-empty after stripping whitespace
    - Not excessively long
    - Contains at least one valid character

    Args:
        name: Display name to validate

    Raises:
        NamingError: If validation fails
    """
    if not name:
        raise NamingError(NamingErrorCode.NAME_INVALID, "Display name cannot be empty")

    stripped = name.strip()
    if not stripped:
        raise NamingError(
            NamingErrorCode.NAME_INVALID, "Display name cannot be only whitespace"
        )

    if len(stripped) > MAX_DISPLAY_NAME_LENGTH:
        raise NamingError(
            NamingErrorCode.NAME_INVALID,
            f"Display name exceeds maximum length of {MAX_DISPLAY_NAME_LENGTH} characters",
        )

    # Verify it will produce a valid external_id
    try:
        normalize_name(stripped)
    except NamingError:
        raise NamingError(
            NamingErrorCode.NAME_INVALID,
            f"Display name '{name}' contains no valid characters",
        ) from None


def build_external_id(display_name: str, extension: str = "") -> str:
    """Build external_id from display name and optional extension.

    When ``extension`` is provided, it is appended with an underscore
    separator so that MIDI and audio artifacts sharing the same base
    name receive distinct ``external_id`` values (e.g.
    ``foo_mid`` vs ``foo_wav``).

    Args:
        display_name: User-visible name
        extension: Optional file extension (without dot) to disambiguate

    Returns:
        Normalized external_id
    """
    base = normalize_name(display_name)
    if extension:
        ext = extension.lower().lstrip(".")
        return f"{base}_{ext}"
    return base


def parse_filename(filename: str) -> tuple[str, str]:
    """Parse filename into base name and extension.

    Only recognizes extensions from KNOWN_EXTENSIONS to avoid misinterpreting
    decimal numbers in filenames (e.g., "effect_0.8depth" should NOT be parsed
    as base="effect_0" with extension="8depth").

    Args:
        filename: Full filename (e.g., "speech.wav")

    Returns:
        Tuple of (base_name, extension) where extension excludes the dot.
        If no recognized extension, returns (filename, "").
    """
    if "." not in filename:
        return filename, ""

    # Split at last dot
    parts = filename.rsplit(".", 1)
    if len(parts) == 2:
        potential_ext = parts[1].lower()
        if potential_ext in KNOWN_EXTENSIONS:
            return parts[0], potential_ext
    return filename, ""


def build_filename(external_id: str, extension: str) -> str:
    """Build filename from external_id and extension.

    Filename is always external_id + "." + extension (normalized, filesystem-safe).

    Args:
        external_id: Normalized lookup key (from build_external_id)
        extension: File extension (without leading dot)

    Returns:
        Complete filename (e.g., "tremolo_source.mp3")
    """
    ext = extension.lstrip(".").lower()
    if ext:
        return f"{external_id}.{ext}"
    return external_id


def normalize_with_extension(filename: str) -> NormalizedName:
    """Normalize a filename, preserving extension separately.

    Args:
        filename: Full filename (e.g., "My Speech.wav")

    Returns:
        NormalizedName with display_name, external_id, and extension
    """
    base_name, extension = parse_filename(filename)
    display_name = base_name.strip()
    external_id = normalize_name(display_name)

    return NormalizedName(
        display_name=display_name,
        external_id=external_id,
        extension=extension if extension else None,
    )


def resolve_file_by_name(
    blob_storage_service: "BlobStorageServiceInterface",
    file_ref: str | dict[str, Any],
) -> tuple[str, str] | None:
    """Resolve a file reference to its blob_id and namespace.

    This function handles multiple input formats:
    1. String filename: Looks up by external_id (e.g., "greeting.wav")
    2. String blob_id: Looks up by blob_id (e.g., "bmd-abc123")
    3. Dict with blob_id/namespace: Returns directly if valid
    4. Dict with filename: Extracts filename and looks up

    Args:
        blob_storage_service: The blob storage service instance
        file_ref: File reference - can be:
                  - String filename (e.g., "greeting.wav")
                  - String blob_id (e.g., "bmd-abc123")
                  - Dict with "blob_id" and "namespace" keys
                  - Dict with "filename" key

    Returns:
        Tuple of (blob_id, namespace) if found, None if not found.

    Example:
        # String filename
        result = resolve_file_by_name(blob_service, "samantha_greeting.wav")

        # Dict with blob_id (from flow_input.attachments)
        result = resolve_file_by_name(blob_service, {"blob_id": "bmd-123", "namespace": "rest"})
    """
    # Handle dict input (e.g., from model passing attachment object)
    if isinstance(file_ref, dict):
        return _resolve_dict_file_ref(blob_storage_service, file_ref)

    # Handle string input
    file_name = file_ref

    # Check if input looks like a blob_id (starts with "bmd-")
    if file_name.startswith("bmd-"):
        result = _search_by_blob_id(blob_storage_service, file_name)
        if result:
            return result

    # Fall through to filename lookup
    return _search_by_filename(blob_storage_service, file_name)


def _resolve_dict_file_ref(
    blob_storage_service: "BlobStorageServiceInterface",
    file_ref: dict[str, Any],
) -> tuple[str, str] | None:
    """Resolve a dict file reference to blob_id and namespace.

    Handles dicts that contain either:
    - blob_id + namespace: Return directly
    - blob_id only: Look up namespace
    - filename: Look up by filename
    """
    blob_id = file_ref.get("blob_id")
    namespace = file_ref.get("namespace")

    # If we have both blob_id and namespace, return directly
    if blob_id and namespace:
        return (str(blob_id), str(namespace))

    # If we have blob_id but no namespace, look it up
    if blob_id:
        return _search_by_blob_id(blob_storage_service, str(blob_id))

    # If we have filename, look it up
    filename = file_ref.get("filename")
    if filename:
        return _search_by_filename(blob_storage_service, str(filename))

    # No valid keys found
    return None


def _search_by_blob_id(
    blob_storage_service: "BlobStorageServiceInterface",
    blob_id: str,
) -> tuple[str, str] | None:
    """Search for a blob by its blob_id."""
    result = blob_storage_service.search_blobs("", {"blob_id": blob_id})
    return _extract_blob_info_from_result(result)


def _search_by_filename(
    blob_storage_service: "BlobStorageServiceInterface",
    file_name: str,
) -> tuple[str, str] | None:
    """Search for a blob by filename using exact external_id match.

    When the input filename has an extension (e.g. "foo.wav"), searches by
    the extension-qualified external_id ("foo_wav") to prevent false matches
    against files sharing the same base name (e.g. "foo_raw_wav").
    """
    base_name, input_ext = parse_filename(file_name)

    try:
        if input_ext:
            search_id = build_external_id(base_name, input_ext)
        else:
            search_id = normalize_name(base_name)
    except NamingError:
        return None

    result = blob_storage_service.search_blobs("", {"external_id": search_id})
    if result.get("action_status") != "completed":
        return None

    data = cast(dict[str, Any], result.get("data", {}))
    files = cast(list[dict[str, Any]], data.get("files", []))
    if not files:
        return None

    return _extract_blob_info_from_result(result)


def _extract_blob_info_from_result(
    result: ActionResult,
) -> tuple[str, str] | None:
    """Extract blob_id and namespace from search result."""
    if result.get("action_status") != "completed":
        return None

    data = cast(dict[str, Any], result.get("data", {}))
    files = cast(list[dict[str, Any]], data.get("files", []))
    if not files:
        return None

    file_info = files[0]
    blob_id = cast(str | None, file_info.get("blob_id"))
    metadata = cast(dict[str, Any], file_info.get("metadata", {}))
    namespace = cast(str | None, metadata.get("plugin_namespace"))

    if not blob_id or not namespace:
        return None

    return blob_id, namespace
