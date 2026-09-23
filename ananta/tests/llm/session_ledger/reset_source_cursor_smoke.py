#!/usr/bin/env python3
"""Cycle 4b D13 smoke — ``reset_source_cursor``.

Exercises the full ABC + service + repository stack against the stub
state-service pattern, asserting:

1. ``confirm=False`` returns a structured dry-run envelope reporting
   the ``active_cursor_count_before`` count signal and zero
   ``deleted_count`` (matches Claude-C's ``backfill_summary_*``
   dry-run shape).
2. ``confirm=True`` with active cursors fires a HARD delete
   (``soft_delete=False``) and reports the correct ``deleted_count``.
3. Idempotent path: re-running on a source with no active cursors
   returns ``deleted_count=0`` without firing the UPDATE.

Run:
    .venv/bin/python3 ananta/tests/llm/session_ledger/reset_source_cursor_smoke.py
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(REPO_ROOT / "ananta" / "src"))
sys.path.insert(0, str(REPO_ROOT / "ananta" / "tests" / "llm" / "session_ledger"))

from _stub_state_service import StubBlobStorageService, StubStateService  # noqa: E402
from ananta.llm.session_ledger.schema import TABLE_SOURCE_CURSOR  # noqa: E402
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


def _plant_cursors(state: StubStateService, *, count: int) -> None:
    """Plant ``count`` live ``__source_cursor`` rows for the count read.

    ``count_active_source_cursors`` now rides ``query_state`` + ``len`` (no raw
    ``COUNT(*)``); the stub's planted-row shim returns these for the
    ``session_ledger__source_cursor`` table, so ``len`` reports ``count``.
    """
    state.add_select_response(
        "session_ledger__source_cursor",
        [
            {
                "id": f"scu_{i}",
                "source_id": "src_test",
                "cursor_scope": "discovery",
                "is_deleted": 0,
            }
            for i in range(count)
        ],
    )


# ─── Tests ───────────────────────────────────────────────────────────────────


def _cursor_deletes(state: StubStateService) -> list[object]:
    return [d for d in state.deletes if d.table == TABLE_SOURCE_CURSOR]


def test_dry_run_reports_active_cursor_count() -> None:
    state = StubStateService()
    _plant_cursors(state, count=5)
    service = _make_service(state)
    result = service.reset_source_cursor(source_id="src_test", confirm=False)
    _check(result["confirmed"] is False, "dry-run reports confirmed=False")
    _check(result["deleted_count"] == 0, "dry-run reports deleted_count=0")
    _check(
        result["source_id"] == "src_test",
        "dry-run echoes the supplied source_id",
    )
    _check(
        result["active_cursor_count_before"] == 5,
        "dry-run reports active_cursor_count_before from the query_state count",
    )
    _check(len(_cursor_deletes(state)) == 0, "dry-run did NOT fire any delete")
    _check(
        result["null_external_id_count"] == 0 and result["precondition_met"] is True,
        "at 0 nulls the dry-run reports precondition_met=True (reset is safe to confirm)",
    )


def test_confirm_hard_deletes_active_cursors() -> None:
    state = StubStateService()
    _plant_cursors(state, count=3)
    service = _make_service(state)
    result = service.reset_source_cursor(source_id="src_test", confirm=True)
    _check(result["confirmed"] is True, "confirm=True reports confirmed=True")
    _check(result["deleted_count"] == 3, "confirm=True reports deleted_count from before-count")
    _check(
        result["source_id"] == "src_test",
        "confirm=True echoes the supplied source_id",
    )
    deletes = _cursor_deletes(state)
    _check(len(deletes) == 1, "confirm=True fired exactly one hard-delete")
    if deletes:
        delete = deletes[0]
        _check(
            delete.soft_delete is False,  # type: ignore[attr-defined]
            "delete is a HARD delete (soft_delete=False) -- no recovery path for a cursor reset",
        )
        _check(
            delete.filters.get("source_id") == "src_test"  # type: ignore[attr-defined]
            and delete.filters.get("is_deleted") == 0,  # type: ignore[attr-defined]
            "delete filters by source_id AND is_deleted = 0 (targets the live rows)",
        )


def test_idempotent_on_empty_source() -> None:
    state = StubStateService()
    _plant_cursors(state, count=0)
    service = _make_service(state)
    result = service.reset_source_cursor(source_id="src_test", confirm=True)
    _check(result["confirmed"] is True, "confirm=True reports confirmed=True")
    _check(
        result["deleted_count"] == 0,
        "idempotent: empty-source path reports deleted_count=0",
    )
    _check(
        len(_cursor_deletes(state)) == 0,
        "idempotent: empty-source path fires NO delete (early return)",
    )


def test_confirm_refuses_when_null_external_ids_remain() -> None:
    """The dup-window guard (mirrors reset_ingest_state): a re-walk of a source
    with legacy null-``external_id`` events would duplicate them (NULLs DISTINCT
    in the ``(session_id, external_id)`` unique), so ``confirm=True`` must REFUSE
    while any remain — before clearing any cursor. The guard lives on the SERVICE
    verb (the direct operator path), not the repository method."""
    state = StubStateService()
    _plant_cursors(state, count=4)
    state.set_count_result(3)  # 3 null-external_id events remain
    service = _make_service(state)
    raised = False
    try:
        service.reset_source_cursor(source_id="src_test", confirm=True)
    except ValueError as exc:
        raised = True
        _check(
            "null external_id" in str(exc) and "backfill_event_external_ids" in str(exc),
            "guard ValueError names the null-external_id precondition + the backfill remedy",
        )
    _check(raised, "confirm=True REFUSES while null-external_id events remain (dup-window guard)")
    _check(
        len(_cursor_deletes(state)) == 0,
        "guard refuses BEFORE clearing any cursor (no delete fired)",
    )


def test_dry_run_surfaces_null_external_id_precondition() -> None:
    state = StubStateService()
    _plant_cursors(state, count=2)
    state.set_count_result(5)
    service = _make_service(state)
    result = service.reset_source_cursor(source_id="src_test", confirm=False)
    _check(
        result["null_external_id_count"] == 5,
        f"dry-run surfaces null_external_id_count=5 (got {result['null_external_id_count']!r})",
    )
    _check(
        result["precondition_met"] is False,
        "dry-run precondition_met=False when nulls remain",
    )
    _check(len(_cursor_deletes(state)) == 0, "dry-run with nulls present still deletes nothing")


def main() -> int:
    print("=== reset_source_cursor smoke ===")
    test_dry_run_reports_active_cursor_count()
    test_confirm_hard_deletes_active_cursors()
    test_idempotent_on_empty_source()
    test_confirm_refuses_when_null_external_ids_remain()
    test_dry_run_surfaces_null_external_id_precondition()
    print(f"\nResults: {_passed} passed, {len(_failed)} failed")
    if _failed:
        for label in _failed:
            print(f"  - {label}")
        return 1
    return 0


_ = Any


if __name__ == "__main__":
    sys.exit(main())
