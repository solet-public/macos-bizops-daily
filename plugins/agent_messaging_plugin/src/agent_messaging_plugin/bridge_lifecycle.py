"""REL-09 — bridge-lifecycle full cleanup: sweep and close share ONE sequence.

Root cause this module kills: ``BridgeSessionManager.sweep_idle`` existed but
was never driven, and even the documented intent only popped the bridge —
the ``_inference_providers`` sidecar entry, the ◆R2 tombstone, and the
durable ``peer_binding`` row all survived. Consequences: a
roleless-bound-this-lifetime holder whose bridge died read as NEVER-bound
(no tombstone → resolver case 3c → DEFAULT = silent-Qwen, the governing-rule
violation), and zombie registry rows accumulated forever (40+ stale June
rows observed).

The fix has two halves:

* :func:`run_full_bridge_cleanup` — the single cleanup sequence both the
  explicit ``close_bridge`` route and the idle sweeper run, so an
  idle-swept session is INDISTINGUISHABLE from a cleanly-closed one. Step
  order is preserved from the historical close route: sidecar clear (+
  tombstone) → the sys:autonomic Trigger-2 hook → registry unregister.
  The first two are best-effort (cleanup must converge even if one layer
  faults); the unregister is the durable-state ground truth and always
  runs.
* :class:`BridgeLifecycleSweeper` — the missing DRIVER: a daemon thread
  that periodically runs ``sweep_idle`` and routes every expired bridge
  through the full cleanup. In-process by design (deterministic plugin
  logic, same idiom as the Trigger-2 grace timer — not a scheduling-service
  model turn).

Trigger-2 interaction (deliberate decision, coordinator-concurred): an
idle-swept ``sys:autonomic`` holder DOES fire grace succession — swept is
exactly the ended/crash class the succession exists for, and the grace
window + the reconnect NO-OP absorb a holder that comes back.
"""

from __future__ import annotations

import logging
import threading
from typing import TYPE_CHECKING, Any, Protocol

if TYPE_CHECKING:
    from collections.abc import Callable

logger = logging.getLogger(__name__)


class _SweepableBridgeManager(Protocol):
    """The one ``BridgeSessionManager`` method the sweeper drives."""

    def sweep_idle(self) -> list[str]: ...


class _ReconcilableRegistry(Protocol):
    """The ``PeerRegistry`` surface the startup reconciliation walks."""

    def list_agent_ids(self) -> dict[str, list[Any]]: ...

    def unregister(self, bridge_id: str) -> int: ...


def purge_preboot_bindings(registry: _ReconcilableRegistry) -> int:
    """Startup reconciliation: purge every persisted pre-restart peer_binding.

    A blue-green swap / restart SIGTERM-severs every bridge without a close,
    so the persistent registry rows outlive their bridges (the 40+ stale
    June rows class). At ``start_interface`` time ZERO live bridges exist —
    every row present references a dead bridge by construction — so the
    whole snapshot is purged. Live sessions re-register within seconds
    (auto-registration on transport open); until then a send to a purged
    peer fails LOUD (``peer_unreachable``) instead of false-succeeding into
    a dead bridge's queue.
    """
    grouped = registry.list_agent_ids()
    bridge_ids = {
        binding.bridge_id
        for bindings in grouped.values()
        for binding in bindings
    }
    removed = 0
    for bridge_id in sorted(bridge_ids):
        removed += registry.unregister(bridge_id)
    if removed:
        logger.warning(
            "startup reconciliation: purged %d zombie peer_binding row(s) "
            "across %d pre-restart bridge(s); live sessions re-register on "
            "reconnect", removed, len(bridge_ids),
        )
    return removed


def run_full_bridge_cleanup(
    bridge_id: str,
    *,
    inference_provider_clear: Callable[[str], int] | None,
    autonomic_on_close: Callable[[str], str] | None,
    unregister: Callable[[str], int],
) -> int:
    """Run unregister's FULL cleanup for one departing bridge.

    Returns the number of peer_binding rows removed. Callers invoke this
    BEFORE the bridge session itself is closed/popped when possible (the
    close route) or immediately after the sweep popped it (the sweeper) —
    both work because every step keys on ``bridge_id`` and reads the
    registry rows, which exist until the final unregister here.
    """
    if inference_provider_clear is not None:
        try:
            inference_provider_clear(bridge_id)
        except Exception:  # noqa: BLE001 — sidecar cleanup is best-effort; unregister must still run
            logger.warning(
                "bridge cleanup: inference_provider_clear raised for %s; "
                "continuing", bridge_id, exc_info=True,
            )
    if autonomic_on_close is not None:
        try:
            autonomic_on_close(bridge_id)
        except Exception:  # noqa: BLE001 — lifecycle policy never blocks the cleanup
            logger.warning(
                "bridge cleanup: autonomic_on_close raised for %s; "
                "continuing", bridge_id, exc_info=True,
            )
    return unregister(bridge_id)


class BridgeLifecycleSweeper:
    """Drive ``sweep_idle`` on an interval and fully clean every expired bridge.

    Daemon thread; ``stop()`` is idempotent and joins the thread. A sweep
    tick that faults logs loud and the loop continues — a transient state
    fault must not kill the platform's only idle-reaper.
    """

    def __init__(
        self,
        *,
        bridge_manager: _SweepableBridgeManager,
        cleanup: Callable[[str], int],
        interval_seconds: int,
        on_tick: Callable[[], object] | None = None,
    ) -> None:
        self._bridge_manager = bridge_manager
        self._cleanup = cleanup
        self._interval_seconds = interval_seconds
        # Optional per-tick rider (INF-02 completion serve-timeout sweep
        # piggybacks the existing cadence instead of growing its own
        # thread). Runs AFTER the idle sweep; a rider fault is logged loud
        # and never kills the reaper.
        self._on_tick = on_tick
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            raise RuntimeError("BridgeLifecycleSweeper already running")
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._run, name="bridge-lifecycle-sweeper", daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=5)
        self._thread = None

    def sweep_once(self) -> list[str]:
        """One sweep tick: expire idle bridges, fully clean each. Testable seam."""
        expired = self._bridge_manager.sweep_idle()
        for bridge_id in expired:
            removed = self._cleanup(bridge_id)
            logger.info(
                "idle sweep: bridge %s expired → full cleanup "
                "(%d binding(s) unregistered)", bridge_id, removed,
            )
        if self._on_tick is not None:
            try:
                self._on_tick()
            except Exception:  # noqa: BLE001 — a rider fault must not kill the reaper
                logger.exception("sweeper on_tick rider FAULTED; sweeper continues")
        return expired

    def _run(self) -> None:
        while not self._stop_event.wait(self._interval_seconds):
            try:
                self.sweep_once()
            except Exception:  # noqa: BLE001 — the reaper must survive transient faults, loudly
                logger.exception("idle sweep tick FAULTED; sweeper continues")


__all__ = [
    "BridgeLifecycleSweeper",
    "purge_preboot_bindings",
    "run_full_bridge_cleanup",
]
