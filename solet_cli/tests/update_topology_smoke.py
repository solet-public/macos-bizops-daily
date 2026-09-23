"""Closed-reason matrix for the Step-4 topology reducer, over the Step-7 local-state frontier (section 6.2)."""

from __future__ import annotations

import sys
from dataclasses import replace
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(_ROOT / "solet_cli" / "src"), str(_ROOT / "solet_setup_contracts" / "src"), str(_ROOT / "solet_cli" / "tests")]
from existing_install_inspection_contract_smoke import _classification_witnesses  # noqa: E402
from solet_manager.existing_install_inspection import (  # noqa: E402
    ExistingInstallClass,
    ExistingInstallFacts,
    ObservationAvailability,
    ObservedBoolean,
    ObservedPathPairs,
    ObservedPaths,
    ObservedRawRows,
    RawRow,
    WorkingTreeCondition,
)
from solet_manager.update_topology import UpdateTopology, analyze_update_topology  # noqa: E402


def _observed(*values: str) -> ObservedPaths:
    return ObservedPaths(ObservationAvailability.OBSERVED, values)


def _row(path: str, *, status: str = "M", old_mode: str = "100644", new_mode: str = "100644") -> RawRow:
    return RawRow(old_mode, new_mode, "0" * 40, "1" * 40, status, path)


def _facts(**changes: object) -> ExistingInstallFacts:
    base = _classification_witnesses()[ExistingInstallClass.CLEAN_FAST_FORWARD_SEED_CLONE]
    return replace(base, **changes)  # type: ignore[arg-type]


_BASELINE, _CANDIDATE = "a" * 40, "c" * 40


def _topology(facts: ExistingInstallFacts, *, candidate: str = _CANDIDATE) -> UpdateTopology:
    return analyze_update_topology(facts, baseline_commit=_BASELINE, candidate_commit=candidate)


def _assert_real_clone_actionable() -> None:
    """The real-clone shape (unstaged content-only tracked edits plus untracked paths) is actionable on facts alone."""
    assert _topology(_facts()).actionable
    real = _facts(working_tree=WorkingTreeCondition.MIXED_CHANGES, tracked_paths=_observed("NOTICE"), untracked_paths=_observed(".gitignore"), tracked_entries=ObservedRawRows(ObservationAvailability.OBSERVED, (_row("NOTICE"),)))
    reduced = _topology(real)
    assert reduced.actionable and "tracked_state_present" not in reduced.reasons, reduced


def _reason_cases() -> dict[str, dict[str, object]]:
    unknown = ObservedPaths(ObservationAvailability.UNKNOWN, ())
    return {
        "managed_identity_drift": {"head_commit": "d" * 40},
        "already_current": {},
        "local_state_unobserved": {"staged_paths": unknown},
        "staged_changes_present": {"staged_paths": _observed("NOTICE")},
        "tracked_shape_changed": {"tracked_entries": ObservedRawRows(ObservationAvailability.OBSERVED, (_row("NOTICE", new_mode="100755"),))},
        "git_metadata_present": {"untracked_paths": _observed(".gitattributes")},
        "detached_head": {"detached": ObservedBoolean.TRUE},
        "shallow_history": {"shallow": ObservedBoolean.TRUE},
        "submodule_present": {"submodules": _observed("x")},
        "linked_worktree_present": {"linked_worktrees": _observed("x")},
        "repository_operation_present": {"repository_operations": _observed("x")},
        "untracked_destination_collision": {"untracked_destination_collisions": _observed("x")},
        "casefold_collision": {"casefold_collisions": ObservedPathPairs(ObservationAvailability.OBSERVED, (("a", "A"),))},
    }


def _assert_each_reason() -> None:
    for reason, changes in _reason_cases().items():
        candidate = _BASELINE if reason == "already_current" else _CANDIDATE
        topology = _topology(_facts(**changes), candidate=candidate)
        assert reason in topology.reasons, (reason, topology.reasons)
        assert reason == "already_current" or not topology.actionable


def _assert_shape_and_metadata_variants() -> None:
    """A deletion, a type change and a chmod all read as tracked_shape_changed; a tracked root .gitignore is git metadata."""
    for row in (_row("NOTICE", status="D", new_mode="000000"), _row("NOTICE", status="T", new_mode="120000"), _row("NOTICE", new_mode="100755")):
        assert "tracked_shape_changed" in _topology(_facts(tracked_entries=ObservedRawRows(ObservationAvailability.OBSERVED, (row,)))).reasons
    assert "git_metadata_present" in _topology(_facts(tracked_paths=_observed(".gitignore"))).reasons
    assert "git_metadata_present" not in _topology(_facts(untracked_paths=_observed(".gitignore"))).reasons


def main() -> int:
    _assert_real_clone_actionable()
    _assert_each_reason()
    _assert_shape_and_metadata_variants()
    print("update_topology_smoke OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
