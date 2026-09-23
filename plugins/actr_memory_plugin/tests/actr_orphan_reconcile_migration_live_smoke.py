#!/usr/bin/env python3
"""Live-Postgres behavioral smoke for the actr orphan-reconcile migration (sub-slice A).

Pins the migrated ``ACTRMemoryBackend._find_orphaned_memories`` against a REAL
``PostgresProvider`` end-to-end. The raw ``memory ⋈ pgvector__embeddings``
LEFT-JOIN anti-join is now the owning-service path (D1/GAP-5):

  get_all_memories(status='active')  [own-ns read_state]
    → Python is_deleted-in-{None,0} filter (the GAP-9 OR-predicate; no backfill)
    → vector_service.find_missing_external_ids(memory_ids)  [the LANDED verb]
    → memories whose id has no ACTIVE vector = orphaned → reindex candidates.

WHY end-to-end (both tables + the REAL find_missing, not a stub): the slice's
load-bearing new seam is the ``data.result.missing`` extraction off the REAL
vector_service verb (the prod-breaking class [[prove extraction vs real plugin]]).
So the backend's ``vector_service`` here is the genuine ``PGVectorServicePlugin``
→ ``PGVectorProvider.find_missing_external_ids`` reading the real embeddings
table via ``read_state`` (the same =ANY path STUB-1 proved), NOT a hand-built
envelope.

Seed (one throwaway schema, DROPped in finally):
  memory:     m1,m2 active+vectored · m3 active+NO-vector (the orphan) ·
              m4 archived+no-vector (excluded by status) ·
              m5 active+is_deleted=1 (excluded by the Python filter)
  embeddings: external_id ∈ {m1,m2} (is_deleted=0)
Expected _find_orphaned_memories() == [{id:m3, content:...}]:
  m1/m2 have active vectors (not missing); m4 is not status='active'; m5 is
  is_deleted=1. ``created_at`` is platform-read-only — never written (seed omits
  it; the DB default applies).

Env-gated behind ``ACTR_ORPHAN_RECONCILE_LIVE_SMOKE=1`` (needs the live DB). Run::

    ACTR_ORPHAN_RECONCILE_LIVE_SMOKE=1 \\
      .venv/bin/python3 plugins/actr_memory_plugin/tests/actr_orphan_reconcile_migration_live_smoke.py
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
sys.path.insert(0, str(REPO_ROOT / "plugins" / "pgvector_service_plugin" / "src"))
sys.path.insert(0, str(REPO_ROOT / "plugins" / "actr_memory_plugin" / "src"))

importlib.import_module("ananta.core.config.config_manager")
from actr_memory_plugin.backend import ACTRMemoryBackend  # noqa: E402
from pgvector_service_plugin.plugin import PGVectorServicePlugin  # noqa: E402
from pgvector_service_plugin.postgres_backend.vector.provider import (  # noqa: E402
    PGVectorProvider,
)
from postgres_state_management_plugin.postgres_backend.config import (  # noqa: E402
    PostgresConfig,
)
from postgres_state_management_plugin.postgres_backend.provider import (  # noqa: E402
    PostgresProvider,
)

_passed = 0
_failed: list[str] = []

_MEMORY_TABLE = "actr_memory_plugin__memory"
_EMBEDDINGS_TABLE = "pgvector_service_plugin__embeddings"


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
    """``read_state`` over a real provider, routing by (namespace, table) —
    serves BOTH the memory read (get_all_memories) AND the embeddings read
    (find_missing_external_ids), each through the genuine ``provider.select``
    (so the =ANY list-filter on external_id is the real grammar, not a stub)."""

    def __init__(self, provider: PostgresProvider) -> None:
        self._provider = provider

    def read_state(self, namespace: str, query: dict[str, Any]) -> dict[str, Any]:
        table = query.get("table")
        if not isinstance(table, str):
            return {"action_status": "error", "data": None, "error": "bad table"}
        filters = cast("dict[str, Any]", query.get("filters") or {})
        limit = query.get("limit")
        try:
            rows = self._provider.select(
                namespace=namespace,
                table=table,
                conditions=filters,
                limit=cast("int | None", limit),
            )
        except (psycopg.Error, OSError, RuntimeError, ValueError) as exc:
            return {"action_status": "error", "data": None, "error": str(exc)}
        return {
            "action_status": "completed",
            "data": {"namespace": namespace, "records": rows},
            "error": None,
            "timestamp": "",
        }


_MEMORY_DDL = (
    "id text PRIMARY KEY, namespace text NOT NULL, content text NOT NULL, "
    "status text NOT NULL, tags text, retrieval_times text, source_memory_ids text, "
    "is_deleted integer NOT NULL DEFAULT 0, "
    "created_at timestamp NOT NULL DEFAULT (NOW() AT TIME ZONE 'UTC'), "
    "updated_at timestamp NOT NULL DEFAULT (NOW() AT TIME ZONE 'UTC')"
)
_EMBEDDINGS_DDL = (
    "id text PRIMARY KEY, external_id text, "
    "is_deleted integer NOT NULL DEFAULT 0, "
    "created_at timestamp NOT NULL DEFAULT (NOW() AT TIME ZONE 'UTC'), "
    "updated_at timestamp NOT NULL DEFAULT (NOW() AT TIME ZONE 'UTC')"
)


def _create_tables(provider: PostgresProvider, schema: str) -> None:
    with provider.get_transactional_connection() as conn, conn.cursor() as cur:
        cur.execute(cast(LiteralString, f'CREATE TABLE "{schema}"."{_MEMORY_TABLE}" ({_MEMORY_DDL})'))
        cur.execute(cast(LiteralString, f'CREATE TABLE "{schema}"."{_EMBEDDINGS_TABLE}" ({_EMBEDDINGS_DDL})'))


def _seed_memory(
    provider: PostgresProvider, schema: str, *, mid: str, content: str, status: str, is_deleted: int,
) -> None:
    with provider.get_transactional_connection() as conn, conn.cursor() as cur:
        cur.execute(
            cast(
                LiteralString,
                f'INSERT INTO "{schema}"."{_MEMORY_TABLE}" '
                "(id, namespace, content, status, tags, is_deleted) "
                "VALUES (%s, %s, %s, %s, %s, %s)",
            ),
            (mid, "actr_memory_plugin", content, status, "[]", is_deleted),
        )


def _seed_vector(provider: PostgresProvider, schema: str, *, vid: str, external_id: str) -> None:
    with provider.get_transactional_connection() as conn, conn.cursor() as cur:
        cur.execute(
            cast(
                LiteralString,
                f'INSERT INTO "{schema}"."{_EMBEDDINGS_TABLE}" (id, external_id, is_deleted) '
                "VALUES (%s, %s, 0)",
            ),
            (vid, external_id),
        )


def _backend(provider: PostgresProvider) -> ACTRMemoryBackend:
    """Partial-construct the backend with the two attrs _find_orphaned_memories
    touches: state_service (memory read) + vector_service (the REAL pgvector
    plugin → find_missing over the embeddings table)."""
    adapter = _LiveStateAdapter(provider)
    vector_provider = object.__new__(PGVectorProvider)
    vector_provider._state_service = cast("Any", adapter)
    vector_plugin = object.__new__(PGVectorServicePlugin)
    vector_plugin._provider = vector_provider
    backend = object.__new__(ACTRMemoryBackend)
    backend.state_service = cast("Any", adapter)
    backend.vector_service = cast("Any", vector_plugin)
    return backend


def test_find_orphaned_memories(provider: PostgresProvider, schema: str) -> None:
    _seed_memory(provider, schema, mid="m1", content="c1", status="active", is_deleted=0)
    _seed_memory(provider, schema, mid="m2", content="c2", status="active", is_deleted=0)
    _seed_memory(provider, schema, mid="m3", content="c3", status="active", is_deleted=0)
    _seed_memory(provider, schema, mid="m4", content="c4", status="archived", is_deleted=0)
    _seed_memory(provider, schema, mid="m5", content="c5", status="active", is_deleted=1)
    _seed_vector(provider, schema, vid="v1", external_id="m1")
    _seed_vector(provider, schema, vid="v2", external_id="m2")

    orphaned = _backend(provider)._find_orphaned_memories()
    ids = sorted(row["id"] for row in orphaned)
    _check(
        ids == ["m3"],
        f"orphaned = only the active, non-deleted, vector-less memory (m3); got {ids}",
    )
    _check(
        all(row.get("content") for row in orphaned) and orphaned and orphaned[0].get("content") == "c3",
        f"orphaned rows carry {{id, content}} (m3→'c3'); got {orphaned}",
    )
    # Discriminators, made explicit:
    _check("m1" not in ids and "m2" not in ids, "memories WITH an active vector (m1,m2) are NOT orphaned (real find_missing)")
    _check("m4" not in ids, "archived memory (m4) excluded — status='active' filter")
    _check("m5" not in ids, "is_deleted=1 memory (m5) excluded — the GAP-9 Python is_deleted filter")


def test_no_active_memories(provider: PostgresProvider, schema: str) -> None:
    """Empty candidate set short-circuits (no vector_service call needed)."""
    with provider.get_transactional_connection() as conn, conn.cursor() as cur:
        cur.execute(cast(LiteralString, f'DELETE FROM "{schema}"."{_MEMORY_TABLE}" WHERE status = %s'), ("active",))
    _check(_backend(provider)._find_orphaned_memories() == [], "no active memories → [] (short-circuit)")


def main() -> int:
    if os.environ.get("ACTR_ORPHAN_RECONCILE_LIVE_SMOKE") != "1":
        print("=== actr_orphan_reconcile_migration_live_smoke ===")
        print("  SKIP  set ACTR_ORPHAN_RECONCILE_LIVE_SMOKE=1 to run; needs the live solet DB.")
        return 0
    print("=== actr_orphan_reconcile_migration_live_smoke ===")
    schema_name = f"example_test_orphanrecon_{secrets.token_hex(3)}"
    provider = PostgresProvider(_load_pg_config(schema_name))
    provider.initialize()
    try:
        _create_tables(provider, schema_name)
        test_find_orphaned_memories(provider, schema_name)
        test_no_active_memories(provider, schema_name)
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
