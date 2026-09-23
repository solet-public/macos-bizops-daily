#!/usr/bin/env python3
"""Owed-delivery: store-naive vs runtime-aware datetime tz-mismatch fix.

The state store's ``DATETIME`` columns are ``timestamp without time zone``, so
every timestamp read back (``created_at``, ``last_emitted_at``) is offset-NAIVE
UTC — while ``_clock()`` (= ``datetime.now(UTC)``) and the bridge activity stamp
are offset-AWARE UTC. Code that compared the two directly raised
``TypeError: can't compare/subtract offset-naive and offset-aware datetimes``.

The fix: ``_parse_iso`` coerces a naive stored cell → aware UTC (honoring its own
docstring), the single boundary, so every comparison in the module is
aware-vs-aware UTC.

RED-FIRST FIDELITY (the reason this shipped): the existing deaf-wake smokes seed
timestamps via ``datetime(..., tzinfo=UTC).isoformat()`` — AWARE strings — so
their fake never reproduced the naive-on-read the real ``timestamp without time
zone`` column produces. These tests seed timestamps as NAIVE strings exactly as
the column returns them (no offset) and pass an AWARE ``now``. Revert the
``_parse_iso`` coercion and each site test raises ``TypeError`` (verified by
hand).

A4 (2026-08-04): pruned from three sites to the two that survive marker
retirement. ``_escalatable`` (the sweep-tick escalation site) and
``_stamp_consumed_rows``/``reconcile_role_consumption`` (the Guard-1
consumption-reconcile site) retired with the apparatus they served — see
``workbench/2026-08-04_marker_retirement_architect_memo_architect.md``
Amendment 4. ``_parse_iso`` itself and ``_within_reemit_window`` (shared by
the surviving role-replay drain, ``list_undelivered_for_instance``) are
untouched by that retirement and still need this exact naive-vs-aware
coverage.

Run:
    SOLET_NAME=<name>-test .venv/bin/python3 \\
        plugins/agent_messaging_plugin/tests/deaf_wake_naive_datetime_smoke.py
"""

from __future__ import annotations

import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT / "ananta" / "src"))
sys.path.insert(0, str(REPO_ROOT / "plugins" / "agent_messaging_plugin" / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from ananta.llm.agent_messaging.service import (  # noqa: E402
    _parse_iso,
    _within_reemit_window,
)

# T0 as the store returns it: a NAIVE (offset-less) UTC wall-clock string — the
# exact shape a ``timestamp without time zone`` column reads back (cf. psql:
# ``2026-07-07 12:00:00.000000``). ``datetime.fromisoformat`` parses it naive.
NAIVE_T0 = "2026-07-07 12:00:00.000000"
NAIVE_T0_T = "2026-07-07T12:00:00.000000"  # 'T' separator variant, also naive
# The live datetimes the drain compares against are AWARE UTC.
NOW_AWARE = datetime(2026, 7, 7, 13, 0, 0, tzinfo=UTC)  # T0 + 1h

_passed = 0
_failed: list[str] = []


def _check(condition: object, label: str) -> None:
    global _passed
    if condition:
        _passed += 1
        print(f"  PASS  {label}")
        return
    _failed.append(label)
    print(f"  FAIL  {label}")


# ---------------------------------------------------------------------------
# The fix contract: _parse_iso coerces a naive stored cell to aware UTC
# ---------------------------------------------------------------------------


def test_parse_iso_coerces_naive_stored_cell_to_aware_utc() -> None:
    space = _parse_iso(NAIVE_T0)
    _check(
        space is not None and space.tzinfo is not None
        and space.utcoffset() == timedelta(0),
        "_parse_iso: naive space-separated stored cell → AWARE UTC",
    )
    tee = _parse_iso(NAIVE_T0_T)
    _check(
        tee is not None and tee.tzinfo is not None,
        "_parse_iso: naive 'T'-separated stored cell → AWARE UTC",
    )
    already = _parse_iso("2026-07-07T12:00:00+00:00")
    _check(
        already is not None and already.utcoffset() == timedelta(0),
        "_parse_iso: an already-aware cell stays aware UTC (idempotent)",
    )
    _check(
        _parse_iso("") is None and _parse_iso(None) is None
        and _parse_iso("not-a-date") is None,
        "_parse_iso: empty / None / unparseable → None (unchanged)",
    )


# ---------------------------------------------------------------------------
# Site — the DRAIN re-emit window: _within_reemit_window(now_aware - emitted_naive)
# — in list_undelivered_for_instance (the surviving role-replay drain)
# ---------------------------------------------------------------------------


def test_within_reemit_window_no_fault_on_naive_last_emitted() -> None:
    # emitted 59 min before now → 60s... actually 2026-07-07 12:59 vs 13:00 = 60s.
    recent_naive = "2026-07-07 12:59:00.000000"
    try:
        within = _within_reemit_window(recent_naive, NOW_AWARE, 300.0)
        raised = False
    except TypeError:
        within = None
        raised = True
    _check(
        not raised and within is True,
        "DRAIN: _within_reemit_window handles a NAIVE last_emitted_at "
        "(60s < 300s window → within) with no TypeError — the re-emit-window site",
    )
    # A far-past naive emission is OUTSIDE the window (eligible to re-emit).
    _check(
        _within_reemit_window(NAIVE_T0, NOW_AWARE, 300.0) is False,
        "DRAIN: a far-past NAIVE last_emitted_at is outside the re-emit window",
    )


def main() -> None:
    print("=== owed-delivery naive-vs-aware datetime tz-mismatch smoke ===")
    test_parse_iso_coerces_naive_stored_cell_to_aware_utc()
    test_within_reemit_window_no_fault_on_naive_last_emitted()
    print(f"\n{_passed} passed, {len(_failed)} failed")
    if _failed:
        for label in _failed:
            print(f"  FAILED: {label}")
        sys.exit(1)


if __name__ == "__main__":
    main()
