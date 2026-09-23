#!/usr/bin/env python3
"""Red-first contract for injected metered and flat-rate objectives."""

from __future__ import annotations

import copy
import json
import sys
import tempfile
import unittest
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
SRC = REPO_ROOT / "plugins" / "agent_messaging_plugin" / "src"
DATA = REPO_ROOT / "plugins" / "agent_messaging_plugin" / "model_profiles"
sys.path.insert(0, str(SRC))

from agent_messaging_plugin.usage_economics import (  # noqa: E402
    AllowancePool,
    FlatRateQuotaStrategy,
    MeteredApiStrategy,
    ProjectedAction,
    RotationDecisionInput,
    UsageEconomicsProfileValidationError,
    UsageVector,
    load_usage_economics_profile_catalog,
)

AS_OF = datetime(2026, 9, 22, 12, 0, tzinfo=UTC)


def _action(
    name: str,
    *,
    input_tokens: int = 0,
    cached_tokens: int = 0,
    cache_write_tokens: int = 0,
    output_tokens: int = 0,
    reasoning_tokens: int = 0,
    latency: float = 0.0,
    accepted_work: float = 10.0,
    quota_by_pool: dict[str, float | None] | None = None,
) -> ProjectedAction:
    return ProjectedAction(
        name=name,
        total_usage=UsageVector(
            input_tokens=input_tokens,
            cached_input_tokens=cached_tokens,
            cache_write_input_tokens=cache_write_tokens,
            output_tokens=output_tokens,
            reasoning_output_tokens=reasoning_tokens,
            tool_calls=0,
        ),
        post_action_context_tokens=100_000,
        latency_seconds=latency,
        quality_score=1.0,
        accepted_work_units=accepted_work,
        quota_by_pool={} if quota_by_pool is None else quota_by_pool,
    )


class UsageEconomicsSmoke(unittest.TestCase):
    def setUp(self) -> None:
        self.path = DATA / "usage_economics.v1.json"
        raw = json.loads(self.path.read_text(encoding="utf-8"))
        newer_rows = [
            f"{row['profile_id']} effective_at={row['effective_at']}"
            for row in raw["profiles"]
            if datetime.fromisoformat(row["effective_at"].replace("Z", "+00:00")) > AS_OF
        ]
        try:
            self.catalog = load_usage_economics_profile_catalog(self.path, as_of=AS_OF)
        except UsageEconomicsProfileValidationError as exc:
            self.fail(
                f"usage economics test AS_OF={AS_OF.isoformat()} is older than catalog row(s): "
                f"{newer_rows or ['none identified']}; catalog error: {exc}",
            )

    def test_current_metered_prices_are_fetched_profile_data(self) -> None:
        sol = self.catalog.resolve("openai-metered-gpt-5.6-sol-2026-08-22")
        self.assertEqual(sol.input_per_mtok, 4.0)
        self.assertEqual(sol.cached_input_per_mtok, 0.4)
        self.assertEqual(sol.cache_write_per_mtok, 5.0)
        self.assertEqual(sol.output_per_mtok, 20.0)
        self.assertEqual(sol.refresh_status, "current")
        self.assertIn("developers.openai.com", sol.source_ref)

        sonnet = self.catalog.resolve("anthropic-metered-sonnet-5-2026-08-22")
        self.assertEqual(sonnet.input_per_mtok, 2.0)
        self.assertEqual(sonnet.cached_input_per_mtok, 0.2)
        self.assertEqual(sonnet.cache_write_per_mtok, 2.5)
        self.assertEqual(sonnet.output_per_mtok, 10.0)

    def test_search_snippet_price_is_a_rejected_stale_fixture(self) -> None:
        raw = json.loads(self.path.read_text(encoding="utf-8"))
        stale = next(row for row in raw["profiles"] if row["profile_id"].endswith("stale-snippet"))
        self.assertEqual(stale["refresh_status"], "stale")
        with self.assertRaisesRegex(UsageEconomicsProfileValidationError, "refresh_status"):
            self.catalog.resolve(stale["profile_id"])

    def test_metered_and_flat_rate_can_choose_differently(self) -> None:
        pool_usage = {
            "rolling-five-hour": 1.0,
            "weekly-all-model": 1.0,
            "weekly-model-family": 1.0,
        }
        inputs = RotationDecisionInput(
            provider="anthropic",
            runtime="claude_code",
            model="claude-sonnet-5",
            effort="high",
            current_context_tokens=220_000,
            capacity_tokens=258_400,
            expected_calls_after=20,
            quality_floor=0.9,
            actions=(
                _action(
                    "keep",
                    cached_tokens=4_000_000,
                    accepted_work=10.0,
                    quota_by_pool=pool_usage,
                ),
                _action(
                    "clear",
                    cached_tokens=600_000,
                    cache_write_tokens=100_000,
                    latency=8.0,
                    accepted_work=9.0,
                    quota_by_pool=pool_usage,
                ),
            ),
        )
        metered_profile = self.catalog.resolve("anthropic-metered-sonnet-5-2026-08-22")
        flat_measured = self._measured_max_profile()
        metered = MeteredApiStrategy(metered_profile).decide(inputs)
        flat = FlatRateQuotaStrategy(flat_measured).decide(inputs)
        self.assertTrue(metered.resolved)
        self.assertTrue(flat.resolved)
        self.assertEqual(metered.chosen_action, "clear")
        self.assertEqual(flat.chosen_action, "keep")
        self.assertEqual(metered.profile_id, metered_profile.profile_id)
        self.assertEqual(flat.profile_id, flat_measured.profile_id)
        self.assertNotEqual(metered.objective, flat.objective)

    def test_flat_rate_missing_quota_is_unknown_not_free_or_unlimited(self) -> None:
        template = self.catalog.resolve("anthropic-max-20x-2026-08-22")
        result = FlatRateQuotaStrategy(template).decide(
            RotationDecisionInput(
                provider="anthropic",
                runtime="claude_code",
                model="claude-sonnet-5",
                effort="high",
                current_context_tokens=100_000,
                capacity_tokens=258_400,
                expected_calls_after=5,
                quality_floor=0.9,
                actions=(_action("keep"), _action("clear")),
            ),
        )
        self.assertFalse(result.resolved)
        self.assertIsNone(result.chosen_action)
        self.assertIn("quota", result.explanation)
        self.assertNotIn("free", result.explanation.lower())
        self.assertIn("neither zero marginal cost nor unlimited", result.explanation.lower())

    def test_max_plan_all_pools_apply_and_most_constrained_drives(self) -> None:
        profile = self._measured_max_profile(
            remaining_by_pool={
                "rolling-five-hour": 50.0,
                "weekly-all-model": 30.0,
                "weekly-model-family": 5.0,
            },
        )
        usage = {
            "rolling-five-hour": 1.0,
            "weekly-all-model": 1.0,
            "weekly-model-family": 1.0,
        }
        result = FlatRateQuotaStrategy(profile).decide(
            RotationDecisionInput(
                provider="anthropic",
                runtime="claude_code",
                model="claude-sonnet-5",
                effort="high",
                current_context_tokens=100_000,
                capacity_tokens=1_000_000,
                expected_calls_after=10,
                quality_floor=0.9,
                actions=(_action("keep", quota_by_pool=usage),),
            ),
        )
        self.assertTrue(result.resolved)
        self.assertEqual(result.binding_pool_id, "weekly-model-family")
        self.assertIn("fixed_weekly", result.explanation)
        self.assertIn("provider_reported_usage", result.explanation)

    def test_usage_credit_crossover_is_data_selected_or_capacity_blocked(self) -> None:
        depleted = self._measured_max_profile(remaining_by_pool={
            "rolling-five-hour": 0.0,
            "weekly-all-model": 0.0,
            "weekly-model-family": 0.0,
        })
        usage = {pool.pool_id: 1.0 for pool in depleted.allowance_pools}
        inputs = RotationDecisionInput(
            provider="anthropic",
            runtime="claude_code",
            model="claude-sonnet-5",
            effort="high",
            current_context_tokens=100_000,
            capacity_tokens=1_000_000,
            expected_calls_after=5,
            quality_floor=0.9,
            actions=(
                _action("keep", cached_tokens=2_000_000, quota_by_pool=usage),
                _action("clear", input_tokens=50_000, quota_by_pool=usage),
            ),
        )
        enabled = replace(depleted, overage_enabled=True)
        crossover = FlatRateQuotaStrategy(enabled, catalog=self.catalog).decide(inputs)
        self.assertTrue(crossover.resolved)
        self.assertEqual(crossover.chosen_action, "clear")
        self.assertEqual(
            crossover.crossover_profile_id,
            "anthropic-metered-sonnet-5-2026-08-22",
        )
        self.assertIn("usage-credit crossover", crossover.explanation)

        blocked = FlatRateQuotaStrategy(
            replace(depleted, overage_enabled=False),
            catalog=self.catalog,
        ).decide(inputs)
        self.assertFalse(blocked.resolved)
        self.assertEqual(blocked.status, "capacity_blocked")
        self.assertIsNone(blocked.chosen_action)

    def test_invented_conversion_stale_reset_and_missing_reading_are_refused(self) -> None:
        base = json.loads(self.path.read_text(encoding="utf-8"))
        max_row = next(
            row for row in base["profiles"]
            if row["profile_id"] == "anthropic-max-20x-2026-08-22"
        )
        mutations = {
            "token conversion": lambda row: row["allowance_pools"][0].update(
                {"token_conversion": 1000},
            ),
            "stale reset": lambda row: row["allowance_pools"][0].update(
                {
                    "reading_status": "current",
                    "remaining": 10,
                    "next_reset_at": "2026-08-22T20:00:00+00:00",
                },
            ),
        }
        for expected, mutate in mutations.items():
            with self.subTest(expected=expected), tempfile.TemporaryDirectory() as tmp:
                row = copy.deepcopy(max_row)
                mutate(row)
                path = Path(tmp) / "bad.json"
                path.write_text(
                    json.dumps({"schema_version": 1, "profiles": [row]}),
                    encoding="utf-8",
                )
                with self.assertRaisesRegex(UsageEconomicsProfileValidationError, expected):
                    load_usage_economics_profile_catalog(path, as_of=AS_OF)

        missing = self._measured_max_profile()
        pools = list(missing.allowance_pools)
        pools[0] = replace(pools[0], reading_status="unavailable", remaining=None)
        usage = {pool.pool_id: 1.0 for pool in pools}
        result = FlatRateQuotaStrategy(replace(missing, allowance_pools=tuple(pools))).decide(
            RotationDecisionInput(
                provider="anthropic",
                runtime="claude_code",
                model="claude-sonnet-5",
                effort="high",
                current_context_tokens=100_000,
                capacity_tokens=1_000_000,
                expected_calls_after=5,
                quality_floor=0.9,
                actions=(_action("keep", quota_by_pool=usage),),
            ),
        )
        self.assertFalse(result.resolved)
        self.assertIn("rolling-five-hour", result.explanation)
        self.assertIn("unavailable", result.explanation)

    def test_missing_used_price_refuses_a_dollar_verdict(self) -> None:
        profile = self.catalog.resolve("openai-metered-gpt-5.6-sol-2026-08-22")
        missing = replace(profile, reasoning_output_per_mtok=None)
        inputs = RotationDecisionInput(
            provider="openai",
            runtime="codex",
            model="gpt-5.6-sol",
            effort="xhigh",
            current_context_tokens=100_000,
            capacity_tokens=258_400,
            expected_calls_after=5,
            quality_floor=0.9,
            actions=(_action("keep", reasoning_tokens=10_000),),
        )
        result = MeteredApiStrategy(missing).decide(inputs)
        self.assertFalse(result.resolved)
        self.assertIn("reasoning_output_per_mtok", result.explanation)

    def test_exact_metered_tie_keeps_and_exposes_versioned_constraints(self) -> None:
        profile = self.catalog.resolve("openai-metered-gpt-5.6-sol-2026-08-22")
        inputs = RotationDecisionInput(
            provider="openai",
            runtime="codex",
            model="gpt-5.6-sol",
            effort="xhigh",
            current_context_tokens=100_000,
            capacity_tokens=258_400,
            expected_calls_after=5,
            quality_floor=0.9,
            actions=(_action("compact"), _action("keep")),
        )
        result = MeteredApiStrategy(profile).decide(inputs)
        self.assertTrue(result.resolved)
        self.assertEqual(result.chosen_action, "keep")
        self.assertEqual(result.explanation_version, "usage-economics-explanation-v1")
        self.assertIn("capacity_tokens=258400", result.projected_constraints)
        self.assertEqual(result.applicable_pool_ids, ())

    def test_price_data_changes_the_choice_without_engine_changes(self) -> None:
        base = self.catalog.resolve("openai-metered-gpt-5.6-sol-2026-08-22")
        cheap_output = replace(base, input_per_mtok=4.0, output_per_mtok=1.0)
        expensive_output = replace(base, input_per_mtok=1.0, output_per_mtok=1_000.0)
        inputs = RotationDecisionInput(
            provider="openai",
            runtime="codex",
            model="gpt-5.6-sol",
            effort="xhigh",
            current_context_tokens=200_000,
            capacity_tokens=258_400,
            expected_calls_after=10,
            quality_floor=0.9,
            actions=(
                _action("compact", output_tokens=1_000),
                _action("clear", input_tokens=100_000),
            ),
        )
        self.assertEqual(MeteredApiStrategy(cheap_output).decide(inputs).chosen_action, "compact")
        self.assertEqual(MeteredApiStrategy(expensive_output).decide(inputs).chosen_action, "clear")

    def test_stale_future_or_provenance_free_profiles_fail_loud(self) -> None:
        base = json.loads(self.path.read_text(encoding="utf-8"))
        current = next(
            row for row in base["profiles"]
            if row["profile_id"] == "openai-metered-gpt-5.6-sol-2026-08-22"
        )
        mutations = {
            "refresh_status": lambda row: row.update({"refresh_status": "stale"}),
            "source_ref": lambda row: row.update({"source_ref": ""}),
            "not effective": lambda row: row.update(
                {"effective_at": (AS_OF + timedelta(seconds=1)).isoformat()},
            ),
        }
        for expected, mutate in mutations.items():
            with self.subTest(expected=expected), tempfile.TemporaryDirectory() as tmp:
                raw = {"schema_version": 1, "profiles": [copy.deepcopy(current)]}
                mutate(raw["profiles"][0])
                path = Path(tmp) / "bad.json"
                path.write_text(json.dumps(raw), encoding="utf-8")
                with self.assertRaisesRegex(UsageEconomicsProfileValidationError, expected):
                    load_usage_economics_profile_catalog(path, as_of=AS_OF)

    def _measured_max_profile(
        self,
        *,
        remaining_by_pool: dict[str, float] | None = None,
    ):
        template = self.catalog.resolve("anthropic-max-20x-2026-08-22")
        remaining = remaining_by_pool or {
            "rolling-five-hour": 80.0,
            "weekly-all-model": 70.0,
            "weekly-model-family": 60.0,
        }
        reset_by_kind = {
            "rolling_window": AS_OF + timedelta(hours=5),
            "fixed_weekly": AS_OF + timedelta(days=7),
        }
        pools: list[AllowancePool] = []
        for pool in template.allowance_pools:
            pools.append(replace(
                pool,
                limit_status="known",
                limit=100.0,
                reading_status="current",
                remaining=remaining[pool.pool_id],
                next_reset_at=reset_by_kind[pool.reset_kind],
            ))
        return replace(
            template,
            profile_id="anthropic-max-20x-measured-fixture",
            allowance_pools=tuple(pools),
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
