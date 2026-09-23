"""Memory Service Interface - ACT-R inspired memory with decay and consolidation."""

from abc import ABC, abstractmethod
from typing import Any, ClassVar


class MemoryServiceInterface(ABC):
    """Biologically-inspired memory system with decay, strengthening, and consolidation.

    Based on ACT-R cognitive architecture:
    - Memories decay over time (unused memories fade)
    - Retrieval strengthens memories (spaced repetition effect)
    - Episodic memories consolidate into semantic summaries

    Plugins implementing this interface should:
    1. Define service_interfaces property returning tuple containing MemoryServiceInterface
    2. Define supported_interface_versions property with version mapping
    3. Support vector similarity search for recall
    4. Implement strength-based decay model
    """

    INTERFACE_VERSION: ClassVar[str] = "1.3.0"  # Added short-term memory, tags, learning, audit, maintenance

    @abstractmethod
    def remember(
        self,
        content: str,
        tags: list[str] | None = None,
        source_file: str | None = None,
        session_id: str | None = None,
        embed: bool = True,
    ) -> dict[str, Any]:
        """Store a new memory.

        Args:
            content: The memory content to store
            tags: Optional tags for organization and filtering
            source_file: Optional source file identifier
            session_id: Optional session identifier
            embed: Whether to generate a vector embedding (default True).
                Set to False for focus-only working-context mirrors.

        Returns:
            Dict with memory_id or error
        """
        ...

    @abstractmethod
    def recall(
        self,
        query: str,
        top_k: int = 5,
        memory_type: str = "all",
        include_archived: bool = False,
        tags: list[str] | None = None,
        exclude_ids: list[str] | None = None,
        score_by_similarity: bool = False,
        exclude_tags: list[str] | None = None,
    ) -> dict[str, Any]:
        """Retrieve memories by semantic similarity.

        Retrieval strengthens matching memories (spaced repetition effect).

        Args:
            query: Search query for semantic similarity
            top_k: Maximum number of results
            memory_type: Type filter ('all', 'episodic', 'semantic_l1', 'semantic_l2')
            include_archived: Whether to include archived memories
            tags: Optional list of tags - only return memories with ALL specified tags
            exclude_ids: Optional list of memory IDs to exclude from results
            score_by_similarity: When True, rank by raw cosine similarity only,
                bypassing ACT-R strength weighting and retrieval boost. Use for
                reference documentation searches where retrieval history should
                not affect ranking.
            exclude_tags: Optional list of tags - exclude memories with ANY of these tags.
                Used to separate knowledge base content from personal memories.

        Returns:
            Dict with list of matching memories or error
        """
        ...

    @abstractmethod
    def forget(self, memory_id: str) -> dict[str, Any]:
        """Archive a memory (soft delete).

        Archived memories are excluded from recall but retained for audit.

        Args:
            memory_id: The memory identifier to archive

        Returns:
            Dict indicating success or error
        """
        ...

    @abstractmethod
    def reinforce(self, memory_id: str) -> dict[str, Any]:
        """Explicitly reinforce a memory by adding a retrieval timestamp.

        This strengthens the memory's ACT-R activation, making it more likely
        to surface in future recalls. Use this when surfacing memories in context
        to ensure useful memories stay strong.

        Args:
            memory_id: The memory identifier to reinforce

        Returns:
            Dict with memory_id, new_strength, and retrieval_count, or error
        """
        ...

    @abstractmethod
    def memorize(
        self,
        memory_id: str | None = None,
        content: str | None = None,
    ) -> dict[str, Any]:
        """Add content to spaced repetition memorization queue.

        Creates a memory and schedules it for spaced repetition review.

        Args:
            memory_id: Optional existing memory ID to memorize
            content: Optional new content to memorize

        Returns:
            Dict with memory_id and next review time or error
        """
        ...

    @abstractmethod
    def memory_stats(self) -> dict[str, Any]:
        """Get memory system statistics.

        Returns:
            Dict with counts by type, status, and strength distribution
        """
        ...

    @abstractmethod
    def list_memories(
        self,
        memory_type: str | None = None,
        status: str = "active",
        tag: str | None = None,
        sort_by: str = "strength",
        limit: int = 20,
    ) -> dict[str, Any]:
        """List memories with optional filters.

        Args:
            memory_type: Optional type filter ('episodic', 'semantic_l1', 'semantic_l2')
            status: Status filter ('active', 'archived')
            tag: Optional tag filter
            sort_by: Sort field ('strength', 'created_at', 'retrieval_count')
            limit: Maximum results

        Returns:
            Dict with list of memories
        """
        ...

    @abstractmethod
    def consolidate(
        self,
        strength_threshold: float = -1.5,
        min_age_days: int = 7,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        """Consolidate old episodic memories into semantic summaries.

        Groups related episodic memories and creates semantic L1/L2 summaries.

        Args:
            strength_threshold: Minimum strength threshold for consolidation
            min_age_days: Minimum age in days before consolidation
            dry_run: If True, report what would be consolidated without doing it

        Returns:
            Dict with consolidation summary
        """
        ...

    @abstractmethod
    def export_memories(
        self,
        file_path: str | None = None,
        include_archived: bool = False,
        include_embeddings: bool = False,
        tags: list[str] | None = None,
    ) -> dict[str, Any]:
        """Export memories for backup or transfer.

        Args:
            file_path: Output file path (auto-generated if not specified)
            include_archived: Include archived memories
            include_embeddings: Include vector embeddings in export
            tags: Optional ALL-semantics tag filter — only records carrying every
                listed tag are exported (e.g. a single origin's projection records)

        Returns:
            Dict with file path and memory count
        """
        ...

    @abstractmethod
    def purge_memories(
        self,
        confirm: bool = False,
    ) -> dict[str, Any]:
        """Permanently delete all memories (hard delete).

        Args:
            confirm: Must be True to proceed with purge

        Returns:
            Dict with purge summary
        """
        ...

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

        Args:
            session_id: Optional session filter (memories are global by default)
            max_events: Maximum number of events to return
            max_age_hours: Optional maximum age filter
            namespace_filter: Optional namespace filter

        Returns:
            Dict with history string and event_count
        """
        ...

    @abstractmethod
    def get_session_event_stats(self, session_id: str) -> dict[str, Any]:
        """Get conversation event statistics for a session.

        Args:
            session_id: Session identifier

        Returns:
            Dict with event statistics
        """
        ...

    # ==========================================================================
    # SHORT-TERM MEMORY
    # ==========================================================================

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
        """Store a short-term interaction event.

        Args:
            session_id: Session identifier
            source_namespace: Source namespace for the event
            event_type: Type of event (e.g., 'user_input', 'assistant_response')
            content: Event content
            metadata: Optional additional metadata
            timestamp: Optional explicit timestamp (ISO format)

        Returns:
            Dict with event_id or error
        """
        ...

    @abstractmethod
    def get_recent_memory_structured(
        self,
        session_id: str | None = None,
        max_events: int = 20,
        max_age_hours: int | None = None,
        namespace_filter: str | None = None,
    ) -> dict[str, Any]:
        """Retrieve recent memory as structured events.

        Args:
            session_id: Optional session filter
            max_events: Maximum number of events to return
            max_age_hours: Optional maximum age filter
            namespace_filter: Optional namespace filter

        Returns:
            Envelope dict: {"events": [<event dicts with timestamp, type,
            content, metadata>], "count": N}. The dispatch contract for
            service-interface verbs requires a dict return.
        """
        ...

    # ==========================================================================
    # TAG OPERATIONS
    # ==========================================================================

    @abstractmethod
    def delete_memories_by_tag(self, tag: str) -> dict[str, Any]:
        """Delete all memories with a specific tag.

        Used for knowledge base lifecycle management.

        Args:
            tag: Tag to match for deletion

        Returns:
            Dict with deleted_count or error
        """
        ...

    @abstractmethod
    def delete_memories_by_ids(self, ids: list[str]) -> dict[str, Any]:
        """Hard-delete specific memories by id, with their embeddings.

        The id-list counterpart of delete_memories_by_tag, for callers that
        already hold the exact ids (e.g. knowledge-base per-file and sweep
        deletes). Cascade VECTOR-FIRST: the embeddings are deleted before the
        memory records, so a crash mid-cascade leaves at most an orphan-MEMORY
        (reconcilable via reindex), never an orphan-VECTOR. Fails fast.

        Args:
            ids: Memory ids to delete

        Returns:
            Dict with deleted_count or error
        """
        ...

    @abstractmethod
    def get_memories_by_tag(
        self,
        tag: str,
        include_archived: bool = False,
    ) -> dict[str, Any]:
        """Get all memories with a specific tag.

        Args:
            tag: Tag to match
            include_archived: Whether to include archived memories

        Returns:
            Dict with memories list or error
        """
        ...

    @abstractmethod
    def upsert_memory_by_tag(
        self,
        content: str,
        tag: str,
        tags: list[str] | None = None,
        session_id: str | None = None,
    ) -> dict[str, Any]:
        """Replace-by-tag: store new content, then delete previous memories with this tag.

        Args:
            content: The content to store as the canonical memory
            tag: Tag identifying the memory slot (stays the replace key)
            tags: Optional provenance/umbrella tags unioned onto the stored
                record (the slot tag is always included and first); needed so a
                projection record literally carries its umbrella/origin tags
            session_id: Optional session identifier

        Returns:
            Dict with deleted_count, duplicates_reconciled, memory_id, tag
        """
        ...

    @abstractmethod
    def memorize_by_tag(self, tag: str) -> dict[str, Any]:
        """Add all memories with a tag to the memorization queue.

        Args:
            tag: Tag to match

        Returns:
            Dict with queued_count or error
        """
        ...

    # ==========================================================================
    # MEMORIZATION QUEUE
    # ==========================================================================

    @abstractmethod
    def stop_memorizing(self, memory_id: str) -> dict[str, Any]:
        """Remove a memory from the memorization queue.

        Args:
            memory_id: Memory identifier to stop memorizing

        Returns:
            Dict indicating success or error
        """
        ...

    @abstractmethod
    def list_memorizing(self, include_completed: bool = False) -> dict[str, Any]:
        """List memories in the memorization queue.

        Args:
            include_completed: Include completed memorization items

        Returns:
            Dict with queue items
        """
        ...

    @abstractmethod
    def process_memorization_queue(self) -> dict[str, Any]:
        """Process pending memorization reviews.

        Returns:
            Dict with processing summary
        """
        ...

    # ==========================================================================
    # MAINTENANCE
    # ==========================================================================

    @abstractmethod
    def recompute_strengths(self) -> dict[str, Any]:
        """Recompute all memory strengths based on decay model.

        Returns:
            Dict with update summary
        """
        ...

    # ==========================================================================
    # CRON-ONLY EDGE_SINK SIBLINGS (Phase 2, 2026-06-17)
    # ==========================================================================
    # The three *_cron methods are EDGE_SINK siblings of the discoverable
    # maintenance verbs above (process_memorization_queue / consolidate /
    # recompute_strengths). They exist so the actr_memory scheduler crons can
    # dispatch a terminal-action shape — invoked by the actr_memory_plugin
    # cron path only, not by the model. Implementations are thin Shape-A
    # pass-throughs that share the same backend method as the discoverable
    # sibling. See `services/memory_service/interfaces/public.py` for the
    # @service_interface_process declarations + the canonical contract at
    # `knowledge_bases/ananta_platform/21_scheduling_service/
    # 01_template_flow_record_lifecycle.md`.

    @abstractmethod
    def process_memorization_queue_cron(self) -> dict[str, Any]:
        """Cron-only EDGE_SINK wrapper around process_memorization_queue.

        Returns:
            Dict with processing summary (same envelope as the discoverable
            sibling).
        """
        ...

    @abstractmethod
    def consolidate_cron(self, dry_run: bool = False) -> dict[str, Any]:
        """Cron-only EDGE_SINK wrapper around consolidate.

        Args:
            dry_run: If True, report what would be consolidated without doing it

        Returns:
            Dict with consolidation summary (same envelope as the discoverable
            sibling).
        """
        ...

    @abstractmethod
    def recompute_strengths_cron(self) -> dict[str, Any]:
        """Cron-only EDGE_SINK wrapper around recompute_strengths.

        Returns:
            Dict with update summary (same envelope as the discoverable
            sibling).
        """
        ...

    @abstractmethod
    def import_memories(
        self,
        file_path: str,
        regenerate_embeddings: bool = True,
    ) -> dict[str, Any]:
        """Import memories from a file.

        Args:
            file_path: Path to import file
            regenerate_embeddings: Whether to regenerate embeddings

        Returns:
            Dict with import summary
        """
        ...

    @abstractmethod
    def cleanup_orphaned_vectors(
        self, dry_run: bool = False, confirm: bool = False
    ) -> dict[str, Any]:
        """Rebuild the memory-vector namespace (clear then reindex every memory).

        Cascade-vector-first makes orphan-vectors unreachable-by-construction, so
        this is a one-shot operator-gated repair for pre-existing orphans.

        Args:
            dry_run: If True, report the would-be {cleared, reindexed} counts
                without mutating.
            confirm: Must be True for the destructive run (it wipes every vector
                before regenerating); an un-confirmed non-dry-run is rejected.

        Returns:
            Dict {dry_run, cleared, reindexed}.
        """
        ...

    @abstractmethod
    def reindex_orphaned_vectors(self) -> dict[str, Any]:
        """Attempt to relink orphaned vectors to memories.

        Returns:
            Dict with reindex summary
        """
        ...

    # ==========================================================================
    # LEARNING
    # ==========================================================================

    @abstractmethod
    def ingest_session(
        self,
        transcript: str,
        session_id: str | None = None,
        chunk_by_turns: bool = True,
    ) -> dict[str, Any]:
        """Ingest a session transcript into memory.

        Args:
            transcript: Session transcript text
            session_id: Optional session identifier
            chunk_by_turns: Whether to chunk by conversation turns

        Returns:
            Dict with ingestion summary
        """
        ...

    @abstractmethod
    def learn(
        self,
        path: str,
        pattern: str = "*.md",
        recursive: bool = True,
        memorize: bool = False,
        tags: list[str] | None = None,
    ) -> dict[str, Any]:
        """Learn from files in a directory.

        Args:
            path: Directory path to learn from
            pattern: File pattern to match
            recursive: Whether to search recursively
            memorize: Whether to add to memorization queue
            tags: Optional tags to apply

        Returns:
            Dict with learning summary
        """
        ...

    # ==========================================================================
    # AUDIT
    # ==========================================================================

    @abstractmethod
    def audit_pinned_notes(
        self,
        include_completed: bool = False,
        strength_threshold: float | None = None,
    ) -> dict[str, Any]:
        """Audit pinned notes for review.

        Args:
            include_completed: Include completed items
            strength_threshold: Optional strength filter

        Returns:
            Dict with audit results
        """
        ...

    @abstractmethod
    def review_blocked_intents(
        self,
        min_age_days: int = 7,
        strength_threshold: float = -1.0,
    ) -> dict[str, Any]:
        """Review blocked intents for potential unblocking.

        Args:
            min_age_days: Minimum age in days
            strength_threshold: Strength threshold for review

        Returns:
            Dict with review results
        """
        ...

    # ==========================================================================
    # FOCUS BUFFER
    # ==========================================================================

    @abstractmethod
    def focus(self, memory_id: str, *, session_id: str) -> dict[str, Any]:
        """Pin a memory into the acting session's focus buffer (JOS-02).

        Capacity is a per-session safety valve (MAX_FOCUSED, currently 20);
        the real constraint is FOCUS_BUDGET_FRACTION at prompt assembly.
        Error on overflow — caller must unfocus something first. A memory can
        be pinned in at most one session.

        Args:
            memory_id: The memory identifier to pin
            session_id: The acting session owning the pin (required)

        Returns:
            Dict with memory_id, session_id, and focused count, or error
        """
        ...

    @abstractmethod
    def unfocus(self, memory_id: str, *, session_id: str) -> dict[str, Any]:
        """Remove a memory from the acting session's focus buffer.

        The delete is session-predicated — a session can only unpin its own
        pins (JOS-02).

        Args:
            memory_id: The memory identifier to unpin
            session_id: The acting session (required)

        Returns:
            Dict indicating success or error
        """
        ...

    @abstractmethod
    def unfocus_all_for_session(self, *, session_id: str) -> dict[str, Any]:
        """Release EVERY pin held by one session (JOS-02 R1 terminal release).

        Whole-buffer release for ephemeral run sessions. Idempotent: an empty
        buffer is a benign no-op.

        Args:
            session_id: The session whose buffer is released (required)

        Returns:
            Dict with session_id, released_memory_ids, and count
        """
        ...

    @abstractmethod
    def get_focused(self, *, session_id: str) -> dict[str, Any]:
        """Return the acting session's focused memories with full content.

        Focus is session-scoped (JOS-02): each session owns its own buffer,
        capped by the per-session MAX_FOCUSED safety valve.

        Args:
            session_id: The acting session whose buffer to read (required)

        Returns:
            Envelope dict: {"memories": [<focused memory dicts>], "count": N}.
            The dispatch contract for service-interface verbs requires a dict
            return (action_processor rejects non-dict results).
        """
        ...

    # ==========================================================================
    # LIFECYCLE
    # ==========================================================================

    @abstractmethod
    def store_compaction_summary(
        self,
        context_id: str,
        summary: str,
        compacted_event_count: int,
        session_id: str | None = None,
    ) -> dict[str, Any]:
        """Store a compaction summary from context management.

        Args:
            context_id: Context identifier
            summary: Compacted summary text
            compacted_event_count: Number of events compacted
            session_id: Optional session identifier

        Returns:
            Dict with storage result
        """
        ...
