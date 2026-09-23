#!/usr/bin/env python3
"""Live-Postgres behavioral smoke for the session_source read_events migration.

Pins ``AgentMessagingSessionSourcePlugin.read_events`` after its SQL-lockdown
rewire (D1/GAP-5): the raw ``SELECT ... FROM core__agent_message WHERE
thread_id=%s AND is_deleted=0 [AND cursor>hw] ORDER BY cursor ASC`` is now a
page-loop over the OWNING agent_messaging ``read_thread_messages`` verb (the
unscoped, int-cursor-paginated message read landed as STUB-4), reached via
``plugin_manager.plugins['agent_messaging_plugin']``.

The session_source plugin's ``_agent_messaging_service()`` here resolves a REAL
``AgentMessagingService`` (over a real ``PostgresProvider`` + the production
``agent_message`` DDL), so ``read_events`` drives the genuine
``read_thread_messages`` → ``repo.list_messages`` → ``query_ordered`` →
``select_ordered`` path end-to-end (not a stub). (The plugin's
read_thread_messages delegation is a 1-line passthrough, so we wire the service
directly into ``plugins['agent_messaging_plugin']`` — same shape as C's
discover_sessions live smoke.)

Covers:
* field mapping AgentMessageRow → RawSessionEvent (thread_id, cursor,
  role.value, kind.value, content=[{type,text}] re-serialized from TextPart,
  error, metadata; event_at=created_at, vendor_event_id=id,
  vendor_parent_event_id=action_id);
* the cursor BASE: a ``cursor_payload=None`` read → after_cursor=0 → returns the
  FIRST message (cursor 1), proving the 1-based-cursor invariant that makes the
  unbounded old scan ≡ ``cursor > 0`` (no off-by-one silent drop);
* cursor RESUME / no re-baseline: a ``{cursor_high_water: N}`` payload (the SAME
  int key the old raw path used — unchanged by the migration) returns strictly
  cursor > N;
* the unbounded→paginated page-loop (forced multi-page via a small page limit),
  yielding every message once, in order, no dup/skip across page boundaries;
* the per-event actor snapshot: message ``metadata`` is surfaced so normalize()
  can read sender_session_label / sender_agent_instance_id.

``created_at`` is platform-read-only — never written; ``cursor`` order is the
contract.

Env-gated behind ``SESSION_SOURCE_READ_EVENTS_LIVE_SMOKE=1``. Run::

    SESSION_SOURCE_READ_EVENTS_LIVE_SMOKE=1 \\
      .venv/bin/python3 plugins/agent_messaging_session_source_plugin/tests/read_events_read_thread_messages_live_smoke.py
"""

from __future__ import annotations

import importlib
import json
import os
import secrets
import sys
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any, LiteralString, cast

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT / "ananta" / "src"))
sys.path.insert(
    0, str(REPO_ROOT / "plugins" / "postgres_state_management_plugin" / "src"),
)
sys.path.insert(
    0,
    str(REPO_ROOT / "plugins" / "agent_messaging_session_source_plugin" / "src"),
)

importlib.import_module("ananta.core.config.config_manager")
from agent_messaging_session_source_plugin import plugin as source_module  # noqa: E402
from agent_messaging_session_source_plugin.plugin import (  # noqa: E402
    AgentMessagingSessionSourcePlugin,
)
from ananta.llm.agent_messaging.repository import (  # noqa: E402
    AgentMessagingRepository,
)
from ananta.llm.agent_messaging.schema import (  # noqa: E402
    NAMESPACE,
    get_agent_messaging_schema,
)
from ananta.llm.agent_messaging.service import AgentMessagingService  # noqa: E402
from ananta.llm.session_ledger.types import ExternalSessionRef  # noqa: E402
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
from psycopg.types.json import Json  # noqa: E402

_passed = 0
_failed: list[str] = []

_MESSAGE_TABLE = "core__agent_message"
_ROOT = "local:agent_messaging"
_THREAD_ID = "agt-_rd01"

# 5 messages, cursors 1..5 — varied role/kind to exercise the full mapping.
# ids are hex (agm_<hex>), matching real message ids: list_messages keeps
# ``cursor > after_cursor`` strict via a HEX-aware ``agm_g`` after-sentinel
# ('g' > any uuid-hex char), so a non-hex id would defeat the boundary fence
# and re-emit the boundary row (caught here when first-draft ids used 'm').
_SEED: tuple[dict[str, Any], ...] = (
    {
        "id": "agm_00000001", "cursor": 1, "role": "originator", "kind": "message",
        "content": [{"type": "text", "text": "hello"}], "error": None,
        "action_id": None,
        "metadata": {"sender_session_label": "Claude-A", "sender_agent_instance_id": "agi-aaa"},
    },
    {
        "id": "agm_00000002", "cursor": 2, "role": "agent", "kind": "message",
        "content": [{"type": "text", "text": "hi back"}], "error": None,
        "action_id": "ae-_x2",
        "metadata": {"sender_session_label": "Codex", "sender_agent_instance_id": "agi-bbb"},
    },
    {
        "id": "agm_00000003", "cursor": 3, "role": "system", "kind": "error",
        "content": [], "error": {"code": "boom", "message": "kaboom"},
        "action_id": None, "metadata": {},
    },
    {
        "id": "agm_00000004", "cursor": 4, "role": "agent", "kind": "result",
        "content": [{"type": "text", "text": "tool output"}], "error": None,
        "action_id": "ae-_r4", "metadata": {},
    },
    {
        "id": "agm_00000005", "cursor": 5, "role": "agent", "kind": "message",
        "content": [{"type": "text", "text": "last"}], "error": None,
        "action_id": None, "metadata": {},
    },
)
_ALL_CURSORS = [1, 2, 3, 4, 5]


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
    """query_ordered over a real provider through the PRODUCTION path
    (parse_ordered_query → select_ordered WITH after + include_deleted) —
    the exact path repo.list_messages drives."""

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


def _seed_messages(provider: PostgresProvider, schema: str) -> None:
    """Insert the message rows directly (read-only ``created_at`` omitted, DB
    default NOW() applies; cursors are explicit so the order contract is
    checkable). ``content``/``error``/``metadata`` are jsonb (psycopg ``Json``)."""
    with provider.get_transactional_connection() as conn, conn.cursor() as cur:
        for row in _SEED:
            cur.execute(
                cast(
                    LiteralString,
                    f'INSERT INTO "{schema}"."{_MESSAGE_TABLE}" '
                    "(id, namespace, thread_id, cursor, role, kind, content, error, "
                    "action_id, metadata, is_deleted) "
                    "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,0)",
                ),
                (
                    row["id"], NAMESPACE, _THREAD_ID, row["cursor"], row["role"], row["kind"],
                    Json(row["content"]),
                    Json(row["error"]) if row["error"] is not None else None,
                    row["action_id"], Json(row["metadata"]),
                ),
            )


def _source(provider: PostgresProvider) -> AgentMessagingSessionSourcePlugin:
    adapter = _LiveStateAdapter(provider)
    service = object.__new__(AgentMessagingService)
    service._repo = AgentMessagingRepository(cast("Any", adapter))
    service._config = cast("Any", SimpleNamespace(enabled=True))
    orchestrator = cast(
        "Any",
        SimpleNamespace(
            plugin_manager=SimpleNamespace(plugins={"agent_messaging_plugin": service}),
        ),
    )
    source = object.__new__(AgentMessagingSessionSourcePlugin)
    source.name = "agent_messaging_session_source_plugin"
    source.orchestrator_ref = orchestrator
    return source


def _ref() -> ExternalSessionRef:
    return ExternalSessionRef(
        external_session_id=_THREAD_ID,
        vendor_session_label="Demo",
        project_path=None,
        # read_events ignores ref timing; a concrete datetime satisfies the DTO.
        first_seen_at=datetime(2026, 6, 22, tzinfo=UTC),
    )


def _read_cursors(
    source: AgentMessagingSessionSourcePlugin, payload: dict[str, object] | None,
) -> list[int]:
    return [int(cast(int, e.payload["cursor"])) for e in source.read_events(_ROOT, _ref(), payload)]


def test_cursor_base_reads_from_first(source: AgentMessagingSessionSourcePlugin) -> None:
    # cursor_payload=None → after_cursor=0 → the FIRST message (cursor 1) is
    # returned: the 1-based-cursor invariant (no off-by-one drop).
    cursors = _read_cursors(source, None)
    _check(
        cursors == _ALL_CURSORS,
        f"read_events(None) yields all messages from the FIRST (cursor 1), in order; got {cursors}",
    )


def test_field_mapping(source: AgentMessagingSessionSourcePlugin) -> None:
    raws = list(source.read_events(_ROOT, _ref(), None))
    first = raws[0]
    # Full-payload equality proves the faithful AgentMessageRow→payload mapping
    # AND that content is a re-serialized list[dict] (TextPart→{type,text}), not
    # TextPart objects — exactly the list-shaped 'content' normalize() consumes.
    _check(
        first.payload == {
            "thread_id": _THREAD_ID,
            "cursor": 1,
            "role": "originator",
            "kind": "message",
            "content": [{"type": "text", "text": "hello"}],
            "error": None,
            "metadata": {
                "sender_session_label": "Claude-A",
                "sender_agent_instance_id": "agi-aaa",
            },
        },
        f"m1 payload maps faithfully; got {first.payload}",
    )
    _check(first.vendor_event_id == "agm_00000001", "id → vendor_event_id")
    _check(first.vendor_parent_event_id is None, "null action_id → vendor_parent_event_id None")
    _check(raws[1].vendor_parent_event_id == "ae-_x2", "action_id → vendor_parent_event_id")
    # event_at must stay tz-AWARE: the old raw path stamped naive datetimes UTC,
    # and read_thread_messages' repo._coerce_datetime passes datetimes through
    # unchanged. created_at comes back NAIVE (DATETIME → TIMESTAMP, stored
    # NOW() AT TIME ZONE 'UTC'), so read_events re-stamps UTC via _as_utc to keep
    # event_at tz-aware. Asserted against the live DB so a naive regression can't
    # slip past a UTC-environment blind spot.
    _check(first.event_at.tzinfo is not None, "event_at is tz-aware (no naive-datetime regression)")
    _check(
        raws[2].payload["error"] == {"code": "boom", "message": "kaboom"},
        f"kind='error' row carries the structured error dict; got {raws[2].payload['error']}",
    )


def test_cursor_resume_no_rebaseline(source: AgentMessagingSessionSourcePlugin) -> None:
    # The cursor key is the SAME int ``cursor_high_water`` the old raw path used
    # — unchanged by the migration — so a persisted payload resumes identically
    # (no re-baseline, unlike discover_sessions whose token FORMAT changed).
    after = _read_cursors(source, {"cursor_high_water": 2})
    _check(after == [3, 4, 5], f"resume after cursor_high_water=2 → strictly cursor>2; got {after}")
    drained = _read_cursors(source, {"cursor_high_water": 5})
    _check(drained == [], f"resume after the last cursor → drained; got {drained}")


def test_multi_page_loop(source: AgentMessagingSessionSourcePlugin) -> None:
    original = source_module._MESSAGE_PAGE_LIMIT
    source_module._MESSAGE_PAGE_LIMIT = 2  # force 3 pages over 5 messages
    try:
        cursors = _read_cursors(source, None)
    finally:
        source_module._MESSAGE_PAGE_LIMIT = original
    _check(
        cursors == _ALL_CURSORS,
        f"page-loop (limit=2) yields all 5 once, in cursor order, no dup/skip; got {cursors}",
    )
    _check(len(cursors) == len(set(cursors)), "no duplicate across page boundaries")


def test_actor_snapshot_metadata(source: AgentMessagingSessionSourcePlugin) -> None:
    raws = list(source.read_events(_ROOT, _ref(), None))
    meta = cast(dict[str, object], raws[0].payload["metadata"])
    _check(
        meta.get("sender_session_label") == "Claude-A"
        and meta.get("sender_agent_instance_id") == "agi-aaa",
        f"per-event metadata surfaced for the actor snapshot; got {meta}",
    )


def main() -> int:
    if os.environ.get("SESSION_SOURCE_READ_EVENTS_LIVE_SMOKE") != "1":
        print("=== read_events_read_thread_messages_live_smoke ===")
        print("  SKIP  set SESSION_SOURCE_READ_EVENTS_LIVE_SMOKE=1 to run; needs the live solet DB.")
        return 0
    print("=== read_events_read_thread_messages_live_smoke ===")
    schema_name = f"example_test_read_events_{secrets.token_hex(3)}"
    provider = PostgresProvider(_load_pg_config(schema_name))
    provider.initialize()
    try:
        _create_tables(provider)
        _seed_messages(provider, schema_name)
        source = _source(provider)
        test_cursor_base_reads_from_first(source)
        test_field_mapping(source)
        test_cursor_resume_no_rebaseline(source)
        test_multi_page_loop(source)
        test_actor_snapshot_metadata(source)
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
