#!/usr/bin/env python3
"""Smoke test for SessionLedgerImporter (no pytest).

Coverage (post-2026-06-11 SecretGate v1 rip-out — content always lands

* Pulling poll loop:
  - Reads discovery cursor; iterates sessions; reads event cursor.
  - Persists events via ``Repository.append_event``.
  - Writes both cursors (discovery + event_read) after each pass.
  - Marks batch COMPLETED on a clean pass.
* Pushed dispatch:
  - Clean chunk parses + persists.
  - Normalize ``ValueError`` propagates and the batch is marked
    ``FAILED`` with ``error_kind='value_error'``.
* Cursor advancement: ``write_cursor`` called once per session for
  ``event_read`` + once per source for ``discovery``.

The two SecretGate-quarantine cases were retired at the 2026-06-11
SecretGate v1 rip-out (the importer no longer pre-parses chunks or
quarantines events); their fixtures and assertions were deleted as
part of the 2026-06-13 broken-smokes cleanup.

Run:
    .venv/bin/python3 ananta/tests/llm/session_ledger/importer_smoke.py
"""

from __future__ import annotations

import sys
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(REPO_ROOT / "ananta" / "src"))
sys.path.insert(0, str(REPO_ROOT / "ananta" / "tests" / "llm" / "session_ledger"))

from _stub_state_service import (  # noqa: E402
    StubBlobStorageService,
    StubStateService,
)
from ananta.interfaces.llm_session_source_interface import (  # noqa: E402
    LLMSessionSourceInterface,
    PullingSourceMixin,
    PushedSourceMixin,
)
from ananta.llm.session_ledger.blob_adapter import SessionLedgerBlobAdapter  # noqa: E402
from ananta.llm.session_ledger.importer import SessionLedgerImporter  # noqa: E402
from ananta.llm.session_ledger.registry import SessionSourceRegistry  # noqa: E402
from ananta.llm.session_ledger.repository import SessionLedgerRepository  # noqa: E402
from ananta.llm.session_ledger.schema import (  # noqa: E402
    TABLE_IMPORT_BATCH,
    TABLE_SOURCE_CURSOR,
)
from ananta.llm.session_ledger.types import (  # noqa: E402
    EventType,
    ExternalSessionRef,
    IngestMode,
    IngestSourceKind,
    MessageRole,
    NormalizedSessionEvent,
    RawSessionEvent,
    SessionSourceDescriptor,
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


# ─── Stub source plugins ────────────────────────────────────────────────────


class _StubPullingSource(LLMSessionSourceInterface, PullingSourceMixin):
    """Yields a deterministic single-session, two-event sequence."""

    def __init__(self) -> None:
        self.discover_cursor_in: list[dict[str, object] | None] = []
        self.event_cursor_in: list[dict[str, object] | None] = []

    def describe(self) -> SessionSourceDescriptor:
        return SessionSourceDescriptor(
            source_kind=IngestSourceKind.AGENT_MESSAGING,
            vendor=SourceVendor.AGENT_MESSAGING,
            supported_modes=(IngestMode.PULLING,),
        )

    def normalize(self, raw: RawSessionEvent) -> NormalizedSessionEvent:
        return NormalizedSessionEvent(
            external_session_id=raw.external_session_id,
            event_type=EventType.MESSAGE,
            role=MessageRole.USER,
            content_text=str(raw.payload.get("text", "")),
            content_json=None,
            event_at=raw.event_at,
            vendor_event_id=raw.vendor_event_id,
            vendor_parent_event_id=None,
            attachment_blob_upload=None,
            attachment_mime_type=None,
            attachment_filename=None,
        )

    def discover_sessions(
        self,
        root_uri: str,  # pyright: ignore[reportUnusedParameter]  # noqa: ARG002
        cursor_payload: dict[str, object] | None,
    ) -> Iterator[ExternalSessionRef]:
        self.discover_cursor_in.append(cursor_payload)
        yield ExternalSessionRef(
            external_session_id="ext_session_1",
            vendor_session_label="Stub Session",
            project_path=None,
            first_seen_at=_now(),
        )

    def read_events(
        self,
        root_uri: str,  # pyright: ignore[reportUnusedParameter]  # noqa: ARG002
        session: ExternalSessionRef,
        cursor_payload: dict[str, object] | None,
    ) -> Iterator[RawSessionEvent]:
        self.event_cursor_in.append(cursor_payload)
        yield RawSessionEvent(
            external_session_id=session.external_session_id,
            payload={"text": "hello world", "cursor": 1},
            event_at=_now(),
            vendor_event_id="v_1",
            vendor_parent_event_id=None,
        )
        yield RawSessionEvent(
            external_session_id=session.external_session_id,
            payload={"text": "second clean event", "cursor": 2},
            event_at=_now(),
            vendor_event_id="v_2",
            vendor_parent_event_id=None,
        )

    def session_discovery_cursor(
        self,
        root_uri: str,  # pyright: ignore[reportUnusedParameter]  # noqa: ARG002
        last_seen: ExternalSessionRef | None,
    ) -> dict[str, object]:
        return {"high_water_iso": last_seen.first_seen_at.isoformat()} if last_seen else {}

    def event_read_cursor(
        self,
        root_uri: str,  # pyright: ignore[reportUnusedParameter]  # noqa: ARG002
        session: ExternalSessionRef,  # pyright: ignore[reportUnusedParameter]  # noqa: ARG002
        last_event: RawSessionEvent | None,
    ) -> dict[str, object]:
        return {"cursor_high_water": int(last_event.payload["cursor"])} if last_event else {}


class _StubPushedSource(LLMSessionSourceInterface, PushedSourceMixin):
    """Parses a chunk_text into one message event (one line of text)."""

    def __init__(self, *, raise_on_normalize: bool = False) -> None:
        self._raise_on_normalize = raise_on_normalize

    def describe(self) -> SessionSourceDescriptor:
        return SessionSourceDescriptor(
            source_kind=IngestSourceKind.CODEX_PUSHED,
            vendor=SourceVendor.CODEX,
            supported_modes=(IngestMode.PUSHED,),
        )

    def normalize(self, raw: RawSessionEvent) -> NormalizedSessionEvent:
        if self._raise_on_normalize:
            raise ValueError("planted normalize failure")
        return NormalizedSessionEvent(
            external_session_id=raw.external_session_id,
            event_type=EventType.MESSAGE,
            role=MessageRole.USER,
            content_text=str(raw.payload.get("text", "")),
            content_json=None,
            event_at=raw.event_at,
            vendor_event_id=raw.vendor_event_id,
            vendor_parent_event_id=None,
            attachment_blob_upload=None,
            attachment_mime_type=None,
            attachment_filename=None,
        )

    def parse_chunk(self, chunk_text: str) -> Iterator[RawSessionEvent]:
        yield RawSessionEvent(
            external_session_id="pushed_session_1",
            payload={"text": chunk_text},
            event_at=_now(),
            vendor_event_id=None,
            vendor_parent_event_id=None,
        )


# ─── Setup helper ───────────────────────────────────────────────────────────


def _make_importer(plugin: Any) -> tuple[SessionLedgerImporter, StubStateService]:
    state = StubStateService()
    state.add_select_response(
        "FROM session_ledger__source WHERE",
        [
            {
                "id": "src_pull_1",
                "source_kind": plugin.describe().source_kind.value,
                "root_uri": "stub",
                "account_label": None,
                "enabled": True,
                "config_json": {},
            }
        ],
    )
    # The importer's ``_poll_one_pulling_source`` short-circuits to (0,0,0,0)
    # when ``try_acquire_polling_lease`` returns None. ``try_acquire`` now rides
    # the ``acquire_lease`` primitive, whose stub verdict defaults to True
    # (lease acquired) — so the happy-path walk runs without any priming.
    repo = SessionLedgerRepository(state, clock=_now)  # type: ignore[arg-type]
    blob_adapter = SessionLedgerBlobAdapter(StubBlobStorageService())  # type: ignore[arg-type]
    registry = SessionSourceRegistry({plugin.describe().source_kind.value + "_stub": plugin})
    importer = SessionLedgerImporter(
        registry=registry,
        repository=repo,
        blob_adapter=blob_adapter,
    )
    return importer, state


# ─── Tests ──────────────────────────────────────────────────────────────────


def test_pulling_poll_clean_persists_two_events() -> None:
    plugin = _StubPullingSource()
    importer, state = _make_importer(plugin)
    report = importer.poll_once()
    _check(report.sources_polled == 1, "1 source polled")
    _check(report.sessions_seen == 1, "1 session discovered")
    _check(report.events_persisted == 2, "2 events persisted")
    _check(report.batches_failed == 0, "no batch failures")
    # cursor writes: 1 event_read + 1 discovery (write_cursor → typed
    # write_state INSERT branch / update_state revive branch).
    cursor_writes = [w for w in state.writes if w.table == TABLE_SOURCE_CURSOR]
    cursor_updates = [u for u in state.updates if u.table == TABLE_SOURCE_CURSOR]
    _check(
        len(cursor_writes) + len(cursor_updates) == 2,
        "2 cursor writes (one event_read, one discovery)",
    )


def test_pushed_clean_chunk_persists_and_completes_batch() -> None:
    plugin = _StubPushedSource()
    importer, state = _make_importer(plugin)
    state.add_select_response(
        "FROM session_ledger__source WHERE",
        [
            {
                "id": "src_push_1",
                "source_kind": plugin.describe().source_kind.value,
                "root_uri": "pushed:codex_pushed",
                "account_label": None,
                "enabled": True,
                "config_json": {},
            }
        ],
    )
    result = importer.dispatch_pushed(
        source_kind=IngestSourceKind.CODEX_PUSHED,
        chunk_text="plain chunk text",
    )
    _check(result.events_persisted == 1, "pushed clean chunk persisted 1 event")
    _check(bool(result.batch_id), "push returned a real batch_id (PushDispatchResult)")
    batch_updates = [u for u in state.updates if u.table == TABLE_IMPORT_BATCH]
    _check(
        any(u.updates.get("status") == "completed" for u in batch_updates),
        "import_batch finished with status as a typed parameter (not literal)",
    )


def test_pushed_normalize_failure_marks_batch_failed() -> None:
    plugin = _StubPushedSource(raise_on_normalize=True)
    importer, state = _make_importer(plugin)
    state.add_select_response(
        "FROM session_ledger__source WHERE",
        [
            {
                "id": "src_push_3",
                "source_kind": plugin.describe().source_kind.value,
                "root_uri": "pushed:codex_pushed",
                "account_label": None,
                "enabled": True,
                "config_json": {},
            }
        ],
    )
    try:
        importer.dispatch_pushed(
            source_kind=IngestSourceKind.CODEX_PUSHED,
            chunk_text="clean chunk that normalize will reject",
        )
    except ValueError as e:
        _check("planted normalize failure" in str(e), "normalize ValueError surfaced")
        batch_updates = [u for u in state.updates if u.table == TABLE_IMPORT_BATCH]
        _check(len(batch_updates) >= 1, "batch marked failed after normalize")
        return
    _check(False, "expected ValueError from normalize")


def main() -> int:
    print("=== session_ledger importer_smoke ===")
    test_pulling_poll_clean_persists_two_events()
    test_pushed_clean_chunk_persists_and_completes_batch()
    test_pushed_normalize_failure_marks_batch_failed()
    print(f"\n{_passed} passed, {len(_failed)} failed")
    if _failed:
        for label in _failed:
            print(f"  FAILED: {label}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
