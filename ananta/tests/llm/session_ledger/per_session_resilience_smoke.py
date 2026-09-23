#!/usr/bin/env python3
"""Regression smoke locking the 2026-05-31 per-session-resilience fixes.

Bugs from `dispatch:in_flight:ledger_ingest_pass2` (pre-fix behavior):

* Bug 4 — A single session whose content_text exceeded
  ``CONTENT_INLINE_TEXT_MAX_BYTES`` hit a broken
  ``blob_storage_service.store_blob`` wiring path and raised
  ``BlobAdapterError`` mid-batch. The outer try/except in
  ``_poll_one_pulling_source`` caught it, marked the batch FAILED,
  and the loop never advanced past that session. ``trigger_poll``
  capped sessions_seen at the failing session's index every poll.

* Bug 5 — The same failure short-circuited ``write_cursor`` for
  ``CursorScope.DISCOVERY`` (which only runs after the loop
  completes). So ``session_ledger__source_cursor`` had 114 event_read
  rows but ZERO discovery rows; every poll re-iterated from the
  beginning.

* Bug 6 — The same failure also blocked the per-session event_read
  cursor write (which only runs if the inner loop completes). So
  the next poll re-yielded the same events. With no DB-level
  ``UNIQUE(session_id, vendor_event_id)`` constraint, the
  repository gladly inserted them again, producing 3x duplicates of
  ``vendor_event_id agm-_efff7b4ca72842049ab45424d3bea3ef`` per
  poll for session ``agt-_518fdb4ac9784535a3597ffab0f0cd08``.

This smoke pins the new behavior:

1. A poisoned session (its second event raises BlobAdapterError on
   persist) is skipped; sessions BEFORE and AFTER it complete
   normally; the DISCOVERY cursor IS written at the end of the
   pass.
2. When the same session is re-polled, vendor_event_ids that already
   have a row in ``session_ledger__event`` are short-circuited at
   ``find_event_id_by_vendor_id`` — no second insert lands.

Run:
    .venv/bin/python3 ananta/tests/llm/session_ledger/per_session_resilience_smoke.py
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

from _stub_state_service import StubStateService  # noqa: E402
from ananta.interfaces.llm_session_source_interface import (  # noqa: E402
    LLMSessionSourceInterface,
    PullingSourceMixin,
)
from ananta.llm.session_ledger.blob_adapter import (  # noqa: E402
    SessionLedgerBlobAdapter,
)
from ananta.llm.session_ledger.importer import SessionLedgerImporter  # noqa: E402
from ananta.llm.session_ledger.registry import SessionSourceRegistry  # noqa: E402
from ananta.llm.session_ledger.repository import SessionLedgerRepository  # noqa: E402
from ananta.llm.session_ledger.schema import (  # noqa: E402
    TABLE_EVENT,
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


# ---------------------------------------------------------------------------
# Fixture source plugin — three sessions; the middle one poisons.
# ---------------------------------------------------------------------------


_POISON_TEXT = "POISON__force_blob_error"


class _ThreeSessionSource(LLMSessionSourceInterface, PullingSourceMixin):
    """Yields three sessions; session B's second event has content that the
    test's blob adapter rejects, simulating session 132's BlobAdapterError."""

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
        self, root_uri: str, cursor_payload: dict[str, object] | None,
    ) -> Iterator[ExternalSessionRef]:
        del root_uri, cursor_payload
        for ext_id, label in (("ext_a", "session A"), ("ext_b", "session B"),
                              ("ext_c", "session C")):
            yield ExternalSessionRef(
                external_session_id=ext_id,
                vendor_session_label=label,
                project_path=None,
                first_seen_at=datetime(2026, 5, 31, 12, 0, tzinfo=UTC),
            )

    def read_events(
        self, root_uri: str, session: ExternalSessionRef,
        cursor_payload: dict[str, object] | None,
    ) -> Iterator[RawSessionEvent]:
        del root_uri, cursor_payload
        if session.external_session_id == "ext_a":
            yield _raw("ext_a", "vid_a1", "hello A")
            return
        if session.external_session_id == "ext_b":
            yield _raw("ext_b", "vid_b1", "hello B")
            yield _raw("ext_b", "vid_b2", _POISON_TEXT)
            return
        if session.external_session_id == "ext_c":
            yield _raw("ext_c", "vid_c1", "hello C")
            return

    def session_discovery_cursor(
        self, root_uri: str, last_seen: ExternalSessionRef | None,
    ) -> dict[str, object]:
        del root_uri
        if last_seen is None:
            return {}
        return {"last_seen": last_seen.external_session_id}

    def event_read_cursor(
        self, root_uri: str, session: ExternalSessionRef,
        last_event: RawSessionEvent | None,
    ) -> dict[str, object]:
        del root_uri, session
        if last_event is None:
            return {}
        return {"last_vendor_event_id": last_event.vendor_event_id}


def _raw(ext_id: str, vendor_event_id: str, text: str) -> RawSessionEvent:
    return RawSessionEvent(
        external_session_id=ext_id,
        payload={"text": text},
        event_at=datetime(2026, 5, 31, 12, 0, tzinfo=UTC),
        vendor_event_id=vendor_event_id,
        vendor_parent_event_id=None,
    )


# ---------------------------------------------------------------------------
# Failing blob adapter — store_blob succeeds normally, fails on the POISON
# marker so the smoke can drive BlobAdapterError without a real upstream.
# ---------------------------------------------------------------------------


class _PoisonBlobStorageService:
    def __init__(self) -> None:
        self.calls: list[bytes] = []

    def store_blob(
        self, namespace: str, content: bytes, metadata: dict[str, object],
    ) -> dict[str, Any]:
        del namespace, metadata
        self.calls.append(content)
        if _POISON_TEXT.encode("utf-8") in content:
            return {
                "action_status": "error",
                "data": {},
                "actions": [],
                "error": "simulated 'take_action' missing on blob plugin",
                "timestamp": "",
            }
        return {
            "action_status": "completed",
            "data": {"blob_id": f"blob_{len(self.calls)}"},
            "actions": [],
            "error": None,
            "timestamp": "",
        }


class _ForceOffloadAdapter(SessionLedgerBlobAdapter):
    """Forces the poison marker into the offload path so the smoke can drive
    BlobAdapterError without inflating fixture content past 4 KB."""

    def should_offload_text(self, content_text: str) -> bool:
        if _POISON_TEXT in content_text:
            return True
        return super().should_offload_text(content_text)


def _build_importer(state: StubStateService) -> SessionLedgerImporter:
    repo = SessionLedgerRepository(state_service=state)  # type: ignore[arg-type]
    adapter = _ForceOffloadAdapter(
        blob_storage_service=_PoisonBlobStorageService(),  # type: ignore[arg-type]
    )
    plugin = _ThreeSessionSource()
    registry = SessionSourceRegistry({"three_session_source": plugin})  # type: ignore[arg-type]
    # SecretGate v1 was ripped 2026-06-11 — the importer no longer accepts
    # a ``secret_gate=`` kwarg and no scan runs at ingest. events_quarantined
    # is always 0 on the ImporterReport returned by poll_once.
    return SessionLedgerImporter(
        registry=registry, repository=repo, blob_adapter=adapter,
    )


def _seed_source_row(state: StubStateService) -> None:
    state.add_select_response(
        "FROM session_ledger__source WHERE",
        [
            {
                "id": "src_test",
                "source_kind": IngestSourceKind.AGENT_MESSAGING.value,
                "root_uri": "local:agent_messaging",
                "account_label": None,
                "enabled": True,
                "config_json": {},
            }
        ],
    )
    # Prime the W5.Q polling-lease acquisition path so
    # ``_poll_one_pulling_source`` does not short-circuit to (0,0,0,0)
    # before the per-session try/except can fire. Mirrors the
    # ``importer_smoke._make_importer`` canonical primer shape.
    state.add_fetch_one_response(
        lambda sql, _params: (
            "UPDATE session_ledger__source" in sql
            and "polling_lease_until = %s" in sql
            and "polling_lease_token = %s" in sql
            and "RETURNING id" in sql
        ),
        {"id": "src_test"},
    )


# ---------------------------------------------------------------------------
# (1) Per-session try/except — poisoned session skipped, others persist.
# ---------------------------------------------------------------------------


def test_poisoned_session_does_not_kill_batch() -> None:
    state = StubStateService()
    _seed_source_row(state)
    importer = _build_importer(state)
    report = importer.poll_once()
    _check(
        report.sources_polled == 1,
        f"sources_polled == 1 (got {report.sources_polled})",
    )
    _check(
        report.sessions_seen == 3,
        f"all 3 sessions yielded by discover_sessions are visited (got "
        f"{report.sessions_seen}); pre-fix this would stop at the poison "
        "session because the outer try/except aborted the whole batch",
    )
    _check(
        report.events_persisted >= 2,
        f"events_persisted >= 2 (sessions A + C both write at least one event; "
        f"got {report.events_persisted})",
    )
    _check(
        report.batches_failed == 0,
        f"batches_failed == 0 (per-session try/except means the batch is "
        f"COMPLETED with diagnostic logging; got {report.batches_failed})",
    )

    discovery_writes = [
        w for w in state.writes
        if w.table == TABLE_SOURCE_CURSOR
        and w.record.get("cursor_scope") == "discovery"
    ]
    _check(
        len(discovery_writes) >= 1,
        f"DISCOVERY cursor written despite mid-loop poison "
        f"(got {len(discovery_writes)} discovery-scoped writes); pre-fix "
        "this was zero",
    )


# ---------------------------------------------------------------------------
# (2) Idempotency by vendor_event_id — re-poll does not insert duplicates.
# ---------------------------------------------------------------------------


def test_repeated_poll_skips_already_persisted_vendor_events() -> None:
    state = StubStateService()
    _seed_source_row(state)
    # The smoke's StubStateService doesn't actually persist rows the
    # repository wrote during the first poll, so simulate the "already exists"
    # path by adding a canned response to find_event_id_by_vendor_id.
    state.add_select_response(
        "FROM session_ledger__event WHERE session_id = %s AND vendor_event_id = %s",
        [{"id": "evt_pre_existing"}],
    )
    importer = _build_importer(state)
    report = importer.poll_once()
    _check(
        report.events_persisted == 0,
        f"events_persisted == 0 on re-poll where every vendor_event_id is "
        f"already known to the repository (got {report.events_persisted})",
    )
    event_writes = [w for w in state.writes if w.table == TABLE_EVENT]
    _check(
        len(event_writes) == 0,
        f"NO event write_state fires when "
        f"find_event_id_by_vendor_id returns a row (got {len(event_writes)})",
    )


def main() -> int:
    print("=== per_session_resilience_smoke ===")
    test_poisoned_session_does_not_kill_batch()
    test_repeated_poll_skips_already_persisted_vendor_events()
    print(f"\n{_passed} passed, {len(_failed)} failed")
    if _failed:
        for label in _failed:
            print(f"  FAILED: {label}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
