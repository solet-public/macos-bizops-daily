"""In-memory bridge session registry + long-poll event-queue manager.

Ported from ``agent_channel_plugin.plugin`` during the bridge
consolidation work — see
``workbench/2026-05-16_codex_mcp_channel_and_inter_agent_outstanding_work.md``
Phase 2c for the pickup contract.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import uuid
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Final

from .models import EVENTS_AFTER_PAGE_LIMIT, BridgeSessionState, QueuedEvent

if TYPE_CHECKING:
    from collections.abc import Callable

    from .mcp_streamable.auth import BearerClaim

logger = logging.getLogger(__name__)


# The ONE literal for the binding-liveness window (WS-2a W3 / WS-2e §4.3.2).
# Both transports long-poll continuously, so a live session's bridge is touched
# every ~25-30s; 90 is >3x that worst case and far under the 3_600s idle sweep,
# which makes staleness a clean discriminator rather than a heuristic.
DEFAULT_BINDING_LIVENESS_WINDOW_S: Final[int] = 90


BRIDGE_ID_PREFIX: Final[str] = "agc-"
_BRIDGE_ID_HEX_LEN: Final[int] = 12


# M5 §14.4 — per-session policy sentinels.
EMPTY_ALLOWLIST: tuple[str, ...] = ()
SHIPPER_ALLOWLIST: tuple[str, ...] = (
    "service_interface::session_ledger_service::ingest_raw_chunk",
    "service_interface::session_ledger_service::ingest_blob",
    "service_interface::session_ledger_service::shipper_self_revoke",
)
MANAGEMENT_ALLOWLIST: tuple[str, ...] = (
    "service_interface::knowledge_service::search",
    # Doc-authoring lifecycle for management clients (operator ruling
    # 2026-07-17: ChatGPT authors/reviews workbench docs). delete_file is
    # deliberately absent — archive_file is the sanctioned retire path.
    "service_interface::knowledge_service::browse",
    "service_interface::knowledge_service::read_file",
    "service_interface::knowledge_service::create_file",
    "service_interface::knowledge_service::edit_file",
    "service_interface::knowledge_service::archive_file",
    "service_interface::session_ledger_service::search_sessions",
    "service_interface::session_ledger_service::search_event_content",
    "service_interface::session_ledger_service::list_sessions",
    "service_interface::session_ledger_service::list_active_sessions",
    "service_interface::session_ledger_service::list_events_by_source_window",
    "service_interface::session_ledger_service::get_session_timeline",
    "service_interface::session_ledger_service::list_tool_calls",
    "service_interface::session_ledger_service::list_canonical_contributors",
    "service_interface::vault_service::oauth_client_list",
    "plugin::agent_messaging_plugin::peer_send_by_name",
)
# Distinct identity from EMPTY_ALLOWLIST so PlatformSurface can recognise
# "unrestricted" via ``policy is _UNRESTRICTED`` and skip the membership
# check. The placeholder string never matches any real process_key.
_UNRESTRICTED: tuple[str, ...] = ("__operator_equivalent_unrestricted__",)


class BridgeNotFoundError(LookupError):
    """Raised when a caller references a bridge_id that is not registered."""


class BridgeQueueFullError(RuntimeError):
    """Raised when a bridge's event queue has reached its capacity."""


SessionIdFactory = "Callable[[str], str]"


class BridgeSessionManager:
    """Owns the in-memory registry of live bridges + their event queues.

    Thread model: registry mutations come from both the FastAPI event
    loop (bridge open/close, long-poll drain) and the platform action
    queue (event append from ``deliver_result`` / ``deliver_error`` /
    ``post_message``).  A single mutex guards the registry dict; per-
    bridge event-queue locks live inside ``BridgeSessionState``.

    Long-poll wakeup uses one ``asyncio.Event`` per bridge.  The loop
    that owns the event is captured the first time ``events_after``
    runs against the bridge, so cross-thread ``append_event`` calls can
    schedule the wakeup via ``loop.call_soon_threadsafe``.
    """

    def __init__(
        self,
        *,
        session_id_factory: Callable[[str], str],
        idle_timeout_s: int,
        max_pending_events: int,
        long_poll_timeout_s: int,
        binding_liveness_window_s: int = DEFAULT_BINDING_LIVENESS_WINDOW_S,
        policy_resolver: Callable[[BearerClaim], tuple[str, ...]] | None = None,
    ) -> None:
        self._session_id_factory = session_id_factory
        self._idle_timeout_s = idle_timeout_s
        self._max_pending_events = max_pending_events
        self._long_poll_timeout_s = long_poll_timeout_s
        # WS-2a W3 / WS-2e §4.3.2. Lives HERE rather than being threaded through
        # every dispatch call site: the manager is already constructed from
        # config and already passed to all five of them, so one wiring point
        # replaces five chances to silently drop the keyword and leave the knob
        # inert at that site.
        self._binding_liveness_window_s = binding_liveness_window_s
        self._bridges: dict[str, BridgeSessionState] = {}
        self._registry_lock = threading.Lock()
        # Per-bridge wakeup primitives: each entry is the asyncio.Event
        # the long-poll waits on plus the loop that owns it (needed for
        # cross-thread set scheduling from the action-queue thread).
        self._wakeups: dict[
            str, tuple[asyncio.Event, asyncio.AbstractEventLoop]
        ] = {}
        self._wakeups_lock = threading.Lock()
        # M5 §14.4: pluggable policy resolver. Production wires a
        # vault-backed resolver that distinguishes operator-equivalent,
        # paired-shipper, and fail-closed cases. None = fail-closed
        # default (EMPTY_ALLOWLIST) — used by stdio bridges and tests
        # that don't need bearer-driven policy.
        self._policy_resolver = policy_resolver

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def open(
        self,
        solet_name: str,
        parent_pid: int | None = None,
    ) -> BridgeSessionState:
        bridge_id = BRIDGE_ID_PREFIX + uuid.uuid4().hex[:_BRIDGE_ID_HEX_LEN]
        session_id = self._session_id_factory(solet_name)
        bridge = BridgeSessionState(
            bridge_id=bridge_id,
            session_id=session_id,
            parent_pid=parent_pid,
        )
        with self._registry_lock:
            self._bridges[bridge_id] = bridge
        logger.info(
            "bridge opened: %s -> session %s (parent_pid=%s)",
            bridge_id, session_id, parent_pid,
        )
        return bridge

    def open_bridge(
        self,
        claim: BearerClaim,
        parent_pid: int | None = None,
    ) -> BridgeSessionState:
        """M5 §14.4: open a bridge bound to an authenticated bearer claim.

        Sets ``client_id`` + ``process_export_allowlist`` on the new
        bridge from the resolved session policy. The legacy ``open()``
        method stays available for stdio bridges that have no claim
        (they default to ``client_id=""`` and ``allowlist=()`` — pure
        bridge identity, no policy enforcement).
        """
        bridge = self.open(solet_name="", parent_pid=parent_pid)
        bridge.agent_instance_id = claim.agent_instance_id
        bridge.session_label = claim.session_label
        bridge.client_id = claim.client_id
        bridge.process_export_allowlist = self._resolve_session_policy(claim)
        logger.info(
            "bridge bound to OAuth client: bridge_id=%s client_id=%s "
            "allowlist=%s",
            bridge.bridge_id,
            claim.client_id,
            "_UNRESTRICTED"
            if bridge.process_export_allowlist is _UNRESTRICTED
            else list(bridge.process_export_allowlist),
        )
        return bridge

    def _resolve_session_policy(self, claim: BearerClaim) -> tuple[str, ...]:
        """Resolve the per-session export allowlist for ``claim``.

        Delegates to the injected ``policy_resolver`` if present.
        Defaults to ``EMPTY_ALLOWLIST`` (fail-closed) when no resolver
        is wired — every ``process_call`` / ``process_search`` /
        ``process_schema`` against such a session is rejected.
        """
        if self._policy_resolver is None:
            return EMPTY_ALLOWLIST
        return self._policy_resolver(claim)

    def close(self, bridge_id: str) -> bool:
        with self._registry_lock:
            bridge = self._bridges.pop(bridge_id, None)
        if bridge is None:
            return False
        bridge.closed = True
        # Wake any in-flight long-poll so the handler can observe the
        # close and return promptly instead of waiting out the timeout.
        self._signal_wakeup(bridge_id)
        with self._wakeups_lock:
            self._wakeups.pop(bridge_id, None)
        logger.info("bridge closed: %s", bridge_id)
        return True

    @property
    def binding_liveness_window_s(self) -> int:
        """Seconds a bridge may go unpolled before its bindings read as dead."""
        return self._binding_liveness_window_s

    def get(self, bridge_id: str) -> BridgeSessionState | None:
        with self._registry_lock:
            return self._bridges.get(bridge_id)

    def touch(self, bridge_id: str) -> None:
        bridge = self.get(bridge_id)
        if bridge is None:
            raise BridgeNotFoundError(bridge_id)
        bridge.touch()

    def list_active(self) -> list[BridgeSessionState]:
        with self._registry_lock:
            return [b for b in self._bridges.values() if not b.closed]

    # ------------------------------------------------------------------
    # Idle sweep
    # ------------------------------------------------------------------

    def sweep_idle(
        self,
        *,
        now: datetime | None = None,
        idle_timeout_s: int | None = None,
    ) -> list[str]:
        cutoff = now or datetime.now(UTC)
        threshold = idle_timeout_s if idle_timeout_s is not None else self._idle_timeout_s
        expired: list[str] = []
        with self._registry_lock:
            for bridge_id, bridge in self._bridges.items():
                if bridge.closed:
                    continue
                last_seen = datetime.fromisoformat(bridge.last_seen_at)
                if (cutoff - last_seen).total_seconds() > threshold:
                    expired.append(bridge_id)
            for bridge_id in expired:
                bridge = self._bridges.pop(bridge_id)
                bridge.closed = True
        for bridge_id in expired:
            self._signal_wakeup(bridge_id)
            with self._wakeups_lock:
                self._wakeups.pop(bridge_id, None)
            logger.info("expired idle bridge %s", bridge_id)
        return expired

    # ------------------------------------------------------------------
    # Event-queue operations
    # ------------------------------------------------------------------

    def append_event(
        self,
        bridge_id: str,
        event_type: str,
        content: str,
        meta: dict[str, object] | None = None,
    ) -> QueuedEvent:
        bridge = self.get(bridge_id)
        if bridge is None:
            raise BridgeNotFoundError(bridge_id)
        if bridge.pending_event_count() >= self._max_pending_events:
            raise BridgeQueueFullError(bridge_id)
        event = bridge.append_event(event_type, content, meta)
        self._signal_wakeup(bridge_id)
        return event

    async def events_after(
        self,
        bridge_id: str,
        after_cursor: int,
        timeout_s: float | None = None,
    ) -> tuple[list[QueuedEvent], list[QueuedEvent]]:
        """Long-poll one bridge queue; return ``(acked, pending)``.

        ``acked`` are the events the caller's cursor acknowledges — drained
        exactly once by the first state read (the post-wakeup re-read drains
        nothing new for the same cursor). The events route feeds ``acked`` to
        the watcher consumption reconcile; MCP-transport pumps ignore it.
        """
        bridge = self.get(bridge_id)
        if bridge is None:
            raise BridgeNotFoundError(bridge_id)
        bridge.touch()
        # Critical ordering: clear the wakeup FIRST, then check for events.
        # If we checked first and cleared after, an ``append_event`` race
        # between the check and the clear would wipe the signal — wait()
        # would then sit until timeout while the event sits unread in the
        # queue.  This was the root cause of "peer_send delivered to bridge
        # but no notifications/claude/channel surfaced" during Phase 4
        # cutover verification (2026-05-16).  With clear-then-check, any
        # append after our clear either lands in the post-clear bridge
        # snapshot (returned immediately) or sets the wakeup before our
        # wait() starts (returns immediately).
        wakeup = self._ensure_wakeup(bridge_id)
        wakeup.clear()
        acked, events = bridge.events_after(after_cursor, limit=EVENTS_AFTER_PAGE_LIMIT)
        if events:
            return acked, events
        deadline = timeout_s if timeout_s is not None else self._long_poll_timeout_s
        try:
            await asyncio.wait_for(wakeup.wait(), timeout=deadline)
        except TimeoutError:
            return acked, []
        # Wakeup fired — either a new event landed or the bridge closed.
        if bridge.closed:
            return acked, []
        _, late_events = bridge.events_after(after_cursor, limit=EVENTS_AFTER_PAGE_LIMIT)
        return acked, late_events

    # ------------------------------------------------------------------
    # Wakeup plumbing
    # ------------------------------------------------------------------

    def _ensure_wakeup(self, bridge_id: str) -> asyncio.Event:
        loop = asyncio.get_running_loop()
        with self._wakeups_lock:
            existing = self._wakeups.get(bridge_id)
            if existing is not None and existing[1] is loop:
                return existing[0]
            event = asyncio.Event()
            self._wakeups[bridge_id] = (event, loop)
            return event

    def _signal_wakeup(self, bridge_id: str) -> None:
        with self._wakeups_lock:
            entry = self._wakeups.get(bridge_id)
        if entry is None:
            return
        event, loop = entry
        # Always go through call_soon_threadsafe so behaviour is
        # identical whether the append happens on the FastAPI loop or
        # on the action-queue worker thread.
        loop.call_soon_threadsafe(event.set)


__all__ = [
    "BRIDGE_ID_PREFIX",
    "BridgeNotFoundError",
    "BridgeQueueFullError",
    "BridgeSessionManager",
    "MANAGEMENT_ALLOWLIST",
]
