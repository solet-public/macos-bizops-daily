"""Injected usage-economics strategies for context-rotation actions.

The verdict engine receives projected actions.  It does not discover model,
price, plan, quota, or reset facts.  Metered and subscription objectives share
an input shape but deliberately do not normalize into one fake token price.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass, replace

from .usage_economics_profiles import (
    AllowancePool,
    FlatRateQuotaProfile,
    MeteredApiProfile,
    UsageEconomicsProfile,
    UsageEconomicsProfileCatalog,
    UsageEconomicsProfileValidationError,
    load_usage_economics_profile_catalog,
)


@dataclass(frozen=True)
class UsageVector:
    """Projected observable usage for one candidate action and work horizon."""

    input_tokens: int | None
    cached_input_tokens: int | None
    cache_write_input_tokens: int | None
    output_tokens: int | None
    reasoning_output_tokens: int | None
    tool_calls: int | None


@dataclass(frozen=True)
class ProjectedAction:
    name: str
    total_usage: UsageVector
    post_action_context_tokens: int | None
    latency_seconds: float | None
    quality_score: float | None
    accepted_work_units: float | None
    quota_by_pool: Mapping[str, float | None]


@dataclass(frozen=True)
class RotationDecisionInput:
    provider: str
    runtime: str
    model: str
    effort: str
    current_context_tokens: int
    capacity_tokens: int
    expected_calls_after: int
    quality_floor: float | None
    actions: tuple[ProjectedAction, ...]


@dataclass(frozen=True)
class RotationEconomicsDecision:
    resolved: bool
    status: str
    chosen_action: str | None
    profile_id: str
    profile_version: str
    objective: str
    explanation: str
    explanation_version: str = "usage-economics-explanation-v1"
    projected_constraints: tuple[str, ...] = ()
    applicable_pool_ids: tuple[str, ...] = ()
    binding_pool_id: str | None = None
    crossover_profile_id: str | None = None
    expected_cost: float | None = None


def _projected_constraints(inputs: RotationDecisionInput) -> tuple[str, ...]:
    constraints = [
        f"capacity_tokens={inputs.capacity_tokens}",
        f"expected_calls_after={inputs.expected_calls_after}",
        f"quality_floor={inputs.quality_floor}",
    ]
    constraints.extend(
        f"{action.name}:post_context={action.post_action_context_tokens},"
        f"latency_seconds={action.latency_seconds},quality_score={action.quality_score},"
        f"accepted_work_units={action.accepted_work_units}"
        for action in inputs.actions
    )
    return tuple(constraints)


def _pool_ids(pools: tuple[AllowancePool, ...]) -> tuple[str, ...]:
    return tuple(pool.pool_id for pool in pools)


def _action_tie_priority(action: ProjectedAction) -> int:
    return 0 if action.name == "keep" else 1


def _validate_inputs(inputs: RotationDecisionInput) -> str | None:
    if inputs.capacity_tokens <= 0:
        return f"capacity_tokens must be positive, got {inputs.capacity_tokens}"
    if inputs.current_context_tokens < 0:
        return f"current_context_tokens must be non-negative, got {inputs.current_context_tokens}"
    if inputs.expected_calls_after < 0:
        return f"expected_calls_after must be non-negative, got {inputs.expected_calls_after}"
    if not inputs.actions:
        return "at least one projected action is required"
    return None


def _action_constraint_values(
    action: ProjectedAction,
) -> tuple[float, int, float, float] | None:
    quality_score = action.quality_score
    post_context = action.post_action_context_tokens
    latency = action.latency_seconds
    accepted_work = action.accepted_work_units
    if (
        quality_score is None
        or post_context is None
        or latency is None
        or accepted_work is None
    ):
        return None
    return quality_score, post_context, latency, accepted_work


def _eligible_actions(inputs: RotationDecisionInput) -> tuple[ProjectedAction, ...]:
    quality_floor = inputs.quality_floor
    if quality_floor is None:
        return ()
    eligible: list[ProjectedAction] = []
    for action in inputs.actions:
        values = _action_constraint_values(action)
        if values is None:
            continue
        quality_score, post_context, latency, accepted_work = values
        if (
            quality_score >= quality_floor
            and 0 <= post_context <= inputs.capacity_tokens
            and latency >= 0
            and accepted_work >= 0
        ):
            eligible.append(action)
    return tuple(eligible)


def _constraint_projection_error(action: ProjectedAction) -> str | None:
    values = {
        "post_action_context_tokens": action.post_action_context_tokens,
        "latency_seconds": action.latency_seconds,
        "quality_score": action.quality_score,
        "accepted_work_units": action.accepted_work_units,
    }
    missing = sorted(name for name, value in values.items() if value is None)
    if not missing:
        return None
    return f"{action.name} is missing projected constraint(s): {', '.join(missing)}"


def _input_projection_errors(inputs: RotationDecisionInput) -> list[str]:
    return [] if inputs.quality_floor is not None else ["quality_floor is missing"]


def _usage_projection_error(action: ProjectedAction) -> str | None:
    usage = action.total_usage
    values = {
        "input_tokens": usage.input_tokens,
        "cached_input_tokens": usage.cached_input_tokens,
        "cache_write_input_tokens": usage.cache_write_input_tokens,
        "output_tokens": usage.output_tokens,
        "reasoning_output_tokens": usage.reasoning_output_tokens,
        "tool_calls": usage.tool_calls,
    }
    missing = sorted(name for name, value in values.items() if value is None)
    if not missing:
        return None
    return f"{action.name} is missing projected usage: {', '.join(missing)}"


def _metered_projection_errors(inputs: RotationDecisionInput) -> list[str]:
    errors = _input_projection_errors(inputs)
    for action in inputs.actions:
        for error in (
            _constraint_projection_error(action),
            _usage_projection_error(action),
        ):
            if error is not None:
                errors.append(error)
    return errors


def _metered_scope_error(
    profile: MeteredApiProfile,
    inputs: RotationDecisionInput,
) -> str | None:
    error = _validate_inputs(inputs)
    if error is not None:
        return error
    if (
        inputs.provider == profile.provider
        and inputs.runtime == profile.runtime
        and inputs.model == profile.model
    ):
        return None
    return (
        "metered profile scope does not match "
        f"{inputs.provider}/{inputs.runtime}/{inputs.model}"
    )


class MeteredApiStrategy:
    """Minimize expected monetary cost subject to declared constraints."""

    OBJECTIVE = "metered_api:minimize_expected_monetary_cost"

    def __init__(self, profile: UsageEconomicsProfile) -> None:
        if not isinstance(profile, MeteredApiProfile):
            raise UsageEconomicsProfileValidationError("metered_api requires a metered profile")
        self.profile = profile

    def _cost(self, action: ProjectedAction) -> tuple[float | None, str | None]:
        usage = action.total_usage
        dimensions = (
            ("input_per_mtok", usage.input_tokens, self.profile.input_per_mtok, 1_000_000),
            (
                "cached_input_per_mtok",
                usage.cached_input_tokens,
                self.profile.cached_input_per_mtok,
                1_000_000,
            ),
            (
                "cache_write_per_mtok",
                usage.cache_write_input_tokens,
                self.profile.cache_write_per_mtok,
                1_000_000,
            ),
            ("output_per_mtok", usage.output_tokens, self.profile.output_per_mtok, 1_000_000),
            (
                "reasoning_output_per_mtok",
                usage.reasoning_output_tokens,
                self.profile.reasoning_output_per_mtok,
                1_000_000,
            ),
            ("tool_call_per_1k", usage.tool_calls, self.profile.tool_call_per_1k, 1_000),
        )
        total = 0.0
        for name, quantity, price, divisor in dimensions:
            assert quantity is not None
            if quantity < 0:
                return None, f"{action.name} has negative {name} usage"
            if quantity and price is None:
                return None, f"{name} is required to price {action.name}"
            total += quantity * float(price or 0.0) / divisor
        return total, None

    def decide(self, inputs: RotationDecisionInput) -> RotationEconomicsDecision:
        error = _metered_scope_error(self.profile, inputs)
        if error is not None:
            return self._unresolved("invalid_inputs", error, inputs=inputs)
        projection_errors = _metered_projection_errors(inputs)
        if projection_errors:
            return self._unresolved(
                "projection_unresolved",
                "; ".join(projection_errors),
                inputs=inputs,
            )
        eligible = _eligible_actions(inputs)
        if not eligible:
            return self._unresolved(
                "constraints_blocked",
                "no action meets capacity/quality/latency constraints",
                inputs=inputs,
            )
        priced: list[tuple[float, ProjectedAction]] = []
        for action in eligible:
            cost, price_error = self._cost(action)
            if price_error is not None:
                return self._unresolved("price_unknown", price_error, inputs=inputs)
            assert cost is not None
            priced.append((cost, action))
        cost, chosen = min(
            priced,
            key=lambda item: (
                item[0],
                _action_tie_priority(item[1]),
                item[1].latency_seconds,
                item[1].name,
            ),
        )
        return RotationEconomicsDecision(
            resolved=True,
            status="resolved",
            chosen_action=chosen.name,
            profile_id=self.profile.profile_id,
            profile_version=self.profile.profile_version,
            objective=self.OBJECTIVE,
            explanation=(
                f"{chosen.name} has the lowest projected {self.profile.currency} cost "
                f"({cost:.6f}) under {self.profile.profile_id}; capability and action "
                "calibration remain separate inputs"
            ),
            projected_constraints=_projected_constraints(inputs),
            expected_cost=cost,
        )

    def _unresolved(
        self,
        status: str,
        explanation: str,
        *,
        inputs: RotationDecisionInput,
    ) -> RotationEconomicsDecision:
        return RotationEconomicsDecision(
            resolved=False,
            status=status,
            chosen_action=None,
            profile_id=self.profile.profile_id,
            profile_version=self.profile.profile_version,
            objective=self.OBJECTIVE,
            explanation=explanation,
            projected_constraints=_projected_constraints(inputs),
        )


type _QuotaScore = tuple[
    float,
    float,
    int,
    float,
    str,
    ProjectedAction,
    AllowancePool | None,
]


def _flat_scope_error(
    profile: FlatRateQuotaProfile,
    inputs: RotationDecisionInput,
) -> str | None:
    error = _validate_inputs(inputs)
    if error is not None:
        return error
    if inputs.provider == profile.provider and inputs.runtime in profile.included_runtimes:
        return None
    return (
        "flat-rate profile scope does not match "
        f"{inputs.provider}/{inputs.runtime}/{inputs.model}"
    )


def _applicable_pools(
    profile: FlatRateQuotaProfile,
    inputs: RotationDecisionInput,
) -> tuple[AllowancePool, ...]:
    return tuple(
        pool
        for pool in profile.allowance_pools
        if pool.applies(runtime=inputs.runtime, model=inputs.model)
    )


def _pool_telemetry_error(pools: tuple[AllowancePool, ...]) -> str | None:
    for pool in pools:
        if pool.limit_status == "unbounded":
            continue
        if pool.reading_status != "current" or pool.remaining is None:
            return (
                f"quota pool {pool.pool_id} reading is {pool.reading_status}; "
                "unknown/unavailable is neither zero marginal cost nor unlimited capacity"
            )
    return None


def _quota_fraction(amount: float, remaining: float) -> float:
    if remaining > 0:
        return amount / remaining
    return math.inf if amount > 0 else 0.0


def _quota_score(
    action: ProjectedAction,
    pools: tuple[AllowancePool, ...],
) -> tuple[_QuotaScore | None, str | None]:
    assert action.accepted_work_units is not None
    assert action.latency_seconds is not None
    binding_fraction = -1.0
    binding_pool: AllowancePool | None = None
    for pool in pools:
        if pool.limit_status == "unbounded":
            continue
        amount = action.quota_by_pool.get(pool.pool_id)
        if amount is None:
            return None, f"{action.name}:{pool.pool_id}"
        if amount < 0:
            return None, f"{action.name}:{pool.pool_id}:negative"
        assert pool.remaining is not None
        if amount > pool.remaining:
            return None, None
        fraction = _quota_fraction(amount, pool.remaining)
        if fraction > binding_fraction:
            binding_fraction = fraction
            binding_pool = pool
    return (
        (
            -action.accepted_work_units,
            binding_fraction,
            _action_tie_priority(action),
            action.latency_seconds,
            action.name,
            action,
            binding_pool,
        ),
        None,
    )


def _quota_scores(
    actions: tuple[ProjectedAction, ...],
    pools: tuple[AllowancePool, ...],
) -> tuple[list[_QuotaScore], list[str]]:
    scores: list[_QuotaScore] = []
    missing: list[str] = []
    for action in actions:
        score, error = _quota_score(action, pools)
        if score is not None:
            scores.append(score)
        if error is not None:
            missing.append(error)
    return scores, missing


class FlatRateQuotaStrategy:
    """Maximize accepted work while respecting every applicable pool."""

    OBJECTIVE = "flat_rate_quota:maximize_accepted_work_over_reset_horizons"

    def __init__(
        self,
        profile: UsageEconomicsProfile,
        *,
        catalog: UsageEconomicsProfileCatalog | None = None,
    ) -> None:
        if not isinstance(profile, FlatRateQuotaProfile):
            raise UsageEconomicsProfileValidationError("flat_rate_quota requires a flat-rate profile")
        self.profile = profile
        self.catalog = catalog

    def decide(self, inputs: RotationDecisionInput) -> RotationEconomicsDecision:
        error = _flat_scope_error(self.profile, inputs)
        if error is not None:
            return self._unresolved("invalid_inputs", error, inputs=inputs)
        pools = _applicable_pools(self.profile, inputs)
        if not pools:
            return self._unresolved(
                "telemetry_unresolved",
                "no applicable allowance pool is declared",
                inputs=inputs,
            )
        projection_errors = _input_projection_errors(inputs) + [
            error
            for action in inputs.actions
            if (error := _constraint_projection_error(action)) is not None
        ]
        if projection_errors:
            return self._unresolved(
                "projection_unresolved",
                "; ".join(projection_errors),
                inputs=inputs,
                pools=pools,
            )
        telemetry_error = _pool_telemetry_error(pools)
        if telemetry_error is not None:
            return self._unresolved(
                "telemetry_unresolved",
                telemetry_error,
                inputs=inputs,
                pools=pools,
            )
        scored, missing = _quota_scores(_eligible_actions(inputs), pools)
        if missing:
            return self._unresolved(
                "telemetry_unresolved",
                "missing applicable-pool quota reading(s): " + ", ".join(sorted(missing)),
                inputs=inputs,
                pools=pools,
            )
        if not scored:
            return self._capacity_blocked_or_crossover(inputs, pools)
        return self._resolved_decision(min(scored), inputs=inputs, pools=pools)

    def _resolved_decision(
        self,
        score: _QuotaScore,
        *,
        inputs: RotationDecisionInput,
        pools: tuple[AllowancePool, ...],
    ) -> RotationEconomicsDecision:
        _, fraction, _, _, _, chosen, binding = score
        binding_text = "genuinely unbounded pools only"
        binding_id: str | None = None
        if binding is not None:
            binding_id = binding.pool_id
            binding_text = (
                f"binding pool {binding.pool_id} ({binding.reset_kind}, "
                f"native unit {binding.native_unit}, action/remaining {fraction:.3f})"
            )
        return RotationEconomicsDecision(
            resolved=True,
            status="resolved",
            chosen_action=chosen.name,
            profile_id=self.profile.profile_id,
            profile_version=self.profile.profile_version,
            objective=self.OBJECTIVE,
            explanation=(
                f"{chosen.name} maximizes accepted work subject to every applicable "
                f"allowance horizon; {binding_text}. Fixed subscription cost "
                "is descriptive plan data, not a per-token action price"
            ),
            projected_constraints=_projected_constraints(inputs),
            applicable_pool_ids=_pool_ids(pools),
            binding_pool_id=binding_id,
        )

    def _capacity_blocked_or_crossover(
        self,
        inputs: RotationDecisionInput,
        pools: tuple[AllowancePool, ...],
    ) -> RotationEconomicsDecision:
        binding = min(
            (pool for pool in pools if pool.limit_status != "unbounded"),
            key=lambda pool: float(pool.remaining or 0.0),
            default=None,
        )
        if not self.profile.overage_enabled:
            return replace(
                self._unresolved(
                    "capacity_blocked",
                    "included allowance capacity is exhausted and usage-credit crossover is disabled",
                    inputs=inputs,
                    pools=pools,
                ),
                binding_pool_id=None if binding is None else binding.pool_id,
            )
        if self.catalog is None or self.profile.overage_profile_id is None:
            return self._unresolved(
                "crossover_unresolved",
                "usage-credit crossover is enabled but no catalog/profile identity was supplied",
                inputs=inputs,
                pools=pools,
            )
        overage = self.catalog.resolve(self.profile.overage_profile_id)
        if not isinstance(overage, MeteredApiProfile):
            return self._unresolved(
                "crossover_unresolved",
                f"overage profile {self.profile.overage_profile_id} is not metered_api",
                inputs=inputs,
                pools=pools,
            )
        metered = MeteredApiStrategy(overage).decide(inputs)
        return replace(
            metered,
            profile_id=self.profile.profile_id,
            profile_version=self.profile.profile_version,
            objective=f"{self.OBJECTIVE}->metered_api",
            explanation=(
                f"included capacity exhausted; usage-credit crossover selected "
                f"separately versioned {overage.profile_id}. {metered.explanation}"
            ),
            binding_pool_id=None if binding is None else binding.pool_id,
            crossover_profile_id=overage.profile_id,
            applicable_pool_ids=_pool_ids(pools),
        )

    def _unresolved(
        self,
        status: str,
        explanation: str,
        *,
        inputs: RotationDecisionInput,
        pools: tuple[AllowancePool, ...] = (),
    ) -> RotationEconomicsDecision:
        return RotationEconomicsDecision(
            resolved=False,
            status=status,
            chosen_action=None,
            profile_id=self.profile.profile_id,
            profile_version=self.profile.profile_version,
            objective=self.OBJECTIVE,
            explanation=explanation,
            projected_constraints=_projected_constraints(inputs),
            applicable_pool_ids=_pool_ids(pools),
        )


__all__ = [
    "AllowancePool",
    "FlatRateQuotaProfile",
    "FlatRateQuotaStrategy",
    "MeteredApiProfile",
    "MeteredApiStrategy",
    "ProjectedAction",
    "RotationDecisionInput",
    "RotationEconomicsDecision",
    "UsageEconomicsProfileCatalog",
    "UsageEconomicsProfileValidationError",
    "UsageVector",
    "load_usage_economics_profile_catalog",
]
