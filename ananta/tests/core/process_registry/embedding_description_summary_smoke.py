#!/usr/bin/env python3
"""Smoke: embedding_description validation logs an aggregate summary, not
one WARNING line per process.

On a platform with ~50 pre-existing embedding_description gaps, one WARNING
per process repeats the same lines on every single boot forever, drowning
real signal. ``validate_all_embedding_descriptions`` now collects findings
and logs ONE aggregate WARNING per category (missing / out-of-range) plus a
DEBUG line carrying the full list.

Coverage:

1. Multiple missing-embedding_description processes -> exactly ONE WARNING
   (not N), with the count in the message; full names land at DEBUG.
2. Multiple out-of-range processes -> exactly ONE WARNING with the count;
   full names + lengths land at DEBUG.
3. Zero findings -> no WARNING logged at all.
4. A non-string embedding_description still raises FrameworkError immediately
   (unchanged fail-loud behavior for a genuinely malformed value).

Offline: no live solet, no DB. Run:
    .venv/bin/python3 ananta/tests/core/process_registry/embedding_description_summary_smoke.py
"""

from __future__ import annotations

import logging
import sys
import traceback
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(_REPO_ROOT / "ananta" / "src"))

from ananta.core.process_registry.plugin_registration_validator import (  # noqa: E402
    PluginRegistrationValidator,
)
from ananta.error_handling import FrameworkError  # noqa: E402

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
    def __init__(self, level: int) -> None:
        super().__init__(level=level)
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)


def _capture(level: int, fn: object) -> list[logging.LogRecord]:
    handler = _RecordingHandler(level)
    logger = logging.getLogger("ananta.core.process_registry.plugin_registration_validator")
    prior_level = logger.level
    logger.addHandler(handler)
    logger.setLevel(level)
    try:
        fn()  # type: ignore[operator]
    finally:
        logger.removeHandler(handler)
        logger.setLevel(prior_level)
    return handler.records


def _registry(processes: dict[str, dict[str, object]]) -> dict[str, object]:
    return {"processes": processes}


def test_missing_descriptions_produce_one_aggregate_warning() -> None:
    registry = _registry({
        "service_interface::a::foo": {"provider_type": "service_interface", "provider": "a"},
        "service_interface::b::bar": {"provider_type": "service_interface", "provider": "b"},
        "plugin::c::baz": {"provider_type": "plugin", "provider": "c"},
    })
    validator = PluginRegistrationValidator()
    records = _capture(
        logging.DEBUG, lambda: validator.validate_all_embedding_descriptions(registry)
    )
    warnings = [r for r in records if r.levelno == logging.WARNING]
    debugs = [r for r in records if r.levelno == logging.DEBUG]
    _check(len(warnings) == 1, f"exactly one WARNING for missing descriptions (got {len(warnings)})")
    _check(
        warnings and "3 discoverable process(es) missing embedding_description" in warnings[0].getMessage(),
        "WARNING carries the correct count (3)",
    )
    _check(
        any("service_interface::a::foo" in d.getMessage() for d in debugs),
        "DEBUG line names the specific missing process",
    )


def test_out_of_range_descriptions_produce_one_aggregate_warning() -> None:
    registry = _registry({
        "service_interface::a::foo": {
            "provider_type": "service_interface", "provider": "a", "embedding_description": "x" * 10,
        },
        "service_interface::b::bar": {
            "provider_type": "service_interface", "provider": "b", "embedding_description": "y" * 900,
        },
    })
    validator = PluginRegistrationValidator()
    records = _capture(
        logging.DEBUG, lambda: validator.validate_all_embedding_descriptions(registry)
    )
    warnings = [r for r in records if r.levelno == logging.WARNING]
    debugs = [r for r in records if r.levelno == logging.DEBUG]
    _check(len(warnings) == 1, f"exactly one WARNING for out-of-range descriptions (got {len(warnings)})")
    _check(
        warnings and "2 discoverable process(es) have an embedding_description" in warnings[0].getMessage(),
        "WARNING carries the correct count (2)",
    )
    _check(
        any("length 900" in d.getMessage() for d in debugs),
        "DEBUG line names the specific out-of-range process with its length",
    )


def test_clean_registry_logs_nothing() -> None:
    registry = _registry({
        "service_interface::a::foo": {
            "provider_type": "service_interface", "provider": "a", "embedding_description": "z" * 250,
        },
    })
    validator = PluginRegistrationValidator()
    records = _capture(
        logging.DEBUG, lambda: validator.validate_all_embedding_descriptions(registry)
    )
    _check(len(records) == 0, f"no log records at all when everything is in range (got {len(records)})")


def test_non_string_description_still_raises_immediately() -> None:
    registry = _registry({
        "service_interface::a::foo": {
            "provider_type": "service_interface", "provider": "a", "embedding_description": 12345,
        },
    })
    validator = PluginRegistrationValidator()
    raised = False
    try:
        validator.validate_all_embedding_descriptions(registry)
    except FrameworkError as exc:
        raised = "must be a string" in exc.message
    _check(raised, "non-string embedding_description still raises FrameworkError immediately")


def main() -> int:
    print("=== embedding_description_summary_smoke ===")
    try:
        test_missing_descriptions_produce_one_aggregate_warning()
        test_out_of_range_descriptions_produce_one_aggregate_warning()
        test_clean_registry_logs_nothing()
        test_non_string_description_still_raises_immediately()
    except AssertionError:
        print("\nEMBEDDING_DESCRIPTION_SUMMARY_SMOKE: FAIL")
        traceback.print_exc()
        return 1
    except Exception:
        print("\nEMBEDDING_DESCRIPTION_SUMMARY_SMOKE: ERROR")
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
