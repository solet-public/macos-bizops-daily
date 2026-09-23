"""WBS-to-plan projection — project WBS steps into plan-shaped text.

Reads a Work Breakdown Structure document and produces plan steps in
the canonical ``[ ] N. Work Item X.Y / WBS Step Z — Title`` format,
ready for grafting into the active plan.

Continuation tails for multi-phase workflows are appended automatically.

Per-work-item projection emits only the next incomplete work item from
a multi-work-item WBS, keeping projected plan size bounded.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass

from ananta.core.plans.parser import (
    assert_executable_joseki_wbs_steps_declare_kind,
    parse,
)
from ananta.core.result_processing import ResultProcessorKind

logger = logging.getLogger(__name__)

# ── WBS parsing regexes ────────────────────────────────────────────

WBS_STEP_RE = re.compile(r"^\[ \]\s+(\d+)\.\s+(.*)")
WBS_SUBSTEP_RE = re.compile(r"^\s+([a-z])\)\s+(.*)")
WBS_WORK_ITEM_RE = re.compile(r"^###\s+Work Item\s+([\d.]+):\s+(.*)")
WBS_AWAIT_RE = re.compile(r"^\[ \]\s+\d+\.\s+Await USER message\s*$")
WBS_PHASE_HEADER_RE = re.compile(r"^##\s+Phase\s+(\d+)\.")
WBS_JOSEKI_KEY_RE = re.compile(r"^JOSEKI_KEY:\s*(\S+)", re.MULTILINE)
# Step-level RESULT_PROCESSOR_KIND annotation as it appears inside a WBS
# source body.  Projection preserves the line on the projected step so the
# parser can recover the typed value downstream.
WBS_RESULT_PROCESSOR_KIND_RE = re.compile(
    r"^\s+RESULT_PROCESSOR_KIND:\s*(\S+)\s*$",
)
_STEP_ANNOTATION_RE = re.compile(
    r"<!--\s*Step\s+(\d+):\s*status=completed\b",
)
_TERMINAL_PROCESS_KEY = "record_work_breakdown_structure_step_state"
_NON_WORK_ITEM_H3_RE = re.compile(r"^###\s+(?!Work Item\s)")  # e.g. "### Phase 2 Completion"

# ── Work-item range dataclass ──────────────────────────────────────


@dataclass(frozen=True, slots=True)
class WorkItemRange:
    """Parsed work-item metadata extracted from a WBS document."""

    number: str  # e.g. "1", "1.1", "2"
    title: str
    first_step: int
    last_step: int
    terminal_step: int | None  # last step with record_...step_state
    first_line_index: int  # index into the WBS lines list
    last_line_index: int  # index of the last line belonging to this WI


# ── Work-item parsing ─────────────────────────────────────────────


@dataclass(slots=True)
class _WorkItemAccumulator:
    """Mutable state for building a :class:`WorkItemRange`."""

    number: str
    title: str
    first_line_index: int
    first_step: int | None = None
    last_step: int | None = None
    _current_step: int | None = None
    _terminal_step: int | None = None
    last_content_line: int = 0

    def record_step(self, step_num: int, line_index: int) -> None:
        """Track a WBS step line."""
        if self.first_step is None:
            self.first_step = step_num
        self.last_step = step_num
        self._current_step = step_num
        self.last_content_line = line_index

    def record_substep(self, line: str, line_index: int) -> None:
        """Track a WBS sub-step line."""
        if _TERMINAL_PROCESS_KEY in line and self._current_step is not None:
            self._terminal_step = self._current_step
        self.last_content_line = line_index

    def freeze(self) -> WorkItemRange | None:
        """Finalize into an immutable range, or ``None`` if incomplete."""
        if self.first_step is None:
            return None
        return WorkItemRange(
            number=self.number,
            title=self.title,
            first_step=self.first_step,
            last_step=self.last_step or self.first_step,
            terminal_step=self._terminal_step,
            first_line_index=self.first_line_index,
            last_line_index=self.last_content_line,
        )


def _close_accumulator(
    acc: _WorkItemAccumulator | None,
    items: list[WorkItemRange],
) -> None:
    """Freeze the accumulator and append to *items* if valid."""
    if acc is None:
        return
    frozen = acc.freeze()
    if frozen is not None:
        items.append(frozen)


def parse_work_items(wbs_content: str) -> list[WorkItemRange]:
    """Parse a WBS document into an ordered list of work-item ranges.

    Each ``### Work Item N: Title`` section is parsed for its first
    and last WBS step numbers, and whether the last step contains a
    terminal ``record_work_breakdown_structure_step_state`` call.

    Non-work-item trailing sections (e.g. ``### Phase 2 Completion``)
    are NOT included — they are handled as trailing content by the
    per-work-item projector.
    """
    lines = wbs_content.splitlines()
    items: list[WorkItemRange] = []
    acc: _WorkItemAccumulator | None = None

    for idx, line in enumerate(lines):
        wi_match = WBS_WORK_ITEM_RE.match(line)
        if wi_match:
            _close_accumulator(acc, items)
            acc = _WorkItemAccumulator(
                number=wi_match.group(1),
                title=wi_match.group(2).strip(),
                first_line_index=idx,
            )
            continue

        # Non-work-item ### headers (e.g. "### Phase 2 Completion")
        # close the current work item — trailing steps belong to the
        # completion section, not the last work item.
        if _NON_WORK_ITEM_H3_RE.match(line):
            _close_accumulator(acc, items)
            acc = None
            continue

        if acc is None:
            continue

        step_match = WBS_STEP_RE.match(line)
        if step_match:
            acc.record_step(int(step_match.group(1)), idx)
            continue

        sub_match = WBS_SUBSTEP_RE.match(line)
        if sub_match:
            acc.record_substep(line, idx)
            continue

        if line.strip():
            acc.last_content_line = idx

    _close_accumulator(acc, items)
    return items


def _parse_completed_steps(wbs_content: str) -> set[int]:
    """Extract the set of WBS step numbers with ``status=completed``."""
    return {int(m.group(1)) for m in _STEP_ANNOTATION_RE.finditer(wbs_content)}


def parse_completed_step_numbers(wbs_content: str) -> set[int]:
    """Public read of the durable step-completion annotations.

    The ``<!-- Step N: status=completed … -->`` annotations appended by
    ``wbs_lifecycle.record_step_state`` are the durable execution record;
    the pull engine (``pull_execution``, Phase 4) reads them to resume
    from the first unexecuted step after a driver disconnect.
    """
    return _parse_completed_steps(wbs_content)


def _find_next_incomplete(
    items: list[WorkItemRange],
    completed: set[int],
) -> WorkItemRange | None:
    """Find the first work item whose terminal step is not in *completed*."""
    for item in items:
        if item.terminal_step is None:
            # No terminal record step — item cannot be formally completed.
            return item
        if item.terminal_step not in completed:
            return item
    return None


def find_next_incomplete_work_item(
    wbs_content: str,
) -> WorkItemRange | None:
    """Find the first work item whose terminal step is not completed.

    Returns ``None`` when all work items are complete.
    """
    items = parse_work_items(wbs_content)
    if not items:
        return None
    return _find_next_incomplete(items, _parse_completed_steps(wbs_content))


# Phase-to-transition mapping for continuation tail generation.
#
# ``kind`` selects the emitted plan-step shape:
#
# - ``authored_register_wbs``: single-step authored-by-value transition —
#   the executing agent authors the next-phase WBS following the phase
#   guidance article and registers it via
#   ``register_authored_work_breakdown_structure`` (the qwen
#   push-generation verb was retired per DEP-01). Used for creative
#   phases where the agent owns WBS shape (assembly, delivery).
#
# - ``deterministic_generate_wbs``: single-step deterministic path —
#   the WBS is emitted from the upstream Pipeline Spec authored once
#   during continuation-plan setup, via ``generate_section_stem_wbs``
#   (or another registered generator). The phase boundary does NOT
#   re-author the spec — front-loaded piece-level decisions flow
#   through unchanged.
@dataclass(frozen=True, slots=True)
class PhaseContinuation:
    """How to emit the transition into the next phase."""

    kind: str  # "authored_register_wbs" | "deterministic_generate_wbs"
    wbs_article: str
    wbs_section: str
    entry_article: str
    entry_section: str


PHASE_CONTINUATION: dict[int, PhaseContinuation] = {
    1: PhaseContinuation(
        kind="deterministic_generate_wbs",
        wbs_article="phase2_wbs_transition.md",
        wbs_section="section-stem-phase-wbs-creation",
        entry_article="phase2_execution_entry.md",
        entry_section="section-stem-phase-graft",
    ),
    2: PhaseContinuation(
        kind="authored_register_wbs",
        wbs_article="phase3_wbs_transition.md",
        wbs_section="full-composition-assembly-phase-wbs-creation",
        entry_article="phase3_execution_entry.md",
        entry_section="full-composition-assembly-phase-graft",
    ),
    3: PhaseContinuation(
        kind="authored_register_wbs",
        wbs_article="phase4_wbs_transition.md",
        wbs_section="final-format-and-delivery-phase-wbs-creation",
        entry_article="phase4_execution_entry.md",
        entry_section="final-format-and-delivery-phase-graft",
    ),
}


@dataclass(slots=True)
class _ProjectionState:
    """Mutable loop state for line-by-line WBS projection."""

    steps: list[str]
    step_counter: int
    current_work_item: str = ""
    current_work_item_title: str = ""
    phase_number: int | None = None


def _ingest_phase_or_work_item(line: str, state: _ProjectionState) -> bool:
    """Update phase / work-item context from a header line.

    Returns ``True`` when the line was consumed (caller should ``continue``).
    """
    phase_match = WBS_PHASE_HEADER_RE.match(line)
    if phase_match:
        state.phase_number = int(phase_match.group(1))
        return True
    wi_match = WBS_WORK_ITEM_RE.match(line)
    if wi_match:
        state.current_work_item = f"Work Item {wi_match.group(1)}"
        state.current_work_item_title = wi_match.group(2).strip()
        return True
    return False


def _ingest_step_or_metadata(line: str, state: _ProjectionState) -> bool:
    """Emit a step / annotation / sub-step from a body line.

    Returns ``True`` when the line was consumed; ``False`` otherwise.
    Mutates ``state.steps`` and ``state.step_counter`` in place.
    """
    step_match = WBS_STEP_RE.match(line)
    if step_match:
        _emit_step_sequential(
            state.steps,
            step_match,
            line,
            state.step_counter,
            state.current_work_item,
            state.current_work_item_title,
        )
        state.step_counter += 1
        return True
    rpk_match = WBS_RESULT_PROCESSOR_KIND_RE.match(line)
    if rpk_match and state.steps:
        _emit_result_processor_kind(state.steps, rpk_match.group(1))
        return True
    sub_match = WBS_SUBSTEP_RE.match(line)
    if sub_match and state.steps:
        _emit_substep(state.steps, sub_match, state.current_work_item)
        return True
    return False


def project_wbs_to_plan_steps(wbs_content: str) -> str:
    """Project WBS steps into plan-shaped text for grafting.

    Reads the WBS document and produces plan steps in the format::

        [ ] 1. Work Item 1.1 / WBS Step 1 — <work_item_title> — <step_title>
            a) Work Item 1.1 / WBS Step 1 — <sub_step_desc> (<process_key>)

    Arguments blocks and Description lines are stripped — bound
    arguments are lifted from the focused WBS at execution time.

    For phases 1-3, appends a continuation tail with the next-phase
    WBS creation and execution-entry steps.
    """
    state = _ProjectionState(
        steps=["[X] 1. Phase execution segment grafted"],
        step_counter=2,
    )

    for line in wbs_content.splitlines():
        if _ingest_phase_or_work_item(line, state):
            continue
        _ingest_step_or_metadata(line, state)

    if state.phase_number is not None:
        _append_phase_continuation(state, wbs_content)

    projected = _join_plan_lines(state.steps)
    _assert_projected_steps_declare_kind(projected)
    return projected


def _append_phase_continuation(
    state: _ProjectionState,
    wbs_content: str,
) -> None:
    """Append the joseki or generic phase continuation tail."""
    assert state.phase_number is not None
    joseki_match = WBS_JOSEKI_KEY_RE.search(wbs_content)
    if joseki_match:
        _append_joseki_continuation_tail(
            state.steps,
            state.phase_number,
            joseki_match.group(1),
        )
    else:
        _append_continuation_tail(state.steps, state.phase_number)


def project_next_work_item(wbs_content: str, wbs_id: str = "") -> str:
    """Project only the next incomplete work item into plan-shaped text.

    Parses the WBS document, finds the first work item whose terminal
    ``record_work_breakdown_structure_step_state`` step has NOT been
    annotated with ``status=completed``, and projects only that work
    item's steps.

    **Non-final work items:** A generated step is appended that calls
    ``graft_work_breakdown_structure_segment`` again for the same WBS,
    so the next work item will be projected when the current one
    completes.

    **Final work item:** The final work item's steps are emitted,
    followed by any trailing non-work-item content (e.g.
    ``### Phase 2 Completion`` sections), then the joseki or phase
    continuation tail.

    Falls back to :func:`project_wbs_to_plan_steps` when no work items
    are found in the document (single-work-item WBS or legacy format).
    """
    items = parse_work_items(wbs_content)
    if not items:
        logger.info(
            "PER_WORK_ITEM: No work items found, falling back to full projection",
        )
        return project_wbs_to_plan_steps(wbs_content)

    completed = _parse_completed_steps(wbs_content)
    logger.info(
        "PER_WORK_ITEM: %d work items, completed_steps=%s, terminals=%s",
        len(items),
        sorted(completed),
        [(wi.number, wi.terminal_step) for wi in items],
    )
    target = _find_next_incomplete(items, completed)
    if target is None:
        logger.info(
            "PER_WORK_ITEM: All work items complete, projecting trailing content",
        )
        return _project_trailing_content(wbs_content, items)

    target_index = items.index(target)
    is_final = target_index == len(items) - 1
    logger.info(
        "PER_WORK_ITEM: Projecting Work Item %s (%s) [%d/%d, final=%s]",
        target.number,
        target.title,
        target_index + 1,
        len(items),
        is_final,
    )

    all_lines = wbs_content.splitlines()
    wi_lines = _extract_work_item_lines(all_lines, target, items, is_final)
    steps = _project_lines_to_steps(
        wi_lines,
        f"Work Item {target.number}",
        target.title,
    )

    if is_final:
        _maybe_append_continuation_tail(steps, wbs_content, all_lines)
    else:
        _append_next_work_item_graft_step(steps, wbs_id=wbs_id)

    projected = _join_plan_lines(steps)
    _assert_projected_steps_declare_kind(projected)
    return projected


def _extract_work_item_lines(
    all_lines: list[str],
    target: WorkItemRange,
    items: list[WorkItemRange],
    is_final: bool,
) -> list[str]:
    """Extract the lines belonging to a work item.

    For the final work item, includes trailing non-work-item content
    (e.g. ``### Phase 2 Completion`` sections with their steps).
    """
    start = target.first_line_index

    if is_final:
        # Include everything from this work item to end of document,
        # excluding annotation comments at the very end
        end = len(all_lines)
        # Trim trailing annotation lines and blank lines
        while end > start and (
            not all_lines[end - 1].strip() or _STEP_ANNOTATION_RE.match(all_lines[end - 1])
        ):
            end -= 1
        return all_lines[start:end]

    # Non-final: include lines up to the start of the next work item
    target_idx = items.index(target)
    next_item = items[target_idx + 1]
    end = next_item.first_line_index
    return all_lines[start:end]


def _extract_phase_number(lines: list[str]) -> int | None:
    """Extract the phase number from a WBS document's lines."""
    for line in lines:
        phase_match = WBS_PHASE_HEADER_RE.match(line)
        if phase_match:
            return int(phase_match.group(1))
    return None


def _project_lines_to_steps(
    source_lines: list[str],
    initial_work_item: str,
    initial_title: str,
) -> list[str]:
    """Project WBS source lines into plan step lines.

    Returns a mutable list of plan lines starting with a completed
    graft marker at position 1.
    """
    steps: list[str] = ["[X] 1. Phase execution segment grafted"]
    step_counter = 2
    current_wi = initial_work_item
    current_title = initial_title

    for line in source_lines:
        wi_match = WBS_WORK_ITEM_RE.match(line)
        if wi_match:
            current_wi = f"Work Item {wi_match.group(1)}"
            current_title = wi_match.group(2).strip()
            continue
        step_match = WBS_STEP_RE.match(line)
        if step_match:
            _emit_step_sequential(
                steps,
                step_match,
                line,
                step_counter,
                current_wi,
                current_title,
            )
            step_counter += 1
            continue
        rpk_match = WBS_RESULT_PROCESSOR_KIND_RE.match(line)
        if rpk_match and steps:
            _emit_result_processor_kind(steps, rpk_match.group(1))
            continue
        sub_match = WBS_SUBSTEP_RE.match(line)
        if sub_match:
            _emit_substep(steps, sub_match, current_wi)

    return steps


def _maybe_append_continuation_tail(
    steps: list[str],
    wbs_content: str,
    all_lines: list[str],
) -> None:
    """Append joseki or phase continuation tail if a phase header exists."""
    phase_number = _extract_phase_number(all_lines)
    if phase_number is None:
        return
    joseki_match = WBS_JOSEKI_KEY_RE.search(wbs_content)
    if joseki_match:
        _append_joseki_continuation_tail(
            steps,
            phase_number,
            joseki_match.group(1),
        )
    else:
        _append_continuation_tail(steps, phase_number)


def _project_trailing_content(
    wbs_content: str,
    items: list[WorkItemRange],
) -> str:
    """Project only trailing non-work-item content when all items are done.

    This handles the case where all work items are complete but trailing
    sections (e.g. ``### Phase 2 Completion``) still need projection,
    followed by the continuation tail.
    """
    all_lines = wbs_content.splitlines()
    last_item = items[-1]
    trailing_lines = all_lines[last_item.last_line_index + 1 :]
    steps = _project_lines_to_steps(trailing_lines, "", "")
    _maybe_append_continuation_tail(steps, wbs_content, all_lines)
    projected = _join_plan_lines(steps)
    _assert_projected_steps_declare_kind(projected)
    return projected


def _append_next_work_item_graft_step(steps: list[str], wbs_id: str = "") -> None:
    """Append a generated step that re-grafts the next work item."""
    next_num = _next_step_number(steps)
    steps.append(
        f"[ ] {next_num}. Graft the next work item from the WBS",
    )
    steps.append(f"    RESULT_PROCESSOR_KIND: {ResultProcessorKind.INFERENCE.value}")
    if wbs_id:
        steps.append(
            f"    Description: Provide wbs_id={wbs_id} and "
            "anchor_step_number (this step number). The platform reads the "
            "WBS and projects the next incomplete work item automatically. "
            "Do not provide a segment argument.",
        )
    else:
        steps.append(
            "    Description: Provide wbs_id (from ACTIVE_WBS) and "
            "anchor_step_number (this step number). The platform reads the "
            "WBS and projects the next incomplete work item automatically. "
            "Do not provide a segment argument.",
        )
    steps.append(
        "    a) Graft next work item "
        "(service_interface::thinking_service::"
        "graft_work_breakdown_structure_segment)",
    )


def _emit_step_sequential(
    steps: list[str],
    match: re.Match[str],
    line: str,
    plan_num: int,
    work_item: str,
    work_item_title: str,
) -> None:
    """Append a projected step header with a caller-supplied plan step number."""
    wbs_num = match.group(1)
    step_title = match.group(2).strip()
    if WBS_AWAIT_RE.match(line):
        steps.append(f"[-] {plan_num}. Await USER message")
    else:
        prefix = f"{work_item} / WBS Step {wbs_num}"
        if work_item_title:
            steps.append(
                f"[ ] {plan_num}. {prefix} — {work_item_title} — {step_title}",
            )
        else:
            steps.append(f"[ ] {plan_num}. {prefix} — {step_title}")


def _emit_substep(
    steps: list[str],
    match: re.Match[str],
    work_item: str,
) -> None:
    """Append a projected sub-step to the steps list."""
    letter = match.group(1)
    description = match.group(2)
    for candidate in reversed(steps):
        parent_match = re.search(r"WBS Step (\d+)", candidate)
        if parent_match and not candidate.lstrip().startswith(
            ("a)", "b)", "c)", "d)", "e)", "f)"),
        ):
            prefix = f"{work_item} / WBS Step {parent_match.group(1)}"
            steps.append(f"    {letter}) {prefix} — {description}")
            return


def _assert_projected_steps_declare_kind(projected_plan_text: str) -> None:
    """Fail projection if any executable projected step lacks the annotation.

    The projected plan text does not carry the Joseki/WBS execution
    headers (`ACTIVE_WBS:` etc.) until ``transitions.inject_wbs_headers``
    runs, so the parser's own document-level predicate does not yet
    fire.  Projection is the canonical caller that knows the output is
    destined for a Joseki/WBS execution context, so it enforces the
    annotation here for a clear, source-grounded error message.
    """
    parsed = parse(projected_plan_text)
    assert_executable_joseki_wbs_steps_declare_kind(parsed.steps)


def _emit_result_processor_kind(steps: list[str], raw: str) -> None:
    """Append a validated ``RESULT_PROCESSOR_KIND:`` line.

    Raises :class:`ValueError` if the value is not a known
    :class:`ResultProcessorKind` member, or if it is ``bridge_delivery``
    — that variant is platform-set on direct MCP invocations and must
    never appear in plan or WBS source (handoff 2026-05-10 Section 7).
    """
    try:
        kind = ResultProcessorKind(raw)
    except ValueError as exc:
        allowed = sorted(
            k.value for k in ResultProcessorKind
            if k is not ResultProcessorKind.BRIDGE_DELIVERY
        )
        msg = (
            f"RESULT_PROCESSOR_KIND_INVALID: WBS source declares "
            f"RESULT_PROCESSOR_KIND={raw!r}; allowed values: {allowed}"
        )
        raise ValueError(msg) from exc
    if kind is ResultProcessorKind.BRIDGE_DELIVERY:
        msg = (
            "RESULT_PROCESSOR_KIND_FORBIDDEN: WBS source declares "
            "RESULT_PROCESSOR_KIND='bridge_delivery'; bridge delivery "
            "is platform-set on direct MCP process_call invocations "
            "only and must never appear in plan or WBS source."
        )
        raise ValueError(msg)
    steps.append(f"    RESULT_PROCESSOR_KIND: {kind.value}")


def _append_continuation_tail(
    steps: list[str],
    phase_number: int,
) -> None:
    """Append next-phase planning steps after the WBS execution steps.

    Dispatches on ``PHASE_CONTINUATION[phase_number].kind`` to emit the
    authored-by-value single-step (agent authors + registers the WBS)
    or the two-step deterministic pipeline_spec flow.
    """
    continuation = PHASE_CONTINUATION.get(phase_number)
    if continuation is None:
        return
    if continuation.kind == "deterministic_generate_wbs":
        _append_deterministic_wbs_continuation(
            steps, phase_number, continuation,
        )
        return
    _append_authored_wbs_continuation(steps, phase_number, continuation)


def _append_authored_wbs_continuation(
    steps: list[str],
    phase_number: int,
    continuation: PhaseContinuation,
) -> None:
    """Emit the single-step authored-by-value WBS registration transition."""
    next_phase = phase_number + 1
    next_num = _next_step_number(steps)

    steps.append(
        f"[ ] {next_num}. Author and register the Phase {next_phase} "
        f"Work Breakdown Structure",
    )
    steps.append(f"    RESULT_PROCESSOR_KIND: {ResultProcessorKind.INFERENCE.value}")
    steps.append(f"    GUIDANCE_ARTICLE: {continuation.wbs_article}")
    steps.append(f"    GUIDANCE_SECTION: {continuation.wbs_section}")
    steps.append(
        f"    a) Author the Phase {next_phase} WBS document following the "
        "phase guidance and register it by value "
        "(service_interface::thinking_service::register_authored_work_breakdown_structure)",
    )

    steps.append(
        f"[ ] {next_num + 1}. Graft the Phase {next_phase} execution segment into the active plan",
    )
    steps.append(f"    RESULT_PROCESSOR_KIND: {ResultProcessorKind.INFERENCE.value}")
    steps.append(f"    GUIDANCE_ARTICLE: {continuation.entry_article}")
    steps.append(f"    GUIDANCE_SECTION: {continuation.entry_section}")
    steps.append(
        "    a) Graft the projected Phase "
        f"{next_phase} execution segment into the active plan "
        "(service_interface::thinking_service::graft_work_breakdown_structure_segment)",
    )

    steps.append(
        f"[ ] {next_num + 2}. Execute Phase {next_phase}",
    )
    steps.append(
        f"    a) <Projected Phase {next_phase} execution step> "
        f"(<Projected Phase {next_phase} process key>)",
    )


def _append_deterministic_wbs_continuation(
    steps: list[str],
    phase_number: int,
    continuation: PhaseContinuation,
) -> None:
    """Emit the deterministic generate_wbs flow.

    The Pipeline Spec is authored once during continuation-plan setup
    (before Phase 1) and reused at every phase boundary. The phase
    boundary itself does NOT re-author the spec.

    Step layout:
      N.   Generate the Phase ``next`` Work Breakdown Structure
           deterministically
           (``generate_section_stem_wbs`` reads the upstream Pipeline
           Spec, loads the style-family schema, validates, and emits
           the WBS)
      N+1. Graft the Phase ``next`` execution segment
      N+2. Execute Phase ``next``
    """
    next_phase = phase_number + 1
    next_num = _next_step_number(steps)

    steps.append(
        f"[ ] {next_num}. Generate the Phase {next_phase} Work Breakdown Structure",
    )
    steps.append(f"    RESULT_PROCESSOR_KIND: {ResultProcessorKind.INFERENCE.value}")
    steps.append(f"    GUIDANCE_ARTICLE: {continuation.wbs_article}")
    steps.append(f"    GUIDANCE_SECTION: {continuation.wbs_section}")
    steps.append(
        "    a) Generate the Work Breakdown Structure deterministically from "
        "the upstream Pipeline Spec — pass pipeline_spec_id (the active "
        "Pipeline Spec authored once for this manifest during continuation-"
        "plan setup) plus style_family and artifact_prefix from the Work "
        "Manifest "
        "(service_interface::thinking_service::generate_section_stem_wbs)",
    )

    steps.append(
        f"[ ] {next_num + 1}. Graft the Phase {next_phase} execution segment into the active plan",
    )
    steps.append(f"    RESULT_PROCESSOR_KIND: {ResultProcessorKind.INFERENCE.value}")
    steps.append(f"    GUIDANCE_ARTICLE: {continuation.entry_article}")
    steps.append(f"    GUIDANCE_SECTION: {continuation.entry_section}")
    steps.append(
        "    a) Graft the projected Phase "
        f"{next_phase} execution segment into the active plan "
        "(service_interface::thinking_service::graft_work_breakdown_structure_segment)",
    )

    steps.append(
        f"[ ] {next_num + 2}. Execute Phase {next_phase}",
    )
    steps.append(
        f"    a) <Projected Phase {next_phase} execution step> "
        f"(<Projected Phase {next_phase} process key>)",
    )


def _append_joseki_continuation_tail(
    steps: list[str],
    phase_number: int,
    completed_joseki_key: str,
) -> None:
    """Append joseki-program continuation after a joseki fragment.

    Instead of hard-coded next-phase WBS creation, appends steps that
    consult the manifest's joseki program to determine the next fragment.
    The next fragment is authored by the agent following the joseki card
    and registered via ``register_authored_work_breakdown_structure``
    (the qwen push-generation verb was retired per DEP-01), with a
    phase-transition fallback if this was the last joseki in the
    current phase.
    """
    next_num = _next_step_number(steps)

    steps.append(
        f"[ ] {next_num}. Continue joseki program — "
        f"consult the Work Manifest's Phase {phase_number} joseki chain",
    )
    steps.append(f"    RESULT_PROCESSOR_KIND: {ResultProcessorKind.INFERENCE.value}")
    steps.append(
        "    Description: The joseki fragment for "
        f"'{completed_joseki_key}' is complete. "
        "Read the focused Work Manifest to determine whether "
        "another joseki fragment follows in this phase or "
        "whether the phase is complete and should transition.",
    )
    steps.append(
        "    a) Search the knowledge base for the next joseki card "
        "(service_interface::knowledge_service::search)",
    )

    steps.append(
        f"[ ] {next_num + 1}. Author the next joseki WBS fragment or transition to the next phase",
    )
    steps.append(f"    RESULT_PROCESSOR_KIND: {ResultProcessorKind.INFERENCE.value}")
    steps.append("    MIN_ACTIONS: 1")
    steps.append(
        "    a) Author the next joseki WBS fragment following the joseki "
        "card and register it by value "
        "(service_interface::thinking_service::register_authored_work_breakdown_structure)",
    )
    steps.append(
        "    b) Close the phase "
        "(service_interface::thinking_service::record_work_manifest_phase_state)",
    )
    steps.append(
        "    c) (<POST_MESSAGE>)",
    )


def _next_step_number(steps: list[str]) -> int:
    """Compute the next sequential step number from existing steps."""
    last_step_line = next(
        (s for s in reversed(steps) if not s.startswith("    ")),
        "",
    )
    last_num_match = re.match(r"\[.\]\s+(\d+)\.", last_step_line)
    return int(last_num_match.group(1)) + 1 if last_num_match else len(steps) + 1


def _join_plan_lines(steps: list[str]) -> str:
    """Join step lines with blank lines between step blocks."""
    result: list[str] = []
    for s in steps:
        if s.startswith("    "):
            result.append(s)
        else:
            if result:
                result.append("")
            result.append(s)
    return "\n".join(result)
