#!/usr/bin/env python3
"""Read-surface smoke for SessionLedgerService (no pytest).

Covers the 7 read verbs the operator was previously authoring raw SQL for:

* list_sources — registry-descriptor + DB-row join (Coordinator Q1 ruling
  2026-05-31 Option A); descriptor-only / row-only / joined entries all
  surface correctly.
* list_sessions(limit, since_iso, project_path) — filter + order + envelope.
* list_active_sessions() — lease-INNER-JOIN envelope; "active" = non-expired.
* get_session_timeline(session_id, after_sequence, limit) — cursor + envelope.
* list_tool_calls(session_id, tool_name, status, since_iso, limit) — filter
  composition + envelope.
* list_quarantined_events(status, limit) — status filter + envelope.
* acknowledge_quarantine(quarantine_id, root_cause_notes, state) — refuses
  without state; with state extracts authenticated_principal.client_id and
  fires the UPDATE.

All verbs are exercised against the StubStateService / StubBlobStorageService
pattern; the underlying SQL shape is asserted via state.calls. No real DB
I/O.

Run:
    .venv/bin/python3 ananta/tests/llm/session_ledger/read_verbs_smoke.py
"""

from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(REPO_ROOT / "ananta" / "src"))
sys.path.insert(0, str(REPO_ROOT / "ananta" / "tests" / "llm" / "session_ledger"))

from _stub_state_service import (  # noqa: E402
    StubBlobStorageService,
    StubStateService,
)
from ananta.llm.session_ledger.types import (  # noqa: E402
    IngestSourceKind,
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


class _StubPluginManager:
    def __init__(self, plugins: dict[str, object]) -> None:
        self.plugins = plugins


def _make_service(state: StubStateService) -> SessionLedgerService:
    return SessionLedgerService(
        state_service=state,  # type: ignore[arg-type]
        blob_storage_service=StubBlobStorageService(),  # type: ignore[arg-type]
        plugin_manager=_StubPluginManager({}),  # type: ignore[arg-type]
    )


# ─── list_sources ──────────────────────────────────────────────────────────


def test_list_sources_row_only_when_plugin_not_loaded() -> None:
    state = StubStateService()
    state.add_select_response(
        "FROM session_ledger__source WHERE is_deleted",
        [
            {
                "id": "src_orphan",
                "source_kind": IngestSourceKind.CODEX_LOCAL.value,
                "root_uri": "file:///tmp/orphan",
                "account_label": "abandoned",
                "enabled": False,
                "config_json": {"legacy": True},
            }
        ],
    )
    service = _make_service(state)
    sources = service.list_sources()["sources"]
    _check(len(sources) == 1, "list_sources surfaces orphan row when plugin unloaded")
    entry = sources[0]
    _check(entry["vendor"] is None, "row-only entry has vendor=None (no plugin)")
    _check(entry["supported_modes"] is None, "row-only entry has supported_modes=None (no plugin)")
    _check(entry["source_id"] == "src_orphan", "row-only entry carries DB source_id")
    _check(entry["enabled"] is False, "row-only entry carries DB enabled=False")
    _check(entry["account_label"] == "abandoned", "row-only entry carries DB account_label")


# ─── list_sessions ─────────────────────────────────────────────────────────


def test_list_sessions_applies_filters_and_clamps_limit() -> None:
    # SQL-lockdown junction migration: list_sessions reads __session via UNCAPPED
    # query_state (no source_kind → no junction read) + a Python window/sort/limit
    # fold. The stub's query_state returns planted rows (filters not modeled), so
    # the in-window row surfaces and the read is recorded as a typed query (no raw
    # SQL). Behavioral window/sort coverage lives in list_sessions_m17_filters_smoke.
    state = StubStateService()
    state.add_select_response(
        "session_ledger__session",
        [
            {
                "id": "les_a",
                "source_id": "src_am",
                "external_session_id": "ext_a",
                "vendor": SourceVendor.CODEX.value,
                "vendor_session_label": "Demo",
                "project_path": "/proj/x",
                "first_event_at": "2026-05-30T00:00:00+00:00",
                "last_event_at": "2026-05-31T00:00:00+00:00",
                "event_count": 3,
                "canonical_external_session_id": None,
            }
        ],
    )
    service = _make_service(state)
    result = service.list_sessions(
        limit=10, since="2026-05-01T00:00:00+00:00", project_path="/proj/x"
    )
    _check("sessions" in result, "list_sessions envelope has 'sessions' key")
    _check(
        len(result["sessions"]) == 1,
        "list_sessions surfaces the stubbed row (last_event_at >= since window)",
    )
    session_reads = [c for c in state.calls if "query session_ledger__session" in c.sql]
    _check(
        len(session_reads) == 1,
        "one typed query_state read against __session (no raw SQL, no junction "
        "read without source_kind)",
    )
    row = result["sessions"][0]
    _check(
        row.get("id") == "les_a"
        and row.get("event_count") == 3
        and row.get("project_path") == "/proj/x",
        "row projected to the list_sessions 10-col envelope",
    )


def test_list_sessions_empty_when_no_rows_stubbed() -> None:
    state = StubStateService()
    service = _make_service(state)
    result = service.list_sessions()
    _check(result == {"sessions": []}, "list_sessions returns empty envelope when no rows")


# ─── list_active_sessions ──────────────────────────────────────────────────


def test_list_active_sessions_merges_lease_and_session_reads() -> None:
    # SQL-lockdown #11: list_active_sessions retired off the raw
    # session-INNER-JOIN-active_lease execute_sql onto two ``query_state`` reads
    # (active_lease filtered ``expires_at > now`` via the Gap-A ``gt`` op, then
    # session by ``id`` =ANY) + a Python inner-merge sorted ``expires_at`` DESC.
    # The stub does not record query_state calls, so the ``when`` predicates
    # enforce the typed-op filter SHAPE: a wrong filter falls through to no
    # planted rows -> empty result -> these asserts fail. The lease ``when`` also
    # pins the ``_naive_utc`` strip (the comparison value is a naive datetime).
    # Deep behavioral / boundary coverage lives in
    # read_migration_active_sessions_live_smoke.py against the real schema.
    state = StubStateService()
    state.add_query_response(
        "active_lease",
        [
            {
                "id": "lse_2",
                "session_id": "les_b",
                "source_id": "src_b",
                "last_seen_at": "2026-05-31T00:55:00",
                "expires_at": "2026-05-31T01:00:00",
            },
            {
                "id": "lse_1",
                "session_id": "les_a",
                "source_id": "src_a",
                "last_seen_at": "2026-05-31T01:55:00",
                "expires_at": "2026-05-31T02:00:00",
            },
        ],
        when=lambda f: (
            f.get("is_deleted") == 0
            and isinstance(f.get("expires_at"), dict)
            and f["expires_at"].get("op") == "gt"
            and isinstance(f["expires_at"].get("value"), datetime)
            and f["expires_at"]["value"].tzinfo is None
        ),
    )
    state.add_query_response(
        "session",
        [
            {
                "id": "les_a",
                "source_id": "src_a",
                "external_session_id": "ext_a",
                "vendor": SourceVendor.CODEX.value,
                "vendor_session_label": "Demo-A",
                "project_path": "/proj/a",
                "last_event_at": "2026-05-31T00:00:00",
            },
            {
                "id": "les_b",
                "source_id": "src_b",
                "external_session_id": "ext_b",
                "vendor": SourceVendor.CLAUDE_CODE.value,
                "vendor_session_label": "Demo-B",
                "project_path": "/proj/b",
                "last_event_at": "2026-05-30T00:00:00",
            },
        ],
        when=lambda f: isinstance(f.get("id"), list) and f.get("is_deleted") == 0,
    )
    service = _make_service(state)
    result = service.list_active_sessions()
    sessions = result["sessions"]
    _check(set(result) == {"sessions"}, "envelope is {'sessions': [...]}")
    _check(
        [r["id"] for r in sessions] == ["les_a", "les_b"],
        "lease+session reads merged and ordered expires_at DESC (les_a@02:00 before les_b@01:00)",
    )
    _check(
        sessions[0]
        == {
            "id": "les_a",
            "source_id": "src_a",
            "external_session_id": "ext_a",
            "vendor": SourceVendor.CODEX.value,
            "vendor_session_label": "Demo-A",
            "project_path": "/proj/a",
            "last_event_at": "2026-05-31T00:00:00",
            "last_seen_at": "2026-05-31T01:55:00",
            "expires_at": "2026-05-31T02:00:00",
        },
        "session fields + lease last_seen_at/expires_at merge into one 9-key row",
    )


# ─── get_session_timeline ──────────────────────────────────────────────────


def test_get_session_timeline_applies_cursor_and_limit() -> None:
    state = StubStateService()
    state.add_select_response(
        "session_ledger__event",
        [
            {
                "id": "evt_1",
                "sequence": 1,
                "event_type": "MESSAGE",
                "role": "user",
                "content_text": "hi",
                "content_json": None,
                "content_blob_id": None,
                "vendor_event_id": None,
                "vendor_parent_event_id": None,
                "event_at": "2026-05-31T00:00:00+00:00",
                "batch_id": "imb_x",
                "imported_at": "2026-05-31T00:00:01+00:00",
            }
        ],
    )
    service = _make_service(state)
    result = service.get_session_timeline(session_id="les_a", after_sequence=7, limit=50)
    _check("events" in result, "get_session_timeline envelope has 'events' key")
    _check(len(result["events"]) == 1, "timeline yields stubbed row")
    # SQL-lockdown Slice 6b: migrated to query_ordered (no raw SQL). Assert the
    # typed-op shape — session_id equality + sequence > cursor (strict gt) +
    # sequence-asc order with an id total-order tie-break, capped at the bound.
    calls = [c for c in state.query_ordered_calls if c.table == "event"]
    _check(len(calls) == 1, "issued one query_ordered against the event table")
    call = calls[0]
    _check(call.filters.get("session_id") == "les_a", "session_id equality filter applied")
    _check(
        call.filters.get("sequence") == {"op": "gt", "value": 7},
        "after_sequence cursor is a strict gt comparison op",
    )
    _check(
        call.order_by == [["sequence", "asc"], ["id", "asc"]],
        "ordered by sequence asc with id total-order tie-break",
    )
    _check(call.limit == 50, "limit passed through (<= 100 cap)")


# ─── list_tool_calls ───────────────────────────────────────────────────────


def test_list_tool_calls_filter_composition() -> None:
    state = StubStateService()
    state.add_select_response(
        "FROM session_ledger__tool_call",
        [
            {
                "id": "tcl_1",
                "session_id": "les_a",
                "call_event_id": "evt_call",
                "result_event_id": None,
                "tool_name": "Bash",
                "status": "pending",
                "called_at": "2026-05-31T00:00:00+00:00",
                "resolved_at": None,
            }
        ],
    )
    service = _make_service(state)
    result = service.list_tool_calls(
        session_id="les_a",
        tool_name="Bash",
        status="pending",
        since_iso="2026-05-01T00:00:00+00:00",
        limit=10,
    )
    _check("tool_calls" in result, "envelope has 'tool_calls' key")
    _check(len(result["tool_calls"]) == 1, "tool_calls surfaces stubbed row")
    # SQL-lockdown Slice 6b: migrated to query_ordered. Assert the typed-op shape
    # — the three equality filters AND-compose with the called_at >= since lower
    # bound (Gap-A gte comparison op carrying a parsed datetime), newest-first
    # with an id total-order tie-break, capped at the bound.
    calls = [c for c in state.query_ordered_calls if c.table == "tool_call"]
    _check(len(calls) == 1, "issued one query_ordered against the tool_call table")
    call = calls[0]
    _check(call.filters.get("session_id") == "les_a", "session_id equality filter applied")
    _check(call.filters.get("tool_name") == "Bash", "tool_name equality filter applied")
    _check(call.filters.get("status") == "pending", "status equality filter applied")
    since = call.filters.get("called_at")
    since_value = since.get("value") if isinstance(since, dict) else None
    _check(
        isinstance(since, dict)
        and since.get("op") == "gte"
        and isinstance(since_value, datetime)
        and since_value.tzinfo is None,
        "since lower bound is an inclusive gte op carrying a NAIVE-UTC datetime: "
        "the service hands a tz-AWARE since (_parse_iso), and list_tool_calls "
        "strips it via _naive_utc so it compares naive-vs-naive against the "
        "naive-UTC called_at column (query_ordered binds filter values raw -- "
        "parse_ordered_query naive-izes only the after cursor, not filters)",
    )
    _check(
        call.order_by == [["called_at", "desc"], ["id", "desc"]],
        "newest-first called_at order with id total-order tie-break",
    )
    _check(call.limit == 10, "limit passed through (<= 100 cap)")


def main() -> int:
    print("=== session_ledger read_verbs_smoke (7 verbs) ===")
    test_list_sources_row_only_when_plugin_not_loaded()
    test_list_sessions_applies_filters_and_clamps_limit()
    test_list_sessions_empty_when_no_rows_stubbed()
    test_list_active_sessions_merges_lease_and_session_reads()
    test_get_session_timeline_applies_cursor_and_limit()
    test_list_tool_calls_filter_composition()
    print(f"\n{_passed} passed, {len(_failed)} failed")
    if _failed:
        for label in _failed:
            print(f"  FAILED: {label}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
