#!/usr/bin/env python3
"""Phase 0 freeze — deterministic continuation advances ONE step, no inference (no pytest).

Protects contract (2) of the Phase 0 "freeze current contracts" work
(``workbench/2026-07-01_claude_coding_agent_substrate_architecture_and_planning_v2.md``
PART VI): a deterministic continuation advances exactly one validated WBS step
and makes NO inference call.

Two layers, both production code:

* The validator ``core.result_processing.contracts.validate_deterministic_continuation``
  (called from ``coordinator.py:286``) proves the step is *validated* and
  *single*: a next step with more than one continuation action, a MIN_ACTIONS
  choice step, a non-deterministic current step, or an argument with no
  mechanical (closed-world) source are all rejected — the last is the "no
  inference-authored arguments" guarantee.
* The processor ``deterministic_continuation.DeterministicContinuationProcessor``
  submits exactly the one validated action and advances the plan once. It is
  structurally incapable of inference: its only collaborators are a submission
  service and a plan advancer (asserted here, so a regression that injects an
  inference collaborator breaks the freeze).

Offline: constructed inputs + recording stubs; no live solet / LM Studio / Postgres.

Run:
    .venv/bin/python3 \\
      ananta/tests/core/substrate_contracts/deterministic_continuation_single_step_smoke.py
"""

from __future__ import annotations

import dataclasses
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_REPO_ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(_HERE))
sys.path.insert(0, str(_REPO_ROOT / "ananta" / "src"))

from ananta.core.plans.parser import parse  # noqa: E402
from ananta.core.result_processing.contracts import (  # noqa: E402
    ResultContractViolationError,
    validate_deterministic_continuation,
)
from ananta.core.result_processing.deterministic_continuation import (  # noqa: E402
    DeterministicContinuationProcessor,
)
from substrate_contract_fixtures import (  # noqa: E402
    CONTRACT_WBS,
    CONTRACT_WBS_CURRENT_INFERENCE,
    CONTRACT_WBS_MIN_ACTIONS_NEXT,
    CONTRACT_WBS_MULTI_ACTION_NEXT,
    GRAFT_KEY,
    Checker,
    RecordingPlanAdvancer,
    RecordingSubmissionService,
    build_completed_action,
    build_continuation_input,
    build_validated_continuation,
)


def _violation(wbs: str, required_args: frozenset[str] | None = None) -> str | None:
    """Validate a cutover-derived input; return the raised invariant token."""
    plan = parse(wbs)
    payload = (
        build_continuation_input(plan, required_args=required_args)
        if required_args is not None
        else build_continuation_input(plan)
    )
    try:
        validate_deterministic_continuation(payload)
    except ResultContractViolationError as exc:
        invariant = exc.details.get("invariant")
        return invariant if isinstance(invariant, str) else "<no-invariant>"
    return None


def test_valid_input_advances_exactly_one_step(c: Checker) -> None:
    plan = parse(CONTRACT_WBS)
    result = validate_deterministic_continuation(build_continuation_input(plan))
    c.check(
        result.completed_step_number == 2 and result.next_step_number == 3,
        f"advances current step 2 -> next step 3 (got {result.completed_step_number}"
        f"->{result.next_step_number})",
    )
    c.check(
        result.next_step_number == result.completed_step_number + 1,
        "advances exactly one step (next == completed + 1)",
    )
    c.check(
        result.next_action_definition.get("process_key") == GRAFT_KEY,
        "the single next action is the graft-next-segment step",
    )


def test_multi_action_next_step_rejected(c: Checker) -> None:
    token = _violation(CONTRACT_WBS_MULTI_ACTION_NEXT)
    c.check(
        token == "next_step_continuation_key_count_invalid",
        f"a two-action next step is refused (proves exactly one) (got {token!r})",
    )


def test_min_actions_next_step_requires_inference(c: Checker) -> None:
    token = _violation(CONTRACT_WBS_MIN_ACTIONS_NEXT)
    c.check(
        token == "next_step_is_min_actions_choice",
        f"a MIN_ACTIONS choice next step is refused (needs inference) (got {token!r})",
    )


def test_non_deterministic_current_step_rejected(c: Checker) -> None:
    token = _violation(CONTRACT_WBS_CURRENT_INFERENCE)
    c.check(
        token == "current_step_kind_not_deterministic",
        f"an inference current step cannot deterministically continue (got {token!r})",
    )


def test_unbound_argument_not_mechanically_derivable(c: Checker) -> None:
    """No inference-authored args: a required arg with no closed-world source fails."""
    token = _violation(CONTRACT_WBS, required_args=frozenset({"unmapped_target"}))
    c.check(
        token == "arguments_not_mechanically_derivable",
        f"a required arg with no mechanical source is refused (got {token!r})",
    )


def test_processor_submits_one_and_advances_once(c: Checker) -> None:
    submitter = RecordingSubmissionService()
    advancer = RecordingPlanAdvancer()
    processor = DeterministicContinuationProcessor(
        submission_service=submitter,  # type: ignore[arg-type]
        plan_advancer=advancer,  # type: ignore[arg-type]
    )
    continuation = build_validated_continuation()
    processor.submit(
        completed=build_completed_action(),
        continuation=continuation,
        flow_token_id=None,
    )
    c.check(len(submitter.calls) == 1, "processor submits exactly one action")
    c.check(advancer.advance_count == 1, "processor advances the plan exactly once")
    if submitter.calls:
        call = submitter.calls[0]
        c.check(
            call["action_definition"] is continuation.next_action_definition,
            "the submitted action is the validated next action (no substitution)",
        )
        c.check(
            call["parent_action_id"] == continuation.completed_action_id,
            "the new action is parented to the completed action",
        )


def test_processor_has_no_inference_collaborator(c: Checker) -> None:
    """Structural freeze: the deterministic processor cannot call inference."""
    fields = {f.name for f in dataclasses.fields(DeterministicContinuationProcessor)}
    c.check(
        fields == {"submission_service", "plan_advancer"},
        f"processor collaborators are submission + advancer only (got {fields})",
    )


def test_glue_advancer_fails_loud_when_unresolvable(c: Checker) -> None:
    """The production advancer REFUSES to no-op when the service is absent.

    Live-proven failure shape (Track-A first production run, 2026-07-05):
    the advancer silently no-opped behind ``getattr(aqp, '_thinking_service',
    None)`` — an attribute NOTHING ever set — so the plan marker never moved
    and every multi-hop deterministic chain died at hop 2 on
    ``completed_key_not_declared_by_current_step``. The advancer now resolves
    the plan-lifecycle service lazily and raises when unresolvable; a silent
    no-op here only relocates the failure to a mute contract violation.
    """
    from ananta.core.actions.result_processing_glue import _AQPPlanAdvancer

    class _Focused:
        def get_focused(self) -> dict[str, object]:
            return {"memories": [], "count": 0}

    advancer = _AQPPlanAdvancer(
        memory_provider=_Focused(),
        plan_lifecycle_resolver=lambda: None,
    )
    try:
        advancer.advance(session_id="sess-smoke")
        c.check(False, "unresolvable plan-lifecycle service raises (no silent no-op)")
    except RuntimeError:
        c.check(True, "unresolvable plan-lifecycle service raises (no silent no-op)")


def main() -> int:
    c = Checker("Deterministic continuation: one step, no inference (Phase 0 contract 2)")
    print(f"=== {c.title} ===")
    test_valid_input_advances_exactly_one_step(c)
    test_multi_action_next_step_rejected(c)
    test_min_actions_next_step_requires_inference(c)
    test_non_deterministic_current_step_rejected(c)
    test_unbound_argument_not_mechanically_derivable(c)
    test_processor_submits_one_and_advances_once(c)
    test_processor_has_no_inference_collaborator(c)
    test_glue_advancer_fails_loud_when_unresolvable(c)
    return c.summary()


if __name__ == "__main__":
    sys.exit(main())
