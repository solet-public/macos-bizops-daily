#!/usr/bin/env python3
"""Focused scheduler action-definition configuration contract smoke.

Proves the discoverable service-interface surface, service-to-plugin forwarding,
plugin registration behavior, mutually exclusive scheduling modes, terminal
memory-tag behavior, and the scheduled peer-notification documentation fixture.

Project policy: no pytest. Exits 0 on success, 1 after reporting all failures.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT / "ananta" / "src"))
sys.path.insert(
    0,
    str(REPO_ROOT / "plugins" / "default_scheduling_plugin" / "src"),
)

from ananta.services.scheduling_service import SchedulingService  # noqa: E402
from ananta.services.scheduling_service.interfaces.public import (  # noqa: E402
    SchedulingServiceAPI,
)
from default_scheduling_plugin.factories.schedule_factory import (  # noqa: E402
    ScheduleFactory,
)
from default_scheduling_plugin.plugin import SchedulingPlugin  # noqa: E402

_passed = 0
_failed: list[str] = []

_PEER_ACTION = {
    "process_key": "plugin::agent_messaging_plugin::peer_send_by_name",
    "arguments": {
        "name": "Coordinator",
        "content": "Scheduled deterministic notification",
    },
}
_JOSEKI_ACTION = {
    "process_key": "service_interface::thinking_service::run_joseki",
    "arguments": {
        "joseki_key": "run_platform_quality_gates",
        "bindings": {},
        "label": "Scheduled platform quality sweep",
    },
}
_PROCESSOR_ACTION = {
    **_PEER_ACTION,
    "result_processor": {"mode": "record-contract-result"},
    "result_processor_kind": "deterministic_continuation",
}
_PLUGIN_METHOD_BASES: dict[str, dict[str, Any]] = {
    "create_cron_schedule": {"cron_expression": "*/15 * * * *"},
    "execute_in_seconds": {"seconds": 60},
}


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
    except BaseException as exc:  # noqa: BLE001 - smoke records actual class
        raised = exc
    _check(isinstance(raised, ValueError), label)
    return raised if isinstance(raised, ValueError) else None


class _CapturingSchedulingPlugin:
    """Minimal ready plugin double for service forwarding evidence."""

    def __init__(self) -> None:
        self.method: str | None = None
        self.params: dict[str, Any] | None = None
        self.state: dict[str, Any] | None = None

    def is_ready(self) -> bool:
        return True

    def create_cron_schedule(
        self,
        params: dict[str, Any],
        state: dict[str, Any],
    ) -> dict[str, Any]:
        self.method = "create_cron_schedule"
        self.params = params
        self.state = state
        return {"action_status": "completed", "data": {"schedule_id": "captured"}}

    def execute_in_seconds(
        self,
        params: dict[str, Any],
        state: dict[str, Any],
    ) -> dict[str, Any]:
        self.method = "execute_in_seconds"
        self.params = params
        self.state = state
        return {"action_status": "completed", "data": {"schedule_id": "captured"}}


class _CapturingMemoryService:
    """Record delayed one-step memory writes without external persistence."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def remember(
        self,
        *,
        content: str,
        tags: list[str],
        session_id: str | None,
    ) -> dict[str, Any]:
        self.calls.append(
            {"content": content, "tags": tags, "session_id": session_id}
        )
        return {"success": True}


def _call_plugin(
    method_name: str,
    action_params: dict[str, Any],
    *,
    memory_service: _CapturingMemoryService | None = None,
) -> tuple[SchedulingPlugin, dict[str, Any]]:
    plugin = SchedulingPlugin()
    plugin.logger.disabled = True
    if memory_service is not None:
        plugin._memory_service = memory_service  # noqa: SLF001
    params = {**_PLUGIN_METHOD_BASES[method_name], **action_params}
    method = getattr(plugin, method_name)
    result = method(
        params,
        {"session_id": "session-contract", "flow_id": "flow-contract"},
    )
    return plugin, result


def _is_parameter_error(result: dict[str, Any]) -> bool:
    error = result.get("error")
    return (
        isinstance(error, dict)
        and error.get("code") == "default_scheduling_plugin.parameter_error"
    )


print("Case A: discoverable service-interface schema")
service_metadata = SchedulingServiceAPI.create_cron_schedule._service_interface_metadata  # type: ignore[attr-defined]  # noqa: SLF001
service_params = service_metadata.parameters
_check(service_metadata.is_discoverable, "create_cron_schedule remains discoverable")
_check(
    "action_definitions" in service_params,
    "schema exposes canonical action_definitions",
)
if "action_definitions" in service_params:
    _check(
        service_params["action_definitions"].required is False,
        "action_definitions is optional because memory_tag is the alternate mode",
    )
_check(
    service_params["memory_tag"].required is False,
    "memory_tag is not required when action_definitions are supplied",
)
_check(
    "model decides" not in service_params["memory_tag"].description.lower(),
    "service memory_tag description does not claim model decision",
)
_check(
    "terminal" in service_params["memory_tag"].description.lower()
    and "does not start a model turn"
    in service_params["memory_tag"].description.lower(),
    "service memory_tag description states terminal no-model-turn behavior",
)


print("\nCase B: service forwards canonical action definitions unchanged")
capturing_plugin = _CapturingSchedulingPlugin()
service = SchedulingService.__new__(SchedulingService)
service._plugin = capturing_plugin  # type: ignore[assignment]  # noqa: SLF001
service_result: dict[str, Any] | None = None
service_error: BaseException | None = None
try:
    service_result = service.create_cron_schedule(
        cron_expression="*/15 * * * *",
        action_definitions=[_PEER_ACTION],
        label="Scheduled peer notification",
        tags=["scheduler-contract"],
        state={
            "current_session_id": "session-contract",
            "flow_id": "flow-contract",
        },
    )
except BaseException as exc:  # noqa: BLE001 - red-first smoke records defect
    service_error = exc
_check(
    service_error is None
    and service_result is not None
    and service_result["action_status"] == "completed",
    "service accepts action_definitions keyword",
)
_check(
    capturing_plugin.params is not None
    and capturing_plugin.params.get("action_definitions") == [_PEER_ACTION],
    "service forwards {process_key, arguments} bytes unchanged",
)
_check(
    capturing_plugin.params is not None and "actions" not in capturing_plugin.params,
    "service does not rewrite canonical action_definitions to the legacy alias",
)
_check(
    capturing_plugin.state is not None
    and capturing_plugin.state.get("session_id") == "session-contract",
    "service preserves normal state normalization",
)

delayed_service_result: dict[str, Any] | None = None
delayed_service_error: BaseException | None = None
try:
    delayed_service_result = service.execute_in_seconds(
        seconds=60,
        action_definitions=[_PEER_ACTION],
        label="Delayed peer notification",
        tags=["scheduler-contract"],
        state={
            "current_session_id": "session-contract",
            "flow_id": "flow-contract",
        },
    )
except BaseException as exc:  # noqa: BLE001 - red-first smoke records defect
    delayed_service_error = exc
_check(
    delayed_service_error is None
    and delayed_service_result is not None
    and delayed_service_result["action_status"] == "completed",
    "delayed service accepts action_definitions keyword",
)
_check(
    capturing_plugin.method == "execute_in_seconds"
    and capturing_plugin.params is not None
    and capturing_plugin.params.get("action_definitions") == [_PEER_ACTION],
    "delayed service forwards {process_key, arguments} bytes unchanged",
)
_check(
    capturing_plugin.params is not None and "actions" not in capturing_plugin.params,
    "delayed service does not rewrite action_definitions to the legacy alias",
)
_check(
    capturing_plugin.state is not None
    and capturing_plugin.state.get("session_id") == "session-contract",
    "delayed service preserves normal state normalization",
)


print("\nCase C: plugin surface accepts and persists the canonical peer action")
plugin = SchedulingPlugin()
plugin_result = plugin.create_cron_schedule(
    {
        "cron_expression": "*/15 * * * *",
        "action_definitions": [_PEER_ACTION],
        "label": "Scheduled peer notification",
    },
    {"session_id": "session-contract", "flow_id": "flow-contract"},
)
_check(
    str(plugin_result.get("action_status", "")).lower() == "completed",
    "plugin create_cron_schedule accepts action_definitions",
)
saved = next(iter(plugin._memory_schedules.values()), None)  # noqa: SLF001
_check(saved is not None and len(saved.actions) == 1, "plugin persists one action")
if saved is not None and saved.actions:
    saved_action = saved.actions[0]
    _check(
        saved_action.name == _PEER_ACTION["process_key"],
        "plugin preserves the scheduled process key",
    )
    _check(
        saved_action.parameters == _PEER_ACTION["arguments"],
        "plugin preserves the scheduled arguments",
    )
    _check(
        saved_action.result_processor is None
        and saved_action.result_processor_kind is None,
        "scheduled peer action remains terminal without inference",
    )

joseki_plugin = SchedulingPlugin()
joseki_result = joseki_plugin.create_cron_schedule(
    {
        "cron_expression": "0 6 * * 1",
        "action_definitions": [_JOSEKI_ACTION],
        "label": "Scheduled platform quality sweep",
    },
    {"session_id": "session-contract", "flow_id": "flow-contract"},
)
_check(
    str(joseki_result.get("action_status", "")).lower() == "completed",
    "plugin accepts deterministic run_joseki action definition",
)
joseki_saved = next(iter(joseki_plugin._memory_schedules.values()), None)  # noqa: SLF001
_check(
    joseki_saved is not None
    and len(joseki_saved.actions) == 1
    and joseki_saved.actions[0].name == _JOSEKI_ACTION["process_key"],
    "scheduled joseki fixture pins the run_joseki process key",
)
_check(
    joseki_saved is not None
    and joseki_saved.actions[0].parameters == _JOSEKI_ACTION["arguments"],
    "scheduled joseki fixture pins key, bindings, and label arguments",
)
_check(
    joseki_saved is not None
    and joseki_saved.actions[0].result_processor is None
    and joseki_saved.actions[0].result_processor_kind is None,
    "scheduled run_joseki submission carries no inference processor",
)

for method_name in _PLUGIN_METHOD_BASES:
    processor_plugin, processor_result = _call_plugin(
        method_name,
        {"action_definitions": [_PROCESSOR_ACTION]},
    )
    _check(
        str(processor_result.get("action_status", "")).lower() == "completed",
        f"{method_name} accepts a legitimate deterministic result processor",
    )
    processor_saved = next(
        iter(processor_plugin._memory_schedules.values()),  # noqa: SLF001
        None,
    )
    _check(
        processor_saved is not None
        and len(processor_saved.actions) == 1
        and processor_saved.actions[0].result_processor
        == _PROCESSOR_ACTION["result_processor"],
        f"{method_name} preserves result_processor",
    )
    _check(
        processor_saved is not None
        and processor_saved.actions[0].result_processor_kind
        == _PROCESSOR_ACTION["result_processor_kind"],
        f"{method_name} preserves result_processor_kind",
    )

for method_name in _PLUGIN_METHOD_BASES:
    for legacy_label, legacy_params in (
        (
            "legacy actions entry",
            {
                "actions": [
                    {
                        "name": _PEER_ACTION["process_key"],
                        "parameters": _PEER_ACTION["arguments"],
                    }
                ]
            },
        ),
        (
            "legacy action_name",
            {
                "action_name": _PEER_ACTION["process_key"],
                "action_parameters": _PEER_ACTION["arguments"],
            },
        ),
    ):
        legacy_plugin, legacy_result = _call_plugin(method_name, legacy_params)
        legacy_saved = next(
            iter(legacy_plugin._memory_schedules.values()),  # noqa: SLF001
            None,
        )
        _check(
            str(legacy_result.get("action_status", "")).lower() == "completed",
            f"{method_name} preserves valid {legacy_label}",
        )
        _check(
            legacy_saved is not None
            and legacy_saved.actions[0].name == _PEER_ACTION["process_key"]
            and legacy_saved.actions[0].parameters == _PEER_ACTION["arguments"],
            f"{method_name} normalizes valid {legacy_label} without data loss",
        )


print("\nCase D: scheduling modes are exactly-one and fail loudly")
both_error = _expect_value_error(
    lambda: ScheduleFactory.parse_actions_from_params(
        {
            "action_definitions": [_PEER_ACTION],
            "memory_tag": "terminal:lookup",
        }
    ),
    "factory rejects action_definitions plus memory_tag",
)
if both_error is not None:
    _check(
        "exactly one" in str(both_error).lower(),
        "both-mode error explains the exactly-one contract",
    )

neither_error = _expect_value_error(
    lambda: ScheduleFactory.parse_actions_from_params({}),
    "factory rejects neither action definitions nor memory_tag",
)
if neither_error is not None:
    _check(
        "exactly one" in str(neither_error).lower(),
        "neither-mode error explains the exactly-one contract",
    )

alias_error = _expect_value_error(
    lambda: ScheduleFactory.parse_actions_from_params(
        {"actions": [_PEER_ACTION], "action_definitions": [_PEER_ACTION]}
    ),
    "factory rejects simultaneous canonical and legacy action aliases",
)
if alias_error is not None:
    _check(
        "only one" in str(alias_error).lower(),
        "alias-conflict error explains that only one action key is allowed",
    )

for empty_key in ("action_definitions", "actions"):
    empty_error = _expect_value_error(
        lambda empty_key=empty_key: ScheduleFactory.parse_actions_from_params(
            {empty_key: []}
        ),
        f"factory rejects empty {empty_key} as zero executable work",
    )
    if empty_error is not None:
        _check(
            "at least one" in str(empty_error).lower(),
            f"empty {empty_key} error requires at least one action",
        )


print("\nCase E: invalid cron and malformed actions fail before persistence")
invalid_cron_plugin, invalid_cron_result = _call_plugin(
    "create_cron_schedule",
    {
        "cron_expression": "not a cron",
        "action_definitions": [_PEER_ACTION],
    },
)
invalid_cron_error = invalid_cron_result.get("error")
_check(
    isinstance(invalid_cron_error, dict)
    and invalid_cron_error.get("code")
    == "default_scheduling_plugin.invalid_cron_expression",
    "create_cron_schedule preserves the exact invalid-cron runtime code",
)
_check(
    not invalid_cron_plugin._memory_schedules,  # noqa: SLF001
    "invalid cron persists zero schedules",
)

invalid_action_inputs: tuple[tuple[str, dict[str, Any]], ...] = (
    ("blank legacy action_name", {"action_name": ""}),
    ("whitespace legacy action_name", {"action_name": "  "}),
    (
        "blank canonical process_key",
        {"action_definitions": [{"process_key": "", "arguments": {}}]},
    ),
    (
        "whitespace canonical process_key",
        {"action_definitions": [{"process_key": "  ", "arguments": {}}]},
    ),
    (
        "blank action-entry name",
        {"actions": [{"name": "", "parameters": {}}]},
    ),
    (
        "whitespace action-entry name",
        {"actions": [{"name": "  ", "parameters": {}}]},
    ),
    (
        "unknown action-entry field",
        {"action_definitions": [{**_PEER_ACTION, "bogus": True}]},
    ),
    (
        "conflicting name and process_key aliases",
        {
            "actions": [
                {
                    "name": "service_interface::thinking_service::run_joseki",
                    "process_key": _PEER_ACTION["process_key"],
                    "parameters": {},
                }
            ]
        },
    ),
    (
        "conflicting parameters and arguments aliases",
        {
            "actions": [
                {
                    "name": _PEER_ACTION["process_key"],
                    "parameters": {"source": "legacy"},
                    "arguments": {"source": "canonical"},
                }
            ]
        },
    ),
    ("non-list action_definitions", {"action_definitions": _PEER_ACTION}),
    ("non-object action entry", {"action_definitions": ["not-an-object"]}),
    ("missing action process key", {"action_definitions": [{"arguments": {}}]}),
    (
        "non-object action arguments",
        {
            "action_definitions": [
                {"process_key": _PEER_ACTION["process_key"], "arguments": []}
            ]
        },
    ),
    ("empty canonical action list", {"action_definitions": []}),
    (
        "inference-bearing scheduled action",
        {
            "action_definitions": [
                {**_PEER_ACTION, "result_processor_kind": "inference"}
            ]
        },
    ),
)

for method_name in _PLUGIN_METHOD_BASES:
    for case_label, invalid_params in invalid_action_inputs:
        invalid_plugin, invalid_result = _call_plugin(method_name, invalid_params)
        _check(
            str(invalid_result.get("action_status", "")).lower() == "error",
            f"{method_name} rejects {case_label}",
        )
        _check(
            _is_parameter_error(invalid_result),
            f"{method_name} reports PARAMETER_ERROR for {case_label}",
        )
        _check(
            not invalid_plugin._memory_schedules,  # noqa: SLF001
            f"{method_name} persists zero schedules for {case_label}",
        )

for method_name in _PLUGIN_METHOD_BASES:
    for case_label, memory_tag in (
        ("empty memory_tag", ""),
        ("whitespace memory_tag", "  "),
        ("non-string memory_tag", 42),
    ):
        invalid_plugin, invalid_result = _call_plugin(
            method_name,
            {"memory_tag": memory_tag},
        )
        _check(
            str(invalid_result.get("action_status", "")).lower() == "error",
            f"{method_name} rejects {case_label}",
        )
        _check(
            _is_parameter_error(invalid_result),
            f"{method_name} reports PARAMETER_ERROR for {case_label}",
        )
        _check(
            not invalid_plugin._memory_schedules,  # noqa: SLF001
            f"{method_name} persists zero schedules for {case_label}",
        )


print("\nCase F: delayed validation precedes every memory or schedule side effect")
mixed_memory = _CapturingMemoryService()
mixed_plugin, mixed_result = _call_plugin(
    "execute_in_seconds",
    {
        "memory_tag": "side-effect:probe",
        "content": "must-not-persist",
        "action_definitions": [_PEER_ACTION],
    },
    memory_service=mixed_memory,
)
_check(
    str(mixed_result.get("action_status", "")).lower() == "error",
    "delayed mixed action/memory mode fails",
)
_check(
    _is_parameter_error(mixed_result),
    "delayed mixed action/memory mode reports PARAMETER_ERROR",
)
_check(not mixed_memory.calls, "mixed-mode rejection performs zero memory writes")
_check(
    not mixed_plugin._memory_schedules,  # noqa: SLF001
    "mixed-mode rejection performs zero schedule writes",
)

direct_content_memory = _CapturingMemoryService()
direct_content_plugin, direct_content_result = _call_plugin(
    "execute_in_seconds",
    {
        "action_definitions": [_PEER_ACTION],
        "content": "must-not-be-ignored",
        "tags": ["  invalid:must-not-persist  "],
    },
    memory_service=direct_content_memory,
)
_check(
    str(direct_content_result.get("action_status", "")).lower() == "error",
    "delayed direct-action mode rejects content",
)
_check(
    _is_parameter_error(direct_content_result),
    "delayed direct-action content reports PARAMETER_ERROR",
)
_check(
    not direct_content_memory.calls,
    "direct-action content rejection performs zero memory writes",
)
_check(
    not direct_content_plugin._memory_schedules,  # noqa: SLF001
    "direct-action content rejection performs zero schedule writes",
)


print("\nCase G: memory_tag remains a legitimate terminal read")
memory_actions, memory_name, memory_parameters = ScheduleFactory.parse_actions_from_params(
    {"memory_tag": "terminal:lookup"}
)
_check(
    memory_name == "service_interface::memory_service::get_memories_by_tag",
    "memory_tag builds the canonical memory lookup",
)
_check(
    memory_parameters == {"tag": "terminal:lookup", "include_archived": False},
    "memory lookup parameters remain intact",
)
_check(
    len(memory_actions) == 1
    and memory_actions[0].result_processor is None
    and memory_actions[0].result_processor_kind is None,
    "memory lookup is terminal and does not start inference",
)

valid_memory = _CapturingMemoryService()
valid_memory_plugin, valid_memory_result = _call_plugin(
    "execute_in_seconds",
    {"memory_tag": "  terminal:one-step  ", "content": "remember then read"},
    memory_service=valid_memory,
)
_check(
    str(valid_memory_result.get("action_status", "")).lower() == "completed",
    "valid delayed memory-tag/content mode completes",
)
_check(
    valid_memory.calls
    == [
        {
            "content": "remember then read",
            "tags": ["terminal:one-step"],
            "session_id": "session-contract",
        }
    ],
    "valid delayed memory write uses the normalized tag after validation",
)
_check(
    len(valid_memory_plugin._memory_schedules) == 1,  # noqa: SLF001
    "valid delayed memory-tag/content mode persists one schedule",
)

delayed_tags_plugin, delayed_tags_result = _call_plugin(
    "execute_in_seconds",
    {
        "action_definitions": [_PEER_ACTION],
        "tags": ["  delayed:first  ", "", "delayed:second"],
    },
)
_check(
    str(delayed_tags_result.get("action_status", "")).lower() == "completed",
    "execute_in_seconds accepts list tags",
)
delayed_tags_saved = next(
    iter(delayed_tags_plugin._memory_schedules.values()),  # noqa: SLF001
    None,
)
_check(
    delayed_tags_saved is not None
    and delayed_tags_saved.tags == ["delayed:first", "delayed:second"],
    "execute_in_seconds normalizes and persists list tags",
)


print("\nCase H: discoverable metadata and references tell the real contract")
plugin_metadata = SchedulingPlugin.create_cron_schedule._platform_process_metadata  # type: ignore[attr-defined]  # noqa: SLF001
plugin_params = plugin_metadata.parameters
_check(
    plugin_params["tags"].type == service_params["tags"].type
    and plugin_params["tags"].type.value == "list",
    "direct plugin create-cron tags metadata matches service LIST semantics",
)
delayed_plugin_params = SchedulingPlugin.execute_in_seconds._platform_process_metadata.parameters  # type: ignore[attr-defined]  # noqa: SLF001
delayed_service_params = SchedulingServiceAPI.execute_in_seconds._service_interface_metadata.parameters  # type: ignore[attr-defined]  # noqa: SLF001
delayed_plugin_tags = delayed_plugin_params.get("tags")
_check(
    delayed_plugin_tags is not None
    and delayed_plugin_tags.type == delayed_service_params["tags"].type
    and delayed_plugin_tags.type.value == "list",
    "direct plugin execute-in-seconds tags metadata matches service LIST semantics",
)
_check(
    "action_definitions" in plugin_params
    and plugin_params["action_definitions"].required is False,
    "plugin metadata exposes optional action_definitions",
)
_check(
    plugin_params["memory_tag"].required is False,
    "plugin metadata exposes memory_tag as the alternate mode",
)
_check(
    "model decides" not in plugin_params["memory_tag"].description.lower(),
    "plugin memory_tag description does not claim model decision",
)
_check(
    "terminal" in plugin_params["memory_tag"].description.lower()
    and "does not start a model turn"
    in plugin_params["memory_tag"].description.lower(),
    "plugin memory_tag description states terminal no-model-turn behavior",
)

metadata_surfaces = (
    (
        "service create_cron_schedule",
        SchedulingServiceAPI.create_cron_schedule._service_interface_metadata.parameters,  # type: ignore[attr-defined]  # noqa: SLF001
    ),
    (
        "service execute_in_seconds",
        SchedulingServiceAPI.execute_in_seconds._service_interface_metadata.parameters,  # type: ignore[attr-defined]  # noqa: SLF001
    ),
    (
        "plugin create_cron_schedule",
        SchedulingPlugin.create_cron_schedule._platform_process_metadata.parameters,  # type: ignore[attr-defined]  # noqa: SLF001
    ),
    (
        "plugin execute_in_seconds",
        SchedulingPlugin.execute_in_seconds._platform_process_metadata.parameters,  # type: ignore[attr-defined]  # noqa: SLF001
    ),
)
for surface_name, surface_params in metadata_surfaces:
    action_description = surface_params["action_definitions"].description.lower()
    _check(
        "non-empty" in action_description
        and "syntactically valid" in action_description
        and "execution resolves" in action_description,
        f"{surface_name} metadata states the validated registration/execution boundary",
    )
    _check(
        "arbitrary" not in action_description,
        f"{surface_name} metadata does not promise arbitrary action acceptance",
    )

for surface_name, surface_params in metadata_surfaces:
    if "content" not in surface_params:
        continue
    content_description = surface_params["content"].description.lower()
    _check(
        "only valid with memory_tag" in content_description,
        f"{surface_name} metadata rejects content outside memory-tag mode",
    )

process_dir = (
    REPO_ROOT / "plugins" / "default_scheduling_plugin" / "knowledge_base" / "processes"
)
card_docs: list[tuple[str, dict[str, Any]]] = []
for process_name in ("create_cron_schedule", "execute_in_seconds"):
    process_doc = json.loads((process_dir / f"{process_name}.json").read_text())
    card_docs.append((f"plugin {process_name}", process_doc))
    combined = " ".join(
        str(process_doc.get(field, ""))
        for field in ("description", "embedding_description")
    ).lower()
    _check(
        "model decides" not in combined,
        f"{process_name} process description does not promise inference",
    )
    _check(
        "terminal" in combined and "does not start a model turn" in combined,
        f"{process_name} process description identifies terminal no-model-turn lookup",
    )

service_process_dir = (
    REPO_ROOT / "ananta" / "knowledge_base" / "processes" / "scheduling_service"
)
for process_name in ("create_cron_schedule", "execute_in_seconds"):
    service_process_doc = json.loads(
        (service_process_dir / f"{process_name}.json").read_text()
    )
    card_docs.append((f"service {process_name}", service_process_doc))
    service_combined = " ".join(
        str(service_process_doc.get(field, ""))
        for field in ("description", "embedding_description")
    ).lower()
    _check(
        "model decides" not in service_combined,
        f"service {process_name} description does not promise inference",
    )
    _check(
        "terminal" in service_combined
        and "does not start a model turn" in service_combined,
        f"service {process_name} description identifies terminal no-model-turn lookup",
    )
    _check(
        "mechanizable joseki" in service_combined,
        f"service {process_name} limits Joseki scheduling to mechanizable cards",
    )

for card_name, card_doc in card_docs:
    card_text = json.dumps(card_doc).lower()
    _check(
        "arbitrary" not in card_text
        and "non-empty" in card_text
        and "syntactically valid" in card_text
        and "execution resolves" in card_text,
        f"{card_name} card states the validated registration/execution boundary",
    )
    _check(
        "joseki" not in card_text or "mechanizable joseki" in card_text,
        f"{card_name} card makes only mechanizable Joseki claims",
    )
    error_text = json.dumps(card_doc["error_processor_customizations"]).lower()
    _check(
        "default_scheduling_plugin.parameter_error" in error_text,
        f"{card_name} card names the runtime PARAMETER_ERROR code",
    )

for card_name, card_doc in card_docs:
    if "create_cron_schedule" not in card_name:
        continue
    error_text = json.dumps(card_doc["error_processor_customizations"]).lower()
    _check(
        "default_scheduling_plugin.invalid_cron_expression" in error_text
        and "default_scheduling_plugin.parameter_error" in error_text,
        f"{card_name} card distinguishes invalid-cron and parameter errors",
    )

for card_name, card_doc in card_docs:
    if "execute_in_seconds" not in card_name:
        continue
    card_text = json.dumps(card_doc).lower()
    _check(
        "content is valid only with memory_tag" in card_text,
        f"{card_name} card rejects content outside memory-tag mode",
    )

reference = (
    REPO_ROOT
    / "plugins"
    / "default_scheduling_plugin"
    / "knowledge_base"
    / "scheduling_reference.md"
).read_text()
_check(
    "plugin::agent_messaging_plugin::peer_send_by_name" in reference,
    "reference includes the scheduled peer_send_by_name fixture",
)
_check(
    '"action_definitions"' in reference,
    "reference fixture uses the canonical action_definitions field",
)
_check(
    '"process_key": "service_interface::thinking_service::run_joseki"'
    in reference,
    "reference includes the scheduled run_joseki fixture",
)
_check(
    '"joseki_key": "run_platform_quality_gates"' in reference
    and '"bindings": {}' in reference
    and '"label": "Scheduled platform quality sweep"' in reference,
    "reference pins the mechanizable joseki key, bindings, and label",
)
_check(
    "no model" in reference.lower() and "terminal" in reference.lower(),
    "reference distinguishes direct execution from terminal memory lookup",
)
_check(
    "non-empty" in reference.lower()
    and "syntactically valid" in reference.lower()
    and "execution resolves" in reference.lower()
    and "arbitrary" not in reference.lower(),
    "reference states the validated registration/execution boundary",
)
_check(
    "content is valid only with `memory_tag`" in reference.lower(),
    "reference rejects delayed content outside memory-tag mode",
)

# The template-flow reference article lives in a platform knowledge-base
# section that ships only in the origin checkout; a seed clone announces
# itself by the factory provenance stamp at its root and prunes that section.
# There the reference half is out of scope by construction. In the origin
# checkout the read below stays a hard requirement.
_template_flow_path = (
    REPO_ROOT
    / "ananta"
    / "knowledge_bases"
    / "ananta_platform"
    / "21_scheduling_service"
    / "01_template_flow_record_lifecycle.md"
)
if (REPO_ROOT / "PROVENANCE.json").is_file() and not _template_flow_path.exists():
    print("SKIP  template-flow reference checks: reference article is pruned from seed clones")
else:
    template_flow = _template_flow_path.read_text()
    _check(
        "intentionally not wired through the validator" not in template_flow.lower()
        and "execute_in_seconds" in template_flow
        and "before persistence" in template_flow.lower(),
        "template-flow contract applies scheduled-action validation to delayed registration",
    )
    _check(
        "non-empty" in template_flow.lower()
        and "syntactically valid" in template_flow.lower()
        and "execution resolves" in template_flow.lower(),
        "template-flow states the validated registration/execution boundary",
    )


print(f"\n{_passed} passed, {len(_failed)} failed")
if _failed:
    for failed_label in _failed:
        print(f"  FAILED: {failed_label}")
    sys.exit(1)
sys.exit(0)
