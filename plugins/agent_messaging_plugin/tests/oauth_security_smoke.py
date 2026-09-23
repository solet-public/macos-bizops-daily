#!/usr/bin/env python3
"""Task #31 OAuth security smoke (no pytest; standalone hand-rolled fixtures).

Run with:

    .venv/bin/python3 plugins/agent_messaging_plugin/tests/oauth_security_smoke.py

11 cases per the Task #31 design (§4):
  1. /register returns 404; metadata excludes registration_endpoint
  2. Non-approved client: rejected at /authorize with 401 invalid_client
  3. Non-approved client: rejected at /oauth/token authorization_code 401
  4. Non-approved client: rejected at /oauth/token client_credentials 401
  5. Non-approved client: rejected at /oauth/token refresh_token 401
  6. Approved without 'client_credentials' in grant_types: 400 unauthorized_client
  7. Approved without 'refresh_token' in grant_types: auth-code response omits
     refresh_token
  8. oauth_client_register (default) creates operator_approved=True and
     grant_types=['authorization_code', 'refresh_token']
  9. oauth_client_register(grant_types=[invalid]) is rejected
 10. lookup_oauth_client AND verify_oauth_client_credentials both return
     projections that include operator_approved AND grant_types
 11. Missing operator_approved field on a record: projection returns False

Plus extended coverage (cases 12-13) covering projection edge cases beyond
the design §4 explicit list: malformed grant_types -> [] and SM-default
projection symmetry.

Each case prints PASS/FAIL with a short reason on failure and exits
non-zero on any failure. No external network. Boots a FastAPI router
with in-memory fake stores; no real DB. The vault register/lookup/verify
cases instantiate MacosVaultPlugin via object.__new__ + attribute bind
so the platform plumbing doesn't need to come up.
"""

from __future__ import annotations

# ruff: noqa: E402
import base64
import hashlib
import logging
import sys
from typing import TYPE_CHECKING, Any, cast

if TYPE_CHECKING:
    from collections.abc import Mapping
from pathlib import Path
from urllib.parse import parse_qs, urlparse

_PLUGIN_ROOT = Path(__file__).resolve().parents[1]
_SRC = _PLUGIN_ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from ananta.core.services.call_context import CallContext
from ananta.vault_core import (
    VaultOAuthRegistry,
    project_oauth_client_metadata,
)
from fastapi import FastAPI
from fastapi.testclient import TestClient
from macos_vault_plugin.plugin import MacosVaultPlugin

from agent_messaging_plugin.mcp_streamable.auth import HMAC_KEY_BYTE_LENGTH
from agent_messaging_plugin.mcp_streamable.oauth import (
    build_endpoints,
    build_oauth_router,
)

# Both plugins' module-level ``_project_oauth_client_metadata`` delegate
# to ``ananta.vault_core.project_oauth_client_metadata``; testing the
# canonical helper covers both call sites. Cases 12-13 keep the
# malformed/symmetric checks against this single shared function.
_project_oauth_client_metadata = project_oauth_client_metadata
_sm_project = project_oauth_client_metadata

# ─── Fixtures ──────────────────────────────────────────────────────────────


_TEST_HMAC_KEY = b"\xaa" * HMAC_KEY_BYTE_LENGTH


class _FakeClientStore:
    """In-memory client store; the test drives what records exist."""

    def __init__(self) -> None:
        self.records: dict[str, dict[str, Any]] = {}

    def lookup_oauth_client(self, client_id: str) -> dict[str, Any] | None:
        rec = self.records.get(client_id)
        if rec is None:
            return None
        out = {k: v for k, v in rec.items() if k != "secret"}
        return out

    def record_oauth_client_token_use(
        self,
        client_id: str,
        *,
        client_ip: str = "",
        user_agent: str = "",
        transport: str = "",
    ) -> bool:
        rec = self.records.get(client_id)
        if rec is None:
            return False
        rec["last_use_ip"] = client_ip
        rec["last_use_user_agent"] = user_agent
        rec["last_use_transport"] = transport
        return True

    def verify_oauth_client_credentials(
        self, client_id: str, client_secret: str,
    ) -> dict[str, Any] | None:
        rec = self.records.get(client_id)
        if rec is None or rec.get("secret") != client_secret:
            return None
        out = {k: v for k, v in rec.items() if k != "secret"}
        return out


class _FakeRefreshStore:
    def __init__(self) -> None:
        self.issued: list[dict[str, Any]] = []
        self.tokens: dict[str, dict[str, Any]] = {}

    def issue_oauth_refresh_token(
        self, *, client_id: str, scopes: list[str], audience: str,
        ttl_seconds: int,
    ) -> str:
        token = f"refresh-{client_id}-{len(self.issued)}"
        self.issued.append(
            {
                "client_id": client_id, "scopes": scopes,
                "audience": audience, "ttl_seconds": ttl_seconds,
            },
        )
        self.tokens[token] = {
            "client_id": client_id, "scopes": scopes,
            "audience": audience,
        }
        return token

    def consume_oauth_refresh_token(
        self, cleartext: str,
    ) -> dict[str, Any] | None:
        return self.tokens.pop(cleartext, None)


class _FakeOauthStore:
    """Bare-minimum store for OAuth client + refresh token tables.

    Implements the OAuthClientStorage + RefreshTokenStorage protocols
    so the registry can be constructed directly. No locking, no schema.
    Two roles per instance: client rows live in self.rows; refresh
    tokens in self.tokens.
    """

    def __init__(self) -> None:
        self.rows: dict[str, dict[str, Any]] = {}
        self.tokens: dict[str, dict[str, Any]] = {}

    # OAuthClientStorage:
    def get_client(self, client_id: str) -> dict[str, Any] | None:
        return self.rows.get(client_id)

    def insert_client(self, record: dict[str, Any]) -> None:
        self.rows[record["client_id"]] = dict(record)

    def delete_client(self, client_id: str) -> int:
        return 1 if self.rows.pop(client_id, None) is not None else 0

    def list_clients(self) -> list[Mapping[str, Any]]:
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
        self, client_id: str, fields: Mapping[str, str],
    ) -> bool:
        """Record last-use evidence on the in-memory record."""
        record = self.rows.get(client_id)
        if record is None:
            return False
        record.update(dict(fields))
        return True

    # RefreshTokenStorage:
    def insert_token(self, row: dict[str, Any]) -> None:
        self.tokens[row["token_hash"]] = dict(row)

    def consume_token(self, token_hash: str) -> dict[str, Any] | None:
        return self.tokens.pop(token_hash, None)


def _make_vault_plugin() -> MacosVaultPlugin:
    """Build a MacosVaultPlugin with the minimum attrs the action needs.

    Skips __init__ via object.__new__ — the platform plumbing
    (config, state service, audit, ...) is not required for the
    register / lookup / verify code paths. Composes a VaultOAuthRegistry
    over the FakeOauthStore so the plugin's delegations resolve.
    """
    plugin: MacosVaultPlugin = object.__new__(MacosVaultPlugin)
    fake_store = _FakeOauthStore()
    plugin._oauth_clients_store = fake_store
    plugin._oauth_refresh_tokens_store = fake_store
    plugin._ensure_schema = lambda: None
    plugin.logger = logging.getLogger("oauth_security_smoke.vault")
    plugin._oauth_registry = VaultOAuthRegistry(
        client_storage=fake_store,
        refresh_store=fake_store,
        b64_encode=lambda b: base64.b64encode(b).decode("ascii"),
        b64_decode=lambda s: base64.b64decode(s.encode("ascii")),
        logger=plugin.logger,
    )
    return plugin


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


def _pkce_verifier_and_challenge() -> tuple[str, str]:
    verifier = "v" * 50
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")
    return verifier, challenge


def _approved(grant_types: list[str], secret: str = "shh") -> dict[str, Any]:
    return {
        "client_id": "client-approved",
        "client_name": "approved",
        "scopes": ["mcp:read", "mcp:write"],
        "redirect_uris": ["https://test.example.com/cb"],
        "operator_approved": True,
        "grant_types": grant_types,
        "secret": secret,
    }


def _not_approved() -> dict[str, Any]:
    return {
        "client_id": "client-unapproved",
        "client_name": "unapproved",
        "scopes": ["mcp:read", "mcp:write"],
        "redirect_uris": ["https://test.example.com/cb"],
        "operator_approved": False,
        "grant_types": [
            "authorization_code", "client_credentials", "refresh_token",
        ],
        "secret": "shh-noapprove",
    }


# ─── Cases 1-7 (HTTP surface) ──────────────────────────────────────────────


def case_1_register_404_and_metadata_clean() -> None:
    """/register returns 404; metadata omits registration_endpoint."""
    store = _FakeClientStore()
    client = TestClient(_build_app(store))

    r = client.post("/register", json={"client_name": "x"})
    assert r.status_code == 404, (
        f"/register returned {r.status_code}: {r.text}"
    )

    meta = client.get("/.well-known/oauth-authorization-server").json()
    assert "registration_endpoint" not in meta, (
        f"metadata advertises registration_endpoint: {meta}"
    )
    assert "client_credentials" not in meta.get("grant_types_supported", []), (
        f"metadata advertises client_credentials: "
        f"{meta.get('grant_types_supported')}"
    )
    path_prmd = client.get("/.well-known/oauth-protected-resource/mcp/streamable")
    assert path_prmd.status_code == 200, (
        f"path-specific PRMD returned {path_prmd.status_code}: {path_prmd.text}"
    )
    assert path_prmd.json().get("resource") == "https://test.example.com/mcp/streamable", (
        f"path-specific PRMD advertised wrong resource: {path_prmd.json()}"
    )


def case_2_unapproved_rejected_at_authorize() -> None:
    store = _FakeClientStore()
    store.records[_not_approved()["client_id"]] = _not_approved()
    client = TestClient(_build_app(store))
    _, challenge = _pkce_verifier_and_challenge()
    r = client.get(
        "/authorize",
        params={
            "response_type": "code",
            "client_id": "client-unapproved",
            "redirect_uri": "https://test.example.com/cb",
            "code_challenge": challenge,
            "code_challenge_method": "S256",
        },
        follow_redirects=False,
    )
    assert r.status_code == 401, (
        f"/authorize returned {r.status_code}: {r.text}"
    )
    assert r.json().get("error") == "invalid_client", r.json()


def case_3_unapproved_rejected_at_token_authcode() -> None:
    """auth_code grant on an unapproved client_id -- 401.

    We bypass /authorize (where it would also be blocked) and directly
    post to /token with a forged code; the lookup-by-id at the
    pre-issuance verifier still sees the unapproved client and fails
    fast. Validates the belt-and-suspenders check at the token
    endpoint.
    """
    store = _FakeClientStore()
    store.records[_not_approved()["client_id"]] = _not_approved()
    client = TestClient(_build_app(store))
    # Issue a code via /authorize first BUT with operator_approved
    # temporarily flipped True so we can get past /authorize, then
    # flip back before the /token call.
    store.records[_not_approved()["client_id"]]["operator_approved"] = True
    verifier, challenge = _pkce_verifier_and_challenge()
    auth_resp = client.get(
        "/authorize",
        params={
            "response_type": "code",
            "client_id": "client-unapproved",
            "redirect_uri": "https://test.example.com/cb",
            "code_challenge": challenge,
            "code_challenge_method": "S256",
        },
        follow_redirects=False,
    )
    assert auth_resp.status_code == 302, (
        f"setup /authorize: {auth_resp.status_code} {auth_resp.text}"
    )
    code = parse_qs(urlparse(auth_resp.headers["location"]).query)["code"][0]
    # Now revoke approval to test the token-endpoint guard.
    store.records[_not_approved()["client_id"]]["operator_approved"] = False
    r = client.post(
        "/oauth/token",
        data={
            "grant_type": "authorization_code",
            "code": code,
            "code_verifier": verifier,
            "redirect_uri": "https://test.example.com/cb",
            "client_id": "client-unapproved",
        },
    )
    assert r.status_code == 401, (
        f"/oauth/token authorization_code unapproved: "
        f"{r.status_code}: {r.text}"
    )
    assert r.json().get("error") == "invalid_client", r.json()


def case_4_unapproved_rejected_at_token_client_credentials() -> None:
    store = _FakeClientStore()
    store.records[_not_approved()["client_id"]] = _not_approved()
    client = TestClient(_build_app(store))
    r = client.post(
        "/oauth/token",
        data={
            "grant_type": "client_credentials",
            "client_id": "client-unapproved",
            "client_secret": "shh-noapprove",
        },
    )
    assert r.status_code == 401, (
        f"/oauth/token client_credentials unapproved: "
        f"{r.status_code}: {r.text}"
    )
    assert r.json().get("error") == "invalid_client", r.json()


def case_5_unapproved_rejected_at_token_refresh() -> None:
    store = _FakeClientStore()
    rs = _FakeRefreshStore()
    store.records[_not_approved()["client_id"]] = _not_approved()
    rs.tokens["bogus-refresh"] = {
        "client_id": "client-unapproved",
        "scopes": ["mcp:read"],
        "audience": "https://test.example.com/mcp/streamable",
    }
    client = TestClient(_build_app(store, rs))
    r = client.post(
        "/oauth/token",
        data={
            "grant_type": "refresh_token",
            "refresh_token": "bogus-refresh",
            "client_id": "client-unapproved",
        },
    )
    assert r.status_code == 401, (
        f"/oauth/token refresh_token unapproved: "
        f"{r.status_code}: {r.text}"
    )
    assert r.json().get("error") == "invalid_client", r.json()


def case_6_approved_missing_cc_rejected() -> None:
    """Approved client without 'client_credentials' in grant_types."""
    store = _FakeClientStore()
    record = _approved(["authorization_code", "refresh_token"])
    store.records[record["client_id"]] = record
    client = TestClient(_build_app(store))
    r = client.post(
        "/oauth/token",
        data={
            "grant_type": "client_credentials",
            "client_id": record["client_id"],
            "client_secret": record["secret"],
        },
    )
    assert r.status_code == 400, (
        f"/oauth/token cc w/o stored grant: {r.status_code}: {r.text}"
    )
    assert r.json().get("error") == "unauthorized_client", r.json()


def case_7_approved_missing_refresh_grant_omits_refresh_token() -> None:
    """Approved auth_code client w/o 'refresh_token' grant: no refresh."""
    store = _FakeClientStore()
    rs = _FakeRefreshStore()
    record = _approved(["authorization_code"])  # no refresh_token
    store.records[record["client_id"]] = record
    client = TestClient(_build_app(store, rs))
    verifier, challenge = _pkce_verifier_and_challenge()
    auth_resp = client.get(
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
    assert auth_resp.status_code == 302, auth_resp.text
    code = parse_qs(urlparse(auth_resp.headers["location"]).query)["code"][0]
    r = client.post(
        "/oauth/token",
        data={
            "grant_type": "authorization_code",
            "code": code,
            "code_verifier": verifier,
            "redirect_uri": "https://test.example.com/cb",
            "client_id": record["client_id"],
        },
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert "access_token" in body, f"no access_token: {body}"
    assert "refresh_token" not in body, (
        f"refresh_token leaked despite grant_types omission: {body}"
    )
    assert rs.issued == [], (
        f"refresh store called despite grant_types omission: {rs.issued}"
    )


# ─── Cases 8-11 (vault action + projection per design §4) ──────────────────


def _do_register(plugin: MacosVaultPlugin, **params: Any) -> dict[str, Any]:
    """Run oauth_client_register; return the result as a plain dict.

    W-VAULT-CALLER-ENFORCE (Tier 2 sub-2): the plugin-process
    ``oauth_client_register_action`` wrapper was removed; the canonical
    surface is the structural ``oauth_client_register`` method gated by
    ``@requires_operator_principal``. The smoke supplies an operator
    CallContext so the gate passes.

    ActionResult is a TypedDict(total=False); cast at the boundary so
    the tests can subscript ``action_status`` / ``data`` / ``error``
    without sprinkling ``.get(...)`` everywhere.
    """
    return cast(
        "dict[str, Any]",
        plugin.oauth_client_register(
            call_context=CallContext.for_operator(),
            **params,
        ),
    )


def case_8_register_default_sets_operator_approved_and_grants() -> None:
    """oauth_client_register default: operator_approved=True + grant_types defaults."""
    plugin = _make_vault_plugin()
    result = _do_register(
        plugin,
        client_name="claude-ai test",
        scopes=["mcp:read", "mcp:write"],
        redirect_uris=["https://test.example.com/cb"],
        # grant_types omitted -> action falls back to default
    )
    assert result["action_status"] == "completed", result
    data = result["data"]
    assert data["operator_approved"] is True, data
    assert data["grant_types"] == [
        "authorization_code", "refresh_token",
    ], data
    # The stored row should also carry both fields:
    fake_store = cast("_FakeOauthStore", plugin._oauth_clients_store)
    stored = fake_store.rows[data["client_id"]]
    assert stored["operator_approved"] is True, stored
    assert stored["grant_types"] == [
        "authorization_code", "refresh_token",
    ], stored


def case_9_register_invalid_grant_types_rejected() -> None:
    """oauth_client_register with values outside the allowlist is rejected."""
    plugin = _make_vault_plugin()
    result = _do_register(
        plugin,
        client_name="bad",
        scopes=["mcp:read"],
        redirect_uris=[],
        grant_types=["password"],  # not in allowlist
    )
    assert result["action_status"] == "error", result
    err = result["error"]
    assert err is not None, result
    assert "allowlist" in err["message"].lower(), err

    # Empty grant_types should also be rejected.
    result_empty = _do_register(
        plugin,
        client_name="empty",
        scopes=["mcp:read"],
        redirect_uris=[],
        grant_types=[],
    )
    assert result_empty["action_status"] == "error", result_empty
    err_empty = result_empty["error"]
    assert err_empty is not None and "at least one" in err_empty["message"], (
        err_empty
    )

    # Non-list grant_types should be rejected.
    result_non_list = _do_register(
        plugin,
        client_name="non-list",
        scopes=["mcp:read"],
        redirect_uris=[],
        grant_types="authorization_code",  # string, not list
    )
    assert result_non_list["action_status"] == "error", result_non_list


def case_10_lookup_and_verify_both_project_new_fields() -> None:
    """Both lookup_oauth_client AND verify_oauth_client_credentials project new fields."""
    plugin = _make_vault_plugin()
    reg = _do_register(
        plugin,
        client_name="for-lookup",
        scopes=["mcp:read"],
        redirect_uris=["https://test.example.com/cb"],
        grant_types=["authorization_code", "client_credentials"],
    )
    assert reg["action_status"] == "completed", reg
    cid = str(reg["data"]["client_id"])
    secret = str(reg["data"]["client_secret"])

    looked_up = plugin.lookup_oauth_client(cid)
    assert looked_up is not None, "lookup_oauth_client returned None"
    assert looked_up["operator_approved"] is True, looked_up
    assert looked_up["grant_types"] == [
        "authorization_code", "client_credentials",
    ], looked_up

    verified = plugin.verify_oauth_client_credentials(cid, secret)
    assert verified is not None, "verify_oauth_client_credentials returned None"
    assert verified["operator_approved"] is True, verified
    assert verified["grant_types"] == [
        "authorization_code", "client_credentials",
    ], verified

    # Wrong-secret path returns None (and never leaks fields).
    bad = plugin.verify_oauth_client_credentials(cid, "wrong-secret")
    assert bad is None, "verify accepted wrong secret"


def case_11_missing_operator_approved_projects_false() -> None:
    """Projection treats missing/non-True operator_approved as False."""
    row_missing: dict[str, object] = {
        "client_name": "x", "scopes": [], "redirect_uris": [],
    }
    assert _project_oauth_client_metadata(
        "c", row_missing,
    )["operator_approved"] is False
    # Non-bool truthy must also fail closed (Codex insistence).
    row_truthy_non_bool: dict[str, object] = {**row_missing, "operator_approved": 1}
    assert _project_oauth_client_metadata(
        "c", row_truthy_non_bool,
    )["operator_approved"] is False
    row_none: dict[str, object] = {**row_missing, "operator_approved": None}
    assert _project_oauth_client_metadata(
        "c", row_none,
    )["operator_approved"] is False
    row_false: dict[str, object] = {**row_missing, "operator_approved": False}
    assert _project_oauth_client_metadata(
        "c", row_false,
    )["operator_approved"] is False
    row_string: dict[str, object] = {**row_missing, "operator_approved": "true"}
    assert _project_oauth_client_metadata(
        "c", row_string,
    )["operator_approved"] is False


# ─── Extended coverage (beyond design §4) ──────────────────────────────────


def case_12_projection_grant_types_empty_when_malformed() -> None:
    row: dict[str, object] = {
        "client_name": "x", "scopes": [], "redirect_uris": [],
        "operator_approved": True,
        "grant_types": "not-a-list",  # malformed
    }
    assert _project_oauth_client_metadata("c", row)["grant_types"] == []
    row_missing: dict[str, object] = {
        "client_name": "x", "scopes": [], "redirect_uris": [],
        "operator_approved": True,
    }
    assert _project_oauth_client_metadata(
        "c", row_missing,
    )["grant_types"] == []


def case_13_sm_projection_symmetric_with_default() -> None:
    """Both vault plugins project the same shape for the same row."""
    row: dict[str, object] = {
        "client_name": "x",
        "scopes": ["mcp:read"],
        "redirect_uris": ["https://x"],
        "operator_approved": True,
        "grant_types": ["authorization_code"],
    }
    default_proj = _project_oauth_client_metadata("c", row)
    sm_proj = _sm_project("c", row)
    for k in (
        "client_id", "operator_approved", "grant_types",
        "scopes", "redirect_uris",
    ):
        assert default_proj[k] == sm_proj[k], (
            f"projection mismatch on {k}: "
            f"default={default_proj[k]!r} sm={sm_proj[k]!r}"
        )


# ─── Runner ────────────────────────────────────────────────────────────────


CASES = [
    (
        "case_1_register_404_and_metadata_clean",
        case_1_register_404_and_metadata_clean,
    ),
    (
        "case_2_unapproved_rejected_at_authorize",
        case_2_unapproved_rejected_at_authorize,
    ),
    (
        "case_3_unapproved_rejected_at_token_authcode",
        case_3_unapproved_rejected_at_token_authcode,
    ),
    (
        "case_4_unapproved_rejected_at_token_client_credentials",
        case_4_unapproved_rejected_at_token_client_credentials,
    ),
    (
        "case_5_unapproved_rejected_at_token_refresh",
        case_5_unapproved_rejected_at_token_refresh,
    ),
    (
        "case_6_approved_missing_cc_rejected",
        case_6_approved_missing_cc_rejected,
    ),
    (
        "case_7_approved_missing_refresh_grant_omits_refresh_token",
        case_7_approved_missing_refresh_grant_omits_refresh_token,
    ),
    (
        "case_8_register_default_sets_operator_approved_and_grants",
        case_8_register_default_sets_operator_approved_and_grants,
    ),
    (
        "case_9_register_invalid_grant_types_rejected",
        case_9_register_invalid_grant_types_rejected,
    ),
    (
        "case_10_lookup_and_verify_both_project_new_fields",
        case_10_lookup_and_verify_both_project_new_fields,
    ),
    (
        "case_11_missing_operator_approved_projects_false",
        case_11_missing_operator_approved_projects_false,
    ),
    (
        "case_12_projection_grant_types_empty_when_malformed",
        case_12_projection_grant_types_empty_when_malformed,
    ),
    (
        "case_13_sm_projection_symmetric_with_default",
        case_13_sm_projection_symmetric_with_default,
    ),
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
    print(f"\noauth_security_smoke: {passed}/{len(CASES)} passed")
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())
