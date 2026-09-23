# pyright: reportUnusedFunction=false
"""OAuth 2.1 surface for the Streamable HTTP MCP transport.

Implements the endpoints the MCP 2025-06-18 authorization profile
mandates for browser-driven clients (claude.ai's custom connector).
Task #31 hard-disabled Dynamic Client Registration and narrowed the
discovery surface; per-client ``grant_types`` + ``operator_approved``
enforcement live at every token-issuance path.

* **GET ``/.well-known/oauth-authorization-server``** — RFC 8414
  metadata document advertising the authorization + token endpoints,
  the (narrowed) supported grant types (``authorization_code`` plus
  ``refresh_token`` when refresh is enabled — ``client_credentials``
  is intentionally NOT advertised so anonymous clients see only the
  browser flow), PKCE method (``S256``), and scopes. The
  ``registration_endpoint`` field is omitted.
* **GET ``/.well-known/oauth-protected-resource``** — MCP
  2025-06-18 §authorization protected-resource document linking this
  transport to its authorization server.
* **GET ``/authorize``** — RFC 6749 §4.1.1 authorization endpoint.
  Validates ``client_id``, requires ``operator_approved=True`` AND
  ``"authorization_code"`` in the client's stored ``grant_types``,
  exact-matches ``redirect_uri`` against the client's pre-registered
  list, requires PKCE ``S256``, mints a single-use authorization
  code, and redirects to ``{redirect_uri}?code=...&state=...``.
  Auto-approves the consent step (single-user solet; the
  operator already proved possession of ``client_secret`` by
  registering the client out-of-band).
* **POST ``/oauth/token``** — accepts ``authorization_code`` (claude.ai
  connector flow with RFC 7636 PKCE), ``refresh_token`` (OAuth 2.1
  §4.3 rotation), and ``client_credentials`` (operator-created
  machine clients whose stored ``grant_types`` explicitly includes
  it). Every grant verifies ``operator_approved`` AND that the
  requested grant is in the client's stored ``grant_types``;
  ``refresh_token`` is only issued when the client opted in via
  ``grant_types``. Token issuance reuses the existing sealed-box
  bearer encoder so :class:`BearerVerifier` accepts the output
  unchanged; the ``aud`` claim binds the token to this solet's
  canonical MCP URI per RFC 8707.
* **POST ``/register``** — **hard-disabled**: returns plain 404
  (Task #31). Dynamic Client Registration is the only path the
  pre-Task-#31 server had for issuing client credentials without
  operator consent; closing it was the P0 security fix. Operator
  pre-registration via the platform process
  ``service_interface::vault_service::oauth_client_register`` is the
  only way to introduce a usable client.

Out of scope (later): token revocation (RFC 7009), multi-tenant
consent UI, DCR-with-invite-tokens (Task #32).
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import logging
import secrets
import threading
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, Final, Protocol
from urllib.parse import parse_qs, quote, urlencode, urlparse

import jwt
from ananta.services.store.errors import StoreError
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, RedirectResponse, Response

from .auth import HMAC_SIGNING_ALGORITHM

if TYPE_CHECKING:
    from collections.abc import Iterable


logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------
# Endpoint paths.
# ---------------------------------------------------------------------

OAUTH_AUTHORIZATION_SERVER_PATH: Final[str] = (
    "/.well-known/oauth-authorization-server"
)
OAUTH_PROTECTED_RESOURCE_PATH: Final[str] = (
    "/.well-known/oauth-protected-resource"
)
OAUTH_AUTHORIZE_PATH: Final[str] = "/authorize"
OAUTH_TOKEN_PATH: Final[str] = "/oauth/token"
OAUTH_REGISTER_PATH: Final[str] = "/register"


# ---------------------------------------------------------------------
# Configuration constants.
# ---------------------------------------------------------------------

# Access-token TTL.  24h matches what claude.ai Desktop expects: the
# client has no way to silently renew inside a session window, so a
# 5-min token expires before the user invokes a single tool.  Refresh
# tokens (see DEFAULT_REFRESH_TOKEN_TTL_SECONDS) cover longer-term
# renewals.
DEFAULT_TOKEN_TTL_SECONDS: Final[int] = 86_400
DEFAULT_AUTH_CODE_TTL_SECONDS: Final[int] = 600  # RFC 6749 §4.1.2 SHOULD <=10 min
# Refresh-token TTL.  30 days is the canonical OAuth 2.1 value for
# long-lived rotated refresh tokens; clients fall back to a fresh
# authorize flow when this expires.
DEFAULT_REFRESH_TOKEN_TTL_SECONDS: Final[int] = 30 * 24 * 60 * 60
DEFAULT_SCOPES: Final[tuple[str, ...]] = ("mcp:read", "mcp:write")

# OAuth subject the sealed-box claim carries.  Matches the existing
# phone-bearer agent_id so peer_list / native-wake delivery sees a
# single bucket regardless of which token-acquisition path the client
# used.
_OAUTH_AGENT_ID: Final[str] = "claude_phone"

# RFC 6749 §5.2 error tokens.
_ERROR_INVALID_REQUEST: Final[str] = "invalid_request"
_ERROR_INVALID_CLIENT: Final[str] = "invalid_client"
_ERROR_INVALID_GRANT: Final[str] = "invalid_grant"
_ERROR_UNAUTHORIZED_CLIENT: Final[str] = "unauthorized_client"  # RFC 6749 §5.2
_ERROR_UNSUPPORTED_GRANT_TYPE: Final[str] = "unsupported_grant_type"
_ERROR_INVALID_TARGET: Final[str] = "invalid_target"  # RFC 8707 §2



# ---------------------------------------------------------------------
# Adapter protocols — the vault plugin satisfies all three.
# ---------------------------------------------------------------------


class OAuthClientStore(Protocol):
    """Adapter: read + verify OAuth client credentials from the vault."""

    def lookup_oauth_client(self, client_id: str) -> dict[str, Any] | None:
        """Return ``{client_id, client_name, scopes, redirect_uris}`` or None."""
        ...

    def verify_oauth_client_credentials(
        self, client_id: str, client_secret: str,
    ) -> dict[str, Any] | None:
        """Return client metadata on hit, None on bad credentials."""
        ...

    def record_oauth_client_token_use(
        self,
        client_id: str,
        *,
        client_ip: str = "",
        user_agent: str = "",
        transport: str = "",
    ) -> bool:
        """Persist evidence observed at a successful token issuance.

        Returns True iff a client row matched. Never raises for an
        unknown client: the token is already minted when this runs.
        """
        ...


class RefreshTokenStore(Protocol):
    """Adapter: issue + consume OAuth 2.1 refresh tokens from the vault."""

    def issue_oauth_refresh_token(
        self,
        *,
        client_id: str,
        scopes: list[str],
        audience: str,
        ttl_seconds: int,
    ) -> str:
        """Mint a fresh refresh token; persist its hash; return cleartext."""
        ...

    def consume_oauth_refresh_token(
        self, cleartext: str,
    ) -> dict[str, Any] | None:
        """Look up + single-use invalidate a refresh token.

        Returns ``{client_id, scopes, audience}`` on success; None on
        unknown / expired tokens.  The matching row is hard-deleted
        before returning so a replay attempt finds nothing.
        """
        ...


# ---------------------------------------------------------------------
# OAuth endpoint configuration + auth-code cache.
# ---------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class OAuthEndpoints:
    """Canonical URLs the well-known metadata + bearer aud claim echo."""

    issuer: str
    token_endpoint: str
    authorization_endpoint: str
    registration_endpoint: str
    resource: str  # canonical MCP URI; used as the bearer audience
    authorization_servers: tuple[str, ...]
    resource_aliases: tuple[str, ...] = ()


def build_endpoints(*, issuer: str, streamable_path: str) -> OAuthEndpoints:
    """Derive every public-facing URL from a single issuer base."""
    base = issuer.rstrip("/")
    return OAuthEndpoints(
        issuer=base,
        token_endpoint=base + OAUTH_TOKEN_PATH,
        authorization_endpoint=base + OAUTH_AUTHORIZE_PATH,
        registration_endpoint=base + OAUTH_REGISTER_PATH,
        resource=base + streamable_path,
        authorization_servers=(base,),
    )


@dataclass(slots=True)
class _PendingAuthCode:
    """One entry in the in-process auth-code cache."""

    client_id: str
    redirect_uri: str
    code_challenge: str
    scopes: list[str]
    resource: str
    created_at: float = field(default_factory=time.monotonic)


class AuthCodeCache:
    """Thread-safe, TTL-bounded, single-use auth-code store.

    Entries live in process memory only.  A container restart drops
    every in-flight code; the user simply retries the Connect button
    in claude.ai and the new /authorize handshake mints a fresh code.
    Single-use semantics are enforced at pop time — the cache deletes
    the entry the moment it's returned, so a replay returns None.
    """

    def __init__(self, *, ttl_seconds: int = DEFAULT_AUTH_CODE_TTL_SECONDS) -> None:
        self._ttl = ttl_seconds
        self._entries: dict[str, _PendingAuthCode] = {}
        self._lock = threading.Lock()

    def issue(self, entry: _PendingAuthCode) -> str:
        """Mint a fresh code; index ``entry`` under it; return the code."""
        code = secrets.token_urlsafe(32)
        with self._lock:
            self._sweep_locked()
            self._entries[code] = entry
        return code

    def consume(self, code: str) -> _PendingAuthCode | None:
        """Return + remove ``code``; ``None`` on miss or expiry."""
        with self._lock:
            self._sweep_locked()
            entry = self._entries.pop(code, None)
        return entry

    def _sweep_locked(self) -> None:
        """Drop expired entries; caller holds ``self._lock``."""
        now = time.monotonic()
        expired = [
            code
            for code, entry in self._entries.items()
            if now - entry.created_at > self._ttl
        ]
        for code in expired:
            self._entries.pop(code, None)


# ---------------------------------------------------------------------
# Router construction.
# ---------------------------------------------------------------------


def build_oauth_router(
    *,
    endpoints: OAuthEndpoints,
    client_store: OAuthClientStore,
    refresh_token_store: RefreshTokenStore | None = None,
    hmac_key: bytes,
    token_ttl_seconds: int = DEFAULT_TOKEN_TTL_SECONDS,
    auth_code_ttl_seconds: int = DEFAULT_AUTH_CODE_TTL_SECONDS,
    refresh_token_ttl_seconds: int = DEFAULT_REFRESH_TOKEN_TTL_SECONDS,
    scopes_supported: Iterable[str] = DEFAULT_SCOPES,
) -> APIRouter:
    """Build the FastAPI router carrying the five OAuth endpoints.

    ``/register`` is hard-disabled (Task #31): the handler returns a
    plain 404 regardless of what callers configure here. Operator
    pre-registration via the platform process
    ``service_interface::vault_service::oauth_client_register`` is the
    only path to introduce a usable client.

    ``refresh_token_store`` is optional — when omitted,
    ``authorization_code`` responses do not include ``refresh_token``
    and the ``refresh_token`` grant is rejected. Passing the vault
    plugin (which implements ``issue/consume_oauth_refresh_token``)
    enables OAuth 2.1 §4.3 refresh-token rotation.
    """
    router = APIRouter()
    scopes_tuple = tuple(scopes_supported)
    auth_code_cache = AuthCodeCache(ttl_seconds=auth_code_ttl_seconds)
    supports_refresh = refresh_token_store is not None

    @router.get(OAUTH_AUTHORIZATION_SERVER_PATH)
    async def authorization_server_metadata() -> Response:
        return JSONResponse(
            content=_authorization_server_document(
                endpoints, scopes_tuple, supports_refresh=supports_refresh,
            ),
            status_code=200,
        )

    @router.get(OAUTH_PROTECTED_RESOURCE_PATH)
    async def protected_resource_metadata() -> Response:
        return JSONResponse(
            content=_protected_resource_document(endpoints, scopes_tuple),
            status_code=200,
        )

    @router.get(OAUTH_PROTECTED_RESOURCE_PATH + "/{resource_path:path}")
    async def protected_resource_metadata_for_path(
        resource_path: str,
    ) -> Response:
        if not _resource_path_matches(endpoints, resource_path):
            return Response(status_code=404)
        return JSONResponse(
            content=_protected_resource_document(endpoints, scopes_tuple),
            status_code=200,
        )

    @router.get(OAUTH_AUTHORIZE_PATH)
    async def authorize(request: Request) -> Response:
        return _handle_authorize(
            request,
            endpoints=endpoints,
            client_store=client_store,
            auth_code_cache=auth_code_cache,
        )

    @router.post(OAUTH_TOKEN_PATH)
    async def token_endpoint(request: Request) -> Response:
        return await _handle_token_request(
            request,
            endpoints=endpoints,
            client_store=client_store,
            hmac_key=hmac_key,
            auth_code_cache=auth_code_cache,
            refresh_token_store=refresh_token_store,
            token_ttl_seconds=token_ttl_seconds,
            refresh_token_ttl_seconds=refresh_token_ttl_seconds,
        )

    @router.post(OAUTH_REGISTER_PATH)
    async def register_endpoint(request: Request) -> Response:
        # DCR hard-disabled per Task #31; helper returns a plain 404.
        return await _handle_register(request)

    return router


def build_oauth_metadata_router(
    *,
    endpoints: OAuthEndpoints | None = None,
    streamable_path: str,
    scopes_supported: Iterable[str] = DEFAULT_SCOPES,
    supports_refresh: bool = True,
) -> APIRouter:
    """Build just the OAuth discovery metadata routes.

    Used by local tunnel deployments that intentionally leave
    ``streamable_no_auth=True`` because an outer boundary already gates
    MCP access.  The tunnel client still probes OAuth discovery during
    readiness checks, including the RFC 9728 path-specific protected
    resource URL.  When no static ``endpoints`` are supplied, derive the
    issuer/resource from the request URL so dynamic localhost ingress ports
    do not get baked into config.
    """
    router = APIRouter()
    scopes_tuple = tuple(scopes_supported)

    @router.get(OAUTH_AUTHORIZATION_SERVER_PATH)
    async def authorization_server_metadata(request: Request) -> Response:
        resolved = endpoints or _endpoints_from_request(
            request, streamable_path=streamable_path,
        )
        return JSONResponse(
            content=_authorization_server_document(
                resolved, scopes_tuple, supports_refresh=supports_refresh,
            ),
            status_code=200,
        )

    @router.get(OAUTH_PROTECTED_RESOURCE_PATH)
    async def protected_resource_metadata(request: Request) -> Response:
        resolved = endpoints or _endpoints_from_request(
            request, streamable_path=streamable_path,
        )
        return JSONResponse(
            content=_protected_resource_document(resolved, scopes_tuple),
            status_code=200,
        )

    @router.get(OAUTH_PROTECTED_RESOURCE_PATH + "/{resource_path:path}")
    async def protected_resource_metadata_for_path(
        request: Request,
        resource_path: str,
    ) -> Response:
        resolved = endpoints or _endpoints_from_request(
            request, streamable_path=streamable_path,
        )
        if not _resource_path_matches(resolved, resource_path):
            return Response(status_code=404)
        return JSONResponse(
            content=_protected_resource_document(resolved, scopes_tuple),
            status_code=200,
        )

    return router


def build_dynamic_oauth_router(
    *,
    streamable_path: str,
    client_store: OAuthClientStore,
    refresh_token_store: RefreshTokenStore | None = None,
    hmac_key: bytes,
    resource_aliases: Iterable[str] = (),
    token_ttl_seconds: int = DEFAULT_TOKEN_TTL_SECONDS,
    auth_code_ttl_seconds: int = DEFAULT_AUTH_CODE_TTL_SECONDS,
    refresh_token_ttl_seconds: int = DEFAULT_REFRESH_TOKEN_TTL_SECONDS,
    scopes_supported: Iterable[str] = DEFAULT_SCOPES,
) -> APIRouter:
    """Build OAuth routes whose issuer/resource follow the request origin.

    Local OpenAI tunnel deployments terminate the public control-plane route
    outside this FastAPI app and forward requests to a dynamic loopback port.
    The app therefore cannot safely bake an issuer URL at mount time.  Deriving
    endpoints per request keeps ``/authorize`` and ``/oauth/token`` aligned with
    the same origin advertised by discovery metadata.
    """
    router = APIRouter()
    scopes_tuple = tuple(scopes_supported)
    aliases_tuple = tuple(a for a in resource_aliases if a)
    auth_code_cache = AuthCodeCache(ttl_seconds=auth_code_ttl_seconds)
    supports_refresh = refresh_token_store is not None

    def _resolve(request: Request) -> OAuthEndpoints:
        resolved = _endpoints_from_request(request, streamable_path=streamable_path)
        return OAuthEndpoints(
            issuer=resolved.issuer,
            token_endpoint=resolved.token_endpoint,
            authorization_endpoint=resolved.authorization_endpoint,
            registration_endpoint=resolved.registration_endpoint,
            resource=resolved.resource,
            authorization_servers=resolved.authorization_servers,
            resource_aliases=aliases_tuple,
        )

    @router.get(OAUTH_AUTHORIZATION_SERVER_PATH)
    async def authorization_server_metadata(request: Request) -> Response:
        return JSONResponse(
            content=_authorization_server_document(
                _resolve(request),
                scopes_tuple,
                supports_refresh=supports_refresh,
            ),
            status_code=200,
        )

    @router.get(OAUTH_PROTECTED_RESOURCE_PATH)
    async def protected_resource_metadata(request: Request) -> Response:
        return JSONResponse(
            content=_protected_resource_document(_resolve(request), scopes_tuple),
            status_code=200,
        )

    @router.get(OAUTH_PROTECTED_RESOURCE_PATH + "/{resource_path:path}")
    async def protected_resource_metadata_for_path(
        request: Request,
        resource_path: str,
    ) -> Response:
        resolved = _resolve(request)
        if not _resource_path_matches(resolved, resource_path):
            return Response(status_code=404)
        return JSONResponse(
            content=_protected_resource_document(resolved, scopes_tuple),
            status_code=200,
        )

    @router.get(OAUTH_AUTHORIZE_PATH)
    async def authorize(request: Request) -> Response:
        return _handle_authorize(
            request,
            endpoints=_resolve(request),
            client_store=client_store,
            auth_code_cache=auth_code_cache,
        )

    @router.post(OAUTH_TOKEN_PATH)
    async def token_endpoint(request: Request) -> Response:
        return await _handle_token_request(
            request,
            endpoints=_resolve(request),
            client_store=client_store,
            hmac_key=hmac_key,
            auth_code_cache=auth_code_cache,
            refresh_token_store=refresh_token_store,
            token_ttl_seconds=token_ttl_seconds,
            refresh_token_ttl_seconds=refresh_token_ttl_seconds,
        )

    @router.post(OAUTH_REGISTER_PATH)
    async def register_endpoint(request: Request) -> Response:
        return await _handle_register(request)

    return router


# ---------------------------------------------------------------------
# Metadata document builders.
# ---------------------------------------------------------------------


def _authorization_server_document(
    endpoints: OAuthEndpoints,
    scopes: tuple[str, ...],
    *,
    supports_refresh: bool,
) -> dict[str, Any]:
    """RFC 8414 §2 metadata; advertises the supported grants + PKCE.

    Discovery surface is intentionally narrower than the per-client
    enforcement: ``client_credentials`` stays unadvertised in metadata
    (operator-created machine clients can still use it explicitly when
    their stored ``grant_types`` includes it). DCR was removed in
    Task #31 — the ``registration_endpoint`` field is omitted.
    """
    grant_types = ["authorization_code"]
    if supports_refresh:
        grant_types.append("refresh_token")
    return {
        "issuer": endpoints.issuer,
        "authorization_endpoint": endpoints.authorization_endpoint,
        "token_endpoint": endpoints.token_endpoint,
        "token_endpoint_auth_methods_supported": [
            "client_secret_basic",
            "client_secret_post",
            "none",
        ],
        "grant_types_supported": grant_types,
        "response_types_supported": ["code"],
        "code_challenge_methods_supported": ["S256"],
        "scopes_supported": list(scopes),
    }


def _protected_resource_document(
    endpoints: OAuthEndpoints, scopes: tuple[str, ...],
) -> dict[str, Any]:
    """MCP 2025-06-18 §authorization protected-resource document."""
    return {
        "resource": _advertised_resource(endpoints),
        "authorization_servers": list(endpoints.authorization_servers),
        "scopes_supported": list(scopes),
        "bearer_methods_supported": ["header"],
    }


def _endpoints_from_request(
    request: Request, *, streamable_path: str,
) -> OAuthEndpoints:
    """Derive endpoint URLs from the current request origin."""
    base = str(request.base_url).rstrip("/")
    return build_endpoints(issuer=base, streamable_path=streamable_path)


def _resource_path_matches(
    endpoints: OAuthEndpoints, resource_path: str,
) -> bool:
    """True when a path-specific PRMD suffix names this MCP resource."""
    candidates = {
        urlparse(resource).path.lstrip("/")
        for resource in (endpoints.resource, *endpoints.resource_aliases)
    }
    return resource_path.strip("/") in candidates


def _advertised_resource(endpoints: OAuthEndpoints) -> str:
    """Return the resource external MCP clients should bind tokens to."""
    return endpoints.resource_aliases[0] if endpoints.resource_aliases else endpoints.resource


# ---------------------------------------------------------------------
# /authorize handler — synchronous; PKCE only.
# ---------------------------------------------------------------------


def _validate_authorize_response_type(params: dict[str, str]) -> Response | None:
    """RFC 6749 §4.1.1: response_type must be 'code'."""
    response_type = params.get("response_type", "")
    if response_type != "code":
        return _oauth_error(
            _ERROR_UNSUPPORTED_GRANT_TYPE,
            f"unsupported response_type {response_type!r}; only 'code' is supported",
            http_status=400,
        )
    return None


def _resolve_authorize_client(
    params: dict[str, str], client_store: OAuthClientStore,
) -> tuple[str, dict[str, Any]] | Response:
    """Look up + gate the client by id; return (client_id, client) or Response."""
    client_id = params.get("client_id", "")
    if not client_id:
        return _oauth_error(
            _ERROR_INVALID_REQUEST, "client_id is required", http_status=400,
        )
    client = client_store.lookup_oauth_client(client_id)
    if client is None:
        return _oauth_error(
            _ERROR_INVALID_CLIENT,
            f"client_id {client_id!r} is not registered",
            http_status=400,
        )
    approval_error = _require_grant_eligible(client)
    if approval_error is not None:
        return approval_error
    grant_error = _require_client_grant(client, "authorization_code")
    if grant_error is not None:
        return grant_error
    return client_id, client


def _validate_authorize_redirect_uri(
    params: dict[str, str], client: dict[str, Any],
) -> tuple[str, Response | None]:
    """Exact-match the supplied redirect_uri against the registered list."""
    redirect_uri = params.get("redirect_uri", "")
    if not redirect_uri:
        return "", _oauth_error(
            _ERROR_INVALID_REQUEST, "redirect_uri is required", http_status=400,
        )
    registered_uris = client.get("redirect_uris") or []
    if not isinstance(registered_uris, list) or redirect_uri not in registered_uris:
        return redirect_uri, _oauth_error(
            _ERROR_INVALID_REQUEST,
            f"redirect_uri {redirect_uri!r} is not registered for this client",
            http_status=400,
        )
    return redirect_uri, None


def _validate_authorize_pkce(params: dict[str, str]) -> tuple[str, Response | None]:
    """PKCE S256 challenge is mandatory; plain is not supported."""
    code_challenge = params.get("code_challenge", "")
    if not code_challenge:
        return "", _oauth_error(
            _ERROR_INVALID_REQUEST,
            "code_challenge is required (PKCE is mandatory)",
            http_status=400,
        )
    if params.get("code_challenge_method", "") != "S256":
        return code_challenge, _oauth_error(
            _ERROR_INVALID_REQUEST,
            "code_challenge_method must be 'S256' (plain is not supported)",
            http_status=400,
        )
    return code_challenge, None


def _validate_authorize_resource(
    params: dict[str, str], endpoints: OAuthEndpoints,
) -> tuple[str, Response | None]:
    """RFC 8707 §2: resource MUST identify this MCP server when supplied."""
    resource = params.get("resource", "")
    if resource and not _resource_identifies_endpoint(resource, endpoints):
        return "", _oauth_error(
            _ERROR_INVALID_TARGET,
            f"resource {resource!r} does not match this MCP endpoint "
            f"({endpoints.resource!r})",
            http_status=400,
        )
    return (resource or _advertised_resource(endpoints)), None


def _build_authorize_redirect(
    redirect_uri: str, code: str, state: str,
) -> str:
    """Construct the final 302 target preserving any existing query string."""
    redirect_query: dict[str, str] = {"code": code}
    if state:
        redirect_query["state"] = state
    separator = "&" if "?" in redirect_uri else "?"
    return f"{redirect_uri}{separator}{urlencode(redirect_query, quote_via=quote)}"


def _handle_authorize(
    request: Request,
    *,
    endpoints: OAuthEndpoints,
    client_store: OAuthClientStore,
    auth_code_cache: AuthCodeCache,
) -> Response:
    """Validate the /authorize request + redirect with a single-use code."""
    params = dict(request.query_params)
    if (err := _validate_authorize_response_type(params)) is not None:
        return err
    resolved = _resolve_authorize_client(params, client_store)
    if isinstance(resolved, Response):
        return resolved
    client_id, client = resolved
    redirect_uri, err = _validate_authorize_redirect_uri(params, client)
    if err is not None:
        return err
    code_challenge, err = _validate_authorize_pkce(params)
    if err is not None:
        return err
    effective_resource, err = _validate_authorize_resource(params, endpoints)
    if err is not None:
        return err
    scopes = _intersect_scopes(
        list(client.get("scopes") or []), params.get("scope", ""),
    )
    code = auth_code_cache.issue(
        _PendingAuthCode(
            client_id=client_id,
            redirect_uri=redirect_uri,
            code_challenge=code_challenge,
            scopes=scopes,
            resource=effective_resource,
        ),
    )
    state = params.get("state", "")
    target = _build_authorize_redirect(redirect_uri, code, state)
    logger.info(
        "oauth_authorize: client_id=%s redirect_uri=%s code=<%d chars> state=%r",
        client_id, redirect_uri, len(code), state,
    )
    return RedirectResponse(url=target, status_code=302)


# ---------------------------------------------------------------------
# /oauth/token handler — accepts client_credentials AND authorization_code.
# ---------------------------------------------------------------------


async def _handle_token_request(
    request: Request,
    *,
    endpoints: OAuthEndpoints,
    client_store: OAuthClientStore,
    hmac_key: bytes,
    auth_code_cache: AuthCodeCache,
    refresh_token_store: RefreshTokenStore | None,
    token_ttl_seconds: int,
    refresh_token_ttl_seconds: int,
) -> Response:
    """Process a POST /oauth/token request for any supported grant."""
    body = await _parse_token_body(request)
    if isinstance(body, Response):
        return body
    grant_type = body.get("grant_type", "")
    if grant_type == "client_credentials":
        return _handle_client_credentials_grant(
            request,
            body,
            endpoints=endpoints,
            client_store=client_store,
            hmac_key=hmac_key,
            token_ttl_seconds=token_ttl_seconds,
        )
    if grant_type == "authorization_code":
        return _handle_authorization_code_grant(
            request,
            body,
            endpoints=endpoints,
            client_store=client_store,
            hmac_key=hmac_key,
            auth_code_cache=auth_code_cache,
            refresh_token_store=refresh_token_store,
            token_ttl_seconds=token_ttl_seconds,
            refresh_token_ttl_seconds=refresh_token_ttl_seconds,
        )
    if grant_type == "refresh_token":
        if refresh_token_store is None:
            return _oauth_error(
                _ERROR_UNSUPPORTED_GRANT_TYPE,
                "refresh_token grant is not enabled on this server",
                http_status=400,
            )
        return _handle_refresh_token_grant(
            request,
            body,
            endpoints=endpoints,
            client_store=client_store,
            hmac_key=hmac_key,
            refresh_token_store=refresh_token_store,
            token_ttl_seconds=token_ttl_seconds,
            refresh_token_ttl_seconds=refresh_token_ttl_seconds,
        )
    supported = (
        "client_credentials, authorization_code"
        + (", refresh_token" if refresh_token_store is not None else "")
    )
    return _oauth_error(
        _ERROR_UNSUPPORTED_GRANT_TYPE,
        f"unsupported grant_type {grant_type!r}; supported: {supported}",
        http_status=400,
    )


def _handle_client_credentials_grant(
    request: Request,
    body: dict[str, str],
    *,
    endpoints: OAuthEndpoints,
    client_store: OAuthClientStore,
    hmac_key: bytes,
    token_ttl_seconds: int,
) -> Response:
    """RFC 6749 §4.4 client_credentials grant."""
    creds = _extract_client_credentials(request, body)
    if isinstance(creds, Response):
        return creds
    client_id, client_secret = creds
    verification = client_store.verify_oauth_client_credentials(
        client_id, client_secret,
    )
    if verification is None:
        logger.warning(
            "oauth_token (client_credentials): invalid_client client_id=%r",
            client_id,
        )
        return _oauth_error(
            _ERROR_INVALID_CLIENT,
            "client authentication failed",
            http_status=401,
        )
    approval_error = _require_grant_eligible(verification)
    if approval_error is not None:
        return approval_error
    grant_error = _require_client_grant(verification, "client_credentials")
    if grant_error is not None:
        return grant_error
    effective_resource, resource_error = _validate_resource_parameter(body, endpoints)
    if resource_error is not None:
        return resource_error
    granted_scopes = _intersect_scopes(
        list(verification.get("scopes") or []), body.get("scope", ""),
    )
    response = _issue_access_token_response(
        endpoints=endpoints,
        resource=effective_resource,
        hmac_key=hmac_key,
        client_id=verification["client_id"],
        client_name=verification.get("client_name") or "",
        scopes=granted_scopes,
        token_ttl_seconds=token_ttl_seconds,
    )
    _record_token_use(
        request, client_store,
        client_id=str(verification["client_id"]),
        transport="client_credentials",
    )
    return response


def _consume_auth_code(
    body: dict[str, str], auth_code_cache: AuthCodeCache,
) -> tuple[str, _PendingAuthCode] | Response:
    """Pull (code, verifier) from body + single-use consume the cache entry."""
    code = body.get("code", "")
    code_verifier = body.get("code_verifier", "")
    if not code or not code_verifier:
        return _oauth_error(
            _ERROR_INVALID_REQUEST,
            "code and code_verifier are required for authorization_code grant",
            http_status=400,
        )
    entry = auth_code_cache.consume(code)
    if entry is None:
        return _oauth_error(
            _ERROR_INVALID_GRANT,
            "authorization code is unknown, expired, or already redeemed",
            http_status=400,
        )
    return code_verifier, entry


def _validate_auth_code_bindings(
    body: dict[str, str], entry: _PendingAuthCode, code_verifier: str,
) -> Response | None:
    """Match client_id + redirect_uri + PKCE verifier against the bound code."""
    client_id_param = body.get("client_id", "")
    redirect_uri = body.get("redirect_uri", "")
    if client_id_param and client_id_param != entry.client_id:
        return _oauth_error(
            _ERROR_INVALID_GRANT,
            "client_id does not match the one bound to this code",
            http_status=400,
        )
    if redirect_uri and redirect_uri != entry.redirect_uri:
        return _oauth_error(
            _ERROR_INVALID_GRANT,
            "redirect_uri does not match the one bound to this code",
            http_status=400,
        )
    if _pkce_s256_challenge(code_verifier) != entry.code_challenge:
        return _oauth_error(
            _ERROR_INVALID_GRANT,
            "code_verifier does not match the stored code_challenge",
            http_status=400,
        )
    return None


def _resolve_token_grant_client(
    request: Request,
    body: dict[str, str],
    client_store: OAuthClientStore,
    *,
    client_id: str,
    on_lookup_miss_message: str,
    required_grant: str,
) -> dict[str, Any] | Response:
    """Optional-secret client resolution + approval/grant gate.

    Returns the verified client metadata dict, or an error Response.
    Shared between authorization_code and refresh_token grants — RFC
    6749 §4.1.3 permits public clients when PKCE is in use, but a
    supplied secret still gets verified for confidential clients.
    """
    basic = _extract_basic_auth(request)
    supplied_secret = body.get("client_secret") or (
        basic[1] if basic is not None else None
    )
    if supplied_secret:
        client = client_store.verify_oauth_client_credentials(
            client_id, supplied_secret,
        )
        if client is None:
            return _oauth_error(
                _ERROR_INVALID_CLIENT,
                "client authentication failed",
                http_status=401,
            )
    else:
        client = client_store.lookup_oauth_client(client_id)
        if client is None:
            return _oauth_error(
                _ERROR_INVALID_CLIENT,
                on_lookup_miss_message,
                http_status=401,
            )
    approval_error = _require_grant_eligible(client)
    if approval_error is not None:
        return approval_error
    grant_error = _require_client_grant(client, required_grant)
    if grant_error is not None:
        return grant_error
    return client


def _handle_authorization_code_grant(
    request: Request,
    body: dict[str, str],
    *,
    endpoints: OAuthEndpoints,
    client_store: OAuthClientStore,
    hmac_key: bytes,
    auth_code_cache: AuthCodeCache,
    refresh_token_store: RefreshTokenStore | None,
    token_ttl_seconds: int,
    refresh_token_ttl_seconds: int,
) -> Response:
    """RFC 6749 §4.1.3 authorization_code grant + RFC 7636 PKCE."""
    consumed = _consume_auth_code(body, auth_code_cache)
    if isinstance(consumed, Response):
        return consumed
    code_verifier, entry = consumed
    if (err := _validate_auth_code_bindings(body, entry, code_verifier)) is not None:
        return err
    resolved = _resolve_token_grant_client(
        request, body, client_store,
        client_id=entry.client_id,
        on_lookup_miss_message=(
            "client metadata vanished between authorize and token exchange"
        ),
        required_grant="authorization_code",
    )
    if isinstance(resolved, Response):
        return resolved
    verification = resolved
    resource_param = body.get("resource", "")
    if resource_param and resource_param != entry.resource:
        return _oauth_error(
            _ERROR_INVALID_TARGET,
            "resource parameter on /token must match the one supplied at /authorize",
            http_status=400,
        )
    response = _issue_access_token_response(
        endpoints=endpoints,
        resource=entry.resource,
        hmac_key=hmac_key,
        client_id=entry.client_id,
        client_name=verification.get("client_name") or "",
        scopes=entry.scopes,
        token_ttl_seconds=token_ttl_seconds,
        refresh_token_store=refresh_token_store,
        refresh_token_ttl_seconds=refresh_token_ttl_seconds,
        client_metadata=verification,
    )
    _record_token_use(
        request, client_store,
        client_id=entry.client_id,
        transport="authorization_code",
    )
    return response


def _handle_refresh_token_grant(
    request: Request,
    body: dict[str, str],
    *,
    endpoints: OAuthEndpoints,
    client_store: OAuthClientStore,
    hmac_key: bytes,
    refresh_token_store: RefreshTokenStore,
    token_ttl_seconds: int,
    refresh_token_ttl_seconds: int,
) -> Response:
    """OAuth 2.1 §4.3 refresh_token grant — rotates the token on consume."""
    consumed = _consume_refresh_token(body, refresh_token_store)
    if isinstance(consumed, Response):
        return consumed
    claims, stored_client_id = consumed
    if (err := _validate_refresh_client_id_match(body, stored_client_id)) is not None:
        return err
    resolved = _resolve_token_grant_client(
        request, body, client_store,
        client_id=stored_client_id,
        on_lookup_miss_message=(
            "client metadata vanished between refresh issuance and exchange"
        ),
        required_grant="refresh_token",
    )
    if isinstance(resolved, Response):
        return resolved
    client_metadata = resolved
    if (err := _validate_refresh_audience(claims, endpoints)) is not None:
        return err
    effective_resource = str(claims.get("audience") or endpoints.resource)
    response = _issue_access_token_response(
        endpoints=endpoints,
        resource=effective_resource,
        hmac_key=hmac_key,
        client_id=stored_client_id,
        client_name=client_metadata.get("client_name") or "",
        scopes=_scopes_from_claims(claims),
        token_ttl_seconds=token_ttl_seconds,
        refresh_token_store=refresh_token_store,
        refresh_token_ttl_seconds=refresh_token_ttl_seconds,
        client_metadata=client_metadata,
    )
    _record_token_use(
        request, client_store,
        client_id=stored_client_id,
        transport="refresh_token",
    )
    return response


def _consume_refresh_token(
    body: dict[str, str], refresh_token_store: RefreshTokenStore,
) -> tuple[dict[str, Any], str] | Response:
    """Single-use consume of the refresh token; returns (claims, client_id)."""
    refresh_token = body.get("refresh_token", "")
    if not refresh_token:
        return _oauth_error(
            _ERROR_INVALID_REQUEST,
            "refresh_token is required for refresh_token grant",
            http_status=400,
        )
    claims = refresh_token_store.consume_oauth_refresh_token(refresh_token)
    if claims is None:
        return _oauth_error(
            _ERROR_INVALID_GRANT,
            "refresh_token is unknown, expired, or already redeemed",
            http_status=400,
        )
    return claims, str(claims.get("client_id") or "")


def _validate_refresh_client_id_match(
    body: dict[str, str], stored_client_id: str,
) -> Response | None:
    """OAuth 2.1 §4.3.1: forbid cross-client refresh-token use."""
    requested_client_id = body.get("client_id", "")
    if requested_client_id and requested_client_id != stored_client_id:
        return _oauth_error(
            _ERROR_INVALID_GRANT,
            "client_id does not match the one bound to this refresh_token",
            http_status=400,
        )
    return None


def _validate_refresh_audience(
    claims: dict[str, Any], endpoints: OAuthEndpoints,
) -> Response | None:
    """Audience-binding: refuse rotation across migrated solets."""
    bound_audience = claims.get("audience") or ""
    if bound_audience and not _resource_identifies_endpoint(bound_audience, endpoints):
        return _oauth_error(
            _ERROR_INVALID_GRANT,
            "refresh_token is bound to a different audience",
            http_status=400,
        )
    return None


def _scopes_from_claims(claims: dict[str, Any]) -> list[str]:
    """Normalize the scopes payload on a refresh-token claims dict."""
    scopes_raw = claims.get("scopes") or []
    return (
        [str(s) for s in scopes_raw] if isinstance(scopes_raw, list) else []
    )


# ---------------------------------------------------------------------
# POST /register — RFC 7591 dynamic client registration.
# ---------------------------------------------------------------------


async def _handle_register(
    request: Request,  # noqa: ARG001  — kept for signature symmetry
) -> Response:
    """RFC 7591 Dynamic Client Registration is HARD-DISABLED (Task #31).

    The endpoint returns a plain 404 — no error envelope, no
    WWW-Authenticate header (``/register`` is not a protected
    resource, so the auth challenge would only confuse clients and
    logs). The corresponding ``registration_endpoint`` field is
    omitted from the authorization-server metadata so spec-compliant
    clients do not even attempt DCR.

    Operator-pre-registration via the platform process
    ``service_interface::vault_service::oauth_client_register`` is the
    only registration path. Task #32 will reintroduce DCR with
    invite-token validation + ``operator_approved=False`` defaults.
    """
    return Response(status_code=404)


# ---------------------------------------------------------------------
# Token issuance — shared by both grant handlers.
# ---------------------------------------------------------------------


def _record_token_use(
    request: Request,
    client_store: OAuthClientStore,
    *,
    client_id: str,
    transport: str,
) -> None:
    """Record who actually used the credential, best-effort.

    Catches STORAGE and TRANSPORT failures only, by name:
    :class:`StoreError` (the normalized base every ``Store`` backend
    raises) and :class:`OSError` (socket/filesystem). This runs AFTER
    the access token has been minted, so a storage outage must not
    turn a successful, correctly-authenticated issuance into a 500
    over bookkeeping.

    It deliberately does NOT catch bare ``Exception``. A ``TypeError``
    or ``AttributeError`` here is a programming error in this module,
    not a storage outage, and swallowing it would hide a real bug
    behind a log line -- the no-silent-fallback rule. Those propagate.

    The evidence is observed, not self-declared: the peer IP comes
    from the connection, the User-Agent from the request headers, and
    the transport from which grant handler we are in. None of it is
    proof of a product -- a User-Agent is client-controlled -- but it
    is something an operator can compare against the operator-typed
    client_name, which on its own corroborates nothing.
    """
    try:
        client_ip = request.client.host if request.client is not None else ""
        user_agent = request.headers.get("User-Agent", "")
        client_store.record_oauth_client_token_use(
            client_id,
            client_ip=client_ip,
            user_agent=user_agent,
            transport=transport,
        )
    except (StoreError, OSError):
        logger.exception(
            "oauth_token: failed to record token use for client_id=%s "
            "(token was still issued)",
            client_id,
        )


def _issue_access_token_response(
    *,
    endpoints: OAuthEndpoints,
    resource: str | None = None,
    hmac_key: bytes,
    client_id: str,
    client_name: str,
    scopes: list[str],
    token_ttl_seconds: int,
    refresh_token_store: RefreshTokenStore | None = None,
    refresh_token_ttl_seconds: int = DEFAULT_REFRESH_TOKEN_TTL_SECONDS,
    client_metadata: dict[str, Any] | None = None,
) -> Response:
    """Build the JSON response carrying the HMAC-signed access token.

    A ``refresh_token`` is included only when ALL of:
      (a) ``refresh_token_store`` is supplied (server-side enabled),
      (b) ``client_metadata`` is supplied AND its ``grant_types`` list
          contains ``"refresh_token"`` (per-client opted in).

    Client_credentials callers pass neither and never receive a
    refresh_token (their long-lived secret is the renewal mechanism).
    """
    effective_resource = resource or endpoints.resource
    access_token = _issue_access_token(
        client_id=client_id,
        client_name=client_name,
        scopes=scopes,
        resource=effective_resource,
        hmac_key=hmac_key,
        token_ttl_seconds=token_ttl_seconds,
    )
    payload: dict[str, Any] = {
        "access_token": access_token,
        "token_type": "Bearer",
        "expires_in": token_ttl_seconds,
        "scope": " ".join(scopes),
    }
    client_opted_in_refresh = False
    if isinstance(client_metadata, dict):
        raw_grants = client_metadata.get("grant_types")
        if isinstance(raw_grants, list):
            client_opted_in_refresh = "refresh_token" in raw_grants
    if refresh_token_store is not None and client_opted_in_refresh:
        payload["refresh_token"] = refresh_token_store.issue_oauth_refresh_token(
            client_id=client_id,
            scopes=scopes,
            audience=effective_resource,
            ttl_seconds=refresh_token_ttl_seconds,
        )
        payload["refresh_token_expires_in"] = refresh_token_ttl_seconds
    logger.info(
        "oauth_token: issued access_token client_id=%s scopes=%s aud=%s "
        "with_refresh=%s",
        client_id, scopes, effective_resource,
        refresh_token_store is not None and client_opted_in_refresh,
    )
    return JSONResponse(
        content=payload,
        status_code=200,
        headers={
            "Cache-Control": "no-store",
            "Pragma": "no-cache",
        },
    )


def _issue_access_token(
    *,
    client_id: str,
    client_name: str,
    scopes: list[str],
    resource: str,
    hmac_key: bytes,
    token_ttl_seconds: int,
) -> str:
    """Sign a JWT bearer claim with HMAC-SHA256 and return the compact string.

    The payload carries the same fields the BearerVerifier expects
    plus an ``exp`` claim (Unix seconds) so pyjwt enforces expiration
    automatically on the verify side. The ``issued_at`` ISO string
    remains the canonical timestamp used by the verifier's skew
    window check.
    """
    now = datetime.now(UTC).replace(microsecond=0)
    claim: dict[str, Any] = {
        "agent_id": _OAUTH_AGENT_ID,
        "agent_instance_id": f"agi-oauth-{client_id}",
        "agent_session_id": _oauth_agent_session_id(client_id),
        "issued_at": now.isoformat().replace("+00:00", "Z"),
        "session_label": client_name or client_id,
        "scopes": scopes,
        "aud": resource,
        "client_id": client_id,  # M5 §14.2: REQUIRED on every issued token.
        "exp": int((now + timedelta(seconds=token_ttl_seconds)).timestamp()),
    }
    return jwt.encode(claim, hmac_key, algorithm=HMAC_SIGNING_ALGORITHM)


def _oauth_agent_session_id(client_id: str) -> str:
    """Stable logical-session id for a registered OAuth client."""
    digest = hashlib.sha256(client_id.encode("utf-8")).hexdigest()[:32]
    return f"ases-oauth-{digest}"


# ---------------------------------------------------------------------
# Helpers.
# ---------------------------------------------------------------------


async def _parse_token_body(request: Request) -> dict[str, str] | Response:
    """Parse the form-encoded body of a /oauth/token request."""
    content_type = request.headers.get("Content-Type", "").lower()
    raw = await request.body()
    if not raw:
        return _oauth_error(
            _ERROR_INVALID_REQUEST,
            "request body is empty",
            http_status=400,
        )
    if "application/json" in content_type:
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError as exc:
            return _oauth_error(
                _ERROR_INVALID_REQUEST,
                f"JSON body did not parse: {exc}",
                http_status=400,
            )
        if not isinstance(parsed, dict):
            return _oauth_error(
                _ERROR_INVALID_REQUEST,
                "JSON body must be an object",
                http_status=400,
            )
        return {str(k): str(v) for k, v in parsed.items()}
    try:
        decoded = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        return _oauth_error(
            _ERROR_INVALID_REQUEST,
            f"request body is not valid UTF-8: {exc}",
            http_status=400,
        )
    parsed_form = parse_qs(decoded, keep_blank_values=True)
    return {k: v[-1] for k, v in parsed_form.items()}


def _extract_client_credentials(
    request: Request, body: dict[str, str],
) -> tuple[str, str] | Response:
    """Pull client_id + client_secret out of Basic auth OR form body."""
    basic = _extract_basic_auth(request)
    if basic is not None:
        client_id, client_secret = basic
        if not client_id or not client_secret:
            return _oauth_error(
                _ERROR_INVALID_CLIENT,
                "client_id and client_secret must both be present",
                http_status=401,
            )
        return client_id, client_secret
    client_id = body.get("client_id", "")
    client_secret = body.get("client_secret", "")
    if not client_id or not client_secret:
        return _oauth_error(
            _ERROR_INVALID_REQUEST,
            "client_id and client_secret are required (Basic auth or form body)",
            http_status=400,
        )
    return client_id, client_secret


def _extract_basic_auth(request: Request) -> tuple[str, str] | None:
    """Decode HTTP Basic creds if present; return ``None`` otherwise."""
    auth_header = request.headers.get("Authorization", "")
    if not auth_header.lower().startswith("basic "):
        return None
    try:
        decoded = base64.b64decode(
            auth_header.split(None, 1)[1].strip(),
        ).decode("utf-8")
    except (binascii.Error, UnicodeDecodeError, IndexError):
        return None
    if ":" not in decoded:
        return None
    cid, secret = decoded.split(":", 1)
    return cid, secret


def _intersect_scopes(granted: list[str], requested: str) -> list[str]:
    """Intersect the registered scopes with the client's requested set."""
    if not requested.strip():
        return list(granted)
    requested_set = {s for s in requested.split() if s}
    matched = [s for s in granted if s in requested_set]
    return matched or list(granted)


def _validate_resource_parameter(
    body: dict[str, str], endpoints: OAuthEndpoints,
) -> tuple[str, Response | None]:
    """RFC 8707 §2: ``resource`` MUST identify this MCP server when supplied."""
    resource = body.get("resource", "")
    if not resource:
        return _advertised_resource(endpoints), None
    if not _resource_identifies_endpoint(resource, endpoints):
        return "", _oauth_error(
            _ERROR_INVALID_TARGET,
            f"resource {resource!r} does not match this MCP endpoint "
            f"({endpoints.resource!r})",
            http_status=400,
        )
    return resource, None


def _resource_identifies_endpoint(resource: str, endpoints: OAuthEndpoints) -> bool:
    """Return true when ``resource`` is the local MCP URI or a configured alias."""
    accepted = (endpoints.resource, *endpoints.resource_aliases)
    return resource in accepted


def _pkce_s256_challenge(verifier: str) -> str:
    """RFC 7636 §4.2: SHA256(verifier) base64url-encoded without padding."""
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")


def _oauth_error(code: str, description: str, *, http_status: int) -> Response:
    """RFC 6749 §5.2 error envelope."""
    return JSONResponse(
        content={"error": code, "error_description": description},
        status_code=http_status,
    )


def _require_grant_eligible(
    client_metadata: dict[str, Any] | None,
) -> Response | None:
    """Fail-closed grant-eligibility check.

    Spec §13.4 (M5). Returns an ``invalid_client`` 401 response if the
    metadata is missing, malformed, or the client has NEITHER
    ``operator_approved=True`` NOR ``machine_grant_enabled=True``.
    Returns ``None`` to signal "caller continues".

    Two grant-eligibility paths:
    * ``operator_approved=True`` — operator-registered clients (Task #31).
    * ``machine_grant_enabled=True`` — server-internal machine clients
      minted via :func:`VaultOAuthRegistry.mint_internal_machine_client`
      (e.g., shipper pairing — spec §13.4).

    A missing field is treated as ``False`` (strict identity check
    via ``is True``); never infer eligibility from row creation
    timestamps or other side-channel signals.
    """
    if not isinstance(client_metadata, dict):
        return _oauth_error(
            _ERROR_INVALID_CLIENT,
            "client metadata is missing grant eligibility",
            http_status=401,
        )
    if client_metadata.get("operator_approved") is True:
        return None
    if client_metadata.get("machine_grant_enabled") is True:
        return None
    return _oauth_error(
        _ERROR_INVALID_CLIENT,
        "client is not eligible for OAuth grant on this server",
        http_status=401,
    )


def _require_client_grant(
    client_metadata: dict[str, Any], requested_grant: str,
) -> Response | None:
    """Per-client grant-type allowlist enforcement.

    Returns an ``unauthorized_client`` 400 response if the client's
    stored ``grant_types`` does not include the requested grant.
    Returns ``None`` to signal "caller continues".

    A missing or malformed ``grant_types`` field is treated as the
    empty list — every grant is rejected.
    """
    raw_grants = client_metadata.get("grant_types")
    grants: list[str]
    if isinstance(raw_grants, list):
        grants = [str(g) for g in raw_grants if isinstance(g, str)]
    else:
        grants = []
    if requested_grant not in grants:
        return _oauth_error(
            _ERROR_UNAUTHORIZED_CLIENT,
            f"client is not authorized for the {requested_grant!r} grant",
            http_status=400,
        )
    return None


__all__ = [
    "DEFAULT_AUTH_CODE_TTL_SECONDS",
    "DEFAULT_REFRESH_TOKEN_TTL_SECONDS",
    "DEFAULT_SCOPES",
    "DEFAULT_TOKEN_TTL_SECONDS",
    "OAUTH_AUTHORIZATION_SERVER_PATH",
    "OAUTH_AUTHORIZE_PATH",
    "OAUTH_PROTECTED_RESOURCE_PATH",
    "OAUTH_REGISTER_PATH",
    "OAUTH_TOKEN_PATH",
    "AuthCodeCache",
    "OAuthClientStore",
    "OAuthEndpoints",
    "RefreshTokenStore",
    "build_endpoints",
    "build_oauth_router",
]
