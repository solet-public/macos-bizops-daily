"""Codex CLI ``memories_1.sqlite::stage1_outputs`` parser (vendor='codex').

Brief §3. Path 1 SEED-PATH CONSUMER — NOT a ``__session`` row producer.
This module is the read-only data layer for the M20 one-shot rewrite
verb ``lift_codex_stage1_summaries``; the verb is the load-bearing
entry point that lifts each ``stage1_outputs.rollout_summary`` into the
EXISTING canonical ``__session.summary_text`` (joined by thread_id
across the codex_state / codex_history / codex_filesystem source kinds
that already produce __session rows for the same Codex thread UUID).

State-DB contents (probed empirically 2026-06-11 PT against operator's
``~/.codex/memories_1.sqlite``; 6 rows, 5 with ``selected_for_phase2=1``):

* ``stage1_outputs`` — one row per Codex thread whose phase-1 memory
  extraction completed. Schema:
  ::

      CREATE TABLE stage1_outputs (
          thread_id                                TEXT PRIMARY KEY,
          source_updated_at                        INTEGER NOT NULL,
          raw_memory                               TEXT NOT NULL,
          rollout_summary                          TEXT NOT NULL,
          rollout_slug                             TEXT,
          generated_at                             INTEGER NOT NULL,
          usage_count                              INTEGER,
          last_usage                               INTEGER,
          selected_for_phase2                      INTEGER NOT NULL DEFAULT 0,
          selected_for_phase2_source_updated_at    INTEGER
      )

Timestamp-unit verification (brief §0.0 MANDATORY probe; pattern from
M11 sustains):

The SQL probe against operator's 6 rows + Codex Rust write-site
cross-check at
``~/Workspace/codex-rs-wake-0.138.0/codex-rs/state/src/runtime/memories.rs``
confirms both ``source_updated_at`` and ``generated_at`` are
**seconds-since-epoch** (no ``_ms`` suffix; pattern matches M19
state_5.threads.created_at + M8 history.jsonl.ts):

* ``source_updated_at``: chrono ``.timestamp()`` writes (line 1246 +
  3236; INSERT/UPDATE bind a chrono ``i64`` seconds value). SQL
  probe on operator's data: range ``2026-05-30 22:23:53`` to
  ``2026-06-05 14:30:20`` interpreted as ``unixepoch`` seconds —
  matches operator's actual machine activity window. Interpreted as
  milliseconds gives 1970.
* ``generated_at``: written via ``.bind(now)`` where
  ``now = Utc::now().timestamp()`` (line 59 → 880). Same pattern.
* ``last_usage``: written via the same ``now`` (line 69). Same
  pattern.

So the cursor key is ``{"generated_at_high_water": int}`` (unit-agnostic
name; actual unit is seconds). The
plugin doesn't use this cursor in normal poll (discover_sessions is a
no-op) — the verb is the load-bearing reader.

``selected_for_phase2`` semantics (brief §3.1 probe (a)):

Verified Codex-AUTOMATIC via the Rust write-site at
``memories.rs:1224-1251`` (``mark_global_phase2_job_succeeded``): when
a phase-2 consolidation job completes, it FIRST clears every
``selected_for_phase2=1`` flag, then UPDATEs ``selected_for_phase2=1``
on each row in ``selected_outputs[]`` (the rows Codex's phase-2
algorithm chose as canonical inputs for the next consolidation pass).
NOT operator-curated. The rollout_summary text is still meaningful as
canonical narrative because it's the LLM-generated stage1 summary of
the Codex thread's rollout — just not operator-blessed; flag as
"machine-curated" in M20 acceptance for downstream operator review per
brief §3.1.

``~/.codex/memories/.git`` provenance (brief §3.1 probe (b)):

Operator's checkout has a single baseline commit
``ec72d7c Initialize Codex git baseline`` (2026-06-07 02:05:28 UTC); no
remote configured; no further commits. The N3 markdown
(``memory_summary.md``, ``MEMORY.md``, ``raw_memories.md``,
``rollout_summaries/``) is tracked but operator-local — no off-machine
push, no observed divergence-vs-N2-sqlite risk surfaced today. If
that cadence changes (new commits appearing on the operator's tree
without corresponding N2 INSERT/UPDATE activity), document the
divergence-risk at that point.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

DEFAULT_DB_PATH = "~/.codex/memories_1.sqlite"

SEED_ATTRIBUTION = "internal:auto_summarize:codex_stage1_seed"

_STAGE1_COLUMNS: tuple[str, ...] = (
    "thread_id",
    "source_updated_at",
    "raw_memory",
    "rollout_summary",
    "rollout_slug",
    "generated_at",
    "usage_count",
    "last_usage",
    "selected_for_phase2",
    "selected_for_phase2_source_updated_at",
)


@dataclass(frozen=True, slots=True)
class Stage1Row:
    """One ``stage1_outputs`` row exposed to the M20 rewrite verb."""

    thread_id: str
    source_updated_at: int
    rollout_summary: str
    rollout_slug: str | None
    generated_at: int
    selected_for_phase2: bool
    payload: dict[str, Any]


def open_readonly(db_path: Path) -> sqlite3.Connection:
    """Open ``db_path`` read-only via SQLite's ``mode=ro`` URI scheme.

    Also issues ``PRAGMA query_only=ON`` so even a stale handle that
    re-opens through ATTACH cannot mutate the operator-state file. The
    two together honor the canonical no-direct-write contract per
    [[no-direct-db-changes]].
    """
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    con.execute("PRAGMA query_only=ON")
    return con


def iter_stage1_rows(
    con: sqlite3.Connection,
    *,
    after_generated_at: int | None = None,
    only_selected_for_phase2: bool = True,
) -> Iterator[Stage1Row]:
    """Yield ``Stage1Row`` for stage1_outputs.

    Default filters to ``selected_for_phase2 = 1`` because only Codex-
    phase2-blessed rows carry tier-1 narrative weight per brief §3.0
    (G8 mitigation operates on the canonical-selected subset).

    ``after_generated_at`` enables monotonic-cursor advance if the
    caller wants to skip rows already processed. Returned in ascending
    ``generated_at`` order.
    """
    select_columns = ", ".join(_STAGE1_COLUMNS)
    clauses: list[str] = []
    params: list[Any] = []
    if only_selected_for_phase2:
        clauses.append("selected_for_phase2 = 1")
    if after_generated_at is not None:
        clauses.append("generated_at > ?")
        params.append(after_generated_at)
    where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
    sql = (
        f"SELECT {select_columns} FROM stage1_outputs{where} "
        f"ORDER BY generated_at"
    )
    for row in con.execute(sql, params):
        payload = dict(zip(_STAGE1_COLUMNS, row, strict=True))
        yield Stage1Row(
            thread_id=str(payload["thread_id"]),
            source_updated_at=int(payload["source_updated_at"]),
            rollout_summary=str(payload["rollout_summary"]),
            rollout_slug=(
                str(payload["rollout_slug"])
                if payload["rollout_slug"] is not None
                else None
            ),
            generated_at=int(payload["generated_at"]),
            selected_for_phase2=bool(payload["selected_for_phase2"]),
            payload=payload,
        )


def epoch_seconds_to_utc(epoch_seconds: int) -> datetime:
    """Decode a stage1_outputs INTEGER seconds-since-epoch column."""
    return datetime.fromtimestamp(epoch_seconds, tz=UTC)
