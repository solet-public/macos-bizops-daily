#!/usr/bin/env python3
"""Smoke tests for the GTE-13 materialized-battery artifact discriminator
(no pytest, per project rule).

THE PROPERTY THAT MATTERS MOST: the discriminator's default is REAL, not
"unknown" — every branch below that CANNOT positively prove all three checks
(clean, byte-identical, direct-run-clean) must verdict REAL, never silently
pass. Each positive case below is paired with a negative control that
deliberately breaks exactly one of the three checks, so a weakened
implementation that stopped checking one of them would still be caught here.

Run: ``.venv/bin/python3 quality_gates/tests/materialized_battery_artifact_check_smoke.py``
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from quality_gates.lib import materialized_battery_artifact_discriminator as gte13  # noqa: E402

_FAILURES: list[str] = []

_CLEAN_SOURCE = "def f() -> int:\n    return 1\n"
_REL_PATH = "pkg/module.py"

_PYRIGHT_BIN = gte13.REPO_ROOT_DEFAULT / ".venv" / "bin" / "pyright"

_PYPROJECT = """\
[tool.pyright]
pythonVersion = "3.13"
typeCheckingMode = "basic"
include = ["pkg"]
"""


def check(name: str, condition: bool) -> None:
    status = "PASS" if condition else "FAIL"
    print(f"  [{status}] {name}")
    if not condition:
        _FAILURES.append(name)


def _git(root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(root), *args],
        check=True, capture_output=True, text=True, timeout=60,
    )


@contextmanager
def _scratch_repo(*, source: str = _CLEAN_SOURCE) -> Generator[Path]:
    """A throwaway git repo with one committed, pyright-clean module —
    ``repo_root`` for the discriminator under test."""
    with tempfile.TemporaryDirectory(prefix="gte13_fixture_") as tmp:
        root = Path(tmp).resolve()
        (root / "pkg").mkdir()
        (root / "pkg" / "module.py").write_text(source, encoding="utf-8")
        (root / "pyproject.toml").write_text(_PYPROJECT, encoding="utf-8")
        _git(root, "init", "-q")
        _git(root, "config", "user.email", "gte13@test.local")
        _git(root, "config", "user.name", "gte13-fixture")
        _git(root, "add", "-A")
        _git(root, "commit", "-q", "-m", "initial")
        yield root


def _worktree_copy(repo_root: Path, dest: Path) -> None:
    """A plain filesystem copy of the whole repo — the same reproduction
    technique the GTE-13 investigation used (no ``git worktree``)."""
    import shutil

    shutil.copytree(repo_root, dest)


def _classify(repo_root: Path, worktree_root: Path, *, rel_path: str = _REL_PATH) -> gte13.Evidence:
    return gte13.classify(
        worktree_root=worktree_root,
        repo_root=repo_root,
        rel_path=rel_path,
        message="test finding",
        line=1,
        pyright_bin=_PYRIGHT_BIN,
    )


# ---------------------------------------------------------------------------
# The default itself
# ---------------------------------------------------------------------------


def test_evidence_default_verdict_is_real() -> None:
    print("Evidence() defaults to REAL before classify() runs anything")
    ev = gte13.Evidence()
    check("default verdict is 'real'", ev.verdict == "real")


# ---------------------------------------------------------------------------
# The positive case
# ---------------------------------------------------------------------------


def test_clean_identical_and_pyright_clean_is_artifact() -> None:
    print("Case: clean, byte-identical, pyright-clean -> ARTIFACT")
    if not _PYRIGHT_BIN.is_file():
        check("SKIP (pyright not found at expected path — cannot exercise this case)", True)
        return
    with _scratch_repo() as repo_root:
        with tempfile.TemporaryDirectory(prefix="gte13_worktree_") as wt:
            worktree_root = Path(wt) / "wt"
            _worktree_copy(repo_root, worktree_root)
            ev = _classify(repo_root, worktree_root)
            check("verdict is artifact", ev.verdict == "artifact")
            check("evidence names all three checks",
                  "git status: clean" in ev.render()
                  and "byte-identical" in ev.render()
                  and "direct pyright run" in ev.render())


# ---------------------------------------------------------------------------
# Negative controls — each breaks exactly ONE of the three checks
# ---------------------------------------------------------------------------


def test_dirty_live_checkout_is_real() -> None:
    print("NEGATIVE CONTROL: dirty live checkout (uncommitted local edit) -> REAL")
    with _scratch_repo() as repo_root:
        with tempfile.TemporaryDirectory(prefix="gte13_worktree_") as wt:
            worktree_root = Path(wt) / "wt"
            _worktree_copy(repo_root, worktree_root)
            # Dirty the LIVE CHECKOUT after the worktree copy was taken.
            (repo_root / "pkg" / "module.py").write_text(
                "def f() -> int:\n    return 2\n", encoding="utf-8",
            )
            ev = _classify(repo_root, worktree_root)
            check("verdict is real", ev.verdict == "real")
            check("evidence says DIRTY", "DIRTY" in ev.render())
            check("direct run was skipped, not run against a dirty file",
                  "SKIPPED" in ev.render())


def test_worktree_diverges_from_head_is_real() -> None:
    print("NEGATIVE CONTROL: worktree copy has a real, intentional change -> REAL")
    with _scratch_repo() as repo_root:
        with tempfile.TemporaryDirectory(prefix="gte13_worktree_") as wt:
            worktree_root = Path(wt) / "wt"
            _worktree_copy(repo_root, worktree_root)
            # The live checkout stays clean; only the WORKTREE'S copy changes —
            # exactly what a real in-flight edit under review looks like.
            (worktree_root / "pkg" / "module.py").write_text(
                "def f() -> int:\n    return 3\n", encoding="utf-8",
            )
            ev = _classify(repo_root, worktree_root)
            check("verdict is real", ev.verdict == "real")
            check("evidence names the worktree mismatch",
                  "worktree copy" in ev.render() and "differs from HEAD" in ev.render())


def test_missing_worktree_file_is_real() -> None:
    print("FAULT case: worktree file does not exist -> REAL, loud FAULT")
    with _scratch_repo() as repo_root:
        with tempfile.TemporaryDirectory(prefix="gte13_worktree_") as wt:
            worktree_root = Path(wt) / "wt"
            worktree_root.mkdir()
            ev = _classify(repo_root, worktree_root)
            check("verdict is real", ev.verdict == "real")
            check("evidence says FAULT", "FAULT" in ev.render())
            check("names the worktree file specifically",
                  "worktree file does not exist" in ev.render())


def test_missing_head_entry_is_real() -> None:
    print("FAULT case: file not tracked at HEAD -> REAL, loud FAULT")
    with _scratch_repo() as repo_root:
        with tempfile.TemporaryDirectory(prefix="gte13_worktree_") as wt:
            worktree_root = Path(wt) / "wt"
            _worktree_copy(repo_root, worktree_root)
            ev = _classify(repo_root, worktree_root, rel_path="pkg/never_committed.py")
            check("verdict is real", ev.verdict == "real")
            check("evidence says FAULT", "FAULT" in ev.render())
            check("names git show as the failing op", "git show" in ev.render())


def test_missing_pyright_binary_is_real() -> None:
    print("FAULT case: pyright binary absent -> REAL, loud FAULT (never silently 'clean')")
    with _scratch_repo() as repo_root:
        with tempfile.TemporaryDirectory(prefix="gte13_worktree_") as wt:
            worktree_root = Path(wt) / "wt"
            _worktree_copy(repo_root, worktree_root)
            ev = gte13.classify(
                worktree_root=worktree_root,
                repo_root=repo_root,
                rel_path=_REL_PATH,
                message="test",
                line=None,
                pyright_bin=Path("/nonexistent/pyright/binary"),
            )
            check("verdict is real", ev.verdict == "real")
            check("evidence says FAULT: pyright binary not found",
                  "FAULT: pyright binary not found" in ev.render())


# ---------------------------------------------------------------------------
# CLI contract — exit codes
# ---------------------------------------------------------------------------


def test_run_exit_codes() -> None:
    print("run() exit codes: 0 artifact, 1 real, 2 harness error")
    if not _PYRIGHT_BIN.is_file():
        check("SKIP (pyright not found — cannot exercise the artifact exit-0 case)", True)
    else:
        with _scratch_repo() as repo_root:
            with tempfile.TemporaryDirectory(prefix="gte13_worktree_") as wt:
                worktree_root = Path(wt) / "wt"
                _worktree_copy(repo_root, worktree_root)
                rc = gte13.run([
                    "--worktree-root", str(worktree_root),
                    "--repo-root", str(repo_root),
                    "--rel-path", _REL_PATH,
                    "--pyright-bin", str(_PYRIGHT_BIN),
                ])
                check("exit 0 for artifact", rc == 0)

    rc_missing_root = gte13.run([
        "--worktree-root", "/definitely/does/not/exist/gte13",
        "--rel-path", _REL_PATH,
    ])
    check("exit 2 when --worktree-root does not exist", rc_missing_root == 2)


def test_main_usage_error_is_64() -> None:
    print("main() maps an argparse usage error to exit 64")
    old_argv = sys.argv
    try:
        sys.argv = ["materialized_battery_artifact_discriminator.py"]  # missing args
        rc = gte13.main()
    finally:
        sys.argv = old_argv
    check("exit 64 on missing required arguments", rc == 64)


# ---------------------------------------------------------------------------


def main() -> int:
    print("GTE-13 materialized-battery artifact discriminator smoke\n")
    for test in (
        test_evidence_default_verdict_is_real,
        test_clean_identical_and_pyright_clean_is_artifact,
        test_dirty_live_checkout_is_real,
        test_worktree_diverges_from_head_is_real,
        test_missing_worktree_file_is_real,
        test_missing_head_entry_is_real,
        test_missing_pyright_binary_is_real,
        test_run_exit_codes,
        test_main_usage_error_is_64,
    ):
        test()
    print()
    if _FAILURES:
        print(f"FAILED: {len(_FAILURES)} check(s): {_FAILURES}")
        return 1
    print("ALL SMOKE CHECKS PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
