"""SSE notification multiplexer for the Streamable HTTP MCP transport.

Each :class:`StreamableSession` owns a synthetic bridge that
:class:`BridgeSessionManager` deposits events into — peer messages
(``peer_message``), bridge-delivery results / errors, and any
``post_message`` envelopes routed to the session.  When the phone
opens an HTTP GET on the streamable endpoint, this module subscribes
to that event stream and re-emits each event as a JSON-RPC
``notifications/claude/channel`` framed as one Server-Sent-Event per
event.

Output framing matches the SSE wire format expected by browsers and
phone-side HTTP clients:

.. code-block::

   id: <event cursor>
   event: message
   data: {"jsonrpc":"2.0","method":"notifications/claude/channel","params":{...}}

   <blank line>

The ``id`` field carries the underlying ``QueuedEvent.cursor``; the
spec lets the client resume after a broken connection by including
``Last-Event-ID`` on the reconnect GET (handled at the router level
by initialising ``session.sse_cursor`` from that header).

Cancellation: the async generator stops on three conditions:
``BridgeSessionManager.close`` (synthetic bridge gone), client
disconnect (FastAPI propagates :class:`asyncio.CancelledError` into
the generator), or explicit DELETE on the session.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import TYPE_CHECKING, Any, Final

from ..bridge_sessions import BridgeNotFoundError

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from ..bridge_sessions import BridgeSessionManager
    from .session import StreamableSession

logger = logging.getLogger(__name__)


# Long-poll interval inside the SSE loop.  Matches the stdio bridge
# forwarder's value — 25 s gives the asyncio.Event wakeup machinery
# plenty of headroom while keeping idle connections alive enough that
# intermediate proxies don't drop them.
_LONG_POLL_TIMEOUT_S: Final[float] = 25.0

# Heartbeat comment sent on the SSE channel when no event arrives
# within ``_LONG_POLL_TIMEOUT_S``.  SSE comments (lines beginning with
# ``:``) are silently dropped by the client renderer but keep the TCP
# connection from being culled by NAT / HTTP proxies.
_SSE_HEARTBEAT: Final[bytes] = b": heartbeat\n\n"

# Method name MCP clients listen for to render bridge-channel events.
# Identical to the stdio bridge's notification method so a phone-side
# Claude client with a stdio-transport renderer handles streamable
# events with no extra wiring.
_CHANNEL_METHOD: Final[str] = "notifications/claude/channel"


async def stream_session_events(
    *,
    session: StreamableSession,
    bridge_manager: BridgeSessionManager,
) -> AsyncIterator[bytes]:
    """Yield SSE-framed bytes for one phone GET request.

    The caller (the router) wraps this in a
    :class:`fastapi.responses.StreamingResponse` with media-type
    ``text/event-stream``.  The generator runs until the session
    closes or the client disconnects; either condition terminates
    cleanly without raising.
    """
    cursor = session.sse_cursor
    logger.info(
        "streamable SSE stream opened: mcp_session_id=%s bridge_id=%s cursor=%d",
        session.mcp_session_id,
        session.bridge_id,
        cursor,
    )
    try:
        while True:
            try:
                # The acked half is the watcher consumption signal — an MCP
                # transport confirms consumption via /peer/drain instead.
                _, events = await bridge_manager.events_after(
                    session.bridge_id,
                    cursor,
                    timeout_s=_LONG_POLL_TIMEOUT_S,
                )
            except BridgeNotFoundError:
                # Synthetic bridge was closed (explicit DELETE or idle
                # sweep).  Terminate the stream cleanly — the spec
                # treats this as "session expired".
                logger.info(
                    "streamable SSE stream closing: bridge %s gone",
                    session.bridge_id,
                )
                return
            if not events:
                # Long-poll timed out without an event.  Send a comment
                # heartbeat to keep the connection alive; do NOT emit
                # an SSE event (would confuse the client renderer).
                yield _SSE_HEARTBEAT
                continue
            for event in events:
                yield _format_sse_event(event)
                if event.cursor > cursor:
                    cursor = event.cursor
                    session.sse_cursor = cursor
    except asyncio.CancelledError:
        # Client disconnected — propagate up so uvicorn can clean up.
        logger.info(
            "streamable SSE stream cancelled: mcp_session_id=%s",
            session.mcp_session_id,
        )
        raise


def _format_sse_event(event: Any) -> bytes:
    """Render one :class:`QueuedEvent` as an SSE-framed JSON-RPC notification."""
    source_event_type = event.event_type or "post_message"
    content_raw = event.content or ""
    if source_event_type != "post_message":
        # Preserve the discriminator the stdio bridge prepends so the
        # phone-side renderer can string-match identically across
        # transports.
        content = f"[{source_event_type}] {content_raw}"
    else:
        content = content_raw
    meta_in = event.meta if isinstance(event.meta, dict) else {}
    flow_id_raw = meta_in.get("flow_id")
    flow_id = "" if flow_id_raw is None else str(flow_id_raw)
    notification = {
        "jsonrpc": "2.0",
        "method": _CHANNEL_METHOD,
        "params": {
            "content": content,
            "meta": {
                "source": "homunculus",
                "event_type": "post_message",
                "source_event_type": source_event_type,
                "flow_id": flow_id,
                "cursor": str(event.cursor),
            },
        },
    }
    payload = json.dumps(notification, ensure_ascii=False)
    # SSE framing: id + event + data + blank line.  Use bytes so the
    # FastAPI StreamingResponse skips the str→bytes round-trip per chunk.
    return (
        f"id: {event.cursor}\n"
        f"event: message\n"
        f"data: {payload}\n\n"
    ).encode()


__all__ = ["stream_session_events"]
