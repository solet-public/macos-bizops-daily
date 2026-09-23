"""Durable installation journal and deterministic transaction roll-up."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, replace
from pathlib import Path
from typing import cast

from solet_setup_contracts import canonical_sha256

from .errors import StateConflictError, StateError
from .journal_migrations import CURRENT_JOURNAL_VERSION, JOURNAL_BOUNDARY_KEYS, activation_site_key
from .journal_rollup import (
    StageProbeStatuses,
    derive_stage_statuses,
    roll_up_transaction,
)
from .journal_validation import (
    JournalTransaction,
    ParsedJournal,
    parse_transaction_fields,
    validate_attempt,
    validate_stage_probe_attempt,
    validate_transaction_state,
)
from .models import (
    CheckpointStatus,
    JsonValue,
    TransactionStatus,
)
from .release_lock import SeedLock
from .state_io import atomic_write_json, load_json_object

__all__ = [
    "StageProbeStatuses",
    "Transaction",
    "assert_resume_identity",
    "append_maintenance_attempt",
    "canonical_sha256",
    "create_import_maintenance_operation",
    "load_transaction",
    "maintenance_evidence",
    "parse_maintenance_operation_bytes",
    "read_maintenance_operation",
    "roll_up_transaction",
    "transition_maintenance_operation",
    "target_install_state_projection",
    "utc_now",
    "write_transaction",
    "write_maintenance_operation",
]


from .maintenance_journal import (
    append_maintenance_attempt,
    create_import_maintenance_operation,
    maintenance_evidence,
    parse_maintenance_operation_bytes,
    read_maintenance_operation,
    transition_maintenance_operation,
    utc_now,
    write_maintenance_operation,
)


@dataclass(frozen=True)
class Transaction:
    """Schema-versioned installation journal."""

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
    journal_version: int = CURRENT_JOURNAL_VERSION

    @classmethod
    def create(
        cls,
        *,
        name: str,
        target: Path,
        input_fingerprint: str,
        answers: dict[str, JsonValue],
        seed: SeedLock,
        flow_id: str,
        flow_source_revision: str,
        flow_contract_digest: str,
        stage_ids: tuple[str, ...],
        completion_probe_ids: tuple[str, ...],
        stage_probe_statuses: StageProbeStatuses | None = None,
        probe_activations: dict[str, dict[str, str]] | None = None,
    ) -> Transaction:
        now = utc_now()
        source_probes = _initial_stage_probe_statuses(stage_ids, stage_probe_statuses)
        activations = _initial_probe_activations(source_probes) if probe_activations is None else {probe_id: dict(value) for probe_id, value in probe_activations.items()}
        probes = _storage_stage_probe_statuses(source_probes)
        stages = derive_stage_statuses(
            dict.fromkeys(stage_ids, CheckpointStatus.PENDING),
            probes,
            {},
            {},
            activations,
        )
        return cls(
            operation_id=str(uuid.uuid4()),
            name=name,
            target=str(target),
            input_fingerprint=input_fingerprint,
            answers=answers,
            answers_fingerprint=canonical_sha256(answers),
            approval_fingerprint=None,
            approval_recorded_at=None,
            seed=seed,
            flow_id=flow_id,
            flow_source_revision=flow_source_revision,
            flow_contract_digest=flow_contract_digest,
            status=TransactionStatus.PENDING,
            stages=stages,
            stage_probe_statuses=probes,
            probe_activations=activations,
            stage_probe_attempts=(),
            operation_stages={},
            operation_statuses={},
            operation_attempts=(),
            evidence=(),
            completion=dict.fromkeys(completion_probe_ids, CheckpointStatus.PENDING),
            result_kind=None,
            created_at=now,
            updated_at=now,
        )

    def with_statuses(
        self,
        *,
        stages: dict[str, CheckpointStatus] | None = None,
        completion: dict[str, CheckpointStatus] | None = None,
    ) -> Transaction:
        next_stages = self.stages if stages is None else stages
        next_completion = self.completion if completion is None else completion
        return replace(
            self,
            stages=next_stages,
            completion=next_completion,
            status=roll_up_transaction(next_stages, next_completion),
            updated_at=utc_now(),
        )

    def approve(self, fingerprint: str) -> Transaction:
        now = utc_now()
        return replace(
            self,
            approval_fingerprint=fingerprint,
            approval_recorded_at=now,
            updated_at=now,
        )

    def with_answers(self, answers: dict[str, JsonValue]) -> Transaction:
        return replace(
            self,
            answers=answers,
            answers_fingerprint=canonical_sha256(answers),
            updated_at=utc_now(),
        )

    def reconciled_identity(self, *, flow_source_revision: str) -> Transaction:
        """Rebind only the stored flow revision and its answer fingerprint."""

        answers = dict(self.answers)
        answers["flow_source_revision"] = flow_source_revision
        return replace(
            self,
            answers=answers,
            answers_fingerprint=canonical_sha256(answers),
            flow_source_revision=flow_source_revision,
            approval_fingerprint=None,
            approval_recorded_at=None,
            # The approval fingerprint covers replacement bytes. Keep the
            # historical timestamp stable while the lock-time prepare repeats.
            updated_at=self.updated_at,
        )

    def with_probe_activations(
        self,
        probe_activations: dict[str, dict[str, str]],
    ) -> Transaction:
        """Persist a whole derived activation map and atomically recompute roll-up."""

        activations = {probe_id: dict(value) for probe_id, value in probe_activations.items()}
        stages = derive_stage_statuses(
            self.stages,
            self.stage_probe_statuses,
            self.operation_stages,
            self.operation_statuses,
            activations,
        )
        return replace(
            self,
            probe_activations=activations,
            stages=stages,
            status=roll_up_transaction(stages, self.completion),
            updated_at=utc_now(),
        )

    def rebind_completion(self, probe_ids: tuple[str, ...]) -> Transaction:
        """Re-scope the completion set, preserving statuses already recorded.

        The mirror of :meth:`bind_operations` for completion checks: probes the
        answers no longer require are dropped, newly required probes enter as
        pending, and the roll-up is recomputed in the same write so the
        persisted status never trails the set it summarizes.

        A probe that already ran is dropped like any other once it stops being
        required.  Its attempt stays in retained history, which the journal
        accepts for an unbound probe exactly as it does for an unbound
        operation; a deselected check must not keep a target from converging on
        the strength of a result nobody asked for.
        """

        completion = {probe_id: self.completion.get(probe_id, CheckpointStatus.PENDING) for probe_id in probe_ids}
        if completion == self.completion:
            return self
        return replace(
            self,
            completion=completion,
            status=roll_up_transaction(self.stages, completion),
            updated_at=utc_now(),
        )

    def bind_operations(self, operation_stages: dict[str, str]) -> Transaction:
        """Bind the selected graph and derive stages from selected operations."""

        unknown_stages = sorted(set(operation_stages.values()) - set(self.stages))
        if unknown_stages:
            raise StateError(f"operation graph references unknown stages: {unknown_stages}")
        statuses = {operation_id: self.operation_statuses.get(operation_id, CheckpointStatus.PENDING) for operation_id in operation_stages}
        stages = derive_stage_statuses(
            self.stages,
            self.stage_probe_statuses,
            operation_stages,
            statuses,
            self.probe_activations,
        )
        return replace(
            self,
            operation_stages=dict(operation_stages),
            operation_statuses=statuses,
            stages=stages,
            status=roll_up_transaction(stages, self.completion),
            updated_at=utc_now(),
        )

    def with_operation_status(
        self,
        operation_id: str,
        status: CheckpointStatus,
        *,
        attempt: dict[str, JsonValue] | None = None,
    ) -> Transaction:
        if operation_id not in self.operation_stages:
            raise StateError(f"operation {operation_id!r} is not bound to the transaction")
        statuses = {**self.operation_statuses, operation_id: status}
        attempts, evidence = _append_operation_attempt(
            self,
            operation_id,
            status,
            attempt,
        )
        stages = derive_stage_statuses(
            self.stages,
            self.stage_probe_statuses,
            self.operation_stages,
            statuses,
            self.probe_activations,
        )
        return replace(
            self,
            operation_statuses=statuses,
            operation_attempts=attempts,
            evidence=evidence,
            stages=stages,
            status=roll_up_transaction(stages, self.completion),
            result_kind=None,
            updated_at=utc_now(),
        )

    def with_stage_probe_status(
        self,
        stage_id: str,
        boundary: str,
        probe_id: str,
        status: CheckpointStatus,
        *,
        attempt: dict[str, JsonValue] | None = None,
    ) -> Transaction:
        _assert_stage_probe_bound(self.stage_probe_statuses, stage_id, boundary, probe_id)
        statuses = _copy_stage_probe_statuses(self.stage_probe_statuses)
        statuses[stage_id][boundary][probe_id] = status
        attempts, evidence = _append_stage_probe_attempt(
            self,
            stage_id,
            boundary,
            probe_id,
            status,
            attempt,
        )
        stages = derive_stage_statuses(
            self.stages,
            statuses,
            self.operation_stages,
            self.operation_statuses,
            self.probe_activations,
        )
        return replace(
            self,
            stage_probe_statuses=statuses,
            stage_probe_attempts=attempts,
            evidence=evidence,
            stages=stages,
            status=roll_up_transaction(stages, self.completion),
            updated_at=utc_now(),
        )

    def with_completion_result(
        self,
        probe_id: str,
        status: CheckpointStatus,
        *,
        attempt: dict[str, JsonValue],
    ) -> Transaction:
        if probe_id not in self.completion:
            raise StateError(f"completion probe {probe_id!r} is not bound to the transaction")
        validate_attempt(attempt, probe_id, "completion", status)
        completion = {**self.completion, probe_id: status}
        evidence = (*self.evidence, *_attempt_evidence(attempt))
        return replace(
            self,
            completion=completion,
            operation_attempts=(*self.operation_attempts, attempt),
            evidence=evidence,
            status=roll_up_transaction(self.stages, completion),
            updated_at=utc_now(),
        )

    def with_result_kind(self, result_kind: str) -> Transaction:
        return replace(self, result_kind=result_kind, updated_at=utc_now())

    def reconciled_contract(
        self,
        *,
        flow_contract_digest: str,
        stages: dict[str, CheckpointStatus],
        stage_probe_statuses: StageProbeStatuses,
        probe_activations: dict[str, dict[str, str]],
        stage_probe_attempts: tuple[dict[str, JsonValue], ...],
        completion_probe_ids: tuple[str, ...],
    ) -> Transaction:
        """Return a reviewed contract migration with its completion set rebound.

        Reconciliation changes the contract that declares completion work, so
        the stored membership must move with the destination bundle.  Preserve
        outcomes for probes still required, admit new requirements as pending,
        and leave retained attempts intact for probes no longer required.
        Unlike ordinary answer revision, this is part of an approval-bound
        replacement and therefore deliberately preserves ``updated_at``.
        """

        completion = {
            probe_id: self.completion.get(probe_id, CheckpointStatus.PENDING)
            for probe_id in completion_probe_ids
        }

        return replace(
            self,
            flow_contract_digest=flow_contract_digest,
            approval_fingerprint=None,
            approval_recorded_at=None,
            stages=stages,
            stage_probe_statuses=stage_probe_statuses,
            probe_activations={probe_id: dict(value) for probe_id, value in probe_activations.items()},
            stage_probe_attempts=stage_probe_attempts,
            completion=completion,
            status=roll_up_transaction(stages, completion),
            # The approval fingerprint includes the exact replacement bytes;
            # retain the prior recorded timestamp so a lock-time re-preview is
            # stable unless a real action-driving input drifted.
            updated_at=self.updated_at,
        )

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "schema_version": CURRENT_JOURNAL_VERSION,
            "operation_id": self.operation_id,
            "name": self.name,
            "target": self.target,
            "input_fingerprint": self.input_fingerprint,
            "answers": self.answers,
            "answers_fingerprint": self.answers_fingerprint,
            "approval_fingerprint": self.approval_fingerprint,
            "approval_recorded_at": self.approval_recorded_at,
            **self.seed.identity_dict(),
            "flow_id": self.flow_id,
            "flow_source_revision": self.flow_source_revision,
            "flow_contract_digest": self.flow_contract_digest,
            "status": self.status.value,
            "stages": {key: status.value for key, status in self.stages.items()},
            "stage_probe_statuses": _public_stage_probe_statuses(self.stage_probe_statuses),
            "probe_activations": cast(JsonValue, self.probe_activations),
            "stage_probe_attempts": list(self.stage_probe_attempts),
            "operation_stages": cast(JsonValue, self.operation_stages),
            "operation_statuses": {key: status.value for key, status in self.operation_statuses.items()},
            "operation_attempts": list(self.operation_attempts),
            "evidence": list(self.evidence),
            "completion": {key: status.value for key, status in self.completion.items()},
            "result_kind": self.result_kind,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, JsonValue]) -> Transaction:
        parsed = parse_transaction_fields(raw)
        transaction = _transaction_from_parsed(parsed)
        validate_transaction_state(
            cast(JournalTransaction, transaction),
            canonical_answers_fingerprint=canonical_sha256(transaction.answers),
        )
        return transaction


def _initial_stage_probe_statuses(
    stage_ids: tuple[str, ...],
    statuses: StageProbeStatuses | None,
) -> StageProbeStatuses:
    if statuses is None:
        return {stage_id: {boundary: {} for boundary in JOURNAL_BOUNDARY_KEYS} for stage_id in stage_ids}
    probes = _copy_stage_probe_statuses(statuses)
    if set(probes) != set(stage_ids):
        raise StateError("stage probe status ids differ from transaction stages")
    return probes


def _initial_probe_activations(
    statuses: StageProbeStatuses,
) -> dict[str, dict[str, str]]:
    """Legacy callers get an explicit active carrier rather than stored N/A."""

    activations: dict[str, dict[str, str]] = {}
    for stage_id, boundaries in statuses.items():
        for boundary, probes in boundaries.items():
            for probe_id, status in probes.items():
                active = status is not CheckpointStatus.NOT_APPLICABLE
                activation = {
                    "state": "active" if active else "inactive",
                    "reason": "plan_bound" if active else "plan_dropped",
                    "decided_by": "plan",
                }
                activations[activation_site_key(stage_id, boundary, probe_id)] = activation
    return activations


def _storage_stage_probe_statuses(statuses: StageProbeStatuses) -> StageProbeStatuses:
    """V3 journals retain probe outcomes separately from their activation."""

    return {stage_id: {boundary: {probe_id: (CheckpointStatus.PENDING if status is CheckpointStatus.NOT_APPLICABLE else status) for probe_id, status in probes.items()} for boundary, probes in boundaries.items()} for stage_id, boundaries in statuses.items()}


def _copy_stage_probe_statuses(
    statuses: StageProbeStatuses,
) -> StageProbeStatuses:
    return {stage_id: {boundary: dict(boundaries[boundary]) for boundary in JOURNAL_BOUNDARY_KEYS} for stage_id, boundaries in statuses.items()}


def _public_stage_probe_statuses(statuses: StageProbeStatuses) -> JsonValue:
    return {stage_id: {boundary: {probe_id: status.value for probe_id, status in probes.items()} for boundary, probes in boundaries.items()} for stage_id, boundaries in statuses.items()}


def _append_operation_attempt(
    transaction: Transaction,
    operation_id: str,
    status: CheckpointStatus,
    attempt: dict[str, JsonValue] | None,
) -> tuple[tuple[dict[str, JsonValue], ...], tuple[dict[str, JsonValue], ...]]:
    if attempt is None:
        return transaction.operation_attempts, transaction.evidence
    validate_attempt(
        attempt,
        operation_id,
        transaction.operation_stages[operation_id],
        status,
    )
    return (
        (*transaction.operation_attempts, attempt),
        (*transaction.evidence, *_attempt_evidence(attempt)),
    )


def _assert_stage_probe_bound(
    statuses: StageProbeStatuses,
    stage_id: str,
    boundary: str,
    probe_id: str,
) -> None:
    if stage_id not in statuses or boundary not in {"entry", "exit"}:
        raise StateError(f"stage probe boundary is not bound: {stage_id}/{boundary}")
    if probe_id not in statuses[stage_id][boundary]:
        raise StateError(f"stage probe {probe_id!r} is not bound to {stage_id!r}/{boundary}")


def _append_stage_probe_attempt(
    transaction: Transaction,
    stage_id: str,
    boundary: str,
    probe_id: str,
    status: CheckpointStatus,
    attempt: dict[str, JsonValue] | None,
) -> tuple[tuple[dict[str, JsonValue], ...], tuple[dict[str, JsonValue], ...]]:
    if attempt is None:
        return transaction.stage_probe_attempts, transaction.evidence
    validate_stage_probe_attempt(
        attempt,
        stage_id=stage_id,
        boundary=boundary,
        probe_id=probe_id,
        status=status,
    )
    identity = (stage_id, boundary, probe_id, attempt["attempt"])
    if any(_stage_attempt_identity(item) == identity for item in transaction.stage_probe_attempts):
        raise StateError(f"duplicate stage probe attempt identity: {identity}")
    return (
        (*transaction.stage_probe_attempts, attempt),
        (*transaction.evidence, *_attempt_evidence(attempt)),
    )


def _stage_attempt_identity(
    attempt: dict[str, JsonValue],
) -> tuple[JsonValue | None, JsonValue | None, JsonValue | None, JsonValue | None]:
    return (
        attempt.get("stage_id"),
        attempt.get("boundary"),
        attempt.get("probe_id"),
        attempt.get("attempt"),
    )


def _attempt_evidence(
    attempt: dict[str, JsonValue],
) -> tuple[dict[str, JsonValue], ...]:
    raw = attempt.get("evidence")
    if not isinstance(raw, list):
        return ()
    return tuple(cast(dict[str, JsonValue], item) for item in raw if isinstance(item, dict))


def load_transaction(path: Path) -> Transaction | None:
    raw = load_json_object(path, missing_ok=True)
    return None if raw is None else Transaction.from_dict(raw)


def write_transaction(path: Path, transaction: Transaction) -> None:
    if transaction.journal_version != CURRENT_JOURNAL_VERSION:
        raise StateError("refusing to write a transaction with pending journal migrations")
    atomic_write_json(path, transaction.to_dict())
    projection_path = Path(transaction.target) / ".solet" / "install-state.json"
    if projection_path.parent.is_dir():
        atomic_write_json(
            projection_path,
            target_install_state_projection(transaction),
        )


def target_install_state_projection(transaction: Transaction) -> dict[str, JsonValue]:
    """Return the target-owned, non-secret proof of the manager journal identity.

    The manager journal remains canonical for resume and lifecycle decisions.
    This atomically written projection becomes available only after Genesis has
    created the target's private ``.solet`` directory, where target-local
    doctor probes can verify the exact immutable identity they were invoked
    with without reading manager-private state paths.
    """

    return {
        "name": transaction.name,
        "target": transaction.target,
        "flow_id": transaction.flow_id,
        "flow_source_revision": transaction.flow_source_revision,
        "answers_fingerprint": transaction.answers_fingerprint,
    }


def _transaction_from_parsed(parsed: ParsedJournal) -> Transaction:
    return Transaction(
        operation_id=parsed.operation_id,
        name=parsed.name, target=parsed.target,
        input_fingerprint=parsed.input_fingerprint, answers=parsed.answers,
        answers_fingerprint=parsed.answers_fingerprint, approval_fingerprint=parsed.approval_fingerprint,
        approval_recorded_at=parsed.approval_recorded_at, seed=parsed.seed,
        flow_id=parsed.flow_id, flow_source_revision=parsed.flow_source_revision,
        flow_contract_digest=parsed.flow_contract_digest,
        status=parsed.status,
        stages=parsed.stages,
        stage_probe_statuses=parsed.stage_probe_statuses,
        probe_activations=parsed.probe_activations,
        stage_probe_attempts=parsed.stage_probe_attempts,
        operation_stages=parsed.operation_stages,
        operation_statuses=parsed.operation_statuses,
        operation_attempts=parsed.operation_attempts,
        evidence=parsed.evidence,
        completion=parsed.completion,
        result_kind=parsed.result_kind,
        created_at=parsed.created_at,
        updated_at=parsed.updated_at,
        journal_version=CURRENT_JOURNAL_VERSION,
    )


def assert_resume_identity(
    existing: Transaction,
    *,
    name: str,
    target: Path,
    input_fingerprint: str,
) -> None:
    """Resume is governed by original immutable identity inputs."""

    if existing.name != name or existing.target != str(target):
        raise StateConflictError("existing transaction belongs to a different name or target")
    if existing.input_fingerprint != input_fingerprint:
        raise StateConflictError(
            "requested inputs differ from the retained transaction",
            repair=("Resume with the original inputs or inspect the transaction before another lifecycle step."),
        )
