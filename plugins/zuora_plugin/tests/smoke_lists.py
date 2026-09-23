#!/usr/bin/env python3
"""list_subscriptions / list_invoices data-export smoke tests for zuora_plugin.

Business-data limits + data-export migration, 2026-08-02
(workbench/2026-08-02_business_data_limits_and_spill_floor_design_coordinator_day.md,
§7.3 "full data-export" build class). Both verbs now ALWAYS write to the
caller-supplied output_tsv_path — the previous implementation returned an
inline ``subscriptions``/``invoices`` list with no bound of any kind.

list_subscriptions pages internally against Zuora's documented page/pageSize
contract (GET /v1/subscriptions/accounts/{account-key}, ZUORA_LIST_PAGE_SIZE_MAX
per call) — the previous implementation passed neither parameter and silently
returned at most 20 (Zuora's own pageSize default) with no signal more
existed; this is the regression these pagination tests guard against.

list_invoices' vendor endpoint has no independently-confirmed pagination
contract (see billing_actions' module docstring), so it issues a single call
and applies row_limit as a post-fetch cap on what is written.

Hermetic — a ``MagicMock`` standing in for ``ZuoraClient`` (``get`` mocked
directly), no live tenant. The containment gate drives the REAL
export_containment module bound to a temp workspace root.

Exercises:
  1. list_subscriptions — writes a TSV handle, never an inline list
  2. list_subscriptions pages internally past a single 40-item vendor page
  3. list_subscriptions — a SHORT final page (< pageSize) signals no more data
     without needing to reach the effective limit
  4. list_subscriptions — cap landing exactly on a full page boundary reports
     truncated=True (can't confirm completeness without one more call)
  5. list_subscriptions REGRESSION GUARD: the previous no-params call silently
     stopped at 20 — this asserts the fix actually requests pageSize=ZUORA_LIST_PAGE_SIZE_MAX
  6-9. list_subscriptions — 4-case override-friction set (§5)
  10. list_invoices — writes a TSV handle, never an inline list
  11. list_invoices — row_limit caps what is WRITTEN from a single-call
      response (no vendor-side pagination assumed)
  12-15. list_invoices — 4-case override-friction set (§5)
  16. red-first: export path OUTSIDE every allowed root -> ExportPathRefusedError,
      no file written, no vendor call made

Run:
    SOLET_NAME=<name> .venv/bin/python3 \
        plugins/zuora_plugin/tests/smoke_lists.py

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

from zuora_plugin import billing_actions  # noqa: E402
from zuora_plugin.constants import (  # noqa: E402
    CONFIG_KEY_EXPORT_ALLOWED_ROOTS,
    DEFAULT_ROW_LIMIT,
    LIST_ROW_LIMIT_CAP,
    PARAM_ACKNOWLEDGE_OVERRIDE,
    PARAM_ROW_LIMIT,
    ZUORA_LIST_PAGE_SIZE_MAX,
)
from zuora_plugin.export_containment import (  # noqa: E402
    ExportPathRefusedError,
    assert_export_path_allowed,
)

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


def _gate_for(roots: list[str]) -> Any:
    def gate(output_tsv_path: str) -> str:
        return assert_export_path_allowed(
            output_tsv_path, roots, config_key=CONFIG_KEY_EXPORT_ALLOWED_ROOTS, plugin_name="zuora_plugin",
        )

    return gate


def _passthrough_gate(path: str) -> str:
    return path


def _list_response(list_key: str, items: list[dict[str, Any]]) -> httpx.Response:
    return httpx.Response(200, json={list_key: items, "success": True}, request=httpx.Request("GET", "https://fake/v1/x"))


def _paged_client(list_key: str, pages: list[list[dict[str, Any]]]) -> Any:
    client = MagicMock()
    client.get.side_effect = [_list_response(list_key, page) for page in pages]
    return client


def test_list_subscriptions_writes_tsv() -> None:
    client = _paged_client("subscriptions", [[{"id": "s1", "status": "Active"}, {"id": "s2", "status": "Active"}]])
    with tempfile.TemporaryDirectory(prefix="zuora_subs_shape_") as workspace:
        out_path = str(Path(workspace) / "out.tsv")
        result = billing_actions.list_subscriptions(client, {"account_id": "acc1", "output_tsv_path": out_path}, _passthrough_gate)
        _assert("row_count matches", result["row_count"] == 2)
        _assert("no subscriptions field — never inline", "subscriptions" not in result)
        lines = Path(out_path).read_text(encoding="utf-8").splitlines()
        _assert("record fields carried", "Active" in lines[1])


def test_list_subscriptions_pages_past_single_vendor_page() -> None:
    full_page = [{"id": f"s{i}"} for i in range(ZUORA_LIST_PAGE_SIZE_MAX)]
    short_page = [{"id": f"t{i}"} for i in range(10)]
    client = _paged_client("subscriptions", [full_page, short_page])
    with tempfile.TemporaryDirectory(prefix="zuora_subs_page_") as workspace:
        out_path = str(Path(workspace) / "out.tsv")
        result = billing_actions.list_subscriptions(
            client,
            {"account_id": "acc1", "output_tsv_path": out_path, PARAM_ACKNOWLEDGE_OVERRIDE: True, PARAM_ROW_LIMIT: 1000},
            _passthrough_gate,
        )
        _assert("accumulated across two pages", result["row_count"] == ZUORA_LIST_PAGE_SIZE_MAX + 10)
        _assert("two vendor calls", client.get.call_count == 2)
        first_call = client.get.call_args_list[0]
        _assert(
            "first call requests page=1, pageSize=ZUORA_LIST_PAGE_SIZE_MAX",
            first_call.kwargs["params"] == {"page": 1, "pageSize": ZUORA_LIST_PAGE_SIZE_MAX},
            str(first_call.kwargs),
        )


def test_list_subscriptions_short_final_page_not_truncated() -> None:
    short_page = [{"id": "s1"}]
    client = _paged_client("subscriptions", [short_page])
    with tempfile.TemporaryDirectory(prefix="zuora_subs_short_") as workspace:
        out_path = str(Path(workspace) / "out.tsv")
        result = billing_actions.list_subscriptions(client, {"account_id": "acc1", "output_tsv_path": out_path}, _passthrough_gate)
        _assert("short page ends pagination", client.get.call_count == 1)
        _assert("not truncated — vendor's own short-page signal", result["truncated"] is False)


def test_list_subscriptions_cap_on_full_page_boundary_is_truncated() -> None:
    full_page = [{"id": f"s{i}"} for i in range(ZUORA_LIST_PAGE_SIZE_MAX)]
    client = _paged_client("subscriptions", [full_page])
    with tempfile.TemporaryDirectory(prefix="zuora_subs_boundary_") as workspace:
        out_path = str(Path(workspace) / "out.tsv")
        result = billing_actions.list_subscriptions(
            client,
            {
                "account_id": "acc1",
                "output_tsv_path": out_path,
                PARAM_ACKNOWLEDGE_OVERRIDE: True,
                PARAM_ROW_LIMIT: ZUORA_LIST_PAGE_SIZE_MAX,
            },
            _passthrough_gate,
        )
        _assert("row_count equals the cap", result["row_count"] == ZUORA_LIST_PAGE_SIZE_MAX)
        _assert("truncated True — cap landed on a full page, more may exist", result["truncated"] is True)


def test_list_subscriptions_regression_guard_requests_full_page_size() -> None:
    """§7.3 regression: the old verb passed NO page/pageSize params, silently
    capping at Zuora's own default of 20. Assert the fix actually asks for
    ZUORA_LIST_PAGE_SIZE_MAX (40), not Zuora's smaller implicit default."""
    client = _paged_client("subscriptions", [[{"id": "s1"}]])
    billing_actions.list_subscriptions(client, {"account_id": "acc1", "output_tsv_path": "/tmp/x.tsv"}, _passthrough_gate)
    called_params = client.get.call_args_list[0].kwargs["params"]
    _assert(
        "pageSize explicitly requested at the vendor's own documented maximum",
        called_params.get("pageSize") == ZUORA_LIST_PAGE_SIZE_MAX,
        str(called_params),
    )
    _assert("pageSize requested is NOT Zuora's smaller implicit default (20)", called_params.get("pageSize") != 20)


def _list_override_friction_cases(verb: Any, list_key: str, label_prefix: str) -> None:
    with tempfile.TemporaryDirectory(prefix=f"zuora_{label_prefix}_friction_") as workspace:
        out_path = str(Path(workspace) / "out.tsv")

        client = _paged_client(list_key, [[{"id": str(i)} for i in range(DEFAULT_ROW_LIMIT + 50)]])
        result = verb(client, {"account_id": "acc1", "output_tsv_path": out_path}, _passthrough_gate)
        _assert(f"{label_prefix}: default caps at {DEFAULT_ROW_LIMIT}", result["row_count"] == DEFAULT_ROW_LIMIT)

        client = _paged_client(list_key, [[{"id": str(i)} for i in range(600)]])
        result = verb(
            client, {"account_id": "acc1", "output_tsv_path": out_path, PARAM_ACKNOWLEDGE_OVERRIDE: True, PARAM_ROW_LIMIT: 600}, _passthrough_gate,
        )
        _assert(f"{label_prefix}: override reaches 600", result["row_count"] == 600)

        client = _paged_client(list_key, [[]])
        raised = False
        try:
            verb(client, {"account_id": "acc1", "output_tsv_path": out_path, PARAM_ROW_LIMIT: 600}, _passthrough_gate)
        except ValueError:
            raised = True
        _assert(f"{label_prefix}: row_limit alone (no override flag) refused", raised)

        raised = False
        try:
            verb(client, {"account_id": "acc1", "output_tsv_path": out_path, PARAM_ACKNOWLEDGE_OVERRIDE: True}, _passthrough_gate)
        except ValueError:
            raised = True
        _assert(f"{label_prefix}: override flag alone (no row_limit) refused", raised)

        raised = False
        try:
            verb(
                client,
                {"account_id": "acc1", "output_tsv_path": out_path, PARAM_ACKNOWLEDGE_OVERRIDE: True, PARAM_ROW_LIMIT: LIST_ROW_LIMIT_CAP + 1},
                _passthrough_gate,
            )
        except ValueError:
            raised = True
        _assert(f"{label_prefix}: row_limit above the hard cap refused (not clamped)", raised)


def test_list_subscriptions_override_friction() -> None:
    _list_override_friction_cases(billing_actions.list_subscriptions, "subscriptions", "list_subscriptions")


def test_list_invoices_writes_tsv() -> None:
    client = _paged_client("invoices", [[{"id": "inv1", "amount": 100}, {"id": "inv2", "amount": 200}]])
    with tempfile.TemporaryDirectory(prefix="zuora_inv_shape_") as workspace:
        out_path = str(Path(workspace) / "out.tsv")
        result = billing_actions.list_invoices(client, {"account_id": "acc1", "output_tsv_path": out_path}, _passthrough_gate)
        _assert("row_count matches", result["row_count"] == 2)
        _assert("no invoices field — never inline", "invoices" not in result)
        _assert("single call — no confirmed vendor pagination", client.get.call_count == 1)


def test_list_invoices_row_limit_caps_single_call_response() -> None:
    client = _paged_client("invoices", [[{"id": str(i)} for i in range(50)]])
    with tempfile.TemporaryDirectory(prefix="zuora_inv_cap_") as workspace:
        out_path = str(Path(workspace) / "out.tsv")
        result = billing_actions.list_invoices(
            client, {"account_id": "acc1", "output_tsv_path": out_path, PARAM_ACKNOWLEDGE_OVERRIDE: True, PARAM_ROW_LIMIT: 10}, _passthrough_gate,
        )
        _assert("write-side cap applied", result["row_count"] == 10)
        _assert("truncated True — more than the cap were returned by the vendor call", result["truncated"] is True)


def test_list_invoices_override_friction() -> None:
    _list_override_friction_cases(billing_actions.list_invoices, "invoices", "list_invoices")


def test_export_path_outside_allowed_root_refused() -> None:
    with tempfile.TemporaryDirectory(prefix="zuora_allowed_") as allowed_root, tempfile.TemporaryDirectory(prefix="zuora_outside_") as outside_root:
        gate = _gate_for([allowed_root])
        out_path = str(Path(outside_root) / "out.tsv")
        client = MagicMock()
        raised = False
        try:
            billing_actions.list_subscriptions(client, {"account_id": "acc1", "output_tsv_path": out_path}, gate)
        except ExportPathRefusedError:
            raised = True
        _assert("outside-root path refused", raised)
        _assert("vendor never called when the path is refused", client.get.call_count == 0)
        _assert("no file written", not Path(out_path).exists())


def main() -> int:
    print("\nzuora_plugin list_subscriptions / list_invoices data-export smoke tests")
    print("=" * 47)
    test_list_subscriptions_writes_tsv()
    test_list_subscriptions_pages_past_single_vendor_page()
    test_list_subscriptions_short_final_page_not_truncated()
    test_list_subscriptions_cap_on_full_page_boundary_is_truncated()
    test_list_subscriptions_regression_guard_requests_full_page_size()
    test_list_subscriptions_override_friction()
    test_list_invoices_writes_tsv()
    test_list_invoices_row_limit_caps_single_call_response()
    test_list_invoices_override_friction()
    test_export_path_outside_allowed_root_refused()
    print()
    print(f"Results: {_passed} passed, {len(_failed)} failed")
    if _failed:
        print("FAILED:", _failed)
        return 1
    print("All list_subscriptions / list_invoices data-export smoke tests passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
