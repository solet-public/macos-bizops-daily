#!/usr/bin/env python3
"""W5.K (B) smoke — `default_scheduling_plugin._action_executor` injection contract.

Background: between 2026-06-12 20:35 PT and 2026-06-13 11:51 PT, the running platform instance had
`default_scheduling_plugin._action_executor=None`, causing every cron-driven
callback to fail silently at `plugin.py:493-498` with `SCHEDULER-CALLBACK-ERROR:
ActionExecutor not initialized`. The failure was a 22-hour silent break (nothing
logs at INFO on successful fires; the ERROR was buried in the profile log; no
session observed the symptom because cron-driven session wake-ups were exactly
what was broken). Root cause was state corruption from a prior blue-green cycle;
the 11:51 cutover self-healed it. See the P0 investigation record for the
empirical investigation and the W5.K reality-flip semantics (folded into
the same doc; both dev-checkout workbench records — not part of the
shipped tree).

W5.K (A): a `StartupError` is raised at the end of `_init_actions` (startup
sequence step 19) if `default_scheduling_plugin._action_executor` is still None
after the canonical injection walk completes. This converts the failure mode
from "silent for 22 hours" to "platform startup fails loudly."

W5.K (B) — this smoke: prove the contract three ways.

  1. `set_action_factory` sets `_action_executor` to a non-None ActionExecutor
     wrapping the supplied action_factory.
  2. The `_verify_action_factory_injected_into_scheduling_plugin` startup-time
     check returns silently when injection succeeded.
  3. The same check raises `StartupError` when injection did not happen
     (synthesizes the 2026-06-12/13 state-corruption condition).

Project policy: no pytest. Exits 0 on success, 1 on first failure.
"""

from __future__ import annotations

import asyncio
import sys
import types
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT / "ananta" / "src"))
sys.path.insert(0, str(REPO_ROOT / "plugins" / "default_scheduling_plugin" / "src"))

from ananta.core.orchestration.startup_sequence import (  # noqa: E402
    StartupError,
    _verify_action_factory_injected_into_scheduling_plugin,
)
from default_scheduling_plugin.execution.action_executor import ActionExecutor  # noqa: E402
from default_scheduling_plugin.plugin import SchedulingPlugin  # noqa: E402

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


class _FakeActionFactory:
    """Stand-in implementing ActionFactoryProtocol's structural contract."""

    def submit_action_definition(
        self,
        _action_definition: dict[str, Any],
        _context: dict[str, Any] | None = None,
    ) -> str:
        return "ae-smoke-fake-action-id"


def _fresh_scheduling_plugin() -> SchedulingPlugin:
    """Return a SchedulingPlugin with the post-__init__ default state.

    `_action_executor` is None; `action_factory` is None. This is the state
    immediately after the plugin loads but before `set_action_factory` runs.
    """
    return SchedulingPlugin()


# ----- Case 1: set_action_factory wires _action_executor ---------------------
print("Case 1: set_action_factory wires _action_executor")

plugin1 = _fresh_scheduling_plugin()
_check(
    plugin1._action_executor is None,
    "fresh plugin starts with _action_executor=None",
)

factory = _FakeActionFactory()
plugin1.set_action_factory(factory)  # type: ignore[arg-type]

_check(
    plugin1._action_executor is not None,
    "after set_action_factory, _action_executor is not None",
)
_check(
    isinstance(plugin1._action_executor, ActionExecutor),
    "_action_executor is an ActionExecutor instance",
)
_check(
    plugin1.action_factory is factory,  # pyright: ignore[reportUnnecessaryComparison]
    "action_factory attribute matches the supplied factory",
)


# ----- Case 1b: scheduler waits for lifecycle + ActionFactory ----------------
class _FakeSchedulerManager:
    """Records APScheduler starts without opening a scheduler thread."""

    def __init__(self) -> None:
        self.start_calls = 0
        self.listener_registration_calls = 0
        self.scheduler = types.SimpleNamespace(running=False)

    def start(self) -> None:
        self.start_calls += 1
        self.scheduler.running = True

    def register_listeners(self) -> None:
        self.listener_registration_calls += 1


def _configure_with(plugin: SchedulingPlugin, manager: _FakeSchedulerManager) -> None:
    def _configure() -> None:
        plugin._scheduler_manager = manager  # type: ignore[assignment]

    plugin._configure_scheduler = _configure  # type: ignore[method-assign]


print("\nCase 1b: APScheduler start is gated on lifecycle and ActionFactory")

plugin_lifecycle_first = _fresh_scheduling_plugin()
manager_lifecycle_first = _FakeSchedulerManager()
_configure_with(plugin_lifecycle_first, manager_lifecycle_first)
lifecycle_result = asyncio.run(plugin_lifecycle_first.start_services())

_check(
    lifecycle_result["action_status"] == "completed" and plugin_lifecycle_first._services_started,
    "lifecycle startup completes and satisfies readiness before ActionFactory injection",
)
_check(
    manager_lifecycle_first.start_calls == 0 and not manager_lifecycle_first.scheduler.running,
    "APScheduler does not start while ActionFactory is absent",
)

plugin_lifecycle_first.set_action_factory(factory)  # type: ignore[arg-type]
_check(
    manager_lifecycle_first.start_calls == 1 and manager_lifecycle_first.scheduler.running,
    "ActionFactory injection starts the waiting APScheduler exactly once",
)

plugin_factory_first = _fresh_scheduling_plugin()
manager_factory_first = _FakeSchedulerManager()
_configure_with(plugin_factory_first, manager_factory_first)
plugin_factory_first.set_action_factory(factory)  # type: ignore[arg-type]
_check(
    manager_factory_first.start_calls == 0,
    "ActionFactory injection alone does not start APScheduler before lifecycle startup",
)
asyncio.run(plugin_factory_first.start_services())
_check(
    manager_factory_first.start_calls == 1 and manager_factory_first.scheduler.running,
    "lifecycle startup starts APScheduler when ActionFactory arrived first",
)

asyncio.run(plugin_factory_first.start_services())
plugin_factory_first.set_action_factory(factory)  # type: ignore[arg-type]
_check(
    manager_factory_first.start_calls == 1,
    "repeated lifecycle and injection calls do not start APScheduler twice",
)


# ----- Case 2: startup verifier passes when injection succeeded --------------
print("\nCase 2: startup verifier silent-passes when injection succeeded")


def _make_orch_with_plugin(plugin: SchedulingPlugin) -> Any:
    """Synthesize the minimal orchestrator shape the verifier reads."""
    plugin_manager = types.SimpleNamespace(plugins={"default_scheduling_plugin": plugin})
    return types.SimpleNamespace(plugin_manager=plugin_manager)


orch_ok = _make_orch_with_plugin(plugin1)
raised: Exception | None = None
try:
    _verify_action_factory_injected_into_scheduling_plugin(orch_ok)
except Exception as exc:  # noqa: BLE001 — smoke-only catch
    raised = exc

_check(
    raised is None,
    "verifier returns silently when _action_executor is set",
)


# ----- Case 3: startup verifier RAISES when injection didn't happen ----------
print("\nCase 3: startup verifier raises when _action_executor is None")

plugin_broken = _fresh_scheduling_plugin()
_check(
    plugin_broken._action_executor is None,
    "synthesized broken plugin has _action_executor=None",
)

orch_broken = _make_orch_with_plugin(plugin_broken)
raised_correct: Exception | None = None
try:
    _verify_action_factory_injected_into_scheduling_plugin(orch_broken)
except StartupError as exc:
    raised_correct = exc
except Exception as exc:  # noqa: BLE001 — should specifically be StartupError
    raised_correct = exc

_check(
    isinstance(raised_correct, StartupError),
    "verifier raises StartupError (not a different exception class)",
)
_check(
    raised_correct is not None and "_action_executor is None" in str(raised_correct),
    "StartupError message names the failed attribute",
)
_check(
    raised_correct is not None and "default_scheduling_plugin" in str(raised_correct),
    "StartupError message names the affected plugin",
)


# ----- Case 4: verifier is a no-op when plugin not in manifest ---------------
print("\nCase 4: verifier no-ops when default_scheduling_plugin not in manifest")

orch_no_plugin = types.SimpleNamespace(plugin_manager=types.SimpleNamespace(plugins={}))
raised_no_plugin: Exception | None = None
try:
    _verify_action_factory_injected_into_scheduling_plugin(orch_no_plugin)
except Exception as exc:  # noqa: BLE001
    raised_no_plugin = exc

_check(
    raised_no_plugin is None,
    "verifier silent-passes when scheduling plugin is absent from manifest",
)


# ----- Report ----------------------------------------------------------------
print()
print(f"Passed: {_passed}")
print(f"Failed: {len(_failed)}")
if _failed:
    for label in _failed:
        print(f"  - {label}")
    sys.exit(1)
sys.exit(0)
