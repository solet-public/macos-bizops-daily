#!/usr/bin/env python3
"""Query-verb + classification smoke tests for external_postgres_plugin.

Hermetic — a faked psycopg connection/cursor, no live Postgres (the live
read-only proof is smoke_readonly.py; the live write-capability proof is
smoke_write.py). Red-first: every check asserts REAL behavior in
query_actions / connection.classify_pg_error / the EDGE parity.

Exercises:
  1. run_query — writes a TSV handle (path/columns/row_count/truncated), never
     rows/records inline (override-friction default/override behavior is
     smoke_data_export.py's job, not duplicated here)
  2. run_query — refuses a two-statement string and a write leader (guard)
  3. run_statement — no-result-set statement commits and returns rowcount
     inline; a RETURNING statement routes through the same always-TSV path as
     run_query and commits; a RETURNING statement WITHOUT output_tsv_path
     rolls back (never silently discards the returned rows while committing);
     single-statement SHAPE guard still applies; the read-leader
     ACCESS-CONTROL guard never runs on this path (a write leader is not
     refused) — see query_actions.run_statement's own docstring
  4. list_schemas — system schemas excluded via a bound param
  5. list_tables — schema passed as a BOUND parameter (not interpolated)
  6. describe_table — column shape + nullable coercion
  7. test_connection — ok/read_only shape + resolved host echo (conn.info.host)
  8. TOPOLOGY-LEAK (SECURITY): connection/auth classes classify to a GENERIC
     message that NEVER contains the driver exception's host/user marker;
     syntax classes carry the server's primary message (caller's own query)
  9. statement_timeout bounds: a 0/negative/non-int config value fails LOUD
      (external_pg.not_configured) — the DoS bound is non-disableable
  10. platform_pg_port bounds: a non-int / 0 / out-of-range [1,65535] config value
      fails LOUD (external_pg.not_configured) — protects the §8.4 guard's port arm
  11. EDGE parity: validate_edge_process_provider raises nothing

The plugin-level dispatch->driver-fault integration (a run_query/etc. call surfacing
a topology-safe classified error) now runs entirely inside the D0.3 background
worker, not the dispatch call — covered by smoke_async_jobs.py's worker tests,
not duplicated here.

Run:
    SOLET_NAME=<name> .venv/bin/python3 \
        plugins/external_postgres_plugin/tests/smoke_query.py

Exits 0 on success, 1 on first failure.
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT / "plugins" / "external_postgres_plugin" / "src"))

from external_postgres_plugin import connection, query_actions  # noqa: E402
from external_postgres_plugin.app_config import ExternalPgConfigError  # noqa: E402
from external_postgres_plugin.plugin import ExternalPostgresPlugin  # noqa: E402
from external_postgres_plugin.statement_guard import StatementGuardError  # noqa: E402

_HOST_MARKER = "SECRET-HOST-9.9.9.9"
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
    cur.description = [SimpleNamespace(name=c) for c in columns] if columns else None
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
    def __init__(self, sqlstate: str, message: str, primary: str | None = None) -> None:
        super().__init__(message)
        self.sqlstate = sqlstate
        self.diag = SimpleNamespace(message_primary=primary)


def _passthrough_gate(path: str) -> str:
    """A no-op containment gate — real containment is smoke_data_export.py's job."""
    return path


def test_run_query_writes_tsv() -> None:
    conn, _ = _fake_conn(["id", "name"], [(1, "alice"), (2, "bob")])
    with tempfile.TemporaryDirectory(prefix="epg_shape_") as workspace:
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


def test_run_query_duplicate_columns() -> None:
    # A JOIN like SELECT a.id, b.id yields two 'id' columns. The TSV header
    # preserves BOTH names positionally — a dict would collapse them.
    conn, _ = _fake_conn(["id", "id"], [(1, 2)])
    with tempfile.TemporaryDirectory(prefix="epg_shape_") as workspace:
        out_path = str(Path(workspace) / "out.tsv")
        result = query_actions.run_query(
            conn,
            {"sql": "SELECT a.id, b.id FROM a JOIN b ON true", "output_tsv_path": out_path},
            _passthrough_gate,
        )
        _assert("duplicate columns both present", result["columns"] == ["id", "id"])
        lines = Path(out_path).read_text(encoding="utf-8").splitlines()
        _assert("duplicate-column values NOT collapsed in the tsv row", lines[1] == "1\t2")


def test_run_query_guard() -> None:
    conn, _ = _fake_conn(["id"], [(1,)])
    with tempfile.TemporaryDirectory(prefix="epg_guard_") as workspace:
        out_path = str(Path(workspace) / "out.tsv")
        two = False
        try:
            query_actions.run_query(
                conn, {"sql": "SELECT 1; DELETE FROM t", "output_tsv_path": out_path}, _passthrough_gate,
            )
        except StatementGuardError:
            two = True
        _assert("two-statement run_query refused", two)
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
    result = query_actions.run_statement(
        conn, {"sql": "UPDATE t SET x = 1"}, _passthrough_gate,
    )
    _assert("no-result-set has_result_set False", result["has_result_set"] is False)
    _assert("no-result-set carries rowcount", "rowcount" in result)
    _assert("no path/columns/row_count keys when no result set", "path" not in result and "columns" not in result)
    _assert("no-result-set commits", conn.commit.called)
    _assert("no-result-set never rolls back", not conn.rollback.called)


def test_run_statement_with_returning_writes_tsv() -> None:
    conn, _ = _fake_conn(["id"], [(1,), (2,)])
    with tempfile.TemporaryDirectory(prefix="epg_stmt_") as workspace:
        out_path = str(Path(workspace) / "out.tsv")
        result = query_actions.run_statement(
            conn,
            {"sql": "INSERT INTO t (v) VALUES (1), (2) RETURNING id", "output_tsv_path": out_path},
            _passthrough_gate,
        )
        _assert("returning has_result_set True", result["has_result_set"] is True)
        _assert("returning carries path", result["path"] == out_path)
        _assert("returning carries row_count", result["row_count"] == 2)
        _assert("returning commits", conn.commit.called)
        lines = Path(out_path).read_text(encoding="utf-8").splitlines()
        _assert("tsv actually written", lines[0] == "id" and "1" in lines and "2" in lines)


def test_run_statement_returning_without_path_rolls_back() -> None:
    conn, _ = _fake_conn(["id"], [(1,)])
    raised = False
    try:
        query_actions.run_statement(
            conn, {"sql": "INSERT INTO t (v) VALUES (1) RETURNING id"}, _passthrough_gate,
        )
    except ValueError:
        raised = True
    _assert("missing output_tsv_path for a RETURNING statement raises", raised)
    _assert("rolls back rather than silently discarding rows", conn.rollback.called)
    _assert("never commits when rows would be discarded", not conn.commit.called)


def test_run_statement_single_statement_guard_only() -> None:
    conn, _ = _fake_conn(None, [])
    two = False
    try:
        query_actions.run_statement(
            conn, {"sql": "UPDATE t SET x = 1; UPDATE t SET x = 2"}, _passthrough_gate,
        )
    except StatementGuardError:
        two = True
    _assert("two-statement run_statement refused (shape guard only)", two)


def test_run_statement_no_read_leader_classification() -> None:
    # The read/write ACCESS-CONTROL guard (assert_read_statement) must NEVER
    # run on this path -- a write leader is not refused here, unlike run_query.
    conn, _ = _fake_conn(None, [])
    refused = False
    try:
        query_actions.run_statement(conn, {"sql": "DELETE FROM t"}, _passthrough_gate)
    except StatementGuardError:
        refused = True
    _assert("run_statement never applies the read-leader access-control guard", not refused)


def test_list_schemas() -> None:
    conn, cur = _fake_conn(["schema_name"], [("public",), ("analytics",)])
    result = query_actions.list_schemas(conn, {})
    _assert("schemas returned", result["schemas"] == ["public", "analytics"])
    passed = cur.execute.call_args.args[1][0]
    _assert("system schemas excluded via bound param", "pg_catalog" in passed and "information_schema" in passed)


def test_list_tables_bound_param() -> None:
    conn, cur = _fake_conn(["table_name", "table_type"], [("orders", "BASE TABLE")])
    result = query_actions.list_tables(conn, {"schema": "public"})
    _assert("tables shape", result["tables"] == [{"name": "orders", "kind": "BASE TABLE"}])
    _assert("schema passed as a BOUND parameter", cur.execute.call_args.args[1] == ("public",))


def test_describe_table() -> None:
    conn, _ = _fake_conn(
        ["column_name", "data_type", "is_nullable", "column_default"],
        [("id", "integer", "NO", "nextval(...)"), ("name", "text", "YES", None)],
    )
    cols = query_actions.describe_table(conn, {"schema": "public", "table": "t"})["columns"]
    _assert("column name/type", cols[0]["name"] == "id" and cols[0]["type"] == "integer")
    _assert("NO -> nullable False", cols[0]["nullable"] is False)
    _assert("YES -> nullable True", cols[1]["nullable"] is True)
    _assert("null default -> None", cols[1]["default"] is None)


def test_test_connection() -> None:
    conn, _ = _fake_conn(["version", "current_user", "tro"], [("PostgreSQL 17", "readonly", "on")])
    conn.info.host = "db.example.com"  # the resolved host libpq reports (conn.info.host)
    result = query_actions.test_connection(conn, {})
    _assert("ok True", result["ok"] is True)
    _assert("resolved host present", "host" in result)
    _assert("resolved host echoes conn.info.host", result["host"] == "db.example.com", str(result.get("host")))
    _assert("server_version carried", result["server_version"] == "PostgreSQL 17")
    _assert("read_only True when 'on'", result["read_only"] is True)


def test_classify_topology_leak() -> None:
    conn_err = _FakeDriverError("08006", f"connection to server at {_HOST_MARKER} port 5432 failed")
    code, message = connection.classify_pg_error(conn_err)
    _assert("connection-class -> api_error", code == "external_pg.api_error")
    _assert("connection message hides host marker", _HOST_MARKER not in message, message)

    auth_err = _FakeDriverError("28P01", f"password authentication failed for user {_USER_MARKER}")
    code, message = connection.classify_pg_error(auth_err)
    _assert("auth-class -> auth_failed", code == "external_pg.auth_failed")
    _assert("auth message hides user marker", _USER_MARKER not in message, message)

    perm_err = _FakeDriverError("42501", f"permission denied for relation on host {_HOST_MARKER}")
    _, message = connection.classify_pg_error(perm_err)
    _assert("permission message hides host marker", _HOST_MARKER not in message, message)

    syntax_err = _FakeDriverError("42601", f"raw str with {_HOST_MARKER}", primary='syntax error at or near "SELCT"')
    code, message = connection.classify_pg_error(syntax_err)
    _assert("syntax-class -> query_failed", code == "external_pg.query_failed")
    _assert("syntax carries the server's primary message", "syntax error" in message)
    _assert("syntax message uses message_primary, not raw str (no host)", _HOST_MARKER not in message, message)


def test_statement_timeout_bounds() -> None:
    # F2 (Codex rider): the statement_timeout is the non-disableable belt-tier DoS
    # bound. A 0/negative config value would DISABLE the server-side cancel, so the
    # parse fails LOUD (external_pg.not_configured) instead of opening a timeout-less
    # connection. Red-first: a regression that drops the bounds check fails here.
    plugin = ExternalPostgresPlugin()
    plugin.config_provider = {"statement_timeout_ms": "5000"}
    _assert("positive timeout parses", plugin._statement_timeout_ms() == 5000)
    plugin.config_provider = {}
    _assert("absent timeout uses the 30s default", plugin._statement_timeout_ms() == 30_000)
    for bad in ("0", "-1", "-30000"):
        plugin.config_provider = {"statement_timeout_ms": bad}
        code = ""
        try:
            plugin._statement_timeout_ms()
        except ExternalPgConfigError as exc:
            code = exc.code
        _assert(f"non-positive timeout {bad!r} refused fail-loud", code == "external_pg.not_configured")
    plugin.config_provider = {"statement_timeout_ms": "not-an-int"}
    code = ""
    try:
        plugin._statement_timeout_ms()
    except ExternalPgConfigError as exc:
        code = exc.code
    _assert("non-integer timeout refused fail-loud", code == "external_pg.not_configured")


def test_platform_pg_port_bounds() -> None:
    # F4 (Codex-class rider, coordinator-ruled GUARD-INTEGRITY): platform_pg_port
    # feeds the §8.4 containment guard's (host,port,dbname) INSTANCE compare — a
    # fat-fingered value would silently weaken the guard's port arm, so a
    # present-but-invalid value fails LOUD (not_configured). Same class as F2;
    # PORT range [1, 65535] (not merely > 0). Red-first: a regression that drops
    # the bounds check fails here.
    plugin = ExternalPostgresPlugin()
    plugin.config_provider = {"platform_pg_port": "5432"}
    _assert("valid port parses", plugin._platform_pg_port() == 5432)
    plugin.config_provider = {}
    _assert("absent port uses the 5432 default", plugin._platform_pg_port() == 5432)
    for bad in ("0", "-1", "65536", "not-an-int"):
        plugin.config_provider = {"platform_pg_port": bad}
        code = ""
        try:
            plugin._platform_pg_port()
        except ExternalPgConfigError as exc:
            code = exc.code
        _assert(f"invalid port {bad!r} refused fail-loud", code == "external_pg.not_configured")


def test_edge_parity() -> None:
    from ananta.core.plugins.action_discovery import discover_actions
    from ananta.core.process_registry.plugin_registration_validator import (
        PluginRegistrationValidator,
    )

    plugin = ExternalPostgresPlugin()
    actions = discover_actions(plugin)
    raised = None
    try:
        PluginRegistrationValidator().validate_edge_process_provider(
            "external_postgres_plugin", plugin, actions
        )
    except Exception as exc:  # FrameworkError on mismatch
        raised = exc
    _assert("EDGE parity: validator raises nothing", raised is None, str(raised))
    _assert("all 8 verbs discovered", len(actions) == 8)


def main() -> int:
    print("\nexternal_postgres_plugin query-verb smoke tests")
    print("=" * 47)
    test_run_query_writes_tsv()
    test_run_query_duplicate_columns()
    test_run_query_guard()
    test_run_statement_no_result_set_commits_inline()
    test_run_statement_with_returning_writes_tsv()
    test_run_statement_returning_without_path_rolls_back()
    test_run_statement_single_statement_guard_only()
    test_run_statement_no_read_leader_classification()
    test_list_schemas()
    test_list_tables_bound_param()
    test_describe_table()
    test_test_connection()
    test_classify_topology_leak()
    test_statement_timeout_bounds()
    test_platform_pg_port_bounds()
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
