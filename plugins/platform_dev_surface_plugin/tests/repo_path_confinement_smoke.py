#!/usr/bin/env python3
"""repo_service path-confinement acceptance smoke (no pytest, offline).

Covers B3 §5's ``repo_path_confinement_smoke`` row + Rev-A ASK-4: the confinement
boundary is LOAD-BEARING because the worktree root contains profile/ (secrets)
and the git dir. This pins control 1 (design §2.3): every path arg is resolved
(``..`` collapsed, symlinks followed) and MUST land inside the root — traversal,
absolute-outside, and symlink-target-escape are typed-rejected, never clamped.

RELEASE-COPY LINKAGE (Dawn): path_security is ROOT-PARAMETERIZED — it takes the
root as an argument and never touches ``__file__`` — so it is release-context
safe by construction. This smoke proves that by confining against a throwaway
tmp root that is NOT the worktree.

RED-FIRST: the traversal-reject assertions are the pin — removing the confinement
check would let ``../`` / absolute-outside / symlink-escape leak, failing this smoke.

Run from repo root:
    .venv/bin/python3 plugins/platform_dev_surface_plugin/tests/repo_path_confinement_smoke.py
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(_REPO_ROOT / "plugins" / "platform_dev_surface_plugin" / "src"))

from platform_dev_surface_plugin.repo import path_security as ps  # noqa: E402
from platform_dev_surface_plugin.repo.errors import RepoPathError  # noqa: E402

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


def _rejects(root: Path, raw: str) -> bool:
    try:
        ps.confine(root, raw)
        return False
    except RepoPathError:
        return True


def main() -> int:
    print("repo_service path-confinement smoke")
    # A throwaway root that is NOT the worktree — proves root-parameterization
    # (no __file__ dependence; release-context safe).
    root = Path(tempfile.mkdtemp(prefix="confine_root_"))
    (root / "inside").mkdir()
    (root / "inside" / "file.txt").write_text("ok", encoding="utf-8")
    outside = Path(tempfile.mkdtemp(prefix="confine_outside_"))
    (outside / "secret.txt").write_text("leak", encoding="utf-8")
    os.symlink(outside, root / "escape")  # symlink INSIDE root pointing OUTSIDE

    resolved = ps.confine(root, "inside/file.txt")
    _check(resolved == (root / "inside" / "file.txt").resolve(),
           "an in-root path confines to its real path (root-parameterized, no __file__)")

    _check(_rejects(root, "../" + outside.name + "/secret.txt"), "reject .. traversal above root")
    _check(_rejects(root, str(outside / "secret.txt")), "reject absolute path outside root")
    _check(_rejects(root, "inside/../../" + outside.name), "reject deep .. escape")
    _check(_rejects(root, "escape/secret.txt"), "reject symlink whose TARGET escapes root")
    _check(_rejects(root, str(root / "escape" / "secret.txt")), "reject absolute-into-escaping-symlink")

    # The root itself confines (base of list_files); not an escape.
    _check(ps.confine(root, ".") == root.resolve(), "the root itself confines (not an escape)")

    print(f"\n{_passed} passed, {len(_failed)} failed")
    if _failed:
        for label in _failed:
            print(f"  FAILED: {label}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
