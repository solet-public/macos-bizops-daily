#!/usr/bin/env python3
"""Unit smoke for the REL-05 F2 ``agent_role_message`` consumed backfill.

## Why this file is new (2026-08-15)

``role_message_consumed_backfill.py`` shipped with a docstring stating that "S7
exercises it against a real (non-stub) state fake". **There was no such test.**
Searched repo-wide: the only references to ``backfill_role_message_consumed``
were its own definition and the two lines in ``plugin.py`` that import and call
it. A claim of coverage in a docstring is not coverage, and it is worse than
silence — it stops the next reader from looking.

That mattered on 2026-08-15. This backfill reads ``core.agent_role_message`` —
4,346 rows measured — with no limit, on the BOOT path. The default state-read row
bound dropped to 100 that day, so that read is now REFUSED outright. The only
reason it did not take the platform down is that its one-shot marker is already
set on this profile: **a one-shot marker is not a bound, it is a delay.** A
sibling read on the same boot path, one that is not marker-gated, did take
``start_interface`` down, and with it agent messaging and the action queue.

## What this smoke verifies

Against the REAL ActionResult envelope shapes (flat ``data.records`` for reads,
NESTED ``data.result.updated`` for the update, ``data.found`` for the marker):

  - ONLY ``delivered=true, consumed=false`` rows are grandfathered;
  - an undelivered row is LEFT OWED — it was genuinely never delivered and must
    still be delivered, which is the whole point of the predicate;
  - an already-consumed row is untouched (idempotent, so a lost marker is safe);
  - each grandfathered row gets ``consumed=true`` + ``consumed_at`` + ``emit_count=1``;
  - the durable marker gates a second run to a no-op, and is set on success ONLY;
  - the walk stays complete PAST the 100-row read bound (section C).

The fake is ``RealShapeState`` wrapped in ``CapEnforcingState``, which refuses an
over-bound unbounded read exactly as the provider does. Without that wrapper this
smoke would stay green against code that cannot boot.

Run:
    .venv/bin/python3 plugins/agent_messaging_plugin/tests/role_message_consumed_backfill_smoke.py
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT / "ananta" / "src"))
sys.path.insert(0, str(REPO_ROOT / "plugins" / "agent_messaging_plugin" / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

if TYPE_CHECKING:
    from ananta.interfaces.state_management_interface import StateManagementInterface

from _real_state_fake import CapEnforcingState, RealShapeState  # noqa: E402
from ananta.llm.agent_messaging.schema import (  # noqa: E402
    COL_CONSUMED,
    COL_CONSUMED_AT,
    COL_EMIT_COUNT,
    NAMESPACE,
    TABLE_AGENT_ROLE_MESSAGE,
)

from agent_messaging_plugin.role_message_consumed_backfill import (  # noqa: E402
    STATUS_ALREADY_DONE,
    STATUS_COMPLETED,
    backfill_role_message_consumed,
)

_passed = 0
_failed: list[str] = []

_PAGE = 100
_COL_DELIVERED = "delivered"


def _check(condition: object, label: str) -> None:
    global _passed
    if condition:
        _passed += 1
        print(f"  PASS  {label}")
    else:
        _failed.append(label)
        print(f"  FAIL  {label}")


def _seed(
    state: RealShapeState,
    n: int,
    rid: str,
    *,
    delivered: bool,
    consumed: bool,
) -> None:
    """Insert one role-message row with an order-stable cursor pair.

    ``id``/``created_at`` are explicit and zero-padded so page boundaries depend
    on the walk rather than on string collation. ``conflict_columns`` is required:
    with an empty list the fake's conflict test is vacuously true and every seed
    merges into the first row.
    """
    state.upsert_state(
        NAMESPACE,
        {
            "table": TABLE_AGENT_ROLE_MESSAGE,
            "conflict_columns": ["id"],
            "record": {
                "id": rid,
                "created_at": f"2026-08-15T00:00:00.{n:06d}",
                "recipient_kind": "role",
                "recipient_key": f"lane-{n:05d}",
                "message_id": f"msg-{n:05d}",
                _COL_DELIVERED: delivered,
                COL_CONSUMED: consumed,
                COL_EMIT_COUNT: 0,
            },
        },
    )


def _by_id(state: RealShapeState) -> dict[str, dict[str, Any]]:
    return {
        str(r["id"]): r for r in state.rows(NAMESPACE, TABLE_AGENT_ROLE_MESSAGE)
    }


def _semantics() -> None:
    print("A. only delivered-but-unconsumed history is grandfathered")
    state = RealShapeState()
    seeds = [
        ("arm-_a", True, False),   # delivered, not consumed -> grandfather
        ("arm-_b", False, False),  # NEVER delivered -> leave OWED
        ("arm-_c", True, True),    # already consumed -> untouched
        ("arm-_d", True, False),   # grandfather
    ]
    for n, (rid, delivered, consumed) in enumerate(seeds):
        _seed(state, n, rid, delivered=delivered, consumed=consumed)

    repo_state = cast("StateManagementInterface", CapEnforcingState(state))
    result = backfill_role_message_consumed(repo_state)
    updated = sorted(cast("list[str]", result["updated"]))
    _check(result["status"] == STATUS_COMPLETED, "first run -> status=completed")
    _check(
        updated == ["arm-_a", "arm-_d"],
        f"grandfathers ONLY delivered+unconsumed rows (got {updated!r})",
    )

    rows = _by_id(state)
    _check(
        rows["arm-_b"][COL_CONSUMED] is False,
        "an UNDELIVERED row is left owed — it must still be delivered, which is "
        "the distinction the predicate exists to preserve",
    )
    _check(
        rows["arm-_b"].get(COL_EMIT_COUNT) == 0,
        "and its emit_count is untouched, so it still participates in re-emit",
    )
    _check(
        rows["arm-_a"][COL_CONSUMED] is True
        and rows["arm-_a"][COL_EMIT_COUNT] == 1
        and rows["arm-_a"].get(COL_CONSUMED_AT),
        "a grandfathered row gets consumed=true, emit_count=1, and a consumed_at",
    )
    _check(
        rows["arm-_c"][COL_EMIT_COUNT] == 0,
        "an already-consumed row is not re-stamped (idempotent — a lost marker "
        "is a no-op, not a double count)",
    )

    print("\nB. the marker gates a second run to a true no-op")
    again = backfill_role_message_consumed(repo_state)
    _check(
        again["status"] == STATUS_ALREADY_DONE,
        "second run -> already_done (marker-gated)",
    )
    _check(again["updated"] == [], "second run grandfathers nothing")

    print("\nC. set-on-success-only: a failed pass leaves the marker unset")
    # The self-healing claim in the module docstring, measured rather than
    # assumed: if the marker were set before the pass, a fault would strand
    # history half-grandfathered with no re-run.
    faulty = RealShapeState()
    _seed(faulty, 0, "arm-_x", delivered=True, consumed=False)
    faulty.fail_next("update")
    wrapped = cast("StateManagementInterface", CapEnforcingState(faulty))
    raised = False
    try:
        backfill_role_message_consumed(wrapped)
    except Exception:  # noqa: BLE001 — any fault must leave the marker unset
        raised = True
    _check(raised, "a failed update propagates rather than being swallowed")
    recovered = backfill_role_message_consumed(wrapped)
    _check(
        recovered["status"] == STATUS_COMPLETED,
        "the next run re-runs — the marker was never set by the failed pass",
    )
    _check(
        recovered["updated"] == ["arm-_x"],
        "and it completes the work the failed pass did not",
    )


def _past_the_page_boundary() -> None:
    print("\nD. the walk stays complete PAST the 100-row read bound")
    # 250 rows = three pages. The rows needing work are scattered, plus one on
    # the very last row so an off-by-one at the final boundary cannot hide.
    state = RealShapeState()
    total = 250
    expected: list[str] = []
    for n in range(total):
        rid = f"arm-{n:05d}"
        needs = n % 5 == 0
        _seed(state, n, rid, delivered=needs, consumed=False)
        if needs:
            expected.append(rid)
    last = f"arm-{total:05d}"
    _seed(state, total, last, delivered=True, consumed=False)
    expected.append(last)

    repo_state = cast("StateManagementInterface", CapEnforcingState(state))
    result = backfill_role_message_consumed(repo_state)
    updated = sorted(cast("list[str]", result["updated"]))
    _check(
        updated == sorted(expected),
        f"every delivered row is grandfathered across all pages "
        f"(expected {len(expected)}, got {len(updated)})",
    )
    _check(
        any(int(rid.split("-")[1]) > _PAGE for rid in updated),
        "including rows beyond the first page — the walk genuinely paged",
    )
    _check(last in updated, "including the very last row in the table")
    stragglers = [
        r
        for r in state.rows(NAMESPACE, TABLE_AGENT_ROLE_MESSAGE)
        if r.get(_COL_DELIVERED) and not r.get(COL_CONSUMED)
    ]
    _check(
        stragglers == [],
        f"no delivered row is left un-grandfathered — a prefix walk would flood "
        f"re-emits for exactly these (got {len(stragglers)})",
    )


def main() -> int:
    _semantics()
    _past_the_page_boundary()

    print()
    print(f"PASSED: {_passed}")
    print(f"FAILED: {len(_failed)}")
    for label in _failed:
        print(f"  - {label}")
    return 1 if _failed else 0


if __name__ == "__main__":
    sys.exit(main())
