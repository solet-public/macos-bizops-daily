#!/usr/bin/env python3
"""Store + verb smoke for the model capability catalog (iss_48ea8171, §9 item 1).

Proves the contract that makes the catalog safe to consult:

* a seed writes every cell as ``pending_crosscheck`` and NEVER ``accepted``;
* ``select_dispatch_tier`` refuses ``catalog_stale`` while nothing is accepted,
  and serves the operator's verdict once cells are accepted and fresh;
* re-seeding never downgrades an accepted cell (idempotent, start-up safe);
* the selected answer carries the current dispatch-policy version;
* the default billing objective is derived from the economics profiles;
* every seeded record names only declared schema columns (the drift class
  the real-shape fake exists to catch);
* ``record_model_capability_cell`` (operator ruling rul_9e7a67ba) writes a
  caller's reading as an accepted, immediately servable cell with its source
  kept as observation evidence, reports the values it replaced, and refuses
  malformed input.

PURE UNIT, offline: real-shape state fake, no database, network, or model turn.
"""

from __future__ import annotations

import sys
from datetime import UTC, datetime, timedelta
from math import inf, nan
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

_SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(_SRC))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _real_state_fake import RealShapeState  # noqa: E402
from ananta.llm.agent_messaging.role_binding import AGENT_ROLE_BINDING_NAMESPACE  # noqa: E402

from agent_messaging_plugin import model_capability_verbs as verbs  # noqa: E402
from agent_messaging_plugin.dispatch_tier_selection import TierSelectionError  # noqa: E402
from agent_messaging_plugin.model_capability_fetch import (  # noqa: E402
    SOURCE_LEADERBOARD_HTML,
    SOURCE_MODEL_DETAIL_HTML,
    Reading,
)
from agent_messaging_plugin.model_capability_store import (  # noqa: E402
    CatalogError,
    default_billing_objective,
    load_seed_table,
    open_refresh_run,
    read_cells,
    reconcile_and_write,
    seed_catalog,
)
from agent_messaging_plugin.schema import (  # noqa: E402
    CELL_ACCEPTANCE_ACCEPTED,
    CELL_ACCEPTANCE_PENDING_CROSSCHECK,
    TABLE_MODEL_CAPABILITY_CELL,
    TABLE_MODEL_CAPABILITY_OBSERVATION,
    TABLE_MODEL_CAPABILITY_REFRESH_RUN,
    get_model_capability_cell_schema,
    get_model_capability_observation_schema,
    get_model_capability_refresh_run_schema,
)

if TYPE_CHECKING:
    from ananta.interfaces.state_management_interface import StateManagementInterface

_passed = 0
_failed: list[str] = []
_NOW = datetime(2026, 9, 22, 12, tzinfo=UTC)  # on or after the newest usage_economics effective_at
_STANDARDIZER = {"id", "external_id", "namespace", "created_at", "updated_at", "created_by", "updated_by", "is_deleted"}


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
    except (CatalogError, TierSelectionError) as exc:
        return exc.code
    return ""


def _typed(fake: RealShapeState) -> StateManagementInterface:
    """The fake IS the real shape; the cast is for pyright only."""
    return cast("StateManagementInterface", fake)


def _accept_all(state: RealShapeState, *, measured_at: datetime) -> None:
    """Stand in for a real cross-checked refresh run: flip every cell to accepted."""
    for row in state.rows(AGENT_ROLE_BINDING_NAMESPACE, TABLE_MODEL_CAPABILITY_CELL):
        row["acceptance"] = CELL_ACCEPTANCE_ACCEPTED
        row["accepted_at"] = measured_at.isoformat()
        row["measured_at"] = measured_at.isoformat()
        row["last_refresh_run_id"] = "mcr-test-accepted"


def test_seed_writes_pending_never_accepted() -> None:
    state = RealShapeState()
    seed = load_seed_table()
    result = seed_catalog(_typed(state), seed=seed)
    cells = read_cells(_typed(state))
    _check(result["cells_seeded"] == len(seed.cells) == len(cells), f"seed writes every cell once ({len(cells)})")
    _check(all(row["acceptance"] == CELL_ACCEPTANCE_PENDING_CROSSCHECK for row in cells), "every seeded cell is pending_crosscheck")
    _check(not any(row["acceptance"] == CELL_ACCEPTANCE_ACCEPTED for row in cells), "a seed never accepts")
    runs = state.rows(AGENT_ROLE_BINDING_NAMESPACE, TABLE_MODEL_CAPABILITY_REFRESH_RUN)
    _check(len(runs) == 1 and runs[0]["trigger"] == "seed" and runs[0]["status"] == "completed", "one completed seed run row")
    observations = state.rows(AGENT_ROLE_BINDING_NAMESPACE, TABLE_MODEL_CAPABILITY_OBSERVATION)
    priced = sum(1 for cell in seed.cells if cell.cost_per_task_usd is not None)
    _check(len(observations) == len(seed.cells) + priced, "one observation per seeded metric, source seed_table")


def _cell(rows: list[dict[str, object]], model: str, effort: str) -> dict[str, object]:
    return next(row for row in rows if row["model"] == model and row["effort"] == effort)


def test_seed_derives_relative_multipliers() -> None:
    state = RealShapeState()
    seed_catalog(_typed(state), seed=load_seed_table())
    cells = read_cells(_typed(state))
    luna = _cell(cells, "gpt-5.6-luna", "low")
    sonnet = _cell(cells, "claude-sonnet-5", "max")
    haiku = _cell(cells, "claude-haiku-4.5", "non_reasoning")
    _check(luna["relative_cost_multiplier"] == 1.0, "the cheapest priced seed cell is the 1x anchor")
    _check(round(float(str(sonnet["relative_cost_multiplier"]))) == 509, "sonnet-5 max is 509x the anchor, as the operator's table says")
    _check(haiku["relative_cost_multiplier"] is None, "an unpriced cell gets no multiplier, never a guessed one")


def test_seeded_records_name_only_declared_columns() -> None:
    state = RealShapeState()
    seed_catalog(_typed(state), seed=load_seed_table())
    declared = {
        TABLE_MODEL_CAPABILITY_CELL: set(get_model_capability_cell_schema().columns),
        TABLE_MODEL_CAPABILITY_OBSERVATION: set(get_model_capability_observation_schema().columns),
        TABLE_MODEL_CAPABILITY_REFRESH_RUN: set(get_model_capability_refresh_run_schema().columns),
    }
    for table, columns in declared.items():
        rows = state.rows(AGENT_ROLE_BINDING_NAMESPACE, table)
        stray = {key for row in rows for key in row} - columns - _STANDARDIZER
        _check(not stray, f"{table}: no undeclared columns written ({sorted(stray)})")


def test_selector_refuses_until_accepted_then_serves() -> None:
    state = RealShapeState()
    seed_catalog(_typed(state), seed=load_seed_table())
    params = {"required_score": 38, "billing_objective": "metered_usd", "dispatch_kind": "review"}
    _check(_code(lambda: verbs.select_dispatch_tier(_typed(state), params, now=_NOW)) == "catalog_stale", "pending cells are never served: catalog_stale")
    _accept_all(state, measured_at=_NOW - timedelta(hours=1))
    picked = verbs.select_dispatch_tier(_typed(state), params, now=_NOW)
    _check(picked["selected"]["model"] == "gpt-5.6-sol" and picked["selected"]["effort"] == "medium", "review kind, score 38: sol medium (39, $0.50) beats terra xhigh (38, $0.63)")
    _check(picked["excluded"]["quota_unknown"] > 0, "unknown flat-rate quota cells are excluded and counted")
    _check(picked["policy_version"] == "model-dispatch-policy-v3", "policy version reported when a kind was consulted")
    _check(picked["catalog_run_id"] == "mcr-test-accepted", "the accepting run id rides the answer")
    _check(
        picked["selection_receipt"]["selected"] == {"runtime": "codex", "model": "gpt-5.6-sol", "effort": "medium"},
        "selector returns a spawn-verifiable receipt for its cheapest answer",
    )
    receipt = picked["selection_receipt"]
    _check(
        verbs.verify_selection_receipt(
            _typed(state), difficulty_score=38, selection_receipt=receipt, dispatch_kind="review",
            scope_tags=(), agent_runtime="codex", model="gpt-5.6-sol", effort="medium", now=_NOW,
        ) == receipt,
        "spawn receipt replays to the current cheapest-clearing tuple",
    )
    _check(
        _code(lambda: verbs.verify_selection_receipt(
            _typed(state), difficulty_score=38, selection_receipt=receipt, dispatch_kind="review",
            scope_tags=(), agent_runtime="codex", model="gpt-5.6-sol", effort="high", now=_NOW,
        )) == "selection_receipt_mismatch",
        "a spawn tuple not selected by the receipt fails closed",
    )
    unconstrained = verbs.select_dispatch_tier(_typed(state), {"required_score": 45, "billing_objective": "metered_usd"}, now=_NOW)
    _check(unconstrained["selected"]["model"] == "gpt-6-astra" and unconstrained["selected"]["effort"] == "low", "no kind, score 45: astra low -- the operator's example")
    _check(unconstrained["policy_version"] is None, "policy version is null when no kind was consulted")
    relative = verbs.select_dispatch_tier(_typed(state), {"required_score": 45}, now=_NOW)
    _check(relative["billing_objective"] == "relative" and relative["selected"]["model"] == "gpt-6-astra", "default objective on this checkout is relative (flat-rate plan declared) and agrees")
    _accept_all(state, measured_at=_NOW - timedelta(hours=100))
    _check(_code(lambda: verbs.select_dispatch_tier(_typed(state), params, now=_NOW)) == "catalog_stale", "accepted but past the 72h window: catalog_stale")
    _accept_all(state, measured_at=_NOW - timedelta(hours=10))
    _check(_code(lambda: verbs.select_dispatch_tier(_typed(state), {**params, "max_staleness_hours": 6}, now=_NOW)) == "catalog_stale", "a caller may tighten the window")
    _check(_code(lambda: verbs.select_dispatch_tier(_typed(state), {**params, "max_staleness_hours": 500}, now=_NOW)) == "", "a caller cannot loosen the window (500h clamps to stored 72h; fresh at 10h)")


def test_reseed_preserves_accepted_cells() -> None:
    state = RealShapeState()
    seed = load_seed_table()
    seed_catalog(_typed(state), seed=seed)
    _accept_all(state, measured_at=_NOW)
    for row in state.rows(AGENT_ROLE_BINDING_NAMESPACE, TABLE_MODEL_CAPABILITY_CELL):
        if row["model"] == "claude-opus-5" and row["effort"] == "low":
            row["capability_score"] = 41.0
    result = seed_catalog(_typed(state), seed=seed)
    _check(result["cells_seeded"] == 0 and len(result["accepted_cells_preserved"]) == len(seed.cells), "re-seed touches no accepted cell")
    opus = next(row for row in read_cells(_typed(state)) if row["model"] == "claude-opus-5" and row["effort"] == "low")
    _check(opus["capability_score"] == 41.0 and opus["acceptance"] == CELL_ACCEPTANCE_ACCEPTED, "an accepted, refreshed value survives a re-seed")
    _check(len(read_cells(_typed(state))) == len(seed.cells), "re-seed creates no duplicate rows")


def test_read_verb_and_refusals() -> None:
    state = RealShapeState()
    seed_catalog(_typed(state), seed=load_seed_table())
    hidden = verbs.read_model_capability_catalog(_typed(state), runtime=None, model=None, include_unaccepted=False, now=_NOW)
    _check(hidden["cells"] == [] and hidden["total_cells"] > 0 and hidden["servable_cells"] == 0, "read hides pending cells unless asked")
    shown = verbs.read_model_capability_catalog(_typed(state), runtime="codex", model="gpt-6-astra", include_unaccepted=True, now=_NOW)
    _check(len(shown["cells"]) == 5 and all(not row["servable"] for row in shown["cells"]), "read filters by runtime/model and marks pending cells unservable")
    _check(_code(lambda: verbs.select_dispatch_tier(_typed(state), {}, now=_NOW)) == "parameter_invalid", "missing required_score refuses")
    _check(_code(lambda: verbs.select_dispatch_tier(_typed(state), {"required_score": 1, "billing_objective": "tokens"}, now=_NOW)) == "parameter_invalid", "unknown objective refuses")
    _check(_code(lambda: verbs.select_dispatch_tier(_typed(state), {"required_score": 1, "max_staleness_hours": 0}, now=_NOW)) == "parameter_invalid", "non-positive window refuses")
    _check(_code(lambda: verbs.select_dispatch_tier(_typed(RealShapeState()), {"required_score": 1}, now=_NOW)) == "catalog_empty", "an unseeded catalog refuses as empty")


def test_billing_objective_is_derived() -> None:
    _check(default_billing_objective("claude_code", now=_NOW) == "relative", "claude_code on this checkout: flat-rate plan -> relative")
    _check(default_billing_objective("codex", now=_NOW) == "metered_usd", "codex on this checkout: no flat-rate profile -> metered_usd")


def test_provider_fault_is_loud() -> None:
    from ananta.llm.agent_messaging.state_results import StateOperationError

    state = RealShapeState()
    state.fail_next("write")
    try:
        seed_catalog(_typed(state), seed=load_seed_table())
    except StateOperationError:
        _check(True, "a failed state write raises StateOperationError, never a quiet partial seed")
        return
    _check(False, "a failed state write raises StateOperationError, never a quiet partial seed")


_GPT6_SOL_HIGH = {
    "runtime": "codex", "model": "gpt-6-sol", "effort": "high", "capability_score": 43,
    "cost_per_task_usd": 0.37, "source_ref": "https://artificialanalysis.ai/models/gpt-6-sol-high",
    "note": "released 2026-09-22",
}


def test_record_verb_accepts_and_serves_a_new_model() -> None:
    state = RealShapeState()
    result = verbs.record_model_capability_cell(_typed(state), dict(_GPT6_SOL_HIGH))
    _check(result["acceptance"] == CELL_ACCEPTANCE_ACCEPTED and result["previous"] is None, "a recorded reading for a new model is accepted, with no previous cell")
    cell = _cell(read_cells(_typed(state)), "gpt-6-sol", "high")
    _check(cell["acceptance"] == CELL_ACCEPTANCE_ACCEPTED and cell["capability_score"] == 43.0 and cell["cost_per_task_usd"] == 0.37, "the cell holds the recorded score and cost")
    _check(cell["provider"] == "openai" and cell["agreeing_observation_ids"] == result["observation_ids"], "provider derives from runtime; the cell cites its evidence rows")
    _check_record_evidence_and_selection(state)


def _check_record_evidence_and_selection(state: RealShapeState) -> None:
    observations = state.rows(AGENT_ROLE_BINDING_NAMESPACE, TABLE_MODEL_CAPABILITY_OBSERVATION)
    _check(
        len(observations) == 2 and all(o["source_id"] == "manual_record" and _GPT6_SOL_HIGH["source_ref"] in o["fetch_method"] for o in observations),
        "one observation per metric, source manual_record, fetch_method naming the caller's source",
    )
    runs = state.rows(AGENT_ROLE_BINDING_NAMESPACE, TABLE_MODEL_CAPABILITY_REFRESH_RUN)
    _check(len(runs) == 1 and runs[0]["trigger"] == "manual" and runs[0]["status"] == "completed" and runs[0]["cells_accepted"] == 1, "one completed manual run row")
    picked = verbs.select_dispatch_tier(_typed(state), {"required_score": 40, "billing_objective": "metered_usd"}, now=datetime.now(UTC))
    _check(picked["selected"]["model"] == "gpt-6-sol" and picked["selected"]["effort"] == "high", "the recorded cell is servable to select_dispatch_tier at once")


def test_manual_relative_costs_follow_the_whole_table() -> None:
    state = RealShapeState()
    for model, score, cost in (("gpt-6-sol", 50, 10.0), ("gpt-6-luna", 40, 1.0)):
        verbs.record_model_capability_cell(_typed(state), {
            "runtime": "codex", "model": model, "effort": "high", "capability_score": score,
            "cost_per_task_usd": cost, "source_ref": "review cost control",
        })
    rows = read_cells(_typed(state))
    _check(_cell(rows, "gpt-6-sol", "high")["relative_cost_multiplier"] == 10.0, "prior manual cell is rebased to the current price floor")
    picked = verbs.select_dispatch_tier(_typed(state), {"required_score": 35, "billing_objective": "relative"}, now=datetime.now(UTC))
    _check(picked["selected"]["model"] == "gpt-6-luna", "relative selector chooses the cheaper eligible cell")


def test_manual_reading_conflicts_with_later_live_readings() -> None:
    state = RealShapeState()
    verbs.record_model_capability_cell(_typed(state), {**_GPT6_SOL_HIGH, "capability_score": 90, "cost_per_task_usd": 1.0})
    readings = [
        Reading(
            source_id=source, provider="openai", runtime="codex", model="gpt-6-sol", effort="high",
            metric=metric, value_number=value, value_text=None, raw_excerpt="review control",
            fetch_status="ok", fetched_at=datetime.now(UTC).isoformat(),
        )
        for source in (SOURCE_LEADERBOARD_HTML, SOURCE_MODEL_DETAIL_HTML)
        for metric, value in (("intelligence_index", 40.0), ("cost_per_task_usd", 1.0))
    ]
    counts = reconcile_and_write(_typed(state), open_refresh_run(_typed(state), trigger="manual"), readings)
    cell = _cell(read_cells(_typed(state)), "gpt-6-sol", "high")
    _check(counts["cells_conflicted"] == 1 and cell["acceptance"] == "crosscheck_conflict", "later live readings cannot silently replace a conflicting direct reading")
    _check("manual versus live" in str(cell["disagreement_note"]), "conflict names the manual/live boundary")


def test_record_overwrites_and_reports_previous() -> None:
    state = RealShapeState()
    seed_catalog(_typed(state), seed=load_seed_table())
    before = _cell(read_cells(_typed(state)), "gpt-5.6-sol", "high")
    result = verbs.record_model_capability_cell(_typed(state), {
        "runtime": "codex", "model": "gpt-5.6-sol", "effort": "high", "capability_score": 41,
        "cost_per_task_usd": 0.79, "source_ref": "register iev_test",
    })
    after = _cell(read_cells(_typed(state)), "gpt-5.6-sol", "high")
    previous = result["previous"]
    _check(isinstance(previous, dict) and previous["acceptance"] == CELL_ACCEPTANCE_PENDING_CROSSCHECK and previous["capability_score"] == before["capability_score"], "previous reports the replaced values and acceptance")
    _check(after["acceptance"] == CELL_ACCEPTANCE_ACCEPTED and after["capability_score"] == 41.0, "a recorded reading overwrites a pending seed cell as accepted")
    others = [row for row in read_cells(_typed(state)) if (row["model"], row["effort"]) != ("gpt-5.6-sol", "high")]
    _check(all(row["acceptance"] == CELL_ACCEPTANCE_PENDING_CROSSCHECK for row in others), "no other cell is touched")


def test_record_refusals() -> None:
    state = RealShapeState()
    bad = {
        "unknown runtime": {**_GPT6_SOL_HIGH, "runtime": "gemini"},
        "effort outside the selector's ladder": {**_GPT6_SOL_HIGH, "effort": "ultra"},
        "score above 100": {**_GPT6_SOL_HIGH, "capability_score": 101},
        "missing score": {key: value for key, value in _GPT6_SOL_HIGH.items() if key != "capability_score"},
        "negative cost": {**_GPT6_SOL_HIGH, "cost_per_task_usd": -1},
        "NaN cost": {**_GPT6_SOL_HIGH, "cost_per_task_usd": nan},
        "infinite cost": {**_GPT6_SOL_HIGH, "cost_per_task_usd": inf},
        "blank source_ref": {**_GPT6_SOL_HIGH, "source_ref": "  "},
        "boolean score": {**_GPT6_SOL_HIGH, "capability_score": True},
        "zero staleness window": {**_GPT6_SOL_HIGH, "staleness_window_hours": 0},
    }
    for label, params in bad.items():
        _check(_code(lambda params=params: verbs.record_model_capability_cell(_typed(state), params)) == "parameter_invalid", f"refuses {label}")
    _check(read_cells(_typed(state)) == [], "a refused call writes nothing")


def test_recorded_rows_name_only_declared_columns() -> None:
    state = RealShapeState()
    verbs.record_model_capability_cell(_typed(state), dict(_GPT6_SOL_HIGH))
    declared = {
        TABLE_MODEL_CAPABILITY_CELL: set(get_model_capability_cell_schema().columns),
        TABLE_MODEL_CAPABILITY_OBSERVATION: set(get_model_capability_observation_schema().columns),
        TABLE_MODEL_CAPABILITY_REFRESH_RUN: set(get_model_capability_refresh_run_schema().columns),
    }
    for table, columns in declared.items():
        rows = state.rows(AGENT_ROLE_BINDING_NAMESPACE, table)
        stray = {key for row in rows for key in row} - columns - _STANDARDIZER
        _check(bool(rows) and not stray, f"record verb {table}: rows written, no undeclared columns ({sorted(stray)})")


class _QueryLog(RealShapeState):
    """The real-shape fake, recording every query_ordered payload."""

    def __init__(self) -> None:
        super().__init__()
        self.queries: list[dict[str, object]] = []

    def query_ordered(self, namespace: str, query: dict[str, Any]) -> dict[str, Any]:
        self.queries.append(query)
        return super().query_ordered(namespace, query)


def test_filtered_read_pushes_filters_down_and_bounds_the_floor() -> None:
    state = _QueryLog()
    for model, score, cost in (("gpt-6-sol", 50, 10.0), ("gpt-6-luna", 40, 1.0)):
        verbs.record_model_capability_cell(_typed(state), {**_GPT6_SOL_HIGH, "model": model, "capability_score": score, "cost_per_task_usd": cost})
    state.queries.clear()
    rows = read_cells(_typed(state), runtime="codex", model="gpt-6-sol")
    _check([row["model"] for row in rows] == ["gpt-6-sol"] and rows[0]["relative_cost_multiplier"] == 10.0, "a filtered read returns only its rows, priced against the table-wide floor")
    filters = [query.get("filters") for query in state.queries]
    _check({"runtime": "codex", "model": "gpt-6-sol"} in filters and {} not in filters, "runtime and model filters are sent to the database; nothing reads the whole table")
    bounded = [query for query in state.queries if query.get("limit") == 1]
    _check(len(bounded) == 1 and bounded[0]["order_by"][0] == ["cost_per_task_usd", "asc"], "the price floor is one bounded row, not a table scan")


if __name__ == "__main__":
    test_seed_writes_pending_never_accepted()
    test_seed_derives_relative_multipliers()
    test_seeded_records_name_only_declared_columns()
    test_selector_refuses_until_accepted_then_serves()
    test_reseed_preserves_accepted_cells()
    test_read_verb_and_refusals()
    test_billing_objective_is_derived()
    test_provider_fault_is_loud()
    test_record_verb_accepts_and_serves_a_new_model()
    test_manual_relative_costs_follow_the_whole_table()
    test_manual_reading_conflicts_with_later_live_readings()
    test_record_overwrites_and_reports_previous()
    test_record_refusals()
    test_recorded_rows_name_only_declared_columns()
    test_filtered_read_pushes_filters_down_and_bounds_the_floor()
    print(f"\n{_passed} passed, {len(_failed)} failed")
    raise SystemExit(1 if _failed else 0)
