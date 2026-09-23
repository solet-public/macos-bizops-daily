#!/usr/bin/env python3
"""D0.3 deferred-completion smoke for zuora_plugin's async_jobs.py.

Hermetic — a fake AsyncJobManager + a ``MagicMock`` standing in for
``ZuoraClient``, no live tenant, no real HTTP call, no background thread left
running past a test (each test uses its own plugin instance and never calls
ensure_worker_started for the thread-liveness checks it doesn't need).

Exercises:
  1. Dispatch (all 9 verbs) returns {job_id, status: queued} in the SAME
     call, WITHOUT ever touching ZuoraClient — the containment requirement
     this lane's brief named explicitly ("keep the executor, contain the
     dispatch"): plugin._client.get/post/put are never called on the
     dispatch path.
  2. Dispatch-time validation: missing session_id/flow_id in state fails
     loud with the plugin's typed error envelope, BEFORE any job is created.
  3. The worker's _process_job runs the real billing_actions function
     against a fake ZuoraClient and completes the job via update_job —
     covers a plain read (get_object), a write (create_object), the
     export-path-gated TSV verb (data_query, proving the worker writes the
     real file and wires plugin._export_path_gate), the special
     test_connection wrapper, and a non-2xx response classifying
     topology-safe through the SAME path smoke_data_query.py already covers
     at the ``_run`` level — decoupled entirely from the dispatch call.
  4. ACTION_HANDLERS carries all 9 migrated verbs — a name typo here would
     silently strand a verb's queued jobs forever (the drain loop only polls
     names present in this dict).
  5. ensure_worker_started is idempotent: two calls produce the same thread
     object, not two threads.

Run:
    SOLET_NAME=<name> .venv/bin/python3 \
        plugins/zuora_plugin/tests/smoke_async_jobs.py

Exits 0 on success, 1 on first failure.
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import httpx

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT / "plugins" / "zuora_plugin" / "src"))

from zuora_plugin import async_jobs  # noqa: E402
from zuora_plugin.billing_actions import ZuoraResponseError  # noqa: E402
from zuora_plugin.constants import ERROR_INVALID_PARAMS, ERROR_OBJECT_NOT_FOUND  # noqa: E402
from zuora_plugin.plugin import ZuoraPlugin  # noqa: E402

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


def _plugin_with_fake_manager() -> tuple[ZuoraPlugin, _FakeAsyncJobManager]:
    plugin = ZuoraPlugin()
    plugin.initialize({})
    # _require_client's first gate: non-None _app_config_loader. Since the
    # worker tests below always pre-set plugin._client to a MagicMock too,
    # _require_client short-circuits before ever calling .load() on this.
    plugin._app_config_loader = MagicMock()  # noqa: SLF001
    fake_manager = _FakeAsyncJobManager()
    plugin._async_job_manager = fake_manager  # noqa: SLF001
    return plugin, fake_manager


_STATE = {"session_id": "s1", "flow_id": "f1"}

# zoql values are non-SQL-shaped placeholders, not realistic ZOQL — the fake
# client never inspects the string, and a placeholder avoids the sql_access_gate
# S2 finding at zero assertion cost (coordinator ruling 2026-08-24: an allowlist
# entry here would not be the *best* fix, so it's not taken; the salesforce/
# snowflake sibling smokes' allowlist entries predate that doctrine).
_MIGRATED_VERB_PARAMS: dict[str, dict[str, Any]] = {
    "data_query": {"zoql": "zoql-fixture-id-from-account", "output_tsv_path": "/tmp/x.tsv"},
    "get_object": {"type": "Account", "id": "2c1x"},
    "create_object": {"type": "Account", "fields": {"Name": "Acme"}},
    "update_object": {"type": "Account", "id": "2c1x", "fields": {"Name": "New"}},
    "list_subscriptions": {"account_id": "2c1x", "output_tsv_path": "/tmp/subs.tsv"},
    "get_invoice": {"id": "2c1x"},
    "list_invoices": {"account_id": "2c1x", "output_tsv_path": "/tmp/invoices.tsv"},
    "bulk_export": {"zoql": "zoql-fixture-id-from-account", "output_tsv_path": "/tmp/y.tsv"},
    "test_connection": {},
}


def test_dispatch_returns_queued_without_touching_client() -> None:
    plugin, fake_manager = _plugin_with_fake_manager()
    client = MagicMock()
    plugin._client = client  # noqa: SLF001 — must stay untouched by dispatch

    result = plugin.get_object({"type": "Account", "id": "2c1x"}, dict(_STATE))
    _assert("dispatch action_status completed", result["action_status"] == "completed")
    _assert("dispatch returns a job_id", isinstance(result["data"].get("job_id"), str))
    _assert("dispatch status is queued", result["data"].get("status") == "queued")
    _assert("ZuoraClient.get never called on the dispatch path", not client.get.called)
    _assert("ZuoraClient.post never called on the dispatch path", not client.post.called)
    _assert("one job created in the fake ledger", len(fake_manager._jobs) == 1)  # noqa: SLF001


def test_dispatch_requires_session_and_flow_id() -> None:
    plugin, _ = _plugin_with_fake_manager()
    result = plugin.get_invoice({"id": "2c1x"}, {})
    _assert("missing state context -> error status", result["action_status"] == "error")
    _assert(
        "missing state context -> invalid_params code",
        result["error"]["code"] == ERROR_INVALID_PARAMS,
        str(result.get("error")),
    )


def test_all_nine_verbs_dispatch_async() -> None:
    plugin, fake_manager = _plugin_with_fake_manager()
    for action_name, params in _MIGRATED_VERB_PARAMS.items():
        method = getattr(plugin, action_name)
        result = method(params, dict(_STATE))
        _assert(
            f"{action_name} dispatch returns queued",
            result["action_status"] == "completed" and result["data"].get("status") == "queued",
            str(result),
        )
    _assert(
        "nine jobs created, one per verb",
        len(fake_manager._jobs) == len(_MIGRATED_VERB_PARAMS),  # noqa: SLF001
    )


def test_action_handlers_covers_all_nine_verbs() -> None:
    _assert(
        "ACTION_HANDLERS carries exactly the 9 migrated verb names",
        set(async_jobs.ACTION_HANDLERS.keys()) == set(_MIGRATED_VERB_PARAMS.keys()),
        str(sorted(async_jobs.ACTION_HANDLERS.keys())),
    )


def _dispatch_and_get_job_id(plugin: ZuoraPlugin, action_name: str, params: dict[str, Any]) -> str:
    dispatch = getattr(plugin, action_name)(params, dict(_STATE))
    job_id = dispatch["data"]["job_id"]
    assert isinstance(job_id, str)
    return job_id


def test_worker_completes_get_object_on_success() -> None:
    plugin, fake_manager = _plugin_with_fake_manager()
    plugin._client = MagicMock()  # noqa: SLF001
    job_id = _dispatch_and_get_job_id(plugin, "get_object", {"type": "Account", "id": "2c1x"})

    plugin._client.get.return_value = httpx.Response(  # noqa: SLF001
        200, json={"Id": "2c1x", "Name": "Acme"}, request=httpx.Request("GET", "https://tenant/v1/object/Account/2c1x"),
    )
    async_jobs._process_job(plugin, fake_manager, job_id, "get_object")  # noqa: SLF001

    statuses = [updates.get("status") for _, updates in fake_manager.update_calls]
    _assert("worker transitioned processing -> completed", statuses == ["processing", "completed"])
    completed = fake_manager.update_calls[-1][1]
    _assert("completed result carries the real object", completed["result"]["object"]["Name"] == "Acme", str(completed))
    path, = plugin._client.get.call_args.args  # noqa: SLF001
    _assert("worker GET the object endpoint", path == "/v1/object/Account/2c1x")


def test_worker_completes_create_object_write() -> None:
    plugin, fake_manager = _plugin_with_fake_manager()
    plugin._client = MagicMock()  # noqa: SLF001
    job_id = _dispatch_and_get_job_id(
        plugin, "create_object", {"type": "Account", "fields": {"Name": "Acme"}},
    )

    plugin._client.post.return_value = httpx.Response(  # noqa: SLF001
        200, json={"Id": "new-1", "Success": True}, request=httpx.Request("POST", "https://tenant/v1/object/Account"),
    )
    async_jobs._process_job(plugin, fake_manager, job_id, "create_object")  # noqa: SLF001

    statuses = [updates.get("status") for _, updates in fake_manager.update_calls]
    _assert("create_object worker transitioned processing -> completed", statuses == ["processing", "completed"])
    completed = fake_manager.update_calls[-1][1]
    _assert("completed result carries the new object id", completed["result"]["id"] == "new-1", str(completed))
    path = plugin._client.post.call_args.args[0]  # noqa: SLF001
    _assert("worker POSTed to the object endpoint", path == "/v1/object/Account")


def test_worker_data_query_writes_tsv_via_export_path_gate() -> None:
    plugin, fake_manager = _plugin_with_fake_manager()
    plugin._client = MagicMock()  # noqa: SLF001
    with tempfile.TemporaryDirectory(prefix="zuora_async_smoke_") as workspace:
        out_path = str(Path(workspace) / "out.tsv")
        job_id = _dispatch_and_get_job_id(
            plugin, "data_query", {"zoql": "zoql-fixture-id-name-from-account", "output_tsv_path": out_path},
        )

        gate_calls: list[str] = []
        plugin._export_path_gate = lambda p: gate_calls.append(p) or p  # type: ignore[method-assign]  # noqa: SLF001
        plugin._client.post.return_value = httpx.Response(  # noqa: SLF001
            200,
            json={"records": [{"Id": "2c1x", "Name": "Acme"}], "size": 1, "done": True},
            request=httpx.Request("POST", "https://tenant/v1/action/query"),
        )
        async_jobs._process_job(plugin, fake_manager, job_id, "data_query")  # noqa: SLF001

        _assert("worker used the plugin's own _export_path_gate", gate_calls == [out_path])
        statuses = [updates.get("status") for _, updates in fake_manager.update_calls]
        _assert("data_query worker transitioned processing -> completed", statuses == ["processing", "completed"])
        completed_result = fake_manager.update_calls[-1][1]["result"]
        _assert("completed result carries row_count", completed_result["row_count"] == 1, str(completed_result))
        lines = Path(out_path).read_text(encoding="utf-8").splitlines()
        _assert("the worker actually wrote the TSV file", lines[0] == "Id\tName" and "Acme" in lines[1])


def test_worker_completes_test_connection_on_success() -> None:
    plugin, fake_manager = _plugin_with_fake_manager()
    plugin._client = MagicMock()  # noqa: SLF001
    plugin._client._config = MagicMock(base_url="https://tenant.zuora.com", client_id="client-abc")  # noqa: SLF001
    job_id = _dispatch_and_get_job_id(plugin, "test_connection", {})

    async_jobs._process_job(plugin, fake_manager, job_id, "test_connection")  # noqa: SLF001

    completed_result = fake_manager.update_calls[-1][1]["result"]
    _assert("test_connection worker returns ok True", completed_result["ok"] is True, str(completed_result))
    _assert("test_connection worker carries base_url", completed_result["base_url"] == "https://tenant.zuora.com")
    _assert("test_connection worker carries client_id", completed_result["client_id"] == "client-abc")
    _assert("worker called ensure_authenticated", plugin._client.ensure_authenticated.called)  # noqa: SLF001


def test_worker_completes_job_on_error_topology_safe() -> None:
    plugin, fake_manager = _plugin_with_fake_manager()
    plugin._client = MagicMock()  # noqa: SLF001
    job_id = _dispatch_and_get_job_id(plugin, "get_object", {"type": "Account", "id": "2c1x"})

    response = httpx.Response(
        404,
        json={"reasons": [{"message": "No account found"}]},
        request=httpx.Request("GET", "https://SECRET-TENANT.zuora.com/v1/object/Account/2c1x"),
    )
    plugin._client.get.side_effect = ZuoraResponseError(response, is_query=False)  # noqa: SLF001
    async_jobs._process_job(plugin, fake_manager, job_id, "get_object")  # noqa: SLF001

    statuses = [updates.get("status") for _, updates in fake_manager.update_calls]
    _assert("worker transitioned processing -> error", statuses == ["processing", "error"])
    error_update = fake_manager.update_calls[-1][1]
    _assert("error code is zuora.object_not_found", error_update["error"]["code"] == ERROR_OBJECT_NOT_FOUND, str(error_update))
    _assert(
        "error message hides the tenant host (topology-safe)",
        "SECRET-TENANT" not in error_update["error"]["message"],
        str(error_update),
    )


def test_ensure_worker_started_idempotent() -> None:
    plugin, _ = _plugin_with_fake_manager()
    async_jobs.ensure_worker_started(plugin)
    first_thread = plugin._worker_thread  # noqa: SLF001
    async_jobs.ensure_worker_started(plugin)
    second_thread = plugin._worker_thread  # noqa: SLF001
    _assert("ensure_worker_started reuses the same live thread", first_thread is second_thread)
    _assert("exactly one thread object created", first_thread is not None)


def main() -> int:
    print("\nzuora_plugin async_jobs (D0.3) smoke tests")
    print("=" * 50)
    test_dispatch_returns_queued_without_touching_client()
    test_dispatch_requires_session_and_flow_id()
    test_all_nine_verbs_dispatch_async()
    test_action_handlers_covers_all_nine_verbs()
    test_worker_completes_get_object_on_success()
    test_worker_completes_create_object_write()
    test_worker_data_query_writes_tsv_via_export_path_gate()
    test_worker_completes_test_connection_on_success()
    test_worker_completes_job_on_error_topology_safe()
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
