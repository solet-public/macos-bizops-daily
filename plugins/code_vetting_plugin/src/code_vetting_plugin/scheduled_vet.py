"""scheduled_vet.py — W3C-3: the single-slot background executor + regression helper for the daily self-vet.

An EDGE_SINK cron handler MUST return promptly (action-queue fast-return contract; the 2026-07-10
``trigger_poll`` fleet-wide stall is the worked example), and a full L1 self-vet over the platform's tree is minutes.
So ``trigger_scheduled_self_vet`` submits the run to :class:`SingleSlotVetExecutor` and returns a fast
started/already-running receipt; the scan runs OFF the dispatch path on a daemon thread.

Pure helpers only (no plugin / no ``verify`` imports) so the plugin's module-import of this module stays out
of the L1 verb-import closure's forbidden set (R6). The background body that touches state/memory lives on
the plugin (obtained at runtime), not here.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable, Mapping

logger = logging.getLogger(__name__)

# The tag on the queued regression memory (Option A — a persisted note the operator/heartbeat surfaces async).
REGRESSION_MEMORY_TAG = "code_vetting:self_vet_regression"

# A scheduled run REGRESSES only when a load-bearing severity worsens; medium/low churn is silent
# (trend-not-nag, ruling-approved).
_REGRESSION_SEVERITIES: tuple[str, ...] = ("blocker", "high")


class SingleSlotVetExecutor:
    """Single-slot, daemon-backed background executor + singleton guard — the slot IS the lease.

    Mirrors the session-ledger's ``BoundedSummaryExecutor``: a ``BoundedSemaphore(1)`` admits one background
    self-vet at a time and doubles as the singleton guard — a second :meth:`submit` while the slot is held
    returns False (the caller reports ``already_running``). Daemon threads make it shutdown-safe with NO stop
    hook (an in-flight run is abandoned at interpreter exit / blue-green drain; the next cron fire starts a
    fresh run). The in-memory semaphore is per-process, so a blue-green swap window can run two over the
    shared DB — harmless: each run persists a bounded-retention ``vetting_runs`` row and is idempotent.
    """

    def __init__(self, *, name: str = "code-vetting-self-vet") -> None:
        self._name = name
        self._slot = threading.BoundedSemaphore(1)

    def submit(self, work: Callable[[], object]) -> bool:
        """Start ``work`` on the single slot; return True (started) or False (slot held → already running)."""
        if not self._slot.acquire(blocking=False):
            return False
        try:
            threading.Thread(target=self._run, args=(work,), name=self._name, daemon=True).start()
        except RuntimeError:
            # Thread creation failed (e.g. OS thread limit): release the slot so the executor never wedges
            # at concurrency-0, and report not-started (the next cron fire retries).
            self._slot.release()
            logger.exception("failed to start the background self-vet thread")
            return False
        return True

    def _run(self, work: Callable[[], object]) -> None:
        try:
            work()
        except Exception:  # noqa: BLE001 — background boundary; the run persists before any notify, and the next fire starts fresh
            logger.exception("background self-vet failed at the top level; the next cron fire starts a fresh run")
        finally:
            # Released exactly once per accepted submit, after the run returns or raises — so the singleton
            # slot is freed for the next cron fire even if the thread dies (never wedges at concurrency-0).
            self._slot.release()


def _severity_count(counts: Mapping[str, object], severity: str) -> int:
    value = counts.get(severity, 0)
    return value if isinstance(value, int) else 0


def is_regression(new_counts: Mapping[str, object], prior_counts: Mapping[str, object]) -> bool:
    """True iff the new run's blocker OR high count exceeds the prior run's (the ruling-approved 'worse').

    ``*_counts`` are ``counts_by_severity`` histograms (severity name -> int). Medium/low churn is silent.
    """
    return any(_severity_count(new_counts, sev) > _severity_count(prior_counts, sev) for sev in _REGRESSION_SEVERITIES)
