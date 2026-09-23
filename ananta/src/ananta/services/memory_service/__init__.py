"""Memory Service - Wrapper over bound memory plugin.

Follows the standard platform pattern (like VectorService, InferenceService):
- Thin wrapper that delegates to bound plugin
- Plugin provides the single implementation
- Eager initialization - fails fast in _init_plugin(), not lazily

Inherits from MemoryServiceInterface to satisfy type contracts.
"""

import logging
from typing import Any

from ananta.core.plugins.plugin_manager import PluginManager
from ananta.error_handling import FrameworkError
from ananta.interfaces.bootstrappable_service_interface import BootstrappableServiceInterface
from ananta.interfaces.memory_service_interface import MemoryServiceInterface

# Tag applied to all knowledge base chunks by the knowledge plugin.
# Memory recall excludes these by default to separate personal memories
# from reference documentation. Knowledge base content is accessed
# exclusively through knowledge_service::search.
_KNOWLEDGE_OFFICIAL_TAG = "knowledge:official"

logger = logging.getLogger(__name__)


class MemoryService(BootstrappableServiceInterface, MemoryServiceInterface):
    """Service wrapper for memory plugin providers.

    Initialization is EAGER, not lazy:
    - Plugin is resolved and validated in _init_plugin()
    - _get_backend() assumes initialization is complete
    - NO _ensure_ready() lazy initialization pattern
    """

    def __init__(
        self,
        plugin_manager: PluginManager,
        memory_plugin_name: str,
    ) -> None:
        if not memory_plugin_name:
            raise FrameworkError(
                "memory_plugin_name is required. "
                "Ensure MEMORY_SERVICE is bound in config/service_bindings.json."
            )

        self._memory_plugin_name = memory_plugin_name
        self._memory_plugin: MemoryServiceInterface | None = None

        super().__init__(plugin_manager)
        self.plugin_manager: PluginManager = plugin_manager

    def _init_bootstrap(self) -> None:
        raise FrameworkError(
            "MemoryService does not support bootstrap mode."
        )

    def _init_plugin(self) -> None:
        """Eagerly initialize the memory plugin. Fails fast on any error."""
        logger.debug(f"MemoryService initializing with plugin: {self._memory_plugin_name}")

        # Resolve plugin immediately - fail fast if not found
        plugin = self.plugin_manager.get_plugin(self._memory_plugin_name)

        if not isinstance(plugin, MemoryServiceInterface):
            raise FrameworkError(
                f"Memory plugin '{self._memory_plugin_name}' does not implement "
                f"MemoryServiceInterface. Plugin type: {type(plugin)}"
            )

        # Validate readiness immediately - fail fast if not ready
        if not plugin.is_ready():
            error = plugin.readiness_error or "Unknown"
            raise FrameworkError(f"Memory plugin not ready: {error}")

        # Set as active provider
        setter = getattr(plugin, "set_as_active_provider", None)
        if callable(setter):
            setter("MemoryServiceInterface")

        self._memory_plugin = plugin
        logger.debug("MemoryService initialization complete")

    def _get_backend(self) -> MemoryServiceInterface:
        """Get the initialized backend. Assumes _init_plugin() succeeded.

        This is NOT lazy initialization. If _memory_plugin is None,
        it means _init_plugin() was never called, which is a programming error.
        """
        if self._memory_plugin is None:
            raise FrameworkError(
                "Memory plugin not initialized. "
                "This indicates _init_plugin() was not called - programming error."
            )
        return self._memory_plugin

    # ==========================================================================
    # CORE MEMORY INTERFACE
    # ==========================================================================

    def remember(
        self,
        content: str,
        tags: list[str] | None = None,
        source_file: str | None = None,
        session_id: str | None = None,
        embed: bool = True,
    ) -> dict[str, Any]:
        return self._get_backend().remember(
            content=content, tags=tags, source_file=source_file,
            session_id=session_id, embed=embed,
        )

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
        # When the caller does not request specific inclusion tags,
        # automatically exclude knowledge base content.  Callers that
        # need knowledge base chunks (e.g. knowledge_service::search)
        # always pass explicit `tags`, so this default never interferes.
        effective_exclude = exclude_tags
        if exclude_tags is None and tags is None:
            effective_exclude = [_KNOWLEDGE_OFFICIAL_TAG]

        return self._get_backend().recall(
            query=query,
            top_k=top_k,
            memory_type=memory_type,
            include_archived=include_archived,
            tags=tags,
            exclude_ids=exclude_ids,
            score_by_similarity=score_by_similarity,
            exclude_tags=effective_exclude,
        )

    def forget(self, memory_id: str) -> dict[str, Any]:
        return self._get_backend().forget(memory_id)

    def reinforce(self, memory_id: str) -> dict[str, Any]:
        return self._get_backend().reinforce(memory_id)

    def memorize(
        self,
        memory_id: str | None = None,
        content: str | None = None,
    ) -> dict[str, Any]:
        return self._get_backend().memorize(memory_id=memory_id, content=content)

    def memory_stats(self) -> dict[str, Any]:
        return self._get_backend().memory_stats()

    def list_memories(
        self,
        memory_type: str | None = None,
        status: str = "active",
        tag: str | None = None,
        sort_by: str = "strength",
        limit: int = 20,
    ) -> dict[str, Any]:
        return self._get_backend().list_memories(
            memory_type=memory_type, status=status, tag=tag, sort_by=sort_by, limit=limit
        )

    def consolidate(
        self,
        strength_threshold: float = -1.5,
        min_age_days: int = 7,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        return self._get_backend().consolidate(
            strength_threshold=strength_threshold, min_age_days=min_age_days, dry_run=dry_run
        )

    def export_memories(
        self,
        file_path: str | None = None,
        include_archived: bool = False,
        include_embeddings: bool = False,
        tags: list[str] | None = None,
    ) -> dict[str, Any]:
        return self._get_backend().export_memories(
            file_path=file_path,
            include_archived=include_archived,
            include_embeddings=include_embeddings,
            tags=tags,
        )

    def purge_memories(self, confirm: bool = False) -> dict[str, Any]:
        return self._get_backend().purge_memories(confirm=confirm)

    def get_recent_memory(
        self,
        session_id: str | None = None,
        max_events: int = 20,
        max_age_hours: int | None = None,
        namespace_filter: str | None = None,
    ) -> dict[str, Any]:
        return self._get_backend().get_recent_memory(
            session_id=session_id,
            max_events=max_events,
            max_age_hours=max_age_hours,
            namespace_filter=namespace_filter,
        )

    def get_session_event_stats(self, session_id: str) -> dict[str, Any]:
        return self._get_backend().get_session_event_stats(session_id)

    # ==========================================================================
    # SHORT-TERM MEMORY
    # ==========================================================================

    def store_interaction(
        self,
        session_id: str,
        source_namespace: str,
        event_type: str,
        content: str,
        metadata: dict[str, Any] | None = None,
        timestamp: str | None = None,
    ) -> dict[str, Any]:
        return self._get_backend().store_interaction(
            session_id=session_id,
            source_namespace=source_namespace,
            event_type=event_type,
            content=content,
            metadata=metadata,
            timestamp=timestamp,
        )

    def get_recent_memory_structured(
        self,
        session_id: str | None = None,
        max_events: int = 20,
        max_age_hours: int | None = None,
        namespace_filter: str | None = None,
    ) -> dict[str, Any]:
        return self._get_backend().get_recent_memory_structured(
            session_id=session_id,
            max_events=max_events,
            max_age_hours=max_age_hours,
            namespace_filter=namespace_filter,
        )

    # ==========================================================================
    # TAG OPERATIONS (canonical for knowledge base lifecycle)
    # ==========================================================================

    def delete_memories_by_tag(self, tag: str) -> dict[str, Any]:
        return self._get_backend().delete_memories_by_tag(tag)

    def delete_memories_by_ids(self, ids: list[str]) -> dict[str, Any]:
        return self._get_backend().delete_memories_by_ids(ids)

    def get_memories_by_tag(
        self,
        tag: str,
        include_archived: bool = False,
    ) -> dict[str, Any]:
        return self._get_backend().get_memories_by_tag(tag=tag, include_archived=include_archived)

    def upsert_memory_by_tag(
        self,
        content: str,
        tag: str,
        tags: list[str] | None = None,
        session_id: str | None = None,
    ) -> dict[str, Any]:
        return self._get_backend().upsert_memory_by_tag(
            content=content, tag=tag, tags=tags, session_id=session_id
        )

    def memorize_by_tag(self, tag: str) -> dict[str, Any]:
        return self._get_backend().memorize_by_tag(tag)

    # ==========================================================================
    # MEMORIZATION QUEUE
    # ==========================================================================

    def stop_memorizing(self, memory_id: str) -> dict[str, Any]:
        return self._get_backend().stop_memorizing(memory_id)

    def list_memorizing(self, include_completed: bool = False) -> dict[str, Any]:
        return self._get_backend().list_memorizing(include_completed=include_completed)

    def process_memorization_queue(self) -> dict[str, Any]:
        return self._get_backend().process_memorization_queue()

    # ==========================================================================
    # MAINTENANCE
    # ==========================================================================

    def recompute_strengths(self) -> dict[str, Any]:
        return self._get_backend().recompute_strengths()

    # ==========================================================================
    # CRON-ONLY EDGE_SINK SIBLINGS (Phase 2, 2026-06-17)
    # ==========================================================================
    # Thin delegation wrappers for the cron-only EDGE_SINK siblings declared
    # on MemoryServiceInterface. The dispatcher reaches MemoryService via
    # `provider_manager.get_service_instance("memory_service", ...)`, then
    # calls the method by name; these wrappers forward to the bound backend
    # (ACTRMemoryPlugin). See `services/memory_service/interfaces/public.py`
    # for the @service_interface_process declarations + the canonical
    # contract at `knowledge_bases/ananta_platform/21_scheduling_service/
    # 01_template_flow_record_lifecycle.md`.

    def process_memorization_queue_cron(self) -> dict[str, Any]:
        return self._get_backend().process_memorization_queue_cron()

    def consolidate_cron(self, dry_run: bool = False) -> dict[str, Any]:
        return self._get_backend().consolidate_cron(dry_run=dry_run)

    def recompute_strengths_cron(self) -> dict[str, Any]:
        return self._get_backend().recompute_strengths_cron()

    def import_memories(
        self,
        file_path: str,
        regenerate_embeddings: bool = True,
    ) -> dict[str, Any]:
        return self._get_backend().import_memories(
            file_path=file_path, regenerate_embeddings=regenerate_embeddings
        )

    def cleanup_orphaned_vectors(
        self, dry_run: bool = False, confirm: bool = False
    ) -> dict[str, Any]:
        return self._get_backend().cleanup_orphaned_vectors(dry_run=dry_run, confirm=confirm)

    def reindex_orphaned_vectors(self) -> dict[str, Any]:
        return self._get_backend().reindex_orphaned_vectors()

    # ==========================================================================
    # LEARNING
    # ==========================================================================

    def ingest_session(
        self,
        transcript: str,
        session_id: str | None = None,
        chunk_by_turns: bool = True,
    ) -> dict[str, Any]:
        return self._get_backend().ingest_session(
            transcript=transcript, session_id=session_id, chunk_by_turns=chunk_by_turns
        )

    def learn(
        self,
        path: str,
        pattern: str = "*.md",
        recursive: bool = True,
        memorize: bool = False,
        tags: list[str] | None = None,
    ) -> dict[str, Any]:
        return self._get_backend().learn(
            path=path, pattern=pattern, recursive=recursive, memorize=memorize, tags=tags
        )

    # ==========================================================================
    # AUDIT
    # ==========================================================================

    def audit_pinned_notes(
        self,
        include_completed: bool = False,
        strength_threshold: float | None = None,
    ) -> dict[str, Any]:
        return self._get_backend().audit_pinned_notes(
            include_completed=include_completed, strength_threshold=strength_threshold
        )

    def review_blocked_intents(
        self,
        min_age_days: int = 7,
        strength_threshold: float = -1.0,
    ) -> dict[str, Any]:
        return self._get_backend().review_blocked_intents(
            min_age_days=min_age_days, strength_threshold=strength_threshold
        )

    # ==========================================================================
    # FOCUS BUFFER (session-scoped — JOS-02)
    # ==========================================================================

    @staticmethod
    def _resolve_acting_session(
        session_id: str,
        state: dict[str, Any] | None,
    ) -> str:
        """Resolve the acting session for a focus-buffer operation.

        The server-built ``state`` (injected by ActionProcessor with the
        action's OWN session and always overwritten server-side) is
        authoritative on the verb path; the explicit ``session_id`` kwarg is
        the Python-caller path. An argument-supplied session can never
        override ``state`` (V-2), and no session at all is a fail-fast error.
        """
        if state is not None:
            injected = str(state.get("session_id") or "")
            if injected:
                return injected
        if not session_id:
            raise FrameworkError(
                message=(
                    "focus-buffer operation requires an acting session; pass "
                    "session_id (Python callers) or invoke via an action that "
                    "carries one (verb callers)"
                ),
                error_code="memory.session_required",
            )
        return session_id

    def focus(
        self,
        memory_id: str,
        *,
        session_id: str = "",
        state: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        resolved = self._resolve_acting_session(session_id, state)
        return self._get_backend().focus(memory_id, session_id=resolved)

    def unfocus(
        self,
        memory_id: str,
        *,
        session_id: str = "",
        state: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        resolved = self._resolve_acting_session(session_id, state)
        return self._get_backend().unfocus(memory_id, session_id=resolved)

    def unfocus_all_for_session(self, *, session_id: str) -> dict[str, Any]:
        return self._get_backend().unfocus_all_for_session(session_id=session_id)

    def get_focused(
        self,
        *,
        session_id: str = "",
        state: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        resolved = self._resolve_acting_session(session_id, state)
        return self._get_backend().get_focused(session_id=resolved)

    # ==========================================================================
    # LIFECYCLE
    # ==========================================================================

    def store_compaction_summary(
        self,
        context_id: str,
        summary: str,
        compacted_event_count: int,
        session_id: str | None = None,
    ) -> dict[str, Any]:
        return self._get_backend().store_compaction_summary(
            context_id=context_id,
            summary=summary,
            compacted_event_count=compacted_event_count,
            session_id=session_id,
        )

    # ==========================================================================
    # BOOTSTRAP INTERFACE
    # ==========================================================================

    def _capture_bootstrap_state(self) -> dict[str, Any]:
        return {}

    def _restore_bootstrap_data(self, data: dict[str, Any]) -> None:
        del data  # Unused - bootstrap mode not supported


__all__ = ["MemoryService"]
