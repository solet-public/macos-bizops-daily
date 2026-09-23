#!/usr/bin/env python3
"""B2 crash-consistency + process-identity smoke — the finisher cannot orphan,
mis-kill a reused pid, act before the swap is durable, run off the active
instance, or crash the heartbeat loop.

Codex round-1 BLOCKER 2: ``complete_swap``'s enqueue runs AFTER the irreversible
symlink swap; if ``submit_action_definition`` throws, the prior is left
alive-but-inactive with no reconcile → an orphan. Codex round-2 reopened it with
four deeper gaps, each pinned here:

* B2·1 — the record is INERT until ``current`` names its ``candidate_release_id``
  (a tick in the ``{record-write → swap}`` window, or after an aborted swap,
  never acts): ``SKIPPED_NOT_DURABLE``.
* B2·2 — only the router-ACTIVE instance finishes a swap; any other
  same-solet heartbeat is fenced off: ``SKIPPED_NOT_ACTIVE``.
* B2·3 — a recycled pid is unmasked by a start-time identity token and
  unregistered-without-signalling, NEVER killed (``UNREGISTERED_PID_REUSED``);
  and a raising ``os.kill`` / ``unregister`` cannot crash the heartbeat thread.
  Both the backstop AND the normal ``complete_swap`` path enforce this.
* B2·4 — these scenarios (durability window, active fence, identity mismatch,
  raise-safety) on top of the round-1 matrix.

Real ``ps`` start-time tokens + real throwaway subprocesses exercise the genuine
identity mechanism (not just stubs). Scratch lives under ``~/.ananta`` (operator
NO-/tmp rule), cleaned in ``finally``; the solet names are throwaways, so
nothing touches the live solet's state.

Standalone — not pytest::

    .venv/bin/python3 plugins/macos_self_deployment_plugin/tests/complete_swap_crash_consistency_smoke.py
"""

from __future__ import annotations

import logging
import os
import signal
import subprocess
import sys
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Any, cast

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT / "plugins" / "macos_self_deployment_plugin" / "src"))
sys.path.insert(0, str(REPO_ROOT / "ananta" / "src"))

from ananta.interfaces.lifecycle_result_types import RestartStatus  # noqa: E402
from macos_self_deployment_plugin import process_identity  # noqa: E402
from macos_self_deployment_plugin.constants import (  # noqa: E402
    FINISHER_ESCALATION_NEEDS_INTERVENTION,
)
from macos_self_deployment_plugin.heartbeat_lifecycle import (  # noqa: E402
    _CONVERGED_OUTCOMES,
    RECONCILE_SKIPPED_NOT_ACTIVE,
    RECONCILE_SKIPPED_NOT_DURABLE,
    RECONCILE_SKIPPED_SELF,
    RECONCILE_TERMINATE_FAILED,
    RECONCILE_TERMINATED_ORPHAN,
    RECONCILE_UNREGISTERED_DEAD,
    RECONCILE_UNREGISTERED_PID_REUSED,
    _run_pending_finisher_backstop,
    reconcile_pending_finisher,
)
from macos_self_deployment_plugin.pending_finisher import (  # noqa: E402
    PendingFinisher,
    clear_pending_finisher,
    pending_finisher_path,
    read_pending_finisher,
    write_pending_finisher,
)
from macos_self_deployment_plugin.plugin import MacosSelfDeploymentPlugin, _runtime_dir  # noqa: E402
from macos_self_deployment_plugin.release_manager import (  # noqa: E402
    CandidatePaths,
    SwapResult,
)
from macos_self_deployment_plugin.router_client import RouterClient, RouterClientError  # noqa: E402
from macos_self_deployment_plugin.swap_executor import (  # noqa: E402
    SetColorActiveFn,
    SpawnFn,
    SwapExecutor,
)

_passed = 0
_failed: list[str] = []
_LOGGER = logging.getLogger("b2_crash_consistency_smoke")
_CANDIDATE_REL = "rel-new"


def _check(condition: object, label: str) -> None:
    global _passed
    if condition:
        _passed += 1
        print(f"  PASS  {label}")
    else:
        _failed.append(label)
        print(f"  FAIL  {label}")


def _rec(
    *,
    pid: int = 999,
    iid: str = "example-blue-1",
    color: str = "blue",
    candidate: str = _CANDIDATE_REL,
    token: str | None = "tok-blue",
) -> PendingFinisher:
    return PendingFinisher(
        prior_pid=pid, prior_instance_id=iid, prior_color=color,
        candidate_release_id=candidate, prior_start_token=token,
    )


class _Spy:
    """Records terminate(pid)/unregister(iid); terminate returns a configured bool."""

    def __init__(self, *, terminate_ok: bool = True) -> None:
        self.terminated: list[int] = []
        self.unregistered: list[str] = []
        self._terminate_ok = terminate_ok

    def terminate(self, pid: int) -> bool:
        self.terminated.append(pid)
        return self._terminate_ok

    def unregister(self, instance_id: str) -> None:
        self.unregistered.append(instance_id)


def _reconcile(
    rec: PendingFinisher, *, spy: _Spy, token: str | None = "tok-blue",
    self_iid: str = "example-green-2", active: str | None = "example-green-2",
    current: str | None = _CANDIDATE_REL,
) -> str:
    """Drive reconcile with the happy defaults; each case overrides one knob."""
    return reconcile_pending_finisher(
        record=rec, self_instance_id=self_iid, active_instance_id=active,
        current_release=current,
        start_token=lambda _p: token, terminate=spy.terminate, unregister=spy.unregister,
    )


class _RaisingActionFactory:
    """submit_action_definition raises — models a StateService failure."""

    def submit_action_definition(
        self, action_definition: dict[str, object], context: dict[str, object] | None = None,
    ) -> str:
        del action_definition, context
        msg = "injected StateService failure after a durable cutover"
        raise RuntimeError(msg)


class _StubRouter:
    """Never-called router placeholder (the executor paths touch no router)."""


class _FakeRouter:
    """Configurable router for the production-wrapper tests."""

    def __init__(
        self, *, status_payload: dict[str, Any],
        status_raises: bool = False, unregister_raises: bool = False,
    ) -> None:
        self._status_payload = status_payload
        self._status_raises = status_raises
        self._unregister_raises = unregister_raises
        self.unregistered: list[str] = []

    def status(self) -> dict[str, Any]:
        if self._status_raises:
            raise RouterClientError("status", "injected status() failure")
        return self._status_payload

    def unregister_color(self, instance_id: str) -> dict[str, Any]:
        self.unregistered.append(instance_id)
        if self._unregister_raises:
            raise RouterClientError("unregister_color", "injected unregister failure")
        return {"unregistered": True}


def _router_status(*, active: str, known: tuple[str, ...]) -> dict[str, Any]:
    return {
        "active_instance_id": active,
        "colors": [{"instance_id": iid} for iid in known],
        "drain_entries": [],
    }


def _spawn_sleeper() -> subprocess.Popen[bytes]:
    return subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])  # noqa: S603


def _make_executor(runtime_dir: Path, solet_name: str) -> SwapExecutor:
    return SwapExecutor(
        router_client=cast("RouterClient", _StubRouter()),
        action_factory=_RaisingActionFactory(),
        session_factory=lambda: "sess-b2-smoke",
        solet_name=solet_name,
        runtime_dir=runtime_dir,
        set_color_active=cast("SetColorActiveFn", lambda _active: None),
        spawn_fn=cast("SpawnFn", lambda *_a: 0),
        logger=_LOGGER,
        ready_timeout_seconds=1,
        ready_poll_interval_seconds=0.01,
        post_activate_grace_seconds=0.0,
    )


def _fake_candidate() -> CandidatePaths:
    base = Path("/scratch") / _CANDIDATE_REL
    return CandidatePaths(
        release_id=_CANDIDATE_REL, release_dir=base, code_root=base / "code",
        venv_python=base / "venv" / "bin" / "python3", version_file=base / "VERSION",
        missing_pth_targets=(), schema_snapshot=None,
    )


def test_module_round_trip(tmp: Path) -> None:
    print("scenario 1: pending_finisher durability (new fields, corrupt, clear)")
    path = pending_finisher_path(tmp, "b2mod")
    record = _rec(pid=4321, iid="example-blue-x", token="Mon Jun 28 21:00:00 2026")
    write_pending_finisher(path, record)
    _check(read_pending_finisher(path) == record, "write→read round-trips (incl candidate+token)")
    none_token = _rec(token=None)
    write_pending_finisher(path, none_token)
    _check(read_pending_finisher(path) == none_token, "None start-token round-trips")
    path.write_text("{not json", encoding="utf-8")
    _check(read_pending_finisher(path) is None, "corrupt file → None (never raises)")
    clear_pending_finisher(path)
    _check(read_pending_finisher(path) is None, "cleared → None")
    clear_pending_finisher(path)
    _check(True, "clear is idempotent")


def test_reconcile_matrix() -> None:
    print("scenario 2: reconcile decision matrix (all 8 outcomes + clear-decision)")
    spy = _Spy()
    out = _reconcile(_rec(), spy=spy)
    _check(out == RECONCILE_TERMINATED_ORPHAN, "verified live orphan → terminated_orphan")
    _check(spy.terminated == [999] and spy.unregistered == ["example-blue-1"], "orphan SIGTERM'd + unregistered")

    spy = _Spy()
    out = _reconcile(_rec(iid="example-green-2"), spy=spy)
    _check(out == RECONCILE_SKIPPED_SELF, "prior==self → skipped_self")
    _check(not spy.terminated and not spy.unregistered, "skipped_self touched nothing")

    spy = _Spy()
    out = _reconcile(_rec(), spy=spy, active="example-other")
    _check(out == RECONCILE_SKIPPED_NOT_ACTIVE, "self not router-active → skipped_not_active (B2·2)")
    _check(not spy.terminated, "non-active fence did NOT SIGTERM")

    spy = _Spy()
    out = _reconcile(_rec(), spy=spy, current="rel-OLD")
    _check(out == RECONCILE_SKIPPED_NOT_DURABLE, "current!=candidate → skipped_not_durable (B2·1)")
    _check(not spy.terminated, "pre-durable window did NOT SIGTERM")

    # CYCLE-3 ROLLBACK REGRESSION (2026-06-29): a prior whose 30s router drain
    # entry EXPIRED (so it is absent from the router's known set) but is STILL
    # ALIVE (live token matches) must be TERMINATED — NOT treated as converged +
    # cleared. Pre-fix the backstop gated on router registration and cleared the
    # record on this live process → the complete_swap finisher then no-op'd and
    # the old instance lingered holding :9000. Liveness is now authoritative.
    #
    # NOTE (label honesty): post-fix this case is INPUT-IDENTICAL to the
    # verified-live-orphan case above — the fix DELETED the router-registration
    # input from reconcile_pending_finisher's signature, so "drain-expired" is no
    # longer an expressible input here. The protection is therefore ARCHITECTURAL
    # (the buggy field is gone), not a distinct gate this case exercises; it
    # re-asserts the alive→terminate decision. The full in-loop reap under a
    # suppressed normal finisher is observed end-to-end in
    # backstop_loop_integration_smoke.py.
    spy = _Spy()
    out = _reconcile(_rec(), spy=spy)
    _check(
        out == RECONCILE_TERMINATED_ORPHAN,
        "alive prior whose drain entry expired → TERMINATED_ORPHAN (cycle-3 fix, not cleared)",
    )
    _check(
        spy.terminated == [999] and spy.unregistered == ["example-blue-1"],
        "the live drain-expired orphan was SIGTERM'd + unregistered",
    )

    spy = _Spy()
    out = _reconcile(_rec(), spy=spy, token=None)
    _check(out == RECONCILE_UNREGISTERED_DEAD, "live token None (dead) → unregistered_dead")
    _check(not spy.terminated and spy.unregistered == ["example-blue-1"], "dead → unregister only, NO SIGTERM")

    spy = _Spy()
    out = _reconcile(_rec(token="tok-blue"), spy=spy, token="tok-DIFFERENT")
    _check(out == RECONCILE_UNREGISTERED_PID_REUSED, "token mismatch → unregistered_pid_reused (B2·3)")
    _check(not spy.terminated and spy.unregistered == ["example-blue-1"], "reused pid → unregister only, NEVER SIGTERM")

    spy = _Spy(terminate_ok=False)
    out = _reconcile(_rec(), spy=spy)
    _check(out == RECONCILE_TERMINATE_FAILED, "terminate returns False → terminate_failed")

    print("  -- clear-decision (record dropped ONLY on converged outcomes) --")
    _check(
        _CONVERGED_OUTCOMES == frozenset({
            RECONCILE_TERMINATED_ORPHAN, RECONCILE_UNREGISTERED_DEAD,
            RECONCILE_UNREGISTERED_PID_REUSED,
        }),
        "converged set = {terminated, dead, pid_reused}",
    )
    for non_converged in (
        RECONCILE_SKIPPED_SELF, RECONCILE_SKIPPED_NOT_ACTIVE,
        RECONCILE_SKIPPED_NOT_DURABLE, RECONCILE_TERMINATE_FAILED,
    ):
        _check(non_converged not in _CONVERGED_OUTCOMES, f"{non_converged} keeps the record (no clear)")


def _reconcile_real(
    rec: PendingFinisher, *, terminate: Callable[[int], bool], unregister: Callable[[str], None],
) -> str:
    """Reconcile with the REAL ps-backed start_token + happy fixed gates."""
    return reconcile_pending_finisher(
        record=rec, self_instance_id="example-green-2", active_instance_id="example-green-2",
        current_release=_CANDIDATE_REL,
        start_token=process_identity.start_token, terminate=terminate, unregister=unregister,
    )


def test_reconcile_real_process() -> None:
    print("scenario 3: reconcile against REAL processes (real ps start-time tokens)")
    sleeper = _spawn_sleeper()
    try:
        real_token = process_identity.start_token(sleeper.pid)
        _check(real_token is not None, "real ps captured a start-time token for the live subprocess")
        # token MATCH → genuine orphan → real SIGTERM kills it.
        spy_un: list[str] = []

        def _real_kill(pid: int) -> bool:
            os.kill(pid, signal.SIGTERM)
            return True

        out = _reconcile_real(
            _rec(pid=sleeper.pid, token=real_token), terminate=_real_kill, unregister=spy_un.append,
        )
        _check(out == RECONCILE_TERMINATED_ORPHAN, "real token match → terminated_orphan")
        sleeper.wait(timeout=5)
        _check(sleeper.poll() is not None, "the real subprocess was actually SIGTERM'd + reaped")
    finally:
        if sleeper.poll() is None:
            sleeper.kill()
            sleeper.wait(timeout=5)

    # token MISMATCH against MY OWN live pid (stale recorded token) → never killed.
    spy = _Spy()
    out = _reconcile_real(
        _rec(pid=os.getpid(), token="Thu Jan  1 00:00:00 1970"),
        terminate=spy.terminate, unregister=spy.unregister,
    )
    _check(out == RECONCILE_UNREGISTERED_PID_REUSED, "real mismatch on my own live pid → pid_reused")
    _check(not spy.terminated, "smoke process (the 'reused' pid) was NOT SIGTERM'd")

    # genuinely dead pid → real ps returns None → unregistered_dead, no kill.
    dead = _spawn_sleeper()
    dead.kill()
    dead.wait(timeout=5)
    spy = _Spy()
    out = _reconcile_real(_rec(pid=dead.pid, token="anything"), terminate=spy.terminate, unregister=spy.unregister)
    _check(out == RECONCILE_UNREGISTERED_DEAD, "real dead pid → unregistered_dead")
    _check(not spy.terminated, "real dead pid was NOT SIGTERM'd")


def test_wrapper_resilience(tmp: Path) -> None:
    print("scenario 4: _run_pending_finisher_backstop never crashes the heartbeat loop")
    path = pending_finisher_path(tmp, "b2wrap")

    # status() raises → return, record untouched.
    write_pending_finisher(path, _rec())
    _run_pending_finisher_backstop(
        client=cast("RouterClient", _FakeRouter(status_payload={}, status_raises=True)),
        self_instance_id="example-green-2", pending_finisher_file=path,
        current_release_lookup=lambda: _CANDIDATE_REL, logger=_LOGGER,
    )
    _check(read_pending_finisher(path) is not None, "router status() raise → no crash, record kept")

    # current_release_lookup raises → return, record untouched.
    def _boom() -> str:
        raise RuntimeError("lookup boom")

    _run_pending_finisher_backstop(
        client=cast("RouterClient", _FakeRouter(
            status_payload=_router_status(active="example-green-2", known=("example-blue-1",)))),
        self_instance_id="example-green-2", pending_finisher_file=path,
        current_release_lookup=_boom, logger=_LOGGER,
    )
    _check(read_pending_finisher(path) is not None, "lookup raise → no crash, record kept")

    # happy path end-to-end: real subprocess orphan SIGTERM'd + record cleared.
    sleeper = _spawn_sleeper()
    try:
        token = process_identity.start_token(sleeper.pid)
        write_pending_finisher(path, _rec(pid=sleeper.pid, token=token))
        router = _FakeRouter(status_payload=_router_status(active="example-green-2", known=("example-blue-1",)))
        _run_pending_finisher_backstop(
            client=cast("RouterClient", router), self_instance_id="example-green-2",
            pending_finisher_file=path, current_release_lookup=lambda: _CANDIDATE_REL, logger=_LOGGER,
        )
        sleeper.wait(timeout=5)
        _check(sleeper.poll() is not None, "wrapper end-to-end SIGTERM'd the real orphan")
        _check(read_pending_finisher(path) is None, "converged → record cleared")
        _check(router.unregistered == ["example-blue-1"], "wrapper unregistered the prior")
    finally:
        if sleeper.poll() is None:
            sleeper.kill()
            sleeper.wait(timeout=5)

    # unregister raises on a converged outcome → still clears, no crash (dead prior path).
    dead = _spawn_sleeper()
    dead.kill()
    dead.wait(timeout=5)
    write_pending_finisher(path, _rec(pid=dead.pid))
    router = _FakeRouter(
        status_payload=_router_status(active="example-green-2", known=("example-blue-1",)),
        unregister_raises=True,
    )
    _run_pending_finisher_backstop(
        client=cast("RouterClient", router), self_instance_id="example-green-2",
        pending_finisher_file=path, current_release_lookup=lambda: _CANDIDATE_REL, logger=_LOGGER,
    )
    _check(read_pending_finisher(path) is None, "unregister raise on converged → no crash, record cleared")

    _wrapper_oskill_raise_safety(path)


def _raise(exc: BaseException) -> Any:
    raise exc


def _wrapper_oskill_raise_safety(path: Path) -> None:
    """os.kill raising inside _terminate must not crash the loop (monkeypatched).

    To reach the terminate step deterministically we point the record at our OWN
    live pid with a real matching token (registered+active+durable). os.kill is
    monkeypatched to RAISE instead of signalling, so the smoke is never killed.
    """
    real_kill = os.kill
    record = _rec(pid=os.getpid(), token=process_identity.start_token(os.getpid()))
    router = _FakeRouter(status_payload=_router_status(active="example-green-2", known=("example-blue-1",)))

    def _run() -> None:
        _run_pending_finisher_backstop(
            client=cast("RouterClient", router), self_instance_id="example-green-2",
            pending_finisher_file=path, current_release_lookup=lambda: _CANDIDATE_REL, logger=_LOGGER,
        )

    try:
        os.kill = lambda _pid, _sig: _raise(ProcessLookupError())  # type: ignore[assignment]
        write_pending_finisher(path, record)
        _run()
        _check(read_pending_finisher(path) is None, "os.kill ProcessLookupError → converged + cleared, no crash")

        os.kill = lambda _pid, _sig: _raise(PermissionError())  # type: ignore[assignment]
        write_pending_finisher(path, record)
        _run()
        _check(read_pending_finisher(path) is not None, "os.kill PermissionError → terminate_failed, record KEPT, no crash")
    finally:
        os.kill = real_kill


def test_executor_writes_record_before_swap(tmp: Path) -> None:
    print("scenario 5: executor writes the durable record (candidate+token) BEFORE the swap")
    executor = _make_executor(tmp, "b2exec")
    path = pending_finisher_path(tmp, "b2exec")
    clear_pending_finisher(path)
    prior_pid = os.getpid()
    prior_start_token = process_identity.start_token(prior_pid)
    swap = executor._swap_or_compensate(  # noqa: SLF001
        candidate=_fake_candidate(),
        symlink_swap=lambda _c: SwapResult(current=_CANDIDATE_REL, previous="rel-old"),
        prior_color="blue", self_instance_id="example-blue-1", instance_id="example-green-2",
        prior_pid=prior_pid, prior_start_token=prior_start_token,
        pid=999, start_token=None, reason="b2-smoke", expected_etag="etag",
        compensation_codes=("confirmed", "unconfirmed"),
        finisher_escalation=FINISHER_ESCALATION_NEEDS_INTERVENTION,
    )
    _check(isinstance(swap, SwapResult), "successful swap returned a SwapResult")
    record = read_pending_finisher(path)
    _check(record is not None, "record written before the swap")
    assert record is not None
    _check(record.prior_pid == prior_pid, "record names the supplied prior pid")
    _check(record.candidate_release_id == _CANDIDATE_REL, "record carries candidate_release_id (B2·1)")
    _check(
        record.prior_start_token == prior_start_token,
        "record carries the supplied start-time token (B2·3)",
    )
    _check(
        record.escalation == FINISHER_ESCALATION_NEEDS_INTERVENTION,
        "record carries the executor's finisher escalation policy (iss_8d1ec833)",
    )


def test_executor_enqueue_failure_is_non_fatal(tmp: Path) -> None:
    print("scenario 6: enqueue failure after a durable cutover → QUEUED, record persists")
    executor = _make_executor(tmp, "b2exec2")
    path = pending_finisher_path(tmp, "b2exec2")
    record = _rec(pid=os.getpid())
    write_pending_finisher(path, record)
    result = executor._finish_queued(  # noqa: SLF001
        next_color="green", next_instance_id="example-green-2",
        candidate_release_id=record.candidate_release_id,
        pid=999, prior_pid=record.prior_pid,
        prior_instance_id=record.prior_instance_id, prior_color=record.prior_color,
        prior_start_token=record.prior_start_token, poller_gate="local_service_quiesced",
        set_active_targets=[],
        activate_result={}, reason="b2-smoke", expected_etag="etag",
    )
    _check(result.status is RestartStatus.QUEUED, "enqueue raised, cutover still QUEUED (not failed)")
    _check(result.restart_action_id == "", "no action id when the enqueue failed")
    _check(read_pending_finisher(path) == record, "durable record PERSISTS (backstop will finish it)")


def test_complete_swap_normal_path() -> None:
    print("scenario 7: complete_swap normal path is record-driven + identity-guarded (B2·3)")
    solet = "b2completeswap"
    path = pending_finisher_path(_runtime_dir(), solet)
    plugin = MacosSelfDeploymentPlugin()
    plugin._solet_name = solet  # noqa: SLF001
    plugin._router_client = None  # noqa: SLF001 — unregister becomes a skip, identity is the focus
    try:
        # (a) record ABSENT → idempotent no-op (the backstop already converged).
        clear_pending_finisher(path)
        data = plugin.complete_swap(prior_pid=os.getpid(), prior_instance_id="example-blue-1", prior_color="blue")
        _check("pending_finisher_absent_noop" in data["steps_completed"], "absent record → no-op (no bare-pid kill)")

        # (b) identity MISMATCH on my own live pid (stale token) → never SIGTERM the smoke.
        write_pending_finisher(path, _rec(pid=os.getpid(), token="Thu Jan  1 00:00:00 1970"))
        data = plugin.complete_swap(prior_pid=os.getpid(), prior_instance_id="example-blue-1", prior_color="blue")
        _check(
            "prior_pid_reused_skip_sigterm" in data["steps_completed"],
            "token mismatch → skip SIGTERM (the live smoke process is not killed)",
        )
        _check(read_pending_finisher(path) is None, "complete_swap cleared the record after handling")

        # (c) real subprocess prior + matching token → real SIGTERM kills it.
        sleeper = _spawn_sleeper()
        try:
            token = process_identity.start_token(sleeper.pid)
            write_pending_finisher(path, _rec(pid=sleeper.pid, token=token))
            data = plugin.complete_swap(prior_pid=sleeper.pid, prior_instance_id="example-blue-1", prior_color="blue")
            sleeper.wait(timeout=5)
            _check(sleeper.poll() is not None, "complete_swap SIGTERM'd the real prior")
            _check("pending_finisher_cleared" in data["steps_completed"], "record cleared on the real path")
        finally:
            if sleeper.poll() is None:
                sleeper.kill()
                sleeper.wait(timeout=5)
    finally:
        clear_pending_finisher(path)


def main() -> int:
    print("=== complete_swap_crash_consistency_smoke (B2 round-2) ===")
    ananta_scratch = Path.home() / ".ananta"
    ananta_scratch.mkdir(exist_ok=True)
    tmp = Path(tempfile.mkdtemp(prefix="b2_smoke_", dir=str(ananta_scratch)))
    try:
        test_module_round_trip(tmp)
        test_reconcile_matrix()
        test_reconcile_real_process()
        test_wrapper_resilience(tmp)
        test_executor_writes_record_before_swap(tmp)
        test_executor_enqueue_failure_is_non_fatal(tmp)
        test_complete_swap_normal_path()
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
