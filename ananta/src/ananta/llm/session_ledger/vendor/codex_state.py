"""Codex CLI ``state_5.sqlite::threads`` parser + normalizer (vendor='codex').

State-DB contents (probed empirically 2026-06-11 PT against operator's
state_5.sqlite — 406 thread rows, 171 thread_dynamic_tools rows):

* ``threads`` — one row per Codex CLI thread. Columns of interest:
  ``id`` (TEXT PRIMARY KEY; matches CODEX_LOCAL external_session_id +
  CODEX_PUSHED external_session_id, so canonical-pointer dedupe per
  spec §5.4 rank 2 happens naturally),
  ``title`` (TEXT NOT NULL; in practice this is operator's verbatim
  first-user-message text — `title == first_user_message == preview`
  for 92% of rows — lifted as ``summary_text_seed`` per v2 §5.5
  Path 1 when non-empty),
  ``created_at`` (INTEGER seconds-since-epoch NOT NULL),
  ``updated_at`` (INTEGER seconds-since-epoch NOT NULL),
  ``model``, ``reasoning_effort``, ``cwd``, ``git_branch``,
  ``tokens_used``, ``archived`` — bundled into one SYSTEM event per
  thread with ``content_json.subtype='codex_state'``.

* ``thread_dynamic_tools`` — composite PK (thread_id, position); 7
  columns; FK ``thread_id`` references ``threads.id``. ANCILLARY-tier
  per spec §5.4 rank 5; each row emitted as one SYSTEM event with
  ``content_json.subtype='codex_dynamic_tool'``.

Cursor semantics (plugin-private; opaque to platform):

* Discovery cursor:  ``{"max_rowid": int | null}`` — SQLite implicit
  rowid is insertion-monotonic (table is NOT ``WITHOUT ROWID``),
  ``MIN(rowid)=1 / MAX(rowid)=406`` confirmed; rowid order tracks
  ``created_at`` order. New threads land at MAX(rowid)+1.
* Event-read cursor: ``{"max_position": int | null}`` — for each
  thread, the highest ``position`` seen across BOTH the synthetic
  state row (always position=0) and the dynamic-tools rows (position
  starts at 0 per Codex). Re-reads dedupe on (subtype, position).

All reads use ``PRAGMA query_only=ON`` per spec §3.5 acceptance
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

DEFAULT_DB_PATH = "~/.codex/state_5.sqlite"

SUBTYPE_THREAD_STATE = "codex_state"
SUBTYPE_DYNAMIC_TOOL = "codex_dynamic_tool"

_STATE_PAYLOAD_KEYS: tuple[str, ...] = (
    "model",
    "reasoning_effort",
    "cwd",
    "git_sha",
    "git_branch",
    "git_origin_url",
    "tokens_used",
    "has_user_event",
    "archived",
    "archived_at",
    "source",
    "model_provider",
    "sandbox_policy",
    "approval_mode",
    "cli_version",
    "agent_nickname",
    "agent_role",
    "memory_mode",
    "thread_source",
    "rollout_path",
    "preview",
)

_DYNAMIC_TOOL_KEYS: tuple[str, ...] = (
    "position",
    "name",
    "description",
    "input_schema",
    "defer_loading",
    "namespace",
)


@dataclass(frozen=True, slots=True)
class ThreadRow:
    """Subset of ``threads`` columns surfaced to the source plugin.

    The plugin uses ``id`` as ``external_session_id`` and ``title``
    as the ``summary_text_seed`` when non-empty.
    """

    id: str
    title: str
    created_at: int
    updated_at: int
    cwd: str
    state_metadata: dict[str, Any]


def open_readonly(db_path: Path) -> sqlite3.Connection:
    """Open ``db_path`` read-only via SQLite's ``mode=ro`` URI scheme.

    Also issues ``PRAGMA query_only=ON`` so even a stale handle that
    re-opens through ATTACH cannot mutate the operator-state file.
    The two together honor the acceptance criterion that fixture mtime
    is preserved across live runs (spec §3.5).
    """
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    con.execute("PRAGMA query_only=ON")
    return con


def iter_thread_rows(
    con: sqlite3.Connection,
    *,
    after_rowid: int | None,
) -> Iterator[tuple[int, ThreadRow]]:
    """Yield (rowid, ThreadRow) for threads with rowid > ``after_rowid``.

    Returned in ascending rowid order so the cursor advances
    monotonically. ``after_rowid=None`` means yield from the
    beginning.
    """
    select_columns = "rowid, id, title, created_at, updated_at, cwd, " + ", ".join(
        _STATE_PAYLOAD_KEYS,
    )
    if after_rowid is None:
        sql = f"SELECT {select_columns} FROM threads ORDER BY rowid"
        params: tuple[Any, ...] = ()
    else:
        sql = f"SELECT {select_columns} FROM threads WHERE rowid > ? ORDER BY rowid"
        params = (after_rowid,)
    for row in con.execute(sql, params):
        rowid = int(row[0])
        thread_id = str(row[1])
        title = str(row[2])
        created_at = int(row[3])
        updated_at = int(row[4])
        cwd = str(row[5])
        state_metadata = _row_to_state_payload(row[6:])
        yield rowid, ThreadRow(
            id=thread_id,
            title=title,
            created_at=created_at,
            updated_at=updated_at,
            cwd=cwd,
            state_metadata=state_metadata,
        )


def iter_dynamic_tools(
    con: sqlite3.Connection,
    *,
    thread_id: str,
) -> Iterator[dict[str, Any]]:
    """Yield ``thread_dynamic_tools`` rows for ``thread_id`` (ordered by position)."""
    select_columns = ", ".join(_DYNAMIC_TOOL_KEYS)
    sql = (
        f"SELECT {select_columns} FROM thread_dynamic_tools "
        f"WHERE thread_id = ? ORDER BY position"
    )
    for row in con.execute(sql, (thread_id,)):
        yield _row_to_dynamic_tool_payload(row)


def build_state_event(
    thread: ThreadRow,
) -> RawSessionEvent:
    """One synthetic SYSTEM event per thread (`subtype='codex_state'`)."""
    payload = {
        "subtype": SUBTYPE_THREAD_STATE,
        "position": 0,
        **thread.state_metadata,
    }
    return RawSessionEvent(
        external_session_id=thread.id,
        payload=payload,
        event_at=_epoch_seconds_to_utc(thread.created_at),
        vendor_event_id=f"{thread.id}::state",
        vendor_parent_event_id=None,
    )


def build_dynamic_tool_event(
    thread: ThreadRow,
    tool: dict[str, Any],
) -> RawSessionEvent:
    """One SYSTEM event per dynamic-tool row (`subtype='codex_dynamic_tool'`)."""
    position = int(tool["position"])
    payload = {"subtype": SUBTYPE_DYNAMIC_TOOL, **tool}
    return RawSessionEvent(
        external_session_id=thread.id,
        payload=payload,
        event_at=_epoch_seconds_to_utc(thread.created_at),
        vendor_event_id=f"{thread.id}::tool::{position}",
        vendor_parent_event_id=f"{thread.id}::state",
    )


def normalize_raw(raw: RawSessionEvent) -> NormalizedSessionEvent:
    """Vendor → canonical normalization for state-DB SYSTEM events.

    All M19 events are SYSTEM-typed with a ``subtype`` discriminator
    carried in ``content_json`` per the platform's SYSTEM-event
    subtype-lift convention (KB
    ``19_session_ledger_01_system_event_subtype_lift.md``). The raw
    payload is reused as-is for ``content_json`` so SQL filters can
    pivot on ``content_json::jsonb->>'subtype'``.
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


def _row_to_state_payload(values: tuple[Any, ...]) -> dict[str, Any]:
    return {
        key: _scalar(value) for key, value in zip(_STATE_PAYLOAD_KEYS, values, strict=True)
    }


def _row_to_dynamic_tool_payload(values: tuple[Any, ...]) -> dict[str, Any]:
    return {
        key: _scalar(value) for key, value in zip(_DYNAMIC_TOOL_KEYS, values, strict=True)
    }


def _scalar(value: Any) -> Any:
    """SQLite drops NULL as Python None; other scalars pass through."""
    return value


def _epoch_seconds_to_utc(epoch_seconds: int) -> datetime:
    return datetime.fromtimestamp(epoch_seconds, tz=UTC)
