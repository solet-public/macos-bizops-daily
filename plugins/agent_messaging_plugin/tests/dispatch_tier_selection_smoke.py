#!/usr/bin/env python3
"""Proves the pure tier selector reproduces the operator's hand-computed verdicts.

The fixture is the 2026-09-19 schema_version-2 table on iss_f452fe5e (AA
Intelligence Index + AA cost-per-task, USD), the exact numbers the operator
reasoned over. The v3 header dropped dollar cost for that project; this smoke
keeps it because the platform capability must serve metered billing.
"""

from __future__ import annotations

import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, cast

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "ananta" / "src"))
sys.path.insert(0, str(ROOT / "plugins" / "agent_messaging_plugin" / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

if TYPE_CHECKING:
    from ananta.interfaces.state_management_interface import StateManagementInterface

from _real_state_fake import RealShapeState  # noqa: E402
from ananta.llm.agent_messaging.role_binding import AGENT_ROLE_BINDING_NAMESPACE  # noqa: E402

from agent_messaging_plugin import model_capability_verbs as verbs  # noqa: E402
from agent_messaging_plugin.dispatch_tier_selection import (  # noqa: E402
    CapabilityCell,
    TierSelectionError,
    select_tier,
)
from agent_messaging_plugin.model_capability_store import load_seed_table, seed_catalog  # noqa: E402
from agent_messaging_plugin.model_dispatch_policy import DispatchPolicyError  # noqa: E402
from agent_messaging_plugin.schema import CELL_ACCEPTANCE_ACCEPTED, TABLE_MODEL_CAPABILITY_CELL  # noqa: E402

_passed = 0
_failed: list[str] = []
_NOW = datetime(2026, 9, 20, tzinfo=UTC)
_MEASURED = datetime(2026, 9, 19, tzinfo=UTC)
_WINDOW = timedelta(days=7)

# (runtime, model, effort, score, usd) -- usd None where AA published no cost.
_TABLE: tuple[tuple[str, str, str, float, float | None], ...] = (
    ("claude_code", "claude-haiku-4.5", "non_reasoning", 15, None),
    ("claude_code", "claude-sonnet-5", "low", 24, 0.51),
    ("claude_code", "claude-sonnet-5", "medium", 28, 1.00),
    ("claude_code", "claude-sonnet-5", "high", 32, 1.79),
    ("claude_code", "claude-sonnet-5", "xhigh", 34, 2.87),
    ("claude_code", "claude-sonnet-5", "max", 38, 5.09),
    ("claude_code", "claude-opus-5", "low", 39, 1.10),
    ("claude_code", "claude-opus-5", "medium", 45, 2.19),
    ("claude_code", "claude-opus-5", "high", 48, 3.61),
    ("claude_code", "claude-opus-5", "xhigh", 50, 4.88),
    ("claude_code", "claude-opus-5", "max", 51, 5.86),
    ("claude_code", "claude-fable-5-1", "low", 47, 2.37),
    ("claude_code", "claude-fable-5-1", "medium", 49, 2.98),
    ("claude_code", "claude-fable-5-1", "high", 51, 3.91),
    ("claude_code", "claude-fable-5-1", "xhigh", 53, 5.98),
    ("claude_code", "claude-fable-5-1", "max", 53, 7.63),
    ("codex", "gpt-5.6-sol", "low", 33, 0.26),
    ("codex", "gpt-5.6-sol", "medium", 39, 0.50),
    ("codex", "gpt-5.6-sol", "high", 42, 0.81),
    ("codex", "gpt-5.6-sol", "xhigh", 44, 1.18),
    ("codex", "gpt-5.6-sol", "max", 47, 1.99),
    ("codex", "gpt-5.6-terra", "low", 27, 0.14),
    ("codex", "gpt-5.6-terra", "medium", 30, 0.18),
    ("codex", "gpt-5.6-terra", "high", 34, 0.34),
    ("codex", "gpt-5.6-terra", "xhigh", 38, 0.63),
    ("codex", "gpt-5.6-terra", "max", 42, 1.40),
    ("codex", "gpt-5.6-luna", "low", 21, 0.01),
    ("codex", "gpt-5.6-luna", "medium", 25, 0.02),
    ("codex", "gpt-5.6-luna", "high", 32, 0.04),
    ("codex", "gpt-5.6-luna", "xhigh", 35, 0.09),
    ("codex", "gpt-5.6-luna", "max", 37, 0.18),
    ("codex", "gpt-6-astra", "low", 46, 0.82),
    ("codex", "gpt-6-astra", "medium", 50, 1.54),
    ("codex", "gpt-6-astra", "high", 51, 1.73),
    ("codex", "gpt-6-astra", "xhigh", 52, 2.31),
    ("codex", "gpt-6-astra", "max", 53, 3.26),
)
_CHEAPEST_USD = 0.01


def _cell(row: tuple[str, str, str, float, float | None], *, measured_at: datetime = _MEASURED, acceptance: str = "accepted") -> CapabilityCell:
    runtime, model, effort, score, usd = row
    return CapabilityCell(
        runtime=runtime, model=model, effort=effort, capability_score=score, cost_per_task_usd=usd,
        relative_cost_multiplier=None if usd is None else round(usd / _CHEAPEST_USD),
        measured_at=measured_at, acceptance=acceptance,
    )


CATALOG = tuple(_cell(row) for row in _TABLE)
CLAUDE_PAIRS = frozenset(cell.pair for cell in CATALOG if cell.runtime == "claude_code")


def _check(condition: object, label: str) -> None:
    global _passed
    if condition:
        _passed += 1
        print(f"  PASS  {label}")
    else:
        _failed.append(label)
        print(f"  FAIL  {label}")


def _code(callback: object) -> str:
    try:
        assert callable(callback)
        callback()
    except TierSelectionError as exc:
        return exc.code
    return ""


def _named(cell: CapabilityCell) -> str:
    return f"{cell.model}@{cell.effort}"


def test_operator_dominance_examples() -> None:
    claude_only = select_tier(CATALOG, required_score=38, objective="metered_usd", now=_NOW, max_age=_WINDOW, capability_floor_pairs=CLAUDE_PAIRS)
    _check(_named(claude_only.selected) == "claude-opus-5@low", "score 38, Claude-only: opus-5 low beats sonnet-5 max")
    dominated = {(_named(weak), _named(strong)) for weak, strong in claude_only.dominated}
    _check(("claude-sonnet-5@max", "claude-opus-5@low") in dominated, "sonnet-5 max is reported as dominated by opus-5 low")

    cross = select_tier(CATALOG, required_score=45, objective="metered_usd", now=_NOW, max_age=_WINDOW)
    _check(_named(cross.selected) == "gpt-6-astra@low", "score 45, any vendor: astra low beats opus-5 medium")
    cross_dominated = {(_named(weak), _named(strong)) for weak, strong in cross.dominated}
    _check(("claude-opus-5@medium", "gpt-6-astra@low") in cross_dominated, "opus-5 medium is reported as dominated by astra low")


def test_ladder_shows_diminishing_returns() -> None:
    sonnet = select_tier(CATALOG, required_score=24, objective="metered_usd", now=_NOW, max_age=_WINDOW, capability_floor_pairs=frozenset({("claude_code", "claude-sonnet-5")}))
    steps = {step.to_effort: step.points_per_cost_unit for step in sonnet.ladder}
    low_to_medium, xhigh_to_max = steps["medium"], steps["max"]
    _check(low_to_medium is not None and round(low_to_medium, 1) == 8.2, "sonnet-5 low->medium buys 8.2 points per dollar")
    _check(xhigh_to_max is not None and round(xhigh_to_max, 1) == 1.8, "sonnet-5 xhigh->max buys 1.8 points per dollar")

    fable = select_tier(CATALOG, required_score=47, objective="metered_usd", now=_NOW, max_age=_WINDOW, capability_floor_pairs=frozenset({("claude_code", "claude-fable-5-1")}))
    top = next(step for step in fable.ladder if step.to_effort == "max")
    _check(top.score_gain == 0 and top.points_per_cost_unit == 0.0, "fable-5.1 xhigh->max buys zero points for real cost")


def test_cheap_model_effort_is_exhausted_before_switching() -> None:
    picked = select_tier(CATALOG, required_score=35, objective="metered_usd", now=_NOW, max_age=_WINDOW)
    _check(_named(picked.selected) == "gpt-5.6-luna@xhigh", "score 35: luna xhigh ($0.09) wins before any model switch")


def test_exhausted_vendor_fails_over_and_unknown_quota_refuses() -> None:
    quotas = {cell.pair: "available" for cell in CATALOG}
    quotas[("codex", "gpt-5.6-luna")] = "exhausted"
    fallback = select_tier(
        CATALOG, required_score=35, objective="metered_usd", now=_NOW, max_age=_WINDOW,
        quota_status_by_pair=quotas,
    )
    _check(_named(fallback.selected) == "gpt-5.6-sol@medium", "exhausted cheapest vendor/model falls back to next cheapest clearing cell")
    _check(fallback.excluded["quota_exhausted"] == 5, "all exhausted-model cells are counted")
    unknown = {cell.pair: "unknown" for cell in CATALOG}
    _check(
        _code(lambda: select_tier(
            CATALOG, required_score=35, objective="metered_usd", now=_NOW, max_age=_WINDOW,
            quota_status_by_pair=unknown,
        )) == "quota_state_unknown",
        "unknown quota state refuses rather than treating it as unlimited",
    )


def test_objectives_and_margins() -> None:
    relative = select_tier(CATALOG, required_score=45, objective="relative", now=_NOW, max_age=_WINDOW)
    _check(_named(relative.selected) == "gpt-6-astra@low", "relative objective agrees with metered when the table is complete")
    margin = select_tier(CATALOG, required_score=45, objective="metered_usd", now=_NOW, max_age=_WINDOW, score_margin=2)
    _check(margin.effective_required_score == 47 and _named(margin.selected) == "gpt-6-astra@medium", "score_margin raises the bar: 47 -> astra medium ($1.54), not sol max ($1.99)")
    _check(margin.excluded["unpriced"] == 1, "the unpriced haiku cell is excluded and counted, never guessed")


def test_near_tie_prefers_lower_effort() -> None:
    tie = (
        _cell(("codex", "model-a", "high", 40, 1.00)),
        _cell(("codex", "model-b", "low", 40, 1.03)),
    )
    picked = select_tier(tie, required_score=40, objective="metered_usd", now=_NOW, max_age=_WINDOW)
    _check(_named(picked.selected) == "model-b@low", "within cost_tolerance the lower effort ordinal wins")
    strict = select_tier(tie, required_score=40, objective="metered_usd", now=_NOW, max_age=_WINDOW, cost_tolerance=0.0)
    _check(_named(strict.selected) == "model-a@high", "cost_tolerance=0 is the exact argmin")


def test_refusals_are_distinct() -> None:
    _check(_code(lambda: select_tier((), required_score=1, objective="metered_usd", now=_NOW, max_age=_WINDOW)) == "catalog_empty", "empty catalog refuses")
    stale = tuple(_cell(row, measured_at=_NOW - timedelta(days=30)) for row in _TABLE)
    _check(_code(lambda: select_tier(stale, required_score=1, objective="metered_usd", now=_NOW, max_age=_WINDOW)) == "catalog_stale", "all-stale catalog refuses as stale, not as impossible")
    pending = tuple(_cell(row, acceptance="pending_crosscheck") for row in _TABLE)
    _check(_code(lambda: select_tier(pending, required_score=1, objective="metered_usd", now=_NOW, max_age=_WINDOW)) == "catalog_stale", "un-cross-checked cells are never served")
    _check(_code(lambda: select_tier(CATALOG, required_score=99, objective="metered_usd", now=_NOW, max_age=_WINDOW)) == "no_cell_clears_threshold", "impossible threshold refuses distinctly")
    _check(_code(lambda: select_tier(CATALOG, required_score=1, objective="metered_usd", now=_NOW, max_age=_WINDOW, score_margin=-1)) == "parameter_invalid", "negative margin refuses")


def test_register_phase_selection_with_state_schema_provenance() -> None:
    state = RealShapeState()
    typed = cast("StateManagementInterface", state)
    now = datetime(2026, 9, 24, 12, tzinfo=UTC)
    seed_catalog(typed, seed=load_seed_table())
    for row in state.rows(AGENT_ROLE_BINDING_NAMESPACE, TABLE_MODEL_CAPABILITY_CELL):
        row["acceptance"] = CELL_ACCEPTANCE_ACCEPTED
        row["accepted_at"] = now.isoformat()
        row["measured_at"] = now.isoformat()
        row["last_refresh_run_id"] = "phase-selector-fixture"
    kinds = (
        "DESIGN", "FIX", "build", "design", "diagnose", "docs", "fix", "implement",
        "implementation", "infrastructure", "integration", "repair", "review",
        "smoke-test", "test", "test-close",
    )
    for kind in kinds:
        selected = verbs.select_dispatch_tier(
            typed,
            {"required_score": 50, "scope_tags": ["state_schema"], "dispatch_kind": kind,
             "cost_tolerance": 0},
            now=now,
        )
        cell = selected["selected"]
        _check(
            (cell["runtime"], cell["model"], cell["effort"], cell["capability_score"])
            == ("codex", "gpt-6-astra", "medium", 50),
            f"register kind {kind!r} clears score 50 at Astra medium",
        )
        _check(
            selected["capability_floors"] == []
            and selected["excluded"]["capability_floor_disallowed"] == 0,
            f"register kind {kind!r} has no state_schema model floor",
        )
    for kind in ("unknown-phase", "test ", "TEST"):
        selected = verbs.select_dispatch_tier(
            typed, {"required_score": 50, "dispatch_kind": kind}, now=now,
        )
        _check(selected["selected"]["capability_score"] >= 50, f"open nonblank provenance {kind!r} selects")
    for kind in ("", " ", "\t", 123):
        try:
            verbs.select_dispatch_tier(typed, {"required_score": 50, "dispatch_kind": kind}, now=now)
        except DispatchPolicyError as error:
            code = error.code
        else:
            code = ""
        _check(code in {"dispatch_policy_violation", "dispatch_kind_required"}, f"malformed kind {kind!r} refuses")


if __name__ == "__main__":
    test_operator_dominance_examples()
    test_ladder_shows_diminishing_returns()
    test_cheap_model_effort_is_exhausted_before_switching()
    test_exhausted_vendor_fails_over_and_unknown_quota_refuses()
    test_objectives_and_margins()
    test_near_tie_prefers_lower_effort()
    test_refusals_are_distinct()
    test_register_phase_selection_with_state_schema_provenance()
    print(f"\n{_passed} passed, {len(_failed)} failed")
    raise SystemExit(1 if _failed else 0)
