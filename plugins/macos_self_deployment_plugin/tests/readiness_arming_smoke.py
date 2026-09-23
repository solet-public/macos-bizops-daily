"""Regression smoke for strict-I2 registration-deadline arming.

The heartbeat must not start from plugin readiness, because that phase is
upstream of synchronous inference prewarm and knowledge-base hydration.  It is
armed only after ``RuntimeManager`` has started the action queue, the dispatch
boundary for the bridge ``start_interface`` starting action.

Run directly with:
``.venv/bin/python3 plugins/macos_self_deployment_plugin/tests/readiness_arming_smoke.py``.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

_PROJECT_ROOT = Path(__file__).resolve().parents[3]
_SOURCE_ROOTS = (
    _PROJECT_ROOT / "ananta" / "src",
    _PROJECT_ROOT / "plugins" / "macos_self_deployment_plugin" / "src",
    _PROJECT_ROOT,
)
for source_root in reversed(_SOURCE_ROOTS):
    if str(source_root) not in sys.path:
        sys.path.insert(0, str(source_root))

from ananta.core.orchestration.runtime_manager import RuntimeManager  # noqa: E402
from macos_self_deployment_plugin.plugin import MacosSelfDeploymentPlugin  # noqa: E402


def _expect(condition: bool, message: str) -> None:
    if not condition:
        print(f"FAIL: {message}", file=sys.stderr)
        raise SystemExit(1)
    print(f"  OK  {message}")


class _Poller:
    def __init__(self, events: list[str]) -> None:
        self._events = events
        self.running = False

    async def start(self) -> None:
        self.running = True
        self._events.append("poller_started")


class _SelfDeploymentPlugin:
    def __init__(self, events: list[str], poller: _Poller) -> None:
        self._events = events
        self._poller = poller

    def arm_registration_deadline(self) -> None:
        _expect(self._poller.running, "poller is running before deadline arms")
        self._events.append("deadline_armed")


class _PluginManager:
    def __init__(self, events: list[str], poller: _Poller, plugin: object) -> None:
        self._events = events
        self._poller = poller
        self.plugins = {"macos_self_deployment_plugin": plugin}

    def _mark_serving(self) -> None:
        _expect(self._poller.running, "poller is running before roster becomes serving")
        self._events.append("roster_serving")


async def _scenario_runtime_arms_after_poller_start() -> None:
    print("Scenario 1: RuntimeManager arms strict-I2 after action dispatch starts")
    events: list[str] = []
    poller = _Poller(events)
    deployment_plugin = _SelfDeploymentPlugin(events, poller)
    plugin_manager = _PluginManager(events, poller, deployment_plugin)
    orchestrator = SimpleNamespace(
        action_queue_poller=poller,
        plugin_manager=plugin_manager,
    )

    await RuntimeManager(orchestrator)._start_action_queue_poller()

    _expect(
        events == ["poller_started", "roster_serving", "deadline_armed"],
        f"serving and deadline transitions follow poller start exactly (events={events!r})",
    )


def _scenario_plugin_arming_is_explicit() -> None:
    print("Scenario 2: self-deployment plugin exposes explicit late arming")
    plugin = MacosSelfDeploymentPlugin()
    calls: list[str] = []
    plugin._spawn_heartbeat_thread = lambda: calls.append("spawned")  # type: ignore[method-assign]

    plugin.arm_registration_deadline()

    _expect(calls == ["spawned"], "late arming delegates to heartbeat spawn once")


def main() -> int:
    asyncio.run(_scenario_runtime_arms_after_poller_start())
    print()
    _scenario_plugin_arming_is_explicit()
    print("\nAll readiness-arming scenarios passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
