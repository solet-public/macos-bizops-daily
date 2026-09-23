"""Prompt context builder for memory-centric LLM interactions.

This module provides context construction for LLM prompts:
- Relevant memories: Semantically matched to user query via ACT-R
- Identity memories: Core identity/personality (always present)

The context builder integrates with the memory service to provide
contextually relevant memories for LLM decision-making.

Single Responsibility: Build structured context for LLM prompts.
Complexity: B (multiple memory types, but each is independent).
"""

from dataclasses import dataclass, field
from typing import Any, Protocol

# ─────────────────────────────────────────────────────────────────────────────
# Constants
# ─────────────────────────────────────────────────────────────────────────────

# Default limits for memory retrieval
DEFAULT_RECENT_EVENTS: int = 12
DEFAULT_RELEVANT_TOP_K: int = 5
DEFAULT_IDENTITY_TOP_K: int = 1  # We are only ever one person

# Identity query for identity memory retrieval
IDENTITY_QUERY: str = "who am I my identity my name my preferences"

# Tag constants (internal, fixed vocabulary - not user-extensible)
# Tool use tags (must match ToolUseTag values from tool_use_types.py)
TAG_TOOL_USE: str = "tool_use"
TAG_TOOL_SUCCESS: str = "tool_success"
# Identity tag for memory filtering
IDENTITY_TAG: str = "identity"
# Additional tags to suppress from semantic recall context
TAG_CONVERSATION: str = "conversation"
TAG_CONSOLIDATED: str = "consolidated"
# Knowledge base content tag — excluded from personal memory recall.
# Knowledge base articles are accessed exclusively via knowledge_service::search.
TAG_KNOWLEDGE_OFFICIAL: str = "knowledge:official"

_EXCLUDED_RECALL_TAGS: frozenset[str] = frozenset({
    TAG_TOOL_USE, IDENTITY_TAG, TAG_CONVERSATION, TAG_CONSOLIDATED,
    TAG_KNOWLEDGE_OFFICIAL,
})


def _has_excluded_tag(mem: dict[str, Any]) -> bool:
    """Return True if the memory carries any tag that should be excluded from recall context."""
    tags = mem.get("tags") or []
    return bool(_EXCLUDED_RECALL_TAGS.intersection(tags))


# ─────────────────────────────────────────────────────────────────────────────
# Protocol for Memory Service
# ─────────────────────────────────────────────────────────────────────────────


class MemoryServiceProtocol(Protocol):
    """Protocol for memory service used by context builder.

    Uses structural typing to decouple from concrete implementation.
    All methods must match the memory_service public interface.
    """

    def get_recent_memory(
        self,
        session_id: str | None = None,
        max_events: int = 20,
        max_age_hours: int | None = None,
        namespace_filter: str | None = None,
    ) -> dict[str, Any]:
        """Retrieve recent interaction history.

        Memories are global - session_id is an optional filter.
        """
        ...

    def recall(
        self,
        query: str,
        top_k: int = 5,
        memory_type: str = "all",
        include_archived: bool = False,
        tags: list[str] | None = None,
        exclude_ids: list[str] | None = None,
    ) -> dict[str, Any]:
        """Search memories with strength-weighted ranking."""
        ...

    def reinforce(self, memory_id: str) -> dict[str, Any]:
        """Reinforce a memory by adding a retrieval timestamp."""
        ...


# ─────────────────────────────────────────────────────────────────────────────
# Data Classes
# ─────────────────────────────────────────────────────────────────────────────


@dataclass(slots=True)
class LLMContext:
    """Structured context for LLM prompts.

    This dataclass holds context assembled from memory:
    - relevant_memories: Semantically matched to user query
    - identity_memories: Core identity/personality (always included)

    Attributes:
        session_id: Session identifier (optional)
        recent_conversation: Formatted recent conversation history (deprecated, unused)
        recent_event_count: Number of events in recent history (deprecated, unused)
        relevant_memories: List of semantically relevant memories
        identity_memories: List of identity-related memories
        excluded_memory_ids: IDs already included to prevent duplicates
    """

    session_id: str | None
    recent_conversation: str
    recent_event_count: int
    relevant_memories: list[dict[str, Any]] = field(default_factory=list)
    identity_memories: list[dict[str, Any]] = field(default_factory=list)
    excluded_memory_ids: set[str] = field(default_factory=set)

    def get_all_memory_ids(self) -> set[str]:
        """Get all memory IDs included in this context.

        Returns:
            Set of memory IDs from relevant and identity memories.
        """
        ids: set[str] = set()
        for mem in self.relevant_memories:
            if mem_id := mem.get("id"):
                ids.add(str(mem_id))
        for mem in self.identity_memories:
            if mem_id := mem.get("id"):
                ids.add(str(mem_id))
        return ids


# ─────────────────────────────────────────────────────────────────────────────
# Context Builder
# ─────────────────────────────────────────────────────────────────────────────


class PromptContextBuilder:
    """Build structured context for LLM prompts.

    This service assembles context from the memory service:
    - Relevant memories: Semantic search based on user query
    - Identity memories: Always present for personalization

    The builder also reinforces memories that are surfaced, strengthening
    them for future recall (retrieval practice effect).
    """

    def __init__(self, memory_service: MemoryServiceProtocol) -> None:
        """Initialize the context builder.

        Args:
            memory_service: Memory service instance for querying memories.
        """
        self._memory_service = memory_service

    def build_context(
        self,
        session_id: str | None = None,
        user_input: str | None = None,
        *,
        max_recent_events: int = DEFAULT_RECENT_EVENTS,
        relevant_top_k: int = DEFAULT_RELEVANT_TOP_K,
        identity_top_k: int = DEFAULT_IDENTITY_TOP_K,
        reinforce_memories: bool = True,
    ) -> LLMContext:
        """Build structured context for LLM prompts.

        Args:
            session_id: Session identifier (optional, for future use).
            user_input: Current user input for semantic relevance matching.
                If None or empty, relevant memory recall is skipped.
            max_recent_events: Maximum recent events (deprecated, unused).
            relevant_top_k: Maximum relevant memories to retrieve.
            identity_top_k: Maximum identity memories to retrieve.
            reinforce_memories: Whether to reinforce surfaced memories.

        Returns:
            LLMContext with memories populated.
        """
        excluded_ids: set[str] = set()

        # Build context from memory
        recent_conversation, recent_event_count = self._get_recent_conversation(
            session_id, max_recent_events
        )
        relevant_memories = self._get_relevant_memories(user_input, relevant_top_k, excluded_ids)
        identity_memories = self._get_identity_memories(identity_top_k, excluded_ids)

        if reinforce_memories:
            self._reinforce_surfaced_memories(relevant_memories, identity_memories)

        return LLMContext(
            session_id=session_id,
            recent_conversation=str(recent_conversation),
            recent_event_count=int(recent_event_count),
            relevant_memories=relevant_memories,
            identity_memories=identity_memories,
            excluded_memory_ids=excluded_ids,
        )

    def _get_recent_conversation(
        self, session_id: str | None, max_recent_events: int
    ) -> tuple[str, int]:
        """Get recent conversation history (legacy, typically disabled via max_recent_events=0)."""
        if not session_id or max_recent_events <= 0:
            return "", 0

        recent_result = self._memory_service.get_recent_memory(
            session_id=session_id,
            max_events=max_recent_events,
        )
        return recent_result.get("history", ""), recent_result.get("event_count", 0)

    def _get_relevant_memories(
        self, user_input: str | None, relevant_top_k: int, excluded_ids: set[str]
    ) -> list[dict[str, Any]]:
        """Get semantically relevant memories via ACT-R recall."""
        if not user_input:
            return []

        relevant_result = self._memory_service.recall(query=user_input, top_k=relevant_top_k)
        raw_memories: list[dict[str, Any]] = relevant_result.get("memories", [])

        # Filter out identity, conversation, consolidated, and knowledge base memories.
        # Knowledge base content is accessed exclusively via knowledge_service::search;
        # the memory service auto-excludes it, but we filter here as defense-in-depth.
        relevant_memories = [m for m in raw_memories if not _has_excluded_tag(m)]

        # Collect IDs to prevent duplicates
        for mem in relevant_memories:
            if mem_id := mem.get("id"):
                excluded_ids.add(str(mem_id))

        return relevant_memories

    def _get_identity_memories(
        self, identity_top_k: int, excluded_ids: set[str]
    ) -> list[dict[str, Any]]:
        """Get identity/personality memories."""
        identity_result = self._memory_service.recall(
            query=IDENTITY_QUERY,
            top_k=identity_top_k,
            tags=[IDENTITY_TAG],
            exclude_ids=list(excluded_ids) if excluded_ids else None,
        )
        identity_memories: list[dict[str, Any]] = identity_result.get("memories", [])

        # Collect IDs from identity memories
        for mem in identity_memories:
            if mem_id := mem.get("id"):
                excluded_ids.add(str(mem_id))

        return identity_memories

    def _reinforce_surfaced_memories(
        self,
        relevant_memories: list[dict[str, Any]],
        identity_memories: list[dict[str, Any]],
    ) -> None:
        """Reinforce memories that were surfaced in context.

        This implements the retrieval practice effect: memories that are
        accessed become stronger and more likely to surface again.

        Args:
            relevant_memories: Relevant memories to reinforce.
            identity_memories: Identity memories to reinforce.
        """
        for mem in relevant_memories + identity_memories:
            if mem_id := mem.get("id"):
                self._memory_service.reinforce(str(mem_id))
