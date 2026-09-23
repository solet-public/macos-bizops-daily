#!/usr/bin/env python3
"""Shared fixtures for the Phase 0 "freeze current contracts" smokes (no pytest).

Phase 0 of the coding-agent substrate transition plan
(``workbench/2026-07-01_claude_coding_agent_substrate_architecture_and_planning_v2.md``,
PART VI) protects the working substrate contracts with tests BEFORE the later
transition phases change the substrate. This module holds item (5) of that
work list — one small NON-neuro-ambient fixture WBS — plus the builders and
recording stubs the four contract smokes share.

The fixture is a generic WBS-state-recording chain (record step state → graft
the next segment), deliberately not an audio / neuro-ambient composition, so
the contract tests read against the general substrate rather than one domain's
plan shape. Every process key is a REAL registered ``service_interface::
thinking_service`` verb (the whole-tree integration gate C3.1 rejects call-site
references to non-existent keys, tests included), and none is a companion
(``::upsert_plan`` / ``::post_message``) or an excluded shape, so all are valid
deterministic-continuation keys.

Everything here is offline: no live solet, no LM Studio, no Postgres. Plans flow
through the real ``core.plans.parser`` and the validators exercised by the
smokes are the real production functions; only the DB / service collaborators
are recording stubs.
"""

from __future__ import annotations

import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

_REPO_ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(_REPO_ROOT / "ananta" / "src"))

from ananta.core.result_processing.contracts import (  # noqa: E402
    ArgumentSource,
    DeterministicContinuationInput,
    ValidatedDeterministicContinuation,
)
from ananta.core.result_processing.coordinator import CompletedAction  # noqa: E402
from ananta.core.result_processing.enums import ResultProcessorKind  # noqa: E402

# ---------------------------------------------------------------------------
# Process keys used by the fixture WBS — REAL registered thinking_service verbs
# ---------------------------------------------------------------------------

OUTLINE_KEY = (
    "service_interface::thinking_service::register_authored_work_breakdown_structure"
)
RECORD_STEP_KEY = (
    "service_interface::thinking_service::record_work_breakdown_structure_step_state"
)
GRAFT_KEY = "service_interface::thinking_service::graft_work_breakdown_structure_segment"
RECORD_PHASE_KEY = "service_interface::thinking_service::record_work_manifest_phase_state"

_ACTIVE_WBS_HEADER = (
    "ACTIVE_WBS: phase0-contract-fixture\n"
    "WORK_ITEM: Record WBS state across a deterministic two-step chain\n\n"
)


# ---------------------------------------------------------------------------
# Fixture WBS variants
# ---------------------------------------------------------------------------

# Canonical valid WBS. Step 2 is the active ``[>]`` step (deterministic
# continuation), step 3 is a single-action deterministic next step. Used as the
# happy path for the deterministic-continuation and RPK smokes.
CONTRACT_WBS = _ACTIVE_WBS_HEADER + (
    "[X] 1. Outline the work breakdown structure\n"
    "    RESULT_PROCESSOR_KIND: deterministic_continuation\n"
    f"    a) Register authored WBS ({OUTLINE_KEY})\n"
    "        Arguments:\n"
    '        {"wbs_id": "wbs-fixture-001"}\n'
    "[>] 2. Record the current step state\n"
    "    RESULT_PROCESSOR_KIND: deterministic_continuation\n"
    f"    a) Record step state ({RECORD_STEP_KEY})\n"
    "        Arguments:\n"
    '        {"wbs_id": "wbs-fixture-001"}\n'
    "[ ] 3. Graft the next work item\n"
    "    RESULT_PROCESSOR_KIND: deterministic_continuation\n"
    f"    a) Graft next segment ({GRAFT_KEY})\n"
    "        Arguments:\n"
    '        {"wbs_id": "wbs-fixture-001", "anchor_step_number": "2"}\n'
)

# Same header, but step 3 declares TWO continuation actions — a deterministic
# next step is only permitted to advance ONE action.
CONTRACT_WBS_MULTI_ACTION_NEXT = _ACTIVE_WBS_HEADER + (
    "[X] 1. Outline the work breakdown structure\n"
    "    RESULT_PROCESSOR_KIND: deterministic_continuation\n"
    f"    a) Register authored WBS ({OUTLINE_KEY})\n"
    "        Arguments:\n"
    '        {"wbs_id": "wbs-fixture-001"}\n'
    "[>] 2. Record the current step state\n"
    "    RESULT_PROCESSOR_KIND: deterministic_continuation\n"
    f"    a) Record step state ({RECORD_STEP_KEY})\n"
    "        Arguments:\n"
    '        {"wbs_id": "wbs-fixture-001"}\n'
    "[ ] 3. Graft the next item and record the phase\n"
    "    RESULT_PROCESSOR_KIND: deterministic_continuation\n"
    f"    a) Graft next segment ({GRAFT_KEY})\n"
    "        Arguments:\n"
    '        {"wbs_id": "wbs-fixture-001"}\n'
    f"    b) Record phase state ({RECORD_PHASE_KEY})\n"
    "        Arguments:\n"
    '        {"phase": "phase1"}\n'
)

# Same header, but step 3 carries MIN_ACTIONS (a choice step) — choice steps
# require inference and must not be advanced deterministically.
CONTRACT_WBS_MIN_ACTIONS_NEXT = _ACTIVE_WBS_HEADER + (
    "[X] 1. Outline the work breakdown structure\n"
    "    RESULT_PROCESSOR_KIND: deterministic_continuation\n"
    f"    a) Register authored WBS ({OUTLINE_KEY})\n"
    "        Arguments:\n"
    '        {"wbs_id": "wbs-fixture-001"}\n'
    "[>] 2. Record the current step state\n"
    "    RESULT_PROCESSOR_KIND: deterministic_continuation\n"
    f"    a) Record step state ({RECORD_STEP_KEY})\n"
    "        Arguments:\n"
    '        {"wbs_id": "wbs-fixture-001"}\n'
    "[ ] 3. Choose the next planning action\n"
    "    RESULT_PROCESSOR_KIND: deterministic_continuation\n"
    "    MIN_ACTIONS: 2\n"
    f"    a) Graft next segment ({GRAFT_KEY})\n"
    "        Arguments:\n"
    '        {"wbs_id": "wbs-fixture-001"}\n'
)

# Same header, but the active step 2 declares inference — the deterministic
# continuation path must refuse a current step that is not deterministic.
CONTRACT_WBS_CURRENT_INFERENCE = _ACTIVE_WBS_HEADER + (
    "[X] 1. Outline the work breakdown structure\n"
    "    RESULT_PROCESSOR_KIND: deterministic_continuation\n"
    f"    a) Register authored WBS ({OUTLINE_KEY})\n"
    "        Arguments:\n"
    '        {"wbs_id": "wbs-fixture-001"}\n'
    "[>] 2. Record the current step state\n"
    "    RESULT_PROCESSOR_KIND: inference\n"
    f"    a) Record step state ({RECORD_STEP_KEY})\n"
    "        Arguments:\n"
    '        {"wbs_id": "wbs-fixture-001"}\n'
    "[ ] 3. Graft the next work item\n"
    "    RESULT_PROCESSOR_KIND: deterministic_continuation\n"
    f"    a) Graft next segment ({GRAFT_KEY})\n"
    "        Arguments:\n"
    '        {"wbs_id": "wbs-fixture-001", "anchor_step_number": "2"}\n'
)


# ---------------------------------------------------------------------------
# Deterministic-continuation input builder
# ---------------------------------------------------------------------------


def build_continuation_input(
    active_plan: object,
    *,
    required_args: frozenset[str] = frozenset({"wbs_id", "anchor_step_number"}),
) -> DeterministicContinuationInput:
    """Build a ``DeterministicContinuationInput`` for the fixture WBS.

    Completed = step 2 (``record_...step_state``, deterministic); next = step 3
    (``graft_...segment``). ``required_args`` defaults to the two WBS-bound
    arguments of step 3 so every one resolves to a closed-world source (no
    inference). Pass a required arg with no bound / composed / runtime / slot
    source to force the "not mechanically derivable" rejection.
    """
    return DeterministicContinuationInput(
        action_id="ae-record-1",
        completed_process_key=RECORD_STEP_KEY,
        completed_parameters={"wbs_id": "wbs-fixture-001"},
        result_data={"status": "completed"},
        result_processor_kind=ResultProcessorKind.DETERMINISTIC_CONTINUATION,
        session_id="ses-1",
        flow_id="flow-1",
        context_id="ctx-1",
        work_product_run_id=None,
        wbs_id="wbs-fixture-001",
        active_plan=active_plan,  # type: ignore[arg-type]
        focused_wbs=None,
        required_args_by_process={GRAFT_KEY: required_args},
        owned_arg_slots_by_process={},
        allowed_result_field_sources={},
    )


def build_validated_continuation() -> ValidatedDeterministicContinuation:
    """A pre-validated continuation for the deterministic processor smoke.

    Constructed directly (the frozen validation output) so the processor test
    is independent of the validator: it proves the processor submits exactly
    this one action and advances once.
    """
    return ValidatedDeterministicContinuation(
        completed_action_id="ae-record-1",
        completed_process_key=RECORD_STEP_KEY,
        completed_step_number=2,
        next_step_number=3,
        next_action_definition={
            "process_key": GRAFT_KEY,
            "arguments": {"wbs_id": "wbs-fixture-001", "anchor_step_number": "2"},
            "session_id": "ses-1",
            "flow_id": "flow-1",
            "context_id": "ctx-1",
            "result_processor_kind": ResultProcessorKind.DETERMINISTIC_CONTINUATION.value,
        },
        next_argument_sources={
            "wbs_id": ArgumentSource.WBS_BOUND,
            "anchor_step_number": ArgumentSource.WBS_BOUND,
        },
    )


def build_completed_action() -> CompletedAction:
    """The just-completed action snapshot fed to the deterministic processor."""
    return CompletedAction(
        action_id="ae-record-1",
        process_key=RECORD_STEP_KEY,
        parameters={"wbs_id": "wbs-fixture-001"},
        notes=None,
        result_processor=None,
        error_processor=None,
        result_processor_kind=ResultProcessorKind.DETERMINISTIC_CONTINUATION,
        result_processor_target=None,
        session_id="ses-1",
        flow_id="flow-1",
        context_id="ctx-1",
    )


# ---------------------------------------------------------------------------
# Recording stubs (offline collaborators)
# ---------------------------------------------------------------------------


class RecordingStateService:
    """Records ``update_state`` calls; ``query_state`` returns no rows.

    The empty ``query_state`` envelope makes the poller's
    ``_retrieve_action_details`` return ``None`` so ``_mark_action_completed``
    early-returns right after its status-to-completed step — exactly the seam
    the point (1) smoke asserts on.
    """

    def __init__(self) -> None:
        self.update_calls: list[dict[str, Any]] = []
        self.query_calls: list[tuple[str, dict[str, Any]]] = []
        # Ordered interleaving of both call kinds, so a smoke can assert that
        # the status write precedes the result lookup (dispatch before result).
        self.events: list[str] = []

    def update_state(
        self,
        *,
        namespace: str,
        query: dict[str, Any],
        updates: dict[str, Any],
    ) -> dict[str, object]:
        self.update_calls.append(
            {"namespace": namespace, "query": query, "updates": updates},
        )
        self.events.append("update")
        return {"action_status": "completed", "data": {"rows_affected": 1}}

    def query_state(self, namespace: str, query: dict[str, Any]) -> dict[str, object]:
        self.query_calls.append((namespace, query))
        self.events.append("query")
        # A SUCCESSFUL read that found no rows — which is what the docstring
        # above has always claimed this returns. It used to return a bare `{}`,
        # an envelope with no `action_status` at all, i.e. a read that FAILED.
        # Both make `_first_record` return None, so every assertion here behaved
        # identically and the difference stayed invisible — until a reader that
        # distinguishes "no rows" from "could not read" (the poller's
        # double-execution detector) started reporting this fixture as an
        # unreadable row. Empty-but-completed is the faithful shape.
        return {"action_status": "completed", "data": {"records": []}}


class RecordingSubmissionService:
    """Captures the single action the deterministic processor submits."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def submit_action(
        self,
        *,
        action_definition: object,
        parent_action_id: str,
    ) -> str:
        self.calls.append(
            {
                "action_definition": action_definition,
                "parent_action_id": parent_action_id,
            },
        )
        return "ae-next-1"


class RecordingPlanAdvancer:
    """Counts deterministic plan advances (session-keyed per JOS-02)."""

    def __init__(self) -> None:
        self.advance_count = 0
        self.sessions: list[str] = []

    def advance(self, *, session_id: str) -> None:
        self.advance_count += 1
        self.sessions.append(session_id)


def build_offline_poller(state_service: object) -> Any:
    """An ``ActionQueuePoller`` with only ``state_service`` wired.

    ``__init__`` requires the full collaborator graph and fails fast without a
    blob store, but the Phase 0 status / self-completion seams touch only
    ``state_service`` (and ``_async_process_cache``). Bypass ``__init__`` with
    ``object.__new__`` and set exactly what those methods read.

    (The former ``ananta.core.config`` pre-warm here was removed once SUB-04(a)
    root-fixed the ``utils``↔``config_manager`` cycle: ``ananta.utils.filesystem``
    now imports ``ActionStatus``/``ErrorSeverity`` from the leaf
    ``ananta.core.domain.enums`` instead of ``core.plugins.plugin_contracts``,
    so a cold poller import no longer re-enters ``ananta.utils`` mid-init.)
    """
    from ananta.core.actions.action_queue_poller import ActionQueuePoller

    poller: Any = object.__new__(ActionQueuePoller)
    poller.state_service = state_service
    poller._async_process_cache = {}
    return poller


# ---------------------------------------------------------------------------
# Minimal pass/fail accumulator shared by the Phase 0 smokes
# ---------------------------------------------------------------------------


class Checker:
    """Standalone pass/fail accumulator (project policy: no pytest)."""

    def __init__(self, title: str) -> None:
        self.title = title
        self.passed = 0
        self.failed: list[str] = []

    def check(self, condition: object, label: str) -> None:
        if condition:
            self.passed += 1
            print(f"  PASS  {label}")
        else:
            self.failed.append(label)
            print(f"  FAIL  {label}")

    def expect_raises(
        self,
        exc_type: type[BaseException],
        label: str,
        fn: Callable[[], object],
    ) -> None:
        """Assert ``fn()`` raises ``exc_type`` (and record the outcome)."""
        try:
            fn()
        except exc_type:
            self.check(True, label)
        except BaseException as unexpected:  # noqa: BLE001 (record, don't crash the run)
            self.check(
                False,
                f"{label} [raised {type(unexpected).__name__}: {unexpected}]",
            )
        else:
            self.check(False, f"{label} [did not raise {exc_type.__name__}]")

    def summary(self) -> int:
        print(f"\n{self.passed} passed, {len(self.failed)} failed")
        for label in self.failed:
            print(f"  FAILED: {label}")
        return 1 if self.failed else 0
