#!/usr/bin/env python3
"""Unit smoke for the D1 ``managed_session`` ledger + ``session_transition``
audit trail (``session_lifecycle_store.py``), against ``RealShapeState``
(real provider ActionResult envelopes, not a hand-rolled convenience fake).

TWO GUARDS, TWO TOKENS (AMEND 2b + the second-guard-vacuous trap): this smoke
proves each guard fires on its OWN failure mode, not the other's —

  - an ILLEGAL edge (not in the §3.2 matrix) is refused by the Python check
    BEFORE any state write — ``IllegalLifecycleTransitionError``, and the
    ledger row is verifiably untouched (still in ``from_state``), proving
    the CAS was never reached;
  - a LEGAL edge that loses its race (another writer already moved the row)
    raises ``StaleLifecycleStateError`` from the predicated ``update_state``
    itself — exercised by actually racing it (moving the row out from under
    the call between read and write), not by asserting the illegal-edge path
    twice under a different name;
  - the audit row is inserted ONLY after a successful ledger write, and NOT
    at all when the ledger write loses its race (ordering, not just
    presence — measured by checking session_transition row count is zero
    after a StaleLifecycleStateError).

Also covers ``backfill_registration``/``set_host_ref`` — the registration-hook
fix (Dawn ruling arm-11511b07): the ``spawning -> live`` edge fires exactly
once on first registration, a reconnect self-corrects identity without
re-firing, and an agent_instance_id with no ledger row is a silent no-op.

Run:
    .venv/bin/python3 plugins/agent_messaging_plugin/tests/session_lifecycle_store_smoke.py
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import TYPE_CHECKING, cast

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT / "ananta" / "src"))
sys.path.insert(0, str(REPO_ROOT / "plugins" / "agent_messaging_plugin" / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

if TYPE_CHECKING:
    from ananta.interfaces.state_management_interface import StateManagementInterface

from _real_state_fake import RealShapeState  # noqa: E402
from ananta.llm.agent_messaging.role_binding import AGENT_ROLE_BINDING_NAMESPACE  # noqa: E402
from ananta.llm.agent_messaging.state_results import require_records  # noqa: E402

from agent_messaging_plugin.http_routes import (  # noqa: E402
    _managed_session_registration_backfill,
)
from agent_messaging_plugin.schema import (  # noqa: E402
    LIFECYCLE_IDLE,
    LIFECYCLE_LIVE,
    LIFECYCLE_RETIRED,
    LIFECYCLE_SPAWNING,
    LIFECYCLE_TERMINATED,
    TABLE_SESSION_TRANSITION,
)
from agent_messaging_plugin.session_lifecycle_store import (  # noqa: E402
    IllegalLifecycleTransitionError,
    ManagedSessionSpec,
    SessionNotFoundError,
    StaleLifecycleStateError,
    backfill_registration,
    insert_managed_session,
    iter_managed_sessions_unbounded,
    list_managed_sessions,
    read_managed_session,
    set_host_ref,
    transition_lifecycle_state,
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


def _transition_count(state: StateManagementInterface, agent_instance_id: str) -> int:
    result = state.query_state(
        AGENT_ROLE_BINDING_NAMESPACE,
        {"table": TABLE_SESSION_TRANSITION, "filters": {"agent_instance_id": agent_instance_id}},
    )
    return len(require_records(result))


def test_insert_and_read() -> None:
    state = _state()
    spec = ManagedSessionSpec(
        agent_instance_id="agi-spawn-1",
        lane_id="lane-x",
        brief_ref="workbench/brief.md",
        work_class="analysis_deliverable",
        budget_line="budget-1",
        host="headless",
        spawned_by_instance_id="agi-spawner",
        spawned_by_role="Claude-C",
        directed_by="operator:none",
    )
    insert_managed_session(state, spec)
    row = read_managed_session(state, "agi-spawn-1")
    _check(row["lifecycle_state"] == LIFECYCLE_SPAWNING, "fresh spawn lands in 'spawning' state")
    _check(row["lane_id"] == "lane-x", "ledger row carries lineage (lane_id)")

    not_found = False
    try:
        read_managed_session(state, "agi-nonexistent")
    except SessionNotFoundError:
        not_found = True
    _check(not_found, "reading an unknown instance raises SessionNotFoundError")


def test_list_filters() -> None:
    state = _state()
    insert_managed_session(
        state,
        ManagedSessionSpec(
            agent_instance_id="agi-spawn-1",
            lane_id="lane-x",
            brief_ref="",
            work_class="analysis_deliverable",
            budget_line="budget-1",
            host="headless",
        ),
    )
    insert_managed_session(
        state,
        ManagedSessionSpec(
            agent_instance_id="agi-spawn-2",
            lane_id="lane-y",
            brief_ref="",
            work_class="read_only",
            budget_line="budget-2",
            host="operator",
        ),
    )
    lane_x_rows = list_managed_sessions(state, {"lane_id": "lane-x"})
    _check(
        len(lane_x_rows) == 1 and lane_x_rows[0]["agent_instance_id"] == "agi-spawn-1",
        "list_managed_sessions filters by lane_id",
    )
    all_rows = list(
        iter_managed_sessions_unbounded(
            state,
            reason="store smoke intentionally verifies all inserted fixture rows",
        )
    )
    _check(len(all_rows) == 2, "the named full-ledger helper returns all fixture rows")


def test_transition_guards() -> None:
    """TWO GUARDS, TWO TOKENS, chained on ONE evolving row: legal transition
    lands (ledger + audit), an illegal edge is refused BEFORE any write
    (ledger untouched), then a legal edge that loses its race to a
    concurrent writer is refused by the predicated write itself (ledger
    left at the WINNER's state, no audit row for the loser)."""
    state = _state()
    insert_managed_session(
        state,
        ManagedSessionSpec(
            agent_instance_id="agi-spawn-1",
            lane_id="lane-x",
            brief_ref="",
            work_class="analysis_deliverable",
            budget_line="budget-1",
            host="headless",
        ),
    )

    # --- legal transition succeeds: ledger + audit both land ---
    transition_lifecycle_state(
        state,
        agent_instance_id="agi-spawn-1",
        from_state=LIFECYCLE_SPAWNING,
        to_state=LIFECYCLE_LIVE,
        directed_by="operator:none",
        reason="registration hook",
    )
    _check(
        read_managed_session(state, "agi-spawn-1")["lifecycle_state"] == LIFECYCLE_LIVE,
        "a legal transition updates lifecycle_state",
    )
    _check(
        _transition_count(state, "agi-spawn-1") == 1,
        "a successful transition inserts exactly one session_transition audit row",
    )

    # --- illegal edge: refused BEFORE any write, ledger untouched ---
    illegal_raised = False
    try:
        transition_lifecycle_state(
            state,
            agent_instance_id="agi-spawn-1",
            from_state=LIFECYCLE_LIVE,
            to_state=LIFECYCLE_RETIRED,
            directed_by="operator:none",
        )
    except IllegalLifecycleTransitionError:
        illegal_raised = True
    _check(
        illegal_raised,
        "live -> retired (not in the matrix) raises IllegalLifecycleTransitionError",
    )
    _check(
        read_managed_session(state, "agi-spawn-1")["lifecycle_state"] == LIFECYCLE_LIVE,
        "an illegal-edge attempt leaves lifecycle_state untouched (the Python check "
        "fired BEFORE any state write — the CAS was never reached)",
    )
    _check(
        _transition_count(state, "agi-spawn-1") == 1,
        "an illegal-edge attempt inserts NO audit row (still exactly one, from the "
        "earlier legal transition)",
    )

    # --- legal edge, lost race: predicated CAS itself refuses ---
    # Simulate the row having ALREADY moved to 'terminated' by a concurrent writer
    # between this caller's read and its write — the CAS predicate (still filtered
    # on from_state='live') then matches zero rows. Reaches directly into the
    # fake's internal row store (test-only) rather than going through the
    # module under test, so the race is genuinely external to it.
    real_rows = state._rows[(AGENT_ROLE_BINDING_NAMESPACE, "managed_session")]  # type: ignore[attr-defined]
    for r in real_rows:
        if r["agent_instance_id"] == "agi-spawn-1":
            r["lifecycle_state"] = LIFECYCLE_TERMINATED
    stale_raised = False
    try:
        transition_lifecycle_state(
            state,
            agent_instance_id="agi-spawn-1",
            from_state=LIFECYCLE_LIVE,
            to_state=LIFECYCLE_IDLE,
            directed_by="operator:none",
        )
    except StaleLifecycleStateError:
        stale_raised = True
    _check(
        stale_raised,
        "a LEGAL edge (live->idle) that lost the race (row already moved to "
        "terminated by a concurrent writer) raises StaleLifecycleStateError — "
        "the PREDICATED WRITE itself refused, a different code path than the "
        "illegal-edge Python check above",
    )
    _check(
        read_managed_session(state, "agi-spawn-1")["lifecycle_state"] == LIFECYCLE_TERMINATED,
        "a stale-race attempt leaves the concurrent writer's state intact "
        "(never clobbered by the losing caller)",
    )
    _check(
        _transition_count(state, "agi-spawn-1") == 1,
        "a lost-race attempt inserts NO audit row (ledger-write-before-audit-insert "
        "ordering — never document a transition that didn't happen)",
    )


def test_backfill_registration_fires_once() -> None:
    state = _state()
    insert_managed_session(
        state,
        ManagedSessionSpec(
            agent_instance_id="agi-spawn-3",
            lane_id="lane-z",
            brief_ref="",
            work_class="read_only",
            budget_line="budget-3",
            host="headless",
        ),
    )
    backfill_registration(
        state,
        agent_instance_id="agi-spawn-3",
        agent_id="claude_code",
        agent_session_id="sess-3",
    )
    row = read_managed_session(state, "agi-spawn-3")
    _check(
        row["agent_session_id"] == "sess-3" and row["agent_id"] == "claude_code",
        "backfill_registration writes agent_session_id/agent_id onto the ledger row",
    )
    _check(
        row["lifecycle_state"] == LIFECYCLE_LIVE,
        "backfill_registration fires spawning->live on first registration",
    )
    _check(
        _transition_count(state, "agi-spawn-3") == 1,
        "backfill_registration's spawning->live edge is audited exactly once",
    )


def test_backfill_reconnect_does_not_refire() -> None:
    """A reconnect (or a late/duplicate register) on a row already past
    'spawning' must re-write identity (self-correcting) but never attempt
    the one-time edge again."""
    state = _state()
    insert_managed_session(
        state,
        ManagedSessionSpec(
            agent_instance_id="agi-spawn-3",
            lane_id="lane-z",
            brief_ref="",
            work_class="read_only",
            budget_line="budget-3",
            host="headless",
        ),
    )
    backfill_registration(
        state,
        agent_instance_id="agi-spawn-3",
        agent_id="claude_code",
        agent_session_id="sess-3",
    )
    backfill_registration(
        state,
        agent_instance_id="agi-spawn-3",
        agent_id="claude_code",
        agent_session_id="sess-3-b",
    )
    row = read_managed_session(state, "agi-spawn-3")
    _check(
        row["lifecycle_state"] == LIFECYCLE_LIVE,
        "a reconnect backfill on an already-'live' row does not re-fire the edge",
    )
    _check(
        row["agent_session_id"] == "sess-3-b",
        "a reconnect backfill still re-writes agent_session_id (self-correcting, "
        "mirrors the state-table self-refresh pattern)",
    )
    _check(
        _transition_count(state, "agi-spawn-3") == 1,
        "a reconnect backfill inserts NO additional audit row",
    )


def test_backfill_with_no_managed_session_row_is_noop() -> None:
    state = _state()
    no_row_raised = False
    try:
        backfill_registration(
            state,
            agent_instance_id="agi-no-such-session",
            agent_id="claude_code",
            agent_session_id="sess-none",
        )
    except Exception:  # noqa: BLE001 — this smoke asserts NO exception of any kind
        no_row_raised = True
    _check(
        not no_row_raised,
        "backfill_registration on an agent_instance_id with no managed_session "
        "row returns cleanly so the registration route can decide whether to birth inventory",
    )


def test_hand_launched_registration_creates_honest_operator_row() -> None:
    """CATCHES: preserving the old no-row no-op in the actual registration seam.

    This faithfully simulates a hand-launched bridge registering without any
    prior spawn lineage, then reads the row back through the store API. A
    fixture row inserted before the assertion would not exercise C1.
    """
    state = _state()
    _managed_session_registration_backfill(
        state,
        agent_instance_id="agi-hand-launched",
        agent_id="codex",
        agent_session_id="ases-hand-launched",
    )
    row = read_managed_session(state, "agi-hand-launched")
    _check(
        row["host"] == "operator" and row["lifecycle_state"] == LIFECYCLE_LIVE,
        "a hand-launched registration creates and activates a real operator row",
    )
    _check(
        row["agent_id"] == "codex" and row["agent_session_id"] == "ases-hand-launched",
        "the registration-created row reads back the registering identity",
    )
    _check(
        row.get("report_by") is None
        and row.get("expires_at") is None
        and row["report_by_seconds"] == 0,
        "the inventory row carries no fabricated deadline or TTL contract",
    )
    _managed_session_registration_backfill(
        state,
        agent_instance_id="agi-hand-launched",
        agent_id="codex-reconnected",
        agent_session_id="ases-hand-launched",
    )
    reconnected = read_managed_session(state, "agi-hand-launched")
    _check(
        reconnected["agent_id"] == "codex-reconnected"
        and _transition_count(state, "agi-hand-launched") == 1,
        "a reconnect updates identity without creating a second row or transition",
    )


def test_backfill_registration_recovers_spawn_id_from_agent_session_id() -> None:
    """spawn/registration-gaps fix (2026-08-08, coordinator-seat ruling): a watch-
    hosted worker registers under a DIFFERENT agent_instance_id than the one
    its managed_session row was created under (``_resolve_watch_identity``
    deliberately mints ``agi-watch-<digest>``, never the spawn id). The
    primary lookup misses; the fallback must recover the spawn id from
    agent_session_id's guaranteed-by-construction ``ases-<agent_instance_id>``
    shape and find the row via THAT — a lifecycle claim, so this asserts the
    row actually advances (columns populate, transitions to live), not
    merely that the call returns without raising."""
    state = _state()
    insert_managed_session(
        state,
        ManagedSessionSpec(
            agent_instance_id="agi-spawn-recover",
            lane_id="lane-z",
            brief_ref="",
            work_class="read_only",
            budget_line="budget-3",
            host="tmux",
        ),
    )
    backfill_registration(
        state,
        agent_instance_id="agi-watch-deadbeef",  # the watch-minted id -- NOT the spawn id
        agent_id="claude_code",
        agent_session_id="ases-agi-spawn-recover",  # embeds the ORIGINAL spawn id
    )
    row = read_managed_session(state, "agi-spawn-recover")
    _check(
        row["agent_session_id"] == "ases-agi-spawn-recover" and row["agent_id"] == "claude_code",
        "the recovered-id fallback backfills agent_session_id/agent_id onto the SPAWN-time row",
    )
    _check(
        row["lifecycle_state"] == LIFECYCLE_LIVE,
        "the recovered-id fallback fires spawning->live exactly like a direct-id match would",
    )
    _check(
        _transition_count(state, "agi-spawn-recover") == 1,
        "the recovered-id fallback's spawning->live edge is audited exactly once",
    )


def test_backfill_registration_fallback_refuses_when_recovered_row_not_spawning() -> None:
    """Guard 2/3 (coordinator-seat ruling): the fallback may ONLY match a row still
    in 'spawning' -- a row that already completed its lineage must never be
    re-keyed by a later registration arriving under a different id. Red-
    first shape: first backfill the row normally (it leaves 'spawning'),
    THEN attempt a second registration whose agent_session_id recovers the
    SAME now-non-spawning row under a different agent_instance_id -- must
    refuse (no re-backfill via this path, no re-transition, no new audit
    row), never silently re-key it."""
    state = _state()
    insert_managed_session(
        state,
        ManagedSessionSpec(
            agent_instance_id="agi-spawn-completed",
            lane_id="lane-z",
            brief_ref="",
            work_class="read_only",
            budget_line="budget-3",
            host="tmux",
        ),
    )
    backfill_registration(
        state,
        agent_instance_id="agi-spawn-completed",
        agent_id="claude_code",
        agent_session_id="ases-agi-spawn-completed",
    )
    row_before = read_managed_session(state, "agi-spawn-completed")
    _check(
        row_before["lifecycle_state"] == LIFECYCLE_LIVE,
        "setup: the row is live before the refusal case",
    )

    backfill_registration(
        state,
        agent_instance_id="agi-watch-imposter",
        agent_id="imposter_agent",  # DISTINCT from the setup call's "claude_code" -- if the guard
        # fails to block the backfill, this value would silently overwrite the column and a same-
        # value assertion (re-checking agent_session_id, unchanged by construction here since it's
        # deterministically recovered from the same instance id either way) would never catch it.
        agent_session_id="ases-agi-spawn-completed",  # recovers the now-non-spawning row
    )
    row_after = read_managed_session(state, "agi-spawn-completed")
    _check(
        row_after["agent_id"] == "claude_code",
        "the row's agent_id is unchanged by the refused fallback attempt (a distinguishable "
        "value proves the guard actually blocked the write, not merely that it wrote the same value twice)",
    )
    _check(
        row_after["agent_session_id"] == "ases-agi-spawn-completed",
        "the row's agent_session_id is unchanged by the refused fallback attempt",
    )
    _check(
        row_after["lifecycle_state"] == LIFECYCLE_LIVE,
        "a non-spawning row is left completely untouched by a mismatched later registration",
    )
    _check(
        _transition_count(state, "agi-spawn-completed") == 1,
        "the refused fallback attempt inserts NO additional audit row",
    )


def test_backfill_registration_fallback_noop_when_recovered_id_also_has_no_row() -> None:
    """Guard 4 preserved: recovery failing to resolve ANY row (not just the
    primary miss) is the same silent no-op as today -- never an error, never
    a fabricated row."""
    state = _state()
    no_row_raised = False
    try:
        backfill_registration(
            state,
            agent_instance_id="agi-watch-orphan",
            agent_id="claude_code",
            agent_session_id="ases-agi-never-spawned",
        )
    except Exception:  # noqa: BLE001 -- this smoke asserts NO exception of any kind
        no_row_raised = True
    _check(
        not no_row_raised,
        "a recovered id that also has no managed_session row is a silent no-op",
    )


def test_backfill_registration_fallback_noop_when_session_id_has_no_spawn_shape() -> None:
    """Guard 4, the other edge: an agent_session_id that doesn't even have
    the ases-<id> shape (e.g. an operator-launched session's own
    ases-<epoch>-<pid>-<random> id) must never be treated as recoverable --
    silent no-op, matching the documented genuine-no-spawn-lineage path."""
    state = _state()
    no_row_raised = False
    try:
        backfill_registration(
            state,
            agent_instance_id="agi-watch-something",
            agent_id="claude_code",
            agent_session_id="ases-1786034326-39546-11549",  # operator-shaped, not spawn-shaped
        )
    except Exception:  # noqa: BLE001
        no_row_raised = True
    _check(
        not no_row_raised,
        "a non-spawn-shaped agent_session_id is never treated as recoverable -- silent no-op",
    )


def test_set_host_ref() -> None:
    state = _state()
    insert_managed_session(
        state,
        ManagedSessionSpec(
            agent_instance_id="agi-spawn-3",
            lane_id="lane-z",
            brief_ref="",
            work_class="read_only",
            budget_line="budget-3",
            host="headless",
        ),
    )
    set_host_ref(state, agent_instance_id="agi-spawn-3", host_ref="driver-pid-4242")
    _check(
        read_managed_session(state, "agi-spawn-3")["host_ref"] == "driver-pid-4242",
        "set_host_ref persists the adapter's host_ref onto the ledger row",
    )


def test_written_columns_are_declared() -> None:
    """Every column the store writes must exist in the declared TableSchema.

    The battery's fakes accept any record shape, so a write-path column that
    was never declared passes every lifecycle smoke and then fails at the
    real provider with Postgres's undefined-column error (measured live
    2026-08-28: ``provisioning_mode``). This check closes that seam: the
    insert payload — including the conditional report_by/expires_at keys —
    is compared against the schema declaration itself.
    """
    from agent_messaging_plugin.schema import get_managed_session_schema

    state = _state()
    record = insert_managed_session(
        state,
        ManagedSessionSpec(
            agent_instance_id="agi-schema-parity",
            lane_id="lane-schema-parity",
            brief_ref="workbench/brief.md",
            work_class="analysis_deliverable",
            budget_line="budget-parity",
            host="tmux",
            spawned_by_instance_id="agi-spawner",
            spawned_by_role="Claude-C",
            directed_by="operator:none",
            report_by_seconds=900,
            ttl_seconds=3600,
        ),
    )
    declared = set(get_managed_session_schema().columns)
    undeclared = sorted(set(record) - declared)
    _check(
        not undeclared,
        f"every written managed_session column is declared in the schema "
        f"(undeclared: {undeclared})",
    )


def main() -> int:
    test_insert_and_read()
    test_written_columns_are_declared()
    test_list_filters()
    test_transition_guards()
    test_backfill_registration_fires_once()
    test_backfill_reconnect_does_not_refire()
    test_backfill_with_no_managed_session_row_is_noop()
    test_hand_launched_registration_creates_honest_operator_row()
    test_backfill_registration_recovers_spawn_id_from_agent_session_id()
    test_backfill_registration_fallback_refuses_when_recovered_row_not_spawning()
    test_backfill_registration_fallback_noop_when_recovered_id_also_has_no_row()
    test_backfill_registration_fallback_noop_when_session_id_has_no_spawn_shape()
    test_set_host_ref()

    print()
    print(f"PASSED: {_passed}")
    print(f"FAILED: {len(_failed)}")
    for label in _failed:
        print(f"  - {label}")
    return 1 if _failed else 0


if __name__ == "__main__":
    sys.exit(main())
