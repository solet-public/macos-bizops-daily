#!/usr/bin/env python3
"""Offline smoke for the ``list_quiescent_sessions`` read-then-route migration.

SQL-lockdown: the M6 auto-summarize feed retires off a single SQL statement
(canonical + cutoff filter + ``NOT EXISTS __summary`` anti-join +
``(summary_text IS NULL OR != sentinel)`` disjunction + INNER JOIN ``__source``
+ ORDER BY ``last_event_at`` ASC LIMIT) onto three ``query_state`` reads + a
Python fold (:func:`select_quiescent_sessions`).

This smoke runs the REAL ``SessionLedgerRepository.list_quiescent_sessions``
through a FILTER-HONORING shim (so the candidate/summary/source ``query_state``
shapes + the delegator's tz-aware-clock cutoff normalization are exercised in CI
— the live smoke that hits real Postgres is env-gated/skipped) plus focused
direct ``select_quiescent_sessions`` assertions:

* the cutoff (``last_event_at <= now - quiescence``, tz-aware clock → naive UTC),
  canonical-only (``canonical_external_session_id IS NULL``), and ``is_deleted=0``
  filters select the right candidates;
* a session WITH a live ``__summary`` row is EXCLUDED (idempotency seam);
* a session whose ``summary_text`` is the trivial sentinel is EXCLUDED;
* a session whose source row is ABSENT is DROPPED (the INNER JOIN); a present
  source contributes ``source_kind``;
* results are newest-quiescent-first (DESC — 2026-06-30 recency change, an
  intentional divergence from the retired SQL's ASC so recent sessions become
  searchable first) and capped at ``limit``;
* no candidates → ``[]`` (no summary/source reads needed).

Run:
    .venv/bin/python3 ananta/tests/llm/session_ledger/quiescent_fold_smoke.py
"""

from __future__ import annotations

import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

REPO_ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(REPO_ROOT / "ananta" / "src"))

from ananta.llm.session_ledger.read_support import (  # noqa: E402
    select_quiescent_sessions,
)
from ananta.llm.session_ledger.repository import (  # noqa: E402
    SessionLedgerRepository,
)
from ananta.llm.session_ledger.schema import (  # noqa: E402
    TABLE_SESSION,
    TABLE_SOURCE,
    TABLE_SUMMARY,
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


_SENTINEL = "[trivial]"
_CLOCK = datetime(2026, 6, 15, 12, 0, 0, tzinfo=UTC)  # tz-aware (production shape)


def _match_range(op: str, val: object, target: object) -> bool:
    """Gap-A range comparison (``lt`` / ``lte`` / ``gt`` / ``gte``).

    A NULL value fails (SQL semantics). Datetime row values are parsed from
    their ISO-string read form before comparison, as Postgres compares the
    typed column.
    """
    if val is None:
        return False
    left = datetime.fromisoformat(str(val)) if isinstance(target, datetime) else val
    if op == "lte":
        return left <= target  # type: ignore[operator]
    if op == "lt":
        return left < target  # type: ignore[operator]
    if op == "gte":
        return left >= target  # type: ignore[operator]
    return left > target  # type: ignore[operator]  # "gt"


def _match_dict_cond(val: object, cond: dict[str, Any]) -> bool:
    """Apply one dict-form condition (``is_null`` / ``is_not_null`` / range).

    An unrecognized op imposes no constraint (matches), faithful to the
    original fall-through.
    """
    op = cond.get("op")
    if op == "is_null":
        return val is None
    if op == "is_not_null":
        return val is not None
    if op in ("lt", "lte", "gt", "gte"):
        return _match_range(str(op), val, cond["value"])
    return True


def _matches(row: dict[str, Any], filters: dict[str, Any]) -> bool:
    """Faithful in-memory application of the query_state filter grammar.

    Mirrors the SQL the shim stands in for: scalar equality, ``= ANY`` (list),
    and the dict-form conditions (null-checks + Gap-A range ops) delegated to
    :func:`_match_dict_cond`.
    """
    for col, cond in filters.items():
        val = row.get(col)
        if isinstance(cond, dict):
            if not _match_dict_cond(val, cond):
                return False
        elif isinstance(cond, list):
            if val not in cond:
                return False
        elif val != cond:
            return False
    return True


class _FilterShim:
    """query_state stand-in that HONORS the filter grammar over planted rows."""

    def __init__(self, tables: dict[str, list[dict[str, Any]]]) -> None:
        self._tables = tables

    def query_state(self, namespace: str, query: dict[str, Any]) -> dict[str, Any]:
        _ = namespace  # shim keys off query["table"]; namespace mirrors the real signature
        filters = cast("dict[str, Any]", query.get("filters") or {})
        rows = [
            dict(row)
            for row in self._tables.get(str(query["table"]), [])
            if _matches(row, filters)
        ]
        return {"action_status": "completed", "data": {"records": rows},
                "actions": [], "error": None}


def _session(
    *,
    sid: str,
    source_id: str,
    last_event_at: str,
    canonical: str | None = None,
    summary_text: str | None = None,
    is_deleted: int = 0,
) -> dict[str, Any]:
    return {
        "id": sid,
        "source_id": source_id,
        "external_session_id": f"ext-{sid}",
        "vendor": "claude_code",
        "vendor_session_label": sid,
        "project_path": f"/p/{sid}",
        "first_event_at": last_event_at,
        "last_event_at": last_event_at,
        "event_count": 3,
        "summary_text": summary_text,
        "canonical_external_session_id": canonical,
        "is_deleted": is_deleted,
    }


# Candidates (canonical, live, last_event_at <= cutoff=11:59) + the discriminators.
_SESSIONS: list[dict[str, Any]] = [
    _session(sid="s0", source_id="src-A", last_event_at="2026-06-10T00:00:00"),  # survives (older)
    _session(sid="s1", source_id="src-A", last_event_at="2026-06-12T00:00:00"),  # survives (newer → sorts first)
    _session(sid="s2", source_id="src-A", last_event_at="2026-06-11T00:00:00"),  # has __summary → excluded
    _session(sid="s3", source_id="src-A", last_event_at="2026-06-11T12:00:00",
             summary_text=_SENTINEL),  # sentinel → excluded
    _session(sid="s6", source_id="src-GONE", last_event_at="2026-06-09T00:00:00"),  # source absent → dropped
    _session(sid="s4", source_id="src-A", last_event_at="2026-06-08T00:00:00",
             canonical="ext-shared"),  # sibling → not a candidate
    _session(sid="s5", source_id="src-A", last_event_at="2026-06-15T11:59:30"),  # after cutoff → not a candidate
    _session(sid="s7", source_id="src-A", last_event_at="2026-06-07T00:00:00",
             is_deleted=1),  # soft-deleted → not a candidate
]
_SUMMARIES: list[dict[str, Any]] = [
    {"id": "sum-s2", "session_id": "s2", "is_deleted": 0},
]
_SOURCES: list[dict[str, Any]] = [
    {"id": "src-A", "source_kind": "claude_code", "is_deleted": 0},
    # src-GONE intentionally absent → s6 is INNER-JOIN-dropped.
]


def _repo(*, sessions: list[dict[str, Any]] | None = None) -> SessionLedgerRepository:
    shim = _FilterShim(
        {
            TABLE_SESSION: sessions if sessions is not None else _SESSIONS,
            TABLE_SUMMARY: _SUMMARIES,
            TABLE_SOURCE: _SOURCES,
        }
    )
    return SessionLedgerRepository(
        state_service=cast("Any", shim), clock=lambda: _CLOCK,
    )


def test_real_method_read_then_route() -> None:
    rows = _repo().list_quiescent_sessions(
        quiescence_minutes=1, limit=50, trivial_sentinel=_SENTINEL,
    )
    ids = [str(r["id"]) for r in rows]
    _check(
        ids == ["s1", "s0"],
        "real list_quiescent_sessions(): only un-summarized, non-sentinel, "
        "canonical, past-cutoff, source-present sessions — newest-first",
    )
    _check(
        all(s not in ids for s in ("s2", "s3", "s4", "s5", "s6", "s7")),
        "excluded: has-summary (s2) / sentinel (s3) / sibling (s4) / recent (s5) "
        "/ source-absent (s6) / soft-deleted (s7)",
    )
    if rows:
        _check(
            rows[0]["source_kind"] == "claude_code"
            and rows[0].get("summary_text") is None,
            "survivor carries projected source_kind + the session's summary_text",
        )


def test_limit_truncates_newest_first() -> None:
    rows = _repo().list_quiescent_sessions(
        quiescence_minutes=1, limit=1, trivial_sentinel=_SENTINEL,
    )
    _check(
        [str(r["id"]) for r in rows] == ["s1"],
        "limit=1 returns the single newest-quiescent survivor (s1)",
    )


def test_no_candidates_returns_empty() -> None:
    # A clock far in the past → cutoff precedes every session → no candidate.
    shim = _FilterShim(
        {TABLE_SESSION: _SESSIONS, TABLE_SUMMARY: _SUMMARIES, TABLE_SOURCE: _SOURCES},
    )
    repo = SessionLedgerRepository(
        state_service=cast("Any", shim),
        clock=lambda: datetime(2020, 1, 1, 0, 0, 0, tzinfo=UTC),
    )
    rows = repo.list_quiescent_sessions(
        quiescence_minutes=1, limit=50, trivial_sentinel=_SENTINEL,
    )
    _check(rows == [], "no candidates past the cutoff → [] (short-circuits)")


def test_soft_deleted_source_still_summarized() -> None:
    # Faithful to the original INNER JOIN (NO src.is_deleted filter): a candidate
    # under a SOFT-DELETED but present source is still returned, with source_kind
    # from the deleted source row. Guards against accidentally adding is_deleted=0
    # to the __source read — which would silently DROP these sessions.
    shim = _FilterShim(
        {
            TABLE_SESSION: [
                _session(sid="sd", source_id="src-DEL",
                         last_event_at="2026-06-05T00:00:00"),
            ],
            TABLE_SUMMARY: [],
            TABLE_SOURCE: [{"id": "src-DEL", "source_kind": "codex", "is_deleted": 1}],
        }
    )
    repo = SessionLedgerRepository(state_service=cast("Any", shim), clock=lambda: _CLOCK)
    rows = repo.list_quiescent_sessions(
        quiescence_minutes=1, limit=50, trivial_sentinel=_SENTINEL,
    )
    _check(
        [str(r["id"]) for r in rows] == ["sd"]
        and bool(rows)
        and rows[0]["source_kind"] == "codex",
        "candidate under a soft-deleted (but present) source is still returned "
        "(faithful: no is_deleted filter on the __source read)",
    )


def test_pure_helper_exclusions() -> None:
    candidates = [
        _session(sid="a", source_id="src-A", last_event_at="2026-06-03T00:00:00"),
        _session(sid="b", source_id="src-A", last_event_at="2026-06-01T00:00:00"),
        _session(sid="c", source_id="src-A", last_event_at="2026-06-02T00:00:00",
                 summary_text=_SENTINEL),
        _session(sid="d", source_id="src-GONE", last_event_at="2026-06-01T00:00:00"),
    ]
    out = select_quiescent_sessions(
        candidates,
        summarized_session_ids={"a"},
        source_kind_by_id={"src-A": "claude_code"},
        trivial_sentinel=_SENTINEL,
        limit=50,
    )
    _check(
        [str(r["id"]) for r in out] == ["b"],
        "pure helper: a(summarized)/c(sentinel)/d(source-absent) excluded → [b]",
    )


def main() -> int:
    print("=== quiescent_fold_smoke ===")
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            print(name)
            fn()
    print(f"\n{_passed} passed, {len(_failed)} failed")
    for label in _failed:
        print(f"  FAILED: {label}")
    return 1 if _failed else 0


if __name__ == "__main__":
    sys.exit(main())
