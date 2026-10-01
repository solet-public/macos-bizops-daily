"""Versioned metered-price and subscription-quota profile loading."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from .allowance_pools import AllowancePool, parse_pools
from .dispatch_weights import DispatchWeights, parse_dispatch_weights
from .usage_economics_fields import (
    UsageEconomicsProfileValidationError,
    require_number,
    require_stamp,
    require_strings,
    require_text,
)


@dataclass(frozen=True)
class MeteredApiProfile:
    profile_id: str
    profile_version: str
    provider: str
    runtime: str
    model: str
    effective_at: datetime
    currency: str
    price_unit: str
    input_per_mtok: float | None
    cached_input_per_mtok: float | None
    cache_write_per_mtok: float | None
    output_per_mtok: float | None
    reasoning_output_per_mtok: float | None
    tool_call_per_1k: float | None
    source_ref: str
    fetched_at: datetime
    refresh_status: str
    fixture_only: bool


@dataclass(frozen=True)
class FlatRateQuotaProfile:
    profile_id: str
    profile_version: str
    provider: str
    plan_id: str
    billing_cadence: str
    fixed_subscription_cost: float
    currency: str
    effective_at: datetime
    included_runtimes: tuple[str, ...]
    included_channels: tuple[str, ...]
    allowance_pools: tuple[AllowancePool, ...]
    overage_profile_id: str | None
    overage_enabled: bool
    source_ref: str
    fetched_at: datetime
    refresh_status: str
    dispatch_weights: DispatchWeights | None = None


type UsageEconomicsProfile = MeteredApiProfile | FlatRateQuotaProfile


@dataclass(frozen=True)
class UsageEconomicsProfileCatalog:
    profiles: tuple[UsageEconomicsProfile, ...]

    def resolve(self, profile_id: str) -> UsageEconomicsProfile:
        matches = [profile for profile in self.profiles if profile.profile_id == profile_id]
        if len(matches) != 1:
            raise UsageEconomicsProfileValidationError(
                f"no unique usage-economics profile {profile_id!r}",
            )
        profile = matches[0]
        if profile.refresh_status != "current":
            raise UsageEconomicsProfileValidationError(
                f"refresh_status is {profile.refresh_status!r} for {profile_id}; "
                f"refresh from {profile.source_ref}",
            )
        if isinstance(profile, MeteredApiProfile) and profile.fixture_only:
            raise UsageEconomicsProfileValidationError(
                f"{profile_id} is a stale fixture and cannot produce a verdict",
            )
        return profile


def _effective_at(raw: dict[str, Any], *, as_of: datetime) -> datetime:
    effective = require_stamp(raw.get("effective_at"), "effective_at")
    if effective > as_of:
        raise UsageEconomicsProfileValidationError(f"not effective until {effective.isoformat()}")
    return effective


def _metered(raw: dict[str, Any], *, as_of: datetime) -> MeteredApiProfile:
    effective = _effective_at(raw, as_of=as_of)
    fixture_only = raw.get("fixture_only") is True
    refresh_status = require_text(raw.get("refresh_status"), "refresh_status")
    if refresh_status != "current" and not fixture_only:
        raise UsageEconomicsProfileValidationError(f"refresh_status is {refresh_status!r}")
    return MeteredApiProfile(
        profile_id=require_text(raw.get("profile_id"), "profile_id"),
        profile_version=require_text(raw.get("profile_version"), "profile_version"),
        provider=require_text(raw.get("provider"), "provider"),
        runtime=require_text(raw.get("runtime"), "runtime"),
        model=require_text(raw.get("model"), "model"),
        effective_at=effective,
        currency=require_text(raw.get("currency"), "currency"),
        price_unit=require_text(raw.get("price_unit"), "price_unit"),
        input_per_mtok=require_number(raw.get("input_per_mtok"), "input_per_mtok", optional=True),
        cached_input_per_mtok=require_number(
            raw.get("cached_input_per_mtok"),
            "cached_input_per_mtok",
            optional=True,
        ),
        cache_write_per_mtok=require_number(
            raw.get("cache_write_per_mtok"),
            "cache_write_per_mtok",
            optional=True,
        ),
        output_per_mtok=require_number(raw.get("output_per_mtok"), "output_per_mtok", optional=True),
        reasoning_output_per_mtok=require_number(
            raw.get("reasoning_output_per_mtok"),
            "reasoning_output_per_mtok",
            optional=True,
        ),
        tool_call_per_1k=require_number(
            raw.get("tool_call_per_1k"),
            "tool_call_per_1k",
            optional=True,
        ),
        source_ref=require_text(raw.get("source_ref"), "source_ref"),
        fetched_at=require_stamp(raw.get("fetched_at"), "fetched_at"),
        refresh_status=refresh_status,
        fixture_only=fixture_only,
    )


def _overage(raw: dict[str, Any]) -> tuple[str | None, bool]:
    value = raw.get("overage")
    if not isinstance(value, dict):
        raise UsageEconomicsProfileValidationError("overage must be an object")
    profile_id = value.get("strategy_profile_id")
    return (
        None if profile_id is None else require_text(profile_id, "strategy_profile_id"),
        value.get("enabled") is True,
    )


def _flat(raw: dict[str, Any], *, as_of: datetime) -> FlatRateQuotaProfile:
    effective = _effective_at(raw, as_of=as_of)
    refresh_status = require_text(raw.get("refresh_status"), "refresh_status")
    if refresh_status != "current":
        raise UsageEconomicsProfileValidationError(f"refresh_status is {refresh_status!r}")
    overage_id, overage_enabled = _overage(raw)
    subscription_cost = require_number(
        raw.get("fixed_subscription_cost"),
        "fixed_subscription_cost",
    )
    assert subscription_cost is not None
    return FlatRateQuotaProfile(
        profile_id=require_text(raw.get("profile_id"), "profile_id"),
        profile_version=require_text(raw.get("profile_version"), "profile_version"),
        provider=require_text(raw.get("provider"), "provider"),
        plan_id=require_text(raw.get("plan_id"), "plan_id"),
        billing_cadence=require_text(raw.get("billing_cadence"), "billing_cadence"),
        fixed_subscription_cost=subscription_cost,
        currency=require_text(raw.get("currency"), "currency"),
        effective_at=effective,
        included_runtimes=require_strings(raw.get("included_runtimes"), "included_runtimes"),
        included_channels=require_strings(raw.get("included_channels"), "included_channels"),
        allowance_pools=parse_pools(raw, as_of=as_of),
        overage_profile_id=overage_id,
        overage_enabled=overage_enabled,
        source_ref=require_text(raw.get("source_ref"), "source_ref"),
        fetched_at=require_stamp(raw.get("fetched_at"), "fetched_at"),
        refresh_status=refresh_status,
        dispatch_weights=parse_dispatch_weights(raw.get("dispatch_weights")),
    )


def _root(path: Path) -> dict[str, Any]:
    try:
        raw: Any = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise UsageEconomicsProfileValidationError(
            f"cannot load economics profile {path}: {exc}"
        ) from exc
    if not isinstance(raw, dict) or raw.get("schema_version") != 1:
        raise UsageEconomicsProfileValidationError("unsupported schema_version")
    return raw


def _profile_row(raw: object, *, as_of: datetime) -> UsageEconomicsProfile:
    if not isinstance(raw, dict):
        raise UsageEconomicsProfileValidationError("profiles entries must be objects")
    kind = require_text(raw.get("kind"), "kind")
    if kind == "metered_api":
        return _metered(raw, as_of=as_of)
    if kind == "flat_rate_quota":
        return _flat(raw, as_of=as_of)
    raise UsageEconomicsProfileValidationError(
        f"unsupported economics kind {kind!r}; add a strategy implementation",
    )


def _validate_profile_links(profiles: tuple[UsageEconomicsProfile, ...]) -> None:
    by_id = {profile.profile_id: profile for profile in profiles}
    if len(by_id) != len(profiles):
        raise UsageEconomicsProfileValidationError("profile_id values must be unique")
    for profile in profiles:
        if not isinstance(profile, FlatRateQuotaProfile) or profile.overage_profile_id is None:
            continue
        referenced = by_id.get(profile.overage_profile_id)
        if not isinstance(referenced, MeteredApiProfile):
            raise UsageEconomicsProfileValidationError(
                f"overage profile {profile.overage_profile_id!r} is missing or not metered_api",
            )


def load_usage_economics_profile_catalog(
    path: Path,
    *,
    as_of: datetime,
) -> UsageEconomicsProfileCatalog:
    """Load versioned strategies and their plan/price configuration."""
    if as_of.tzinfo is None:
        raise UsageEconomicsProfileValidationError("as_of must be timezone-aware")
    rows = _root(path).get("profiles")
    if not isinstance(rows, list) or not rows:
        raise UsageEconomicsProfileValidationError("profiles must be a non-empty list")
    profiles = tuple(_profile_row(row, as_of=as_of) for row in rows)
    _validate_profile_links(profiles)
    return UsageEconomicsProfileCatalog(profiles=profiles)


__all__ = [
    "AllowancePool",
    "DispatchWeights",
    "FlatRateQuotaProfile",
    "MeteredApiProfile",
    "UsageEconomicsProfile",
    "UsageEconomicsProfileCatalog",
    "UsageEconomicsProfileValidationError",
    "load_usage_economics_profile_catalog",
]
