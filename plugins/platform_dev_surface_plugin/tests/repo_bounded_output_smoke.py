#!/usr/bin/env python3
"""repo_service bounded-output acceptance smoke (no pytest, offline).

Covers B3 §5's ``repo_bounded_output_smoke`` row: output caps fire with an
EXPLICIT truncation marker + the true total (no silent cap). Pins the
repo-specific bounds — read_file line/byte cap and search result cap.

Run from repo root:
    .venv/bin/python3 plugins/platform_dev_surface_plugin/tests/repo_bounded_output_smoke.py
"""

from __future__ import annotations

import shutil
import sys
import tempfile
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(_REPO_ROOT / "plugins" / "platform_dev_surface_plugin" / "src"))

from platform_dev_surface_plugin.repo import operations as ops_mod  # noqa: E402
from platform_dev_surface_plugin.repo.errors import RepoToolError  # noqa: E402
from platform_dev_surface_plugin.repo.operations import RepoOperations  # noqa: E402

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


def _git_init(root: Path) -> None:
    """Make the fixture a git worktree — the shape this service actually runs in.

    `platform_dev_surface_plugin` operates on a worktree by construction (genesis
    git-inits every born tree), and since 2026-08-08 `search` falls back to the
    git-native grep when ripgrep is absent. A non-worktree fixture therefore tested
    the service outside its own contract and could not exercise the fallback path
    at all — it failed with "not a git repository" on exactly the node/rg-less
    machines the fallback exists for.
    """
    run = __import__("subprocess").run
    for args in (["-c", "init.defaultBranch=main", "init", "-q"],
                 ["config", "user.email", "t@localhost"], ["config", "user.name", "t"],
                 ["add", "-A"], ["-c", "commit.gpgsign=false", "commit", "-q", "-m", "fixture"]):
        run(["git", *args], cwd=root, capture_output=True, text=True, timeout=60, check=True)


def _build_fixture() -> Path:
    root = Path(tempfile.mkdtemp(prefix="bounded_fixture_"))
    over = ops_mod._MAX_FILE_LINES + 500
    (root / "big.txt").write_text("\n".join(f"line {i}" for i in range(over)), encoding="utf-8")
    for i in range(30):
        (root / f"m{i}.txt").write_text("NEEDLE here\n", encoding="utf-8")
    _git_init(root)
    return root


def _test_read_cap(ops: RepoOperations) -> None:
    over = ops_mod._MAX_FILE_LINES + 500
    rf = ops.read_file("big.txt")
    _check(rf["truncated"] is True, "read_file over the line cap marks truncated=True")
    _check(rf["total_lines"] == over, "read_file reports the TRUE total_lines (no silent cap)")
    _check(len(rf["content"].splitlines()) <= ops_mod._MAX_FILE_LINES, "content respects the line cap")


def _test_search_cap(ops: RepoOperations) -> None:
    result = ops.search("NEEDLE", max_results=10)
    _check(result["hit_count"] == 10, "search honors max_results")
    _check(result["truncated"] is True, "search over the cap marks truncated=True")


def _test_search_failloud(ops: RepoOperations) -> None:
    # B-N2 RED-FIRST: a malformed regex (rg exit 2) is a TYPED error, never
    # swallowed into empty hits. Removing the exit>=2 guard makes this raise
    # nothing (silent empty) and fails the smoke.
    try:
        ops.search("(")  # unclosed group -> ripgrep regex parse error
    except RepoToolError:
        _check(True, "search RAISES RepoToolError on a malformed regex (B-N2, not silent-empty)")
        return
    _check(False, "search RAISES RepoToolError on a malformed regex (B-N2, not silent-empty)")


def main() -> int:
    print("repo_service bounded-output smoke")
    root = _build_fixture()
    ops = RepoOperations(root, _FakeStore())
    _test_read_cap(ops)
    _test_search_cap(ops)
    _test_search_failloud(ops)
    shutil.rmtree(root, ignore_errors=True)
    print(f"\n{_passed} passed, {len(_failed)} failed")
    if _failed:
        for label in _failed:
            print(f"  FAILED: {label}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
