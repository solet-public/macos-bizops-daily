#!/usr/bin/env python3
"""Shared scope rules for the quality gates: what counts as ours to measure.

Every gate has to answer the same question before it measures anything —
*which files are this repository's?* — and until 2026-08-16 four of them
answered it four times, by walking the filesystem. That walk reaches vendored
dependency trees: `plugins/cosyvoice2_tts_plugin/src/.venv_cosyvoice` holds
18,321 `.py` files against that plugin's 15 tracked ones. A bare gate run over
that plugin reported sympy's C-grade functions as this repo's findings, and
then died with a `RecursionError` inside radon's AST walk.

**Being IGNORED is the property that distinguishes vendored code from ours.**
Name-based pruning (`.venv*`) is kept as cheap defence in depth, but it is a
name check: `node_modules`, a plain `vendor/`, or a stray site-packages copy
walks straight through it. `.gitignore` does not care what a directory is
called.

The predicate is `--cached --others --exclude-standard`: the index, PLUS
untracked files that are not ignored. Both halves are load-bearing, and the
second half was learned the hard way.

**Do NOT narrow this to `--cached` alone.** Tracked-only was the first version
of this module and it caused a FALSE GREEN within the hour: a brand-new,
never-added smoke carrying a CC-12 function was invisible to the aggregate,
which printed "radon_cc gate clean" while a scoped run of the same gate
reported the violation. A new file is exactly the file most likely to carry a
new defect, and "not yet `git add`ed" is a statement about a developer's
workflow, not about whether the code is ours. Measured on this checkout:
tracked-only resolved 2,132 paths and dropped 3 untracked files; the index-plus
-unignored predicate resolves 2,135 and still admits zero vendored files.

This module exists because the alternative was three byte-identical copies of
that rule, and a load-bearing filter with three copies is how one copy rots.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

# Exit code the gate wrappers return when the analyser raised and no verdict
# exists. Deliberately not the blocking code: a crash is not a verdict, and
# rendering it as a violation count tells the reader they have debt to fix
# when nothing was measured at all.
GATE_CRASH_EXIT = 70

# Directory-name prefix for bundled virtualenvs. Defence in depth only — see
# the module docstring for why the tracked check is the load-bearing one.
BUNDLED_VENV_PREFIX = ".venv"


class GateCrashError(Exception):
    """A gate could not produce a verdict.

    Categorically different from a finding. A finding says "this code is too
    complex" or "this class does too much"; a crash says the measurement never
    happened, so the gate has nothing to say — about that file, or about
    anything after it in an aborted scan. Callers must render it as a crash and
    never fold it into a violation count.
    """

    def __init__(self, path: Path, detail: str) -> None:
        super().__init__(f"{path}: {detail}")
        self.path = path
        self.detail = detail


def _git_ls_files(pathspec: Path, cwd: Path) -> list[str]:
    """Run `git ls-files` for in-repo paths and return its NUL entries.

    Fast-fails rather than degrading: a scope query that silently fell back to
    a filesystem walk would re-admit the vendored trees this module exists to
    exclude, and would do it invisibly.
    """
    try:
        result = subprocess.run(
            ["git", "ls-files", "-z", "--cached", "--others",
             "--exclude-standard", "--", str(pathspec)],
            cwd=str(cwd), capture_output=True, text=True, timeout=60, check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise GateCrashError(pathspec, f"cannot list git-tracked files: {exc}") from exc
    if result.returncode != 0:
        raise GateCrashError(
            pathspec,
            f"git ls-files failed (exit {result.returncode}): "
            f"{result.stderr.strip() or '(no stderr)'}",
        )
    return [entry for entry in result.stdout.split("\0") if entry]


def _under_bundled_venv(path: Path) -> bool:
    """True when any path component is a bundled-venv directory (name check)."""
    return any(part.startswith(BUNDLED_VENV_PREFIX) for part in path.parts)


def repo_python_files(directory: Path) -> list[Path]:
    """Every in-repo `.py` file under `directory`, ignored trees excluded.

    "In-repo" = tracked or untracked-but-not-ignored. Used by the gate wrappers
    to expand a DIRECTORY argument. A file the caller names explicitly is their
    business and is not filtered — see each wrapper's `_expand_targets`.
    """
    resolved = directory.resolve()
    files: list[Path] = []
    for rel in _git_ls_files(resolved, cwd=resolved):
        if not rel.endswith(".py"):
            continue
        path = resolved / rel
        if _under_bundled_venv(path):
            continue
        files.append(path)
    return sorted(files)


def repo_files(repo_root: Path) -> frozenset[Path]:
    """Every in-repo path in the checkout, as resolved absolute paths.

    The aggregate intersects its declared quality-surface roots with this set,
    rather than replacing them: the roots encode the operator's ruling about
    what is in scope, and this encodes what is ours rather than vendored.

    Applies the same bundled-venv name prune as `repo_python_files`, so the two
    scope functions agree on what "ours" means (iss_c795a79b): before 2026-09-20
    only the per-directory expansion pruned by name and this whole-checkout set
    relied on the ignore filter alone.
    """
    return frozenset(
        path
        for path in (
            (repo_root / rel).resolve()
            for rel in _git_ls_files(repo_root, cwd=repo_root)
        )
        if not _under_bundled_venv(path)
    )
