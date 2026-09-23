#!/usr/bin/env python3
"""Drain SIGTERM → exit-0 respawn-suppression smoke (blue-green ghost fix).

Verifies the contract that the *missing* SIGTERM handler silently broke (every
cutover SIGTERM-killed the launchd-managed drained color → ``Crashed=true``
ghost respawn):

* **Path match (the critical, silent-failure axis).** The WRITE side (plugin
  ``drain_sentinel.write`` / ``held`` / ``sentinel_path``) and the READ side
  (core ``ananta.core.runtime.is_draining`` / ``draining_sentinel_path``)
  resolve the SAME file. A drift would make the SIGTERM handler read a
  non-existent sentinel → always exit non-zero → ghost respawn returns.
* **Real exit-code logic.** ``EventOrchestrator.sigterm_exit_code`` returns 0
  for a normal completion or an intentional drain (sentinel present → launchd
  must NOT respawn) and non-zero only for a stray SIGTERM of a live color
  (sentinel absent → launchd respawns = correct supervision).
* **Real-SIGTERM end-to-end via subprocess.** A process using the REAL core
  seam (``loop.add_signal_handler(SIGTERM)`` + ``is_draining``) exits 0 when the
  drain sentinel is present and non-zero when absent — and the drain exit lands
  WELL INSIDE the swap finisher's SIGKILL grace
  (``DEFAULT_PRIOR_TERM_GRACE_SECONDS``); a SIGKILL'd process cannot exit 0.

Standalone — not pytest. Run with::

    SOLET_NAME=<name> .venv/bin/python3 \
        plugins/macos_self_deployment_plugin/tests/drain_sigterm_exit_smoke.py
"""

from __future__ import annotations

# ruff: noqa: E402
import os
import signal
import subprocess
import sys
import time
import traceback
from collections.abc import Callable

# Use the caller-supplied solet name before importing modules that resolve
# SOLET_NAME. Every sentinel created by this smoke is removed on the way out.
NAME = os.environ["SOLET_NAME"]

from pathlib import Path

_PLUGIN_ROOT = Path(__file__).resolve().parents[1]
_SRC = _PLUGIN_ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from ananta.core.runtime import (  # noqa: E402
    draining_sentinel_path,
    is_draining,
)
from macos_self_deployment_plugin import drain_sentinel  # noqa: E402
from macos_self_deployment_plugin.constants import (  # noqa: E402
    DEFAULT_PRIOR_TERM_GRACE_SECONDS,
)

# Subprocess that exercises the REAL core seam: install the SIGTERM handler on
# the main-thread asyncio loop, read ``is_draining`` AT RECEIPT, and exit with
# the disposition ``EventOrchestrator.sigterm_exit_code`` computes (mirrored in
# the final line — the 2-line policy is also asserted directly against the real
# property in ``_check_real_exit_code_property``).
_SUBPROC = (
    "import asyncio, signal, sys\n"
    "from ananta.core.runtime import is_draining\n"
    "async def _main():\n"
    "    loop = asyncio.get_running_loop()\n"
    "    done = asyncio.Event()\n"
    "    seen = {'sigterm': False, 'draining': False}\n"
    "    def _on_sigterm():\n"
    "        seen['sigterm'] = True\n"
    "        seen['draining'] = is_draining()\n"
    "        done.set()\n"
    "    loop.add_signal_handler(signal.SIGTERM, _on_sigterm)\n"
    "    print('READY', flush=True)\n"
    "    await done.wait()\n"
    "    sys.exit(1 if (seen['sigterm'] and not seen['draining']) else 0)\n"
    "asyncio.run(_main())\n"
)


def _check(name: str, fn: Callable[[], None], failures: list[str]) -> None:
    try:
        fn()
    except Exception:  # noqa: BLE001 — smoke runner: report every failure, keep going
        failures.append(name)
        print(f"  ✗ {name}")
        traceback.print_exc()
    else:
        print(f"  ✓ {name}")


def _check_path_match() -> None:
    """Plugin write side and core read side resolve the IDENTICAL file."""
    plugin_path = drain_sentinel.sentinel_path(NAME)
    core_path = draining_sentinel_path(NAME)
    assert plugin_path == core_path, (plugin_path, core_path)
    # Write via the plugin → core reads it as draining → remove → not draining.
    written = drain_sentinel.write(NAME)
    try:
        assert written == core_path, (written, core_path)
        assert is_draining(NAME) is True, "core is_draining did not see plugin write"
        assert is_draining() is True, "is_draining() via env name did not see write"
    finally:
        written.unlink(missing_ok=True)
    assert is_draining(NAME) is False, "is_draining still True after unlink"


def _check_held_context() -> None:
    """``held()`` makes the color appear draining only inside the with-block."""
    assert is_draining(NAME) is False
    with drain_sentinel.held(NAME):
        assert is_draining(NAME) is True, "held() did not present a draining sentinel"
    assert is_draining(NAME) is False, "held() left a stale sentinel"


def _check_real_exit_code_property() -> None:
    """The REAL ``EventOrchestrator.sigterm_exit_code`` for the three cases.

    ``__new__`` bypasses the heavy ``__init__`` — we only need the two bool
    attrs the handler sets and the property that reads them.
    """
    from ananta.core.event_orchestrator import EventOrchestrator

    obj = EventOrchestrator.__new__(EventOrchestrator)
    obj._sigterm_received = False
    obj._draining_at_sigterm = False
    assert obj.sigterm_exit_code == 0, "no SIGTERM must exit 0"
    obj._sigterm_received = True
    obj._draining_at_sigterm = True
    assert obj.sigterm_exit_code == 0, "drain SIGTERM must exit 0 (no respawn)"
    obj._draining_at_sigterm = False
    assert obj.sigterm_exit_code == 1, "stray SIGTERM must exit non-zero (respawn)"


def _run_sigterm_subprocess(*, draining: bool) -> tuple[int, float]:
    """Spawn the real-seam subprocess, SIGTERM it, return (exit_code, seconds)."""
    if draining:
        drain_sentinel.write(NAME)
    else:
        draining_sentinel_path(NAME).unlink(missing_ok=True)
    proc = subprocess.Popen(
        [sys.executable, "-c", _SUBPROC],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        env={**os.environ, "SOLET_NAME": NAME},
    )
    try:
        # Wait for the handler to be installed (READY) before signalling.
        assert proc.stdout is not None
        deadline = time.monotonic() + 15.0
        while time.monotonic() < deadline:
            line = proc.stdout.readline()
            if line.strip() == "READY":
                break
        else:
            raise AssertionError("subprocess never printed READY")
        start = time.monotonic()
        proc.send_signal(signal.SIGTERM)
        # Bound by the SIGKILL grace: a drained color MUST exit well inside it.
        code = proc.wait(timeout=DEFAULT_PRIOR_TERM_GRACE_SECONDS)
        return code, time.monotonic() - start
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=5)
        draining_sentinel_path(NAME).unlink(missing_ok=True)


def _check_real_sigterm_draining_exits_zero() -> None:
    code, secs = _run_sigterm_subprocess(draining=True)
    assert code == 0, f"drain SIGTERM exited {code}, expected 0 (would ghost-respawn)"
    assert secs < DEFAULT_PRIOR_TERM_GRACE_SECONDS, (
        f"drain exit took {secs:.2f}s — must be well inside the "
        f"{DEFAULT_PRIOR_TERM_GRACE_SECONDS}s SIGKILL grace"
    )
    print(f"    (drain SIGTERM exited 0 in {secs:.3f}s)")


def _check_real_sigterm_stray_exits_nonzero() -> None:
    code, secs = _run_sigterm_subprocess(draining=False)
    assert code != 0, f"stray SIGTERM exited 0, expected non-zero (must respawn) [{secs:.3f}s]"
    print(f"    (stray SIGTERM exited {code} in {secs:.3f}s)")


def main() -> int:
    failures: list[str] = []
    # Start from a clean slate.
    draining_sentinel_path(NAME).unlink(missing_ok=True)

    print("Drain SIGTERM → exit-0 respawn-suppression smoke:")
    _check("path-match (plugin write ↔ core read)", _check_path_match, failures)
    _check("held() context window", _check_held_context, failures)
    _check("real sigterm_exit_code property", _check_real_exit_code_property, failures)
    _check("real-SIGTERM drain → exit 0 within grace", _check_real_sigterm_draining_exits_zero, failures)
    _check("real-SIGTERM stray → exit non-zero", _check_real_sigterm_stray_exits_nonzero, failures)

    if failures:
        print(f"\nFAIL — {len(failures)} case(s): {', '.join(failures)}")
        return 1
    print("\nPASS — drain SIGTERM exits 0 (no ghost respawn), stray SIGTERM respawns.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
