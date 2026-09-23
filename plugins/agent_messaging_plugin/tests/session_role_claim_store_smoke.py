#!/usr/bin/env python3
"""Unit smoke for the D1 AMEND-4b cardinality gate
(``session_role_claim_store.py``).

TRUST-MODEL HONESTY (Dawn ruling, 2026-08-03): this gate is genuine against an
HONESTLY-IDENTIFIED session (an ``agent_session_id`` nobody is forging) racing
itself or another honest session. It is NOT hardened against a forged
identity pair — that caveat is load-bearing and this smoke's language matches
it (never says "enforced" bare).

Exercises, against REAL ``ActionResult`` envelope shapes (nested
``data.result.{inserted,updated,deleted}`` for mutations, flat
``data.records`` for reads):

  - fresh INSERT wins the gate (branch: first claim);
  - a same-session re-claim of the SAME role is an idempotent refresh
    (branch i) — no mutation beyond the initial insert;
  - a same-session claim of a DIFFERENT role while the first role's binding
    STILL names it is refused ``CardinalityConflictError`` (branch ii) — a
    policy refusal, not retried;
  - a stale orphan (the row's held_role has no live binding naming this
    session) self-repairs in place (branch iii);
  - a lost predicate on the self-repair (another writer moved the row first)
    is NOT swallowed — the caller re-enters the top of the bounded loop
    (Architect ratification #1), converging once the race clears;
  - the displacer's cleanup delete is predicated: it fires when the loser's
    row still names the displaced role, and is a BENIGN no-op (not an error)
    when it does not.

Run:
    .venv/bin/python3 plugins/agent_messaging_plugin/tests/session_role_claim_store_smoke.py
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT / "ananta" / "src"))
sys.path.insert(0, str(REPO_ROOT / "plugins" / "agent_messaging_plugin" / "src"))

if TYPE_CHECKING:
    from ananta.core.domain.types import ActionResult
    from ananta.interfaces.state_management_interface import StateManagementInterface

from agent_messaging_plugin.schema import (  # noqa: E402
    TABLE_ROLE_BINDING,
    TABLE_SESSION_ROLE_CLAIM,
    session_role_claim_external_id,
)
from agent_messaging_plugin.session_role_claim_store import (  # noqa: E402
    CardinalityConflictError,
    CardinalityGatedClaim,
    delete_session_role_claim_if_still_holds,
    win_cardinality_gate,
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


def _envelope(data: dict[str, Any]) -> ActionResult:
    return cast("ActionResult", {"action_status": "completed", "data": data})


class _FakeState:
    """Faithful fake over REAL ActionResult envelopes for the two tables the
    gate touches: ``session_role_claim`` (UNIQUE on external_id, one row per
    session) and ``role_binding`` (read-only here, to answer 'does the
    binding still name this principal')."""

    def __init__(
        self,
        session_rows: list[dict[str, Any]] | None = None,
        binding_rows: list[dict[str, Any]] | None = None,
    ) -> None:
        self.session_rows = session_rows if session_rows is not None else []
        self.binding_rows = binding_rows if binding_rows is not None else []
        # Test hook: external_ids in this set make the NEXT matching update
        # report 0 rows affected once (simulates a concurrent writer winning
        # the race), then self-heals for subsequent attempts.
        self.sabotage_next_update: set[str] = set()

    def query_state(self, namespace: str, query: dict[str, object]) -> ActionResult:
        table = query.get("table")
        filters = cast("dict[str, Any]", query.get("filters", {}))
        if table == TABLE_SESSION_ROLE_CLAIM:
            rows = [
                r for r in self.session_rows
                if all(r.get(k) == v for k, v in filters.items())
            ]
            return _envelope({"records": rows})
        if table == TABLE_ROLE_BINDING:
            rows = [
                r for r in self.binding_rows
                if all(r.get(k) == v for k, v in filters.items())
            ]
            return _envelope({"records": rows})
        raise AssertionError(f"unexpected table {table!r}")

    def upsert_state(self, namespace: str, data: dict[str, object]) -> ActionResult:
        record = cast("dict[str, Any]", data["record"])
        external_id = record["external_id"]
        exists = any(r["external_id"] == external_id for r in self.session_rows)
        if exists:
            return _envelope({"result": {"inserted": False}})
        self.session_rows.append(dict(record, is_deleted=0))
        return _envelope({"result": {"inserted": True}})

    def update_state(
        self, namespace: str, query: dict[str, object], updates: dict[str, object],
    ) -> ActionResult:
        filters = cast("dict[str, Any]", query.get("filters", {}))
        external_id = filters.get("external_id")
        if external_id in self.sabotage_next_update:
            self.sabotage_next_update.discard(external_id)
            return _envelope({"result": {"updated": 0}})
        affected = 0
        for row in self.session_rows:
            if all(row.get(k) == v for k, v in filters.items()):
                row.update(updates)
                affected += 1
        return _envelope({"result": {"updated": affected}})

    def delete_records(self, namespace: str, query: dict[str, object]) -> ActionResult:
        filters = cast("dict[str, Any]", query.get("filters", {}))
        before = len(self.session_rows)
        self.session_rows = [
            r for r in self.session_rows
            if not all(r.get(k) == v for k, v in filters.items())
        ]
        return _envelope({"result": {"deleted": before - len(self.session_rows)}})


def main() -> int:
    # --- fresh claim wins the gate ---
    state = _FakeState()
    repo = cast("StateManagementInterface", state)
    win_cardinality_gate(
        repo, CardinalityGatedClaim(
            agent_session_id="ases-1", requested_role="Claude-C", agent_instance_id="agi-1",
        ),
    )
    _check(len(state.session_rows) == 1, "fresh claim inserts exactly one session_role_claim row")
    _check(
        state.session_rows[0]["held_role"] == "Claude-C",
        "the row names the claimed role",
    )

    # --- branch (i): same-session re-claim of the SAME role is a no-op refresh ---
    win_cardinality_gate(
        repo, CardinalityGatedClaim(
            agent_session_id="ases-1", requested_role="Claude-C", agent_instance_id="agi-1",
        ),
    )
    _check(len(state.session_rows) == 1, "branch (i) refresh does not add a second row")

    # --- branch (ii): a genuine second role while the first binding still names us ---
    state.binding_rows.append({"role": "Claude-C", "agent_session_id": "ases-1", "is_deleted": 0})
    conflict_raised = False
    try:
        win_cardinality_gate(
            repo, CardinalityGatedClaim(
                agent_session_id="ases-1",
                requested_role="Some-Other-Lane",
                agent_instance_id="agi-1",
            ),
        )
    except CardinalityConflictError:
        conflict_raised = True
    _check(
        conflict_raised,
        "branch (ii): a second role while the first binding STILL names this "
        "session is refused (CardinalityConflictError), not silently allowed",
    )
    _check(
        state.session_rows[0]["held_role"] == "Claude-C",
        "a refused branch-(ii) attempt leaves the existing row untouched",
    )

    # --- branch (iii): stale orphan self-repairs ---
    state.binding_rows.clear()  # Claude-C's binding is gone -> row is now a stale orphan
    win_cardinality_gate(
        repo, CardinalityGatedClaim(
            agent_session_id="ases-1", requested_role="New-Lane", agent_instance_id="agi-1",
        ),
    )
    _check(
        state.session_rows[0]["held_role"] == "New-Lane",
        "branch (iii): a stale orphan (no live binding names the old held_role) "
        "self-repairs to the newly requested role",
    )
    _check(len(state.session_rows) == 1, "self-repair updates in place, never adds a row")

    # --- lost predicate on self-repair re-enters the loop, not a fall-through ---
    state.binding_rows.clear()
    ext_id = session_role_claim_external_id("ases-1")
    state.sabotage_next_update.add(ext_id)
    win_cardinality_gate(
        repo, CardinalityGatedClaim(
            agent_session_id="ases-1", requested_role="Third-Lane", agent_instance_id="agi-1",
        ),
    )
    _check(
        state.session_rows[0]["held_role"] == "Third-Lane",
        "a lost predicate on self-repair re-enters the bounded loop and "
        "converges once the simulated race clears (no fall-through)",
    )

    # --- displacer cleanup: predicated delete ---
    fresh = _FakeState(
        session_rows=[{
            "external_id": session_role_claim_external_id("ases-loser"),
            "agent_session_id": "ases-loser",
            "held_role": "Claude-C",
            "agent_instance_id": "agi-loser",
            "is_deleted": 0,
        }],
    )
    fresh_repo = cast("StateManagementInterface", fresh)
    matched = delete_session_role_claim_if_still_holds(
        fresh_repo, agent_session_id="ases-loser", expected_held_role="Claude-C",
    )
    _check(matched is True, "displacer delete fires when the row still names the displaced role")
    _check(fresh.session_rows == [], "the loser's row is gone after a matching displacer delete")

    stale_no_match = delete_session_role_claim_if_still_holds(
        fresh_repo, agent_session_id="ases-loser", expected_held_role="Claude-C",
    )
    _check(
        stale_no_match is False,
        "a second delete attempt (row already gone) is a BENIGN no-op, "
        "not an error — the predicate simply no longer matches",
    )

    print()
    print(f"PASSED: {_passed}")
    print(f"FAILED: {len(_failed)}")
    for label in _failed:
        print(f"  - {label}")
    return 1 if _failed else 0


if __name__ == "__main__":
    sys.exit(main())
