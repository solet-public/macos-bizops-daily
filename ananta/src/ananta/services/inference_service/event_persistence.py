"""Event persistence — build metadata, append events, track sessions.

Pure helpers for constructing context event metadata, plus platform
functions for persisting events to the context management service.
Replaces plugin methods:
  _append_context_event, _cleanup_orphaned_content,
  _update_session_tracking, _check_compaction_after_event,
  _store_input_event_if_first, _do_store_input_event,
  _build_input_event_metadata (merged with existing helper),
  _store_post_inference_events, _persist_blocks_after_turn,
  _store_assistant_response, _store_system_messages_if_first_request
"""

from __future__ import annotations

import contextlib
import json
import logging
from typing import Any, Protocol

from ananta.services.context_management.types import (
    ContextActorType,
    ContextEventType,
    ContextMode,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_MAX_TRACKED_FLOWS: int = 1000

_PROMPT_KEY = "prompt"
_PROMPT_USER_KEY = "user"
_ACTION_RESULT_KEY = "action_result"
_PROCESS_KEYS_KEY = "process_keys"

_HISTORY_KIND_EVENT_MAP: dict[str, tuple[ContextEventType, ContextActorType]] = {
    "output_event": (ContextEventType.OUTPUT, ContextActorType.AGENT),
    "input_event": (ContextEventType.INPUT, ContextActorType.HUMAN),
}


# ---------------------------------------------------------------------------
# Protocols — narrow dependency contracts
# ---------------------------------------------------------------------------

class ContextEventWriter(Protocol):
    """Narrow protocol for appending context events."""

    def append_event(
        self,
        *,
        context_id: str,
        event_type: str,
        actor_type: str,
        content_path: str,
        content_char_count: int,
        actor_id: str,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]: ...

    def get_event_created_at(self, event_id: str) -> str | None: ...


class ContentStorage(Protocol):
    """Narrow protocol for content file storage."""

    def store_event(
        self, context_id: str, content: str,
    ) -> tuple[str, int]: ...

    def delete(self, path: str) -> None: ...


class SessionService(Protocol):
    """Narrow protocol for session tracking."""

    def get_or_create_session(
        self,
        *,
        context_id: str,
        provider: str,
        context_mode: str,
    ) -> Any: ...

    def update_cursor(
        self,
        *,
        context_id: str,
        last_event_id: str,
        last_event_created_at: str,
    ) -> None: ...


class CompactionService(Protocol):
    """Narrow protocol for compaction triggering."""

    def check_and_trigger_compaction(
        self,
        context_id: str,
        config: Any,
    ) -> bool: ...


class MemoryService(Protocol):
    """Narrow protocol for memory interaction storage."""

    def store_interaction(
        self,
        *,
        session_id: str,
        source_namespace: str,
        event_type: str,
        content: str,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]: ...


class MessageBlock(Protocol):
    """Narrow protocol for prompt context message blocks."""

    @property
    def block_id(self) -> str: ...

    @property
    def content(self) -> str: ...

    @property
    def source_kind(self) -> str: ...

    @property
    def transition_behavior(self) -> str: ...

    @property
    def history_kind(self) -> str: ...

    @property
    def source_reference(self) -> Any: ...


class PromptContextLike(Protocol):
    """Narrow protocol for PromptContext fields used by persistence."""

    @property
    def message_blocks(self) -> list[Any]: ...

    @property
    def session_id(self) -> str: ...

    @property
    def observation_source_memory_id(self) -> str: ...

    @property
    def is_wbs_execution_context(self) -> bool: ...

    @property
    def raw_observation_dict(self) -> dict[str, Any] | None: ...


# ---------------------------------------------------------------------------
# Pure metadata helpers (pre-existing, preserved)
# ---------------------------------------------------------------------------

def build_input_event_metadata(
    flow_id: str,
    session_id: str,
    original_input: str | None,
) -> dict[str, Any]:
    """Build metadata for an INPUT context event."""
    metadata: dict[str, Any] = {}
    if flow_id:
        metadata["flow_id"] = flow_id
    if session_id:
        metadata["session_id"] = session_id
    if original_input:
        metadata["original_input"] = original_input
    return metadata


def merge_flow_input_metadata(
    metadata: dict[str, Any],
    resolved_action_params: dict[str, Any],
) -> dict[str, Any]:
    """Merge flow_input fields into event metadata.

    Extracts IO routing metadata (namespace, source, destination,
    posted_at) from the flow_input and adds them to the event metadata.
    """
    prompt = resolved_action_params.get("prompt", {})
    if not isinstance(prompt, dict):
        return metadata
    user = prompt.get("user", {})
    if not isinstance(user, dict):
        return metadata
    flow_input = user.get("flow_input")
    if isinstance(flow_input, str):
        try:
            flow_input = json.loads(flow_input)
        except (json.JSONDecodeError, ValueError):
            return metadata
    if not isinstance(flow_input, dict):
        return metadata

    for key in ("namespace", "source", "destination", "posted_at", "session_id"):
        value = flow_input.get(key)
        if value and isinstance(value, str):
            metadata[key] = value

    return metadata


def is_processor_callback(resolved_action_params: dict[str, Any]) -> bool:
    """Check if this inference call is a processor callback.

    Processor callbacks have an ``observation`` field in their prompt,
    indicating they were triggered by a prior action's result_processor.
    """
    prompt_part = resolved_action_params.get("prompt", {})
    return isinstance(prompt_part, dict) and "observation" in prompt_part


def build_ossification_metadata(
    flow_id: str,
    session_id: str,
    action_name: str,
) -> dict[str, Any]:
    """Build metadata for persisting inference blocks after a turn."""
    metadata: dict[str, Any] = {"action_name": action_name}
    if flow_id:
        metadata["flow_id"] = flow_id
    if session_id:
        metadata["session_id"] = session_id
    return metadata


def extract_process_keys_for_tracking(
    actions: list[dict[str, Any]],
) -> list[str]:
    """Extract process keys from normalized actions for session tracking."""
    return [
        a.get("process_key", "")
        for a in actions
        if a.get("process_key")
    ]


# ---------------------------------------------------------------------------
# Input event metadata (merged from plugin._build_input_event_metadata)
# ---------------------------------------------------------------------------

def build_input_event_metadata_from_params(
    resolved_action_params: dict[str, Any],
) -> dict[str, Any]:
    """Build metadata dict for INPUT event from resolved action parameters.

    Includes discovered process keys (for tracking) and IO source metadata
    extracted from flow_input.
    """
    metadata: dict[str, Any] = {}

    discovered = _extract_process_keys_from_params(resolved_action_params)
    if discovered:
        metadata["contains_process_keys"] = discovered
        logger.debug(
            "Tracking %d process keys in INPUT event", len(discovered),
        )

    _merge_flow_input_metadata_into(metadata, resolved_action_params)
    return metadata


def _extract_process_keys_from_params(
    action_params: dict[str, Any],
) -> list[str] | None:
    """Extract process_keys from action_params for context tracking."""
    prompt = action_params.get(_PROMPT_KEY, {})
    user = prompt.get(_PROMPT_USER_KEY, {}) if isinstance(prompt, dict) else {}
    action_result = (
        user.get(_ACTION_RESULT_KEY) if isinstance(user, dict) else None
    )
    if not isinstance(action_result, dict):
        return None
    process_keys = action_result.get(_PROCESS_KEYS_KEY)
    if not isinstance(process_keys, list):
        return None
    return [k for k in process_keys if isinstance(k, str)]


def _merge_flow_input_metadata_into(
    metadata: dict[str, Any],
    resolved_action_params: dict[str, Any],
) -> None:
    """Extract IO source metadata from flow_input and merge into metadata."""
    user = _get_user_dict(resolved_action_params)
    flow_input_raw = _get_flow_input_raw(user) if user else None
    flow_input = _parse_flow_input(flow_input_raw) if flow_input_raw else None
    if not flow_input:
        return

    for fi_key, meta_key in (
        ("source_namespace", "source_namespace"),
        ("source", "source"),
        ("sender_name", "sender_name"),
        ("session_id", "session_id"),
    ):
        value = flow_input.get(fi_key, "")
        if value:
            metadata[meta_key] = value


# ---------------------------------------------------------------------------
# Original-input extraction helpers
# ---------------------------------------------------------------------------

def extract_original_input(
    resolved_action_params: dict[str, Any],
) -> str | None:
    """Extract original user input from resolved_action_params.

    Path: resolved_action_params["prompt"]["user"]["flow_input"]["original_input"]
    or:   resolved_action_params["prompt"]["user"]["input"]["flow_input"]["original_input"]

    Returns None if not found/empty (system-triggered flows).
    """
    user = _get_user_dict(resolved_action_params)
    if user is None:
        return None

    flow_input_raw = _get_flow_input_raw(user)
    if not flow_input_raw:
        logger.warning(
            "_extract_original_input: flow_input not found. keys=%s",
            list(user.keys()),
        )
        return None

    flow_input = _parse_flow_input(flow_input_raw)
    if flow_input is None:
        return None

    original_input = flow_input.get("original_input")
    if not isinstance(original_input, str) or not original_input:
        logger.warning(
            "_extract_original_input: original_input not valid. "
            "type=%s, value=%r",
            type(original_input).__name__,
            original_input,
        )
        return None

    logger.debug(
        "_extract_original_input: SUCCESS, extracted=%s...",
        original_input[:50],
    )
    return original_input


def _get_user_dict(
    resolved_action_params: dict[str, Any],
) -> dict[str, Any] | None:
    """Extract the user dict from resolved_action_params."""
    prompt = resolved_action_params.get("prompt", {})
    if not isinstance(prompt, dict):
        return None
    user = prompt.get("user", {})
    if not isinstance(user, dict):
        return None
    return user


def _get_flow_input_raw(user: dict[str, Any]) -> Any:
    """Extract raw flow_input from user dict, checking both paths."""
    flow_input_raw = user.get("flow_input")
    if flow_input_raw:
        return flow_input_raw
    input_wrapper = user.get("input", {})
    if isinstance(input_wrapper, dict):
        flow_input_raw = input_wrapper.get("flow_input")
    return flow_input_raw


def _parse_flow_input(flow_input_raw: Any) -> dict[str, Any] | None:
    """Parse flow_input from raw value to dict."""
    if isinstance(flow_input_raw, str):
        try:
            parsed = json.loads(flow_input_raw)
            if isinstance(parsed, dict):
                return dict(parsed)
        except (json.JSONDecodeError, ValueError):
            pass
        return None
    if isinstance(flow_input_raw, dict):
        return dict(flow_input_raw)
    return None


# ---------------------------------------------------------------------------
# Block ossification metadata (from plugin._build_ossification_metadata)
# ---------------------------------------------------------------------------

def build_block_ossification_metadata(
    block: MessageBlock,
    session_id: str,
    source_memory_id: str = "",
) -> dict[str, Any] | None:
    """Build event metadata for a living-to-ossified block."""
    meta: dict[str, Any] = {}
    source_ref = block.source_reference
    if source_ref is not None and getattr(source_ref, "ref", None):
        meta["source_namespace"] = source_ref.ref
    if session_id:
        meta["session_id"] = session_id
    if source_memory_id:
        meta["source_memory_id"] = source_memory_id
    return meta or None


# ---------------------------------------------------------------------------
# Event append (core persistence)
# ---------------------------------------------------------------------------

def append_context_event(
    context_id: str,
    content: str,
    event_type: ContextEventType,
    actor_type: ContextActorType,
    *,
    event_writer: ContextEventWriter,
    content_storage: ContentStorage,
    provider_name: str,
    sessions: SessionService | None = None,
    compaction: CompactionService | None = None,
    compaction_config: Any = None,
    event_metadata: dict[str, Any] | None = None,
) -> None:
    """Append event to context management service.

    Stores content to file, appends event metadata, updates session
    tracking, and triggers compaction check. Fail-fast on any failure.

    Raises:
        RuntimeError: If event append fails.
    """
    from ananta.core.plugins.plugin_contracts import ActionStatus

    content_path, char_count = content_storage.store_event(
        context_id, content,
    )

    result = event_writer.append_event(
        context_id=context_id,
        event_type=event_type.value,
        actor_type=actor_type.value,
        content_path=content_path,
        content_char_count=char_count,
        actor_id=provider_name,
        metadata=event_metadata,
    )

    if result.get("action_status") != ActionStatus.COMPLETED.value:
        _cleanup_orphaned_content(content_path, content_storage)
        raise RuntimeError(
            f"Failed to append context event: {result.get('error')}",
        )

    if sessions is not None:
        update_session_tracking(
            context_id,
            result,
            event_writer=event_writer,
            sessions=sessions,
            provider_name=provider_name,
        )

    logger.debug(
        "Appended %s event to context %s: %d chars",
        event_type.value, context_id, char_count,
    )

    if compaction is not None and compaction_config is not None:
        check_compaction_after_event(
            context_id, compaction=compaction, config=compaction_config,
        )


def _cleanup_orphaned_content(
    content_path: str | None,
    content_storage: ContentStorage,
) -> None:
    """Clean up orphaned content file on failure."""
    if content_path:
        with contextlib.suppress(Exception):
            content_storage.delete(content_path)


# ---------------------------------------------------------------------------
# Session tracking
# ---------------------------------------------------------------------------

def update_session_tracking(
    context_id: str,
    result: dict[str, Any],
    *,
    event_writer: ContextEventWriter,
    sessions: SessionService,
    provider_name: str,
) -> None:
    """Update session cursor after successful event append.

    Raises:
        RuntimeError: If event data is missing or corrupted.
    """
    data = result.get("data", {})
    inner_result = data.get("result", {}) if isinstance(data, dict) else {}
    event_id = (
        inner_result.get("generated_id")
        if isinstance(inner_result, dict) else None
    )
    if not event_id:
        return

    created_at = event_writer.get_event_created_at(str(event_id))
    if not created_at:
        raise RuntimeError(
            f"Event {event_id} missing created_at - "
            f"data corruption detected (context_id={context_id})",
        )

    sessions.get_or_create_session(
        context_id=context_id,
        provider=provider_name,
        context_mode=ContextMode.PLATFORM.value,
    )
    sessions.update_cursor(
        context_id=context_id,
        last_event_id=str(event_id),
        last_event_created_at=created_at,
    )


def check_compaction_after_event(
    context_id: str,
    *,
    compaction: CompactionService,
    config: Any,
) -> None:
    """Check and trigger compaction after event append.

    Raises:
        FrameworkError: If compaction fails (fail-fast).
    """
    compacted = compaction.check_and_trigger_compaction(context_id, config)
    if compacted:
        logger.info("Compaction triggered for context %s", context_id)


# ---------------------------------------------------------------------------
# Input event storage
# ---------------------------------------------------------------------------

def store_input_event_if_first(
    context_id: str,
    flow_id: str,
    resolved_action_params: dict[str, Any],
    flows_with_input_stored: set[str],
    *,
    event_writer: ContextEventWriter,
    content_storage: ContentStorage,
    provider_name: str,
    sessions: SessionService | None = None,
    compaction: CompactionService | None = None,
    compaction_config: Any = None,
) -> None:
    """Store INPUT event only for first inference call per flow.

    Prevents duplicates when multiple process_results calls share the
    same flow_input.original_input. Uses flow_id tracking with bounded
    set size.

    Raises:
        RuntimeError: If flow_id is empty (required for deduplication).
    """
    if not flow_id:
        raise RuntimeError(
            "flow_id is required for INPUT event storage but is empty "
            f"(context_id={context_id})",
        )

    if flow_id in flows_with_input_stored:
        logger.debug("INPUT event already stored for flow %s...", flow_id[:8])
        return

    original_input = extract_original_input(resolved_action_params)
    if not original_input:
        logger.debug(
            "No original_input in flow %s... (system-triggered)",
            flow_id[:8],
        )
        return

    logger.info("Storing INPUT event for flow %s...", flow_id[:8])
    event_metadata = build_input_event_metadata_from_params(
        resolved_action_params,
    )
    append_context_event(
        context_id,
        original_input,
        ContextEventType.INPUT,
        ContextActorType.HUMAN,
        event_writer=event_writer,
        content_storage=content_storage,
        provider_name=provider_name,
        sessions=sessions,
        compaction=compaction,
        compaction_config=compaction_config,
        event_metadata=event_metadata or None,
    )
    flows_with_input_stored.add(flow_id)

    _prune_flow_tracking(flows_with_input_stored)


def _prune_flow_tracking(tracked: set[str]) -> None:
    """Bound set size to prevent unbounded memory growth."""
    if len(tracked) > _MAX_TRACKED_FLOWS:
        to_remove = list(tracked)[: _MAX_TRACKED_FLOWS // 2]
        tracked -= set(to_remove)
        logger.debug("Pruned flow tracking set to %d entries", len(tracked))


# ---------------------------------------------------------------------------
# Post-inference event storage
# ---------------------------------------------------------------------------

def store_post_inference_events(
    context_id: str,
    state: dict[str, Any],
    resolved_action_params: dict[str, Any],
    completion_text: str,
    prompt_ctx: PromptContextLike,
    flows_with_input_stored: set[str],
    *,
    event_writer: ContextEventWriter,
    content_storage: ContentStorage,
    provider_name: str,
    sessions: SessionService | None = None,
    compaction: CompactionService | None = None,
    compaction_config: Any = None,
) -> None:
    """Store context events after inference (platform mode only).

    Handles INPUT event, reasoning OUTPUT event (both skipped for
    processor callbacks), and block persistence with living_to_ossified.
    """
    from ananta.core.prompts.decode.action_extraction import (
        extract_reasoning_text,
    )

    flow_id = state.get("flow_id", "")
    append_kw = _append_kwargs(
        event_writer, content_storage, provider_name,
        sessions, compaction, compaction_config,
    )

    if not is_processor_callback(resolved_action_params):
        store_input_event_if_first(
            context_id, flow_id, resolved_action_params,
            flows_with_input_stored, **append_kw,
        )
        reasoning_text = extract_reasoning_text(completion_text)
        if reasoning_text:
            append_context_event(
                context_id, reasoning_text,
                ContextEventType.OUTPUT, ContextActorType.AGENT,
                **append_kw, event_metadata={"source": "inference_reasoning"},
            )

    persist_blocks_after_turn(
        context_id, flow_id, prompt_ctx, **append_kw,
    )


def _append_kwargs(
    event_writer: ContextEventWriter,
    content_storage: ContentStorage,
    provider_name: str,
    sessions: SessionService | None,
    compaction: CompactionService | None,
    compaction_config: Any,
) -> dict[str, Any]:
    """Build shared keyword arguments for append_context_event calls."""
    return {
        "event_writer": event_writer,
        "content_storage": content_storage,
        "provider_name": provider_name,
        "sessions": sessions,
        "compaction": compaction,
        "compaction_config": compaction_config,
    }


# ---------------------------------------------------------------------------
# Block persistence
# ---------------------------------------------------------------------------

def persist_blocks_after_turn(
    context_id: str,
    flow_id: str,
    prompt_ctx: PromptContextLike,
    *,
    event_writer: ContextEventWriter,
    content_storage: ContentStorage,
    provider_name: str,
    sessions: SessionService | None = None,
    compaction: CompactionService | None = None,
    compaction_config: Any = None,
) -> None:
    """Persist observation blocks with living_to_ossified transition.

    During WBS execution, observation content is compacted to ~100 chars
    (action label + process key + output filename + trailer) instead of
    the full JSON result payload (~400 chars).  This prevents context
    overflow over 100+ WBS execution steps.
    """
    is_wbs = prompt_ctx.is_wbs_execution_context
    for block in prompt_ctx.message_blocks:
        if not _should_persist_block(block):
            continue

        mapping = _HISTORY_KIND_EVENT_MAP.get(block.history_kind)
        if mapping is None:
            logger.warning(
                "Block %s: unexpected history_kind=%s, skipping",
                block.block_id, block.history_kind,
            )
            continue

        content = block.content
        if is_wbs:
            original_len = len(content)
            content = compact_wbs_observation(
                content, prompt_ctx.raw_observation_dict,
            )
            logger.info(
                "WBS_COMPACTION: %s %d -> %d chars",
                block.block_id, original_len, len(content),
            )

        event_type, actor_type = mapping
        append_context_event(
            context_id, content, event_type, actor_type,
            event_writer=event_writer,
            content_storage=content_storage,
            provider_name=provider_name,
            sessions=sessions,
            compaction=compaction,
            compaction_config=compaction_config,
            event_metadata=build_block_ossification_metadata(
                block, prompt_ctx.session_id,
                source_memory_id=prompt_ctx.observation_source_memory_id,
            ),
        )
        logger.info(
            "PERSISTED_BLOCK: %s (%s) flow %s, %d chars",
            block.block_id, block.history_kind,
            flow_id[:8], len(content),
        )


def _should_persist_block(block: Any) -> bool:
    """Check if a message block should be persisted."""
    return bool(
        block.source_kind == "response_processor_output"
        and block.transition_behavior == "living_to_ossified"
    )


# ---------------------------------------------------------------------------
# WBS observation compaction
# ---------------------------------------------------------------------------

def _extract_trailer(content: str) -> tuple[str, str]:
    """Split block content into body and JSON metadata trailer.

    Returns (body, trailer) where trailer is the last line if it is a
    JSON object containing "namespace" and "posted_at" keys, otherwise
    returns (content, "").
    """
    stripped = content.rstrip()
    parts = stripped.rsplit("\n", maxsplit=1)
    if len(parts) < 2:
        return content, ""
    last_line = parts[1].strip()
    if (
        last_line.startswith("{")
        and last_line.endswith("}")
        and '"namespace"' in last_line
        and '"posted_at"' in last_line
    ):
        return parts[0].rstrip(), last_line
    return content, ""


def _extract_output_name(raw_observation: dict[str, Any]) -> str:
    """Extract the output filename from a raw observation dict.

    Looks for available_attachments in the action_result data, which is
    the canonical list of output filenames injected by the action queue
    poller after successful execution.
    """
    action_result = raw_observation.get("action_result")
    if not isinstance(action_result, dict):
        return ""
    data = action_result.get("data")
    if not isinstance(data, dict):
        return ""
    available = data.get("available_attachments")
    if isinstance(available, list) and available:
        first = available[0]
        return str(first) if isinstance(first, str) else ""
    return ""


def compact_wbs_observation(
    content: str,
    raw_observation: dict[str, Any] | None,
) -> str:
    """Compact a WBS observation block for storage.

    During WBS execution, full action result JSON (~400 chars per step)
    is replaced with a compact label (~100 chars) containing the action
    label, process key, output filename, and metadata trailer.

    Args:
        content: Full block content (rendered observation + trailer)
        raw_observation: PromptContext.raw_observation_dict with action_result

    Returns:
        Compact content preserving essential traceability fields
    """
    if raw_observation is None:
        return content

    action_result = raw_observation.get("action_result")
    if not isinstance(action_result, dict):
        return content

    # Extract fields from action_result
    label = action_result.get("action_label", "")
    process_key = action_result.get("process_key", "")
    if not process_key:
        process_key = raw_observation.get("process_key", "")

    if not label and not process_key:
        return content

    # Build compact body
    parts: list[str] = []
    if label:
        parts.append(str(label))
    if process_key:
        parts.append(f"process: {process_key}")
    output_name = _extract_output_name(raw_observation)
    if output_name:
        parts.append(f"output: {output_name}")
    compact_body = "\n".join(parts)

    # Preserve the existing trailer from the original content
    _body, trailer = _extract_trailer(content)
    if trailer:
        return f"{compact_body}\n\n{trailer}"
    return compact_body


# ---------------------------------------------------------------------------
# Assistant response storage
# ---------------------------------------------------------------------------

def store_assistant_response(
    session_id: str,
    completion_text: str,
    *,
    memory_service: MemoryService,
    provider_name: str,
) -> None:
    """Store assistant response in memory service.

    Raises:
        RuntimeError: If storage fails.
    """
    from ananta.core.plugins.plugin_contracts import ActionStatus

    result = memory_service.store_interaction(
        session_id=session_id,
        source_namespace=provider_name,
        event_type="assistant_response",
        content=completion_text,
        metadata={"source": provider_name},
    )
    if result.get("action_status") != ActionStatus.COMPLETED.value:
        raise RuntimeError(
            f"Failed to store assistant response: {result.get('error')}",
        )
    event_id = result.get("data", {}).get("event_id")
    logger.debug("Stored assistant response: event_id=%s", event_id)


# ---------------------------------------------------------------------------
# System message storage (no-op by design)
# ---------------------------------------------------------------------------

def store_system_messages_if_first_request() -> None:
    """NO-OP: System messages are NOT stored as context events.

    System messages (prompt + identity) are built fresh on each request
    from system prompt config and identity memories. Storing them as
    events would cause staleness when identity.json is updated.
    """
