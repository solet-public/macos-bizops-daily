"""Codex CLI ``goals_1.sqlite::thread_goals`` parser + normalizer (vendor='codex').

State-DB contents (probed empirically 2026-06-11 PT against operator's
``~/.codex/goals_1.sqlite``; 0 rows today — schema-only probe):

* ``thread_goals`` — one row per active/completed goal per thread.
  ``thread_id`` is the PRIMARY KEY (so at most one goal row per Codex
  thread at a time; status transitions UPDATE the row in place). Schema
  (P1 §B.M11, verified 2026-06-11 PT against operator's empty DB):
  ::

      CREATE TABLE thread_goals (
          thread_id           TEXT PRIMARY KEY NOT NULL,
          goal_id             TEXT NOT NULL,
          objective           TEXT NOT NULL,
          status              TEXT NOT NULL CHECK(status IN (
                              'active','paused','blocked',
                              'usage_limited','budget_limited','complete')),
          token_budget        INTEGER,
          tokens_used         INTEGER NOT NULL DEFAULT 0,
          time_used_seconds   INTEGER NOT NULL DEFAULT 0,
          created_at_ms       INTEGER NOT NULL,
          updated_at_ms       INTEGER NOT NULL
      )

Timestamp-unit verification (brief §0.0 MANDATORY probe):

Operator's ``thread_goals`` is empty (0 rows), so the SQL-side
``datetime(…, 'unixepoch')`` probe cannot distinguish ms vs seconds
empirically. Falling back to the canonical write-site in the Codex Rust
source (operator-local checkout at
``~/Workspace/codex-rs-wake-0.138.0/codex-rs/state/src/runtime/goals.rs``
lines 67, 124):

::

    let now_ms = datetime_to_epoch_millis(Utc::now());
    …
    .bind(now_ms)  // created_at_ms
    .bind(now_ms)  // updated_at_ms

The ``_ms`` suffix on the column name is honest — these are
**milliseconds-since-epoch**. ``event_at = datetime.fromtimestamp(ts_ms /
1000, tz=UTC)``. Smokes assert ``event_at.year >= 2025`` so a future
unit regression (or another Codex schema mutation that re-purposes the
field) is caught at smoke time, not silently displaced 50 years.

Cross-reference with prior Tier B / C M-sections, since Codex CLI uses
mixed unit conventions across its own files:

* M19 ``state_5.threads.created_at`` (and ``updated_at``) — INTEGER
  **seconds** (no ``_ms`` suffix).
* M19 ``state_5.threads.created_at_ms`` (and ``updated_at_ms``,
  nullable) — INTEGER **milliseconds** (``_ms`` suffix).
* M8 ``history.jsonl.ts`` — INTEGER **seconds** (no ``_ms`` suffix
  despite the field carrying wall-clock time).
* **M11 ``goals_1.thread_goals.{created_at_ms, updated_at_ms}``**
  — INTEGER **milliseconds**, confirmed via Codex Rust write-site.

Pattern across vendor data: ``_ms`` suffix => milliseconds; unsuffixed
=> seconds. Honest naming where present.

Each row emits ONE SYSTEM event with::

    content_json = {
        "subtype": "codex_goal",
        "goal_id": …,
        "objective": …,
        "status": …,
        "token_budget": … | None,
        "tokens_used": …,
        "time_used_seconds": …,
        "created_at_ms": …,
        "updated_at_ms": …,
    }

The ``vendor_event_id`` carries the ``updated_at_ms`` so status
transitions emit as distinct events at the importer's ``(session_id,
vendor_event_id)`` idempotency seam. Cursor semantics live on the source
plugin (``{updated_at_high_water: int}``, unit-agnostic key; the
actual unit is ms by the above).

All reads use ``PRAGMA query_only=ON`` per brief §2.4 acceptance
(read-only honored; fixture mtime preserved under live run).
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ananta.llm.session_ledger.types import (
    EventType,
    NormalizedSessionEvent,
    RawSessionEvent,
)

DEFAULT_DB_PATH = "~/.codex/goals_1.sqlite"

SUBTYPE_THREAD_GOAL = "codex_goal"

_GOAL_PAYLOAD_KEYS: tuple[str, ...] = (
    "goal_id",
    "objective",
    "status",
    "token_budget",
    "tokens_used",
    "time_used_seconds",
    "created_at_ms",
    "updated_at_ms",
)


@dataclass(frozen=True, slots=True)
class GoalRow:
    """A single ``thread_goals`` row exposed to the source plugin."""

    thread_id: str
    goal_id: str
    objective: str
    status: str
    updated_at_ms: int
    created_at_ms: int
    payload: dict[str, Any]


def open_readonly(db_path: Path) -> sqlite3.Connection:
    """Open ``db_path`` read-only via SQLite's ``mode=ro`` URI scheme.

    Also issues ``PRAGMA query_only=ON`` so even a stale handle that
    re-opens through ATTACH cannot mutate the operator-state file. The
    two together honor the acceptance criterion that fixture mtime is
    preserved across live runs (brief §2.4).
    """
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    con.execute("PRAGMA query_only=ON")
    return con


def iter_goal_rows(
    con: sqlite3.Connection,
    *,
    after_updated_at: int | None,
) -> Iterator[GoalRow]:
    """Yield ``GoalRow`` for goals with ``updated_at_ms > after_updated_at``.

    Returned in ascending ``updated_at_ms`` order so the cursor advances
    monotonically and status transitions land as distinct events at the
    importer's ``(session_id, vendor_event_id)`` idempotency seam.
    """
    select_columns = "thread_id, " + ", ".join(_GOAL_PAYLOAD_KEYS)
    if after_updated_at is None:
        sql = f"SELECT {select_columns} FROM thread_goals ORDER BY updated_at_ms"
        params: tuple[Any, ...] = ()
    else:
        sql = (
            f"SELECT {select_columns} FROM thread_goals "
            f"WHERE updated_at_ms > ? ORDER BY updated_at_ms"
        )
        params = (after_updated_at,)
    for row in con.execute(sql, params):
        thread_id = str(row[0])
        payload = dict(zip(_GOAL_PAYLOAD_KEYS, row[1:], strict=True))
        yield GoalRow(
            thread_id=thread_id,
            goal_id=str(payload["goal_id"]),
            objective=str(payload["objective"]),
            status=str(payload["status"]),
            updated_at_ms=int(payload["updated_at_ms"]),
            created_at_ms=int(payload["created_at_ms"]),
            payload=payload,
        )


def build_goal_event(row: GoalRow) -> RawSessionEvent:
    """Build the SYSTEM event for a ``thread_goals`` row.

    ``event_at`` uses ``updated_at_ms`` (not ``created_at_ms``) so each
    status transition lands at its own wall-clock; the importer's
    timeline-ordering then reflects the user-observable change moment.
    """
    payload = {"subtype": SUBTYPE_THREAD_GOAL, **row.payload}
    return RawSessionEvent(
        external_session_id=row.thread_id,
        payload=payload,
        event_at=datetime.fromtimestamp(row.updated_at_ms / 1000, tz=UTC),
        vendor_event_id=f"goal_{row.goal_id}_{row.updated_at_ms}",
        vendor_parent_event_id=None,
    )


def normalize_raw(raw: RawSessionEvent) -> NormalizedSessionEvent:
    """Vendor → canonical normalization for goal SYSTEM events.

    All M11 events are SYSTEM-typed with ``content_json.subtype='codex_goal'``
    per the platform's SYSTEM-event subtype-lift convention (KB
    ``19_session_ledger_01_system_event_subtype_lift.md``). The raw
    payload is reused as-is for ``content_json`` so SQL filters can pivot
    on ``content_json::jsonb->>'subtype'``.
    """
    return NormalizedSessionEvent(
        external_session_id=raw.external_session_id,
        event_type=EventType.SYSTEM,
        role=None,
        content_text=None,
        content_json=dict(raw.payload),
        event_at=raw.event_at,
        vendor_event_id=raw.vendor_event_id,
        vendor_parent_event_id=raw.vendor_parent_event_id,
        attachment_blob_upload=None,
        attachment_mime_type=None,
        attachment_filename=None,
    )
