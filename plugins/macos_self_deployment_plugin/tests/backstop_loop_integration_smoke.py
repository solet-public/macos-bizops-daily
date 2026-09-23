#!/usr/bin/env python3
"""Backstop loop-integration smoke — the pending-finisher backstop arms, reaps,
and clears INSIDE the real ``_run_steady_state_heartbeat`` loop (not the wrapper
called directly).

WHY THIS EXISTS (cycle-3 proving-run RCA, 2026-06-29). The live 5-cycle blue-green
proving run reaped every prior via the NORMAL ``complete_swap`` finisher winning
inside a ~10s drain; the heartbeat backstop ``reconcile_pending_finisher`` — the
exact function ``b6e847f`` rewrote — never fired its ``terminated_orphan`` path
live, because no drain ever ran long enough for the backstop to win the race. So
the backstop's live-loop reap was INFERRED from unit tests + construction, not
OBSERVED. ``complete_swap_crash_consistency_smoke`` proves the decision matrix and
calls ``_run_pending_finisher_backstop`` DIRECTLY; this smoke closes the last gap
by driving the REAL steady-state heartbeat loop in a thread, with the normal
finisher SUPPRESSED (it is simply never enqueued), so the backstop is the SOLE
reaper — exactly the ``test_executor_enqueue_failure_is_non_fatal`` condition,
now observed end-to-end through the loop.

Three scenarios, each against REAL subprocesses + REAL ps start-time tokens +
REAL ``os.kill`` (no stubbed terminate), so the test cannot false-green:

* reap — green active, a drain-expired-but-ALIVE prior (absent from the router's
  known set, live token matches): the loop tick SIGTERMs it for real, the process
  actually dies, the durable record is cleared, ``terminated_orphan`` is logged.
* not-durable skip (anti-rig) — the SAME live prior, but ``current`` does not yet
  name the candidate: the loop must NOT kill it and must KEEP the record. Proves
  the reap is gated, not rigged to always fire.
* already-cleared no-op — the normal finisher already converged + cleared the
  record: the loop tick reaps nothing, a control subprocess survives.

Scratch lives under ``~/.ananta`` (operator NO-/tmp rule), cleaned in ``finally``;
the solet names are throwaways + the subprocesses are inert sleepers, so
nothing touches the live solet's state.

Standalone — not pytest::

    .venv/bin/python3 plugins/macos_self_deployment_plugin/tests/backstop_loop_integration_smoke.py
"""

from __future__ import annotations

import logging
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any, cast

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT / "plugins" / "macos_self_deployment_plugin" / "src"))
sys.path.insert(0, str(REPO_ROOT / "ananta" / "src"))

from macos_self_deployment_plugin import process_identity  # noqa: E402
from macos_self_deployment_plugin.heartbeat_lifecycle import (  # noqa: E402
    _run_steady_state_heartbeat,
)
from macos_self_deployment_plugin.pending_finisher import (  # noqa: E402
    PendingFinisher,
    clear_pending_finisher,
    pending_finisher_path,
    read_pending_finisher,
    write_pending_finisher,
)
from macos_self_deployment_plugin.router_client import RouterClient  # noqa: E402

_passed = 0
_failed: list[str] = []

_CANDIDATE_REL = "rel-new"
_SELF_IID = "example-green-2"
_PRIOR_IID = "example-blue-1"
_DUMMY_PORT = 0
_POLL_INTERVAL_SECONDS = 0.05
_OUTCOME_DEADLINE_SECONDS = 5.0
_SETTLE_SECONDS = 0.2


def _check(condition: object, label: str) -> None:
    global _passed
    if condition:
        _passed += 1
        print(f"  PASS  {label}")
    else:
        _failed.append(label)
        print(f"  FAIL  {label}")


class _CapturingHandler(logging.Handler):
    """Records every formatted log message so a test can assert the outcome token."""

    def __init__(self) -> None:
        super().__init__()
        self.messages: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.messages.append(record.getMessage())


class _LoopRouter:
    """Minimal router for the loop: alive heartbeats, green active, prior absent.

    The prior is omitted from ``colors`` + ``drain_entries`` to model its 30s
    router drain entry having EXPIRED while the process is still alive — the exact
    cycle-3 condition. ``terminate`` is NOT stubbed anywhere: the backstop's real
    ``os.kill`` is what the reaped subprocess receives.
    """

    def __init__(self, *, active: str) -> None:
        self._active = active
        self.heartbeats: list[str] = []
        self.unregistered: list[str] = []

    def heartbeat(self, instance_id: str) -> dict[str, Any]:
        self.heartbeats.append(instance_id)
        return {"alive": True}

    def status(self) -> dict[str, Any]:
        # active_color present -> the loop's Part 15 steady-state re-assert
        # (_ensure_active_color on healthy ticks) is a no-op here.
        return {
            "active_color": "green",
            "active_instance_id": self._active,
            "colors": [{"instance_id": self._active}],
            "drain_entries": [],
        }

    def unregister_color(self, instance_id: str) -> dict[str, Any]:
        self.unregistered.append(instance_id)
        return {"unregistered": True}


def _spawn_sleeper() -> subprocess.Popen[bytes]:
    return subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])  # noqa: S603


def _record_for(pid: int, token: str | None) -> PendingFinisher:
    return PendingFinisher(
        prior_pid=pid,
        prior_instance_id=_PRIOR_IID,
        prior_color="blue",
        candidate_release_id=_CANDIDATE_REL,
        prior_start_token=token,
    )


def _fresh_logger() -> tuple[logging.Logger, _CapturingHandler]:
    logger = logging.getLogger("backstop_loop_integration_smoke")
    logger.handlers.clear()
    handler = _CapturingHandler()
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    logger.propagate = False
    return logger, handler


def _start_loop(
    *,
    client: _LoopRouter,
    path: Path,
    current_lookup: Callable[[], str | None],
    logger: logging.Logger,
    stop_event: threading.Event,
) -> threading.Thread:
    """Run the REAL steady-state heartbeat loop in a daemon thread."""

    def _run() -> None:
        _run_steady_state_heartbeat(
            client=cast("RouterClient", client),
            port=_DUMMY_PORT,
            self_color="green",
            self_instance_id=_SELF_IID,
            stop_event=stop_event,
            pending_finisher_file=path,
            current_release_lookup=current_lookup,
            logger=logger,
            set_color_active=lambda _active: None,
        )

    thread = threading.Thread(target=_run, name="loop-int-smoke", daemon=True)
    thread.start()
    return thread


def _poll_until(predicate: Callable[[], bool], *, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(_POLL_INTERVAL_SECONDS)
    return predicate()


def _terminated_orphan_logged(handler: _CapturingHandler) -> bool:
    return any("terminated_orphan" in message for message in handler.messages)


def test_loop_reaps_drain_expired_alive_prior(tmp: Path) -> None:
    print("scenario 1: REAL heartbeat loop reaps a drain-expired-but-ALIVE prior")
    path = pending_finisher_path(tmp, "loopint1")
    clear_pending_finisher(path)
    sleeper = _spawn_sleeper()
    try:
        token = process_identity.start_token(sleeper.pid)
        _check(token is not None, "real ps start-time token captured for the live prior")
        write_pending_finisher(path, _record_for(sleeper.pid, token))
        # "drain-expired" is COSMETIC here: the fixed reconcile reads only
        # active_instance_id + process liveness, NOT router registration, so
        # whether the prior is listed in status() is indistinguishable. What is
        # actually exercised is the real differential behavior — alive-prior →
        # in-loop reap (terminated_orphan); the drain-expiry framing just names
        # the cycle-3 condition the b6e847f architectural fix made irrelevant.
        router = _LoopRouter(active=_SELF_IID)
        logger, handler = _fresh_logger()
        stop_event = threading.Event()
        thread = _start_loop(
            client=router, path=path, current_lookup=lambda: _CANDIDATE_REL,
            logger=logger, stop_event=stop_event,
        )
        try:
            reaped = _poll_until(
                lambda: read_pending_finisher(path) is None, timeout=_OUTCOME_DEADLINE_SECONDS,
            )
        finally:
            stop_event.set()
            thread.join(timeout=_OUTCOME_DEADLINE_SECONDS)
        _check(reaped, "the loop TICK converged the backstop (durable record cleared)")
        sleeper.wait(timeout=5)
        _check(sleeper.poll() is not None, "the REAL prior subprocess was actually SIGTERM'd + reaped IN-LOOP")
        _check(read_pending_finisher(path) is None, "durable record CLEARED after the reap")
        _check(_PRIOR_IID in router.unregistered, "the loop unregistered the reaped prior")
        _check(_terminated_orphan_logged(handler), "loop backstop logged terminated_orphan (new reap path)")
        _check(not thread.is_alive(), "the heartbeat loop thread stopped cleanly")
    finally:
        if sleeper.poll() is None:
            sleeper.kill()
            sleeper.wait(timeout=5)
        clear_pending_finisher(path)


def test_loop_does_not_kill_when_not_durable(tmp: Path) -> None:
    print("scenario 2: REAL loop does NOT kill a live prior when the durability gate fails")
    path = pending_finisher_path(tmp, "loopint2")
    clear_pending_finisher(path)
    sleeper = _spawn_sleeper()
    try:
        token = process_identity.start_token(sleeper.pid)
        write_pending_finisher(path, _record_for(sleeper.pid, token))
        router = _LoopRouter(active=_SELF_IID)
        logger, handler = _fresh_logger()
        stop_event = threading.Event()
        # current != candidate → SKIPPED_NOT_DURABLE: the live prior MUST survive.
        thread = _start_loop(
            client=router, path=path, current_lookup=lambda: "rel-OTHER",
            logger=logger, stop_event=stop_event,
        )
        try:
            ticked = _poll_until(lambda: bool(router.heartbeats), timeout=_OUTCOME_DEADLINE_SECONDS)
            time.sleep(_SETTLE_SECONDS)
        finally:
            stop_event.set()
            thread.join(timeout=_OUTCOME_DEADLINE_SECONDS)
        _check(ticked, "the loop ran at least one heartbeat tick")
        _check(sleeper.poll() is None, "the live prior was NOT killed (durability gate skipped the reap)")
        _check(read_pending_finisher(path) is not None, "the durable record was KEPT (no premature clear)")
        _check(not _terminated_orphan_logged(handler), "no terminated_orphan logged on the durability skip")
    finally:
        if sleeper.poll() is None:
            sleeper.kill()
            sleeper.wait(timeout=5)
        clear_pending_finisher(path)


def test_loop_noops_when_record_already_cleared(tmp: Path) -> None:
    print("scenario 3: REAL loop no-ops when the normal finisher already cleared the record")
    path = pending_finisher_path(tmp, "loopint3")
    clear_pending_finisher(path)  # normal complete_swap finisher already converged + cleared
    control = _spawn_sleeper()
    try:
        router = _LoopRouter(active=_SELF_IID)
        logger, handler = _fresh_logger()
        stop_event = threading.Event()
        thread = _start_loop(
            client=router, path=path, current_lookup=lambda: _CANDIDATE_REL,
            logger=logger, stop_event=stop_event,
        )
        try:
            ticked = _poll_until(lambda: bool(router.heartbeats), timeout=_OUTCOME_DEADLINE_SECONDS)
            time.sleep(_SETTLE_SECONDS)
        finally:
            stop_event.set()
            thread.join(timeout=_OUTCOME_DEADLINE_SECONDS)
        _check(ticked, "the loop ran at least one heartbeat tick with no record present")
        _check(control.poll() is None, "no process was killed when there was no orphan record")
        _check(read_pending_finisher(path) is None, "record stays absent (idempotent no-op)")
        _check(not _terminated_orphan_logged(handler), "no reap path fired on the no-op")
    finally:
        if control.poll() is None:
            control.kill()
            control.wait(timeout=5)
        clear_pending_finisher(path)


def main() -> int:
    print("=== backstop_loop_integration_smoke (live-loop reap, offline) ===")
    scratch = Path.home() / ".ananta"
    scratch.mkdir(exist_ok=True)
    tmp = Path(tempfile.mkdtemp(prefix="loopint_smoke_", dir=str(scratch)))
    try:
        test_loop_reaps_drain_expired_alive_prior(tmp)
        test_loop_does_not_kill_when_not_durable(tmp)
        test_loop_noops_when_record_already_cleared(tmp)
    finally:
        for child in tmp.iterdir():
            child.unlink()
        tmp.rmdir()
    print(f"\n{_passed} passed, {len(_failed)} failed")
    if _failed:
        for label in _failed:
            print(f"  FAILED: {label}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
