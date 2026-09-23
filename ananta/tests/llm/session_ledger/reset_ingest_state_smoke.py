#!/usr/bin/env python3
"""Offline smoke for ``service_interface::session_ledger_service::reset_ingest_state``.

GAP-5 slice 3 reworked this verb from a DESTRUCTIVE content-table truncation into
a NON-DESTRUCTIVE per-source cursor reset: it clears every source's
``session_ledger__source_cursor`` rows so the next poll replays each source and
the live ``(session_id, external_id)`` upsert reconverges — no content is deleted.

KEYSTONE (the proof the verb is non-destructive, replacing the old
"preserved_tables never touched" test): on a confirmed run the service issues
``delete_records`` ONLY against ``source_cursor`` and NEVER against any content
table (session/event/tool_call/attachment/import_batch), and fires no destructive
op on content at all.

The smoke drives the REAL ``SessionLedgerService`` + ``SessionLedgerRepository``
against an in-process stub state service (per [[sandbox-mutating-smokes]] — never
the live DB); the stub models the source rows, per-source cursor rows, and content
row counts the verb reads, and records every state call so the keystone can assert
exactly which tables were deleted from. Plain ``main()`` runner per [[no-pytest]].

Run::

    .venv/bin/python3 ananta/tests/llm/session_ledger/reset_ingest_state_smoke.py
"""

from __future__ import annotations

import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(REPO_ROOT / "ananta" / "src"))

from ananta.llm.session_ledger.repository import (  # noqa: E402
    SessionLedgerRepository,
)
from ananta.llm.session_ledger.types import IngestSourceKind  # noqa: E402
from ananta.services.session_ledger_service.service import (  # noqa: E402
    SessionLedgerService,
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


# Bare physical table names (the form the repository's typed seam passes). The
# content tables the reset PRESERVES — none may EVER appear in a delete.
_CONTENT_TABLES = ("session", "event", "tool_call", "attachment", "import_batch")
_CURSOR_TABLE = "source_cursor"


def _ok_records(records: list[dict[str, object]]) -> dict[str, Any]:
    return {"action_status": "completed", "data": {"records": records}, "error": None}


def _ok_scalar(key: str, value: int) -> dict[str, Any]:
    return {"action_status": "completed", "data": {"result": {key: value}}, "error": None}


# ───── In-process stub ──────────────────────────────────────────────────────


class _StubStateService:
    """Models sources + per-source cursor rows + content counts; records calls.

    Surfaces only the three primitives the non-destructive verb reaches:
    ``query_state`` (list_sources + active-cursor counts), ``delete_records``
    (the cursor clear), and ``count`` (the dry-run preserved-content counts).
    Bare table names match the repository's typed-seam contract.
    """

    def __init__(
        self,
        *,
        cursors_per_source: dict[str, int],
        content_counts: dict[str, int],
        null_external_id_count: int = 0,
    ) -> None:
        self.cursors_per_source = dict(cursors_per_source)
        self.content_counts = dict(content_counts)
        self.null_external_id_count = null_external_id_count
        self.query_calls: list[str] = []
        self.delete_calls: list[str] = []
        self.count_calls: list[str] = []
        self.null_count_calls: list[str] = []

    def _source_row(self, source_id: str, ordinal: int) -> dict[str, object]:
        return {
            "id": source_id,
            "source_kind": IngestSourceKind.CODEX_LOCAL.value,
            "root_uri": f"file:///stub/{source_id}",
            "account_label": None,
            "enabled": True,
            "config_json": {},
            # list_sources sorts by str(created_at); keep it source-stable.
            "created_at": f"2026-06-22T00:00:{ordinal:02d}+00:00",
            "is_deleted": 0,
        }

    def query_state(self, namespace: str, query: dict[str, Any]) -> dict[str, Any]:
        del namespace
        table = str(query["table"])
        self.query_calls.append(table)
        if table == "source":
            rows = [
                self._source_row(source_id, ordinal)
                for ordinal, source_id in enumerate(sorted(self.cursors_per_source))
            ]
            return _ok_records(rows)
        if table == _CURSOR_TABLE:
            source_id = str(query["filters"]["source_id"])
            count = self.cursors_per_source.get(source_id, 0)
            rows: list[dict[str, object]] = [
                {"id": f"cur_{source_id}_{i}", "source_id": source_id}
                for i in range(count)
            ]
            return _ok_records(rows)
        raise AssertionError(f"unexpected query_state table {table!r}")

    def delete_records(self, namespace: str, data: dict[str, Any]) -> dict[str, Any]:
        del namespace
        table = str(data["table"])
        self.delete_calls.append(table)
        if table == _CURSOR_TABLE:
            source_id = str(data["filters"]["source_id"])
            cleared = self.cursors_per_source.get(source_id, 0)
            self.cursors_per_source[source_id] = 0
            return _ok_scalar("deleted", cleared)
        # A delete on anything but the cursor table is a destructive-path
        # regression — the keystone assertions catch it via ``delete_calls``.
        return _ok_scalar("deleted", 0)

    def count(self, namespace: str, data: dict[str, Any]) -> dict[str, Any]:
        del namespace
        table = str(data["table"])
        filters = data.get("filters") or {}
        # The null-external_id precondition count is the only count carrying an
        # ``external_id`` filter; content-table counts carry only ``is_deleted``.
        if "external_id" in filters:
            self.null_count_calls.append(table)
            return _ok_scalar("value", self.null_external_id_count)
        self.count_calls.append(table)
        return _ok_scalar("value", self.content_counts.get(table, 0))


def _build_service(state: _StubStateService) -> SessionLedgerService:
    def _frozen_clock() -> datetime:
        return datetime(2026, 6, 22, 18, 0, tzinfo=UTC)

    repo = SessionLedgerRepository(state_service=state, clock=_frozen_clock)  # type: ignore[arg-type]
    service = SessionLedgerService.__new__(SessionLedgerService)
    service._registry = None  # type: ignore[assignment]
    service._repository = repo  # type: ignore[assignment]
    service._secret_gate = None  # type: ignore[assignment]
    service._blob_adapter = None  # type: ignore[assignment]
    service._importer = None  # type: ignore[assignment]
    service._summary_writer = None  # type: ignore[assignment]
    service._operator_equivalent_check = None
    service._scheduling_service = None
    return service


# ───── (1) Dry-run reports cursors + preserved content WITHOUT deleting ──────


def test_dry_run_reports_cursors_and_preserved_without_deleting() -> None:
    state = _StubStateService(
        cursors_per_source={"src_a": 2, "src_b": 1},
        content_counts={
            "session": 5, "event": 40, "tool_call": 3, "attachment": 1,
            "import_batch": 2,
        },
    )
    service = _build_service(state)
    result = service.reset_ingest_state(confirm=False)
    _check(result["confirmed"] is False, f"dry-run confirmed=False (got {result['confirmed']!r})")
    _check(result["action"] == "cursor_reset_replay", "action names the replay semantics")
    _check(result["content_preserved"] is True, "content_preserved=True on dry-run")
    _check(
        result["deleted_count"] == 0,
        f"dry-run deletes NOTHING (deleted_count=0, got {result['deleted_count']!r})",
    )
    _check(
        result["active_cursor_count_before"] == 3,
        f"dry-run reports the 3 cursors that WOULD clear (got {result['active_cursor_count_before']!r})",
    )
    _check(result["sources_total"] == 2, f"sources_total=2 (got {result['sources_total']!r})")
    # KEYSTONE (dry-run): the dry-run mutates nothing.
    _check(
        state.delete_calls == [],
        f"KEYSTONE(dry-run): NO delete_records fires on a dry-run (got {state.delete_calls})",
    )
    # preserved_content reports each content table's live count (reframed as
    # rows_PRESERVED, never rows_deleted), via one count() per table.
    preserved: Any = result["preserved_content"]
    pc = {row["table"]: row["rows_preserved"] for row in preserved if isinstance(row, dict)}
    expected = {
        "session_ledger__session": 5, "session_ledger__event": 40,
        "session_ledger__tool_call": 3, "session_ledger__attachment": 1,
        "session_ledger__import_batch": 2,
    }
    _check(pc == expected, f"preserved_content reports the 5 content tables' live counts (got {pc})")
    _check(
        state.count_calls == list(_CONTENT_TABLES),
        f"one autocommit count() per preserved content table (got {state.count_calls})",
    )
    _check(
        result["null_external_id_count"] == 0 and result["precondition_met"] is True,
        "at 0 nulls the dry-run reports precondition_met=True (reset is safe to confirm)",
    )


# ───── (2) KEYSTONE — confirm clears ONLY cursors, never content ────────────


def test_confirm_clears_only_cursors_never_content() -> None:
    state = _StubStateService(
        cursors_per_source={"src_a": 2, "src_b": 1},
        content_counts={},
    )
    service = _build_service(state)
    result = service.reset_ingest_state(confirm=True)
    _check(result["confirmed"] is True, f"confirm=True returns confirmed=True (got {result['confirmed']!r})")
    _check(
        result["deleted_count"] == 3,
        f"confirm clears all 3 cursors across 2 sources (got {result['deleted_count']!r})",
    )
    # KEYSTONE: every delete targets the cursor table; NONE targets content.
    _check(
        bool(state.delete_calls) and all(t == _CURSOR_TABLE for t in state.delete_calls),
        f"KEYSTONE: delete_records fires ONLY on {_CURSOR_TABLE!r} (got {state.delete_calls})",
    )
    _check(
        not any(t in _CONTENT_TABLES for t in state.delete_calls),
        f"KEYSTONE: NO content table is ever deleted — non-destructive (got {state.delete_calls})",
    )
    _check(
        state.delete_calls == [_CURSOR_TABLE, _CURSOR_TABLE],
        f"exactly one cursor-delete per source (got {state.delete_calls})",
    )
    # The confirm path is pure cursor-clear — no dry-run preserved-content count.
    _check(
        state.count_calls == [],
        f"confirm path issues NO count() — preserved_content is dry-run-only (got {state.count_calls})",
    )
    _check(
        "preserved_content" not in result,
        "confirm result omits preserved_content (dry-run-only field)",
    )


# ───── (3) Multi-source breakdown + zero-cursor source skips its delete ──────


def test_multi_source_breakdown_skips_zero_cursor_source() -> None:
    state = _StubStateService(
        cursors_per_source={"src_a": 4, "src_b": 0, "src_c": 1},
        content_counts={},
    )
    service = _build_service(state)
    result = service.reset_ingest_state(confirm=True)
    _check(result["sources_total"] == 3, f"all 3 sources processed (got {result['sources_total']!r})")
    _check(
        result["deleted_count"] == 5,
        f"deleted_count sums every source (4+0+1=5, got {result['deleted_count']!r})",
    )
    per = {
        str(row["source_id"]): row["deleted_count"]
        for row in result["per_source"] if isinstance(row, dict)
    }
    _check(
        per == {"src_a": 4, "src_b": 0, "src_c": 1},
        f"per-source deleted_count breakdown (got {per})",
    )
    # src_b has 0 cursors → reset_source_cursor early-returns, no delete fires.
    _check(
        state.delete_calls == [_CURSOR_TABLE, _CURSOR_TABLE],
        f"the zero-cursor source skips its delete (2 deletes for 3 sources, got {state.delete_calls})",
    )


# ───── (4) Empty ledger (no sources) stays valid + non-destructive ──────────


def test_empty_ledger_no_sources() -> None:
    state = _StubStateService(
        cursors_per_source={},
        content_counts={"session": 0, "event": 0, "tool_call": 0, "attachment": 0, "import_batch": 0},
    )
    service = _build_service(state)
    confirmed = service.reset_ingest_state(confirm=True)
    _check(confirmed["sources_total"] == 0, "no sources → sources_total=0")
    _check(confirmed["deleted_count"] == 0, "no sources → deleted_count=0")
    _check(confirmed["per_source"] == [], "no sources → empty per_source")
    _check(state.delete_calls == [], "no sources → no delete fires")
    dry = service.reset_ingest_state(confirm=False)
    preserved: Any = dry["preserved_content"]
    _check(
        all(int(row["rows_preserved"]) == 0 for row in preserved if isinstance(row, dict)),
        "empty dry-run reports all content tables at 0 rows_preserved",
    )
    _check(state.delete_calls == [], "empty dry-run still deletes nothing")


# ───── (5) Guard — confirm REFUSES while null-external_id events remain ─────


def test_confirm_refuses_when_null_external_ids_remain() -> None:
    """The slice-3 dup-window guard: a non-destructive reset that left legacy
    null-``external_id`` rows in place would let a re-walk duplicate them (NULLs
    are DISTINCT in the ``(session_id, external_id)`` unique). ``confirm=True``
    must REFUSE while any remain — fail-loud, before clearing any cursor."""
    state = _StubStateService(
        cursors_per_source={"src_a": 2},
        content_counts={},
        null_external_id_count=3,
    )
    service = _build_service(state)
    raised = False
    try:
        service.reset_ingest_state(confirm=True)
    except ValueError as exc:
        raised = True
        _check(
            "null external_id" in str(exc) and "backfill_event_external_ids" in str(exc),
            "guard ValueError names the null-external_id precondition + the backfill remedy",
        )
    _check(raised, "confirm=True RAISES while null-external_id events remain (the dup-window guard)")
    _check(
        state.delete_calls == [],
        f"guard refuses BEFORE clearing any cursor — no delete fired (got {state.delete_calls})",
    )


# ───── (6) Dry-run surfaces the null-external_id precondition ────────────────


def test_dry_run_surfaces_null_external_id_precondition() -> None:
    state = _StubStateService(
        cursors_per_source={"src_a": 1},
        content_counts={"session": 1, "event": 1, "tool_call": 0, "attachment": 0, "import_batch": 0},
        null_external_id_count=5,
    )
    service = _build_service(state)
    result = service.reset_ingest_state(confirm=False)
    _check(
        result["null_external_id_count"] == 5,
        f"dry-run surfaces null_external_id_count=5 (got {result['null_external_id_count']!r})",
    )
    _check(
        result["precondition_met"] is False,
        f"dry-run precondition_met=False when nulls remain (got {result['precondition_met']!r})",
    )
    _check(state.delete_calls == [], "dry-run with nulls present still deletes nothing")
    _check(
        state.null_count_calls == ["event"],
        f"exactly one is_null count() on the event table (got {state.null_count_calls})",
    )


def main() -> int:
    print("=== reset_ingest_state_smoke ===")
    test_dry_run_reports_cursors_and_preserved_without_deleting()
    test_confirm_clears_only_cursors_never_content()
    test_multi_source_breakdown_skips_zero_cursor_source()
    test_empty_ledger_no_sources()
    test_confirm_refuses_when_null_external_ids_remain()
    test_dry_run_surfaces_null_external_id_precondition()
    print(f"\n{_passed} passed, {len(_failed)} failed")
    if _failed:
        for label in _failed:
            print(f"  FAILED: {label}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
