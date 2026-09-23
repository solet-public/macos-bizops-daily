#!/usr/bin/env python3
"""Live-Postgres behavioral smoke for core execute_sql Slice-4 (schema-infra, SQL lockdown).

Pins ``SchemaRegistryService.persist_schema``'s migrated deletion against a REAL
``PostgresProvider``: the raw ``DELETE FROM core__schema_registry WHERE
full_table_name = ?`` is gone, replaced by ``delete_records(..., soft_delete=
False)``. The HARD-delete flag is load-bearing — ``persist_schema`` clears all
rows for a table then re-inserts the current columns (fresh ids), which is how a
REMOVED column drops from the registry. A soft delete would leave the old rows
physically present, and the ``(full_table_name, column_name)`` UNIQUE index would
then COLLIDE with the re-insert. This smoke proves:

* first registration writes one row per column;
* a re-registration with a column REMOVED ends with exactly the current columns
  (the removed column's row is physically gone — not ``is_deleted=1``);
* the re-registration SUCCEEDS against the live UNIQUE index (which is the direct
  evidence the delete was hard, not soft — a soft delete would raise on collision);
* a non-completed delete envelope makes ``persist_schema`` RAISE (fail-fast).

The other two Slice-4 changes are dead-code deletions verified statically (zero
callers): ``add_data_sensitivity_column`` (backwards-compat ALTER; the column is
declared in the schema) and ``TableSchema.to_create_sql`` (orphaned renderer,
superseded by the owner plugins' ``ddl_renderer``). They carry no runtime behavior
to pin here.

Env-gated behind ``CORE_SLICE4_LIVE_SMOKE=1`` (needs the live DB up; own throwaway
schema).

Run::

    CORE_SLICE4_LIVE_SMOKE=1 \\
      .venv/bin/python3 ananta/tests/llm/core_slice4/core_slice4_migration_live_smoke.py
"""

from __future__ import annotations

import json
import os
import secrets
import sys
from pathlib import Path
from typing import Any, LiteralString, cast

REPO_ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(REPO_ROOT / "ananta" / "src"))
sys.path.insert(
    0, str(REPO_ROOT / "plugins" / "postgres_state_management_plugin" / "src"),
)

from ananta.services.schema_management import SchemaRegistryService  # noqa: E402
from ananta.types.schema_types import (  # noqa: E402
    ColumnDefinition,
    ColumnType,
    SchemaDefinition,
    TableSchema,
)
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


def _ok(data: dict[str, Any]) -> dict[str, Any]:
    return {"action_status": "completed", "data": data, "actions": [], "error": None, "timestamp": ""}


class _LiveStateAdapter:
    """Faithful StateManagementInterface stand-in mirroring the plugin facade 1:1.

    ``delete_records`` → ``provider.delete`` honoring the ``soft_delete`` flag (the
    facade default is True; ``persist_schema`` passes False); ``write_state`` →
    ``provider.insert`` (record carries its own id); ``read_state`` →
    ``provider.select``.
    """

    def __init__(self, provider: PostgresProvider) -> None:
        self._provider = provider

    def delete_records(self, namespace: str, query: dict[str, Any]) -> dict[str, Any]:
        soft = query.get("soft_delete", True)
        deleted = self._provider.delete(
            namespace=namespace,
            table=str(query["table"]),
            conditions=cast("dict[str, Any]", query.get("filters") or {}),
            soft_delete=bool(soft),
        )
        return _ok({"result": {"deleted": deleted, "soft_delete": bool(soft)}})

    def write_state(self, namespace: str, data: dict[str, Any]) -> dict[str, Any]:
        generated_id = self._provider.insert(
            namespace=namespace,
            table=str(data["table"]),
            data=cast("dict[str, Any]", data["record"]),
        )
        return _ok({"result": {"generated_id": generated_id}})

    def read_state(self, namespace: str, query: dict[str, Any]) -> dict[str, Any]:
        conds = query.get("filters") or {}
        rows = self._provider.select(
            namespace=namespace,
            table=str(query["table"]),
            conditions=cast("dict[str, Any]", conds) if isinstance(conds, dict) else None,
            limit=cast("int | None", query.get("limit")),
        )
        return _ok({"records": rows, "count": len(rows)})


# ─── Sandbox DDL — core__schema_registry (record columns + standard fields) ──

_SCHEMA_REGISTRY_DDL = (
    "id text PRIMARY KEY, namespace text, created_by text, updated_by text, "
    "external_id text, table_namespace text NOT NULL, table_name text NOT NULL, "
    "full_table_name text NOT NULL, column_name text NOT NULL, column_type text, "
    "column_position integer, is_primary_key integer, is_not_null integer, "
    "default_value text, is_unique integer, check_constraint text, "
    "is_standard_field integer, column_description text, data_sensitivity real, "
    "is_deleted integer NOT NULL DEFAULT 0, "
    "created_at timestamp NOT NULL DEFAULT now(), "
    "updated_at timestamp NOT NULL DEFAULT now()"
)


def _create_table(provider: PostgresProvider, schema: str) -> None:
    with provider.get_transactional_connection() as conn, conn.cursor() as cur:
        cur.execute(cast(
            LiteralString,
            f'CREATE TABLE "{schema}"."core__schema_registry" ({_SCHEMA_REGISTRY_DDL})',
        ))
        # The load-bearing UNIQUE index: a soft-delete + re-insert would collide here.
        cur.execute(cast(
            LiteralString,
            f'CREATE UNIQUE INDEX "idx_table_column" ON '
            f'"{schema}"."core__schema_registry" (full_table_name, column_name)',
        ))


def _count(provider: PostgresProvider, schema: str, where_sql: str, params: tuple[object, ...]) -> int:
    rows = provider.execute_query(
        f'SELECT COUNT(*) FROM "{schema}"."core__schema_registry" WHERE {where_sql}', params,
    )
    return cast("int", rows[0][0]) if rows else -1


def _schema_def(columns: list[str], table_name: str = "widget") -> SchemaDefinition:
    return SchemaDefinition(
        namespace="myplugin",
        tables={
            table_name: TableSchema(
                table_name=table_name,
                columns={
                    name: ColumnDefinition(type=ColumnType.TEXT, description=f"column {name}")
                    for name in columns
                },
            )
        },
    )


# ─── Cases ───────────────────────────────────────────────────────────────────


def test_persist_then_reregister_drops_removed_column(provider: PostgresProvider, schema: str) -> None:
    """persist_schema writes one row per column; a re-register with a column removed
    ends with exactly the current columns (removed row physically gone, no collision)."""
    svc = SchemaRegistryService(cast("Any", _LiveStateAdapter(provider)))

    svc.persist_schema("myplugin", "widget", _schema_def(["colA", "colB", "colC"]))
    _check(_count(provider, schema, "full_table_name = %s", ("myplugin__widget",)) == 3,
           "first registration wrote 3 column rows")
    _check(svc.get_column_names("myplugin__widget") == ["colA", "colB", "colC"],
           "get_column_names returns all 3 columns in position order")

    # Re-register with colC REMOVED. If the clear were a SOFT delete, the old
    # colA/colB rows would linger and the re-insert would collide on the UNIQUE
    # index → this call would RAISE. Succeeding proves the delete is HARD.
    svc.persist_schema("myplugin", "widget", _schema_def(["colA", "colB"]))
    _check(_count(provider, schema, "full_table_name = %s", ("myplugin__widget",)) == 2,
           "re-registration left exactly 2 rows (hard-delete cleared, no collision)")
    _check(_count(provider, schema, "full_table_name = %s AND column_name = %s",
                  ("myplugin__widget", "colC")) == 0,
           "removed column colC is PHYSICALLY gone (hard delete, not is_deleted=1)")
    _check(_count(provider, schema, "is_deleted = 1", ()) == 0,
           "no soft-deleted rows linger anywhere (delete_records used soft_delete=False)")
    _check(svc.get_column_names("myplugin__widget") == ["colA", "colB"],
           "get_column_names reflects the current 2-column set")


def test_persist_isolates_other_tables(provider: PostgresProvider, schema: str) -> None:
    """persist_schema's clear is scoped to its own full_table_name — other tables untouched."""
    svc = SchemaRegistryService(cast("Any", _LiveStateAdapter(provider)))
    svc.persist_schema("myplugin", "gadget", _schema_def(["x", "y"], table_name="gadget"))
    # Re-register widget; gadget rows must survive.
    svc.persist_schema("myplugin", "widget", _schema_def(["colA"]))
    _check(_count(provider, schema, "full_table_name = %s", ("myplugin__gadget",)) == 2,
           "unrelated table (myplugin__gadget) rows survive a widget re-register")


class _DeleteErrorAdapter:
    """delete_records returns a non-completed envelope — persist_schema must RAISE."""

    def delete_records(self, namespace: str, query: dict[str, Any]) -> dict[str, Any]:
        _ = namespace, query
        return {"action_status": "error", "data": None, "actions": [], "error": "simulated DB failure"}

    def write_state(self, namespace: str, data: dict[str, Any]) -> dict[str, Any]:
        _ = namespace, data
        return _ok({"result": {"generated_id": "should-not-reach"}})

    def read_state(self, namespace: str, query: dict[str, Any]) -> dict[str, Any]:
        _ = namespace, query
        return _ok({"records": []})


def test_fail_fast_delete_error() -> None:
    """A non-completed delete envelope makes persist_schema RAISE, never a silent skip."""
    svc = SchemaRegistryService(cast("Any", _DeleteErrorAdapter()))
    raised = False
    try:
        svc.persist_schema("myplugin", "widget", _schema_def(["colA"]))
    except RuntimeError:
        raised = True
    _check(raised, "persist_schema RAISES when the clear (delete_records) does not complete")


def main() -> int:
    if os.environ.get("CORE_SLICE4_LIVE_SMOKE") != "1":
        print("=== core_slice4_migration_live_smoke ===")
        print(
            "  SKIP  set CORE_SLICE4_LIVE_SMOKE=1 to run; needs the live "
            "solet DB (own throwaway schema)."
        )
        return 0
    print("=== core_slice4_migration_live_smoke ===")
    schema_name = f"example_test_slice4_{secrets.token_hex(3)}"
    provider = PostgresProvider(_load_pg_config(schema_name))
    provider.initialize()
    try:
        _create_table(provider, schema_name)
        test_persist_then_reregister_drops_removed_column(provider, schema_name)
        test_persist_isolates_other_tables(provider, schema_name)
        test_fail_fast_delete_error()
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
