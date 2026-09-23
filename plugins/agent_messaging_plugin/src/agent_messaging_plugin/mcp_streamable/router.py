# pyright: reportUnusedFunction=false
"""FastAPI router for the Streamable HTTP MCP transport.

Mounts a single endpoint at ``/api/v1/mcp/streamable`` accepting:

* ``POST`` — JSON-RPC request(s).  Returns ``application/json`` for
  one-shot requests, ``text/event-stream`` for streamed responses
  (not used in v1 — every tool call resolves synchronously inside
  the dispatcher), or 202 Accepted for notification-only payloads.
* ``GET`` — opens an SSE stream the server uses to push
  ``notifications/claude/channel`` events.  Per the MCP spec, the
  server MUST NOT send JSON-RPC responses on this stream (except
  for resumption — we do not implement resumption in v1).
* ``DELETE`` — explicit session close.  Tears down the synthetic
  bridge + peer binding.

Session identity is carried by the ``Mcp-Session-Id`` HTTP header.
The server allocates the id on the ``initialize`` response; every
subsequent request MUST echo it.  Sessions without a matching entry
return HTTP 404 per the spec, prompting the client to re-initialize.

Auth: every request (POST, GET, DELETE) carries
``Authorization: Bearer <sealed_box_token>``.  The token must
decrypt against the solet's vault identity keypair AND match
the session's bound ``agent_instance_id`` after initialize.  The
``initialize`` exchange itself binds the session to the claim's
``agent_instance_id``.

Origin protection: the MCP spec mandates ``Origin`` header
validation against DNS rebinding.  The configured
``allowed_origins`` list (passed in at router build time) gates
incoming connections; an empty list disables the check (for the
phone's mDNS hostname which won't send Origin).
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import logging
from typing import TYPE_CHECKING, Any, Final

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse

from .auth import BearerAuthError, BearerVerifier
from .dispatch import (
    INTERNAL_ERROR,
    PARSE_ERROR,
    DispatchContext,
    JsonRpcError,
    JsonRpcRequest,
    dispatch_request,
    parse_envelope,
)
from .notifications import stream_session_events

if TYPE_CHECKING:
    from ..bridge_sessions import BridgeSessionManager
    from ..peer_registry import PeerRegistry
    from ..platform_surface import PlatformSurface
    from .session import StreamableSession, StreamableSessionManager

logger = logging.getLogger(__name__)


# Streamable HTTP MCP endpoint path.  Single endpoint per the spec —
# the method (POST / GET / DELETE) discriminates.  The router can
# additionally mount the same handlers at one or more aliases (see
# ``build_streamable_router``'s ``path_aliases`` kwarg) — handy when
# an upstream client UI keeps a server URL cached and there's no
# delete affordance for stale entries.
STREAMABLE_PATH: Final[str] = "/api/v1/mcp/streamable"
STREAMABLE_ALIAS_PATH: Final[str] = "/mcp/streamable"

# HTTP header carrying the session id.  Case-insensitive in HTTP, so
# we read via ``request.headers.get`` which lower-cases.
_SESSION_HEADER: Final[str] = "Mcp-Session-Id"

# SSE media type.  Lowercase per the spec, no charset suffix.
_SSE_MEDIA_TYPE: Final[str] = "text/event-stream"

# Auth header.
_AUTH_HEADER: Final[str] = "Authorization"

# JSON-RPC error code for "session not found / expired".  HTTP 404
# carries this in the body per MCP spec §session-management.
_SESSION_NOT_FOUND_CODE: Final[int] = -32001


def build_streamable_router(
    *,
    bridge_manager: BridgeSessionManager,
    peer_registry: PeerRegistry,
    platform_surface: PlatformSurface,
    agent_messaging_service: Any,
    session_manager: StreamableSessionManager,
    bearer_verifier: BearerVerifier,
    state_service: Any | None = None,
    allowed_origins: tuple[str, ...] = (),
    resource_metadata_url: str = "",
    cors_origins: tuple[str, ...] = (),
    path_aliases: tuple[str, ...] = (),
    solet_name: str = "",
) -> APIRouter:
    """Build the FastAPI router carrying the Streamable HTTP MCP endpoint.

    All collaborators are passed at construction time; nothing
    reaches back into a plugin instance.

    Args:
        allowed_origins: DNS-rebinding allow-list per the MCP spec
            security warning.  Empty tuple disables the check.
        resource_metadata_url: Public URL of the
            ``/.well-known/oauth-protected-resource`` document.  Included
            in ``WWW-Authenticate`` headers on 401 responses so
            claude.ai's connector validator can discover the OAuth
            authorization server.  Empty string omits the
            ``resource_metadata`` parameter from the challenge.
        cors_origins: Origins (e.g. ``https://claude.ai``) that
            receive CORS headers + an OPTIONS preflight handler.
            Empty tuple disables both.
    """
    dispatch_ctx = DispatchContext(
        bridge_manager=bridge_manager,
        peer_registry=peer_registry,
        platform_surface=platform_surface,
        agent_messaging_service=agent_messaging_service,
        state_service=state_service,
        solet_name=solet_name,
    )
    router = APIRouter()
    mount_paths = (STREAMABLE_PATH, *path_aliases)
    for path in mount_paths:
        _mount_streamable_handlers(
            router,
            path=path,
            session_manager=session_manager,
            bearer_verifier=bearer_verifier,
            bridge_manager=bridge_manager,
            dispatch_ctx=dispatch_ctx,
            allowed_origins=allowed_origins,
            resource_metadata_url=resource_metadata_url,
            cors_origins=cors_origins,
        )
    return router


def _mount_streamable_handlers(
    router: APIRouter,
    *,
    path: str,
    session_manager: StreamableSessionManager,
    bearer_verifier: BearerVerifier,
    bridge_manager: BridgeSessionManager,
    dispatch_ctx: DispatchContext,
    allowed_origins: tuple[str, ...],
    resource_metadata_url: str,
    cors_origins: tuple[str, ...],
) -> None:
    """Register POST/GET/DELETE/OPTIONS handlers at ``path`` on ``router``.

    Called once per mount point so the primary URL + any aliases all
    route to the same handlers without duplicating their bodies.  All
    handlers close over the shared collaborators above.
    """

    @router.post(path)
    async def streamable_post(request: Request) -> Response:
        response = await _handle_post(
            request,
            session_manager=session_manager,
            bearer_verifier=bearer_verifier,
            dispatch_ctx=dispatch_ctx,
            allowed_origins=allowed_origins,
            resource_metadata_url=resource_metadata_url,
            solet_name=dispatch_ctx.solet_name,
        )
        _apply_cors_headers(response, request, cors_origins)
        return response

    @router.get(path)
    async def streamable_get(request: Request) -> Response:
        response = await _handle_get(
            request,
            session_manager=session_manager,
            bearer_verifier=bearer_verifier,
            bridge_manager=bridge_manager,
            allowed_origins=allowed_origins,
            resource_metadata_url=resource_metadata_url,
            solet_name=dispatch_ctx.solet_name,
        )
        _apply_cors_headers(response, request, cors_origins)
        return response

    @router.delete(path)
    async def streamable_delete(request: Request) -> Response:
        response = await _handle_delete(
            request,
            session_manager=session_manager,
            bearer_verifier=bearer_verifier,
            allowed_origins=allowed_origins,
            resource_metadata_url=resource_metadata_url,
            solet_name=dispatch_ctx.solet_name,
        )
        _apply_cors_headers(response, request, cors_origins)
        return response

    @router.options(path)
    async def streamable_options(request: Request) -> Response:
        """CORS preflight for the streamable endpoint."""
        if not _cors_origin_allowed(request, cors_origins):
            return Response(status_code=204)
        response = Response(status_code=204)
        _apply_cors_headers(response, request, cors_origins)
        response.headers["Access-Control-Allow-Methods"] = (
            "GET, POST, DELETE, OPTIONS"
        )
        response.headers["Access-Control-Allow-Headers"] = (
            "Authorization, Content-Type, Accept, Mcp-Session-Id, "
            "Last-Event-ID"
        )
        response.headers["Access-Control-Max-Age"] = "86400"
        return response


# ---------------------------------------------------------------------
# Method handlers — kept separate so unit tests can drive them with
# fake Request / Response objects.  Each handler validates Origin +
# bearer + session (where applicable) before delegating to dispatch
# or notifications.
# ---------------------------------------------------------------------


async def _handle_post(
    request: Request,
    *,
    session_manager: StreamableSessionManager,
    bearer_verifier: BearerVerifier,
    dispatch_ctx: DispatchContext,
    allowed_origins: tuple[str, ...],
    resource_metadata_url: str = "",
    solet_name: str = "",
) -> Response:
    pre = await _validate_post_preconditions(
        request,
        bearer_verifier=bearer_verifier,
        allowed_origins=allowed_origins,
        resource_metadata_url=resource_metadata_url,
        solet_name=solet_name,
    )
    if isinstance(pre, Response):
        return pre
    claim, envelope = pre
    session_or_err = _resolve_session(
        request, envelope, claim=claim, session_manager=session_manager,
    )
    if isinstance(session_or_err, Response):
        return session_or_err
    return _dispatch_and_build_response(
        envelope, session_or_err, dispatch_ctx,
    )


async def _validate_post_preconditions(
    request: Request,
    *,
    bearer_verifier: BearerVerifier,
    allowed_origins: tuple[str, ...],
    resource_metadata_url: str,
    solet_name: str,
) -> tuple[Any, JsonRpcRequest] | Response:
    """Run origin + auth + body checks and parse the JSON-RPC envelope.

    Returns ``(claim, envelope)`` on success or a ready-to-send
    ``Response`` carrying the appropriate error.
    """
    origin_err = _check_origin(request, allowed_origins)
    if origin_err is not None:
        return origin_err
    auth_err = _authorize_or_response(
        request,
        bearer_verifier,
        resource_metadata_url=resource_metadata_url,
        solet_name=solet_name,
    )
    if isinstance(auth_err, Response):
        return auth_err
    claim = auth_err
    body = await _read_post_body(request)
    if isinstance(body, Response):
        return body
    # Multi-message JSON-RPC batches are permitted by the spec; for v1
    # we accept a single envelope.  Phone clients ship one request at a
    # time, and the spec permits 405 on batches we don't handle.
    if isinstance(body, list):
        return JSONResponse(
            content={
                "jsonrpc": "2.0",
                "id": None,
                "error": {
                    "code": -32600,
                    "message": "JSON-RPC batches are not supported by this server",
                },
            },
            status_code=400,
        )
    try:
        envelope = parse_envelope(body)
    except JsonRpcError as exc:
        return _jsonrpc_error_response(exc, http_status=400)
    return claim, envelope


def _resolve_session(
    request: Request,
    envelope: JsonRpcRequest,
    *,
    claim: Any,
    session_manager: StreamableSessionManager,
) -> StreamableSession | Response:
    """Allocate a fresh session on ``initialize`` or look up an existing one.

    Non-initialize methods MUST carry ``Mcp-Session-Id`` matching a
    session previously bound to this ``agent_instance_id``; mismatches
    return spec-mandated error responses.
    """
    if envelope.method == "initialize":
        return session_manager.allocate(
            claim,
            client_info=_dict_or_empty(envelope.params.get("clientInfo")),
            protocol_version=str(envelope.params.get("protocolVersion") or ""),
        )
    session_header = request.headers.get(_SESSION_HEADER)
    if not session_header:
        logger.warning(
            "streamable POST without %s for method=%s; allocating implicit session",
            _SESSION_HEADER,
            envelope.method,
        )
        return session_manager.allocate(
            claim,
            client_info={},
            protocol_version="",
        )
    session = session_manager.get(session_header)
    if session is None:
        return _session_not_found_response(envelope.id)
    if session.agent_instance_id != claim.agent_instance_id:
        return _bearer_session_mismatch_response(envelope.id)
    return session


def _dispatch_and_build_response(
    envelope: JsonRpcRequest,
    session: StreamableSession,
    dispatch_ctx: DispatchContext,
) -> Response:
    """Run dispatch under structured error handling and shape the HTTP response.

    Notification-only payloads (``response is None``) return 202
    Accepted per spec; ``initialize`` responses include the freshly
    allocated ``Mcp-Session-Id`` header.
    """
    try:
        response = dispatch_request(
            envelope, session=session, context=dispatch_ctx,
        )
    except JsonRpcError as exc:
        error_response = _jsonrpc_error_response(exc, http_status=200)
        error_response.headers[_SESSION_HEADER] = session.mcp_session_id
        return error_response
    except Exception as exc:  # noqa: BLE001 — last-resort safety net
        logger.exception("streamable dispatch failed: %s", envelope.method)
        error_response = _jsonrpc_error_response(
            JsonRpcError(
                INTERNAL_ERROR,
                f"internal error during {envelope.method}: {exc}",
                request_id=envelope.id,
            ),
            http_status=500,
        )
        error_response.headers[_SESSION_HEADER] = session.mcp_session_id
        return error_response
    if response is None:
        return Response(status_code=202)
    response_headers: dict[str, str] = {}
    response_headers[_SESSION_HEADER] = session.mcp_session_id
    return JSONResponse(
        content=response.to_wire(),
        status_code=200,
        headers=response_headers,
    )


async def _handle_get(
    request: Request,
    *,
    session_manager: StreamableSessionManager,
    bearer_verifier: BearerVerifier,
    bridge_manager: BridgeSessionManager,
    allowed_origins: tuple[str, ...],
    resource_metadata_url: str = "",
    solet_name: str = "",
) -> Response:
    origin_err = _check_origin(request, allowed_origins)
    if origin_err is not None:
        return origin_err
    auth_err = _authorize_or_response(
        request,
        bearer_verifier,
        resource_metadata_url=resource_metadata_url,
        solet_name=solet_name,
    )
    if isinstance(auth_err, Response):
        return auth_err
    claim = auth_err
    session_header = request.headers.get(_SESSION_HEADER)
    if not session_header:
        return _missing_session_response(request_id=None)
    session = session_manager.get(session_header)
    if session is None:
        return _session_not_found_response(request_id=None)
    if session.agent_instance_id != claim.agent_instance_id:
        return _bearer_session_mismatch_response(request_id=None)
    # Last-Event-ID lets the client resume an SSE stream after a broken
    # connection by replaying events since that cursor.  Spec §5.2.
    last_event_id = request.headers.get("Last-Event-ID")
    if last_event_id:
        try:
            session.sse_cursor = int(last_event_id)
        except ValueError:
            # Malformed Last-Event-ID — start from current cursor; the
            # spec permits us to ignore it.
            logger.warning(
                "streamable GET: ignoring malformed Last-Event-ID %r",
                last_event_id,
            )
    return StreamingResponse(
        stream_session_events(
            session=session, bridge_manager=bridge_manager,
        ),
        media_type=_SSE_MEDIA_TYPE,
        headers={"Cache-Control": "no-cache"},
    )


async def _handle_delete(
    request: Request,
    *,
    session_manager: StreamableSessionManager,
    bearer_verifier: BearerVerifier,
    allowed_origins: tuple[str, ...],
    resource_metadata_url: str = "",
    solet_name: str = "",
) -> Response:
    origin_err = _check_origin(request, allowed_origins)
    if origin_err is not None:
        return origin_err
    auth_err = _authorize_or_response(
        request,
        bearer_verifier,
        resource_metadata_url=resource_metadata_url,
        solet_name=solet_name,
    )
    if isinstance(auth_err, Response):
        return auth_err
    claim = auth_err
    session_header = request.headers.get(_SESSION_HEADER)
    if not session_header:
        return _missing_session_response(request_id=None)
    session = session_manager.get(session_header)
    if session is None:
        return _session_not_found_response(request_id=None)
    if session.agent_instance_id != claim.agent_instance_id:
        return _bearer_session_mismatch_response(request_id=None)
    session_manager.close(session_header)
    return Response(status_code=204)


# ---------------------------------------------------------------------
# Validation helpers — small, pure, and uniformly return either a
# success value or a ready-to-send Response.
# ---------------------------------------------------------------------


def _check_origin(
    request: Request, allowed_origins: tuple[str, ...],
) -> Response | None:
    """Reject requests whose ``Origin`` header is not in the allow list.

    Per the MCP spec security warning: validate ``Origin`` against DNS
    rebinding attacks.  Empty allow-list disables the check (right for
    Caddy-fronted mDNS access where the iPhone client doesn't send
    Origin); any non-empty allow-list enforces strict membership.
    """
    if not allowed_origins:
        return None
    origin = request.headers.get("Origin")
    if origin is None:
        # No Origin header — allow.  Browser clients send it, native
        # clients (curl, the phone-side MCP client) don't.  We only
        # block when an Origin IS present and disallowed.
        return None
    if origin not in allowed_origins:
        return JSONResponse(
            content={
                "code": "origin_not_allowed",
                "message": (
                    f"Origin {origin!r} is not in the configured allow-list"
                ),
            },
            status_code=403,
        )
    return None


def _authorize_or_response(
    request: Request,
    bearer_verifier: BearerVerifier,
    *,
    resource_metadata_url: str = "",
    solet_name: str = "",
) -> Any:
    """Verify ``Authorization: Bearer <token>``; return claim or Response.

    On 401, the ``WWW-Authenticate: Bearer ...`` header carries a
    ``resource_metadata`` parameter pointing at this transport's
    ``/.well-known/oauth-protected-resource`` document so MCP clients
    (claude.ai's custom-connector validator in particular) can
    discover the authorization server and recover by exchanging
    credentials at ``/oauth/token``.  The MCP spec (2025-06-18
    §authorization) requires this parameter.

    The ``realm`` parameter carries the solet identity so MCP
    clients prompting the user for credentials surface the actual
    deployment name rather than a generic streamable-server label.
    """
    from .oauth import OAUTH_PROTECTED_RESOURCE_PATH  # noqa: PLC0415 — break import cycle
    from .tools import build_server_name  # noqa: PLC0415 — break import cycle
    header = request.headers.get(_AUTH_HEADER)
    try:
        return bearer_verifier.verify(header)
    except BearerAuthError as exc:
        realm = build_server_name(solet_name)
        challenge_error = _oauth_bearer_challenge_error(exc.code)
        challenge = f'Bearer realm="{_quote_auth_param(realm)}"'
        if challenge_error:
            challenge += f', error="{challenge_error}"'
            challenge += (
                f', error_description="{_quote_auth_param(exc.code)}"'
            )
        # A pinned issuer supplies a static resource_metadata URL. Under
        # the dynamic origin-following surface (local tunnel) none is
        # configured, so derive it from the request origin — the same
        # ``base_url`` the dynamic OAuth router echoes into its discovery
        # docs. Without this the 401 carries no discovery pointer and a
        # cold-start MCP client (fresh ChatGPT/claude.ai connect after the
        # enforcement cutover severed its session) cannot rediscover the
        # authorization server.
        effective_metadata_url = resource_metadata_url or (
            str(request.base_url).rstrip("/") + OAUTH_PROTECTED_RESOURCE_PATH
        )
        if effective_metadata_url:
            challenge += (
                ', resource_metadata="'
                f'{_quote_auth_param(effective_metadata_url)}"'
            )
        logger.warning(
            "streamable bearer auth rejected: path=%s code=%s "
            "challenge_error=%s token_profile=%s",
            request.url.path,
            exc.code,
            challenge_error or "",
            _authorization_header_profile(header),
        )
        return JSONResponse(
            content={"code": exc.code, "message": exc.message},
            status_code=401,
            headers={"WWW-Authenticate": challenge},
        )


def _oauth_bearer_challenge_error(code: str) -> str:
    """Map internal verifier codes to RFC 6750 challenge error tokens."""
    if code == "bearer.missing":
        # A bare challenge is enough to trigger discovery/auth. Keep the
        # precise verifier code in the JSON body for operators.
        return ""
    if code in {"bearer.malformed", "bearer.empty"}:
        return "invalid_request"
    return "invalid_token"


def _quote_auth_param(value: str) -> str:
    """Quote-safe value for WWW-Authenticate auth-params."""
    return value.replace("\\", "\\\\").replace('"', '\\"')


def _authorization_header_profile(header: str | None) -> dict[str, Any]:
    """Return non-secret auth-header diagnostics for 401 logs.

    The token itself is a bearer credential, so logs only receive shape,
    length, a short one-way fingerprint, and unverified JWT header fields.
    """
    if not header:
        return {"present": False}
    parts = header.split(None, 1)
    profile: dict[str, Any] = {
        "present": True,
        "scheme": parts[0].lower() if parts else "",
    }
    if len(parts) != 2:
        profile["token"] = "absent"
        return profile
    token = parts[1].strip()
    profile.update({
        "token_chars": len(token),
        "sha256_12": hashlib.sha256(token.encode("utf-8")).hexdigest()[:12],
        "dot_count": token.count("."),
    })
    segments = token.split(".")
    if len(segments) == 3:
        profile["jwt_header"] = _unverified_jwt_header_profile(segments[0])
    return profile


def _unverified_jwt_header_profile(segment: str) -> dict[str, str] | str:
    """Decode a JWT header without trusting it; return only safe fields."""
    try:
        padded = segment + "=" * (-len(segment) % 4)
        raw = base64.urlsafe_b64decode(padded.encode("ascii"))
        parsed = json.loads(raw.decode("utf-8"))
    except (binascii.Error, UnicodeDecodeError, json.JSONDecodeError, ValueError):
        return "unparseable"
    if not isinstance(parsed, dict):
        return "non_object"
    return {
        key: str(parsed[key])
        for key in ("alg", "typ", "kid")
        if key in parsed
    }


# ---------------------------------------------------------------------
# CORS helpers — applied to every streamable response when the request
# carries an Origin matching the configured allow-list.
# ---------------------------------------------------------------------


def _cors_origin_allowed(
    request: Request, cors_origins: tuple[str, ...],
) -> bool:
    """True iff the request's Origin is in the configured allow-list."""
    if not cors_origins:
        return False
    origin = request.headers.get("Origin")
    return origin is not None and origin in cors_origins


def _apply_cors_headers(
    response: Response,
    request: Request,
    cors_origins: tuple[str, ...],
) -> None:
    """Tack CORS headers onto ``response`` when the Origin is allow-listed.

    Per the Fetch spec, browsers ignore CORS headers that don't echo
    the request's Origin exactly, so a wildcard ``*`` is not
    interchangeable with the literal origin string.  We echo whatever
    matched the allow-list.  Non-browser callers (curl, mint smoke
    scripts) don't send Origin and therefore get no headers — which
    is correct.
    """
    if not _cors_origin_allowed(request, cors_origins):
        return
    origin = request.headers.get("Origin") or ""
    response.headers["Access-Control-Allow-Origin"] = origin
    response.headers["Access-Control-Expose-Headers"] = "Mcp-Session-Id"
    response.headers["Vary"] = "Origin"


async def _read_post_body(
    request: Request,
) -> dict[str, Any] | list[dict[str, Any]] | Response:
    """Parse the POST body as JSON; return Response on parse failure."""
    try:
        body = await request.json()
    except (ValueError, json.JSONDecodeError, RuntimeError) as exc:
        return _jsonrpc_error_response(
            JsonRpcError(
                PARSE_ERROR,
                f"failed to parse request body as JSON: {exc}",
                request_id=None,
            ),
            http_status=400,
        )
    if not isinstance(body, dict | list):
        return _jsonrpc_error_response(
            JsonRpcError(
                PARSE_ERROR,
                "request body must be a JSON object or array",
                request_id=None,
            ),
            http_status=400,
        )
    return body


def _missing_session_response(request_id: int | str | None) -> Response:
    return _jsonrpc_error_response(
        JsonRpcError(
            _SESSION_NOT_FOUND_CODE,
            "Mcp-Session-Id header is required for non-initialize requests",
            request_id=request_id,
        ),
        http_status=400,
    )


def _session_not_found_response(request_id: int | str | None) -> Response:
    return _jsonrpc_error_response(
        JsonRpcError(
            _SESSION_NOT_FOUND_CODE,
            "session not found; send a new initialize request without a session id",
            request_id=request_id,
        ),
        http_status=404,
    )


def _bearer_session_mismatch_response(
    request_id: int | str | None,
) -> Response:
    """Reject a request whose bearer claim doesn't match the session binding.

    Prevents one valid token from being replayed against a session
    bound to a different ``agent_instance_id`` — the session is
    durable across the bearer-token skew window, so without this check
    a token issued for instance A could resume instance B's session
    if A's token happens to land first.
    """
    return _jsonrpc_error_response(
        JsonRpcError(
            _SESSION_NOT_FOUND_CODE,
            "bearer token agent_instance_id does not match session binding",
            request_id=request_id,
        ),
        http_status=403,
    )


def _jsonrpc_error_response(exc: JsonRpcError, http_status: int) -> Response:
    return JSONResponse(content=exc.to_wire(), status_code=http_status)


def _dict_or_empty(value: object) -> dict[str, object]:
    return value if isinstance(value, dict) else {}


__all__ = ["STREAMABLE_ALIAS_PATH", "STREAMABLE_PATH", "build_streamable_router"]
