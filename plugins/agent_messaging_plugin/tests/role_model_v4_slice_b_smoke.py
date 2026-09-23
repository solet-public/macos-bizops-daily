#!/usr/bin/env python3
"""Slice-B smoke for role-model v4 — §5.1 claim/displace CAS, §5.0 re-check,
reverse-lookup + dup policy, the no-tombstone invariant, and the REL-07 live-heal
mechanics (no pytest, no DB).

Slice B builds the RACE-SAFE claim path + the act-time ownership re-check, and
heals the live REL-07 defect (a claimed role's reconnect reroute could never match
because the binding's ``agent_session_id`` was left empty). Every state op runs
against the REAL provider ActionResult shapes (``RealShapeState`` + a faithful
``upsert_state(on_conflict='do_nothing')``: a UNIQUE conflict returns the
completed ``inserted=False`` envelope with no provider exception/log; the UNIQUE
index ignores ``is_deleted`` — so a soft-delete tombstone still conflicts).

Covers:

  §5.1 claim/displace CAS
    * first-claim INSERT wins → action='claimed', epoch 0, session id stored;
    * a DIFFERENT session claiming a held role → displace (epoch E→E+1), prior returned;
    * a self-re-claim (same stable session id, rotated agi) → idempotent 'refreshed',
      NO epoch bump, re-points the instance in place, no prior;
    * existing-row claims use DO-NOTHING upsert, never exception-driven write_state;
    * a PERSISTENT non-conflict upsert fault → immediate StateOperationError.
  no-tombstone invariant (load-bearing)
    * claim → HARD-delete release → re-claim SUCCEEDS;
    * a FORBIDDEN soft-delete tombstone DEADLOCKS the slot (RoleClaimContendedError).
  §5.0 act-time re-check (holds_role)
    * true for the current holder; false after displace (reference abort);
    * empty session id / vacant role → false.
  reverse-lookup + dup policy (peer_registry)
    * resolve_by_agent_session_id → the live binding / None / PeerSessionAmbiguousError;
    * agent_session_id_for_instance sources the claimant's stable id (REL-07(1)).
  reroute acceptance (REL-07(1) mechanism)
    * a binding written WITH a session id → refresh_role_binding_cas MATCHES on reconnect;
    * the pre-fix EMPTY session id → the CAS matches NOTHING (the observed defect).

Role names are OPAQUE, operator-defined strings throughout.

Run:
    SOLET_NAME=<name>-test .venv/bin/python3 \
        plugins/agent_messaging_plugin/tests/role_model_v4_slice_b_smoke.py
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, cast

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT / "ananta" / "src"))
sys.path.insert(0, str(REPO_ROOT / "plugins" / "agent_messaging_plugin" / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _real_state_fake import RealShapeState  # noqa: E402
from ananta.interfaces.state_management_interface import (  # noqa: E402
    StateManagementInterface,
)
from ananta.llm.agent_messaging.role_binding import (  # noqa: E402
    AGENT_ROLE_BINDING_NAMESPACE,
    COL_AGENT_INSTANCE_ID,
    COL_AGENT_SESSION_ID,
    COL_CLAIM_EPOCH,
    COL_ROLE,
    HOLDER_KIND_SESSION,
    TABLE_ROLE_BINDING,
    role_binding_external_id,
)
from ananta.llm.agent_messaging.state_results import StateOperationError  # noqa: E402
from ananta.services.store import open_store  # noqa: E402

from agent_messaging_plugin.models import BridgeBinding  # noqa: E402
from agent_messaging_plugin.peer_registry import (  # noqa: E402
    PeerRegistry,
    PeerSessionAmbiguousError,
)
from agent_messaging_plugin.role_binding_store import (  # noqa: E402
    HolderClaim,
    RoleClaimContendedError,
    claim_role_binding_v4,
    holds_role,
    refresh_role_binding_cas,
)
from agent_messaging_plugin.schema import (  # noqa: E402
    PEER_BINDING_NAMESPACE,
    get_peer_binding_schema,
)

NS = AGENT_ROLE_BINDING_NAMESPACE

# Arbitrary, operator-defined-shaped role — proves opacity (never special-cased).
_ARBITRARY_ROLE = "zz-Ω arbitrary/role #7!"

_passed = 0
_failed: list[str] = []


def _check(condition: object, label: str) -> None:
    global _passed
    if condition:
        _passed += 1
        print(f"  PASS  {label}")
        return
    _failed.append(label)
    print(f"  FAIL  {label}")


def _error_result(code: str, message: str) -> dict[str, Any]:
    """The exact non-completed envelope ``create_error_result`` produces."""
    return {
        "action_status": "error",
        "error": {
            "type": "plugin_error",
            "code": code,
            "message": message,
            "details": {},
            "severity": "error",
            "timestamp": "",
        },
    }


class _StateWithInsert(RealShapeState):
    """RealShapeState + a tracked faithful ``write_state`` for migration callers.

    The claim path must never call this expected-error primitive; ``write_calls``
    pins that absence. Migration still uses ``write_state``, so the fake keeps
    the production UNIQUE behavior for that separate surface.
    """

    def __init__(self) -> None:
        super().__init__()
        self.write_calls = 0

    def write_state(self, namespace: str, data: dict[str, Any]) -> dict[str, Any]:
        self.write_calls += 1
        table = str(data["table"])
        record = dict(cast("dict[str, Any]", data["record"]))
        external_id = record.get("external_id")
        rows = self.rows(namespace, table)
        # UNIQUE(external_id) ignores is_deleted — conflict on ANY matching row.
        if any(r.get("external_id") == external_id for r in rows):
            return _error_result(
                "state.write_failed",
                f"duplicate key value violates unique constraint: "
                f"external_id={external_id!r}",
            )
        record.setdefault("is_deleted", 0)
        rows.append(record)
        return {
            "action_status": "completed",
            "data": {"result": {"generated_id": f"gen-{len(rows)}", "inserted": 1}},
        }


class _StatePersistentUpsertFault(_StateWithInsert):
    """``upsert_state`` always fails with a non-conflict provider fault."""

    def upsert_state(
        self, namespace: str, data: dict[str, Any],
    ) -> dict[str, Any]:
        del namespace, data
        return _error_result(
            "state.upsert_failed",
            'null value in column "agent_instance_id" violates not-null constraint '
            "(scripted persistent fault)",
        )


def _state() -> StateManagementInterface:
    return cast("StateManagementInterface", _StateWithInsert())


def _session_claim(agent_id: str, agi: str, sid: str, label: str = "lbl") -> HolderClaim:
    return HolderClaim(
        holder_kind=HOLDER_KIND_SESSION,
        holder_identity={"agent_id": agent_id, "session_label": label},
        agent_instance_id=agi,
        agent_session_id=sid,
        session_label=label,
    )


def _binding_row(state: StateManagementInterface, name: str) -> dict[str, Any]:
    rows = cast(_StateWithInsert, state).rows(NS, TABLE_ROLE_BINDING)
    return next(r for r in rows if r.get(COL_ROLE) == name)


# ---------------------------------------------------------------------------
# §5.1 claim / displace / self-re-claim CAS
# ---------------------------------------------------------------------------


def test_first_claim() -> None:
    state = _state()
    outcome = claim_role_binding_v4(
        state, name=_ARBITRARY_ROLE, claim=_session_claim("claude_code", "agi-1", "sess-1"),
    )
    _check(outcome["action"] == "claimed", "first claim → action='claimed'")
    _check(outcome["prior"] is None, "first claim → no prior holder")
    row = _binding_row(state, _ARBITRARY_ROLE)
    _check(row.get(COL_CLAIM_EPOCH) == 0, "first claim → claim_epoch 0")
    _check(
        row.get(COL_AGENT_SESSION_ID) == "sess-1",
        "first claim → the claimant's stable session id is stored (reroute can key on it)",
    )


def test_self_reclaim_idempotent() -> None:
    state = _state()
    claim_role_binding_v4(state, name=_ARBITRARY_ROLE, claim=_session_claim("cc", "agi-1", "sess-1"))
    # Same stable session id, ROTATED agi (a reconnect re-claim).
    outcome = claim_role_binding_v4(
        state, name=_ARBITRARY_ROLE, claim=_session_claim("cc", "agi-1b", "sess-1"),
    )
    _check(outcome["action"] == "refreshed", "self-re-claim (same session id) → 'refreshed', not 'displaced'")
    _check(outcome["prior"] is None, "self-re-claim → no prior (no handover)")
    row = _binding_row(state, _ARBITRARY_ROLE)
    _check(row.get(COL_CLAIM_EPOCH) == 0, "self-re-claim → NO epoch bump")
    _check(row.get(COL_AGENT_INSTANCE_ID) == "agi-1b", "self-re-claim → re-points the instance in place")


def test_displace_bumps_epoch_and_returns_prior() -> None:
    state = _state()
    claim_role_binding_v4(state, name=_ARBITRARY_ROLE, claim=_session_claim("cc", "agi-1", "sess-1"))
    outcome = claim_role_binding_v4(
        state, name=_ARBITRARY_ROLE, claim=_session_claim("codex", "agi-2", "sess-2"),
    )
    _check(outcome["action"] == "displaced", "different session claiming a held role → 'displaced'")
    prior = outcome["prior"]
    _check(
        prior is not None and prior.agent_session_id == "sess-1",
        "displace → PRIOR holder (sess-1) returned for the §5.4 session-routed notify",
    )
    row = _binding_row(state, _ARBITRARY_ROLE)
    _check(row.get(COL_CLAIM_EPOCH) == 1, "displace → claim_epoch bumped 0→1")
    _check(row.get(COL_AGENT_SESSION_ID) == "sess-2", "displace → new holder's session id written")


def test_claim_conflict_uses_quiet_do_nothing_upsert() -> None:
    fake = _StateWithInsert()
    state = cast("StateManagementInterface", fake)
    claim_role_binding_v4(
        state,
        name=_ARBITRARY_ROLE,
        claim=_session_claim("cc", "agi-1", "sess-1"),
    )
    outcome = claim_role_binding_v4(
        state,
        name=_ARBITRARY_ROLE,
        claim=_session_claim("cc", "agi-1b", "sess-1"),
    )
    _check(
        outcome["action"] == "refreshed" and fake.write_calls == 0,
        "first claim + existing-row refresh use completed DO-NOTHING upsert "
        "(zero exception-driven write_state calls / ERROR tracebacks)",
    )


def test_persistent_fault_not_masked_as_contention() -> None:
    state = cast("StateManagementInterface", _StatePersistentUpsertFault())
    raised = False
    try:
        claim_role_binding_v4(state, name=_ARBITRARY_ROLE, claim=_session_claim("cc", "agi-1", "sess-1"))
    except StateOperationError:
        raised = True
    _check(
        raised,
        "a persistent non-conflict upsert fault fails immediately as "
        "StateOperationError (never masked as contention)",
    )


# ---------------------------------------------------------------------------
# No-tombstone invariant (load-bearing)
# ---------------------------------------------------------------------------


def test_hard_delete_release_allows_reclaim() -> None:
    state = _state()
    claim_role_binding_v4(state, name=_ARBITRARY_ROLE, claim=_session_claim("cc", "agi-1", "sess-1"))
    # v4 release = HARD delete (slice-D wraps this; the invariant is what matters).
    state.delete_records(
        NS,
        {"table": TABLE_ROLE_BINDING, "filters": {"external_id": role_binding_external_id(_ARBITRARY_ROLE)}, "soft_delete": False},
    )
    _check(len(cast(_StateWithInsert, state).rows(NS, TABLE_ROLE_BINDING)) == 0, "hard-delete removes the row (no tombstone)")
    outcome = claim_role_binding_v4(state, name=_ARBITRARY_ROLE, claim=_session_claim("codex", "agi-2", "sess-2"))
    _check(outcome["action"] == "claimed", "claim → HARD-delete release → re-claim SUCCEEDS (fresh 'claimed', no deadlock)")


def test_soft_delete_tombstone_deadlocks() -> None:
    state = _state()
    claim_role_binding_v4(state, name=_ARBITRARY_ROLE, claim=_session_claim("cc", "agi-1", "sess-1"))
    # The FORBIDDEN soft release — leaves an is_deleted=1 tombstone.
    state.delete_records(
        NS,
        {"table": TABLE_ROLE_BINDING, "filters": {"external_id": role_binding_external_id(_ARBITRARY_ROLE)}, "soft_delete": True},
    )
    raised = False
    try:
        claim_role_binding_v4(state, name=_ARBITRARY_ROLE, claim=_session_claim("codex", "agi-2", "sess-2"))
    except RoleClaimContendedError:
        raised = True
    _check(
        raised,
        "a soft-delete tombstone DEADLOCKS the slot (INSERT conflicts on the dead row, is_deleted=0 re-read+CAS hide it) — why release MUST hard-delete",
    )


# ---------------------------------------------------------------------------
# §5.0 act-time ownership re-check (holds_role)
# ---------------------------------------------------------------------------


def test_holds_role_recheck() -> None:
    state = _state()
    claim_role_binding_v4(state, name=_ARBITRARY_ROLE, claim=_session_claim("cc", "agi-1", "sess-1"))
    _check(holds_role(state, _ARBITRARY_ROLE, "sess-1"), "holds_role → True for the current holder's session id")
    _check(not holds_role(state, _ARBITRARY_ROLE, "sess-OTHER"), "holds_role → False for a non-holder session id")
    _check(not holds_role(state, _ARBITRARY_ROLE, ""), "holds_role → False for empty session id (never an identity)")
    _check(not holds_role(state, "nobody-holds-this", "sess-1"), "holds_role → False for a vacant role")


def test_holds_role_reference_abort_after_displace() -> None:
    state = _state()
    claim_role_binding_v4(state, name=_ARBITRARY_ROLE, claim=_session_claim("cc", "agi-1", "sess-1"))
    claim_role_binding_v4(state, name=_ARBITRARY_ROLE, claim=_session_claim("codex", "agi-2", "sess-2"))
    _check(
        not holds_role(state, _ARBITRARY_ROLE, "sess-1"),
        "reference abort: a DISPLACED session's act-time re-check → False (must not act on a stale routing decision)",
    )
    _check(holds_role(state, _ARBITRARY_ROLE, "sess-2"), "the new holder's act-time re-check → True")


# ---------------------------------------------------------------------------
# Reverse-lookup + fail-loud dup policy (peer_registry)
# ---------------------------------------------------------------------------


def _registry() -> PeerRegistry:
    store = open_store(
        get_peer_binding_schema(), namespace=PEER_BINDING_NAMESPACE, backend="in_memory",
    )
    return PeerRegistry(bindings_store=store)


def _binding(agent_id: str, agi: str, label: str, sid: str, bridge: str) -> BridgeBinding:
    return BridgeBinding(
        bridge_id=bridge,
        agent_id=agent_id,
        agent_instance_id=agi,
        session_label=label,
        parent_pid=None,
        agent_session_id=sid,
    )


def test_resolve_by_agent_session_id() -> None:
    registry = _registry()
    registry.register(_binding("claude_code", "agi-1", "L1", "sess-1", "b1"))
    found = registry.resolve_by_agent_session_id("sess-1")
    _check(found is not None and found.agent_instance_id == "agi-1", "resolve_by_agent_session_id → the live binding")
    _check(registry.resolve_by_agent_session_id("no-such-session") is None, "resolve_by_agent_session_id → None for no match")
    _check(registry.resolve_by_agent_session_id("") is None, "resolve_by_agent_session_id → None for an empty session id")


def test_agent_session_id_for_instance() -> None:
    registry = _registry()
    registry.register(_binding("claude_code", "agi-1", "L1", "sess-1", "b1"))
    _check(
        registry.agent_session_id_for_instance("agi-1") == "sess-1",
        "agent_session_id_for_instance sources the claimant's stable id from its OWN binding (REL-07(1))",
    )
    _check(registry.agent_session_id_for_instance("unregistered") == "", "agent_session_id_for_instance → '' for an unregistered instance")


def test_resolve_by_session_id_dup_fails_loud() -> None:
    registry = _registry()
    # Two live bindings sharing one session id (different bridge/instance/label so
    # register's sweep does not evict either) — an invariant breach.
    registry.register(_binding("claude_code", "agi-2", "L2", "dup", "b2"))
    registry.register(_binding("codex", "agi-3", "L3", "dup", "b3"))
    raised = False
    try:
        registry.resolve_by_agent_session_id("dup")
    except PeerSessionAmbiguousError:
        raised = True
    _check(raised, "resolve_by_agent_session_id → PeerSessionAmbiguousError on >1 match (fail-loud dup policy)")


# ---------------------------------------------------------------------------
# Reroute acceptance — the REL-07(1) fix mechanism (old-table live path)
# ---------------------------------------------------------------------------


def test_reroute_matches_with_session_id() -> None:
    # §9 CUTOVER: refresh_role_binding_cas now targets the v4 role_binding table, so
    # the reconnect reroute keys off a v4 claim (the live path post-cutover).
    state = _state()
    claim_role_binding_v4(state, name=_ARBITRARY_ROLE, claim=_session_claim("claude_code", "agi-1", "sess-1", "L"))
    matched = refresh_role_binding_cas(
        state, agent_session_id="sess-1", new_agent_instance_id="agi-reconnect",
    )
    _check(matched == 1, "post-cutover: a v4 binding WITH a session id → reconnect CAS MATCHES (reroute heals a claimed role)")
    rows = cast(_StateWithInsert, state).rows(NS, TABLE_ROLE_BINDING)
    row = next(r for r in rows if r.get(COL_ROLE) == _ARBITRARY_ROLE)
    _check(row.get(COL_AGENT_INSTANCE_ID) == "agi-reconnect", "reroute re-points the v4 role to the reconnected instance")


def test_reroute_empty_session_id_matches_nothing() -> None:
    state = _state()
    _check(
        refresh_role_binding_cas(state, agent_session_id="", new_agent_instance_id="agi-y") == 0,
        "refresh with an EMPTY session id → matches NOTHING (the guard; carry-fwd (c) now also rejects empty-session claims upstream)",
    )


def main() -> int:
    print("=== role-model v4 slice-B smoke ===")
    test_first_claim()
    test_self_reclaim_idempotent()
    test_displace_bumps_epoch_and_returns_prior()
    test_claim_conflict_uses_quiet_do_nothing_upsert()
    test_persistent_fault_not_masked_as_contention()
    test_hard_delete_release_allows_reclaim()
    test_soft_delete_tombstone_deadlocks()
    test_holds_role_recheck()
    test_holds_role_reference_abort_after_displace()
    test_resolve_by_agent_session_id()
    test_agent_session_id_for_instance()
    test_resolve_by_session_id_dup_fails_loud()
    test_reroute_matches_with_session_id()
    test_reroute_empty_session_id_matches_nothing()
    print(f"\n{_passed} passed, {len(_failed)} failed")
    if _failed:
        for label in _failed:
            print(f"  FAILED: {label}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
