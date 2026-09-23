#!/usr/bin/env python3
"""Regression smoke for Control #5's capped-prefix repair-drain starvation.

Run:
    .venv/bin/python3 ananta/tests/llm/agent_messaging/role_drain_cap_starvation_smoke.py
"""

from __future__ import annotations

import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

REPO_ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(REPO_ROOT / "ananta" / "src"))

from ananta.interfaces.state_management_interface import StateManagementInterface  # noqa: E402
from ananta.llm.agent_messaging.role_binding import (  # noqa: E402
    AGENT_ROLE_BINDING_NAMESPACE,
    HOLDER_KIND_SESSION,
    TABLE_ROLE_BINDING,
    role_binding_external_id,
)
from ananta.llm.agent_messaging.schema import NAMESPACE as ROLE_NAMESPACE  # noqa: E402
from ananta.llm.agent_messaging.schema import TABLE_AGENT_ROLE_MESSAGE  # noqa: E402
from ananta.llm.agent_messaging.service import (  # noqa: E402
    AgentMessagingConfig,
    AgentMessagingService,
)
from ananta.services.state_service.ordered_query import (  # noqa: E402
    apply_ordered_query_in_memory,
    parse_ordered_query,
)

_FAILED: list[str] = []
_PASSED = 0
_INSTANCE = "agi-holder"
_NOW = datetime(2026, 9, 8, tzinfo=UTC)


def _check(condition: object, label: str) -> None:
    global _PASSED
    if condition:
        _PASSED += 1
        print(f"  PASS  {label}")
        return
    _FAILED.append(label)
    print(f"  FAIL  {label}")


class _State:
    """Faithful ordered-query fake for the role-repair read path."""

    def __init__(self) -> None:
        self._tables: dict[tuple[str, str], list[dict[str, object]]] = {}

    def _rows(self, namespace: str, table: str) -> list[dict[str, object]]:
        return self._tables.setdefault((namespace, table), [])

    def upsert_state(self, namespace: str, data: dict[str, object]) -> dict[str, Any]:
        table = cast(str, data["table"])
        record = dict(cast(dict[str, object], data["record"]))
        self._rows(namespace, table).append(record)
        return {"action_status": "completed", "data": {"result": {"upserted": 1}}}

    def query_state(self, namespace: str, data: dict[str, object]) -> dict[str, Any]:
        table = cast(str, data["table"])
        filters = cast(dict[str, object], data.get("filters", {}))
        records = [
            dict(row)
            for row in self._rows(namespace, table)
            if all(row.get(key) == value for key, value in filters.items())
        ]
        return {"action_status": "completed", "data": {"records": records}}

    def query_ordered(self, namespace: str, data: dict[str, object]) -> dict[str, Any]:
        table = cast(str, data["table"])
        records = apply_ordered_query_in_memory(
            self._rows(namespace, table),
            parse_ordered_query(data),
        )
        return {"action_status": "completed", "data": {"records": records}}

    def update_state(
        self,
        namespace: str,
        query: dict[str, object],
        updates: dict[str, object],
    ) -> dict[str, Any]:
        del namespace, query, updates
        raise AssertionError("the repair-drain read path must not update state")


def _service(state: _State) -> AgentMessagingService:
    return AgentMessagingService(
        repository=cast(Any, None),
        state_service=cast(StateManagementInterface, state),
        config=AgentMessagingConfig(),
        clock=lambda: _NOW,
    )


def _bind(state: _State, role: str) -> None:
    state.upsert_state(
        AGENT_ROLE_BINDING_NAMESPACE,
        {
            "table": TABLE_ROLE_BINDING,
            "record": {
                "external_id": role_binding_external_id(role),
                "role": role,
                "holder_kind": HOLDER_KIND_SESSION,
                "agent_id": "codex",
                "agent_instance_id": _INSTANCE,
                "agent_session_id": "sess-holder",
                "session_label": role,
                "is_deleted": 0,
            },
        },
    )


def _row(
    state: _State,
    *,
    row_id: str,
    created_at: str,
    emit_count: int,
) -> None:
    state.upsert_state(
        ROLE_NAMESPACE,
        {
            "table": TABLE_AGENT_ROLE_MESSAGE,
            "record": {
                "id": row_id,
                "external_id": f"role:R:{row_id}",
                "recipient_kind": "role",
                "recipient_key": "R",
                "important": True,
                "consumed": False,
                "escalated": False,
                "emit_count": emit_count,
                "last_emitted_at": None,
                "created_at": created_at,
                "is_deleted": 0,
            },
        },
    )


def _ids(service: AgentMessagingService, *, limit: int) -> list[str]:
    return [
        str(row["id"])
        for row in service.list_undelivered_for_instance(
            agent_instance_id=_INSTANCE,
            limit=limit,
            now=_NOW,
        )
    ]


def test_capped_prefix_does_not_hide_newer_eligible_row() -> None:
    state = _State()
    _bind(state, "R")
    _row(state, row_id="capped-1", created_at="2026-08-04T00:00:00", emit_count=3)
    _row(state, row_id="capped-2", created_at="2026-08-04T00:01:00", emit_count=3)
    _row(state, row_id="eligible", created_at="2026-08-04T00:02:00", emit_count=0)
    _check(
        _ids(_service(state), limit=2) == ["eligible"],
        "capped raw prefix is scanned past and newer eligible row is returned",
    )


def test_uncapped_control_preserves_oldest_first_limit() -> None:
    state = _State()
    _bind(state, "R")
    _row(state, row_id="first", created_at="2026-08-04T00:00:00", emit_count=0)
    _row(state, row_id="second", created_at="2026-08-04T00:01:00", emit_count=0)
    _row(state, row_id="third", created_at="2026-08-04T00:02:00", emit_count=0)
    _check(
        _ids(_service(state), limit=2) == ["first", "second"],
        "uncapped control remains oldest-first and globally limited",
    )


def test_younger_capped_row_does_not_displace_older_eligible_row() -> None:
    state = _State()
    _bind(state, "R")
    _row(state, row_id="eligible", created_at="2026-08-04T00:00:00", emit_count=0)
    _row(state, row_id="capped", created_at="2026-08-04T00:01:00", emit_count=3)
    _check(
        _ids(_service(state), limit=1) == ["eligible"],
        "younger capped control leaves older eligible row unaffected",
    )


def main() -> int:
    print("=== Control #5 capped-prefix starvation smoke ===")
    test_capped_prefix_does_not_hide_newer_eligible_row()
    test_uncapped_control_preserves_oldest_first_limit()
    test_younger_capped_row_does_not_displace_older_eligible_row()
    print(f"\\n{_PASSED} passed, {len(_FAILED)} failed")
    if _FAILED:
        for label in _FAILED:
            print(f"  FAILED: {label}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
