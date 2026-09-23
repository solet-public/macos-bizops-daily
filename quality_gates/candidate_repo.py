#!/usr/bin/env python3
"""Build a private, self-contained candidate repository for gating a landing.

Why this exists
---------------

The mandatory pre-commit hook gates the WHOLE shared checkout.  When several
lanes share one working tree, a clean, fully reviewed change is blocked by
unrelated work-in-progress sitting beside it — guilt by proximity, not a real
finding.  The ratified answer is not to weaken the hook but to point it at the
tree that will actually exist after the commit: base ref + exactly the reviewed
paths, and nothing else.

This script builds that tree.  With ``--with-git`` it also gives the candidate
its own private Git directory, so a hook-enabled commit can run inside it.
``core.hooksPath`` in this repository is the RELATIVE value ``.githooks`` and
Git chdirs to the top of the working tree before running hooks, so a candidate
with ``GIT_WORK_TREE`` attached executes the CANDIDATE's own hook copy with the
candidate as its working directory.  Nothing is bypassed; the same gate runs
against a cleaner subject.

The inherited-index problem, and the file this script deletes
-------------------------------------------------------------

A private Git directory is seeded by copying the shared one, and that copy
carries the shared *index* with it.  The shared index legitimately holds staged
paths belonging to other lanes — in this checkout, a preserved 13-path
scheduler set.  A fidelity-correct candidate worktree deliberately EXCLUDES
that work, so index and worktree disagree the moment the copy lands, and
index-based gates report a census for a tree that is not there.

So ``.git/index`` is deleted, unconditionally, and rebuilt from the base ref
before anything reads it.  A bare deletion is not enough and is explicitly not
what happens here: an index-less Git directory reports every tracked file as
deleted, which changes fidelity in its own unmeasured way.  Delete, then
rebuild from the base, then add exactly the scope — that lands the index on the
same content the worktree already has.

Everything this script does to the shared repository is read-only.  It proves
that rather than asserting it: the shared staged-path count, the staged-diff
digest, the shared config and ``HEAD`` are measured before and after, and any
drift is a hard failure.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import NoReturn

_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from quality_gates.candidate_tree import (  # noqa: E402
    CandidateTree,
    CandidateTreeError,
    FrozenEntry,
    Rename,
    materialize_candidate_tree,
    materialize_frozen_entries,
    materialize_staged_tree,
    read_rename_file,
    read_scope_file,
)


class CandidateRepoError(RuntimeError):
    """An actionable failure while building or proving a candidate repository."""


@dataclass(frozen=True)
class SharedRepoProof:
    """The shared checkout's observable state, measured before and after."""

    head: str
    index_entries: int
    protected_paths: int
    staged_digest: str
    protected_digest: str
    config_digest: str

    def render(self) -> str:
        # Two different counts, never conflated: every tracked file the index
        # holds, and the far smaller set staged AGAINST HEAD -- the protected
        # surface a landing must leave alone.
        return (
            f"HEAD={self.head} index_entries={self.index_entries} "
            f"protected_paths={self.protected_paths} "
            f"staged_digest={self.staged_digest[:16]} "
            f"protected_digest={self.protected_digest[:16]} "
            f"config_digest={self.config_digest[:16]}"
        )


def _git(repo_root: Path, *arguments: str) -> bytes:
    result = subprocess.run(
        ("git", *arguments),
        cwd=repo_root,
        check=False,
        capture_output=True,
    )
    if result.returncode != 0:
        detail = result.stderr.decode(errors="replace").strip()
        raise CandidateRepoError(f"git {' '.join(arguments)} failed: {detail}")
    return result.stdout


def _digest(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _git_common_dir(repo_root: Path) -> Path:
    """The Git directory this checkout shares with every other worktree of it.

    ``repo_root/.git`` is a DIRECTORY only in a main checkout.  In a linked
    worktree it is a FILE holding a ``gitdir:`` pointer, so every path built by
    appending to it raises ``NotADirectoryError`` — which is exactly what this
    tool did the first time a lane ran it from its own worktree.  Git is asked
    where the common directory is rather than the layout being assumed, and a
    relative answer (``.git``, in a main checkout) is resolved against
    ``repo_root`` the way Git itself resolves it.
    """

    reported = Path(_git(repo_root, "rev-parse", "--git-common-dir").decode().strip())
    return reported if reported.is_absolute() else repo_root / reported


def _head_pointer(repo_root: Path) -> tuple[str, str]:
    """``repo_root``'s own HEAD, as a branch ref or a detached commit.

    The common Git directory carries the MAIN checkout's HEAD, so a candidate
    seeded from it would silently describe a different branch than the tree it
    was built from.  Asked and re-applied here instead of copied.
    """

    symbolic = subprocess.run(
        ("git", "symbolic-ref", "--quiet", "HEAD"),
        cwd=repo_root,
        check=False,
        capture_output=True,
    )
    if symbolic.returncode == 0:
        return "ref", symbolic.stdout.decode().strip()
    return "sha", _git(repo_root, "rev-parse", "HEAD").decode().strip()


def measure_shared_repo(repo_root: Path) -> SharedRepoProof:
    """Measure the shared checkout with read-only Git verbs only."""

    staged = _git(repo_root, "ls-files", "--stage", "-z")
    protected = _git(repo_root, "diff", "--cached", "--name-only", "--no-renames", "-z")
    config = (_git_common_dir(repo_root) / "config").read_bytes()
    return SharedRepoProof(
        head=_git(repo_root, "rev-parse", "HEAD").decode().strip(),
        index_entries=len([record for record in staged.split(b"\0") if record]),
        protected_paths=len([record for record in protected.split(b"\0") if record]),
        staged_digest=_digest(staged),
        protected_digest=_digest(protected),
        config_digest=_digest(config),
    )


def assert_shared_repo_untouched(before: SharedRepoProof, after: SharedRepoProof) -> None:
    if before == after:
        return
    raise CandidateRepoError(
        "the shared repository changed while building the candidate — "
        f"before: {before.render()} after: {after.render()}"
    )


def _assert_physical(path: Path, *, label: str) -> Path:
    """Return ``path`` proven to be its own realpath.

    On this host ``/tmp`` is a symlink to ``private/tmp``, so a candidate built
    under a ``/tmp/...`` spelling has two names for every directory in it.
    Runtime assertions cannot see that — CPython resolves symlinks, so
    ``sys.prefix`` and ``__file__`` report the physical spelling while injected
    pointer files keep the logical one, and a static analyser that canonicalises
    paths differently then sees two package roots.  The property that matters is
    spelling CONSISTENCY, so it is asserted here rather than inferred later.
    """

    resolved = path.resolve()
    if str(path) != str(resolved):
        raise CandidateRepoError(
            f"{label} must be its own physical path: {path} resolves to {resolved}"
        )
    return resolved


# Control files a Git process leaves in the shared directory only WHILE it
# works, and which a copy must therefore never carry (iss_f99ee944).  Every
# name is a fact about the shared repository's in-flight operation, not about
# its content: a copied `index.lock` (~2MB measured during a live commit) makes
# the candidate's own `read-tree` stop on a lock nothing will ever release,
# a copied `HEAD.lock`/`refs/**.lock` does the same to `symbolic-ref`, and a
# copied `MERGE_HEAD` turns the candidate's next commit into a MERGE with a
# second parent it never reviewed.  Enumerated as a class, not at the one site
# the ladder hit.
_TRANSIENT_GITDIR_SUFFIX = ".lock"
_TRANSIENT_GITDIR_TOP_LEVEL = frozenset({
    "MERGE_HEAD", "MERGE_MSG", "MERGE_MODE", "MERGE_RR", "MERGE_AUTOSTASH",
    "AUTO_MERGE", "CHERRY_PICK_HEAD", "REVERT_HEAD", "REBASE_HEAD", "SQUASH_MSG",
    "BISECT_LOG", "BISECT_START", "BISECT_TERMS", "BISECT_EXPECT", "BISECT_RUN",
    "BISECT_ANCESTORS_OK", "BISECT_NAMES",
    "sequencer", "rebase-merge", "rebase-apply",
    "gc.pid",
})
_TRANSIENT_OBJECTS_PREFIX = "tmp_"


def is_transient_gitdir_entry(relative_directory: Path, name: str) -> bool:
    """Whether ``name`` inside ``relative_directory`` of a Git directory is
    in-flight control state that a candidate copy must skip.

    ``relative_directory`` is the entry's parent relative to the Git directory
    (``Path(".")`` at the top level).  Lock files are transient at ANY depth;
    the in-progress-operation markers only mean that at the top level; under
    ``objects/`` only the ``tmp_*`` spellings Git uses for a half-written
    loose object or pack are transient.
    """

    if name.endswith(_TRANSIENT_GITDIR_SUFFIX):
        return True
    parts = relative_directory.parts
    if not parts:
        return name in _TRANSIENT_GITDIR_TOP_LEVEL
    return parts[0] == "objects" and name.startswith(_TRANSIENT_OBJECTS_PREFIX)


def provision_private_git(
    repo_root: Path, candidate: CandidateTree, *, base_ref: str, scope: tuple[str, ...]
) -> Path:
    """Give the candidate its own Git directory, coherent with its own worktree.

    Returns the candidate's ``.git`` path.  The inherited index is deleted and
    rebuilt from ``base_ref``; see this module's docstring for why a bare
    deletion is not the fix.
    """

    git_dir = candidate.root / ".git"
    source_git_dir = _git_common_dir(repo_root)

    # The linked-worktree admin directories are deliberately left behind: each
    # holds a `gitdir` pointer naming a path OUTSIDE the candidate, which is
    # the same escape this function already refuses for alternates and
    # core.worktree.  A candidate drives its own tree through GIT_DIR and
    # GIT_WORK_TREE and never consults them.  Transient control files are
    # left behind for a different reason: they describe an operation IN
    # FLIGHT in the shared repository (a Git-Controller commit racing this
    # materialization), and copying one gives the candidate a lock or a merge
    # state nothing inside it will ever release.  The shared files themselves
    # are never touched — skipping the copy is the whole fix.
    skipped_transient: list[str] = []

    def _skip_admin_and_transient(directory: str, names: list[str]) -> set[str]:
        relative_directory = Path(directory).relative_to(source_git_dir)
        skipped = {
            name for name in names
            if is_transient_gitdir_entry(relative_directory, name)
        }
        skipped_transient.extend(
            str(relative_directory / name) for name in sorted(skipped)
        )
        if not relative_directory.parts:
            skipped |= {name for name in names if name == "worktrees"}
        return skipped

    shutil.copytree(
        source_git_dir, git_dir, symlinks=True, ignore=_skip_admin_and_transient
    )
    if skipped_transient:
        print(
            "candidate_repo: shared Git directory is mid-operation; "
            f"{len(skipped_transient)} transient control file(s) not copied "
            f"into the candidate: {', '.join(skipped_transient)}",
            file=sys.stderr,
        )

    inherited_index = git_dir / "index"
    if inherited_index.exists():
        inherited_index.unlink()
    if inherited_index.exists():
        raise CandidateRepoError("the inherited index survived deletion")

    alternates = git_dir / "objects" / "info" / "alternates"
    if alternates.exists():
        raise CandidateRepoError(
            "the candidate Git directory carries an alternates file pointing outside itself"
        )

    environment = {
        **os.environ,
        "GIT_DIR": str(git_dir),
        "GIT_WORK_TREE": str(candidate.root),
    }

    def candidate_git(*arguments: str) -> bytes:
        result = subprocess.run(
            ("git", *arguments),
            cwd=candidate.root,
            env=environment,
            check=False,
            capture_output=True,
        )
        if result.returncode != 0:
            detail = result.stderr.decode(errors="replace").strip()
            raise CandidateRepoError(f"candidate git {' '.join(arguments)} failed: {detail}")
        return result.stdout

    absolute_git_dir = candidate_git("rev-parse", "--absolute-git-dir").decode().strip()
    if Path(absolute_git_dir).resolve() != git_dir.resolve():
        raise CandidateRepoError(
            f"candidate git dir resolved outside the candidate: {absolute_git_dir}"
        )
    worktree_setting = subprocess.run(
        ("git", "config", "--get", "core.worktree"),
        cwd=candidate.root,
        env=environment,
        check=False,
        capture_output=True,
    )
    if worktree_setting.returncode == 0 and worktree_setting.stdout.strip():
        raise CandidateRepoError(
            "the candidate Git directory pins core.worktree and would escape the candidate"
        )

    # The copy carries the COMMON directory's HEAD, which in a linked worktree
    # belongs to the main checkout, so it is re-pointed at this tree's own —
    # UNLESS the tree's own HEAD is not what the candidate's content was built
    # from. `materialize_candidate_tree` reads its blobs from `base_ref`, not
    # from `repo_root`'s current HEAD, and the two commonly differ (the
    # re-homed-candidate-at-master rule builds from a `base_ref` ahead of a
    # behind-master lane worktree's own HEAD). Re-applying the source HEAD in
    # that case mislabels the candidate: `git diff --cached` against that HEAD
    # would see the whole base-gap as staged, and a verifier checking
    # `HEAD == base_ref` fails on a tree that is actually correct
    # (iss_bd0f7e17). The candidate's HEAD must describe the content it
    # actually holds.
    source_head_kind, source_head_value = _head_pointer(repo_root)
    source_head_commit = _git(repo_root, "rev-parse", "HEAD").decode().strip()
    base_commit = _git(repo_root, "rev-parse", f"{base_ref}^{{commit}}").decode().strip()
    if base_commit == source_head_commit:
        if source_head_kind == "ref":
            candidate_git("symbolic-ref", "HEAD", source_head_value)
        else:
            candidate_git("update-ref", "--no-deref", "HEAD", source_head_value)
    else:
        candidate_git("update-ref", "--no-deref", "HEAD", base_commit)
        _write_provenance(
            candidate,
            source_head_kind=source_head_kind,
            source_head_value=source_head_value,
            base_ref=base_ref,
            base_commit=base_commit,
        )

    # Delete-then-rebuild: the index now describes the base, and adding exactly
    # the scope lands it on the content the worktree already carries.
    candidate_git("read-tree", base_ref)
    candidate_git("add", "--", *scope)
    return git_dir


def _write_provenance(
    candidate: CandidateTree,
    *,
    source_head_kind: str,
    source_head_value: str,
    base_ref: str,
    base_commit: str,
) -> None:
    """Record the source tree's own HEAD beside a detached-at-base_ref candidate.

    The candidate's Git HEAD is deliberately `base_ref`, not the source
    worktree's branch, once the two diverge — see `provision_private_git`.
    This sibling file (outside `candidate.root`, so it is never itself
    candidate content or part of the manifest) keeps the source branch
    discoverable for anyone auditing the candidate afterward.
    """

    provenance = candidate.manifest.parent / "candidate-provenance.txt"
    provenance.write_text(
        f"source_head_kind={source_head_kind}\n"
        f"source_head_value={source_head_value}\n"
        f"base_ref={base_ref}\n"
        f"base_commit={base_commit}\n",
        encoding="utf-8",
    )


def _site_packages(venv: Path) -> Path:
    matches = sorted(venv.glob("lib/python3.*/site-packages"))
    if len(matches) != 1:
        raise CandidateRepoError(
            f"expected exactly one site-packages under {venv}, found {len(matches)}"
        )
    return matches[0]


def _interpreter_version(interpreter: Path) -> str:
    result = subprocess.run(
        (str(interpreter), "-c", "import sys; print('.'.join(map(str, sys.version_info[:3])))"),
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise CandidateRepoError(f"{interpreter} did not report a version: {result.stderr.strip()}")
    return result.stdout.strip()


def _shared_interpreter(venv: Path) -> Path:
    """The shared venv's OWN interpreter, which is the only reliable base.

    `pyvenv.cfg`'s `executable` field records a versioned Homebrew Cellar path
    that a patch upgrade deletes; the venv keeps working because `base_prefix`
    reaches the interpreter through the stable `opt` symlink instead.  Reading
    that field therefore fails exactly when the toolchain has been updated, so
    the venv's own `python3` is used and the resulting child is checked for
    version parity rather than assumed to have it.
    """

    interpreter = venv / "bin" / "python3"
    if not interpreter.exists():
        raise CandidateRepoError(f"shared virtualenv has no interpreter at {interpreter}")
    return interpreter


def _root_pattern(root: Path) -> re.Pattern[str]:
    """Match ``root`` only where it is a real path PREFIX, not a substring."""

    return re.compile(rf"{re.escape(str(root))}(?=/|$)", re.MULTILINE)


def _rehome(payload: str, source_root: Path, candidate_root: Path) -> str:
    """Re-home on PATH BOUNDARIES, never as a bare substring.

    A lane worktree is a SIBLING of the checkout that owns the shared venv and
    its directory name begins with the same characters, so `/…/base` is a prefix
    SUBSTRING of `/…/base_lane_worktrees/…` without ever being a parent of it.
    A bare `str.replace` therefore rewrites paths that were never under
    `source_root` at all — silently, because every guard in this module is a
    containment test that such a corrupted path still satisfies.  Only a match
    ending at a separator or a line end is real containment.
    """

    return _root_pattern(source_root).sub(lambda _: str(candidate_root), payload)


def _venv_source_root(shared_venv: Path, repo_root: Path) -> Path:
    """The checkout whose paths the shared venv's pointers actually name.

    `repo_root` is the WRONG key for re-homing.  A lane worktree reaches the
    shared virtualenv through a `.venv` symlink, so the editable pointers inside
    it carry absolute paths into the checkout that OWNS that venv — the main
    one — and never mention the worktree.  Keying on `repo_root` makes every
    replacement a no-op and every guard blind, and the candidate then imports
    shared-checkout source while reporting success.

    Resolving the venv identifies that owner directly.  In a main checkout the
    venv is not a symlink, so this returns `repo_root` unchanged and the
    established path keeps its exact behaviour.
    """

    owner = shared_venv.resolve().parent
    if owner == repo_root:
        return owner
    if not (owner / ".venv").is_dir():
        raise CandidateRepoError(
            f"shared virtualenv at {shared_venv} resolves outside any checkout: {owner}"
        )
    return owner


def _validate_editable_pointers(payload: str, candidate_root: Path, entry_name: str) -> None:
    """Require every non-blank editable-pth line to be a contained absolute path."""
    candidate_resolved = candidate_root.resolve()
    for pointer in payload.splitlines():
        if not pointer.strip():
            continue
        if not Path(pointer).is_absolute():
            raise CandidateRepoError(
                f"editable pointer {pointer!r} is not an absolute path in {entry_name}"
            )
        if not Path(pointer).resolve().is_relative_to(candidate_resolved):
            raise CandidateRepoError(
                f"editable pointer {pointer!r} does not resolve inside the candidate root "
                f"{candidate_root}: {entry_name}"
            )


def _install_site_packages(
    shared: Path, candidate: Path, source_root: Path, candidate_root: Path
) -> int:
    """Share third-party libraries, but re-home every editable pointer.

    An editable install's pointer file carries an ABSOLUTE path into the shared
    checkout.  Symlinking the shared venv (or copying the pointer unchanged)
    leaves that pointer resolving to the shared tree while the candidate's own
    copy of the same package sits at the candidate root — one module, two
    directories, so the same class loads as two distinct types and a static
    analyser reports that a type is not assignable to itself.
    """

    rehomed = 0
    for entry in sorted(shared.iterdir()):
        if entry.name == "__pycache__":
            continue
        destination = candidate / entry.name
        if destination.exists() or destination.is_symlink():
            continue
        if entry.suffix == ".pth" and entry.name.startswith("__editable__"):
            payload = _rehome(entry.read_text(encoding="utf-8"), source_root, candidate_root)
            _validate_editable_pointers(payload, candidate_root, entry.name)
            destination.write_text(payload, encoding="utf-8")
            rehomed += 1
            continue
        destination.symlink_to(entry)
    return rehomed


def _install_console_scripts(
    shared_venv: Path, candidate_venv: Path, source_root: Path, candidate_root: Path
) -> None:
    """Symlink native binaries; COPY text scripts with their shebang re-homed.

    `ruff` is a Mach-O binary and must be symlinked.  `pyright` and `radon` are
    text scripts whose shebang is an absolute path into the SHARED venv, so a
    symlink would run the shared interpreter and undo the whole construction.
    """

    for entry in sorted((shared_venv / "bin").iterdir()):
        if entry.name.startswith("python"):
            continue  # the real venv owns its own interpreters
        destination = candidate_venv / "bin" / entry.name
        if destination.exists() or destination.is_symlink():
            continue
        try:
            payload = entry.read_text(encoding="utf-8")
        except (UnicodeError, OSError):
            destination.symlink_to(entry)
            continue
        if not payload.startswith("#!"):
            destination.symlink_to(entry)
            continue
        destination.write_text(_rehome(payload, source_root, candidate_root), encoding="utf-8")
        destination.chmod(0o755)


def _shared_root_intruders(paths: list[str], roots: set[str]) -> list[str]:
    """Return interpreter paths that still reach either shared checkout."""

    return [
        entry for entry in paths if any(entry.startswith(f"{root}/") for root in roots)
    ]


def _assert_venv_spelling(
    candidate_root: Path, candidate_venv: Path, repo_root: Path, source_root: Path
) -> None:
    """Assert SPELLING consistency, which runtime views cannot see.

    CPython resolves symlinks, so `sys.prefix` and `__file__` report the
    physical path and every naive runtime assertion passes even while injected
    pointer files carry a second spelling.  The failing tool is a static
    analyser with its own canonicalisation, so the property to assert is that
    every path agrees with its own realpath — not that the interpreter is happy.
    """

    probe = (
        "import json,sys,site\n"
        "print(json.dumps({'prefix': sys.prefix, 'path': [p for p in sys.path if p]}))\n"
    )
    result = subprocess.run(
        (str(candidate_venv / "bin" / "python3"), "-c", probe),
        cwd=candidate_root,
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise CandidateRepoError(f"candidate interpreter failed to start: {result.stderr.strip()}")
    view = json.loads(result.stdout)

    if Path(view["prefix"]).resolve() != candidate_venv.resolve():
        raise CandidateRepoError(f"candidate sys.prefix escaped the candidate: {view['prefix']}")
    # BOTH checkouts are intruders, and from a worktree they are different
    # directories.  Checking only `repo_root` is what let an under-homed
    # candidate pass: the pointers named the venv's owner, which a worktree run
    # never mentions, so the one path that could reach out went unexamined.
    outsiders = {str(repo_root), str(source_root)}
    intruders = _shared_root_intruders(view["path"], outsiders)
    if intruders:
        raise CandidateRepoError(f"candidate sys.path reaches the shared checkout: {intruders}")
    unstable = [
        entry
        for entry in view["path"]
        if Path(entry).exists() and str(Path(entry).resolve()) != entry
    ]
    if unstable:
        raise CandidateRepoError(
            f"candidate sys.path entries disagree with their own realpath: {unstable}"
        )


def provision_candidate_venv(repo_root: Path, candidate_root: Path) -> Path:
    """Give the candidate a REAL virtualenv co-located with its own tree.

    This reproduces the post-merge topology — venv beside checkout — so it
    raises fidelity rather than lowering a bar.  Symlinking the shared venv, or
    symlinking its `bin` directory, both fail: CPython resolves `sys.prefix`
    back through a symlinked `bin` to the shared venv, so re-homed pointers are
    never loaded at all.  A venv's identity lives in the `pyvenv.cfg` beside the
    interpreter that was actually invoked.
    """

    shared_venv = repo_root / ".venv"
    if not shared_venv.is_dir():
        raise CandidateRepoError(f"no shared virtualenv to mirror at {shared_venv}")
    candidate_venv = candidate_root / ".venv"

    shared_interpreter = _shared_interpreter(shared_venv)
    result = subprocess.run(
        (str(shared_interpreter), "-m", "venv", "--without-pip", str(candidate_venv)),
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise CandidateRepoError(f"candidate venv creation failed: {result.stderr.strip()}")

    shared_version = _interpreter_version(shared_interpreter)
    candidate_version = _interpreter_version(candidate_venv / "bin" / "python3")
    if shared_version != candidate_version:
        raise CandidateRepoError(
            f"candidate interpreter {candidate_version} does not match "
            f"the shared interpreter {shared_version}"
        )

    source_root = _venv_source_root(shared_venv, repo_root)
    rehomed = _install_site_packages(
        _site_packages(shared_venv), _site_packages(candidate_venv), source_root, candidate_root
    )
    if not rehomed:
        raise CandidateRepoError(
            "no editable pointers were re-homed; the shared venv's layout is not what this expects"
        )
    _install_console_scripts(shared_venv, candidate_venv, source_root, candidate_root)
    _assert_venv_spelling(candidate_root, candidate_venv, repo_root, source_root)
    return candidate_venv


def prepare_wave_integration_candidate(
    repo_root: Path,
    destination: Path,
    entries: tuple[FrozenEntry, ...],
    *,
    base_ref: str,
    with_venv: bool = True,
) -> tuple[CandidateTree, SharedRepoProof]:
    """Prepare a composed wave candidate without rereading any source lane.

    The caller supplies final bytes already frozen by ``landing_wave``.  This
    preserves the candidate repository's private-venv/origin proof while
    keeping source overlay acquisition out of an integration path.
    """
    root = _assert_physical(repo_root.resolve(), label="repository root")
    out = _assert_physical(destination, label="candidate destination")
    if out.exists() and any(out.iterdir()):
        raise CandidateRepoError(f"candidate destination is not empty: {out}")
    out.mkdir(parents=True, exist_ok=True)
    before = measure_shared_repo(root)
    candidate = materialize_frozen_entries(root, out, entries, base_ref=base_ref)
    if with_venv:
        provision_candidate_venv(root, candidate.root)
    after = measure_shared_repo(root)
    assert_shared_repo_untouched(before, after)
    return candidate, after


def build(
    repo_root: Path,
    destination: Path,
    scope: tuple[str, ...],
    *,
    renames: tuple[Rename, ...],
    base_ref: str,
    with_git: bool,
    with_venv: bool = False,
    staged: bool = False,
) -> tuple[CandidateTree, SharedRepoProof]:
    root = _assert_physical(repo_root.resolve(), label="repository root")
    out = _assert_physical(destination, label="candidate destination")
    if out.exists() and any(out.iterdir()):
        raise CandidateRepoError(f"candidate destination is not empty: {out}")
    out.mkdir(parents=True, exist_ok=True)

    before = measure_shared_repo(root)
    if staged:
        if renames:
            raise CandidateRepoError("a staged candidate takes its content from the index, not renames")
        candidate = materialize_staged_tree(root, out, scope, base_ref=base_ref)
    else:
        candidate = materialize_candidate_tree(
            root, out, scope, renames=renames, base_ref=base_ref
        )
    if with_git:
        provision_private_git(root, candidate, base_ref=base_ref, scope=scope)
    if with_venv:
        provision_candidate_venv(root, candidate.root)
    after = measure_shared_repo(root)
    assert_shared_repo_untouched(before, after)
    return candidate, after


def _fail(message: str) -> NoReturn:
    print(f"candidate_repo STOP: {message}", file=sys.stderr)
    raise SystemExit(70)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build a private candidate repository.")
    parser.add_argument("--repo-root", type=Path, default=Path.cwd())
    parser.add_argument("--base-ref", required=True)
    parser.add_argument("--scope-file", type=Path, required=True)
    parser.add_argument("--rename-file", type=Path)
    parser.add_argument(
        "--out",
        type=Path,
        required=True,
        help="candidate destination; must already be its own physical path",
    )
    parser.add_argument(
        "--with-git",
        action="store_true",
        help="provision a private Git directory whose index is rebuilt from the base ref",
    )
    parser.add_argument(
        "--with-venv",
        action="store_true",
        help="provision a candidate-local virtualenv so the full battery can run inside the tree",
    )
    parser.add_argument(
        "--staged",
        action="store_true",
        help="take scoped content from the INDEX, not the working tree",
    )
    args = parser.parse_args(argv)

    try:
        scope = read_scope_file(args.scope_file)
        renames = () if args.rename_file is None else read_rename_file(args.rename_file)
        candidate, proof = build(
            args.repo_root,
            args.out,
            scope,
            renames=renames,
            base_ref=args.base_ref,
            with_git=args.with_git,
            with_venv=args.with_venv,
            staged=args.staged,
        )
    except (CandidateRepoError, CandidateTreeError) as exc:
        _fail(str(exc))

    print(f"candidate root    : {candidate.root}")
    print(f"candidate manifest: {candidate.manifest}")
    print(f"candidate entries : {len(candidate.paths)}")
    print(f"scope applied     : {len(scope)} ({'staged index' if args.staged else 'working tree'})")
    print(f"private git       : {'yes' if args.with_git else 'no'}")
    print(f"candidate venv    : {'yes' if args.with_venv else 'no'}")
    print(f"shared repo proof : {proof.render()}")
    print("shared repository verified byte-unchanged across the build")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
