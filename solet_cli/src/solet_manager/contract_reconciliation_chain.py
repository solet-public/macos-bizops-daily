"""Resolution of explicitly declared contract-reconciliation chains."""

from __future__ import annotations

from .contract_reconciliation_rules import AnswerValueMigration
from .contracts import ContractReconciliation, StageProbeMapping
from .errors import StateConflictError
from .transaction import Transaction


def resolve_reconciliation_chain(
    transaction: Transaction,
    destination_digest: str,
    declarations: tuple[ContractReconciliation, ...],
) -> ContractReconciliation:
    """Resolve one explicit, unambiguous migration chain to the installed digest."""

    current = (
        transaction.flow_id,
        transaction.flow_source_revision,
        transaction.flow_contract_digest,
    )
    chain: list[ContractReconciliation] = []
    visited: set[tuple[str, str, str]] = set()
    while True:
        if current in visited:
            raise StateConflictError("contract reconciliation manifest contains a migration cycle")
        visited.add(current)
        candidates = identity_reconciliation_candidates(current, declarations)
        if not candidates:
            _raise_missing_chain_entry(chain, current)
        direct = destination_reconciliation_candidates(candidates, destination_digest)
        if direct:
            return _one_direct_reconciliation(direct, current, destination_digest, chain)
        if len(candidates) != 1:
            raise StateConflictError(
                "contract reconciliation manifest authoring error: multiple forward entries match "
                f"intermediate source identity {current[1]}"
            )
        next_candidates = digest_reconciliation_candidates(candidates[0], declarations)
        if not next_candidates:
            raise StateConflictError(
                "release-declared reconciliation entries exist for this source but none target "
                f"the installed contract {destination_digest}; a forward-only entry for this "
                "destination is needed"
            )
        if len(next_candidates) != 1:
            raise StateConflictError(
                "contract reconciliation manifest authoring error: intermediate destination "
                f"digest {candidates[0].destination_digest} has "
                f"{len(next_candidates)} declared successor identities"
            )
        chain.append(candidates[0])
        current = _source_identity(next_candidates[0])


def identity_reconciliation_candidates(
    identity: tuple[str, str, str],
    declarations: tuple[ContractReconciliation, ...],
) -> tuple[ContractReconciliation, ...]:
    return tuple(item for item in declarations if _source_identity(item) == identity)


def destination_reconciliation_candidates(
    source_candidates: tuple[ContractReconciliation, ...],
    destination_digest: str,
) -> tuple[ContractReconciliation, ...]:
    return tuple(item for item in source_candidates if item.destination_digest == destination_digest)


def digest_reconciliation_candidates(
    item: ContractReconciliation,
    declarations: tuple[ContractReconciliation, ...],
) -> tuple[ContractReconciliation, ...]:
    return tuple(
        candidate
        for candidate in declarations
        if candidate.flow_id == item.flow_id
        and candidate.source_digest == item.destination_digest
    )


def _source_identity(item: ContractReconciliation) -> tuple[str, str, str]:
    return item.flow_id, item.source_revision, item.source_digest


def _raise_missing_chain_entry(
    chain: list[ContractReconciliation], identity: tuple[str, str, str]
) -> None:
    if not chain:
        raise StateConflictError("no release-declared contract reconciliation matches this source identity")
    raise StateConflictError(
        f"release-declared reconciliation chain has no entry for intermediate identity {identity[1]}"
    )


def _one_direct_reconciliation(
    direct: tuple[ContractReconciliation, ...],
    current: tuple[str, str, str],
    destination_digest: str,
    chain: list[ContractReconciliation],
) -> ContractReconciliation:
    if len(direct) != 1:
        raise StateConflictError(
            "contract reconciliation manifest authoring error: multiple entries match "
            f"source digest {current[2]} and installed destination digest {destination_digest}"
        )
    return compose_reconciliation_chain((*chain, direct[0]))


def compose_reconciliation_chain(
    chain: tuple[ContractReconciliation, ...],
) -> ContractReconciliation:
    if len(chain) == 1:
        return chain[0]
    if any(item.first_use_inactive_probe_migrations for item in chain):
        raise StateConflictError(
            "chained reconciliation with first-use probe migrations requires a direct declaration"
        )
    mappings, reset_ids, answer_migrations = _compose_chain_data(chain)
    _validate_unique_destinations(mappings)
    return _build_composed_reconciliation(chain, mappings, reset_ids, answer_migrations)


def _compose_chain_data(
    chain: tuple[ContractReconciliation, ...],
) -> tuple[
    dict[tuple[str, str, str], tuple[str, str, str]],
    list[str],
    tuple[AnswerValueMigration, ...],
]:
    mappings: dict[tuple[str, str, str], tuple[str, str, str]] = {}
    reset_ids: list[str] = []
    answer_migrations: list[AnswerValueMigration] = []
    for item in chain:
        mappings = compose_stage_probe_mappings(mappings, item.stage_probe_mappings)
        for operation_id in item.operation_statuses_to_reset:
            if operation_id not in reset_ids:
                reset_ids.append(operation_id)
        answer_migrations.extend(item.answer_value_migrations)
    return mappings, reset_ids, tuple(answer_migrations)


def _validate_unique_destinations(
    mappings: dict[tuple[str, str, str], tuple[str, str, str]],
) -> None:
    if len(set(mappings.values())) != len(mappings):
        raise StateConflictError("contract reconciliation chain maps multiple statuses to one destination")


def _build_composed_reconciliation(
    chain: tuple[ContractReconciliation, ...],
    mappings: dict[tuple[str, str, str], tuple[str, str, str]],
    reset_ids: list[str],
    answer_migrations: tuple[AnswerValueMigration, ...],
) -> ContractReconciliation:
    first = chain[0]
    last = chain[-1]
    return ContractReconciliation(
        migration_id="chain:" + ",".join(item.migration_id for item in chain),
        flow_id=first.flow_id,
        source_revision=first.source_revision,
        source_digest=first.source_digest,
        destination_digest=last.destination_digest,
        stage_probe_mappings=tuple(
            StageProbeMapping(source=source, destination=destination)
            for source, destination in mappings.items()
            if source != destination
        ),
        operation_statuses_to_reset=tuple(reset_ids),
        answer_value_migrations=answer_migrations,
    )


def compose_stage_probe_mappings(
    prior: dict[tuple[str, str, str], tuple[str, str, str]],
    next_mappings: tuple[StageProbeMapping, ...],
) -> dict[tuple[str, str, str], tuple[str, str, str]]:
    next_map = {item.source: item.destination for item in next_mappings}
    composed = {
        source: next_map.get(destination, destination)
        for source, destination in prior.items()
    }
    for source, destination in next_map.items():
        if source not in composed and source not in prior.values():
            composed[source] = destination
    return composed
