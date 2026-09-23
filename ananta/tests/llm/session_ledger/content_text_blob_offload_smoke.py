#!/usr/bin/env python3
"""Smoke for the content_text/content_blob_id mutual-exclusion contract.

Background: 2026-06-17 PT. A pre-existing importer bug had `append_event`
writing the full `content_text` payload inline EVEN WHEN the upstream
importer had offloaded the payload to blob storage and supplied a
`content_blob_id`. Multi-MB content_text rows would then drive the
trigram GIN index on __event.content_text into a `work_mem`
`psycopg.errors.ProgramLimitExceeded: out of memory` during `trigger_poll`
once the importer reached a vendor session with oversized output (codex
sessions with large model emissions, agent_messaging threads with very
long peer messages, etc.).

The schema declaration at `schema.py` is explicit:
    "Inline text when len(text) <= CONTENT_INLINE_TEXT_MAX_BYTES.
     Otherwise persisted via content_blob_id."

The fix at `ingest.py:append_event` NULLs `content_text` in the INSERT
when `content_blob_id is not None`. This smoke positively locks that
contract against regression.

Cases:
  A. content_blob_id=None → content_text in INSERT params equals the
     normalized payload (inline path; the common case).
  B. content_blob_id="bmd_xyz" → content_text in INSERT params is None
     (offloaded path; the contract this smoke locks).
  C. content_blob_id is set even when content_text is empty → contract
     still holds: content_text=None in the INSERT.

Project policy: no pytest. Exits 0 on success, 1 on first failure.

Run from repo root:
    .venv/bin/python3 ananta/tests/llm/session_ledger/content_text_blob_offload_smoke.py
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
    SessionLedgerRepository,
)
from ananta.llm.session_ledger.schema import TABLE_EVENT  # noqa: E402
from ananta.llm.session_ledger.types import (  # noqa: E402
    EventType,
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
    return datetime(2026, 6, 17, 12, 0, 0, tzinfo=UTC)


def _normalized(content_text: str) -> NormalizedSessionEvent:
    return NormalizedSessionEvent(
        external_session_id="sess-x",
        event_type=EventType.MESSAGE,
        role=MessageRole.USER,
        content_text=content_text,
        content_json=None,
        event_at=_now(),
        vendor_event_id="vendor-evt-1",
        vendor_parent_event_id=None,
        attachment_blob_upload=None,
        attachment_mime_type=None,
        attachment_filename=None,
    )


def _event_record_for(content_blob_id: str | None, content_text: str) -> dict[str, object]:
    state = StubStateService()
    repo = SessionLedgerRepository(state, clock=_now)  # type: ignore[arg-type]
    repo.append_event(
        session_id="les_abc",
        normalized=_normalized(content_text),
        batch_id="imb_xyz",
        content_blob_id=content_blob_id,
        session_vendor=SourceVendor.CODEX,
        source_kind=IngestSourceKind.CODEX_LOCAL,
        external_id="cdx_evt_offload",
    )
    # GAP-5: the event row lands via the (session_id, external_id) DO-NOTHING
    # upsert now, not write_state.
    event_upserts = [u for u in state.upserts if u.table == TABLE_EVENT]
    assert len(event_upserts) == 1, (
        f"expected exactly one __event upsert (got {len(event_upserts)})"
    )
    return event_upserts[0].record


def test_a_inline_path_writes_content_text() -> None:
    print("A. content_blob_id=None -> content_text in the event record carries the payload")
    rec = _event_record_for(content_blob_id=None, content_text="ping inline")
    _check(rec["content_text"] == "ping inline", f"content_text == inline payload (got {rec['content_text']!r})")
    _check(rec["content_blob_id"] is None, f"content_blob_id IS NULL (got {rec['content_blob_id']!r})")


def test_b_offload_path_nulls_content_text() -> None:
    print("B. content_blob_id set -> content_text in the event record is NULL")
    huge = "x" * 100_000  # 100 KB; way over CONTENT_INLINE_TEXT_MAX_BYTES
    rec = _event_record_for(content_blob_id="bmd_abc123", content_text=huge)
    ctext = rec["content_text"]
    _check(
        ctext is None,
        f"content_text IS NULL on offload (got {type(ctext).__name__}, "
        f"len={len(ctext) if isinstance(ctext, str) else 'n/a'!r})",
    )
    _check(
        rec["content_blob_id"] == "bmd_abc123",
        f"content_blob_id carries the offload pointer (got {rec['content_blob_id']!r})",
    )


def test_c_offload_holds_even_for_empty_text() -> None:
    print("C. content_blob_id set + content_text='' -> still NULL")
    rec = _event_record_for(content_blob_id="bmd_def456", content_text="")
    _check(
        rec["content_text"] is None,
        f"content_text IS NULL when blob_id set even on empty payload (got {rec['content_text']!r})",
    )
    _check(
        rec["content_blob_id"] == "bmd_def456",
        f"content_blob_id preserved (got {rec['content_blob_id']!r})",
    )


def main() -> int:
    print("=== session_ledger content_text_blob_offload_smoke ===")
    for scenario in (
        test_a_inline_path_writes_content_text,
        test_b_offload_path_nulls_content_text,
        test_c_offload_holds_even_for_empty_text,
    ):
        try:
            scenario()
        except AssertionError as e:
            print(f"FAIL: {e}")
            return 1
        except Exception as e:  # noqa: BLE001
            print(f"FAIL (unexpected exception in scenario): {type(e).__name__}: {e}")
            return 1
    print(f"\n{_passed} passed, {len(_failed)} failed")
    if _failed:
        for label in _failed:
            print(f"  FAILED: {label}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
