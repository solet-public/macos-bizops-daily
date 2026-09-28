"""SIGUSR1 all-thread Python stack dump to a dedicated log file (iss_e0648481).

``kill -USR1 <pid>`` writes every thread's Python stack to
``<app_home>/data/logs/faulthandler_stacks.log`` without stopping or
restarting the process. Two 2026-09-28 action-path stalls (iss_30fb08fd) were
diagnosed from native ``sample`` output alone, because nothing could name the
Python frame the busy handler thread was in; this makes the next one legible.

The file, not stderr: under the LaunchAgent stderr is easy to lose, and a
dump that lands nowhere is no dump. ``faulthandler`` writes to the raw file
descriptor from inside the signal handler, so the file object must stay open
and referenced for the whole process lifetime -- if it were garbage-collected
the descriptor would close and a later dump would write to whatever reused
it. ``_dump_file_ref`` holds that reference.

Attribution (review N5): both blue-green colours append to the same file, so
every entry names its process. Install writes ``=== stack-dump handler
installed pid=<pid> at <UTC> ===``. Each dump is followed by ``=== SIGUSR1
stack dump above: pid=<pid> at <UTC> ===``, written by a chained Python-level
handler. It follows the dump rather than preceding it on purpose: the stacks
themselves are written by faulthandler's C-level handler, which runs even when
a thread holds the GIL in C code (the 2026-08-15 failure mode). A Python
handler cannot run then, so only the trailer can be lost, never the stacks.
"""

from __future__ import annotations

import faulthandler
import os
import signal
from datetime import UTC, datetime
from pathlib import Path
from types import FrameType
from typing import TextIO

STACK_DUMP_FILENAME = "faulthandler_stacks.log"

_dump_file_ref: TextIO | None = None


def install_stack_dump_handler(app_home: Path) -> Path:
    """Register the SIGUSR1 dump handler; return the dump file's path.

    Idempotent: a second call keeps the first registration and file. Raises
    whatever opening the file or registering the handler raises -- a process
    that cannot be made diagnosable should say so at startup.
    """
    global _dump_file_ref
    path = app_home / "data" / "logs" / STACK_DUMP_FILENAME
    if _dump_file_ref is not None:
        return path
    path.parent.mkdir(parents=True, exist_ok=True)
    dump_file = path.open("a", encoding="utf-8")
    _write_marker(dump_file, "stack-dump handler installed")

    def _after_dump(_signum: int, _frame: FrameType | None) -> None:
        _write_marker(dump_file, "SIGUSR1 stack dump above:")

    # Python-level handler first, then faulthandler chained in front of it:
    # the C-level dump runs immediately, then chains to this marker.
    signal.signal(signal.SIGUSR1, _after_dump)
    faulthandler.register(signal.SIGUSR1, file=dump_file, all_threads=True, chain=True)
    _dump_file_ref = dump_file
    return path


def _write_marker(dump_file: TextIO, label: str) -> None:
    stamp = datetime.now(UTC).isoformat(timespec="seconds")
    dump_file.write(f"=== {label} pid={os.getpid()} at {stamp} ===\n")
    dump_file.flush()


__all__ = ["STACK_DUMP_FILENAME", "install_stack_dump_handler"]
