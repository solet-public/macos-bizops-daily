"""Operator-recorded allowance-pool readings laid over the static usage profile (iss_c6fbf9ae).

The shipped ``usage_economics.v1.json`` cannot know how much of a flat-rate
plan's window is used, so every pool starts ``unknown`` and the selector
excludes every cell it covers. A reading becomes current only when someone
records one: an explicit percent of the window from the provider's own
usage page, the instant it was taken, when it stops being true, who gave it,
and who wrote it down. Nothing here infers a reading, and a reading past its
expiry reads as unknown again, so the selector fails closed.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Final

from ananta.llm.agent_messaging.role_binding import AGENT_ROLE_BINDING_NAMESPACE
from ananta.llm.agent_messaging.state_results import require_completed, require_deleted, require_records

from .model_capability_store import CatalogError
from .schema import TABLE_ALLOWANCE_POOL_READING
from .usage_economics_profiles import (
    AllowancePool,
    FlatRateQuotaProfile,
    UsageEconomicsProfileCatalog,
    UsageEconomicsProfileValidationError,
    load_usage_economics_profile_catalog,
)

if TYPE_CHECKING:
    from pathlib import Path

    from ananta.interfaces.state_management_interface import StateManagementInterface

PERCENT_NATIVE_UNIT: Final[str] = "provider_reported_usage"
"""The only native unit a reading can be recorded in: percent of the window, 0-100."""
MAX_READINGS: Final[int] = 100
_KEY: Final[tuple[str, str]] = ("profile_id", "pool_id")
_PERCENT_SUM_TOLERANCE: Final[float] = 1e-6


@dataclass(frozen=True, slots=True)
class PoolReading:
    """One stored reading of one allowance pool."""

    profile_id: str
    pool_id: str
    consumed: float
    remaining: float
    as_of: datetime
    next_reset_at: datetime | None
    expires_at: datetime | None
    source: str
    recorded_by: str
    recorded_at: datetime

    @property
    def effective_expiry(self) -> datetime:
        bounds = [bound for bound in (self.next_reset_at, self.expires_at) if bound is not None]
        return min(bounds)

    def is_current(self, now: datetime) -> bool:
        return self.as_of <= now < self.effective_expiry

    def to_payload(self, now: datetime) -> dict[str, Any]:
        return {
            "profile_id": self.profile_id,
            "pool_id": self.pool_id,
            "consumed": self.consumed,
            "remaining": self.remaining,
            "as_of": self.as_of.isoformat(),
            "next_reset_at": None if self.next_reset_at is None else self.next_reset_at.isoformat(),
            "expires_at": None if self.expires_at is None else self.expires_at.isoformat(),
            "effective_expiry": self.effective_expiry.isoformat(),
            "state": "current" if self.is_current(now) else "expired",
            "source": self.source,
            "recorded_by": self.recorded_by,
            "recorded_at": self.recorded_at.isoformat(),
        }


def _text(raw: object, field: str) -> str:
    if not isinstance(raw, str) or not raw.strip():
        raise CatalogError("parameter_invalid", f"{field} is required and must be a non-blank string.")
    return raw.strip()


def _stamp(raw: object, field: str) -> datetime:
    if not isinstance(raw, str) or not raw.strip():
        raise CatalogError("parameter_invalid", f"{field} must be an ISO-8601 timestamp with a timezone.")
    try:
        value = datetime.fromisoformat(raw.strip().replace("Z", "+00:00"))
    except ValueError as exc:
        raise CatalogError("parameter_invalid", f"{field} is not an ISO-8601 timestamp: {raw!r}.") from exc
    if value.tzinfo is None:
        raise CatalogError("parameter_invalid", f"{field} must carry a timezone: {raw!r}.")
    return value.astimezone(UTC)


def _optional_stamp(raw: object, field: str) -> datetime | None:
    return None if raw is None or raw == "" else _stamp(raw, field)


def _percent(raw: object, field: str) -> float | None:
    if raw is None or raw == "":
        return None
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        raise CatalogError("parameter_invalid", f"{field} must be a number, percent of the window.")
    value = float(raw)
    if not 0.0 <= value <= 100.0:
        raise CatalogError("parameter_invalid", f"{field} must be between 0 and 100 percent; got {value}.")
    return value


def _consumed_and_remaining(params: dict[str, Any]) -> tuple[float, float]:
    consumed = _percent(params.get("consumed"), "consumed")
    remaining = _percent(params.get("remaining"), "remaining")
    if consumed is None and remaining is None:
        raise CatalogError("parameter_invalid", "consumed or remaining (percent of the window, 0-100) is required.")
    if consumed is None:
        assert remaining is not None
        return 100.0 - remaining, remaining
    if remaining is None:
        return consumed, 100.0 - consumed
    if abs(consumed + remaining - 100.0) > _PERCENT_SUM_TOLERANCE:
        raise CatalogError("parameter_invalid", f"consumed {consumed} and remaining {remaining} must sum to 100.")
    return consumed, remaining


def _flat_profile(path: Path, profile_id: str, *, now: datetime) -> FlatRateQuotaProfile:
    try:
        catalog = load_usage_economics_profile_catalog(path, as_of=now)
    except UsageEconomicsProfileValidationError as exc:
        raise CatalogError("quota_state_unknown", f"quota profile is unavailable: {exc}") from exc
    profile = next((item for item in catalog.profiles if item.profile_id == profile_id), None)
    if not isinstance(profile, FlatRateQuotaProfile):
        known = sorted(item.profile_id for item in catalog.profiles if isinstance(item, FlatRateQuotaProfile))
        raise CatalogError("parameter_invalid", f"profile_id {profile_id!r} is not a flat-rate quota profile; known: {known}.")
    return profile


def _static_pool(path: Path, profile_id: str, pool_id: str, *, now: datetime) -> AllowancePool:
    profile = _flat_profile(path, profile_id, now=now)
    pool = next((item for item in profile.allowance_pools if item.pool_id == pool_id), None)
    if pool is None:
        raise CatalogError(
            "parameter_invalid",
            f"pool_id {pool_id!r} is not a pool of {profile_id}; pools: {sorted(p.pool_id for p in profile.allowance_pools)}.",
        )
    if pool.native_unit != PERCENT_NATIVE_UNIT:
        raise CatalogError(
            "parameter_invalid",
            f"pool {pool_id} counts {pool.native_unit!r}; only {PERCENT_NATIVE_UNIT} percent-of-window readings can be recorded.",
        )
    return pool


def _expiry_fields(params: dict[str, Any], *, as_of: datetime, now: datetime) -> tuple[datetime | None, datetime | None]:
    next_reset_at = _optional_stamp(params.get("next_reset_at"), "next_reset_at")
    expires_at = _optional_stamp(params.get("expires_at"), "expires_at")
    if next_reset_at is None and expires_at is None:
        raise CatalogError("parameter_invalid", "next_reset_at or expires_at is required: a reading must say when it stops being true.")
    for field, bound in (("next_reset_at", next_reset_at), ("expires_at", expires_at)):
        if bound is not None and bound <= max(as_of, now):
            raise CatalogError("parameter_invalid", f"{field} {bound.isoformat()} is not after the reading and now.")
    return next_reset_at, expires_at


def record_allowance_pool_reading(
    state: StateManagementInterface, params: dict[str, Any], *, profile_path: Path, now: datetime,
) -> dict[str, Any]:
    """Write one explicit reading, replacing any earlier one for the same pool."""
    profile_id = _text(params.get("profile_id"), "profile_id")
    pool_id = _text(params.get("pool_id"), "pool_id")
    pool = _static_pool(profile_path, profile_id, pool_id, now=now)
    consumed, remaining = _consumed_and_remaining(params)
    as_of = _stamp(params.get("as_of"), "as_of")
    if as_of > now:
        raise CatalogError("parameter_invalid", f"as_of {as_of.isoformat()} is in the future.")
    next_reset_at, expires_at = _expiry_fields(params, as_of=as_of, now=now)
    reading = PoolReading(
        profile_id=profile_id, pool_id=pool_id, consumed=consumed, remaining=remaining, as_of=as_of,
        next_reset_at=next_reset_at, expires_at=expires_at, source=_text(params.get("source"), "source"),
        recorded_by=_text(params.get("recorded_by"), "recorded_by"), recorded_at=now,
    )
    record = {
        "profile_id": profile_id, "pool_id": pool_id, "native_unit": pool.native_unit,
        "consumed": consumed, "remaining": remaining, "as_of": as_of.isoformat(),
        "next_reset_at": None if next_reset_at is None else next_reset_at.isoformat(),
        "expires_at": None if expires_at is None else expires_at.isoformat(),
        "source": reading.source, "recorded_by": reading.recorded_by, "recorded_at": now.isoformat(),
    }
    require_completed(
        state.upsert_state(
            AGENT_ROLE_BINDING_NAMESPACE,
            {"table": TABLE_ALLOWANCE_POOL_READING, "record": record, "conflict_columns": list(_KEY)},
        ),
        "record allowance pool reading",
    )
    return reading.to_payload(now)


def _row_reading(row: dict[str, Any]) -> PoolReading:
    try:
        return PoolReading(
            profile_id=str(row["profile_id"]), pool_id=str(row["pool_id"]),
            consumed=float(row["consumed"]), remaining=float(row["remaining"]),
            as_of=_stamp(row["as_of"], "as_of"),
            next_reset_at=_optional_stamp(row.get("next_reset_at"), "next_reset_at"),
            expires_at=_optional_stamp(row.get("expires_at"), "expires_at"),
            source=str(row["source"]), recorded_by=str(row["recorded_by"]),
            recorded_at=_stamp(row["recorded_at"], "recorded_at"),
        )
    except (KeyError, TypeError, ValueError, CatalogError) as exc:
        raise CatalogError("catalog_invalid", f"allowance_pool_reading row is unreadable: {exc}") from exc


def read_pool_readings(state: StateManagementInterface, *, profile_id: str | None = None) -> tuple[PoolReading, ...]:
    """Every stored reading, current or expired, oldest pool first."""
    rows = require_records(
        state.query_ordered(
            AGENT_ROLE_BINDING_NAMESPACE,
            {
                "table": TABLE_ALLOWANCE_POOL_READING,
                "filters": {} if profile_id is None else {"profile_id": profile_id},
                "order_by": [["profile_id", "asc"], ["pool_id", "asc"], ["id", "asc"]],
                "limit": MAX_READINGS,
            },
        ),
    )
    if len(rows) >= MAX_READINGS:
        raise CatalogError("catalog_too_large", f"allowance reading read filled a {MAX_READINGS}-row page; refusing a possibly truncated table.")
    return tuple(_row_reading(dict(row)) for row in rows)


def read_allowance_pool_readings(state: StateManagementInterface, params: dict[str, Any], *, now: datetime) -> dict[str, Any]:
    """Stored readings with each marked current or expired against ``now``."""
    raw = params.get("profile_id")
    profile_id = None if raw is None or raw == "" else _text(raw, "profile_id")
    readings = read_pool_readings(state, profile_id=profile_id)
    return {
        "readings": [reading.to_payload(now) for reading in readings],
        "current_readings": sum(1 for reading in readings if reading.is_current(now)),
        "as_of": now.isoformat(),
    }


def retract_allowance_pool_reading(state: StateManagementInterface, params: dict[str, Any]) -> dict[str, Any]:
    """Delete the reading for one pool, so the pool reads unknown again."""
    profile_id = _text(params.get("profile_id"), "profile_id")
    pool_id = _text(params.get("pool_id"), "pool_id")
    deleted = require_deleted(
        state.delete_records(
            AGENT_ROLE_BINDING_NAMESPACE,
            {
                "table": TABLE_ALLOWANCE_POOL_READING,
                "filters": {"profile_id": profile_id, "pool_id": pool_id},
                "soft_delete": False,
            },
        ),
    )
    if deleted == 0:
        raise CatalogError("reading_not_found", f"no recorded reading for {profile_id}/{pool_id}.")
    return {"profile_id": profile_id, "pool_id": pool_id, "retracted": deleted}


def apply_pool_readings(
    economics: UsageEconomicsProfileCatalog, readings: tuple[PoolReading, ...], *, now: datetime,
) -> tuple[UsageEconomicsProfileCatalog, tuple[str, ...]]:
    """The profile with each unexpired reading laid over its pool, plus a note per expired one.

    A pool with no reading, or only an expired one, keeps the static
    profile's own status. The profile is never made more available than a
    recorded, unexpired reading says.
    """
    by_key = {(reading.profile_id, reading.pool_id): reading for reading in readings}
    expired: list[str] = []
    profiles = []
    for profile in economics.profiles:
        if not isinstance(profile, FlatRateQuotaProfile):
            profiles.append(profile)
            continue
        pools = tuple(_overlay_pool(profile.profile_id, pool, by_key, now, expired) for pool in profile.allowance_pools)
        profiles.append(replace(profile, allowance_pools=pools))
    return UsageEconomicsProfileCatalog(profiles=tuple(profiles)), tuple(expired)


def _overlay_pool(
    profile_id: str, pool: AllowancePool, by_key: dict[tuple[str, str], PoolReading], now: datetime, expired: list[str],
) -> AllowancePool:
    reading = by_key.get((profile_id, pool.pool_id))
    if reading is None:
        return pool
    if not reading.is_current(now):
        expired.append(f"{profile_id}/{pool.pool_id} reading expired {reading.effective_expiry.isoformat()}")
        return pool
    return replace(
        pool, reading_status="current", consumed=reading.consumed, remaining=reading.remaining,
        next_reset_at=reading.next_reset_at,
    )


__all__ = [
    "MAX_READINGS",
    "PERCENT_NATIVE_UNIT",
    "PoolReading",
    "apply_pool_readings",
    "read_allowance_pool_readings",
    "read_pool_readings",
    "record_allowance_pool_reading",
    "retract_allowance_pool_reading",
]
