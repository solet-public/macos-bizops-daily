"""Declared stage-boundary activation and executable-frontier derivation."""

from __future__ import annotations

from dataclasses import replace
from typing import cast

from .condition_evaluator import condition_is_inactive, condition_refs
from .contracts import ContractBundle, ContractReconciliation, FirstUseInactiveProbeMigration
from .decision_activation import active_decision_ids
from .decision_state import derive_decision_dispositions
from .errors import ContractError, ReopenUnsafeAppliedStateError, StateError
from .journal_migrations import JOURNAL_BOUNDARY_KEYS, activation_site_key
from .journal_rollup import derive_stage_statuses
from .models import CheckpointStatus, JsonValue
from .transaction import StageProbeStatuses, Transaction


def initial_stage_probe_statuses(
    bundle: ContractBundle,
    answers: dict[str, JsonValue],
) -> StageProbeStatuses:
    """Build stable declared-key boundary maps; activation is a separate carrier."""

    decisions = _answer_decisions(answers)
    inactive_decision_ids = frozenset(bundle.decisions) - active_decision_ids(bundle, decisions)
    return {
        stage_id: _initial_stage_boundaries(bundle, stage, decisions, inactive_decision_ids)
        for stage_id, stage in bundle.stages.items()
    }


def _initial_stage_boundaries(
    bundle: ContractBundle,
    stage: dict[str, JsonValue],
    decisions: dict[str, JsonValue],
    inactive_decision_ids: frozenset[str],
) -> dict[str, dict[str, CheckpointStatus]]:
    return {
        "entry": _initial_boundary_statuses(
            bundle,
            stage,
            "entry_probe_refs",
            decisions,
            inactive_decision_ids,
        ),
        "exit": _initial_boundary_statuses(
            bundle,
            stage,
            "exit_probe_refs",
            decisions,
            inactive_decision_ids,
        ),
    }


def _initial_boundary_statuses(
    bundle: ContractBundle,
    stage: dict[str, JsonValue],
    field_name: str,
    decisions: dict[str, JsonValue],
    inactive_decision_ids: frozenset[str],
) -> dict[str, CheckpointStatus]:
    statuses: dict[str, CheckpointStatus] = {}
    stage_inactive = condition_is_inactive(
        stage.get("required_when"), decisions, inactive_decision_ids=inactive_decision_ids
    )
    for probe_id in _string_tuple(stage.get(field_name), allow_missing=True):
        probe_inactive = condition_is_inactive(
            bundle.probes[probe_id].get("required_when"),
            decisions,
            inactive_decision_ids=inactive_decision_ids,
        )
        statuses[probe_id] = (
            CheckpointStatus.NOT_APPLICABLE
            if stage_inactive or probe_inactive
            else CheckpointStatus.PENDING
        )
    return statuses


def initial_probe_activations(
    bundle: ContractBundle,
    answers: dict[str, JsonValue],
    *,
    prior_decision_ids: frozenset[str] | None = None,
) -> dict[str, dict[str, str]]:
    """Derive the complete closed activation map from current declared probes."""

    decisions = _answer_decisions(answers)
    inactive_ids = frozenset(bundle.decisions) - active_decision_ids(bundle, decisions)
    dispositions = derive_decision_dispositions(
        bundle,
        decisions,
        prior_decision_ids=(
            frozenset(bundle.decisions) if prior_decision_ids is None else prior_decision_ids
        ),
    )
    activations: dict[str, dict[str, str]] = {}
    for stage_id, stage in bundle.stages.items():
        for boundary in ("entry", "exit"):
            field = "entry_probe_refs" if boundary == "entry" else "exit_probe_refs"
            for probe_id in _string_tuple(stage.get(field), allow_missing=True):
                activation = _probe_activation(
                    stage,
                    bundle.probes[probe_id],
                    decisions,
                    inactive_ids,
                    dispositions,
                )
                activations[activation_site_key(stage_id, boundary, probe_id)] = activation
    return activations


def _probe_activation(
    stage: dict[str, JsonValue],
    probe: dict[str, JsonValue],
    decisions: dict[str, JsonValue],
    inactive_ids: frozenset[str],
    dispositions: dict[str, str],
) -> dict[str, str]:
    condition = stage.get("required_when")
    if condition_is_inactive(condition, decisions, inactive_decision_ids=inactive_ids):
        return _inactive_activation(condition)
    condition = probe.get("required_when")
    if condition_is_inactive(condition, decisions, inactive_decision_ids=inactive_ids):
        return _inactive_activation(condition)
    refs = condition_refs(condition)
    pending = next(
        (item for item in refs if dispositions.get(item) == "pending_newly_introduced"), None
    )
    if pending is not None:
        return {"state": "active", "reason": "newly_introduced_pending", "decided_by": pending}
    if refs:
        return {"state": "active", "reason": "decision_selected", "decided_by": refs[0]}
    return {"state": "active", "reason": "plan_bound", "decided_by": "plan"}


def _inactive_activation(condition: JsonValue) -> dict[str, str]:
    refs = condition_refs(condition)
    return {
        "state": "inactive",
        "reason": "decision_unselected",
        "decided_by": refs[0] if refs else "plan",
    }


def reconcile_stage_probe_activation(
    bundle: ContractBundle,
    transaction: Transaction,
    answers: dict[str, JsonValue],
) -> Transaction:
    """Recompute activation without rewriting verified history."""

    expected = initial_stage_probe_statuses(bundle, answers)
    expected_activations = initial_probe_activations(bundle, answers)
    validate_stage_probe_keys(bundle, transaction.stage_probe_statuses)
    for boundaries in expected.values():
        for probes in boundaries.values():
            for expected_initial in probes.values():
                if expected_initial not in {
                    CheckpointStatus.PENDING,
                    CheckpointStatus.NOT_APPLICABLE,
                }:
                    raise AssertionError("initial stage probe status is invalid")
    for stage_id, boundaries in transaction.stage_probe_statuses.items():
        for boundary, probes in boundaries.items():
            for probe_id in probes:
                site = activation_site_key(stage_id, boundary, probe_id)
                current = transaction.probe_activations[site]
                expected_activation = expected_activations[site]
                if current == expected_activation:
                    continue
                _assert_reopen_is_safe(
                    transaction, stage_id, boundary, probe_id, expected_activation
                )
    return transaction.with_probe_activations(expected_activations)


def _assert_reopen_is_safe(
    transaction: Transaction,
    stage_id: str,
    boundary: str,
    probe_id: str,
    expected: dict[str, str],
) -> None:
    if (
        transaction.stages[stage_id] is not CheckpointStatus.VERIFIED
        or expected["state"] != "active"
    ):
        return
    applied = [
        operation_id
        for operation_id, operation_stage in transaction.operation_stages.items()
        if operation_stage == stage_id
        and transaction.operation_statuses[operation_id]
        in {CheckpointStatus.APPLIED, CheckpointStatus.VERIFIED}
    ]
    if applied:
        raise ReopenUnsafeAppliedStateError(
            "reopen_unsafe_applied_state: "
            f"stage={stage_id} probe={probe_id} remediation_path=fresh create/dry-run",
            repair="Use fresh create/dry-run; this verified stage has persisted applied work.",
        )


def validate_stage_probe_keys(
    bundle: ContractBundle,
    statuses: StageProbeStatuses,
) -> None:
    expected_stages = set(bundle.stages)
    if set(statuses) != expected_stages:
        raise StateError(
            "stage probe map stage ids differ from the pinned flow: "
            f"{sorted(set(statuses) ^ expected_stages)}"
        )
    for stage_id, stage in bundle.stages.items():
        _validate_stage_boundary_keys(stage_id, stage, statuses[stage_id])


def _validate_stage_boundary_keys(
    stage_id: str,
    stage: dict[str, JsonValue],
    boundaries: dict[str, dict[str, CheckpointStatus]],
) -> None:
    if frozenset(boundaries) != JOURNAL_BOUNDARY_KEYS:
        raise StateError(f"stage {stage_id!r} probe map must have exact entry/exit keys")
    for boundary, field_name in (
        ("entry", "entry_probe_refs"),
        ("exit", "exit_probe_refs"),
    ):
        expected = set(_string_tuple(stage.get(field_name), allow_missing=True))
        observed = set(boundaries[boundary])
        if observed != expected:
            raise StateError(
                f"stage {stage_id!r} {boundary} probe keys differ from flow: "
                f"{sorted(observed ^ expected)}"
            )


def validate_stage_probe_state(
    bundle: ContractBundle,
    transaction: Transaction,
    answers: dict[str, JsonValue],
    *,
    prior_decision_ids: frozenset[str] | None = None,
) -> None:
    validate_stage_probe_keys(bundle, transaction.stage_probe_statuses)
    expected_activations = initial_probe_activations(
        bundle, answers, prior_decision_ids=prior_decision_ids
    )
    for stage_id, boundaries in transaction.stage_probe_statuses.items():
        for boundary, probes in boundaries.items():
            for probe_id in probes:
                site = activation_site_key(stage_id, boundary, probe_id)
                if transaction.probe_activations[site] != expected_activations[site]:
                    raise StateError(
                        f"stage probe activation mismatch: {stage_id}/{boundary}/{probe_id}"
                    )


def reconcile_contract_stage_probe_state(
    bundle: ContractBundle,
    transaction: Transaction,
    reconciliation: ContractReconciliation,
) -> Transaction:
    """Normalize declared historical probe identities before strict validation.

    Ordinary decision activation intentionally remains unable to change map
    keys.  This migration-only path accepts only a release-declared mapping and
    rejects any attempt or status that would need a guessed reset.
    """

    # ``completion_verifier`` depends on the ordinary frontier helpers in this
    # module, so import its public resolver only when reconciliation executes.
    # This keeps completion-membership derivation authoritative without an
    # import-time cycle.
    from .completion_verifier import resolved_completion_probe_ids


    if set(transaction.stages) != set(bundle.stages):
        raise StateError("contract reconciliation changes declared stage ids")
    expected = initial_stage_probe_statuses(bundle, transaction.answers)
    expected_activations = initial_probe_activations(
        bundle,
        transaction.answers,
        prior_decision_ids=frozenset(_answer_decisions(transaction.answers)),
    )
    mappings = {item.source: item.destination for item in reconciliation.stage_probe_mappings}
    first_use_migrations = {
        item.destination: item for item in reconciliation.first_use_inactive_probe_migrations
    }
    _validate_reconciliation_mappings(transaction, expected, mappings)
    _validate_first_use_migration_identities(transaction, expected, first_use_migrations)
    attempts = _normalize_attempts(transaction, mappings)
    source_statuses = transaction.stage_probe_statuses
    normalized = _copy_statuses(expected)
    mapped_destinations: set[tuple[str, str, str]] = set()
    for source, status in _iter_statuses(source_statuses):
        destination = mappings.get(source, source)
        if destination not in _status_identities(expected):
            raise StateError(
                f"contract reconciliation has an undeclared destination stage probe: {destination}"
            )
        if destination in mapped_destinations:
            raise StateError(f"contract reconciliation maps multiple statuses to {destination}")
        mapped_destinations.add(destination)
        normalized[destination[0]][destination[1]][destination[2]] = status
    latest = _latest_attempt_identities(attempts)
    _reconcile_activation_without_history(
        transaction,
        expected,
        normalized,
        latest,
        mapped_destinations,
        first_use_migrations,
    )
    rescoped = _scope_operations_to_contract(bundle, transaction)
    rescoped = _reset_verified_operation_statuses(rescoped, bundle, reconciliation)
    stored = _storage_statuses(normalized)
    stages = derive_stage_statuses(
        dict(rescoped.stages),
        stored,
        rescoped.operation_stages,
        rescoped.operation_statuses,
        expected_activations,
    )
    candidate = rescoped.reconciled_contract(
        flow_contract_digest=reconciliation.destination_digest,
        stages=stages,
        stage_probe_statuses=stored,
        probe_activations=expected_activations,
        stage_probe_attempts=attempts,
        completion_probe_ids=resolved_completion_probe_ids(bundle, transaction.answers),
    )
    parsed = Transaction.from_dict(candidate.to_dict())
    validate_stage_probe_keys(bundle, parsed.stage_probe_statuses)
    validate_stage_probe_state(
        bundle,
        parsed,
        parsed.answers,
        prior_decision_ids=frozenset(_answer_decisions(transaction.answers)),
    )
    return parsed


def _reset_verified_operation_statuses(
    transaction: Transaction,
    bundle: ContractBundle,
    reconciliation: ContractReconciliation,
) -> Transaction:
    """Invalidate only declared verified outcomes after a contract postcondition change."""

    undeclared = sorted(set(reconciliation.operation_statuses_to_reset) - set(bundle.operations))
    if undeclared:
        raise StateError(
            "contract reconciliation resets operations absent from the destination contract: "
            f"{undeclared}"
        )
    reset_ids = tuple(
        operation_id
        for operation_id in reconciliation.operation_statuses_to_reset
        if transaction.operation_statuses.get(operation_id) is CheckpointStatus.VERIFIED
    )
    if not reset_ids:
        return transaction
    statuses = dict(transaction.operation_statuses)
    for operation_id in reset_ids:
        statuses[operation_id] = CheckpointStatus.PENDING
    return replace(
        transaction,
        operation_statuses=statuses,
        result_kind=f"contract_reconciliation_reset:{reconciliation.migration_id}",
    )


def _scope_operations_to_contract(
    bundle: ContractBundle,
    transaction: Transaction,
) -> Transaction:
    """Drop bindings for operations the destination contract no longer declares.

    Carried verbatim, a retired operation's retained status keeps feeding the
    destination stage roll-up and pins a stage that nothing can clear.  Only the
    BINDING is dropped: ``bind_operations`` recomputes stages and status in the
    same write, and the operation's attempt records stay in retained history.
    """

    scoped = {
        operation_id: stage_id
        for operation_id, stage_id in transaction.operation_stages.items()
        if operation_id in bundle.operations
    }
    if scoped == transaction.operation_stages:
        return transaction
    return transaction.bind_operations(scoped)


def _validate_reconciliation_mappings(
    transaction: Transaction,
    expected: StageProbeStatuses,
    mappings: dict[tuple[str, str, str], tuple[str, str, str]],
) -> None:
    source_identities = _status_identities(transaction.stage_probe_statuses)
    destination_identities = _status_identities(expected)
    for source, destination in mappings.items():
        if source not in source_identities:
            raise StateError(f"contract reconciliation source stage probe is undeclared: {source}")
        if destination not in destination_identities:
            raise StateError(
                f"contract reconciliation destination stage probe is undeclared: {destination}"
            )
    for source in source_identities:
        if source not in destination_identities and source not in mappings:
            raise StateError(
                "contract reconciliation removes a stage probe without a declared mapping: "
                f"{source}"
            )


def _validate_first_use_migration_identities(
    transaction: Transaction,
    expected: StageProbeStatuses,
    migrations: dict[tuple[str, str, str], FirstUseInactiveProbeMigration],
) -> None:
    source_identities = _status_identities(transaction.stage_probe_statuses)
    destination_identities = _status_identities(expected)
    for identity in migrations:
        if identity not in source_identities or identity not in destination_identities:
            raise StateError(
                "contract reconciliation first-use migration probe is not declared "
                f"in both contracts: {identity}"
            )


def _normalize_attempts(
    transaction: Transaction,
    mappings: dict[tuple[str, str, str], tuple[str, str, str]],
) -> tuple[dict[str, JsonValue], ...]:
    normalized: list[dict[str, JsonValue]] = []
    for attempt in transaction.stage_probe_attempts:
        source = _attempt_identity(attempt)
        destination = mappings.get(source, source)
        updated = dict(attempt)
        updated["stage_id"] = destination[0]
        updated["boundary"] = destination[1]
        updated["probe_id"] = destination[2]
        normalized.append(updated)
    return tuple(normalized)


def _reconcile_activation_without_history(
    transaction: Transaction,
    expected: StageProbeStatuses,
    normalized: StageProbeStatuses,
    latest: set[tuple[str, str, str]],
    mapped_destinations: set[tuple[str, str, str]],
    first_use_migrations: dict[tuple[str, str, str], FirstUseInactiveProbeMigration],
) -> None:
    for identity in _status_identities(expected):
        stage_id, boundary, probe_id = identity
        expected_status = expected[stage_id][boundary][probe_id]
        current = normalized[stage_id][boundary][probe_id]
        if identity not in mapped_destinations:
            if (
                expected_status is not CheckpointStatus.NOT_APPLICABLE
                and transaction.stages[stage_id] is CheckpointStatus.VERIFIED
            ):
                raise StateError(
                    "contract reconciliation adds an active probe to an already-verified stage: "
                    f"{identity}"
                )
            continue
        if (current is CheckpointStatus.NOT_APPLICABLE) == (
            expected_status is CheckpointStatus.NOT_APPLICABLE
        ):
            continue
        rule = first_use_migrations.get(identity)
        if _allows_first_use_inactive_migration(
            rule,
            current=current,
            expected=expected_status,
            has_attempt=identity in latest,
            stage_status=transaction.stages[stage_id],
        ):
            normalized[stage_id][boundary][probe_id] = expected_status
            continue
        if identity in latest or transaction.stages[stage_id] is CheckpointStatus.VERIFIED:
            raise StateError(
                "contract reconciliation would change an historical or verified probe activation: "
                f"{identity}"
            )
        normalized[stage_id][boundary][probe_id] = expected_status


def _allows_first_use_inactive_migration(
    rule: FirstUseInactiveProbeMigration | None,
    *,
    current: CheckpointStatus,
    expected: CheckpointStatus,
    has_attempt: bool,
    stage_status: CheckpointStatus,
) -> bool:
    """Admit only the release-declared BLOCKED first-use supersession."""

    return (
        rule is not None
        and current is CheckpointStatus.BLOCKED
        and expected is CheckpointStatus.NOT_APPLICABLE
        and has_attempt
        and stage_status is not CheckpointStatus.VERIFIED
    )


def _status_identities(statuses: StageProbeStatuses) -> set[tuple[str, str, str]]:
    return {identity for identity, _ in _iter_statuses(statuses)}


def _iter_statuses(
    statuses: StageProbeStatuses,
) -> tuple[tuple[tuple[str, str, str], CheckpointStatus], ...]:
    return tuple(
        ((stage_id, boundary, probe_id), status)
        for stage_id, boundaries in statuses.items()
        for boundary, probes in boundaries.items()
        for probe_id, status in probes.items()
    )


def _copy_statuses(statuses: StageProbeStatuses) -> StageProbeStatuses:
    return {
        stage_id: {boundary: dict(probes) for boundary, probes in boundaries.items()}
        for stage_id, boundaries in statuses.items()
    }


def _storage_statuses(statuses: StageProbeStatuses) -> StageProbeStatuses:
    """Persist outcomes only; activation makes inactive sites effective N/A."""

    return {
        stage_id: {
            boundary: {
                probe_id: (
                    CheckpointStatus.PENDING
                    if status is CheckpointStatus.NOT_APPLICABLE
                    else status
                )
                for probe_id, status in probes.items()
            }
            for boundary, probes in boundaries.items()
        }
        for stage_id, boundaries in statuses.items()
    }


def _attempt_identity(attempt: dict[str, JsonValue]) -> tuple[str, str, str]:
    stage_id = attempt.get("stage_id")
    boundary = attempt.get("boundary")
    probe_id = attempt.get("probe_id")
    if (
        not isinstance(stage_id, str)
        or boundary not in {"entry", "exit"}
        or not isinstance(probe_id, str)
    ):
        raise StateError("contract reconciliation stage attempt identity is invalid")
    return stage_id, boundary, probe_id


def _latest_attempt_identities(
    attempts: tuple[dict[str, JsonValue], ...],
) -> set[tuple[str, str, str]]:
    return {_attempt_identity(attempt) for attempt in attempts}


def current_frontier_stage_ids(
    bundle: ContractBundle,
    transaction: Transaction,
    answers: dict[str, JsonValue],
) -> tuple[str, ...]:
    decisions = _answer_decisions(answers)
    inactive_decision_ids = frozenset(bundle.decisions) - active_decision_ids(bundle, decisions)
    ordered = sorted(bundle.stages.items(), key=lambda item: _sequence(item[1]))
    return tuple(
        stage_id
        for stage_id, stage in ordered
        if _is_frontier_stage(stage_id, stage, transaction, decisions, inactive_decision_ids)
    )


def _is_frontier_stage(
    stage_id: str,
    stage: dict[str, JsonValue],
    transaction: Transaction,
    decisions: dict[str, JsonValue],
    inactive_decision_ids: frozenset[str],
) -> bool:
    status = transaction.stages[stage_id]
    if status in {CheckpointStatus.VERIFIED, CheckpointStatus.NOT_APPLICABLE}:
        return False
    if condition_is_inactive(
        stage.get("required_when"),
        decisions,
        inactive_decision_ids=inactive_decision_ids,
    ):
        return False
    dependencies = _string_tuple(stage.get("depends_on_stage_refs"), allow_missing=True)
    return all(
        transaction.stages[item] in {CheckpointStatus.VERIFIED, CheckpointStatus.NOT_APPLICABLE}
        for item in dependencies
    )


def _answer_decisions(answers: dict[str, JsonValue]) -> dict[str, JsonValue]:
    decisions = answers.get("decisions")
    if not isinstance(decisions, dict):
        raise ContractError("normalized decisions are not an object")
    return cast(dict[str, JsonValue], decisions)


def _string_tuple(value: JsonValue, *, allow_missing: bool) -> tuple[str, ...]:
    if value is None and allow_missing:
        return ()
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ContractError("expected a string reference array")
    return tuple(item for item in value if isinstance(item, str))


def _sequence(stage: dict[str, JsonValue]) -> int:
    value = stage.get("sequence")
    if isinstance(value, bool) or not isinstance(value, int):
        raise ContractError("setup stage sequence must be an integer")
    return value
