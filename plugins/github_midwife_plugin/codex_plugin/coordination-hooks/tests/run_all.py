#!/usr/bin/env python3
"""Run every offline coordination-hooks smoke in a controlled subprocess."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

sys.dont_write_bytecode = True


def main() -> int:
    tests_dir = Path(__file__).resolve().parent
    smokes = sorted(tests_dir.glob("*_smoke.py"))
    if not smokes:
        print("FAIL: no smoke files found")
        return 1
    failed: list[str] = []
    for smoke in smokes:
        proc = subprocess.run(
            [sys.executable, "-B", str(smoke)],
            cwd=tests_dir,
            check=False,
        )
        if proc.returncode != 0:
            failed.append(smoke.name)
    if failed:
        print(f"FAIL: {len(failed)} Codex plugin smoke(s): {', '.join(failed)}")
        return 1
    print(f"PASS: {len(smokes)} Codex plugin smoke files")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
