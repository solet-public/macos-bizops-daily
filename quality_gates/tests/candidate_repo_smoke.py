"""Focused production-path smoke for the private candidate repository.

The property under test is the one that made a landing stop: a private Git
directory seeded from a shared one inherits the shared INDEX, which legitimately
holds another lane's staged paths.  A fidelity-correct candidate worktree
excludes that work, so index and worktree disagree on arrival and an
index-based gate reports a census for a tree that is not there.

Every check below runs against a throwaway fixture repository created under a
temporary directory.  Nothing here reads or writes the shared checkout.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from tempfile import TemporaryDirectory

_REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO_ROOT))

from quality_gates import candidate_repo  # noqa: E402

_OTHER_LANE = "other_lane/protected.txt"
_OVERLAY = "src/overlay.txt"


def _fixture_env() -> dict[str, str]:
    """A Git environment that cannot reach out of the fixture.

    Inheriting GIT_DIR or GIT_WORK_TREE from the caller would aim these
    commands at whatever repository invoked the smoke, so they are stripped
    rather than trusted.
    """

    environment = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith("GIT_")
    }
    environment.update(
        GIT_AUTHOR_NAME="Candidate Smoke",
        GIT_AUTHOR_EMAIL="candidate@example.invalid",
        GIT_COMMITTER_NAME="Candidate Smoke",
        GIT_COMMITTER_EMAIL="candidate@example.invalid",
    )
    return environment


def _git(root: Path, *arguments: str) -> str:
    result = subprocess.run(
        ("git", *arguments),
        cwd=root,
        env=_fixture_env(),
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _write(root: Path, relpath: str, content: str, *, executable: bool = False) -> None:
    path = root / relpath
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    if executable:
        path.chmod(0o755)


def _build_fixture_repo(root: Path) -> str:
    """A repo whose index holds one lane's staged work, as the shared one does."""

    _git(root, "init", "-q", "-b", "main")
    _write(root, "src/stable.txt", "stable base\n")
    _write(root, _OVERLAY, "base overlay bytes\n")
    _write(root, "scripts/run.sh", "#!/bin/sh\necho base\n", executable=True)
    _write(root, _OTHER_LANE, "other lane base\n")
    (root / "link").symlink_to("src")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "base")
    base_ref = _git(root, "rev-parse", "HEAD")

    # The other lane stages work that the candidate must never carry.
    _write(root, _OTHER_LANE, "other lane STAGED bytes\n")
    _write(root, "other_lane/added.txt", "other lane staged addition\n")
    _git(root, "add", _OTHER_LANE, "other_lane/added.txt")

    # Our lane edits its own path in the worktree only.
    _write(root, _OVERLAY, "reviewed overlay bytes\n")
    return base_ref


def _candidate_status(candidate_root: Path) -> list[str]:
    environment = {
        **_fixture_env(),
        "GIT_DIR": str(candidate_root / ".git"),
        "GIT_WORK_TREE": str(candidate_root),
    }
    result = subprocess.run(
        ("git", "status", "--porcelain", "-uall"),
        cwd=candidate_root,
        env=environment,
        check=True,
        capture_output=True,
        text=True,
    )
    return [line for line in result.stdout.splitlines() if line.strip()]


def _check_inherited_index_is_replaced() -> int:
    with TemporaryDirectory() as temporary:
        workspace = Path(temporary).resolve()
        repo = workspace / "repo"
        repo.mkdir()
        base_ref = _build_fixture_repo(repo)

        before = candidate_repo.measure_shared_repo(repo)
        assert before.protected_paths == 2, before.render()

        candidate, after = candidate_repo.build(
            repo,
            workspace / "candidate",
            (_OVERLAY,),
            renames=(),
            base_ref=base_ref,
            with_git=True,
        )
        assert before == after, "the fixture repository changed during the build"

        _assert_candidate_is_ours_only(candidate.root)
    return 9


def _assert_candidate_is_ours_only(candidate_root: Path) -> None:
    """The candidate carries this lane's overlay and no trace of the other's."""

    status = _candidate_status(candidate_root)
    assert status == [f"M  {_OVERLAY}"], status

    leaked = [line for line in status if "other_lane" in line]
    assert not leaked, f"the inherited index leaked another lane's work: {leaked}"
    assert not (candidate_root / "other_lane/added.txt").exists()
    assert (candidate_root / _OTHER_LANE).read_bytes() == b"other lane base\n"
    assert (candidate_root / _OVERLAY).read_bytes() == b"reviewed overlay bytes\n"

    script = candidate_root / "scripts/run.sh"
    assert os.access(script, os.X_OK), "the candidate dropped a tracked exec bit"
    assert (candidate_root / "link").is_symlink(), "the candidate flattened a symlink"


def _check_linked_worktree_is_measurable() -> int:
    """A lane running from its OWN worktree must be able to build a candidate.

    In a linked worktree ``.git`` is a FILE holding a ``gitdir:`` pointer, not a
    directory.  Every fleet lane works in such a worktree, so a tool that reads
    ``repo_root/.git/config`` — or copies ``repo_root/.git`` — dies with
    ``NotADirectoryError`` before a single gate runs.  Both calls below raised
    exactly that before the common-directory fix.
    """

    with TemporaryDirectory() as temporary:
        workspace = Path(temporary).resolve()
        repo = workspace / "repo"
        repo.mkdir()
        base_ref = _build_fixture_repo(repo)

        linked = workspace / "linked"
        _git(repo, "worktree", "add", "-q", "-b", "lane/one", str(linked), base_ref)
        # The discriminator: without this shape the check silently degrades
        # into a second main-checkout run and can no longer fail.
        assert (linked / ".git").is_file(), "the fixture worktree is not a linked one"

        # (1) The worktree is measurable, and it measures the SAME config —
        # one common directory, whichever checkout asks.
        from_main = candidate_repo.measure_shared_repo(repo)
        from_worktree = candidate_repo.measure_shared_repo(linked)
        assert from_worktree.config_digest == from_main.config_digest, (
            f"{from_worktree.render()} != {from_main.render()}"
        )
        # A genuinely different checkout: the other lane's staged work lives in
        # the main index only, so the worktree protects nothing.
        assert from_main.protected_paths == 2, from_main.render()
        assert from_worktree.protected_paths == 0, from_worktree.render()

        # (2) A full build with git measurement enabled, from the worktree root.
        _write(linked, _OVERLAY, "reviewed overlay bytes\n")
        candidate, after = candidate_repo.build(
            linked,
            workspace / "candidate",
            (_OVERLAY,),
            renames=(),
            base_ref=base_ref,
            with_git=True,
        )
        assert from_worktree == after, "the worktree changed during the build"
        _assert_candidate_is_ours_only(candidate.root)

        candidate_git_dir = candidate.root / ".git"
        assert candidate_git_dir.is_dir(), "the candidate did not get a real Git directory"
        assert not (candidate_git_dir / "worktrees").exists(), (
            "the candidate inherited worktree admin pointers into the shared tree"
        )
        # HEAD describes the tree the candidate was built from, not the main
        # checkout the common directory happens to belong to.
        head = (candidate_git_dir / "HEAD").read_text(encoding="utf-8").strip()
        assert head == "ref: refs/heads/lane/one", head
    return 8


def _check_index_free_git_dir_is_not_the_fix() -> int:
    """Deleting the index without rebuilding reports every file as deleted."""

    with TemporaryDirectory() as temporary:
        workspace = Path(temporary).resolve()
        repo = workspace / "repo"
        repo.mkdir()
        base_ref = _build_fixture_repo(repo)
        candidate, _ = candidate_repo.build(
            repo,
            workspace / "candidate",
            (_OVERLAY,),
            renames=(),
            base_ref=base_ref,
            with_git=True,
        )
        (candidate.root / ".git" / "index").unlink()
        status = _candidate_status(candidate.root)
        deletions = [line for line in status if line.startswith("D ")]
        assert deletions, "an index-free Git directory was expected to report deletions"
        assert len(status) > 1, status
    return 2


def _check_destination_must_be_physical() -> int:
    with TemporaryDirectory() as temporary:
        workspace = Path(temporary).resolve()
        repo = workspace / "repo"
        repo.mkdir()
        base_ref = _build_fixture_repo(repo)

        indirect = workspace / "via-link"
        indirect.symlink_to(workspace / "real")
        (workspace / "real").mkdir()
        try:
            candidate_repo.build(
                repo,
                indirect / "candidate",
                (_OVERLAY,),
                renames=(),
                base_ref=base_ref,
                with_git=False,
            )
            raise AssertionError("a symlinked destination spelling was accepted")
        except candidate_repo.CandidateRepoError as exc:
            assert "physical path" in str(exc), str(exc)

        occupied = workspace / "occupied"
        occupied.mkdir()
        (occupied / "leftover.txt").write_text("stale\n", encoding="utf-8")
        try:
            candidate_repo.build(
                repo,
                occupied,
                (_OVERLAY,),
                renames=(),
                base_ref=base_ref,
                with_git=False,
            )
            raise AssertionError("a non-empty destination was accepted")
        except candidate_repo.CandidateRepoError as exc:
            assert "not empty" in str(exc), str(exc)
    return 4


def _seed_fixture_venv(repo: Path) -> None:
    """A venv carrying the two shapes that break a naive copy.

    An editable pointer holding an ABSOLUTE path into the tree, and a console
    script whose shebang is an absolute path into the venv.  Symlinking either
    one leaves the candidate resolving back to the original tree.
    """

    venv = repo / ".venv"
    subprocess.run(
        (sys.executable, "-m", "venv", "--without-pip", str(venv)),
        check=True,
        capture_output=True,
    )
    site_packages = next(venv.glob("lib/python3.*/site-packages"))
    (site_packages / "__editable__.fixture_pkg-1.0.0.pth").write_text(
        f"{repo / 'src'}\n", encoding="utf-8"
    )
    console_script = venv / "bin" / "fixturetool"
    console_script.write_text(
        f"#!{venv / 'bin' / 'python3'}\nprint('fixture')\n", encoding="utf-8"
    )
    console_script.chmod(0o755)
    (venv / "bin" / "fixturebinary").write_bytes(b"\xcf\xfa\xed\xfe not utf-8 \x00\x01")


def _check_candidate_venv_is_rehomed() -> int:
    with TemporaryDirectory() as temporary:
        workspace = Path(temporary).resolve()
        repo = workspace / "repo"
        repo.mkdir()
        base_ref = _build_fixture_repo(repo)
        (repo / "src").mkdir(exist_ok=True)
        _seed_fixture_venv(repo)

        candidate, _ = candidate_repo.build(
            repo,
            workspace / "candidate",
            (_OVERLAY,),
            renames=(),
            base_ref=base_ref,
            with_git=False,
            with_venv=True,
        )
        candidate_venv = candidate.root / ".venv"
        assert (candidate_venv / "pyvenv.cfg").is_file(), "candidate venv is not a real venv"
        assert not candidate_venv.is_symlink(), "the shared venv was symlinked wholesale"

        site_packages = next(candidate_venv.glob("lib/python3.*/site-packages"))
        pointer = site_packages / "__editable__.fixture_pkg-1.0.0.pth"
        assert pointer.is_file() and not pointer.is_symlink(), "the pointer was not re-homed"
        payload = pointer.read_text(encoding="utf-8")
        assert str(repo) not in payload, f"pointer still names the shared tree: {payload!r}"
        assert payload.strip() == str(candidate.root / "src"), payload

        script = candidate_venv / "bin" / "fixturetool"
        assert not script.is_symlink(), "a text console script was symlinked, keeping its shebang"
        assert str(repo) not in script.read_text(encoding="utf-8"), "shebang still names the shared venv"
        assert (candidate_venv / "bin" / "fixturebinary").is_symlink(), "a binary was copied, not linked"

        probe = subprocess.run(
            (str(candidate_venv / "bin" / "python3"), "-c", "import sys; print(sys.prefix)"),
            check=True,
            capture_output=True,
            text=True,
        )
        assert Path(probe.stdout.strip()).resolve() == candidate_venv.resolve(), probe.stdout
    return 9


def _check_editable_pointer_lines_are_classified() -> int:
    """Blank lines are harmless; executable pth lines are not path pointers."""
    with TemporaryDirectory() as temporary:
        workspace = Path(temporary).resolve()
        shared = workspace / "shared"
        candidate = workspace / "candidate"
        source_root = workspace / "source"
        candidate_root = workspace / "candidate-root"
        for directory in (shared, candidate, source_root, candidate_root):
            directory.mkdir()

        pointer = shared / "__editable__.fixture.pth"
        pointer.write_text(f"{source_root}\n\n", encoding="utf-8")
        assert candidate_repo._install_site_packages(shared, candidate, source_root, candidate_root) == 1
        assert (candidate / pointer.name).read_text(encoding="utf-8") == f"{candidate_root}\n\n"

        executable = shared / "__editable__.executable.pth"
        executable.write_text("import fixture_finder; fixture_finder.install()\n", encoding="utf-8")
        try:
            candidate_repo._install_site_packages(shared, candidate, source_root, candidate_root)
            raise AssertionError("an executable editable-pth line was treated as a path pointer")
        except candidate_repo.CandidateRepoError as exc:
            assert "not an absolute path" in str(exc), str(exc)
            assert "does not resolve inside" not in str(exc), str(exc)
    return 4


def _check_shared_repo_drift_is_fatal() -> int:
    before = candidate_repo.SharedRepoProof(
        head="a" * 40,
        index_entries=11729,
        protected_paths=13,
        staged_digest="b" * 64,
        protected_digest="c" * 64,
        config_digest="d" * 64,
    )
    drifted = candidate_repo.SharedRepoProof(
        head="a" * 40,
        index_entries=11729,
        protected_paths=12,
        staged_digest="b" * 64,
        protected_digest="c" * 64,
        config_digest="d" * 64,
    )
    candidate_repo.assert_shared_repo_untouched(before, before)
    try:
        candidate_repo.assert_shared_repo_untouched(before, drifted)
        raise AssertionError("a changed protected-path count was accepted")
    except candidate_repo.CandidateRepoError as exc:
        assert "shared repository changed" in str(exc), str(exc)
    return 2


def _check_candidate_venv_rehomes_from_a_worktree() -> int:
    """A candidate built from a LINKED WORKTREE must import its OWN source.

    This is the failure the explicit ``PYTHONPATH`` prefix existed to work
    around.  A lane worktree reaches the shared virtualenv through a ``.venv``
    symlink, so every editable pointer inside it carries an absolute path into
    the checkout that OWNS that venv — the main one — and never names the
    worktree.  Re-homing keyed on ``repo_root`` therefore matched nothing, the
    pointer was copied through verbatim, and the candidate imported
    shared-checkout source while every guard reported success.

    The property asserted is that the IMPORT RESOLVES inside the candidate, not
    that a file exists there: an under-homed candidate has both files, runs
    fine, and quietly gates the wrong tree.  The two copies therefore carry
    different marker values, so the check can say which one actually loaded.
    """

    with TemporaryDirectory() as temporary:
        workspace = Path(temporary).resolve()
        repo = workspace / "repo"
        repo.mkdir()
        _build_fixture_repo(repo)

        # A real module for the editable pointer to resolve to, committed
        # without sweeping in the other lane's staged work.
        _write(repo, "src/fixture_pkg.py", "MARKER = 'main-checkout'\n")
        _git(repo, "add", "src/fixture_pkg.py")
        _git(repo, "commit", "-qm", "module", "--", "src/fixture_pkg.py")
        base_ref = _git(repo, "rev-parse", "HEAD")

        _seed_fixture_venv(repo)

        linked = workspace / "linked"
        _git(repo, "worktree", "add", "-q", "-b", "lane/venv", str(linked), base_ref)
        assert (linked / ".git").is_file(), "the fixture worktree is not a linked one"
        # The measured fleet layout: the worktree does not own a virtualenv, it
        # points at the main checkout's.  Without this the run degrades into a
        # loud "no shared virtualenv" error and stops testing under-homing.
        (linked / ".venv").symlink_to(repo / ".venv")

        # The lane's own edit, which is what the candidate must end up running.
        _write(linked, "src/fixture_pkg.py", "MARKER = 'lane-worktree'\n")

        candidate, _ = candidate_repo.build(
            linked,
            workspace / "candidate",
            ("src/fixture_pkg.py",),
            renames=(),
            base_ref=base_ref,
            with_git=False,
            with_venv=True,
        )

        candidate_venv = candidate.root / ".venv"
        site_packages = next(candidate_venv.glob("lib/python3.*/site-packages"))
        payload = (site_packages / "__editable__.fixture_pkg-1.0.0.pth").read_text(
            encoding="utf-8"
        )
        assert payload.strip() == str(candidate.root / "src"), (
            f"pointer was not re-homed from the worktree: {payload!r}"
        )
        assert str(repo) not in payload, f"pointer still names the venv's owner: {payload!r}"

        # The property that matters: the IMPORT lands in the candidate.
        probe = subprocess.run(
            (
                str(candidate_venv / "bin" / "python3"),
                "-c",
                "import fixture_pkg; print(fixture_pkg.MARKER); print(fixture_pkg.__file__)",
            ),
            check=True,
            capture_output=True,
            text=True,
        )
        marker, resolved = probe.stdout.split()
        assert marker == "lane-worktree", (
            f"the candidate imported the WRONG tree: marker={marker!r} from {resolved}"
        )
        assert Path(resolved).resolve() == (candidate.root / "src" / "fixture_pkg.py").resolve(), (
            f"import resolved outside the candidate: {resolved}"
        )
    return 4


def _check_rehoming_respects_path_boundaries() -> int:
    """Re-homing must not rewrite a SIBLING whose name merely shares a prefix.

    A lane worktree lives beside the checkout that owns the venv and its
    directory name begins with the same characters, so ``/w/base`` is a prefix
    substring of ``/w/base_lane_worktrees/l`` without being a parent of it.  A
    bare ``str.replace`` corrupts that sibling silently, and every containment
    guard in the module still passes on the corrupted value — so the naive fix
    for under-homing introduces a second, quieter defect.
    """

    source_root = Path("/w/base")
    candidate_root = Path("/w/candidate")

    sibling = "/w/base_lane_worktrees/l/src\n"
    assert _rehome_under_test(sibling, source_root, candidate_root) == sibling, (
        "a sibling path sharing a name prefix was rewritten"
    )

    contained = "/w/base/ananta/src\n"
    assert (
        _rehome_under_test(contained, source_root, candidate_root) == "/w/candidate/ananta/src\n"
    ), "a genuinely contained path was not re-homed"

    exact = "/w/base\n"
    assert _rehome_under_test(exact, source_root, candidate_root) == "/w/candidate\n", (
        "the root itself was not re-homed at a line end"
    )
    return 3


def _rehome_under_test(payload: str, source_root: Path, candidate_root: Path) -> str:
    return candidate_repo._rehome(payload, source_root, candidate_root)


def _check_pointer_guard_can_still_fail() -> int:
    """The under-homing guard must go RED on a pointer that was not re-homed.

    This guard was defeated by the very key it was checking: from a worktree it
    searched for the worktree path while the payload named the shared checkout,
    so it PASSED on exactly the input it exists to catch, and the ``rehomed``
    backstop counted the pass as a success.  A fix that repairs re-homing but
    leaves the guard keyed the same way looks verified and stays defeatable, so
    the guard is exercised here against a pointer that still names its source.
    """

    with TemporaryDirectory() as temporary:
        workspace = Path(temporary).resolve()
        source_root = workspace / "source"
        candidate_root = workspace / "candidate"
        shared = workspace / "shared_site"
        candidate_site = workspace / "candidate_site"
        for directory in (source_root, candidate_root, shared, candidate_site):
            directory.mkdir()

        # A pointer whose path is NOT under source_root, so re-homing cannot
        # rewrite it and the guard is the only thing standing between an
        # escaping pointer and a green build.
        (shared / "__editable__.escaping-1.0.0.pth").write_text(
            f"{workspace / 'elsewhere'}\n{source_root}/pkg\n", encoding="utf-8"
        )
        try:
            candidate_repo._install_site_packages(
                shared, candidate_site, source_root, candidate_root
            )
            raise AssertionError("an escaping pointer outside the candidate root was accepted")
        except candidate_repo.CandidateRepoError as exc:
            assert "does not resolve inside the candidate root" in str(exc), str(exc)
            assert str(workspace / "elsewhere") in str(exc), str(exc)
    return 1


def _check_pointer_guard_accepts_rehomed_prefix_sibling() -> int:
    """A correctly re-homed pointer may live under a base-prefixed sibling.

    Fleet worktrees commonly live under ``<owner>_lane_worktrees``.  That path
    has the owner checkout as a string prefix but is not contained by it, so a
    string-substring guard falsely rejects a correctly re-homed pointer.  The
    boundary-aware re-homing plus resolution guard must accept it.
    """

    with TemporaryDirectory() as temporary:
        workspace = Path(temporary).resolve()
        source_root = workspace / "base"
        candidate_root = workspace / "base_lane_worktrees" / "lane"
        shared = workspace / "shared_site"
        candidate_site = workspace / "candidate_site"
        for directory in (source_root, candidate_root, shared, candidate_site):
            directory.mkdir(parents=True, exist_ok=True)

        (shared / "__editable__.rehomed-1.0.0.pth").write_text(
            f"{source_root}/pkg\n", encoding="utf-8"
        )
        candidate_repo._install_site_packages(shared, candidate_site, source_root, candidate_root)
        payload = (candidate_site / "__editable__.rehomed-1.0.0.pth").read_text(encoding="utf-8")
        assert payload == f"{candidate_root}/pkg\n", payload
    return 1


def _check_venv_intruder_guard_can_still_fail() -> int:
    """Both source roots remain fatal when the candidate interpreter reaches one."""

    with TemporaryDirectory() as temporary:
        workspace = Path(temporary).resolve()
        candidate_root = workspace / "candidate"
        candidate_venv = candidate_root / ".venv"
        repo_root = workspace / "worktree"
        source_root = workspace / "venv_owner"
        candidate_root.mkdir()

        original_run = candidate_repo.subprocess.run

        def _intruding_probe(*_: object, **__: object) -> subprocess.CompletedProcess[str]:
            return subprocess.CompletedProcess(
                args=(),
                returncode=0,
                stdout=json.dumps(
                    {
                        "prefix": str(candidate_venv),
                        "path": [str(source_root / "ananta" / "src")],
                    }
                ),
                stderr="",
            )

        candidate_repo.subprocess.run = _intruding_probe
        try:
            candidate_repo._assert_venv_spelling(
                candidate_root, candidate_venv, repo_root, source_root
            )
            raise AssertionError("a candidate sys.path intruder was accepted")
        except candidate_repo.CandidateRepoError as exc:
            assert "candidate sys.path reaches the shared checkout" in str(exc), str(exc)
            assert str(source_root / "ananta" / "src") in str(exc), str(exc)
        finally:
            candidate_repo.subprocess.run = original_run
    return 1


def _check_provisioning_never_writes_the_shared_index() -> int:
    """Regression guard for iss_97f1764b: candidate git work stays in the candidate.

    A naive gitdir resolution turns candidate Git operations into LIVE INDEX
    MUTATIONS on another session's checkout.  The common-directory fix made
    that impossible by construction — ``--git-common-dir`` is used only as a
    read-only copytree source and every write target sits under
    ``candidate.root`` — but it proved that by measurement, and a measurement
    does not survive its author.

    The mutation this catches: a refactor that aims any write (an index
    rebuild, a checkout, a reset) at ``_git_common_dir(repo_root)`` instead of
    the candidate's own Git directory.  Both indexes are compared byte for
    byte, because the main checkout and the linked worktree keep separate ones
    and a write to either is the same class of damage.
    """

    with TemporaryDirectory() as temporary:
        workspace = Path(temporary).resolve()
        repo = workspace / "repo"
        repo.mkdir()
        base_ref = _build_fixture_repo(repo)

        linked = workspace / "linked"
        _git(repo, "worktree", "add", "-q", "-b", "lane/guard", str(linked), base_ref)
        assert (linked / ".git").is_file(), "the fixture worktree is not a linked one"
        _write(linked, _OVERLAY, "reviewed overlay bytes\n")

        common_dir = candidate_repo._git_common_dir(linked)
        main_index = common_dir / "index"
        worktree_index = Path(
            (linked / ".git").read_text(encoding="utf-8").split("gitdir:", 1)[1].strip()
        ) / "index"
        assert main_index.is_file(), f"fixture has no main index at {main_index}"
        assert worktree_index.is_file(), f"fixture has no worktree index at {worktree_index}"

        before = (main_index.read_bytes(), worktree_index.read_bytes())
        candidate_repo.build(
            linked,
            workspace / "candidate",
            (_OVERLAY,),
            renames=(),
            base_ref=base_ref,
            with_git=True,
        )
        after = (main_index.read_bytes(), worktree_index.read_bytes())

        assert before[0] == after[0], "provisioning WROTE the main checkout's index"
        assert before[1] == after[1], "provisioning WROTE the linked worktree's index"
    return 4


def _check_wave_candidate_never_rereads_source_overlay() -> int:
    """Composed integration preparation consumes supplied bytes, never live lane bytes."""
    with TemporaryDirectory() as temporary:
        workspace = Path(temporary).resolve()
        repo = workspace / "repo"
        repo.mkdir()
        base_ref = _build_fixture_repo(repo)
        _write(repo, _OVERLAY, "dirty source bytes that must not be read\n")
        candidate, proof = candidate_repo.prepare_wave_integration_candidate(
            repo,
            workspace / "candidate",
            (
                candidate_repo.FrozenEntry(
                    _OVERLAY, b"frozen manifest bytes\n", "100644"
                ),
            ),
            base_ref=base_ref,
            with_venv=False,
        )
        assert (candidate.root / _OVERLAY).read_bytes() == b"frozen manifest bytes\n"
        assert proof.head == _git(repo, "rev-parse", "HEAD").strip()
        assert (repo / _OVERLAY).read_bytes() == b"dirty source bytes that must not be read\n"
    return 3


def _check_head_detaches_at_base_ref_when_worktree_diverges() -> int:
    """NEGATIVE regression (iss_bd0f7e17).

    ``provision_private_git`` must label the candidate's HEAD with
    ``base_ref``, not the source worktree's own HEAD, whenever the two
    commits differ — the re-homed-candidate-at-master shape where a lane
    worktree is built against a ``base_ref`` ahead of its own current commit.
    Getting this wrong makes ``git diff --cached`` see the whole base-gap as
    staged (a superset of the reviewed scope) and fails any verifier that
    checks ``HEAD == base_ref`` on a tree that is actually correct.
    """

    with TemporaryDirectory() as temporary:
        workspace = Path(temporary).resolve()
        repo = workspace / "repo"
        repo.mkdir()
        _git(repo, "init", "-q", "-b", "main")
        _write(repo, _OVERLAY, "base overlay bytes\n")
        _git(repo, "add", "-A")
        _git(repo, "commit", "-qm", "base")
        base_ref = _git(repo, "rev-parse", "HEAD")

        linked = workspace / "linked"
        _git(repo, "worktree", "add", "-q", "-b", "lane/behind", str(linked), base_ref)
        assert (linked / ".git").is_file(), "the fixture worktree is not a linked one"

        # Master advances past the worktree's own HEAD after the worktree was
        # created — the exact re-homed-candidate-at-master shape this fix
        # targets: base_ref differs from the source worktree's current HEAD.
        _write(repo, "master-only.txt", "master moved on\n")
        _git(repo, "add", "master-only.txt")
        _git(repo, "commit", "-qm", "master advances")
        ahead_base_ref = _git(repo, "rev-parse", "HEAD")

        _write(linked, _OVERLAY, "reviewed overlay bytes\n")
        candidate, _after = candidate_repo.build(
            linked,
            workspace / "candidate",
            (_OVERLAY,),
            renames=(),
            base_ref=ahead_base_ref,
            with_git=True,
        )

        candidate_git_dir = candidate.root / ".git"
        environment = {
            **_fixture_env(),
            "GIT_DIR": str(candidate_git_dir),
            "GIT_WORK_TREE": str(candidate.root),
        }
        symbolic = subprocess.run(
            ("git", "symbolic-ref", "--quiet", "HEAD"),
            cwd=candidate.root,
            env=environment,
            check=False,
            capture_output=True,
        )
        assert symbolic.returncode != 0, (
            "candidate HEAD must be DETACHED at base_ref, not carry the "
            f"source worktree's own branch: {symbolic.stdout!r}"
        )
        head_commit = subprocess.run(
            ("git", "rev-parse", "HEAD"),
            cwd=candidate.root,
            env=environment,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        assert head_commit == ahead_base_ref, (head_commit, ahead_base_ref)

        staged = subprocess.run(
            ("git", "diff", "--cached", "--name-only"),
            cwd=candidate.root,
            env=environment,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.split()
        assert staged == [_OVERLAY], staged

        provenance = (
            candidate.manifest.parent / "candidate-provenance.txt"
        ).read_text(encoding="utf-8")
        assert "source_head_kind=ref" in provenance, provenance
        assert "source_head_value=refs/heads/lane/behind" in provenance, provenance
        assert f"base_commit={ahead_base_ref}" in provenance, provenance
    return 5


_LIVE_LOCKS = ("index.lock", "HEAD.lock", "refs/heads/main.lock", "config.lock")


def _write_transient_git_controls(repo: Path, git_dir: Path, base_ref: str) -> None:
    """Model the shared Git directory while a commit and merge are in flight."""
    for name in _LIVE_LOCKS:
        _write(repo, f".git/{name}", "held by another process\n")
    (git_dir / "index.lock").write_bytes(b"\0" * 1_988_551)
    _write(repo, ".git/MERGE_HEAD", f"{base_ref}\n")
    _write(repo, ".git/MERGE_MSG", "Merge in flight\n")
    (git_dir / "objects" / "tmp_obj_abc123").write_bytes(b"half-written")


def _assert_candidate_omits_locks(candidate_git_dir: Path) -> None:
    copied_locks = sorted(
        str(path.relative_to(candidate_git_dir))
        for path in candidate_git_dir.rglob("*.lock")
    )
    assert not copied_locks, f"the candidate inherited live lock files: {copied_locks}"


def _assert_candidate_omits_operation_markers(candidate_git_dir: Path) -> None:
    for name in ("MERGE_HEAD", "MERGE_MSG", "objects/tmp_obj_abc123"):
        assert not (candidate_git_dir / name).exists(), (
            f"the candidate inherited in-flight control state: {name}"
        )


def _assert_candidate_keeps_required_git_content(candidate_git_dir: Path) -> None:
    """The discriminator: only transient state is skipped from the copy."""
    for name in ("config", "HEAD", "refs/heads/main"):
        assert (candidate_git_dir / name).exists(), f"the copy dropped {name}"
    assert any((candidate_git_dir / "objects").iterdir()), "the copy dropped the objects"


def _assert_shared_transient_controls_remain(shared_git_dir: Path) -> None:
    """The repair copies around shared controls; it never releases them."""
    for name in _LIVE_LOCKS:
        assert (shared_git_dir / name).exists(), f"the shared {name} was removed"
    assert (shared_git_dir / "MERGE_HEAD").exists(), "the shared MERGE_HEAD was removed"


def _assert_candidate_can_commit(candidate_root: Path, candidate_git_dir: Path, base_ref: str) -> None:
    """A copied merge marker must not turn the candidate commit into a merge."""
    environment = {
        **_fixture_env(),
        "GIT_DIR": str(candidate_git_dir),
        "GIT_WORK_TREE": str(candidate_root),
    }
    subprocess.run(
        ("git", "commit", "-qm", "candidate commit"),
        cwd=candidate_root,
        env=environment,
        check=True,
        capture_output=True,
        timeout=60,
    )
    parents = subprocess.run(
        ("git", "rev-list", "--parents", "-n", "1", "HEAD"),
        cwd=candidate_root,
        env=environment,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.split()
    assert len(parents) == 2, f"candidate commit is not a single-parent commit: {parents}"
    assert parents[1] == base_ref, parents


def _assert_transient_gitdir_classifier_boundaries() -> None:
    """Check lock, operation-marker, and objects/tmp_* classifications."""
    root = Path(".")
    assert candidate_repo.is_transient_gitdir_entry(root, "index.lock")
    assert candidate_repo.is_transient_gitdir_entry(Path("refs/heads"), "main.lock")
    assert candidate_repo.is_transient_gitdir_entry(root, "MERGE_HEAD")
    assert candidate_repo.is_transient_gitdir_entry(Path("objects/pack"), "tmp_pack_x")
    assert not candidate_repo.is_transient_gitdir_entry(root, "index")
    assert not candidate_repo.is_transient_gitdir_entry(root, "packed-refs")
    assert not candidate_repo.is_transient_gitdir_entry(Path("objects/pack"), "pack-x.pack")
    # Operation markers mean nothing below the top level (a ref could carry
    # such a name); a `tmp_` prefix means nothing outside `objects/`.
    assert not candidate_repo.is_transient_gitdir_entry(Path("refs/heads"), "MERGE_HEAD")
    assert not candidate_repo.is_transient_gitdir_entry(Path("refs/heads"), "tmp_branch")


def _check_transient_control_files_are_not_copied() -> int:
    """NEGATIVE regression (iss_f99ee944) for shared in-flight Git state."""
    with TemporaryDirectory() as temporary:
        workspace = Path(temporary).resolve()
        repo = workspace / "repo"
        repo.mkdir()
        base_ref = _build_fixture_repo(repo)
        shared_git_dir = repo / ".git"
        _write_transient_git_controls(repo, shared_git_dir, base_ref)
        before = candidate_repo.measure_shared_repo(repo)

        candidate, after = candidate_repo.build(
            repo, workspace / "candidate", (_OVERLAY,), renames=(), base_ref=base_ref,
            with_git=True,
        )
        assert before == after, "the fixture repository changed during the build"
        candidate_git_dir = candidate.root / ".git"
        _assert_candidate_omits_locks(candidate_git_dir)
        _assert_candidate_omits_operation_markers(candidate_git_dir)
        _assert_candidate_keeps_required_git_content(candidate_git_dir)
        _assert_shared_transient_controls_remain(shared_git_dir)
        _assert_candidate_can_commit(candidate.root, candidate_git_dir, base_ref)

    _assert_transient_gitdir_classifier_boundaries()
    return 14


def main() -> int:
    check_count = _check_inherited_index_is_replaced()
    check_count += _check_linked_worktree_is_measurable()
    check_count += _check_index_free_git_dir_is_not_the_fix()
    check_count += _check_destination_must_be_physical()
    check_count += _check_candidate_venv_is_rehomed()
    check_count += _check_editable_pointer_lines_are_classified()
    check_count += _check_candidate_venv_rehomes_from_a_worktree()
    check_count += _check_rehoming_respects_path_boundaries()
    check_count += _check_pointer_guard_can_still_fail()
    check_count += _check_pointer_guard_accepts_rehomed_prefix_sibling()
    check_count += _check_venv_intruder_guard_can_still_fail()
    check_count += _check_provisioning_never_writes_the_shared_index()
    check_count += _check_wave_candidate_never_rereads_source_overlay()
    check_count += _check_shared_repo_drift_is_fatal()
    check_count += _check_head_detaches_at_base_ref_when_worktree_diverges()
    check_count += _check_transient_control_files_are_not_copied()
    print(f"candidate_repo_smoke OK: {check_count} checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
