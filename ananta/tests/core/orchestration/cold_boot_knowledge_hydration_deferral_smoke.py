#!/usr/bin/env python3
"""Regression smoke for cold-start activation versus first-boot KB hydration.

The fixture blocks auto-install on an event instead of sleeping. It proves the
startup path can arm router registration while first-boot knowledge hydration is
still in progress.
"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
import threading
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(REPO_ROOT / "ananta" / "src"))

from ananta.core.orchestration import startup_sequence  # noqa: E402
from ananta.core.orchestration.runtime_manager import RuntimeManager  # noqa: E402

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


class _Bindings:
    @staticmethod
    def get_plugin_name(_service_name: object) -> str:
        return "default_knowledge_plugin"


class _KnowledgePlugin:
    def __init__(self, entered: threading.Event, release: threading.Event) -> None:
        self._entered = entered
        self._release = release

    def auto_install_knowledge_bases(self, manifest_plugin_set: set[str] | None) -> None:
        _check(manifest_plugin_set is None, "missing manifest retains install-all semantics")
        self._entered.set()
        self._release.wait(timeout=1.0)


class _FailingKnowledgePlugin:
    def __init__(self) -> None:
        self.calls = 0

    def auto_install_knowledge_bases(self, manifest_plugin_set: set[str] | None) -> None:
        self.calls += 1
        raise RuntimeError("injected hydration failure")


class _Poller:
    def __init__(self, events: list[str]) -> None:
        self._events = events
        self.running = False

    async def start(self) -> None:
        self.running = True
        self._events.append("poller_started")


class _PluginManager:
    def __init__(self, plugins: dict[str, object], events: list[str]) -> None:
        self.plugins = plugins
        self._events = events

    def _mark_serving(self) -> None:
        self._events.append("serving_marked")


class _SelfDeploymentPlugin:
    def __init__(self, events: list[str]) -> None:
        self._events = events

    def arm_registration_deadline(self) -> None:
        self._events.append("registration_armed")


class _Orchestrator:
    def __init__(self, app_home: str, plugins: dict[str, object], events: list[str]) -> None:
        self.APP_HOME = app_home
        self.service_bindings = _Bindings()
        self.plugin_manager = _PluginManager(plugins, events)
        self.action_queue_poller = _Poller(events)


def test_hydration_defers_without_holding_registration() -> None:
    entered = threading.Event()
    release = threading.Event()
    events: list[str] = []
    knowledge = _KnowledgePlugin(entered, release)
    self_deployment = _SelfDeploymentPlugin(events)
    with tempfile.TemporaryDirectory() as app_home:
        orchestrator = _Orchestrator(
            app_home,
            {
                "default_knowledge_plugin": knowledge,
                "macos_self_deployment_plugin": self_deployment,
            },
            events,
        )
        old_probe_mode = os.environ.pop("SOLET_PROBE_MODE", None)
        try:
            startup_sequence._auto_install_knowledge_bases(orchestrator)
            _check(
                entered.wait(timeout=0.1),
                "first-boot knowledge hydration starts in a background worker",
            )
            _check(
                orchestrator.knowledge_hydration_status["state"] == "pending",
                "in-progress hydration exposes pending state to health reporting",
            )
            asyncio.run(RuntimeManager(orchestrator)._start_action_queue_poller())
            _check(
                events == ["poller_started", "serving_marked", "registration_armed"],
                "registration arms while first-boot hydration remains blocked",
            )
        finally:
            release.set()
            if old_probe_mode is not None:
                os.environ["SOLET_PROBE_MODE"] = old_probe_mode


def test_hydration_failure_retries_and_reports_failed_state() -> None:
    events: list[str] = []
    failing_knowledge = _FailingKnowledgePlugin()
    with tempfile.TemporaryDirectory() as app_home:
        orchestrator = _Orchestrator(app_home, {}, events)
        startup_sequence._run_auto_install_knowledge_bases(
            orchestrator,
            failing_knowledge,
            None,
            sleep=lambda _seconds: None,
        )
        _check(
            failing_knowledge.calls > 1,
            "failing hydration is retried without sleeping in the fixture",
        )
        _check(
            orchestrator.knowledge_hydration_status["state"] == "failed",
            "exhausted hydration retries expose failed state to health reporting",
        )


def test_hydration_completion_reports_complete_state() -> None:
    events: list[str] = []
    knowledge = _FailingKnowledgePlugin()
    knowledge.auto_install_knowledge_bases = lambda manifest_plugin_set: None  # type: ignore[method-assign]
    with tempfile.TemporaryDirectory() as app_home:
        orchestrator = _Orchestrator(app_home, {}, events)
        startup_sequence._run_auto_install_knowledge_bases(orchestrator, knowledge, None)
        _check(
            orchestrator.knowledge_hydration_status == {"state": "complete", "attempt": 1},
            "successful hydration exposes complete state to health reporting",
        )


def main() -> int:
    print("=== cold_boot_knowledge_hydration_deferral_smoke ===")
    test_hydration_defers_without_holding_registration()
    test_hydration_failure_retries_and_reports_failed_state()
    test_hydration_completion_reports_complete_state()
    print(f"\n{_passed} passed, {len(_failed)} failed")
    return 1 if _failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
