"""Address book plugin memory service integration helpers."""

from __future__ import annotations

import contextlib
import logging
from typing import Any

from ananta.error_handling import FrameworkError

_BACKEND_NOT_READY_CODE = "memory.backend_unavailable"


def format_for_memory(
    name: str,
    address_type: str,
    description: str,
    entries: list[dict[str, str]],
    tags: list[str],
) -> str:
    entry_lines = [
        f"  - {e['field_type']}: {e['description']} = {e['value']}" for e in entries
    ]
    entries_str = "\n".join(entry_lines) if entry_lines else "  (no entries)"
    tags_str = ", ".join(tags) if tags else "none"
    return (
        f"Address Book: {name}\n"
        f"Type: {address_type}\n"
        f"Description: {description}\n"
        f"Tags: {tags_str}\n"
        f"Entries:\n"
        f"{entries_str}"
    )


def ingest_to_memory(
    memory_service: Any,
    auto_ingest_enabled: bool,
    name: str,
    address_type: str,
    description: str,
    entries: list[dict[str, str]],
    tags: list[str],
    logger: logging.Logger,
) -> str | None:
    if not memory_service or not auto_ingest_enabled:
        return None
    try:
        memory_content = format_for_memory(name, address_type, description, entries, tags)
        memory_tags = ["address-book", f"type:{address_type}"] + tags
        result = memory_service.remember(content=memory_content, tags=memory_tags)
        # memory_service.remember returns the actr backend shape directly:
        # {"memory_id": <str>, "message": <str>} — memory_id is top-level, NOT
        # under a "data" key. Reading result["data"]["memory_id"] silently
        # returned None on every registration, leaving address.memory_id unlinked
        # (memory created + recallable, but never back-linked → orphaned on delete).
        if isinstance(result, dict):
            memory_id = result.get("memory_id")
            if isinstance(memory_id, str):
                logger.debug(f"Address '{name}' ingested to memory: {memory_id}")
                return memory_id
        return None
    except FrameworkError as e:
        if getattr(e, "error_code", None) == _BACKEND_NOT_READY_CODE:
            logger.debug(
                f"Address '{name}' memory ingest deferred — backend not yet initialized"
            )
            return None
        logger.error(f"Failed to ingest address to memory: {e}")
        return None
    except Exception as e:
        logger.error(f"Failed to ingest address to memory: {e}")
        return None


def strengthen_memory(
    memory_service: Any,
    strengthen_enabled: bool,
    description: str,
) -> None:
    if not memory_service or not strengthen_enabled:
        return
    with contextlib.suppress(Exception):
        memory_service.recall(query=description, top_k=1)


def archive_memory(
    memory_service: Any,
    memory_id: str,
    logger: logging.Logger,
) -> None:
    if not memory_service or not memory_id:
        return
    try:
        memory_service.forget(memory_id=memory_id)
        logger.debug(f"Memory archived: {memory_id}")
    except Exception as e:
        logger.error(f"Failed to archive memory: {e}")
