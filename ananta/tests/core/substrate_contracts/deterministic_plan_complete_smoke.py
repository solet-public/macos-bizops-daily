#!/usr/bin/env python3
"""Smoke: JOS-07 — terminal-step completion must NOT raise current_step_missing.

Drives the REAL ``SuccessfulResultCoordinator`` (production code, same
collaborator seam as ``call_site_wiring_smoke.py`` proves is wired) with a
genuine ``core.plans.parser.parse``-parsed plan, exactly the shape a plan
takes once its last step's completion has already been recorded: every step
``[X]``, no ``[>]`` anywhere.

Before the fix, ``validate_deterministic_continuation`` could not distinguish
that legitimate end state from a corrupted/empty plan that never had a
current step — both raised ``current_step_missing`` as a full
``ResultContractViolationError``, recorded in
``core__result_processing_violations`` and routed to the completed action's
process-level error handler, on every single completed run. The fix checks
``ParsedPlan.is_complete`` before that check ever runs and returns a
dedicated ``DispatchOutcome.DETERMINISTIC_PLAN_COMPLETE`` instead.

Three legs, because the fix is a discrimination, not a suppression:

  1. **Terminal-complete plan** (the JOS-07 case) — outcome
     ``deterministic_plan_complete``; error dispatcher NOT called; submitter
     NOT called.
  2. **Empty plan** (genuine corruption; the pre-existing
     ``core__inference_deferred_vertex``-adjacent case the 2026-05-03
     substrate-contract smoke first pinned) — MUST still raise
     ``current_step_missing`` as a real violation. This is the important
     leg: it proves ``is_complete`` did not quietly widen into the
     corruption case (``is_complete`` is ``False`` on an empty plan —
     ``bool(steps)`` is falsy — so the two cases stay distinguishable by
     construction, not by coincidence).
  3. **Mid-plan happy path** (step 2 active, step 3 pending) — unaffected;
     still submits the next step as ``deterministic_submitted``.

Offline: constructed inputs + recording stubs; no live solet / LM Studio /
Postgres. Reuses ``substrate_contract_fixtures``'s real registered
``thinking_service`` process keys so the whole-tree integration gate's
call-site check has real verbs to validate against.

Run:
    .venv/bin/python3 \\
      ananta/tests/core/substrate_contracts/deterministic_plan_complete_smoke.py
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_REPO_ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(_HERE))
sys.path.insert(0, str(_REPO_ROOT / "ananta" / "src"))

from ananta.core.plans.parser import parse  # noqa: E402
from ananta.core.plans.types import ParsedPlan  # noqa: E402
from ananta.core.result_processing.coordinator import (  # noqa: E402
    CompletedAction,
    DeterministicResolvedContext,
    DispatchOutcome,
    SuccessfulResultCoordinator,
)
from ananta.core.result_processing.enums import ResultProcessorKind  # noqa: E402
from substrate_contract_fixtures import (  # noqa: E402
    GRAFT_KEY,
    OUTLINE_KEY,
    RECORD_STEP_KEY,
)

_passed = 0
_failed: list[str] = []


def _check(condition: object, label: str) -> None:
    global _passed
    if condition:
        _passed += 1
        print(f"  PASS  {label}")
    else:
        _failed.append(label)
        print(f"  FAIL  {label}")


# ---------------------------------------------------------------------------
# Recording collaborator stubs (mirrors the 2026-05-03 coordinator harness)
# ---------------------------------------------------------------------------


@dataclass
class _Inference:
    calls: list[tuple[str, str, str]] = field(default_factory=list)

    def dispatch_inference(self, *, completed, validated, flow_token_id) -> None:
        self.calls.append(("inference", completed.action_id, flow_token_id or ""))


@dataclass
class _Error:
    calls: list[tuple[str, str, str, str]] = field(default_factory=list)

    def dispatch_violation(self, *, completed, violation, flow_token_id) -> None:
        self.calls.append((
            "violation",
            completed.action_id,
            violation.violation.invariant,
            flow_token_id or "",
        ))


@dataclass
class _Submitter:
    calls: list[tuple[str, str, int, str]] = field(default_factory=list)

    def submit(self, *, completed, continuation, flow_token_id) -> None:
        self.calls.append((
            "submit",
            completed.action_id,
            continuation.next_step_number,
            flow_token_id or "",
        ))


@dataclass
class _Resolver:
    plan: ParsedPlan
    required_args: dict[str, frozenset[str]] = field(default_factory=dict)

    def resolve(self, *, completed) -> DeterministicResolvedContext:
        return DeterministicResolvedContext(
            active_plan=self.plan,
            focused_wbs=None,
            wbs_id="wbs-jos07-smoke",
            work_product_run_id=None,
            required_args_by_process=self.required_args,
            owned_arg_slots_by_process={},
            allowed_result_field_sources={},
        )


class _StubBridgeSubmitter:
    def dispatch_success(self, *, completed, validated, flow_token_id) -> None:
        raise AssertionError("bridge dispatch not expected in this smoke")


class _StubTriggerDataReader:
    def read_trigger_data(self, flow_id):
        return None


class _StubProcessRegistryProbe:
    def is_process_registered(self, process_key) -> bool:
        return False


def _coord(*, inf: _Inference, err: _Error, sub: _Submitter, res: _Resolver) -> SuccessfulResultCoordinator:
    return SuccessfulResultCoordinator(
        inference_dispatcher=inf,
        deterministic_context_resolver=res,
        deterministic_submitter=sub,
        bridge_delivery_submitter=_StubBridgeSubmitter(),
        trigger_data_reader=_StubTriggerDataReader(),
        process_registry_probe=_StubProcessRegistryProbe(),
        error_dispatcher=err,
    )


def _completed(kind: ResultProcessorKind | None) -> CompletedAction:
    return CompletedAction(
        action_id="ae-jos07-1",
        process_key=RECORD_STEP_KEY,
        parameters={"wbs_id": "wbs-jos07-smoke"},
        notes="step",
        result_processor=None,
        error_processor={"template": "process_error"},
        result_processor_kind=kind,
        result_processor_target=None,
        session_id="sess-jos07",
        flow_id="flow-jos07",
        context_id="ctx-jos07",
    )


# ---------------------------------------------------------------------------
# Fixture plan text
# ---------------------------------------------------------------------------

_HEADER = "ACTIVE_WBS: wbs-jos07-smoke\n\n"

# Every step [X] — the shape the active plan takes the instant the LAST step's
# own completion has been recorded and there is nothing left to advance to.
TERMINAL_COMPLETE_WBS = _HEADER + (
    "[X] 1. Outline the work breakdown structure\n"
    "    RESULT_PROCESSOR_KIND: deterministic_continuation\n"
    f"    a) Register authored WBS ({OUTLINE_KEY})\n"
    "        Arguments:\n"
    '        {"wbs_id": "wbs-jos07-smoke"}\n'
    "[X] 2. Record the current step state\n"
    "    RESULT_PROCESSOR_KIND: deterministic_continuation\n"
    f"    a) Record step state ({RECORD_STEP_KEY})\n"
    "        Arguments:\n"
    '        {"wbs_id": "wbs-jos07-smoke"}\n'
)

MID_PLAN_WBS = _HEADER + (
    "[X] 1. Outline the work breakdown structure\n"
    "    RESULT_PROCESSOR_KIND: deterministic_continuation\n"
    f"    a) Register authored WBS ({OUTLINE_KEY})\n"
    "        Arguments:\n"
    '        {"wbs_id": "wbs-jos07-smoke"}\n'
    "[>] 2. Record the current step state\n"
    "    RESULT_PROCESSOR_KIND: deterministic_continuation\n"
    f"    a) Record step state ({RECORD_STEP_KEY})\n"
    "        Arguments:\n"
    '        {"wbs_id": "wbs-jos07-smoke"}\n'
    "[ ] 3. Graft the next work item\n"
    "    RESULT_PROCESSOR_KIND: deterministic_continuation\n"
    f"    a) Graft next segment ({GRAFT_KEY})\n"
    "        Arguments:\n"
    '        {"wbs_id": "wbs-jos07-smoke", "anchor_step_number": "2"}\n'
)

EMPTY_PLAN = ParsedPlan(header_lines=(), steps=())


# ---------------------------------------------------------------------------
# Cases
# ---------------------------------------------------------------------------


def _case_terminal_complete_plan_does_not_violate() -> None:
    print(
        "\nCase 1: terminal-complete plan (JOS-07) — no spurious violation, "
        "no submit",
    )
    plan = parse(TERMINAL_COMPLETE_WBS)
    _check(plan.is_complete, "fixture sanity: parsed plan reports is_complete=True")
    _check(plan.current_step is None, "fixture sanity: parsed plan has no [>] current step")

    inf, err, sub = _Inference(), _Error(), _Submitter()
    coord = _coord(inf=inf, err=err, sub=sub, res=_Resolver(plan=plan))
    outcome = coord.handle_successful_result(
        completed=_completed(ResultProcessorKind.DETERMINISTIC_CONTINUATION),
        result={"action_status": "completed"},
        plugin_returned_actions=(),
        flow_token_id="tok-terminal",
    )
    _check(
        outcome is DispatchOutcome.DETERMINISTIC_PLAN_COMPLETE,
        f"outcome == DETERMINISTIC_PLAN_COMPLETE (got {outcome})",
    )
    _check(err.calls == [], f"error dispatcher NOT called (got {err.calls})")
    _check(sub.calls == [], f"submitter NOT called (got {sub.calls})")


def _case_empty_plan_still_raises_current_step_missing() -> None:
    print(
        "\nCase 2: empty/malformed plan (genuine corruption) — MUST still "
        "raise current_step_missing — proves is_complete did not widen "
        "into the corruption case",
    )
    inf, err, sub = _Inference(), _Error(), _Submitter()
    coord = _coord(inf=inf, err=err, sub=sub, res=_Resolver(plan=EMPTY_PLAN))
    outcome = coord.handle_successful_result(
        completed=_completed(ResultProcessorKind.DETERMINISTIC_CONTINUATION),
        result={"action_status": "completed"},
        plugin_returned_actions=(),
        flow_token_id="tok-empty",
    )
    _check(
        outcome is DispatchOutcome.CONTRACT_VIOLATION_DISPATCHED,
        f"outcome == CONTRACT_VIOLATION_DISPATCHED (got {outcome})",
    )
    _check(
        bool(err.calls) and err.calls[0][2] == "current_step_missing",
        f"error dispatcher called with current_step_missing (got {err.calls})",
    )
    _check(sub.calls == [], f"submitter NOT called (got {sub.calls})")


def _case_mid_plan_happy_path_still_submits() -> None:
    print(
        "\nCase 3: mid-plan continuation (regression) — unaffected, still "
        "submits the next step",
    )
    plan = parse(MID_PLAN_WBS)
    _check(not plan.is_complete, "fixture sanity: mid-plan reports is_complete=False")

    inf, err, sub = _Inference(), _Error(), _Submitter()
    coord = _coord(
        inf=inf, err=err, sub=sub,
        res=_Resolver(
            plan=plan,
            required_args={GRAFT_KEY: frozenset({"wbs_id", "anchor_step_number"})},
        ),
    )
    outcome = coord.handle_successful_result(
        completed=_completed(ResultProcessorKind.DETERMINISTIC_CONTINUATION),
        result={"action_status": "completed"},
        plugin_returned_actions=(),
        flow_token_id="tok-mid",
    )
    _check(
        outcome is DispatchOutcome.DETERMINISTIC_SUBMITTED,
        f"outcome == DETERMINISTIC_SUBMITTED (got {outcome})",
    )
    _check(
        sub.calls == [("submit", "ae-jos07-1", 3, "tok-mid")],
        f"submitter called for step 3 (got {sub.calls})",
    )
    _check(err.calls == [], f"error dispatcher NOT called (got {err.calls})")


def main() -> int:
    print("Smoke: deterministic-continuation terminal-plan-complete (JOS-07)")
    _case_terminal_complete_plan_does_not_violate()
    _case_empty_plan_still_raises_current_step_missing()
    _case_mid_plan_happy_path_still_submits()

    print(f"\n{_passed} passed, {len(_failed)} failed")
    if _failed:
        for label in _failed:
            print(f"  - {label}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
