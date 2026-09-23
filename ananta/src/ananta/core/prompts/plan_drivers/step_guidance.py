"""Step guidance computation -- public API and handler dispatch.

Extracts step guidance logic from the inference plugin into a
platform-owned module.  The ``compute_step_guidance`` function is
the single entry point; it selects a handler based on turn type,
builds guidance messages, and optionally replaces the user prompt.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from ananta.core.plans.parser import parse as parse_plan
from ananta.core.plans.types import ParsedPlan, ParsedPlanStep
from ananta.core.prompts.plan_drivers.guidance_articles import (
    GuidanceArticleReader as GuidanceArticleReader,
)
from ananta.core.prompts.plan_drivers.guidance_articles import (
    encode_plan_steps_as_newline_proxy,
    extract_driver_reinforcement,
    resolve_step_guidance_text,
)
from ananta.core.prompts.plan_drivers.guidance_drivers import (
    ProcessDataLookup as ProcessDataLookup,
)
from ananta.core.prompts.plan_drivers.guidance_drivers import (
    build_driver_messages,
    build_focused_step_instruction,
)
from ananta.core.prompts.plan_state import PlanState

logger = logging.getLogger(__name__)

# ── Constants ────────────────────────────────────────────────────────

_INITIAL_TURN_GUIDANCE_FILE = "initial_turn_select_plan.md"
_CONTINUATION_PREAMBLE_FILE = "plan_continuation_preamble.md"


# ── Result type ──────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class StepGuidanceResult:
    """Output of step guidance computation."""

    guidance_messages: list[dict[str, str]] = field(default_factory=list)
    user_prompt: str | None = None  # None = no change to user prompt


# ── Public API ───────────────────────────────────────────────────────


def compute_step_guidance(
    plan_state: PlanState,
    *,
    tool_observation: str | None = None,
    has_focused_plan: bool = False,
    output_schema: dict[str, Any] | None = None,
    system_prompt: str = "",
    user_prompt: str = "",
    session_id: str = "",
    raw_observation_dict: dict[str, Any] | None = None,
    is_process_error: bool = False,
    article_reader: GuidanceArticleReader,
    process_lookup: ProcessDataLookup,
) -> StepGuidanceResult:
    """Compute step guidance messages and optional user prompt replacement.

    This is the single entry point for all step guidance logic.
    It selects a handler based on turn type (initial, focused-resume,
    or observation-continuation), builds guidance messages, and
    optionally produces a replacement user prompt.

    Returns an empty result when guidance does not apply (no output
    schema, no actions key, or process error vertex).
    """
    if not _should_compute_guidance(output_schema, is_process_error):
        return StepGuidanceResult()

    handler = _select_handler(has_focused_plan, tool_observation)
    if handler is None:
        return StepGuidanceResult()

    return handler(
        plan_state,
        tool_observation=tool_observation,
        system_prompt=system_prompt,
        user_prompt=user_prompt,
        session_id=session_id,
        raw_observation_dict=raw_observation_dict,
        article_reader=article_reader,
        process_lookup=process_lookup,
    )


# ── Guard ────────────────────────────────────────────────────────────


def _should_compute_guidance(
    output_schema: dict[str, Any] | None,
    is_process_error: bool,
) -> bool:
    """Check whether guidance should be computed for this turn."""
    if not output_schema:
        return False
    if "actions" not in output_schema.get("required", []):
        return False
    return not is_process_error


# ── Handler dispatch ─────────────────────────────────────────────────

type _HandlerFn = Callable[..., StepGuidanceResult]


def _select_handler(
    has_focused_plan: bool,
    tool_observation: str | None,
) -> _HandlerFn | None:
    """Pick the step-guidance handler for this turn type."""
    if not has_focused_plan and not tool_observation:
        return _handle_initial_turn
    if has_focused_plan and not tool_observation:
        return _handle_focused_step
    if has_focused_plan and tool_observation:
        return _handle_planning_extension
    return None


# ── Handlers ─────────────────────────────────────────────────────────


def _handle_initial_turn(
    plan_state: PlanState,
    *,
    tool_observation: str | None = None,
    system_prompt: str = "",
    user_prompt: str = "",
    session_id: str = "",
    raw_observation_dict: dict[str, Any] | None = None,
    article_reader: GuidanceArticleReader,
    process_lookup: ProcessDataLookup,
) -> StepGuidanceResult:
    """Inject openings catalog as ephemeral assistant message.

    Loads ``initial_turn_select_plan.md``, encodes plan step blocks
    with U+2424 line separators, and produces two assistant messages:
    the catalog and the initial plan status.
    """
    guidance_text = article_reader.read_article(_INITIAL_TURN_GUIDANCE_FILE)
    if guidance_text is None:
        return StepGuidanceResult()

    guidance_text = encode_plan_steps_as_newline_proxy(guidance_text)
    plan_status = _build_initial_turn_plan_status()

    messages: list[dict[str, str]] = [
        {"role": "assistant", "content": guidance_text},
        {"role": "assistant", "content": plan_status},
    ]

    logger.info(
        "STEP_GUIDANCE: Injected openings catalog + PLAN status",
    )
    return StepGuidanceResult(guidance_messages=messages)


def _handle_focused_step(
    plan_state: PlanState,
    *,
    tool_observation: str | None = None,
    system_prompt: str = "",
    user_prompt: str = "",
    session_id: str = "",
    raw_observation_dict: dict[str, Any] | None = None,
    article_reader: GuidanceArticleReader,
    process_lookup: ProcessDataLookup,
) -> StepGuidanceResult:
    """Inject step-derived instruction on focused-plan resume turns.

    When a user message resumes a paused plan (after ``[-] Await USER
    message``), the pipeline produces no synthetic_driver instruction.
    This handler fills that gap by building a plan-derived instruction
    from the current ``[>]`` step's text.
    """
    plan_text = plan_state.focused_plan_text
    if not plan_text:
        return StepGuidanceResult()

    parsed = parse_plan(plan_text)
    active = parsed.current_step
    target = _resolve_guidance_target_step(parsed)
    if target is None:
        logger.info("FOCUSED_STEP_DRIVER: No executable step found")
        return StepGuidanceResult()

    messages: list[dict[str, str]] = []

    # Inject guidance article before the driver instruction
    guidance_text = resolve_step_guidance_text(parsed, target, article_reader)
    if guidance_text:
        messages.append({"role": "assistant", "content": guidance_text})
        _log_guidance_injection(parsed, target)

    instruction = build_focused_step_instruction(
        target,
        active_step=active,
        plan_state=plan_state,
        session_id=session_id,
        process_lookup=process_lookup,
    )
    if not instruction:
        return StepGuidanceResult(guidance_messages=messages)

    # The step driver replaces ctx.user_prompt.  Only assistant-role
    # guidance articles go into guidance_messages — the user instruction
    # goes exclusively into user_prompt so APIStage doesn't duplicate it.
    logger.info(
        "FOCUSED_STEP_DRIVER: Injected plan step %d instruction (%d chars)",
        target.number,
        len(instruction),
    )
    return StepGuidanceResult(
        guidance_messages=messages,
        user_prompt=instruction,
    )


def _handle_planning_extension(
    plan_state: PlanState,
    *,
    tool_observation: str | None = None,
    system_prompt: str = "",
    user_prompt: str = "",
    session_id: str = "",
    raw_observation_dict: dict[str, Any] | None = None,
    article_reader: GuidanceArticleReader,
    process_lookup: ProcessDataLookup,
) -> StepGuidanceResult:
    """Inject guidance article and step instruction on observation turns.

    For all observation turns with a focused plan:

    1. The guidance article is injected as an assistant message.
    2. The step instruction is appended to the observation as the
       user prompt replacement.
    3. If the guidance article contains a ``### Driver Reinforcement``
       subsection, that block is appended to the USER driver so the
       model sees the most critical contract at the point of action.
    """
    plan_text = plan_state.focused_plan_text
    if not plan_text:
        return StepGuidanceResult()

    parsed = parse_plan(plan_text)
    active = parsed.current_step
    target = _resolve_guidance_target_step(parsed)
    if target is None:
        return StepGuidanceResult()

    messages: list[dict[str, str]] = []

    # Resolve guidance text before injection so we can extract
    # the optional Driver Reinforcement subsection.
    guidance_text = resolve_step_guidance_text(parsed, target, article_reader)
    reinforcement = ""
    if guidance_text:
        reinforcement, remaining = extract_driver_reinforcement(guidance_text)
        messages.append({"role": "assistant", "content": remaining})
        _log_guidance_injection(parsed, target)

    obs_key = (
        raw_observation_dict.get("process_key")
        if raw_observation_dict else None
    )
    instruction = build_focused_step_instruction(
        target,
        active_step=active,
        plan_state=plan_state,
        session_id=session_id,
        has_reinforcement=bool(reinforcement),
        process_lookup=process_lookup,
        observation_process_key=obs_key if isinstance(obs_key, str) else None,
    )
    if not instruction:
        return StepGuidanceResult(guidance_messages=messages)

    # Build the user prompt replacement.  Pass empty string as the
    # observation base — APIStage renders ctx.tool_observation as
    # a separate block, so including it here would duplicate it.
    # The driver contains only instruction + reinforcement.
    _extra_msgs, user_content = build_driver_messages(
        instruction,
        target,
        plan_state,
        tool_observation="",
        raw_observation_dict=raw_observation_dict,
        reinforcement=reinforcement,
        process_lookup=process_lookup,
    )
    messages.extend({"role": m["role"], "content": m["content"]} for m in _extra_msgs)

    label = "PLANNING_EXTENSION" if target.has_planning_extension else "OBSERVATION_STEP"
    logger.info(
        "%s_DRIVER: Injected step %d instruction (%d chars%s)",
        label,
        target.number,
        len(instruction),
        f", reinforcement {len(reinforcement)} chars" if reinforcement else "",
    )

    return StepGuidanceResult(
        guidance_messages=messages,
        user_prompt=user_content,
    )


# ── Target step resolution ───────────────────────────────────────────


def _resolve_guidance_target_step(
    parsed: ParsedPlan,
) -> ParsedPlanStep | None:
    """Choose the step whose guidance should drive this focused turn.

    Normal focused turns use the active ``[>]`` step (falling back to
    the first executable step). When the active step is a real wait
    boundary with no process keys, use the next concrete pending step.
    """
    current = parsed.current_step
    if current is None:
        first_num = parsed.first_executable_step_number
        if first_num is not None:
            current = parsed.step_by_number(first_num)
    if current is None:
        return None
    if current.process_keys:
        return current
    for step in parsed.steps:
        if step.number <= current.number:
            continue
        if step.is_completed or step.is_skipped:
            continue
        if step.process_keys:
            return step
        return None
    return current


# ── Initial turn plan status ─────────────────────────────────────────


def _build_initial_turn_plan_status() -> str:
    """Build the initial turn plan status in ACTIVE_PLAN format.

    Uses the same format as continuation turns so ``has_focused_plan``
    detection and model context are consistent across all turns.
    """
    timestamp = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    return (
        f"{timestamp}\n"
        "ACTIVE_PLAN:\n"
        "Full plan: pln-example01\n"
        "[>] 1. Select the appropriate Opening Plan\n"
        "    a) Record the selected Opening Plan "
        "(service_interface::thinking_service::upsert_plan)\n"
        "    b) Retrieve relevant memories "
        "(service_interface::memory_service::recall)"
    )


# ── Continuation preamble ────────────────────────────────────────────


def inject_continuation_preamble(
    article_reader: GuidanceArticleReader,
) -> list[dict[str, str]]:
    """Load continuation preamble and return as assistant messages.

    Loads ``plan_continuation_preamble.md`` and returns it as an
    assistant message. Returns an empty list if the article is missing.
    """
    preamble_text = article_reader.read_article(_CONTINUATION_PREAMBLE_FILE)
    if preamble_text is None:
        return []

    logger.info("STEP_GUIDANCE: Injected continuation preamble")
    return [{"role": "assistant", "content": preamble_text}]


# ── Logging helper ───────────────────────────────────────────────────


def _log_guidance_injection(
    parsed: ParsedPlan,
    step: ParsedPlanStep,
) -> None:
    """Log which guidance article/section was injected."""
    article = step.guidance_article or parsed.plan_guidance_article
    section = step.guidance_section_id or parsed.plan_guidance_section_id
    logger.info(
        "STEP_GUIDANCE: Injected %s%s",
        article,
        f"#{section}" if section else "",
    )
