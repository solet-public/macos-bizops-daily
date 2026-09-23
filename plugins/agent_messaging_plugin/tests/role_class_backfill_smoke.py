#!/usr/bin/env python3
"""Unit smoke for the D1 ``role_class`` backfill (fleet session-management
Phase B, §3.1).

Verifies, against REAL ``ActionResult`` envelope shapes (flat
``data.records`` for reads, nested ``data.result.updated`` for the update,
``data.found`` for the key-value marker):

  - pre-Phase-B rows (``role_class`` unset) get stamped with the default;
  - rows that already carry a ``role_class`` are left untouched;
  - the stamp is marker-gated (one-shot, self-healing on an unset marker);
  - a pre-existing >1-named-role holder is DETECTED AND REPORTED, never
    silently normalized — and this check re-runs EVERY call (not marker
    gated), because a rename-aliases-adds-never-releases trap makes new
    violations plausible after the one-shot stamp has already fired;
  - ``sys:*`` slots are excluded from the cardinality count (design §2);
  - **both walks are complete past the 100-row read bound** (section C).

## Why this smoke moved onto ``RealShapeState`` (2026-08-15)

It previously carried a private ``_FakeState`` that answered every read with the
whole table and implemented ``query_state`` only. That fake could not express the
defect this module now guards against: on 2026-08-15 the default state-read row
bound dropped to 100, ``_detect_cardinality_violations`` was reading 133 live
role bindings with no limit, and the refusal took ``start_interface`` — and with
it agent messaging and the action queue — down at boot.

The private fake would have stayed green through all of it, twice over: it had no
``query_ordered`` to page with, and its five-row fixture never reached a page
boundary. **A fixture that cannot reach the bound cannot test the bound.** So the
reads are paginated now, and section C plants the violation on the THIRD page,
where a single-page read reports a clean fleet.

Run:
    .venv/bin/python3 plugins/agent_messaging_plugin/tests/role_class_backfill_smoke.py
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
from ananta.llm.agent_messaging.role_binding import (  # noqa: E402
    AGENT_ROLE_BINDING_NAMESPACE,
    TABLE_ROLE,
    TABLE_ROLE_BINDING,
)

from agent_messaging_plugin.role_class_backfill import (  # noqa: E402
    STATUS_ALREADY_DONE,
    STATUS_COMPLETED,
    backfill_role_class,
)

_passed = 0
_failed: list[str] = []

# One page. Fixtures below deliberately straddle it — a fixture smaller than this
# exercises the walk's first iteration only, which is the shape that stayed green
# through the outage.
_PAGE = 100


def _check(condition: object, label: str) -> None:
    global _passed
    if condition:
        _passed += 1
        print(f"  PASS  {label}")
    else:
        _failed.append(label)
        print(f"  FAIL  {label}")


def _seed_role(
    state: RealShapeState, n: int, name: str, role_class: str | None,
) -> None:
    """Insert a ``role`` row with an explicit, order-stable cursor pair.

    ``id`` and ``created_at`` are zero-padded so lexical order matches insertion
    order; the fake's generated ``gen-{n}`` ids are not, and would make the walk's
    page boundaries depend on string collation rather than on the code.
    """
    record: dict[str, Any] = {
        "id": f"rol-{n:05d}",
        "created_at": f"2026-08-15T00:00:00.{n:06d}",
        "role": name,
    }
    if role_class is not None:
        record["role_class"] = role_class
    # conflict_columns is REQUIRED, not optional decoration: with an empty list
    # the fake's `all(... for c in conflict)` is vacuously true against the first
    # existing row, so every later seed MERGES into it and the fixture silently
    # collapses to a single row.
    state.upsert_state(
        AGENT_ROLE_BINDING_NAMESPACE,
        {"table": TABLE_ROLE, "record": record, "conflict_columns": ["id"]},
    )


def _seed_binding(
    state: RealShapeState,
    n: int,
    name: str,
    *,
    agent_instance_id: str,
    holder_kind: str = "session",
) -> None:
    state.upsert_state(
        AGENT_ROLE_BINDING_NAMESPACE,
        {
            "table": TABLE_ROLE_BINDING,
            "conflict_columns": ["id"],
            "record": {
                "id": f"rb-{n:05d}",
                "created_at": f"2026-08-15T00:00:00.{n:06d}",
                "role": name,
                "agent_instance_id": agent_instance_id,
                "holder_kind": holder_kind,
                "is_deleted": 0,
            },
        },
    )


def _semantics() -> None:
    print("A. stamping, marker gating, and cardinality detection")
    state = RealShapeState()
    for n, (name, klass) in enumerate(
        [
            ("Coordinator-Dawn", None),      # pre-Phase-B -> stamp
            ("Git-Controller", "principal"),  # already set -> leave
            ("Claude-C", None),              # pre-Phase-B -> stamp
        ],
    ):
        _seed_role(state, n, name, klass)
    for n, (name, agi, kind) in enumerate(
        [
            ("Claude-C", "agi-1", "session"),
            ("some-lane", "agi-1", "session"),        # agi-1 holds 2 -> violation
            ("Coordinator-Dawn", "agi-2", "session"),
            ("sys:autonomic", "agi-2", "session"),    # exempt slot, not a 2nd role
            ("provider-role", "", "inference_provider"),
        ],
    ):
        _seed_binding(state, n, name, agent_instance_id=agi, holder_kind=kind)

    repo_state = cast("StateManagementInterface", CapEnforcingState(state))
    result = backfill_role_class(repo_state)
    stamped = sorted(cast("list[str]", result["stamped"]))
    _check(result["status"] == STATUS_COMPLETED, "first run -> status=completed")
    _check(
        stamped == ["Claude-C", "Coordinator-Dawn"],
        f"stamps ONLY rows missing role_class, leaves already-set rows "
        f"(stamped={stamped!r})",
    )
    rows = cast(
        "list[dict[str, Any]]",
        state.query_state(
            AGENT_ROLE_BINDING_NAMESPACE, {"table": TABLE_ROLE, "filters": {}},
        )["data"]["records"],
    )
    by_role = {str(r["role"]): r for r in rows}
    _check(
        by_role["Coordinator-Dawn"].get("role_class") == "project"
        and by_role["Claude-C"].get("role_class") == "project",
        "stamped rows now carry role_class='project'",
    )
    _check(
        by_role["Git-Controller"].get("role_class") == "principal",
        "already-set row's role_class is untouched",
    )

    violations = cast("list[dict[str, Any]]", result["cardinality_violations"])
    _check(
        len(violations) == 1,
        f"exactly one cardinality violation reported (got {violations!r})",
    )
    if violations:
        _check(
            violations[0]
            == {"agent_instance_id": "agi-1", "roles": ["Claude-C", "some-lane"]},
            f"violation names the offending instance + BOTH roles, sys:* excluded "
            f"(got {violations[0]!r})",
        )

    print("\nB. the stamp is one-shot; the cardinality report is not")
    again = backfill_role_class(repo_state)
    _check(
        again["status"] == STATUS_ALREADY_DONE,
        "second run -> already_done (marker-gated)",
    )
    _check(again["stamped"] == [], "second run stamps nothing")
    _check(
        len(cast("list[Any]", again["cardinality_violations"])) == 1,
        "cardinality detection re-runs even after the stamp is marker-done "
        "(NOT itself marker-gated — new violations can appear later)",
    )


def _past_the_page_boundary() -> None:
    print("\nC. both walks stay complete PAST the 100-row read bound")
    # 250 of each: three pages. This is the fixture the pre-2026-08-15 smoke did
    # not have, and its absence is why a five-row green coexisted with a boot
    # failure on 133 live rows.
    state = RealShapeState()
    total = 250
    for n in range(total):
        # Every role row is missing role_class, so a walk that stops early stamps
        # a prefix and reports success for a job it did not finish.
        _seed_role(state, n, f"lane-{n:05d}", None)
    for n in range(total):
        _seed_binding(state, n, f"lane-{n:05d}", agent_instance_id=f"agi-{n:05d}")
    # THE PLANTED VIOLATION, on the third page. A single-page read sees rows
    # 0..99, none of which collide, and reports a perfectly clean fleet.
    _seed_binding(state, total, "second-role", agent_instance_id="agi-00240")

    repo_state = cast("StateManagementInterface", CapEnforcingState(state))
    result = backfill_role_class(repo_state)

    stamped = cast("list[str]", result["stamped"])
    _check(
        len(stamped) == total,
        f"every one of {total} unstamped role rows is stamped, not just the "
        f"first page (got {len(stamped)})",
    )
    _check(
        len(stamped) > _PAGE,
        "and the count is past one page, so the walk genuinely paged",
    )
    remaining = [
        r
        for r in cast(
            "list[dict[str, Any]]",
            state.query_state(
                AGENT_ROLE_BINDING_NAMESPACE, {"table": TABLE_ROLE, "filters": {}},
            )["data"]["records"],
        )
        if not r.get("role_class")
    ]
    _check(
        remaining == [],
        f"no role row is left unstamped after the walk (got {len(remaining)})",
    )

    violations = cast("list[dict[str, Any]]", result["cardinality_violations"])
    _check(
        len(violations) == 1,
        f"the violation planted on page THREE is still found (got {violations!r}) "
        f"— this is the check a single-page read fails while looking healthy",
    )
    if violations:
        _check(
            violations[0]["agent_instance_id"] == "agi-00240",
            f"and it names the right holder (got {violations[0]!r})",
        )

    print("\nD. soft-deleted bindings stay excluded across pages")
    # include_deleted=False replaced an explicit is_deleted=0 filter when the read
    # was paginated. Same predicate, different spelling — pinned so the swap
    # cannot silently widen the report to tombstoned holders.
    _seed_binding(
        state, total + 1, "tombstoned-role", agent_instance_id="agi-00240",
    )
    rows = state.rows(AGENT_ROLE_BINDING_NAMESPACE, TABLE_ROLE_BINDING)
    for row in rows:
        if row.get("role") == "tombstoned-role":
            row["is_deleted"] = 1
    after = backfill_role_class(repo_state)
    violation_roles = {
        role
        for v in cast("list[dict[str, Any]]", after["cardinality_violations"])
        for role in cast("list[str]", v["roles"])
    }
    _check(
        "tombstoned-role" not in violation_roles,
        "a soft-deleted binding does not count toward the cardinality report",
    )
    _check(
        len(cast("list[Any]", after["cardinality_violations"])) == 1,
        "and the live violation is still reported (the exclusion did not eat it)",
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
