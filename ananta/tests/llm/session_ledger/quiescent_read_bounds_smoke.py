#!/usr/bin/env python3
"""Smoke: list_quiescent_sessions survives the read_state row cap (no pytest).

Proven live 2026-08-15. After the row cap deployed, the auto-summarize drain
failed on EVERY 10-minute cycle:

    LedgerRepositoryError: state-service session failed:
    query.unbounded_read_over_cap: read_state on table 'session' returned more
    than the 10000-row cap

`list_quiescent_sessions` issues three reads and all three break at scale, in
two distinct ways:

1. The CANDIDATE read is deliberately non-selective — every canonical, live,
   past-cutoff session — because the anti-join and the 1..50 LIMIT are applied
   downstream in `select_quiescent_sessions` (the flat filter grammar cannot
   express them). It genuinely wants the whole eligible set, so it carries the
   sanctioned `unbounded=True` consent.
2. The SUMMARY and SOURCE reads are membership reads whose result size is
   bounded by the candidate list, not by anything they state. Once candidates
   exceed the cap they are refused too. They are chunked, so each query is
   bounded BY CONSTRUCTION and needs no opt-in.

Pinned here so a future edit cannot silently drop either property and re-break
summarization on a mature deployment (27,139 session rows when this fired).

PURE UNIT: the real `SessionLedgerReadMixin` against a spy state service that
records every query. No DB, no platform, no inference.

Run:
    .venv/bin/python3 ananta/tests/llm/session_ledger/quiescent_read_bounds_smoke.py

Project policy: no pytest. Exits 0 on success, 1 on failure.
"""

from __future__ import annotations

import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

REPO_ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(REPO_ROOT / "ananta" / "src"))

from ananta.interfaces.state_management_interface import (  # noqa: E402
    StateManagementInterface,
)
from ananta.llm.session_ledger.read import SessionLedgerReadMixin  # noqa: E402
from ananta.services.state_service.read_bounds import MAX_READ_ROWS  # noqa: E402

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


class _SpyState:
    """Records every query_state call; serves rows per table."""

    def __init__(self, session_rows: list[dict[str, Any]]) -> None:
        self.session_rows = session_rows
        self.queries: list[dict[str, Any]] = []

    def query_state(self, namespace: str, query: dict[str, Any]) -> dict[str, Any]:
        del namespace
        self.queries.append(query)
        table = str(query.get("table", ""))
        if table.endswith("session"):
            rows: list[dict[str, Any]] = self.session_rows
        elif table.endswith("source"):
            requested = query.get("filters", {}).get("id", [])
            rows = [{"id": sid, "source_kind": "claude_code"} for sid in requested]
        else:  # summary table: nothing summarized yet
            rows = []
        return {"action_status": "completed", "data": {"records": rows}}


def _reader(spy: _SpyState) -> SessionLedgerReadMixin:
    return SessionLedgerReadMixin(
        cast(StateManagementInterface, spy),
        clock=lambda: datetime(2026, 8, 15, 12, 0, 0, tzinfo=UTC),
    )


def _for_table(queries: list[dict[str, Any]], suffix: str) -> list[dict[str, Any]]:
    return [q for q in queries if str(q.get("table", "")).endswith(suffix)]


def _check_candidate_read(session_q: list[dict[str, Any]]) -> None:
    _check(len(session_q) == 1, "the candidate read is issued once")
    _check(
        bool(session_q) and session_q[0].get("unbounded") is True,
        "the candidate read carries the sanctioned unbounded=True consent",
    )


def _check_membership_reads(
    summary_q: list[dict[str, Any]], source_q: list[dict[str, Any]], total: int,
) -> None:
    _check(len(summary_q) > 1, f"the summary membership read is CHUNKED ({len(summary_q)} reads)")
    _check(
        all(len(q["filters"]["session_id"]) <= MAX_READ_ROWS for q in summary_q),
        "every summary chunk stays within the row cap",
    )
    _check(
        sum(len(q["filters"]["session_id"]) for q in summary_q) == total,
        "every candidate id is looked up exactly once (no chunk dropped)",
    )
    _check(
        all(q["filters"].get("is_deleted") == 0 for q in summary_q),
        "chunking preserves the caller's other filters",
    )
    _check(
        all(q.get("unbounded") is not True for q in summary_q),
        "the chunked reads do NOT opt out of the cap — bounded by construction",
    )
    _check(
        bool(source_q) and all(len(q["filters"]["id"]) <= MAX_READ_ROWS for q in source_q),
        "the source membership read is bounded too",
    )


def main() -> int:
    print("=== quiescent_read_bounds_smoke ===")

    # A candidate set comfortably over the cap, spanning several chunks.
    total = MAX_READ_ROWS * 2 + 41
    rows = [
        {
            "id": f"s{i}",
            "source_id": f"src{i % 7}",
            "last_event_at": "2026-08-15 00:00:00",
            "summary_text": None,
        }
        for i in range(total)
    ]
    spy = _SpyState(rows)

    result = _reader(spy).list_quiescent_sessions(
        quiescence_minutes=10, limit=50, trivial_sentinel="__trivial__",
    )

    _check_candidate_read(_for_table(spy.queries, "session"))
    _check_membership_reads(
        _for_table(spy.queries, "summary"), _for_table(spy.queries, "source"), total,
    )

    # Behaviour, not just plumbing: the LIMIT is still honoured downstream.
    _check(len(result) <= 50, f"the caller's limit is still applied ({len(result)} rows)")
    _check(bool(result), "a non-empty eligible set still yields work to summarize")

    print(f"\n{_passed} passed, {len(_failed)} failed")
    if _failed:
        for label in _failed:
            print(f"  FAILED: {label}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
