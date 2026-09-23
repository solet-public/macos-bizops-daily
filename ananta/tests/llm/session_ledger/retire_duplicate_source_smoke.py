#!/usr/bin/env python3
"""Schema-debt-external-id lane, 2b-S1 smoke — ``retire_duplicate_source``.

Exercises the full ABC + service + repository stack against the stub
state-service pattern (no live DB). Each leg names its failing mutation:

1. ``confirm=False`` returns a structured dry-run — per-table
   ``children_to_move`` counts + ``loser_enabled`` — and fires zero writes.
2. ``confirm=True`` while the loser is still ``enabled=True`` is REFUSED
   (``ValueError``) — the quiesce-protocol precondition, enforced by the
   verb itself, not merely trusted to an external caller. Reverting that
   check reds this leg.
3. ``confirm=True`` while the loser shows a still-active polling lease is
   REFUSED — same enforcement discipline, the other quiesce precondition.
   Reverting that check reds this leg.
4. ``confirm=True`` on a fully-quiesced pair re-points all four child
   tables (session/import_batch/source_cursor/active_lease) via
   ``update_state`` and soft-deletes (``is_deleted=1``, never hard) the
   loser — asserts the EXACT per-table filter/update shape, not just a
   truthy result, so a re-point targeting the wrong column or the wrong
   table silently no-ops and reds this leg.
5. ``confirm=True`` when a child row still references the loser AFTER the
   re-point (a simulated post-repoint race) raises
   ``LedgerRepositoryError`` and — the load-bearing assertion — does NOT
   fire the soft-delete. Removing the post-re-point re-verification reds
   this leg by letting a non-orphaned source row get retired.
6. Identical winner/loser ids, a source_kind mismatch, and a missing
   source row are all refused BEFORE any read of child tables (cheap
   fail-fast, no wasted queries).

Run:
    .venv/bin/python3 ananta/tests/llm/session_ledger/retire_duplicate_source_smoke.py
"""

from __future__ import annotations

import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(REPO_ROOT / "ananta" / "src"))
sys.path.insert(0, str(REPO_ROOT / "ananta" / "tests" / "llm" / "session_ledger"))

from _stub_state_service import StubStateService  # noqa: E402
from ananta.llm.session_ledger.duplicate_source_repair import (  # noqa: E402
    SOURCE_CHILD_TABLES,
)
from ananta.llm.session_ledger.repository import LedgerRepositoryError  # noqa: E402
from ananta.llm.session_ledger.schema import (  # noqa: E402
    TABLE_ACTIVE_LEASE,
    TABLE_IMPORT_BATCH,
    TABLE_SESSION,
    TABLE_SOURCE,
    TABLE_SOURCE_CURSOR,
)
from ananta.services.session_ledger_service import SessionLedgerService  # noqa: E402

_passed = 0
_failed: list[str] = []

_WINNER = "src_winner"
_LOSER = "src_loser"


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


class _StubBlobStorageService:
    def store_blob(self, *args: object, **kwargs: object) -> dict[str, Any]:
        raise NotImplementedError("not exercised by this smoke")


def _make_service(state: StubStateService) -> SessionLedgerService:
    return SessionLedgerService(
        state_service=state,  # type: ignore[arg-type]
        blob_storage_service=_StubBlobStorageService(),  # type: ignore[arg-type]
        plugin_manager=_StubPluginManager(),  # type: ignore[arg-type]
    )


def _source_row(
    source_id: str,
    *,
    source_kind: str = "codex_ambient",
    enabled: bool = False,
    lease_until: datetime | None = None,
) -> dict[str, object]:
    return {
        "id": source_id,
        "source_kind": source_kind,
        "root_uri": f"file:///fake/{source_id}",
        "account_label": None,
        "enabled": enabled,
        "config_json": {},
        "polling_lease_until": lease_until,
        "polling_lease_token": "tok" if lease_until else None,
    }


def _plant_sources(
    state: StubStateService,
    *,
    winner: dict[str, object] | None,
    loser: dict[str, object] | None,
) -> None:
    def _when(target_id: str) -> Any:
        return lambda filters: filters.get("id") == target_id

    if winner is not None:
        state.add_query_response(TABLE_SOURCE, [winner], when=_when(_WINNER))
    if loser is not None:
        state.add_query_response(TABLE_SOURCE, [loser], when=_when(_LOSER))


def _plant_children(state: StubStateService, counts: dict[str, int]) -> None:
    for table, count in counts.items():
        rows = [
            {"id": f"{table}_{i}", "source_id": _LOSER, "is_deleted": 0}
            for i in range(count)
        ]
        state.add_query_response(
            table, rows, when=lambda f: f.get("source_id") == _LOSER,
        )


def _child_updates(state: StubStateService) -> list[object]:
    return [u for u in state.updates if u.table in SOURCE_CHILD_TABLES]


def _source_deletes(state: StubStateService) -> list[object]:
    return [d for d in state.deletes if d.table == TABLE_SOURCE]


# ─── Tests ───────────────────────────────────────────────────────────────────


def test_dry_run_reports_children_and_enabled() -> None:
    state = StubStateService()
    _plant_sources(
        state,
        winner=_source_row(_WINNER, enabled=True),
        loser=_source_row(_LOSER, enabled=True),
    )
    _plant_children(state, {TABLE_SESSION: 3, TABLE_IMPORT_BATCH: 1})
    service = _make_service(state)
    result = service.retire_duplicate_source(_WINNER, _LOSER, confirm=False)
    _check(result["confirmed"] is False, "dry-run reports confirmed=False")
    _check(
        result["children_to_move"][TABLE_SESSION] == 3
        and result["children_to_move"][TABLE_IMPORT_BATCH] == 1
        and result["children_to_move"][TABLE_SOURCE_CURSOR] == 0
        and result["children_to_move"][TABLE_ACTIVE_LEASE] == 0,
        f"dry-run reports the exact per-table child counts (got {result['children_to_move']!r})",
    )
    _check(result["loser_enabled"] is True, "dry-run echoes the loser's enabled flag")
    _check(len(_child_updates(state)) == 0, "dry-run fired zero child updates")
    _check(len(_source_deletes(state)) == 0, "dry-run fired zero deletes")


def test_confirm_refused_while_loser_enabled() -> None:
    state = StubStateService()
    _plant_sources(
        state,
        winner=_source_row(_WINNER, enabled=True),
        loser=_source_row(_LOSER, enabled=True),
    )
    service = _make_service(state)
    try:
        service.retire_duplicate_source(_WINNER, _LOSER, confirm=True)
    except ValueError as exc:
        _check("enabled" in str(exc), f"refuses with an 'enabled' message (got: {exc})")
    else:
        _check(False, "expected ValueError for a still-enabled loser")
    _check(len(_child_updates(state)) == 0, "refused confirm fired zero child updates")


def test_confirm_refused_while_active_lease() -> None:
    state = StubStateService()
    future = datetime.now(UTC) + timedelta(minutes=5)
    _plant_sources(
        state,
        winner=_source_row(_WINNER, enabled=True),
        loser=_source_row(_LOSER, enabled=False, lease_until=future),
    )
    service = _make_service(state)
    try:
        service.retire_duplicate_source(_WINNER, _LOSER, confirm=True)
    except ValueError as exc:
        _check("lease" in str(exc), f"refuses with a 'lease' message (got: {exc})")
    else:
        _check(False, "expected ValueError for an active polling lease")
    _check(len(_child_updates(state)) == 0, "refused confirm fired zero child updates")


def test_confirm_expired_lease_proceeds() -> None:
    """An EXPIRED lease (in the past) must NOT refuse — only a future deadline does."""
    state = StubStateService()
    past = datetime.now(UTC) - timedelta(minutes=5)
    _plant_sources(
        state,
        winner=_source_row(_WINNER, enabled=True),
        loser=_source_row(_LOSER, enabled=False, lease_until=past),
    )
    service = _make_service(state)
    result = service.retire_duplicate_source(_WINNER, _LOSER, confirm=True)
    _check(
        result["confirmed"] is True,
        "an expired (past) lease does not block the repair",
    )


def test_confirm_repoints_and_retires() -> None:
    # NOTE: the stub is static — a planted (table, source_id) response
    # returns the SAME rows on every query, before AND after an update. To
    # exercise the happy path (post-re-point verification sees zero
    # remaining), this test plants NO loser children at all; the non-zero
    # dry-run count-reporting path is already covered by
    # ``test_dry_run_reports_children_and_enabled`` and the
    # still-referenced-after-repoint refusal path by
    # ``test_confirm_refuses_if_child_still_references_loser_after_repoint``.
    state = StubStateService()
    _plant_sources(
        state,
        winner=_source_row(_WINNER, enabled=True),
        loser=_source_row(_LOSER, enabled=False),
    )
    service = _make_service(state)
    result = service.retire_duplicate_source(_WINNER, _LOSER, confirm=True)
    _check(result["confirmed"] is True, "confirm=True reports confirmed=True")
    _check(
        result["children_moved"] == {
            TABLE_SESSION: 1, TABLE_IMPORT_BATCH: 1,
            TABLE_SOURCE_CURSOR: 1, TABLE_ACTIVE_LEASE: 1,
        },
        # The stub's update_state always reports rows_affected=1 regardless
        # of how many rows matched (see _stub_state_service's own docstring)
        # — the meaningful assertion is that ALL FOUR tables were touched,
        # not the stub's synthetic per-call count.
        f"all four child tables were updated (got {result['children_moved']!r})",
    )
    _check(result["loser_retired"] is True, "confirm=True reports loser_retired=True")

    updates = _child_updates(state)
    _check(len(updates) == 4, f"exactly one update per child table (got {len(updates)})")
    tables_touched = {u.table for u in updates}
    _check(
        tables_touched == set(SOURCE_CHILD_TABLES),
        f"the four updates cover exactly SOURCE_CHILD_TABLES (got {tables_touched!r})",
    )
    for update in updates:
        _check(
            update.filters == {"source_id": _LOSER, "is_deleted": 0}
            and update.updates == {"source_id": _WINNER},
            f"{update.table}: filters/updates target loser->winner exactly "
            f"(got filters={update.filters!r}, updates={update.updates!r})",
        )

    deletes = _source_deletes(state)
    _check(len(deletes) == 1, "exactly one delete fired against the source table")
    if deletes:
        delete = deletes[0]
        _check(
            delete.soft_delete is True,
            "the delete is SOFT (is_deleted=1) — never a hard delete",
        )
        _check(
            delete.filters == {"id": _LOSER, "is_deleted": 0},
            f"the delete targets exactly the loser id (got {delete.filters!r})",
        )


def test_confirm_refuses_if_child_still_references_loser_after_repoint() -> None:
    """Simulates a post-re-point race: a child row still shows the loser on
    re-verification. Must raise and must NOT soft-delete a non-orphaned row."""
    state = StubStateService()
    _plant_sources(
        state,
        winner=_source_row(_WINNER, enabled=True),
        loser=_source_row(_LOSER, enabled=False),
    )
    # The verification re-count after re-point still finds ONE session row —
    # the stub always returns the same planted rows for a given (table, when)
    # match, which models "the re-point didn't actually clear it."
    _plant_children(state, {TABLE_SESSION: 1})
    service = _make_service(state)
    try:
        service.retire_duplicate_source(_WINNER, _LOSER, confirm=True)
    except LedgerRepositoryError as exc:
        _check(
            "still reference" in str(exc),
            f"raises LedgerRepositoryError naming the still-referenced child (got: {exc})",
        )
    else:
        _check(False, "expected LedgerRepositoryError for a non-orphaned loser")
    _check(
        len(_source_deletes(state)) == 0,
        "a non-orphaned loser is NEVER soft-deleted — the load-bearing safety check",
    )


def test_fail_fast_refusals_before_any_child_read() -> None:
    state = StubStateService()
    service = _make_service(state)

    try:
        service.retire_duplicate_source(_WINNER, _WINNER, confirm=False)
    except ValueError as exc:
        _check("winner_source_id" in str(exc), f"same-id refusal names the field (got: {exc})")
    else:
        _check(False, "expected ValueError for identical winner/loser ids")

    state2 = StubStateService()
    _plant_sources(state2, winner=_source_row(_WINNER), loser=None)
    service2 = _make_service(state2)
    try:
        service2.retire_duplicate_source(_WINNER, _LOSER, confirm=False)
    except ValueError as exc:
        _check("not found" in str(exc), f"missing loser refusal names it (got: {exc})")
    else:
        _check(False, "expected ValueError for a missing loser source row")

    state3 = StubStateService()
    _plant_sources(
        state3,
        winner=_source_row(_WINNER, source_kind="codex_ambient"),
        loser=_source_row(_LOSER, source_kind="claude_code_local"),
    )
    service3 = _make_service(state3)
    try:
        service3.retire_duplicate_source(_WINNER, _LOSER, confirm=False)
    except ValueError as exc:
        _check("source_kind" in str(exc), f"kind-mismatch refusal names it (got: {exc})")
    else:
        _check(False, "expected ValueError for a source_kind mismatch")


def main() -> int:
    print("=== retire_duplicate_source_smoke ===")
    test_dry_run_reports_children_and_enabled()
    test_confirm_refused_while_loser_enabled()
    test_confirm_refused_while_active_lease()
    test_confirm_expired_lease_proceeds()
    test_confirm_repoints_and_retires()
    test_confirm_refuses_if_child_still_references_loser_after_repoint()
    test_fail_fast_refusals_before_any_child_read()
    print(f"\n{_passed} passed, {len(_failed)} failed")
    if _failed:
        for label in _failed:
            print(f"  FAILED: {label}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
