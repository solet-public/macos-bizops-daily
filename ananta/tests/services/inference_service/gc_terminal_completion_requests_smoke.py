#!/usr/bin/env python3
"""Smoke: INF-08 — ``gc_terminal_completion_requests`` retention rider.

Mirrors the INF-06 forwarded-vertex terminal-row GC shape
(``agent_messaging_plugin.forwarded_vertex_reconcile.gc_terminal_rows``) for
the INF-02 ``core__inference_completion_request`` durable queue: aged
``served``/``failed`` rows are hard-deleted so the table does not grow
unbounded, while ``pending`` rows (live work) and rows whose age cannot be
read are NEVER touched.

This is a DESTRUCTIVE code path (hard-delete via ``delete_records``), so the
discriminator that matters most is not the row count — it is the
never-delete-on-unknown-age contract in ``_terminal_row_aged``. Verified
red-first during development by temporarily mutating that function's
missing-``updated_at`` branch to ``return True`` (reap unknown-age rows)
instead of ``return False``: under that mutation this smoke's assertion that
``icr-no-stamp-failed`` survives goes RED (it gets reaped alongside the two
genuinely-aged rows); restoring the real "never reap unknown age" branch
returns it to GREEN. That mutation is not re-run here (a smoke asserts
against the shipped function, it does not patch it) — this file exists so
the assertion that catches it stays live in the gate battery instead of only
in the ad-hoc harness that proved it once.

Five-row fixture, one query per status (``served``, ``failed``), covering:

  - an aged ``served`` row            -> reaped
  - a fresh ``served`` row            -> survives (not old enough)
  - an aged ``failed`` row            -> reaped
  - a ``failed`` row with no timestamp -> survives (never-delete-on-unknown-age)
  - an aged ``pending`` row           -> survives (live work, not terminal)

Project policy: no pytest. Exits 0 on success, 1 on first failure.

Run with:

    .venv/bin/python3 ananta/tests/services/inference_service/gc_terminal_completion_requests_smoke.py
"""

from __future__ import annotations

import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(REPO_ROOT / "ananta" / "src"))

from ananta.services.inference_service.completion_request_queue import (  # noqa: E402
    COL_UPDATED_AT,
    gc_terminal_completion_requests,
)
from ananta.services.inference_service.completion_request_schema import (  # noqa: E402
    COL_IS_DELETED,
    COL_REQUEST_ID,
    COL_STATUS,
    STATUS_FAILED,
    STATUS_PENDING,
    STATUS_SERVED,
)

_passed = 0
_failed: list[str] = []


def _check(condition: object, label: str) -> None:
    global _passed
    if condition:
        _passed += 1
        print(f"  PASS  {label}")
    else:
        _failed.append(label)
        print(f"  FAIL  {label}")


class _FakeCompletionRequestStore:
    """Real-shape ``CompletionRequestStore`` fake — ActionResult envelopes."""

    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self.rows = rows
        self.deleted: list[str] = []

    def write_state(self, namespace: str, data: dict[str, object]) -> object:
        raise AssertionError("write_state not expected in this smoke")

    def query_state(self, namespace: str, filters: dict[str, object]) -> object:
        status = filters["filters"][COL_STATUS]
        records = [
            row for row in self.rows
            if row[COL_STATUS] == status and not row.get(COL_IS_DELETED)
        ]
        return {"action_status": "completed", "data": {"records": records}}

    def update_state(
        self, namespace: str, query: dict[str, object], updates: dict[str, object],
    ) -> object:
        raise AssertionError("update_state not expected in this smoke")

    def delete_records(self, namespace: str, query: dict[str, object]) -> object:
        request_id = str(query["filters"][COL_REQUEST_ID])
        self.deleted.append(request_id)
        for row in self.rows:
            if row[COL_REQUEST_ID] == request_id:
                row[COL_IS_DELETED] = 1
        return {"action_status": "completed", "data": {"result": {"deleted": 1}}}


def _make_rows() -> list[dict[str, Any]]:
    old = (datetime.now(UTC) - timedelta(days=2)).isoformat()
    fresh = datetime.now(UTC).isoformat()
    return [
        {
            COL_REQUEST_ID: "icr-old-served", COL_STATUS: STATUS_SERVED,
            COL_UPDATED_AT: old, COL_IS_DELETED: 0,
        },
        {
            COL_REQUEST_ID: "icr-fresh-served", COL_STATUS: STATUS_SERVED,
            COL_UPDATED_AT: fresh, COL_IS_DELETED: 0,
        },
        {
            COL_REQUEST_ID: "icr-old-failed", COL_STATUS: STATUS_FAILED,
            COL_UPDATED_AT: old, COL_IS_DELETED: 0,
        },
        {
            # never-delete-on-unknown-age: no COL_UPDATED_AT at all.
            COL_REQUEST_ID: "icr-no-stamp-failed", COL_STATUS: STATUS_FAILED,
            COL_IS_DELETED: 0,
        },
        {
            # live work, never terminal-reaped regardless of age.
            COL_REQUEST_ID: "icr-old-pending", COL_STATUS: STATUS_PENDING,
            COL_UPDATED_AT: old, COL_IS_DELETED: 0,
        },
    ]


def _case_reaps_only_aged_terminal_rows() -> None:
    print("\nCase: gc reaps aged served/failed rows only")
    store = _FakeCompletionRequestStore(_make_rows())

    reaped = gc_terminal_completion_requests(store, terminal_gc_after_seconds=3600)

    _check(reaped == 2, f"reaped count == 2 (got {reaped})")
    _check(
        sorted(store.deleted) == ["icr-old-failed", "icr-old-served"],
        f"deleted set == {{icr-old-failed, icr-old-served}} (got {sorted(store.deleted)})",
    )
    _check(
        "icr-fresh-served" not in store.deleted,
        "a fresh terminal row (not old enough) survives",
    )
    _check(
        "icr-no-stamp-failed" not in store.deleted,
        "the never-delete-on-unknown-age contract: a terminal row with no "
        "updated_at survives — THIS is the discriminator a mutated "
        "_terminal_row_aged (missing-timestamp branch returning True instead "
        "of False) would flip red",
    )
    _check(
        "icr-old-pending" not in store.deleted,
        "a pending row (live work, not terminal) is never reaped regardless of age",
    )


def main() -> int:
    print("Smoke: gc_terminal_completion_requests terminal-row retention (INF-08)")
    _case_reaps_only_aged_terminal_rows()

    print(f"\n{_passed} passed, {len(_failed)} failed")
    if _failed:
        for label in _failed:
            print(f"  - {label}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
