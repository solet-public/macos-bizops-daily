"""Spawn-path-guaranteed heartbeat lifecycle for macos_self_deployment_plugin.

Owns the three-phase loop that enforces invariant I2 of
``workbench/2026-06-05_bridge_port_routing_and_session_lifecycle_design.md``
under the strict-I2 verdict ratified in §6 Slice 2.5:

1. **Bind-wait inside the unified transient-state budget.** Polls the
   supplied lookup for the bridge HTTP port held by another plugin
   (production: ``agent_messaging_plugin.bridge_port``). Bridge bind
   happens as a starting-action after readiness, so a small ordering
   gap is normal — the budget absorbs it. **Strict-I2:** if the port
   never appears within budget, the ``sigterm_callback`` fires with
   :data:`FAILED_REGISTRATION_LOCAL_BRIDGE_PORT_NEVER_APPEARED`.
2. **Bounded-window first register inside the same unified budget.**
   Per the TLA spec's ``ChildSelfSigtermOnFailedRegistration`` action
   (line 411), the deadline is single: ``now - childSpawnedAt > RegDeadline``
   is the SIGTERM trigger from both ``spawning`` and ``bindingPort``
   states. Budget consumed by Phase 1 carries through. If registration
   never succeeds within remaining budget, ``sigterm_callback`` fires
   with :data:`FAILED_REGISTRATION_BOUNDED_WINDOW_EXPIRED`.
3. **Steady-state heartbeat with passive reconciliation.** Re-register
   on ``{'unknown_instance': True}`` without a budget — the budget
   gates only the cold-start window.

Lives outside :class:`MacosSelfDeploymentPlugin` so the class body stays
under the god-class threshold (`quality_gates/god_class_check.py` —
non-process LOC ≤ 500). Per the operator's 2026-05-25 framing:
decomposition into coherent submodules, not allowlist additions.

The bind-wait code path itself disappears in Slice 3 (per-color port
files retired; child knows its bound port via ``socket.getsockname()``
directly). Slice 2.5 holds the invariant across the Slice-2-to-Slice-3
transition — the rule must hold in EVERY commit, not just at endpoints.
"""

from __future__ import annotations

import logging
import os
import signal
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any, Final

from macos_self_deployment_plugin import process_identity
from macos_self_deployment_plugin.constants import (
    DEFAULT_BACKSTOP_TERM_VERIFY_SECONDS,
    DEFAULT_BRIDGE_PORT_POLL_INTERVAL_SECONDS,
    DEFAULT_HEARTBEAT_INTERVAL_SECONDS,
    DEFAULT_PRIOR_TERM_POLL_INTERVAL_SECONDS,
    DEFAULT_REGISTRATION_POLL_INTERVAL_SECONDS,
    DEFAULT_TRANSIENT_STATE_BUDGET_SECONDS,
    FAILED_REGISTRATION_BOUNDED_WINDOW_EXPIRED,
    FAILED_REGISTRATION_LOCAL_BRIDGE_PORT_NEVER_APPEARED,
    PLUGIN_NAME,
)
from macos_self_deployment_plugin.pending_finisher import (
    PendingFinisher,
    clear_pending_finisher,
    read_pending_finisher,
)
from macos_self_deployment_plugin.router_client import (
    RouterClient,
    RouterClientError,
)

PortLookup = Callable[[], int | None]
# Returns the release id ``current`` names, or ``None``. The backstop gates the
# pending-finisher on ``current == record.candidate_release_id`` so the record is
# inert until the swap it describes is observably durable (Codex round-2 B2·1).
CurrentReleaseLookup = Callable[[], str | None]
# ``token`` is one of the FAILED_REGISTRATION_* constants — the phase
# that missed the unified deadline. Production callback logs the token
# at ERROR + SIGTERMs the running process; smokes inject a recording
# stub so the test runner survives.
SigtermCallback = Callable[[str], None]
SetColorActive = Callable[[bool], None]
PostRegistrationCallback = Callable[[], None]


class PollerGateReactivation:
    """Restore a quiesced poller only after router authority returns to it.

    The gate is process-local, so a ``swap_rollback`` action running on the
    currently active process cannot restore the drained process directly.  Its
    heartbeat observes the router instead.  A current-self snapshot alone is
    deliberately insufficient: quiesce runs while self is still router-active,
    before the forward cutover is committed.  Only a transition through another
    active instance proves this process was drained and later reactivated.
    """

    def __init__(
        self,
        *,
        self_instance_id: str,
        set_color_active: SetColorActive,
    ) -> None:
        self._self_instance_id = self_instance_id
        self._set_color_active = set_color_active
        self._observed_other_active = False

    def observe(self, snapshot: dict[str, Any]) -> None:
        """Apply a proven other-active → self-active router transition."""
        active_instance_id = snapshot.get("active_instance_id")
        if not isinstance(active_instance_id, str):
            return
        if active_instance_id != self._self_instance_id:
            self._observed_other_active = True
            return
        if not self._observed_other_active:
            return
        self._set_color_active(True)
        self._observed_other_active = False


def run(
    *,
    client: RouterClient,
    self_color: str,
    self_instance_id: str,
    port_lookup: PortLookup,
    stop_event: threading.Event,
    sigterm_callback: SigtermCallback,
    logger: logging.Logger,
    set_color_active: SetColorActive,
    post_registration_callback: PostRegistrationCallback | None = None,
    pending_finisher_file: Path | None = None,
    current_release_lookup: CurrentReleaseLookup | None = None,
    budget_seconds: float = DEFAULT_TRANSIENT_STATE_BUDGET_SECONDS,
    streamable_port_lookup: PortLookup | None = None,
) -> None:
    """Three-phase heartbeat with one unified transient-state budget.

    ``budget_seconds`` covers Phases 1 + 2 together — bind-wait and
    register both draw from the same deadline. Phase 3 (steady-state
    heartbeat) is unbudgeted. ``sigterm_callback`` is invoked exactly
    once on budget expiry, with the structured token naming the phase
    that missed the deadline. Exits silently when ``stop_event`` fires
    or when the steady-state loop receives the stop signal.

    ``streamable_port_lookup`` (BLG-04) is best-effort and OUTSIDE the
    strict-I2 budget/SIGTERM contract entirely — unlike the bridge port,
    a streamable port that never appears (streamable disabled, or this
    solet doesn't run it) is a normal, permanent, non-fatal state, not a
    spawn-path failure. Whatever value the lookup returns (possibly
    ``None``, possibly not yet bound) rides the same ``register_color``
    calls Phases 2 and 3 already make; the router's ``register()``
    preserves the last-known value across a call that reports ``None``,
    so a lookup that resolves after the very first register still gets
    delivered on the next opportunity instead of being lost.
    """
    logger.info(
        "%s: heartbeat lifecycle starting color=%s instance=%s budget=%ss",
        PLUGIN_NAME, self_color, self_instance_id, budget_seconds,
    )
    deadline = time.monotonic() + budget_seconds
    port = _wait_for_bridge_port(port_lookup, stop_event, deadline)
    if port is None:
        if stop_event.is_set():
            return
        sigterm_callback(FAILED_REGISTRATION_LOCAL_BRIDGE_PORT_NEVER_APPEARED)
        return
    logger.info("%s: bridge port acquired port=%s", PLUGIN_NAME, port)
    if not _register_within_deadline(
        client=client,
        port=port,
        self_color=self_color,
        self_instance_id=self_instance_id,
        deadline=deadline,
        stop_event=stop_event,
        logger=logger,
        streamable_port_lookup=streamable_port_lookup,
    ):
        if stop_event.is_set():
            return
        sigterm_callback(FAILED_REGISTRATION_BOUNDED_WINDOW_EXPIRED)
        return
    logger.info(
        "%s: first register accepted; entering steady-state heartbeat",
        PLUGIN_NAME,
    )
    _notify_post_registration_if_active(
        client=client,
        self_instance_id=self_instance_id,
        logger=logger,
        callback=post_registration_callback,
    )
    _run_steady_state_heartbeat(
        client=client,
        port=port,
        self_color=self_color,
        self_instance_id=self_instance_id,
        stop_event=stop_event,
        pending_finisher_file=pending_finisher_file,
        current_release_lookup=current_release_lookup,
        logger=logger,
        set_color_active=set_color_active,
        streamable_port_lookup=streamable_port_lookup,
        post_registration_callback=post_registration_callback,
    )


def real_sigterm_callback(
    logger: logging.Logger,
    budget_seconds: float = DEFAULT_TRANSIENT_STATE_BUDGET_SECONDS,
) -> SigtermCallback:
    """Production ``sigterm_callback`` — logs the structured token + SIGTERMs self.

    Returns a closure so the plugin can wire it without owning a method
    body. The closure captures the plugin's logger so log lines remain
    attributable. Operators + smokes grep
    :data:`FAILED_REGISTRATION_LOCAL_BRIDGE_PORT_NEVER_APPEARED` or
    :data:`FAILED_REGISTRATION_BOUNDED_WINDOW_EXPIRED` in the log to
    confirm the spawn-path guarantee fired in either phase rather than
    the child silently sitting idle.
    """

    def _send(token: str) -> None:
        logger.error(
            "%s: %s — transient-state phase exceeded %ss unified budget; "
            "self-SIGTERM per strict-I2 spawn-path guarantee "
            "(invariant I2 of 2026-06-05_bridge_port_routing_and_session_lifecycle_design).",
            PLUGIN_NAME,
            token,
            budget_seconds,
        )
        os.kill(os.getpid(), signal.SIGTERM)

    return _send


# ---------------------------------------------------------------------
# Phase 1 — wait for the bridge HTTP server to bind its port.
# ---------------------------------------------------------------------


def _wait_for_bridge_port(
    port_lookup: PortLookup,
    stop_event: threading.Event,
    deadline: float,
) -> int | None:
    """Poll the supplied lookup until the bridge port appears or deadline.

    ``port_lookup`` returns the integer port held by another plugin
    (production: ``agent_messaging_plugin.bridge_port`` via the plugin
    manager). ``None`` until that plugin's ``start_interface`` allocates
    and binds; this loop absorbs the ordering window inside the unified
    budget. Returns the port or ``None`` if the deadline elapsed or
    ``stop_event`` fired.
    """
    while time.monotonic() < deadline and not stop_event.is_set():
        port = port_lookup()
        if port is not None:
            return port
        time.sleep(DEFAULT_BRIDGE_PORT_POLL_INTERVAL_SECONDS)
    return None


# ---------------------------------------------------------------------
# Phase 2 — bounded-window first registration against the same deadline.
# ---------------------------------------------------------------------


def _register_within_deadline(
    *,
    client: RouterClient,
    port: int,
    self_color: str,
    self_instance_id: str,
    deadline: float,
    stop_event: threading.Event,
    logger: logging.Logger,
    streamable_port_lookup: PortLookup | None = None,
) -> bool:
    """Retry registration until success OR the unified deadline OR stop fires.

    Phase 2 of the strict-I2 lifecycle: shares the deadline that Phase 1
    started against, so the budget Phase 1 consumed reduces what's
    available here. Returns True iff registered.
    """
    while time.monotonic() < deadline:
        if stop_event.is_set():
            return False
        streamable_port = (
            streamable_port_lookup() if streamable_port_lookup is not None else None
        )
        if _register_and_activate_if_needed(
            client=client,
            port=port,
            self_color=self_color,
            self_instance_id=self_instance_id,
            logger=logger,
            streamable_port=streamable_port,
        ):
            return True
        time.sleep(DEFAULT_REGISTRATION_POLL_INTERVAL_SECONDS)
    return False


# ---------------------------------------------------------------------
# Phase 3 — steady-state heartbeat with passive reconciliation.
# ---------------------------------------------------------------------


def _process_heartbeat_response(
    *,
    response: dict[str, Any],
    client: RouterClient,
    port: int,
    self_color: str,
    self_instance_id: str,
    logger: logging.Logger,
    streamable_port_lookup: PortLookup | None,
    streamable_delivered: bool,
    poller_gate_reactivation: PollerGateReactivation,
    post_registration_callback: PostRegistrationCallback | None,
) -> bool:
    """Act on one heartbeat response; return the updated ``streamable_delivered``.

    Split out of :func:`_run_steady_state_heartbeat` to keep that loop's own
    cyclomatic complexity within the project's A/B ceiling — this is pure
    per-tick decision logic with no loop state of its own beyond what's
    threaded in and returned.
    """
    if response.get("unknown_instance") or not response.get("alive"):
        streamable_port = (
            streamable_port_lookup() if streamable_port_lookup is not None else None
        )
        delivered = _register_and_activate_if_needed(
            client=client,
            port=port,
            self_color=self_color,
            self_instance_id=self_instance_id,
            logger=logger,
            streamable_port=streamable_port,
        ) and streamable_port is not None
        return streamable_delivered or delivered

    _ensure_active_color(
        client=client,
        self_color=self_color,
        self_instance_id=self_instance_id,
        logger=logger,
    )
    _notify_post_registration_if_active(
        client=client,
        self_instance_id=self_instance_id,
        logger=logger,
        callback=post_registration_callback,
    )
    _observe_poller_gate_reactivation(
        client=client,
        poller_gate_reactivation=poller_gate_reactivation,
        logger=logger,
    )
    if streamable_delivered or streamable_port_lookup is None:
        return streamable_delivered
    streamable_port = streamable_port_lookup()
    if streamable_port is not None and _safe_register(
        client=client,
        port=port,
        self_color=self_color,
        self_instance_id=self_instance_id,
        logger=logger,
        streamable_port=streamable_port,
    ):
        return True
    return streamable_delivered


def _observe_poller_gate_reactivation(
    *,
    client: RouterClient,
    poller_gate_reactivation: PollerGateReactivation,
    logger: logging.Logger,
) -> None:
    """Feed one router snapshot into the local poller-gate transition guard."""
    try:
        poller_gate_reactivation.observe(client.status())
    except RouterClientError as exc:
        logger.warning(
            "%s: status check for poller-gate reactivation failed: %s",
            PLUGIN_NAME,
            exc,
        )


def _run_steady_state_heartbeat(
    *,
    client: RouterClient,
    port: int,
    self_color: str,
    self_instance_id: str,
    stop_event: threading.Event,
    pending_finisher_file: Path | None,
    current_release_lookup: CurrentReleaseLookup | None,
    logger: logging.Logger,
    set_color_active: SetColorActive,
    streamable_port_lookup: PortLookup | None = None,
    post_registration_callback: PostRegistrationCallback | None = None,
) -> None:
    """Heartbeat every ``DEFAULT_HEARTBEAT_INTERVAL_SECONDS``; re-register on miss.

    A HEALTHY heartbeat also re-asserts activation via
    ``_ensure_active_color`` (field-verified on a live deployment): a plain
    platform restart can wedge the router at ``no_active_color`` forever — the
    new instance
    registers while the outgoing instance is still active (so the one-shot
    cold-start auto-activate correctly declines), then the router's
    heartbeat GC drops the outgoing instance and clears the active binding,
    and no later path ever re-checked. Re-asserting on every healthy tick
    heals that within one heartbeat interval. It cannot steal authority:
    router activation is atomic (``activate`` swaps directly, so a live
    swap never exposes an ``active_color=None`` window), and the helper
    activates only when the router reports NO active color.

    Also runs the B2 pending-finisher backstop each tick on this (active)
    color: if a prior cutover's ``complete_swap`` enqueue failed after a
    durable swap (a StateService/session-row failure), no action will ever
    SIGTERM the orphaned prior — so the durable pending record is completed
    here, idempotently and verify-then-kill. This is a PERIODIC check (not a
    one-shot at startup): the prior writes the record AFTER the new color has
    already booted + run its startup reconcile, so a startup-only check would
    miss it. Disabled when ``pending_finisher_file`` is ``None``.

    ``streamable_port_lookup`` (BLG-04): a re-register on ``unknown_instance``
    picks up the freshest lookup value the same way Phase 2 does. On an
    otherwise-healthy tick (no re-register triggered) a streamable port that
    was not yet known when this instance first registered is delivered via
    ONE extra ``register_color`` call as soon as the lookup starts returning
    a value — not on every tick after that, since the router already has it
    once delivery succeeds.
    """
    streamable_delivered = streamable_port_lookup is None
    poller_gate_reactivation = PollerGateReactivation(
        self_instance_id=self_instance_id,
        set_color_active=set_color_active,
    )
    while not stop_event.is_set():
        try:
            response = client.heartbeat(self_instance_id)
        except RouterClientError as exc:
            logger.debug("heartbeat failed: %s (will retry)", exc)
        else:
            streamable_delivered = _process_heartbeat_response(
                response=response,
                client=client,
                port=port,
                self_color=self_color,
                self_instance_id=self_instance_id,
                logger=logger,
                streamable_port_lookup=streamable_port_lookup,
                streamable_delivered=streamable_delivered,
                poller_gate_reactivation=poller_gate_reactivation,
                post_registration_callback=post_registration_callback,
            )
        if pending_finisher_file is not None and current_release_lookup is not None:
            _run_pending_finisher_backstop(
                client=client,
                self_instance_id=self_instance_id,
                pending_finisher_file=pending_finisher_file,
                current_release_lookup=current_release_lookup,
                logger=logger,
            )
        if stop_event.wait(DEFAULT_HEARTBEAT_INTERVAL_SECONDS):
            break


def _notify_post_registration_if_active(
    *,
    client: RouterClient,
    self_instance_id: str,
    logger: logging.Logger,
    callback: PostRegistrationCallback | None,
) -> None:
    """Run the idempotent startup release only once router names this instance active."""
    if callback is None:
        return
    try:
        snapshot = client.status()
    except RouterClientError as exc:
        logger.warning("%s: post-registration status check failed: %s", PLUGIN_NAME, exc)
        return
    if snapshot.get("active_instance_id") == self_instance_id:
        callback()


# ---------------------------------------------------------------------
# B2 — post-cutover pending-finisher backstop (verify-then-kill, idempotent).
# ---------------------------------------------------------------------

# Outcome tokens for :func:`reconcile_pending_finisher` — observable for logs +
# tests.
RECONCILE_TERMINATED_ORPHAN: Final[str] = "terminated_orphan"
RECONCILE_UNREGISTERED_DEAD: Final[str] = "unregistered_dead"
RECONCILE_UNREGISTERED_PID_REUSED: Final[str] = "unregistered_pid_reused"
RECONCILE_SKIPPED_SELF: Final[str] = "skipped_self"
RECONCILE_SKIPPED_NOT_ACTIVE: Final[str] = "skipped_not_active"
RECONCILE_SKIPPED_NOT_DURABLE: Final[str] = "skipped_not_durable"
RECONCILE_TERMINATE_FAILED: Final[str] = "terminate_failed"

# The record is cleared ONLY on these — cleanup converged, or the prior is
# provably gone (dead / pid recycled), or another path already finished it.
# Every SKIPPED_* gate that means "not yet / not mine" and TERMINATE_FAILED
# leave the record so the next eligible tick retries: a non-converged tick can
# never drop it (Codex round-2 B2·3 + advisor's clear-decision enumeration).
_CONVERGED_OUTCOMES: Final[frozenset[str]] = frozenset(
    {
        RECONCILE_TERMINATED_ORPHAN,
        RECONCILE_UNREGISTERED_DEAD,
        RECONCILE_UNREGISTERED_PID_REUSED,
    },
)


def reconcile_pending_finisher(
    *,
    record: PendingFinisher,
    self_instance_id: str,
    active_instance_id: str | None,
    current_release: str | None,
    start_token: Callable[[int], str | None],
    terminate: Callable[[int], bool],
    unregister: Callable[[str], None],
) -> str:
    """Idempotently complete (or skip) a leftover post-cutover finisher.

    Pure decision logic — all I/O is injected so a smoke drives every branch with
    zero real processes or sockets. The gates run in order; the prior is SIGTERM'd
    only when EVERY safety condition holds:

    * ``record.prior_instance_id == self_instance_id`` → ``SKIPPED_SELF``: the
      draining prior color must NEVER SIGTERM itself (C2) and must leave the
      record for the NEW active color.
    * ``active_instance_id != self_instance_id`` → ``SKIPPED_NOT_ACTIVE``
      (B2·2 fence): ONLY the router-active instance finishes a swap; any other
      same-solet heartbeat leaves the record untouched.
    * ``current_release != record.candidate_release_id`` →
      ``SKIPPED_NOT_DURABLE`` (B2·1): the record is inert until ``current``
      actually names the candidate — a tick in the ``{record-write → swap}``
      window, or after an aborted/rolled-back swap, never acts on it.

    Past the safety gates, **process LIVENESS — not router registration — is
    authoritative** (cycle-3 rollback fix, 2026-06-29). The earlier design gated
    on "prior still in the router's known set", treating an absent prior as
    already-converged and clearing the record. That invariant was FALSE for a
    prior whose 30s router DRAIN ENTRY EXPIRED while the process was still alive
    (``RouterState.status`` drops drain entries past ``drain_ends_at``): when the
    normal ``complete_swap`` finisher was delayed past the drain window — under
    repeat-cutover boot pressure — this backstop saw the drain-expired-but-ALIVE
    prior, declared it converged, and CLEARED the record out from under the
    finisher → the finisher then no-op'd (``pending_finisher_absent``) and the old
    instance was never SIGTERM'd (it lingered holding ``:9000``). Probing the live
    start-token instead settles it for ALL cases — a killed/dead prior is gone, a
    live one is a real orphan to reap — with no dependency on router routing state:

    * ``start_token(prior_pid) is None`` → ``UNREGISTERED_DEAD``: the prior is
      gone (the normal finisher already killed it, or it died on its own);
      unregister the stale binding, never SIGTERM.
    * live token ``!=`` ``record.prior_start_token`` →
      ``UNREGISTERED_PID_REUSED`` (B2·3 PID-reuse guard): the pid was recycled
      by an unrelated process; unregister the stale binding, NEVER SIGTERM the
      innocent process.
    * identity verified (live token matches) → ``TERMINATED_ORPHAN``:
      SIGTERM + unregister, regardless of whether the prior's router drain entry
      has expired. ``terminate`` returning ``False`` (a transient can't-signal,
      OR — production wrapper — the prior still alive when the bounded
      post-SIGTERM verification expires) yields ``TERMINATE_FAILED`` and leaves
      the record to retry on the next tick. The backstop never escalates past
      SIGTERM (``no_sigkill_reachable_from_this_channel``, iss_8d1ec833).
    """
    if record.prior_instance_id == self_instance_id:
        return RECONCILE_SKIPPED_SELF
    if active_instance_id != self_instance_id:
        return RECONCILE_SKIPPED_NOT_ACTIVE
    if current_release != record.candidate_release_id:
        return RECONCILE_SKIPPED_NOT_DURABLE
    live_token = start_token(record.prior_pid)
    if live_token is None:
        unregister(record.prior_instance_id)
        return RECONCILE_UNREGISTERED_DEAD
    if live_token != record.prior_start_token:
        unregister(record.prior_instance_id)
        return RECONCILE_UNREGISTERED_PID_REUSED
    if not terminate(record.prior_pid):
        return RECONCILE_TERMINATE_FAILED
    unregister(record.prior_instance_id)
    return RECONCILE_TERMINATED_ORPHAN


def _router_active_instance_id(snapshot: dict[str, object]) -> str | None:
    """The instance id the router currently has active, or ``None``."""
    active = snapshot.get("active_instance_id")
    return active if isinstance(active, str) else None


def _run_pending_finisher_backstop(
    *,
    client: RouterClient,
    self_instance_id: str,
    pending_finisher_file: Path,
    current_release_lookup: CurrentReleaseLookup,
    logger: logging.Logger,
) -> None:
    """Production wrapper: read the record, verify-then-kill, clear (idempotent).

    All terminate/unregister/lookup I/O is exception-wrapped so a transient
    failure (a ``ProcessLookupError`` between probe + signal, a router hiccup)
    logs and leaves the record for the next tick rather than crashing the
    heartbeat thread — an unguarded ``os.kill`` raise here would kill the loop
    for the life of the process (Codex round-2 B2·3).
    """
    record = read_pending_finisher(pending_finisher_file)
    if record is None:
        return
    try:
        snapshot = client.status()
    except RouterClientError as exc:
        logger.debug("pending-finisher backstop: router status() failed: %s", exc)
        return
    try:
        current_release = current_release_lookup()
    except Exception as exc:  # noqa: BLE001 — lookup must never crash the loop
        logger.warning(
            "%s: pending-finisher backstop current-release lookup failed: %s",
            PLUGIN_NAME, exc,
        )
        return

    def _terminate(pid: int) -> bool:
        # ``True`` means the prior is VERIFIED gone, not merely signalled: a
        # prior that ignores SIGTERM must not let TERMINATED_ORPHAN clear the
        # record out from under it. The wait is bounded per tick
        # (DEFAULT_BACKSTOP_TERM_VERIFY_SECONDS) because this runs on the
        # heartbeat thread; a still-alive prior comes back as TERMINATE_FAILED
        # with the record kept and is SIGTERM'd again next tick — never SIGKILL.
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            return True  # already gone between probe + signal — converged
        except OSError as exc:
            logger.warning(
                "%s: pending-finisher backstop SIGTERM(%d) failed: %s",
                PLUGIN_NAME, pid, exc,
            )
            return False
        deadline = time.monotonic() + DEFAULT_BACKSTOP_TERM_VERIFY_SECONDS
        while True:
            if process_identity.process_exited(pid):
                return True
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            time.sleep(min(DEFAULT_PRIOR_TERM_POLL_INTERVAL_SECONDS, remaining))
        logger.warning(
            "%s: pending-finisher backstop TERMINATE_TIMEOUT: prior pid=%d still "
            "alive %.1fs after SIGTERM; record kept, no escalation (never SIGKILL); "
            "will retry next tick",
            PLUGIN_NAME, pid, DEFAULT_BACKSTOP_TERM_VERIFY_SECONDS,
        )
        return False

    def _unregister(instance_id: str) -> None:
        try:
            client.unregister_color(instance_id)
        except RouterClientError as exc:
            logger.warning(
                "%s: pending-finisher backstop unregister_color(%s) failed: %s",
                PLUGIN_NAME, instance_id, exc,
            )

    outcome = reconcile_pending_finisher(
        record=record,
        self_instance_id=self_instance_id,
        active_instance_id=_router_active_instance_id(snapshot),
        current_release=current_release,
        start_token=process_identity.start_token,
        terminate=_terminate,
        unregister=_unregister,
    )
    if outcome in _CONVERGED_OUTCOMES:
        clear_pending_finisher(pending_finisher_file)
    if outcome in (
        RECONCILE_TERMINATED_ORPHAN,
        RECONCILE_UNREGISTERED_DEAD,
        RECONCILE_UNREGISTERED_PID_REUSED,
    ):
        logger.info(
            "%s: pending-finisher backstop %s prior=%s pid=%d",
            PLUGIN_NAME, outcome, record.prior_instance_id, record.prior_pid,
        )


# ---------------------------------------------------------------------
# Register / activate primitives — shared by Phases 2 + 3.
# ---------------------------------------------------------------------


def _register_and_activate_if_needed(
    *,
    client: RouterClient,
    port: int,
    self_color: str,
    self_instance_id: str,
    logger: logging.Logger,
    streamable_port: int | None = None,
) -> bool:
    """Register self; on success, claim active color when none is set yet."""
    if not _safe_register(
        client=client,
        port=port,
        self_color=self_color,
        self_instance_id=self_instance_id,
        logger=logger,
        streamable_port=streamable_port,
    ):
        return False
    _ensure_active_color(
        client=client,
        self_color=self_color,
        self_instance_id=self_instance_id,
        logger=logger,
    )
    return True


def _safe_register(
    *,
    client: RouterClient,
    port: int,
    self_color: str,
    self_instance_id: str,
    logger: logging.Logger,
    streamable_port: int | None = None,
) -> bool:
    """One register_color attempt; True iff the router accepted."""
    try:
        result = client.register_color(
            port, self_color, self_instance_id, streamable_port=streamable_port,
        )
    except RouterClientError as exc:
        logger.warning("%s: register_color failed: %s", PLUGIN_NAME, exc)
        return False
    if not result.get("accepted"):
        logger.warning(
            "%s: router refused register_color: %s",
            PLUGIN_NAME,
            result.get("reason"),
        )
        return False
    return True


def _ensure_active_color(
    *,
    client: RouterClient,
    self_color: str,
    self_instance_id: str,
    logger: logging.Logger,
) -> bool:
    """Activate self only when the router has no active color yet.

    Closes the cold-start gap: a fresh router exits boot with
    ``active_color=None`` and would otherwise sit idle until an
    operator activated a color via the mgmt socket by hand. The first
    successful register triggers a status probe; when the router still
    has no active color, the lifecycle claims its own color. When some
    other color is already active (blue-green steady state with two
    registrants or a hot-swap mid-flight), this is a no-op — the
    helper must never steal authority from a live color.

    Also called on every HEALTHY steady-state heartbeat tick: the cold-start
    probe is one-shot and races the router's heartbeat-timeout GC on the
    outgoing instance after a plain restart — register lands while the old
    instance is still active (probe sees
    a color, declines), the GC then drops the old instance and clears
    the active binding, and the router sits at ``no_active_color``
    forever. The steady-state re-assert makes the same conservative
    activation self-healing instead of one-shot.
    """
    try:
        snapshot = client.status()
    except RouterClientError as exc:
        logger.warning(
            "%s: status check before auto-activate failed: %s",
            PLUGIN_NAME, exc,
        )
        return False
    if snapshot.get("active_color") is not None:
        return True
    try:
        result = client.activate(self_color, self_instance_id)
    except RouterClientError as exc:
        logger.warning("%s: auto-activate failed: %s", PLUGIN_NAME, exc)
        return False
    if not result.get("activated"):
        logger.warning(
            "%s: router refused auto-activate: %s",
            PLUGIN_NAME, result.get("reason"),
        )
        return False
    logger.info(
        "%s: auto-activated color=%s instance_id=%s (router had no active color)",
        PLUGIN_NAME, self_color, self_instance_id,
    )
    return True
