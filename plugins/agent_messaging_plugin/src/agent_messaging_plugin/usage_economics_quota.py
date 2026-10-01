"""Which flat-rate plan covers a runtime, and the fail-closed quota state of a runtime/model pair."""

from __future__ import annotations

from typing import Literal

from .allowance_pools import AllowancePool
from .usage_economics_fields import UsageEconomicsProfileValidationError
from .usage_economics_profiles import FlatRateQuotaProfile, UsageEconomicsProfileCatalog

QuotaStatus = Literal["available", "exhausted", "unknown"]


def plan_for_runtime(catalog: UsageEconomicsProfileCatalog, runtime: str) -> FlatRateQuotaProfile | None:
    """The one current flat-rate plan covering ``runtime``; none when no plan covers it, a refusal when two do."""
    plans = [
        profile
        for profile in catalog.profiles
        if isinstance(profile, FlatRateQuotaProfile)
        and profile.refresh_status == "current"
        and runtime in profile.included_runtimes
    ]
    if len(plans) > 1:
        raise UsageEconomicsProfileValidationError(
            f"{len(plans)} current flat-rate plans cover runtime {runtime!r}; dispatch weights are ambiguous",
        )
    return plans[0] if plans else None


def quota_status_for_pair(
    catalog: UsageEconomicsProfileCatalog,
    *,
    runtime: str,
    model: str,
) -> QuotaStatus:
    """Return the fail-closed quota state for one selectable runtime/model pair."""
    plans = [
        profile
        for profile in catalog.profiles
        if isinstance(profile, FlatRateQuotaProfile) and runtime in profile.included_runtimes
    ]
    return _plan_quota_status(plans, runtime=runtime, model=model)


def _plan_quota_status(
    plans: list[FlatRateQuotaProfile], *, runtime: str, model: str,
) -> QuotaStatus:
    if not plans:
        return "available"
    if len(plans) != 1:
        return "unknown"
    return _pool_quota_status(plans[0].allowance_pools, runtime=runtime, model=model)


def _is_known_exhausted(pool: AllowancePool) -> bool:
    return pool.reading_status == "current" and pool.remaining is not None and pool.remaining <= 0


def _is_unread(pool: AllowancePool) -> bool:
    return pool.reading_status != "current" or pool.remaining is None


def _pool_quota_status(
    pools: tuple[AllowancePool, ...], *, runtime: str, model: str,
) -> QuotaStatus:
    """A known exhausted pool wins over an unread sibling, so one used-up window still excludes the cell."""
    applicable = [pool for pool in pools if pool.applies(runtime=runtime, model=model)]
    if not applicable:
        return "unknown"
    if any(_is_known_exhausted(pool) for pool in applicable):
        return "exhausted"
    if any(_is_unread(pool) for pool in applicable):
        return "unknown"
    return "available"


__all__ = ["QuotaStatus", "plan_for_runtime", "quota_status_for_pair"]
