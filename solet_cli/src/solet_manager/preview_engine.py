"""No-write create preview orchestration."""

from __future__ import annotations

import shlex
from dataclasses import dataclass
from pathlib import Path
from typing import cast

from .adapters import (
    AdapterRegistry,
    OperationResult,
    invoke_adapter,
    resolve_long_lived_python,
)
from .config import CreateConfig
from .config_loading import validate_name_path_collision
from .contracts import (
    ContractBundle,
    target_contract_directory,
    validate_normalized_answers,
)
from .decision_discovery import (
    discover_and_qualify_decisions,
    eligible_discovered_decision_ids,
)
from .errors import (
    ContractError,
    OperationAttemptMismatch,
    StateConflictError,
    StateError,
)
from .flow import (
    PlannedOperation,
    SetupPlan,
    active_discovered_decision_ids,
    approval_fingerprint,
    build_setup_plan,
    canonical_planned_actions,
    current_frontier_stage_ids,
    order_decision_prompts,
    reconcile_stage_probe_activation,
    render_consents,
    render_decisions,
    static_decision_prompts,
    validate_stage_probe_state,
)
from .inference_probe_policy import advisory_inference_probe_result
from .models import CheckpointStatus, CommandResult, ExitCode, JsonValue
from .operation_reconciliation import (
    load_transaction_for_operation_reconciliation,
    reconcile_pinned_operation_attempt,
)
from .operation_records import next_attempt, operation_probe_request, operation_request
from .paths import ManagerPaths
from .permission_preflight import (
    permission_preflight_fingerprint_content,
    render_permission_preflight,
)
from .plan_builder import remediation_plan
from .registry import InstanceRegistry
from .release_lock import SeedLock, load_seed_lock
from .resume_rules import (
    assert_decision_revision_allowed,
    merge_decision_inputs,
)
from .stage_boundaries import (
    BoundaryFailure,
    canonical_stage_probe_observation,
    run_stage_boundaries,
    stage_probe_status_dict,
)
from .transaction import (
    Transaction,
    assert_resume_identity,
    canonical_sha256,
)


@dataclass(frozen=True)
class PreviewRound:
    """One prospective frontier evaluation and its no-write observations."""

    transaction: Transaction
    answers: dict[str, JsonValue]
    frontier: tuple[str, ...]
    plan: SetupPlan | None
    stage_observations: dict[str, JsonValue]
    decision_observations: dict[str, JsonValue]
    decision_prompts: list[JsonValue]
    decision_errors: list[JsonValue]
    unresolved_actions: list[str]
    operation_results: dict[str, OperationResult]
    terminal: bool


def preview_create(
    *,
    paths: ManagerPaths,
    contract_directory: Path | None,
    seed_lock_path: Path,
    registry: InstanceRegistry,
    config: CreateConfig,
    decision_selections: dict[str, JsonValue] | None = None,
    decision_source: str = "flag",
    decision_sources: dict[str, str] | None = None,
) -> CommandResult:
    """Return the exact no-write preview currently knowable by the manager."""

    selections, sources = merge_decision_inputs(
        config,
        decision_selections,
        decision_source,
        decision_sources,
    )
    transaction, _mismatch = load_transaction_for_operation_reconciliation(
        paths.transaction_path(config.name)
    )
    if transaction is not None:
        return _resume_preview(
            paths=paths,
            registry=registry,
            config=config,
            transaction=transaction,
            selections=selections,
            decision_source=decision_source,
            sources=sources,
        )
    return _new_preview(
        paths=paths,
        contract_directory=contract_directory,
        seed_lock_path=seed_lock_path,
        config=config,
        selections=selections,
        decision_source=decision_source,
        sources=sources,
    )


def _resume_preview(
    *,
    paths: ManagerPaths,
    registry: InstanceRegistry,
    config: CreateConfig,
    transaction: Transaction,
    selections: dict[str, JsonValue],
    decision_source: str,
    sources: dict[str, str],
) -> CommandResult:
    assert_resume_identity(
        transaction,
        name=config.name,
        target=config.target,
        input_fingerprint=canonical_sha256(config.to_identity_dict()),
    )
    validate_name_path_collision(
        name=config.name,
        target=config.target,
        is_matching_resume=True,
    )
    if not config.target.exists():
        bundle = ContractBundle.load(
            source_revision=transaction.flow_source_revision,
            expected_digest=transaction.flow_contract_digest,
            resume_compatibility=True,
        )
        plan = build_setup_plan(
            bundle=bundle,
            config=config,
            seed=transaction.seed,
            journal_path=paths.transaction_path(config.name),
            prospective_consents=False,
            decision_selections=selections,
            recorded_answers=transaction.answers,
            decision_source=decision_source,
            decision_sources=sources,
        )
        decisions = cast(dict[str, JsonValue], plan.answers["decisions"])
        deferred = active_discovered_decision_ids(bundle, decisions)
        unresolved_static = tuple(
            decision_id
            for decision_id in plan.unresolved_decisions
            if decision_id not in deferred
        )
        return acquisition_preview(
            config=config,
            seed=transaction.seed,
            flow_id=bundle.flow_id,
            flow_source_revision=bundle.source_revision,
            flow_contract_digest=bundle.contract_digest,
            deferred_decisions=deferred,
            rendered_decisions=render_decisions(bundle, plan),
            unresolved_decisions=unresolved_static,
            decision_prompts=static_decision_prompts(bundle, plan),
            permission_preflight=render_permission_preflight(bundle, plan, {}),
        )
    bundle = ContractBundle.load(
        source_revision=transaction.flow_source_revision,
        directory=target_contract_directory(config.target),
        expected_digest=transaction.flow_contract_digest,
        resume_compatibility=True,
    )
    return setup_preview(
        paths=paths,
        registry=registry,
        config=config,
        bundle=bundle,
        decision_selections=selections,
        decision_source=decision_source,
        decision_sources=sources,
    )


def _new_preview(
    *,
    paths: ManagerPaths,
    contract_directory: Path | None,
    seed_lock_path: Path,
    config: CreateConfig,
    selections: dict[str, JsonValue],
    decision_source: str,
    sources: dict[str, str],
) -> CommandResult:
    validate_name_path_collision(
        name=config.name,
        target=config.target,
        is_matching_resume=False,
    )
    if config.target.exists():
        if _is_empty_directory(config.target):
            quoted_target = shlex.quote(str(config.target))
            raise StateConflictError(
                "new create expected a target path that does not exist; found "
                f"an empty directory at {config.target}",
                repair=(
                    f"Run `rmdir -- {quoted_target}`, then rerun the same create "
                    "command; or choose a target path that does not exist."
                ),
            )
        raise StateConflictError(
            "new create expected a target path that does not exist; found an "
            f"existing non-empty or non-directory path at {config.target}",
            repair=(
                "Choose a target path that does not exist. To diagnose this path "
                f"without changing it, run `solet inspect --target "
                f"{shlex.quote(str(config.target))}`."
            ),
        )
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
        decision_selections=selections,
        decision_source=decision_source,
        decision_sources=sources,
    )
    decisions = cast(dict[str, JsonValue], plan.answers["decisions"])
    deferred = active_discovered_decision_ids(bundle, decisions)
    unresolved_static = tuple(
        decision_id
        for decision_id in plan.unresolved_decisions
        if decision_id not in deferred
    )
    return acquisition_preview(
        config=config,
        seed=seed,
        flow_id=bundle.flow_id,
        flow_source_revision=bundle.source_revision,
        flow_contract_digest=bundle.contract_digest,
        deferred_decisions=deferred,
        rendered_decisions=render_decisions(bundle, plan),
        unresolved_decisions=unresolved_static,
        decision_prompts=static_decision_prompts(bundle, plan),
        permission_preflight=render_permission_preflight(bundle, plan, {}),
    )


def _is_empty_directory(target: Path) -> bool:
    if not target.is_dir():
        return False
    try:
        return next(target.iterdir(), None) is None
    except OSError:
        return False


def acquisition_preview(
    *,
    config: CreateConfig,
    seed: SeedLock,
    flow_id: str,
    flow_source_revision: str,
    flow_contract_digest: str,
    deferred_decisions: tuple[str, ...],
    rendered_decisions: list[JsonValue],
    unresolved_decisions: tuple[str, ...],
    decision_prompts: list[JsonValue],
    permission_preflight: dict[str, JsonValue] | None,
) -> CommandResult:
    if permission_preflight is None:
        raise ContractError("acquisition preview lacks permission preflight")
    action: dict[str, JsonValue] = {
        "id": "manager.acquire_locked_seed",
        "title": f"Materialize locked seed {_seed_identity_label(seed)}",
        "mutation_kind": "directory_create",
        "target": str(config.target),
        "requires_confirmation": True,
        "condition_or_evidence_ref": "target_absent",
    }
    resolved_config = config.to_public_dict()
    resolved_config["decisions"] = {
        decision_id: value
        for decision_id, value in config.decisions.items()
        if decision_id not in deferred_decisions
    }
    fingerprint = canonical_sha256(
        {
            "flow_id": flow_id,
            "flow_source_revision": flow_source_revision,
            "flow_contract_digest": flow_contract_digest,
            "seed_identity": seed.identity_dict(),
            "name": config.name,
            "target": str(config.target),
            "resolved_config": resolved_config,
            "manager_actions": [action],
            "permission_preflight": permission_preflight_fingerprint_content(
                permission_preflight
            ),
        }
    )
    blocking = bool(unresolved_decisions)
    data = _acquisition_data(
        config=config,
        seed=seed,
        flow_contract_digest=flow_contract_digest,
        action=action,
        rendered_decisions=rendered_decisions,
        unresolved_decisions=unresolved_decisions,
        deferred_decisions=deferred_decisions,
        decision_prompts=decision_prompts,
        permission_preflight=permission_preflight,
    )
    data["approval_fingerprint"] = fingerprint
    return CommandResult(
        kind="create_preview",
        status="awaiting_user" if blocking else "preview_ready",
        message=f'Create Solet "{config.name}" at {config.target}',
        exit_code=ExitCode.HUMAN_ACTION if blocking else ExitCode.OK,
        error_kind="decisions_required" if blocking else None,
        repair=(
            "Add these exact [decisions] keys: "
            + ", ".join(unresolved_decisions)
            + "; or use interactive selection."
            if blocking
            else None
        ),
        data=data,
    )


def _seed_identity_label(seed: SeedLock) -> str:
    return seed.release_tag if seed.release_tag is not None else f"commit:{seed.commit}"


def _acquisition_data(
    *,
    config: CreateConfig,
    seed: SeedLock,
    flow_contract_digest: str,
    action: dict[str, JsonValue],
    rendered_decisions: list[JsonValue],
    unresolved_decisions: tuple[str, ...],
    deferred_decisions: tuple[str, ...],
    decision_prompts: list[JsonValue],
    permission_preflight: dict[str, JsonValue] | None,
) -> dict[str, JsonValue]:
    return {
        "name": config.name,
        "target": str(config.target),
        "source": seed.identity_dict(),
        "flow_contract_digest": flow_contract_digest,
        "autostart": config.autostart,
        "operations": [],
        "manager_actions": [action],
        "planned_actions": [],
        "consents": [],
        "decisions": rendered_decisions,
        "unresolved_decisions": list(unresolved_decisions),
        "deferred_setup_decisions": list(deferred_decisions),
        "unresolved_consents": [],
        "unresolved_actions": [],
        "decision_prompts": decision_prompts,
        "decision_errors": [
            {
                "id": decision_id,
                "error_kind": "decision_selection_required",
            }
            for decision_id in unresolved_decisions
        ],
        "probe_observations": {"target_state": "absent"},
        "permission_preflight": permission_preflight,
        "dry_run_writes": 0,
    }


def setup_preview(
    *,
    paths: ManagerPaths,
    registry: InstanceRegistry,
    config: CreateConfig,
    bundle: ContractBundle,
    decision_selections: dict[str, JsonValue] | None,
    decision_source: str,
    decision_sources: dict[str, str] | None,
) -> CommandResult:
    transaction, mismatch = _load_setup_transaction_for_operation_reconciliation(
        paths, config, bundle
    )
    initial = PreviewRound(
        transaction=transaction,
        answers=transaction.answers,
        frontier=(),
        plan=None,
        stage_observations={},
        decision_observations={},
        decision_prompts=[],
        decision_errors=[],
        unresolved_actions=[],
        operation_results={},
        terminal=False,
    )
    adapter_registry = AdapterRegistry(
        target=config.target,
        base_python=resolve_long_lived_python(),
    )
    if mismatch is not None:
        transaction = reconcile_pinned_operation_attempt(
            paths=paths,
            config=config,
            bundle=bundle,
            transaction=transaction,
            mismatch=mismatch,
            registry=adapter_registry,
            persist=False,
        )
    evaluated = _converge_preview(
        paths=paths,
        config=config,
        bundle=bundle,
        durable=transaction,
        registry=adapter_registry,
        initial=initial,
        decision_selections=decision_selections,
        decision_source=decision_source,
        decision_sources=decision_sources,
    )
    plan = evaluated.plan or _empty_frontier_plan(
        paths,
        config,
        bundle,
        transaction,
        evaluated.answers,
    )
    return _render_setup_preview(
        registry=registry,
        config=config,
        bundle=bundle,
        transaction=transaction,
        evaluated=evaluated,
        plan=plan,
    )


def _load_setup_transaction(  # pyright: ignore[reportUnusedFunction]
    paths: ManagerPaths,
    config: CreateConfig,
    bundle: ContractBundle,
) -> Transaction:
    """Load the ordinary strict preview transaction compatibility surface."""

    transaction, _mismatch = _load_setup_transaction_for_operation_reconciliation(
        paths, config, bundle
    )
    if _mismatch is not None:
        raise _mismatch
    return transaction


def _load_setup_transaction_for_operation_reconciliation(
    paths: ManagerPaths,
    config: CreateConfig,
    bundle: ContractBundle,
) -> tuple[Transaction, OperationAttemptMismatch | None]:
    transaction, mismatch = load_transaction_for_operation_reconciliation(
        paths.transaction_path(config.name)
    )
    if transaction is None:
        raise StateConflictError("materialized target lacks its manager transaction")
    try:
        validate_normalized_answers(bundle, transaction.answers)
    except ContractError as exc:
        raise StateError(
            f"recorded normalized answers are corrupt: {exc}",
            repair="Restore the pinned transaction journal from a trusted copy.",
        ) from exc
    transaction = reconcile_stage_probe_activation(
        bundle,
        transaction,
        transaction.answers,
    )
    validate_stage_probe_state(bundle, transaction, transaction.answers)
    return transaction, mismatch


def _converge_preview(
    *,
    paths: ManagerPaths,
    config: CreateConfig,
    bundle: ContractBundle,
    durable: Transaction,
    registry: AdapterRegistry,
    initial: PreviewRound,
    decision_selections: dict[str, JsonValue] | None,
    decision_source: str,
    decision_sources: dict[str, str] | None,
) -> PreviewRound:
    current = initial
    for _round in range(len(bundle.stages) + 1):
        current = _evaluate_frontier(
            paths=paths,
            config=config,
            bundle=bundle,
            durable=durable,
            registry=registry,
            previous=current,
            decision_selections=decision_selections,
            decision_source=decision_source,
            decision_sources=decision_sources,
        )
        if current.terminal:
            return current
    raise StateConflictError("stage frontier did not converge")


def _evaluate_frontier(
    *,
    paths: ManagerPaths,
    config: CreateConfig,
    bundle: ContractBundle,
    durable: Transaction,
    registry: AdapterRegistry,
    previous: PreviewRound,
    decision_selections: dict[str, JsonValue] | None,
    decision_source: str,
    decision_sources: dict[str, str] | None,
) -> PreviewRound:
    frontier = current_frontier_stage_ids(bundle, previous.transaction, previous.answers)
    if not frontier:
        return _empty_round(previous, frontier)
    plan = build_setup_plan(
        bundle=bundle,
        config=config,
        seed=durable.seed,
        journal_path=paths.transaction_path(config.name),
        prospective_consents=True,
        decision_selections=(
            config.decisions if decision_selections is None else decision_selections
        ),
        recorded_answers=previous.answers,
        decision_source=decision_source,
        decision_sources=(
            config.decision_sources if decision_sources is None else decision_sources
        ),
        resolution_stage_ids=set(frontier),
        operation_stage_ids=set(frontier),
    )
    # Discovery resolves only at each decision's declared stage.  A runnable
    # adapter vector is necessary to execute that discovery, not permission to
    # move it ahead of the declared frontier.
    eligible_discovery_ids = eligible_discovered_decision_ids(
        bundle=bundle,
        plan=plan,
        frontier_stage_ids=set(frontier),
    )
    assert_decision_revision_allowed(durable, plan.answers, bundle)
    working = reconcile_stage_probe_activation(
        bundle,
        previous.transaction,
        plan.answers,
    )
    discovered, decision_errors, decision_observations = (
        discover_and_qualify_decisions(
            bundle=bundle,
            transaction=durable,
            plan=plan,
            registry=registry,
            eligible_decision_ids=eligible_discovery_ids,
        )
    )
    decision_prompts = order_decision_prompts(
        bundle,
        static_decision_prompts(bundle, plan),
        discovered,
    )
    working, observations, entry_failures = run_stage_boundaries(
        bundle=bundle,
        transaction=working,
        registry=registry,
        stage_ids=frontier,
        boundary="entry",
        answers=plan.answers,
        persist_path=None,
    )
    return _evaluate_frontier_boundaries(
        durable=durable,
        bundle=bundle,
        registry=registry,
        plan=plan,
        frontier=frontier,
        working=working,
        observations=observations,
        decision_observations=decision_observations,
        decision_prompts=decision_prompts,
        decision_errors=decision_errors,
        entry_failures=entry_failures,
    )


def _evaluate_frontier_boundaries(
    *,
    durable: Transaction,
    bundle: ContractBundle,
    registry: AdapterRegistry,
    plan: SetupPlan,
    frontier: tuple[str, ...],
    working: Transaction,
    observations: dict[str, JsonValue],
    decision_observations: dict[str, JsonValue],
    decision_prompts: list[JsonValue],
    decision_errors: list[JsonValue],
    entry_failures: list[BoundaryFailure],
) -> PreviewRound:
    terminal_failures = _terminal_boundary_failures(entry_failures)
    unresolved = [failure.identity for failure in terminal_failures]
    if _frontier_is_blocked(plan, decision_errors, terminal_failures):
        unresolved.extend(operation.operation_id for operation in plan.operations)
        return _round_result(
            working,
            plan,
            frontier,
            observations,
            decision_observations,
            decision_prompts,
            decision_errors,
            unresolved,
            {},
            terminal=True,
        )
    if entry_failures:
        return _remediation_round(
            durable=durable,
            bundle=bundle,
            registry=registry,
            plan=plan,
            failure=entry_failures[0],
            working=working,
            frontier=frontier,
            observations=observations,
            decision_observations=decision_observations,
            decision_prompts=decision_prompts,
            decision_errors=decision_errors,
            unresolved=unresolved,
        )
    if plan.operations:
        results, operation_failures = _probe_operations(
            durable,
            bundle,
            plan,
            registry,
        )
        unresolved.extend(operation_failures)
        return _round_result(
            working,
            plan,
            frontier,
            observations,
            decision_observations,
            decision_prompts,
            decision_errors,
            unresolved,
            results,
            terminal=True,
        )
    return _evaluate_exit_frontier(
        durable=durable,
        bundle=bundle,
        registry=registry,
        plan=plan,
        frontier=frontier,
        working=working,
        observations=observations,
        decision_observations=decision_observations,
        decision_prompts=decision_prompts,
        decision_errors=decision_errors,
        unresolved=unresolved,
    )


def _evaluate_exit_frontier(
    *,
    durable: Transaction,
    bundle: ContractBundle,
    registry: AdapterRegistry,
    plan: SetupPlan,
    frontier: tuple[str, ...],
    working: Transaction,
    observations: dict[str, JsonValue],
    decision_observations: dict[str, JsonValue],
    decision_prompts: list[JsonValue],
    decision_errors: list[JsonValue],
    unresolved: list[str],
) -> PreviewRound:
    working, exit_observations, exit_failures = run_stage_boundaries(
        bundle=bundle,
        transaction=working,
        registry=registry,
        stage_ids=frontier,
        boundary="exit",
        answers=plan.answers,
        persist_path=None,
    )
    observations.update(exit_observations)
    terminal_exit_failures = _terminal_boundary_failures(exit_failures)
    unresolved.extend(failure.identity for failure in terminal_exit_failures)
    if exit_failures and not terminal_exit_failures:
        return _remediation_round(
            durable=durable,
            bundle=bundle,
            registry=registry,
            plan=plan,
            failure=exit_failures[0],
            working=working,
            frontier=frontier,
            observations=observations,
            decision_observations=decision_observations,
            decision_prompts=decision_prompts,
            decision_errors=decision_errors,
            unresolved=unresolved,
        )
    return _round_result(
        working,
        plan,
        frontier,
        observations,
        decision_observations,
        decision_prompts,
        decision_errors,
        unresolved,
        {},
        terminal=bool(terminal_exit_failures),
    )


def _remediation_round(
    *,
    durable: Transaction,
    bundle: ContractBundle,
    registry: AdapterRegistry,
    plan: SetupPlan,
    failure: BoundaryFailure,
    working: Transaction,
    frontier: tuple[str, ...],
    observations: dict[str, JsonValue],
    decision_observations: dict[str, JsonValue],
    decision_prompts: list[JsonValue],
    decision_errors: list[JsonValue],
    unresolved: list[str],
) -> PreviewRound:
    remediation = remediation_plan(
        bundle=bundle,
        stage_id=failure.stage_id,
        operation_ids=failure.remediation_operation_ids,
        plan=plan,
    )
    results, operation_failures = _probe_operations(
        durable,
        bundle,
        remediation,
        registry,
    )
    unresolved.extend(operation_failures)
    return _round_result(
        working,
        remediation,
        frontier,
        observations,
        decision_observations,
        decision_prompts,
        decision_errors,
        unresolved,
        results,
        terminal=True,
    )


def _empty_round(previous: PreviewRound, frontier: tuple[str, ...]) -> PreviewRound:
    return PreviewRound(
        transaction=previous.transaction,
        answers=previous.answers,
        frontier=frontier,
        plan=previous.plan,
        stage_observations={},
        decision_observations={},
        decision_prompts=[],
        decision_errors=[],
        unresolved_actions=[],
        operation_results={},
        terminal=True,
    )


def _round_result(
    transaction: Transaction,
    plan: SetupPlan,
    frontier: tuple[str, ...],
    stage_observations: dict[str, JsonValue],
    decision_observations: dict[str, JsonValue],
    decision_prompts: list[JsonValue],
    decision_errors: list[JsonValue],
    unresolved_actions: list[str],
    operation_results: dict[str, OperationResult],
    *,
    terminal: bool,
) -> PreviewRound:
    return PreviewRound(
        transaction=transaction,
        answers=plan.answers,
        frontier=frontier,
        plan=plan,
        stage_observations=stage_observations,
        decision_observations=decision_observations,
        decision_prompts=decision_prompts,
        decision_errors=decision_errors,
        unresolved_actions=unresolved_actions,
        operation_results=operation_results,
        terminal=terminal,
    )


def _frontier_is_blocked(
    plan: SetupPlan,
    decision_errors: list[JsonValue],
    entry_failures: list[BoundaryFailure],
) -> bool:
    return bool(
        plan.unresolved_decisions
        or plan.unresolved_consents
        or decision_errors
        or entry_failures
    )


def _terminal_boundary_failures(
    failures: list[BoundaryFailure],
) -> list[BoundaryFailure]:
    return [
        failure
        for failure in failures
        if not failure.remediation_operation_ids
    ]


def _probe_operations(
    transaction: Transaction,
    bundle: ContractBundle,
    plan: SetupPlan,
    registry: AdapterRegistry,
) -> tuple[dict[str, OperationResult], list[str]]:
    results: dict[str, OperationResult] = {}
    failures: list[str] = []
    runnable_ids: set[str] = set()
    remediation_observations: dict[str, OperationResult] = {}
    for operation in plan.operations:
        request = operation_request(
            transaction,
            bundle,
            operation,
            phase="probe",
            probe_purpose="preview",
            approval=None,
            attempt=next_attempt(transaction, operation.operation_id),
            answers_fingerprint=canonical_sha256(plan.answers),
        )
        result = advisory_inference_probe_result(
            transaction.answers,
            request,
            invoke_adapter(registry, runner=operation.runner, request=request),
        )
        results[operation.operation_id] = result
        blocks = (
            _operation_probe_blocks(operation.requires_confirmation, result)
            and not _precondition_remediation_scheduled(bundle, operation, runnable_ids)
            and not _scheduled_peer_remediates_block(
                transaction,
                bundle,
                plan,
                registry,
                result,
                runnable_ids,
                remediation_observations,
            )
        )
        if blocks:
            failures.append(operation.operation_id)
        else:
            runnable_ids.add(operation.operation_id)
    return results, failures


def _precondition_remediation_scheduled(
    bundle: ContractBundle,
    operation: PlannedOperation,
    runnable_ids: set[str],
) -> bool:
    """True when this round already carries the fix for the block.

    A precondition probe can name the operation(s) that resolve it
    (``remediation_operation_refs``); if one of those is already scheduled
    earlier in this same frontier and is itself runnable, the block is not
    a genuine external blocker -- it is this operation waiting its turn
    behind a peer that will satisfy the precondition once applied. Mirrors
    the self-remediation exception ``operation_executor._pre_apply_stop``
    already grants at apply time, generalized to any preceding peer.
    """
    for probe_id in operation.precondition_probe_ids:
        probe = bundle.probes.get(probe_id)
        if probe is None:
            continue
        remediation_refs = probe.get("remediation_operation_refs")
        if not isinstance(remediation_refs, list):
            continue
        if any(ref in runnable_ids for ref in remediation_refs if isinstance(ref, str)):
            return True
    return False


def _scheduled_peer_remediates_block(
    transaction: Transaction,
    bundle: ContractBundle,
    plan: SetupPlan,
    registry: AdapterRegistry,
    result: OperationResult,
    runnable_ids: set[str],
    observations: dict[str, OperationResult],
) -> bool:
    """True when a runnable peer's own postcondition is this block's reason.

    An adapter can block on a condition its flow entry does not name as a
    precondition (the Core AI LaunchAgent deferral holds
    ``install_launchagent`` on the unverified pinned asset). The block is not
    an external blocker when a peer already runnable in this round declares a
    postcondition probe that remediates to that peer, and that probe
    currently blocks with the same ``error_kind``: applying the peer is what
    clears it. Fail-closed: a blocked peer is never runnable, an unobserved
    or passing probe excuses nothing, and a different reason stays blocked.
    """
    if result.checkpoint_status is not CheckpointStatus.BLOCKED or not result.error_kind:
        return False
    for peer in plan.operations:
        if peer.operation_id not in runnable_ids:
            continue
        for probe_id in peer.postcondition_probe_ids:
            if not _probe_names_remediation(bundle, probe_id, peer.operation_id):
                continue
            observed = observations.get(probe_id)
            if observed is None:
                observed = _observe_remediation_probe(transaction, bundle, registry, peer, probe_id)
                observations[probe_id] = observed
            if (
                observed.checkpoint_status is CheckpointStatus.BLOCKED
                and observed.error_kind == result.error_kind
            ):
                return True
    return False


def _probe_names_remediation(bundle: ContractBundle, probe_id: str, operation_id: str) -> bool:
    probe = bundle.probes.get(probe_id)
    remediation_refs = None if probe is None else probe.get("remediation_operation_refs")
    return isinstance(remediation_refs, list) and operation_id in remediation_refs


def _observe_remediation_probe(
    transaction: Transaction,
    bundle: ContractBundle,
    registry: AdapterRegistry,
    peer: PlannedOperation,
    probe_id: str,
) -> OperationResult:
    runner, request = operation_probe_request(
        transaction,
        bundle,
        peer,
        probe_id=probe_id,
        purpose="preview",
        attempt=next_attempt(transaction, probe_id),
    )
    return advisory_inference_probe_result(
        transaction.answers,
        request,
        invoke_adapter(registry, runner=runner, request=request),
    )


def _operation_probe_blocks(
    requires_confirmation: bool,
    result: OperationResult,
) -> bool:
    if result.checkpoint_status is CheckpointStatus.AWAITING_USER:
        return not result.planned_actions
    if result.checkpoint_status in {
        CheckpointStatus.BLOCKED,
        CheckpointStatus.FAILED,
    }:
        return True
    return bool(
        requires_confirmation
        and result.checkpoint_status is not CheckpointStatus.VERIFIED
        and not result.planned_actions
    )


def _empty_frontier_plan(
    paths: ManagerPaths,
    config: CreateConfig,
    bundle: ContractBundle,
    transaction: Transaction,
    answers: dict[str, JsonValue],
) -> SetupPlan:
    return build_setup_plan(
        bundle=bundle,
        config=config,
        seed=transaction.seed,
        journal_path=paths.transaction_path(config.name),
        prospective_consents=False,
        recorded_answers=answers,
        resolution_stage_ids=set(),
        operation_stage_ids=set(),
    )


def _render_setup_preview(
    *,
    registry: InstanceRegistry,
    config: CreateConfig,
    bundle: ContractBundle,
    transaction: Transaction,
    evaluated: PreviewRound,
    plan: SetupPlan,
) -> CommandResult:
    probes = _manager_probe_observations(registry, config, plan)
    probes["stage_boundaries"] = evaluated.stage_observations
    probes["discovered_decisions"] = evaluated.decision_observations
    probes["operation_results"] = {
        operation_id: canonical_stage_probe_observation(result)
        for operation_id, result in sorted(evaluated.operation_results.items())
    }
    planned_actions = canonical_planned_actions(evaluated.operation_results)
    consent_states = _consent_states(bundle, plan)
    permission_preflight = render_permission_preflight(
        bundle,
        plan,
        evaluated.stage_observations,
    )
    blocking = _preview_is_blocked(
        plan,
        evaluated.decision_errors,
        evaluated.unresolved_actions,
        consent_states,
        completed=(not evaluated.frontier and all(
            status in {
                CheckpointStatus.VERIFIED,
                CheckpointStatus.NOT_APPLICABLE,
            }
            for status in evaluated.transaction.stages.values()
        )),
    )
    fingerprint = approval_fingerprint(
        bundle=bundle,
        seed=transaction.seed,
        name=config.name,
        target=config.target,
        plan=plan,
        probe_observations=probes,
        consent_states=consent_states,
        planned_actions=planned_actions,
        permission_preflight=permission_preflight,
    )
    data = _setup_preview_data(
        config=config,
        bundle=bundle,
        transaction=transaction,
        evaluated=evaluated,
        plan=plan,
        probes=probes,
        planned_actions=planned_actions,
        permission_preflight=permission_preflight,
    )
    data["approval_fingerprint"] = fingerprint
    return _setup_preview_result(
        config=config,
        evaluated=evaluated,
        plan=plan,
        blocking=blocking,
        data=data,
    )


def _manager_probe_observations(
    registry: InstanceRegistry,
    config: CreateConfig,
    plan: SetupPlan,
) -> dict[str, JsonValue]:
    record = registry.get(config.name)
    return {
        "target_state": "present" if config.target.exists() else "absent",
        "registry_state": "managed" if record is not None else "vacant",
        "long_lived_python": str(resolve_long_lived_python() or "unavailable"),
        "planned_operation_ids": [
            operation.operation_id for operation in plan.operations
        ],
    }


def _consent_states(
    bundle: ContractBundle,
    plan: SetupPlan,
) -> dict[str, JsonValue]:
    raw = plan.answers.get("consents")
    if not isinstance(raw, dict):
        raise StateConflictError("normalized consent answers are not an object")
    current_ids = {
        consent_id
        for operation in plan.operations
        for consent_id in _string_refs(
            bundle.operations[operation.operation_id].get("consent_refs")
        )
    }
    return {key: value for key, value in raw.items() if key in current_ids}


def _string_refs(value: JsonValue) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise StateConflictError("flow reference list is invalid")
    return tuple(item for item in value if isinstance(item, str))


def _preview_is_blocked(
    plan: SetupPlan,
    decision_errors: list[JsonValue],
    unresolved_actions: list[str],
    consent_states: dict[str, JsonValue],
    completed: bool = False,
) -> bool:
    return bool(
        plan.unresolved_decisions
        or plan.unresolved_consents
        or decision_errors
        or unresolved_actions
        or any(value is not True for value in consent_states.values())
        or (not plan.operations and not completed)
    )


def _setup_preview_data(
    *,
    config: CreateConfig,
    bundle: ContractBundle,
    transaction: Transaction,
    evaluated: PreviewRound,
    plan: SetupPlan,
    probes: dict[str, JsonValue],
    planned_actions: list[JsonValue],
    permission_preflight: dict[str, JsonValue],
) -> dict[str, JsonValue]:
    return cast(
        dict[str, JsonValue],
        {
        "name": config.name,
        "target": str(config.target),
        "source": transaction.seed.identity_dict(),
        "flow_contract_digest": bundle.contract_digest,
        "autostart": config.autostart,
        "frontier": list(evaluated.frontier),
        "operations": [operation.to_dict() for operation in plan.operations],
        "manager_actions": [],
        "planned_actions": planned_actions,
        "consents": render_consents(bundle, plan, planned_actions),
        "decisions": render_decisions(bundle, plan),
        "unresolved_decisions": list(plan.unresolved_decisions),
        "unresolved_consents": list(plan.unresolved_consents),
        "unresolved_actions": sorted(set(evaluated.unresolved_actions)),
        "decision_prompts": evaluated.decision_prompts,
        "decision_errors": evaluated.decision_errors,
        "probe_observations": probes,
        "stage_probe_statuses": stage_probe_status_dict(evaluated.transaction),
        "stage_statuses": {
            stage_id: status.value
            for stage_id, status in evaluated.transaction.stages.items()
        },
        "adapter_probe_statuses": {
            operation_id: result.checkpoint_status.value
            for operation_id, result in sorted(evaluated.operation_results.items())
        },
        "permission_preflight": permission_preflight,
        "dry_run_writes": 0,
            "normalized_answers": plan.answers,
        },
    )


def _setup_preview_result(
    *,
    config: CreateConfig,
    evaluated: PreviewRound,
    plan: SetupPlan,
    blocking: bool,
    data: dict[str, JsonValue],
) -> CommandResult:
    verified = not evaluated.frontier and evaluated.transaction.status.value == "verified"
    decision_required = _decision_is_required(plan, evaluated.decision_errors)
    keys = _required_decision_keys(plan, evaluated.decision_errors)
    return CommandResult(
        kind="create_preview",
        status="verified" if verified else "awaiting_user" if blocking else "preview_ready",
        message=_preview_message(
            config,
            evaluated.decision_errors,
            evaluated.unresolved_actions,
            evaluated.operation_results,
        ),
        exit_code=(ExitCode.OK if verified or not blocking else ExitCode.HUMAN_ACTION),
        error_kind=(
            "decisions_required"
            if decision_required
            else "setup_preview_unresolved"
            if blocking
            else None
        ),
        repair=_preview_repair(
            decision_required,
            blocking,
            keys,
            evaluated.decision_errors,
            evaluated.unresolved_actions,
            evaluated.operation_results,
        ),
        data=data,
    )


def _decision_is_required(
    plan: SetupPlan,
    errors: list[JsonValue],
) -> bool:
    blocking_error_kinds = {
        "adapter_missing",
        "decision_selection_required",
        "candidate_set_empty",
        "decision_qualification_failed",
    }
    return bool(plan.unresolved_decisions) or any(
        isinstance(item, dict) and item.get("error_kind") in blocking_error_kinds
        for item in errors
    )


def _required_decision_keys(
    plan: SetupPlan,
    errors: list[JsonValue],
) -> list[str]:
    return sorted(
        {
            *plan.unresolved_decisions,
            *(
                str(item["id"])
                for item in errors
                if isinstance(item, dict) and isinstance(item.get("id"), str)
            ),
        }
    )


def _preview_repair(
    decision_required: bool,
    blocking: bool,
    keys: list[str],
    errors: list[JsonValue],
    unresolved_actions: list[str],
    operation_results: dict[str, OperationResult],
) -> str | None:
    unavailable_ids = _decision_error_ids(errors, "adapter_missing")
    if unavailable_ids:
        return _adapter_missing_repair(unavailable_ids, errors)
    if _decision_error_ids(errors, "decision_qualification_failed"):
        return (
            "No repair is available by adding a decision value: zero candidates "
            "passed qualification. Correct the failures listed in "
            "data.decision_errors so every required qualification probe returns "
            "verified for at least one candidate, then rerun preview."
        )
    if _decision_error_ids(errors, "candidate_set_empty"):
        return _candidate_set_empty_repair(errors)
    if decision_required:
        return "Add these exact [decisions] keys: " + ", ".join(keys) + "; then re-run preview."
    if unresolved_actions:
        return _unresolved_action_repair(unresolved_actions, operation_results)
    if blocking:
        return "Resolve every listed consent, adapter probe, and host action before approval."
    return None


def _unresolved_action_repair(
    unresolved_actions: list[str],
    operation_results: dict[str, OperationResult],
) -> str:
    return " ".join(
        _named_action_reason(operation_id, operation_results.get(operation_id))
        for operation_id in sorted(set(unresolved_actions))
    )


def _named_action_reason(operation_id: str, result: OperationResult | None) -> str:
    if result is None:
        return f"{operation_id} is unresolved."
    reason = result.error_kind or result.checkpoint_status.value
    detail = f"{operation_id} is {result.checkpoint_status.value} ({reason})."
    if result.repair:
        detail += f" {result.repair}"
    return detail


def _adapter_missing_repair(ids: list[str], errors: list[JsonValue]) -> str:
    decisions = ", ".join(repr(item) for item in ids)
    for error in errors:
        if (
            isinstance(error, dict)
            and error.get("error_kind") == "adapter_missing"
            and isinstance(error.get("repair"), str)
        ):
            return f"Required discovery adapter for {decisions} is unavailable. {error['repair']}"
    return (
        f"Required discovery adapter for {decisions} is unavailable. "
        "Repair the target-local adapter, then rerun preview."
    )


def _candidate_set_empty_repair(errors: list[JsonValue]) -> str:
    for error in errors:
        if (
            isinstance(error, dict)
            and error.get("error_kind") == "candidate_set_empty"
            and isinstance(error.get("repair"), str)
        ):
            return str(error["repair"])
    return (
        "No repair is available by adding a decision value: discovery returned "
        "zero candidates. Make a required candidate available, then rerun preview."
    )


def _preview_message(
    config: CreateConfig,
    errors: list[JsonValue],
    unresolved_actions: list[str],
    operation_results: dict[str, OperationResult],
) -> str:
    unavailable_ids = _decision_error_ids(errors, "adapter_missing")
    if unavailable_ids:
        decisions = ", ".join(repr(item) for item in unavailable_ids)
        return f"Required discovery adapter is unavailable for {decisions}."
    qualification_ids = _decision_error_ids(
        errors,
        "decision_qualification_failed",
    )
    if qualification_ids:
        decisions = ", ".join(repr(item) for item in qualification_ids)
        return (
            f"Required decision {decisions} expected at least one permitted "
            "candidate after qualification; found zero. No decision value can be "
            "supplied until at least one candidate passes every required "
            "qualification probe."
        )
    empty_ids = _decision_error_ids(errors, "candidate_set_empty")
    if empty_ids:
        decisions = ", ".join(repr(item) for item in empty_ids)
        return (
            f"Required decision {decisions} expected discovery to return at least "
            "one candidate; found zero. No decision value can be supplied until "
            "the declared provider returns a candidate."
        )
    if unresolved_actions:
        return (
            f'Solet "{config.name}" setup is blocked: '
            + _unresolved_action_repair(unresolved_actions, operation_results)
        )
    return f'Resume Solet "{config.name}" at {config.target}'


def _decision_error_ids(errors: list[JsonValue], error_kind: str) -> list[str]:
    return sorted(
        str(item["id"])
        for item in errors
        if isinstance(item, dict)
        and item.get("error_kind") == error_kind
        and isinstance(item.get("id"), str)
    )
