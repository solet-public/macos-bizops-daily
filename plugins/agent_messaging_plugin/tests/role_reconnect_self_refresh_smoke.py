#!/usr/bin/env python3
"""Acceptance smoke for the S1–S3 agent_session_id splice (no pytest, no DB).

Closes a CONFIRMED live defect: role-addressed delivery resolves the holder ONLY
from the ``agent_role_binding`` state table, but on bridge reconnect the
``agent_instance_id`` rotates and NOTHING re-pointed that table — ``peer_register``
refreshed only the vestigial address-book entry. So a reconnected durable-role
holder stranded its own role wakes + backlog until an explicit re-claim.

These drive the REAL ``/peer/register`` route END-TO-END (FastAPI TestClient +
in-memory state) — a green here means the ROUTE re-points the state table on
reconnect, which is exactly the fix. RED before S1–S3 (the route called the AB
refresh, never the state CAS); GREEN after.

Run:
    SOLET_NAME=<name>-test .venv/bin/python3 \
        plugins/agent_messaging_plugin/tests/role_reconnect_self_refresh_smoke.py
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT / "ananta" / "src"))
sys.path.insert(0, str(REPO_ROOT / "plugins" / "agent_messaging_plugin" / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

if TYPE_CHECKING:
    from ananta.interfaces.state_management_interface import StateManagementInterface

from _real_state_fake import RealShapeState  # noqa: E402
from ananta.llm.agent_messaging.models import TextPart  # noqa: E402
from ananta.llm.agent_messaging.role_binding import HOLDER_KIND_SESSION  # noqa: E402
from ananta.llm.agent_messaging.service import AgentMessagingService  # noqa: E402
from ananta.services.store import Store, open_store  # noqa: E402
from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from agent_messaging_plugin.bridge_sessions import BridgeSessionManager  # noqa: E402
from agent_messaging_plugin.http_routes import register_routes  # noqa: E402
from agent_messaging_plugin.models import BridgeBinding  # noqa: E402
from agent_messaging_plugin.peer_registry import PeerRegistry  # noqa: E402
from agent_messaging_plugin.role_binding_store import (  # noqa: E402
    HolderClaim,
    claim_role_binding_v4,
    resolve_role_binding,
)
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
        return
    _failed.append(label)
    print(f"  FAIL  {label}")


def _state() -> StateManagementInterface:
    return cast("StateManagementInterface", RealShapeState())


def _fresh_peer_registry() -> PeerRegistry:
    store: Store = open_store(
        get_peer_binding_schema(), namespace=PEER_BINDING_NAMESPACE, backend="in_memory",
    )
    return PeerRegistry(bindings_store=store)


def _bridge_manager() -> BridgeSessionManager:
    return BridgeSessionManager(
        session_id_factory=lambda _name: "ags-http",
        idle_timeout_s=3600,
        max_pending_events=20,
        long_poll_timeout_s=1,
    )


class _EnabledConfig:
    """Minimal config stub — the role backlog methods only read ``enabled``."""

    enabled = True
    allowed_backends: tuple[str, ...] = ()


class _NoOwedDirectWakeService:
    """Minimal route stub; these smokes assert role self-refresh, not delivery.

    A4 (2026-08-04): used to carry rehome_owed_direct_wakes/rehome_owed_role_wakes
    because the /peer/register route called both on every register; both calls
    retired with the escalation/consumption-reconcile apparatus they served, so
    this fake is now a pure placeholder for the ``agent_messaging_service``
    param the route still requires.
    """


def _service(state: StateManagementInterface) -> AgentMessagingService:
    return AgentMessagingService(
        repository=cast("Any", object()),
        state_service=state,
        config=cast("Any", _EnabledConfig()),
    )


def _client(
    manager: BridgeSessionManager, registry: PeerRegistry, state: StateManagementInterface,
) -> TestClient:
    app = FastAPI()
    register_routes(
        app,
        bridge_manager=manager,
        peer_registry=registry,
        platform_surface=cast("Any", object()),
        agent_messaging_service=cast("Any", _NoOwedDirectWakeService()),
        config={"long_poll_timeout_seconds": 1},
        state_service=state,
    )
    return TestClient(app)


def _claim(
    state: StateManagementInterface, *, role: str, agi: str, session_id: str,
) -> None:
    # §9 CUTOVER: seed the v4 role_binding table (the live claim path post-cutover).
    claim_role_binding_v4(
        state,
        name=role,
        claim=HolderClaim(
            holder_kind=HOLDER_KIND_SESSION,
            holder_identity={"agent_id": "claude_code", "session_label": role},
            agent_instance_id=agi,
            agent_session_id=session_id,
            session_label=role,
        ),
    )


def _register(
    client: TestClient,
    bridge_id: str,
    *,
    agent_instance_id: str,
    agent_session_id: str,
    session_role: str = "",
) -> Any:
    body: dict[str, Any] = {
        "agent_id": "claude_code",
        "agent_instance_id": agent_instance_id,
        "agent_session_id": agent_session_id,
        "session_label": "session-label",
    }
    if session_role:
        body["session_role"] = session_role
    return client.post(
        f"/api/v1/bridge/{bridge_id}/peer/register",
        json=body,
    )


def _open_bridge(manager: BridgeSessionManager) -> str:
    return manager.open(solet_name="", parent_pid=123).bridge_id


def test_register_response_carries_no_rehome_tokens() -> None:
    """A4 (2026-08-04): role_rehome/direct_rehome retired from the register
    response — they reported the (now-gone) escalation/consumption-reconcile
    re-home apparatus. Positive assertion that they are truly absent, not
    just unread, so a stray re-add doesn't slip back in silently."""
    manager, registry, state = _bridge_manager(), _fresh_peer_registry(), _state()
    client = _client(manager, registry, state)
    response = _register(
        client,
        _open_bridge(manager),
        agent_instance_id="agi-rehome-token",
        agent_session_id="sess-rehome-token",
    )
    payload = response.json()
    _check(
        "role_rehome" not in payload and "direct_rehome" not in payload,
        f"register response carries no rehome tokens (got keys: {sorted(payload)})",
    )


def test_reconnect_repoints_resolution() -> None:
    manager, registry, state = _bridge_manager(), _fresh_peer_registry(), _state()
    _claim(state, role="R", agi="agi-X", session_id="sess-1")
    client = _client(manager, registry, state)
    bridge_id = _open_bridge(manager)
    resp = _register(client, bridge_id, agent_instance_id="agi-Y", agent_session_id="sess-1")
    _check(resp.status_code == 200, "reconnect register returns 200")
    _check(resp.json().get("self_refresh") == "rerouted:1", "self_refresh token = rerouted:1")
    _check(
        resolve_role_binding(state, "R").agent_instance_id == "agi-Y",
        "reconnect RE-POINTS role R to the rotated instance (agi-Y) in the state table",
    )


def test_backlog_redelivers_across_strand() -> None:
    manager, registry, state = _bridge_manager(), _fresh_peer_registry(), _state()
    _claim(state, role="R", agi="agi-X", session_id="sess-1")
    service = _service(state)
    service.persist_role_message(
        recipient_kind="role",
        recipient_key="R",
        message_id="arm-backlog",
        sender_agent_id="example",
        sender_agent_instance_id="example",
        sender_session_label="Example",
        important=True,
        content=[TextPart(type="text", text="IMPORTANT: backlog")],
    )
    client = _client(manager, registry, state)
    _register(client, _open_bridge(manager), agent_instance_id="agi-Y", agent_session_id="sess-1")
    owed = service.list_undelivered_for_instance(agent_instance_id="agi-Y", limit=50)
    _check(
        len(owed) == 1 and owed[0].get("recipient_key") == "R",
        "backlog for R re-delivers to the reconnected holder (agi-Y) after the re-point",
    )


def test_multi_role_single_cas() -> None:
    manager, registry, state = _bridge_manager(), _fresh_peer_registry(), _state()
    _claim(state, role="R1", agi="agi-X", session_id="sess-1")
    _claim(state, role="R2", agi="agi-X", session_id="sess-1")
    client = _client(manager, registry, state)
    resp = _register(
        client,
        _open_bridge(manager),
        agent_instance_id="agi-Y",
        agent_session_id="sess-1",
    )
    _check(
        resp.json().get("self_refresh") == "rerouted:2",
        "single register re-points BOTH roles (rerouted:2)",
    )
    _check(
        resolve_role_binding(state, "R1").agent_instance_id == "agi-Y"
        and resolve_role_binding(state, "R2").agent_instance_id == "agi-Y",
        "one session-id CAS re-points ALL held roles (R1 + R2) to agi-Y",
    )


def test_missing_carrier_fails_loud_no_mutation() -> None:
    manager, registry, state = _bridge_manager(), _fresh_peer_registry(), _state()
    _claim(state, role="R", agi="agi-X", session_id="sess-1")
    client = _client(manager, registry, state)
    resp = _register(client, _open_bridge(manager), agent_instance_id="agi-Y", agent_session_id="")
    _check(resp.status_code == 200, "missing carrier: registration still succeeds (200)")
    _check(
        resp.json().get("self_refresh") == "no_session_key",
        "empty agent_session_id → no_session_key token",
    )
    _check(
        resolve_role_binding(state, "R").agent_instance_id == "agi-X",
        "missing carrier: state table NOT mutated (no accidental session-less CAS-match)",
    )


def test_empty_register_uses_preserved_session_id_for_self_refresh() -> None:
    """A repaired binding must survive an empty heartbeat/re-register.

    This is the 2026-07-26 Codex bridge failure mode: once the server knows a
    logical session id for an ``agent_instance_id``, a later client request with
    ``agent_session_id=""`` must preserve it and feed that effective value into
    state-table self-refresh.
    """
    manager, registry, state = _bridge_manager(), _fresh_peer_registry(), _state()
    registry.register(
        BridgeBinding(
            bridge_id="agc-known",
            agent_id="claude_code",
            agent_instance_id="agi-Y",
            session_label="session-label",
            parent_pid=123,
            agent_session_id="sess-1",
        ),
    )
    _claim(state, role="R", agi="agi-X", session_id="sess-1")
    client = _client(manager, registry, state)
    bridge_id = _open_bridge(manager)
    resp = _register(client, bridge_id, agent_instance_id="agi-Y", agent_session_id="")

    _check(resp.status_code == 200, "empty re-register with preserved id returns 200")
    _check(
        resp.json().get("agent_session_id") == "sess-1",
        "register response exposes preserved agent_session_id",
    )
    _check(
        resp.json().get("self_refresh") == "rerouted:1",
        "preserved agent_session_id feeds state-table self-refresh",
    )
    _check(
        client.get(f"/api/v1/bridge/{bridge_id}/current_identity").json().get(
            "agent_session_id",
        )
        == "sess-1",
        "current_identity keeps the preserved agent_session_id after empty re-register",
    )
    _check(
        resolve_role_binding(state, "R").agent_instance_id == "agi-Y",
        "empty re-register re-points role R using the preserved session id",
    )


def test_state_fault_returns_error_token_still_200() -> None:
    """A state fault during the reconnect CAS is loud-but-non-fatal: the register
    response is 200 with self_refresh='error', and the roles are NOT re-pointed
    (strand until re-claim — the accepted one-shot-at-register tradeoff)."""
    manager, registry, state = _bridge_manager(), _fresh_peer_registry(), _state()
    _claim(state, role="R", agi="agi-X", session_id="sess-1")
    cast("Any", state).fail_next("update")  # force the reconnect CAS update to fault
    client = _client(manager, registry, state)
    resp = _register(
        client,
        _open_bridge(manager),
        agent_instance_id="agi-Y",
        agent_session_id="sess-1",
    )
    _check(resp.status_code == 200, "state fault mid-register: registration STILL succeeds (200)")
    _check(
        resp.json().get("self_refresh") == "error",
        "state fault → 'error' token (loud, non-fatal)",
    )
    _check(
        resolve_role_binding(state, "R").agent_instance_id == "agi-X",
        "state fault: roles NOT re-pointed (strand until re-claim) — the accepted loud tradeoff",
    )


def test_session_role_held_answers_held_on_a_healthy_reconnect() -> None:
    """The register response answers "do I still hold my configured role?" so the
    forwarder's steady-state re-assert never has to ask via the MODEL_INITIATED
    /process/call claim path (which phantom-stamps model activity with no model
    turn and can consume an owed wake to an idle session)."""
    manager, registry, state = _bridge_manager(), _fresh_peer_registry(), _state()
    _claim(state, role="R", agi="agi-X", session_id="sess-1")
    client = _client(manager, registry, state)
    resp = _register(
        client,
        _open_bridge(manager),
        agent_instance_id="agi-Y",
        agent_session_id="sess-1",
        session_role="R",
    )
    _check(
        resp.json().get("self_refresh") == "rerouted:1",
        "healthy reconnect: the role WAS re-pointed",
    )
    _check(
        resp.json().get("session_role_held") == "held",
        "healthy reconnect → 'held' (caller skips the re-claim; nothing to recover)",
    )


def test_session_role_held_is_unknown_when_the_self_refresh_faulted() -> None:
    """R10 — the regression guard for the skip.

    ``holds_role`` compares the stable agent_session_id ALONE; it cannot see
    whether the binding's agent_instance_id pointer is live. When the re-point
    CAS faults, the binding is left held-by-my-session-id but pointing at my DEAD
    instance — the state whose documented remedy is precisely the re-claim
    ('strand until re-claim'). Answering 'held' there would make the forwarder
    SKIP the one thing that heals it, converting a loud one-shot fault into a
    silent permanent strand. Must read 'unknown' → caller claims as before."""
    manager, registry, state = _bridge_manager(), _fresh_peer_registry(), _state()
    _claim(state, role="R", agi="agi-X", session_id="sess-1")
    cast("Any", state).fail_next("update")  # force the reconnect CAS update to fault
    client = _client(manager, registry, state)
    resp = _register(
        client,
        _open_bridge(manager),
        agent_instance_id="agi-Y",
        agent_session_id="sess-1",
        session_role="R",
    )
    _check(resp.json().get("self_refresh") == "error", "precondition: the CAS faulted")
    _check(
        resp.json().get("session_role_held") == "unknown",
        "faulted self-refresh → 'unknown', NEVER 'held' (the re-claim must still run)",
    )


def test_session_role_held_answers_not_held_for_a_vacant_role() -> None:
    """A role nobody holds must read 'not_held' so the claim still fires — this is
    the platform-side-lost-binding recovery the re-assert exists for, and the
    reason the claim cannot simply be deleted."""
    manager, registry, state = _bridge_manager(), _fresh_peer_registry(), _state()
    client = _client(manager, registry, state)
    resp = _register(
        client,
        _open_bridge(manager),
        agent_instance_id="agi-Y",
        agent_session_id="sess-1",
        session_role="nobody-holds-this",
    )
    _check(
        resp.json().get("session_role_held") == "not_held",
        "vacant role → 'not_held' (recovery claim still runs)",
    )


def test_peer_binding_captures_session_id() -> None:
    """S1 data-capture: the PEER_BINDING row carries agent_session_id — non-empty
    when supplied (surfaced via current_identity, was '' pre-S1), empty when not
    (no fail). DATA CAPTURE only: no lookup / routing / dedup on it here."""
    manager, registry, state = _bridge_manager(), _fresh_peer_registry(), _state()
    client = _client(manager, registry, state)
    b1 = _open_bridge(manager)
    _register(client, b1, agent_instance_id="agi-Y", agent_session_id="sess-1")
    _check(
        client.get(f"/api/v1/bridge/{b1}/current_identity").json().get("agent_session_id")
        == "sess-1",
        "PEER_BINDING captures agent_session_id → current_identity surfaces it (was '' pre-S1)",
    )
    b2 = _open_bridge(manager)
    r2 = _register(client, b2, agent_instance_id="agi-Z", agent_session_id="")
    _check(r2.status_code == 200, "empty agent_session_id: register still 200")
    _check(
        client.get(f"/api/v1/bridge/{b2}/current_identity").json().get("agent_session_id")
        == "",
        "PEER_BINDING captures empty agent_session_id as empty (no fail)",
    )


def test_self_refresh_is_state_token_not_address_book() -> None:
    manager, registry, state = _bridge_manager(), _fresh_peer_registry(), _state()
    _claim(state, role="R", agi="agi-X", session_id="sess-1")
    client = _client(manager, registry, state)
    resp = _register(
        client,
        _open_bridge(manager),
        agent_instance_id="agi-Y",
        agent_session_id="sess-1",
    )
    token = str(resp.json().get("self_refresh"))
    _check(token.startswith("rerouted:"), "S3: self_refresh is a STATE-table token (rerouted:n)")
    _check(
        token not in {"updated", "no_matching_role", "no_resolver", "refresh_error_silent"},
        "S3: no address-book self-refresh token (that path was retired)",
    )


def main() -> int:
    print("=== S1-S3 role reconnect self-refresh acceptance smoke ===")
    test_reconnect_repoints_resolution()
    test_backlog_redelivers_across_strand()
    test_multi_role_single_cas()
    test_register_response_carries_no_rehome_tokens()
    test_missing_carrier_fails_loud_no_mutation()
    test_empty_register_uses_preserved_session_id_for_self_refresh()
    test_state_fault_returns_error_token_still_200()
    test_session_role_held_answers_held_on_a_healthy_reconnect()
    test_session_role_held_is_unknown_when_the_self_refresh_faulted()
    test_session_role_held_answers_not_held_for_a_vacant_role()
    test_peer_binding_captures_session_id()
    test_self_refresh_is_state_token_not_address_book()
    print(f"\n{_passed} passed, {len(_failed)} failed")
    if _failed:
        for label in _failed:
            print(f"  FAILED: {label}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
