"""Pull-based WBS step execution (Phase 4, Seam C).

An external agent drives WBS execution over MCP without surrendering its
reasoning to a local model: it PULLS the next executable step (with
resolved arguments, support articles, the expected result contract, and
completion criteria), executes it with its own tools, and PUSHES an
observation back. The platform validates every observation BEFORE any
state advances — an invalid observation changes nothing.

Durability / disconnect-resume (POR Phase 4 ◆R2): this module keeps NO
session state. Completion is read from the ``<!-- Step N: status=completed
… -->`` annotations that :func:`ananta.core.plans.wbs_lifecycle.
record_step_state` appends to the WBS document in the knowledge base —
the same durable substrate the graft projector reads. A driver that dies
mid-WBS loses nothing: a fresh session calls ``start_wbs_execution`` /
``get_next_wbs_step`` and resumes from the first unexecuted step.

Q15 (POR ~:604): auto-submission is offered ONLY for the intersection of
(a) an EXPLICIT ``AUTO_SAFE: true`` author annotation, (b)
``RESULT_PROCESSOR_KIND: deterministic_continuation``, and (c) a fully
closed-world single-action argument set (bound literals + resolved
``Composed:`` references). Everything else returns ``mode='agent_review'``
so control goes back to the agent. The eligibility rules adapt the
deterministic-continuation invariants (``result_processing/contracts.py``)
to pull-mode state, where completion lives in annotations rather than
active-plan ``[>]`` markers.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from ananta.core.plans.contracts.action_contract import resolve_single_composed_source
from ananta.core.plans.parser import parse
from ananta.core.plans.projection import parse_completed_step_numbers

if TYPE_CHECKING:
    from ananta.core.plans.types import ParsedPlan, ParsedPlanStep

# Result-envelope status values accepted as success — the same three
# synonyms ``validate_common_success`` accepts (contracts.py).
_SUCCESS_STATUSES = ("completed", "succeeded", "success")

# Envelope kinds returned by :func:`next_step_envelope`.
KIND_EXECUTE = "execute"
KIND_AWAIT_USER = "await_user"
KIND_COMPLETE = "complete"

# Advance modes (Q15).
MODE_AUTO_SAFE = "auto_safe"
MODE_AGENT_REVIEW = "agent_review"
MODE_COMPLETE = "complete"


@dataclass(frozen=True)
class SubStepView:
    """One sub-step of a pull-mode envelope, arguments resolved."""

    label: str
    description: str
    process_key: str
    arguments: dict[str, Any]
    unresolved_composed: tuple[str, ...]

    def to_payload(self) -> dict[str, Any]:
        return {
            "label": self.label,
            "description": self.description,
            "process_key": self.process_key,
            "arguments": dict(self.arguments),
            "unresolved_composed": list(self.unresolved_composed),
        }


@dataclass(frozen=True)
class StepEnvelope:
    """Everything a pull-mode driver needs to execute one step."""

    kind: str
    wbs_id: str
    step_number: int | None = None
    title: str = ""
    process_keys: tuple[str, ...] = ()
    sub_steps: tuple[SubStepView, ...] = ()
    support_articles: tuple[str, ...] = ()
    result_processor_kind: str | None = None
    auto_safe: bool = False
    expected_result_contract: dict[str, Any] = field(default_factory=dict)
    completion_criteria: str = ""
    remaining_step_numbers: tuple[int, ...] = ()

    def to_payload(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "wbs_id": self.wbs_id,
            "step_number": self.step_number,
            "title": self.title,
            "process_keys": list(self.process_keys),
            "sub_steps": [s.to_payload() for s in self.sub_steps],
            "support_articles": list(self.support_articles),
            "result_processor_kind": self.result_processor_kind,
            "auto_safe": self.auto_safe,
            "expected_result_contract": dict(self.expected_result_contract),
            "completion_criteria": self.completion_criteria,
            "remaining_step_numbers": list(self.remaining_step_numbers),
        }


# ---------------------------------------------------------------------------
# Completion / next-step resolution (annotation-based, durable)
# ---------------------------------------------------------------------------


def executed_step_numbers(wbs_content: str, parsed: ParsedPlan) -> set[int]:
    """Steps that count as executed in pull mode.

    Union of the durable KB annotations (``status=completed``) and any
    ``[X]``/``[-]`` markers already present in the document — markers
    appear when a WBS rode the classic projected-plan path before the
    pull driver took over.
    """
    executed = parse_completed_step_numbers(wbs_content)
    for step in parsed.steps:
        if step.is_completed or step.is_skipped:
            executed.add(step.number)
    return executed


def remaining_steps(
    parsed: ParsedPlan, executed: set[int],
) -> list[ParsedPlanStep]:
    """Plan steps not yet executed, in document order."""
    return [s for s in parsed.steps if s.number not in executed]


def next_step_envelope(wbs_id: str, wbs_content: str) -> StepEnvelope:
    """Build the pull-mode envelope for the next unexecuted step.

    ``kind='execute'`` carries a full step envelope; ``kind='await_user'``
    means the next step is a non-executable control step (the driver must
    stop and involve the operator); ``kind='complete'`` means every step
    is executed.
    """
    parsed = parse(wbs_content)
    executed = executed_step_numbers(wbs_content, parsed)
    pending = remaining_steps(parsed, executed)
    if not pending:
        return StepEnvelope(kind=KIND_COMPLETE, wbs_id=wbs_id)

    step = pending[0]
    if not step.process_keys:
        return StepEnvelope(
            kind=KIND_AWAIT_USER,
            wbs_id=wbs_id,
            step_number=step.number,
            title=_step_title(step),
            completion_criteria=(
                "Non-executable control step — stop pulling and involve "
                "the operator; record an observation only after the "
                "control condition is satisfied."
            ),
            remaining_step_numbers=tuple(s.number for s in pending),
        )

    return StepEnvelope(
        kind=KIND_EXECUTE,
        wbs_id=wbs_id,
        step_number=step.number,
        title=_step_title(step),
        process_keys=step.process_keys,
        sub_steps=tuple(_sub_step_views(step, parsed)),
        support_articles=step.support_articles,
        result_processor_kind=(
            step.result_processor_kind.value
            if step.result_processor_kind
            else None
        ),
        auto_safe=step.auto_safe,
        expected_result_contract=_expected_result_contract(step),
        completion_criteria=_completion_criteria(step),
        remaining_step_numbers=tuple(s.number for s in pending),
    )


def _step_title(step: ParsedPlanStep) -> str:
    """The step header line without marker/number prefix."""
    if not step.lines:
        return ""
    header = step.lines[0].strip()
    _, _, title = header.partition(f"{step.number}.")
    return title.strip() or header


def _sub_step_views(
    step: ParsedPlanStep, parsed: ParsedPlan,
) -> list[SubStepView]:
    """Resolve every bound sub-step's arguments for the envelope."""
    views: list[SubStepView] = []
    for bound in step.bound_sub_steps:
        arguments: dict[str, Any] = dict(bound.arguments or {})
        unresolved: list[str] = []
        for ref in bound.composed_references or ():
            resolved = [
                value
                for source_step in ref.source_steps
                if (value := resolve_single_composed_source(
                    ref, source_step, parsed,
                )) is not None
            ]
            if len(resolved) == len(ref.source_steps):
                arguments[ref.target_arg] = (
                    resolved[0] if len(resolved) == 1 else resolved
                )
            else:
                unresolved.append(ref.target_arg)
        views.append(
            SubStepView(
                label=bound.label,
                description=_sub_step_description(step, bound.process_key),
                process_key=bound.process_key,
                arguments=arguments,
                unresolved_composed=tuple(unresolved),
            ),
        )
    return views


def _sub_step_description(step: ParsedPlanStep, process_key: str) -> str:
    """The sub-step line text for *process_key*, if present."""
    needle = f"({process_key})"
    for line in step.lines:
        if needle in line:
            return line.strip()
    return ""


def _expected_result_contract(step: ParsedPlanStep) -> dict[str, Any]:
    return {
        "result_status_one_of": list(_SUCCESS_STATUSES),
        "process_key_one_of": list(step.process_keys),
        "observation_shape": {
            "process_key": "the executed process key",
            "result": "the tool's result envelope (dict)",
            "state_summary": "optional short summary for the durable record",
            "output_artifacts": "optional list of produced artifact names",
        },
    }


def _completion_criteria(step: ParsedPlanStep) -> str:
    terminal = any(
        "record_work_breakdown_structure_step_state" in key
        for key in step.process_keys
    )
    base = (
        f"Execute every sub-step, then record ONE observation for step "
        f"{step.number} with a success-status result envelope. The step "
        f"completes only when the platform validates and records it."
    )
    if terminal:
        base += (
            " This is a terminal step-state record — completing it marks "
            "its work item complete."
        )
    return base


# ---------------------------------------------------------------------------
# Observation validation (nothing advances unless this passes)
# ---------------------------------------------------------------------------


def validate_observation(
    wbs_id: str,
    wbs_content: str,
    step_number: int,
    process_key: str,
    result: object,
) -> list[str]:
    """Validate a pull-mode observation. Empty list = valid.

    Mirrors the ``validate_common_success`` invariants (result is a dict
    with a success status) plus the pull-mode ordering rules: the step
    must exist, must not already be executed, and must be THE next
    unexecuted step (out-of-order observations are rejected — the WBS is
    a sequence, and skipping breaks Composed references and work-product
    handoffs). The declared process key must belong to the step.
    """
    parsed = parse(wbs_content)
    step = next((s for s in parsed.steps if s.number == step_number), None)
    if step is None:
        return [f"step {step_number} does not exist in WBS {wbs_id!r}"]

    errors: list[str] = []
    executed = executed_step_numbers(wbs_content, parsed)
    if step_number in executed:
        errors.append(
            f"step {step_number} is already executed — observations are "
            f"append-once; nothing was changed",
        )
    pending = remaining_steps(parsed, executed)
    if pending and pending[0].number != step_number and step_number not in executed:
        errors.append(
            f"step {step_number} is not the next unexecuted step "
            f"(next is {pending[0].number}) — out-of-order observations "
            f"are rejected",
        )
    if process_key not in step.process_keys:
        errors.append(
            f"process key {process_key!r} is not declared by step "
            f"{step_number} (declared: {list(step.process_keys)})",
        )
    errors.extend(_result_envelope_errors(result))
    return errors


def _result_envelope_errors(result: object) -> list[str]:
    """The result must be a dict carrying a success status."""
    if not isinstance(result, dict):
        return [
            f"observation result must be a JSON object "
            f"(got {type(result).__name__})",
        ]
    status = result.get("action_status", result.get("status"))
    status_value = getattr(status, "value", status)
    if status_value not in _SUCCESS_STATUSES:
        return [
            f"observation result status must be one of "
            f"{list(_SUCCESS_STATUSES)} (got {status_value!r}) — record "
            f"failures by repairing and re-executing, not by observing "
            f"a failed result",
        ]
    return []


# ---------------------------------------------------------------------------
# Q15 advance evaluation
# ---------------------------------------------------------------------------


def advance_evaluation(wbs_id: str, wbs_content: str) -> dict[str, Any]:
    """Evaluate what the driver may do with the next step (Q15).

    Returns ``mode='auto_safe'`` with a fully-built, closed-world action
    definition ONLY when the next step is executable, explicitly marked
    ``AUTO_SAFE: true``, declares deterministic continuation, has exactly
    one continuation sub-step, and every argument resolves mechanically.
    Otherwise ``mode='agent_review'`` with the reasons, or
    ``mode='complete'``.
    """
    envelope = next_step_envelope(wbs_id, wbs_content)
    if envelope.kind == KIND_COMPLETE:
        return {"mode": MODE_COMPLETE, "wbs_id": wbs_id, "reasons": []}

    reasons = _auto_safe_blockers(envelope)
    if reasons:
        return {
            "mode": MODE_AGENT_REVIEW,
            "wbs_id": wbs_id,
            "step_number": envelope.step_number,
            "reasons": reasons,
            "envelope": envelope.to_payload(),
        }

    action_sub_step = envelope.sub_steps[0]
    return {
        "mode": MODE_AUTO_SAFE,
        "wbs_id": wbs_id,
        "step_number": envelope.step_number,
        "reasons": [],
        "action_definition": {
            "process_key": action_sub_step.process_key,
            "arguments": dict(action_sub_step.arguments),
        },
        "envelope": envelope.to_payload(),
    }


def _auto_safe_blockers(envelope: StepEnvelope) -> list[str]:
    """Every reason the next step must go back to the agent (Q15)."""
    reasons: list[str] = []
    if envelope.kind != KIND_EXECUTE:
        reasons.append(f"next step is kind={envelope.kind!r}, not executable")
        return reasons
    if not envelope.auto_safe:
        reasons.append("step is not explicitly marked AUTO_SAFE: true")
    if envelope.result_processor_kind != "deterministic_continuation":
        reasons.append(
            "step does not declare "
            "RESULT_PROCESSOR_KIND: deterministic_continuation",
        )
    if len(envelope.sub_steps) != 1:
        reasons.append(
            f"auto-safe requires exactly one bound sub-step "
            f"(step has {len(envelope.sub_steps)})",
        )
    for sub in envelope.sub_steps:
        if sub.unresolved_composed:
            reasons.append(
                f"Composed reference(s) unresolved: "
                f"{list(sub.unresolved_composed)}",
            )
    return reasons
