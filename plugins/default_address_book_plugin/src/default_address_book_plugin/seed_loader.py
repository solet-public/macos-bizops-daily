"""Address book plugin seed loader — auto-registers entries from a profile JSON file."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from ananta.interfaces.state_service_protocol import StateServiceProtocol

from .address_ops import register_impl
from .address_queries import get_by_name


def _load_seed_payload(app_home: Path, plugin_name: str, logger: logging.Logger) -> list[Any]:
    """Read and validate the seed file structure. Returns the entries list or []."""
    seed_file = app_home / "config" / "plugins" / plugin_name / "entries.json"
    if not seed_file.is_file():
        logger.debug("no address book seed file at %s — skipping auto-seed", seed_file)
        return []
    try:
        payload = json.loads(seed_file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(
            f"{plugin_name}: failed to read seed file {seed_file}: {exc}"
        ) from exc
    if not isinstance(payload, dict):
        raise RuntimeError(
            f"{plugin_name}: seed file {seed_file} must contain a JSON object, "
            f"got {type(payload).__name__}"
        )
    entries = payload.get("entries")
    if not isinstance(entries, list):
        raise RuntimeError(f"{plugin_name}: seed file {seed_file} missing 'entries' list")
    return entries  # type: ignore[return-value]


def _validate_field_entry(fe: Any, j: int, entry_name: str, plugin_name: str) -> dict[str, str]:
    """Validate a single field_type/description/value dict. Raises on bad shape."""
    if not isinstance(fe, dict):
        raise RuntimeError(
            f"{plugin_name}: seed entry '{entry_name}' field #{j} must be an object"
        )
    for required in ("field_type", "description", "value"):
        if required not in fe or not isinstance(fe[required], str):
            raise RuntimeError(
                f"{plugin_name}: seed entry '{entry_name}' field #{j} "
                f"missing string field '{required}'"
            )
    return {
        "field_type": fe["field_type"],
        "description": fe["description"],
        "value": fe["value"],
    }


def _validate_tags_field(
    tags_raw: Any,
    entry_name: str,
    plugin_name: str,
) -> None:
    """Raise if tags_raw is not a list of strings."""
    if not isinstance(tags_raw, list) or not all(isinstance(t, str) for t in tags_raw):
        raise RuntimeError(
            f"{plugin_name}: seed entry '{entry_name}' 'tags' must be a list of strings"
        )


def _validate_required_string_fields(
    name: Any,
    address_type: Any,
    description: Any,
    entries_raw: Any,
    index: int,
    plugin_name: str,
) -> None:
    """Raise if name, address_type, description, or entries_raw fail basic shape checks."""
    if not isinstance(name, str) or not name:
        raise RuntimeError(f"{plugin_name}: seed entry #{index} missing 'name'")
    if not isinstance(address_type, str) or not address_type:
        raise RuntimeError(f"{plugin_name}: seed entry '{name}' missing 'address_type'")
    if not isinstance(description, str):
        raise RuntimeError(f"{plugin_name}: seed entry '{name}' has non-string 'description'")
    if not isinstance(entries_raw, list) or not entries_raw:
        raise RuntimeError(
            f"{plugin_name}: seed entry '{name}' must have a non-empty 'entries' list"
        )


def _parse_seed_entry(
    raw: Any,
    index: int,
    plugin_name: str,
) -> tuple[str, str, str, list[str], list[dict[str, str]]]:
    """Validate and normalise a single seed entry."""
    if not isinstance(raw, dict):
        raise RuntimeError(f"{plugin_name}: seed entry #{index} in seed file must be an object")

    name = raw.get("name")
    address_type = raw.get("address_type")
    description = raw.get("description", "")
    tags_raw = raw.get("tags", [])
    entries_raw = raw.get("entries")

    _validate_required_string_fields(name, address_type, description, entries_raw, index, plugin_name)
    _validate_tags_field(tags_raw, str(name), plugin_name)
    assert isinstance(entries_raw, list)

    field_entries = [
        _validate_field_entry(fe, j, str(name), plugin_name)
        for j, fe in enumerate(entries_raw)
    ]
    return str(name), str(address_type), str(description), list(tags_raw), field_entries


def auto_seed_entries_from_file(
    app_home: Path,
    plugin_name: str,
    logger: logging.Logger,
    state_service: StateServiceProtocol,
    memory_service: Any,
    auto_ingest_enabled: bool,
) -> None:
    """Auto-register entries from a profile-side JSON file, if present.

    File location: ``$APP_HOME/config/plugins/default_address_book_plugin/entries.json``.
    Idempotent — entries that already exist (by name) are skipped.
    """
    entries = _load_seed_payload(app_home, plugin_name, logger)
    if not entries:
        return

    registered = 0
    skipped = 0
    for index, raw in enumerate(entries):
        name, address_type, description, tags, field_entries = _parse_seed_entry(
            raw, index, plugin_name
        )
        if get_by_name(state_service, plugin_name, name):
            logger.debug("address book entry '%s' already present — skipping seed", name)
            skipped += 1
            continue

        result = register_impl(
            state_service, plugin_name, memory_service, auto_ingest_enabled,
            logger, name, address_type, description, field_entries, tags,
        )
        if result.get("action_status") != "completed":
            raise RuntimeError(
                f"{plugin_name}: failed to seed address book entry '{name}': "
                f"{result.get('error')}"
            )
        registered += 1

    logger.info(
        "Address book auto-seed: registered=%d skipped=%d",
        registered,
        skipped,
    )
