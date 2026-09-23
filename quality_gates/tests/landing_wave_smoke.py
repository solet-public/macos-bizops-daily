#!/usr/bin/env python3
"""Offline smoke controls for the read-only landing-wave helpers."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from tempfile import TemporaryDirectory

_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from quality_gates import landing_wave, landing_wave_battery  # noqa: E402
from quality_gates.candidate_tree import FrozenEntry  # noqa: E402


def _git(root: Path, *arguments: str) -> str:
    result = subprocess.run(
        ("git", *arguments), cwd=root, text=True, capture_output=True, check=False,
    )
    if result.returncode != 0:
        raise AssertionError(result.stderr)
    return result.stdout


def _write(root: Path, relative: str, content: str) -> None:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _repository() -> tuple[TemporaryDirectory[str], Path]:
    temporary = TemporaryDirectory()
    root = Path(temporary.name).resolve() / "repo"
    root.mkdir()
    _git(root, "init", "-q")
    _write(root, "member-a.txt", "base a\n")
    _write(root, "member-b.txt", "base b\n")
    _git(root, "add", "member-a.txt", "member-b.txt")
    _git(
        root,
        "-c",
        "user.name=Landing Wave Smoke",
        "-c",
        "user.email=landing-wave@example.invalid",
        "commit",
        "-qm",
        "base",
    )
    return temporary, root


def _check_source_and_composition() -> int:
    temporary, root = _repository()
    try:
        base = _git(root, "rev-parse", "HEAD").strip()
        sources = landing_wave.validate_sources(root, (root,), pinned_base=base)
        assert len(sources) == 1 and sources[0].head == base
        candidate = landing_wave.compose_wave(
            root,
            (Path(temporary.name).resolve() / "candidate"),
            (
                FrozenEntry("member-a.txt", b"frozen a\n", "100755"),
                FrozenEntry("member-b.txt", None, None),
                FrozenEntry("new-link", b"member-a.txt", "120000"),
            ),
            base_ref=base,
        )
        evidence = landing_wave.verify_integration_tree(candidate)
        assert evidence.path_count == 2
        assert landing_wave.verify_final_tree(candidate, evidence) == evidence
        assert (candidate.root / "member-a.txt").read_bytes() == b"frozen a\n"
        assert (candidate.root / "new-link").is_symlink()
    finally:
        temporary.cleanup()
    return 6


def _check_global_staged_equality() -> int:
    temporary, root = _repository()
    try:
        _write(root, "member-a.txt", "changed a\n")
        _git(root, "add", "member-a.txt")
        evidence = landing_wave.verify_staged_member(
            root, unit_id="unt_a", exact_paths=("member-a.txt",),
        )
        assert evidence.paths == ("member-a.txt",)
        _write(root, "member-b.txt", "contamination\n")
        _git(root, "add", "member-b.txt")
        try:
            landing_wave.verify_staged_member(
                root, unit_id="unt_a", exact_paths=("member-a.txt",),
            )
            raise AssertionError("global staged-index contamination was accepted")
        except landing_wave.LandingWaveError as error:
            assert "does not equal" in str(error)
    finally:
        temporary.cleanup()
    return 3


def _check_one_physical_sweep() -> int:
    temporary, root = _repository()
    try:
        candidate = landing_wave.compose_wave(
            root,
            Path(temporary.name).resolve() / "candidate",
            (FrozenEntry("member-a.txt", b"frozen\n", "100644"),),
            base_ref="HEAD",
        )
        calls: list[tuple[str, ...]] = []

        def runner(command: tuple[str, ...], **_: object) -> subprocess.CompletedProcess[str]:
            calls.append(command)
            return subprocess.CompletedProcess(command, 0, "ok", "")

        receipt = landing_wave_battery.run_wave_battery(
            candidate.root,
            candidate.manifest,
            (("gate-a",), ("gate-b",)),
            runner=runner,
        )
        assert receipt.physical_sweeps == 1
        assert receipt.command_count == 2
        assert calls == [("gate-a",), ("gate-b",)]
    finally:
        temporary.cleanup()
    return 5


def _check_completion_receipt() -> int:
    payload: dict[str, object] = {
        "repository_id": "rep_7111540c-ac4a-468c-8f84-b67d8756d1d0",
        "wave_id": "lwv_test",
        "landing_id": "lnd_common",
        "final_master_commit_sha": "a" * 40,
        "final_master_tree_sha": "b" * 40,
        "wave_tip_commit_sha": "c" * 40,
        "manifest_sha256": "d" * 64,
        "accepted_wave_authorization_sha256": "e" * 64,
        "accepted_authorization_message_id": "arm_message",
        "accepted_authorization_sender_instance_id": "agi_main",
        "pre_merge_frontier_timestamp": "2026-09-12T00:00:00+00:00",
        "pre_merge_newest_message_id": None,
        "successful_gate_run_id": "lwr_run",
        "completed_at": "2026-09-12T00:01:00+00:00",
        "recorded_by_actor_id": "act_controller",
        "per_unit_landings": [
            {"unit_id": "unt_a", "unit_commit_sha": "f" * 40, "landing_id": "lnd_common"},
            {"unit_id": "unt_b", "unit_commit_sha": "1" * 40, "landing_id": "lnd_common"},
        ],
    }
    receipt = landing_wave.build_observation_receipt(payload)
    assert receipt["schema_version"] == "landing_wave_completion.v1"
    malformed = dict(payload)
    malformed["per_unit_landings"] = [
        {"unit_id": "unt_a", "unit_commit_sha": "f" * 40, "landing_id": "lnd_other"}
    ]
    try:
        landing_wave.build_observation_receipt(malformed)
        raise AssertionError("different landing mapping was accepted")
    except landing_wave.LandingWaveError:
        pass
    return 3


def _commit(root: Path, message: str) -> str:
    _git(
        root,
        "-c",
        "user.name=Landing Wave Smoke",
        "-c",
        "user.email=landing-wave@example.invalid",
        "commit",
        "-qm",
        message,
    )
    return _git(root, "rev-parse", "HEAD").strip()


def _two_lane_fixture() -> tuple[TemporaryDirectory[str], Path, Path, Path, Path]:
    temporary, shared = _repository()
    root = Path(temporary.name).resolve()
    source_a = root / "source-a"
    source_b = root / "source-b"
    integration = root / "integration"
    _git(shared, "worktree", "add", "-q", "-b", "lane/source-a", str(source_a), "HEAD")
    _git(shared, "worktree", "add", "-q", "-b", "lane/source-b", str(source_b), "HEAD")
    _git(shared, "worktree", "add", "-q", "-b", "lane/integration", str(integration), "HEAD")
    return temporary, shared, source_a, source_b, integration


def _stage_and_commit_member(root: Path, path: str, unit_id: str) -> str:
    landing_wave.verify_empty_index(root)
    _git(root, "add", path)
    landing_wave.verify_staged_member(root, unit_id=unit_id, exact_paths=(path,))
    commit = _commit(root, unit_id)
    landing_wave.verify_empty_index(root)
    return commit


def _check_two_lane_commit_and_merge() -> int:
    """Approved disposable fixture: two scoped commits then one --no-ff merge."""
    temporary, shared, source_a, source_b, integration = _two_lane_fixture()
    try:
        master_before = _git(shared, "rev-parse", "HEAD").strip()
        _write(source_a, "member-a.txt", "lane a final\n")
        _write(source_b, "member-b.txt", "lane b final\n")
        _write(integration, "member-a.txt", (source_a / "member-a.txt").read_text())
        _write(integration, "member-b.txt", (source_b / "member-b.txt").read_text())
        first = _stage_and_commit_member(integration, "member-a.txt", "unt_a")
        second = _stage_and_commit_member(integration, "member-b.txt", "unt_b")
        assert _git(shared, "rev-parse", "HEAD").strip() == master_before
        _git(shared, "merge", "--no-ff", "lane/integration", "-m", "merge wave")
        assert _git(shared, "merge-base", "--is-ancestor", first, "HEAD") == ""
        assert _git(shared, "merge-base", "--is-ancestor", second, "HEAD") == ""
    finally:
        temporary.cleanup()
    return 8


def _check_commit_two_failure_preserves_master() -> int:
    """A second-commit hook failure leaves the first commit on integration only."""
    temporary, shared, _source_a, _source_b, integration = _two_lane_fixture()
    try:
        master_before = _git(shared, "rev-parse", "HEAD").strip()
        _write(integration, "member-a.txt", "first member\n")
        first = _stage_and_commit_member(integration, "member-a.txt", "unt_a")
        hook = integration / "test-hooks" / "pre-commit"
        hook.parent.mkdir(parents=True, exist_ok=True)
        hook.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
        hook.chmod(0o755)
        _write(integration, "member-b.txt", "second member\n")
        _git(integration, "add", "member-b.txt")
        landing_wave.verify_staged_member(
            integration, unit_id="unt_b", exact_paths=("member-b.txt",),
        )
        failed = subprocess.run(
            (
                "git",
                "-c",
                "user.name=Landing Wave Smoke",
                "-c",
                "user.email=landing-wave@example.invalid",
                "-c",
                f"core.hooksPath={hook.parent}",
                "commit",
                "-m",
                "unt_b",
            ),
            cwd=integration,
            text=True,
            capture_output=True,
            check=False,
        )
        assert failed.returncode != 0
        assert _git(shared, "rev-parse", "HEAD").strip() == master_before
        ancestor = subprocess.run(
            ("git", "merge-base", "--is-ancestor", first, "HEAD"),
            cwd=shared,
            check=False,
        )
        assert ancestor.returncode != 0
        assert _git(integration, "rev-parse", "HEAD").strip() == first
    finally:
        temporary.cleanup()
    return 7


def main() -> int:
    checks = _check_source_and_composition()
    checks += _check_global_staged_equality()
    checks += _check_one_physical_sweep()
    checks += _check_completion_receipt()
    checks += _check_two_lane_commit_and_merge()
    checks += _check_commit_two_failure_preserves_master()
    print(f"landing_wave_smoke OK: {checks} checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
