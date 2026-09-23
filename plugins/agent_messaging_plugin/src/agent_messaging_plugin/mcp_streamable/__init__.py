"""Streamable HTTP MCP transport for phone-side Claude connectivity.

Sibling of :mod:`agent_messaging_plugin.mcp_bridge` (which is the stdio
transport launched as a CLI subprocess).  Where ``mcp_bridge`` is an
out-of-process client of the solet bridge HTTP surface, ``mcp_streamable``
is an **in-process** MCP server: it mounts a FastAPI router on the
same app that already serves ``/api/v1/bridge/*`` and dispatches each
JSON-RPC tool call directly against :class:`PlatformSurface`,
:class:`BridgeSessionManager`, :class:`PeerRegistry`, and
``agent_messaging_service`` — no subprocess, no HTTP loopback.

Wire-protocol contract: MCP 2025-03-26 Streamable HTTP transport.  See
https://modelcontextprotocol.io/specification/2025-03-26/basic/transports#streamable-http
for the spec.  The single endpoint at ``/api/v1/mcp/streamable``
accepts:

* ``POST`` — JSON-RPC request(s); response is either ``application/json``
  for one-shot requests or ``text/event-stream`` for streamed responses.
* ``GET`` — opens an SSE stream for server-initiated notifications
  (peer messages, bridge-delivery results, channel events).
* ``DELETE`` — explicit session close.

Session identity is allocated by the server at ``initialize`` time as
an ``Mcp-Session-Id`` header on the response; the client echoes it on
every subsequent request.  Each session owns a synthetic bridge
allocated via :meth:`BridgeSessionManager.open` plus a peer-registry
binding registered with the agent_id/agent_instance_id carried in the
HMAC-signed bearer token — so peer_send targeting the phone routes
through the exact same machinery used for stdio-bridge peers.
"""

from .auth import (
    BearerAuthError,
    BearerClaim,
    BearerVerifier,
)
from .router import build_streamable_router
from .session import StreamableSession, StreamableSessionManager

__all__ = [
    "BearerAuthError",
    "BearerClaim",
    "BearerVerifier",
    "StreamableSession",
    "StreamableSessionManager",
    "build_streamable_router",
]
