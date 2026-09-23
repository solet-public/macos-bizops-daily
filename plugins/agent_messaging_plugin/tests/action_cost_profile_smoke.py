#!/usr/bin/env python3
"""Red-first contract for measured, action-specific cost calibrations."""

from __future__ import annotations

import copy
import json
import sys
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
SRC = REPO_ROOT / "plugins" / "agent_messaging_plugin" / "src"
DATA = REPO_ROOT / "plugins" / "agent_messaging_plugin" / "model_profiles"
sys.path.insert(0, str(SRC))

from agent_messaging_plugin import rotation_thresholds  # noqa: E402
from agent_messaging_plugin.action_cost_profile import (  # noqa: E402
    ActionCalibrationUnavailableError,
    ActionCostProfileValidationError,
    load_action_cost_profile_catalog,
)

AS_OF = datetime(2026, 8, 22, 23, 59, tzinfo=UTC)


class ActionCostProfileSmoke(unittest.TestCase):
    def setUp(self) -> None:
        self.path = DATA / "context_action_costs.v1.json"
        self.catalog = load_action_cost_profile_catalog(self.path, as_of=AS_OF)

    def test_codex_compact_prefix_is_measured_but_economics_are_unpriced(self) -> None:
        compact = self.catalog.resolve(
            "openai", "codex", "gpt-5.6-sol", "xhigh", "compact",
        )
        self.assertEqual(compact.post_action_prefix_tokens, 25_844)
        self.assertEqual(compact.post_action_prefix.sample_count, 1)
        self.assertFalse(compact.priced)
        with self.assertRaisesRegex(
            ActionCalibrationUnavailableError,
            "compact.*action cost.*not measured",
        ):
            compact.require_priced()

    def test_codex_clear_stays_explicitly_unmeasured(self) -> None:
        clear = self.catalog.resolve(
            "openai", "codex", "gpt-5.6-sol", "xhigh", "clear",
        )
        self.assertIsNone(clear.post_action_prefix_tokens)
        self.assertFalse(clear.priced)
        self.assertIn("disposable", clear.applicability_limits)

    def test_claude_profile_preserves_the_live_h_components(self) -> None:
        clear = self.catalog.resolve(
            "anthropic", "claude_code", "claude-fable-5", "high", "clear",
        )
        self.assertEqual(clear.boot_prefix.estimate_tokens, 43_474)
        self.assertEqual(clear.rehydration_prefix.estimate_tokens, 38_415)
        self.assertEqual(clear.post_action_prefix_tokens, 81_889)
        self.assertEqual(clear.post_action_prefix_tokens, rotation_thresholds.POLICY_H_TOKENS)
        self.assertTrue(clear.priced)
        self.assertEqual(clear.cache_read_multiplier, 0.1)
        self.assertEqual(clear.cache_write_multiplier, 2.0)

    def test_sample_count_and_cross_scope_reuse_fail_loud(self) -> None:
        raw = json.loads(self.path.read_text(encoding="utf-8"))
        compact = next(row for row in raw["profiles"] if row["action"] == "compact")
        compact["action_prefix"]["sample_count"] = 2
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "bad.json"
            path.write_text(json.dumps(raw), encoding="utf-8")
            with self.assertRaisesRegex(ActionCostProfileValidationError, "sample_count"):
                load_action_cost_profile_catalog(path, as_of=AS_OF)

        with self.assertRaisesRegex(
            ActionCalibrationUnavailableError,
            "no action calibration.*gpt-5.6-terra.*xhigh.*compact",
        ):
            self.catalog.resolve(
                "openai", "codex", "gpt-5.6-terra", "xhigh", "compact",
            )

    def test_stale_or_provenance_free_calibration_is_rejected(self) -> None:
        base = json.loads(self.path.read_text(encoding="utf-8"))
        for key, value, expected in (
            ("refresh_status", "stale", "refresh_status"),
            ("source_ref", "", "source_ref"),
        ):
            with self.subTest(key=key), tempfile.TemporaryDirectory() as tmp:
                raw = copy.deepcopy(base)
                raw["provenance"][key] = value
                path = Path(tmp) / "bad.json"
                path.write_text(json.dumps(raw), encoding="utf-8")
                with self.assertRaisesRegex(ActionCostProfileValidationError, expected):
                    load_action_cost_profile_catalog(path, as_of=AS_OF)

        future = copy.deepcopy(base)
        future["profiles"][0]["measured_at"] = "2026-08-23T00:00:00+00:00"
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "future.json"
            path.write_text(json.dumps(future), encoding="utf-8")
            with self.assertRaisesRegex(ActionCostProfileValidationError, "future"):
                load_action_cost_profile_catalog(path, as_of=AS_OF)

    def test_priced_requires_every_measurement_population_and_normalization(self) -> None:
        raw = json.loads(self.path.read_text(encoding="utf-8"))
        clear = next(
            row
            for row in raw["profiles"]
            if row["runtime"] == "codex" and row["action"] == "clear"
        )
        clear["priced"] = True
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "bad.json"
            path.write_text(json.dumps(raw), encoding="utf-8")
            with self.assertRaisesRegex(
                ActionCostProfileValidationError,
                "priced.*boot_prefix.*rehydration_prefix.*action_prefix",
            ):
                load_action_cost_profile_catalog(path, as_of=AS_OF)


if __name__ == "__main__":
    unittest.main(verbosity=2)
