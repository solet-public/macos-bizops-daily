"""Transport-neutral peer_send dispatch core.

Both the legacy bridge HTTP route (``/api/v1/bridge/{id}/peer/send``)
and the Streamable HTTP MCP transport's ``tools/call peer_send``
funnel through the **same** dispatch logic:

* resolve the recipient ``BridgeBinding`` via ``PeerRegistry.resolve``
  (raises :class:`PeerAmbiguousError` / :class:`PeerUnreachableError`),
* persist the message via ``agent_messaging_service.peer_send``,
* touch the sender bridge + both bindings so ``updated_at`` reflects
  the dispatch,
* route through the recipient's native wake adapter if registered,
  else append a ``peer_message`` channel event on the recipient's
  bridge,
* drive-on-delivery (2026-08-04): ALONGSIDE the step above, a best-effort
  extra nudge through the recipient's ``managed_session`` driver channel
  when one is live and eligible — see
  :func:`agent_messaging_plugin.session_lifecycle_verbs.drive_on_delivery`.

Wake is a TRANSPORT property, not a sender-declared one (A4, 2026-08-04):
every send takes this path, unconditionally — the IMPORTANT marker is
matched and stripped as input hygiene for old-habit prose, but no longer
gates anything (this was ruled NONE; see
``workbench/2026-08-04_marker_retirement_architect_memo_architect.md``
before proposing a wake toggle here).

The shape of this logic is protocol semantics, not HTTP / JSON-RPC
plumbing — both call sites used to duplicate it.  This module is the
single source of truth; callers translate :class:`PeerSendOutcome`
and the raised exceptions into their own response types.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Final

from ananta.llm.agent_messaging.models import PeerSendRequest
from ananta.llm.agent_messaging.schema import (
    META_KEY_DELIVERY_EXTERNAL_ID,
    META_KEY_RECIPIENT_KEY,
    META_KEY_RECIPIENT_KIND,
    META_KEY_ROLE_CREATED_AT,
    META_KEY_ROLE_ROW_ID,
    RECIPIENT_KIND_ROLE,
    ROLE_THREAD_PREFIX,
)
from ananta.llm.agent_messaging.service import role_message_external_id

from .bridge_sessions import BridgeNotFoundError, BridgeQueueFullError
from .peer_registry import PeerAmbiguousError, PeerUnreachableError
from .sender_provenance import (
    SENDER_PRINCIPAL_KIND_STDIO_AGENT,
    SENDER_PRINCIPAL_KIND_SYSTEM,
    SENDER_PRINCIPAL_KIND_UNKNOWN,
)
from .session_lifecycle_verbs import (
    DRIVE_DRIVER_UNAVAILABLE,
    DriveOnDeliveryOutcome,
    drive_on_delivery,
)

if TYPE_CHECKING:
    from ananta.interfaces.state_management_interface import StateManagementInterface
    from ananta.llm.agent_messaging.models import TextPart

    from .bridge_sessions import BridgeSessionManager
    from .peer_registry import BridgeBinding, PeerRegistry
    from .peer_role_management import ResolvedRole

logger = logging.getLogger(__name__)


# Channel event type used when delivering an IMPORTANT message via the
# recipient's bridge event queue (no native wake adapter registered).
# Imported by the existing http_routes module for the
# ``EVENT_PEER_MESSAGE`` re-export it still publishes.
EVENT_PEER_MESSAGE: Final[str] = "peer_message"

# Channel event type the claude_code native wake adapter rides (its wake IS an
# ``append_event`` on the recipient's bridge queue — v10 Q3-revised). Named so
# the events route can recognise acked wake deliveries without a magic string.
EVENT_POST_MESSAGE: Final[str] = "post_message"


# Matches the leading "IMPORTANT" marker (with ":" or whitespace tail).
# A4 (2026-08-04): retired as a delivery-gating signal — kept as a
# permanent cosmetic strip so literal "IMPORTANT:" text from old-habit
# senders never leaks into delivered prose. Stripped, never branched on.
IMPORTANT_MARKER_RE: Final[re.Pattern[str]] = re.compile(
    r"^\s*IMPORTANT[:\s]\s*", re.MULTILINE,
)


def _resolve_transport_provenance(
    *,
    sender_principal_kind: str,
    sender_bridge_id: str,
    sender_transport_principal: str,
    sender_identity_trust: str,
) -> tuple[str, str]:
    """Return explicit, transport-derived provenance without inferring authority."""
    if sender_principal_kind == SENDER_PRINCIPAL_KIND_STDIO_AGENT:
        return (
            sender_transport_principal or f"stdio_bridge:{sender_bridge_id}",
            sender_identity_trust or "stdio_bridge_registered",
        )
    if sender_principal_kind == SENDER_PRINCIPAL_KIND_SYSTEM:
        return (
            sender_transport_principal or "system",
            sender_identity_trust or "system",
        )
    return sender_transport_principal or "unknown", sender_identity_trust or "unknown"


# Delivery discriminator literals.  Exported so callers can compare
# against named constants instead of string literals.
#
# REL-06 (honest-return relabel): the two live-delivery kinds name what the
# code can truthfully prove. The platform only ever QUEUES a turn-triggering
# event on the recipient's live bridge long-poll — it never observes the wake
# or the consumption. ``queued_wake`` rides a ``post_message`` envelope through
# a registered native adapter; ``queued_notification`` rides a ``peer_message``
# channel event. Emission to the client happens at the forwarder's next drain;
# whether it becomes a turn is client-side and NOT confirmed by this field —
# consumption is tracked separately (REL-05 direct-wake outbox).
#
# Wake semantics, field-verified against a live deployment (2026-07-23): a
# recipient bound by a no-MCP ``watch`` subprocess is a PULL recipient — the
# queued event streams into a background task's output and surfaces when the
# session next looks; no turn starts. ``queued_watcher`` names that truthfully
# so senders never read a watcher delivery as a live wake. Consumption for
# these rides the watcher's
# events-ack (the /events route), not model activity.
DELIVERY_QUEUED_NOTIFICATION: Final[str] = "queued_notification"
DELIVERY_QUEUED_WAKE: Final[str] = "queued_wake"
DELIVERY_QUEUED_WATCHER: Final[str] = "queued_watcher"
# v10 Control #4: an IMPORTANT role message whose authoritative envelope was
# persisted (delivered=false) but whose current holder could not be reached
# (no live binding / wake failure / queue full). NOT a failure — the row sits
# durable and Control #5's repair loop re-delivers on the holder's next attach.
DELIVERY_QUEUED_FOR_REPLAY: Final[str] = "queued_for_replay"

@dataclass(frozen=True, slots=True)
class PeerSendOutcome:
    """Typed result of a peer_send dispatch.

    The fields are the union of every key the legacy bridge HTTP
    response and the streamable JSON-RPC tool result emit.  Callers
    pick whichever subset their transport surfaces.
    """

    thread_id: str
    message_id: str
    cursor: int
    delivered_to_agent_id: str
    delivered_to_agent_instance_id: str
    from_agent_id: str
    from_agent_instance_id: str
    delivery: str
    drive_on_delivery: DriveOnDeliveryOutcome
    delivered_to_bridge_id: str
    drive_on_delivery_detail: str | None = None

    def to_payload(self) -> dict[str, Any]:
        """Return the response shape both transports serialise."""
        return {
            "thread_id": self.thread_id,
            "message_id": self.message_id,
            "cursor": self.cursor,
            "delivered_to_agent_id": self.delivered_to_agent_id,
            "delivered_to_agent_instance_id": self.delivered_to_agent_instance_id,
            "from_agent_id": self.from_agent_id,
            "from_agent_instance_id": self.from_agent_instance_id,
            "delivery": self.delivery,
            "drive_on_delivery": self.drive_on_delivery,
            "drive_on_delivery_detail": self.drive_on_delivery_detail,
            "delivered_to_bridge_id": self.delivered_to_bridge_id,
        }


class NativeWakeError(Exception):
    """A registered native wake adapter raised mid-dispatch.

    The loop-prevention contract treats IMPORTANT delivery as a hard
    promise, so the adapter's failure surfaces to the caller as a
    stable error rather than a silent drop.  Carries the offending
    ``peer_agent_id`` for diagnostics + the originating exception
    via ``__cause__``.
    """

    def __init__(self, message: str, *, peer_agent_id: str) -> None:
        super().__init__(message)
        self.peer_agent_id: str = peer_agent_id


def dispatch_peer_send(
    *,
    bridge_manager: BridgeSessionManager,
    peer_registry: PeerRegistry,
    agent_messaging_service: Any,
    state_service: StateManagementInterface | None,
    sender_bridge_id: str,
    sender_agent_id: str,
    sender_agent_instance_id: str,
    sender_session_label: str,
    sender_parent_pid: int | None,
    peer_id: str,
    peer_agent_instance_id: str | None,
    content: list[TextPart],
    peer_agent_session_id: str | None = None,
    reply_to_role: str = "",
) -> PeerSendOutcome:
    """Dispatch a peer_send and return the typed outcome.

    ``reply_to_role`` is the SENDER's own durable role, when it has exactly one
    (:func:`role_binding_store.sole_role_for_reply_address`). A direct send used to
    hand the recipient an instance-only reply-to unconditionally, so every reply
    address the fleet handed out churned on the next restart wave — the WS-2c V4
    defect. Empty (roleless or multi-role sender) preserves the previous
    instance reply-to exactly.

    Raises:
        :class:`PeerAmbiguousError`: multiple instances of ``peer_id``
            registered and no ``peer_agent_instance_id`` hint given.
        :class:`PeerUnreachableError`: no binding matches the
            requested recipient.
        :class:`BridgeNotFoundError`: the recipient's bridge has been
            torn down between resolve and append.
        :class:`BridgeQueueFullError`: the recipient's event queue
            has hit its capacity.
        :class:`NativeWakeError`: a registered native wake adapter
            raised during IMPORTANT delivery.
    """
    recipient = _resolve_peer_recipient(
        peer_registry=peer_registry,
        peer_id=peer_id,
        peer_agent_instance_id=peer_agent_instance_id,
        peer_agent_session_id=peer_agent_session_id,
    )
    # A2: the SENDER's stable session key, resolved SERVER-SIDE from the sender's
    # own registered binding — never accepted from the caller, so it is a routing
    # key the registry vouches for rather than an identity claim. "" when the
    # sender has no binding (the CLI's one-shot bridge), which keeps that hint
    # instance-only rather than fabricating an unresolvable key.
    sender_agent_session_id = peer_registry.agent_session_id_for_instance(
        sender_agent_instance_id,
    )
    prose = "\n".join(part.text for part in content)
    marker_match = IMPORTANT_MARKER_RE.match(prose)
    # A4: the marker is stripped as input hygiene, never branched on for
    # anything else — every send takes this path (ruled NONE on a wake
    # toggle; see the module docstring).
    delivered_prose = prose[marker_match.end():] if marker_match else prose
    result = agent_messaging_service.peer_send(
        PeerSendRequest(
            sender_bridge_id=sender_bridge_id,
            sender_agent_id=sender_agent_id,
            sender_agent_instance_id=sender_agent_instance_id,
            sender_session_label=sender_session_label,
            peer_agent_id=peer_id,
            peer_agent_instance_id=recipient.agent_instance_id,
            peer_session_label=recipient.session_label,
            # REL-08 read-side: stamp the recipient's stable session key onto the
            # peer thread at creation so its inbox survives instance rotation.
            peer_agent_session_id=recipient.agent_session_id,
            sender_agent_session_id=sender_agent_session_id,
            content=content,
            important=True,
        ),
    )
    # Bump activity timestamps for the sender bridge + both endpoint
    # bindings.  Postgres-trigger-style semantics: every dispatch
    # makes ``updated_at`` carry "last active" meaning that every
    # peer_list read surfaces.
    sender_bridge = bridge_manager.get(sender_bridge_id)
    if sender_bridge is not None:
        sender_bridge.touch()
    peer_registry.touch_binding(sender_agent_instance_id)
    peer_registry.touch_binding(recipient.agent_instance_id)
    # A4 Amendment 5: binding_is_live generalized to EVERY recipient kind
    # (was watcher-only). The message row is ALREADY persisted at this
    # point, so raising here loses nothing — it tells the sender immediately
    # that nobody is polling (a resolved-but-stale binding: bridge present,
    # last_seen_at past the liveness window — e.g. a SIGKILLed process with
    # no client-initiated /close) instead of reporting a successful
    # ``queued_wake``/``queued_notification`` that will never be drained.
    if not binding_is_live(
        bridge_manager=bridge_manager,
        binding=recipient,
        window_seconds=bridge_manager.binding_liveness_window_s,
    ):
        raise PeerUnreachableError(
            f"{peer_id} instance {recipient.agent_instance_id}'s bridge "
            f"{recipient.bridge_id} has not polled within "
            f"{bridge_manager.binding_liveness_window_s}s — nothing is draining it. The "
            f"message IS persisted (message_id {result.message_id}) and readable "
            f"from that identity's inbox; it was not delivered to a live session.",
        )
    adapter = peer_registry.wake_adapter_for(peer_id)
    if adapter is not None:
        try:
            delivered_to_bridge_id = adapter.wake(
                recipient_parent_pid=recipient.parent_pid,
                delivered_prose=delivered_prose,
                sender_agent_id=sender_agent_id,
                sender_agent_instance_id=sender_agent_instance_id,
                sender_session_label=sender_session_label,
                thread_id=result.thread_id,
                message_id=result.message_id,
                # WS-2c V4: the sender's own role, when it holds exactly one, so the
                # return leg survives the sender's next reconnect. Empty keeps the
                # historical instance reply-to.
                reply_to_role=reply_to_role,
                # A2: rides ALONGSIDE the instance id in the hint, never replacing it.
                sender_agent_session_id=sender_agent_session_id,
            )
        except Exception as exc:  # noqa: BLE001 — adapter contract: any exception is failure
            logger.exception("native wake failed for %s", peer_id)
            raise NativeWakeError(
                f"native wake failed for {peer_id}: {exc}",
                peer_agent_id=peer_id,
            ) from exc
        # A watcher-held recipient is a pull consumer — the same queued event,
        # but no turn starts. Label truthfully.
        delivery_kind = (
            DELIVERY_QUEUED_WATCHER
            if recipient.is_watcher
            else DELIVERY_QUEUED_WAKE
        )
        delivered_bridge = str(delivered_to_bridge_id)
    else:
        meta = build_peer_message_meta(
            sender_agent_id=sender_agent_id,
            sender_agent_instance_id=sender_agent_instance_id,
            sender_agent_session_id=sender_agent_session_id,
            sender_session_label=sender_session_label,
            sender_parent_pid=sender_parent_pid,
            sender_bridge_id=sender_bridge_id,
            recipient_agent_id=recipient.agent_id,
            recipient_agent_instance_id=recipient.agent_instance_id,
            thread_id=result.thread_id,
            message_id=result.message_id,
            thread_cursor=result.cursor,
        )
        bridge_manager.append_event(
            recipient.bridge_id,
            EVENT_PEER_MESSAGE,
            delivered_prose,
            meta,
        )
        delivery_kind = (
            DELIVERY_QUEUED_WATCHER
            if recipient.is_watcher
            else DELIVERY_QUEUED_NOTIFICATION
        )
        delivered_bridge = recipient.bridge_id
    # A4 (2026-08-04): the REL-05 direct-wake insurance outbox retired here —
    # sweep_overdue_sessions + _notify_steward_of_overdue is its successor,
    # keyed on the recipient's own report_by promise rather than a per-message
    # consumption heuristic. See the architect memo, Amendment 4.
    # Drive-on-delivery (2026-08-04): a managed recipient's driver channel
    # gets an extra best-effort nudge ALONGSIDE the notify above (never
    # instead of it — this call cannot raise and cannot change delivery_kind).
    #
    # ★ IT IS UNCONDITIONAL AGAIN (GAU-06, 2026-08-19). A ``drive=False``
    # parameter lived here for one prospective caller — the rotation
    # SELF-notice, which must not inject a turn — and was removed WITHOUT ever
    # being invoked, because that caller measured this whole function as the
    # wrong route and went to the service-level ``peer_send`` instead. The knob
    # disarmed only the nudge below, while three other properties of this
    # function were equally wrong for a self-notice: the hardcoded
    # ``important=True`` above, the ``EVENT_PEER_MESSAGE`` label that destroys
    # the event-name discriminator a read-side filter needs, and the
    # ``[peer:...]`` envelope that presents a session's own measurement as mail
    # from somebody else. A parameter that fixes one quarter of a mismatch is
    # not a fix, it is a control nobody can reason about — and an uninvoked one
    # is worse, because the next reader has to work out what it was for.
    #
    # THE TRAP IT WAS WRITTEN TO DISARM IS REAL AND MOVED, NOT DROPPED: see
    # ``rotation_self_notice._notify_rotation_self``, which now carries it at
    # the call site where the decision lives. In short — ``drive_on_delivery``
    # NO-OPS for a session with no managed row (every operator-present seat) and
    # fires on every managed watcher-held worker, so a notice routed through
    # this default looks perfectly well behaved when hand-tested on a seat and
    # injects a turn on exactly the population it was meant to protect.
    drive_details: list[str] = []
    drive_outcome = drive_on_delivery(
        state_service,
        recipient_agent_instance_id=recipient.agent_instance_id,
        recipient_agent_session_id=recipient.agent_session_id,
        sender_label=sender_session_label,
        detail_sink=drive_details.append,
    )
    return PeerSendOutcome(
        thread_id=str(result.thread_id),
        message_id=str(result.message_id),
        cursor=int(result.cursor),
        delivered_to_agent_id=peer_id,
        delivered_to_agent_instance_id=recipient.agent_instance_id,
        from_agent_id=sender_agent_id,
        from_agent_instance_id=sender_agent_instance_id,
        delivery=delivery_kind,
        drive_on_delivery=drive_outcome,
        delivered_to_bridge_id=delivered_bridge,
        drive_on_delivery_detail=drive_details[0] if drive_details else None,
    )


def _resolve_peer_recipient(
    *,
    peer_registry: PeerRegistry,
    peer_id: str,
    peer_agent_instance_id: str | None,
    peer_agent_session_id: str | None,
) -> BridgeBinding:
    """Resolve exact instance first, then its stable-session fallback (A2).

    The try covers registry resolution alone. A later PeerUnreachableError can
    mean an already-resolved watcher stopped polling after persistence; routing
    that error elsewhere would duplicate the message and violate live-instance
    precedence.
    """
    try:
        return peer_registry.resolve(peer_id, peer_agent_instance_id)
    except PeerUnreachableError as original_error:
        if not peer_agent_session_id:
            raise
        recipient = peer_registry.resolve_by_agent_session_id(
            peer_agent_session_id,
        )
        # A stable key is a fallback routing key, not permission to change the
        # requested agent kind. Unknown and cross-kind keys preserve the exact
        # original instance error.
        if recipient is None or recipient.agent_id != peer_id:
            raise original_error
        return recipient


@dataclass(frozen=True, slots=True)
class RoleSendOutcome:
    """Typed result of a role-addressed (``peer_send_by_name``) dispatch.

    ``thread_id`` is the synthetic ``"role:{name}"`` handle (display only).
    ``delivery`` is one of ``queued_wake`` / ``queued_notification`` (the role
    event was QUEUED on the live holder's bridge; ``delivered`` is flipped by
    the holder's forwarder on confirmed emission, NOT at send — v10
    Q3-revised), ``queued_watcher`` (the holder is a no-MCP watcher: the
    event streams into its watch output and surfaces when the session next
    looks — no turn starts; consumption is the watcher's events-ack), or
    ``queued_for_replay`` (persisted but the holder was unreachable; the
    repair loop re-delivers). Every role send is delivery-attempted (A4,
    2026-08-04) — there is no longer a silent/inbox-only outcome.
    """

    thread_id: str
    message_id: str
    role: ResolvedRole
    delivery: str
    drive_on_delivery: DriveOnDeliveryOutcome
    delivered_to_bridge_id: str
    drive_on_delivery_detail: str | None = None

    def to_payload(self) -> dict[str, Any]:
        return {
            "thread_id": self.thread_id,
            "message_id": self.message_id,
            "delivery": self.delivery,
            "drive_on_delivery": self.drive_on_delivery,
            "drive_on_delivery_detail": self.drive_on_delivery_detail,
            "delivered_to_bridge_id": self.delivered_to_bridge_id,
            "resolved_agent_id": self.role.agent_id,
            "resolved_agent_instance_id": self.role.agent_instance_id,
            "resolved_session_label": self.role.session_label,
        }


def binding_is_live(
    *,
    bridge_manager: BridgeSessionManager,
    binding: BridgeBinding,
    window_seconds: int,
    now: datetime | None = None,
) -> bool:
    """Is this binding's bridge actually being polled right now? (WS-2a W3)

    The shared liveness discriminator of WS-2e §4.3.2 — this is consumer 1
    (dispatch labelling); consumer 2 (the duplicate-role claim gate) keys on
    the same window so the two layers can never disagree about who is alive.

    A MISSING bridge is not live: it was closed or swept. A present bridge is
    live iff its ``last_seen_at`` is within ``window_seconds``, because both
    transports long-poll continuously and a healthy bridge is touched on every
    poll. An unparseable timestamp is treated as NOT live — a bridge whose
    liveness cannot be established should not be told to anyone as reachable.
    """
    bridge = bridge_manager.get(binding.bridge_id)
    if bridge is None or bridge.closed:
        return False
    try:
        last_seen = datetime.fromisoformat(bridge.last_seen_at)
    except (TypeError, ValueError):
        return False
    return (
        (now or datetime.now(UTC)) - last_seen
    ).total_seconds() <= window_seconds


def _deliver_important_to_binding(
    *,
    bridge_manager: BridgeSessionManager,
    peer_registry: PeerRegistry,
    recipient: BridgeBinding,
    delivered_prose: str,
    sender_agent_id: str,
    sender_agent_instance_id: str,
    sender_session_label: str,
    sender_parent_pid: int | None,
    sender_bridge_id: str,
    thread_id: str,
    message_id: str,
    thread_cursor: int,
    recipient_kind: str,
    recipient_key: str,
    delivery_external_id: str,
    role_created_at: str,
    role_row_id: str,
    reply_to_role: str = "",
) -> tuple[str, str]:
    """Deliver an IMPORTANT role message to a resolved recipient binding.

    Returns ``(delivery_kind, delivered_to_bridge_id)``: native wake adapter if
    one is registered for the recipient's ``agent_id`` (``queued_wake``),
    else a ``peer_message`` channel event on the recipient's bridge
    (``queued_notification``). Either transport reports ``queued_watcher``
    when the resolved holder is a no-MCP watcher binding (pull recipient — the
    event surfaces in the watch output, no turn starts). Raises
    :class:`NativeWakeError` if a registered adapter fails — the
    loop-prevention contract treats IMPORTANT delivery as a hard promise.

    Role-only (called solely from :func:`dispatch_role_send`), so the v10
    Control #5 role-delivery meta keys (``recipient_kind`` / ``recipient_key`` /
    ``delivery_external_id``) ride the bridge event on BOTH paths — the
    ``queued_notification`` path stamps them on the ``append_event`` meta, the
    ``queued_wake`` path passes them via ``wake(delivery_meta=)`` (the
    native wake is the SAME bridge queue, NOT a direct push). The holder's bridge
    forwarder reads them off ``/events`` to recognise a role delivery and confirm
    ``delivered=true`` (the M7 flip). NEITHER path flips at send: the forwarder
    ``/peer/delivered`` is the SOLE delivered-authority for both (v10 Q3-revised,
    Codex BLOCKER-3).
    """
    # A4 Amendment 5: binding_is_live generalized to EVERY recipient kind
    # (was watcher-only). Raising here routes into dispatch_role_send's
    # EXISTING PeerUnreachableError branch, which already returns
    # `queued_for_replay` — honest, durable, and re-delivered. No new label,
    # no schema change.
    if not binding_is_live(
        bridge_manager=bridge_manager,
        binding=recipient,
        window_seconds=bridge_manager.binding_liveness_window_s,
    ):
        raise PeerUnreachableError(
            f"binding {recipient.agent_instance_id} resolves to bridge "
            f"{recipient.bridge_id}, which has not polled within "
            f"{bridge_manager.binding_liveness_window_s}s — treating as unreachable",
        )
    adapter = peer_registry.wake_adapter_for(recipient.agent_id)
    if adapter is not None:
        try:
            delivered_to_bridge_id = adapter.wake(
                recipient_parent_pid=recipient.parent_pid,
                delivered_prose=delivered_prose,
                sender_agent_id=sender_agent_id,
                sender_agent_instance_id=sender_agent_instance_id,
                sender_session_label=sender_session_label,
                thread_id=thread_id,
                message_id=message_id,
                # REL-01 Fork 4: role sends carry the durable reply-to role so the
                # holder's envelope surfaces a role reply-to (two-way, reconnect-
                # surviving return leg). Empty for the direct-peer path.
                reply_to_role=reply_to_role,
                # v10 Control #5 / Q3-revised (Codex B3): the wake is the SAME
                # bridge queue as queued_notification — stamp the role keys so the
                # holder's forwarder confirms delivery (/peer/delivered), not a
                # send-flip.
                delivery_meta={
                    META_KEY_RECIPIENT_KIND: recipient_kind,
                    META_KEY_RECIPIENT_KEY: recipient_key,
                    META_KEY_DELIVERY_EXTERNAL_ID: delivery_external_id,
                    META_KEY_ROLE_CREATED_AT: role_created_at,
                    META_KEY_ROLE_ROW_ID: role_row_id,
                },
            )
        except Exception as exc:  # noqa: BLE001 — adapter contract: any exception is failure
            logger.exception("native wake failed for %s", recipient.agent_id)
            raise NativeWakeError(
                f"native wake failed for {recipient.agent_id}: {exc}",
                peer_agent_id=recipient.agent_id,
            ) from exc
        if recipient.is_watcher:
            # Pull recipient — same queued event, no turn starts.
            return DELIVERY_QUEUED_WATCHER, str(delivered_to_bridge_id)
        return DELIVERY_QUEUED_WAKE, str(delivered_to_bridge_id)
    meta = build_peer_message_meta(
        sender_agent_id=sender_agent_id,
        sender_agent_instance_id=sender_agent_instance_id,
        sender_session_label=sender_session_label,
        sender_parent_pid=sender_parent_pid,
        sender_bridge_id=sender_bridge_id,
        recipient_agent_id=recipient.agent_id,
        recipient_agent_instance_id=recipient.agent_instance_id,
        thread_id=thread_id,
        message_id=message_id,
        thread_cursor=thread_cursor,
    )
    meta[META_KEY_RECIPIENT_KIND] = recipient_kind
    meta[META_KEY_RECIPIENT_KEY] = recipient_key
    meta[META_KEY_DELIVERY_EXTERNAL_ID] = delivery_external_id
    meta[META_KEY_ROLE_CREATED_AT] = role_created_at
    meta[META_KEY_ROLE_ROW_ID] = role_row_id
    # REL-01 Fork 4 (Codex blocker): the no-adapter path serves Codex + streamable
    # recipients (no native wake adapter). Carry the reply-to on the PROSE so
    # two-way ROLE addressing works for them too — the SAME helper the native wake
    # envelope uses, so it is transport-agnostic and adds no role-coupling to the
    # forwarder. Empty ``reply_to_role`` yields the instance reply-to, matching the
    # native path.
    hinted_prose = f"{delivered_prose}\n\n" + build_wake_reply_hint(
        reply_to_role=reply_to_role,
        sender_agent_id=sender_agent_id,
        sender_agent_instance_id=sender_agent_instance_id,
        thread_id=thread_id,
        message_id=message_id,
    )
    bridge_manager.append_event(
        recipient.bridge_id, EVENT_PEER_MESSAGE, hinted_prose, meta,
    )
    if recipient.is_watcher:
        return DELIVERY_QUEUED_WATCHER, recipient.bridge_id
    return DELIVERY_QUEUED_NOTIFICATION, recipient.bridge_id


def dispatch_role_send(
    *,
    bridge_manager: BridgeSessionManager,
    peer_registry: PeerRegistry,
    agent_messaging_service: Any,
    state_service: StateManagementInterface | None,
    role_name: str,
    role: ResolvedRole,
    sender_bridge_id: str,
    sender_agent_id: str,
    sender_agent_instance_id: str,
    sender_session_label: str,
    sender_parent_pid: int | None,
    content: list[TextPart],
    message_id: str,
    reply_to_role: str = "",
    sender_principal_kind: str = SENDER_PRINCIPAL_KIND_UNKNOWN,
    sender_transport_principal: str = "",
    sender_identity_trust: str = "",
) -> RoleSendOutcome:
    """Persist-first role-addressed dispatch (v10 Controls #1/#3/#4).

    The caller has already existence-gated the role (Scope-decision C: an
    unknown role name is REJECTED before reaching here, so a typo can't seed a
    durable-but-undeliverable row). This function then:

    1. **Persist-first (#4.1/#1.1):** ONE authoritative ``upsert_state`` of the
       complete envelope (``delivered=false``), BEFORE resolving the live
       holder binding — so a role send never requires a live binding.
    2. **Best-effort deliver (#4.2):** resolve the current holder's live
       binding and emit; on no-binding / wake failure / queue full →
       ``queued_for_replay`` (success — the row persists, Control #5 re-delivers).
       On success, NEITHER path flips ``delivered`` here at send (v10 Q3-revised,
       Codex BLOCKER-3): both the ``queued_notification`` channel event AND the
       ``queued_wake`` wake are merely QUEUED on the holder's bridge
       (the native wake is the same append_event queue, not a direct push), and
       both carry the Control #5 role keys. The holder's forwarder is the SOLE
       flip authority for both (POST /peer/delivered after confirmed emission),
       so an unconfirmed queued event stays ``delivered=false`` for the repair
       drain to re-deliver.

    A4 (2026-08-04): every role send takes step 2 — the former "non-IMPORTANT
    -> inbox-only, never emitted" branch is retired (ruled NONE on a wake
    toggle). The IMPORTANT marker is still matched and stripped from the
    delivered prose as input hygiene, never branched on.
    """
    prose = "\n".join(part.text for part in content)
    marker_match = IMPORTANT_MARKER_RE.match(prose)
    delivered_prose = prose[marker_match.end():] if marker_match else prose
    transport_principal, identity_trust = _resolve_transport_provenance(
        sender_principal_kind=sender_principal_kind,
        sender_bridge_id=sender_bridge_id,
        sender_transport_principal=sender_transport_principal,
        sender_identity_trust=sender_identity_trust,
    )
    persisted = agent_messaging_service.persist_role_message(
        recipient_kind=RECIPIENT_KIND_ROLE,
        recipient_key=role_name,
        message_id=message_id,
        sender_agent_id=sender_agent_id,
        sender_agent_instance_id=sender_agent_instance_id,
        sender_session_label=sender_session_label,
        sender_principal_kind=sender_principal_kind,
        sender_transport_principal=transport_principal,
        sender_identity_trust=identity_trust,
        important=True,
        content=content,
    )
    thread_id = f"{ROLE_THREAD_PREFIX}{role_name}"
    delivery_external_id = role_message_external_id(
        RECIPIENT_KIND_ROLE, role_name, message_id,
    )
    try:
        # A parked Codex lane replaces its managed binding with an
        # ``agi-watch-*`` binding. The role row keeps the stable session key,
        # so preserve exact-instance precedence but use that key if the
        # managed instance is no longer registered. Without it, the fleet's
        # standard peer_send_by_name path leaves a live watcher queued only for
        # replay even though its bridge is actively polling.
        recipient = _resolve_peer_recipient(
            peer_registry=peer_registry,
            peer_id=role.agent_id,
            peer_agent_instance_id=role.agent_instance_id,
            peer_agent_session_id=role.agent_session_id,
        )
        delivery_kind, delivered_to_bridge_id = _deliver_important_to_binding(
            bridge_manager=bridge_manager,
            peer_registry=peer_registry,
            recipient=recipient,
            delivered_prose=delivered_prose,
            sender_agent_id=sender_agent_id,
            sender_agent_instance_id=sender_agent_instance_id,
            sender_session_label=sender_session_label,
            sender_parent_pid=sender_parent_pid,
            sender_bridge_id=sender_bridge_id,
            thread_id=thread_id,
            message_id=message_id,
            thread_cursor=0,
            recipient_kind=RECIPIENT_KIND_ROLE,
            recipient_key=role_name,
            delivery_external_id=delivery_external_id,
            # Control #5 / (A): the persisted ROW's created_at, so the holder's
            # watcher advances its role mark on the same quantity the role-inbox
            # section pages on. Never a clock read taken here — see
            # RoleMessagePersisted.
            role_created_at=persisted.created_at,
            role_row_id=persisted.role_row_id,
            reply_to_role=reply_to_role,
        )
    except (
        PeerUnreachableError,
        PeerAmbiguousError,
        BridgeNotFoundError,
        BridgeQueueFullError,
        NativeWakeError,
    ):
        # Holder unreachable — the envelope is durable; replay on next attach.
        return RoleSendOutcome(
            thread_id=thread_id,
            message_id=message_id,
            role=role,
            delivery=DELIVERY_QUEUED_FOR_REPLAY,
            drive_on_delivery=DRIVE_DRIVER_UNAVAILABLE,
            delivered_to_bridge_id="",
        )
    # v10 Q3 — REVISED (Codex BLOCKER-3 / Architect 2026-06-19): NO send-time
    # flip for EITHER path. Native wake is NOT a direct push — the claude_code
    # adapter's wake() is the SAME ``manager.append_event`` bridge queue as the
    # queued_notification path (verified plugin.py:1726+), and a closed/reconnected bridge
    # drops queued events. Both paths therefore stamp the role keys on the
    # bridge EVENT and rely on the holder's forwarder POST /peer/delivered
    # (Control #5 M7) as the SOLE delivered-authority: a reconnect-before-drain
    # leaves ``delivered=false`` and the repair drain re-delivers, for BOTH
    # paths. This is STRONGER than the old optimistic send-flip (forwarder-
    # CONFIRMED at-least-once) and removes the takeover-between-wake-and-flip
    # suppression — the flip is always whoever currently holds + drains.
    # Drive-on-delivery (2026-08-04): ALONGSIDE the notify above, never
    # instead of it — best-effort, cannot raise, cannot change delivery_kind.
    drive_details: list[str] = []
    drive_outcome = drive_on_delivery(
        state_service,
        recipient_agent_instance_id=recipient.agent_instance_id,
        recipient_agent_session_id=recipient.agent_session_id,
        sender_label=sender_session_label,
        detail_sink=drive_details.append,
    )
    return RoleSendOutcome(
        thread_id=thread_id,
        message_id=message_id,
        role=role,
        delivery=delivery_kind,
        drive_on_delivery=drive_outcome,
        delivered_to_bridge_id=delivered_to_bridge_id,
        drive_on_delivery_detail=drive_details[0] if drive_details else None,
    )


def build_wake_reply_hint(
    *,
    reply_to_role: str,
    sender_agent_id: str,
    sender_agent_instance_id: str,
    thread_id: str,
    message_id: str,
    sender_agent_session_id: str = "",
) -> str:
    """The reply-to hint carried on an IMPORTANT delivery (REL-01 Fork 4).

    A role-addressed send (``reply_to_role`` set) surfaces a ROLE reply-to
    (``peer_send_by_name name=<role>``) so the return leg survives a holder
    reconnect — an instance reply-to churns on every reconnect, the original
    defect. A direct instance send keeps the same-connection instance reply-to.
    Shared by BOTH the native-wake envelope (``plugin.wake``) and the no-adapter
    channel-event path (``_deliver_important_to_binding`` below), so Codex /
    streamable recipients get the same two-way role reply-to — transport-agnostic,
    with no role-coupling in the forwarder. ``reply_to_role`` is treated as an
    opaque, operator-defined string (never enumerated / special-cased).
    """
    if reply_to_role:
        return (
            f"(reply via peer_send_by_name with name={reply_to_role}; "
            f'prefix prose with "IMPORTANT: " only if you need a response. '
            f"thread_id={thread_id}, message_id={message_id}.)"
        )
    # WS-2c V4 / A2: the sender's STABLE session key rides ALONGSIDE the instance
    # id, never replacing it — old receivers keep reading the field they already
    # know, and a receiver whose stored instance id has rotated has a second key
    # that survives. Omitted entirely when the sender has no registered binding,
    # so an unresolvable hint is never fabricated.
    session_hint = (
        f", peer_agent_session_id={sender_agent_session_id}"
        if sender_agent_session_id
        else ""
    )
    return (
        f"(reply via peer_send with peer_id={sender_agent_id}, "
        f"peer_agent_instance_id={sender_agent_instance_id}{session_hint}; "
        f'prefix prose with "IMPORTANT: " only if you need a response. '
        f"thread_id={thread_id}, message_id={message_id}.)"
    )


def build_peer_message_meta(
    *,
    sender_agent_id: str,
    sender_agent_instance_id: str,
    sender_session_label: str,
    sender_parent_pid: int | None,
    sender_bridge_id: str,
    recipient_agent_id: str,
    recipient_agent_instance_id: str,
    thread_id: str,
    message_id: str,
    thread_cursor: int,
    sender_agent_session_id: str = "",
) -> dict[str, object]:
    """Construct the ``peer_message`` channel-event metadata dict."""
    meta: dict[str, object] = {
        "thread_id": thread_id,
        "message_id": message_id,
        "thread_cursor": thread_cursor,
        "from_agent_id": sender_agent_id,
        "from_agent_instance_id": sender_agent_instance_id,
        "from_session_label": sender_session_label,
        "from_parent_pid": sender_parent_pid,
        "to_agent_id": recipient_agent_id,
        "to_agent_instance_id": recipient_agent_instance_id,
        "from_bridge_id": sender_bridge_id,
        "important": True,
        "sent_at": datetime.now(UTC).isoformat(),
    }
    if sender_agent_session_id:
        meta["from_agent_session_id"] = sender_agent_session_id
    return meta


__all__ = [
    "DELIVERY_QUEUED_NOTIFICATION",
    "DELIVERY_QUEUED_FOR_REPLAY",
    "DELIVERY_QUEUED_WAKE",
    "DELIVERY_QUEUED_WATCHER",
    "EVENT_PEER_MESSAGE",
    "EVENT_POST_MESSAGE",
    "IMPORTANT_MARKER_RE",
    "NativeWakeError",
    "PeerSendOutcome",
    "RoleSendOutcome",
    "build_peer_message_meta",
    "build_wake_reply_hint",
    "dispatch_peer_send",
    "dispatch_role_send",
]
