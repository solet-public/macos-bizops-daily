"""Versioned metered-price and subscription-quota profile loading."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Literal


class UsageEconomicsProfileValidationError(ValueError):
    """A usage profile cannot support an honest verdict."""


def _text(raw: object, field: str) -> str:
    if not isinstance(raw, str) or not raw.strip():
        raise UsageEconomicsProfileValidationError(f"{field} must be a non-empty string")
    return raw.strip()


def _stamp(raw: object, field: str) -> datetime:
    if not isinstance(raw, str) or not raw:
        raise UsageEconomicsProfileValidationError(f"{field} must be a timestamp")
    try:
        value = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as exc:
        raise UsageEconomicsProfileValidationError(f"{field} is invalid: {raw!r}") from exc
    if value.tzinfo is None:
        raise UsageEconomicsProfileValidationError(f"{field} must be timezone-aware")
    return value


def _number(raw: object, field: str, *, optional: bool = False) -> float | None:
    if raw is None and optional:
        return None
    if not isinstance(raw, (int, float)) or isinstance(raw, bool) or raw < 0:
        raise UsageEconomicsProfileValidationError(f"{field} must be non-negative")
    return float(raw)


def _strings(raw: object, field: str) -> tuple[str, ...]:
    if not isinstance(raw, list) or not raw:
        raise UsageEconomicsProfileValidationError(f"{field} must be a non-empty list")
    values = tuple(_text(value, field) for value in raw)
    if len(values) != len(set(values)):
        raise UsageEconomicsProfileValidationError(f"{field} contains duplicates")
    return values


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
class AllowancePool:
    pool_id: str
    native_unit: str
    applicable_runtimes: tuple[str, ...]
    applicable_model_prefixes: tuple[str, ...]
    limit_status: str
    limit: float | None
    reading_status: str
    consumed: float | None
    remaining: float | None
    reset_kind: str
    next_reset_at: datetime | None
    telemetry_authority: str

    def applies(self, *, runtime: str, model: str) -> bool:
        runtime_match = "*" in self.applicable_runtimes or runtime in self.applicable_runtimes
        model_match = "*" in self.applicable_model_prefixes or any(
            model.startswith(prefix) for prefix in self.applicable_model_prefixes
        )
        return runtime_match and model_match


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


type UsageEconomicsProfile = MeteredApiProfile | FlatRateQuotaProfile
QuotaStatus = Literal["available", "exhausted", "unknown"]


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
    effective = _stamp(raw.get("effective_at"), "effective_at")
    if effective > as_of:
        raise UsageEconomicsProfileValidationError(f"not effective until {effective.isoformat()}")
    return effective


def _metered(raw: dict[str, Any], *, as_of: datetime) -> MeteredApiProfile:
    effective = _effective_at(raw, as_of=as_of)
    fixture_only = raw.get("fixture_only") is True
    refresh_status = _text(raw.get("refresh_status"), "refresh_status")
    if refresh_status != "current" and not fixture_only:
        raise UsageEconomicsProfileValidationError(f"refresh_status is {refresh_status!r}")
    return MeteredApiProfile(
        profile_id=_text(raw.get("profile_id"), "profile_id"),
        profile_version=_text(raw.get("profile_version"), "profile_version"),
        provider=_text(raw.get("provider"), "provider"),
        runtime=_text(raw.get("runtime"), "runtime"),
        model=_text(raw.get("model"), "model"),
        effective_at=effective,
        currency=_text(raw.get("currency"), "currency"),
        price_unit=_text(raw.get("price_unit"), "price_unit"),
        input_per_mtok=_number(raw.get("input_per_mtok"), "input_per_mtok", optional=True),
        cached_input_per_mtok=_number(
            raw.get("cached_input_per_mtok"),
            "cached_input_per_mtok",
            optional=True,
        ),
        cache_write_per_mtok=_number(
            raw.get("cache_write_per_mtok"),
            "cache_write_per_mtok",
            optional=True,
        ),
        output_per_mtok=_number(raw.get("output_per_mtok"), "output_per_mtok", optional=True),
        reasoning_output_per_mtok=_number(
            raw.get("reasoning_output_per_mtok"),
            "reasoning_output_per_mtok",
            optional=True,
        ),
        tool_call_per_1k=_number(
            raw.get("tool_call_per_1k"),
            "tool_call_per_1k",
            optional=True,
        ),
        source_ref=_text(raw.get("source_ref"), "source_ref"),
        fetched_at=_stamp(raw.get("fetched_at"), "fetched_at"),
        refresh_status=refresh_status,
        fixture_only=fixture_only,
    )


def _reject_token_conversion(raw: dict[str, Any]) -> None:
    forbidden = sorted(key for key in raw if "token_conversion" in key)
    if forbidden:
        raise UsageEconomicsProfileValidationError(
            f"invented token conversion is forbidden: {', '.join(forbidden)}",
        )


def _limit_fields(raw: dict[str, Any]) -> tuple[str, float | None]:
    status = _text(raw.get("limit_status"), "limit_status")
    if status not in {"known", "unknown", "unbounded"}:
        raise UsageEconomicsProfileValidationError(f"invalid limit_status {status!r}")
    limit = _number(raw.get("limit"), "limit", optional=True)
    if status == "known" and (limit is None or limit <= 0):
        raise UsageEconomicsProfileValidationError("known limit must be positive")
    if status != "known" and limit is not None:
        raise UsageEconomicsProfileValidationError(f"{status} limit must be null")
    return status, limit


def _reading_fields(
    raw: dict[str, Any], *, as_of: datetime
) -> tuple[str, float | None, float | None, datetime | None]:
    status = _text(raw.get("reading_status"), "reading_status")
    if status not in {"current", "unknown", "unavailable"}:
        raise UsageEconomicsProfileValidationError(f"stale or invalid reading_status {status!r}")
    consumed, remaining = _reading_counts(raw, status=status)
    reset = _reading_reset(raw, status=status, as_of=as_of)
    return status, consumed, remaining, reset


def _reading_counts(
    raw: dict[str, Any], *, status: str
) -> tuple[float | None, float | None]:
    consumed = _number(raw.get("consumed"), "consumed", optional=True)
    remaining = _number(raw.get("remaining"), "remaining", optional=True)
    if status == "current" and consumed is None and remaining is None:
        raise UsageEconomicsProfileValidationError("current pool needs consumed or remaining")
    if status != "current" and (consumed is not None or remaining is not None):
        raise UsageEconomicsProfileValidationError(
            f"{status} pool cannot carry a numeric reading",
        )
    return consumed, remaining


def _reading_reset(
    raw: dict[str, Any], *, status: str, as_of: datetime
) -> datetime | None:
    reset = None if raw.get("next_reset_at") is None else _stamp(
        raw.get("next_reset_at"),
        "next_reset_at",
    )
    if status == "current" and reset is None:
        raise UsageEconomicsProfileValidationError("current pool needs next_reset_at")
    if status == "current" and reset is not None and reset <= as_of:
        raise UsageEconomicsProfileValidationError(
            f"stale reset timestamp {reset.isoformat()}",
        )
    return reset


def _pool(raw: object, *, as_of: datetime) -> AllowancePool:
    if not isinstance(raw, dict):
        raise UsageEconomicsProfileValidationError("allowance_pools entries must be objects")
    _reject_token_conversion(raw)
    limit_status, limit = _limit_fields(raw)
    reading_status, consumed, remaining, reset = _reading_fields(raw, as_of=as_of)
    return AllowancePool(
        pool_id=_text(raw.get("pool_id"), "pool_id"),
        native_unit=_text(raw.get("native_unit"), "native_unit"),
        applicable_runtimes=_strings(raw.get("applicable_runtimes"), "applicable_runtimes"),
        applicable_model_prefixes=_strings(
            raw.get("applicable_model_prefixes"),
            "applicable_model_prefixes",
        ),
        limit_status=limit_status,
        limit=limit,
        reading_status=reading_status,
        consumed=consumed,
        remaining=remaining,
        reset_kind=_text(raw.get("reset_kind"), "reset_kind"),
        next_reset_at=reset,
        telemetry_authority=_text(raw.get("telemetry_authority"), "telemetry_authority"),
    )


def _pools(raw: dict[str, Any], *, as_of: datetime) -> tuple[AllowancePool, ...]:
    rows = raw.get("allowance_pools")
    if not isinstance(rows, list) or not rows:
        raise UsageEconomicsProfileValidationError("allowance_pools must be a non-empty list")
    pools = tuple(_pool(pool, as_of=as_of) for pool in rows)
    if len({pool.pool_id for pool in pools}) != len(pools):
        raise UsageEconomicsProfileValidationError("allowance pool ids must be unique")
    return pools


def _overage(raw: dict[str, Any]) -> tuple[str | None, bool]:
    value = raw.get("overage")
    if not isinstance(value, dict):
        raise UsageEconomicsProfileValidationError("overage must be an object")
    profile_id = value.get("strategy_profile_id")
    return (
        None if profile_id is None else _text(profile_id, "strategy_profile_id"),
        value.get("enabled") is True,
    )


def _flat(raw: dict[str, Any], *, as_of: datetime) -> FlatRateQuotaProfile:
    effective = _effective_at(raw, as_of=as_of)
    refresh_status = _text(raw.get("refresh_status"), "refresh_status")
    if refresh_status != "current":
        raise UsageEconomicsProfileValidationError(f"refresh_status is {refresh_status!r}")
    overage_id, overage_enabled = _overage(raw)
    subscription_cost = _number(
        raw.get("fixed_subscription_cost"),
        "fixed_subscription_cost",
    )
    assert subscription_cost is not None
    return FlatRateQuotaProfile(
        profile_id=_text(raw.get("profile_id"), "profile_id"),
        profile_version=_text(raw.get("profile_version"), "profile_version"),
        provider=_text(raw.get("provider"), "provider"),
        plan_id=_text(raw.get("plan_id"), "plan_id"),
        billing_cadence=_text(raw.get("billing_cadence"), "billing_cadence"),
        fixed_subscription_cost=subscription_cost,
        currency=_text(raw.get("currency"), "currency"),
        effective_at=effective,
        included_runtimes=_strings(raw.get("included_runtimes"), "included_runtimes"),
        included_channels=_strings(raw.get("included_channels"), "included_channels"),
        allowance_pools=_pools(raw, as_of=as_of),
        overage_profile_id=overage_id,
        overage_enabled=overage_enabled,
        source_ref=_text(raw.get("source_ref"), "source_ref"),
        fetched_at=_stamp(raw.get("fetched_at"), "fetched_at"),
        refresh_status=refresh_status,
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
    kind = _text(raw.get("kind"), "kind")
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


def _pool_quota_status(
    pools: tuple[AllowancePool, ...], *, runtime: str, model: str,
) -> QuotaStatus:
    applicable = [pool for pool in pools if pool.applies(runtime=runtime, model=model)]
    if not applicable:
        return "unknown"
    if any(pool.reading_status != "current" or pool.remaining is None for pool in applicable):
        return "unknown"
    if any((remaining := pool.remaining) is not None and remaining <= 0 for pool in applicable):
        return "exhausted"
    return "available"


__all__ = [
    "AllowancePool",
    "FlatRateQuotaProfile",
    "MeteredApiProfile",
    "QuotaStatus",
    "UsageEconomicsProfile",
    "UsageEconomicsProfileCatalog",
    "UsageEconomicsProfileValidationError",
    "load_usage_economics_profile_catalog",
    "quota_status_for_pair",
]
