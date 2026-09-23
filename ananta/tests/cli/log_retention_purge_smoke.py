#!/usr/bin/env python3
"""Smoke: logging_setup.purge_old_logs deletes only files past retention.

Coverage:

1. A file older than retention_days is deleted; a fresh file is kept.
2. The sweep recurses into subdirectories (plugin_logs/<plugin>/ shape).
3. Returned (files_deleted, bytes_freed) matches what was actually removed.
4. A missing log directory returns (0, 0) without raising.
5. retention_days=0 deletes everything (boundary).

Standalone — not pytest. Run with::

    .venv/bin/python3 ananta/tests/cli/log_retention_purge_smoke.py
"""

from __future__ import annotations

import os
import sys
import tempfile
import time
import traceback
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT / "ananta" / "src"))

from ananta.logging_setup import purge_old_logs  # noqa: E402

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


def _write_aged(path: Path, content: bytes, age_days: float) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    aged_ts = time.time() - (age_days * 86400)
    os.utime(path, (aged_ts, aged_ts))


def test_deletes_old_keeps_fresh() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        log_dir = Path(tmp)
        old_file = log_dir / "2020-01-01_profile.log"
        fresh_file = log_dir / "2026-07-13_profile.log"
        _write_aged(old_file, b"stale" * 10, age_days=10)
        _write_aged(fresh_file, b"live", age_days=1)

        files_deleted, bytes_freed = purge_old_logs(str(log_dir), retention_days=7)

        _check(not old_file.exists(), "10-day-old file deleted")
        _check(fresh_file.exists(), "1-day-old file kept")
        _check(files_deleted == 1, f"reports 1 file deleted (got {files_deleted})")
        _check(bytes_freed == 50, f"reports 50 bytes freed (got {bytes_freed})")


def test_recurses_into_subdirectories() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        log_dir = Path(tmp)
        nested_old = log_dir / "plugin_logs" / "some_plugin" / "old.log"
        nested_fresh = log_dir / "plugin_logs" / "some_plugin" / "fresh.log"
        _write_aged(nested_old, b"x", age_days=30)
        _write_aged(nested_fresh, b"y", age_days=0)

        files_deleted, _ = purge_old_logs(str(log_dir), retention_days=7)

        _check(not nested_old.exists(), "old file in nested plugin_logs/ deleted")
        _check(nested_fresh.exists(), "fresh file in nested plugin_logs/ kept")
        _check(files_deleted == 1, f"reports 1 file deleted (got {files_deleted})")


def test_missing_directory_is_a_noop() -> None:
    files_deleted, bytes_freed = purge_old_logs("/nonexistent/does/not/exist", retention_days=7)
    _check(files_deleted == 0 and bytes_freed == 0, "missing log dir returns (0, 0), no raise")


def test_zero_retention_deletes_everything() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        log_dir = Path(tmp)
        f = log_dir / "just_written.log"
        _write_aged(f, b"z", age_days=0.01)

        files_deleted, _ = purge_old_logs(str(log_dir), retention_days=0)

        _check(not f.exists(), "retention_days=0 deletes even a near-fresh file")
        _check(files_deleted == 1, f"reports 1 file deleted (got {files_deleted})")


def main() -> int:
    print("=== log_retention_purge_smoke ===")
    try:
        test_deletes_old_keeps_fresh()
        test_recurses_into_subdirectories()
        test_missing_directory_is_a_noop()
        test_zero_retention_deletes_everything()
    except AssertionError:
        print("\nLOG_RETENTION_PURGE_SMOKE: FAIL")
        traceback.print_exc()
        return 1
    except Exception:
        print("\nLOG_RETENTION_PURGE_SMOKE: ERROR")
        traceback.print_exc()
        return 2
    if _failed:
        print(f"\n{_passed} passed, {len(_failed)} failed")
        for label in _failed:
            print(f"  FAILED: {label}")
        return 1
    print(f"\n{_passed} passed, 0 failed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
