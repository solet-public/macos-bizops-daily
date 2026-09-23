"""State-layer primitives for the model capability catalog (iss_48ea8171).

Three tables, one contract: a cell is READABLE by dispatch only while its
`acceptance` is `accepted` and its `measured_at` is inside the staleness
window. This module owns the writes that move a cell between those states
and the one read that hands the selector a catalog; it never decides which
cell wins — that is `dispatch_tier_selection`, which is pure.

Seeding is deliberately weak: a seed writes every cell as
`pending_crosscheck`, never `accepted`, and never downgrades a cell a real
refresh run already accepted. The hand-reconciled 2026-09-19 table is a
starting point, not evidence, and the store says so on every row it seeds.
"""

from __future__ import annotations

import json
import math
from collections import Counter
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final

from ananta.llm.agent_messaging.role_binding import AGENT_ROLE_BINDING_NAMESPACE
from ananta.llm.agent_messaging.state_results import require_completed, require_records

from .dispatch_tier_selection import EFFORT_ORDER, BillingObjective, CapabilityCell
from .model_capability_fetch import Reading, roster_cells
from .model_capability_reconcile import (
    CAPABILITY_SCORE_TOLERANCE,
    COST_RELATIVE_TOLERANCE,
    ObservationRef,
    ReconcileOutcome,
    reconcile_cell,
)
from .schema import (
    CELL_ACCEPTANCE_ACCEPTED,
    CELL_ACCEPTANCE_CROSSCHECK_CONFLICT,
    CELL_ACCEPTANCE_PENDING_CROSSCHECK,
    TABLE_MODEL_CAPABILITY_CELL,
    TABLE_MODEL_CAPABILITY_OBSERVATION,
    TABLE_MODEL_CAPABILITY_REFRESH_RUN,
)
from .usage_economics_profiles import (
    FlatRateQuotaProfile,
    UsageEconomicsProfileValidationError,
    load_usage_economics_profile_catalog,
)

if TYPE_CHECKING:
    from ananta.interfaces.state_management_interface import StateManagementInterface

_PLUGIN_ROOT: Final[Path] = Path(__file__).resolve().parents[2]
SEED_PATH: Final[Path] = _PLUGIN_ROOT / "model_capability_seed.v1.json"
USAGE_ECONOMICS_PATH: Final[Path] = _PLUGIN_ROOT / "model_profiles" / "usage_economics.v1.json"
DEFAULT_STALENESS_WINDOW_HOURS: Final[int] = 72
"""The §6 recommendation, accepted by the coordinating seat 2026-09-20 (agm-_b5cb72b8)."""

MAX_CATALOG_ROWS: Final[int] = 100
"""The state layer's own `query_ordered` page cap. A full page is refused as
a possible truncation rather than served as the whole catalog."""

_CELL_KEY: Final[tuple[str, str, str]] = ("runtime", "model", "effort")
_SEED_SOURCE_ID: Final[str] = "seed_table"


class CatalogError(Exception):
    """A catalog operation was refused. Carries a code for the verb layer."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(message)


@dataclass(frozen=True, slots=True)
class SeedCell:
    provider: str
    runtime: str
    model: str
    effort: str
    capability_score: float
    cost_per_task_usd: float | None


@dataclass(frozen=True, slots=True)
class SeedTable:
    seed_version: str
    measured_at: str
    fetch_method: str
    cells: tuple[SeedCell, ...]


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _parse_stamp(raw: object, field: str) -> datetime:
    if not isinstance(raw, str) or not raw:
        raise CatalogError("catalog_invalid", f"{field} must be an ISO-8601 timestamp.")
    try:
        value = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as exc:
        raise CatalogError("catalog_invalid", f"{field} is not ISO-8601: {raw!r}.") from exc
    if value.tzinfo is None:
        raise CatalogError("catalog_invalid", f"{field} must be timezone-aware.")
    return value


def _seed_number(raw: object, field: str, *, optional: bool) -> float | None:
    if raw is None and optional:
        return None
    if not isinstance(raw, (int, float)) or isinstance(raw, bool) or raw < 0:
        raise CatalogError("seed_invalid", f"{field} must be a non-negative number.")
    return float(raw)


def _seed_text(raw: object, field: str) -> str:
    if not isinstance(raw, str) or not raw.strip():
        raise CatalogError("seed_invalid", f"{field} must be a non-empty string.")
    return raw.strip()


def _seed_cell(raw: object, index: int) -> SeedCell:
    if not isinstance(raw, dict):
        raise CatalogError("seed_invalid", f"cells[{index}] must be an object.")
    row: dict[str, Any] = raw
    effort = _seed_text(row.get("effort"), f"cells[{index}].effort")
    if effort not in EFFORT_ORDER:
        raise CatalogError("seed_invalid", f"cells[{index}].effort {effort!r} is not a known effort.")
    score = _seed_number(row.get("capability_score"), f"cells[{index}].capability_score", optional=False)
    if score is None:
        raise CatalogError("seed_invalid", f"cells[{index}].capability_score is required.")
    return SeedCell(
        provider=_seed_text(row.get("provider"), f"cells[{index}].provider"),
        runtime=_seed_text(row.get("runtime"), f"cells[{index}].runtime"),
        model=_seed_text(row.get("model"), f"cells[{index}].model"),
        effort=effort,
        capability_score=score,
        cost_per_task_usd=_seed_number(row.get("cost_per_task_usd"), f"cells[{index}].cost_per_task_usd", optional=True),
    )


def load_seed_table(path: Path = SEED_PATH) -> SeedTable:
    """Parse and validate the declarative seed; refuse rather than half-load."""
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise CatalogError("seed_unavailable", f"seed table is unreadable: {path}: {exc}") from exc
    if not isinstance(raw, dict) or raw.get("schema_version") != 1:
        raise CatalogError("seed_invalid", "seed table must be an object with schema_version=1.")
    root: dict[str, Any] = raw
    provenance = root.get("provenance")
    if not isinstance(provenance, dict):
        raise CatalogError("seed_invalid", "seed table requires a provenance object.")
    cells_raw = root.get("cells")
    if not isinstance(cells_raw, list) or not cells_raw:
        raise CatalogError("seed_invalid", "seed table requires a non-empty cells list.")
    cells = tuple(_seed_cell(row, index) for index, row in enumerate(cells_raw))
    keys = {(cell.runtime, cell.model, cell.effort) for cell in cells}
    if len(keys) != len(cells):
        raise CatalogError("seed_invalid", "seed table repeats a (runtime, model, effort) cell.")
    measured_at = _seed_text(root.get("measured_at"), "measured_at")
    _parse_stamp(measured_at, "measured_at")
    return SeedTable(
        seed_version=_seed_text(root.get("seed_version"), "seed_version"),
        measured_at=measured_at,
        fetch_method=_seed_text(provenance.get("fetch_method"), "provenance.fetch_method"),
        cells=cells,
    )


def open_refresh_run(state: StateManagementInterface, *, trigger: str) -> str:
    record: dict[str, Any] = {
        "started_at": _now_iso(),
        "finished_at": None,
        "trigger": trigger,
        "status": "running",
        "sources_ok": [],
        "sources_failed": [],
        "cells_accepted": 0,
        "cells_conflicted": 0,
        "cells_unchanged": 0,
        "note": None,
    }
    data = require_completed(
        state.write_state(AGENT_ROLE_BINDING_NAMESPACE, {"table": TABLE_MODEL_CAPABILITY_REFRESH_RUN, "record": record}),
        "open model capability refresh run",
    )
    result = data.get("result")
    run_id = result.get("generated_id") if isinstance(result, dict) else None
    if not isinstance(run_id, str) or not run_id:
        raise CatalogError("catalog_invalid", "refresh run insert returned no generated_id.")
    return run_id


def close_refresh_run(
    state: StateManagementInterface, run_id: str, *,
    counts: dict[str, int], sources_ok: list[str], note: str | None, sources_failed: list[str] | None = None,
) -> None:
    updates: dict[str, Any] = {
        "finished_at": _now_iso(),
        "status": "completed",
        "sources_ok": sources_ok,
        "sources_failed": sources_failed or [],
        "note": note,
        **counts,
    }
    require_completed(
        state.update_state(
            AGENT_ROLE_BINDING_NAMESPACE,
            {"table": TABLE_MODEL_CAPABILITY_REFRESH_RUN, "filters": {"id": run_id}},
            updates,
        ),
        "close model capability refresh run",
    )


def read_cells(state: StateManagementInterface, *, runtime: str | None = None, model: str | None = None) -> list[dict[str, Any]]:
    """Catalog rows matching the filters, unfiltered by acceptance, with current relative costs.

    The runtime and model filters run in the database. Each accepted row's
    relative_cost_multiplier is derived at read time against the catalog-wide
    price floor, which is one bounded query, so a cheaper cell recorded later
    rebases every other cell without rewriting stored rows.
    """
    filters: dict[str, Any] = {}
    if runtime:
        filters["runtime"] = runtime
    if model:
        filters["model"] = model
    rows = require_records(
        state.query_ordered(
            AGENT_ROLE_BINDING_NAMESPACE,
            {
                "table": TABLE_MODEL_CAPABILITY_CELL,
                "filters": filters,
                "order_by": [["runtime", "asc"], ["model", "asc"], ["effort", "asc"], ["id", "asc"]],
                "limit": MAX_CATALOG_ROWS,
            },
        ),
    )
    if len(rows) >= MAX_CATALOG_ROWS:
        raise CatalogError("catalog_too_large", f"catalog read filled a {MAX_CATALOG_ROWS}-row page; refusing a possibly truncated table.")
    result = [dict(row) for row in rows]
    _derive_current_relative_costs(result, _accepted_cost_floor(state))
    return result


def _accepted_cost_floor(state: StateManagementInterface) -> float | None:
    """The cheapest positive cost among accepted cells, read as one row."""
    rows = require_records(
        state.query_ordered(
            AGENT_ROLE_BINDING_NAMESPACE,
            {
                "table": TABLE_MODEL_CAPABILITY_CELL,
                "filters": {"acceptance": CELL_ACCEPTANCE_ACCEPTED, "cost_per_task_usd": {"op": "gt", "value": 0}},
                "order_by": [["cost_per_task_usd", "asc"], ["id", "asc"]],
                "limit": 1,
            },
        ),
    )
    cost = rows[0].get("cost_per_task_usd") if rows else None
    if not isinstance(cost, (int, float)) or isinstance(cost, bool) or not math.isfinite(cost):
        return None
    return float(cost)


def _relative_cost(cost: object, floor: float | None) -> float | None:
    if not isinstance(cost, (int, float)) or not math.isfinite(cost):
        return None
    if cost == 0:
        return 0.0
    return float(cost) / floor if floor is not None else None


def _derive_current_relative_costs(rows: list[dict[str, Any]], floor: float | None) -> None:
    for row in rows:
        if row.get("acceptance") == CELL_ACCEPTANCE_ACCEPTED:
            row["relative_cost_multiplier"] = _relative_cost(row.get("cost_per_task_usd"), floor)


def _existing_cell(state: StateManagementInterface, cell: SeedCell) -> dict[str, Any] | None:
    rows = require_records(
        state.query_ordered(
            AGENT_ROLE_BINDING_NAMESPACE,
            {
                "table": TABLE_MODEL_CAPABILITY_CELL,
                "filters": {"runtime": cell.runtime, "model": cell.model, "effort": cell.effort},
                "order_by": [["updated_at", "desc"], ["id", "desc"]],
                "limit": 1,
            },
        ),
    )
    return dict(rows[0]) if rows else None


def _record_seed_observations(state: StateManagementInterface, run_id: str, seed: SeedTable, cell: SeedCell) -> None:
    metrics: list[tuple[str, float | None]] = [("intelligence_index", cell.capability_score)]
    if cell.cost_per_task_usd is not None:
        metrics.append(("cost_per_task_usd", cell.cost_per_task_usd))
    for metric, value in metrics:
        record: dict[str, Any] = {
            "refresh_run_id": run_id,
            "source_id": _SEED_SOURCE_ID,
            "fetch_method": seed.fetch_method,
            "fetched_at": seed.measured_at,
            "provider": cell.provider,
            "runtime": cell.runtime,
            "model": cell.model,
            "effort": cell.effort,
            "metric": metric,
            "value_number": value,
            "value_text": None,
            "raw_excerpt": f"{seed.seed_version}: {cell.model}@{cell.effort} {metric}={value}",
            "fetch_status": "ok",
        }
        require_completed(
            state.write_state(AGENT_ROLE_BINDING_NAMESPACE, {"table": TABLE_MODEL_CAPABILITY_OBSERVATION, "record": record}),
            "record seed observation",
        )


def _relative_multipliers(cells: tuple[SeedCell, ...]) -> dict[tuple[str, str, str], float | None]:
    """cost over the cheapest priced cell in the SEED — a seed-relative figure, re-derived by every real run."""
    priced = [cell.cost_per_task_usd for cell in cells if cell.cost_per_task_usd]
    floor = min(priced) if priced else None
    return {
        (cell.runtime, cell.model, cell.effort): (
            None if cell.cost_per_task_usd is None or floor is None else cell.cost_per_task_usd / floor
        )
        for cell in cells
    }


def _seed_cell_record(seed: SeedTable, cell: SeedCell, run_id: str, *, staleness_window_hours: int, relative: float | None) -> dict[str, Any]:
    return {
        "provider": cell.provider,
        "runtime": cell.runtime,
        "model": cell.model,
        "effort": cell.effort,
        "capability_score": cell.capability_score,
        "cost_per_task_usd": cell.cost_per_task_usd,
        "relative_cost_multiplier": relative,
        "currency": "USD",
        "effort_supported": True,
        "measured_at": seed.measured_at,
        "accepted_at": None,
        "acceptance": CELL_ACCEPTANCE_PENDING_CROSSCHECK,
        "agreeing_observation_ids": [],
        "disagreement_note": "seeded from the hand-reconciled table; awaiting a cross-checked refresh run",
        "staleness_window_hours": staleness_window_hours,
        "last_refresh_run_id": run_id,
    }


def seed_catalog(state: StateManagementInterface, *, seed: SeedTable, staleness_window_hours: int = DEFAULT_STALENESS_WINDOW_HOURS) -> dict[str, Any]:
    """Write the seed as `pending_crosscheck` cells; never touch an accepted cell.

    Idempotent: re-seeding rewrites pending cells with the same values and
    leaves accepted ones alone, so a seed can run at every start-up without
    ever undoing what a real refresh established.
    """
    if staleness_window_hours <= 0:
        raise CatalogError("parameter_invalid", "staleness_window_hours must be positive.")
    run_id = open_refresh_run(state, trigger="seed")
    relative = _relative_multipliers(seed.cells)
    seeded = 0
    preserved: list[str] = []
    for cell in seed.cells:
        existing = _existing_cell(state, cell)
        if existing is not None and existing.get("acceptance") == CELL_ACCEPTANCE_ACCEPTED:
            preserved.append(f"{cell.model}@{cell.effort}")
            continue
        _record_seed_observations(state, run_id, seed, cell)
        require_completed(
            state.upsert_state(
                AGENT_ROLE_BINDING_NAMESPACE,
                {
                    "table": TABLE_MODEL_CAPABILITY_CELL,
                    "record": _seed_cell_record(
                        seed, cell, run_id, staleness_window_hours=staleness_window_hours,
                        relative=relative[(cell.runtime, cell.model, cell.effort)],
                    ),
                    "conflict_columns": list(_CELL_KEY),
                },
            ),
            "seed model capability cell",
        )
        seeded += 1
    counts = {"cells_accepted": 0, "cells_conflicted": 0, "cells_unchanged": len(preserved)}
    close_refresh_run(state, run_id, counts=counts, sources_ok=[_SEED_SOURCE_ID], note=f"seed {seed.seed_version}: {seeded} pending, {len(preserved)} accepted cells preserved")
    return {"run_id": run_id, "seed_version": seed.seed_version, "cells_seeded": seeded, "accepted_cells_preserved": preserved}


def _write_reading_observation(state: StateManagementInterface, run_id: str, reading: Reading) -> str:
    """One `model_capability_observation` row for one real fetched reading.
    Returns its generated id, so the reconciler's `agreeing_observation_ids`
    can cite it verbatim."""
    record: dict[str, Any] = {
        "refresh_run_id": run_id,
        "source_id": reading.source_id,
        "fetch_method": f"GET (parsed by model_capability_fetch, {reading.source_id})",
        "fetched_at": reading.fetched_at,
        "provider": reading.provider,
        "runtime": reading.runtime,
        "model": reading.model,
        "effort": reading.effort,
        "metric": reading.metric,
        "value_number": reading.value_number,
        "value_text": reading.value_text,
        "raw_excerpt": reading.raw_excerpt,
        "fetch_status": reading.fetch_status,
    }
    data = require_completed(
        state.write_state(AGENT_ROLE_BINDING_NAMESPACE, {"table": TABLE_MODEL_CAPABILITY_OBSERVATION, "record": record}),
        "record model capability observation",
    )
    result = data.get("result")
    observation_id = result.get("generated_id") if isinstance(result, dict) else None
    if not isinstance(observation_id, str) or not observation_id:
        raise CatalogError("catalog_invalid", "observation insert returned no generated_id.")
    return observation_id


def _reconciled_cell_record(outcome: ReconcileOutcome, run_id: str, *, staleness_window_hours: int, relative: float | None) -> dict[str, Any]:
    runtime, model, effort = outcome.cell_key
    provider = "anthropic" if runtime == "claude_code" else "openai"
    return {
        "provider": provider,
        "runtime": runtime,
        "model": model,
        "effort": effort,
        "capability_score": outcome.capability_score,
        "cost_per_task_usd": outcome.cost_per_task_usd,
        "relative_cost_multiplier": relative,
        "currency": "USD",
        "effort_supported": True,
        "measured_at": outcome.measured_at,
        "accepted_at": _now_iso() if outcome.acceptance == CELL_ACCEPTANCE_ACCEPTED else None,
        "acceptance": outcome.acceptance,
        "agreeing_observation_ids": outcome.agreeing_observation_ids,
        "disagreement_note": outcome.disagreement_note,
        "staleness_window_hours": staleness_window_hours,
        "last_refresh_run_id": run_id,
    }


def _existing_cell_by_key(state: StateManagementInterface, key: tuple[str, str, str]) -> dict[str, Any] | None:
    """`_existing_cell` keyed by (runtime, model, effort) rather than a `SeedCell`."""
    runtime, model, effort = key
    rows = require_records(
        state.query_ordered(
            AGENT_ROLE_BINDING_NAMESPACE,
            {
                "table": TABLE_MODEL_CAPABILITY_CELL,
                "filters": {"runtime": runtime, "model": model, "effort": effort},
                "order_by": [["updated_at", "desc"], ["id", "desc"]],
                "limit": 1,
            },
        ),
    )
    return dict(rows[0]) if rows else None


def _write_all_observations(state: StateManagementInterface, run_id: str, readings: list[Reading]) -> dict[tuple[str, str, str], list[ObservationRef]]:
    written: dict[tuple[str, str, str], list[ObservationRef]] = {}
    for reading in readings:
        observation_id = _write_reading_observation(state, run_id, reading)
        written.setdefault((reading.runtime, reading.model, reading.effort), []).append(
            ObservationRef(observation_id=observation_id, reading=reading),
        )
    return written


def _cost_floor(outcomes: dict[tuple[str, str, str], ReconcileOutcome]) -> float | None:
    priced = [o.cost_per_task_usd for o in outcomes.values() if o.acceptance == CELL_ACCEPTANCE_ACCEPTED and o.cost_per_task_usd]
    return min(priced) if priced else None


def _manual_source_for_cell(state: StateManagementInterface, existing: dict[str, Any]) -> bool:
    """Identify a direct recording by its persisted observation, not its run trigger."""
    ids = existing.get("agreeing_observation_ids")
    if not isinstance(ids, list) or not ids or not isinstance(ids[0], str):
        return False
    rows = require_records(
        state.query_ordered(
            AGENT_ROLE_BINDING_NAMESPACE,
            {
                "table": TABLE_MODEL_CAPABILITY_OBSERVATION,
                "filters": {"id": ids[0]},
                "order_by": [["created_at", "desc"], ["id", "desc"]],
                "limit": 1,
            },
        ),
    )
    return bool(rows and rows[0].get("source_id") == _MANUAL_SOURCE_ID)


def _manual_conflict(
    existing: dict[str, Any], outcome: ReconcileOutcome,
) -> ReconcileOutcome | None:
    """A direct reading and two agreeing live pages still require agreement."""
    prior_score = existing.get("capability_score")
    current_score = outcome.capability_score
    if not isinstance(prior_score, (int, float)) or current_score is None:
        return None
    score_delta = abs(float(prior_score) - current_score)
    prior_cost = existing.get("cost_per_task_usd")
    current_cost = outcome.cost_per_task_usd
    cost_delta = 0.0
    if isinstance(prior_cost, (int, float)) and current_cost is not None:
        denominator = max(abs(float(prior_cost)), abs(current_cost)) or 1.0
        cost_delta = abs(float(prior_cost) - current_cost) / denominator
    if score_delta <= CAPABILITY_SCORE_TOLERANCE and cost_delta <= COST_RELATIVE_TOLERANCE:
        return None
    detail = f"manual versus live disagreement: score delta {score_delta:.3f}, cost delta {cost_delta:.1%}"
    return ReconcileOutcome(
        cell_key=outcome.cell_key,
        acceptance=CELL_ACCEPTANCE_CROSSCHECK_CONFLICT,
        capability_score=None,
        cost_per_task_usd=None,
        measured_at=None,
        agreeing_observation_ids=[],
        disagreement_note=detail,
    )


def _apply_manual_conflict_if_needed(
    state: StateManagementInterface, key: tuple[str, str, str], outcome: ReconcileOutcome,
) -> ReconcileOutcome:
    if outcome.acceptance != CELL_ACCEPTANCE_ACCEPTED:
        return outcome
    existing = _existing_cell_by_key(state, key)
    if existing is None or not _manual_source_for_cell(state, existing):
        return outcome
    return _manual_conflict(existing, outcome) or outcome


def _classify_and_maybe_write(
    state: StateManagementInterface, key: tuple[str, str, str], outcome: ReconcileOutcome, run_id: str, *,
    staleness_window_hours: int, floor: float | None,
) -> str:
    """Returns 'accepted' | 'conflicted' | 'unchanged'. A `pending_crosscheck`
    outcome for a cell a PAST run already accepted skips the write entirely
    -- see `reconcile_and_write`'s own docstring for the never-downgrade
    asymmetry this implements."""
    outcome = _apply_manual_conflict_if_needed(state, key, outcome)
    if outcome.acceptance == CELL_ACCEPTANCE_PENDING_CROSSCHECK:
        existing = _existing_cell_by_key(state, key)
        if existing is not None and existing.get("acceptance") == CELL_ACCEPTANCE_ACCEPTED:
            return "unchanged"
        classification = "unchanged"
    elif outcome.acceptance == CELL_ACCEPTANCE_ACCEPTED:
        classification = "accepted"
    elif outcome.acceptance == CELL_ACCEPTANCE_CROSSCHECK_CONFLICT:
        classification = "conflicted"
    else:
        raise CatalogError("catalog_invalid", f"reconcile_cell produced an unexpected acceptance {outcome.acceptance!r}.")
    relative = None if outcome.cost_per_task_usd is None or floor is None else outcome.cost_per_task_usd / floor
    require_completed(
        state.upsert_state(
            AGENT_ROLE_BINDING_NAMESPACE,
            {
                "table": TABLE_MODEL_CAPABILITY_CELL,
                "record": _reconciled_cell_record(outcome, run_id, staleness_window_hours=staleness_window_hours, relative=relative),
                "conflict_columns": list(_CELL_KEY),
            },
        ),
        "reconcile model capability cell",
    )
    return classification


def reconcile_and_write(
    state: StateManagementInterface, run_id: str, readings: list[Reading], *,
    staleness_window_hours: int = DEFAULT_STALENESS_WINDOW_HOURS,
) -> dict[str, int]:
    """Write every reading as an observation, reconcile each roster cell from
    this run's readings, and upsert the cell table.

    Mirrors `seed_catalog`'s own "never silently undo an already-accepted
    cell on weaker evidence" rule: a genuine `crosscheck_conflict` DOES
    overwrite a prior accepted cell (real disagreement is real signal to
    surface), but a merely `pending_crosscheck` outcome -- this run simply
    didn't gather 2 sources for that cell, e.g. a fetch failure -- never
    downgrades a cell a past run already accepted.
    """
    written = _write_all_observations(state, run_id, readings)
    outcomes = {key: reconcile_cell(key, written.get(key, [])) for key in roster_cells()}
    floor = _cost_floor(outcomes)
    counts = Counter(
        _classify_and_maybe_write(state, key, outcome, run_id, staleness_window_hours=staleness_window_hours, floor=floor)
        for key, outcome in outcomes.items()
    )
    return {"cells_accepted": counts["accepted"], "cells_conflicted": counts["conflicted"], "cells_unchanged": counts["unchanged"]}


_MANUAL_SOURCE_ID: Final[str] = "manual_record"
_RUNTIME_PROVIDERS: Final[dict[str, str]] = {"claude_code": "anthropic", "codex": "openai"}
_MAX_EXCERPT: Final[int] = 512


@dataclass(frozen=True, slots=True)
class ManualCell:
    """One caller-supplied catalog reading (operator ruling rul_9e7a67ba:
    anyone with better information can update the table directly)."""

    runtime: str
    model: str
    effort: str
    capability_score: float
    cost_per_task_usd: float | None
    source_ref: str
    note: str | None


def _validate_manual_cell(cell: ManualCell) -> None:
    if cell.runtime not in _RUNTIME_PROVIDERS:
        raise CatalogError("parameter_invalid", f"runtime must be one of {sorted(_RUNTIME_PROVIDERS)}; got {cell.runtime!r}.")
    if not cell.model.strip():
        raise CatalogError("parameter_invalid", "model must be a non-blank canonical model id.")
    if cell.effort not in EFFORT_ORDER:
        raise CatalogError("parameter_invalid", f"effort must be one of {list(EFFORT_ORDER)}; got {cell.effort!r}.")
    if not math.isfinite(cell.capability_score) or not 0 <= cell.capability_score <= 100:
        raise CatalogError("parameter_invalid", f"capability_score is on the 0-100 Intelligence Index scale; got {cell.capability_score}.")
    if cell.cost_per_task_usd is not None and (not math.isfinite(cell.cost_per_task_usd) or cell.cost_per_task_usd < 0):
        raise CatalogError("parameter_invalid", f"cost_per_task_usd must be non-negative; got {cell.cost_per_task_usd}.")
    if not cell.source_ref.strip():
        raise CatalogError("parameter_invalid", "source_ref is required: name where the reading came from (a URL or a document).")


def _record_manual_observations(state: StateManagementInterface, run_id: str, cell: ManualCell, *, recorded_at: str) -> list[str]:
    metrics: list[tuple[str, float]] = [("intelligence_index", cell.capability_score)]
    if cell.cost_per_task_usd is not None:
        metrics.append(("cost_per_task_usd", cell.cost_per_task_usd))
    excerpt = (f"{cell.source_ref} | {cell.note}" if cell.note else cell.source_ref)[:_MAX_EXCERPT]
    observation_ids: list[str] = []
    for metric, value in metrics:
        record: dict[str, Any] = {
            "refresh_run_id": run_id,
            "source_id": _MANUAL_SOURCE_ID,
            "fetch_method": f"recorded directly via record_model_capability_cell; source: {cell.source_ref}"[:_MAX_EXCERPT],
            "fetched_at": recorded_at,
            "provider": _RUNTIME_PROVIDERS[cell.runtime],
            "runtime": cell.runtime,
            "model": cell.model,
            "effort": cell.effort,
            "metric": metric,
            "value_number": value,
            "value_text": None,
            "raw_excerpt": excerpt,
            "fetch_status": "ok",
        }
        data = require_completed(
            state.write_state(AGENT_ROLE_BINDING_NAMESPACE, {"table": TABLE_MODEL_CAPABILITY_OBSERVATION, "record": record}),
            "record manual model capability observation",
        )
        result = data.get("result")
        observation_id = result.get("generated_id") if isinstance(result, dict) else None
        if not isinstance(observation_id, str) or not observation_id:
            raise CatalogError("catalog_invalid", "observation insert returned no generated_id.")
        observation_ids.append(observation_id)
    return observation_ids


def record_manual_cell(
    state: StateManagementInterface, cell: ManualCell, *,
    staleness_window_hours: int = DEFAULT_STALENESS_WINDOW_HOURS,
) -> dict[str, Any]:
    """Write one caller-supplied reading as an ACCEPTED cell, with its source.

    Operator ruling rul_9e7a67ba (2026-09-22): models change daily, and anyone
    who gets better information must be able to update the catalog directly.
    The reading is kept as evidence exactly like a fetched one: its own
    refresh-run row (trigger=manual) and one observation per metric whose
    fetch_method names the caller's source. It overwrites any prior value for
    the same (runtime, model, effort), whatever that cell's acceptance was.
    A later refresh can still move a roster cell to crosscheck_conflict if the
    live sources disagree with it, so a bad manual reading surfaces loudly.
    """
    _validate_manual_cell(cell)
    if staleness_window_hours <= 0:
        raise CatalogError("parameter_invalid", "staleness_window_hours must be positive.")
    key = (cell.runtime, cell.model, cell.effort)
    previous = _existing_cell_by_key(state, key)
    run_id = open_refresh_run(state, trigger="manual")
    recorded_at = _now_iso()
    observation_ids = _record_manual_observations(state, run_id, cell, recorded_at=recorded_at)
    record: dict[str, Any] = {
        "provider": _RUNTIME_PROVIDERS[cell.runtime],
        "runtime": cell.runtime,
        "model": cell.model,
        "effort": cell.effort,
        "capability_score": cell.capability_score,
        "cost_per_task_usd": cell.cost_per_task_usd,
        "relative_cost_multiplier": None,  # derived at read time by read_cells against the live price floor
        "currency": "USD",
        "effort_supported": True,
        "measured_at": recorded_at,
        "accepted_at": recorded_at,
        "acceptance": CELL_ACCEPTANCE_ACCEPTED,
        "agreeing_observation_ids": observation_ids,
        "disagreement_note": None,
        "staleness_window_hours": staleness_window_hours,
        "last_refresh_run_id": run_id,
    }
    require_completed(
        state.upsert_state(
            AGENT_ROLE_BINDING_NAMESPACE,
            {"table": TABLE_MODEL_CAPABILITY_CELL, "record": record, "conflict_columns": list(_CELL_KEY)},
        ),
        "record manual model capability cell",
    )
    close_refresh_run(
        state, run_id,
        counts={"cells_accepted": 1, "cells_conflicted": 0, "cells_unchanged": 0},
        sources_ok=[_MANUAL_SOURCE_ID],
        note=f"record_model_capability_cell {cell.model}@{cell.effort}; source: {cell.source_ref}"[:_MAX_EXCERPT],
    )
    return {
        "run_id": run_id,
        "runtime": cell.runtime,
        "model": cell.model,
        "effort": cell.effort,
        "capability_score": cell.capability_score,
        "cost_per_task_usd": cell.cost_per_task_usd,
        "acceptance": CELL_ACCEPTANCE_ACCEPTED,
        "observation_ids": observation_ids,
        "previous": None if previous is None else {
            "capability_score": previous.get("capability_score"),
            "cost_per_task_usd": previous.get("cost_per_task_usd"),
            "acceptance": previous.get("acceptance"),
            "measured_at": previous.get("measured_at"),
        },
    }


def _optional_float(raw: object) -> float | None:
    if raw is None:
        return None
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        raise CatalogError("catalog_invalid", f"numeric catalog column holds {raw!r}.")
    return float(raw)


def selection_catalog(rows: list[dict[str, Any]]) -> tuple[CapabilityCell, ...]:
    """Every scored cell as the pure selector wants it; unscored rows are dropped here, counted by the verb."""
    cells: list[CapabilityCell] = []
    for row in rows:
        score = _optional_float(row.get("capability_score"))
        if score is None:
            continue
        measured_raw = row.get("measured_at")
        measured_at = _parse_stamp(measured_raw, "measured_at") if measured_raw else datetime.min.replace(tzinfo=UTC)
        cells.append(
            CapabilityCell(
                runtime=str(row.get("runtime", "")),
                model=str(row.get("model", "")),
                effort=str(row.get("effort", "")),
                capability_score=score,
                cost_per_task_usd=_optional_float(row.get("cost_per_task_usd")),
                relative_cost_multiplier=_optional_float(row.get("relative_cost_multiplier")),
                measured_at=measured_at,
                acceptance=str(row.get("acceptance", "")),
            ),
        )
    return tuple(cells)


def catalog_staleness_window(cells: tuple[dict[str, Any], ...] | list[dict[str, Any]]) -> timedelta:
    """The tightest window any served cell carries; the default when the catalog is empty."""
    hours = [int(row["staleness_window_hours"]) for row in cells if isinstance(row.get("staleness_window_hours"), int)]
    return timedelta(hours=min(hours) if hours else DEFAULT_STALENESS_WINDOW_HOURS)


def default_billing_objective(runtime: str | None, *, now: datetime, path: Path = USAGE_ECONOMICS_PATH) -> BillingObjective:
    """`relative` when a current flat-rate plan covers the runtime, else `metered_usd`.

    Derived from the declared economics profiles rather than assumed: a solet
    on metered billing gets dollars by default, and only a declared
    subscription turns that into a quota proxy. With no runtime named, any
    current flat-rate plan selects `relative`.
    """
    try:
        catalog = load_usage_economics_profile_catalog(path, as_of=now)
    except UsageEconomicsProfileValidationError as exc:
        raise CatalogError("usage_economics_invalid", f"usage economics profiles unreadable: {exc}") from exc
    for profile in catalog.profiles:
        if not isinstance(profile, FlatRateQuotaProfile) or profile.refresh_status != "current":
            continue
        if runtime is None or runtime in profile.included_runtimes:
            return "relative"
    return "metered_usd"


__all__ = [
    "DEFAULT_STALENESS_WINDOW_HOURS",
    "MAX_CATALOG_ROWS",
    "SEED_PATH",
    "CatalogError",
    "ManualCell",
    "SeedCell",
    "SeedTable",
    "catalog_staleness_window",
    "close_refresh_run",
    "default_billing_objective",
    "load_seed_table",
    "open_refresh_run",
    "read_cells",
    "reconcile_and_write",
    "record_manual_cell",
    "seed_catalog",
    "selection_catalog",
]
