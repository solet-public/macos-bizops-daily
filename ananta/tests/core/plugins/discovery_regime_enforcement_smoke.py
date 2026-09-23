#!/usr/bin/env python3
"""Regression smoke for the boot-only plugin discovery regime (regs 237-241).

The live action-poller boundary makes a plugin roster serving. Discovery is
allowed before that boundary, where replacement teardown is ordered through
PluginInstaller.remove; it must refuse afterwards. Boot also reports a
manifest-declared plugin that discovery did not load, while continuing startup.

Offline only; no entry-point installation or live Solet is required.
"""

from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import cast
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(REPO_ROOT / "ananta" / "src"))

from ananta.core.orchestration import startup_sequence  # noqa: E402
from ananta.core.orchestration.managers.plugin_lifecycle_manager import (  # noqa: E402
    PluginLifecycleManager,
)
from ananta.core.orchestration.runtime_manager import RuntimeManager  # noqa: E402
from ananta.core.plugins.plugin_base import PluginBase  # noqa: E402
from ananta.core.plugins.plugin_manager import PluginManager  # noqa: E402
from ananta.core.services.service_transition_coordinator import (  # noqa: E402
    ServiceTransitionCoordinator,
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


class _ResidentPlugin:
    def __init__(self, events: list[tuple[str, bool]]) -> None:
        self._events = events

    def stop_services(self) -> None:
        self._events.append(("stop", True))

    def prepare_for_readiness(self) -> None:
        return None

    def start_services(self) -> None:
        return None

    def is_running(self) -> bool:
        return True

    def set_active(self, _active: bool) -> None:
        return None

    def get_readiness_error(self) -> None:
        return None

    def set_error(self, _error_message: str) -> None:
        return None

    def get_available_actions(self) -> list[object]:
        return [SimpleNamespace(plugin="resident", function="probe")]


class _Registry:
    def __init__(self, manager: PluginManager, events: list[tuple[str, bool]]) -> None:
        self._manager = manager
        self._events = events

    def unregister_dynamic_processes(self, _process_keys: list[str]) -> None:
        self._events.append(("deregister", "resident" in self._manager.plugins))


class _Poller:
    async def start(self) -> None:
        return None


class _ServingManager:
    def __init__(self) -> None:
        self.marked_serving = False
        self.plugins: dict[str, object] = {}

    def _mark_serving(self) -> None:
        self.marked_serving = True


class _LifecyclePluginManager:
    def __init__(self) -> None:
        self.plugins: dict[str, object] = {}
        self.discover_calls: list[tuple[object | None, set[str] | None]] = []

    def discover_plugins(
        self, config_manager: object | None = None, *, allowed_plugins: set[str] | None = None
    ) -> None:
        self.discover_calls.append((config_manager, allowed_plugins))

    def set_orchestrator_ref(self, _orchestrator: object) -> None:
        return None


def _case_serving_guard_and_teardown() -> None:
    print("\n1: serving guard refuses live discovery; pre-serving replacement tears down")
    manager = PluginManager()
    mark_serving = getattr(manager, "_mark_serving", None)
    _check(callable(mark_serving), "PluginManager exposes a serving-state transition")
    if callable(mark_serving):
        mark_serving()
        try:
            manager.discover_plugins(allowed_plugins=set())
        except RuntimeError as exc:
            _check("serving" in str(exc).lower(), "post-serving discover_plugins refuses loudly")
        else:
            _check(False, "post-serving discover_plugins refuses loudly")

    replacement_manager = PluginManager()
    events: list[tuple[str, bool]] = []
    replacement_manager.plugins["resident"] = cast(PluginBase, _ResidentPlugin(events))
    registry = _Registry(replacement_manager, events)
    replacement_manager._orchestrator_ref = cast(  # noqa: SLF001
        object, SimpleNamespace(_process_registry_manager=registry)
    )
    replacement_manager._discovery.discover = lambda _allowed, _config: {}  # noqa: SLF001
    replacement_manager.discover_plugins(allowed_plugins=set())
    _check(events == [("stop", True), ("deregister", True)], "replacement uses stop then deregister before roster deletion")
    _check("resident" not in replacement_manager.plugins, "replacement roster entry is deleted after teardown")


def _case_serving_transition() -> None:
    print("\n2: orchestrator marks the roster serving after action dispatch starts")
    plugin_manager = _ServingManager()
    orchestrator = SimpleNamespace(
        action_queue_poller=_Poller(),
        plugin_manager=plugin_manager,
    )
    asyncio.run(RuntimeManager(orchestrator)._start_action_queue_poller())
    _check(plugin_manager.marked_serving, "action-poller start marks PluginManager serving")


def _case_boot_completeness_is_loud_not_fatal() -> None:
    print("\n3: boot reports a missing manifest plugin and continues")
    records: list[logging.LogRecord] = []

    class _Capture(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append(record)

    handler = _Capture()
    startup_sequence.logger.addHandler(handler)
    try:
        reporter = getattr(startup_sequence, "_report_plugin_discovery_completeness", None)
        _check(callable(reporter), "startup exposes discovery-completeness reporting")
        if callable(reporter):
            reporter(SimpleNamespace(plugins={"loaded": object()}), {"loaded", "missing"})
    finally:
        startup_sequence.logger.removeHandler(handler)
    _check(
        any("missing" in record.getMessage() and record.levelno >= logging.ERROR for record in records),
        "incomplete boot roster is logged at error level without raising",
    )


def _case_manifest_propagation_and_clone_removal() -> None:
    print("\n4: published lifecycle path keeps manifest gating; async clone is absent")
    manager = PluginLifecycleManager()
    plugin_manager = _LifecyclePluginManager()
    orchestrator = SimpleNamespace(APP_HOME="/unused")
    with patch(
        "ananta.core.orchestration.managers.plugin_lifecycle_manager.load_manifest_plugin_set",
        return_value={"allowed"},
    ):
        manager.discover_and_initialize_plugins(plugin_manager, orchestrator)
    _check(
        plugin_manager.discover_calls == [(None, {"allowed"})],
        "lifecycle discovery forwards the manifest allowlist",
    )
    _check(
        not hasattr(ServiceTransitionCoordinator, "execute_full_transition"),
        "unused async execute_full_transition clone is deleted",
    )


def main() -> int:
    print("Discovery-regime enforcement smoke")
    print("=================================")
    _case_serving_guard_and_teardown()
    _case_serving_transition()
    _case_boot_completeness_is_loud_not_fatal()
    _case_manifest_propagation_and_clone_removal()
    print(f"\nPASSED: {_passed}")
    print(f"FAILED: {len(_failed)}")
    return 1 if _failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
