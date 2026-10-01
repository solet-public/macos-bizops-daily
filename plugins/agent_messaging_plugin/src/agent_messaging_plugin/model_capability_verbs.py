"""Model capability catalog verbs (iss_48ea8171): seed, read, select, record.

Validation layer between the plugin's ``@platform_process`` methods and the
store + pure selector. Every refusal is a ``CatalogError`` or
``TierSelectionError`` with a stable code; the plugin method maps it to a
failure envelope unchanged. Nothing here touches the network — the refresh
that would is a later, separately authorized unit.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

import httpx

from .allowance_pool_readings import (
    apply_pool_readings,
    read_allowance_pool_readings,
    read_pool_readings,
    record_allowance_pool_reading,
    retract_allowance_pool_reading,
)
from .dispatch_tier_selection import BillingObjective, CapabilityCell, TierSelection, TierSelectionError, select_tier
from .model_capability_fetch import fetch_all_readings
from .model_capability_store import (
    KNOWN_RUNTIMES,
    CatalogError,
    ManualCell,
    catalog_staleness_window,
    close_refresh_run,
    default_billing_objective,
    load_seed_table,
    open_refresh_run,
    read_cells,
    reconcile_and_write,
    record_manual_cell,
    seed_catalog,
    selection_catalog,
)
from .model_dispatch_policy import (
    AppliedFloor,
    DispatchPolicyError,
    applied_capability_floors,
    load_dispatch_policy,
    validate_dispatch_kind,
)
from .schema import CELL_ACCEPTANCE_ACCEPTED
from .usage_economics_profiles import (
    FlatRateQuotaProfile,
    UsageEconomicsProfileValidationError,
    load_usage_economics_profile_catalog,
)
from .usage_economics_quota import plan_for_runtime, quota_status_for_pair

if TYPE_CHECKING:
    from ananta.interfaces.state_management_interface import StateManagementInterface

_OBJECTIVES: frozenset[str] = frozenset({"metered_usd", "relative", "allowance_weighted"})
_USAGE_ECONOMICS_PROFILE_PATH = Path(__file__).resolve().parents[2] / "model_profiles" / "usage_economics.v1.json"


def seed_model_capability_catalog(state: StateManagementInterface, *, staleness_window_hours: int | None) -> dict[str, Any]:
    """Load the declarative seed and write it as pending cells (never accepted)."""
    seed = load_seed_table()
    if staleness_window_hours is None:
        return seed_catalog(state, seed=seed)
    return seed_catalog(state, seed=seed, staleness_window_hours=staleness_window_hours)


_VALID_TRIGGERS: frozenset[str] = frozenset({"manual", "cron", "policy_change"})


def refresh_model_capability_catalog(state: StateManagementInterface, *, trigger: str, client: httpx.Client | None = None) -> dict[str, Any]:
    """iss_d136ae29 -- the real crosscheck refresh: fetch live readings from
    the two independent Artificial Analysis pages, reconcile each roster
    cell, and write the result. This is the only verb that can ever move a
    cell to `accepted`; `seed_model_capability_catalog` never does.

    A source that fails to fetch is recorded on the run's `sources_failed`
    column, never silently dropped -- and never aborts the whole run: one
    bad URL for one model+effort must not blank out every other cell's
    otherwise-good readings.

    `client` is an injection point for tests (an `httpx.Client` built on a
    `MockTransport`) -- production callers never pass it and get a real one.
    """
    if trigger not in _VALID_TRIGGERS:
        raise CatalogError("parameter_invalid", f"trigger must be one of {sorted(_VALID_TRIGGERS)}; got {trigger!r}.")
    run_id = open_refresh_run(state, trigger=trigger)
    owns_client = client is None
    active_client = client or httpx.Client()
    try:
        try:
            readings, failures = fetch_all_readings(active_client)
        except Exception as exc:  # noqa: BLE001 -- a refresh run must close cleanly even on an unanticipated fetch-layer fault
            close_refresh_run(
                state, run_id,
                counts={"cells_accepted": 0, "cells_conflicted": 0, "cells_unchanged": 0},
                sources_ok=[], sources_failed=[f"unexpected: {exc}"], note=f"refresh aborted before reconciliation: {exc}",
            )
            raise CatalogError("refresh_fetch_failed", f"model capability refresh could not fetch: {exc}") from exc
    finally:
        if owns_client:
            active_client.close()
    sources_ok = sorted({reading.source_id for reading in readings})
    sources_failed = [f"{source_id}: {reason}" for source_id, reason in failures]
    counts = reconcile_and_write(state, run_id, readings)
    note = f"{len(readings)} readings from {len(sources_ok)} source(s); {len(sources_failed)} source fetch(es) failed" if sources_failed else None
    close_refresh_run(state, run_id, counts=counts, sources_ok=sources_ok, sources_failed=sources_failed, note=note)
    return {
        "run_id": run_id,
        "trigger": trigger,
        "readings_gathered": len(readings),
        "sources_ok": sources_ok,
        "sources_failed": sources_failed,
        **counts,
    }


def _required_text(raw: object, field: str) -> str:
    if not isinstance(raw, str) or not raw.strip():
        raise CatalogError("parameter_invalid", f"{field} is required and must be a non-blank string.")
    return raw.strip()


def _optional_number(raw: object, field: str) -> float | None:
    if raw is None or raw == "":
        return None
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        raise CatalogError("parameter_invalid", f"{field} must be a number.")
    return float(raw)


def record_model_capability_cell(state: StateManagementInterface, params: dict[str, Any]) -> dict[str, Any]:
    """Operator ruling rul_9e7a67ba: anyone with better information records one
    catalog cell directly, as accepted, with the source it came from."""
    score = _optional_number(params.get("capability_score"), "capability_score")
    if score is None:
        raise CatalogError("parameter_invalid", "capability_score is required.")
    note_raw = params.get("note")
    if note_raw is not None and not isinstance(note_raw, str):
        raise CatalogError("parameter_invalid", "note must be a string when given.")
    window_raw = params.get("staleness_window_hours")
    if window_raw not in (None, "") and (isinstance(window_raw, bool) or not isinstance(window_raw, int)):
        raise CatalogError("parameter_invalid", "staleness_window_hours must be an integer.")
    cell = ManualCell(
        runtime=_required_text(params.get("runtime"), "runtime"),
        model=_required_text(params.get("model"), "model"),
        effort=_required_text(params.get("effort"), "effort"),
        capability_score=score,
        cost_per_task_usd=_optional_number(params.get("cost_per_task_usd"), "cost_per_task_usd"),
        source_ref=_required_text(params.get("source_ref"), "source_ref"),
        note=(note_raw.strip() or None) if isinstance(note_raw, str) else None,
    )
    if window_raw in (None, ""):
        return record_manual_cell(state, cell)
    return record_manual_cell(state, cell, staleness_window_hours=window_raw)


def _is_fresh(row: dict[str, Any], *, now: datetime) -> bool:
    measured = row.get("measured_at")
    window = row.get("staleness_window_hours")
    if not isinstance(measured, str) or not isinstance(window, int):
        return False
    try:
        measured_at = datetime.fromisoformat(measured.replace("Z", "+00:00"))
    except ValueError:
        return False
    return measured_at.tzinfo is not None and now - measured_at <= timedelta(hours=window)


def read_model_capability_catalog(
    state: StateManagementInterface, *, runtime: str | None, model: str | None, include_unaccepted: bool, now: datetime,
) -> dict[str, Any]:
    """The catalog as stored, each row annotated with whether dispatch may use it now."""
    rows = read_cells(state, runtime=runtime, model=model)
    annotated: list[dict[str, Any]] = []
    for row in rows:
        accepted = row.get("acceptance") == CELL_ACCEPTANCE_ACCEPTED
        if not accepted and not include_unaccepted:
            continue
        fresh = _is_fresh(row, now=now)
        annotated.append({**row, "is_fresh": fresh, "servable": accepted and fresh})
    return {
        "cells": annotated,
        "total_cells": len(rows),
        "servable_cells": sum(1 for row in annotated if row["servable"]),
        "as_of": now.isoformat(),
    }


def _plan_for(runtime: str | None, *, now: datetime) -> FlatRateQuotaProfile | None:
    """The current flat-rate plan covering the runtime constraint; none without a constraint or a covering plan."""
    if runtime is None:
        return None
    try:
        return plan_for_runtime(load_usage_economics_profile_catalog(_USAGE_ECONOMICS_PROFILE_PATH, as_of=now), runtime)
    except UsageEconomicsProfileValidationError as exc:
        raise CatalogError("usage_economics_invalid", f"usage economics profiles unreadable: {exc}") from exc


def _plan_weight_fn(plan: FlatRateQuotaProfile | None) -> Callable[[str, str], float] | None:
    """The plan's declared weight per (runtime, model); cells outside the plan's runtimes weigh 1.0."""
    if plan is None or plan.dispatch_weights is None:
        return None
    weights = plan.dispatch_weights
    return lambda runtime, model: weights.weight_for(model) if runtime in plan.included_runtimes else 1.0


def _objective(raw: object, *, plan: FlatRateQuotaProfile | None, now: datetime) -> BillingObjective:
    weighted = plan is not None and plan.dispatch_weights is not None
    if raw is None or raw == "":
        return "allowance_weighted" if weighted else default_billing_objective(None, now=now)
    if not isinstance(raw, str) or raw not in _OBJECTIVES:
        raise CatalogError("parameter_invalid", f"billing_objective must be one of {sorted(_OBJECTIVES)}; got {raw!r}.")
    if raw == "allowance_weighted" and not weighted:
        raise CatalogError(
            "parameter_invalid",
            "allowance_weighted is plan-scoped: name a runtime covered by a current flat-rate plan that declares dispatch_weights.",
        )
    return cast(BillingObjective, raw)


def _selection_warnings(raw_objective: object, plan: FlatRateQuotaProfile | None) -> dict[str, list[str]]:
    """``{"warnings": [...]}`` when a plan covers the runtime but declares no weights and the caller took the default ranking."""
    if plan is None or plan.dispatch_weights is not None or raw_objective not in (None, ""):
        return {}
    return {"warnings": [f"plan {plan.profile_id} declares no dispatch_weights; ranking equals metered dollars."]}


def _number(raw: object, field: str, *, default: float) -> float:
    if raw is None or raw == "":
        return default
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        raise CatalogError("parameter_invalid", f"{field} must be a number.")
    return float(raw)


def _capability_floor_pairs(
    dispatch_kind: str | None, scope_tags: tuple[str, ...],
) -> tuple[frozenset[tuple[str, str]] | None, str | None, tuple[AppliedFloor, ...]]:
    """Capability-floor pairs only; dispatch kind is provenance, never a filter."""
    if dispatch_kind is None and not scope_tags:
        return None, None, ()
    policy = load_dispatch_policy()
    if dispatch_kind is not None:
        validate_dispatch_kind(dispatch_kind)
    allowed: frozenset[tuple[str, str]] | None = None
    floors = applied_capability_floors(policy, scope_tags=scope_tags, brief_text="")
    for floor in floors:
        floor_pairs = frozenset(policy.capability_floors[floor.tag].floor_pairs)
        allowed = floor_pairs if allowed is None else allowed & floor_pairs
    return allowed, policy.policy_version, floors


def _window(cells: list[dict[str, Any]], max_staleness_hours: object) -> timedelta:
    stored = catalog_staleness_window(cells)
    if max_staleness_hours is None or max_staleness_hours == "":
        return stored
    if isinstance(max_staleness_hours, bool) or not isinstance(max_staleness_hours, int) or max_staleness_hours <= 0:
        raise CatalogError("parameter_invalid", "max_staleness_hours must be a positive integer.")
    return min(stored, timedelta(hours=max_staleness_hours))


def _cell_payload(cell: CapabilityCell, *, weighted: bool) -> dict[str, Any]:
    payload = asdict(cell)
    payload["measured_at"] = cell.measured_at.isoformat()
    if not weighted:
        del payload["plan_weight"]
    return payload


def _cost_basis(plan: FlatRateQuotaProfile, selected: CapabilityCell) -> dict[str, Any]:
    """The declared weight table behind an allowance_weighted answer, for the audit trail."""
    weights = plan.dispatch_weights
    if weights is None:
        raise CatalogError("catalog_invalid", f"plan {plan.profile_id} lost its dispatch_weights mid-selection.")
    return {
        "plan_profile_id": plan.profile_id,
        "profile_version": plan.profile_version,
        "plan_id": plan.plan_id,
        "basis": weights.basis,
        "ruling_id": weights.ruling_id,
        "evidence_ref": weights.evidence_ref,
        "declared_at": weights.declared_at.isoformat(),
        "default_weight": weights.default_weight,
        "by_model_prefix": dict(weights.by_model_prefix),
        "selected_weight": selected.plan_weight,
        "note": weights.note,
    }


def _selection_payload(
    selection: TierSelection,
    *,
    run_id: str | None,
    score_margin: float,
    cost_tolerance: float,
    max_staleness_hours: int | None,
    runtime_constraint: str | None,
    plan: FlatRateQuotaProfile | None,
) -> dict[str, Any]:
    weighted = selection.objective == "allowance_weighted"
    payload = {
        "selected": _cell_payload(selection.selected, weighted=weighted),
        "billing_objective": selection.objective,
        "required_score": selection.required_score,
        "effective_required_score": selection.effective_required_score,
        "frontier": [_cell_payload(cell, weighted=weighted) for cell in selection.frontier],
        "dominated": [
            {"weaker": _cell_payload(weaker, weighted=weighted), "stronger": _cell_payload(stronger, weighted=weighted)}
            for weaker, stronger in selection.dominated
        ],
        "ladder": [asdict(step) for step in selection.ladder],
        "excluded": dict(selection.excluded),
        "catalog_run_id": run_id,
    }
    if weighted and plan is not None:
        payload["cost_basis"] = _cost_basis(plan, selection.selected)
    payload["selection_receipt"] = {
        "required_score": selection.required_score,
        "billing_objective": selection.objective,
        "score_margin": score_margin,
        "cost_tolerance": cost_tolerance,
        "max_staleness_hours": max_staleness_hours,
        "runtime_constraint": runtime_constraint,
        "selected": {
            "runtime": selection.selected.runtime,
            "model": selection.selected.model,
            "effort": selection.selected.effort,
        },
    }
    return payload


def _quota_statuses(
    state: StateManagementInterface, catalog: tuple[CapabilityCell, ...], *, now: datetime,
) -> tuple[dict[tuple[str, str], str], tuple[str, ...]]:
    """Quota status per pair from the static profile with recorded readings laid over it, plus expired-reading notes."""
    try:
        static = load_usage_economics_profile_catalog(_USAGE_ECONOMICS_PROFILE_PATH, as_of=now)
    except UsageEconomicsProfileValidationError as exc:
        raise CatalogError("quota_state_unknown", f"quota profile is unavailable: {exc}") from exc
    economics, expired = apply_pool_readings(static, read_pool_readings(state), now=now)
    statuses = {cell.pair: quota_status_for_pair(economics, runtime=cell.runtime, model=cell.model) for cell in catalog}
    return statuses, expired


def _required_score(params: dict[str, Any]) -> float:
    raw = params.get("required_score")
    if raw is None or raw == "":
        raise CatalogError("parameter_invalid", "required_score is required.")
    return _number(raw, "required_score", default=0.0)


def _runtime_constraint(raw: object) -> str | None:
    if raw is None or raw == "":
        return None
    if not isinstance(raw, str) or raw not in KNOWN_RUNTIMES:
        raise CatalogError("parameter_invalid", f"runtime must be one of {list(KNOWN_RUNTIMES)}; got {raw!r}.")
    return raw


def _select_within(
    catalog: tuple[CapabilityCell, ...], runtime: str | None, expired_readings: tuple[str, ...], **kwargs: Any,
) -> TierSelection:
    """Run the pure selector over only ``runtime``'s cells, filtered before domination and choice.

    A refusal names the runtime and any expired allowance readings, which no longer count as exhausted.
    """
    candidates = catalog if runtime is None else tuple(cell for cell in catalog if cell.runtime == runtime)
    try:
        return select_tier(candidates, **kwargs)
    except TierSelectionError as exc:
        message = exc.message if runtime is None else f"runtime={runtime}: {exc.message}"
        if expired_readings:
            message = f"{message} Expired allowance readings no longer count: {list(expired_readings)}."
        raise TierSelectionError(exc.code, message) from exc


def select_dispatch_tier(state: StateManagementInterface, params: dict[str, Any], *, now: datetime | None = None) -> dict[str, Any]:
    """Cheapest fresh, accepted, policy-allowed (model, effort) clearing the required score, within ``runtime`` when given."""
    moment = now or datetime.now(UTC)
    required_score = _required_score(params)
    dispatch_kind = params.get("dispatch_kind")
    if dispatch_kind is not None and not isinstance(dispatch_kind, str):
        raise DispatchPolicyError("dispatch_policy_violation", f"unknown dispatch_kind {dispatch_kind!r}.")
    floor_pairs, policy_version, floors = _capability_floor_pairs(
        dispatch_kind, _scope_tags(params.get("scope_tags")),
    )
    runtime = _runtime_constraint(params.get("runtime"))
    rows = read_cells(state)
    plan = _plan_for(runtime, now=moment)
    catalog = selection_catalog(rows, _plan_weight_fn(plan))
    objective = _objective(params.get("billing_objective"), plan=plan, now=moment)
    score_margin = _number(params.get("score_margin"), "score_margin", default=0.0)
    cost_tolerance = _number(params.get("cost_tolerance"), "cost_tolerance", default=0.05)
    raw_max_staleness = params.get("max_staleness_hours")
    max_staleness_hours = None if raw_max_staleness is None or raw_max_staleness == "" else raw_max_staleness
    quota_status_by_pair, expired_readings = _quota_statuses(state, catalog, now=moment)
    selection = _select_within(
        catalog,
        runtime,
        expired_readings,
        required_score=required_score,
        objective=objective,
        now=moment,
        max_age=_window(rows, params.get("max_staleness_hours")),
        capability_floor_pairs=floor_pairs,
        quota_status_by_pair=quota_status_by_pair,
        score_margin=score_margin,
        cost_tolerance=cost_tolerance,
    )
    run_ids = {str(row.get("last_refresh_run_id")) for row in rows if row.get("acceptance") == CELL_ACCEPTANCE_ACCEPTED}
    payload = _selection_payload(
        selection,
        run_id=max(run_ids) if run_ids else None,
        score_margin=score_margin,
        cost_tolerance=cost_tolerance,
        max_staleness_hours=max_staleness_hours,
        runtime_constraint=runtime,
        plan=plan,
    )
    payload.update(_selection_warnings(params.get("billing_objective"), plan))
    payload["policy_version"] = policy_version
    payload["capability_floors"] = [asdict(floor) for floor in floors]
    payload["unscored_cells"] = len(rows) - len(catalog)
    payload["expired_allowance_readings"] = list(expired_readings)
    return payload


def record_pool_reading(state: StateManagementInterface, params: dict[str, Any], *, now: datetime | None = None) -> dict[str, Any]:
    """Record one explicit allowance-pool reading against the shipped usage profile."""
    return record_allowance_pool_reading(state, params, profile_path=_USAGE_ECONOMICS_PROFILE_PATH, now=now or datetime.now(UTC))


def read_pool_readings_view(state: StateManagementInterface, params: dict[str, Any], *, now: datetime | None = None) -> dict[str, Any]:
    """Recorded allowance-pool readings, each marked current or expired."""
    return read_allowance_pool_readings(state, params, now=now or datetime.now(UTC))


def retract_pool_reading(state: StateManagementInterface, params: dict[str, Any]) -> dict[str, Any]:
    """Delete one recorded allowance-pool reading."""
    return retract_allowance_pool_reading(state, params)


def _receipt_runtime_constraint(selection_receipt: dict[str, Any], *, agent_runtime: str, model: str, effort: str) -> object:
    """Check the receipt's selected tuple against the spawn tuple; return its runtime constraint."""
    selected = selection_receipt.get("selected")
    if not isinstance(selected, dict) or set(selected) != {"runtime", "model", "effort"}:
        raise CatalogError("selection_receipt_invalid", "selection_receipt.selected must identify runtime, model, and effort.")
    if selected != {"runtime": agent_runtime, "model": model, "effort": effort}:
        raise CatalogError("selection_receipt_mismatch", "spawn tuple differs from selection_receipt.selected.")
    constraint = selection_receipt["runtime_constraint"]
    if constraint is not None and constraint != agent_runtime:
        raise CatalogError("selection_receipt_mismatch", "spawn runtime differs from selection_receipt.runtime_constraint.")
    return constraint


def verify_selection_receipt(
    state: StateManagementInterface,
    *,
    difficulty_score: object,
    selection_receipt: object,
    dispatch_kind: str,
    scope_tags: tuple[str, ...],
    agent_runtime: str,
    model: str,
    effort: str,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Replay the selector and prove the requested spawn is its cheapest answer."""
    if isinstance(difficulty_score, bool) or not isinstance(difficulty_score, (int, float)):
        raise CatalogError("difficulty_score_required", "spawn_session requires a numeric difficulty_score.")
    if not isinstance(selection_receipt, dict):
        raise CatalogError("selection_receipt_required", "spawn_session requires a selector-issued selection_receipt.")
    required_keys = {
        "required_score", "billing_objective", "score_margin", "cost_tolerance", "max_staleness_hours",
        "runtime_constraint", "selected",
    }
    if set(selection_receipt) != required_keys:
        raise CatalogError("selection_receipt_invalid", "selection_receipt has missing or unexpected fields.")
    if selection_receipt.get("required_score") != float(difficulty_score):
        raise CatalogError("selection_receipt_mismatch", "selection_receipt.required_score must equal difficulty_score.")
    constraint = _receipt_runtime_constraint(selection_receipt, agent_runtime=agent_runtime, model=model, effort=effort)
    if selection_receipt["billing_objective"] == "allowance_weighted" and constraint is None:
        raise CatalogError("selection_receipt_mismatch", "an allowance_weighted selection_receipt must carry its runtime_constraint.")
    actual = select_dispatch_tier(
        state,
        {
            "required_score": difficulty_score,
            "dispatch_kind": dispatch_kind,
            "scope_tags": list(scope_tags),
            "billing_objective": selection_receipt["billing_objective"],
            "score_margin": selection_receipt["score_margin"],
            "cost_tolerance": selection_receipt["cost_tolerance"],
            "max_staleness_hours": selection_receipt["max_staleness_hours"],
            "runtime": constraint,
        },
        now=now,
    )
    if actual["selection_receipt"] != selection_receipt:
        raise CatalogError("selection_receipt_mismatch", "selection_receipt is not the current cheapest-clearing answer.")
    return selection_receipt


def _scope_tags(raw: object) -> tuple[str, ...]:
    if raw is None or raw == "":
        return ()
    if not isinstance(raw, (list, tuple)) or any(not isinstance(item, str) or not item for item in raw):
        raise CatalogError("parameter_invalid", "scope_tags must be a list of non-empty strings.")
    return tuple(raw)


__all__ = [
    "read_model_capability_catalog",
    "read_pool_readings_view",
    "record_model_capability_cell",
    "record_pool_reading",
    "refresh_model_capability_catalog",
    "retract_pool_reading",
    "seed_model_capability_catalog",
    "select_dispatch_tier",
    "verify_selection_receipt",
]
