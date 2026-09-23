#!/usr/bin/env python3
"""Regression guard for rejecting undeclared process-call arguments offline.

Exercises the real ActionFactory submission boundary and the real bridge wrapper
for one service_interface verb and one plugin verb.  The action processor uses
the same validator before dispatching service-interface actions, preventing its
former filter from silently dropping a bypassed queued argument.
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path
from types import SimpleNamespace

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT / "ananta" / "src"))
sys.path.insert(0, str(REPO_ROOT / "plugins" / "agent_messaging_plugin" / "src"))

from ananta.core.actions.action_factory import (  # noqa: E402
    UNKNOWN_ARGUMENTS_ERROR_CODE,
    ActionFactory,
)
from ananta.core.actions.action_processor import ActionProcessor  # noqa: E402
from ananta.error_handling import FrameworkError  # noqa: E402

from agent_messaging_plugin.models import BridgeSessionState  # noqa: E402
from agent_messaging_plugin.platform_surface import (  # noqa: E402
    BridgeError,
    PlatformSurface,
)
from agent_messaging_plugin.process_exposure import ProcessExportPolicy  # noqa: E402

_SERVICE_KEY = "service_interface::knowledge_service::search"
_PLUGIN_KEY = "plugin::agent_messaging_plugin::peer_identity"
_FLOW_ID = "flow-unknown-arguments-smoke"
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


class _TemplateEngine:
    def resolve_templates(
        self, action_def: dict[str, object], _context: dict[str, object],
    ) -> dict[str, object]:
        return action_def


class _StateService:
    def generate_unique_string(self, length: int, encoding: str) -> dict[str, object]:
        return {
            "action_status": "completed",
            "data": {"random_string": "smoke"[:length]},
        }


class _Recorder:
    def __init__(self) -> None:
        self.actions: list[dict[str, object]] = []

    def store_action_event(self, action: dict[str, object]) -> str:
        self.actions.append(action)
        return f"ae-smoke-{len(self.actions)}"


class _FlowManager:
    def __init__(self) -> None:
        self.failed: list[tuple[str, str]] = []

    def create_flow(self, **_kwargs: object) -> str:
        return _FLOW_ID

    def update_flow_status(self, flow_id: str, status: str) -> None:
        self.failed.append((flow_id, status))


class _CompilationContextBuilder:
    def build_context(self, **_kwargs: object) -> dict[str, object]:
        return {}


class _BridgeManager:
    def __init__(self, bridge: BridgeSessionState) -> None:
        self._bridge = bridge

    def get(self, bridge_id: str) -> BridgeSessionState | None:
        return self._bridge if bridge_id == self._bridge.bridge_id else None


def _registry() -> dict[str, object]:
    return {
        "processes": {
            _SERVICE_KEY: {
                "parameters": {
                    "query": {"required": True},
                    "top_k": {"required": False},
                },
            },
            _PLUGIN_KEY: {"parameters": {}},
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


def _definition(process_key: str, arguments: dict[str, object]) -> dict[str, object]:
    return {
        "process_key": process_key,
        "arguments": arguments,
        "flow_id": _FLOW_ID,
        "result_processor_kind": None,
        "error_processor_kind": None,
    }


def _surface(factory: ActionFactory) -> tuple[PlatformSurface, _FlowManager]:
    bridge = BridgeSessionState(bridge_id="agc-unknown-arguments", session_id="sess-smoke")
    flows = _FlowManager()
    return (
        PlatformSurface(
            action_factory=factory,
            flow_manager=flows,
            compilation_context_builder=_CompilationContextBuilder(),
            bridge_manager=_BridgeManager(bridge),
            export_policy=ProcessExportPolicy(
                allow_patterns=("service_interface::*", "plugin::*"),
            ),
        ),
        flows,
    )


def _assert_core_unknown(process_key: str, arguments: dict[str, object]) -> None:
    factory, _recorder = _factory()
    try:
        factory.submit_action_definition(_definition(process_key, arguments))
    except FrameworkError as exc:
        _check(exc.error_code == UNKNOWN_ARGUMENTS_ERROR_CODE, f"{process_key} core code")
        _check("xyzzy" in str(exc), f"{process_key} core error names offending key")
        _check(
            "Declared arguments" in str(exc),
            f"{process_key} core error names declared set",
        )
    else:
        _check(False, f"{process_key} core rejects undeclared argument")


def _assert_bridge_unknown(process_key: str, arguments: dict[str, object]) -> None:
    factory, _recorder = _factory()
    surface, flows = _surface(factory)
    try:
        surface.process_call(
            process_key,
            arguments,
            trigger_data={"bridge_id": "agc-unknown-arguments", "session_id": "sess-smoke"},
            deliver_to_bridge=False,
        )
    except BridgeError as exc:
        _check(exc.code == UNKNOWN_ARGUMENTS_ERROR_CODE, f"{process_key} bridge preserves code")
        _check("xyzzy" in str(exc), f"{process_key} bridge error names offending key")
        _check(
            "Declared arguments" in str(exc),
            f"{process_key} bridge error names declared set",
        )
        _check(flows.failed == [(_FLOW_ID, "failed")], f"{process_key} bridge fails its flow")
    else:
        _check(False, f"{process_key} bridge rejects undeclared argument")


def test_unknown_arguments_refused_on_both_families_and_surfaces() -> None:
    for process_key, arguments in (
        (_SERVICE_KEY, {"query": "needle", "xyzzy": "bad"}),
        (_PLUGIN_KEY, {"xyzzy": "bad"}),
    ):
        _assert_core_unknown(process_key, arguments)
        _assert_bridge_unknown(process_key, arguments)


def test_service_processor_rejects_a_bypassed_undeclared_argument() -> None:
    processor = object.__new__(ActionProcessor)
    action = SimpleNamespace(process_key=_SERVICE_KEY)
    try:
        processor._filter_and_inject_arguments(
            {"query": "needle", "xyzzy": "bad"},
            {"query": {"required": True}},
            action,
        )
    except FrameworkError as exc:
        _check(
            exc.error_code == UNKNOWN_ARGUMENTS_ERROR_CODE,
            "service processor rejects a bypassed undeclared argument with the action code",
        )
    else:
        _check(False, "service processor never silently drops a bypassed undeclared argument")


def test_declared_arguments_still_pass_and_missing_required_keeps_its_code() -> None:
    factory, recorder = _factory()
    factory.submit_action_definition(_definition(_SERVICE_KEY, {"query": "needle", "top_k": 1}))
    factory.submit_action_definition(_definition(_PLUGIN_KEY, {}))
    _check(len(recorder.actions) == 2, "declared service and plugin arguments still submit")

    try:
        factory.submit_action_definition(_definition(_SERVICE_KEY, {"top_k": 1}))
    except FrameworkError as exc:
        _check(
            exc.error_code == "action.missing_required_arguments",
            "missing required argument retains its existing core code",
        )
    else:
        _check(False, "missing required argument is refused at core boundary")

    surface, _flows = _surface(factory)
    try:
        surface.process_call(
            _SERVICE_KEY,
            {"top_k": 1},
            trigger_data={"bridge_id": "agc-unknown-arguments", "session_id": "ags-smoke"},
            deliver_to_bridge=False,
        )
    except BridgeError as exc:
        _check(
            exc.code == "bridge.process_call_failed",
            "missing required argument retains its existing bridge code",
        )
    else:
        _check(False, "missing required argument is refused at bridge boundary")


def main() -> int:
    logging.getLogger("agent_messaging_plugin.platform_surface").setLevel(logging.CRITICAL)
    print("process-call undeclared argument refusal smoke")
    test_unknown_arguments_refused_on_both_families_and_surfaces()
    test_service_processor_rejects_a_bypassed_undeclared_argument()
    test_declared_arguments_still_pass_and_missing_required_keeps_its_code()
    if _failed:
        print(f"\nFAIL: {len(_failed)} check(s) failed")
        return 1
    print(f"\nPASS: {_passed} checks")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
