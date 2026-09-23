#!/usr/bin/env python3
"""JIR-C comment + transition smoke tests (no pytest, no live Jira).

Hermetic — a faked JIRA client with .raw-bearing Comment resources and
list-of-dict transitions. Red-first: each check asserts real parsing /
internal-pagination / override / transition behavior against the 2026-08-03
reopened design (jira EXITED the data-export requirement entirely, operator veto "no PII
in Jira"; paging is hidden inside the effective row limit, operator ruling
"the paging is an implementation detail that should be hidden" — design doc
§0.1/§5.4). There is no output_tsv_path, no containment gate, and no
caller-visible start_at/next_start_at left on this verb; a test asserting any
of those would itself be stale.

Exercises:
  1. add_comment       — returns the new comment id; passes (key, body) through
  2. add_comment       — missing body raises ValueError
  3. list_comments     — .raw parse (id/author/body/created) returned INLINE;
     author None-safe (a null author stays None, not a TSV empty string)
  4. list_comments     — a short final page (< requested page size) ends
     pagination truthfully: truncated=False
  5. list_comments     — the 500-row default is reached via internal
     pagination across Atlassian's 100/call ceiling (5 internal calls), each
     call's start_at advancing by the PRIOR call's returned page length —
     never surfaced to the caller
  6. list_comments     — reaching the target while comments still exist
     (full pages, no short-page signal) reports truncated=True — this is the
     exact bug class fixed this session: "full pages, target reached" must
     NOT be reported as complete
  7. list_comments     — max narrows the effective limit for THIS call only
     (never widens past the ceiling — see _clamp_within_ceiling unit checks)
  8. list_comments     — the acknowledge_default_limit_override/row_limit
     pair: both-required-together, row_limit above the 5,000 cap refused —
     see _resolve_effective_limit unit checks
  9. list_transitions  — list-of-dict transitions -> {id,name,to_status}
  10. transition_issue — transition invoked with id+comment; re-fetch -> new_status
  11. transition_issue — missing transition_id raises ValueError

Run:
    SOLET_NAME=<name> .venv/bin/python3 plugins/jira_plugin/tests/smoke_comments.py

Exits 0 on success, 1 on first failure.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT / "plugins" / "jira_plugin" / "src"))

from jira_plugin import comment_actions  # noqa: E402
from jira_plugin.constants import DEFAULT_ROW_LIMIT, ROW_LIMIT_CAP  # noqa: E402

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


def _comment(raw: dict[str, Any]) -> MagicMock:
    c = MagicMock()
    c.raw = raw
    return c


def _paged_comments_client(all_raw: list[dict[str, Any]]) -> MagicMock:
    """A fake client whose .comments() honors start_at/max_results like the
    real Atlassian endpoint — pages the given raw-comment list by offset."""
    client = MagicMock()

    def _comments(key: str, start_at: int = 0, max_results: int = 100) -> list[MagicMock]:
        page = all_raw[start_at : start_at + max_results]
        return [_comment(raw) for raw in page]

    client.comments.side_effect = _comments
    return client


# ---------------------------------------------------------------------------
# add_comment
# ---------------------------------------------------------------------------


def test_add_comment() -> None:
    client = MagicMock()
    created = MagicMock()
    created.id = "cmt-1"
    client.add_comment.return_value = created
    result = comment_actions.add_comment(client, {"key": "EXAMPLE-1", "body": "looks good"})
    _assert("returns comment_id", result["comment_id"] == "cmt-1")
    args = client.add_comment.call_args.args
    _assert("passes key + body", args == ("EXAMPLE-1", "looks good"))


def test_add_comment_requires_body() -> None:
    raised = False
    try:
        comment_actions.add_comment(MagicMock(), {"key": "EXAMPLE-1"})
    except ValueError:
        raised = True
    _assert("missing body raises ValueError", raised)


# ---------------------------------------------------------------------------
# list_comments
# ---------------------------------------------------------------------------


def test_list_comments_parse() -> None:
    raw = [
        {"id": "c1", "author": {"displayName": "Alice"}, "body": "first", "created": "2026-07-01T00:00:00Z"},
        {"id": "c2", "author": None, "body": "second", "created": "2026-07-02T00:00:00Z"},
    ]
    client = _paged_comments_client(raw)
    result = comment_actions.list_comments(client, {"key": "EXAMPLE-1"})
    _assert("row_count is 2", result["row_count"] == 2)
    _assert("comments returned inline", "comments" in result)
    _assert("short page (2 < 100) -> not truncated", result["truncated"] is False)
    rows = result["comments"]
    _assert("author flattened", rows[0]["author"] == "Alice")
    _assert("body parsed", rows[0]["body"] == "first")
    _assert("null author is None (inline, not a TSV empty-string artifact)", rows[1]["author"] is None)


def test_list_comments_default_pages_internally() -> None:
    raw = [{"id": f"c{i}", "author": None, "body": "x", "created": "2026-07-01T00:00:00Z"} for i in range(550)]
    client = _paged_comments_client(raw)
    result = comment_actions.list_comments(client, {"key": "EXAMPLE-1"})
    calls = client.comments.call_args_list
    _assert(f"5 internal calls to reach the {DEFAULT_ROW_LIMIT}-row default", len(calls) == 5)
    _assert(f"row_count is exactly {DEFAULT_ROW_LIMIT}", result["row_count"] == DEFAULT_ROW_LIMIT)
    _assert("first call starts at offset 0", calls[0].kwargs.get("start_at") == 0)
    for i in range(1, len(calls)):
        _assert(
            f"call {i + 1}'s start_at advances by 100 over call {i}",
            calls[i].kwargs.get("start_at") == calls[i - 1].kwargs.get("start_at") + 100,
        )
    _assert(
        "caller never sees start_at/next_start_at anywhere in the result",
        "start_at" not in result and "next_start_at" not in result,
    )


def test_list_comments_short_page_ends_pagination_truthfully() -> None:
    raw = [{"id": f"c{i}", "author": None, "body": "x", "created": "2026-07-01T00:00:00Z"} for i in range(250)]
    client = _paged_comments_client(raw)
    result = comment_actions.list_comments(client, {"key": "EXAMPLE-1"})
    _assert("all 250 comments collected across pages", result["row_count"] == 250)
    _assert("short final page -> not truncated", result["truncated"] is False)
    _assert("exactly 3 internal calls (100+100+50)", client.comments.call_count == 3)


def test_list_comments_target_reached_with_more_available_is_truncated() -> None:
    # This is the exact bug class fixed this session: reaching the target via
    # full pages (never a short page) must report truncated=True, not False.
    raw = [{"id": f"c{i}", "author": None, "body": "x", "created": "2026-07-01T00:00:00Z"} for i in range(1000)]
    client = _paged_comments_client(raw)
    result = comment_actions.list_comments(
        client, {"key": "EXAMPLE-1", "acknowledge_default_limit_override": True, "row_limit": 200}
    )
    _assert("row_count is exactly the 200 row_limit", result["row_count"] == 200)
    _assert("truncated=True — full pages, no short-page confirmation of end-of-data", result["truncated"] is True)
    _assert("exactly 2 internal calls (100+100)", client.comments.call_count == 2)


def test_list_comments_max_narrows_this_call() -> None:
    raw = [{"id": f"c{i}", "author": None, "body": "x", "created": "2026-07-01T00:00:00Z"} for i in range(300)]
    client = _paged_comments_client(raw)
    result = comment_actions.list_comments(client, {"key": "EXAMPLE-1", "max": 30})
    _assert("max narrows row_count", result["row_count"] == 30)
    _assert("single internal call suffices", client.comments.call_count == 1)
    _assert("max_results sent as 30, not the page ceiling", client.comments.call_args.kwargs.get("max_results") == 30)


def test_list_comments_requires_key() -> None:
    raised = False
    try:
        comment_actions.list_comments(MagicMock(), {})
    except ValueError:
        raised = True
    _assert("missing key raises ValueError", raised)


# ---------------------------------------------------------------------------
# override pair + clamp — unit checks on the pure helpers (own copy, mirrors
# issue_actions' — each module owns its own §5 helpers, not shared)
# ---------------------------------------------------------------------------


def test_resolve_effective_limit_default() -> None:
    _assert(
        "no override -> DEFAULT_ROW_LIMIT",
        comment_actions._resolve_effective_limit({}, verb="list_comments") == DEFAULT_ROW_LIMIT,
    )


def test_resolve_effective_limit_pair_required_together() -> None:
    raised_override_only = False
    try:
        comment_actions._resolve_effective_limit({"acknowledge_default_limit_override": True}, verb="list_comments")
    except ValueError:
        raised_override_only = True
    _assert("override alone (no row_limit) raises ValueError", raised_override_only)

    raised_row_limit_only = False
    try:
        comment_actions._resolve_effective_limit({"row_limit": 1000}, verb="list_comments")
    except ValueError:
        raised_row_limit_only = True
    _assert("row_limit alone (no override) raises ValueError", raised_row_limit_only)


def test_resolve_effective_limit_cap_refused_not_clamped() -> None:
    raised = False
    try:
        comment_actions._resolve_effective_limit(
            {"acknowledge_default_limit_override": True, "row_limit": ROW_LIMIT_CAP + 1}, verb="list_comments"
        )
    except ValueError:
        raised = True
    _assert(f"row_limit above the {ROW_LIMIT_CAP} cap is refused, not silently clamped", raised)


def test_clamp_within_ceiling_never_widens() -> None:
    _assert("a value above the ceiling is clamped down", comment_actions._clamp_within_ceiling(99999, 500) == 500)
    _assert("an absent value falls back to the ceiling", comment_actions._clamp_within_ceiling(None, 500) == 500)


# ---------------------------------------------------------------------------
# transitions
# ---------------------------------------------------------------------------


def test_list_transitions() -> None:
    client = MagicMock()
    client.transitions.return_value = [
        {"id": "11", "name": "Start Progress", "to": {"name": "In Progress"}},
        {"id": "21", "name": "Done", "to": {"name": "Done"}},
    ]
    result = comment_actions.list_transitions(client, {"key": "EXAMPLE-1"})
    rows = result["transitions"]
    _assert("two transitions", len(rows) == 2)
    _assert("id carried", rows[0]["id"] == "11")
    _assert("name carried", rows[0]["name"] == "Start Progress")
    _assert("to_status flattened", rows[0]["to_status"] == "In Progress")


def test_transition_issue() -> None:
    client = MagicMock()
    client.issue.return_value.raw = {"fields": {"status": {"name": "In Progress"}}}
    result = comment_actions.transition_issue(
        client, {"key": "EXAMPLE-1", "transition_id": "11", "comment": "moving it"}
    )
    _assert("returns ok", result["ok"] is True)
    _assert("new_status re-fetched", result["new_status"] == "In Progress")
    args = client.transition_issue.call_args
    _assert("transition invoked with key+id", args.args == ("EXAMPLE-1", "11"))
    _assert("comment passed through", args.kwargs.get("comment") == "moving it")


def test_transition_requires_id() -> None:
    raised = False
    try:
        comment_actions.transition_issue(MagicMock(), {"key": "EXAMPLE-1"})
    except ValueError:
        raised = True
    _assert("missing transition_id raises ValueError", raised)


def main() -> int:
    print("\njira_plugin JIR-C comment + transition smoke tests")
    print("=" * 40)
    test_add_comment()
    test_add_comment_requires_body()
    test_list_comments_parse()
    test_list_comments_default_pages_internally()
    test_list_comments_short_page_ends_pagination_truthfully()
    test_list_comments_target_reached_with_more_available_is_truncated()
    test_list_comments_max_narrows_this_call()
    test_list_comments_requires_key()
    test_resolve_effective_limit_default()
    test_resolve_effective_limit_pair_required_together()
    test_resolve_effective_limit_cap_refused_not_clamped()
    test_clamp_within_ceiling_never_widens()
    test_list_transitions()
    test_transition_issue()
    test_transition_requires_id()
    print()
    print(f"Results: {_passed} passed, {len(_failed)} failed")
    if _failed:
        print("FAILED:", _failed)
        return 1
    print("All JIR-C comment smoke tests passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
