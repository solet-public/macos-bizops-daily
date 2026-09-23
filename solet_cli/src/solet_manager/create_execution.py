"""Approved-preview locking and create transaction orchestration."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from .adapters import AdapterRegistry, resolve_long_lived_python
from .completion_verifier import (
    rebind_completion_probes,
    resolved_completion_probe_ids,
)
from .config import CreateConfig
from .contract_reconciliation import recover_contract_reconciliation
from .contracts import (
    ContractBundle,
    target_contract_directory,
    validate_normalized_answers,
)
from .errors import ProbeDriftError, StateConflictError
from .flow import (
    SetupPlan,
    build_setup_plan,
    current_frontier_stage_ids,
    initial_probe_activations,
    initial_stage_probe_statuses,
    reconcile_stage_probe_activation,
)
from .journal_validation import validate_transaction_state
from .models import CheckpointStatus, CommandResult, ExitCode, InstanceRecord, JsonValue
from .operation_executor import approval_stale_preview, run_operations, stage_remediation_preview
from .operation_reconciliation import (
    load_transaction_for_operation_reconciliation,
    reconcile_pinned_operation_attempt,
)
from .operation_records import actions_by_operation
from .paths import ManagerPaths
from .plan_builder import remediation_plan
from .preview_engine import setup_preview
from .registry import InstanceRegistry
from .release_lock import SeedLock, load_seed_lock
from .resume_rules import (
    assert_decision_revision_allowed,
    merge_decision_inputs,
    read_only_auto_advance_is_pure,
    static_decision_selections,
)
from .source_acquisition import materialize_locked_seed
from .stage_boundaries import run_stage_boundaries, stage_stop_result
from .state_io import instance_lock
from .transaction import (
    Transaction,
    assert_resume_identity,
    canonical_sha256,
    load_transaction,
    write_transaction,
)

PreviewCall = Callable[..., CommandResult]


def execute_create(
    *,
    paths: ManagerPaths,
    contract_directory: Path | None,
    seed_lock_path: Path,
    registry: InstanceRegistry,
    preview_call: PreviewCall,
    config: CreateConfig,
    approved_fingerprint: str,
    decision_selections: dict[str, JsonValue] | None = None,
    decision_source: str = "flag",
    decision_sources: dict[str, str] | None = None,
    stage_limit: str | None = None,
) -> CommandResult:
    """Start or resume only after the current exact preview was approved."""

    selections, sources = merge_decision_inputs(
        config,
        decision_selections,
        decision_source,
        decision_sources,
    )
    preview = preview_call(
        config,
        decision_selections=selections,
        decision_source=decision_source,
        decision_sources=sources,
    )
    rejected = _rejected_preview(preview, approved_fingerprint)
    if rejected is not None:
        return rejected
    with instance_lock(paths.lock_path(config.name), create=True):
        if recover_contract_reconciliation(paths, config.name):
            locked_preview = preview_call(
                config,
                decision_selections=selections,
                decision_source=decision_source,
                decision_sources=sources,
            )
            rejected = _rejected_preview(locked_preview, approved_fingerprint)
            if rejected is not None:
                return rejected
        locked_preview = preview_call(
            config,
            decision_selections=selections,
            decision_source=decision_source,
            decision_sources=sources,
        )
        rejected = _rejected_preview(locked_preview, approved_fingerprint)
        if rejected is not None:
            return rejected
        return _execute_locked_create(
            paths=paths,
            contract_directory=contract_directory,
            seed_lock_path=seed_lock_path,
            registry=registry,
            config=config,
            locked_preview=locked_preview,
            approved_fingerprint=approved_fingerprint,
            selections=selections,
            decision_source=decision_source,
            sources=sources,
            stage_limit=stage_limit,
        )


def _rejected_preview(
    preview: CommandResult,
    approved_fingerprint: str,
) -> CommandResult | None:
    if preview.status != "preview_ready":
        return preview
    if str(preview.data["approval_fingerprint"]) != approved_fingerprint:
        return approval_stale_preview(preview)
    return None


def _execute_locked_create(
    *,
    paths: ManagerPaths,
    contract_directory: Path | None,
    seed_lock_path: Path,
    registry: InstanceRegistry,
    config: CreateConfig,
    locked_preview: CommandResult,
    approved_fingerprint: str,
    selections: dict[str, JsonValue],
    decision_source: str,
    sources: dict[str, str],
    stage_limit: str | None,
) -> CommandResult:
    transaction = _load_or_create_transaction(
        paths=paths,
        contract_directory=contract_directory,
        seed_lock_path=seed_lock_path,
        registry=registry,
        config=config,
        approved_fingerprint=approved_fingerprint,
        selections=selections,
        decision_source=decision_source,
        sources=sources,
        allow_operation_attempt_mismatch=True,
    )
    if not config.target.exists():
        return _materialize_target(paths, config, transaction)
    bundle = ContractBundle.load(
        source_revision=transaction.flow_source_revision,
        directory=target_contract_directory(config.target),
        expected_digest=transaction.flow_contract_digest,
        resume_compatibility=True,
    )
    mismatch = validate_transaction_state(
        transaction,
        canonical_answers_fingerprint=canonical_sha256(transaction.answers),
        allow_operation_attempt_mismatch=True,
    )
    if mismatch is not None:
        transaction = reconcile_pinned_operation_attempt(
            paths=paths,
            config=config,
            bundle=bundle,
            transaction=transaction,
            mismatch=mismatch,
            registry=AdapterRegistry(
                target=config.target,
                base_python=resolve_long_lived_python(),
            ),
            persist=True,
        )
    approved_frontier = _approved_frontier(locked_preview)
    if stage_limit is not None and approved_frontier != (stage_limit,):
        raise StateConflictError(
            "--resume-stage no longer matches the preview's sole executable frontier"
        )
    answers = _approved_answers(locked_preview, bundle)
    assert_decision_revision_allowed(transaction, answers, bundle)
    transaction = reconcile_stage_probe_activation(
        bundle,
        rebind_completion_probes(bundle, transaction.with_answers(answers), answers),
        answers,
    )
    adapter_registry = AdapterRegistry(
        target=config.target,
        base_python=resolve_long_lived_python(),
    )
    def refresh_preview() -> CommandResult:
        return setup_preview(
            paths=paths,
            registry=registry,
            config=config,
            bundle=bundle,
            decision_selections=config.decisions,
            decision_source="config",
            decision_sources=config.decision_sources,
        )
    transaction, stopped = _advance_to_approved_frontier(
        bundle=bundle,
        transaction=transaction,
        approved_frontier=approved_frontier,
        adapter_registry=adapter_registry,
        config=config,
        paths=paths,
        selections=selections,
        decision_source=decision_source,
        sources=sources,
        refresh_preview=refresh_preview,
    )
    if stopped is not None:
        return stopped
    plan, transaction, stopped = _approve_frontier(
        bundle=bundle,
        transaction=transaction,
        approved_frontier=approved_frontier,
        whole_plan_operation_stage_ids=set(bundle.stages),
        approved_fingerprint=approved_fingerprint,
        adapter_registry=adapter_registry,
        config=config,
        paths=paths,
        selections=selections,
        decision_source=decision_source,
        sources=sources,
    )
    if stopped is not None:
        return stopped
    return run_operations(
        bundle=bundle,
        plan=plan,
        transaction=transaction,
        approved_actions=actions_by_operation(
            locked_preview.data.get("planned_actions")
        ),
        config=config,
        paths=paths,
        instance_registry=registry,
        refresh_preview=refresh_preview,
        stop_after_stage=stage_limit,
    )


def _load_or_create_transaction(
    *,
    paths: ManagerPaths,
    contract_directory: Path | None,
    seed_lock_path: Path,
    registry: InstanceRegistry,
    config: CreateConfig,
    approved_fingerprint: str,
    selections: dict[str, JsonValue],
    decision_source: str,
    sources: dict[str, str],
    allow_operation_attempt_mismatch: bool = False,
) -> Transaction:
    input_fingerprint = canonical_sha256(config.to_identity_dict())
    if allow_operation_attempt_mismatch:
        transaction, _mismatch = load_transaction_for_operation_reconciliation(
            paths.transaction_path(config.name)
        )
    else:
        transaction = load_transaction(paths.transaction_path(config.name))
    if transaction is not None:
        assert_resume_identity(
            transaction,
            name=config.name,
            target=config.target,
            input_fingerprint=input_fingerprint,
        )
        installed_seed = load_seed_lock(seed_lock_path)
        if transaction.seed.identity_dict() != installed_seed.identity_dict():
            raise StateConflictError(
                "retained transaction seed does not match the installed seed lock: "
                f"transaction={_seed_label(transaction.seed)}; "
                f"installed={_seed_label(installed_seed)}",
                repair=(
                    "Resume using the manager seed recorded by this transaction. "
                    "A different installed seed cannot reuse this transaction."
                ),
            )
        return transaction
    seed = load_seed_lock(seed_lock_path)
    bundle = ContractBundle.load(
        source_revision=seed.commit,
        directory=contract_directory,
    )
    plan = build_setup_plan(
        bundle=bundle,
        config=config,
        seed=seed,
        journal_path=paths.transaction_path(config.name),
        prospective_consents=False,
        decision_selections=static_decision_selections(bundle, selections),
        decision_source=decision_source,
        decision_sources=sources,
    )
    created = Transaction.create(
        name=config.name,
        target=config.target,
        input_fingerprint=input_fingerprint,
        answers=plan.answers,
        seed=seed,
        flow_id=bundle.flow_id,
        flow_source_revision=bundle.source_revision,
        flow_contract_digest=bundle.contract_digest,
        stage_ids=_ordered_stage_ids(bundle),
        completion_probe_ids=resolved_completion_probe_ids(bundle, plan.answers),
        stage_probe_statuses=initial_stage_probe_statuses(bundle, plan.answers),
        probe_activations=initial_probe_activations(bundle, plan.answers),
    ).approve(approved_fingerprint)
    orphaned_record = registry.get(config.name)
    if orphaned_record is not None:
        registry.discard_orphan(orphaned_record)
    write_transaction(paths.transaction_path(config.name), created)
    registry.add(_provisional_record(created))
    return created


def _seed_label(seed: SeedLock) -> str:
    """Return an actionable immutable seed identifier for a resume conflict."""

    return f"{seed.repository}@{seed.source_ref()} ({seed.commit})"


def _ordered_stage_ids(bundle: ContractBundle) -> tuple[str, ...]:
    return tuple(
        stage_id
        for stage_id, _definition in sorted(
            bundle.stages.items(),
            key=lambda item: _stage_sequence(item[1]),
        )
    )


def _provisional_record(transaction: Transaction) -> InstanceRecord:
    """Project the durable transaction into an honest, resumable manager record."""

    target = Path(transaction.target)
    name = transaction.name
    return InstanceRecord(
        name=name,
        target=transaction.target,
        launcher=str(target / "client" / "bin" / name),
        seed_repository=transaction.seed.repository,
        seed_tag=transaction.seed.release_tag,
        seed_commit=transaction.seed.commit,
        seed_tree_hash=transaction.seed.tree_hash,
        profile=transaction.seed.profile,
        flow_id=transaction.flow_id,
        flow_source_revision=transaction.flow_source_revision,
        flow_contract_digest=transaction.flow_contract_digest,
        created_at=transaction.created_at,
        updated_at=transaction.updated_at,
        lifecycle_state="setup_incomplete",
        input_fingerprint=transaction.input_fingerprint,
        expected_router_name=name,
        expected_router_socket=str(Path.home() / ".ananta/runtime" / f"{name}.router.sock"),
        expected_router_port_range="8800-8999",
    )


def _stage_sequence(definition: dict[str, JsonValue]) -> int:
    value = definition.get("sequence")
    if isinstance(value, bool) or not isinstance(value, int):
        raise StateConflictError("setup stage sequence must be an integer")
    return value


def _materialize_target(
    paths: ManagerPaths,
    config: CreateConfig,
    transaction: Transaction,
) -> CommandResult:
    materialize_locked_seed(
        transaction.seed,
        config.target,
        cache_dir=paths.acquisition_dir,
    )
    ContractBundle.load(
        source_revision=transaction.flow_source_revision,
        directory=target_contract_directory(config.target),
        expected_digest=transaction.flow_contract_digest,
    )
    return CommandResult(
        kind="create",
        status="awaiting_user",
        message="Locked seed materialized; target-local probes now require a fresh preview.",
        exit_code=ExitCode.HUMAN_ACTION,
        error_kind="probe_drift",
        repair=(
            "Run the same create command again, review the target-local preview, and approve it."
        ),
        data={
            "name": config.name,
            "target": str(config.target),
            "transaction": str(paths.transaction_path(config.name)),
            "seed_commit": transaction.seed.commit,
            "flow_contract_digest": transaction.flow_contract_digest,
        },
    )


def _approved_frontier(preview: CommandResult) -> tuple[str, ...]:
    value = preview.data.get("frontier")
    if isinstance(value, list) and all(isinstance(item, str) for item in value):
        return tuple(str(item) for item in value)
    return ()


def _approved_answers(
    preview: CommandResult,
    bundle: ContractBundle,
) -> dict[str, JsonValue]:
    value = preview.data.get("normalized_answers")
    if not isinstance(value, dict):
        raise StateConflictError("approved preview lacks normalized answers")
    validate_normalized_answers(bundle, value)
    return value


def _advance_to_approved_frontier(
    *,
    bundle: ContractBundle,
    transaction: Transaction,
    approved_frontier: tuple[str, ...],
    adapter_registry: AdapterRegistry,
    config: CreateConfig,
    paths: ManagerPaths,
    selections: dict[str, JsonValue],
    decision_source: str,
    sources: dict[str, str],
    refresh_preview: Callable[[], CommandResult],
) -> tuple[Transaction, CommandResult | None]:
    current_transaction = transaction
    for _round in range(len(bundle.stages) + 1):
        current = current_frontier_stage_ids(
            bundle,
            current_transaction,
            current_transaction.answers,
        )
        if current == approved_frontier:
            return current_transaction, None
        plan = _durable_read_only_plan(
            bundle=bundle,
            transaction=current_transaction,
            frontier=current,
            config=config,
            paths=paths,
            selections=selections,
            decision_source=decision_source,
            sources=sources,
        )
        if not read_only_auto_advance_is_pure(current_transaction.answers, plan):
            raise ProbeDriftError(
                "approved frontier differs from the durable read-only frontier"
            )
        current_transaction = rebind_completion_probes(
            bundle,
            current_transaction.with_answers(plan.answers),
            plan.answers,
        )
        current_transaction, stopped = _cross_boundaries(
            bundle=bundle,
            transaction=current_transaction,
            registry=adapter_registry,
            stage_ids=current,
            paths=paths,
            refresh_preview=refresh_preview,
        )
        if stopped is not None:
            return current_transaction, stopped
    raise StateConflictError("durable stage frontier did not converge")


def _durable_read_only_plan(
    *,
    bundle: ContractBundle,
    transaction: Transaction,
    frontier: tuple[str, ...],
    config: CreateConfig,
    paths: ManagerPaths,
    selections: dict[str, JsonValue],
    decision_source: str,
    sources: dict[str, str],
) -> SetupPlan:
    frontier_set = set(frontier)
    return build_setup_plan(
        bundle=bundle,
        config=config,
        seed=transaction.seed,
        journal_path=paths.transaction_path(config.name),
        prospective_consents=False,
        decision_selections=selections,
        recorded_answers=transaction.answers,
        decision_source=decision_source,
        decision_sources=sources,
        resolution_stage_ids=frontier_set,
        operation_stage_ids=frontier_set,
    )


def _cross_boundaries(
    *,
    bundle: ContractBundle,
    transaction: Transaction,
    registry: AdapterRegistry,
    stage_ids: tuple[str, ...],
    paths: ManagerPaths,
    refresh_preview: Callable[[], CommandResult],
) -> tuple[Transaction, CommandResult | None]:
    updated = transaction
    for boundary in ("entry", "exit"):
        updated, _observations, failures = run_stage_boundaries(
            bundle=bundle,
            transaction=updated,
            registry=registry,
            stage_ids=stage_ids,
            boundary=boundary,
            answers=updated.answers,
            persist_path=paths.transaction_path(updated.name),
        )
        if failures:
            outcome = stage_remediation_preview(
                failure=failures[0],
                transaction=updated,
                paths=paths,
                refresh_preview=refresh_preview,
            )
            return outcome.transaction, outcome.terminal_result
    return updated, None


def _approve_frontier(
    *,
    bundle: ContractBundle,
    transaction: Transaction,
    approved_frontier: tuple[str, ...],
    whole_plan_operation_stage_ids: set[str],
    approved_fingerprint: str,
    adapter_registry: AdapterRegistry,
    config: CreateConfig,
    paths: ManagerPaths,
    selections: dict[str, JsonValue],
    decision_source: str,
    sources: dict[str, str],
) -> tuple[SetupPlan, Transaction, CommandResult | None]:
    plan = build_setup_plan(
        bundle=bundle,
        config=config,
        seed=transaction.seed,
        journal_path=paths.transaction_path(config.name),
        prospective_consents=True,
        decision_selections=selections,
        recorded_answers=transaction.answers,
        decision_source=decision_source,
        decision_sources=sources,
        resolution_stage_ids=set(approved_frontier),
        operation_stage_ids=set(approved_frontier),
    )
    whole_plan = build_setup_plan(
        bundle=bundle,
        config=config,
        seed=transaction.seed,
        journal_path=paths.transaction_path(config.name),
        prospective_consents=True,
        decision_selections=selections,
        recorded_answers=transaction.answers,
        decision_source=decision_source,
        decision_sources=sources,
        resolution_stage_ids=set(approved_frontier),
        operation_stage_ids=whole_plan_operation_stage_ids,
    )
    assert_decision_revision_allowed(transaction, plan.answers, bundle)
    updated = rebind_completion_probes(
        bundle,
        transaction.with_answers(plan.answers),
        plan.answers,
    ).approve(approved_fingerprint)
    updated, _observations, failures = run_stage_boundaries(
        bundle=bundle,
        transaction=updated,
        registry=adapter_registry,
        stage_ids=approved_frontier,
        boundary="entry",
        answers=updated.answers,
        persist_path=paths.transaction_path(config.name),
    )
    if failures:
        if not failures[0].remediation_operation_ids:
            return plan, updated, stage_stop_result(failures[0].identity, updated)
        plan = remediation_plan(
            bundle=bundle,
            stage_id=failures[0].stage_id,
            operation_ids=failures[0].remediation_operation_ids,
            plan=plan,
        )
    else:
        resumed_stage_ids = _failed_exit_stage_ids(updated, approved_frontier)
        if resumed_stage_ids:
            updated, _observations, failures = run_stage_boundaries(
                bundle=bundle,
                transaction=updated,
                registry=adapter_registry,
                stage_ids=resumed_stage_ids,
                boundary="exit",
                answers=updated.answers,
                persist_path=paths.transaction_path(updated.name),
            )
            if failures:
                if not failures[0].remediation_operation_ids:
                    return plan, updated, stage_stop_result(failures[0].identity, updated)
                plan = remediation_plan(
                    bundle=bundle,
                    stage_id=failures[0].stage_id,
                    operation_ids=failures[0].remediation_operation_ids,
                    plan=plan,
                )
    operation_stages = {
        operation.operation_id: operation.stage_id
        for operation in whole_plan.operations
    }
    operation_stages.update(
        {
            operation.operation_id: operation.stage_id
            for operation in plan.operations
        }
    )
    updated = updated.bind_operations(operation_stages)
    write_transaction(paths.transaction_path(config.name), updated)
    return plan, updated, None


def _failed_exit_stage_ids(
    transaction: Transaction,
    stage_ids: tuple[str, ...],
) -> tuple[str, ...]:
    incomplete = {
        CheckpointStatus.AWAITING_USER,
        CheckpointStatus.BLOCKED,
        CheckpointStatus.FAILED,
    }
    return tuple(
        stage_id
        for stage_id in stage_ids
        if any(
            status in incomplete
            for status in transaction.stage_probe_statuses[stage_id]["exit"].values()
        )
    )
