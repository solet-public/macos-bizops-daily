#!/usr/bin/env python3
"""repo_root release-copy anchoring smoke (no pytest, offline).

Class-pin for the 2026-07-06 B3 Half-2 DEPLOY FATALITY: the platform runs from
a materialized release copy (``~/.ananta/releases/<name>/rel-.../code/``) whose
tree has NO ``.git`` but DOES carry a ``quality_gates/`` snapshot. The original
``locate_repo_root`` anchored at ``__file__`` and walked ancestors — which, from
the release copy, either fail-louded at green boot (the observed fatality) or,
worse, would have silently resolved the FROZEN release snapshot as "the repo"
and gated an immutable copy instead of the working tree.

The fix anchors at ``app_home`` (deploy-invariant: the release-copy process is
spawned with the worktree's ``--app-home``; its parent IS the worktree). This
smoke pins that behavior by importing the locator from a RELOCATED copy that
lives outside the worktree (simulating the release ``code/`` dir, complete with
its own ``quality_gates/`` and no ``.git``) and asserting it STILL resolves the
worktree via ``app_home``.

RED-FIRST: against the pre-fix ``__file__``-anchored shape, ``locate_repo_root``
took no ``app_home`` argument and walked the relocated module's own ancestors —
so this smoke's "resolves the worktree via app_home" assertion is structurally
unreachable under that shape (reverting repo_root.py to the __file__ walk fails
this smoke).

Run from repo root:
    .venv/bin/python3 plugins/platform_dev_surface_plugin/tests/repo_root_release_copy_smoke.py
"""

from __future__ import annotations

import importlib.util
import shutil
import sys
import tempfile
from pathlib import Path
from types import ModuleType

_SMOKE_FILE = Path(__file__).resolve()
_WORKTREE = next(
    p
    for p in _SMOKE_FILE.parents
    if (p / ".git").exists() and (p / "quality_gates").is_dir()
)
# app_home anchor: <worktree>/profile — its parent IS the worktree. Deploy-
# invariant (the release-copy process runs with this same --app-home).
_APP_HOME = _WORKTREE / "profile"
_REPO_ROOT_SRC = (
    _WORKTREE
    / "plugins/platform_dev_surface_plugin/src/platform_dev_surface_plugin/repo_root.py"
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


def _import_from(path: Path) -> ModuleType:
    """Import repo_root.py from an arbitrary on-disk path (fresh module object)."""
    spec = importlib.util.spec_from_file_location("repo_root_relocated", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"could not build import spec for {path}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def main() -> int:
    print("repo_root release-copy anchoring smoke")

    # Simulate the materialized release copy: the locator living OUTSIDE the
    # worktree, in a tree that (like the real release code/ dir) carries a
    # quality_gates/ snapshot but has NO .git.
    release = Path(tempfile.mkdtemp(prefix="fake_release_code_"))
    (release / "quality_gates").mkdir()
    relocated_py = release / "repo_root.py"
    shutil.copy(_REPO_ROOT_SRC, relocated_py)
    mod = _import_from(relocated_py)

    _check(
        Path(mod.__file__ or "").resolve() == relocated_py.resolve(),
        "locator's __file__ is the relocated (release-like) path, outside the worktree",
    )
    _check(_WORKTREE not in relocated_py.parents, "the relocated copy lives outside the worktree")

    # THE PIN: anchored at APP_HOME, it resolves the WORKTREE — not the release
    # snapshot it was imported from, and nothing derived from __file__.
    resolved = mod.locate_repo_root(_APP_HOME)
    _check(resolved == _WORKTREE, "resolves the WORKTREE root via app_home (not __file__)")
    _check(
        (resolved / ".git").exists() and (resolved / "quality_gates").is_dir(),
        "resolved root carries BOTH markers (.git + quality_gates)",
    )
    _check(
        resolved != release and release not in resolved.parents,
        "the release-copy location is NOT the anchor (would gate a frozen snapshot)",
    )

    # Fail-loud on a no-repo app_home (the cloud-solet path): typed RuntimeError.
    norepo = Path(tempfile.mkdtemp(prefix="no_repo_apphome_"))
    try:
        mod.locate_repo_root(norepo)
        _check(False, "no-repo app_home raises RuntimeError (cloud fail-loud)")
    except RuntimeError:
        _check(True, "no-repo app_home raises RuntimeError (cloud fail-loud)")

    shutil.rmtree(release, ignore_errors=True)
    shutil.rmtree(norepo, ignore_errors=True)

    print(f"\n{_passed} passed, {len(_failed)} failed")
    if _failed:
        for label in _failed:
            print(f"  FAILED: {label}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
