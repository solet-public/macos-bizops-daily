#!/usr/bin/env python3
"""Smoke U1's fleet-status read/classify contract through the plugin shim.

Each assertion names the fixture mutation that would make it red, so a green
run proves the classification rather than merely exercising a happy path.
"""

from __future__ import annotations

import sys
from datetime import UTC, datetime, timedelta
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
from ananta.llm.agent_messaging.schema import TABLE_AGENT_ROLE_MESSAGE  # noqa: E402

from agent_messaging_plugin.fleet_status import (  # noqa: E402
    _ACTIVE_SESSION_READ_CEILING,
    _ARMED_DEPENDENCY_READ_CEILING,
    _NO_CONTRACT_SESSION_READ_CEILING,
    FLEET_STATUS_MAX_OWED_MESSAGES,
)
from agent_messaging_plugin.plugin import AgentMessagingPlugin  # noqa: E402
from agent_messaging_plugin.schema import (  # noqa: E402
    TABLE_MANAGED_SESSION,
    TABLE_SESSION_CONTEXT_STATUS,
    TABLE_SESSION_CONTEXT_STATUS_HISTORY,
    TABLE_SESSION_DEPENDENCY,
)

_passed = 0
_failed: list[str] = []
_OVER_OLD_FLEET_READ_CEILING = 1_001


def _check(condition: object, label: str) -> None:
    global _passed
    if condition:
        _passed += 1
        print(f"  PASS  {label}")
    else:
        _failed.append(label)
        print(f"  FAIL  {label}")


def _bare_plugin(state: StateManagementInterface) -> AgentMessagingPlugin:
    plugin = object.__new__(AgentMessagingPlugin)
    plugin._get_state_service = lambda: state  # type: ignore[method-assign]
    return plugin


def _write(state: RealShapeState, namespace: str, table: str, record: dict[str, Any]) -> None:
    result = state.write_state(namespace, {"table": table, "record": record})
    _check(result.get("action_status") == "completed", f"fixture write {table} completed")


def _session(
    agent_instance_id: str,
    *,
    report_by: str | None,
    lifecycle_state: str = "live",
    lane_id: str = "lane-test",
) -> dict[str, Any]:
    return {
        "agent_instance_id": agent_instance_id,
        "lane_id": lane_id,
        "host": "tmux",
        "lifecycle_state": lifecycle_state,
        "report_by": report_by,
        "report_by_seconds": 300,
        "expires_at": "2026-09-02T08:00:00+00:00",
        "worktree_disposition": "",
    }


def _write_bulk_read_ceiling_fixture(state: RealShapeState) -> None:
    """Create more matching rows than the retired 1,000-row fleet ceiling."""
    for index in range(_OVER_OLD_FLEET_READ_CEILING):
        instance_id = f"agi-bulk-{index:04d}"
        session_result = state.write_state(
            AGENT_ROLE_BINDING_NAMESPACE,
            {
                "table": TABLE_MANAGED_SESSION,
                "record": _session(instance_id, report_by=None, lane_id="zz-bulk"),
            },
        )
        if session_result.get("action_status") != "completed":
            raise AssertionError(f"bulk managed-session fixture write failed at {index}")
        dependency_result = state.write_state(
            AGENT_ROLE_BINDING_NAMESPACE,
            {
                "table": TABLE_SESSION_DEPENDENCY,
                "record": {
                    "id": f"sdp-bulk-{index:04d}",
                    "waiter_instance_id": "agi-holding",
                    "condition_kind": "session_terminal",
                    "condition_ref": instance_id,
                    "fired_at": None,
                },
            },
        )
        if dependency_result.get("action_status") != "completed":
            raise AssertionError(f"bulk session-dependency fixture write failed at {index}")


def _result(plugin: AgentMessagingPlugin) -> dict[str, Any]:
    result = plugin.fleet_status({"parameters": {"scope": "all"}}, {})
    _check(result.get("action_status") == "completed", "fleet_status transport returns success")
    return cast(dict[str, Any], result["data"])


def _by_id(result: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {str(row["agent_instance_id"]): row for row in result["sessions"]}


def _assert_classifications(sessions: dict[str, dict[str, Any]]) -> None:
    _check(
        sessions["agi-no-contract"]["classification"] == "no-contract"
        and sessions["agi-no-contract"]["classification"] != "STALLED",
        "MUTATION report_by=None classifies no-contract and never STALLED",
    )
    _check(
        sessions["agi-holding"]["classification"] == "holding"
        and len(sessions["agi-holding"]["holds"]) == _OVER_OLD_FLEET_READ_CEILING + 1
        and sessions["agi-holding"]["holds"][0]
        == {"id": "sdp-armed-1", "kind": "session_terminal", "ref": "agi-prerequisite"},
        "MUTATION >1,000 armed sdp rows yields every holding id/kind/ref citation",
    )
    _check(
        sessions["agi-stalled"]["classification"] == "STALLED",
        "MUTATION past report_by without a hold classifies STALLED",
    )


def _assert_gauge_projection(result: dict[str, Any], sessions: dict[str, dict[str, Any]]) -> None:
    _check(
        sessions["agi-no-contract"]["gauge"] == {"status": "no gauge"},
        "MUTATION unresolved gauge renders no gauge rather than 0/0/false",
    )
    _check(
        sessions["agi-working"]["gauge"]["current_tokens"] == 123
        and sessions["agi-working"]["gauge"]["ceiling"] == 1000,
        "MUTATION resolved gauge retains its measured token values",
    )
    _check(
        result["unregistered"]["count"] == _OVER_OLD_FLEET_READ_CEILING + 1,
        "MUTATION >1,000 report_by is-null rows are counted completely",
    )


def _assert_owed_messages(result: dict[str, Any], ordered_queries: list[dict[str, Any]]) -> None:
    _check(
        result["legacy_direct_delivery_unknown"] is True
        and result["owed_messages"][0]["message_id"] == "arm-owed-1",
        "owed output is role-only, FIFO, and declares direct-delivery uncertainty",
    )
    role_message_queries = [
        query for query in ordered_queries if query.get("table") == TABLE_AGENT_ROLE_MESSAGE
    ]
    _check(
        len(result["owed_messages"]) == FLEET_STATUS_MAX_OWED_MESSAGES
        and result["owed_messages_truncated"] == 1
        and len(role_message_queries) == 1
        and role_message_queries[0].get("order_by") == [["created_at", "asc"], ["id", "asc"]]
        and role_message_queries[0].get("limit") == FLEET_STATUS_MAX_OWED_MESSAGES + 1
        and role_message_queries[0].get("unbounded") is True,
        "MUTATION >100 owed rows uses one FIFO MAX+1 page and exposes overflow indicator",
    )


def test_fleet_status() -> None:
    state = RealShapeState()
    now = datetime.now(UTC)
    future = (now + timedelta(minutes=10)).isoformat()
    past = (now - timedelta(minutes=10)).isoformat()
    _write(state, AGENT_ROLE_BINDING_NAMESPACE, TABLE_MANAGED_SESSION, _session("agi-working", report_by=future, lane_id="lane-working"))
    _write(state, AGENT_ROLE_BINDING_NAMESPACE, TABLE_MANAGED_SESSION, _session("agi-no-contract", report_by=None, lane_id="lane-no-contract"))
    _write(state, AGENT_ROLE_BINDING_NAMESPACE, TABLE_MANAGED_SESSION, _session("agi-holding", report_by=future, lane_id="lane-holding"))
    _write(state, AGENT_ROLE_BINDING_NAMESPACE, TABLE_MANAGED_SESSION, _session("agi-idle", report_by=future, lifecycle_state="idle", lane_id="lane-idle"))
    _write(state, AGENT_ROLE_BINDING_NAMESPACE, TABLE_MANAGED_SESSION, _session("agi-stalled", report_by=past, lane_id="lane-stalled"))
    _write(
        state,
        AGENT_ROLE_BINDING_NAMESPACE,
        TABLE_SESSION_DEPENDENCY,
        {
            "id": "sdp-armed-1",
            "waiter_instance_id": "agi-holding",
            "condition_kind": "session_terminal",
            "condition_ref": "agi-prerequisite",
            "fired_at": None,
        },
    )
    _write_bulk_read_ceiling_fixture(state)
    _write(
        state,
        AGENT_ROLE_BINDING_NAMESPACE,
        TABLE_SESSION_CONTEXT_STATUS,
        {
            "agent_instance_id": "agi-working",
            "claude_session_id": "runtime-working",
            "model": "gpt-5.6-terra",
            "current_tokens": 123,
            "ceiling": 1000,
            "measured_at": now.isoformat(),
            "is_deleted": 0,
        },
    )
    _write(
        state,
        AGENT_ROLE_BINDING_NAMESPACE,
        TABLE_SESSION_CONTEXT_STATUS_HISTORY,
        {
            "agent_instance_id": "agi-working",
            "claude_session_id": "runtime-working",
            "recorded_at": now.isoformat(),
        },
    )
    _write(
        state,
        "core",
        TABLE_AGENT_ROLE_MESSAGE,
        {
            "recipient_kind": "role",
            "recipient_key": "Coordinator-Main",
            "message_id": "arm-owed-1",
            "thread_id": "role:Coordinator-Main",
            "sender_agent_id": "codex",
            "sender_agent_instance_id": "agi-working",
            "important": True,
            "consumed": False,
            "escalated": False,
            "emit_count": 0,
        },
    )

    # A bounded fleet-status page must never walk the entire owed backlog.  The
    # 101 additional records make the result exceed its 100-record display
    # bound, while the tracked query proves the implementation made one ordered
    # MAX+1 read rather than paging the full table.
    for index in range(FLEET_STATUS_MAX_OWED_MESSAGES + 1):
        _write(
            state,
            "core",
            TABLE_AGENT_ROLE_MESSAGE,
            {
                "recipient_kind": "role",
                "recipient_key": "Coordinator-Main",
                "message_id": f"arm-owed-bulk-{index:03d}",
                "thread_id": "role:Coordinator-Main",
                "sender_agent_id": "codex",
                "sender_agent_instance_id": "agi-working",
                "important": True,
                "consumed": False,
                "escalated": False,
                "emit_count": 0,
                "created_at": (now + timedelta(seconds=index + 1)).isoformat(),
            },
        )

    ordered_queries: list[dict[str, Any]] = []
    original_query_ordered = state.query_ordered

    def _tracked_query_ordered(namespace: str, query: dict[str, Any]) -> dict[str, Any]:
        ordered_queries.append(dict(query))
        return original_query_ordered(namespace, query)

    state.query_ordered = _tracked_query_ordered  # type: ignore[method-assign]

    result = _result(_bare_plugin(cast("StateManagementInterface", state)))
    sessions = _by_id(result)
    _check(
        _ACTIVE_SESSION_READ_CEILING > _OVER_OLD_FLEET_READ_CEILING
        and _NO_CONTRACT_SESSION_READ_CEILING > _OVER_OLD_FLEET_READ_CEILING
        and _ARMED_DEPENDENCY_READ_CEILING > _OVER_OLD_FLEET_READ_CEILING
        and result["class_counts"]["no-contract"] == _OVER_OLD_FLEET_READ_CEILING + 1,
        "MUTATION >1,000 matching rows complete all three read-ceiling paths",
    )
    _assert_classifications(sessions)
    _assert_gauge_projection(result, sessions)
    _assert_owed_messages(result, ordered_queries)


def main() -> int:
    test_fleet_status()
    print(f"fleet_status_smoke: {_passed} passed, {len(_failed)} failed")
    if _failed:
        print("Failures:")
        for failure in _failed:
            print(f"- {failure}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
