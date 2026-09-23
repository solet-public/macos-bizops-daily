#!/usr/bin/env python3
"""Phase 1 smoke — cron action_def contract validator.

Background: 2026-06-17 design memo (`workbench/2026-06-17_scheduler_cron_action_contract_design.md`)
locks the primitive-level fix for the ~78 errors/10min `Empty source_namespace in
flow trigger_data` failure pattern fired by cron-driven actr_memory verbs. The
fix wires a validator at the cron registration boundary (and at the recurring +
one-time restoration paths) that rejects action_defs declaring
`result_processor_kind == "inference"`, since that kind requires session
context that cron-fired actions cannot provide. The policy set is scoped to
`{"inference"}` only per design memo §3 ("Today only 'inference' qualifies
because `_resolve_io_process_key` is the only path that requires
source_namespace"); other kinds like `bridge_delivery` route through
structurally distinct paths and are not in the policy set today.

This smoke positively asserts the validator's contract per §6 of the design memo
plus the §10.1 round-2 fold for the `_restore_one_time_schedule` path.

Cases:
  A. validator rejects `result_processor_kind="inference"` (BUG-ACTIVE shape)
  B. validator accepts EDGE_SINK shape (both kind and processor None)
  C. validator passes half-bandaged state (customizations present, kind None)
     per Q3 permissive resolution — proves non-over-rejection
  D. validator accepts `deterministic_continuation` kind — proves policy is
     bounded to session-context-required kinds
  E. validator accepts `bridge_delivery` kind — proves policy is scoped per
     design memo §3 ("Today only 'inference' qualifies"); revisit if
     bridge_delivery in a cron path is empirically shown to require session
     context
  F. `validate_persisted_cron_action_def` rejects dict shape with bad kind
     (restoration-path coverage)
  G. `validate_persisted_cron_action_def` accepts dict shape with omitted kind
     (restoration-path canonical EDGE_SINK shape)
  H. `validate_persisted_cron_action_def` accepts dict shape with deterministic
     kind (parallel to D, restoration path)
  I. `SESSION_CONTEXT_REQUIRED_PROCESSOR_KINDS` is exactly `frozenset({"inference"})`
     — locks the policy boundary structurally per design memo §3 scope

Project policy: no pytest. Exits 0 on success, 1 on first failure.
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT / "ananta" / "src"))
sys.path.insert(0, str(REPO_ROOT / "plugins" / "default_scheduling_plugin" / "src"))

from default_scheduling_plugin.models import ActionData  # noqa: E402
from default_scheduling_plugin.validation import (  # noqa: E402
    CRON_ACTION_CONTRACT_KB_HINT,
    SESSION_CONTEXT_REQUIRED_PROCESSOR_KINDS,
    validate_cron_action_def,
    validate_persisted_cron_action_def,
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


def _expect_value_error(callable_: object, label: str) -> ValueError | None:
    raised: BaseException | None = None
    try:
        callable_()  # type: ignore[operator]
    except ValueError as exc:
        raised = exc
    except BaseException as exc:  # noqa: BLE001 — smoke wants the actual class
        raised = exc
    _check(isinstance(raised, ValueError), label)
    return raised if isinstance(raised, ValueError) else None


# ----- Case A: rejects inference kind ----------------------------------------
print("Case A: validator REJECTS result_processor_kind='inference'")

inference_action = ActionData(
    name="service_interface::memory_service::consolidate",
    parameters={"strategy": "rolling"},
    result_processor=None,
    result_processor_kind="inference",
)
raised_a = _expect_value_error(
    lambda: validate_cron_action_def(inference_action),
    "validator raises ValueError for inference kind",
)
if raised_a is not None:
    msg = str(raised_a)
    _check(
        "Empty source_namespace" in msg,
        "error message names the runtime failure mode ('Empty source_namespace')",
    )
    _check(
        CRON_ACTION_CONTRACT_KB_HINT in msg,
        "error message names the KB article",
    )
    _check(
        "EDGE_SINK" in msg,
        "error message names the canonical EDGE_SINK fix",
    )
    _check(
        "consolidate" in msg,
        "error message identifies the failing action's process_key",
    )


# ----- Case B: accepts EDGE_SINK shape ---------------------------------------
print("\nCase B: validator ACCEPTS EDGE_SINK shape (kind=None, processor=None)")

edge_sink_action = ActionData(
    name="service_interface::session_ledger_service::trigger_poll",
    parameters={},
    result_processor=None,
    result_processor_kind=None,
)
raised_b: BaseException | None = None
try:
    validate_cron_action_def(edge_sink_action)
except BaseException as exc:  # noqa: BLE001 — should not raise
    raised_b = exc
_check(
    raised_b is None,
    "validator returns cleanly for EDGE_SINK shape",
)


# ----- Case C: half-bandaged state PASSES per Q3 permissive ------------------
print("\nCase C: validator PASSES half-bandaged state (kind=None, customizations set)")

half_bandaged_action = ActionData(
    name="service_interface::memory_service::recall",
    parameters={},
    result_processor={"template": "Summarize <<RESULT>>", "process_key": "x"},
    result_processor_kind=None,
)
raised_c: BaseException | None = None
try:
    validate_cron_action_def(half_bandaged_action)
except BaseException as exc:  # noqa: BLE001 — should not raise
    raised_c = exc
_check(
    raised_c is None,
    "validator does NOT reject when customizations present but kind is None (Q3 permissive)",
)


# ----- Case D: deterministic_continuation kind PASSES ------------------------
print("\nCase D: validator ACCEPTS result_processor_kind='deterministic_continuation'")

deterministic_action = ActionData(
    name="service_interface::memory_service::remember",
    parameters={},
    result_processor=None,
    result_processor_kind="deterministic_continuation",
)
raised_d: BaseException | None = None
try:
    validate_cron_action_def(deterministic_action)
except BaseException as exc:  # noqa: BLE001 — should not raise
    raised_d = exc
_check(
    raised_d is None,
    "validator policy is BOUNDED to session-context-required kinds (no over-rejection)",
)


# ----- Case E: ACCEPTS bridge_delivery kind (NOT in policy set) --------------
print("\nCase E: validator ACCEPTS result_processor_kind='bridge_delivery'")
# `bridge_delivery` is structurally distinct from `inference`: it routes through
# the bridge-delivery EDGE_SINK dispatcher (see ReAct Execution Contract KB),
# not the source_namespace-requiring `_resolve_io_process_key` path. Until
# empirical evidence shows bridge_delivery in a cron path requires session
# context, the validator's policy remains scoped to `inference` only.

bridge_action = ActionData(
    name="plugin::agent_messaging_plugin::deliver_result",
    parameters={},
    result_processor=None,
    result_processor_kind="bridge_delivery",
)
raised_e: BaseException | None = None
try:
    validate_cron_action_def(bridge_action)
except BaseException as exc:  # noqa: BLE001 — should not raise under current scope
    raised_e = exc
_check(
    raised_e is None,
    "validator does NOT reject bridge_delivery kind (out of policy scope today)",
)


# ----- Case F: persisted-dict validator REJECTS bad kind ---------------------
print("\nCase F: validate_persisted_cron_action_def REJECTS dict shape with bad kind")

persisted_bad = {
    "name": "service_interface::memory_service::consolidate",
    "parameters": {},
    "result_processor": None,
    "result_processor_kind": "inference",
}
raised_f = _expect_value_error(
    lambda: validate_persisted_cron_action_def(persisted_bad),
    "persisted-dict validator raises ValueError for inference kind",
)
if raised_f is not None:
    _check(
        "consolidate" in str(raised_f),
        "persisted-dict error message identifies the action name",
    )


# ----- Case G: persisted-dict validator ACCEPTS EDGE_SINK shape --------------
print("\nCase G: validate_persisted_cron_action_def ACCEPTS dict shape with omitted kind")

persisted_edge_sink = {
    "name": "service_interface::session_ledger_service::trigger_poll",
    "parameters": {},
    "result_processor": None,
    "result_processor_kind": None,
}
raised_g: BaseException | None = None
try:
    validate_persisted_cron_action_def(persisted_edge_sink)
except BaseException as exc:  # noqa: BLE001 — should not raise
    raised_g = exc
_check(
    raised_g is None,
    "persisted-dict validator returns cleanly for canonical EDGE_SINK shape",
)


# ----- Case H: persisted-dict validator ACCEPTS deterministic kind -----------
print("\nCase H: validate_persisted_cron_action_def ACCEPTS deterministic kind")

persisted_deterministic = {
    "name": "service_interface::knowledge_service::search",
    "parameters": {},
    "result_processor": None,
    "result_processor_kind": "deterministic_continuation",
}
raised_h: BaseException | None = None
try:
    validate_persisted_cron_action_def(persisted_deterministic)
except BaseException as exc:  # noqa: BLE001 — should not raise
    raised_h = exc
_check(
    raised_h is None,
    "persisted-dict validator policy bounded same as registration-time",
)


# ----- Case I: constant set is exactly {'inference'} -------------------------
print("\nCase I: SESSION_CONTEXT_REQUIRED_PROCESSOR_KINDS contents")

_check(
    SESSION_CONTEXT_REQUIRED_PROCESSOR_KINDS == frozenset({"inference"}),
    "policy set contains exactly {'inference'} per design memo §3 scope",
)


# ----- Summary ---------------------------------------------------------------
print(f"\n{_passed} passed, {len(_failed)} failed")
if _failed:
    for label in _failed:
        print(f"  FAILED: {label}")
    sys.exit(1)
sys.exit(0)
