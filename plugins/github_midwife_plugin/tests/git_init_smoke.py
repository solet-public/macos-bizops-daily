"""Workstream A smoke (2026-07-20) — git-init of the born solet worktree.

Drives ``git_init.git_init_worktree`` against a throwaway tmp tree with REAL
git (no mocking — the git subprocesses ARE the substrate under test), and pins:

  * a fresh born tree becomes a real git worktree — ``git rev-parse
    --is-inside-work-tree`` returns ``true`` (the exact probe
    ``platform_dev_surface_plugin`` readiness runs; the bug this fixes);
  * FRESH empty history — exactly ONE commit, on branch ``main``, with the
    born-tree ``.gitignore`` present;
  * a LOCAL git identity is set (repo config, never ``--global``);
  * gitignored runtime/secret paths are NOT committed while source IS
    (``.gitignore`` keeps ``profile/config/manifest.yaml`` + ``.venv`` out);
  * idempotent: a second call on an already-``.git`` tree is a no-op
    (``status="skipped"``), and an existing ``.gitignore`` is preserved.

The tmp tree is created under the OS temp dir (outside the fleet worktree), so
``git init`` there never touches the shared repository.

Run directly: ``.venv/bin/python3 plugins/github_midwife_plugin/tests/git_init_smoke.py``.
"""

from __future__ import annotations

# ruff: noqa: E402
import subprocess
import sys
import tempfile
from pathlib import Path

_PLUGIN_ROOT = Path(__file__).resolve().parents[1]
_SRC = _PLUGIN_ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from github_midwife_plugin.git_init import git_init_worktree

_CHECKS_RUN: list[str] = []


class SmokeFailureError(AssertionError):
    """Raised on any check failure; message is the failure detail."""


def _check(label: str, condition: bool, detail: str = "") -> None:
    _CHECKS_RUN.append(label)
    if not condition:
        raise SmokeFailureError(f"{label}: {detail}")


def _git_out(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, timeout=60,
    )
    return result.stdout.strip() if result.returncode == 0 else ""


def _make_born_tree(root: Path) -> Path:
    """A minimal seed-born tree: some source, a materialized (gitignore-able)
    config + a stub .venv, and NO .git (a fresh clone of a published seed)."""
    clone = root / "clone"
    (clone / "ananta" / "src").mkdir(parents=True)
    (clone / "ananta" / "src" / "core.py").write_text("X = 1\n", encoding="utf-8")
    (clone / "root_manifest.yaml").write_text("schema_version: 1\n", encoding="utf-8")
    # gitignore-able runtime/secret + venv — must NOT land in the initial commit.
    (clone / "profile" / "config").mkdir(parents=True)
    (clone / "profile" / "config" / "manifest.yaml").write_text("regenerated: true\n", encoding="utf-8")
    (clone / ".venv" / "bin").mkdir(parents=True)
    (clone / ".venv" / "bin" / "python3").write_text("#!/bin/sh\n", encoding="utf-8")
    return clone


def _check_fresh_init(root: Path) -> None:
    clone = _make_born_tree(root)
    record = git_init_worktree(clone, "testhum")

    _check("git_init reports completed on a fresh tree", record["status"] == "completed", str(record))
    _check("git_init wrote the .gitignore", record.get("gitignore_written") is True, str(record))
    _check("the born tree carries a .gitignore file", (clone / ".gitignore").is_file(), str(clone))

    _check(
        "the born tree is now a real git worktree (the platform_dev_surface readiness probe)",
        _git_out(clone, "rev-parse", "--is-inside-work-tree") == "true",
        "git rev-parse --is-inside-work-tree did not return true",
    )
    _check("the born tree is on branch main", _git_out(clone, "rev-parse", "--abbrev-ref", "HEAD") == "main",
           _git_out(clone, "rev-parse", "--abbrev-ref", "HEAD"))
    commit_count = _git_out(clone, "rev-list", "--count", "HEAD")
    _check("fresh empty history — exactly one initial commit", commit_count == "1", commit_count)

    # LOCAL identity (repo config), never --global.
    _check("a local git user.name was set", _git_out(clone, "config", "--local", "user.name") == "testhum",
           _git_out(clone, "config", "--local", "user.name"))
    _check("a local git user.email was set", _git_out(clone, "config", "--local", "user.email") == "testhum@localhost",
           _git_out(clone, "config", "--local", "user.email"))

    # gitignore keeps runtime/secret + venv out; source is tracked.
    tracked = set(_git_out(clone, "ls-files").splitlines())
    _check("source is tracked in the initial commit", "ananta/src/core.py" in tracked, str(sorted(tracked)))
    _check("the .gitignore itself is tracked", ".gitignore" in tracked, str(sorted(tracked)))
    _check(
        "the per-launch regenerated manifest.yaml is NOT committed (gitignored)",
        "profile/config/manifest.yaml" not in tracked,
        str(sorted(tracked)),
    )
    _check(
        "the .venv is NOT committed (gitignored)",
        not any(t.startswith(".venv/") for t in tracked),
        str(sorted(tracked)),
    )
    # A clean worktree (nothing staged/untracked outside .gitignore).
    _check("the worktree is clean after the initial commit", _git_out(clone, "status", "--porcelain") == "",
           _git_out(clone, "status", "--porcelain"))


def _check_idempotent(root: Path) -> None:
    clone = _make_born_tree(root)
    first = git_init_worktree(clone, "testhum")
    _check("first init completes", first["status"] == "completed", str(first))
    head_before = _git_out(clone, "rev-parse", "HEAD")
    # A stray edit to the (existing) .gitignore must be preserved by a re-run.
    (clone / ".gitignore").write_text("# operator edit\n", encoding="utf-8")

    second = git_init_worktree(clone, "testhum")
    _check("a second init on an existing worktree is skipped (idempotent)", second["status"] == "skipped", str(second))
    _check("the re-run left HEAD unchanged", _git_out(clone, "rev-parse", "HEAD") == head_before, "HEAD moved on re-run")
    _check(
        "the re-run preserved the operator's existing .gitignore (never clobbered)",
        (clone / ".gitignore").read_text(encoding="utf-8") == "# operator edit\n",
        (clone / ".gitignore").read_text(encoding="utf-8"),
    )


def _check_cloned_seed_gets_a_gitignore(root: Path) -> None:
    """The ADOPTER case: ``.git`` PRESENT, ``.gitignore`` ABSENT.

    The seed is distributed as a GitHub repo, so every adopter's clone arrives
    already a worktree. ``_check_idempotent`` cannot see this: it writes a
    ``.gitignore`` before its re-run, so it only ever exercises the clobber-guard
    and the one path an adopter actually takes goes untested. Without a
    ``.gitignore`` a born clone shows every ``__pycache__`` and its whole ``.venv``
    as untracked, and an operator can commit runtime state and secrets that the
    ignore list exists to keep out.
    """
    clone = _make_born_tree(root)
    subprocess.run(["git", "-C", str(clone), "init", "-q"], check=True, timeout=60)
    _check("fixture: the clone is already a worktree", (clone / ".git").exists())
    _check("fixture: the clone has no .gitignore", not (clone / ".gitignore").exists())

    record = git_init_worktree(clone, "testhum")

    _check(
        "a cloned seed still reports skipped (history is never touched)",
        record["status"] == "skipped",
        str(record),
    )
    _check(
        "a cloned seed with no .gitignore GETS one",
        (clone / ".gitignore").is_file(),
        f"no .gitignore written; record={record}",
    )
    _check(
        "the skipped record reports the write it performed",
        record.get("gitignore_written") is True,
        str(record),
    )
    body = (clone / ".gitignore").read_text(encoding="utf-8")
    _check(
        "the written .gitignore covers runtime state and secrets",
        "profile/data/" in body and "profile/config/vault/" in body,
        body[:200],
    )


def main() -> int:
    try:
        with tempfile.TemporaryDirectory() as tmp:
            _check_fresh_init(Path(tmp))
        with tempfile.TemporaryDirectory() as tmp:
            _check_idempotent(Path(tmp))
        with tempfile.TemporaryDirectory() as tmp:
            _check_cloned_seed_gets_a_gitignore(Path(tmp))
    except SmokeFailureError as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        print(f"  ({len(_CHECKS_RUN)} checks attempted before failure)", file=sys.stderr)
        return 1

    print(f"git_init_smoke OK: {len(_CHECKS_RUN)} checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
