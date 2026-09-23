#!/usr/bin/env python3
"""OAuth token-use recording smoke (no pytest; standalone fixtures).

Run with:

    .venv/bin/python3 \
      plugins/agent_messaging_plugin/tests/oauth_token_use_recording_smoke.py

Covers the defect where ``last_used_at`` was documented on
``OauthClientRecord`` and projected by ``list_clients`` but was never
written by anything, and on the local Postgres backend had no column
to be written to. The registry therefore could not answer "when did
this credential last mint a token", and an operator auditing a client
had only the operator-typed ``client_name`` -- an assertion about what
a client claims to be, with nothing observed at use to compare it to.

Cases:
  1. client_credentials success records all four fields
  2. authorization_code success records transport=authorization_code
  3. refresh_token success records transport=refresh_token
  4. FAILED token request records NOTHING (recording is bound to
     successful issuance, not to the attempt)
  5. a store whose record method RAISES still returns a 200 token
     (bookkeeping must never fail an authenticated issuance)
  6. registry writes all four fields and truncates the User-Agent
  7. registry returns False for an unknown client and does not raise
  8. list_clients projects the three evidence fields

Each case prints PASS/FAIL and the script exits non-zero on any
failure. No external network, no real DB.
"""

from __future__ import annotations

# ruff: noqa: E402
import base64
import hashlib
import logging
import sys
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

_REPO_ROOT = Path(__file__).resolve().parents[3]
for _src in (
    _REPO_ROOT / "ananta" / "src",
    _REPO_ROOT / "plugins" / "agent_messaging_plugin" / "src",
):
    if str(_src) not in sys.path:
        sys.path.insert(0, str(_src))

from ananta.services.store.errors import StoreError
from ananta.vault_core.oauth_registry import VaultOAuthRegistry
from ananta.vault_core.records import MAX_LAST_USE_USER_AGENT_LEN
from fastapi import FastAPI
from fastapi.testclient import TestClient

from agent_messaging_plugin.mcp_streamable.oauth import (
    build_endpoints,
    build_oauth_router,
)

_TEST_HMAC_KEY = b"k" * 32
_UA = "ClaudeMobile/2.1 (iPhone; iOS 18.2)"


class _FakeClientStore:
    """In-memory OAuthClientStore; records what was observed at use."""

    def __init__(self) -> None:
        self.records: dict[str, dict[str, Any]] = {}
        self.use_calls: list[dict[str, str]] = []
        self.raise_on_record: BaseException | None = None

    def lookup_oauth_client(self, client_id: str) -> dict[str, Any] | None:
        rec = self.records.get(client_id)
        if rec is None:
            return None
        return {k: v for k, v in rec.items() if k != "secret"}

    def verify_oauth_client_credentials(
        self, client_id: str, client_secret: str,
    ) -> dict[str, Any] | None:
        rec = self.records.get(client_id)
        if rec is None or rec.get("secret") != client_secret:
            return None
        return {k: v for k, v in rec.items() if k != "secret"}

    def record_oauth_client_token_use(
        self,
        client_id: str,
        *,
        client_ip: str = "",
        user_agent: str = "",
        transport: str = "",
    ) -> bool:
        if self.raise_on_record is not None:
            raise self.raise_on_record
        self.use_calls.append(
            {
                "client_id": client_id,
                "client_ip": client_ip,
                "user_agent": user_agent,
                "transport": transport,
            },
        )
        return client_id in self.records


class _FakeRefreshStore:
    def __init__(self) -> None:
        self.tokens: dict[str, dict[str, Any]] = {}
        self._n = 0

    def issue_oauth_refresh_token(
        self, *, client_id: str, scopes: list[str], audience: str,
        ttl_seconds: int,
    ) -> str:
        self._n += 1
        token = f"refresh-{client_id}-{self._n}"
        self.tokens[token] = {
            "client_id": client_id, "scopes": scopes, "audience": audience,
        }
        return token

    def consume_oauth_refresh_token(
        self, cleartext: str,
    ) -> dict[str, Any] | None:
        return self.tokens.pop(cleartext, None)


class _FakeStorage:
    """In-memory OAuthClientStorage for the registry-level cases."""

    def __init__(self) -> None:
        self.rows: dict[str, dict[str, Any]] = {}

    def get_client(self, client_id: str) -> dict[str, Any] | None:
        return self.rows.get(client_id)

    def insert_client(self, record: dict[str, Any]) -> None:
        self.rows[record["client_id"]] = dict(record)

    def delete_client(self, client_id: str) -> int:
        return 1 if self.rows.pop(client_id, None) is not None else 0

    def list_clients(self) -> list[dict[str, Any]]:
        return list(self.rows.values())

    def update_client_redirect_uris(
        self, client_id: str, redirect_uris: list[str],
    ) -> bool:
        row = self.rows.get(client_id)
        if row is None:
            return False
        row["redirect_uris"] = redirect_uris
        return True

    def record_client_token_use(
        self, client_id: str, fields: Any,
    ) -> bool:
        row = self.rows.get(client_id)
        if row is None:
            return False
        row.update(dict(fields))
        return True


def _approved(grant_types: list[str]) -> dict[str, Any]:
    return {
        "client_id": "client-approved",
        "client_name": "VendorChat MCP connector QTOb4VcHdCsW",
        "scopes": ["mcp:read", "mcp:write"],
        "redirect_uris": ["https://test.example.com/cb"],
        "operator_approved": True,
        "grant_types": grant_types,
        "secret": "shh",
    }


def _build_app(
    store: _FakeClientStore,
    refresh_store: _FakeRefreshStore | None = None,
) -> FastAPI:
    endpoints = build_endpoints(
        issuer="https://test.example.com",
        streamable_path="/mcp/streamable",
    )
    router = build_oauth_router(
        endpoints=endpoints,
        client_store=store,
        refresh_token_store=refresh_store,
        hmac_key=_TEST_HMAC_KEY,
    )
    app = FastAPI()
    app.include_router(router)
    return app


def _pkce() -> tuple[str, str]:
    verifier = "v" * 50
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return verifier, base64.urlsafe_b64encode(digest).decode().rstrip("=")


def _auth_code(client: TestClient, record: dict[str, Any], challenge: str) -> str:
    resp = client.get(
        "/authorize",
        params={
            "response_type": "code",
            "client_id": record["client_id"],
            "redirect_uri": "https://test.example.com/cb",
            "code_challenge": challenge,
            "code_challenge_method": "S256",
        },
        follow_redirects=False,
    )
    assert resp.status_code == 302, resp.text
    return parse_qs(urlparse(resp.headers["location"]).query)["code"][0]


# ─── HTTP-surface cases ────────────────────────────────────────────────────


def case_1_client_credentials_records_all_fields() -> None:
    store = _FakeClientStore()
    rec = _approved(["client_credentials"])
    store.records[rec["client_id"]] = rec
    client = TestClient(_build_app(store))
    r = client.post(
        "/oauth/token",
        data={
            "grant_type": "client_credentials",
            "client_id": rec["client_id"],
            "client_secret": "shh",
        },
        headers={"User-Agent": _UA},
    )
    assert r.status_code == 200, f"{r.status_code}: {r.text}"
    assert len(store.use_calls) == 1, (
        f"expected exactly one token-use record, got {store.use_calls}"
    )
    call = store.use_calls[0]
    assert call["client_id"] == rec["client_id"], call
    assert call["transport"] == "client_credentials", call
    assert call["user_agent"] == _UA, call
    assert call["client_ip"], f"peer IP not observed: {call}"


def case_2_authorization_code_records_transport() -> None:
    store = _FakeClientStore()
    rs = _FakeRefreshStore()
    rec = _approved(["authorization_code", "refresh_token"])
    store.records[rec["client_id"]] = rec
    client = TestClient(_build_app(store, rs))
    verifier, challenge = _pkce()
    code = _auth_code(client, rec, challenge)
    r = client.post(
        "/oauth/token",
        data={
            "grant_type": "authorization_code",
            "code": code,
            "client_id": rec["client_id"],
            "redirect_uri": "https://test.example.com/cb",
            "code_verifier": verifier,
        },
        headers={"User-Agent": _UA},
    )
    assert r.status_code == 200, f"{r.status_code}: {r.text}"
    assert len(store.use_calls) == 1, store.use_calls
    assert store.use_calls[0]["transport"] == "authorization_code", (
        store.use_calls
    )


def case_3_refresh_records_transport() -> None:
    store = _FakeClientStore()
    rs = _FakeRefreshStore()
    rec = _approved(["authorization_code", "refresh_token"])
    store.records[rec["client_id"]] = rec
    client = TestClient(_build_app(store, rs))
    verifier, challenge = _pkce()
    code = _auth_code(client, rec, challenge)
    first = client.post(
        "/oauth/token",
        data={
            "grant_type": "authorization_code",
            "code": code,
            "client_id": rec["client_id"],
            "redirect_uri": "https://test.example.com/cb",
            "code_verifier": verifier,
        },
    )
    assert first.status_code == 200, first.text
    refresh_token = first.json()["refresh_token"]
    store.use_calls.clear()
    r = client.post(
        "/oauth/token",
        data={
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "client_id": rec["client_id"],
        },
        headers={"User-Agent": _UA},
    )
    assert r.status_code == 200, f"{r.status_code}: {r.text}"
    assert len(store.use_calls) == 1, store.use_calls
    assert store.use_calls[0]["transport"] == "refresh_token", store.use_calls


def case_4_failed_request_records_nothing() -> None:
    """The discriminator: recording is bound to issuance, not attempts."""
    store = _FakeClientStore()
    rec = _approved(["client_credentials"])
    store.records[rec["client_id"]] = rec
    client = TestClient(_build_app(store))
    r = client.post(
        "/oauth/token",
        data={
            "grant_type": "client_credentials",
            "client_id": rec["client_id"],
            "client_secret": "WRONG",
        },
        headers={"User-Agent": _UA},
    )
    assert r.status_code == 401, f"{r.status_code}: {r.text}"
    assert store.use_calls == [], (
        f"a REJECTED token request recorded a use: {store.use_calls}"
    )


def case_5_recording_failure_does_not_break_issuance() -> None:
    store = _FakeClientStore()
    rec = _approved(["client_credentials"])
    store.records[rec["client_id"]] = rec
    store.raise_on_record = StoreError("backend unreachable")
    client = TestClient(_build_app(store))
    r = client.post(
        "/oauth/token",
        data={
            "grant_type": "client_credentials",
            "client_id": rec["client_id"],
            "client_secret": "shh",
        },
    )
    assert r.status_code == 200, (
        f"a raising bookkeeping write broke a valid issuance: "
        f"{r.status_code}: {r.text}"
    )
    assert r.json().get("access_token"), r.json()


# ─── Registry-level cases ──────────────────────────────────────────────────


def _registry(storage: _FakeStorage) -> VaultOAuthRegistry:
    return VaultOAuthRegistry(
        client_storage=storage,
        refresh_store=None,
        b64_encode=lambda b: base64.b64encode(b).decode("ascii"),
        b64_decode=lambda s: base64.b64decode(s.encode("ascii")),
        logger=logging.getLogger("oauth_token_use_recording_smoke"),
    )


def case_6_registry_writes_fields_and_truncates_user_agent() -> None:
    storage = _FakeStorage()
    storage.rows["c1"] = {"client_id": "c1", "client_name": "n"}
    reg = _registry(storage)
    long_ua = "U" * (MAX_LAST_USE_USER_AGENT_LEN + 500)
    assert reg.record_token_use(
        "c1", client_ip="10.0.0.9", user_agent=long_ua,
        transport="client_credentials",
    ) is True
    row = storage.rows["c1"]
    assert row["last_used_at"], f"last_used_at not written: {row}"
    assert row["last_use_ip"] == "10.0.0.9", row
    assert row["last_use_transport"] == "client_credentials", row
    assert len(row["last_use_user_agent"]) == MAX_LAST_USE_USER_AGENT_LEN, (
        f"User-Agent not truncated: {len(row['last_use_user_agent'])}"
    )


def case_7_registry_unknown_client_returns_false_without_raising() -> None:
    reg = _registry(_FakeStorage())
    assert reg.record_token_use("nope", transport="refresh_token") is False


def case_8_list_clients_projects_evidence_fields() -> None:
    storage = _FakeStorage()
    storage.rows["c1"] = {
        "client_id": "c1",
        "client_name": "n",
        "created_at": "2026-07-04T17:02:59",
    }
    reg = _registry(storage)
    reg.record_token_use("c1", client_ip="127.0.0.1", user_agent="UA",
                         transport="authorization_code")
    projected = reg.list_clients()[0]
    for field in (
        "last_used_at", "last_use_ip", "last_use_user_agent",
        "last_use_transport",
    ):
        assert field in projected, f"{field} missing from projection"
    assert projected["last_use_transport"] == "authorization_code", projected


def case_9_programming_error_is_not_swallowed() -> None:
    """The no-silent-fallback discriminator.

    A storage outage is caught (case 5). A TypeError here is a bug in
    this module, not an outage, and must NOT be hidden behind a log
    line -- so it propagates rather than being swallowed.
    """
    store = _FakeClientStore()
    rec = _approved(["client_credentials"])
    store.records[rec["client_id"]] = rec
    store.raise_on_record = TypeError("programming error, not an outage")
    client = TestClient(_build_app(store), raise_server_exceptions=False)
    r = client.post(
        "/oauth/token",
        data={
            "grant_type": "client_credentials",
            "client_id": rec["client_id"],
            "client_secret": "shh",
        },
    )
    assert r.status_code == 500, (
        "a TypeError in bookkeeping was swallowed instead of surfacing; "
        f"got {r.status_code}"
    )


CASES = [
    ("client_credentials records all fields", case_1_client_credentials_records_all_fields),
    ("authorization_code records transport", case_2_authorization_code_records_transport),
    ("refresh_token records transport", case_3_refresh_records_transport),
    ("failed request records nothing", case_4_failed_request_records_nothing),
    ("recording failure does not break issuance", case_5_recording_failure_does_not_break_issuance),
    ("registry writes fields + truncates UA",
     case_6_registry_writes_fields_and_truncates_user_agent),
    ("registry unknown client returns False",
     case_7_registry_unknown_client_returns_false_without_raising),
    ("list_clients projects evidence fields", case_8_list_clients_projects_evidence_fields),
    ("programming error is NOT swallowed", case_9_programming_error_is_not_swallowed),
]


def main() -> int:
    passed = 0
    failed: list[tuple[str, BaseException]] = []
    for name, fn in CASES:
        try:
            fn()
        except BaseException as exc:  # noqa: BLE001
            failed.append((name, exc))
            print(f"  FAIL  {name}: {exc!r}")
            continue
        passed += 1
        print(f"  pass  {name}")
    print(f"\noauth_token_use_recording_smoke: {passed}/{len(CASES)} passed")
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())
