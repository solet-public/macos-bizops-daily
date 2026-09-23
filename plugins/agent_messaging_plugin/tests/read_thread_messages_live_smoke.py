#!/usr/bin/env python3
"""Live-Postgres behavioral smoke for ``read_thread_messages`` (GAP-5 STUB-4).

Pins the new UNSCOPED per-thread message-read verb against a REAL
``PostgresProvider``. read_thread_messages is the unscoped counterpart to
``list_messages``: it reuses the SAME repository read (``_repo.list_messages`` —
int-cursor pagination, ``cursor`` strictly greater than ``after_cursor``) but
WITHOUT the bridge-ownership gate (no ``bridge_id``), because the only consumer
(the session-ledger projection) reads threads it does not own. It returns a
minimal ``AgentThreadMessagesPage`` (no thread ``status`` — the owned-thread
fetch is intentionally skipped). Structural-only / non-discoverable.

THE discriminator: a thread owned by ``bridge-owner`` is read by
read_thread_messages with NO bridge_id (returns the messages), while the
ownership-scoped ``list_messages`` called by a NON-owning bridge RAISES — proving
read_thread_messages deliberately bypasses the gate list_messages enforces.

``created_at``/``cursor`` are seeded via direct INSERT only where the schema
allows; ``created_at`` is platform-read-only (omitted, DB default applies).

Env-gated behind ``READ_THREAD_MESSAGES_LIVE_SMOKE=1``. Run::

    READ_THREAD_MESSAGES_LIVE_SMOKE=1 \\
      .venv/bin/python3 plugins/agent_messaging_plugin/tests/read_thread_messages_live_smoke.py
"""

from __future__ import annotations

import importlib
import json
import os
import secrets
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any, LiteralString, cast

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT / "ananta" / "src"))
sys.path.insert(
    0, str(REPO_ROOT / "plugins" / "postgres_state_management_plugin" / "src"),
)

importlib.import_module("ananta.core.config.config_manager")
from ananta.llm.agent_messaging.models import (  # noqa: E402
    ListAgentMessagesRequest,
    ReadThreadMessagesRequest,
)
from ananta.llm.agent_messaging.repository import (  # noqa: E402
    AgentMessagingRepository,
)
from ananta.llm.agent_messaging.schema import (  # noqa: E402
    NAMESPACE,
    get_agent_messaging_schema,
)
from ananta.llm.agent_messaging.service import AgentMessagingService  # noqa: E402
from ananta.services.state_service.ordered_query import (  # noqa: E402
    parse_ordered_query,
)
from ananta.types.schema_standardizer import SchemaStandardizer  # noqa: E402
from postgres_state_management_plugin.postgres_backend.config import (  # noqa: E402
    PostgresConfig,
)
from postgres_state_management_plugin.postgres_backend.ddl_renderer import (  # noqa: E402
    emit_create_table_ops,
)
from postgres_state_management_plugin.postgres_backend.provider import (  # noqa: E402
    PostgresProvider,
)

_passed = 0
_failed: list[str] = []

_THREAD_TABLE = "core__agent_thread"
_MESSAGE_TABLE = "core__agent_message"


def _check(condition: object, label: str) -> None:
    global _passed
    if condition:
        _passed += 1
        print(f"  PASS  {label}")
    else:
        _failed.append(label)
        print(f"  FAIL  {label}")


_PROFILE_PG_CONFIG = (
    REPO_ROOT / "profile" / "config" / "plugins"
    / "postgres_state_management_plugin.json"
)


def _load_pg_config(schema_name: str) -> PostgresConfig:
    config = PostgresConfig(**json.loads(_PROFILE_PG_CONFIG.read_text(encoding="utf-8")))
    config.pg_schema = schema_name
    return config


def _envelope(data: dict[str, Any]) -> dict[str, Any]:
    return {"action_status": "completed", "data": data, "actions": [], "error": None}


class _LiveStateAdapter:
    """query_ordered + query_state over a real provider through the production
    path (repo.list_messages uses query_ordered; _require_owned_thread's
    get_thread uses query_state)."""

    def __init__(self, provider: PostgresProvider) -> None:
        self._provider = provider

    def query_ordered(self, namespace: str, data: dict[str, Any]) -> dict[str, Any]:
        spec = parse_ordered_query(data)
        rows = self._provider.select_ordered(
            namespace=namespace,
            table=spec.table,
            conditions=spec.filters,
            order_columns=spec.order_columns,
            direction=spec.direction,
            limit=spec.limit,
            after=spec.after,
            include_deleted=spec.include_deleted,
        )
        return _envelope({"records": rows, "count": len(rows)})

    def query_state(self, namespace: str, query: dict[str, Any]) -> dict[str, Any]:
        table = str(query["table"])
        filters = cast("dict[str, Any]", query.get("filters") or {})
        rows = self._provider.select(namespace=namespace, table=table, conditions=filters)
        return _envelope({"records": rows, "count": len(rows)})


def _create_tables(provider: PostgresProvider) -> None:
    schema = SchemaStandardizer().standardize_schema(get_agent_messaging_schema())
    schema_name = provider.config.schema_name
    ops = [
        op
        for table in schema.tables.values()
        for op in emit_create_table_ops(NAMESPACE, table, schema_name)
    ]
    with provider.get_transactional_connection() as conn, conn.cursor() as cur:
        provider.apply_schema_change_ops(cur, schema, ops)


def _seed_thread(provider: PostgresProvider, schema: str, *, tid: str, owner_bridge: str) -> None:
    with provider.get_transactional_connection() as conn, conn.cursor() as cur:
        cur.execute(
            cast(
                LiteralString,
                f'INSERT INTO "{schema}"."{_THREAD_TABLE}" '
                "(id, namespace, originator_type, originator_bridge_id, target_backend, "
                "target_plugin_name, status, last_message_cursor, is_deleted) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,0)",
            ),
            (tid, "core", "mcp_bridge", owner_bridge, "peer:claude_code",
             "agent_messaging_plugin", "open", 0),
        )


def _seed_message(
    provider: PostgresProvider, schema: str, *, mid: str, thread_id: str, cursor: int, text: str,
) -> None:
    content = json.dumps([{"type": "text", "text": text}])
    with provider.get_transactional_connection() as conn, conn.cursor() as cur:
        cur.execute(
            cast(
                LiteralString,
                f'INSERT INTO "{schema}"."{_MESSAGE_TABLE}" '
                "(id, namespace, thread_id, cursor, role, kind, content, is_deleted) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,0)",
            ),
            (mid, "core", thread_id, cursor, "agent", "message", content),
        )


def _service(provider: PostgresProvider) -> AgentMessagingService:
    service = object.__new__(AgentMessagingService)
    service._repo = AgentMessagingRepository(cast("Any", _LiveStateAdapter(provider)))
    service._config = cast("Any", SimpleNamespace(enabled=True))
    return service


def _ids(page: Any) -> list[str]:
    return [m.id for m in page.messages]


def test_unscoped_read_and_isolation(service: AgentMessagingService) -> None:
    page = service.read_thread_messages(ReadThreadMessagesRequest(thread_id="agt-1"))
    _check(_ids(page) == ["agm-_001", "agm-_002", "agm-_003"], f"reads agt-1's messages in cursor order; got {_ids(page)}")
    _check(page.thread_id == "agt-1" and page.next_cursor == 3, f"minimal page: thread_id + int next_cursor=3; got {page}")
    _check(page.messages[0].content[0].text == "hello-1", "AgentMessageRow content round-trips")
    other = service.read_thread_messages(ReadThreadMessagesRequest(thread_id="agt-2"))
    _check(_ids(other) == ["agm-_004"], f"thread isolation: agt-2 returns only its own message; got {_ids(other)}")


def test_int_cursor_pagination(service: AgentMessagingService) -> None:
    after1 = service.read_thread_messages(ReadThreadMessagesRequest(thread_id="agt-1", after_cursor=1))
    _check(_ids(after1) == ["agm-_002", "agm-_003"], f"after_cursor=1 → strict cursor>1 (m2,m3); got {_ids(after1)}")
    p1 = service.read_thread_messages(ReadThreadMessagesRequest(thread_id="agt-1", after_cursor=0, limit=2))
    _check(_ids(p1) == ["agm-_001", "agm-_002"] and p1.next_cursor == 2, f"page1 (limit=2) → m1,m2 next=2; got {_ids(p1)}/{p1.next_cursor}")
    p2 = service.read_thread_messages(ReadThreadMessagesRequest(thread_id="agt-1", after_cursor=p1.next_cursor, limit=2))
    _check(_ids(p2) == ["agm-_003"] and p2.next_cursor == 3, f"page2 → m3 next=3; got {_ids(p2)}/{p2.next_cursor}")


def test_empty_thread(service: AgentMessagingService) -> None:
    empty = service.read_thread_messages(ReadThreadMessagesRequest(thread_id="agt-nope"))
    _check(_ids(empty) == [] and empty.next_cursor == 0, f"unknown thread → empty page, next_cursor echoes 0; got {empty}")


def test_ownership_bypass_discriminator(service: AgentMessagingService) -> None:
    """THE security-relevant proof: read_thread_messages reads agt-1 (owned by
    'bridge-owner') with NO bridge_id, while list_messages from a NON-owning
    bridge RAISES — read_thread_messages deliberately bypasses the gate."""
    unscoped = service.read_thread_messages(ReadThreadMessagesRequest(thread_id="agt-1"))
    _check(_ids(unscoped) == ["agm-_001", "agm-_002", "agm-_003"], "read_thread_messages returns agt-1 messages with NO ownership check")
    raised = False
    try:
        service.list_messages(
            ListAgentMessagesRequest(bridge_id="bridge-other", thread_id="agt-1"),
        )
    except Exception:  # noqa: BLE001 — AgentThreadUnauthorizedError; import-light assertion
        raised = True
    _check(raised, "list_messages from a NON-owning bridge RAISES (the gate read_thread_messages bypasses)")


def main() -> int:
    if os.environ.get("READ_THREAD_MESSAGES_LIVE_SMOKE") != "1":
        print("=== read_thread_messages_live_smoke ===")
        print("  SKIP  set READ_THREAD_MESSAGES_LIVE_SMOKE=1 to run; needs the live solet DB.")
        return 0
    print("=== read_thread_messages_live_smoke ===")
    schema_name = f"example_test_readthreadmsg_{secrets.token_hex(3)}"
    provider = PostgresProvider(_load_pg_config(schema_name))
    provider.initialize()
    try:
        _create_tables(provider)
        _seed_thread(provider, schema_name, tid="agt-1", owner_bridge="bridge-owner")
        _seed_thread(provider, schema_name, tid="agt-2", owner_bridge="bridge-owner")
        _seed_message(provider, schema_name, mid="agm-_001", thread_id="agt-1", cursor=1, text="hello-1")
        _seed_message(provider, schema_name, mid="agm-_002", thread_id="agt-1", cursor=2, text="hello-2")
        _seed_message(provider, schema_name, mid="agm-_003", thread_id="agt-1", cursor=3, text="hello-3")
        _seed_message(provider, schema_name, mid="agm-_004", thread_id="agt-2", cursor=1, text="other")
        service = _service(provider)
        test_unscoped_read_and_isolation(service)
        test_int_cursor_pagination(service)
        test_empty_thread(service)
        test_ownership_bypass_discriminator(service)
    finally:
        with provider.get_transactional_connection() as conn, conn.cursor() as cur:
            cur.execute(cast(LiteralString, f'DROP SCHEMA IF EXISTS "{schema_name}" CASCADE'))

    print(f"\n{_passed} passed, {len(_failed)} failed")
    if _failed:
        for label in _failed:
            print(f"  FAILED: {label}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
