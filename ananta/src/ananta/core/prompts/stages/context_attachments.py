"""Attachment handling for ContextStage.

Module-level functions that query memory for recent file uploads and
format them as a human-readable summary for injection into context.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from ananta.core.prompts.context import PromptContext
    from ananta.core.services.prompt_context_builder import PromptContextBuilder
    from ananta.services.context_management.config import ContextManagementConfig


def add_attachment_summary(
    ctx: PromptContext,
    *,
    session_id: str | None,
    builder: PromptContextBuilder | None,
    config: ContextManagementConfig | None,
    stage_name: str,
) -> None:
    """Add recent attachment summary to context if available.

    Args:
        ctx: PromptContext to populate with attachment_summary
        session_id: Session identifier for memory query
        builder: PromptContextBuilder with memory service access
        config: Context management config (attachment_scan_limit)
        stage_name: Stage name for decision logging
    """
    recent_attachments = get_recent_attachments(
        session_id=session_id, builder=builder, config=config,
    )
    if not recent_attachments:
        return

    ctx.attachment_summary = (
        f"[Recent files uploaded by user]\n"
        f"{format_attachment_summary(recent_attachments)}"
    )
    ctx.add_decision(
        stage_name, f"Added {len(recent_attachments)} recent attachment(s) to context"
    )


def get_recent_attachments(
    *,
    session_id: str | None,
    builder: PromptContextBuilder | None,
    config: ContextManagementConfig | None,
) -> list[dict[str, Any]]:
    """Get recent attachments from memory events.

    Queries memory service for recent events with attachments in metadata.
    Used in platform mode to surface file uploads from previous turns.

    Args:
        session_id: Optional session to filter by
        builder: PromptContextBuilder with memory service access
        config: Context management config (attachment_scan_limit)

    Returns:
        List of attachment dicts with blob_id, filename, etc.

    Raises:
        ValueError: If config is not available (required for attachment_scan_limit)
    """
    if not config:
        raise ValueError(
            "ContextStage requires context_config for attachment scanning. "
            "Ensure context_config is passed during initialization."
        )

    max_events = config.attachment_scan_limit
    records_result = fetch_recent_memory_records(
        session_id=session_id, builder=builder, max_events=max_events,
    )
    if not records_result:
        return []

    return extract_attachments_from_records(records_result)


def fetch_recent_memory_records(
    *,
    session_id: str | None,
    builder: PromptContextBuilder | None,
    max_events: int,
) -> list[dict[str, Any]]:
    """Fetch recent memory records from memory service.

    Args:
        session_id: Optional session to filter by
        builder: PromptContextBuilder with memory service access
        max_events: Maximum number of events to scan

    Returns:
        List of memory record dicts
    """
    if not builder or not hasattr(builder, "_memory_service"):
        return []

    memory_service = builder._memory_service
    get_structured = getattr(memory_service, "get_recent_memory_structured", None)
    if not callable(get_structured):
        return []

    envelope = get_structured(session_id=session_id, max_events=max_events)
    if not isinstance(envelope, dict):
        raise TypeError(
            "memory_service.get_recent_memory_structured() must return the "
            '{"events": [...], "count": N} envelope, '
            f"got {type(envelope).__name__}"
        )

    events = envelope.get("events")
    return events if isinstance(events, list) else []


def extract_attachments_from_records(
    records: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Extract valid attachments from memory records.

    Args:
        records: List of memory record dicts with metadata

    Returns:
        List of attachment dicts that have a blob_id
    """
    attachments: list[dict[str, Any]] = []
    for record in records:
        metadata = record.get("metadata")
        if not isinstance(metadata, dict):
            continue
        record_attachments = metadata.get("attachments", [])
        if isinstance(record_attachments, list):
            attachments.extend(
                att for att in record_attachments
                if isinstance(att, dict) and att.get("blob_id")
            )
    return attachments


def format_attachment_summary(attachments: list[dict[str, Any]]) -> str:
    """Format attachment list for system message.

    Args:
        attachments: List of attachment dicts

    Returns:
        Formatted string describing recent uploads
    """
    lines = [format_single_attachment(att) for att in attachments]
    return "\n".join(lines)


def format_single_attachment(att: dict[str, Any]) -> str:
    """Format a single attachment for display.

    Args:
        att: Attachment dict with filename, content_type, size_bytes

    Returns:
        Formatted line like ``- report.pdf [application/pdf] (1.2 MB)``
    """
    # NOTE: blob_id intentionally hidden from LLM context to prevent hallucination
    filename = (
        att.get("original_name") or att.get("filename") or att.get("name") or "file"
    )
    content_type = att.get("content_type") or att.get("mime_type") or ""
    size_str = format_file_size(att.get("size_bytes"))
    type_hint = f" [{content_type}]" if content_type else ""
    return f"- {filename}{type_hint}{size_str}"


def format_file_size(size_bytes: int | None) -> str:
    """Format file size for human-readable display.

    Args:
        size_bytes: File size in bytes, or None

    Returns:
        Formatted string like ``(1.2 MB)`` or empty string
    """
    if not size_bytes:
        return ""
    if size_bytes >= 1024 * 1024:
        return f" ({size_bytes / (1024 * 1024):.1f} MB)"
    if size_bytes >= 1024:
        return f" ({size_bytes / 1024:.1f} KB)"
    return f" ({size_bytes} bytes)"
