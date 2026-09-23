"""Versioned, structural migrations for persisted setup journals."""

from __future__ import annotations

import json
from collections.abc import Callable
from copy import deepcopy
from dataclasses import dataclass
from hashlib import sha256
from typing import Literal

from .errors import StateError
from .models import JsonValue

CURRENT_JOURNAL_VERSION = 3

JOURNAL_KEYS = frozenset(
    {
        "schema_version",
        "operation_id",
        "name",
        "target",
        "input_fingerprint",
        "answers",
        "answers_fingerprint",
        "approval_fingerprint",
        "approval_recorded_at",
        "seed_repository",
        "seed_tag",
        "seed_commit",
        "seed_tree_hash",
        "seed_archive_sha256",
        "profile",
        "flow_id",
        "flow_source_revision",
        "flow_contract_digest",
        "status",
        "stages",
        "stage_probe_statuses",
        "probe_activations",
        "stage_probe_attempts",
        "operation_stages",
        "operation_statuses",
        "operation_attempts",
        "evidence",
        "completion",
        "result_kind",
        "created_at",
        "updated_at",
    }
)
V2_JOURNAL_KEYS = JOURNAL_KEYS - {"probe_activations"}
PROBE_ACTIVATION_KEYS = frozenset({"state", "reason", "decided_by"})
PROBE_ACTIVATION_REASONS = frozenset(
    {
        "plan_bound",
        "decision_selected",
        "decision_unselected",
        "plan_dropped",
        "newly_introduced_pending",
    }
)


def activation_site_key(stage_id: str, boundary: str, probe_ref: str) -> str:
    """Encode the journal's existing stage/boundary/probe address as one JSON key."""

    return f"{stage_id}:{boundary}:{probe_ref}"


OPERATION_ATTEMPT_KEYS = frozenset(
    {
        "operation_id",
        "stage_id",
        "phase",
        "attempt",
        "request_id",
        "checkpoint_status",
        "error_kind",
        "retry_safe",
        "exit_code",
        "timed_out",
        "duration_ms",
        "planned_actions",
        "evidence",
        "reason",
        "repair",
        "recorded_at",
    }
)
LEGACY_OPERATION_ATTEMPT_KEYS = OPERATION_ATTEMPT_KEYS - {
    "exit_code",
    "timed_out",
    "duration_ms",
    "reason",
}
LEGACY_OPERATION_ATTEMPT_DEFAULTS: dict[str, JsonValue] = {
    "exit_code": None,
    "timed_out": False,
    "duration_ms": 0,
    "reason": None,
}
STAGE_PROBE_ATTEMPT_KEYS = frozenset(
    {
        "probe_id",
        "stage_id",
        "boundary",
        "attempt",
        "request_id",
        "checkpoint_status",
        "error_kind",
        "retry_safe",
        "evidence",
        "repair",
        "recorded_at",
    }
)
JOURNAL_BOUNDARY_KEYS = frozenset({"entry", "exit"})

type JournalValuePolicy = Literal["value_preserving", "value_changing"]
type JournalValidator = Callable[[dict[str, JsonValue]], None]
type JournalTransform = Callable[[dict[str, JsonValue]], dict[str, JsonValue]]


@dataclass(frozen=True)
class JournalMigration:
    """One exact, consecutive structural journal-generation upgrade."""

    from_version: int
    to_version: int
    name: str
    value_policy: JournalValuePolicy
    validate_source: JournalValidator
    transform: JournalTransform
    validate_target: JournalValidator


def journal_shape_fingerprint() -> str:
    """Return the pinned closed-shape surface for persisted journal bytes.

    The boundary keys are recorded independently for the journal parser and
    stage-activation validator because both sites enforce the same container.
    """

    surface = {
        "journal_boundary_keys": sorted(JOURNAL_BOUNDARY_KEYS),
        "operation_attempt_keys": sorted(OPERATION_ATTEMPT_KEYS),
        "stage_activation_boundary_keys": sorted(JOURNAL_BOUNDARY_KEYS),
        "stage_probe_attempt_keys": sorted(STAGE_PROBE_ATTEMPT_KEYS),
        "probe_activation_keys": sorted(PROBE_ACTIVATION_KEYS),
        "probe_activation_reasons": sorted(PROBE_ACTIVATION_REASONS),
        "transaction_keys": sorted(JOURNAL_KEYS),
    }
    encoded = json.dumps(surface, separators=(",", ":"), sort_keys=True).encode()
    return sha256(encoded).hexdigest()


def validate_journal_migration_registry() -> None:
    """Refuse an incomplete or non-consecutive migration registry."""

    if not JOURNAL_MIGRATIONS:
        raise StateError("journal migration registry is empty")
    expected_source = JOURNAL_MIGRATIONS[0].from_version
    for migration in JOURNAL_MIGRATIONS:
        if migration.from_version != expected_source:
            raise StateError("journal migration registry has a missing edge")
        if migration.to_version != migration.from_version + 1:
            raise StateError("journal migration edges must be consecutive")
        expected_source = migration.to_version
    if expected_source != CURRENT_JOURNAL_VERSION:
        raise StateError("journal migration registry does not reach current version")


def migrate_journal(raw: dict[str, JsonValue]) -> dict[str, JsonValue]:
    """Return a fresh current-generation journal or fail before parsing it."""

    version = _journal_version(raw)
    if version > CURRENT_JOURNAL_VERSION:
        raise StateError(
            "unsupported journal version: "
            f"{version}; this manager supports through {CURRENT_JOURNAL_VERSION}"
        )
    validate_journal_migration_registry()
    value = deepcopy(raw)
    migrations = {migration.from_version: migration for migration in JOURNAL_MIGRATIONS}
    while version < CURRENT_JOURNAL_VERSION:
        migration = migrations.get(version)
        if migration is None:
            raise StateError(f"journal migration unavailable from version {version}")
        migration.validate_source(value)
        original_answers = deepcopy(value["answers"])
        migrated = migration.transform(value)
        if migrated.get("answers") != original_answers:
            raise StateError(f"journal migration {migration.name} changed answers")
        migration.validate_target(migrated)
        value = migrated
        version = migration.to_version
    _validate_v3(value)
    return value


def _journal_version(raw: dict[str, JsonValue]) -> int:
    version = raw.get("schema_version")
    if isinstance(version, bool) or not isinstance(version, int) or version < 1:
        raise StateError("journal schema_version must be a positive integer")
    return version


def _validate_exact_root(raw: dict[str, JsonValue], version: int, keys: frozenset[str]) -> None:
    if frozenset(raw) != keys or raw.get("schema_version") != version:
        raise StateError(f"journal does not match the closed v{version} shape")


def _validate_v1(raw: dict[str, JsonValue]) -> None:
    _validate_exact_root(raw, 1, V2_JOURNAL_KEYS)
    attempts = raw["operation_attempts"]
    if not isinstance(attempts, list):
        raise StateError("v1 operation_attempts must be an object array")
    for attempt in attempts:
        if not isinstance(attempt, dict) or frozenset(attempt) not in {
            LEGACY_OPERATION_ATTEMPT_KEYS,
            OPERATION_ATTEMPT_KEYS,
        }:
            raise StateError("v1 operation attempt does not match a supported exact shape")


def _validate_v2(raw: dict[str, JsonValue]) -> None:
    _validate_exact_root(raw, 2, V2_JOURNAL_KEYS)
    attempts = raw["operation_attempts"]
    if not isinstance(attempts, list):
        raise StateError("v2 operation_attempts must be an object array")
    if not all(
        isinstance(attempt, dict) and frozenset(attempt) == OPERATION_ATTEMPT_KEYS
        for attempt in attempts
    ):
        raise StateError("v2 operation attempt does not match the closed shape")


def _validate_v3(raw: dict[str, JsonValue]) -> None:
    _validate_exact_root(raw, 3, JOURNAL_KEYS)
    v2_shape = {key: value for key, value in raw.items() if key != "probe_activations"}
    v2_shape["schema_version"] = 2
    _validate_v2(v2_shape)
    _validate_v3_statuses(raw["stage_probe_statuses"])
    _validate_v3_activations(raw["probe_activations"])


def _validate_v3_statuses(value: JsonValue) -> None:
    if not isinstance(value, dict):
        raise StateError("v3 stage_probe_statuses must be an object")
    if any(
        status == "not_applicable"
        for boundaries in value.values()
        if isinstance(boundaries, dict)
        for probes in boundaries.values()
        if isinstance(probes, dict)
        for status in probes.values()
    ):
        raise StateError("v3 stage probe statuses cannot store not_applicable")


def _validate_v3_activations(value: JsonValue) -> None:
    if not isinstance(value, dict):
        raise StateError("v3 probe_activations must be an object")
    for probe_ref, activation in value.items():
        _validate_v3_activation(probe_ref, activation)


def _validate_v3_activation(probe_ref: str, activation: JsonValue) -> None:
    if not isinstance(activation, dict):
        raise StateError("v3 probe activation is malformed")
    if frozenset(activation) != PROBE_ACTIVATION_KEYS:
        raise StateError("v3 probe activation does not match the closed shape")
    state = activation.get("state")
    reason = activation.get("reason")
    decided_by = activation.get("decided_by")
    if state not in {"active", "inactive"} or reason not in PROBE_ACTIVATION_REASONS:
        raise StateError("v3 probe activation has an invalid state or reason")
    if not isinstance(decided_by, str) or not decided_by:
        raise StateError("v3 probe activation decided_by must be a non-empty string")
    if (state == "inactive") != (reason in {"decision_unselected", "plan_dropped"}):
        raise StateError("v3 probe activation state/reason pairing is invalid")


def _migrate_v1_to_v2(raw: dict[str, JsonValue]) -> dict[str, JsonValue]:
    """Add only the four absent diagnostics from the known pre-r12 shape."""

    migrated = deepcopy(raw)
    attempts = migrated["operation_attempts"]
    if not isinstance(attempts, list):
        raise StateError("v1 operation_attempts must be an object array")
    migrated["operation_attempts"] = [
        {**attempt, **LEGACY_OPERATION_ATTEMPT_DEFAULTS}
        if isinstance(attempt, dict) and frozenset(attempt) == LEGACY_OPERATION_ATTEMPT_KEYS
        else attempt
        for attempt in attempts
    ]
    migrated["schema_version"] = 2
    return migrated


def _migrate_v2_to_v3(raw: dict[str, JsonValue]) -> dict[str, JsonValue]:
    """Separate legacy stored inactivity from its v3 activation carrier."""

    migrated = deepcopy(raw)
    statuses = _v2_statuses(migrated["stage_probe_statuses"])
    migrated["probe_activations"] = _migrate_v2_probe_activations(statuses)
    migrated["schema_version"] = 3
    return migrated


def _v2_statuses(value: JsonValue) -> dict[str, JsonValue]:
    if not isinstance(value, dict):
        raise StateError("v2 stage_probe_statuses must be an object")
    return value


def _migrate_v2_probe_activations(
    statuses: dict[str, JsonValue],
) -> dict[str, JsonValue]:
    activations: dict[str, JsonValue] = {}
    for stage_id, boundaries in statuses.items():
        if not isinstance(boundaries, dict):
            raise StateError("v2 stage probe boundaries must be an object")
        for boundary, probes in boundaries.items():
            activations.update(_migrate_v2_boundary(stage_id, boundary, probes))
    return activations


def _migrate_v2_boundary(
    stage_id: str,
    boundary: str,
    value: JsonValue,
) -> dict[str, JsonValue]:
    if not isinstance(value, dict):
        raise StateError("v2 stage probe statuses must be an object")
    activations: dict[str, JsonValue] = {}
    for probe_ref, status in value.items():
        if not isinstance(status, str):
            raise StateError("v2 stage probe status is malformed")
        active = status != "not_applicable"
        activations[activation_site_key(stage_id, boundary, probe_ref)] = {
            "state": "active" if active else "inactive",
            "reason": "plan_bound" if active else "plan_dropped",
            "decided_by": "plan",
        }
        if not active:
            value[probe_ref] = "pending"
    return activations


JOURNAL_MIGRATIONS: tuple[JournalMigration, ...] = (
    JournalMigration(
        from_version=1,
        to_version=2,
        name="normalize_pre_r12_operation_attempt_diagnostics",
        value_policy="value_preserving",
        validate_source=_validate_v1,
        transform=_migrate_v1_to_v2,
        validate_target=_validate_v2,
    ),
    JournalMigration(
        from_version=2,
        to_version=3,
        name="separate_probe_activation_from_effective_status",
        value_policy="value_changing",
        validate_source=_validate_v2,
        transform=_migrate_v2_to_v3,
        validate_target=_validate_v3,
    ),
)
