"""ACT-R Memory Backend - Core implementation of memory service.

This module contains the ACTRMemoryBackend class which provides the full
implementation of the memory system based on ACT-R (Adaptive Control of
Thought-Rational) cognitive architecture.

Provides two memory systems:

1. SHORT-TERM SESSION MEMORY
   - store_interaction() / get_recent_memory()
   - Conversation history within a session
   - SQL-based storage in state_service

2. LONG-TERM ACT-R MEMORY
   - remember() / recall() / memorize() / learn()
   - Knowledge with decay and spaced repetition
   - Vector-based semantic search via vector_service
"""

import json
import logging
import os
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Final, cast

from ananta.core.domain.enums import ActionStatus
from ananta.error_handling import FrameworkError
from ananta.services.memory_service.actr import (
    DEFAULT_TOP_K,
    EPISODIC_CONSOLIDATION_THRESHOLD,
    INITIAL_INTERVAL_DAYS,
    INTERVAL_MULTIPLIER,
    MAX_INTERVAL_DAYS,
    MEMORIZATION_COMPLETE_REVIEWS,
    MIN_AGE_FOR_CONSOLIDATION_DAYS,
    MIN_CLUSTER_SIZE,
    RETRIEVAL_BOOST_TOP_N,
    chunk_code_file,
    chunk_document,
    compute_strength,
)
from ananta.services.memory_service.actr.activation import compute_final_score
from ananta.services.memory_service.actr.consolidation import cluster_memories, generate_summary
from ananta.services.memory_service.actr.constants import EMBEDDING_MAX_CHARS, VECTOR_NAMESPACE
from ananta.services.memory_service.schema import NAMESPACE
from ananta.services.state_service.bounded_read import (
    assert_within_ceiling,
    iter_table_rows,
)

from .constants import CONFIG_KEY_EXPORT_ALLOWED_ROOTS, PLUGIN_NAME
from .export_containment import assert_path_within_allowed_roots
from .memory_store import (
    delete_memory_records,
    find_memories_by_tag,
    get_all_memories,
    get_all_memorizations,
    get_memorization,
    get_memory,
    save_memorization,
    save_memory,
    store_memory_vector,
)
from .record_helpers import (
    build_learn_result,
    build_recall_filters,
    content_preview,
    create_memory_record,
    filter_memories_by_all_tags,
    format_memory_record,
    is_review_due,
    normalize_tags,
    parse_created_at,
    parse_json_field,
    parse_memory_json_fields,
    update_audit_counts,
)
from .session_query import (
    build_memory_query,
    convert_memory_record,
    query_event_summary,
    query_namespace_breakdown,
)

logger = logging.getLogger(__name__)

# A boot invariant over rows that MUST NOT EXIST: every focus_buffer row is
# supposed to carry a session_id, and the JOS-02 migration clears the legacy ones
# that do not. The healthy count is 0 (MEASURED: actr_memory_plugin.focus_buffer
# holds 0 unscoped rows, 2026-08-15), and the error path prints every offending
# memory_id, so a ceiling here bounds an error message rather than a table.
#
# 100 deliberately matches the platform's default row bound rather than exceeding
# it: this read needs no `unbounded` opt-in and stays correct when the default
# drops to 100. A hundred legacy pins is already far more than "the migration did
# not run" needs to be obvious.
_UNSCOPED_FOCUS_CEILING = 100

# Bound on the PURGE walk (``_get_all_memory_ids``), which pages the whole
# ``memory`` table to collect every id for embedding cleanup.
#
# This is NOT a claim that the table is small — it plainly is not, and that is
# why the walk is paginated rather than ceiling-checked like the focus read
# above. It is a claim about the MECHANISM: a purge that collects ids in Python
# and deletes them one at a time is sized for a corpus, not for a warehouse.
#
# Measured 22,238 rows on 2026-08-15, and growing fast — it gained ~7,600 rows in
# fifteen minutes of that day's sweep, because it accumulates a row per
# knowledge-base chunk ingested. Half a million is roughly 20x the measured size
# and ~5,000 pages; past it the honest repair is a set-based delete evaluated in
# the database, not a larger number here. Purge is an explicit, confirmed,
# destructive operator action, so failing loud and making them re-decide is the
# right behaviour at that size.
_PURGE_WALK_CEILING = 500_000
_PURGE_WALK_CEILING_REASON = (
    "one row per stored memory, including every knowledge-base chunk (measured "
    "22,238 on 2026-08-15); a purge that collects ids in Python and deletes them "
    "individually is sized for a corpus, not a warehouse."
)

# Tag used for identity memories (must match prompt_context_builder.IDENTITY_TAG)
IDENTITY_TAG = "identity"

# Canonical tag on knowledge-base chunks (must match default_knowledge_plugin's
# TAG_DOMAIN_OFFICIAL and the recall-time exclude_tags / purge exemptions). KB
# chunks are stored as EPISODIC memories carrying this tag, so they are pulled
# by consolidate()'s episodic scan and MUST be exempted below — otherwise an
# aged, never-reinforced chunk (recall via score_by_similarity skips the
# retrieval boost, so its strength sits at -10.0) is consolidated away and
# forgotten, silently dropping it out of KB search.
KNOWLEDGE_OFFICIAL_TAG = "knowledge:official"

# Umbrella tag on every agent-memory-passthrough projection record (unified
# memory passthrough, 2026-07-16). These records back a frontier agent's local
# memory dir and are recalled by similarity (score_by_similarity=True skips the
# retrieval boost), so they are NEVER reinforced — identical decay hazard to KB
# chunks: an aged, never-boosted record sinks below the consolidation threshold
# and, once past the 7-day age gate, would be archived + vector-deleted. So the
# tag earns the SAME protection knowledge:official gets, at all three sites
# (consolidation exclusion below, purge exclusion via PURGE_PROTECTED_TAGS, and
# the recall-boost skip that is an intentional no-op). All three move together.
AGENT_MEMORY_TAG = "agent_memory"

# Memories with these tags should never be consolidated into semantic summaries.
CONSOLIDATION_EXCLUDED_TAGS: set[str] = {
    "tool_use",
    "identity",
    "conversation",
    KNOWLEDGE_OFFICIAL_TAG,
    AGENT_MEMORY_TAG,
}

# Memories with these tags survive a purge (`_delete_all_memory_records` /
# `_get_all_memory_ids` when exclude_protected=True). knowledge:official chunks
# are reinstalled by the knowledge plugin and must persist; agent_memory records
# are the canonical projection-backing store and purging them would be a
# catastrophic forget. Distinct from CONSOLIDATION_EXCLUDED_TAGS: identity /
# tool_use / conversation are consolidation-exempt but still purgeable (identity
# is reseeded after a purge).
PURGE_PROTECTED_TAGS: frozenset[str] = frozenset({KNOWLEDGE_OFFICIAL_TAG, AGENT_MEMORY_TAG})

# Max orphaned memories reindexed per reconcile pass (was the raw query's
# `LIMIT 1000`). The reconcile is idempotent: reindexed memories gain an active
# vector and drop out of the next pass, so the backlog drains over repeated runs.
_REINDEX_BATCH_LIMIT: Final[int] = 1000


def _validate_vector_match(match: dict[str, Any]) -> tuple[str, float]:
    """Extract and validate memory_id and similarity from a vector match."""
    memory_id = match.get("external_id")
    if not memory_id:
        raise FrameworkError(
            message=f"Vector match missing 'external_id': {match.keys()}",
            error_code="memory.vector_error",
        )
    distance = match.get("distance")
    if distance is None:
        raise FrameworkError(
            message=f"Vector match missing 'distance': {match.keys()}",
            error_code="memory.vector_error",
        )
    return str(memory_id), 1.0 - float(distance)


def _memory_passes_tag_filter(
    memory: dict[str, Any],
    required_tags: list[str] | None,
    exclude_tag_set: frozenset[str],
) -> bool:
    """Return True if the memory satisfies inclusion/exclusion tag constraints."""
    memory_tags: list[str] = memory.get("tags", [])
    if required_tags and not all(t in memory_tags for t in required_tags):
        return False
    if exclude_tag_set and exclude_tag_set.intersection(memory_tags):
        return False
    return True


def _compute_scores(
    memory: dict[str, Any],
    similarity: float,
    now: datetime,
    score_by_similarity: bool,
) -> tuple[float, float]:
    """Compute strength and final score for a recalled memory."""
    if score_by_similarity:
        return 0.0, similarity
    strength = compute_strength(memory, now)
    return strength, compute_final_score(similarity, strength)


def _build_recall_result(
    memory: dict[str, Any],
    strength: float,
    similarity: float,
    final_score: float,
) -> dict[str, Any]:
    """Build a recall result dict from a memory record."""
    return {
        "id": memory["id"],
        "content": memory["content"],
        "memory_type": memory.get("memory_type", "episodic"),
        "strength": strength,
        "similarity": similarity,
        "final_score": final_score,
        "created_at": memory.get("created_at", ""),
        "retrieval_count": memory.get("retrieval_count", 0),
        "tags": memory.get("tags", []),
    }


class ACTRMemoryBackend:
    """Backend implementation for ACT-R memory system.

    Provides:
    - Short-term session memory (conversation history)
    - Long-term ACT-R memory (knowledge with decay and memorization)

    The ACT-R memory system uses:
    - Vector embeddings for semantic search
    - Base-level activation for memory strength
    - Spaced repetition for intentional memorization
    - Automatic consolidation of weak memories
    """

    def __init__(
        self,
        state_service: Any,
        vector_service: Any | None = None,
        embedding_service: Any | None = None,
        inference_service: Any | None = None,
        export_allowed_roots: list[str] | None = None,
    ) -> None:
        """Initialize ACTRMemoryBackend.

        Args:
            state_service: State service for database operations (required)
            vector_service: Vector service for embeddings (required for ACT-R)
            embedding_service: Embedding service for generating vectors (required for ACT-R)
            inference_service: Inference service for consolidation summaries (optional)
            export_allowed_roots: Operator-configured workspace roots that
                export_memories / import_memories file paths must be contained
                under. Empty (the default) REFUSES every export/import.
        """
        self.state_service: Any = state_service
        self.vector_service: Any = vector_service
        self.embedding_service: Any = embedding_service
        self.inference_service: Any = inference_service
        self._export_allowed_roots: list[str] = export_allowed_roots or []

        # Short-term memory namespace
        self.namespace = "core"

        # Default configuration (can be overridden)
        self.default_max_events = 20
        self.default_max_age_hours = 24

        # ACT-R memory configuration
        self.actr_enabled = vector_service is not None and embedding_service is not None

    def store_interaction(
        self,
        session_id: str,
        source_namespace: str,
        event_type: str,
        content: str,
        metadata: dict[str, Any] | None = None,
        timestamp: str | None = None,
    ) -> dict[str, Any]:
        """Store an interaction event in memory.

        Args:
            session_id: Session identifier
            source_namespace: Plugin namespace that sent this event
            event_type: Type of event ('user_input', 'assistant_response', 'system_message')
            content: Event content (message text)
            metadata: Optional metadata dict (will be JSON-serialized)
            timestamp: Optional ISO8601 timestamp (auto-generated if None)

        Returns:
            ActionResult dict with status and event_id
        """
        if not session_id:
            raise FrameworkError(
                message="session_id is required",
                error_code="memory.validation_error",
            )

        if event_type not in ("user_input", "assistant_response", "system_message"):
            raise FrameworkError(
                message=f"Invalid event_type: {event_type}",
                error_code="memory.validation_error",
            )

        # Generate event ID and timestamp
        event_id = str(uuid.uuid4())
        if timestamp is None:
            timestamp = datetime.now(UTC).isoformat()

        # Serialize metadata if provided
        metadata_json = json.dumps(metadata) if metadata else None

        # Store in database
        result = self.state_service.write_state(
            namespace=self.namespace,
            data={
                "table": "memory_events",
                "records": [
                    {
                        "id": event_id,
                        "session_id": session_id,
                        "source_namespace": source_namespace,
                        "event_type": event_type,
                        "content": content,
                        "metadata": metadata_json,
                        "timestamp": timestamp,
                    }
                ],
            },
        )

        if result is None:
            raise FrameworkError(
                message="State service returned None - table may not exist or write failed",
                error_code="memory.storage_error",
            )

        if result.get("action_status") == ActionStatus.COMPLETED.value:
            return {
                "action_status": ActionStatus.COMPLETED.value,
                "data": {"event_id": event_id},
            }
        else:
            error_msg = result.get("error", "Unknown error storing memory event")
            raise FrameworkError(
                message=f"Failed to store memory event: {error_msg}",
                error_code="memory.storage_error",
            )

    def get_recent_memory(
        self,
        session_id: str | None = None,
        max_events: int = 20,
        max_age_hours: int | None = None,
        namespace_filter: str | None = None,
    ) -> dict[str, Any]:
        """Retrieve recent memory formatted for LLM context.

        Memories are stored with session context but retrieved globally.
        Session boundaries are interface channels, not memory boundaries.

        Args:
            session_id: Optional session filter (usually not needed - memories are global)
            max_events: Maximum number of events to retrieve (SQL LIMIT)
            max_age_hours: Optional time window in hours (SQL WHERE)
            namespace_filter: Optional plugin namespace filter (SQL WHERE)

        Returns:
            Dict with formatted history string and event count.
        """
        records = self.get_recent_memory_structured(
            session_id=session_id,
            max_events=max_events,
            max_age_hours=max_age_hours,
            namespace_filter=namespace_filter,
        )

        if not records:
            return {"history": "", "event_count": 0}

        formatted_lines = [format_memory_record(record) for record in records]
        return {"history": "\n".join(formatted_lines), "event_count": len(records)}

    def get_recent_memory_structured(
        self,
        session_id: str | None = None,
        max_events: int = 20,
        max_age_hours: int | None = None,
        namespace_filter: str | None = None,
    ) -> list[dict[str, Any]]:
        """Retrieve recent memory as structured data.

        Memories are global - session_id is optional filter, not required.
        """
        query_data = build_memory_query(
            session_id, max_events, max_age_hours, namespace_filter
        )
        result = self.state_service.query_ordered(namespace="core", data=query_data)

        if result.get("action_status") != ActionStatus.COMPLETED.value:
            raise FrameworkError(
                message=f"Failed to retrieve memory: {result.get('error')}",
                error_code="memory.query_failed",
            )

        raw_records = cast(list[dict[str, Any]], result.get("data", {}).get("records", []))
        # Reverse for chronological display (query_ordered returns timestamp DESC).
        return list(reversed([convert_memory_record(r) for r in raw_records]))

    def clear_session_memory(
        self,
        session_id: str,
        namespace_filter: str | None = None,
    ) -> dict[str, Any]:
        """Clear memory for a session.

        Args:
            session_id: Session identifier
            namespace_filter: Optional - only clear events from specific namespace

        Returns:
            ActionResult dict with status and count of deleted events
        """
        if not session_id:
            raise FrameworkError(
                message="session_id is required",
                error_code="memory.validation_error",
            )

        # Hard delete (clear_session_memory is a wipe; the raw DELETE was a hard
        # delete, so soft_delete=False preserves semantics — a soft delete would
        # leave the events as is_deleted=1 rather than removing them).
        filters: dict[str, object] = {"session_id": session_id}
        if namespace_filter:
            filters["source_namespace"] = namespace_filter

        result = self.state_service.delete_records(
            namespace="core",
            query={"table": "memory_events", "filters": filters, "soft_delete": False},
        )

        if result.get("action_status") == ActionStatus.COMPLETED.value:
            inner = cast(dict[str, Any], result.get("data", {})).get("result", {})
            deleted = inner.get("deleted", 0) if isinstance(inner, dict) else 0
            return {
                "action_status": ActionStatus.COMPLETED.value,
                "data": {"deleted_count": deleted},
            }
        else:
            raise FrameworkError(
                message=result.get("error", "Unknown error clearing memory"),
                error_code="memory.storage_error",
            )

    def get_session_event_stats(
        self,
        session_id: str,
    ) -> dict[str, Any]:
        """Get conversation event statistics for a session (NOT long-term memories).

        Raises FrameworkError on database errors. Returns empty stats for sessions
        with no events (this is not an error condition).
        """
        if not session_id:
            raise FrameworkError(
                message="session_id is required",
                error_code="memory.validation_error",
            )

        summary = query_event_summary(self.state_service, session_id)
        by_namespace = query_namespace_breakdown(self.state_service, session_id)
        return {**summary, "by_namespace": by_namespace}

    # -------------------------------------------------------------------------
    # ACT-R MEMORY HELPERS
    # -------------------------------------------------------------------------

    def _generate_embedding(self, content: str) -> list[float]:
        """Generate embedding for content. Raises FrameworkError on failure."""
        if self.embedding_service is None:
            raise FrameworkError(
                message="Embedding service not available",
                error_code="memory.embedding_unavailable",
            )

        embedding_result = self.embedding_service.generate_embeddings(inputs=[content])

        # Check for error response from embedding service
        if embedding_result.get("action_status") == ActionStatus.ERROR.value:
            error_info = embedding_result.get("error", {})
            error_msg = (
                error_info.get("message", str(error_info))
                if isinstance(error_info, dict)
                else str(error_info)
            )
            raise FrameworkError(
                message=f"Embedding service error: {error_msg}",
                error_code="memory.embedding_error",
            )

        embedding: list[float] = (
            embedding_result.get("data", {}).get("result", {}).get("embeddings", [[]])[0]
        )
        if not embedding:
            embedding = embedding_result.get("data", {}).get("embeddings", [[]])[0]

        # Fail fast if embedding is empty
        if not embedding:
            raise FrameworkError(
                message=f"Embedding service returned empty embedding. Response: {embedding_result}",
                error_code="memory.embedding_empty",
            )

        return embedding

    # -------------------------------------------------------------------------
    # ACT-R CORE MEMORY OPERATIONS
    # -------------------------------------------------------------------------

    def remember(
        self,
        content: str,
        tags: list[str] | None = None,
        source_file: str | None = None,
        session_id: str | None = None,
        embed: bool = True,
    ) -> dict[str, Any]:
        """Store a new episodic memory.

        Args:
            content: Memory content text.
            tags: Optional tags for filtering.
            source_file: Optional source file reference.
            session_id: Optional session ID.
            embed: Whether to generate a vector embedding for this memory.
                Set to ``False`` for focus-only working-context mirrors
                that are delivered through the focus mechanism rather than
                semantic recall search.
        """
        if not self.actr_enabled:
            raise FrameworkError(
                message="ACT-R memory not enabled. Requires vector_service and embedding_service.",
                error_code="memory.actr_not_enabled",
            )
        # After check, services are guaranteed available
        assert self.embedding_service is not None
        assert self.vector_service is not None

        if not content or not content.strip():
            raise FrameworkError(
                message="Content is required",
                error_code="memory.validation_error",
            )

        normalized_tags = normalize_tags(tags)
        now = datetime.now(UTC)
        now_iso = now.isoformat()

        if embed and len(content) <= EMBEDDING_MAX_CHARS:
            # Fail-fast: generate embedding BEFORE creating any records so a
            # failed embedding never leaves behind a persisted memory with no vector.
            embedding = self._generate_embedding(content)
            memory = create_memory_record(
                content, normalized_tags, source_file, session_id, now_iso
            )
            memory_id = save_memory(self.state_service, memory)
            store_memory_vector(self.vector_service, memory_id, embedding, now_iso, normalized_tags)
        elif embed:
            # Content exceeds EMBEDDING_MAX_CHARS — store without vector
            # rather than sending a truncated blob to the embedding model.
            logger.warning(
                "EMBEDDING_SKIPPED: content length %d exceeds "
                "EMBEDDING_MAX_CHARS %d — storing memory without vector",
                len(content), EMBEDDING_MAX_CHARS,
            )
            memory = create_memory_record(
                content, normalized_tags, source_file, session_id, now_iso
            )
            memory_id = save_memory(self.state_service, memory)
        else:
            # Focus-only mirror: store the memory record without a vector.
            memory = create_memory_record(
                content, normalized_tags, source_file, session_id, now_iso
            )
            memory_id = save_memory(self.state_service, memory)

        return {
            "memory_id": memory_id,
            "message": f"Remembered: {content[:50]}{'...' if len(content) > 50 else ''}",
        }

    def recall(
        self,
        query: str,
        top_k: int = DEFAULT_TOP_K,
        memory_type: str = "all",
        include_archived: bool = False,
        tags: list[str] | None = None,
        exclude_ids: list[str] | None = None,
        score_by_similarity: bool = False,
        exclude_tags: list[str] | None = None,
    ) -> dict[str, Any]:
        """Search memories with strength-weighted ranking."""
        if not self.actr_enabled:
            raise FrameworkError(
                message="ACT-R memory not enabled. Requires vector_service and embedding_service.",
                error_code="memory.actr_not_enabled",
            )
        assert self.embedding_service is not None
        assert self.vector_service is not None

        if not query or not query.strip():
            raise FrameworkError(
                message="Query is required",
                error_code="memory.validation_error",
            )

        return self._execute_recall(
            query, top_k, memory_type, include_archived, tags, exclude_ids,
            score_by_similarity, exclude_tags,
        )

    def _execute_recall(
        self,
        query: str,
        top_k: int,
        memory_type: str,
        include_archived: bool,
        tags: list[str] | None,
        exclude_ids: list[str] | None,
        score_by_similarity: bool = False,
        exclude_tags: list[str] | None = None,
    ) -> dict[str, Any]:
        """Execute the recall search after validation."""
        now = datetime.now(UTC)
        exclude_set: set[str] = set(exclude_ids) if exclude_ids else set()
        exclude_tag_set: frozenset[str] = frozenset(exclude_tags) if exclude_tags else frozenset()

        query_embedding = self._generate_embedding(query)

        matches = self._get_recall_matches(
            query_embedding, top_k, memory_type, include_archived, tags, exclude_ids
        )

        results = self._process_recall_matches(
            matches, exclude_set, tags, now, score_by_similarity, exclude_tag_set,
        )
        results.sort(key=lambda r: r["final_score"], reverse=True)
        results = results[:top_k]

        if not score_by_similarity:
            self._record_retrieval_boost(results, now)

        return {"memories": results, "count": len(results)}

    def _get_recall_matches(
        self,
        query_embedding: list[float],
        top_k: int,
        memory_type: str,
        include_archived: bool,
        tags: list[str] | None,
        exclude_ids: list[str] | None,
    ) -> list[dict[str, Any]]:
        """Get vector matches for recall query."""
        filter_dict = build_recall_filters(memory_type, include_archived)
        has_extra_filters = bool(tags or exclude_ids)
        return self._search_recall_vectors(query_embedding, top_k, filter_dict, has_extra_filters)

    def _search_recall_vectors(
        self,
        query_embedding: list[float],
        top_k: int,
        filter_dict: dict[str, Any],
        has_extra_filters: bool,
    ) -> list[dict[str, Any]]:
        """Search vectors for recall with oversampling."""
        if self.vector_service is None:
            raise FrameworkError(
                message="Vector service not available",
                error_code="memory.vector_unavailable",
            )
        # When filtering by tags/exclusions, we need more results since filtering happens post-search
        # Increase oversampling significantly to ensure we find tagged memories
        oversample_factor = 50 if has_extra_filters else 3

        search_result = self.vector_service.search_similar(
            namespace=VECTOR_NAMESPACE,
            query_vector=query_embedding,
            top_k=top_k * oversample_factor,
            filters=filter_dict if filter_dict else None,
        )

        data = search_result.get("data")
        if data is None:
            raise FrameworkError(
                message=f"Vector search returned no data: {search_result}",
                error_code="memory.vector_error",
            )

        result = data.get("result")
        if result is None:
            return []
        results: list[dict[str, Any]] = result.get("results", [])
        return results

    def _process_recall_matches(
        self,
        matches: list[dict[str, Any]],
        exclude_set: set[str],
        tags: list[str] | None,
        now: datetime,
        score_by_similarity: bool = False,
        exclude_tag_set: frozenset[str] = frozenset(),
    ) -> list[dict[str, Any]]:
        """Process vector matches into scored memory results."""
        results: list[dict[str, Any]] = []
        for match in matches:
            memory_id, similarity = _validate_vector_match(match)
            if memory_id in exclude_set:
                continue

            memory = get_memory(self.state_service, memory_id)
            if not memory:
                logger.debug(f"Orphaned vector skipped (run cleanup_orphaned_vectors): {memory_id}")
                continue

            if not _memory_passes_tag_filter(memory, tags, exclude_tag_set):
                continue

            strength, final_score = _compute_scores(memory, similarity, now, score_by_similarity)
            results.append(
                _build_recall_result(memory, strength, similarity, final_score),
            )
        return results

    def _record_retrieval_boost(self, results: list[dict[str, Any]], now: datetime) -> None:
        """Record retrieval for top results to strengthen them."""
        now_iso = now.isoformat()
        for r in results[:RETRIEVAL_BOOST_TOP_N]:
            memory = get_memory(self.state_service, r["id"])
            if memory:
                memory["retrieval_times"] = memory.get("retrieval_times", []) + [now_iso]
                memory["retrieval_count"] = memory.get("retrieval_count", 0) + 1
                memory["strength"] = compute_strength(memory, now)
                save_memory(self.state_service, memory)

    def forget(self, memory_id: str) -> dict[str, Any]:
        """Archive a memory (soft delete)."""
        if not self.actr_enabled:
            raise FrameworkError(
                message="ACT-R memory not enabled. Requires vector_service and embedding_service.",
                error_code="memory.actr_not_enabled",
            )
        # After check, services are guaranteed available
        assert self.vector_service is not None

        memory = get_memory(self.state_service, memory_id)
        if not memory:
            raise FrameworkError(
                message=f"Memory not found: {memory_id}",
                error_code="memory.not_found",
            )

        memory["status"] = "archived"
        save_memory(self.state_service, memory)

        # Delete the embedding (memory_id is the vector's external_id). The memory
        # row is already archived above and PERSISTS (archive != hard-delete), so
        # the ordering is memory-first by design — but fail-loud, do NOT swallow:
        # a swallowed vector-delete leaves an archived-but-still-searchable memory
        # (a silent fast-fail violation). (delete_by_external_ids returns COMPLETED
        # for an already-absent external_id, so the old "vector may not exist"
        # case is not an error.)
        delete_result = self.vector_service.delete_by_external_ids(
            namespace=VECTOR_NAMESPACE,
            external_ids=[memory_id],
        )
        if delete_result.get("action_status") != ActionStatus.COMPLETED.value:
            raise FrameworkError(
                message=f"Failed to delete embedding for archived {memory_id}: {delete_result.get('error')}",
                error_code="memory.embedding_delete_failed",
            )

        return {"message": f"Archived memory: {memory_id}"}

    def delete_memory(self, memory_id: str) -> dict[str, Any]:
        """Hard delete a memory from the database.

        Unlike forget() which archives, this permanently removes the memory.
        Use for cleanup of transient memories like identity seeds that are
        recreated on startup.

        Args:
            memory_id: ID of the memory to delete

        Returns:
            Dict with message on success, or error on failure
        """
        if not self.actr_enabled:
            raise FrameworkError(
                message="ACT-R memory not enabled. Requires vector_service and embedding_service.",
                error_code="memory.actr_not_enabled",
            )
        assert self.vector_service is not None

        memory = get_memory(self.state_service, memory_id)
        if not memory:
            raise FrameworkError(
                message=f"Memory not found: {memory_id}",
                error_code="memory.not_found",
            )

        # Cascade VECTOR-FIRST (Architect RECIPROCAL ruling, GAP-5): delete the
        # embedding BEFORE the memory record. The two are not cross-service
        # atomic, so a crash between them must leave at most an orphan-MEMORY
        # (memory present, vector gone) — reconcilable SQL-free via
        # reindex_orphaned_vectors — NEVER an orphan-VECTOR. Fail-loud, do NOT
        # swallow: a swallowed vector-delete failure silently reintroduces the
        # orphan-vector this ordering exists to design out. (delete_by_external_ids
        # returns COMPLETED for an already-absent external_id, so the old
        # "vector may not exist" case is not an error.)
        delete_result = self.vector_service.delete_by_external_ids(
            namespace=VECTOR_NAMESPACE,
            external_ids=[memory_id],
        )
        if delete_result.get("action_status") != ActionStatus.COMPLETED.value:
            raise FrameworkError(
                message=f"Failed to delete embedding for {memory_id}: {delete_result.get('error')}",
                error_code="memory.embedding_delete_failed",
            )

        # Hard-delete from database (soft_delete=False — actr deletes are hard;
        # the docstring's "permanently removes" was previously not honored
        # because delete_records defaults to soft).
        self.state_service.delete_records(
            namespace=NAMESPACE,
            query={"table": "memory", "filters": {"id": memory_id}, "soft_delete": False},
        )

        return {"message": f"Deleted memory: {memory_id}"}

    def delete_memories_by_tag(self, tag: str) -> dict[str, Any]:
        """Delete all memories with the given tag and their embeddings. Fails fast."""
        if not self.actr_enabled:
            raise FrameworkError(
                message="Cannot delete memories: ACT-R memory not enabled",
                error_code="memory.actr_not_enabled",
            )
        assert self.vector_service is not None

        # 1. Find all memory IDs with this tag
        memories = find_memories_by_tag(self.state_service, tag)
        if not memories:
            return {
                "action_status": ActionStatus.COMPLETED.value,
                "data": {"deleted_count": 0, "message": f"No memories with tag '{tag}'"},
            }

        memory_ids = [m["id"] for m in memories]

        # 2. Delete embeddings using VectorService API (memory_id = external_id)
        delete_result = self.vector_service.delete_by_external_ids(
            namespace=VECTOR_NAMESPACE,
            external_ids=memory_ids,
        )

        if delete_result.get("action_status") != ActionStatus.COMPLETED.value:
            raise FrameworkError(
                message=f"Failed to delete embeddings: {delete_result.get('error')}",
                error_code="memory.embedding_delete_failed",
            )

        # 3. Delete memory records
        deleted_count = delete_memory_records(self.state_service, memory_ids)

        logger.debug(f"Deleted {deleted_count} memories with tag '{tag}'")
        return {
            "action_status": ActionStatus.COMPLETED.value,
            "data": {"deleted_count": deleted_count},
        }

    def delete_memories_by_ids(self, ids: list[str]) -> dict[str, Any]:
        """Hard-delete the given memories by id and their embeddings. Fails fast.

        The id-list counterpart of ``delete_memories_by_tag`` for callers that
        already hold the exact ids (the knowledge-base per-file and sweep delete
        paths resolve ids themselves, so a tag round-trip would be redundant).
        Same cascade VECTOR-FIRST ordering (Architect ruling): delete the
        embeddings BEFORE the memory records, so a crash mid-cascade leaves at
        most an orphan-MEMORY (reconcilable SQL-free via
        ``reindex_orphaned_vectors``), never an orphan-VECTOR. ``deleted_count``
        is the number of ids acted on (mirrors ``delete_memory_records``; absent
        ids are a no-op delete that the vector + record paths both tolerate).
        """
        if not self.actr_enabled:
            raise FrameworkError(
                message="Cannot delete memories: ACT-R memory not enabled",
                error_code="memory.actr_not_enabled",
            )
        assert self.vector_service is not None

        if not ids:
            return {
                "action_status": ActionStatus.COMPLETED.value,
                "data": {"deleted_count": 0, "message": "No memory ids provided"},
            }

        # 1. Cascade VECTOR-FIRST: delete embeddings (memory_id = external_id).
        delete_result = self.vector_service.delete_by_external_ids(
            namespace=VECTOR_NAMESPACE,
            external_ids=ids,
        )
        if delete_result.get("action_status") != ActionStatus.COMPLETED.value:
            raise FrameworkError(
                message=f"Failed to delete embeddings: {delete_result.get('error')}",
                error_code="memory.embedding_delete_failed",
            )

        # 2. Hard-delete the memory records.
        deleted_count = delete_memory_records(self.state_service, ids)

        logger.debug(f"Deleted {deleted_count} memories by id")
        return {
            "action_status": ActionStatus.COMPLETED.value,
            "data": {"deleted_count": deleted_count},
        }

    def upsert_memory_by_tag(
        self,
        content: str,
        tag: str,
        tags: list[str] | None = None,
        session_id: str | None = None,
    ) -> dict[str, Any]:
        """Replace-by-tag: store new content, then delete previous memories with this tag.

        The slot ``tag`` stays the replace key — every prior record carrying it
        is deleted. ``tags`` are provenance / umbrella tags unioned onto the
        stored record so they land on the memory too: the slot tag is always
        first and always present, duplicates are removed, and order is preserved.
        Tag matching is exact membership, so a projection-backing record must
        carry the ``agent_memory`` umbrella tag literally — that is what earns it
        the consolidation/purge protection (PURGE_PROTECTED_TAGS /
        CONSOLIDATION_EXCLUDED_TAGS).

        Uses remember-first ordering so a failed write leaves the old content
        intact (no data-loss window). Duplicate-slot reconciliation falls out of
        that ordering: because ALL prior records on the slot are snapshotted and
        deleted, a slot that had accumulated duplicates (a prior upsert whose
        old-vector delete failed leaves >1 record) collapses to the single new
        record — newest wins, stale repaired and reported. The
        store-new-then-delete-old window is accepted; there is no CAS.
        """
        # 1. Snapshot existing memory IDs with this tag (before writing)
        existing = find_memories_by_tag(self.state_service, tag)
        existing_ids = [m["id"] for m in existing]

        # 2. Store new content first — if this fails, old content is preserved.
        #    Union the slot tag with the provenance tags (slot first, deduped).
        extra_tags: list[str] = tags if tags is not None else []
        record_tags: list[str] = list(dict.fromkeys([tag, *extra_tags]))
        remember_result = self.remember(
            content=content, tags=record_tags, session_id=session_id
        )
        new_memory_id = remember_result.get("memory_id", "")

        # 3. Delete every previously-existing record on this slot (not the new one).
        deleted_count = 0
        if existing_ids:
            assert self.vector_service is not None
            delete_result = self.vector_service.delete_by_external_ids(
                namespace=VECTOR_NAMESPACE,
                external_ids=existing_ids,
            )
            if delete_result.get("action_status") == ActionStatus.COMPLETED.value:
                deleted_count = delete_memory_records(self.state_service, existing_ids)
            else:
                logger.warning(
                    f"Failed to delete embeddings for old memories with tag '{tag}': "
                    f"{delete_result.get('error')}"
                )

        # A healthy slot holds 0 or 1 prior record; deleting >1 means we just
        # repaired stale duplicates left by an earlier failed old-vector delete.
        duplicates_reconciled = deleted_count - 1 if deleted_count > 1 else 0
        if duplicates_reconciled:
            logger.warning(
                f"Reconciled {duplicates_reconciled} duplicate record(s) on slot "
                f"'{tag}' during upsert (newest wins, stale deleted)"
            )

        logger.debug(
            f"Upserted memory for tag '{tag}': deleted {deleted_count}, "
            f"new memory_id={new_memory_id}"
        )
        return {
            "action_status": ActionStatus.COMPLETED.value,
            "data": {
                "deleted_count": deleted_count,
                "duplicates_reconciled": duplicates_reconciled,
                "memory_id": new_memory_id,
                "tag": tag,
                "message": f"Upserted memory for tag '{tag}' (deleted {deleted_count} existing)",
            },
        }

    def get_memories_by_tag(self, tag: str, include_archived: bool = False) -> dict[str, Any]:
        """Get all memories with a specific tag.

        Direct tag-based lookup without semantic search. Useful for verification
        and retrieval of tagged memory sets (like identity memories).

        Args:
            tag: Tag to match
            include_archived: Whether to include archived memories

        Returns:
            Dict with memories list and count, or error on failure
        """
        if not self.actr_enabled:
            raise FrameworkError(
                message="ACT-R memory not enabled. Requires vector_service and embedding_service.",
                error_code="memory.actr_not_enabled",
            )

        status = None if include_archived else "active"
        memories = get_all_memories(self.state_service, status=status, tag=tag)

        # Parse JSON fields for each memory
        for memory in memories:
            parse_memory_json_fields(memory)

        return {"memories": memories, "count": len(memories)}

    def reinforce(self, memory_id: str) -> dict[str, Any]:
        """Reinforce a memory by adding a retrieval timestamp.

        This explicitly strengthens the memory's ACT-R activation, making it
        more likely to surface in future recalls. Use this when surfacing
        memories in context to ensure useful memories stay strong.

        Args:
            memory_id: ID of the memory to reinforce

        Returns:
            Dict with memory_id, new_strength, and retrieval_count
        """
        if not self.actr_enabled:
            raise FrameworkError(
                message="ACT-R memory not enabled. Requires vector_service and embedding_service.",
                error_code="memory.actr_not_enabled",
            )

        memory = get_memory(self.state_service, memory_id)
        if not memory:
            raise FrameworkError(
                message=f"Memory not found: {memory_id}",
                error_code="memory.not_found",
            )

        if memory.get("status") == "archived":
            raise FrameworkError(
                message=f"Cannot reinforce archived memory: {memory_id}",
                error_code="memory.archived",
            )

        now = datetime.now(UTC)
        now_iso = now.isoformat()

        # Add retrieval timestamp (this is the reinforcement mechanism)
        memory["retrieval_times"] = memory.get("retrieval_times", []) + [now_iso]
        memory["retrieval_count"] = memory.get("retrieval_count", 0) + 1
        memory["strength"] = compute_strength(memory, now)

        save_memory(self.state_service, memory)

        return {
            "memory_id": memory_id,
            "new_strength": memory["strength"],
            "retrieval_count": memory["retrieval_count"],
        }

    # -------------------------------------------------------------------------
    # FOCUS BUFFER
    # -------------------------------------------------------------------------

    # Per-session safety valve only; the real constraint is
    # FOCUS_BUDGET_FRACTION at prompt assembly (JOS-02 §4 — deliberately the
    # pre-scoping value, now applied per session).
    MAX_FOCUSED = 20

    @staticmethod
    def _require_session_id(session_id: str) -> str:
        """Focus operations are session-scoped (JOS-02); no session, no scope."""
        if not session_id:
            raise FrameworkError(
                message="focus-buffer operation requires a non-empty session_id",
                error_code="memory.session_required",
            )
        return session_id

    def focus(self, memory_id: str, *, session_id: str) -> dict[str, Any]:
        """Pin a memory into the acting session's focus buffer.

        Capacity is the per-session MAX_FOCUSED safety valve; a memory can be
        pinned in at most one session (memory_id is globally unique).
        """
        self._require_session_id(session_id)
        memory = get_memory(self.state_service, memory_id)
        if not memory:
            raise FrameworkError(
                message=f"Memory not found: {memory_id}",
                error_code="memory.not_found",
            )

        if memory.get("status") == "archived":
            raise FrameworkError(
                message=f"Cannot focus archived memory: {memory_id}",
                error_code="memory.archived",
            )

        # Already focused anywhere? memory_id is globally UNIQUE — a pin in
        # another session must surface as a typed error, not a silent insert
        # failure (JOS-02 F3 discussion).
        existing = self.state_service.read_state(
            namespace=NAMESPACE,
            query={"table": "focus_buffer", "filters": {"memory_id": memory_id}},
        )
        existing_rows = existing.get("data", {}).get("records", [])
        if existing_rows:
            holder = existing_rows[0].get("session_id", "")
            raise FrameworkError(
                message=(
                    f"Memory already focused: {memory_id} "
                    f"(held by session {holder!r})"
                ),
                error_code="memory.already_focused",
            )

        # Per-session capacity
        session_focused = self.state_service.read_state(
            namespace=NAMESPACE,
            query={
                "table": "focus_buffer",
                "filters": {"session_id": session_id},
            },
        )
        focused_rows = session_focused.get("data", {}).get("records", [])
        if len(focused_rows) >= self.MAX_FOCUSED:
            raise FrameworkError(
                message=(
                    f"Focus buffer full for session {session_id!r} "
                    f"({self.MAX_FOCUSED} max). Unfocus a memory first."
                ),
                error_code="memory.focus_buffer_full",
            )

        result = self.state_service.write_state(
            namespace=NAMESPACE,
            data={
                "table": "focus_buffer",
                "record": {"memory_id": memory_id, "session_id": session_id},
            },
        )
        if result.get("action_status") != ActionStatus.COMPLETED.value:
            raise FrameworkError(
                message=f"Failed to focus memory: {result.get('error', 'unknown')}",
                error_code="memory.storage_error",
            )

        return {
            "memory_id": memory_id,
            "session_id": session_id,
            "focused_count": len(focused_rows) + 1,
        }

    def unfocus(self, memory_id: str, *, session_id: str) -> dict[str, Any]:
        """Remove a memory from the acting session's focus buffer.

        The delete is predicated on the session — a session can only unpin
        its own pins (JOS-02 §4).
        """
        self._require_session_id(session_id)
        existing = self.state_service.read_state(
            namespace=NAMESPACE,
            query={
                "table": "focus_buffer",
                "filters": {"memory_id": memory_id, "session_id": session_id},
            },
        )
        existing_rows = existing.get("data", {}).get("records", [])
        if not existing_rows:
            raise FrameworkError(
                message=(
                    f"Memory not in focus buffer for session "
                    f"{session_id!r}: {memory_id}"
                ),
                error_code="memory.not_focused",
            )

        record_id = existing_rows[0].get("id")
        self.state_service.delete_records(
            namespace=NAMESPACE,
            query={"table": "focus_buffer", "filters": {"id": record_id}, "soft_delete": False},
        )

        return {
            "memory_id": memory_id,
            "session_id": session_id,
            "message": f"Unfocused memory: {memory_id}",
        }

    def unfocus_all_for_session(self, *, session_id: str) -> dict[str, Any]:
        """Release EVERY pin held by one session (JOS-02 R1 terminal release).

        Whole-buffer release for ephemeral run sessions — plan, WBS document,
        artifact pins alike. Idempotent: an empty buffer is a benign no-op.
        """
        self._require_session_id(session_id)
        existing = self.state_service.read_state(
            namespace=NAMESPACE,
            query={
                "table": "focus_buffer",
                "filters": {"session_id": session_id},
            },
        )
        rows = existing.get("data", {}).get("records", [])
        released: list[str] = []
        for row in rows:
            record_id = row.get("id")
            if record_id:
                self.state_service.delete_records(
                    namespace=NAMESPACE,
                    query={
                        "table": "focus_buffer",
                        "filters": {"id": record_id},
                        "soft_delete": False,
                    },
                )
                released.append(str(row.get("memory_id", "")))
        return {
            "session_id": session_id,
            "released_memory_ids": released,
            "count": len(released),
        }

    def assert_no_unscoped_focus_rows(self) -> None:
        """JOS-02 F3 boot invariant: NULL-session focus rows are a hard error.

        A legacy (pre-JOS-02) pin is invisible to every session-filtered read
        yet permanently blocks re-pinning its memory anywhere (memory_id is
        globally unique and the already-focused check is by memory_id alone).
        Raising at readiness with the migration pointer is the ONLY loud
        surface such a row can have.
        """
        # This previously read every row in focus_buffer and re-tested
        # `session_id` in Python. The predicates it needs already exist in the
        # filter grammar, so the read is now SELECTIVE rather than merely
        # bounded: in the healthy case it matches zero rows, and the cost stops
        # scaling with how many memories are pinned.
        #
        # TWO reads, deliberately. The Python test being replaced was `not
        # r.get("session_id")`, which is true for NULL *and* for the empty
        # string — and the JOS-02 migration this invariant guards uses that same
        # falsy test to decide what to delete. SQL `IS NULL` does not match '',
        # and the flat filter grammar has no OR, so expressing only the NULL half
        # would have quietly narrowed a boot invariant to a subset of what its
        # own migration cleans. A row with session_id='' would then pass
        # readiness while still being unre-pinnable — the exact hazard this
        # invariant exists to catch.
        rows: list[dict[str, Any]] = []
        for predicate in ({"op": "is_null"}, ""):
            result = self.state_service.read_state(
                namespace=NAMESPACE,
                query={
                    "table": "focus_buffer",
                    "filters": {"session_id": predicate},
                    "limit": _UNSCOPED_FOCUS_CEILING,
                },
            )
            matched = result.get("data", {}).get("records", [])
            rows.extend(
                assert_within_ceiling(
                    matched,
                    table="focus_buffer",
                    ceiling=_UNSCOPED_FOCUS_CEILING,
                    reason=(
                        "these are pre-JOS-02 legacy pins with no session_id; the "
                        "migration clears them and nothing creates new ones, so the "
                        "expected count is zero and any large number means the "
                        "migration never ran."
                    ),
                )
            )
        unscoped = [str(r.get("memory_id", "?")) for r in rows]
        if unscoped:
            raise FrameworkError(
                message=(
                    f"focus_buffer holds {len(unscoped)} pre-JOS-02 row(s) with "
                    f"no session_id (memory_ids: {unscoped}). Run "
                    f"plugins/actr_memory_plugin/migrations/"
                    f"2026_07_07_jos02_clear_unscoped_focus_pins.py before boot."
                ),
                error_code="memory.unscoped_focus_rows",
            )

    def get_focused(self, *, session_id: str) -> list[dict[str, Any]]:
        """Return the acting session's focused memories with full content."""
        self._require_session_id(session_id)
        result = self.state_service.read_state(
            namespace=NAMESPACE,
            query={
                "table": "focus_buffer",
                "filters": {"session_id": session_id},
            },
        )
        focused_rows = result.get("data", {}).get("records", [])
        if not focused_rows:
            return []

        memories: list[dict[str, Any]] = []
        for row in focused_rows:
            mem_id = row.get("memory_id", "")
            memory = get_memory(self.state_service, mem_id)
            if memory:
                # Ensure memory_id field is present (canonical key for focus buffer consumers)
                memory["memory_id"] = str(memory.get("id", mem_id))
                memories.append(memory)
            else:
                # Orphaned focus entry — memory was deleted. Hard delete to free slot.
                record_id = row.get("id")
                if record_id:
                    self.state_service.delete_records(
                        namespace=NAMESPACE,
                        query={"table": "focus_buffer", "filters": {"id": record_id}, "soft_delete": False},
                    )
                logger.debug(f"Cleaned orphaned focus buffer entry for memory: {mem_id}")

        return memories

    # -------------------------------------------------------------------------
    # ACT-R MEMORIZATION (SPACED REPETITION)
    # -------------------------------------------------------------------------

    def memorize(
        self,
        memory_id: str | None = None,
        content: str | None = None,
    ) -> dict[str, Any]:
        """Add a memory to the memorization queue."""
        if not self.actr_enabled:
            raise FrameworkError(
                message="ACT-R memory not enabled. Requires vector_service and embedding_service.",
                error_code="memory.actr_not_enabled",
            )

        if not memory_id and not content:
            raise FrameworkError(
                message="Must provide either memory_id or content",
                error_code="memory.validation_error",
            )

        resolved_memory_id = self._resolve_memory_id(memory_id, content)

        existing_result = self._check_existing_memorization(resolved_memory_id)
        if existing_result:
            return existing_result

        return self._create_memorization(resolved_memory_id)

    def _resolve_memory_id(self, memory_id: str | None, content: str | None) -> str:
        """Resolve or create memory_id from content."""
        if content and not memory_id:
            result = self.remember(content, tags=["memorizing"])
            return str(result["memory_id"])

        assert memory_id is not None
        memory = get_memory(self.state_service, memory_id)
        if not memory:
            raise FrameworkError(
                message=f"Memory not found: {memory_id}",
                error_code="memory.not_found",
            )

        return memory_id

    def _check_existing_memorization(self, memory_id: str) -> dict[str, Any] | None:
        """Check if memory is already being memorized."""
        existing = get_memorization(self.state_service, memory_id)
        if existing and existing.get("status") == "active":
            return {
                "memory_id": memory_id,
                "next_review_at": existing.get("next_review_at", ""),
                "message": "Already memorizing this memory",
            }
        return None

    def _create_memorization(self, memory_id: str) -> dict[str, Any]:
        """Create a new memorization record."""
        now = datetime.now(UTC)
        next_review = now + timedelta(days=INITIAL_INTERVAL_DAYS)

        memorization: dict[str, Any] = {
            "id": str(uuid.uuid4()),
            "memory_id": memory_id,
            "started_at": now.isoformat(),
            "review_count": 0,
            "last_review_at": None,
            "next_review_at": next_review.isoformat(),
            "interval_days": INITIAL_INTERVAL_DAYS,
            "status": "active",
        }

        save_memorization(self.state_service, memorization)

        return {
            "memory_id": memory_id,
            "next_review_at": next_review.isoformat(),
            "message": f"Started memorizing. First review in {INITIAL_INTERVAL_DAYS} day(s).",
        }

    def stop_memorizing(self, memory_id: str) -> dict[str, Any]:
        """Remove a memory from memorization queue."""
        memorization = get_memorization(self.state_service, memory_id)
        if not memorization:
            raise FrameworkError(
                message=f"Not memorizing memory: {memory_id}",
                error_code="memory.not_memorizing",
            )

        memorization["status"] = "paused"
        save_memorization(self.state_service, memorization)

        return {"message": f"Stopped memorizing memory: {memory_id}"}

    def list_memorizing(self, include_completed: bool = False) -> dict[str, Any]:
        """List all memories being memorized."""
        now = datetime.now(UTC)
        records = get_all_memorizations(self.state_service)

        if not include_completed:
            records = [r for r in records if r.get("status") == "active"]

        enriched = [self._enrich_memorization_record(r, now) for r in records]
        enriched.sort(key=lambda r: r.get("next_review_at", ""))

        return {
            "total": len(enriched),
            "due_now": sum(1 for r in enriched if r.get("is_due")),
            "memorizations": enriched,
        }

    def _enrich_memorization_record(self, record: dict[str, Any], now: datetime) -> dict[str, Any]:
        """Enrich a memorization record with memory content and due status."""
        memory = get_memory(self.state_service, record["memory_id"])
        next_review_str = record.get("next_review_at", "")
        due = is_review_due(next_review_str, now)

        return {
            "memory_id": record["memory_id"],
            "content_preview": (memory["content"][:100] + "..." if memory else "[deleted]"),
            "review_count": record.get("review_count", 0),
            "interval_days": record.get("interval_days", 0),
            "next_review_at": next_review_str,
            "is_due": due,
            "status": record.get("status", ""),
        }

    def process_memorization_queue(self) -> dict[str, Any]:
        """Process all due memorization reviews."""
        now = datetime.now(UTC)
        now_iso = now.isoformat()

        records = get_all_memorizations(self.state_service, status="active")

        processed = 0
        completed = 0

        for record in records:
            next_review_str = record.get("next_review_at", "")
            if not next_review_str:
                continue

            try:
                next_review = datetime.fromisoformat(next_review_str.replace("Z", "+00:00"))
                if next_review.tzinfo is None:
                    next_review = next_review.replace(tzinfo=UTC)
            except ValueError:
                continue

            if next_review > now:
                continue  # Not due yet

            memory_id = record["memory_id"]
            memory = get_memory(self.state_service, memory_id)

            if not memory:
                record["status"] = "orphaned"
                save_memorization(self.state_service, record)
                continue

            # "Retrieve" the memory - this is the reinforcement
            memory["retrieval_times"] = memory.get("retrieval_times", []) + [now_iso]
            memory["retrieval_count"] = memory.get("retrieval_count", 0) + 1
            memory["strength"] = compute_strength(memory, now)
            save_memory(self.state_service, memory)

            # Update memorization record
            record["review_count"] = record.get("review_count", 0) + 1
            record["last_review_at"] = now_iso

            if record["review_count"] >= MEMORIZATION_COMPLETE_REVIEWS:
                record["status"] = "completed"
                completed += 1
            else:
                new_interval = min(
                    record.get("interval_days", INITIAL_INTERVAL_DAYS) * INTERVAL_MULTIPLIER,
                    MAX_INTERVAL_DAYS,
                )
                record["interval_days"] = new_interval
                record["next_review_at"] = (now + timedelta(days=new_interval)).isoformat()

            save_memorization(self.state_service, record)
            processed += 1

        return {
            "processed": processed,
            "completed": completed,
            "message": f"Processed {processed} reviews, {completed} completed memorization",
        }

    # -------------------------------------------------------------------------
    # ACT-R LEARNING / INGESTION
    # -------------------------------------------------------------------------

    def learn(
        self,
        path: str,
        pattern: str = "*.md",
        recursive: bool = True,
        memorize: bool = False,
        tags: list[str] | None = None,
    ) -> dict[str, Any]:
        """Ingest knowledge from files, optionally memorizing it all."""
        if not self.actr_enabled:
            raise FrameworkError(
                message="ACT-R memory not enabled. Requires vector_service and embedding_service.",
                error_code="memory.actr_not_enabled",
            )

        file_path = Path(path).expanduser()
        if not file_path.exists():
            raise FrameworkError(
                message=f"Path not found: {path}",
                error_code="memory.path_not_found",
            )

        tags = tags or []
        memory_ids, skipped_count = self._ingest_path(file_path, pattern, recursive, tags)
        memorized_count = self._memorize_all(memory_ids) if memorize else 0

        return build_learn_result(path, memory_ids, skipped_count, memorized_count, memorize)

    def _ingest_path(
        self, file_path: Path, pattern: str, recursive: bool, tags: list[str]
    ) -> tuple[list[str], int]:
        """Ingest files from path. Returns (memory_ids, skipped_count)."""
        if file_path.is_file():
            return self._ingest_single_file(file_path, tags)
        return self._ingest_directory(file_path, pattern, recursive, tags)

    def _ingest_single_file(self, file_path: Path, tags: list[str]) -> tuple[list[str], int]:
        """Ingest a single file."""
        if self._should_skip_path(file_path):
            return [], 1
        result = self._ingest_file(file_path, tags)
        if "error" in result:
            return [], 0
        return result.get("memory_ids", []), 0

    def _ingest_directory(
        self, dir_path: Path, pattern: str, recursive: bool, tags: list[str]
    ) -> tuple[list[str], int]:
        """Ingest files from a directory."""
        files = list(dir_path.rglob(pattern)) if recursive else list(dir_path.glob(pattern))

        memory_ids: list[str] = []
        skipped_count = 0

        for f in files:
            if self._should_skip_path(f):
                skipped_count += 1
                continue
            result = self._ingest_file(f, tags)
            if "error" not in result:
                memory_ids.extend(result.get("memory_ids", []))

        return memory_ids, skipped_count

    def _memorize_all(self, memory_ids: list[str]) -> int:
        """Add all memory IDs to memorization queue. Returns count memorized."""
        count = 0
        for mid in memory_ids:
            try:
                self.memorize(memory_id=mid)
                count += 1
            except FrameworkError:
                pass  # Skip if already memorizing
        return count

    # File extensions that should use code-aware chunking
    CODE_EXTENSIONS = {
        ".py",
        ".js",
        ".ts",
        ".tsx",
        ".jsx",  # Python, JavaScript, TypeScript
        ".java",
        ".kt",
        ".scala",  # JVM languages
        ".go",
        ".rs",
        ".c",
        ".cpp",
        ".h",
        ".hpp",  # Systems languages
        ".rb",
        ".php",
        ".swift",
        ".m",  # Other popular languages
        ".sh",
        ".bash",
        ".zsh",  # Shell scripts
        ".sql",
        ".graphql",  # Query languages
        ".yaml",
        ".yml",
        ".toml",
        ".json",  # Config files (structured)
    }

    # Directories to always skip during ingestion (standard .gitignore patterns)
    IGNORED_DIRS = {
        # Virtual environments
        "venv",
        ".venv",
        "env",
        ".env",
        "virtualenv",
        # Python
        "__pycache__",
        ".pytest_cache",
        ".mypy_cache",
        ".ruff_cache",
        "*.egg-info",
        ".eggs",
        "dist",
        "build",
        # Node
        "node_modules",
        ".npm",
        # Git
        ".git",
        # IDE
        ".idea",
        ".vscode",
        ".vs",
        # OS
        ".DS_Store",
        # Test/coverage
        ".coverage",
        "htmlcov",
        ".tox",
    }

    def _should_skip_path(self, path: Path) -> bool:
        """Check if a path should be skipped during ingestion."""
        # Skip hidden files/directories (starting with .)
        for part in path.parts:
            if part.startswith(".") and part not in {".", ".."}:
                return True
            # Skip ignored directory names
            if part in self.IGNORED_DIRS:
                return True
            # Skip venv-like directories (contains 'venv' in name)
            if "venv" in part.lower():
                return True
        return False

    def _ingest_file(
        self,
        file_path: Path,
        tags: list[str],
    ) -> dict[str, Any]:
        """Ingest a single file as memories.

        Uses code-aware chunking for source code files, which breaks at
        function/class boundaries instead of paragraph breaks.
        """
        try:
            content = file_path.read_text(encoding="utf-8")
        except Exception as e:
            raise FrameworkError(
                message=f"Failed to read file {file_path}: {e}",
                error_code="memory.file_read_error",
            ) from e

        if not content.strip():
            return {"memory_ids": []}

        # Use code-aware chunking for source code files
        suffix = file_path.suffix.lower()
        if suffix in self.CODE_EXTENSIONS:
            chunks = chunk_code_file(
                content=content,
                source_file=str(file_path),
            )
        else:
            chunks = chunk_document(
                content=content,
                source_file=str(file_path),
            )

        memory_ids = []
        file_tags = tags + [f"source:{file_path.name}"]

        for chunk in chunks:
            result = self.remember(
                content=chunk["content"],
                tags=file_tags,
                source_file=str(file_path),
            )
            memory_ids.append(result["memory_id"])

        return {"memory_ids": memory_ids}

    def ingest_session(
        self,
        transcript: str,
        session_id: str | None = None,
        chunk_by_turns: bool = True,
    ) -> dict[str, Any]:
        """Ingest a conversation transcript as episodic memories."""
        if not self.actr_enabled:
            raise FrameworkError(
                message="ACT-R memory not enabled. Requires vector_service and embedding_service.",
                error_code="memory.actr_not_enabled",
            )

        if not transcript or not transcript.strip():
            raise FrameworkError(
                message="Transcript is required",
                error_code="memory.validation_error",
            )

        session_id = session_id or str(uuid.uuid4())

        if chunk_by_turns:
            from ananta.services.memory_service.actr.chunking import chunk_by_turns as split_turns

            chunks = split_turns(transcript)
        else:
            chunk_data = chunk_document(transcript)
            chunks = [c["content"] for c in chunk_data]

        memory_ids = []
        for chunk in chunks:
            if not chunk.strip():
                continue
            result = self.remember(
                content=chunk,
                session_id=session_id,
                tags=["conversation", f"session:{session_id}"],
            )
            memory_ids.append(result["memory_id"])

        return {
            "transcript_session_id": session_id,
            "memories_created": len(memory_ids),
            "memory_ids": memory_ids,
        }

    def memorize_by_tag(self, tag: str) -> dict[str, Any]:
        """Add all memories with a specific tag to memorization queue."""
        memories = get_all_memories(self.state_service, status="active", tag=tag)

        memorized = 0
        already_memorizing = 0

        for memory in memories:
            try:
                result = self.memorize(memory_id=memory["id"])
                if "Already memorizing" in result.get("message", ""):
                    already_memorizing += 1
                else:
                    memorized += 1
            except FrameworkError:
                already_memorizing += 1

        return {
            "tag": tag,
            "matching_memories": len(memories),
            "newly_memorizing": memorized,
            "already_memorizing": already_memorizing,
        }

    # -------------------------------------------------------------------------
    # ACT-R LIFECYCLE
    # -------------------------------------------------------------------------

    def consolidate(
        self,
        strength_threshold: float = EPISODIC_CONSOLIDATION_THRESHOLD,
        min_age_days: int = MIN_AGE_FOR_CONSOLIDATION_DAYS,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        """Summarize weak episodic memories into semantic memories."""
        if not self.actr_enabled:
            raise FrameworkError(
                message="ACT-R memory not enabled. Requires vector_service and embedding_service.",
                error_code="memory.actr_not_enabled",
            )

        now = datetime.now(UTC)
        cutoff = now - timedelta(days=min_age_days)

        all_memories = get_all_memories(self.state_service, memory_type="episodic", status="active")
        candidates = self._filter_consolidation_candidates(
            all_memories, strength_threshold, cutoff, now
        )

        if len(candidates) < MIN_CLUSTER_SIZE:
            return {
                "candidates_found": len(candidates),
                "clusters_formed": 0,
                "consolidations": [],
                "dry_run": dry_run,
            }

        clusters = cluster_memories(candidates)
        consolidations = self._process_consolidation_clusters(clusters, dry_run)

        return {
            "candidates_found": len(candidates),
            "clusters_formed": len(clusters),
            "consolidations": consolidations,
            "dry_run": dry_run,
        }

    def _filter_consolidation_candidates(
        self,
        memories: list[dict[str, Any]],
        strength_threshold: float,
        cutoff: datetime,
        now: datetime,
    ) -> list[dict[str, Any]]:
        """Filter memories eligible for consolidation by strength and age."""
        candidates = []
        for memory in memories:
            tags = memory.get("tags", [])
            if any(tag in tags for tag in CONSOLIDATION_EXCLUDED_TAGS):
                continue
            content = str(memory.get("content", ""))
            if content.startswith("Tool:"):
                # Defensive: tool-use records should not be consolidated
                continue
            strength = compute_strength(memory, now)
            created = parse_created_at(memory)
            if created is None:
                continue
            if strength < strength_threshold and created < cutoff:
                candidates.append(memory)
        return candidates

    def _process_consolidation_clusters(
        self, clusters: list[list[dict[str, Any]]], dry_run: bool
    ) -> list[dict[str, Any]]:
        """Process clusters for consolidation."""
        consolidations = []
        for cluster in clusters:
            if len(cluster) < MIN_CLUSTER_SIZE:
                continue
            result = self._consolidate_single_cluster(cluster, dry_run)
            if result:
                consolidations.append(result)
        return consolidations

    def _consolidate_single_cluster(
        self, cluster: list[dict[str, Any]], dry_run: bool
    ) -> dict[str, Any] | None:
        """Consolidate a single cluster of memories."""
        if dry_run:
            return {
                "action": "would_consolidate",
                "source_count": len(cluster),
                "new_memory_id": None,
                "summary_preview": cluster[0]["content"][:100] + "...",
            }

        summary = self._generate_cluster_summary(cluster)
        result = self.remember(content=summary, tags=["consolidated", "semantic_l1"])

        new_memory_id = result["memory_id"]
        self._finalize_consolidated_memory(new_memory_id, cluster)

        return {
            "action": "consolidated",
            "source_count": len(cluster),
            "new_memory_id": new_memory_id,
            "summary_preview": summary[:100] + "...",
        }

    def _generate_cluster_summary(self, cluster: list[dict[str, Any]]) -> str:
        """Generate summary for a cluster of memories."""
        contents = [m["content"] for m in cluster]
        if self.inference_service:
            return generate_summary(contents, self.inference_service)
        return " | ".join([c[:100] for c in contents[:3]]) + "..."

    def _finalize_consolidated_memory(
        self, new_memory_id: str, cluster: list[dict[str, Any]]
    ) -> None:
        """Update consolidated memory type and archive source memories."""
        new_memory = get_memory(self.state_service, new_memory_id)
        if new_memory:
            new_memory["memory_type"] = "semantic_l1"
            new_memory["source_memory_ids"] = [m["id"] for m in cluster]
            save_memory(self.state_service, new_memory)

        for memory in cluster:
            self.forget(memory["id"])

    def recompute_strengths(self) -> dict[str, Any]:
        """Recalculate activation strength for all active memories."""
        now = datetime.now(UTC)

        all_memories = get_all_memories(self.state_service, status="active")
        updated = 0

        for memory in all_memories:
            new_strength = compute_strength(memory, now)
            old_strength = memory.get("strength", 0)

            if abs(old_strength - new_strength) > 0.01:
                memory["strength"] = new_strength
                save_memory(self.state_service, memory)
                updated += 1

        return {
            "total_memories": len(all_memories),
            "updated": updated,
        }

    # -------------------------------------------------------------------------
    # ACT-R INTROSPECTION
    # -------------------------------------------------------------------------

    def memory_stats(self) -> dict[str, Any]:
        """Get overview of ACT-R memory store."""
        now = datetime.now(UTC)

        all_memories = get_all_memories(self.state_service)

        by_type: dict[str, int] = {"episodic": 0, "semantic_l1": 0, "semantic_l2": 0}
        by_status: dict[str, int] = {"active": 0, "archived": 0}
        strength_distribution: dict[str, int] = {
            "strong": 0,
            "medium": 0,
            "weak": 0,
            "very_weak": 0,
        }

        for memory in all_memories:
            # By type
            mtype = memory.get("memory_type", "episodic")
            by_type[mtype] = by_type.get(mtype, 0) + 1

            # By status
            status = memory.get("status", "active")
            by_status[status] = by_status.get(status, 0) + 1

            # By strength
            strength = compute_strength(memory, now)
            if strength > 0:
                strength_distribution["strong"] += 1
            elif strength > -1:
                strength_distribution["medium"] += 1
            elif strength > -2:
                strength_distribution["weak"] += 1
            else:
                strength_distribution["very_weak"] += 1

        return {
            "total": len(all_memories),
            "by_type": by_type,
            "by_status": by_status,
            "strength_distribution": strength_distribution,
        }

    def list_memories(
        self,
        memory_type: str | None = None,
        status: str = "active",
        tag: str | None = None,
        sort_by: str = "strength",
        limit: int = 20,
    ) -> dict[str, Any]:
        """List memories with filters."""
        now = datetime.now(UTC)

        all_memories = get_all_memories(
            self.state_service,
            memory_type=memory_type,
            status=status,
            tag=tag,
        )

        # Compute current strength
        for memory in all_memories:
            memory["_current_strength"] = compute_strength(memory, now)

        # Sort
        if sort_by == "strength":
            all_memories.sort(key=lambda m: m["_current_strength"], reverse=True)
        elif sort_by == "created_at":
            all_memories.sort(key=lambda m: m.get("created_at", ""), reverse=True)
        elif sort_by == "retrieval_count":
            all_memories.sort(key=lambda m: m.get("retrieval_count", 0), reverse=True)

        # Limit
        memories = all_memories[:limit]

        return {
            "memories": [
                {
                    "id": m["id"],
                    "content": m["content"][:200] + ("..." if len(m["content"]) > 200 else ""),
                    "memory_type": m.get("memory_type", "episodic"),
                    "strength": m["_current_strength"],
                    "retrieval_count": m.get("retrieval_count", 0),
                    "created_at": m.get("created_at", ""),
                    "tags": m.get("tags", []),
                }
                for m in memories
            ],
            "total": len(all_memories),
            "showing": len(memories),
        }

    # -------------------------------------------------------------------------
    # ACT-R IMPORT/EXPORT
    # -------------------------------------------------------------------------

    def export_memories(
        self,
        file_path: str | None = None,
        include_archived: bool = False,
        include_embeddings: bool = False,
        tags: list[str] | None = None,
    ) -> dict[str, Any]:
        """Export memories to a JSON file under an operator-allowed root.

        ``tags`` filters with ALL semantics (a record must carry EVERY listed
        tag), matching recall's tag filter, so a per-origin export —
        ``["agent_memory", "agent_memory:origin:X"]`` — yields only origin X's
        projection records and never another origin's (the cross-agent
        hydrate-leak guard). ``file_path`` (or the auto-generated default) must
        resolve under a configured ``export_allowed_roots`` entry; an unset roots
        list REFUSES the export.
        """
        status_filter = None if include_archived else "active"
        all_memories = get_all_memories(self.state_service, status=status_filter)
        filtered = filter_memories_by_all_tags(all_memories, tags)

        # Strip internal-only fields from the exported copy.
        export_data = []
        for memory in filtered:
            mem_copy = dict(memory)
            mem_copy.pop("_current_strength", None)
            export_data.append(mem_copy)

        output_path = Path(self._resolve_and_gate_export_path(file_path))

        # Write
        try:
            with open(output_path, "w", encoding="utf-8") as f:
                json.dump(
                    {
                        "version": "1.0",
                        "exported_at": datetime.now(UTC).isoformat(),
                        "memory_count": len(export_data),
                        "memories": export_data,
                    },
                    f,
                    indent=2,
                )
        except Exception as e:
            raise FrameworkError(
                message=f"Failed to write export file: {e}",
                error_code="memory.export_error",
            ) from e

        return {
            "file_path": str(output_path),
            "memory_count": len(export_data),
        }

    def _resolve_and_gate_export_path(self, file_path: str | None) -> str:
        """Resolve the export path and gate it against ``export_allowed_roots``.

        When no path is given, auto-generate a timestamped file under the first
        allowed root (so the convenience default still lands somewhere sanctioned);
        with no roots configured, the containment gate refuses.
        """
        if file_path is None and self._export_allowed_roots:
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            first_root = os.path.realpath(
                os.path.expandvars(os.path.expanduser(self._export_allowed_roots[0]))
            )
            file_path = os.path.join(first_root, f"actr_memory_export_{timestamp}.json")
        return assert_path_within_allowed_roots(
            file_path or "",
            self._export_allowed_roots,
            config_key=CONFIG_KEY_EXPORT_ALLOWED_ROOTS,
            plugin_name=PLUGIN_NAME,
        )

    def import_memories(
        self,
        file_path: str,
        regenerate_embeddings: bool = True,
    ) -> dict[str, Any]:
        """Import memories from JSON file."""
        if not self.actr_enabled:
            raise FrameworkError(
                message="ACT-R memory not enabled. Requires vector_service and embedding_service.",
                error_code="memory.actr_not_enabled",
            )
        assert self.embedding_service is not None
        assert self.vector_service is not None

        data = self._load_import_file(file_path)

        imported, skipped = self._import_memory_list(data.get("memories", []))

        return {
            "imported": imported,
            "skipped": skipped,
            "total_in_file": len(data.get("memories", [])),
        }

    def _load_import_file(self, file_path: str) -> dict[str, Any]:
        """Load and parse import file (path gated against export_allowed_roots)."""
        input_path = Path(
            assert_path_within_allowed_roots(
                file_path,
                self._export_allowed_roots,
                config_key=CONFIG_KEY_EXPORT_ALLOWED_ROOTS,
                plugin_name=PLUGIN_NAME,
            )
        )
        if not input_path.exists():
            raise FrameworkError(
                message=f"File not found: {file_path}",
                error_code="memory.file_not_found",
            )

        try:
            with open(input_path, encoding="utf-8") as f:
                result: dict[str, Any] = json.load(f)
                return result
        except Exception as e:
            raise FrameworkError(
                message=f"Failed to read import file: {e}",
                error_code="memory.import_error",
            ) from e

    def _import_memory_list(self, memories: list[dict[str, Any]]) -> tuple[int, int]:
        """Import a list of memories. Returns (imported, skipped)."""
        imported = 0
        skipped = 0

        for memory in memories:
            if get_memory(self.state_service, memory.get("id", "")):
                skipped += 1
                continue

            if self._import_single_memory(memory):
                imported += 1

        return imported, skipped

    def _import_single_memory(self, memory: dict[str, Any]) -> bool:
        """Import a single memory with embedding. Returns True on success."""
        if self.vector_service is None:
            logger.error("Vector service not available for import")
            return False

        try:
            embedding = self._generate_embedding(memory["content"])
        except FrameworkError as e:
            logger.error(f"Failed to generate embedding for import: {e}")
            return False

        try:
            self.vector_service.store_vectors(
                VECTOR_NAMESPACE,
                [
                    {
                        "external_id": memory["id"],
                        "vector": embedding,
                        "dimension": len(embedding),
                        "metadata": {
                            "memory_type": memory.get("memory_type", "episodic"),
                            "status": memory.get("status", "active"),
                            "created_at": memory.get("created_at", ""),
                        },
                    }
                ],
            )
        except Exception as e:
            logger.error(f"Failed to store vector for import: {e}")
            return False

        save_memory(self.state_service, memory)
        return True

    def purge_memories(self, confirm: bool = False) -> dict[str, Any]:
        """Purge all memories (long-term and short-term) and their embeddings.

        Protected entries (PURGE_PROTECTED_TAGS) are preserved: knowledge:official
        chunks (reinstalled by the knowledge plugin) and agent_memory projection
        records (the canonical store a frontier agent's local memory dir is
        derived from). Both must persist across purges.
        """
        if not confirm:
            raise FrameworkError(
                message="Purge requires confirm=True",
                error_code="memory.purge_not_confirmed",
            )

        if not self.actr_enabled:
            raise FrameworkError(
                message="Cannot purge memories: ACT-R memory not enabled",
                error_code="memory.actr_not_enabled",
            )
        assert self.vector_service is not None

        # 1. Get memory IDs (excluding purge-protected entries)
        all_memory_ids = self._get_all_memory_ids(exclude_protected=True)

        # 2. Delete embeddings for non-knowledge memories
        if all_memory_ids:
            delete_result = self.vector_service.delete_by_external_ids(
                namespace=VECTOR_NAMESPACE,
                external_ids=all_memory_ids,
            )

            if delete_result.get("action_status") != ActionStatus.COMPLETED.value:
                raise FrameworkError(
                    message=f"Failed to delete embeddings: {delete_result.get('error')}",
                    error_code="memory.embedding_delete_failed",
                )

        # 3. Delete memory records (preserving purge-protected entries)
        deleted_count = self._delete_all_memory_records(exclude_protected=True)

        # 4. Reseed identity memories - system cannot function without identity
        #    (The former short-term-event bulk-wipe was removed per the D8 operator
        #    decision, 2026-06-21: purge no longer clears the core interaction log —
        #    that was an actr→core cross-namespace wipe we no longer need.)
        identity_reseeded = self._reseed_identity_memories()

        return {
            "action_status": ActionStatus.COMPLETED.value,
            "data": {
                "purged_count": deleted_count,
                "identity_reseeded": identity_reseeded,
            },
        }

    def _delete_all_memory_records(self, exclude_protected: bool = False) -> int:
        """Delete all memory records. Returns count deleted.

        Args:
            exclude_protected: If True, preserve records carrying a
                PURGE_PROTECTED_TAGS tag — knowledge:official KB chunks (installed
                by the knowledge plugin) and agent_memory projection records (the
                canonical store a frontier agent's local memory dir is derived
                from). Both must survive a purge.
        """
        all_memories = get_all_memories(self.state_service)

        if exclude_protected:
            memory_ids = [
                m["id"]
                for m in all_memories
                if m.get("id") and PURGE_PROTECTED_TAGS.isdisjoint(m.get("tags") or [])
            ]
        else:
            memory_ids = [m["id"] for m in all_memories if m.get("id")]

        # Also hard-delete memorizations (soft_delete=False — actr deletes are hard).
        memorizations = get_all_memorizations(self.state_service)
        for record in memorizations:
            record_id = record.get("id")
            if record_id:
                self.state_service.delete_records(
                    namespace=NAMESPACE,
                    query={"table": "memorization", "filters": {"id": record_id}, "soft_delete": False},
                )

        # Delete all memories
        return delete_memory_records(self.state_service, memory_ids)

    def _reseed_identity_memories(self) -> int:
        """Reseed identity memories from config after purge. Fails fast.

        Reads identity strings from APP_HOME/config/identity.json and stores them
        as memories with the 'identity' tag. This ensures the system can function
        immediately after a purge.

        Returns:
            Number of identity memories seeded

        Raises:
            FrameworkError: If identity.json is missing, empty, or invalid
        """
        app_home = os.environ.get("APP_HOME")
        if not app_home:
            raise FrameworkError(
                message="APP_HOME environment variable not set - cannot locate identity.json",
                error_code="memory.config_missing",
            )

        identity_path = Path(app_home) / "config" / "identity.json"
        if not identity_path.exists():
            raise FrameworkError(
                message=f"Identity config not found: {identity_path}",
                error_code="memory.config_missing",
            )

        with open(identity_path) as f:
            config = json.load(f)

        # Extract identity list from config dict (matches startup_sequence format)
        identity_items = config.get("identity", []) if isinstance(config, dict) else []

        if not identity_items:
            raise FrameworkError(
                message=f"identity.json 'identity' array is empty or missing: {identity_path}",
                error_code="memory.config_invalid",
            )

        if not isinstance(identity_items, list):
            raise FrameworkError(
                message=f"identity.json 'identity' must be a list, got: {type(identity_items).__name__}",
                error_code="memory.config_invalid",
            )

        seeded_count = 0
        for i, item in enumerate(identity_items):
            if not isinstance(item, str) or not item.strip():
                raise FrameworkError(
                    message=f"identity.json item[{i}] must be a non-empty string",
                    error_code="memory.config_invalid",
                )

            result = self.remember(content=item, tags=[IDENTITY_TAG])
            if "error" in result:
                raise FrameworkError(
                    message=f"Failed to seed identity item[{i}]: {result['error']}",
                    error_code="memory.storage_error",
                )
            seeded_count += 1

        logger.debug(f"Reseeded {seeded_count} identity memories after purge")
        return seeded_count

    def _get_all_memory_ids(self, exclude_protected: bool = False) -> list[str]:
        """Get all live memory IDs for embedding cleanup. Fails fast.

        ``query_state`` (→ ``read_state`` → ``select``) applies NO ``is_deleted``
        filter — the ``is_deleted=0`` default is ``query_ordered``-only — so the
        original ``is_deleted IS NULL OR is_deleted = 0`` WHERE is preserved in
        Python (an OR-predicate the flat filter grammar can't express), matching
        ``_live_memory_ids`` / ``_find_orphaned_memories``. No-op today (actr
        memory hard-deletes) but faithful. The PURGE_PROTECTED_TAGS membership
        test is likewise a JSON-array test the grammar can't express, applied in
        Python (the canonical single-namespace restructure), mirroring the recall
        path's tag-exclusion. It must stay symmetric with
        ``_delete_all_memory_records``: purge deletes a record's vector here and
        its row there, so a tag protected in one path but not the other would
        leave an orphaned vector or record.

        Args:
            exclude_protected: If True, skip records carrying a
                PURGE_PROTECTED_TAGS tag (knowledge:official / agent_memory).
        """
        # PAGINATED, not read whole (2026-08-15 read-cap repair). This is a
        # BOOT-PATH read: _handle_clean_restart -> purge_memories -> here, and
        # that startup step raises StartupError on a non-COMPLETED result, so a
        # refusal aborts the boot rather than logging a warning. `memory` was
        # measured at 22,238 rows against a 100-row default bound, so the
        # unpaginated read this replaced could not have completed.
        #
        # It has not fired only because the step returns early unless
        # ANANTA_CLEAN_RESTART=true — and an env gate is a delay, not a bound.
        # The boot that sets it is a rebuilt database or a clean-restart
        # recovery: precisely the boot someone reaches for when a deploy has
        # already gone wrong, and the worst possible moment to discover this.
        #
        # include_deleted=True is REQUIRED, not a convenience. query_ordered's
        # default applies SQL `is_deleted = 0`, which does not match NULL, while
        # the Python test below deliberately keeps `is_deleted IS NULL OR = 0`
        # (an OR the flat filter grammar cannot express — see the docstring).
        # Letting the default filter here would silently narrow the purge to a
        # subset and strand vectors whose records get deleted by
        # _delete_all_memory_records, breaking the symmetry the docstring
        # requires between the two halves of a purge.
        rows = iter_table_rows(
            self.state_service,
            namespace=NAMESPACE,
            table="memory",
            filters={},
            ceiling=_PURGE_WALK_CEILING,
            reason=_PURGE_WALK_CEILING_REASON,
            include_deleted=True,
        )
        ids: list[str] = []
        for raw in rows:
            # iter_table_rows yields dict[str, object] — deliberately narrower
            # than the dict[str, Any] the previous `cast` produced, so pyright
            # now sees `row.get(...)` as `object` rather than silently `Any`.
            # Cast once at the boundary rather than at each use: the surrounding
            # method is Any-typed by the state layer's own shape, and casting per
            # call site would spread the same assertion over three lines.
            row = cast(dict[str, Any], raw)
            if row.get("is_deleted") not in (None, 0):
                continue
            if exclude_protected:
                parse_json_field(row, "tags", row.get("id"))
                if not PURGE_PROTECTED_TAGS.isdisjoint(row.get("tags") or []):
                    continue
            ids.append(str(row.get("id")))
        return ids

    def cleanup_orphaned_vectors(
        self, dry_run: bool = False, confirm: bool = False
    ) -> dict[str, Any]:
        """One-shot operator-gated REBUILD of the shared memory-vector namespace.

        Cascade-vector-first (the delete paths) now makes orphan-VECTORS
        unreachable-by-construction, so this handles only the PRE-EXISTING
        orphans accumulated before that fix. Vectors are REGENERABLE, so the
        repair is a namespace REBUILD: clear EVERY vector in ``VECTOR_NAMESPACE``,
        then reindex every live memory through the SQL-free reindex path.
        ``VECTOR_NAMESPACE`` is shared by actr memories AND knowledge-base chunks
        (kb chunks are memories written via ``remember``), so one rebuild covers
        both. No vector enumeration, no foreign SQL.

        ``dry_run=True`` reports the would-clear / would-reindex counts WITHOUT
        mutating. The destructive run REQUIRES ``confirm=True`` (mirrors
        ``purge_memories``): it WIPES every vector before regenerating, so an
        un-confirmed call is rejected.
        """
        if not self.actr_enabled:
            raise FrameworkError(
                message="ACT-R memory not enabled. Requires vector_service and embedding_service.",
                error_code="memory.actr_not_enabled",
            )
        assert self.vector_service is not None

        if dry_run:
            # Same {cleared, reindexed} shape as the real run — here they are the
            # would-be counts (current vectors that would be wiped; live memories
            # that would be regenerated), nothing is mutated.
            return {
                "dry_run": True,
                "cleared": self._current_vector_count(),
                "reindexed": len(self._live_memory_ids()),
            }

        if not confirm:
            raise FrameworkError(
                message=(
                    "cleanup_orphaned_vectors rebuilds the whole vector namespace "
                    "(clears then regenerates every vector) — pass confirm=True to proceed"
                ),
                error_code="memory.rebuild_not_confirmed",
            )

        clear_result = self.vector_service.delete_all_in_namespace(VECTOR_NAMESPACE)
        if clear_result.get("action_status") != ActionStatus.COMPLETED.value:
            raise FrameworkError(
                message=f"Failed to clear vector namespace: {clear_result.get('error')}",
                error_code="memory.vector_clear_failed",
            )
        cleared_inner = cast(dict[str, Any], clear_result.get("data", {})).get("result", {})
        cleared = int(cleared_inner.get("deleted_count", 0)) if isinstance(cleared_inner, dict) else 0

        reindexed = self._rebuild_all_vectors()
        return {"dry_run": False, "cleared": cleared, "reindexed": reindexed}

    def _live_memory_ids(self) -> list[str]:
        """Ids of every active, non-soft-deleted memory. actr memories AND kb
        chunks share the ``memory`` table and ``get_all_memories`` is not
        actr-scoped, so this enumerates BOTH — the rebuild must regenerate every
        one (else clear-all would wipe kb chunk vectors with no restore)."""
        return [
            str(memory["id"])
            for memory in get_all_memories(self.state_service, status="active")
            if memory.get("is_deleted") in (None, 0) and memory.get("id")
        ]

    def _current_vector_count(self) -> int:
        """Current active-vector count in ``VECTOR_NAMESPACE`` (0 when empty — the
        provider's stats raise on an empty namespace, surfaced as a non-completed
        envelope, which here means nothing to clear)."""
        assert self.vector_service is not None
        stats = self.vector_service.get_namespace_stats(VECTOR_NAMESPACE)
        if stats.get("action_status") != ActionStatus.COMPLETED.value:
            return 0
        inner = cast(dict[str, Any], stats.get("data", {})).get("result", {})
        return int(inner.get("vector_count", 0)) if isinstance(inner, dict) else 0

    def _rebuild_all_vectors(self) -> int:
        """Reindex EVERY live memory after a namespace clear, via the SQL-free
        reindex path. Loops the capped reconcile (``_REINDEX_BATCH_LIMIT`` per
        pass) until drained; each pass reindexes a non-empty orphan batch in full
        (or fails loud), so it makes guaranteed forward progress and terminates in
        ceil(N / cap) passes."""
        total = 0
        while True:
            rows = self._find_orphaned_memories()
            if not rows:
                return total
            total += self._reindex_memory_batch(rows)

    # -------------------------------------------------------------------------
    # v58 NEW METHODS: Reindex and Compaction
    # -------------------------------------------------------------------------

    def reindex_orphaned_vectors(self) -> dict[str, Any]:
        """Reindex memories that have DB records but no embeddings. Fails fast."""
        if not self.actr_enabled:
            raise FrameworkError(
                message="Cannot reindex: ACT-R memory not enabled",
                error_code="memory.actr_not_enabled",
            )

        rows = self._find_orphaned_memories()
        if not rows:
            return {
                "action_status": ActionStatus.COMPLETED.value,
                "data": {"reindex_count": 0, "message": "No orphaned memories"},
            }

        reindexed = self._reindex_memory_batch(rows)
        return {
            "action_status": ActionStatus.COMPLETED.value,
            "data": {"reindex_count": reindexed},
        }

    def _find_orphaned_memories(self) -> list[dict[str, Any]]:
        """Find active memories with no ACTIVE vector (orphaned → reindex).

        SQL-lockdown migration (D1/GAP-5, sub-slice A): the raw
        ``memory ⋈ pgvector__embeddings`` LEFT-JOIN anti-join is replaced by the
        owning-service path — a single-namespace read of active memories (own
        ns, via ``get_all_memories``) + a Python ``is_deleted`` filter (the
        GAP-9 ``is_deleted IS NULL OR = 0`` OR-predicate the flat grammar can't
        express; no NULL→0 backfill needed) → ``find_missing_external_ids`` (the
        landed memory→vector verb on the owning ``vector_service``) returns the
        subset lacking a vector. No foreign ``pgvector__embeddings`` SQL.

        BEHAVIOR-EQUIVALENCE: ``find_missing_external_ids`` filters the vector
        side to ``is_deleted = 0`` (active vectors only), whereas the old
        anti-join matched a vector regardless of its ``is_deleted``. The only
        divergent state — an ACTIVE memory whose ONLY vector is soft-deleted —
        is UNREACHABLE: every vector soft-delete is paired with archiving the
        memory (``forget`` → status != 'active', so not a candidate) or removing
        it (``delete_memory`` / ``delete_memories_by_tag`` /
        ``upsert_memory_by_tag`` / ``purge_memories`` delete the memory record
        too). A memory with NO vector row at all — the real reindex target — is
        reported missing by BOTH paths. The Python ``is_deleted`` filter is
        itself a no-op today (actr memory deletes are HARD, so no
        ``is_deleted=1`` memory exists) but faithfully preserves the original
        WHERE.

        Capped at ``_REINDEX_BATCH_LIMIT`` (the reconcile drains over idempotent
        re-runs: reindexed memories gain an active vector and drop out).
        """
        memories = get_all_memories(self.state_service, status="active")
        by_id = {
            str(memory["id"]): memory
            for memory in memories
            if memory.get("is_deleted") in (None, 0)
        }
        if not by_id:
            return []

        missing_ids = self._memory_ids_without_vector(list(by_id.keys()))
        orphaned = [
            {"id": memory_id, "content": str(by_id[memory_id].get("content", ""))}
            for memory_id in missing_ids
        ]
        # SCALE NOTE: unlike the old DB-side LIMIT, this materializes all active
        # memories + the full missing set in Python before truncating. Fine for a
        # periodic idempotent reconcile bounded by the active-memory count; if
        # that ever grows very large, push the cap into a paginated primitive
        # rather than scanning all in Python.
        return orphaned[:_REINDEX_BATCH_LIMIT]

    def _memory_ids_without_vector(self, candidate_ids: list[str]) -> list[str]:
        """The subset of ``candidate_ids`` with no ACTIVE vector, via the owning
        ``vector_service`` (D1: foreign reads route through the owning
        interface, not raw ``pgvector__embeddings`` SQL). Fails fast on a missing
        service or a non-completed envelope; extracts ``data.result.missing``."""
        if self.vector_service is None:
            raise FrameworkError(
                message="Vector service not available for orphan reconcile",
                error_code="memory.vector_service_unavailable",
            )
        result = self.vector_service.find_missing_external_ids(
            namespace=VECTOR_NAMESPACE,
            candidate_external_ids=candidate_ids,
        )
        if result.get("action_status") != ActionStatus.COMPLETED.value:
            raise FrameworkError(
                message=f"find_missing_external_ids failed: {result.get('error')}",
                error_code="memory.vector_query_failed",
            )
        data = cast(dict[str, Any], result.get("data", {}))
        inner = cast(dict[str, Any], data.get("result", {}))
        return cast(list[str], inner.get("missing", []))

    def _reindex_memory_batch(self, rows: list[dict[str, Any]]) -> int:
        """Reindex a batch of orphaned memories. Returns count reindexed."""
        now_iso = datetime.now(UTC).isoformat()
        reindexed = 0

        for row in rows:
            memory_id = row.get("id")
            content = row.get("content")

            if not memory_id or not content:
                raise FrameworkError(
                    message="Invalid memory record: missing id or content",
                    error_code="memory.invalid_record",
                )

            embedding = self._generate_embedding(content)
            store_memory_vector(self.vector_service, memory_id, embedding, now_iso)
            reindexed += 1

        return reindexed

    def store_compaction_summary(
        self,
        context_id: str,
        summary: str,
        compacted_event_count: int,
        session_id: str | None = None,
    ) -> dict[str, Any]:
        """Store a compaction summary as a long-term memory.

        Called by ContextManagementService when compacting conversation history.

        Args:
            context_id: ID of the context being compacted
            summary: The generated summary text
            compacted_event_count: Number of events that were compacted
            session_id: Optional session ID for the summary memory

        Returns:
            Result from remember() with the new memory_id
        """
        if not self.actr_enabled:
            raise FrameworkError(
                message="Cannot store compaction summary: ACT-R memory not enabled",
                error_code="memory.actr_not_enabled",
            )

        tags = ["compaction_summary", f"context:{context_id}"]
        if compacted_event_count:
            tags.append(f"events:{compacted_event_count}")

        return self.remember(content=summary, tags=tags, session_id=session_id)

    # -------------------------------------------------------------------------
    # P3 MAINTENANCE: Audit and Review Operations
    # -------------------------------------------------------------------------

    def audit_pinned_notes(
        self,
        include_completed: bool = False,
        strength_threshold: float | None = None,
    ) -> dict[str, Any]:
        """Audit memories in the memorization queue."""
        now = datetime.now(UTC)
        records = get_all_memorizations(self.state_service)

        if not include_completed:
            records = [r for r in records if r.get("status") == "active"]

        items, counts = self._build_audit_items(records, strength_threshold, now)
        items.sort(key=lambda x: x.get("strength", 0))

        return {
            "total_pinned": len(items),
            "active": counts["active"],
            "completed": counts["completed"],
            "weak_pins": counts["weak"],
            "items": items,
        }

    def _build_audit_items(
        self,
        records: list[dict[str, Any]],
        strength_threshold: float | None,
        now: datetime,
    ) -> tuple[list[dict[str, Any]], dict[str, int]]:
        """Build audit items from memorization records."""
        items: list[dict[str, Any]] = []
        counts = {"active": 0, "completed": 0, "weak": 0}

        for record in records:
            item = self._build_audit_item(record, strength_threshold, now, counts)
            if item:
                items.append(item)

        return items, counts

    def _build_audit_item(
        self,
        record: dict[str, Any],
        strength_threshold: float | None,
        now: datetime,
        counts: dict[str, int],
    ) -> dict[str, Any] | None:
        """Build a single audit item from a memorization record."""
        memory = get_memory(self.state_service, record["memory_id"])
        if not memory:
            return None

        strength = memory.get("current_strength", 0.0)
        if strength_threshold is not None and strength >= strength_threshold:
            return None

        update_audit_counts(strength, record.get("status", ""), counts)

        return {
            "memory_id": record["memory_id"],
            "content_preview": content_preview(memory.get("content", ""), 150),
            "strength": round(strength, 3),
            "review_count": record.get("review_count", 0),
            "interval_days": record.get("interval_days", 0),
            "next_review_at": record.get("next_review_at", ""),
            "is_due": is_review_due(record.get("next_review_at", ""), now),
            "status": record.get("status", ""),
            "tags": memory.get("tags", []),
            "created_at": memory.get("created_at", ""),
        }

    def review_blocked_intents(
        self,
        min_age_days: int = 7,
        strength_threshold: float = -1.0,
    ) -> dict[str, Any]:
        """Review blocked intent memories for staleness.

        Args:
            min_age_days: Only show blocked intents older than this
            strength_threshold: Show intents weaker than this (candidates for retry)

        Returns:
            Review results with retry candidates and still-valid intents
        """
        now = datetime.now(UTC)
        age_cutoff = now - timedelta(days=min_age_days)

        # Query memories tagged as blocked intents
        # The tag format for blocked intents would be "blocked" or "tool_failure"
        blocked_tags = ["blocked", "tool_failure"]

        retry_candidates: list[dict[str, Any]] = []
        still_valid: list[dict[str, Any]] = []
        total_blocked = 0

        for tag in blocked_tags:
            memories = self.list_memories(tag=tag, limit=100)
            memory_list = memories.get("memories", [])

            for memory in memory_list:
                total_blocked += 1

                strength = memory.get("current_strength", 0.0)
                created_at_str = memory.get("created_at", "")

                # Parse created_at for age check
                is_old = False
                if created_at_str:
                    try:
                        created_at = datetime.fromisoformat(created_at_str.replace("Z", "+00:00"))
                        if created_at.tzinfo is None:
                            created_at = created_at.replace(tzinfo=UTC)
                        is_old = created_at < age_cutoff
                    except ValueError:
                        pass

                item = {
                    "memory_id": memory.get("id", ""),
                    "content_preview": memory["content"][:150] + "..."
                    if len(memory.get("content", "")) > 150
                    else memory.get("content", ""),
                    "strength": round(strength, 3),
                    "created_at": created_at_str,
                    "tags": memory.get("tags", []),
                    "is_old": is_old,
                    "age_days": (
                        now
                        - datetime.fromisoformat(created_at_str.replace("Z", "+00:00")).replace(
                            tzinfo=UTC
                        )
                    ).days
                    if created_at_str
                    else 0,
                }

                # Classify as retry candidate or still valid
                if is_old and strength < strength_threshold:
                    retry_candidates.append(item)
                else:
                    still_valid.append(item)

        # Sort retry candidates by strength (weakest first)
        retry_candidates.sort(key=lambda x: x.get("strength", 0))

        # Sort still valid by strength (strongest first)
        still_valid.sort(key=lambda x: x.get("strength", 0), reverse=True)

        return {
            "total_blocked": total_blocked,
            "stale_count": len(retry_candidates),
            "retry_candidates": retry_candidates,
            "still_valid": still_valid,
        }


__all__ = ["ACTRMemoryBackend"]
