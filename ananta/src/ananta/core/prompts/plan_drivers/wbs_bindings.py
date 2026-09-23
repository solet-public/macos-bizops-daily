"""WBS binding helpers — lift bound arguments, synthesize delivery bindings.

Pure functions for extracting bound arguments from WBS steps and
synthesizing record-state and delivery arguments during WBS execution.
"""

from __future__ import annotations

import json
import re

from ananta.core.plans import BoundSubStep, ParsedPlanStep, parse

# Regex constants for WBS text parsing.
WBS_STEP_REF_RE = re.compile(r"WBS Step (\d+)")
WBS_ID_RE = re.compile(r"^WBS ID:\s*(\S+)", re.MULTILINE)
MANIFEST_ID_RE = re.compile(r"^WORK_MANIFEST:\s*(\S+)", re.MULTILINE)
PHASE_NUMBER_RE = re.compile(r"^##\s+Phase\s+(\d+)\.", re.MULTILINE)

RECORD_WBS_STEP_STATE_SUFFIX = (
    "::record_work_breakdown_structure_step_state"
)
RECORD_MANIFEST_PHASE_STATE_SUFFIX = (
    "::record_work_manifest_phase_state"
)


def resolve_wbs_step_number(step: ParsedPlanStep) -> int | None:
    """Extract the WBS step number from a projected active plan step."""
    match = WBS_STEP_REF_RE.search(step.full_text())
    return int(match.group(1)) if match else None


def extract_step_description(wbs_step: ParsedPlanStep) -> str:
    """Extract the Description field from a WBS step."""
    desc_lines: list[str] = []
    collecting = False
    for line in wbs_step.lines:
        stripped = line.strip()
        if stripped.startswith("Description:"):
            desc_lines.append(stripped)
            collecting = True
        elif collecting:
            if stripped.startswith(("a)", "b)", "c)", "Arguments:")):
                break
            if stripped:
                desc_lines.append(stripped)
            else:
                break
    if not desc_lines:
        return ""
    return "Step description:\n" + "\n".join(desc_lines)


def _is_placeholder(value: object) -> bool:
    """Check if a value is an unresolved angle-bracket placeholder."""
    return isinstance(value, str) and value.startswith("<") and value.endswith(">")


def _filter_visible_args(
    arguments: dict[str, object],
    schema_visible_keys: set[str] | None,
) -> dict[str, object]:
    """Return only schema-visible argument entries."""
    if schema_visible_keys is None:
        return arguments
    return {k: v for k, v in arguments.items() if k in schema_visible_keys}


def _collect_entry_lines(
    bs: BoundSubStep,
    schema_visible_keys: set[str] | None,
) -> tuple[list[str], list[str]]:
    """Return (bound_lines, unbound_lines) for one bound sub-step entry."""
    bound: list[str] = []
    unbound: list[str] = []
    if isinstance(bs.arguments, dict):
        visible = _filter_visible_args(bs.arguments, schema_visible_keys)
        for k, v in visible.items():
            if _is_placeholder(v):
                unbound.append(f"  {k}: compose from the step description")
            else:
                bound.append(f"  {k}: {v}")
    else:
        bound.append(f"  {json.dumps(bs.arguments, separators=(',', ':'))}")
    return bound, unbound


def format_bound_entries(
    wbs_step: ParsedPlanStep,
    *,
    schema_visible_keys: set[str] | None = None,
) -> str:
    """Format bound arguments from a parsed WBS step.

    When ``schema_visible_keys`` is provided, only arguments whose keys
    appear in that set are shown.  Arguments the model cannot emit (e.g.
    platform-owned slots, array-of-objects properties stripped from the
    decode contract) are omitted so the driver and the schema stay
    aligned.
    """
    entries = [bs for bs in wbs_step.bound_sub_steps if bs.arguments is not None]
    if not entries:
        return ""
    bound_lines: list[str] = []
    unbound_lines: list[str] = []
    for bs in entries:
        b, u = _collect_entry_lines(bs, schema_visible_keys)
        bound_lines.extend(b)
        unbound_lines.extend(u)
    lines: list[str] = []
    if bound_lines:
        lines.append("Bound arguments — use these exact values:")
        lines.extend(bound_lines)
    if unbound_lines:
        if lines:
            lines.append("")
        lines.append("Unbound arguments — compose from the step description:")
        lines.extend(unbound_lines)
    if not lines:
        return ""
    lines.append("")
    lines.append("Keep step_summary to one sentence. Keep reason to one sentence.")
    return "\n".join(lines)


def step_has_post_message_sub_step(step: ParsedPlanStep) -> bool:
    """Check whether this step has a ``<POST_MESSAGE>`` sub-step."""
    return any(
        bs.process_key == "<POST_MESSAGE>"
        for bs in step.bound_sub_steps
    )


def resolve_session_id_from_context(session_id: str) -> str:
    """Extract the originating session_id."""
    return session_id or ""


def lift_bound_arguments_from_wbs(
    wbs_text: str,
    step: ParsedPlanStep,
    session_id: str = "",
    delivery_attachment: str | None = None,
    *,
    schema_visible_keys: set[str] | None = None,
) -> tuple[str, str | None, str]:
    """Lift bound arguments from the focused WBS for the current step.

    Returns (bound_text, delivery_attachment, delivery_session_id).
    The delivery fields are populated only for <POST_MESSAGE> steps.

    When ``schema_visible_keys`` is provided, the bound-argument block
    only shows arguments that appear in the decode contract schema.
    """
    wbs_step_number = resolve_wbs_step_number(step)
    if wbs_step_number is None:
        return "", None, ""

    wbs_parsed = parse(wbs_text)
    wbs_step = wbs_parsed.step_by_number(wbs_step_number)
    if wbs_step is None:
        return "", None, ""

    desc_block = extract_step_description(wbs_step)
    bound_block = format_bound_entries(
        wbs_step, schema_visible_keys=schema_visible_keys,
    )
    record_block = synthesize_record_step_args(wbs_text, wbs_step, wbs_step_number)

    # Steps that pair a bound audio (or other) action with a
    # record-state action need BOTH blocks. Returning early after only
    # bound_block leaves the model without wbs_id/step_number/status
    # for record_work_breakdown_structure_step_state.
    parts = [b for b in (desc_block, bound_block, record_block) if b]
    if parts:
        return "\n\n".join(parts), None, ""

    if step_has_post_message_sub_step(wbs_step):
        text, attachment, sess = synthesize_delivery_bindings(
            delivery_attachment, session_id,
        )
        return text, attachment, sess

    return "", None, ""


def synthesize_record_step_args(
    wbs_text: str,
    wbs_step: ParsedPlanStep,
    wbs_step_number: int,
) -> str:
    """Synthesize bound arguments for record-state sub-steps."""
    result = synthesize_wbs_step_state(wbs_text, wbs_step, wbs_step_number)
    if result:
        return result
    return synthesize_manifest_phase_state(wbs_text, wbs_step)


def synthesize_wbs_step_state(
    wbs_text: str,
    wbs_step: ParsedPlanStep,
    wbs_step_number: int,
) -> str:
    """Synthesize args for record_work_breakdown_structure_step_state."""
    has_record = any(
        bs.process_key.endswith(RECORD_WBS_STEP_STATE_SUFFIX)
        for bs in wbs_step.bound_sub_steps
    )
    if not has_record:
        return ""

    wbs_id_match = WBS_ID_RE.search(wbs_text)
    if not wbs_id_match:
        return ""

    wbs_id = wbs_id_match.group(1)
    record_key = next(
        bs.process_key
        for bs in wbs_step.bound_sub_steps
        if bs.process_key.endswith(RECORD_WBS_STEP_STATE_SUFFIX)
    )

    synthesized: dict[str, object] = {
        "wbs_id": wbs_id,
        "step_number": wbs_step_number,
        "status": "completed",
    }

    wbs_parsed = parse(wbs_text)
    last_wbs_step = wbs_parsed.steps[-1] if wbs_parsed.steps else None
    is_completion_handoff = (
        last_wbs_step is not None
        and last_wbs_step.number == wbs_step_number
    )

    lines = [
        "Bound arguments from the approved Work Breakdown Structure:",
        "",
        f"  {record_key}:",
        f"    {json.dumps(synthesized, separators=(',', ':'))}",
        "",
        "The platform will inject these bound values into your "
        "action arguments automatically. Emit only the required "
        "arguments (those in the response schema) in your response.",
    ]

    if is_completion_handoff:
        lines.append("")
        lines.append(
            "For the record_work_breakdown_structure_step_state action:",
        )
        lines.append(f"- `wbs_id:` `{wbs_id}`")
        lines.append(f"- `step_number:` `{wbs_step_number}`")
        lines.append("- `status:` `completed`")
        lines.append(
            "- `state_summary:` one short sentence stating that the "
            "phase work is complete and the next phase should be "
            "planned next",
        )
        lines.append(
            "- `output_artifacts:` include the key output filename "
            "from this phase",
        )

    return "\n".join(lines)


def synthesize_manifest_phase_state(
    wbs_text: str,
    wbs_step: ParsedPlanStep,
) -> str:
    """Synthesize args for record_work_manifest_phase_state."""
    has_record = any(
        bs.process_key.endswith(RECORD_MANIFEST_PHASE_STATE_SUFFIX)
        for bs in wbs_step.bound_sub_steps
    )
    if not has_record:
        return ""

    manifest_match = MANIFEST_ID_RE.search(wbs_text)
    phase_match = PHASE_NUMBER_RE.search(wbs_text)
    if not manifest_match or not phase_match:
        return ""

    manifest_id = manifest_match.group(1)
    phase_number = int(phase_match.group(1))
    record_key = next(
        bs.process_key
        for bs in wbs_step.bound_sub_steps
        if bs.process_key.endswith(RECORD_MANIFEST_PHASE_STATE_SUFFIX)
    )

    synthesized: dict[str, object] = {
        "manifest_id": manifest_id,
        "phase_number": phase_number,
        "status": "completed",
        "outcome_summary": f"Phase {phase_number} execution completed",
    }

    return "\n".join([
        "Bound arguments from the approved Work Breakdown Structure:",
        "",
        f"  {record_key}:",
        f"    {json.dumps(synthesized, separators=(',', ':'))}",
        "",
        "The platform will inject these bound values into your "
        "action arguments automatically. Emit only the required "
        "arguments (those in the response schema) in your response.",
    ])


def synthesize_delivery_bindings(
    final_filename: str | None,
    session_id: str,
) -> tuple[str, str | None, str]:
    """Synthesize delivery bindings for a ``<POST_MESSAGE>`` step.

    Returns (text, final_filename, session_id).
    """
    if not final_filename:
        return "", None, ""

    lines = [
        "Delivery binding synthesized from the approved Work Breakdown Structure:",
        "",
        f"  The final delivery artifact is: `{final_filename}`",
        f"  Use `{final_filename}` as the attachment ref in the "
        "post_message attachments array.",
    ]
    if session_id:
        lines.append(f"  session_id: `{session_id}`")
    lines.extend([
        "",
        "Do not invent a descriptive attachment name. Use the exact "
        "filename above.",
    ])

    return "\n".join(lines), final_filename, session_id
