#!/usr/bin/env python3
"""Smoke: cli.py FATAL paths emit logger.critical alongside stderr.

Verifies the 2026-06-02 follow-on (Architect's wedge-fixed writeup §22-26):
the silent-wedge debug cost four launch cycles because
``_run_orchestrator_or_exit`` and ``_setup_environment_or_exit`` wrote
their FATAL only to ``sys.stderr`` via ``print``, never reaching the
platform's file logger. Both functions now mirror the FATAL into
``logger.critical`` so ``tail profile/data/logs/*.log`` surfaces the
same diagnosis the operator's terminal shows.

Coverage:

1. ``_setup_environment_or_exit`` — ValidationError branch fires both
   stderr and logger.critical; details line + traceback both land in
   the file logger.
2. ``_setup_environment_or_exit`` — bare Exception branch likewise.
3. ``_run_orchestrator_or_exit`` — AnantaError branch likewise.
4. ``_run_orchestrator_or_exit`` — bare Exception branch likewise.
5. sys.exit(1) preserved on every branch (backward-compat).

Stubs ``setup_environment`` + ``initialize_components_sync`` to force
each branch, captures stderr + the module logger's records.

Standalone — not pytest. Run with::

    .venv/bin/python3 ananta/tests/cli/fatal_path_logging_smoke.py
"""

from __future__ import annotations

import argparse
import io
import logging
import sys
import traceback
from contextlib import redirect_stderr
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT / "ananta" / "src"))

from ananta.error_handling import (  # noqa: E402
    ErrorCode,
    ErrorSeverity,
    SystemError,
    ValidationError,
)

from ananta import cli as _cli  # noqa: E402

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


class _RecordingHandler(logging.Handler):
    def __init__(self) -> None:
        super().__init__(level=logging.CRITICAL)
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        if record.levelno >= logging.CRITICAL:
            self.records.append(record)


def _install_handler() -> _RecordingHandler:
    handler = _RecordingHandler()
    logger = logging.getLogger("ananta.cli")
    logger.addHandler(handler)
    logger.setLevel(logging.CRITICAL)
    return handler


def _remove_handler(handler: _RecordingHandler) -> None:
    logger = logging.getLogger("ananta.cli")
    logger.removeHandler(handler)


def _fake_args() -> argparse.Namespace:
    return argparse.Namespace(app_home="/tmp/does-not-matter")


def _run_setup_branch(
    *,
    raise_exc: BaseException,
) -> tuple[str, list[logging.LogRecord], int]:
    """Stub setup_environment to raise; capture stderr + critical records + exit code."""
    handler = _install_handler()
    captured = io.StringIO()
    exit_code = 0
    original_setup = _cli.setup_environment

    def _boom(_args: argparse.Namespace) -> Path:
        del _args
        raise raise_exc

    _cli.setup_environment = _boom  # type: ignore[assignment]
    try:
        with redirect_stderr(captured):
            try:
                _cli._setup_environment_or_exit(_fake_args())
            except SystemExit as exc:
                exit_code = int(exc.code) if isinstance(exc.code, int) else 1
    finally:
        _cli.setup_environment = original_setup  # type: ignore[assignment]
        _remove_handler(handler)
    return captured.getvalue(), list(handler.records), exit_code


def _run_orchestrator_branch(
    *,
    raise_exc: BaseException,
) -> tuple[str, list[logging.LogRecord], int]:
    """Stub initialize_components_sync to raise; capture the same triple."""
    handler = _install_handler()
    captured = io.StringIO()
    exit_code = 0
    original_init = _cli.initialize_components_sync

    def _boom(_app_home: Path, _args: argparse.Namespace) -> Any:
        del _app_home, _args
        raise raise_exc

    _cli.initialize_components_sync = _boom  # type: ignore[assignment]
    try:
        with redirect_stderr(captured):
            try:
                _cli._run_orchestrator_or_exit(Path("/tmp"), _fake_args())
            except SystemExit as exc:
                exit_code = int(exc.code) if isinstance(exc.code, int) else 1
    finally:
        _cli.initialize_components_sync = original_init  # type: ignore[assignment]
        _remove_handler(handler)
    return captured.getvalue(), list(handler.records), exit_code


def test_setup_validation_error_fires_both_streams() -> None:
    err = ValidationError(
        message="bad input",
        error_code=ErrorCode.VALIDATION_GENERIC,
        details={"field": "app_home"},
        severity=ErrorSeverity.CRITICAL,
    )
    stderr_text, records, exit_code = _run_setup_branch(raise_exc=err)
    _check(
        "FATAL ValidationError during setup" in stderr_text,
        "ValidationError: stderr still carries the FATAL header",
    )
    headers = [r.getMessage() for r in records]
    _check(
        any("FATAL ValidationError during setup" in m for m in headers),
        "ValidationError: logger.critical mirrors the FATAL header",
    )
    _check(
        any("Details:" in m for m in headers),
        "ValidationError: logger.critical mirrors the Details line",
    )
    _check(
        any("FATAL traceback:" in m for m in headers),
        "ValidationError: logger.critical mirrors the traceback",
    )
    _check(exit_code == 1, "ValidationError: sys.exit(1) preserved")


def test_setup_bare_exception_fires_both_streams() -> None:
    err = RuntimeError("disk full")
    stderr_text, records, exit_code = _run_setup_branch(raise_exc=err)
    _check(
        "FATAL Exception during setup" in stderr_text,
        "bare Exception (setup): stderr still carries the FATAL header",
    )
    headers = [r.getMessage() for r in records]
    _check(
        any("FATAL Exception during setup" in m for m in headers),
        "bare Exception (setup): logger.critical mirrors the FATAL header",
    )
    _check(
        any("FATAL traceback:" in m for m in headers),
        "bare Exception (setup): logger.critical mirrors the traceback",
    )
    _check(exit_code == 1, "bare Exception (setup): sys.exit(1) preserved")


def test_run_orchestrator_ananta_error_fires_both_streams() -> None:
    err = SystemError(
        message="orchestrator construction failed",
        error_code=ErrorCode.SYSTEM_GENERIC,
        severity=ErrorSeverity.CRITICAL,
    )
    stderr_text, records, exit_code = _run_orchestrator_branch(raise_exc=err)
    _check(
        "FATAL AnantaError during runtime" in stderr_text,
        "AnantaError: stderr still carries the FATAL header",
    )
    headers = [r.getMessage() for r in records]
    _check(
        any("FATAL AnantaError during runtime" in m for m in headers),
        "AnantaError: logger.critical mirrors the FATAL header",
    )
    _check(
        any("FATAL traceback:" in m for m in headers),
        "AnantaError: logger.critical mirrors the traceback",
    )
    _check(exit_code == 1, "AnantaError: sys.exit(1) preserved")


def test_run_orchestrator_bare_exception_fires_both_streams() -> None:
    err = RuntimeError("daemon thread holding")
    stderr_text, records, exit_code = _run_orchestrator_branch(raise_exc=err)
    _check(
        "FATAL Exception during runtime" in stderr_text,
        "bare Exception (run): stderr still carries the FATAL header",
    )
    headers = [r.getMessage() for r in records]
    _check(
        any("FATAL Exception during runtime" in m for m in headers),
        "bare Exception (run): logger.critical mirrors the FATAL header",
    )
    _check(
        any("FATAL traceback:" in m for m in headers),
        "bare Exception (run): logger.critical mirrors the traceback",
    )
    _check(exit_code == 1, "bare Exception (run): sys.exit(1) preserved")


def main() -> int:
    print("=== fatal_path_logging_smoke ===")
    try:
        test_setup_validation_error_fires_both_streams()
        test_setup_bare_exception_fires_both_streams()
        test_run_orchestrator_ananta_error_fires_both_streams()
        test_run_orchestrator_bare_exception_fires_both_streams()
    except AssertionError:
        print("\nFATAL_PATH_LOGGING_SMOKE: FAIL")
        traceback.print_exc()
        return 1
    except Exception:
        print("\nFATAL_PATH_LOGGING_SMOKE: ERROR")
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
