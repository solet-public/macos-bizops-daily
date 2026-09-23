#!/usr/bin/env python3
"""D0.3 deferred-completion migration smoke tests for marketo_plugin.

Sync-verb Phase 1, Lane C2 (2026-08-09 —
the 2026-08-09 sync-verb Phase 1 wave-1 dispatch brief,
workbench/2026-08-09_sync_verb_d03_deferred_completion_doctrine_syncverb-doctrine.md).
Seven verbs now dispatch an AsyncJobManager job and return {job_id, status} in
milliseconds instead of running their vendor fetch inline on the dispatch
path: batch 1's get_leads/get_activities/list_campaigns/list_static_lists
(internally-paginating, multi-vendor-round-trip) and batch 2's
describe_lead_fields/get_api_usage/list_activity_types (single-vendor-call
reads). A background worker thread (MarketoPlugin._worker_loop) does the
real I/O for all seven.

Scope: this is a regression guard on THIS PLUGIN's contract with
AsyncJobManager — dispatch creates a job with the right shape, the worker
picks up queued marketo_plugin jobs and calls the right execute_<verb>
against them, success/failure route to the right terminal update_job call,
and check_marketo_job_status never omits a schema-declared key (the exact
defect class the coordinator seat flagged live during this dispatch — a schema property
absent from any reachable return shape bricks the verb on every
ExecutionContext.store_result call). It is NOT a regression guard on
AsyncJobManager's own internals (a separate, platform-level component) — a
lightweight fake stands in for it here, matching its documented method
signatures and return shapes (async_job_manager.py's own docstrings).

Hermetic — no live instance, no live state service.

Run:
    SOLET_NAME=<name> .venv/bin/python3 \
        plugins/marketo_plugin/tests/smoke_async_dispatch.py

Exits 0 on success, 1 on first failure.
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT / "plugins" / "marketo_plugin" / "src"))

from marketo_plugin.constants import CONFIG_KEY_EXPORT_ALLOWED_ROOTS, PLUGIN_NAME  # noqa: E402
from marketo_plugin.plugin import MarketoPlugin  # noqa: E402

_passed = 0
_failed: list[str] = []
_TMP_DIR = tempfile.mkdtemp(prefix="marketo_smoke_async_dispatch_")


def _assert(label: str, cond: bool, msg: str = "") -> None:
    global _passed
    if cond:
        _passed += 1
        print(f"  PASS  {label}")
    else:
        _failed.append(label)
        print(f"  FAIL  {label}: {msg or 'assertion failed'}")


def _tmp_tsv_path() -> str:
    return str(Path(_TMP_DIR) / f"out_{len(list(Path(_TMP_DIR).iterdir()))}.tsv")


class _FakeAsyncJobManager:
    """Matches AsyncJobManager's documented call shapes (async_job_manager.py
    docstrings), in-memory, no state service — exercises THIS PLUGIN's
    contract with the manager, not the manager's own internals."""

    def __init__(self) -> None:
        self._jobs: dict[str, dict[str, Any]] = {}
        self._payloads: dict[tuple[str, str], dict[str, Any]] = {}
        self._next_id = 0

    def create_job(
        self,
        plugin_name: str,
        action_name: str,
        request_data: dict[str, Any] | None = None,
        job_id: str | None = None,
        name: str | None = None,
        description: str | None = None,
        flow_id_trace: str | None = None,
        job_metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        notes = (request_data or {}).get("notes")
        if not isinstance(notes, str) or not notes.strip():
            return {"action_status": "error", "error": {"message": "notes required"}}
        self._next_id += 1
        generated_id = f"job-{self._next_id}"
        self._jobs[generated_id] = {
            "id": generated_id,
            "provider_name": f"{plugin_name}.{action_name}",
            "status": "queued",
            "metadata": job_metadata,
        }
        self._payloads[(generated_id, "request")] = dict(request_data or {})
        return {"action_status": "completed", "data": {"job_id": generated_id, "status": "queued"}}

    def list_jobs(
        self,
        status: str | None = None,
        provider_name: str | None = None,
        limit: int = 10,
        order_by: str = "created_at DESC",
    ) -> dict[str, Any]:
        jobs = list(self._jobs.values())
        if status is not None:
            jobs = [j for j in jobs if j["status"] == status]
        if provider_name is not None:
            jobs = [j for j in jobs if j["provider_name"] == provider_name]
        return {"action_status": "completed", "data": {"jobs": jobs[:limit]}}

    def get_job(self, job_id: str) -> dict[str, Any]:
        job = self._jobs.get(job_id)
        if job is None:
            return {"action_status": "error", "error": {"message": f"Job not found: {job_id}"}}
        return {"action_status": "completed", "data": {"job": dict(job)}}

    def get_job_payload(self, job_id: str, payload_type: str = "request") -> dict[str, Any]:
        payload = self._payloads.get((job_id, payload_type))
        if payload is None:
            return {"action_status": "error", "error": {"message": "not found"}}
        return {"action_status": "completed", "data": {"payload": payload}}

    def update_job(self, job_id: str, updates: dict[str, Any]) -> dict[str, Any]:
        job = self._jobs.get(job_id)
        if job is None:
            return {"action_status": "error", "error": {"message": f"Job not found: {job_id}"}}
        if "status" in updates:
            job["status"] = updates["status"]
        if "result" in updates:
            self._payloads[(job_id, "result")] = updates["result"]
        if "error" in updates:
            self._payloads[(job_id, "error")] = updates["error"]
        return {"action_status": "completed", "data": {"job_id": job_id, "updated": True}}


def _plugin_with_fake_manager() -> tuple[MarketoPlugin, _FakeAsyncJobManager]:
    plugin = MarketoPlugin()
    plugin.logger = MagicMock()
    plugin.config_provider = {CONFIG_KEY_EXPORT_ALLOWED_ROOTS: [_TMP_DIR]}
    manager = _FakeAsyncJobManager()
    orchestrator = MagicMock()
    orchestrator.async_job_manager = manager
    plugin.orchestrator_ref = orchestrator
    return plugin, manager


def _state() -> dict[str, Any]:
    return {"session_id": "sess-1", "flow_id": "flow-1"}


# ---------------------------------------------------------------------------
# Dispatch: required session_id/flow_id validation (D0.3 doctrine §1)
# ---------------------------------------------------------------------------


def test_dispatch_requires_session_and_flow_id() -> None:
    plugin, _manager = _plugin_with_fake_manager()
    result = plugin.get_leads(
        {"filter_type": "id", "filter_values": ["1"], "output_tsv_path": _tmp_tsv_path()},
        {},
    )
    _assert("missing session_id/flow_id refused, not job-created", result["error"] is not None, str(result))
    _assert(
        "missing session_id/flow_id classified as invalid_params",
        result["error"]["code"] == "marketo.invalid_params",
        str(result),
    )


def test_dispatch_invalid_params_never_reaches_create_job() -> None:
    plugin, manager = _plugin_with_fake_manager()
    result = plugin.get_leads({"output_tsv_path": _tmp_tsv_path()}, _state())
    _assert("missing filter_type refused before job creation", result["error"] is not None, str(result))
    _assert("no job was created for the invalid dispatch", len(manager._jobs) == 0)


# ---------------------------------------------------------------------------
# Dispatch: ms-scale, correct job shape, no vendor I/O
# ---------------------------------------------------------------------------


def test_dispatch_creates_job_and_returns_handle_immediately() -> None:
    plugin, manager = _plugin_with_fake_manager()
    result = plugin.get_leads(
        {"filter_type": "id", "filter_values": ["1"], "output_tsv_path": _tmp_tsv_path()},
        _state(),
    )
    _assert("dispatch succeeded", result["error"] is None, str(result))
    _assert("dispatch returns job_id", isinstance(result["data"]["job_id"], str) and result["data"]["job_id"])
    _assert("dispatch returns status=queued", result["data"]["status"] == "queued")
    job_id = result["data"]["job_id"]
    _assert("exactly one job created", len(manager._jobs) == 1)
    _assert("job registered under marketo_plugin.get_leads", manager._jobs[job_id]["provider_name"] == f"{PLUGIN_NAME}.get_leads")
    _assert("job starts queued", manager._jobs[job_id]["status"] == "queued")
    metadata = manager._jobs[job_id]["metadata"]
    _assert("job_metadata carries completion_handlers", "completion_handlers" in metadata, str(metadata))
    _assert("job_metadata carries session_id", metadata["session_id"] == "sess-1")
    _assert("job_metadata carries flow_id", metadata["flow_id"] == "flow-1")


def test_dispatch_covers_all_seven_migrated_verbs() -> None:
    plugin, manager = _plugin_with_fake_manager()
    cases: list[tuple[str, dict[str, Any]]] = [
        ("get_leads", {"filter_type": "id", "filter_values": ["1"], "output_tsv_path": _tmp_tsv_path()}),
        ("get_activities", {"since_datetime": "2026-08-09T00:00:00Z", "activity_type_ids": [1], "output_tsv_path": _tmp_tsv_path()}),
        ("list_campaigns", {"output_tsv_path": _tmp_tsv_path()}),
        ("list_static_lists", {"output_tsv_path": _tmp_tsv_path()}),
        ("describe_lead_fields", {"output_tsv_path": _tmp_tsv_path()}),
        ("list_activity_types", {"output_tsv_path": _tmp_tsv_path()}),
        ("get_api_usage", {}),
    ]
    for verb, params in cases:
        result = getattr(plugin, verb)(params, _state())
        _assert(f"{verb}: dispatch succeeded", result["error"] is None, str(result))
        _assert(f"{verb}: returns status=queued", result["data"]["status"] == "queued")
    provider_names = {job["provider_name"] for job in manager._jobs.values()}
    for verb, _params in cases:
        _assert(f"{verb}: job registered under marketo_plugin.{verb}", f"{PLUGIN_NAME}.{verb}" in provider_names)


# ---------------------------------------------------------------------------
# Worker: picks up queued jobs, runs the real execute_<verb>, completes them
# ---------------------------------------------------------------------------


def test_worker_processes_queued_job_to_completion() -> None:
    plugin, manager = _plugin_with_fake_manager()
    plugin._app_config_loader = MagicMock()
    plugin._client = MagicMock()
    plugin._client.get_json.side_effect = [
        {"success": True, "result": [{"id": 1}, {"id": 2}], "nextPageToken": None, "moreResult": False},
    ]
    dispatch = plugin.get_leads(
        {"filter_type": "id", "filter_values": ["1"], "output_tsv_path": _tmp_tsv_path()},
        _state(),
    )
    job_id = dispatch["data"]["job_id"]

    plugin._process_pending_jobs()

    job = manager._jobs[job_id]
    _assert("worker completed the job", job["status"] == "completed", str(job))
    result_payload = manager._payloads[(job_id, "result")]
    _assert("completed job result carries row_count", result_payload["row_count"] == 2, str(result_payload))
    _assert("completed job result carries a path", isinstance(result_payload["path"], str) and result_payload["path"])


def test_worker_processes_single_call_verb_to_completion() -> None:
    """Batch 2's single-vendor-call verbs (no pagination) reach completion too."""
    plugin, manager = _plugin_with_fake_manager()
    plugin._app_config_loader = MagicMock()
    plugin._client = MagicMock()
    plugin._client.get_json.return_value = {
        "success": True,
        "result": [{"date": "2026-08-09", "total": 42, "users": [{"userId": "1", "total": 42}]}],
    }
    dispatch = plugin.get_api_usage({}, _state())
    job_id = dispatch["data"]["job_id"]

    plugin._process_pending_jobs()

    job = manager._jobs[job_id]
    _assert("worker completed a single-call verb's job", job["status"] == "completed", str(job))
    result_payload = manager._payloads[(job_id, "result")]
    _assert("completed job result carries calls_today", result_payload["calls_today"] == 42, str(result_payload))


def test_worker_routes_vendor_failure_to_error_status() -> None:
    plugin, manager = _plugin_with_fake_manager()
    plugin._app_config_loader = MagicMock()
    plugin._client = MagicMock()
    plugin._client.get_json.side_effect = RuntimeError("boom")
    dispatch = plugin.list_campaigns({"output_tsv_path": _tmp_tsv_path()}, _state())
    job_id = dispatch["data"]["job_id"]

    plugin._process_pending_jobs()

    job = manager._jobs[job_id]
    _assert("worker marked the job error, not completed", job["status"] == "error", str(job))
    error_payload = manager._payloads[(job_id, "error")]
    _assert("error payload carries a code", "code" in error_payload, str(error_payload))
    _assert("error payload carries a message", "message" in error_payload, str(error_payload))


def test_worker_ignores_jobs_from_other_plugins() -> None:
    plugin, manager = _plugin_with_fake_manager()
    manager._jobs["other-1"] = {"id": "other-1", "provider_name": "zuora_plugin.get_invoices", "status": "queued", "metadata": {}}
    manager._payloads[("other-1", "request")] = {"notes": "unrelated"}

    plugin._process_pending_jobs()

    _assert("a different plugin's queued job is left untouched", manager._jobs["other-1"]["status"] == "queued")


# ---------------------------------------------------------------------------
# Boot-ordering race (CON-07 / public issue #23, §47.5): orchestrator_ref is
# set before EventOrchestrator._delegate_service_attributes() has run, so
# `async_job_manager` does not exist on it yet — not None, ABSENT. A direct
# `self.orchestrator_ref.async_job_manager` read raises AttributeError in
# that window; every sibling connector (external_postgres_plugin,
# g_suite_plugin) reads it with getattr(..., None) instead. `_BareOrchestrator`
# below has no such attribute at all, reproducing that exact window without a
# real EventOrchestrator.
# ---------------------------------------------------------------------------


class _BareOrchestrator:
    """Stand-in for an EventOrchestrator before service-attribute delegation
    has run: genuinely has no `async_job_manager` attribute (not None)."""


def test_worker_survives_orchestrator_before_service_delegation() -> None:
    """The mutation this catches: reverting either read below back to a bare
    `self.orchestrator_ref.async_job_manager` reintroduces an uncaught
    AttributeError here instead of a clean early return."""
    plugin = MarketoPlugin()
    plugin.logger = MagicMock()
    plugin.orchestrator_ref = _BareOrchestrator()
    plugin._process_pending_jobs()  # must not raise AttributeError
    _assert("worker tolerates orchestrator_ref before service delegation (no crash)", True)


def test_require_async_job_manager_raises_runtime_error_not_attribute_error() -> None:
    """Same boot-race window, through the request-path wrapper: it must raise
    its own documented RuntimeError, not leak the platform's AttributeError."""
    plugin = MarketoPlugin()
    plugin.orchestrator_ref = _BareOrchestrator()
    try:
        plugin._require_async_job_manager()
        _assert("_require_async_job_manager raises when manager absent", False, "no exception raised")
    except AttributeError as exc:
        _assert("_require_async_job_manager raises RuntimeError, not AttributeError", False, str(exc))
    except RuntimeError:
        _assert("_require_async_job_manager raises RuntimeError, not AttributeError", True)


# ---------------------------------------------------------------------------
# check_marketo_job_status: every schema key present in every reachable state
# (coordinator-seat advisory, 2026-08-09: an absent schema-declared key bricks the
# verb on every ExecutionContext.store_result call, not just some.)
# ---------------------------------------------------------------------------

_STATUS_SCHEMA_KEYS = {"job_id", "status", "result", "error"}


def test_check_job_status_schema_keys_present_when_queued() -> None:
    plugin, manager = _plugin_with_fake_manager()
    manager._jobs["q1"] = {"id": "q1", "provider_name": f"{PLUGIN_NAME}.get_leads", "status": "queued", "metadata": {}}
    response = plugin.check_marketo_job_status({"job_id": "q1"}, _state())
    _assert("queued: call succeeded", response["error"] is None, str(response))
    _assert("queued: every schema key present", _STATUS_SCHEMA_KEYS <= set(response["data"].keys()), str(response["data"]))
    _assert("queued: result is null, not absent", response["data"]["result"] is None)
    _assert("queued: error is null, not absent", response["data"]["error"] is None)


def test_check_job_status_schema_keys_present_when_processing() -> None:
    plugin, manager = _plugin_with_fake_manager()
    manager._jobs["p1"] = {"id": "p1", "provider_name": f"{PLUGIN_NAME}.get_leads", "status": "processing", "metadata": {}}
    response = plugin.check_marketo_job_status({"job_id": "p1"}, _state())
    _assert("processing: every schema key present", _STATUS_SCHEMA_KEYS <= set(response["data"].keys()), str(response["data"]))
    _assert("processing: result is null, not absent", response["data"]["result"] is None)
    _assert("processing: error is null, not absent", response["data"]["error"] is None)


def test_check_job_status_schema_keys_present_when_completed() -> None:
    plugin, manager = _plugin_with_fake_manager()
    manager._jobs["c1"] = {"id": "c1", "provider_name": f"{PLUGIN_NAME}.get_leads", "status": "completed", "metadata": {}}
    manager._payloads[("c1", "result")] = {"path": "/tmp/x.tsv", "row_count": 3, "columns": ["id"], "truncated": False}
    response = plugin.check_marketo_job_status({"job_id": "c1"}, _state())
    _assert("completed: every schema key present", _STATUS_SCHEMA_KEYS <= set(response["data"].keys()), str(response["data"]))
    _assert("completed: result is populated", response["data"]["result"]["row_count"] == 3)
    _assert("completed: error is null, not absent", response["data"]["error"] is None)


def test_check_job_status_schema_keys_present_when_error() -> None:
    plugin, manager = _plugin_with_fake_manager()
    manager._jobs["e1"] = {"id": "e1", "provider_name": f"{PLUGIN_NAME}.get_leads", "status": "error", "metadata": {}}
    manager._payloads[("e1", "error")] = {"code": "marketo.rate_limited", "message": "rate limited"}
    response = plugin.check_marketo_job_status({"job_id": "e1"}, _state())
    _assert("error: every schema key present", _STATUS_SCHEMA_KEYS <= set(response["data"].keys()), str(response["data"]))
    _assert("error: result is null, not absent", response["data"]["result"] is None)
    _assert("error: error is populated", response["data"]["error"]["code"] == "marketo.rate_limited")


def test_check_job_status_unknown_job_id_refused() -> None:
    plugin, _manager = _plugin_with_fake_manager()
    response = plugin.check_marketo_job_status({"job_id": "nonexistent"}, _state())
    _assert("unknown job_id refused", response["error"] is not None, str(response))
    _assert("unknown job_id classified job_not_found", response["error"]["code"] == "marketo.job_not_found", str(response))


def main() -> int:
    print("\nmarketo_plugin D0.3 async-dispatch + worker smoke tests")
    print("=" * 58)
    test_dispatch_requires_session_and_flow_id()
    test_dispatch_invalid_params_never_reaches_create_job()
    test_dispatch_creates_job_and_returns_handle_immediately()
    test_dispatch_covers_all_seven_migrated_verbs()
    test_worker_processes_queued_job_to_completion()
    test_worker_processes_single_call_verb_to_completion()
    test_worker_routes_vendor_failure_to_error_status()
    test_worker_ignores_jobs_from_other_plugins()
    test_worker_survives_orchestrator_before_service_delegation()
    test_require_async_job_manager_raises_runtime_error_not_attribute_error()
    test_check_job_status_schema_keys_present_when_queued()
    test_check_job_status_schema_keys_present_when_processing()
    test_check_job_status_schema_keys_present_when_completed()
    test_check_job_status_schema_keys_present_when_error()
    test_check_job_status_unknown_job_id_refused()
    print()
    print(f"Results: {_passed} passed, {len(_failed)} failed")
    if _failed:
        print("FAILED:", _failed)
        return 1
    print("All D0.3 async-dispatch smoke tests passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
