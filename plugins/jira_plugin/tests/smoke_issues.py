#!/usr/bin/env python3
"""JIR-B issue-verb + error-classification smoke tests (no pytest, no live Jira).

Hermetic — a faked JIRA client, synthetic JIRAError objects. Red-first: each
check asserts real parsing / internal-pagination / override / classification /
topology-hygiene behavior against the 2026-08-03 reopened design (jira EXITED
the data-export requirement entirely, operator veto "no PII in Jira"; paging is hidden
inside the effective row limit, operator ruling "the paging is an
implementation detail that should be hidden" — design doc §0.1/§5.4). There
is no output_tsv_path, no containment gate, and no caller-visible pagination
parameter left on this verb; a test asserting any of those would itself be
stale.

Exercises:
  1.  jql_search — trimmed row shape (key/summary/status/assignee/updated)
      returned INLINE; rides /search/jql (enhanced_search_issues) — the
      legacy /search client method is NEVER called (Atlassian removed it,
      HTTP 410, 2026)
  2.  jql_search — assignee=None (unassigned) does NOT crash; row assignee is
      None (not the empty-string TSV artifact of the retired shape)
  3.  jql_search — a short final page (< requested page size) ends pagination
      truthfully: truncated=False, total falls back to the exact row count,
      approximate_issue_count is NOT called
  4.  jql_search — the 500-row default is reached via internal pagination
      across Atlassian's 100/call ceiling (5 internal HTTP calls), each
      forwarding the PRIOR call's nextPageToken verbatim — no caller-visible
      token anywhere in params or the result
  5.  jql_search — reaching the target while the vendor still has more
      (nextPageToken still present) reports truncated=True and total comes
      from a fresh approximate_issue_count(jql) call — this is the exact bug
      class fixed this session: a "full pages, target reached" exit must NOT
      be reported as complete
  6.  jql_search — max_results narrows the effective limit for THIS call only
      (never widens past the ceiling — see _clamp_within_ceiling unit checks)
  7.  jql_search — the acknowledge_default_limit_override/row_limit pair:
      both-required-together, row_limit above the 5,000 cap refused, a
      non-positive/non-int row_limit refused — see _resolve_effective_limit
      unit checks
  8.  jql_search — fields union: a caller `fields` list ADDS to the render
      set, never replaces it (rows are never hollow)
  9.  get_issue — .raw parse: description/status/people/labels/attachment
      metadata (UNCHANGED — single-record reads stay inline-capable)
  10. create_issue / update_issue / delete_issue — unchanged CRUD behavior
  11. classify — 401/403/429 -> generic codes with NO exc detail; 404/400 ->
      detail from exc.text
  12. TOPOLOGY-LEAK (red-first) — site host in JIRAError.url is ABSENT from
      the classified message on BOTH the generic (401) AND detail (404/400)
      paths
  13. _run — ValueError/AppConfigError/JIRAError/JiraServiceError/broad
      routing (ExportPathRefusedError no longer exists as a class — its
      import alone would fail red on the retired module)
  14. blob export service resolution (add_attachment's upload path — the only
      surviving blob-write path on this connector)

Run:
    SOLET_NAME=<name> .venv/bin/python3 plugins/jira_plugin/tests/smoke_issues.py

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
from jira_plugin import issue_actions  # noqa: E402
from jira_plugin.app_config import AppConfigError  # noqa: E402
from jira_plugin.constants import (  # noqa: E402
    DEFAULT_ROW_LIMIT,
    ERROR_API_ERROR,
    ERROR_AUTH_FAILED,
    ERROR_BAD_REQUEST,
    ERROR_INVALID_PARAMS,
    ERROR_NOT_CONFIGURED,
    ERROR_NOT_FOUND,
    ERROR_PERMISSION_DENIED,
    ERROR_RATE_LIMITED,
    JQL_PAGE_SIZE,
    ROW_LIMIT_CAP,
)
from jira_plugin.errors import JiraServiceError, classify_jira_error  # noqa: E402
from jira_plugin.plugin import JiraPlugin  # noqa: E402

_SECRET_HOST = "SECRETHOST.atlassian.net"
_HOST_URL = f"https://{_SECRET_HOST}/rest/api/2/issue/EXAMPLE-1"

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


def _issue(key: str, summary: str, status: str, assignee: str | None, updated: str) -> dict[str, Any]:
    fields: dict[str, Any] = {
        "summary": summary,
        "status": {"name": status},
        "assignee": {"displayName": assignee} if assignee is not None else None,
        "updated": updated,
    }
    return {"key": key, "fields": fields}


def _paged_jql_client(all_issues: list[dict[str, Any]]) -> tuple[MagicMock, list[dict[str, Any]]]:
    """A fake client whose enhanced_search_issues honors maxResults/nextPageToken
    like the real /search/jql endpoint — pages the given issue list, emitting a
    nextPageToken only while more remain. Returns (client, recorded_responses)
    so tests can cross-check forwarded tokens against what was actually sent."""
    client = MagicMock()
    responses: list[dict[str, Any]] = []
    served = {"i": 0}

    def _search(
        jql: str, maxResults: int, fields: list[str], json_result: bool, nextPageToken: str | None = None
    ) -> dict[str, Any]:
        start = served["i"]
        page = all_issues[start : start + maxResults]
        served["i"] += len(page)
        resp: dict[str, Any] = {"issues": page}
        if served["i"] < len(all_issues):
            resp["nextPageToken"] = f"tok-{served['i']}"
        responses.append(resp)
        return resp

    client.enhanced_search_issues.side_effect = _search
    return client, responses


# ---------------------------------------------------------------------------
# jql_search
# ---------------------------------------------------------------------------


def test_jql_row_shape() -> None:
    client = MagicMock()
    client.enhanced_search_issues.return_value = {
        "issues": [
            _issue("EXAMPLE-1", "Fix bug", "In Progress", "Alice", "2026-07-01T00:00:00Z"),
            _issue("EXAMPLE-2", "Add docs", "To Do", None, "2026-07-02T00:00:00Z"),
        ],
    }
    result = issue_actions.jql_search(client, {"jql": "project = EXAMPLE"})
    _assert("legacy /search never called (410-removed)", client.search_issues.call_count == 0)
    _assert("single page skips approximate count", client.approximate_issue_count.call_count == 0)
    _assert("issues returned inline", "issues" in result)
    _assert("row_count is 2", result["row_count"] == 2)
    _assert("total falls back to page length", result["total"] == 2)
    _assert("not truncated", result["truncated"] is False)
    rows = result["issues"]
    _assert(
        "row shape is the trimmed set",
        set(rows[0]) == {"key", "summary", "status", "assignee", "updated"},
    )
    _assert("status flattened to name", rows[0]["status"] == "In Progress")
    _assert("assignee flattened to displayName", rows[0]["assignee"] == "Alice")
    _assert("unassigned assignee is None (inline, not a TSV empty-string artifact)", rows[1]["assignee"] is None)


def test_jql_short_page_ends_pagination_truthfully() -> None:
    client, _ = _paged_jql_client(
        [_issue(f"EXAMPLE-{i}", "x", "Done", "Bob", "2026-07-01T00:00:00Z") for i in range(250)]
    )
    result = issue_actions.jql_search(client, {"jql": "project = EXAMPLE"})
    _assert("all 250 issues collected across pages", result["row_count"] == 250)
    _assert("short final page -> not truncated", result["truncated"] is False)
    _assert("total is the exact count, no approximate call", result["total"] == 250)
    _assert(
        "approximate_issue_count NOT called when not truncated",
        client.approximate_issue_count.call_count == 0,
    )
    _assert("exactly 3 internal calls (100+100+50)", client.enhanced_search_issues.call_count == 3)


def test_jql_default_pages_internally_and_forwards_tokens() -> None:
    # RED MUTATION: hardcode nextPageToken to a stale/empty value on internal
    # calls 2+ and this drops to fewer than 5 calls / wrong row_count.
    client, responses = _paged_jql_client(
        [_issue(f"EXAMPLE-{i}", "x", "Done", "Bob", "2026-07-01T00:00:00Z") for i in range(550)]
    )
    result = issue_actions.jql_search(client, {"jql": "project = EXAMPLE"})
    calls = client.enhanced_search_issues.call_args_list
    _assert(f"5 internal calls to reach the {DEFAULT_ROW_LIMIT}-row default", len(calls) == 5)
    _assert(f"row_count is exactly {DEFAULT_ROW_LIMIT}", result["row_count"] == DEFAULT_ROW_LIMIT)
    _assert("first call carries no nextPageToken", "nextPageToken" not in calls[0].kwargs)
    for i in range(1, len(calls)):
        expected = responses[i - 1].get("nextPageToken")
        _assert(
            f"call {i + 1} forwards call {i}'s nextPageToken verbatim",
            calls[i].kwargs.get("nextPageToken") == expected,
        )
    _assert(f"each call requests at most {JQL_PAGE_SIZE} (Atlassian's ceiling)", all(c.kwargs["maxResults"] <= JQL_PAGE_SIZE for c in calls))
    _assert("caller never sees a token anywhere in the result", "next_page_token" not in result and "nextPageToken" not in result)


def test_jql_target_reached_with_more_available_is_truncated() -> None:
    # This is the exact bug class fixed this session: stopping at the target
    # while the vendor still has more must report truncated=True, not False.
    client, _ = _paged_jql_client(
        [_issue(f"EXAMPLE-{i}", "x", "Done", "Bob", "2026-07-01T00:00:00Z") for i in range(600)]
    )
    client.approximate_issue_count.return_value = 600
    result = issue_actions.jql_search(client, {"jql": "project = EXAMPLE"})
    _assert("truncated=True when target reached but vendor has more", result["truncated"] is True)
    _assert("total comes from approximate_issue_count when truncated", result["total"] == 600)
    _assert(
        "approximate count queried with the same JQL",
        client.approximate_issue_count.call_args.args[0] == "project = EXAMPLE",
    )


def test_jql_max_results_narrows_this_call() -> None:
    client, _ = _paged_jql_client(
        [_issue(f"EXAMPLE-{i}", "x", "Done", "Bob", "2026-07-01T00:00:00Z") for i in range(200)]
    )
    result = issue_actions.jql_search(client, {"jql": "project = EXAMPLE", "max_results": 50})
    _assert("max_results narrows row_count", result["row_count"] == 50)
    _assert("single internal call suffices", client.enhanced_search_issues.call_count == 1)
    _assert("maxResults sent as 50, not the page ceiling", client.enhanced_search_issues.call_args.kwargs["maxResults"] == 50)


def test_jql_fields_union_no_hollow_rows() -> None:
    client = MagicMock()
    client.enhanced_search_issues.return_value = {
        "issues": [_issue("EXAMPLE-1", "Fix bug", "In Progress", "Alice", "2026-07-01T00:00:00Z")],
    }
    result = issue_actions.jql_search(client, {"jql": "project = EXAMPLE", "fields": ["priority"]})
    fetched = client.enhanced_search_issues.call_args.kwargs.get("fields")
    for render_field in ("summary", "status", "assignee", "updated"):
        _assert(f"render field '{render_field}' still fetched", render_field in fetched)
    _assert("caller extra field appended", "priority" in fetched)
    _assert("no duplicate render fields", len(fetched) == len(set(fetched)))
    _assert("row not hollow — summary populated", result["issues"][0]["summary"] == "Fix bug")


def test_jql_requires_jql() -> None:
    raised = False
    try:
        issue_actions.jql_search(MagicMock(), {})
    except ValueError:
        raised = True
    _assert("missing jql raises ValueError", raised)


# ---------------------------------------------------------------------------
# override pair + clamp — unit checks on the pure helpers
# ---------------------------------------------------------------------------


def test_resolve_effective_limit_default() -> None:
    _assert(
        "no override -> DEFAULT_ROW_LIMIT",
        issue_actions._resolve_effective_limit({}, verb="jql_search") == DEFAULT_ROW_LIMIT,
    )


def test_resolve_effective_limit_pair_required_together() -> None:
    raised_override_only = False
    try:
        issue_actions._resolve_effective_limit({"acknowledge_default_limit_override": True}, verb="jql_search")
    except ValueError:
        raised_override_only = True
    _assert("override alone (no row_limit) raises ValueError", raised_override_only)

    raised_row_limit_only = False
    try:
        issue_actions._resolve_effective_limit({"row_limit": 1000}, verb="jql_search")
    except ValueError:
        raised_row_limit_only = True
    _assert("row_limit alone (no override) raises ValueError", raised_row_limit_only)


def test_resolve_effective_limit_raises_ceiling() -> None:
    limit = issue_actions._resolve_effective_limit(
        {"acknowledge_default_limit_override": True, "row_limit": 2000}, verb="jql_search"
    )
    _assert("an acknowledged override raises the ceiling", limit == 2000)


def test_resolve_effective_limit_cap_refused_not_clamped() -> None:
    raised = False
    try:
        issue_actions._resolve_effective_limit(
            {"acknowledge_default_limit_override": True, "row_limit": ROW_LIMIT_CAP + 1}, verb="jql_search"
        )
    except ValueError:
        raised = True
    _assert(f"row_limit above the {ROW_LIMIT_CAP} cap is refused, not silently clamped", raised)


def test_resolve_effective_limit_invalid_row_limit() -> None:
    for bad in (0, -5, "abc", True):
        raised = False
        try:
            issue_actions._resolve_effective_limit(
                {"acknowledge_default_limit_override": True, "row_limit": bad}, verb="jql_search"
            )
        except ValueError:
            raised = True
        _assert(f"row_limit={bad!r} raises ValueError", raised)


def test_clamp_within_ceiling_never_widens() -> None:
    _assert("a value above the ceiling is clamped down", issue_actions._clamp_within_ceiling(99999, 500) == 500)
    _assert("a value below the ceiling narrows", issue_actions._clamp_within_ceiling(10, 500) == 10)
    _assert("an absent value falls back to the ceiling", issue_actions._clamp_within_ceiling(None, 500) == 500)
    _assert("an invalid value falls back to the ceiling", issue_actions._clamp_within_ceiling("nope", 500) == 500)


# ---------------------------------------------------------------------------
# get_issue / create / update / delete
# ---------------------------------------------------------------------------


def test_get_issue_parses_raw() -> None:
    client = MagicMock()
    client.issue.return_value.raw = {
        "key": "EXAMPLE-9",
        "fields": {
            "summary": "Ship it",
            "description": "the full body",
            "status": {"name": "Done"},
            "assignee": None,
            "reporter": {"displayName": "Carol"},
            "labels": ["backend", "urgent"],
            "attachment": [
                {"id": "att-1", "filename": "log.txt", "mimeType": "text/plain", "size": "42"}
            ],
        },
    }
    result = issue_actions.get_issue(client, {"key": "EXAMPLE-9"})
    _assert("summary parsed", result["summary"] == "Ship it")
    _assert("description parsed", result["description"] == "the full body")
    _assert("status flattened", result["status"] == "Done")
    _assert("unassigned -> None", result["assignee"] is None)
    _assert("reporter flattened", result["reporter"] == "Carol")
    _assert("labels carried", result["labels"] == ["backend", "urgent"])
    att = result["attachments"][0]
    _assert("attachment id parsed", att["attachment_id"] == "att-1")
    _assert("attachment size coerced to int", att["size"] == 42)


def test_create_issue_merges_fields() -> None:
    client = MagicMock()
    created = MagicMock()
    created.key = "EXAMPLE-100"
    created.id = "10100"
    client.create_issue.return_value = created
    result = issue_actions.create_issue(
        client,
        {
            "project": "EXAMPLE",
            "issue_type": "Task",
            "summary": "New task",
            "description": "desc",
            "fields": {"priority": {"name": "High"}},
        },
    )
    _assert("returns key", result["key"] == "EXAMPLE-100")
    _assert("returns id", result["id"] == "10100")
    sent = client.create_issue.call_args.kwargs["fields"]
    _assert("project key set", sent["project"] == {"key": "EXAMPLE"})
    _assert("issuetype name set", sent["issuetype"] == {"name": "Task"})
    _assert("extra field merged", sent["priority"] == {"name": "High"})


def test_update_issue_requires_fields() -> None:
    raised = False
    try:
        issue_actions.update_issue(MagicMock(), {"key": "EXAMPLE-1", "fields": {}})
    except ValueError:
        raised = True
    _assert("empty fields raises ValueError", raised)


def test_update_issue_applies() -> None:
    client = MagicMock()
    result = issue_actions.update_issue(client, {"key": "EXAMPLE-1", "fields": {"summary": "new"}})
    _assert("update returns ok", result["ok"] is True)
    client.issue.return_value.update.assert_called_once_with(fields={"summary": "new"})


def test_delete_issue() -> None:
    client = MagicMock()
    result = issue_actions.delete_issue(client, {"key": "EXAMPLE-7"})
    _assert("delete returns ok", result["ok"] is True)
    _assert("issue(key).delete() invoked", client.issue.return_value.delete.called)


def test_delete_requires_key() -> None:
    raised = False
    try:
        issue_actions.delete_issue(MagicMock(), {})
    except ValueError:
        raised = True
    _assert("missing key raises ValueError", raised)


# ---------------------------------------------------------------------------
# classification + topology leak
# ---------------------------------------------------------------------------


def test_classify_generic_codes() -> None:
    for status, expected in (
        (401, ERROR_AUTH_FAILED),
        (403, ERROR_PERMISSION_DENIED),
        (429, ERROR_RATE_LIMITED),
    ):
        exc = JIRAError(text="raw provider detail", status_code=status, url=_HOST_URL)
        code, message = classify_jira_error(exc)
        _assert(f"{status} -> {expected}", code == expected)
        _assert(f"{status} message omits exc detail (generic)", "raw provider detail" not in message)


def test_classify_detail_codes() -> None:
    exc404 = JIRAError(text="Issue does not exist", status_code=404, url=_HOST_URL)
    code, message = classify_jira_error(exc404)
    _assert("404 -> not_found", code == ERROR_NOT_FOUND)
    _assert("404 includes body detail", "Issue does not exist" in message)

    exc400 = JIRAError(
        text='{"errorMessages":["Field foo is required"],"errors":{}}',
        status_code=400,
        url=_HOST_URL,
    )
    code, message = classify_jira_error(exc400)
    _assert("400 -> bad_request", code == ERROR_BAD_REQUEST)
    _assert("400 parses errorMessages from JSON body", "Field foo is required" in message)


def test_topology_leak_absent_both_paths() -> None:
    # RED-FIRST: str(JIRAError) embeds exc.url (= the site host). The classifier
    # builds detail from exc.text ONLY, so the host must be absent on BOTH the
    # generic (401) and the detail (404/400) paths. To see red, change errors.py
    # to use str(exc)/f"{exc}" and the host reappears here.
    for status in (401, 403, 429, 404, 400):
        exc = JIRAError(text="body detail", status_code=status, url=_HOST_URL)
        _, message = classify_jira_error(exc)
        _assert(f"host absent from {status} message", _SECRET_HOST not in message, message)


# ---------------------------------------------------------------------------
# _run routing (via a real plugin instance, no readiness needed)
# ---------------------------------------------------------------------------


def test_run_routes_errors() -> None:
    plugin = JiraPlugin()

    def raise_value() -> dict[str, Any]:
        raise ValueError("bad param")

    def raise_config() -> dict[str, Any]:
        raise AppConfigError("address_book_entry_missing", "register the jira_site entry")

    def raise_jira() -> dict[str, Any]:
        raise JIRAError(text="nope", status_code=403, url=_HOST_URL)

    def raise_service() -> dict[str, Any]:
        raise JiraServiceError("blob_storage_service_not_available", "no blob service")

    def raise_broad() -> dict[str, Any]:
        raise RuntimeError(f"could not connect to {_SECRET_HOST}:443")

    _assert("ValueError -> invalid_params", plugin._run(raise_value, "v")["error"]["code"] == ERROR_INVALID_PARAMS)
    _assert("AppConfigError -> not_configured", plugin._run(raise_config, "v")["error"]["code"] == ERROR_NOT_CONFIGURED)
    _assert("JIRAError -> classified", plugin._run(raise_jira, "v")["error"]["code"] == ERROR_PERMISSION_DENIED)
    _assert(
        "JiraServiceError -> its code",
        plugin._run(raise_service, "v")["error"]["code"] == "blob_storage_service_not_available",
    )
    broad = plugin._run(raise_broad, "v")
    _assert("broad -> api_error", broad["error"]["code"] == ERROR_API_ERROR)
    _assert("broad message omits the host (generic)", _SECRET_HOST not in broad["error"]["message"])


def test_export_containment_module_is_gone() -> None:
    # RED-FIRST: this import must fail — the module is deleted, not orphaned.
    raised = False
    try:
        import jira_plugin.export_containment  # noqa: F401
    except ModuleNotFoundError:
        raised = True
    _assert("export_containment module no longer importable", raised)


def test_run_success_shape() -> None:
    plugin = JiraPlugin()
    ok = plugin._run(lambda: {"x": 1}, "v")
    _assert("success action_status", ok["action_status"] == "completed")
    _assert("success carries data", ok["data"] == {"x": 1})
    _assert("success error is None", ok["error"] is None)


# ---------------------------------------------------------------------------
# Blob export service resolution (attachments — the sole surviving blob-write path)
# ---------------------------------------------------------------------------


def test_store_blob_resolves_service_at_point_of_use() -> None:
    """§20.1 regression: blob_storage_service is constructed AFTER plugin
    readiness, so readiness-time resolution cached None forever and every
    export hard-failed; the fix resolves lazily at first use."""
    plugin = JiraPlugin()
    blob_service = MagicMock()
    blob_service.store_blob.return_value = {
        "action_status": "completed",
        "data": {"blob_id": "blob-attachment-1"},
    }
    orch = MagicMock()
    orch.get_service.return_value = blob_service
    plugin.orchestrator_ref = orch
    blob_id = plugin._store_blob(b"x" * 64, "attachment.bin", "application/octet-stream")
    _assert("store succeeds via point-of-use resolution", blob_id == "blob-attachment-1")
    plugin._store_blob(b"y", "again.bin", "application/octet-stream")
    _assert(
        "one get_service call across two stores (cached)",
        orch.get_service.call_count == 1,
        str(orch.get_service.call_count),
    )


def test_store_blob_unavailable_error_is_self_describing() -> None:
    plugin = JiraPlugin()
    orch = MagicMock()
    orch.get_service.return_value = None
    plugin.orchestrator_ref = orch
    raised: JiraServiceError | None = None
    try:
        plugin._store_blob(b"z" * 54321, "attachment.bin", "application/octet-stream")
    except JiraServiceError as exc:
        raised = exc
    _assert("unavailable blob storage raises the typed error", raised is not None)
    message = str(raised)
    _assert("error names the observed payload size", "54321" in message, message)


def main() -> int:
    print("\njira_plugin JIR-B issue + classification smoke tests")
    print("=" * 40)
    test_jql_row_shape()
    test_jql_short_page_ends_pagination_truthfully()
    test_jql_default_pages_internally_and_forwards_tokens()
    test_jql_target_reached_with_more_available_is_truncated()
    test_jql_max_results_narrows_this_call()
    test_jql_fields_union_no_hollow_rows()
    test_jql_requires_jql()
    test_resolve_effective_limit_default()
    test_resolve_effective_limit_pair_required_together()
    test_resolve_effective_limit_raises_ceiling()
    test_resolve_effective_limit_cap_refused_not_clamped()
    test_resolve_effective_limit_invalid_row_limit()
    test_clamp_within_ceiling_never_widens()
    test_get_issue_parses_raw()
    test_create_issue_merges_fields()
    test_update_issue_requires_fields()
    test_update_issue_applies()
    test_delete_issue()
    test_delete_requires_key()
    test_classify_generic_codes()
    test_classify_detail_codes()
    test_topology_leak_absent_both_paths()
    test_run_routes_errors()
    test_export_containment_module_is_gone()
    test_run_success_shape()
    test_store_blob_resolves_service_at_point_of_use()
    test_store_blob_unavailable_error_is_self_describing()
    print()
    print(f"Results: {_passed} passed, {len(_failed)} failed")
    if _failed:
        print("FAILED:", _failed)
        return 1
    print("All JIR-B issue smoke tests passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
