#!/usr/bin/env python3
"""Shared "battery ran under measurable host contention" signal.

Gate/smoke infrastructure that is reliable in isolation becomes unreliable —
flaky-red, silently-skipped, or unattributable — once real fleet conditions
(concurrent lanes, host load, timeouts) apply. Five issues named this same
underlying gap from different angles:

- iss_35dbc06e (342): a concurrent session editing the shared checkout mid-scan
  produced a red the host lock does not cover.
- iss_dfca317c (357): the host-lock smoke's own contention check has no
  holder-liveness check, so a stale/leaked lock is indistinguishable from a
  live sibling and skips forever as a normal disclosed skip.
- iss_c35292ca (358): the host-lock wait is only 0.5s, so under steady fleet
  load the smoke can go effectively dark while every packet reports a
  disclosed skip — nothing tracked whether it ever actually ran.
- iss_cf3a897d (756): a smoke failed under the pooled/concurrent register run
  but passed 70/70 in isolation, with no code-level way to tell "this red
  co-occurred with real contention" from "this is a real regression".
  and
- iss_0ecb55dc (760): a smoke timed out under ~35+ concurrent lanes, and even
  a quiet-host control run still had a concurrent process active, so
  host-load vs. real-regression remained undistinguished.

Every one of these is an instance of the SAME missing fact: whether a given
battery run executed under measurable host contention was never recorded
anywhere the run's own result could carry it. This module measures that fact
along the two axes already present in this codebase's own concurrency model
(module docstring of ``run_smokes.py``):

- ``concurrent_live_batteries``: how many OTHER full-battery runs were
  genuinely alive (pid-liveness verified, not just "receipt says running") at
  the moment measured — the general "how many lanes/sessions were sharing
  this host" signal (342, 756, 760).
- ``host_lock_wait_seconds``: how long THIS battery waited to acquire the
  host-global serial-only lock (``run_smokes.py``'s ``_HOST_SERIAL_LOCK_PATH``)
  — a direct, always-present measurement of contention for the specific
  host-exclusive resource the ``_SERIAL_ONLY`` smokes share (357, 358).

The liveness probe (``holder_pid_is_alive``) is the one new primitive: a
receipt or lock file that says "running"/"held" is trusted only when the pid
it recorded is still alive, so a run that crashed without reaching its own
cleanup is not silently counted as live contention. The same probe is reused
by ``run_smokes_host_lock_smoke.py`` (iss_dfca317c) to turn a lock held by a
DEAD holder into a fail (a real leak, worth a red) instead of an indefinite,
indistinguishable skip.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import json
import os
from pathlib import Path

# Anything at or below this is "acquired essentially immediately" — real
# contention on this lock has measured in the seconds, not milliseconds
# (quality_gates/tests/run_smokes_host_lock_smoke.py's own fixtures use
# sleeps of 0.1-1.5s to produce a measurable, non-flaky gap).
CONTENDED_THRESHOLD_SECONDS = 0.05

_LOCK_HOLDER_PID_PREFIX = "pid="


@dataclasses.dataclass(frozen=True, slots=True)
class BatteryContention:
    """One battery run's measured host-contention snapshot."""

    measured_at: str
    concurrent_live_batteries_start: int
    concurrent_live_batteries_finish: int
    host_lock_wait_seconds: float
    host_lock_contended: bool

    @property
    def concurrent_live_batteries_peak(self) -> int:
        """The higher of the start/finish counts.

        A battery can run for minutes; a single end-of-run snapshot would
        miss contention that came and went mid-run, which is exactly the
        shape of iss_cf3a897d/756 and iss_0ecb55dc/760 (concurrent load from
        other lanes, not necessarily still present at the exact end).
        """
        return max(self.concurrent_live_batteries_start, self.concurrent_live_batteries_finish)

    def as_dict(self) -> dict[str, object]:
        payload = dataclasses.asdict(self)
        payload["concurrent_live_batteries_peak"] = self.concurrent_live_batteries_peak
        return payload

    def render(self) -> str:
        """One human-readable line for the run's own stdout summary."""
        peak = self.concurrent_live_batteries_peak
        plural = "y" if peak == 1 else "ies"
        contended = " (CONTENDED)" if self.host_lock_contended else ""
        return (
            f"contention: {peak} concurrent live batter{plural} (peak), "
            f"host-lock wait {self.host_lock_wait_seconds:.2f}s{contended}"
        )


def utc_timestamp() -> str:
    """Return an unambiguous measurement timestamp (matches run_smokes.py)."""
    return dt.datetime.now(tz=dt.UTC).isoformat(timespec="seconds")


def holder_pid_is_alive(pid: int) -> bool:
    """Probe whether ``pid`` names a live process via POSIX signal 0.

    ``ProcessLookupError`` is the only "no" answer: the pid is gone.
    ``PermissionError`` means the process exists but is owned by someone
    else — still alive, just not ours to signal. Any other outcome
    (including success) means alive.
    """
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def parse_lock_holder_pid(lock_text: str) -> int | None:
    """Parse the ``pid=<N> cwd=<path>`` text a lock holder writes into its file.

    Returns ``None`` on anything that doesn't parse as a decimal pid — a
    genuinely empty or foreign-format lock file is a "can't tell" case, not
    a dead-pid case, and callers must not conflate the two.
    """
    for token in lock_text.split():
        if token.startswith(_LOCK_HOLDER_PID_PREFIX):
            candidate = token[len(_LOCK_HOLDER_PID_PREFIX) :]
            if candidate.isdigit():
                return int(candidate)
    return None


def count_concurrent_live_batteries(receipt_dir: Path, *, exclude_path: Path | None = None) -> int:
    """Count OTHER full-battery receipts that are genuinely still running.

    A receipt with ``result == "running"`` is trusted only when its recorded
    pid is still alive. A hard-killed battery never reaches
    ``_finish_receipt``, so its receipt can say "running" long after the
    process is gone — counting that as live contention would be exactly the
    stale-vs-live confusion iss_dfca317c calls out for the host lock; the
    same liveness probe resolves it here too.
    """
    if not receipt_dir.is_dir():
        return 0
    count = 0
    for candidate in receipt_dir.glob("*.json"):
        if exclude_path is not None and candidate == exclude_path:
            continue
        try:
            loaded = json.loads(candidate.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(loaded, dict) or loaded.get("result") != "running":
            continue
        pid = loaded.get("pid")
        if isinstance(pid, int) and holder_pid_is_alive(pid):
            count += 1
    return count


def measure(
    *,
    concurrent_live_batteries_start: int,
    concurrent_live_batteries_finish: int,
    host_lock_wait_seconds: float,
) -> BatteryContention:
    """Assemble one battery's contention snapshot.

    The caller takes both ``concurrent_live_batteries_*`` snapshots itself
    (via ``count_concurrent_live_batteries``, once before and once after
    running the suite) since they bracket the whole battery, not a single
    instant this function could measure on its own.
    """
    return BatteryContention(
        measured_at=utc_timestamp(),
        concurrent_live_batteries_start=concurrent_live_batteries_start,
        concurrent_live_batteries_finish=concurrent_live_batteries_finish,
        host_lock_wait_seconds=host_lock_wait_seconds,
        host_lock_contended=host_lock_wait_seconds > CONTENDED_THRESHOLD_SECONDS,
    )


_SKIP_EVENT_LOG_ENV = "RUN_SMOKES_HOST_LOCK_SKIP_LOG_PATH"
_DEFAULT_SKIP_EVENT_LOG = Path.home() / ".ananta" / "runtime" / "run_smokes_host_lock_skips.jsonl"


def skip_event_log_path() -> Path:
    """Return the durable, append-only host-lock-skip event log path."""
    configured = os.environ.get(_SKIP_EVENT_LOG_ENV)
    return Path(configured) if configured else _DEFAULT_SKIP_EVENT_LOG


def record_host_lock_skip_event(
    *, holder_pid: int | None, holder_alive: bool | None, waited_seconds: float, log_path: Path | None = None
) -> None:
    """Append one disclosed host-lock skip to a durable, cross-run log.

    iss_c35292ca: every prior signal was a single packet's SKIP line with no
    memory across runs, so "went dark" was an inference, never a fact. This
    log is the cheap run counter the issue asks for — its line count (or the
    gap between consecutive ``recorded_at`` timestamps) turns that inference
    into something a reader can just look at.
    """
    path = log_path or skip_event_log_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    event = {
        "recorded_at": utc_timestamp(),
        "holder_pid": holder_pid,
        "holder_alive": holder_alive,
        "waited_seconds": waited_seconds,
    }
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(event, sort_keys=True) + "\n")
