"""Spool janitor for the session dispatch bridge (W4 M1.5, design §3 D2.2).

A shared library function — NOT a separate process — invoked under a host-level
``flock`` by whichever drainer is currently cycling. The flock serialises
concurrent janitor runs across every local drainer (in-process local-solet drainer
plus, from M1.5b, the client-deployed cloud-solet drainers), so it runs
correctly in local-only, cloud-only, and mixed topologies with no daemon to
supervise.

On each run it:

1. **Retires stale drainers.** A cursor whose heartbeat is older than
   ``RETIREMENT_THRESHOLD_SECONDS`` is marked ``retired`` and dropped from the
   live set, so a crashed / long-unreachable drainer stops holding the watermark.
2. **Enforces the retention ceiling.** If the spool exceeds
   ``RETENTION_CEILING_BYTES`` or its oldest file is older than
   ``RETENTION_CEILING_SECONDS``, it FORCE-ADVANCES every cursor (live and
   retired) to the newest spool file, drops everything below, and writes an
   audit ``GapMarker`` — bounded disk wins over perfect observability, but the
   dropped range is recorded, never silently swallowed.
3. **Otherwise deletes the drained backlog.** It deletes spool files strictly
   below the watermark = ``min(position)`` over LIVE cursors. Files at or above
   any live cursor are retained (the watermark file itself stays — it is the
   resume anchor and the "files at/above X remain" invariant).
"""

from __future__ import annotations

import fcntl
import json
import os
from collections.abc import Generator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from .cursor import (
    INITIAL_POSITION,
    force_advanced_cursor,
    heartbeat_age_seconds,
    read_all_cursors,
    retired_cursor,
    write_cursor,
)
from .spool_schema import (
    GAP_MARKER_PREFIX,
    GAP_MARKER_SUFFIX,
    SPOOL_GLOB,
    CursorState,
    GapMarker,
)

# Janitor tunables (design OQ-5 — values, not architecture; ship defaults, tune later).
HEARTBEAT_INTERVAL_SECONDS = 5.0  # the drainer refreshes its heartbeat this often (== drain tick).
RETIREMENT_THRESHOLD_SECONDS = 60.0  # ~12 missed heartbeats -> drainer dropped from the live set.
RETENTION_CEILING_BYTES = 100 * 1024 * 1024  # 100 MB of spool ...
RETENTION_CEILING_SECONDS = 7 * 24 * 60 * 60  # ... or 7 days, whichever first, triggers force-advance.

_GAP_MARKER_TS_FORMAT = "%Y%m%dT%H%M%S_%f"


@dataclass(frozen=True)
class JanitorReport:
    """Outcome of one janitor run (for logging + smoke verification)."""

    deleted: list[str]  # spool filenames deleted this run
    retired: list[str]  # drainer ids newly retired this run
    force_advanced: bool  # whether the retention ceiling forced an advance
    gap_marker: str | None  # path of the gap marker written on force-advance, else None
    watermark: str  # the deletion watermark used (newest filename on force-advance)


@contextmanager
def _janitor_lock(lock_path: Path) -> Generator[None]:
    """Host-level exclusive flock serialising concurrent janitor runs."""
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def list_spool_files(spool_dir: Path) -> list[Path]:
    """Spool files sorted by filename (== chronological for the producer's names).

    Skips dotfiles so the janitor's own gap markers / log never look like spool
    records. Shared by the drainer (to pick the next files to drain) and the
    janitor (to compute the deletion set)."""
    if not spool_dir.is_dir():
        return []
    files = [p for p in spool_dir.glob(SPOOL_GLOB) if not p.name.startswith(".")]
    return sorted(files, key=lambda path: path.name)


def _min_position(cursors: list[CursorState]) -> str:
    """Smallest position across the given cursors (INITIAL_POSITION if none)."""
    if not cursors:
        return INITIAL_POSITION
    return min(cursor["position"] for cursor in cursors)


def _live_watermark(cursors: list[CursorState]) -> str:
    """Deletion watermark = min position over LIVE (non-retired) cursors."""
    return _min_position([cursor for cursor in cursors if not cursor["retired"]])


def _delete_below(files: list[Path], watermark: str) -> list[str]:
    """Delete files strictly below the watermark; return deleted filenames.

    Strictly-below (``<``) is load-bearing: the file *at* the watermark is the
    last fully-drained file of the slowest live drainer and is retained as the
    resume anchor (the "files at/above X remain" invariant — design §3 D2.2)."""
    deleted: list[str] = []
    for spool_file in files:
        if spool_file.name < watermark:
            spool_file.unlink()
            deleted.append(spool_file.name)
    return deleted


def _apply_retirement(
    cursor_dir: Path,
    cursors: list[CursorState],
    now: datetime,
    retirement_threshold_seconds: float,
) -> tuple[list[CursorState], list[str]]:
    """Retire cursors with a stale heartbeat; return (updated cursors, retired ids)."""
    updated: list[CursorState] = []
    newly_retired: list[str] = []
    for cursor in cursors:
        is_stale = heartbeat_age_seconds(cursor, now) > retirement_threshold_seconds
        if not cursor["retired"] and is_stale:
            retired = retired_cursor(cursor)
            write_cursor(cursor_dir, retired)
            updated.append(retired)
            newly_retired.append(cursor["drainer_id"])
        else:
            updated.append(cursor)
    return updated, newly_retired


def _ceiling_exceeded(
    files: list[Path],
    now: datetime,
    retention_ceiling_bytes: int,
    retention_ceiling_seconds: float,
) -> bool:
    """True if the spool is over the size OR age retention ceiling."""
    if not files:
        return False
    if sum(spool_file.stat().st_size for spool_file in files) > retention_ceiling_bytes:
        return True
    oldest_mtime = min(spool_file.stat().st_mtime for spool_file in files)
    return now.timestamp() - oldest_mtime > retention_ceiling_seconds


def _write_gap_marker(
    spool_dir: Path,
    now: datetime,
    prior_min_cursor: str,
    files_dropped: int,
    drainers_force_advanced: list[str],
) -> Path:
    """Write an accumulating, never-auto-deleted gap marker (design §3 D2.2)."""
    marker = GapMarker(
        advanced_at=now.isoformat(),
        prior_min_cursor=prior_min_cursor,
        files_dropped=files_dropped,
        drainers_force_advanced=drainers_force_advanced,
    )
    name = f"{GAP_MARKER_PREFIX}{now.strftime(_GAP_MARKER_TS_FORMAT)}{GAP_MARKER_SUFFIX}"
    path = spool_dir / name
    path.write_text(json.dumps(marker, ensure_ascii=False, sort_keys=True), encoding="utf-8")
    return path


def _force_advance(
    spool_dir: Path,
    cursor_dir: Path,
    files: list[Path],
    cursors: list[CursorState],
    now: datetime,
    retired: list[str],
) -> JanitorReport:
    """Retention-ceiling force-advance: move every cursor to the newest file, drop
    everything below it, and record the gap."""
    newest = files[-1].name
    prior_min = _min_position(cursors)
    advanced_ids: list[str] = []
    for cursor in cursors:
        if cursor["position"] < newest:
            write_cursor(cursor_dir, force_advanced_cursor(cursor, newest))
            advanced_ids.append(cursor["drainer_id"])
    deleted = _delete_below(files, newest)
    gap_path = _write_gap_marker(spool_dir, now, prior_min, len(deleted), advanced_ids)
    return JanitorReport(
        deleted=deleted,
        retired=retired,
        force_advanced=True,
        gap_marker=str(gap_path),
        watermark=newest,
    )


def _run_locked(
    spool_dir: Path,
    cursor_dir: Path,
    now: datetime,
    retirement_threshold_seconds: float,
    retention_ceiling_bytes: int,
    retention_ceiling_seconds: float,
) -> JanitorReport:
    files = list_spool_files(spool_dir)
    cursors, retired = _apply_retirement(
        cursor_dir, read_all_cursors(cursor_dir), now, retirement_threshold_seconds
    )
    if _ceiling_exceeded(files, now, retention_ceiling_bytes, retention_ceiling_seconds):
        return _force_advance(spool_dir, cursor_dir, files, cursors, now, retired)
    watermark = _live_watermark(cursors)
    deleted = _delete_below(files, watermark)
    return JanitorReport(
        deleted=deleted,
        retired=retired,
        force_advanced=False,
        gap_marker=None,
        watermark=watermark,
    )


def run_janitor(
    spool_dir: Path,
    cursor_dir: Path,
    lock_path: Path,
    *,
    now: datetime,
    retirement_threshold_seconds: float = RETIREMENT_THRESHOLD_SECONDS,
    retention_ceiling_bytes: int = RETENTION_CEILING_BYTES,
    retention_ceiling_seconds: float = RETENTION_CEILING_SECONDS,
) -> JanitorReport:
    """Run the janitor once under the host-level flock. ``now`` and the thresholds
    are injected so the drainer passes wall-clock + defaults while smokes pin them."""
    with _janitor_lock(lock_path):
        return _run_locked(
            spool_dir,
            cursor_dir,
            now,
            retirement_threshold_seconds,
            retention_ceiling_bytes,
            retention_ceiling_seconds,
        )
