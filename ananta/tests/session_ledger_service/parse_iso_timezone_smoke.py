#!/usr/bin/env python3
"""M6.5 Bug 1 smoke — ``_parse_iso`` UTC normalization.

Run:

    .venv/bin/python3 ananta/tests/session_ledger_service/parse_iso_timezone_smoke.py

Per 2026-06-11 M6.5 Bug 1 (Coordinator-Dawn dispatch §3): the
``_parse_iso`` helper at
``ananta/src/ananta/services/session_ledger_service/service.py:_parse_iso``
silently coerced naïve datetimes through ``fromisoformat``. A
UTC-aware filter value then compared against a possibly-naïve persisted
value let pre-cutoff sessions through (operator empirical evidence
2026-06-11 PT). Post-fix the helper:

* Returns a UTC-aware ``datetime`` for any zoned ISO input.
* Raises ``ValueError`` on a naïve input.
* Normalizes equivalent zoned inputs (``Z`` / ``+00:00`` / non-UTC
  offsets that compute to the same instant) to the same UTC value.

Verifications:

1. UTC-aware ``'Z'`` suffix returns aware UTC.
2. UTC-aware ``'+00:00'`` suffix returns aware UTC.
3. Non-UTC zoned input (``'-08:00'``) returns aware UTC at the equivalent instant.
4. Three differently-spelled instants for the same wall moment yield
   identical ``datetime`` values.
5. Naïve input raises ``ValueError``.
"""

from __future__ import annotations

import sys
from datetime import UTC, datetime
from pathlib import Path

# Allow running from the repo root or anywhere — wire ``ananta/src`` onto sys.path.
_REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(_REPO_ROOT / "ananta" / "src"))

from ananta.services.session_ledger_service.service import (  # noqa: E402
    _parse_iso,
)


def _expect(condition: bool, message: str) -> None:
    if not condition:
        print(f"FAIL: {message}", file=sys.stderr)
        sys.exit(1)
    print(f"PASS: {message}")


def test_z_suffix_returns_utc_aware() -> None:
    got = _parse_iso("2025-11-01T00:00:00Z")
    _expect(got.tzinfo is not None, "Z-suffix input returns tz-aware datetime")
    _expect(got.utcoffset() == UTC.utcoffset(got), "Z-suffix input is UTC zone")
    _expect(
        got == datetime(2025, 11, 1, 0, 0, 0, tzinfo=UTC),
        "Z-suffix input parses to the literal UTC instant",
    )


def test_plus_zero_zero_returns_utc_aware() -> None:
    got = _parse_iso("2025-11-01T00:00:00+00:00")
    _expect(got.tzinfo is not None, "+00:00 input returns tz-aware datetime")
    _expect(
        got == datetime(2025, 11, 1, 0, 0, 0, tzinfo=UTC),
        "+00:00 input parses to the literal UTC instant",
    )


def test_non_utc_zone_normalizes_to_utc() -> None:
    got = _parse_iso("2025-10-31T16:00:00-08:00")
    expected = datetime(2025, 11, 1, 0, 0, 0, tzinfo=UTC)
    _expect(got == expected, "-08:00 input normalizes to equivalent UTC instant")


def test_equivalent_inputs_yield_identical_datetimes() -> None:
    a = _parse_iso("2025-11-01T00:00:00Z")
    b = _parse_iso("2025-11-01T00:00:00+00:00")
    c = _parse_iso("2025-10-31T16:00:00-08:00")
    _expect(
        a == b == c,
        "three equivalent zoned spellings yield identical datetimes",
    )


def test_naive_input_raises_valueerror() -> None:
    raised = False
    try:
        _parse_iso("2025-11-01T00:00:00")
    except ValueError as exc:
        raised = True
        message = str(exc)
        _expect(
            "naïve" in message or "naive" in message or "timezone" in message,
            f"ValueError message names the naïve-input problem; got {message!r}",
        )
    _expect(raised, "naïve input raises ValueError")


def main() -> None:
    print("M6.5 Bug 1 — _parse_iso UTC normalization smoke")
    print("=" * 60)
    test_z_suffix_returns_utc_aware()
    test_plus_zero_zero_returns_utc_aware()
    test_non_utc_zone_normalizes_to_utc()
    test_equivalent_inputs_yield_identical_datetimes()
    test_naive_input_raises_valueerror()
    print("=" * 60)
    print("ALL PASS")


if __name__ == "__main__":
    main()
