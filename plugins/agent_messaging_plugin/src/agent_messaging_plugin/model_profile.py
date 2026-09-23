"""Strict, declarative provider/model capability profiles.

Capability data deliberately excludes prices and action-cost calibration.  The
rotation caller supplies the active runtime-effective ceiling separately; a
provider maximum is provenance, not permission to overrun a smaller runtime.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any


class ProfileValidationError(ValueError):
    """A capability catalog cannot safely answer a model question."""


class UnsupportedEffortError(ProfileValidationError):
    """The selected effort is not supported by the resolved model."""


def _aware_datetime(raw: object, field: str) -> datetime:
    if not isinstance(raw, str) or not raw:
        raise ProfileValidationError(f"{field} must be a non-empty timestamp")
    try:
        value = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ProfileValidationError(f"{field} is not an ISO-8601 timestamp: {raw!r}") from exc
    if value.tzinfo is None:
        raise ProfileValidationError(f"{field} must be timezone-aware")
    return value


def _non_empty_string(raw: object, field: str) -> str:
    if not isinstance(raw, str) or not raw.strip():
        raise ProfileValidationError(f"{field} must be a non-empty string")
    return raw.strip()


def _positive_int(raw: object, field: str) -> int:
    if not isinstance(raw, int) or isinstance(raw, bool) or raw <= 0:
        raise ProfileValidationError(f"non-positive {field}: {raw!r}")
    return raw


def _string_tuple(raw: object, field: str) -> tuple[str, ...]:
    if not isinstance(raw, list) or not raw:
        raise ProfileValidationError(f"{field} must be a non-empty list")
    values = tuple(_non_empty_string(value, field) for value in raw)
    if len(values) != len(set(values)):
        raise ProfileValidationError(f"{field} contains duplicates")
    return values


@dataclass(frozen=True)
class ModelCapabilityProfile:
    """One provider/runtime model row resolved from versioned data."""

    profile_version: str
    provider: str
    runtime: str
    canonical_model_id: str
    aliases: tuple[str, ...]
    context_ceiling: int
    max_output_tokens: int
    supported_efforts: tuple[str, ...]
    effort_order: tuple[str, ...]
    default_effort: str
    supported_usage_counters: tuple[str, ...]
    compaction_observability: str
    clear_observability: str
    effective_at: datetime
    source_url: str
    fetched_at: datetime
    refresh_status: str

    @property
    def provider_context_ceiling(self) -> int:
        """Explicit name for callers comparing it with runtime capacity."""
        return self.context_ceiling

    def require_effort(self, effort: str) -> str:
        if effort not in self.supported_efforts:
            raise UnsupportedEffortError(
                f"{self.canonical_model_id} does not support effort {effort!r}; "
                f"supported efforts are {', '.join(self.supported_efforts)}",
            )
        return effort

    def effort_rank(self, effort: str) -> int:
        self.require_effort(effort)
        return self.effort_order.index(effort)


@dataclass(frozen=True)
class ModelProfileCatalog:
    """Alias index over validated model capability rows."""

    profiles: tuple[ModelCapabilityProfile, ...]

    def resolve(self, provider: str, runtime: str, model: str) -> ModelCapabilityProfile:
        matches = [
            row
            for row in self.profiles
            if row.provider == provider
            and row.runtime == runtime
            and (model == row.canonical_model_id or model in row.aliases)
        ]
        if len(matches) != 1:
            raise ProfileValidationError(
                f"no unique current capability profile for provider={provider!r}, "
                f"runtime={runtime!r}, model={model!r}; refresh model_profiles",
            )
        return matches[0]


def _model_row(
    raw: object,
    *,
    provider: str,
    runtime: str,
    profile_version: str,
    effort_order: tuple[str, ...],
    effective_at: datetime,
    source_url: str,
    fetched_at: datetime,
    refresh_status: str,
) -> ModelCapabilityProfile:
    if not isinstance(raw, dict):
        raise ProfileValidationError("models entries must be objects")
    canonical = _non_empty_string(raw.get("canonical_model_id"), "canonical_model_id")
    aliases = _string_tuple(raw.get("aliases"), f"{canonical}.aliases")
    supported = _string_tuple(raw.get("supported_efforts"), f"{canonical}.supported_efforts")
    unknown_efforts = sorted(set(supported) - set(effort_order))
    if unknown_efforts:
        raise ProfileValidationError(
            f"{canonical}.supported_efforts are absent from effort_order: {unknown_efforts}",
        )
    default_effort = _non_empty_string(raw.get("default_effort"), f"{canonical}.default_effort")
    if default_effort not in supported:
        raise ProfileValidationError(
            f"{canonical}.default_effort {default_effort!r} is unsupported",
        )
    return ModelCapabilityProfile(
        profile_version=profile_version,
        provider=provider,
        runtime=runtime,
        canonical_model_id=canonical,
        aliases=aliases,
        context_ceiling=_positive_int(raw.get("context_ceiling"), "context_ceiling"),
        max_output_tokens=_positive_int(raw.get("max_output_tokens"), "max_output_tokens"),
        supported_efforts=supported,
        effort_order=effort_order,
        default_effort=default_effort,
        supported_usage_counters=_string_tuple(
            raw.get("supported_usage_counters"),
            f"{canonical}.supported_usage_counters",
        ),
        compaction_observability=_non_empty_string(
            raw.get("compaction_observability"),
            f"{canonical}.compaction_observability",
        ),
        clear_observability=_non_empty_string(
            raw.get("clear_observability"),
            f"{canonical}.clear_observability",
        ),
        effective_at=effective_at,
        source_url=source_url,
        fetched_at=fetched_at,
        refresh_status=refresh_status,
    )


def _profile_root(path: Path) -> dict[str, Any]:
    try:
        raw: Any = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ProfileValidationError(f"cannot load capability profile {path}: {exc}") from exc
    if not isinstance(raw, dict):
        raise ProfileValidationError("capability profile root must be an object")
    if raw.get("schema_version") != 1:
        raise ProfileValidationError(f"unsupported schema_version: {raw.get('schema_version')!r}")
    return raw


def _profile_provenance(raw: dict[str, Any]) -> tuple[str, datetime, str]:
    provenance = raw.get("provenance")
    if not isinstance(provenance, dict):
        raise ProfileValidationError("provenance must be an object")
    source_url = _non_empty_string(provenance.get("source_url"), "source_url")
    fetched_at = _aware_datetime(provenance.get("fetched_at"), "fetched_at")
    refresh_status = _non_empty_string(provenance.get("refresh_status"), "refresh_status")
    if refresh_status != "current":
        raise ProfileValidationError(
            f"refresh_status is {refresh_status!r}; refresh model_profiles from {source_url}",
        )
    return source_url, fetched_at, refresh_status


def _unique_profile_aliases(profiles: tuple[ModelCapabilityProfile, ...]) -> None:
    keys = [
        (profile.provider, profile.runtime, alias)
        for profile in profiles
        for alias in (profile.canonical_model_id, *profile.aliases)
    ]
    if len(keys) != len(set(keys)):
        raise ProfileValidationError("canonical_model_id/aliases are not unique")


def load_model_profile_catalog(path: Path, *, as_of: datetime) -> ModelProfileCatalog:
    """Load current capability data, refusing stale/future/provenance-free rows."""
    if as_of.tzinfo is None:
        raise ProfileValidationError("as_of must be timezone-aware")
    raw = _profile_root(path)
    provider = _non_empty_string(raw.get("provider"), "provider")
    runtime = _non_empty_string(raw.get("runtime"), "runtime")
    version = _non_empty_string(raw.get("profile_version"), "profile_version")
    effort_order = _string_tuple(raw.get("effort_order"), "effort_order")
    effective_at = _aware_datetime(raw.get("effective_at"), "effective_at")
    if effective_at > as_of:
        raise ProfileValidationError(f"not effective until {effective_at.isoformat()}")
    source_url, fetched_at, refresh_status = _profile_provenance(raw)
    models = raw.get("models")
    if not isinstance(models, list) or not models:
        raise ProfileValidationError("models must be a non-empty list")
    profiles = tuple(
        _model_row(
            row,
            provider=provider,
            runtime=runtime,
            profile_version=version,
            effort_order=effort_order,
            effective_at=effective_at,
            source_url=source_url,
            fetched_at=fetched_at,
            refresh_status=refresh_status,
        )
        for row in models
    )
    _unique_profile_aliases(profiles)
    return ModelProfileCatalog(profiles=profiles)


__all__ = [
    "ModelCapabilityProfile",
    "ModelProfileCatalog",
    "ProfileValidationError",
    "UnsupportedEffortError",
    "load_model_profile_catalog",
]
