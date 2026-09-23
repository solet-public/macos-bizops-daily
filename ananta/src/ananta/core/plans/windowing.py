"""Plan window construction for focused memory.

Builds compact, bounded views of the active plan for focused memory
storage.  The inference model sees only this windowed view; the full
plan lives in the knowledge base and is read by the advancement logic.

Three window types:

- **Full window**: all steps rendered (opening/scoping phase, no WBS).
- **Projected window**: trim completed planning prefix, keep WBS
  execution tail (first WBS execution step, no completed WBS yet).
- **Checkpoint excerpt**: current step + small future horizon +
  completed-work summary (deep WBS execution).

Always uses ``ACTIVE_PLAN:`` as the detection header.
"""

from __future__ import annotations

import datetime
import re

from ananta.core.plans.parser import parse
from ananta.core.plans.types import ParsedPlan, ParsedPlanStep

# Number of future steps to include in checkpoint excerpts.
COMPACT_FUTURE_STEPS = 2

# Non-WBS plans with more steps than this get horizon-bounded windowing.
_NON_WBS_FULL_WINDOW_THRESHOLD = 10

# Header detection regexes — shared with graft logic.
PLAYBOOK_HEADER_RE = re.compile(r"^PLAYBOOK:\s*(\S+)", re.MULTILINE)
WORK_MANIFEST_HEADER_RE = re.compile(
    r"^WORK_MANIFEST:\s*(\S+)", re.MULTILINE,
)
ACTIVE_WBS_HEADER_RE = re.compile(r"^ACTIVE_WBS:\s*(\S+)", re.MULTILINE)
ACTIVE_WORK_PRODUCT_RUN_RE = re.compile(
    r"^ACTIVE_WORK_PRODUCT_RUN:\s*(\S+)", re.MULTILINE,
)


def _detect_plan_refs(
    plan_text: str,
    playbook_ref: str | None,
) -> tuple[str | None, str | None, str | None, str | None]:
    """Extract manifest, WBS, run, and playbook refs from plan text."""
    if playbook_ref is None:
        playbook_ref = extract_playbook_ref_from_plan(plan_text)
    manifest_match = WORK_MANIFEST_HEADER_RE.search(plan_text)
    manifest_ref = manifest_match.group(1) if manifest_match else None
    wbs_match = ACTIVE_WBS_HEADER_RE.search(plan_text)
    wbs_ref = wbs_match.group(1) if wbs_match else None
    run_match = ACTIVE_WORK_PRODUCT_RUN_RE.search(plan_text)
    run_ref = run_match.group(1) if run_match else None
    return manifest_ref, wbs_ref, run_ref, playbook_ref


def build_plan_window(
    plan_text: str,
    plan_ref: str | None = None,
    playbook_ref: str | None = None,
) -> str:
    """Build a focused summary of the plan for memory.

    **Two-tier compaction** (advancement reads from knowledge base,
    so focused memory can safely store a compact view):

    - **Non-WBS plan**: full window (opening/scoping phase)
    - **WBS plan, no completed WBS steps**: projected plan view
      (trim planning prefix, keep execution tail)
    - **WBS plan, completed WBS steps exist**: checkpoint excerpt
      (current step + 2 future steps + summary)

    Always uses ``ACTIVE_PLAN:`` header for detection compatibility.
    """
    parsed = parse(plan_text)
    if not parsed.steps:
        return plan_text

    manifest_ref, wbs_ref, run_ref, playbook_ref = _detect_plan_refs(
        plan_text, playbook_ref,
    )
    timestamp = datetime.datetime.now(tz=datetime.UTC).strftime(
        "%Y-%m-%dT%H:%M:%SZ",
    )
    refs = {
        "plan_ref": plan_ref, "playbook_ref": playbook_ref,
        "manifest_ref": manifest_ref, "wbs_ref": wbs_ref,
        "work_product_run_ref": run_ref,
    }
    current = parsed.current_step
    has_wbs = has_wbs_execution_steps(parsed)

    if has_wbs and current is not None:
        if has_completed_wbs_before_step(parsed, current.number):
            return _build_checkpoint_excerpt_window(
                timestamp, parsed, current, **refs,
            )
        return _build_projected_plan_window(
            timestamp, parsed, current, **refs,
        )

    if len(parsed.steps) > _NON_WBS_FULL_WINDOW_THRESHOLD and current is not None:
        return _build_non_wbs_horizon_window(
            timestamp, parsed, current, **refs,
        )

    sections = render_plan_sections(parsed.steps, **refs)
    return "\n".join([timestamp, *sections])


# ── Public helpers ─────────────────────────────────────────────────


def render_plan_sections(
    steps: tuple[ParsedPlanStep, ...],
    plan_ref: str | None,
    playbook_ref: str | None = None,
    manifest_ref: str | None = None,
    wbs_ref: str | None = None,
    work_product_run_ref: str | None = None,
) -> list[str]:
    """Render all steps under ACTIVE_PLAN with full detail.

    All steps — completed and pending — appear under ``ACTIVE_PLAN:``
    so the model can read the full context and update every step.
    """
    lines: list[str] = []

    lines.append("ACTIVE_PLAN:")
    if manifest_ref:
        lines.append(f"WORK_MANIFEST: {manifest_ref}")
    if wbs_ref:
        lines.append(f"ACTIVE_WBS: {wbs_ref}")
    if work_product_run_ref:
        lines.append(f"ACTIVE_WORK_PRODUCT_RUN: {work_product_run_ref}")
    if playbook_ref:
        lines.append(f"PLAYBOOK: {playbook_ref}")
    if plan_ref:
        lines.append(f"Full plan: {plan_ref}")
    for i, step in enumerate(steps):
        step_lines = _step_lines_trimmed(step)
        lines.extend(step_lines)
        if i < len(steps) - 1:
            lines.append("")

    return lines


def extract_playbook_ref_from_plan(plan_text: str) -> str | None:
    """Extract the PLAYBOOK header reference from plan text."""
    match = PLAYBOOK_HEADER_RE.search(plan_text)
    return match.group(1) if match else None


def has_wbs_execution_steps(parsed: ParsedPlan) -> bool:
    """Check if any step in the plan contains WBS Step markers."""
    return any(
        "WBS Step" in ln
        for s in parsed.steps
        for ln in s.lines
    )


def has_completed_wbs_before_step(
    parsed: ParsedPlan, current_number: int,
) -> bool:
    """Check if any completed WBS step precedes the current step."""
    for s in parsed.steps:
        if s.number >= current_number:
            break
        if s.is_completed and any("WBS Step" in ln for ln in s.lines):
            return True
    return False


# ── Internal helpers ───────────────────────────────────────────────


def _build_window_header(
    timestamp: str,
    *,
    plan_ref: str | None,
    playbook_ref: str | None,
    manifest_ref: str | None,
    wbs_ref: str | None,
    work_product_run_ref: str | None = None,
) -> list[str]:
    """Build the ACTIVE_PLAN header lines."""
    lines: list[str] = [timestamp, "ACTIVE_PLAN:"]
    if manifest_ref:
        lines.append(f"WORK_MANIFEST: {manifest_ref}")
    if wbs_ref:
        lines.append(f"ACTIVE_WBS: {wbs_ref}")
    if work_product_run_ref:
        lines.append(f"ACTIVE_WORK_PRODUCT_RUN: {work_product_run_ref}")
    if playbook_ref:
        lines.append(f"PLAYBOOK: {playbook_ref}")
    if plan_ref:
        lines.append(f"Full plan: {plan_ref}")
    return lines


def _step_lines_trimmed(step: ParsedPlanStep) -> list[str]:
    """Return step lines with trailing blank lines stripped."""
    lines = list(step.lines)
    while lines and lines[-1].strip() == "":
        lines.pop()
    return lines


def _build_checkpoint_excerpt_window(
    timestamp: str,
    parsed: ParsedPlan,
    current: ParsedPlanStep,
    *,
    plan_ref: str | None,
    playbook_ref: str | None,
    manifest_ref: str | None,
    wbs_ref: str | None,
    work_product_run_ref: str | None = None,
) -> str:
    """Checkpoint excerpt: current + 2 future steps + completion summary."""
    completed_count = sum(1 for s in parsed.steps if s.is_completed)
    summary = (
        f"Earlier {completed_count} execution steps "
        "completed successfully."
    )

    horizon: list[ParsedPlanStep] = []
    collecting = False
    future_count = 0
    for step in parsed.steps:
        if step.number == current.number:
            horizon.append(step)
            collecting = True
            continue
        if collecting and not step.is_completed:
            horizon.append(step)
            future_count += 1
            if future_count >= COMPACT_FUTURE_STEPS:
                break

    lines = _build_window_header(
        timestamp,
        plan_ref=plan_ref, playbook_ref=playbook_ref,
        manifest_ref=manifest_ref, wbs_ref=wbs_ref,
        work_product_run_ref=work_product_run_ref,
    )
    lines.append("")
    lines.append(summary)
    for step in horizon:
        lines.append("")
        lines.extend(_step_lines_trimmed(step))

    return "\n".join(lines)


# Maximum future execution steps to show in the projected window.
# Beyond this horizon, remaining steps are summarized with a count.
PROJECTED_FUTURE_HORIZON = 5


def _find_first_wbs_context_index(
    steps: tuple[ParsedPlanStep, ...],
) -> int:
    """Return index of the context step just before the first WBS step."""
    for i, step in enumerate(steps):
        if any("WBS Step" in ln for ln in step.lines):
            return max(0, i - 1)
    return 0


def _collect_horizon_steps(
    steps: tuple[ParsedPlanStep, ...] | list[ParsedPlanStep],
    current_number: int,
    future_limit: int,
    *,
    skip_completed_before_current: bool = False,
) -> tuple[list[ParsedPlanStep], int]:
    """Collect steps around the current step with a bounded future horizon.

    Returns the visible horizon steps and the total count of future
    steps encountered (including those beyond the limit).
    """
    horizon: list[ParsedPlanStep] = []
    future_count = 0
    past_current = False
    for step in steps:
        if step.number < current_number:
            if skip_completed_before_current and step.is_completed:
                continue
            horizon.append(step)
        elif step.number == current_number:
            horizon.append(step)
            past_current = True
        elif past_current:
            future_count += 1
            if future_count <= future_limit:
                horizon.append(step)
    return horizon, future_count


def _append_horizon_steps_and_remainder(
    lines: list[str],
    horizon: list[ParsedPlanStep],
    future_count: int,
    future_limit: int,
    *,
    remainder_label: str = "execution step(s)",
) -> None:
    """Append rendered horizon steps and an optional remainder note."""
    for step in horizon:
        lines.append("")
        lines.extend(_step_lines_trimmed(step))
    remaining = future_count - future_limit
    if remaining > 0:
        lines.append("")
        lines.append(f"... {remaining} more {remainder_label} follow.")


def _build_projected_plan_window(
    timestamp: str,
    parsed: ParsedPlan,
    current: ParsedPlanStep,
    *,
    plan_ref: str | None,
    playbook_ref: str | None,
    manifest_ref: str | None,
    wbs_ref: str | None,
    work_product_run_ref: str | None = None,
) -> str:
    """Projected plan view: trim planning prefix, bounded execution horizon.

    Shows the planning context step (step before first WBS), then the
    current step + a bounded future horizon. Steps beyond the horizon
    are summarized with a count so the model knows more work follows.
    """
    first_wbs_idx = _find_first_wbs_context_index(parsed.steps)
    visible_steps = parsed.steps[first_wbs_idx:]

    horizon, future_count = _collect_horizon_steps(
        visible_steps, current.number, PROJECTED_FUTURE_HORIZON,
    )

    lines = _build_window_header(
        timestamp,
        plan_ref=plan_ref, playbook_ref=playbook_ref,
        manifest_ref=manifest_ref, wbs_ref=wbs_ref,
        work_product_run_ref=work_product_run_ref,
    )
    _append_horizon_steps_and_remainder(
        lines, horizon, future_count, PROJECTED_FUTURE_HORIZON,
    )

    return "\n".join(lines)


# Number of future steps to show in non-WBS horizon windows.
_NON_WBS_FUTURE_HORIZON = 4


def _build_non_wbs_horizon_window(
    timestamp: str,
    parsed: ParsedPlan,
    current: ParsedPlanStep,
    *,
    plan_ref: str | None,
    playbook_ref: str | None,
    manifest_ref: str | None,
    wbs_ref: str | None,
    work_product_run_ref: str | None = None,
) -> str:
    """Horizon window for large non-WBS plans.

    Shows completed steps compactly, current step in full, then a
    bounded future horizon. Remaining steps are noted with a count.
    """
    completed_count = sum(1 for s in parsed.steps if s.is_completed)

    horizon, future_count = _collect_horizon_steps(
        parsed.steps, current.number, _NON_WBS_FUTURE_HORIZON,
        skip_completed_before_current=True,
    )

    lines = _build_window_header(
        timestamp,
        plan_ref=plan_ref, playbook_ref=playbook_ref,
        manifest_ref=manifest_ref, wbs_ref=wbs_ref,
        work_product_run_ref=work_product_run_ref,
    )
    if completed_count > 0:
        lines.append("")
        lines.append(
            f"Earlier {completed_count} step(s) completed successfully.",
        )
    _append_horizon_steps_and_remainder(
        lines, horizon, future_count, _NON_WBS_FUTURE_HORIZON,
        remainder_label="step(s)",
    )

    return "\n".join(lines)
