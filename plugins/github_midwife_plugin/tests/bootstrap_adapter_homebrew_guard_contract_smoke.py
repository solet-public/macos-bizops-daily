#!/usr/bin/env python3
"""Prove an ambient Homebrew accepts the bootstrap adapter's exact guard env."""

from __future__ import annotations

# ruff: noqa: E402
import shutil
import subprocess
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[3]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from bootstrap_adapter.homebrew import homebrew_guard_environment

_BREW_CANDIDATES = (
    "/opt/homebrew/bin/brew",
    "/usr/local/bin/brew",
)


def _ambient_brew() -> str | None:
    """Resolve the same absolute executable shapes the bootstrap adapter accepts."""

    candidates = (shutil.which("brew"), *_BREW_CANDIDATES)
    for candidate in dict.fromkeys(item for item in candidates if item is not None):
        path = Path(candidate)
        if path.is_absolute() and path.is_file() and path.stat().st_mode & 0o111:
            return str(path)
    return None


def main() -> int:
    brew = _ambient_brew()
    if brew is None:
        print("SKIP: ambient Homebrew executable is absent; exact guard contract is unavailable")
        return 77
    completed = subprocess.run(
        [brew, "install", "--dry-run", "--cask", "codex"],
        capture_output=True,
        check=False,
        env=homebrew_guard_environment(),
        text=True,
        timeout=300,
    )
    if completed.returncode != 0:
        raise AssertionError(
            "ambient Homebrew rejects the exact bootstrap guard environment: "
            f"exit={completed.returncode} stderr={completed.stderr.strip()}"
        )
    print("bootstrap_adapter_homebrew_guard_contract_smoke OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
