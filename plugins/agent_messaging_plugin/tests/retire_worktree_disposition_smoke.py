#!/usr/bin/env python3
"""D3 retirement ordering and worktree-disposition regression proof.

The fixture is a real-shaped in-memory StateManagementInterface.  It replaces
only the worktree and host seams, so no test command can touch a real lane
worktree or host process.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any, cast

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT / "ananta" / "src"))
sys.path.insert(0, str(REPO_ROOT / "plugins" / "agent_messaging_plugin" / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _real_state_fake import RealShapeState  # noqa: E402
from ananta.interfaces.state_management_interface import StateManagementInterface  # noqa: E402
from ananta.llm.agent_messaging.role_binding import AGENT_ROLE_BINDING_NAMESPACE  # noqa: E402

import agent_messaging_plugin.session_lifecycle_verbs as lifecycle_verbs  # noqa: E402
from agent_messaging_plugin.lane_worktrees import (  # noqa: E402
    LaneWorktree,
    LaneWorktreeDisposability,
    LaneWorktreeError,
)
from agent_messaging_plugin.schema import LIFECYCLE_LIVE, LIFECYCLE_RETIRED  # noqa: E402
from agent_messaging_plugin.session_lifecycle_store import (  # noqa: E402
    ManagedSessionSpec,
    insert_managed_session,
    read_managed_session,
    transition_lifecycle_state,
)
from agent_messaging_plugin.session_lifecycle_verbs import (  # noqa: E402
    VerbError,
    retire_session,
    session_status,
)


def _state() -> StateManagementInterface:
    return cast("StateManagementInterface", RealShapeState())


def _insert_live(state: StateManagementInterface, agent_instance_id: str) -> None:
    insert_managed_session(
        state,
        ManagedSessionSpec(
            agent_instance_id=agent_instance_id,
            lane_id="lane-d3",
            brief_ref="",
            work_class="production_mutation",
            budget_line="d3",
            host="operator",
            role_name="lane-d3",
        ),
    )
    transition_lifecycle_state(
        state,
        agent_instance_id=agent_instance_id,
        from_state="spawning",
        to_state=LIFECYCLE_LIVE,
        directed_by="operator:none",
    )


def _assert_refusal_precedes_termination() -> None:
    """Red mutation: move adjudication after terminate_session; then it calls the seam."""
    state = _state()
    _insert_live(state, "agi-d3-refusal")
    original_adjudicate = lifecycle_verbs._adjudicate_retire_lane_worktree  # noqa: SLF001
    original_terminate = lifecycle_verbs.terminate_session
    termination_calls: list[str] = []

    def refuse(_: object) -> None:
        raise VerbError("lane_worktree_not_disposable", "fixture names unlanded bytes")

    def must_not_terminate(*_: Any, **__: Any) -> dict[str, object]:
        termination_calls.append("called")
        return {"session_terminal_edges_fired": 0}

    lifecycle_verbs._adjudicate_retire_lane_worktree = refuse  # type: ignore[assignment]  # noqa: SLF001
    lifecycle_verbs.terminate_session = must_not_terminate  # type: ignore[assignment]
    try:
        try:
            retire_session(state, agent_instance_id="agi-d3-refusal", directed_by="operator:none")
        except VerbError as exc:
            assert exc.code == "lane_worktree_not_disposable", exc
        else:
            raise AssertionError("a non-disposable worktree must refuse retirement")
    finally:
        lifecycle_verbs._adjudicate_retire_lane_worktree = original_adjudicate  # type: ignore[assignment]  # noqa: SLF001
        lifecycle_verbs.terminate_session = original_terminate  # type: ignore[assignment]

    assert termination_calls == [], termination_calls
    assert read_managed_session(state, "agi-d3-refusal")["lifecycle_state"] == LIFECYCLE_LIVE


def _assert_teardown_error_records_terminal_outcome() -> None:
    """Red mutation: propagate remove_lane_worktree's error; retirement then wedges."""
    state = _state()
    _insert_live(state, "agi-d3-error")
    original_root = lifecycle_verbs._resolve_lane_repo_root  # noqa: SLF001
    original_for = lifecycle_verbs.lane_worktree_for
    original_disposability = lifecycle_verbs.lane_worktree_disposability
    original_remove = lifecycle_verbs.remove_lane_worktree
    worktree = LaneWorktree(
        repo_root=Path("/fixture-repo"),
        root=Path("/fixture-root"),
        path=Path("/fixture-root/lane-d3"),
        branch="lane/lane-d3/agi-d3-error",
    )

    def disposable(_: LaneWorktree) -> LaneWorktreeDisposability:
        return LaneWorktreeDisposability(True, (), "fixture disposable", 0)

    def teardown_error(_: LaneWorktree) -> None:
        raise LaneWorktreeError("fixture removal error")

    lifecycle_verbs._resolve_lane_repo_root = lambda _root="": worktree.repo_root  # type: ignore[assignment]  # noqa: SLF001
    lifecycle_verbs.lane_worktree_for = lambda *_args, **_kwargs: worktree  # type: ignore[assignment]
    lifecycle_verbs.lane_worktree_disposability = disposable  # type: ignore[assignment]
    lifecycle_verbs.remove_lane_worktree = teardown_error  # type: ignore[assignment]
    try:
        result = retire_session(state, agent_instance_id="agi-d3-error", directed_by="operator:none")
    finally:
        lifecycle_verbs._resolve_lane_repo_root = original_root  # type: ignore[assignment]  # noqa: SLF001
        lifecycle_verbs.lane_worktree_for = original_for  # type: ignore[assignment]
        lifecycle_verbs.lane_worktree_disposability = original_disposability  # type: ignore[assignment]
        lifecycle_verbs.remove_lane_worktree = original_remove  # type: ignore[assignment]

    assert result == {"already_retired": False, "dependencies_fired": 0}, result
    row = read_managed_session(state, "agi-d3-error")
    assert row["lifecycle_state"] == LIFECYCLE_RETIRED, row
    assert row["worktree_disposition"] == "retained_error", row
    # session_status is an intentional reader, so the ledger field is visible
    # to an operator instead of being an inert write-only status.
    assert session_status(state, "agi-d3-error")["worktree_disposition"] == "retained_error"
    replay = retire_session(state, agent_instance_id="agi-d3-error", directed_by="operator:none")
    assert replay == {"already_retired": True, "dependencies_fired": 0}, replay


def _assert_unprovisioned_row_never_resolves_a_repo_root() -> None:
    """Red mutation: resolve the repo root before the no-worktree gate; APP_HOME then errors."""
    state = _state()
    _insert_live(state, "agi-d3-unprovisioned")
    state.update_state(
        AGENT_ROLE_BINDING_NAMESPACE,
        {"table": "managed_session", "filters": {"agent_instance_id": "agi-d3-unprovisioned"}},
        {"role_name": "", "local_name": ""},
    )
    prior_app_home = os.environ.pop("APP_HOME", None)
    try:
        result = retire_session(
            state, agent_instance_id="agi-d3-unprovisioned", directed_by="operator:none",
        )
    finally:
        if prior_app_home is not None:
            os.environ["APP_HOME"] = prior_app_home

    assert result == {"already_retired": False, "dependencies_fired": 0}, result
    row = read_managed_session(state, "agi-d3-unprovisioned")
    assert row["lifecycle_state"] == LIFECYCLE_RETIRED, row
    assert row["worktree_disposition"] == "not_provisioned", row
    assert (
        session_status(state, "agi-d3-unprovisioned")["worktree_disposition"]
        == "not_provisioned"
    )


def main() -> int:
    _assert_refusal_precedes_termination()
    _assert_teardown_error_records_terminal_outcome()
    _assert_unprovisioned_row_never_resolves_a_repo_root()
    print("retire_worktree_disposition_smoke OK: 13 checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
