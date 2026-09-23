#!/usr/bin/env python3
"""INF-01 sub-slice-2 — sys:autonomic auto-assignment lifecycle smoke (no pytest).

Drives the §D.9 matrix offline against ``AutonomicAssignment`` with the shared
REAL-SHAPE state fake (real provider ActionResult envelopes → the production
extraction paths run) plus in-memory bridge/registry/provider collaborators:

  T1  vacancy-fill-at-boot: register(provides_inference) on a VACANT slot →
      claim, new-holder notice, drain invoked (lane re-lit).
  T2  non-provider register → ``not_provider`` (slot untouched).
  T3  live-held slot + another register → ``held`` (no displacement).
  T4  crash-heal: holder's provider gone → next register claims; the DEAD
      prior gets NO displaced notice (genuinely-ended rule).
  T5  close of a non-holder bridge → ``not_holder`` (no timer armed).
  T6  close of the holder's bridge → ``scheduled``; cancel_all() disarms.
  T7  succession NO-OPs: reconnected-inside-grace → ``reconnected``;
      superseded-by-another-claim → ``superseded``.
  T8  succession-at-end: newest-live provider-capable OTHER session claimed
      (``succeeded``), dead peer_binding rows never promoted (selection runs
      over live bridges only), non-capable sessions skipped.
  T9  zero live candidates → ``vacant`` (holder row left as-is — no-vacant-
      release invariant; the lane DEFERs via the flip until the next claim).
  T10 manual-set displaces a LIVE holder → both-party notices (§5.4);
      rejects a target with no live provider / no registration.
  T11 drain honesty: v1 hook drains nothing → rows RETAINED (no silent
      hard-delete); with a re-drive override the drained rows are
      HARD-deleted (unique flow_id slot freed) and the remainder counted.
  T12 drain wiring: invoked on vacancy-fill + crash-heal (dark lane re-lit),
      NOT on a live→live manual displacement, NOT on ``held``.

The ★ flip-assertion (vacancy→DEFER, never LOCAL) lives core-side in
``ananta/src/ananta/services/inference_service/tests/autonomic_flip_smoke.py``.

Run from repo root:
    SOLET_NAME=<name>-test .venv/bin/python3 \
        plugins/agent_messaging_plugin/tests/autonomic_assignment_smoke.py
"""

from __future__ import annotations

import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT / "ananta" / "src"))
sys.path.insert(0, str(REPO_ROOT / "plugins" / "agent_messaging_plugin" / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _real_state_fake import CapEnforcingState, RealShapeState  # noqa: E402
from ananta.llm.agent_messaging.role_binding import SYS_AUTONOMIC_SLOT  # noqa: E402
from ananta.services.inference_service.schema import (  # noqa: E402
    COL_FLOW_ID,
    INFERENCE_DEFERRED_VERTEX_NAMESPACE,
    TABLE_INFERENCE_DEFERRED_VERTEX,
)

from agent_messaging_plugin.autonomic_assignment import (  # noqa: E402
    AutonomicAssignment,
    select_newest_live,
)
from agent_messaging_plugin.models import BridgeBinding  # noqa: E402
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


class _Bridge:
    def __init__(self, bridge_id: str, created_at: str) -> None:
        self.bridge_id = bridge_id
        self.created_at = created_at


def _binding(
    bridge_id: str, agi: str, sid: str, agent_id: str = "claude_code",
    label: str = "",
) -> BridgeBinding:
    return BridgeBinding(
        bridge_id=bridge_id,
        agent_id=agent_id,
        agent_instance_id=agi,
        session_label=label or agi,
        parent_pid=None,
        agent_session_id=sid,
    )


class _Harness:
    """Mutable world the collaborators close over: bridges, bindings, providers."""

    def __init__(self) -> None:
        # The shared REAL-SHAPE fake grew ``write_state`` in slice-D — no
        # local INSERT subclass needed anymore.
        self.state = RealShapeState()
        self.bridges: list[_Bridge] = []
        self.bindings: dict[str, list[BridgeBinding]] = {}
        self.live_providers: set[str] = set()
        self.live_by_session: dict[str, BridgeBinding] = {}
        self.notices: list[tuple[str, str, str]] = []
        self._queue_ordinal = 0

    def add_session(
        self, bridge_id: str, agi: str, sid: str, created_at: str,
        *, provider: bool = True,
    ) -> BridgeBinding:
        bound = _binding(bridge_id, agi, sid)
        self.bridges.append(_Bridge(bridge_id, created_at))
        self.bindings[bridge_id] = [bound]
        if provider:
            self.live_providers.add(agi)
        if sid:
            self.live_by_session[sid] = bound
        return bound

    def drop_session(self, bridge_id: str) -> None:
        """Simulate close_bridge aftermath: bridge gone, provider cleared."""
        for bound in self.bindings.get(bridge_id, []):
            self.live_providers.discard(bound.agent_instance_id)
            self.live_by_session.pop(bound.agent_session_id, None)
        self.bridges = [b for b in self.bridges if b.bridge_id != bridge_id]
        self.bindings.pop(bridge_id, None)

    def assignment(self, *, grace_seconds: int = 30) -> AutonomicAssignment:
        return AutonomicAssignment(
            state_service=lambda: self.state,
            list_active_bridges=lambda: list(self.bridges),
            bindings_for_bridge=lambda bid: list(self.bindings.get(bid, [])),
            live_binding_for_session=self.live_by_session.get,
            has_live_provider=self.live_providers.__contains__,
            send_notice=self._notice,
            grace_seconds=grace_seconds,
            # INF-02 collaborators — inert here; the completion-handler
            # matrix lives in autonomic_completion_smoke.py.
            forward_completion=lambda _holder, _row: None,
            serve_window_seconds=900,
            # INF-06 collaborators — inert here (no re-drive primitive), which
            # preserves T11's "retain-without-primitive" case; the forwarded
            # sweep/drain/GC matrix lives in forward_vertex_reliability_smoke.py.
            resubmit_vertex=lambda _flow, _method: False,
            forward_serve_window_seconds=900,
            forward_attempts_cap=5,
            terminal_gc_after_seconds=172_800,
        )

    def _notice(
        self, *, peer_id: str, peer_agent_instance_id: str, prose: str, kind: str,
    ) -> bool:
        del prose
        self.notices.append((peer_id, peer_agent_instance_id, kind))
        return True

    def holder_agi(self) -> str | None:
        try:
            return resolve_role_binding_v4(
                cast("Any", self.state), SYS_AUTONOMIC_SLOT,
            ).agent_instance_id
        except RoleBindingVacantError:
            return None

    def queue_deferred(self, flow_id: str, *, state: str = "deferred") -> None:
        self._queue_ordinal += 1
        self.state.rows(
            INFERENCE_DEFERRED_VERTEX_NAMESPACE, TABLE_INFERENCE_DEFERRED_VERTEX,
        ).append({
            "role": SYS_AUTONOMIC_SLOT, "flow_id": flow_id,
            "method": "process_error", "agent_instance_id": None, "is_deleted": 0,
            "state": state, "id": f"dv-{self._queue_ordinal:06d}",
            "created_at": (datetime(2026, 8, 1, tzinfo=UTC) + timedelta(
                seconds=self._queue_ordinal,
            )).isoformat(),
        })

    def deferred_flow_ids(self) -> set[str]:
        return {
            str(r.get(COL_FLOW_ID)) for r in self.state.rows(
                INFERENCE_DEFERRED_VERTEX_NAMESPACE, TABLE_INFERENCE_DEFERRED_VERTEX,
            )
        }


class _DrainSpy(AutonomicAssignment):
    """Records drain invocations without running the real drain."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.drain_reasons: list[str] = []

    def _drain_deferred(self, reason: str) -> tuple[int, int]:
        self.drain_reasons.append(reason)
        return (0, 0)


def _register(
    assignment: AutonomicAssignment, bound: BridgeBinding, *, provides: bool = True,
) -> str:
    return assignment.on_register(
        agent_id=bound.agent_id,
        agent_instance_id=bound.agent_instance_id,
        agent_session_id=bound.agent_session_id,
        session_label=bound.session_label,
        provides_inference=provides,
    )


def _trigger1_fill_cases(world: _Harness, assignment: AutonomicAssignment) -> None:
    first = world.add_session("br-1", "agi-1", "sid-1", "2026-07-03T01:00:00")

    # T2 — a non-provider register never claims.
    _check(
        _register(assignment, first, provides=False) == "not_provider"
        and world.holder_agi() is None,
        "T2 non-provider register → not_provider (slot untouched)",
    )

    # T1 — vacancy-fill at boot.
    token = _register(assignment, first)
    _check(
        token == "vacancy_fill" and world.holder_agi() == "agi-1",
        "T1 vacancy-fill: first provider-capable register claims the slot",
    )
    _check(
        ("claude_code", "agi-1", "autonomic-new-holder") in world.notices
        and not any(k == "autonomic-displaced" for _, _, k in world.notices),
        "T1 new-holder notice fired; no displaced notice on a fresh claim",
    )

    # T3 — live-held slot: another register is a no-op.
    second = world.add_session("br-2", "agi-2", "sid-2", "2026-07-03T02:00:00")
    _check(
        _register(assignment, second) == "held" and world.holder_agi() == "agi-1",
        "T3 live holder → held (no displacement by a later register)",
    )


def _trigger1_crash_heal_case(
    world: _Harness, assignment: AutonomicAssignment,
) -> None:
    # T4 — crash-heal: holder dies without close — its bridge is popped
    # (sweep) and its provider is unreachable, but its registry row SURVIVES
    # (the stale-inclusive peer_binding reality). Next register claims, and
    # the stale row must NOT draw a displaced notice.
    world.notices.clear()
    world.live_providers.discard("agi-1")
    world.bridges = [b for b in world.bridges if b.bridge_id != "br-1"]
    third = world.add_session("br-3", "agi-3", "sid-3", "2026-07-03T03:00:00")
    _check(
        _register(assignment, third) == "crash_heal"
        and world.holder_agi() == "agi-3",
        "T4 crash-heal: dead holder replaced by the just-registered session",
    )
    _check(
        ("claude_code", "agi-3", "autonomic-new-holder") in world.notices
        and not any(k == "autonomic-displaced" for _, _, k in world.notices),
        "T4 genuinely-ended prior gets NO displaced notice (REL-04 rule)",
    )


def _trigger1_cases() -> None:
    print("Trigger-1 — vacancy-fill / crash-heal on register:")
    world = _Harness()
    assignment = world.assignment()
    _trigger1_fill_cases(world, assignment)
    _trigger1_crash_heal_case(world, assignment)


def _trigger2_cases() -> None:
    print("Trigger-2 — grace-delayed succession at end:")

    world = _Harness()
    assignment = world.assignment()
    holder = world.add_session("br-h", "agi-h", "sid-h", "2026-07-03T01:00:00")
    _register(assignment, holder)
    world.add_session("br-o", "agi-o", "sid-o", "2026-07-03T02:00:00")

    # T5 — closing a non-holder bridge arms nothing.
    _check(
        assignment.on_bridge_close("br-o") == "not_holder",
        "T5 non-holder bridge close → not_holder (no timer)",
    )

    # T6 — closing the holder's bridge arms the grace timer; cancel_all disarms.
    _check(
        assignment.on_bridge_close("br-h") == "scheduled",
        "T6 holder bridge close → succession scheduled",
    )
    assignment.cancel_all()

    # T7a — holder reconnected inside grace (provider live again) → NO-OP.
    departed = resolve_role_binding_v4(cast("Any", world.state), SYS_AUTONOMIC_SLOT)
    _check(
        assignment._succession_check(departed) == "reconnected"  # noqa: SLF001 — smoke drives the timer body
        and world.holder_agi() == "agi-h",
        "T7a reconnect inside grace → NO-OP (holder kept)",
    )

    # T7b — another session claimed during grace → superseded, no touch.
    world.drop_session("br-h")
    assignment.set_slot(agent_instance_id="agi-o")
    _check(
        assignment._succession_check(departed) == "superseded"  # noqa: SLF001 — smoke drives the timer body
        and world.holder_agi() == "agi-o",
        "T7b superseded during grace → NO-OP (new claim kept)",
    )

    # T8 — genuine end: newest-live provider-capable OTHER claims. The dead
    # holder's peer_binding-shaped leftovers are NOT selectable (selection
    # walks live bridges only), and a non-capable newest session is skipped.
    world2 = _Harness()
    assignment2 = world2.assignment()
    holder2 = world2.add_session("br-A", "agi-A", "sid-A", "2026-07-03T01:00:00")
    _register(assignment2, holder2)
    world2.add_session("br-B", "agi-B", "sid-B", "2026-07-03T02:00:00")
    world2.add_session(  # newest but NOT provider-capable → must be skipped
        "br-C", "agi-C", "sid-C", "2026-07-03T03:00:00", provider=False,
    )
    departed2 = resolve_role_binding_v4(cast("Any", world2.state), SYS_AUTONOMIC_SLOT)
    world2.drop_session("br-A")
    world2.notices.clear()
    _check(
        assignment2._succession_check(departed2) == "succeeded"  # noqa: SLF001 — smoke drives the timer body
        and world2.holder_agi() == "agi-B",
        "T8 succession: newest LIVE provider-capable other claims (agi-B)",
    )
    _check(
        ("claude_code", "agi-B", "autonomic-new-holder") in world2.notices
        and not any(k == "autonomic-displaced" for _, _, k in world2.notices),
        "T8 successor notified; departed (ended) holder is not",
    )

    # T9 — zero live candidates → vacant (row left as-is; lane DEFERs).
    world3 = _Harness()
    assignment3 = world3.assignment()
    holder3 = world3.add_session("br-X", "agi-X", "sid-X", "2026-07-03T01:00:00")
    _register(assignment3, holder3)
    departed3 = resolve_role_binding_v4(cast("Any", world3.state), SYS_AUTONOMIC_SLOT)
    world3.drop_session("br-X")
    _check(
        assignment3._succession_check(departed3) == "vacant"  # noqa: SLF001 — smoke drives the timer body
        and world3.holder_agi() == "agi-X",
        "T9 zero candidates → vacant token; binding left (no-vacant-release)",
    )


def _selection_cases() -> None:
    print("Selection — newest-live, provider-capable, dead rows never promoted:")

    live = [_Bridge("br-1", "2026-07-03T01:00:00"), _Bridge("br-2", "2026-07-03T02:00:00")]
    bindings = {
        "br-1": [_binding("br-1", "agi-1", "sid-1")],
        "br-2": [_binding("br-2", "agi-2", "sid-2")],
        # a stale registry row for a CLOSED bridge (newest created_at would
        # win if selection read peer_binding rows) — must never be reachable
        # because its bridge is not in the live list.
        "br-dead": [_binding("br-dead", "agi-dead", "sid-dead")],
    }
    chosen = select_newest_live(
        cast("Any", live),
        bindings_for_bridge=lambda bid: bindings.get(bid, []),
        has_live_provider=lambda agi: agi != "agi-1",
        exclude_agent_session_id="sid-x",
    )
    _check(
        chosen is not None and chosen.agent_instance_id == "agi-2",
        "S1 newest live provider-capable wins; dead-bridge rows unreachable",
    )
    chosen = select_newest_live(
        cast("Any", live),
        bindings_for_bridge=lambda bid: bindings.get(bid, []),
        has_live_provider=lambda _agi: True,
        exclude_agent_session_id="sid-2",
    )
    _check(
        chosen is not None and chosen.agent_instance_id == "agi-1",
        "S2 departed session excluded by stable sid (falls to next-newest)",
    )
    chosen = select_newest_live(
        cast("Any", live),
        bindings_for_bridge=lambda bid: bindings.get(bid, []),
        has_live_provider=lambda _agi: False,
    )
    _check(chosen is None, "S3 no provider-capable candidate → None (leave vacant)")


def _manual_set_cases() -> None:
    print("Manual-set — set_autonomic_slot core:")

    world = _Harness()
    assignment = world.assignment()
    holder = world.add_session("br-1", "agi-1", "sid-1", "2026-07-03T01:00:00")
    _register(assignment, holder)
    world.add_session("br-2", "agi-2", "sid-2", "2026-07-03T02:00:00")
    world.notices.clear()

    # T10 — displaces the LIVE holder with both-party notices (§5.4).
    outcome = assignment.set_slot(agent_instance_id="agi-2")
    _check(
        outcome.get("success") is True and outcome.get("action") == "displaced"
        and world.holder_agi() == "agi-2",
        "T10 manual-set displaces the live holder (action=displaced)",
    )
    _check(
        ("claude_code", "agi-1", "autonomic-displaced") in world.notices
        and ("claude_code", "agi-2", "autonomic-new-holder") in world.notices,
        "T10 both-party notices: displaced LIVE prior + new holder (§5.4)",
    )

    # T10b — fail-fast rejections.
    _check(
        assignment.set_slot(agent_instance_id="agi-ghost").get("code")
        == "no_live_provider",
        "T10b no live provider → no_live_provider (lane-strand guard)",
    )
    world.live_providers.add("agi-unbound")
    _check(
        assignment.set_slot(agent_instance_id="agi-unbound").get("code")
        == "not_registered",
        "T10c provider without a live registration → not_registered",
    )


def _drain_cases() -> None:
    print("Drain — first-claim honesty + wiring:")

    # T11 — v1 hook drains nothing: rows RETAINED (never silently deleted).
    world = _Harness()
    assignment = world.assignment()
    world.queue_deferred("flow-1")
    world.queue_deferred("flow-2")
    first = world.add_session("br-1", "agi-1", "sid-1", "2026-07-03T01:00:00")
    _register(assignment, first)
    _check(
        world.deferred_flow_ids() == {"flow-1", "flow-2"},
        "T11 v1 drain retains all rows (no re-drive primitive → no delete)",
    )

    # T11b — with a re-drive (SUB-05 lands via resubmit_vertex): drained rows
    # HARD-deleted. The drain re-drives through self.forwarded.resubmit_flow,
    # which delegates to the injected resubmit_vertex (here: always True).
    world2 = _Harness()
    redrive = AutonomicAssignment(
        state_service=lambda: world2.state,
        list_active_bridges=lambda: list(world2.bridges),
        bindings_for_bridge=lambda bid: list(world2.bindings.get(bid, [])),
        live_binding_for_session=world2.live_by_session.get,
        has_live_provider=world2.live_providers.__contains__,
        send_notice=world2._notice,  # noqa: SLF001 — harness wiring
        grace_seconds=30,
        forward_completion=lambda _holder, _row: None,
        serve_window_seconds=900,
        resubmit_vertex=lambda _flow, _method: True,
        forward_serve_window_seconds=900,
        forward_attempts_cap=5,
        terminal_gc_after_seconds=172_800,
    )
    world2.queue_deferred("flow-3")
    world2.queue_deferred("flow-4")
    drained, remaining = redrive._drain_deferred("smoke")  # noqa: SLF001 — drives the drain directly
    _check(
        (drained, remaining) == (2, 0) and world2.deferred_flow_ids() == set(),
        "T11b re-drive override → rows HARD-deleted (flow_id slots freed)",
    )

    # T12 — wiring: drain fires on vacancy-fill + crash-heal, not on held /
    # live→live manual displacement.
    world3 = _Harness()
    spy = _DrainSpy(
        state_service=lambda: world3.state,
        list_active_bridges=lambda: list(world3.bridges),
        bindings_for_bridge=lambda bid: list(world3.bindings.get(bid, [])),
        live_binding_for_session=world3.live_by_session.get,
        has_live_provider=world3.live_providers.__contains__,
        send_notice=world3._notice,  # noqa: SLF001 — harness wiring
        grace_seconds=30,
        forward_completion=lambda _holder, _row: None,
        serve_window_seconds=900,
        resubmit_vertex=lambda _flow, _method: False,
        forward_serve_window_seconds=900,
        forward_attempts_cap=5,
        terminal_gc_after_seconds=172_800,
    )
    a = world3.add_session("br-1", "agi-1", "sid-1", "2026-07-03T01:00:00")
    _register(spy, a)  # vacancy_fill → drain
    b = world3.add_session("br-2", "agi-2", "sid-2", "2026-07-03T02:00:00")
    _register(spy, b)  # held → no drain
    spy.set_slot(agent_instance_id="agi-2")  # live→live displace → no drain
    world3.live_providers.discard("agi-2")
    c = world3.add_session("br-3", "agi-3", "sid-3", "2026-07-03T03:00:00")
    _register(spy, c)  # crash_heal → drain
    _check(
        spy.drain_reasons == ["vacancy_fill", "crash_heal"],
        "T12 drain wired to dark-lane re-lights only (fill + crash-heal)",
    )


def _paged_drain_case() -> None:
    """A first-claim redrive snapshots every matching row before mutation."""

    class _NoQueueQueryState(CapEnforcingState):
        def query_state(self, namespace: str, query: dict[str, Any]) -> dict[str, Any]:
            if query.get("table") == TABLE_INFERENCE_DEFERRED_VERTEX:
                raise AssertionError("autonomic drain must page the queue, never query_state it")
            return super().query_state(namespace, query)

    world = _Harness()
    world.state = _NoQueueQueryState(world.state)
    for index in range(210):
        state = "deferred" if index % 2 == 0 else "forwarded"
        world.queue_deferred(f"flow-redrive-{index:03d}", state=state)
    for index in range(101):
        world.queue_deferred(f"flow-failed-{index:03d}", state="failed")
    resubmitted: list[str] = []
    assignment = AutonomicAssignment(
        state_service=lambda: world.state,
        list_active_bridges=lambda: [],
        bindings_for_bridge=lambda _bridge_id: [],
        live_binding_for_session=lambda _session_id: None,
        has_live_provider=lambda _agent_instance_id: False,
        send_notice=world._notice,
        grace_seconds=30,
        forward_completion=lambda _holder, _row: None,
        serve_window_seconds=900,
        resubmit_vertex=lambda flow_id, _row: (resubmitted.append(flow_id) or True),
        forward_serve_window_seconds=900,
        forward_attempts_cap=5,
        terminal_gc_after_seconds=172_800,
    )
    drained, remaining = assignment._drain_deferred("paged-smoke")  # noqa: SLF001
    surviving = world.state.rows(
        INFERENCE_DEFERRED_VERTEX_NAMESPACE, TABLE_INFERENCE_DEFERRED_VERTEX,
    )
    _check(
        drained == 210 and remaining == 0 and len(resubmitted) == len(set(resubmitted)) == 210,
        "autonomic pages >200 deferred/forwarded rows and resubmits each exactly once",
    )
    _check(
        len(surviving) == 101 and all(row["state"] == "failed" for row in surviving),
        "terminal failed rows are provider-filtered out and remain untouched",
    )


def main() -> int:
    print("=== sys:autonomic auto-assignment lifecycle smoke ===")
    _trigger1_cases()
    _trigger2_cases()
    _selection_cases()
    _manual_set_cases()
    _drain_cases()
    _paged_drain_case()
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
