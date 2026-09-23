"""Metadata preparation and transformation for blob storage.

Pure functions — no state service calls, no I/O.  Used by FilesystemProvider.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from ananta.utils.naming import NamingError, build_external_id, normalize_name, parse_filename

from ..validation import normalize_metadata

_SCHEMA_FIELDS: frozenset[str] = frozenset({
    "blob_id", "name", "extension", "external_id", "filename", "original_name",
    "mime_type", "size", "preview", "description", "plugin_namespace", "file_hash", "saved_at",
})

_STATE_MANAGED_FIELDS: frozenset[str] = frozenset({
    "id", "namespace", "created_at", "updated_at", "last_read_at", "created_by", "updated_by",
})

_RESERVED_FIELDS: frozenset[str] = frozenset({"action_result", "action_status", "file_content"})


def prepare_metadata_for_storage(
    metadata: dict[str, Any], content_size: int, log_prefix: str
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Enrich, normalize, sanitize and split metadata; return (schema_metadata, normalized)."""
    logger = logging.getLogger(__name__)
    logger.debug("%s METADATA: Enriching and normalizing", log_prefix)
    sanitized = enrich_and_sanitize_metadata(metadata, content_size)
    normalized = normalize_metadata({**metadata, "size": content_size})
    derive_name_fields_from_original(sanitized, log_prefix, logger)
    schema_metadata, extra_fields = split_schema_extra_fields(sanitized)
    logger.debug(
        "%s METADATA: Enriched (schema_fields=%s, extra_fields=%s)",
        log_prefix, list(schema_metadata.keys()), list(extra_fields.keys()),
    )
    return schema_metadata, normalized


def enrich_and_sanitize_metadata(metadata: dict[str, Any], content_size: int) -> dict[str, Any]:
    enriched = {**metadata, "size": content_size}
    if "mime_type" not in enriched:
        enriched["mime_type"] = enriched.get("content_type", "application/octet-stream")
    normalized = normalize_metadata(enriched)
    filtered = filter_reserved_fields(normalized)
    return sanitize_nul_bytes(filtered)


def filter_reserved_fields(metadata: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in metadata.items() if k not in _RESERVED_FIELDS}


def sanitize_nul_bytes(metadata: dict[str, Any]) -> dict[str, Any]:
    """Sanitize TEXT fields: PostgreSQL TEXT columns cannot contain NUL bytes."""
    return {
        k: v.replace("\x00", "") if isinstance(v, str) else v
        for k, v in metadata.items()
    }


def derive_name_fields_from_original(
    sanitized: dict[str, Any], log_prefix: str, logger: Any
) -> None:
    if "original_name" not in sanitized:
        return
    original_name = sanitized["original_name"]
    base_name, ext = parse_filename(original_name)
    _maybe_set_name(sanitized, base_name, original_name, log_prefix, logger)
    _maybe_set_extension(sanitized, ext, original_name, log_prefix, logger)
    _maybe_set_external_id(sanitized, base_name, ext, original_name, log_prefix, logger)
    _maybe_set_filename(sanitized, log_prefix, logger)


def _maybe_set_name(
    sanitized: dict[str, Any], base_name: str, original_name: str, log_prefix: str, logger: Any
) -> None:
    if "name" in sanitized:
        return
    sanitized["name"] = base_name
    logger.debug(
        "%s METADATA: Derived name='%s' from original_name='%s'",
        log_prefix, base_name, original_name,
    )


def _maybe_set_extension(
    sanitized: dict[str, Any], ext: str, original_name: str, log_prefix: str, logger: Any
) -> None:
    if "extension" in sanitized or not ext:
        return
    sanitized["extension"] = ext
    logger.debug(
        "%s METADATA: Derived extension='%s' from original_name='%s'",
        log_prefix, ext, original_name,
    )


def _maybe_set_external_id(
    sanitized: dict[str, Any],
    base_name: str,
    ext: str,
    original_name: str,
    log_prefix: str,
    logger: Any,
) -> None:
    if "external_id" in sanitized:
        return
    try:
        derived_ext = sanitized.get("extension") or ext
        sanitized["external_id"] = (
            build_external_id(base_name, derived_ext) if derived_ext else normalize_name(base_name)
        )
        logger.debug(
            "%s METADATA: Derived external_id='%s' from original_name='%s'",
            log_prefix, sanitized["external_id"], original_name,
        )
    except NamingError:
        logger.debug(
            "%s METADATA: Could not derive external_id from original_name='%s' (no valid characters)",
            log_prefix, original_name,
        )


def _maybe_set_filename(
    sanitized: dict[str, Any], log_prefix: str, logger: Any
) -> None:
    if "filename" in sanitized:
        return
    ext_id = sanitized.get("external_id")
    extension = sanitized.get("extension")
    if not (ext_id and extension):
        return
    sanitized["filename"] = f"{ext_id}.{extension}"
    logger.debug(
        "%s METADATA: Derived filename='%s' from external_id and extension",
        log_prefix, sanitized["filename"],
    )


def split_schema_extra_fields(
    sanitized: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    schema_metadata = {k: v for k, v in sanitized.items() if k in _SCHEMA_FIELDS}
    extra_fields = {k: v for k, v in sanitized.items() if k not in _SCHEMA_FIELDS}
    if extra_fields:
        schema_metadata["plugin_metadata"] = json.dumps(extra_fields)
    return schema_metadata, extra_fields


def filter_state_managed_fields(metadata: dict[str, Any]) -> dict[str, Any]:
    """Filter out fields managed by the state plugin.

    Note: external_id is NOT filtered — it's used for name-based uniqueness enforcement.
    """
    return {k: v for k, v in metadata.items() if k not in _STATE_MANAGED_FIELDS}
