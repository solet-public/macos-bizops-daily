#!/usr/bin/env python3
"""Acceptance smoke for the D1 registration-hook fix, END-TO-END through the
REAL ``/peer/register`` route (FastAPI TestClient + in-memory state) — the
MUST-FIX Reviewer-A surfaced independently three times (Dawn ruling
arm-11511b07): ``managed_session`` declares ``agent_session_id``/``agent_id``/
``host_ref`` columns but nothing wrote them, and no hook owned the
``spawning -> live`` edge the design's §3.2 matrix assigns to registration.

RED before this fix: a spawned session's first ``/peer/register`` call left
its ledger row stuck in ``spawning`` forever, with the identity columns
permanently null (session_status/list_sessions could never show them, and
retire_session's role-claim-prune step (session_lifecycle_verbs.py:331,
keyed on ``agent_session_id``) was permanently unreachable). GREEN after:
the route backfills the columns and fires the edge on first registration,
mirroring ``_state_table_self_refresh``'s loud-but-non-fatal posture (a
backfill fault must never block registration itself).

Run:
    .venv/bin/python3 \
        plugins/agent_messaging_plugin/tests/registration_hook_managed_session_smoke.py
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
from ananta.llm.agent_messaging.role_binding import AGENT_ROLE_BINDING_NAMESPACE  # noqa: E402
from ananta.llm.agent_messaging.state_results import require_records  # noqa: E402
from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from agent_messaging_plugin.bridge_sessions import BridgeSessionManager  # noqa: E402
from agent_messaging_plugin.http_routes import register_routes  # noqa: E402
from agent_messaging_plugin.peer_registry import PeerRegistry  # noqa: E402
from agent_messaging_plugin.schema import (  # noqa: E402
    LIFECYCLE_LIVE,
    LIFECYCLE_TERMINATED,
    PEER_BINDING_NAMESPACE,
    get_peer_binding_schema,
)
from agent_messaging_plugin.session_lifecycle_store import (  # noqa: E402
    ManagedSessionSpec,
    insert_managed_session,
    read_managed_session,
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
    from ananta.services.store import Store, open_store  # noqa: PLC0415

    store: Store = open_store(
        get_peer_binding_schema(),
        namespace=PEER_BINDING_NAMESPACE,
        backend="in_memory",
    )
    return PeerRegistry(bindings_store=store)


def _bridge_manager() -> BridgeSessionManager:
    return BridgeSessionManager(
        session_id_factory=lambda _name: "ags-http",
        idle_timeout_s=3600,
        max_pending_events=20,
        long_poll_timeout_s=1,
    )


class _NoOwedDirectWakeService:
    """Minimal route stub — this smoke asserts the managed_session backfill.

    A4 (2026-08-04): used to carry rehome_owed_direct_wakes/rehome_owed_role_wakes
    because the /peer/register route called both on every register; both calls
    retired with the escalation/consumption-reconcile apparatus they served, so
    this fake is now a pure placeholder for the ``agent_messaging_service``
    param the route still requires.
    """


def _client(
    manager: BridgeSessionManager,
    registry: PeerRegistry,
    state: StateManagementInterface,
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


def _open_bridge(manager: BridgeSessionManager) -> str:
    return manager.open(solet_name="", parent_pid=123).bridge_id


def _register(
    client: TestClient,
    bridge_id: str,
    *,
    agent_instance_id: str,
    agent_session_id: str,
    session_label: str = "session-label",
) -> Any:
    return client.post(
        f"/api/v1/bridge/{bridge_id}/peer/register",
        json={
            "agent_id": "claude_code",
            "agent_instance_id": agent_instance_id,
            "agent_session_id": agent_session_id,
            "session_label": session_label,
        },
    )


def test_first_registration_fires_spawning_to_live_and_backfills() -> None:
    manager, registry, state = _bridge_manager(), _fresh_peer_registry(), _state()
    insert_managed_session(
        state,
        ManagedSessionSpec(
            agent_instance_id="agi-hook-1",
            lane_id="lane-hook",
            brief_ref="",
            work_class="read_only",
            budget_line="budget-hook",
            host="headless",
        ),
    )
    client = _client(manager, registry, state)
    resp = _register(
        client,
        _open_bridge(manager),
        agent_instance_id="agi-hook-1",
        agent_session_id="sess-hook-1",
    )
    _check(resp.status_code == 200, "registration with a spawning-state row still returns 200")
    row = read_managed_session(state, "agi-hook-1")
    _check(
        row["lifecycle_state"] == LIFECYCLE_LIVE,
        "the ROUTE fires spawning->live on first registration (was stuck 'spawning' pre-fix)",
    )
    _check(
        row["agent_session_id"] == "sess-hook-1" and row["agent_id"] == "claude_code",
        "the ROUTE backfills agent_session_id/agent_id (were permanently null pre-fix)",
    )


def test_reconnect_does_not_refire_edge_or_clobber_later_state() -> None:
    """A reconnect (or a late/duplicate register) on a row already past
    'spawning' must re-write identity (self-correcting) but never attempt
    the one-time edge again — and must never crash if something else moved
    the row to a terminal state in the interim."""
    manager, registry, state = _bridge_manager(), _fresh_peer_registry(), _state()
    insert_managed_session(
        state,
        ManagedSessionSpec(
            agent_instance_id="agi-hook-2",
            lane_id="lane-hook",
            brief_ref="",
            work_class="read_only",
            budget_line="budget-hook",
            host="headless",
        ),
    )
    client = _client(manager, registry, state)
    bridge_id = _open_bridge(manager)
    _register(client, bridge_id, agent_instance_id="agi-hook-2", agent_session_id="sess-hook-2a")
    _check(
        read_managed_session(state, "agi-hook-2")["lifecycle_state"] == LIFECYCLE_LIVE,
        "precondition: first registration reached 'live'",
    )
    # Simulate something else (terminate_session / the sweep) moving the row
    # to a terminal state before a reconnect arrives.
    real_rows = state._rows[  # type: ignore[attr-defined]
        ("agent_messaging_plugin", "managed_session")
    ]
    for r in real_rows:
        if r["agent_instance_id"] == "agi-hook-2":
            r["lifecycle_state"] = LIFECYCLE_TERMINATED
    resp = _register(
        client,
        bridge_id,
        agent_instance_id="agi-hook-2",
        agent_session_id="sess-hook-2b",
    )
    _check(
        resp.status_code == 200,
        "a reconnect arriving after the row went terminal still returns 200 "
        "(the backfill's identity write is unconditional and harmless; only "
        "the edge is state-guarded, and it correctly does not fire here)",
    )
    row = read_managed_session(state, "agi-hook-2")
    _check(
        row["lifecycle_state"] == LIFECYCLE_TERMINATED,
        "a terminal row is NEVER pulled back to 'live' by a late registration",
    )
    _check(
        row["agent_session_id"] == "sess-hook-2b",
        "identity columns still self-correct even on a terminal row (harmless)",
    )


def test_hand_launched_registration_creates_operator_inventory_row() -> None:
    """A registration with no spawn lineage creates a real, no-contract row."""
    manager, registry, state = _bridge_manager(), _fresh_peer_registry(), _state()
    client = _client(manager, registry, state)
    resp = _register(
        client,
        _open_bridge(manager),
        agent_instance_id="agi-no-lineage",
        agent_session_id="sess-no-lineage",
    )
    _check(resp.status_code == 200, "registration with no managed_session row returns 200")
    result = state.query_state(
        AGENT_ROLE_BINDING_NAMESPACE,
        {"table": "managed_session", "filters": {"agent_instance_id": "agi-no-lineage"}},
    )
    _check(
        len(require_records(result)) == 1,
        "an ordinary hand-launched registration creates exactly one inventory row",
    )
    rows = require_records(result)
    if rows:
        row = rows[0]
        _check(
            row["host"] == "operator" and row["lifecycle_state"] == "live",
            "the inventory row is an active operator session, not a fabricated spawn",
        )
        _check(
            row.get("report_by") is None and row.get("expires_at") is None,
            "the row carries no report-by or TTL deadline the session never agreed to",
        )
        _check(
            row.get("provisioning_mode") == "operator_existing_checkout",
            "the operator row records its shared checkout rather than a fabricated worktree",
        )
        _check(
            row.get("local_name") == "session-label",
            "the operator row uses the effective registration label as its local name",
        )
        _check(
            row.get("model") == "" and row.get("effort") == "",
            "model and effort stay intentionally empty when registration has no launcher evidence",
        )


def test_hand_launched_empty_label_uses_instance_id_as_local_name() -> None:
    """No peer label must not mint an empty local-name collision key."""
    manager, registry, state = _bridge_manager(), _fresh_peer_registry(), _state()
    client = _client(manager, registry, state)
    resp = _register(
        client,
        _open_bridge(manager),
        agent_instance_id="agi-empty-label",
        agent_session_id="sess-empty-label",
        session_label="",
    )
    _check(resp.status_code == 200, "an unlabeled hand-launched registration still returns 200")
    row = read_managed_session(state, "agi-empty-label")
    _check(
        row.get("local_name") == "agi-empty-label",
        "an empty effective label falls back to the registering instance id",
    )


def main() -> int:
    print("=== D1 registration-hook (managed_session backfill) acceptance smoke ===")
    test_first_registration_fires_spawning_to_live_and_backfills()
    test_reconnect_does_not_refire_edge_or_clobber_later_state()
    test_hand_launched_registration_creates_operator_inventory_row()
    test_hand_launched_empty_label_uses_instance_id_as_local_name()
    print(f"\n{_passed} passed, {len(_failed)} failed")
    if _failed:
        for label in _failed:
            print(f"  FAILED: {label}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
