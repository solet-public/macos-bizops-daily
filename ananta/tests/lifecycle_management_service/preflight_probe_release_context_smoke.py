#!/usr/bin/env python3
"""S5 — release-context fidelity pin for the GTE-06 probe (no pytest).

The RELEASE-CONTEXT DIVERGENCE class (the ``repo_root`` APP_HOME
deploy-fix / ``repo_root_release_copy_smoke`` pattern): a gate proven
only against the worktree can silently validate the WRONG tree at
deploy time. This smoke pins that the probe's verdict follows exactly
the tree its interpreter/env resolves — the mechanism by which the
production probe is bound to the candidate release's ``code/`` (its own
venv + re-pointed ``.pth``), not the worktree.

Two divergent copies of the same fixture package exist side by side:

* breakage planted ONLY in the probed tree (other tree healthy) → RED —
  the probe cannot be reading the healthy sibling;
* breakage planted ONLY in the non-probed tree (probed tree healthy) →
  GREEN — the probe cannot be reading the broken sibling.

Run:
    SOLET_NAME=<name> .venv/bin/python3 ananta/tests/lifecycle_management_service/preflight_probe_release_context_smoke.py
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT / "ananta" / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _probe_fixture_support import (  # noqa: E402
    BROKEN_SOURCE,
    FIXTURE_PLUGIN_NAME,
    GOOD_SOURCE,
    run_probe_subprocess,
    write_fixture,
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


def run_smoke() -> int:
    print("=== preflight_probe_release_context_smoke (S5: verdict follows the PROBED tree) ===")
    with tempfile.TemporaryDirectory() as tmp:
        release_like = Path(tmp) / "release_copy"
        worktree_like = Path(tmp) / "worktree"

        # Case A: breakage ONLY in the probed (release-like) tree.
        write_fixture(release_like, BROKEN_SOURCE)
        write_fixture(worktree_like, GOOD_SOURCE)
        exit_code, envelope, stderr = run_probe_subprocess(
            fixture_dir=release_like, plugins=[FIXTURE_PLUGIN_NAME],
        )
        _check(
            exit_code == 3 and envelope is not None and envelope.get("ok") is False,
            f"[1] probe RED when the PROBED tree is broken (healthy sibling "
            f"ignored) — exit={exit_code}, stderr tail: {stderr[-150:]!r}",
        )

        # Case B: breakage ONLY in the non-probed (worktree-like) tree.
        write_fixture(release_like, GOOD_SOURCE)
        write_fixture(worktree_like, BROKEN_SOURCE)
        exit_code, envelope, stderr = run_probe_subprocess(
            fixture_dir=release_like, plugins=[FIXTURE_PLUGIN_NAME],
        )
        _check(
            exit_code == 0 and envelope is not None and envelope.get("ok") is True,
            f"[2] probe GREEN when the PROBED tree is healthy (broken sibling "
            f"ignored) — exit={exit_code}, stderr tail: {stderr[-150:]!r}",
        )

    print(f"\npreflight_probe_release_context_smoke: {_passed} passed, {len(_failed)} failed")
    for label in _failed:
        print(f"  FAILED: {label}")
    return 1 if _failed else 0


if __name__ == "__main__":
    sys.exit(run_smoke())
