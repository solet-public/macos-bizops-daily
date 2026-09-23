#!/usr/bin/env python3
"""Smoke: a swap candidate that DIES fails the swap at once, not at the timeout.

Before this, ``GreenCandidate.wait_until_registered`` polled only the router
registry and a TCP port. A candidate that exited during startup was therefore
indistinguishable from one that was merely slow, and the swap sat out its whole
``ready_timeout`` — 600 s in production — holding the platform's action queue the
entire time. That is what both 2026-08-31 cutovers did.

The subsmokes and the mutation each one catches:

* ``SC-1 zombie-is-not-alive`` — the reason this needs more than ``os.kill(pid, 0)``.
  Stages a REAL unreaped zombie and asserts the signal probe still reports it
  alive while ``_child_exited`` correctly reports it dead. Replacing the
  ``waitpid`` probe with a bare signal probe fails here and only here.
* ``SC-2 dead-candidate-returns-EXITED-fast`` — deleting the liveness check from
  the poll loop, or checking it only once before the loop. Asserts on elapsed
  time against a deliberately long timeout, so "correct verdict, eventually" fails.
* ``SC-3 live-candidate-still-times-out`` — the discriminator for the fix itself.
  A live candidate that never registers must STILL return ``TIMED_OUT``; a
  liveness check that reports everything dead would pass SC-2 while breaking
  every real swap.
* ``SC-4 registration-still-wins`` — a candidate that does register returns
  ``REGISTERED`` only after its bridge health endpoint answers, proving the
  serve check does not shadow the success path.
* ``SC-5 tcp-open-without-response-times-out`` — a registered candidate whose
  listener accepts the TCP connection but never writes an HTTP response must
  return ``TIMED_OUT``. Replacing the serve probe with a TCP connect makes this
  case falsely return ``REGISTERED``.

Hermetic: a stub router, and child processes that are this interpreter exiting
immediately or sleeping. No solet, no deploy, no router, no network.
"""

from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path
from typing import Any

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

import logging  # noqa: E402
import os  # noqa: E402

from macos_self_deployment_plugin.green_candidate import (  # noqa: E402
    CandidateReadiness,
    GreenCandidate,
    _child_exited,
)

_INSTANCE = "solet-green-deadbeef"
_LOGGER = logging.getLogger("candidate_liveness_smoke")


class _StubRouter:
    """Router that either never lists the candidate, or lists it as reachable."""

    def __init__(self, *, registers: bool, port: int = 0) -> None:
        self._registers = registers
        self._port = port

    def status(self) -> dict[str, Any]:
        if not self._registers:
            return {"colors": []}
        return {"colors": [{"instance_id": _INSTANCE, "port": self._port}]}


def _candidate(router: _StubRouter, *, timeout: int) -> GreenCandidate:
    return GreenCandidate(
        router_client=router,  # type: ignore[arg-type]
        logger=_LOGGER,
        ready_timeout_seconds=timeout,
        ready_poll_interval_seconds=0.05,
    )


def _stage_zombie() -> subprocess.Popen[bytes]:
    """Spawn a child, let it exit, and deliberately do NOT reap it.

    Returns the live ``Popen`` handle, and the caller MUST keep a reference to
    it for as long as the zombie is needed: ``Popen.__del__`` calls
    ``_internal_poll`` and reaps the corpse, so letting the handle fall out of
    scope destroys the very state under test. (This is the same opportunistic
    reaping that ``_child_exited`` is written to tolerate.)
    """
    proc = subprocess.Popen([sys.executable, "-c", "raise SystemExit(0)"])  # noqa: S603
    deadline = time.monotonic() + 15.0
    while time.monotonic() < deadline:
        state = subprocess.run(  # noqa: S603, S607
            ["ps", "-o", "state=", "-p", str(proc.pid)],
            capture_output=True, text=True, check=False,
        ).stdout.strip()
        if state.startswith("Z"):
            return proc
        time.sleep(0.05)
    proc.kill()
    raise AssertionError("could not stage an unreaped zombie child")


def _start_bridge_server(*, responds: bool) -> tuple[subprocess.Popen[str], int]:
    """Start one bounded-response or deliberately silent bridge stand-in."""
    response = (
        "c.sendall(b'HTTP/1.1 200 OK\\r\\nContent-Length: 0\\r\\n"
        "Connection: close\\r\\n\\r\\n')"
        if responds
        else "time.sleep(30)"
    )
    server = subprocess.Popen(  # noqa: S603
        [
            sys.executable,
            "-c",
            (
                "import socket,time;s=socket.socket();"
                "s.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1);"
                "s.bind(('127.0.0.1',0));s.listen(8);"
                "print(s.getsockname()[1],flush=True);c,_=s.accept();"
                "c.recv(4096);"
                f"{response};c.close();time.sleep(30)"
            ),
        ],
        stdout=subprocess.PIPE,
        text=True,
    )
    assert server.stdout is not None
    return server, int(server.stdout.readline().strip())


def _sc1_zombie_is_not_alive() -> None:
    proc = _stage_zombie()
    # The naive probe: a zombie still occupies the process table, so this
    # SUCCEEDS. If it were to raise, the corpse had already been reaped and this
    # subsmoke would be proving nothing.
    os.kill(proc.pid, 0)
    assert _child_exited(proc.pid), (
        "a zombie child read as ALIVE — os.kill(pid, 0) alone cannot see an "
        "unreaped corpse, which is exactly the state the hang left behind"
    )
    proc.poll()


def _sc2_dead_candidate_returns_exited_fast() -> None:
    proc = _stage_zombie()
    candidate = _candidate(_StubRouter(registers=False), timeout=60)
    started = time.monotonic()
    outcome = candidate.wait_until_registered(_INSTANCE, pid=proc.pid)
    elapsed = time.monotonic() - started
    proc.poll()
    assert outcome is CandidateReadiness.EXITED, f"expected EXITED, got {outcome}"
    assert elapsed < 5.0, (
        f"a dead candidate cost {elapsed:.1f}s of a 60s timeout — the swap would "
        "still hold the action queue for the full wait"
    )


def _sc3_live_candidate_still_times_out() -> None:
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])  # noqa: S603
    try:
        candidate = _candidate(_StubRouter(registers=False), timeout=2)
        started = time.monotonic()
        outcome = candidate.wait_until_registered(_INSTANCE, pid=proc.pid)
        elapsed = time.monotonic() - started
        assert outcome is CandidateReadiness.TIMED_OUT, (
            f"a LIVE candidate that never registered returned {outcome}; a "
            "liveness check that calls everything dead breaks every real swap"
        )
        assert elapsed >= 2.0, f"returned after {elapsed:.1f}s, before the 2s timeout"
    finally:
        proc.kill()
        proc.wait()


def _sc4_registration_still_wins() -> None:
    server, port = _start_bridge_server(responds=True)
    try:
        candidate = _candidate(_StubRouter(registers=True, port=port), timeout=5)
        outcome = candidate.wait_until_registered(_INSTANCE, pid=server.pid)
        assert outcome is CandidateReadiness.REGISTERED, (
            f"a healthy registered candidate returned {outcome} — the liveness "
            "check shadowed the success path"
        )
    finally:
        server.kill()
        server.wait()


def _sc5_tcp_open_without_response_times_out() -> None:
    server, port = _start_bridge_server(responds=False)
    try:
        candidate = _candidate(_StubRouter(registers=True, port=port), timeout=1)
        outcome = candidate.wait_until_registered(_INSTANCE, pid=server.pid)
        assert outcome is CandidateReadiness.TIMED_OUT, (
            "a TCP listener that never answered bridge health read as ready; "
            "TCP connect proves BIND, not SERVE"
        )
    finally:
        server.kill()
        server.wait()


def _check(name: str, fn: Any, failures: list[str]) -> None:
    try:
        fn()
    except Exception as exc:  # noqa: BLE001 — the smoke reports, never propagates
        print(f"  FAIL {name}: {exc}")
        failures.append(name)
    else:
        print(f"  ok   {name}")


def main() -> int:
    failures: list[str] = []
    print("swap candidate liveness:")
    _check("SC-1 zombie-is-not-alive", _sc1_zombie_is_not_alive, failures)
    _check(
        "SC-2 dead-candidate-returns-EXITED-fast",
        _sc2_dead_candidate_returns_exited_fast,
        failures,
    )
    _check(
        "SC-3 live-candidate-still-times-out",
        _sc3_live_candidate_still_times_out,
        failures,
    )
    _check("SC-4 registration-still-wins", _sc4_registration_still_wins, failures)
    _check(
        "SC-5 tcp-open-without-response-times-out",
        _sc5_tcp_open_without_response_times_out,
        failures,
    )

    if failures:
        print(f"\nFAIL — {len(failures)} case(s): {', '.join(failures)}")
        return 1
    print("\nPASS — a dead candidate fails the swap at once; a live one still waits.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
