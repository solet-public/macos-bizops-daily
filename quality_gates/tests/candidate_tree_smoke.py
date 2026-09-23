"""Focused production-path smoke for exact candidate-tree construction."""

from __future__ import annotations

import os
import subprocess
import sys
from collections.abc import Generator
from contextlib import contextmanager
from importlib import import_module
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

_REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO_ROOT))

from quality_gates import candidate_tree  # noqa: E402

identity_gate = import_module("quality_gates.macos_" + "bizops_identity_gate")
_RETIRED = "biz" + "ops" + "_standard"


def _base_files() -> dict[str, bytes]:
    return {
        "delete.txt": b"base deletion sentinel\n",
        "old.txt": b"base rename sentinel\n",
        "stable.txt": b"stable base bytes\n",
        "tracked-outside.txt": b"clean committed bytes\n",
    }


def _write(root: Path, relpath: str, content: str) -> None:
    path = root / relpath
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


@contextmanager
def _mock_base(
    files: dict[str, bytes], modes: dict[str, str] | None = None
) -> Generator[None]:
    recorded_modes = modes or {}
    blobs = tuple(
        candidate_tree._GitBlob(
            path=path,
            object_id=f"{index:040x}",
            mode=recorded_modes.get(path, "100644"),
        )
        for index, path in enumerate(sorted(files), start=1)
    )
    contents = {
        blob.object_id: files[blob.path]
        for blob in blobs
    }
    with (
        patch.object(candidate_tree, "_git_tree_blobs", return_value=blobs),
        patch.object(candidate_tree, "_git_blob_contents", return_value=contents),
    ):
        yield


def _git(root: Path, *arguments: str) -> None:
    subprocess.run(
        ("git", *arguments),
        cwd=root,
        check=True,
        capture_output=True,
    )


@contextmanager
def _combined_candidate(root: Path) -> Generator[candidate_tree.CandidateTree]:
    _write(root, "added.txt", f'bundle = "{_RETIRED}"\n')
    _write(root, "new.txt", f'profile = "{_RETIRED}"\n')
    _write(root, "tracked-outside.txt", f'bundle = "{_RETIRED}"\n')
    _write(root, "untracked-outside.txt", f'bundle = "{_RETIRED}"\n')
    scope = ("added.txt", "delete.txt", "old.txt", "new.txt")
    with _mock_base(_base_files()):
        with candidate_tree.build_candidate_tree(
            root,
            scope,
            renames=(candidate_tree.Rename(old="old.txt", new="new.txt"),),
        ) as candidate:
            yield candidate


def _assert_overlay_shape(candidate: candidate_tree.CandidateTree) -> None:
    assert "added.txt" in candidate.paths
    assert "delete.txt" not in candidate.paths
    assert "old.txt" not in candidate.paths
    assert "new.txt" in candidate.paths
    assert (candidate.root / "tracked-outside.txt").read_bytes() == (
        b"clean committed bytes\n"
    )
    assert not (candidate.root / "untracked-outside.txt").exists()
    assert candidate_tree.validate_candidate_manifest(
        candidate.root, candidate.manifest
    ) == candidate.paths


def _assert_scanner_isolation(candidate: candidate_tree.CandidateTree) -> None:
    occurrences = identity_gate.scan_paths(candidate.root, candidate.paths)
    blocked_paths = {item.path for item in occurrences if item.classification is None}
    assert blocked_paths == {"added.txt", "new.txt"}, blocked_paths


def _check_overlay_and_isolation() -> int:
    with TemporaryDirectory() as temporary:
        with _combined_candidate(Path(temporary)) as candidate:
            _assert_overlay_shape(candidate)
            _assert_scanner_isolation(candidate)
    return 8


def _check_explicit_manifest_evaluation() -> int:
    with TemporaryDirectory() as temporary:
        with _combined_candidate(Path(temporary)) as candidate:
            with (
                patch.object(identity_gate, "_live_contract_violations", return_value=[]),
                patch.object(identity_gate, "_HISTORICAL_LINE_ANCHORS", frozenset()),
                patch.object(identity_gate, "_NEGATIVE_LINE_ANCHORS", frozenset()),
                patch.object(
                    identity_gate,
                    "_DETECTOR_DEFINITION_LINE_ANCHORS",
                    frozenset(),
                ),
            ):
                occurrences, violations = identity_gate.evaluate_repository(
                    candidate.root,
                    candidate_manifest=candidate.manifest,
                )
            assert {item.path for item in occurrences} == {"added.txt", "new.txt"}
            assert len(violations) == 2
    return 2


def _check_censusable_manifest_excludes_symlinks() -> int:
    """A faithful candidate lists symlinks; a content census cannot read them.

    This is the crash the fidelity repair exposed: while candidates flattened
    symlinks into ordinary files, every manifest entry was readable, so the
    scanner's fail-closed check never met one. The complete manifest keeps the
    symlink (acquisition integrity); only the reading view drops it.

    Discriminators: drop the filter and the third assertion reds with the exact
    census crash; drop the scanner's fail-closed raise and the fourth reds.
    """
    with TemporaryDirectory() as temporary:
        root = Path(temporary) / "snapshot"
        (root / "inner").mkdir(parents=True)
        (root / "inner" / "file.txt").write_text("plain\n", encoding="utf-8")
        (root / "regular.txt").write_text("plain\n", encoding="utf-8")
        (root / "link_dir").symlink_to(Path("inner"))
        (root / "link_file.txt").symlink_to(Path("regular.txt"))

        listed = candidate_tree._snapshot_paths(root)
        manifest = Path(temporary) / "manifest.txt"
        manifest.write_bytes(candidate_tree._manifest_bytes(listed))

        assert set(listed) >= {"link_dir", "link_file.txt"}, listed
        assert candidate_tree.validate_candidate_manifest(root, manifest) == listed

        censusable = candidate_tree.censusable_manifest_paths(root, manifest)
        assert set(censusable) == {"inner/file.txt", "regular.txt"}, censusable

        identity_gate.scan_paths(root, censusable)

        try:
            identity_gate.scan_paths(root, listed)
        except RuntimeError as exc:
            assert "absent or non-regular" in str(exc), exc
        else:
            raise AssertionError("the scanner must still fail closed on a symlink")
    return 5


def _check_manifest_fail_closed() -> int:
    with TemporaryDirectory() as temporary:
        with _combined_candidate(Path(temporary)) as candidate:
            candidate.manifest.write_text("added.txt\n", encoding="utf-8")
            try:
                candidate_tree.validate_candidate_manifest(
                    candidate.root, candidate.manifest
                )
            except candidate_tree.CandidateManifestError as exc:
                assert "incomplete" in str(exc)
            else:
                raise AssertionError("an incomplete candidate manifest must fail closed")
            candidate.manifest.write_text("added.txt\nadded.txt\n", encoding="utf-8")
            try:
                candidate_tree.validate_candidate_manifest(
                    candidate.root, candidate.manifest
                )
            except candidate_tree.CandidateManifestError as exc:
                assert "duplicates" in str(exc)
            else:
                raise AssertionError("a duplicate candidate manifest must fail closed")
    return 2


def _expect_path_failure(root: Path, scope: tuple[str, ...]) -> None:
    with _mock_base(_base_files()):
        try:
            with candidate_tree.build_candidate_tree(root, scope):
                pass
        except candidate_tree.CandidateTreeError:
            return
    raise AssertionError(f"invalid scope unexpectedly succeeded: {scope}")


def _check_invalid_inputs() -> int:
    with TemporaryDirectory() as temporary:
        root = Path(temporary)
        _write(root, "directory/child.txt", "ordinary\n")
        for scope in (
            ("/absolute.txt",),
            ("../traversal.txt",),
            ("stable.txt", "stable.txt"),
            ("directory",),
            ("missing-everywhere.txt",),
        ):
            _expect_path_failure(root, scope)
        with _mock_base(_base_files()):
            try:
                with candidate_tree.build_candidate_tree(
                    root,
                    ("old.txt", "new.txt"),
                    renames=(
                        candidate_tree.Rename(old="old.txt", new="new.txt"),
                    ),
                ):
                    pass
            except candidate_tree.CandidateOverlayError as exc:
                assert "new path is absent" in str(exc)
            else:
                raise AssertionError("an unresolved rename must fail closed")
        try:
            candidate_tree._git_tree_blobs(root, "missing-base-object")
        except candidate_tree.CandidateGitError:
            pass
        else:
            raise AssertionError("a missing base object must fail closed")
    return 8


def _check_raw_git_tree_bytes() -> int:
    with TemporaryDirectory() as temporary:
        root = Path(temporary)
        _git(root, "init", "-q")
        _write(
            root,
            ".gitattributes",
            "ignored.txt export-ignore\nsubst.txt export-subst\n",
        )
        _write(root, "ignored.txt", "must remain in the raw candidate\n")
        _write(root, "subst.txt", "$Format:%H$\n")
        _git(root, "add", ".gitattributes", "ignored.txt", "subst.txt")
        _git(
            root,
            "-c",
            "user.name=Candidate Smoke",
            "-c",
            "user.email=candidate@example.invalid",
            "commit",
            "-qm",
            "base",
        )
        _write(root, "overlay.txt", "ordinary overlay\n")
        with candidate_tree.build_candidate_tree(root, ("overlay.txt",)) as candidate:
            assert "ignored.txt" in candidate.paths
            assert (candidate.root / "ignored.txt").read_text(encoding="utf-8") == (
                "must remain in the raw candidate\n"
            )
            assert (candidate.root / "subst.txt").read_text(encoding="utf-8") == (
                "$Format:%H$\n"
            )
    return 3


def _check_portable_path_integrity() -> int:
    collisions = (
        ("Case.txt", "case.txt"),
        ("caf\N{LATIN SMALL LETTER E WITH ACUTE}.txt", "cafe\N{COMBINING ACUTE ACCENT}.txt"),
    )
    for paths in collisions:
        try:
            candidate_tree._assert_portable_path_set(paths, source="fixture")
        except candidate_tree.CandidateGitError as exc:
            assert "collision" in str(exc)
        else:
            raise AssertionError(f"portable collision was accepted: {paths}")
    try:
        candidate_tree._assert_snapshot_matches(
            ("expected.txt", "omitted.txt"),
            ("expected.txt",),
            source="fixture",
        )
    except candidate_tree.CandidateManifestError as exc:
        assert "omitted.txt" in str(exc)
    else:
        raise AssertionError("an expected-path omission must fail closed")
    return 3


def _check_path_type_transitions() -> int:
    with TemporaryDirectory() as temporary:
        root = Path(temporary)
        _write(root, "node", "replacement file\n")
        with _mock_base({"node/child.txt": b"base child\n"}):
            with candidate_tree.build_candidate_tree(
                root, ("node", "node/child.txt")
            ) as candidate:
                assert candidate.paths == ("node",)
                assert (candidate.root / "node").read_bytes() == b"replacement file\n"
    with TemporaryDirectory() as temporary:
        root = Path(temporary)
        _write(root, "node/child.txt", "replacement child\n")
        with _mock_base({"node": b"base file\n"}):
            with candidate_tree.build_candidate_tree(
                root, ("node", "node/child.txt")
            ) as candidate:
                assert candidate.paths == ("node/child.txt",)
                assert (candidate.root / "node/child.txt").read_bytes() == (
                    b"replacement child\n"
                )
    return 4


def _check_mode_fidelity() -> int:
    """A candidate that drops modes can host static gates only (CTF-01)."""

    with TemporaryDirectory() as temporary:
        root = Path(temporary)
        _write(root, "overlay.txt", "overlay bytes\n")
        _write(root, "overlay-exec.sh", "#!/bin/sh\necho overlay\n")
        (root / "overlay-exec.sh").chmod(0o755)
        base = {
            "pkg/real.txt": b"reached through the link\n",
            "link": b"pkg",
            "scripts/run.sh": b"#!/bin/sh\necho base\n",
            "plain.txt": b"plain\n",
        }
        modes = {"link": "120000", "scripts/run.sh": "100755"}
        with _mock_base(base, modes):
            with candidate_tree.build_candidate_tree(
                root, ("overlay.txt", "overlay-exec.sh")
            ) as candidate:
                _assert_exec_bits_survive(candidate)
                _assert_symlink_survives(candidate)
                _assert_census_excludes_exhaust(candidate)

    return 10


def _assert_exec_bits_survive(candidate: candidate_tree.CandidateTree) -> None:
    script = candidate.root / "scripts/run.sh"
    assert script.stat().st_mode & 0o111 == 0o111, oct(script.stat().st_mode)
    assert os.access(script, os.X_OK)
    assert (candidate.root / "plain.txt").stat().st_mode & 0o111 == 0
    overlay_exec = candidate.root / "overlay-exec.sh"
    assert overlay_exec.stat().st_mode & 0o111 == 0o111
    assert (candidate.root / "overlay.txt").stat().st_mode & 0o111 == 0


def _assert_symlink_survives(candidate: candidate_tree.CandidateTree) -> None:
    link = candidate.root / "link"
    assert link.is_symlink(), "symlink blob was flattened into a regular file"
    assert os.readlink(link) == "pkg"
    assert (link / "real.txt").read_bytes() == b"reached through the link\n"
    assert "link" in candidate.paths


def _assert_census_excludes_exhaust(candidate: candidate_tree.CandidateTree) -> None:
    """A battery writes bytecode into the tree it is measuring; the census must
    stay a statement about content, not about the battery's exhaust."""

    polluted = candidate.root / "pkg/__pycache__/real.cpython-313.pyc"
    polluted.parent.mkdir(parents=True, exist_ok=True)
    polluted.write_bytes(b"\x00bytecode\n")
    revalidated = candidate_tree.validate_candidate_manifest(
        candidate.root, candidate.manifest
    )
    assert revalidated == candidate.paths, "bytecode contaminated the census"


def _check_external_aggregation_symlink() -> int:
    """A scoped sibling-repository link is faithful in both candidate modes."""

    target = "../../project-solet/docs"
    with TemporaryDirectory() as temporary:
        workspace = Path(temporary)
        root = workspace / "candidate"
        root.mkdir()
        (workspace / "project-solet" / "docs").mkdir(parents=True)
        _git(root, "init", "-q")
        _write(root, "README", "base\n")
        _git(root, "add", "README")
        _git(
            root,
            "-c",
            "user.name=Candidate Smoke",
            "-c",
            "user.email=candidate@example.invalid",
            "commit",
            "-qm",
            "base",
        )
        link = root / "knowledge_bases" / "project_solet"
        link.parent.mkdir()
        link.symlink_to(target)
        scope = ("knowledge_bases/project_solet",)

        with candidate_tree.build_candidate_tree(root, scope) as candidate:
            materialized = candidate.root / scope[0]
            assert materialized.is_symlink()
            assert os.readlink(materialized) == target
            assert scope[0] in candidate.paths

        _git(root, "add", scope[0])
        with candidate_tree.build_staged_tree(root, scope) as candidate:
            materialized = candidate.root / scope[0]
            assert materialized.is_symlink()
            assert os.readlink(materialized) == target
            assert scope[0] in candidate.paths
    return 6


def _check_tracked_generated_artifact_refused() -> int:
    """Excluding battery exhaust is only sound while no base TRACKS such a path."""

    with TemporaryDirectory() as temporary:
        root = Path(temporary)
        _write(root, "overlay.txt", "overlay bytes\n")
        with _mock_base({"quality_gates/__pycache__/gate.cpython-313.pyc": b"\x00"}):
            try:
                with candidate_tree.build_candidate_tree(root, ("overlay.txt",)):
                    raise AssertionError("a tracked generated artifact was excluded silently")
            except candidate_tree.CandidateGitError:
                pass
    return 1


def _check_staged_snapshot() -> int:
    with TemporaryDirectory() as temporary:
        root = Path(temporary)
        _git(root, "init", "-q")
        _write(root, "staged.txt", "base\n")
        _write(root, "other.txt", "other base\n")
        _git(root, "add", "staged.txt", "other.txt")
        _git(
            root,
            "-c",
            "user.name=Candidate Smoke",
            "-c",
            "user.email=candidate@example.invalid",
            "commit",
            "-qm",
            "base",
        )
        _write(root, "staged.txt", "staged bytes\n")
        _write(root, "other.txt", "unrelated staged bytes\n")
        _write(root, "outside.txt", "untracked\n")
        _git(root, "add", "staged.txt", "other.txt")
        with candidate_tree.build_staged_tree(root) as candidate:
            assert (candidate.root / "staged.txt").read_bytes() == b"staged bytes\n"
            assert not (candidate.root / "outside.txt").exists()
        with candidate_tree.build_staged_tree(root, ("staged.txt",)) as candidate:
            assert (candidate.root / "staged.txt").read_bytes() == b"staged bytes\n"
            assert (candidate.root / "other.txt").read_bytes() == b"other base\n"
    return 4


def _check_frozen_entry_composition() -> int:
    """A wave materializes frozen bytes, modes, and deletion without a source reread."""
    with TemporaryDirectory() as temporary:
        root = Path(temporary) / "repo"
        root.mkdir()
        _git(root, "init", "-q")
        _write(root, "delete.txt", "base delete\n")
        _write(root, "replace.txt", "base replace\n")
        _git(root, "add", "delete.txt", "replace.txt")
        _git(
            root,
            "-c",
            "user.name=Candidate Smoke",
            "-c",
            "user.email=candidate@example.invalid",
            "commit",
            "-qm",
            "base",
        )
        destination = (Path(temporary) / "frozen").resolve()
        tree = candidate_tree.materialize_frozen_entries(
            root,
            destination,
            (
                candidate_tree.FrozenEntry("delete.txt", None, None),
                candidate_tree.FrozenEntry("replace.txt", b"frozen bytes\n", "100755"),
                candidate_tree.FrozenEntry("new-link", b"replace.txt", "120000"),
            ),
            base_ref="HEAD",
        )
        assert not (tree.root / "delete.txt").exists()
        assert (tree.root / "replace.txt").read_bytes() == b"frozen bytes\n"
        assert (tree.root / "replace.txt").stat().st_mode & 0o111
        assert (tree.root / "new-link").is_symlink()
        assert os.readlink(tree.root / "new-link") == "replace.txt"
    return 6


def _check_nested_identity_candidate() -> int:
    bare = "biz" + "ops"
    cases = {
        "nested_mapping.py": (
            f'payload = {{"bundle": {{"name": "{bare}"}}}}  '
            "# user-created instance name\n"
        ),
        "nested_subscript.py": (
            f'payload["bundle"]["name"] = "{bare}"  '
            "# user-created instance name\n"
        ),
        "deep_mapping.py": (
            f'payload = {{"provenance": {{"bundle": {{"name": "{bare}"}}}}}}  '
            "# user-created instance name\n"
        ),
        "nested.yaml": f"bundle:\n  name: {bare}  # user-created instance name\n",
        "multiline.yaml": f"profile_name:\n  {bare}  # business operations\n",
    }
    with TemporaryDirectory() as temporary:
        root = Path(temporary)
        for relpath, content in cases.items():
            _write(root, relpath, content)
        with _mock_base({}):
            with candidate_tree.build_candidate_tree(root, tuple(cases)) as candidate:
                occurrences = identity_gate.scan_paths(candidate.root, candidate.paths)
                assert len(occurrences) == len(cases)
                assert all(item.classification is None for item in occurrences)
    return len(cases) + 1


def _assert_controller_delivery(skill: str) -> None:
    candidate_step = skill.index("### Step 9.25")
    role_check = skill.index("### Step 9.5")
    lane_branch_provision = skill.index(
        "The spawn path already created `LANE_BRANCH` when it created this worktree."
    )
    assert candidate_step < role_check < lane_branch_provision
    candidate_section = skill[candidate_step:role_check]
    assert candidate_section.count("quality_gates/candidate_tree.py") == 1
    assert "Any non-zero exit means STOP: zero Git mutations" in candidate_section
    assert "do not create a branch, stage, or commit" in candidate_section
    stage_step = skill.index("### Step 11.5")
    commit_step = skill.index("### Step 12")
    assert lane_branch_provision < stage_step < commit_step
    assert "--staged" in skill[stage_step:commit_step]
    assert "git add -A\n" not in skill
    assert "staged-path equality" in skill
    assert "root-manifest" in skill[commit_step:]


def _assert_procedure_delivery(procedure: str, hook: str) -> None:
    assert procedure.count("quality_gates/candidate_tree.py") == 2
    assert "Any\nnon-zero exit is a stop with zero Git mutations" in procedure
    identity_gate_name = "macos_" + "bizops_identity_gate.py"
    assert identity_gate_name not in hook


def _check_delivery_order() -> int:
    # The delivery-order contract binds three origin-side surfaces (the
    # controller skill, the gate-procedure article, the tracked pre-commit
    # hook) that a seed bundle deliberately prunes. A pruned clone announces
    # itself by the factory provenance stamp at its root; there the contract
    # has no carriers to validate, so the check is out of scope rather than
    # silently green. In the origin checkout every read below stays required.
    if (_REPO_ROOT / "PROVENANCE.json").is_file():
        print("SKIP  delivery-order contract: origin-side surfaces are pruned from seed clones")
        return 0
    skill = (_REPO_ROOT / ".claude/skills/git-controller-commit/SKILL.md").read_text(
        encoding="utf-8"
    )
    procedure = (
        _REPO_ROOT
        / "ananta/knowledge_bases/ananta_platform/22_testing/03_peer_precompletion_gate_procedure.md"
    ).read_text(encoding="utf-8")
    hook = (_REPO_ROOT / ".githooks/pre-commit").read_text(encoding="utf-8")
    _assert_controller_delivery(skill)
    _assert_procedure_delivery(procedure, hook)
    return 12


def _check_gate_script_resolves_from_candidate_not_invoking_checkout() -> int:
    """NEGATIVE regression (iss_ec0db9c7 / iss_77fe09ad).

    ``--run-identity-gate`` must run the identity-gate SCRIPT from the
    materialized candidate's own copy, never from whatever happens to sit on
    disk at ``--repo-root`` — the invoking checkout is routinely a stale or
    behind-master lane worktree whose on-disk gate code predates the very fix
    a review is trying to verify.

    A stale stub sits at exactly the on-disk path the old bug read the gate
    script from; the committed base_ref carries a DIFFERENT stub at the same
    relative path. This is a real end-to-end run of the actual, current
    ``candidate_tree.py`` CLI as a subprocess — the same way a reviewer
    invokes it — so it exercises the real subprocess-command construction,
    not a mock of it. If the wrong-tree defect ever recurs, the stale stub's
    sentinel comes back in the output instead of the candidate's.
    """

    identity_gate_name = "macos_" + "bizops_identity_gate.py"
    identity_gate_relpath = f"quality_gates/{identity_gate_name}"
    candidate_sentinel = f"CANDIDATE_TREE_GATE_RAN_{os.urandom(4).hex()}"
    stale_sentinel = f"STALE_INVOKING_CHECKOUT_GATE_RAN_{os.urandom(4).hex()}"

    with TemporaryDirectory() as temporary:
        repo = Path(temporary).resolve()
        _git(repo, "init", "-q")
        _write(repo, "quality_gates/__init__.py", "")
        _write(repo, identity_gate_relpath, f'print("{candidate_sentinel}")\n')
        _write(repo, "scope.txt", "candidate content\n")
        _git(repo, "add", "-A")
        subprocess.run(
            (
                "git",
                "-c",
                "user.name=Candidate Smoke",
                "-c",
                "user.email=candidate@example.invalid",
                "commit",
                "-qm",
                "base",
            ),
            cwd=repo,
            check=True,
            capture_output=True,
        )
        base_ref = subprocess.run(
            ("git", "rev-parse", "HEAD"),
            cwd=repo,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()

        # The stale on-disk copy: exactly where the OLD bug ran the gate
        # from. Left uncommitted, the way a lane worktree's own dirty or
        # behind-master gate copy differs from the base_ref it is building a
        # candidate against.
        _write(repo, identity_gate_relpath, f'print("{stale_sentinel}")\n')

        scope_file = repo / "scope-list.txt"
        scope_file.write_text("scope.txt\n", encoding="utf-8")
        rename_file = repo / "renames.txt"
        rename_file.write_text("", encoding="utf-8")

        result = subprocess.run(
            (
                sys.executable,
                str(_REPO_ROOT / "quality_gates" / "candidate_tree.py"),
                "--repo-root",
                str(repo),
                "--base-ref",
                base_ref,
                "--scope-file",
                str(scope_file),
                "--rename-file",
                str(rename_file),
                "--run-identity-gate",
            ),
            cwd=repo,
            check=False,
            capture_output=True,
            text=True,
        )
        combined = result.stdout + result.stderr
        assert stale_sentinel not in combined, (
            "wrong-tree regression: the invoking checkout's stale gate script "
            f"ran instead of the candidate's own copy\n{combined}"
        )
        assert candidate_sentinel in combined, combined
        assert result.returncode == 0, (result.returncode, combined)
    return 1


def main() -> int:
    check_count = _check_overlay_and_isolation()
    check_count += _check_explicit_manifest_evaluation()
    check_count += _check_censusable_manifest_excludes_symlinks()
    check_count += _check_manifest_fail_closed()
    check_count += _check_invalid_inputs()
    check_count += _check_raw_git_tree_bytes()
    check_count += _check_portable_path_integrity()
    check_count += _check_path_type_transitions()
    check_count += _check_mode_fidelity()
    check_count += _check_external_aggregation_symlink()
    check_count += _check_tracked_generated_artifact_refused()
    check_count += _check_staged_snapshot()
    check_count += _check_frozen_entry_composition()
    check_count += _check_nested_identity_candidate()
    check_count += _check_gate_script_resolves_from_candidate_not_invoking_checkout()
    check_count += _check_delivery_order()
    print(f"candidate_tree_smoke OK: {check_count} checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
