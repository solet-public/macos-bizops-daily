"""The colour census (iss_75b87670, Dax Part 57 section 57.1): which processes serve this target.

A target's colours are the pids whose command ends with ``-m ananta.cli --app-home <target>/profile``.  Every colour runs that
argv, whichever interpreter started it: the launchd job runs the target's own venv, and a blue-green swap spawns the next colour
detached from a release venv (``child_spawn``).  The match keys on the ``--app-home`` suffix, so another solet's colours, which
name another app home, are never this target's.

The census is read-only.  The update planner, the post-restart verification and the doctor all read it from the one process-table
seam (``run_ps``), so the three cannot disagree about who is serving.
"""

from __future__ import annotations

import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import cast

from .models import JsonValue

__all__ = [
    "COLOUR_OUTSIDE_LAUNCHAGENT",
    "ProcessTableReader",
    "colour_command_suffix",
    "colour_outside_guidance",
    "colour_pids",
    "parse_process_rows",
    "read_colour_census",
    "read_process_rows",
    "recovery_command",
]

#: The stable Step 7 reason: a healthy solet whose serving colour is not the process launchd owns.
COLOUR_OUTSIDE_LAUNCHAGENT = "colour_outside_launchagent"

type ProcessRow = tuple[int, str, str]
type ProcessTableReader = Callable[[int], subprocess.CompletedProcess[str]]

_TIMEOUT_SECONDS = 30


def parse_process_rows(stdout: str) -> list[ProcessRow]:
    """``pid lstart(5 fields) command`` rows of the closed ``ps -axo pid=,lstart=,command=`` vector; other lines are skipped."""
    rows: list[ProcessRow] = []
    for line in stdout.splitlines():
        parts = line.strip().split(None, 6)
        if len(parts) < 7 or not parts[0].isdigit():
            continue
        rows.append((int(parts[0]), " ".join(parts[1:6]), parts[6]))
    return rows


def read_process_rows(run_ps: ProcessTableReader) -> list[ProcessRow] | None:
    """The process table, or ``None`` when it cannot be read: a caller must not treat that as an empty table."""
    try:
        completed = run_ps(_TIMEOUT_SECONDS)
    except (OSError, subprocess.SubprocessError):
        return None
    if completed.returncode != 0:
        return None
    return parse_process_rows(completed.stdout)


def colour_command_suffix(app_home: Path) -> str:
    """The argv tail every colour of the target shares."""
    return f" -m ananta.cli --app-home {app_home}"


def colour_pids(rows: list[ProcessRow], target: Path) -> tuple[int, ...]:
    """The sorted pids of ``rows`` that are colours of ``target``."""
    suffix = colour_command_suffix(target / "profile")
    return tuple(sorted(pid for pid, _, command in rows if command.endswith(suffix)))


def read_colour_census(run_ps: ProcessTableReader, target: Path) -> tuple[int, ...] | None:
    """The target's colour pids, or ``None`` when the process table cannot be read."""
    rows = read_process_rows(run_ps)
    return None if rows is None else colour_pids(rows, target)


def recovery_command(uid: int, label: str, pids: tuple[int, ...]) -> str:
    """The one command that hands serving back to launchd: stop the colours outside it, then restart the job."""
    return f"kill {' '.join(str(pid) for pid in pids)} && launchctl kickstart -k gui/{uid}/{label}"


def colour_outside_guidance(uid: int, label: str, observation: dict[str, JsonValue]) -> tuple[str, str]:
    """``(message, repair)`` for ``colour_outside_launchagent``, from the journaled ``pre_transition`` observation."""
    census = tuple(pid for pid in cast(list[JsonValue], observation.get("colour_pids") or []) if isinstance(pid, int))
    launchd_pid = observation.get("pid")
    outside = tuple(pid for pid in census if pid != launchd_pid)
    if not outside:
        message = f"The LaunchAgent {label} runs pid {launchd_pid}, which is not a colour of this solet; nothing was started."
        return message, f"Inspect it with `ps -p {launchd_pid} -o command=`, restart the job with `launchctl kickstart -k gui/{uid}/{label}`, then preview again."
    listed = ", ".join(str(pid) for pid in outside)
    message = f"The LaunchAgent {label} is not the process serving this solet: pid {listed} runs its code outside launchd, as after an in-process swap; nothing was started."
    repair = f"Hand serving back to launchd once, then preview again: `{recovery_command(uid, label, outside)}`. If a pid survives, `kill -9` it, then preview again."
    return message, repair
