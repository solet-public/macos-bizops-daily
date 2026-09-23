#!/usr/bin/env python3
"""Live-Postgres behavioral smoke for core execute_sql Slice-2 (SQL lockdown #0).

Pins the three migrated single-table sites against a REAL ``PostgresProvider``:

* ``work_product_store.save_register`` — raw ``UPDATE`` → ``update_state``.
* ``action_processor._get_template_variables`` — raw ``SELECT key,value … WHERE
  namespace`` (f-string-interpolated, all scopes) → ``list_key_values`` (kills the
  injection; preserves all-scopes semantics) + the ``data.values`` dict parse.
* ``job_service._query_latest_job`` — raw dynamic ``WHERE`` (incl. ``provider_name
  LIKE 'plugin.%'``) + ``ORDER BY created_at DESC LIMIT 1`` → read-then-route:
  ``query_state`` equality filters + Python prefix-filter + ``(created_at, id)``
  VALUE-coerced max (the LIKE branch is not grammar-expressible; created_at must
  compare by value not spelling; id is the deterministic tie-break the raw query
  lacked).

Each migrated path is driven through the REAL production method (constructed over
a faithful state adapter wired to a live provider) and asserted against the
deterministic expected result for the known seeded corpus — with raw column
read-backs (``_scalar``) where a write is verified, and DB-error-envelope stubs
for the fail-fast cases. (It does not run a parallel raw query as a differential
oracle the way the Slice-1 read smoke does.) Sandbox schema is DROPped in a
``finally``.

Env-gated behind ``CORE_SLICE2_LIVE_SMOKE=1`` (needs the live DB up; own throwaway
schema).

Run::

    CORE_SLICE2_LIVE_SMOKE=1 \\
      .venv/bin/python3 ananta/tests/llm/core_slice2/core_slice2_migration_live_smoke.py
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

from ananta.core.actions.action_processor import ActionProcessor  # noqa: E402
from ananta.core.plans.work_product_store import WorkProductStoreAdapter  # noqa: E402
from ananta.services.job_service.service import JobService  # noqa: E402
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

    ``read_state``/``query_state`` → ``provider.select``; ``update_state`` →
    ``provider.update`` (rows-affected); ``list_key_values`` → ``provider.select``
    over ``core__key_value_store`` filtered by the row ``namespace`` column,
    returning ``data.values`` (the kv_list shape).
    """

    def __init__(self, provider: PostgresProvider) -> None:
        self._provider = provider

    def read_state(self, namespace: str, query: dict[str, Any]) -> dict[str, Any]:
        filters = query.get("filters") or {}
        rows = self._provider.select(
            namespace=namespace,
            table=str(query["table"]),
            conditions=cast("dict[str, Any]", filters) if isinstance(filters, dict) else None,
            limit=cast("int | None", query.get("limit")),
        )
        return _ok({"records": rows, "count": len(rows)})

    def query_state(self, namespace: str, filters: dict[str, Any]) -> dict[str, Any]:
        return self.read_state(namespace, filters)

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

    def list_key_values(
        self, namespace: str | None = None, scope: str | None = None, pattern: str | None = None
    ) -> dict[str, Any]:
        _ = pattern
        conditions: dict[str, Any] = {}
        if namespace:
            conditions["namespace"] = namespace
        if scope:
            conditions["scope"] = scope
        rows = self._provider.select(
            namespace="core", table="key_value_store",
            conditions=conditions if conditions else None,
        )
        return _ok({"namespace": namespace, "scope": scope, "values": rows, "count": len(rows)})


# ─── Sandbox DDL ─────────────────────────────────────────────────────────────

_DDL: tuple[tuple[str, str], ...] = (
    (
        "default_thinking_plugin__thinking_wbs",
        "id text PRIMARY KEY, work_products_data text, "
        "is_deleted integer NOT NULL DEFAULT 0, "
        "created_at timestamp NOT NULL, updated_at timestamp NOT NULL",
    ),
    (
        "core__key_value_store",
        "id text PRIMARY KEY, namespace text NOT NULL, key text NOT NULL, "
        "value text, scope text NOT NULL DEFAULT 'GLOBAL', "
        "is_deleted integer NOT NULL DEFAULT 0, "
        "created_at timestamp NOT NULL, updated_at timestamp NOT NULL",
    ),
    (
        "core__job",
        "id text PRIMARY KEY, provider_name text NOT NULL, status text NOT NULL, "
        "is_deleted integer NOT NULL DEFAULT 0, "
        "created_at timestamp NOT NULL, updated_at timestamp NOT NULL",
    ),
    # Required since JobService attaches stored payloads to every job it
    # returns (2026-08-14): the ledger row has no result/error column, so
    # _query_latest_job now reads this companion table too. Without it the
    # sandbox is a fake that no longer carries the shape it stands in for,
    # and the job tests below fail on a missing relation rather than on
    # anything they mean to assert.
    (
        "core__job_payload",
        "id text PRIMARY KEY, job_id text NOT NULL, payload_type text NOT NULL, "
        "payload_data text, sequence integer, "
        "is_deleted integer NOT NULL DEFAULT 0, "
        "created_at timestamp NOT NULL, updated_at timestamp NOT NULL",
    ),
)

_SEED_AT = "2026-06-01T00:00:00"


def _create_trigger_function(provider: PostgresProvider, schema: str) -> None:
    with provider.get_transactional_connection() as conn, conn.cursor() as cur:
        cur.execute(cast(
            LiteralString,
            f'CREATE OR REPLACE FUNCTION "{schema}".update_updated_at_column() '
            "RETURNS TRIGGER AS $$ BEGIN "
            "NEW.updated_at = (NOW() AT TIME ZONE 'UTC'); RETURN NEW; "
            "END; $$ LANGUAGE plpgsql;",
        ))


def _create_tables(provider: PostgresProvider, schema: str) -> None:
    with provider.get_transactional_connection() as conn, conn.cursor() as cur:
        for table, body in _DDL:
            cur.execute(cast(LiteralString, f'CREATE TABLE "{schema}"."{table}" ({body})'))
            cur.execute(cast(
                LiteralString,
                f'CREATE TRIGGER "{table}_upd" BEFORE UPDATE ON "{schema}"."{table}" '
                f'FOR EACH ROW EXECUTE FUNCTION "{schema}".update_updated_at_column();',
            ))


def _insert(provider: PostgresProvider, schema: str, table: str, row: dict[str, object]) -> None:
    cols = list(row.keys())
    placeholders = ", ".join(["%s"] * len(cols))
    col_csv = ", ".join(f'"{c}"' for c in cols)
    with provider.get_transactional_connection() as conn, conn.cursor() as cur:
        cur.execute(
            cast(LiteralString, f'INSERT INTO "{schema}"."{table}" ({col_csv}) VALUES ({placeholders})'),
            tuple(row[c] for c in cols),
        )


def _scalar(provider: PostgresProvider, schema: str, table: str, col: str, row_id: str) -> object:
    rows = provider.execute_query(f'SELECT "{col}" FROM "{schema}"."{table}" WHERE id = %s', (row_id,))
    return rows[0][0] if rows else "<<absent>>"


# ─── Cases ───────────────────────────────────────────────────────────────────


def test_work_product_save_register(provider: PostgresProvider, schema: str) -> None:
    """save_register (update_state) round-trips work_products_data + bumps updated_at."""
    _insert(provider, schema, "default_thinking_plugin__thinking_wbs", {
        "id": "wbs-abc", "work_products_data": None, "is_deleted": 0,
        "created_at": _SEED_AT, "updated_at": _SEED_AT,
    })
    adapter = _LiveStateAdapter(provider)
    store = WorkProductStoreAdapter(state_service=cast("Any", adapter))
    _check(store.load_register("wbs-abc") is None, "load_register before save → None (no register yet)")
    store.save_register("wbs-abc", '{"frag": 1}')
    _check(_scalar(provider, schema, "default_thinking_plugin__thinking_wbs", "work_products_data", "wbs-abc") == '{"frag": 1}',
           "save_register wrote work_products_data via update_state")
    _check(store.load_register("wbs-abc") == '{"frag": 1}', "load_register reads back the saved register")
    _check(str(_scalar(provider, schema, "default_thinking_plugin__thinking_wbs", "updated_at", "wbs-abc")) > _SEED_AT,
           "updated_at advanced via trigger on the update_state write")


def test_template_variables_all_scopes(provider: PostgresProvider, schema: str) -> None:
    """_get_template_variables (list_key_values) returns all keys across ALL scopes."""
    ns = "myplugin.actions"
    rows = [
        ("kv-1", ns, "alpha", "A", "GLOBAL"),
        ("kv-2", ns, "beta", "B", "SESSION"),   # different scope — must still appear
        ("kv-3", ns, "gamma", "C", "FLOW"),      # different scope — must still appear
        ("kv-4", "other.ns", "delta", "D", "GLOBAL"),  # different namespace — must NOT appear
    ]
    for rid, rns, key, val, scope in rows:
        _insert(provider, schema, "core__key_value_store", {
            "id": rid, "namespace": rns, "key": key, "value": val, "scope": scope,
            "is_deleted": 0, "created_at": _SEED_AT, "updated_at": _SEED_AT,
        })
    proc = ActionProcessor.__new__(ActionProcessor)
    proc.state_service = cast("Any", _LiveStateAdapter(provider))
    variables = proc._get_template_variables(ns)  # noqa: SLF001 — exercising the real migrated method
    _check(variables == {"alpha": "A", "beta": "B", "gamma": "C"},
           f"all scopes (GLOBAL/SESSION/FLOW) returned, other namespace excluded; got {variables}")
    _check(proc._get_template_variables("absent.ns") == {}, "unknown namespace → {{}}")  # noqa: SLF001


def test_job_query_read_then_route(provider: PostgresProvider, schema: str) -> None:
    """_query_latest_job: exact match, LIKE-prefix, status filter, created_at-DESC, tie-break."""
    jobs = [
        ("job-1", "plugA.do", "completed", "2026-06-02T00:00:01"),
        ("job-2", "plugA.do", "running", "2026-06-02T00:00:05"),   # newer plugA.do
        ("job-3", "plugA.other", "completed", "2026-06-02T00:00:09"),  # plugA. prefix, not plugA.do
        ("job-4", "plugB.do", "completed", "2026-06-02T00:00:20"),  # different plugin
    ]
    for rid, pname, status, created in jobs:
        _insert(provider, schema, "core__job", {
            "id": rid, "provider_name": pname, "status": status,
            "is_deleted": 0, "created_at": created, "updated_at": _SEED_AT,
        })
    svc = JobService(state_service=cast("Any", _LiveStateAdapter(provider)))

    exact = svc._query_latest_job("plugA", "do", None)  # noqa: SLF001
    _check(exact is not None and str(exact["id"]) == "job-2", f"exact provider_name='plugA.do' → newest (job-2); got {exact and exact.get('id')}")

    prefix = svc._query_latest_job("plugA", None, None)  # noqa: SLF001 — LIKE 'plugA.%'
    _check(prefix is not None and str(prefix["id"]) == "job-3", f"LIKE prefix 'plugA.' → newest plugA.* (job-3, incl plugA.other); got {prefix and prefix.get('id')}")

    with_status = svc._query_latest_job("plugA", None, "completed")  # noqa: SLF001
    _check(with_status is not None and str(with_status["id"]) == "job-3", f"prefix + status=completed → job-3; got {with_status and with_status.get('id')}")
    running = svc._query_latest_job("plugA", "do", "running")  # noqa: SLF001
    _check(running is not None and str(running["id"]) == "job-2", f"exact + status=running → job-2; got {running and running.get('id')}")

    none = svc._query_latest_job("nope", None, None)  # noqa: SLF001
    _check(none is None, "no match → None")


def test_job_created_at_tie_break(provider: PostgresProvider, schema: str) -> None:
    """Equal created_at jobs tie-break deterministically on id (raw query had no secondary sort)."""
    for rid in ("job-tieA", "job-tieB"):
        _insert(provider, schema, "core__job", {
            "id": rid, "provider_name": "tie.do", "status": "completed",
            "is_deleted": 0, "created_at": "2026-06-03T00:00:00", "updated_at": _SEED_AT,
        })
    svc = JobService(state_service=cast("Any", _LiveStateAdapter(provider)))
    got = svc._query_latest_job("tie", "do", None)  # noqa: SLF001
    _check(got is not None and str(got["id"]) == "job-tieB",
           f"equal created_at → highest id wins deterministically (job-tieB); got {got and got.get('id')}")
    # Divergent cursor-spelling robustness: a tz-aware seed compares by VALUE.
    _insert(provider, schema, "core__job", {
        "id": "job-tieC", "provider_name": "tie.do", "status": "completed",
        "is_deleted": 0, "created_at": "2026-06-03T00:00:00.000000", "updated_at": _SEED_AT,
    })
    got2 = svc._query_latest_job("tie", "do", None)  # noqa: SLF001
    _check(got2 is not None and str(got2["id"]) == "job-tieC",
           f"equal-instant '...00' vs '...00.000000' compared by value, id tie-break → job-tieC; got {got2 and got2.get('id')}")


class _ErrorStateAdapter:
    """A state adapter that returns a DB-error envelope for every read.

    Exercises the fail-fast contract (Codex MAJOR-1): a non-completed envelope
    must surface as an error, never silently degrade to "no rows" / "no vars".
    """

    def query_state(self, namespace: str, filters: dict[str, Any]) -> dict[str, Any]:
        _ = namespace, filters
        return {"action_status": "error", "data": None, "actions": [], "error": "simulated DB failure"}

    def list_key_values(
        self, namespace: str | None = None, scope: str | None = None, pattern: str | None = None
    ) -> dict[str, Any]:
        _ = namespace, scope, pattern
        return {"action_status": "error", "data": None, "actions": [], "error": "simulated DB failure"}


class _MalformedRowAdapter:
    """Returns a COMPLETED envelope whose job row is missing ``provider_name``.

    Exercises Codex item 7: a malformed completed row must RAISE, not get silently
    dropped by the prefix-filter and read as "no job found".
    """

    def query_state(self, namespace: str, filters: dict[str, Any]) -> dict[str, Any]:
        _ = namespace, filters
        return _ok({"records": [{"id": "job-x", "created_at": "2026-06-01T00:00:00"}], "count": 1})


def test_fail_fast_work_product_missing_row(provider: PostgresProvider) -> None:
    """save_register on a MISSING WBS row RAISES (0 affected — not a silent no-op)."""
    store = WorkProductStoreAdapter(state_service=cast("Any", _LiveStateAdapter(provider)))
    raised = False
    try:
        store.save_register("wbs-does-not-exist", '{"x": 1}')
    except RuntimeError:
        raised = True
    _check(raised, "save_register on a missing WBS row RAISES (affected != 1), not a silent success")


def test_fail_fast_job_db_error() -> None:
    """A DB-error envelope → get_latest_job returns STATUS_ERROR, not completed/job=None."""
    svc = JobService(state_service=cast("Any", _ErrorStateAdapter()))
    result = svc.get_latest_job("plugA", "do", None)
    job = result.get("data", {}).get("result", {}).get("job") if isinstance(result.get("data"), dict) else "?"
    _check(result.get("action_status") == "error" and job is None,
           f"job DB-error envelope → STATUS_ERROR + job=None (not completed/no-job); got status={result.get('action_status')}")


def test_fail_fast_job_malformed_row() -> None:
    """A completed job row missing provider_name → STATUS_ERROR (raises, not silent drop)."""
    svc = JobService(state_service=cast("Any", _MalformedRowAdapter()))
    result = svc.get_latest_job("plugA", None, None)  # LIKE-prefix branch
    _check(result.get("action_status") == "error",
           f"malformed completed row (no provider_name) → STATUS_ERROR, not silent None; got {result.get('action_status')}")


def test_fail_fast_template_vars_db_error() -> None:
    """A DB-error envelope → _get_template_variables RAISES, not silent {{}}."""
    proc = ActionProcessor.__new__(ActionProcessor)
    proc.state_service = cast("Any", _ErrorStateAdapter())
    raised = False
    try:
        proc._get_template_variables("myplugin.actions")  # noqa: SLF001
    except Exception:  # noqa: BLE001 — any surfaced error proves fail-fast
        raised = True
    _check(raised, "template-var DB-error envelope → _get_template_variables RAISES (not silent {})")


def main() -> int:
    if os.environ.get("CORE_SLICE2_LIVE_SMOKE") != "1":
        print("=== core_slice2_migration_live_smoke ===")
        print(
            "  SKIP  set CORE_SLICE2_LIVE_SMOKE=1 to run; needs the live "
            "solet DB (own throwaway schema)."
        )
        return 0
    print("=== core_slice2_migration_live_smoke ===")
    schema_name = f"example_test_slice2_{secrets.token_hex(3)}"
    provider = PostgresProvider(_load_pg_config(schema_name))
    provider.initialize()
    try:
        _create_trigger_function(provider, schema_name)
        _create_tables(provider, schema_name)
        test_work_product_save_register(provider, schema_name)
        test_template_variables_all_scopes(provider, schema_name)
        test_job_query_read_then_route(provider, schema_name)
        test_job_created_at_tie_break(provider, schema_name)
        test_fail_fast_work_product_missing_row(provider)
        test_fail_fast_job_db_error()
        test_fail_fast_job_malformed_row()
        test_fail_fast_template_vars_db_error()
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
