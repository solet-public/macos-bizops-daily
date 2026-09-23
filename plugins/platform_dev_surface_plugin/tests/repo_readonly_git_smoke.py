#!/usr/bin/env python3
"""repo_service read-only-git acceptance smoke (no pytest, offline).

Covers B3 §5's ``repo_readonly_git_smoke`` row: git_status / git_diff run
read-only against the worktree, and NO mutating git verb is reachable from the
service. The structural pin reads the operations source and asserts the only git
subcommands it can invoke are status / diff / apply, and that ``apply`` is ONLY
ever used with ``--check`` (the read-only carve-out design §2.2).

RED-FIRST (structural): adding any mutating git verb (commit/add/checkout/…) or a
bare ``git apply`` to operations.py fails the source-pin assertions below.

Run from repo root:
    .venv/bin/python3 plugins/platform_dev_surface_plugin/tests/repo_readonly_git_smoke.py
"""

from __future__ import annotations

import re
import sys
import tempfile
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(_REPO_ROOT / "plugins" / "platform_dev_surface_plugin" / "src"))

from platform_dev_surface_plugin.repo.errors import RepoToolError  # noqa: E402
from platform_dev_surface_plugin.repo.operations import RepoOperations  # noqa: E402

_OPS_SRC = (
    _REPO_ROOT
    / "plugins/platform_dev_surface_plugin/src/platform_dev_surface_plugin/repo/operations.py"
)
_FORBIDDEN_GIT_VERBS = (
    "commit", "add", "checkout", "push", "reset", "merge",
    "rebase", "stash", "clean", "restore", "rm", "cherry-pick",
)

_passed = 0
_failed: list[str] = []


def _check(condition: object, label: str) -> None:
    global _passed
    if condition:
        _passed += 1
        print(f"  PASS  {label}")
    else:
        _failed.append(label)
        print(f"  FAIL  {label}")


class _FakeStore:
    def store(self, **_: object) -> str:
        return "patch-fake"


def _test_ref_injection(ops: RepoOperations) -> None:
    # F3 RED-FIRST: a ref is a git revision; '--output=<path>' is option
    # injection that makes git WRITE/TRUNCATE the file. The guard must raise and
    # write NOTHING. With the guard reverted, git writes the marker (the write
    # primitive) — this pin proves it's closed.
    marker = Path(tempfile.mkdtemp(prefix="f3_")) / "should_not_be_written.txt"
    try:
        ops.git_diff(ref=f"--output={marker}")
    except RepoToolError:
        _check(not marker.exists(), "git_diff(ref='--output=...') RAISES and writes NOTHING (F3)")
        return
    _check(False, "git_diff(ref='--output=...') RAISES on option injection (F3)")


def main() -> int:
    print("repo_service read-only-git smoke")
    src = _OPS_SRC.read_text(encoding="utf-8")

    # Structural pin: no mutating git verb appears as a quoted argv literal.
    leaked = [v for v in _FORBIDDEN_GIT_VERBS if f'"{v}"' in src]
    _check(not leaked, f"operations source invokes NO mutating git verb (found {leaked})")

    # apply is ONLY ever paired with --check (never a bare apply).
    apply_sites = re.findall(r'"apply"[^\]]*', src)
    _check(bool(apply_sites), "operations uses git apply (for --check)")
    _check(all("--check" in site for site in apply_sites),
           "every git-apply use carries --check (read-only, never mutates)")

    # Live: git_status / git_diff run read-only against the worktree.
    ops = RepoOperations(_REPO_ROOT, _FakeStore())
    _test_ref_injection(ops)  # F3: pin the operations.py:214 option-injection guard
    status = ops.git_status()
    _check(set(status.keys()) >= {"branch", "staged", "unstaged", "untracked"},
           "git_status returns the structured shape")
    _check(isinstance(status["branch"], str) and status["branch"] != "",
           "git_status reports a branch (read-only)")
    diff = ops.git_diff()
    _check(set(diff.keys()) >= {"diff", "truncated", "diff_chars_total", "stat"},
           "git_diff returns the structured shape (read-only)")

    print(f"\n{_passed} passed, {len(_failed)} failed")
    if _failed:
        for label in _failed:
            print(f"  FAILED: {label}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
