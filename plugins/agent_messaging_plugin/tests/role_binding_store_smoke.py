#!/usr/bin/env python3
"""Unit smoke for the v4 ``role_binding`` store surface (REAL state shapes).

Drives the state-interface binding core against :class:`RealShapeState` — the
shared harness returning the ACTUAL provider ActionResult envelopes
(``action_status`` + nested ``data.result.updated`` + soft-delete semantics; its
``upsert_state(on_conflict='do_nothing')`` faithfully models the completed
``inserted`` bool used by the v4 first-claim race).

This smoke exercises the LIVE v4 role-model surface after the §9 cutover — the
legacy ``agent_role_binding`` claim verb (``claim_role_binding`` v3) was retired
(zero prod callers) and this smoke was migrated onto the v4 ``role_binding``
table (§9.14 item-1 / Lane-2 P8):

  * claim — ``claim_role_binding_v4`` first-claim INSERTs; a same-name claim by a
    DIFFERENT session displaces the holder in place (single row, external_id UNIQUE);
  * resolve — ``resolve_role_binding`` (the live delegate → ``resolve_role_binding_v4``)
    found → ResolvedRole; vacant → RoleBindingVacantError;
  * CAS self-refresh — ``refresh_role_binding_cas`` re-points every role the session
    holds (reads the NESTED ``data.result.updated``); empty / UNCLAIMED session id is
    fail-closed (returns 0);
  * release — ``release_role_binding_v4`` HARD-deletes (``soft_delete=False``): resolve
    after release is vacant (a soft release would still resolve — the no-tombstone
    invariant §5.1);
  * FAIL-LOUD — a provider-error ActionResult RAISES (``StateOperationError`` on the
    claim upsert, CAS update, and resolve query) rather than reading as
    empty/zero success.

Run:
    .venv/bin/python3 plugins/agent_messaging_plugin/tests/role_binding_store_smoke.py
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
    COL_ROLE,
    HOLDER_KIND_SESSION,
    TABLE_ROLE_BINDING,
)
from ananta.llm.agent_messaging.state_results import StateOperationError  # noqa: E402

from agent_messaging_plugin.role_binding_store import (  # noqa: E402
    UNCLAIMED_SESSION_ID,
    HolderClaim,
    RoleBindingVacantError,
    claim_role_binding_v4,
    refresh_role_binding_cas,
    release_role_binding_v4,
    resolve_role_binding,
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


def _state() -> tuple[RealShapeState, StateManagementInterface]:
    fake = RealShapeState()
    return fake, cast(StateManagementInterface, fake)


def _session_claim(
    agent_id: str, agent_instance_id: str, agent_session_id: str, label: str,
) -> HolderClaim:
    """A ``holder_kind='session'`` claim. ``session_label`` lives in
    ``holder_identity`` (that is where the typed §4.6 resolve parse reads it back
    from — it is NOT a top-level column), mirroring the production claim path."""
    return HolderClaim(
        holder_kind=HOLDER_KIND_SESSION,
        holder_identity={"agent_id": agent_id, "session_label": label},
        agent_instance_id=agent_instance_id,
        agent_session_id=agent_session_id,
        session_label=label,
    )


class _PersistentUpsertFault(RealShapeState):
    """``upsert_state`` always returns a non-conflict provider error."""

    def upsert_state(
        self, namespace: str, data: dict[str, Any],
    ) -> dict[str, Any]:
        del namespace, data  # required override signature; the fault is unconditional
        return {
            "action_status": "error",
            "error": {
                "type": "plugin_error",
                "code": "state.upsert_failed",
                "message": (
                    'null value in column "agent_instance_id" violates '
                    "not-null constraint"
                ),
                "details": {},
                "severity": "error",
                "timestamp": "",
            },
        }


def _binding_rows(fake: RealShapeState) -> list[dict[str, object]]:
    return [
        r
        for r in fake.rows(AGENT_ROLE_BINDING_NAMESPACE, TABLE_ROLE_BINDING)
        if r.get("is_deleted", 0) != 1
    ]


def test_resolve_found_and_vacant() -> None:
    state = _state()[1]
    claim_role_binding_v4(
        state, name="Architect",
        claim=_session_claim("claude_code", "agi-1", "sess-A", "Architect"),
    )
    resolved = resolve_role_binding(state, "Architect")
    _check(
        resolved.name == "Architect"
        and resolved.agent_id == "claude_code"
        and resolved.agent_instance_id == "agi-1"
        and resolved.session_label == "Architect",
        "resolve → ResolvedRole routing tuple (agent_id + session_label typed-parsed from holder_identity)",
    )
    try:
        resolve_role_binding(state, "Nobody")
        _check(False, "resolve vacant → RoleBindingVacantError")
    except RoleBindingVacantError:
        _check(True, "resolve vacant → RoleBindingVacantError")


def test_claim_displaces_in_place() -> None:
    fake, state = _state()
    claim_role_binding_v4(
        state, name="Coord",
        claim=_session_claim("claude_code", "agi-1", "sess-A", "Coordinator"),
    )
    claim_role_binding_v4(
        state, name="Coord",
        claim=_session_claim("claude_code", "agi-2", "sess-B", "Coordinator-Dusk"),
    )
    rows = _binding_rows(fake)
    _check(len(rows) == 1, "same-name re-claim → single row (external_id UNIQUE, displace in place)")
    _check(
        rows[0].get(COL_AGENT_INSTANCE_ID) == "agi-2"
        and rows[0].get(COL_AGENT_SESSION_ID) == "sess-B",
        "a DIFFERENT session's claim displaces the routing tuple (incl. agent_session_id) in place",
    )


def test_claim_displaces_unclaimed_incumbent() -> None:
    fake, state = _state()
    # A legacy UNCLAIMED-session incumbent (the kind migrate_agent_role_binding_to_v4
    # can carry forward) is displaced in place by a real-session claim → single row,
    # real session id. Under v4 a claim always carries a real session id (REL-07); the
    # UNCLAIMED holder is the pre-existing row, never the incoming claim.
    claim_role_binding_v4(
        state, name="GitCtl",
        claim=_session_claim("claude_code", "agi-legacy", UNCLAIMED_SESSION_ID, "Git-Controller"),
    )
    claim_role_binding_v4(
        state, name="GitCtl",
        claim=_session_claim("claude_code", "agi-live", "sess-G", "Git-Controller"),
    )
    rows = _binding_rows(fake)
    _check(
        len(rows) == 1 and rows[0].get(COL_AGENT_SESSION_ID) == "sess-G",
        "real-session claim displaces an UNCLAIMED-session incumbent in place → single row, real session id",
    )


def test_cas_refresh_repoints_all_held_roles() -> None:
    fake, state = _state()
    for role in ("Architect", "Coordinator"):
        claim_role_binding_v4(
            state, name=role,
            claim=_session_claim("claude_code", "agi-old", "sess-X", role),
        )
    claim_role_binding_v4(
        state, name="GitCtl",
        claim=_session_claim("claude_code", "agi-other", "sess-Y", "Git-Controller"),
    )
    affected = refresh_role_binding_cas(
        state, agent_session_id="sess-X", new_agent_instance_id="agi-new",
    )
    # Reads the NESTED data.result.updated — flat data.updated would read 0 here.
    _check(affected == 2, "CAS on session id alone re-points BOTH held roles (returns 2)")
    repointed = {
        str(r[COL_ROLE]): str(r[COL_AGENT_INSTANCE_ID]) for r in _binding_rows(fake)
    }
    _check(
        repointed.get("Architect") == "agi-new"
        and repointed.get("Coordinator") == "agi-new"
        and repointed.get("GitCtl") == "agi-other",
        "CAS re-points only the matching session's roles; other session untouched",
    )


def test_cas_no_match_and_fail_closed() -> None:
    fake, state = _state()
    claim_role_binding_v4(
        state, name="Architect",
        claim=_session_claim("claude_code", "agi-1", "sess-A", "Architect"),
    )
    _check(
        refresh_role_binding_cas(
            state, agent_session_id="sess-NOBODY", new_agent_instance_id="agi-x",
        ) == 0,
        "CAS with an unheld session id → 0 (no-op → caller re-claims)",
    )
    _check(
        refresh_role_binding_cas(
            state, agent_session_id="", new_agent_instance_id="agi-x",
        ) == 0,
        "CAS with EMPTY session id → 0 fail-closed",
    )
    _check(
        refresh_role_binding_cas(
            state, agent_session_id=UNCLAIMED_SESSION_ID, new_agent_instance_id="agi-x",
        ) == 0,
        "CAS with UNCLAIMED sentinel → 0 fail-closed",
    )
    _check(
        _binding_rows(fake)[0].get(COL_AGENT_INSTANCE_ID) == "agi-1",
        "fail-closed CAS attempts leave the real binding untouched",
    )


def test_release_hard_deletes() -> None:
    fake, state = _state()
    claim_role_binding_v4(
        state, name="Architect",
        claim=_session_claim("claude_code", "agi-1", "sess-A", "Architect"),
    )
    out = release_role_binding_v4(state, "Architect")
    _check(out == {"released": True, "name": "Architect"}, "release → {released, name}")
    _check(not _binding_rows(fake), "release HARD-deletes the row (soft_delete=False)")
    # A soft release would leave is_deleted=1 and still RESOLVE (no-tombstone §5.1); the
    # hard delete + is_deleted=0 filter make resolve vacant.
    try:
        resolve_role_binding(state, "Architect")
        _check(False, "resolve after release → vacant")
    except RoleBindingVacantError:
        _check(True, "resolve after release → vacant")


def test_fail_loud_on_provider_error() -> None:
    fake, state = _state()
    claim_role_binding_v4(
        state, name="Architect",
        claim=_session_claim("claude_code", "agi-1", "sess-A", "Architect"),
    )
    # A failed CAS update must RAISE, not read as 0-rows success.
    fake.fail_next("update")
    try:
        refresh_role_binding_cas(
            state, agent_session_id="sess-A", new_agent_instance_id="agi-2",
        )
        _check(False, "failed update → StateOperationError (not silent 0)")
    except StateOperationError:
        _check(True, "failed update → StateOperationError (not silent 0)")
    # A claim upsert fault must fail immediately — expected role conflicts now use
    # the completed inserted=False shape, so no provider error is control flow.
    persistent = _PersistentUpsertFault()
    try:
        claim_role_binding_v4(
            cast(StateManagementInterface, persistent), name="X",
            claim=_session_claim("c", "i", "s", "l"),
        )
        _check(False, "claim upsert fault → StateOperationError (not silent success)")
    except StateOperationError:
        _check(True, "claim upsert fault → StateOperationError (not silent success)")
    # A failed resolve query must RAISE, not read as vacant.
    fake.fail_next("query")
    try:
        resolve_role_binding(state, "Architect")
        _check(False, "failed resolve query → StateOperationError (not silent vacant)")
    except StateOperationError:
        _check(True, "failed resolve query → StateOperationError (not silent vacant)")


def main() -> int:
    print("=== v4 role_binding store smoke (REAL shapes) ===")
    test_resolve_found_and_vacant()
    test_claim_displaces_in_place()
    test_claim_displaces_unclaimed_incumbent()
    test_cas_refresh_repoints_all_held_roles()
    test_cas_no_match_and_fail_closed()
    test_release_hard_deletes()
    test_fail_loud_on_provider_error()
    print(f"\n{_passed} passed, {len(_failed)} failed")
    if _failed:
        for label in _failed:
            print(f"  FAILED: {label}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
