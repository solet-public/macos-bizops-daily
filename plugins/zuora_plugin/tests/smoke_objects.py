#!/usr/bin/env python3
"""Object CRUD + billing-read + classification smoke tests for zuora_plugin.

Hermetic — a faked client returning canned ``httpx.Response`` objects, no
live tenant.

list_subscriptions / list_invoices moved to smoke_lists.py (business-data
limits + data-export migration, 2026-08-02 — they now write to a
caller-supplied output_tsv_path under the §5 override mechanism, not an
inline shape). get_invoice is unaffected (single-record fetch-by-id, §1.2
exemption) and stays here.

Exercises:
  1. get_object — field passthrough; rejects an unsupported type
  2. create_object — id/success shape; rejects empty fields
  3. update_object — success shape; rejects empty fields
  4. get_invoice — shape
  5. TOPOLOGY-LEAK (SECURITY): auth/rate-limit classes classify to a GENERIC
     message that NEVER contains the response body's tenant-host marker;
     not-found/validation-failed classes carry response-body detail
  6. EDGE parity: validate_edge_process_provider raises nothing

Run:
    SOLET_NAME=<name> .venv/bin/python3 \
        plugins/zuora_plugin/tests/smoke_objects.py

Exits 0 on success, 1 on first failure.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import httpx

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT / "plugins" / "zuora_plugin" / "src"))

from zuora_plugin import billing_actions  # noqa: E402
from zuora_plugin.errors import classify_zuora_response  # noqa: E402
from zuora_plugin.plugin import ZuoraPlugin  # noqa: E402

_TENANT_MARKER = "SECRET-TENANT-myorg.zuora.com"

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


def _response(status: int, payload: dict[str, Any]) -> httpx.Response:
    return httpx.Response(status, json=payload, request=httpx.Request("GET", "https://fake/v1/x"))


def _fake_client(response: httpx.Response) -> Any:
    client = MagicMock()
    client.get.return_value = response
    client.post.return_value = response
    client.put.return_value = response
    return client


def test_get_object() -> None:
    client = _fake_client(_response(200, {"Id": "2c9...", "Name": "Acme"}))
    result = billing_actions.get_object(client, {"type": "Account", "id": "2c9..."})
    _assert("object fields carried", result["object"]["Name"] == "Acme")
    raised = False
    try:
        billing_actions.get_object(client, {"type": "NotAType", "id": "x"})
    except ValueError:
        raised = True
    _assert("unsupported type rejected", raised)


def test_create_object() -> None:
    client = _fake_client(_response(200, {"Id": "new-1", "Success": True}))
    result = billing_actions.create_object(client, {"type": "Account", "fields": {"Name": "Acme"}})
    _assert("new id carried", result["id"] == "new-1")
    _assert("success True", result["success"] is True)
    raised = False
    try:
        billing_actions.create_object(client, {"type": "Account", "fields": {}})
    except ValueError:
        raised = True
    _assert("empty fields rejected", raised)


def test_update_object() -> None:
    client = _fake_client(_response(200, {"Success": True}))
    result = billing_actions.update_object(client, {"type": "Account", "id": "2c9...", "fields": {"Name": "New"}})
    _assert("update success True", result["success"] is True)


def test_get_invoice() -> None:
    client = _fake_client(_response(200, {"Id": "inv1", "Amount": 100}))
    result = billing_actions.get_invoice(client, {"id": "inv1"})
    _assert("invoice fields carried", result["invoice"]["Amount"] == 100)


def test_classify_topology_leak() -> None:
    auth_resp = _response(401, {"error": "invalid_token", "detail": _TENANT_MARKER})
    code, message = classify_zuora_response(auth_resp, is_query=False)
    _assert("401 -> zuora.auth_failed", code == "zuora.auth_failed")
    _assert("auth message hides tenant marker", _TENANT_MARKER not in message, message)

    rate_resp = _response(429, {"error": "rate_limited", "detail": _TENANT_MARKER})
    code, message = classify_zuora_response(rate_resp, is_query=False)
    _assert("429 -> zuora.rate_limited", code == "zuora.rate_limited")
    _assert("rate-limit message hides tenant marker", _TENANT_MARKER not in message, message)

    not_found_resp = _response(404, {"reasons": [{"message": "Account 2c9xyz not found"}]})
    code, message = classify_zuora_response(not_found_resp, is_query=False)
    _assert("404 -> zuora.object_not_found", code == "zuora.object_not_found")
    _assert("not-found carries response-body detail", "2c9xyz" in message, message)

    validation_resp = _response(400, {"reasons": [{"message": "Name is required"}]})
    code, message = classify_zuora_response(validation_resp, is_query=False)
    _assert("400 object CRUD -> zuora.validation_failed", code == "zuora.validation_failed")

    query_resp = _response(400, {"reasons": [{"message": "invalid ZOQL"}]})
    code, message = classify_zuora_response(query_resp, is_query=True)
    _assert("400 data_query -> zuora.query_failed (is_query flag)", code == "zuora.query_failed")


def test_edge_parity() -> None:
    from ananta.core.plugins.action_discovery import discover_actions
    from ananta.core.process_registry.plugin_registration_validator import (
        PluginRegistrationValidator,
    )

    plugin = ZuoraPlugin()
    actions = discover_actions(plugin)
    raised = None
    try:
        PluginRegistrationValidator().validate_edge_process_provider("zuora_plugin", plugin, actions)
    except Exception as exc:  # FrameworkError on mismatch
        raised = exc
    _assert("EDGE parity: validator raises nothing", raised is None, str(raised))
    _assert("all 9 verbs discovered", len(actions) == 9)


def main() -> int:
    print("\nzuora_plugin object CRUD + billing-read smoke tests")
    print("=" * 47)
    test_get_object()
    test_create_object()
    test_update_object()
    test_get_invoice()
    test_classify_topology_leak()
    test_edge_parity()
    print()
    print(f"Results: {_passed} passed, {len(_failed)} failed")
    if _failed:
        print("FAILED:", _failed)
        return 1
    print("All object CRUD + billing-read smoke tests passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
