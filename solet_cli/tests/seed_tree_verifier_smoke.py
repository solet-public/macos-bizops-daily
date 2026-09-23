"""Read-only seed-tree baseline verifier regression coverage."""

from __future__ import annotations

import sys
from collections.abc import Sequence
from pathlib import Path
from typing import cast

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from solet_manager.doctor import _doctor_result  # pyright: ignore[reportPrivateUsage]  # noqa: E402
from solet_manager.models import CheckpointStatus, JsonValue  # noqa: E402
from solet_manager.release_lock import SeedLock  # noqa: E402
from solet_manager.seed_tree_verifier import (  # noqa: E402
    GitQueryResult,
    SeedTreeDeviation,
    SeedTreeVerification,
    verify_seed_tree,
)
from solet_manager.transaction import Transaction, canonical_sha256  # noqa: E402

_checks = 0
_EXPECTED = "a" * 40


def _check(condition: bool, message: str) -> None:
    global _checks
    _checks += 1
    if not condition:
        raise AssertionError(f"red: {message}")


class FakeGit:
    def __init__(
        self,
        *,
        head_tree: str = _EXPECTED,
        worktree: str = "",
        index: str = "",
        failure: str | None = None,
    ) -> None:
        self.head_tree = head_tree
        self.worktree = worktree
        self.index = index
        self.failure = failure
        self.commands: list[tuple[str, ...]] = []

    def __call__(self, command: Sequence[str], _timeout: int) -> GitQueryResult:
        current = tuple(command)
        self.commands.append(current)
        if self.failure is not None:
            return GitQueryResult(128, "", self.failure)
        if current[-2:] == ("rev-parse", "HEAD^{tree}"):
            return GitQueryResult(0, f"{self.head_tree}\n", "")
        if current[-3:] == ("diff", "--name-status", "-z"):
            return GitQueryResult(0, self.worktree, "")
        if current[-4:] == ("diff", "--cached", "--name-status", "-z"):
            return GitQueryResult(0, self.index, "")
        raise AssertionError(f"red: unexpected Git query: {current!r}")


def _assert_clean_baseline_is_verified() -> None:
    git = FakeGit()
    result = verify_seed_tree(Path("/fixture/target"), _EXPECTED, runner=git)
    data = result.to_dict()
    _check(result.status == "verified", "clean sealed tree did not verify")
    _check(data["format"] == "seed-tree-baseline-v1", "baseline seam format changed")
    _check(data["baseline_matches"] is True, "clean baseline is not consumable by overlay")
    _check(data["deviations"] == [], "clean baseline reported a deviation")
    _check(
        all(command[0] == "git" and "--no-optional-locks" in command for command in git.commands),
        "verifier did not use read-only Git vectors",
    )
    _check(
        all(command[-3] not in {"add", "commit", "checkout", "reset"} for command in git.commands),
        "verifier issued a Git mutation command",
    )


def _assert_tracked_drift_is_named_but_warn_only() -> None:
    git = FakeGit(worktree="M\0plugins/example.py\0", index="D\0legacy.py\0")
    result = verify_seed_tree(Path("/fixture/target"), _EXPECTED, runner=git)
    data = result.to_dict()
    _check(result.status == "warn", "tracked drift was not warn-level")
    _check(data["reason"] == "tracked_tree_deviation", "drift reason is not precise")
    deviations = data["deviations"]
    _check(isinstance(deviations, list) and len(deviations) == 2, "drift entries missing")
    _check(
        deviations == [
            {"location": "worktree", "status": "M", "paths": ["plugins/example.py"]},
            {"location": "index", "status": "D", "paths": ["legacy.py"]},
        ],
        "drift output did not name each tracked deviation",
    )


def _assert_baseline_hash_mismatch_is_named() -> None:
    result = verify_seed_tree(
        Path("/fixture/target"),
        _EXPECTED,
        runner=FakeGit(head_tree="b" * 40),
    )
    data = result.to_dict()
    _check(result.status == "warn", "baseline hash mismatch was not warn-level")
    _check(data["reason"] == "seed_tree_hash_mismatch", "hash mismatch reason is imprecise")
    _check(data["observed_head_tree_hash"] == "b" * 40, "observed tree hash was dropped")


def _assert_unverifiable_tree_is_named() -> None:
    result = verify_seed_tree(
        Path("/fixture/target"),
        _EXPECTED,
        runner=FakeGit(failure="not a git repository"),
    )
    data = result.to_dict()
    _check(result.status == "warn", "unverifiable tree was not warn-level")
    _check(data["reason"] == "seed_tree_unverifiable", "query failure reason is imprecise")
    _check("not a git repository" in str(data["query_error"]), "query failure lost its cause")


def _assert_doctor_surfaces_warning_without_changing_its_verdict() -> None:
    seed = SeedLock("owner/repo", "v1", "a" * 40, _EXPECTED, None, "fixture")
    transaction = Transaction.create(
        name="fixture",
        target=Path("/fixture/target"),
        input_fingerprint=canonical_sha256({"name": "fixture"}),
        answers={"decisions": {}, "consents": {}},
        seed=seed,
        flow_id="fixture-flow",
        flow_source_revision="a" * 40,
        flow_contract_digest="sha256:" + "b" * 64,
        stage_ids=("fixture-stage",),
        completion_probe_ids=("fixture-check",),
    ).with_statuses(
        stages={"fixture-stage": CheckpointStatus.VERIFIED},
        completion={"fixture-check": CheckpointStatus.VERIFIED},
    )
    warning = SeedTreeVerification(
        expected_tree_hash=_EXPECTED,
        observed_head_tree_hash=_EXPECTED,
        deviations=(SeedTreeDeviation("worktree", "M", ("contracts.json",)),),
    )
    checks: list[JsonValue] = [
        {
            "id": "fixture-check",
            "status": "verified",
            "declared_expectation_advisory": None,
            "repair": None,
        }
    ]
    result = _doctor_result("fixture", transaction, checks, warning)
    _check(result.exit_code == 0, "seed-tree warning changed doctor acceptance exit")
    surfaced = result.data.get("seed_tree_verification")
    if not isinstance(surfaced, dict):
        raise AssertionError("red: doctor omitted the seed-tree probe")
    probe = cast(dict[str, JsonValue], surfaced)
    _check(probe.get("status") == "warn", "doctor lost the warning-level status")
    _check(probe.get("reason") == "tracked_tree_deviation", "doctor lost the deviation reason")


def main() -> int:
    _assert_clean_baseline_is_verified()
    _assert_tracked_drift_is_named_but_warn_only()
    _assert_baseline_hash_mismatch_is_named()
    _assert_unverifiable_tree_is_named()
    _assert_doctor_surfaces_warning_without_changing_its_verdict()
    print(f"seed_tree_verifier_smoke OK: {_checks} checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
