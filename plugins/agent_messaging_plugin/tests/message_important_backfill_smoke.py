#!/usr/bin/env python3
"""Unit smoke for the GAP-2 agent_message ``important`` backfill (SQL-lockdown).

``backfill_message_important`` is a ONE-SHOT, durable-marker-gated projection of
the JSONB ``metadata.important`` flag onto the new first-class ``important``
column. This smoke verifies, against the REAL ActionResult envelope shapes (the
``state_results`` helpers are the production extraction path — flat
``data.records`` for reads, NESTED ``data.result.updated`` for the update,
``data.found`` for the key-value marker):

  - ONLY rows with truthy ``metadata.important`` AND a still-false column flip;
  - the flip uses ``update_state`` (no raw SQL), one call per row;
  - the durable marker gates a second run to a no-op (``already_done``);
  - dual-shape metadata (dict OR JSON-string) is accepted;
  - set-on-success-only is the only thing that suppresses a re-run (self-healing);
  - **the walk stays complete past the 100-row read bound** (section C).

## Two things changed here on 2026-08-15, and both are the point

**It was never registered.** This file existed and was NOT in
``quality_gates/gate_smokes.txt``, which is an allowlist — an unregistered smoke
silently never runs. It has been registered. Every claim below was unverified
until then.

**Its fixture could not reach the bound.** The private ``_FakeState`` it carried
answered every read with the whole table and implemented only ``query_state``,
and its six-row fixture never reached a page boundary. Meanwhile this backfill
reads ``core.agent_message`` — 14,270 rows measured — with no limit, at boot. The
default read bound dropped to 100 that day, so that read is now REFUSED; the only
reason it did not take the platform down is that its one-shot marker is already
set on this profile. **A one-shot marker is not a bound, it is a delay.** A
sibling read on the same boot path, one that is not marker-gated, did take
``start_interface`` down.

So the fake is now ``RealShapeState`` wrapped in ``CapEnforcingState`` (which
refuses an over-bound unbounded read the way the provider does), and section C
uses a fixture that straddles three pages.

Run:
    .venv/bin/python3 plugins/agent_messaging_plugin/tests/message_important_backfill_smoke.py
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
    NAMESPACE,
    TABLE_AGENT_MESSAGE,
)

from agent_messaging_plugin.message_important_backfill import (  # noqa: E402
    STATUS_ALREADY_DONE,
    STATUS_COMPLETED,
    backfill_message_important,
)

_passed = 0
_failed: list[str] = []

_PAGE = 100


def _check(condition: object, label: str) -> None:
    global _passed
    if condition:
        _passed += 1
        print(f"  PASS  {label}")
    else:
        _failed.append(label)
        print(f"  FAIL  {label}")


def _seed(
    state: RealShapeState, n: int, mid: str, *, important_col: bool, metadata: Any,
) -> None:
    """Insert one message row with an order-stable cursor pair.

    ``id``/``created_at`` are supplied explicitly and zero-padded: the fake's
    generated ``gen-{n}`` ids are not padded, so page boundaries would depend on
    string collation rather than on the walk. ``conflict_columns`` is required —
    with an empty list the fake's conflict test is vacuously true and every seed
    merges into the first row.
    """
    state.upsert_state(
        NAMESPACE,
        {
            "table": TABLE_AGENT_MESSAGE,
            "conflict_columns": ["id"],
            "record": {
                "id": mid,
                "created_at": f"2026-08-15T00:00:00.{n:06d}",
                "important": important_col,
                "metadata": metadata,
            },
        },
    )


def _updates(state: RealShapeState, ns: str, table: str) -> list[dict[str, Any]]:
    return state.rows(ns, table)


def _semantics() -> None:
    print("A. only truthy-metadata + still-false-column rows flip")
    state = RealShapeState()
    seeds: list[tuple[str, bool, Any]] = [
        ("agm-_a", False, {"important": True}),     # flip
        ("agm-_b", False, {"important": False}),    # leave
        ("agm-_c", False, {}),                      # leave (absent)
        ("agm-_d", True, {"important": True}),      # already projected
        ("agm-_e", False, '{"important": true}'),   # JSON-string -> flip
        ("agm-_f", False, "{}"),                    # JSON-string empty
    ]
    for n, (mid, col, meta) in enumerate(seeds):
        _seed(state, n, mid, important_col=col, metadata=meta)

    repo_state = cast("StateManagementInterface", CapEnforcingState(state))
    result = backfill_message_important(repo_state)
    flipped = sorted(cast("list[str]", result["updated"]))
    _check(result["status"] == STATUS_COMPLETED, "first run -> status=completed")
    _check(
        flipped == ["agm-_a", "agm-_e"],
        f"first run flips ONLY truthy-metadata + false-column rows (dict + "
        f"JSON-string); leaves false/absent/already-projected (flipped={flipped!r})",
    )
    by_id = {str(r["id"]): r for r in _updates(state, NAMESPACE, TABLE_AGENT_MESSAGE)}
    _check(
        by_id["agm-_a"]["important"] is True,
        "dict-metadata row now carries important=True on the column",
    )
    _check(
        by_id["agm-_e"]["important"] is True,
        "JSON-string-metadata row now carries important=True on the column",
    )
    _check(
        by_id["agm-_b"]["important"] is False and by_id["agm-_c"]["important"] is False,
        "false / absent metadata rows are untouched",
    )

    print("\nB. the marker gates a second run to a true no-op")
    again = backfill_message_important(repo_state)
    _check(
        again["status"] == STATUS_ALREADY_DONE,
        "second run -> already_done (marker-gated)",
    )
    _check(again["updated"] == [], "second run flips nothing")

    fresh = RealShapeState()
    _seed(fresh, 0, "agm-_z", important_col=False, metadata={"important": True})
    fresh_result = backfill_message_important(
        cast("StateManagementInterface", CapEnforcingState(fresh)),
    )
    _check(
        fresh_result["status"] == STATUS_COMPLETED,
        "no marker -> runs (self-healing)",
    )
    _check(
        fresh_result["updated"] == ["agm-_z"],
        "set-on-success-only is the only re-run suppressor",
    )


def _past_the_page_boundary() -> None:
    print("\nC. the walk stays complete PAST the 100-row read bound")
    # 250 rows = three pages, with the rows that need flipping deliberately
    # scattered so a walk that stops early is caught wherever it stops. This is
    # the fixture the pre-2026-08-15 smoke did not have, and its absence is why a
    # six-row green sat alongside a read production now refuses outright.
    state = RealShapeState()
    total = 250
    expected: list[str] = []
    for n in range(total):
        needs_flip = n % 7 == 0
        mid = f"agm-{n:05d}"
        _seed(
            state,
            n,
            mid,
            important_col=False,
            metadata={"important": True} if needs_flip else {"important": False},
        )
        if needs_flip:
            expected.append(mid)
    # And one on the very last row, so an off-by-one at the final page boundary
    # cannot hide.
    last = f"agm-{total:05d}"
    _seed(state, total, last, important_col=False, metadata={"important": True})
    expected.append(last)

    repo_state = cast("StateManagementInterface", CapEnforcingState(state))
    result = backfill_message_important(repo_state)
    flipped = sorted(cast("list[str]", result["updated"]))
    _check(
        flipped == sorted(expected),
        f"every row needing the flip is flipped across all pages "
        f"(expected {len(expected)}, got {len(flipped)})",
    )
    _check(
        any(int(mid.split("-")[1]) > _PAGE for mid in flipped),
        "including rows beyond the first page — the walk genuinely paged",
    )
    _check(last in flipped, "including the very last row in the table")
    stragglers = [
        r
        for r in _updates(state, NAMESPACE, TABLE_AGENT_MESSAGE)
        if r.get("metadata", {}) == {"important": True} and r.get("important") is not True
    ]
    _check(
        stragglers == [],
        f"no row that needed the projection is left unprojected "
        f"(got {len(stragglers)})",
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
