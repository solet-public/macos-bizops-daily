#!/usr/bin/env python3
"""REL-12 regression smoke: action_factory must not stamp a result_processor
onto a ``result_processor_kind=None`` (terminal / EDGE_SINK / memory-tag
heartbeat) action.

Root cause: ``ActionFactory._validate_action_legacy`` attached a
registry-default ``result_processor`` to ANY non-``bridge_delivery`` action —
including ``kind=None`` cron actions — whenever the target process declared
``result_processor_customizations``. That non-None ``result_processor`` then
broke the poller's ``EDGE_SINK_SKIP`` condition
(``result_processor_kind is None and result_processor is None``,
``action_queue_poller.py``), dropping the action into result-contract
validation where ``contracts.py::_check_result_processor_kind`` raised
``result_processor_kind_missing`` — the platform-wide dead-cron-lane defect.
The trigger processes (``get_memories_by_tag`` heartbeat, ``execute_in_seconds``,
the actr_memory ``*_action`` verbs) all carry ``result_processor_customizations``.

The fix makes ``kind=None`` terminal regardless of registered customizations.
This smoke is RED before that fix (case 1 stamps a result_processor) and GREEN
after; the positive controls (cases 2/3) prove the fix does not disturb the
normal ``inference`` / ``bridge_delivery`` stamping behaviour.

Run:

    .venv/bin/python3 ananta/tests/core/actions/action_factory_edge_sink_skip_smoke.py
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(REPO_ROOT / "ananta" / "src"))

from ananta.core.actions.action_factory import ActionFactory  # noqa: E402

# A cron-dispatched read verb that carries result_processor_customizations —
# the exact REL-12 trigger shape.
_CRON_VERB = "service_interface::memory_service::get_memories_by_tag"
_INFERENCE_BASE = "service_interface::inference_service::process_results"

_failures: list[str] = []


def _check(condition: bool, message: str) -> None:
    status = "PASS" if condition else "FAIL"
    print(f"  [{status}] {message}")
    if not condition:
        _failures.append(message)


def _make_factory() -> ActionFactory:
    """ActionFactory over a minimal-but-real registry.

    The cron verb declares ``result_processor_customizations`` (so
    ``_get_result_processor_from_customizations`` returns a truthy processor),
    and the inference base template exists (its precondition). No helper is
    stubbed — the real ``_validate_action_legacy`` gate is exercised.
    """
    registry: dict[str, object] = {
        "processes": {
            _CRON_VERB: {
                "result_processor_customizations": {
                    "output_action_guidance": "decide what to do with the recalled memory",
                },
            },
            _INFERENCE_BASE: {
                "action_definition_template": {"arguments": {"params": {}}},
            },
        },
    }
    return ActionFactory(process_registry=registry)


def test_kind_none_action_is_left_terminal() -> None:
    """Case 1 (RED-FIRST): kind=None cron action must NOT get a result_processor."""
    factory = _make_factory()
    action_def: dict[str, object] = {
        "process_key": _CRON_VERB,
        "result_processor_kind": None,
    }
    factory._validate_action_legacy(action_def)
    _check(
        "result_processor" not in action_def,
        "kind=None action carries NO result_processor after validation "
        "(EDGE_SINK/heartbeat stays terminal — the REL-12 fix)",
    )
    # The poller's EDGE_SINK_SKIP fires only when BOTH are None; assert it holds.
    _check(
        action_def.get("result_processor_kind") is None
        and action_def.get("result_processor") is None,
        "kind=None action satisfies the poller both-None EDGE_SINK_SKIP condition",
    )


def test_inference_action_still_stamps_result_processor() -> None:
    """Case 2 (positive control): kind=inference MUST still get a result_processor."""
    factory = _make_factory()
    action_def: dict[str, object] = {
        "process_key": _CRON_VERB,
        "result_processor_kind": "inference",
    }
    factory._validate_action_legacy(action_def)
    _check(
        action_def.get("result_processor") is not None,
        "kind=inference action still receives its registry-default result_processor "
        "(fix does not disturb normal stamping)",
    )


def test_bridge_delivery_action_is_not_stamped() -> None:
    """Case 3 (control): kind=bridge_delivery remains un-stamped (unchanged)."""
    factory = _make_factory()
    action_def: dict[str, object] = {
        "process_key": _CRON_VERB,
        "result_processor_kind": "bridge_delivery",
    }
    factory._validate_action_legacy(action_def)
    _check(
        "result_processor" not in action_def,
        "kind=bridge_delivery action carries NO result_processor (bridge owns routing)",
    )


def main() -> int:
    print("REL-12 action_factory EDGE_SINK_SKIP smoke")
    test_kind_none_action_is_left_terminal()
    test_inference_action_still_stamps_result_processor()
    test_bridge_delivery_action_is_not_stamped()
    if _failures:
        print(f"\nFAIL: {len(_failures)} check(s) failed")
        return 1
    print("\nPASS: kind=None actions stay terminal; inference/bridge_delivery unchanged")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
