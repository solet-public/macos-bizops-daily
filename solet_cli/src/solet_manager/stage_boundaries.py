"""Stage-boundary probe execution and journal rendering."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from pathlib import Path

from .adapters import AdapterRegistry, OperationRequest, OperationResult, invoke_adapter
from .contracts import ContractBundle, startup_readiness_budget
from .errors import StateConflictError
from .flow import unresolved_decision_ids_for_stages
from .inference_probe_policy import advisory_inference_probe_result
from .journal_migrations import activation_site_key
from .models import CheckpointStatus, CommandResult, ExitCode, JsonValue
from .probe_input_projection import probe_public_inputs
from .transaction import Transaction, utc_now, write_transaction


@dataclass(frozen=True)
class BoundaryProbeOutcome:
    """One stage-boundary probe result and its updated transaction."""

    transaction: Transaction
    observation: dict[str, JsonValue]
    failed: bool


@dataclass(frozen=True)
class BoundaryFailure:
    """A failed boundary probe and its flow-declared remediation operations."""

    identity: str
    stage_id: str
    boundary: str
    probe_id: str
    remediation_operation_ids: tuple[str, ...]


def run_stage_boundaries(
    *,
    bundle: ContractBundle,
    transaction: Transaction,
    registry: AdapterRegistry,
    stage_ids: tuple[str, ...],
    boundary: str,
    answers: dict[str, JsonValue],
    persist_path: Path | None,
) -> tuple[Transaction, dict[str, JsonValue], list[BoundaryFailure]]:
    observations: dict[str, JsonValue] = {}
    updated = transaction
    for stage_id in stage_ids:
        for probe_id in _boundary_probe_ids(bundle, stage_id, boundary):
            identity = f"{stage_id}:{boundary}:{probe_id}"
            outcome = _run_boundary_probe(
                bundle=bundle,
                transaction=updated,
                registry=registry,
                stage_id=stage_id,
                boundary=boundary,
                probe_id=probe_id,
                answers=answers,
                persist_path=persist_path,
            )
            updated = outcome.transaction
            if outcome.observation:
                observations[identity] = outcome.observation
            if outcome.failed:
                return (
                    updated,
                    observations,
                    [
                        BoundaryFailure(
                            identity=identity,
                            stage_id=stage_id,
                            boundary=boundary,
                            probe_id=probe_id,
                            remediation_operation_ids=_remediation_operation_ids(
                                bundle,
                                probe_id,
                            ),
                        )
                    ],
                )
    return updated, observations, []


def _run_boundary_probe(
    *,
    bundle: ContractBundle,
    transaction: Transaction,
    registry: AdapterRegistry,
    stage_id: str,
    boundary: str,
    probe_id: str,
    answers: dict[str, JsonValue],
    persist_path: Path | None,
) -> BoundaryProbeOutcome:
    if persist_path is not None:
        write_transaction(persist_path, transaction)
    current = transaction.stage_probe_statuses[stage_id][boundary][probe_id]
    if (
        transaction.probe_activations[activation_site_key(stage_id, boundary, probe_id)]["state"]
        == "inactive"
    ):
        return BoundaryProbeOutcome(transaction, {}, False)
    if current is CheckpointStatus.VERIFIED:
        return BoundaryProbeOutcome(
            transaction=transaction,
            observation=recorded_stage_probe_observation(
                transaction,
                stage_id=stage_id,
                boundary=boundary,
                probe_id=probe_id,
            ),
            failed=False,
        )
    attempt = next_stage_probe_attempt(
        transaction,
        stage_id=stage_id,
        boundary=boundary,
        probe_id=probe_id,
    )
    result = _invoke_boundary_probe(
        bundle=bundle,
        transaction=transaction,
        registry=registry,
        stage_id=stage_id,
        boundary=boundary,
        probe_id=probe_id,
        answers=answers,
        attempt=attempt,
    )
    updated = transaction.with_stage_probe_status(
        stage_id,
        boundary,
        probe_id,
        result.checkpoint_status,
        attempt=stage_probe_attempt_record(
            result,
            stage_id=stage_id,
            boundary=boundary,
            attempt=attempt,
        ),
    )
    if persist_path is not None:
        write_transaction(persist_path, updated)
    accepted = {
        CheckpointStatus.VERIFIED,
        CheckpointStatus.NOT_APPLICABLE,
    }
    return BoundaryProbeOutcome(
        updated,
        canonical_stage_probe_observation(result),
        result.checkpoint_status not in accepted,
    )


def _invoke_boundary_probe(
    *,
    bundle: ContractBundle,
    transaction: Transaction,
    registry: AdapterRegistry,
    stage_id: str,
    boundary: str,
    probe_id: str,
    answers: dict[str, JsonValue],
    attempt: int,
) -> OperationResult:
    definition = bundle.probes[probe_id]
    if definition.get("runner") == "manager":
        return manager_stage_probe_result(
            bundle=bundle,
            stage_id=stage_id,
            probe_id=probe_id,
            boundary=boundary,
            answers=answers,
        )
    public_inputs = probe_public_inputs(transaction, str(definition["probe_ref"]))
    timeout_seconds = 30
    if boundary == "exit" and probe_id in startup_readiness_budget(bundle).consumer_probe_refs:
        readiness = startup_readiness_budget(bundle)
        timeout_seconds = readiness.parent_budget_seconds
        public_inputs.update(
            readiness.public_inputs(
                consumer_probe_purpose="stage_exit",
                consumer_probe_ref=probe_id,
            )
        )
    request = OperationRequest(
        request_id=str(uuid.uuid4()),
        operation_id=probe_id,
        operation_ref=str(definition["probe_ref"]),
        phase="probe",
        probe_purpose="stage_entry" if boundary == "entry" else "stage_exit",
        attempt=attempt,
        name=transaction.name,
        target=transaction.target,
        flow_id=bundle.flow_id,
        flow_source_revision=bundle.source_revision,
        answers_fingerprint=transaction.answers_fingerprint,
        approval_fingerprint=None,
        dry_run=True,
        timeout_seconds=timeout_seconds,
        public_inputs=public_inputs,
    )
    result = invoke_adapter(registry, runner=str(definition["runner"]), request=request)
    if not isinstance(result, OperationResult):
        return result
    answers = getattr(transaction, "answers", {})
    if not isinstance(answers, dict):
        return result
    return advisory_inference_probe_result(answers, request, result)


def _boundary_probe_ids(
    bundle: ContractBundle,
    stage_id: str,
    boundary: str,
) -> tuple[str, ...]:
    field_name = "entry_probe_refs" if boundary == "entry" else "exit_probe_refs"
    value = bundle.stages[stage_id].get(field_name)
    if value is None:
        return ()
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise StateConflictError("flow reference list is invalid")
    return tuple(item for item in value if isinstance(item, str))


def _remediation_operation_ids(
    bundle: ContractBundle,
    probe_id: str,
) -> tuple[str, ...]:
    declared = _declared_probe_remediation_ids(bundle, probe_id)
    return declared or _permission_remediation_operation_ids(bundle, probe_id)


def _declared_probe_remediation_ids(
    bundle: ContractBundle,
    probe_id: str,
) -> tuple[str, ...]:
    probe = bundle.probes.get(probe_id)
    if probe is None:
        raise StateConflictError(f"boundary probe {probe_id!r} is not declared")
    if "remediation_operation_refs" in probe:
        declared = probe["remediation_operation_refs"]
        if not isinstance(declared, list) or not all(
            isinstance(operation_id, str) for operation_id in declared
        ):
            raise StateConflictError(
                f"boundary probe {probe_id!r} has invalid remediation references"
            )
        declared_operation_ids = tuple(
            operation_id for operation_id in declared if isinstance(operation_id, str)
        )
        unknown = sorted(set(declared_operation_ids) - set(bundle.operations))
        if unknown:
            raise StateConflictError(
                f"boundary probe {probe_id!r} has unknown remediation operations: {unknown}"
            )
        if declared_operation_ids:
            return tuple(dict.fromkeys(declared_operation_ids))
    return ()


def _permission_remediation_operation_ids(
    bundle: ContractBundle,
    probe_id: str,
) -> tuple[str, ...]:
    raw_permissions = bundle.flow.get("permissions")
    if not isinstance(raw_permissions, dict):
        raise StateConflictError("flow permissions registry is invalid")
    operation_ids: set[str] = set()
    for permission_id, permission in raw_permissions.items():
        if not isinstance(permission, dict):
            raise StateConflictError("flow permission declaration is invalid")
        if permission.get("probe_ref") != probe_id:
            continue
        operation_id = permission.get("grant_operation_ref")
        if operation_id is None:
            continue
        if not isinstance(operation_id, str) or operation_id not in bundle.operations:
            raise StateConflictError(
                f"permission {permission_id!r} has an invalid grant operation reference"
            )
        operation_ids.add(operation_id)
    return tuple(sorted(operation_ids))


def canonical_stage_probe_observation(
    result: OperationResult,
) -> dict[str, JsonValue]:
    return {
        "checkpoint_status": result.checkpoint_status.value,
        "error_kind": result.error_kind,
        "retry_safe": result.retry_safe,
        "evidence": [
            {key: value for key, value in item.items() if key != "captured_at"}
            for item in result.evidence
        ],
        "repair": result.repair,
    }


def recorded_stage_probe_observation(
    transaction: Transaction,
    *,
    stage_id: str,
    boundary: str,
    probe_id: str,
) -> dict[str, JsonValue]:
    attempt = _recorded_probe_attempt(
        transaction,
        stage_id=stage_id,
        boundary=boundary,
        probe_id=probe_id,
    )
    evidence = attempt.get("evidence")
    if not isinstance(evidence, list):
        raise StateConflictError(
            f"recorded stage probe evidence is invalid: {stage_id}/{boundary}/{probe_id}"
        )
    return {
        "checkpoint_status": attempt.get("checkpoint_status"),
        "error_kind": attempt.get("error_kind"),
        "retry_safe": attempt.get("retry_safe"),
        "evidence": [
            {key: value for key, value in item.items() if key != "captured_at"}
            for item in evidence
            if isinstance(item, dict)
        ],
        "repair": attempt.get("repair"),
    }


def _recorded_probe_attempt(
    transaction: Transaction,
    *,
    stage_id: str,
    boundary: str,
    probe_id: str,
) -> dict[str, JsonValue]:
    for attempt in reversed(transaction.stage_probe_attempts):
        if (
            attempt.get("stage_id") == stage_id
            and attempt.get("boundary") == boundary
            and attempt.get("probe_id") == probe_id
        ):
            return attempt
    raise StateConflictError(
        f"verified stage probe lacks its journal attempt: {stage_id}/{boundary}/{probe_id}"
    )


def manager_stage_probe_result(
    *,
    bundle: ContractBundle,
    stage_id: str,
    probe_id: str,
    boundary: str,
    answers: dict[str, JsonValue],
) -> OperationResult:
    if probe_id != "decisions_resolved":
        raise StateConflictError(f"unknown manager-owned probe {probe_id!r}")
    unresolved = unresolved_decision_ids_for_stages(bundle, answers, {stage_id})
    status = CheckpointStatus.AWAITING_USER if unresolved else CheckpointStatus.VERIFIED
    evidence: tuple[dict[str, JsonValue], ...] = (
        {
            "id": "decisions_resolved",
            "kind": "decision_state",
            "status": status.value,
            "summary": (
                "unresolved [decisions] keys: " + ", ".join(unresolved)
                if unresolved
                else f"all active required decisions for {stage_id} are resolved"
            ),
            "observed": list(unresolved),
            "expected": [],
            "source": "manager::decisions.all_required_resolved",
            "digest": "none",
            "captured_at": utc_now(),
            "sensitivity": "public",
        },
    )
    return OperationResult(
        request_id=str(uuid.uuid4()),
        operation_id=probe_id,
        phase="probe",
        probe_purpose="stage_entry" if boundary == "entry" else "stage_exit",
        checkpoint_status=status,
        error_kind="decisions_required" if unresolved else None,
        retry_safe=True,
        exit_code=0,
        timed_out=False,
        duration_ms=0,
        stdout="",
        stderr="",
        planned_actions=(),
        discovered_candidates=(),
        evidence=evidence,
        repair=(
            "Add these exact [decisions] keys: " + ", ".join(unresolved) if unresolved else None
        ),
    )


def stage_probe_attempt_record(
    result: OperationResult,
    *,
    stage_id: str,
    boundary: str,
    attempt: int,
) -> dict[str, JsonValue]:
    return {
        "probe_id": result.operation_id,
        "stage_id": stage_id,
        "boundary": boundary,
        "attempt": attempt,
        "request_id": result.request_id,
        "checkpoint_status": result.checkpoint_status.value,
        "error_kind": result.error_kind,
        "retry_safe": result.retry_safe,
        "evidence": list(result.evidence),
        "repair": result.repair,
        "recorded_at": utc_now(),
    }


def next_stage_probe_attempt(
    transaction: Transaction,
    *,
    stage_id: str,
    boundary: str,
    probe_id: str,
) -> int:
    values = [
        attempt
        for recorded in transaction.stage_probe_attempts
        if recorded.get("stage_id") == stage_id
        and recorded.get("boundary") == boundary
        and recorded.get("probe_id") == probe_id
        and isinstance((attempt := recorded.get("attempt")), int)
        and not isinstance(attempt, bool)
    ]
    return 1 + max(values, default=0)


def stage_probe_status_dict(transaction: Transaction) -> dict[str, JsonValue]:
    return {
        stage_id: {
            boundary: {probe_id: status.value for probe_id, status in statuses.items()}
            for boundary, statuses in boundaries.items()
        }
        for stage_id, boundaries in transaction.stage_probe_statuses.items()
    }


def stage_stop_result(identity: str, transaction: Transaction) -> CommandResult:
    stage_id, boundary, probe_id = identity.split(":", 2)
    attempt = _optional_recorded_probe_attempt(
        transaction,
        stage_id=stage_id,
        boundary=boundary,
        probe_id=probe_id,
    )
    status = transaction.stage_probe_statuses[stage_id][boundary][probe_id]
    return CommandResult(
        kind="create",
        status=transaction.status.value,
        message=f"Setup stopped at stage probe {stage_id}/{boundary}/{probe_id}.",
        exit_code=(ExitCode.FAILED if status is CheckpointStatus.FAILED else ExitCode.HUMAN_ACTION),
        error_kind=_attempt_text(attempt, "error_kind", "stage_probe_incomplete"),
        repair=_attempt_text(
            attempt,
            "repair",
            "Repair the stage boundary probe and resume.",
        ),
        data={
            "frontier": [stage_id] if stage_id in transaction.stages else [],
            "stage_id": stage_id,
            "boundary": boundary,
            "probe_id": probe_id,
            "attempt": attempt,
            "transaction": transaction.to_dict(),
        },
    )


def _optional_recorded_probe_attempt(
    transaction: Transaction,
    *,
    stage_id: str,
    boundary: str,
    probe_id: str,
) -> dict[str, JsonValue] | None:
    return next(
        (
            item
            for item in reversed(transaction.stage_probe_attempts)
            if item.get("stage_id") == stage_id
            and item.get("boundary") == boundary
            and item.get("probe_id") == probe_id
        ),
        None,
    )


def _attempt_text(
    attempt: dict[str, JsonValue] | None,
    field: str,
    default: str,
) -> str:
    if attempt is None:
        return default
    return str(attempt.get(field) or default)
