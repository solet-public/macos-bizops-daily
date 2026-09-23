#!/usr/bin/env python3
"""Shared fail-safe ``dry_run`` coercion vectors for iss_8918fbc4.

The coercer sits at a public destructive-process boundary.  This smoke pins
the mandated safe-default matrix without importing or invoking any engine.
"""

from __future__ import annotations

import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[3]
_ANANTA_SRC = _REPO_ROOT / "ananta" / "src"
if str(_ANANTA_SRC) not in sys.path:
    sys.path.insert(0, str(_ANANTA_SRC))

from ananta.utils.dry_run import coerce_dry_run  # noqa: E402


def _expect(actual: bool, expected: bool, label: str) -> None:
    if actual != expected:
        raise AssertionError(f"{label}: expected {expected!r}, got {actual!r}")
    print(f"  OK  {label}")


def _expect_refused(value: object, label: str) -> None:
    fake_calls: list[bool] = []

    def fake_engine(dry_run: bool) -> None:
        fake_calls.append(dry_run)

    try:
        fake_engine(coerce_dry_run(value))
    except ValueError:
        pass
    else:
        raise AssertionError(f"{label}: expected ValueError")
    if fake_calls:
        raise AssertionError(f"{label}: fake engine was touched")
    print(f"  OK  {label}: refused before fake engine")


def main() -> int:
    print("shared dry_run coercion smoke")
    for value, expected, label in (
        (None, True, "omitted/None is report-only"),
        (True, True, "JSON true is preserved"),
        (False, False, "JSON false is preserved"),
        ("true", True, "spelled true is preserved"),
        (" false ", False, "trimmed spelled false is preserved"),
    ):
        _expect(coerce_dry_run(value), expected, label)
    _expect_refused("", "empty string")
    _expect_refused(0, "zero")
    _expect_refused("misspelled", "misspelled string")
    print("all shared dry_run coercion vectors passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
