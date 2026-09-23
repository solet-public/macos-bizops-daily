"""Plan-scoped recovery for the one safe interrupted-apply journal shape."""

from __future__ import annotations

from pathlib import Path

from .adapters import AdapterRegistry
from .config import CreateConfig
from .contracts import ContractBundle
from .errors import OperationAttemptMismatch
from .flow import build_setup_plan
from .journal_validation import parse_transaction_fields, validate_transaction_state
from .models import JsonValue
from .operation_executor import reconcile_interrupted_operation_attempt
from .paths import ManagerPaths
from .state_io import load_json_object
from .transaction import Transaction, _transaction_from_parsed, canonical_sha256


def load_transaction_for_operation_reconciliation(
    path: Path,
) -> tuple[Transaction | None, OperationAttemptMismatch | None]:
    """Load a journal only long enough to resolve the typed recoverable mismatch."""

    raw = load_json_object(path, missing_ok=True)
    if raw is None:
        return None, None
    return _decode_transaction_for_operation_reconciliation(raw)


def _decode_transaction_for_operation_reconciliation(
    raw: dict[str, JsonValue],
) -> tuple[Transaction, OperationAttemptMismatch | None]:
    """Decode a structurally valid journal while retaining one typed mismatch."""

    transaction = _transaction_from_parsed(parse_transaction_fields(raw))
    mismatch = validate_transaction_state(
        transaction,
        canonical_answers_fingerprint=canonical_sha256(transaction.answers),
        allow_operation_attempt_mismatch=True,
    )
    return transaction, mismatch


def reconcile_pinned_operation_attempt(
    *,
    paths: ManagerPaths,
    config: CreateConfig,
    bundle: ContractBundle,
    transaction: Transaction,
    mismatch: OperationAttemptMismatch,
    registry: AdapterRegistry,
    persist: bool,
) -> Transaction:
    """Probe the transaction's own pinned operation, optionally persisting it."""

    operation = _reconciliation_operation(paths, config, bundle, transaction, mismatch)
    return reconcile_interrupted_operation_attempt(
        bundle=bundle,
        operation=operation,
        transaction=transaction,
        mismatch=mismatch,
        registry=registry,
        paths=paths if persist else None,
    ).transaction


def _reconciliation_operation(
    paths: ManagerPaths,
    config: CreateConfig,
    bundle: ContractBundle,
    transaction: Transaction,
    mismatch: OperationAttemptMismatch,
):
    stage_id = transaction.operation_stages.get(mismatch.operation)
    if stage_id is None:
        raise mismatch
    plan = build_setup_plan(
        bundle=bundle,
        config=config,
        seed=transaction.seed,
        journal_path=paths.transaction_path(config.name),
        recorded_answers=transaction.answers,
        operation_stage_ids={stage_id},
    )
    operations = [
        operation for operation in plan.operations if operation.operation_id == mismatch.operation
    ]
    if len(operations) != 1:
        raise mismatch
    return operations[0]
