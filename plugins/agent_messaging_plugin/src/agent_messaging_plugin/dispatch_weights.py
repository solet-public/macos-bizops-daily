"""Operator-declared dispatch weights for a flat-rate plan (iss_eef0812b)."""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from .usage_economics_fields import (
    UsageEconomicsProfileValidationError,
    reject_token_conversion,
    require_stamp,
    require_text,
)

_WEIGHT_BASES = ("operator_policy", "measured")
_WEIGHT_KEYS = frozenset(
    {"basis", "ruling_id", "evidence_ref", "declared_at", "default_weight", "by_model_prefix", "note"},
)


@dataclass(frozen=True)
class DispatchWeights:
    """Operator-declared dispatch preference for a plan's models; never a token or allowance conversion."""

    basis: str
    ruling_id: str | None
    evidence_ref: str | None
    declared_at: datetime
    default_weight: float
    by_model_prefix: tuple[tuple[str, float], ...]
    note: str | None

    def weight_for(self, model: str) -> float:
        """The weight of the longest declared prefix of ``model``, else the declared default."""
        matches = [(prefix, weight) for prefix, weight in self.by_model_prefix if model.startswith(prefix)]
        if not matches:
            return self.default_weight
        return max(matches, key=lambda item: len(item[0]))[1]


def _weight(raw: object, field: str) -> float:
    if not isinstance(raw, (int, float)) or isinstance(raw, bool) or not math.isfinite(raw) or raw <= 0:
        raise UsageEconomicsProfileValidationError(f"{field} must be a finite number above zero")
    return float(raw)


def _prefix_weights(raw: object) -> tuple[tuple[str, float], ...]:
    if not isinstance(raw, dict) or not raw:
        raise UsageEconomicsProfileValidationError("by_model_prefix must be a non-empty object")
    return tuple(
        (require_text(prefix, "by_model_prefix key"), _weight(weight, f"by_model_prefix[{prefix!r}]"))
        for prefix, weight in raw.items()
    )


def _optional_text(raw: dict[str, Any], key: str) -> str | None:
    return None if raw.get(key) is None else require_text(raw[key], f"dispatch_weights.{key}")


def _weight_basis(raw: dict[str, Any]) -> str:
    basis = require_text(raw.get("basis"), "dispatch_weights.basis")
    if basis not in _WEIGHT_BASES:
        raise UsageEconomicsProfileValidationError(f"dispatch_weights.basis must be one of {list(_WEIGHT_BASES)}")
    required = {"operator_policy": "ruling_id", "measured": "evidence_ref"}[basis]
    if raw.get(required) is None:
        raise UsageEconomicsProfileValidationError(f"a {basis} weight table needs a {required}")
    return basis


def parse_dispatch_weights(raw: object) -> DispatchWeights | None:
    """Parse the optional per-plan dispatch weight table; absent is a warning at selection, never a load error."""
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise UsageEconomicsProfileValidationError("dispatch_weights must be an object")
    reject_token_conversion(raw)
    unknown = sorted(set(raw) - _WEIGHT_KEYS)
    if unknown:
        raise UsageEconomicsProfileValidationError(f"dispatch_weights has unknown keys: {', '.join(unknown)}")
    return DispatchWeights(
        basis=_weight_basis(raw),
        ruling_id=_optional_text(raw, "ruling_id"),
        evidence_ref=_optional_text(raw, "evidence_ref"),
        declared_at=require_stamp(raw.get("declared_at"), "dispatch_weights.declared_at"),
        default_weight=_weight(raw.get("default_weight"), "dispatch_weights.default_weight"),
        by_model_prefix=_prefix_weights(raw.get("by_model_prefix")),
        note=_optional_text(raw, "note"),
    )


__all__ = ["DispatchWeights", "parse_dispatch_weights"]
