#!/usr/bin/env python3
"""OAuth + token-store smoke tests for g_suite_plugin (no pytest, no live Google).

Exercises the OAuth core against a faked vault and a patched Google refresh —
no network, no credentials. Red-first: each check asserts real behavior, so a
regression in the token store or the Google-library wiring fails here.

Exercises:
  1. build_authorization_request — PKCE S256 consent URL, offline+consent, state
  2. store_initial_tokens — refresh token written before access token
  3. get_access_token — cache hit returns the vaulted token, no refresh
  4. get_access_token — no refresh token + force_refresh -> gsuite.not_connected
  5. refresh path — access token rewritten, refresh token NOT rotated (Google
     durable-refresh semantics: a plain refresh omits a new refresh token)
  6. refresh path — invalid_grant maps to gsuite.auth_expired

Requires the Google client libraries importable (they are pulled by installing
this plugin: `pip install -e plugins/g_suite_plugin`). Run:
    SOLET_NAME=<name> .venv/bin/python3 plugins/g_suite_plugin/tests/smoke_oauth.py

Exits 0 on success, 1 on first failure.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch
from urllib.parse import parse_qs, urlparse

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT / "plugins" / "g_suite_plugin" / "src"))

from g_suite_plugin.constants import (  # noqa: E402
    VAULT_KEY_ACCESS_TOKEN,
    VAULT_KEY_REFRESH_TOKEN,
)
from g_suite_plugin.oauth.app_config import AppConfigLoader, OAuthAppConfig  # noqa: E402
from g_suite_plugin.oauth.oauth_client import (  # noqa: E402
    GoogleTokens,
    build_authorization_request,
)
from g_suite_plugin.oauth.oauth_routes import _html_result, _redact  # noqa: E402
from g_suite_plugin.oauth.token_store import (  # noqa: E402
    TokenStore,
    TokenStoreError,
    _build_access_envelope,
)
from g_suite_plugin.plugin import GSuitePlugin  # noqa: E402

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


_APP = OAuthAppConfig(
    client_id="cid.apps.googleusercontent.com",
    client_secret="secret",
    redirect_uri="https://host.example.com/oauth/google/callback",
)


def _loader() -> AppConfigLoader:
    loader = MagicMock(spec=AppConfigLoader)
    loader.load.return_value = _APP
    return loader


def _vault(exists: dict[str, bool], values: dict[str, str]) -> MagicMock:
    vault = MagicMock()
    vault.exists.side_effect = lambda key: {
        "action_status": "completed",
        "data": {"exists": exists.get(key, False)},
    }
    vault.retrieve.side_effect = lambda key: {
        "action_status": "completed",
        "data": {"value": values.get(key, "")},
    }
    vault.store.return_value = {"action_status": "completed", "data": {}}
    vault.rotate.return_value = {"action_status": "completed", "data": {}}
    return vault


def test_authorization_request_pkce() -> None:
    request = build_authorization_request(_APP)
    parsed = urlparse(request.authorize_url)
    qs = parse_qs(parsed.query)
    _assert("authorize host is accounts.google.com", parsed.netloc == "accounts.google.com")
    _assert("PKCE code_challenge present", "code_challenge" in qs)
    _assert("PKCE method is S256", qs.get("code_challenge_method") == ["S256"])
    _assert("offline access requested", qs.get("access_type") == ["offline"])
    _assert("prompt=consent (forces refresh token)", qs.get("prompt") == ["consent"])
    _assert("state round-trips", qs.get("state") == [request.state])
    _assert("code_verifier populated", len(request.code_verifier) > 0)


def test_store_initial_tokens_order() -> None:
    vault = _vault(exists={}, values={})
    store = TokenStore(vault, _loader())
    store.store_initial_tokens(refresh_token="rt1", access_token="at1", expires_in=3600)
    stores = [c for c in vault.method_calls if c[0] == "store"]
    _assert("refresh stored before access", bool(stores) and stores[0].kwargs["key"] == VAULT_KEY_REFRESH_TOKEN)
    _assert("refresh value correct", stores[0].kwargs["value"] == "rt1")


def test_cache_hit() -> None:
    envelope = _build_access_envelope("cached", 3600)
    vault = _vault(
        exists={VAULT_KEY_REFRESH_TOKEN: True, VAULT_KEY_ACCESS_TOKEN: True},
        values={VAULT_KEY_ACCESS_TOKEN: envelope},
    )
    store = TokenStore(vault, _loader())
    token = store.get_access_token()
    _assert("cache hit returns vaulted token", token == "cached")
    vault.rotate.assert_not_called()
    _assert("no rotation on cache hit", not vault.rotate.called)


def test_not_connected() -> None:
    vault = _vault(exists={}, values={})
    store = TokenStore(vault, _loader())
    raised_code = ""
    try:
        store.get_access_token(force_refresh=True)
    except TokenStoreError as exc:
        raised_code = exc.code
    _assert("force-refresh w/o token -> gsuite.not_connected", raised_code == "gsuite.not_connected")


def test_refresh_does_not_rotate_refresh() -> None:
    stale = _build_access_envelope("old", -100)  # already expired
    vault = _vault(
        exists={VAULT_KEY_REFRESH_TOKEN: True, VAULT_KEY_ACCESS_TOKEN: True},
        values={VAULT_KEY_ACCESS_TOKEN: stale, VAULT_KEY_REFRESH_TOKEN: "rt-durable"},
    )
    store = TokenStore(vault, _loader())
    with patch(
        "g_suite_plugin.oauth.token_store.refresh_access_token",
        return_value=GoogleTokens(access_token="fresh", refresh_token=None, expires_in=3600),
    ):
        token = store.get_access_token()
    _assert("refresh returns new access token", token == "fresh")
    rotated_keys = [c.args[0] for c in vault.rotate.call_args_list]
    _assert("access token rotated", VAULT_KEY_ACCESS_TOKEN in rotated_keys)
    _assert("refresh token NOT rotated (Google durable)", VAULT_KEY_REFRESH_TOKEN not in rotated_keys)


def test_invalid_grant_maps_to_auth_expired() -> None:
    stale = _build_access_envelope("old", -100)
    vault = _vault(
        exists={VAULT_KEY_REFRESH_TOKEN: True, VAULT_KEY_ACCESS_TOKEN: True},
        values={VAULT_KEY_ACCESS_TOKEN: stale, VAULT_KEY_REFRESH_TOKEN: "rt-revoked"},
    )
    store = TokenStore(vault, _loader())
    raised_code = ""
    with patch(
        "g_suite_plugin.oauth.token_store.refresh_access_token",
        side_effect=RuntimeError("('invalid_grant: Token has been expired or revoked.')"),
    ):
        try:
            store.get_access_token()
        except TokenStoreError as exc:
            raised_code = exc.code
    _assert("invalid_grant -> gsuite.auth_expired", raised_code == "gsuite.auth_expired")


def test_store_raises_on_vault_failure() -> None:
    # SECURITY/robustness (Codex review 2026-07-08): a failed vault write must NOT
    # be swallowed into a false "connected" state. store_initial_tokens must raise.
    vault = _vault(exists={}, values={})
    vault.store.return_value = {"action_status": "error", "data": {}}
    store = TokenStore(vault, _loader())
    raised_code = ""
    try:
        store.store_initial_tokens(refresh_token="rt", access_token="at", expires_in=3600)
    except TokenStoreError as exc:
        raised_code = exc.code
    _assert("vault write failure -> token_store_failed", raised_code == "token_store_failed")


def test_callback_html_escapes() -> None:
    # SECURITY (Codex): the externally-reachable callback must not reflect markup.
    out = _html_result("OAuth Error", "<script>alert(1)</script> & bad", success=False)
    _assert("raw <script> not rendered", "<script>" not in out)
    _assert("value is html-escaped", "&lt;script&gt;" in out)


def test_redact_state() -> None:
    _assert("state truncated to short prefix", _redact("abcdefghij") == "prefix=abcdef…")
    _assert("empty state labelled, not echoed", _redact("") == "empty")


def _start_interface_host(config: dict[str, object], params: dict[str, object]) -> str:
    plugin = GSuitePlugin()
    plugin.initialize(config)
    plugin._token_store = MagicMock(spec=TokenStore)
    plugin._app_config_loader = MagicMock(spec=AppConfigLoader)
    with patch.object(plugin._oauth_server, "start", return_value=8765) as start:
        result = plugin.start_interface(params, {})
    _assert(
        "start_interface host probe completed",
        result.get("action_status") == "completed",
        str(result),
    )
    return str(start.call_args.kwargs["host"])


def test_start_interface_host_selection() -> None:
    """Local-safe IPv4 default, explicit cloud bind preserved."""
    _assert(
        "local callback default is explicit IPv4 loopback",
        _start_interface_host({}, {}) == "127.0.0.1",
    )
    _assert(
        "cloud callback bind remains explicitly configurable",
        _start_interface_host({"callback_host": "0.0.0.0"}, {}) == "0.0.0.0",
    )
    _assert(
        "call argument overrides configured callback host",
        _start_interface_host({"callback_host": "0.0.0.0"}, {"host": "127.0.0.1"})
        == "127.0.0.1",
    )


def test_start_refuses_an_occupied_port() -> None:
    # Field-verified on a live deployment: start() used to set _started BEFORE
    # the socket bound and swallow the failure (uvicorn raises SystemExit,
    # which `except Exception` does not catch), so a port collision reported
    # SUCCESS and the operator's
    # browser landed on whatever else owned the port — a bare 404, nothing
    # logged, on a mandatory first-run path. Hold the port ourselves and assert
    # start() now raises instead of returning a port nothing is listening on.
    import socket

    from g_suite_plugin.oauth.http_server import OAuthServer, OAuthServerStartError

    holder = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        holder.bind(("127.0.0.1", 0))
        holder.listen(1)
        held_port = holder.getsockname()[1]

        server = OAuthServer()
        raised: BaseException | None = None
        returned: int | None = None

        # Force the server onto the held port: PortManager.allocate() would
        # otherwise probe past it to a free one. Patched on the INSTANCE, and
        # the stub sets _allocated_port (which the `port` property reads) so
        # nothing on the PortManager CLASS is mutated — a class-level patch
        # would leak to any other smoke sharing this process.
        port_manager = server._port_manager

        def _allocate_the_held_port(preferred_port: int | None = None) -> int:
            port_manager._allocated_port = held_port
            return held_port

        with patch.object(port_manager, "allocate", side_effect=_allocate_the_held_port):
            try:
                returned = server.start(
                    host="127.0.0.1",
                    preferred_port=held_port,
                    token_store=MagicMock(),
                    app_config_loader=MagicMock(),
                    pending_states={},
                )
            except OAuthServerStartError as exc:
                raised = exc
            finally:
                server.stop()

        _assert(
            "occupied port raises OAuthServerStartError, never returns a port",
            raised is not None and returned is None,
            f"start() returned {returned!r} instead of raising",
        )
        _assert(
            "start failure names the host and port",
            raised is not None and f"127.0.0.1:{held_port}" in str(raised),
            f"message was {raised!r}",
        )
    finally:
        holder.close()


def main() -> int:
    print("\ng_suite_plugin OAuth smoke tests")
    print("=" * 40)
    test_authorization_request_pkce()
    test_store_initial_tokens_order()
    test_cache_hit()
    test_not_connected()
    test_refresh_does_not_rotate_refresh()
    test_invalid_grant_maps_to_auth_expired()
    test_store_raises_on_vault_failure()
    test_callback_html_escapes()
    test_redact_state()
    test_start_interface_host_selection()
    test_start_refuses_an_occupied_port()
    print()
    print(f"Results: {_passed} passed, {len(_failed)} failed")
    if _failed:
        print("FAILED:", _failed)
        return 1
    print("All OAuth smoke tests passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
