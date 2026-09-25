#!/usr/bin/env python3
"""Unit smoke for the D1 platform sweep (``session_sweep.py``) — the
``on_tick`` rider that marks overdue sessions, fires+delivers armed
``deadline`` dependency edges, and prunes stale ``session_role_claim`` rows
(Architect ratification #3). Also covers the ``retire_session`` crash-mid-
retire redrive leg (Coordinator-Dawn's explicit fold-in — both touch the
same states, so one file measures both).

``sweep_overdue_sessions`` / ``sweep_deadline_dependencies`` are pure
functions against ``RealShapeState``, with a controlled clock (no real time
in a sweep test). The dependency-delivery + pruner legs use REAL
``BridgeSessionManager``/``PeerRegistry`` instances (in-process, no server) —
the same technique ``direct_wake_outbox_smoke.py`` uses for REL-05 — so the
resolve-then-append delivery path is exercised for real, not stubbed.

Run:
    SOLET_NAME=<name>-test .venv/bin/python3 \
        plugins/agent_messaging_plugin/tests/session_sweep_smoke.py
"""

from __future__ import annotations

import hashlib
import sys
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT / "ananta" / "src"))
sys.path.insert(0, str(REPO_ROOT / "plugins" / "agent_messaging_plugin" / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

if TYPE_CHECKING:
    from ananta.interfaces.state_management_interface import StateManagementInterface

from _real_state_fake import CapEnforcingState, RealShapeState  # noqa: E402
from _recorded_lane_worktree_fixture import RecordedLaneWorktreeFixture  # noqa: E402
from ananta.llm.agent_messaging.role_binding import AGENT_ROLE_BINDING_NAMESPACE  # noqa: E402
from ananta.services.store import Store, open_store  # noqa: E402

import agent_messaging_plugin.overdue_notice as overdue_notice  # noqa: E402
import agent_messaging_plugin.session_hosts as session_hosts  # noqa: E402
from agent_messaging_plugin.bridge_sessions import BridgeSessionManager  # noqa: E402
from agent_messaging_plugin.gauge_notice_record_store import (  # noqa: E402
    read_gauge_notice_records,
)
from agent_messaging_plugin.local_cli.spool import watch_instance_digest  # noqa: E402
from agent_messaging_plugin.managed_dispatch import (  # noqa: E402
    DISPATCH_WORKER_LOST,
    DispatchSpec,
    managed_dispatch_status,
    prepare_managed_dispatch,
    read_managed_dispatch,
    record_first_turn_evidence,
    supervise_managed_dispatches,
)
from agent_messaging_plugin.models import BridgeBinding  # noqa: E402
from agent_messaging_plugin.peer_registry import PeerRegistry  # noqa: E402
from agent_messaging_plugin.schema import (  # noqa: E402
    CONDITION_DEADLINE,
    CONDITION_LANE_CLOSED,
    CONDITION_SESSION_TERMINAL,
    LIFECYCLE_IDLE,
    LIFECYCLE_LIVE,
    LIFECYCLE_OVERDUE,
    LIFECYCLE_RETIRED,
    LIFECYCLE_SPAWNING,
    LIFECYCLE_TERMINATED,
    NOTICE_DELIVERY_APPENDED,
    NOTICE_DELIVERY_NO_STEWARD_BINDING,
    PEER_BINDING_NAMESPACE,
    TABLE_SESSION_DEPENDENCY,
    TABLE_SESSION_ROLE_CLAIM,
    WORK_CLASS_READ_ONLY,
    get_peer_binding_schema,
    session_role_claim_external_id,
)
from agent_messaging_plugin.session_context_status_store import (  # noqa: E402
    upsert_session_context_status,
)
from agent_messaging_plugin.session_lifecycle_store import (  # noqa: E402
    ManagedSessionSpec,
    backfill_registration,
    insert_managed_session,
    read_managed_session,
    set_host_ref,
    transition_lifecycle_state,
)
from agent_messaging_plugin.session_lifecycle_verbs import (  # noqa: E402
    retire_session,
    terminate_session,
)
from agent_messaging_plugin.session_sweep import (  # noqa: E402
    DEFAULT_REGISTRATION_BOUND_S,
    EVENT_SESSION_REGISTRATION_OVERDUE_NOTICE,
    GAUGE_COVERAGE_GRACE_S,
    GAUGE_STALE_LAG_S,
    GAUGE_STALE_ROTATION_GRACE_S,
    NoticeLatch,
    SessionRoleClaimPruner,
    StewardNoticeCounts,
    _notify_rotation_due,
    last_report_alive,
    sweep_deadline_dependencies,
    sweep_gauge_coverage,
    sweep_gauge_staleness,
    sweep_lane_closed_dependencies,
    sweep_managed_dispatches,
    sweep_overdue_sessions,
    sweep_rotation_due_sessions,
    sweep_unregistered_spawning_sessions,
)

T0 = datetime(2026, 8, 3, 12, 0, 0, tzinfo=UTC)


def _past_grace() -> datetime:
    """A clock far enough past the fixtures' spawn time that the gauge leg's
    startup grace no longer applies.

    The fixtures create rows at the REAL wall clock, so this is derived from
    ``datetime.now`` rather than from :data:`T0`. Every gauge-coverage test
    passes this explicitly: after the R4 lane added the grace, "this session is
    dark" is a claim about a session that has HAD TIME to report, and a test
    that does not say how old its row is no longer states its own precondition.
    """
    return datetime.now(UTC) + timedelta(seconds=GAUGE_COVERAGE_GRACE_S + 60)

_passed = 0
_failed: list[str] = []


def _check(condition: object, label: str) -> None:
    global _passed
    if condition:
        _passed += 1
        print(f"  PASS  {label}")
        return
    _failed.append(label)
    print(f"  FAIL  {label}")


def _state() -> StateManagementInterface:
    return cast("StateManagementInterface", RealShapeState())


_TEST_MANAGED_HOST = "test-managed-dispatch-host"


class _LivenessDriver:
    def __init__(self, outcome: bool | Exception) -> None:
        self.outcome = outcome

    def verify_config(self) -> None:
        return

    def spawn(self, spec: dict[str, Any]) -> str:  # noqa: ARG002
        return "managed-host-ref"

    def terminate(self, host_ref: str, grace_seconds: float = 0) -> None:  # noqa: ARG002
        return

    def alive(self, host_ref: str) -> bool:  # noqa: ARG002
        if isinstance(self.outcome, Exception):
            raise self.outcome
        return self.outcome

    def driver_channel(self, agent_instance_id: str, host_ref: str) -> None:  # noqa: ARG002
        return None


def _dispatch_spec(tmp: Path, dispatch_id: str, *, uptake_seconds: int = 60) -> DispatchSpec:
    brief = tmp / "brief.md"
    if not brief.exists():
        brief.write_text("supervisor fixture\n", encoding="utf-8")
    return DispatchSpec(
        dispatch_id=dispatch_id,
        lane_id=dispatch_id,
        role_name="Managed-Worker",
        role_class="project",
        work_class="production_mutation",
        budget_line="managed-dispatch-smoke",
        brief_ref=str(brief),
        brief_sha256=hashlib.sha256(brief.read_bytes()).hexdigest(),
        expected_path=str(tmp / f"{dispatch_id}.md"),
        completion_contract={
            "evidence_obligations": [
                {"id": "focused", "allowed_statuses": ["pass"]},
            ],
            "allowed_verdicts": ["READY-FOR-REVIEW", "BLOCKED"],
        },
        model="gpt-5.6-sol",
        effort="xhigh",
        agent_runtime="codex",
        allowed_hosts=[_TEST_MANAGED_HOST],
        host=_TEST_MANAGED_HOST,
        visibility="headless",
        local_name="Managed-Worker",
        report_by_seconds=900,

        allowed_tools=("Read",),
        permission_mode="bypassPermissions",
        transport="mcp",
        allow_askuserquestion=False,
        degraded_hooks_acknowledged=False,
        spawned_by_instance_id="agi-steward",
        spawned_by_role="Coordinator-Main",
        directed_by="operator:seat",
        uptake_due_at=(T0 + timedelta(seconds=uptake_seconds)).isoformat(),
        report_by=(T0 + timedelta(minutes=15)).isoformat(),
        watchdog_due_at=(T0 + timedelta(minutes=3)).isoformat(),

    )


def _linked_live_attempt(
    state: StateManagementInterface,
    tmp: Path,
    *,
    dispatch_id: str,
    agent_instance_id: str,
) -> None:
    prepare_managed_dispatch(state, _dispatch_spec(tmp, dispatch_id), now=T0)
    insert_managed_session(
        state,
        ManagedSessionSpec(
            agent_instance_id=agent_instance_id,
            lane_id=dispatch_id,
            brief_ref=str(tmp / "brief.md"),
            work_class="production_mutation",
            budget_line="managed-dispatch-smoke",
            host=_TEST_MANAGED_HOST,
            agent_runtime="codex",
            dispatch_id=dispatch_id,
        ),
    )
    set_host_ref(state, agent_instance_id=agent_instance_id, host_ref="managed-host-ref")
    transition_lifecycle_state(
        state,
        agent_instance_id=agent_instance_id,
        from_state=LIFECYCLE_SPAWNING,
        to_state=LIFECYCLE_LIVE,
        directed_by="fixture",
    )
    record_first_turn_evidence(
        state,
        dispatch_id=dispatch_id,
        agent_instance_id=agent_instance_id,
        source="charter",
        delivered=True,
        error="",
        host=_TEST_MANAGED_HOST,
        host_ref="managed-host-ref",
        agent_runtime="codex",
        observed_at=T0 + timedelta(seconds=1),
    )


def test_managed_tmux_death_converges_and_deduplicates() -> None:
    """Fixture 4: definitive native death ends false-live within one sweep."""
    with tempfile.TemporaryDirectory() as raw:
        state = _state()
        tmp = Path(raw)
        _linked_live_attempt(
            state,
            tmp,
            dispatch_id="mdp-dead",
            agent_instance_id="agi-dead",
        )
        key = (session_hosts.AGENT_RUNTIME_CODEX, _TEST_MANAGED_HOST)
        prior = session_hosts._REGISTRY.get(key)  # noqa: SLF001
        session_hosts._REGISTRY[key] = _LivenessDriver(False)  # noqa: SLF001
        registry = _peer_registry()
        manager = _bridge_manager()
        steward_bridge_id = _register_live_binding(
            registry,
            manager,
            agent_instance_id="agi-steward",
        )
        try:
            first = sweep_managed_dispatches(
                state,
                peer_registry=registry,
                bridge_manager=manager,
                now=T0 + timedelta(seconds=2),
            )
            second = sweep_managed_dispatches(
                state,
                peer_registry=registry,
                bridge_manager=manager,
                now=T0 + timedelta(seconds=3),
            )
        finally:
            if prior is None:
                session_hosts._REGISTRY.pop(key, None)  # noqa: SLF001
            else:
                session_hosts._REGISTRY[key] = prior  # noqa: SLF001
        _check(first["dead"] == 1, "04 definitive dead host is found in one interval")
        _check(
            read_managed_session(state, "agi-dead")["lifecycle_state"] == LIFECYCLE_TERMINATED,
            "04 false-live attempt converges to terminated",
        )
        _check(
            read_managed_dispatch(state, "mdp-dead")["state"] == DISPATCH_WORKER_LOST,
            "04 dispatch converges to worker_lost",
        )
        _check(second["dead"] == 0, "04 terminal attempt is not noticed twice")
        _, notices = manager.get(steward_bridge_id).events_after(-1)
        _check(
            len(notices) == 1 and notices[0].event_type == "managed_dispatch_notice",
            "04 steward receives exactly one deduplicated worker-lost notice",
        )


def test_managed_probe_fault_is_unknown() -> None:
    """Fixture 5: a driver exception is neither alive nor dead."""
    with tempfile.TemporaryDirectory() as raw:
        state = _state()
        tmp = Path(raw)
        _linked_live_attempt(
            state,
            tmp,
            dispatch_id="mdp-unknown",
            agent_instance_id="agi-unknown",
        )
        key = (session_hosts.AGENT_RUNTIME_CODEX, _TEST_MANAGED_HOST)
        prior = session_hosts._REGISTRY.get(key)  # noqa: SLF001
        driver = _LivenessDriver(RuntimeError("probe fault"))
        session_hosts._REGISTRY[key] = driver  # noqa: SLF001
        try:
            result = sweep_managed_dispatches(state, now=T0 + timedelta(seconds=2))
            unknown_projection = read_managed_dispatch(state, "mdp-unknown")
            status = managed_dispatch_status(
                state,
                "mdp-unknown",
                now=T0 + timedelta(seconds=2),
            )
            reprobe = supervise_managed_dispatches(
                state,
                now=T0 + timedelta(seconds=33),
            )
            repeated = sweep_managed_dispatches(state, now=T0 + timedelta(seconds=40))
            escalated = sweep_managed_dispatches(state, now=T0 + timedelta(seconds=123))
            driver.outcome = True
            recovered = sweep_managed_dispatches(state, now=T0 + timedelta(seconds=124))
        finally:
            if prior is None:
                session_hosts._REGISTRY.pop(key, None)  # noqa: SLF001
            else:
                session_hosts._REGISTRY[key] = prior  # noqa: SLF001
        row = read_managed_session(state, "agi-unknown")
        _check(result["unknown"] == 1, "05 probe fault counts as unknown")
        _check(row["lifecycle_state"] == LIFECYCLE_LIVE, "05 unknown does not terminate live row")
        _check(status["host_liveness"] == "unknown", "05 aggregate status is explicitly unknown")
        _check(
            bool(unknown_projection.get("next_liveness_probe_at"))
            and bool(unknown_projection.get("liveness_escalation_due_at")),
            "05 unknown liveness persists a bounded re-probe obligation",
        )
        _check(
            [item["condition"] for item in reprobe["conditions"]]
            == ["liveness_reprobe_due"],
            "05 elapsed re-probe deadline surfaces the exact action",
        )
        _check(repeated["unknown"] == 1, "05 repeated probe fault remains unknown")
        _check(
            any(
                item["condition"] == "liveness_unknown_escalation"
                for item in escalated["conditions"]
            ),
            "05 repeated faults reach bounded coordinator escalation",
        )
        _check(
            recovered["alive"] == 1
            and not read_managed_dispatch(state, "mdp-unknown").get(
                "next_liveness_probe_at"
            ),
            "05 a successful re-probe clears unknown obligations",
        )


def test_managed_dispatch_sweep_is_uncapped() -> None:
    """Fixture 14: every owed row beyond the normal query page is evaluated."""
    with tempfile.TemporaryDirectory() as raw:
        state = _state()
        tmp = Path(raw)
        for index in range(125):
            prepare_managed_dispatch(
                state,
                _dispatch_spec(tmp, f"mdp-page-{index}", uptake_seconds=10),
                now=T0,
            )
        result = sweep_managed_dispatches(state, now=T0 + timedelta(seconds=20))
        _check(result["dispatches_evaluated"] == 125, "14 all 125 dispatches are evaluated")
        _check(result["notices_emitted"] == 125, "14 every owed dispatch gets one notice")


def _peer_registry() -> PeerRegistry:
    store: Store = open_store(
        get_peer_binding_schema(), namespace=PEER_BINDING_NAMESPACE, backend="in_memory",
    )
    return PeerRegistry(bindings_store=store)


def _bridge_manager() -> BridgeSessionManager:
    return BridgeSessionManager(
        session_id_factory=lambda _n: "ags-http",
        idle_timeout_s=3600,
        max_pending_events=50,
        long_poll_timeout_s=1,
    )


def _spawn_live(
    state: StateManagementInterface,
    *,
    agent_instance_id: str,
    lifecycle_state: str = LIFECYCLE_LIVE,
    report_by_seconds: int = 0,
    report_by_override: str | None = None,
    spawned_by_instance_id: str = "",
) -> None:
    insert_managed_session(
        state,
        ManagedSessionSpec(
            agent_instance_id=agent_instance_id, lane_id="lane-x", brief_ref="",
            work_class=WORK_CLASS_READ_ONLY, budget_line="b1", host="operator",
            report_by_seconds=report_by_seconds,
            spawned_by_instance_id=spawned_by_instance_id,
        ),
    )
    if report_by_override is not None:
        state.update_state(
            AGENT_ROLE_BINDING_NAMESPACE,
            {"table": "managed_session", "filters": {"agent_instance_id": agent_instance_id}},
            {"report_by": report_by_override},
        )
    if lifecycle_state != LIFECYCLE_SPAWNING:
        transition_lifecycle_state(
            state, agent_instance_id=agent_instance_id, from_state=LIFECYCLE_SPAWNING,
            to_state=LIFECYCLE_LIVE, directed_by="operator:none",
        )
        if lifecycle_state == LIFECYCLE_IDLE:
            transition_lifecycle_state(
                state, agent_instance_id=agent_instance_id, from_state=LIFECYCLE_LIVE,
                to_state=LIFECYCLE_IDLE, directed_by="operator:none",
            )


# ---------------------------------------------------------------------------
# sweep_overdue_sessions
# ---------------------------------------------------------------------------


def test_overdue_no_report_by_never_swept() -> None:
    state = _state()
    _spawn_live(state, agent_instance_id="agi-no-contract")  # no report_by at all
    marked = sweep_overdue_sessions(state, now=T0 + timedelta(days=365))
    _check(marked == 0, "a row with no report_by is never swept (no contract, not expired)")
    _check(
        read_managed_session(state, "agi-no-contract")["lifecycle_state"] == LIFECYCLE_LIVE,
        "its lifecycle_state is untouched",
    )


def test_overdue_marks_past_deadline_live_and_idle() -> None:
    state = _state()
    past = (T0 - timedelta(seconds=10)).isoformat()
    _spawn_live(
        state, agent_instance_id="agi-live-late", lifecycle_state=LIFECYCLE_LIVE,
        report_by_override=past,
    )
    _spawn_live(
        state, agent_instance_id="agi-idle-late", lifecycle_state=LIFECYCLE_IDLE,
        report_by_override=past,
    )
    marked = sweep_overdue_sessions(state, now=T0)
    _check(marked == 2, "both a late LIVE row and a late IDLE row are marked overdue")
    _check(
        read_managed_session(state, "agi-live-late")["lifecycle_state"] == LIFECYCLE_OVERDUE
        and read_managed_session(state, "agi-idle-late")["lifecycle_state"] == LIFECYCLE_OVERDUE,
        "both rows now read 'overdue'",
    )


def test_overdue_skips_future_deadline() -> None:
    state = _state()
    future = (T0 + timedelta(seconds=300)).isoformat()
    _spawn_live(
        state, agent_instance_id="agi-not-yet", lifecycle_state=LIFECYCLE_LIVE,
        report_by_override=future,
    )
    marked = sweep_overdue_sessions(state, now=T0)
    _check(marked == 0, "a report_by still in the future is not swept")


def _register_live_binding(
    reg: PeerRegistry, mgr: BridgeSessionManager, *, agent_instance_id: str,
    agent_session_id: str = "",
) -> str:
    """Same pattern ``test_deadline_dependency_fires_and_delivers`` uses —
    a real bridge + a real peer registry binding, no server, so the
    resolve-then-append delivery path is exercised for real, not stubbed.

    ``agent_session_id`` is what a real registration carries (the launcher's
    exported ``$AGENT_SESSION_ID``); it defaults to empty so every pre-GAU-26
    caller keeps the binding shape it was written against."""
    bridge_id = mgr.open(solet_name="", parent_pid=1).bridge_id
    reg.register(
        BridgeBinding(
            bridge_id=bridge_id, agent_id="claude_code", agent_instance_id=agent_instance_id,
            session_label=agent_instance_id, parent_pid=1,
            agent_session_id=agent_session_id,
        ),
    )
    return bridge_id


def _register_watcher_held_steward(
    state: StateManagementInterface,
    reg: PeerRegistry,
    mgr: BridgeSessionManager,
    *,
    ledger_instance_id: str,
    agent_session_id: str,
) -> tuple[str, str]:
    """A steward in the population GAU-26 is about it: its ``managed_session``
    row keys on its LEDGER id, while its live bridge binding keys on the
    WATCH id its session id derives to.

    Built through the real write paths, not by poking columns.
    ``backfill_registration`` is the production registration hook, and the
    watch id comes from ``watch_instance_digest`` — the same function
    ``_resolve_watch_identity`` (``local_cli/cli.py``) uses to mint it — so
    the fixture cannot drift from the id scheme it is testing.

    Returns ``(watch_instance_id, steward_bridge_id)``.
    """
    _spawn_live(
        state, agent_instance_id=ledger_instance_id, lifecycle_state=LIFECYCLE_SPAWNING,
    )
    backfill_registration(
        state, agent_instance_id=ledger_instance_id, agent_id="claude_code",
        agent_session_id=agent_session_id,
    )
    watch_instance_id = f"agi-watch-{watch_instance_digest(agent_session_id)}"
    bridge_id = _register_live_binding(
        reg, mgr, agent_instance_id=watch_instance_id, agent_session_id=agent_session_id,
    )
    return watch_instance_id, bridge_id


def test_overdue_notifies_steward() -> None:
    """D2-lane-tail follow-up #3: the fix. The MANAGED-spawner leg -- a row
    spawned WITH a recorded steward (spawned_by_instance_id) that ALSO has
    its own managed_session row goes overdue and delivers exactly one
    session_overdue_notice event to the steward's live bridge. See
    :func:`test_overdue_notifies_unmanaged_steward` for the dominant
    UNMANAGED-spawner leg (an operator-launched seat with no managed_session
    row of its own)."""
    state = _state()
    reg = _peer_registry()
    mgr = _bridge_manager()
    _spawn_live(state, agent_instance_id="agi-steward")
    state.update_state(
        AGENT_ROLE_BINDING_NAMESPACE,
        {"table": "managed_session", "filters": {"agent_instance_id": "agi-steward"}},
        {"agent_id": "claude_code"},
    )
    steward_bridge_id = _register_live_binding(reg, mgr, agent_instance_id="agi-steward")
    past = (T0 - timedelta(seconds=10)).isoformat()
    _spawn_live(
        state, agent_instance_id="agi-worker", lifecycle_state=LIFECYCLE_LIVE,
        report_by_override=past, spawned_by_instance_id="agi-steward",
    )
    marked = sweep_overdue_sessions(state, peer_registry=reg, bridge_manager=mgr, now=T0)
    _check(marked == 1, "the overdue row is still transitioned")
    _check(
        read_managed_session(state, "agi-worker")["lifecycle_state"] == LIFECYCLE_OVERDUE,
        "the row reads 'overdue'",
    )
    _, events = mgr.get(steward_bridge_id).events_after(-1)
    _check(
        len(events) == 1
        and events[0].event_type == "session_overdue_notice"
        and "agi-worker" in events[0].content,
        f"RED-vs-GREEN: the steward's bridge gets exactly one delivered "
        f"overdue-notice event naming the overdue session (got {events!r}) "
        "-- before this fix, sweep_overdue_sessions sent NO notification "
        "of any kind",
    )


def test_overdue_fresh_heartbeat_is_quiet_but_the_row_stays_lapsed() -> None:
    """iss_8126960b: a fresh passive heartbeat suppresses only the direct wake.

    Killing mutation: route every lapsed row through ``EVENT_SESSION_OVERDUE_NOTICE``
    (the former implementation).  This emits the wake-class event and calls the
    managed-driver nudge despite the fresh server stamp.
    """
    state = _state()
    reg = _peer_registry()
    mgr = _bridge_manager()
    steward_bridge_id = _register_live_binding(reg, mgr, agent_instance_id="agi-steward")
    report_by = (T0 - timedelta(seconds=1)).isoformat()
    _spawn_live(
        state,
        agent_instance_id="agi-fresh-late",
        lifecycle_state=LIFECYCLE_LIVE,
        report_by_override=report_by,
        spawned_by_instance_id="agi-steward",
    )
    state.update_state(
        AGENT_ROLE_BINDING_NAMESPACE,
        {"table": "managed_session", "filters": {"agent_instance_id": "agi-fresh-late"}},
        {"last_heartbeat_at": (T0 - timedelta(seconds=1)).isoformat()},
    )
    drive_calls: list[str] = []
    original_drive = overdue_notice.drive_on_delivery
    overdue_notice.drive_on_delivery = lambda _state, **kwargs: drive_calls.append(
        str(kwargs["recipient_agent_instance_id"]),
    )
    try:
        marked = sweep_overdue_sessions(state, peer_registry=reg, bridge_manager=mgr, now=T0)
    finally:
        overdue_notice.drive_on_delivery = original_drive
    _, events = mgr.get(steward_bridge_id).events_after(-1)
    row = read_managed_session(state, "agi-fresh-late")
    _check(marked == 1 and row["lifecycle_state"] == LIFECYCLE_OVERDUE, "fresh heartbeat leaves the report_by-lapsed row honestly overdue")
    _check(row["report_by"] == report_by, "fresh heartbeat does not re-arm or otherwise alter report_by")
    _check(
        len(events) == 1 and events[0].event_type == overdue_notice.EVENT_SESSION_OVERDUE_QUIET_NOTICE and "quiet low-priority line" in events[0].content,
        "fresh heartbeat emits one visible quiet line, never the wake-class event",
    )
    _check(not drive_calls, "fresh-heartbeat quiet line never invokes the direct driver wake")


def test_overdue_stale_heartbeat_keeps_the_full_alarm_and_wake() -> None:
    """iss_8126960b: a stamp just beyond the 180s hook cadence stays urgent."""
    state = _state()
    reg = _peer_registry()
    mgr = _bridge_manager()
    steward_bridge_id = _register_live_binding(reg, mgr, agent_instance_id="agi-steward")
    _spawn_live(
        state,
        agent_instance_id="agi-stale-late",
        lifecycle_state=LIFECYCLE_LIVE,
        report_by_override=(T0 - timedelta(seconds=1)).isoformat(),
        spawned_by_instance_id="agi-steward",
    )
    state.update_state(
        AGENT_ROLE_BINDING_NAMESPACE,
        {"table": "managed_session", "filters": {"agent_instance_id": "agi-stale-late"}},
        {"last_heartbeat_at": (T0 - timedelta(seconds=181)).isoformat()},
    )
    drive_calls: list[str] = []
    original_drive = overdue_notice.drive_on_delivery
    overdue_notice.drive_on_delivery = lambda _state, **kwargs: drive_calls.append(
        str(kwargs["recipient_agent_instance_id"]),
    )
    try:
        marked = sweep_overdue_sessions(state, peer_registry=reg, bridge_manager=mgr, now=T0)
    finally:
        overdue_notice.drive_on_delivery = original_drive
    _, events = mgr.get(steward_bridge_id).events_after(-1)
    _check(marked == 1, "stale-heartbeat lapsed row is still marked overdue")
    _check(
        len(events) == 1 and events[0].event_type == overdue_notice.EVENT_SESSION_OVERDUE_NOTICE,
        "stale heartbeat keeps the existing wake-class overdue event",
    )
    _check(drive_calls == ["agi-steward"], "stale heartbeat keeps the existing direct driver wake")


def test_overdue_notifies_unmanaged_steward() -> None:
    """RED-FIRST (D3 slice-0c, coordinator-seat queue addition 2026-08-04 13:22Z):
    the dominant case in practice -- a steward with NO managed_session row
    of its own (the operator-launched-seat shape; today every worker is
    seat-spawned, and the seat itself is operator-launched, never spawned
    via spawn_session). Before this fix, steward resolution went ONLY
    through the spawner's managed_session row for its agent_id;
    an unmanaged spawner has no such row, so the lookup returned "" and the
    notice silently never fired (measured live, session_sweep.py:175,
    2026-08-04 13:13:01Z: "spawner ... has no managed_session row ... cannot
    resolve a live binding to notify"). The fix resolves the steward
    straight from the peer registry by instance id, with no managed_session
    detour required."""
    state = _state()
    reg = _peer_registry()
    mgr = _bridge_manager()
    # The steward is registered in the peer registry directly -- no
    # _spawn_live() call for it at all, so it has NO managed_session row.
    steward_bridge_id = _register_live_binding(
        reg, mgr, agent_instance_id="agi-unmanaged-steward",
    )
    past = (T0 - timedelta(seconds=10)).isoformat()
    _spawn_live(
        state, agent_instance_id="agi-worker-unmanaged-steward", lifecycle_state=LIFECYCLE_LIVE,
        report_by_override=past, spawned_by_instance_id="agi-unmanaged-steward",
    )
    marked = sweep_overdue_sessions(state, peer_registry=reg, bridge_manager=mgr, now=T0)
    _check(marked == 1, "the overdue row is still transitioned")
    _, events = mgr.get(steward_bridge_id).events_after(-1)
    _check(
        len(events) == 1
        and events[0].event_type == "session_overdue_notice"
        and "agi-worker-unmanaged-steward" in events[0].content,
        f"RED-vs-GREEN: an UNMANAGED steward (no managed_session row of its "
        f"own) still gets the overdue notice delivered (got {events!r}) -- "
        "before this fix, resolution went only through the spawner's "
        "managed_session row and silently found nothing to notify",
    )


# ---------------------------------------------------------------------------
# GAU-26 — steward resolution across the fleet's TWO session-id minting schemes
#
# Every steward-notify leg in this module keys on the LEDGER
# ``spawned_by_instance_id`` recorded on the worker's row. A watcher-held
# steward's live bridge binding keys on a WATCH id instead
# (``agi-watch-<sha256(agent_session_id)[:24]>``), so the lookup missed by
# construction for the majority population — measured live 2026-08-19 19:27:36Z,
# and again at 19:57:38Z from the other direction (a BRIDGE-BOUND steward, the
# operator seat under its plain ledger id, took the same leg's notice with
# delivery_outcome=appended). Same leg, same night, same release: a matched
# pass/fail pair, which is why both discriminators below reproduce a MEASURED
# failure rather than a hypothesised one.
#
# The fix reads the join key from the durable ``managed_session`` row and never
# rebuilds it. ``test_overdue_join_reads_the_session_id_and_never_derives_it``
# is the one that enforces the second half of that sentence.
# ---------------------------------------------------------------------------


def test_overdue_notifies_a_watch_id_registered_steward() -> None:
    """DISCRIMINATOR (a): a steward whose live binding is registered under a
    WATCH id gets the overdue notice.

    The specimen is the measured one — ``lane-gau-store``'s ledger id
    ``agi-73ba7ce5…`` paired with watch id ``agi-watch-d09a711455d6e5eb7631c087``
    — and the fixture derives that watch id rather than hard-coding it, so the
    24 hex characters matching the id observed in the live registry at 19:48Z is
    a property of the code under test, not of this file.

    RED before the fix: ``resolve_by_agent_instance_id`` is a ``read_one`` on
    the binding table keyed by the ledger id, which appears nowhere in it, and
    the ``(agent_id, instance_id)`` fallback re-keys the SAME ledger id against
    the SAME table — both legs miss, the leg logs 'steward not notified' and
    returns."""
    state = _state()
    reg = _peer_registry()
    mgr = _bridge_manager()
    ledger_id = "agi-73ba7ce552285765b4716a1059326da0"
    watch_id, steward_bridge_id = _register_watcher_held_steward(
        state, reg, mgr,
        ledger_instance_id=ledger_id,
        agent_session_id=f"ases-{ledger_id}",
    )
    _check(
        watch_id == "agi-watch-d09a711455d6e5eb7631c087",
        f"the fixture's derived watch id reproduces the id observed live in the "
        f"peer registry at 2026-08-19 19:48Z (got {watch_id!r})",
    )
    past = (T0 - timedelta(seconds=10)).isoformat()
    _spawn_live(
        state, agent_instance_id="agi-worker-watch-held-steward",
        lifecycle_state=LIFECYCLE_LIVE, report_by_override=past,
        spawned_by_instance_id=ledger_id,
    )
    marked = sweep_overdue_sessions(state, peer_registry=reg, bridge_manager=mgr, now=T0)
    _check(marked == 1, "the overdue row is still transitioned")
    _, events = mgr.get(steward_bridge_id).events_after(-1)
    _check(
        len(events) == 1
        and events[0].event_type == "session_overdue_notice"
        and "agi-worker-watch-held-steward" in events[0].content,
        f"RED-vs-GREEN (GAU-26 discriminator a): a WATCHER-HELD steward gets the "
        f"overdue notice on its live bridge (got {events!r}) -- before this fix "
        "both resolution legs keyed the ledger id against a registry holding only "
        "its watch id, and the alarm was delivered nowhere",
    )


def test_overdue_join_reads_the_session_id_and_never_derives_it() -> None:
    """DISCRIMINATOR (b), the one a derive-based fix cannot pass: the join key
    is READ from the ledger row, never rebuilt from the ledger id.

    ``"ases-" + <ledger id>`` is the SPAWN path's env injection
    (``tmux_adapter``/``headless_adapter``), not a join. The counter-example is
    first-party and live: the operator seat pairs ledger
    ``agi-6be1383613fbd0ec10874571e89956e1`` with session
    ``ases-1786663089-37639-3748``. This steward carries that measured pairing
    and holds its bridge under the watch id that session id derives to — the
    reconnect shape ``agent_session_id`` exists for ("survives reconnect /
    agent_instance_id rotation", models.py), and the state in which the
    session-id join is the ONLY leg that can resolve it.

    A DECOY live binding is registered under exactly the value a reconstructor
    would build. So an implementation that rebuilds the key does not merely fail
    to deliver — it MISROUTES BY NAME, and the second check says so.
    """
    state = _state()
    reg = _peer_registry()
    mgr = _bridge_manager()
    ledger_id = "agi-6be1383613fbd0ec10874571e89956e1"
    _, steward_bridge_id = _register_watcher_held_steward(
        state, reg, mgr,
        ledger_instance_id=ledger_id,
        agent_session_id="ases-1786663089-37639-3748",
    )
    decoy_bridge_id = mgr.open(solet_name="", parent_pid=1).bridge_id
    reg.register(
        BridgeBinding(
            bridge_id=decoy_bridge_id, agent_id="claude_code",
            agent_instance_id="agi-decoy-somebody-else", session_label="decoy",
            parent_pid=1, agent_session_id=f"ases-{ledger_id}",
        ),
    )
    past = (T0 - timedelta(seconds=10)).isoformat()
    _spawn_live(
        state, agent_instance_id="agi-worker-foreign-session-id",
        lifecycle_state=LIFECYCLE_LIVE, report_by_override=past,
        spawned_by_instance_id=ledger_id,
    )
    marked = sweep_overdue_sessions(state, peer_registry=reg, bridge_manager=mgr, now=T0)
    _check(marked == 1, "the overdue row is still transitioned")
    _, events = mgr.get(steward_bridge_id).events_after(-1)
    _check(
        len(events) == 1 and events[0].event_type == "session_overdue_notice",
        f"RED-vs-GREEN (GAU-26 discriminator b): a steward whose agent_session_id "
        f"is NOT derivable from its ledger id still resolves, because the key is "
        f"read from its managed_session row (got {events!r})",
    )
    _, decoy_events = mgr.get(decoy_bridge_id).events_after(-1)
    _check(
        decoy_events == [],
        f"THE DERIVE-KILLER: the decoy bound to 'ases-' + the ledger id -- exactly "
        f"what a reconstructing implementation builds -- receives NOTHING (got "
        f"{decoy_events!r}). A deriving build misroutes this session's private "
        "overdue notice to a different session, and fails here by name",
    )


def test_overdue_bridge_bound_steward_keeps_its_direct_route() -> None:
    """The complementary half, and the measured PASSING side of the pair: a
    BRIDGE-BOUND steward registered under its plain ledger id is resolved by the
    direct lookup, ahead of any ledger read.

    Live at 19:57:38.027Z — a gauge_coverage_notice to steward
    ``agi-6be1383613fbd0ec10874571e89956e1`` (the seat) recorded
    ``delivery_outcome=appended`` on the same leg that lost the watcher-held
    one. The decoy is present again, so an implementation that consults a
    DERIVED key before the direct binding fails here too."""
    state = _state()
    reg = _peer_registry()
    mgr = _bridge_manager()
    ledger_id = "agi-6be1383613fbd0ec10874571e89956e1"
    steward_bridge_id = _register_live_binding(
        reg, mgr, agent_instance_id=ledger_id,
        agent_session_id="ases-1786663089-37639-3748",
    )
    decoy_bridge_id = mgr.open(solet_name="", parent_pid=1).bridge_id
    reg.register(
        BridgeBinding(
            bridge_id=decoy_bridge_id, agent_id="claude_code",
            agent_instance_id="agi-decoy-somebody-else", session_label="decoy",
            parent_pid=1, agent_session_id=f"ases-{ledger_id}",
        ),
    )
    past = (T0 - timedelta(seconds=10)).isoformat()
    _spawn_live(
        state, agent_instance_id="agi-worker-bridge-bound-steward",
        lifecycle_state=LIFECYCLE_LIVE, report_by_override=past,
        spawned_by_instance_id=ledger_id,
    )
    sweep_overdue_sessions(state, peer_registry=reg, bridge_manager=mgr, now=T0)
    _, events = mgr.get(steward_bridge_id).events_after(-1)
    _, decoy_events = mgr.get(decoy_bridge_id).events_after(-1)
    _check(
        len(events) == 1 and decoy_events == [],
        f"a bridge-bound steward with NO managed_session row still takes the "
        f"direct route, and the derived-key decoy stays empty (steward "
        f"{events!r}, decoy {decoy_events!r})",
    )


def test_overdue_steward_row_without_a_session_id_is_best_effort() -> None:
    """A ledger row whose ``agent_session_id`` is still empty (spawned, never
    registered) has no join key. Omission must read as NOT RECORDED, never as a
    licence to reconstruct one -- the leg degrades to the existing warning."""
    state = _state()
    reg = _peer_registry()
    mgr = _bridge_manager()
    ledger_id = "agi-never-registered-steward"
    _spawn_live(state, agent_instance_id=ledger_id, lifecycle_state=LIFECYCLE_SPAWNING)
    decoy_bridge_id = mgr.open(solet_name="", parent_pid=1).bridge_id
    reg.register(
        BridgeBinding(
            bridge_id=decoy_bridge_id, agent_id="claude_code",
            agent_instance_id="agi-decoy-somebody-else", session_label="decoy",
            parent_pid=1, agent_session_id=f"ases-{ledger_id}",
        ),
    )
    past = (T0 - timedelta(seconds=10)).isoformat()
    _spawn_live(
        state, agent_instance_id="agi-worker-sessionless-steward",
        lifecycle_state=LIFECYCLE_LIVE, report_by_override=past,
        spawned_by_instance_id=ledger_id,
    )
    marked = sweep_overdue_sessions(state, peer_registry=reg, bridge_manager=mgr, now=T0)
    _, decoy_events = mgr.get(decoy_bridge_id).events_after(-1)
    _check(
        marked == 1 and decoy_events == [],
        f"an empty agent_session_id is best-effort silence, not a rebuilt key: "
        f"the row is still marked and the decoy gets nothing (got {decoy_events!r})",
    )


def test_overdue_ambiguous_session_id_is_never_a_guessed_recipient() -> None:
    """Two live bindings under one ``agent_session_id`` is presence, not
    absence -- but it is not a delivery target either. The join must degrade to
    the leg's warning rather than picking one, and must never raise back into
    the sweep loop (the other overdue rows in this tick still have to be
    marked)."""
    state = _state()
    reg = _peer_registry()
    mgr = _bridge_manager()
    ledger_id = "agi-ambiguous-steward"
    session_id = f"ases-{ledger_id}"
    _, first_bridge_id = _register_watcher_held_steward(
        state, reg, mgr, ledger_instance_id=ledger_id, agent_session_id=session_id,
    )
    twin_bridge_id = mgr.open(solet_name="", parent_pid=1).bridge_id
    reg.register(
        BridgeBinding(
            bridge_id=twin_bridge_id, agent_id="claude_code",
            agent_instance_id="agi-watch-twin-row", session_label="twin",
            parent_pid=1, agent_session_id=session_id,
        ),
    )
    past = (T0 - timedelta(seconds=10)).isoformat()
    _spawn_live(
        state, agent_instance_id="agi-worker-ambiguous-steward",
        lifecycle_state=LIFECYCLE_LIVE, report_by_override=past,
        spawned_by_instance_id=ledger_id,
    )
    _spawn_live(
        state, agent_instance_id="agi-worker-sharing-the-tick",
        lifecycle_state=LIFECYCLE_LIVE, report_by_override=past,
    )
    marked = sweep_overdue_sessions(state, peer_registry=reg, bridge_manager=mgr, now=T0)
    _check(
        marked == 2,
        f"an ambiguous session id does not raise out of the sweep -- both rows in "
        f"the tick are still marked (got {marked})",
    )
    _, first_events = mgr.get(first_bridge_id).events_after(-1)
    _, twin_events = mgr.get(twin_bridge_id).events_after(-1)
    _check(
        first_events == [] and twin_events == [],
        f"and NEITHER candidate is picked: a private steward notice delivered to "
        f"the wrong one of two sessions sharing a session id is the failure this "
        f"whole defect is about (first {first_events!r}, twin {twin_events!r})",
    )


def test_overdue_no_spawner_is_silent_noop() -> None:
    """An operator-hosted row (or any row with no recorded spawner) has no
    steward to notify by construction -- marked overdue, zero notify
    attempts, no crash."""
    state = _state()
    reg = _peer_registry()
    mgr = _bridge_manager()
    past = (T0 - timedelta(seconds=10)).isoformat()
    _spawn_live(
        state, agent_instance_id="agi-orphan", lifecycle_state=LIFECYCLE_LIVE,
        report_by_override=past,
    )
    marked = sweep_overdue_sessions(state, peer_registry=reg, bridge_manager=mgr, now=T0)
    _check(marked == 1, "a spawner-less row is still transitioned")
    _check(
        read_managed_session(state, "agi-orphan")["lifecycle_state"] == LIFECYCLE_OVERDUE,
        "the row reads 'overdue'",
    )


def test_overdue_unresolvable_spawner_is_best_effort() -> None:
    """A recorded spawner with no live binding (never registered, or
    already gone) is best-effort -- the row is still marked, no crash, no
    delivery."""
    state = _state()
    reg = _peer_registry()
    mgr = _bridge_manager()
    past = (T0 - timedelta(seconds=10)).isoformat()
    _spawn_live(
        state, agent_instance_id="agi-worker-ghost", lifecycle_state=LIFECYCLE_LIVE,
        report_by_override=past, spawned_by_instance_id="agi-steward-ghost",
    )
    marked = sweep_overdue_sessions(state, peer_registry=reg, bridge_manager=mgr, now=T0)
    _check(marked == 1, "a row with an unresolvable spawner is still transitioned")


def test_overdue_marks_without_notify_when_registry_absent() -> None:
    """peer_registry/bridge_manager are OPTIONAL (unlike the sibling
    dependency sweeps) -- an early-boot tick with neither available must
    still mark overdue rows; it just cannot notify."""
    state = _state()
    past = (T0 - timedelta(seconds=10)).isoformat()
    _spawn_live(
        state, agent_instance_id="agi-early-boot", lifecycle_state=LIFECYCLE_LIVE,
        report_by_override=past, spawned_by_instance_id="agi-steward-unreachable",
    )
    marked = sweep_overdue_sessions(state, now=T0)  # no peer_registry/bridge_manager at all
    _check(
        marked == 1,
        "the state transition still runs with no peer_registry/bridge_manager passed at all",
    )


# ---------------------------------------------------------------------------
# GAU-28 — the overdue leg's alarm leaves a first-party record, delivered or not
#
# Its only trace used to be a WARNING in a rotating log file: the leg returned
# on `binding is None` before append_event and before any write, so nothing
# durable existed at all. A trace that ages out is not a record — every audit of
# alarm loss that reads the notice store under-counted this leg to exactly zero,
# including any future audit of GAU-26's blast radius. GAU-26's fix makes the
# delivery SUCCEED; it does not make the FAILURE observable, so without this the
# resolver could regress silently afterwards.
# ---------------------------------------------------------------------------


def _overdue_records(state: StateManagementInterface) -> list[dict[str, Any]]:
    rows, _ = read_gauge_notice_records(state, notice_type="session_overdue_notice")
    return rows


def test_overdue_alarm_that_reached_nobody_is_still_recorded() -> None:
    """★ THE GAU-28 FIX. An unresolvable steward is a RECORDED OUTCOME, not an
    early return.

    Same fixture as ``test_overdue_unresolvable_spawner_is_best_effort``, which
    asserts the row is still marked; this asserts the half that was missing —
    that the alarm left something behind.

    MUTATION: restore the early ``return`` ahead of the record → this fails and
    the marking test does not, which is exactly how the defect survived."""
    state = _state()
    reg = _peer_registry()
    mgr = _bridge_manager()
    past = (T0 - timedelta(seconds=10)).isoformat()
    _spawn_live(
        state, agent_instance_id="agi-worker-unrecorded", lifecycle_state=LIFECYCLE_LIVE,
        report_by_override=past, spawned_by_instance_id="agi-steward-ghost",
    )
    marked = sweep_overdue_sessions(state, peer_registry=reg, bridge_manager=mgr, now=T0)
    _check(marked == 1, "the row is still transitioned")
    rows = _overdue_records(state)
    _check(
        len(rows) == 1,
        f"RED-vs-GREEN (GAU-28): the undelivered overdue alarm leaves exactly one "
        f"durable row (got {len(rows)}) -- before this fix its only trace was a "
        "WARNING in a log file that rotates",
    )
    row = rows[0] if rows else {}
    _check(
        row.get("delivery_outcome") == NOTICE_DELIVERY_NO_STEWARD_BINDING
        and row.get("steward_instance_id") is None,
        f"and it says WHY nobody got it, with a NULL steward meaning 'none was "
        f"resolved' rather than 'not recorded' (got {row.get('delivery_outcome')!r}, "
        f"steward {row.get('steward_instance_id')!r})",
    )
    _check(
        row.get("agent_instance_id") == "agi-worker-unrecorded",
        "the SUBJECT is the overdue session, never the steward it was addressed to",
    )


def test_overdue_delivered_alarm_records_the_steward_it_reached() -> None:
    """The other outcome, so the table can tell a delivered alarm from a lost
    one rather than only proving the failure case. Uses the GAU-26 watcher-held
    steward, which is the population that produced both defects."""
    state = _state()
    reg = _peer_registry()
    mgr = _bridge_manager()
    ledger_id = "agi-73ba7ce552285765b4716a1059326da0"
    watch_id, steward_bridge_id = _register_watcher_held_steward(
        state, reg, mgr, ledger_instance_id=ledger_id, agent_session_id=f"ases-{ledger_id}",
    )
    past = (T0 - timedelta(seconds=10)).isoformat()
    _spawn_live(
        state, agent_instance_id="agi-worker-recorded", lifecycle_state=LIFECYCLE_LIVE,
        report_by_override=past, spawned_by_instance_id=ledger_id,
    )
    sweep_overdue_sessions(state, peer_registry=reg, bridge_manager=mgr, now=T0)
    _, events = mgr.get(steward_bridge_id).events_after(-1)
    rows = _overdue_records(state)
    row = rows[0] if rows else {}
    _check(
        len(events) == 1
        and len(rows) == 1
        and row.get("delivery_outcome") == NOTICE_DELIVERY_APPENDED
        and row.get("steward_instance_id") == watch_id,
        f"a DELIVERED overdue alarm is recorded as 'appended' and names the "
        f"binding it reached (got {row.get('delivery_outcome')!r}, steward "
        f"{row.get('steward_instance_id')!r}, {len(events)} event(s))",
    )


def test_overdue_record_carries_the_measured_lateness() -> None:
    """The evidence columns, and what they mean for THIS leg: ``threshold_s``
    is 0.0 because the bound is the deadline itself, and ``observed_s`` is the
    seconds past ``report_by`` measured on the sweep's own clock.

    MUTATION: record the lateness against a re-derived 'now' instead of the
    sweep's clock → the number stops matching the fixture's deadline."""
    state = _state()
    reg = _peer_registry()
    mgr = _bridge_manager()
    past = (T0 - timedelta(seconds=137)).isoformat()
    _spawn_live(
        state, agent_instance_id="agi-worker-late-by-137", lifecycle_state=LIFECYCLE_LIVE,
        report_by_override=past, spawned_by_instance_id="agi-steward-ghost",
    )
    sweep_overdue_sessions(state, peer_registry=reg, bridge_manager=mgr, now=T0)
    rows = _overdue_records(state)
    row = rows[0] if rows else {}
    _check(
        row.get("observed_s") == 137.0 and row.get("threshold_s") == 0.0,
        f"the record carries the MEASURED lateness (137s past report_by) against "
        f"a zero-second bound, not a re-derived or defaulted number (got "
        f"observed_s={row.get('observed_s')!r}, threshold_s={row.get('threshold_s')!r})",
    )
    _check(
        row.get("emitted_at") == T0.isoformat(),
        f"and it is stamped with the sweep's own clock, the moment the sweep "
        f"DECIDED to fire (got {row.get('emitted_at')!r})",
    )


def test_overdue_terminates_stuck_spawning_row() -> None:
    """A definitive native-death observation retains the termination path."""
    state = _state()
    past = (T0 - timedelta(seconds=10)).isoformat()
    _spawn_live(
        state, agent_instance_id="agi-spawn-stuck", lifecycle_state=LIFECYCLE_SPAWNING,
        report_by_override=past,
    )
    marked = sweep_overdue_sessions(state, now=T0, host_alive_probe=lambda _row: False)
    _check(
        marked == 1,
        "RED-vs-GREEN: a stuck 'spawning' row past its report_by deadline IS "
        "swept (before this fix, sweep_overdue_sessions scanned only "
        "live/idle and this row sat in 'spawning' forever)",
    )
    _check(
        read_managed_session(state, "agi-spawn-stuck")["lifecycle_state"] == LIFECYCLE_TERMINATED,
        "the row reaches 'terminated' directly -- 'overdue' is not a legal "
        "edge from 'spawning'",
    )


def test_overdue_skips_spawning_row_with_future_deadline() -> None:
    state = _state()
    future = (T0 + timedelta(seconds=300)).isoformat()
    _spawn_live(
        state, agent_instance_id="agi-spawn-not-yet", lifecycle_state=LIFECYCLE_SPAWNING,
        report_by_override=future,
    )
    marked = sweep_overdue_sessions(state, now=T0)
    _check(marked == 0, "a spawning row's report_by still in the future is not swept")
    _check(
        read_managed_session(state, "agi-spawn-not-yet")["lifecycle_state"] == LIFECYCLE_SPAWNING,
        "its lifecycle_state is untouched",
    )


def test_overdue_spawning_alive_row_is_extended_not_reaped() -> None:
    """2026-08-13 (live-measured): a spawning row past its deadline whose host
    process is OBSERVED ALIVE is a live session whose registration never
    completed, not an orphaned spawn — a tmux worker productive for hours was
    reaped mid-programme by the deadline alone. Observed-alive earns a
    deadline re-arm + a distinct steward notice; nothing is terminated.

    RED MUTATION: drop the probe branch (always terminate) — this leg's
    lifecycle assertion goes red; or notify without re-arming — the deadline
    assertion goes red."""
    state = _state()
    reg = _peer_registry()
    mgr = _bridge_manager()
    _spawn_live(state, agent_instance_id="agi-alive-steward")
    state.update_state(
        AGENT_ROLE_BINDING_NAMESPACE,
        {"table": "managed_session", "filters": {"agent_instance_id": "agi-alive-steward"}},
        {"agent_id": "claude_code"},
    )
    steward_bridge_id = _register_live_binding(reg, mgr, agent_instance_id="agi-alive-steward")
    past = (T0 - timedelta(seconds=10)).isoformat()
    _spawn_live(
        state, agent_instance_id="agi-alive-unreg", lifecycle_state=LIFECYCLE_SPAWNING,
        report_by_seconds=3600, report_by_override=past,
        spawned_by_instance_id="agi-alive-steward",
    )
    marked = sweep_overdue_sessions(
        state, peer_registry=reg, bridge_manager=mgr, now=T0,
        host_alive_probe=lambda _row: True,
    )
    _check(marked == 0, "an observed-alive spawning row is NOT counted as swept")
    row = read_managed_session(state, "agi-alive-unreg")
    _check(
        row["lifecycle_state"] == LIFECYCLE_SPAWNING,
        "observed-alive: the row stays 'spawning', never terminated",
    )
    new_report_by = str(row.get("report_by") or "")
    _check(
        new_report_by > T0.isoformat(),
        f"observed-alive: report_by was re-armed into the future (got {new_report_by!r})",
    )
    _, events = mgr.get(steward_bridge_id).events_after(-1)
    _check(
        len(events) == 1
        and events[0].event_type == "session_spawn_unregistered_notice"
        and "agi-alive-unreg" in events[0].content
        and "OBSERVED ALIVE" in events[0].content,
        f"the steward gets exactly one spawn-UNREGISTERED notice (distinct "
        f"class from orphaned) naming the row (got {events!r})",
    )


def test_overdue_spawning_alive_has_no_lifetime_cap() -> None:
    """Elapsed report windows never terminate an observed-live host."""
    state = _state()
    past = (T0 - timedelta(seconds=10)).isoformat()
    _spawn_live(
        state, agent_instance_id="agi-alive-exhausted", lifecycle_state=LIFECYCLE_SPAWNING,
        report_by_seconds=300, report_by_override=past,
    )
    # Age the spawn timestamp past the patience bound (4 windows x 300s).
    state.update_state(
        AGENT_ROLE_BINDING_NAMESPACE,
        {"table": "managed_session", "filters": {"agent_instance_id": "agi-alive-exhausted"}},
        {"last_transition_at": (T0 - timedelta(seconds=300 * 5)).isoformat()},
    )
    marked = sweep_overdue_sessions(state, now=T0, host_alive_probe=lambda _row: True)
    _check(marked == 0, "an observed-live host remains spawning beyond the old patience limit")
    _check(
        read_managed_session(state, "agi-alive-exhausted")["lifecycle_state"]
        == LIFECYCLE_SPAWNING,
        "elapsed time never terminates an observed-live session",
    )


def test_overdue_spawning_operator_host_alive_is_not_evidence() -> None:
    """An operator host is unobservable: elapsed time never proves death."""
    state = _state()
    past = (T0 - timedelta(seconds=10)).isoformat()
    _spawn_live(
        state, agent_instance_id="agi-op-host", lifecycle_state=LIFECYCLE_SPAWNING,
        report_by_override=past,
    )
    marked = sweep_overdue_sessions(state, now=T0)
    _check(
        marked == 0,
        "an unobservable spawning row past deadline is preserved — the "
        "operator driver's vacuous alive() is never liveness evidence",
    )


def test_spawning_native_probe_never_infers_death_from_age_or_fault() -> None:
    key = (session_hosts.AGENT_RUNTIME_CODEX, _TEST_MANAGED_HOST)
    prior = session_hosts._REGISTRY.get(key)  # noqa: SLF001
    try:
        for observation in (True, False, RuntimeError("probe unavailable")):
            state = _state()
            session_hosts._REGISTRY[key] = _LivenessDriver(observation)  # noqa: SLF001
            _spawn_live(state, agent_instance_id="agi-native-proof", lifecycle_state=LIFECYCLE_SPAWNING,
                        report_by_override=(T0 - timedelta(days=3650)).isoformat())
            state.update_state(
                AGENT_ROLE_BINDING_NAMESPACE,
                {"table": "managed_session", "filters": {"agent_instance_id": "agi-native-proof"}},
                {"host": _TEST_MANAGED_HOST, "host_ref": "native-proof",
                 "agent_runtime": session_hosts.AGENT_RUNTIME_CODEX,
                 "last_transition_at": (T0 - timedelta(days=3650)).isoformat()},
            )
            marked = sweep_overdue_sessions(state, now=T0)
            actual = read_managed_session(state, "agi-native-proof")["lifecycle_state"]
            expected = LIFECYCLE_TERMINATED if observation is False else LIFECYCLE_SPAWNING
            _check(actual == expected and marked == int(observation is False),
                   f"native spawning observation {observation!r} alone controls termination, never age")
    finally:
        if prior is None:
            session_hosts._REGISTRY.pop(key, None)  # noqa: SLF001
        else:
            session_hosts._REGISTRY[key] = prior  # noqa: SLF001


def test_overdue_spawning_notifies_steward_of_orphan() -> None:
    """The steward (spawner) of an orphaned spawn is very likely still
    alive and would want to know its spawn never came up -- distinct event
    type from the live/idle overdue notice (a receiver must be able to
    tell the two classes apart: 'went quiet' vs 'never came up')."""
    state = _state()
    reg = _peer_registry()
    mgr = _bridge_manager()
    _spawn_live(state, agent_instance_id="agi-spawn-steward")
    state.update_state(
        AGENT_ROLE_BINDING_NAMESPACE,
        {"table": "managed_session", "filters": {"agent_instance_id": "agi-spawn-steward"}},
        {"agent_id": "claude_code"},
    )
    steward_bridge_id = _register_live_binding(reg, mgr, agent_instance_id="agi-spawn-steward")
    past = (T0 - timedelta(seconds=10)).isoformat()
    _spawn_live(
        state, agent_instance_id="agi-spawn-orphan", lifecycle_state=LIFECYCLE_SPAWNING,
        report_by_override=past, spawned_by_instance_id="agi-spawn-steward",
    )
    marked = sweep_overdue_sessions(state, peer_registry=reg, bridge_manager=mgr, now=T0,
                                    host_alive_probe=lambda _row: False)
    _check(marked == 1, "the orphaned spawning row is still transitioned")
    _, events = mgr.get(steward_bridge_id).events_after(-1)
    _check(
        len(events) == 1
        and events[0].event_type == "session_spawn_orphaned_notice"
        and "agi-spawn-orphan" in events[0].content,
        f"the steward's bridge gets exactly one delivered spawn-orphaned "
        f"notice naming the orphaned session (got {events!r})",
    )


# ---------------------------------------------------------------------------
# sweep_deadline_dependencies
# ---------------------------------------------------------------------------


def _seed_dependency(
    state: StateManagementInterface,
    *,
    row_id: str,
    condition_kind: str,
    condition_ref: str,
    waiter_instance_id: str = "",
    waiter_lane_id: str = "",
    fired_at: str | None = None,
) -> None:
    state.write_state(
        AGENT_ROLE_BINDING_NAMESPACE,
        {
            "table": TABLE_SESSION_DEPENDENCY,
            "record": {
                "id": row_id,
                "external_id": row_id,
                "condition_kind": condition_kind,
                "condition_ref": condition_ref,
                "waiter_instance_id": waiter_instance_id,
                "waiter_lane_id": waiter_lane_id,
                "fired_at": fired_at,
            },
        },
    )


def test_deadline_dependency_not_yet_due_skipped() -> None:
    state = _state()
    reg = _peer_registry()
    mgr = _bridge_manager()
    future = (T0 + timedelta(seconds=60)).isoformat()
    _seed_dependency(
        state, row_id="sdp-future", condition_kind=CONDITION_DEADLINE, condition_ref=future,
    )
    fired = sweep_deadline_dependencies(state, peer_registry=reg, bridge_manager=mgr, now=T0)
    _check(fired == 0, "a deadline still in the future is not fired")


def test_deadline_dependency_fires_and_delivers() -> None:
    state = _state()
    reg = _peer_registry()
    mgr = _bridge_manager()
    _spawn_live(state, agent_instance_id="agi-waiter")
    # The D1 registration hook that would normally backfill agent_id onto a
    # managed_session row does not exist yet (Reviewer-A's independent
    # finding, out of scope for this slice — headless-adapter work) — set it
    # directly here to exercise the delivery path AS IF that hook existed.
    state.update_state(
        AGENT_ROLE_BINDING_NAMESPACE,
        {"table": "managed_session", "filters": {"agent_instance_id": "agi-waiter"}},
        {"agent_id": "claude_code"},
    )
    bridge_id = mgr.open(solet_name="", parent_pid=1).bridge_id
    reg.register(
        BridgeBinding(
            bridge_id=bridge_id, agent_id="claude_code", agent_instance_id="agi-waiter",
            session_label="Waiter", parent_pid=1,
        ),
    )
    past = (T0 - timedelta(seconds=5)).isoformat()
    _seed_dependency(
        state, row_id="sdp-1", condition_kind=CONDITION_DEADLINE, condition_ref=past,
        waiter_instance_id="agi-waiter",
    )
    fired = sweep_deadline_dependencies(state, peer_registry=reg, bridge_manager=mgr, now=T0)
    _check(fired == 1, "a past-deadline armed edge is fired exactly once")
    rows = state.query_state(
        AGENT_ROLE_BINDING_NAMESPACE,
        {"table": TABLE_SESSION_DEPENDENCY, "filters": {"id": "sdp-1"}},
    )["data"]["records"]
    _check(rows[0]["fired_at"] is not None, "fired_at is stamped on the edge")
    _, events = mgr.get(bridge_id).events_after(-1)
    _check(
        len(events) == 1
        and events[0].event_type == "session_dependency_wake"
        and "deadline" in events[0].content,
        f"the waiter's bridge gets exactly one delivered wake event (got {events!r})",
    )
    again = sweep_deadline_dependencies(state, peer_registry=reg, bridge_manager=mgr, now=T0)
    _, events2 = mgr.get(bridge_id).events_after(events[-1].cursor)
    _check(
        again == 0 and events2 == [],
        "a re-run does not re-fire or re-deliver an already-fired edge",
    )


def test_deadline_dependency_unmanaged_waiter_still_delivers() -> None:
    """RED-FIRST (phase-2 slice 3, the phase-1 unified finding — seat
    log-proven 2026-08-05 00:13:27Z on edge sdp-2nm84y6h8k21s): a
    watch-transport waiter registered directly in the peer registry (no
    ``managed_session`` row of its own -- the dominant shape for a watch-arm
    subject or any other non-spawn_session-managed session) must still get
    the wake delivered. Before this fix, ``_deliver_dependency_wake``
    resolved the waiter's ``agent_id`` ONLY via its ``managed_session`` row
    (``_managed_session_agent_id``) and returned on an empty result BEFORE
    ever consulting the peer registry -- so an unmanaged, watch-registered
    waiter got a 'no managed_session row' WARNING and no delivery, for all
    three condition kinds. Mirrors the identical fix already landed for
    ``_notify_steward_of_overdue`` (see
    ``test_overdue_notifies_unmanaged_steward`` above) and the observed live
    identity shape (``agi-watch-...``)."""
    state = _state()
    reg = _peer_registry()
    mgr = _bridge_manager()
    # The waiter is registered in the peer registry directly -- no
    # _spawn_live() call for it at all, so it has NO managed_session row.
    bridge_id = _register_live_binding(
        reg, mgr, agent_instance_id="agi-watch-92a6ae0e3e134e5e11774007",
    )
    past = (T0 - timedelta(seconds=5)).isoformat()
    _seed_dependency(
        state, row_id="sdp-unmanaged-waiter", condition_kind=CONDITION_DEADLINE,
        condition_ref=past, waiter_instance_id="agi-watch-92a6ae0e3e134e5e11774007",
    )
    fired = sweep_deadline_dependencies(state, peer_registry=reg, bridge_manager=mgr, now=T0)
    _check(fired == 1, "the deadline edge fires")
    _, events = mgr.get(bridge_id).events_after(-1)
    _check(
        len(events) == 1
        and events[0].event_type == "session_dependency_wake"
        and "deadline" in events[0].content,
        f"RED-vs-GREEN: an UNMANAGED (watch-registered) waiter still gets the "
        f"wake delivered (got {events!r}) -- before this fix, resolution went "
        "only through the waiter's managed_session row and silently found "
        "nothing to notify",
    )


def test_deadline_dependency_unresolvable_waiter_is_best_effort() -> None:
    state = _state()
    reg = _peer_registry()
    mgr = _bridge_manager()
    # No managed_session row at all for this waiter — agent_id is unknowable.
    past = (T0 - timedelta(seconds=5)).isoformat()
    _seed_dependency(
        state, row_id="sdp-2", condition_kind=CONDITION_DEADLINE, condition_ref=past,
        waiter_instance_id="agi-ghost",
    )
    fired = sweep_deadline_dependencies(state, peer_registry=reg, bridge_manager=mgr, now=T0)
    _check(
        fired == 1,
        "the edge still fires (state) even though delivery cannot be resolved",
    )


def test_deadline_dependency_lane_scoped_is_logged_noop() -> None:
    state = _state()
    reg = _peer_registry()
    mgr = _bridge_manager()
    past = (T0 - timedelta(seconds=5)).isoformat()
    _seed_dependency(
        state, row_id="sdp-3", condition_kind=CONDITION_DEADLINE, condition_ref=past,
        waiter_lane_id="lane-only",
    )
    fired = sweep_deadline_dependencies(state, peer_registry=reg, bridge_manager=mgr, now=T0)
    _check(
        fired == 1,
        "a lane-scoped edge (no waiter_instance_id) still fires; delivery is a no-op, "
        "not a crash",
    )


# ---------------------------------------------------------------------------
# sweep_lane_closed_dependencies (Dawn ruling 2026-08-03, arm-124065ee —
# 'lane_closed' replaced the unbuildable 'lane_landed' spec kind)
# ---------------------------------------------------------------------------


def test_lane_closed_empty_lane_is_not_closed() -> None:
    state = _state()
    reg = _peer_registry()
    mgr = _bridge_manager()
    _seed_dependency(
        state, row_id="sdp-lane-empty", condition_kind=CONDITION_LANE_CLOSED,
        condition_ref="lane-never-spawned",
    )
    fired = sweep_lane_closed_dependencies(state, peer_registry=reg, bridge_manager=mgr, now=T0)
    _check(
        fired == 0,
        "a lane_id with ZERO managed_session rows is NOT closed — no vacuous "
        "truth on an empty set",
    )


def test_lane_closed_open_while_any_session_non_terminal() -> None:
    state = _state()
    reg = _peer_registry()
    mgr = _bridge_manager()
    _spawn_live(state, agent_instance_id="agi-lane-a", lifecycle_state=LIFECYCLE_LIVE)
    state.update_state(
        AGENT_ROLE_BINDING_NAMESPACE,
        {"table": "managed_session", "filters": {"agent_instance_id": "agi-lane-a"}},
        {"lane_id": "lane-mixed"},
    )
    terminate_session(state, agent_instance_id="agi-lane-a", directed_by="operator:none")
    insert_managed_session(
        state,
        ManagedSessionSpec(
            agent_instance_id="agi-lane-b", lane_id="lane-mixed", brief_ref="",
            work_class=WORK_CLASS_READ_ONLY, budget_line="b1", host="operator",
        ),
    )  # left 'spawning' — non-terminal
    _seed_dependency(
        state, row_id="sdp-lane-mixed", condition_kind=CONDITION_LANE_CLOSED,
        condition_ref="lane-mixed",
    )
    fired = sweep_lane_closed_dependencies(state, peer_registry=reg, bridge_manager=mgr, now=T0)
    _check(
        fired == 0,
        "one terminal + one non-terminal managed_session row for the lane -> "
        "NOT closed yet",
    )


def test_lane_closed_fires_when_every_session_terminal() -> None:
    state = _state()
    reg = _peer_registry()
    mgr = _bridge_manager()
    for agi in ("agi-lane-x", "agi-lane-y"):
        _spawn_live(state, agent_instance_id=agi, lifecycle_state=LIFECYCLE_LIVE)
        state.update_state(
            AGENT_ROLE_BINDING_NAMESPACE,
            {"table": "managed_session", "filters": {"agent_instance_id": agi}},
            {"lane_id": "lane-done"},
        )
        terminate_session(state, agent_instance_id=agi, directed_by="operator:none")
    _seed_dependency(
        state, row_id="sdp-lane-done", condition_kind=CONDITION_LANE_CLOSED,
        condition_ref="lane-done",
    )
    fired = sweep_lane_closed_dependencies(state, peer_registry=reg, bridge_manager=mgr, now=T0)
    _check(fired == 1, "every managed_session row for the lane is terminal -> fires")
    again = sweep_lane_closed_dependencies(state, peer_registry=reg, bridge_manager=mgr, now=T0)
    _check(again == 0, "a re-run does not re-fire an already-fired lane_closed edge")


# ---------------------------------------------------------------------------
# SessionRoleClaimPruner
# ---------------------------------------------------------------------------


def _seed_claim(
    state: StateManagementInterface, *, agent_session_id: str, held_role: str,
) -> None:
    state.write_state(
        AGENT_ROLE_BINDING_NAMESPACE,
        {
            "table": TABLE_SESSION_ROLE_CLAIM,
            "record": {
                "id": f"src-{agent_session_id}",
                "external_id": session_role_claim_external_id(agent_session_id),
                "agent_session_id": agent_session_id,
                "held_role": held_role,
                "agent_instance_id": f"agi-{agent_session_id}",
                "claimed_at": T0.isoformat(),
            },
        },
    )


def _claim_rows(state: StateManagementInterface) -> list[dict[str, Any]]:
    return [
        r for r in state.rows(AGENT_ROLE_BINDING_NAMESPACE, TABLE_SESSION_ROLE_CLAIM)
        if not r.get("is_deleted")
    ]


def test_pruner_terminal_managed_session_pruned_immediately() -> None:
    state = _state()
    reg = _peer_registry()
    _spawn_live(state, agent_instance_id="agi-term")
    state.update_state(
        AGENT_ROLE_BINDING_NAMESPACE,
        {"table": "managed_session", "filters": {"agent_instance_id": "agi-term"}},
        {"agent_session_id": "sess-term"},
    )
    terminate_session(state, agent_instance_id="agi-term", directed_by="operator:none")
    _seed_claim(state, agent_session_id="sess-term", held_role="Some-Lane")
    pruner = SessionRoleClaimPruner(clock=lambda: T0)
    pruned = pruner.sweep(state, peer_registry=reg)
    _check(
        pruned == 1 and _claim_rows(state) == [],
        "a claim whose managed_session is terminal is pruned with NO grace wait",
    )


def test_pruner_live_managed_session_never_pruned() -> None:
    state = _state()
    reg = _peer_registry()
    _spawn_live(state, agent_instance_id="agi-alive")
    state.update_state(
        AGENT_ROLE_BINDING_NAMESPACE,
        {"table": "managed_session", "filters": {"agent_instance_id": "agi-alive"}},
        {"agent_session_id": "sess-alive"},
    )
    _seed_claim(state, agent_session_id="sess-alive", held_role="Some-Lane")
    pruner = SessionRoleClaimPruner(clock=lambda: T0 + timedelta(days=365))
    pruned = pruner.sweep(state, peer_registry=reg)
    _check(
        pruned == 0 and len(_claim_rows(state)) == 1,
        "a claim whose managed_session is non-terminal is NEVER pruned (ledger-authoritative "
        "alive), regardless of elapsed time",
    )


def test_pruner_live_registered_session_never_pruned() -> None:
    state = _state()
    reg = _peer_registry()
    mgr = _bridge_manager()
    bridge_id = mgr.open(solet_name="", parent_pid=1).bridge_id
    reg.register(
        BridgeBinding(
            bridge_id=bridge_id, agent_id="claude_code", agent_instance_id="agi-reg",
            session_label="Reg", parent_pid=1, agent_session_id="sess-reg",
        ),
    )
    _seed_claim(state, agent_session_id="sess-reg", held_role="Some-Lane")
    pruner = SessionRoleClaimPruner(clock=lambda: T0 + timedelta(days=365))
    pruned = pruner.sweep(state, peer_registry=reg)
    _check(
        pruned == 0 and len(_claim_rows(state)) == 1,
        "no managed_session row, but a LIVE registry binding for the session -> never pruned",
    )


def test_pruner_absence_within_grace_window_not_pruned() -> None:
    state = _state()
    reg = _peer_registry()
    _seed_claim(state, agent_session_id="sess-ghost", held_role="Some-Lane")
    clock_value = {"now": T0}
    pruner = SessionRoleClaimPruner(grace_window_s=300, clock=lambda: clock_value["now"])
    pruned = pruner.sweep(state, peer_registry=reg)
    _check(
        pruned == 0 and len(_claim_rows(state)) == 1,
        "genuine absence (no managed_session, no live binding) within the grace window "
        "is NOT pruned — the blue-green-bounce guard",
    )
    clock_value["now"] = T0 + timedelta(seconds=100)
    pruned2 = pruner.sweep(state, peer_registry=reg)
    _check(
        pruned2 == 0 and len(_claim_rows(state)) == 1,
        "still within the window on the next tick -> still not pruned",
    )


def test_pruner_absence_past_grace_window_pruned() -> None:
    state = _state()
    reg = _peer_registry()
    _seed_claim(state, agent_session_id="sess-stale", held_role="Some-Lane")
    clock_value = {"now": T0}
    pruner = SessionRoleClaimPruner(grace_window_s=300, clock=lambda: clock_value["now"])
    pruner.sweep(state, peer_registry=reg)  # first-observed-absent stamped at T0
    clock_value["now"] = T0 + timedelta(seconds=301)
    pruned = pruner.sweep(state, peer_registry=reg)
    _check(
        pruned == 1 and _claim_rows(state) == [],
        "absence past the grace window IS pruned",
    )


def test_pruner_pages_claims_without_a_target_query_state_read() -> None:
    """More than two provider pages preserve prune and grace-map semantics."""

    class _NoClaimQueryState(CapEnforcingState):
        def query_state(self, namespace: str, query: dict[str, Any]) -> dict[str, Any]:
            if query.get("table") == TABLE_SESSION_ROLE_CLAIM:
                raise AssertionError("D1 must page session_role_claim, never query_state it")
            return super().query_state(namespace, query)

    inner = _state()
    state = _NoClaimQueryState(inner)
    for index in range(205):
        session_id = f"sess-terminal-{index:03d}"
        _seed_claim(state, agent_session_id=session_id, held_role="Some-Lane")
        inner.write_state(
            AGENT_ROLE_BINDING_NAMESPACE,
            {
                "table": "managed_session",
                "record": {
                    "agent_session_id": session_id,
                    "agent_instance_id": f"agi-{session_id}",
                    "lifecycle_state": LIFECYCLE_TERMINATED,
                },
            },
        )
    _seed_claim(state, agent_session_id="sess-absent", held_role="Some-Lane")
    _spawn_live(inner, agent_instance_id="agi-live-page")
    inner.update_state(
        AGENT_ROLE_BINDING_NAMESPACE,
        {"table": "managed_session", "filters": {"agent_instance_id": "agi-live-page"}},
        {"agent_session_id": "sess-live-page"},
    )
    _seed_claim(state, agent_session_id="sess-live-page", held_role="Some-Lane")

    pruner = SessionRoleClaimPruner(grace_window_s=300, clock=lambda: T0)
    first_pruned = pruner.sweep(state, peer_registry=_peer_registry())
    _check(
        first_pruned == 205 and {row["agent_session_id"] for row in _claim_rows(state)}
        == {"sess-absent", "sess-live-page"},
        "D1 pages >200 claims: terminal rows prune while absent-grace and live rows survive",
    )
    _check(
        "sess-absent" in pruner._first_absent_at,
        "the absent row is tracked for grace even when terminal rows span pages",
    )
    inner.delete_records(
        AGENT_ROLE_BINDING_NAMESPACE,
        {
            "table": TABLE_SESSION_ROLE_CLAIM,
            "filters": {"agent_session_id": "sess-absent"},
            "soft_delete": False,
        },
    )
    pruner.sweep(state, peer_registry=_peer_registry())
    _check(
        "sess-absent" not in pruner._first_absent_at
        and {row["agent_session_id"] for row in _claim_rows(state)} == {"sess-live-page"},
        "grace-map cleanup forgets an absent claim that vanishes across paged sweeps",
    )


# ---------------------------------------------------------------------------
# retire_session crash-mid-retire redrivability (Coordinator-Dawn fold-in)
# ---------------------------------------------------------------------------


def test_retire_session_crash_mid_retire_is_redrivable() -> None:
    """Simulates a crash BETWEEN retire_session's steps: the row is already
    'terminated' (step 1 done, by a prior crashed attempt or a plain
    terminate_session call) and an armed session_terminal dependency edge is
    still un-fired (step 3 not done) — re-running retire_session must finish
    the job: fire the pending edge and complete terminated -> retired.
    """
    with tempfile.TemporaryDirectory() as raw:
        with RecordedLaneWorktreeFixture(Path(raw)) as fixture:
            state = _state()
            _spawn_live(state, agent_instance_id="agi-crash")
            terminate_session(state, agent_instance_id="agi-crash", directed_by="operator:none")
            _seed_dependency(
                state, row_id="sdp-crash", condition_kind=CONDITION_SESSION_TERMINAL,
                condition_ref="agi-crash", waiter_instance_id="agi-waiter-crash",
            )
            _check(
                read_managed_session(state, "agi-crash")["lifecycle_state"] == LIFECYCLE_TERMINATED,
                "setup: the row is 'terminated' but NOT yet 'retired' (simulating the crash point)",
            )
            result = retire_session(state, agent_instance_id="agi-crash", directed_by="operator:none")
            _check(
                result == {"already_retired": False, "dependencies_fired": 1},
                f"re-running retire_session finishes the job: fires the pending edge and "
                f"completes the transition (got {result!r})",
            )
            _check(
                read_managed_session(state, "agi-crash")["lifecycle_state"] == LIFECYCLE_RETIRED,
                "the row reaches 'retired' despite the simulated mid-retire crash",
            )
            _check(
                fixture.has_recorded_provisioning() is False and bool(fixture.retirement_calls),
                "crash-mid-retire teardown is recorded through a temp-root-contained fixture",
            )


# ---------------------------------------------------------------------------
# W4A registration watchdog (sweep_unregistered_spawning_sessions)
# ---------------------------------------------------------------------------


def _spawn_unregistered(
    state: StateManagementInterface,
    *,
    agent_instance_id: str,
    spawned_at: datetime,
    host: str = "headless",
    spawned_by_instance_id: str = "",
    degraded_hooks_acknowledged: bool = False,
) -> None:
    """A ``spawning`` row whose spawn timestamp we CONTROL, so the watchdog
    tests advance a clock across the bound instead of asserting on a static
    row. ``last_transition_at`` is the anchor the bound is measured from."""
    insert_managed_session(
        state,
        ManagedSessionSpec(
            agent_instance_id=agent_instance_id, lane_id="lane-z", brief_ref="",
            work_class=WORK_CLASS_READ_ONLY, budget_line="b1", host=host,
            spawned_by_instance_id=spawned_by_instance_id,
            degraded_hooks_acknowledged=degraded_hooks_acknowledged,
        ),
    )
    state.update_state(
        AGENT_ROLE_BINDING_NAMESPACE,
        {"table": "managed_session", "filters": {"agent_instance_id": agent_instance_id}},
        {"last_transition_at": spawned_at.isoformat()},
    )


def test_registration_within_bound_is_not_marked() -> None:
    """The advancing half #1: the SAME row, read before the bound, is clean."""
    state = _state()
    _spawn_unregistered(state, agent_instance_id="agi-fresh", spawned_at=T0)
    marked = sweep_unregistered_spawning_sessions(
        state, now=T0 + timedelta(seconds=DEFAULT_REGISTRATION_BOUND_S - 1),
    )
    _check(marked == 0, "a spawning row inside the registration bound is not marked")
    _check(
        not read_managed_session(state, "agi-fresh").get("registration_overdue_at"),
        "and carries no registration_overdue_at",
    )


def test_registration_past_bound_marks_field_not_state() -> None:
    """The advancing half #2 AND the design call itself: past the bound the
    row is MARKED but its lifecycle_state is untouched. A new lifecycle state
    would have destroyed the fact that the row is still spawning; the field
    keeps both facts."""
    state = _state()
    _spawn_unregistered(state, agent_instance_id="agi-deaf", spawned_at=T0)
    marked = sweep_unregistered_spawning_sessions(
        state, now=T0 + timedelta(seconds=DEFAULT_REGISTRATION_BOUND_S + 1),
    )
    _check(marked == 1, "the same row, past the bound, is marked")
    row = read_managed_session(state, "agi-deaf")
    _check(bool(row.get("registration_overdue_at")), "registration_overdue_at is stamped")
    _check(
        row["lifecycle_state"] == LIFECYCLE_SPAWNING,
        "FIELD-NOT-STATE: lifecycle_state is still 'spawning' -- the watchdog "
        "attributes, it does not transition",
    )
    reason = str(row.get("registration_overdue_reason") or "")
    _check(
        "has not registered" in reason and "registration hook has not run" in reason,
        f"the reason states what was OBSERVED at the seam (got {reason!r})",
    )


def test_registration_watchdog_never_reaps() -> None:
    """The other half of the leg-separation contract: unlike the report_by
    spawning leg, this one kills nothing, even long past the bound."""
    state = _state()
    _spawn_unregistered(state, agent_instance_id="agi-alive", spawned_at=T0)
    sweep_unregistered_spawning_sessions(state, now=T0 + timedelta(days=365))
    _check(
        read_managed_session(state, "agi-alive")["lifecycle_state"] == LIFECYCLE_SPAWNING,
        "a year past the bound the row is STILL 'spawning' -- attribution, never the reaper",
    )


def test_registration_fires_without_any_report_by() -> None:
    """Independence from the report-or-die contract: an operator-host row is
    given no report_by by insert_managed_session, and the report_by spawning
    leg skips such a row by design. The watchdog must not inherit that blind
    spot -- its bound is registration, not the work deadline."""
    state = _state()
    _spawn_unregistered(state, agent_instance_id="agi-nocontract", spawned_at=T0, host="operator")
    _check(
        not read_managed_session(state, "agi-nocontract").get("report_by"),
        "setup: the row genuinely has no report_by",
    )
    _check(
        sweep_overdue_sessions(state, now=T0 + timedelta(days=365)) == 0,
        "setup: the report_by spawning leg cannot see it (no contract)",
    )
    marked = sweep_unregistered_spawning_sessions(state, now=T0 + timedelta(days=365))
    _check(marked == 1, "the registration watchdog marks it anyway")
    _check(
        bool(read_managed_session(state, "agi-nocontract").get("registration_overdue_at")),
        "and the mark is actually ON THE ROW, not merely counted by the sweep",
    )


def test_registration_mark_is_idempotent_and_keeps_first_observation() -> None:
    state = _state()
    _spawn_unregistered(state, agent_instance_id="agi-once", spawned_at=T0)
    first_clock = T0 + timedelta(seconds=DEFAULT_REGISTRATION_BOUND_S + 1)
    sweep_unregistered_spawning_sessions(state, now=first_clock)
    stamped = read_managed_session(state, "agi-once")["registration_overdue_at"]
    again = sweep_unregistered_spawning_sessions(state, now=first_clock + timedelta(hours=5))
    _check(again == 0, "a second sweep does not re-mark an already-marked row")
    _check(
        read_managed_session(state, "agi-once")["registration_overdue_at"] == stamped,
        "the field records the FIRST observation ('since when'), not the last tick",
    )


def test_registration_late_registration_clears_the_mark() -> None:
    """A worker that registers LATE is a different story from one that never
    did, so the mark clears rather than leaving the row permanently deaf."""
    state = _state()
    _spawn_unregistered(state, agent_instance_id="agi-late", spawned_at=T0)
    sweep_unregistered_spawning_sessions(
        state, now=T0 + timedelta(seconds=DEFAULT_REGISTRATION_BOUND_S + 1),
    )
    _check(
        bool(read_managed_session(state, "agi-late").get("registration_overdue_at")),
        "setup: the row is marked registration-overdue",
    )
    backfill_registration(
        state, agent_instance_id="agi-late", agent_id="claude_code",
        agent_session_id="ases-agi-late",
    )
    row = read_managed_session(state, "agi-late")
    _check(not row.get("registration_overdue_at"), "a late registration clears the mark")
    _check(row["lifecycle_state"] == LIFECYCLE_LIVE, "and the row completes spawning->live")


def test_registration_non_spawning_rows_are_never_marked() -> None:
    state = _state()
    _spawn_live(state, agent_instance_id="agi-running", lifecycle_state=LIFECYCLE_LIVE)
    marked = sweep_unregistered_spawning_sessions(state, now=T0 + timedelta(days=365))
    _check(marked == 0, "a row that already registered (live) is never marked")


def test_registration_acknowledged_degraded_is_marked_but_says_so() -> None:
    """Item 3's half of the story: an acknowledged degraded spawn is still
    observed and still recorded -- honesty about what happened -- but the
    reason says the risk was accepted, so it does not read as a surprise."""
    state = _state()
    _spawn_unregistered(
        state, agent_instance_id="agi-degraded", spawned_at=T0,
        degraded_hooks_acknowledged=True,
    )
    marked = sweep_unregistered_spawning_sessions(
        state, now=T0 + timedelta(seconds=DEFAULT_REGISTRATION_BOUND_S + 1),
    )
    _check(marked == 1, "an acknowledged-degraded row is still marked (the fact is still true)")
    _check(
        "ACKNOWLEDGED" in str(
            read_managed_session(state, "agi-degraded").get("registration_overdue_reason") or "",
        ),
        "but its reason records that this was an accepted risk",
    )


def test_registration_notifies_steward_with_distinct_event() -> None:
    state = _state()
    reg = _peer_registry()
    mgr = _bridge_manager()
    _spawn_live(state, agent_instance_id="agi-steward-w4a")
    state.update_state(
        AGENT_ROLE_BINDING_NAMESPACE,
        {"table": "managed_session", "filters": {"agent_instance_id": "agi-steward-w4a"}},
        {"agent_id": "claude_code"},
    )
    steward_bridge_id = _register_live_binding(reg, mgr, agent_instance_id="agi-steward-w4a")
    _spawn_unregistered(
        state, agent_instance_id="agi-deaf-child", spawned_at=T0,
        spawned_by_instance_id="agi-steward-w4a",
    )
    sweep_unregistered_spawning_sessions(
        state, peer_registry=reg, bridge_manager=mgr,
        now=T0 + timedelta(seconds=DEFAULT_REGISTRATION_BOUND_S + 1),
    )
    _, events = mgr.get(steward_bridge_id).events_after(-1)
    _check(
        len(events) == 1
        and events[0].event_type == EVENT_SESSION_REGISTRATION_OVERDUE_NOTICE
        and "agi-deaf-child" in events[0].content,
        f"the steward gets exactly one registration-overdue notice, under an "
        f"event type distinct from the other three spawn notices (got {events!r})",
    )


def test_registration_marks_without_notify_when_registry_absent() -> None:
    state = _state()
    _spawn_unregistered(state, agent_instance_id="agi-noreg", spawned_at=T0)
    marked = sweep_unregistered_spawning_sessions(
        state, now=T0 + timedelta(seconds=DEFAULT_REGISTRATION_BOUND_S + 1),
    )
    _check(marked == 1, "an early-boot tick with no bridge still MARKS the row")


# ---------------------------------------------------------------------------
# L4a: sweep_rotation_due_sessions / sweep_gauge_coverage
# ---------------------------------------------------------------------------


def _gauge(state: StateManagementInterface, agent_instance_id: str, **over: object) -> None:
    """Write a gauge row the way report_context_status would."""
    kwargs: dict[str, object] = {
        "agent_instance_id": agent_instance_id, "claude_session_id": "s1",
        "model": "claude-sonnet-5", "current_tokens": 900_000, "ceiling": 1_000_000,
        "measured_at": T0.isoformat(), "cache_cold": False,
        "reporter_surface": "checkout", "reporter_generation": 2,
    }
    kwargs.update(over)
    upsert_session_context_status(state, **kwargs)  # type: ignore[arg-type]


def _wired() -> tuple[StateManagementInterface, PeerRegistry, BridgeSessionManager, str]:
    state, reg, mgr = _state(), _peer_registry(), _bridge_manager()
    _spawn_live(state, agent_instance_id="agi-steward")
    state.update_state(
        AGENT_ROLE_BINDING_NAMESPACE,
        {"table": "managed_session", "filters": {"agent_instance_id": "agi-steward"}},
        {"agent_id": "claude_code"},
    )
    bridge_id = _register_live_binding(reg, mgr, agent_instance_id="agi-steward")
    _spawn_live(state, agent_instance_id="agi-worker", spawned_by_instance_id="agi-steward")
    return state, reg, mgr, bridge_id


def test_rotation_due_notice_carries_the_measured_number() -> None:
    """The steward path shares the same one-line durability notice."""
    state, reg, mgr, bridge_id = _wired()
    _gauge(state, "agi-worker")
    n = sweep_rotation_due_sessions(state, peer_registry=reg, bridge_manager=mgr)
    _check(n == 1, "a session past the rotation threshold produces one notice")
    _, events = mgr.get(bridge_id).events_after(-1)
    body = events[0].content if events else ""
    _check(events and events[0].event_type == "rotation_due_notice",
           "the event is typed rotation_due_notice, distinct from the overdue notice")
    _check(body == "context is 900,000 — make sure everything is durable.",
           "the notice contains only current context and the durability instruction")
    _check(not any(term in body.lower() for term in ("warm_", "rotate at", "pays for itself", "break-even")),
           "the steward path contains none of the retired economics vocabulary")


def test_rotation_due_is_silent_below_the_threshold() -> None:
    state, reg, mgr, bridge_id = _wired()
    _gauge(state, "agi-worker", current_tokens=319_999)
    n = sweep_rotation_due_sessions(state, peer_registry=reg, bridge_manager=mgr)
    _check(n == 0, "319,999 is below the derived 320,000 notice point")
    _, events = mgr.get(bridge_id).events_after(-1)
    _check(not events, "and nothing is delivered -- a notice that always fires is ignored")


def test_the_saturated_band_below_the_fraction_now_reaches_the_steward() -> None:
    """GAU-08 at the L4a leg: 300,000 on a 1M ceiling was SKIPPED.

    This leg used to gate on `fraction < ROTATION_THRESHOLD_FRACTION`, so it
    said nothing to a steward about a session sitting in `warm_immediate` --
    the most urgent band the policy has -- for the entire 200,000 tokens
    between where that band saturates and where 0.5 of a 1M ceiling arrives.
    300,000 is the first token of that range, chosen over a midpoint because
    an off-by-one at the band edge is the mutation a midpoint cannot catch.
    """
    state, reg, mgr, bridge_id = _wired()
    _gauge(state, "agi-worker", current_tokens=300_000, model="claude-opus-5")
    n = sweep_rotation_due_sessions(state, peer_registry=reg, bridge_manager=mgr)
    _check(n == 1, "300,000 on a 1M ceiling now produces a notice (it produced none "
                   "while this leg decided on the fraction alone)")
    _, events = mgr.get(bridge_id).events_after(-1)
    body = events[0].content if events else ""
    _check("band=warm_immediate" in body,
           "and the notice names warm_immediate -- the band that fired it")
    _check("0.300" in body,
           "...beside the fraction 0.300, which is BELOW the 0.5 hint: the two "
           "numbers now appear together without contradicting the decision")


def test_rotation_due_uses_the_runtime_window_minimum() -> None:
    state, reg, mgr, bridge_id = _wired()
    _gauge(state, "agi-worker", current_tokens=144_000, ceiling=200_000,
           model="claude-haiku-4-5")
    n = sweep_rotation_due_sessions(state, peer_registry=reg, bridge_manager=mgr)
    _, events = mgr.get(bridge_id).events_after(-1)
    _check(n == 1 and events and events[0].content == (
        "context is 144,000 — make sure everything is durable."
    ), "the steward uses the 200K runtime window and notices at 144K")


def test_consumer_4_prose_names_an_axis_the_decision_actually_used() -> None:
    """FIXED (GAU-12, 2026-08-18). This test used to PIN the residual left by
    GAU-08: `_rotation_prose` always printed the BAND while `_rotation_due_row`
    decided on the union, so a fraction-only firing on a small ceiling sent a
    notice typed `rotation_due_notice` whose body read "keep working".

    That pin is flipped here to assert the corrected prose, using the same
    remedy GAU-08 already applied to the hook's notice
    (`rotation_due_watch.build_notification_content`): a "DUE BECAUSE" clause
    naming the axis that fired, sourced from `RotationDueVerdict`'s own
    decomposition (`band_actionable` / `fraction_crossed`) rather than a
    second copy of the predicate at the prose site.

    Both non-contradiction cases are asserted, each checked for what it must
    NOT say as well as what it must -- a notice that merely mentions the right
    axis while still implying the other one fired would pass a contains-only
    test and still be misleading:

    * The BAND-FIRED case (below) is UNCHANGED behaviour, not a new
      assertion -- proving this fix did not destroy the discriminator that
      already told band-fired and fraction-fired apart. 300,000 on a 1M
      ceiling still fires BECAUSE the band is actionable, still names that
      band, and must NOT claim the fraction hint was crossed (it is not, at
      0.300 of the ceiling).
    * The FRACTION-FIRED case is what GAU-12 fixes: a small ceiling at the
      model's own halfway point fires because the fraction crossed, while the
      model-blind band is still `warm_keep`. The prose must say THAT is why it
      fired, and must NOT claim the band asked for a rotation.
    """
    state, reg, mgr, bridge_id = _wired()

    _gauge(state, "agi-worker", current_tokens=300_000, ceiling=1_000_000,
           model="claude-opus-5")
    n = sweep_rotation_due_sessions(state, peer_registry=reg, bridge_manager=mgr)
    _check(n == 1, "the band-fired case still notifies")
    _, events = mgr.get(bridge_id).events_after(-1)
    band_fired_body = events[0].content if events else ""
    _check("band=warm_immediate" in band_fired_body,
           "band-fired case: the discriminator this fix must not destroy -- "
           "still names the band that fired")
    _check("DUE BECAUSE the ECONOMICS BAND is 'warm_immediate'" in band_fired_body,
           "...and says IN WORDS that the band is why, not the fraction")
    _check("NOT crossed" in band_fired_body,
           "...and explicitly disclaims the fraction hint, which is NOT "
           "crossed at 0.300 of the ceiling")

    state2, reg2, mgr2, bridge_id2 = _wired()
    _gauge(state2, "agi-worker", current_tokens=100_000, ceiling=200_000,
           model="claude-haiku-4-5")
    n2 = sweep_rotation_due_sessions(state2, peer_registry=reg2, bridge_manager=mgr2)
    _check(n2 == 1, "a small-ceiling session at its own halfway point is notified -- "
                    "the fraction term keeps this reachable where the bands cannot")
    _, events2 = mgr2.get(bridge_id2).events_after(-1)
    fraction_fired_body = events2[0].content if events2 else ""
    _check("band=warm_keep" in fraction_fired_body,
           "fraction-fired case: the band is still shown -- informational, not "
           "hidden -- but no longer the unqualified verdict")
    _check("DUE BECAUSE" in fraction_fired_body and "fires first" in fraction_fired_body,
           "FIXED: the notice now says the FRACTION is why it fired, so an "
           "event typed rotation_due_notice no longer contradicts its own body "
           "by reading a bare 'keep working'")
    _check("DUE BECAUSE the ECONOMICS BAND" not in fraction_fired_body,
           "...and does not claim the band-only branch's reason, which would "
           "be a lie for this row")


def test_rotation_due_flags_an_unattributable_reporter() -> None:
    """A stale-copy row sends no cache state, so its band is the WARM DEFAULT
    rather than a measurement. Presenting that as an urgent verdict is false
    precision, so the notice says the reporter cannot be attributed."""
    state, reg, mgr, bridge_id = _wired()
    _gauge(state, "agi-worker", reporter_surface=None, reporter_generation=None)
    sweep_rotation_due_sessions(state, peer_registry=reg, bridge_manager=mgr)
    _, events = mgr.get(bridge_id).events_after(-1)
    body = events[0].content if events else ""
    _check("UNATTRIBUTABLE" in body,
           "a row from a pre-attribution reporter is flagged, not silently trusted")
    _check("provisional" in body,
           "and the band is marked provisional rather than presented as measured")


def test_gauge_coverage_catches_a_live_session_with_no_row() -> None:
    """The signature measured 2026-08-16: hooks running, gauge write silently
    failing. Neither the hook (it must swallow its own faults) nor the session
    (it does not know) can report this; the sweep sees both facts."""
    state, reg, mgr, bridge_id = _wired()  # agi-worker is LIVE with NO gauge row
    n = sweep_gauge_coverage(
        state, now=_past_grace(), peer_registry=reg, bridge_manager=mgr,
    )
    _check(n == 1, "a live session with no gauge row is detected")
    _, events = mgr.get(bridge_id).events_after(-1)
    _check(events and events[0].event_type == "gauge_coverage_notice"
           and "agi-worker" in events[0].content,
           "the steward is told which session is dark")


def test_gauge_coverage_is_silent_when_the_row_exists() -> None:
    state, reg, mgr, bridge_id = _wired()
    _gauge(state, "agi-worker")
    n = sweep_gauge_coverage(
        state, now=_past_grace(), peer_registry=reg, bridge_manager=mgr,
    )
    _check(n == 0, "a session that IS reporting produces no coverage notice")
    _, events = mgr.get(bridge_id).events_after(-1)
    _check(not events, "and nothing is delivered")


# ---------------------------------------------------------------------------
# R4 change 1: the gauge leg's STARTUP GRACE
# ---------------------------------------------------------------------------


def test_gauge_coverage_grants_a_newly_live_session_its_startup_grace() -> None:
    """The false alarm this fixes, measured live 2026-08-17T16:33:11Z.

    Four lanes ~2 minutes old were reported as "the reporting path is failing
    SILENTLY". All four were merely NEW — they had not completed a first
    reporting tick — and every one reported normally minutes later. A newly LIVE
    session is dark by construction until its first tick, so without this
    predicate every spawn wave manufactures one false alarm per lane.

    The latch cannot substitute for it: each wave is a fresh episode with fresh
    keys, so suppression of a REPEAT does nothing about a fresh false POSITIVE.
    """
    state, reg, mgr, bridge_id = _wired()  # agi-worker LIVE, no gauge row, born now
    n = sweep_gauge_coverage(
        state, now=datetime.now(UTC), peer_registry=reg, bridge_manager=mgr,
    )
    _check(n == 0, "a just-born live session is NOT called dark")
    _, events = mgr.get(bridge_id).events_after(-1)
    _check(not events, "and its steward is not woken about it")


def test_gauge_coverage_still_fires_once_the_grace_expires() -> None:
    """The other half, and the one that keeps the grace from being a mute
    button: the SAME row, still dark, is reported once it has had time."""
    state, reg, mgr, bridge_id = _wired()
    early = sweep_gauge_coverage(
        state, now=datetime.now(UTC), peer_registry=reg, bridge_manager=mgr,
    )
    late = sweep_gauge_coverage(
        state, now=_past_grace(), peer_registry=reg, bridge_manager=mgr,
    )
    _check((early, late) == (0, 1), "silent while young, reported once aged")
    _, events = mgr.get(bridge_id).events_after(-1)
    _check(len(events) == 1, "exactly one notice, delivered on the later tick")
    _check(
        "startup grace" in events[0].content,
        "and the prose names the grace it passed, so the reader can see the "
        "measurement the finding rests on",
    )


def test_gauge_coverage_does_not_grant_grace_on_an_unreadable_timestamp() -> None:
    """The fail-toward direction, stated because it is the opposite of
    the former spawning lifetime policy, which no longer exists.

    The grace is an EXCEPTION to an alarm, so it may only apply on positive
    evidence that the row is young. A row whose transition timestamp cannot be
    read is still reported — suppressing an alarm on a timestamp nobody could
    parse is how a detector goes quiet for a reason nobody chose.
    """
    state, reg, mgr, _bridge_id = _wired()
    state.update_state(
        AGENT_ROLE_BINDING_NAMESPACE,
        {"table": "managed_session", "filters": {"agent_instance_id": "agi-worker"}},
        {"last_transition_at": ""},
    )
    n = sweep_gauge_coverage(
        state, now=datetime.now(UTC), peer_registry=reg, bridge_manager=mgr,
    )
    _check(n == 1, "an unreadable age does NOT buy silence")


# ---------------------------------------------------------------------------
# GAU-13: the grace is shorter than a spawned worker's real boot-to-first-tick,
# and the notice asserts a negative the next tick can falsify
# ---------------------------------------------------------------------------

# The worst boot-to-first-tick MEASURED for a spawned tmux worker, across the
# three data points in the GAU-13 backlog entry (lane-gau10-stall-boolean
# 2026-08-18T15:35:12Z spawn -> first tick ~15:43Z; lane-r2-holds-false
# 16:30:31Z spawn -> first WORK turn >=7.5 min later; and that lane's own row
# confirmed present once work turns ticked). A spawned worker's clock to its
# first tick is spawn -> charter dispatch -> first WORK turn, and the
# bootstrap-ack turn lands no gauge tick, so the gap is structural rather than
# incidental. Named here, in the test, so the grace constant can never be
# lowered back under the measurement without this failing and saying why.
MEASURED_WORST_BOOT_TO_FIRST_TICK_S = 480.0


def test_gauge_coverage_grace_covers_the_measured_boot_to_first_tick() -> None:
    """★ CATCHES: GAU-13(a) -- a grace shorter than the boot it exists to cover.

    Asserted BEHAVIOURALLY (a session that old is not called dark) rather than
    only on the constant, because the constant is the current implementation of
    the property and not the property itself.
    """
    _check(
        GAUGE_COVERAGE_GRACE_S >= MEASURED_WORST_BOOT_TO_FIRST_TICK_S,
        f"the startup grace ({GAUGE_COVERAGE_GRACE_S}s) covers the WORST "
        f"MEASURED boot-to-first-tick ({MEASURED_WORST_BOOT_TO_FIRST_TICK_S}s) "
        "for a spawned worker -- a grace under the measurement manufactures one "
        "false alarm per lane per spawn wave",
    )
    state, reg, mgr, bridge_id = _wired()
    at_worst_boot = datetime.now(UTC) + timedelta(
        seconds=MEASURED_WORST_BOOT_TO_FIRST_TICK_S - 30,
    )
    n = sweep_gauge_coverage(
        state, now=at_worst_boot, peer_registry=reg, bridge_manager=mgr,
    )
    _check(
        n == 0,
        "a live session still inside the measured boot-to-first-tick window is "
        "NOT reported dark -- this is the exact false alarm measured against "
        "lane-gau10-stall-boolean and lane-r2-holds-false on 2026-08-18",
    )
    _, events = mgr.get(bridge_id).events_after(-1)
    _check(not events, "and its steward is not woken about it")


def test_gauge_coverage_notice_states_the_measurement_not_the_inference() -> None:
    """★ CATCHES: GAU-13(b) -- the notice asserts a negative the NEXT TICK can
    falsify.

    Measured 2026-08-18: the notice told the reader the dark session was "past
    the startup grace, so this is not a session that simply has not reported
    yet" -- and the row landed healthy five minutes later. It WAS a session that
    simply had not reported yet. The notice is entitled to report what it
    measured (no row after N seconds live); it is not entitled to rule out the
    explanation that turned out to be the right one.

    False alarms here train the reader to skim L4b, which is the leg that
    catches the REAL GAU-01 family -- so the cost of the overclaim is paid by a
    different defect's detection.
    """
    state, reg, mgr, bridge_id = _wired()
    sweep_gauge_coverage(state, now=_past_grace(), peer_registry=reg, bridge_manager=mgr)
    _, events = mgr.get(bridge_id).events_after(-1)
    body = events[0].content if events else ""
    _check(bool(body), "the dark session still produces a notice")
    _check(
        "not a session that simply has not reported yet" not in body,
        "the notice does NOT rule out 'it just has not reported yet' -- that is "
        f"the inference the next tick falsified. Got: {body!r}",
    )
    _check(
        "likeliest cause" not in body,
        "...nor does it present a CAUSE it did not measure as the likeliest "
        f"one. Got: {body!r}",
    )
    _check(
        str(int(GAUGE_COVERAGE_GRACE_S)) in body,
        "...and it DOES state the measurable fact instead: how long the session "
        f"has been live with no row. Got: {body!r}",
    )


def test_gauge_coverage_notice_says_when_no_reporter_has_run_at_all() -> None:
    """★ CATCHES: attributing a dark row to a broken WRITE when the session has
    produced no reporter output at all.

    The two hooks that write these rows are BOTH PostToolUse hooks on the same
    tool call: the heartbeat writes the lifecycle row and rotation_due_watch
    writes the gauge row. So "report_alive has landed since this row went live"
    is positive evidence that the session completes tool calls and that its
    solet path resolves -- and its absence is positive evidence of the opposite.
    The notice must not claim the first when it measured the second.

    This row has NEVER reported alive -- its report_by is still the deadline
    armed at spawn, so the derived last-report_alive lands ON the transition
    rather than after it -- and the notice must say the session has produced NO
    reporter output rather than blaming the gauge write path specifically.

    The window is set EXPLICITLY rather than left at the fixture default: with
    no report_by at all the evidence is UNKNOWN, which is a third case and not
    this one. A test that leaves its own precondition to a default is not
    stating which branch it pins.
    """
    state, reg, mgr, bridge_id = _wired()
    row = read_managed_session(state, "agi-worker")
    became_live = datetime.fromisoformat(str(row["last_transition_at"]))
    if became_live.tzinfo is None:
        became_live = became_live.replace(tzinfo=UTC)
    state.update_state(
        AGENT_ROLE_BINDING_NAMESPACE,
        {"table": "managed_session", "filters": {"agent_instance_id": "agi-worker"}},
        {
            "report_by_seconds": 300,
            "report_by": (became_live + timedelta(seconds=300)).isoformat(),
            "report_by_source": "explicit_self_report",
        },
    )
    sweep_gauge_coverage(state, now=_past_grace(), peer_registry=reg, bridge_manager=mgr)
    _, events = mgr.get(bridge_id).events_after(-1)
    body = events[0].content if events else ""
    _check(
        "report_alive is landing" not in body,
        "a row with no report_alive since it went live is NOT described as one "
        f"whose report_alive is landing. Got: {body!r}",
    )
    _check(
        "no report_alive" in body.lower() or "never reported alive" in body.lower(),
        "...the notice names the second measurement (no lifecycle report either) "
        f"so the reader can tell the two failures apart. Got: {body!r}",
    )


def test_gauge_coverage_notice_names_the_evidence_when_the_session_has_ticked()\
        -> None:
    """★ CATCHES: the other half -- throwing away the STRONG signal.

    When report_alive HAS landed since the row went live, PostToolUse
    demonstrably fires for this session and its solet path demonstrably
    resolves, and there is still no gauge row. THAT is the 2026-08-16 signature
    the leg was built for, and it is now evidenced rather than assumed. The
    notice must say so, because it is a materially different finding from a
    session that has produced nothing at all.
    """
    state, reg, mgr, bridge_id = _wired()
    row = read_managed_session(state, "agi-worker")
    became_live = datetime.fromisoformat(str(row["last_transition_at"]))
    if became_live.tzinfo is None:
        became_live = became_live.replace(tzinfo=UTC)
    ticked_at = became_live + timedelta(seconds=600)
    state.update_state(
        AGENT_ROLE_BINDING_NAMESPACE,
        {"table": "managed_session", "filters": {"agent_instance_id": "agi-worker"}},
        {
            "report_by_seconds": 300,
            "report_by": (ticked_at + timedelta(seconds=300)).isoformat(),
            "report_by_source": "explicit_self_report",
        },
    )
    sweep_gauge_coverage(state, now=_past_grace(), peer_registry=reg, bridge_manager=mgr)
    _, events = mgr.get(bridge_id).events_after(-1)
    body = events[0].content if events else ""
    _check(
        "report_alive" in body and "no report_alive" not in body.lower(),
        "a session whose report_alive IS landing is described that way -- the "
        f"evidenced form of the finding. Got: {body!r}",
    )
    _check(
        "not a session that simply has not reported yet" not in body,
        "...and even the strong branch does not assert the unfalsifiable "
        f"negative. Got: {body!r}",
    )


# ---------------------------------------------------------------------------
# L4b composition: NoticeLatch — what makes the two legs SAFE to put on a tick
# ---------------------------------------------------------------------------


def test_rotation_due_notifies_once_per_episode() -> None:
    """The composition guard. Unlike the overdue notice, rotation-due rides no
    state edge: the gauge stays over the threshold until the session rotates,
    so on a 300s tick an unlatched leg delivers the same notice every 5 minutes
    forever. Repetition is not a smaller version of the warning -- it destroys
    the channel the warning arrives on."""
    state, reg, mgr, bridge_id = _wired()
    _gauge(state, "agi-worker")
    latch = NoticeLatch()
    first = sweep_rotation_due_sessions(state, peer_registry=reg, bridge_manager=mgr, latch=latch)
    second = sweep_rotation_due_sessions(state, peer_registry=reg, bridge_manager=mgr, latch=latch)
    third = sweep_rotation_due_sessions(state, peer_registry=reg, bridge_manager=mgr, latch=latch)
    _check((first, second, third) == (1, 0, 0), "the condition persists; the notice does not")
    _, events = mgr.get(bridge_id).events_after(-1)
    _check(len(events) == 1, "exactly ONE event reached the steward across three ticks")


def test_rotation_due_latch_rearms_when_the_session_rotates() -> None:
    """One notice per EPISODE, not one per lifetime. A session that rotates and
    later climbs back over the threshold is a NEW fact about the world, and
    suppressing it would make the latch a mute button."""
    state, reg, mgr, bridge_id = _wired()
    _gauge(state, "agi-worker")
    latch = NoticeLatch()
    sweep_rotation_due_sessions(state, peer_registry=reg, bridge_manager=mgr, latch=latch)
    _gauge(state, "agi-worker", current_tokens=1_000)  # rotated: back under the threshold
    cleared = sweep_rotation_due_sessions(state, peer_registry=reg, bridge_manager=mgr, latch=latch)
    _gauge(state, "agi-worker", current_tokens=950_000)  # climbed again: a second episode
    again = sweep_rotation_due_sessions(state, peer_registry=reg, bridge_manager=mgr, latch=latch)
    _check(cleared == 0, "no notice while the condition is clear")
    _check(again == 1, "a SECOND episode notifies again -- the latch released on the clear")
    _, events = mgr.get(bridge_id).events_after(-1)
    _check(len(events) == 2, "two episodes, two events")


def test_rotation_due_latch_does_not_swallow_an_undelivered_notice() -> None:
    """Latch on DELIVERY, never on detection. If the notice could not be
    delivered (no live steward binding this tick), latching it would let the
    delivery failure silence the whole episode -- the failure mode where the
    louder the outage, the quieter the alarm."""
    state, reg, mgr = _state(), _peer_registry(), _bridge_manager()
    _spawn_live(state, agent_instance_id="agi-steward")
    _spawn_live(state, agent_instance_id="agi-worker", spawned_by_instance_id="agi-steward")
    _gauge(state, "agi-worker")
    latch = NoticeLatch()
    undelivered = sweep_rotation_due_sessions(
        state, peer_registry=reg, bridge_manager=mgr, latch=latch,
    )
    _check(undelivered == 0, "no live steward binding -- nothing delivered")
    bridge_id = _register_live_binding(reg, mgr, agent_instance_id="agi-steward")
    state.update_state(
        AGENT_ROLE_BINDING_NAMESPACE,
        {"table": "managed_session", "filters": {"agent_instance_id": "agi-steward"}},
        {"agent_id": "claude_code"},
    )
    retried = sweep_rotation_due_sessions(state, peer_registry=reg, bridge_manager=mgr, latch=latch)
    _check(retried == 1, "the next tick RETRIES -- an undelivered notice was never latched")
    _, events = mgr.get(bridge_id).events_after(-1)
    _check(len(events) == 1, "and the steward gets it once, not never and not twice")


def test_gauge_coverage_notifies_once_and_releases_on_recovery() -> None:
    """Same discipline on the darkness notice. A dark session stays dark until
    a person fixes it, so unlatched this repeats for the whole outage.

    The re-arm is asserted through the latch's own state rather than by
    staging a second outage: the ONLY way a gauge row goes missing again once
    it exists is a deletion, and manufacturing one here would be testing a
    fixture rather than the leg. What is genuinely reachable -- and what this
    asserts -- is that recovery RELEASES the key, so a later outage is a fresh
    notice instead of a permanent silence."""
    state, reg, mgr, bridge_id = _wired()  # agi-worker LIVE, no gauge row
    latch = NoticeLatch()
    aged = _past_grace()
    first = sweep_gauge_coverage(
        state, now=aged, peer_registry=reg, bridge_manager=mgr, latch=latch,
    )
    second = sweep_gauge_coverage(
        state, now=aged, peer_registry=reg, bridge_manager=mgr, latch=latch,
    )
    _check((first, second) == (1, 0), "one notice for one outage, not one per tick")
    _, events = mgr.get(bridge_id).events_after(-1)
    _check(len(events) == 1, "exactly ONE event across the outage's ticks")
    _check(latch.suppressed("agi-worker"), "the key is latched while the outage holds")
    _gauge(state, "agi-worker")  # reporting recovered
    recovered = sweep_gauge_coverage(
        state, now=aged, peer_registry=reg, bridge_manager=mgr, latch=latch,
    )
    _check(recovered == 0, "nothing to say while it reports")
    _check(
        not latch.suppressed("agi-worker"),
        "and recovery RELEASED the key -- a later outage notifies rather than being "
        "suppressed by the first one",
    )


# ---------------------------------------------------------------------------
# R4 change 3: a notice must not be able to swallow its own message bug
# ---------------------------------------------------------------------------


def test_a_broken_notice_message_surfaces_instead_of_being_swallowed() -> None:
    """Found by M5's blast radius, not by a separate investigation.

    Both notice legs composed their prose as an ARGUMENT INSIDE the try that
    guards ``append_event``. That guard exists for DELIVERY faults, but a broad
    ``except Exception`` around the prose too means a bug in the message itself
    is caught, logged as "append failed", and the notice silently vanishes while
    the log names the wrong cause. In a notice family whose entire purpose is to
    be the thing that speaks up, that is the fail-open shape these legs exist to
    catch, living inside the alarm.

    ``_rotation_prose`` formats ``fraction`` with ``:.3f``, so an enriched row
    without it raises. With the prose composed outside the try, that surfaces.
    Swallowing it would return False and report zero — indistinguishable from an
    unreachable steward.
    """
    state, reg, mgr, _bridge_id = _wired()
    raised = False
    try:
        _notify_rotation_due(
            state=state, peer_registry=reg, bridge_manager=mgr,
            row={},  # no 'fraction' -> _rotation_prose raises
            agent_instance_id="agi-worker", spawner_instance_id="agi-steward",
        )
    except (TypeError, ValueError):
        raised = True
    _check(raised, "a broken notice MESSAGE surfaces rather than being reported "
                   "as a delivery failure")


# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------


def test_latches_are_independent_per_notice_kind() -> None:
    """Why the rider holds TWO latches rather than one shared set: the same
    agent_instance_id can be both rotation-due and dark, and a shared latch
    would let whichever notice fired first suppress the other kind entirely."""
    latch = NoticeLatch()
    _check(not latch.suppressed("agi-x"), "an unseen key is not suppressed")
    latch.record_sent("agi-x")
    _check(latch.suppressed("agi-x"), "a recorded key suppresses its repeat")
    latch.retain_active({"agi-x"})
    _check(latch.suppressed("agi-x"), "a still-active key stays latched")
    latch.retain_active(set())
    _check(not latch.suppressed("agi-x"), "a cleared condition releases the key")


def test_l4a_legs_no_op_without_a_bridge() -> None:
    """Same posture as sweep_overdue_sessions: an early-boot tick with no
    bridge must not raise. Unlike the overdue sweep there is no state
    transition to preserve here, so both legs simply return 0."""
    state = _state()
    _spawn_live(state, agent_instance_id="agi-worker", spawned_by_instance_id="agi-steward")
    _check(sweep_rotation_due_sessions(state) == 0, "rotation-due leg no-ops with no bridge")
    _check(sweep_gauge_coverage(state) == 0, "gauge-coverage leg no-ops with no bridge")


# ---------------------------------------------------------------------------
# GAU-01(b): the gauge row that STOPPED, as distinct from the one never written
#
# The defect these pin, measured 2026-08-18: a lane sat with a FROZEN gauge row
# for 85 minutes while alive and completing tool calls, and nothing surfaced it.
# L4b asks whether a row EXISTS; this one existed and simply never changed
# again, so a frozen row read as coverage.
#
# Every test below sets report_by_seconds EXPLICITLY. `_spawn_live` defaults it
# to 0, which derives to NO EVIDENCE rather than "never ticked" -- a test that
# leaves its own precondition to that default is not stating which branch it
# pins.
# ---------------------------------------------------------------------------


def _ticking(
    state: StateManagementInterface, agent_instance_id: str, *, last_alive: datetime,
) -> None:
    """Arm the lifecycle row so the §3.3 identity derives to ``last_alive``.

    Writes the PAIR, never `report_by` alone: the derivation is
    ``report_by - report_by_seconds``, so a test that set only one of them
    would be pinning a value it did not choose.

    ALSO backdates ``last_transition_at`` to a day before ``last_alive``
    (GAU-22(c)): ``_spawn_live`` stamps its OWN transition at real wall-clock
    "now", which every gauge-stale fixture below implicitly relied on being
    OLDER than its (backdated) gauge ``measured_at`` -- true for a genuinely
    long-lived ticking session, false by fixture accident otherwise. A
    session that is TICKING has, by construction, been alive for a while;
    modelling that here is what keeps GAU-22(c)'s rotation-window grace from
    misreading every "long-lived, reporter died" fixture as "just rotated".
    """
    window_s = 300
    state.update_state(
        AGENT_ROLE_BINDING_NAMESPACE,
        {"table": "managed_session", "filters": {"agent_instance_id": agent_instance_id}},
        {
            "report_by_seconds": window_s,
            "report_by": (last_alive + timedelta(seconds=window_s)).isoformat(),
            "report_by_source": "explicit_self_report",
            "last_transition_at": (last_alive - timedelta(days=1)).isoformat(),
        },
    )


def test_last_report_alive_derives_the_tick_moment() -> None:
    """The identity the whole leg rests on, pinned on its own before anything
    composes it: report_by minus report_by_seconds IS the last report_alive."""
    moment = datetime(2026, 8, 18, 12, 0, 0, tzinfo=UTC)
    row = {
        "report_by": (moment + timedelta(seconds=300)).isoformat(),
        "report_by_seconds": 300,
        "report_by_source": "explicit_self_report",
    }
    _check(last_report_alive(row) == moment, "the derived tick moment is exact")
    for source in ("confirmed_drive", "observed_spawning", ""):
        _check(
            last_report_alive({**row, "report_by_source": source}) is None,
            f"report_by provenance {source!r} is not misidentified as report_alive",
        )
    _check(
        last_report_alive({"report_by": moment.isoformat(), "report_by_seconds": 0}) is None,
        "a zero window is NO EVIDENCE (None), never a datetime — absence of the "
        "WINDOW is not evidence of absence of a TICK",
    )


def test_gauge_stale_fires_when_alive_and_the_gauge_arrested() -> None:
    """★ THE GAU-01 SIGNATURE. Lifecycle advancing, gauge frozen — the one row
    of the discriminator table that is a finding."""
    state, reg, mgr, bridge_id = _wired()
    now = datetime.now(UTC)
    _gauge(state, "agi-worker", measured_at=(now - timedelta(seconds=5400)).isoformat())
    _ticking(state, "agi-worker", last_alive=now - timedelta(seconds=30))
    n = sweep_gauge_staleness(state, now=now, peer_registry=reg, bridge_manager=mgr)
    _check(n == 1, "a live, reporting session with a frozen gauge row is detected")
    _, events = mgr.get(bridge_id).events_after(-1)
    _check(
        events and events[0].event_type == "gauge_stale_notice",
        "and it arrives as its OWN event type, not the missing-row one",
    )


def test_gauge_stale_names_carried_forward_heartbeat_failures() -> None:
    """D-5.3: a passive heartbeat that transports failures is evidence of a
    failing heartbeat path, not an explicit report_alive identity and not a
    confident claim that the gauge alone froze."""
    state, reg, mgr, bridge_id = _wired()
    now = datetime.now(UTC)
    _gauge(state, "agi-worker", measured_at=(now - timedelta(seconds=5400)).isoformat())
    state.update_state(
        AGENT_ROLE_BINDING_NAMESPACE,
        {"table": "managed_session", "filters": {"agent_instance_id": "agi-worker"}},
        {
            "last_heartbeat_at": (now - timedelta(seconds=30)).isoformat(),
            "heartbeat_failures_since_last": 1,
            "heartbeat_failure_first_at": (now - timedelta(seconds=60)).isoformat(),
            "last_transition_at": (now - timedelta(days=1)).isoformat(),
        },
    )
    n = sweep_gauge_staleness(state, now=now, peer_registry=reg, bridge_manager=mgr)
    _check(n == 1, "a carried-forward heartbeat failure is detected against the frozen gauge")
    _, events = mgr.get(bridge_id).events_after(-1)
    body = events[0].content if events else ""
    _check(
        "heartbeat-FAILING" in body and "gauge reporter alone froze" in body,
        "the notice diagnoses the recorded heartbeat failure rather than asserting gauge-only failure",
    )


def test_gauge_stale_is_silent_when_both_clocks_stopped() -> None:
    """Row 3 of the table: not advancing + stale = quiet or dead. That is the
    D1 sweep's `overdue` job. Saying "your gauge reporter is broken" about a
    session that stopped calling tools would be a confident wrong diagnosis.

    ★ THE LAG IS DELIBERATELY LARGE AND NEGATIVE (-1800s), not merely negative.
    An earlier draft used -100s and a mutation SURVIVED it: replacing the signed
    comparison with `abs(lag)` still passed, because 100 is inside the tolerance
    either way. A test that pins DIRECTION has to put the magnitude past the
    threshold, or it is only re-testing the threshold. Found by the mutation
    battery, not by reading.
    """
    state, reg, mgr, _ = _wired()
    now = datetime.now(UTC)
    _gauge(state, "agi-worker", measured_at=(now - timedelta(seconds=5400)).isoformat())
    _ticking(state, "agi-worker", last_alive=now - timedelta(seconds=7200))
    n = sweep_gauge_staleness(state, now=now, peer_registry=reg, bridge_manager=mgr)
    _check(
        n == 0,
        "a session whose BOTH clocks stopped is not a gauge finding — and the "
        "gauge being the FRESHER of the two does not invert into one",
    )


def test_gauge_stale_is_silent_on_a_healthy_throttle_skew() -> None:
    """The false-alarm guard, pinned at the MEASURED healthy maximum. Three live
    lanes over ~45 minutes ran +109.8s at the widest; the gauge throttles at
    120s and the heartbeat at 180s. A threshold that fired here would alarm on
    the normal fleet and train the reader to skim the channel."""
    state, reg, mgr, _ = _wired()
    now = datetime.now(UTC)
    _gauge(state, "agi-worker", measured_at=(now - timedelta(seconds=140)).isoformat())
    _ticking(state, "agi-worker", last_alive=now - timedelta(seconds=30))
    n = sweep_gauge_staleness(state, now=now, peer_registry=reg, bridge_manager=mgr)
    _check(n == 0, "a 110s skew — inside the measured healthy band — is not a finding")


def test_gauge_stale_threshold_is_a_boundary_not_a_vibe() -> None:
    """Both sides of GAUGE_STALE_LAG_S, so the constant is pinned rather than
    merely referenced. A test that only ever exercises one side cannot tell a
    threshold from a hardcoded True."""
    now = datetime.now(UTC)
    for lag, expected, label in (
        (GAUGE_STALE_LAG_S - 60, 0, "just inside the tolerance stays silent"),
        (GAUGE_STALE_LAG_S + 60, 1, "just past the tolerance fires"),
    ):
        state, reg, mgr, _ = _wired()
        _gauge(state, "agi-worker", measured_at=(now - timedelta(seconds=lag + 30)).isoformat())
        _ticking(state, "agi-worker", last_alive=now - timedelta(seconds=30))
        n = sweep_gauge_staleness(state, now=now, peer_registry=reg, bridge_manager=mgr)
        _check(n == expected, label)


def test_gauge_stale_leaves_the_missing_row_to_the_coverage_leg() -> None:
    """Row 1 of the table's complement: NO row at all is L4b's finding. Two legs
    must never both fire on one condition, or the steward gets two notices
    naming different causes for one session."""
    state, reg, mgr, _ = _wired()  # agi-worker LIVE with NO gauge row
    now = datetime.now(UTC)
    _ticking(state, "agi-worker", last_alive=now - timedelta(seconds=30))
    n = sweep_gauge_staleness(state, now=now, peer_registry=reg, bridge_manager=mgr)
    _check(n == 0, "a session with no gauge row is NOT claimed by the staleness leg")


def test_gauge_stale_treats_a_missing_window_as_no_evidence() -> None:
    """The tri-state, defended at the leg. report_by_seconds of 0 carries no
    window, so arrest is not establishable — and inferring it from a missing
    column is the exact move the identity's None exists to block."""
    state, reg, mgr, _ = _wired()
    now = datetime.now(UTC)
    _gauge(state, "agi-worker", measured_at=(now - timedelta(seconds=5400)).isoformat())
    # report_by_seconds left at the _spawn_live default of 0 — stated, not inherited.
    n = sweep_gauge_staleness(state, now=now, peer_registry=reg, bridge_manager=mgr)
    _check(n == 0, "no report_by window means NO EVIDENCE, never a reported arrest")


def test_gauge_stale_notice_states_both_clocks_and_names_no_cause() -> None:
    """The GAU-13 prose rule, one leg over: state the measurement, diagnose only
    as far as the evidence carries, assert no negative the next tick falsifies.

    The divergence DOES establish which runtime's gauge reporter is implicated.
    WHY it stopped is not visible from here, and a notice asserting it would be
    a guess wearing a measurement's clothes. The prose must not point Codex to
    a Claude-specific hook, and must direct the reader to retained history."""
    state, reg, mgr, bridge_id = _wired()
    now = datetime.now(UTC)
    measured_at = now - timedelta(seconds=5400)
    last_alive = now - timedelta(seconds=30)
    _gauge(state, "agi-worker", measured_at=measured_at.isoformat())
    _ticking(state, "agi-worker", last_alive=last_alive)
    sweep_gauge_staleness(state, now=now, peer_registry=reg, bridge_manager=mgr)
    _, events = mgr.get(bridge_id).events_after(-1)
    body = events[0].content if events else ""
    _check(
        measured_at.isoformat() in body and last_alive.isoformat() in body,
        f"BOTH measured timestamps appear in the notice. Got: {body!r}",
    )
    _check(
        "that session's gauge reporter" in body
        and "session_context_status_history" in body
        and "upsert-only and keeps no history" not in body,
        "the runtime-neutral gauge writer and GAU-15 history verb are named",
    )
    _check(
        "likeliest cause" not in body and "transcript_path" not in body,
        "but no CAUSE is asserted — the leg cannot see which, and the detector "
        f"must outlive today's leading candidate. Got: {body!r}",
    )
    _check(
        "no session_context_status row at all" not in body,
        "and it never reuses the missing-row leg's wording — different cause, "
        f"different fix. Got: {body!r}",
    )


def test_gauge_stale_notifies_once_and_releases_on_recovery() -> None:
    """Latched like every sibling: an arrested gauge stays arrested until it is
    fixed, so unlatched this re-delivers every 300s for the whole outage. The
    release makes a RELAPSE a fresh notice rather than a silence — a real shape
    here, since a hook failing on one payload may succeed on the next."""
    state, reg, mgr, _ = _wired()
    now = datetime.now(UTC)
    latch = NoticeLatch()
    _gauge(state, "agi-worker", measured_at=(now - timedelta(seconds=5400)).isoformat())
    _ticking(state, "agi-worker", last_alive=now - timedelta(seconds=30))
    first = sweep_gauge_staleness(
        state, now=now, peer_registry=reg, bridge_manager=mgr, latch=latch,
    )
    second = sweep_gauge_staleness(
        state, now=now, peer_registry=reg, bridge_manager=mgr, latch=latch,
    )
    _check(first == 1 and second == 0, "one notice per episode, not one per tick")
    _gauge(state, "agi-worker", measured_at=(now - timedelta(seconds=10)).isoformat())
    recovered = sweep_gauge_staleness(
        state, now=now, peer_registry=reg, bridge_manager=mgr, latch=latch,
    )
    _check(recovered == 0, "a recovered gauge produces no notice")
    _gauge(state, "agi-worker", measured_at=(now - timedelta(seconds=5400)).isoformat())
    relapse = sweep_gauge_staleness(
        state, now=now, peer_registry=reg, bridge_manager=mgr, latch=latch,
    )
    _check(relapse == 1, "and a RELAPSE notifies again rather than staying silent")


def test_gauge_stale_leg_no_ops_without_a_bridge() -> None:
    """Same posture as its siblings: unwired is a no-op, never a fault."""
    state, _, _, _ = _wired()
    _check(
        sweep_gauge_staleness(state, now=datetime.now(UTC)) == 0,
        "the leg is inert without a peer registry and bridge manager",
    )

# ---------------------------------------------------------------------------
# GAU-25: DETECTIONS and DELIVERIES are two numbers and must never collapse
# ---------------------------------------------------------------------------


def _unresolvable_steward() -> tuple[StateManagementInterface, PeerRegistry, BridgeSessionManager]:
    """A worker whose spawner is registered NOWHERE — the GAU-26 shape, reduced.

    This is the fixture GAU-25 was blind to and it is the only one that
    discriminates: with a RESOLVABLE steward, detections and deliveries are
    equal and a collapsed counter looks correct. The two numbers only diverge
    when a real detection fails to deliver, so any test of this fix that wires a
    working steward proves nothing at all.
    """
    state, reg, mgr = _state(), _peer_registry(), _bridge_manager()
    _spawn_live(state, agent_instance_id="agi-worker", spawned_by_instance_id="agi-nobody")
    return state, reg, mgr


def test_gauge_stale_counts_the_detection_the_delivery_lost() -> None:
    """★ GAU-25's FILED SPECIMEN, as a test. The 2026-08-19 17:47:33Z tick
    detected a real arrested gauge, failed to resolve its steward, and reported
    ``L4d=0`` — because the leg's only number was the DELIVERY count.

    THE MUTATION THIS CATCHES, named explicitly: collapse the two numbers back
    into one — i.e. make ``_fill_counts`` receive ``detected=notified``, or drop
    the ``counts`` sink and let a caller read the ``int`` return as the
    detection count. Either way ``detected`` reads 0 here and this test fails.
    A green over a resolvable steward would survive that mutation; this fixture
    does not.
    """
    state, reg, mgr = _unresolvable_steward()
    now = datetime.now(UTC)
    _gauge(state, "agi-worker", measured_at=(now - timedelta(seconds=5400)).isoformat())
    _ticking(state, "agi-worker", last_alive=now - timedelta(seconds=30))
    counts = StewardNoticeCounts()
    delivered = sweep_gauge_staleness(
        state, now=now, peer_registry=reg, bridge_manager=mgr, counts=counts,
    )
    _check(delivered == 0, "nothing was delivered — the steward resolves to no binding")
    _check(
        counts.detected == 1,
        "★ but the DETECTOR fired, and the sweep says so. This is the number "
        "the operator-facing line could not previously report, and the whole "
        "of GAU-25: L4d=0 was consistent with any number of real detections",
    )
    _check(
        counts.undelivered == 1,
        "and the failed delivery is counted as its own population — an alarm "
        "nobody received, not a quiet tick",
    )
    _check(
        counts.delivered == delivered,
        "the sink's delivered field and the legacy int return are the SAME "
        "number — the return value's meaning is unchanged, which is what lets "
        "~86 existing call sites keep reading it",
    )


def _transitioned(
    state: StateManagementInterface, agent_instance_id: str, *, last_transition_at: datetime,
) -> None:
    """Set ``last_transition_at`` directly -- the GAU-22(c) rotation-window
    signal reads this column, and ``_spawn_live``'s own transition call stamps
    real wall-clock time, not a fixture-controlled one."""
    state.update_state(
        AGENT_ROLE_BINDING_NAMESPACE,
        {"table": "managed_session", "filters": {"agent_instance_id": agent_instance_id}},
        {"last_transition_at": last_transition_at.isoformat()},
    )


def test_gau22_rotation_window_holds_fire_inside_the_grace() -> None:
    """★ GAU-22(c), THE POSITIVE CASE. A rotation just happened
    (``last_transition_at`` newer than the gauge's ``measured_at``) and the
    gauge is stale PAST GAUGE_STALE_LAG_S purely because the successor has
    not written its first row yet -- this is the L4d false-positive GAU-22
    measured live (an 8.9-minute specimen), and it must NOT fire."""
    state, reg, mgr, _ = _wired()
    now = datetime.now(UTC)
    _gauge(state, "agi-worker", measured_at=(now - timedelta(seconds=1200)).isoformat())
    _ticking(state, "agi-worker", last_alive=now - timedelta(seconds=30))
    _transitioned(state, "agi-worker", last_transition_at=now - timedelta(seconds=300))
    n = sweep_gauge_staleness(state, now=now, peer_registry=reg, bridge_manager=mgr)
    _check(n == 0, "a rotation inside its grace window holds fire even past GAUGE_STALE_LAG_S")


def test_gau22_rotation_window_still_fires_once_grace_expires() -> None:
    """The negative control: a rotation whose successor STILL has not
    written a gauge row well past the grace window is a genuinely stuck
    reporter, not a transient rotation gap -- it must still alarm."""
    state, reg, mgr, _ = _wired()
    now = datetime.now(UTC)
    _gauge(state, "agi-worker", measured_at=(now - timedelta(seconds=1800)).isoformat())
    _ticking(state, "agi-worker", last_alive=now - timedelta(seconds=30))
    _transitioned(
        state, "agi-worker",
        last_transition_at=now - timedelta(seconds=GAUGE_STALE_ROTATION_GRACE_S + 60),
    )
    n = sweep_gauge_staleness(state, now=now, peer_registry=reg, bridge_manager=mgr)
    _check(n == 1, "a rotation-window finding past its own grace still fires -- the grace is not a suppression")


def test_gau22_a_transition_older_than_the_gauge_is_not_a_rotation_window() -> None:
    """CATCHES: the rotation-window check misfiring on a normal ongoing
    session whose last transition long predates its gauge -- this is NOT a
    rotation in progress, so the grace must not apply and the ordinary lag
    check (GAU-01(b)) must decide, unchanged."""
    state, reg, mgr, _ = _wired()
    now = datetime.now(UTC)
    _gauge(state, "agi-worker", measured_at=(now - timedelta(seconds=5400)).isoformat())
    _ticking(state, "agi-worker", last_alive=now - timedelta(seconds=30))
    _transitioned(state, "agi-worker", last_transition_at=now - timedelta(seconds=7200))
    n = sweep_gauge_staleness(state, now=now, peer_registry=reg, bridge_manager=mgr)
    _check(n == 1, "an old transition (not a rotation-in-progress) leaves the ordinary GAU-01(b) alarm intact")


def test_every_steward_leg_reports_detections_separately_from_deliveries() -> None:
    """The audit, as an assertion: L4a and L4b carried the IDENTICAL defect, so
    fixing only the filed leg (L4d) would have left two live instances of it.

    A ruling's footprint is always larger than the first count. L4c is NOT here
    because it was already honest — it reports appended/unroutable/undeliverable
    and cannot print all-zero while a detection went undelivered.
    """
    now = datetime.now(UTC)

    state, reg, mgr = _unresolvable_steward()
    _gauge(state, "agi-worker")
    due = StewardNoticeCounts()
    _check(
        sweep_rotation_due_sessions(
            state, peer_registry=reg, bridge_manager=mgr, counts=due,
        ) == 0 and due.detected == 1 and due.undelivered == 1,
        "L4a (rotation-due) reports its detection even when delivery fails",
    )

    state, reg, mgr = _unresolvable_steward()
    dark = StewardNoticeCounts()
    _check(
        sweep_gauge_coverage(
            state, now=_past_grace(), peer_registry=reg, bridge_manager=mgr, counts=dark,
        ) == 0 and dark.detected == 1 and dark.undelivered == 1,
        "L4b (gauge-coverage) reports its detection even when delivery fails",
    )

    state, reg, mgr = _unresolvable_steward()
    _gauge(state, "agi-worker", measured_at=(now - timedelta(seconds=5400)).isoformat())
    _ticking(state, "agi-worker", last_alive=now - timedelta(seconds=30))
    stale = StewardNoticeCounts()
    _check(
        sweep_gauge_staleness(
            state, now=now, peer_registry=reg, bridge_manager=mgr, counts=stale,
        ) == 0 and stale.detected == 1 and stale.undelivered == 1,
        "L4d (gauge-staleness) reports its detection even when delivery fails",
    )


def test_latch_suppressed_detections_stay_visible_but_do_not_re_warn() -> None:
    """The two halves of the latch decision, pinned together because they pull
    in opposite directions and a fix that got one right could get the other
    wrong silently.

    A session already notified stays in ``detected`` (the condition still
    holds, and the operator line must not claim the fleet is clean) but is NOT
    in ``actionable`` (so the WARNING does not re-fire every 300s for the whole
    of an outage already reported).
    """
    state, reg, mgr, _bridge_id = _wired()
    now = datetime.now(UTC)
    _gauge(state, "agi-worker", measured_at=(now - timedelta(seconds=5400)).isoformat())
    _ticking(state, "agi-worker", last_alive=now - timedelta(seconds=30))
    latch = NoticeLatch()
    first, second = StewardNoticeCounts(), StewardNoticeCounts()
    sweep_gauge_staleness(
        state, now=now, peer_registry=reg, bridge_manager=mgr, latch=latch, counts=first,
    )
    sweep_gauge_staleness(
        state, now=now, peer_registry=reg, bridge_manager=mgr, latch=latch, counts=second,
    )
    _check(
        (first.detected, first.delivered, first.actionable) == (1, 1, 1),
        "the first tick detects, delivers, and is actionable",
    )
    _check(
        (second.detected, second.delivered, second.undelivered) == (1, 0, 0),
        "★ the second tick STILL REPORTS THE DETECTION — a suppressed notice "
        "must not make the operator line read as an all-clear",
    )
    _check(
        second.actionable == 0,
        "but it is not actionable, so the WARNING stays quiet across an "
        "outage already reported — which is what the latch is for",
    )


def test_the_rider_actually_passes_the_sink_and_prints_both_numbers() -> None:
    """★ THE INERT-KNOB GUARD. Every assertion above would pass unchanged if the
    rider never passed a sink at all — the legs would fill nothing, the log line
    would print zeros, and the fix would be dead on the surface it exists for.
    A config knob that is never passed is inert, and a green over an inert knob
    is a green that lies.

    So this drives the REAL ``_run_rotation_surface_sweep`` against a spy and
    asserts three things the unit tests structurally cannot:

    1. the rider hands each leg a ``StewardNoticeCounts``;
    2. the emitted line carries the DETECTED count, not the delivered one;
    3. the WARNING fires on a tick with detections and ZERO deliveries — the
       ``_run_counted_leg`` gate reads the value the leg lambda returns, so a
       lambda that returned the delivered count would emit no warning at all.
    """
    import logging
    from types import SimpleNamespace

    import agent_messaging_plugin.plugin as plugin_mod
    from agent_messaging_plugin import rotation_self_notice

    saved = (
        plugin_mod.sweep_rotation_due_sessions,
        plugin_mod.sweep_gauge_coverage,
        plugin_mod.sweep_rotation_self_notice,
        plugin_mod.sweep_gauge_staleness,
    )
    seen: dict[str, object] = {}

    def _quiet(*_args: object, **kwargs: object) -> int:
        seen.setdefault("quiet_sink", kwargs.get("counts"))
        return 0

    def _stale_spy(*_args: object, **kwargs: object) -> int:
        sink = kwargs.get("counts")
        seen["stale_sink"] = sink
        if isinstance(sink, StewardNoticeCounts):
            # Three real detections, none of them delivered: the exact shape of
            # the 17:47:33Z specimen, scaled up so a collapsed counter cannot
            # coincidentally match.
            sink.detected, sink.delivered, sink.undelivered = 3, 0, 3
        return 0

    plugin_mod.sweep_rotation_due_sessions = _quiet  # type: ignore[assignment]
    plugin_mod.sweep_gauge_coverage = _quiet  # type: ignore[assignment]
    plugin_mod.sweep_rotation_self_notice = (  # type: ignore[assignment]
        lambda *_a, **_k: rotation_self_notice.SelfNoticeCounts()
    )
    plugin_mod.sweep_gauge_staleness = _stale_spy  # type: ignore[assignment]

    records: list[logging.LogRecord] = []

    class _Capture(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append(record)

    handler = _Capture()
    plugin_mod.logger.addHandler(handler)
    prior_level = plugin_mod.logger.level
    plugin_mod.logger.setLevel(logging.INFO)
    try:
        fake_self = SimpleNamespace(
            _log_self_notice_counts=plugin_mod.AgentMessagingPlugin._log_self_notice_counts,  # noqa: SLF001
            _get_state_service=lambda: object(),
            _peer_registry=object(),
            _bridge_manager=object(),
            _rotation_due_latch=object(),
            _gauge_coverage_latch=object(),
            _gauge_stale_latch=object(),
            _rotation_self_latch=object(),
            _require_service=lambda: object(),
        )
        plugin_mod.AgentMessagingPlugin._run_rotation_surface_sweep(  # noqa: SLF001
            cast("Any", fake_self),
        )
    finally:
        plugin_mod.logger.removeHandler(handler)
        plugin_mod.logger.setLevel(prior_level)
        (
            plugin_mod.sweep_rotation_due_sessions,
            plugin_mod.sweep_gauge_coverage,
            plugin_mod.sweep_rotation_self_notice,
            plugin_mod.sweep_gauge_staleness,
        ) = saved  # type: ignore[assignment]

    _check(
        isinstance(seen.get("stale_sink"), StewardNoticeCounts),
        "★ the rider PASSES a counts sink to the L4d leg. Without this the "
        "whole fix is an inert knob and every other assertion here is vacuous",
    )
    _check(
        isinstance(seen.get("quiet_sink"), StewardNoticeCounts),
        "and to the L4a/L4b legs too — the audit fixed all three, not just the "
        "one that was filed",
    )
    summary = next(
        (r.getMessage() for r in records if "rotation surface swept" in r.getMessage()),
        "",
    )
    _check(
        "L4d=3 detected(0 delivered/3 undelivered)" in summary,
        "★ the operator-facing line prints DETECTIONS and DELIVERIES as "
        "separate numbers. The pre-fix line for this exact tick read "
        f"'L4d=0 session(s) with an arrested gauge row'. Got: {summary!r}",
    )
    _check(
        "L4d=0 session(s)" not in summary,
        "and the old collapsed spelling is gone — nobody can read the new line "
        "the way L4d=0 used to be read",
    )
    _check(
        any(
            r.levelno == logging.WARNING and "stopped advancing" in r.getMessage()
            for r in records
        ),
        "★ and the WARNING fires on a tick with 3 detections and 0 deliveries. "
        "_run_counted_leg gates on the leg lambda's return value, so this is "
        "the assertion that pins the lambda returning delivered+undelivered "
        "rather than delivered — the same GAU-25 defect one level up",
    )


def main() -> int:
    test_managed_tmux_death_converges_and_deduplicates()
    test_managed_probe_fault_is_unknown()
    test_managed_dispatch_sweep_is_uncapped()
    test_overdue_no_report_by_never_swept()
    test_overdue_marks_past_deadline_live_and_idle()
    test_overdue_skips_future_deadline()
    test_overdue_notifies_steward()
    test_overdue_fresh_heartbeat_is_quiet_but_the_row_stays_lapsed()
    test_overdue_stale_heartbeat_keeps_the_full_alarm_and_wake()
    test_overdue_notifies_unmanaged_steward()
    test_overdue_notifies_a_watch_id_registered_steward()
    test_overdue_join_reads_the_session_id_and_never_derives_it()
    test_overdue_bridge_bound_steward_keeps_its_direct_route()
    test_overdue_steward_row_without_a_session_id_is_best_effort()
    test_overdue_ambiguous_session_id_is_never_a_guessed_recipient()
    test_overdue_alarm_that_reached_nobody_is_still_recorded()
    test_overdue_delivered_alarm_records_the_steward_it_reached()
    test_overdue_record_carries_the_measured_lateness()
    test_overdue_no_spawner_is_silent_noop()
    test_overdue_unresolvable_spawner_is_best_effort()
    test_overdue_marks_without_notify_when_registry_absent()
    test_overdue_terminates_stuck_spawning_row()
    test_overdue_skips_spawning_row_with_future_deadline()
    test_overdue_spawning_alive_row_is_extended_not_reaped()
    test_overdue_spawning_alive_has_no_lifetime_cap()
    test_overdue_spawning_operator_host_alive_is_not_evidence()
    test_spawning_native_probe_never_infers_death_from_age_or_fault()
    test_overdue_spawning_notifies_steward_of_orphan()
    test_deadline_dependency_not_yet_due_skipped()
    test_deadline_dependency_fires_and_delivers()
    test_deadline_dependency_unmanaged_waiter_still_delivers()
    test_deadline_dependency_unresolvable_waiter_is_best_effort()
    test_deadline_dependency_lane_scoped_is_logged_noop()
    test_lane_closed_empty_lane_is_not_closed()
    test_lane_closed_open_while_any_session_non_terminal()
    test_lane_closed_fires_when_every_session_terminal()
    test_pruner_terminal_managed_session_pruned_immediately()
    test_pruner_live_managed_session_never_pruned()
    test_pruner_live_registered_session_never_pruned()
    test_pruner_absence_within_grace_window_not_pruned()
    test_pruner_absence_past_grace_window_pruned()
    test_pruner_pages_claims_without_a_target_query_state_read()
    test_retire_session_crash_mid_retire_is_redrivable()
    test_registration_within_bound_is_not_marked()
    test_registration_past_bound_marks_field_not_state()
    test_registration_watchdog_never_reaps()
    test_registration_fires_without_any_report_by()
    test_registration_mark_is_idempotent_and_keeps_first_observation()
    test_registration_late_registration_clears_the_mark()
    test_registration_non_spawning_rows_are_never_marked()
    test_registration_acknowledged_degraded_is_marked_but_says_so()
    test_registration_notifies_steward_with_distinct_event()
    test_registration_marks_without_notify_when_registry_absent()

    test_rotation_due_notice_carries_the_measured_number()
    test_rotation_due_is_silent_below_the_threshold()
    test_rotation_due_uses_the_runtime_window_minimum()
    test_gauge_coverage_catches_a_live_session_with_no_row()
    test_gauge_coverage_is_silent_when_the_row_exists()
    test_l4a_legs_no_op_without_a_bridge()

    test_gauge_coverage_grants_a_newly_live_session_its_startup_grace()
    test_gauge_coverage_still_fires_once_the_grace_expires()
    test_gauge_coverage_does_not_grant_grace_on_an_unreadable_timestamp()
    test_gauge_coverage_grace_covers_the_measured_boot_to_first_tick()
    test_gauge_coverage_notice_states_the_measurement_not_the_inference()
    test_gauge_coverage_notice_says_when_no_reporter_has_run_at_all()
    test_gauge_coverage_notice_names_the_evidence_when_the_session_has_ticked()


    test_rotation_due_notifies_once_per_episode()
    test_rotation_due_latch_rearms_when_the_session_rotates()
    test_rotation_due_latch_does_not_swallow_an_undelivered_notice()
    test_gauge_coverage_notifies_once_and_releases_on_recovery()
    test_latches_are_independent_per_notice_kind()

    test_last_report_alive_derives_the_tick_moment()
    test_gauge_stale_fires_when_alive_and_the_gauge_arrested()
    test_gauge_stale_names_carried_forward_heartbeat_failures()
    test_gauge_stale_is_silent_when_both_clocks_stopped()
    test_gauge_stale_is_silent_on_a_healthy_throttle_skew()
    test_gauge_stale_threshold_is_a_boundary_not_a_vibe()
    test_gauge_stale_leaves_the_missing_row_to_the_coverage_leg()
    test_gauge_stale_treats_a_missing_window_as_no_evidence()
    test_gauge_stale_notice_states_both_clocks_and_names_no_cause()
    test_gauge_stale_notifies_once_and_releases_on_recovery()
    test_gauge_stale_leg_no_ops_without_a_bridge()
    test_gauge_stale_counts_the_detection_the_delivery_lost()
    test_gau22_rotation_window_holds_fire_inside_the_grace()
    test_gau22_rotation_window_still_fires_once_grace_expires()
    test_gau22_a_transition_older_than_the_gauge_is_not_a_rotation_window()
    test_every_steward_leg_reports_detections_separately_from_deliveries()
    test_latch_suppressed_detections_stay_visible_but_do_not_re_warn()
    test_the_rider_actually_passes_the_sink_and_prints_both_numbers()

    print()
    print(f"PASSED: {_passed}")
    print(f"FAILED: {len(_failed)}")
    for label in _failed:
        print(f"  - {label}")
    return 1 if _failed else 0


if __name__ == "__main__":
    sys.exit(main())
