"""Cursor read/write/advance helpers for the session dispatch bridge (W4 M1.5).

A *cursor* is one drainer's durable read position into the singleton spool
(design §3 D2.2). Each connected solet has exactly one cursor file at
``<spool>/cursors/<drainer_id>.cursor``. These are pure, side-effect-scoped
helpers: read, advance, retire, and atomic-versioned write. The janitor
(``janitor.py``) consumes them; the drainer (``drainer.py``) writes its own.

Atomicity (design §3 D2.2 "write-temp + rename"): ``write_cursor`` writes to a
pid-scoped temp file, ``fsync``s, then ``os.replace``s over the target. POSIX
rename is atomic, so a janitor reading concurrently with a drainer write always
observes a complete older-or-newer generation — never a torn file. The
``version`` write counter is the generation marker on top of that guarantee.

On a corrupt/unreadable cursor the reader returns ``None`` rather than guessing;
the drainer then rebuilds from the oldest retained spool file (``position=""``)
and re-drains forward, which is safe because the consumer is idempotent
(design §6: ``upsert_memory_by_tag`` is last-write-wins, the audit ``remember``
dedupes by ``(task id, event)``).
"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from pathlib import Path

from .spool_schema import (
    CURSOR_FIELD_DRAINER_ID,
    CURSOR_FIELD_HEARTBEAT,
    CURSOR_FIELD_POSITION,
    CURSOR_FIELD_RETIRED,
    CURSOR_FIELD_VERSION,
    CURSOR_SUFFIX,
    CursorState,
)

# Empty position == "nothing drained yet"; sorts before every real filename, so a
# fresh cursor re-drains the whole retained spool and a watermark of "" deletes
# nothing.
INITIAL_POSITION = ""


def now_iso() -> str:
    """Current UTC time as an ISO-8601 string (the heartbeat / timestamp format)."""
    return datetime.now(UTC).isoformat()


def cursor_path(cursor_dir: Path, drainer_id: str) -> Path:
    """Filesystem path of one drainer's cursor file."""
    return cursor_dir / f"{drainer_id}{CURSOR_SUFFIX}"


def fresh_cursor(drainer_id: str, heartbeat: str) -> CursorState:
    """A brand-new cursor that has drained nothing yet (version 1)."""
    return CursorState(
        version=1,
        drainer_id=drainer_id,
        position=INITIAL_POSITION,
        heartbeat=heartbeat,
        retired=False,
    )


def next_cursor(prev: CursorState | None, drainer_id: str, position: str, heartbeat: str) -> CursorState:
    """The drainer's per-tick cursor: advance ``position``, refresh ``heartbeat``,
    bump ``version``, and clear ``retired`` (writing proves the drainer is alive,
    so a previously-retired drainer un-retires itself on return)."""
    version = prev["version"] + 1 if prev is not None else 1
    return CursorState(
        version=version,
        drainer_id=drainer_id,
        position=position,
        heartbeat=heartbeat,
        retired=False,
    )


def retired_cursor(state: CursorState) -> CursorState:
    """Mark a cursor retired (janitor side) — bump ``version``, preserve the stale
    ``heartbeat`` and ``position`` so the audit trail of when it went quiet stays."""
    return CursorState(
        version=state["version"] + 1,
        drainer_id=state["drainer_id"],
        position=state["position"],
        heartbeat=state["heartbeat"],
        retired=True,
    )


def force_advanced_cursor(state: CursorState, position: str) -> CursorState:
    """Force-advance a cursor's ``position`` (janitor retention-ceiling side) —
    bump ``version`` but preserve ``heartbeat`` and ``retired``: force-advance
    never fakes liveness for a dead drainer, it only moves the read position past
    dropped records."""
    return CursorState(
        version=state["version"] + 1,
        drainer_id=state["drainer_id"],
        position=position,
        heartbeat=state["heartbeat"],
        retired=state["retired"],
    )


def heartbeat_age_seconds(state: CursorState, now: datetime) -> float:
    """Seconds since this cursor's last heartbeat (used to detect staleness)."""
    return (now - datetime.fromisoformat(state["heartbeat"])).total_seconds()


def _coerce(raw: object) -> CursorState | None:
    """Validate a parsed JSON object against the cursor schema; ``None`` if invalid."""
    if not isinstance(raw, dict):
        return None
    version = raw.get(CURSOR_FIELD_VERSION)
    drainer_id = raw.get(CURSOR_FIELD_DRAINER_ID)
    position = raw.get(CURSOR_FIELD_POSITION)
    heartbeat = raw.get(CURSOR_FIELD_HEARTBEAT)
    retired = raw.get(CURSOR_FIELD_RETIRED)
    if not (
        isinstance(version, int)
        and isinstance(drainer_id, str)
        and isinstance(position, str)
        and isinstance(heartbeat, str)
        and isinstance(retired, bool)
    ):
        return None
    return CursorState(
        version=version,
        drainer_id=drainer_id,
        position=position,
        heartbeat=heartbeat,
        retired=retired,
    )


def read_cursor(cursor_dir: Path, drainer_id: str) -> CursorState | None:
    """Read one cursor; ``None`` if absent, unreadable, or corrupt (the caller
    rebuilds from the oldest retained spool file — design §6 cursor-corruption)."""
    path = cursor_path(cursor_dir, drainer_id)
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return None
    try:
        return _coerce(json.loads(raw))
    except json.JSONDecodeError:
        return None


def read_all_cursors(cursor_dir: Path) -> list[CursorState]:
    """Read every valid cursor in the directory (skips corrupt/unreadable ones)."""
    if not cursor_dir.is_dir():
        return []
    cursors: list[CursorState] = []
    for path in sorted(cursor_dir.glob(f"*{CURSOR_SUFFIX}")):
        try:
            raw = path.read_text(encoding="utf-8")
        except OSError:
            continue
        try:
            parsed = _coerce(json.loads(raw))
        except json.JSONDecodeError:
            continue
        if parsed is not None:
            cursors.append(parsed)
    return cursors


def write_cursor(cursor_dir: Path, state: CursorState) -> None:
    """Atomically write a cursor (temp + fsync + rename — design §3 D2.2)."""
    cursor_dir.mkdir(parents=True, exist_ok=True)
    target = cursor_path(cursor_dir, state["drainer_id"])
    tmp = cursor_dir / f"{state['drainer_id']}{CURSOR_SUFFIX}.tmp.{os.getpid()}"
    payload = json.dumps(state, ensure_ascii=False, sort_keys=True)
    with tmp.open("w", encoding="utf-8") as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, target)
