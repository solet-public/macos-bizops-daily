#!/usr/bin/env python3
"""A4 slice 2 — FLEET_TRANSPORT watch-transport parity proof (no new code).

Architect memo (workbench/2026-08-04_marker_retirement_architect_memo_architect.md),
Amendment 6: "the moment the gate at peer_dispatch.py is deleted, every
delivery flows through the same append_event -> the same
_stream_events/_drain_inbox -> the same spool tee, with zero changes needed
in local_cli/cli.py or wake.py." That claim is proved here end-to-end against
REAL production collaborators (BridgeSessionManager, PeerRegistry, the actual
``/events`` FastAPI route, ``dispatch_peer_send``, ``_emit_line``) rather than
by code inspection alone -- a different transport's consumer, exercised with
its own smoke, riding slice 1's code unchanged.

RED-FIRST shape (memo, slice 2): pre-slice-1 the same call chain asserts the
spool stays empty (a markerless send never reached ``append_event``);
post-slice-1 it asserts a line lands. Verified by hand at authoring time by
temporarily reverting peer_dispatch.py's gate deletion and re-running this
file -- both directions are recorded in the slice's land report, not
re-derived by this smoke on every run (there is no repo mechanism to flip the
production code mid-process safely, so the mutation-proof is a one-time,
documented check like the other slices' hand-verified red-first legs).

Run:
    .venv/bin/python3 plugins/agent_messaging_plugin/tests/watch_transport_parity_smoke.py
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path
from typing import Any, cast

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT / "ananta" / "src"))
sys.path.insert(0, str(REPO_ROOT / "plugins" / "agent_messaging_plugin" / "src"))

from _real_state_fake import RealShapeState  # noqa: E402
from ananta.llm.agent_messaging.models import TextPart  # noqa: E402
from ananta.services.store import open_store  # noqa: E402
from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from agent_messaging_plugin.bridge_sessions import BridgeSessionManager  # noqa: E402
from agent_messaging_plugin.http_routes import register_routes  # noqa: E402
from agent_messaging_plugin.local_cli.cli import _emit_line  # noqa: E402
from agent_messaging_plugin.models import (  # noqa: E402
    WATCH_AGENT_INSTANCE_PREFIX,
    BridgeBinding,
)
from agent_messaging_plugin.peer_dispatch import dispatch_peer_send  # noqa: E402
from agent_messaging_plugin.peer_registry import PeerRegistry  # noqa: E402
from agent_messaging_plugin.schema import (  # noqa: E402
    PEER_BINDING_NAMESPACE,
    get_peer_binding_schema,
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


class _FakeMessagingService:
    """Minimal peer_send stand-in.

    dispatch_peer_send's own logic (not this fake) is what is under test --
    this exists only because dispatch_peer_send requires an
    agent_messaging_service collaborator to persist the envelope, and that
    persistence is orthogonal to whether the QUEUED event reaches the /events
    route and the watch spool.
    """

    def peer_send(self, request: Any) -> Any:  # noqa: ANN401, ARG002
        class _Result:
            thread_id = "agt-parity"
            message_id = "agm-parity"
            cursor = 1

        return _Result()


def _registry() -> PeerRegistry:
    store = open_store(
        get_peer_binding_schema(),
        namespace=PEER_BINDING_NAMESPACE,
        backend="in_memory",
    )
    return PeerRegistry(bindings_store=store)


def _routes_client(manager: BridgeSessionManager, registry: PeerRegistry) -> TestClient:
    app = FastAPI()
    register_routes(
        app,
        bridge_manager=manager,
        peer_registry=registry,
        platform_surface=cast("Any", object()),
        agent_messaging_service=_FakeMessagingService(),
        config={"long_poll_timeout_seconds": 1},
        state_service=cast("Any", object()),
    )
    return TestClient(app)


def test_markerless_send_reaches_the_watch_spool() -> None:
    manager = BridgeSessionManager(
        session_id_factory=lambda _n: "ags-parity",
        idle_timeout_s=3600,
        max_pending_events=20,
        long_poll_timeout_s=1,
    )
    registry = _registry()
    http = _routes_client(manager, registry)

    # The watch client's bridge: opened directly on the real manager (the
    # same primitive the real /bridge/open route calls), then polled over
    # the REAL ASGI-mounted /events route via TestClient -- no mocked HTTP
    # layer, the same pattern watcher_ack_consumption_smoke.py already
    # exercises for this exact route.
    watcher_bridge_id = manager.open(solet_name="", parent_pid=None).bridge_id
    watcher_instance_id = f"{WATCH_AGENT_INSTANCE_PREFIX}parity00000000000000000"
    registry.register(
        BridgeBinding(
            bridge_id=watcher_bridge_id,
            agent_id="claude_code",
            agent_instance_id=watcher_instance_id,
            session_label="Watch-Parity",
            parent_pid=None,
        ),
    )

    # The send: REAL dispatch_peer_send, no marker text at all -- this is the
    # exact call slice 1 changed. sender_bridge_id is never opened (mirrors
    # direct_wake_outbox_smoke.py's pattern); dispatch_peer_send tolerates an
    # absent sender bridge (a guarded .get(), not a hard requirement).
    dispatch_peer_send(
        bridge_manager=manager,
        peer_registry=registry,
        agent_messaging_service=_FakeMessagingService(),
        state_service=cast("Any", RealShapeState()),
        sender_bridge_id="agc-sender",
        sender_agent_id="claude_code",
        sender_agent_instance_id="agi-sender",
        sender_session_label="Sender",
        sender_parent_pid=None,
        peer_id="claude_code",
        peer_agent_instance_id=watcher_instance_id,
        content=[TextPart(type="text", text="fyi, no marker")],
    )

    # The watch client's real poll — this is precisely _stream_events'
    # per-iteration `client.events(after=cursor)` call (BridgeClient.events
    # is a thin GET wrapper over this exact route; driving the route
    # directly here proves the server half without needing an
    # async-incompatible transport shim for the sync BridgeClient).
    payload = http.get(f"/api/v1/bridge/{watcher_bridge_id}/events?after=-1").json()
    events = payload.get("events", [])
    _check(
        len(events) == 1,
        "A4 slice 2: a markerless send is retrievable over the REAL /events "
        f"HTTP route (got {len(events)} event(s))",
    )
    _check(
        bool(events) and events[0].get("content") == "fyi, no marker",
        "the queued event carries the markerless prose verbatim (no gate "
        "stripped or blocked it)",
    )

    # The spool tee — _stream_events' per-event body, called directly (the
    # function itself is an infinite long-poll loop, not unit-callable; this
    # is the exact composition it performs per event, unchanged by slice 1).
    tmp = Path(tempfile.mkdtemp())
    spool_path = tmp / "watch-parity.spool"
    for event in events:
        _emit_line({"watch": "event", "event": event}, spool_path)

    _check(spool_path.is_file(), "the spool file was created (a line was teed)")
    spooled = spool_path.read_text(encoding="utf-8") if spool_path.is_file() else ""
    _check(
        "fyi, no marker" in spooled,
        "the markerless delivery's prose reached the watch spool file "
        f"(spool content: {spooled!r})",
    )


def main() -> int:
    print("=== A4 slice 2 — watch-transport parity (real /events + spool) ===")
    test_markerless_send_reaches_the_watch_spool()
    print(f"\n{_passed} passed, {len(_failed)} failed")
    if _failed:
        for label in _failed:
            print(f"  FAILED: {label}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
