"""State service operations for blob metadata.

Free functions that wrap StateServiceProtocol calls.  Used by FilesystemProvider.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from ananta.core.plugins.plugin_contracts import ActionResult
from ananta.interfaces.state_service_protocol import StateServiceProtocol
from ananta.services.state_service.bounded_read import assert_within_ceiling

from ..errors import BlobStorageErrorCode, create_error_response, is_unique_constraint_error

# ⚠ THIS READ IS EXPECTED TO REFUSE TODAY, AND THAT IS THE POINT.
#
# MEASURED 2026-08-15: default_blob_storage_plugin.metadata holds **193,390
# rows**. Shipping that as records would be a LARGER read than the 109,393-row
# scan that froze the platform for 3h20m and started this whole programme. So
# there is no ceiling at which "read them all" is a safe repair — raising the
# number until it fits would be reconstructing the outage on purpose.
#
# The ceiling is therefore set at the OLD platform cap, below the measured size,
# so the read fails loud instead of succeeding expensively. Deliberately NO
# `unbounded` opt-in: this call site is not entitled to that scan.
#
# What this function actually needs is a set difference (which blob ids on disk
# have no metadata row), and that belongs in the database. The state interface
# cannot express it today — the same missing-verb gap as the absent GROUP BY.
# Until it can, get_metadata_blob_ids must not run.
#
# Safe to leave refusing: its only caller, filesystem_provider's
# _cleanup_orphaned_files, has NO caller of its own anywhere in the repository
# (verified 2026-08-15), so nothing invokes this path today. Before anything
# wires it up, it needs redesigning — see the report.
_METADATA_TABLE_CEILING = 10_000
_METADATA_TABLE_CEILING_REASON = (
    "one row per stored blob (measured: 193,390), and the caller unlinks every "
    "blob file whose id is missing from this set, so a partial read would delete "
    "live blobs; reading them all would be a larger scan than the one that caused "
    "the 2026-08-15 outage."
)


def extract_generated_id(result: ActionResult) -> str | None:
    """Extract auto-generated ID from a state service write_state result."""
    data = result.get("data")
    if not isinstance(data, dict):
        return None
    result_obj = data.get("result")
    if isinstance(result_obj, dict):
        generated_id = result_obj.get("generated_id")
        if isinstance(generated_id, str):
            return generated_id
    return None


def get_metadata_from_state(
    state_service: StateServiceProtocol,
    plugin_name: str,
    namespace: str,
    blob_id: str,
) -> dict[str, Any] | None:
    """Retrieve metadata for a specific blob (namespace-scoped lookup)."""
    result = state_service.read_state(
        namespace=plugin_name,
        query={"table": "metadata", "filters": {"blob_id": blob_id, "plugin_namespace": namespace}},
    )
    if result.get("action_status") == "completed":
        data = result.get("data", {})
        records = data.get("records", [])
        if isinstance(records, list) and records:
            first_record = records[0]
            if isinstance(first_record, dict):
                return first_record
    return None


def find_metadata_by_blob_id(
    state_service: StateServiceProtocol,
    plugin_name: str,
    blob_id: str,
) -> dict[str, Any] | None:
    """Find metadata for a blob by ID only (cross-namespace lookup)."""
    result = state_service.read_state(
        namespace=plugin_name,
        query={"table": "metadata", "filters": {"blob_id": blob_id}},
    )
    if result.get("action_status") == "completed":
        data = result.get("data", {})
        records = data.get("records", [])
        if isinstance(records, list) and records:
            first_record = records[0]
            if isinstance(first_record, dict):
                return first_record
    return None


def store_metadata_to_state(
    state_service: StateServiceProtocol,
    plugin_name: str,
    namespace: str,
    schema_metadata: dict[str, Any],
    log_prefix: str,
) -> tuple[str | None, ActionResult | None]:
    """Store metadata record; return (generated_id, None) or (None, error_response)."""
    logger = logging.getLogger(__name__)
    logger.debug("%s STATE_WRITE: Storing metadata to state service (state will generate ID)", log_prefix)
    schema_metadata["plugin_namespace"] = namespace

    metadata_result = state_service.write_state(
        namespace=plugin_name,
        data={"table": "metadata", "record": schema_metadata},
    )

    action_status = metadata_result.get("action_status")
    logger.debug("%s STATE_WRITE: Response action_status=%s", log_prefix, action_status)

    if action_status != "completed":
        error_details = metadata_result.get("error", "Unknown error")
        logger.error("%s STATE_WRITE: FAILED - %s", log_prefix, error_details)
        return None, create_error_response(
            BlobStorageErrorCode.METADATA_STORAGE_ERROR.value,
            f"Failed to store metadata: {error_details}",
        )

    generated_id = extract_generated_id(metadata_result)
    if not generated_id:
        logger.error("%s ERROR: State service did not return generated ID", log_prefix)
        return None, create_error_response(
            BlobStorageErrorCode.METADATA_STORAGE_ERROR.value,
            "State service did not return generated ID",
        )

    logger.debug("%s STATE_GENERATED_ID: %s", log_prefix, generated_id)
    return generated_id, None


def store_metadata_with_conflict_check(
    state_service: StateServiceProtocol,
    plugin_name: str,
    namespace: str,
    metadata: dict[str, Any],
    log_prefix: str,
) -> tuple[str | None, ActionResult | None]:
    """Store metadata, mapping unique-constraint violations to EXTERNAL_ID_CONFLICT."""
    generated_id, error_result = store_metadata_to_state(
        state_service, plugin_name, namespace, metadata, log_prefix
    )
    if error_result is None:
        return generated_id, None

    error_dict: object = error_result.get("error", {})
    error_message = str(
        error_dict.get("message", "") if isinstance(error_dict, dict) else error_dict
    )
    if is_unique_constraint_error(error_message):
        external_id = metadata.get("external_id", "<unknown>")
        return None, create_error_response(
            BlobStorageErrorCode.EXTERNAL_ID_CONFLICT.value,
            f"Cannot store blob: external_id '{external_id}' already exists. "
            "Use a different name or delete the existing blob.",
        )
    return None, error_result


def rollback_blob_and_metadata(
    state_service: StateServiceProtocol,
    plugin_name: str,
    blob_path: Path,
    generated_id: str,
    log_prefix: str,
) -> None:
    logger = logging.getLogger(__name__)
    if blob_path.exists():
        blob_path.unlink()
        logger.debug("%s ROLLBACK: Deleted blob file", log_prefix)
    state_service.delete_records(
        namespace=plugin_name,
        query={"table": "metadata", "filters": {"id": generated_id}},
    )
    logger.debug("%s ROLLBACK: Deleted metadata record", log_prefix)


def update_blob_id_in_metadata(
    state_service: StateServiceProtocol,
    plugin_name: str,
    generated_id: str,
    blob_path: Path,
    log_prefix: str,
) -> ActionResult | None:
    """Update metadata with blob_id field; rolls back on failure. Returns error or None."""
    logger = logging.getLogger(__name__)
    logger.debug("%s STATE_UPDATE: Adding blob_id to metadata record", log_prefix)
    update_result = state_service.update_state(
        namespace=plugin_name,
        query={"table": "metadata", "filters": {"id": generated_id}},
        updates={"blob_id": generated_id},
    )
    action_status = update_result.get("action_status")
    logger.debug("%s STATE_UPDATE: Response action_status=%s", log_prefix, action_status)

    if action_status != "completed":
        error_details = update_result.get("error", "Unknown error")
        logger.error(
            "%s STATE_UPDATE: FAILED - blob_id field is required for retrieval: %s",
            log_prefix, error_details,
        )
        rollback_blob_and_metadata(state_service, plugin_name, blob_path, generated_id, log_prefix)
        return create_error_response(
            BlobStorageErrorCode.METADATA_STORAGE_ERROR.value,
            f"Failed to set blob_id field in metadata: {error_details}",
        )
    return None


def get_metadata_blob_ids(
    state_service: StateServiceProtocol, plugin_name: str
) -> set[str]:
    """Return set of blob_ids that have metadata records."""
    result = state_service.read_state(
        namespace=plugin_name,
        query={"table": "metadata", "filters": {}, "limit": _METADATA_TABLE_CEILING},
    )
    if result.get("action_status") != "completed":
        # Was `return set()`. The sole caller — filesystem_provider's
        # _cleanup_orphaned_files — treats every blob_id ABSENT from this set as
        # an orphan and unlinks its file. An empty set therefore does not mean
        # "no metadata", it means "delete every blob on disk".
        #
        # That made the row cap a data-destruction trigger rather than a
        # protection: once this table passed 10,000 rows the read would be
        # refused, the refusal would degrade to an empty set, and the next
        # cleanup pass would erase the entire blob store. Any transient database
        # error does the same thing.
        #
        # Fail loud instead. The caller wraps this in a blanket `except
        # Exception: pass`, so the raise skips that cleanup pass — which is the
        # safe direction for a destructive operation whose input is unavailable.
        raise RuntimeError(
            f"blob metadata read failed for namespace {plugin_name!r}; refusing to "
            f"report an empty metadata set, because the caller deletes every blob "
            f"file missing from it: {result.get('error')}"
        )
    data = result.get("data", {})
    records = data.get("records", [])
    if not isinstance(records, list):
        return set()
    # This set is used to decide which blobs are ORPHANED, so a truncated read
    # would silently mark live blobs as having no metadata. That is the exact
    # shape of harm the no-silent-truncation rule exists for: the caller cannot
    # tell a short answer from a complete one, and acts destructively on it.
    records = assert_within_ceiling(
        records,
        table="metadata",
        ceiling=_METADATA_TABLE_CEILING,
        reason=_METADATA_TABLE_CEILING_REASON,
    )
    return {r["blob_id"] for r in records if isinstance(r, dict) and "blob_id" in r}


def fetch_namespace_records(
    state_service: StateServiceProtocol,
    plugin_name: str,
    namespace: str,
) -> list[Any]:
    """Fetch all metadata records for a namespace (empty namespace = all namespaces)."""
    query_filters: dict[str, Any] = {}
    if namespace:
        query_filters["plugin_namespace"] = namespace
    result = state_service.read_state(
        namespace=plugin_name,
        query={"table": "metadata", "filters": query_filters},
    )
    if result.get("action_status") != "completed":
        return []
    data: dict[str, Any] = result.get("data") or {}
    records = data.get("records", [])
    return records if isinstance(records, list) else []
