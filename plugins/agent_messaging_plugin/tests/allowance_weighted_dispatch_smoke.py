#!/usr/bin/env python3
"""Proves the allowance-weighted dispatch objective (iss_eef0812b, design DESIGN.md sections 2 and 5).

A flat-rate profile may declare operator-owned dispatch weights per model
prefix. Under a runtime constraint covered by that plan the default objective
is ``allowance_weighted`` (relative cost times the declared weight). Without a
covered runtime constraint, or for a plan with no table, ranking is unchanged:
this smoke compares whole payloads to prove Codex and unconstrained selections
are byte-identical. The golden table runs the SHIPPED profile over a fixed
catalog taken from the live cells of 2026-10-01T03:15Z.
"""

from __future__ import annotations

import copy
import json
import sys
import tempfile
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "ananta" / "src"))
sys.path.insert(0, str(ROOT / "plugins" / "agent_messaging_plugin" / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

if TYPE_CHECKING:
    from ananta.interfaces.state_management_interface import StateManagementInterface

from _real_state_fake import RealShapeState  # noqa: E402
from ananta.llm.agent_messaging.role_binding import AGENT_ROLE_BINDING_NAMESPACE  # noqa: E402

from agent_messaging_plugin import model_capability_verbs as verbs  # noqa: E402
from agent_messaging_plugin.dispatch_tier_selection import CapabilityCell, select_tier  # noqa: E402
from agent_messaging_plugin.dispatch_weights import DispatchWeights  # noqa: E402
from agent_messaging_plugin.model_capability_store import CatalogError, ManualCell, record_manual_cell  # noqa: E402
from agent_messaging_plugin.schema import TABLE_MODEL_CAPABILITY_CELL  # noqa: E402
from agent_messaging_plugin.usage_economics_profiles import (  # noqa: E402
    UsageEconomicsProfileValidationError,
    load_usage_economics_profile_catalog,
)
from agent_messaging_plugin.usage_economics_quota import plan_for_runtime  # noqa: E402

_passed = 0
_failed: list[str] = []
_NOW = datetime(2026, 10, 1, 3, 15, tzinfo=UTC)
_SHIPPED = ROOT / "plugins" / "agent_messaging_plugin" / "model_profiles" / "usage_economics.v1.json"
_PROFILE = "anthropic-max-20x-2026-08-22"
_RULING = "rul_29449a0b-3b9e-4251-bf87-4d295269a409"

# (runtime, model, effort, score, cost) -- live cells at 2026-10-01T03:15Z, the ones that can matter here.
_CELLS: tuple[tuple[str, str, str, float, float], ...] = (
    ("claude_code", "claude-opus-5-5", "low", 42, 0.55),
    ("claude_code", "claude-opus-5-5", "medium", 51, 1.34),
    ("claude_code", "claude-opus-5-5", "high", 54, 1.82),
    ("claude_code", "claude-opus-5-5", "xhigh", 56, 3.46),
    ("claude_code", "claude-opus-5-5", "max", 58, 5.98),
    ("claude_code", "claude-sonnet-5-5", "medium", 41, 0.59),
    ("claude_code", "claude-sonnet-5-5", "high", 47, 1.08),
    ("claude_code", "claude-sonnet-5-5", "xhigh", 52, 2.74),
    ("claude_code", "claude-sonnet-5-5", "max", 56, 7.62),
    ("claude_code", "claude-sonnet-5", "low", 24, 0.51),
    ("claude_code", "claude-haiku-4.5", "non_reasoning", 16, 0.28),
    ("codex", "gpt-6-luna", "low", 21, 0.0045),
    ("codex", "gpt-6-sol", "medium", 40, 0.25),
    ("codex", "gpt-6-sol", "high", 43, 0.38),
    ("codex", "gpt-6-astra", "low", 46, 0.82),
)
# Expected choices for required scores 40..56, DESIGN.md section 5 (shipped weight: claude-sonnet 0.5).
_WEIGHTED: dict[int, tuple[str, str]] = {
    **dict.fromkeys((40, 41), ("claude-sonnet-5-5", "medium")),
    42: ("claude-opus-5-5", "low"),
    **dict.fromkeys(range(43, 48), ("claude-sonnet-5-5", "high")),
    **dict.fromkeys(range(48, 52), ("claude-opus-5-5", "medium")),
    52: ("claude-sonnet-5-5", "xhigh"),
    **dict.fromkeys((53, 54), ("claude-opus-5-5", "high")),
    **dict.fromkeys((55, 56), ("claude-opus-5-5", "xhigh")),
}
# The same catalog under today's ranking (relative cost, no weights).
_UNWEIGHTED: dict[int, tuple[str, str]] = {
    **dict.fromkeys((40, 41, 42), ("claude-opus-5-5", "low")),
    **dict.fromkeys(range(43, 48), ("claude-sonnet-5-5", "high")),
    **dict.fromkeys(range(48, 52), ("claude-opus-5-5", "medium")),
    **dict.fromkeys((52, 53, 54), ("claude-opus-5-5", "high")),
    **dict.fromkeys((55, 56), ("claude-opus-5-5", "xhigh")),
}


def _check(condition: object, label: str) -> None:
    global _passed
    if condition:
        _passed += 1
        print(f"  PASS  {label}")
    else:
        _failed.append(label)
        print(f"  FAIL  {label}")


def _state() -> StateManagementInterface:
    state = RealShapeState()
    typed = cast("StateManagementInterface", state)
    for runtime, model, effort, score, cost in _CELLS:
        record_manual_cell(typed, ManualCell(runtime, model, effort, score, cost, "weighted-dispatch fixture", None))
    for row in state.rows(AGENT_ROLE_BINDING_NAMESPACE, TABLE_MODEL_CAPABILITY_CELL):
        row["measured_at"] = _NOW.isoformat()
        row["accepted_at"] = _NOW.isoformat()
    return typed


def _refusal(callback: Any) -> tuple[str, str]:
    try:
        callback()
    except CatalogError as exc:
        return exc.code, exc.message
    return "", ""


def _profile_json(mutate: Any = None) -> dict[str, Any]:
    raw = cast("dict[str, Any]", json.loads(_SHIPPED.read_text(encoding="utf-8")))
    if mutate is not None:
        mutate(next(row for row in raw["profiles"] if row["profile_id"] == _PROFILE))
    return raw


def _load(raw: dict[str, Any]) -> Any:
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "usage_economics.v1.json"
        path.write_text(json.dumps(raw), encoding="utf-8")
        return load_usage_economics_profile_catalog(path, as_of=_NOW)


def _load_refusal(mutate: Any) -> str:
    try:
        _load(_profile_json(mutate))
    except UsageEconomicsProfileValidationError as exc:
        return str(exc)
    return ""


class _ProfileFile:
    """Point the verbs at a temporary profile file, restoring the shipped path afterwards."""

    def __init__(self, mutate: Any, *, whole: Any = None) -> None:
        self._mutate = mutate
        self._whole = whole
        self._directory = tempfile.TemporaryDirectory()
        self._saved = verbs._USAGE_ECONOMICS_PROFILE_PATH  # pyright: ignore[reportPrivateUsage]

    def __enter__(self) -> None:
        path = Path(self._directory.name) / "usage_economics.v1.json"
        raw = _profile_json(self._mutate)
        if self._whole is not None:
            self._whole(raw)
        path.write_text(json.dumps(raw), encoding="utf-8")
        verbs._USAGE_ECONOMICS_PROFILE_PATH = path  # pyright: ignore[reportPrivateUsage]

    def __exit__(self, *exc: object) -> None:
        verbs._USAGE_ECONOMICS_PROFILE_PATH = self._saved  # pyright: ignore[reportPrivateUsage]
        self._directory.cleanup()


def _drop_weights(row: dict[str, Any]) -> None:
    del row["dispatch_weights"]


def _drop_pools(row: dict[str, Any]) -> None:
    del row["allowance_pools"]


def _add_second_covering_plan(raw: dict[str, Any]) -> None:
    plan = next(row for row in raw["profiles"] if row["profile_id"] == _PROFILE)
    raw["profiles"].append({**copy.deepcopy(plan), "profile_id": _PROFILE + "-second"})


def _select(state: StateManagementInterface, score: float, **params: Any) -> dict[str, Any]:
    return verbs.select_dispatch_tier(state, {"required_score": score, **params}, now=_NOW)


def _pick(selection: dict[str, Any]) -> tuple[str, str]:
    return str(selection["selected"]["model"]), str(selection["selected"]["effort"])


def test_shipped_profile_declares_the_decided_table() -> None:
    plan = plan_for_runtime(load_usage_economics_profile_catalog(_SHIPPED, as_of=_NOW), "claude_code")
    weights = None if plan is None else plan.dispatch_weights
    _check(plan is not None and plan.profile_id == _PROFILE, "claude_code is covered by the Max 20x plan")
    _check(weights is not None and weights.basis == "operator_policy" and weights.ruling_id == _RULING, "the table is operator policy under rul_29449a0b")
    _check(weights is not None and dict(weights.by_model_prefix) == {"claude-sonnet": 0.5} and weights.default_weight == 1.0, "shipped weights: claude-sonnet 0.5, default 1.0, no Haiku or Fable entry")
    _check(weights is not None and weights.weight_for("claude-sonnet-5-5") == 0.5 and weights.weight_for("claude-opus-5-5") == 1.0, "Sonnet 5.5 weighs 0.5 and Opus 5.5 the declared default")
    _check(plan_for_runtime(load_usage_economics_profile_catalog(_SHIPPED, as_of=_NOW), "codex") is None, "codex is covered by no flat-rate plan")


def test_table_validation_is_loud() -> None:
    def table(row: dict[str, Any]) -> dict[str, Any]:
        return cast("dict[str, Any]", row["dispatch_weights"])

    def without(key: str) -> Any:
        return lambda row: table(row).pop(key)

    def setting(key: str, value: object) -> Any:
        return lambda row: table(row).__setitem__(key, value)

    cases: dict[str, tuple[Any, str]] = {
        "no default_weight": (without("default_weight"), "default_weight"),
        "zero weight": (lambda row: table(row)["by_model_prefix"].__setitem__("claude-sonnet", 0), "above zero"),
        "negative default": (setting("default_weight", -1.0), "above zero"),
        "boolean weight": (setting("default_weight", True), "above zero"),
        "operator_policy without ruling": (without("ruling_id"), "ruling_id"),
        "unknown basis": (setting("basis", "vibes"), "basis"),
        "measured without evidence": (lambda row: table(row).update({"basis": "measured", "ruling_id": None}), "evidence_ref"),
        "empty prefix table": (setting("by_model_prefix", {}), "by_model_prefix"),
        "blank prefix": (setting("by_model_prefix", {" ": 0.5}), "by_model_prefix key"),
        "unknown key": (setting("tokens_per_point", 3), "unknown keys"),
        "token conversion key": (setting("token_conversion", 3), "token conversion"),
        "not an object": (lambda row: row.__setitem__("dispatch_weights", [1]), "must be an object"),
        "no declared_at": (without("declared_at"), "declared_at"),
    }
    for label, (mutate, expected) in cases.items():
        message = _load_refusal(mutate)
        _check(expected in message, f"refused ({label}): {message[:70]!r}")
    _check(_load(_profile_json(_drop_weights)) is not None, "a plan without the table still loads")
    plain = plan_for_runtime(_load(_profile_json(_drop_weights)), "claude_code")
    _check(plain is not None and plain.dispatch_weights is None, "an absent table reads as None, never a default table")

    def second_plan(raw: dict[str, Any]) -> dict[str, Any]:
        clone = copy.deepcopy(next(row for row in raw["profiles"] if row["profile_id"] == _PROFILE))
        clone["profile_id"] = "anthropic-max-20x-clone"
        raw["profiles"].append(clone)
        return raw

    try:
        plan_for_runtime(_load(second_plan(_profile_json())), "claude_code")
        ambiguous = ""
    except UsageEconomicsProfileValidationError as exc:
        ambiguous = str(exc)
    _check("ambiguous" in ambiguous, "two current plans covering one runtime refuse instead of guessing")


def test_longest_prefix_wins() -> None:
    table = DispatchWeights(
        basis="operator_policy", ruling_id=_RULING, evidence_ref=None, declared_at=_NOW, default_weight=1.0,
        by_model_prefix=(("claude-sonnet", 0.5), ("claude-sonnet-5-5", 0.4)), note=None,
    )
    _check(table.weight_for("claude-sonnet-5-5") == 0.4 and table.weight_for("claude-sonnet-5") == 0.5, "the longest matching prefix wins, whatever the order")
    _check(table.weight_for("gpt-6-sol") == 1.0, "no match takes the declared default")


def test_pure_selector_objective() -> None:
    def cell(model: str, effort: str, score: float, relative: float | None, weight: float) -> CapabilityCell:
        return CapabilityCell("claude_code", model, effort, score, None if relative is None else relative / 100, relative, _NOW, "accepted", weight)

    opus = cell("claude-opus-5-5", "medium", 51, 297.78, 1.0)
    sonnet = cell("claude-sonnet-5-5", "xhigh", 52, 608.89, 0.4)
    _check(sonnet.cost("allowance_weighted") == 608.89 * 0.4 and sonnet.cost("relative") == 608.89, "allowance_weighted is relative cost times the weight; relative is untouched")
    _check(replace(sonnet, plan_weight=1.0).cost("allowance_weighted") == sonnet.cost("relative"), "weight 1.0 equals the relative ranking")
    _check(cell("m", "low", 1, None, 0.5).cost("allowance_weighted") is None, "an unpriced cell stays unpriced")
    now, window = _NOW, timedelta(hours=72)
    weighted = select_tier((opus, sonnet), required_score=50, objective="allowance_weighted", now=now, max_age=window)
    plain = select_tier((opus, sonnet), required_score=50, objective="relative", now=now, max_age=window)
    _check(weighted.selected.model == "claude-sonnet-5-5" and plain.selected.model == "claude-opus-5-5", "the weight reorders cells that relative ranks the other way")
    scaled = select_tier(
        (replace(opus, relative_cost_multiplier=2 * 297.78), replace(sonnet, relative_cost_multiplier=2 * 608.89)),
        required_score=50, objective="allowance_weighted", now=now, max_age=window,
    )
    _check(scaled.selected.model == weighted.selected.model, "the ranking is scale-invariant, so the catalog-wide price floor does not matter")


def test_golden_table_on_the_shipped_profile() -> None:
    state = _state()
    weighted = {score: _pick(_select(state, score, runtime="claude_code")) for score in range(40, 57)}
    _check(weighted == _WEIGHTED, "required 40-56 on the shipped profile reproduces DESIGN.md section 5 (recommended column)")
    sample = _select(state, 45, runtime="claude_code")
    _check(sample["billing_objective"] == "allowance_weighted", "a covered runtime constraint defaults to allowance_weighted")
    basis = sample.get("cost_basis", {})
    _check(
        basis.get("plan_profile_id") == _PROFILE and basis.get("ruling_id") == _RULING and basis.get("selected_weight") == 0.5
        and basis.get("by_model_prefix") == {"claude-sonnet": 0.5} and basis.get("default_weight") == 1.0,
        "cost_basis names the plan, the ruling, the table and the weight applied",
    )
    _check(sample["selected"].get("plan_weight") == 0.5 and "warnings" not in sample, "the selected cell carries its weight and there is no warning")
    with _ProfileFile(_drop_weights):
        unweighted = {score: _pick(_select(state, score, runtime="claude_code")) for score in range(40, 57)}
        _check(unweighted == _UNWEIGHTED, "absent a weights table, the same catalog reproduces today's ranking, including Sonnet high at 43-47")


def _outside_the_plan(state: StateManagementInterface) -> dict[str, list[dict[str, Any]]]:
    return {
        "free": [_select(state, score) for score in (14, 41, 44, 52, 56)],
        "codex": [_select(state, score, runtime="codex") for score in (21, 41, 44)],
        "explicit_relative": [_select(state, score, runtime="claude_code", billing_objective="relative") for score in (41, 52)],
        "explicit_metered": [_select(state, score, runtime="claude_code", billing_objective="metered_usd") for score in (41, 52)],
    }


def test_ranking_outside_the_plan_is_byte_identical() -> None:
    state = _state()
    with_table = _outside_the_plan(state)
    with _ProfileFile(_drop_weights):
        without_table = _outside_the_plan(state)
    for key in with_table:
        _check(json.dumps(with_table[key], sort_keys=True) == json.dumps(without_table[key], sort_keys=True), f"{key}: payloads are identical with and without the weight table")
    unweighted = with_table["free"] + with_table["codex"]
    _check(all("plan_weight" not in payload["selected"] and "cost_basis" not in payload for payload in unweighted), "unconstrained and Codex payloads carry no weight fields")
    _check({payload["billing_objective"] for payload in with_table["codex"]} == {"relative"}, "a Codex-constrained selection keeps today's objective")


def test_missing_table_warns_without_refusing() -> None:
    state = _state()
    with _ProfileFile(_drop_weights):
        defaulted = _select(state, 45, runtime="claude_code")
        explicit = _select(state, 45, runtime="claude_code", billing_objective="relative")
        _check(defaulted["billing_objective"] == "relative" and _PROFILE in defaulted["warnings"][0], "a covering plan with no table keeps relative ranking and says so in warnings")
        _check("warnings" not in explicit, "an explicitly requested objective is not warned about")
        _check("warnings" not in _select(state, 45, runtime="codex"), "a runtime no plan covers has nothing to warn about")


def test_allowance_weighted_is_plan_scoped() -> None:
    state = _state()
    for params in ({}, {"runtime": "codex"}):
        code, message = _refusal(lambda params=params: _select(state, 45, billing_objective="allowance_weighted", **params))
        _check(code == "parameter_invalid" and "plan-scoped" in message, f"explicit allowance_weighted with {params or 'no runtime'} is refused")
    with _ProfileFile(_drop_weights):
        code, _ = _refusal(lambda: _select(state, 45, runtime="claude_code", billing_objective="allowance_weighted"))
        _check(code == "parameter_invalid", "explicit allowance_weighted needs a table, not just a covered runtime")
    code, _ = _refusal(lambda: _select(state, 45, runtime="claude_code", billing_objective="weighted"))
    _check(code == "parameter_invalid", "an unknown objective is still refused")


def test_profile_faults_refuse_with_their_documented_codes() -> None:
    state = _state()
    with _ProfileFile(_drop_pools):
        code, message = _refusal(lambda: _select(state, 45, runtime="claude_code"))
        _check(code == "usage_economics_invalid", f"a runtime-constrained selection loads the profile first and refuses usage_economics_invalid ({message[:60]})")
        code, _ = _refusal(lambda: _select(state, 45))
        _check(code == "quota_state_unknown", "an unconstrained selection keeps refusing quota_state_unknown for the same fault")
    with _ProfileFile(lambda row: row["dispatch_weights"].update({"default_weight": 0})):
        code, _ = _refusal(lambda: _select(state, 45, runtime="claude_code"))
        _check(code == "usage_economics_invalid", "a malformed dispatch_weights table refuses a constrained selection as usage_economics_invalid")
    with _ProfileFile(lambda row: None, whole=_add_second_covering_plan):
        code, message = _refusal(lambda: _select(state, 45, runtime="claude_code"))
        _check(code == "usage_economics_invalid" and "ambiguous" in message, "two current plans covering the runtime refuse usage_economics_invalid as ambiguous")


def _verify(state: StateManagementInterface, receipt: dict[str, Any], selected: dict[str, Any]) -> None:
    verbs.verify_selection_receipt(
        state, difficulty_score=float(receipt["required_score"]), selection_receipt=receipt, dispatch_kind="implement",
        scope_tags=(), agent_runtime=selected["runtime"], model=selected["model"], effort=selected["effort"], now=_NOW,
    )


def test_receipt_round_trips_and_pins_the_ranking() -> None:
    state = _state()
    selection = _select(state, 41, runtime="claude_code")
    receipt = selection["selection_receipt"]
    _check(
        set(receipt) == {"required_score", "billing_objective", "score_margin", "cost_tolerance", "max_staleness_hours", "runtime_constraint", "selected"},
        "the receipt key set is unchanged",
    )
    _check(receipt["billing_objective"] == "allowance_weighted" and receipt["selected"]["model"] == "claude-sonnet-5-5", "the receipt pins the weighted ranking")
    _check(_refusal(lambda: _verify(state, receipt, receipt["selected"])) == ("", ""), "it verifies for its spawn tuple")
    relative = {**receipt, "billing_objective": "relative"}
    _check(_refusal(lambda: _verify(state, relative, relative["selected"]))[0] == "selection_receipt_mismatch", "a receipt re-labelled relative no longer replays to the same answer")
    stripped = {**receipt, "runtime_constraint": None}
    code, message = _refusal(lambda: _verify(state, stripped, stripped["selected"]))
    _check(code == "selection_receipt_mismatch" and "runtime_constraint" in message, "a weighted receipt without its runtime constraint is refused as a mismatch")
    legacy = _select(state, 41, runtime="claude_code", billing_objective="relative")["selection_receipt"]
    _check(_refusal(lambda: _verify(state, legacy, legacy["selected"])) == ("", ""), "a receipt issued under relative still verifies unchanged")

    def heavier(row: dict[str, Any]) -> None:
        row["dispatch_weights"]["by_model_prefix"]["claude-sonnet"] = 1.0

    with _ProfileFile(heavier):
        _check(_refusal(lambda: _verify(state, receipt, receipt["selected"]))[0] == "selection_receipt_mismatch", "editing the weight invalidates an in-flight receipt until the dispatcher re-selects")


def test_an_exhausted_pool_still_excludes_a_weighted_cell() -> None:
    state = _state()
    for pool in ("rolling-five-hour", "weekly-all-model", "weekly-model-family"):
        verbs.record_pool_reading(
            state,
            {
                "profile_id": _PROFILE, "pool_id": pool, "consumed": 100.0 if pool == "weekly-model-family" else 10.0,
                "as_of": (_NOW - timedelta(minutes=5)).isoformat(), "next_reset_at": (_NOW + timedelta(hours=2)).isoformat(),
                "source": f"fixture reading; {_RULING}", "recorded_by": "Coordinator",
            },
            now=_NOW,
        )
    selection = _select(state, 45, runtime="claude_code")
    _check(_pick(selection) == ("claude-opus-5-5", "medium"), "Sonnet's own pool is used up: the weight cannot bring Sonnet back")
    _check(selection["excluded"]["quota_exhausted"] >= 1 and selection["billing_objective"] == "allowance_weighted", "the exclusion is the exhausted pool, under the weighted objective")


def test_haiku_is_not_declared_so_it_keeps_the_default_weight() -> None:
    """Known consequence of dec_838f872f (no Haiku entry): a weighted Sonnet 5 low undercuts undeclared Haiku."""
    state = _state()
    _check(_pick(_select(state, 14, runtime="claude_code")) == ("claude-sonnet-5", "low"), "required 14 now selects Sonnet 5 low (24 / $0.51) over Haiku (16 / $0.28)")
    with _ProfileFile(_drop_weights):
        _check(_pick(_select(state, 14, runtime="claude_code")) == ("claude-haiku-4.5", "non_reasoning"), "without the table Haiku wins as before")


if __name__ == "__main__":
    print(f"selector under test: {verbs.__file__}")
    test_shipped_profile_declares_the_decided_table()
    test_table_validation_is_loud()
    test_longest_prefix_wins()
    test_pure_selector_objective()
    test_golden_table_on_the_shipped_profile()
    test_ranking_outside_the_plan_is_byte_identical()
    test_missing_table_warns_without_refusing()
    test_allowance_weighted_is_plan_scoped()
    test_profile_faults_refuse_with_their_documented_codes()
    test_receipt_round_trips_and_pins_the_ranking()
    test_an_exhausted_pool_still_excludes_a_weighted_cell()
    test_haiku_is_not_declared_so_it_keeps_the_default_weight()
    print(f"\n{_passed} passed, {len(_failed)} failed")
    sys.exit(1 if _failed else 0)
