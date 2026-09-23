#!/usr/bin/env python3
"""Offline smoke for the ``list_events_by_source_window`` denormalize migration.

SQL-lockdown Slice 7 (Architect-ruled denormalize): the verb's 3-table JOIN
(event → session → source) retires onto a single-table ``query_ordered`` read
over ``__event`` using the ``session_vendor`` + ``source_kind`` columns
denormalized at append time. The two-sided ``event_at`` window does NOT fit the
one-condition-per-column filter grammar, so the read carries only the ``until``
upper bound (the DESC anchor) in the query and applies ``since`` as a Python
post-filter (:func:`select_events_in_window`).

This smoke runs through a FILTER-HONORING shim (faithful ``query_ordered`` /
``query_state`` / ``update_state`` over planted rows — order_by sort, limit,
auto ``is_deleted=0`` for query_ordered, the dict-op grammar) so the REAL
``repo.list_events_by_source_window`` + ``repo.backfill_event_source_denormalization``
are exercised in CI, plus focused pure ``select_events_in_window`` assertions:

* the post-filter faithfulness: ``since``/``until`` boundary inclusivity, and —
  the discriminator — that when the in-window count is BELOW the limit the page
  is diluted with ``< since`` rows that are correctly dropped (so the result
  equals the original ``BETWEEN since AND until ORDER BY event_at DESC LIMIT``);
* ``source_kind`` / ``vendor`` single-table equality filters;
* a tz-aware ``since``/``until`` is normalized naive-vs-naive (F1 seam — no
  silent-0 / TypeError) by running the REAL method with a tz-aware clock;
* the limit clamp narrows >100 to 100 (no fail-loud — the verb pre-clamps);
* the backfill fills NULL ``session_vendor``/``source_kind`` per session, is
  idempotent, and fails loud on a session whose source does not resolve.

Run:
    .venv/bin/python3 ananta/tests/llm/session_ledger/events_window_fold_smoke.py
"""

from __future__ import annotations

import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

REPO_ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(REPO_ROOT / "ananta" / "src"))

from ananta.llm.session_ledger.base import LedgerRepositoryError  # noqa: E402
from ananta.llm.session_ledger.repository import (  # noqa: E402
    SessionLedgerRepository,
)
from ananta.llm.session_ledger.schema import (  # noqa: E402
    TABLE_EVENT,
    TABLE_SESSION,
    TABLE_SOURCE,
)
from ananta.llm.session_ledger.search import (  # noqa: E402
    _event_at_naive,
    select_events_in_window,
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


_CLOCK = datetime(2026, 6, 22, 12, 0, 0, tzinfo=UTC)  # tz-aware (production shape)


def _cmp_operand(value: object, target: object) -> object:
    """Parse a row value for a range compare: datetime target → parse ISO; else raw."""
    if isinstance(target, datetime):
        return datetime.fromisoformat(str(value))
    return value


def _matches(row: dict[str, Any], filters: dict[str, Any]) -> bool:
    """Faithful in-memory application of the flat filter grammar.

    Scalar equality, ``= ANY`` (list), ``is_null`` / ``is_not_null``, and the
    Gap-A range ops. A datetime range target parses the row value from its ISO
    form before comparison (Postgres compares the typed column); a non-datetime
    target (e.g. a TEXT ``id`` keyset cursor) compares raw. NULL fails a range op.
    """
    for col, cond in filters.items():
        val = row.get(col)
        if isinstance(cond, dict):
            op = cond.get("op")
            if op == "is_null" and val is not None:
                return False
            if op == "is_not_null" and val is None:
                return False
            if op in ("lt", "lte", "gt", "gte"):
                if val is None:
                    return False
                target = cond["value"]
                left = _cmp_operand(val, target)
                if op == "lte" and not left <= target:  # type: ignore[operator]
                    return False
                if op == "lt" and not left < target:  # type: ignore[operator]
                    return False
                if op == "gte" and not left >= target:  # type: ignore[operator]
                    return False
                if op == "gt" and not left > target:  # type: ignore[operator]
                    return False
        elif isinstance(cond, list):
            if val not in cond:
                return False
        elif val != cond:
            return False
    return True


def _sort_key(value: object) -> str:
    return "" if value is None else str(value)


class _StateShim:
    """query_ordered / query_state / update_state stand-in honoring the grammar.

    ``query_ordered`` filters → auto-drops ``is_deleted != 0`` (the primitive's
    default the read relies on) → multi-key order_by sort → limit; it also
    records the last limit it received so the clamp can be asserted.
    ``query_state`` honors filters only (NO auto is_deleted — callers pass it).
    ``update_state`` applies filters + sets the update dict in place, returning
    the rows-affected count in a faithful ``data.result.updated`` envelope.
    """

    def __init__(self, tables: dict[str, list[dict[str, Any]]]) -> None:
        self._tables = tables
        self.last_ordered_limit: int | None = None

    def _records(self, rows: list[dict[str, Any]]) -> dict[str, Any]:
        return {"action_status": "completed", "data": {"records": rows},
                "actions": [], "error": None}

    def query_state(self, namespace: str, query: dict[str, Any]) -> dict[str, Any]:
        filters = cast("dict[str, Any]", query.get("filters") or {})
        rows = [dict(r) for r in self._tables.get(str(query["table"]), [])
                if _matches(r, filters)]
        return self._records(rows)

    def query_ordered(self, namespace: str, query: dict[str, Any]) -> dict[str, Any]:
        self.last_ordered_limit = int(query["limit"])
        filters = cast("dict[str, Any]", query.get("filters") or {})
        rows = [dict(r) for r in self._tables.get(str(query["table"]), [])
                if int(r.get("is_deleted", 0)) == 0 and _matches(r, filters)]
        for col, direction in reversed(cast("list[list[str]]", query["order_by"])):
            rows.sort(key=lambda r: _sort_key(r.get(col)), reverse=(direction == "desc"))
        return self._records(rows[: self.last_ordered_limit])

    def update_state(
        self, namespace: str, query: dict[str, Any], updates: dict[str, Any],
    ) -> dict[str, Any]:
        filters = cast("dict[str, Any]", query.get("filters") or {})
        updated = 0
        for row in self._tables.get(str(query["table"]), []):
            if _matches(row, filters):
                row.update(updates)
                updated += 1
        return {"action_status": "completed", "data": {"result": {"updated": updated}},
                "actions": [], "error": None}


def _event(
    *,
    eid: str,
    session_id: str,
    event_at: str,
    source_kind: str | None = "codex_local",
    session_vendor: str | None = "codex",
    role: str = "user",
    is_deleted: int = 0,
) -> dict[str, Any]:
    return {
        "id": eid,
        "session_id": session_id,
        "sequence": int(eid.rsplit("-", 1)[-1]) if eid.rsplit("-", 1)[-1].isdigit() else 0,
        "event_type": "message",
        "role": role,
        "content_text": f"body-{eid}",
        "content_json": None,
        "content_blob_id": None,
        "event_at": event_at,
        "session_vendor": session_vendor,
        "source_kind": source_kind,
        "is_deleted": is_deleted,
    }


def _repo(tables: dict[str, list[dict[str, Any]]]) -> tuple[SessionLedgerRepository, _StateShim]:
    shim = _StateShim(tables)
    repo = SessionLedgerRepository(state_service=cast("Any", shim), clock=lambda: _CLOCK)
    return repo, shim


# ── pure select_events_in_window ─────────────────────────────────────────────

def _row(eid: str, event_at: str, **kw: Any) -> dict[str, Any]:
    return _event(eid=eid, session_id="s", event_at=event_at, **kw)


def test_pure_since_filter_and_projection() -> None:
    # A DESC page (as query_ordered would return): newest first.
    page = [
        _row("e-10", "2026-06-22T11:00:00"),
        _row("e-9", "2026-06-22T10:00:00"),   # == since → kept
        _row("e-8", "2026-06-22T09:59:59"),   # < since → dropped
        _row("e-7", "2026-06-22T09:00:00"),   # < since → dropped
    ]
    out = select_events_in_window(page, since_naive=datetime(2026, 6, 22, 10, 0, 0))
    _check(
        [r["event_id"] for r in out] == ["e-10", "e-9"],
        "pure: since drops the < since suffix, keeps >= since (boundary inclusive)",
    )
    first = out[0]
    _check(
        set(first) == {"event_id", "session_id", "sequence", "event_at", "role",
                       "content_text", "session_vendor", "source_kind"}
        and first["event_id"] == "e-10"
        and first["session_vendor"] == "codex"
        and first["source_kind"] == "codex_local",
        "pure: projection renames id→event_id + carries the 8-key envelope",
    )


def test_pure_since_none_passthrough() -> None:
    page = [_row("e-2", "2026-06-22T11:00:00"), _row("e-1", "2026-06-22T08:00:00")]
    out = select_events_in_window(page, since_naive=None)
    _check(
        [r["event_id"] for r in out] == ["e-2", "e-1"],
        "pure: since_naive=None applies no lower bound (whole page, in order)",
    )


def test_event_at_naive_tz_handling() -> None:
    naive = _event_at_naive("2026-06-22T10:00:00")
    aware = _event_at_naive(datetime(2026, 6, 22, 10, 0, 0, tzinfo=UTC))
    passthrough = _event_at_naive(datetime(2026, 6, 22, 10, 0, 0))
    _check(
        naive == datetime(2026, 6, 22, 10, 0, 0) and naive.tzinfo is None,
        "_event_at_naive: naive ISO string → naive datetime",
    )
    _check(
        aware == datetime(2026, 6, 22, 10, 0, 0) and aware.tzinfo is None,
        "_event_at_naive: tz-aware datetime → converted to naive UTC",
    )
    _check(passthrough.tzinfo is None, "_event_at_naive: naive datetime passes through")


# ── real list_events_by_source_window via the shim ──────────────────────────

def _window_events() -> list[dict[str, Any]]:
    return [
        _event(eid="ev-after-30", session_id="se", event_at="2026-06-22T12:30:00"),
        _event(eid="ev-atuntil-20", session_id="se", event_at="2026-06-22T12:00:00"),
        _event(eid="ev-mid-15", session_id="se", event_at="2026-06-22T11:00:00"),
        _event(eid="ev-atsince-10", session_id="se", event_at="2026-06-22T10:00:00"),
        _event(eid="ev-below-09", session_id="se", event_at="2026-06-22T09:00:00"),
        _event(eid="ev-below-08", session_id="se", event_at="2026-06-22T08:00:00"),
    ]


def test_real_window_boundary_inclusivity() -> None:
    repo, _ = _repo({TABLE_EVENT: _window_events()})
    rows = repo.list_events_by_source_window(
        source_kind="codex_local",
        since=datetime(2026, 6, 22, 10, 0, 0, tzinfo=UTC),  # tz-aware (F1 seam)
        until=datetime(2026, 6, 22, 12, 0, 0, tzinfo=UTC),
        limit=10,
    )
    _check(
        [r["event_id"] for r in rows] == ["ev-atuntil-20", "ev-mid-15", "ev-atsince-10"],
        "real: [since, until] inclusive both ends; > until + < since excluded; DESC",
    )


def test_real_window_dilution_faithful() -> None:
    # in-window count (2: 11:00, 12:00) < limit (3) → the newest-3 <=until page is
    # diluted with the 10:00 row (< since), which the post-filter drops. Result must
    # equal the original BETWEEN[11:00,12:00] DESC LIMIT 3 = [12:00, 11:00].
    repo, _ = _repo({TABLE_EVENT: _window_events()})
    rows = repo.list_events_by_source_window(
        source_kind="codex_local",
        since=datetime(2026, 6, 22, 11, 0, 0, tzinfo=UTC),
        until=datetime(2026, 6, 22, 12, 0, 0, tzinfo=UTC),
        limit=3,
    )
    _check(
        [r["event_id"] for r in rows] == ["ev-atuntil-20", "ev-mid-15"],
        "real: in-window<limit → page diluted with <since rows, suffix dropped "
        "(faithful to BETWEEN ... DESC LIMIT)",
    )


def test_real_window_source_kind_and_vendor_filters() -> None:
    events = [
        _event(eid="cx-1", session_id="a", event_at="2026-06-22T11:00:00",
               source_kind="codex_local", session_vendor="codex"),
        _event(eid="cc-1", session_id="b", event_at="2026-06-22T11:30:00",
               source_kind="claude_code_local", session_vendor="claude_code"),
        _event(eid="cx-2", session_id="c", event_at="2026-06-22T10:30:00",
               source_kind="codex_history", session_vendor="codex"),
    ]
    repo, _ = _repo({TABLE_EVENT: events})
    until = datetime(2026, 6, 22, 12, 0, 0, tzinfo=UTC)
    by_kind = repo.list_events_by_source_window(source_kind="codex_local", until=until, limit=10)
    by_vendor = repo.list_events_by_source_window(vendor="codex", until=until, limit=10)
    by_both = repo.list_events_by_source_window(
        source_kind="codex_history", vendor="codex", until=until, limit=10)
    _check([r["event_id"] for r in by_kind] == ["cx-1"], "real: source_kind filter is single-table equality")
    _check(
        [r["event_id"] for r in by_vendor] == ["cx-1", "cx-2"],
        "real: vendor filter rides denormalized session_vendor (both codex events, DESC)",
    )
    _check([r["event_id"] for r in by_both] == ["cx-2"], "real: source_kind + vendor both applied")


def test_real_window_limit_clamp_narrows_to_100() -> None:
    repo, shim = _repo({TABLE_EVENT: _window_events()})
    repo.list_events_by_source_window(
        vendor="codex", until=datetime(2026, 6, 22, 12, 0, 0, tzinfo=UTC), limit=150,
    )
    _check(
        shim.last_ordered_limit == 100,
        "real: limit 150 pre-clamped to 100 (no Gap-C fail-loud — verb caps before the read)",
    )


# ── real backfill_event_source_denormalization via the shim ─────────────────

def _backfill_tables() -> dict[str, list[dict[str, Any]]]:
    return {
        TABLE_SOURCE: [
            {"id": "src-A", "source_kind": "codex_local", "is_deleted": 0},
            {"id": "src-B", "source_kind": "claude_code_local", "is_deleted": 0},
        ],
        TABLE_SESSION: [
            {"id": "ses-1", "source_id": "src-A", "vendor": "codex",
             "created_at": "2026-06-01T00:00:00", "is_deleted": 0},
            {"id": "ses-2", "source_id": "src-B", "vendor": "claude_code",
             "created_at": "2026-06-02T00:00:00", "is_deleted": 0},
        ],
        TABLE_EVENT: [
            # pre-migration rows: NULL denorm columns
            _event(eid="old-1", session_id="ses-1", event_at="2026-06-01T01:00:00",
                   session_vendor=None, source_kind=None),
            _event(eid="old-2", session_id="ses-2", event_at="2026-06-02T01:00:00",
                   session_vendor=None, source_kind=None),
            # an already-denormalized row (new event) — must be left untouched
            _event(eid="new-1", session_id="ses-1", event_at="2026-06-03T01:00:00",
                   session_vendor="codex", source_kind="codex_local"),
        ],
    }


def test_real_backfill_fills_and_is_idempotent() -> None:
    tables = _backfill_tables()
    repo, _ = _repo(tables)
    first = repo.backfill_event_source_denormalization()
    by_id = {e["id"]: e for e in tables[TABLE_EVENT]}
    _check(
        first == {"sessions_scanned": 2, "events_denormalized": 2},
        "real backfill: scans both sessions, fills the 2 NULL events (skips the new one)",
    )
    _check(
        by_id["old-1"]["session_vendor"] == "codex"
        and by_id["old-1"]["source_kind"] == "codex_local"
        and by_id["old-2"]["session_vendor"] == "claude_code"
        and by_id["old-2"]["source_kind"] == "claude_code_local",
        "real backfill: each event filled from its session's vendor + source's kind",
    )
    second = repo.backfill_event_source_denormalization()
    _check(
        second["events_denormalized"] == 0,
        "real backfill: idempotent — a second run fills nothing (session_vendor now set)",
    )


def test_real_backfill_fails_loud_on_unresolved_source() -> None:
    tables = _backfill_tables()
    tables[TABLE_SESSION].append(
        {"id": "ses-orphan", "source_id": "src-GONE", "vendor": "codex",
         "created_at": "2026-06-04T00:00:00", "is_deleted": 0},
    )
    tables[TABLE_EVENT].append(
        _event(eid="orphan-1", session_id="ses-orphan", event_at="2026-06-04T01:00:00",
               session_vendor=None, source_kind=None),
    )
    repo, _ = _repo(tables)
    raised = False
    try:
        repo.backfill_event_source_denormalization()
    except LedgerRepositoryError:
        raised = True
    _check(
        raised,
        "real backfill: a session whose source_id has no live __source row FAILS LOUD",
    )


def main() -> int:
    print("=== events_window_fold_smoke ===")
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
