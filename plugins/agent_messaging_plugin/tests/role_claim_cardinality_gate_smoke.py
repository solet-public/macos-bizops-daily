#!/usr/bin/env python3
"""Integration smoke: the D1 AMEND-4b cardinality gate wired into
``claim_role_for_session`` end-to-end (real ``RealShapeState`` + a real
``PeerRegistry`` over an in-memory ``Store`` — not a hand-rolled fake; see
``session_role_claim_store_smoke.py`` for the gate's own unit-level coverage).

Per the Architect ratification (arm-a0cd684f9317) and Dawn's Q2 conditions,
this smoke drives the two displacement legs SEPARATELY and asserts WHICH
guard fired each time (a passing green here must not let one mechanism
silently cover for the other):

  (a) the displacer's cleanup delete is DISABLED (monkeypatched to a no-op) —
      displacement proceeds anyway, the loser's stale row PERSISTS, and its
      own NEXT claim (for a different role) self-repairs via branch (iii).
      Proves resilience when the delete never fires.
  (b) a CLEAN displacement (delete enabled) — the loser's row is GONE
      immediately after displacement, before the loser ever claims again.
      Proves the delete itself does the work in the common case.

Plus the two conditions Dawn's Q2 ruling attached to landing this gate:

  (c) the REL-07(1) fill-when-empty path (``agent_session_id`` sourced from
      the claimant's own live ``peer_binding`` row) still produces a correct
      session_role_claim row keyed on the FILLED value — the zero-regression
      claim gets MEASURED here, not asserted.
  (d) the §3.1 reserved-mint guard: a FRESH mint of a ``<solet>-Main``
      shape name is refused ``reserved_role_name``; an ALREADY-legislated one
      (a pre-existing role row) claims normally — enforce-by-class, never
      class-assignment (Dawn ruling Q1).

TRUST-MODEL HONESTY (Dawn ruling): every "displaces" / "refuses" assertion
below is genuine only against an HONESTLY-IDENTIFIED session — this smoke
does not attempt to forge an identity pair, and the gate is not claimed to
defend against one (tracked debt,
``reference_peer_claim_role_trusts_caller_asserted_instance_id``).

Run:
    .venv/bin/python3 plugins/agent_messaging_plugin/tests/role_claim_cardinality_gate_smoke.py
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, cast

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT / "ananta" / "src"))
sys.path.insert(0, str(REPO_ROOT / "plugins" / "agent_messaging_plugin" / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from typing import TYPE_CHECKING  # noqa: E402

if TYPE_CHECKING:
    from ananta.interfaces.state_management_interface import StateManagementInterface

from _real_state_fake import RealShapeState  # noqa: E402
from ananta.llm.agent_messaging.role_binding import (  # noqa: E402
    AGENT_ROLE_BINDING_NAMESPACE,
    TABLE_ROLE,
    role_binding_external_id,
)
from ananta.llm.agent_messaging.state_results import require_records  # noqa: E402
from ananta.services.store import open_store  # noqa: E402

from agent_messaging_plugin import role_claim as role_claim_module  # noqa: E402
from agent_messaging_plugin.models import BridgeBinding  # noqa: E402
from agent_messaging_plugin.peer_registry import PeerRegistry  # noqa: E402
from agent_messaging_plugin.role_claim import RoleClaimFailure, RoleClaimOrigin  # noqa: E402
from agent_messaging_plugin.schema import (  # noqa: E402
    PEER_BINDING_NAMESPACE,
    TABLE_SESSION_ROLE_CLAIM,
    get_peer_binding_schema,
    session_role_claim_external_id,
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


def _fresh_peer_registry() -> PeerRegistry:
    store = open_store(
        get_peer_binding_schema(), namespace=PEER_BINDING_NAMESPACE, backend="in_memory",
    )
    return PeerRegistry(bindings_store=store)


def _own_row(state: StateManagementInterface, agent_session_id: str) -> dict[str, Any] | None:
    result = state.query_state(
        AGENT_ROLE_BINDING_NAMESPACE,
        {
            "table": TABLE_SESSION_ROLE_CLAIM,
            "filters": {
                "external_id": session_role_claim_external_id(agent_session_id),
                "is_deleted": 0,
            },
        },
    )
    records = require_records(result)
    return records[0] if records else None


def _claim(
    state: StateManagementInterface,
    *,
    name: str,
    agent_id: str,
    agent_instance_id: str,
    agent_session_id: str,
    peer_registry: PeerRegistry | None = None,
) -> Any:
    return role_claim_module.claim_role_for_session(
        origin=RoleClaimOrigin.MODEL_TURN,
        name=name,
        agent_id=agent_id,
        agent_instance_id=agent_instance_id,
        agent_session_id=agent_session_id,
        session_label=name,
        state_service=state,
        bridge_manager=None,
        peer_registry=peer_registry,
        agent_messaging_service=None,
        call_context=None,
    )


def test_displacer_delete_disabled_branch_iii_repairs() -> None:
    state = _state()
    result = _claim(
        state, name="Claude-C", agent_id="claude_code",
        agent_instance_id="agi-loser", agent_session_id="ases-loser",
    )
    _check(
        getattr(result, "action", None) in ("claimed", "updated"),
        "(a) setup: loser's original claim lands",
    )

    original_delete = role_claim_module.delete_session_role_claim_if_still_holds
    role_claim_module.delete_session_role_claim_if_still_holds = (
        lambda *args, **kwargs: False  # noqa: ARG005 — disabled cleanup, simulates a killed delete
    )
    try:
        displaced = _claim(
            state, name="Claude-C", agent_id="claude_code",
            agent_instance_id="agi-winner", agent_session_id="ases-winner",
        )
    finally:
        role_claim_module.delete_session_role_claim_if_still_holds = original_delete
    _check(
        getattr(displaced, "action", None) == "displaced",
        "(a) displacement itself succeeds even though cleanup is disabled",
    )
    loser_row = _own_row(state, "ases-loser")
    _check(
        loser_row is not None and loser_row.get("held_role") == "Claude-C",
        "(a) with cleanup disabled, the loser's stale row PERSISTS after displacement",
    )

    # The loser's own next claim (a DIFFERENT role) must self-repair via
    # branch (iii) rather than raising cardinality_conflict — proving
    # resilience when the displacer's delete never fired.
    repaired = _claim(
        state, name="Claude-C-Backup-Lane", agent_id="claude_code",
        agent_instance_id="agi-loser", agent_session_id="ases-loser",
    )
    _check(
        not isinstance(repaired, RoleClaimFailure),
        f"(a) branch (iii) self-repairs the loser's next claim (not a "
        f"cardinality_conflict) — got {repaired!r}",
    )
    healed_row = _own_row(state, "ases-loser")
    _check(
        healed_row is not None and healed_row.get("held_role") == "Claude-C-Backup-Lane",
        "(a) the self-repaired row now names the loser's NEW role",
    )


def test_clean_displacement_delete_fires_immediately() -> None:
    state = _state()
    _claim(
        state, name="Coordinator-Dawn", agent_id="claude_code",
        agent_instance_id="agi-loser2", agent_session_id="ases-loser2",
    )
    displaced = _claim(
        state, name="Coordinator-Dawn", agent_id="claude_code",
        agent_instance_id="agi-winner2", agent_session_id="ases-winner2",
    )
    _check(getattr(displaced, "action", None) == "displaced", "(b) clean displacement succeeds")
    # Assert the guard that fired: the row is gone RIGHT AFTER displacement —
    # before the loser ever attempts another claim, so this cannot be
    # branch (iii) self-repair (there was no subsequent claim to trigger it).
    _check(
        _own_row(state, "ases-loser2") is None,
        "(b) the loser's row is GONE immediately after a clean displacement "
        "(the delete fired — not branch (iii), which never ran)",
    )


def test_rel07_fill_when_empty_still_works_through_gate() -> None:
    state = _state()
    registry = _fresh_peer_registry()
    registry.register(
        BridgeBinding(
            bridge_id="bridge-1",
            agent_id="claude_code",
            agent_instance_id="agi-relfill",
            session_label="Claude-C",
            parent_pid=None,
            agent_session_id="ases-relfill-stable",
        ),
    )
    result = _claim(
        state, name="Claude-C", agent_id="claude_code",
        agent_instance_id="agi-relfill", agent_session_id="",  # deliberately empty
        peer_registry=registry,
    )
    _check(
        not isinstance(result, RoleClaimFailure),
        f"(c) an empty agent_session_id claim still succeeds (got {result!r})",
    )
    _check(
        getattr(result, "agent_session_id", "") == "ases-relfill-stable",
        "(c) the claim resolves to the REGISTRY-filled agent_session_id, not empty",
    )
    row = _own_row(state, "ases-relfill-stable")
    _check(
        row is not None and row.get("held_role") == "Claude-C",
        "(c) the session_role_claim row is keyed on the FILLED value (measured, "
        "not just asserted) — the zero-regression claim through the new gate",
    )


def test_reserved_mint_guard() -> None:
    state = _state()
    fresh_mint = _claim(
        state, name="Coordinator-Main", agent_id="claude_code",
        agent_instance_id="agi-primary", agent_session_id="ases-primary",
    )
    _check(
        isinstance(fresh_mint, RoleClaimFailure) and fresh_mint.code == "reserved_role_name",
        f"(d) a fresh mint of a <solet>-Main shape name is refused "
        f"reserved_role_name (got {fresh_mint!r})",
    )
    _check(
        _own_row(state, "ases-primary") is None,
        "(d) a refused mint leaves no session_role_claim row behind",
    )

    # A pre-legislated name (role row already exists) claims normally — the
    # guard fires ONLY on a fresh mint (Dawn Q1: enforce-by-class, never
    # class-assignment).
    state.write_state(
        AGENT_ROLE_BINDING_NAMESPACE,
        {
            "table": TABLE_ROLE,
            "record": {
                "external_id": role_binding_external_id("Coordinator-Main"),
                "role": "Coordinator-Main",
                "role_class": "primary",
            },
        },
    )
    legislated = _claim(
        state, name="Coordinator-Main", agent_id="claude_code",
        agent_instance_id="agi-primary", agent_session_id="ases-primary",
    )
    _check(
        not isinstance(legislated, RoleClaimFailure),
        f"(d) an ALREADY-legislated <solet>-Main name claims normally "
        f"(got {legislated!r})",
    )


def main() -> int:
    test_displacer_delete_disabled_branch_iii_repairs()
    test_clean_displacement_delete_fires_immediately()
    test_rel07_fill_when_empty_still_works_through_gate()
    test_reserved_mint_guard()

    print()
    print(f"PASSED: {_passed}")
    print(f"FAILED: {len(_failed)}")
    for label in _failed:
        print(f"  - {label}")
    return 1 if _failed else 0


if __name__ == "__main__":
    sys.exit(main())
