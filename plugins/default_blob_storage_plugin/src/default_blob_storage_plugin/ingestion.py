"""File ingestion helpers for blob storage."""
from __future__ import annotations

import mimetypes
from pathlib import Path

from ananta.core.plugins.plugin_contracts import ActionResult

from .constants import (
    INGESTION_META_ARTIFACT_TYPE,
    INGESTION_META_BYTE_COUNT,
    INGESTION_META_FILENAME,
    INGESTION_META_MIME_TYPE,
    INGESTION_META_ORIGINAL_NAME,
    INGESTION_META_SOURCE_PATH,
    IngestionErrorCode,
)
from .errors import create_error_response


def validate_path(file_path: str) -> ActionResult | None:
    """Run fail-fast filesystem checks on the supplied ingestion path."""
    path_obj = Path(file_path)
    if not path_obj.is_absolute():
        return create_error_response(
            IngestionErrorCode.FILE_PATH_NOT_ABSOLUTE,
            f"File path must be absolute: {file_path!r}",
        )
    if not path_obj.exists():
        return create_error_response(
            IngestionErrorCode.FILE_NOT_FOUND,
            f"File not found: {file_path!r}",
        )
    if not path_obj.is_file():
        return create_error_response(
            IngestionErrorCode.FILE_NOT_REGULAR,
            f"Path is not a regular file: {file_path!r}",
        )
    # Permission probe: open() raises PermissionError on unreadable files
    try:
        with path_obj.open("rb"):
            pass
    except PermissionError:
        return create_error_response(
            IngestionErrorCode.FILE_UNREADABLE,
            f"File is not readable (permission denied): {file_path!r}",
        )
    return None


def read_content(file_path: str) -> bytes | ActionResult:
    """Read file bytes; return ActionResult error if the file is empty."""
    content = Path(file_path).read_bytes()
    if not content:
        return create_error_response(
            IngestionErrorCode.FILE_EMPTY,
            f"File is empty (zero bytes): {file_path!r}",
        )
    return content


def resolve_mime_type(mime_type: str | None, resolved_filename: str) -> str | ActionResult:
    """Return the resolved MIME type, or an ActionResult error if unknown."""
    if mime_type is not None:
        return mime_type
    guessed, _ = mimetypes.guess_type(resolved_filename)
    if guessed is None:
        return create_error_response(
            IngestionErrorCode.MIME_TYPE_UNKNOWN,
            (
                "Could not infer MIME type from filename "
                f"{resolved_filename!r}; supply mime_type explicitly."
            ),
        )
    return guessed


def build_metadata(
    extra_metadata: dict[str, object],
    resolved_filename: str,
    resolved_mime: str,
    source_path: str,
    byte_count: int,
    resolved_artifact_type: str,
) -> dict[str, object]:
    """Merge auto-derived ingestion metadata with caller-supplied extras."""
    merged: dict[str, object] = {**extra_metadata}
    merged[INGESTION_META_ORIGINAL_NAME] = resolved_filename
    merged[INGESTION_META_FILENAME] = resolved_filename
    merged[INGESTION_META_MIME_TYPE] = resolved_mime
    merged[INGESTION_META_SOURCE_PATH] = source_path
    merged[INGESTION_META_BYTE_COUNT] = byte_count
    merged[INGESTION_META_ARTIFACT_TYPE] = resolved_artifact_type
    return merged
