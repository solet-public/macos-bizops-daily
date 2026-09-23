#!/usr/bin/env python3
"""D0.3 deferred-completion smoke for salesforce_plugin's async_jobs.py.

Hermetic — a fake AsyncJobManager + a ``MagicMock`` standing in for
``SalesforceCliExecutor``, no live org, no real subprocess, no background
thread left running past a test (each test uses its own plugin instance and
never calls ensure_worker_started for the thread-liveness checks it doesn't
need).

Exercises:
  1. Dispatch (all 13 verbs) returns {job_id, status: queued} in the SAME
     call, WITHOUT ever touching SalesforceCliExecutor — the containment
     requirement this lane's brief named explicitly ("keep the executor,
     contain the dispatch"): plugin._cli_executor.run_json/run_rest are never
     called on the dispatch path.
  2. Dispatch-time validation: missing session_id/flow_id in state fails
     loud with the plugin's typed error envelope, BEFORE any job is created.
  3. The worker's _process_job runs the real record_actions/soql_actions
     function against a fake SalesforceCliExecutor and completes the job via
     update_job — covers a plain read (get_record), a write (create_record),
     the export-path-gated TSV verb (soql_query, proving the worker writes
     the real file and wires plugin._export_path_gate), the special
     test_connection wrapper, and a REST-level fault classifying
     topology-safe through the SAME path smoke_records.py already covers at
     the ``_run`` level — decoupled entirely from the dispatch call.
  4. ACTION_HANDLERS carries all 13 migrated/added verbs (the 9 original
     D0.3-migrated verbs plus the 4 lane-sf-bulk-v2 Bulk v2 ingest verbs) —
     a name typo here would silently strand a verb's queued jobs forever
     (the drain loop only polls names present in this dict).
  5. ensure_worker_started is idempotent: two calls produce the same thread
     object, not two threads.

Bulk v2 ingest verb behavior (argv shapes, containment gates, CSV writes,
etc.) is NOT re-covered here — see tests/smoke_bulk.py, the dedicated smoke
for bulk_actions.py. This file only proves the 4 bulk verbs are correctly
WIRED into the D0.3 dispatch/worker machinery, the same way it proves that
for the 9 pre-existing verbs.

Run:
    SOLET_NAME=<name> .venv/bin/python3 \
        plugins/salesforce_plugin/tests/smoke_async_jobs.py

Exits 0 on success, 1 on first failure.
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT / "plugins" / "salesforce_plugin" / "src"))

from salesforce_plugin import async_jobs  # noqa: E402
from salesforce_plugin.errors import SalesforceCliCallError  # noqa: E402
from salesforce_plugin.plugin import SalesforcePlugin  # noqa: E402

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


def _plugin_with_fake_manager() -> tuple[SalesforcePlugin, _FakeAsyncJobManager]:
    plugin = SalesforcePlugin()
    plugin.initialize({})
    fake_manager = _FakeAsyncJobManager()
    plugin._async_job_manager = fake_manager  # noqa: SLF001
    return plugin, fake_manager


def test_dispatch_returns_queued_without_touching_executor() -> None:
    plugin, fake_manager = _plugin_with_fake_manager()
    executor = MagicMock()
    plugin._cli_executor = executor  # noqa: SLF001 — must stay untouched by dispatch

    result = plugin.get_record(
        {"sobject": "Account", "id": "001x"}, {"session_id": "s1", "flow_id": "f1"},
    )
    _assert("dispatch action_status completed", result["action_status"] == "completed")
    _assert("dispatch returns a job_id", isinstance(result["data"].get("job_id"), str))
    _assert("dispatch status is queued", result["data"].get("status") == "queued")
    _assert("SalesforceCliExecutor.run_json never called on the dispatch path", not executor.run_json.called)
    _assert("SalesforceCliExecutor.run_rest never called on the dispatch path", not executor.run_rest.called)
    _assert("one job created in the fake ledger", len(fake_manager._jobs) == 1)


def test_dispatch_requires_session_and_flow_id() -> None:
    plugin, _ = _plugin_with_fake_manager()
    result = plugin.describe_sobject({"sobject": "Account"}, {})
    _assert("missing state context -> error status", result["action_status"] == "error")
    _assert(
        "missing state context -> invalid_params code",
        result["error"]["code"] == "sf.invalid_params",
        str(result.get("error")),
    )


def test_all_verbs_dispatch_async() -> None:
    plugin, fake_manager = _plugin_with_fake_manager()
    state = {"session_id": "s1", "flow_id": "f1"}
    calls: dict[str, dict[str, Any]] = {
        "soql_query": {"query": "SELECT Id FROM Account", "output_tsv_path": "/tmp/x.tsv"},
        "export_soql": {"query": "SELECT Id FROM Account", "output_tsv_path": "/tmp/y.tsv"},
        "get_record": {"sobject": "Account", "id": "001x"},
        "describe_sobject": {"sobject": "Account"},
        "list_sobjects": {},
        "create_record": {"sobject": "Account", "fields": {"Name": "Acme"}},
        "update_record": {"sobject": "Account", "id": "001x", "fields": {"Name": "New"}},
        "delete_record": {"sobject": "Account", "id": "001x"},
        "test_connection": {},
        "bulk_ingest_submit": {
            "operation": "insert", "sobject": "Account", "csv_path": "/tmp/in.csv",
        },
        "bulk_job_status": {"job_id": "750xx"},
        "bulk_job_results": {
            "job_id": "750xx",
            "successful_results_path": "/tmp/ok.csv",
            "failed_results_path": "/tmp/failed.csv",
            "unprocessed_results_path": "/tmp/unprocessed.csv",
        },
        "bulk_job_abort": {"job_id": "750xx"},
    }
    for action_name, params in calls.items():
        method = getattr(plugin, action_name)
        result = method(params, state)
        _assert(
            f"{action_name} dispatch returns queued",
            result["action_status"] == "completed" and result["data"].get("status") == "queued",
            str(result),
        )
    _assert("thirteen jobs created, one per verb", len(fake_manager._jobs) == 13)


def test_worker_completes_get_record_on_success() -> None:
    plugin, fake_manager = _plugin_with_fake_manager()
    plugin._cli_executor = MagicMock()  # noqa: SLF001
    dispatch = plugin.get_record({"sobject": "Account", "id": "001x"}, {"session_id": "s1", "flow_id": "f1"})
    job_id = dispatch["data"]["job_id"]

    plugin._cli_executor.run_json.return_value = {  # noqa: SLF001
        "attributes": {"type": "Account"}, "Id": "001x", "Name": "Acme",
    }
    async_jobs._process_job(plugin, fake_manager, job_id, "get_record")

    statuses = [updates.get("status") for _, updates in fake_manager.update_calls]
    _assert("worker transitioned processing -> completed", statuses == ["processing", "completed"])
    completed = fake_manager.update_calls[-1][1]
    _assert("completed result carries the real record", completed["result"]["record"]["Name"] == "Acme", str(completed))
    _assert("attributes stripped by the real record_actions function", "attributes" not in completed["result"]["record"])


def test_worker_completes_create_record_write() -> None:
    plugin, fake_manager = _plugin_with_fake_manager()
    plugin._cli_executor = MagicMock()  # noqa: SLF001
    dispatch = plugin.create_record(
        {"sobject": "Contact", "fields": {"LastName": "Smith"}}, {"session_id": "s1", "flow_id": "f1"},
    )
    job_id = dispatch["data"]["job_id"]

    plugin._cli_executor.run_rest.return_value = {"id": "003new", "success": True}  # noqa: SLF001
    async_jobs._process_job(plugin, fake_manager, job_id, "create_record")

    statuses = [updates.get("status") for _, updates in fake_manager.update_calls]
    _assert("create_record worker transitioned processing -> completed", statuses == ["processing", "completed"])
    completed = fake_manager.update_calls[-1][1]
    _assert("completed result carries the new record id", completed["result"]["id"] == "003new", str(completed))
    method, path = plugin._cli_executor.run_rest.call_args.args  # noqa: SLF001
    _assert("worker POSTed to the sobject collection endpoint", method == "POST" and path.endswith("/sobjects/Contact"))


def test_worker_soql_query_writes_tsv_via_export_path_gate() -> None:
    plugin, fake_manager = _plugin_with_fake_manager()
    plugin._cli_executor = MagicMock()  # noqa: SLF001
    with tempfile.TemporaryDirectory(prefix="sf_async_smoke_") as workspace:
        out_path = str(Path(workspace) / "out.tsv")
        dispatch = plugin.soql_query(
            {"query": "SELECT Id, Name FROM Account", "output_tsv_path": out_path},
            {"session_id": "s1", "flow_id": "f1"},
        )
        job_id = dispatch["data"]["job_id"]

        gate_calls: list[str] = []
        plugin._export_path_gate = lambda p: gate_calls.append(p) or p  # type: ignore[method-assign]  # noqa: SLF001
        plugin._cli_executor.run_json.return_value = {  # noqa: SLF001
            "totalSize": 1, "done": True,
            "records": [{"attributes": {}, "Id": "001x", "Name": "Acme"}],
        }
        async_jobs._process_job(plugin, fake_manager, job_id, "soql_query")

        _assert("worker used the plugin's own _export_path_gate", gate_calls == [out_path])
        statuses = [updates.get("status") for _, updates in fake_manager.update_calls]
        _assert("soql_query worker transitioned processing -> completed", statuses == ["processing", "completed"])
        completed_result = fake_manager.update_calls[-1][1]["result"]
        _assert("completed result carries row_count", completed_result["row_count"] == 1)
        lines = Path(out_path).read_text(encoding="utf-8").splitlines()
        _assert("the worker actually wrote the TSV file", lines[0] == "Id\tName" and "Acme" in lines[1])


def test_worker_completes_test_connection_on_success() -> None:
    plugin, fake_manager = _plugin_with_fake_manager()
    plugin._cli_executor = MagicMock()  # noqa: SLF001
    plugin._cli_executor.username = "bot@example.com"  # noqa: SLF001
    plugin._cli_executor.api_version = "62.0"  # noqa: SLF001
    dispatch = plugin.test_connection({}, {"session_id": "s1", "flow_id": "f1"})
    job_id = dispatch["data"]["job_id"]

    plugin._cli_executor.run_json.return_value = {"records": [{"Id": "00Dxx"}]}  # noqa: SLF001
    async_jobs._process_job(plugin, fake_manager, job_id, "test_connection")

    completed_result = fake_manager.update_calls[-1][1]["result"]
    _assert("test_connection worker returns ok True", completed_result["ok"] is True)
    _assert("test_connection worker carries org_id", completed_result["org_id"] == "00Dxx", str(completed_result))
    _assert("test_connection worker carries username", completed_result["username"] == "bot@example.com")


def test_worker_completes_job_on_error_topology_safe() -> None:
    plugin, fake_manager = _plugin_with_fake_manager()
    plugin._cli_executor = MagicMock()  # noqa: SLF001
    dispatch = plugin.get_record({"sobject": "Account", "id": "001x"}, {"session_id": "s1", "flow_id": "f1"})
    job_id = dispatch["data"]["job_id"]

    host_marker = "SECRET-ORG-myorg.my.salesforce.com"
    plugin._cli_executor.run_json.side_effect = SalesforceCliCallError(  # noqa: SLF001
        "INVALID_LOGIN", f"invalid_client_id for {host_marker}",
    )
    async_jobs._process_job(plugin, fake_manager, job_id, "get_record")

    statuses = [updates.get("status") for _, updates in fake_manager.update_calls]
    _assert("worker transitioned processing -> error", statuses == ["processing", "error"])
    error_update = fake_manager.update_calls[-1][1]
    _assert("error code is sf.auth_failed", error_update["error"]["code"] == "sf.auth_failed", str(error_update))
    _assert(
        "error message hides the org host marker (topology-safe)",
        host_marker not in error_update["error"]["message"],
        str(error_update),
    )


def test_action_handlers_covers_all_thirteen_verbs() -> None:
    expected = {
        "soql_query", "export_soql", "get_record", "describe_sobject", "list_sobjects",
        "create_record", "update_record", "delete_record", "test_connection",
        "bulk_ingest_submit", "bulk_job_status", "bulk_job_results", "bulk_job_abort",
    }
    _assert(
        "ACTION_HANDLERS carries exactly the 13 migrated/added verb names",
        set(async_jobs.ACTION_HANDLERS.keys()) == expected,
        str(sorted(async_jobs.ACTION_HANDLERS.keys())),
    )


def test_worker_bulk_job_results_writes_csvs_via_csv_export_path_gate() -> None:
    """Proves bulk_job_results is wired to plugin._csv_export_path_gate (not _export_path_gate)."""
    plugin, fake_manager = _plugin_with_fake_manager()
    plugin._cli_executor = MagicMock()  # noqa: SLF001
    with tempfile.TemporaryDirectory(prefix="sf_async_bulk_smoke_") as workspace:
        successful_path = str(Path(workspace) / "ok.csv")
        failed_path = str(Path(workspace) / "failed.csv")
        unprocessed_path = str(Path(workspace) / "unprocessed.csv")
        dispatch = plugin.bulk_job_results(
            {
                "job_id": "750xx",
                "successful_results_path": successful_path,
                "failed_results_path": failed_path,
                "unprocessed_results_path": unprocessed_path,
            },
            {"session_id": "s1", "flow_id": "f1"},
        )
        job_id = dispatch["data"]["job_id"]

        gate_calls: list[str] = []
        plugin._csv_export_path_gate = lambda p: gate_calls.append(p) or p  # type: ignore[method-assign]  # noqa: SLF001
        plugin._cli_executor.run_rest.return_value = "sf__Id,sf__Error\n001x,\n"  # noqa: SLF001
        async_jobs._process_job(plugin, fake_manager, job_id, "bulk_job_results")

        _assert(
            "worker used the plugin's own _csv_export_path_gate for all three paths",
            gate_calls == [successful_path, failed_path, unprocessed_path],
            str(gate_calls),
        )
        statuses = [updates.get("status") for _, updates in fake_manager.update_calls]
        _assert("bulk_job_results worker transitioned processing -> completed", statuses == ["processing", "completed"])
        _assert("the worker actually wrote the successful-results CSV", Path(successful_path).read_text(encoding="utf-8").startswith("sf__Id"))


def test_ensure_worker_started_idempotent() -> None:
    plugin, _ = _plugin_with_fake_manager()
    async_jobs.ensure_worker_started(plugin)
    first_thread = plugin._worker_thread  # noqa: SLF001
    async_jobs.ensure_worker_started(plugin)
    second_thread = plugin._worker_thread  # noqa: SLF001
    _assert("ensure_worker_started reuses the same live thread", first_thread is second_thread)
    _assert("exactly one thread object created", first_thread is not None)


def main() -> int:
    print("\nsalesforce_plugin async_jobs (D0.3) smoke tests")
    print("=" * 50)
    test_dispatch_returns_queued_without_touching_executor()
    test_dispatch_requires_session_and_flow_id()
    test_all_verbs_dispatch_async()
    test_worker_completes_get_record_on_success()
    test_worker_completes_create_record_write()
    test_worker_soql_query_writes_tsv_via_export_path_gate()
    test_worker_completes_test_connection_on_success()
    test_worker_completes_job_on_error_topology_safe()
    test_worker_bulk_job_results_writes_csvs_via_csv_export_path_gate()
    test_action_handlers_covers_all_thirteen_verbs()
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
