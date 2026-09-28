"""Context management for conversational inference.

This package provides:
- Message formatting: Convert LLMContext to LM Studio format

These are plugin-specific components that do NOT belong in platform services.
The platform provides PromptContextBuilder; this package provides the
LM Studio-specific formatting layer.

Note: Context window management is handled via ContextManagementService.
Cache warming is handled via the plugin's warm_cache(WarmingRequest) interface method,
which uses generate_text_completion with config values from the platform.
"""

from .message_formatter import (
    format_memories_as_context,
    format_messages_for_lm_studio,
    merge_system_messages,
    parse_conversation_to_messages,
)

__all__ = [
    # Message formatting
    "format_messages_for_lm_studio",
    "format_memories_as_context",
    "merge_system_messages",
    "parse_conversation_to_messages",
]
