"""Field readers shared by the usage-economics profile parsers (a leaf: imports nothing local)."""

from __future__ import annotations

from datetime import datetime
from typing import Any


class UsageEconomicsProfileValidationError(ValueError):
    """A usage profile cannot support an honest verdict."""


def require_text(raw: object, field: str) -> str:
    if not isinstance(raw, str) or not raw.strip():
        raise UsageEconomicsProfileValidationError(f"{field} must be a non-empty string")
    return raw.strip()


def require_stamp(raw: object, field: str) -> datetime:
    if not isinstance(raw, str) or not raw:
        raise UsageEconomicsProfileValidationError(f"{field} must be a timestamp")
    try:
        value = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as exc:
        raise UsageEconomicsProfileValidationError(f"{field} is invalid: {raw!r}") from exc
    if value.tzinfo is None:
        raise UsageEconomicsProfileValidationError(f"{field} must be timezone-aware")
    return value


def require_number(raw: object, field: str, *, optional: bool = False) -> float | None:
    if raw is None and optional:
        return None
    if not isinstance(raw, (int, float)) or isinstance(raw, bool) or raw < 0:
        raise UsageEconomicsProfileValidationError(f"{field} must be non-negative")
    return float(raw)


def require_strings(raw: object, field: str) -> tuple[str, ...]:
    if not isinstance(raw, list) or not raw:
        raise UsageEconomicsProfileValidationError(f"{field} must be a non-empty list")
    values = tuple(require_text(value, field) for value in raw)
    if len(values) != len(set(values)):
        raise UsageEconomicsProfileValidationError(f"{field} contains duplicates")
    return values


def reject_token_conversion(raw: dict[str, Any]) -> None:
    forbidden = sorted(key for key in raw if "token_conversion" in key)
    if forbidden:
        raise UsageEconomicsProfileValidationError(
            f"invented token conversion is forbidden: {', '.join(forbidden)}",
        )


__all__ = [
    "UsageEconomicsProfileValidationError",
    "reject_token_conversion",
    "require_number",
    "require_stamp",
    "require_strings",
    "require_text",
]
