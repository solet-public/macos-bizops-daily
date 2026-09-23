#!/usr/bin/env python3
"""Live-Postgres behavioral smoke for the session_source discover_sessions migration.

Pins ``AgentMessagingSessionSourcePlugin.discover_sessions`` after its
SQL-lockdown rewire (D1/GAP-5): the raw ``SELECT ... FROM core__agent_thread
WHERE created_at > hw ORDER BY created_at`` is now a page-loop over the OWNING
agent_messaging ``list_threads`` verb (the unscoped global enumeration I landed
as STUB-2), reached via ``plugin_manager.plugins['agent_messaging_plugin']``.

The session_source plugin's ``_agent_messaging_service()`` here resolves a REAL
``AgentMessagingService`` (over a real ``PostgresProvider`` + the production
``agent_thread`` DDL), so ``discover_sessions`` drives the genuine
``list_threads`` → ``query_ordered`` → ``select_ordered`` path end-to-end (not a
stub). (The plugin's list_threads delegation is a 1-line passthrough already
covered by STUB-2's smoke, so we wire the service directly into
``plugins['agent_messaging_plugin']``.)

Covers:
* field mapping AgentThreadRow → ExternalSessionRef (id, title, working_directory,
  first_seen_at, + the 4 per-peer snapshot labels);
* the opaque-cursor round-trip + TIE-SAFETY: session_discovery_cursor
  reconstructs list_threads' (created_at, id) token from the last ref; feeding
  it back resumes correctly ACROSS a same-created_at boundary (the old
  created_at-only high-water would have dropped the tied sibling);
* the unbounded→paginated page-loop (forced multi-page via a small page limit);
* the re-baseline: a pre-migration ``{created_at_high_water_iso}`` payload →
  full re-walk;
* malformed cursor → ValueError (the interface contract).

``created_at`` is platform-read-only — never written; tied/distinct timestamps
come from NOW() transaction-stability (one txn = tied group; later txn = later).

Env-gated behind ``SESSION_SOURCE_DISCOVER_LIVE_SMOKE=1``. Run::

    SESSION_SOURCE_DISCOVER_LIVE_SMOKE=1 \\
      .venv/bin/python3 plugins/agent_messaging_session_source_plugin/tests/discover_sessions_list_threads_live_smoke.py
"""

from __future__ import annotations

import importlib
import json
import os
import secrets
import sys
import time
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
_ROOT = "local:agent_messaging"
_T1 = ("agt-_t01", "agt-_t02", "agt-_t03")  # one txn → tied created_at
_T2 = ("agt-_t04", "agt-_t05")              # later txn → strictly-greater created_at
_ALL = ["agt-_t01", "agt-_t02", "agt-_t03", "agt-_t04", "agt-_t05"]


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
    (parse_ordered_query → select_ordered WITH after + include_deleted)."""

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


def _create_thread_table(provider: PostgresProvider) -> None:
    schema = SchemaStandardizer().standardize_schema(get_agent_messaging_schema())
    schema_name = provider.config.schema_name
    ops = [
        op
        for table in schema.tables.values()
        for op in emit_create_table_ops(NAMESPACE, table, schema_name)
    ]
    with provider.get_transactional_connection() as conn, conn.cursor() as cur:
        provider.apply_schema_change_ops(cur, schema, ops)


def _seed_group(provider: PostgresProvider, schema: str, ids: tuple[str, ...]) -> None:
    """Insert ids in ONE txn so they share the transaction-stable NOW()
    created_at (read-only field — omitted, DB default applies). Per-thread
    title/working_directory/labels are set so the ref mapping is checkable."""
    with provider.get_transactional_connection() as conn, conn.cursor() as cur:
        for tid in ids:
            suffix = tid[-2:]
            cur.execute(
                cast(
                    LiteralString,
                    f'INSERT INTO "{schema}"."{_THREAD_TABLE}" '
                    "(id, namespace, originator_type, target_backend, target_plugin_name, "
                    "status, title, working_directory, originator_session_label, "
                    "originator_agent_instance_id, recipient_session_label, "
                    "recipient_agent_instance_id, is_deleted) "
                    "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,0)",
                ),
                (
                    tid, "core", "mcp_bridge", "peer:claude_code",
                    "agent_messaging_plugin", "open",
                    f"title-{suffix}", f"/wd/{suffix}",
                    f"orig-label-{suffix}", f"agi-orig-{suffix}",
                    f"recip-label-{suffix}", f"agi-recip-{suffix}",
                ),
            )


def _seed(provider: PostgresProvider, schema: str) -> None:
    _seed_group(provider, schema, _T1)
    time.sleep(0.01)  # strictly-later NOW() for T2 (deterministic, not a flake)
    _seed_group(provider, schema, _T2)


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


def _discover_ids(source: AgentMessagingSessionSourcePlugin, payload: dict[str, object] | None) -> list[str]:
    return [ref.external_session_id for ref in source.discover_sessions(_ROOT, payload)]


def test_field_mapping_and_order(source: AgentMessagingSessionSourcePlugin) -> None:
    refs = list(source.discover_sessions(_ROOT, None))
    ids = [r.external_session_id for r in refs]
    _check(ids == _ALL, f"discover_sessions(None) yields all threads in (created_at,id) order; got {ids}")
    first = refs[0]
    _check(
        first.vendor_session_label == "title-01"
        and first.project_path == "/wd/01"
        and first.originator_session_label == "orig-label-01"
        and first.originator_agent_instance_id == "agi-orig-01"
        and first.recipient_session_label == "recip-label-01"
        and first.recipient_agent_instance_id == "agi-recip-01",
        f"AgentThreadRow→ExternalSessionRef field mapping (t01); got {first}",
    )


def test_cursor_roundtrip_tie_safety(source: AgentMessagingSessionSourcePlugin) -> None:
    refs = list(source.discover_sessions(_ROOT, None))
    by_id = {r.external_session_id: r for r in refs}
    # Resume AFTER t02 — t02 and t03 share T1 created_at. The reconstructed
    # opaque (created_at,id) cursor must return t03 (NOT drop the tied sibling).
    payload = source.session_discovery_cursor(_ROOT, by_id["agt-_t02"])
    _check("thread_cursor" in payload and isinstance(payload["thread_cursor"], str),
           f"session_discovery_cursor reconstructs an opaque thread_cursor; got {payload}")
    after = _discover_ids(source, payload)
    _check(
        after == ["agt-_t03", "agt-_t04", "agt-_t05"],
        f"resume after t02 returns t03 (TIED at T1, not dropped) + t04,t05; got {after}",
    )
    # Resume after the last ref → drained.
    drained = _discover_ids(source, source.session_discovery_cursor(_ROOT, by_id["agt-_t05"]))
    _check(drained == [], f"resume after the last thread → drained; got {drained}")


def test_multi_page_loop(source: AgentMessagingSessionSourcePlugin) -> None:
    original = source_module._DISCOVER_PAGE_LIMIT
    source_module._DISCOVER_PAGE_LIMIT = 2  # force 3 pages over 5 threads
    try:
        ids = _discover_ids(source, None)
    finally:
        source_module._DISCOVER_PAGE_LIMIT = original
    _check(ids == _ALL, f"page-loop (limit=2) yields all 5 once, in order, no dup/skip; got {ids}")
    _check(len(ids) == len(set(ids)), "no duplicate across page boundaries")


def test_rebaseline_old_payload(source: AgentMessagingSessionSourcePlugin) -> None:
    legacy: dict[str, object] = {"created_at_high_water_iso": "2026-06-01T00:00:00"}
    _check(
        _discover_ids(source, legacy) == _ALL,
        "pre-migration {created_at_high_water_iso} payload → full re-walk (no thread_cursor key)",
    )


def test_malformed_cursor(source: AgentMessagingSessionSourcePlugin) -> None:
    raised = False
    malformed: dict[str, object] = {"thread_cursor": 123}
    try:
        _discover_ids(source, malformed)
    except ValueError:
        raised = True
    _check(raised, "non-string thread_cursor → ValueError (interface malformed-cursor contract)")


def main() -> int:
    if os.environ.get("SESSION_SOURCE_DISCOVER_LIVE_SMOKE") != "1":
        print("=== discover_sessions_list_threads_live_smoke ===")
        print("  SKIP  set SESSION_SOURCE_DISCOVER_LIVE_SMOKE=1 to run; needs the live solet DB.")
        return 0
    print("=== discover_sessions_list_threads_live_smoke ===")
    schema_name = f"example_test_discover_{secrets.token_hex(3)}"
    provider = PostgresProvider(_load_pg_config(schema_name))
    provider.initialize()
    try:
        _create_thread_table(provider)
        _seed(provider, schema_name)
        source = _source(provider)
        test_field_mapping_and_order(source)
        test_cursor_roundtrip_tie_safety(source)
        test_multi_page_loop(source)
        test_rebaseline_old_payload(source)
        test_malformed_cursor(source)
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
