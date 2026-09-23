#!/usr/bin/env python3
"""REL-09 — bridge-lifecycle full-cleanup smoke (no pytest).

Kills the class where an idle-dead bridge diverges from a cleanly-closed
one. Pre-REL-09, ``sweep_idle`` had NO driver, and even a driven sweep
would only have popped the bridge — leaving the inference-provider sidecar,
the ◆R2 tombstone, and the durable peer_binding row behind (silent-Qwen on
the roleless lane + the 40+ zombie-row class). Cases:

  A  run_full_bridge_cleanup: exact step order (sidecar clear → Trigger-2
     hook → unregister); best-effort guards (a raising clear/hook never
     blocks the unregister); None hooks tolerated.
  B  BridgeLifecycleSweeper over a REAL BridgeSessionManager: an idle
     bridge is expired + fully cleaned, a fresh one untouched; the daemon
     thread ticks, stops, and stop() is idempotent.
  C  ★ Trigger-2-on-sweep (deliberate decision, coordinator-concurred):
     an idle-swept sys:autonomic holder fires grace succession — the
     newest live provider-capable session claims, with EXACTLY ONE
     new-holder notice and ZERO displaced notices (the swept prior is
     genuinely ended; no double-fire with the REL-04 machinery).
     The swept-roleless→tombstone→DEFER resolver behavior itself is
     covered by session_inference_provider_smoke (case 3b); here the
     cleanup's tombstoning step is asserted at the wiring level.
  D  purge_preboot_bindings: every persisted row (all pre-restart by
     construction at boot) is unregistered per source bridge.
  E  close-route convergence (TestClient): POST /close runs the SAME
     cleanup sequence — swept and closed are indistinguishable.
  F  INF-06 wiring guard: the composed _on_sweep_tick INVOKES both forwarded
     riders (sweep_serve_timeouts + gc_terminal_rows) AND the D1
     session-lifecycle sweep, even when the INF-02 completion sweep raises —
     a silent un-wiring is the stall this slice prevents, so it must be
     regression-guarded. (A4, 2026-08-04: the REL-05 deaf-wake reconciler
     rider retired from this tick; D1's session sweep is the surviving rider
     this case now asserts survives a raising INF-02 sweep.)
     ★ L4 (2026-08-17): the same guard now covers the rotation-surface rider.
     It needs the guard MORE than the others do: the rider is wrapped in its
     own try/except, so an un-wiring that leaves a duck-typed self without the
     attribute raises INSIDE the guard and is logged, not failed — the tick
     goes green while no notice is ever delivered. Asserting invocation is the
     only thing that can tell "composed" from "composed and swallowed".
     ★ R4 (2026-08-17): and now the TTL-surface rider, the third independently
     fault-isolated leg, for exactly the same reason and with the same two
     assertions — it is INVOKED, and its own fault does not cost the other two
     their tick (F4), just as theirs do not cost it its own.

Run:
    SOLET_NAME=<name>-test .venv/bin/python3 \
        plugins/agent_messaging_plugin/tests/bridge_lifecycle_sweep_smoke.py
"""

from __future__ import annotations

import sys
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT / "ananta" / "src"))
sys.path.insert(0, str(REPO_ROOT / "plugins" / "agent_messaging_plugin" / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _real_state_fake import RealShapeState  # noqa: E402
from ananta.llm.agent_messaging.role_binding import SYS_AUTONOMIC_SLOT  # noqa: E402
from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from agent_messaging_plugin.autonomic_assignment import AutonomicAssignment  # noqa: E402
from agent_messaging_plugin.bridge_lifecycle import (  # noqa: E402
    BridgeLifecycleSweeper,
    purge_preboot_bindings,
    run_full_bridge_cleanup,
)
from agent_messaging_plugin.bridge_sessions import BridgeSessionManager  # noqa: E402
from agent_messaging_plugin.http_routes import register_routes  # noqa: E402
from agent_messaging_plugin.models import BridgeBinding  # noqa: E402
from agent_messaging_plugin.plugin import AgentMessagingPlugin  # noqa: E402
from agent_messaging_plugin.role_binding_store import (  # noqa: E402
    RoleBindingVacantError,
    resolve_role_binding_v4,
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


def _manager(idle_timeout_s: int = 3600) -> BridgeSessionManager:
    return BridgeSessionManager(
        session_id_factory=lambda _name: "ses-smoke",
        idle_timeout_s=idle_timeout_s,
        max_pending_events=50,
        long_poll_timeout_s=1,
    )


def _binding(bridge_id: str, agi: str, sid: str) -> BridgeBinding:
    return BridgeBinding(
        bridge_id=bridge_id,
        agent_id="claude_code",
        agent_instance_id=agi,
        session_label=agi,
        parent_pid=None,
        agent_session_id=sid,
    )


def _cleanup_order_cases() -> None:
    print("A — run_full_bridge_cleanup sequence:")

    calls: list[str] = []
    removed = run_full_bridge_cleanup(
        "br-1",
        inference_provider_clear=lambda _b: (calls.append("clear"), 1)[1],
        autonomic_on_close=lambda _b: (calls.append("autonomic"), "scheduled")[1],
        unregister=lambda _b: (calls.append("unregister"), 2)[1],
    )
    _check(
        calls == ["clear", "autonomic", "unregister"] and removed == 2,
        "A1 order: sidecar clear → Trigger-2 hook → unregister (count returned)",
    )

    def _boom(_bridge_id: str) -> int:
        raise RuntimeError("simulated fault")

    calls2: list[str] = []
    removed = run_full_bridge_cleanup(
        "br-2",
        inference_provider_clear=_boom,
        autonomic_on_close=_boom,  # type: ignore[arg-type]
        unregister=lambda _b: (calls2.append("unregister"), 1)[1],
    )
    _check(
        calls2 == ["unregister"] and removed == 1,
        "A2 best-effort: raising clear/hook never blocks the unregister",
    )

    removed = run_full_bridge_cleanup(
        "br-3",
        inference_provider_clear=None,
        autonomic_on_close=None,
        unregister=lambda _b: 0,
    )
    _check(removed == 0, "A3 None hooks tolerated (unregister-only path)")


def _sweeper_cases() -> None:
    print("B — sweeper over a real BridgeSessionManager:")

    manager = _manager(idle_timeout_s=3600)
    fresh = manager.open(solet_name="example-test")
    stale = manager.open(solet_name="example-test")
    stale.last_seen_at = (datetime.now(UTC) - timedelta(hours=2)).isoformat()
    cleaned: list[str] = []
    sweeper = BridgeLifecycleSweeper(
        bridge_manager=manager,
        cleanup=lambda bid: (cleaned.append(bid), 1)[1],
        interval_seconds=3600,
    )
    expired = sweeper.sweep_once()
    live_ids = {b.bridge_id for b in manager.list_active()}
    _check(
        expired == [stale.bridge_id] and cleaned == [stale.bridge_id]
        and live_ids == {fresh.bridge_id},
        "B1 idle bridge expired + fully cleaned; fresh bridge untouched",
    )

    manager2 = _manager(idle_timeout_s=0)
    manager2.open(solet_name="example-test").last_seen_at = (
        datetime.now(UTC) - timedelta(seconds=5)
    ).isoformat()
    ticked = threading.Event()
    sweeper2 = BridgeLifecycleSweeper(
        bridge_manager=manager2,
        cleanup=lambda _bid: (ticked.set(), 0)[1],
        interval_seconds=0,  # Event.wait(0) → immediate ticks until stop()
    )
    sweeper2.start()
    tick_seen = ticked.wait(timeout=2.0)
    sweeper2.stop()
    sweeper2.stop()  # idempotent
    thread_dead = sweeper2._thread is None  # noqa: SLF001 — smoke asserts lifecycle
    _check(
        tick_seen and thread_dead,
        "B2 daemon thread ticks, stop() joins and is idempotent",
    )


class _World:
    """Mini-harness for the Trigger-2-on-sweep end-to-end case."""

    def __init__(self) -> None:
        self.state = RealShapeState()
        self.bridges: list[Any] = []
        self.bindings: dict[str, list[BridgeBinding]] = {}
        self.live_providers: set[str] = set()
        self.live_by_session: dict[str, BridgeBinding] = {}
        self.notices: list[tuple[str, str]] = []
        self.tombstoned: list[str] = []

    def add(self, bridge_id: str, agi: str, sid: str, created_at: str) -> BridgeBinding:
        bound = _binding(bridge_id, agi, sid)
        self.bridges.append(
            type("B", (), {"bridge_id": bridge_id, "created_at": created_at})(),
        )
        self.bindings[bridge_id] = [bound]
        self.live_providers.add(agi)
        self.live_by_session[sid] = bound
        return bound

    def assignment(self) -> AutonomicAssignment:
        return AutonomicAssignment(
            state_service=lambda: self.state,
            list_active_bridges=lambda: list(self.bridges),
            bindings_for_bridge=lambda bid: list(self.bindings.get(bid, [])),
            live_binding_for_session=self.live_by_session.get,
            has_live_provider=self.live_providers.__contains__,
            send_notice=self._notice,
            grace_seconds=30,
            # INF-02 collaborators — inert here; the completion-handler
            # matrix lives in autonomic_completion_smoke.py.
            forward_completion=lambda _holder, _row: None,
            serve_window_seconds=900,
            # INF-06 collaborators — inert here; the forwarded sweep/drain/GC
            # matrix lives in forward_vertex_redrive_smoke.py.
            resubmit_vertex=lambda _flow, _method: False,
            forward_serve_window_seconds=900,
            forward_attempts_cap=5,
            terminal_gc_after_seconds=172_800,
        )

    def _notice(
        self, *, peer_id: str, peer_agent_instance_id: str, prose: str, kind: str,
    ) -> bool:
        del peer_id, prose
        self.notices.append((peer_agent_instance_id, kind))
        return True

    def clear_for_bridge(self, bridge_id: str) -> int:
        """Models _clear_inference_providers_for_bridge: pop provider + tombstone."""
        cleared = 0
        for bound in self.bindings.get(bridge_id, []):
            if bound.agent_instance_id in self.live_providers:
                self.live_providers.discard(bound.agent_instance_id)
                self.tombstoned.append(bound.agent_instance_id)
                cleared += 1
        return cleared

    def unregister(self, bridge_id: str) -> int:
        removed = len(self.bindings.get(bridge_id, []))
        for bound in self.bindings.get(bridge_id, []):
            self.live_by_session.pop(bound.agent_session_id, None)
        self.bindings.pop(bridge_id, None)
        self.bridges = [b for b in self.bridges if b.bridge_id != bridge_id]
        return removed

    def holder_agi(self) -> str | None:
        try:
            return resolve_role_binding_v4(
                cast("Any", self.state), SYS_AUTONOMIC_SLOT,
            ).agent_instance_id
        except RoleBindingVacantError:
            return None


def _trigger2_on_sweep_cases() -> None:
    print("C — ★ swept sys:autonomic holder fires grace succession:")

    world = _World()
    assignment = world.assignment()
    holder = world.add("br-h", "agi-h", "sid-h", "2026-07-03T01:00:00")
    assignment.on_register(
        agent_id=holder.agent_id,
        agent_instance_id=holder.agent_instance_id,
        agent_session_id=holder.agent_session_id,
        session_label=holder.session_label,
        provides_inference=True,
    )
    world.add("br-o", "agi-o", "sid-o", "2026-07-03T02:00:00")
    world.notices.clear()
    departed = resolve_role_binding_v4(cast("Any", world.state), SYS_AUTONOMIC_SLOT)

    # The sweep path: full cleanup for the holder's bridge (as the sweeper
    # would run it), then the grace timer body.
    run_full_bridge_cleanup(
        "br-h",
        inference_provider_clear=world.clear_for_bridge,
        autonomic_on_close=assignment.on_bridge_close,
        unregister=world.unregister,
    )
    assignment.cancel_all()  # smoke drives the timer body directly below
    _check(
        world.tombstoned == ["agi-h"],
        "C1 sweep cleanup TOMBSTONES the swept holder (silent-Qwen kill, wiring)",
    )
    token = assignment._succession_check(departed)  # noqa: SLF001 — drives the timer body
    _check(
        token == "succeeded" and world.holder_agi() == "agi-o",
        "C2 succession after sweep: newest live provider-capable claims",
    )
    _check(
        world.notices == [("agi-o", "autonomic-new-holder")],
        "C3 exactly ONE new-holder notice, ZERO displaced (ended prior silent)",
    )


def _purge_cases() -> None:
    print("D — startup reconciliation purge:")

    class _Registry:
        def __init__(self) -> None:
            self.rows = {
                "br-a": [_binding("br-a", "agi-1", "sid-1")],
                "br-b": [
                    _binding("br-b", "agi-2", "sid-2"),
                    _binding("br-b", "agi-3", "sid-3"),
                ],
            }
            self.unregistered: list[str] = []

        def list_agent_ids(self) -> dict[str, list[BridgeBinding]]:
            return {"claude_code": [b for rows in self.rows.values() for b in rows]}

        def unregister(self, bridge_id: str) -> int:
            self.unregistered.append(bridge_id)
            return len(self.rows.pop(bridge_id, []))

    registry = _Registry()
    removed = purge_preboot_bindings(registry)
    _check(
        removed == 3 and sorted(registry.unregistered) == ["br-a", "br-b"]
        and registry.rows == {},
        "D1 every pre-boot row purged, per source bridge",
    )

    empty = _Registry()
    empty.rows = {}
    _check(
        purge_preboot_bindings(empty) == 0,
        "D2 empty registry → zero purged (quiet no-op)",
    )


def _close_route_convergence_cases() -> None:
    print("E — close route runs the same cleanup (TestClient):")

    manager = _manager()
    calls: list[str] = []

    class _Registry:
        def unregister(self, bridge_id: str) -> int:
            del bridge_id
            calls.append("unregister")
            return 1

    app = FastAPI()
    stub = object()
    register_routes(
        app,
        bridge_manager=manager,
        peer_registry=cast("Any", _Registry()),
        platform_surface=cast("Any", stub),
        agent_messaging_service=stub,
        config={"long_poll_timeout_seconds": 1},
        inference_provider_clear=lambda _b: (calls.append("clear"), 1)[1],
        autonomic_on_close=lambda _b: (calls.append("autonomic"), "not_holder")[1],
    )
    with TestClient(app) as client:
        opened = client.post("/api/v1/bridge/open", json={})
        bridge_id = opened.json()["bridge_id"]
        closed = client.post(f"/api/v1/bridge/{bridge_id}/close", json={})
    _check(
        closed.status_code == 200
        and calls == ["clear", "autonomic", "unregister"]
        and manager.get(bridge_id) is None,
        "E1 POST /close → identical cleanup sequence + bridge closed",
    )


class _ForwardedRiderSpy:
    """Stand-in for AutonomicAssignment.forwarded — records rider invocations."""

    def __init__(self, calls: list[str]) -> None:
        self._calls = calls

    def sweep_serve_timeouts(self) -> tuple[int, int]:
        self._calls.append("forwarded.sweep_serve_timeouts")
        return (0, 0)

    def gc_terminal_rows(self) -> int:
        self._calls.append("forwarded.gc_terminal_rows")
        return 0


def _sweep_tick_forwarded_rider_cases() -> None:
    """F — the composed sweeper tick INVOKES the INF-06 forwarded riders.

    Regression-guard (reviewer RIDER-1): the forwarded serve-timeout sweep +
    terminal-row GC ARE the recovery path for a died/timed-out holder; a future
    silent un-wiring of them from ``_on_sweep_tick`` would silently reintroduce
    the exact stall this slice exists to prevent, and nothing else guards the
    wiring. Drives the REAL ``AgentMessagingPlugin._on_sweep_tick`` against a
    duck-typed self and asserts BOTH forwarded riders fire — including when the
    INF-02 completion sweep RAISES (the tick's per-rider fault-isolation must not
    let one rider's fault skip the forwarded recovery riders).
    """
    print("F — _on_sweep_tick invokes the INF-06 forwarded riders:")

    calls: list[str] = []
    swept: list[str] = []
    autonomic = SimpleNamespace(
        completions=SimpleNamespace(
            sweep_serve_timeouts=lambda: calls.append("completions.sweep"),
        ),
        forwarded=_ForwardedRiderSpy(calls),
    )
    fake_self = SimpleNamespace(
        _autonomic_assignment=autonomic,
        _run_session_lifecycle_sweep=lambda: swept.append("d1_sweep"),
        _run_rotation_surface_sweep=lambda: swept.append("l4_rotation_surface"),
        _run_ttl_surface_sweep=lambda: swept.append("r4_ttl_surface"),
    )
    AgentMessagingPlugin._on_sweep_tick(cast("Any", fake_self))  # noqa: SLF001
    _check(
        "forwarded.sweep_serve_timeouts" in calls
        and "forwarded.gc_terminal_rows" in calls,
        "F1 _on_sweep_tick INVOKES both INF-06 forwarded riders (sweep + GC)",
    )
    _check(
        swept == ["d1_sweep", "l4_rotation_surface", "r4_ttl_surface"],
        "F1 _on_sweep_tick invokes the D1 session sweep AND the L4 rotation-surface "
        "rider AND the R4 TTL rider -- an un-invoked rider delivers no notice "
        "however well it is tested",
    )

    # A raising INF-02 completion sweep must NOT skip the forwarded riders or
    # the D1 session-lifecycle sweep (per-rider fault-isolation).
    def _boom() -> None:
        raise RuntimeError("INF-02 sweep fault")

    calls.clear()
    swept.clear()
    autonomic2 = SimpleNamespace(
        completions=SimpleNamespace(sweep_serve_timeouts=_boom),
        forwarded=_ForwardedRiderSpy(calls),
    )
    fake_self2 = SimpleNamespace(
        _autonomic_assignment=autonomic2,
        _run_session_lifecycle_sweep=lambda: swept.append("d1_sweep"),
        _run_rotation_surface_sweep=lambda: swept.append("l4_rotation_surface"),
        _run_ttl_surface_sweep=lambda: swept.append("r4_ttl_surface"),
    )
    AgentMessagingPlugin._on_sweep_tick(cast("Any", fake_self2))  # noqa: SLF001
    _check(
        "forwarded.sweep_serve_timeouts" in calls
        and "forwarded.gc_terminal_rows" in calls
        and swept == ["d1_sweep", "l4_rotation_surface", "r4_ttl_surface"],
        "F2 a RAISING INF-02 sweep does not skip the forwarded riders / D1 sweep / "
        "L4 rotation surface / R4 TTL rider",
    )

    # F3 — the fault-isolation runs BOTH ways: a raising D1 sweep must not cost
    # the tick its L4 notices, and a raising L4 rider must not cost the D1
    # sweep. The two are separate try/excepts precisely so neither can.
    def _d1_boom() -> None:
        raise RuntimeError("D1 sweep fault")

    calls.clear()
    swept.clear()
    autonomic3 = SimpleNamespace(
        completions=SimpleNamespace(sweep_serve_timeouts=lambda: calls.append("completions.sweep")),
        forwarded=_ForwardedRiderSpy(calls),
    )
    fake_self3 = SimpleNamespace(
        _autonomic_assignment=autonomic3,
        _run_session_lifecycle_sweep=_d1_boom,
        _run_rotation_surface_sweep=lambda: swept.append("l4_rotation_surface"),
        _run_ttl_surface_sweep=lambda: swept.append("r4_ttl_surface"),
    )
    AgentMessagingPlugin._on_sweep_tick(cast("Any", fake_self3))  # noqa: SLF001
    _check(
        swept == ["l4_rotation_surface", "r4_ttl_surface"],
        "F3 a RAISING D1 sweep does not skip the L4 rotation-surface or R4 TTL riders",
    )

    def _l4_boom() -> None:
        raise RuntimeError("L4 rotation-surface fault")

    swept.clear()
    fake_self4 = SimpleNamespace(
        _autonomic_assignment=SimpleNamespace(
            completions=SimpleNamespace(sweep_serve_timeouts=lambda: None),
            forwarded=_ForwardedRiderSpy(calls),
        ),
        _run_session_lifecycle_sweep=lambda: swept.append("d1_sweep"),
        _run_rotation_surface_sweep=_l4_boom,
        _run_ttl_surface_sweep=lambda: swept.append("r4_ttl_surface"),
    )
    AgentMessagingPlugin._on_sweep_tick(cast("Any", fake_self4))  # noqa: SLF001
    _check(
        swept == ["d1_sweep", "r4_ttl_surface"],
        "F3 a RAISING L4 rotation-surface rider does not skip the D1 sweep or the "
        "R4 TTL rider",
    )

    # F4 — the R4 TTL rider is a THIRD independently fault-isolated rider, so it
    # gets the same two guarantees as its neighbours: it must be INVOKED, and its
    # own fault must not cost the others their tick. The invocation half is not
    # ceremony — the rider sits inside its own try/except, so un-wiring it from
    # _on_sweep_tick raises INSIDE the guard, is logged, and leaves the tick
    # green while no TTL notice is ever delivered. Only asserting invocation
    # separates "composed" from "composed and swallowed".
    def _r4_boom() -> None:
        raise RuntimeError("R4 TTL-surface fault")

    swept.clear()
    fake_self5 = SimpleNamespace(
        _autonomic_assignment=SimpleNamespace(
            completions=SimpleNamespace(sweep_serve_timeouts=lambda: None),
            forwarded=_ForwardedRiderSpy(calls),
        ),
        _run_session_lifecycle_sweep=lambda: swept.append("d1_sweep"),
        _run_rotation_surface_sweep=lambda: swept.append("l4_rotation_surface"),
        _run_ttl_surface_sweep=_r4_boom,
    )
    AgentMessagingPlugin._on_sweep_tick(cast("Any", fake_self5))  # noqa: SLF001
    _check(
        swept == ["d1_sweep", "l4_rotation_surface"],
        "F4 a RAISING R4 TTL rider does not skip the D1 sweep or the L4 rotation "
        "surface",
    )


def main() -> int:
    print("=== REL-09 bridge-lifecycle full-cleanup smoke ===")
    _cleanup_order_cases()
    _sweeper_cases()
    _trigger2_on_sweep_cases()
    _purge_cases()
    _close_route_convergence_cases()
    _sweep_tick_forwarded_rider_cases()
    total = _passed + len(_failed)
    print(f"\n{_passed}/{total} checks passed")
    if _failed:
        print("FAILED:")
        for label in _failed:
            print(f"  - {label}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
