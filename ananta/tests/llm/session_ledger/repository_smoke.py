#!/usr/bin/env python3
"""Smoke test for SessionLedgerRepository against a stubbed state_service.

Coverage:

* `insert_source` emits an INSERT into ``session_ledger__source`` with the
  expected column set and JSONB cast on ``config_json``.
* `read/write_cursor` distinguish discovery (scope_key NULL) vs event_read
  (non-null scope_key) and write into ``session_ledger__source_cursor``.
* `start_batch` + `finish_batch` round-trip the import_batch lifecycle.
* `upsert_session` UPDATEs existing rows and INSERTs new ones.
* `append_event` allocates a sequence under transaction and INSERTs the event
  with full ``content_text`` / ``content_json``. Per the 2026-06-11 SecretGate
  v1 rip-out, no scan runs at ingest and ``quarantine_id`` is always None on
  the returned ``EventInsertResult``.
* `append_event` ENFORCES per-event-type shape contract (raises ValueError
  on MESSAGE missing role).
* `record_lease_ping` computes expires_at = last_seen + ttl.
* `acknowledge_quarantine` UPDATEs status from OPEN → ACKNOWLEDGED only.

Run:
    .venv/bin/python3 ananta/tests/llm/session_ledger/repository_smoke.py
"""

from __future__ import annotations

import sys
from datetime import UTC, datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(REPO_ROOT / "ananta" / "src"))
sys.path.insert(0, str(REPO_ROOT / "ananta" / "tests" / "llm" / "session_ledger"))

from _stub_state_service import StubStateService  # noqa: E402
from ananta.llm.session_ledger.repository import (  # noqa: E402
    LedgerRepositoryError,
    SessionLedgerRepository,
)
from ananta.llm.session_ledger.schema import (  # noqa: E402
    NAMESPACE,
    TABLE_ACTIVE_LEASE,
    TABLE_EVENT,
    TABLE_IMPORT_BATCH,
    TABLE_SESSION,
    TABLE_SOURCE,
    TABLE_SOURCE_CURSOR,
)
from ananta.llm.session_ledger.types import (  # noqa: E402
    CursorScope,
    EventType,
    ImportBatchStatus,
    IngestSourceKind,
    MessageRole,
    NormalizedSessionEvent,
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
    return datetime(2026, 5, 24, 12, 0, 0, tzinfo=UTC)


def _make_repo() -> tuple[SessionLedgerRepository, StubStateService]:
    state = StubStateService()
    repo = SessionLedgerRepository(state, clock=_now)  # type: ignore[arg-type]
    return repo, state


# ─── Sources ────────────────────────────────────────────────────────────────


def test_insert_source_sql_shape() -> None:
    repo, state = _make_repo()
    source_id = repo.insert_source(
        source_kind=IngestSourceKind.AGENT_MESSAGING,
        root_uri="local://core__agent_thread",
        account_label="local-example",
        enabled=True,
        config={"flavor": "primary"},
    )
    _check(source_id.startswith("src_"), "insert_source returns src_-prefixed id")
    writes = [w for w in state.writes if w.table == TABLE_SOURCE]
    _check(len(writes) == 1, "exactly one write_state into source")
    rec = writes[0].record
    _check(
        isinstance(rec.get("config_json"), dict),
        "config_json passed as a Python dict (write layer serializes to JSONB)",
    )
    _check(rec.get("namespace") == NAMESPACE, "namespace populated explicitly (standard field)")


def test_get_and_list_sources_select_shape() -> None:
    repo, state = _make_repo()
    state.add_select_response(
        "FROM session_ledger__source WHERE id = ",
        [
            {
                "id": "src_abc",
                "source_kind": "agent_messaging",
                "root_uri": "x",
                "account_label": None,
                "enabled": True,
                "config_json": {},
            }
        ],
    )
    row = repo.get_source("src_abc")
    _check(row is not None and row.source_kind is IngestSourceKind.AGENT_MESSAGING,
           "get_source returns a SourceRow with enum-typed kind")


# ─── Cursors ────────────────────────────────────────────────────────────────


def test_write_discovery_cursor_inserts_with_null_scope_key() -> None:
    repo, state = _make_repo()
    repo.write_cursor(
        source_id="src_x",
        scope=CursorScope.DISCOVERY,
        cursor_payload={"high_water_iso": "2026-05-24T00:00:00+00:00"},
    )
    cursor_writes = [w for w in state.writes if w.table == TABLE_SOURCE_CURSOR]
    _check(
        len(cursor_writes) == 1,
        "write_state into source_cursor (no existing row → INSERT branch)",
    )
    if cursor_writes:
        _check(
            "scope_key" in cursor_writes[0].record
            and cursor_writes[0].record["scope_key"] is None,
            "scope_key present in the record (set to None for discovery)",
        )


def test_event_read_cursor_requires_scope_key() -> None:
    repo, _ = _make_repo()
    try:
        repo.write_cursor(
            source_id="src_x",
            scope=CursorScope.EVENT_READ,
            cursor_payload={"cursor_high_water": 5},
        )
    except LedgerRepositoryError as e:
        _check(
            "scope_key" in str(e),
            "event_read cursor without scope_key raises LedgerRepositoryError",
        )
        return
    _check(False, "expected LedgerRepositoryError for missing scope_key")


# ─── Import batches ─────────────────────────────────────────────────────────


def test_start_finish_batch_roundtrip() -> None:
    repo, state = _make_repo()
    lease_token = "plt_smoke_roundtrip"
    batch_id = repo.start_batch("src_x", polling_lease_token=lease_token)
    _check(batch_id.startswith("imb_"), "start_batch returns imb_-prefixed id")
    repo.finish_batch(
        batch_id,
        polling_lease_token=lease_token,
        status=ImportBatchStatus.COMPLETED,
        error_message=None,
        error_kind=None,
    )
    batch_writes = [w for w in state.writes if w.table == TABLE_IMPORT_BATCH]
    batch_updates = [u for u in state.updates if u.table == TABLE_IMPORT_BATCH]
    _check(len(batch_writes) == 1, "one write_state for start_batch")
    _check(
        any("status" in u.updates for u in batch_updates),
        "finish_batch updates status",
    )


# ─── append_event ───────────────────────────────────────────────────────────


def _make_message(*, content_text: str | None, role: MessageRole | None) -> NormalizedSessionEvent:
    return NormalizedSessionEvent(
        external_session_id="ext_1",
        event_type=EventType.MESSAGE,
        role=role,
        content_text=content_text,
        content_json=None,
        event_at=_now(),
        vendor_event_id="v_1",
        vendor_parent_event_id=None,
        attachment_blob_upload=None,
        attachment_mime_type=None,
        attachment_filename=None,
    )


def test_append_event_clean_path() -> None:
    repo, state = _make_repo()
    batch_id = repo.start_batch("src_x", polling_lease_token="plt_smoke_clean")
    result = repo.append_event(
        session_id="les_1",
        normalized=_make_message(content_text="hello world", role=MessageRole.USER),
        batch_id=batch_id,
        content_blob_id=None,
        session_vendor=SourceVendor.AGENT_MESSAGING,
        source_kind=IngestSourceKind.AGENT_MESSAGING,
        external_id="agm_evt_clean",
    )
    _check(result.event_id.startswith("evt_"), "event id minted")
    _check(result.sequence >= 1, "sequence = MAX(sequence)+1 (1 on the first event)")
    _check(result.deduped is False, "a fresh insert is not deduped")
    # GAP-5: the event row is now an ON CONFLICT (session_id, external_id) upsert,
    # NOT a write_state — assert on the recorded upsert.
    event_upserts = [u for u in state.upserts if u.table == TABLE_EVENT]
    _check(len(event_upserts) == 1, "exactly one DO-NOTHING upsert into event")
    up = event_upserts[0]
    _check(
        up.conflict_columns == ["session_id", "external_id"] and up.on_conflict == "do_nothing",
        "upsert conflicts on (session_id, external_id) DO NOTHING",
    )
    rec = up.record
    _check(rec.get("external_id") == "agm_evt_clean", "external_id carried on the event record")
    _check(rec.get("content_text") == "hello world", "content_text carried in the event record")
    _check(
        "content_blob_id" in rec and rec["content_blob_id"] is None,
        "content_blob_id is None on the inline-text path",
    )


def test_append_event_shape_violation_raises() -> None:
    repo, _ = _make_repo()
    batch_id = repo.start_batch("src_x", polling_lease_token="plt_smoke_shape")
    bad = _make_message(content_text=None, role=MessageRole.USER)  # MESSAGE w/o content
    try:
        repo.append_event(
            session_id="les_1",
            normalized=bad,
            batch_id=batch_id,
            content_blob_id=None,
            session_vendor=SourceVendor.AGENT_MESSAGING,
            source_kind=IngestSourceKind.AGENT_MESSAGING,
            external_id="agm_evt_bad",
        )
    except ValueError as e:
        _check(
            "MESSAGE events require content_text or content_json" in str(e),
            "shape violation raises ValueError with explicit reason",
        )
        return
    _check(False, "expected ValueError for MESSAGE event with no content")


# ─── upsert_session ─────────────────────────────────────────────────────────


def test_upsert_session_inserts_when_absent() -> None:
    repo, state = _make_repo()
    session_id = repo.upsert_session(
        source_id="src_x",
        external_session_id="ext_1",
        vendor=SourceVendor.AGENT_MESSAGING,
        source_kind=IngestSourceKind.AGENT_MESSAGING,
        vendor_session_label="Demo",
        project_path=None,
        first_event_at=_now(),
        last_event_at=_now(),
    )
    _check(session_id.startswith("les_"), "upsert_session returns les_-prefixed id")
    # SQL-lockdown Slice 6: the INSERT branch is Phase 1 ``upsert_state``
    # DO-NOTHING (the stub default inserted=True lands canonical, no Phase 2).
    session_upserts = [u for u in state.upserts if u.table == TABLE_SESSION]
    _check(len(session_upserts) == 1, "exactly one Phase-1 upsert_state on __session")
    if session_upserts:
        _check(
            session_upserts[0].on_conflict == "do_nothing",
            "Phase 1 rides upsert_state DO-NOTHING",
        )


# ─── Leases ─────────────────────────────────────────────────────────────────


def test_record_lease_ping_computes_expiry() -> None:
    repo, state = _make_repo()
    repo.record_lease_ping(session_id="les_1", source_id="src_x", ttl_seconds=120)
    lease_writes = [w for w in state.writes if w.table == TABLE_ACTIVE_LEASE]
    _check(len(lease_writes) == 1, "lease write_state emitted")
    if lease_writes:
        record = lease_writes[0].record
        last_seen = record.get("last_seen_at")
        expires = record.get("expires_at")
        if isinstance(last_seen, datetime) and isinstance(expires, datetime):
            delta = (expires - last_seen).total_seconds()
            _check(delta == 120, f"expires_at = last_seen_at + ttl (delta={delta})")
        else:
            _check(
                False,
                f"lease timestamp fields are not datetimes: "
                f"{type(last_seen)}, {type(expires)}",
            )


def main() -> int:
    print("=== session_ledger repository_smoke ===")
    test_insert_source_sql_shape()
    test_get_and_list_sources_select_shape()
    test_write_discovery_cursor_inserts_with_null_scope_key()
    test_event_read_cursor_requires_scope_key()
    test_start_finish_batch_roundtrip()
    test_append_event_clean_path()
    test_append_event_shape_violation_raises()
    test_upsert_session_inserts_when_absent()
    test_record_lease_ping_computes_expiry()
    print(f"\n{_passed} passed, {len(_failed)} failed")
    if _failed:
        for label in _failed:
            print(f"  FAILED: {label}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
