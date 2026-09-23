#!/usr/bin/env python3
"""M6.5 Bug 2 — upsert_session bound-merge + COALESCE-asymmetry under the migration.

Run:

    .venv/bin/python3 ananta/tests/llm/session_ledger/least_greatest_upsert_concurrent_smoke.py

ORIGIN. M6.5 Bug 2 (2026-06-11) made the ``upsert_session`` UPDATE a
single-statement SQL ``LEAST``/``GREATEST`` + ``COALESCE`` so the event-time
bounds were race-free under concurrent ingest — the read+write happened inside
one Postgres statement, no read-then-write window.

MIGRATION (SQL-lockdown Slice 5). The state-interface ``update_state`` SET
grammar is ``column = value`` only — it cannot emit SQL ``LEAST`` / ``GREATEST``
/ ``COALESCE`` expressions. So the merge is recomputed in Python from a read of
the current row (``_update_existing_session``) and written back. That
re-introduces a read-then-write window, which is **safe only under
single-writer-per-session**. The REAL fence (Architect ruling 2026-06-21) is
NOT a lease on the push path and NOT a "single-caller HTTP handler" (an earlier
draft of this header wrongly claimed the latter) — it is the platform's
SINGLE-THREADED SERIAL action-queue dispatch:

  * ``upsert_session`` is importer-only (callers ``importer._persist_normalized``
    + ``._poll_one_session``);
  * a pulling poll holds the per-source polling lease (``try_acquire_polling_lease``);
  * the push entrypoint ``ingest_raw_chunk`` is a SYNCHRONOUS (``is_async=False``)
    ``@service_interface_process`` EDGE terminal; ``ActionQueuePoller._poll_once``
    drains claimed actions in a serial ``for action: await _process_action`` loop
    and awaits each to completion before the next, so two push-path
    ``upsert_session`` writes to one session never overlap;
  * the only non-importer writer of first/last_event_at is the operator-gated
    ``inverted_bounds_repair`` (quiescent).

So no two writers race a session's bounds — the serial-dispatch fence is the
replacement for M6.5 Bug 2's SQL-atomic shape. This smoke therefore no longer
asserts an SQL shape — it asserts the merge SEMANTICS the migration must
preserve (min/max/COALESCE correctness over a planted existing row). It does
NOT exercise overlapping concurrent upserts, and neither do the live smokes
(they drive a sequential lifecycle) — there is no concurrency to test because
the serial dispatch makes overlap impossible. That dispatch invariant is itself
guarded by ``ingest_raw_chunk_sync_dispatch_tripwire_smoke.py``; the rationale
is documented above ``_update_existing_session`` + in
``2026-06-20_ledger_migration_slice_plan.md`` §1/§3.

Verifications:

1. Two upserts simulating racing polls land the UNION of their event-time
   ranges: ``first_event_at`` narrows to the global min, ``last_event_at``
   widens to the global max (the ``LEAST``/``GREATEST`` equivalent).
2. The seven COALESCE merges honour their ASYMMETRIC directions:
   ``vendor_session_label`` / ``project_path`` are new-wins
   (``COALESCE(new, existing)``); the four originator/recipient actor columns +
   ``summary_text`` are existing-wins snapshot (``COALESCE(existing, new)``).
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


def _build_repo_with_existing_row(
    existing: dict[str, object],
) -> tuple[SessionLedgerRepository, StubStateService]:
    stub = StubStateService()
    stub.add_select_response("session_ledger__session", [existing])
    fixed_now = datetime(2026, 6, 11, 12, 0, 0, tzinfo=UTC)
    repo = SessionLedgerRepository(stub, clock=lambda: fixed_now)  # type: ignore[arg-type]
    return repo, stub


def _session_update(stub: StubStateService) -> dict[str, object]:
    session_updates = [u for u in stub.updates if u.table == TABLE_SESSION]
    _expect(
        len(session_updates) == 1,
        f"exactly one update_state on __session; got {len(session_updates)}",
    )
    return session_updates[0].updates


def test_racing_upserts_land_union_of_ranges() -> None:
    # Existing canonical bounds: [2026-05-05, 2026-05-05].
    existing: dict[str, object] = {
        "id": "les_existing",
        "first_event_at": datetime(2026, 5, 5, 0, 0, 0, tzinfo=UTC),
        "last_event_at": datetime(2026, 5, 5, 0, 0, 0, tzinfo=UTC),
    }
    # Poll A surfaces an EARLIER range; Poll B surfaces a LATER range. Each is
    # applied against the same stored row (the single-writer fence guarantees
    # they do not interleave); the union is min(first)..max(last).
    repo_a, stub_a = _build_repo_with_existing_row(dict(existing))
    repo_a.upsert_session(
        source_id="src_1",
        external_session_id="ext_1",
        vendor=SourceVendor.CLAUDE_CODE,
        source_kind=IngestSourceKind.CLAUDE_CODE_LOCAL,
        vendor_session_label=None,
        project_path=None,
        first_event_at=datetime(2026, 4, 30, 0, 0, 0, tzinfo=UTC),
        last_event_at=datetime(2026, 5, 1, 0, 0, 0, tzinfo=UTC),
    )
    upd_a = _session_update(stub_a)
    # SQL-lockdown Slice 6: the UPDATE path now rides the autocommit ``_update``
    # seam, which normalizes tz-aware datetimes to naive UTC (the F1 storage
    # seam) BEFORE handing them to the state service — so the recorded bound is
    # naive (the value that reaches Postgres), where the pre-migration
    # ``txn.update_state`` stub recorded the aware input. The min/max logic is
    # unchanged; only the recorded representation is naive-UTC.
    _expect(
        upd_a["first_event_at"] == datetime(2026, 4, 30, 0, 0, 0),
        "Poll A narrows first_event_at to its earlier value (min)",
    )
    _expect(
        upd_a["last_event_at"] == datetime(2026, 5, 5, 0, 0, 0),
        "Poll A keeps the existing later last_event_at (max)",
    )

    repo_b, stub_b = _build_repo_with_existing_row(dict(existing))
    repo_b.upsert_session(
        source_id="src_1",
        external_session_id="ext_1",
        vendor=SourceVendor.CLAUDE_CODE,
        source_kind=IngestSourceKind.CLAUDE_CODE_LOCAL,
        vendor_session_label=None,
        project_path=None,
        first_event_at=datetime(2026, 5, 8, 0, 0, 0, tzinfo=UTC),
        last_event_at=datetime(2026, 5, 10, 0, 0, 0, tzinfo=UTC),
    )
    upd_b = _session_update(stub_b)
    _expect(
        upd_b["first_event_at"] == datetime(2026, 5, 5, 0, 0, 0),
        "Poll B keeps the existing earlier first_event_at (min)",
    )
    _expect(
        upd_b["last_event_at"] == datetime(2026, 5, 10, 0, 0, 0),
        "Poll B widens last_event_at to its later value (max)",
    )


def test_coalesce_directions_are_asymmetric() -> None:
    existing: dict[str, object] = {
        "id": "les_existing",
        "first_event_at": datetime(2026, 5, 5, 0, 0, 0, tzinfo=UTC),
        "last_event_at": datetime(2026, 5, 5, 0, 0, 0, tzinfo=UTC),
        # new-wins columns already populated — a fresh value must overwrite.
        "vendor_session_label": "old-label",
        "project_path": "/old/path",
        # existing-wins snapshot columns already populated — must NOT be touched.
        "originator_session_label": "orig-snap",
        "originator_agent_instance_id": "agi-snap",
        "recipient_session_label": "recip-snap",
        "recipient_agent_instance_id": "agi-recip-snap",
        "summary_text": "frozen-summary",
    }
    repo, stub = _build_repo_with_existing_row(existing)
    repo.upsert_session(
        source_id="src_1",
        external_session_id="ext_1",
        vendor=SourceVendor.CLAUDE_CODE,
        source_kind=IngestSourceKind.CLAUDE_CODE_LOCAL,
        vendor_session_label="new-label",
        project_path="/new/path",
        first_event_at=datetime(2026, 5, 5, 0, 0, 0, tzinfo=UTC),
        last_event_at=datetime(2026, 5, 5, 0, 0, 0, tzinfo=UTC),
        originator_session_label="should-not-overwrite",
        originator_agent_instance_id="agi-should-not-overwrite",
        recipient_session_label="should-not-overwrite",
        recipient_agent_instance_id="agi-should-not-overwrite",
        summary_text_seed="should-not-overwrite",
    )
    upd = _session_update(stub)
    # new-wins: COALESCE(new, existing)
    _expect(
        upd["vendor_session_label"] == "new-label",
        "vendor_session_label is new-wins (COALESCE(new, existing))",
    )
    _expect(
        upd["project_path"] == "/new/path",
        "project_path is new-wins (COALESCE(new, existing))",
    )
    # existing-wins snapshot: COALESCE(existing, new)
    _expect(
        upd["originator_session_label"] == "orig-snap",
        "originator_session_label is existing-wins snapshot (COALESCE(existing, new))",
    )
    _expect(
        upd["originator_agent_instance_id"] == "agi-snap",
        "originator_agent_instance_id is existing-wins snapshot",
    )
    _expect(
        upd["recipient_session_label"] == "recip-snap",
        "recipient_session_label is existing-wins snapshot",
    )
    _expect(
        upd["recipient_agent_instance_id"] == "agi-recip-snap",
        "recipient_agent_instance_id is existing-wins snapshot",
    )
    _expect(
        upd["summary_text"] == "frozen-summary",
        "summary_text is existing-wins snapshot (M6 never overwrites a seed)",
    )


def test_coalesce_backfills_null_existing() -> None:
    # When a snapshot column is NULL, the incoming value backfills it.
    existing: dict[str, object] = {
        "id": "les_existing",
        "first_event_at": datetime(2026, 5, 5, 0, 0, 0, tzinfo=UTC),
        "last_event_at": datetime(2026, 5, 5, 0, 0, 0, tzinfo=UTC),
        "originator_session_label": None,
    }
    repo, stub = _build_repo_with_existing_row(existing)
    repo.upsert_session(
        source_id="src_1",
        external_session_id="ext_1",
        vendor=SourceVendor.CLAUDE_CODE,
        source_kind=IngestSourceKind.CLAUDE_CODE_LOCAL,
        vendor_session_label=None,
        project_path=None,
        first_event_at=datetime(2026, 5, 5, 0, 0, 0, tzinfo=UTC),
        last_event_at=datetime(2026, 5, 5, 0, 0, 0, tzinfo=UTC),
        originator_session_label="backfilled",
    )
    upd = _session_update(stub)
    _expect(
        upd["originator_session_label"] == "backfilled",
        "a NULL snapshot column is backfilled by the incoming value",
    )


def main() -> None:
    print("M6.5 Bug 2 — upsert_session bound-merge + COALESCE asymmetry")
    print("=" * 60)
    test_racing_upserts_land_union_of_ranges()
    test_coalesce_directions_are_asymmetric()
    test_coalesce_backfills_null_existing()
    print("=" * 60)
    print("ALL PASS")


if __name__ == "__main__":
    main()
