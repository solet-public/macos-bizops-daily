#!/usr/bin/env python3
"""Check or materialize the canonical coordination-hook policy files."""

from __future__ import annotations

import argparse
import shutil
from pathlib import Path

_COMMON_DIR = Path(__file__).resolve().parent
_PLUGIN_DIR = _COMMON_DIR.parent
_POLICY_FILES = (
    "_git_controller_lex.py",
    "_git_controller_walker.py",
    "_git_policy.py",
)
_RUNNER_HOOK_DIRS = (
    _PLUGIN_DIR / "claude_plugin" / "coordination-hooks" / "hooks",
    _PLUGIN_DIR / "codex_plugin" / "coordination-hooks" / "hooks",
)


def _mismatches() -> list[Path]:
    """Return materialized files whose bytes differ from the canonical file."""
    mismatches: list[Path] = []
    for runner_dir in _RUNNER_HOOK_DIRS:
        for name in _POLICY_FILES:
            destination = runner_dir / name
            if not destination.is_file():
                mismatches.append(destination)
                continue
            if destination.read_bytes() != (_COMMON_DIR / name).read_bytes():
                mismatches.append(destination)
    return mismatches


def _write_materialized_files() -> None:
    """Replace both runner copies with the canonical policy bytes."""
    for runner_dir in _RUNNER_HOOK_DIRS:
        runner_dir.mkdir(parents=True, exist_ok=True)
        for name in _POLICY_FILES:
            shutil.copyfile(_COMMON_DIR / name, runner_dir / name)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument(
        "--check",
        action="store_true",
        help="verify that both runner copies match the canonical files",
    )
    mode.add_argument(
        "--write",
        action="store_true",
        help="materialize canonical files before checking",
    )
    args = parser.parse_args()
    if args.write:
        _write_materialized_files()
    mismatches = _mismatches()
    if mismatches:
        print("coordination-hook policy copies differ from canonical source:")
        for path in mismatches:
            print(f"  {path}")
        return 1
    print("coordination-hook policy copies match canonical source")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
