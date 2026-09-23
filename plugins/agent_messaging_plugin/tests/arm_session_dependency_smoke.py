#!/usr/bin/env python3
"""Unit smoke: ``arm_session_dependency`` — the drive-on-delivery lane's rider
verb (slice 2, 2026-08-04), the FIRST caller of the D1 ``session_dependency``
wake-edge machinery (schema + sweep evaluation + delivery already existed;
nothing armed a row until now).

Legs:
  (a) a valid session-scoped arm writes exactly the row the schema expects
      (``waiter_instance_id`` set, ``waiter_lane_id`` absent, ``fired_at``
      NULL) and the return payload echoes it back.
  (b) ``invalid_waiter`` — empty/whitespace-only ``waiter_instance_id``
      refuses BEFORE any write (no row lands).
  (c) ``unknown_condition_kind`` — a condition_kind outside
      {lane_closed, session_terminal, deadline} refuses.
  (d) ``invalid_condition_ref`` per kind: ``session_terminal`` requires an
      ``agi-``-prefixed condition_ref; ``deadline`` requires a parseable
      ISO-8601 timestamp; ``lane_closed`` requires non-empty. Each leg named
      to its own kind, not one generic "bad ref" assertion.
  (e) each VALID kind arms cleanly (one leg per kind, proving the shape
      checks are not accidentally rejecting their own legal inputs).
  (f) lane-scoped arming is unsupported BY CONSTRUCTION — there is no
      ``waiter_lane_id`` parameter to pass at all; a caller cannot even
      express the request, confirmed by the dataclass's own field set.

Run:
    .venv/bin/python3 plugins/agent_messaging_plugin/tests/arm_session_dependency_smoke.py
"""

from __future__ import annotations

import dataclasses
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT / "ananta" / "src"))
sys.path.insert(0, str(REPO_ROOT / "plugins" / "agent_messaging_plugin" / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

if TYPE_CHECKING:
    from ananta.interfaces.state_management_interface import StateManagementInterface

from _real_state_fake import RealShapeState  # noqa: E402
from ananta.llm.agent_messaging.role_binding import AGENT_ROLE_BINDING_NAMESPACE  # noqa: E402
from ananta.llm.agent_messaging.state_results import require_records  # noqa: E402

from agent_messaging_plugin.schema import (  # noqa: E402
    CONDITION_DEADLINE,
    CONDITION_LANE_CLOSED,
    CONDITION_SESSION_TERMINAL,
    TABLE_SESSION_DEPENDENCY,
)
from agent_messaging_plugin.session_lifecycle_verbs import (  # noqa: E402
    ArmSessionDependencyRequest,
    VerbError,
)
from agent_messaging_plugin.session_lifecycle_verbs import (  # noqa: E402
    arm_session_dependency as verb_arm_session_dependency,
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


def _state() -> StateManagementInterface:
    return cast("StateManagementInterface", RealShapeState())


def _rows(state: StateManagementInterface) -> list[dict[str, Any]]:
    result = state.query_state(
        AGENT_ROLE_BINDING_NAMESPACE, {"table": TABLE_SESSION_DEPENDENCY, "filters": {}},
    )
    return require_records(result)


def _expect_error(state: StateManagementInterface, req: ArmSessionDependencyRequest) -> str:
    try:
        verb_arm_session_dependency(state, req)
    except VerbError as exc:
        return exc.code
    return ""


def test_valid_session_terminal_arm_writes_the_row() -> None:
    state = _state()
    result = verb_arm_session_dependency(
        state,
        ArmSessionDependencyRequest(
            waiter_instance_id="agi-waiter01",
            condition_kind=CONDITION_SESSION_TERMINAL,
            condition_ref="agi-watched01",
        ),
    )
    _check(
        result == {
            "waiter_instance_id": "agi-waiter01",
            "condition_kind": CONDITION_SESSION_TERMINAL,
            "condition_ref": "agi-watched01",
            "armed": True,
        },
        "arm_session_dependency returns the armed edge's fields",
    )
    rows = _rows(state)
    _check(len(rows) == 1, "exactly one session_dependency row was written")
    row = rows[0]
    _check(
        row.get("waiter_instance_id") == "agi-waiter01"
        and row.get("condition_kind") == CONDITION_SESSION_TERMINAL
        and row.get("condition_ref") == "agi-watched01"
        and row.get("fired_at") is None,
        "the written row carries the exact session-scoped fields, fired_at NULL (armed)",
    )
    _check(
        "waiter_lane_id" not in row or row.get("waiter_lane_id") is None,
        "no waiter_lane_id is ever written (session-scoped only, v1)",
    )


def test_invalid_waiter_refuses_before_any_write() -> None:
    state = _state()
    for bad_waiter in ("", "   "):
        code = _expect_error(
            state,
            ArmSessionDependencyRequest(
                waiter_instance_id=bad_waiter,
                condition_kind=CONDITION_LANE_CLOSED,
                condition_ref="lane-x",
            ),
        )
        _check(
            code == "invalid_waiter",
            f"waiter_instance_id={bad_waiter!r} -> invalid_waiter (got {code!r})",
        )
    _check(not _rows(state), "no row was written across any invalid_waiter attempt")


def test_unknown_condition_kind_refuses() -> None:
    state = _state()
    code = _expect_error(
        state,
        ArmSessionDependencyRequest(
            waiter_instance_id="agi-waiter01",
            condition_kind="not_a_real_kind",
            condition_ref="whatever",
        ),
    )
    _check(code == "unknown_condition_kind", f"unrecognised condition_kind refuses (got {code!r})")
    _check(not _rows(state), "no row was written")


def test_condition_ref_shape_checks_per_kind() -> None:
    state = _state()
    code = _expect_error(
        state,
        ArmSessionDependencyRequest(
            waiter_instance_id="agi-waiter01",
            condition_kind=CONDITION_SESSION_TERMINAL,
            condition_ref="not-an-instance-id",
        ),
    )
    _check(
        code == "invalid_condition_ref",
        f"session_terminal: a non-'agi-' condition_ref refuses (got {code!r})",
    )
    code = _expect_error(
        state,
        ArmSessionDependencyRequest(
            waiter_instance_id="agi-waiter01",
            condition_kind=CONDITION_DEADLINE,
            condition_ref="not a timestamp",
        ),
    )
    _check(
        code == "invalid_condition_ref",
        f"deadline: an unparseable timestamp condition_ref refuses (got {code!r})",
    )
    code = _expect_error(
        state,
        ArmSessionDependencyRequest(
            waiter_instance_id="agi-waiter01",
            condition_kind=CONDITION_LANE_CLOSED,
            condition_ref="   ",
        ),
    )
    _check(
        code == "invalid_condition_ref",
        f"lane_closed: a blank condition_ref refuses (got {code!r})",
    )
    _check(not _rows(state), "no row was written across any invalid_condition_ref attempt")


def test_every_valid_kind_arms_cleanly() -> None:
    """Proves the shape checks are not accidentally rejecting their OWN
    legal inputs — one leg per kind, each a genuinely valid value."""
    cases = (
        (CONDITION_SESSION_TERMINAL, "agi-realwaiter00000000000000000"),
        (CONDITION_DEADLINE, "2026-12-31T23:59:59+00:00"),
        (CONDITION_LANE_CLOSED, "lane-real"),
    )
    for condition_kind, condition_ref in cases:
        state = _state()
        result = verb_arm_session_dependency(
            state,
            ArmSessionDependencyRequest(
                waiter_instance_id="agi-waiter01",
                condition_kind=condition_kind,
                condition_ref=condition_ref,
            ),
        )
        _check(
            result.get("armed") is True,
            f"a genuinely valid {condition_kind} condition_ref arms cleanly",
        )


def test_lane_scoped_arming_is_unexpressable() -> None:
    """Unsupported BY CONSTRUCTION (ruling 5, 2026-08-04 sign-off): there is
    no waiter_lane_id field on the request at all, so a caller cannot even
    construct a lane-scoped arm request — confirmed structurally against the
    dataclass's own declared fields, not by probing a rejected value."""
    field_names = {f.name for f in dataclasses.fields(ArmSessionDependencyRequest)}
    _check(
        field_names == {"waiter_instance_id", "condition_kind", "condition_ref"},
        f"ArmSessionDependencyRequest has exactly the session-scoped fields (got {field_names})",
    )
    _check(
        "waiter_lane_id" not in field_names,
        "no waiter_lane_id field exists — lane-scoped arming is unexpressable, not merely refused",
    )


def main() -> int:
    print("=== arm_session_dependency smoke (drive-on-delivery slice 2) ===")
    test_valid_session_terminal_arm_writes_the_row()
    test_invalid_waiter_refuses_before_any_write()
    test_unknown_condition_kind_refuses()
    test_condition_ref_shape_checks_per_kind()
    test_every_valid_kind_arms_cleanly()
    test_lane_scoped_arming_is_unexpressable()
    print(f"\n{_passed} passed, {len(_failed)} failed")
    if _failed:
        for label in _failed:
            print(f"  FAILED: {label}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
