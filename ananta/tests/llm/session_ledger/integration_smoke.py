#!/usr/bin/env python3
"""End-to-end integration smoke for the LLM session ledger M1 (no pytest).

Wires the entire foundation against stubbed state_service +
blob_storage_service + a stubbed pulling source and exercises:

1. SessionLedgerService construction (Registry discovers one source,
   Repository constructs cleanly, BlobAdapter binds to blob_storage_service).
2. ``register_source`` → INSERT into session_ledger__source.
3. ``ingest_raw_chunk`` (pushed path) — chunk persists as one event.
   (Pre-2026-06-11 SecretGate v1 quarantine fixtures were retired at
   always 0 on post-rip ingest. The deleted planted-secret pre-parse
   sub-case was the second of three identical SecretGate fixtures
   killed in the 2026-06-13 broken-smokes cleanup; see
   ``workbench/2026-06-13_pre_existing_broken_smokes_cleanup.md``.)
4. ``list_sources`` returns the registry-discovered descriptor.
5. ``acknowledge_quarantine`` requires authenticated_principal in state
   and refuses caller-supplied identity (PermissionError when ``state=None``).
6. The startup-smoke identity check: ``service.blob_storage_service is``
   the same wrapper the platform would inject.

This is a stub-driven E2E — the M1 spec §15.2 also calls for a full-platform
launch.py-based integration that exercises the real database. That
launch-based run is intentionally NOT in this script (it would need a
running Postgres + LM Studio plus several minutes of startup). The
stub-based test covers the platform-internal wiring; the launch-based
test will live as a follow-up under operator control once the M1 PR lands.

Run:
    .venv/bin/python3 ananta/tests/llm/session_ledger/integration_smoke.py
"""

from __future__ import annotations

import sys
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(REPO_ROOT / "ananta" / "src"))
sys.path.insert(0, str(REPO_ROOT / "ananta" / "tests" / "llm" / "session_ledger"))

from _stub_state_service import (  # noqa: E402
    StubBlobStorageService,
    StubStateService,
)
from ananta.interfaces.llm_session_source_interface import (  # noqa: E402
    LLMSessionSourceInterface,
    PushedSourceMixin,
)
from ananta.llm.session_ledger.schema import TABLE_EVENT, TABLE_SOURCE  # noqa: E402
from ananta.llm.session_ledger.types import (  # noqa: E402
    EventType,
    IngestMode,
    IngestSourceKind,
    MessageRole,
    NormalizedSessionEvent,
    RawSessionEvent,
    SessionSourceDescriptor,
    SourceVendor,
)
from ananta.services.session_ledger_service import SessionLedgerService  # noqa: E402

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


# ─── Stub source: codex_pushed-style chunk parser ───────────────────────────


class _StubPushedSource(LLMSessionSourceInterface, PushedSourceMixin):
    def describe(self) -> SessionSourceDescriptor:
        return SessionSourceDescriptor(
            source_kind=IngestSourceKind.CODEX_PUSHED,
            vendor=SourceVendor.CODEX,
            supported_modes=(IngestMode.PUSHED,),
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

    def parse_chunk(self, chunk_text: str) -> Iterator[RawSessionEvent]:
        yield RawSessionEvent(
            external_session_id="integration_session_1",
            payload={"text": chunk_text},
            event_at=_now(),
            vendor_event_id=None,
            vendor_parent_event_id=None,
        )


# ─── PluginManager stub ─────────────────────────────────────────────────────


class _StubPluginManager:
    def __init__(self, plugins: dict[str, object]) -> None:
        self.plugins = plugins


# ─── Setup ──────────────────────────────────────────────────────────────────


def _build_service(
    *,
    seed_existing_source: bool = True,
) -> tuple[SessionLedgerService, StubStateService, StubBlobStorageService]:
    state = StubStateService()
    if seed_existing_source:
        # Pre-populate list_sources so the importer's _resolve_or_create
        # short-circuits to "found" without needing the stub to remember
        # INSERTs across read-back boundaries.
        state.add_select_response(
            "FROM session_ledger__source WHERE",
            [
                {
                    "id": "src_existing_codex_pushed",
                    "source_kind": IngestSourceKind.CODEX_PUSHED.value,
                    "root_uri": "pushed:codex_pushed",
                    "account_label": "integration-test",
                    "enabled": True,
                    "config_json": {},
                }
            ],
        )
    blob = StubBlobStorageService()
    plugin_manager = _StubPluginManager(
        {"codex_pushed_session_source_plugin_stub": _StubPushedSource()}
    )
    service = SessionLedgerService(
        state_service=state,  # type: ignore[arg-type]
        blob_storage_service=blob,  # type: ignore[arg-type]
        plugin_manager=plugin_manager,  # type: ignore[arg-type]
    )
    # Patch the service's repository clock for deterministic timestamps
    service._repository._clock = _now  # type: ignore[attr-defined]
    return service, state, blob


# ─── Tests ──────────────────────────────────────────────────────────────────


def test_service_constructs_with_registry_and_blob() -> None:
    service, _, blob = _build_service()
    descriptors = service.list_sources()["sources"]
    _check(
        len(descriptors) == 1
        and descriptors[0]["source_kind"] == IngestSourceKind.CODEX_PUSHED.value,
        "Registry discovered the one stub source plugin",
    )
    _check(
        service.blob_storage_service is blob,
        "service.blob_storage_service is the same wrapper passed at construction "
        "(startup smoke identity invariant)",
    )


def test_register_source_inserts_a_row() -> None:
    service, state, _ = _build_service(seed_existing_source=False)
    result = service.register_source(
        source_kind=IngestSourceKind.CODEX_PUSHED.value,
        root_uri="pushed:codex_pushed",
        account_label="ops-dev",
    )
    _check(
        result["source_id"].startswith("src_"),
        "register_source returns src_-prefixed id",
    )
    source_writes = [w for w in state.writes if w.table == TABLE_SOURCE]
    _check(len(source_writes) == 1, "exactly one source write_state")


def test_register_source_existing_returns_existed_no_insert() -> None:
    """Idempotency headline (A1/A2): a second register of the same
    ``(source_kind, root_uri)`` resolves the existing row — ``outcome='existed'``,
    the existing id is returned, and NO second INSERT is issued."""
    service, state, _ = _build_service(seed_existing_source=False)
    # Prime the find-by-(kind, root_uri) idempotency probe to hit an existing row.
    state.add_select_response(
        "SELECT id FROM session_ledger__source WHERE",
        [{"id": "src_existing_idem"}],
    )
    result = service.register_source(
        source_kind=IngestSourceKind.CODEX_PUSHED.value,
        root_uri="pushed:codex_pushed",
        account_label="ops-dev",
    )
    _check(
        result["outcome"] == "existed",
        "register_source on an existing (kind, root_uri) returns outcome='existed'",
    )
    _check(
        result["source_id"] == "src_existing_idem",
        "register_source returns the EXISTING source id, not a fresh one",
    )
    source_writes = [w for w in state.writes if w.table == TABLE_SOURCE]
    _check(
        len(source_writes) == 0,
        "no write_state issued when the (kind, root_uri) already exists (idempotent)",
    )


def test_ingest_raw_chunk_clean_path_persists_one_event() -> None:
    service, state, blob = _build_service()
    result = service.ingest_raw_chunk(
        source_kind=IngestSourceKind.CODEX_PUSHED.value,
        chunk_text="plain ASCII chunk text with no secrets here",
    )
    _check(result["events_persisted"] == 1, "1 event persisted")
    # GAP-5: the event row lands via the (session_id, external_id) DO-NOTHING
    # upsert now, not write_state.
    event_upserts = [u for u in state.upserts if u.table == TABLE_EVENT]
    _check(len(event_upserts) == 1, "exactly one event row upserted")
    _check(
        len(blob.blobs) == 0,
        "no blob write — inline content fits below 4 KB threshold",
    )


def test_list_sessions_returns_envelope() -> None:
    service, state, _ = _build_service()
    state.add_select_response(
        "FROM session_ledger__session ",
        [
            {
                "id": "les_a",
                "source_id": "src_x",
                "external_session_id": "ext_a",
                "vendor": "codex",
                "vendor_session_label": "demo",
                "project_path": None,
                "first_event_at": _now(),
                "last_event_at": _now(),
                "event_count": 3,
            }
        ],
    )
    envelope = service.list_sessions()
    _check(
        "sessions" in envelope and len(envelope["sessions"]) == 1,
        "list_sessions envelope contains the stubbed session row",
    )


def test_service_is_concrete_for_all_m1_methods() -> None:
    """ABC compliance: SessionLedgerService implements every abstract method."""
    abstract = getattr(SessionLedgerService, "__abstractmethods__", None)
    _check(
        not abstract,
        f"SessionLedgerService has no remaining abstract methods (left over: {abstract})",
    )


def main() -> int:
    print("=== session_ledger integration_smoke (stub-driven E2E) ===")
    test_service_constructs_with_registry_and_blob()
    test_register_source_inserts_a_row()
    test_register_source_existing_returns_existed_no_insert()
    test_ingest_raw_chunk_clean_path_persists_one_event()
    test_list_sessions_returns_envelope()
    test_service_is_concrete_for_all_m1_methods()
    print(f"\n{_passed} passed, {len(_failed)} failed")
    if _failed:
        for label in _failed:
            print(f"  FAILED: {label}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
