"""Memory Service Public API.

AI-discoverable memory operations with @service_interface_process decorators.

Discoverability Policy (Task #47, 2026-05-24):
- The base decorator default for ``@service_interface_process`` is
  ``is_discoverable=False``. This file declares ``is_discoverable=True`` or
  ``is_discoverable=False`` EXPLICITLY on every method so the policy is visible
  at the call site (rather than relying on the silent default).
- DISCOVERABLE (True): all long-term ACT-R memory operations (remember, recall,
  forget, reinforce, memorize, learn, ingest_session), tag operations
  (delete_memories_by_tag, delete_memories_by_ids, get_memories_by_tag,
  upsert_memory_by_tag, memorize_by_tag), memorization-queue management (stop_memorizing,
  list_memorizing, process_memorization_queue), maintenance (consolidate,
  recompute_strengths, memory_stats, list_memories, export_memories,
  import_memories, cleanup_orphaned_vectors, reindex_orphaned_vectors,
  purge_memories), audit (audit_pinned_notes, review_blocked_intents), and
  the focus buffer (focus, unfocus, get_focused). The user / operator / agent
  calls these directly.
- INTERNAL (False): the four short-term session-memory helpers
  (get_recent_memory, get_session_event_stats, store_interaction,
  get_recent_memory_structured) and store_compaction_summary. These are
  called by the orchestrator / IO plugins / context-management code, not by
  the agent in a ReAct loop. Surfacing them in ``process_search`` would only
  add noise and let the model misuse them.

This service provides two memory systems:

1. SHORT-TERM SESSION MEMORY (Phase 1)
   - store_interaction() / get_recent_memory()
   - Conversation history within a session
   - SQL-based storage

2. LONG-TERM ACT-R MEMORY (Phase 2)
   - remember() / recall() / memorize() / learn()
   - Knowledge with decay and spaced repetition
   - Vector-based semantic search
   - Based on ACT-R cognitive architecture
"""

from abc import ABC, abstractmethod
from typing import Any

from ananta.core.actions.action_metadata import (
    MergeErrorProcessorCustomizations,
    ParameterMetadata,
    ParameterType,
    ReturnValueSchema,
)
from ananta.core.domain.enums import ProcessorPolicyCategory
from ananta.core.services.service_interface_decorator import service_interface_process

# ─────────────────────────────────────────────────────────────────────────────
# ACT-R Memory Type Definitions
# ─────────────────────────────────────────────────────────────────────────────

# Memory types
MEMORY_TYPE_EPISODIC = "episodic"
MEMORY_TYPE_SEMANTIC_L1 = "semantic_l1"
MEMORY_TYPE_SEMANTIC_L2 = "semantic_l2"

# Memory statuses
MEMORY_STATUS_ACTIVE = "active"
MEMORY_STATUS_ARCHIVED = "archived"

# Memorization statuses
MEMORIZATION_STATUS_ACTIVE = "active"
MEMORIZATION_STATUS_PAUSED = "paused"
MEMORIZATION_STATUS_COMPLETED = "completed"


class MemoryServiceAPI(ABC):
    """Public memory operations - AI-discoverable via process registry.

    This interface defines memory operations that can be discovered and
    invoked by the AI orchestration system and action templates.
    """

    @service_interface_process(
        name="get_recent_memory",
        is_discoverable=False,
        provider="memory_service",
        parameters={
            "session_id": ParameterMetadata(
                description="Optional session filter (memories are global by default)",
                required=False,
                type=ParameterType.STRING,
                default=None,
            ),
            "max_events": ParameterMetadata(
                description="Maximum number of events to retrieve (SQL LIMIT)",
                required=False,
                type=ParameterType.INTEGER,
                default=20,
            ),
            "max_age_hours": ParameterMetadata(
                description="Only retrieve events from the last N hours (SQL WHERE filter)",
                required=False,
                type=ParameterType.INTEGER,
                default=None,
            ),
            "namespace_filter": ParameterMetadata(
                description="Only retrieve events from specific plugin namespace",
                required=False,
                type=ParameterType.STRING,
                default=None,
            ),
        },
        return_value_schema=ReturnValueSchema(
            description="Recent memory with formatted history",
            type=ParameterType.OBJECT,
            properties={
                "history": ParameterMetadata(
                    type=ParameterType.STRING, description="Formatted conversation history"
                ),
                "event_count": ParameterMetadata(
                    type=ParameterType.INTEGER, description="Number of events in history"
                ),
            },
        ),
        error_processor_customizations=MergeErrorProcessorCustomizations(
        )
    )
    @abstractmethod
    def get_recent_memory(
        self,
        session_id: str | None = None,
        max_events: int = 20,
        max_age_hours: int | None = None,
        namespace_filter: str | None = None,
    ) -> dict[str, Any]:
        """Retrieve recent memory formatted for LLM context.

        Memories are global - session_id is an optional filter.
        """
        pass

    @service_interface_process(
        name="get_session_event_stats",
        is_discoverable=False,
        provider="memory_service",
        parameters={
            "session_id": ParameterMetadata(
                description="Session identifier to get stats for",
                required=True,
                type=ParameterType.STRING,
            ),
        },
        return_value_schema=ReturnValueSchema(
            description="Session event statistics",
            type=ParameterType.OBJECT,
            properties={
                "total_events": ParameterMetadata(
                    type=ParameterType.INTEGER,
                    description="Total number of conversation events in session",
                ),
                "by_namespace": ParameterMetadata(
                    type=ParameterType.OBJECT,
                    description="Event counts broken down by source namespace",
                ),
                "oldest_event": ParameterMetadata(
                    type=ParameterType.STRING, description="Timestamp of oldest event"
                ),
                "newest_event": ParameterMetadata(
                    type=ParameterType.STRING, description="Timestamp of newest event"
                ),
            },
        ),
        error_processor_customizations=MergeErrorProcessorCustomizations()
    )
    @abstractmethod
    def get_session_event_stats(self, session_id: str) -> dict[str, Any]:
        """Get conversation event statistics for a session (not long-term memories)."""
        pass

    @service_interface_process(
        name="store_interaction",
        is_discoverable=False,
        provider="memory_service",
        parameters={
            "session_id": ParameterMetadata(
                description="Session identifier for grouping events",
                required=True,
                type=ParameterType.STRING,
            ),
            "source_namespace": ParameterMetadata(
                description="Plugin namespace that generated this event (e.g., 'console_plugin')",
                required=True,
                type=ParameterType.STRING,
            ),
            "event_type": ParameterMetadata(
                description="Type of event: 'user_input' or 'system_output'",
                required=True,
                type=ParameterType.STRING,
            ),
            "content": ParameterMetadata(
                description="The actual content of the interaction",
                required=True,
                type=ParameterType.STRING,
            ),
            "metadata": ParameterMetadata(
                description="Optional metadata about the event",
                required=False,
                type=ParameterType.OBJECT,
                default=None,
            ),
            "timestamp": ParameterMetadata(
                description="Optional ISO timestamp (defaults to current time)",
                required=False,
                type=ParameterType.STRING,
                default=None,
            ),
        },
        return_value_schema=ReturnValueSchema(
            description="Result with event_id",
            type=ParameterType.OBJECT,
            properties={
                "action_status": ParameterMetadata(
                    type=ParameterType.STRING, description="Operation status"
                ),
                "data": ParameterMetadata(
                    type=ParameterType.OBJECT, description="Contains event_id"
                ),
            },
        ),
        error_processor_customizations=MergeErrorProcessorCustomizations()
    )
    @abstractmethod
    def store_interaction(
        self,
        session_id: str,
        source_namespace: str,
        event_type: str,
        content: str,
        metadata: dict[str, Any] | None = None,
        timestamp: str | None = None,
    ) -> dict[str, Any]:
        """Store an interaction event in short-term session memory."""
        pass

    @service_interface_process(
        name="get_recent_memory_structured",
        is_discoverable=False,
        provider="memory_service",
        parameters={
            "session_id": ParameterMetadata(
                description="Optional session filter (memories are global by default)",
                required=False,
                type=ParameterType.STRING,
                default=None,
            ),
            "max_events": ParameterMetadata(
                description="Maximum number of events to retrieve",
                required=False,
                type=ParameterType.INTEGER,
                default=20,
            ),
            "max_age_hours": ParameterMetadata(
                description="Only retrieve events from the last N hours",
                required=False,
                type=ParameterType.INTEGER,
                default=None,
            ),
            "namespace_filter": ParameterMetadata(
                description="Only retrieve events from specific plugin namespace",
                required=False,
                type=ParameterType.STRING,
                default=None,
            ),
        },
        return_value_schema=ReturnValueSchema(
            description="Recent interaction events as structured records",
            type=ParameterType.OBJECT,
            properties={
                "events": ParameterMetadata(
                    type=ParameterType.LIST,
                    description=(
                        "Event records with session_id, event_type, content, "
                        "and ISO timestamp"
                    ),
                ),
                "count": ParameterMetadata(
                    type=ParameterType.INTEGER, description="Number of events returned"
                ),
            },
        ),
        error_processor_customizations=MergeErrorProcessorCustomizations(
        )
    )
    @abstractmethod
    def get_recent_memory_structured(
        self,
        session_id: str | None = None,
        max_events: int = 20,
        max_age_hours: int | None = None,
        namespace_filter: str | None = None,
    ) -> dict[str, Any]:
        """Retrieve recent memory as {"events": [...], "count": N}."""
        pass

    # ─────────────────────────────────────────────────────────────────────────
    # LONG-TERM ACT-R MEMORY (Phase 2)
    # ─────────────────────────────────────────────────────────────────────────

    @service_interface_process(
        name="remember",
        is_discoverable=True,
        provider="memory_service",
        parameters={
            "content": ParameterMetadata(
                description="The content to remember", required=True, type=ParameterType.STRING
            ),
            "tags": ParameterMetadata(
                description="Optional tags for organization",
                required=False,
                type=ParameterType.LIST,
                default=[],
            ),
            "source_file": ParameterMetadata(
                description="Source file path if from document ingestion",
                required=False,
                type=ParameterType.STRING,
                default=None,
            ),
            "session_id": ParameterMetadata(
                description="Session identifier for grouping",
                required=False,
                type=ParameterType.STRING,
                default=None,
            ),
            "embed": ParameterMetadata(
                description=(
                    "Whether to generate a vector embedding. "
                    "Set to false for focus-only working-context mirrors."
                ),
                required=False,
                type=ParameterType.BOOLEAN,
                default=True,
            ),
        },
        return_value_schema=ReturnValueSchema(
            description="Result with memory_id",
            type=ParameterType.OBJECT,
            properties={
                "memory_id": ParameterMetadata(
                    type=ParameterType.STRING, description="UUID of the created memory"
                ),
                "message": ParameterMetadata(
                    type=ParameterType.STRING, description="Confirmation message"
                ),
            },
        ),
        error_processor_customizations=MergeErrorProcessorCustomizations()
    )
    @abstractmethod
    def remember(
        self,
        content: str,
        tags: list[str] | None = None,
        source_file: str | None = None,
        session_id: str | None = None,
        embed: bool = True,
    ) -> dict[str, Any]:
        """Store a new episodic memory."""
        pass

    @service_interface_process(
        name="recall",
        is_discoverable=True,
        provider="memory_service",
        parameters={
            "query": ParameterMetadata(
                description="Natural language search query",
                required=True,
                type=ParameterType.STRING,
            ),
            "top_k": ParameterMetadata(
                description="Number of results to return",
                required=False,
                type=ParameterType.INTEGER,
                default=5,
            ),
            "memory_type": ParameterMetadata(
                description="Filter by type: 'episodic', 'semantic_l1', 'semantic_l2', or 'all'",
                required=False,
                type=ParameterType.STRING,
                default="all",
            ),
            "include_archived": ParameterMetadata(
                description="Include archived memories in search",
                required=False,
                type=ParameterType.BOOLEAN,
                default=False,
            ),
            "tags": ParameterMetadata(
                description="Filter by tags - only return memories that have ALL specified tags (e.g., ['tool_use', 'tool_success'])",
                required=False,
                type=ParameterType.LIST,
                default=None,
            ),
            "exclude_ids": ParameterMetadata(
                description="List of memory IDs to exclude from results (useful for avoiding duplicates)",
                required=False,
                type=ParameterType.LIST,
                default=None,
            ),
        },
        return_value_schema=ReturnValueSchema(
            description="List of matching memories with scores",
            type=ParameterType.OBJECT,
            properties={
                "memories": ParameterMetadata(
                    type=ParameterType.LIST,
                    description="List of memory records with content, strength, and scores",
                ),
                "count": ParameterMetadata(
                    type=ParameterType.INTEGER, description="Number of memories returned"
                ),
            },
        ),
        error_processor_customizations=MergeErrorProcessorCustomizations()
    )
    @abstractmethod
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
        pass

    @service_interface_process(
        name="forget",
        is_discoverable=True,
        provider="memory_service",
        parameters={
            "memory_id": ParameterMetadata(
                description="ID of memory to archive", required=True, type=ParameterType.STRING
            ),
        },
        return_value_schema=ReturnValueSchema(
            description="Confirmation of archival",
            type=ParameterType.OBJECT,
            properties={
                "message": ParameterMetadata(
                    type=ParameterType.STRING, description="Confirmation message"
                ),
            },
        ),
        error_processor_customizations=MergeErrorProcessorCustomizations()
    )
    @abstractmethod
    def forget(self, memory_id: str) -> dict[str, Any]:
        """Archive a memory (soft delete)."""
        pass

    @service_interface_process(
        name="reinforce",
        is_discoverable=True,
        provider="memory_service",
        parameters={
            "memory_id": ParameterMetadata(
                description="ID of the memory to reinforce",
                required=True,
                type=ParameterType.STRING,
            ),
        },
        return_value_schema=ReturnValueSchema(
            description="Reinforcement confirmation with updated strength",
            type=ParameterType.OBJECT,
            properties={
                "memory_id": ParameterMetadata(
                    type=ParameterType.STRING, description="ID of the reinforced memory"
                ),
                "new_strength": ParameterMetadata(
                    type=ParameterType.FLOAT, description="Updated ACT-R activation strength"
                ),
                "retrieval_count": ParameterMetadata(
                    type=ParameterType.INTEGER, description="Total number of retrievals"
                ),
            },
        ),
        error_processor_customizations=MergeErrorProcessorCustomizations()
    )
    @abstractmethod
    def reinforce(self, memory_id: str) -> dict[str, Any]:
        """Reinforce a memory by adding a retrieval timestamp."""
        pass

    @service_interface_process(
        name="memorize",
        is_discoverable=True,
        provider="memory_service",
        parameters={
            "memory_id": ParameterMetadata(
                description="ID of existing memory to memorize",
                required=False,
                type=ParameterType.STRING,
                default=None,
            ),
            "content": ParameterMetadata(
                description="New content to remember and memorize",
                required=False,
                type=ParameterType.STRING,
                default=None,
            ),
        },
        return_value_schema=ReturnValueSchema(
            description="Memorization confirmation with next review date",
            type=ParameterType.OBJECT,
            properties={
                "memory_id": ParameterMetadata(
                    type=ParameterType.STRING, description="ID of the memory being memorized"
                ),
                "next_review_at": ParameterMetadata(
                    type=ParameterType.STRING, description="ISO timestamp of next scheduled review"
                ),
                "message": ParameterMetadata(
                    type=ParameterType.STRING, description="Confirmation message"
                ),
            },
        ),
        error_processor_customizations=MergeErrorProcessorCustomizations()
    )
    @abstractmethod
    def memorize(self, memory_id: str | None = None, content: str | None = None) -> dict[str, Any]:
        """Add a memory to the memorization queue."""
        pass

    @service_interface_process(
        name="learn",
        is_discoverable=True,
        provider="memory_service",
        parameters={
            "path": ParameterMetadata(
                description="File or directory path to ingest",
                required=True,
                type=ParameterType.STRING,
            ),
            "pattern": ParameterMetadata(
                description="Glob pattern for directories (default: *.md)",
                required=False,
                type=ParameterType.STRING,
                default="*.md",
            ),
            "recursive": ParameterMetadata(
                description="Search directories recursively",
                required=False,
                type=ParameterType.BOOLEAN,
                default=True,
            ),
            "memorize": ParameterMetadata(
                description="Add all ingested memories to memorization queue",
                required=False,
                type=ParameterType.BOOLEAN,
                default=False,
            ),
            "tags": ParameterMetadata(
                description="Tags to apply to all memories",
                required=False,
                type=ParameterType.LIST,
                default=[],
            ),
        },
        return_value_schema=ReturnValueSchema(
            description="Ingestion results",
            type=ParameterType.OBJECT,
            properties={
                "path": ParameterMetadata(
                    type=ParameterType.STRING, description="Path that was ingested"
                ),
                "memories_created": ParameterMetadata(
                    type=ParameterType.INTEGER, description="Number of memories created"
                ),
                "memorized": ParameterMetadata(
                    type=ParameterType.INTEGER,
                    description="Number of memories added to memorization queue",
                ),
                "message": ParameterMetadata(
                    type=ParameterType.STRING, description="Summary message"
                ),
            },
        ),
        error_processor_customizations=MergeErrorProcessorCustomizations()
    )
    @abstractmethod
    def learn(
        self,
        path: str,
        pattern: str = "*.md",
        recursive: bool = True,
        memorize: bool = False,
        tags: list[str] | None = None,
    ) -> dict[str, Any]:
        """Ingest knowledge from files, optionally memorizing it all."""
        pass

    @service_interface_process(
        name="ingest_session",
        is_discoverable=True,
        provider="memory_service",
        parameters={
            "transcript": ParameterMetadata(
                description="Conversation transcript text", required=True, type=ParameterType.STRING
            ),
            "session_id": ParameterMetadata(
                description="Session identifier for grouping",
                required=False,
                type=ParameterType.STRING,
                default=None,
            ),
            "chunk_by_turns": ParameterMetadata(
                description="Chunk by conversation turns vs fixed size",
                required=False,
                type=ParameterType.BOOLEAN,
                default=True,
            ),
        },
        return_value_schema=ReturnValueSchema(
            description="Ingestion results",
            type=ParameterType.OBJECT,
            properties={
                "transcript_session_id": ParameterMetadata(
                    type=ParameterType.STRING,
                    description="Session ID used for grouping the ingested memories (NOT the IO routing session)",
                ),
                "memories_created": ParameterMetadata(
                    type=ParameterType.INTEGER, description="Number of memories created"
                ),
                "memory_ids": ParameterMetadata(
                    type=ParameterType.LIST, description="List of created memory IDs"
                ),
            },
        ),
        error_processor_customizations=MergeErrorProcessorCustomizations()
    )
    @abstractmethod
    def ingest_session(
        self, transcript: str, session_id: str | None = None, chunk_by_turns: bool = True
    ) -> dict[str, Any]:
        """Ingest a conversation transcript as episodic memories."""
        pass

    # ─────────────────────────────────────────────────────────────────────────
    # TAG OPERATIONS (Knowledge Base Lifecycle)
    # ─────────────────────────────────────────────────────────────────────────

    @service_interface_process(
        name="delete_memories_by_tag",
        is_discoverable=True,
        provider="memory_service",
        parameters={
            "tag": ParameterMetadata(
                description="Tag to match for deletion (e.g., 'knowledge_base:my_project')",
                required=True,
                type=ParameterType.STRING,
            ),
        },
        return_value_schema=ReturnValueSchema(
            description="Deletion results",
            type=ParameterType.OBJECT,
            properties={
                "action_status": ParameterMetadata(
                    type=ParameterType.STRING, description="Operation status"
                ),
                "data": ParameterMetadata(
                    type=ParameterType.OBJECT,
                    description="Contains deleted_count",
                ),
            },
        ),
        error_processor_customizations=MergeErrorProcessorCustomizations()
    )
    @abstractmethod
    def delete_memories_by_tag(self, tag: str) -> dict[str, Any]:
        """Delete all memories with a specific tag."""
        pass

    @service_interface_process(
        name="delete_memories_by_ids",
        is_discoverable=True,
        provider="memory_service",
        parameters={
            "ids": ParameterMetadata(
                description="Memory ids to hard-delete, with their embeddings",
                required=True,
                type=ParameterType.LIST,
            ),
        },
        return_value_schema=ReturnValueSchema(
            description="Deletion results",
            type=ParameterType.OBJECT,
            properties={
                "action_status": ParameterMetadata(
                    type=ParameterType.STRING, description="Operation status"
                ),
                "data": ParameterMetadata(
                    type=ParameterType.OBJECT,
                    description="Contains deleted_count",
                ),
            },
        ),
        error_processor_customizations=MergeErrorProcessorCustomizations()
    )
    @abstractmethod
    def delete_memories_by_ids(self, ids: list[str]) -> dict[str, Any]:
        """Hard-delete specific memories by id, with their embeddings."""
        pass

    @service_interface_process(
        name="get_memories_by_tag",
        is_discoverable=True,
        provider="memory_service",
        parameters={
            "tag": ParameterMetadata(
                description="Tag to match (e.g., 'knowledge_base:my_project')",
                required=True,
                type=ParameterType.STRING,
            ),
            "include_archived": ParameterMetadata(
                description="Include archived memories",
                required=False,
                type=ParameterType.BOOLEAN,
                default=False,
            ),
        },
        return_value_schema=ReturnValueSchema(
            description="Matching memories",
            type=ParameterType.OBJECT,
            properties={
                "action_status": ParameterMetadata(
                    type=ParameterType.STRING, description="Operation status"
                ),
                "data": ParameterMetadata(
                    type=ParameterType.OBJECT,
                    description="Contains memories list and count",
                ),
            },
        ),
        error_processor_customizations=MergeErrorProcessorCustomizations(
        )
    )
    @abstractmethod
    def get_memories_by_tag(
        self,
        tag: str,
        include_archived: bool = False,
    ) -> dict[str, Any]:
        """List all memories with a specific tag."""
        pass

    @service_interface_process(
        name="upsert_memory_by_tag",
        is_discoverable=True,
        provider="memory_service",
        parameters={
            "content": ParameterMetadata(
                description="The content to store as the canonical memory for this tag",
                required=True,
                type=ParameterType.STRING,
            ),
            "tag": ParameterMetadata(
                description="Tag identifying the memory slot (e.g., 'heartbeat:global') — stays the replace key",
                required=True,
                type=ParameterType.STRING,
            ),
            "tags": ParameterMetadata(
                description="Optional provenance/umbrella tags unioned onto the stored record (the slot tag is always included first); the record carries all of them so umbrella/origin tags are literally present",
                required=False,
                type=ParameterType.LIST,
                default=None,
            ),
            "session_id": ParameterMetadata(
                description="Session identifier for grouping",
                required=False,
                type=ParameterType.STRING,
                default=None,
            ),
        },
        return_value_schema=ReturnValueSchema(
            description="Upsert result with deleted count and new memory ID",
            type=ParameterType.OBJECT,
            properties={
                "action_status": ParameterMetadata(
                    type=ParameterType.STRING, description="Operation status"
                ),
                "data": ParameterMetadata(
                    type=ParameterType.OBJECT,
                    description="Contains deleted_count, duplicates_reconciled, memory_id, tag",
                ),
            },
        ),
        error_processor_customizations=MergeErrorProcessorCustomizations()
    )
    @abstractmethod
    def upsert_memory_by_tag(
        self,
        content: str,
        tag: str,
        tags: list[str] | None = None,
        session_id: str | None = None,
    ) -> dict[str, Any]:
        """Replace-by-tag: store new content, then delete previous memories with this tag."""
        pass

    @service_interface_process(
        name="memorize_by_tag",
        is_discoverable=True,
        provider="memory_service",
        parameters={
            "tag": ParameterMetadata(
                description="Tag to match", required=True, type=ParameterType.STRING
            ),
        },
        return_value_schema=ReturnValueSchema(
            description="Batch memorization results",
            type=ParameterType.OBJECT,
            properties={
                "tag": ParameterMetadata(
                    type=ParameterType.STRING, description="Tag that was matched"
                ),
                "matching_memories": ParameterMetadata(
                    type=ParameterType.INTEGER, description="Number of memories with this tag"
                ),
                "newly_memorizing": ParameterMetadata(
                    type=ParameterType.INTEGER, description="Number newly added to queue"
                ),
                "already_memorizing": ParameterMetadata(
                    type=ParameterType.INTEGER, description="Number already in queue"
                ),
            },
        ),
        error_processor_customizations=MergeErrorProcessorCustomizations()
    )
    @abstractmethod
    def memorize_by_tag(self, tag: str) -> dict[str, Any]:
        """Add all memories with a specific tag to memorization queue."""
        pass

    @service_interface_process(
        name="stop_memorizing",
        is_discoverable=True,
        provider="memory_service",
        parameters={
            "memory_id": ParameterMetadata(
                description="ID of memory to stop memorizing",
                required=True,
                type=ParameterType.STRING,
            ),
        },
        return_value_schema=ReturnValueSchema(
            description="Confirmation",
            type=ParameterType.OBJECT,
            properties={
                "message": ParameterMetadata(
                    type=ParameterType.STRING, description="Confirmation message"
                ),
            },
        ),
        error_processor_customizations=MergeErrorProcessorCustomizations()
    )
    @abstractmethod
    def stop_memorizing(self, memory_id: str) -> dict[str, Any]:
        """Remove a memory from memorization queue."""
        pass

    @service_interface_process(
        name="list_memorizing",
        is_discoverable=True,
        provider="memory_service",
        parameters={
            "include_completed": ParameterMetadata(
                description="Include completed memorizations",
                required=False,
                type=ParameterType.BOOLEAN,
                default=False,
            ),
        },
        return_value_schema=ReturnValueSchema(
            description="List of memorization records",
            type=ParameterType.OBJECT,
            properties={
                "total": ParameterMetadata(
                    type=ParameterType.INTEGER, description="Total in queue"
                ),
                "due_now": ParameterMetadata(
                    type=ParameterType.INTEGER, description="Number due for review"
                ),
                "memorizations": ParameterMetadata(
                    type=ParameterType.LIST, description="List of memorization records"
                ),
            },
        ),
        error_processor_customizations=MergeErrorProcessorCustomizations(
        )
    )
    @abstractmethod
    def list_memorizing(self, include_completed: bool = False) -> dict[str, Any]:
        """List all memories being memorized."""
        pass

    @service_interface_process(
        name="process_memorization_queue",
        is_discoverable=True,
        provider="memory_service",
        parameters={},
        return_value_schema=ReturnValueSchema(
            description="Processing results",
            type=ParameterType.OBJECT,
            properties={
                "processed": ParameterMetadata(
                    type=ParameterType.INTEGER, description="Number of reviews processed"
                ),
                "completed": ParameterMetadata(
                    type=ParameterType.INTEGER, description="Number that completed memorization"
                ),
                "message": ParameterMetadata(
                    type=ParameterType.STRING, description="Summary message"
                ),
            },
        ),
        error_processor_customizations=MergeErrorProcessorCustomizations()
    )
    @abstractmethod
    def process_memorization_queue(self) -> dict[str, Any]:
        """Process all due memorization reviews."""
        pass

    @service_interface_process(
        name="consolidate",
        is_discoverable=True,
        provider="memory_service",
        parameters={
            "strength_threshold": ParameterMetadata(
                description="Consolidate memories weaker than this",
                required=False,
                type=ParameterType.FLOAT,
                default=-1.5,
            ),
            "min_age_days": ParameterMetadata(
                description="Only consolidate memories older than this",
                required=False,
                type=ParameterType.INTEGER,
                default=7,
            ),
            "dry_run": ParameterMetadata(
                description="Preview without executing",
                required=False,
                type=ParameterType.BOOLEAN,
                default=False,
            ),
        },
        return_value_schema=ReturnValueSchema(
            description="Consolidation results",
            type=ParameterType.OBJECT,
            properties={
                "candidates_found": ParameterMetadata(
                    type=ParameterType.INTEGER, description="Number of weak memories found"
                ),
                "clusters_formed": ParameterMetadata(
                    type=ParameterType.INTEGER, description="Number of clusters for summarization"
                ),
                "consolidations": ParameterMetadata(
                    type=ParameterType.LIST, description="List of consolidation actions taken"
                ),
                "dry_run": ParameterMetadata(
                    type=ParameterType.BOOLEAN, description="Whether this was a dry run"
                ),
            },
        ),
        error_processor_customizations=MergeErrorProcessorCustomizations()
    )
    @abstractmethod
    def consolidate(
        self, strength_threshold: float = -1.5, min_age_days: int = 7, dry_run: bool = False
    ) -> dict[str, Any]:
        """Summarize weak episodic memories into semantic memories."""
        pass

    @service_interface_process(
        name="recompute_strengths",
        is_discoverable=True,
        provider="memory_service",
        parameters={},
        return_value_schema=ReturnValueSchema(
            description="Recomputation results",
            type=ParameterType.OBJECT,
            properties={
                "total_memories": ParameterMetadata(
                    type=ParameterType.INTEGER, description="Total memories processed"
                ),
                "updated": ParameterMetadata(
                    type=ParameterType.INTEGER,
                    description="Number of memories with updated strength",
                ),
            },
        ),
        error_processor_customizations=MergeErrorProcessorCustomizations(
        )
    )
    @abstractmethod
    def recompute_strengths(self) -> dict[str, Any]:
        """Recalculate activation strength for all active memories."""
        pass

    # ─────────────────────────────────────────────────────────────────────────
    # CRON-ONLY EDGE_SINK SIBLINGS (Phase 2, 2026-06-17)
    # ─────────────────────────────────────────────────────────────────────────
    # The three *_cron methods below are the cron-fired EDGE_SINK siblings of
    # the model-discoverable maintenance verbs above (process_memorization_queue
    # L982 / consolidate L1016 / recompute_strengths L1075). They exist
    # because the actr_memory_plugin scheduler crons must dispatch a
    # terminal-action shape (EDGE_SINK_SKIP in action_queue_poller._dispatch_*)
    # to avoid the `Empty source_namespace in flow trigger_data` failure mode
    # that the inference-scaffold path triggers for cron-fired flows
    # (~78 errors/10min per target before this fix). The bandage approach —
    # flipping the existing discoverable verbs to EDGE_SINK — would have
    # silently degraded the model-callable surface (the EDGE verbs have
    # active inference test fixtures at
    # `ananta/tests/fixtures/memory_service/*.inference.json`). Per the
    # canonical EDGE_SINK contract documented at
    # `knowledge_bases/ananta_platform/21_scheduling_service/
    # 01_template_flow_record_lifecycle.md`, the wrapping pattern preserves
    # discoverability while routing the cron path through a terminal-action
    # sibling. Each *_cron verb is `is_discoverable=False` (cron-fired only;
    # not model-discoverable), declares
    # `processor_policy_category=ProcessorPolicyCategory.EDGE_SINK`, and
    # omits result_processor_customizations + error_processor_customizations
    # per the canonical EDGE_SINK contract. The binding plugin implements
    # them as thin Shape-A pass-throughs that call the same backend method
    # the EDGE-category sibling calls; semantic-equivalence is preserved
    # because both share the same `self._backend.<verb>()` invocation.
    # Mirrors the session_ledger trigger_poll pattern at
    # `ananta/src/ananta/services/session_ledger_service/interfaces/public.py:756`.

    @service_interface_process(
        name="process_memorization_queue_cron",
        is_discoverable=False,  # cron-fired only; not model-discoverable
        provider="memory_service",
        # EDGE_SINK terminal-action shape — action_queue_poller short-circuits
        # at the EDGE_SINK_SKIP branch (result_processor_kind is None and
        # result_processor is None → no dispatch). No inference scaffold fires;
        # no <<get_flow_input>> macro lookup happens; no core__flows pre-seed
        # is required.
        processor_policy_category=ProcessorPolicyCategory.EDGE_SINK,
        parameters={},
        return_value_schema=ReturnValueSchema(
            description="Processing results",
            type=ParameterType.OBJECT,
            properties={
                "processed": ParameterMetadata(
                    type=ParameterType.INTEGER, description="Number of reviews processed"
                ),
                "completed": ParameterMetadata(
                    type=ParameterType.INTEGER, description="Number that completed memorization"
                ),
                "message": ParameterMetadata(
                    type=ParameterType.STRING, description="Summary message"
                ),
            },
        ),
        # No result_processor_customizations / error_processor_customizations
        # per the canonical EDGE_SINK contract.
    )
    @abstractmethod
    def process_memorization_queue_cron(self) -> dict[str, Any]:
        """Cron-only EDGE_SINK wrapper around process_memorization_queue.

        Thin Shape-A pass-through invoked by the actr_memory_plugin
        memorization-queue cron. Calls the same backend method as the
        discoverable sibling; terminates at EDGE_SINK_SKIP.
        """
        pass

    @service_interface_process(
        name="consolidate_cron",
        is_discoverable=False,  # cron-fired only; not model-discoverable
        provider="memory_service",
        processor_policy_category=ProcessorPolicyCategory.EDGE_SINK,
        parameters={
            "dry_run": ParameterMetadata(
                description="Preview without executing",
                required=False,
                type=ParameterType.BOOLEAN,
                default=False,
            ),
        },
        return_value_schema=ReturnValueSchema(
            description="Consolidation results",
            type=ParameterType.OBJECT,
            properties={
                "candidates_found": ParameterMetadata(
                    type=ParameterType.INTEGER, description="Number of weak memories found"
                ),
                "clusters_formed": ParameterMetadata(
                    type=ParameterType.INTEGER, description="Number of clusters for summarization"
                ),
                "consolidations": ParameterMetadata(
                    type=ParameterType.LIST, description="List of consolidation actions taken"
                ),
                "dry_run": ParameterMetadata(
                    type=ParameterType.BOOLEAN, description="Whether this was a dry run"
                ),
            },
        ),
        # No result_processor_customizations / error_processor_customizations
        # per the canonical EDGE_SINK contract.
    )
    @abstractmethod
    def consolidate_cron(self, dry_run: bool = False) -> dict[str, Any]:
        """Cron-only EDGE_SINK wrapper around consolidate.

        Thin Shape-A pass-through invoked by the actr_memory_plugin
        consolidation cron. Calls the same backend method as the
        discoverable sibling; terminates at EDGE_SINK_SKIP.
        """
        pass

    @service_interface_process(
        name="recompute_strengths_cron",
        is_discoverable=False,  # cron-fired only; not model-discoverable
        provider="memory_service",
        processor_policy_category=ProcessorPolicyCategory.EDGE_SINK,
        parameters={},
        return_value_schema=ReturnValueSchema(
            description="Recomputation results",
            type=ParameterType.OBJECT,
            properties={
                "total_memories": ParameterMetadata(
                    type=ParameterType.INTEGER, description="Total memories processed"
                ),
                "updated": ParameterMetadata(
                    type=ParameterType.INTEGER,
                    description="Number of memories with updated strength",
                ),
            },
        ),
        # No result_processor_customizations / error_processor_customizations
        # per the canonical EDGE_SINK contract.
    )
    @abstractmethod
    def recompute_strengths_cron(self) -> dict[str, Any]:
        """Cron-only EDGE_SINK wrapper around recompute_strengths.

        Thin Shape-A pass-through invoked by the actr_memory_plugin
        strength-recomputation cron. Calls the same backend method as
        the discoverable sibling; terminates at EDGE_SINK_SKIP.
        """
        pass

    @service_interface_process(
        name="memory_stats",
        is_discoverable=True,
        provider="memory_service",
        parameters={},
        return_value_schema=ReturnValueSchema(
            description="Long-term memory statistics",
            type=ParameterType.OBJECT,
            properties={
                "total": ParameterMetadata(
                    type=ParameterType.INTEGER, description="Total number of long-term memories"
                ),
                "by_type": ParameterMetadata(
                    type=ParameterType.OBJECT,
                    description="Counts by memory type (episodic, semantic_l1, semantic_l2)",
                ),
                "by_status": ParameterMetadata(
                    type=ParameterType.OBJECT, description="Counts by status (active, archived)"
                ),
                "strength_distribution": ParameterMetadata(
                    type=ParameterType.OBJECT,
                    description="Counts by strength category (strong, medium, weak, very_weak)",
                ),
            },
        ),
        error_processor_customizations=MergeErrorProcessorCustomizations(
        )
    )
    @abstractmethod
    def memory_stats(self) -> dict[str, Any]:
        """Get memory store statistics."""
        pass

    @service_interface_process(
        name="list_memories",
        is_discoverable=True,
        provider="memory_service",
        parameters={
            "memory_type": ParameterMetadata(
                description="Filter by type",
                required=False,
                type=ParameterType.STRING,
                default=None,
            ),
            "status": ParameterMetadata(
                description="Filter by status",
                required=False,
                type=ParameterType.STRING,
                default="active",
            ),
            "tag": ParameterMetadata(
                description="Filter by tag", required=False, type=ParameterType.STRING, default=None
            ),
            "sort_by": ParameterMetadata(
                description="Sort by: 'strength', 'created_at', 'retrieval_count'",
                required=False,
                type=ParameterType.STRING,
                default="strength",
            ),
            "limit": ParameterMetadata(
                description="Maximum results",
                required=False,
                type=ParameterType.INTEGER,
                default=20,
            ),
        },
        return_value_schema=ReturnValueSchema(
            description="List of memories",
            type=ParameterType.OBJECT,
            properties={
                "memories": ParameterMetadata(
                    type=ParameterType.LIST, description="List of memory records"
                ),
                "total": ParameterMetadata(
                    type=ParameterType.INTEGER, description="Total matching memories"
                ),
                "showing": ParameterMetadata(
                    type=ParameterType.INTEGER, description="Number shown"
                ),
            },
        ),
        error_processor_customizations=MergeErrorProcessorCustomizations()
    )
    @abstractmethod
    def list_memories(
        self,
        memory_type: str | None = None,
        status: str = "active",
        tag: str | None = None,
        sort_by: str = "strength",
        limit: int = 20,
    ) -> dict[str, Any]:
        """List memories with filters."""
        pass

    @service_interface_process(
        name="export_memories",
        is_discoverable=True,
        provider="memory_service",
        parameters={
            "file_path": ParameterMetadata(
                description="Output file path (default: timestamped)",
                required=False,
                type=ParameterType.STRING,
                default=None,
            ),
            "include_archived": ParameterMetadata(
                description="Include archived memories",
                required=False,
                type=ParameterType.BOOLEAN,
                default=False,
            ),
            "include_embeddings": ParameterMetadata(
                description="Include embedding vectors (large file)",
                required=False,
                type=ParameterType.BOOLEAN,
                default=False,
            ),
            "tags": ParameterMetadata(
                description="Optional ALL-semantics tag filter — export only records carrying EVERY listed tag (e.g. one origin's projection: ['agent_memory', 'agent_memory:origin:<X>'])",
                required=False,
                type=ParameterType.LIST,
                default=None,
            ),
        },
        return_value_schema=ReturnValueSchema(
            description="Export results",
            type=ParameterType.OBJECT,
            properties={
                "file_path": ParameterMetadata(
                    type=ParameterType.STRING, description="Path to exported file"
                ),
                "memory_count": ParameterMetadata(
                    type=ParameterType.INTEGER, description="Number of memories exported"
                ),
            },
        ),
        error_processor_customizations=MergeErrorProcessorCustomizations()
    )
    @abstractmethod
    def export_memories(
        self,
        file_path: str | None = None,
        include_archived: bool = False,
        include_embeddings: bool = False,
        tags: list[str] | None = None,
    ) -> dict[str, Any]:
        """Export memories to JSON file."""
        pass

    @service_interface_process(
        name="import_memories",
        is_discoverable=True,
        provider="memory_service",
        parameters={
            "file_path": ParameterMetadata(
                description="Path to import file", required=True, type=ParameterType.STRING
            ),
            "regenerate_embeddings": ParameterMetadata(
                description="Regenerate embeddings (required if not in export)",
                required=False,
                type=ParameterType.BOOLEAN,
                default=True,
            ),
        },
        return_value_schema=ReturnValueSchema(
            description="Import results",
            type=ParameterType.OBJECT,
            properties={
                "imported": ParameterMetadata(
                    type=ParameterType.INTEGER, description="Number of memories imported"
                ),
                "skipped": ParameterMetadata(
                    type=ParameterType.INTEGER, description="Number skipped (already exist)"
                ),
                "total_in_file": ParameterMetadata(
                    type=ParameterType.INTEGER, description="Total memories in file"
                ),
            },
        ),
        error_processor_customizations=MergeErrorProcessorCustomizations()
    )
    @abstractmethod
    def import_memories(self, file_path: str, regenerate_embeddings: bool = True) -> dict[str, Any]:
        """Import memories from JSON file."""
        pass

    @service_interface_process(
        name="cleanup_orphaned_vectors",
        is_discoverable=True,
        provider="memory_service",
        parameters={
            "dry_run": ParameterMetadata(
                description="If True, report the would-be {cleared, reindexed} counts without mutating",
                required=False,
                type=ParameterType.BOOLEAN,
                default=False,
            ),
            "confirm": ParameterMetadata(
                description="Must be True for the destructive rebuild (wipes then regenerates every vector); an un-confirmed non-dry-run is rejected",
                required=False,
                type=ParameterType.BOOLEAN,
                default=False,
            ),
        },
        return_value_schema=ReturnValueSchema(
            description="Rebuild results",
            type=ParameterType.OBJECT,
            properties={
                "dry_run": ParameterMetadata(
                    type=ParameterType.BOOLEAN, description="Whether this was a dry run (no mutation)"
                ),
                "cleared": ParameterMetadata(
                    type=ParameterType.INTEGER,
                    description="Vectors cleared from the namespace (would-be count under dry_run)",
                ),
                "reindexed": ParameterMetadata(
                    type=ParameterType.INTEGER,
                    description="Live memories reindexed (would-be count under dry_run)",
                ),
            },
        ),
        error_processor_customizations=MergeErrorProcessorCustomizations()
    )
    @abstractmethod
    def cleanup_orphaned_vectors(
        self, dry_run: bool = False, confirm: bool = False
    ) -> dict[str, Any]:
        """Rebuild the memory-vector namespace: clear then reindex every memory."""
        pass

    @service_interface_process(
        name="reindex_orphaned_vectors",
        is_discoverable=True,
        provider="memory_service",
        parameters={},
        return_value_schema=ReturnValueSchema(
            description="Reindexing results",
            type=ParameterType.OBJECT,
            properties={
                "action_status": ParameterMetadata(
                    type=ParameterType.STRING, description="Operation status"
                ),
                "data": ParameterMetadata(
                    type=ParameterType.OBJECT,
                    description="Contains reindex_count and message",
                ),
            },
        ),
        error_processor_customizations=MergeErrorProcessorCustomizations()
    )
    @abstractmethod
    def reindex_orphaned_vectors(self) -> dict[str, Any]:
        """Reindex memories that have DB records but no embeddings."""
        pass

    @service_interface_process(
        name="purge_memories",
        is_discoverable=True,
        provider="memory_service",
        parameters={
            "confirm": ParameterMetadata(
                description="Must be True to proceed with purge (safety check)",
                required=True,
                type=ParameterType.BOOLEAN,
            ),
        },
        return_value_schema=ReturnValueSchema(
            description="Purge operation results with counts of deleted items",
            type=ParameterType.OBJECT,
            properties={
                "purged": ParameterMetadata(
                    type=ParameterType.BOOLEAN, description="True if purge completed"
                ),
                "deleted_memories": ParameterMetadata(
                    type=ParameterType.INTEGER, description="Number of memories deleted"
                ),
                "deleted_memorizations": ParameterMetadata(
                    type=ParameterType.INTEGER, description="Number of memorizations deleted"
                ),
                "deleted_vectors": ParameterMetadata(
                    type=ParameterType.INTEGER, description="Number of vectors deleted"
                ),
            },
        ),
        error_processor_customizations=MergeErrorProcessorCustomizations()
    )
    @abstractmethod
    def purge_memories(self, confirm: bool = False) -> dict[str, Any]:
        """Purge all memories and memorizations from the system."""
        pass

    # ─────────────────────────────────────────────────────────────────────────
    # P3 MAINTENANCE: Audit and Review Operations
    # ─────────────────────────────────────────────────────────────────────────

    @service_interface_process(
        name="audit_pinned_notes",
        is_discoverable=True,
        provider="memory_service",
        parameters={
            "include_completed": ParameterMetadata(
                description="Include memorizations that have completed the learning cycle",
                required=False,
                type=ParameterType.BOOLEAN,
                default=False,
            ),
            "strength_threshold": ParameterMetadata(
                description="Only show memories with strength below this value (for finding weak pins)",
                required=False,
                type=ParameterType.FLOAT,
                default=None,
            ),
        },
        return_value_schema=ReturnValueSchema(
            description="Audit results for pinned notes",
            type=ParameterType.OBJECT,
            properties={
                "total_pinned": ParameterMetadata(
                    type=ParameterType.INTEGER, description="Total number of pinned memories"
                ),
                "active": ParameterMetadata(
                    type=ParameterType.INTEGER, description="Number actively being memorized"
                ),
                "completed": ParameterMetadata(
                    type=ParameterType.INTEGER, description="Number that completed memorization"
                ),
                "weak_pins": ParameterMetadata(
                    type=ParameterType.INTEGER,
                    description="Number of pinned memories with low strength",
                ),
                "items": ParameterMetadata(
                    type=ParameterType.LIST,
                    description="List of pinned memory details with strength and schedule",
                ),
            },
        ),
        error_processor_customizations=MergeErrorProcessorCustomizations(
        )
    )
    @abstractmethod
    def audit_pinned_notes(
        self,
        include_completed: bool = False,
        strength_threshold: float | None = None,
    ) -> dict[str, Any]:
        """Audit memories in the memorization queue."""
        pass

    @service_interface_process(
        name="review_blocked_intents",
        is_discoverable=True,
        provider="memory_service",
        parameters={
            "min_age_days": ParameterMetadata(
                description="Only show blocked intents older than this many days",
                required=False,
                type=ParameterType.INTEGER,
                default=7,
            ),
            "strength_threshold": ParameterMetadata(
                description="Only show blocked intents weaker than this (candidates for retry)",
                required=False,
                type=ParameterType.FLOAT,
                default=-1.0,
            ),
        },
        return_value_schema=ReturnValueSchema(
            description="Review results for blocked intents",
            type=ParameterType.OBJECT,
            properties={
                "total_blocked": ParameterMetadata(
                    type=ParameterType.INTEGER, description="Total blocked intent memories"
                ),
                "stale_count": ParameterMetadata(
                    type=ParameterType.INTEGER,
                    description="Number of stale blocked intents (old and weak)",
                ),
                "retry_candidates": ParameterMetadata(
                    type=ParameterType.LIST,
                    description="Blocked intents that may warrant retry via discovery",
                ),
                "still_valid": ParameterMetadata(
                    type=ParameterType.LIST,
                    description="Blocked intents that are still strong (recently accessed)",
                ),
            },
        ),
        error_processor_customizations=MergeErrorProcessorCustomizations(
        )
    )
    @abstractmethod
    def review_blocked_intents(
        self,
        min_age_days: int = 7,
        strength_threshold: float = -1.0,
    ) -> dict[str, Any]:
        """Review blocked intent memories for staleness."""
        pass

    # ─────────────────────────────────────────────────────────────────────────
    # FOCUS BUFFER
    # ─────────────────────────────────────────────────────────────────────────

    @service_interface_process(
        name="focus",
        is_discoverable=True,
        provider="memory_service",
        parameters={
            "memory_id": ParameterMetadata(
                description="ID of the memory to pin to focus buffer",
                required=True,
                type=ParameterType.STRING,
            ),
            "state": ParameterMetadata(
                description=(
                    "Current application state (automatically injected at "
                    "execution time; carries the acting session — JOS-02)"
                ),
                required=False,
                type=ParameterType.OBJECT,
            ),
        },
        return_value_schema=ReturnValueSchema(
            description="Focus result with memory_id and current focused count",
            type=ParameterType.OBJECT,
            properties={
                "memory_id": ParameterMetadata(
                    type=ParameterType.STRING, description="ID of the focused memory"
                ),
                "session_id": ParameterMetadata(
                    type=ParameterType.STRING, description="Session owning the pin"
                ),
                "focused_count": ParameterMetadata(
                    type=ParameterType.INTEGER,
                    description="Number of memories focused in the acting session",
                ),
            },
        ),
        error_processor_customizations=MergeErrorProcessorCustomizations()
    )
    @abstractmethod
    def focus(self, memory_id: str, *, session_id: str) -> dict[str, Any]:
        """Pin a memory into the acting session's focus buffer (JOS-02)."""
        pass

    @service_interface_process(
        name="unfocus",
        is_discoverable=True,
        provider="memory_service",
        parameters={
            "memory_id": ParameterMetadata(
                description="ID of the memory to unpin from focus buffer",
                required=True,
                type=ParameterType.STRING,
            ),
            "state": ParameterMetadata(
                description=(
                    "Current application state (automatically injected at "
                    "execution time; carries the acting session — JOS-02)"
                ),
                required=False,
                type=ParameterType.OBJECT,
            ),
        },
        return_value_schema=ReturnValueSchema(
            description="Unfocus confirmation",
            type=ParameterType.OBJECT,
            properties={
                "memory_id": ParameterMetadata(
                    type=ParameterType.STRING, description="ID of the unfocused memory"
                ),
                "session_id": ParameterMetadata(
                    type=ParameterType.STRING, description="Session the pin was released from"
                ),
                "message": ParameterMetadata(
                    type=ParameterType.STRING, description="Confirmation message"
                ),
            },
        ),
        error_processor_customizations=MergeErrorProcessorCustomizations(
        )
    )
    @abstractmethod
    def unfocus(self, memory_id: str, *, session_id: str) -> dict[str, Any]:
        """Remove a memory from the acting session's focus buffer (JOS-02)."""
        pass

    @service_interface_process(
        name="get_focused",
        is_discoverable=True,
        provider="memory_service",
        parameters={
            "state": ParameterMetadata(
                description=(
                    "Current application state (automatically injected at "
                    "execution time; carries the acting session — JOS-02)"
                ),
                required=False,
                type=ParameterType.OBJECT,
            ),
        },
        return_value_schema=ReturnValueSchema(
            description="The acting session's focused memories with full content",
            type=ParameterType.OBJECT,
            properties={
                "memories": ParameterMetadata(
                    type=ParameterType.LIST,
                    description="Focused memory records with memory_id and full content",
                ),
                "count": ParameterMetadata(
                    type=ParameterType.INTEGER, description="Number of focused memories"
                ),
            },
        ),
        error_processor_customizations=MergeErrorProcessorCustomizations(
        )
    )
    @abstractmethod
    def get_focused(self, *, session_id: str) -> dict[str, Any]:
        """Return the acting session's focused memories as {"memories": [...], "count": N}."""
        pass

    # ─────────────────────────────────────────────────────────────────────────
    # LIFECYCLE
    # ─────────────────────────────────────────────────────────────────────────

    @service_interface_process(
        name="store_compaction_summary",
        is_discoverable=False,
        provider="memory_service",
        parameters={
            "context_id": ParameterMetadata(
                description="ID of the context being compacted",
                required=True,
                type=ParameterType.STRING,
            ),
            "summary": ParameterMetadata(
                description="The generated summary text",
                required=True,
                type=ParameterType.STRING,
            ),
            "compacted_event_count": ParameterMetadata(
                description="Number of events that were compacted",
                required=True,
                type=ParameterType.INTEGER,
            ),
            "session_id": ParameterMetadata(
                description="Optional session identifier",
                required=False,
                type=ParameterType.STRING,
                default=None,
            ),
        },
        return_value_schema=ReturnValueSchema(
            description="Result with memory_id of stored summary",
            type=ParameterType.OBJECT,
            properties={
                "memory_id": ParameterMetadata(
                    type=ParameterType.STRING, description="UUID of the created summary memory"
                ),
                "message": ParameterMetadata(
                    type=ParameterType.STRING, description="Confirmation message"
                ),
            },
        ),
        error_processor_customizations=MergeErrorProcessorCustomizations()
    )
    @abstractmethod
    def store_compaction_summary(
        self,
        context_id: str,
        summary: str,
        compacted_event_count: int,
        session_id: str | None = None,
    ) -> dict[str, Any]:
        """Store a compaction summary from context management."""
        pass
