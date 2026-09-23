#!/usr/bin/env python3
"""Red-first contract for declarative provider/model capability profiles."""

from __future__ import annotations

import copy
import hashlib
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

from agent_messaging_plugin.model_profile import (  # noqa: E402
    ProfileValidationError,
    UnsupportedEffortError,
    load_model_profile_catalog,
)

AS_OF = datetime(2026, 8, 22, 23, 59, tzinfo=UTC)


class ModelProfileSmoke(unittest.TestCase):
    def setUp(self) -> None:
        self.path = DATA / "openai_gpt_5_6_capabilities.v1.json"
        self.catalog = load_model_profile_catalog(self.path, as_of=AS_OF)

    def test_current_catalog_is_capability_only_and_source_grounded(self) -> None:
        raw = json.loads(self.path.read_text(encoding="utf-8"))
        serialized = json.dumps(raw).lower()
        self.assertNotIn("input_price", serialized)
        self.assertNotIn("output_price", serialized)
        for model_id in ("gpt-5.6-sol", "gpt-5.6-terra", "gpt-5.6-luna"):
            profile = self.catalog.resolve("openai", "codex", model_id)
            self.assertEqual(profile.context_ceiling, 1_050_000)
            self.assertEqual(profile.max_output_tokens, 128_000)
            self.assertEqual(
                profile.supported_efforts,
                ("none", "low", "medium", "high", "xhigh", "max"),
            )
            self.assertEqual(profile.default_effort, "medium")
            self.assertEqual(profile.refresh_status, "current")
            self.assertTrue(profile.source_url.startswith("https://developers.openai.com/"))

    def test_alias_default_and_effort_order_are_data(self) -> None:
        sol = self.catalog.resolve("openai", "codex", "gpt-5.6")
        self.assertEqual(sol.canonical_model_id, "gpt-5.6-sol")
        self.assertEqual(sol.default_effort, "medium")
        self.assertLess(sol.effort_rank("low"), sol.effort_rank("xhigh"))

    def test_unsupported_effort_fails_precisely(self) -> None:
        sol = self.catalog.resolve("openai", "codex", "gpt-5.6-sol")
        with self.assertRaisesRegex(
            UnsupportedEffortError,
            "gpt-5.6-sol.*ultra.*none.*low.*medium.*high.*xhigh.*max",
        ):
            sol.require_effort("ultra")

    def test_future_model_and_effort_are_data_only(self) -> None:
        module_path = SRC / "agent_messaging_plugin" / "model_profile.py"
        before = hashlib.sha256(module_path.read_bytes()).hexdigest()
        raw = json.loads(self.path.read_text(encoding="utf-8"))
        raw["effort_order"].append("ultra")
        future = copy.deepcopy(raw["models"][0])
        future.update(
            {
                "canonical_model_id": "gpt-6-nova",
                "aliases": ["gpt-6"],
                "supported_efforts": [
                    "none", "low", "medium", "high", "xhigh", "max", "ultra",
                ],
                "default_effort": "high",
            },
        )
        raw["models"].append(future)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "future.json"
            path.write_text(json.dumps(raw), encoding="utf-8")
            catalog = load_model_profile_catalog(path, as_of=AS_OF)
        nova = catalog.resolve("openai", "codex", "gpt-6")
        self.assertEqual(nova.canonical_model_id, "gpt-6-nova")
        self.assertEqual(nova.require_effort("ultra"), "ultra")
        self.assertEqual(before, hashlib.sha256(module_path.read_bytes()).hexdigest())

    def test_bad_capacity_provenance_and_effective_state_fail_loud(self) -> None:
        base = json.loads(self.path.read_text(encoding="utf-8"))
        mutations = {
            "non-positive context_ceiling": lambda row: row["models"][0].update(
                {"context_ceiling": 0},
            ),
            "source_url": lambda row: row["provenance"].update({"source_url": ""}),
            "refresh_status": lambda row: row["provenance"].update(
                {"refresh_status": "stale"},
            ),
            "not effective": lambda row: row.update(
                {"effective_at": "2026-08-23T00:00:00+00:00"},
            ),
        }
        for expected, mutate in mutations.items():
            with self.subTest(expected=expected), tempfile.TemporaryDirectory() as tmp:
                raw = copy.deepcopy(base)
                mutate(raw)
                path = Path(tmp) / "bad.json"
                path.write_text(json.dumps(raw), encoding="utf-8")
                with self.assertRaisesRegex(ProfileValidationError, expected):
                    load_model_profile_catalog(path, as_of=AS_OF)


if __name__ == "__main__":
    unittest.main(verbosity=2)
