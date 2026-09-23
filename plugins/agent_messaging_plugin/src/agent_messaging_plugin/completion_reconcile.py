"""INF-02 — completion-request per-type drain handlers.

The agent_messaging half of the durable completion request/response queue:
claim-time reconcile (holder-death requeue + unassigned forward) and the
serve-timeout sweep. Extracted from :class:`AutonomicAssignment` (which
delegates here) so the slot-lifecycle class stays scoped to Trigger-1/2 +
manual-set — the completion handlers share the drain MECHANISM, never the
deferred-vertex table (the Reviewer-A INF-02 rider).

FAILED-transition triggers owned here (a completion must never hang pending
forever on a dead holder):

* **holder transition** — any ``pending`` row stamped to a holder other
  than the incoming one was forwarded to a session that no longer holds the
  slot (displaced, crashed, or succeeded-away): CAS back to unassigned
  (attempts+1; terminal-fail at the cap) and re-forward. A late serve from
  a displaced-but-alive holder stays safe — the serve CAS admits exactly
  one transition.
* **serve timeout** — a ``pending`` row whose forward stamp exceeded the
  serve window without a served/failed transition re-queues the same way,
  riding the bridge-lifecycle sweeper cadence (no new thread).

Collaborators arrive as injected callables so the handlers are
offline-smokeable against fakes (no FastAPI, no live bridge).
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from ananta.llm.agent_messaging.role_binding import HOLDER_KIND_SESSION
from ananta.services.inference_service.completion_request_queue import (
    REQUEUE_FAILED_TERMINAL,
    clear_stamp_after_failed_forward,
    forwarded_before,
    pending_stamped_requests,
    pending_unassigned_requests,
    requeue_stale_assignment,
    stamp_for_forward,
)
from ananta.services.inference_service.completion_request_schema import (
    COL_HOLDER_AGENT_INSTANCE_ID,
    COL_REQUEST_ID,
)

if TYPE_CHECKING:
    from collections.abc import Callable

logger = logging.getLogger(__name__)


def live_session_holder_id(
    resolve_slot: Callable[[], Any],
    holder_is_live: Callable[[Any], bool],
) -> str | None:
    """The live SESSION holder's instance id, or ``None`` (sweep target).

    Takes the slot-lifecycle's own resolution callables (so subclass seams
    in the smokes govern this too); a non-session or dead holder yields
    ``None`` — the sweep then leaves re-queued rows in the unassigned
    backlog for the next claim's drain.
    """
    holder = resolve_slot()
    if (
        holder is not None
        and holder_is_live(holder)
        and holder.holder_kind == HOLDER_KIND_SESSION
    ):
        return str(holder.agent_instance_id)
    return None


class CompletionReconciler:
    """Claim-time reconcile + serve-timeout sweep for completion requests."""

    def __init__(
        self,
        *,
        state_service: Callable[[], Any],
        forward_completion: Callable[[str, dict[str, object]], None],
        resolve_live_session_holder: Callable[[], str | None],
        serve_window_seconds: int,
    ) -> None:
        self._state_service = state_service
        self._forward_completion = forward_completion
        self._resolve_live_session_holder = resolve_live_session_holder
        self._serve_window_seconds = serve_window_seconds

    def reconcile(self, reason: str, *, new_holder: str) -> None:
        """Re-queue away-transitioned in-flight requests + forward the backlog.

        Runs on EVERY claim. NEVER raises — claim-side policy must not fail
        the claim; rows stay durable for the next claim/sweep.
        """
        try:
            requeued = self._requeue_stamped_except(
                new_holder, reason=f"holder transition ({reason})",
            )
            forwarded = self._forward_unassigned_to(new_holder)
            if requeued or forwarded:
                logger.info(
                    "sys:autonomic completion reconcile (%s): %d in-flight "
                    "re-queued from prior holder(s), %d forwarded to %s",
                    reason, requeued, forwarded, new_holder,
                )
        except Exception:  # noqa: BLE001 — completion policy never fails the claim; rows stay durable
            logger.exception(
                "sys:autonomic completion reconcile FAULTED (%s) — rows stay "
                "durably queued; the next claim/sweep retries", reason,
            )

    def sweep_serve_timeouts(self) -> tuple[int, int]:
        """Serve-timeout sweep (rides the bridge-lifecycle sweeper cadence).

        Re-queued rows forward immediately when a live holder exists.
        Returns ``(requeued, forwarded)``; NEVER raises (sweeper-tick
        context).
        """
        try:
            cutoff = (
                datetime.now(UTC) - timedelta(seconds=self._serve_window_seconds)
            ).isoformat()
            requeued = 0
            for row in pending_stamped_requests(self._state_service()):
                if not forwarded_before(row, cutoff_iso=cutoff):
                    continue
                outcome = requeue_stale_assignment(
                    self._state_service(), row=row, reason="serve timeout",
                )
                if outcome != REQUEUE_FAILED_TERMINAL:
                    requeued += 1
            forwarded = 0
            holder = self._resolve_live_session_holder()
            if holder is not None:
                forwarded = self._forward_unassigned_to(holder)
        except Exception:  # noqa: BLE001 — the sweep must survive transient faults, loudly
            logger.exception(
                "completion serve-timeout sweep FAULTED — rows stay durably "
                "queued; the next tick retries",
            )
            return (0, 0)
        return (requeued, forwarded)

    def _requeue_stamped_except(self, new_holder: str, *, reason: str) -> int:
        """Re-queue every pending row stamped to a holder ≠ ``new_holder``."""
        state = self._state_service()
        requeued = 0
        for row in pending_stamped_requests(state):
            holder = str(row.get(COL_HOLDER_AGENT_INSTANCE_ID) or "")
            if holder == new_holder:
                continue
            outcome = requeue_stale_assignment(state, row=row, reason=reason)
            if outcome != REQUEUE_FAILED_TERMINAL:
                requeued += 1
        return requeued

    def _forward_unassigned_to(self, holder_agent_instance_id: str) -> int:
        """Stamp-CAS + forward every unassigned pending request to the holder.

        The CAS win gates the emission (a submit racing this drain cannot
        double-forward); an emission fault clears the stamp so the row
        returns to the unassigned backlog (loud, durable).
        """
        state = self._state_service()
        forwarded = 0
        for row in pending_unassigned_requests(state):
            request_id = str(row.get(COL_REQUEST_ID) or "")
            if not request_id or not stamp_for_forward(
                state,
                request_id=request_id,
                holder_agent_instance_id=holder_agent_instance_id,
            ):
                continue
            try:
                self._forward_completion(holder_agent_instance_id, row)
                forwarded += 1
            except Exception:  # noqa: BLE001 — forward fault → back to the durable backlog, never lost
                clear_stamp_after_failed_forward(
                    state,
                    request_id=request_id,
                    holder_agent_instance_id=holder_agent_instance_id,
                )
                logger.warning(
                    "completion request %s: claim-time forward to %s FAILED "
                    "— returned to the unassigned backlog",
                    request_id, holder_agent_instance_id, exc_info=True,
                )
        return forwarded


__all__ = ["CompletionReconciler", "live_session_holder_id"]
