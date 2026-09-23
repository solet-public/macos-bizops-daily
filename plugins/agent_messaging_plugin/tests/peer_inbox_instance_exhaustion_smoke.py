#!/usr/bin/env python3
"""Hermetic regression coverage for newest-first instance inbox paging.

Pins truthful ``instance_exhausted`` and the cursorless backlog fix: a new
reader sees newest pending work instead of an old five-row prefix. The 100/101
cases exercise the explicit ``query_ordered(unbounded=True)`` seam.
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

from _real_state_fake import RealShapeState  # noqa: E402
from ananta.interfaces.state_management_interface import StateManagementInterface  # noqa: E402, TC002
from ananta.llm.agent_messaging.repository import AgentMessagingRepository  # noqa: E402
from ananta.llm.agent_messaging.schema import (  # noqa: E402
    ID_PREFIX_MESSAGE,
    NAMESPACE,
    TABLE_AGENT_MESSAGE,
    TABLE_AGENT_THREAD,
)
from ananta.llm.agent_messaging.service import (  # noqa: E402
    AgentMessagingConfig,
    AgentMessagingService,
)
from ananta.services.store import Store, open_store  # noqa: E402

from agent_messaging_plugin.models import BridgeBinding  # noqa: E402
from agent_messaging_plugin.peer_registry import PeerRegistry  # noqa: E402
from agent_messaging_plugin.plugin import AgentMessagingPlugin  # noqa: E402
from agent_messaging_plugin.schema import (  # noqa: E402
    PEER_BINDING_NAMESPACE,
    get_peer_binding_schema,
)

_AGENT_ID = "claude_code"
_INSTANCE_ID = "agi-instance"
_SESSION_ID = "ases-instance"
_START = datetime(2026, 8, 1, tzinfo=UTC)
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


def _registry() -> PeerRegistry:
    store: Store = open_store(
        get_peer_binding_schema(), namespace=PEER_BINDING_NAMESPACE, backend="in_memory",
    )
    registry = PeerRegistry(bindings_store=store)
    registry.register(
        BridgeBinding(
            bridge_id="agc-instance",
            agent_id=_AGENT_ID,
            agent_instance_id=_INSTANCE_ID,
            session_label="Instance-Reader",
            parent_pid=1,
            agent_session_id=_SESSION_ID,
        ),
    )
    return registry


def _plugin(state: RealShapeState) -> AgentMessagingPlugin:
    service = AgentMessagingService(
        repository=AgentMessagingRepository(cast(StateManagementInterface, state)),
        state_service=cast(StateManagementInterface, state),
        config=AgentMessagingConfig(),
    )
    plugin = AgentMessagingPlugin()
    plugin._active = True  # noqa: SLF001
    plugin._peer_registry = _registry()  # noqa: SLF001
    plugin._service = cast(Any, service)  # noqa: SLF001
    return plugin


def _seed(state: RealShapeState, *, count: int, threads: int) -> None:
    for index in range(count):
        thread_id = f"agt-{index % threads}"
        if not any(row["id"] == thread_id for row in state.rows(NAMESPACE, TABLE_AGENT_THREAD)):
            state.rows(NAMESPACE, TABLE_AGENT_THREAD).append(
                {
                    "id": thread_id,
                    "namespace": "core",
                    "target_backend": f"peer:{_AGENT_ID}",
                    "recipient_agent_instance_id": _INSTANCE_ID,
                    "recipient_agent_session_id": _SESSION_ID,
                    "is_deleted": 0,
                },
            )
        created_at = (_START + timedelta(seconds=index)).replace(tzinfo=None).isoformat()
        state.rows(NAMESPACE, TABLE_AGENT_MESSAGE).append(
            {
                "id": f"{ID_PREFIX_MESSAGE}-_{index:03d}",
                "namespace": "core",
                "thread_id": thread_id,
                "cursor": index,
                "role": "originator",
                "kind": "message",
                "content": [{"type": "text", "text": f"message {index}"}],
                "action_id": None,
                "backend_session_id": None,
                "error": None,
                "artifacts": [],
                "metadata": {},
                "important": True,
                "created_at": created_at,
                "is_deleted": 0,
            },
        )


def _read(plugin: AgentMessagingPlugin, **params: object) -> dict[str, Any]:
    result = plugin.peer_inbox_action({"agent_session_id": _SESSION_ID, **params}, {})
    assert result["action_status"] == "completed"
    return cast("dict[str, Any]", result["data"])


def _cursors(data: dict[str, Any]) -> list[int]:
    return [entry["message"]["cursor"] for entry in data["entries"]]


def test_default_page_reaches_the_newest_pending_work() -> None:
    state = RealShapeState()
    _seed(state, count=6, threads=2)
    plugin = _plugin(state)
    first = _read(plugin)
    _check(_cursors(first) == [5, 4, 3, 2, 1], "bare default returns the newest five across two threads")
    _check(first["instance_exhausted"] is False, "a full newest-first page with one older row is not exhausted")
    second = _read(plugin, after=first["next_after_created_at"])
    _check(_cursors(second) == [0], "backward after cursor reaches the final older message")
    _check(second["instance_exhausted"] is True, "the terminal backward page reports instance_exhausted=true")


def test_backlog_depths_repeat_and_cursor_completion() -> None:
    """0/5/6/12 rows: newest page, cursorless repeat, then full drain."""
    for count in (0, 5, 6, 12):
        state = RealShapeState()
        _seed(state, count=count, threads=2)
        plugin = _plugin(state)
        first = _read(plugin)
        repeated = _read(plugin)
        expected_first = list(range(count - 1, max(-1, count - 6), -1))
        _check(
            _cursors(first) == expected_first,
            f"depth {count}: cursorless page starts at the newest row",
        )
        _check(
            _cursors(repeated) == expected_first,
            f"depth {count}: cursorless repeat is explicit page-one replay",
        )
        _check(
            first["instance_exhausted"] is (count <= 5),
            f"depth {count}: instance_exhausted means no older row remains",
        )
        collected = _cursors(first)
        page = first
        while not page["instance_exhausted"]:
            page = _read(plugin, after=page["next_after_created_at"])
            collected.extend(_cursors(page))
        _check(
            collected == list(range(count - 1, -1, -1)),
            f"depth {count}: echoed cursor reaches every row exactly once",
        )


def test_hundred_row_seam() -> None:
    exact_state = RealShapeState()
    _seed(exact_state, count=100, threads=1)
    exact = _read(_plugin(exact_state), limit=100)
    _check(len(exact["entries"]) == 100 and exact["instance_exhausted"] is True, "exactly 100 rows are exhausted")

    over_state = RealShapeState()
    _seed(over_state, count=101, threads=1)
    over = _read(_plugin(over_state), limit=100)
    _check(len(over["entries"]) == 100 and over["instance_exhausted"] is False, "101 rows retain a bounded first page and expose lookahead")


def test_empty_is_exhausted() -> None:
    empty = _read(_plugin(RealShapeState()))
    _check(empty["entries"] == [] and empty["instance_exhausted"] is True, "an empty instance inbox is exhausted")


def main() -> None:
    print("=== peer_inbox instance exhaustion smoke ===")
    test_default_page_reaches_the_newest_pending_work()
    test_backlog_depths_repeat_and_cursor_completion()
    test_hundred_row_seam()
    test_empty_is_exhausted()
    print(f"\n{_passed} passed, {len(_failed)} failed")
    if _failed:
        for label in _failed:
            print(f"  FAILED: {label}")
        sys.exit(1)


if __name__ == "__main__":
    main()
