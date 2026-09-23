"""Message formatter for LM Studio inference.

This module converts PromptContextBuilder's LLMContext into the
message list format expected by LM Studio's OpenAI-compatible API.

Single Responsibility: Format LLMContext for LM Studio.
Complexity: B (parsing and formatting, but each function is focused).
"""

import re
from typing import Any

from ananta.core.services.prompt_context_builder import LLMContext

# Role mapping from memory service event_type to OpenAI role
ROLE_MAP: dict[str, str] = {
    "User": "user",
    "Assistant": "assistant",
    "System": "system",
}


def parse_conversation_to_messages(
    conversation_text: str,
) -> list[dict[str, str]]:
    """Parse formatted conversation history to message list.

    Parses the format produced by MemoryService.get_recent_memory():
        [timestamp] Role (interface): content

    Args:
        conversation_text: Formatted conversation string from MemoryService.

    Returns:
        List of message dicts with role and content keys.
    """
    if not conversation_text or conversation_text == "No memory available.":
        return []

    messages: list[dict[str, str]] = []

    # Pattern: [timestamp] Role (interface): content
    # Example: [2025-01-15T10:00:00Z] User (console): Hello!
    pattern = re.compile(r"^\[([^\]]+)\]\s+(User|Assistant|System)\s+\([^)]+\):\s*(.*)$")

    for line in conversation_text.split("\n"):
        line = line.strip()
        if not line:
            continue

        match = pattern.match(line)
        if match:
            role_name = match.group(2)
            content = match.group(3)
            role = ROLE_MAP.get(role_name, "user")
            messages.append({"role": role, "content": content})

    return messages


def format_memories_as_context(
    memories: list[dict[str, Any]],
    *,
    prefix: str,
) -> str:
    """Format memory list as context text.

    Args:
        memories: List of memory dicts from recall().
        prefix: Header prefix for the context block.

    Returns:
        Formatted context string, or empty string if no memories.
    """
    if not memories:
        return ""

    lines: list[str] = [prefix]
    for mem in memories:
        content = mem.get("content", "")
        if content:
            lines.append(f"- {content}")

    return "\n".join(lines)


def format_messages_for_lm_studio(
    context: LLMContext,
    system_prompt: str,
    original_user_message: str,
) -> list[dict[str, str]]:
    """Convert LLMContext to LM Studio message format.

    Assembles messages in order:
    1. System prompt (main instructions)
    2. Identity context (if present)
    3. Relevant memories (if present)
    4. Recent conversation (parsed to messages)
    5. Original user message (with environment data preserved)

    Args:
        context: LLMContext from PromptContextBuilder.
        system_prompt: Main system prompt text.
        original_user_message: Original formatted user message from template.
            Must contain the full template content including environment data
            (date, time, timezone) and instructions.

    Returns:
        List of message dicts for LM Studio API.

    Raises:
        ValueError: If original_user_message is empty.
    """
    if not original_user_message:
        raise ValueError(
            "original_user_message is required and cannot be empty. "
            "The full formatted user message from the template must be preserved."
        )

    messages: list[dict[str, str]] = []

    # System prompt (main instructions)
    messages.append({"role": "system", "content": system_prompt})

    # Identity context (always present)
    identity_text = format_memories_as_context(
        context.identity_memories,
        prefix="Identity context:",
    )
    if identity_text:
        messages.append({"role": "system", "content": identity_text})

    # Relevant memories (ACT-R semantic recall)
    relevant_text = format_memories_as_context(
        context.relevant_memories,
        prefix="Relevant context:",
    )
    if relevant_text:
        messages.append({"role": "system", "content": relevant_text})

    # Recent conversation (deprecated) - parse to structured messages
    conversation_messages = parse_conversation_to_messages(context.recent_conversation)
    messages.extend(conversation_messages)

    # Original user message with environment data preserved
    messages.append({"role": "user", "content": original_user_message})

    return messages


def merge_system_messages(
    messages: list[dict[str, str]],
) -> list[dict[str, str]]:
    """Merge consecutive system messages to reduce message count.

    Some LLM providers have message count limits. This function
    consolidates consecutive system messages into a single message.

    Args:
        messages: List of message dicts.

    Returns:
        List of message dicts with consecutive system messages merged.
    """
    if not messages:
        return []

    merged: list[dict[str, str]] = []
    pending_system: list[str] = []

    for msg in messages:
        if msg["role"] == "system":
            pending_system.append(msg["content"])
        else:
            # Flush pending system messages
            if pending_system:
                merged.append(
                    {
                        "role": "system",
                        "content": "\n\n".join(pending_system),
                    }
                )
                pending_system = []
            merged.append(msg)

    # Flush any remaining system messages
    if pending_system:
        merged.append(
            {
                "role": "system",
                "content": "\n\n".join(pending_system),
            }
        )

    return merged
