#!/usr/bin/env python3
"""Focused proof for the explicit N-aware keep/compact/clear boundary."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
SRC = REPO_ROOT / "plugins" / "agent_messaging_plugin" / "src"
sys.path.insert(0, str(SRC))

from agent_messaging_plugin.rotation_thresholds import (  # noqa: E402
    RotationActionCalibration,
    calculated_context_verdict,
)


def _calibration(action: str, *, h: int, one_time: float) -> RotationActionCalibration:
    return RotationActionCalibration(
        action=action,
        post_action_prefix_tokens=h,
        one_time_cost_units=one_time,
        calibration_profile_id=f"{action}-calibration",
        calibration_profile_version="v1",
    )


def _verdict(
    *,
    current: int = 200_000,
    ceiling: int = 258_400,
    calls: int = 10,
    cache_state: str = "observed_warm",
    multiplier: float | None = 0.1,
    calibrations: tuple[RotationActionCalibration, ...] | None = None,
):
    return calculated_context_verdict(
        current_tokens=current,
        ceiling=ceiling,
        expected_calls_after=calls,
        cache_state=cache_state,
        cache_read_multiplier=multiplier,
        calibrations=(
            (_calibration("compact", h=25_844, one_time=40_000),
             _calibration("clear", h=40_000, one_time=100_000))
            if calibrations is None
            else calibrations
        ),
        required_actions=("compact", "clear"),
        capability_profile_id="openai-gpt-5.6-capabilities-v1",
        capability_profile_version="v1",
        usage_economics_profile_id="openai-metered-gpt-5.6-sol-2026-08-22",
        usage_economics_profile_version="v1",
        objective="metered_api:minimize_expected_monetary_cost",
    )


class ContextRotationCalculationSmoke(unittest.TestCase):
    def test_compact_clear_and_keep_use_distinct_measured_h_and_cost(self) -> None:
        verdict = _verdict()
        self.assertTrue(verdict.resolved)
        self.assertEqual(verdict.chosen_action, "compact")
        by_action = {row.action: row for row in verdict.action_evaluations}
        self.assertEqual(by_action["compact"].post_action_prefix_tokens, 25_844)
        self.assertEqual(by_action["clear"].post_action_prefix_tokens, 40_000)
        self.assertNotEqual(
            by_action["compact"].projected_total_cost_units,
            by_action["clear"].projected_total_cost_units,
        )

    def test_unknown_cache_and_missing_compact_calibration_are_unresolved(self) -> None:
        unknown = _verdict(cache_state="unknown", multiplier=None)
        self.assertFalse(unknown.resolved)
        self.assertIn("cache state", unknown.economic_cause)

        missing = _verdict(calibrations=(_calibration("clear", h=40_000, one_time=100_000),))
        self.assertFalse(missing.resolved)
        self.assertIn("compact", missing.economic_cause)

    def test_insufficient_calls_keep_and_exact_break_even_tie_keep(self) -> None:
        insufficient = _verdict(calls=0)
        self.assertEqual(insufficient.chosen_action, "keep")

        # keep = N*C*r = 100; compact = one_time + N*H*r = 100.
        exact = _verdict(
            current=1_000,
            ceiling=10_000,
            calls=1,
            calibrations=(
                _calibration("compact", h=500, one_time=50),
                _calibration("clear", h=400, one_time=1_000),
            ),
        )
        self.assertEqual(exact.economic_choice, "keep")
        self.assertEqual(exact.chosen_action, "keep")
        self.assertEqual(exact.action_evaluations[0].break_even_horizon_calls, 1.0)

    def test_capacity_pressure_is_separate_and_can_override_keep(self) -> None:
        verdict = _verdict(
            current=235_000,
            calls=0,
        )
        self.assertEqual(verdict.capacity_band, "capacity_critical")
        self.assertEqual(verdict.economic_choice, "keep")
        self.assertEqual(verdict.chosen_action, "compact")
        self.assertIn("room running out", verdict.capacity_cause)

    def test_unknown_or_non_positive_capacity_is_refused(self) -> None:
        with self.assertRaisesRegex(ValueError, "ceiling must be positive"):
            _verdict(ceiling=0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
