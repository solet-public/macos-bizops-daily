"""Cost-aware (model, effort) tier selection — the pure decision core.

Design of record: register issue iss_48ea8171, unit unt_4a6526fd (the
design document is that unit's deliverable). This module owns no I/O: the catalog it
selects over is loaded by the store layer, and the verb layer wraps the
result. Keeping the decision pure is what lets the smoke prove, against the
operator's own hand-computed numbers, that the algorithm reproduces them.

The operator's heuristic — effort has sharply diminishing returns inside one
model, and a stronger model at its base effort frequently beats a weaker
model's top effort on capability AND cost — is what a human uses to
approximate the cheapest clearing cell WITHOUT a complete table. With a
complete, fresh table the exact answer is a filtered argmin over cost, and
that argmin already prefers Opus-5 low over Sonnet-5 max because it is
cheaper. What the heuristic still contributes here is (1) the explanation —
every answer carries the per-model effort ladder with marginal
points-per-cost-unit so the diminishing returns are visible, and every
dominated cell is named — and (2) the near-tie rule: when two feasible cells
cost within ``cost_tolerance`` of each other, the one at the LOWER effort
ordinal wins, because effort buys reasoning tokens (latency, variance) that
the score alone does not show.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Final, Literal

EFFORT_ORDER: Final[tuple[str, ...]] = ("non_reasoning", "none", "low", "medium", "high", "xhigh", "max")
BillingObjective = Literal["metered_usd", "relative"]
_DEFAULT_COST_TOLERANCE: Final[float] = 0.05


class TierSelectionError(Exception):
    """A deterministic refusal carrying a verb-layer code."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(message)


@dataclass(frozen=True, slots=True)
class CapabilityCell:
    """One accepted (runtime, model, effort) measurement, as the store serves it."""

    runtime: str
    model: str
    effort: str
    capability_score: float
    cost_per_task_usd: float | None
    relative_cost_multiplier: float | None
    measured_at: datetime
    acceptance: str

    @property
    def pair(self) -> tuple[str, str]:
        return (self.runtime, self.model)

    @property
    def effort_ordinal(self) -> int:
        return EFFORT_ORDER.index(self.effort)

    def cost(self, objective: BillingObjective) -> float | None:
        return self.cost_per_task_usd if objective == "metered_usd" else self.relative_cost_multiplier


@dataclass(frozen=True, slots=True)
class LadderStep:
    """One effort step inside a model, with its marginal efficiency."""

    from_effort: str
    to_effort: str
    score_gain: float
    cost_delta: float
    points_per_cost_unit: float | None


@dataclass(frozen=True, slots=True)
class TierSelection:
    selected: CapabilityCell
    objective: BillingObjective
    required_score: float
    effective_required_score: float
    frontier: tuple[CapabilityCell, ...]
    dominated: tuple[tuple[CapabilityCell, CapabilityCell], ...]
    ladder: tuple[LadderStep, ...]
    excluded: dict[str, int]


def _validate_effort(cell: CapabilityCell) -> None:
    if cell.effort not in EFFORT_ORDER:
        raise TierSelectionError("catalog_invalid", f"unknown effort {cell.effort!r} on {cell.model}.")


def _fresh(cell: CapabilityCell, *, now: datetime, max_age: timedelta) -> bool:
    return now - cell.measured_at <= max_age


def _feasible_cells(
    catalog: tuple[CapabilityCell, ...], *, objective: BillingObjective, threshold: float,
    capability_floor_pairs: frozenset[tuple[str, str]] | None, quota_status_by_pair: Mapping[tuple[str, str], str],
    now: datetime, max_age: timedelta,
) -> tuple[list[CapabilityCell], dict[str, int]]:
    excluded = {
        "not_accepted": 0, "stale": 0, "capability_floor_disallowed": 0,
        "quota_exhausted": 0, "quota_unknown": 0, "unpriced": 0, "below_threshold": 0,
    }
    feasible: list[CapabilityCell] = []
    for cell in catalog:
        _validate_effort(cell)
        if cell.acceptance != "accepted":
            excluded["not_accepted"] += 1
        elif not _fresh(cell, now=now, max_age=max_age):
            excluded["stale"] += 1
        elif capability_floor_pairs is not None and cell.pair not in capability_floor_pairs:
            excluded["capability_floor_disallowed"] += 1
        elif quota_status_by_pair.get(cell.pair) == "exhausted":
            excluded["quota_exhausted"] += 1
        elif quota_status_by_pair.get(cell.pair) != "available":
            excluded["quota_unknown"] += 1
        elif cell.cost(objective) is None:
            excluded["unpriced"] += 1
        elif cell.capability_score < threshold:
            excluded["below_threshold"] += 1
        else:
            feasible.append(cell)
    return feasible, excluded


def _cost_of(cell: CapabilityCell, objective: BillingObjective) -> float:
    cost = cell.cost(objective)
    if cost is None:
        raise TierSelectionError("catalog_invalid", f"{cell.model}@{cell.effort} lost its price mid-selection.")
    return cost


def _pick_cheapest(feasible: list[CapabilityCell], *, objective: BillingObjective, cost_tolerance: float) -> CapabilityCell:
    """Exact argmin over cost; near-ties resolve to the lower effort, then the higher score."""
    cheapest = min(feasible, key=lambda cell: _cost_of(cell, objective))
    floor = _cost_of(cheapest, objective)
    near = [cell for cell in feasible if _cost_of(cell, objective) <= floor * (1.0 + cost_tolerance)]
    return min(near, key=lambda cell: (cell.effort_ordinal, -cell.capability_score, _cost_of(cell, objective)))


def _frontier(feasible: list[CapabilityCell], objective: BillingObjective) -> tuple[CapabilityCell, ...]:
    """Cells no other feasible cell beats on both axes at once."""
    kept: list[CapabilityCell] = []
    for cell in sorted(feasible, key=lambda item: (_cost_of(item, objective), -item.capability_score)):
        if all(cell.capability_score > other.capability_score for other in kept):
            kept.append(cell)
    return tuple(kept)


def _dominations(
    catalog: tuple[CapabilityCell, ...], frontier: tuple[CapabilityCell, ...], objective: BillingObjective,
) -> tuple[tuple[CapabilityCell, CapabilityCell], ...]:
    """(weaker, stronger) pairs where a different model's cell wins on both axes — the operator's examples."""
    found: list[tuple[CapabilityCell, CapabilityCell]] = []
    for weaker in catalog:
        weaker_cost = weaker.cost(objective)
        if weaker_cost is None:
            continue
        winners = [
            stronger for stronger in frontier
            if stronger.pair != weaker.pair
            and stronger.capability_score >= weaker.capability_score
            and _cost_of(stronger, objective) < weaker_cost
        ]
        if winners:
            found.append((weaker, min(winners, key=lambda cell: _cost_of(cell, objective))))
    return tuple(found)


def _ladder(catalog: tuple[CapabilityCell, ...], selected: CapabilityCell, objective: BillingObjective) -> tuple[LadderStep, ...]:
    """The selected model's own effort ladder, step by step, with marginal efficiency."""
    rungs = sorted(
        (cell for cell in catalog if cell.pair == selected.pair and cell.cost(objective) is not None),
        key=lambda cell: cell.effort_ordinal,
    )
    steps: list[LadderStep] = []
    for lower, upper in zip(rungs, rungs[1:], strict=False):
        gain = upper.capability_score - lower.capability_score
        delta = _cost_of(upper, objective) - _cost_of(lower, objective)
        steps.append(LadderStep(lower.effort, upper.effort, gain, delta, gain / delta if delta > 0 else None))
    return tuple(steps)


def select_tier(
    catalog: tuple[CapabilityCell, ...], *, required_score: float, objective: BillingObjective,
    now: datetime, max_age: timedelta, capability_floor_pairs: frozenset[tuple[str, str]] | None = None,
    quota_status_by_pair: Mapping[tuple[str, str], str] | None = None,
    score_margin: float = 0.0, cost_tolerance: float = _DEFAULT_COST_TOLERANCE,
) -> TierSelection:
    """Cheapest fresh, quota-available cell clearing ``required_score + score_margin``.

    Refuses rather than guessing: an empty catalog, a catalog with nothing
    fresh, or a threshold nothing clears each raise a distinct code so the
    caller can tell a stale store from an impossible ask.
    """
    if not catalog:
        raise TierSelectionError("catalog_empty", "no capability cells to select from.")
    if score_margin < 0 or cost_tolerance < 0:
        raise TierSelectionError("parameter_invalid", "score_margin and cost_tolerance must be non-negative.")
    threshold = required_score + score_margin
    quotas = quota_status_by_pair or {cell.pair: "available" for cell in catalog}
    feasible, excluded = _feasible_cells(
        catalog, objective=objective, threshold=threshold, capability_floor_pairs=capability_floor_pairs,
        quota_status_by_pair=quotas, now=now, max_age=max_age,
    )
    if not feasible:
        usable = len(catalog) - excluded["not_accepted"] - excluded["stale"]
        selectable = (
            usable - excluded["capability_floor_disallowed"] - excluded["quota_exhausted"] - excluded["quota_unknown"]
        )
        code = "catalog_stale" if usable == 0 else "quota_state_unknown" if selectable == 0 and excluded["quota_unknown"] else "no_cell_clears_threshold"
        raise TierSelectionError(code, f"no feasible cell for required_score={threshold}: {excluded}.")
    selected = _pick_cheapest(feasible, objective=objective, cost_tolerance=cost_tolerance)
    frontier = _frontier(feasible, objective)
    return TierSelection(
        selected=selected,
        objective=objective,
        required_score=required_score,
        effective_required_score=threshold,
        frontier=frontier,
        dominated=_dominations(catalog, frontier, objective),
        ladder=_ladder(catalog, selected, objective),
        excluded=excluded,
    )


__all__ = [
    "EFFORT_ORDER",
    "BillingObjective",
    "CapabilityCell",
    "LadderStep",
    "TierSelection",
    "TierSelectionError",
    "select_tier",
]
