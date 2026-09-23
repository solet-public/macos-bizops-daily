#!/usr/bin/env python3
"""Smoke: the crosscheck reconciler's agreement/conflict logic (iss_d136ae29).

Fixture-based, no network, no state layer -- exercises `reconcile_cell`
directly against hand-built `Reading`s, per the dispatch brief's explicit
verification list: agreeing, disagreeing, missing-second-source, one source
`fetch_status != ok`, and (implicitly) a fully-absent metric.

Run:
    .venv/bin/python3 plugins/agent_messaging_plugin/tests/model_capability_reconcile_smoke.py
"""

from __future__ import annotations

import sys
from pathlib import Path

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from agent_messaging_plugin.model_capability_fetch import Reading  # noqa: E402
from agent_messaging_plugin.model_capability_reconcile import (  # noqa: E402
    CAPABILITY_SCORE_TOLERANCE,
    COST_RELATIVE_TOLERANCE,
    ObservationRef,
    reconcile_cell,
)
from agent_messaging_plugin.schema import (  # noqa: E402
    CELL_ACCEPTANCE_ACCEPTED,
    CELL_ACCEPTANCE_CROSSCHECK_CONFLICT,
    CELL_ACCEPTANCE_PENDING_CROSSCHECK,
)

_CELL = ("claude_code", "claude-sonnet-5", "max")
_passed = 0
_failed: list[str] = []


def _check(condition: object, label: str) -> None:
    global _passed
    if condition:
        _passed += 1
        print(f"  PASS  {label}")
    else:
        _failed.append(label)
        print(f"  FAIL  {label}")


def _reading(source_id: str, metric: str, value: float | None, *, fetch_status: str = "ok", fetched_at: str = "2026-09-21T12:00:00+00:00") -> Reading:
    return Reading(
        source_id=source_id, provider="anthropic", runtime=_CELL[0], model=_CELL[1], effort=_CELL[2],
        metric=metric, value_number=value, value_text=None, raw_excerpt="fixture", fetch_status=fetch_status,
        fetched_at=fetched_at,
    )


def _ref(obs_id: str, reading: Reading) -> ObservationRef:
    return ObservationRef(observation_id=obs_id, reading=reading)


def scenario_agreeing_two_sources_accepts() -> None:
    obs = [
        _ref("obs1", _reading("artificial_analysis_leaderboard_html", "intelligence_index", 38.0)),
        _ref("obs2", _reading("artificial_analysis_model_detail_html", "intelligence_index", 38.0)),
        _ref("obs3", _reading("artificial_analysis_leaderboard_html", "cost_per_task_usd", 5.09)),
        _ref("obs4", _reading("artificial_analysis_model_detail_html", "cost_per_task_usd", 5.09)),
    ]
    outcome = reconcile_cell(_CELL, obs)
    _check(outcome.acceptance == CELL_ACCEPTANCE_ACCEPTED, "two agreeing sources (score+cost) -> accepted")
    _check(outcome.capability_score == 38.0, f"agreed capability_score is the mean of agreeing readings (got {outcome.capability_score})")
    _check(outcome.cost_per_task_usd == 5.09, f"agreed cost_per_task_usd is the mean of agreeing readings (got {outcome.cost_per_task_usd})")
    _check(set(outcome.agreeing_observation_ids) == {"obs1", "obs2", "obs3", "obs4"}, "agreeing_observation_ids names every corroborating reading")


def scenario_score_within_tolerance_accepts() -> None:
    obs = [
        _ref("obs1", _reading("artificial_analysis_leaderboard_html", "intelligence_index", 38.0)),
        _ref("obs2", _reading("artificial_analysis_model_detail_html", "intelligence_index", 38.0 + CAPABILITY_SCORE_TOLERANCE)),
    ]
    outcome = reconcile_cell(_CELL, obs)
    _check(outcome.acceptance == CELL_ACCEPTANCE_ACCEPTED, f"score spread exactly at tolerance ({CAPABILITY_SCORE_TOLERANCE}) still accepts")


def scenario_score_beyond_tolerance_conflicts() -> None:
    obs = [
        _ref("obs1", _reading("artificial_analysis_leaderboard_html", "intelligence_index", 38.0)),
        _ref("obs2", _reading("artificial_analysis_model_detail_html", "intelligence_index", 38.0 + CAPABILITY_SCORE_TOLERANCE + 0.1)),
    ]
    outcome = reconcile_cell(_CELL, obs)
    _check(outcome.acceptance == CELL_ACCEPTANCE_CROSSCHECK_CONFLICT, "score spread just past tolerance -> crosscheck_conflict")
    _check(outcome.capability_score is None, "a conflicted cell carries no capability_score")
    _check(outcome.disagreement_note is not None and "intelligence_index" in outcome.disagreement_note, "disagreement_note names the disagreeing metric")


def scenario_cost_beyond_tolerance_conflicts_even_with_score_agreement() -> None:
    obs = [
        _ref("obs1", _reading("artificial_analysis_leaderboard_html", "intelligence_index", 38.0)),
        _ref("obs2", _reading("artificial_analysis_model_detail_html", "intelligence_index", 38.0)),
        _ref("obs3", _reading("artificial_analysis_leaderboard_html", "cost_per_task_usd", 5.00)),
        _ref("obs4", _reading("artificial_analysis_model_detail_html", "cost_per_task_usd", 5.00 * (1 + COST_RELATIVE_TOLERANCE + 0.05))),
    ]
    outcome = reconcile_cell(_CELL, obs)
    _check(outcome.acceptance == CELL_ACCEPTANCE_CROSSCHECK_CONFLICT, "agreeing score but disagreeing cost still refuses acceptance")
    _check(outcome.disagreement_note is not None and "cost_per_task_usd" in outcome.disagreement_note, "disagreement_note names cost, not score, as the disagreeing metric")


def scenario_missing_second_source_stays_pending() -> None:
    obs = [_ref("obs1", _reading("artificial_analysis_leaderboard_html", "intelligence_index", 38.0))]
    outcome = reconcile_cell(_CELL, obs)
    _check(outcome.acceptance == CELL_ACCEPTANCE_PENDING_CROSSCHECK, "only 1 distinct source -> pending_crosscheck, not accepted or conflicted")
    _check(outcome.agreeing_observation_ids == [], "a pending cell names no agreeing observations")


def scenario_no_observations_stays_pending() -> None:
    outcome = reconcile_cell(_CELL, [])
    _check(outcome.acceptance == CELL_ACCEPTANCE_PENDING_CROSSCHECK, "zero observations -> pending_crosscheck, never a crash")


def scenario_one_source_fetch_failed_does_not_count() -> None:
    obs = [
        _ref("obs1", _reading("artificial_analysis_leaderboard_html", "intelligence_index", 38.0)),
        _ref("obs2", _reading("artificial_analysis_model_detail_html", "intelligence_index", None, fetch_status="http_error")),
    ]
    outcome = reconcile_cell(_CELL, obs)
    _check(outcome.acceptance == CELL_ACCEPTANCE_PENDING_CROSSCHECK, "a failed fetch (fetch_status != ok) does not count toward the 2-source requirement")


def scenario_seed_alone_never_satisfies_two_source() -> None:
    """The brief's explicit anti-pattern check: seed_table must never, by
    itself or paired with only one real fetch, satisfy the requirement."""
    obs = [
        _ref("obs1", _reading("seed_table", "intelligence_index", 38.0)),
        _ref("obs2", _reading("seed_table", "intelligence_index", 38.0)),
    ]
    # Two seed_table readings collapse to one distinct source_id by construction
    # (the reconciler's freshest-per-source-id dedup) -- this is the seed
    # attempting to corroborate itself and must not accept.
    outcome = reconcile_cell(_CELL, obs)
    _check(outcome.acceptance == CELL_ACCEPTANCE_PENDING_CROSSCHECK, "two readings from the SAME source_id (e.g. seed_table twice) still count as only 1 distinct source")


def scenario_stale_reading_ignored_for_freshest_pick() -> None:
    obs = [
        _ref("obs1", _reading("artificial_analysis_leaderboard_html", "intelligence_index", 30.0, fetched_at="2026-09-01T00:00:00+00:00")),
        _ref("obs2", _reading("artificial_analysis_leaderboard_html", "intelligence_index", 38.0, fetched_at="2026-09-21T00:00:00+00:00")),
        _ref("obs3", _reading("artificial_analysis_model_detail_html", "intelligence_index", 38.0, fetched_at="2026-09-21T00:00:00+00:00")),
    ]
    outcome = reconcile_cell(_CELL, obs)
    _check(outcome.acceptance == CELL_ACCEPTANCE_ACCEPTED, "a stale older reading from the same source is superseded by the freshest one, not double-counted")
    _check(outcome.capability_score == 38.0, "the freshest reading's value wins, not the stale one")
    _check("obs1" not in outcome.agreeing_observation_ids, "the stale superseded reading is never cited as agreeing evidence")


def main() -> int:
    print("model_capability_reconcile_smoke")
    scenario_agreeing_two_sources_accepts()
    scenario_score_within_tolerance_accepts()
    scenario_score_beyond_tolerance_conflicts()
    scenario_cost_beyond_tolerance_conflicts_even_with_score_agreement()
    scenario_missing_second_source_stays_pending()
    scenario_no_observations_stays_pending()
    scenario_one_source_fetch_failed_does_not_count()
    scenario_seed_alone_never_satisfies_two_source()
    scenario_stale_reading_ignored_for_freshest_pick()
    print(f"\n{_passed} passed, {len(_failed)} failed")
    return 1 if _failed else 0


if __name__ == "__main__":
    sys.exit(main())
