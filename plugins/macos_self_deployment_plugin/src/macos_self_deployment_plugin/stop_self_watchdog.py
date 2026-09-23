"""Detached SIGTERM watchdog for ``stop_self``.

Slice 4.5 of
``workbench/2026-06-05_bridge_port_routing_and_session_lifecycle_design.md``
introduces the ``stop_self`` verb on the
:class:`SelfDeploymentServiceInterface` base. The verb runs INSIDE the
the solet process it's being asked to kill; a synchronous SIGTERM would
prevent the verb's response from flushing back to the caller (the
network write races the signal handler). The fix is a detached
subprocess: ``stop_self`` spawns a tiny Python subprocess that sleeps
briefly, sends SIGTERM, waits a bounded window, escalates to SIGKILL
if the target is still alive — all from outside the solet process, so
the verb's response has time to fully serialize and ship before any
signal arrives.

``start_new_session=True`` puts the watchdog in its own POSIX session,
so the SIGTERM it later sends to the solet doesn't cascade back to the
watchdog itself.

Lives as a sibling module to :mod:`drain_sentinel` so it's easy to
test (the plugin injects an alternate spawner via
``set_watchdog_spawner_for_smoke``).
"""

from __future__ import annotations

import logging
import subprocess
import sys
from collections.abc import Callable

from macos_self_deployment_plugin.constants import (
    DEFAULT_STOP_SELF_PRE_SIGTERM_DELAY_SECONDS,
    DEFAULT_STOP_SELF_SIGKILL_ESCALATION_SECONDS,
    PLUGIN_NAME,
)

# A spawner takes (target_pid) and returns the spawned watchdog's pid
# (used purely for audit; the watchdog dies on its own after escalation).
WatchdogSpawner = Callable[[int], int]

_logger = logging.getLogger(PLUGIN_NAME)


def spawn(target_pid: int) -> int:
    """Production watchdog spawner: detach a subprocess that SIGTERMs ``target_pid``.

    The subprocess script is constructed inline so there's no separate
    file to keep in sync. ``start_new_session=True`` is the load-bearing
    detachment (POSIX session leader new); without it, the SIGTERM the
    watchdog later sends could cascade back if our process tree got
    re-parented.
    """
    script = (
        "import os, signal, time\n"
        f"time.sleep({DEFAULT_STOP_SELF_PRE_SIGTERM_DELAY_SECONDS})\n"
        "try:\n"
        f"    os.kill({target_pid}, signal.SIGTERM)\n"
        "except ProcessLookupError:\n"
        "    pass\n"
        f"time.sleep({DEFAULT_STOP_SELF_SIGKILL_ESCALATION_SECONDS})\n"
        "try:\n"
        f"    os.kill({target_pid}, signal.SIGKILL)\n"
        "except ProcessLookupError:\n"
        "    pass\n"
    )
    proc = subprocess.Popen(  # noqa: S603 — interpreter + inline script, no shell
        [sys.executable, "-c", script],
        start_new_session=True,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    _logger.info(
        "%s: stop_self watchdog spawned pid=%d targeting the solet pid=%d "
        "(SIGTERM in %.1fs, SIGKILL escalation in %.1fs)",
        PLUGIN_NAME,
        proc.pid,
        target_pid,
        DEFAULT_STOP_SELF_PRE_SIGTERM_DELAY_SECONDS,
        DEFAULT_STOP_SELF_PRE_SIGTERM_DELAY_SECONDS
        + DEFAULT_STOP_SELF_SIGKILL_ESCALATION_SECONDS,
    )
    return proc.pid
