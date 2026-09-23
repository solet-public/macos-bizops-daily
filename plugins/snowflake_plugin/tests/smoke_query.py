#!/usr/bin/env python3
"""Query-verb + classification smoke tests for snowflake_plugin.

Hermetic — a faked Snowflake connection/cursor, no live warehouse (there is no
LIVE write-capability smoke here — see this module's own run_statement tests'
docstring below for why). Red-first: every check asserts REAL behavior in
query_actions / connection.classify_snowflake_error / the EDGE parity.

Exercises:
  1. run_query — writes a TSV handle (path/columns/row_count/truncated), never
     rows/records inline (override-friction default/override behavior is
     smoke_data_export.py's job, not duplicated here)
  2. run_query — refuses a write-leader statement (guard)
  3. run_statement — no-result-set statement disables autocommit, commits, and
     returns rowcount inline; a result-producing statement (e.g. RETURNING)
     routes through the same always-TSV path as run_query and commits; a
     result-producing statement WITHOUT output_tsv_path rolls back (never
     silently discards the returned rows while committing); the read-leader
     ACCESS-CONTROL guard never runs on this path (a DELETE is not refused) —
     see query_actions.run_statement's own docstring. No single-statement
     SHAPE guard test exists here (unlike external_postgres_plugin's
     run_statement): Snowflake enforces single-statement natively at the
     driver, not via a plugin-side parser, so there is nothing to exercise
     against a fake connection — see connection.py's module docstring for the
     live verification.
  4. list_databases — name extraction from a SHOW DATABASES row
  5. list_schemas / list_tables / describe_table — shape + identifier escaping
  6. test_connection — account/user/role/warehouse/version shape
  7. TOPOLOGY-LEAK (SECURITY): auth/timeout/warehouse-suspended classes
     classify to a GENERIC message that NEVER contains the driver exception's
     account/user marker; object-not-found keeps driver detail
  8. EDGE parity: validate_edge_process_provider raises nothing

The plugin-level dispatch->driver-fault integration (a run_query/etc. call
surfacing a topology-safe classified error) now runs entirely inside the D0.3
background worker, not the dispatch call — covered by smoke_async_jobs.py's
worker tests, not duplicated here.

NO LIVE smoke exists for run_statement (unlike external_postgres_plugin's
smoke_write.py against a local scratch database). The only reachable
Snowflake target from this checkout is the operator's own live account,
connected on an explicitly reader-scoped role — not a scratch/test target —
so a live write-capability proof was deliberately NOT attempted here. This is
a named, disclosed gap, not a silent omission: see this batch's commit
message.

Run:
    SOLET_NAME=<name> .venv/bin/python3 \
        plugins/snowflake_plugin/tests/smoke_query.py

Exits 0 on success, 1 on first failure.
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT / "plugins" / "snowflake_plugin" / "src"))

from snowflake_plugin import connection, query_actions  # noqa: E402
from snowflake_plugin.plugin import SnowflakePlugin  # noqa: E402
from snowflake_plugin.statement_guard import StatementGuardError  # noqa: E402

_ACCOUNT_MARKER = "SECRET-ACCOUNT-9.9.9.9"
_USER_MARKER = "SECRET-USER-marker"

_passed = 0
_failed: list[str] = []


def _assert(label: str, cond: bool, msg: str = "") -> None:
    global _passed
    if cond:
        _passed += 1
        print(f"  PASS  {label}")
    else:
        _failed.append(label)
        print(f"  FAIL  {label}: {msg or 'assertion failed'}")


def _fake_conn(columns: list[str] | None, rows: list[tuple[Any, ...]]) -> tuple[Any, Any]:
    cur = MagicMock()
    cur.description = [(c,) for c in columns] if columns else None
    cur.fetchmany.return_value = rows
    cur.fetchall.return_value = rows
    cur.fetchone.return_value = rows[0] if rows else None
    ctx = MagicMock()
    ctx.__enter__.return_value = cur
    ctx.__exit__.return_value = False
    conn = MagicMock()
    conn.cursor.return_value = ctx
    return conn, cur



class _FakeDriverError(Exception):
    def __init__(self, errno: int, message: str, sqlstate: str | None = None) -> None:
        super().__init__(message)
        self.errno = errno
        self.sqlstate = sqlstate
        self.msg = message


def _passthrough_gate(path: str) -> str:
    """A no-op containment gate — real containment is smoke_data_export.py's job."""
    return path


def test_run_query_writes_tsv() -> None:
    conn, _ = _fake_conn(["id", "name"], [(1, "alice"), (2, "bob")])
    with tempfile.TemporaryDirectory(prefix="snw_shape_") as workspace:
        out_path = str(Path(workspace) / "out.tsv")
        result = query_actions.run_query(
            conn, {"sql": "SELECT id, name FROM t", "output_tsv_path": out_path}, _passthrough_gate,
        )
        _assert("handle columns carried", result["columns"] == ["id", "name"])
        _assert("handle row_count", result["row_count"] == 2)
        _assert("handle not truncated", result["truncated"] is False)
        _assert("no rows/records field — never inline", "rows" not in result and "records" not in result)
        lines = Path(out_path).read_text(encoding="utf-8").splitlines()
        _assert("tsv header", lines[0] == "id\tname")
        _assert("tsv data rows", "1\talice" in lines and "2\tbob" in lines)


def test_run_query_guard() -> None:
    conn, _ = _fake_conn(["id"], [(1,)])
    with tempfile.TemporaryDirectory(prefix="snw_guard_") as workspace:
        out_path = str(Path(workspace) / "out.tsv")
        write = False
        try:
            query_actions.run_query(
                conn, {"sql": "DELETE FROM t", "output_tsv_path": out_path}, _passthrough_gate,
            )
        except StatementGuardError:
            write = True
        _assert("write-leader run_query refused", write)


def test_run_statement_no_result_set_commits_inline() -> None:
    conn, cur = _fake_conn(None, [])
    cur.rowcount = 3
    result = query_actions.run_statement(
        conn, {"sql": "UPDATE t SET x = 1"}, _passthrough_gate,
    )
    _assert("no-result-set has_result_set False", result["has_result_set"] is False)
    _assert("no-result-set carries rowcount", result.get("rowcount") == 3)
    _assert("no path/columns/row_count keys when no result set", "path" not in result and "columns" not in result)
    _assert("autocommit disabled before executing", conn.autocommit.call_args == ((False,),))
    _assert("no-result-set commits", conn.commit.called)
    _assert("no-result-set never rolls back", not conn.rollback.called)


def test_run_statement_with_result_set_writes_tsv() -> None:
    conn, _ = _fake_conn(["id"], [(1,), (2,)])
    with tempfile.TemporaryDirectory(prefix="snw_stmt_") as workspace:
        out_path = str(Path(workspace) / "out.tsv")
        result = query_actions.run_statement(
            conn,
            {"sql": "INSERT INTO t (v) VALUES (1), (2) RETURNING id", "output_tsv_path": out_path},
            _passthrough_gate,
        )
        _assert("result-set has_result_set True", result["has_result_set"] is True)
        _assert("result-set carries path", result["path"] == out_path)
        _assert("result-set carries row_count", result["row_count"] == 2)
        _assert("result-set commits", conn.commit.called)
        lines = Path(out_path).read_text(encoding="utf-8").splitlines()
        _assert("tsv actually written", lines[0] == "id" and "1" in lines and "2" in lines)


def test_run_statement_result_set_without_path_rolls_back() -> None:
    conn, _ = _fake_conn(["id"], [(1,)])
    raised = False
    try:
        query_actions.run_statement(
            conn, {"sql": "INSERT INTO t (v) VALUES (1) RETURNING id"}, _passthrough_gate,
        )
    except ValueError:
        raised = True
    _assert("missing output_tsv_path for a result-producing statement raises", raised)
    _assert("rolls back rather than silently discarding rows", conn.rollback.called)
    _assert("never commits when rows would be discarded", not conn.commit.called)


def test_run_statement_no_read_leader_classification() -> None:
    # The read/write ACCESS-CONTROL guard (assert_read_statement) must NEVER
    # run on this path -- a DELETE is not refused here, unlike run_query.
    conn, _ = _fake_conn(None, [])
    refused = False
    try:
        query_actions.run_statement(conn, {"sql": "DELETE FROM t"}, _passthrough_gate)
    except StatementGuardError:
        refused = True
    _assert("run_statement never applies the read-leader access-control guard", not refused)


def test_list_databases() -> None:
    conn, _ = _fake_conn(["created_on", "name", "is_default"], [("2026-01-01", "ANALYTICS", "N")])
    result = query_actions.list_databases(conn, {})
    _assert("databases shape", result["databases"] == [{"name": "ANALYTICS"}])


def test_list_schemas() -> None:
    conn, _ = _fake_conn(["created_on", "name"], [("2026-01-01", "PUBLIC")])
    result = query_actions.list_schemas(conn, {"database": "ANALYTICS"})
    _assert("schemas shape", result["schemas"] == ["PUBLIC"])


def test_list_tables() -> None:
    conn, _ = _fake_conn(["created_on", "name", "kind"], [("2026-01-01", "ORDERS", "TABLE")])
    result = query_actions.list_tables(conn, {"database": "ANALYTICS", "schema": "PUBLIC"})
    _assert("tables shape", result["tables"] == [{"name": "ORDERS", "kind": "TABLE"}])


def test_describe_table() -> None:
    conn, _ = _fake_conn(
        ["name", "type", "kind", "null?", "default"],
        [("id", "NUMBER(38,0)", "COLUMN", "N", None), ("label", "VARCHAR", "COLUMN", "Y", "''")],
    )
    cols = query_actions.describe_table(conn, {"database": "d", "schema": "s", "table": "t"})["columns"]
    _assert("column name/type", cols[0]["name"] == "id" and cols[0]["type"] == "NUMBER(38,0)")
    _assert("N -> nullable False", cols[0]["nullable"] is False)
    _assert("Y -> nullable True", cols[1]["nullable"] is True)


def test_test_connection() -> None:
    conn, _ = _fake_conn(
        ["account", "user", "role", "warehouse", "version"],
        [("myorg-acct", "OPERATOR_USER", "EXAMPLE_READONLY", "WH_XS", "8.1.0")],
    )
    result = query_actions.test_connection(conn, {})
    _assert("ok True", result["ok"] is True)
    _assert("account carried", result["account"] == "myorg-acct")
    _assert("role carried", result["role"] == "EXAMPLE_READONLY")


def test_classify_topology_leak() -> None:
    auth_err = _FakeDriverError(390144, f"JWT rejected for account {_ACCOUNT_MARKER} user {_USER_MARKER}")
    code, message = connection.classify_snowflake_error(auth_err)
    _assert("auth-class -> auth_failed", code == "snowflake.auth_failed")
    _assert("auth message hides account marker", _ACCOUNT_MARKER not in message, message)
    _assert("auth message hides user marker", _USER_MARKER not in message, message)

    timeout_err = _FakeDriverError(604, f"query timed out on account {_ACCOUNT_MARKER}")
    code, message = connection.classify_snowflake_error(timeout_err)
    _assert("timeout-class -> timeout", code == "snowflake.timeout")
    _assert("timeout message hides account marker", _ACCOUNT_MARKER not in message, message)

    suspended_err = _FakeDriverError(606, f"warehouse suspended for {_ACCOUNT_MARKER}")
    code, message = connection.classify_snowflake_error(suspended_err)
    _assert("warehouse-suspended-class -> warehouse_suspended", code == "snowflake.warehouse_suspended")
    _assert("warehouse message hides account marker", _ACCOUNT_MARKER not in message, message)

    not_found_err = _FakeDriverError(2003, "SQL compilation error: Table 'FOO' does not exist")
    code, message = connection.classify_snowflake_error(not_found_err)
    _assert("not-found carries driver detail", "FOO" in message, message)


def test_edge_parity() -> None:
    from ananta.core.plugins.action_discovery import discover_actions
    from ananta.core.process_registry.plugin_registration_validator import (
        PluginRegistrationValidator,
    )

    plugin = SnowflakePlugin()
    actions = discover_actions(plugin)
    raised = None
    try:
        PluginRegistrationValidator().validate_edge_process_provider(
            "snowflake_plugin", plugin, actions
        )
    except Exception as exc:  # FrameworkError on mismatch
        raised = exc
    _assert("EDGE parity: validator raises nothing", raised is None, str(raised))
    _assert("all 8 verbs discovered", len(actions) == 8)


def main() -> int:
    print("\nsnowflake_plugin query-verb smoke tests")
    print("=" * 47)
    test_run_query_writes_tsv()
    test_run_query_guard()
    test_run_statement_no_result_set_commits_inline()
    test_run_statement_with_result_set_writes_tsv()
    test_run_statement_result_set_without_path_rolls_back()
    test_run_statement_no_read_leader_classification()
    test_list_databases()
    test_list_schemas()
    test_list_tables()
    test_describe_table()
    test_test_connection()
    test_classify_topology_leak()
    test_edge_parity()
    print()
    print(f"Results: {_passed} passed, {len(_failed)} failed")
    if _failed:
        print("FAILED:", _failed)
        return 1
    print("All query-verb smoke tests passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
