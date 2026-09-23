#!/usr/bin/env python3
"""Guard scheduler context from becoming caller arguments.

Runs each ledger cron verb through the real scheduler submission sequence:
``execute_scheduled_actions`` -> ``_execute_single_action`` ->
``_build_action_definition`` -> ``_apply_context_to_definition`` ->
``_submit_action`` -> ``ActionFactory.submit_action_definition``.  The real
factory is deliberate: a recording substitute would miss the validator that
refused the production scheduler's context fields.

Killing mutation: restore ActionExecutor's former writes of ``session_id`` and
``flow_id`` into ``arguments``.  All three scheduler-tick cases fail with
``action.unknown_arguments``.  Separately, the old emitted shape is submitted
directly to the real factory and must be refused, proving the green tick did
not merely bypass validation.
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT / "ananta" / "src"))
sys.path.insert(0, str(REPO_ROOT / "plugins" / "default_scheduling_plugin" / "src"))

from ananta.core.actions.action_factory import (  # noqa: E402
    UNKNOWN_ARGUMENTS_ERROR_CODE,
    ActionFactory,
)
from ananta.error_handling import FrameworkError  # noqa: E402
from default_scheduling_plugin.execution.action_executor import ActionExecutor  # noqa: E402
from default_scheduling_plugin.models import ActionData, ScheduleData  # noqa: E402

_FLOW_ID = "flow-ledger-scheduler-context-smoke"
_SESSION_ID = "sess-ledger-scheduler-context-smoke"
_KEYS = (
    "service_interface::session_ledger_service::trigger_poll",
    "service_interface::session_ledger_service::summarize_quiescent_sessions",
    "service_interface::session_ledger_service::drain_event_embeddings",
)
_PASSED = 0
_FAILED: list[str] = []


def _check(condition: object, label: str) -> None:
    global _PASSED
    if condition:
        _PASSED += 1
        print(f"  PASS  {label}")
    else:
        _FAILED.append(label)
        print(f"  FAIL  {label}")


class _TemplateEngine:
    def resolve_templates(
        self, action_definition: dict[str, object], _context: dict[str, object]
    ) -> dict[str, object]:
        return action_definition


class _StateService:
    def generate_unique_string(self, length: int, encoding: str) -> dict[str, object]:
        return {"action_status": "completed", "data": {"random_string": "smoke"[:length]}}


class _Recorder:
    def __init__(self) -> None:
        self.actions: list[dict[str, object]] = []

    def store_action_event(self, action: dict[str, object]) -> str:
        self.actions.append(action)
        return f"ae-scheduler-context-{len(self.actions)}"


def _registry() -> dict[str, object]:
    return {
        "processes": {
            _KEYS[0]: {"parameters": {}},
            _KEYS[1]: {
                "parameters": {
                    "quiescence_minutes": {"required": False},
                    "batch_size": {"required": False},
                },
            },
            _KEYS[2]: {"parameters": {"page_size": {"required": False}}},
        },
    }


def _factory() -> tuple[ActionFactory, _Recorder]:
    recorder = _Recorder()
    return (
        ActionFactory(
            process_registry=_registry(),
            template_engine=_TemplateEngine(),
            state_service=_StateService(),
            action_event_recorder=recorder,
        ),
        recorder,
    )


def _schedule(process_key: str) -> ScheduleData:
    return ScheduleData(
        id=f"sch-{process_key.rsplit('::', maxsplit=1)[-1]}",
        label="ledger scheduler context smoke",
        tags=["smoke"],
        type="recurring",
        actions=[ActionData(name=process_key, parameters={})],
        cron_expression="*/10 * * * *",
        session_id=_SESSION_ID,
        flow_id=_FLOW_ID,
    )


def _old_definition(process_key: str) -> dict[str, object]:
    return {
        "process_key": process_key,
        "arguments": {"session_id": _SESSION_ID, "flow_id": _FLOW_ID},
        "session_id": _SESSION_ID,
        "flow_id": _FLOW_ID,
        "result_processor_kind": None,
        "error_processor_kind": None,
    }


def test_real_scheduler_ticks_submit_without_context_arguments() -> None:
    for process_key in _KEYS:
        factory, recorder = _factory()
        success, error = ActionExecutor(factory).execute_scheduled_actions(_schedule(process_key))
        _check(success, f"{process_key} scheduler tick passes through real ActionFactory")
        _check(error is None, f"{process_key} scheduler tick has no submission error")
        _check(len(recorder.actions) == 1, f"{process_key} scheduler tick stores one action")
        stored = recorder.actions[0] if recorder.actions else {}
        _check(
            stored.get("session_id") == _SESSION_ID and stored.get("flow_id") == _FLOW_ID,
            f"{process_key} keeps scheduler context as top-level action metadata",
        )
        _check(
            stored.get("parameters") == {},
            f"{process_key} never turns scheduler context into process arguments",
        )


def test_old_scheduler_arguments_are_refused_by_real_validator() -> None:
    for process_key in _KEYS:
        factory, recorder = _factory()
        try:
            factory.submit_action_definition(_old_definition(process_key))
        except FrameworkError as exc:
            _check(
                exc.error_code == UNKNOWN_ARGUMENTS_ERROR_CODE,
                f"{process_key} old context-in-arguments shape has action-level refusal",
            )
            _check(
                "session_id" in str(exc) and "flow_id" in str(exc),
                f"{process_key} refusal names both old undeclared context fields",
            )
        else:
            _check(False, f"{process_key} old context-in-arguments shape is refused")
        _check(not recorder.actions, f"{process_key} refused old shape stores no action")


test_real_scheduler_ticks_submit_without_context_arguments()
test_old_scheduler_arguments_are_refused_by_real_validator()

print()
print(f"Passed: {_PASSED}")
print(f"Failed: {len(_FAILED)}")
if _FAILED:
    for failure in _FAILED:
        print(f"  - {failure}")
    sys.exit(1)
