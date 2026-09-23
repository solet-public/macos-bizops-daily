"""ContextStage - Injects conversation and memory context.

Platform mode: Retrieves context events from the context management service.
Delegated mode: Retrieves memories from PromptContextBuilder.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from ananta.core.plans import parse as parse_plan
from ananta.core.prompts.context import ACTIVE_PLAN_MARKER, PromptContext
from ananta.core.prompts.stages.context_attachments import add_attachment_summary
from ananta.core.prompts.stages.context_timestamps import append_event_timestamp
from ananta.core.prompts.stages.context_wbs import (
    deduplicate_focused_history_entries,
    extract_focused_plan_text,
    trim_wbs_execution_history,
)
from ananta.services.context_management.types import (
    CONTEXT_EVENT_TO_MESSAGE_ROLE,
    TIMESTAMPED_EVENT_TYPES,
    ContextEventType,
    MessageRole,
)

if TYPE_CHECKING:
    from ananta.core.services.prompt_context_builder import PromptContextBuilder
    from ananta.services.context_management.config import ContextManagementConfig
    from ananta.services.context_management.content_storage import (
        FileContextContentStorage,
    )
    from ananta.services.context_management.service import ContextManagementService

logger = logging.getLogger(__name__)

# Keys for extracting flow_input from resolved_action_params (no magic strings)
PROMPT_KEY = "prompt"
PROMPT_USER_KEY = "user"
FLOW_INPUT_KEY = "flow_input"
ORIGINAL_INPUT_KEY = "original_input"

# Key for skipping semantic recall (used by specialized inference that should not
# pollute the memory system with intermediate queries).
SKIP_SEMANTIC_RECALL_KEY = "skip_semantic_recall"

# Key for skipping conversation history loading.
SKIP_CONVERSATION_HISTORY_KEY = "skip_conversation_history"

# Callable type for reading playbook sections: (playbook_id, section_id) -> section_content
PlaybookSectionReader = Callable[[str, str], str]


class ContextStage:
    """Injects conversation and memory context.

    Platform mode (context_id set):
    - conversation_history from context events (INPUT/OUTPUT)
    - relevant_memories/identity_memories from PromptContextBuilder

    Delegated mode (no context_id):
    - relevant_memories/identity_memories from PromptContextBuilder
    - Model manages its own conversation history

    Records what was injected for observability.
    """

    name = "context"

    def __init__(
        self,
        context_builder: PromptContextBuilder | None,
        context_management_service: ContextManagementService | None = None,
        content_storage: FileContextContentStorage | None = None,
        context_config: ContextManagementConfig | None = None,
        memory_service: object | None = None,
        playbook_section_reader: PlaybookSectionReader | None = None,
    ) -> None:
        """Initialize with context sources.

        Args:
            context_builder: PromptContextBuilder for memory-based context
            context_management_service: Service for platform-managed context
            content_storage: File storage for reading context event content
            context_config: Configuration for context management behavior
            memory_service: Memory service for focus buffer retrieval
            playbook_section_reader: Callable to read playbook sections by (playbook_id, section_id)
        """
        self._builder = context_builder
        self._context_service = context_management_service
        self._content_storage = content_storage
        self._config = context_config
        self._memory_service = memory_service
        self._playbook_section_reader = playbook_section_reader

    def _load_focused_memories(self, ctx: PromptContext) -> None:
        """Load focused memories from the focus buffer.

        Focused memories get highest priority in context rendering.
        Gracefully handles missing memory service (focus buffer is optional).
        Respects profile_include_focused_memories flag.
        """
        if not ctx.profile_include_focused_memories:
            ctx.add_decision(self.name, "Focused memories: skipped (profile flag)")
            return
        if not self._memory_service:
            return

        get_focused = getattr(self._memory_service, "get_focused", None)
        if not callable(get_focused):
            return

        # Focus is session-scoped (JOS-02): assemble THIS session's pins.
        # A session-less assembly has no focus by definition (V-5 ruling).
        if not ctx.session_id:
            ctx.add_decision(self.name, "Focused memories: skipped (no session)")
            return

        envelope = get_focused(session_id=ctx.session_id)
        if not isinstance(envelope, dict):
            raise TypeError(
                "memory_service.get_focused() must return the "
                '{"memories": [...], "count": N} envelope, '
                f"got {type(envelope).__name__}"
            )

        focused = envelope.get("memories")
        if isinstance(focused, list) and focused:
            ctx.focused_memories = focused
            ctx.has_focused_plan = any(
                isinstance(m.get("content"), str) and ACTIVE_PLAN_MARKER in m["content"]
                for m in focused
            )
            ctx.add_decision(self.name, f"Focused memories: {len(focused)} items")

    def _load_playbook_section(self, ctx: PromptContext) -> None:
        """Hydrate the playbook section for the current plan step.

        Uses the canonical plan parser to find the current ``[>]`` step
        and extract ``PLAYBOOK`` / ``PLAYBOOK_SECTION`` metadata.  If both
        are present, reads the section content via the playbook_section_reader
        callable and stores it on ``ctx.playbook_section_content``.

        Fail-fast: raises ValueError if PLAYBOOK_SECTION is present but the
        section cannot be read (missing playbook, missing section ID).
        """
        if not self._playbook_section_reader:
            return
        if not ctx.has_focused_plan:
            return

        plan_text = self._extract_focused_plan_text(ctx)
        if not plan_text:
            return

        parsed = parse_plan(plan_text)
        current = parsed.current_step
        if current is None:
            return

        if not current.playbook_section_id:
            return  # No section reference on this step — no hydration needed

        section_id = current.playbook_section_id

        if not current.playbook_id:
            raise ValueError(
                f"PLAYBOOK_SECTION: {section_id} found without PLAYBOOK on current step. "
                "Both are required for playbook section hydration."
            )

        playbook_id = current.playbook_id

        # Read the section content — fail-fast on missing section
        section_content = self._playbook_section_reader(playbook_id, section_id)

        ctx.playbook_section_content = section_content
        ctx.playbook_section_id = section_id
        ctx.add_decision(
            self.name,
            f"Playbook section hydrated: {playbook_id}/{section_id} ({len(section_content)} chars)",
        )

    @staticmethod
    def _extract_focused_plan_text(ctx: PromptContext) -> str | None:
        """Extract focused plan text from focused memories."""
        return extract_focused_plan_text(ctx)

    def _require_original_input(self, ctx: PromptContext) -> str:
        """Extract original user input for memory recall.

        Source of truth: flow_input.original_input from resolved_action_params.
        This is the user's actual request, not the action result wrapper.

        Args:
            ctx: PromptContext with resolved_action_params

        Returns:
            Original user input string

        Raises:
            ValueError: If flow_input or original_input is missing/malformed
        """
        prompt = ctx.resolved_action_params.get(PROMPT_KEY, {})
        user = prompt.get(PROMPT_USER_KEY, {}) if isinstance(prompt, dict) else {}
        flow_input_raw = user.get(FLOW_INPUT_KEY) if isinstance(user, dict) else None

        if not flow_input_raw:
            raise ValueError(
                f"{PROMPT_KEY}.{PROMPT_USER_KEY}.{FLOW_INPUT_KEY} is required for memory recall"
            )

        # flow_input is a JSON string from template function
        if isinstance(flow_input_raw, str):
            try:
                flow_input = json.loads(flow_input_raw)
            except json.JSONDecodeError as e:
                raise ValueError(f"{FLOW_INPUT_KEY} must be valid JSON: {e}") from e
        elif isinstance(flow_input_raw, dict):
            flow_input = flow_input_raw
        else:
            raise ValueError(f"{FLOW_INPUT_KEY} must be a JSON string or dict")

        original_input = flow_input.get(ORIGINAL_INPUT_KEY)
        if not isinstance(original_input, str):
            raise ValueError(f"{FLOW_INPUT_KEY}.{ORIGINAL_INPUT_KEY} must be a string")

        return original_input

    def _get_recall_query(self, ctx: PromptContext) -> str:
        """Get query for memory recall.

        Extracts flow_input.original_input from resolved action params.
        This is the user's actual request, used for semantic recall.

        Contract (fail-fast, no fallback):
        - Non-empty string: Use for semantic recall
        - Empty string in original_input: System-triggered flow, skip semantic recall
        - Missing/invalid flow_input: Fail-fast (configuration error)

        Args:
            ctx: PromptContext with resolved_action_params

        Returns:
            Recall query string (empty for system-triggered flows)

        Raises:
            ValueError: If flow_input is missing or malformed (fail-fast).
        """
        return self._require_original_input(ctx)

    def execute(self, ctx: PromptContext) -> PromptContext:
        """Inject conversation and memory context.

        Platform mode (context_id set):
        - Loads conversation history from context events
        - Loads relevant/identity memories from memory service
        - FAIL-FAST if context_management_service or content_storage missing

        Delegated mode (no context_id):
        - Loads relevant/identity memories from memory service
        - Model manages its own conversation history
        - APIStage will use cache-friendly ordering

        If skip_semantic_recall=True in raw_action_params, semantic recall is skipped.
        This prevents memory pollution during intent classification.
        See: knowledge_base/2026-02-05_claude_memory_system_refactor_v2.md

        Args:
            ctx: PromptContext with optional context_id

        Returns:
            Same context with memories set (and conversation_history for platform mode)
        """
        skip_semantic_recall = (
            bool(ctx.raw_action_params.get(SKIP_SEMANTIC_RECALL_KEY, False))
            or not ctx.profile_include_semantic_recall
        )
        skip_conversation_history = (
            bool(ctx.raw_action_params.get(SKIP_CONVERSATION_HISTORY_KEY, False))
            or not ctx.profile_include_conversation_history
        )

        # Delegated mode: no context_id means model manages its own context
        # but we still populate memories for the model to use
        if not ctx.context_id:
            return self._execute_delegated_mode(ctx, skip_semantic_recall=skip_semantic_recall)

        # Platform mode: FAIL-FAST if services missing
        if not self._context_service:
            raise ValueError(
                "ContextStage requires context_management_service for platform mode. "
                "Ensure context_management_service is injected."
            )
        if not self._content_storage:
            raise ValueError(
                "ContextStage requires content_storage for platform mode. "
                "Ensure content_storage is injected."
            )

        return self._execute_with_platform_context(
            ctx,
            skip_semantic_recall=skip_semantic_recall,
            skip_conversation_history=skip_conversation_history,
        )

    def _execute_delegated_mode(
        self, ctx: PromptContext, *, skip_semantic_recall: bool = False
    ) -> PromptContext:
        """Build memories for delegated mode.

        Uses PromptContextBuilder to populate relevant_memories/identity_memories.
        The model manages its own conversation history, but we provide memory context.

        Args:
            ctx: PromptContext without context_id
            skip_semantic_recall: If True, skip semantic recall (intent classification mode)

        Returns:
            Same context with relevant_memories/identity_memories set from builder
        """
        # Builder is REQUIRED for memory retrieval
        if not self._builder:
            raise ValueError(
                "ContextStage requires context_builder for memory retrieval. "
                "Ensure context_builder is injected."
            )

        # When semantic recall is disabled, do not require flow_input.original_input.
        # This allows artifact authoring profiles to skip recall without needing
        # to provide a flow_input shim.
        if skip_semantic_recall:
            recall_query = ""
        else:
            # Use original user input for recall, not action result wrapper
            # Fail-fast if flow_input.original_input is missing (but empty string is valid)
            recall_query = self._get_recall_query(ctx)

        # Skip semantic recall in these cases:
        # 1. Empty recall query = system-triggered flow (process_error, process_results)
        # 2. skip_semantic_recall=True = intent classification mode (prevents memory pollution)
        if skip_semantic_recall:
            ctx.add_decision(
                self.name, "Semantic recall skipped: profile or skip flag"
            )
            llm_context = self._builder.build_context(
                session_id=ctx.session_id,
                user_input="",  # Empty query = skip semantic recall
                max_recent_events=0,  # Delegated mode: model manages its own history
            )
        elif not recall_query:
            ctx.add_decision(
                self.name, "Semantic recall skipped: empty original_input (system-triggered flow)"
            )
            # Get identity memories without semantic recall
            llm_context = self._builder.build_context(
                session_id=ctx.session_id,
                user_input="",  # Empty query = skip semantic recall
                max_recent_events=0,  # Delegated mode: model manages its own history
            )
        else:
            llm_context = self._builder.build_context(
                session_id=ctx.session_id,
                user_input=recall_query,
                max_recent_events=0,  # Delegated mode: model manages its own history
            )

        # Load focused memories (highest priority)
        self._load_focused_memories(ctx)

        # Hydrate playbook section for current plan step (after focused memories are loaded)
        self._load_playbook_section(ctx)

        # Set memories from builder results
        ctx.relevant_memories = llm_context.relevant_memories or []
        ctx.identity_memories = llm_context.identity_memories or []
        if recall_query and not skip_semantic_recall:  # Only log count if we did semantic recall
            ctx.add_decision(self.name, f"Relevant memories: {len(ctx.relevant_memories)} memories")
        ctx.add_decision(self.name, f"Identity memories: {len(ctx.identity_memories)} items")

        # CRITICAL: Identity must NEVER be empty - fail-fast if retrieval failed
        if not ctx.identity_memories:
            raise ValueError(
                "Identity memories are empty - the system cannot function without identity. "
                f"Check that memories with tag 'identity' exist. flow_id={ctx.flow_id}"
            )

        return ctx

    def _execute_with_platform_context(
        self,
        ctx: PromptContext,
        *,
        skip_semantic_recall: bool = False,
        skip_conversation_history: bool = False,
    ) -> PromptContext:
        """Build context from platform-managed context events.

        For platform context, we populate conversation_history with properly
        role-mapped messages. Memories (relevant/identity) come from the builder.

        Precondition: execute() has validated that _context_service, _content_storage,
        and ctx.context_id are all set. No fallback behavior.

        Args:
            ctx: PromptContext with context_id set
            skip_semantic_recall: If True, skip semantic recall
            skip_conversation_history: If True, skip loading conversation events

        Returns:
            Same context with conversation_history from context events, memories from builder

        Raises:
            AssertionError: If preconditions violated (programming error).
        """
        # These are preconditions validated by execute() - assert to catch programming errors
        assert self._context_service is not None, "execute() must validate _context_service"
        assert self._content_storage is not None, "execute() must validate _content_storage"
        assert ctx.context_id, "execute() must validate ctx.context_id"

        # Build conversation history from snapshot + events
        if skip_conversation_history:
            ctx.add_decision(
                self.name,
                "Conversation history skipped: skip_conversation_history=True",
            )
            events: list[dict[str, Any]] = []
        else:
            conversation_history, events = self._build_conversation_history(ctx)
            ctx.conversation_history = conversation_history

        # Set stored system events flag for APIStage
        ctx.has_stored_system_events = self._context_service.events.has_system_events(
            ctx.context_id
        )
        ctx.add_decision(
            self.name,
            f"Platform context: {len(events)} events, {len(ctx.conversation_history)} loaded, "
            f"has_stored_system={ctx.has_stored_system_events}",
        )

        # Add recent attachment info
        self._add_attachment_summary(ctx)

        # Build memories (relevant/identity) — also loads focused memories
        self._build_memories(ctx, skip_semantic_recall=skip_semantic_recall)

        # Deduplicate conversation history against focused memories.
        # Must run AFTER _build_memories because it needs focused_memories.
        self._deduplicate_focused_history(ctx)

        # Trim conversation history for WBS execution steps.
        # Must run AFTER _build_memories because it needs focused_memories
        # to detect the ACTIVE_WBS marker and parse the current step.
        self._trim_wbs_execution_history(ctx)

        # Post-completion trim: when the plan has completed and been
        # defocused, the conversation history may still be enormous
        # from WBS execution.  Apply foundation-only trimming so
        # process_results and other post-completion steps can fit
        # in context.
        self._trim_post_completion_history(ctx)

        return ctx

    def _deduplicate_focused_history(self, ctx: PromptContext) -> None:
        """Remove conversation history entries that duplicate focused memories."""
        deduplicate_focused_history_entries(ctx, stage_name=self.name)

    _logger = logging.getLogger(__name__)

    def _trim_wbs_execution_history(self, ctx: PromptContext) -> None:
        """Apply compact execution regime to conversation history."""
        trim_wbs_execution_history(ctx, stage_name=self.name)

    _POST_COMPLETION_HISTORY_THRESHOLD = 20

    def _trim_post_completion_history(self, ctx: PromptContext) -> None:
        """Trim oversized history after plan completion.

        When the plan has completed (no focused plan) and the
        conversation history is large, apply the same foundation-only
        trim used for WBS execution.  This prevents process_results
        and other post-completion steps from overflowing the context.
        """
        if ctx.has_focused_plan:
            return
        if len(ctx.conversation_history) <= self._POST_COMPLETION_HISTORY_THRESHOLD:
            return
        from ananta.core.prompts.stages.context_wbs import _extract_foundation_messages
        original_count = len(ctx.conversation_history)
        ctx.conversation_history = _extract_foundation_messages(
            ctx.conversation_history,
        )
        self._logger.info(
            "POST_COMPLETION_TRIM: %d -> %d messages (plan completed, no focused plan)",
            original_count, len(ctx.conversation_history),
        )

    def _build_conversation_history(
        self, ctx: PromptContext
    ) -> tuple[list[dict[str, str]], list[dict[str, Any]]]:
        """Build conversation history from snapshot and events.

        Returns:
            Tuple of (conversation_history, events)
        """
        assert self._context_service is not None
        assert self._content_storage is not None
        assert ctx.context_id is not None

        conversation_history: list[dict[str, str]] = []
        snapshot = self._context_service.snapshots.get_latest_snapshot(ctx.context_id)

        if snapshot:
            self._load_snapshot_summary(ctx, snapshot, conversation_history)
            end_event_id = str(snapshot.get("end_event_id", ""))
            events = self._context_service.events.list_events_after_snapshot(
                ctx.context_id, end_event_id
            )
        else:
            events = self._context_service.events.list_all_events(ctx.context_id)

        self._load_events_into_history(ctx, events, conversation_history)
        return conversation_history, events

    def _load_snapshot_summary(
        self,
        ctx: PromptContext,
        snapshot: dict[str, Any],
        conversation_history: list[dict[str, str]],
    ) -> None:
        """Load snapshot summary into conversation history."""
        assert self._content_storage is not None

        summary_path = str(snapshot.get("summary_path", ""))
        # FAIL-FAST: Empty summary_path indicates data corruption (DB schema requires NOT NULL)
        if not summary_path:
            raise ValueError(
                f"Snapshot has empty summary_path - data corruption detected. "
                f"snapshot_id={snapshot.get('id')}, context_id={ctx.context_id}"
            )

        try:
            summary_content = self._content_storage.read_text(summary_path)
            conversation_history.append(
                {"role": MessageRole.SYSTEM.value, "content": f"[Previous context summary]\n{summary_content}"}
            )
            ctx.add_decision(
                self.name,
                f"Loaded snapshot {snapshot.get('id')} "
                f"({snapshot.get('summary_char_count', 0)} chars)",
            )
        except FileNotFoundError as e:
            raise ValueError(
                f"Snapshot file not found: {summary_path}. "
                f"Data corruption detected - snapshot record exists but file missing. "
                f"context_id={ctx.context_id}, snapshot_id={snapshot.get('id')}"
            ) from e

    def _load_events_into_history(
        self,
        ctx: PromptContext,
        events: list[dict[str, Any]],
        conversation_history: list[dict[str, str]],
    ) -> None:
        """Load event content into conversation history.

        SYSTEM events are skipped because system messages (prompt + identity)
        should be built fresh on each request from current config and memories,
        not loaded from stale stored events.
        """
        assert self._content_storage is not None

        for event in events:
            self._process_single_event(ctx, event, conversation_history)

    @staticmethod
    def _should_skip_event(event: dict[str, Any]) -> bool:
        """Return True for events that should not appear in conversation history."""
        event_type_str = str(event.get("event_type", ""))
        # SYSTEM events — rebuilt fresh each request from identity memories
        if event_type_str == ContextEventType.SYSTEM.value:
            return True
        # inference_reasoning — step_summary is internal bookkeeping,
        # kept in storage for audit (event_persistence.py:667)
        meta = event.get("metadata") or {}
        if meta.get("source") == "inference_reasoning":
            return True
        return False

    def _process_single_event(
        self,
        ctx: PromptContext,
        event: dict[str, Any],
        conversation_history: list[dict[str, str]],
    ) -> None:
        assert self._content_storage is not None

        if self._should_skip_event(event):
            return

        event_meta = event.get("metadata") or {}
        content_path = str(event.get("content_path", ""))
        # FAIL-FAST: Missing content_path indicates data corruption
        if not content_path:
            raise ValueError(
                f"Event missing required content_path. "
                f"context_id={ctx.context_id}, event_id={event.get('id')}"
            )

        event_type_str = str(event.get("event_type", ""))
        try:
            content = self._content_storage.read_text(content_path)
            event_type = ContextEventType(event_type_str)
        except ValueError as e:
            raise ValueError(
                f"Unknown event type '{event_type_str}' in context event. "
                f"context_id={ctx.context_id}, event_id={event.get('id')}"
            ) from e
        except FileNotFoundError as e:
            raise ValueError(
                f"Event content file not found: {content_path}. "
                f"Data corruption detected - event record exists but file missing. "
                f"context_id={ctx.context_id}, event_id={event.get('id')}"
            ) from e

        role = CONTEXT_EVENT_TO_MESSAGE_ROLE.get(event_type)
        if role is None:
            raise ValueError(
                f"No role mapping for event type '{event_type_str}'. "
                f"context_id={ctx.context_id}, event_id={event.get('id')}"
            )
        if event_type in TIMESTAMPED_EVENT_TYPES:
            content = append_event_timestamp(content, event_type, event)
        conversation_history.append({"role": role.value, "content": content})
        # Track source_memory_id for ID-based focus dedup
        source_mem_id = event_meta.get("source_memory_id", "")
        if source_mem_id:
            ctx.history_memory_ids.add(source_mem_id)

    def _add_attachment_summary(self, ctx: PromptContext) -> None:
        """Add recent attachment summary to context if available."""
        add_attachment_summary(
            ctx,
            session_id=ctx.session_id,
            builder=self._builder,
            config=self._config,
            stage_name=self.name,
        )

    def _build_memories(
        self, ctx: PromptContext, *, skip_semantic_recall: bool = False
    ) -> None:
        """Build relevant/identity memories from memory service.

        Args:
            ctx: PromptContext to populate
            skip_semantic_recall: If True, skip semantic recall (intent classification mode)
        """
        if not self._builder:
            raise ValueError(
                "ContextStage requires context_builder for memory retrieval. "
                "Ensure context_builder is injected."
            )

        # When semantic recall is disabled, do not require flow_input.original_input.
        if skip_semantic_recall:
            recall_query = ""
        else:
            recall_query = self._get_recall_query(ctx)

        # Skip semantic recall in these cases:
        # 1. skip_semantic_recall=True = intent classification mode (prevents memory pollution)
        # 2. Empty recall query = system-triggered flow (process_error, process_results)
        if skip_semantic_recall:
            ctx.add_decision(
                self.name, "Semantic recall skipped: intent classification mode (skip_semantic_recall=True)"
            )
            llm_context = self._builder.build_context(
                session_id=ctx.session_id,
                user_input="",
                max_recent_events=0,
            )
        elif not recall_query:
            ctx.add_decision(
                self.name, "Semantic recall skipped: empty original_input (system-triggered flow)"
            )
            llm_context = self._builder.build_context(
                session_id=ctx.session_id,
                user_input="",
                max_recent_events=0,
            )
        else:
            llm_context = self._builder.build_context(
                session_id=ctx.session_id,
                user_input=recall_query,
                max_recent_events=0,
            )
            relevant_count = len(llm_context.relevant_memories or [])
            ctx.add_decision(self.name, f"Relevant memories: {relevant_count} memories")

        # Load focused memories (highest priority)
        self._load_focused_memories(ctx)

        # Hydrate playbook section for current plan step (after focused memories are loaded)
        self._load_playbook_section(ctx)

        ctx.relevant_memories = llm_context.relevant_memories or []
        ctx.identity_memories = llm_context.identity_memories or []
        ctx.add_decision(self.name, f"Identity memories: {len(ctx.identity_memories)} items")

        if not ctx.identity_memories:
            raise ValueError(
                "Identity memories are empty - the system cannot function without identity. "
                f"Check that memories with tag 'identity' exist. flow_id={ctx.flow_id}"
            )


