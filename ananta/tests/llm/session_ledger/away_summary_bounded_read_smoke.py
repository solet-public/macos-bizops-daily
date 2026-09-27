#!/usr/bin/env python3
"""iss_8b8a970b fix smoke: bounded away-summary read across a large conversation group.

``find_latest_away_summary_for_session`` widens its lookup across the
canonical session plus every cross-source sibling
(``_resolve_conversation_group``) and, before this fix, queried
``TABLE_EVENT`` for every matching SYSTEM-event-with-``content_json`` row via
the UNBOUNDED ``_query`` primitive (``query_state``) with no ``limit`` and no
``unbounded=True``. A conversation group with a large sibling — this is not
rare, any long-running claude_code session paired with its
claude_code_history sibling can carry thousands of events — can have well
over 100 such rows, which trips the state service's 100-row cap
(``query.unbounded_read_over_cap``,
``ananta/src/ananta/services/state_service/read_bounds.py:90``) on EVERY
drain, forever (the failure is deterministic on the data, never transient).
Confirmed live in production logs 2026-09-24 through 2026-09-27 for session
``les_c1671f3729594a70a8570762df8e5dff`` — see register issue iss_8b8a970b
and its evidence event ``iev_7fa467f1``.

The fix (this lane) switches the read to the BOUNDED ``_query_ordered``
primitive (``event_at`` DESC, ``sequence`` DESC — the same tie-break
:func:`_select_latest_away_summary` sorts by internally), ``limit=100``, which
never raises the cap error (the primitive is bounded by construction — no
count-then-refuse, just a page).

Two cases, both against a FAITHFUL in-memory fake of the two state
primitives (:class:`_FakeLedgerState`) that models the SAME cap-refusal
``query_state`` enforces in production and the same ordered/limited page
``query_ordered`` returns — not a shortcut fake that assumes away the very
behavior under test:

* RED/GREEN: a conversation group with 150 SYSTEM-events-with-content_json,
  the true most-recent ``away_summary`` recap comfortably within the newest
  100. Before the fix (calling the OLD unbounded shape directly against the
  same fake, to prove the fake models the cap faithfully): raises
  ``LedgerRepositoryError`` naming ``query.unbounded_read_over_cap``. After
  the fix (calling the real, current ``find_latest_away_summary_for_session``):
  returns the recap text without raising.
* KNOWN LIMITATION (documented, not swept under the rug): a conversation group
  whose true most-recent recap sits BEHIND 100 newer non-away_summary
  SYSTEM-events-with-content_json. The bounded read no longer raises, but it
  also does not find a recap outside its window — it returns ``None`` (falls
  through to inference) rather than the stale-but-real recap. This is the
  documented trade the fix makes explicit in
  ``find_latest_away_summary_for_session``'s docstring; this test proves the
  trade is real and bounded (a knowable miss), not an unconditional exception.

Run::

    .venv/bin/python3 ananta/tests/llm/session_ledger/away_summary_bounded_read_smoke.py
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, cast

REPO_ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(REPO_ROOT / "ananta" / "src"))

from ananta.llm.session_ledger.base import (  # noqa: E402
    LedgerRepositoryError,
)
from ananta.llm.session_ledger.repository import SessionLedgerRepository  # noqa: E402
from ananta.llm.session_ledger.schema import TABLE_EVENT  # noqa: E402
from ananta.llm.session_ledger.types import EventType  # noqa: E402
from ananta.services.state_service.read_bounds import (  # noqa: E402
    MAX_READ_ROWS,
    overflow_message,
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


def _matches(row: dict[str, object], filters: dict[str, object]) -> bool:
    """Faithful-enough flat-grammar matcher: equality, list → ``= ANY``,
    ``is_null``/``is_not_null``. Mirrors the sanctioned filter grammar
    ``_query``/``_query_ordered`` document (base.py)."""
    for col, spec in filters.items():
        value = row.get(col)
        if isinstance(spec, dict):
            op = spec.get("op")
            if op == "is_not_null" and value is None:
                return False
            if op == "is_null" and value is not None:
                return False
        elif isinstance(spec, list):
            if value not in spec:
                return False
        else:
            if value != spec:
                return False
    return True


class _FakeLedgerState:
    """Models ``query_state`` (unbounded, capped at MAX_READ_ROWS) and
    ``query_ordered`` (bounded by ``limit``, never capped) over an in-memory
    table set — faithfully enough to prove the cap-refusal / bounded-page
    distinction the fix depends on, without needing Postgres."""

    def __init__(self, tables: dict[str, list[dict[str, object]]]) -> None:
        self._tables = tables

    def query_state(self, namespace: str, query: dict[str, object]) -> dict[str, object]:
        _ = namespace
        table = str(query["table"])
        filters = query.get("filters") or {}
        matched = [r for r in self._tables.get(table, []) if _matches(r, filters)]  # type: ignore[arg-type]
        if not query.get("unbounded") and len(matched) > MAX_READ_ROWS:
            return {
                "action_status": "failed",
                "data": None,
                "actions": [],
                "error": {
                    "type": "plugin_error",
                    "code": "query.unbounded_read_over_cap",
                    "message": overflow_message(table=table),
                    "details": {
                        "namespace": namespace, "table": table,
                        "cap_rows": MAX_READ_ROWS, "row_count": len(matched),
                    },
                },
                "timestamp": "",
            }
        return {
            "action_status": "completed", "data": {"records": matched},
            "actions": [], "error": None, "timestamp": "",
        }

    def query_ordered(self, namespace: str, data: dict[str, object]) -> dict[str, object]:
        _ = namespace
        table = str(data["table"])
        raw_filters = data.get("filters")
        filters: dict[str, object] = (
            dict(raw_filters) if isinstance(raw_filters, dict) else {}
        )
        # query_ordered applies is_deleted=0 by default (include_deleted
        # defaults False) — the real primitive's behavior, faithfully modeled
        # so the caller (post-fix) does not need to pass it explicitly.
        if not data.get("include_deleted", False):
            filters.setdefault("is_deleted", 0)
        matched = [r for r in self._tables.get(table, []) if _matches(r, filters)]  # type: ignore[arg-type]
        order_by = data["order_by"]  # type: ignore[index]
        assert isinstance(order_by, list)
        direction = order_by[0][1]
        reverse = direction == "desc"

        def sort_key(row: dict[str, object]) -> tuple[object, ...]:
            return tuple(row.get(col) for col, _dir in order_by)

        matched.sort(key=sort_key, reverse=reverse)
        limit = int(data["limit"])  # type: ignore[arg-type]
        page = matched[:limit]
        return {
            "action_status": "completed", "data": {"records": page},
            "actions": [], "error": None, "timestamp": "",
        }


def _event(
    *, row_id: str, session_id: str, sequence: int, event_at: str,
    subtype: str | None, is_deleted: int = 0,
) -> dict[str, object]:
    return {
        "id": row_id,
        "session_id": session_id,
        "sequence": sequence,
        "event_type": EventType.SYSTEM.value,
        "event_at": event_at,
        "content_json": {"subtype": subtype} if subtype else {"other": "field"},
        "content_text": f"recap for {row_id}" if subtype == "away_summary" else None,
        "is_deleted": is_deleted,
    }


def _session(row_id: str, external_session_id: str, canonical: str | None) -> dict[str, object]:
    return {
        "id": row_id, "external_session_id": external_session_id,
        "canonical_external_session_id": canonical, "is_deleted": 0,
    }


def test_bounded_read_finds_recap_within_the_newest_100_and_never_raises() -> None:
    """150 SYSTEM-events-with-content_json across a canonical+sibling group;
    the true most-recent away_summary is event_at-newest of all of them, so it
    is comfortably inside the newest-100 bounded page."""
    canonical = "les-canon-A"
    sibling = "les-sib-A"
    ext_id = "ext-A"
    sessions = [
        _session(canonical, ext_id, None),
        _session(sibling, ext_id, ext_id),
    ]
    events = [
        _event(
            row_id=f"evt-old-{i}", session_id=sibling, sequence=i,
            event_at=f"2026-09-01T00:{i:02d}:00", subtype="heartbeat",
        )
        for i in range(149)
    ]
    events.append(
        _event(
            row_id="evt-recap", session_id=sibling, sequence=999,
            event_at="2026-09-27T12:00:00", subtype="away_summary",
        ),
    )
    state = _FakeLedgerState({"session": sessions, "event": events})
    repo = SessionLedgerRepository(cast("Any", state))

    # First prove the fake faithfully models the CAP this fix is escaping —
    # calling the OLD unbounded shape directly must still raise, exactly like
    # production did 2026-09-24 through today (420 occurrences).
    raised = False
    try:
        repo._query(  # noqa: SLF001 — exercising the primitive the old code used
            TABLE_EVENT,
            {
                "session_id": [canonical, sibling],
                "event_type": EventType.SYSTEM.value,
                "is_deleted": 0,
                "content_json": {"op": "is_not_null"},
            },
        )
    except LedgerRepositoryError as exc:
        raised = True
        _check(
            "query.unbounded_read_over_cap" in str(exc),
            f"old unbounded shape raises the cap error (got: {exc})",
        )
    _check(raised, "old unbounded _query over 150 rows raises (fake models the cap)")

    # Now the REAL, current (fixed) method — must NOT raise, and must find
    # the recap.
    result = repo.find_latest_away_summary_for_session(canonical)
    _check(
        result == "recap for evt-recap",
        f"fixed bounded read finds the newest away_summary (got {result!r})",
    )


def test_bounded_read_known_limitation_misses_a_buried_recap() -> None:
    """The true most-recent away_summary is BEHIND 100 newer
    non-away_summary SYSTEM-events-with-content_json. Documents (does not
    silently hide) the bounded fix's known limitation: it misses the recap and
    returns None (falls through to inference) rather than raising."""
    canonical = "les-canon-B"
    sibling = "les-sib-B"
    ext_id = "ext-B"
    sessions = [
        _session(canonical, ext_id, None),
        _session(sibling, ext_id, ext_id),
    ]
    events = [
        _event(
            row_id="evt-recap-buried", session_id=sibling, sequence=0,
            event_at="2026-01-01T00:00:00", subtype="away_summary",
        ),
    ]
    events.extend(
        _event(
            row_id=f"evt-newer-{i}", session_id=sibling, sequence=i + 1,
            event_at=f"2026-09-{(i % 27) + 1:02d}T00:{i % 60:02d}:00",
            subtype="heartbeat",
        )
        for i in range(120)
    )
    state = _FakeLedgerState({"session": sessions, "event": events})
    repo = SessionLedgerRepository(cast("Any", state))

    result = repo.find_latest_away_summary_for_session(canonical)
    _check(
        result is None,
        f"buried recap (behind 120 newer non-matching rows) is not found — "
        f"known, bounded miss, not a raise (got {result!r})",
    )


def main() -> int:
    print("=== away_summary_bounded_read_smoke (iss_8b8a970b fix, 2026-09-27) ===")
    test_bounded_read_finds_recap_within_the_newest_100_and_never_raises()
    test_bounded_read_known_limitation_misses_a_buried_recap()
    print(f"\n{_passed} passed, {len(_failed)} failed")
    if _failed:
        for label in _failed:
            print(f"  FAILED: {label}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
