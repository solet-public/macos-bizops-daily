#!/usr/bin/env python3
"""R3-U1 regression smoke for operator managed-session reconciliation.

The fixture deliberately gives the idle operator row an old transition time.
Its binding is still being polled, so any replacement of positive presence with
inactivity would kill it and fail this test.

Run:
    SOLET_NAME=<name>-test .venv/bin/python3 \
        plugins/agent_messaging_plugin/tests/operator_session_liveness_reconciliation_smoke.py
"""

from __future__ import annotations

import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, cast

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT / "ananta" / "src"))
sys.path.insert(0, str(REPO_ROOT / "plugins" / "agent_messaging_plugin" / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

if TYPE_CHECKING:
    from ananta.interfaces.state_management_interface import StateManagementInterface

from _real_state_fake import RealShapeState  # noqa: E402
from ananta.llm.agent_messaging.role_binding import AGENT_ROLE_BINDING_NAMESPACE  # noqa: E402
from ananta.services.store import Store, open_store  # noqa: E402

from agent_messaging_plugin.bridge_sessions import BridgeSessionManager  # noqa: E402
from agent_messaging_plugin.models import BridgeBinding  # noqa: E402
from agent_messaging_plugin.operator_session_liveness_reconciliation import (  # noqa: E402
    reconcile_operator_session_liveness,
)
from agent_messaging_plugin.peer_registry import PeerRegistry  # noqa: E402
from agent_messaging_plugin.plugin import AgentMessagingPlugin  # noqa: E402
from agent_messaging_plugin.schema import (  # noqa: E402
    LIFECYCLE_IDLE,
    LIFECYCLE_LIVE,
    LIFECYCLE_SPAWNING,
    LIFECYCLE_TERMINATED,
    PEER_BINDING_NAMESPACE,
    get_peer_binding_schema,
)
from agent_messaging_plugin.session_lifecycle_store import (  # noqa: E402
    ManagedSessionSpec,
    insert_managed_session,
    read_managed_session,
    transition_lifecycle_state,
)

T0 = datetime(2026, 9, 3, 20, 0, 0, tzinfo=UTC)


class _Orchestrator:
    """Minimal state-service provider for the public verb fixture."""

    def __init__(self, state: StateManagementInterface) -> None:
        self._state = state

    def get_service(self, name: str) -> StateManagementInterface | None:
        return self._state if name == "state_service" else None


def _state() -> StateManagementInterface:
    return cast("StateManagementInterface", RealShapeState())


def _registry() -> PeerRegistry:
    store: Store = open_store(
        get_peer_binding_schema(),
        namespace=PEER_BINDING_NAMESPACE,
        backend="in_memory",
    )
    return PeerRegistry(bindings_store=store)


def _manager() -> BridgeSessionManager:
    return BridgeSessionManager(
        session_id_factory=lambda _name: "ags-r3-u1",
        idle_timeout_s=3600,
        max_pending_events=50,
        long_poll_timeout_s=1,
    )


def _plugin(
    state: StateManagementInterface,
    registry: PeerRegistry,
    manager: BridgeSessionManager,
) -> AgentMessagingPlugin:
    plugin = AgentMessagingPlugin()
    plugin.orchestrator_ref = _Orchestrator(state)
    plugin._peer_registry = registry  # noqa: SLF001
    plugin._bridge_manager = manager  # noqa: SLF001
    return plugin


def _operator_row(
    state: StateManagementInterface,
    agent_instance_id: str,
    *,
    lifecycle_state: str = LIFECYCLE_LIVE,
) -> None:
    insert_managed_session(
        state,
        ManagedSessionSpec(
            agent_instance_id=agent_instance_id,
            lane_id="r3-u1-fixture",
            brief_ref="",
            work_class="production_mutation",
            budget_line="r3-u1",
            host="operator",
        ),
    )
    transition_lifecycle_state(
        state,
        agent_instance_id=agent_instance_id,
        from_state=LIFECYCLE_SPAWNING,
        to_state=LIFECYCLE_LIVE,
        directed_by="fixture",
    )
    if lifecycle_state == LIFECYCLE_IDLE:
        transition_lifecycle_state(
            state,
            agent_instance_id=agent_instance_id,
            from_state=LIFECYCLE_LIVE,
            to_state=LIFECYCLE_IDLE,
            directed_by="fixture",
        )


def _binding(
    registry: PeerRegistry,
    manager: BridgeSessionManager,
    agent_instance_id: str,
) -> str:
    bridge_id = manager.open(solet_name="", parent_pid=1).bridge_id
    bridge = manager.get(bridge_id)
    assert bridge is not None
    bridge.last_seen_at = T0.isoformat()
    registry.register(
        BridgeBinding(
            bridge_id=bridge_id,
            agent_id="codex",
            agent_instance_id=agent_instance_id,
            session_label=agent_instance_id,
            parent_pid=1,
        ),
    )
    return bridge_id


def _dead_public_verb_fixture() -> tuple[
    StateManagementInterface,
    AgentMessagingPlugin,
]:
    state = _state()
    registry = _registry()
    manager = _manager()
    _operator_row(state, "agi-public-dead")
    bridge_id = _binding(registry, manager, "agi-public-dead")
    manager.close(bridge_id)
    return state, _plugin(state, registry, manager)


def test_public_verb_omitted_dry_run_is_safe() -> None:
    state, plugin = _dead_public_verb_fixture()
    default_result = plugin.reconcile_operator_session_liveness({}, {})
    default_data = cast(dict[str, object], default_result["data"])
    assert default_data["dry_run"] is True
    assert default_data["applied"] == []
    assert read_managed_session(state, "agi-public-dead")["lifecycle_state"] == LIFECYCLE_LIVE


def test_public_verb_false_acts() -> None:
    state, plugin = _dead_public_verb_fixture()
    false_result = plugin.reconcile_operator_session_liveness({"dry_run": False}, {})
    false_data = cast(dict[str, object], false_result["data"])
    assert false_data["dry_run"] is False
    assert false_data["applied"] == ["agi-public-dead"]
    assert read_managed_session(state, "agi-public-dead")["lifecycle_state"] == LIFECYCLE_TERMINATED


def test_public_verb_false_string_acts() -> None:
    state, plugin = _dead_public_verb_fixture()
    false_string_result = plugin.reconcile_operator_session_liveness({"dry_run": "false"}, {})
    false_string_data = cast(dict[str, object], false_string_result["data"])
    assert false_string_data["dry_run"] is False
    assert false_string_data["applied"] == ["agi-public-dead"]


def test_public_verb_true_string_is_safe() -> None:
    state, plugin = _dead_public_verb_fixture()
    true_string_result = plugin.reconcile_operator_session_liveness({"dry_run": "true"}, {})
    true_string_data = cast(dict[str, object], true_string_result["data"])
    assert true_string_data["dry_run"] is True
    assert true_string_data["applied"] == []
    assert read_managed_session(state, "agi-public-dead")["lifecycle_state"] == LIFECYCLE_LIVE


def test_dry_run_then_apply_preserves_live_and_idle_operator_rows() -> None:
    state = _state()
    registry = _registry()
    manager = _manager()
    _operator_row(state, "agi-live")
    _operator_row(state, "agi-dead")
    _operator_row(state, "agi-idle-live", lifecycle_state=LIFECYCLE_IDLE)
    _binding(registry, manager, "agi-live")
    dead_bridge_id = _binding(registry, manager, "agi-dead")
    _binding(registry, manager, "agi-idle-live")
    manager.close(dead_bridge_id)
    state.update_state(
        AGENT_ROLE_BINDING_NAMESPACE,
        {"table": "managed_session", "filters": {"agent_instance_id": "agi-idle-live"}},
        {"last_transition_at": (T0 - timedelta(days=3)).isoformat()},
    )

    dry_run = reconcile_operator_session_liveness(
        state,
        peer_registry=registry,
        bridge_manager=manager,
        dry_run=True,
        now=T0,
    )
    _assert_dry_run(state, dry_run)

    applied = reconcile_operator_session_liveness(
        state,
        peer_registry=registry,
        bridge_manager=manager,
        dry_run=False,
        now=T0,
    )
    _assert_apply_keeps_positive_liveness_rows(state, applied)

    repeated = reconcile_operator_session_liveness(
        state,
        peer_registry=registry,
        bridge_manager=manager,
        dry_run=False,
        now=T0,
    )
    assert repeated["applied"] == []


def _assert_dry_run(state: StateManagementInterface, dry_run: dict[str, object]) -> None:
    classifications = {
        str(row["agent_instance_id"]): row
        for row in cast(list[dict[str, object]], dry_run["classifications"])
    }
    assert dry_run["applied"] == []
    assert classifications["agi-live"]["classification"] == "live"
    assert classifications["agi-idle-live"]["classification"] == "live"
    assert classifications["agi-dead"]["classification"] == "dead"
    assert classifications["agi-dead"]["proposed_to_state"] == LIFECYCLE_TERMINATED
    assert read_managed_session(state, "agi-dead")["lifecycle_state"] == LIFECYCLE_LIVE


def _assert_apply_keeps_positive_liveness_rows(
    state: StateManagementInterface,
    applied: dict[str, object],
) -> None:
    assert applied["applied"] == ["agi-dead"]
    assert read_managed_session(state, "agi-dead")["lifecycle_state"] == LIFECYCLE_TERMINATED
    assert read_managed_session(state, "agi-live")["lifecycle_state"] == LIFECYCLE_LIVE
    assert read_managed_session(state, "agi-idle-live")["lifecycle_state"] == LIFECYCLE_IDLE


def test_indeterminate_binding_data_is_held() -> None:
    state = _state()
    registry = _registry()
    manager = _manager()
    _operator_row(state, "agi-indeterminate")
    bridge_id = _binding(registry, manager, "agi-indeterminate")
    bridge = manager.get(bridge_id)
    assert bridge is not None
    bridge.last_seen_at = "not-a-timestamp"

    result = reconcile_operator_session_liveness(
        state,
        peer_registry=registry,
        bridge_manager=manager,
        dry_run=False,
        now=T0,
    )
    classifications = cast(list[dict[str, object]], result["classifications"])
    assert classifications == [
        {
            "agent_instance_id": "agi-indeterminate",
            "lifecycle_state": LIFECYCLE_LIVE,
            "classification": "indeterminate",
            "detail": "ValueError: Invalid isoformat string: 'not-a-timestamp'",
            "proposed_to_state": None,
        },
    ]
    assert result["applied"] == []
    assert read_managed_session(state, "agi-indeterminate")["lifecycle_state"] == LIFECYCLE_LIVE


def main() -> int:
    test_public_verb_omitted_dry_run_is_safe()
    test_public_verb_false_acts()
    test_public_verb_false_string_acts()
    test_public_verb_true_string_is_safe()
    test_dry_run_then_apply_preserves_live_and_idle_operator_rows()
    test_indeterminate_binding_data_is_held()
    print("PASSED: R3-U1 operator liveness reconciliation")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
