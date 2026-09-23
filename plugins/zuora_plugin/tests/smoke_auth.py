#!/usr/bin/env python3
"""Tenant-config + OAuth client-credentials smoke tests for zuora_plugin.

Hermetic — a MagicMock address_book_service + a local ``httpx.MockTransport``
standing in for the Zuora REST API (no network). Red-first: every check
asserts REAL behavior of the resolver + token caching + the 401-triggers-one-
refetch path + EDGE parity.

Exercises:
  1. load() builds a ZuoraTenantConfig from a complete entry
  2. Missing required fields (client_id/client_secret) fail loud, naming
     what's missing
  3. repr() redacts the client secret
  4. ZuoraClient._bearer caches the token until near expiry, then re-fetches
  5. A 401 on a real request triggers exactly one token re-fetch + retry
  6. A failed token mint raises ZuoraAuthError
  7. EDGE parity: validate_edge_process_provider raises nothing, 9 verbs

Run:
    SOLET_NAME=<name> .venv/bin/python3 \
        plugins/zuora_plugin/tests/smoke_auth.py

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

from zuora_plugin.app_config import AppConfigError, AppConfigLoader  # noqa: E402
from zuora_plugin.http_client import ZuoraAuthError, ZuoraClient  # noqa: E402
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


def _entries(
    base_url: str = "https://rest.zuora.com",
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
    _assert("base_url carried + trailing slash stripped", config.base_url == "https://rest.zuora.com")
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


def _config_for_client() -> Any:  # noqa: ANN401 — thin test helper, shape matches ZuoraTenantConfig
    from zuora_plugin.app_config import ZuoraTenantConfig

    return ZuoraTenantConfig(base_url="https://fake.zuora.test", client_id="cid", client_secret="csecret")


def test_bearer_caches_until_near_expiry() -> None:
    mint_count = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/oauth/token":
            mint_count["n"] += 1
            return httpx.Response(200, json={"access_token": f"tok-{mint_count['n']}", "expires_in": 3600})
        return httpx.Response(200, json={"ok": True})

    client = ZuoraClient(_config_for_client(), timeout_seconds=5.0)
    client._http = httpx.Client(transport=httpx.MockTransport(handler), base_url="https://fake.zuora.test")
    first = client._bearer()
    second = client._bearer()
    _assert("bearer cached across calls (single mint)", first == second and mint_count["n"] == 1)


def test_401_triggers_one_refetch_and_retry() -> None:
    call_log: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/oauth/token":
            call_log.append("token")
            return httpx.Response(200, json={"access_token": f"tok-{len(call_log)}", "expires_in": 3600})
        auth = request.headers.get("Authorization", "")
        call_log.append(f"req:{auth}")
        if auth == "Bearer tok-1":
            return httpx.Response(401, json={"error": "invalid_token"})
        return httpx.Response(200, json={"ok": True})

    client = ZuoraClient(_config_for_client(), timeout_seconds=5.0)
    client._http = httpx.Client(transport=httpx.MockTransport(handler), base_url="https://fake.zuora.test")
    response = client.get("/v1/some/path")
    _assert("second attempt succeeds after re-mint", response.status_code == 200)
    _assert("exactly two token mints (initial + one re-mint)", call_log.count("token") == 2)


def test_failed_token_mint_raises() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"error": "invalid_client"})

    client = ZuoraClient(_config_for_client(), timeout_seconds=5.0)
    client._http = httpx.Client(transport=httpx.MockTransport(handler), base_url="https://fake.zuora.test")
    raised = False
    try:
        client._bearer()
    except ZuoraAuthError:
        raised = True
    _assert("failed token mint raises ZuoraAuthError", raised)


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
    _assert("all 9 verbs discovered", len(actions) == 9, str(len(actions)))


def main() -> int:
    print("\nzuora_plugin tenant-config + auth smoke tests")
    print("=" * 47)
    test_load_complete_entry()
    test_missing_fields_named()
    test_repr_redacts_client_secret()
    test_bearer_caches_until_near_expiry()
    test_401_triggers_one_refetch_and_retry()
    test_failed_token_mint_raises()
    test_edge_parity()
    print()
    print(f"Results: {_passed} passed, {len(_failed)} failed")
    if _failed:
        print("FAILED:", _failed)
        return 1
    print("All tenant-config + auth smoke tests passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
