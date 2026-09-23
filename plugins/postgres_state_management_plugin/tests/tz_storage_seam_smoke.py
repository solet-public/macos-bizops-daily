#!/usr/bin/env python3
"""Task #13 F1 TZ-storage seam smoke.

Per the 2026-06-12 audit-timestamp remediation finding-note §8 (TZ-storage
sub-finding) and Dawn's Phase B authorization for Option F1: the postgres
state-management plugin (and its RDS sibling) strips tz-aware datetimes
to naive UTC at the StateService.execute_sql / transactional boundary
BEFORE handing the params to psycopg2. Without this strip, psycopg2
serializes the tz-aware datetime as TIMESTAMPTZ and Postgres stores it
into a ``timestamp without time zone`` column by applying the server's
session timezone, producing the 7-hour wall-clock skew observed in
Phase 2 between ``__quarantine.restoration_at`` (psycopg2 path) and
``__quarantine.updated_at`` (Postgres trigger path).

Coverage (8 scenarios across postgres + RDS sibling helpers):

1. tz-aware UTC datetime → stripped to naive UTC datetime carrying the
   same wall-clock value.
2. tz-aware non-UTC datetime (PT, +05:30, etc.) → converted to UTC then
   stripped to naive — wall-clock matches the original UTC instant.
3. Naive datetime passes through unchanged (already pre-stripped).
4. Non-datetime params (str, int, None, bool, bytes) pass through
   unchanged.
5. None params input → None output (no allocation).
6. Empty params sequence → unchanged sequence (no allocation).
7. Mixed sequence: only tz-aware datetimes are converted; other
   elements untouched at the same positions.
8. RDS sibling helper mirrors the postgres helper exactly on all of
   the above (scenarios 1-7 run twice).

Per [[sandbox-mutating-smokes]]: pure-function smokes against the
module-level helper; no DB / filesystem side effects.

Run:
    .venv/bin/python3 plugins/postgres_state_management_plugin/tests/tz_storage_seam_smoke.py
"""

from __future__ import annotations

import sys
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT / "ananta" / "src"))
sys.path.insert(
    0,
    str(REPO_ROOT / "plugins" / "postgres_state_management_plugin" / "src"),
)
sys.path.insert(
    0,
    str(REPO_ROOT / "plugins" / "rds_postgres_state_management_plugin" / "src"),
)

from postgres_state_management_plugin.plugin import (  # noqa: E402
    _strip_tz_from_params as pg_strip,
)
from rds_postgres_state_management_plugin.rds_transaction import (  # noqa: E402
    _strip_tz_from_params as rds_strip,
)


def _check(condition: object, label: str) -> None:
    if not condition:
        raise AssertionError(label)


def test_tz_aware_utc_stripped_to_naive_utc(
    strip: object, label: str,
) -> None:
    aware = datetime(2026, 6, 12, 15, 31, 55, 196658, tzinfo=UTC)
    result = strip([aware])  # type: ignore[operator]
    _check(result is not None, f"{label}: None result on non-None input")
    assert result is not None
    landed = result[0]
    _check(
        isinstance(landed, datetime),
        f"{label}: expected datetime in output, got {type(landed).__name__}",
    )
    assert isinstance(landed, datetime)
    _check(
        landed.tzinfo is None,
        f"{label}: expected naive datetime, got tzinfo={landed.tzinfo!r}",
    )
    _check(
        landed.year == 2026
        and landed.month == 6
        and landed.day == 12
        and landed.hour == 15
        and landed.minute == 31
        and landed.second == 55
        and landed.microsecond == 196658,
        f"{label}: wall-clock mismatch — got {landed!r}, expected 2026-06-12 15:31:55.196658",
    )


def test_tz_aware_non_utc_converted_then_stripped(
    strip: object, label: str,
) -> None:
    pt = timezone(timedelta(hours=-7))
    aware_pt = datetime(2026, 6, 12, 8, 31, 55, 196658, tzinfo=pt)
    # PT wall-clock 08:31:55 == UTC wall-clock 15:31:55
    result = strip([aware_pt])  # type: ignore[operator]
    assert result is not None
    landed = result[0]
    assert isinstance(landed, datetime)
    _check(
        landed.tzinfo is None,
        f"{label}: expected naive output, got tzinfo={landed.tzinfo!r}",
    )
    _check(
        landed.hour == 15 and landed.minute == 31 and landed.second == 55,
        f"{label}: expected 15:31:55 (UTC wall-clock), got {landed.hour}:{landed.minute}:{landed.second}",
    )


def test_naive_datetime_passes_through(strip: object, label: str) -> None:
    naive = datetime(2026, 6, 12, 15, 31, 55)
    result = strip([naive])  # type: ignore[operator]
    assert result is not None
    _check(
        result[0] is naive,
        f"{label}: naive datetime should pass through identity",
    )


def test_non_datetime_params_pass_through(
    strip: object, label: str,
) -> None:
    params: list[object] = ["session_id_42", 1234, None, True, b"\x00\x01\x02"]
    result = strip(params)  # type: ignore[operator]
    # Helper returns the input sequence unchanged when nothing was converted.
    _check(
        result is params,
        f"{label}: unchanged input should return the same object (no allocation), got {result!r}",
    )


def test_none_input_returns_none(strip: object, label: str) -> None:
    result = strip(None)  # type: ignore[operator]
    _check(result is None, f"{label}: None input should return None, got {result!r}")


def test_empty_input_passes_through(strip: object, label: str) -> None:
    empty: list[object] = []
    result = strip(empty)  # type: ignore[operator]
    _check(
        result is empty,
        f"{label}: empty list should pass through identity (no allocation)",
    )


def test_mixed_only_tz_aware_converted(strip: object, label: str) -> None:
    aware = datetime(2026, 6, 12, 15, 31, 55, tzinfo=UTC)
    naive = datetime(2025, 1, 1)
    params: list[object] = ["id_99", aware, 42, naive, None]
    result = strip(params)  # type: ignore[operator]
    assert result is not None
    _check(result[0] == "id_99", f"{label}: position 0 string mismatch: {result[0]!r}")
    _check(
        isinstance(result[1], datetime) and result[1].tzinfo is None,
        f"{label}: position 1 (aware) should land naive: {result[1]!r}",
    )
    _check(result[2] == 42, f"{label}: position 2 int mismatch: {result[2]!r}")
    _check(
        result[3] is naive,
        f"{label}: position 3 (naive) should pass identity: {result[3]!r}",
    )
    _check(result[4] is None, f"{label}: position 4 None mismatch: {result[4]!r}")


def run_suite(strip: object, label: str) -> int:
    cases = (
        ("tz_aware_utc_stripped", test_tz_aware_utc_stripped_to_naive_utc),
        ("tz_aware_non_utc_converted", test_tz_aware_non_utc_converted_then_stripped),
        ("naive_passes_through", test_naive_datetime_passes_through),
        ("non_datetime_passes_through", test_non_datetime_params_pass_through),
        ("none_input", test_none_input_returns_none),
        ("empty_input", test_empty_input_passes_through),
        ("mixed_only_tz_aware_converted", test_mixed_only_tz_aware_converted),
    )
    failures = 0
    for name, fn in cases:
        try:
            fn(strip, label)
        except AssertionError as exc:
            sys.stderr.write(f"FAIL {label}::{name}: {exc}\n")
            failures += 1
            continue
        sys.stdout.write(f"OK   {label}::{name}\n")
    return failures


def main() -> int:
    pg_failures = run_suite(pg_strip, "postgres")
    rds_failures = run_suite(rds_strip, "rds")
    total = pg_failures + rds_failures
    if total:
        sys.stderr.write(f"\n{total} TZ-storage seam smoke failure(s).\n")
        return 1
    sys.stdout.write("\nAll Task #13 F1 TZ-storage seam smokes passed (both helpers).\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
