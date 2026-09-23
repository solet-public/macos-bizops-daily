#!/usr/bin/env python3
"""Smoke: MSG-03 — ``gc_terminal_role_messages`` retention rider.

Mirrors the INF-08 ``gc_terminal_completion_requests`` shape
(``ananta.services.inference_service.completion_request_queue``) for the
``core__agent_role_message`` table: aged terminal (``consumed`` or
``escalated``) rows are hard-deleted so the table does not grow unbounded,
while still-owed rows (neither flag set) and rows whose age cannot be read
are NEVER touched.

This is a DESTRUCTIVE code path (hard-delete via ``delete_records``), so the
discriminator that matters most is not the row count — it is the
never-delete-on-unknown-age contract in ``_terminal_row_aged``. Verified
red-first during development by temporarily mutating that branch to
``return True`` (reap unknown-age rows) instead of ``return False``: under
that mutation this smoke's assertion that ``rm-no-stamp-escalated`` survives
goes RED; restoring the real branch returns it to GREEN.

Six-row fixture, two queries (``consumed``, ``escalated``), covering:

  - an aged ``consumed`` row                    -> reaped
  - a fresh ``consumed`` row                    -> survives (not old enough)
  - an aged ``escalated`` row                   -> reaped
  - an aged row BOTH consumed AND escalated     -> reaped exactly ONCE (dedupe)
  - an ``escalated`` row with no timestamp      -> survives (never-delete-on-unknown-age)
  - an aged row that is neither (still owed)    -> survives (live work, not terminal)

Project policy: no pytest. Exits 0 on success, 1 on first failure.

Run with:

    .venv/bin/python3 plugins/agent_messaging_plugin/tests/role_message_retention_smoke.py
"""

from __future__ import annotations

import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT / "ananta" / "src"))
sys.path.insert(0, str(REPO_ROOT / "plugins" / "agent_messaging_plugin" / "src"))

from ananta.llm.agent_messaging.schema import COL_CONSUMED, COL_ESCALATED  # noqa: E402

from agent_messaging_plugin.role_message_retention import (  # noqa: E402
    gc_terminal_role_messages,
)

_passed = 0
_failed: list[str] = []

_COL_ID = "id"
_COL_IS_DELETED = "is_deleted"
_COL_UPDATED_AT = "updated_at"


def _check(condition: object, label: str) -> None:
    global _passed
    if condition:
        _passed += 1
        print(f"  PASS  {label}")
    else:
        _failed.append(label)
        print(f"  FAIL  {label}")


class _FakeRoleMessageStore:
    """Real-shape ``StateManagementInterface`` fake, scoped to this module's
    two calls — ActionResult envelopes, same as INF-08's own smoke fake."""

    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self.rows = rows
        self.deleted: list[str] = []

    def query_state(self, namespace: str, query: dict[str, object]) -> object:
        filters = query["filters"]
        terminal_column = COL_CONSUMED if COL_CONSUMED in filters else COL_ESCALATED
        records = [
            row for row in self.rows
            if row.get(terminal_column) is True and not row.get(_COL_IS_DELETED)
        ]
        return {"action_status": "completed", "data": {"records": records}}

    def delete_records(self, namespace: str, query: dict[str, object]) -> object:
        row_id = str(query["filters"][_COL_ID])
        self.deleted.append(row_id)
        for row in self.rows:
            if row.get(_COL_ID) == row_id:
                row[_COL_IS_DELETED] = 1
        return {"action_status": "completed", "data": {"result": {"deleted": 1}}}


def _make_rows() -> list[dict[str, Any]]:
    old = (datetime.now(UTC) - timedelta(days=2)).isoformat()
    fresh = datetime.now(UTC).isoformat()
    return [
        {
            _COL_ID: "rm-old-consumed", COL_CONSUMED: True, COL_ESCALATED: False,
            _COL_UPDATED_AT: old, _COL_IS_DELETED: 0,
        },
        {
            _COL_ID: "rm-fresh-consumed", COL_CONSUMED: True, COL_ESCALATED: False,
            _COL_UPDATED_AT: fresh, _COL_IS_DELETED: 0,
        },
        {
            _COL_ID: "rm-old-escalated", COL_CONSUMED: False, COL_ESCALATED: True,
            _COL_UPDATED_AT: old, _COL_IS_DELETED: 0,
        },
        {
            # both terminal predicates true — must be reaped exactly ONCE, not twice.
            _COL_ID: "rm-old-both", COL_CONSUMED: True, COL_ESCALATED: True,
            _COL_UPDATED_AT: old, _COL_IS_DELETED: 0,
        },
        {
            # never-delete-on-unknown-age: no _COL_UPDATED_AT at all.
            _COL_ID: "rm-no-stamp-escalated", COL_CONSUMED: False, COL_ESCALATED: True,
            _COL_IS_DELETED: 0,
        },
        {
            # still owed — neither flag set — live work, never terminal-reaped.
            _COL_ID: "rm-old-pending", COL_CONSUMED: False, COL_ESCALATED: False,
            _COL_UPDATED_AT: old, _COL_IS_DELETED: 0,
        },
    ]


def _case_reaps_only_aged_terminal_rows_deduped() -> None:
    print("\nCase: gc reaps aged consumed/escalated rows only, deduped")
    store = _FakeRoleMessageStore(_make_rows())

    reaped = gc_terminal_role_messages(store, terminal_gc_after_seconds=3600)

    _check(reaped == 3, f"reaped count == 3 (got {reaped})")
    _check(
        sorted(store.deleted) == ["rm-old-both", "rm-old-consumed", "rm-old-escalated"],
        f"deleted set == the three aged terminal rows, no duplicate delete call "
        f"for rm-old-both (got {sorted(store.deleted)})",
    )
    _check(
        "rm-fresh-consumed" not in store.deleted,
        "a fresh terminal row (not old enough) survives",
    )
    _check(
        "rm-no-stamp-escalated" not in store.deleted,
        "the never-delete-on-unknown-age contract: a terminal row with no "
        "updated_at survives — THIS is the discriminator a mutated "
        "_terminal_row_aged (missing-timestamp branch returning True instead "
        "of False) would flip red",
    )
    _check(
        "rm-old-pending" not in store.deleted,
        "a still-owed row (neither consumed nor escalated) is never reaped "
        "regardless of age — it is live work, not terminal",
    )


def main() -> int:
    print("Smoke: gc_terminal_role_messages terminal-row retention (MSG-03)")
    _case_reaps_only_aged_terminal_rows_deduped()

    print(f"\n{_passed} passed, {len(_failed)} failed")
    if _failed:
        for label in _failed:
            print(f"  - {label}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
