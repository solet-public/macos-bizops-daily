#!/usr/bin/env python3
"""Integration smoke: D4 Part B item 1 — the ``legislate_role`` governance-act
verb (fleet session-management, §3.1 Q1's assignment half).

Two conditions from the Coordinator-Dawn D4 dispatch, driven as SEPARATE legs
so a passing green does not let one mechanism silently cover for the other:

  (a) the act creates the row: a fresh ``legislate_role`` call on a reserved
      ``<solet>-Main`` name stamps ``role_class='primary'`` at birth; a
      same-class re-run is an idempotent no-op; a DIFFERENT-class re-run is
      refused loud (``role_class_conflict``), never silently reassigned.
  (b) the ordinary claim path's reserved-pattern fresh-mint refusal stays
      fully intact — exercised against a DIFFERENT reserved name
      (``Zzz-Main``) that this smoke deliberately never legislates, so the
      guard is genuinely exercised rather than trivially vacuous the way
      re-using the already-legislated ``Coordinator-Main`` would be. Asserted via
      ``role_claim.claim_role_for_session`` directly (not
      ``session_lifecycle_verbs._validate_spawn_role``, which emits the same
      ``reserved_role_name`` token from a different guard) so the failing
      message text is checked to confirm WHICH guard fired.

Plus condition 2 (primary-class claim policy at claim time): rather than
rebuild cardinality/uniqueness enforcement — already covered end-to-end by
``role_claim_cardinality_gate_smoke.py`` — this smoke asserts it AGAINST THE
ROW THIS ACT ACTUALLY LEGISLATES: a second session claiming the legislated
``Coordinator-Main`` DISPLACES rather than coexists (fleet-unique), and a session
already holding a DIFFERENT named role is refused ``cardinality_conflict``
when it tries to claim it (class-consistency falls out of the same one-role-
per-session invariant — every session's role and that role's class are the
same object).

Run:
    .venv/bin/python3 plugins/agent_messaging_plugin/tests/legislate_role_smoke.py
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

from _real_state_fake import RealShapeState  # noqa: E402
from ananta.llm.agent_messaging.role_binding import (  # noqa: E402
    AGENT_ROLE_BINDING_NAMESPACE,
    COL_ROLE_CLASS,
    ROLE_CLASS_PRIMARY,
    ROLE_CLASS_PRINCIPAL,
    ROLE_CLASS_PROJECT,
    TABLE_ROLE,
    role_binding_external_id,
)
from ananta.llm.agent_messaging.state_results import require_records  # noqa: E402

from agent_messaging_plugin import role_claim as role_claim_module  # noqa: E402
from agent_messaging_plugin.role_binding_store import (  # noqa: E402
    RoleClassConflictError,
    legislate_role_class,
)
from agent_messaging_plugin.role_claim import RoleClaimFailure, RoleClaimOrigin  # noqa: E402
from agent_messaging_plugin.session_lifecycle_verbs import (  # noqa: E402
    LegislateRoleRequest,
    VerbError,
)
from agent_messaging_plugin.session_lifecycle_verbs import (  # noqa: E402
    legislate_role as verb_legislate_role,
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


def _role_row(state: StateManagementInterface, name: str) -> dict[str, Any] | None:
    records = require_records(
        state.query_state(
            AGENT_ROLE_BINDING_NAMESPACE,
            {"table": TABLE_ROLE, "filters": {"external_id": role_binding_external_id(name)}},
        ),
    )
    return records[0] if records else None


def _claim(
    state: StateManagementInterface,
    *,
    name: str,
    agent_instance_id: str,
    agent_session_id: str,
) -> Any:
    return role_claim_module.claim_role_for_session(
        origin=RoleClaimOrigin.MODEL_TURN,
        name=name,
        agent_id="claude_code",
        agent_instance_id=agent_instance_id,
        agent_session_id=agent_session_id,
        session_label=name,
        state_service=state,
        bridge_manager=None,
        peer_registry=None,
        agent_messaging_service=None,
        call_context=None,
    )


def test_legislate_creates_primary_row() -> None:
    """(a) the act creates the row — fresh, idempotent re-run, conflicting re-run."""
    state = _state()

    action = legislate_role_class(
        state, name="Coordinator-Main", role_class=ROLE_CLASS_PRIMARY,
        directed_by="operator_equivalent:Coordinator-Dawn",
        brief_ref=(
            "workbench/2026-08-03_phase_c_brief_and_d4_sitting_agenda_coordinator_dawn.md "
            "Part B item 1"
        ),
    )
    _check(
        action == "legislated",
        f"(a) fresh legislation reports 'legislated' (got {action!r})",
    )
    row = _role_row(state, "Coordinator-Main")
    _check(
        row is not None and row.get(COL_ROLE_CLASS) == ROLE_CLASS_PRIMARY,
        f"(a) the role row is created with role_class='primary' (got {row!r})",
    )

    action_again = legislate_role_class(
        state, name="Coordinator-Main", role_class=ROLE_CLASS_PRIMARY,
        directed_by="operator_equivalent:Coordinator-Dawn", brief_ref="re-run",
    )
    _check(
        action_again == "already_legislated",
        f"(a) a same-class re-run is an idempotent no-op (got {action_again!r})",
    )

    conflict_raised = False
    try:
        legislate_role_class(
            state, name="Coordinator-Main", role_class=ROLE_CLASS_PRINCIPAL,
            directed_by="operator_equivalent:Coordinator-Dawn", brief_ref="conflicting re-run",
        )
    except RoleClassConflictError:
        conflict_raised = True
    _check(
        conflict_raised,
        "(a) a DIFFERENT-class re-run is refused loud (RoleClassConflictError), "
        "never silently reassigned",
    )
    row_after_conflict = _role_row(state, "Coordinator-Main")
    row_after_conflict_class = (row_after_conflict or {}).get(COL_ROLE_CLASS)
    _check(
        row_after_conflict is not None and row_after_conflict_class == ROLE_CLASS_PRIMARY,
        "(a) the refused conflicting re-run left role_class UNCHANGED",
    )


def test_verb_layer_validation() -> None:
    """The legislate_role verb body's own guards, independent of the store primitive."""
    state = _state()

    def _expect_verb_error(req: LegislateRoleRequest, code: str, label: str) -> None:
        try:
            verb_legislate_role(state, req)
        except VerbError as exc:
            _check(exc.code == code, f"{label} (got code={exc.code!r})")
        else:
            _check(False, f"{label} (no VerbError raised)")

    _expect_verb_error(
        LegislateRoleRequest(
            name="Some-Project-Role", role_class=ROLE_CLASS_PROJECT, brief_ref="x",
        ),
        "role_class_not_legislatable",
        "project/ephemeral/chat role_class is refused, never minted through this verb",
    )
    _expect_verb_error(
        LegislateRoleRequest(
            name="Not-Reserved-Shape", role_class=ROLE_CLASS_PRIMARY, brief_ref="x",
        ),
        "reserved_primary_name_required",
        "role_class='primary' on a non-<solet>-Main-shaped name is refused",
    )
    _expect_verb_error(
        LegislateRoleRequest(name="", role_class=ROLE_CLASS_PRIMARY, brief_ref="x"),
        "missing_argument",
        "an empty name is refused",
    )
    _expect_verb_error(
        LegislateRoleRequest(name="Coordinator-Main", role_class=ROLE_CLASS_PRIMARY, brief_ref=""),
        "missing_argument",
        "an empty brief_ref is refused",
    )

    result = verb_legislate_role(
        state,
        LegislateRoleRequest(
            name="Coordinator-Main", role_class=ROLE_CLASS_PRIMARY,
            brief_ref="D4 Part B item 1", directed_by="operator_equivalent:Coordinator-Dawn",
        ),
    )
    _check(
        result == {"action": "legislated", "name": "Coordinator-Main", "role_class": ROLE_CLASS_PRIMARY},
        f"the verb layer's successful path returns the expected outcome shape (got {result!r})",
    )


def test_reserved_mint_refusal_still_intact() -> None:
    """(b) the ordinary claim path's fresh-mint refusal is UNCHANGED by this act.

    Uses a DIFFERENT reserved name (never legislated by this smoke) so the
    guard is genuinely exercised — legislating and then re-claiming the SAME
    name would make ``_reserved_mint_refusal`` structurally vacuous (it only
    fires when NO role row exists yet).
    """
    state = _state()

    fresh_mint = _claim(
        state, name="Zzz-Main", agent_instance_id="agi-imposter", agent_session_id="ases-imposter",
    )
    _check(
        isinstance(fresh_mint, RoleClaimFailure) and fresh_mint.code == "reserved_role_name",
        f"(b) a fresh mint of an UNLEGISLATED <solet>-Main shape name is "
        f"still refused reserved_role_name (got {fresh_mint!r})",
    )
    _check(
        isinstance(fresh_mint, RoleClaimFailure) and "session claim" in fresh_mint.message,
        "(b) the refusal came from role_claim._reserved_mint_refusal (message "
        "names 'session claim'), not session_lifecycle_verbs._validate_spawn_role "
        "(which would name 'spawn') — confirms WHICH guard fired",
    )

    # Now legislate Coordinator-Main through the REAL act and confirm an ordinary claim
    # against the now-legislated name proceeds normally (Dawn ruling Q1:
    # enforce-by-class, never class-assignment — the guard only blocks MINTING).
    legislate_role_class(
        state, name="Coordinator-Main", role_class=ROLE_CLASS_PRIMARY,
        directed_by="operator_equivalent:Coordinator-Dawn", brief_ref="D4 Part B item 1",
    )
    legislated_claim = _claim(
        state, name="Coordinator-Main",
        agent_instance_id="agi-primary-a", agent_session_id="ases-primary-a",
    )
    _check(
        not isinstance(legislated_claim, RoleClaimFailure),
        f"(b) claiming the now-legislated Coordinator-Main proceeds normally through "
        f"the UNMODIFIED claim path (got {legislated_claim!r})",
    )


def test_claim_time_enforcement_against_legislated_row() -> None:
    """Condition 2: primary-class claim policy AGAINST THE ROW THIS ACT LEGISLATES.

    Fleet-uniqueness (one row per role name, by construction of
    ``claim_role_binding_v4``'s CAS) and class-consistency (every session
    holds at most one named role, so a session's class and its role's class
    are the same object) are both landed D1 mechanics — this leg measures
    them against the NEW row rather than re-deriving them.
    """
    state = _state()
    legislate_role_class(
        state, name="Coordinator-Main", role_class=ROLE_CLASS_PRIMARY,
        directed_by="operator_equivalent:Coordinator-Dawn", brief_ref="D4 Part B item 1",
    )

    first = _claim(
        state, name="Coordinator-Main", agent_instance_id="agi-a", agent_session_id="ases-a",
    )
    _check(
        not isinstance(first, RoleClaimFailure) and first.action == "claimed",
        f"first claim of the legislated primary seat is 'claimed' (got {first!r})",
    )

    second = _claim(
        state, name="Coordinator-Main", agent_instance_id="agi-b", agent_session_id="ases-b",
    )
    _check(
        not isinstance(second, RoleClaimFailure) and second.action == "displaced",
        f"a second, DIFFERENT session DISPLACES rather than coexists — "
        f"fleet-unique (got {second!r})",
    )

    # A third session already holding a DIFFERENT named role is refused
    # cardinality_conflict when it tries to also claim the primary seat.
    other_role_claim = _claim(
        state, name="Claude-Zeta", agent_instance_id="agi-c", agent_session_id="ases-c",
    )
    _check(
        not isinstance(other_role_claim, RoleClaimFailure),
        f"setup: session C first claims an unrelated named role (got {other_role_claim!r})",
    )
    conflicting = _claim(
        state, name="Coordinator-Main", agent_instance_id="agi-c", agent_session_id="ases-c",
    )
    _check(
        isinstance(conflicting, RoleClaimFailure) and conflicting.code == "cardinality_conflict",
        f"a session already holding a DIFFERENT named role is refused "
        f"cardinality_conflict — class-consistency (got {conflicting!r})",
    )


def main() -> int:
    test_legislate_creates_primary_row()
    test_verb_layer_validation()
    test_reserved_mint_refusal_still_intact()
    test_claim_time_enforcement_against_legislated_row()

    print()
    print(f"PASSED: {_passed}")
    print(f"FAILED: {len(_failed)}")
    for label in _failed:
        print(f"  - {label}")
    return 1 if _failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
