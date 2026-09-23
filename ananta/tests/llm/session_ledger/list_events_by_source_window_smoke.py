#!/usr/bin/env python3
"""Service-layer smoke — ``list_events_by_source_window`` (SQL-lockdown Slice 7).

Exercises the ABC + service + repository stack against the stub state-service,
asserting the post-migration single-table contract (Architect-ruled denormalize
retired the 3-table JOIN onto one ``query_ordered`` over ``__event`` using the
``session_vendor`` + ``source_kind`` columns):

1. The service returns the canonical per-row envelope (``event_id``,
   ``session_id``, ``sequence``, ``event_at`` (ISO), ``role``, ``content_text``,
   ``vendor`` (remapped from the row's ``session_vendor``), ``source_kind``).
2. The migrated read composes ONE ``query_ordered`` over ``event`` with the
   denormalized equality filters (``source_kind`` / ``session_vendor``) + the
   ``event_at <= until`` upper bound, ordered ``[[event_at, desc], [id, desc]]``
   — a single table, no JOIN.
3. ``limit`` is clamped to ``[1, 100]`` (Slice 7 narrowed the former 200 to the
   ``query_ordered`` cap).
4. The optional ``vendor`` filter binds ``session_vendor`` (denormalized onto
   the event row — no JOIN).

Behavioral window/boundary faithfulness (the ``since`` post-filter, the
until-anchored DESC paging) is proven against a FILTER-HONORING shim in
``events_window_fold_smoke.py`` + the real provider in
``events_window_migration_live_smoke.py``; this smoke asserts the service-layer
envelope + the typed-read PREDICATE the stub records (it does not model filter
application).

Run:
    .venv/bin/python3 ananta/tests/llm/session_ledger/list_events_by_source_window_smoke.py
"""

from __future__ import annotations

import sys
from datetime import UTC, datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(REPO_ROOT / "ananta" / "src"))
sys.path.insert(0, str(REPO_ROOT / "ananta" / "tests" / "llm" / "session_ledger"))

from _stub_state_service import StubBlobStorageService, StubStateService  # noqa: E402
from ananta.llm.session_ledger.types import IngestSourceKind, SourceVendor  # noqa: E402
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


class _StubPluginManager:
    def __init__(self) -> None:
        self.plugins: dict[str, object] = {}


def _make_service(state: StubStateService) -> SessionLedgerService:
    return SessionLedgerService(
        state_service=state,  # type: ignore[arg-type]
        blob_storage_service=StubBlobStorageService(),  # type: ignore[arg-type]
        plugin_manager=_StubPluginManager(),  # type: ignore[arg-type]
    )


def _event_row(
    *,
    row_id: str,
    session_id: str,
    sequence: int,
    event_at: datetime,
    role: str,
    content_text: str,
    vendor: SourceVendor,
    source_kind: IngestSourceKind,
) -> dict[str, object]:
    # Raw ``__event`` row shape (SELECT *): the migrated read projects ``id`` →
    # ``event_id`` + reads the denormalized ``session_vendor`` / ``source_kind``.
    return {
        "id": row_id,
        "session_id": session_id,
        "sequence": sequence,
        "event_at": event_at,
        "role": role,
        "content_text": content_text,
        "session_vendor": vendor.value,
        "source_kind": source_kind.value,
    }


def _event_calls(state: StubStateService) -> list[object]:
    return [c for c in state.query_ordered_calls if c.table == "event"]


# ─── Tests ───────────────────────────────────────────────────────────────────


def test_returns_events_with_envelope() -> None:
    state = StubStateService()
    state.add_select_response(
        "session_ledger__event",
        [
            _event_row(
                row_id="evt_a", session_id="les_x", sequence=1,
                event_at=datetime(2026, 6, 13, 10, 0, 0, tzinfo=UTC),
                role="user", content_text="first",
                vendor=SourceVendor.CLAUDE_CODE,
                source_kind=IngestSourceKind.CLAUDE_CODE_LOCAL),
            _event_row(
                row_id="evt_b", session_id="les_x", sequence=2,
                event_at=datetime(2026, 6, 13, 11, 0, 0, tzinfo=UTC),
                role="assistant", content_text="second",
                vendor=SourceVendor.CLAUDE_CODE,
                source_kind=IngestSourceKind.CLAUDE_CODE_LOCAL),
        ],
    )
    result = _make_service(state).list_events_by_source_window(
        source_kind=IngestSourceKind.CLAUDE_CODE_LOCAL.value,
        since="2026-06-13T00:00:00+00:00",
        until="2026-06-13T23:59:59+00:00",
        limit=20,
    )
    events = result["events"]
    _check(len(events) == 2, "returns both planted events (both inside the window)")
    if events:
        evt = events[0]
        _check(evt["event_id"] == "evt_a", "envelope renames row id → event_id")
        _check(evt["session_id"] == "les_x", "envelope carries session_id")
        _check(evt["sequence"] == 1, "envelope carries sequence")
        _check(
            evt["event_at"] == "2026-06-13T10:00:00+00:00",
            "envelope's event_at is ISO-formatted",
        )
        _check(evt["role"] == "user" and evt["content_text"] == "first",
               "envelope carries role + content_text")
        _check(
            evt["vendor"] == SourceVendor.CLAUDE_CODE.value,
            "service remaps the row's session_vendor → public vendor field",
        )
        _check(
            evt["source_kind"] == IngestSourceKind.CLAUDE_CODE_LOCAL.value,
            "envelope carries the denormalized source_kind",
        )


def test_single_table_query_ordered_predicate() -> None:
    state = StubStateService()
    state.add_select_response("session_ledger__event", [])
    _make_service(state).list_events_by_source_window(
        source_kind=IngestSourceKind.CHATGPT_EXPORT.value,
        since="2026-06-01T00:00:00+00:00",
        until="2026-06-13T00:00:00+00:00",
        limit=50,
    )
    calls = _event_calls(state)
    _check(len(calls) == 1, "exactly one query_ordered fired over the single __event table")
    if calls:
        call = calls[0]
        _check(
            call.filters.get("source_kind") == IngestSourceKind.CHATGPT_EXPORT.value,
            "source_kind equality filter on the denormalized column",
        )
        evt_at = call.filters.get("event_at")
        _check(
            isinstance(evt_at, dict) and evt_at.get("op") == "lte",
            "event_at carries the single 'lte until' upper bound (the DESC anchor)",
        )
        _check(
            "event_at" not in {k for k in call.filters if k != "event_at"}
            and call.filters.get("event_at") is not None,
            "no second event_at condition (the since lower bound is a Python post-filter)",
        )
        _check(
            call.order_by == [["event_at", "desc"], ["id", "desc"]],
            "order_by is event_at DESC with the id total-order tie-break",
        )


def test_limit_clamped_to_100() -> None:
    state = StubStateService()
    state.add_select_response("session_ledger__event", [])
    _make_service(state).list_events_by_source_window(
        source_kind=IngestSourceKind.CODEX_LOCAL.value,
        since="2026-06-01T00:00:00+00:00",
        until="2026-06-13T00:00:00+00:00",
        limit=9999,
    )
    calls = _event_calls(state)
    _check(len(calls) == 1, "one query_ordered fired")
    if calls:
        _check(
            calls[0].limit == 100,
            "limit clamped to 100 (Slice 7 cap) when caller passes 9999",
        )


def test_vendor_filter_binds_session_vendor() -> None:
    state = StubStateService()
    state.add_select_response("session_ledger__event", [])
    _make_service(state).list_events_by_source_window(
        source_kind=IngestSourceKind.CLAUDE_CODE_LOCAL.value,
        since="2026-06-13T00:00:00+00:00",
        until="2026-06-13T23:59:59+00:00",
        limit=10,
        vendor=SourceVendor.CLAUDE_CODE.value,
    )
    calls = _event_calls(state)
    if calls:
        _check(
            calls[0].filters.get("session_vendor") == SourceVendor.CLAUDE_CODE.value,
            "vendor filter binds the denormalized session_vendor column (no JOIN)",
        )


def main() -> int:
    print("=== list_events_by_source_window smoke ===")
    test_returns_events_with_envelope()
    test_single_table_query_ordered_predicate()
    test_limit_clamped_to_100()
    test_vendor_filter_binds_session_vendor()
    print(f"\nResults: {_passed} passed, {len(_failed)} failed")
    if _failed:
        for label in _failed:
            print(f"  - {label}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
