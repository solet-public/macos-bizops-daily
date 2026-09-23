"""INF-06 reliability carve — forwarded-vertex serve-timeout reconciler.

The forwarded-vertex half of the ``sys:autonomic`` re-drive machinery, held by
:class:`~agent_messaging_plugin.autonomic_assignment.AutonomicAssignment` as
``self.forwarded`` — the exact composition shape it already uses for the INF-02
:class:`~agent_messaging_plugin.completion_reconcile.CompletionReconciler`
(``self.completions``). Extracted so ``AutonomicAssignment`` stays a coherent
lifecycle-policy owner (seam boundary + god-class coherence) while this module
owns the durable ``core__inference_deferred_vertex`` ``forwarded``-row lifecycle:

- ``resubmit_flow`` — the SUB-05 RESUBMIT bridge: read a row's ``method``
  (observability-only) and re-drive the flow via the injected primitive (the
  first-claim drain calls this too, for holder-transition re-drives).
- ``sweep_serve_timeouts`` — the serve-timeout rider (rides the bridge-lifecycle
  sweeper tick): a forwarded row whose holder died / timed out before
  self-executing is re-driven (monotone ``attempts++``) or terminal-failed at the
  cap. Deliberately does NOT hard-delete — the re-mint upsert preserves the
  monotone counter (a delete-then-reset would defeat the cap).
- ``gc_terminal_rows`` — reap aged terminal ``failed`` rows so the durable stall
  records never grow unbounded (§8-bis retention rider).

Both sweep methods self-isolate (internal try/except → never raise, return
counts) so the composed sweeper tick can call them directly.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from ananta.core.domain.timestamps import to_naive_utc
from ananta.services.inference_service.deferred_vertex_queue import (
    attempts_of,
    forwarded_before,
    hard_delete_flows,
    increment_attempts,
    live_rows_in_state,
    mark_terminal_failed,
)
from ananta.services.inference_service.schema import (
    COL_FLOW_ID,
    COL_METHOD,
    METHOD_PROCESS_RESULTS,
    STATE_FAILED,
    STATE_FORWARDED,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from ananta.services.inference_service.deferred_vertex_queue import (
        DeferredVertexStore,
    )

logger = logging.getLogger(__name__)

# The standard soft-delete/audit column the ``SchemaStandardizer`` auto-injects on
# every table (so it is NOT declared in ``schema.py``); it carries the terminal-flip
# timestamp the GC ages ``failed`` rows by.
COL_UPDATED_AT = "updated_at"


def terminal_row_aged(row: dict[str, object], *, cutoff_iso: str) -> bool:
    """Is this terminal ``failed`` row older than ``cutoff_iso`` (safe to GC-reap)?

    Compares the terminal-flip timestamp (``updated_at``) by VALUE via
    ``to_naive_utc``, NOT by ISO-8601 spelling: ``updated_at`` is a
    ``timestamp without time zone`` column so it round-trips NAIVE, while
    ``cutoff_iso`` is the tz-aware ``datetime.now(UTC).isoformat()`` — a lexical
    compare of the two spellings mis-orders at the boundary (F-AISLOP; the
    ``to_naive_utc`` bug class). Coerce both sides to one naive-UTC VALUE, then
    compare.

    A missing / empty ``updated_at`` returns False (never-delete-on-unknown-age): a
    DESTRUCTIVE GC must NEVER reap a row whose age it cannot read. This default is
    deliberately the OPPOSITE of the sweep predicates' surface-on-anomaly default
    (``forwarded_before`` returns True) — the asymmetry is keyed to the consequence
    (a reap deletes irrecoverably; a sweep only re-drives, bounded by the attempts
    cap). A present-but-unparseable stamp is a genuine corruption: ``to_naive_utc``
    raises (fail loud); the GC tick catches it, logs, and retries. It is unreachable
    via the DATETIME column.
    """
    updated = row.get(COL_UPDATED_AT)
    if not (isinstance(updated, str) and updated):
        return False
    return to_naive_utc(updated) < to_naive_utc(cutoff_iso)


class ForwardedVertexReconciler:
    """Serve-timeout re-drive + terminal-row GC for durable ``forwarded`` rows."""

    def __init__(
        self,
        *,
        state: Callable[[], DeferredVertexStore],
        resubmit_vertex: Callable[[str, str], bool],
        serve_window_seconds: int,
        attempts_cap: int,
        terminal_gc_after_seconds: int,
    ) -> None:
        self._state = state
        # SUB-05 RESUBMIT primitive: re-enter the organism turn for a flow_id
        # against CURRENT durable state (fresh vertex, fresh decode — NEVER a
        # replay). Signature ``(flow_id, method) -> bool`` (method is
        # observability-only; the re-drive is a fresh process_results initial
        # vertex regardless).
        self._resubmit_vertex = resubmit_vertex
        self._serve_window_seconds = serve_window_seconds
        self._attempts_cap = attempts_cap
        self._terminal_gc_after_seconds = terminal_gc_after_seconds

    def resubmit_flow(self, flow_id: str, row: dict[str, object]) -> bool:
        """Re-drive one row via the SUB-05 RESUBMIT primitive (fresh decode).

        The recorded ``method`` is observability-only — the re-drive is ALWAYS a
        fresh ``process_results`` initial vertex (Architect §6-bis: re-entering
        ``process_error`` WITHOUT its ephemeral error observation is incoherent,
        WITH it is the forbidden replay; the fresh initial vertex is the only
        coherent re-entry, and it reads the CURRENT durable plan/error state — the
        failed step stays ``[>]`` current and the failed result is durably stored,
        so the fresh decode is NOT blind to the failure).
        """
        method = str(row.get(COL_METHOD) or METHOD_PROCESS_RESULTS)
        return self._resubmit_vertex(flow_id, method)

    def sweep_serve_timeouts(self) -> tuple[int, int]:
        """Serve-timeout sweep of ``forwarded`` rows (rides the sweeper tick).

        A holder that took the forwarded action-decode and then died / timed out
        before self-executing leaves a ``forwarded`` row that never cleared (the
        forward returns COMPLETED+actions:[] so the flow already terminated). For
        each such row past the serve window: increment the monotone ``attempts``
        AT sweep-fire (ruling §1 pin 2 — the re-drive decision point); at the cap
        flip to the terminal ``failed`` state (durable stall record + loud log);
        otherwise RESUBMIT (fresh decode). The re-drive SUBMITS a fresh vertex
        action and returns — it never runs inference inline (action-queue
        fast-return contract).

        Crucially the sweep does NOT hard-delete the re-driven row: the re-driven
        vertex re-mints the SAME ``flow_id`` (upsert), which PRESERVES the
        just-incremented ``attempts`` (monotone per flow occupancy — pin 2) and
        refreshes ``forwarded_at`` (re-forward) or flips to ``deferred`` (vacancy).
        A re-drive whose fresh decode neither re-forwards nor defers (advance /
        DEFAULT / done-plan no-op) leaves the row untouched → it is re-swept next
        tick → ``attempts`` climbs to the cap → terminal-fail. Deleting here would
        start a NEW occupancy (attempts reset) and defeat the cap. NEVER raises
        (sweeper-tick context). Returns ``(re_driven, terminal_failed)``.
        """
        try:
            cutoff = (
                datetime.now(UTC) - timedelta(seconds=self._serve_window_seconds)
            ).isoformat()
            re_driven = 0
            terminal = 0
            for row in live_rows_in_state(self._state(), state=STATE_FORWARDED):
                if not forwarded_before(row, cutoff_iso=cutoff):
                    continue
                flow_id = str(row.get(COL_FLOW_ID) or "")
                if not flow_id:
                    continue
                next_attempts = attempts_of(row) + 1
                increment_attempts(self._state(), flow_id=flow_id)
                if next_attempts >= self._attempts_cap:
                    mark_terminal_failed(self._state(), flow_id=flow_id)
                    terminal += 1
                    logger.warning(
                        "INF-06 forwarded-vertex re-drive CAP hit for flow=%s "
                        "(attempts=%d/%d) — terminal 'failed' (durable stall "
                        "record; a plan-level watchdog is separate scope)",
                        flow_id, next_attempts, self._attempts_cap,
                    )
                    continue
                if self.resubmit_flow(flow_id, row):
                    re_driven += 1
            if re_driven or terminal:
                logger.info(
                    "INF-06 forwarded serve-timeout sweep: %d re-driven, %d "
                    "terminal-failed", re_driven, terminal,
                )
        except Exception:  # noqa: BLE001 — the sweep must survive transient faults, loudly
            logger.exception(
                "INF-06 forwarded serve-timeout sweep FAULTED — rows stay "
                "durably queued; the next tick retries",
            )
            return (0, 0)
        return (re_driven, terminal)

    def gc_terminal_rows(self) -> int:
        """Hard-delete aged terminal ``failed`` rows (rides the sweeper tick).

        Keeps the durable stall records from accumulating unboundedly (§8-bis
        retention rider). A ``failed`` row older than ``terminal_gc_after_seconds``
        (by ``updated_at``, the terminal-flip timestamp) is hard-deleted;
        ``terminal_row_aged`` decides by VALUE (``to_naive_utc``, not a fragile
        lexical spelling compare) and never reaps a row whose age it cannot read.
        NEVER raises. Returns the number reaped.
        """
        try:
            cutoff = (
                datetime.now(UTC)
                - timedelta(seconds=self._terminal_gc_after_seconds)
            ).isoformat()
            reaped: list[str] = []
            for row in live_rows_in_state(self._state(), state=STATE_FAILED):
                if not terminal_row_aged(row, cutoff_iso=cutoff):
                    continue
                flow_id = str(row.get(COL_FLOW_ID) or "")
                if flow_id:
                    reaped.append(flow_id)
            hard_delete_flows(self._state(), reaped)
            if reaped:
                logger.info(
                    "INF-06 terminal-row GC: reaped %d aged 'failed' rows",
                    len(reaped),
                )
        except Exception:  # noqa: BLE001 — GC must survive transient faults, loudly
            logger.exception("INF-06 terminal-row GC FAULTED — retried next tick")
            return 0
        return len(reaped)
