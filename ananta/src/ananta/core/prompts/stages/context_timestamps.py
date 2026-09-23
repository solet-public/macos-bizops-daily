"""Event timestamp/trailer handling for ContextStage.

Module-level functions that build JSON metadata trailers for context
events and detect existing trailers to prevent duplication.
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from ananta.services.context_management.types import ContextEventType


def has_json_trailer(content: str) -> bool:
    """Detect an existing JSON metadata trailer at the end of content.

    Trailers are compact JSON objects on the last non-empty line containing
    ``"namespace"`` and ``"posted_at"`` keys.  APIStage appends these to
    live observation messages before persistence; this check prevents
    ContextStage from stacking a duplicate when reloading the event.

    Args:
        content: Message content to check

    Returns:
        True if the last line is a JSON trailer with namespace and posted_at
    """
    last_line = content.rstrip().rsplit("\n", maxsplit=1)[-1].strip()
    return (
        last_line.startswith("{")
        and last_line.endswith("}")
        and '"namespace"' in last_line
        and '"posted_at"' in last_line
    )


def append_event_timestamp(
    content: str,
    event_type: ContextEventType,
    event: dict[str, Any],
) -> str:
    """Append JSON metadata trailer to persisted message content.

    Produces a structured JSON trailer for routing provenance:
    - INPUT events: {"namespace", "posted_at"} (+ "source" when available)
    - OUTPUT events: {"namespace", "posted_at"} (+ "destination" when available)

    Uses the event's stored created_at (ISO8601 UTC, set once at storage time).
    This is cache-friendly because the timestamp never changes for a given event.

    Args:
        content: Message content to append trailer to
        event_type: The context event type (INPUT/OUTPUT)
        event: Event dict with created_at and optional metadata

    Returns:
        Content with JSON trailer appended, or unchanged if no timestamp
    """
    created_at = event.get("created_at")
    if not created_at:
        return content

    # Observation blocks persisted via living_to_ossified already carry a
    # trailer from APIStage._build_live_observation_trailer.  Skip to avoid
    # stacking a second one.
    if has_json_trailer(content):
        return content

    posted_at = format_iso8601(str(created_at))
    trailer: dict[str, str] = {}

    metadata = event.get("metadata")
    if isinstance(metadata, dict):
        namespace = metadata.get("source_namespace", "")
        if namespace:
            trailer["namespace"] = str(namespace)
        # INPUT events use "source", OUTPUT events use "destination"
        address = metadata.get("source", "")
        if address:
            address_key = (
                "source"
                if event_type == ContextEventType.INPUT
                else "destination"
            )
            trailer[address_key] = str(address)
    trailer["posted_at"] = posted_at

    # session_id is NOT included in prompt trailers.  The AQP injects
    # session_id from flow context post-inference, so the model never
    # needs to read it from the trailer.  Certain random session_id
    # strings (e.g. ``sess-2jai3ytcpwbrr``) trigger grammar-compiler
    # explosions in LM Studio's constrained decoding when present in
    # prompt content alongside a JSON response schema.

    return f"{content}\n\n{json.dumps(trailer, separators=(',', ':'))}"


def format_iso8601(iso_timestamp: str) -> str:
    """Format timestamp as ISO8601 with Z suffix for UTC.

    Args:
        iso_timestamp: ISO8601 timestamp string from database

    Returns:
        ISO8601 UTC timestamp like ``2026-01-04T10:30:45Z``
    """
    dt = datetime.fromisoformat(iso_timestamp)
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")
