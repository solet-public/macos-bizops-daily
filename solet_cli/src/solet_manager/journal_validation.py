"""Closed journal parsing and consistency validation."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Protocol, cast

from .adapter_validation import boolean, bounded_integer, optional_int, validate_reason
from .errors import OperationAttemptMismatch, StateError
from .journal_migrations import (
    CURRENT_JOURNAL_VERSION,
    JOURNAL_BOUNDARY_KEYS,
    JOURNAL_KEYS,
    OPERATION_ATTEMPT_KEYS,
    STAGE_PROBE_ATTEMPT_KEYS,
    activation_site_key,
    migrate_journal,
)
from .journal_rollup import StageProbeStatuses, derive_stage_statuses, roll_up_transaction
from .models import CheckpointStatus, JsonValue, TransactionStatus
from .operation_attempt_validation import validate_operation_attempt_coverage
from .release_lock import SeedLock

TRANSACTION_KEYS = JOURNAL_KEYS


@dataclass(frozen=True)
class ParsedJournal:
    operation_id: str
    name: str
    target: str
    input_fingerprint: str
    answers: dict[str, JsonValue]
    answers_fingerprint: str
    approval_fingerprint: str | None
    approval_recorded_at: str | None
    seed: SeedLock
    flow_id: str
    flow_source_revision: str
    flow_contract_digest: str
    status: TransactionStatus
    stages: dict[str, CheckpointStatus]
    stage_probe_statuses: StageProbeStatuses
    probe_activations: dict[str, dict[str, str]]
    stage_probe_attempts: tuple[dict[str, JsonValue], ...]
    operation_stages: dict[str, str]
    operation_statuses: dict[str, CheckpointStatus]
    operation_attempts: tuple[dict[str, JsonValue], ...]
    evidence: tuple[dict[str, JsonValue], ...]
    completion: dict[str, CheckpointStatus]
    result_kind: str | None
    created_at: str
    updated_at: str


class JournalTransaction(Protocol):
    """Read-only fields used to validate a decoded journal."""

    @property
    def answers_fingerprint(self) -> str: ...

    @property
    def operation_stages(self) -> dict[str, str]: ...

    @property
    def operation_statuses(self) -> dict[str, CheckpointStatus]: ...

    @property
    def operation_attempts(self) -> tuple[dict[str, JsonValue], ...]: ...

    @property
    def stage_probe_statuses(self) -> StageProbeStatuses: ...

    @property
    def probe_activations(self) -> dict[str, dict[str, str]]: ...

    @property
    def stage_probe_attempts(self) -> tuple[dict[str, JsonValue], ...]: ...

    @property
    def stages(self) -> dict[str, CheckpointStatus]: ...

    @property
    def completion(self) -> dict[str, CheckpointStatus]: ...

    @property
    def result_kind(self) -> str | None: ...

    @property
    def status(self) -> TransactionStatus: ...


def parse_transaction_fields(raw: dict[str, JsonValue]) -> ParsedJournal:
    normalized = migrate_journal(raw)
    if (
        frozenset(normalized) != TRANSACTION_KEYS
        or normalized.get("schema_version") != CURRENT_JOURNAL_VERSION
    ):
        raise StateError("transaction does not match the closed current journal schema")
    try:
        return _parse_transaction_fields(normalized)
    except (KeyError, TypeError, ValueError) as exc:
        raise StateError(f"transaction fields are invalid: {exc}") from exc


def _parse_transaction_fields(raw: dict[str, JsonValue]) -> ParsedJournal:
    answers = raw["answers"]
    if not isinstance(answers, dict):
        raise TypeError("answers is not an object")
    stages = _status_dict(raw["stages"], "stages")
    completion = _status_dict(raw["completion"], "completion")
    operation_statuses = _status_dict(raw["operation_statuses"], "operation_statuses")
    return ParsedJournal(
        operation_id=str(raw["operation_id"]),
        name=str(raw["name"]),
        target=str(raw["target"]),
        input_fingerprint=str(raw["input_fingerprint"]),
        answers=cast(dict[str, JsonValue], answers),
        answers_fingerprint=str(raw["answers_fingerprint"]),
        approval_fingerprint=_optional_string(raw["approval_fingerprint"]),
        approval_recorded_at=_optional_string(raw["approval_recorded_at"]),
        seed=_parse_seed(raw),
        flow_id=str(raw["flow_id"]),
        flow_source_revision=str(raw["flow_source_revision"]),
        flow_contract_digest=str(raw["flow_contract_digest"]),
        status=TransactionStatus(str(raw["status"])),
        stages=stages,
        stage_probe_statuses=_stage_probe_statuses(
            raw["stage_probe_statuses"], "stage_probe_statuses"
        ),
        probe_activations=_probe_activations(raw["probe_activations"]),
        stage_probe_attempts=_object_tuple(raw["stage_probe_attempts"], "stage_probe_attempts"),
        operation_stages=_string_dict(raw["operation_stages"], "operation_stages"),
        operation_statuses=operation_statuses,
        operation_attempts=_object_tuple(raw["operation_attempts"], "operation_attempts"),
        evidence=_object_tuple(raw["evidence"], "evidence"),
        completion=completion,
        result_kind=_optional_string(raw["result_kind"]),
        created_at=str(raw["created_at"]),
        updated_at=str(raw["updated_at"]),
    )


def _parse_seed(raw: dict[str, JsonValue]) -> SeedLock:
    return SeedLock(
        repository=str(raw["seed_repository"]),
        release_tag=_optional_string(raw["seed_tag"]),
        commit=str(raw["seed_commit"]),
        tree_hash=str(raw["seed_tree_hash"]),
        archive_sha256=_optional_string(raw["seed_archive_sha256"]),
        profile=str(raw["profile"]),
    )


def validate_transaction_state(
    transaction: JournalTransaction,
    *,
    canonical_answers_fingerprint: str,
    allow_operation_attempt_mismatch: bool = False,
) -> OperationAttemptMismatch | None:
    _validate_transaction_links(transaction, canonical_answers_fingerprint)
    mismatch = validate_operation_attempts(transaction)
    latest = validate_stage_attempts(transaction.stage_probe_attempts)
    _validate_stage_attempt_coverage(
        transaction.stage_probe_statuses, transaction.probe_activations, latest
    )
    _validate_derived_state(transaction)
    if mismatch is not None and not allow_operation_attempt_mismatch:
        raise mismatch
    return mismatch


def _validate_transaction_links(
    transaction: JournalTransaction,
    canonical_answers_fingerprint: str,
) -> None:
    if canonical_answers_fingerprint != transaction.answers_fingerprint:
        raise StateError("transaction answers_fingerprint does not match its answers")
    if set(transaction.operation_stages) != set(transaction.operation_statuses):
        raise StateError("transaction operation stage/status ids differ")
    if set(transaction.stage_probe_statuses) != set(transaction.stages):
        raise StateError("transaction stage probe/status ids differ")
    _validate_probe_activations(transaction.stage_probe_statuses, transaction.probe_activations)


def validate_operation_attempts(
    transaction: JournalTransaction,
) -> OperationAttemptMismatch | None:
    latest: dict[str, dict[str, JsonValue]] = {}
    for attempt in transaction.operation_attempts:
        operation_id, stage_id, status = _operation_attempt_identity(attempt)
        expected_stage = _expected_attempt_stage(transaction, operation_id)
        # An attempt whose operation is no longer bound is retained HISTORY, not
        # a mismatch: operation_attempts records what ran, operation_stages
        # records what is currently planned, and re-scoping the plan drops
        # bindings as a normal act.  Only a still-bound operation can contradict
        # its own attempt, so only that case is corruption.
        if expected_stage is not None and expected_stage != stage_id:
            raise StateError(f"transaction attempt stage mismatch for {operation_id!r}")
        validate_attempt(attempt, operation_id, stage_id, status)
        if operation_id in transaction.operation_statuses:
            latest[operation_id] = attempt
    return validate_operation_attempt_coverage(transaction, latest)


def _operation_attempt_identity(
    attempt: dict[str, JsonValue],
) -> tuple[str, str, CheckpointStatus]:
    operation_id = attempt.get("operation_id")
    stage_id = attempt.get("stage_id")
    status = attempt.get("checkpoint_status")
    if not isinstance(operation_id, str):
        raise StateError("transaction operation attempt identity is invalid")
    if not isinstance(stage_id, str) or not isinstance(status, str):
        raise StateError("transaction operation attempt identity is invalid")
    return operation_id, stage_id, CheckpointStatus(status)


def _expected_attempt_stage(
    transaction: JournalTransaction,
    operation_id: str,
) -> str | None:
    if operation_id in transaction.completion:
        return "completion"
    return transaction.operation_stages.get(operation_id)


def validate_stage_attempts(
    attempts: tuple[dict[str, JsonValue], ...],
) -> dict[tuple[str, str, str], dict[str, JsonValue]]:
    identities: set[tuple[str, str, str, int]] = set()
    latest: dict[tuple[str, str, str], dict[str, JsonValue]] = {}
    for attempt in attempts:
        stage_id, boundary, probe_id, number, status = _stage_attempt_identity(attempt)
        validate_stage_probe_attempt(
            attempt,
            stage_id=stage_id,
            boundary=boundary,
            probe_id=probe_id,
            status=status,
        )
        identity = (stage_id, boundary, probe_id, number)
        if identity in identities:
            raise StateError(f"duplicate stage probe attempt identity: {identity}")
        identities.add(identity)
        latest[(stage_id, boundary, probe_id)] = attempt
    return latest


def _stage_attempt_identity(
    attempt: dict[str, JsonValue],
) -> tuple[str, str, str, int, CheckpointStatus]:
    probe_id = attempt.get("probe_id")
    stage_id = attempt.get("stage_id")
    boundary = attempt.get("boundary")
    number = attempt.get("attempt")
    status = attempt.get("checkpoint_status")
    strings = (probe_id, stage_id, boundary, status)
    if not all(isinstance(value, str) for value in strings):
        raise StateError("transaction stage probe attempt identity is invalid")
    if isinstance(number, bool) or not isinstance(number, int):
        raise StateError("transaction stage probe attempt identity is invalid")
    return (
        cast(str, stage_id),
        cast(str, boundary),
        cast(str, probe_id),
        number,
        CheckpointStatus(cast(str, status)),
    )


def _validate_stage_attempt_coverage(
    statuses: StageProbeStatuses,
    activations: dict[str, dict[str, str]],
    latest: dict[tuple[str, str, str], dict[str, JsonValue]],
) -> None:
    for identity, attempt in latest.items():
        current = _declared_probe_status(statuses, identity)
        stage_id, boundary, probe_id = identity
        if (
            current is CheckpointStatus.NOT_APPLICABLE
            or activations[activation_site_key(stage_id, boundary, probe_id)]["state"] == "inactive"
        ):
            # The status map is the current-state authority. A declared contract
            # reconciliation can supersede a historical attempt to first-use
            # inactivity while retaining that attempt record unchanged.
            continue
        if attempt.get("checkpoint_status") != current.value:
            raise StateError(f"stage probe status disagrees with latest attempt: {identity}")
    for stage_id, boundaries in statuses.items():
        for boundary, probes in boundaries.items():
            _require_attempts_for_recorded_statuses(stage_id, boundary, probes, latest)


def _declared_probe_status(
    statuses: StageProbeStatuses,
    identity: tuple[str, str, str],
) -> CheckpointStatus:
    stage_id, boundary, probe_id = identity
    try:
        return statuses[stage_id][boundary][probe_id]
    except KeyError as exc:
        raise StateError(
            f"stage probe attempt references undeclared boundary member: {identity}"
        ) from exc


def _require_attempts_for_recorded_statuses(
    stage_id: str,
    boundary: str,
    probes: dict[str, CheckpointStatus],
    latest: dict[tuple[str, str, str], dict[str, JsonValue]],
) -> None:
    no_attempt = {CheckpointStatus.PENDING, CheckpointStatus.NOT_APPLICABLE}
    for probe_id, current in probes.items():
        identity = (stage_id, boundary, probe_id)
        if current not in no_attempt and identity not in latest:
            raise StateError(f"stage probe status lacks an attempt record: {identity}")


def _validate_derived_state(transaction: JournalTransaction) -> None:
    derived = derive_stage_statuses(
        transaction.stages,
        transaction.stage_probe_statuses,
        transaction.operation_stages,
        transaction.operation_statuses,
        transaction.probe_activations,
    )
    if derived != transaction.stages:
        raise StateError("transaction stage statuses are inconsistent with operation statuses")
    calculated = roll_up_transaction(transaction.stages, transaction.completion)
    if calculated is not transaction.status:
        raise StateError(
            "transaction status is inconsistent: "
            f"recorded={transaction.status.value}, calculated={calculated.value}"
        )


def validate_stage_probe_attempt(
    attempt: dict[str, JsonValue],
    *,
    stage_id: str,
    boundary: str,
    probe_id: str,
    status: CheckpointStatus,
) -> None:
    if frozenset(attempt) != STAGE_PROBE_ATTEMPT_KEYS:
        raise StateError("stage probe attempt does not match the closed v1 shape")
    _validate_stage_attempt_checkpoint(attempt, stage_id, boundary, probe_id, status)
    _validate_attempt_common(attempt, "stage probe attempt")
    _validate_object_array(attempt.get("evidence"), "stage probe attempt evidence")


def _validate_stage_attempt_checkpoint(
    attempt: dict[str, JsonValue],
    stage_id: str,
    boundary: str,
    probe_id: str,
    status: CheckpointStatus,
) -> None:
    expected = (probe_id, stage_id, boundary, status.value)
    actual = (
        attempt.get("probe_id"),
        attempt.get("stage_id"),
        attempt.get("boundary"),
        attempt.get("checkpoint_status"),
    )
    if actual != expected:
        raise StateError("stage probe attempt does not match its checkpoint identity")
    if boundary not in JOURNAL_BOUNDARY_KEYS:
        raise StateError(f"stage probe boundary is invalid: {boundary!r}")


def validate_attempt(
    attempt: dict[str, JsonValue],
    operation_id: str,
    stage_id: str,
    status: CheckpointStatus,
) -> None:
    if frozenset(attempt) != OPERATION_ATTEMPT_KEYS:
        raise StateError("operation attempt does not match the closed v1 shape")
    expected = (operation_id, stage_id, status.value)
    actual = (
        attempt.get("operation_id"),
        attempt.get("stage_id"),
        attempt.get("checkpoint_status"),
    )
    if actual != expected:
        raise StateError("operation attempt does not match its checkpoint identity")
    phase = attempt.get("phase")
    if phase not in {"pre_probe", "apply", "post_probe", "completion_probe"}:
        raise StateError(f"operation attempt phase is invalid: {phase!r}")
    _validate_attempt_common(attempt, "operation attempt")
    try:
        optional_int(attempt["exit_code"], "operation attempt exit_code", minimum=0, maximum=255)
        boolean(attempt["timed_out"], "operation attempt timed_out")
        bounded_integer(attempt["duration_ms"], "operation attempt duration_ms", minimum=0)
        validate_reason(attempt["reason"])
    except (TypeError, ValueError) as exc:
        raise StateError(f"operation attempt diagnostic fields are invalid: {exc}") from exc
    _validate_object_array(attempt.get("planned_actions"), "operation attempt planned_actions")
    _validate_object_array(attempt.get("evidence"), "operation attempt evidence")


def _validate_attempt_common(
    attempt: dict[str, JsonValue],
    attempt_kind: str,
) -> None:
    number = attempt.get("attempt")
    if isinstance(number, bool) or not isinstance(number, int) or number < 1:
        raise StateError(f"{attempt_kind} number must be positive")
    validate_request_id(attempt.get("request_id"), attempt_kind)
    if not isinstance(attempt.get("retry_safe"), bool):
        raise StateError(f"{attempt_kind} retry_safe must be boolean")


def validate_request_id(value: JsonValue | None, attempt_kind: str) -> None:
    if not isinstance(value, str):
        raise StateError(f"{attempt_kind} request_id must be a string")
    try:
        uuid.UUID(value)
    except ValueError as exc:
        raise StateError(f"{attempt_kind} request_id must be a UUID") from exc


def _validate_object_array(value: JsonValue | None, label: str) -> None:
    if not isinstance(value, list) or not all(isinstance(item, dict) for item in value):
        raise StateError(f"{label} must be an object array")


def _string_dict(value: JsonValue, label: str) -> dict[str, str]:
    if not isinstance(value, dict):
        raise TypeError(f"{label} must be a string map")
    if not all(isinstance(item, str) for item in value.values()):
        raise TypeError(f"{label} must be a string map")
    return {key: item for key, item in value.items() if isinstance(item, str)}


def _status_dict(value: JsonValue, label: str) -> dict[str, CheckpointStatus]:
    return {key: CheckpointStatus(item) for key, item in _string_dict(value, label).items()}


def _probe_activations(value: JsonValue) -> dict[str, dict[str, str]]:
    if not isinstance(value, dict):
        raise TypeError("probe_activations must be an object")
    parsed: dict[str, dict[str, str]] = {}
    for probe_id, activation in value.items():
        if not isinstance(activation, dict):
            raise TypeError("probe activation must be an object")
        state = activation.get("state")
        reason = activation.get("reason")
        decided_by = activation.get("decided_by")
        if (
            not isinstance(state, str)
            or not isinstance(reason, str)
            or not isinstance(decided_by, str)
        ):
            raise TypeError("probe activation values must be strings")
        parsed[probe_id] = {
            "state": state,
            "reason": reason,
            "decided_by": decided_by,
        }
    return parsed


def _validate_probe_activations(
    statuses: StageProbeStatuses,
    activations: dict[str, dict[str, str]],
) -> None:
    declared = {
        activation_site_key(stage_id, boundary, probe_id)
        for stage_id, boundaries in statuses.items()
        for boundary, probes in boundaries.items()
        for probe_id in probes
    }
    if set(activations) != declared:
        raise StateError("transaction probe activation ids differ from stage probes")


def _optional_string(value: JsonValue) -> str | None:
    if value is None or isinstance(value, str):
        return value
    raise TypeError("expected string or null")


def _object_tuple(value: JsonValue, label: str) -> tuple[dict[str, JsonValue], ...]:
    if not isinstance(value, list) or not all(isinstance(item, dict) for item in value):
        raise TypeError(f"{label} must be an object array")
    return tuple(cast(dict[str, JsonValue], item) for item in value)


def _stage_probe_statuses(value: JsonValue, label: str) -> StageProbeStatuses:
    if not isinstance(value, dict):
        raise TypeError(f"{label} must be an object")
    parsed: StageProbeStatuses = {}
    for stage_id, boundaries in value.items():
        parsed[stage_id] = _boundary_statuses(boundaries, f"{label}.{stage_id}")
    return parsed


def _boundary_statuses(
    value: JsonValue,
    label: str,
) -> dict[str, dict[str, CheckpointStatus]]:
    if not isinstance(value, dict) or frozenset(value) != JOURNAL_BOUNDARY_KEYS:
        raise TypeError(f"{label} must have exact entry/exit maps")
    return {
        boundary: _status_dict(value[boundary], f"{label}.{boundary}")
        for boundary in sorted(JOURNAL_BOUNDARY_KEYS)
    }
