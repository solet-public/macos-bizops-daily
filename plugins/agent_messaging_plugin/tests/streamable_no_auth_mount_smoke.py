#!/usr/bin/env python3
"""Smoke test for streamable transport OAuth-surface mounting.

Two regression targets, one per ``streamable_no_auth`` setting:

1. **Permissive tunnel mode (``streamable_no_auth=True``).** The no-auth
   branch in ``AgentMessagingPlugin._mount_streamable_transport`` must use
   the module-level logger this plugin already uses elsewhere. The plugin
   instance does not define ``self.logger``; a stray
   ``self.logger.warning(...)`` crashed ``start_interface`` before the
   bridge bound a port.

2. **Enforced mode (``streamable_no_auth=False``).** The OAuth *login*
   surface (``/authorize`` + ``/oauth/token`` + discovery docs) must STILL
   mount under bearer enforcement. Bearer enforcement and the OAuth login
   surface are orthogonal: an external client (ChatGPT / claude.ai) reaching
   a local tunnel with enforcement ON still needs the login endpoints to
   obtain a token. Gating the dynamic surface on ``streamable_no_auth``
   stranded the connector at a 404 on the enforcement cutover — this smoke
   pins that the surface mounts, the REAL verifier (not the permissive one)
   is installed, and a token minted by the dynamic surface interoperates
   with that verifier end-to-end. It also pins the origin-following
   ``resource_metadata`` on the 401 so a cold-start client can rediscover
   the authorization server.

Run:

    .venv/bin/python3 plugins/agent_messaging_plugin/tests/streamable_no_auth_mount_smoke.py
"""

from __future__ import annotations

import base64
import hashlib
import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import yaml

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT / "ananta" / "src"))
sys.path.insert(0, str(REPO_ROOT / "plugins" / "agent_messaging_plugin" / "src"))

from ananta.core.config.config_provider import ConfigProvider  # noqa: E402
from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from agent_messaging_plugin.bridge_sessions import MANAGEMENT_ALLOWLIST  # noqa: E402
from agent_messaging_plugin.mcp_streamable.auth import (  # noqa: E402
    BearerAuthError,
    PermissiveBearerVerifier,
)
from agent_messaging_plugin.mcp_streamable.oauth import (  # noqa: E402
    DEFAULT_TOKEN_TTL_SECONDS,
    OAUTH_AUTHORIZATION_SERVER_PATH,
    OAUTH_AUTHORIZE_PATH,
    OAUTH_PROTECTED_RESOURCE_PATH,
    OAUTH_TOKEN_PATH,
    _oauth_agent_session_id,
)
from agent_messaging_plugin.mcp_streamable.router import (  # noqa: E402
    STREAMABLE_ALIAS_PATH,
    STREAMABLE_PATH,
)
from agent_messaging_plugin.platform_surface import PlatformSurface  # noqa: E402
from agent_messaging_plugin.plugin import (  # noqa: E402
    AgentMessagingPlugin,
    _BridgeRuntimeConfig,
)
from agent_messaging_plugin.process_exposure import ProcessExportPolicy  # noqa: E402


class _FakeBridgeManager:
    def __init__(self) -> None:
        self._next_id = 0
        self._bridges: dict[str, object] = {}

    def open_bridge(self, claim: object, *, parent_pid: int | None = None) -> object:
        del claim, parent_pid
        self._next_id += 1
        bridge = type(
            "FakeBridge",
            (),
            {
                "bridge_id": f"agc-test-{self._next_id}",
                "session_id": f"sess-test-{self._next_id}",
                "closed": False,
                "touch": lambda self: None,
            },
        )()
        self._bridges[bridge.bridge_id] = bridge
        return bridge

    def get(self, bridge_id: str) -> object | None:
        return self._bridges.get(bridge_id)


class _FakePeerRegistry:
    def __init__(self) -> None:
        self.bindings: list[object] = []

    def register(self, binding: object) -> None:
        self.bindings.append(binding)


class _FakePlatformSurface:
    def __init__(self) -> None:
        self.operator_equivalent_check: object | None = None
        self.last_process_call_deliver_to_bridge: bool | None = None

    def set_operator_equivalent_check(self, check: object) -> None:
        # The enforced-mode wiring stamps this callback so an
        # operator-equivalent OAuth client keeps operator authority. We
        # only need to accept it here; the propagation itself is covered
        # by operator_equivalent_propagation_smoke.
        self.operator_equivalent_check = check

    def process_call(
        self,
        process_key: str,
        arguments: dict[str, object],
        *,
        trigger_data: dict[str, object] | None = None,
        deliver_to_bridge: bool = True,
    ) -> dict[str, object]:
        self.last_process_call_deliver_to_bridge = deliver_to_bridge
        return {
            "status": "queued",
            "action_id": "act-test",
            "flow_id": "flow-test",
            "process_key": process_key,
            "arguments": arguments,
            "trigger_data": trigger_data or {},
        }


class _FakeDiscovery:
    def query_process_registry(self, query: str, max_results: int) -> dict[str, object]:
        del query, max_results
        return {
            "processes": [
                {
                    "process_key": "service_interface::knowledge_service::deactivate",
                    "description": "Deactivate a knowledge base.",
                },
            ],
            "process_keys": ["service_interface::knowledge_service::deactivate"],
            "process_count": 1,
        }

    def get_process_schema(self, process_key: str) -> dict[str, object]:
        return {
            "action_status": "completed",
            "data": {
                "process_key": process_key,
                "description": "Search indexed knowledge base content.",
                "invocation_schema": {
                    "type": "object",
                    "properties": {"arguments": {"type": "object"}},
                },
                "is_long_running": False,
                "deprecation": None,
            },
        }


class _PolicyBridgeManager:
    def __init__(self) -> None:
        self.bridge = type(
            "PolicyBridge",
            (),
            {
                "bridge_id": "bridge-policy",
                "closed": False,
                "client_id": "client-test",
                "process_export_allowlist": (
                    "service_interface::knowledge_service::search",
                ),
            },
        )()

    def get(self, bridge_id: str) -> object | None:
        return self.bridge if bridge_id == "bridge-policy" else None


class _FakeOAuthRegistry:
    """Minimal VaultOAuthRegistry stand-in for the enforced-mode checks.

    Backs ``_oauth_client_exists`` (BearerVerifier's M5 §14.3 cross-check)
    and the operator-equivalent propagation wiring.
    """

    def __init__(self, client: dict[str, object]) -> None:
        self._client = client

    def lookup_client(self, client_id: str) -> dict[str, object] | None:
        if client_id == self._client["client_id"]:
            return self._client
        return None

    def is_operator_equivalent(self, client_id: str) -> bool:
        return client_id == self._client["client_id"]


class _FakeVault:
    def __init__(self) -> None:
        self._values: dict[str, str] = {}
        self._client: dict[str, object] = {
            "client_id": "client-test",
            "client_name": "ChatGPT test",
            "scopes": ["mcp:read", "mcp:write"],
            "redirect_uris": [
                "https://chatgpt.com/connector/oauth/test-callback",
            ],
            "grant_types": ["authorization_code", "refresh_token"],
            "operator_approved": True,
        }
        # Exposed as a transitional property on the VaultServiceProxy; the
        # plugin reads it via _maybe_get_vault_oauth_registry.
        self._oauth_registry = _FakeOAuthRegistry(self._client)

    def retrieve(self, key: str) -> dict[str, object]:
        # REAL macos_vault_plugin ActionResult envelope (Dax Part 36 §36.2):
        # action_status=="completed" for BOTH a hit and a genuine miss --
        # there is no top-level "status" key. This fake used to return the
        # imagined {"status": "success"/"error", ...} shape, which is
        # exactly the shape that let the original bearer-HMAC-key bug ship
        # undetected (see vault_bearer_hmac_key_envelope_smoke.py).
        if key not in self._values:
            return {
                "action_status": "completed",
                "data": {"found": False, "key": key, "message": f"Secret '{key}' not found"},
                "error": None,
            }
        return {
            "action_status": "completed",
            "data": {"key": key, "value": self._values[key]},
            "error": None,
        }

    def store(
        self,
        key: str,
        value: str,
        *,
        tags: list[str],
        metadata: dict[str, str],
    ) -> dict[str, object]:
        self._values[key] = value
        return {
            "action_status": "completed",
            "data": {"key": key, "version": 1, "message": "Secret stored"},
            "error": None,
        }

    def lookup_oauth_client(self, client_id: str) -> dict[str, object] | None:
        if client_id == self._client["client_id"]:
            return self._client
        return None

    def record_oauth_client_token_use(
        self,
        client_id: str,
        *,
        client_ip: str = "",
        user_agent: str = "",
        transport: str = "",
    ) -> bool:
        if client_id != self._client["client_id"]:
            return False
        self._client["last_use_ip"] = client_ip
        self._client["last_use_user_agent"] = user_agent
        self._client["last_use_transport"] = transport
        return True

    def verify_oauth_client_credentials(
        self, client_id: str, client_secret: str,
    ) -> dict[str, object] | None:
        if client_id == self._client["client_id"] and client_secret:
            return self._client
        return None

    def issue_oauth_refresh_token(
        self,
        *,
        client_id: str,
        scopes: list[str],
        audience: str,
        ttl_seconds: int,
    ) -> str:
        return "refresh-test"

    def consume_oauth_refresh_token(
        self, cleartext: str,
    ) -> dict[str, object] | None:
        if cleartext != "refresh-test":
            return None
        return {
            "client_id": "client-test",
            "scopes": ["mcp:read", "mcp:write"],
            "audience": "http://127.0.0.1:43210/api/v1/mcp/streamable",
        }


_TUNNEL_RESOURCE = (
    "https://tunnel-service.gateway.unified-0.internal.api.openai.org"
    "/v1/mcp/tunnel_test"
)
_BASE_URL = "http://127.0.0.1:43210"
_REDIRECT_URI = "https://chatgpt.com/connector/oauth/test-callback"


def _build_mounted_app(
    *, no_auth: bool,
) -> tuple[FastAPI, AgentMessagingPlugin, _FakePlatformSurface] | str:
    """Mount the streamable transport on a fresh app; error text on failure."""
    plugin = AgentMessagingPlugin()
    if hasattr(plugin, "logger"):
        return "fixture is invalid; plugin unexpectedly has logger attr"
    fake_vault = _FakeVault()
    plugin._resolve_vault_plugin = (  # type: ignore[method-assign]
        lambda *, require_refresh_token_methods=False: fake_vault
    )
    # _maybe_get_vault_oauth_registry reads the registry off the injected
    # VaultServiceProxy (self._vault_service), NOT _resolve_vault_plugin —
    # so wire it too, or the enforced verifier's client-exists check
    # fail-closes and rejects otherwise-valid tokens.
    plugin._vault_service = fake_vault  # type: ignore[assignment]
    plugin._require_service = lambda: object()  # type: ignore[method-assign]
    app = FastAPI()
    config = _BridgeRuntimeConfig(
        streamable_enabled=True,
        streamable_no_auth=no_auth,
        oauth_resource_aliases=(_TUNNEL_RESOURCE,),
        oauth_enabled=False,
    )
    fake_platform_surface = _FakePlatformSurface()
    try:
        plugin._mount_streamable_transport(
            app=app,
            bridge_manager=_FakeBridgeManager(),  # type: ignore[arg-type]
            peer_registry=_FakePeerRegistry(),  # type: ignore[arg-type]
            platform_surface=fake_platform_surface,  # type: ignore[arg-type]
            bridge_config=config,
        )
    except AttributeError as exc:
        return f"streamable mount raised AttributeError: {exc}"
    except Exception as exc:  # noqa: BLE001
        return f"streamable mount raised {type(exc).__name__}: {exc}"
    return app, plugin, fake_platform_surface


def _check_mounted_surface(
    client: TestClient, plugin: AgentMessagingPlugin,
) -> str | None:
    """A mounted path that 404s must red this check.

    Deliberately request-based, not route.path introspection: fastapi
    0.139.2 wraps app.include_router(...) output (used at plugin.py's
    _mount_streamable_transport call sites) in a private
    _IncludedRouter(BaseRoute) that carries no .path attribute, so a
    {route.path for route in app.routes if hasattr(route, "path")} scan
    silently drops every route mounted that way and reds on a fully
    working mount. A real request is immune to that internal
    representation changing again. Status is checked only for != 404 —
    401/422/whatever is "mounted"; the specific expected status per mode
    (permissive vs enforced) is asserted by the checks that run after this
    one.
    """
    probe_body = {
        "jsonrpc": "2.0",
        "id": 0,
        "method": "tools/call",
        "params": {"name": "not_a_real_tool", "arguments": {}},
    }
    for path in (STREAMABLE_PATH, STREAMABLE_ALIAS_PATH):
        resp = client.post(path, json=probe_body)
        if resp.status_code == 404:
            return f"mounted path returned 404 (not mounted): POST {path}"
    for path in (
        OAUTH_AUTHORIZATION_SERVER_PATH,
        OAUTH_PROTECTED_RESOURCE_PATH,
        OAUTH_AUTHORIZE_PATH,
    ):
        resp = client.get(path)
        if resp.status_code == 404:
            return f"mounted path returned 404 (not mounted): GET {path}"
    # Path-templated resource-metadata route: probe with a known-good
    # substitution rather than the bare template string.
    resp = client.get(OAUTH_PROTECTED_RESOURCE_PATH + STREAMABLE_PATH)
    if resp.status_code == 404:
        return (
            "mounted path returned 404 (not mounted): GET "
            f"{OAUTH_PROTECTED_RESOURCE_PATH}/{{resource_path:path}}"
        )
    resp = client.post(OAUTH_TOKEN_PATH, data={"grant_type": "bogus"})
    if resp.status_code == 404:
        return f"mounted path returned 404 (not mounted): POST {OAUTH_TOKEN_PATH}"
    if plugin._streamable_session_manager is None:
        return "streamable session manager was not installed"
    return None


def _check_protected_resource_metadata(client: TestClient) -> str | None:
    prmd = client.get(OAUTH_PROTECTED_RESOURCE_PATH + STREAMABLE_PATH)
    if prmd.status_code != 200:
        return (
            "path-specific protected-resource metadata returned "
            f"{prmd.status_code}: {prmd.text}"
        )
    prmd_body = prmd.json()
    if prmd_body.get("resource") != _TUNNEL_RESOURCE:
        return f"path-specific metadata resource mismatch: {prmd_body!r}"
    alias_prmd = client.get(
        OAUTH_PROTECTED_RESOURCE_PATH + "/v1/mcp/tunnel_test",
    )
    if alias_prmd.status_code != 200:
        return (
            "alias path-specific protected-resource metadata returned "
            f"{alias_prmd.status_code}: {alias_prmd.text}"
        )
    bad_prmd = client.get(
        OAUTH_PROTECTED_RESOURCE_PATH + "/api/v1/mcp/not-this-server",
    )
    if bad_prmd.status_code != 404:
        return (
            "unrelated path-specific protected-resource metadata returned "
            f"{bad_prmd.status_code}: {bad_prmd.text}"
        )
    return None


def _check_authorization_metadata(client: TestClient) -> str | None:
    auth_meta = client.get(OAUTH_AUTHORIZATION_SERVER_PATH)
    if auth_meta.status_code != 200:
        return (
            "authorization-server metadata returned "
            f"{auth_meta.status_code}: {auth_meta.text}"
        )
    auth_body = auth_meta.json()
    if auth_body.get("issuer") != _BASE_URL:
        return f"authorization metadata issuer mismatch: {auth_body!r}"
    return None


def _check_implicit_session(client: TestClient) -> str | None:
    implicit_session = client.post(
        STREAMABLE_PATH,
        json={
            "jsonrpc": "2.0",
            "id": 99,
            "method": "tools/call",
            "params": {"name": "not_a_real_tool", "arguments": {}},
        },
    )
    if implicit_session.status_code != 200:
        return (
            "tools/call without Mcp-Session-Id returned "
            f"{implicit_session.status_code}: {implicit_session.text}"
        )
    if not implicit_session.headers.get("Mcp-Session-Id"):
        return "implicit-session tools/call did not echo Mcp-Session-Id"
    implicit_body = implicit_session.json()
    if not implicit_body.get("error"):
        return (
            "fixture expected unknown-tool JSON-RPC error body: "
            f"{implicit_body!r}"
        )
    return None


def _initialize_management_session(
    client: TestClient,
) -> tuple[str, str, str | None]:
    initialize = client.post(
        STREAMABLE_PATH,
        json={
            "jsonrpc": "2.0",
            "id": 101,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-03-26",
                "clientInfo": {"name": "smoke", "version": "0"},
            },
        },
    )
    if initialize.status_code != 200:
        return "", "", (
            "initialize for management surface check returned "
            f"{initialize.status_code}: {initialize.text}"
        )
    session_id = initialize.headers.get("Mcp-Session-Id")
    if not session_id:
        return "", "", "initialize did not allocate Mcp-Session-Id"
    instructions = (
        initialize.json()
        .get("result", {})
        .get("instructions", "")
    )
    return session_id, str(instructions), None


def _check_management_instructions(instructions: str) -> str | None:
    if (
        "peer_send_by_name" not in instructions
        or "operator control plane" not in instructions
    ):
        return f"initialize instructions do not prefer role routing: {instructions!r}"
    return None


def _management_tools_by_name(
    client: TestClient,
    session_id: str,
) -> tuple[dict[str, dict[str, object]], str | None]:
    tools = client.post(
        STREAMABLE_PATH,
        headers={"Mcp-Session-Id": session_id},
        json={
            "jsonrpc": "2.0",
            "id": 102,
            "method": "tools/list",
            "params": {},
        },
    )
    if tools.status_code != 200:
        return {}, f"tools/list returned {tools.status_code}: {tools.text}"
    rows = tools.json().get("result", {}).get("tools", [])
    return {
        row.get("name"): row
        for row in rows
        if isinstance(row, dict) and isinstance(row.get("name"), str)
    }, None


def _check_management_tool_descriptions(
    by_name: dict[str, dict[str, object]],
) -> str | None:
    if "peer_send_by_name" not in by_name:
        return f"tools/list omitted peer_send_by_name: {sorted(by_name)}"
    role_desc = str(by_name["peer_send_by_name"].get("description", ""))
    if "preferred task-assignment tool" not in role_desc:
        return f"peer_send_by_name description does not mark preferred use: {role_desc!r}"
    peer_desc = str(by_name.get("peer_send", {}).get("description", ""))
    if "Do not fan one task out to many peer_list entries" not in peer_desc:
        return f"peer_send description does not warn against fan-out: {peer_desc!r}"
    process_call_desc = str(by_name.get("process_call", {}).get("description", ""))
    if "then call `process_result`" not in process_call_desc:
        return (
            "process_call description does not guide Streamable clients to "
            f"process_result polling: {process_call_desc!r}"
        )
    if "do not poll" in process_call_desc:
        return (
            "process_call description still exposes stdio-only no-poll guidance: "
            f"{process_call_desc!r}"
        )
    process_result_desc = str(by_name.get("process_result", {}).get("description", ""))
    if "follow-up read after process_call" not in process_result_desc:
        return (
            "process_result description does not describe ChatGPT-compatible "
            f"follow-up usage: {process_result_desc!r}"
        )
    return None


def _check_tool_error_preserves_request_id(
    client: TestClient,
    session_id: str,
) -> str | None:
    role_send = client.post(
        STREAMABLE_PATH,
        headers={"Mcp-Session-Id": session_id},
        json={
            "jsonrpc": "2.0",
            "id": 103,
            "method": "tools/call",
            "params": {
                "name": "peer_send_by_name",
                "arguments": {
                    "name": "Coordinator-Dusk",
                    "content": "IMPORTANT: smoke check",
                },
            },
        },
    )
    if role_send.status_code != 200:
        return (
            "peer_send_by_name error probe returned "
            f"{role_send.status_code}: {role_send.text}"
        )
    body = role_send.json()
    if body.get("id") != 103:
        return f"tool-raised JSON-RPC error did not preserve id=103: {body!r}"
    error = body.get("error")
    if not isinstance(error, dict):
        return f"peer_send_by_name probe expected JSON-RPC error: {body!r}"
    data = error.get("data")
    if not isinstance(data, dict):
        return f"peer_send_by_name error missing data: {body!r}"
    if data.get("code") != "bridge.state_service_unavailable":
        return f"unexpected peer_send_by_name probe error code: {body!r}"
    return None


def _check_process_call_structured_poll_contract(
    client: TestClient,
    session_id: str,
    platform_surface: _FakePlatformSurface,
) -> str | None:
    call = client.post(
        STREAMABLE_PATH,
        headers={"Mcp-Session-Id": session_id},
        json={
            "jsonrpc": "2.0",
            "id": 104,
            "method": "tools/call",
            "params": {
                "name": "process_call",
                "arguments": {
                    "process_key": "service_interface::knowledge_service::search",
                    "arguments": {"query": "smoke", "top_k": 1},
                },
            },
        },
    )
    if call.status_code != 200:
        return f"process_call probe returned {call.status_code}: {call.text}"
    body = call.json()
    result = body.get("result")
    if not isinstance(result, dict):
        return f"process_call probe missing result: {body!r}"
    structured = result.get("structuredContent")
    if not isinstance(structured, dict):
        return f"process_call probe missing structuredContent: {body!r}"
    if structured.get("action_id") != "act-test":
        return f"process_call structuredContent mismatch: {body!r}"
    if platform_surface.last_process_call_deliver_to_bridge is not False:
        return (
            "Streamable process_call should suppress bridge delivery; got "
            f"{platform_surface.last_process_call_deliver_to_bridge!r}"
        )
    return None


def _check_operator_management_surface(
    client: TestClient,
    platform_surface: _FakePlatformSurface,
) -> str | None:
    session_id, instructions, err = _initialize_management_session(client)
    if err is not None:
        return err
    instructions_err = _check_management_instructions(instructions)
    if instructions_err is not None:
        return instructions_err
    by_name, tools_err = _management_tools_by_name(client, session_id)
    if tools_err is not None:
        return tools_err
    tool_desc_err = _check_management_tool_descriptions(by_name)
    if tool_desc_err is not None:
        return tool_desc_err
    request_id_err = _check_tool_error_preserves_request_id(client, session_id)
    if request_id_err is not None:
        return request_id_err
    return _check_process_call_structured_poll_contract(
        client, session_id, platform_surface,
    )


def _mint_access_token(
    client: TestClient,
) -> tuple[str, dict[str, object], str | None]:
    """Drive the dynamic /authorize -> /oauth/token flow; return the token."""
    code_verifier = "verifier-test-value-with-enough-entropy-for-pkce"
    code_challenge = base64.urlsafe_b64encode(
        hashlib.sha256(code_verifier.encode("ascii")).digest(),
    ).rstrip(b"=").decode("ascii")
    authorize = client.get(
        OAUTH_AUTHORIZE_PATH,
        params={
            "response_type": "code",
            "client_id": "client-test",
            "redirect_uri": _REDIRECT_URI,
            "resource": _TUNNEL_RESOURCE,
            "scope": "mcp:read mcp:write",
            "state": "state-test",
            "code_challenge": code_challenge,
            "code_challenge_method": "S256",
        },
        follow_redirects=False,
    )
    if authorize.status_code != 302:
        return "", {}, (
            "dynamic /authorize did not redirect: "
            f"{authorize.status_code}: {authorize.text}"
        )
    location = authorize.headers.get("location", "")
    if not location.startswith(_REDIRECT_URI + "?code="):
        return "", {}, f"dynamic /authorize redirect mismatch: {location!r}"
    code = parse_qs(urlparse(location).query).get("code", [""])[0]
    token = client.post(
        OAUTH_TOKEN_PATH,
        data={
            "grant_type": "authorization_code",
            "client_id": "client-test",
            "redirect_uri": _REDIRECT_URI,
            "code": code,
            "code_verifier": code_verifier,
            "resource": _TUNNEL_RESOURCE,
        },
    )
    if token.status_code != 200:
        return "", {}, (
            "dynamic /oauth/token rejected tunnel resource: "
            f"{token.status_code}: {token.text}"
        )
    body = token.json()
    access_token = body.get("access_token")
    if not isinstance(access_token, str) or not access_token:
        return "", body, f"dynamic /oauth/token response missing access_token: {body!r}"
    return access_token, body, None


def _decode_access_token_claims(access_token: str) -> dict[str, object] | str:
    try:
        payload_segment = access_token.split(".")[1]
        padding = "=" * (-len(payload_segment) % 4)
        return json.loads(
            base64.urlsafe_b64decode((payload_segment + padding).encode("ascii")),
        )
    except Exception as exc:  # noqa: BLE001
        return f"access_token was not a decodable JWT: {exc}"


def _check_access_token_claims(
    access_token: str,
    token_body: dict[str, object],
) -> str | None:
    claims = _decode_access_token_claims(access_token)
    if isinstance(claims, str):
        return claims
    if claims.get("aud") != _TUNNEL_RESOURCE:
        return f"access_token aud did not preserve tunnel resource: {claims!r}"
    if claims.get("agent_session_id") != _oauth_agent_session_id("client-test"):
        return f"access_token missing stable agent_session_id: {claims!r}"
    expires_in = token_body.get("expires_in")
    if expires_in != DEFAULT_TOKEN_TTL_SECONDS:
        return (
            "dynamic /oauth/token expires_in should match "
            f"DEFAULT_TOKEN_TTL_SECONDS={DEFAULT_TOKEN_TTL_SECONDS}, "
            f"got {expires_in!r}"
        )
    issued_at_raw = claims.get("issued_at")
    exp = claims.get("exp")
    if not isinstance(issued_at_raw, str) or not isinstance(exp, int):
        return f"access_token missing issued_at/exp claims: {claims!r}"
    issued_at = datetime.fromisoformat(
        issued_at_raw.replace("Z", "+00:00"),
    ).astimezone(UTC)
    jwt_ttl = exp - int(issued_at.timestamp())
    if jwt_ttl != DEFAULT_TOKEN_TTL_SECONDS:
        return (
            "access_token JWT exp should be issued_at + "
            f"{DEFAULT_TOKEN_TTL_SECONDS}s, got {jwt_ttl}s: {claims!r}"
        )
    return None


def _check_authorize_token_flow(client: TestClient) -> str | None:
    access_token, token_body, err = _mint_access_token(client)
    if err is not None:
        return err
    claims_error = _check_access_token_claims(access_token, token_body)
    if claims_error is not None:
        return claims_error
    token_probe = client.post(OAUTH_TOKEN_PATH, data={"grant_type": "bogus"})
    if token_probe.status_code == 404:
        return "dynamic /oauth/token is still unmounted"
    return None


def _check_enforced_verifier_is_real(plugin: AgentMessagingPlugin) -> str | None:
    """Enforcement ON must install the REAL verifier, not the permissive one."""
    verifier = plugin._streamable_bearer_verifier
    if verifier is None:
        return "enforced mode: no bearer verifier was installed"
    if isinstance(verifier, PermissiveBearerVerifier):
        return (
            "enforced mode: PermissiveBearerVerifier installed despite "
            "streamable_no_auth=False (enforcement is a no-op)"
        )
    return None


def _check_enforced_token_interop(
    client: TestClient, plugin: AgentMessagingPlugin,
) -> str | None:
    """A token minted by the dynamic surface must pass the REAL verifier.

    This is the crux property of the fix: enforcement + the dynamic login
    surface are only useful together if the surface mints tokens the
    verifier accepts (same HMAC key, client_id + exp present, redundant
    audience check disabled for the origin-following resource).
    """
    access_token, _token_body, err = _mint_access_token(client)
    if err is not None:
        return err
    verifier = plugin._streamable_bearer_verifier
    if verifier is None:
        return "enforced mode: no verifier to interop-test against"
    try:
        claim = verifier.verify(f"Bearer {access_token}")
    except BearerAuthError as exc:
        return (
            "enforced mode: real verifier REJECTED a token minted by its own "
            f"dynamic OAuth surface: {exc.code} {exc.message}"
        )
    if claim.client_id != "client-test":
        return f"enforced mode: verified claim client_id mismatch: {claim.client_id!r}"
    # Negative control: enforcement must actually reject a bad token.
    try:
        verifier.verify("Bearer not.a.real.token")
    except BearerAuthError:
        return None
    return (
        "enforced mode: real verifier ACCEPTED a garbage token — "
        "enforcement is broken"
    )


def _check_enforced_401_resource_metadata(client: TestClient) -> str | None:
    """A tokenless streamable POST must 401 with an origin-following pointer."""
    request_body = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": "not_a_real_tool", "arguments": {}},
    }
    resp = client.post(
        STREAMABLE_PATH,
        json=request_body,
    )
    if resp.status_code != 401:
        return (
            "enforced mode: tokenless streamable POST returned "
            f"{resp.status_code}, expected 401"
        )
    www = resp.headers.get("WWW-Authenticate", "")
    expected = f'resource_metadata="{_BASE_URL}{OAUTH_PROTECTED_RESOURCE_PATH}"'
    if expected not in www:
        return (
            "enforced mode: 401 WWW-Authenticate is missing the "
            f"origin-following resource_metadata pointer: {www!r}"
        )
    bad = client.post(
        STREAMABLE_PATH,
        headers={"Authorization": "Bearer not.a.real.token"},
        json=request_body,
    )
    if bad.status_code != 401:
        return (
            "enforced mode: invalid bearer streamable POST returned "
            f"{bad.status_code}, expected 401"
        )
    bad_www = bad.headers.get("WWW-Authenticate", "")
    for expected_part in (
        'error="invalid_token"',
        'error_description="bearer.invalid_signature"',
        expected,
    ):
        if expected_part not in bad_www:
            return (
                "enforced mode: invalid bearer WWW-Authenticate missing "
                f"{expected_part!r}: {bad_www!r}"
            )
    bad_body = bad.json()
    if bad_body.get("code") != "bearer.invalid_signature":
        return (
            "enforced mode: invalid bearer response body lost its code: "
            f"{bad_body!r}"
        )
    return None


def _permissive_first_failure(
    app: FastAPI,
    plugin: AgentMessagingPlugin,
    platform_surface: _FakePlatformSurface,
) -> str | None:
    client = TestClient(app, base_url=_BASE_URL)
    structural = _check_mounted_surface(client, plugin)
    if structural is not None:
        return structural
    for check in (
        _check_protected_resource_metadata,
        _check_authorization_metadata,
        _check_implicit_session,
        _check_authorize_token_flow,
    ):
        error = check(client)
        if error is not None:
            return error
    return _check_operator_management_surface(client, platform_surface)


def _enforced_first_failure(
    app: FastAPI, plugin: AgentMessagingPlugin,
) -> str | None:
    # Structural: the dynamic OAuth login surface must mount even under
    # enforcement (red-first — before the fix it was gated on no_auth).
    client = TestClient(app, base_url=_BASE_URL)
    structural = _check_mounted_surface(client, plugin)
    if structural is not None:
        return structural
    verifier_err = _check_enforced_verifier_is_real(plugin)
    if verifier_err is not None:
        return verifier_err
    interop_err = _check_enforced_token_interop(client, plugin)
    if interop_err is not None:
        return interop_err
    return _check_enforced_401_resource_metadata(client)


def _check_session_policy_search_appends_allowed_schema_match() -> str | None:
    surface = PlatformSurface(
        action_factory=object(),
        flow_manager=object(),
        compilation_context_builder=object(),
        bridge_manager=_PolicyBridgeManager(),  # type: ignore[arg-type]
        discovery_service=_FakeDiscovery(),
        export_policy=ProcessExportPolicy(
            enabled=True,
            allow_patterns=("service_interface::knowledge_service::*",),
        ),
    )
    result = surface.process_search(
        "knowledge_service search",
        max_results=8,
        bridge_id="bridge-policy",
    )
    keys = result.get("process_keys")
    if not isinstance(keys, list):
        return f"process_search did not return process_keys list: {result!r}"
    if "service_interface::knowledge_service::search" not in keys:
        return f"allowlisted schema match was not appended: {result!r}"
    if "service_interface::knowledge_service::deactivate" in keys:
        return f"out-of-session process leaked through policy filter: {result!r}"
    return None


def _check_runtime_config_token_ttl_default() -> str | None:
    plugin_yaml = yaml.safe_load(
        (REPO_ROOT / "plugins" / "agent_messaging_plugin" / "plugin.yaml").read_text(),
    )
    metadata_default = (
        plugin_yaml.get("config", {})
        .get("oauth_token_ttl_seconds", {})
        .get("default")
    )
    if metadata_default != DEFAULT_TOKEN_TTL_SECONDS:
        return (
            "plugin.yaml oauth_token_ttl_seconds default should be "
            f"{DEFAULT_TOKEN_TTL_SECONDS}, got {metadata_default!r}"
        )
    management_default = (
        plugin_yaml.get("config", {})
        .get("oauth_management_client_ids", {})
        .get("default")
    )
    if management_default != []:
        return (
            "plugin.yaml oauth_management_client_ids default should be [], "
            f"got {management_default!r}"
        )

    plugin = AgentMessagingPlugin()
    plugin.config_provider = ConfigProvider(plugin.name, {})
    default_config = plugin._build_bridge_runtime_config()  # noqa: SLF001
    if default_config.oauth_token_ttl_seconds != DEFAULT_TOKEN_TTL_SECONDS:
        return (
            "runtime config default oauth_token_ttl_seconds should be "
            f"{DEFAULT_TOKEN_TTL_SECONDS}, "
            f"got {default_config.oauth_token_ttl_seconds}"
        )
    if default_config.oauth_management_client_ids != ():
        return (
            "runtime config default oauth_management_client_ids should be empty, "
            f"got {default_config.oauth_management_client_ids!r}"
        )

    plugin.config_provider = ConfigProvider(
        plugin.name,
        {
            "oauth_token_ttl_seconds": 1234,
            "oauth_management_client_ids": ["client-test"],
        },
    )
    override_config = plugin._build_bridge_runtime_config()  # noqa: SLF001
    if override_config.oauth_token_ttl_seconds != 1234:
        return (
            "runtime config explicit oauth_token_ttl_seconds override was not "
            f"preserved: got {override_config.oauth_token_ttl_seconds}"
        )
    if override_config.oauth_management_client_ids != ("client-test",):
        return (
            "runtime config explicit oauth_management_client_ids override was not "
            f"preserved: got {override_config.oauth_management_client_ids!r}"
        )

    plugin._maybe_get_vault_oauth_registry = lambda: None  # type: ignore[method-assign]
    plugin._maybe_get_session_ledger_service = lambda: None  # type: ignore[method-assign]
    claim = type("Claim", (), {"client_id": "client-test"})()
    policy = plugin._resolve_oauth_session_policy(claim)  # noqa: SLF001
    if policy is not MANAGEMENT_ALLOWLIST:
        return (
            "configured oauth management client should receive "
            f"MANAGEMENT_ALLOWLIST, got {policy!r}"
        )
    if "service_interface::knowledge_service::search" not in policy:
        return "MANAGEMENT_ALLOWLIST must include knowledge_service::search"
    return _check_management_allowlist_doc_verbs(policy)


def _check_management_allowlist_doc_verbs(
    policy: tuple[str, ...],
) -> str | None:
    """Doc-authoring lifecycle membership: browse/read/create/edit/archive
    present, delete_file absent (archive_file is the retire path)."""
    doc_lifecycle_verbs = (
        "service_interface::knowledge_service::browse",
        "service_interface::knowledge_service::read_file",
        "service_interface::knowledge_service::create_file",
        "service_interface::knowledge_service::edit_file",
        "service_interface::knowledge_service::archive_file",
    )
    for verb in doc_lifecycle_verbs:
        if verb not in policy:
            return f"MANAGEMENT_ALLOWLIST must include {verb}"
    if "service_interface::knowledge_service::delete_file" in policy:
        return (
            "MANAGEMENT_ALLOWLIST must NOT include knowledge_service::"
            "delete_file (archive_file is the retire path)"
        )
    return None


def main() -> int:
    failure = _check_runtime_config_token_ttl_default()
    if failure is not None:
        print(f"FAIL (runtime-config defaults): {failure}")
        return 1
    failure = _check_session_policy_search_appends_allowed_schema_match()
    if failure is not None:
        print(f"FAIL (session-policy process_search): {failure}")
        return 1

    permissive = _build_mounted_app(no_auth=True)
    if isinstance(permissive, str):
        print(f"FAIL (permissive/no_auth=True): {permissive}")
        return 1
    app_p, plugin_p, platform_p = permissive
    failure = _permissive_first_failure(app_p, plugin_p, platform_p)
    if failure is not None:
        print(f"FAIL (permissive/no_auth=True): {failure}")
        return 1

    enforced = _build_mounted_app(no_auth=False)
    if isinstance(enforced, str):
        print(f"FAIL (enforced/no_auth=False): {enforced}")
        return 1
    app_e, plugin_e, _platform_e = enforced
    failure = _enforced_first_failure(app_e, plugin_e)
    if failure is not None:
        print(f"FAIL (enforced/no_auth=False): {failure}")
        return 1

    print(
        "PASS: streamable transport mounts the dynamic OAuth login surface "
        "in BOTH permissive (no_auth) and enforced modes; under enforcement "
        "the real verifier accepts a token minted by that surface and 401s "
        "carry an origin-following resource_metadata pointer",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
