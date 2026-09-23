#!/usr/bin/env python3
"""INF-02 autonomic completion handlers smoke (no pytest).

Drives the agent_messaging half of the completion request/response queue
offline — the per-type drain handlers on ``AutonomicAssignment`` (same
MECHANISM as the deferred-vertex drain, its OWN table, per the Reviewer-A
rider), the FAILED-transition triggers, the serve verb, and the forwarder
event:

  C1  claim-time reconcile: unassigned rows stamp + forward to the new
      holder; rows stamped to a PRIOR holder re-queue (holder-death) and
      re-forward.
  C2  a forward fault returns the row to the unassigned backlog (durable,
      never lost) and the reconcile survives (claim never fails on
      completion policy).
  C3  serve-timeout sweep: a stamped row past the serve window re-queues +
      re-forwards to the live holder; a fresh row is untouched; with NO
      live holder the re-queued row waits unassigned.
  C4  serve verb: CAS win → success + the resume continuation action_def
      (correct process triple, request_id-only platform args, correlation
      context_id, NO result_processor); repeat → ``already_served``
      failure; unknown id → ``unknown_request``; empty args →
      ``missing_argument``.
  C5  ``_build_resume_action`` rejects a malformed resume_process_key
      (typed).
  C6  ``SessionInferenceProvider.forward_completion_request`` emits the
      typed ``inference_completion_request`` bridge event with the
      self-contained payload (request_id/purpose/messages/correlation +
      serve_process_key).
  C7  the bridge-lifecycle sweeper's on_tick rider runs each tick and a
      rider fault never kills the reaper.

Offline: the shared REAL-SHAPE state fake (schema-enforced for the NEW
table — the slice-D phantom-column class), in-memory collaborators. No
live solet / Postgres.

Run from repo root:
    SOLET_NAME=<name>-test .venv/bin/python3 \
        plugins/agent_messaging_plugin/tests/autonomic_completion_smoke.py
"""

from __future__ import annotations

import json
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT / "ananta" / "src"))
sys.path.insert(0, str(REPO_ROOT / "plugins" / "agent_messaging_plugin" / "src"))
sys.path.insert(0, str(REPO_ROOT / "plugins" / "agent_messaging_plugin" / "tests"))

from _real_state_fake import (  # noqa: E402
    _STANDARDIZER_COLUMNS,
    RealShapeState,
)
from ananta.error_handling import FrameworkError  # noqa: E402
from ananta.services.inference_service.completion_request_queue import (  # noqa: E402
    insert_completion_request,
    pending_stamped_requests,
    pending_unassigned_requests,
    stamp_for_forward,
)
from ananta.services.inference_service.completion_request_schema import (  # noqa: E402
    COL_ATTEMPTS,
    COL_FORWARDED_AT,
    COL_HOLDER_AGENT_INSTANCE_ID,
    COL_REQUEST_ID,
    TABLE_INFERENCE_COMPLETION_REQUEST,
    get_inference_completion_request_schema,
)

from agent_messaging_plugin.autonomic_assignment import AutonomicAssignment  # noqa: E402
from agent_messaging_plugin.bridge_lifecycle import BridgeLifecycleSweeper  # noqa: E402
from agent_messaging_plugin.plugin import (  # noqa: E402
    AgentMessagingPlugin,
    _build_resume_action,
)
from agent_messaging_plugin.session_inference_provider import (  # noqa: E402
    SERVE_COMPLETION_PROCESS_KEY,
    SessionInferenceProvider,
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


RESUME_KEY = "service_interface::thinking_service::resume_thinking_completion"
CORRELATION = {"context_id": "ctx-1", "playbook_id": "pb-1"}
MESSAGES = [{"role": "user", "content": "plan the work"}]


def _store() -> RealShapeState:
    fake = RealShapeState()
    declared = frozenset(
        get_inference_completion_request_schema()
        .tables[TABLE_INFERENCE_COMPLETION_REQUEST]
        .columns,
    )
    fake._enforced_columns[TABLE_INFERENCE_COMPLETION_REQUEST] = (  # noqa: SLF001 — harness wiring
        declared | _STANDARDIZER_COLUMNS
    )
    return fake


def _enqueue(store: RealShapeState) -> str:
    return insert_completion_request(
        store,
        purpose="playbook_planning",
        resume_process_key=RESUME_KEY,
        correlation=CORRELATION,
        messages=MESSAGES,
    )


class _World:
    """Minimal collaborator set for the completion handlers."""

    def __init__(self) -> None:
        self.state = _store()
        self.forwards: list[tuple[str, str]] = []  # (holder, request_id)
        self.fail_forwards = False
        self.live_holder: Any = None  # ResolvedRole | None override

    def forward(self, holder: str, row: dict[str, object]) -> None:
        if self.fail_forwards:
            raise RuntimeError("bridge append raced disconnect")
        self.forwards.append((holder, str(row.get(COL_REQUEST_ID) or "")))

    def assignment(self, *, serve_window_seconds: int = 600) -> AutonomicAssignment:
        world = self

        class _SlotStubbed(AutonomicAssignment):
            """Slot resolution stubbed — the sweep's holder comes from the world."""

            def _resolve_slot(self) -> Any:
                return world.live_holder

            def _holder_is_live(self, holder: Any) -> bool:
                del holder
                return world.live_holder is not None

        return _SlotStubbed(
            state_service=lambda: self.state,
            list_active_bridges=list,
            bindings_for_bridge=lambda _bid: [],
            live_binding_for_session=lambda _sid: None,
            has_live_provider=lambda _agi: True,
            send_notice=lambda **_kw: True,
            grace_seconds=30,
            forward_completion=self.forward,
            serve_window_seconds=serve_window_seconds,
            # INF-06 collaborators — inert here; the forwarded sweep/drain/GC
            # matrix lives in forward_vertex_redrive_smoke.py.
            resubmit_vertex=lambda _flow, _method: False,
            forward_serve_window_seconds=900,
            forward_attempts_cap=5,
            terminal_gc_after_seconds=172_800,
        )


class _SessionHolder:
    """Duck-typed ResolvedRole stand-in for the sweep's holder resolution."""

    holder_kind = "session"

    def __init__(self, agent_instance_id: str) -> None:
        self.agent_instance_id = agent_instance_id
        self.agent_session_id = ""


class _FakeBridgeManager:
    """Records appended bridge events (the forwarder's emission surface)."""

    def __init__(self) -> None:
        self.events: list[tuple[str, str, str, dict[str, object]]] = []

    def append_event(
        self, bridge_id: str, event_type: str, content: str, meta: dict[str, object],
    ) -> None:
        self.events.append((bridge_id, event_type, content, meta))


def _serve_plugin(store: RealShapeState) -> Any:
    import logging

    plugin: Any = AgentMessagingPlugin.__new__(AgentMessagingPlugin)
    plugin._get_state_service = lambda: store  # noqa: SLF001 — harness wiring
    plugin.logger = logging.getLogger("autonomic_completion_smoke")
    return plugin


def _case_1() -> None:
    print("\n[C1] claim-time reconcile: forward unassigned + requeue prior holder's")
    world = _World()
    rid_unassigned = _enqueue(world.state)
    rid_stamped = _enqueue(world.state)
    stamp_for_forward(
        world.state, request_id=rid_stamped, holder_agent_instance_id="agi-old",
    )
    assignment = world.assignment()
    assignment.completions.reconcile("smoke", new_holder="agi-new")
    _check(
        {rid for _, rid in world.forwards} == {rid_unassigned, rid_stamped},
        "C1 both rows forwarded to the new holder",
    )
    stamped_now = pending_stamped_requests(world.state)
    _check(
        len(stamped_now) == 2
        and all(
            r[COL_HOLDER_AGENT_INSTANCE_ID] == "agi-new" for r in stamped_now
        ),
        "C1 both rows now stamped to the new holder",
    )
    requeued_row = next(
        r for r in stamped_now if r[COL_REQUEST_ID] == rid_stamped
    )
    _check(
        requeued_row[COL_ATTEMPTS] == 1,
        "C1 the prior holder's in-flight row carries the requeue attempt",
    )
    world.forwards.clear()
    assignment.completions.reconcile("smoke", new_holder="agi-new")
    _check(
        not world.forwards,
        "C1 idempotent: a same-holder reconcile re-forwards nothing",
    )


def _case_2() -> None:
    print("\n[C2] forward fault → durable unassigned backlog, reconcile survives")
    world = _World()
    _enqueue(world.state)
    world.fail_forwards = True
    assignment = world.assignment()
    assignment.completions.reconcile("smoke", new_holder="agi-new")
    _check(
        len(pending_unassigned_requests(world.state)) == 1
        and not pending_stamped_requests(world.state),
        "C2 faulted forward returns the row to the unassigned backlog",
    )


def _case_3() -> None:
    print("\n[C3] serve-timeout sweep")
    world = _World()
    rid_stale = _enqueue(world.state)
    rid_fresh = _enqueue(world.state)
    stamp_for_forward(
        world.state, request_id=rid_stale, holder_agent_instance_id="agi-quiet",
    )
    stamp_for_forward(
        world.state, request_id=rid_fresh, holder_agent_instance_id="agi-quiet",
    )
    # Age ONLY the stale row's forward stamp past the serve window.
    for row in world.state.rows("core", TABLE_INFERENCE_COMPLETION_REQUEST):
        if row[COL_REQUEST_ID] == rid_stale:
            row[COL_FORWARDED_AT] = (
                datetime.now(UTC) - timedelta(seconds=3_600)
            ).isoformat()
    world.live_holder = _SessionHolder("agi-live")
    assignment = world.assignment(serve_window_seconds=600)
    requeued, forwarded = assignment.completions.sweep_serve_timeouts()
    _check(
        (requeued, forwarded) == (1, 1)
        and world.forwards == [("agi-live", rid_stale)],
        "C3 stale row re-queued + re-forwarded to the live holder",
    )
    fresh_row = next(
        r for r in pending_stamped_requests(world.state)
        if r[COL_REQUEST_ID] == rid_fresh
    )
    _check(
        fresh_row[COL_HOLDER_AGENT_INSTANCE_ID] == "agi-quiet"
        and fresh_row[COL_ATTEMPTS] == 0,
        "C3 fresh in-flight row untouched by the sweep",
    )
    world2 = _World()
    rid_orphan = _enqueue(world2.state)
    stamp_for_forward(
        world2.state, request_id=rid_orphan, holder_agent_instance_id="agi-gone",
    )
    for row in world2.state.rows("core", TABLE_INFERENCE_COMPLETION_REQUEST):
        row[COL_FORWARDED_AT] = (
            datetime.now(UTC) - timedelta(seconds=3_600)
        ).isoformat()
    assignment2 = world2.assignment(serve_window_seconds=600)
    requeued, forwarded = assignment2.completions.sweep_serve_timeouts()
    _check(
        (requeued, forwarded) == (1, 0)
        and len(pending_unassigned_requests(world2.state)) == 1,
        "C3 with no live holder the re-queued row waits unassigned (durable)",
    )


def _case_4() -> None:
    print("\n[C4] serve verb: CAS win + resume continuation; typed rejections")
    store = _store()
    request_id = insert_completion_request(
        store,
        purpose="playbook_planning",
        resume_process_key=RESUME_KEY,
        correlation=CORRELATION,
        messages=MESSAGES,
    )
    plugin = _serve_plugin(store)
    result = plugin.submit_autonomic_completion(
        {"request_id": request_id, "text": "the served plan"}, {},
    )
    _check(
        result["action_status"] == "completed"
        and result["data"]["status"] == "served",
        "C4 first serve wins and completes",
    )
    actions = result["actions"]
    _check(len(actions) == 1, "C4 exactly one resume continuation returned")
    resume = actions[0]
    _check(
        resume["process"]
        == {
            "provider_type": "service_interface",
            "provider": "thinking_service",
            "function_name": "resume_thinking_completion",
        },
        "C4 resume targets the row's resume_process_key",
    )
    _check(
        resume["arguments"] == {"request_id": request_id}
        and resume.get("context_id") == "ctx-1"
        and "result_processor" not in resume,
        "C4 platform-owned args: request_id only + correlation context_id, "
        "no result_processor",
    )
    repeat = plugin.submit_autonomic_completion(
        {"request_id": request_id, "text": "different text"}, {},
    )
    _check(
        repeat["action_status"] == "failed"
        and repeat["error"]["code"] == "already_served",
        "C4 repeat serve → already_served failure, no second resume",
    )
    unknown = plugin.submit_autonomic_completion(
        {"request_id": "icr-nope", "text": "x"}, {},
    )
    _check(
        unknown["error"]["code"] == "unknown_request",
        "C4 unknown request id → typed rejection",
    )
    missing = plugin.submit_autonomic_completion({"request_id": "", "text": ""}, {})
    _check(
        missing["error"]["code"] == "missing_argument",
        "C4 empty args → missing_argument",
    )


def _case_5() -> None:
    print("\n[C5] malformed resume_process_key is a typed rejection")
    try:
        _build_resume_action(
            {COL_REQUEST_ID: "icr-x", "resume_process_key": "not-a-process-key"},
        )
        _check(False, "C5 malformed resume key raises FrameworkError")
    except FrameworkError:
        _check(True, "C5 malformed resume key raises FrameworkError")


def _case_6() -> None:
    print("\n[C6] forwarder emits the typed bridge event")
    manager = _FakeBridgeManager()
    provider = SessionInferenceProvider(
        bridge_id="br-1",
        agent_instance_id="agi-holder",
        agent_id="claude_code",
        session_label="Holder",
        bridge_manager=cast("Any", manager),
    )
    provider.forward_completion_request(
        request_id="icr-evt",
        purpose="playbook_planning",
        messages=MESSAGES,
        correlation=CORRELATION,
    )
    _check(len(manager.events) == 1, "C6 exactly one event appended")
    bridge_id, event_type, content, meta = manager.events[0]
    payload = json.loads(content)
    _check(
        bridge_id == "br-1"
        and event_type == "inference_completion_request"
        and meta.get("event_type") == "inference_completion_request",
        "C6 typed inference_completion_request event on the holder's bridge",
    )
    _check(
        payload
        == {
            "request_id": "icr-evt",
            "purpose": "playbook_planning",
            "messages": MESSAGES,
            "correlation": CORRELATION,
            "serve_process_key": SERVE_COMPLETION_PROCESS_KEY,
        },
        "C6 self-contained payload incl. the serve verb's process key",
    )


def _case_7() -> None:
    print("\n[C7] sweeper on_tick rider runs per tick and faults never kill it")
    class _IdleManager:
        def sweep_idle(self) -> list[str]:
            return []

    ticks: list[int] = []

    def _rider() -> None:
        ticks.append(1)

    sweeper = BridgeLifecycleSweeper(
        bridge_manager=_IdleManager(),
        cleanup=lambda _bid: 0,
        interval_seconds=3_600,
        on_tick=_rider,
    )
    sweeper.sweep_once()
    _check(ticks == [1], "C7 on_tick rider invoked by the sweep tick")

    def _faulting_rider() -> None:
        raise RuntimeError("rider fault")

    faulty = BridgeLifecycleSweeper(
        bridge_manager=_IdleManager(),
        cleanup=lambda _bid: 0,
        interval_seconds=3_600,
        on_tick=_faulting_rider,
    )
    try:
        faulty.sweep_once()
        _check(True, "C7 a rider fault is contained (sweep completes)")
    except Exception:  # noqa: BLE001 — the assertion IS that nothing propagates
        _check(False, "C7 a rider fault is contained (sweep completes)")


def main() -> int:
    print("INF-02 autonomic completion handlers smoke")
    _case_1()
    _case_2()
    _case_3()
    _case_4()
    _case_5()
    _case_6()
    _case_7()

    print(f"\n{_passed} passed, {len(_failed)} failed")
    if _failed:
        for label in _failed:
            print(f"  FAILED: {label}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
