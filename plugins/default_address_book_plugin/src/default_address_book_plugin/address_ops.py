"""Address book plugin CRUD implementation and vault resolution helpers."""

from __future__ import annotations

import logging
from typing import Any

from ananta.core.domain.types import ActionResult
from ananta.interfaces.state_service_protocol import StateServiceProtocol
from ananta.services.state_service.bounded_read import assert_within_ceiling

from .address_queries import (
    count_tags_in_rows,
    fetch_address_rows,
    filter_rows,
    get_by_name,
    get_entries_for_address,
    get_entry_by_id,
)
from .constants import ErrorCode
from .memory_integration import archive_memory, ingest_to_memory, strengthen_memory
from .result_helpers import error, now, success

# The address book is a HUMAN-CURATED contact list: rows arrive one at a time
# through explicit `register` calls, never from traffic, telemetry, or automated
# ingestion. Its size is bounded by how many contacts a person or deployment
# chooses to enter. 50,000 is far above any plausible curated book while still
# being a real bound.
#
# If this ceiling ever trips, the assumption that broke is "a human entered every
# one of these" — something is writing address rows programmatically, and that is
# the bug to chase rather than a number to raise.
# MEASURED 2026-08-15: default_address_book_plugin.address holds 26 rows.
_ADDRESS_TABLE_CEILING = 50_000
_ADDRESS_TABLE_CEILING_REASON = (
    "the address table is a human-curated contact list written one entry at a "
    "time by explicit registration, so it is bounded by curation and not by "
    "traffic (measured: 26 rows)."
)

# ── Registration ─────────────────────────────────────────────────────────────

def validate_entries(
    entries: list[dict[str, str]],
) -> ActionResult | None:
    """Validate entry shape only. Name-uniqueness is deliberately NOT checked:
    register is an idempotent upsert (replace-by-live-name), so an existing
    name is a valid update target, never a name_exists conflict."""
    for i, entry in enumerate(entries):
        if not all(k in entry for k in ("field_type", "description", "value")):
            return error(
                ErrorCode.INVALID_ENTRY,
                f"Entry {i} missing required fields: field_type, description, value",
            )
    return None


def extract_generated_id(result: Any) -> tuple[str, ActionResult | None]:
    if not isinstance(result, dict):
        return "", error(ErrorCode.INVALID_ENTRY, "Invalid state service response")
    data = result.get("data", {})
    if not isinstance(data, dict):
        return "", error(ErrorCode.INVALID_ENTRY, "Invalid state service response")
    result_obj = data.get("result")
    if not isinstance(result_obj, dict):
        return "", error(
            ErrorCode.INVALID_ENTRY,
            "Invalid state service response: missing result object",
        )
    address_id = result_obj.get("generated_id")
    if not address_id:
        return "", error(
            ErrorCode.INVALID_ENTRY,
            "Invalid state service response: missing generated_id",
        )
    return str(address_id), None


def insert_address_record(
    state_service: StateServiceProtocol,
    namespace: str,
    name: str,
    address_type: str,
    description: str,
    tags: list[str],
    timestamp: str,
) -> tuple[str, ActionResult | None]:
    address_result = state_service.write_state(
        namespace=namespace,
        data={
            "table": "address",
            "record": {
                "name": name,
                "address_type": address_type,
                "description": description,
                "tags": tags,
                "created_at": timestamp,
                "updated_at": timestamp,
            },
        },
    )
    return extract_generated_id(address_result)


def insert_entry_records(
    state_service: StateServiceProtocol,
    namespace: str,
    address_id: str,
    entries: list[dict[str, str]],
    timestamp: str,
) -> None:
    for i, entry in enumerate(entries):
        state_service.write_state(
            namespace=namespace,
            data={
                "table": "address_entry",
                "record": {
                    "default_address_book_plugin__address_id": address_id,
                    "field_type": entry["field_type"],
                    "description": entry["description"],
                    "value": entry["value"],
                    "sort_order": i,
                    "created_at": timestamp,
                    "updated_at": timestamp,
                },
            },
        )


def finalize_registration(
    state_service: StateServiceProtocol,
    namespace: str,
    address_id: str,
    name: str,
    address_type: str,
    description: str,
    entries: list[dict[str, str]],
    tags: list[str],
    memory_service: Any,
    auto_ingest_enabled: bool,
    logger: logging.Logger,
) -> str | None:
    memory_id = ingest_to_memory(
        memory_service, auto_ingest_enabled,
        name, address_type, description, entries, tags, logger,
    )
    if memory_id:
        state_service.update_state(
            namespace=namespace,
            query={"table": "address", "filters": {"id": address_id}},
            updates={"memory_id": memory_id},
        )
    return memory_id


def register_impl(
    state_service: StateServiceProtocol,
    namespace: str,
    memory_service: Any,
    auto_ingest_enabled: bool,
    logger: logging.Logger,
    name: str,
    address_type: str,
    description: str,
    entries: list[dict[str, str]],
    tags: list[str],
) -> ActionResult:
    entry_error = validate_entries(entries)
    if entry_error:
        return entry_error

    timestamp = now()
    existing = get_by_name(state_service, namespace, name)
    if existing:
        return replace_existing_address(
            state_service, namespace, memory_service, auto_ingest_enabled, logger,
            existing, name, address_type, description, entries, tags, timestamp,
        )

    address_id, insert_error = insert_address_record(
        state_service, namespace, name, address_type, description, tags, timestamp,
    )
    if insert_error:
        return insert_error

    insert_entry_records(state_service, namespace, address_id, entries, timestamp)
    memory_id = finalize_registration(
        state_service, namespace, address_id,
        name, address_type, description, entries, tags,
        memory_service, auto_ingest_enabled, logger,
    )

    logger.debug(f"Address registered: {name}")
    return success({"address_id": address_id, "memory_id": memory_id})


def replace_existing_address(
    state_service: StateServiceProtocol,
    namespace: str,
    memory_service: Any,
    auto_ingest_enabled: bool,
    logger: logging.Logger,
    existing: dict[str, Any],
    name: str,
    address_type: str,
    description: str,
    entries: list[dict[str, str]],
    tags: list[str],
    timestamp: str,
) -> ActionResult:
    """Replace a live address in place (the upsert-update path): archive the
    old memory, hard-swap entries, update metadata, and re-ingest + relink
    memory. Keeps the same surrogate ``id`` so any references stay stable."""
    address_id = str(existing["id"])

    old_memory_id = existing.get("memory_id")
    if isinstance(old_memory_id, str):
        archive_memory(memory_service, old_memory_id, logger)

    state_service.delete_records(
        namespace=namespace,
        query={
            "table": "address_entry",
            "filters": {"default_address_book_plugin__address_id": address_id},
            "soft_delete": False,
        },
    )
    insert_entry_records(state_service, namespace, address_id, entries, timestamp)

    memory_id = ingest_to_memory(
        memory_service, auto_ingest_enabled,
        name, address_type, description, entries, tags, logger,
    )
    state_service.update_state(
        namespace=namespace,
        query={"table": "address", "filters": {"id": address_id}},
        updates={
            "address_type": address_type,
            "description": description,
            "tags": tags,
            "memory_id": memory_id,
            "updated_at": timestamp,
        },
    )

    logger.debug(f"Address upserted (replaced in place): {name}")
    return success({"address_id": address_id, "memory_id": memory_id})


# ── Resolve ───────────────────────────────────────────────────────────────────

def resolve_impl(
    state_service: StateServiceProtocol,
    namespace: str,
    memory_service: Any,
    strengthen_enabled: bool,
    name: str,
) -> ActionResult:
    address = get_by_name(state_service, namespace, name)
    if not address:
        return error(ErrorCode.NOT_FOUND, f"Address '{name}' not found")

    entries = get_entries_for_address(state_service, namespace, address["id"])

    if address.get("memory_id"):
        desc = address.get("description")
        if isinstance(desc, str):
            strengthen_memory(memory_service, strengthen_enabled, desc)

    return success({**address, "entries": entries})


# ── Vault resolution ──────────────────────────────────────────────────────────

def apply_vault_result(
    entry: dict[str, Any],
    vault_key: str,
    secret_result: Any,
) -> ActionResult | None:
    if not isinstance(secret_result, dict):
        return None
    if secret_result.get("action_status") == "completed":
        secret_data = secret_result.get("data", {})
        if isinstance(secret_data, dict):
            entry["value"] = secret_data.get("value", "")
            entry["_resolved_from_vault"] = True
        return None
    vault_error = secret_result.get("error", {})
    if isinstance(vault_error, dict):
        return error(
            str(vault_error.get("code", "vault.error")),
            f"Failed to resolve vault reference '{vault_key}': "
            f"{vault_error.get('message', 'unknown error')}",
        )
    return None


def resolve_single_vault_entry(
    vault_service: Any,
    entry: dict[str, Any],
) -> ActionResult | None:
    value = entry.get("value", "")
    if not isinstance(value, str) or not value.startswith("vault::"):
        return None
    vault_key = value[7:]
    try:
        secret_result = vault_service.retrieve(vault_key)
        return apply_vault_result(entry, vault_key, secret_result)
    except Exception as e:
        return error("vault.error", f"Failed to resolve vault reference '{vault_key}': {e}")


def resolve_vault_references_in_result(
    vault_service: Any,
    result: ActionResult,
) -> ActionResult | None:
    data = result.get("data", {})
    if not isinstance(data, dict):  # type: ignore[reportUnnecessaryIsInstance]
        return None
    entries = data.get("entries", [])
    if not isinstance(entries, list):
        return None
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        vault_error = resolve_single_vault_entry(vault_service, entry)
        if vault_error:
            return vault_error
    return None


def resolve_with_secrets_impl(
    state_service: StateServiceProtocol,
    namespace: str,
    memory_service: Any,
    strengthen_enabled: bool,
    vault_service: Any,
    logger: logging.Logger,
    name: str,
) -> ActionResult:
    result = resolve_impl(state_service, namespace, memory_service, strengthen_enabled, name)
    if result.get("action_status") != "completed":
        return result
    if not vault_service:
        logger.error("vault_service not injected - vault references not resolved")
        return result
    vault_error = resolve_vault_references_in_result(vault_service, result)
    if vault_error:
        return vault_error
    return result


# ── Entry CRUD ────────────────────────────────────────────────────────────────

def add_entry_impl(
    state_service: StateServiceProtocol,
    namespace: str,
    logger: logging.Logger,
    name: str,
    field_type: str,
    description: str,
    value: str,
) -> ActionResult:
    address = get_by_name(state_service, namespace, name)
    if not address:
        return error(ErrorCode.NOT_FOUND, f"Address '{name}' not found")

    entries = get_entries_for_address(state_service, namespace, address["id"])
    max_order = max((e.get("sort_order", 0) for e in entries), default=-1)

    timestamp = now()
    result = state_service.write_state(
        namespace=namespace,
        data={
            "table": "address_entry",
            "record": {
                "default_address_book_plugin__address_id": address["id"],
                "field_type": field_type,
                "description": description,
                "value": value,
                "sort_order": max_order + 1,
                "created_at": timestamp,
                "updated_at": timestamp,
            },
        },
    )

    entry_id = None
    if isinstance(result, dict):  # type: ignore[reportUnnecessaryIsInstance]
        data = result.get("data", {})
        if isinstance(data, dict):  # type: ignore[reportUnnecessaryIsInstance]
            result_obj = data.get("result")
            if isinstance(result_obj, dict):
                entry_id = result_obj.get("generated_id")

    logger.debug(f"Entry added to '{name}': {entry_id}")
    return success({"entry_id": entry_id})


def update_entry_impl(
    state_service: StateServiceProtocol,
    namespace: str,
    entry_id: str,
    field_type: str | None,
    description: str | None,
    value: str | None,
) -> ActionResult:
    entry = get_entry_by_id(state_service, namespace, str(entry_id))
    if not entry:
        return error(ErrorCode.ENTRY_NOT_FOUND, f"Entry {entry_id} not found")

    updates: dict[str, Any] = {"updated_at": now()}
    if field_type is not None:
        updates["field_type"] = field_type
    if description is not None:
        updates["description"] = description
    if value is not None:
        updates["value"] = value

    state_service.update_state(
        namespace=namespace,
        query={"table": "address_entry", "filters": {"id": entry_id}},
        updates=updates,
    )

    updated = get_entry_by_id(state_service, namespace, str(entry_id))
    return success(updated or {})


def delete_entry_impl(
    state_service: StateServiceProtocol,
    namespace: str,
    logger: logging.Logger,
    entry_id: str,
) -> ActionResult:
    entry = get_entry_by_id(state_service, namespace, str(entry_id))
    if not entry:
        return error(ErrorCode.ENTRY_NOT_FOUND, f"Entry {entry_id} not found")

    state_service.delete_records(
        namespace=namespace,
        query={"table": "address_entry", "filters": {"id": entry_id}},
    )

    logger.debug(f"Entry deleted: {entry_id}")
    return success({"entry_id": entry_id, "message": "Entry deleted"})


def update_impl(
    state_service: StateServiceProtocol,
    namespace: str,
    logger: logging.Logger,
    name: str,
    address_type: str | None,
    description: str | None,
    tags: list[str] | None,
) -> ActionResult:
    address = get_by_name(state_service, namespace, name)
    if not address:
        return error(ErrorCode.NOT_FOUND, f"Address '{name}' not found")

    updates: dict[str, Any] = {"updated_at": now()}
    if address_type is not None:
        updates["address_type"] = address_type
    if description is not None:
        updates["description"] = description
    if tags is not None:
        updates["tags"] = tags

    state_service.update_state(
        namespace=namespace,
        query={"table": "address", "filters": {"name": name}},
        updates=updates,
    )

    updated = get_by_name(state_service, namespace, name)
    logger.debug(f"Address updated: {name}")
    return success(updated or {})


def delete_impl(
    state_service: StateServiceProtocol,
    namespace: str,
    memory_service: Any,
    logger: logging.Logger,
    name: str,
) -> ActionResult:
    address = get_by_name(state_service, namespace, name)
    if not address:
        return error(ErrorCode.NOT_FOUND, f"Address '{name}' not found")

    memory_id = address.get("memory_id")
    if isinstance(memory_id, str):
        archive_memory(memory_service, memory_id, logger)

    address_id = str(address["id"])
    # HARD delete (soft_delete=False). A soft-deleted row keeps occupying the
    # name lookup slot and orphans its entries; hard delete frees the name for
    # re-registration and removes the child entries. Entries first, then row.
    state_service.delete_records(
        namespace=namespace,
        query={
            "table": "address_entry",
            "filters": {"default_address_book_plugin__address_id": address_id},
            "soft_delete": False,
        },
    )
    state_service.delete_records(
        namespace=namespace,
        query={"table": "address", "filters": {"id": address_id}, "soft_delete": False},
    )

    logger.debug(f"Address hard-deleted: {name}")
    return success({"name": name, "message": "Address deleted"})


# ── Search / list ─────────────────────────────────────────────────────────────

def search_impl(
    state_service: StateServiceProtocol,
    namespace: str,
    query: str | None,
    address_type: str | None,
    tag: str | None,
    limit: int,
) -> ActionResult:
    rows = fetch_address_rows(state_service, namespace, address_type)
    rows = filter_rows(rows, query, tag)
    rows = rows[:limit]
    return success({"addresses": rows, "count": len(rows)})


def list_types_impl(
    state_service: StateServiceProtocol,
    namespace: str,
) -> ActionResult:
    # Counting rows per address_type needs the rows, because the state interface
    # has no grouped aggregate. So the read declares its ceiling and refuses past
    # it rather than silently reporting type counts computed from a prefix — a
    # partial breakdown here would look exactly like a complete one.
    result = state_service.read_state(
        namespace=namespace,
        query={
            "table": "address",
            "limit": _ADDRESS_TABLE_CEILING,
            # Conscious opt-in to a scan larger than the platform DEFAULT bound
            # (100), not a removal of the bound: the limit above IS the bound and
            # assert_within_ceiling below makes reaching it loud. An address book
            # with more than 100 contacts is entirely ordinary, so the default
            # would refuse legitimate reads here.
            "unbounded": True,
        },
    )
    rows: list[dict[str, Any]] = []
    if isinstance(result, dict):  # type: ignore[reportUnnecessaryIsInstance]
        data = result.get("data", {})
        if isinstance(data, dict):  # type: ignore[reportUnnecessaryIsInstance]
            result_rows = data.get("records", [])
            if isinstance(result_rows, list):
                rows = [r for r in result_rows if isinstance(r, dict)]
    rows = assert_within_ceiling(
        rows,
        table="address",
        ceiling=_ADDRESS_TABLE_CEILING,
        reason=_ADDRESS_TABLE_CEILING_REASON,
    )

    type_counts: dict[str, int] = {}
    for r in rows:
        t = str(r.get("address_type", "unknown"))
        type_counts[t] = type_counts.get(t, 0) + 1

    types = [{"type": t, "count": c} for t, c in sorted(type_counts.items())]
    return success({"types": types, "total": len(types)})


def list_tags_impl(
    state_service: StateServiceProtocol,
    namespace: str,
) -> ActionResult:
    rows = fetch_address_rows(state_service, namespace)
    tag_counts = count_tags_in_rows(rows)
    tags = [{"tag": t, "count": c} for t, c in sorted(tag_counts.items())]
    return success({"tags": tags, "total": len(tags)})
