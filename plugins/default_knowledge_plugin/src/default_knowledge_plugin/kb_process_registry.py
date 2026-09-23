"""Process registry refresh helpers for the default knowledge plugin.

Covers: resolving JSON process files, building registry update dicts,
executing bulk or single-key registry refreshes.
No plugin instance — services are passed as explicit parameters.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from ananta.core.process_registry.kb_overlay_loader import apply_deprecation

from .constants import PLUGIN_NAME

logger = logging.getLogger(__name__)

_RESULT_CUSTOMIZATION_TEXT_FIELDS: frozenset[str] = frozenset({
    "action_label", "result_type", "result_description",
    "presentation_guidance", "output_action_guidance",
})
_ERROR_CUSTOMIZATION_TEXT_FIELDS: frozenset[str] = frozenset({
    "action_context", "error_interpretation", "recovery_guidance",
})


def resolve_processes_dir(plugin_name: str, kb_root: Path) -> Path:
    """Resolve the processes/ directory for a plugin or platform services."""
    project_root = kb_root.parent
    if plugin_name == "ananta":
        return project_root / "ananta" / "knowledge_base" / "processes"
    return project_root / "plugins" / plugin_name / "knowledge_base" / "processes"


def resolve_process_json_path(
    plugin_name: str, process_key: str, kb_root: Path,
) -> Path:
    """Resolve path to a specific process JSON file from its process_key."""
    processes_dir = resolve_processes_dir(plugin_name, kb_root)
    parts = process_key.split("::")
    if parts[0] == "service_interface" and len(parts) == 3:
        return processes_dir / parts[1] / f"{parts[2]}.json"
    if parts[0] == "plugin" and len(parts) == 3:
        return processes_dir / f"{parts[2]}.json"
    raise ValueError(f"Invalid process_key format: {process_key}")


def collect_owned_registry_keys(
    registry: dict[str, object], plugin_name: str,
) -> set[str]:
    """Collect all registry keys owned by a plugin or platform services."""
    processes = registry.get("processes")
    if not isinstance(processes, dict):
        return set()
    owned: set[str] = set()
    for key, entry in processes.items():
        if not isinstance(entry, dict):
            continue
        if plugin_name == "ananta":
            if entry.get("provider_type") == "service_interface":
                owned.add(key)
        elif (
            entry.get("provider_type") == "plugin"
            and entry.get("provider") == plugin_name
        ):
            owned.add(key)
    return owned


def merge_customizations(
    json_custs: dict[str, object],
    current_entry: dict[str, object],
    entry_key: str,
    text_fields: frozenset[str],
) -> dict[str, object]:
    """Merge knowledge base text fields into an existing registry customization."""
    existing = current_entry.get(entry_key)
    if isinstance(existing, dict):
        merged = dict(existing)
        for field in text_fields:
            if field in json_custs:
                merged[field] = json_custs[field]
        return merged
    return dict(json_custs)


def get_registry_entry(
    registry: dict[str, object], process_key: str,
) -> dict[str, object] | None:
    """Look up a process entry from the live registry dict."""
    processes = registry.get("processes")
    if not isinstance(processes, dict):
        return None
    entry = processes.get(process_key)
    return entry if isinstance(entry, dict) else None


def _fold_deprecation_updates(
    json_data: dict[str, Any], updates: dict[str, object],
) -> None:
    """Fold a JSON ``deprecation`` block into refresh updates.

    Reuses the restart-path ``apply_deprecation`` derivation against a scratch
    entry so the live-refresh path produces the identical ``deprecation`` +
    derived ``is_discoverable`` fields. No-op when no block is present; raises
    ``FrameworkError`` on a malformed block (fail-fast, like the restart path).
    """
    scratch: dict[str, object] = {}
    apply_deprecation(scratch, json_data)
    updates.update(scratch)


def build_refresh_updates(
    json_data: dict[str, Any],
    process_key: str,
    registry: dict[str, object],
) -> dict[str, object]:
    """Build update dict for a single process from its JSON data."""
    updates: dict[str, object] = {}

    for field in ("display_name", "description", "embedding_description"):
        if field in json_data:
            updates[field] = json_data[field]

    # POR §4.6 rider: honor a ``deprecation`` block on the LIVE-REFRESH path.
    # The Tier-2 overlay (``apply_deprecation``) only ran at RESTART (the full
    # overlay merge); this lighter refresh merge dropped it, so a deprecation
    # edit did not take effect without a restart. Reuse the SAME derivation so
    # refresh and restart converge: the block is surfaced on the entry and
    # ``active_retrieval: false`` DERIVES ``is_discoverable: false`` (demote
    # from process_search while staying callable). ``apply_deprecation`` fails
    # loud on a malformed block, matching the restart path's fail-fast. This
    # runs before the ``current_entry`` early-return so it applies whether or
    # not the entry is already resolvable in the live registry.
    _fold_deprecation_updates(json_data, updates)

    current_entry = get_registry_entry(registry, process_key)
    if current_entry is None:
        return updates

    if "action_definition_template_arguments" in json_data:
        template = current_entry.get("action_definition_template")
        if isinstance(template, dict):
            merged_template = dict(template)
            merged_template["arguments"] = json_data["action_definition_template_arguments"]
            updates["action_definition_template"] = merged_template

    json_result_custs = json_data.get("result_processor_customizations")
    if isinstance(json_result_custs, dict):
        updates["result_processor_customizations"] = merge_customizations(
            json_result_custs, current_entry,
            "result_processor_customizations", _RESULT_CUSTOMIZATION_TEXT_FIELDS,
        )

    json_error_custs = json_data.get("error_processor_customizations")
    if isinstance(json_error_custs, dict):
        updates["error_processor_customizations"] = merge_customizations(
            json_error_custs, current_entry,
            "error_processor_customizations", _ERROR_CUSTOMIZATION_TEXT_FIELDS,
        )

    return updates


def do_refresh_plugin_processes(
    plugin_name: str, kb_root: Path, orchestrator_ref: Any,
) -> dict[str, Any]:
    """Reload all process JSON files and update the live process registry."""
    processes_dir = resolve_processes_dir(plugin_name, kb_root)
    if not processes_dir.is_dir():
        raise FileNotFoundError(f"Processes directory not found: {processes_dir}")

    registry = orchestrator_ref.get_process_registry()
    owned_keys = collect_owned_registry_keys(registry, plugin_name)

    all_updates: dict[str, dict[str, object]] = {}
    json_keys: set[str] = set()
    errors: list[str] = []

    for json_path in sorted(processes_dir.rglob("*.json")):
        try:
            json_data = json.loads(json_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as e:
            errors.append(f"Failed to read {json_path.name}: {e}")
            continue

        file_pk = json_data.get("process_key", "")
        if not file_pk:
            errors.append(f"{json_path.name}: missing process_key")
            continue

        json_keys.add(file_pk)
        updates = build_refresh_updates(json_data, file_pk, registry)
        all_updates[file_pk] = updates

    stale_keys = owned_keys - json_keys
    if stale_keys:
        raise ValueError(
            f"Stale registry entries with no JSON files: {sorted(stale_keys)}"
        )

    if errors:
        raise ValueError(f"JSON read errors: {errors}")

    result = orchestrator_ref.apply_knowledge_base_updates(all_updates)
    result_errors = result.get("errors", [])
    return {
        "status": "success" if not result_errors else "partial",
        "plugin_name": plugin_name,
        "updated_count": result.get("updated_count", 0),
        "process_keys": result.get("process_keys", []),
        "errors": result_errors,
    }


def do_refresh_plugin_process(
    plugin_name: str, process_key: str, kb_root: Path, orchestrator_ref: Any,
) -> dict[str, Any]:
    """Reload a single process JSON file and update the live registry entry."""
    json_path = resolve_process_json_path(plugin_name, process_key, kb_root)
    if not json_path.exists():
        raise FileNotFoundError(f"Process JSON file not found: {json_path}")

    json_data = json.loads(json_path.read_text(encoding="utf-8"))

    file_pk = json_data.get("process_key", "")
    if file_pk != process_key:
        raise ValueError(
            f"process_key mismatch: expected '{process_key}', "
            f"got '{file_pk}' in {json_path.name}"
        )

    registry = orchestrator_ref.get_process_registry()
    updates = build_refresh_updates(json_data, process_key, registry)

    result = orchestrator_ref.apply_knowledge_base_updates({process_key: updates})
    result_errors = result.get("errors", [])
    return {
        "status": "success" if not result_errors else "error",
        "plugin_name": plugin_name,
        # NOT "process_key": at the top level of a result envelope that name is
        # reserved for the key of the verb that PRODUCED the result, and the
        # result-contract invariant enforces it. Using it for the key being
        # refreshed made every call raise RESULT_CONTRACT_VIOLATION *after* the
        # side-effect had already landed and the row was stored completed.
        "refreshed_process_key": process_key,
        "updated": bool(result.get("updated_count", 0)),
        "errors": result_errors,
    }


def registry_has_process_key(
    process_key: str, orchestrator_ref: Any,
) -> bool:
    """Return whether process_key resolves in the live process registry."""
    if orchestrator_ref is None:
        raise RuntimeError(f"{PLUGIN_NAME}: orchestrator_ref not set")
    registry = orchestrator_ref.get_process_registry()
    processes = registry.get("processes")
    return isinstance(processes, dict) and process_key in processes
