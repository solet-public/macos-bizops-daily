"""Hermetic engine proof for interrupted apply resume without duplicate mutation."""

from __future__ import annotations

import sys

import discovered_decision_support as support
from discovered_decision_scenarios import run_journal_resume_round_trip_scenario


def main() -> int:
    run_journal_resume_round_trip_scenario()
    print(
        "journal_resume_round_trip_smoke OK: "
        f"{support._CHECKS} checks passed"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
