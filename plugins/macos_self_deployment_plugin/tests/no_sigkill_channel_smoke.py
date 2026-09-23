#!/usr/bin/env python3
"""``no_sigkill_reachable_from_this_channel`` — behavioural negative smoke.

Adjudication D1 (the 2026-09-03 pair-150 cutover-interface adjudication, §3)
forbids signal 9 anywhere on the reconciliation / ``cutover_release`` channel;
the 2026-09-21 D-1.x capability audit (§2) found it REACHABLE at six sites:
``GreenCandidate.kill`` (``os.kill(pid, 9)``)
from the executor's readiness, activate-error, activate-refusal and
confirmed-rollback compensation legs [K1a–d], and the ``complete_swap``
finisher's SIGKILL-after-grace escalation [K2]. iss_8d1ec833 / iss_351345f7.

This is not a keyword scan. Every leg drives the REAL shared seam
(``SwapExecutor.execute`` / ``MacosSelfDeploymentPlugin.complete_swap`` /
``_run_pending_finisher_backstop``) against a REAL child process that
``SIG_IGN``s SIGTERM. Such a child can only die from signal 9 — so "the child
is still alive afterwards" is the sharpest possible assertion that no SIGKILL
was issued, and on the pre-fix tree every leg fails because the child is dead.

Per leg (SIG_IGN child): the child is STILL ALIVE (``_child_exited`` False),
the result is ``NEEDS_INTERVENTION`` whose message names the pid AND the
spawn-time start token, and the router ``unregister`` was called (D-1.7 — the
activate legs did not unregister at all before this fix). Control (a child
that honours SIGTERM): it exits and the result is the retryable ``FAILED``.

K2: the same SIG_IGN child as the PRIOR, a ``PendingFinisher`` with
``escalation=needs_intervention``; ``complete_swap`` returns
``prior_sigterm_timeout_needs_intervention``, child alive, record KEPT.
Control: ``escalation=sigkill_after_grace`` (an ordinary service-driven swap)
still runs today's ladder. Backstop: the heartbeat backstop's ``_terminate``
now VERIFIES exit before ``TERMINATED_ORPHAN`` can clear the record — a prior
that ignores SIGTERM yields ``TERMINATE_FAILED`` with the record kept.

Scratch lives under ``~/.ananta`` (operator NO-/tmp rule); grace windows are
shrunk so the whole smoke runs in seconds. Standalone — not pytest::

    .venv/bin/python3 plugins/macos_self_deployment_plugin/tests/no_sigkill_channel_smoke.py
"""

from __future__ import annotations

import logging
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any, cast

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT / "plugins" / "macos_self_deployment_plugin" / "src"))
sys.path.insert(0, str(REPO_ROOT / "ananta" / "src"))

from ananta.interfaces.lifecycle_result_types import RestartStatus  # noqa: E402
from macos_self_deployment_plugin import (  # noqa: E402
    green_candidate,
    heartbeat_lifecycle,
    process_identity,
)
from macos_self_deployment_plugin import plugin as plugin_module  # noqa: E402
from macos_self_deployment_plugin.constants import (  # noqa: E402
    COLOR_BLUE,
    COLOR_GREEN,
    FINISHER_ESCALATION_NEEDS_INTERVENTION,
    FINISHER_ESCALATION_SIGKILL_AFTER_GRACE,
    RestartReasonCode,
)
from macos_self_deployment_plugin.green_candidate import _child_exited  # noqa: E402
from macos_self_deployment_plugin.heartbeat_lifecycle import (  # noqa: E402
    RECONCILE_TERMINATE_FAILED,
    RECONCILE_TERMINATED_ORPHAN,
    _run_pending_finisher_backstop,
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
    ReleaseManagerError,
    SwapResult,
)
from macos_self_deployment_plugin.router_client import (  # noqa: E402
    RouterClient,
    RouterClientError,
)
from macos_self_deployment_plugin.swap_executor import (  # noqa: E402
    SetColorActiveFn,
    SwapExecutor,
)

_LOGGER = logging.getLogger("no_sigkill_channel_smoke")
_LOGGER.addHandler(logging.NullHandler())

_passed = 0
_failed: list[str] = []

# Small windows: the SIG_IGN child never exits, so every timed-out leg costs
# exactly this long; the SIGTERM-honouring control exits in milliseconds.
_CANDIDATE_GRACE_SECONDS = 0.6
_FINISHER_GRACE_SECONDS = 0.6
_BACKSTOP_VERIFY_SECONDS = 0.6
_READY_TIMEOUT_SECONDS = 1

_CANDIDATE_REL = "rel-nosigkill"
_PRIOR_IID = "solet-blue-prior"
_SELF_IID = "solet-green-self"

# Leg names (the executor's four post-spawn failure legs, audit §2.1 K1a–d).
_LEG_READINESS = "readiness timeout"
_LEG_ACTIVATE_RPC = "activate RPC error"
_LEG_ACTIVATE_REFUSED = "activate refusal"
_LEG_SYMLINK_ROLLBACK_OK = "symlink raise + confirmed rollback"
_LEGS = (_LEG_READINESS, _LEG_ACTIVATE_RPC, _LEG_ACTIVATE_REFUSED, _LEG_SYMLINK_ROLLBACK_OK)

_COMPENSATION_CODES = (
    RestartReasonCode.CUTOVER_COMPENSATED,
    RestartReasonCode.CUTOVER_ROUTER_ROLLBACK_FAILED,
)


def _check(condition: object, label: str) -> None:
    global _passed
    if condition:
        _passed += 1
        print(f"  PASS  {label}")
    else:
        _failed.append(label)
        print(f"  FAIL  {label}")


# ---------------------------------------------------------------------------
# Real children: one that can only die from signal 9, one that honours SIGTERM.
# ---------------------------------------------------------------------------


def _spawn_ready_child(body: str) -> subprocess.Popen[bytes]:
    """Spawn ``body`` and block until it has printed ``ready``.

    The interpreter needs tens of milliseconds to start; a SIGTERM that lands
    before ``signal.signal`` has run kills even the "ignoring" child with the
    default action, which would make a genuine no-SIGKILL fix look like a
    SIGKILL. Waiting for the child's own ``ready`` line removes that race, so
    a dead SIG_IGN child afterwards can only mean signal 9.
    """
    child = subprocess.Popen(  # noqa: S603
        [
            sys.executable, "-c",
            f"import signal, sys, time; {body}; sys.stdout.write('ready\\n'); "
            "sys.stdout.flush(); time.sleep(60)",
        ],
        stdout=subprocess.PIPE,
    )
    assert child.stdout is not None
    line = child.stdout.readline()
    child.stdout.close()
    if line.strip() != b"ready":
        child.kill()
        msg = f"smoke child failed to start: {line!r}"
        raise RuntimeError(msg)
    return child


def _spawn_sigterm_ignoring_child() -> subprocess.Popen[bytes]:
    return _spawn_ready_child("signal.signal(signal.SIGTERM, signal.SIG_IGN)")


def _spawn_sigterm_honouring_child() -> subprocess.Popen[bytes]:
    return _spawn_ready_child("pass")


def _reap(child: subprocess.Popen[bytes]) -> None:
    """Cleanup only — the smoke itself never sends signal 9 on an assertion path."""
    if not _child_exited(child.pid):
        child.kill()
    try:
        child.wait(timeout=5)
    except (subprocess.TimeoutExpired, ChildProcessError):
        pass


def _settled_alive(pid: int, settle_seconds: float = 0.3) -> bool:
    """Liveness read AFTER a settle window.

    Signal delivery is asynchronous: a child SIGKILLed a moment ago can still
    answer ``kill -0`` for a few milliseconds. Reading too early would let a
    real signal 9 pass as "still alive", so the alive assertion waits first —
    a child that is still there after the settle window was never killed.
    """
    time.sleep(settle_seconds)
    return not _child_exited(pid)


# ---------------------------------------------------------------------------
# Executor fixtures
# ---------------------------------------------------------------------------


class _LegRouter:
    """Recording router whose ``activate`` / ``rollback`` behaviour is per leg."""

    def __init__(self, *, leg: str) -> None:
        self._leg = leg
        self.registered: dict[str, int] = {}
        self.unregister_calls: list[str] = []
        self.rollback_calls: list[tuple[str, str]] = []

    def status(self) -> dict[str, Any]:
        colors = [{"instance_id": iid, "port": p} for iid, p in self.registered.items()]
        return {"active_color": COLOR_BLUE, "active_instance_id": _PRIOR_IID, "colors": colors}

    def register_color(self, port: int, color: str, instance_id: str) -> dict[str, Any]:
        del color
        self.registered[instance_id] = port
        return {"accepted": True}

    def activate(self, color: str, instance_id: str) -> dict[str, Any]:
        if self._leg == _LEG_ACTIVATE_RPC:
            raise RouterClientError("activate", "injected activate RPC failure")
        if self._leg == _LEG_ACTIVATE_REFUSED:
            return {"activated": False, "reason": "injected refusal"}
        del instance_id
        return {"activated": True, "previous_color": COLOR_BLUE, "drain_window_seconds": 30}

    def rollback(self, color: str, instance_id: str) -> dict[str, Any]:
        self.rollback_calls.append((color, instance_id))
        return {"rolled_back": True, "active_color": color}

    def unregister_color(self, instance_id: str) -> dict[str, Any]:
        self.unregister_calls.append(instance_id)
        return {"unregistered": True}


class _NeverEnqueue:
    def submit_action_definition(
        self, action_definition: dict[str, object], context: dict[str, object] | None = None,
    ) -> str:
        del action_definition, context
        msg = "complete_swap must never be enqueued on a failure leg"
        raise AssertionError(msg)


def _fake_candidate() -> CandidatePaths:
    base = Path("/nonexistent") / _CANDIDATE_REL
    return CandidatePaths(
        release_id=_CANDIDATE_REL, release_dir=base, code_root=base / "code",
        venv_python=base / "venv" / "bin" / "python3", version_file=base / "VERSION",
        missing_pth_targets=(), schema_snapshot=None,
    )


def _symlink_swap_for(leg: str) -> Callable[[CandidatePaths], SwapResult]:
    def _swap(candidate: CandidatePaths) -> SwapResult:
        del candidate
        if leg == _LEG_SYMLINK_ROLLBACK_OK:
            raise ReleaseManagerError("injected durable-swap failure after activate")
        return SwapResult(current=_CANDIDATE_REL, previous="rel-old")
    return _swap


def _drive_leg(
    *, leg: str, tmp: Path, spawn_child: Callable[[], subprocess.Popen[bytes]],
) -> dict[str, Any]:
    """Run ``SwapExecutor.execute`` once into ``leg``; return the observations."""
    router = _LegRouter(leg=leg)
    child = spawn_child()

    def fake_spawn(
        app_home: Path, next_color: str, next_instance_id: str,
        solet_name: str, candidate: CandidatePaths,
    ) -> int:
        del app_home, solet_name, candidate
        if leg != _LEG_READINESS:
            router.register_color(50055, next_color, next_instance_id)
        return child.pid

    def _always_reachable(*_a: object, **_k: object) -> bool:
        return True

    original_probe = green_candidate._probe_port_reachable  # noqa: SLF001
    green_candidate._probe_port_reachable = _always_reachable  # type: ignore[assignment]  # noqa: SLF001
    try:
        executor = SwapExecutor(
            router_client=cast("RouterClient", router),
            action_factory=_NeverEnqueue(),
            session_factory=lambda: "sess-nosigkill",
            solet_name=f"nosigkill-{abs(hash(leg)) % 10_000}",
            runtime_dir=tmp,
            set_color_active=cast("SetColorActiveFn", lambda _active: None),
            spawn_fn=fake_spawn,
            logger=_LOGGER,
            ready_timeout_seconds=_READY_TIMEOUT_SECONDS,
            ready_poll_interval_seconds=0.02,
            post_activate_grace_seconds=0.0,
            candidate_term_grace_seconds=_CANDIDATE_GRACE_SECONDS,
        )
        spawn_token = process_identity.start_token(child.pid)
        result = executor.execute(
            app_home=Path("/nonexistent/profile"),
            candidate=_fake_candidate(),
            next_color=COLOR_GREEN,
            reason="no-sigkill-smoke",
            expected_etag="etag-x",
            self_instance_id=_PRIOR_IID,
            self_color=COLOR_BLUE,
            prior_pid=1,
            prior_start_token=None,
            poller_gate="local_service_quiesced",
            set_active_targets=[],
            symlink_swap=_symlink_swap_for(leg),
            spawn_failure=(RestartStatus.FAILED, RestartReasonCode.SPAWN_FAILED),
            register_failure=(RestartStatus.FAILED, RestartReasonCode.REGISTER_TIMEOUT),
            compensation_codes=_COMPENSATION_CODES,
            finisher_escalation=FINISHER_ESCALATION_NEEDS_INTERVENTION,
        )
    finally:
        green_candidate._probe_port_reachable = original_probe  # noqa: SLF001
    child_alive = _settled_alive(child.pid)
    observations = {
        "pid": child.pid,
        "spawn_token": spawn_token,
        "status": result.status,
        "reason_code": result.reason_code,
        "message": result.message,
        "unregister_calls": router.unregister_calls,
        "rollback_calls": router.rollback_calls,
        "child_alive": child_alive,
    }
    _reap(child)
    return observations


def _expected_needs_intervention_code(leg: str) -> str:
    # Legs 1–3 surface the new code directly; the compensation leg maps a
    # still-alive candidate through the existing ``unconfirmed_code`` slot
    # (audit §2.3 Fix A), so its code is the caller's router-rollback code
    # while the message says the router WAS rolled back.
    if leg == _LEG_SYMLINK_ROLLBACK_OK:
        return RestartReasonCode.CUTOVER_ROUTER_ROLLBACK_FAILED
    return RestartReasonCode.CANDIDATE_TERMINATE_TIMEOUT


def _expected_failed_code(leg: str) -> str:
    if leg == _LEG_READINESS:
        return RestartReasonCode.REGISTER_TIMEOUT
    if leg == _LEG_SYMLINK_ROLLBACK_OK:
        return RestartReasonCode.CUTOVER_COMPENSATED
    return RestartReasonCode.ACTIVATE_REFUSED


def test_executor_legs_never_sigkill(tmp: Path) -> None:
    print("scenario 1: four executor failure legs vs a child that can only die from signal 9")
    for leg in _LEGS:
        obs = _drive_leg(leg=leg, tmp=tmp, spawn_child=_spawn_sigterm_ignoring_child)
        message = cast("str", obs["message"])
        _check(obs["child_alive"], f"[{leg}] SIG_IGN candidate is STILL ALIVE (no signal 9 was sent)")
        _check(
            obs["status"] is RestartStatus.NEEDS_INTERVENTION
            and obs["reason_code"] == _expected_needs_intervention_code(leg),
            f"[{leg}] NEEDS_INTERVENTION / {_expected_needs_intervention_code(leg)}",
        )
        _check(
            f"pid={obs['pid']}" in message and repr(obs["spawn_token"]) in message,
            f"[{leg}] message names the pid and the spawn-time start token",
        )
        unreg = cast("list[str]", obs["unregister_calls"])
        _check(
            len(unreg) == 1 and unreg[0].startswith("solet-green-"),
            f"[{leg}] candidate unregistered (D-1.7)",
        )
        if leg == _LEG_SYMLINK_ROLLBACK_OK:
            _check(
                obs["rollback_calls"] == [(COLOR_BLUE, _PRIOR_IID)] and "WAS rolled back" in message,
                f"[{leg}] router rollback confirmed and the message says so (not 'did NOT take')",
            )


def test_executor_legs_control(tmp: Path) -> None:
    print("scenario 2: control — a SIGTERM-honouring candidate exits and the legs stay retryable")
    for leg in _LEGS:
        obs = _drive_leg(leg=leg, tmp=tmp, spawn_child=_spawn_sigterm_honouring_child)
        _check(not obs["child_alive"], f"[{leg}] control candidate exited on SIGTERM")
        _check(
            obs["status"] is RestartStatus.FAILED
            and obs["reason_code"] == _expected_failed_code(leg),
            f"[{leg}] control result is FAILED / {_expected_failed_code(leg)} (retryable)",
        )
        unreg = cast("list[str]", obs["unregister_calls"])
        _check(len(unreg) == 1, f"[{leg}] control candidate unregistered (D-1.7)")


# ---------------------------------------------------------------------------
# K2 — complete_swap finisher escalation is provenance-aware
# ---------------------------------------------------------------------------


def _record(pid: int, token: str | None, escalation: str) -> PendingFinisher:
    return PendingFinisher(
        prior_pid=pid, prior_instance_id=_PRIOR_IID, prior_color=COLOR_BLUE,
        candidate_release_id=_CANDIDATE_REL, prior_start_token=token,
        escalation=escalation,
    )


def _run_complete_swap(
    *, spawn_child: Callable[[], subprocess.Popen[bytes]], escalation: str,
) -> dict[str, Any]:
    solet = "nosigkillfinisher"
    path = pending_finisher_path(_runtime_dir(), solet)
    plugin = MacosSelfDeploymentPlugin()
    plugin._solet_name = solet  # noqa: SLF001
    plugin._router_client = None  # noqa: SLF001 — unregister becomes a skip; the signal is the focus
    child = spawn_child()
    original_grace = plugin_module.DEFAULT_PRIOR_TERM_GRACE_SECONDS
    plugin_module.DEFAULT_PRIOR_TERM_GRACE_SECONDS = _FINISHER_GRACE_SECONDS  # type: ignore[misc]
    try:
        token = process_identity.start_token(child.pid)
        write_pending_finisher(path, _record(child.pid, token, escalation))
        data = plugin.complete_swap(
            prior_pid=child.pid, prior_instance_id=_PRIOR_IID, prior_color=COLOR_BLUE,
        )
        child_alive = _settled_alive(child.pid)
        record_kept = read_pending_finisher(path) is not None
    finally:
        plugin_module.DEFAULT_PRIOR_TERM_GRACE_SECONDS = original_grace  # type: ignore[misc]
        _reap(child)
        clear_pending_finisher(path)
    return {
        "steps": cast("list[str]", data["steps_completed"]),
        "child_alive": child_alive,
        "record_kept": record_kept,
    }


def test_finisher_escalation() -> None:
    print("scenario 3: K2 — complete_swap on a needs_intervention record never SIGKILLs the prior")
    obs = _run_complete_swap(
        spawn_child=_spawn_sigterm_ignoring_child,
        escalation=FINISHER_ESCALATION_NEEDS_INTERVENTION,
    )
    _check(obs["child_alive"], "SIG_IGN prior is STILL ALIVE after complete_swap (no signal 9)")
    _check(
        "prior_sigterm_timeout_needs_intervention" in obs["steps"],
        "complete_swap reports prior_sigterm_timeout_needs_intervention",
    )
    _check(obs["record_kept"], "durable pending-finisher record KEPT (symmetry with sigterm_denied)")
    _check(
        "pending_finisher_kept_sigterm_timeout_needs_intervention" in obs["steps"],
        "steps name the kept record",
    )

    obs = _run_complete_swap(
        spawn_child=_spawn_sigterm_honouring_child,
        escalation=FINISHER_ESCALATION_NEEDS_INTERVENTION,
    )
    _check(not obs["child_alive"], "control: SIGTERM-honouring prior exits under needs_intervention")
    _check(
        "prior_terminated_cleanly" in obs["steps"] and not obs["record_kept"],
        "control: prior_terminated_cleanly and the record is cleared",
    )

    obs = _run_complete_swap(
        spawn_child=_spawn_sigterm_ignoring_child,
        escalation=FINISHER_ESCALATION_SIGKILL_AFTER_GRACE,
    )
    _check(
        "prior_sigkilled" in obs["steps"] and not obs["child_alive"] and not obs["record_kept"],
        "control: an ordinary service-driven record (sigkill_after_grace) keeps today's ladder",
    )


# ---------------------------------------------------------------------------
# Backstop — TERMINATED_ORPHAN requires a VERIFIED exit
# ---------------------------------------------------------------------------


class _BackstopRouter:
    def __init__(self) -> None:
        self.unregistered: list[str] = []

    def status(self) -> dict[str, Any]:
        return {"active_instance_id": _SELF_IID, "colors": [], "drain_entries": []}

    def unregister_color(self, instance_id: str) -> dict[str, Any]:
        self.unregistered.append(instance_id)
        return {"unregistered": True}


class _CapturingHandler(logging.Handler):
    def __init__(self) -> None:
        super().__init__()
        self.messages: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.messages.append(record.getMessage())


def _run_backstop(
    *, tmp: Path, spawn_child: Callable[[], subprocess.Popen[bytes]],
) -> dict[str, Any]:
    path = pending_finisher_path(tmp, "nosigkillbackstop")
    router = _BackstopRouter()
    logger = logging.getLogger(f"no_sigkill_backstop_{id(router)}")
    logger.setLevel(logging.DEBUG)
    logger.propagate = False
    handler = _CapturingHandler()
    logger.addHandler(handler)
    child = spawn_child()
    original_verify = heartbeat_lifecycle.DEFAULT_BACKSTOP_TERM_VERIFY_SECONDS
    heartbeat_lifecycle.DEFAULT_BACKSTOP_TERM_VERIFY_SECONDS = _BACKSTOP_VERIFY_SECONDS  # type: ignore[misc]
    try:
        token = process_identity.start_token(child.pid)
        write_pending_finisher(
            path, _record(child.pid, token, FINISHER_ESCALATION_NEEDS_INTERVENTION),
        )
        _run_pending_finisher_backstop(
            client=cast("RouterClient", router),
            self_instance_id=_SELF_IID,
            pending_finisher_file=path,
            current_release_lookup=lambda: _CANDIDATE_REL,
            logger=logger,
        )
        child_alive = _settled_alive(child.pid)
        record_kept = read_pending_finisher(path) is not None
    finally:
        heartbeat_lifecycle.DEFAULT_BACKSTOP_TERM_VERIFY_SECONDS = original_verify  # type: ignore[misc]
        _reap(child)
        clear_pending_finisher(path)
    return {
        "child_alive": child_alive,
        "record_kept": record_kept,
        "unregistered": router.unregistered,
        "messages": handler.messages,
    }


def test_backstop_verifies_exit(tmp: Path) -> None:
    print("scenario 4: heartbeat backstop verifies exit before clearing; never SIGKILL")
    obs = _run_backstop(tmp=tmp, spawn_child=_spawn_sigterm_ignoring_child)
    _check(obs["child_alive"], "SIG_IGN prior is STILL ALIVE after the backstop tick")
    _check(obs["record_kept"], "record KEPT — TERMINATED_ORPHAN did not clear an un-exited prior")
    _check(obs["unregistered"] == [], "prior NOT unregistered while it is still alive")
    _check(
        any("TERMINATE_TIMEOUT" in m for m in obs["messages"])
        and not any(RECONCILE_TERMINATED_ORPHAN in m for m in obs["messages"]),
        f"backstop logged TERMINATE_TIMEOUT ({RECONCILE_TERMINATE_FAILED}), not terminated_orphan",
    )

    obs = _run_backstop(tmp=tmp, spawn_child=_spawn_sigterm_honouring_child)
    _check(not obs["child_alive"], "control: SIGTERM-honouring prior exited")
    _check(
        not obs["record_kept"] and obs["unregistered"] == [_PRIOR_IID],
        "control: verified exit → record cleared + prior unregistered",
    )
    _check(
        any(RECONCILE_TERMINATED_ORPHAN in m for m in obs["messages"]),
        "control: backstop logged terminated_orphan",
    )


def main() -> int:
    print("=== no_sigkill_channel_smoke (iss_8d1ec833 / iss_351345f7) ===")
    ananta_scratch = Path.home() / ".ananta"
    ananta_scratch.mkdir(exist_ok=True)
    tmp = Path(tempfile.mkdtemp(prefix="nosigkill_smoke_", dir=str(ananta_scratch)))
    try:
        test_executor_legs_never_sigkill(tmp)
        test_executor_legs_control(tmp)
        test_finisher_escalation()
        test_backstop_verifies_exit(tmp)
    finally:
        for entry in tmp.iterdir():
            entry.unlink()
        tmp.rmdir()
    print(f"\nno_sigkill_channel_smoke: {_passed} passed, {len(_failed)} failed")
    for label in _failed:
        print(f"  FAILED: {label}")
    return 1 if _failed else 0


if __name__ == "__main__":
    sys.exit(main())
