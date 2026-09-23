#!/usr/bin/env python3
"""M6.5 Bug 2 smoke — ``upsert_session`` first/last_event_at bound merge.

Run:

    .venv/bin/python3 ananta/tests/llm/session_ledger/claude_code_first_last_event_at_smoke.py

Per 2026-06-11 M6.5 Bug 2 (Coordinator-Dawn dispatch §4): the
``upsert_session`` UPDATE path was missing the ``first_event_at`` clause.
An out-of-order vendor stream could land an earlier event AFTER an
existing session row's ``first_event_at`` had been frozen at INSERT time,
producing ``last_event_at < first_event_at`` inversions. The fix widens
both bounds: ``first_event_at`` narrows to the minimum, ``last_event_at``
widens to the maximum.

SQL-lockdown Slice 5 migrated this UPDATE off the single-statement SQL
``LEAST``/``GREATEST`` onto the state-interface ``update_state`` primitive:
the bound is now recomputed in Python (``min``/``max``) from a read of the
current row and written back. Race-safety no longer rides SQL atomicity —
it rests on single-writer-per-session (``upsert_session`` is importer-only;
pulling polls hold the source lease; pushed dispatch is single-caller; a
source is exactly one mode). See ``_update_existing_session`` and the
Slice-5 acceptance condition in
``2026-06-20_ledger_migration_slice_plan.md`` §1/§3.

Verifications:

1. The UPDATE branch recomputes ``first_event_at`` as the MINIMUM of the
   stored value and the incoming value (the Python ``LEAST`` equivalent).
2. It recomputes ``last_event_at`` as the MAXIMUM (the ``GREATEST``
   equivalent).
3. The INSERT path (no existing row) is unchanged: it carries the
   importer's supplied first/last values directly through the two-phase
   canonical dispatch (deferred to the Slice-6 keystone, still raw SQL).
"""

from __future__ import annotations

import sys
from datetime import UTC, datetime
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(_REPO_ROOT / "ananta" / "src"))
sys.path.insert(0, str(_REPO_ROOT / "ananta" / "tests" / "llm" / "session_ledger"))

from _stub_state_service import StubStateService  # noqa: E402
from ananta.llm.session_ledger.repository import SessionLedgerRepository  # noqa: E402
from ananta.llm.session_ledger.schema import TABLE_SESSION  # noqa: E402
from ananta.llm.session_ledger.types import (  # noqa: E402
    IngestSourceKind,
    SourceVendor,
)


def _expect(condition: bool, message: str) -> None:
    if not condition:
        print(f"FAIL: {message}", file=sys.stderr)
        sys.exit(1)
    print(f"PASS: {message}")


def _build_repo() -> tuple[SessionLedgerRepository, StubStateService]:
    stub = StubStateService()
    fixed_now = datetime(2026, 6, 11, 12, 0, 0, tzinfo=UTC)
    repo = SessionLedgerRepository(stub, clock=lambda: fixed_now)  # type: ignore[arg-type]
    return repo, stub


def test_upsert_update_recomputes_min_max() -> None:
    repo, stub = _build_repo()
    # Plant an existing session row so upsert_session takes the UPDATE branch
    # (the migrated existing-row lookup reads via txn.query_state).
    stub.add_select_response(
        "session_ledger__session",
        [
            {
                "id": "les_existing",
                "first_event_at": datetime(2026, 5, 1, 0, 0, 0, tzinfo=UTC),
                "last_event_at": datetime(2026, 5, 1, 0, 0, 0, tzinfo=UTC),
            }
        ],
    )
    session_id = repo.upsert_session(
        source_id="src_1",
        external_session_id="ext_1",
        vendor=SourceVendor.CLAUDE_CODE,
        source_kind=IngestSourceKind.CLAUDE_CODE_LOCAL,
        vendor_session_label=None,
        project_path="/tmp/p",
        first_event_at=datetime(2026, 4, 30, 0, 0, 0, tzinfo=UTC),  # earlier
        last_event_at=datetime(2026, 5, 2, 0, 0, 0, tzinfo=UTC),  # later
    )
    _expect(session_id == "les_existing", "UPDATE branch returns the existing session id")
    session_updates = [u for u in stub.updates if u.table == TABLE_SESSION]
    _expect(
        len(session_updates) == 1,
        f"exactly one update_state on __session; got {len(session_updates)}",
    )
    upd = session_updates[0].updates
    # SQL-lockdown Slice 6: the UPDATE path rides the autocommit ``_update`` seam,
    # which normalizes tz-aware datetimes to naive UTC (the F1 storage seam)
    # before the state service records them — so the recorded bound is naive (the
    # value that reaches Postgres). The pre-migration ``txn.update_state`` stub
    # recorded the aware input; the min/max logic is unchanged.
    _expect(
        upd["first_event_at"] == datetime(2026, 4, 30, 0, 0, 0),
        "first_event_at narrowed to the earlier value (Python min == SQL LEAST)",
    )
    _expect(
        upd["last_event_at"] == datetime(2026, 5, 2, 0, 0, 0),
        "last_event_at widened to the later value (Python max == SQL GREATEST)",
    )


def test_upsert_insert_path_unchanged() -> None:
    repo, stub = _build_repo()
    # SQL-lockdown Slice 6 (Option B): the INSERT path is Phase 1 ``upsert_state``
    # DO-NOTHING. With the stub default inserted=True the canonical row lands
    # (no Phase 2 demotion write). The bounds are written LITERALLY on insert
    # (the LEAST/GREATEST merge is UPDATE-path-only). The base ``_upsert_do_nothing``
    # seam naive-izes datetimes (F1 seam) before the state service records them.
    first_event = datetime(2026, 6, 1, 0, 0, 0, tzinfo=UTC)
    last_event = datetime(2026, 6, 1, 0, 0, 0, tzinfo=UTC)
    repo.upsert_session(
        source_id="src_1",
        external_session_id="ext_new",
        vendor=SourceVendor.CLAUDE_CODE,
        source_kind=IngestSourceKind.CLAUDE_CODE_LOCAL,
        vendor_session_label=None,
        project_path=None,
        first_event_at=first_event,
        last_event_at=last_event,
    )
    session_upserts = [u for u in stub.upserts if u.table == TABLE_SESSION]
    _expect(
        len(session_upserts) == 1,
        f"exactly one Phase-1 upsert_state on __session (Phase 1 landed; no "
        f"Phase 2); got {len(session_upserts)}",
    )
    up = session_upserts[0]
    # M18 §3.3: Phase 1 is the partial-unique ON CONFLICT DO NOTHING, expressed
    # as a structured conflict_predicate (column+WHERE inference — the DDL
    # renderer hash-suffixes the physical index name, so a named-constraint
    # reference fails at runtime; bug class surfaced 2026-06-12 sub-item 2).
    _expect(
        up.on_conflict == "do_nothing"
        and up.conflict_columns == ["vendor", "external_session_id"]
        and up.conflict_predicate == [
            {"column": "canonical_external_session_id", "op": "is_null"},
            {"column": "is_deleted", "op": "eq", "value": 0},
        ],
        "Phase 1 upsert_state DO-NOTHING mirrors the M18 partial-unique "
        "(vendor, external_session_id) WHERE canonical IS NULL AND is_deleted = 0",
    )
    # INSERT path writes the bounds LITERALLY (no LEAST/GREATEST — UPDATE-only);
    # naive-UTC per the F1 seam.
    _expect(
        up.record.get("first_event_at") == first_event.replace(tzinfo=None)
        and up.record.get("last_event_at") == last_event.replace(tzinfo=None),
        "Phase 1 record carries first_event_at and last_event_at literally (naive UTC)",
    )
    # Phase 1 landed → no Phase 2 demotion write.
    _expect(
        not [w for w in stub.writes if w.table == TABLE_SESSION],
        "inserted=True → no Phase 2 write_state demotion",
    )


def main() -> None:
    print("M6.5 Bug 2 — upsert_session first/last_event_at bound merge")
    print("=" * 60)
    test_upsert_update_recomputes_min_max()
    test_upsert_insert_path_unchanged()
    print("=" * 60)
    print("ALL PASS")


if __name__ == "__main__":
    main()
