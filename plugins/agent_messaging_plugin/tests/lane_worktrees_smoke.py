"""Behavioral proof for per-lane worktree isolation and exact teardown."""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT / "plugins" / "agent_messaging_plugin" / "src"))

import agent_messaging_plugin.lane_worktrees as lane_worktrees  # noqa: E402
from agent_messaging_plugin.lane_worktrees import (  # noqa: E402
    DirtyStaleWorktreeSkippedWarning,
    LaneWorktree,
    LaneWorktreeError,
    lane_worktree_disposability,
    lane_worktree_for,
    provision_lane_worktree,
    remove_lane_worktree,
    sweep_orphaned_lane_worktrees,
    worktree_pythonpath,
)


def _environment(root: Path) -> dict[str, str]:
    """A Git environment that cannot redirect the fixture into a real checkout."""
    return {
        key: value
        for key, value in os.environ.items()
        if not key.startswith("GIT_")
    } | {
        "HOME": str(root / "home"),
        "XDG_CONFIG_HOME": str(root / "xdg-config"),
        "XDG_CACHE_HOME": str(root / "xdg-cache"),
        "GIT_CONFIG_NOSYSTEM": "1",
    }


def _run_git(root: Path, *arguments: str, environment: dict[str, str]) -> str:
    root = root.resolve()
    assert root.is_relative_to(root.parent), root
    result = subprocess.run(
        ("git", "-C", str(root), *arguments),
        check=False,
        capture_output=True,
        text=True,
        env=environment,
    )
    if result.returncode != 0:
        raise AssertionError(result.stderr or result.stdout)
    return result.stdout


def _fixture(root: Path, environment: dict[str, str]) -> Path:
    repo = root / "fixture-repo"
    repo.mkdir(parents=True)
    _run_git(repo, "init", "--initial-branch", "master", environment=environment)
    _run_git(repo, "config", "user.email", "fixture@example.invalid", environment=environment)
    _run_git(repo, "config", "user.name", "fixture", environment=environment)
    (repo / ".venv").mkdir()
    (repo / "tracked.txt").write_text("base\n", encoding="utf-8")
    _run_git(repo, "add", "tracked.txt", environment=environment)
    _run_git(repo, "commit", "-m", "fixture base", environment=environment)
    return repo


def _assert_shared_dirt_isolation(root: Path, environment: dict[str, str]) -> int:
    repo = _fixture(root, environment)
    lane = lane_worktree_for(
        repo, role_name="lane-isolation", agent_instance_id="agi-isolation",
    )
    provision_lane_worktree(lane, environment=environment)
    (repo / "shared-only.txt").write_text("shared dirt\n", encoding="utf-8")
    assert not (lane.path / "shared-only.txt").exists()
    (lane.path / "lane-only.txt").write_text("lane bytes\n", encoding="utf-8")
    _run_git(lane.path, "add", "lane-only.txt", environment=environment)
    _run_git(lane.path, "commit", "-m", "lane commit", environment=environment)
    committed = _run_git(lane.path, "show", "--format=", "--name-only", "HEAD", environment=environment)
    assert committed.splitlines() == ["lane-only.txt"], committed
    assert (lane.path / ".venv").is_symlink()
    assert worktree_pythonpath(lane.path, "inherited") == os.pathsep.join(
        (str(lane.path.resolve()), "inherited"),
    )
    remove_lane_worktree(lane, environment=environment)
    assert not lane.path.exists()
    _run_git(repo, "show-ref", "--verify", "--quiet", f"refs/heads/{lane.branch}", environment=environment)
    return 6


def _assert_red_pre_fix_shape(root: Path, environment: dict[str, str]) -> int:
    repo = _fixture(root, environment)
    lane = lane_worktree_for(repo, role_name="lane-red", agent_instance_id="agi-red")
    provision_lane_worktree(lane, environment=environment)
    (repo / "shared-only.txt").write_text("shared dirt\n", encoding="utf-8")
    failed = False
    try:
        assert (lane.path / "shared-only.txt").exists(), "red: shared dirt leaked into lane tree"
    except AssertionError:
        failed = True
    assert failed, "the red proof must fail when it expects shared dirt in the worktree"
    remove_lane_worktree(lane, environment=environment)
    return 2


def _assert_sweep_is_exact(root: Path, environment: dict[str, str]) -> int:
    repo = _fixture(root, environment)
    live = lane_worktree_for(repo, role_name="lane-live", agent_instance_id="agi-live")
    orphan = lane_worktree_for(repo, role_name="lane-orphan", agent_instance_id="agi-orphan")
    provision_lane_worktree(live, environment=environment)
    provision_lane_worktree(orphan, environment=environment)
    sweep = sweep_orphaned_lane_worktrees(
        repo, terminal_paths=(orphan.path,), environment=environment,
    )
    assert sweep.removed == (orphan.path.resolve(),), sweep
    assert sweep.skipped == (), sweep
    assert live.path.exists()
    _run_git(repo, "show-ref", "--verify", "--quiet", f"refs/heads/{orphan.branch}", environment=environment)
    remove_lane_worktree(live, environment=environment)
    return 5


def _assert_lock_refusal_preserves_shared_venv(root: Path, environment: dict[str, str]) -> int:
    """A lock refusal must retain the provisioned interpreter link.

    Red mutation: move ``_remove_shared_venv_link`` back before Git's removal
    request. Git then refuses the locked worktree while its `.venv` is already
    missing, breaking a still-live lane without removing it.
    """
    repo = _fixture(root, environment)
    lane = lane_worktree_for(repo, role_name="lane-locked", agent_instance_id="agi-locked")
    provision_lane_worktree(lane, environment=environment)
    link = lane.path / ".venv"
    _run_git(repo, "worktree", "lock", str(lane.path), environment=environment)
    refused = False
    try:
        remove_lane_worktree(lane, environment=environment)
    except LaneWorktreeError:
        refused = True
    assert refused, "a locked worktree removal must be refused"
    assert link.is_symlink(), "a lock refusal must preserve the provisioned .venv link"
    _run_git(repo, "worktree", "unlock", str(lane.path), environment=environment)
    remove_lane_worktree(lane, environment=environment)
    assert not lane.path.exists(), "an unlocked disposable lane still removes normally"
    return 3


def _assert_foreign_venv_refuses_before_git_removal(
    root: Path, environment: dict[str, str],
) -> int:
    """A foreign `.venv` must refuse before the earned force reaches Git."""
    repo = _fixture(root, environment)
    lane = lane_worktree_for(repo, role_name="lane-foreign-venv", agent_instance_id="agi-foreign")
    provision_lane_worktree(lane, environment=environment)
    link = lane.path / ".venv"
    link.unlink()
    link.mkdir()
    refused = False
    try:
        remove_lane_worktree(lane, environment=environment)
    except LaneWorktreeError:
        refused = True
    assert refused, "a non-provisioned .venv must refuse removal"
    assert lane.path.exists() and link.is_dir(), "foreign .venv survives the refusal"
    link.rmdir()
    link.symlink_to(repo / ".venv", target_is_directory=True)
    remove_lane_worktree(lane, environment=environment)
    return 2


def _advance_master(repo: Path, name: str, body: str, environment: dict[str, str]) -> None:
    """Land one commit on the integration head after a lane is already checked out."""
    (repo / name).write_text(body, encoding="utf-8")
    _run_git(repo, "add", name, environment=environment)
    _run_git(repo, "commit", "-m", f"master advances {name}", environment=environment)


def _lane_at_base(
    root: Path, environment: dict[str, str], name: str,
) -> tuple[Path, LaneWorktree]:
    """One fixture repo plus one provisioned lane worktree still at its own base."""
    repo = _fixture(root, environment)
    lane = lane_worktree_for(repo, role_name=name, agent_instance_id=f"agi-{name}")
    provision_lane_worktree(lane, environment=environment)
    return repo, lane


def _assert_sweep_tolerates_a_stuck_orphan(root: Path, environment: dict[str, str]) -> int:
    """D1: opportunistic tidying must never fail the operation it is tidying for.

    Red mutation: restore ``suppress=False`` removal inside the sweep loop, so the
    stuck orphan aborts the sweep and the following provision never happens.
    """
    repo, stuck = _lane_at_base(root, environment, "lane-stuck")
    clean = lane_worktree_for(repo, role_name="lane-clean", agent_instance_id="agi-clean")
    provision_lane_worktree(clean, environment=environment)
    (stuck.path / "tracked.txt").write_text("divergent lane bytes\n", encoding="utf-8")

    sweep = sweep_orphaned_lane_worktrees(
        repo,
        terminal_paths=(stuck.path, clean.path),
        environment=environment,
    )

    assert sweep.removed == (clean.path.resolve(),), sweep
    assert len(sweep.skipped) == 1, sweep
    warning = sweep.skipped[0]
    assert isinstance(warning, DirtyStaleWorktreeSkippedWarning), warning
    assert warning.code == "dirty_stale_worktree_skipped", warning
    assert warning.path == stuck.path.resolve(), warning
    assert "tracked.txt" in warning.git_diagnostic, warning
    assert stuck.path.exists(), "a skipped orphan must survive the sweep intact"

    fresh = lane_worktree_for(repo, role_name="lane-fresh", agent_instance_id="agi-fresh")
    provision_lane_worktree(fresh, environment=environment)
    assert fresh.path.is_dir(), "a stuck orphan must not block a later provision"
    return 6


def _assert_disposability_predicate(root: Path, environment: dict[str, str]) -> int:
    """D2: decide disposability in Git's own diff semantics, and name refusals.

    Red mutations: implement the comparison as a per-path content check and (iv),
    (v) and (vi) all pass while destroying real state; unscope it from the at-risk
    set to the whole tree and (vii) flips to not-disposable, so the sweep silently
    stops removing anything.
    """
    cases = (
        ("matching", _case_modified_but_matching, True, ()),
        ("diverging", _case_modified_and_diverging, False, ("tracked.txt",)),
        ("caches", _case_untracked_caches_only, True, ()),
        ("mode", _case_mode_only_change, False, ("tracked.txt",)),
        ("symlink", _case_retargeted_symlink, False, ("link",)),
        ("deleted", _case_tracked_deletion, False, ("tracked.txt",)),
        ("stale-base", _case_stale_base_unrelated_movement, True, ()),
    )
    for name, build, expected, offending in cases:
        repo, lane = _lane_at_base(root / name, environment, f"lane-{name}")
        build(repo, lane, environment)
        verdict = lane_worktree_disposability(lane, environment=environment)
        assert verdict.disposable is expected, (name, verdict)
        assert verdict.offending_paths == offending, (name, verdict)
        for path in offending:
            assert path in verdict.reason, (name, verdict.reason)
    return len(cases)


def _case_modified_but_matching(
    repo: Path, lane: LaneWorktree, environment: dict[str, str],
) -> None:
    """(i) The lane's uncommitted bytes are exactly what already landed."""
    _advance_master(repo, "tracked.txt", "landed\n", environment)
    (lane.path / "tracked.txt").write_text("landed\n", encoding="utf-8")


def _case_modified_and_diverging(
    repo: Path, lane: LaneWorktree, environment: dict[str, str],
) -> None:
    """(ii) Uncommitted bytes that exist nowhere else must refuse, by name."""
    (lane.path / "tracked.txt").write_text("only here\n", encoding="utf-8")


def _case_untracked_caches_only(
    repo: Path, lane: LaneWorktree, environment: dict[str, str],
) -> None:
    """(iii) Declared build output is residue, not state."""
    for directory, filename in (
        (".ruff_cache", "cache.bin"),
        ("__pycache__", "mod.pyc"),
        ("solet.egg-info", "PKG-INFO"),
    ):
        (lane.path / directory).mkdir()
        (lane.path / directory / filename).write_text("residue\n", encoding="utf-8")


def _assert_build_output_allowlist(root: Path, environment: dict[str, str]) -> int:
    """`build/` is reproducible setuptools output, not a `.gitignore` inference.

    Red mutation: remove ``build`` from the declared allowlist.  The fixture then
    becomes non-disposable and names ``build/`` as its refused residue.
    """
    repo, lane = _lane_at_base(root, environment, "lane-build-output")
    for directory, filename in (
        ("build/bdist.macosx-26.0-arm64", "placeholder"),
        ("build/lib", "package.py"),
    ):
        destination = lane.path / directory
        destination.mkdir(parents=True)
        (destination / filename).write_text("setuptools output\n", encoding="utf-8")

    accepted = lane_worktree_disposability(lane, environment=environment)
    assert accepted.disposable, accepted

    declared = lane_worktrees._DISPOSABLE_UNTRACKED_DIRECTORIES
    try:
        lane_worktrees._DISPOSABLE_UNTRACKED_DIRECTORIES = declared - {"build"}
        refused = lane_worktree_disposability(lane, environment=environment)
    finally:
        lane_worktrees._DISPOSABLE_UNTRACKED_DIRECTORIES = declared
    assert not refused.disposable, refused
    assert refused.offending_paths == ("build/",), refused
    assert "build/" in refused.reason, refused.reason
    return 4


def _case_mode_only_change(
    repo: Path, lane: LaneWorktree, environment: dict[str, str],
) -> None:
    """(iv) Identical bytes, changed exec bit; a content check would miss it."""
    (lane.path / "tracked.txt").chmod(0o755)


def _case_retargeted_symlink(
    repo: Path, lane: LaneWorktree, environment: dict[str, str],
) -> None:
    """(v) Link values differ while both targets' contents are identical."""
    for name in ("alpha.txt", "beta.txt"):
        (repo / name).write_text("identical\n", encoding="utf-8")
    (repo / "link").symlink_to("alpha.txt")
    _run_git(repo, "add", "alpha.txt", "beta.txt", "link", environment=environment)
    _run_git(repo, "commit", "-m", "master tracks a symlink", environment=environment)
    _run_git(lane.path, "merge", "--ff-only", "master", environment=environment)
    (lane.path / "link").unlink()
    (lane.path / "link").symlink_to("beta.txt")


def _case_tracked_deletion(
    repo: Path, lane: LaneWorktree, environment: dict[str, str],
) -> None:
    """(vi) A tracked file deleted in the worktree is real unlanded state."""
    (lane.path / "tracked.txt").unlink()


def _case_stale_base_unrelated_movement(
    repo: Path, lane: LaneWorktree, environment: dict[str, str],
) -> None:
    """(vii) Master moved in other lanes' paths; that is the normal case."""
    _advance_master(repo, "unrelated.txt", "another lane landed this\n", environment)
    _advance_master(repo, "tracked.txt", "landed\n", environment)
    (lane.path / "tracked.txt").write_text("landed\n", encoding="utf-8")


def _assert_path_refusals(root: Path) -> int:
    repo = root / "not-a-repo"
    repo.mkdir(parents=True)
    try:
        lane_worktree_for(repo, role_name="../../escape", agent_instance_id="agi-ok")
    except LaneWorktreeError:
        return 1
    raise AssertionError("unsafe role name must fail before any Git command")


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="lane-worktrees-") as temporary:
        root = Path(temporary).resolve()
        environment = _environment(root)
        checks = _assert_red_pre_fix_shape(root / "red", environment)
        checks += _assert_shared_dirt_isolation(root / "green", environment)
        checks += _assert_sweep_is_exact(root / "sweep", environment)
        checks += _assert_lock_refusal_preserves_shared_venv(root / "locked", environment)
        checks += _assert_foreign_venv_refuses_before_git_removal(
            root / "foreign-venv", environment,
        )
        checks += _assert_sweep_tolerates_a_stuck_orphan(root / "stuck", environment)
        checks += _assert_disposability_predicate(root / "disposability", environment)
        checks += _assert_build_output_allowlist(root / "build-output", environment)
        checks += _assert_path_refusals(root / "refusal")
    print(f"lane_worktrees_smoke OK: {checks} checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
