"""Result-processing contract value objects and validation.

This module is the single source of truth for "is this successful tool
result a valid input for inference result-processing or deterministic
continuation, and if deterministic, what does the next action look like?"

The validators are pure: no inference is called, no I/O is performed,
no state is mutated.  They take pre-resolved inputs (parsed plan and
WBS, work-product register, process-schema lookups) and either return
an immutable validated context object or raise
:class:`ResultContractViolationError` with structured details.

Architecture references (handoff
``workbench/2026-05-03_codex_deterministic_result_processor.md``):

* Section 3 — Terminology (Result Processor Kind, Result Contract,
  Contract Violation).
* Section 9 — Mandatory Validation Gate (18 invariants, mechanically
  derived argument rule, companion key rule).
* Section 10 — Contract Violation Payload.

The validator caller is :mod:`ananta.core.result_processing.coordinator`
(Assignment 3); the contract violation payload feeds
:mod:`ananta.core.result_processing.error_dispatch` (Assignment 4).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import NoReturn, Protocol

from ananta.core.plans.types import (
    COMPANION_SUFFIXES,
    BoundSubStep,
    ParsedPlan,
    ParsedPlanStep,
)
from ananta.core.result_processing.enums import (
    ErrorProcessorKind,
    ResultProcessorKind,
)
from ananta.error_handling import FrameworkError

# ---------------------------------------------------------------------------
# Contract violation payload
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ResultContractViolationDetails:
    """Structured details for a result-contract violation.

    Carries the *invariant* identifier (e.g. ``"completed_process_key_mismatch"``)
    plus ``expected`` / ``observed`` payloads so the process-level error
    handler can render a precise recovery prompt.
    """

    action_id: str
    process_key: str
    invariant: str
    message: str
    completed_step_number: int | None
    result_processor_kind: ResultProcessorKind
    expected: Mapping[str, object]
    observed: Mapping[str, object]

    def to_error_details(self) -> dict[str, object]:
        """Render as :class:`FrameworkError` ``details`` dictionary.

        The shape matches handoff Section 10: ``error_kind``, ``invariant``,
        ``message``, ``action_id``, ``process_key``, ``result_processor_kind``,
        ``completed_step_number``, ``expected``, ``observed``.
        """
        return {
            "error_kind": "result_contract_violation",
            "invariant": self.invariant,
            "message": self.message,
            "action_id": self.action_id,
            "process_key": self.process_key,
            "completed_step_number": self.completed_step_number,
            "result_processor_kind": self.result_processor_kind.value,
            "expected": dict(self.expected),
            "observed": dict(self.observed),
        }


class ResultContractViolationError(FrameworkError):
    """Typed exception raised by the validator on any contract failure.

    The carried :class:`ResultContractViolationDetails` populates the
    :class:`FrameworkError` ``details`` dict so downstream handlers can
    inspect structured fields without parsing a message string.
    """

    def __init__(self, violation: ResultContractViolationDetails) -> None:
        self.violation = violation
        super().__init__(
            message=violation.message,
            error_code="result_processing.contract_violation",
            details=violation.to_error_details(),
        )


# ---------------------------------------------------------------------------
# Validated value objects
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ValidatedResultProcessingContext:
    """Common-success validation output.

    The result has cleared every shared invariant (dict shape, completed
    status, matching process key, no plugin-returned actions, valid error
    handler, valid kind).  It is now safe to dispatch by
    :attr:`result_processor_kind`.
    """

    action_id: str
    process_key: str
    result_processor_kind: ResultProcessorKind
    result_data: Mapping[str, object]


class ArgumentSource(StrEnum):
    """Closed set of mechanically derivable next-argument sources.

    Per handoff Section 9 / Mechanically Derived Argument Rule.  The
    deterministic processor (Assignment 3) consumes the source map to
    decide where each next-action argument gets its value from.
    """

    WBS_BOUND = "wbs_bound"
    COMPOSED = "composed"
    RUNTIME_ID = "runtime_id"
    WORK_PRODUCT_SLOT = "work_product_slot"
    RESULT_FIELD = "result_field"


@dataclass(frozen=True, slots=True)
class ValidatedDeterministicContinuation:
    """Deterministic-continuation validation output.

    The next planned action has been proven mechanically derivable:
    every required argument is sourced from a closed-world source
    (:class:`ArgumentSource`) and the next step satisfies every
    deterministic invariant.  :attr:`next_action_definition` carries
    the partially-resolved action ready for slot injection and
    submission by the deterministic processor.
    """

    completed_action_id: str
    completed_process_key: str
    completed_step_number: int
    next_step_number: int
    next_action_definition: Mapping[str, object]
    next_argument_sources: Mapping[str, ArgumentSource]


# ---------------------------------------------------------------------------
# Platform-owned source classification
# ---------------------------------------------------------------------------


# Argument names whose values are platform-owned runtime identifiers.
# Per handoff Section 9 — Mechanically Derived Argument Rule, sources 5.
PLATFORM_OWNED_RUNTIME_IDS: frozenset[str] = frozenset({
    "session_id",
    "flow_id",
    "context_id",
    "work_product_run_id",
    "wbs_id",
})


# ---------------------------------------------------------------------------
# Validator inputs
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class CommonSuccessInput:
    """Inputs to :func:`validate_common_success`.

    The validator does not touch the database; the caller hydrates the
    fields from ``core__action_events`` and ``core__action_results`` for
    the completed action.
    """

    action_id: str
    action_process_key: str
    completed_parameters: Mapping[str, object]
    # ``result_data`` is typed as ``object`` (not ``Mapping``) so the validator
    # can enforce the "result is a dictionary" invariant from handoff
    # Section 9 against arbitrary blob payloads coming off
    # ``core__action_results`` storage.
    result_data: object
    plugin_returned_actions: Sequence[Mapping[str, object]]
    error_processor: object | None
    result_processor_kind: ResultProcessorKind | None
    # ``error_processor_kind`` is consulted to relax the
    # "error_processor available" invariant for bridge-delivery
    # actions: the bridge dispatcher owns failure routing, so no
    # error-processor template is required.
    error_processor_kind: ErrorProcessorKind | None = None


@dataclass(frozen=True, slots=True)
class DeterministicContinuationInput:
    """Inputs to :func:`validate_deterministic_continuation`.

    The caller (Assignment 3 coordinator) supplies:

    * the action coordinates and validated result;
    * the parsed focused plan (with the currently-active step) and the
      parsed focused WBS source when present;
    * platform-owned runtime IDs and the work-product slot inventory
      per process key;
    * the required-argument inventory per process key (the keys the
      next process's invocation schema marks as required);
    * the contract's allow-list of result-field sources for next-step
      arguments (typically empty unless the WBS/Joseki source names a
      result field as the source for a specific argument).
    """

    action_id: str
    completed_process_key: str
    completed_parameters: Mapping[str, object]
    result_data: Mapping[str, object]
    result_processor_kind: ResultProcessorKind
    session_id: str | None
    flow_id: str | None
    context_id: str | None
    work_product_run_id: str | None
    wbs_id: str | None
    active_plan: ParsedPlan
    focused_wbs: ParsedPlan | None
    required_args_by_process: Mapping[str, frozenset[str]]
    owned_arg_slots_by_process: Mapping[str, frozenset[str]]
    allowed_result_field_sources: Mapping[str, str]


# ---------------------------------------------------------------------------
# Common success validation
# ---------------------------------------------------------------------------


def validate_common_success(
    payload: CommonSuccessInput,
) -> ValidatedResultProcessingContext:
    """Validate a successful tool result against the shared invariants.

    Raises :class:`ResultContractViolationError` on the first failed
    invariant.  Returns an immutable
    :class:`ValidatedResultProcessingContext` on success.
    """
    result_mapping = _check_result_is_dict(payload)
    _check_result_status_completed(payload, result_mapping)
    _check_result_process_key_matches(payload, result_mapping)
    _check_no_plugin_returned_actions(payload)
    _check_error_processor_available(payload)
    kind = _check_result_processor_kind(payload)
    return ValidatedResultProcessingContext(
        action_id=payload.action_id,
        process_key=payload.action_process_key,
        result_processor_kind=kind,
        result_data=result_mapping,
    )


def _check_result_is_dict(payload: CommonSuccessInput) -> Mapping[str, object]:
    if not isinstance(payload.result_data, Mapping):
        _raise(
            payload.action_id,
            payload.action_process_key,
            None,
            payload.result_processor_kind or ResultProcessorKind.INFERENCE,
            "result_not_dict",
            "Tool result must be a JSON object",
            expected={"type": "object"},
            observed={"type": type(payload.result_data).__name__},
        )
    return payload.result_data


def _check_result_status_completed(
    payload: CommonSuccessInput,
    result_mapping: Mapping[str, object],
) -> None:
    status = result_mapping.get("action_status")
    if status is None:
        status = result_mapping.get("status")
    status_value = getattr(status, "value", status)
    # ``success`` is the legacy service-side synonym (the action_processor
    # wraps non-plugin results as ``{"success": True, ...}``; some service
    # backends additionally set ``status="success"``).  Accept the three
    # synonyms uniformly rather than forcing every service to migrate.
    if status_value in ("completed", "succeeded", "success"):
        return

    # Distinguish "missing field" from "wrong value" — the recovery path
    # is different for each. Missing usually means a service-interface
    # method returned a raw payload dict that bypassed the action_processor
    # envelope wrapper (the wrapper at action_processor.execute_action
    # injects action_status="completed" precisely so service methods can
    # return plain dicts). Wrong value usually means the service
    # explicitly returned a non-completed action_status (soft failure).
    is_missing = "action_status" not in result_mapping and "status" not in result_mapping
    result_keys = sorted(str(k) for k in result_mapping)
    if is_missing:
        message = (
            "Tool result is missing both 'action_status' and 'status' fields. "
            "Service methods must either return an ActionResult-shaped dict "
            "(with action_status set) or rely on the action_processor "
            "envelope wrapper at action_processor.execute_action. "
            f"Result keys observed: {result_keys}"
        )
    else:
        message = (
            f"Tool result has action_status={status_value!r} "
            "(expected: 'completed', 'succeeded', or 'success'). "
            f"Result keys observed: {result_keys}"
        )
    _raise(
        payload.action_id,
        payload.action_process_key,
        None,
        payload.result_processor_kind or ResultProcessorKind.INFERENCE,
        "result_status_not_completed",
        message,
        expected={"action_status": "completed"},
        observed={
            "action_status": status_value,
            "result_keys": result_keys,
        },
    )


def _check_result_process_key_matches(
    payload: CommonSuccessInput,
    result_mapping: Mapping[str, object],
) -> None:
    result_pk = result_mapping.get("process_key")
    if result_pk is None:
        return
    if result_pk != payload.action_process_key:
        _raise(
            payload.action_id,
            payload.action_process_key,
            None,
            payload.result_processor_kind or ResultProcessorKind.INFERENCE,
            "result_process_key_mismatch",
            "Result process_key does not match action event process_key",
            expected={"process_key": payload.action_process_key},
            observed={"process_key": result_pk},
        )


def _check_no_plugin_returned_actions(payload: CommonSuccessInput) -> None:
    if len(payload.plugin_returned_actions) > 0:
        _raise(
            payload.action_id,
            payload.action_process_key,
            None,
            payload.result_processor_kind or ResultProcessorKind.INFERENCE,
            "plugin_returned_actions_present",
            (
                "Plugin returned actions must be processed before result "
                "contract validation; the validator only inspects "
                "model-independent paths."
            ),
            expected={"actions_count": 0},
            observed={"actions_count": len(payload.plugin_returned_actions)},
        )


def _check_error_processor_available(payload: CommonSuccessInput) -> None:
    # Bridge-delivery actions route failures through the bridge
    # dispatcher, not an error-processor template; the missing
    # template is expected and not a contract violation.
    if payload.error_processor_kind is ErrorProcessorKind.BRIDGE_DELIVERY:
        return
    if payload.error_processor is None:
        _raise(
            payload.action_id,
            payload.action_process_key,
            None,
            payload.result_processor_kind or ResultProcessorKind.INFERENCE,
            "error_processor_missing",
            (
                "Action has no process-level error handler available; "
                "contract violations would have nowhere to route."
            ),
            expected={"error_processor": "non-null"},
            observed={"error_processor": None},
        )


def _check_result_processor_kind(
    payload: CommonSuccessInput,
) -> ResultProcessorKind:
    kind = payload.result_processor_kind
    if kind is None:
        _raise(
            payload.action_id,
            payload.action_process_key,
            None,
            ResultProcessorKind.INFERENCE,
            "result_processor_kind_missing",
            (
                "Action has no result_processor_kind persisted; every "
                "plan-derived EDGE action must carry one."
            ),
            expected={
                "result_processor_kind": sorted(k.value for k in ResultProcessorKind),
            },
            observed={"result_processor_kind": None},
        )
    return kind


# ---------------------------------------------------------------------------
# Deterministic continuation validation
# ---------------------------------------------------------------------------


def validate_deterministic_continuation(
    payload: DeterministicContinuationInput,
) -> ValidatedDeterministicContinuation | None:
    """Validate the 18 deterministic-continuation invariants.

    Raises :class:`ResultContractViolationError` on the first failed
    invariant.  Returns an immutable
    :class:`ValidatedDeterministicContinuation` on success, or ``None``
    when the active plan is legitimately finished (JOS-07: the terminal
    step of a completed plan has no ``[>]`` current step by design — every
    step is ``[X]``/skipped and there is nothing left to advance to. That
    is NOT a contract violation; ``_check_current_step_resolved`` cannot
    distinguish "genuinely no current step because the plan is done" from
    "corrupted, no current step despite unfinished work" on its own, so
    the distinction is made here, before that check ever runs, via
    ``ParsedPlan.is_complete`` — false on an empty/malformed plan, so this
    does not weaken the existing corruption check).
    """
    if payload.active_plan.is_complete:
        return None
    current_step = _check_current_step_resolved(payload)
    _check_current_step_declares_completed_process_key(payload, current_step)
    _check_current_step_permits_deterministic(payload, current_step)
    next_step = _check_next_step_exists(payload, current_step)
    _check_next_step_declares_kind(payload, current_step, next_step)
    _check_next_step_kind_is_deterministic(payload, current_step, next_step)
    # Shape exclusions before the companion-key count so the validator
    # surfaces the precise reason (MIN_ACTIONS / planning-extension /
    # post_message) rather than the generic count mismatch they would
    # trigger.
    _check_next_step_not_excluded_shape(payload, current_step, next_step)
    next_bound = _check_next_step_single_deterministic_action(
        payload, current_step, next_step,
    )
    _check_runtime_ids_present(payload, current_step)
    sources = _check_arguments_mechanically_derived(
        payload, current_step, next_step, next_bound,
    )
    next_action_definition = _build_next_action_definition(
        payload, next_step, next_bound, sources,
    )
    return ValidatedDeterministicContinuation(
        completed_action_id=payload.action_id,
        completed_process_key=payload.completed_process_key,
        completed_step_number=current_step.number,
        next_step_number=next_step.number,
        next_action_definition=next_action_definition,
        next_argument_sources=sources,
    )


def _check_current_step_resolved(
    payload: DeterministicContinuationInput,
) -> ParsedPlanStep:
    current = payload.active_plan.current_step
    if current is None:
        _raise(
            payload.action_id,
            payload.completed_process_key,
            None,
            payload.result_processor_kind,
            "current_step_missing",
            "Active plan has no current ``[>]`` step",
            expected={"current_step": "present"},
            observed={"current_step": None},
        )
    assert current is not None  # narrowed for mypy
    if not current.process_keys:
        _raise(
            payload.action_id,
            payload.completed_process_key,
            current.number,
            payload.result_processor_kind,
            "current_step_not_executable",
            "Current step has no process keys; cannot be a Joseki/WBS executable step",
            expected={"process_keys_count": ">= 1"},
            observed={"process_keys_count": 0},
        )
    return current


def _check_current_step_declares_completed_process_key(
    payload: DeterministicContinuationInput,
    current: ParsedPlanStep,
) -> None:
    if payload.completed_process_key not in current.process_keys:
        _raise(
            payload.action_id,
            payload.completed_process_key,
            current.number,
            payload.result_processor_kind,
            "completed_key_not_declared_by_current_step",
            "Completed action's process_key is not declared by the current step",
            expected={"declared_keys": list(current.process_keys)},
            observed={"completed_process_key": payload.completed_process_key},
        )


def _check_current_step_permits_deterministic(
    payload: DeterministicContinuationInput,
    current: ParsedPlanStep,
) -> None:
    if current.result_processor_kind is not ResultProcessorKind.DETERMINISTIC_CONTINUATION:
        _raise(
            payload.action_id,
            payload.completed_process_key,
            current.number,
            payload.result_processor_kind,
            "current_step_kind_not_deterministic",
            (
                "Current step does not declare "
                "RESULT_PROCESSOR_KIND: deterministic_continuation"
            ),
            expected={
                "current_step_kind": ResultProcessorKind.DETERMINISTIC_CONTINUATION.value,
            },
            observed={
                "current_step_kind": (
                    current.result_processor_kind.value
                    if current.result_processor_kind else None
                ),
            },
        )


def _check_next_step_exists(
    payload: DeterministicContinuationInput,
    current: ParsedPlanStep,
) -> ParsedPlanStep:
    plan = payload.active_plan
    next_step: ParsedPlanStep | None = None
    for step in plan.steps:
        if step.number > current.number and not step.is_completed and not step.is_skipped:
            next_step = step
            break
    if next_step is None:
        _raise(
            payload.action_id,
            payload.completed_process_key,
            current.number,
            payload.result_processor_kind,
            "next_step_missing",
            "Active plan has no executable step after the current one",
            expected={"next_step": "present"},
            observed={"next_step": None},
        )
    assert next_step is not None  # narrowed for mypy
    return next_step


def _check_next_step_declares_kind(
    payload: DeterministicContinuationInput,
    current: ParsedPlanStep,
    next_step: ParsedPlanStep,
) -> None:
    if next_step.result_processor_kind is None:
        _raise(
            payload.action_id,
            payload.completed_process_key,
            current.number,
            payload.result_processor_kind,
            "next_step_kind_missing",
            "Next plan step does not declare RESULT_PROCESSOR_KIND",
            expected={"next_step_kind": "set"},
            observed={"next_step_number": next_step.number, "next_step_kind": None},
        )


def _check_next_step_kind_is_deterministic(
    payload: DeterministicContinuationInput,
    current: ParsedPlanStep,
    next_step: ParsedPlanStep,
) -> None:
    if next_step.result_processor_kind is not ResultProcessorKind.DETERMINISTIC_CONTINUATION:
        _raise(
            payload.action_id,
            payload.completed_process_key,
            current.number,
            payload.result_processor_kind,
            "next_step_kind_not_deterministic",
            (
                "Next plan step declares RESULT_PROCESSOR_KIND="
                f"{next_step.result_processor_kind} — deterministic "
                "continuation can only chain into another deterministic step"
            ),
            expected={
                "next_step_kind": ResultProcessorKind.DETERMINISTIC_CONTINUATION.value,
            },
            observed={
                "next_step_kind": (
                    next_step.result_processor_kind.value
                    if next_step.result_processor_kind else None
                ),
            },
        )


def _check_next_step_single_deterministic_action(
    payload: DeterministicContinuationInput,
    current: ParsedPlanStep,
    next_step: ParsedPlanStep,
) -> BoundSubStep:
    """Apply Section 9 Companion Key Rule and return the one continuation sub-step."""
    if len(next_step.continuation_keys) != 1:
        _raise(
            payload.action_id,
            payload.completed_process_key,
            current.number,
            payload.result_processor_kind,
            "next_step_continuation_key_count_invalid",
            (
                "Deterministic next step must have exactly one non-companion "
                "continuation key"
            ),
            expected={"continuation_keys_count": 1},
            observed={
                "next_step_number": next_step.number,
                "continuation_keys": list(next_step.continuation_keys),
                "companion_keys": list(next_step.companion_keys),
            },
        )
    target_key = next_step.continuation_keys[0]
    bound = [
        b for b in next_step.bound_sub_steps if b.process_key == target_key
    ]
    if len(bound) != 1:
        _raise(
            payload.action_id,
            payload.completed_process_key,
            current.number,
            payload.result_processor_kind,
            "next_step_bound_sub_step_count_invalid",
            (
                "Deterministic next step must have exactly one bound sub-step "
                "for the continuation process key"
            ),
            expected={"bound_sub_steps_for_key": 1},
            observed={"target_key": target_key, "bound_count": len(bound)},
        )
    return bound[0]


def _check_next_step_not_excluded_shape(
    payload: DeterministicContinuationInput,
    current: ParsedPlanStep,
    next_step: ParsedPlanStep,
) -> None:
    """Reject search, choice, planning-extension, and post_message shapes."""
    if next_step.min_actions is not None and next_step.min_actions >= 1:
        _raise(
            payload.action_id,
            payload.completed_process_key,
            current.number,
            payload.result_processor_kind,
            "next_step_is_min_actions_choice",
            "MIN_ACTIONS choice steps require inference",
            expected={"min_actions": None},
            observed={
                "next_step_number": next_step.number,
                "min_actions": next_step.min_actions,
            },
        )
    if next_step.has_planning_extension:
        _raise(
            payload.action_id,
            payload.completed_process_key,
            current.number,
            payload.result_processor_kind,
            "next_step_is_planning_extension",
            "Planning-extension upsert_plan steps require inference",
            expected={"planning_extension": False},
            observed={"next_step_number": next_step.number, "planning_extension": True},
        )
    for key in next_step.process_keys:
        # Match both the namespaced form (``service_interface::...::post_message``)
        # and the parser's pseudo-key form (``<POST_MESSAGE>``).
        if key.endswith("::post_message") or key == "<POST_MESSAGE>":
            _raise(
                payload.action_id,
                payload.completed_process_key,
                current.number,
                payload.result_processor_kind,
                "next_step_contains_post_message",
                "Steps containing post_message require inference",
                expected={"contains_post_message": False},
                observed={
                    "next_step_number": next_step.number,
                    "contains_post_message": True,
                    "matched_key": key,
                },
            )


def _check_runtime_ids_present(
    payload: DeterministicContinuationInput,
    current: ParsedPlanStep,
) -> None:
    missing = [
        name for name, value in (
            ("session_id", payload.session_id),
            ("flow_id", payload.flow_id),
        )
        if not value
    ]
    if missing:
        _raise(
            payload.action_id,
            payload.completed_process_key,
            current.number,
            payload.result_processor_kind,
            "runtime_ids_missing",
            "Required platform-owned runtime identifiers are missing",
            expected={"required": ["session_id", "flow_id"]},
            observed={"missing": missing},
        )


def _check_arguments_mechanically_derived(
    payload: DeterministicContinuationInput,
    current: ParsedPlanStep,
    next_step: ParsedPlanStep,
    next_bound: BoundSubStep,
) -> dict[str, ArgumentSource]:
    """Classify every required next-action argument by mechanical source.

    Returns a mapping from argument name to :class:`ArgumentSource`.
    Raises :class:`ResultContractViolationError` if any required argument
    lacks a known mechanical source.
    """
    required = payload.required_args_by_process.get(
        next_bound.process_key, frozenset(),
    )
    if not required:
        return {}

    owned_slots = payload.owned_arg_slots_by_process.get(
        next_bound.process_key, frozenset(),
    )
    bound_args = dict(next_bound.arguments or {})
    composed_targets = {ref.target_arg for ref in next_bound.composed_references}

    sources: dict[str, ArgumentSource] = {}
    unresolved: list[str] = []
    for arg in sorted(required):
        source = _classify_argument_source(
            arg=arg,
            bound_args=bound_args,
            composed_targets=composed_targets,
            owned_slots=owned_slots,
            allowed_result_fields=payload.allowed_result_field_sources,
        )
        if source is None:
            unresolved.append(arg)
        else:
            sources[arg] = source

    if unresolved:
        _raise(
            payload.action_id,
            payload.completed_process_key,
            current.number,
            payload.result_processor_kind,
            "arguments_not_mechanically_derivable",
            (
                f"Required next-action arguments {unresolved} for "
                f"{next_bound.process_key!r} have no mechanical source; "
                "deterministic continuation cannot fill them without "
                "inference."
            ),
            expected={
                "required_args": sorted(required),
                "allowed_sources": [s.value for s in ArgumentSource],
            },
            observed={
                "next_step_number": next_step.number,
                "next_process_key": next_bound.process_key,
                "unresolved_args": unresolved,
                "resolved_sources": {k: v.value for k, v in sources.items()},
            },
        )
    return sources


def _classify_argument_source(
    *,
    arg: str,
    bound_args: Mapping[str, object],
    composed_targets: frozenset[str] | set[str],
    owned_slots: frozenset[str],
    allowed_result_fields: Mapping[str, str],
) -> ArgumentSource | None:
    """Return the first matching source for *arg*, or ``None`` if unresolved.

    Precedence reflects the existing platform pipeline: literal
    ``Arguments:`` win, then ``Composed:`` references, then
    platform-owned IDs, then declared result-field sources, then
    work-product output slots.
    """
    if arg in bound_args:
        return ArgumentSource.WBS_BOUND
    if arg in composed_targets:
        return ArgumentSource.COMPOSED
    if arg in PLATFORM_OWNED_RUNTIME_IDS:
        return ArgumentSource.RUNTIME_ID
    if arg in allowed_result_fields:
        return ArgumentSource.RESULT_FIELD
    if arg in owned_slots:
        return ArgumentSource.WORK_PRODUCT_SLOT
    return None


def _build_next_action_definition(
    payload: DeterministicContinuationInput,
    next_step: ParsedPlanStep,
    next_bound: BoundSubStep,
    sources: Mapping[str, ArgumentSource],
) -> dict[str, object]:
    """Materialize the next action definition without model authoring.

    The output is what :class:`~ananta.core.actions.action_factory.ActionFactory`
    accepts: ``process_key``, ``arguments`` (literal + result-field
    values pre-filled; ``Composed:`` refs and work-product slots filled
    later by the deterministic processor's pipeline), ``notes``,
    routing IDs, and the step-level processor-kind annotation.
    """
    bound_args = dict(next_bound.arguments or {})
    arguments: dict[str, object] = dict(bound_args)
    for arg_name, source in sources.items():
        if source is ArgumentSource.RUNTIME_ID:
            arguments[arg_name] = _runtime_id_value(payload, arg_name)
        elif source is ArgumentSource.RESULT_FIELD:
            field = payload.allowed_result_field_sources[arg_name]
            arguments[arg_name] = payload.result_data.get(field)

    notes = next_step.summary()
    next_kind = next_step.result_processor_kind
    assert next_kind is not None  # invariant established by validator
    return {
        "process_key": next_bound.process_key,
        "arguments": arguments,
        "notes": notes,
        "session_id": payload.session_id,
        "flow_id": payload.flow_id,
        "context_id": payload.context_id,
        "result_processor_kind": next_kind.value,
    }


def _runtime_id_value(
    payload: DeterministicContinuationInput,
    name: str,
) -> str | None:
    if name == "session_id":
        return payload.session_id
    if name == "flow_id":
        return payload.flow_id
    if name == "context_id":
        return payload.context_id
    if name == "work_product_run_id":
        return payload.work_product_run_id
    if name == "wbs_id":
        return payload.wbs_id
    return None


# ---------------------------------------------------------------------------
# Companion key rule helpers
# ---------------------------------------------------------------------------


def is_companion_key(process_key: str) -> bool:
    """Return ``True`` for keys that are bookkeeping / communication.

    Mirrors :data:`ananta.core.plans.types.COMPANION_SUFFIXES`.  A
    companion key alone does not justify continuation; see handoff
    Section 9 Companion Key Rule.
    """
    return any(process_key.endswith(s) for s in COMPANION_SUFFIXES)


# ---------------------------------------------------------------------------
# Internal: raise helper
# ---------------------------------------------------------------------------


def _raise(
    action_id: str,
    process_key: str,
    completed_step_number: int | None,
    result_processor_kind: ResultProcessorKind,
    invariant: str,
    message: str,
    *,
    expected: Mapping[str, object],
    observed: Mapping[str, object],
) -> NoReturn:
    """Raise a structured :class:`ResultContractViolationError`.

    Typed :class:`NoReturn` so callers can rely on flow narrowing
    after invariant checks (e.g. treating a previously ``X | None`` as
    ``X`` past the guard).
    """
    raise ResultContractViolationError(
        ResultContractViolationDetails(
            action_id=action_id,
            process_key=process_key,
            invariant=invariant,
            message=message,
            completed_step_number=completed_step_number,
            result_processor_kind=result_processor_kind,
            expected=expected,
            observed=observed,
        ),
    )


# ---------------------------------------------------------------------------
# Bridge-delivery contract (handoff 2026-05-10)
# ---------------------------------------------------------------------------


_BRIDGE_DELIVERY_TRIGGER_KEYS: tuple[str, ...] = (
    "bridge_plugin_namespace",
    "bridge_id",
    "session_id",
    "deliver_result_process_key",
    "deliver_error_process_key",
)


@dataclass(frozen=True, slots=True)
class BridgeDeliveryTarget:
    """Resolved originator of a bridge-delivery action.

    Built by the validator from ``flow.trigger_data``.  Carries the
    process keys the dispatcher submits against on each delivery side
    so the dispatcher itself never reaches into loose dict probing
    (handoff 2026-05-10 Section 9).
    """

    plugin_namespace: str
    bridge_id: str
    session_id: str
    deliver_result_process_key: str
    deliver_error_process_key: str


@dataclass(frozen=True, slots=True)
class ValidatedBridgeDeliveryContext:
    """Success-side bridge-delivery validation output."""

    action_id: str
    process_key: str
    target: BridgeDeliveryTarget
    result_data: Mapping[str, object]


@dataclass(frozen=True, slots=True)
class ValidatedBridgeDeliveryFailureContext:
    """Failure-side bridge-delivery validation output.

    ``error_payload`` is the structured description of the failure that
    the bridge dispatcher delivers to the originator.  Schema is
    open-ended — callers commonly include ``error_message``,
    ``error_code``, ``details``, ``failed_arguments``.
    """

    action_id: str
    process_key: str
    target: BridgeDeliveryTarget
    error_payload: Mapping[str, object]


@dataclass(frozen=True, slots=True)
class BridgeDeliveryContractViolationDetails:
    """Structured payload for a bridge-delivery contract violation.

    Routes through the process-level error handler (inference) for
    the originating plugin's ``deliver_*`` process — the documented
    escape valve in handoff Section 8.
    """

    action_id: str
    process_key: str
    invariant: str
    message: str
    expected: Mapping[str, object]
    observed: Mapping[str, object]

    def to_error_details(self) -> dict[str, object]:
        return {
            "error_kind": "bridge_delivery_contract_violation",
            "invariant": self.invariant,
            "message": self.message,
            "action_id": self.action_id,
            "process_key": self.process_key,
            "expected": dict(self.expected),
            "observed": dict(self.observed),
        }


class BridgeDeliveryContractViolationError(FrameworkError):
    """Typed exception for failed bridge-delivery contract checks.

    Always routes to the process-level error handler **inference**
    path, regardless of the action's ``error_processor_kind`` — this
    is the documented escape valve (Section 8 final paragraph) so
    a corrupted or missing bridge state never leaves the agent without
    a human-readable explanation.
    """

    def __init__(
        self, violation: BridgeDeliveryContractViolationDetails,
    ) -> None:
        self.violation = violation
        super().__init__(
            message=violation.message,
            error_code="result_processing.bridge_delivery_contract_violation",
            details=violation.to_error_details(),
        )


# ── Validator inputs ────────────────────────────────────────────────


class FlowTriggerDataReader(Protocol):
    """Read the ``trigger_data`` for a flow row.

    Returns ``None`` when the flow does not exist or its trigger_data
    is unavailable.  The validator interprets ``None`` as "flow
    missing" and raises the appropriate invariant.
    """

    def read_trigger_data(self, flow_id: str) -> Mapping[str, object] | None: ...


class ProcessRegistryProbe(Protocol):
    """Check whether a process_key is registered with the platform."""

    def is_process_registered(self, process_key: str) -> bool: ...


@dataclass(frozen=True, slots=True)
class BridgeDeliverySuccessInput:
    """Inputs to :func:`validate_bridge_delivery_success`.

    The caller hydrates the fields from
    ``core__action_events`` and the stored result row; the validator
    resolves the originator via ``trigger_data_reader`` and verifies
    that the originator plugin's ``deliver_*`` processes are
    registered.
    """

    action_id: str
    action_process_key: str
    completed_parameters: Mapping[str, object]
    result_data: object
    action_session_id: str | None
    action_flow_id: str | None
    error_processor: object | None
    result_processor_kind: ResultProcessorKind | None
    error_processor_kind: ErrorProcessorKind | None
    trigger_data_reader: FlowTriggerDataReader
    process_registry_probe: ProcessRegistryProbe


@dataclass(frozen=True, slots=True)
class BridgeDeliveryFailureInput:
    """Inputs to :func:`validate_bridge_delivery_failure`.

    Same shape as :class:`BridgeDeliverySuccessInput` but carries the
    structured ``error_payload`` the dispatcher will deliver instead of
    a raw result.
    """

    action_id: str
    action_process_key: str
    completed_parameters: Mapping[str, object]
    error_payload: Mapping[str, object]
    action_session_id: str | None
    action_flow_id: str | None
    trigger_data_reader: FlowTriggerDataReader
    process_registry_probe: ProcessRegistryProbe


# ── Validators ──────────────────────────────────────────────────────


def validate_bridge_delivery_success(
    payload: BridgeDeliverySuccessInput,
) -> ValidatedBridgeDeliveryContext:
    """Validate a successful tool result for bridge delivery.

    Runs the same common-success invariants the inference and
    deterministic paths use (via
    :func:`validate_common_success`), then resolves the originator
    target via ``flow.trigger_data`` and verifies the bridge plugin
    exposes the ``deliver_result`` / ``deliver_error`` entry points.
    """
    # Common-success first (result shape, status, process_key match,
    # no plugin-returned actions, kind set).  ``error_processor``
    # absence is permitted on bridge-delivery actions because the
    # dispatcher owns failure routing — see _check_error_processor_available.
    common_input = CommonSuccessInput(
        action_id=payload.action_id,
        action_process_key=payload.action_process_key,
        completed_parameters=payload.completed_parameters,
        result_data=payload.result_data,
        plugin_returned_actions=(),
        error_processor=payload.error_processor,
        result_processor_kind=payload.result_processor_kind,
        error_processor_kind=payload.error_processor_kind,
    )
    common = validate_common_success(common_input)
    target = _resolve_bridge_delivery_target(
        action_id=payload.action_id,
        action_process_key=payload.action_process_key,
        action_session_id=payload.action_session_id,
        action_flow_id=payload.action_flow_id,
        trigger_data_reader=payload.trigger_data_reader,
        process_registry_probe=payload.process_registry_probe,
    )
    return ValidatedBridgeDeliveryContext(
        action_id=payload.action_id,
        process_key=payload.action_process_key,
        target=target,
        result_data=common.result_data,
    )


def validate_bridge_delivery_failure(
    payload: BridgeDeliveryFailureInput,
) -> ValidatedBridgeDeliveryFailureContext:
    """Validate a failed action for bridge delivery.

    No common-success invariants apply — the action failed, so there
    is no successful result to inspect.  The only checks are the
    bridge-delivery contract itself: a resolvable target and
    registered ``deliver_*`` processes.
    """
    target = _resolve_bridge_delivery_target(
        action_id=payload.action_id,
        action_process_key=payload.action_process_key,
        action_session_id=payload.action_session_id,
        action_flow_id=payload.action_flow_id,
        trigger_data_reader=payload.trigger_data_reader,
        process_registry_probe=payload.process_registry_probe,
    )
    return ValidatedBridgeDeliveryFailureContext(
        action_id=payload.action_id,
        process_key=payload.action_process_key,
        target=target,
        error_payload=payload.error_payload,
    )


def _resolve_bridge_delivery_target(
    *,
    action_id: str,
    action_process_key: str,
    action_session_id: str | None,
    action_flow_id: str | None,
    trigger_data_reader: FlowTriggerDataReader,
    process_registry_probe: ProcessRegistryProbe,
) -> BridgeDeliveryTarget:
    """Resolve and verify the originator target.

    Encodes the Section 8 mandatory checks for bridge delivery:
    flow exists + trigger_data resolves; required keys present;
    originator plugin exposes ``deliver_result`` and ``deliver_error``;
    delivery target session_id matches the action's session_id.
    """
    if not action_flow_id:
        _raise_bridge_violation(
            action_id, action_process_key,
            "bridge_target_flow_id_missing",
            "Action has no flow_id; cannot resolve bridge originator",
            expected={"flow_id": "non-null"},
            observed={"flow_id": action_flow_id},
        )
    trigger_data = trigger_data_reader.read_trigger_data(action_flow_id)
    if trigger_data is None:
        _raise_bridge_violation(
            action_id, action_process_key,
            "bridge_target_flow_missing",
            "Originating flow not found or has no trigger_data",
            expected={"flow_id": action_flow_id, "trigger_data": "present"},
            observed={"flow_id": action_flow_id, "trigger_data": None},
        )
    missing = [k for k in _BRIDGE_DELIVERY_TRIGGER_KEYS if not trigger_data.get(k)]
    if missing:
        _raise_bridge_violation(
            action_id, action_process_key,
            "bridge_target_trigger_data_incomplete",
            (
                "Originating flow trigger_data is missing required "
                f"delivery-target keys: {missing}"
            ),
            expected={"required_keys": list(_BRIDGE_DELIVERY_TRIGGER_KEYS)},
            observed={"missing": missing},
        )
    target_session = str(trigger_data["session_id"])
    if action_session_id and action_session_id != target_session:
        _raise_bridge_violation(
            action_id, action_process_key,
            "bridge_target_session_mismatch",
            (
                "Delivery target session_id does not match the "
                "action's session_id"
            ),
            expected={"action_session_id": action_session_id},
            observed={"target_session_id": target_session},
        )
    deliver_result_key = str(trigger_data["deliver_result_process_key"])
    deliver_error_key = str(trigger_data["deliver_error_process_key"])
    for key in (deliver_result_key, deliver_error_key):
        if not process_registry_probe.is_process_registered(key):
            _raise_bridge_violation(
                action_id, action_process_key,
                "bridge_target_process_not_registered",
                (
                    "Bridge delivery process_key is not registered: "
                    f"{key}"
                ),
                expected={"process_registered": True, "process_key": key},
                observed={"process_registered": False},
            )
    return BridgeDeliveryTarget(
        plugin_namespace=str(trigger_data["bridge_plugin_namespace"]),
        bridge_id=str(trigger_data["bridge_id"]),
        session_id=target_session,
        deliver_result_process_key=deliver_result_key,
        deliver_error_process_key=deliver_error_key,
    )


def _raise_bridge_violation(
    action_id: str,
    process_key: str,
    invariant: str,
    message: str,
    *,
    expected: Mapping[str, object],
    observed: Mapping[str, object],
) -> NoReturn:
    """Raise a structured :class:`BridgeDeliveryContractViolationError`."""
    raise BridgeDeliveryContractViolationError(
        BridgeDeliveryContractViolationDetails(
            action_id=action_id,
            process_key=process_key,
            invariant=invariant,
            message=message,
            expected=expected,
            observed=observed,
        ),
    )
