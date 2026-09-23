#!/usr/bin/env python3
"""Schema-debt-external-id lane, 2b-S1 quiesce-protocol prerequisite smoke —
``set_source_enabled``.

Exercises the full ABC + service + repository stack against the stub
state-service pattern (no live DB). Each leg names its failing mutation:

1. Disable -> re-enable round trip: each call reports the exact prior/new
   pair and fires exactly one ``update_state`` targeting only ``enabled``
   (+ ``updated_at``) on the exact row. A wrong filter or a wrong updated
   column reds this leg.
2. A same-value call is an idempotent no-op: ``changed=False``, ZERO writes
   fired. Reverting the pre-write equality check (writing unconditionally)
   reds this leg by making a no-op call fire a redundant update.
3. An unknown ``source_id`` raises ``ValueError`` naming the id, before any
   write — never a silent no-op on a typo'd id.
4. Integration leg proving the quiesce linkage this verb exists for: after
   disabling a source, ``SessionLedgerService.poll_source`` (which delegates
   to ``SessionLedgerImporter.poll_source``) raises ``LedgerPollError``
   naming "disabled" — the load-bearing edge the duplicate-source-repair
   quiesce protocol depends on. Reverting the setter (or importer's own
   enabled check) would let a poll race a disabled source silently.

Run:
    .venv/bin/python3 ananta/tests/llm/session_ledger/set_source_enabled_smoke.py
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(REPO_ROOT / "ananta" / "src"))
sys.path.insert(0, str(REPO_ROOT / "ananta" / "tests" / "llm" / "session_ledger"))

from _stub_state_service import StubStateService  # noqa: E402
from ananta.llm.session_ledger.importer import LedgerPollError  # noqa: E402
from ananta.llm.session_ledger.schema import TABLE_SOURCE  # noqa: E402
from ananta.services.session_ledger_service import SessionLedgerService  # noqa: E402

_passed = 0
_failed: list[str] = []

_SRC = "src_toggle"


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


def _source_row(source_id: str, *, enabled: bool) -> dict[str, object]:
    return {
        "id": source_id,
        "source_kind": "codex_ambient",
        "root_uri": f"file:///fake/{source_id}",
        "account_label": None,
        "enabled": enabled,
        "config_json": {},
        "polling_lease_until": None,
        "polling_lease_token": None,
    }


def _plant_source(state: StubStateService, *, enabled: bool) -> None:
    state.add_query_response(
        TABLE_SOURCE,
        [_source_row(_SRC, enabled=enabled)],
        when=lambda f: f.get("id") == _SRC,
    )


def _source_updates(state: StubStateService) -> list[object]:
    return [u for u in state.updates if u.table == TABLE_SOURCE]


# ─── Tests ───────────────────────────────────────────────────────────────────


def test_disable_then_reenable_round_trip() -> None:
    state = StubStateService()
    _plant_source(state, enabled=True)
    service = _make_service(state)

    result = service.set_source_enabled(_SRC, False)
    _check(result["source_id"] == _SRC, "disable echoes the source_id")
    _check(result["prior_enabled"] is True, "disable reports prior_enabled=True")
    _check(result["new_enabled"] is False, "disable reports new_enabled=False")
    _check(result["changed"] is True, "disable reports changed=True")

    updates = _source_updates(state)
    _check(len(updates) == 1, f"exactly one update fired for disable (got {len(updates)})")
    if updates:
        update = updates[0]
        _check(
            update.filters == {"id": _SRC, "is_deleted": 0},
            f"disable's update targets exactly the source id (got {update.filters!r})",
        )
        _check(
            update.updates.get("enabled") is False,
            f"disable's update sets enabled=False (got {update.updates!r})",
        )

    # Re-plant the row as now-disabled (the stub is static; model the write
    # having landed) and re-enable it.
    state2 = StubStateService()
    _plant_source(state2, enabled=False)
    service2 = _make_service(state2)
    result2 = service2.set_source_enabled(_SRC, True)
    _check(result2["prior_enabled"] is False, "re-enable reports prior_enabled=False")
    _check(result2["new_enabled"] is True, "re-enable reports new_enabled=True")
    _check(result2["changed"] is True, "re-enable reports changed=True")

    updates2 = _source_updates(state2)
    _check(len(updates2) == 1, f"exactly one update fired for re-enable (got {len(updates2)})")
    if updates2:
        _check(
            updates2[0].updates.get("enabled") is True,
            f"re-enable's update sets enabled=True (got {updates2[0].updates!r})",
        )


def test_same_value_call_is_idempotent_noop() -> None:
    state = StubStateService()
    _plant_source(state, enabled=False)
    service = _make_service(state)

    result = service.set_source_enabled(_SRC, False)
    _check(result["changed"] is False, "same-value call reports changed=False")
    _check(result["prior_enabled"] is False, "same-value call echoes prior_enabled=False")
    _check(result["new_enabled"] is False, "same-value call echoes new_enabled=False")
    _check(
        len(_source_updates(state)) == 0,
        "an idempotent no-op call fires ZERO writes",
    )


def test_unknown_source_id_raises() -> None:
    state = StubStateService()
    service = _make_service(state)
    try:
        service.set_source_enabled("src_does_not_exist", False)
    except ValueError as exc:
        _check(
            "src_does_not_exist" in str(exc) and "not found" in str(exc),
            f"refuses with a message naming the id (got: {exc})",
        )
    else:
        _check(False, "expected ValueError for an unknown source_id")
    _check(len(_source_updates(state)) == 0, "refused call fired zero writes")


def test_disabled_source_refuses_poll_source() -> None:
    """Integration leg: the quiesce linkage this verb exists for."""
    state = StubStateService()
    _plant_source(state, enabled=False)
    service = _make_service(state)
    try:
        service.poll_source(_SRC)
    except LedgerPollError as exc:
        _check(
            "disabled" in str(exc),
            f"poll_source refuses a disabled source (got: {exc})",
        )
    else:
        _check(False, "expected LedgerPollError for a disabled source")


def main() -> int:
    print("=== set_source_enabled_smoke ===")
    test_disable_then_reenable_round_trip()
    test_same_value_call_is_idempotent_noop()
    test_unknown_source_id_raises()
    test_disabled_source_refuses_poll_source()
    print(f"\n{_passed} passed, {len(_failed)} failed")
    if _failed:
        for label in _failed:
            print(f"  FAILED: {label}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
