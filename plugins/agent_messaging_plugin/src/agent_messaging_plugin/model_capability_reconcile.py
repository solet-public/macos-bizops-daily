"""The crosscheck reconciler (iss_d136ae29): decide accepted | crosscheck_conflict
| pending_crosscheck for one (runtime, model, effort) cell from its freshest
readings, never trusting a single source.

Pure and offline -- no fetch, no state-layer call. `refresh_model_capability_catalog`
(model_capability_verbs.py) is the only caller: it fetches real readings via
`model_capability_fetch`, writes them as `model_capability_observation` rows,
calls `reconcile_cell` per roster cell, and writes the resulting
`model_capability_cell` row. Splitting the decision out like this is what
makes it testable against fixture readings with zero network access.

## Why two `artificial_analysis_*` sources satisfy "at least two DISTINCT
source_ids" here

The brief (workbench/2026-09-21_dispatch_model_capability_crosscheck_refresh_run.md)
warns against a *fabricated* second source dressed as independent evidence --
concretely, a caller's own operational say-so treated as if it corroborated
itself. `artificial_analysis_leaderboard_html` and
`artificial_analysis_model_detail_html` are not that: they are two separately
fetched, separately rendered pages, verified live 2026-09-21 to be able to
diverge (the leaderboard aggregates many models on one cadence; a per-model
detail page is a different route, plausibly a different render/cache path).
They are not evidence of a *different organization's* measurement -- no
vendor publishes Artificial Analysis's own Intelligence Index, so no such
second organization exists for this specific metric -- but they are two
real, independently-fetched artifacts whose agreement is genuine corroboration
against a transient fetch/parse/cache fault, which is what this reconciler
actually protects against. `anthropic_pricing_page` / `openai_model_docs`
(not implemented here) would add cost-plausibility corroboration from a truly
different publisher as a fast-follow; they cannot corroborate
`capability_score` itself since they don't publish it.

## Tolerance, and why

`capability_score`: absolute tolerance 2.0 index points. Both sources render
the same underlying Artificial Analysis benchmark result; a live spot-check
2026-09-21 found them byte-identical (claude-sonnet-5 max: 38 on both). A
2.0-point allowance covers legitimate cache-propagation lag between the
leaderboard's aggregate render and a per-model detail page's own render
without masking a real score change -- the seed table's own effort-tier
deltas for one model are themselves usually >=1 point (e.g. claude-sonnet-5:
low=24, medium=28 -- a 4-point real step), so 2.0 is well inside "still the
same measurement," not inside "a different effort tier's plausible score."

`cost_per_task_usd`: relative tolerance 5%. Same-benchmark cost figures carry
cents-level precision; a few percent covers rendering/rounding drift between
the two pages without hiding a real re-pricing event, which historically (the
2026-09-19 seed vs. this fast-follow's own live spot-check) moves a cell's
cost by tens of percent, not single digits.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

from .model_capability_fetch import Reading
from .schema import (
    CELL_ACCEPTANCE_ACCEPTED,
    CELL_ACCEPTANCE_CROSSCHECK_CONFLICT,
    CELL_ACCEPTANCE_PENDING_CROSSCHECK,
)

CAPABILITY_SCORE_TOLERANCE: Final[float] = 2.0
"""Absolute Intelligence Index points. See module docstring."""

COST_RELATIVE_TOLERANCE: Final[float] = 0.05
"""Fraction of the larger reading. See module docstring."""

_REQUIRED_METRIC: Final[str] = "intelligence_index"
_OPTIONAL_METRIC: Final[str] = "cost_per_task_usd"


@dataclass(frozen=True, slots=True)
class ObservationRef:
    """The persisted identity of one Reading, once written -- what the cell's
    `agreeing_observation_ids` actually points at."""

    observation_id: str
    reading: Reading


@dataclass(frozen=True, slots=True)
class ReconcileOutcome:
    cell_key: tuple[str, str, str]
    acceptance: str
    capability_score: float | None
    cost_per_task_usd: float | None
    measured_at: str | None
    agreeing_observation_ids: list[str]
    disagreement_note: str | None


def _freshest_ok_by_source(refs: list[ObservationRef], *, metric: str) -> dict[str, ObservationRef]:
    """The single freshest `fetch_status=ok` reading per source_id for one metric."""
    by_source: dict[str, ObservationRef] = {}
    for ref in refs:
        if ref.reading.metric != metric or ref.reading.fetch_status != "ok" or ref.reading.value_number is None:
            continue
        current = by_source.get(ref.reading.source_id)
        if current is None or ref.reading.fetched_at > current.reading.fetched_at:
            by_source[ref.reading.source_id] = ref
    return by_source


def _pending_outcome(cell_key: tuple[str, str, str], score_by_source: dict[str, ObservationRef]) -> ReconcileOutcome:
    have = sorted(score_by_source)
    return ReconcileOutcome(
        cell_key=cell_key, acceptance=CELL_ACCEPTANCE_PENDING_CROSSCHECK,
        capability_score=None, cost_per_task_usd=None, measured_at=None,
        agreeing_observation_ids=[],
        disagreement_note=(
            f"insufficient evidence: {len(score_by_source)} distinct source(s) reported "
            f"{_REQUIRED_METRIC} this run ({have}); at least 2 required."
        ),
    )


def _conflict_outcome(cell_key: tuple[str, str, str], *, metric: str, spread_text: str, values: dict[str, float | None]) -> ReconcileOutcome:
    detail = ", ".join(f"{sid}={v}" for sid, v in sorted(values.items()))
    return ReconcileOutcome(
        cell_key=cell_key, acceptance=CELL_ACCEPTANCE_CROSSCHECK_CONFLICT,
        capability_score=None, cost_per_task_usd=None, measured_at=None,
        agreeing_observation_ids=[],
        disagreement_note=f"{metric} disagreement exceeds tolerance ({spread_text}): {detail}.",
    )


@dataclass(frozen=True, slots=True)
class _ScoreAgreement:
    value: float
    agreeing_ids: list[str]
    measured_at: str


def _reconcile_score(cell_key: tuple[str, str, str], score_by_source: dict[str, ObservationRef]) -> ReconcileOutcome | _ScoreAgreement:
    """Precondition: caller has already checked `len(score_by_source) >= 2`."""
    values = {sid: ref.reading.value_number for sid, ref in score_by_source.items()}
    assert all(v is not None for v in values.values())  # noqa: S101 -- _freshest_ok_by_source already filtered None
    spread = max(values.values()) - min(values.values())  # type: ignore[operator]
    if spread > CAPABILITY_SCORE_TOLERANCE:
        return _conflict_outcome(cell_key, metric=_REQUIRED_METRIC, spread_text=f"{spread:.1f} > {CAPABILITY_SCORE_TOLERANCE}", values=values)
    return _ScoreAgreement(
        value=sum(values.values()) / len(values),  # type: ignore[misc]
        agreeing_ids=[ref.observation_id for ref in score_by_source.values()],
        measured_at=max(ref.reading.fetched_at for ref in score_by_source.values()),
    )


@dataclass(frozen=True, slots=True)
class _CostAgreement:
    value: float | None
    agreeing_ids: list[str]
    note: str | None


def _reconcile_cost(cell_key: tuple[str, str, str], cost_by_source: dict[str, ObservationRef]) -> ReconcileOutcome | _CostAgreement:
    if not cost_by_source:
        return _CostAgreement(value=None, agreeing_ids=[], note=None)
    if len(cost_by_source) == 1:
        (only_source, only_ref), = cost_by_source.items()
        note = f"{_OPTIONAL_METRIC} corroborated by only 1 source ({only_source}); accepted on {_REQUIRED_METRIC}'s 2-source agreement alone."
        return _CostAgreement(value=only_ref.reading.value_number, agreeing_ids=[], note=note)
    values = {sid: ref.reading.value_number for sid, ref in cost_by_source.items()}
    larger = max(abs(v) for v in values.values() if v is not None) or 1.0  # type: ignore[arg-type]
    spread = (max(values.values()) - min(values.values())) / larger  # type: ignore[operator]
    if spread > COST_RELATIVE_TOLERANCE:
        return _conflict_outcome(cell_key, metric=_OPTIONAL_METRIC, spread_text=f"{spread:.1%} > {COST_RELATIVE_TOLERANCE:.0%}", values=values)
    return _CostAgreement(
        value=sum(values.values()) / len(values),  # type: ignore[misc]
        agreeing_ids=[ref.observation_id for ref in cost_by_source.values()],
        note=None,
    )


def reconcile_cell(cell_key: tuple[str, str, str], observations: list[ObservationRef]) -> ReconcileOutcome:
    """`observations` is every reading gathered for this (runtime, model,
    effort) cell across all sources in one refresh run. Never mutates state;
    the caller writes the returned outcome. `capability_score` must clear a
    real 2-source agreement to accept at all; `cost_per_task_usd` is
    corroborated the same way when possible but never blocks acceptance on
    its own missing/single-source evidence -- see `_reconcile_cost`."""
    score_by_source = _freshest_ok_by_source(observations, metric=_REQUIRED_METRIC)
    cost_by_source = _freshest_ok_by_source(observations, metric=_OPTIONAL_METRIC)

    if len(score_by_source) < 2:
        return _pending_outcome(cell_key, score_by_source)

    score = _reconcile_score(cell_key, score_by_source)
    if isinstance(score, ReconcileOutcome):
        return score

    cost = _reconcile_cost(cell_key, cost_by_source)
    if isinstance(cost, ReconcileOutcome):
        return cost

    return ReconcileOutcome(
        cell_key=cell_key, acceptance=CELL_ACCEPTANCE_ACCEPTED,
        capability_score=score.value, cost_per_task_usd=cost.value, measured_at=score.measured_at,
        agreeing_observation_ids=score.agreeing_ids + cost.agreeing_ids, disagreement_note=cost.note,
    )


__all__ = [
    "CAPABILITY_SCORE_TOLERANCE",
    "COST_RELATIVE_TOLERANCE",
    "ObservationRef",
    "ReconcileOutcome",
    "reconcile_cell",
]
