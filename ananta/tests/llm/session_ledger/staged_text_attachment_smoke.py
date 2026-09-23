#!/usr/bin/env python3
"""Smoke test for staged text-attachment ingest (spec §10.10.2, no pytest).

Coverage:

* Clean text attachment: SessionLedgerBlobAdapter.store_attachment_text →
  blob written via blob_storage_service.store_blob, returns the blob_id;
  metadata carries event_id + mime_type + filename.
* Repository.record_attachment is the one persistence path.
* Binary attachment path: SessionLedgerBlobAdapter.store_attachment_binary
  writes via blob_storage_service.store_blob and tags metadata with
  kind=attachment_binary.

Run:
    .venv/bin/python3 ananta/tests/llm/session_ledger/staged_text_attachment_smoke.py
"""

from __future__ import annotations

import sys
from datetime import UTC, datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(REPO_ROOT / "ananta" / "src"))
sys.path.insert(0, str(REPO_ROOT / "ananta" / "tests" / "llm" / "session_ledger"))

from _stub_state_service import (  # noqa: E402
    StubBlobStorageService,
    StubStateService,
)
from ananta.llm.session_ledger.blob_adapter import SessionLedgerBlobAdapter  # noqa: E402
from ananta.llm.session_ledger.repository import SessionLedgerRepository  # noqa: E402
from ananta.llm.session_ledger.schema import NAMESPACE, TABLE_ATTACHMENT  # noqa: E402

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


# ─── Clean path ─────────────────────────────────────────────────────────────


def test_clean_text_attachment_stores_blob() -> None:
    blob = StubBlobStorageService()
    adapter = SessionLedgerBlobAdapter(blob)  # type: ignore[arg-type]
    blob_id = adapter.store_attachment_text(
        content_text="just a friendly README\nwith two lines",
        event_id="evt_1",
        mime_type="text/markdown",
        filename="README.md",
    )
    _check(blob_id.startswith("bmd_"), "store_attachment_text returns bmd_-prefixed id")
    _check(len(blob.blobs) == 1, "exactly one blob written")
    stored = blob.blobs[0]
    _check(stored.namespace == NAMESPACE, "blob namespace = session_ledger")
    _check(stored.metadata.get("kind") == "attachment_text", "metadata kind = attachment_text")
    _check(stored.metadata.get("event_id") == "evt_1", "metadata carries event_id")
    _check(stored.metadata.get("mime_type") == "text/markdown", "metadata carries mime_type")
    _check(stored.metadata.get("filename") == "README.md", "metadata carries filename")


def test_clean_text_attachment_repository_records_clean_row() -> None:
    state = StubStateService()
    repo = SessionLedgerRepository(state, clock=_now)  # type: ignore[arg-type]
    attachment_id = repo.record_attachment(
        event_id="evt_1",
        blob_id="bmd_xyz",
        mime_type="text/plain",
        filename="notes.txt",
        size_bytes=42,
    )
    _check(attachment_id.startswith("atc_"), "atc_-prefixed attachment id")
    attachment_writes = [w for w in state.writes if w.table == TABLE_ATTACHMENT]
    _check(len(attachment_writes) == 1, "one attachment write_state")
    rec = attachment_writes[0].record
    _check(rec["blob_id"] == "bmd_xyz", "blob_id field carries the bmd_ id")
    _check(rec["event_id"] == "evt_1", "event_id field carries the event id")


def test_binary_attachment_metadata_only_scan() -> None:
    """Binary attachments route through the blob adapter; the repository
    records them via record_attachment.
    """
    blob = StubBlobStorageService()
    adapter = SessionLedgerBlobAdapter(blob)  # type: ignore[arg-type]
    blob_id = adapter.store_attachment_binary(
        content=b"\x00\x01\x02\x03",
        event_id="evt_b",
        mime_type="image/png",
        filename="screenshot.png",
    )
    _check(blob_id.startswith("bmd_"), "binary attachment blob written")
    _check(blob.blobs[0].metadata.get("kind") == "attachment_binary", "kind=attachment_binary")


def main() -> int:
    print("=== session_ledger staged_text_attachment_smoke ===")
    test_clean_text_attachment_stores_blob()
    test_clean_text_attachment_repository_records_clean_row()
    test_binary_attachment_metadata_only_scan()
    print(f"\n{_passed} passed, {len(_failed)} failed")
    if _failed:
        for label in _failed:
            print(f"  FAILED: {label}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
