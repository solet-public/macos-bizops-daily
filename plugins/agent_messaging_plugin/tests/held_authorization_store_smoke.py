#!/usr/bin/env python3
"""Unit smoke for ``held_authorization_store.py`` (R1 held-authorization
queue, 2026-08-17, seat GO ruling), against ``RealShapeState`` (real
provider ActionResult envelopes).

Proves the properties the R1 design depends on: a fresh record is open
(``retired_at`` unset) and round-trips through ``list``; the open-only
default filters correctly; ``owed_by_role``/``requesting_peer`` filters
compose; and retirement is predicated on ``retired_at IS NULL`` so a
double-retire is a no-op (``False``, zero rows affected) rather than a
silent overwrite of an earlier retirement's provenance — the exact class of
bug the ``{"op": "is_null"}`` filter grammar exists to prevent (a bare
``None`` filter would match zero rows, silently, forever).

Run:
    .venv/bin/python3 \
        plugins/agent_messaging_plugin/tests/held_authorization_store_smoke.py
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT / "ananta" / "src"))
sys.path.insert(0, str(REPO_ROOT / "plugins" / "agent_messaging_plugin" / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _real_state_fake import RealShapeState  # noqa: E402

from agent_messaging_plugin.held_authorization_store import (  # noqa: E402
    list_held_authorizations,
    record_held_authorization,
    retire_held_authorization,
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


def test_record_round_trips_as_open_entry() -> None:
    state = RealShapeState()
    entry_id = record_held_authorization(
        state,
        requesting_peer="lane-release-pointer",
        owed_by_role="Coordinator",
        branch_or_request_ref="feature/2026-08-17-example",
        reason="citation only, no first-party message in GC's own inbox",
    )
    _check(bool(entry_id), "record_held_authorization returns a non-empty entry_id")
    rows = list_held_authorizations(state, owed_by_role="Coordinator")
    _check(len(rows) == 1, "a fresh record round-trips as exactly one open row")
    _check(
        rows and rows[0]["id"] == entry_id,
        "the round-tripped row's id matches the returned entry_id",
    )
    _check(
        rows and rows[0].get("retired_at") is None,
        "a fresh entry is open — retired_at is unset",
    )
    _check(
        rows and bool(rows[0].get("created_at")),
        "created_at is present and non-empty — the platform's protected "
        "auto-timestamp populated it on insert, since this store never "
        "writes the field itself",
    )


def test_open_only_default_excludes_retired() -> None:
    state = RealShapeState()
    entry_id = record_held_authorization(
        state,
        requesting_peer="lane-x",
        owed_by_role="Coordinator",
        branch_or_request_ref="feature/x",
        reason="test",
    )
    retire_held_authorization(
        state,
        entry_id=entry_id,
        retired_reason="authorized",
        retired_by="Git-Controller",
        retired_at="2026-08-17T12:13:00+00:00",
    )
    open_rows = list_held_authorizations(state, owed_by_role="Coordinator")
    _check(
        len(open_rows) == 0,
        "a retired entry is excluded from the open-only default list — "
        "retired_at IS NULL filtering works, not a bare None comparison",
    )
    all_rows = list_held_authorizations(state, owed_by_role="Coordinator", include_retired=True)
    _check(
        len(all_rows) == 1 and all_rows[0].get("retired_at") == "2026-08-17T12:13:00+00:00",
        "include_retired=True surfaces the retired entry with its retirement stamp",
    )


def test_double_retire_is_a_no_op_not_an_overwrite() -> None:
    """The predicated-update contract: a second retire call must not
    silently overwrite the first retirement's reason/provenance."""
    state = RealShapeState()
    entry_id = record_held_authorization(
        state,
        requesting_peer="lane-y",
        owed_by_role="Git-Controller",
        branch_or_request_ref="feature/y",
        reason="test",
    )
    first = retire_held_authorization(
        state,
        entry_id=entry_id,
        retired_reason="authorized",
        retired_by="Git-Controller",
        retired_at="2026-08-17T05:10:00+00:00",
    )
    second = retire_held_authorization(
        state,
        entry_id=entry_id,
        retired_reason="superseded",  # a would-be overwrite attempt
        retired_by="Coordinator",
        retired_at="2026-08-17T06:00:00+00:00",
    )
    _check(first is True, "the first retire call reports it retired the entry")
    _check(second is False, "a second retire call on an already-retired entry reports False")
    rows = list_held_authorizations(state, owed_by_role="Git-Controller", include_retired=True)
    _check(
        rows and rows[0].get("retired_reason") == "authorized",
        "the SECOND call's retired_reason never overwrote the first — "
        "provenance of the actual retirement survives",
    )


def test_retire_unknown_entry_is_a_no_op() -> None:
    state = RealShapeState()
    result = retire_held_authorization(
        state,
        entry_id="hau_does_not_exist",
        retired_reason="authorized",
        retired_by="Git-Controller",
        retired_at="2026-08-17T05:10:00+00:00",
    )
    _check(result is False, "retiring a non-existent entry_id reports False, not an exception")


def test_filters_compose_with_and() -> None:
    state = RealShapeState()
    record_held_authorization(
        state, requesting_peer="lane-a", owed_by_role="Coordinator",
        branch_or_request_ref="feature/a", reason="test",
    )
    record_held_authorization(
        state, requesting_peer="lane-b", owed_by_role="Coordinator",
        branch_or_request_ref="feature/b", reason="test",
    )
    record_held_authorization(
        state, requesting_peer="lane-a", owed_by_role="Git-Controller",
        branch_or_request_ref="feature/c", reason="test",
    )
    rows = list_held_authorizations(state, owed_by_role="Coordinator", requesting_peer="lane-a")
    _check(
        len(rows) == 1 and rows[0]["requesting_peer"] == "lane-a",
        "owed_by_role and requesting_peer filters compose with AND, "
        "never returning a sibling lane's entry",
    )


def main() -> int:
    test_record_round_trips_as_open_entry()
    test_open_only_default_excludes_retired()
    test_double_retire_is_a_no_op_not_an_overwrite()
    test_retire_unknown_entry_is_a_no_op()
    test_filters_compose_with_and()

    print()
    print(f"PASSED: {_passed}")
    print(f"FAILED: {len(_failed)}")
    for label in _failed:
        print(f"  - {label}")
    return 1 if _failed else 0


if __name__ == "__main__":
    sys.exit(main())
