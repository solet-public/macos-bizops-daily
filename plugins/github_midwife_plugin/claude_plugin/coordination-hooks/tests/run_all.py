#!/usr/bin/env python3
"""Run every coordination-hooks smoke and report one verdict.

This is the single entry point a reviewer needs:

    python3 tests/run_all.py

It requires only `python3`, the plugin's sole runtime dependency (no `node`
needed since 2026-08-08 -- see `wake_waiter.py`'s docstring). It needs no
repository, no network, no configuration, and no environment variables --
and it writes nothing outside a temporary directory that is removed when it
finishes.

Exit 0 when every smoke passes, non-zero otherwise.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

TESTS_DIR = Path(__file__).resolve().parent
PLUGIN_ROOT = TESTS_DIR.parent


def _smokes() -> list[Path]:
    return sorted(TESTS_DIR.glob("*_smoke.py"))


def main() -> int:
    smokes = _smokes()
    if not smokes:
        print("FAIL: no *_smoke.py files found in tests/ — nothing was verified")
        return 1

    print(f"coordination-hooks test suite — {PLUGIN_ROOT.name}")
    print(f"{len(smokes)} smoke(s): {', '.join(path.name for path in smokes)}")
    print("=" * 68)
    print()

    failed: list[str] = []
    for path in smokes:
        # -B so a reviewer never watches __pycache__ appear inside the artifact.
        proc = subprocess.run(
            [sys.executable, "-B", str(path)],
            capture_output=True,
            text=True,
            timeout=600,
            check=False,
        )
        sys.stdout.write(proc.stdout)
        if proc.stderr.strip():
            sys.stderr.write(proc.stderr)
        if proc.returncode != 0:
            failed.append(f"{path.name} (exit {proc.returncode})")
        print()

    print("=" * 68)
    if failed:
        print(f"SUITE FAILED — {len(failed)} of {len(smokes)} smoke(s) red:")
        for line in failed:
            print(f"    {line}")
        return 1
    print(f"SUITE PASSED — all {len(smokes)} smoke(s) green")
    return 0


if __name__ == "__main__":
    sys.exit(main())
