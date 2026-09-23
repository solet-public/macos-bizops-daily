"""Manager registry discovery, transaction-aware status, and declared start orchestration."""

from __future__ import annotations

from pathlib import Path
from typing import cast

from .adapters import invoke_adapter
from .lifecycle_reconciliation import LifecycleReconciliation, reconcile_lifecycle
from .lifecycle_start import start_instance
from .models import CheckpointStatus, CommandResult, ExitCode, InstanceRecord, JsonValue
from .paths import ManagerPaths
from .registry import InstanceRegistry
from .transaction import Transaction, load_transaction


class LifecycleManager:
    def __init__(self, paths: ManagerPaths) -> None:
        self.paths = paths
        self.registry = InstanceRegistry(paths.registry_path)

    def list_instances(self) -> CommandResult:
        records = self.registry.list()
        return CommandResult(
            kind="instance_list",
            status="verified",
            message=f"{len(records)} manager-created instance(s).",
            exit_code=ExitCode.OK,
            data={"instances": [record.to_dict() for record in records]},
        )

    def status(self, name: str, *, candidate_target: Path | None = None) -> CommandResult:
        record = self.registry.get(name)
        target = candidate_target if record is None else Path(record.target)
        if record is not None and record.lifecycle_state == "setup_incomplete":
            return _provisional_status(record, load_transaction(self.paths.transaction_path(name)))
        reconciliation = reconcile_lifecycle(
            name=name,
            registered=record is not None,
            target=target,
        )
        if record is None:
            return _unregistered_status(name, reconciliation)

        target = Path(record.target)
        transaction = load_transaction(self.paths.transaction_path(name))
        if _runtime_disagreement_is_decisive(reconciliation, transaction):
            return _runtime_disagreement_status(
                record=record.to_dict(),
                transaction=transaction,
                reconciliation=reconciliation,
            )
        return _registered_transaction_status(
            name=name,
            record=record.to_dict(),
            target=target,
            transaction=transaction,
            reconciliation=reconciliation,
        )

    def start(self, name: str) -> CommandResult:
        return start_instance(
            paths=self.paths,
            registry=self.registry,
            name=name,
            invoke=invoke_adapter,
        )


def _provisional_status(record: InstanceRecord, transaction: Transaction | None) -> CommandResult:
    """Surface a transaction-backed target without representing it as verified."""

    name = record.name
    return CommandResult(
        kind="instance_status",
        status="awaiting_user",
        message=f"Managed instance {name!r} setup is incomplete; resume the same transaction.",
        exit_code=ExitCode.HUMAN_ACTION,
        error_kind="instance_setup_incomplete",
        repair=f"Resume with: solet create {name}",
        data={
            "instance": record.to_dict(),
            "lifecycle_state": "setup_incomplete",
            "transaction_status": None if transaction is None else transaction.status.value,
            "expected_router": {
                "name": record.expected_router_name,
                "socket": record.expected_router_socket,
                "port_range": record.expected_router_port_range,
            },
        },
    )


def _registered_transaction_status(
    *,
    name: str,
    record: dict[str, JsonValue],
    target: Path,
    transaction: Transaction | None,
    reconciliation: LifecycleReconciliation,
) -> CommandResult:
    status, error_kind, repair, cause = _transaction_public_state(
        name=name,
        target=target,
        transaction=transaction,
    )
    return CommandResult(
        kind="instance_status",
        status=status,
        message=f"Managed instance {name!r} transaction is {status}.",
        exit_code=_transaction_exit_code(status),
        error_kind=error_kind,
        repair=repair,
        data={
            "instance": record,
            "target_present": True,
            "transaction_status": None if transaction is None else transaction.status.value,
            "stage_progress": [] if transaction is None else _stage_progress(transaction),
            "cause": cause,
            "reconciliation": reconciliation.to_dict(),
        },
    )


def _runtime_disagreement_is_decisive(
    reconciliation: LifecycleReconciliation,
    transaction: Transaction | None,
) -> bool:
    if reconciliation.agreement:
        return False
    if reconciliation.runtime.state != "indeterminate":
        return True
    return transaction is None or transaction.status.value == CheckpointStatus.VERIFIED.value


def _transaction_public_state(
    *,
    name: str,
    target: Path,
    transaction: Transaction | None,
) -> tuple[str, str | None, str | None, dict[str, JsonValue] | None]:
    if transaction is None:
        return (
            "failed",
            "instance_transaction_missing",
            "Restore the manager transaction journal or recreate under review.",
            None,
        )
    status = transaction.status.value
    if status == CheckpointStatus.VERIFIED.value:
        return status, None, None, None
    error_kind, repair, cause = _transaction_cause(transaction, status)
    return (
        status,
        error_kind,
        repair or f"Resume with: solet create {name} --target {target} --dry-run",
        cause,
    )


def _transaction_exit_code(status: str) -> ExitCode:
    if status == CheckpointStatus.VERIFIED.value:
        return ExitCode.OK
    if status == CheckpointStatus.FAILED.value:
        return ExitCode.FAILED
    return ExitCode.HUMAN_ACTION


def _unregistered_status(
    name: str,
    reconciliation: LifecycleReconciliation,
) -> CommandResult:
    error_kinds = {
        "absent": "instance_absent",
        "unregistered_runtime_healthy": "instance_unregistered_runtime_healthy",
        "unregistered_runtime_dead": "instance_unregistered_runtime_dead",
        "unregistered_runtime_indeterminate": "instance_unregistered_runtime_indeterminate",
    }
    return CommandResult(
        kind="instance_status",
        status="awaiting_user",
        message=(
            f"Instance {name!r} lifecycle is {reconciliation.classification}; "
            f"{reconciliation.safe_next_action}"
        ),
        exit_code=ExitCode.HUMAN_ACTION,
        error_kind=error_kinds[reconciliation.classification],
        repair=reconciliation.safe_next_action,
        data={
            "name": name,
            "managed": False,
            "reconciliation": reconciliation.to_dict(),
        },
    )


def _runtime_disagreement_status(
    *,
    record: dict[str, JsonValue],
    transaction: Transaction | None,
    reconciliation: LifecycleReconciliation,
) -> CommandResult:
    error_kinds = {
        "registered_runtime_dead": "instance_registered_runtime_dead",
        "registered_target_absent": "instance_registered_target_absent",
        "registered_runtime_indeterminate": "instance_registered_runtime_indeterminate",
    }
    failed = reconciliation.runtime.state in {"dead", "absent"}
    return CommandResult(
        kind="instance_status",
        status="failed" if failed else "awaiting_user",
        message=(
            f"Instance {record['name']!r} lifecycle is {reconciliation.classification}; "
            f"{reconciliation.safe_next_action}"
        ),
        exit_code=ExitCode.FAILED if failed else ExitCode.HUMAN_ACTION,
        error_kind=error_kinds[reconciliation.classification],
        repair=reconciliation.safe_next_action,
        data={
            "instance": record,
            "target_present": reconciliation.runtime.state != "absent",
            "transaction_status": None if transaction is None else transaction.status.value,
            "stage_progress": [] if transaction is None else _stage_progress(transaction),
            "cause": None,
            "reconciliation": reconciliation.to_dict(),
        },
    )


def _transaction_cause(
    transaction: Transaction,
    status: str,
) -> tuple[str, str | None, dict[str, JsonValue] | None]:
    attempts = sorted(
        (*transaction.stage_probe_attempts, *transaction.operation_attempts),
        key=lambda attempt: str(attempt.get("recorded_at", "")),
        reverse=True,
    )
    for attempt in attempts:
        if attempt.get("checkpoint_status") != status:
            continue
        error_kind = attempt.get("error_kind")
        repair = attempt.get("repair")
        if isinstance(error_kind, str):
            cause: dict[str, JsonValue] = {
                "stage_id": cast(str, attempt["stage_id"]),
                "checkpoint_status": status,
            }
            probe_id = attempt.get("probe_id")
            boundary = attempt.get("boundary")
            operation_id = attempt.get("operation_id")
            if isinstance(probe_id, str) and isinstance(boundary, str):
                cause.update({"kind": "stage_probe", "boundary": boundary, "probe_id": probe_id})
            elif isinstance(operation_id, str):
                cause.update({"kind": "operation", "operation_id": operation_id})
            return error_kind, repair if isinstance(repair, str) else None, cause
    return f"instance_transaction_{status}", None, None


def _stage_progress(transaction: Transaction) -> list[JsonValue]:
    operations_by_stage: dict[str, list[JsonValue]] = {
        stage_id: [] for stage_id in transaction.stages
    }
    for operation_id, stage_id in transaction.operation_stages.items():
        operations_by_stage[stage_id].append(
            {
                "operation_id": operation_id,
                "checkpoint_status": transaction.operation_statuses[operation_id].value,
            }
        )
    return [
        {
            "stage_id": stage_id,
            "checkpoint_status": transaction.stages[stage_id].value,
            "entry": {
                probe_id: status.value
                for probe_id, status in transaction.stage_probe_statuses[stage_id]["entry"].items()
            },
            "operations": operations_by_stage[stage_id],
            "exit": {
                probe_id: status.value
                for probe_id, status in transaction.stage_probe_statuses[stage_id]["exit"].items()
            },
        }
        for stage_id in transaction.stages
    ]
