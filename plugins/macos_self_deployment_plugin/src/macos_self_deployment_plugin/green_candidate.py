"""The green candidate's router lifecycle, split out of ``SwapOrchestrator``.

A swap spawns a green CANDIDATE process and drives it against the
local-blue-green router across its lifecycle: wait for it to register +
accept connections, tear it down (bounded SIGTERM + unregister — never
SIGKILL, see :meth:`GreenCandidate.terminate_bounded`), and — when a
post-``activate`` durable swap (cutover OR rollback) fails — compensate by
rolling the router back to the prior color and conditionally tearing the
candidate down (§4.7 F2), returning a typed :class:`CompensationOutcome`.

Extracted from :class:`~.swap_orchestrator.SwapOrchestrator` so the swap
*choreography* stays coherent and bounded (the god-class gate), mirroring
the ``ReleaseBuilder`` / ``ReleaseLedger`` split out of ``ReleaseManager``.
This is a pure relocation — no behaviour change; the orchestrator's smokes
(cutover_failure, instance_authority, poller_gate, swap_round_trip) are the
behaviour-equivalence proof. The controller is stateless: per-swap
identifiers (instance id, pid, prior color) are passed to each method.
"""

from __future__ import annotations

import http.client
import logging
import os
import signal
import time
from dataclasses import dataclass
from enum import StrEnum

from macos_self_deployment_plugin import process_identity
from macos_self_deployment_plugin.constants import (
    DEFAULT_PRIOR_TERM_POLL_INTERVAL_SECONDS,
)
from macos_self_deployment_plugin.router_client import (
    RouterClient,
    RouterClientError,
)


@dataclass(frozen=True, slots=True)
class CompensationOutcome:
    """Outcome of :meth:`GreenCandidate.compensate_failed_swap`.

    The compensation is context-agnostic (it does the same router-rollback +
    F2-gated kill for a failed forward cutover and a failed durable rollback);
    the caller maps these two outcomes to the right ``RestartStatus`` +
    ``reason_code``:

    - ``restored=True`` — the router rollback to the prior color CONFIRMED and
      the candidate exited on SIGTERM (or was already gone) + was unregistered,
      so the pre-swap pair is restored. The caller returns ``FAILED`` (system
      coherent, retryable).
    - ``restored=False`` — EITHER the router rollback did NOT take (RPC error /
      refusal / drain expired), so the candidate is LEFT ALIVE (the router may
      still route to it; killing it would route live traffic to a dead color);
      OR the rollback DID take but the candidate ignored SIGTERM for the whole
      grace window and is still alive (``no_sigkill_reachable_from_this_channel``
      forbids escalating). The two carry distinct messages; both send the
      caller to ``NEEDS_INTERVENTION`` (a human must act).
    """

    restored: bool
    message: str


class CandidateTeardown(StrEnum):
    """How :meth:`GreenCandidate.terminate_bounded` left the candidate.

    The candidate teardown is a bounded SIGTERM and nothing more: this channel
    never escalates to SIGKILL (adjudication D1 property
    ``no_sigkill_reachable_from_this_channel``, iss_8d1ec833). Every value is
    therefore a statement about what was OBSERVED, not about what was forced:

    * ``TERMINATED`` — SIGTERM was delivered and the child exited (zombie
      reaped) inside the grace window.
    * ``ALREADY_GONE`` — no signal was sent: the child had already exited, or
      the pid no longer carries the start-time token captured at spawn (pid
      reused by an unrelated process — never signal it).
    * ``SIGNAL_DENIED`` — ``SIGTERM`` raised ``PermissionError``; the child's
      state is unknown and nothing further was attempted.
    * ``TIMED_OUT`` — SIGTERM was delivered and the grace window expired with
      the child STILL ALIVE. No second signal is ever sent; the caller must
      surface ``NEEDS_INTERVENTION`` naming the pid + token.
    """

    TERMINATED = "terminated"
    ALREADY_GONE = "already_gone"
    SIGNAL_DENIED = "signal_denied"
    TIMED_OUT = "timed_out"

    @property
    def candidate_gone(self) -> bool:
        """True iff the candidate is confirmed exited (terminated / already gone).

        ``TIMED_OUT`` and ``SIGNAL_DENIED`` both leave a possibly-live
        candidate with no further signal available in this channel; callers
        branch on this rather than on the specific value so neither can be
        mistaken for a clean teardown.
        """
        return self in (CandidateTeardown.TERMINATED, CandidateTeardown.ALREADY_GONE)


class CandidateReadiness(StrEnum):
    """Why :meth:`GreenCandidate.wait_until_registered` stopped waiting.

    ``TIMED_OUT`` and ``EXITED`` are both failures, but they are not the same
    failure and must not cost the same wall-clock. A candidate that is still
    running has simply not finished starting, and waiting out the full timeout
    is the correct thing to do. A candidate that is already DEAD will never
    register, and every further second spent polling for it is a second the
    swap holds the platform's action queue for nothing.
    """

    REGISTERED = "registered"
    EXITED = "exited"
    TIMED_OUT = "timed_out"


def _child_exited(pid: int) -> bool:
    """Whether the spawned candidate has exited — zombies included.

    Thin name kept for this module's callers and smokes; the zombie-aware
    probe itself lives in :func:`process_identity.process_exited` so the
    heartbeat backstop's post-SIGTERM verification uses the identical
    definition of "exited".
    """
    return process_identity.process_exited(pid)


def _probe_port_reachable(port: int, timeout_seconds: float = 0.5) -> bool:
    """Require a bounded response from the candidate bridge's health endpoint.

    Used by :meth:`GreenCandidate.wait_until_registered` as a belt-and-
    suspenders check that the registered port serves its bridge surface — not
    just that the color said "I'm here" via the management socket. A TCP open
    proves only that a listener is bound; ``GET /api/v1/bridge/health`` must
    receive a 200 response before activation can proceed. A response timeout,
    malformed HTTP reply, or non-200 status warrants another poll cycle.
    """
    connection = http.client.HTTPConnection(
        "127.0.0.1",
        port,
        timeout=timeout_seconds,
    )
    try:
        connection.request("GET", "/api/v1/bridge/health")
        response = connection.getresponse()
        response.read()
    except (OSError, http.client.HTTPException):
        return False
    finally:
        connection.close()
    return response.status == http.HTTPStatus.OK


class GreenCandidate:
    """Stateless controller for a swap's green candidate vs. the router."""

    def __init__(
        self,
        *,
        router_client: RouterClient,
        logger: logging.Logger,
        ready_timeout_seconds: int,
        ready_poll_interval_seconds: float,
    ) -> None:
        self._router = router_client
        self._logger = logger
        self._ready_timeout = ready_timeout_seconds
        self._ready_poll = ready_poll_interval_seconds

    def wait_until_registered(
        self, instance_id: str, *, pid: int,
    ) -> CandidateReadiness:
        """Poll router.status() until ``instance_id`` appears AND its bridge serves.

        The plain "color is listed in status" check confirms the color
        called ``register_color`` after
        :func:`heartbeat_lifecycle._wait_for_bridge_port` observed the
        bridge_port bound on its plugin instance. As a belt-and-suspenders
        defense against bind-then-wedge races (or a malformed register
        payload), this sends a bounded health request to the registered bridge
        port. Activate only proceeds when both the registry entry AND the
        bridge response are confirmed.
        """
        deadline = time.monotonic() + self._ready_timeout
        while time.monotonic() < deadline:
            if self._registered_and_reachable(instance_id):
                return CandidateReadiness.REGISTERED
            # Liveness is checked AFTER registration so a candidate that came up
            # and exited within one poll cycle is still credited with the
            # registration it achieved.
            if _child_exited(pid):
                self._logger.error(
                    "swap candidate %s (pid %d) exited before registering with "
                    "the router; failing now instead of polling a corpse for "
                    "the remaining %.0fs",
                    instance_id, pid, max(0.0, deadline - time.monotonic()),
                )
                return CandidateReadiness.EXITED
            time.sleep(self._ready_poll)
        return CandidateReadiness.TIMED_OUT

    def _registered_and_reachable(self, instance_id: str) -> bool:
        """One poll: is ``instance_id`` in the registry AND serving bridge health?"""
        try:
            snap = self._router.status()
        except RouterClientError:
            return False
        colors = snap.get("colors") or []
        if not isinstance(colors, list):
            return False
        for entry in colors:
            if isinstance(entry, dict) and entry.get("instance_id") == instance_id:
                port = entry.get("port")
                if isinstance(port, int) and _probe_port_reachable(port):
                    return True
                # Registered but port not (yet) reachable — keep polling.
        return False

    def terminate_bounded(
        self, pid: int, *, start_token: str | None, grace_seconds: float,
    ) -> CandidateTeardown:
        """SIGTERM the spawned child and wait — bounded — for it to exit.

        Replaces the former ``kill`` (``os.kill(pid, 9)``). The poll loop
        mirrors the finisher's ``_signal_and_wait`` shape; what it deliberately
        does NOT mirror is that method's SIGKILL escalation on grace overrun:
        a candidate that ignores SIGTERM is reported as
        :attr:`CandidateTeardown.TIMED_OUT` and left for a human, because this
        channel must never reach signal 9.

        ``start_token`` is the child's start-time identity captured by the
        executor right after spawn (:func:`process_identity.start_token`). The
        live token is re-read here and the signal is REFUSED on a mismatch —
        the same PID-reuse guard the finisher applies to the prior
        (``_signal_verified_prior``): a recycled pid is somebody else's process.
        """
        if _child_exited(pid):
            return CandidateTeardown.ALREADY_GONE
        live_token = process_identity.start_token(pid)
        if live_token is None:
            return CandidateTeardown.ALREADY_GONE
        if live_token != start_token:
            self._logger.warning(
                "refusing to SIGTERM pid=%d: live start token %r differs from "
                "the spawn-time token %r (pid reused); candidate treated as gone",
                pid, live_token, start_token,
            )
            return CandidateTeardown.ALREADY_GONE
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            return CandidateTeardown.ALREADY_GONE
        except PermissionError as exc:
            self._logger.error("SIGTERM denied on candidate pid=%d: %s", pid, exc)
            return CandidateTeardown.SIGNAL_DENIED
        deadline = time.monotonic() + grace_seconds
        while True:
            if _child_exited(pid):
                return CandidateTeardown.TERMINATED
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            time.sleep(min(DEFAULT_PRIOR_TERM_POLL_INTERVAL_SECONDS, remaining))
        self._logger.critical(
            "candidate pid=%d (start token %r) is STILL ALIVE %.1fs after SIGTERM; "
            "this channel never escalates to SIGKILL — manual intervention required",
            pid, start_token, grace_seconds,
        )
        return CandidateTeardown.TIMED_OUT

    def unregister(self, instance_id: str) -> None:
        """Unregister ``instance_id`` from the router; swallow RPC errors."""
        try:
            self._router.unregister_color(instance_id)
        except RouterClientError as exc:
            self._logger.warning("unregister_color(%s) failed: %s", instance_id, exc)

    def rollback_router(self, prior_color: str, prior_instance_id: str) -> bool:
        """Re-activate one draining instance. Returns True iff confirmed.

        Returns ``False`` on BOTH an RPC error AND an explicit router refusal
        (``rolled_back`` falsey / drain window expired). The caller must NOT
        kill the candidate when this returns ``False`` — the router may still
        route to it, and killing it would route live traffic to a dead color.
        """
        try:
            result = self._router.rollback(prior_color, prior_instance_id)
        except RouterClientError as exc:
            self._logger.error("router rollback(%s) failed: %s", prior_color, exc)
            return False
        if not result.get("rolled_back"):
            self._logger.error(
                "router refused rollback(%s): %s",
                prior_color, result.get("reason", "unknown"),
            )
            return False
        return True

    def compensate_failed_swap(
        self, *, prior_color: str, prior_instance_id: str, instance_id: str,
        pid: int, start_token: str | None, grace_seconds: float, exc: Exception,
    ) -> CompensationOutcome:
        """§4.7 post-activate swap-failure compensation; return a typed outcome.

        Reached only when the durable symlink op (``cutover`` for a forward
        swap, ``rollback`` for the durable-rollback verb) raised AFTER a
        successful router ``activate`` and BEFORE ``complete_swap`` was
        enqueued. The symlink op reverts its own half-applied state on an
        in-process ``OSError`` (and never touches the symlinks on a pre-swap
        raise), so ``current``/``previous`` are already unchanged; this restores
        the *routing* side to match.

        F2 — the candidate teardown is GATED on a CONFIRMED router rollback:

        - if ``rollback(prior_color)`` confirms, the prior color is
          authoritative again, so the candidate must not serve — bounded
          SIGTERM (:meth:`terminate_bounded`, never SIGKILL) + unregister it
          and return ``restored=True`` (the caller returns FAILED without
          enqueuing ``complete_swap``, so the prior process is never
          SIGTERM'd). If the candidate ignores SIGTERM past ``grace_seconds``
          it is unregistered anyway (the router must not route to it) and the
          outcome is ``restored=False`` with a message saying the router WAS
          rolled back but the candidate is still alive — distinct from the
          rollback-did-not-take message below — so the caller escalates to
          NEEDS_INTERVENTION without a second signal;
        - if the rollback does NOT take (RPC error or the router refuses /
          drain window expired), the router may STILL route to the candidate.
          Killing it then would route live traffic to a DEAD color — so leave
          the candidate ALIVE and return ``restored=False`` with a message that
          does NOT claim the prior color was restored. The caller escalates to
          NEEDS_INTERVENTION.
        """
        self._logger.error(
            "swap failed after activate; attempting router rollback to prior "
            "color=%s (candidate instance=%s pid=%d): %s",
            prior_color, instance_id, pid, exc,
        )
        if not self.rollback_router(prior_color, prior_instance_id):
            self._logger.critical(
                "swap failed AND router rollback to %s did not take; leaving "
                "candidate instance=%s pid=%d ALIVE to avoid routing live traffic "
                "to a dead color. Manual intervention required.",
                prior_color, instance_id, pid,
            )
            return CompensationOutcome(
                restored=False,
                message=(
                    f"durable swap failed after activate AND router rollback to "
                    f"{prior_color} did NOT take; candidate instance={instance_id} "
                    f"LEFT ALIVE (router may still route to it) — manual "
                    f"intervention required: {exc}"
                ),
            )
        teardown = self.terminate_bounded(
            pid, start_token=start_token, grace_seconds=grace_seconds,
        )
        self.unregister(instance_id)
        if not teardown.candidate_gone:
            return CompensationOutcome(
                restored=False,
                message=(
                    f"durable swap failed after activate; router WAS rolled back "
                    f"to {prior_color} and candidate instance={instance_id} was "
                    f"unregistered, but candidate pid={pid} "
                    f"(start_token={start_token!r}) is not confirmed gone "
                    f"(teardown={teardown.value}, grace={grace_seconds}s) — this "
                    f"channel never escalates to SIGKILL; manual intervention "
                    f"required: {exc}"
                ),
            )
        return CompensationOutcome(
            restored=True,
            message=(
                f"durable swap failed after activate; prior color {prior_color} "
                f"restored, candidate teardown={teardown.value} "
                f"(current/previous unchanged): {exc}"
            ),
        )


__all__ = ["CandidateTeardown", "CompensationOutcome", "GreenCandidate"]
