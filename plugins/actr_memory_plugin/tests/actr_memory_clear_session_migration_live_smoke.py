#!/usr/bin/env python3
"""Live-Postgres behavioral smoke for actr_memory Slice-A0 SQL-lockdown migration.

Pins the migrated ``clear_session_memory`` site (``backend.py``) against a REAL
``PostgresProvider``. The raw ``DELETE FROM core__memory_events WHERE
session_id=? [AND source_namespace=?]`` (a HARD delete) is now a hard
``delete_records`` (``soft_delete=False``) on the ``core`` namespace:

* ``clear_session_memory(session_id[, namespace_filter])`` →
  ``delete_records(core, memory_events, {session_id[, source_namespace]},
  soft_delete=False)`` — fail-fast (raises on a non-completed envelope), returns
  ``deleted_count`` from ``data.result.deleted``.

The REAL method is driven through a partial-constructed backend (``object.__new__``
+ the single ``state_service`` attribute it touches) over a state adapter that
mirrors the plugin's ``delete_records`` 1:1 (``provider.delete`` + the same
``psycopg.Error → error envelope`` wrap). Hard-delete is verified by raw
read-back (the rows are physically absent, not is_deleted=1).

Also EMPIRICALLY pins the empty-filter hard-``delete_records`` FAIL-SAFE: a hard
``delete_records`` with an EMPTY filter composes ``DELETE FROM <t> WHERE`` (no
predicate) → invalid SQL → an error envelope. ``delete_records`` deliberately
cannot express an unconditional hard delete-all (a sound guard against an
accidental whole-table wipe). The former clear-all site this once blocked
(``_delete_all_short_term_events``) was REMOVED entirely under the D8 operator
decision (2026-06-21) — purge no longer bulk-wipes the core interaction log — so
no delete-all primitive is needed; this test guards that the fail-safe stays.

Env-gated behind ``ACTR_CLEAR_SESSION_MIGRATION_LIVE_SMOKE=1`` (needs the live DB
up; own throwaway schema).

Run::

    ACTR_CLEAR_SESSION_MIGRATION_LIVE_SMOKE=1 \\
      .venv/bin/python3 plugins/actr_memory_plugin/tests/actr_memory_clear_session_migration_live_smoke.py
"""

from __future__ import annotations

import importlib
import json
import os
import secrets
import sys
from pathlib import Path
from typing import Any, LiteralString, cast

import psycopg

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT / "ananta" / "src"))
sys.path.insert(
    0, str(REPO_ROOT / "plugins" / "postgres_state_management_plugin" / "src"),
)
sys.path.insert(
    0, str(REPO_ROOT / "plugins" / "actr_memory_plugin" / "src"),
)

# Pre-load config_manager (via importlib so the import sorter can't reorder it
# after the backend) to cache the deep plugin_contracts chain before
# ``ananta.utils`` initializes — avoids the utils↔config circular import when the
# backend is imported standalone.
importlib.import_module("ananta.core.config.config_manager")
from actr_memory_plugin.backend import ACTRMemoryBackend  # noqa: E402
from postgres_state_management_plugin.postgres_backend.config import (  # noqa: E402
    PostgresConfig,
)
from postgres_state_management_plugin.postgres_backend.provider import (  # noqa: E402
    PostgresProvider,
)

_passed = 0
_failed: list[str] = []


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


class _LiveStateAdapter:
    """Mirrors the postgres plugin's ``delete_records`` 1:1: ``provider.delete``
    (soft_delete defaults True; hard when False), wrapped in the same
    ``psycopg.Error → error envelope`` as ``plugin.delete_records``."""

    def __init__(self, provider: PostgresProvider) -> None:
        self._provider = provider

    def delete_records(self, namespace: str, query: dict[str, Any]) -> dict[str, Any]:
        table = query.get("table")
        if not isinstance(table, str):
            return {"action_status": "error", "data": None, "actions": [], "error": "bad table"}
        filters = cast("dict[str, Any]", query.get("filters") or {})
        soft_delete = bool(query.get("soft_delete", True))
        try:
            deleted = self._provider.delete(
                namespace=namespace,
                table=table,
                conditions=filters,
                soft_delete=soft_delete,
            )
        except (psycopg.Error, OSError, RuntimeError, ValueError) as exc:
            return {"action_status": "error", "data": None, "actions": [], "error": str(exc)}
        return {
            "action_status": "completed",
            "data": {"namespace": namespace, "result": {"deleted": deleted, "soft_delete": soft_delete}},
            "actions": [],
            "error": None,
            "timestamp": "",
        }


def _backend(provider: PostgresProvider) -> ACTRMemoryBackend:
    """Partial-construct the backend with only the ``state_service`` attribute
    ``clear_session_memory`` touches."""
    backend = object.__new__(ACTRMemoryBackend)
    backend.state_service = cast("Any", _LiveStateAdapter(provider))
    return backend


_SEED_AT = "2026-06-01T00:00:00"

# Mirrors ananta/src/ananta/config/core_schemas.py get_memory_events_schema
# (business columns) + the auto-injected standard fields.
_DDL = (
    "id text PRIMARY KEY, session_id text NOT NULL, source_namespace text NOT NULL, "
    "event_type text NOT NULL, content text NOT NULL, metadata text, "
    'timestamp timestamp NOT NULL, '
    "is_deleted integer NOT NULL DEFAULT 0, "
    "created_at timestamp NOT NULL, updated_at timestamp NOT NULL"
)
_TABLE = "core__memory_events"


def _create_table(provider: PostgresProvider, schema: str) -> None:
    with provider.get_transactional_connection() as conn, conn.cursor() as cur:
        cur.execute(cast(LiteralString, f'CREATE TABLE "{schema}"."{_TABLE}" ({_DDL})'))


def _seed_event(
    provider: PostgresProvider, schema: str, *, eid: str, session_id: str, source_namespace: str,
) -> None:
    with provider.get_transactional_connection() as conn, conn.cursor() as cur:
        cur.execute(
            cast(
                LiteralString,
                f'INSERT INTO "{schema}"."{_TABLE}" '
                "(id, session_id, source_namespace, event_type, content, timestamp, "
                "is_deleted, created_at, updated_at) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
            ),
            (eid, session_id, source_namespace, "user_input", "hi", _SEED_AT, 0, _SEED_AT, _SEED_AT),
        )


def _count(provider: PostgresProvider, schema: str, *, session_id: str | None = None) -> int:
    if session_id is None:
        rows = provider.execute_query(f'SELECT COUNT(*) FROM "{schema}"."{_TABLE}"')
    else:
        rows = provider.execute_query(
            f'SELECT COUNT(*) FROM "{schema}"."{_TABLE}" WHERE session_id = %s', (session_id,)
        )
    return int(cast("Any", rows[0][0])) if rows else -1


def test_clear_session_memory_hard_delete(provider: PostgresProvider, schema: str) -> None:
    """clear_session_memory hard-deletes the session's events (physically gone),
    returns the count, and leaves other sessions untouched."""
    _seed_event(provider, schema, eid="e-a1", session_id="sess-A", source_namespace="ns1")
    _seed_event(provider, schema, eid="e-a2", session_id="sess-A", source_namespace="ns2")
    _seed_event(provider, schema, eid="e-b1", session_id="sess-B", source_namespace="ns1")
    result = _backend(provider).clear_session_memory("sess-A")
    _check(result.get("data", {}).get("deleted_count") == 2,
           f"clear_session_memory returned deleted_count=2; got {result.get('data', {}).get('deleted_count')}")
    _check(_count(provider, schema, session_id="sess-A") == 0,
           "session A events HARD-deleted (physically absent, not is_deleted=1)")
    _check(_count(provider, schema, session_id="sess-B") == 1,
           "session B untouched (session_id= filter scoped the delete)")


def test_clear_session_memory_namespace_filter(provider: PostgresProvider, schema: str) -> None:
    """namespace_filter adds source_namespace to the delete filters (one ns only)."""
    _seed_event(provider, schema, eid="e-c1", session_id="sess-C", source_namespace="ns1")
    _seed_event(provider, schema, eid="e-c2", session_id="sess-C", source_namespace="ns2")
    result = _backend(provider).clear_session_memory("sess-C", namespace_filter="ns1")
    _check(result.get("data", {}).get("deleted_count") == 1,
           "namespace_filter scoped the delete to source_namespace=ns1 (count=1)")
    rows = provider.execute_query(
        f'SELECT source_namespace FROM "{schema}"."{_TABLE}" WHERE session_id = %s', ("sess-C",)
    )
    _check([r[0] for r in rows] == ["ns2"], "only the ns2 event survives the namespace-filtered clear")


def test_clear_session_memory_empty_session_raises(provider: PostgresProvider, schema: str) -> None:
    """Empty session_id raises (validation preserved)."""
    _ = schema
    raised = False
    try:
        _backend(provider).clear_session_memory("")
    except Exception:  # noqa: BLE001 — FrameworkError; import-light assertion
        raised = True
    _check(raised, "clear_session_memory('') RAISES on the empty-session-id guard")


def test_empty_filter_hard_delete_is_rejected(provider: PostgresProvider, schema: str) -> None:
    """FAIL-SAFE GUARD: a hard delete_records with an EMPTY filter composes
    'DELETE FROM <t> WHERE' (no predicate) → invalid SQL → error envelope.
    delete_records deliberately cannot express an unconditional hard delete-all
    (guards against an accidental whole-table wipe). The former clear-all caller
    (_delete_all_short_term_events) was removed under D8, so nothing needs delete-all."""
    _ = schema
    result = _LiveStateAdapter(provider).delete_records(
        namespace="core",
        query={"table": "memory_events", "filters": {}, "soft_delete": False},
    )
    _check(result.get("action_status") != "completed",
           "empty-filter HARD delete_records FAILS (no-predicate DELETE) — the delete-all fail-safe holds")


def main() -> int:
    if os.environ.get("ACTR_CLEAR_SESSION_MIGRATION_LIVE_SMOKE") != "1":
        print("=== actr_memory_clear_session_migration_live_smoke ===")
        print("  SKIP  set ACTR_CLEAR_SESSION_MIGRATION_LIVE_SMOKE=1 to run; needs the live solet DB.")
        return 0
    print("=== actr_memory_clear_session_migration_live_smoke ===")
    schema_name = f"example_test_actrclr_{secrets.token_hex(3)}"
    provider = PostgresProvider(_load_pg_config(schema_name))
    provider.initialize()
    try:
        _create_table(provider, schema_name)
        test_clear_session_memory_hard_delete(provider, schema_name)
        test_clear_session_memory_namespace_filter(provider, schema_name)
        test_clear_session_memory_empty_session_raises(provider, schema_name)
        test_empty_filter_hard_delete_is_rejected(provider, schema_name)
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
