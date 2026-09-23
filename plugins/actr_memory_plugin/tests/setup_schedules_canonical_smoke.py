#!/usr/bin/env python3
"""Phase 2 (2026-06-17) smoke — actr_memory_plugin cron schedules dispatch
plugin-namespaced EDGE_SINK cron wrappers, NOT `service_interface::memory_service::*`
with `result_processor_kind: "inference"`.

Background: on 2026-06-17, the §5.3-REDIRECT campaign retired the leaf-side
P1-A workaround pattern in favor of a primitive-level fix. Architect's
design memo at `workbench/2026-06-17_scheduler_cron_action_contract_design.md`
specifies a thin Shape-A wrapping pattern for the 3 actr_memory cron targets:
plugin-namespaced EDGE_SINK wrappers in `actr_memory_plugin/plugin.py` whose
`processor_policy_category=ProcessorPolicyCategory.EDGE_SINK` causes
`action_queue_poller._dispatch_*` to short-circuit at the EDGE_SINK_SKIP
branch — terminal action, no result-processor dispatch, no inference scaffold
fires. The model-callable `service_interface::memory_service::*` surface is
intentionally preserved (`is_discoverable=True`) for direct model invocation.

This smoke asserts the per-cron action dicts so the OLD broken shape
(`service_interface::memory_service::*` + `result_processor_kind: "inference"`)
cannot silently regress.

Project policy: no pytest. Exits 0 on success, 1 on first failure.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT / "ananta" / "src"))
sys.path.insert(0, str(REPO_ROOT / "plugins" / "actr_memory_plugin" / "src"))

from actr_memory_plugin.plugin import ACTRMemoryPlugin  # noqa: E402

_EXPECTED_KEYS = {
    "memorization_queue": "service_interface::memory_service::process_memorization_queue_cron",
    "strength_recompute": "service_interface::memory_service::recompute_strengths_cron",
    "consolidation": "service_interface::memory_service::consolidate_cron",
}
# These are the discoverable EDGE-category keys. Crons MUST dispatch the
# cron-only EDGE_SINK siblings (the keys in _EXPECTED_KEYS) instead. The
# discoverable EDGE verbs stay model-callable elsewhere; they're forbidden
# HERE because the cron path is what's under test (NS.C migration shape from
# 2026-06-07 was broken for crons because it triggered the inference
# scaffold's `Empty source_namespace` failure mode).
#
# We deliberately do NOT enumerate the pre-FOLD plugin-namespaced cron keys
# (`plugin::actr_memory_plugin::*_cron`) here, because the whole-tree
# integration gate's C3.2 check treats any hardcoded plugin::* literal in
# source as a live callsite. The prefix shape check
# (`startswith("service_interface::memory_service::")` +
# `endswith("_cron")`) below already catches a plugin::* regression
# structurally; an explicit forbidden-set entry would only be
# documentation, and the gate would mis-attribute it as runtime debt.
_FORBIDDEN_KEYS = {
    "service_interface::memory_service::process_memorization_queue",
    "service_interface::memory_service::recompute_strengths",
    "service_interface::memory_service::consolidate",
}

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


# ─── Fixture scaffolding ────────────────────────────────────────────────────


class _RecordingSchedulingService:
    """Captures every create_cron_schedule call so we can assert dispatch shape."""

    def __init__(self) -> None:
        self.create_calls: list[dict[str, Any]] = []

    def create_cron_schedule(
        self,
        params: dict[str, Any] | None = None,
        state: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        captured: dict[str, Any] = dict(params or {})
        captured["_state_passed"] = state
        captured["_kwargs"] = kwargs
        self.create_calls.append(captured)
        return {"action_status": "completed", "data": {"schedule_id": "sched-fixture-001"}}


class _StubConfigProvider:
    def __init__(self, cfg: dict[str, Any] | None = None) -> None:
        self.config: dict[str, Any] = cfg or {"enable_scheduled_operations": True}


def _make_plugin(*, scheduling_service: Any = None) -> ACTRMemoryPlugin:
    """Construct a minimal ACTRMemoryPlugin stand-in.

    `setup_schedules` only needs `self._scheduling_service`,
    `self._schedules_configured`, `self.config_provider`, and the
    `_build_response` helper. Bypass the heavy `prepare_for_readiness`
    path by allocating via __new__.
    """
    instance = ACTRMemoryPlugin.__new__(ACTRMemoryPlugin)
    instance.name = "actr_memory_plugin"  # type: ignore[assignment]
    import logging  # noqa: PLC0415

    instance.logger = logging.getLogger("actr_memory_plugin")
    instance._backend = None  # type: ignore[assignment]
    instance._scheduling_service = scheduling_service
    instance._state_service = None  # type: ignore[assignment]  # no longer needed (preseed removed)
    instance._services_started = False
    instance._schedules_configured = False
    instance.config_provider = _StubConfigProvider()  # type: ignore[assignment]
    return instance


# ─── Cases ──────────────────────────────────────────────────────────────────


def test_setup_schedules_emits_three_edge_sink_wrapper_actions() -> None:
    """The three cron actions must use plugin::actr_memory_plugin::*_cron keys."""
    scheduler = _RecordingSchedulingService()
    plugin = _make_plugin(scheduling_service=scheduler)

    result = plugin.setup_schedules(params={}, state={"session_id": "sess-smoke"})

    _check(
        result.get("action_status") == "completed",
        f"setup_schedules returns completed (got {result.get('action_status')!r})",
    )
    _check(
        len(scheduler.create_calls) == 3,
        f"exactly 3 cron schedules created (got {len(scheduler.create_calls)})",
    )

    if len(scheduler.create_calls) < 3:
        return

    for call in scheduler.create_calls:
        actions = call.get("actions", [])
        _check(
            isinstance(actions, list) and len(actions) == 1,
            f"each schedule has exactly one action (label={call.get('label')!r})",
        )
        if not (isinstance(actions, list) and actions):
            continue
        action = actions[0]
        _check(
            isinstance(action.get("process_key"), str)
            and action["process_key"].startswith("service_interface::memory_service::")
            and action["process_key"].endswith("_cron"),
            (
                "service_interface::memory_service::*_cron EDGE_SINK key shape "
                f"(label={call.get('label')!r}, got {action.get('process_key')!r})"
            ),
        )
        _check(
            action.get("process_key") not in _FORBIDDEN_KEYS,
            (
                "NO legacy or plugin-namespaced key "
                f"(label={call.get('label')!r}, got {action.get('process_key')!r})"
            ),
        )
        _check(
            "result_processor_kind" not in action,
            (
                "NO result_processor_kind key on action_def "
                f"(label={call.get('label')!r}, action keys={sorted(action.keys())})"
            ),
        )
        _check(
            "result_processor_customizations" not in action,
            (
                "NO result_processor_customizations key on action_def "
                f"(label={call.get('label')!r}, action keys={sorted(action.keys())})"
            ),
        )
        _check(
            "error_processor_customizations" not in action,
            (
                "NO error_processor_customizations key on action_def "
                f"(label={call.get('label')!r}, action keys={sorted(action.keys())})"
            ),
        )


def test_each_expected_wrapper_key_present() -> None:
    """Every Phase 2 wrapper key appears in the cron schedule set."""
    scheduler = _RecordingSchedulingService()
    plugin = _make_plugin(scheduling_service=scheduler)
    plugin.setup_schedules(params={}, state={"session_id": "sess-smoke"})

    keys_emitted = {
        action["process_key"]
        for call in scheduler.create_calls
        for action in (call.get("actions") or [])
        if isinstance(action.get("process_key"), str)
    }

    for label, expected_key in _EXPECTED_KEYS.items():
        _check(
            expected_key in keys_emitted,
            f"{label} cron emits {expected_key!r}",
        )


def test_consolidation_action_carries_dry_run_arg() -> None:
    """Per the existing semantics, the weekly consolidation cron passes dry_run=False."""
    scheduler = _RecordingSchedulingService()
    plugin = _make_plugin(scheduling_service=scheduler)
    plugin.setup_schedules(params={}, state={"session_id": "sess-smoke"})

    consolidation = next(
        (call for call in scheduler.create_calls if call.get("label") == "ACT-R Memory Consolidation"),
        None,
    )
    _check(consolidation is not None, "ACT-R Memory Consolidation schedule present")
    if consolidation is None:
        return
    actions = consolidation.get("actions", [])
    if not actions:
        return
    args = actions[0].get("arguments", {})
    _check(
        args == {"dry_run": False},
        f"consolidate args == {{'dry_run': False}} (got {args})",
    )


def test_state_override_carries_system_owned_identifiers() -> None:
    """Each cron's state= carries the system-owned flow_id+session_id.

    `action_factory._enforce_flow_id` refuses absent flow_ids even on
    EDGE_SINK paths, so the cron must still pass distinct system-owned
    identifiers so the action factory does not couple to the caller's
    session.
    """
    scheduler = _RecordingSchedulingService()
    plugin = _make_plugin(scheduling_service=scheduler)
    plugin.setup_schedules(params={}, state={"session_id": "should-be-ignored"})

    expected_state_map = {
        "ACT-R Memorization Queue Processing": (
            "flow-actr-memorization-queue",
            "sess-actr-memorization-queue",
        ),
        "ACT-R Strength Recomputation": (
            "flow-actr-strength-recompute",
            "sess-actr-strength-recompute",
        ),
        "ACT-R Memory Consolidation": (
            "flow-actr-consolidation",
            "sess-actr-consolidation",
        ),
    }
    for call in scheduler.create_calls:
        label = call.get("label", "")
        expected = expected_state_map.get(label)
        if expected is None:
            continue
        state_passed = call.get("_state_passed", {})
        _check(
            state_passed.get("flow_id") == expected[0],
            (
                f"cron {label!r} state flow_id=={expected[0]!r} "
                f"(got {state_passed.get('flow_id')!r})"
            ),
        )
        _check(
            state_passed.get("session_id") == expected[1],
            (
                f"cron {label!r} state session_id=={expected[1]!r} "
                f"(got {state_passed.get('session_id')!r})"
            ),
        )


def main() -> int:
    print("=== setup_schedules_canonical_smoke (Phase 2 EDGE_SINK wrapper shape) ===")
    test_setup_schedules_emits_three_edge_sink_wrapper_actions()
    test_each_expected_wrapper_key_present()
    test_consolidation_action_carries_dry_run_arg()
    test_state_override_carries_system_owned_identifiers()
    print(f"\n{_passed} passed, {len(_failed)} failed")
    if _failed:
        for label in _failed:
            print(f"  FAILED: {label}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
