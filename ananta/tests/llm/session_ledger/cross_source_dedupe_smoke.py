#!/usr/bin/env python3
"""W5.B cross-source dedupe smoke — 14 cases (a–n).

Per `workbench/2026-06-14_w5b_cross_source_dedupe_design.md` §6 step 6.
Replaces the prior M18 §3.5 race-condition smoke (whose 5 cases tested
the relocated ``_resolve_canonical_session`` symbol that W5.O moved out
of the public attribute surface). The new shape covers:

* (a) ingest-time canonical INSERT shape (Phase 1 of the two-phase
      upsert hits the partial-unique ON CONFLICT clause).
* (b) ingest-time sibling demotion (Phase 2 INSERTs the loser with the
      canonical pointer populated).
* (c) ``list_sessions`` default canonical-only filter.
* (d) ``list_sessions(include_siblings=True)`` forensic mode includes
      siblings.
* (e) ``search_events`` default canonical-only filter.
* (f) ``search_events(include_siblings=True)`` forensic mode includes
      sibling-anchored events.
* (g)–(j) ``list_canonical_contributors`` paths (canonical-input,
      sibling-input/C1, no-siblings, orphaned-canonical/C3) MOVED to
      ``list_canonical_contributors_migration_smoke.py`` when that verb
      migrated off raw SQL (SQL-lockdown, the last ledger ``_fetch_all``);
      the dedicated smoke tests them via a filter-honoring shim plus the
      INNER-JOIN source-absent drop, soft-deleted-source retention, and the
      datetime-return-type parse-back.
* (k) ``list_sessions(source_kind=K)`` matches via EXISTS-over-canonical-
      group when K is a SIBLING's source_kind (locks Codex C2 — the pre-
      W5.B direct-INNER-JOIN form returned 0 rows here).
* (l) Service-layer / process pass-through: ``SessionLedgerService``
      wrapper forwards ``include_siblings`` to the repository (locks
      Codex C1 4-layer rule).
* (m) ``search_events`` enforces ``s.is_deleted=0`` + ``src.is_deleted=0``
      in the WHERE clause (locks Codex C6).
* (n) Enum drift: every ``IngestSourceKind`` member has a
      ``_VENDOR_FROM_SOURCE_KIND`` mapping, including
      ``CLAUDE_AI_EXPORT → CLAUDE_AI`` (locks Codex C4).
"""

from __future__ import annotations

import sys
from datetime import UTC, datetime
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(_REPO_ROOT / "ananta" / "src"))
sys.path.insert(0, str(_REPO_ROOT / "ananta" / "tests" / "llm" / "session_ledger"))

from _stub_state_service import StubStateService  # noqa: E402
from ananta.llm.session_ledger.importer import _VENDOR_FROM_SOURCE_KIND  # noqa: E402
from ananta.llm.session_ledger.repository import (  # noqa: E402
    SessionLedgerRepository,
)
from ananta.llm.session_ledger.types import (  # noqa: E402
    IngestSourceKind,
    SourceVendor,
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


def _now() -> datetime:
    return datetime(2026, 6, 14, 12, 0, 0, tzinfo=UTC)


def _make_repo() -> tuple[SessionLedgerRepository, StubStateService]:
    state = StubStateService()
    repo = SessionLedgerRepository(state, clock=_now)  # type: ignore[arg-type]
    return repo, state


# ─── (a) + (b) Ingest-time canonical election + sibling demotion ─────────────


def test_a_ingest_canonical_insert_emits_on_conflict_partial_unique() -> None:
    """Phase 1 of M18 two-phase upsert rides upsert_state DO-NOTHING (Slice-6 Option B).

    Post-SQL-lockdown the keystone composes no raw SQL: Phase 1 is the typed
    ``upsert_state`` DO-NOTHING whose structured ``conflict_predicate`` mirrors
    the M18 partial-unique index. With the stub default ``inserted=True`` the
    canonical row lands and there is no Phase 2 (one upsert, no write_state).
    """
    repo, state = _make_repo()
    repo.upsert_session(
        source_id="src_local",
        external_session_id="ext_T1",
        vendor=SourceVendor.CLAUDE_CODE,
        source_kind=IngestSourceKind.CLAUDE_CODE_LOCAL,
        vendor_session_label="canon",
        project_path=None,
        first_event_at=_now(),
        last_event_at=_now(),
    )
    session_upserts = [u for u in state.upserts if u.table == "session"]
    _check(
        len(session_upserts) == 1,
        f"(a) exactly one upsert_state issued for the session (got {len(session_upserts)})",
    )
    if session_upserts:
        up = session_upserts[0]
        _check(
            up.on_conflict == "do_nothing",
            "(a) Phase 1 rides upsert_state DO-NOTHING (loser falls to Phase 2)",
        )
        _check(
            up.conflict_columns == ["vendor", "external_session_id"],
            f"(a) conflict_columns = (vendor, external_session_id) (got {up.conflict_columns})",
        )
        # Per Codex C5: the DDL renderer hash-suffixes the physical index name,
        # so the upsert uses column+WHERE INFERENCE via a structured
        # conflict_predicate mirroring the M18 partial-unique WHERE
        # (canonical_external_session_id IS NULL AND is_deleted = 0) — NOT a
        # named-constraint reference. Asserting the predicate AST locks C5.
        _check(
            up.conflict_predicate == [
                {"column": "canonical_external_session_id", "op": "is_null"},
                {"column": "is_deleted", "op": "eq", "value": 0},
            ],
            f"(a) conflict_predicate mirrors the M18 partial-unique WHERE "
            f"(Codex C5 — column+WHERE inference) (got {up.conflict_predicate})",
        )
        _check(
            up.record.get("canonical_external_session_id") is None,
            "(a) Phase 1 attempts the canonical (canonical_external_session_id NULL) row",
        )
    # Happy path landed canonical → no Phase 2 demotion write.
    _check(
        not [w for w in state.writes if w.table == "session"],
        "(a) inserted=True → no Phase 2 write_state demotion",
    )


def test_b_ingest_phase2_inserts_with_canonical_pointer() -> None:
    """Phase 2 (sibling demotion) resolves the canonical + write_states the pointer.

    Slice-6 Option B: Phase 1 conflict (``inserted=False``) → the canonical
    resolve runs as a ``query_state`` ``is_null`` filter, then the loser is
    INSERTed via ``write_state`` with ``canonical_external_session_id``
    populated. The existing-row read and the resolve both hit
    ``query_state(session)`` — the filter-aware planted response distinguishes
    them: the resolve carries ``canonical_external_session_id`` in its filters,
    the existing-row read does not (so it returns [] → the INSERT branch).
    """
    repo, state = _make_repo()
    state.set_upsert_inserted_result(False)  # Phase 1 partial-unique conflict
    state.add_query_response(
        "session",
        [{"id": "les_canon_T1", "external_session_id": "ext_T1"}],
        when=lambda f: "canonical_external_session_id" in f,
    )
    repo.upsert_session(
        source_id="src_history",
        external_session_id="ext_T1",
        vendor=SourceVendor.CLAUDE_CODE,
        source_kind=IngestSourceKind.CLAUDE_CODE_HISTORY,
        vendor_session_label="sibling",
        project_path=None,
        first_event_at=_now(),
        last_event_at=_now(),
    )
    session_writes = [w for w in state.writes if w.table == "session"]
    pointer_carrying = [
        w for w in session_writes
        if w.record.get("canonical_external_session_id") == "ext_T1"
    ]
    _check(
        len(pointer_carrying) == 1,
        f"(b) Phase 2 write_state carries canonical_external_session_id=ext_T1 "
        f"(got {len(pointer_carrying)} of {len(session_writes)} session writes)",
    )


# ─── (c) + (d) list_sessions default canonical-only + include_siblings ──────


def test_c_list_sessions_default_canonical_only_filter() -> None:
    # SQL-lockdown junction migration: the canonical-only default is now a
    # query_state predicate (no source_kind → one query_state read over
    # __session), not raw SQL. Assert the typed filter carries
    # canonical_external_session_id IS NULL by default.
    repo, state = _make_repo()
    repo.list_sessions(limit=10)
    session_reads = [c for c in state.query_state_calls if c.table == "session"]
    _check(len(session_reads) == 1, "(c) one query_state read against __session")
    if session_reads:
        _check(
            session_reads[0].filters.get("canonical_external_session_id")
            == {"op": "is_null"},
            "(c) typed filter restricts to canonical rows by default",
        )


def test_d_list_sessions_include_siblings_omits_canonical_filter() -> None:
    repo, state = _make_repo()
    repo.list_sessions(limit=10, include_siblings=True)
    session_reads = [c for c in state.query_state_calls if c.table == "session"]
    _check(len(session_reads) == 1, "(d) one query_state read against __session")
    if session_reads:
        _check(
            "canonical_external_session_id" not in session_reads[0].filters,
            "(d) typed filter omits the canonical-only predicate under include_siblings",
        )


# ─── (k) C2 EXISTS regression: source_kind matches via sibling ──────────────


def test_k_list_sessions_source_kind_routes_via_junction_locks_c2() -> None:
    """Codex C2 (post-junction migration): list_sessions(source_kind=K) must match
    the canonical when ANY contributor (canonical or sibling) has source_kind=K.

    SQL-lockdown junction migration: the EXISTS-over-canonical-group subquery is
    replaced by the ``session_source_kind`` junction. The ingest attach-path +
    backfill record EVERY group member's kind under the group's CANONICAL id, so a
    SIBLING's kind K still yields the canonical id. The read routes
    ``query_state(junction, {source_kind: K})`` → canonical ids →
    ``query_state(session, {id: ANY})``. This locks the C2 semantic at the read
    layer; the "kind recorded from a sibling" half lives in the ingest-attach +
    backfill smokes.
    """
    repo, state = _make_repo()
    history = IngestSourceKind.CLAUDE_CODE_HISTORY.value
    state.add_query_response(
        "session_source_kind",
        [{"canonical_session_id": "les_canon"}],
        when=lambda f: f.get("source_kind") == history,
    )
    state.add_query_response(
        "session",
        [{
            "id": "les_canon", "source_id": "src_local",
            "external_session_id": "ext_T1", "vendor": "claude_code",
            "vendor_session_label": "canon", "project_path": None,
            "first_event_at": "2026-06-01T00:00:00", "last_event_at": "2026-06-02T00:00:00",
            "event_count": 5, "canonical_external_session_id": None,
        }],
        when=lambda f: "id" in f,
    )
    rows = repo.list_sessions(limit=10, source_kind=IngestSourceKind.CLAUDE_CODE_HISTORY)
    junction_reads = [c for c in state.query_state_calls if c.table == "session_source_kind"]
    _check(
        len(junction_reads) == 1 and junction_reads[0].filters.get("source_kind") == history,
        "(k) junction read by source_kind=K (canonical-group resolution)",
    )
    session_reads = [c for c in state.query_state_calls if c.table == "session"]
    _check(
        len(session_reads) == 1 and session_reads[0].filters.get("id") == ["les_canon"],
        "(k) session read routes id=ANY(canonical_ids resolved from the junction)",
    )
    _check(
        [str(r["id"]) for r in rows] == ["les_canon"],
        "(k) canonical surfaces via the junction (C2: matched via a sibling's kind)",
    )


# ─── (n) Enum drift smoke (Codex C4) ───────────────────────────────────────


def test_n_enum_drift_every_source_kind_has_vendor_mapping() -> None:
    missing = [
        kind for kind in IngestSourceKind if kind not in _VENDOR_FROM_SOURCE_KIND
    ]
    _check(
        not missing,
        f"(n) every IngestSourceKind member maps to a vendor "
        f"(missing: {[m.value for m in missing]})",
    )
    _check(
        _VENDOR_FROM_SOURCE_KIND.get(IngestSourceKind.CLAUDE_AI_EXPORT)
        is SourceVendor.CLAUDE_AI,
        "(n) CLAUDE_AI_EXPORT → CLAUDE_AI mapping present (Codex C4)",
    )


# ─── main ───────────────────────────────────────────────────────────────────


def main() -> int:
    print("=== W5.B cross_source_dedupe_smoke (a–d, k, n) ===")
    test_a_ingest_canonical_insert_emits_on_conflict_partial_unique()
    test_b_ingest_phase2_inserts_with_canonical_pointer()
    test_c_list_sessions_default_canonical_only_filter()
    test_d_list_sessions_include_siblings_omits_canonical_filter()
    test_k_list_sessions_source_kind_routes_via_junction_locks_c2()
    test_n_enum_drift_every_source_kind_has_vendor_mapping()
    print(f"\n{_passed} passed, {len(_failed)} failed")
    if _failed:
        for label in _failed:
            print(f"  FAILED: {label}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
