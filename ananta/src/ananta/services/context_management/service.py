"""Context Management Service - Facade for context management subsystem.

Assembles sub-services and exposes a single injected entry point.
Orchestrates compaction when context exceeds soft limits.
"""

import logging
import threading
from typing import TYPE_CHECKING, Any

from ananta.error_handling import FrameworkError

from .compaction_types import CompactionPlan, CompactionRequest, WarmingRequest
from .config import ContextManagementConfig
from .content_storage import FileContextContentStorage
from .context_event_store import ContextEventStore
from .context_registry import ContextRegistryService
from .context_sessions import ContextSessionRegistry
from .context_snapshot_store import ContextSnapshotStore
from .context_usage_tracker import ContextUsageTracker
from .types import CONTEXT_EVENT_TO_MESSAGE_ROLE, ContextEventType

if TYPE_CHECKING:
    from ananta.interfaces.inference_service_interface import InferenceServiceInterface
    from ananta.interfaces.memory_service_interface import MemoryServiceInterface
    from ananta.services.state_service import StateService

logger = logging.getLogger(__name__)

# Shared storage namespace - used by all components writing context events
CONTEXT_CONTENT_STORAGE_NAMESPACE = "context_management"


class ContextManagementService:
    """Facade for context management subsystem.

    Provides access to:
    - registry: Create and resolve context IDs
    - events: Manage context event metadata
    - sessions: Per-model context tracking
    - snapshots: Manage compaction snapshots
    - usage: Track event and character counts
    - content_storage: Shared file storage for event content (INPUT/OUTPUT events)

    Content storage is owned by this service and shared with all components
    that need to write or read context event content.
    """

    def __init__(self, state_service: "StateService", app_home: str) -> None:
        """Initialize context management service with dependencies.

        Args:
            state_service: State service for database operations
            app_home: Application home directory for file storage
        """
        self._state_service = state_service
        self._app_home = app_home

        # Initialize sub-services
        self.registry = ContextRegistryService(state_service)
        self.events = ContextEventStore(state_service)
        self.sessions = ContextSessionRegistry(state_service)
        self.snapshots = ContextSnapshotStore(state_service)
        self.usage = ContextUsageTracker()

        # Shared content storage for all context event content
        # All INPUT and OUTPUT events use this single storage location
        self.content_storage = FileContextContentStorage(
            app_home, CONTEXT_CONTENT_STORAGE_NAMESPACE
        )

        # Inference service for compaction (set via set_inference_service)
        self._inference_service: InferenceServiceInterface | None = None

        # Memory service for persisting compaction summaries (required once set)
        self._memory_service: MemoryServiceInterface | None = None

        # Per-context compaction locks to prevent concurrent compactions
        self._compaction_locks: dict[str, threading.Lock] = {}
        self._compaction_locks_guard = threading.Lock()

        # Clear stale plugin root contexts at startup
        self._clear_stale_plugin_root_contexts()

    def set_inference_service(self, inference_service: "InferenceServiceInterface") -> None:
        """Set inference service for compaction operations.

        Called during startup to wire the inference plugin for summary generation
        and cache warming.

        Args:
            inference_service: The inference service implementing generate_compaction_summary
        """
        self._inference_service = inference_service
        logger.debug("InferenceService wired to ContextManagementService for compaction")

    def set_memory_service(self, memory_service: "MemoryServiceInterface") -> None:
        """Set memory service for persisting compaction summaries."""
        self._memory_service = memory_service
        logger.debug("MemoryService wired to ContextManagementService")

    def _clear_stale_plugin_root_contexts(self) -> None:
        """Clear stale plugin root context key-value entries at startup.

        When context_streams is cleared (e.g., database reset) but key-value
        entries persist, plugin root context IDs become orphaned. This clears
        any orphaned entries so fresh contexts can be created.
        """
        from .types import NAMESPACE

        logger.debug("STARTUP_CLEANUP: Checking for stale plugin root contexts")

        entries = self._get_context_kv_entries(NAMESPACE)
        logger.debug(f"STARTUP_CLEANUP: Found {len(entries)} key-value entries to check")

        # Log each entry for debugging stale context issues
        # Context entries have values starting with "ctx-" (plugin root contexts)
        context_entries = [
            e for e in entries
            if str(e.get("value", "")).startswith("ctx-")
        ]
        if context_entries:
            for entry in context_entries:
                logger.debug(
                    f"STARTUP_CLEANUP: Context entry found - key='{entry.get('key')}', "
                    f"value='{entry.get('value')}'"
                )
        else:
            logger.debug("STARTUP_CLEANUP: No context-referencing entries found in key-value store")

        cleared_count = sum(
            1 for entry in entries if self._clear_if_stale(entry, NAMESPACE)
        )

        if cleared_count > 0:
            logger.info(f"Startup cleanup: cleared {cleared_count} stale context key-value entries")
        else:
            logger.debug("STARTUP_CLEANUP: No stale context entries to clear")

    def _get_context_kv_entries(self, namespace: str) -> list[dict[str, Any]]:
        """Get all key-value entries from the context namespace."""
        result = self._state_service.list_key_values(namespace=namespace, scope="GLOBAL")
        data = result.get("data")
        if not isinstance(data, dict):
            logger.debug("STARTUP_CLEANUP: No data from list_key_values")
            return []
        raw_entries = data.get("entries")
        return list(raw_entries) if isinstance(raw_entries, list) else []

    def _clear_if_stale(self, entry: dict[str, Any], namespace: str) -> bool:
        """Clear entry if it references a non-existent context. Returns True if cleared."""
        key = entry.get("key", "")
        value = entry.get("value", "")

        if not value:
            return False

        # Plugin root contexts have values starting with "ctx-"
        if not value.startswith("ctx-"):
            return False

        if self.registry.get_context(str(value)):
            return False  # Context exists, not stale

        self._state_service.delete_key_value(namespace, key, "GLOBAL")
        logger.info(f"Cleared stale context key '{key}': {value}")
        return True

    def _get_compaction_lock(self, context_id: str) -> threading.Lock:
        """Get or create a lock for the given context_id."""
        with self._compaction_locks_guard:
            if context_id not in self._compaction_locks:
                self._compaction_locks[context_id] = threading.Lock()
            return self._compaction_locks[context_id]

    def check_and_trigger_compaction(
        self,
        context_id: str,
        config: ContextManagementConfig,
    ) -> bool:
        """Check if compaction is needed and execute if so.

        Called after each event is appended. Checks if context exceeds soft char limit
        and triggers compaction if auto_compact is enabled.

        Compaction is purely char-count based.

        Uses per-context locking to prevent concurrent compactions.

        Args:
            context_id: Context stream ID
            config: Plugin configuration with thresholds

        Returns:
            True if compaction was triggered, False otherwise.

        Raises:
            FrameworkError: If hard char limit exceeded (fail-fast protection).
            FrameworkError: If compaction fails (fail-fast).
        """
        # Get current usage from events
        all_events = self.events.list_all_events(context_id)
        _, char_count = self.usage.compute_usage_delta(all_events)

        # Include snapshot summary chars in total (snapshot replaces compacted events)
        latest_snapshot = self.snapshots.get_latest_snapshot(context_id)
        if latest_snapshot:
            raw_char_count = latest_snapshot.get("summary_char_count")
            # FAIL-FAST: summary_char_count is required for limit accounting
            if raw_char_count is None:
                raise FrameworkError(
                    message="Snapshot missing summary_char_count - data corruption detected",
                    error_code="context_management.snapshot_missing_char_count",
                    details={"snapshot_id": latest_snapshot.get("id"), "context_id": context_id},
                )
            # Convert to int (DB adapters may return string)
            try:
                snapshot_char_count = int(raw_char_count)
            except (ValueError, TypeError) as e:
                raise FrameworkError(
                    message=f"Invalid snapshot summary_char_count: {raw_char_count!r}",
                    error_code="context_management.invalid_char_count",
                    details={"snapshot_id": latest_snapshot.get("id"), "raw_value": raw_char_count},
                ) from e
            char_count += snapshot_char_count

        # Enforce hard char limit (fail-fast)
        self._enforce_hard_limit(context_id, char_count, config)

        if not config.auto_compact or not config.supports_compaction:
            return False

        # Check against soft char limit
        if not self.usage.should_compact(char_count, config.soft_max_char_count):
            return False

        # Acquire per-context lock (non-blocking - skip if another compaction in progress)
        lock = self._get_compaction_lock(context_id)
        if not lock.acquire(blocking=False):
            logger.debug(f"Skipping compaction for {context_id}: already in progress")
            return False

        try:
            logger.info(
                "COMPACTION_TRIGGERED",
                extra={
                    "context_id": context_id,
                    "char_count": char_count,
                    "soft_max_char_count": config.soft_max_char_count,
                },
            )

            # Build and execute compaction plan
            plan = self._build_compaction_plan(context_id, all_events, config)
            self._execute_compaction(plan, config)
            return True
        finally:
            lock.release()

    def _enforce_hard_limit(
        self,
        context_id: str,
        char_count: int,
        config: ContextManagementConfig,
    ) -> None:
        """Enforce hard char limit (fail-fast protection).

        Called before any compaction check. If context exceeds hard char limit,
        raise FrameworkError to prevent unbounded context growth.

        Args:
            context_id: Context stream ID
            char_count: Current character count
            config: Plugin configuration with hard limit

        Raises:
            FrameworkError: If char_count exceeds max_char_count.
        """
        if char_count > config.max_char_count:
            raise FrameworkError(
                message=f"Context exceeded hard char limit: {char_count} > {config.max_char_count}",
                error_code="context_management.hard_limit_exceeded",
                details={
                    "context_id": context_id,
                    "char_count": char_count,
                    "max_char_count": config.max_char_count,
                },
            )

    def _build_compaction_plan(
        self,
        context_id: str,
        all_events: list[dict[str, Any]],
        config: ContextManagementConfig,
    ) -> CompactionPlan:
        """Build a compaction plan from current context state.

        Determines which events to summarize, which to keep based on target_char_count.
        Compaction is purely char-count based.

        Args:
            context_id: Context stream ID
            all_events: All events in the context (chronological order)
            config: Plugin configuration with targets

        Returns:
            CompactionPlan with all details for execution.

        Raises:
            FrameworkError: If insufficient events for compaction.
        """
        if len(all_events) < 2:
            raise FrameworkError(
                message="Insufficient events for compaction (need at least 2)",
                error_code="context_management.compaction_insufficient_events",
                details={
                    "context_id": context_id,
                    "event_count": len(all_events),
                },
            )

        # Reserve space for the summary before calculating the keep-budget.
        # Without this, kept events greedily consume target_char_count, leaving
        # near-zero chars for the summary (which needs min_summary_tokens * chars_per_token).
        min_summary_chars = config.min_summary_tokens * config.chars_per_token
        keep_budget = config.target_char_count - min_summary_chars

        events_to_keep, events_to_summarize = self._split_events_by_char_budget(
            all_events, keep_budget
        )

        # Must have at least one event to summarize
        if not events_to_summarize:
            raise FrameworkError(
                message="No events to summarize - all events fit within target",
                error_code="context_management.compaction_nothing_to_summarize",
                details={
                    "context_id": context_id,
                    "event_count": len(all_events),
                    "target_char_count": config.target_char_count,
                },
            )

        # Get existing summary (latest snapshot)
        latest_snapshot = self.snapshots.get_latest_snapshot(context_id)
        existing_summary: str | None = None
        if latest_snapshot:
            summary_path = latest_snapshot.get("summary_path")
            # FAIL-FAST: Empty summary_path indicates data corruption (DB schema requires NOT NULL)
            if not summary_path:
                raise FrameworkError(
                    message="Snapshot has empty summary_path - data corruption detected",
                    error_code="context_management.snapshot_missing_summary_path",
                    details={
                        "snapshot_id": latest_snapshot.get("id"),
                        "context_id": context_id,
                    },
                )
            existing_summary = self.content_storage.read_text(str(summary_path))

        # Calculate summary budget
        kept_char_count = sum(int(e.get("content_char_count", 0)) for e in events_to_keep)
        summary_budget_chars = config.target_char_count - kept_char_count

        # FAIL-FAST: Reject zero or negative budget
        # This happens when kept events already exceed target_char_count
        if summary_budget_chars <= 0:
            raise FrameworkError(
                message="Insufficient summary budget - kept events exceed target char count",
                error_code="context_management.compaction_zero_budget",
                details={
                    "context_id": context_id,
                    "kept_char_count": kept_char_count,
                    "target_char_count": config.target_char_count,
                    "summary_budget_chars": summary_budget_chars,
                },
            )

        # Convert events to messages for LLM
        messages_to_summarize = self._events_to_messages(events_to_summarize)

        # Events for cache warming (most recent of the kept events, within char budget)
        warming_events = self._select_warming_events(events_to_keep, config.precache_char_count)
        recent_events_for_warming = self._events_to_messages(warming_events)

        # Calculate char counts
        compacted_char_count = sum(
            int(e.get("content_char_count", 0)) for e in events_to_summarize
        )
        remaining_char_count = kept_char_count

        # Summary max tokens
        summary_max_tokens = summary_budget_chars // config.chars_per_token

        # FAIL-FAST: Reject insufficient token budget
        if summary_max_tokens < config.min_summary_tokens:
            raise FrameworkError(
                message=f"Summary token budget too small (min {config.min_summary_tokens} required)",
                error_code="context_management.compaction_insufficient_tokens",
                details={
                    "context_id": context_id,
                    "summary_max_tokens": summary_max_tokens,
                    "min_summary_tokens": config.min_summary_tokens,
                    "summary_budget_chars": summary_budget_chars,
                    "chars_per_token": config.chars_per_token,
                },
            )

        return CompactionPlan(
            context_id=context_id,
            reason="soft_limit_exceeded",
            start_event_id=str(events_to_summarize[0]["id"]),
            end_event_id=str(events_to_summarize[-1]["id"]),
            messages_to_summarize=messages_to_summarize,
            existing_summary=existing_summary,
            summary_budget_chars=summary_budget_chars,
            summary_max_tokens=summary_max_tokens,
            summary_temperature=config.summary_temperature,
            compacted_event_count=len(events_to_summarize),
            compacted_char_count=compacted_char_count,
            remaining_event_count=len(events_to_keep),
            remaining_char_count=remaining_char_count,
            # Use newest kept event (last in chronological order) for cursor
            last_kept_event_id=str(events_to_keep[-1]["id"]),
            last_kept_event_created_at=str(events_to_keep[-1]["created_at"]),
            recent_events_for_warming=recent_events_for_warming,
        )

    def _split_events_by_char_budget(
        self,
        all_events: list[dict[str, Any]],
        target_char_count: int,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        """Split events into keep vs summarize based on char budget.

        Keeps the most recent events that fit within target_char_count.
        All older events are marked for summarization.

        Args:
            all_events: All events in chronological order
            target_char_count: Max chars to keep in retained events

        Returns:
            Tuple of (events_to_keep, events_to_summarize)
        """
        total_chars = 0
        keep_start_index = len(all_events)

        # Iterate from most recent (end) to oldest (start)
        for i in range(len(all_events) - 1, -1, -1):
            event_chars = int(all_events[i].get("content_char_count", 0))
            if total_chars + event_chars > target_char_count:
                break
            total_chars += event_chars
            keep_start_index = i

        # At minimum keep the most recent event (even if it exceeds target)
        if keep_start_index == len(all_events):
            keep_start_index = len(all_events) - 1

        events_to_keep = all_events[keep_start_index:]
        events_to_summarize = all_events[:keep_start_index]

        return events_to_keep, events_to_summarize

    def _select_warming_events(
        self,
        events_to_keep: list[dict[str, Any]],
        max_char_count: int,
    ) -> list[dict[str, Any]]:
        """Select warming events within char count limit.

        Takes the most recent events from events_to_keep (end of list),
        limited by max_char_count (precache_char_count).

        Args:
            events_to_keep: Events that will remain after compaction (chronological order)
            max_char_count: Maximum total chars for warming (precache_char_count)

        Returns:
            Selected events for cache warming (most recent, within char limit).
        """
        total_chars = 0
        selected: list[dict[str, Any]] = []

        # Iterate from most recent (end) to oldest (start)
        for event in reversed(events_to_keep):
            event_chars = int(event.get("content_char_count", 0))
            if total_chars + event_chars > max_char_count:
                break
            selected.append(event)
            total_chars += event_chars

        # Reverse to restore chronological order
        selected.reverse()
        return selected

    def _events_to_messages(
        self,
        events: list[dict[str, Any]],
    ) -> list[dict[str, str]]:
        """Convert events to LLM messages format.

        Reads content from files and maps event types to roles.

        Args:
            events: Event metadata dicts with content_path

        Returns:
            List of {role, content} message dicts.
        """
        messages: list[dict[str, str]] = []
        for event in events:
            content_path = event.get("content_path")
            # FAIL-FAST: Missing content_path indicates data corruption
            if not content_path:
                raise FrameworkError(
                    message="Event missing required content_path",
                    error_code="context_management.missing_content_path",
                    details={"event_id": event.get("id"), "event_type": event.get("event_type")},
                )

            content = self.content_storage.read_text(str(content_path))
            event_type_str = str(event.get("event_type", ""))

            # FAIL-FAST: Map event type to role (no fallback on unknown types)
            try:
                event_type = ContextEventType(event_type_str)
            except ValueError as e:
                raise FrameworkError(
                    message=f"Unknown event type '{event_type_str}' in context event",
                    error_code="context_management.invalid_event_type",
                    details={"event_id": event.get("id"), "event_type": event_type_str},
                ) from e

            role = CONTEXT_EVENT_TO_MESSAGE_ROLE.get(event_type)
            if role is None:
                raise FrameworkError(
                    message=f"No role mapping for event type '{event_type_str}'",
                    error_code="context_management.unmapped_event_type",
                    details={"event_id": event.get("id"), "event_type": event_type_str},
                )

            messages.append({"role": role.value, "content": content})

        return messages

    def _execute_compaction(
        self,
        plan: CompactionPlan,
        config: ContextManagementConfig,
    ) -> None:
        """Execute a compaction plan.

        1. Generate summary via inference service
        2. Store summary content to file
        3. Create snapshot record
        4. Soft-delete old events
        5. Clean up process keys
        6. Optionally warm cache

        Args:
            plan: The compaction plan to execute
            config: Plugin configuration

        Raises:
            FrameworkError: If inference service not wired or compaction fails.
        """
        if not self._inference_service:
            raise FrameworkError(
                message="InferenceService not wired for compaction. "
                "Call set_inference_service() during startup.",
                error_code="context_management.inference_service_not_wired",
                details={"context_id": plan.context_id},
            )

        # Ensure session exists before any session updates (fixes never-created session issue)
        self.sessions.ensure_session_exists(
            context_id=plan.context_id,
            context_mode=config.context_mode.value,
        )

        # 1. Generate summary
        compaction_request = CompactionRequest(
            context_id=plan.context_id,
            messages_to_summarize=plan.messages_to_summarize,
            existing_summary=plan.existing_summary,
            summary_budget_chars=plan.summary_budget_chars,
            max_tokens=plan.summary_max_tokens,
            temperature=plan.summary_temperature,
            reason=plan.reason,
        )

        summary = self._inference_service.generate_compaction_summary(compaction_request)

        # 2. Store summary content to file
        summary_path, summary_char_count = self.content_storage.store_snapshot(
            plan.context_id, summary
        )

        # 3. Create snapshot record
        snapshot_id = self.snapshots.create_snapshot(
            context_id=plan.context_id,
            start_event_id=plan.start_event_id,
            end_event_id=plan.end_event_id,
            summary_path=summary_path,
            summary_char_count=summary_char_count,
            original_char_count=plan.compacted_char_count,
        )

        # 4. Soft-delete old events
        deleted_count = self.events.soft_delete_events_before(
            plan.context_id, plan.end_event_id
        )

        # 5. Update session cursor position
        # Cursor moves to first kept event after compaction
        # Note: Count tracking removed - limits use fresh event counts
        self.sessions.update_cursor(
            context_id=plan.context_id,
            last_event_id=plan.last_kept_event_id,
            last_event_created_at=plan.last_kept_event_created_at,
        )

        # 6. Update cache_state to COLD (compaction invalidates cache)
        from ananta.services.context_management.types import ContextCacheState

        self.sessions.update_cache_state(
            context_id=plan.context_id,
            cache_state=ContextCacheState.COLD.value,
        )

        # 7. Persist compaction summary as long-term memory (required)
        if self._memory_service is None:
            raise FrameworkError(
                message="Cannot persist compaction summary: memory_service not wired",
                error_code="context.memory_not_wired",
            )
        self._memory_service.store_compaction_summary(
            context_id=plan.context_id,
            summary=summary,
            compacted_event_count=plan.compacted_event_count,
            session_id=None,
        )

        logger.info(
            "COMPACTION_COMPLETE",
            extra={
                "context_id": plan.context_id,
                "snapshot_id": snapshot_id,
                "summary_char_count": summary_char_count,
                "deleted_event_count": deleted_count,
                "compacted_char_count": plan.compacted_char_count,
            },
        )

        # 7. Optionally warm cache
        if config.warming_enabled and plan.recent_events_for_warming:
            # Update state to WARMING before triggering
            self.sessions.update_cache_state(
                context_id=plan.context_id,
                cache_state=ContextCacheState.WARMING.value,
            )

            warming_request = WarmingRequest(
                context_id=plan.context_id,
                snapshot_id=snapshot_id,
                messages=plan.recent_events_for_warming,
                max_tokens=config.warm_max_tokens,
                temperature=config.warm_temperature,
            )
            # warm_cache returns True on success, raises PluginError on failure
            self._inference_service.warm_cache(warming_request)

            # Update state to WARM after successful warming
            self.sessions.update_cache_state(
                context_id=plan.context_id,
                cache_state=ContextCacheState.WARM.value,
            )
