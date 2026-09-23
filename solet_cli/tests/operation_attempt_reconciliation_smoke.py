"""Focused regression coverage for interrupted probe-then-apply recovery."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from operation_probe_adapter_support import (
    _FixtureRegistry,
    _operation,
    _require,
    _result,
    _RouteSplitAdapter,
    _transaction,
)
from solet_manager import operation_executor
from solet_manager.adapters import OperationRequest, OperationResult
from solet_manager.contracts import ContractBundle
from solet_manager.errors import OperationAttemptMismatch
from solet_manager.flow import PlannedOperation
from solet_manager.models import CheckpointStatus
from solet_manager.operation_reconciliation import load_transaction_for_operation_reconciliation
from solet_manager.operation_records import attempt_record, operation_request
from solet_manager.paths import ManagerPaths
from solet_manager.transaction import (
    Transaction,
    load_transaction,
    write_transaction,
)


def interrupted_apply_reconciliation_regression(bundle: ContractBundle, root: Path) -> None:
    """Exercise the exact stale applying/pre-probe-pending crash window."""

    operation, paths, interrupted = _interrupted_apply_transaction(bundle, root)
    _assert_interrupted_apply_is_typed(operation, interrupted)
    journal_path = paths.transaction_path(interrupted.name)
    write_transaction(journal_path, interrupted)
    _assert_preview_reconciliation(bundle, operation, journal_path)
    for failure in (
        "artifact_absent",
        "artifact_wrong_size",
        "artifact_wrong_magic",
        "artifact_wrong_provenance",
    ):
        _assert_unsatisfied_reconciliation(
            bundle, operation, paths, journal_path, interrupted, failure
        )


def _interrupted_apply_transaction(
    bundle: ContractBundle,
    root: Path,
) -> tuple[PlannedOperation, ManagerPaths, Transaction]:
    operation = _operation(bundle, "pull_lm_studio_inference_model")
    paths = ManagerPaths(root / "reconcile-config", root / "reconcile-state", root / "cache")
    transaction = replace(
        _transaction(bundle, root / "reconcile-target").bind_operations(
            {operation.operation_id: operation.stage_id}
        ).approve("sha256:" + "f" * 64),
        flow_contract_digest=bundle.contract_digest,
    )
    initial_request = operation_request(
        transaction, bundle, operation, phase="probe", probe_purpose="pre_apply", approval=None, attempt=1
    )
    interrupted = transaction.with_operation_status(
        operation.operation_id,
        CheckpointStatus.PENDING,
        attempt=attempt_record(
            _result(initial_request, CheckpointStatus.PENDING),
            stage_id=operation.stage_id,
            phase="pre_probe",
            attempt=1,
            owner_operation_id=operation.operation_id,
        ),
    ).with_operation_status(operation.operation_id, CheckpointStatus.APPLYING)
    return operation, paths, interrupted


def _assert_interrupted_apply_is_typed(operation: PlannedOperation, interrupted: Transaction) -> None:
    try:
        Transaction.from_dict(interrupted.to_dict())
    except OperationAttemptMismatch as raised:
        _require(
            raised.operation == operation.operation_id
            and raised.current is CheckpointStatus.APPLYING
            and raised.latest["checkpoint_status"] == CheckpointStatus.PENDING.value,
            "the exact applying/pending interruption is typed rather than flattened",
        )
    else:
        raise AssertionError("interrupted applying/pending record must be typed")


def _assert_preview_reconciliation(
    bundle: ContractBundle,
    operation: PlannedOperation,
    journal_path: Path,
) -> None:
    preview_bytes = journal_path.read_bytes()
    preview_transaction, mismatch = load_transaction_for_operation_reconciliation(journal_path)
    _require(preview_transaction is not None and mismatch is not None, "preview decodes typed interruption")
    adapter = _RouteSplitAdapter(operation, preconditions_verified=True)
    with patch.object(operation_executor, "invoke_adapter", adapter):
        reconciled = operation_executor.reconcile_interrupted_operation_attempt(
            bundle=bundle,
            operation=operation,
            transaction=preview_transaction,
            mismatch=mismatch,
            registry=_FixtureRegistry(),
            paths=None,
        )
    _require(
        journal_path.read_bytes() == preview_bytes
        and reconciled.transaction.operation_statuses[operation.operation_id]
        is CheckpointStatus.VERIFIED
        and reconciled.transaction.operation_attempts[-1]["phase"] == "pre_probe"
        and len(reconciled.transaction.operation_attempts) == 2,
        "valid manually installed artifact previews verified with no dry-run write",
    )


def _assert_unsatisfied_reconciliation(
    bundle: ContractBundle,
    operation: PlannedOperation,
    paths: ManagerPaths,
    journal_path: Path,
    interrupted: Transaction,
    failure: str,
) -> None:
    write_transaction(journal_path, interrupted)
    pending, mismatch = load_transaction_for_operation_reconciliation(journal_path)
    _require(pending is not None and mismatch is not None, f"{failure} starts resumable")

    def unsatisfied_adapter(
        _registry: object,
        *,
        runner: str,
        request: OperationRequest,
    ) -> OperationResult:
        _require(runner == operation.runner and request.dry_run, f"{failure} is read-only")
        return _result(request, CheckpointStatus.PENDING, error_kind=failure)

    with patch.object(operation_executor, "invoke_adapter", unsatisfied_adapter):
        persisted = operation_executor.reconcile_interrupted_operation_attempt(
            bundle=bundle,
            operation=operation,
            transaction=pending,
            mismatch=mismatch,
            registry=_FixtureRegistry(),
            paths=paths,
        )
    reloaded = load_transaction(journal_path)
    _require(
        persisted.probe.repair is not None
        and reloaded is not None
        and reloaded.operation_statuses[operation.operation_id] is CheckpointStatus.PENDING
        and reloaded.operation_attempts[-1]["repair"] is not None,
        f"{failure} remains unsatisfied with a resumable repair",
    )
    repeated, repeated_mismatch = load_transaction_for_operation_reconciliation(journal_path)
    _require(
        repeated is not None and repeated_mismatch is None and len(repeated.operation_attempts) == 2,
        f"{failure} approved recovery is idempotent after one evidence append",
    )
