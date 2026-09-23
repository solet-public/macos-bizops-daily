"""Declared start operation and identity-postcondition orchestration."""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Protocol, cast

from .adapters import (
    AdapterRegistry,
    OperationRequest,
    OperationResult,
    resolve_long_lived_python,
)
from .contract_reconciliation import recover_contract_reconciliation
from .contracts import ContractBundle, target_contract_directory
from .errors import StateConflictError
from .flow import active_probe_ids
from .models import CheckpointStatus, CommandResult, ExitCode, InstanceRecord, JsonValue
from .paths import ManagerPaths
from .registry import InstanceRegistry
from .state_io import instance_lock
from .transaction import Transaction, load_transaction


class AdapterInvoker(Protocol):
    def __call__(
        self,
        registry: AdapterRegistry,
        *,
        runner: str,
        request: OperationRequest,
    ) -> OperationResult: ...


def start_instance(
    *,
    paths: ManagerPaths,
    registry: InstanceRegistry,
    name: str,
    invoke: AdapterInvoker,
) -> CommandResult:
    record = registry.require(name)
    target = Path(record.target)
    with instance_lock(paths.lock_path(name), create=True):
        recover_contract_reconciliation(paths, name)
        transaction, bundle = _start_context(paths, name, record, target)
        contract = bundle.start_command
        operation_ref = cast(str, contract["operation_ref"])
        operation_id = operation_ref.replace("::", ".")
        approval = _approval_fingerprint(transaction)
        timeout = cast(int, contract["timeout_seconds"])
        adapter_registry = AdapterRegistry(
            target=target,
            base_python=resolve_long_lived_python(),
        )
        applied = invoke(
            adapter_registry,
            runner=cast(str, contract["runner"]),
            request=_request(
                transaction=transaction,
                bundle=bundle,
                operation_id=operation_id,
                operation_ref=operation_ref,
                phase="apply",
                purpose=None,
                approval=approval,
                timeout=timeout,
            ),
        )
        if applied.checkpoint_status is not CheckpointStatus.APPLIED:
            return start_stop(name, record.to_dict(), applied, [])
        postconditions, failed = _run_postconditions(
            transaction=transaction,
            bundle=bundle,
            contract=contract,
            operation_id=operation_id,
            timeout=timeout,
            registry=adapter_registry,
            invoke=invoke,
        )
        if failed is not None:
            return start_stop(
                name,
                record.to_dict(),
                failed,
                postconditions,
                applied=applied,
            )
        return _verified_start(name, record, applied, postconditions)


def _start_context(
    paths: ManagerPaths,
    name: str,
    record: InstanceRecord,
    target: Path,
) -> tuple[Transaction, ContractBundle]:
    transaction = load_transaction(paths.transaction_path(name))
    if transaction is None:
        raise StateConflictError(f"managed instance {name!r} lacks its transaction journal")
    if not _pinned_identities_match(transaction, record):
        raise StateConflictError("registry and transaction pinned identities differ")
    bundle = ContractBundle.load(
        source_revision=record.flow_source_revision,
        directory=target_contract_directory(target),
        expected_digest=record.flow_contract_digest,
        resume_compatibility=True,
    )
    return transaction, bundle


def _pinned_identities_match(
    transaction: Transaction,
    record: InstanceRecord,
) -> bool:
    return (
        transaction.target == record.target
        and transaction.flow_id == record.flow_id
        and transaction.flow_source_revision == record.flow_source_revision
        and transaction.flow_contract_digest == record.flow_contract_digest
    )


def _approval_fingerprint(transaction: Transaction) -> str:
    approval = transaction.approval_fingerprint
    if approval is None:
        raise StateConflictError(
            "managed transaction has no recorded approval fingerprint",
            repair="Resume create through a reviewed preview before starting the instance.",
        )
    return approval


def _request(
    *,
    transaction: Transaction,
    bundle: ContractBundle,
    operation_id: str,
    operation_ref: str,
    phase: str,
    purpose: str | None,
    approval: str | None,
    timeout: int,
) -> OperationRequest:
    return OperationRequest(
        request_id=str(uuid.uuid4()),
        operation_id=operation_id,
        operation_ref=operation_ref,
        phase=phase,
        probe_purpose=purpose,
        attempt=1,
        name=transaction.name,
        target=transaction.target,
        flow_id=bundle.flow_id,
        flow_source_revision=bundle.source_revision,
        answers_fingerprint=transaction.answers_fingerprint,
        approval_fingerprint=approval,
        dry_run=phase == "probe",
        timeout_seconds=timeout,
        public_inputs={},
    )


def _run_postconditions(
    *,
    transaction: Transaction,
    bundle: ContractBundle,
    contract: dict[str, JsonValue],
    operation_id: str,
    timeout: int,
    registry: AdapterRegistry,
    invoke: AdapterInvoker,
) -> tuple[list[JsonValue], OperationResult | None]:
    probe_ids = _active_postconditions(transaction, bundle, contract)
    results: list[JsonValue] = []
    for probe_id in probe_ids:
        definition = bundle.probes[probe_id]
        result = invoke(
            registry,
            runner=cast(str, definition["runner"]),
            request=_request(
                transaction=transaction,
                bundle=bundle,
                operation_id=f"{operation_id}.{probe_id}",
                operation_ref=cast(str, definition["probe_ref"]),
                phase="probe",
                purpose="post_apply",
                approval=None,
                timeout=timeout,
            ),
        )
        results.append({"probe_id": probe_id, "result": result.to_dict()})
        if result.checkpoint_status is not CheckpointStatus.VERIFIED:
            return results, result
    return results, None


def _active_postconditions(
    transaction: Transaction,
    bundle: ContractBundle,
    contract: dict[str, JsonValue],
) -> tuple[str, ...]:
    decisions = transaction.answers.get("decisions")
    if not isinstance(decisions, dict):
        raise StateConflictError("transaction decisions are not an object")
    declared = tuple(
        cast(str, probe_id)
        for probe_id in cast(list[JsonValue], contract["postcondition_probe_refs"])
    )
    active = active_probe_ids(bundle, decisions, declared)
    if not active:
        raise StateConflictError(
            "start has no active identity postcondition for the recorded decisions"
        )
    return active


def _verified_start(
    name: str,
    record: InstanceRecord,
    applied: OperationResult,
    postconditions: list[JsonValue],
) -> CommandResult:
    return CommandResult(
        kind="instance_start",
        status="verified",
        message=f"Start and every declared identity postcondition verified for {name!r}.",
        exit_code=ExitCode.OK,
        data={
            "instance": record.to_dict(),
            "apply_result": applied.to_dict(),
            "postconditions": postconditions,
        },
    )


def start_stop(
    name: str,
    record: dict[str, JsonValue],
    result: OperationResult,
    postconditions: list[JsonValue],
    *,
    applied: OperationResult | None = None,
) -> CommandResult:
    return CommandResult(
        kind="instance_start",
        status=result.checkpoint_status.value,
        message=f"Start did not satisfy its declared postconditions for {name!r}.",
        exit_code=(
            ExitCode.FAILED
            if result.checkpoint_status is CheckpointStatus.FAILED
            else ExitCode.HUMAN_ACTION
        ),
        error_kind=result.error_kind or "start_not_applied",
        repair=result.repair or "Repair the failed declared start condition and retry.",
        data={
            "instance": record,
            "apply_result": (result if applied is None else applied).to_dict(),
            "postconditions": postconditions,
        },
    )
