"""INF-01 sub-slice-2 — ``sys:autonomic`` auto-assignment lifecycle (§D.9).

The policy owner for the platform's autonomic inference slot: Trigger-1
vacancy-fill / crash-heal on ``peer_register``, Trigger-2 grace-delayed
succession-at-end on bridge close, the ``set_autonomic_slot`` manual
override, the REL-04 transition notices, and the first-claim drain of the
durable deferred-vertex queue. The hook BODIES live here (seam boundary
doc §b: INF-01 owns them); the CAS primitive (``claim_role_binding_v4``,
slice-B) and the slot declaration (slice-C) are consumed, never redefined.

Selection rule (seam §b, cross-validated): newest-live = ``max(created_at)``
over ``BridgeSessionManager.list_active()`` — NEVER over ``peer_binding``
rows alone, which are stale-inclusive (a crashed-without-close holder keeps
its row). Refinement (build decision D1): candidates are further filtered
to sessions with a LIVE inference provider — a holder without one cannot
serve the lane, so claiming it would strand every organism turn in DEFER.

The grace timer is in-process (``threading.Timer``, daemon; build decision
D2): the succession check is deterministic plugin logic, not a model turn.
A timer lost to restart is covered by Trigger-1 crash-heal on the next
register plus the post-flip DEFER queue. A holder that reconnects inside
the grace window is detected at expiry (live provider re-registered via the
S2 self-refresh ride-along) and the check NO-OPs — the spec-allowed path.

Collaborators arrive as injected callables so the whole lifecycle is
offline-smokeable against fakes (no FastAPI, no live bridge).
"""

from __future__ import annotations

import logging
import threading
from typing import TYPE_CHECKING, Any, Protocol

from ananta.llm.agent_messaging.role_binding import (
    HOLDER_KIND_SESSION,
    SYS_AUTONOMIC_SLOT,
)
from ananta.services.inference_service.deferred_vertex_queue import hard_delete_flows
from ananta.services.inference_service.schema import (
    COL_FLOW_ID,
    COL_ROLE,
    COL_STATE,
    INFERENCE_DEFERRED_VERTEX_NAMESPACE,
    STATE_DEFERRED,
    STATE_FORWARDED,
    TABLE_INFERENCE_DEFERRED_VERTEX,
)
from ananta.services.state_service.bounded_read import iter_table_rows

from .completion_reconcile import CompletionReconciler, live_session_holder_id
from .forwarded_vertex_reconcile import ForwardedVertexReconciler
from .role_binding_store import (
    HolderClaim,
    ResolvedRole,
    RoleBindingVacantError,
    claim_role_binding_v4,
    resolve_role_binding_v4,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from .models import BridgeBinding

logger = logging.getLogger(__name__)

# How many undrained flow_ids the loud remainder log enumerates before
# eliding — keeps the log line bounded while staying auditable.
_DRAIN_LOG_SAMPLE = 20

_DRAIN_QUEUE_CEILING = 1_000_000
_DRAIN_QUEUE_CEILING_REASON = (
    "first-claim snapshot covers every live redrivable sys:autonomic vertex "
    "before resubmit mutates the queue"
)


class _LiveBridge(Protocol):
    """The two ``BridgeSessionState`` fields selection reads (duck-typed)."""

    @property
    def bridge_id(self) -> str: ...

    @property
    def created_at(self) -> str: ...


def select_newest_live(
    bridges: list[_LiveBridge],
    *,
    bindings_for_bridge: Callable[[str], list[BridgeBinding]],
    has_live_provider: Callable[[str], bool],
    exclude_agent_session_id: str = "",
    exclude_agent_instance_id: str = "",
) -> BridgeBinding | None:
    """Pure successor selection: newest-live, provider-capable, not-the-departed.

    ``created_at`` is ISO-8601 (lexicographically sortable — verified
    grounding), so the string ``max`` IS the newest bridge. Returns the
    winning bridge's binding, or ``None`` when no eligible candidate exists
    (→ the caller leaves the slot vacant, the post-flip DEFER-loud lane).
    """
    best: tuple[str, BridgeBinding] | None = None
    for bridge in bridges:
        for binding in bindings_for_bridge(bridge.bridge_id):
            if (
                exclude_agent_session_id
                and binding.agent_session_id == exclude_agent_session_id
            ) or binding.agent_instance_id == exclude_agent_instance_id:
                continue
            if not has_live_provider(binding.agent_instance_id):
                continue
            if best is None or bridge.created_at > best[0]:
                best = (bridge.created_at, binding)
    return None if best is None else best[1]


def _new_holder_prose() -> str:
    return (
        f"IMPORTANT: You now hold the {SYS_AUTONOMIC_SLOT!r} system slot — "
        "this session is the platform's autonomic inference lane. Organism "
        "error/result turns route to you until another session takes the slot."
    )


def _displaced_prose(new_agent_instance_id: str) -> str:
    return (
        f"IMPORTANT: You no longer hold the {SYS_AUTONOMIC_SLOT!r} system "
        f"slot — it was re-bound to instance {new_agent_instance_id!r}. "
        "Organism error/result turns no longer route to this session."
    )


class AutonomicAssignment:
    """Trigger-1/2 + manual-set orchestration for the ``sys:autonomic`` slot.

    One instance per plugin lifetime, built in ``start_interface`` with the
    live collaborators; ``http_routes`` receives :meth:`on_register` /
    :meth:`on_bridge_close` as plain callables (same pattern as the
    inference-provider sidecar callbacks).
    """

    def __init__(
        self,
        *,
        state_service: Callable[[], Any],
        list_active_bridges: Callable[[], list[Any]],
        bindings_for_bridge: Callable[[str], list[BridgeBinding]],
        live_binding_for_session: Callable[[str], BridgeBinding | None],
        has_live_provider: Callable[[str], bool],
        send_notice: Callable[..., bool],
        grace_seconds: int,
        forward_completion: Callable[[str, dict[str, object]], None],
        serve_window_seconds: int,
        resubmit_vertex: Callable[[str, str], bool],
        forward_serve_window_seconds: int,
        forward_attempts_cap: int,
        terminal_gc_after_seconds: int,
    ) -> None:
        self._state_service = state_service
        self._list_active_bridges = list_active_bridges
        self._bindings_for_bridge = bindings_for_bridge
        self._live_binding_for_session = live_binding_for_session
        self._has_live_provider = has_live_provider
        self._send_notice = send_notice
        self._grace_seconds = grace_seconds
        # INF-06 reliability — the forwarded-vertex serve-timeout re-drive + GC
        # (PUBLIC — the sweeper's on_tick rider targets it), held exactly as the
        # INF-02 completion reconciler below. It owns the durable ``forwarded``-row
        # lifecycle; the injected ``resubmit_vertex`` re-enters the organism turn
        # for a flow_id against CURRENT durable state (fresh decode, NEVER replay).
        # The first-claim drain shares its RESUBMIT bridge (``resubmit_flow``).
        self.forwarded = ForwardedVertexReconciler(
            state=self._state,
            resubmit_vertex=resubmit_vertex,
            serve_window_seconds=forward_serve_window_seconds,
            attempts_cap=forward_attempts_cap,
            terminal_gc_after_seconds=terminal_gc_after_seconds,
        )
        # INF-02 per-type completion handlers (PUBLIC — the sweeper's
        # on_tick rider targets it); resolution rides THIS instance's seams.
        self.completions = CompletionReconciler(
            state_service=self._state,
            forward_completion=forward_completion,
            resolve_live_session_holder=lambda: live_session_holder_id(
                self._resolve_slot, self._holder_is_live),
            serve_window_seconds=serve_window_seconds,
        )
        self._timers_lock = threading.Lock()
        self._timers: dict[str, threading.Timer] = {}

    # ------------------------------------------------------------------
    # Trigger-1 — vacancy-fill / crash-heal on peer_register
    # ------------------------------------------------------------------

    def on_register(
        self,
        *,
        agent_id: str,
        agent_instance_id: str,
        agent_session_id: str,
        session_label: str,
        provides_inference: bool,
    ) -> str:
        """Fill a vacant (or dead-holder) slot with the just-registered session.

        Runs AFTER the sidecar populate + the S2 self-refresh in the register
        route, so a reconnecting holder has already re-pointed its binding and
        re-registered its provider — it resolves as live-held → ``held``.
        NEVER raises: registration must succeed regardless (token ``error``).
        """
        if not provides_inference:
            return "not_provider"
        try:
            holder = self._resolve_slot()
            if holder is not None and self._holder_is_live(holder):
                return "held"
            reason = "vacancy_fill" if holder is None else "crash_heal"
            self._claim_for(
                agent_id=agent_id,
                agent_instance_id=agent_instance_id,
                agent_session_id=agent_session_id,
                session_label=session_label,
                reason=reason,
            )
        except Exception:  # noqa: BLE001 — the register response must not fail on lifecycle policy
            logger.exception(
                "sys:autonomic Trigger-1 FAULTED for agi=%s — registration "
                "kept; the slot state is unchanged", agent_instance_id,
            )
            return "error"
        return reason

    # ------------------------------------------------------------------
    # Trigger-2 — grace-delayed succession at end (bridge close)
    # ------------------------------------------------------------------

    def on_bridge_close(self, bridge_id: str) -> str:
        """Schedule the grace-delayed succession check when the HOLDER departs.

        MUST run BEFORE ``peer_registry.unregister(bridge_id)`` — the departing
        bindings are only enumerable pre-unregister. AT-END only: nothing is
        promoted while the holder lives; the check fires after the grace window
        and NO-OPs if the holder reconnected. NEVER raises (token ``error``).
        """
        try:
            holder = self._resolve_slot()
            if holder is None:
                return "vacant"
            if not self._holder_departs_with(holder, bridge_id):
                return "not_holder"
            self._schedule_succession(holder)
        except Exception:  # noqa: BLE001 — close must proceed regardless of lifecycle policy
            logger.exception(
                "sys:autonomic Trigger-2 scheduling FAULTED for bridge=%s — "
                "close proceeds; succession will heal on the next register",
                bridge_id,
            )
            return "error"
        return "scheduled"

    def _holder_departs_with(self, holder: ResolvedRole, bridge_id: str) -> bool:
        """Is the slot holder among the bindings this closing bridge owns?"""
        for binding in self._bindings_for_bridge(bridge_id):
            if holder.agent_session_id and (
                binding.agent_session_id == holder.agent_session_id
            ):
                return True
            if binding.agent_instance_id == holder.agent_instance_id:
                return True
        return False

    def _schedule_succession(self, departed: ResolvedRole) -> None:
        """(Re)arm the grace timer for ``departed``; replaces a prior timer."""
        key = departed.agent_session_id or departed.agent_instance_id
        timer = threading.Timer(
            self._grace_seconds, self._succession_check, args=(departed,),
        )
        timer.daemon = True
        with self._timers_lock:
            prior = self._timers.pop(key, None)
            self._timers[key] = timer
        if prior is not None:
            prior.cancel()
        timer.start()
        logger.info(
            "sys:autonomic holder departed (session=%r agi=%s) — succession "
            "check in %ds", departed.agent_session_id,
            departed.agent_instance_id, self._grace_seconds,
        )

    def _succession_check(self, departed: ResolvedRole) -> str:
        """Grace-expiry body: NO-OP on reconnect/supersession, else succeed-or-vacant.

        Public-in-behavior (the smokes drive it directly, bypassing the
        timer); returns a token for observability. NEVER raises — it runs on
        a timer thread with no caller to propagate to.
        """
        key = departed.agent_session_id or departed.agent_instance_id
        with self._timers_lock:
            self._timers.pop(key, None)
        try:
            return self._run_succession(departed)
        except Exception:  # noqa: BLE001 — timer thread: loud, never propagates
            logger.exception(
                "sys:autonomic succession check FAULTED (departed session=%r) "
                "— slot state unchanged; heals on the next register",
                departed.agent_session_id,
            )
            return "error"

    def _run_succession(self, departed: ResolvedRole) -> str:
        current = self._resolve_slot()
        if current is not None:
            if not self._same_holder(current, departed):
                return "superseded"
            if self._holder_is_live(current):
                return "reconnected"
        successor = select_newest_live(
            self._list_active_bridges(),
            bindings_for_bridge=self._bindings_for_bridge,
            has_live_provider=self._has_live_provider,
            exclude_agent_session_id=departed.agent_session_id,
            exclude_agent_instance_id=departed.agent_instance_id,
        )
        if successor is None:
            logger.warning(
                "sys:autonomic left VACANT after grace (departed session=%r, "
                "no live provider-capable successor) — organism turns DEFER "
                "to the durable queue until the next register fills the slot",
                departed.agent_session_id,
            )
            return "vacant"
        self._claim_for(
            agent_id=successor.agent_id,
            agent_instance_id=successor.agent_instance_id,
            agent_session_id=successor.agent_session_id,
            session_label=successor.session_label,
            reason="succession",
        )
        return "succeeded"

    @staticmethod
    def _same_holder(current: ResolvedRole, departed: ResolvedRole) -> bool:
        if current.agent_session_id and departed.agent_session_id:
            return current.agent_session_id == departed.agent_session_id
        return current.agent_instance_id == departed.agent_instance_id

    # ------------------------------------------------------------------
    # Manual-set (the set_autonomic_slot verb body)
    # ------------------------------------------------------------------

    def set_slot(self, *, agent_instance_id: str) -> dict[str, object]:
        """Bind the slot to ``agent_instance_id`` (override-then-resume, §D.9).

        Any bridge session may target any session — but ONLY a live,
        provider-capable one: binding a session that cannot serve inference
        would strand every organism turn in DEFER, so that is a fail-fast
        rejection, not a claim.
        """
        if not self._has_live_provider(agent_instance_id):
            return {
                "success": False,
                "code": "no_live_provider",
                "message": (
                    f"instance {agent_instance_id!r} has no live inference "
                    "provider (not registered with provides_inference=True, "
                    "or its bridge is closed) — binding it would strand the "
                    "autonomic lane"
                ),
            }
        binding = self._binding_for_instance(agent_instance_id)
        if binding is None:
            return {
                "success": False,
                "code": "not_registered",
                "message": (
                    f"instance {agent_instance_id!r} has no live peer "
                    "registration on any open bridge"
                ),
            }
        outcome = self._claim_for(
            agent_id=binding.agent_id,
            agent_instance_id=binding.agent_instance_id,
            agent_session_id=binding.agent_session_id,
            session_label=binding.session_label,
            reason="manual_set",
        )
        return {
            "success": True,
            "action": outcome.get("action"),
            "name": SYS_AUTONOMIC_SLOT,
            "agent_instance_id": binding.agent_instance_id,
        }

    def _binding_for_instance(self, agent_instance_id: str) -> BridgeBinding | None:
        for bridge in self._list_active_bridges():
            for binding in self._bindings_for_bridge(bridge.bridge_id):
                if binding.agent_instance_id == agent_instance_id:
                    return binding
        return None

    # ------------------------------------------------------------------
    # Shared claim + REL-04 notices + first-claim drain
    # ------------------------------------------------------------------

    def _claim_for(
        self,
        *,
        agent_id: str,
        agent_instance_id: str,
        agent_session_id: str,
        session_label: str,
        reason: str,
    ) -> dict[str, object]:
        """CAS the slot to the given session, notify, and drain a re-lit lane."""
        if not agent_session_id:
            # §4.5.3: succession + reconnect-refresh both key on the stable
            # session id. A carrier-less session is claimable (better than a
            # vacant lane) but degrades those paths to agent_instance_id.
            logger.warning(
                "sys:autonomic claim for agi=%s carries NO agent_session_id "
                "(no AGENT_SESSION_ID carrier) — reconnect self-refresh "
                "and sid-keyed succession degrade to instance identity",
                agent_instance_id,
            )
        prior_was_live = self._slot_serves_inference()
        outcome = claim_role_binding_v4(
            self._state(),
            name=SYS_AUTONOMIC_SLOT,
            claim=HolderClaim(
                holder_kind=HOLDER_KIND_SESSION,
                holder_identity={
                    "agent_id": agent_id, "session_label": session_label,
                },
                agent_instance_id=agent_instance_id,
                agent_session_id=agent_session_id,
                session_label=session_label,
            ),
        )
        action = str(outcome.get("action") or "")
        logger.info(
            "sys:autonomic %s (%s): holder now agi=%s session=%r label=%r",
            action, reason, agent_instance_id, agent_session_id, session_label,
        )
        if action != "refreshed":
            self._fire_notices(outcome, new_agent_id=agent_id,
                               new_agent_instance_id=agent_instance_id)
        if not prior_was_live:
            self._drain_deferred(reason)
        # INF-02 per-type completion handler — shared drain MECHANISM, its
        # OWN table (Reviewer-A rider); runs on EVERY claim (away-transition
        # requeue + backlog forward — completion_reconcile.py).
        self.completions.reconcile(reason, new_holder=agent_instance_id)
        return outcome

    def _fire_notices(
        self,
        outcome: dict[str, object],
        *,
        new_agent_id: str,
        new_agent_instance_id: str,
    ) -> None:
        """REL-04 transition notices (build decision D5).

        The successor always hears it holds the slot. A displaced prior holder
        hears it ONLY when it is genuinely LIVE somewhere (§5.4 both-party) —
        a genuinely-ended holder gets none (crash-heal / succession-at-end),
        and a non-session holder has no wake target (log-loud audit).
        """
        prior = outcome.get("prior")
        if isinstance(prior, ResolvedRole):
            if prior.holder_kind != HOLDER_KIND_SESSION:
                logger.warning(
                    "sys:autonomic re-bound away from a %r holder "
                    "(identity=%s) — no wake target; audit only",
                    prior.holder_kind, prior.holder_identity,
                )
            else:
                live = self._prior_live_binding(prior)
                if live is not None:
                    self._send_notice(
                        peer_id=live.agent_id,
                        peer_agent_instance_id=live.agent_instance_id,
                        prose=_displaced_prose(new_agent_instance_id),
                        kind="autonomic-displaced",
                    )
        self._send_notice(
            peer_id=new_agent_id,
            peer_agent_instance_id=new_agent_instance_id,
            prose=_new_holder_prose(),
            kind="autonomic-new-holder",
        )

    def _prior_live_binding(self, prior: ResolvedRole) -> BridgeBinding | None:
        """The displaced prior's CURRENT binding — only if its bridge is OPEN.

        ``peer_binding`` rows are stale-inclusive (a crashed-without-close
        session keeps its row), so resolving by the stable session id alone
        would mistake a genuinely-ended holder for a live one and fire an
        undeliverable displaced notice. Cross-check the resolved binding's
        bridge against the LIVE bridge set — the same liveness authority the
        successor selection uses.
        """
        if not prior.agent_session_id:
            return None
        try:
            live = self._live_binding_for_session(prior.agent_session_id)
        except Exception:  # noqa: BLE001 — ambiguous/faulted lookup → treat as not-live (no notice)
            logger.warning(
                "sys:autonomic displaced-notice lookup failed for session=%r "
                "— treating as genuinely ended (no notice)",
                prior.agent_session_id, exc_info=True,
            )
            return None
        if live is None:
            return None
        open_bridges = {b.bridge_id for b in self._list_active_bridges()}
        if live.bridge_id not in open_bridges:
            return None
        return live

    def _slot_serves_inference(self) -> bool:
        """Was the lane LIT (live provider-backed holder) before this claim?

        Drives the drain decision: a claim that re-lights a dark lane
        (vacant, dead holder) drains the deferred queue; a live→live
        displacement does not (nothing accumulated).
        """
        holder = self._resolve_slot()
        return holder is not None and self._holder_is_live(holder)

    def _drain_deferred(self, reason: str) -> tuple[int, int]:
        """First-claim drain of ``core__inference_deferred_vertex`` by role.

        Fires ONLY on a dark→lit claim (:meth:`_slot_serves_inference` was
        False — no live holder before this claim). Every live re-drivable row for
        the role is enumerated and offered to ``self.forwarded.resubmit_flow`` (the
        SUB-05 RESUBMIT primitive): ``deferred`` rows are the original vacancy-fill
        case;
        ``forwarded`` rows are re-driven here as the §3 holder-transition — the
        dark-lane invariant guarantees any ``forwarded`` row was bound to a
        now-dead holder, so immediate re-drive is warranted (don't wait out the
        serve-timeout sweep). Terminal ``failed`` rows are SKIPPED (the GC sweep
        reaps them; re-driving a capped flow would defeat the cap). Successfully
        re-driven rows are HARD-deleted (frees the unique ``flow_id`` slot; the
        re-driven vertex re-mints a fresh occupancy if it forwards/defers again).
        The remainder is LOG-LOUD with a bounded flow_id sample. Returns
        ``(drained, remaining)``.
        """
        state = self._state()
        redrivable = list(iter_table_rows(
            state,
            namespace=INFERENCE_DEFERRED_VERTEX_NAMESPACE,
            table=TABLE_INFERENCE_DEFERRED_VERTEX,
            filters={
                COL_ROLE: SYS_AUTONOMIC_SLOT,
                COL_STATE: [STATE_DEFERRED, STATE_FORWARDED],
            },
            ceiling=_DRAIN_QUEUE_CEILING,
            reason=_DRAIN_QUEUE_CEILING_REASON,
        ))
        drained: list[str] = []
        for row in redrivable:
            flow_id = str(row.get(COL_FLOW_ID) or "")
            if flow_id and self.forwarded.resubmit_flow(flow_id, row):
                drained.append(flow_id)
        hard_delete_flows(state, drained)
        remaining = len(redrivable) - len(drained)
        self._log_drain_outcome(reason, rows=redrivable, remaining=remaining)
        return len(drained), remaining

    @staticmethod
    def _log_drain_outcome(
        reason: str, *, rows: list[dict[str, object]], remaining: int,
    ) -> None:
        if remaining:
            sample = [
                str(row.get(COL_FLOW_ID) or "") for row in rows[:_DRAIN_LOG_SAMPLE]
            ]
            logger.warning(
                "sys:autonomic first-claim drain (%s): %d deferred flow(s) "
                "remain QUEUED (re-drive primitive is SUB-05; nothing is "
                "lost — rows persist durably). Sample flow_ids: %s%s",
                reason, remaining, sample,
                " …" if remaining > _DRAIN_LOG_SAMPLE else "",
            )
        elif rows:
            logger.info(
                "sys:autonomic first-claim drain (%s): all %d deferred "
                "flow(s) re-driven and hard-deleted", reason, len(rows),
            )

    # ------------------------------------------------------------------
    # Shared plumbing
    # ------------------------------------------------------------------

    def _state(self) -> Any:
        """The bound state service — fail LOUD when unbound (never a silent no-op)."""
        state = self._state_service()
        if state is None:
            raise RuntimeError(
                "state_service unbound — the sys:autonomic lifecycle cannot "
                "resolve/claim the slot without it",
            )
        return state

    def _resolve_slot(self) -> ResolvedRole | None:
        try:
            return resolve_role_binding_v4(self._state(), SYS_AUTONOMIC_SLOT)
        except RoleBindingVacantError:
            return None

    def _holder_is_live(self, holder: ResolvedRole) -> bool:
        """Does the current holder actually serve the lane right now?

        A non-session holder (``inference_provider`` headroom, §6.1) is
        treated as live — auto-assignment never displaces a plugin-owned
        binding; that is an operator/manual-set decision.
        """
        if holder.holder_kind != HOLDER_KIND_SESSION:
            return True
        return self._has_live_provider(holder.agent_instance_id)

    def cancel_all(self) -> None:
        """Cancel outstanding grace timers (plugin shutdown)."""
        with self._timers_lock:
            timers = list(self._timers.values())
            self._timers.clear()
        for timer in timers:
            timer.cancel()


__all__ = [
    "AutonomicAssignment",
    "select_newest_live",
]
