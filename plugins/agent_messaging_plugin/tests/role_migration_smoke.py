#!/usr/bin/env python3
"""Slice-D Phase-1 smoke — the §9 migration copy, parity proof, v4 release wrapper,
and the carry-forward-c reject predicate (no pytest, no DB).

Phase 1 of the campaign-closing cutover slice (design §9): the store-level migration
machinery — idempotent, tombstone-safe, NO raw SQL. The LIVE reader/writer flip (§9
step 5) is Phase 2 (lands last, after the INF-01 window).

Covers:
  migrate_agent_role_binding_to_v4:
    * copies non-deleted legacy rows → role_binding (holder_kind=session, agi/session_id
      copied, holder_identity={agent_id,session_label}, claim_epoch=0) + the role entity;
    * REAL-SHAPE: valid rows with EMPTY / '__unclaimed__' agent_session_id (predate REL-07)
      COPY verbatim (nullable column) + PASS parity — the reject is a live-CLAIM gate, not
      a migrate gate. This is the exact live-data condition that blocked the blue deploy;
    * a soft-deleted (is_deleted=1) legacy row is NOT copied (carry-fwd b: no tombstones);
    * idempotent — a re-run copies 0 and buckets the skip as CONFLICT (re-read
      disambiguates the store's idempotent conflict-skip from a genuine write REJECTION).
  the schema-aware fake (_real_state_fake): a role_binding write naming a column outside
    get_role_binding_schema (a phantom top-level `session_label`) is REJECTED — the
    class-closing guard so no v4 writer can drift from the declared schema untested.
  verify_migration_parity:
    * ok=True when every non-deleted legacy external_id is present in role_binding;
    * ok=False + missing when a legacy binding is absent from v4 (gates the cutover);
    * a v4-only extra row (a system slot born in v4) does NOT break parity (subset check).
  release_role_binding_v4: HARD-deletes the role_binding row (no tombstone).
  session_claim_requires_session_id (carry-fwd c): session+empty→True; session+id→False;
    provider+empty→False.

Run:
    SOLET_NAME=<name>-test .venv/bin/python3 \
        plugins/agent_messaging_plugin/tests/role_migration_smoke.py
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
    COL_HOLDER_IDENTITY,
    COL_HOLDER_KIND,
    COL_ROLE,
    HOLDER_KIND_INFERENCE_PROVIDER,
    HOLDER_KIND_SESSION,
    SYS_AUTONOMIC_SLOT,
    TABLE_AGENT_ROLE_BINDING,
    TABLE_ROLE,
    TABLE_ROLE_BINDING,
    role_binding_external_id,
)

from agent_messaging_plugin.role_binding_store import (  # noqa: E402
    CutoverParityError,
    RoleBindingVacantError,
    migrate_agent_role_binding_to_v4,
    release_role_binding_v4,
    resolve_role_binding_v4,
    run_cutover_migration_at_readiness,
    session_claim_requires_session_id,
    verify_migration_parity,
)

NS = AGENT_ROLE_BINDING_NAMESPACE

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


def _state() -> StateManagementInterface:
    # The shared RealShapeState now carries the faithful write_state (INSERT; live
    # external_id conflict → non-completed, never raise) AND the schema-aware column
    # guard, so the migration smoke enforces get_role_binding_schema on every v4 write
    # — a phantom top-level column is rejected exactly as postgres would (the class the
    # live cutover exposed). No migration-only subclass is needed.
    return cast("StateManagementInterface", RealShapeState())


def _seed_legacy(
    state: StateManagementInterface, *, role: str, agent_id: str, agi: str,
    sid: str, label: str, is_deleted: int = 0,
) -> None:
    state.upsert_state(
        NS,
        {
            "table": TABLE_AGENT_ROLE_BINDING,
            "record": {
                "external_id": role_binding_external_id(role),
                COL_ROLE: role,
                "agent_id": agent_id,
                COL_AGENT_INSTANCE_ID: agi,
                COL_AGENT_SESSION_ID: sid,
                "session_label": label,
                "claimed_at": "2026-07-02T00:00:00+00:00",
                "is_deleted": is_deleted,
            },
            "conflict_columns": ["external_id"],
        },
    )


def _v4_rows(state: StateManagementInterface) -> list[dict[str, Any]]:
    return cast(RealShapeState, state).rows(NS, TABLE_ROLE_BINDING)


# ---------------------------------------------------------------------------
# migrate_agent_role_binding_to_v4
# ---------------------------------------------------------------------------


def test_migrate_copies_and_maps() -> None:
    state = _state()
    _seed_legacy(state, role="Architect", agent_id="claude_code", agi="agi-1", sid="sess-1", label="Arch")
    result = migrate_agent_role_binding_to_v4(state)
    _check(result["source_rows"] == 1 and result["copied"] == 1, "migrate: 1 legacy row → 1 copied")
    rows = _v4_rows(state)
    _check(len(rows) == 1, "migrate: one role_binding row written")
    row = rows[0]
    _check(row.get(COL_HOLDER_KIND) == HOLDER_KIND_SESSION, "migrate: holder_kind=session")
    _check(row.get(COL_AGENT_SESSION_ID) == "sess-1", "migrate: agent_session_id copied verbatim")
    _check(row.get(COL_CLAIM_EPOCH) == 0, "migrate: claim_epoch starts at 0")
    identity = row.get(COL_HOLDER_IDENTITY)
    _check(
        isinstance(identity, dict) and identity.get("agent_id") == "claude_code",
        "migrate: holder_identity carries {agent_id, session_label}",
    )
    entity_rows = cast(RealShapeState, state).rows(NS, TABLE_ROLE)
    _check(any(e.get(COL_ROLE) == "Architect" for e in entity_rows), "migrate: role entity upserted (§5.5)")
    # the migrated row is resolvable via the v4 typed resolver
    resolved = resolve_role_binding_v4(state, "Architect")
    _check(resolved.agent_id == "claude_code" and resolved.agent_session_id == "sess-1", "migrate: v4 resolve of the migrated row")


def test_migrate_skips_tombstones() -> None:
    state = _state()
    _seed_legacy(state, role="Ghost", agent_id="cc", agi="agi-g", sid="sess-g", label="G", is_deleted=1)
    result = migrate_agent_role_binding_to_v4(state)
    _check(
        result["source_rows"] == 0 and result["copied"] == 0,
        "migrate: a soft-deleted (is_deleted=1) legacy row is NOT copied (carry-fwd b: no tombstones)",
    )
    _check(len(_v4_rows(state)) == 0, "migrate: no role_binding row for a tombstone")


def test_migrate_idempotent() -> None:
    state = _state()
    _seed_legacy(state, role="Coordinator", agent_id="cc", agi="agi-c", sid="sess-c", label="Co")
    migrate_agent_role_binding_to_v4(state)
    result2 = migrate_agent_role_binding_to_v4(state)
    _check(
        result2["copied"] == 0
        and result2["skipped_conflict"] == 1
        and result2["skipped_rejected"] == 0,
        "migrate: a re-run copies 0, skips 1 as CONFLICT not rejected (the re-read "
        "disambiguates the store's idempotent conflict-skip from a genuine write fault)",
    )
    _check(len(_v4_rows(state)) == 1, "migrate: idempotent → still exactly one role_binding row")


def test_migrate_real_shape_empty_and_unclaimed_session_id() -> None:
    """The LIVE data shape that BLOCKED the cutover: valid legacy rows (non-empty
    agent_id) whose agent_session_id is EMPTY or the '__unclaimed__' sentinel — they
    predate REL-07 session-id threading. agent_session_id is a NULLABLE v4 column, so
    these MUST copy VERBATIM. The empty-session REJECT is a live-CLAIM-time gate
    (session_claim_requires_session_id), NOT a migrate gate — the migrate copies
    reality (§4.5.3). Rows self-heal to a real session id on each holder's next claim."""
    state = _state()
    _seed_legacy(state, role="LegacyEmpty", agent_id="claude_code", agi="agi-e", sid="", label="E")
    _seed_legacy(state, role="LegacyUnclaimed", agent_id="example", agi="agi-u", sid="__unclaimed__", label="U")
    result = migrate_agent_role_binding_to_v4(state)
    _check(
        result["source_rows"] == 2
        and result["copied"] == 2
        and result["skipped_rejected"] == 0
        and result["skipped_malformed"] == 0,
        "migrate: empty + '__unclaimed__' agent_session_id rows COPY (nullable column; "
        "the reject is a CLAIM-time gate, not a migrate gate) — the live-deploy fix",
    )
    rows = _v4_rows(state)
    empty_row = next(r for r in rows if r.get(COL_ROLE) == "LegacyEmpty")
    _check(
        empty_row.get(COL_AGENT_SESSION_ID) == "",
        "migrate: empty agent_session_id copied verbatim (not dropped, not defaulted)",
    )
    _check(
        verify_migration_parity(state)["ok"] is True,
        "parity: empty/'__unclaimed__' session-id rows PASS parity → the cutover is NOT "
        "blocked (this is exactly the condition that failed the live blue deploy)",
    )


def test_schema_aware_fake_rejects_phantom_column() -> None:
    """The CLASS-CLOSING guard, proven directly: a v4 role_binding write naming a column
    OUTSIDE get_role_binding_schema is REJECTED by the schema-aware fake (models postgres
    'column does not exist'). This is the check that would have caught the phantom
    top-level `session_label` column RED before it shipped — session_label lives in
    holder_identity JSON, it is NOT a role_binding column. No future v4 writer can drift
    from the declared schema without a smoke going red."""
    state = _state()
    clean = state.write_state(NS, {"table": TABLE_ROLE_BINDING, "record": {
        "external_id": role_binding_external_id("Clean"), COL_ROLE: "Clean",
        COL_HOLDER_KIND: HOLDER_KIND_SESSION, COL_CLAIM_EPOCH: 0,
    }})
    _check(
        clean.get("action_status") == "completed",
        "schema-aware fake: a schema-clean role_binding INSERT is ACCEPTED",
    )
    phantom = state.write_state(NS, {"table": TABLE_ROLE_BINDING, "record": {
        "external_id": role_binding_external_id("Phantom"), COL_ROLE: "Phantom",
        COL_HOLDER_KIND: HOLDER_KIND_SESSION, COL_CLAIM_EPOCH: 0,
        "session_label": "PHANTOM",  # NOT a v4 column — must be rejected
    }})
    _check(
        phantom.get("action_status") != "completed",
        "schema-aware fake: a role_binding INSERT naming a phantom 'session_label' "
        "column is REJECTED (the guard that closes the shipped-bug class)",
    )
    _check(
        not any(r.get(COL_ROLE) == "Phantom" for r in _v4_rows(state)),
        "schema-aware fake: the rejected phantom write leaves NO row (as postgres would)",
    )


# ---------------------------------------------------------------------------
# verify_migration_parity
# ---------------------------------------------------------------------------


def test_parity_ok_after_full_migrate() -> None:
    state = _state()
    _seed_legacy(state, role="R1", agent_id="a", agi="i1", sid="s1", label="l1")
    _seed_legacy(state, role="R2", agent_id="a", agi="i2", sid="s2", label="l2")
    migrate_agent_role_binding_to_v4(state)
    parity = verify_migration_parity(state)
    _check(parity["ok"] is True and parity["missing"] == [], "parity: ok=True after a full migrate")
    _check(parity["legacy_count"] == 2 and parity["v4_count"] == 2, "parity: counts reported")


def test_parity_fails_on_missing() -> None:
    state = _state()
    _seed_legacy(state, role="R1", agent_id="a", agi="i1", sid="s1", label="l1")
    migrate_agent_role_binding_to_v4(state)
    # a legacy binding that never made it to v4 → parity must FAIL (gates the cutover)
    _seed_legacy(state, role="Orphan", agent_id="a", agi="i9", sid="s9", label="l9")
    parity = verify_migration_parity(state)
    missing = parity["missing"]
    _check(
        parity["ok"] is False
        and isinstance(missing, list)
        and role_binding_external_id("Orphan") in missing,
        "parity: a legacy binding absent from v4 → ok=False + named in missing (fails the cutover loud)",
    )


def test_parity_tolerates_v4_only_extra() -> None:
    state = _state()
    _seed_legacy(state, role="R1", agent_id="a", agi="i1", sid="s1", label="l1")
    migrate_agent_role_binding_to_v4(state)
    # a v4-only row (a system slot born in v4, never in legacy) must NOT break parity
    state.write_state(
        NS,
        {"table": TABLE_ROLE_BINDING, "record": {
            "external_id": role_binding_external_id(SYS_AUTONOMIC_SLOT), COL_ROLE: SYS_AUTONOMIC_SLOT,
            COL_HOLDER_KIND: HOLDER_KIND_SESSION, COL_CLAIM_EPOCH: 0,
        }},
    )
    parity = verify_migration_parity(state)
    _check(
        parity["ok"] is True and parity["v4_count"] == 2 and parity["legacy_count"] == 1,
        "parity: a v4-only extra (system slot) does NOT break parity (subset check, not count-equality)",
    )


# ---------------------------------------------------------------------------
# release_role_binding_v4 + the carry-fwd-c reject predicate
# ---------------------------------------------------------------------------


def test_release_v4_hard_deletes() -> None:
    state = _state()
    _seed_legacy(state, role="Temp", agent_id="a", agi="i1", sid="s1", label="l1")
    migrate_agent_role_binding_to_v4(state)
    release_role_binding_v4(state, "Temp")
    _check(len(_v4_rows(state)) == 0, "release_role_binding_v4: HARD-deletes the row (no tombstone lingers)")


def test_session_claim_requires_session_id() -> None:
    _check(
        session_claim_requires_session_id(HOLDER_KIND_SESSION, "") is True,
        "carry-fwd c: session holder + EMPTY agent_session_id → reject (True)",
    )
    _check(
        session_claim_requires_session_id(HOLDER_KIND_SESSION, "sess-1") is False,
        "carry-fwd c: session holder + a session id → allow (False)",
    )
    _check(
        session_claim_requires_session_id(HOLDER_KIND_INFERENCE_PROVIDER, "") is False,
        "carry-fwd c: a provider holder is exempt (no session id required)",
    )


def test_migrate_skips_malformed() -> None:
    state = _state()
    _seed_legacy(state, role="Real", agent_id="cc", agi="i1", sid="s1", label="l1")
    # An empty-agent_id legacy row (the live `smoke_role_minimal_b` detritus shape).
    _seed_legacy(state, role="smoke_role_minimal_b", agent_id="", agi="", sid="__unclaimed__", label="")
    result = migrate_agent_role_binding_to_v4(state)
    _check(
        result["copied"] == 1 and result["skipped_malformed"] == 1,
        "migrate: a malformed legacy row (empty agent_id) is SKIPPED (loud); the well-formed one copies",
    )
    raised = False
    try:
        resolve_role_binding_v4(state, "smoke_role_minimal_b")
    except RoleBindingVacantError:
        raised = True
    _check(raised, "the skipped malformed role → VACANT in v4 (re-claimable), never a live-routing throw")
    _check(
        verify_migration_parity(state)["ok"] is True,
        "parity EXCLUDES the malformed row (same predicate) → ok=True; it never gates the cutover",
    )


def test_run_cutover_happy_and_idempotent() -> None:
    state = _state()
    _seed_legacy(state, role="R1", agent_id="a", agi="i1", sid="s1", label="l1")
    outcome = run_cutover_migration_at_readiness(state)
    _check(outcome["status"] == "completed", "run_cutover: fresh → migrate+parity pass → status='completed'")
    _check(len(_v4_rows(state)) == 1, "run_cutover: the legacy row is copied to v4")
    outcome2 = run_cutover_migration_at_readiness(state)
    _check(outcome2["status"] == "already_done", "run_cutover: re-run is a no-op via the one-shot marker (already_done)")


def test_run_cutover_raises_on_parity_fail_then_converges() -> None:
    state = _state()
    _seed_legacy(state, role="R1", agent_id="a", agi="i1", sid="s1", label="l1")
    cast(RealShapeState, state).fail_next("write")  # migrate's binding INSERT for R1 fails → absent from v4
    raised = False
    try:
        run_cutover_migration_at_readiness(state)
    except CutoverParityError:
        raised = True
    _check(raised, "run_cutover: a well-formed legacy row absent from v4 → CutoverParityError (green REFUSES to serve)")
    # marker NOT set on failure → a re-run (write now succeeds) converges (the quiesce-equivalent loop).
    outcome = run_cutover_migration_at_readiness(state)
    _check(outcome["status"] == "completed", "run_cutover: marker unset on failure → re-run CONVERGES (idempotent)")


def main() -> int:
    print("=== slice-D Phase-1 migration smoke ===")
    test_migrate_copies_and_maps()
    test_migrate_skips_tombstones()
    test_migrate_skips_malformed()
    test_migrate_idempotent()
    test_migrate_real_shape_empty_and_unclaimed_session_id()
    test_schema_aware_fake_rejects_phantom_column()
    test_parity_ok_after_full_migrate()
    test_parity_fails_on_missing()
    test_parity_tolerates_v4_only_extra()
    test_run_cutover_happy_and_idempotent()
    test_run_cutover_raises_on_parity_fail_then_converges()
    test_release_v4_hard_deletes()
    test_session_claim_requires_session_id()
    print(f"\n{_passed} passed, {len(_failed)} failed")
    if _failed:
        for label in _failed:
            print(f"  FAILED: {label}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
