"""Decision-revision and read-only frontier rules for resumable create."""

from __future__ import annotations

from .config import CreateConfig
from .contracts import ContractBundle
from .errors import StateConflictError
from .flow import SetupPlan
from .models import JsonValue
from .transaction import Transaction


def merge_decision_inputs(
    config: CreateConfig,
    decision_selections: dict[str, JsonValue] | None,
    decision_source: str,
    decision_sources: dict[str, str] | None,
) -> tuple[dict[str, JsonValue], dict[str, str]]:
    """Merge per-call carriers over TOML/default carriers."""

    selections = dict(config.decisions)
    sources = dict(config.decision_sources)
    overrides = decision_selections or {}
    selections.update(overrides)
    override_sources = decision_sources or {}
    for decision_id in overrides:
        sources[decision_id] = override_sources.get(decision_id, decision_source)
    for decision_id, source in override_sources.items():
        if decision_id in selections:
            sources[decision_id] = source
    return selections, sources


def static_decision_selections(
    bundle: ContractBundle,
    selections: dict[str, JsonValue],
) -> dict[str, JsonValue]:
    """Keep only choices that do not require target-local discovery."""

    retained: dict[str, JsonValue] = {}
    for decision_id, value in selections.items():
        definition = bundle.decisions.get(decision_id)
        if definition is None:
            raise StateConflictError(
                f"decision {decision_id!r} is not declared by the pinned flow"
            )
        source = definition.get("option_source")
        if not isinstance(source, dict) or source.get("mode") != "discovered":
            retained[decision_id] = value
    return retained


def assert_decision_revision_allowed(
    transaction: Transaction,
    proposed_answers: dict[str, JsonValue],
    bundle: ContractBundle,
) -> None:
    recorded = _decision_map(transaction.answers)
    proposed = _decision_map(proposed_answers)
    changed = {
        decision_id
        for decision_id in set(recorded) | set(proposed)
        if recorded.get(decision_id) != proposed.get(decision_id)
    }
    if not changed:
        return
    sequences = {
        stage_id: _sequence(definition)
        for stage_id, definition in bundle.stages.items()
    }
    attempted = _attempted_stage_ids(transaction, sequences)
    blocked = [
        decision_id
        for decision_id in changed
        if _decision_change_is_blocked(
            decision_id,
            bundle,
            sequences,
            attempted,
            transaction.operation_stages,
        )
    ]
    if blocked:
        raise StateConflictError(
            "setup decisions cannot change after their execution frontier began: "
            f"{sorted(blocked)}",
            repair=(
                "Resume with the recorded decisions or create a separately "
                "reviewed instance."
            ),
        )


def _decision_map(answers: dict[str, JsonValue]) -> dict[str, JsonValue]:
    decisions = answers.get("decisions")
    if not isinstance(decisions, dict):
        raise StateConflictError("normalized decision state is invalid")
    return decisions


def _attempted_stage_ids(
    transaction: Transaction,
    sequences: dict[str, int],
) -> set[str]:
    attempts = (*transaction.operation_attempts, *transaction.stage_probe_attempts)
    return {
        str(attempt["stage_id"])
        for attempt in attempts
        if isinstance(attempt.get("stage_id"), str)
        and attempt.get("stage_id") in sequences
    }


def _decision_change_is_blocked(
    decision_id: str,
    bundle: ContractBundle,
    sequences: dict[str, int],
    attempted: set[str],
    operation_stages: dict[str, str],
) -> bool:
    definition = bundle.decisions.get(decision_id)
    if definition is None:
        return True
    consuming_sequences = _consuming_stage_sequences(
        decision_id,
        bundle,
        sequences,
        operation_stages,
    )
    if not consuming_sequences:
        return False
    frontier = min(consuming_sequences)
    return any(
        sequences[stage_id] >= frontier for stage_id in attempted
    )


def _consuming_stage_sequences(
    decision_id: str,
    bundle: ContractBundle,
    sequences: dict[str, int],
    operation_stages: dict[str, str],
) -> tuple[int, ...]:
    """Return the declared stages of operations that consume one decision."""

    consuming: list[int] = []
    for operation_id, stage_id in operation_stages.items():
        operation = bundle.operations.get(operation_id)
        if operation is None or stage_id not in sequences:
            raise StateConflictError(
                f"retained operation binding {operation_id!r} has no declared stage"
            )
        if _references_decision(operation.get("parameters"), decision_id):
            consuming.append(sequences[stage_id])
    return tuple(consuming)


def _references_decision(value: JsonValue | None, decision_id: str) -> bool:
    if isinstance(value, dict):
        if value.get("decision_ref") == decision_id:
            return True
        return any(_references_decision(item, decision_id) for item in value.values())
    if isinstance(value, list):
        return any(_references_decision(item, decision_id) for item in value)
    return False


def read_only_auto_advance_is_pure(
    recorded_answers: dict[str, JsonValue],
    plan: SetupPlan,
) -> bool:
    """Refuse implicit resolution while crossing a mutation-free frontier."""

    if plan.operations or plan.unresolved_decisions or plan.unresolved_consents:
        return False
    return all(
        recorded_answers.get(key) == plan.answers.get(key)
        for key in ("decisions", "consents")
    )


def render_recorded_decisions(answers: dict[str, JsonValue]) -> list[JsonValue]:
    decisions = answers.get("decisions")
    evidence = answers.get("resolution_evidence")
    if not isinstance(decisions, dict):
        return []
    evidence_items = evidence if isinstance(evidence, list) else []
    sources = {
        str(item["id"]): str(item["source"])
        for item in evidence_items
        if isinstance(item, dict) and "id" in item and "source" in item
    }
    return [
        {
            "id": decision_id,
            "selected": selected,
            "source": sources.get(decision_id),
            "status": "resolved",
        }
        for decision_id, selected in sorted(decisions.items())
    ]


def _sequence(stage: dict[str, JsonValue]) -> int:
    value = stage.get("sequence")
    if isinstance(value, bool) or not isinstance(value, int):
        raise StateConflictError("setup stage sequence must be an integer")
    return value
