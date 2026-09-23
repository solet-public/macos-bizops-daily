"""WBS execution logic for ContextStage.

Module-level functions that handle work breakdown structure (WBS) detection,
history deduplication, and execution-mode trimming of conversation history.
"""

from __future__ import annotations

import logging

from ananta.core.plans import parse as parse_plan
from ananta.core.prompts.context import ACTIVE_PLAN_MARKER, PromptContext
from ananta.core.prompts.context_stage.dedup import deduplicate_focused_history

logger = logging.getLogger(__name__)

# Markers for WBS execution regime
_ACTIVE_WBS_MARKER = "ACTIVE_WBS:"
_WBS_STEP_MARKER = "WBS Step"

# Content markers that identify planning artifacts in conversation history.
# These are consumed during scoping and must not appear in execution prompts.
_PLANNING_ARTIFACT_MARKERS = (
    "# Work Manifest",
    "MANIFEST ID:",
    "# Composition Sketch",
    "SKETCH ID:",
    "# Work Breakdown Structure",
    "WBS ID:",
    "# Resolved Intake State",
    "INTAKE ID:",
)


def extract_focused_plan_text(ctx: PromptContext) -> str | None:
    """Extract focused plan text from focused memories.

    Args:
        ctx: PromptContext with focused_memories populated

    Returns:
        Plan text string containing ACTIVE_PLAN_MARKER, or None
    """
    for mem in ctx.focused_memories:
        content = mem.get("content", "")
        if isinstance(content, str) and ACTIVE_PLAN_MARKER in content:
            return content
    return None


def deduplicate_focused_history_entries(
    ctx: PromptContext,
    *,
    stage_name: str,
) -> None:
    """Remove conversation history entries that duplicate focused memories.

    Args:
        ctx: PromptContext with conversation_history and focused_memories
        stage_name: Stage name for decision logging
    """
    from ananta.core.prompts.context_stage.dedup import focused_content_prefixes
    prefixes = focused_content_prefixes(ctx.focused_memories)
    logger.info(
        "DEDUP_FOCUSED_HISTORY: %d focused memories, %d prefixes, %d history entries",
        len(ctx.focused_memories), len(prefixes), len(ctx.conversation_history),
    )
    ctx.conversation_history, removed = deduplicate_focused_history(
        ctx.conversation_history, ctx.focused_memories,
    )
    if removed:
        logger.info(
            "DEDUP_FOCUSED_HISTORY: removed %d duplicate(s) from conversation_history",
            removed,
        )
        ctx.add_decision(stage_name, f"Dedup: removed {removed} focused duplicate(s)")
    else:
        logger.info("DEDUP_FOCUSED_HISTORY: no duplicates found")


def is_wbs_active_plan(ctx: PromptContext) -> bool:
    """Check if the focused plan references a WBS.

    Returns True when the plan has an ``ACTIVE_WBS:`` header or
    any step mentions ``WBS Step``.  Used for the broader history
    trim that applies to ALL steps in a WBS-active plan (including
    phase-transition planning steps like graft + post_message).

    Args:
        ctx: PromptContext with focused_memories populated

    Returns:
        True if the focused plan references a WBS
    """
    if not ctx.has_focused_plan:
        return False
    plan_text = extract_focused_plan_text(ctx)
    if not plan_text:
        return False
    if _ACTIVE_WBS_MARKER in plan_text:
        return True
    parsed = parse_plan(plan_text)
    return any(
        _WBS_STEP_MARKER in ln for s in parsed.steps for ln in s.lines
    )


def is_wbs_execution_step(ctx: PromptContext) -> bool:
    """Check if the current step is a projected WBS execution step.

    Stricter than ``is_wbs_active_plan`` -- also requires the
    current ``[>]`` step itself to contain ``WBS Step``.  Used
    for artifact filtering (only execution steps, not planning
    steps like graft + post_message).

    Args:
        ctx: PromptContext with focused_memories populated

    Returns:
        True if the current step is a WBS execution step
    """
    if not is_wbs_active_plan(ctx):
        return False
    plan_text = extract_focused_plan_text(ctx)
    if not plan_text:
        return False
    parsed = parse_plan(plan_text)
    current = parsed.current_step
    if current is None:
        return False
    return any(_WBS_STEP_MARKER in ln for ln in current.lines)


def is_planning_artifact_message(msg: dict[str, str]) -> bool:
    """Check if a conversation history message is a planning artifact.

    Args:
        msg: Conversation history message dict with role and content

    Returns:
        True if message is an assistant message containing a planning artifact marker
    """
    if msg.get("role") != "assistant":
        return False
    content = msg.get("content", "")
    return any(marker in content[:100] for marker in _PLANNING_ARTIFACT_MARKERS)


def trim_wbs_execution_history(
    ctx: PromptContext,
    *,
    stage_name: str,
) -> None:
    """Apply compact execution regime to conversation history.

    Preserves the canonical message set for WBS execution:

    1. **Creative brief** — the first user message (original request).
    2. **Direction proposal** — the first assistant message after the
       creative brief (the solet's summary of the proposed direction).
    3. **Go-ahead approval** — the first user message after the
       direction proposal.

    These three messages are pinned.  All other conversation history
    (planning artifacts, tool observations from prior steps) is dropped.
    The compact plan view and bound-argument driver are injected by
    later pipeline stages, not from conversation history.

    Focused memories are NOT filtered here because the WBS text is
    needed for schema building and bound-argument lifting.  Instead,
    APIStage skips non-plan focused artifacts during rendering when
    ``ctx.is_wbs_execution_context`` is True.

    Args:
        ctx: PromptContext with conversation_history populated
        stage_name: Stage name for decision logging
    """
    if not is_wbs_active_plan(ctx):
        return
    original_count = len(ctx.conversation_history)
    ctx.is_wbs_active_plan = True
    ctx.is_wbs_execution_context = is_wbs_execution_step(ctx)

    # Filter out planning artifacts from conversation history
    non_artifacts = [
        m for m in ctx.conversation_history
        if not is_planning_artifact_message(m)
    ]
    artifacts_dropped = original_count - len(non_artifacts)

    # Pin the canonical foundation messages from the conversation
    # opening.  These are the first user message (creative brief),
    # the first assistant response, and optionally the next user
    # message (after artifact filtering).
    pinned = _extract_foundation_messages(non_artifacts)

    ctx.conversation_history = pinned
    logger.info(
        "CONTEXT_STAGE_TRIM: %d -> %d messages "
        "(%d artifacts dropped, %d pinned)",
        original_count, len(pinned),
        artifacts_dropped, len(pinned),
    )
    ctx.add_decision(
        stage_name,
        f"WBS execution trimming: {original_count} -> {len(pinned)} messages",
    )


def _extract_foundation_messages(
    messages: list[dict[str, str]],
) -> list[dict[str, str]]:
    """Extract the canonical foundation messages from conversation opening.

    Walks through messages in order and picks the first user message
    (creative brief), the first assistant message after it, and
    optionally the next user message after that.
    """
    result: list[dict[str, str]] = []
    phase = 0  # 0=seeking user, 1=seeking assistant, 2=seeking user, 3=done
    for msg in messages:
        role = msg.get("role", "")
        if phase == 0 and role == "user":
            result.append(msg)
            phase = 1
        elif phase == 1 and role == "assistant":
            result.append(msg)
            phase = 2
        elif phase == 2 and role == "user":
            result.append(msg)
            phase = 3
            break
    return result
