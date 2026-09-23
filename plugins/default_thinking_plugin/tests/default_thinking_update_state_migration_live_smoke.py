#!/usr/bin/env python3
"""Live-Postgres behavioral smoke for default_thinking SQL-lockdown migration.

Pins the two migrated ``default_thinking_plugin`` write sites
(``_finalize_playbook``, plugin.py) against a REAL ``PostgresProvider``. Both
were own-namespace, single-column, primary-key-equality ``UPDATE``s on
``default_thinking_plugin__thinking_playbook``; both are now ``update_state``
calls mirroring the in-file ``_update_playbook_status`` convention:

* ``SET knowledge_base_path = ? WHERE id = ?`` →
  ``update_state(ns=NAMESPACE, {table: thinking_playbook, filters: {id}},
  {knowledge_base_path})``
* ``SET plan_id = ? WHERE id = ?`` →
  ``update_state(... {id}, {plan_id})``

The migrated call SHAPE is the migration, so the smoke replicates the exact
``update_state`` arguments (importing the real ``NAMESPACE``) against a live
``thinking_playbook`` table and asserts the right columns change for the right
row — catching any table / column / filter typo, the realistic failure mode.
The state adapter mirrors the plugin facade 1:1 (``update_state`` →
``provider.update``, rows-affected → ``data.result.updated``). Sandbox schema is
DROPped in a ``finally``.

Env-gated behind ``DEFAULT_THINKING_MIGRATION_LIVE_SMOKE=1`` (needs the live DB
up; own throwaway schema).

Run::

    DEFAULT_THINKING_MIGRATION_LIVE_SMOKE=1 \\
      .venv/bin/python3 plugins/default_thinking_plugin/tests/default_thinking_update_state_migration_live_smoke.py
"""

from __future__ import annotations

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
sys.path.insert(
    0, str(REPO_ROOT / "plugins" / "default_thinking_plugin" / "src"),
)

from default_thinking_plugin.schema import NAMESPACE  # noqa: E402
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
    """Faithful StateManagementInterface stand-in: ``update_state`` →
    ``provider.update`` (rows-affected → ``data.result.updated``)."""

    def __init__(self, provider: PostgresProvider) -> None:
        self._provider = provider

    def update_state(
        self, namespace: str, query: dict[str, Any], updates: dict[str, Any]
    ) -> dict[str, Any]:
        affected = self._provider.update(
            namespace=namespace,
            table=str(query["table"]),
            conditions=cast("dict[str, Any]", query.get("filters") or {}),
            updates=updates,
        )
        return _ok({"namespace": namespace, "result": {"updated": affected}})


_SEED_AT = "2026-06-01T00:00:00"

# Mirrors plugins/default_thinking_plugin/src/default_thinking_plugin/schema.py
# thinking_playbook (business columns) + the auto-injected standard fields.
_DDL = (
    "id text PRIMARY KEY, planning_context_id text NOT NULL, plan_id text, "
    "status text NOT NULL DEFAULT 'active', title text NOT NULL, "
    "knowledge_base_path text NOT NULL, "
    "is_deleted integer NOT NULL DEFAULT 0, "
    "created_at timestamp NOT NULL, updated_at timestamp NOT NULL"
)


def _create_table(provider: PostgresProvider, schema: str) -> None:
    table = f"{NAMESPACE}__thinking_playbook"
    with provider.get_transactional_connection() as conn, conn.cursor() as cur:
        cur.execute(cast(LiteralString, f'CREATE TABLE "{schema}"."{table}" ({_DDL})'))


def _seed_playbook(provider: PostgresProvider, schema: str, *, pid: str) -> None:
    table = f"{NAMESPACE}__thinking_playbook"
    with provider.get_transactional_connection() as conn, conn.cursor() as cur:
        cur.execute(
            cast(
                LiteralString,
                f'INSERT INTO "{schema}"."{table}" '
                '(id, planning_context_id, plan_id, status, title, '
                'knowledge_base_path, is_deleted, created_at, updated_at) '
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
            ),
            (pid, "ctx-1", None, "active", "a-goal", "old/path.md", 0, _SEED_AT, _SEED_AT),
        )


def _scalar(provider: PostgresProvider, schema: str, col: str, pid: str) -> object:
    table = f"{NAMESPACE}__thinking_playbook"
    rows = provider.execute_query(
        f'SELECT "{col}" FROM "{schema}"."{table}" WHERE id = %s', (pid,)
    )
    return rows[0][0] if rows else "<<absent>>"


def test_set_knowledge_base_path(provider: PostgresProvider, schema: str) -> None:
    """Site 1 (plugin.py:4231): the exact migrated update_state shape sets
    knowledge_base_path on the targeted row only."""
    _seed_playbook(provider, schema, pid="pbk-kb")
    _seed_playbook(provider, schema, pid="pbk-other")
    state = _LiveStateAdapter(provider)
    # Mirrors the migrated call verbatim.
    state.update_state(
        namespace=NAMESPACE,
        query={"table": "thinking_playbook", "filters": {"id": "pbk-kb"}},
        updates={"knowledge_base_path": "new/kb/path.md"},
    )
    _check(_scalar(provider, schema, "knowledge_base_path", "pbk-kb") == "new/kb/path.md",
           "update_state set knowledge_base_path on the targeted playbook")
    _check(_scalar(provider, schema, "knowledge_base_path", "pbk-other") == "old/path.md",
           "the id= filter scoped the update to one row (sibling untouched)")


def test_set_plan_id(provider: PostgresProvider, schema: str) -> None:
    """Site 2 (plugin.py:4262): the exact migrated update_state shape sets
    plan_id (NULL → value) on the targeted row."""
    _seed_playbook(provider, schema, pid="pbk-plan")
    state = _LiveStateAdapter(provider)
    _check(_scalar(provider, schema, "plan_id", "pbk-plan") is None, "plan_id starts NULL")
    state.update_state(
        namespace=NAMESPACE,
        query={"table": "thinking_playbook", "filters": {"id": "pbk-plan"}},
        updates={"plan_id": "pln-xyz"},
    )
    _check(_scalar(provider, schema, "plan_id", "pbk-plan") == "pln-xyz",
           "update_state set plan_id (NULL → 'pln-xyz') on the targeted playbook")


def main() -> int:
    if os.environ.get("DEFAULT_THINKING_MIGRATION_LIVE_SMOKE") != "1":
        print("=== default_thinking_update_state_migration_live_smoke ===")
        print("  SKIP  set DEFAULT_THINKING_MIGRATION_LIVE_SMOKE=1 to run; needs the live solet DB.")
        return 0
    print("=== default_thinking_update_state_migration_live_smoke ===")
    schema_name = f"example_test_dthink_{secrets.token_hex(3)}"
    provider = PostgresProvider(_load_pg_config(schema_name))
    provider.initialize()
    try:
        _create_table(provider, schema_name)
        test_set_knowledge_base_path(provider, schema_name)
        test_set_plan_id(provider, schema_name)
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
