#!/usr/bin/env python3
"""Focused regression for managed-dispatch CLI caller attribution."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, cast

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "ananta" / "src"))
sys.path.insert(0, str(ROOT / "plugins" / "agent_messaging_plugin" / "src"))

import agent_messaging_plugin.plugin as plugin_module  # noqa: E402
from agent_messaging_plugin.managed_dispatch import DispatchActor, DispatchError  # noqa: E402
from agent_messaging_plugin.plugin import AgentMessagingPlugin  # noqa: E402

_passed = 0
_failed: list[str] = []


class _Registry:
    def __init__(self, bindings: dict[str, str]) -> None:
        self.bindings = bindings

    def agent_session_id_for_instance(self, instance: str) -> str:
        return self.bindings.get(instance, "")


def _check(condition: object, label: str) -> None:
    global _passed
    if condition:
        _passed += 1
        print(f"  PASS  {label}")
    else:
        _failed.append(label)
        print(f"  FAIL  {label}")


def _plugin(bindings: dict[str, str]) -> AgentMessagingPlugin:
    plugin = AgentMessagingPlugin()
    plugin._peer_registry = cast(Any, _Registry(bindings))  # noqa: SLF001
    return plugin


def _actor(plugin: AgentMessagingPlugin, state: dict[str, Any]) -> DispatchActor | str:
    try:
        return plugin._dispatch_actor_from_state(state)  # noqa: SLF001
    except DispatchError as exc:
        return exc.code


def _report_actor(plugin: AgentMessagingPlugin) -> DispatchActor:
    captured: list[DispatchActor] = []

    def _report(_: object, **kwargs: Any) -> dict[str, str]:
        captured.append(cast(DispatchActor, kwargs["actor"]))
        return {"projection": "accepted"}

    cast(Any, plugin)._get_state_service = lambda: object()  # noqa: SLF001
    original = plugin_module.lifecycle_report_managed_dispatch
    plugin_module.lifecycle_report_managed_dispatch = _report
    try:
        response = plugin.report_managed_dispatch(
            {
                "dispatch_id": "mdp-cli",
                "event_id": "evt-cli",
                "event_kind": "progress",
                "attempt_agent_instance_id": "agi-cli",
                "prior_version": 1,
                "payload": {},
            },
            {"caller_attribution_instance_id": "agi-cli"},
        )
    finally:
        plugin_module.lifecycle_report_managed_dispatch = original
    if response.get("action_status") != "completed" or len(captured) != 1:
        raise AssertionError(
            "report path did not receive one attributed dispatch actor: "
            f"response={response!r} captured={captured!r}"
        )
    return captured[0]


def main() -> int:
    plugin = _plugin({"agi-registered": "ases-registered", "agi-cli": "ases-cli"})
    attributed = _actor(plugin, {"caller_attribution_instance_id": "agi-cli"})
    precedence = _actor(
        plugin,
        {
            "inference_vertex_session_id": "agi-registered",
            "caller_attribution_instance_id": "agi-cli",
        },
    )
    _check(
        attributed == DispatchActor("agi-cli", "ases-cli", "live_peer_binding"),
        "attributed CLI call resolves its current peer binding",
    )
    _check(
        precedence == DispatchActor("agi-registered", "ases-registered", "live_peer_binding"),
        "registered inference identity takes precedence over caller attribution",
    )
    _check(
        _report_actor(plugin) == DispatchActor("agi-cli", "ases-cli", "live_peer_binding"),
        "normal managed-dispatch report path receives the attributed actor",
    )
    _check(
        _actor(_plugin({}), {"caller_attribution_instance_id": "agi-stale"})
        == "dispatch_identity_unregistered"
        and _actor(plugin, {}) == "dispatch_authentication_required",
        "missing or stale identity does not fabricate another session",
    )
    print(f"\nmanaged dispatch CLI actor smoke: {_passed} passed, {len(_failed)} failed")
    return 1 if _failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
