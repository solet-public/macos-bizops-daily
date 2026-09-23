"""Typed plan state for prompt assembly.

Parsed once per prompt assembly by ``PlanStateStage`` and stored on
``PromptContext``.  Downstream stages (catalog, guidance, decode
contract) consume ``PlanState`` instead of reparsing focused plan text
independently.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from ananta.core.plans.parser import parse
from ananta.core.plans.types import ParsedPlan, ParsedPlanStep
from ananta.core.prompts.context import ACTIVE_PLAN_MARKER

_AWAIT_USER_RE = re.compile(r"Await USER message", re.IGNORECASE)
_WBS_STEP_MARKER = "WBS Step"
_ACTIVE_WBS_ID_RE = re.compile(r"^ACTIVE_WBS:\s*(\S+)", re.MULTILINE)
_WBS_ID_LINE_RE = re.compile(r"^WBS ID:\s*(\S+)", re.MULTILINE)


@dataclass(frozen=True)
class PlanState:
    """Structured plan state for a single prompt assembly turn.

    Computed once by ``PlanStateStage`` from focused memories and
    pre-resolved runtime state.  All plan/WBS queries during prompt
    assembly should read from this object rather than reparsing.
    """

    # Raw text from focused memory
    focused_plan_text: str | None = None
    focused_wbs_text: str | None = None
    focused_resolved_intake_text: str | None = None

    # Parsed plan
    parsed_plan: ParsedPlan | None = None
    current_step: ParsedPlanStep | None = None
    current_step_number: int | None = None

    # Resolved process keys for the current step
    all_process_keys: tuple[str, ...] = ()
    model_visible_keys: tuple[str, ...] = ()

    # IO namespace (pre-resolved by plugin)
    io_namespace: str | None = None

    # Flags
    has_focused_plan: bool = False
    is_wbs_execution: bool = False
    is_await_user: bool = False
    is_completion_handoff: bool = False
    has_planning_extension: bool = False

    # WBS metadata
    wbs_step_number: int | None = None


def compute_plan_state(
    focused_memories: list[dict[str, Any]],
    io_namespace: str | None = None,
) -> PlanState:
    """Compute plan state from focused memories.

    This is the single computation point for all plan/WBS state
    needed during prompt assembly.
    """
    plan_text = _extract_focused_content(focused_memories, ACTIVE_PLAN_MARKER)
    active_wbs_id = _extract_active_wbs_id(plan_text) if plan_text else None
    wbs_text = _extract_focused_wbs(focused_memories, active_wbs_id)
    intake_text = _extract_focused_content_by_tag(focused_memories, "resolved_intake_state")

    if not plan_text:
        return PlanState(
            focused_wbs_text=wbs_text,
            focused_resolved_intake_text=intake_text,
            io_namespace=io_namespace,
        )

    parsed = parse(plan_text)
    current = _resolve_current_step(parsed)
    wbs_step_number = _resolve_wbs_step_number(current) if current else None

    return PlanState(
        focused_plan_text=plan_text,
        focused_wbs_text=wbs_text,
        focused_resolved_intake_text=intake_text,
        parsed_plan=parsed,
        current_step=current,
        current_step_number=current.number if current else None,
        all_process_keys=tuple(current.process_keys) if current else (),
        model_visible_keys=tuple(current.process_keys) if current else (),
        io_namespace=io_namespace,
        has_focused_plan=True,
        is_wbs_execution=_is_wbs_step(current),
        is_await_user=_is_await_user_step(current),
        is_completion_handoff=_is_completion_handoff(wbs_text, wbs_step_number),
        has_planning_extension=current.has_planning_extension if current else False,
        wbs_step_number=wbs_step_number,
    )


def _resolve_current_step(parsed: ParsedPlan) -> ParsedPlanStep | None:
    """Resolve the current step from a parsed plan."""
    current = parsed.current_step
    if current is None and parsed.first_executable_step_number is not None:
        current = parsed.step_by_number(parsed.first_executable_step_number)
    return current


def _is_wbs_step(step: ParsedPlanStep | None) -> bool:
    """Check whether the step references a WBS execution marker."""
    if step is None:
        return False
    return any(_WBS_STEP_MARKER in ln for ln in step.lines)


def _is_await_user_step(step: ParsedPlanStep | None) -> bool:
    """Check whether the step is an await-user checkpoint."""
    if step is None:
        return False
    return not step.process_keys and bool(_AWAIT_USER_RE.search(step.full_text()))


def _is_completion_handoff(
    wbs_text: str | None,
    wbs_step_number: int | None,
) -> bool:
    """Check whether the current WBS step is the last step (completion handoff)."""
    if not wbs_text or wbs_step_number is None:
        return False
    wbs_parsed = parse(wbs_text)
    last_wbs = wbs_parsed.steps[-1] if wbs_parsed.steps else None
    return last_wbs is not None and last_wbs.number == wbs_step_number


def _extract_focused_content(
    focused_memories: list[dict[str, Any]],
    marker: str,
) -> str | None:
    """Extract focused memory content containing a marker string."""
    for mem in focused_memories:
        content = mem.get("content", "")
        if isinstance(content, str) and marker in content:
            return content
    return None


def _extract_focused_content_by_tag(
    focused_memories: list[dict[str, Any]],
    tag: str,
) -> str | None:
    """Extract focused memory content with a specific tag."""
    for mem in focused_memories:
        tags: list[str] = mem.get("tags", [])
        if tag in tags:
            content = mem.get("content", "")
            if isinstance(content, str):
                return content
    return None


def _extract_active_wbs_id(plan_text: str) -> str | None:
    """Extract the ACTIVE_WBS identifier from the plan text."""
    match = _ACTIVE_WBS_ID_RE.search(plan_text)
    return match.group(1) if match else None


def _extract_focused_wbs(
    focused_memories: list[dict[str, Any]],
    active_wbs_id: str | None,
) -> str | None:
    """Extract the focused WBS matching the plan's ACTIVE_WBS identifier.

    Prefers the WBS whose ``WBS ID:`` line matches *active_wbs_id*.
    Falls back to the first focused WBS when no ID-match is found.
    """
    fallback: str | None = None
    for mem in focused_memories:
        tags: list[str] = mem.get("tags", [])
        if "work_breakdown_structure" not in tags:
            continue
        content = mem.get("content", "")
        if not isinstance(content, str):
            continue
        if active_wbs_id:
            wbs_id_match = _WBS_ID_LINE_RE.search(content)
            if wbs_id_match and wbs_id_match.group(1) == active_wbs_id:
                return content
        if fallback is None:
            fallback = content
    return fallback


def _resolve_wbs_step_number(step: ParsedPlanStep) -> int | None:
    """Extract the WBS step number from a plan step's text."""
    for line in step.lines:
        match = re.search(r"WBS Step (\d+)", line)
        if match:
            return int(match.group(1))
    return None
