#!/usr/bin/env python3
"""Hide-paging (2026-08-03 change) smoke tests for marketo_plugin.

Operator ruling (2026-08-03, verbatim: "we need to deliver the results - the
paging is an implementation detail that should be hidden"), design doc
§5.4/§7.2 as amended (ruled doc-wide by Coordinator-Day). get_leads,
get_activities, list_campaigns, and list_static_lists now page internally
across Marketo's 300-per-call vendor ceiling up to a §5 override-governed
effective row limit — no next_page_token/more_result field survives on any
of the four, replaced by a ``truncated`` boolean. Basic 2-page accumulation
and hidden-field coverage for each verb already lives in smoke_leads.py /
smoke_campaigns_lists.py; this file is the shared §5 override-friction
4-case set (per verb) plus the named acceptance scenario from the dispatch:
request 500 -> 2 internal pages -> one complete file.

Hermetic — a MagicMock standing in for MarketoClient (get_json mocked
directly, side_effect list of decoded envelope dicts), no live instance. A
real passthrough gate (unit tests of marketing_actions functions directly,
not the plugin's containment gate; see smoke_data_export.py for that).

Exercises, per verb (get_leads, get_activities, list_campaigns,
list_static_lists):
  1. named acceptance scenario — request DEFAULT_ROW_LIMIT (500): 2 internal
     vendor calls (MARKETO_LIST_PAGE_ROW_CAP=300 each), one complete written
     file, not truncated when the vendor's own last page is short
  2. default (no override) against >DEFAULT_ROW_LIMIT records available:
     fetch stops at DEFAULT_ROW_LIMIT, truncated True
  3. override with a valid row_limit above DEFAULT_ROW_LIMIT and below
     MARKETO_LIST_ROW_LIMIT_CAP: fetch reaches the requested count
  4. override flag present with row_limit absent, or row_limit present
     without the override flag: fails loud, no silent partial-honor
  5. row_limit above MARKETO_LIST_ROW_LIMIT_CAP: refused, not clamped

Run:
    SOLET_NAME=<name> .venv/bin/python3 \
        plugins/marketo_plugin/tests/smoke_internal_paging.py

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

from marketo_plugin import marketing_actions  # noqa: E402
from marketo_plugin.constants import (  # noqa: E402
    DEFAULT_ROW_LIMIT,
    MARKETO_LIST_PAGE_ROW_CAP,
    MARKETO_LIST_ROW_LIMIT_CAP,
    PARAM_ACKNOWLEDGE_OVERRIDE,
    PARAM_ROW_LIMIT,
)

_passed = 0
_failed: list[str] = []
_TMP_DIR = tempfile.mkdtemp(prefix="marketo_smoke_internal_paging_")
_path_counter = {"n": 0}


def _assert(label: str, cond: bool, msg: str = "") -> None:
    global _passed
    if cond:
        _passed += 1
        print(f"  PASS  {label}")
    else:
        _failed.append(label)
        print(f"  FAIL  {label}: {msg or 'assertion failed'}")


def _tmp_tsv_path() -> str:
    _path_counter["n"] += 1
    return str(Path(_TMP_DIR) / f"out_{_path_counter['n']}.tsv")


def _passthrough_gate(path: str) -> str:
    return path


def _full_page(n: int, start: int = 0) -> list[dict[str, Any]]:
    return [{"id": i} for i in range(start, start + n)]


def _token_pages_client(pages: list[list[dict[str, Any]]]) -> Any:
    """A MagicMock whose get_json returns successive nextPageToken-carrying
    pages; the last page in the list has no token (vendor-confirmed end)."""
    responses: list[dict[str, Any]] = []
    for i, page in enumerate(pages):
        is_last = i == len(pages) - 1
        responses.append(
            {
                "success": True,
                "result": page,
                "nextPageToken": None if is_last else f"tok-{i}",
                "moreResult": False,
            }
        )
    client = MagicMock()
    client.get_json.side_effect = responses
    return client


def _activity_pages_client(pages: list[list[dict[str, Any]]]) -> Any:
    """A MagicMock for get_activities: a mint call first, then successive
    moreResult-driven pages; the last page reports moreResult=False."""
    responses: list[dict[str, Any]] = [{"success": True, "nextPageToken": "tok-mint"}]
    for i, page in enumerate(pages):
        is_last = i == len(pages) - 1
        responses.append(
            {
                "success": True,
                "result": page,
                "nextPageToken": None if is_last else f"tok-{i}",
                "moreResult": not is_last,
            }
        )
    client = MagicMock()
    client.get_json.side_effect = responses
    return client


# ---------------------------------------------------------------------------
# get_leads
# ---------------------------------------------------------------------------


def test_get_leads_named_acceptance_scenario() -> None:
    """Request 500 -> 2 internal pages (300 + 200) -> one complete file."""
    client = _token_pages_client([_full_page(MARKETO_LIST_PAGE_ROW_CAP), _full_page(200, start=MARKETO_LIST_PAGE_ROW_CAP)])
    result = marketing_actions.get_leads(
        client, {"filter_type": "id", "filter_values": ["x"], "output_tsv_path": _tmp_tsv_path()}, _passthrough_gate,
    )
    _assert("get_leads named scenario: exactly 2 internal vendor calls", client.get_json.call_count == 2)
    _assert("get_leads named scenario: one complete file with 500 rows", result["row_count"] == DEFAULT_ROW_LIMIT)
    _assert("get_leads named scenario: not truncated — vendor confirmed the end", result["truncated"] is False)


def test_get_leads_override_friction() -> None:
    _override_friction_cases(
        lambda client, params: marketing_actions.get_leads(
            client, {"filter_type": "id", "filter_values": ["x"], **params}, _passthrough_gate,
        ),
        "get_leads",
    )


# ---------------------------------------------------------------------------
# get_activities
# ---------------------------------------------------------------------------


def test_get_activities_named_acceptance_scenario() -> None:
    client = _activity_pages_client([_full_page(MARKETO_LIST_PAGE_ROW_CAP), _full_page(200, start=MARKETO_LIST_PAGE_ROW_CAP)])
    result = marketing_actions.get_activities(
        client,
        {"since_datetime": "2026-08-03T00:00:00Z", "activity_type_ids": [1], "output_tsv_path": _tmp_tsv_path()},
        _passthrough_gate,
    )
    _assert("get_activities named scenario: mint + 2 internal vendor calls", client.get_json.call_count == 3)
    _assert("get_activities named scenario: one complete file with 500 rows", result["row_count"] == DEFAULT_ROW_LIMIT)
    _assert("get_activities named scenario: not truncated — moreResult went false", result["truncated"] is False)


def test_get_activities_override_friction() -> None:
    def _call(client: Any, params: dict[str, Any]) -> dict[str, Any]:
        return marketing_actions.get_activities(
            client,
            {"since_datetime": "2026-08-03T00:00:00Z", "activity_type_ids": [1], **params},
            _passthrough_gate,
        )

    _override_friction_cases(_call, "get_activities", activity_shaped=True)


# ---------------------------------------------------------------------------
# list_campaigns
# ---------------------------------------------------------------------------


def test_list_campaigns_named_acceptance_scenario() -> None:
    client = _token_pages_client([_full_page(MARKETO_LIST_PAGE_ROW_CAP), _full_page(200, start=MARKETO_LIST_PAGE_ROW_CAP)])
    result = marketing_actions.list_campaigns(client, {"output_tsv_path": _tmp_tsv_path()}, _passthrough_gate)
    _assert("list_campaigns named scenario: exactly 2 internal vendor calls", client.get_json.call_count == 2)
    _assert("list_campaigns named scenario: one complete file with 500 rows", result["row_count"] == DEFAULT_ROW_LIMIT)
    _assert("list_campaigns named scenario: not truncated", result["truncated"] is False)


def test_list_campaigns_override_friction() -> None:
    _override_friction_cases(
        lambda client, params: marketing_actions.list_campaigns(client, params, _passthrough_gate), "list_campaigns",
    )


# ---------------------------------------------------------------------------
# list_static_lists
# ---------------------------------------------------------------------------


def test_list_static_lists_named_acceptance_scenario() -> None:
    client = _token_pages_client([_full_page(MARKETO_LIST_PAGE_ROW_CAP), _full_page(200, start=MARKETO_LIST_PAGE_ROW_CAP)])
    result = marketing_actions.list_static_lists(client, {"output_tsv_path": _tmp_tsv_path()}, _passthrough_gate)
    _assert("list_static_lists named scenario: exactly 2 internal vendor calls", client.get_json.call_count == 2)
    _assert("list_static_lists named scenario: one complete file with 500 rows", result["row_count"] == DEFAULT_ROW_LIMIT)
    _assert("list_static_lists named scenario: not truncated", result["truncated"] is False)


def test_list_static_lists_override_friction() -> None:
    _override_friction_cases(
        lambda client, params: marketing_actions.list_static_lists(client, params, _passthrough_gate), "list_static_lists",
    )


# ---------------------------------------------------------------------------
# Shared §5 override-friction 4-case set
# ---------------------------------------------------------------------------


def _endless_full_pages_client(*, activity_shaped: bool) -> Any:
    """A client that always returns a full MARKETO_LIST_PAGE_ROW_CAP page
    with a continuation token — enough pages for any cap this suite uses."""
    client = MagicMock()
    call = {"n": 0}

    def _side_effect(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        call["n"] += 1
        if activity_shaped and call["n"] == 1:
            return {"success": True, "nextPageToken": "tok-mint"}
        return {
            "success": True,
            "result": _full_page(MARKETO_LIST_PAGE_ROW_CAP),
            "nextPageToken": f"tok-{call['n']}",
            "moreResult": True,
        }

    client.get_json.side_effect = _side_effect
    return client


def _override_friction_cases(call: Any, label_prefix: str, *, activity_shaped: bool = False) -> None:
    # 1. default (no override) against more than DEFAULT_ROW_LIMIT available: stops at the default, truncated.
    client = _endless_full_pages_client(activity_shaped=activity_shaped)
    result = call(client, {"output_tsv_path": _tmp_tsv_path()})
    _assert(f"{label_prefix}: default caps at {DEFAULT_ROW_LIMIT}", result["row_count"] == DEFAULT_ROW_LIMIT)
    _assert(f"{label_prefix}: default-capped run is truncated", result["truncated"] is True)

    # 2. override reaches a value above the default, below the hard cap.
    target = DEFAULT_ROW_LIMIT + 300
    client = _endless_full_pages_client(activity_shaped=activity_shaped)
    result = call(
        client,
        {"output_tsv_path": _tmp_tsv_path(), PARAM_ACKNOWLEDGE_OVERRIDE: True, PARAM_ROW_LIMIT: target},
    )
    _assert(f"{label_prefix}: override reaches {target}", result["row_count"] == target)

    # 3a. row_limit alone (no override flag) refused.
    raised = False
    try:
        call(client, {"output_tsv_path": _tmp_tsv_path(), PARAM_ROW_LIMIT: target})
    except ValueError:
        raised = True
    _assert(f"{label_prefix}: row_limit alone (no override flag) refused", raised)

    # 3b. override flag alone (no row_limit) refused.
    raised = False
    try:
        call(client, {"output_tsv_path": _tmp_tsv_path(), PARAM_ACKNOWLEDGE_OVERRIDE: True})
    except ValueError:
        raised = True
    _assert(f"{label_prefix}: override flag alone (no row_limit) refused", raised)

    # 4. row_limit above the hard cap refused, not clamped.
    raised = False
    try:
        call(
            client,
            {
                "output_tsv_path": _tmp_tsv_path(),
                PARAM_ACKNOWLEDGE_OVERRIDE: True,
                PARAM_ROW_LIMIT: MARKETO_LIST_ROW_LIMIT_CAP + 1,
            },
        )
    except ValueError:
        raised = True
    _assert(f"{label_prefix}: row_limit above the hard cap refused (not clamped)", raised)


def main() -> int:
    print("\nmarketo_plugin hide-paging (internal pagination) smoke tests")
    print("=" * 71)
    test_get_leads_named_acceptance_scenario()
    test_get_leads_override_friction()
    test_get_activities_named_acceptance_scenario()
    test_get_activities_override_friction()
    test_list_campaigns_named_acceptance_scenario()
    test_list_campaigns_override_friction()
    test_list_static_lists_named_acceptance_scenario()
    test_list_static_lists_override_friction()
    print()
    print(f"Results: {_passed} passed, {len(_failed)} failed")
    if _failed:
        print("FAILED:", _failed)
        return 1
    print("All hide-paging smoke tests passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
