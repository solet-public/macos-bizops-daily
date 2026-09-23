#!/usr/bin/env python3
"""Regression for the gate-smoke registration completeness census.

The negative control creates an actual untracked ``*_smoke.py`` in a temporary
Git repository.  The census must reject it until a distinct, tagged exclusion
is added; merely having a gate register is not enough.
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from quality_gates.gate_smoke_registration_gate import inspect_registration  # noqa: E402

_TODAY = date(2026, 9, 20)
_FAILURES: list[str] = []


def check(name: str, condition: bool) -> None:
    print(f"  [{'PASS' if condition else 'FAIL'}] {name}")
    if not condition:
        _FAILURES.append(name)


def _write(path: Path, text: str = "def main() -> int:\n    return 0\n") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _git(root: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=root, check=True, capture_output=True, text=True)


def _fixture() -> tempfile.TemporaryDirectory[str]:
    fixture = tempfile.TemporaryDirectory(prefix="gate_smoke_registration_")
    root = Path(fixture.name)
    _git(root, "init", "-q")
    _write(root / "quality_gates/tests/registered_smoke.py")
    _write(
        root / "quality_gates/gate_smokes.txt",
        "quality_gates/tests/registered_smoke.py\n",
    )
    _write(
        root / "quality_gates/gate_smoke_exclusions.txt",
        "# Exact deliberately-excluded smoke paths.\n",
    )
    _git(root, "add", ".")
    return fixture


def test_registered_and_excluded_partition_is_clean() -> None:
    with _fixture() as tmp:
        report = inspect_registration(Path(tmp), today=_TODAY)
    check("registered smoke is accepted", report.unregistered == ())
    check("no stale exclusion exists", report.stale_exclusions == ())


def test_new_untracked_smoke_fails_until_explicitly_excluded() -> None:
    with _fixture() as tmp:
        root = Path(tmp)
        forgotten = "quality_gates/tests/forgotten_smoke.py"
        _write(root / forgotten)
        before = inspect_registration(root, today=_TODAY)
        check("temporary untracked smoke is caught", before.unregistered == (forgotten,))

        exclusions = root / "quality_gates/gate_smoke_exclusions.txt"
        exclusions.write_text(
            exclusions.read_text(encoding="utf-8")
            + f"{forgotten}  # owner: iss_fb75bf89 reason: fixture-only deliberate exclusion expires: 2026-12-31\n",
            encoding="utf-8",
        )
        after = inspect_registration(root, today=_TODAY)
        check("explicit tagged exclusion clears only the named smoke", after.unregistered == ())
        check("new exclusion is not stale", after.stale_exclusions == ())


def test_overlap_and_stale_exclusion_fail_closed() -> None:
    with _fixture() as tmp:
        root = Path(tmp)
        exclusions = root / "quality_gates/gate_smoke_exclusions.txt"
        exclusions.write_text(
            "quality_gates/tests/registered_smoke.py  # owner: iss_fb75bf89 reason: fixture overlap expires: 2026-12-31\n"
            "quality_gates/tests/removed_smoke.py  # owner: iss_fb75bf89 reason: fixture stale entry expires: 2026-12-31\n",
            encoding="utf-8",
        )
        report = inspect_registration(root, today=_TODAY)
    check("registered/excluded overlap is reported", report.overlaps == ("quality_gates/tests/registered_smoke.py",))
    check("removed smoke exclusion is stale", report.stale_exclusions == ("quality_gates/tests/removed_smoke.py",))


def main() -> int:
    print("Gate-smoke registration completeness smoke\n")
    test_registered_and_excluded_partition_is_clean()
    test_new_untracked_smoke_fails_until_explicitly_excluded()
    test_overlap_and_stale_exclusion_fail_closed()
    if _FAILURES:
        print(f"\nFAILED: {_FAILURES}")
        return 1
    print("\nALL CHECKS PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
