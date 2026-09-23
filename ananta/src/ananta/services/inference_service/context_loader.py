"""Context message loading and event-to-message conversion.

Platform functions that replace plugin methods for reading context
history back from storage. Replaces plugin methods:
  _load_context_messages, _load_snapshot_summary_message,
  _convert_event_to_message, _require_context_services
"""

from __future__ import annotations

import logging
from typing import Any, Protocol

from ananta.services.context_management.types import (
    CONTEXT_EVENT_TO_MESSAGE_ROLE,
    ContextEventType,
    MessageRole,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Protocols — narrow dependency contracts
# ---------------------------------------------------------------------------

class ContextEventService(Protocol):
    """Narrow protocol for context event read operations."""

    def list_all_events(self, context_id: str) -> list[dict[str, Any]]: ...

    def list_events_after_snapshot(
        self,
        context_id: str,
        end_event_id: str,
        limit: int = 1000,
    ) -> list[dict[str, Any]]: ...


class SnapshotService(Protocol):
    """Narrow protocol for snapshot operations."""

    def get_latest_snapshot(
        self, context_id: str,
    ) -> dict[str, Any] | None: ...


class ContentReader(Protocol):
    """Narrow protocol for reading stored content."""

    def read_text(self, relative_path: str) -> str: ...


class ContextServiceFacade(Protocol):
    """Narrow facade matching ContextManagementService shape."""

    @property
    def events(self) -> ContextEventService: ...

    @property
    def snapshots(self) -> SnapshotService: ...


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

def require_context_services(
    context_service: ContextServiceFacade | None,
    content_storage: ContentReader | None,
) -> tuple[ContextServiceFacade, ContentReader]:
    """Validate and return required context services.

    Raises:
        RuntimeError: If either service is not available.
    """
    if context_service is None:
        raise RuntimeError(
            "Cannot load context: context_management_service not available",
        )
    if content_storage is None:
        raise RuntimeError(
            "Cannot load context: content_storage not available",
        )
    return context_service, content_storage


# ---------------------------------------------------------------------------
# Snapshot summary
# ---------------------------------------------------------------------------

def load_snapshot_summary_message(
    snapshot: dict[str, Any],
    context_id: str,
    content_storage: ContentReader,
) -> dict[str, str]:
    """Load snapshot summary and convert to message format.

    Args:
        snapshot: Snapshot record from context management service.
        context_id: The context stream ID (for error messages).
        content_storage: Storage service to read summary content.

    Returns:
        Message dict with 'role' and 'content' keys.

    Raises:
        RuntimeError: If snapshot has invalid data or file is missing.
    """
    summary_path = str(snapshot.get("summary_path", ""))
    if not summary_path:
        snapshot_id = snapshot.get("id")
        raise RuntimeError(
            f"Snapshot has empty summary_path - data corruption detected. "
            f"snapshot_id={snapshot_id}, context_id={context_id}",
        )
    try:
        summary_content = content_storage.read_text(summary_path)
    except FileNotFoundError as exc:
        raise RuntimeError(
            f"Snapshot file not found: {summary_path}. "
            f"Data corruption - snapshot record exists but file missing.",
        ) from exc

    return {
        "role": MessageRole.SYSTEM.value,
        "content": f"[Previous context summary]\n{summary_content}",
    }


# ---------------------------------------------------------------------------
# Event-to-message conversion
# ---------------------------------------------------------------------------

def convert_event_to_message(
    event: dict[str, Any],
    context_id: str,
    content_storage: ContentReader,
) -> dict[str, str]:
    """Convert a context event to message format.

    Args:
        event: Event record from context management service.
        context_id: The context stream ID (for error messages).
        content_storage: Storage service to read event content.

    Returns:
        Message dict with 'role' and 'content' keys.

    Raises:
        RuntimeError: If event has invalid data or file is missing.
    """
    content_path = str(event.get("content_path", ""))
    event_type_str = str(event.get("event_type", ""))
    event_id = event.get("id")

    if not content_path:
        raise RuntimeError(
            f"Event missing required content_path. "
            f"context_id={context_id}, event_id={event_id}",
        )

    content = _read_event_content(
        content_path, context_id, event_id, content_storage,
    )
    event_type = _parse_event_type(event_type_str, context_id, event_id)
    role = _resolve_message_role(event_type, context_id, event_id)
    return {"role": role.value, "content": content}


def _read_event_content(
    content_path: str,
    context_id: str,
    event_id: Any,
    content_storage: ContentReader,
) -> str:
    """Read event content file from storage."""
    try:
        return content_storage.read_text(content_path)
    except FileNotFoundError as exc:
        raise RuntimeError(
            f"Event content file not found: {content_path}. "
            f"Data corruption - event record exists but file missing. "
            f"context_id={context_id}, event_id={event_id}",
        ) from exc


def _parse_event_type(
    event_type_str: str,
    context_id: str,
    event_id: Any,
) -> ContextEventType:
    """Parse event type string to enum."""
    try:
        return ContextEventType(event_type_str)
    except ValueError as exc:
        raise RuntimeError(
            f"Unknown event type '{event_type_str}' in context event. "
            f"context_id={context_id}, event_id={event_id}",
        ) from exc


def _resolve_message_role(
    event_type: ContextEventType,
    context_id: str,
    event_id: Any,
) -> MessageRole:
    """Resolve message role from event type."""
    role = CONTEXT_EVENT_TO_MESSAGE_ROLE.get(event_type)
    if role is None:
        raise RuntimeError(
            f"No role mapping for event type '{event_type.value}'. "
            f"context_id={context_id}, event_id={event_id}",
        )
    return role


# ---------------------------------------------------------------------------
# Full context message loading
# ---------------------------------------------------------------------------

_CONVERSATION_EVENT_TYPES: frozenset[str] = frozenset({
    ContextEventType.INPUT.value,
    ContextEventType.OUTPUT.value,
})


def load_context_messages(
    context_id: str,
    context_service: ContextServiceFacade,
    content_storage: ContentReader,
) -> list[dict[str, str]]:
    """Load conversation history from context as messages.

    Retrieves events from context storage and converts them to message
    format. Mirrors ContextStage logic without the pipeline infrastructure.

    Args:
        context_id: The context stream ID to load messages from.
        context_service: Facade for event and snapshot access.
        content_storage: Storage service for reading content files.

    Returns:
        List of message dicts with 'role' and 'content' keys.

    Raises:
        RuntimeError: If data corruption is detected.
    """
    messages: list[dict[str, str]] = []

    snapshot = context_service.snapshots.get_latest_snapshot(context_id)
    if snapshot:
        messages.append(
            load_snapshot_summary_message(snapshot, context_id, content_storage),
        )
        end_event_id = str(snapshot.get("end_event_id", ""))
        events = context_service.events.list_events_after_snapshot(
            context_id, end_event_id,
        )
    else:
        events = context_service.events.list_all_events(context_id)

    for event in events:
        event_type_str = str(event.get("event_type", ""))
        if event_type_str not in _CONVERSATION_EVENT_TYPES:
            continue
        messages.append(
            convert_event_to_message(event, context_id, content_storage),
        )

    return messages
