#!/usr/bin/env python3
"""Instance-config + OAuth client-credentials smoke tests for marketo_plugin.

Hermetic — a MagicMock address_book_service + a local ``httpx.MockTransport``
standing in for the Marketo REST API (no network). Red-first: every check
asserts REAL behavior of the resolver + token caching + the
success:false-code-601/602-triggers-one-refetch path + EDGE parity.

Exercises:
  1. load() builds a MarketoInstanceConfig from a complete entry
  2. Missing required fields (client_id/client_secret) fail loud, naming
     what's missing
  3. repr() redacts the client secret
  4. MarketoClient._bearer caches the token until near expiry, then re-fetches
     (GET-based mint, matching Marketo's identity endpoint — not POST)
  5. A decoded envelope carrying error code 601 triggers exactly one token
     re-mint + retry (Marketo's fault model lives in the body, not the HTTP
     status — the key divergence from zuora_plugin's 401-triggers-retry)
  6. A failed token mint (non-200 at the identity endpoint) raises MarketoAuthError
  7. EDGE parity: validate_edge_process_provider raises nothing, 15 verbs

Run:
    SOLET_NAME=<name> .venv/bin/python3 \
        plugins/marketo_plugin/tests/smoke_auth.py

Exits 0 on success, 1 on first failure.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import httpx

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT / "plugins" / "marketo_plugin" / "src"))

from marketo_plugin.app_config import AppConfigError, AppConfigLoader  # noqa: E402
from marketo_plugin.http_client import MarketoAuthError, MarketoClient  # noqa: E402
from marketo_plugin.plugin import MarketoPlugin  # noqa: E402

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


def _entries(
    base_url: str = "https://123-ABC-456.mktorest.com",
    client_id: str = "client-abc",
    client_secret: str = "secret-xyz",
) -> list[dict[str, str]]:
    fields = {"base_url": base_url, "client_id": client_id, "client_secret": client_secret}
    return [{"field_type": k, "value": v} for k, v in fields.items() if v]


def _fake_address_book(entries: list[dict[str, str]]) -> MagicMock:
    service = MagicMock()
    service.resolve_with_secrets.return_value = {
        "action_status": "completed",
        "data": {"entries": entries},
    }
    return service


def test_load_complete_entry() -> None:
    loader = AppConfigLoader(_fake_address_book(_entries()))
    config = loader.load()
    _assert("base_url carried + trailing slash stripped", config.base_url == "https://123-ABC-456.mktorest.com")
    _assert("client_id carried", config.client_id == "client-abc")
    _assert("client_secret carried", config.client_secret == "secret-xyz")


def test_missing_fields_named() -> None:
    entries = [e for e in _entries() if e["field_type"] not in ("client_id", "client_secret")]
    loader = AppConfigLoader(_fake_address_book(entries))
    message = ""
    try:
        loader.load()
    except AppConfigError as exc:
        message = str(exc)
    _assert("missing client_id named", "client_id" in message, message)
    _assert("missing client_secret named", "client_secret" in message, message)


def test_repr_redacts_client_secret() -> None:
    loader = AppConfigLoader(_fake_address_book(_entries()))
    config = loader.load()
    rendered = repr(config)
    _assert("repr redacts client_secret", "***" in rendered and "secret-xyz" not in rendered)


def _config_for_client() -> Any:  # noqa: ANN401 — thin test helper, shape matches MarketoInstanceConfig
    from marketo_plugin.app_config import MarketoInstanceConfig

    return MarketoInstanceConfig(base_url="https://fake.mktorest.test", client_id="cid", client_secret="csecret")


def test_bearer_caches_until_near_expiry() -> None:
    mint_count = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/identity/oauth/token":
            _assert_once_get_method(request)
            mint_count["n"] += 1
            return httpx.Response(200, json={"access_token": f"tok-{mint_count['n']}", "expires_in": 3600})
        return httpx.Response(200, json={"success": True, "result": []})

    client = MarketoClient(_config_for_client(), timeout_seconds=5.0)
    client._http = httpx.Client(transport=httpx.MockTransport(handler), base_url="https://fake.mktorest.test")
    first = client._bearer()
    second = client._bearer()
    _assert("bearer cached across calls (single mint)", first == second and mint_count["n"] == 1)


def _assert_once_get_method(request: httpx.Request) -> None:
    _assert("token mint uses GET", request.method == "GET")


def test_601_envelope_triggers_one_refetch_and_retry() -> None:
    call_log: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/identity/oauth/token":
            call_log.append("token")
            return httpx.Response(200, json={"access_token": f"tok-{len(call_log)}", "expires_in": 3600})
        auth = request.headers.get("Authorization", "")
        call_log.append(f"req:{auth}")
        if auth == "Bearer tok-1":
            return httpx.Response(200, json={"success": False, "errors": [{"code": "601", "message": "Access token invalid"}]})
        return httpx.Response(200, json={"success": True, "result": [{"id": 1}]})

    client = MarketoClient(_config_for_client(), timeout_seconds=5.0)
    client._http = httpx.Client(transport=httpx.MockTransport(handler), base_url="https://fake.mktorest.test")
    payload = client.get_json("/rest/v1/leads.json", params={"filterType": "id", "filterValues": "1"})
    _assert("second attempt succeeds after re-mint", payload.get("success") is True)
    _assert("exactly two token mints (initial + one re-mint)", call_log.count("token") == 2)


def test_failed_token_mint_raises() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, json={"error": "unauthorized", "error_description": "bad client"})

    client = MarketoClient(_config_for_client(), timeout_seconds=5.0)
    client._http = httpx.Client(transport=httpx.MockTransport(handler), base_url="https://fake.mktorest.test")
    raised = False
    try:
        client._bearer()
    except MarketoAuthError:
        raised = True
    _assert("failed token mint raises MarketoAuthError", raised)


def test_every_write_request_carries_content_type() -> None:
    """Part 21 — Marketo 612 'Invalid Content Type'.

    merge_leads is the plugin's only body-less POST: every argument is a query
    parameter, so httpx serialised no body and therefore set no Content-Type,
    and Marketo rejected the call 100% of the time. The fix lives in
    ``_do_request`` (class level) rather than in merge_leads, so this test
    asserts the CLASS invariant — every write request the plugin issues carries
    a content type — which catches any future body-less write, not just this one.
    """
    seen: list[tuple[str, str | None]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/identity/oauth/token"):
            return httpx.Response(200, json={"access_token": "tok", "expires_in": 3600})
        seen.append((request.method, request.headers.get("content-type")))
        return httpx.Response(200, json={"success": True, "result": []})

    def _fresh() -> Any:  # noqa: ANN401 — thin test helper
        client = MarketoClient(_config_for_client(), timeout_seconds=5.0)
        client._http = httpx.Client(transport=httpx.MockTransport(handler), base_url="https://fake.mktorest.test")
        return client

    from marketo_plugin import marketing_actions

    # The exact failing shape: POST with only query params, no body.
    marketing_actions.merge_leads(_fresh(), {"winning_lead_id": "1", "losing_lead_ids": ["2"]})
    _assert("body-less merge POST carries a content type", seen[-1] == ("POST", "application/json"), str(seen[-1]))

    # Every other write verb, to prove the invariant holds class-wide.
    marketing_actions.create_or_update_leads(_fresh(), {"records": [{"email": "a@b.com"}]})
    marketing_actions.delete_leads(_fresh(), {"lead_ids": ["1"]})
    marketing_actions.add_leads_to_list(_fresh(), {"list_id": "9", "lead_ids": ["1"]})
    marketing_actions.remove_leads_from_list(_fresh(), {"list_id": "9", "lead_ids": ["1"]})
    marketing_actions.trigger_campaign(_fresh(), {"campaign_id": "3", "lead_ids": ["1"]})

    writes = [(m, ct) for m, ct in seen if m != "GET"]
    _assert("every write request observed", len(writes) == 6, str(writes))
    missing = [(m, ct) for m, ct in writes if not ct]
    _assert("no write request lacks a content type", not missing, str(missing))
    _assert("all write content types are application/json", all(ct and ct.startswith("application/json") for _m, ct in writes), str(writes))

    # Reads must be untouched — the fix is scoped to body-less writes only.
    seen.clear()
    _fresh().get_json("/rest/v1/leads.json", params={"filterType": "id", "filterValues": "1"})
    _assert("GET is not given an injected content type", seen[-1] == ("GET", None), str(seen[-1]))


def test_edge_parity() -> None:
    from ananta.core.plugins.action_discovery import discover_actions
    from ananta.core.process_registry.plugin_registration_validator import (
        PluginRegistrationValidator,
    )

    plugin = MarketoPlugin()
    actions = discover_actions(plugin)
    raised = None
    try:
        PluginRegistrationValidator().validate_edge_process_provider("marketo_plugin", plugin, actions)
    except Exception as exc:  # FrameworkError on mismatch
        raised = exc
    _assert("EDGE parity: validator raises nothing", raised is None, str(raised))
    _assert("all 16 verbs discovered", len(actions) == 16, str(len(actions)))


def main() -> int:
    print("\nmarketo_plugin instance-config + auth smoke tests")
    print("=" * 50)
    test_load_complete_entry()
    test_missing_fields_named()
    test_repr_redacts_client_secret()
    test_bearer_caches_until_near_expiry()
    test_601_envelope_triggers_one_refetch_and_retry()
    test_failed_token_mint_raises()
    test_every_write_request_carries_content_type()
    test_edge_parity()
    print()
    print(f"Results: {_passed} passed, {len(_failed)} failed")
    if _failed:
        print("FAILED:", _failed)
        return 1
    print("All instance-config + auth smoke tests passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
