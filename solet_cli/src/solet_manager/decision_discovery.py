"""Target-local candidate discovery and decision qualification."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import cast

from .adapters import (
    AdapterRegistry,
    DiscoveredCandidate,
    OperationRequest,
    OperationResult,
    invoke_adapter,
)
from .contracts import ContractBundle
from .errors import StateConflictError
from .flow import SetupPlan, active_discovered_decision_ids
from .inference_probe_policy import advisory_inference_probe_result
from .models import CheckpointStatus, JsonValue
from .operation_records import next_attempt
from .transaction import Transaction, canonical_sha256

_DEFAULT_ADAPTER_TIMEOUT_SECONDS = 30
_INFERENCE_QUALIFICATION_TIMEOUT_SECONDS = 180
_INFERENCE_QUALIFICATION_PROBE_REF = "setup::models.qualify_structured_actions"


@dataclass(frozen=True)
class CandidateContract:
    """Validated candidate-shape and qualification rules."""

    required_metadata: frozenset[str]
    qualification_probe_ids: tuple[str, ...]
    sort_by: str


@dataclass(frozen=True)
class DecisionDiscovery:
    """Rendered discovery result for one active decision."""

    prompt: JsonValue
    errors: tuple[JsonValue, ...]
    observation: JsonValue


def discover_and_qualify_decisions(
    *,
    bundle: ContractBundle,
    transaction: Transaction,
    plan: SetupPlan,
    registry: AdapterRegistry,
    eligible_decision_ids: frozenset[str],
) -> tuple[list[JsonValue], list[JsonValue], dict[str, JsonValue]]:
    decisions = plan.answers.get("decisions")
    if not isinstance(decisions, dict):
        raise StateConflictError("normalized decisions are not an object")
    prompts: list[JsonValue] = []
    errors: list[JsonValue] = []
    observations: dict[str, JsonValue] = {}
    active_ids = active_discovered_decision_ids(
        bundle,
        decisions,
    )
    for decision_id in active_ids:
        if decision_id not in eligible_decision_ids:
            continue
        discovery = _discover_decision(
            bundle=bundle,
            transaction=transaction,
            plan=plan,
            registry=registry,
            decision_id=decision_id,
            selected=decisions.get(decision_id),
        )
        if decisions.get(decision_id) is None:
            prompts.append(discovery.prompt)
        errors.extend(discovery.errors)
        observations[decision_id] = discovery.observation
    return prompts, errors, observations


def eligible_discovered_decision_ids(
    *,
    bundle: ContractBundle,
    plan: SetupPlan,
    frontier_stage_ids: set[str],
) -> frozenset[str]:
    """Return discovered decision IDs declared to resolve at this frontier."""
    decisions = plan.answers.get("decisions")
    if not isinstance(decisions, dict):
        raise StateConflictError("normalized decisions are not an object")
    active_ids = active_discovered_decision_ids(bundle, decisions)
    return frozenset(
        decision_id
        for decision_id in active_ids
        if _decision_discovery_is_eligible(
            bundle=bundle,
            decision_id=decision_id,
            frontier_stage_ids=frontier_stage_ids,
        )
    )


def _decision_discovery_is_eligible(
    *,
    bundle: ContractBundle,
    decision_id: str,
    frontier_stage_ids: set[str],
) -> bool:
    definition = bundle.decisions[decision_id]
    return definition.get("resolution_stage_ref") in frontier_stage_ids


def _discover_decision(
    *,
    bundle: ContractBundle,
    transaction: Transaction,
    plan: SetupPlan,
    registry: AdapterRegistry,
    decision_id: str,
    selected: JsonValue,
) -> DecisionDiscovery:
    definition = bundle.decisions[decision_id]
    source = _option_source(definition, decision_id)
    discovery_probe_id = _required_text(
        source,
        "discovery_probe_ref",
        f"discovered decision {decision_id!r} has an invalid contract",
    )
    contract = _candidate_contract(source, decision_id)
    result = _run_discovery_probe(
        bundle=bundle,
        transaction=transaction,
        plan=plan,
        registry=registry,
        decision_id=decision_id,
        discovery_probe_id=discovery_probe_id,
    )
    candidates, foreign, malformed = _validate_candidates(
        result,
        decision_id=decision_id,
        contract=contract,
    )
    errors = _discovery_errors(
        result,
        decision_id=decision_id,
        foreign=foreign,
        malformed=malformed,
    )
    qualified, qualification_observations = _qualify_candidates(
        bundle=bundle,
        transaction=transaction,
        plan=plan,
        registry=registry,
        decision_id=decision_id,
        candidates=candidates,
        contract=contract,
        discovery_valid=not errors,
    )
    candidate_objects: list[JsonValue] = [item.to_dict() for item in qualified]
    errors.extend(
        _selection_errors(
            decision_id=decision_id,
            selected=selected,
            candidates=candidates,
            qualified=qualified,
            qualification_observations=qualification_observations,
            discovery_valid=not errors,
        )
    )
    prompt: JsonValue = {
        "id": decision_id,
        "title": str(definition["title"]),
        "prompt": str(definition["prompt"]),
        "selection_mode": str(definition["selection_mode"]),
        "minimum_selections": definition.get("minimum_selections"),
        "maximum_selections": definition.get("maximum_selections"),
        "review_required": definition.get("review_required") is True,
        "selected": selected,
        "sort_by": contract.sort_by,
        "candidates": candidate_objects,
    }
    observation: JsonValue = {
        "discovery_status": result.checkpoint_status.value,
        "sort_by": contract.sort_by,
        "candidates": candidate_objects,
        "selected": selected,
        "qualification_statuses": qualification_observations,
    }
    return DecisionDiscovery(prompt, tuple(errors), observation)


def _option_source(
    definition: dict[str, JsonValue],
    decision_id: str,
) -> dict[str, JsonValue]:
    source = definition.get("option_source")
    if not isinstance(source, dict):
        raise StateConflictError(
            f"discovered decision {decision_id!r} lacks option_source"
        )
    return source


def _candidate_contract(
    source: dict[str, JsonValue],
    decision_id: str,
) -> CandidateContract:
    raw = source.get("candidate_contract")
    if not isinstance(raw, dict):
        raise StateConflictError(
            f"discovered decision {decision_id!r} has an invalid contract"
        )
    required_metadata = _text_list(
        raw.get("required_metadata"),
        f"decision {decision_id!r} required_metadata is invalid",
    )
    qualification_ids = _text_list(
        raw.get("qualification_probe_refs"),
        f"decision {decision_id!r} qualification probes are invalid",
    )
    # Operator ruling rul_29e3c969 makes every inference qualification advisory.
    # A resumed transaction owns its pinned contract bytes, so an arbitrary
    # historical contract can still declare these refs.  Do not make that
    # historical metadata an eligibility gate; the doctor reports it later.
    if decision_id == "inference_model":
        qualification_ids = ()
    sort_by = raw.get("sort_by")
    if sort_by != "recommended_first":
        raise StateConflictError(
            f"decision {decision_id!r} must declare recommended_first candidate ordering"
        )
    return CandidateContract(
        required_metadata=frozenset(required_metadata),
        qualification_probe_ids=qualification_ids,
        sort_by="recommended_first",
    )


def _required_text(
    value: dict[str, JsonValue],
    key: str,
    error: str,
) -> str:
    item = value.get(key)
    if not isinstance(item, str):
        raise StateConflictError(error)
    return item


def _text_list(value: JsonValue, error: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise StateConflictError(error)
    return tuple(str(item) for item in value)


def _run_discovery_probe(
    *,
    bundle: ContractBundle,
    transaction: Transaction,
    plan: SetupPlan,
    registry: AdapterRegistry,
    decision_id: str,
    discovery_probe_id: str,
) -> OperationResult:
    definition = bundle.probes[discovery_probe_id]
    operation_id = f"decision.{decision_id}.discover"
    request = OperationRequest(
        request_id=str(uuid.uuid4()),
        operation_id=operation_id,
        operation_ref=str(definition["probe_ref"]),
        phase="probe",
        probe_purpose="decision_discovery",
        attempt=next_attempt(transaction, operation_id),
        name=transaction.name,
        target=transaction.target,
        flow_id=bundle.flow_id,
        flow_source_revision=bundle.source_revision,
        answers_fingerprint=canonical_sha256(plan.answers),
        approval_fingerprint=None,
        dry_run=True,
        timeout_seconds=_DEFAULT_ADAPTER_TIMEOUT_SECONDS,
        public_inputs={"decision_id": decision_id},
    )
    return invoke_adapter(registry, runner=str(definition["runner"]), request=request)


def _validate_candidates(
    result: OperationResult,
    *,
    decision_id: str,
    contract: CandidateContract,
) -> tuple[list[DiscoveredCandidate], list[str], list[str]]:
    candidates = [
        item for item in result.discovered_candidates if item.decision_id == decision_id
    ]
    foreign = [
        item.decision_id
        for item in result.discovered_candidates
        if item.decision_id != decision_id
    ]
    malformed = [
        item.value
        for item in candidates
        if not contract.required_metadata.issubset(item.metadata)
    ]
    return candidates, foreign, malformed


def _discovery_errors(
    result: OperationResult,
    *,
    decision_id: str,
    foreign: list[str],
    malformed: list[str],
) -> list[JsonValue]:
    if result.checkpoint_status is not CheckpointStatus.VERIFIED:
        error: dict[str, JsonValue] = {
            "id": decision_id,
            "error_kind": result.error_kind or "discovery_failed",
        }
        if result.repair:
            error["repair"] = result.repair
        return [error]
    if foreign or malformed:
        error: dict[str, JsonValue] = {
            "id": decision_id,
            "error_kind": "candidate_contract_invalid",
            "foreign_decisions": cast(list[JsonValue], foreign),
            "missing_metadata": cast(list[JsonValue], malformed),
        }
        return [error]
    return []


def _qualify_candidates(
    *,
    bundle: ContractBundle,
    transaction: Transaction,
    plan: SetupPlan,
    registry: AdapterRegistry,
    decision_id: str,
    candidates: list[DiscoveredCandidate],
    contract: CandidateContract,
    discovery_valid: bool,
) -> tuple[list[DiscoveredCandidate], dict[str, JsonValue]]:
    if not discovery_valid:
        return [], {}
    qualified: list[DiscoveredCandidate] = []
    observations: dict[str, JsonValue] = {}
    for index, candidate in enumerate(candidates):
        statuses = _qualify_candidate(
            bundle=bundle,
            transaction=transaction,
            plan=plan,
            registry=registry,
            decision_id=decision_id,
            candidate=candidate,
            candidate_index=index,
            probe_ids=contract.qualification_probe_ids,
        )
        observations[candidate.value] = statuses
        if all(_qualification_verified(value) for value in statuses.values()):
            qualified.append(candidate)
    return qualified, observations


def _qualify_candidate(
    *,
    bundle: ContractBundle,
    transaction: Transaction,
    plan: SetupPlan,
    registry: AdapterRegistry,
    decision_id: str,
    candidate: DiscoveredCandidate,
    candidate_index: int,
    probe_ids: tuple[str, ...],
) -> dict[str, JsonValue]:
    statuses: dict[str, JsonValue] = {}
    for probe_id in probe_ids:
        definition = bundle.probes[probe_id]
        operation_id = (
            f"decision.{decision_id}.qualify{candidate_index}.{probe_id}"
        )
        public_inputs: dict[str, JsonValue] = {
            "decision_id": decision_id,
            "candidate_id": candidate.value,
        }
        request = OperationRequest(
            request_id=str(uuid.uuid4()),
            operation_id=operation_id,
            operation_ref=str(definition["probe_ref"]),
            phase="probe",
            probe_purpose="decision_qualification",
            attempt=next_attempt(transaction, operation_id),
            name=transaction.name,
            target=transaction.target,
            flow_id=bundle.flow_id,
            flow_source_revision=bundle.source_revision,
            answers_fingerprint=canonical_sha256(plan.answers),
            approval_fingerprint=None,
            dry_run=True,
            timeout_seconds=_qualification_timeout_seconds(str(definition["probe_ref"])),
            public_inputs=public_inputs,
        )
        result = advisory_inference_probe_result(
            plan.answers,
            request,
            invoke_adapter(
                registry,
                runner=str(definition["runner"]),
                request=request,
            ),
        )
        status: dict[str, JsonValue] = {
            "checkpoint_status": result.checkpoint_status.value,
            "error_kind": result.error_kind,
        }
        if not _qualification_verified(status):
            status["repair"] = result.repair
            status["observed_summary"] = _qualification_observed_summary(result)
        statuses[probe_id] = status
    return statuses


def _qualification_timeout_seconds(probe_ref: str) -> int:
    """Keep reasoning-model qualification alive for its handler's full budget."""

    if probe_ref == _INFERENCE_QUALIFICATION_PROBE_REF:
        return _INFERENCE_QUALIFICATION_TIMEOUT_SECONDS
    return _DEFAULT_ADAPTER_TIMEOUT_SECONDS


def _qualification_observed_summary(result: OperationResult) -> str:
    for evidence in result.evidence:
        summary = evidence.get("summary")
        if isinstance(summary, str) and summary:
            return summary
    return f"No adapter evidence was returned; checkpoint status is {result.checkpoint_status.value}."


def _qualification_verified(value: JsonValue) -> bool:
    return (
        isinstance(value, dict)
        and value.get("checkpoint_status") == CheckpointStatus.VERIFIED.value
    )


def _selection_errors(
    *,
    decision_id: str,
    selected: JsonValue,
    candidates: list[DiscoveredCandidate],
    qualified: list[DiscoveredCandidate],
    qualification_observations: dict[str, JsonValue],
    discovery_valid: bool,
) -> list[JsonValue]:
    if not discovery_valid:
        return []
    qualification_error = _qualification_availability_error(
        decision_id,
        candidates,
        qualified,
        qualification_observations,
    )
    if qualification_error is not None:
        return [qualification_error]
    if selected is None:
        return [{"id": decision_id, "error_kind": "decision_selection_required"}]
    selected_values = _selected_values(selected)
    if selected_values is None:
        return [
            {
                "id": decision_id,
                "error_kind": "decision_selection_ambiguous",
                "selected": selected,
            }
        ]
    unavailable_error = _unavailable_selection_error(
        decision_id=decision_id,
        selected=selected,
        selected_values=selected_values,
        candidates=candidates,
        qualified=qualified,
    )
    return [] if unavailable_error is None else [unavailable_error]


def _qualification_availability_error(
    decision_id: str,
    candidates: list[DiscoveredCandidate],
    qualified: list[DiscoveredCandidate],
    qualification_observations: dict[str, JsonValue],
) -> dict[str, JsonValue] | None:
    if qualified:
        return None
    error: dict[str, JsonValue] = {
        "id": decision_id,
        "error_kind": (
            "decision_qualification_failed" if candidates else "candidate_set_empty"
        ),
        "expected": {
            "minimum_qualified_candidates": 1,
            "required_probe_status": CheckpointStatus.VERIFIED.value,
        },
        "found": {
            "discovered_candidates": len(candidates),
            "qualified_candidates": 0,
        },
    }
    if candidates:
        error["qualification_failures"] = _qualification_failures(
            candidates,
            qualification_observations,
        )
    else:
        error["repair"] = _empty_candidate_repair(decision_id)
    return error


def _empty_candidate_repair(decision_id: str) -> str:
    if decision_id in {"embedding_model", "inference_model"}:
        return (
            "The selected model service responded successfully but exposes no "
            "models. Load an appropriate embedding or inference model, then "
            "rerun preview."
        )
    return (
        "The selected discovery service responded successfully but exposed no "
        "candidates. Make a required candidate available, then rerun preview."
    )


def _qualification_failures(
    candidates: list[DiscoveredCandidate],
    observations: dict[str, JsonValue],
) -> list[JsonValue]:
    failures: list[JsonValue] = []
    for candidate in candidates:
        raw_probes = observations.get(candidate.value)
        probes = raw_probes if isinstance(raw_probes, dict) else {}
        failed_probes: list[JsonValue] = []
        for probe_id, raw_result in probes.items():
            if not isinstance(raw_result, dict) or _qualification_verified(raw_result):
                continue
            failure: dict[str, JsonValue] = {
                "probe_id": probe_id,
                "checkpoint_status": raw_result.get("checkpoint_status"),
                "error_kind": raw_result.get("error_kind"),
            }
            for key in ("repair", "observed_summary"):
                value = raw_result.get(key)
                if isinstance(value, str) and value:
                    failure[key] = value
            failed_probes.append(failure)
        failures.append(
            {
                "candidate_id": candidate.value,
                "failed_probes": failed_probes,
            }
        )
    return failures


def _unavailable_selection_error(
    *,
    decision_id: str,
    selected: JsonValue,
    selected_values: list[str],
    candidates: list[DiscoveredCandidate],
    qualified: list[DiscoveredCandidate],
) -> dict[str, JsonValue] | None:
    qualified_values = {candidate.value for candidate in qualified}
    unavailable = [value for value in selected_values if value not in qualified_values]
    if not unavailable:
        return None
    raw_values = {candidate.value for candidate in candidates}
    return {
        "id": decision_id,
        "error_kind": (
            "decision_qualification_failed"
            if all(value in raw_values for value in unavailable)
            else "decision_selection_ambiguous"
        ),
        "selected": selected,
        "unavailable": cast(list[JsonValue], unavailable),
    }


def _selected_values(selected: JsonValue) -> list[str] | None:
    if isinstance(selected, str):
        return [selected]
    if not isinstance(selected, list) or not all(
        isinstance(item, str) for item in selected
    ):
        return None
    return [item for item in selected if isinstance(item, str)]
