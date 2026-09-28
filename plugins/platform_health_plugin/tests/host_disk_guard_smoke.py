#!/usr/bin/env python3
"""RED/GREEN smoke for host_disk_guard.py (no pytest, no real disk fill).

Every measurement is an injected fake ``disk_usage`` reader — this smoke
never touches the real filesystem's free space, per the kickoff's own
"don't fill a disk" instruction. Delivery and durable state are exercised
against fixture doubles, never a real peer-messaging bridge or database.

The cron's fire path runs through the REAL platform classes: the plugin's
registry entry is built by ``PluginProcessScanner`` plus the KB overlay
merge, the fire by ``ActionExecutor``, and the submission by
``ActionFactory``. A control points the same pipeline at the EDGE verb and
shows the inference error processor attaching. Whole-tree gate C5.1 is run
on the real module, with a retargeted control that it flags. The plugin's
lifecycle runs against a real ``EventOrchestrator`` in its state at plugin
startup, where ``scheduling_service`` does not exist yet.

Run:
    .venv/bin/python3 plugins/platform_health_plugin/tests/host_disk_guard_smoke.py
"""

from __future__ import annotations

import ast
import functools
import logging
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[3]
for _entry in (
    REPO_ROOT,
    REPO_ROOT / "ananta" / "src",
    REPO_ROOT / "plugins" / "platform_health_plugin" / "src",
    REPO_ROOT / "plugins" / "default_scheduling_plugin" / "src",
):
    if str(_entry) not in sys.path:
        sys.path.insert(0, str(_entry))

import platform_health_plugin.plugin as plugin_module  # noqa: E402
from ananta.core.actions.action_factory import ActionFactory  # noqa: E402
from ananta.core.domain.enums import ProcessorPolicyCategory  # noqa: E402
from ananta.core.event_orchestrator import EventOrchestrator  # noqa: E402
from ananta.core.plugins.protocols import LifecycleManaged  # noqa: E402
from ananta.core.process_registry.invocation_schema_generator import InvocationSchemaGenerator  # noqa: E402
from ananta.core.process_registry.kb_overlay_loader import KnowledgeBaseOverlayLoader  # noqa: E402
from ananta.core.process_registry.plugin_process_scanner import PluginProcessScanner  # noqa: E402
from ananta.core.process_registry.plugin_registration_validator import PluginRegistrationValidator  # noqa: E402
from ananta.core.process_registry.service_interface_metadata_generator import (  # noqa: E402
    ServiceInterfaceMetadataGenerator,
)
from default_scheduling_plugin.execution.action_executor import ActionExecutor  # noqa: E402
from default_scheduling_plugin.factories.schedule_factory import ScheduleFactory  # noqa: E402
from default_scheduling_plugin.models import ActionData  # noqa: E402
from default_scheduling_plugin.validation import validate_cron_action_def  # noqa: E402
from platform_health_plugin.constants import PLUGIN_NAME  # noqa: E402
from platform_health_plugin.host_disk_guard import (  # noqa: E402
    CHECK_CRON_PROCESS_KEY,
    CHECK_PROCESS_KEY,
    STATUS_CRITICAL,
    STATUS_OK,
    STATUS_WARNING,
    HostDiskGuardConfig,
    build_alert_text,
    check_host_disk_headroom,
    classify_headroom,
    ensure_host_disk_guard_schedule,
    measure_free_bytes,
)
from platform_health_plugin.plugin import PlatformHealthPlugin  # noqa: E402

from quality_gates import whole_tree_integration_gate as whole_tree_gate  # noqa: E402

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


GIB = 1024**3


# ─── Fixtures ────────────────────────────────────────────────────────────────


class _FakeUsage:
    def __init__(self, free: int) -> None:
        self.free = free


def _reader(free_bytes: int):
    def _read(path: str) -> _FakeUsage:
        assert isinstance(path, str)
        return _FakeUsage(free_bytes)

    return _read


def _raising_reader(exc: Exception):
    def _read(path: str) -> _FakeUsage:
        raise exc

    return _read


class _FixtureAgentMessagingPlugin:
    """``delivery`` selects which of ``peer_send_by_name``'s real outcomes
    this fixture reproduces: a live wake, the durable-but-nobody-live
    ``queued_for_replay`` case, or a role-vacant dispatch-time failure."""

    def __init__(self, *, delivery: str = "queued_wake") -> None:
        self.calls: list[dict[str, Any]] = []
        self.states: list[dict[str, Any]] = []
        self._delivery = delivery

    def peer_send_by_name(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        self.calls.append(params)
        self.states.append(state)
        if self._delivery == "peer_role_vacant":
            return {
                "action_status": "failed",
                "data": {},
                "error": {"code": "peer_role_vacant", "message": f"role {params['name']!r} has no binding"},
            }
        return {"action_status": "completed", "data": {"delivery": self._delivery}}


class _FixtureSchedulingService:
    """Mirrors the REAL ``ananta.services.scheduling_service.SchedulingService``
    keyword-argument seam exactly (``cron_expression``, ``action_definitions``,
    ``label``, ``tags``, ``state`` as named kwargs; ``tag``/``state`` for the
    lookup) -- not the raw plugin's ``(params, state)`` wrapper, and not a
    looser ``**kwargs`` catch-all that would silently accept a call the real
    service would reject. A fake looser than the real seam cancels out real
    defects (this codebase's own standing lesson)."""

    def __init__(self, *, existing_schedules: list[dict[str, Any]] | None = None, fail_lookup: bool = False) -> None:
        self._existing = existing_schedules or []
        self._fail_lookup = fail_lookup
        self.create_calls: list[dict[str, Any]] = []
        self.get_schedules_by_tag_calls: list[dict[str, Any]] = []

    def get_schedules_by_tag(self, tag: str, state: dict[str, Any] | None = None) -> dict[str, Any]:
        self.get_schedules_by_tag_calls.append({"tag": tag, "state": state})
        if self._fail_lookup:
            return {"action_status": "error", "data": {}, "error": {"message": "state backend unavailable"}}
        matching = [s for s in self._existing if tag in s.get("tags", [])]
        return {"action_status": "completed", "data": {"schedules": matching, "count": len(matching), "tag": tag}}

    def create_cron_schedule(
        self,
        cron_expression: str,
        actions: list[dict[str, Any]] | None = None,
        action_definitions: list[dict[str, Any]] | None = None,
        memory_tag: str | None = None,
        label: str | None = None,
        tags: list[str] | str | None = None,
        state: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        self.create_calls.append({
            "cron_expression": cron_expression,
            "actions": actions,
            "action_definitions": action_definitions,
            "memory_tag": memory_tag,
            "label": label,
            "tags": tags,
            "state": state,
        })
        schedule_id = f"sch_smoke_{len(self.create_calls)}"
        # Persist like the real scheduler, so a later get_schedules_by_tag finds it.
        self._existing.append({"schedule_id": schedule_id, "tags": [tags] if isinstance(tags, str) else list(tags or [])})
        return {"action_status": "completed", "data": {"schedule_id": schedule_id, "message": "created"}}


class _FixturePluginManager:
    def __init__(self, plugins: dict[str, object]) -> None:
        self._plugins = plugins

    def get_plugin(self, plugin_name: str) -> object:
        return self._plugins.get(plugin_name)


class _FixtureStateService:
    """In-memory double for the ``get_key_value``/``set_key_value`` corner of
    ``StateManagementInterface`` — the same ``{namespace, key, scope}`` ->
    value shape and the same ``ActionResult`` envelope the real postgres
    provider returns (``key_value_ops.py``'s ``kv_get``/``kv_set``)."""

    def __init__(self) -> None:
        self._store: dict[tuple[str, str, str], str] = {}
        self.get_calls = 0
        self.set_calls = 0

    def get_key_value(self, namespace: str, key: str, scope: str = "GLOBAL") -> dict[str, Any]:
        self.get_calls += 1
        value = self._store.get((namespace, key, scope))
        if value is None:
            return {"action_status": "completed", "data": {"namespace": namespace, "key": key, "scope": scope, "value": None, "found": False}}
        return {"action_status": "completed", "data": {"namespace": namespace, "key": key, "scope": scope, "value": value, "found": True}}

    def set_key_value(self, namespace: str, key: str, value: object, scope: str = "GLOBAL", ttl: int | None = None) -> dict[str, Any]:
        self.set_calls += 1
        self._store[(namespace, key, scope)] = str(value)
        return {"action_status": "completed", "data": {"namespace": namespace, "key": key, "scope": scope}}


class _FailingStateService:
    def get_key_value(self, namespace: str, key: str, scope: str = "GLOBAL") -> dict[str, Any]:
        return {"action_status": "failed", "data": {}, "error": {"message": "postgres unavailable"}}

    def set_key_value(self, namespace: str, key: str, value: object, scope: str = "GLOBAL", ttl: int | None = None) -> dict[str, Any]:
        return {"action_status": "failed", "data": {}, "error": {"message": "postgres unavailable"}}


class _FixtureOrchestrator:
    """``ensure_host_disk_guard_schedule`` no longer takes an orchestrator at
    all (it takes the ``scheduling_service`` object directly, see the
    module docstring on why); this fixture is now check_host_disk_headroom-
    only, and only carries ``agent_messaging_plugin`` in ``plugin_manager``
    (the alert dispatch path)."""

    def __init__(
        self,
        agent_messaging: _FixtureAgentMessagingPlugin,
        state_service: object | None = None,
    ) -> None:
        self.plugin_manager = _FixturePluginManager({
            "agent_messaging_plugin": agent_messaging,
        })
        self._state_service = state_service if state_service is not None else _FixtureStateService()

    def get_service(self, name: str) -> object | None:
        return self._state_service if name == "state_service" else None


# ─── classify_headroom: exact-byte boundaries + mutant kill ─────────────────


def test_classify_headroom_exact_byte_boundaries() -> None:
    warn, crit = 100 * GIB, 50 * GIB
    _check(
        classify_headroom(crit - 1, warning_floor_bytes=warn, critical_floor_bytes=crit) == STATUS_CRITICAL,
        "critical_floor_bytes - 1 byte -> critical",
    )
    _check(
        classify_headroom(crit, warning_floor_bytes=warn, critical_floor_bytes=crit) == STATUS_WARNING,
        "exactly critical_floor_bytes -> warning, NOT critical (critical is a strict <)",
    )
    _check(
        classify_headroom(warn - 1, warning_floor_bytes=warn, critical_floor_bytes=crit) == STATUS_WARNING,
        "warning_floor_bytes - 1 byte -> warning",
    )
    _check(
        classify_headroom(warn, warning_floor_bytes=warn, critical_floor_bytes=crit) == STATUS_OK,
        "exactly warning_floor_bytes -> ok, NOT warning (warning is a strict <)",
    )


def _mutant_classify_with_le(free_bytes: int, *, warning_floor_bytes: int, critical_floor_bytes: int) -> str:
    """A deliberately broken sibling: '<=' where the real function uses '<'.
    Exists only so the boundary tests below can show they actually
    distinguish the real implementation from this mutant, not just assert a
    value that happens to match by coincidence."""
    if free_bytes <= critical_floor_bytes:
        return STATUS_CRITICAL
    if free_bytes <= warning_floor_bytes:
        return STATUS_WARNING
    return STATUS_OK


def test_le_mutant_dies_at_every_boundary() -> None:
    warn, crit = 100 * GIB, 50 * GIB
    for free_bytes, label in ((crit, "critical_floor_bytes"), (warn, "warning_floor_bytes")):
        real = classify_headroom(free_bytes, warning_floor_bytes=warn, critical_floor_bytes=crit)
        mutant = _mutant_classify_with_le(free_bytes, warning_floor_bytes=warn, critical_floor_bytes=crit)
        _check(
            real != mutant,
            f"at exactly {label}, real classify_headroom ({real!r}) disagrees with the "
            f"'<=' mutant ({mutant!r}) -- the boundary assertions above are load-bearing, "
            "not tautological",
        )


def test_config_rejects_inverted_floors() -> None:
    try:
        HostDiskGuardConfig(warning_floor_bytes=10 * GIB, critical_floor_bytes=20 * GIB)
    except ValueError as exc:
        _check("must not exceed" in str(exc), "inverted floors refused loudly at construction")
    else:
        _check(False, "inverted floors refused loudly at construction")


def test_measure_free_bytes_propagates_failure() -> None:
    """A check that cannot measure must fail loudly (the kickoff's own
    requirement) — never swallow the error and report a false 'ok'."""
    try:
        measure_free_bytes("/Users", disk_usage=_raising_reader(OSError("statvfs failed")))
    except OSError as exc:
        _check("statvfs failed" in str(exc), "measurement failure propagates unchanged")
    else:
        _check(False, "measurement failure propagates unchanged")


def test_measure_free_bytes_happy_path() -> None:
    _check(measure_free_bytes("/Users", disk_usage=_reader(42 * GIB)) == 42 * GIB, "measure_free_bytes reads the injected reader")


def test_build_alert_text_carries_fresh_measurement_no_stale_advice() -> None:
    cfg = HostDiskGuardConfig(warning_floor_bytes=100 * GIB, critical_floor_bytes=50 * GIB)
    text = build_alert_text(status=STATUS_CRITICAL, free_bytes=30 * GIB, path="/Users", config=cfg)
    _check("CRITICAL" in text, "alert text names the status")
    _check("30.0 GiB" in text, "alert text carries the measured value")
    _check("50.0 GiB" in text, "alert text carries the floor it was compared against")
    _check("measured just now" in text, "alert text marks itself as a fresh measurement, not a cached one")


# ─── check_host_disk_headroom: transitions, rate-limiting, delivery ─────────


def test_first_tick_ok_is_quiet_and_writes_no_alert() -> None:
    agent_messaging = _FixtureAgentMessagingPlugin()
    orch = _FixtureOrchestrator(agent_messaging)
    report = check_host_disk_headroom(
        orch,
        state={},
        config=HostDiskGuardConfig(warning_floor_bytes=100 * GIB, critical_floor_bytes=50 * GIB),
        disk_usage=_reader(500 * GIB),
    )
    _check(report["status"] == STATUS_OK, "500 GiB free classifies ok")
    _check(report["transitioned"] is False, "ok with no prior marker is NOT a transition (baseline is ok)")
    _check(report["alerted"] is False, "no transition -> no alert")
    _check(not agent_messaging.calls, "no transition -> no peer_send_by_name call")


def test_first_tick_warning_alerts_and_records_marker() -> None:
    agent_messaging = _FixtureAgentMessagingPlugin(delivery="queued_wake")
    orch = _FixtureOrchestrator(agent_messaging)
    report = check_host_disk_headroom(
        orch,
        state={},
        config=HostDiskGuardConfig(warning_floor_bytes=100 * GIB, critical_floor_bytes=50 * GIB, alert_role="Main-Seat"),
        disk_usage=_reader(80 * GIB),
    )
    _check(report["status"] == STATUS_WARNING, "80 GiB free classifies warning")
    _check(report["transitioned"] is True, "ok(implicit) -> warning is a transition")
    _check(report["alerted"] is True, "live-delivered transition alerts")
    _check(len(agent_messaging.calls) == 1, "exactly one peer_send_by_name call")
    _check(agent_messaging.calls[0]["name"] == "Main-Seat", "alert addressed to the configured role")
    _check("WARNING" in agent_messaging.calls[0]["content"], "alert content names the WARNING status")


def test_repeated_tick_same_status_does_not_realert() -> None:
    """The rate-limit: one alert per TRANSITION, not one per tick."""
    agent_messaging = _FixtureAgentMessagingPlugin(delivery="queued_wake")
    orch = _FixtureOrchestrator(agent_messaging)
    cfg = HostDiskGuardConfig(warning_floor_bytes=100 * GIB, critical_floor_bytes=50 * GIB, alert_role="Main-Seat")
    first = check_host_disk_headroom(orch, state={}, config=cfg, disk_usage=_reader(80 * GIB))
    second = check_host_disk_headroom(orch, state={}, config=cfg, disk_usage=_reader(79 * GIB))  # still warning
    third = check_host_disk_headroom(orch, state={}, config=cfg, disk_usage=_reader(75 * GIB))  # still warning
    _check(first["alerted"] is True, "first warning tick alerts")
    _check(second["transitioned"] is False and second["alerted"] is False, "second consecutive warning tick: no transition, no alert")
    _check(third["transitioned"] is False and third["alerted"] is False, "third consecutive warning tick: still no re-alert")
    _check(len(agent_messaging.calls) == 1, "exactly one peer_send_by_name call across three warning ticks")


def test_escalation_and_recovery_each_alert_once() -> None:
    agent_messaging = _FixtureAgentMessagingPlugin(delivery="queued_wake")
    orch = _FixtureOrchestrator(agent_messaging)
    cfg = HostDiskGuardConfig(warning_floor_bytes=100 * GIB, critical_floor_bytes=50 * GIB, alert_role="Main-Seat")
    to_warning = check_host_disk_headroom(orch, state={}, config=cfg, disk_usage=_reader(80 * GIB))
    to_critical = check_host_disk_headroom(orch, state={}, config=cfg, disk_usage=_reader(10 * GIB))
    still_critical = check_host_disk_headroom(orch, state={}, config=cfg, disk_usage=_reader(5 * GIB))
    recovered = check_host_disk_headroom(orch, state={}, config=cfg, disk_usage=_reader(500 * GIB))
    _check(to_warning["alerted"] is True and to_warning["status"] == STATUS_WARNING, "ok->warning alerts once")
    _check(to_critical["alerted"] is True and to_critical["status"] == STATUS_CRITICAL, "warning->critical alerts again (escalation)")
    _check(still_critical["alerted"] is False, "critical->critical (still falling) does not re-alert")
    _check(recovered["alerted"] is True and recovered["status"] == STATUS_OK, "critical->ok (recovery) alerts")
    _check(len(agent_messaging.calls) == 3, "exactly 3 alerts across 4 ticks: escalation, escalation, recovery")
    _check("RECOVER" not in agent_messaging.calls[-1]["content"].upper() or "OK" in agent_messaging.calls[-1]["content"], "recovery alert names the new (ok) status")


def test_undelivered_alert_fails_loud_and_does_not_advance_marker() -> None:
    agent_messaging = _FixtureAgentMessagingPlugin(delivery="queued_for_replay")
    orch = _FixtureOrchestrator(agent_messaging)
    cfg = HostDiskGuardConfig(warning_floor_bytes=100 * GIB, critical_floor_bytes=50 * GIB, alert_role="Main-Seat")
    first = check_host_disk_headroom(orch, state={}, config=cfg, disk_usage=_reader(80 * GIB))
    second = check_host_disk_headroom(orch, state={}, config=cfg, disk_usage=_reader(79 * GIB))  # still warning
    _check(first["transitioned"] is True, "ok->warning is still detected as a transition")
    _check(first["alerted"] is False, "queued_for_replay (no live holder) is NOT counted as alerted")
    _check(first["alert_reason"] is not None and "queued_for_replay" in first["alert_reason"], "alert_reason names the non-live delivery outcome")
    _check(second["transitioned"] is True, "marker was NOT advanced after the undelivered attempt -- next tick sees the SAME transition again")
    _check(len(agent_messaging.calls) == 2, "the undelivered transition is retried on the next tick, not silently dropped")


def test_role_vacant_dispatch_failure_fails_loud() -> None:
    agent_messaging = _FixtureAgentMessagingPlugin(delivery="peer_role_vacant")
    orch = _FixtureOrchestrator(agent_messaging)
    report = check_host_disk_headroom(
        orch,
        state={},
        config=HostDiskGuardConfig(warning_floor_bytes=100 * GIB, critical_floor_bytes=50 * GIB, alert_role="Nobody-Holds-This"),
        disk_usage=_reader(10 * GIB),
    )
    _check(report["alerted"] is False, "a vacant role (dispatch-time failure, not even queued) is not alerted")
    _check(report["alert_reason"] is not None and "peer_role_vacant" in report["alert_reason"], "alert_reason surfaces the vacant-role code")


def test_unconfigured_alert_role_measures_but_cannot_alert() -> None:
    """No baked-in identity default (see the module docstring): an unset
    alert_role still measures and classifies correctly, but there is
    nobody to tell -- and that must be visible, not silent."""
    agent_messaging = _FixtureAgentMessagingPlugin(delivery="queued_wake")
    orch = _FixtureOrchestrator(agent_messaging)
    cfg = HostDiskGuardConfig(warning_floor_bytes=100 * GIB, critical_floor_bytes=50 * GIB, alert_role=None)
    first = check_host_disk_headroom(orch, state={}, config=cfg, disk_usage=_reader(80 * GIB))
    _check(first["status"] == STATUS_WARNING, "measurement and classification are unaffected by a missing alert target")
    _check(first["transitioned"] is True, "the transition is still detected")
    _check(first["alerted"] is False, "unconfigured target -> not alerted")
    _check(first["alert_reason"] == "alert_target_unconfigured", "alert_reason names the specific unconfigured-target case")
    _check(not agent_messaging.calls, "no peer_send_by_name call is attempted -- there is no target to send to")
    second = check_host_disk_headroom(orch, state={}, config=cfg, disk_usage=_reader(79 * GIB))  # still warning
    _check(second["transitioned"] is True, "marker not advanced while unconfigured -- retried every tick, same as an undelivered alert")


def test_state_service_failure_propagates_loud() -> None:
    agent_messaging = _FixtureAgentMessagingPlugin()
    orch = _FixtureOrchestrator(agent_messaging, state_service=_FailingStateService())
    try:
        check_host_disk_headroom(orch, state={}, config=HostDiskGuardConfig(), disk_usage=_reader(500 * GIB))
    except Exception as exc:  # noqa: BLE001 -- asserting ANY loud failure, not a specific type
        _check("postgres unavailable" in str(exc), "a state_service failure propagates loudly rather than defaulting to a guess")
    else:
        _check(False, "a state_service failure propagates loudly rather than defaulting to a guess")


def test_different_paths_have_independent_markers() -> None:
    agent_messaging = _FixtureAgentMessagingPlugin(delivery="queued_wake")
    state = _FixtureStateService()
    orch = _FixtureOrchestrator(agent_messaging, state_service=state)
    check_host_disk_headroom(orch, state={}, config=HostDiskGuardConfig(path="/Users", warning_floor_bytes=100 * GIB, critical_floor_bytes=50 * GIB, alert_role="Main-Seat"), disk_usage=_reader(80 * GIB))
    other = check_host_disk_headroom(orch, state={}, config=HostDiskGuardConfig(path="/Volumes/Other", warning_floor_bytes=100 * GIB, critical_floor_bytes=50 * GIB, alert_role="Main-Seat"), disk_usage=_reader(80 * GIB))
    _check(other["transitioned"] is True, "a different path's marker is independent -- its own first warning tick still alerts")
    _check(len(agent_messaging.calls) == 2, "two distinct paths produce two distinct alerts")


def test_alert_carries_the_received_state() -> None:
    """The alert hands peer_send_by_name the calling verb's own ``state``,
    unchanged, so its sender ladder can stamp a manual caller's identity."""
    agent_messaging = _FixtureAgentMessagingPlugin(delivery="queued_wake")
    orch = _FixtureOrchestrator(agent_messaging)
    received = {"caller_attribution_role": "Some-Role", "flow_id": "flow-x"}
    check_host_disk_headroom(
        orch,
        state=received,
        config=HostDiskGuardConfig(warning_floor_bytes=100 * GIB, critical_floor_bytes=50 * GIB, alert_role="Main-Seat"),
        disk_usage=_reader(80 * GIB),
    )
    _check(agent_messaging.states == [received], "peer_send_by_name receives the verb's own state unchanged, not {}")


# ─── ensure_host_disk_guard_schedule ─────────────────────────────────────────


def test_ensure_schedule_creates_when_absent() -> None:
    scheduling = _FixtureSchedulingService(existing_schedules=[])
    result = ensure_host_disk_guard_schedule(scheduling, alert_role="Main-Seat", cron_expression="*/10 * * * *", tag="host_disk_guard")
    _check(result["status"] == "created", "no existing schedule -> created")
    _check(len(scheduling.create_calls) == 1, "create_cron_schedule called exactly once")
    call = scheduling.create_calls[0]
    _check(call["action_definitions"] is None, "the cron is registered through the literal actions= list, not action_definitions=")
    _check(
        call["actions"] == [{"process_key": CHECK_CRON_PROCESS_KEY, "arguments": {}}],
        "cron targets the EDGE_SINK cron sibling with empty arguments and no result_processor_kind",
    )
    schedule_state = call["state"]
    _check(
        schedule_state is not None and schedule_state.get("flow_id") == "flow-host-disk-guard",
        "create_cron_schedule carries the system-owned flow_id the schedule row stores and every fire is stamped with",
    )
    _check(
        schedule_state is not None and schedule_state.get("session_id") == "sess-host-disk-guard",
        "state also carries the system-owned session_id",
    )


def test_ensure_schedule_is_idempotent() -> None:
    scheduling = _FixtureSchedulingService(existing_schedules=[{"schedule_id": "sch_existing", "tags": ["host_disk_guard"]}])
    result = ensure_host_disk_guard_schedule(scheduling, alert_role="Main-Seat", tag="host_disk_guard")
    _check(result["status"] == "already_present", "existing schedule under the tag -> already_present")
    _check(len(scheduling.create_calls) == 0, "no duplicate create_cron_schedule call")
    _check(len(scheduling.get_schedules_by_tag_calls) == 1, "get_schedules_by_tag called through the real service seam (tag= kwarg, not params={'tag': ...})")


def test_ensure_schedule_lookup_failure_raises_loudly() -> None:
    scheduling = _FixtureSchedulingService(fail_lookup=True)
    try:
        ensure_host_disk_guard_schedule(scheduling, alert_role="Main-Seat", tag="host_disk_guard")
    except RuntimeError as exc:
        _check("state backend unavailable" in str(exc), "a scheduling-service error response raises loudly, not silently")
    else:
        _check(False, "a scheduling-service error response raises loudly, not silently")


def test_ensure_schedule_refuses_when_alert_role_unset() -> None:
    """No baked-in identity default: a cron with nobody to alert would only
    ever produce alert_target_unconfigured, forever -- refuse instead of
    installing it."""
    scheduling = _FixtureSchedulingService(existing_schedules=[])
    result = ensure_host_disk_guard_schedule(scheduling, alert_role=None, tag="host_disk_guard")
    _check(result["status"] == "refused", "unset alert_role -> refused, not installed")
    _check(result.get("reason") == "alert_target_unconfigured", "refusal names the reason")
    _check(scheduling.create_calls == [], "no create_cron_schedule call")
    _check(scheduling.get_schedules_by_tag_calls == [], "no get_schedules_by_tag call either -- the refusal is entirely local")

    empty_string_result = ensure_host_disk_guard_schedule(scheduling, alert_role="", tag="host_disk_guard")
    _check(empty_string_result["status"] == "refused", "empty-string alert_role also refuses (falsy, not just None)")


# ─── The cron's fire path, through the REAL platform classes ────────────────

_PROCESS_ERROR_KEY = "service_interface::inference_service::process_error"


class _PluginRegistryView:
    """The one attribute the KB overlay loader reads off a plugin manager."""

    def __init__(self, plugins: dict[str, object]) -> None:
        self.plugins = plugins


class _RecordingActionEventRecorder:
    def __init__(self) -> None:
        self.stored: list[dict[str, object]] = []

    def store_action_event(self, action: dict[str, object]) -> str:
        self.stored.append(action)
        return f"ae-smoke-{len(self.stored)}"


class _SuffixStateService:
    def generate_unique_string(self, length: int, encoding: str) -> dict[str, object]:  # noqa: ARG002 — ActionFactory's StateService protocol
        return {"action_status": "completed", "data": {"random_string": "s" * length}}


class _RecordingTemplateEngine:
    """Records every resolution and returns the definition unchanged. The
    real factory calls it only when the definition carries a template
    pattern, which an attached inference processor's prompt does."""

    def __init__(self) -> None:
        self.calls = 0

    def resolve_templates(self, action_def: dict[str, object], context: dict[str, object]) -> dict[str, object]:  # noqa: ARG002 — ActionFactory's TemplateEngine protocol
        self.calls += 1
        return action_def


def _real_registry() -> dict[str, object]:
    """The registry entries production builds for this plugin: the real
    ``PluginProcessScanner`` over the real plugin's ``@platform_process``
    metadata (EdgeProcessProvider validation included), then the real KB
    overlay merge of every shipped process JSON (which hard-fails on a
    missing one). The inference ``process_error`` entry is added after, so a
    verb that declares error customizations has a base template to attach --
    the attachment the control proves happens."""
    plugin = PlatformHealthPlugin()
    registry: dict[str, object] = {"processes": {}}
    schema_generator = InvocationSchemaGenerator()
    scanner = PluginProcessScanner(
        plugin_manager=None,  # type: ignore[arg-type] — unused by the per-plugin registration path
        validator=PluginRegistrationValidator(),
        metadata_generator=ServiceInterfaceMetadataGenerator(),
        schema_generator=schema_generator,
    )
    scanner._process_individual_plugin(PLUGIN_NAME, plugin, registry)  # noqa: SLF001 — the real per-plugin registration step
    KnowledgeBaseOverlayLoader(
        plugin_manager=_PluginRegistryView({PLUGIN_NAME: plugin}),  # type: ignore[arg-type] — only .plugins is read
        schema_generator=schema_generator,
    ).apply(registry)
    processes = registry["processes"]
    assert isinstance(processes, dict)
    processes[_PROCESS_ERROR_KEY] = {"action_definition_template": {"arguments": {"params": {}}}}
    return registry


def _fire(
    registry: dict[str, object], action: ActionData, schedule_state: dict[str, Any],
) -> tuple[dict[str, Any], _RecordingActionEventRecorder, tuple[bool, str | None], _RecordingTemplateEngine]:
    """One scheduler fire: the real ``ActionExecutor`` builds the definition
    and submits it to the real ``ActionFactory``, whose runtime action lands
    in the recorder."""
    recorder = _RecordingActionEventRecorder()
    template_engine = _RecordingTemplateEngine()
    factory = ActionFactory(
        process_registry=registry,
        template_engine=template_engine,  # type: ignore[arg-type]
        state_service=_SuffixStateService(),
        action_event_recorder=recorder,
    )
    executor = ActionExecutor(action_factory=factory, logger=logging.getLogger("host_disk_guard_smoke"))  # type: ignore[arg-type]
    session_id = schedule_state.get("session_id")
    flow_id = schedule_state.get("flow_id")
    built = executor._build_action_definition(action, session_id, flow_id)  # noqa: SLF001 — the real fire-time builder
    outcome = executor._execute_single_action(  # noqa: SLF001 — build + submit, exactly as a fire does
        action=action, schedule_id="sch_smoke", session_id=session_id, flow_id=flow_id,
        action_index=1, total_actions=1,
    )
    return built, recorder, outcome, template_engine


def _installed_cron() -> tuple[list[ActionData], dict[str, Any]]:
    scheduling = _FixtureSchedulingService()
    ensure_host_disk_guard_schedule(scheduling, alert_role="Main-Seat")
    call = scheduling.create_calls[0]
    actions, _, _ = ScheduleFactory.parse_actions_from_params({"actions": call["actions"]})
    return actions, call["state"]


def test_cron_entry_parses_terminal_and_registers_as_edge_sink() -> None:
    actions, _ = _installed_cron()
    _check(
        len(actions) == 1 and actions[0].result_processor_kind is None and actions[0].result_processor is None,
        "the scheduling plugin's own parser keeps the cron entry terminal",
    )
    try:
        validate_cron_action_def(actions[0])
        _check(True, "the scheduling plugin's cron-action validator accepts the entry")
    except ValueError as exc:
        _check(False, f"the scheduling plugin's cron-action validator accepts the entry ({exc})")
    registry = _real_registry()
    processes = registry["processes"]
    assert isinstance(processes, dict)
    entry = processes[CHECK_CRON_PROCESS_KEY]
    _check(
        entry["processor_policy_category"] == ProcessorPolicyCategory.EDGE_SINK and entry["is_discoverable"] is False,
        "registry entry after the KB merge: EDGE_SINK and not discoverable",
    )
    _check(
        "error_processor_customizations" not in entry and "result_processor_customizations" not in entry,
        "registry entry after the KB merge declares no result or error processor customizations",
    )


def test_cron_fire_attaches_no_processor_and_stamps_flow_id() -> None:
    actions, schedule_state = _installed_cron()
    built, recorder, outcome, template_engine = _fire(_real_registry(), actions[0], schedule_state)
    _check(
        built.get("flow_id") == "flow-host-disk-guard" and built.get("session_id") == "sess-host-disk-guard",
        "ActionExecutor._build_action_definition stamps the schedule's flow_id and session_id",
    )
    _check("result_processor_kind" not in built, "the built definition carries no result_processor_kind")
    _check(outcome == (True, None) and len(recorder.stored) == 1, "the real ActionFactory accepts the fired definition")
    stored = recorder.stored[0] if recorder.stored else {}
    _check(stored.get("process_key") == CHECK_CRON_PROCESS_KEY, "the stored runtime action targets the cron sibling")
    _check(
        "error_processor" not in stored,
        "no error_processor attached: a failed fire stays terminal instead of routing into inference process_error",
    )
    _check(
        "result_processor" not in stored and stored.get("result_processor_kind") is None,
        "no result processor attached: a successful fire rides EDGE_SINK_SKIP",
    )
    _check(stored.get("flow_id") == "flow-host-disk-guard", "the stored runtime action carries the stamped flow_id")
    _check(template_engine.calls == 0, "no template resolution ran: nothing in the fired action reads a flow record")


def test_control_edge_verb_as_cron_target_gets_the_inference_error_processor() -> None:
    """Control for the test above: the same pipeline, pointed at the
    discoverable EDGE verb the Round 6 cron targeted, attaches the inference
    ``process_error`` processor, whose prompt reads the flow's input -- a
    flow record the system-owned cron flow does not have. The
    no-error_processor assertion can fail."""
    _, schedule_state = _installed_cron()
    _, recorder, outcome, template_engine = _fire(
        _real_registry(), ActionData(name=CHECK_PROCESS_KEY, parameters={}), schedule_state,
    )
    stored = recorder.stored[0] if recorder.stored else {}
    error_processor = stored.get("error_processor")
    _check(
        outcome == (True, None) and isinstance(error_processor, dict) and error_processor.get("process_key") == _PROCESS_ERROR_KEY,
        "control: an EDGE cron target gets inference process_error attached (the Round 6 defect reproduces)",
    )
    _check(
        template_engine.calls == 1 and "get_flow_input_for_presentation" in str(error_processor),
        "control: the attached error processor carries the flow-input macro, so template resolution runs",
    )


def test_fire_without_flow_id_is_refused() -> None:
    """The same fire with no schedule-level flow_id is refused by the real
    ActionFactory before anything is stored."""
    actions, _ = _installed_cron()
    _, recorder, outcome, _ = _fire(_real_registry(), actions[0], {})
    _check(
        outcome[0] is False and outcome[1] is not None and "flow_id" in outcome[1] and not recorder.stored,
        "a flow_id-less fire is refused, citing flow_id, and nothing is stored",
    )


# ─── Whole-tree gate C5.1 sees the literal ──────────────────────────────────


def test_whole_tree_gate_c51_sees_and_exempts_the_literal() -> None:
    """C5.1 reads only ``actions=`` list literals. Run it on the real module
    (no finding), then on the same source with the literal retargeted at
    the EDGE verb (one finding): the control proves the gate sees this call,
    so the clean result is an exemption, not a blind spot."""
    plugins = {surface.name: surface for surface in whole_tree_gate._discover_plugin_surfaces()}  # noqa: SLF001
    source_path = REPO_ROOT / "plugins" / "platform_health_plugin" / "src" / "platform_health_plugin" / "host_disk_guard.py"
    source = source_path.read_text(encoding="utf-8")
    findings = whole_tree_gate._check_scheduling_in_module(ast.parse(source), source_path, {}, plugins)  # noqa: SLF001
    _check(findings == [], "C5.1: no finding on host_disk_guard.py (EDGE_SINK target exempted)")
    control_source = source.replace(f'"{CHECK_CRON_PROCESS_KEY}"', f'"{CHECK_PROCESS_KEY}"')
    _check(control_source != source, "control: the cron literal is present in the source and was retargeted")
    control = whole_tree_gate._check_scheduling_in_module(ast.parse(control_source), source_path, {}, plugins)  # noqa: SLF001
    _check(
        len(control) == 1 and control[0].check_id == "C5.1",
        "control: the same literal aimed at the EDGE verb is flagged by C5.1",
    )


# ─── PlatformHealthPlugin on the REAL startup ordering ──────────────────────
#
# Startup runs every plugin's prepare_for_readiness and start_services at its
# start_service_plugins step, and creates orch.service_manager (the only home
# of scheduling_service) later, at init_service_manager
# (startup_sequence.py, the one ``orch.service_manager = ServiceManager(...)``
# assignment). These tests use a REAL EventOrchestrator whose __init__ has not
# run -- its state at start_service_plugins -- so get_service is the real
# routing: scheduling_service is None until _run_init_service_manager.


class _FakeConfigManager:
    def __init__(self, config: dict[str, object]) -> None:
        self._config = config

    def get_plugin_config(self, plugin_name: str, default_config: dict[str, object] | None = None) -> dict[str, object]:  # noqa: ARG002
        return {**(default_config or {}), **self._config}


class _LogCapture(logging.Handler):
    def __init__(self) -> None:
        super().__init__(level=logging.ERROR)
        self.messages: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.messages.append(record.getMessage())


def _orchestrator_at_plugin_startup(agent_messaging: _FixtureAgentMessagingPlugin) -> EventOrchestrator:
    """A real EventOrchestrator as it stands at start_service_plugins: the
    plugin manager and the direct-attribute state service exist, and there
    is no service_manager yet."""
    orch = EventOrchestrator.__new__(EventOrchestrator)
    orch.plugin_manager = _FixturePluginManager({"agent_messaging_plugin": agent_messaging})  # type: ignore[assignment]
    orch.state_service = _FixtureStateService()  # type: ignore[assignment]
    return orch


def _run_init_service_manager(orch: EventOrchestrator, scheduling: _FixtureSchedulingService) -> None:
    """What startup's init_service_manager step does for this seam: assign
    orch.service_manager, which carries scheduling_service."""
    orch.service_manager = SimpleNamespace(scheduling_service=scheduling)  # type: ignore[assignment]


def _plugin(
    config: dict[str, object],
    agent_messaging: _FixtureAgentMessagingPlugin,
    disk_usage: Any,
) -> tuple[PlatformHealthPlugin, EventOrchestrator]:
    plugin_module.get_config = lambda: _FakeConfigManager(config)  # type: ignore[assignment]
    plugin_module._check_host_disk_headroom = functools.partial(check_host_disk_headroom, disk_usage=disk_usage)  # type: ignore[assignment]  # noqa: SLF001 — no real disk read
    orch = _orchestrator_at_plugin_startup(agent_messaging)
    plugin = PlatformHealthPlugin()
    plugin.orchestrator_ref = orch
    plugin.prepare_for_readiness()
    plugin.start_services()
    return plugin, orch


def _wait_for_check(plugin: PlatformHealthPlugin) -> bool:
    deadline = time.monotonic() + 10
    while plugin._check_lock.locked() and time.monotonic() < deadline:  # noqa: SLF001
        time.sleep(0.02)
    return not plugin._check_lock.locked()  # noqa: SLF001


def test_plugin_comes_up_before_scheduling_service_exists() -> None:
    """B1: the plugin reaches ready and running with scheduling_service None;
    the install fails loudly before init_service_manager and succeeds, once,
    through the verb after it."""
    scheduling = _FixtureSchedulingService()
    orch = _orchestrator_at_plugin_startup(_FixtureAgentMessagingPlugin())
    _check(orch.get_service("scheduling_service") is None, "real seam: scheduling_service is None at plugin startup")
    plugin_module.get_config = lambda: _FakeConfigManager({"alert_role": "Main-Seat"})  # type: ignore[assignment]
    plugin = PlatformHealthPlugin()
    plugin.orchestrator_ref = orch
    _check(isinstance(plugin, LifecycleManaged), "the plugin is LifecycleManaged, so startup calls both lifecycle methods")
    try:
        plugin.prepare_for_readiness()
        plugin.start_services()
        _check(plugin.is_ready() and plugin.is_running(), "ready and running with scheduling_service None")
    except Exception as exc:  # noqa: BLE001 — any raise here is the B1 startup failure
        _check(False, f"ready and running with scheduling_service None ({type(exc).__name__}: {exc})")
    _check(scheduling.create_calls == [] and scheduling.get_schedules_by_tag_calls == [], "no lifecycle method touches the scheduler")
    try:
        plugin.ensure_host_disk_guard_schedule({}, {})
    except RuntimeError as exc:
        _check("init_service_manager" in str(exc), "the install before init_service_manager raises loudly, naming the step")
    else:
        _check(False, "the install before init_service_manager raises loudly, naming the step")
    _run_init_service_manager(orch, scheduling)
    first = plugin.ensure_host_disk_guard_schedule({}, {})
    second = plugin.ensure_host_disk_guard_schedule({}, {})
    _check(first["data"]["status"] == "created" and second["data"]["status"] == "already_present", "after init_service_manager the verb installs, then finds its own schedule")
    _check(
        len(scheduling.create_calls) == 1 and scheduling.create_calls[0]["actions"] == [{"process_key": CHECK_CRON_PROCESS_KEY, "arguments": {}}],
        "exactly one cron, targeting the EDGE_SINK sibling",
    )
    plugin.stop_services()
    _check(not plugin.is_running(), "stop_services flips is_running")


_STARTING_ACTION = {
    # The entry a deployment adds to its runtime
    # config/starting_action_definitions.json (name, process_key,
    # description; no arguments, no result_processor_kind).
    "name": "ensure_host_disk_guard_schedule",
    "process_key": "plugin::platform_health_plugin::ensure_host_disk_guard_schedule",
    "description": "Install the host-disk headroom cron.",
}


def test_starting_action_submits_as_a_terminal_action_after_startup() -> None:
    """The install trigger: the real EventOrchestrator._submit_starting_actions
    stamps the startup flow/session onto the starting-action entry and submits it
    through the real ActionFactory. With no result_processor_kind the stored
    action carries no result processor, so on success it is terminal
    (EDGE_SINK_SKIP, no inference turn); the verb's error processor stays
    attached, so a failed install is surfaced on the startup flow."""
    recorder = _RecordingActionEventRecorder()
    agent_messaging = _FixtureAgentMessagingPlugin()
    orch = _orchestrator_at_plugin_startup(agent_messaging)
    orch.action_factory = ActionFactory(  # type: ignore[assignment]
        template_engine=_RecordingTemplateEngine(),  # type: ignore[arg-type]
        state_service=_SuffixStateService(),
        action_event_recorder=recorder,
    )
    orch._process_registry = _real_registry()  # noqa: SLF001 — what the orchestrator hands the factory
    orch.current_flow_id = "flow-startup-smoke"
    orch.current_session_id = "sess-startup-smoke"
    orch._submit_starting_actions([dict(_STARTING_ACTION)], {})  # noqa: SLF001 — the real starting-action path
    stored = recorder.stored[0] if recorder.stored else {}
    _check(stored.get("process_key") == _STARTING_ACTION["process_key"] and stored.get("flow_id") == "flow-startup-smoke", "the starting action is submitted on the startup flow")
    _check("result_processor" not in stored and stored.get("result_processor_kind") is None, "no result processor: a successful install is terminal")
    error_processor = stored.get("error_processor")
    _check(isinstance(error_processor, dict) and error_processor.get("process_key") == _PROCESS_ERROR_KEY, "the error processor stays attached: a failed install is surfaced")
    plugin_module.get_config = lambda: _FakeConfigManager({"alert_role": "Main-Seat"})  # type: ignore[assignment]
    plugin = PlatformHealthPlugin()
    plugin.orchestrator_ref = orch
    plugin.prepare_for_readiness()
    scheduling = _FixtureSchedulingService()
    _run_init_service_manager(orch, scheduling)
    parameters = stored.get("parameters")
    result = plugin.ensure_host_disk_guard_schedule(parameters if isinstance(parameters, dict) else {}, {})
    _check(result["data"]["status"] == "created" and len(scheduling.create_calls) == 1, "dispatching the stored action installs the cron")


def test_ensure_verb_unconfigured_refuses_and_logs_error() -> None:
    scheduling = _FixtureSchedulingService()
    plugin, orch = _plugin({}, _FixtureAgentMessagingPlugin(), _reader(500 * GIB))
    _run_init_service_manager(orch, scheduling)
    capture = _LogCapture()
    guard_logger = logging.getLogger("platform_health_plugin.host_disk_guard")
    guard_logger.addHandler(capture)
    try:
        refused = plugin.ensure_host_disk_guard_schedule({}, {})
    finally:
        guard_logger.removeHandler(capture)
    _check(refused["data"]["status"] == "refused", "no alert_role -> the installer refuses")
    _check(scheduling.create_calls == [] and scheduling.get_schedules_by_tag_calls == [], "no alert_role -> the scheduler is not touched")
    _check(any("refusing to install" in message for message in capture.messages), "the refusal is logged at ERROR")


def test_ensure_verb_refuses_an_alert_role_override() -> None:
    """1g: the installed cron fires with empty arguments and resolves its
    target from config alone, so a call-time override must not get an inert
    cron past the refusal."""
    scheduling = _FixtureSchedulingService()
    plugin, orch = _plugin({}, _FixtureAgentMessagingPlugin(), _reader(500 * GIB))
    _run_init_service_manager(orch, scheduling)
    try:
        plugin.ensure_host_disk_guard_schedule({"alert_role": "Main-Seat"}, {})
    except ValueError as exc:
        _check("alert_role" in str(exc), "an alert_role override is refused loudly")
    else:
        _check(False, "an alert_role override is refused loudly")
    _check(scheduling.create_calls == [], "no cron was installed")


def test_check_verb_passes_its_state_to_the_alert() -> None:
    agent_messaging = _FixtureAgentMessagingPlugin()
    plugin, _ = _plugin({"alert_role": "Main-Seat"}, agent_messaging, _reader(80 * GIB))
    received = {"caller_attribution_instance_id": "agi-smoke"}
    result = plugin.check_host_disk_headroom({}, received)
    _check(result["data"]["alerted"] is True, "the direct EDGE verb alerts on a transition")
    _check(agent_messaging.states == [received], "the direct EDGE verb hands its own state to peer_send_by_name")


def test_cron_verb_runs_one_background_check_at_a_time() -> None:
    release = threading.Event()

    def _blocking_reader(path: str) -> _FakeUsage:  # noqa: ARG001
        release.wait(10)
        return _FakeUsage(80 * GIB)

    agent_messaging = _FixtureAgentMessagingPlugin()
    plugin, _ = _plugin({"alert_role": "Main-Seat"}, agent_messaging, _blocking_reader)
    fire_state = {"flow_id": "flow-host-disk-guard", "session_id": "sess-host-disk-guard"}
    first = plugin.check_host_disk_headroom_cron({}, fire_state)
    second = plugin.check_host_disk_headroom_cron({}, fire_state)
    _check(first["data"]["dispatch"] == "started", "a fire returns 'started' at once, before the check has measured")
    _check(second["data"]["dispatch"] == "already_running", "an overlapping fire is a no-op, not a second check")
    _check(agent_messaging.calls == [], "nothing was sent while the background check was still blocked")
    release.set()
    _check(_wait_for_check(plugin), "the background check finishes and releases its slot")
    _check(agent_messaging.states == [fire_state], "the background check alerts with the fire's own state")
    plugin.set_active(False)
    _check(plugin.check_host_disk_headroom_cron({}, fire_state)["data"]["dispatch"] == "inactive", "the inactive colour's fires are no-ops")


def test_cron_background_failure_is_logged_at_error() -> None:
    plugin, _ = _plugin({"alert_role": "Main-Seat"}, _FixtureAgentMessagingPlugin(), _raising_reader(OSError("statvfs failed")))
    capture = _LogCapture()
    plugin.logger.addHandler(capture)
    try:
        fire = plugin.check_host_disk_headroom_cron({}, {})
        finished = _wait_for_check(plugin)
    finally:
        plugin.logger.removeHandler(capture)
    _check(fire["data"]["dispatch"] == "started" and finished, "a failing check does not raise into the scheduler")
    _check(any("host-disk check failed" in message for message in capture.messages), "the failure is logged at ERROR")


def main() -> int:
    print("=== platform_health_plugin host_disk_guard_smoke ===")
    test_classify_headroom_exact_byte_boundaries()
    test_le_mutant_dies_at_every_boundary()
    test_config_rejects_inverted_floors()
    test_measure_free_bytes_propagates_failure()
    test_measure_free_bytes_happy_path()
    test_build_alert_text_carries_fresh_measurement_no_stale_advice()
    test_first_tick_ok_is_quiet_and_writes_no_alert()
    test_first_tick_warning_alerts_and_records_marker()
    test_repeated_tick_same_status_does_not_realert()
    test_escalation_and_recovery_each_alert_once()
    test_undelivered_alert_fails_loud_and_does_not_advance_marker()
    test_role_vacant_dispatch_failure_fails_loud()
    test_unconfigured_alert_role_measures_but_cannot_alert()
    test_state_service_failure_propagates_loud()
    test_different_paths_have_independent_markers()
    test_alert_carries_the_received_state()
    test_ensure_schedule_creates_when_absent()
    test_ensure_schedule_is_idempotent()
    test_ensure_schedule_lookup_failure_raises_loudly()
    test_ensure_schedule_refuses_when_alert_role_unset()
    test_cron_entry_parses_terminal_and_registers_as_edge_sink()
    test_cron_fire_attaches_no_processor_and_stamps_flow_id()
    test_control_edge_verb_as_cron_target_gets_the_inference_error_processor()
    test_fire_without_flow_id_is_refused()
    test_whole_tree_gate_c51_sees_and_exempts_the_literal()
    test_plugin_comes_up_before_scheduling_service_exists()
    test_starting_action_submits_as_a_terminal_action_after_startup()
    test_ensure_verb_unconfigured_refuses_and_logs_error()
    test_ensure_verb_refuses_an_alert_role_override()
    test_check_verb_passes_its_state_to_the_alert()
    test_cron_verb_runs_one_background_check_at_a_time()
    test_cron_background_failure_is_logged_at_error()
    print(f"\n{_passed} passed, {len(_failed)} failed")
    if _failed:
        for label in _failed:
            print(f"  FAILED: {label}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
