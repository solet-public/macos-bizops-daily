#!/usr/bin/env python3
"""Offline smoke for the list_canonical_contributors junction-free migration (SQL-lockdown).

``list_canonical_contributors`` was the LAST ledger read on raw SQL (a CTE input-pair
resolve + an INNER-JOIN-``__source`` group projection + a computed-column ORDER BY).
The migration retires it onto a read-then-route over ``query_state``:

1. resolve the input's ``(vendor, external_session_id)`` group key;
2. read the live group by that pair;
3. resolve per-contributor ``source_kind`` via a ``__source`` ``= ANY`` read (NO
   ``is_deleted`` filter — faithful to the predicate-less INNER JOIN; a group row
   whose ``source_id`` is absent is dropped, the INNER-JOIN drop);
4. Python project to the 9-key shape + sort canonical-first then ``source_kind`` ASC;
5. feed the unchanged ``_build_canonical_contributors_result``.

A filter-HONORING ``_Shim`` (applies the flat-grammar filters + serializes timestamp
cells to ISO strings, mirroring the real ``query_state``/``_serialize_for_json`` path)
backs every test — a filter-blind / datetime-returning stub could not discriminate the
behaviors under test. Covers: canonical-input, sibling-input (C1 regression),
no-siblings, orphaned-canonical (C3 contract), the INNER-JOIN source-absent drop, the
soft-deleted-source retention, the Architect's datetime-RETURN-type catch (the projected
first/last_event_at must be a ``datetime`` — query_state serializes them to ISO strings,
the unchanged builder + downstream expect the datetime the raw ``_fetch_all`` returned),
the canonical-first / source_kind ASC order, and the input-not-found fail-loud raise.

Run::

    .venv/bin/python3 \\
      ananta/tests/llm/session_ledger/list_canonical_contributors_migration_smoke.py
"""

from __future__ import annotations

import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

_REPO_ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(_REPO_ROOT / "ananta" / "src"))

from ananta.llm.session_ledger.repository import (  # noqa: E402
    LedgerRepositoryError,
    SessionLedgerRepository,
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


def _matches(row: dict[str, Any], filters: dict[str, Any]) -> bool:
    """Faithful flat-grammar filter: scalar eq, list = ANY, is_null/is_not_null."""
    for col, cond in filters.items():
        val = row.get(col)
        if isinstance(cond, dict):
            op = cond.get("op")
            if op == "is_null" and val is not None:
                return False
            if op == "is_not_null" and val is None:
                return False
        elif isinstance(cond, list):
            if val not in cond:
                return False
        elif val != cond:
            return False
    return True


def _serialize(value: Any) -> Any:
    """Mirror the real query_state path: naive-UTC datetime cell → offset-less ISO."""
    if isinstance(value, datetime):
        return value.replace(tzinfo=None).isoformat()
    return value


class _Shim:
    """Filter-honoring query_state stand-in; serializes datetimes like the real path."""

    def __init__(self, tables: dict[str, list[dict[str, Any]]]) -> None:
        self._t = tables

    def query_state(self, namespace: str, query: dict[str, Any]) -> dict[str, Any]:
        flt = cast("dict[str, Any]", query.get("filters") or {})
        rows = [
            {k: _serialize(v) for k, v in r.items()}
            for r in self._t.get(str(query["table"]), [])
            if _matches(r, flt)
        ]
        return {"action_status": "completed", "data": {"records": rows},
                "actions": [], "error": None}


_NOW = datetime(2026, 6, 14, 12, 0, 0, tzinfo=UTC)
_LOCAL_TS = datetime(2026, 6, 14, 10, 0, 0, tzinfo=UTC)
_HISTORY_TS = datetime(2026, 6, 14, 11, 0, 0, tzinfo=UTC)


def _session(
    *, sid: str, source_id: str,
    ext: str = "ext_T1", vendor: str = "claude_code", canonical: str | None = None,
    first: datetime = _LOCAL_TS, last: datetime = _LOCAL_TS, event_count: int = 1,
    is_deleted: int = 0,
) -> dict[str, Any]:
    return {
        "id": sid, "source_id": source_id, "external_session_id": ext,
        "vendor": vendor, "vendor_session_label": sid, "project_path": "/p",
        "first_event_at": first, "last_event_at": last, "event_count": event_count,
        "canonical_external_session_id": canonical, "is_deleted": is_deleted,
    }


def _source(*, sid: str, kind: str, is_deleted: int = 0) -> dict[str, Any]:
    return {"id": sid, "source_kind": kind, "root_uri": f"/r/{sid}",
            "enabled": True, "is_deleted": is_deleted}


def _repo(tables: dict[str, list[dict[str, Any]]]) -> SessionLedgerRepository:
    return SessionLedgerRepository(state_service=cast("Any", _Shim(tables)), clock=lambda: _NOW)


def _canon_plus_sibling() -> dict[str, list[dict[str, Any]]]:
    return {
        "session": [
            _session(sid="les_canonical", source_id="src_local", canonical=None),
            _session(sid="les_sibling", source_id="src_history", canonical="ext_T1",
                     first=_HISTORY_TS, last=_HISTORY_TS, event_count=7),
        ],
        "source": [
            _source(sid="src_local", kind="claude_code_local"),
            _source(sid="src_history", kind="claude_code_history"),
        ],
    }


def test_canonical_input() -> None:
    result = _repo(_canon_plus_sibling()).list_canonical_contributors(session_id="les_canonical")
    _check(result["canonical_session_id"] == "les_canonical", "canonical input → canonical_session_id is the canonical")
    _check(result["canonical_external_session_id"] == "ext_T1", "canonical_external_session_id is the shared dedupe key")
    _check(result["vendor"] == "claude_code", "vendor reflected")
    _check(result["orphaned_canonical"] is False, "orphaned_canonical=False when canonical live")
    contributors = cast("list[dict[str, Any]]", result["contributors"])
    _check(len(contributors) == 2, f"two contributor rows (got {len(contributors)})")
    _check(
        [c["session_id"] for c in contributors] == ["les_canonical", "les_sibling"],
        "canonical-first then source_kind ASC order (local < history)",
    )


def test_sibling_input_locks_c1() -> None:
    """Sibling-input must surface the canonical + every sibling (Codex C1 regression lock)."""
    result = _repo(_canon_plus_sibling()).list_canonical_contributors(session_id="les_sibling")
    contributors = cast("list[dict[str, Any]]", result["contributors"])
    _check(any(c["is_canonical"] for c in contributors), "sibling-input still surfaces the canonical (C1 lock)")
    _check(result["canonical_session_id"] == "les_canonical", "canonical_session_id resolves to the canonical even from sibling input")
    _check(len(contributors) == 2, "full group from sibling input")


def test_no_siblings() -> None:
    tables = {
        "session": [_session(sid="les_solo", source_id="src_local", ext="ext_solo", canonical=None, event_count=10)],
        "source": [_source(sid="src_local", kind="claude_code_local")],
    }
    result = _repo(tables).list_canonical_contributors(session_id="les_solo")
    contributors = cast("list[dict[str, Any]]", result["contributors"])
    _check(len(contributors) == 1, "single-canonical group → one contributor")
    _check(result["canonical_session_id"] == "les_solo", "canonical_session_id is the solo canonical")
    _check(result["orphaned_canonical"] is False, "orphaned_canonical=False")


def test_orphaned_canonical_locks_c3() -> None:
    """Only a sibling lives (canonical soft-deleted) → C3 orphaned-canonical contract."""
    tables = {
        "session": [
            _session(sid="les_sibling_only", source_id="src_history", ext="ext_orphaned",
                     canonical="ext_orphaned", first=_HISTORY_TS, last=_HISTORY_TS),
            _session(sid="les_dead_canon", source_id="src_local", ext="ext_orphaned",
                     canonical=None, is_deleted=1),  # soft-deleted canonical → excluded by is_deleted:0
        ],
        "source": [
            _source(sid="src_history", kind="claude_code_history"),
            _source(sid="src_local", kind="claude_code_local"),
        ],
    }
    result = _repo(tables).list_canonical_contributors(session_id="les_sibling_only")
    _check(result["canonical_session_id"] is None, "orphaned: canonical_session_id=None (no live is_canonical row)")
    _check(result["orphaned_canonical"] is True, "orphaned_canonical=True discriminator (C3 contract)")
    _check(result["canonical_external_session_id"] == "ext_orphaned", "orphaned: external id still surfaced")
    contributors = cast("list[dict[str, Any]]", result["contributors"])
    _check(len(contributors) == 1 and contributors[0]["session_id"] == "les_sibling_only", "only the live sibling contributes")


def test_inner_join_source_absent_row_dropped() -> None:
    """A group row whose source_id has no __source row is dropped (the INNER-JOIN drop)."""
    tables = {
        "session": [
            _session(sid="les_canonical", source_id="src_local", canonical=None),
            _session(sid="les_orphan_src", source_id="src_GONE", canonical="ext_T1",
                     first=_HISTORY_TS, last=_HISTORY_TS),
        ],
        "source": [_source(sid="src_local", kind="claude_code_local")],  # src_GONE absent
    }
    result = _repo(tables).list_canonical_contributors(session_id="les_canonical")
    contributors = cast("list[dict[str, Any]]", result["contributors"])
    _check(
        [c["session_id"] for c in contributors] == ["les_canonical"],
        "the row pointing at an absent source is dropped (faithful INNER-JOIN drop)",
    )


def test_soft_deleted_source_retained() -> None:
    """A soft-deleted __source still contributes its kind (no is_deleted filter on the source read)."""
    tables = {
        "session": [_session(sid="les_canonical", source_id="src_soft", canonical=None)],
        "source": [_source(sid="src_soft", kind="claude_code_local", is_deleted=1)],
    }
    result = _repo(tables).list_canonical_contributors(session_id="les_canonical")
    contributors = cast("list[dict[str, Any]]", result["contributors"])
    _check(
        len(contributors) == 1 and contributors[0]["source_kind"] == "claude_code_local",
        "soft-deleted source still resolves source_kind (predicate-less INNER JOIN faithful)",
    )


def test_datetime_return_type_parsed_back() -> None:
    """The Architect's catch: projected first/last_event_at must be datetime, not the ISO string.

    query_state serializes the naive-UTC timestamp cells to offset-less ISO strings; the
    unchanged _build_canonical_contributors_result + its downstream consumer expect the
    datetime the raw _fetch_all returned. The fold parses them back to NAIVE datetime.
    """
    result = _repo(_canon_plus_sibling()).list_canonical_contributors(session_id="les_canonical")
    contributors = cast("list[dict[str, Any]]", result["contributors"])
    first = contributors[0]["first_event_at"]
    last = contributors[0]["last_event_at"]
    _check(isinstance(first, datetime), f"first_event_at parsed back to datetime, not ISO str (got {type(first).__name__})")
    _check(isinstance(last, datetime), f"last_event_at parsed back to datetime, not ISO str (got {type(last).__name__})")
    _check(
        isinstance(first, datetime) and first.tzinfo is None,
        "first_event_at is NAIVE (matches the old raw _fetch_all psycopg naive datetime)",
    )
    _check(
        isinstance(first, datetime) and first == _LOCAL_TS.replace(tzinfo=None),
        "first_event_at round-trips to the planted instant",
    )


def test_input_not_found_raises() -> None:
    raised = False
    try:
        _repo(_canon_plus_sibling()).list_canonical_contributors(session_id="les_DOES_NOT_EXIST")
    except LedgerRepositoryError:
        raised = True
    _check(raised, "input id resolving to no live row → LedgerRepositoryError (fail-loud contract preserved)")


def main() -> int:
    print("=== list_canonical_contributors_migration smoke ===")
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            print(name)
            fn()
    print(f"\n{_passed} passed, {len(_failed)} failed")
    for label in _failed:
        print(f"  FAILED: {label}")
    return 1 if _failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
