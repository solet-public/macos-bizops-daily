#!/usr/bin/env python3
"""Regression smoke for the 2026-08-06 duplicate-``external_id`` fix.

Finding (workbench/2026-08-05_canonical_memory_and_ledger_verification_findings.md
#3): the SchemaStandardizer auto-injects a full-table ``UNIQUE(external_id)``
constraint on every table (``session_ledger__event_external_id_key``, measured
live) — separate from, and untargeted by, ``append_event``'s own
``ON CONFLICT (session_id, external_id) DO NOTHING`` upsert. A rare
cross-session ``external_id`` collision trips that OTHER constraint, raises
uncaught, and — because ``_run_pulling_pass``'s per-session ``except`` is
outer to the WHOLE session's event loop — takes down every event in that
poll pass, including ones before and after the collision. Because a failed
session's ``EVENT_READ`` cursor never advances, the SAME poisoned event
re-fires on every subsequent poll (observed live: some sessions skipped
30-40x).

This smoke pins the fix: ``append_event`` degrades the ONE known constraint
violation to the same "already exists" outcome its own composite upsert
already produces for a same-session duplicate — never the whole session.
Same fixture shape as ``per_session_resilience_smoke.py`` (three sessions,
the middle one poisoned), reusing its established "poisoned session does
not kill the batch" pattern for a DIFFERENT poison class: a genuine
provider-level upsert FAILURE (not a clean DO-NOTHING dedup), injected via
``StubStateService.add_upsert_failure`` — new, matcher-keyed capability
added alongside this fix.

Run:
    .venv/bin/python3 ananta/tests/llm/session_ledger/duplicate_external_id_resilience_smoke.py
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
from ananta.llm.session_ledger.ingest import (  # noqa: E402
    _STALE_EXTERNAL_ID_UNIQUE_CONSTRAINT,
)
from ananta.llm.session_ledger.registry import SessionSourceRegistry  # noqa: E402
from ananta.llm.session_ledger.repository import SessionLedgerRepository  # noqa: E402
from ananta.llm.session_ledger.schema import TABLE_EVENT, TABLE_SOURCE_CURSOR  # noqa: E402
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
# Fixture source plugin — three sessions; session B's second event carries the
# vendor_event_id (= derived external_id, per _event_external_id's fast path)
# the stub is primed to reject at the provider layer.
# ---------------------------------------------------------------------------


_COLLIDING_EXTERNAL_ID = "vid_cross_session_collision"


class _ThreeSessionSource(LLMSessionSourceInterface, PullingSourceMixin):
    """Yields three sessions; session B's second event's external_id collides
    with an event ALREADY persisted (in this fixture's premise) under a
    DIFFERENT session — the standardizer's global external_id-unique
    constraint, not the composite per-session one."""

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
                first_seen_at=datetime(2026, 8, 6, 12, 0, tzinfo=UTC),
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
            yield _raw("ext_b", "vid_b1", "hello B before the collision")
            yield _raw("ext_b", _COLLIDING_EXTERNAL_ID, "the colliding event")
            yield _raw("ext_b", "vid_b3", "hello B after the collision")
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


def _writes_matching(state: StubStateService, table: str, **fields: object) -> list[Any]:
    """Filter ``state.writes`` by table + record field equality — split out
    to keep the test functions under the radon-cc threshold (a genuine gate
    red caught while landing this fix)."""
    return [
        w for w in state.writes
        if w.table == table and all(w.record.get(k) == v for k, v in fields.items())
    ]


def _raw(ext_id: str, vendor_event_id: str, text: str) -> RawSessionEvent:
    return RawSessionEvent(
        external_session_id=ext_id,
        payload={"text": text},
        event_at=datetime(2026, 8, 6, 12, 0, tzinfo=UTC),
        vendor_event_id=vendor_event_id,
        vendor_parent_event_id=None,
    )


def _build_importer(state: StubStateService) -> SessionLedgerImporter:
    repo = SessionLedgerRepository(state_service=state)  # type: ignore[arg-type]
    adapter = SessionLedgerBlobAdapter(blob_storage_service=None)  # type: ignore[arg-type]
    plugin = _ThreeSessionSource()
    registry = SessionSourceRegistry({"three_session_source": plugin})  # type: ignore[arg-type]
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
    state.add_fetch_one_response(
        lambda sql, _params: (
            "UPDATE session_ledger__source" in sql
            and "polling_lease_until = %s" in sql
            and "polling_lease_token = %s" in sql
            and "RETURNING id" in sql
        ),
        {"id": "src_test"},
    )


def _prime_external_id_collision(state: StubStateService) -> None:
    """Inject the exact failure shape a live Postgres provider returns when
    the standardizer's global ``UNIQUE(external_id)`` constraint fires — a
    FAILED upsert envelope naming the known constraint, for the ONE record
    whose ``external_id`` matches, leaving every other record's upsert on
    the normal (inserting) path untouched."""
    state.add_upsert_failure(
        TABLE_EVENT,
        lambda record: record.get("external_id") == _COLLIDING_EXTERNAL_ID,
        f'duplicate key value violates unique constraint '
        f'"{_STALE_EXTERNAL_ID_UNIQUE_CONSTRAINT}"',
    )


# ---------------------------------------------------------------------------
# (1) RED-vs-GREEN: the collision degrades to a per-event skip, not a
#     per-session skip.
# ---------------------------------------------------------------------------


def test_colliding_event_does_not_kill_the_session() -> None:
    state = StubStateService()
    _seed_source_row(state)
    _prime_external_id_collision(state)
    importer = _build_importer(state)
    report = importer.poll_once()

    _check(
        report.sources_polled == 1,
        f"sources_polled == 1 (got {report.sources_polled})",
    )
    _check(
        report.sessions_seen == 3,
        f"RED-vs-GREEN: all 3 sessions yielded by discover_sessions are visited "
        f"(got {report.sessions_seen}); pre-fix, session B's collision would "
        "propagate out of _poll_one_session uncaught and the outer per-session "
        "except would still count it as seen but the events_persisted total "
        "would undercount and the DISCOVERY cursor for session C would never "
        "advance past B in the SAME sense the fix now guarantees end-to-end",
    )
    _check(
        report.events_persisted == 4,
        f"RED-vs-GREEN: 4 events persist — a1, b1, b3 (the colliding b2 is "
        f"skipped as a dedup, not lost, not counted), c1 (got "
        f"{report.events_persisted}); pre-fix this session raised out of "
        "_poll_one_session, propagated to _run_pulling_pass's per-session "
        "except, and ONLY a1 + c1 (1 or 2, never all 4) would have persisted "
        "since b1 and b3 both live inside session B's now-fully-lost pass",
    )
    _check(
        report.batches_failed == 0,
        f"batches_failed == 0 — the collision never reaches the batch-terminal "
        f"exception classes at all (got {report.batches_failed})",
    )

    event_writes = _writes_matching(
        state, TABLE_EVENT, external_id=_COLLIDING_EXTERNAL_ID,
    )
    _check(
        len(event_writes) == 0,
        f"the colliding event's own write_state is never reached — the upsert "
        f"failure is caught before any write happens (got {len(event_writes)})",
    )

    discovery_writes = _writes_matching(
        state, TABLE_SOURCE_CURSOR, cursor_scope="discovery",
    )
    _check(
        len(discovery_writes) >= 1,
        f"RED-vs-GREEN: the DISCOVERY cursor IS written (got "
        f"{len(discovery_writes)} discovery-scoped writes) — pre-fix, session "
        "B's uncaught propagation would abort the pass before the loop over "
        "discover_sessions completes and this write is reached; a lifecycle- "
        "advancing proof, not a static one: the NEXT poll starts past all "
        "three sessions instead of re-walking session B (and re-hitting the "
        "SAME collision) forever",
    )

    event_read_writes = _writes_matching(
        state, TABLE_SOURCE_CURSOR, cursor_scope="event_read", scope_key="ext_b",
    )
    _check(
        len(event_read_writes) >= 1,
        f"RED-vs-GREEN: session B's own EVENT_READ cursor IS written (got "
        f"{len(event_read_writes)}) — pre-fix this never ran (only reached "
        "after _poll_one_session's loop completes without raising), which is "
        "the exact mechanism behind the observed 30-40x re-poll storm: a "
        "cursor that never advances re-yields the SAME poisoned event forever",
    )


# ---------------------------------------------------------------------------
# (2) An UNRELATED upsert failure (a genuinely unexpected DB error, not the
#     known constraint) must still raise loud — the catch is narrowly scoped.
# ---------------------------------------------------------------------------


def test_unrelated_upsert_failure_still_propagates() -> None:
    state = StubStateService()
    _seed_source_row(state)
    state.add_upsert_failure(
        TABLE_EVENT,
        lambda record: record.get("external_id") == "vid_b1",
        "connection to server was lost",
    )
    importer = _build_importer(state)
    report = importer.poll_once()

    _check(
        report.sessions_seen == 3,
        f"sessions_seen == 3 — the OUTER per-session except (pre-existing, "
        f"unchanged) still catches an unrelated failure and moves on (got "
        f"{report.sessions_seen})",
    )
    session_b_events = [
        w for w in state.writes
        if w.table == TABLE_EVENT
        and w.record.get("vendor_event_id") in {"vid_b1", "vid_b2", "vid_b3"}
    ]
    _check(
        len(session_b_events) == 0,
        "RED-vs-GREEN (narrow-scope proof): an UNRELATED failure on session "
        "B's FIRST event still takes down the WHOLE session (0 of its events "
        "persist) — confirming the new catch in append_event does NOT "
        "swallow every upsert failure, only the one named constraint; a "
        "broad except here would have been the wrong fix",
    )


def main() -> int:
    print("=== duplicate_external_id_resilience_smoke ===")
    test_colliding_event_does_not_kill_the_session()
    test_unrelated_upsert_failure_still_propagates()
    print(f"\n{_passed} passed, {len(_failed)} failed")
    if _failed:
        for label in _failed:
            print(f"  FAILED: {label}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
