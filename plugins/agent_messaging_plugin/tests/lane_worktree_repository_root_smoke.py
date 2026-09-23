#!/usr/bin/env python3
"""Regression proof for cross-repository lane worktree lifecycle roots.

A dispatched repository has a durable identity but no machine-local checkout
path in the project register.  This proof supplies that checkout explicitly,
then verifies the same resolved root is persisted and used at retirement.
"""

from __future__ import annotations

import os
import sys
import tempfile
from dataclasses import replace
from pathlib import Path
from typing import cast

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT / "ananta" / "src"))
sys.path.insert(0, str(REPO_ROOT / "plugins" / "agent_messaging_plugin" / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _real_state_fake import RealShapeState  # noqa: E402
from ananta.interfaces.state_management_interface import StateManagementInterface  # noqa: E402

import agent_messaging_plugin.session_lifecycle_verbs as lifecycle_verbs  # noqa: E402
from agent_messaging_plugin.lane_worktrees import (  # noqa: E402
    ActiveLaneWorktreeInventory,
    LaneWorktree,
    LaneWorktreeSweep,
)
from agent_messaging_plugin.session_lifecycle_store import (  # noqa: E402
    ManagedSessionSpec,
    insert_managed_session,
    read_managed_session,
)
from agent_messaging_plugin.session_lifecycle_verbs import (  # noqa: E402
    SpawnSessionRequest,
    VerbError,
    spawn_session,
)


def _state() -> StateManagementInterface:
    return cast("StateManagementInterface", RealShapeState())


def _request(repository_root: Path) -> SpawnSessionRequest:
    return SpawnSessionRequest(
        role_class="ephemeral",
        lane_id="foreign-lane",
        brief_ref="workbench/foreign.md",
        work_class="analysis_deliverable",
        budget_line="foreign-root-proof",
        host="operator",
        directed_by="operator:test",
        dispatch_kind="infrastructure",
        repository_root=str(repository_root),
    )


def _assert_cross_repository_provision_and_retire() -> None:
    """A mutation dropping either root hand-off makes this exact proof fail."""
    state = _state()
    with tempfile.TemporaryDirectory() as raw:
        temporary_root = Path(raw)
        serving_root = temporary_root / "serving"
        serving_app_home = serving_root / "profile"
        foreign_root = temporary_root / "foreign"
        (serving_root / ".git").mkdir(parents=True)
        serving_app_home.mkdir()
        (foreign_root / ".git").mkdir(parents=True)
        serving_root = serving_root.resolve()
        serving_app_home = serving_app_home.resolve()
        foreign_root = foreign_root.resolve()
        prior_app_home = os.environ.get("APP_HOME")
        os.environ["APP_HOME"] = str(serving_app_home)

        original_inventory = lifecycle_verbs._active_lane_worktree_inventory  # noqa: SLF001
        original_sweep = lifecycle_verbs.sweep_orphaned_lane_worktrees
        original_for = lifecycle_verbs.lane_worktree_for
        original_provision = lifecycle_verbs.provision_lane_worktree
        provisioned_roots: list[Path] = []
        retired_roots: list[Path] = []
        worktree = LaneWorktree(
            repo_root=foreign_root,
            root=foreign_root / "_lane_worktrees",
            path=foreign_root / "_lane_worktrees" / "foreign-lane--agi-foreign",
            branch="lane/foreign-lane/agi-foreign",
        )

        def fake_worktree_for(
            repo_root: Path, *, role_name: str, agent_instance_id: str
        ) -> LaneWorktree:
            del role_name, agent_instance_id
            provisioned_roots.append(repo_root)
            return worktree

        try:
            lifecycle_verbs._active_lane_worktree_inventory = (  # type: ignore[assignment]  # noqa: SLF001
                lambda _state, _root: ActiveLaneWorktreeInventory(
                    paths=(), terminal_paths=(), cleanup_safe=True, incomplete_rows=(),
                )
            )
            lifecycle_verbs.sweep_orphaned_lane_worktrees = (  # type: ignore[assignment]
                lambda *_args, **_kwargs: LaneWorktreeSweep(removed=(), skipped=())
            )
            lifecycle_verbs.lane_worktree_for = fake_worktree_for  # type: ignore[assignment]
            lifecycle_verbs.provision_lane_worktree = lambda _worktree: None  # type: ignore[assignment]

            provisioned = lifecycle_verbs._provision_worktree_for_request(  # noqa: SLF001
                state, _request(foreign_root), "foreign-lane", "agi-foreign"
            )
            assert provisioned == worktree
            assert provisioned_roots == [foreign_root], provisioned_roots

            insert_managed_session(
                state,
                ManagedSessionSpec(
                    agent_instance_id="agi-foreign",
                    lane_id="foreign-lane",
                    brief_ref="workbench/foreign.md",
                    work_class="analysis_deliverable",
                    budget_line="foreign-root-proof",
                    host="operator",
                    lane_repo_root=str(provisioned.repo_root),
                ),
            )
            row = read_managed_session(state, "agi-foreign")
            assert row["lane_repo_root"] == str(foreign_root), row

            def fake_retiring_worktree_for(
                repo_root: Path, *, role_name: str, agent_instance_id: str
            ) -> LaneWorktree:
                del role_name, agent_instance_id
                retired_roots.append(repo_root)
                return worktree

            lifecycle_verbs.lane_worktree_for = fake_retiring_worktree_for  # type: ignore[assignment]
            assert lifecycle_verbs._retiring_lane_worktree(row) == worktree  # noqa: SLF001
            assert retired_roots == [foreign_root], retired_roots

            # Existing rows retain APP_HOME-derived behavior.
            assert lifecycle_verbs._resolve_lane_repo_root() == serving_root  # noqa: SLF001
        finally:
            lifecycle_verbs._active_lane_worktree_inventory = original_inventory  # type: ignore[assignment]  # noqa: SLF001
            lifecycle_verbs.sweep_orphaned_lane_worktrees = original_sweep  # type: ignore[assignment]
            lifecycle_verbs.lane_worktree_for = original_for  # type: ignore[assignment]
            lifecycle_verbs.provision_lane_worktree = original_provision  # type: ignore[assignment]
            if prior_app_home is None:
                os.environ.pop("APP_HOME", None)
            else:
                os.environ["APP_HOME"] = prior_app_home


def _assert_spawn_session_wires_resolved_root() -> None:
    """Exercise the actual spawn-to-ledger wire, including a killed assignment mutant."""
    state = _state()
    with tempfile.TemporaryDirectory() as raw:
        foreign_root = (Path(raw) / "foreign").resolve()
        (foreign_root / ".git").mkdir(parents=True)
        brief = foreign_root / "workbench" / "foreign.md"
        brief.parent.mkdir()
        brief.write_text("foreign source brief\n", encoding="utf-8")
        original_provision = lifecycle_verbs._provision_spawn_worktree  # noqa: SLF001
        original_host_spawn = lifecycle_verbs._spawn_host_in_lane_worktree  # noqa: SLF001
        original_first_turn = lifecycle_verbs._dispatch_first_turn  # noqa: SLF001
        original_root_for_spec = lifecycle_verbs._provisioned_lane_repo_root  # noqa: SLF001

        def fake_provision(
            _state: StateManagementInterface,
            *,
            role_name: str,
            agent_instance_id: str,
            repository_root: str,
        ) -> LaneWorktree:
            assert repository_root == str(foreign_root), repository_root
            return LaneWorktree(
                repo_root=foreign_root,
                root=foreign_root / "_lane_worktrees",
                path=foreign_root / "_lane_worktrees" / f"{role_name}--{agent_instance_id}",
                branch=f"lane/{role_name}/{agent_instance_id}",
            )

        try:
            lifecycle_verbs._provision_spawn_worktree = fake_provision  # type: ignore[assignment]  # noqa: SLF001
            lifecycle_verbs._spawn_host_in_lane_worktree = (  # type: ignore[assignment]  # noqa: SLF001
                lambda *_args: "fixture-host"
            )
            lifecycle_verbs._dispatch_first_turn = (  # type: ignore[assignment]  # noqa: SLF001
                lambda *_args, **_kwargs: ("fallback", True, "")
            )
            spawned = spawn_session(state, _request(foreign_root))
            row = read_managed_session(state, str(spawned["agent_instance_id"]))
            assert row["lane_repo_root"] == str(foreign_root), row

            lifecycle_verbs._provisioned_lane_repo_root = lambda _worktree: ""  # type: ignore[assignment]  # noqa: SLF001
            mutant_request = replace(_request(foreign_root), lane_id="foreign-mutant")
            mutant = spawn_session(state, mutant_request)
            mutant_row = read_managed_session(state, str(mutant["agent_instance_id"]))
            try:
                assert mutant_row["lane_repo_root"] == str(foreign_root), mutant_row
            except AssertionError:
                pass
            else:
                raise AssertionError("dropped lane_repo_root wiring mutant survived")
        finally:
            lifecycle_verbs._provision_spawn_worktree = original_provision  # type: ignore[assignment]  # noqa: SLF001
            lifecycle_verbs._spawn_host_in_lane_worktree = original_host_spawn  # type: ignore[assignment]  # noqa: SLF001
            lifecycle_verbs._dispatch_first_turn = original_first_turn  # type: ignore[assignment]  # noqa: SLF001
            lifecycle_verbs._provisioned_lane_repo_root = original_root_for_spec  # type: ignore[assignment]  # noqa: SLF001


def _assert_invalid_override_fails_loudly() -> None:
    with tempfile.TemporaryDirectory() as raw:
        missing = Path(raw) / "missing-checkout"
        try:
            lifecycle_verbs._resolve_lane_repo_root(str(missing))  # noqa: SLF001
        except VerbError as error:
            assert error.code == "lane_worktree_repo_missing", error
        else:
            raise AssertionError("a missing explicit repository root must fail loudly")
    try:
        lifecycle_verbs._resolve_lane_repo_root("relative-checkout")  # noqa: SLF001
    except VerbError as error:
        assert error.code == "lane_worktree_repo_override_invalid", error
    else:
        raise AssertionError("a relative explicit repository root must fail loudly")


def main() -> int:
    _assert_cross_repository_provision_and_retire()
    _assert_spawn_session_wires_resolved_root()
    _assert_invalid_override_fails_loudly()
    print("lane_worktree_repository_root_smoke OK: 10 checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
