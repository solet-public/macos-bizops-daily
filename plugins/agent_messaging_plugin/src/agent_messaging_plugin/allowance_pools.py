"""The allowance-pool record and its parser for flat-rate usage profiles."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from .usage_economics_fields import (
    UsageEconomicsProfileValidationError,
    reject_token_conversion,
    require_number,
    require_stamp,
    require_strings,
    require_text,
)


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


def _limit_fields(raw: dict[str, Any]) -> tuple[str, float | None]:
    status = require_text(raw.get("limit_status"), "limit_status")
    if status not in {"known", "unknown", "unbounded"}:
        raise UsageEconomicsProfileValidationError(f"invalid limit_status {status!r}")
    limit = require_number(raw.get("limit"), "limit", optional=True)
    if status == "known" and (limit is None or limit <= 0):
        raise UsageEconomicsProfileValidationError("known limit must be positive")
    if status != "known" and limit is not None:
        raise UsageEconomicsProfileValidationError(f"{status} limit must be null")
    return status, limit


def _reading_fields(
    raw: dict[str, Any], *, as_of: datetime
) -> tuple[str, float | None, float | None, datetime | None]:
    status = require_text(raw.get("reading_status"), "reading_status")
    if status not in {"current", "unknown", "unavailable"}:
        raise UsageEconomicsProfileValidationError(f"stale or invalid reading_status {status!r}")
    consumed, remaining = _reading_counts(raw, status=status)
    reset = _reading_reset(raw, status=status, as_of=as_of)
    return status, consumed, remaining, reset


def _reading_counts(
    raw: dict[str, Any], *, status: str
) -> tuple[float | None, float | None]:
    consumed = require_number(raw.get("consumed"), "consumed", optional=True)
    remaining = require_number(raw.get("remaining"), "remaining", optional=True)
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
    reset = None if raw.get("next_reset_at") is None else require_stamp(
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
    reject_token_conversion(raw)
    limit_status, limit = _limit_fields(raw)
    reading_status, consumed, remaining, reset = _reading_fields(raw, as_of=as_of)
    return AllowancePool(
        pool_id=require_text(raw.get("pool_id"), "pool_id"),
        native_unit=require_text(raw.get("native_unit"), "native_unit"),
        applicable_runtimes=require_strings(raw.get("applicable_runtimes"), "applicable_runtimes"),
        applicable_model_prefixes=require_strings(
            raw.get("applicable_model_prefixes"),
            "applicable_model_prefixes",
        ),
        limit_status=limit_status,
        limit=limit,
        reading_status=reading_status,
        consumed=consumed,
        remaining=remaining,
        reset_kind=require_text(raw.get("reset_kind"), "reset_kind"),
        next_reset_at=reset,
        telemetry_authority=require_text(raw.get("telemetry_authority"), "telemetry_authority"),
    )


def parse_pools(raw: dict[str, Any], *, as_of: datetime) -> tuple[AllowancePool, ...]:
    rows = raw.get("allowance_pools")
    if not isinstance(rows, list) or not rows:
        raise UsageEconomicsProfileValidationError("allowance_pools must be a non-empty list")
    pools = tuple(_pool(pool, as_of=as_of) for pool in rows)
    if len({pool.pool_id for pool in pools}) != len(pools):
        raise UsageEconomicsProfileValidationError("allowance pool ids must be unique")
    return pools


__all__ = ["AllowancePool", "parse_pools"]
