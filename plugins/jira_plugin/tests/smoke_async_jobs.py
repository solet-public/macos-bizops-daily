#!/usr/bin/env python3
"""D0.3 deferred-completion smoke for jira_plugin's async_jobs.py.

Hermetic — a fake AsyncJobManager + a fake ``jira.JIRA`` client (via
``plugin._require_client`` monkeypatched directly, mirroring
external_postgres_plugin's ``connection.connect`` monkeypatch), no live
Jira site, no real background thread left running past a test (each test
uses its own plugin instance and never calls ``ensure_worker_started`` for
the thread-liveness check, which gets its own dedicated test).

Exercises:
  1. jql_search/get_issue/create_issue/update_issue/delete_issue dispatch
     all return {job_id, status: queued} in the SAME call, WITHOUT ever
     building a Jira client (plugin._require_client is never invoked on
     the dispatch path).
  2. Dispatch-time validation: missing session_id/flow_id in state fails
     loud with the plugin's typed error envelope — BEFORE any job is
     created (checked for jql_search and get_issue; create_issue/
     update_issue/delete_issue share the exact same _dispatch_async path).
  3. The worker's _process_job runs the real issue_actions function against
     a fake client and completes the job via update_job with the correct
     result shape on success, and the classified (topology-safe) error on
     a JIRAError fault — decoupled entirely from the dispatch call. Covers
     jql_search (paginated read) and create_issue (a write, proving the
     write path is wired through the same worker as reads).
  4. ACTION_HANDLERS carries exactly the 9 migrated verb names — a typo
     here would silently strand that verb's queued jobs forever.
  5. ensure_worker_started is idempotent: two calls produce the same thread
     object, not two threads.
  6. add_comment/list_comments/list_transitions/transition_issue dispatch
     all return {job_id, status: queued} without building a client; the
     worker completes transition_issue (a two-call verb — transition_issue
     then a re-fetch of the issue for its new status) with the real
     comment_actions function, proving both calls happen worker-side.
  7. test_connection dispatch returns queued without building a client;
     the worker completes it via async_jobs.py's own _test_connection
     (moved here from plugin.py, its only remaining caller, to avoid a
     plugin.py <-> async_jobs.py circular top-level import).

Run:
    SOLET_NAME=<name> .venv/bin/python3 \
        plugins/jira_plugin/tests/smoke_async_jobs.py

Exits 0 on success, 1 on first failure.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT / "plugins" / "jira_plugin" / "src"))

from jira import JIRAError  # noqa: E402
from jira_plugin import async_jobs  # noqa: E402
from jira_plugin.plugin import JiraPlugin  # noqa: E402

_SECRET_HOST = "SECRETHOST.atlassian.net"
_HOST_URL = f"https://{_SECRET_HOST}/rest/api/2/search/jql"

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


class _ClientCallRecorder:
    """Callable stand-in for plugin._require_client that counts invocations.

    A plain class instead of a per-iteration closure — a function defined
    inside a for loop that captures the loop's own locals trips B023
    (ruff/flake8-bugbear: "function definition does not bind loop
    variable"), since the closure's capture is late-binding and would read
    a stale value if it outlived the iteration it was built in, even though
    this particular usage never does.
    """

    def __init__(self) -> None:
        self.calls: list[None] = []

    def __call__(self) -> Any:
        self.calls.append(None)
        return MagicMock()


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


def _plugin_with_fake_manager() -> tuple[JiraPlugin, _FakeAsyncJobManager]:
    plugin = JiraPlugin()
    fake_manager = _FakeAsyncJobManager()
    plugin._async_job_manager = fake_manager
    return plugin, fake_manager


def test_jql_search_dispatch_returns_queued_without_building_client() -> None:
    plugin, fake_manager = _plugin_with_fake_manager()
    client_calls: list[None] = []

    def _record_require_client() -> Any:
        client_calls.append(None)
        return MagicMock()

    plugin._require_client = _record_require_client  # type: ignore[method-assign]
    result = plugin.jql_search({"jql": "project = X"}, {"session_id": "s1", "flow_id": "f1"})
    _assert("dispatch action_status completed", result["action_status"] == "completed")
    _assert("dispatch returns a job_id", isinstance(result["data"].get("job_id"), str))
    _assert("dispatch status is queued", result["data"].get("status") == "queued")
    _assert("_require_client never called on the dispatch path", client_calls == [])
    _assert("one job created in the fake ledger", len(fake_manager._jobs) == 1)


def test_jql_search_dispatch_requires_session_and_flow_id() -> None:
    plugin, _ = _plugin_with_fake_manager()
    result = plugin.jql_search({"jql": "project = X"}, {})
    _assert("missing state context -> error status", result["action_status"] == "error")
    _assert(
        "missing state context -> invalid_params code",
        result["error"]["code"] == "jira.invalid_params",
        str(result.get("error")),
    )


def test_worker_completes_jql_search_job_on_success() -> None:
    plugin, fake_manager = _plugin_with_fake_manager()
    dispatch = plugin.jql_search({"jql": "project = X"}, {"session_id": "s1", "flow_id": "f1"})
    job_id = dispatch["data"]["job_id"]

    fake_client = MagicMock()
    fake_client.enhanced_search_issues.return_value = {
        "issues": [
            {
                "key": "X-1",
                "fields": {
                    "summary": "Fix bug",
                    "status": {"name": "Done"},
                    "assignee": {"displayName": "Alice"},
                    "updated": "2026-08-10T00:00:00Z",
                },
            },
        ],
    }
    plugin._require_client = lambda: fake_client  # type: ignore[method-assign]
    async_jobs._process_job(plugin, fake_manager, job_id, "jql_search")

    statuses = [updates.get("status") for _, updates in fake_manager.update_calls]
    _assert("worker transitioned processing -> completed", statuses == ["processing", "completed"])
    completed_result = fake_manager.update_calls[-1][1]["result"]
    _assert("completed result carries the real issue row", completed_result["issues"][0]["key"] == "X-1", str(completed_result))
    _assert("completed result row_count is 1", completed_result["row_count"] == 1)


def test_worker_completes_jql_search_job_on_error_topology_safe() -> None:
    plugin, fake_manager = _plugin_with_fake_manager()
    dispatch = plugin.jql_search({"jql": "project = X"}, {"session_id": "s1", "flow_id": "f1"})
    job_id = dispatch["data"]["job_id"]

    fake_client = MagicMock()
    fake_client.enhanced_search_issues.side_effect = JIRAError(
        text="raw provider detail", status_code=401, url=_HOST_URL,
    )
    plugin._require_client = lambda: fake_client  # type: ignore[method-assign]
    async_jobs._process_job(plugin, fake_manager, job_id, "jql_search")

    statuses = [updates.get("status") for _, updates in fake_manager.update_calls]
    _assert("worker transitioned processing -> error", statuses == ["processing", "error"])
    error_update = fake_manager.update_calls[-1][1]
    _assert("error code is auth_failed", error_update["error"]["code"] == "jira.auth_failed", str(error_update))
    _assert(
        "error message hides the site host (topology-safe)",
        _SECRET_HOST not in error_update["error"]["message"],
        str(error_update),
    )


def test_get_issue_create_update_delete_dispatch_return_queued_without_building_client() -> None:
    for verb, params in (
        ("get_issue", {"key": "X-1"}),
        ("create_issue", {"project": "X", "issue_type": "Task", "summary": "s"}),
        ("update_issue", {"key": "X-1", "fields": {"summary": "new"}}),
        ("delete_issue", {"key": "X-1"}),
    ):
        plugin, fake_manager = _plugin_with_fake_manager()
        recorder = _ClientCallRecorder()
        plugin._require_client = recorder  # type: ignore[method-assign]
        dispatch = getattr(plugin, verb)
        result = dispatch(params, {"session_id": "s1", "flow_id": "f1"})
        _assert(f"{verb} dispatch action_status completed", result["action_status"] == "completed")
        _assert(f"{verb} dispatch returns a job_id", isinstance(result["data"].get("job_id"), str))
        _assert(f"{verb} dispatch status is queued", result["data"].get("status") == "queued")
        _assert(f"{verb} _require_client never called on the dispatch path", recorder.calls == [])
        _assert(f"{verb} one job created in the fake ledger", len(fake_manager._jobs) == 1)


def test_get_issue_dispatch_requires_session_and_flow_id() -> None:
    plugin, _ = _plugin_with_fake_manager()
    result = plugin.get_issue({"key": "X-1"}, {})
    _assert("get_issue missing state context -> error status", result["action_status"] == "error")
    _assert(
        "get_issue missing state context -> invalid_params code",
        result["error"]["code"] == "jira.invalid_params",
        str(result.get("error")),
    )


def test_worker_completes_create_issue_job_on_success() -> None:
    plugin, fake_manager = _plugin_with_fake_manager()
    dispatch = plugin.create_issue(
        {"project": "X", "issue_type": "Task", "summary": "New task"},
        {"session_id": "s1", "flow_id": "f1"},
    )
    job_id = dispatch["data"]["job_id"]

    created = MagicMock()
    created.key = "X-100"
    created.id = "10100"
    fake_client = MagicMock()
    fake_client.create_issue.return_value = created
    plugin._require_client = lambda: fake_client  # type: ignore[method-assign]
    async_jobs._process_job(plugin, fake_manager, job_id, "create_issue")

    statuses = [updates.get("status") for _, updates in fake_manager.update_calls]
    _assert("create_issue worker transitioned processing -> completed", statuses == ["processing", "completed"])
    completed_result = fake_manager.update_calls[-1][1]["result"]
    _assert("completed result carries the new issue key", completed_result["key"] == "X-100", str(completed_result))


def test_worker_completes_create_issue_job_on_error_topology_safe() -> None:
    plugin, fake_manager = _plugin_with_fake_manager()
    dispatch = plugin.create_issue(
        {"project": "X", "issue_type": "Task", "summary": "New task"},
        {"session_id": "s1", "flow_id": "f1"},
    )
    job_id = dispatch["data"]["job_id"]

    fake_client = MagicMock()
    fake_client.create_issue.side_effect = JIRAError(text="raw provider detail", status_code=403, url=_HOST_URL)
    plugin._require_client = lambda: fake_client  # type: ignore[method-assign]
    async_jobs._process_job(plugin, fake_manager, job_id, "create_issue")

    statuses = [updates.get("status") for _, updates in fake_manager.update_calls]
    _assert("create_issue worker transitioned processing -> error", statuses == ["processing", "error"])
    error_update = fake_manager.update_calls[-1][1]
    _assert("error code is permission_denied", error_update["error"]["code"] == "jira.permission_denied", str(error_update))
    _assert(
        "error message hides the site host (topology-safe)",
        _SECRET_HOST not in error_update["error"]["message"],
        str(error_update),
    )


def test_comment_transition_verbs_dispatch_return_queued_without_building_client() -> None:
    for verb, params in (
        ("add_comment", {"key": "X-1", "body": "a comment"}),
        ("list_comments", {"key": "X-1"}),
        ("list_transitions", {"key": "X-1"}),
        ("transition_issue", {"key": "X-1", "transition_id": "5"}),
    ):
        plugin, fake_manager = _plugin_with_fake_manager()
        recorder = _ClientCallRecorder()
        plugin._require_client = recorder  # type: ignore[method-assign]
        dispatch = getattr(plugin, verb)
        result = dispatch(params, {"session_id": "s1", "flow_id": "f1"})
        _assert(f"{verb} dispatch action_status completed", result["action_status"] == "completed")
        _assert(f"{verb} dispatch returns a job_id", isinstance(result["data"].get("job_id"), str))
        _assert(f"{verb} dispatch status is queued", result["data"].get("status") == "queued")
        _assert(f"{verb} _require_client never called on the dispatch path", recorder.calls == [])


def test_worker_completes_transition_issue_job_on_success() -> None:
    plugin, fake_manager = _plugin_with_fake_manager()
    dispatch = plugin.transition_issue(
        {"key": "X-1", "transition_id": "5"}, {"session_id": "s1", "flow_id": "f1"},
    )
    job_id = dispatch["data"]["job_id"]

    fake_client = MagicMock()
    fake_client.issue.return_value.raw = {"fields": {"status": {"name": "Done"}}}
    plugin._require_client = lambda: fake_client  # type: ignore[method-assign]
    async_jobs._process_job(plugin, fake_manager, job_id, "transition_issue")

    statuses = [updates.get("status") for _, updates in fake_manager.update_calls]
    _assert("transition_issue worker transitioned processing -> completed", statuses == ["processing", "completed"])
    completed_result = fake_manager.update_calls[-1][1]["result"]
    _assert("worker applied the transition", fake_client.transition_issue.called)
    _assert("completed result carries the new status", completed_result["new_status"] == "Done", str(completed_result))


def test_test_connection_dispatch_returns_queued_without_building_client() -> None:
    plugin, fake_manager = _plugin_with_fake_manager()
    client_calls: list[None] = []

    def _record_require_client() -> Any:
        client_calls.append(None)
        return MagicMock()

    plugin._require_client = _record_require_client  # type: ignore[method-assign]
    result = plugin.test_connection({}, {"session_id": "s1", "flow_id": "f1"})
    _assert("test_connection dispatch action_status completed", result["action_status"] == "completed")
    _assert("test_connection dispatch returns a job_id", isinstance(result["data"].get("job_id"), str))
    _assert("test_connection dispatch status is queued", result["data"].get("status") == "queued")
    _assert("test_connection _require_client never called on the dispatch path", client_calls == [])


def test_worker_completes_test_connection_job_on_success() -> None:
    plugin, fake_manager = _plugin_with_fake_manager()
    dispatch = plugin.test_connection({}, {"session_id": "s1", "flow_id": "f1"})
    job_id = dispatch["data"]["job_id"]

    fake_client = MagicMock()
    fake_client.myself.return_value = {"accountId": "acc-1", "displayName": "Service Account"}
    fake_client.server_url = "https://example.atlassian.net"
    plugin._require_client = lambda: fake_client  # type: ignore[method-assign]
    async_jobs._process_job(plugin, fake_manager, job_id, "test_connection")

    statuses = [updates.get("status") for _, updates in fake_manager.update_calls]
    _assert("test_connection worker transitioned processing -> completed", statuses == ["processing", "completed"])
    completed_result = fake_manager.update_calls[-1][1]["result"]
    _assert("completed result carries ok=True", completed_result["ok"] is True, str(completed_result))
    _assert("completed result carries account_id", completed_result["account_id"] == "acc-1", str(completed_result))


def test_action_handlers_carries_exactly_this_batchs_verbs() -> None:
    _assert(
        "ACTION_HANDLERS has exactly the 10 migrated verb names",
        set(async_jobs.ACTION_HANDLERS) == {
            "jql_search", "get_issue", "create_issue", "update_issue", "delete_issue",
            "add_comment", "list_comments", "list_transitions", "transition_issue", "test_connection",
        },
        str(set(async_jobs.ACTION_HANDLERS)),
    )


def test_ensure_worker_started_idempotent() -> None:
    plugin, _ = _plugin_with_fake_manager()
    async_jobs.ensure_worker_started(plugin)
    first_thread = plugin._worker_thread
    async_jobs.ensure_worker_started(plugin)
    second_thread = plugin._worker_thread
    _assert("ensure_worker_started reuses the same live thread", first_thread is second_thread)
    _assert("exactly one thread object created", first_thread is not None)


def main() -> int:
    print("\njira_plugin async_jobs (D0.3) smoke tests")
    print("=" * 42)
    test_jql_search_dispatch_returns_queued_without_building_client()
    test_jql_search_dispatch_requires_session_and_flow_id()
    test_worker_completes_jql_search_job_on_success()
    test_worker_completes_jql_search_job_on_error_topology_safe()
    test_get_issue_create_update_delete_dispatch_return_queued_without_building_client()
    test_get_issue_dispatch_requires_session_and_flow_id()
    test_worker_completes_create_issue_job_on_success()
    test_worker_completes_create_issue_job_on_error_topology_safe()
    test_comment_transition_verbs_dispatch_return_queued_without_building_client()
    test_worker_completes_transition_issue_job_on_success()
    test_test_connection_dispatch_returns_queued_without_building_client()
    test_worker_completes_test_connection_job_on_success()
    test_action_handlers_carries_exactly_this_batchs_verbs()
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
