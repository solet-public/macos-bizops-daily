#!/usr/bin/env python3
"""D0.3 deferred-completion smoke for g_suite_plugin's dispatch->worker->completion wiring.

Hermetic — a fake AsyncJobManager, a faked token store, and monkeypatched
drive_actions/sheets_actions functions. No live Google API, no real
background thread left running past a test (start_services/stop_services are
always paired inside a try/finally). Modeled on
external_postgres_plugin/tests/smoke_async_jobs.py.

Exercises:
  1. Dispatch (drive_download_file/drive_upload_file/sheets_create_from_files)
     returns {job_id, status: queued} in the SAME call, WITHOUT ever calling
     the real drive_actions/sheets_actions function (the dispatch path never
     touches Google).
  2. Dispatch-time validation: missing session_id/flow_id in state, and no
     connected account, both fail loud with the plugin's typed error
     envelope BEFORE any job is created.
  3. The worker's _process_job runs the real action function against a faked
     Google client and completes the job via update_job with the correct
     result shape on success, and a typed (code, message) on a raised
     exception — decoupled entirely from the dispatch call.
  4. start_services/stop_services are idempotent: two consecutive
     start_services calls produce the same worker thread object, not two.

Run:
    SOLET_NAME=<name> .venv/bin/python3 \
        plugins/g_suite_plugin/tests/smoke_async_jobs.py

Exits 0 on success, 1 on first failure.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT / "plugins" / "g_suite_plugin" / "src"))

from g_suite_plugin import drive_actions  # noqa: E402
from g_suite_plugin.plugin import GSuitePlugin  # noqa: E402
from googleapiclient.errors import HttpError  # noqa: E402

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

    def list_jobs(
        self, status: str | None = None, provider_name: str | None = None, **_: Any
    ) -> dict[str, Any]:
        jobs = [
            j
            for j in self._jobs.values()
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


def _plugin_with_fake_manager() -> tuple[GSuitePlugin, _FakeAsyncJobManager]:
    plugin = GSuitePlugin()
    plugin.initialize({})
    plugin._token_store = SimpleNamespace(is_connected=lambda: True)
    plugin._service_factory = SimpleNamespace(drive=lambda: "fake-drive", sheets=lambda: "fake-sheets")
    fake_manager = _FakeAsyncJobManager()
    plugin._deferred.manager = fake_manager
    return plugin, fake_manager


def test_dispatch_returns_queued_without_calling_google() -> None:
    plugin, fake_manager = _plugin_with_fake_manager()
    calls: list[Any] = []
    original = drive_actions.download_file
    drive_actions.download_file = lambda *a, **kw: calls.append((a, kw)) or {}  # type: ignore[assignment]
    try:
        result = plugin.drive_download_file({"id": "f1"}, {"session_id": "s1", "flow_id": "fl1"})
    finally:
        drive_actions.download_file = original  # type: ignore[assignment]
    _assert("dispatch action_status completed", result["action_status"] == "completed")
    _assert("dispatch returns a job_id", isinstance(result["data"].get("job_id"), str))
    _assert("dispatch status is queued", result["data"].get("status") == "queued")
    _assert("drive_actions.download_file never called on the dispatch path", calls == [])
    _assert("one job created in the fake ledger", len(fake_manager._jobs) == 1)


def test_dispatch_requires_session_and_flow_id() -> None:
    plugin, _ = _plugin_with_fake_manager()
    result = plugin.drive_upload_file({"name": "x", "blob_key": "b1"}, {})
    _assert("missing state context -> error status", result["action_status"] == "error")
    _assert(
        "missing state context -> invalid_params code",
        result["error"]["code"] == "gsuite.invalid_params",
        str(result.get("error")),
    )


def test_dispatch_requires_connected_account() -> None:
    plugin, _ = _plugin_with_fake_manager()
    plugin._token_store = SimpleNamespace(is_connected=lambda: False)
    result = plugin.sheets_create_from_files(
        {"title": "t", "tabs": []}, {"session_id": "s1", "flow_id": "fl1"}
    )
    _assert("no connected account -> error status", result["action_status"] == "error")
    _assert(
        "no connected account -> not_connected code",
        result["error"]["code"] == "gsuite.not_connected",
        str(result.get("error")),
    )


def test_worker_completes_job_on_success() -> None:
    plugin, fake_manager = _plugin_with_fake_manager()
    dispatch = plugin.drive_download_file({"id": "f1"}, {"session_id": "s1", "flow_id": "fl1"})
    job_id = dispatch["data"]["job_id"]

    original = drive_actions.download_file
    drive_actions.download_file = lambda *a, **kw: {  # type: ignore[assignment]
        "file_blob_key": "blob-123",
        "name": "report.pdf",
        "mime": "application/pdf",
    }
    try:
        plugin._process_job({"id": job_id})
    finally:
        drive_actions.download_file = original  # type: ignore[assignment]

    statuses = [updates.get("status") for _, updates in fake_manager.update_calls]
    _assert("worker transitioned processing -> completed", statuses == ["processing", "completed"])
    completed_update = fake_manager.update_calls[-1][1]
    _assert(
        "completed result carries the real file_blob_key",
        completed_update["result"]["file_blob_key"] == "blob-123",
        str(completed_update),
    )


def test_worker_completes_job_on_error() -> None:
    plugin, fake_manager = _plugin_with_fake_manager()
    dispatch = plugin.drive_upload_file(
        {"name": "x", "blob_key": "b1"}, {"session_id": "s1", "flow_id": "fl1"}
    )
    job_id = dispatch["data"]["job_id"]

    original = drive_actions.upload_file
    resp = SimpleNamespace(status=404, reason="Not Found")

    def _boom(*_a: Any, **_kw: Any) -> Any:
        raise HttpError(resp, b"not found")

    drive_actions.upload_file = _boom  # type: ignore[assignment]
    try:
        plugin._process_job({"id": job_id})
    finally:
        drive_actions.upload_file = original  # type: ignore[assignment]

    statuses = [updates.get("status") for _, updates in fake_manager.update_calls]
    _assert("worker transitioned processing -> error", statuses == ["processing", "error"])
    error_update = fake_manager.update_calls[-1][1]
    _assert(
        "HttpError 404 classifies as not_found",
        error_update["error"]["code"] == "gsuite.not_found",
        str(error_update),
    )


def test_all_migrated_verbs_have_a_registered_producer() -> None:
    plugin, _ = _plugin_with_fake_manager()
    producers = plugin._deferred_producers()
    for verb in (
        "gmail_list_messages",
        "gmail_get_message",
        "gmail_send",
        "drive_download_file",
        "drive_upload_file",
        "drive_list_files",
        "drive_create_folder",
        "drive_share",
        "sheets_create_from_files",
        "sheets_create",
        "sheets_get_values",
        "sheets_update_values",
        "sheets_append_values",
        "sheets_batch_update",
        "slides_create",
        "slides_get",
        "slides_batch_update",
        "docs_create",
        "docs_get",
        "docs_batch_update",
    ):
        _assert(f"{verb} has a registered deferred producer", verb in producers)


def test_worker_unknown_verb_fails_loud() -> None:
    plugin, fake_manager = _plugin_with_fake_manager()
    job_id = fake_manager.create_job(
        plugin_name="g_suite_plugin", action_name="workspace_job", request_data={"verb": "no_such_verb", "params": {}}
    )["data"]["job_id"]
    plugin._process_job({"id": job_id})
    error_update = fake_manager.update_calls[-1][1]
    _assert(
        "unregistered verb classifies as invalid_params",
        error_update["error"]["code"] == "gsuite.invalid_params",
        str(error_update),
    )


def test_start_stop_services_idempotent() -> None:
    plugin, _ = _plugin_with_fake_manager()
    try:
        asyncio.run(plugin.start_services())
        first_thread = plugin._deferred.thread
        asyncio.run(plugin.start_services())
        second_thread = plugin._deferred.thread
        _assert("start_services reuses the same live thread", first_thread is second_thread)
        _assert("exactly one thread object created", first_thread is not None)
    finally:
        asyncio.run(plugin.stop_services())
    _assert("stop_services clears the thread handle", plugin._deferred.thread is None)


def main() -> int:
    print("\ng_suite_plugin async dispatch/worker (D0.3) smoke tests")
    print("=" * 58)
    test_dispatch_returns_queued_without_calling_google()
    test_dispatch_requires_session_and_flow_id()
    test_dispatch_requires_connected_account()
    test_worker_completes_job_on_success()
    test_worker_completes_job_on_error()
    test_all_migrated_verbs_have_a_registered_producer()
    test_worker_unknown_verb_fails_loud()
    test_start_stop_services_idempotent()
    print()
    print(f"Results: {_passed} passed, {len(_failed)} failed")
    if _failed:
        print("FAILED:", _failed)
        return 1
    print("All async_jobs smoke tests passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
