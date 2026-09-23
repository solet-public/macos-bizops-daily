"""Hermetic cleanup contract for ``swap_round_trip_smoke`` private scratch."""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[3]
_SMOKE = _ROOT / "plugins/macos_self_deployment_plugin/tests/swap_round_trip_smoke.py"
_CHECKS = 0


def _check(condition: bool, label: str) -> None:
    global _CHECKS
    _CHECKS += 1
    if not condition:
        raise AssertionError(label)


def _run(home: Path, *args: str) -> subprocess.CompletedProcess[str]:
    environment = dict(os.environ)
    environment["HOME"] = str(home)
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    return subprocess.run(
        [sys.executable, str(_SMOKE), *args],
        check=False,
        capture_output=True,
        text=True,
        env=environment,
    )


def _scratch_dirs(home: Path) -> list[Path]:
    scratch = home / ".ananta"
    return sorted(scratch.glob("lbg_smoke_*")) if scratch.is_dir() else []


def _check_cleanup_on_success(root: Path) -> None:
    completed = _run(root / "success")
    _check(
        completed.returncode == 0,
        f"success smoke failed: stdout={completed.stdout} stderr={completed.stderr}",
    )
    _check(
        _scratch_dirs(root / "success") == [],
        "success removes private scratch (red: remove the finalizer)",
    )


def _check_cleanup_on_post_router_failure(root: Path) -> None:
    completed = _run(root / "failure", "--fail-after-router")
    _check(completed.returncode == 1, f"injected failure exit: {completed.returncode}")
    _check(
        _scratch_dirs(root / "failure") == [],
        "post-router failure removes private scratch (red: raise before harness stop)",
    )


def _check_explicit_retain_is_narrow(root: Path) -> None:
    home = root / "retain"
    completed = _run(home, "--retain-artifacts")
    retained = _scratch_dirs(home)
    _check(completed.returncode == 0, f"retained smoke failed: {completed.stderr}")
    _check(len(retained) == 1, f"retain preserves exactly one named run: {retained}")
    _check(
        retained[0].parent == home / ".ananta" and retained[0].name.startswith("lbg_smoke_"),
        f"retain scope is private run only: {retained}",
    )


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="lbg-", dir="/tmp") as temporary:
        root = Path(temporary)
        _check_cleanup_on_success(root)
        _check_cleanup_on_post_router_failure(root)
        _check_explicit_retain_is_narrow(root)
    print(f"{_CHECKS} checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
