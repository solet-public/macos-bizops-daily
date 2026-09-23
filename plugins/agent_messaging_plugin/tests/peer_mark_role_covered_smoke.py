#!/usr/bin/env python3
"""Smoke coverage for ``peer_mark_role_covered`` (pull-surface boundary design,
workbench/2026-08-02_pull_surface_boundary_design_claude_d.md §2, R1).

This is the verb's identity-fencing SECURITY surface, tested at the layer
where the fence actually lives — the plugin's ``state`` dict. The floor/
byte-ceiling logic this verb's mark feeds is covered separately in
``ananta/tests/llm/agent_messaging/role_inbox_smoke.py`` (which seeds a mark
directly rather than attesting one live, per design §5b.vi).

Every check below names the mutation that turns it red:

- R1 identity source     → read ``params["agent_instance_id"]`` (a caller-
                            supplied argument) instead of
                            ``state["inference_vertex_session_id"]``
- unregistered refusal   → fall back to ``caller_attribution_instance_id``
                            when the registered-route key is absent (the
                            exact CLI-attribution family R1 says must NEVER
                            be consulted here)
- ownership re-check     → skip ``holds_role`` and trust the resolved
                            instance id's mere existence in the registry
- attest-by-message_id   → accept a caller-asserted ``(created_at, id)``
                            pair instead of looking the row up
- monotonic no-op        → overwrite the mark with an older attestation
- displaced-holder race  → advance the mark for a session no longer holding
                            the role (the SAME silent-loss-for-the-next-
                            holder shape R1 cites for ``peer_claim_role``)

Run:
    SOLET_NAME=<name>-test .venv/bin/python3 \
        plugins/agent_messaging_plugin/tests/peer_mark_role_covered_smoke.py
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, cast

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT / "ananta" / "src"))
sys.path.insert(0, str(REPO_ROOT / "plugins" / "agent_messaging_plugin" / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _real_state_fake import RealShapeState  # noqa: E402
from ananta.interfaces.state_management_interface import (  # noqa: E402, TC002
    StateManagementInterface,
)
from ananta.llm.agent_messaging.role_binding import (  # noqa: E402
    HOLDER_KIND_SESSION,
)
from ananta.llm.agent_messaging.schema import (  # noqa: E402
    NAMESPACE as ROLE_NAMESPACE,
)
from ananta.llm.agent_messaging.schema import (  # noqa: E402
    TABLE_AGENT_ROLE_MESSAGE,
    TABLE_ROLE_COVERED_MARK,
)
from ananta.llm.agent_messaging.service import (  # noqa: E402
    AgentMessagingConfig,
    AgentMessagingService,
)
from ananta.services.store import Store, open_store  # noqa: E402

from agent_messaging_plugin.models import BridgeBinding  # noqa: E402
from agent_messaging_plugin.peer_registry import PeerRegistry  # noqa: E402
from agent_messaging_plugin.plugin import AgentMessagingPlugin  # noqa: E402
from agent_messaging_plugin.role_binding_store import (  # noqa: E402
    HolderClaim,
    claim_role_binding_v4,
)
from agent_messaging_plugin.schema import (  # noqa: E402
    PEER_BINDING_NAMESPACE,
    get_peer_binding_schema,
)

_passed = 0
_failed: list[str] = []

_ROLE = "Claude-D"
_INSTANCE_ID = "agi-holder"
_SESSION_ID = "sess-holder"
_MESSAGE_ID = "msg-arm-001"


def _check(condition: object, label: str) -> None:
    global _passed
    if condition:
        _passed += 1
        print(f"  PASS  {label}")
        return
    _failed.append(label)
    print(f"  FAIL  {label}")


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _registry() -> PeerRegistry:
    store: Store = open_store(
        get_peer_binding_schema(), namespace=PEER_BINDING_NAMESPACE, backend="in_memory",
    )
    return PeerRegistry(bindings_store=store)


def _binding(
    *,
    bridge_id: str = "agc-1",
    agent_instance_id: str = _INSTANCE_ID,
    agent_session_id: str = _SESSION_ID,
    session_label: str = "Claude-D",
) -> BridgeBinding:
    return BridgeBinding(
        bridge_id=bridge_id,
        agent_id="claude_code",
        agent_instance_id=agent_instance_id,
        session_label=session_label,
        parent_pid=1,
        agent_session_id=agent_session_id,
    )


def _claim(
    state: StateManagementInterface,
    *,
    agent_instance_id: str = _INSTANCE_ID,
    agent_session_id: str = _SESSION_ID,
) -> None:
    claim_role_binding_v4(
        state,
        name=_ROLE,
        claim=HolderClaim(
            holder_kind=HOLDER_KIND_SESSION,
            holder_identity={"agent_id": "claude_code"},
            agent_instance_id=agent_instance_id,
            agent_session_id=agent_session_id,
            session_label="Claude-D",
        ),
    )


def _seed_role_msg(
    state: StateManagementInterface, *, row_id: str, message_id: str, created_at: str,
) -> None:
    state.upsert_state(
        ROLE_NAMESPACE,
        {
            "table": TABLE_AGENT_ROLE_MESSAGE,
            "record": {
                "id": row_id,
                "external_id": f"role:{_ROLE}:{message_id}",
                "recipient_kind": "role",
                "recipient_key": _ROLE,
                "message_id": message_id,
                "sender_agent_id": "codex",
                "sender_agent_instance_id": "agi-sender",
                "sender_session_label": "Coordinator",
                "thread_id": f"role:{_ROLE}",
                "important": True,
                "delivered": False,
                "consumed": False,
                "escalated": False,
                "content": [{"type": "text", "text": "hello"}],
                "created_at": created_at,
                "is_deleted": 0,
            },
            "conflict_columns": ["external_id"],
        },
    )


def _plugin(state: StateManagementInterface, registry: PeerRegistry) -> AgentMessagingPlugin:
    plugin = AgentMessagingPlugin()
    plugin._peer_registry = registry  # noqa: SLF001
    plugin._get_state_service = lambda: state  # type: ignore[method-assign]  # noqa: SLF001
    plugin._service = cast(  # noqa: SLF001
        "Any",
        AgentMessagingService(
            repository=cast(Any, None),
            state_service=state,
            config=AgentMessagingConfig(),
        ),
    )
    return plugin


def _fresh() -> tuple[RealShapeState, StateManagementInterface, PeerRegistry]:
    fake = RealShapeState()
    state = cast("StateManagementInterface", fake)
    registry = _registry()
    return fake, state, registry


def _mark_row(state: RealShapeState, role: str = _ROLE) -> dict[str, Any] | None:
    result = state.query_state(
        ROLE_NAMESPACE,
        {"table": TABLE_ROLE_COVERED_MARK, "filters": {"recipient_key": role}},
    )
    records = result["data"]["records"]
    return records[0] if records else None


# ---------------------------------------------------------------------------
# R1 — identity fencing
# ---------------------------------------------------------------------------


def test_unregistered_route_refused() -> None:
    """No ``inference_vertex_session_id`` in ``state`` at all — the one-shot
    ``solet call`` shape R1 exists to refuse."""
    _, state, registry = _fresh()
    registry.register(_binding())
    _claim(state)
    _seed_role_msg(state, row_id="arm-001", message_id=_MESSAGE_ID, created_at="2026-08-02T00:00:00")
    plugin = _plugin(state, registry)

    result = plugin.peer_mark_role_covered(
        {"name": _ROLE, "message_id": _MESSAGE_ID}, {},
    )
    _check(result["action_status"] == "failed", "no registered-route identity → refused")
    _check(
        result["error"]["code"] == "unregistered_route",
        "refusal code is unregistered_route",
    )
    _check(_mark_row(cast("RealShapeState", state)) is None, "no mark was written")


def test_caller_attribution_is_never_consulted_as_a_fallback() -> None:
    """§34.6's ``caller_attribution_*`` family (the LOCAL-CLI provenance) is
    populated but ``inference_vertex_session_id`` (the REGISTERED-bridge
    identity) is not — R1 says this verb must refuse rather than fall back
    to the weaker family, even though it names the SAME live, role-holding
    instance the happy path would have accepted."""
    _, state, registry = _fresh()
    registry.register(_binding())
    _claim(state)
    _seed_role_msg(state, row_id="arm-001", message_id=_MESSAGE_ID, created_at="2026-08-02T00:00:00")
    plugin = _plugin(state, registry)

    result = plugin.peer_mark_role_covered(
        {"name": _ROLE, "message_id": _MESSAGE_ID},
        {
            "caller_attribution_instance_id": _INSTANCE_ID,
            "caller_attribution_agent_id": "claude_code",
            "caller_attribution_role": _ROLE,
        },
    )
    _check(
        result["action_status"] == "failed"
        and result["error"]["code"] == "unregistered_route",
        "caller_attribution_* naming a valid holder is NOT consulted as a fallback",
    )


def test_registered_route_can_attest() -> None:
    _, state, registry = _fresh()
    registry.register(_binding())
    _claim(state)
    _seed_role_msg(state, row_id="arm-001", message_id=_MESSAGE_ID, created_at="2026-08-02T00:00:00")
    plugin = _plugin(state, registry)

    result = plugin.peer_mark_role_covered(
        {"name": _ROLE, "message_id": _MESSAGE_ID},
        {"inference_vertex_session_id": _INSTANCE_ID},
    )
    _check(result["action_status"] == "completed", "registered-route identity can attest")
    _check(
        result["data"]["covered_message_id"] == _MESSAGE_ID
        and result["data"]["covered_id"] == "arm-001",
        "the stored mark is the attested ROW's own (created_at, id), not a caller-asserted one",
    )
    row = _mark_row(cast("RealShapeState", state))
    assert row is not None
    _check(
        row["attested_by_agent_instance_id"] == _INSTANCE_ID
        and row["attested_by_agent_session_id"] == _SESSION_ID,
        "the persisted row's attested_by_* fields are server-sourced, matching the caller",
    )


def test_role_not_held_refused() -> None:
    """The caller is a live, registered instance — just not the role's holder."""
    _, state, registry = _fresh()
    registry.register(_binding())
    _claim(state, agent_instance_id="agi-actual-holder", agent_session_id="sess-actual-holder")
    _seed_role_msg(state, row_id="arm-001", message_id=_MESSAGE_ID, created_at="2026-08-02T00:00:00")
    plugin = _plugin(state, registry)

    result = plugin.peer_mark_role_covered(
        {"name": _ROLE, "message_id": _MESSAGE_ID},
        {"inference_vertex_session_id": _INSTANCE_ID},
    )
    _check(result["action_status"] == "failed", "a non-holder cannot attest")
    _check(result["error"]["code"] == "peer_role_not_held", "refusal code is peer_role_not_held")


def test_displaced_holder_cannot_advance_the_mark() -> None:
    """The identical silent-loss-for-the-next-holder shape §12.2 ruled out
    for the watch spool: a session displaced BETWEEN reading its mail and
    attesting it must not advance the mark past mail the NEW holder never
    saw. The live-at-attestation-time re-check (not claim-time) closes it."""
    _, state, registry = _fresh()
    registry.register(_binding(bridge_id="agc-1", agent_instance_id="agi-old", agent_session_id="sess-old"))
    registry.register(
        _binding(bridge_id="agc-2", agent_instance_id="agi-new", agent_session_id="sess-new"),
    )
    _claim(state, agent_instance_id="agi-old", agent_session_id="sess-old")
    _seed_role_msg(state, row_id="arm-001", message_id=_MESSAGE_ID, created_at="2026-08-02T00:00:00")
    # A NEW claim displaces the old holder — role_binding is compare-and-swap,
    # so this simulates the displacement landing between the old holder's
    # read and its (now-stale) attestation.
    _claim(state, agent_instance_id="agi-new", agent_session_id="sess-new")
    plugin = _plugin(state, registry)

    result = plugin.peer_mark_role_covered(
        {"name": _ROLE, "message_id": _MESSAGE_ID},
        {"inference_vertex_session_id": "agi-old"},
    )
    _check(
        result["action_status"] == "failed"
        and result["error"]["code"] == "peer_role_not_held",
        "the displaced prior holder is refused — cannot advance the mark for the new holder",
    )
    _check(_mark_row(cast("RealShapeState", state)) is None, "no mark was written by the displaced holder")


# ---------------------------------------------------------------------------
# Attest-by-message_id + monotonic no-op
# ---------------------------------------------------------------------------


def test_message_not_found_refused() -> None:
    _, state, registry = _fresh()
    registry.register(_binding())
    _claim(state)
    plugin = _plugin(state, registry)

    result = plugin.peer_mark_role_covered(
        {"name": _ROLE, "message_id": "msg-does-not-exist"},
        {"inference_vertex_session_id": _INSTANCE_ID},
    )
    _check(result["action_status"] == "failed", "an unknown message_id cannot be attested")
    _check(
        result["error"]["code"] == "role_message_not_found",
        "refusal code is role_message_not_found",
    )


def test_monotonic_no_op_does_not_regress() -> None:
    _, state, registry = _fresh()
    registry.register(_binding())
    _claim(state)
    _seed_role_msg(state, row_id="arm-001", message_id="msg-1", created_at="2026-08-02T00:00:00")
    _seed_role_msg(state, row_id="arm-002", message_id="msg-2", created_at="2026-08-02T00:05:00")
    plugin = _plugin(state, registry)
    state_dict = {"inference_vertex_session_id": _INSTANCE_ID}

    newer = plugin.peer_mark_role_covered({"name": _ROLE, "message_id": "msg-2"}, state_dict)
    _check(newer["data"]["covered_id"] == "arm-002", "first attestation advances to msg-2")

    older = plugin.peer_mark_role_covered({"name": _ROLE, "message_id": "msg-1"}, state_dict)
    _check(
        older["action_status"] == "completed" and older["data"]["covered_id"] == "arm-002",
        "attesting an OLDER message is a no-op — returns the PRE-EXISTING (newer) mark",
    )
    row = _mark_row(cast("RealShapeState", state))
    assert row is not None
    _check(row["covered_id"] == "arm-002", "the stored row itself is unchanged (not regressed)")


def test_missing_argument_refused() -> None:
    _, state, registry = _fresh()
    plugin = _plugin(state, registry)
    result = plugin.peer_mark_role_covered(
        {"name": "", "message_id": ""}, {"inference_vertex_session_id": _INSTANCE_ID},
    )
    _check(result["action_status"] == "failed", "empty name/message_id refused")
    _check(result["error"]["code"] == "missing_argument", "refusal code is missing_argument")


def main() -> int:
    print("=== peer_mark_role_covered (pull-surface boundary R1) smoke ===")
    test_unregistered_route_refused()
    test_caller_attribution_is_never_consulted_as_a_fallback()
    test_registered_route_can_attest()
    test_role_not_held_refused()
    test_displaced_holder_cannot_advance_the_mark()
    test_message_not_found_refused()
    test_monotonic_no_op_does_not_regress()
    test_missing_argument_refused()
    print(f"\n{_passed} passed, {len(_failed)} failed")
    if _failed:
        for label in _failed:
            print(f"  FAILED: {label}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
