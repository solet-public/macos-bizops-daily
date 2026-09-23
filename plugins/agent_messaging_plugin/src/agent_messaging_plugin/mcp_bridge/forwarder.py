"""HTTP forwarder for the Python MCP stdio bridge.

The `Forwarder` owns the lifecycle of a single bridge session against
the solet's consolidated `/api/v1/bridge/*` surface:

  - opens a bridge session at startup and remembers `bridge_id`
  - long-polls `/events` and emits each event as an MCP notification
  - exposes one async method per MCP tool that issues the matching
    HTTP request and returns the parsed JSON body

The Node bridges this replaces hardcoded their port; this Forwarder
takes a `base_url` discovered from `port_manager.read_port_file` so
multi-solet operation works.
"""

from __future__ import annotations

import asyncio
import contextlib
import re
import sys
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final
from urllib.parse import quote, urlencode

import httpx
from mcp.shared.message import SessionMessage
from mcp.types import JSONRPCMessage, JSONRPCNotification

from .owed_delivery import OwedDeliveryCoordinator

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from anyio.streams.memory import MemoryObjectSendStream

# Long-poll timeout for /events. The route holds the connection open
# until an event arrives or this elapses; on timeout we reconnect with
# the same cursor and continue.
EVENTS_POLL_TIMEOUT_S: Final[float] = 25.0

# Backoff after a transient HTTP failure before retrying the poll.
POLL_RETRY_DELAY_S: Final[float] = 2.0

# Backoff between bridge open/reconnect attempts when the solet is unreachable.
BRIDGE_CONNECT_RETRY_S: Final[float] = 3.0

# Consecutive poll failures tolerated before we attempt to reopen the bridge.
CONSECUTIVE_POLL_FAILURES_BEFORE_RECONNECT: Final[int] = 3

# Attempts at the peer/register POST before giving up. Bounded, unlike the
# unbounded bridge-open retry, because an open bridge that never registers is
# still useful for process calls -- so this must not block startup forever --
# but a SINGLE attempt loses the peer identity permanently on any transient
# error, leaving a session that works for process_call and is invisible to
# every peer. That half-alive state presents to operators as "that agent is
# ignoring me", never as a registration error.
REGISTER_IDENTITY_ATTEMPTS: Final[int] = 5

# The bridge process-call route is asynchronous. Role claims are tiny EDGE
# actions, so wait briefly for their action_id to reach a terminal result rather
# than declaring the initial `queued` receipt a failed claim.

# Wall-clock interval between steady-state re-assertions of the peer binding.
# Registration at open/reconnect is necessary but not sufficient: the binding
# can go missing underneath a perfectly healthy bridge (server-side idle sweep,
# a competing registration under the same label, or a reconnect race).
#
# This MUST be elapsed-time based, never successful-drain-count based. A long
# poll returns immediately when events are queued, so an event burst can produce
# hundreds of successful drains in seconds. The prior "8 drains" scheduler
# therefore collapsed its intended few-minute heartbeat into a ~1.8s register /
# claim burst under INF-06 traffic. A monotonic deadline keeps event volume from
# changing heartbeat frequency and is immune to wall-clock adjustments.
REGISTER_REASSERT_INTERVAL_S: Final[float] = 200.0

# Per-request timeout for non-long-poll calls.
DEFAULT_REQUEST_TIMEOUT_S: Final[float] = 30.0
UNCLAIMED_AGENT_SESSION_ID: Final[str] = "__unclaimed__"

CLAUDE_CHANNEL_NOTIFICATION_METHOD: Final[str] = "notifications/claude/channel"
SOLET_PEER_MESSAGE_NOTIFICATION_METHOD: Final[str] = "notifications/homunculus/peer_message"
CODEX_NATIVE_PEER_EVENT_TYPES: Final[frozenset[str]] = frozenset(
    {"peer_message", "post_message"},
)


def _log(msg: str) -> None:
    """Write to stderr; stdout is reserved for MCP JSON-RPC framing."""
    print(f"[solet-bridge] {msg}", file=sys.stderr, flush=True)


# Path pattern for a route addressed at a specific bridge_id (Bridge id
# prefix is ``agc-`` per agent_messaging_plugin/01_bridge_overview.md). A
# 404 against such a path means the solet doesn't know this bridge anymore —
# typically because the solet restarted and minted a fresh bridge_id pool, so
# the subprocess's cached id is stale. Anything matching this regex with
# status 404 should trigger a reconnect.
_BRIDGE_ID_ROUTE_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"^/api/v1/bridge/agc-[^/]+(?:/|$)",
)
_BRIDGE_NOT_FOUND_CODE: Final[str] = "bridge_not_found"


def _is_bridge_gone(exc: BaseException) -> bool:
    """True if an HTTP error indicates the bridge session no longer exists.

    This deliberately fails closed.  A route under the caller's ``agc-``
    prefix can return ``peer_unreachable`` when its *recipient* disappeared;
    that must not reconnect the healthy caller bridge.  Only the server's
    affirmative own-bridge shape (404 + ``bridge_not_found``) is actionable.
    """
    status_code = getattr(exc, "status_code", None)
    path = getattr(exc, "path", None)
    if (
        status_code == 404
        and isinstance(path, str)
        and _BRIDGE_ID_ROUTE_PATTERN.match(path)
        and getattr(exc, "response_code", None) == _BRIDGE_NOT_FOUND_CODE
    ):
        return True
    return False


_ROLE_CLAIM_ACTIONS: Final[frozenset[str]] = frozenset(
    {"claimed", "updated", "displaced"},
)


def _role_claim_succeeded(payload: dict[str, Any]) -> bool:
    """Return whether the claim response confirms a landed role binding.

    Reads the SYNCHRONOUS ``peer/claim_role`` body — the outcome is the
    response, so there is no receipt to resolve and no ``status`` envelope to
    unwrap. The three actions are the complete set the shared claim body can
    return on success: ``claimed`` (fresh), ``updated`` (idempotent
    self-re-claim, the steady-state case), ``displaced`` (took it from another
    session). Matching the set explicitly rather than "no error key" keeps an
    unrecognised action loud instead of passing as success.
    """
    return str(payload.get("action") or "") in _ROLE_CLAIM_ACTIONS


class BridgeHTTPError(RuntimeError):
    """HTTP call to the solet bridge surface failed.

    Carries the parsed response code with ``status_code`` and ``path`` so
    :func:`_is_bridge_gone` can distinguish the caller's missing bridge from
    a recipient-scoped 404 on the same caller-addressed route.
    """

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        path: str | None = None,
        response_code: str | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.path = path
        self.response_code = response_code


class Forwarder:
    """Owns the bridge session and forwards MCP tool calls to solet HTTP."""

    def __init__(
        self,
        base_url: str,
        solet_name: str,
        *,
        agent_id: str,
        agent_instance_id: str,
        agent_session_id: str = "",
        session_label: str,
        parent_pid: int,
        provides_inference: bool,
        wake_capable: bool = True,
        session_role: str = "",
        monotonic_clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._solet_name = solet_name
        self._agent_id = agent_id
        self._agent_instance_id = agent_instance_id
        # v10 Control #2.D: the stable logical-session key, forwarded on every
        # peer/register so the server can store it on the BridgeBinding (for the
        # reconnect self-refresh CAS) + inject it into role-claim verbs. Empty
        # when no carrier set it (read-defensively → CAS fails closed).
        self._agent_session_id = agent_session_id
        self._session_label = session_label
        self._session_role = session_role
        # Server's answer to "do I still hold self._session_role?", refreshed on
        # every peer/register response. "held" suppresses the re-claim; anything
        # else ("not_held" / "unknown" / never-answered) claims as before, so an
        # older server that does not send the field degrades to prior behavior.
        self._session_role_held = "unknown"
        self._parent_pid = parent_pid
        # INF-01 §D.9 client half: declared on EVERY register POST (auto,
        # reconnect, and manual relabel) — the server's provider-sidecar
        # populate and the sys:autonomic Trigger-1 vacancy-fill both key
        # off it, and the sidecar entry does not survive a reconnect, so
        # the capability must be re-asserted each time.
        self._provides_inference = provides_inference
        # codex-watch-migration wake_capable design (2026-08-06): declared on
        # every register POST, same re-assertion discipline as
        # provides_inference above — the server stores it on the
        # BridgeBinding, and a reconnect must re-declare it (nothing here
        # survives implicitly).
        self._wake_capable = wake_capable
        self._monotonic_clock = monotonic_clock
        self._client = httpx.AsyncClient(
            base_url=self._base_url,
            timeout=DEFAULT_REQUEST_TIMEOUT_S,
        )
        self._bridge_id: str | None = None
        self._cursor: int = -1
        # ``_cursor`` belongs to one BridgeSessionState and is intentionally
        # reset on each open.  ``_generation`` fences late work from an old
        # BridgeSessionState; ``_claude_wire_cursor`` instead belongs to this
        # forwarder/Claude-MCP lifetime and therefore never resets on open.
        self._generation: int = 0
        self._claude_wire_cursor: int = 0
        self._poll_task: asyncio.Task[None] | None = None
        self._poll_active: bool = False
        self._reconnect_lock = asyncio.Lock()
        self._write_stream: MemoryObjectSendStream[SessionMessage] | None = None
        # v10 Control #5: client-side at-least-once OWED role delivery (live
        # settle + repair drain). Structurally satisfies OwedDeliveryTransport
        # via the bridge_ready / running / emit_event / drain_page /
        # flip_delivered surface.
        self._owed_delivery = OwedDeliveryCoordinator(self)

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def bind_write_stream(
        self,
        write_stream: MemoryObjectSendStream[SessionMessage],
    ) -> None:
        """Attach the MCP write stream so the poll loop can emit notifications."""
        self._write_stream = write_stream

    @property
    def bridge_id(self) -> str | None:
        return self._bridge_id

    async def open_bridge(self) -> str:
        """Open a bridge session, register identity, start the poll loop."""
        await self._open_with_retry()
        effective_label = await self._register_identity()
        if effective_label is not None and self._claim_needed():
            await self._claim_session_role()
        self._poll_active = True
        self._poll_task = asyncio.create_task(self._long_poll_loop())
        self._owed_delivery.start()
        if self._bridge_id is None:
            msg = "bridge_id missing after open_with_retry"
            raise BridgeHTTPError(msg)
        return self._bridge_id

    async def close(self) -> None:
        """Stop the poll + repair loops, close the bridge, dispose the client."""
        self._poll_active = False
        await self._owed_delivery.stop()
        if self._poll_task is not None:
            self._poll_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._poll_task
        if self._bridge_id is not None:
            try:
                await self._post(f"/api/v1/bridge/{self._bridge_id}/close", {})
            except Exception as exc:  # noqa: BLE001
                _log(f"close (best-effort) failed: {exc}")
        await self._client.aclose()

    # ------------------------------------------------------------------
    # Internal: open + identity
    # ------------------------------------------------------------------

    async def _open_with_retry(self) -> None:
        """Retry POST /api/v1/bridge/open until the solet accepts the connection."""
        attempt = 0
        while True:
            attempt += 1
            try:
                payload = await self._post(
                    "/api/v1/bridge/open",
                    {"parent_pid": self._parent_pid},
                )
                bridge_id = payload.get("bridge_id")
                if not isinstance(bridge_id, str):
                    msg = f"bridge open returned no bridge_id: {payload!r}"
                    raise BridgeHTTPError(msg)
                self._bridge_id = bridge_id
                self._cursor = -1
                self._generation += 1
                _log(f"bridge opened (parent_pid={self._parent_pid}): {bridge_id}")
                return
            except Exception as exc:  # noqa: BLE001
                _log(f"waiting for solet bridge API (attempt {attempt}): {exc}")
                await asyncio.sleep(BRIDGE_CONNECT_RETRY_S)

    async def _register_identity(self) -> str | None:
        """Replace the peer-registry binding for this bridge subprocess.

        Returns the **effective** ``session_label`` from the server's
        response — the preserve-on-empty path (2026-06-01 §4.2) may
        restore a previously-stored label even when this subprocess's
        cached ``self._session_label`` is empty. Returns ``None`` on
        registration failure (logged) so callers can decide whether
        to suppress the reconnect announcement.
        """
        if self._bridge_id is None:
            return None
        body = {
            "agent_id": self._agent_id,
            "agent_instance_id": self._agent_instance_id,
            "agent_session_id": self._agent_session_id,
            "session_label": self._session_label,
            "parent_pid": self._parent_pid,
            "provides_inference": self._provides_inference,
            "wake_capable": self._wake_capable,
            "session_role": self._session_role,
        }
        for attempt in range(1, REGISTER_IDENTITY_ATTEMPTS + 1):
            try:
                payload = await self._post(
                    f"/api/v1/bridge/{self._bridge_id}/peer/register",
                    body,
                )
            except Exception as exc:  # noqa: BLE001
                _log(
                    "peer identity registration failed "
                    f"(attempt {attempt}/{REGISTER_IDENTITY_ATTEMPTS}): {exc}",
                )
                if attempt == REGISTER_IDENTITY_ATTEMPTS:
                    _log(
                        "PEER IDENTITY UNREGISTERED after "
                        f"{REGISTER_IDENTITY_ATTEMPTS} attempts: this session can "
                        "call processes but is INVISIBLE to peer_list/peer_send "
                        "and cannot receive peer messages. Recover with the "
                        "peer_register tool.",
                    )
                    return None
                await asyncio.sleep(BRIDGE_CONNECT_RETRY_S)
                continue
            effective_label_raw = payload.get("session_label")
            effective_label = (
                str(effective_label_raw)
                if isinstance(effective_label_raw, str)
                else ""
            )
            self._cache_effective_agent_session_id(payload)
            self._cache_session_role_held(payload)
            _log(
                f"peer identity registered: {self._agent_id}/{self._agent_instance_id}"
                f' (label="{effective_label}")',
            )
            return effective_label
        return None

    async def _reassert_identity(self) -> None:
        """Steady-state re-assert of the peer binding from the poll loop.

        Single attempt, deliberately: this runs every few minutes forever, so a
        retry storm here would be worse than the missed window it papers over —
        the next tick is the retry. Failure is logged and otherwise ignored; the
        bridge stays useful for process calls either way, and `_handle_poll_failure`
        owns the reconnect decision.

        Silent on the happy path. Logging every few minutes would bury the
        `peer identity registered` line that actually matters at open/reconnect.

        The role re-claim is CONDITIONAL (`_claim_needed`): register itself is an
        INFRA route, but the claim runs through MODEL_INITIATED `/process/call`,
        so an unconditional re-claim here stamped model activity with no model
        turn every few minutes forever — silently consuming owed wakes to an idle
        session. When the register response confirms the role is still held there
        is nothing to recover, so the claim is skipped entirely.
        """
        if self._bridge_id is None:
            return
        try:
            payload = await self._post(
                f"/api/v1/bridge/{self._bridge_id}/peer/register",
                {
                    "agent_id": self._agent_id,
                    "agent_instance_id": self._agent_instance_id,
                    "agent_session_id": self._agent_session_id,
                    "session_label": self._session_label,
                    "parent_pid": self._parent_pid,
                    "provides_inference": self._provides_inference,
                    "wake_capable": self._wake_capable,
                    "session_role": self._session_role,
                },
            )
            self._cache_effective_agent_session_id(payload)
            self._cache_session_role_held(payload)
            if self._claim_needed():
                await self._claim_session_role()
        except Exception as exc:  # noqa: BLE001
            _log(f"steady-state peer identity re-assert failed (will retry): {exc}")

    async def _claim_session_role(self) -> bool:
        """Claim the configured standing role for this registered bridge identity."""
        if not self._session_role:
            return True
        if self._bridge_id is None:
            return False
        if not self._agent_session_id:
            _log(
                f'ROLE UNCLAIMED: "{self._session_role}" requires a non-empty '
                "agent_session_id",
            )
            return False

        try:
            # The INFRA transport, and the reason this method no longer polls.
            # Claiming through MODEL_INITIATED /process/call stamped model
            # activity for a claim no model made — silently consuming owed
            # IMPORTANT wakes — and, being an EDGE process, delivered its
            # outcome as a bridge_delivery_result notification on every tick.
            # This route runs the same shared body synchronously and answers in
            # the response, so neither happens. Identity is taken from this
            # bridge's registered binding server-side; only the role name is
            # sent. A genuine /rename claim still uses /process/call and still
            # stamps, which is correct.
            payload = await self._post(
                f"/api/v1/bridge/{self._bridge_id}/peer/claim_role",
                {"name": self._session_role},
            )
        except Exception as exc:  # noqa: BLE001
            _log(f'ROLE UNCLAIMED: "{self._session_role}" claim failed: {exc}')
            return False

        if not _role_claim_succeeded(payload):
            _log(
                f'ROLE UNCLAIMED: "{self._session_role}" claim returned '
                f"{payload!r}",
            )
            return False
        _log(
            f'peer role claimed: "{self._session_role}" '
            f"for {self._agent_id}/{self._agent_instance_id}",
        )
        return True

    def _cache_session_role_held(self, payload: dict[str, Any]) -> None:
        """Adopt the register response's ``session_role_held`` verdict.

        Absent or non-string (a server predating the field) reads as
        ``"unknown"`` — the caller then claims exactly as it always did.
        """
        raw = payload.get("session_role_held")
        self._session_role_held = raw if isinstance(raw, str) else "unknown"

    def _claim_needed(self) -> bool:
        """Should the configured role be (re-)claimed after a registration?

        ``False`` only when the server just confirmed we STILL hold the role.
        The claim runs through ``/process/call``, a MODEL_INITIATED route, so
        issuing it from the forwarder with no model turn phantom-stamps
        ``last_model_activity_at`` and can mark an owed wake to an idle session
        consumed. Skipping a claim that would change nothing removes that stamp
        from the steady state; every other verdict still claims, so a genuine
        recovery is never suppressed.
        """
        return self._session_role_held != "held"

    def _cache_effective_agent_session_id(self, payload: dict[str, Any]) -> None:
        """Adopt a server-preserved logical session id from /peer/register."""
        raw_agent_session_id = payload.get("agent_session_id")
        if not isinstance(raw_agent_session_id, str):
            return
        if (
            not raw_agent_session_id
            or raw_agent_session_id == UNCLAIMED_AGENT_SESSION_ID
            or raw_agent_session_id == self._agent_session_id
        ):
            return
        self._agent_session_id = raw_agent_session_id

    async def _reconnect(self) -> None:
        """Reopen the bridge, re-register identity, announce reconnect.

        After a successful re-register, emit a channel_message so the
        operator sees ``Solet reconnected -- peer_registry restored as
        <label>`` (2026-06-01 reconnect-UX design §5.1). Slice A's
        server-side preserve-on-empty contract makes ``<label>`` the
        operator's last ``/rename`` value automatically; the prose
        falls back to ``no prior label found`` when no stored label
        was recovered (first-time peer or post-DB-wipe).
        """
        async with self._reconnect_lock:
            _log("reconnecting bridge...")
            await self._open_with_retry()
            effective_label = await self._register_identity()
            if effective_label is not None:
                if self._claim_needed():
                    await self._claim_session_role()
                await self._announce_reconnect(effective_label)

    async def _announce_reconnect(self, effective_label: str) -> None:
        """Emit a channel_message announcing the successful reconnect.

        Best-effort: the announcement is operator UX, not load-bearing
        for correctness. A missing write stream (very early-startup
        race) or a send failure both fall through silently with a
        stderr log — they MUST NOT cause the reconnect itself to
        retry, since the reconnect already succeeded by the time we
        get here.
        """
        if self._write_stream is None:
            return
        suffix = (
            f"peer_registry restored as {effective_label}"
            if effective_label
            else "no prior label found"
        )
        content = f"Solet reconnected -- {suffix}"
        # Wire-meta shape MUST match the 5-key contract Claude Code accepts
        # (see ``_claude_channel_meta`` — empty ``flow_id`` is silently
        # rejected at the client renderer). The announcement is locally
        # minted, not a passthrough of a server event, so we synthesize
        # a stable flow_id token from the bridge_id.
        bridge_token = self._bridge_id or "no-bridge"
        meta: dict[str, Any] = {
            "source": "homunculus",
            "event_type": "post_message",
            "source_event_type": "post_message",
            "flow_id": f"bridge-reconnect-{bridge_token}",
            "cursor": "",
        }
        notification = JSONRPCNotification(
            jsonrpc="2.0",
            method=CLAUDE_CHANNEL_NOTIFICATION_METHOD,
            params={"content": content, "meta": meta},
        )
        message = SessionMessage(message=JSONRPCMessage(notification))
        try:
            await self._write_stream.send(message)
        except Exception as exc:  # noqa: BLE001
            _log(f"reconnect announcement send failed: {exc}")

    # ------------------------------------------------------------------
    # Internal: long-poll
    # ------------------------------------------------------------------

    async def _long_poll_loop(self) -> None:
        """Drain /events forever; emit each as an MCP notification."""
        consecutive_failures = 0
        next_reassert_at = self._monotonic_clock() + REGISTER_REASSERT_INTERVAL_S
        while self._poll_active:
            if self._bridge_id is None:
                await asyncio.sleep(0.5)
                continue
            try:
                bridge_id = self._bridge_id
                generation = self._generation
                await self._drain_once(bridge_id, generation)
                consecutive_failures = 0
                if self._monotonic_clock() >= next_reassert_at:
                    await self._reassert_identity()
                    next_reassert_at = (
                        self._monotonic_clock() + REGISTER_REASSERT_INTERVAL_S
                    )
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                consecutive_failures = await self._handle_poll_failure(
                    exc,
                    consecutive_failures,
                )

    async def _drain_once(self, bridge_id: str, generation: int | None = None) -> None:
        """Fetch one events page, dispatch each event, advance the cursor.

        The page-level ``next_cursor`` advance is the cursor authority here:
        when a role event is dedup-suppressed, it skips ``_emit_event`` (and its
        per-event cursor bump), so this page-level advance is what prevents the
        suppressed event from being re-fetched on the next poll.
        """
        poll_generation = self._generation if generation is None else generation
        events_payload = await self._fetch_events(bridge_id, self._cursor)
        if self._generation != poll_generation:
            _log(
                "discarding stale events page "
                f"(bridge={bridge_id}, generation={poll_generation}, "
                f"current_generation={self._generation})",
            )
            return
        events = events_payload.get("events") or []
        if isinstance(events, list):
            for event in events:
                if isinstance(event, dict):
                    await self._dispatch_incoming_event(event)
        next_cursor = events_payload.get("next_cursor")
        if isinstance(next_cursor, int):
            if self._generation != poll_generation:
                _log(
                    "skipping stale page cursor update "
                    f"(bridge={bridge_id}, generation={poll_generation}, "
                    f"current_generation={self._generation})",
                )
                return
            self._cursor = max(self._cursor, next_cursor - 1)

    async def _dispatch_incoming_event(self, event: dict[str, Any]) -> None:
        """Emit one event; route a live role delivery through the owed coordinator.

        A role ``peer_message`` (Control #5 meta keys present) is settled via
        :class:`OwedDeliveryCoordinator` — emitted at most once (dedup) with its
        ``delivered`` flag re-confirmed (M7). Only ROLE deliveries route here on
        the live path; a direct IMPORTANT send's original emission is recorded
        server-side, so its owed rows are handled by the coordinator's repair
        drain, not this live path. Every other event emits on the normal path.
        """
        role_keys = OwedDeliveryCoordinator.role_delivery_keys(event)
        if role_keys is None:
            await self._emit_event(event)
            return
        external_id, recipient_key = role_keys
        await self._owed_delivery.settle_live(
            event=event, external_id=external_id, recipient_key=recipient_key,
        )

    async def _handle_poll_failure(self, exc: BaseException, failures_so_far: int) -> int:
        """Decide whether to reconnect or just back off; return updated counter."""
        failures = failures_so_far + 1
        bridge_gone = _is_bridge_gone(exc)
        if bridge_gone or failures >= CONSECUTIVE_POLL_FAILURES_BEFORE_RECONNECT:
            reason = (
                "bridge lost during poll"
                if bridge_gone
                else f"{failures} consecutive poll failures"
            )
            _log(f"{reason}, reconnecting...")
            try:
                await self._reconnect()
                failures = 0
            except Exception as reconnect_exc:  # noqa: BLE001
                _log(f"reconnect failed during poll: {reconnect_exc}")
        else:
            _log(f"poll error: {exc}")
        await asyncio.sleep(POLL_RETRY_DELAY_S)
        return failures

    async def _fetch_events(self, bridge_id: str, after: int) -> dict[str, Any]:
        """GET /events with long-poll timeout; returns parsed JSON body."""
        path = f"/api/v1/bridge/{bridge_id}/events"
        params = {"after": str(after)}
        response = await self._client.get(
            path,
            params=params,
            timeout=EVENTS_POLL_TIMEOUT_S + 5.0,
        )
        return self._unwrap(response, path)

    async def _emit_event(
        self,
        event: dict[str, Any],
        generation: int | None = None,
    ) -> None:
        """Push one bridge event as a transport-specific MCP notification."""
        if self._write_stream is None:
            return
        emit_generation = self._generation if generation is None else generation
        source_event_type = str(event.get("event_type") or "post_message")
        content_raw = event.get("content")
        content = "" if content_raw is None else str(content_raw)
        if source_event_type != "post_message":
            # Preserve the original event_type as a discriminator the
            # consumer can string-match on, matching the Node bridge.
            content = f"[{source_event_type}] {content}"
        method = self._notification_method_for(source_event_type)
        meta = self._notification_meta_for(event, source_event_type, method)
        if method == SOLET_PEER_MESSAGE_NOTIFICATION_METHOD:
            content = self._solet_peer_message_content(
                content,
                source_event_type,
                meta,
            )
        notification = JSONRPCNotification(
            jsonrpc="2.0",
            method=method,
            params={"content": content, "meta": meta},
        )
        message = SessionMessage(message=JSONRPCMessage(notification))
        # The poll loop runs outside any request context; the captured
        # write stream is the same one ServerSession writes to.
        await self._write_stream.send(message)
        cursor = event.get("cursor")
        if isinstance(cursor, int):
            if self._generation != emit_generation:
                _log(
                    "skipping stale event cursor update "
                    f"(generation={emit_generation}, "
                    f"current_generation={self._generation})",
                )
                return
            self._cursor = max(self._cursor, cursor)

    def _notification_method_for(self, source_event_type: str) -> str:
        """Return the MCP notification method this bridge should emit."""
        if (
            self._agent_id == "codex"
            and source_event_type in CODEX_NATIVE_PEER_EVENT_TYPES
        ):
            return SOLET_PEER_MESSAGE_NOTIFICATION_METHOD
        return CLAUDE_CHANNEL_NOTIFICATION_METHOD

    def _notification_meta_for(
        self,
        event: dict[str, Any],
        source_event_type: str,
        method: str,
    ) -> dict[str, Any]:
        """Build wire meta for the selected notification method."""
        raw_meta = event.get("meta")
        event_meta: dict[str, Any] = raw_meta if isinstance(raw_meta, dict) else {}
        if method == SOLET_PEER_MESSAGE_NOTIFICATION_METHOD:
            return self._solet_peer_message_meta(event, source_event_type, event_meta)
        return self._claude_channel_meta(event, source_event_type, event_meta)

    def _claude_channel_meta(
        self,
        event: dict[str, Any],
        source_event_type: str,
        event_meta: dict[str, Any],
    ) -> dict[str, Any]:
        # Wire meta is restricted to the canonical 5-key shape Claude Code
        # accepts on `notifications/claude/channel` (matches the legacy
        # Node bridge byte-for-byte).  Extra keys (e.g. thread_id /
        # message_id from the wake adapter) get silently dropped by the
        # MCP client renderer — verified during Phase 4 cutover when our
        # initial 7-key meta caused every wake notification to vanish at
        # the client.  Bridge-internal metadata (thread_id, message_id)
        # lives in the channel content envelope itself if a receiver
        # needs it, not in the wire meta.
        #
        # ``flow_id`` lookup order: top-level event field (legacy shape)
        # → event.meta["flow_id"] (Python QueuedEvent puts it in meta) →
        # empty string.  Without the meta fallback every wake-adapter
        # event ships flow_id="" which Claude Code silently rejects.
        flow_id = self._flow_id_for(event, event_meta)
        wire_cursor = self._claude_wire_cursor
        self._claude_wire_cursor += 1
        return {
            "source": "homunculus",
            "event_type": "post_message",
            "source_event_type": source_event_type,
            "flow_id": flow_id,
            "cursor": str(wire_cursor),
        }

    def _solet_peer_message_meta(
        self,
        event: dict[str, Any],
        source_event_type: str,
        event_meta: dict[str, Any],
    ) -> dict[str, Any]:
        """Preserve full bridge metadata for Codex's native peer-message sink."""
        cursor = event.get("cursor", "")
        meta: dict[str, Any] = dict(event_meta)
        meta.update(
            {
                "source": "homunculus",
                "event_type": source_event_type,
                "source_event_type": source_event_type,
                "flow_id": self._flow_id_for(event, event_meta),
                "cursor": cursor,
                "bridge_cursor": cursor,
                "recipient_agent_id": event_meta.get("to_agent_id") or self._agent_id,
                "recipient_agent_instance_id": (
                    event_meta.get("to_agent_instance_id") or self._agent_instance_id
                ),
                "trigger_turn": True,
            },
        )
        return meta

    @staticmethod
    def _flow_id_for(event: dict[str, Any], event_meta: dict[str, Any]) -> str:
        flow_id_raw = event.get("flow_id")
        if flow_id_raw is None:
            flow_id_raw = event_meta.get("flow_id")
        return "" if flow_id_raw is None else str(flow_id_raw)

    @staticmethod
    def _solet_peer_message_content(
        content: str,
        source_event_type: str,
        meta: dict[str, Any],
    ) -> str:
        """Make Codex peer-message content readable without JSON parsing."""
        if source_event_type != "peer_message":
            return content
        prose = content.removeprefix("[peer_message] ").removeprefix("[peer_message]")
        sender_agent_id = str(meta.get("from_agent_id") or "unknown")
        sender_label = meta.get("from_session_label")
        sender_instance = meta.get("from_agent_instance_id")
        label_part = f' "{sender_label}"' if sender_label else ""
        instance_part = f" instance={sender_instance}" if sender_instance else ""
        return f"[peer:{sender_agent_id}{label_part}{instance_part}] {prose}"

    # ------------------------------------------------------------------
    # Internal: HTTP helpers
    # ------------------------------------------------------------------

    async def _post(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        response = await self._client.post(path, json=body)
        return self._unwrap(response, path)

    async def _get(self, path: str) -> dict[str, Any]:
        response = await self._client.get(path)
        return self._unwrap(response, path)

    @staticmethod
    def _unwrap(response: httpx.Response, path: str) -> dict[str, Any]:
        """Parse JSON body; raise BridgeHTTPError on non-2xx.

        On the error path, attaches ``status_code``, ``path``, and parsed
        response ``code`` so :func:`_is_bridge_gone` can require the
        affirmative caller-bridge-missing shape.
        """
        try:
            parsed = response.json() if response.content else {}
        except ValueError:
            parsed = {"raw": response.text}
        if not response.is_success:
            message = ""
            response_code: str | None = None
            if isinstance(parsed, dict):
                message = str(parsed.get("message") or "")
                code = parsed.get("code")
                if isinstance(code, str):
                    response_code = code
            if not message:
                message = response.text or response.reason_phrase
            msg = f"Solet {path} failed ({response.status_code}): {message}"
            raise BridgeHTTPError(
                msg,
                status_code=response.status_code,
                path=path,
                response_code=response_code,
            )
        if not isinstance(parsed, dict):
            return {"result": parsed}
        return parsed

    def _require_bridge(self) -> str:
        """Return current bridge_id or raise a clear error."""
        if self._bridge_id is None:
            msg = "Bridge not ready yet -- waiting for solet bridge API to come online."
            raise BridgeHTTPError(msg)
        return self._bridge_id

    def _bridge_path(self, suffix: str) -> str:
        """Build a bridge-scoped path from the current bridge id."""
        return f"/api/v1/bridge/{self._require_bridge()}{suffix}"

    async def _call_with_reconnect(
        self,
        op: str,
        coroutine_factory: Callable[[], Awaitable[dict[str, Any]]],
    ) -> dict[str, Any]:
        """Run an HTTP coroutine; reconnect once if the bridge is gone."""
        try:
            return await coroutine_factory()
        except BridgeHTTPError as exc:
            if not _is_bridge_gone(exc):
                raise
            _log(f"{op}: bridge gone, reconnecting and retrying once")
            await self._reconnect()
            return await coroutine_factory()

    # ------------------------------------------------------------------
    # OwedDeliveryTransport surface (v10 Control #5)
    #
    # The thin slice the OwedDeliveryCoordinator drives. Kept here (not in the
    # coordinator) because each method needs the bridge session's HTTP client /
    # cursor / write stream; the coordinator owns only the owed-delivery policy.
    # ------------------------------------------------------------------

    @property
    def bridge_ready(self) -> bool:
        """True once a bridge session is open (a ``bridge_id`` exists)."""
        return self._bridge_id is not None

    @property
    def running(self) -> bool:
        """True while the poll loop is active (cleared on ``close``)."""
        return self._poll_active

    async def emit_event(self, event: dict[str, Any]) -> None:
        """Emit one bridge event as an MCP notification (coordinator entry)."""
        await self._emit_event(event)

    async def drain_page(self, limit: int) -> dict[str, Any]:
        """POST ``/peer/drain``; return the full payload.

        ``{"undelivered": [...role rows], "re_emit_cap": N}``.
        """
        async def call() -> dict[str, Any]:
            return await self._post(self._bridge_path("/peer/drain"), {"limit": limit})

        return await self._call_with_reconnect("peer_drain", call)

    async def flip_delivered(self, *, external_id: str, recipient_key: str) -> None:
        """POST ``/peer/delivered`` to confirm ``delivered=true`` for a ROLE row."""
        async def call() -> dict[str, Any]:
            return await self._post(
                self._bridge_path("/peer/delivered"),
                {"external_id": external_id, "recipient_key": recipient_key},
            )

        await self._call_with_reconnect("peer_delivered", call)

    # ------------------------------------------------------------------
    # Tool surface — claude_code_channel half
    # ------------------------------------------------------------------

    async def current_identity(self) -> dict[str, Any]:
        async def call() -> dict[str, Any]:
            return await self._get(self._bridge_path("/current_identity"))

        payload = await self._call_with_reconnect("current_identity", call)
        self._cache_effective_agent_session_id(payload)
        payload.update(
            {
                "transport": "stdio",
                "solet_name": self._solet_name,
                "agent_session_id": self._agent_session_id,
                "mcp_session_id": "",
                "identity_trust": "stdio_bridge",
                "streamable_no_auth": False,
            },
        )
        return payload

    async def process_search(
        self,
        *,
        query: str,
        max_results: int = 10,
    ) -> dict[str, Any]:
        async def call() -> dict[str, Any]:
            return await self._post(
                self._bridge_path("/process/search"),
                {"query": query, "max_results": max_results},
            )

        return await self._call_with_reconnect("process_search", call)

    async def process_schema(self, *, process_key: str) -> dict[str, Any]:
        async def call() -> dict[str, Any]:
            return await self._post(
                self._bridge_path("/process/schema"),
                {"process_key": process_key},
            )

        return await self._call_with_reconnect("process_schema", call)

    async def process_call(
        self,
        *,
        process_key: str,
        arguments: dict[str, Any],
        reason: str | None = None,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {
            "process_key": process_key,
            "arguments": arguments,
        }
        if reason is not None:
            body["reason"] = reason

        async def call() -> dict[str, Any]:
            return await self._post(
                self._bridge_path("/process/call"),
                body,
            )

        return await self._call_with_reconnect("process_call", call)

    async def process_result(self, *, action_id: str) -> dict[str, Any]:
        async def call() -> dict[str, Any]:
            path = self._bridge_path(
                f"/process/result/{quote(action_id, safe='')}",
            )
            return await self._get(path)

        return await self._call_with_reconnect("process_result", call)

    async def download(self, *, blob_id: str, output_path: str) -> dict[str, Any]:
        """Stream a blob to a local file; mirrors the Node bridge's shape."""
        async def call() -> dict[str, Any]:
            path = self._bridge_path(f"/download/{quote(blob_id, safe='')}")
            response = await self._client.get(
                path,
                timeout=DEFAULT_REQUEST_TIMEOUT_S * 4,
            )
            if not response.is_success:
                msg = f"Download failed ({response.status_code}): {response.text}"
                raise BridgeHTTPError(
                    msg,
                    status_code=response.status_code,
                    path=path,
                )
            body = response.content
            # Write to disk synchronously — the file is bounded in size and
            # the bridge subprocess has no other concurrent work that
            # benefits from offloading this to a thread.
            target = Path(output_path)
            target.write_bytes(body)
            filename = blob_id
            disposition = response.headers.get("content-disposition", "")
            if "filename=" in disposition:
                raw = disposition.split("filename=", 1)[1].strip()
                filename = raw.strip('";')
            return {
                "status": "downloaded",
                "filename": filename,
                "size": len(body),
                "path": output_path,
            }

        return await self._call_with_reconnect("download", call)

    # ------------------------------------------------------------------
    # Tool surface — agent_channel half
    # ------------------------------------------------------------------

    async def peer_register(
        self,
        *,
        agent_id: str,
        session_label: str | None = None,
    ) -> dict[str, Any]:
        body = {
            "agent_id": agent_id,
            "agent_instance_id": self._agent_instance_id,
            "agent_session_id": self._agent_session_id,
            "session_label": session_label or self._session_label,
            "parent_pid": self._parent_pid,
            "provides_inference": self._provides_inference,
            "wake_capable": self._wake_capable,
        }

        async def call() -> dict[str, Any]:
            payload = await self._post(self._bridge_path("/peer/register"), body)
            self._cache_effective_agent_session_id(payload)
            return payload

        return await self._call_with_reconnect("peer_register", call)

    async def peer_list(self) -> dict[str, Any]:
        async def call() -> dict[str, Any]:
            return await self._get(self._bridge_path("/peer/list"))

        return await self._call_with_reconnect("peer_list", call)

    async def peer_send(
        self,
        *,
        peer_id: str,
        content: list[dict[str, Any]],
        peer_agent_instance_id: str | None = None,
        peer_agent_session_id: str | None = None,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {"peer_id": peer_id, "content": content}
        if peer_agent_instance_id is not None:
            body["peer_agent_instance_id"] = peer_agent_instance_id
        if peer_agent_session_id is not None:
            body["peer_agent_session_id"] = peer_agent_session_id

        async def call() -> dict[str, Any]:
            return await self._post(self._bridge_path("/peer/send"), body)

        return await self._call_with_reconnect("peer_send", call)

    async def peer_send_by_name(self, *, name: str, content: str) -> dict[str, Any]:
        body = {"name": name, "content": content}

        async def call() -> dict[str, Any]:
            return await self._post(self._bridge_path("/peer/send_by_name"), body)

        return await self._call_with_reconnect("peer_send_by_name", call)

    async def peer_inbox(
        self,
        *,
        after: str | None = None,
        limit: int | None = None,
        include_important: bool = True,
        role_after: str | None = None,
        observer: bool = False,
    ) -> dict[str, Any]:
        params: dict[str, str] = {}
        if after is not None:
            params["after"] = after
        if limit is not None:
            params["limit"] = str(limit)
        if not include_important:
            params["include_important"] = "false"
        if role_after is not None:
            params["role_after"] = role_after
        if observer:
            params["observer"] = "true"
        query = f"?{urlencode(params)}" if params else ""

        async def call() -> dict[str, Any]:
            return await self._get(self._bridge_path(f"/peer/inbox{query}"))

        return await self._call_with_reconnect("peer_inbox", call)
