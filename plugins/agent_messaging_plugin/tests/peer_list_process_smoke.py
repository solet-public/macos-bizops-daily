#!/usr/bin/env python3
"""Smoke coverage for the ``peer_list`` platform process (WS-1a follow-on).

Closes the peer-enumeration asymmetry ``peer_inbox`` left open: a no-MCP
session could read its OWN mail via ``peer_inbox`` but had no way to see who
else was live. ``peer_list`` is a global, unfiltered registry snapshot — no
identity resolution, no cursors, zero parameters — so this suite is smaller
than ``peer_inbox_process_smoke.py`` by construction, not by omission.

Every check below names the mutation that turns it red:

- inactive plugin      → return an empty snapshot instead of failing loud
                         (makes "messaging is off" read as "no peers exist")
- field-set widening   → start emitting ``bridge_id`` or ``agent_session_id``,
                         silently exposing routing/identity fields neither
                         pre-existing MCP surface has ever shown
- three-surface drift  → hand-roll the dict in one surface instead of calling
                         the shared ``peer_list_view.serialize_peer_list``
- serialized shape     → return_value_schema keys stop matching the actual
                         returned keys
- grouping             → flatten per-agent_id grouping or drop the sort on
                         ``agent_ids``

Run:
    SOLET_NAME=<name>-test .venv/bin/python3 \
        plugins/agent_messaging_plugin/tests/peer_list_process_smoke.py
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT / "ananta" / "src"))
sys.path.insert(0, str(REPO_ROOT / "plugins" / "agent_messaging_plugin" / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from ananta.services.store import Store, open_store  # noqa: E402

from agent_messaging_plugin.models import BridgeBinding  # noqa: E402
from agent_messaging_plugin.peer_list_view import serialize_peer_list  # noqa: E402
from agent_messaging_plugin.peer_registry import PeerRegistry  # noqa: E402
from agent_messaging_plugin.plugin import AgentMessagingPlugin  # noqa: E402
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


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _registry() -> PeerRegistry:
    store: Store = open_store(
        get_peer_binding_schema(),
        namespace=PEER_BINDING_NAMESPACE,
        backend="in_memory",
    )
    return PeerRegistry(bindings_store=store)


def _binding(
    *,
    bridge_id: str,
    agent_id: str,
    agent_instance_id: str,
    session_label: str,
    parent_pid: int = 1,
    agent_session_id: str = "",
) -> BridgeBinding:
    return BridgeBinding(
        bridge_id=bridge_id,
        agent_id=agent_id,
        agent_instance_id=agent_instance_id,
        session_label=session_label,
        parent_pid=parent_pid,
        agent_session_id=agent_session_id,
    )


def _plugin(*, registry: PeerRegistry | None, active: bool = True) -> AgentMessagingPlugin:
    plugin = AgentMessagingPlugin()
    plugin._active = active  # noqa: SLF001
    plugin._peer_registry = registry  # noqa: SLF001
    return plugin


def _call(plugin: AgentMessagingPlugin) -> dict[str, Any]:
    return plugin.peer_list({}, {})


# ---------------------------------------------------------------------------
# bridge.not_running — an inactive plugin fails loud, never an empty snapshot
# ---------------------------------------------------------------------------


def test_inactive_plugin_fails_loud_not_empty() -> None:
    result = _call(_plugin(registry=_registry(), active=False))
    _check(result["action_status"] == "failed", "inactive plugin fails the call")
    _check(
        result["error"]["code"] == "bridge.not_running",
        "failure code is bridge.not_running",
    )


def test_missing_registry_fails_loud() -> None:
    result = _call(_plugin(registry=None, active=True))
    _check(result["action_status"] == "failed", "no registry fails the call")
    _check(result["error"]["code"] == "bridge.not_running", "same failure code")


# ---------------------------------------------------------------------------
# The happy path — grouped snapshot, sorted agent_ids
# ---------------------------------------------------------------------------


def test_snapshot_groups_by_agent_id_and_sorts_keys() -> None:
    registry = _registry()
    registry.register(
        _binding(
            bridge_id="agc-1", agent_id="claude_code", agent_instance_id="agi-1",
            session_label="Claude-D",
        ),
    )
    registry.register(
        _binding(
            bridge_id="agc-2", agent_id="claude_code", agent_instance_id="agi-2",
            session_label="Coordinator-Dawn",
        ),
    )
    registry.register(
        _binding(
            bridge_id="agc-3", agent_id="codex", agent_instance_id="agi-3",
            session_label="Codex-1",
        ),
    )
    result = _call(_plugin(registry=registry))
    _check(result["action_status"] == "completed", "populated registry succeeds")
    data = result["data"]
    _check(
        data["agent_ids"] == ["claude_code", "codex"],
        "agent_ids is the sorted list of distinct agent_id kinds",
    )
    _check(
        len(data["instances"]["claude_code"]) == 2
        and len(data["instances"]["codex"]) == 1,
        "instances groups bindings under their agent_id, counts match",
    )
    labels = {row["session_label"] for row in data["instances"]["claude_code"]}
    _check(
        labels == {"Claude-D", "Coordinator-Dawn"},
        "each instance row carries its own session_label",
    )


def test_empty_registry_returns_empty_snapshot_not_a_failure() -> None:
    result = _call(_plugin(registry=_registry()))
    _check(result["action_status"] == "completed", "empty registry still succeeds")
    _check(
        result["data"] == {"agent_ids": [], "instances": {}},
        "an empty registry is a real empty snapshot, distinct from bridge.not_running",
    )


# ---------------------------------------------------------------------------
# The field-set decision — pinned so it cannot silently widen
# ---------------------------------------------------------------------------


def test_bridge_id_and_agent_session_id_are_never_exposed() -> None:
    registry = _registry()
    registry.register(
        _binding(
            bridge_id="agc-secret", agent_id="claude_code",
            agent_instance_id="agi-1", session_label="Claude-D",
            agent_session_id="ases-secret-session",
        ),
    )
    data = _call(_plugin(registry=registry))["data"]
    row = data["instances"]["claude_code"][0]
    _check(
        set(row)
        == {
            "agent_instance_id", "session_label", "parent_pid",
            "registered_at", "created_at", "updated_at",
        },
        "exactly the six-field subset — no more, no fewer",
    )
    _check(
        "agc-secret" not in str(row) and "ases-secret-session" not in str(row),
        "bridge_id and agent_session_id never leak into the serialized row, "
        "even though both were present on the underlying binding",
    )


# ---------------------------------------------------------------------------
# Three-surface parity — one shared serializer, not three hand-rolled copies
# ---------------------------------------------------------------------------


def test_process_output_matches_the_shared_serializer_directly() -> None:
    """The process must call serialize_peer_list, not reimplement its shape.

    A hand-rolled copy in the process body would pass every test above yet
    still drift from the HTTP route / MCP tool the moment either changes —
    exactly the class of bug ``peer_inbox_view``'s docstring describes. This
    asserts byte-identical output against the shared function called
    independently on the same snapshot, which only holds if the process
    delegates rather than reimplements.
    """
    registry = _registry()
    registry.register(
        _binding(
            bridge_id="agc-1", agent_id="claude_code", agent_instance_id="agi-1",
            session_label="Claude-D",
        ),
    )
    process_data = _call(_plugin(registry=registry))["data"]
    direct = serialize_peer_list(registry.list_agent_ids())
    _check(
        process_data == direct,
        "peer_list's returned data is byte-identical to calling "
        "serialize_peer_list directly on the same registry snapshot",
    )


# ---------------------------------------------------------------------------
# Declared schema matches the actual return shape
# ---------------------------------------------------------------------------

_EXPECTED_KEYS = {"agent_ids", "instances"}


def test_serialized_snapshot_matches_the_declared_schema() -> None:
    registry = _registry()
    registry.register(
        _binding(
            bridge_id="agc-1", agent_id="claude_code", agent_instance_id="agi-1",
            session_label="Claude-D",
        ),
    )
    data = _call(_plugin(registry=registry))["data"]
    _check(
        set(data) == _EXPECTED_KEYS,
        "the returned keys are exactly agent_ids + instances",
    )
    declared = AgentMessagingPlugin.peer_list._platform_process_metadata  # noqa: SLF001
    schema_keys = set(declared.return_value_schema.properties)
    _check(
        schema_keys == _EXPECTED_KEYS,
        "return_value_schema declares exactly the keys the code returns",
    )
    _check(declared.name == "peer_list", "the decorator registers the verb as 'peer_list'")
    _check(
        declared.parameters == {},
        "peer_list takes zero parameters — a global snapshot has nothing to scope by",
    )


def main() -> None:
    print("=== peer_list platform process smoke ===")
    for test in (
        test_inactive_plugin_fails_loud_not_empty,
        test_missing_registry_fails_loud,
        test_snapshot_groups_by_agent_id_and_sorts_keys,
        test_empty_registry_returns_empty_snapshot_not_a_failure,
        test_bridge_id_and_agent_session_id_are_never_exposed,
        test_process_output_matches_the_shared_serializer_directly,
        test_serialized_snapshot_matches_the_declared_schema,
    ):
        test()
    print(f"\n{_passed} passed, {len(_failed)} failed")
    if _failed:
        for label in _failed:
            print(f"  FAILED: {label}")
        sys.exit(1)


if __name__ == "__main__":
    main()
