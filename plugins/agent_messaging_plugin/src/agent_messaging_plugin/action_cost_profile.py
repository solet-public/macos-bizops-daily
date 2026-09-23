"""Measured, action-specific clear/compact calibration profiles."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any


class ActionCostProfileValidationError(ValueError):
    """A calibration catalog is structurally unsafe or not current."""


class ActionCalibrationUnavailableError(ActionCostProfileValidationError):
    """No applicable measurement supports the requested action verdict."""


def _text(raw: object, field: str) -> str:
    if not isinstance(raw, str) or not raw.strip():
        raise ActionCostProfileValidationError(f"{field} must be a non-empty string")
    return raw.strip()


def _stamp(raw: object, field: str) -> datetime:
    if not isinstance(raw, str):
        raise ActionCostProfileValidationError(f"{field} must be a timestamp")
    try:
        value = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ActionCostProfileValidationError(f"{field} is invalid: {raw!r}") from exc
    if value.tzinfo is None:
        raise ActionCostProfileValidationError(f"{field} must be timezone-aware")
    return value


@dataclass(frozen=True)
class TokenEstimate:
    """One measured token component with its population and uncertainty."""

    estimate_tokens: int | None
    estimator: str
    sample_count: int
    uncertainty: str
    measurement_population: tuple[str, ...]


@dataclass(frozen=True)
class ActionCostCalibration:
    """One provider/runtime/model/effort/action calibration row."""

    profile_version: str
    provider: str
    runtime: str
    model: str
    effort: str
    action: str
    boot_prefix: TokenEstimate
    rehydration_prefix: TokenEstimate
    post_action_prefix: TokenEstimate
    measured_at: datetime
    applicability_limits: str
    cache_read_multiplier: float | None
    cache_write_multiplier: float | None
    priced: bool
    source_ref: str

    @property
    def post_action_prefix_tokens(self) -> int | None:
        return self.post_action_prefix.estimate_tokens

    def require_priced(self) -> ActionCostCalibration:
        if not self.priced:
            raise ActionCalibrationUnavailableError(
                f"{self.action} action cost is not measured for "
                f"{self.runtime}/{self.model}/{self.effort}: {self.applicability_limits}",
            )
        _validate_priced_calibration(self)
        return self


@dataclass(frozen=True)
class ActionCostProfileCatalog:
    profiles: tuple[ActionCostCalibration, ...]

    def resolve(
        self,
        provider: str,
        runtime: str,
        model: str,
        effort: str,
        action: str,
    ) -> ActionCostCalibration:
        matches = [
            row
            for row in self.profiles
            if (row.provider, row.runtime, row.model, row.effort, row.action)
            == (provider, runtime, model, effort, action)
        ]
        if len(matches) != 1:
            raise ActionCalibrationUnavailableError(
                f"no action calibration for {provider}/{runtime}/{model}/{effort}/{action}",
            )
        return matches[0]


def _estimate_tokens(raw: dict[str, Any], field: str) -> int | None:
    estimate = raw.get("estimate_tokens")
    if estimate is not None and (
        not isinstance(estimate, int) or isinstance(estimate, bool) or estimate < 0
    ):
        raise ActionCostProfileValidationError(f"{field}.estimate_tokens must be non-negative or null")
    return estimate


def _measurement_population(
    raw: dict[str, Any], field: str
) -> tuple[int, tuple[str, ...]]:
    sample_count = raw.get("sample_count")
    if not isinstance(sample_count, int) or isinstance(sample_count, bool) or sample_count < 0:
        raise ActionCostProfileValidationError(f"{field}.sample_count must be non-negative")
    population_raw = raw.get("measurement_population")
    if not isinstance(population_raw, list):
        raise ActionCostProfileValidationError(f"{field}.measurement_population must be a list")
    population = tuple(_text(value, f"{field}.measurement_population") for value in population_raw)
    if sample_count != len(population):
        raise ActionCostProfileValidationError(
            f"{field}.sample_count={sample_count} does not match measurement_population={len(population)}",
        )
    return sample_count, population


def _estimate(raw: object, field: str) -> TokenEstimate:
    if not isinstance(raw, dict):
        raise ActionCostProfileValidationError(f"{field} must be an object")
    estimate = _estimate_tokens(raw, field)
    sample_count, population = _measurement_population(raw, field)
    if (estimate is None) != (sample_count == 0):
        raise ActionCostProfileValidationError(
            f"{field} estimate/sample_count must both be measured or both unavailable",
        )
    return TokenEstimate(
        estimate_tokens=estimate,
        estimator=_text(raw.get("estimator"), f"{field}.estimator"),
        sample_count=sample_count,
        uncertainty=_text(raw.get("uncertainty"), f"{field}.uncertainty"),
        measurement_population=population,
    )


def _optional_multiplier(raw: object, field: str) -> float | None:
    if raw is None:
        return None
    if not isinstance(raw, (int, float)) or isinstance(raw, bool) or raw < 0:
        raise ActionCostProfileValidationError(f"{field} must be non-negative or null")
    return float(raw)


def _missing_priced_measurements(calibration: ActionCostCalibration) -> list[str]:
    estimates = (
        ("boot_prefix", calibration.boot_prefix),
        ("rehydration_prefix", calibration.rehydration_prefix),
        ("action_prefix", calibration.post_action_prefix),
    )
    return [
        name
        for name, estimate in estimates
        if estimate.estimate_tokens is None
        or estimate.sample_count <= 0
        or not estimate.measurement_population
    ]


def _require_priced_normalization(calibration: ActionCostCalibration) -> None:
    multipliers = (
        calibration.cache_read_multiplier,
        calibration.cache_write_multiplier,
    )
    if all(value is not None and value > 0 for value in multipliers):
        return
    raise ActionCostProfileValidationError(
        "priced calibration requires positive cache_read_multiplier and "
        "cache_write_multiplier normalization inputs",
    )


def _require_consistent_priced_prefix(calibration: ActionCostCalibration) -> None:
    boot = calibration.boot_prefix.estimate_tokens
    rehydration = calibration.rehydration_prefix.estimate_tokens
    action_prefix = calibration.post_action_prefix.estimate_tokens
    assert boot is not None and rehydration is not None and action_prefix is not None
    if action_prefix == boot + rehydration:
        return
    raise ActionCostProfileValidationError(
        "priced action_prefix must equal boot_prefix + rehydration_prefix; "
        f"got {action_prefix} != {boot} + {rehydration}",
    )


def _validate_priced_calibration(calibration: ActionCostCalibration) -> None:
    missing = _missing_priced_measurements(calibration)
    if missing:
        raise ActionCostProfileValidationError(
            "priced calibration requires measured boot_prefix, rehydration_prefix, "
            "action_prefix with non-empty populations; missing " + ", ".join(missing),
        )
    _require_priced_normalization(calibration)
    _require_consistent_priced_prefix(calibration)


def _profile(
    raw: object,
    *,
    version: str,
    source_ref: str,
    as_of: datetime,
) -> ActionCostCalibration:
    if not isinstance(raw, dict):
        raise ActionCostProfileValidationError("profiles entries must be objects")
    priced = raw.get("priced")
    if not isinstance(priced, bool):
        raise ActionCostProfileValidationError("priced must be a boolean")
    measured_at = _stamp(raw.get("measured_at"), "measured_at")
    if measured_at > as_of:
        raise ActionCostProfileValidationError(
            f"measurement is from the future: {measured_at.isoformat()}",
        )
    calibration = ActionCostCalibration(
        profile_version=version,
        provider=_text(raw.get("provider"), "provider"),
        runtime=_text(raw.get("runtime"), "runtime"),
        model=_text(raw.get("model"), "model"),
        effort=_text(raw.get("effort"), "effort"),
        action=_text(raw.get("action"), "action"),
        boot_prefix=_estimate(raw.get("boot_prefix"), "boot_prefix"),
        rehydration_prefix=_estimate(raw.get("rehydration_prefix"), "rehydration_prefix"),
        post_action_prefix=_estimate(raw.get("action_prefix"), "action_prefix"),
        measured_at=measured_at,
        applicability_limits=_text(raw.get("applicability_limits"), "applicability_limits"),
        cache_read_multiplier=_optional_multiplier(
            raw.get("cache_read_multiplier"),
            "cache_read_multiplier",
        ),
        cache_write_multiplier=_optional_multiplier(
            raw.get("cache_write_multiplier"),
            "cache_write_multiplier",
        ),
        priced=priced,
        source_ref=source_ref,
    )
    if calibration.priced:
        _validate_priced_calibration(calibration)
    return calibration


def _profile_root(path: Path) -> dict[str, Any]:
    try:
        raw: Any = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ActionCostProfileValidationError(f"cannot load action profile {path}: {exc}") from exc
    if not isinstance(raw, dict) or raw.get("schema_version") != 1:
        raise ActionCostProfileValidationError("unsupported schema_version")
    return raw


def _profile_header(
    raw: dict[str, Any], *, as_of: datetime
) -> tuple[str, str]:
    version = _text(raw.get("profile_version"), "profile_version")
    effective = _stamp(raw.get("effective_at"), "effective_at")
    if effective > as_of:
        raise ActionCostProfileValidationError(f"not effective until {effective.isoformat()}")
    provenance = raw.get("provenance")
    if not isinstance(provenance, dict):
        raise ActionCostProfileValidationError("provenance must be an object")
    source_ref = _text(provenance.get("source_ref"), "source_ref")
    refresh_status = _text(provenance.get("refresh_status"), "refresh_status")
    if refresh_status != "current":
        raise ActionCostProfileValidationError(f"refresh_status is {refresh_status!r}")
    return version, source_ref


def load_action_cost_profile_catalog(
    path: Path,
    *,
    as_of: datetime,
) -> ActionCostProfileCatalog:
    """Load calibration rows, refusing stale or provenance-free data."""
    if as_of.tzinfo is None:
        raise ActionCostProfileValidationError("as_of must be timezone-aware")
    raw = _profile_root(path)
    version, source_ref = _profile_header(raw, as_of=as_of)
    rows = raw.get("profiles")
    if not isinstance(rows, list) or not rows:
        raise ActionCostProfileValidationError("profiles must be a non-empty list")
    profiles = tuple(
        _profile(row, version=version, source_ref=source_ref, as_of=as_of)
        for row in rows
    )
    keys = [(row.provider, row.runtime, row.model, row.effort, row.action) for row in profiles]
    if len(keys) != len(set(keys)):
        raise ActionCostProfileValidationError("calibration scope keys must be unique")
    return ActionCostProfileCatalog(profiles=profiles)


__all__ = [
    "ActionCalibrationUnavailableError",
    "ActionCostCalibration",
    "ActionCostProfileCatalog",
    "ActionCostProfileValidationError",
    "TokenEstimate",
    "load_action_cost_profile_catalog",
]
