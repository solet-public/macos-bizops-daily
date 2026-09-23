"""Bridge session + event-queue runtime state for ``agent_messaging_plugin``.

A bridge is the durable handle an MCP client holds while it owns one or
more agent threads.  The bridge id appears in every URL and is the
authoritative ownership token; the platform session id is minted at
bridge open and persisted on every thread row so the bridge-delivery
contract validator finds the same session id on the action side.

Ported from the now-deleted ``agent_channel_plugin/models.py`` (2026-05-16)
during the bridge-consolidation work — see
``workbench/2026-05-16_codex_mcp_channel_and_inter_agent_outstanding_work.md``.
"""

from __future__ import annotations

import threading
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Final, Protocol

# Instance-id prefix minted by the no-MCP `<name> watch` client (registered-
# presence receive). A binding whose ``agent_instance_id`` carries this prefix
# is held by a WATCHER subprocess — a pull recipient that long-polls ``/events``
# and streams into a background task's output, never running model turns. The
# prefix is the single watcher discriminator the server reads (delivery-status
# labelling + events-ack consumption); the watch CLI imports it from here so
# client and server can never drift.
WATCH_AGENT_INSTANCE_PREFIX: Final[str] = "agi-watch-"

# iss_c93a9b2f: a bridge queue can hold up to ``max_pending_events`` (200)
# messages before a sender gets BridgeQueueFullError, but nothing previously
# bounded how many of those ``events_after`` hands back IN ONE CALL — a role
# holder that fell behind could have its full backlog (up to 200) dispatched
# to the MCP client back-to-back in a single long-poll response, which
# contributed to killing a session under real fleet load. This is a page-size
# bound on ONE call, not a change to total eventual delivery: anything past
# the limit stays in ``pending_events`` (unacked) and is returned on the very
# next poll, since the long-poll loop re-calls immediately on success.
EVENTS_AFTER_PAGE_LIMIT: Final[int] = 25


class NativeWakeAdapter(Protocol):
    """Protocol for plugins that surface a native MCP wake channel.

    The default ``peer_send`` delivery path enqueues a
    ``notifications/claude/channel`` event on the recipient's
    bridge.  Some MCP client transports do not auto-surface those
    notifications between turns and need an alternate "chat-style"
    wake path.  Plugins that own such an alternate path implement
    this Protocol and register themselves via
    ``AgentMessagingPlugin.register_native_wake_adapter``.

    With this plugin's consolidation (one plugin owning both the
    agent-to-agent surface and the Claude-Code IO surface),
    self-registration is the common case: the plugin registers an
    adapter for ``agent_id == "claude_code"`` that targets its own
    IO ``post_message`` path.  External plugins can still register
    adapters for other agent_ids if a future transport needs one.

    Native wake delivers via plain text (no ``meta`` field), so
    implementations MUST embed ``sender_agent_instance_id`` in the
    envelope so the receiver can construct a targeted reply.
    ``recipient_parent_pid`` lets the adapter pair with the matching
    sibling bridge inside the same OS process tree.
    """

    def wake(
        self,
        *,
        recipient_parent_pid: int | None,
        delivered_prose: str,
        sender_agent_id: str,
        sender_agent_instance_id: str,
        sender_session_label: str,
        thread_id: str,
        message_id: str,
        reply_to_role: str = "",
        sender_agent_session_id: str = "",
        delivery_meta: Mapping[str, object] | None = None,
    ) -> str:
        """Push prose into the agent's native MCP surface.

        Returns the receiver-side bridge id (for inclusion in the
        peer_send response's ``delivered_to_bridge_id`` field).

        ``reply_to_role`` (REL-01 Fork 4): the sender's DURABLE role for a
        role-addressed (``peer_send_by_name``) send. When non-empty the adapter
        MUST surface a role reply-to hint (``peer_send_by_name name=<role>``) so
        the return leg survives a holder reconnect. Empty for a direct instance
        ``peer_send`` (which keeps the same-connection instance reply-to).

        ``sender_agent_session_id`` (WS-2c V4 / A2): the sender's STABLE
        per-logical-session key, resolved server-side from the sender's registered
        binding. Implementations surface it ALONGSIDE the instance id — never
        instead of it — so a reply still resolves after the sender's instance has
        rotated, while a receiver that only knows the old field keeps working.
        Empty when the sender has no registered binding; adapters MUST then omit
        it rather than emit an unresolvable key.

        ``delivery_meta`` (v10 Control #5 / Q3-revised): extra bridge-event meta
        the adapter MUST merge onto the wake event. For a role-addressed send it
        carries the Control #5 role keys (``recipient_kind`` / ``recipient_key``
        / ``delivery_external_id``) so the holder's forwarder recognises the role
        delivery on ``/events`` and confirms it (``/peer/delivered``) — the wake
        is the SAME bridge queue as the queued_notification path, NOT a direct
        push, so the forwarder is the sole delivered-authority for both.
        ``None`` for a plain instance peer_send.

        Raises if the adapter cannot deliver — the loop-prevention
        contract treats IMPORTANT as a hard delivery promise, so
        silent drops are not acceptable.
        """
        ...


@dataclass(frozen=True, slots=True)
class BridgeBinding:
    """One entry in the per-agent_id peer registry.

    Multiple instances of the same ``agent_id`` (e.g., several
    concurrent Claude Code sessions) each register their own
    ``BridgeBinding``.  ``agent_instance_id`` is the durable routing
    key — the bridge subprocess generates it at startup and reuses
    it across reconnects, so the registry replaces (not duplicates)
    when the same instance re-registers with a new ``bridge_id``.

    Timestamps come from the backing :class:`Store`.  ``created_at``
    replaces the prior ``registered_at`` field (one-release deprecated
    alias still surfaced in ``peer_list`` responses).  ``updated_at``
    is bumped by every dispatch operation (``peer_send``,
    ``peer_inbox``, native wake) so it carries "last active"
    semantics that fall out of the canonical platform timestamp
    convention.  Construction-time bindings (those passed INTO
    ``PeerRegistry.register``) omit both timestamps; the store fills
    them on insert and the registry's read paths return rebuilt
    bindings carrying the persisted values.
    """

    bridge_id: str
    agent_id: str
    agent_instance_id: str
    session_label: str
    parent_pid: int | None
    created_at: str = ""
    updated_at: str = ""
    # S1 (agent_session_id splice): the STABLE per-logical-session key — survives
    # bridge reconnect / agent_instance_id rotation. Drives the reconnect
    # state-table self-refresh (peer_register → refresh_role_binding_cas) and
    # surfaces via current_identity. Empty for sessions launched without
    # AGENT_SESSION_ID exported (streamable / older clients) -> no self-refresh.
    agent_session_id: str = ""
    # codex-watch-migration wake_capable design (2026-08-06): DECLARED, never
    # probed, at registration time by the bridge subprocess itself
    # (mcp_bridge/__main__.py's _run(), same shape as provides_inference) —
    # true when this binding's transport has a native turn-injection wake
    # path (Claude Code's registered wake adapter), false when it does not
    # (stock Codex's bridge, which has no equivalent surface once the patched
    # build retires). Default true: every existing/non-declaring registration
    # path — Claude Code, GC, seat, every watcher binding above all — reads
    # as the common case.
    #
    # codex-0147-dead-spool-retirement (2026-08-13): this field is now
    # PERSISTED COMPATIBILITY METADATA only — the dispatch-side spool tee
    # that once read it (peer_dispatch.py's former
    # ``_tee_spool_if_wake_incapable``) is retired, since stock Codex's Stop
    # hook cannot consume it (async command hooks do not execute on stock
    # Codex — codex-0147-async-hook-regression, 2026-08-13). A Codex
    # recipient still reaches its delivery through the durable inbox /
    # watch-transport read path and, for a spawn_session-managed worker,
    # drive_on_delivery's driver-channel nudge — both independent of this
    # flag's value. Kept on the schema/dataclass so a future native wake
    # path can flip it without a migration; nothing currently branches on it
    # at dispatch time.
    wake_capable: bool = True
    # MSG-04/identity-unification (2026-08-20): DECLARED, never probed, by
    # `solet-bridge watch` on every peer/register call — the one caller that KNOWS
    # it is a watcher regardless of what `agent_instance_id` it registers
    # under. Needed because that fix stops deriving `agent_instance_id` from
    # `agent_session_id` (the `agi-watch-{digest}` scheme) in favor of the
    # caller's own ledger `AGENT_INSTANCE_ID` when one exists — so the prefix
    # this class used to infer "is a watcher" from is no longer reliably
    # present on exactly the bindings that need the label. Default False:
    # every non-watch registration path (Claude Code, GC, seat) is unaffected.
    watcher_declared: bool = False

    @property
    def is_watcher(self) -> bool:
        """True when this binding is held by a no-MCP ``watch`` subprocess.

        A watcher is a PULL recipient: IMPORTANT deliveries are queued on its
        bridge and surface when its long-poll streams them — no model turn
        starts. Dispatch labels such deliveries ``queued_watcher`` and the
        events route treats the watcher's long-poll ack as consumption.

        Checks the explicit ``watcher_declared`` flag FIRST, then falls back
        to the legacy ``agi-watch-`` prefix convention — the OR keeps a
        manual/no-ledger-id watch (which still mints the derived, prefixed
        identity) detected exactly as before, while a ledger-identified
        watch binding (which carries no such prefix) is now detected via the
        flag instead of an identity convention it no longer follows.
        """
        return self.watcher_declared or self.agent_instance_id.startswith(
            WATCH_AGENT_INSTANCE_PREFIX,
        )


@dataclass(slots=True)
class QueuedEvent:
    """One outbound channel event waiting for bridge consumption.

    ``content`` is plain English prose — the same text a human would
    read in a chat surface.  Structured fields (thread_id, sender,
    payload, etc.) live in ``meta`` so the MCP-side notification
    renderer can surface ``content`` directly without parsing.
    """

    cursor: int
    event_type: str
    content: str
    meta: dict[str, object] = field(default_factory=dict)
    created_at: str = field(
        default_factory=lambda: datetime.now(UTC).isoformat(),
    )


@dataclass(slots=True)
class BridgeSessionState:
    """In-memory state for one active bridge.

    The event queue is mutated from at least two threads:

    * the action queue thread (calls ``append_event`` from
      ``deliver_result`` / ``deliver_error`` and from ``post_message``)
    * the FastAPI event loop thread (calls ``events_after`` while
      long-polling)

    A per-bridge lock around every queue mutation keeps appends and
    drains atomic — without it, an append concurrent with a drain can
    lose the event because ``events_after`` rebinds ``pending_events``
    via list comprehension.
    """

    bridge_id: str
    session_id: str
    # OS PID of the MCP host that spawned the bridge subprocess (e.g.
    # this Claude Code session, or this Codex session).  Live-routing
    # metadata only — used to pair sibling bridges inside the same OS
    # process tree.  Never persisted as identity.
    parent_pid: int | None = None
    # Durable per-bridge UUID generated by the bridge subprocess at
    # startup ("agi-<uuid>").  This is the routing key for
    # multi-instance addressing, persisted on peer threads, and
    # carried in peer_message notification meta + native-wake
    # envelopes so receivers can construct targeted replies.
    agent_instance_id: str | None = None
    # Mutable, non-unique human metadata supplied by the bridge at
    # registration ("codex on baroque-suite").  Never used as a
    # routing key — only for human-facing display in peer_list,
    # peer_message meta, and native-wake envelopes.
    session_label: str = ""
    # §34.6: the launcher-exported ``ases-...`` session key asserted by an
    # UNREGISTERED caller (the local CLI's one-shot bridge) purely so its sends
    # can be attributed. ATTRIBUTION ONLY — this is never a routing key, never
    # a registration, and never persisted as identity: the server treats it as
    # a lookup key into the peer registry and reads the identity out of the
    # REGISTERED binding, so an unresolvable key degrades to the system
    # sentinel rather than promoting an unverifiable claim.
    caller_agent_session_id: str = ""
    # M5 §14.4: OAuth client_id that opened this bridge. Empty string
    # for legacy stdio bridges that never carried a bearer. Set at
    # bridge-establishment time from the validated BearerClaim.
    client_id: str = ""
    # M5 §14.4: per-session allowlist of process_keys this bridge may
    # invoke via process_call / process_search / process_schema.
    # Default is EMPTY_ALLOWLIST (fail-closed) — a bridge with no
    # resolved policy can call nothing. Populated by
    # BridgeSessionManager._resolve_session_policy at open time.
    process_export_allowlist: tuple[str, ...] = ()
    created_at: str = field(
        default_factory=lambda: datetime.now(UTC).isoformat(),
    )
    last_seen_at: str = field(
        default_factory=lambda: datetime.now(UTC).isoformat(),
    )
    # REL-05: the last time this bridge invoked a MODEL-INITIATED route
    # (peer/send, process/*, agent/*, ... — NEVER a forwarder/infra
    # route: open, events, drain, delivered, register [F1], close, health). The
    # consumption reconciler reads it to decide whether an owed IMPORTANT send
    # entered a turn context. Distinct from ``last_seen_at`` (which every drain
    # long-poll bumps); a deaf session's forwarder keeps ``last_seen_at`` fresh
    # while this stays stale — that is exactly the Vector-B discriminator. Empty
    # until the first model-initiated route.
    last_model_activity_at: str = ""
    # REL-05 QUIET-GAP: the model-activity stamp IMMEDIATELY PRECEDING
    # ``last_model_activity_at``. The pair bounds the quiet gap the consumption
    # reconciler needs: an emission proves it was surfaced only when it landed in
    # a gap long enough to be a TURN BOUNDARY. Activity alone cannot discriminate
    # "a new turn started on this wake" from "a turn already in flight made its
    # next call" — and the latter marked every wake to a BUSY session consumed on
    # its first emit, which is the silent-loss class this pair closes.
    prev_model_activity_at: str = ""
    closed: bool = False
    next_event_id: int = 0
    pending_events: list[QueuedEvent] = field(default_factory=list)
    _events_lock: threading.Lock = field(default_factory=threading.Lock)

    def touch(self) -> None:
        self.last_seen_at = datetime.now(UTC).isoformat()

    def stamp_model_activity(self) -> str:
        """Record that a MODEL-INITIATED route just fired; return the timestamp.

        Called ONLY from the route-activity middleware for routes classified as
        model-initiated (never forwarder/infra). The returned ISO timestamp is
        mirrored to the durable ``peer_binding`` row so the server-side sweep can
        read it without the live session.
        """
        stamp = datetime.now(UTC).isoformat()
        self.prev_model_activity_at = self.last_model_activity_at
        self.last_model_activity_at = stamp
        return stamp

    def append_event(
        self,
        event_type: str,
        content: str,
        meta: dict[str, object] | None = None,
    ) -> QueuedEvent:
        with self._events_lock:
            event = QueuedEvent(
                cursor=self.next_event_id,
                event_type=event_type,
                content=content,
                meta=dict(meta) if meta else {},
            )
            self.pending_events.append(event)
            self.next_event_id += 1
            return event

    def events_after(
        self, after: int, *, limit: int | None = EVENTS_AFTER_PAGE_LIMIT,
    ) -> tuple[list[QueuedEvent], list[QueuedEvent]]:
        """Return ``(acked, pending)`` for a client cursor.

        ``limit`` bounds only what is RETURNED as ``pending`` this call —
        ``self.pending_events`` still ends this call holding every row with
        ``cursor > after`` (unchanged from before this parameter existed), so
        a row past the limit stays legitimately un-acked and is handed back
        on the caller's next poll (oldest-first, since ``pending_events`` is
        append-ordered and never reordered). ``None`` restores the old
        unbounded behavior for callers that need it (none currently do; kept
        so this is additive, not a breaking signature change).

        ``acked`` are the events the client's ``after`` cursor acknowledges
        (cursor <= after) — they were returned by an earlier call and the
        client has provably received them, so they are drained here exactly
        once. The events route reads ``acked`` as the watcher consumption
        signal (a pull recipient acking a wake event HAS surfaced it).
        ``pending`` are the still-undelivered events (cursor > after).
        """
        with self._events_lock:
            acked = [e for e in self.pending_events if e.cursor <= after]
            self.pending_events = [
                e for e in self.pending_events if e.cursor > after
            ]
            pending = list(self.pending_events)
            if limit is not None:
                pending = pending[:limit]
            return acked, pending

    def pending_event_count(self) -> int:
        with self._events_lock:
            return len(self.pending_events)


__all__ = [
    "EVENTS_AFTER_PAGE_LIMIT",
    "WATCH_AGENT_INSTANCE_PREFIX",
    "BridgeBinding",
    "BridgeSessionState",
    "NativeWakeAdapter",
    "QueuedEvent",
]
