"""Focused history deduplication — remove conversation events that duplicate focused memories.

When an EDGE_SINK action stores content as focused memory AND the result is
also persisted as a conversation event, the same document appears twice in
the prompt.  This module removes the history copy (which has a metadata
trailer) when a focused memory copy exists.

Pure functions — no service dependencies.
"""

from __future__ import annotations

from typing import Any

from ananta.core.prompts.context import ACTIVE_PLAN_MARKER


def focused_content_prefixes(
    focused_memories: list[dict[str, Any]],
) -> set[str]:
    """Extract content prefixes from focused memories for deduplication."""
    prefixes: set[str] = set()
    for mem in focused_memories:
        content = mem.get("content", "")
        if isinstance(content, str) and len(content) > 50:
            prefixes.add(content[:100])
    return prefixes


def is_focused_duplicate(
    msg: dict[str, Any], prefixes: set[str],
) -> bool:
    """Check if a message is a conversation history duplicate of a focused memory.

    Only removes messages that have a metadata trailer (conversation
    history events).  Focused memory renderings (no trailer) are kept.
    """
    if msg.get("role") == "system":
        return False
    content = msg.get("content", "")
    if ACTIVE_PLAN_MARKER in content:
        return False
    # Only remove if the message has a metadata trailer (history event)
    if not content.rstrip().endswith("}"):
        return False
    return any(content.startswith(p) for p in prefixes)


def deduplicate_focused_history(
    conversation_history: list[dict[str, str]],
    focused_memories: list[dict[str, Any]],
) -> tuple[list[dict[str, str]], int]:
    """Remove conversation history entries that duplicate focused memories.

    Returns the filtered history and the number of removed entries.
    """
    prefixes = focused_content_prefixes(focused_memories)
    if not prefixes:
        return conversation_history, 0
    filtered = [
        m for m in conversation_history
        if not is_focused_duplicate(m, prefixes)
    ]
    removed = len(conversation_history) - len(filtered)
    return filtered, removed
