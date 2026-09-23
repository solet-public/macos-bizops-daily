"""Search filter matching for blob metadata queries.

Pure functions — no state, no I/O.  Used by FilesystemProvider.search_blobs.
"""

from __future__ import annotations

import json
from typing import Any

_SEARCH_RESERVED_KEYS: frozenset[str] = frozenset({
    "blob_id", "mime_type", "tags", "original_name", "external_id",
    "plugin_namespace", "limit", "offset",
})


def matches_filters(metadata: dict[str, Any], filters: dict[str, Any]) -> bool:
    if not _matches_blob_id(metadata, filters):
        return False
    if not _matches_mime_type(metadata, filters):
        return False
    if not _matches_tags(metadata, filters):
        return False
    if not _matches_name(metadata, filters):
        return False
    if not _matches_external_id(metadata, filters):
        return False
    if not _matches_namespace(metadata, filters):
        return False
    return _matches_plugin_metadata(metadata, filters)


def _matches_blob_id(metadata: dict[str, Any], filters: dict[str, Any]) -> bool:
    if "blob_id" not in filters:
        return True
    return bool(metadata.get("blob_id") == filters["blob_id"])


def _matches_mime_type(metadata: dict[str, Any], filters: dict[str, Any]) -> bool:
    if "mime_type" not in filters:
        return True
    filter_type = str(filters["mime_type"])
    file_type = str(metadata.get("mime_type", ""))
    if filter_type.endswith("/*"):
        prefix = filter_type[:-1]
        return bool(file_type.startswith(prefix))
    return bool(file_type == filter_type)


def _matches_tags(metadata: dict[str, Any], filters: dict[str, Any]) -> bool:
    if "tags" not in filters:
        return True
    file_tags = _parse_tags(metadata.get("tags", ""))
    filter_tags = _normalize_filter_tags(filters["tags"])
    return any(tag in file_tags for tag in filter_tags)


def _parse_tags(tags_str: str) -> list[str]:
    if not tags_str:
        return []
    return [tag.strip() for tag in tags_str.split(",")]


def _normalize_filter_tags(tags: Any) -> list[str]:
    if isinstance(tags, list):
        return tags
    return [tags]


def _matches_name(metadata: dict[str, Any], filters: dict[str, Any]) -> bool:
    if "original_name" not in filters:
        return True
    original_name = (metadata.get("original_name") or "").lower()
    search_name = filters["original_name"].lower()
    return search_name in original_name


def _matches_external_id(metadata: dict[str, Any], filters: dict[str, Any]) -> bool:
    if "external_id" not in filters:
        return True
    external_id = str(metadata.get("external_id") or "").lower()
    search_id = str(filters["external_id"]).lower()
    return search_id == external_id


def _matches_namespace(metadata: dict[str, Any], filters: dict[str, Any]) -> bool:
    if "plugin_namespace" not in filters:
        return True
    return bool(metadata.get("plugin_namespace") == filters["plugin_namespace"])


def _matches_plugin_metadata(metadata: dict[str, Any], filters: dict[str, Any]) -> bool:
    """AND-match across dotted-path filters into the plugin_metadata JSON field."""
    plugin_meta_cache: dict[str, Any] | None = None
    for key, expected in filters.items():
        if key in _SEARCH_RESERVED_KEYS:
            continue
        if not key.startswith("plugin_metadata."):
            continue
        if plugin_meta_cache is None:
            plugin_meta_cache = extract_plugin_metadata(metadata)
        path = key[len("plugin_metadata."):]
        actual = resolve_dotted_path(plugin_meta_cache, path)
        if isinstance(expected, list):
            if actual not in expected:
                return False
        elif actual != expected:
            return False
    return True


def extract_plugin_metadata(metadata: dict[str, Any]) -> dict[str, Any]:
    """Return the plugin_metadata field as a dict, parsing JSON if needed."""
    raw = metadata.get("plugin_metadata")
    if raw is None:
        return {}
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def resolve_dotted_path(data: dict[str, Any], path: str) -> Any:
    """Walk a dotted key path through nested dicts; return None if missing."""
    cursor: Any = data
    for part in path.split("."):
        if not isinstance(cursor, dict):
            return None
        cursor = cursor.get(part)
        if cursor is None:
            return None
    return cursor


def collect_matching_files(
    records: list[Any], metadata_filters: dict[str, Any]
) -> list[dict[str, Any]]:
    """Filter records by metadata_filters, format for response, and sort by created_at desc."""
    result: list[dict[str, Any]] = [
        {"blob_id": m.get("blob_id"), "metadata": m}
        for m in records
        if isinstance(m, dict) and matches_filters(m, metadata_filters)
    ]
    result.sort(key=lambda x: x["metadata"].get("created_at", ""), reverse=True)
    return result
