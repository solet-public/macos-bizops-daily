#!/usr/bin/env python3
"""Live-Postgres smoke for the actr memory HARD-DELETE slice (2026-06-21).

Pins three things against a REAL ``PostgresProvider`` (sandbox schema):

1. The one-shot MIGRATION core (``migrations/purge_soft_deleted_actr_rows.py``):
   seed mixed ``is_deleted`` 0/1 rows across ``memory`` / ``memorization`` /
   ``focus_buffer`` → ``purge_soft_deleted`` HARD-deletes only the ``is_deleted=1``
   rows (physically gone) and leaves the ``is_deleted=0`` rows; idempotent
   (re-run deletes 0).
2. Part 1 (flip): ``delete_memory_records`` (memory_store.py) now hard-deletes —
   the row is PHYSICALLY absent, not flagged ``is_deleted=1``. RED-GREEN: before
   the ``soft_delete=False`` flip this row survives as ``is_deleted=1``.
3. Part 3 (read-filter removal): ``get_memory`` no longer filters ``is_deleted`` —
   it returns a row by id regardless of the flag (RED-GREEN: the old
   ``is_deleted=0`` filter hid an ``is_deleted=1`` row), and returns None once
   the row is hard-deleted.

The state adapter mirrors the postgres plugin 1:1 (``delete_records`` →
``provider.delete``; ``read_state`` → ``provider.select``). Sandbox schema is
DROPped in a ``finally``.

Env-gated behind ``ACTR_HARD_DELETE_MIGRATION_LIVE_SMOKE=1`` (needs the live DB).

Run::

    ACTR_HARD_DELETE_MIGRATION_LIVE_SMOKE=1 \\
      .venv/bin/python3 plugins/actr_memory_plugin/tests/actr_memory_hard_delete_migration_live_smoke.py
"""

from __future__ import annotations

import importlib
import json
import os
import secrets
import sys
from pathlib import Path
from typing import Any, LiteralString, cast

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT / "ananta" / "src"))
sys.path.insert(
    0, str(REPO_ROOT / "plugins" / "postgres_state_management_plugin" / "src"),
)
sys.path.insert(0, str(REPO_ROOT / "plugins" / "actr_memory_plugin" / "src"))
sys.path.insert(0, str(REPO_ROOT / "plugins" / "actr_memory_plugin" / "migrations"))

# Pre-load config_manager (via importlib so the import sorter can't reorder it)
# to break the utils↔config cycle when importing the backend standalone.
importlib.import_module("ananta.core.config.config_manager")
from actr_memory_plugin.memory_store import (  # noqa: E402
    delete_memory_records,
    get_memory,
)
from postgres_state_management_plugin.postgres_backend.config import (  # noqa: E402
    PostgresConfig,
)
from postgres_state_management_plugin.postgres_backend.provider import (  # noqa: E402
    PostgresProvider,
)
from purge_soft_deleted_actr_rows import (  # noqa: E402  # pyright: ignore[reportMissingImports]  # runtime sys.path (migrations/)
    NAMESPACE,
    SOFT_DELETED_TABLES,
    purge_soft_deleted,
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
    """Faithful StateManagementInterface stand-in over a live provider:
    ``delete_records`` → ``provider.delete``; ``read_state`` → ``provider.select``."""

    def __init__(self, provider: PostgresProvider) -> None:
        self._provider = provider

    def delete_records(self, namespace: str, query: dict[str, Any]) -> dict[str, Any]:
        deleted = self._provider.delete(
            namespace=namespace,
            table=str(query["table"]),
            conditions=cast("dict[str, Any]", query.get("filters") or {}),
            soft_delete=bool(query.get("soft_delete", True)),
        )
        return {
            "action_status": "completed",
            "data": {"result": {"deleted": deleted}},
        }

    def read_state(self, namespace: str, query: dict[str, Any]) -> dict[str, Any]:
        rows = self._provider.select(
            namespace=namespace,
            table=str(query["table"]),
            conditions=cast("dict[str, Any]", query.get("filters")) or None,
        )
        return {"action_status": "completed", "data": {"records": rows}}


_SEED_AT = "2026-06-01T00:00:00"

# Minimal DDL mirroring the actr standardized columns + the business columns the
# read paths touch. `memory` carries the JSON columns get_memory parses.
_DDL: dict[str, str] = {
    "memory": (
        "id text PRIMARY KEY, content text, memory_type text, status text, "
        "tags text, retrieval_times text, source_memory_ids text, "
        "is_deleted integer NOT NULL DEFAULT 0, "
        "created_at timestamp NOT NULL, updated_at timestamp NOT NULL"
    ),
    "memorization": (
        "id text PRIMARY KEY, is_deleted integer NOT NULL DEFAULT 0, "
        "created_at timestamp NOT NULL, updated_at timestamp NOT NULL"
    ),
    "focus_buffer": (
        "id text PRIMARY KEY, memory_id text, is_deleted integer NOT NULL DEFAULT 0, "
        "created_at timestamp NOT NULL, updated_at timestamp NOT NULL"
    ),
}


def _create_tables(provider: PostgresProvider, schema: str) -> None:
    with provider.get_transactional_connection() as conn, conn.cursor() as cur:
        for table, cols in _DDL.items():
            cur.execute(cast(LiteralString, f'CREATE TABLE "{schema}"."{NAMESPACE}__{table}" ({cols})'))


def _seed_memory(provider: PostgresProvider, schema: str, *, mid: str, is_deleted: int) -> None:
    with provider.get_transactional_connection() as conn, conn.cursor() as cur:
        cur.execute(
            cast(
                LiteralString,
                f'INSERT INTO "{schema}"."{NAMESPACE}__memory" '
                "(id, content, memory_type, status, tags, retrieval_times, "
                "source_memory_ids, is_deleted, created_at, updated_at) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
            ),
            (mid, "hi", "episodic", "active", "[]", "[]", "[]", is_deleted, _SEED_AT, _SEED_AT),
        )


def _seed_minimal(provider: PostgresProvider, schema: str, *, table: str, rid: str, is_deleted: int) -> None:
    extra_col = ", memory_id" if table == "focus_buffer" else ""
    extra_val = ", %s" if table == "focus_buffer" else ""
    params: tuple[Any, ...] = (
        (rid, "m-x", is_deleted, _SEED_AT, _SEED_AT)
        if table == "focus_buffer"
        else (rid, is_deleted, _SEED_AT, _SEED_AT)
    )
    with provider.get_transactional_connection() as conn, conn.cursor() as cur:
        cur.execute(
            cast(
                LiteralString,
                f'INSERT INTO "{schema}"."{NAMESPACE}__{table}" '
                f"(id{extra_col}, is_deleted, created_at, updated_at) "
                f"VALUES (%s{extra_val}, %s, %s, %s)",
            ),
            params,
        )


def _ids(provider: PostgresProvider, schema: str, table: str) -> list[str]:
    rows = provider.execute_query(f'SELECT id FROM "{schema}"."{NAMESPACE}__{table}" ORDER BY id')
    return [str(r[0]) for r in rows]


def test_migration_purges_soft_deleted_only(provider: PostgresProvider, schema: str) -> None:
    """purge_soft_deleted hard-deletes is_deleted=1 rows in all 3 tables, keeps is_deleted=0."""
    _seed_memory(provider, schema, mid="mem-live", is_deleted=0)
    _seed_memory(provider, schema, mid="mem-soft", is_deleted=1)
    _seed_minimal(provider, schema, table="memorization", rid="mz-live", is_deleted=0)
    _seed_minimal(provider, schema, table="memorization", rid="mz-soft", is_deleted=1)
    _seed_minimal(provider, schema, table="focus_buffer", rid="fb-live", is_deleted=0)
    _seed_minimal(provider, schema, table="focus_buffer", rid="fb-soft", is_deleted=1)

    counts = purge_soft_deleted(_LiveStateAdapter(provider), NAMESPACE)

    _check(counts == dict.fromkeys(SOFT_DELETED_TABLES, 1),
           f"purge_soft_deleted deleted exactly one soft row per table; got {counts}")
    _check(_ids(provider, schema, "memory") == ["mem-live"],
           "memory: is_deleted=1 row HARD-gone, is_deleted=0 row survives")
    _check(_ids(provider, schema, "memorization") == ["mz-live"],
           "memorization: is_deleted=1 row HARD-gone, is_deleted=0 row survives")
    _check(_ids(provider, schema, "focus_buffer") == ["fb-live"],
           "focus_buffer: is_deleted=1 row HARD-gone, is_deleted=0 row survives")


def test_migration_idempotent(provider: PostgresProvider, schema: str) -> None:
    """A second run deletes 0 (no is_deleted=1 rows remain)."""
    _ = schema
    counts = purge_soft_deleted(_LiveStateAdapter(provider), NAMESPACE)
    _check(counts == dict.fromkeys(SOFT_DELETED_TABLES, 0),
           f"re-run of purge_soft_deleted is idempotent (0 deleted per table); got {counts}")


def test_soft_delete_leaves_row_control(provider: PostgresProvider, schema: str) -> None:
    """RED CONTROL: a SOFT delete (the pre-flip behavior) leaves the row physically
    present as is_deleted=1 — proving the hard-delete assertions below genuinely
    discriminate (under the old soft default they would fail)."""
    _seed_memory(provider, schema, mid="mem-soft-ctrl", is_deleted=0)
    _LiveStateAdapter(provider).delete_records(
        namespace=NAMESPACE,
        query={"table": "memory", "filters": {"id": "mem-soft-ctrl"}, "soft_delete": True},
    )
    rows = provider.execute_query(
        f'SELECT is_deleted FROM "{schema}"."{NAMESPACE}__memory" WHERE id = %s', ("mem-soft-ctrl",)
    )
    _check(len(rows) == 1 and int(cast("Any", rows[0][0])) == 1,
           "CONTROL: soft delete leaves the row present as is_deleted=1 (so the hard assertions discriminate)")


def test_delete_memory_records_is_hard(provider: PostgresProvider, schema: str) -> None:
    """Part 1: delete_memory_records physically removes the row (not is_deleted=1)."""
    _seed_memory(provider, schema, mid="mem-del", is_deleted=0)
    deleted = delete_memory_records(_LiveStateAdapter(provider), ["mem-del"])
    _check(deleted == 1, "delete_memory_records reported 1 deleted")
    _check("mem-del" not in _ids(provider, schema, "memory"),
           "delete_memory_records HARD-deleted the row (physically absent, not is_deleted=1)")


def test_get_memory_no_is_deleted_filter(provider: PostgresProvider, schema: str) -> None:
    """Part 3: get_memory no longer filters is_deleted (returns the row regardless),
    and returns None once the row is hard-deleted."""
    _seed_memory(provider, schema, mid="mem-flagged", is_deleted=1)
    got = get_memory(_LiveStateAdapter(provider), "mem-flagged")
    _check(got is not None and got.get("id") == "mem-flagged",
           "get_memory returns an is_deleted=1 row (the is_deleted=0 filter was removed)")
    delete_memory_records(_LiveStateAdapter(provider), ["mem-flagged"])
    _check(get_memory(_LiveStateAdapter(provider), "mem-flagged") is None,
           "get_memory returns None after the row is hard-deleted")


def main() -> int:
    if os.environ.get("ACTR_HARD_DELETE_MIGRATION_LIVE_SMOKE") != "1":
        print("=== actr_memory_hard_delete_migration_live_smoke ===")
        print("  SKIP  set ACTR_HARD_DELETE_MIGRATION_LIVE_SMOKE=1 to run; needs the live solet DB.")
        return 0
    print("=== actr_memory_hard_delete_migration_live_smoke ===")
    schema_name = f"example_test_actrhd_{secrets.token_hex(3)}"
    provider = PostgresProvider(_load_pg_config(schema_name))
    provider.initialize()
    try:
        _create_tables(provider, schema_name)
        test_migration_purges_soft_deleted_only(provider, schema_name)
        test_migration_idempotent(provider, schema_name)
        test_soft_delete_leaves_row_control(provider, schema_name)
        test_delete_memory_records_is_hard(provider, schema_name)
        test_get_memory_no_is_deleted_filter(provider, schema_name)
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
