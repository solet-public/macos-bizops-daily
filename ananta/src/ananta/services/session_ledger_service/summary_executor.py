"""Single-slot background executor + singleton guard for the auto-summary drain.

The auto-summarize cron is a HEARTBEAT: on each fire it submits the WHOLE
drain-until-empty pass (``SessionLedgerService._drain_all_quiescent``) to this
executor rather than doing any summarization on the action-queue thread. That
serves two ends at once with one ``BoundedSemaphore(1)``:

* **WI-0 non-blocking.** The cron returns in milliseconds; the drain (including
  its synchronous ``generate_completion`` inference calls) runs on the daemon
  worker, so a model call can never park the action queue — the LIVE BUG that an
  inline ``generate_completion`` inside ``_summarize_one_session`` once caused.
* **Singleton drainer (per process).** The single slot doubles as the singleton
  guard: while a drain is running the slot is held, so an overlapping cron fire's
  ``submit`` returns False and that fire is a no-op. At most one drainer runs at a
  time PER PROCESS — the semaphore is in-memory, so a blue-green swap window (blue
  still draining on its slot while green claims the next cron fire on its own fresh
  slot) can run two drainers over the shared DB. Harm is bounded by push
  idempotency (done == embedded exclusion): worst case is wasted inference + a
  duplicate-push race in the swap window, not corruption.

⚠️  Inference routing is still a Phase-0b stopgap superseded by Phase 5: the
drain calls the raw synchronous provider seam. Phase 5's done-when says *"revisit
Phase 0b to route summaries through the resolver"*; when the
``InferenceProviderResolver`` lands, route auto-summary inference through it
(role-based, off the critical path). The single-slot background/guard mechanism
here stays regardless — it is what makes the drain both non-blocking and a
singleton.

Design (concurrency-1 + daemon):

* **Concurrency 1 = the guard.** ``BoundedSemaphore(1)`` admits one background
  drain at a time; a second ``submit`` while the slot is held returns False
  (caller treats it as "a drainer is already running").
* **Bounded by the provider, not here.** Each summary's model call is bounded by
  ``LMStudioProvider.generate_completion`` (``timeout=self.timeout``, 600s), which
  raises ``InferenceTimeoutError`` rather than blocking forever; the drain
  catches it per-session and continues. No inner timeout thread here.
* **Shutdown-safe.** The worker thread is ``daemon=True`` — an in-flight drain is
  abandoned at interpreter exit / blue-green drain and can never hang shutdown
  (no ``join`` on a live model call). A restart just starts a fresh drain; the
  work is idempotent (a dropped summary leaves ``summary_text`` NULL and the
  session is re-picked on the next drain).
"""

from __future__ import annotations

import logging
import threading
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from collections.abc import Callable

logger = logging.getLogger(__name__)


class SummaryExecutor(Protocol):
    """Seam for handing the auto-summary drain to background execution.

    Implementations run ``work`` (the whole drain-until-empty pass) off the
    action-queue thread. Tests inject a synchronous or recording implementation;
    production uses :class:`BoundedSummaryExecutor`.
    """

    def submit(self, work: Callable[[], object]) -> bool:
        """Schedule ``work`` for background execution.

        Returns True if accepted (a drainer was started), False if the single
        slot is already held (a drainer is already running → the caller treats
        this fire as a no-op). The work's return value is discarded.
        """
        ...


class BoundedSummaryExecutor:
    """Single-slot, daemon-backed background executor + singleton guard.

    See the module docstring. The single slot admits one background drain at a
    time and doubles as the singleton guard (a second ``submit`` while the slot
    is held returns False). Per-call model timeouts are enforced by the provider
    (``generate_completion`` raises on timeout), not here. Not lifecycle-managed
    (the session-ledger service has no stop hook); shutdown-safety comes entirely
    from daemon threads.
    """

    def __init__(self, *, name: str = "ledger-summary") -> None:
        self._name = name
        self._slot = threading.BoundedSemaphore(1)

    def submit(self, work: Callable[[], object]) -> bool:
        if not self._slot.acquire(blocking=False):
            return False
        try:
            threading.Thread(
                target=self._run, args=(work,), name=self._name, daemon=True,
            ).start()
        except RuntimeError:
            # Thread creation failed (e.g. OS thread limit). Release the slot so
            # the executor never wedges permanently at concurrency-0, and report
            # not-accepted (the caller counts the session skipped → re-picked
            # next pass).
            self._slot.release()
            logger.exception("failed to start background auto-summary thread")
            return False
        return True

    def _run(self, work: Callable[[], object]) -> None:
        try:
            work()
        except Exception:  # noqa: BLE001 — background boundary; drain owns per-session isolation
            logger.exception(
                "background auto-summary drain failed at the top level; "
                "the next cron fire starts a fresh drain",
            )
        finally:
            # Released exactly once per accepted submit, after the drain returns
            # or raises — so the singleton slot is freed for the next cron fire
            # even if the drain thread dies (it can never wedge at concurrency-0).
            self._slot.release()
