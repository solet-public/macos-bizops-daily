"""Shared role-claim body — ONE implementation behind TWO transports.

``peer_claim_role`` is reachable two ways, and the difference between them is
the whole point of this module:

* ``/process/call`` → the ``peer_claim_role`` platform verb. MODEL_INITIATED.
  A ``/rename`` claim is a genuine model turn and MUST stamp
  ``last_model_activity_at``.
* ``{API_PREFIX}/{bridge_id}/peer/claim_role`` → the dedicated bridge route.
  INFRA. The forwarder's housekeeping claim (open / reconnect / steady-state
  re-assert) runs with no model turn behind it and MUST NOT stamp.

Before the split, the forwarder's housekeeping claim traversed the
MODEL_INITIATED ``/process/call`` route every ~176s forever, phantom-stamping
model activity with no model turn — which silently marked owed IMPORTANT wakes
to an idle session as consumed — and fired a ``bridge_delivery_result``
notification each tick. See the 2026-07-29 Architect ruling,
``workbench/2026-07-29_role_claim_route_classification_architect_ruling.md``.

WHY A SHARED HELPER RATHER THAN THE ``peer_send_by_name`` PRECEDENT. That
precedent reaches its synchronous response through ``_peer_send_by_name_impl``,
a parallel implementation of the verb in ``http_routes``. It is cheap there
because ``peer_send_by_name`` is nearly pure dispatch. It is NOT cheap here: a
claim terminates in the handover settle below, which owns semantics a duplicate
would have to reproduce exactly — ``refreshed`` surfacing as ``updated`` with no
wake, ``claimed``/``displaced`` notifying the displaced prior at its CURRENT
bridge plus the new-holder confirm, the non-serializable ``prior`` never
reaching a public result, and every declared ``return_value_schema`` property
being present. A duplicate that got any of those subtly wrong would fire phantom
handover wakes — the same defect class the split exists to remove. So the
notify + serialization contract is single-sourced here and both transports call
it. The Architect ruling ratifies this shape (§4, Condition 1 clarification).

:class:`RoleClaimOrigin` records WHICH transport a claim arrived on. It is
declarative only — this module never stamps and never infers model-ness from
it. Route classification remains the single source of truth for stamping and
lives in :mod:`route_activity`, keyed on path alone. The parameter exists so
both call sites state their classification out loud, and so whoever adds a
third transport has to answer the question rather than inherit an answer.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING, Any

from ananta.llm.agent_messaging.models import TextPart
from ananta.llm.agent_messaging.role_binding import (
    AGENT_ROLE_BINDING_NAMESPACE,
    HOLDER_KIND_INFERENCE_PROVIDER,
    HOLDER_KIND_SESSION,
    TABLE_ROLE,
    is_reserved_primary_name,
    is_system_role,
    role_binding_external_id,
)
from ananta.llm.agent_messaging.state_results import require_records

from .bridge_sessions import BridgeNotFoundError, BridgeQueueFullError
from .constants import (
    SYSTEM_AGENT_ID,
    SYSTEM_ROLE_HANDOVER_ID,
    SYSTEM_ROLE_HANDOVER_LABEL,
)
from .peer_dispatch import (
    NativeWakeError,
    binding_is_live,
    dispatch_peer_send,
)
from .peer_registry import (
    PeerAmbiguousError,
    PeerSessionAmbiguousError,
    PeerUnreachableError,
)
from .role_binding_store import (
    HolderClaim,
    RoleBindingMalformedError,
    RoleBindingVacantError,
    claim_role_binding_v4,
    resolve_role_binding_v4,
    session_claim_requires_session_id,
    upsert_role_entity,
)
from .session_role_claim_store import (
    CardinalityConflictError,
    CardinalityGatedClaim,
    SessionRoleClaimContendedError,
    delete_session_role_claim_if_still_holds,
    win_cardinality_gate,
)
from .system_slots import SystemSlotClaimDecision, evaluate_system_slot_claim

if TYPE_CHECKING:  # pragma: no cover — type-only references
    from ananta.interfaces.state_management_interface import StateManagementInterface

    from .bridge_sessions import BridgeSessionManager
    from .peer_registry import PeerRegistry
    from .role_binding_store import ResolvedRole

logger = logging.getLogger(__name__)


class RoleClaimOrigin(Enum):
    """Which transport a claim arrived on — declarative, never a stamp switch.

    ``MODEL_TURN`` is the ``/process/call`` verb (a real model turn, e.g.
    ``/rename``); ``INFRA`` is the bridge route the forwarder uses for
    housekeeping. Nothing in this module branches on it beyond logging: the
    stamping decision belongs to the route layer, which classifies on path
    alone and cannot be told otherwise by a caller.
    """

    MODEL_TURN = "model_turn"
    INFRA = "infra"


@dataclass(frozen=True)
class RoleClaimSuccess:
    """A landed claim, in the shape both transports must publish.

    :meth:`to_public` is the SINGLE definition of the public payload. Every
    property declared by the verb's ``return_value_schema`` must appear in it:
    ``ExecutionContext.store_result`` raises ``PlaceholderResolutionError`` on a
    declared-but-absent property, which fails the action AFTER the binding write
    has already landed — reporting a bogus failure for a claim that actually
    succeeded. Keeping one definition is what stops the two transports drifting
    into that trap independently.
    """

    action: str
    name: str
    agent_instance_id: str
    agent_session_id: str

    def to_public(self) -> dict[str, str]:
        """The claim outcome as published to callers of EITHER transport."""
        return {
            "action": self.action,
            "name": self.name,
            "agent_instance_id": self.agent_instance_id,
            "agent_session_id": self.agent_session_id,
        }


@dataclass(frozen=True)
class RoleClaimFailure:
    """A refused claim. Each transport renders this into its own error shape."""

    code: str
    message: str


RoleClaimResult = RoleClaimSuccess | RoleClaimFailure


def displaced_prose(name: str, new_agent_instance_id: str) -> str:
    """REL-04 displaced-holder notice. ``name`` is an opaque operator-defined role."""
    return (
        f"IMPORTANT: You have been displaced from role {name!r} by instance "
        f"{new_agent_instance_id}. You no longer hold this role — a role-addressed "
        f"message to {name!r} now reaches the new holder. Re-claim the role "
        f"(/rename) if this displacement was not intended."
    )


def new_holder_prose(name: str) -> str:
    """REL-04 new-holder confirmation. ``name`` is an opaque operator-defined role.

    Names the full process key rather than a bare verb. Until Part 24 registered
    ``plugin::agent_messaging_plugin::peer_inbox`` this notice instructed every
    new role holder to call something only an MCP session could reach — a
    no-MCP session read "drain your backlog with peer_inbox" and had no
    peer_inbox to call. The key is copy-runnable on both transports, so the
    instruction is now executable by whoever receives it.

    **Attestation closing step (rotation-systematization fix loop, 2026-08-08
    — ``workbench/2026-08-07_rotation_systematization_findings_rotation-impl.md``,
    "the seam and the correctness condition"):** added because the covered-mark
    floor (``peer_mark_role_covered``) was found to be permanently inert for
    every role in practice — nothing anywhere ever called it, BY DESIGN (the
    verb is registered-route-only), so a role's mark never advances unless the
    holder itself attests. This is the ONE place that instruction can live and
    reach every deployment (the platform's own handover text, not a
    checkout-local convention). The correctness condition is stated IN the
    prose itself, not merely documented elsewhere, because a safety rule that
    lives outside the instruction a reader actually receives gets followed
    unsafely. The transport caveat is equally explicit and load-bearing: a
    ``watch``-transport session's own outbound calls are a bare
    ``solet-bridge call`` subprocess, which the platform stamps with
    ``caller_attribution_*`` rather than ``inference_vertex_session_id`` — the
    field ``peer_mark_role_covered`` requires. Such a session CANNOT attest,
    ever, no matter how it tries; the prose says so plainly rather than
    implying a capability the reader may not have. This function adds NO code
    path that attests on any session's behalf — the platform still only ever
    accepts an attestation from the holder's own live, registered-route turn.
    """
    return (
        f"IMPORTANT: You now hold role {name!r}. Drain your role backlog with "
        f"plugin::agent_messaging_plugin::peer_inbox (passing your own "
        f"agent_session_id) — role-addressed messages sent to {name!r} while "
        f"it was held by another session (or unclaimed) are waiting. Page the "
        f"role section with role_after until next_role_cursor is null; "
        f"role_section_status 'ok' does not mean drained. If the page reports "
        f"role_read_page_status 'pending_ack', acknowledge its opaque "
        f"role_read_page_token only after your receiver successfully rendered "
        f"the returned rows; that exact per-instance display receipt is weaker "
        f"than the role-wide covered mark and neither is proof of model consumption. "
        f"role_floor_applied=true, the default drain stopped at this role's "
        f"covered mark — echo the returned role_history_cursor back as "
        f"role_after for a deliberate read of anything before it. "
        f"Once you have read and acted on this backlog, attest your own "
        f"progress: call plugin::agent_messaging_plugin::peer_mark_role_covered "
        f"with the newest message_id from the LAST peer_inbox response's "
        f"role_entries you actually read — never a computed or assumed "
        f"boundary, and never the newest message in the whole backlog if your "
        f"last read did not return it. This requires a call dispatched through "
        f"your own registered bridge (a live MCP tool call) — a bare "
        f"solet-bridge call subprocess cannot attest and is refused loud. If "
        f"your session has no registered bridge (e.g. watch transport with no "
        f"MCP route), you cannot perform this step; that is expected, not an "
        f"error — this role's covered mark will simply stay behind your reads, "
        f"and nothing else will attest on your behalf."
    )


def send_handover_notice(
    *,
    bridge_manager: BridgeSessionManager | None,
    peer_registry: PeerRegistry | None,
    agent_messaging_service: Any | None,
    state_service: StateManagementInterface | None,
    peer_id: str,
    peer_agent_instance_id: str,
    prose: str,
    kind: str,
    peer_agent_session_id: str = "",
) -> bool:
    """Best-effort IMPORTANT role-handover notice to a specific instance (REL-04).

    Persists durably + wakes when the recipient is live. NEVER raises — a role
    claim must not fail because a handover notice could not be delivered
    (displacement often happens BECAUSE the prior holder is dead). Returns
    ``True`` on delivery, ``False`` on best-effort failure (loud log).
    """
    if bridge_manager is None or peer_registry is None or agent_messaging_service is None:
        logger.warning(
            "REL-04 %s notice skipped (bridge not started): agi=%s",
            kind, peer_agent_instance_id,
        )
        return False
    try:
        dispatch_peer_send(
            bridge_manager=bridge_manager,
            peer_registry=peer_registry,
            agent_messaging_service=agent_messaging_service,
            state_service=state_service,
            sender_bridge_id=SYSTEM_ROLE_HANDOVER_ID,
            sender_agent_id=SYSTEM_AGENT_ID,
            sender_agent_instance_id=SYSTEM_ROLE_HANDOVER_ID,
            sender_session_label=SYSTEM_ROLE_HANDOVER_LABEL,
            sender_parent_pid=None,
            peer_id=peer_id,
            peer_agent_instance_id=peer_agent_instance_id,
            peer_agent_session_id=peer_agent_session_id or None,
            content=[TextPart(type="text", text=prose)],
        )
    except (
        PeerAmbiguousError,
        PeerUnreachableError,
        BridgeNotFoundError,
        BridgeQueueFullError,
        NativeWakeError,
    ) as exc:
        logger.warning(
            "REL-04 %s notice undelivered (agi=%s): %s — role claim proceeds",
            kind, peer_agent_instance_id, exc,
        )
        return False
    return True


def is_genuine_displacement(
    prior: Any, new_agent_instance_id: str, new_agent_session_id: str,
) -> bool:
    """True iff ``prior`` is a DIFFERENT session than the new holder (REL-07(2)).

    Keys on the stable ``agent_session_id`` when both sides have one — an
    ``agent_instance_id`` rotates on reconnect, so the old instance-id check
    mistook a same-session re-claim for a displacement and double-woke it.
    Falls back to instance identity only for a legacy binding row that carries
    no session id.
    """
    if prior is None:
        return False
    prior_sid = str(getattr(prior, "agent_session_id", "") or "")
    if prior_sid and new_agent_session_id:
        return prior_sid != new_agent_session_id
    return bool(prior.agent_instance_id) and (
        prior.agent_instance_id != new_agent_instance_id
    )


def displaced_target(
    prior: Any, peer_registry: PeerRegistry | None,
) -> tuple[str, str]:
    """Route the displaced-holder notice to the prior holder's CURRENT bridge.

    The role binding records the ``agent_instance_id`` as of the CLAIM; by the
    time a reconnect displaces it, that instance has rotated. Resolve the
    prior's live binding by its stable session id; fall back to the recorded
    instance when it has no session id, no live binding, or an ambiguous one
    (best-effort — an undeliverable notice never gates a claim).
    """
    prior_sid = str(getattr(prior, "agent_session_id", "") or "")
    if prior_sid and peer_registry is not None:
        try:
            live = peer_registry.resolve_by_agent_session_id(prior_sid)
        except PeerSessionAmbiguousError as exc:
            logger.warning(
                "REL-04 displaced-notice: ambiguous session id %r — "
                "falling back to recorded instance: %s", prior_sid, exc,
            )
            live = None
        if live is not None:
            return live.agent_id, live.agent_instance_id
    return prior.agent_id, prior.agent_instance_id


def notify_role_handover(
    *,
    bridge_manager: BridgeSessionManager | None,
    peer_registry: PeerRegistry | None,
    agent_messaging_service: Any | None,
    state_service: StateManagementInterface | None,
    name: str,
    new_agent_id: str,
    new_agent_instance_id: str,
    new_agent_session_id: str,
    prior: Any,
) -> None:
    """Fire the REL-04/§5.4 handover notices for a GENUINE role claim.

    Notifies a displaced PRIOR holder — routed to its CURRENT bridge via its
    stable session id (§5.4), not the stale recorded instance — then confirms
    to the new holder so it drains any role backlog. Best-effort throughout;
    the claim already succeeded and never fails on a notify. The caller
    suppresses this entirely for an idempotent self-re-claim, so any prior seen
    here is a different session. ``name`` is opaque — never special-cased.

    §5.4 provider-transition rule: a displaced holder that is an
    ``inference_provider`` has NO wake target (a provider consumes no
    messages), so the transition is LOG-LOUD, never silent — an audit line
    instead of an undeliverable notice. The displaced-notice fires ONLY when
    the displaced holder is a session.
    """
    if prior is not None and (
        str(getattr(prior, "holder_kind", "") or "") == HOLDER_KIND_INFERENCE_PROVIDER
    ):
        logger.warning(
            "role %r handover: displaced holder was an inference_provider "
            "(identity=%s) — no wake target, logging the transition for audit "
            "(new holder %s/%s).",
            name, getattr(prior, "holder_identity", {}), new_agent_id,
            new_agent_instance_id,
        )
    elif is_genuine_displacement(prior, new_agent_instance_id, new_agent_session_id):
        target_agent_id, target_instance_id = displaced_target(prior, peer_registry)
        send_handover_notice(
            bridge_manager=bridge_manager,
            peer_registry=peer_registry,
            agent_messaging_service=agent_messaging_service,
            state_service=state_service,
            peer_id=target_agent_id or new_agent_id,
            peer_agent_instance_id=target_instance_id,
            peer_agent_session_id=str(getattr(prior, "agent_session_id", "") or ""),
            prose=displaced_prose(name, new_agent_instance_id),
            kind="displaced-holder",
        )
    send_handover_notice(
        bridge_manager=bridge_manager,
        peer_registry=peer_registry,
        agent_messaging_service=agent_messaging_service,
        state_service=state_service,
        peer_id=new_agent_id,
        peer_agent_instance_id=new_agent_instance_id,
        peer_agent_session_id=new_agent_session_id,
        prose=new_holder_prose(name),
        kind="new-holder",
    )


def settle_role_handover(
    *,
    bridge_manager: BridgeSessionManager | None,
    peer_registry: PeerRegistry | None,
    agent_messaging_service: Any | None,
    state_service: StateManagementInterface | None,
    name: str,
    agent_id: str,
    agent_instance_id: str,
    agent_session_id: str,
    outcome: dict[str, Any],
) -> RoleClaimSuccess:
    """Consume ``claim_role_binding_v4``'s outcome (action + pre-CAS prior) + fire
    the REL-04/§5.4 handover notices (§9 CUTOVER — the v4 claim now decides
    self-re-claim vs displace).

    ``action='refreshed'`` = an idempotent self-re-claim → report
    ``action='updated'`` (the ``/rename`` refresh contract) and fire NO wake.
    ``'claimed'`` (fresh) / ``'displaced'`` (prior set) → notify: a displaced prior
    at its current bridge (§5.4) + the new-holder confirm. The new holder is always
    a SESSION here (the claimant), so carry-forward (d) [confirm-iff-session] is
    satisfied by construction — no kind gate needed.

    The v4 outcome carries ``prior`` (a ``ResolvedRole``) for the notify ONLY — it
    is NOT json-serializable, so it MUST NOT reach the public result (result
    persistence json.dumps would TypeError on a real displace — Codex BLOCKER-1).
    Returning a :class:`RoleClaimSuccess` is what structurally prevents that:
    ``prior`` has nowhere to ride along. ``agent_session_id`` is the RESOLVED id
    (the caller may omit it and have it sourced from ``peer_binding``), so echoing
    it is what tells the claimant which session id its role is now keyed on.
    """
    action = str(outcome.get("action") or "")
    resolved_name = str(outcome.get("name") or name)
    resolved_instance = str(outcome.get("agent_instance_id") or agent_instance_id)
    if action == "refreshed":
        # /rename refresh contract: an idempotent self-re-claim reports
        # ``updated`` and fires NO wake. This is the branch that keeps a
        # steady-state re-assert from waking its own session forever.
        return RoleClaimSuccess(
            action="updated",
            name=resolved_name,
            agent_instance_id=resolved_instance,
            agent_session_id=agent_session_id,
        )
    notify_role_handover(
        bridge_manager=bridge_manager,
        peer_registry=peer_registry,
        agent_messaging_service=agent_messaging_service,
        state_service=state_service,
        name=name,
        new_agent_id=agent_id,
        new_agent_instance_id=agent_instance_id,
        new_agent_session_id=agent_session_id,
        prior=outcome.get("prior"),
    )
    return RoleClaimSuccess(
        action=action,
        name=resolved_name,
        agent_instance_id=resolved_instance,
        agent_session_id=agent_session_id,
    )



def _current_holder(state_service: Any, name: str) -> ResolvedRole | None:
    """The role's current holder, or ``None`` when there is nothing to protect.

    A vacant or malformed binding reads as "no holder": this gate exists to
    stop a silent hijack of a LIVE session, so an unreadable row must never
    become a new way for a legitimate claim to fail.
    """
    try:
        return resolve_role_binding_v4(state_service, name)
    except (RoleBindingVacantError, RoleBindingMalformedError):
        return None


def _holder_is_live(
    *,
    bridge_manager: BridgeSessionManager,
    peer_registry: PeerRegistry,
    holder_session_id: str,
) -> tuple[bool, bool]:
    """``(is_live, ambiguous)`` for the holder's session.

    Ambiguity counts as LIVE: more than one binding under one session id is
    precisely the confusion this gate surfaces, so it must refuse rather than
    wave a claim through on an unresolvable holder.
    """
    try:
        binding = peer_registry.resolve_by_agent_session_id(holder_session_id)
    except PeerSessionAmbiguousError:
        return True, True
    if binding is None:
        return False, False
    return binding_is_live(
        bridge_manager=bridge_manager,
        binding=binding,
        window_seconds=bridge_manager.binding_liveness_window_s,
    ), False


def _protects_a_live_holder(
    *,
    holder: ResolvedRole,
    agent_session_id: str,
    agent_instance_id: str,
) -> bool:
    """Is this holder a DIFFERENT session than the claimant?

    Self-refresh (same session id) and the same instance re-claiming are both
    the claimant itself and are never refused.
    """
    if not holder.agent_session_id:
        return False
    if holder.agent_session_id == agent_session_id:
        return False
    return holder.agent_instance_id != agent_instance_id


def _live_holder_refusal(
    *,
    state_service: Any,
    bridge_manager: BridgeSessionManager | None,
    peer_registry: PeerRegistry | None,
    name: str,
    agent_session_id: str,
    agent_instance_id: str,
    takeover: bool,
) -> RoleClaimFailure | None:
    """Refuse a claim against a LIVE different-session holder (§4.3.2).

    Returns the refusal, or ``None`` to let the claim proceed. Fails OPEN on
    every uncertainty — explicit takeover, a missing collaborator, a vacant or
    unreadable binding, a holder with no live binding, or a holder DEAD by the
    window all allow the claim. Crash succession in particular must stay cheap:
    a gate that also refused dead holders would strand every role behind a
    session that can no longer release it.
    """
    if takeover or bridge_manager is None or peer_registry is None:
        return None
    holder = _current_holder(state_service, name)
    if holder is None or not _protects_a_live_holder(
        holder=holder,
        agent_session_id=agent_session_id,
        agent_instance_id=agent_instance_id,
    ):
        return None
    is_live, ambiguous = _holder_is_live(
        bridge_manager=bridge_manager,
        peer_registry=peer_registry,
        holder_session_id=holder.agent_session_id,
    )
    if not is_live:
        return None
    where = "ambiguous (>1 live binding)" if ambiguous else "live"
    return RoleClaimFailure(
        code="role_held_live",
        message=(
            f"role {name!r} is held by a {where} session: label "
            f"{holder.session_label!r}, instance {holder.agent_instance_id}, "
            f"session {holder.agent_session_id}. Claiming would take its "
            f"deliveries. Re-run with an explicit takeover after confirming "
            f"with the operator, or wait for the holder to release it."
        ),
    )


def _claim_preflight_refusal(
    *,
    name: str,
    agent_id: str,
    agent_instance_id: str,
    state_service: Any | None,
    call_context: Any | None,
) -> RoleClaimFailure | None:
    """Argument and reserved-keyspace checks — both fail-closed.

    Split out of :func:`claim_role_for_session` so the decision table below
    reads as the decision table it is. These are unrelated to liveness: they
    refuse malformed input and a claim the caller may not make at all.

    The ``state_service is None`` check deliberately stays in the caller: it is
    what narrows the type for every downstream use, and extracting it would
    force either a duplicate guard or an ``assert`` on a load-bearing path.
    """
    if not name or not agent_id or not agent_instance_id:
        return RoleClaimFailure(
            code="missing_argument",
            message=(
                "peer_claim_role requires non-empty 'name', "
                "'agent_id', and 'agent_instance_id'."
            ),
        )
    # §6.1 reserved-keyspace gate: a general claim never assigns a SESSION-FILLED
    # system slot (that is the §D.9 auto-assignment lane), and a PLUGIN-OWNED slot
    # is claimable ONLY by its declared owner — verified against the SERVER-BUILT
    # principal, never spoofable caller input. A normal (non-sys:) role →
    # NOT_SYSTEM → proceeds.
    verdict = evaluate_system_slot_claim(name, call_context)
    if verdict.decision is SystemSlotClaimDecision.REJECT:
        return RoleClaimFailure(
            code="system_slot_claim_denied",
            message=verdict.reason,
        )
    return None


def _role_entity_exists(state_service: Any, name: str) -> bool:
    """Does the first-class ``role`` entity row for ``name`` already exist?

    Distinguishes a FRESH MINT (no row yet) from a claim against an
    already-legislated name — the reserved-mint guard below only fires on the
    former (§3.1 / Dawn ruling Q1: peer_claim_role is enforce-by-class, never
    class-assignment; only a fresh mint of a reserved-pattern name is refused).
    """
    result = state_service.query_state(
        AGENT_ROLE_BINDING_NAMESPACE,
        {"table": TABLE_ROLE, "filters": {"external_id": role_binding_external_id(name)}},
    )
    return bool(require_records(result))


def _reserved_mint_refusal(state_service: Any, name: str) -> RoleClaimFailure | None:
    """Refuse a FRESH MINT of a reserved primary-seat name (``<solet>-Main``
    shape) — the ONE D1-scoped claim-path guard from §3.1 (Dawn ruling Q1).
    ``sys:*`` names are already keyspace-rejected upstream by
    ``evaluate_system_slot_claim``; this guards the DIFFERENT ``-Main`` pattern,
    and only when no role row exists yet. A name already legislated (via this
    plugin's ``legislate_role`` governance-act verb, outside D1) claims
    normally regardless of this check.
    """
    if not is_reserved_primary_name(name) or is_system_role(name):
        return None
    if _role_entity_exists(state_service, name):
        return None
    return RoleClaimFailure(
        code="reserved_role_name",
        message=(
            f"role {name!r} matches the reserved primary-seat pattern "
            "(<solet>-Main) and has no legislated role row yet — minting "
            "it via an ordinary claim is refused. Primary-seat legislation is "
            "a governance act (this plugin's legislate_role verb), not a "
            "session claim."
        ),
    )


def _mint_and_cardinality_gate_refusal(
    state_service: Any, *, name: str, agent_session_id: str, agent_instance_id: str,
) -> RoleClaimFailure | None:
    """Fleet session-management Phase B, D1 (§3.1 reserved-mint guard + §2
    AMEND-4b cardinality gate, Dawn ruling gap 2). Evaluated BEFORE the
    binding CAS so a refusal mutates nothing beyond a harmless role-entity
    upsert (§5.5 — a lost CAS then leaves at most an orphan entity, never
    read on resolve). ``sys:*`` slots are slot machinery, exempt from the
    cardinality count (§2) — they keep the existing §6.1 gate untouched, no
    ``session_role_claim`` row.

    TRUST MODEL (``session_role_claim_store`` module docstring, Q2): keyed on
    ``agent_session_id`` at the SAME trust level the existing binding CAS
    already operates at — genuine against an honestly-identified session,
    NOT hardened against a forged identity pair (tracked debt,
    ``reference_peer_claim_role_trusts_caller_asserted_instance_id``).

    Returns the refusal, or ``None`` to let the claim proceed (a system role
    always returns ``None`` here — its gate is untouched).
    """
    reserved = _reserved_mint_refusal(state_service, name)
    if reserved is not None:
        return reserved
    # §5.5 entity-first: upsert the role entity BEFORE the binding CAS.
    upsert_role_entity(state_service, name=name)
    if is_system_role(name):
        return None
    try:
        win_cardinality_gate(
            state_service,
            CardinalityGatedClaim(
                agent_session_id=agent_session_id,
                requested_role=name,
                agent_instance_id=agent_instance_id,
            ),
        )
    except CardinalityConflictError as exc:
        return RoleClaimFailure(code="cardinality_conflict", message=str(exc))
    except SessionRoleClaimContendedError as exc:
        return RoleClaimFailure(code="session_role_claim_contended", message=str(exc))
    return None


def _prune_displaced_session_role_claim(
    state_service: Any, *, name: str, outcome: dict[str, Any],
) -> None:
    """Displacer cleanup (Dawn ruling gap 1): prune the LOSER's own
    ``session_role_claim`` row so it does not linger as a stale orphan
    (harmless either way — the loser's own next claim self-repairs via
    branch (iii) — but pruning now keeps the common case clean). No-op for
    ``sys:*`` names (they never win a ``session_role_claim`` row) or a
    non-displacing outcome.
    """
    if is_system_role(name) or str(outcome.get("action") or "") != "displaced":
        return
    prior = outcome.get("prior")
    prior_session_id = str(getattr(prior, "agent_session_id", "") or "")
    if prior_session_id:
        delete_session_role_claim_if_still_holds(
            state_service, agent_session_id=prior_session_id, expected_held_role=name,
        )


def _log_operator_takeover(
    *,
    takeover: bool,
    name: str,
    agent_id: str,
    agent_instance_id: str,
    agent_session_id: str,
    outcome: dict[str, Any],
) -> None:
    """Audit a successful explicit takeover with both routing identities."""
    if not takeover or str(outcome.get("action") or "") != "displaced":
        return
    prior = outcome.get("prior")
    logger.warning(
        "operator-confirmed role takeover: name=%r claimer=%s/%s "
        "session=%s displaced=%s/%s session=%s",
        name,
        agent_id,
        agent_instance_id,
        agent_session_id,
        str(getattr(prior, "agent_id", "") or "unknown"),
        str(getattr(prior, "agent_instance_id", "") or "unknown"),
        str(getattr(prior, "agent_session_id", "") or "unknown"),
    )


def claim_role_for_session(
    *,
    origin: RoleClaimOrigin,
    name: str,
    agent_id: str,
    agent_instance_id: str,
    agent_session_id: str,
    session_label: str,
    state_service: Any | None,
    bridge_manager: BridgeSessionManager | None,
    peer_registry: PeerRegistry | None,
    agent_messaging_service: Any | None,
    call_context: Any | None,
    takeover: bool = False,
) -> RoleClaimResult:
    """Claim-or-replace the ``agent_role_binding`` row for ``name`` (v10 #2.C).

    The single body behind both transports. ``origin`` is recorded, never acted
    on — see the module docstring.

    ``call_context`` MUST be the SERVER-BUILT context so the §6.1 reserved-
    keyspace gate cannot be forged. The verb lifts it out of ``state``; the
    bridge route passes ``None``, which is correct AND fail-closed: with no
    plugin principal every ``sys:`` name is refused, and the forwarder has no
    business claiming a system slot. A normal role is unaffected either way.
    """
    name = name.strip()
    agent_id = agent_id.strip()
    agent_instance_id = agent_instance_id.strip()
    agent_session_id = agent_session_id.strip()
    session_label = session_label or name
    preflight = _claim_preflight_refusal(
        name=name,
        agent_id=agent_id,
        agent_instance_id=agent_instance_id,
        state_service=state_service,
        call_context=call_context,
    )
    if preflight is not None:
        return preflight
    if state_service is None:
        return RoleClaimFailure(
            code="state_service_unavailable",
            message="state_service is not bound on this solet.",
        )
    # REL-07(1): source the claimant's stable session id from its OWN live
    # peer_binding row when the claim args omit it (they always do). An empty
    # agent_session_id makes the reconnect CAS (keyed on it alone) unable to
    # re-point this role — the class fresh-C's REL-07 diagnostic surfaced.
    if not agent_session_id and peer_registry is not None:
        agent_session_id = peer_registry.agent_session_id_for_instance(
            agent_instance_id,
        )
    # carry-forward (c) [§4.5.3/§11]: a durable session claim MUST carry a stable
    # agent_session_id (the reconnect CAS + the §D.9 succession key on it). The
    # pre-cutover 'no worse than pre-fix' empty-allowed fallback DIES at the §9
    # cutover — reject an unsourceable session claim rather than write a dead binding.
    if session_claim_requires_session_id(HOLDER_KIND_SESSION, agent_session_id):
        return RoleClaimFailure(
            code="missing_session_id",
            message=(
                "peer_claim_role requires a non-empty agent_session_id for a "
                "session holder; the claimant's peer_binding carried none "
                "(launch with the session-id carrier exported, or pass it)."
            ),
        )
    # WS-2e §4.3.2 claim decision table, evaluated BEFORE the CAS so a refusal
    # mutates nothing. Only ONE row of the table refuses: a DIFFERENT session
    # that is LIVE, claiming without explicit takeover.
    #
    # Every other row passes exactly as before — vacant, self-refresh, and
    # crash-succession from a DEAD holder. Succession staying cheap is the
    # load-bearing half: a liveness check that refused dead holders too would
    # strand every role behind a session that can no longer release it.
    held = _live_holder_refusal(
        state_service=state_service,
        bridge_manager=bridge_manager,
        peer_registry=peer_registry,
        name=name,
        agent_session_id=agent_session_id,
        agent_instance_id=agent_instance_id,
        takeover=takeover,
    )
    if held is not None:
        return held
    gate_refusal = _mint_and_cardinality_gate_refusal(
        state_service, name=name, agent_session_id=agent_session_id,
        agent_instance_id=agent_instance_id,
    )
    if gate_refusal is not None:
        return gate_refusal
    # §9 CUTOVER: the v4 predicated-CAS claim (claim / displace / self-reclaim in
    # one) returns action + the PRE-CAS displaced prior for the §5.4 notify.
    outcome = claim_role_binding_v4(
        state_service,
        name=name,
        claim=HolderClaim(
            holder_kind=HOLDER_KIND_SESSION,
            holder_identity={"agent_id": agent_id, "session_label": session_label},
            agent_instance_id=agent_instance_id,
            agent_session_id=agent_session_id,
            session_label=session_label,
        ),
    )
    _prune_displaced_session_role_claim(state_service, name=name, outcome=outcome)
    _log_operator_takeover(
        takeover=takeover,
        name=name,
        agent_id=agent_id,
        agent_instance_id=agent_instance_id,
        agent_session_id=agent_session_id,
        outcome=outcome,
    )
    settled = settle_role_handover(
        bridge_manager=bridge_manager,
        peer_registry=peer_registry,
        agent_messaging_service=agent_messaging_service,
        state_service=state_service,
        name=name,
        agent_id=agent_id,
        agent_instance_id=agent_instance_id,
        agent_session_id=agent_session_id,
        outcome=outcome,
    )
    logger.debug(
        "role claim settled: name=%r action=%s origin=%s agi=%s",
        name, settled.action, origin.value, agent_instance_id,
    )
    return settled


__all__ = [
    "RoleClaimFailure",
    "RoleClaimOrigin",
    "RoleClaimResult",
    "RoleClaimSuccess",
    "claim_role_for_session",
    "displaced_prose",
    "displaced_target",
    "is_genuine_displacement",
    "new_holder_prose",
    "notify_role_handover",
    "send_handover_notice",
    "settle_role_handover",
]
