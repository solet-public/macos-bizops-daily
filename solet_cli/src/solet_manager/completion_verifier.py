"""Required completion-probe execution for create and doctor."""

from __future__ import annotations

import uuid

from .adapters import AdapterRegistry, OperationRequest, invoke_adapter
from .contracts import ContractBundle, startup_readiness_budget
from .inference_probe_policy import advisory_inference_probe_result
from .models import JsonValue
from .operation_records import attempt_record, next_attempt
from .paths import ManagerPaths
from .probe_input_projection import probe_public_inputs
from .transaction import Transaction, write_transaction


def run_completion_probes(
    bundle: ContractBundle,
    transaction: Transaction,
    registry: AdapterRegistry,
    paths: ManagerPaths,
) -> tuple[Transaction, list[JsonValue]]:
    checks: list[JsonValue] = []
    updated = transaction
    for probe_id in _bound_completion_probe_ids(bundle, transaction):
        updated, check = _run_completion_probe(
            bundle=bundle,
            transaction=updated,
            registry=registry,
            paths=paths,
            probe_id=probe_id,
        )
        checks.append(check)
    return updated, checks


def _run_completion_probe(
    *,
    bundle: ContractBundle,
    transaction: Transaction,
    registry: AdapterRegistry,
    paths: ManagerPaths,
    probe_id: str,
) -> tuple[Transaction, JsonValue]:
    definition = bundle.probes[probe_id]
    attempt = next_attempt(transaction, probe_id)
    readiness = startup_readiness_budget(bundle)
    public_inputs = probe_public_inputs(transaction, str(definition["probe_ref"]))
    timeout_seconds = 30
    if (
        "completion" in readiness.consumer_probe_purposes
        and probe_id in readiness.consumer_probe_refs
    ):
        timeout_seconds = readiness.parent_budget_seconds
        public_inputs.update(readiness.public_inputs(
            consumer_probe_purpose="completion",
            consumer_probe_ref=probe_id,
        ))
    request = OperationRequest(
        request_id=str(uuid.uuid4()),
        operation_id=probe_id,
        operation_ref=str(definition["probe_ref"]),
        phase="probe",
        probe_purpose="completion",
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
    result = advisory_inference_probe_result(
        transaction.answers, request,
        invoke_adapter(registry, runner=str(definition["runner"]), request=request),
    )
    updated = transaction.with_completion_result(
        probe_id,
        result.checkpoint_status,
        attempt=attempt_record(
            result,
            stage_id="completion",
            phase="completion_probe",
            attempt=attempt,
            owner_operation_id=probe_id,
        ),
    )
    write_transaction(paths.transaction_path(updated.name), updated)
    return updated, {
        "id": probe_id,
        "status": result.checkpoint_status.value,
        "declared_expectation_advisory": definition.get("expectation"),
        "repair": result.repair,
    }


def resolved_completion_probe_ids(
    bundle: ContractBundle,
    answers: dict[str, JsonValue],
) -> tuple[str, ...]:
    """Return the completion checks not ruled out by the recorded decisions.

    A gate whose decision is merely *unresolved* is not a gate that failed:
    the probe stays required until the decision is answered, or until the
    decision becomes unreachable.  Only ``condition_is_inactive`` separates
    those, so it -- never a bare ``condition_matches`` -- decides membership.
    """

    from .condition_evaluator import condition_is_inactive
    from .decision_activation import active_decision_ids

    decisions = answers.get("decisions")
    if not isinstance(decisions, dict):
        raise ValueError("normalized answers lack resolved decisions")
    inactive_decision_ids = frozenset(bundle.decisions) - active_decision_ids(
        bundle, decisions
    )
    return tuple(
        probe_id
        for probe_id in bundle.completion_probe_ids
        if not condition_is_inactive(
            bundle.probes[probe_id].get("required_when"),
            decisions,
            inactive_decision_ids=inactive_decision_ids,
        )
    )


def rebind_completion_probes(
    bundle: ContractBundle,
    transaction: Transaction,
    answers: dict[str, JsonValue],
) -> Transaction:
    """Re-scope the completion set to the probes the answers still require.

    Answer revision can activate a gate that was unreachable at create time
    and deactivate one that was reachable, so the set is re-derived on every
    answer write rather than frozen at create.
    """

    return transaction.rebind_completion(
        resolved_completion_probe_ids(bundle, answers)
    )


def _bound_completion_probe_ids(
    bundle: ContractBundle,
    transaction: Transaction,
) -> tuple[str, ...]:
    """Preserve the flow's declared report order for the transaction's subset."""

    return tuple(
        probe_id
        for probe_id in bundle.completion_probe_ids
        if probe_id in transaction.completion
    )
