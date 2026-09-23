"""Process-identity token — the PID-reuse guard for the B2 post-cutover finisher.

The pending-finisher record (``pending_finisher.py``) names the prior colour by
``(pid, instance_id)``. But a bare pid is not a stable identity: the prior
process can exit and the OS recycle its pid for an unrelated process before the
router GCs the stale binding. The router binds ``instance_id → colour/port`` but
NOT ``→ pid``, so "router still knows the instance AND ``kill(pid, 0)`` succeeds"
does NOT prove the pid is still the prior process — it could be a reused pid.
SIGTERMing it would kill an innocent process (Codex round-2 B2·3).

The fix is a **process-identity token** = the process's start time, captured the
instant the record is written (about our own pid) and re-checked before any
SIGTERM. A reused pid has a later start time, so a token mismatch unmasks the
reuse and the finisher unregisters the stale binding WITHOUT signalling.

``psutil`` is not a platform dependency, so the token is the macOS-native
``ps -o lstart=`` absolute start timestamp (stable for the life of the process).
The probe runs only on the rare finisher path (a prior colour still registered
after a cutover), never per heartbeat tick.
"""

from __future__ import annotations

import os
import subprocess
from typing import Final

_PS_TIMEOUT_SECONDS: Final[float] = 5.0


def start_token(pid: int) -> str | None:
    """Return ``pid``'s start-time identity token, or ``None`` if no such process.

    ``None`` means "no live process with this pid" (the prior is gone) OR the
    probe could not run — both are treated as "do not SIGTERM" by the callers, so
    uncertainty fails safe (never signals on an unverifiable pid).
    """
    if pid <= 0:
        return None
    try:
        result = subprocess.run(
            ["ps", "-o", "lstart=", "-p", str(pid)],
            capture_output=True,
            text=True,
            timeout=_PS_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    token = result.stdout.strip()
    if result.returncode != 0 or not token:
        return None
    return token


def process_exited(pid: int) -> bool:
    """Whether ``pid`` has exited — zombies included.

    ``os.kill(pid, 0)`` on its own quietly gets this wrong. A child that has
    died but not yet been reaped is a ZOMBIE: it still occupies the process
    table, so the signal probe succeeds and the corpse reads as perfectly
    healthy. That is precisely the state the 2026-08-31 spawn hang left behind
    and a liveness check that cannot see it would be a check in name only.
    ``waitpid`` with ``WNOHANG`` is what tells a zombie from a live process, and
    it reaps the corpse as a side effect.

    The order of the two probes is load-bearing:

    * The signal probe runs FIRST so a process already reaped by someone else
      reads as gone. ``subprocess`` opportunistically reaps garbage-collected
      ``Popen`` handles whenever any other subprocess is created anywhere in
      this process, and the swap path deliberately discards its handle — so
      "reaped by another thread" is a real, reachable state, not a theoretical one.
    * ``ChildProcessError`` from ``waitpid`` while the pid still exists is left
      deliberately as ALIVE. That pid is not ours to judge (the finisher's prior
      is our PARENT, not our child — launchd reaps it, and the signal probe
      above is what observes that), and a false "died" verdict would abort a
      perfectly healthy swap — the strictly worse error.

    Shared by the swap candidate teardown (``green_candidate``) and the
    heartbeat backstop's post-SIGTERM verification (``heartbeat_lifecycle``)
    so the two cannot drift on what "exited" means.
    """
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    except PermissionError:
        return False
    try:
        reaped, _status = os.waitpid(pid, os.WNOHANG)
    except ChildProcessError:
        return False
    return reaped == pid


__all__ = ["process_exited", "start_token"]
