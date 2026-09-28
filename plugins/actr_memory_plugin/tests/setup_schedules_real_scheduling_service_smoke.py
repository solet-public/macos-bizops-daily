#!/usr/bin/env python3
"""setup_schedules against the REAL SchedulingService wrapper (iss_2250e07c).

`setup_schedules_canonical_smoke.py` already asserts the cron action shape,
but its `_RecordingSchedulingService` fixture accepts
`create_cron_schedule(params=..., state=..., **kwargs)` — looser than the
real `ananta.services.scheduling_service.SchedulingService.create_cron_schedule`
signature (`cron_expression`, `actions`, `action_definitions`, `memory_tag`,
`label`, `tags`, `state` as explicit keywords, NO `params` kwarg at all). That
looseness let `setup_schedules` call `create_cron_schedule(params={...},
state={...})` for years without ever raising, even though the real wrapper
has always rejected `params=` with a `TypeError`. The live instance had zero schedules
tagged `actr_memory_plugin` as a result.

This smoke closes that gap by using the REAL `SchedulingService` class (per
the forwarding-proof pattern in
`plugins/default_scheduling_plugin/tests/scheduler_action_definition_configuration_smoke.py`
Case B), with only the one genuine protocol boundary faked: the underlying
`SchedulingPluginProtocol` plugin the service delegates to, which legitimately
takes `params=`/`state=`. If `setup_schedules` ever regresses back to calling
the service with `params=` instead of real keywords, `SchedulingService.
create_cron_schedule` raises `TypeError` for real here, exactly as it does live.

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
from ananta.core.plugins.plugin_base import PluginReadiness  # noqa: E402
from ananta.services.scheduling_service import SchedulingService  # noqa: E402

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


class _FakeUnderlyingSchedulingPlugin:
    """The real protocol boundary `SchedulingService` delegates to.

    Mirrors `SchedulingPluginProtocol.{create_cron_schedule,
    clear_scheduled_actions_by_tag}(params, state)` exactly — this is the
    one seam that legitimately takes `params=`/`state=`, one layer below the
    service wrapper under test. `store`, when the SAME instance (or its
    `store` dict) is reused across two `_make_plugin(...)` calls, models the
    real scheduler's cross-boot persistence
    (`default_scheduling_plugin/plugin.py:580` `restore_schedules` — the
    scheduler plugin and its DB outlive any one `actr_memory_plugin` process
    restart): a fake with no clear-by-tag support cannot represent a
    restored schedule at all, which is exactly why the pre-B1-fix fixture
    never caught the duplication (review B1).
    """

    def __init__(
        self, *, fail_label: str | None = None, store: dict[str, dict[str, Any]] | None = None,
    ) -> None:
        self.calls: list[dict[str, Any]] = []
        self._fail_label = fail_label
        self.store: dict[str, dict[str, Any]] = store if store is not None else {}
        self._next_id = 1

    def is_ready(self) -> bool:
        return True

    def get_readiness_error(self) -> str | None:
        return None

    def create_cron_schedule(
        self, params: dict[str, Any], state: dict[str, Any],
    ) -> dict[str, Any]:
        self.calls.append({"params": params, "state": state})
        if params.get("label") == self._fail_label:
            return {
                "action_status": "error",
                "error": {"message": f"simulated failure for {self._fail_label!r}"},
            }
        schedule_id = f"sched-{self._next_id}"
        self._next_id += 1
        self.store[schedule_id] = {
            "tags": list(params.get("tags", [])), "label": params.get("label"),
        }
        return {"action_status": "completed", "data": {"schedule_id": schedule_id}}

    def clear_scheduled_actions_by_tag(
        self, params: dict[str, Any], state: dict[str, Any],  # noqa: ARG002
    ) -> dict[str, Any]:
        tag = params.get("tag")
        to_remove = [sid for sid, row in self.store.items() if tag in row.get("tags", [])]
        for sid in to_remove:
            del self.store[sid]
        return {"action_status": "completed", "data": {"cleared_count": len(to_remove)}}


class _StubConfigProvider:
    def __init__(self) -> None:
        self.config: dict[str, Any] = {"enable_scheduled_operations": True}


class _FakeOrchestrator:
    """Models the REAL startup ordering (`iss_d1a7662f`), not an eager one.

    The real `EventOrchestrator.get_service` routes `scheduling_service`
    through `service_manager`, which does not exist until
    `init_service_manager` runs — AFTER `start_service_plugins`
    (`prepare_for_readiness`/`start_services`). `service_manager_ready`
    defaults False, so `get_service("scheduling_service")` returns None at
    readiness time exactly like the real orchestrator does; call
    `advance_past_init_service_manager()` to flip it, simulating the startup
    step that runs after `init_actions` (where `starting_actions` fire).
    """

    def __init__(self, scheduling_service: Any) -> None:
        self._scheduling_service = scheduling_service
        self._service_manager_ready = False

    def advance_past_init_service_manager(self) -> None:
        self._service_manager_ready = True

    def get_service(self, name: str) -> Any:
        if name != "scheduling_service" or not self._service_manager_ready:
            return None
        return self._scheduling_service


def _make_real_scheduling_service(
    underlying: _FakeUnderlyingSchedulingPlugin,
) -> SchedulingService:
    service = SchedulingService.__new__(SchedulingService)
    service._plugin = underlying  # type: ignore[assignment]  # noqa: SLF001
    return service


def _make_plugin(
    *, scheduling_service: Any = None, orchestrator_ref: Any = None,
) -> ACTRMemoryPlugin:
    instance = ACTRMemoryPlugin.__new__(ACTRMemoryPlugin)
    instance.name = "actr_memory_plugin"  # type: ignore[assignment]
    import logging  # noqa: PLC0415

    instance.logger = logging.getLogger("actr_memory_plugin.setup_schedules_real_smoke")
    instance.logger.disabled = True
    instance._backend = None  # type: ignore[assignment]
    instance.orchestrator_ref = orchestrator_ref  # type: ignore[assignment]
    instance._scheduling_service = scheduling_service
    instance._state_service = None  # type: ignore[assignment]
    instance._services_started = False
    instance._schedules_configured = False
    instance.config_provider = _StubConfigProvider()  # type: ignore[assignment]
    instance.readiness_state = PluginReadiness.READY
    instance.readiness_error = None
    return instance


def test_setup_schedules_drives_the_real_wrapper_and_creates_three_schedules() -> None:
    """No TypeError through the real keyword-only wrapper; 3 schedules created."""
    underlying = _FakeUnderlyingSchedulingPlugin()
    service = _make_real_scheduling_service(underlying)
    plugin = _make_plugin(scheduling_service=service)

    result = plugin.setup_schedules(params={}, state={"session_id": "sess-smoke"})

    _check(
        result.get("action_status") == "completed",
        f"setup_schedules completes against the real SchedulingService (got {result})",
    )
    _check(
        len(underlying.calls) == 3,
        f"exactly 3 create_cron_schedule calls reached the underlying plugin (got {len(underlying.calls)})",
    )
    _check(plugin.is_ready(), "plugin stays ready after a successful setup")


def test_each_schedule_carries_a_distinct_system_owned_flow_id() -> None:
    """The real wrapper's state= forwarding must not drop flow_id/session_id."""
    underlying = _FakeUnderlyingSchedulingPlugin()
    service = _make_real_scheduling_service(underlying)
    plugin = _make_plugin(scheduling_service=service)
    plugin.setup_schedules(params={}, state={"session_id": "should-be-overridden"})

    flow_ids = {call["state"].get("flow_id") for call in underlying.calls}
    session_ids = {call["state"].get("session_id") for call in underlying.calls}
    _check(
        None not in flow_ids and len(flow_ids) == 3,
        f"each cron carries a distinct, present flow_id (got {flow_ids})",
    )
    _check(
        None not in session_ids and len(session_ids) == 3,
        f"each cron carries a distinct, present session_id (got {session_ids})",
    )
    _check(
        "should-be-overridden" not in session_ids,
        "the caller-injected session_id is not leaked into the cron state",
    )


def test_setup_failure_is_visible_not_swallowed() -> None:
    """A non-exception create_cron_schedule failure must surface, not be skipped.

    Before the fix, a non-completed `action_status` from `create_cron_schedule`
    was silently excluded from `created_schedules` and `setup_schedules` still
    returned COMPLETED. This proves the LOUD path: an ERROR result, AND the
    plugin's readiness flips to not-ready (`set_error`), so a health check
    after setup no longer reports ready.
    """
    underlying = _FakeUnderlyingSchedulingPlugin(fail_label="ACT-R Strength Recomputation")
    service = _make_real_scheduling_service(underlying)
    plugin = _make_plugin(scheduling_service=service)

    result = plugin.setup_schedules(params={}, state={"session_id": "sess-smoke"})

    _check(
        result.get("action_status") == "error",
        f"setup_schedules surfaces the failure as action_status=error (got {result.get('action_status')!r})",
    )
    _check(
        not plugin.is_ready(),
        "plugin readiness flips to not-ready after a required schedule fails to install",
    )
    _check(
        plugin.get_readiness_error() is not None,
        "plugin.get_readiness_error() names the failure instead of staying silent",
    )
    _check(
        not plugin._schedules_configured,  # noqa: SLF001
        "the idempotency flag is NOT set on a failed setup (a retry must be possible)",
    )


def test_startup_ordering_lazy_resolution_and_ensure_schedules() -> None:
    """Proves the real startup-ordering class defect and its fix (`iss_d1a7662f`).

    `scheduling_service` is a service-manager-tier service: always None at
    `prepare_for_readiness`/`start_services` (before `init_service_manager`).
    Readiness must not require it (the plugin never raises on this); the
    `ensure_schedules` boot entry point must resolve it fresh and raise loud
    if it is STILL unavailable at that later point, mirroring
    `SessionLedgerService.ensure_periodic_poll_schedule`'s precedent.
    """
    underlying = _FakeUnderlyingSchedulingPlugin()
    service = _make_real_scheduling_service(underlying)
    orchestrator = _FakeOrchestrator(service)
    plugin = _make_plugin(scheduling_service=None, orchestrator_ref=orchestrator)

    _check(
        plugin._scheduling_service is None,  # noqa: SLF001
        "scheduling_service really is None right after readiness (matches real ordering)",
    )
    setup_result = plugin.setup_schedules(params={}, state={"session_id": "sess-smoke"})
    _check(
        setup_result.get("action_status") == "error",
        "setup_schedules returns a visible error while scheduling_service is still unavailable "
        f"(got {setup_result.get('action_status')!r})",
    )
    _check(
        plugin.is_ready(),
        "prepare_for_readiness-equivalent state stays ready — readiness never required scheduling_service",
    )

    raised: BaseException | None = None
    try:
        plugin.ensure_schedules()
    except Exception as exc:  # noqa: BLE001 — smoke records the actual class
        raised = exc
    _check(
        raised is not None,
        f"ensure_schedules raises loud while scheduling_service is still unavailable (got {raised!r})",
    )

    orchestrator.advance_past_init_service_manager()
    ensure_result = plugin.ensure_schedules()
    expected_schedules = ["memorization_queue", "strength_recompute", "consolidation"]
    _check(
        ensure_result.get("schedules") == expected_schedules and len(underlying.calls) == 3,
        "ensure_schedules succeeds once scheduling_service resolves (lazy re-acquisition), "
        f"got {ensure_result!r} with {len(underlying.calls)} calls",
    )


def test_two_boots_leave_exactly_three_schedules_not_six() -> None:
    """Cross-boot idempotency (review B1): a restart must not duplicate crons.

    The real scheduler persists schedules and restores ALL of them at every
    boot; `create_cron_schedule` never dedupes on its own. A fresh
    `ACTRMemoryPlugin` process each boot (fresh `_schedules_configured`)
    against the SAME persisted store (the always-running scheduler +
    its DB) must still end up with exactly 3 schedules after two boots,
    not 6 — proving the clear-by-tag-before-create fix actually works,
    not just that it was called.
    """
    underlying = _FakeUnderlyingSchedulingPlugin()

    # Boot 1.
    service_1 = _make_real_scheduling_service(underlying)
    plugin_1 = _make_plugin(scheduling_service=service_1)
    result_1 = plugin_1.setup_schedules(params={}, state={"session_id": "sess-boot-1"})
    _check(
        result_1.get("action_status") == "completed" and len(underlying.store) == 3,
        f"boot 1 creates exactly 3 schedules (got {len(underlying.store)})",
    )

    # Boot 2: a brand-new plugin instance (fresh _schedules_configured),
    # same underlying store — models a real process restart against the
    # scheduler's persisted, restored schedules.
    service_2 = _make_real_scheduling_service(underlying)
    plugin_2 = _make_plugin(scheduling_service=service_2)
    result_2 = plugin_2.setup_schedules(params={}, state={"session_id": "sess-boot-2"})
    _check(
        result_2.get("action_status") == "completed" and len(underlying.store) == 3,
        f"boot 2 still has exactly 3 schedules, not 6 (got {len(underlying.store)})",
    )
    _check(
        len(underlying.calls) == 6,
        f"6 create_cron_schedule calls total across both boots (got {len(underlying.calls)}) "
        "-- the clear happens before create, not instead of it",
    )


def main() -> int:
    print("=== setup_schedules_real_scheduling_service_smoke (iss_2250e07c) ===")
    test_setup_schedules_drives_the_real_wrapper_and_creates_three_schedules()
    test_each_schedule_carries_a_distinct_system_owned_flow_id()
    test_setup_failure_is_visible_not_swallowed()
    test_two_boots_leave_exactly_three_schedules_not_six()
    test_startup_ordering_lazy_resolution_and_ensure_schedules()
    print(f"\n{_passed} passed, {len(_failed)} failed")
    if _failed:
        for label in _failed:
            print(f"  FAILED: {label}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
