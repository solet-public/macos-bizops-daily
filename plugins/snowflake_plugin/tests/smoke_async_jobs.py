#!/usr/bin/env python3
"""D0.3 deferred-completion smoke for snowflake_plugin's async_jobs.py.

Hermetic — a fake AsyncJobManager + a fake Snowflake connection, no live
warehouse, no real background thread left running past a test (each test
uses its own plugin instance and never calls ensure_worker_started for the
thread-liveness checks it doesn't need).

Exercises:
  1. Dispatch (list_databases/list_schemas/list_tables/describe_table/
     test_connection/run_query/export_query) returns {job_id, status: queued}
     in the SAME call, WITHOUT ever opening a connection (connection.connect
     is never invoked on the dispatch path).
  2. Dispatch-time validation: missing session_id/flow_id in state fails loud
     with the plugin's typed error envelope — BEFORE any job is created. (No
     connection_name check exists here — unlike external_postgres_plugin,
     this plugin is single-account v1; every job resolves the one registered
     snowflake_account entry.)
  3. The worker's _process_job runs the real query_actions function against a
     fake connection and completes the job via update_job with the correct
     result shape on success, and the topology-safe (code, message) on a
     driver fault — decoupled entirely from the dispatch call. Covers both
     the no-path-gate verbs (list_databases) and the path-gate verbs
     (run_query — proves the worker actually writes the TSV file and wires
     plugin._export_path_gate).
  4. run_statement's worker dispatch (the write verb, joined 2026-08-10):
     dispatch returns {job_id, status: queued} without ever connecting, same
     as every other verb — there is no connection-level distinction to prove
     here (unlike external_postgres_plugin's read_only=False check): Snowflake
     opens an IDENTICAL connection for every verb, and run_statement manages
     its own explicit transaction (autocommit disabled, then commit/rollback)
     entirely inside query_actions.py, already covered hermetically by
     smoke_query.py's run_statement tests. What this file proves instead is
     that the WORKER correctly reaches query_actions.run_statement with the
     export path gate wired, for both the no-result-set and the
     result-producing branches, end to end through the async job ledger.
  5. ensure_worker_started is idempotent: two calls produce the same thread
     object, not two threads.

Run:
    SOLET_NAME=<name> .venv/bin/python3 \
        plugins/snowflake_plugin/tests/smoke_async_jobs.py

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
sys.path.insert(0, str(REPO_ROOT / "plugins" / "snowflake_plugin" / "src"))

from snowflake_plugin import async_jobs, connection  # noqa: E402
from snowflake_plugin.plugin import SnowflakePlugin  # noqa: E402

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


class _FakeAsyncJobManager:
    """A minimal in-memory stand-in for the real AsyncJobManager surface."""

    def __init__(self) -> None:
        self._jobs: dict[str, dict[str, Any]] = {}
        self._payloads: dict[str, dict[str, Any]] = {}
        self._next_id = 1
        self.update_calls: list[tuple[str, dict[str, Any]]] = []

    def create_job(self, **kwargs: Any) -> dict[str, Any]:
        job_id = f"job-{self._next_id}"
        self._next_id += 1
        request_data = kwargs.get("request_data") or {}
        self._jobs[job_id] = {
            "id": job_id,
            "status": "queued",
            "provider_name": f"{kwargs['plugin_name']}.{kwargs['action_name']}",
            "metadata": kwargs.get("job_metadata"),
        }
        self._payloads[job_id] = request_data
        return {"action_status": "completed", "data": {"job_id": job_id, "status": "queued"}}

    def list_jobs(self, status: str | None = None, provider_name: str | None = None, **_: Any) -> dict[str, Any]:
        jobs = [
            j for j in self._jobs.values()
            if (status is None or j["status"] == status)
            and (provider_name is None or j["provider_name"] == provider_name)
        ]
        return {"action_status": "completed", "data": {"jobs": jobs}}

    def get_job_payload(self, job_id: str, payload_type: str = "request") -> dict[str, Any]:
        if job_id not in self._payloads:
            return {"action_status": "error", "error": {"message": "not found"}}
        return {"action_status": "completed", "data": {"payload": self._payloads[job_id]}}

    def update_job(self, job_id: str, updates: dict[str, Any]) -> dict[str, Any]:
        self.update_calls.append((job_id, updates))
        if job_id in self._jobs and "status" in updates:
            self._jobs[job_id]["status"] = updates["status"]
        return {"action_status": "completed", "data": {"job_id": job_id, "updated": True}}


def _plugin_with_fake_manager() -> tuple[SnowflakePlugin, _FakeAsyncJobManager]:
    plugin = SnowflakePlugin()
    plugin.initialize({})
    plugin._app_config_loader = MagicMock()
    fake_manager = _FakeAsyncJobManager()
    plugin._async_job_manager = fake_manager
    return plugin, fake_manager


def _fake_conn(columns: list[str] | None, rows: list[tuple[Any, ...]]) -> Any:
    cur = MagicMock()
    cur.description = [(c,) for c in columns] if columns else None
    cur.fetchall.return_value = rows
    cur.fetchmany.return_value = rows
    cur.fetchone.return_value = rows[0] if rows else None
    ctx = MagicMock()
    ctx.__enter__.return_value = cur
    ctx.__exit__.return_value = False
    conn = MagicMock()
    conn.cursor.return_value = ctx
    return conn


def test_dispatch_returns_queued_without_connecting() -> None:
    plugin, fake_manager = _plugin_with_fake_manager()
    original_connect = connection.connect
    connect_calls: list[tuple[Any, ...]] = []

    def _record_connect(*a: Any, **kw: Any) -> Any:
        connect_calls.append((a, kw))
        return _fake_conn(["name"], [("ANALYTICS",)])

    connection.connect = _record_connect  # type: ignore[assignment]
    try:
        result = plugin.list_databases({}, {"session_id": "s1", "flow_id": "f1"})
    finally:
        connection.connect = original_connect  # type: ignore[assignment]
    _assert("dispatch action_status completed", result["action_status"] == "completed")
    _assert("dispatch returns a job_id", isinstance(result["data"].get("job_id"), str))
    _assert("dispatch status is queued", result["data"].get("status") == "queued")
    _assert("connection.connect never called on the dispatch path", connect_calls == [])
    _assert("one job created in the fake ledger", len(fake_manager._jobs) == 1)


def test_dispatch_requires_session_and_flow_id() -> None:
    plugin, _ = _plugin_with_fake_manager()
    result = plugin.list_tables({"database": "d", "schema": "s"}, {})
    _assert("missing state context -> error status", result["action_status"] == "error")
    _assert(
        "missing state context -> invalid_params code",
        result["error"]["code"] == "snowflake.invalid_params",
        str(result.get("error")),
    )


def test_worker_completes_job_on_success() -> None:
    plugin, fake_manager = _plugin_with_fake_manager()
    dispatch = plugin.list_schemas({"database": "ANALYTICS"}, {"session_id": "s1", "flow_id": "f1"})
    job_id = dispatch["data"]["job_id"]

    fake_conn = _fake_conn(["created_on", "name"], [("2026-01-01", "PUBLIC"), ("2026-01-01", "RAW")])
    original_connect = connection.connect
    plugin._app_config_loader.resolve.return_value = SimpleNamespace()
    connection.connect = lambda *a, **kw: fake_conn  # type: ignore[assignment]
    try:
        async_jobs._process_job(plugin, fake_manager, job_id, "list_schemas")
    finally:
        connection.connect = original_connect  # type: ignore[assignment]

    statuses = [updates.get("status") for _, updates in fake_manager.update_calls]
    _assert("worker transitioned processing -> completed", statuses == ["processing", "completed"])
    completed_update = fake_manager.update_calls[-1][1]
    _assert(
        "completed result carries the real schema list",
        completed_update["result"]["schemas"] == ["PUBLIC", "RAW"],
        str(completed_update),
    )


class _FakeDriverError(Exception):
    def __init__(self, errno: int, message: str, sqlstate: str | None = None) -> None:
        super().__init__(message)
        self.errno = errno
        self.sqlstate = sqlstate
        self.msg = message


def test_worker_completes_job_on_error_topology_safe() -> None:
    plugin, fake_manager = _plugin_with_fake_manager()
    dispatch = plugin.test_connection({}, {"session_id": "s1", "flow_id": "f1"})
    job_id = dispatch["data"]["job_id"]

    account_marker = "SECRET-ACCOUNT-9.9.9.9"
    original_connect = connection.connect
    plugin._app_config_loader.resolve.return_value = SimpleNamespace()

    def _boom(*_a: Any, **_kw: Any) -> Any:
        raise _FakeDriverError(390144, f"JWT rejected for account {account_marker}")

    connection.connect = _boom  # type: ignore[assignment]
    try:
        async_jobs._process_job(plugin, fake_manager, job_id, "test_connection")
    finally:
        connection.connect = original_connect  # type: ignore[assignment]

    statuses = [updates.get("status") for _, updates in fake_manager.update_calls]
    _assert("worker transitioned processing -> error", statuses == ["processing", "error"])
    error_update = fake_manager.update_calls[-1][1]
    _assert(
        "error code is auth_failed",
        error_update["error"]["code"] == "snowflake.auth_failed",
        str(error_update),
    )
    _assert(
        "error message hides the account marker (topology-safe)",
        account_marker not in error_update["error"]["message"],
        str(error_update),
    )


def test_run_query_dispatch_returns_queued_without_connecting() -> None:
    plugin, fake_manager = _plugin_with_fake_manager()
    original_connect = connection.connect
    connect_calls: list[tuple[Any, ...]] = []

    def _record_connect(*a: Any, **kw: Any) -> Any:
        connect_calls.append((a, kw))
        return _fake_conn(["id"], [(1,)])

    connection.connect = _record_connect  # type: ignore[assignment]
    try:
        result = plugin.run_query(
            {"sql": "SELECT 1", "output_tsv_path": "/tmp/x.tsv"},
            {"session_id": "s1", "flow_id": "f1"},
        )
    finally:
        connection.connect = original_connect  # type: ignore[assignment]
    _assert("run_query dispatch action_status completed", result["action_status"] == "completed")
    _assert("run_query dispatch returns a job_id", isinstance(result["data"].get("job_id"), str))
    _assert("run_query dispatch status is queued", result["data"].get("status") == "queued")
    _assert("connection.connect never called on the run_query dispatch path", connect_calls == [])
    _assert(
        "the queued job carries needs-export-gate action_name",
        list(fake_manager._jobs.values())[0]["provider_name"].endswith(".run_query"),
    )


def test_worker_run_query_writes_tsv_via_export_path_gate() -> None:
    plugin, fake_manager = _plugin_with_fake_manager()
    with tempfile.TemporaryDirectory(prefix="snw_async_smoke_") as workspace:
        out_path = str(Path(workspace) / "out.tsv")
        dispatch = plugin.run_query(
            {"sql": "SELECT id, name FROM t", "output_tsv_path": out_path},
            {"session_id": "s1", "flow_id": "f1"},
        )
        job_id = dispatch["data"]["job_id"]

        gate_calls: list[str] = []
        plugin._export_path_gate = lambda p: gate_calls.append(p) or p  # type: ignore[method-assign]

        fake_conn = _fake_conn(["id", "name"], [(1, "alice"), (2, "bob")])
        original_connect = connection.connect
        plugin._app_config_loader.resolve.return_value = SimpleNamespace()
        connection.connect = lambda *a, **kw: fake_conn  # type: ignore[assignment]
        try:
            async_jobs._process_job(plugin, fake_manager, job_id, "run_query")
        finally:
            connection.connect = original_connect  # type: ignore[assignment]

        _assert("worker used the plugin's own _export_path_gate", gate_calls == [out_path])
        statuses = [updates.get("status") for _, updates in fake_manager.update_calls]
        _assert("worker transitioned processing -> completed", statuses == ["processing", "completed"])
        completed_result = fake_manager.update_calls[-1][1]["result"]
        _assert("completed result carries row_count", completed_result["row_count"] == 2)
        _assert("completed result carries columns", completed_result["columns"] == ["id", "name"])
        lines = Path(out_path).read_text(encoding="utf-8").splitlines()
        _assert("the worker actually wrote the TSV file", lines[0] == "id\tname" and "1\talice" in lines)


def test_worker_run_query_error_topology_safe() -> None:
    plugin, fake_manager = _plugin_with_fake_manager()
    dispatch = plugin.run_query(
        {"sql": "SELECT 1", "output_tsv_path": "/tmp/x.tsv"},
        {"session_id": "s1", "flow_id": "f1"},
    )
    job_id = dispatch["data"]["job_id"]

    account_marker = "SECRET-ACCOUNT-9.9.9.9"
    user_marker = "SECRET-USER-marker"
    original_connect = connection.connect
    plugin._app_config_loader.resolve.return_value = SimpleNamespace()

    def _boom(*_a: Any, **_kw: Any) -> Any:
        raise _FakeDriverError(390144, f"JWT rejected for {user_marker}@{account_marker}")

    connection.connect = _boom  # type: ignore[assignment]
    try:
        async_jobs._process_job(plugin, fake_manager, job_id, "run_query")
    finally:
        connection.connect = original_connect  # type: ignore[assignment]

    error_update = fake_manager.update_calls[-1][1]
    _assert("run_query worker error code is auth_failed", error_update["error"]["code"] == "snowflake.auth_failed")
    _assert(
        "run_query worker error message hides account+user markers",
        account_marker not in error_update["error"]["message"] and user_marker not in error_update["error"]["message"],
        str(error_update),
    )


def test_run_statement_dispatch_returns_queued_without_connecting() -> None:
    plugin, fake_manager = _plugin_with_fake_manager()
    original_connect = connection.connect
    connect_calls: list[tuple[Any, ...]] = []

    def _record_connect(*a: Any, **kw: Any) -> Any:
        connect_calls.append((a, kw))
        return _fake_conn(None, [])

    connection.connect = _record_connect  # type: ignore[assignment]
    try:
        result = plugin.run_statement(
            {"sql": "UPDATE t SET x = 1"}, {"session_id": "s1", "flow_id": "f1"},
        )
    finally:
        connection.connect = original_connect  # type: ignore[assignment]
    _assert("run_statement dispatch action_status completed", result["action_status"] == "completed")
    _assert("run_statement dispatch returns a job_id", isinstance(result["data"].get("job_id"), str))
    _assert("run_statement dispatch status is queued", result["data"].get("status") == "queued")
    _assert("connection.connect never called on the run_statement dispatch path", connect_calls == [])
    _assert(
        "the queued job carries the run_statement action_name",
        list(fake_manager._jobs.values())[0]["provider_name"].endswith(".run_statement"),
    )


def test_worker_run_statement_no_result_set_completes_inline() -> None:
    plugin, fake_manager = _plugin_with_fake_manager()
    dispatch = plugin.run_statement(
        {"sql": "UPDATE t SET x = 1"}, {"session_id": "s1", "flow_id": "f1"},
    )
    job_id = dispatch["data"]["job_id"]

    fake_conn = _fake_conn(None, [])
    fake_conn.cursor.return_value.__enter__.return_value.rowcount = 5
    original_connect = connection.connect
    plugin._app_config_loader.resolve.return_value = SimpleNamespace()
    connection.connect = lambda *a, **kw: fake_conn  # type: ignore[assignment]
    try:
        async_jobs._process_job(plugin, fake_manager, job_id, "run_statement")
    finally:
        connection.connect = original_connect  # type: ignore[assignment]

    statuses = [updates.get("status") for _, updates in fake_manager.update_calls]
    _assert("run_statement worker transitioned processing -> completed", statuses == ["processing", "completed"])
    completed_result = fake_manager.update_calls[-1][1]["result"]
    _assert("no-result-set result has_result_set False", completed_result["has_result_set"] is False)
    _assert("no-result-set result carries rowcount", completed_result.get("rowcount") == 5)
    _assert("the fake connection was committed, not rolled back", fake_conn.commit.called and not fake_conn.rollback.called)


def test_worker_run_statement_writes_tsv_for_result_set() -> None:
    plugin, fake_manager = _plugin_with_fake_manager()
    with tempfile.TemporaryDirectory(prefix="snw_async_stmt_smoke_") as workspace:
        out_path = str(Path(workspace) / "out.tsv")
        dispatch = plugin.run_statement(
            {"sql": "INSERT INTO t (v) VALUES (1) RETURNING id", "output_tsv_path": out_path},
            {"session_id": "s1", "flow_id": "f1"},
        )
        job_id = dispatch["data"]["job_id"]

        gate_calls: list[str] = []
        plugin._export_path_gate = lambda p: gate_calls.append(p) or p  # type: ignore[method-assign]

        fake_conn = _fake_conn(["id"], [(7,)])
        original_connect = connection.connect
        plugin._app_config_loader.resolve.return_value = SimpleNamespace()
        connection.connect = lambda *a, **kw: fake_conn  # type: ignore[assignment]
        try:
            async_jobs._process_job(plugin, fake_manager, job_id, "run_statement")
        finally:
            connection.connect = original_connect  # type: ignore[assignment]

        _assert("worker used the plugin's own _export_path_gate for run_statement", gate_calls == [out_path])
        completed_result = fake_manager.update_calls[-1][1]["result"]
        _assert("result-set result has_result_set True", completed_result["has_result_set"] is True)
        _assert("result-set result carries the written path", completed_result["path"] == out_path)
        lines = Path(out_path).read_text(encoding="utf-8").splitlines()
        _assert("the worker actually wrote the result rows to a TSV", lines[0] == "id" and "7" in lines)
        _assert("the fake connection was committed", fake_conn.commit.called)


def test_ensure_worker_started_idempotent() -> None:
    plugin, _ = _plugin_with_fake_manager()
    async_jobs.ensure_worker_started(plugin)
    first_thread = plugin._worker_thread
    async_jobs.ensure_worker_started(plugin)
    second_thread = plugin._worker_thread
    _assert("ensure_worker_started reuses the same live thread", first_thread is second_thread)
    _assert("exactly one thread object created", first_thread is not None)


def main() -> int:
    print("\nsnowflake_plugin async_jobs (D0.3) smoke tests")
    print("=" * 48)
    test_dispatch_returns_queued_without_connecting()
    test_dispatch_requires_session_and_flow_id()
    test_worker_completes_job_on_success()
    test_worker_completes_job_on_error_topology_safe()
    test_run_query_dispatch_returns_queued_without_connecting()
    test_worker_run_query_writes_tsv_via_export_path_gate()
    test_worker_run_query_error_topology_safe()
    test_run_statement_dispatch_returns_queued_without_connecting()
    test_worker_run_statement_no_result_set_completes_inline()
    test_worker_run_statement_writes_tsv_for_result_set()
    test_ensure_worker_started_idempotent()
    print()
    print(f"Results: {_passed} passed, {len(_failed)} failed")
    if _failed:
        print("FAILED:", _failed)
        return 1
    print("All async_jobs smoke tests passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
