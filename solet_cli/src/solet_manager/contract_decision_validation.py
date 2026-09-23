"""Decision-stage, operation-use, and probe-runner contract validation."""

from __future__ import annotations

import re
from typing import Protocol

from .errors import ContractError
from .models import JsonValue

_CALLABLE_REF_PATTERN = re.compile(r"^[a-z][a-z0-9_]*::[a-z][a-z0-9_.]*$")
_REVIEWED_PROBE_RUNNERS = frozenset(
    {
        "bootstrap",
        "genesis",
        "hydration",
        "platform_process",
        "external_cli",
        "system",
        "system_settings",
        "manager",
    }
)


class DecisionContractView(Protocol):
    @property
    def stages(self) -> dict[str, dict[str, JsonValue]]: ...

    @property
    def operations(self) -> dict[str, dict[str, JsonValue]]: ...

    @property
    def probes(self) -> dict[str, dict[str, JsonValue]]: ...

    @property
    def decisions(self) -> dict[str, dict[str, JsonValue]]: ...

    @property
    def consents(self) -> dict[str, dict[str, JsonValue]]: ...

    @property
    def start_command(self) -> dict[str, JsonValue]: ...


def validate_decision_contract(bundle: DecisionContractView) -> None:
    stage_order = _validated_stage_order(bundle.stages)
    _validate_decision_registry(bundle, stage_order)
    operation_stage, probe_stages = _collect_stage_uses(bundle)
    _validate_operations(bundle, stage_order, operation_stage, probe_stages)
    _validate_start_probes(bundle)
    _validate_probe_conditions(bundle, stage_order, probe_stages)
    _validate_consent_conditions(bundle, stage_order, operation_stage)


def _validated_stage_order(
    stages: dict[str, dict[str, JsonValue]],
) -> dict[str, int]:
    order = {
        stage_id: _stage_sequence(stage_id, definition)
        for stage_id, definition in stages.items()
    }
    for stage_id, definition in stages.items():
        dependencies = _optional_string_tuple(
            definition.get("depends_on_stage_refs"),
            f"stages.{stage_id}.depends_on_stage_refs",
        )
        later = [item for item in dependencies if order[item] >= order[stage_id]]
        if later:
            raise ContractError(
                f"stage {stage_id!r} depends on non-earlier stages: {later}"
            )
    return order


def _validate_decision_registry(
    bundle: DecisionContractView,
    stage_order: dict[str, int],
) -> None:
    reachable = {"setup_profile"}
    for decision_id, definition in bundle.decisions.items():
        resolution_stage = _decision_resolution_stage(
            decision_id, definition, bundle.stages
        )
        reachable.update(
            _validate_decision_options(decision_id, definition, bundle.decisions, stage_order)
        )
        _check_condition_stage(
            definition.get("required_when"),
            use_stage=resolution_stage,
            use_label=f"decision {decision_id!r}",
            decisions=bundle.decisions,
            stage_order=stage_order,
        )
        _validate_discovered_decision(decision_id, definition, bundle.probes)
    _validate_available_when_dependency_cycles(bundle.decisions)
    unreachable = sorted(set(bundle.decisions) - reachable)
    if unreachable:
        raise ContractError(
            f"declared decisions are unreachable from flow follow-ups: {unreachable}"
        )


def _decision_resolution_stage(
    decision_id: str,
    definition: dict[str, JsonValue],
    stages: dict[str, dict[str, JsonValue]],
) -> str:
    stage = definition.get("resolution_stage_ref")
    if not isinstance(stage, str) or stage not in stages:
        raise ContractError(
            f"decision {decision_id!r} has missing or unknown resolution_stage_ref"
        )
    return stage


def _validate_decision_options(
    decision_id: str,
    definition: dict[str, JsonValue],
    decisions: dict[str, dict[str, JsonValue]],
    stage_order: dict[str, int],
) -> set[str]:
    source = definition.get("option_source")
    options = source.get("options") if isinstance(source, dict) else None
    if not isinstance(options, dict):
        return set()
    reachable: set[str] = set()
    for option_id, option in options.items():
        if not isinstance(option, dict):
            raise ContractError(
                f"decision {decision_id!r} option {option_id!r} must be an object"
            )
        _validate_option_availability(decision_id, option_id, option, decisions, stage_order)
        followups = _optional_string_tuple(
            option.get("followup_decision_refs"),
            f"decisions.{decision_id}.options.{option_id}.followup_decision_refs",
        )
        unknown = sorted(set(followups) - set(decisions))
        if unknown:
            raise ContractError(
                f"decision {decision_id!r} option {option_id!r} "
                f"has unknown follow-ups: {unknown}"
            )
        reachable.update(followups)
    return reachable


def _validate_option_availability(
    decision_id: str,
    option_id: str,
    option: dict[str, JsonValue],
    decisions: dict[str, dict[str, JsonValue]],
    stage_order: dict[str, int],
) -> None:
    if "available_when" not in option:
        return
    condition = option["available_when"]
    label = f"decision {decision_id!r} option {option_id!r} available_when"
    _validate_option_condition(condition, label)
    if decision_id in _condition_decision_refs(condition):
        raise ContractError(f"{label} cannot depend on its own decision")
    _check_condition_stage(
        condition,
        use_stage=str(decisions[decision_id]["resolution_stage_ref"]),
        use_label=label,
        decisions=decisions,
        stage_order=stage_order,
    )


def _validate_option_condition(condition: JsonValue, label: str) -> None:
    """Accept only the closed decision expressions supported by the evaluator."""

    if not isinstance(condition, dict):
        raise ContractError(f"{label} must be a decision condition object")
    if set(condition) in ({"all"}, {"any"}):
        children = next(iter(condition.values()))
        if not isinstance(children, list) or not children:
            raise ContractError(f"{label} compound must contain a non-empty array")
        for child in children:
            _validate_option_condition(child, label)
        return
    if set(condition) == {"not"}:
        _validate_option_condition(condition["not"], label)
        return
    _validate_option_condition_leaf(condition, label)


def _validate_option_condition_leaf(condition: dict[str, JsonValue], label: str) -> None:
    if set(condition) != {"decision_ref", "operator", "value"}:
        raise ContractError(f"{label} must contain only decision_ref, operator, and value")
    if not isinstance(condition["decision_ref"], str):
        raise ContractError(f"{label} decision_ref must be a string")
    if condition["operator"] not in ("equals", "not_equals", "contains", "not_contains"):
        raise ContractError(f"{label} has an unsupported operator")
    if not isinstance(condition["value"], (str, bool, int, float)):
        raise ContractError(f"{label} value must be a string, boolean, or number")


def _validate_discovered_decision(
    decision_id: str,
    definition: dict[str, JsonValue],
    probes: dict[str, dict[str, JsonValue]],
) -> None:
    source = definition.get("option_source")
    if not isinstance(source, dict) or source.get("mode") != "discovered":
        return
    candidate_contract = source.get("candidate_contract")
    qualification = (
        candidate_contract.get("qualification_probe_refs")
        if isinstance(candidate_contract, dict)
        else None
    )
    qualification_refs = (
        _string_tuple(
            qualification,
            f"decisions.{decision_id}.qualification_probe_refs",
        )
        if isinstance(qualification, list)
        else ()
    )
    refs = (source.get("discovery_probe_ref"), *qualification_refs)
    for probe_id in refs:
        if not isinstance(probe_id, str) or probe_id not in probes:
            raise ContractError(
                f"discovered decision {decision_id!r} "
                f"has undeclared probe {probe_id!r}"
            )
        validate_probe_execution_path(probe_id, probes[probe_id])


def _collect_stage_uses(
    bundle: DecisionContractView,
) -> tuple[dict[str, str], dict[str, list[str]]]:
    operation_stage: dict[str, str] = {}
    probe_stages: dict[str, list[str]] = {}
    for stage_id, definition in bundle.stages.items():
        for operation_id in _string_tuple(
            definition.get("operation_refs"), f"stages.{stage_id}.operation_refs"
        ):
            operation_stage[operation_id] = stage_id
        _collect_boundary_uses(stage_id, definition, bundle.probes, probe_stages)
    return operation_stage, probe_stages


def _collect_boundary_uses(
    stage_id: str,
    definition: dict[str, JsonValue],
    probes: dict[str, dict[str, JsonValue]],
    probe_stages: dict[str, list[str]],
) -> None:
    for boundary in ("entry_probe_refs", "exit_probe_refs"):
        for probe_id in _optional_string_tuple(
            definition.get(boundary), f"stages.{stage_id}.{boundary}"
        ):
            probe_stages.setdefault(probe_id, []).append(stage_id)
            validate_probe_execution_path(probe_id, probes[probe_id])


def _validate_operations(
    bundle: DecisionContractView,
    stage_order: dict[str, int],
    operation_stage: dict[str, str],
    probe_stages: dict[str, list[str]],
) -> None:
    for operation_id, definition in bundle.operations.items():
        owning_stage = operation_stage.get(operation_id)
        if owning_stage is None:
            continue
        _validate_operation(
            operation_id,
            definition,
            owning_stage,
            bundle,
            stage_order,
            probe_stages,
        )


def _validate_operation(
    operation_id: str,
    definition: dict[str, JsonValue],
    owning_stage: str,
    bundle: DecisionContractView,
    stage_order: dict[str, int],
    probe_stages: dict[str, list[str]],
) -> None:
    _check_condition_stage(
        definition.get("required_when"),
        use_stage=owning_stage,
        use_label=f"operation {operation_id!r}",
        decisions=bundle.decisions,
        stage_order=stage_order,
    )
    _validate_operation_parameters(
        operation_id,
        definition,
        owning_stage,
        bundle.decisions,
        stage_order,
    )
    _validate_operation_probes(
        operation_id,
        definition,
        owning_stage,
        bundle.probes,
        probe_stages,
    )


def _validate_operation_parameters(
    operation_id: str,
    definition: dict[str, JsonValue],
    owning_stage: str,
    decisions: dict[str, dict[str, JsonValue]],
    stage_order: dict[str, int],
) -> None:
    parameters = definition.get("parameters")
    if not isinstance(parameters, dict):
        return
    for parameter_id, source in parameters.items():
        decision_id = source.get("decision_ref") if isinstance(source, dict) else None
        if isinstance(decision_id, str):
            _assert_decision_available(
                decision_id,
                owning_stage,
                f"operation {operation_id!r} parameter {parameter_id!r}",
                decisions,
                stage_order,
            )


def _validate_operation_probes(
    operation_id: str,
    definition: dict[str, JsonValue],
    owning_stage: str,
    probes: dict[str, dict[str, JsonValue]],
    probe_stages: dict[str, list[str]],
) -> None:
    idempotency = definition.get("idempotency")
    if not isinstance(idempotency, dict):
        return
    for key in ("precondition_probe_refs", "postcondition_probe_refs"):
        refs = _optional_string_tuple(
            idempotency.get(key), f"operations.{operation_id}.{key}"
        )
        for probe_id in refs:
            validate_probe_execution_path(probe_id, probes[probe_id])
            probe_stages.setdefault(probe_id, []).append(owning_stage)


def _validate_start_probes(bundle: DecisionContractView) -> None:
    refs = _string_tuple(
        bundle.start_command.get("postcondition_probe_refs"),
        "executor_contracts.start_command.postcondition_probe_refs",
    )
    for probe_id in refs:
        validate_probe_execution_path(probe_id, bundle.probes[probe_id])


def _validate_probe_conditions(
    bundle: DecisionContractView,
    stage_order: dict[str, int],
    probe_stages: dict[str, list[str]],
) -> None:
    for probe_id, use_stages in probe_stages.items():
        earliest = min(use_stages, key=stage_order.__getitem__)
        _check_condition_stage(
            bundle.probes[probe_id].get("required_when"),
            use_stage=earliest,
            use_label=f"probe {probe_id!r}",
            decisions=bundle.decisions,
            stage_order=stage_order,
        )


def _validate_consent_conditions(
    bundle: DecisionContractView,
    stage_order: dict[str, int],
    operation_stage: dict[str, str],
) -> None:
    uses = _consent_stage_uses(bundle, operation_stage)
    for consent_id, definition in bundle.consents.items():
        stages = uses.get(consent_id)
        if not stages:
            continue
        earliest = min(stages, key=stage_order.__getitem__)
        _check_condition_stage(
            definition.get("required_when"),
            use_stage=earliest,
            use_label=f"consent {consent_id!r}",
            decisions=bundle.decisions,
            stage_order=stage_order,
        )


def _consent_stage_uses(
    bundle: DecisionContractView,
    operation_stage: dict[str, str],
) -> dict[str, list[str]]:
    uses: dict[str, list[str]] = {}
    for operation_id, definition in bundle.operations.items():
        owning_stage = operation_stage.get(operation_id)
        if owning_stage is None:
            continue
        refs = _optional_string_tuple(
            definition.get("consent_refs"),
            f"operations.{operation_id}.consent_refs",
        )
        for consent_id in refs:
            uses.setdefault(consent_id, []).append(owning_stage)
    return uses


def validate_probe_execution_path(
    probe_id: str,
    definition: dict[str, JsonValue],
) -> None:
    probe_ref = definition.get("probe_ref")
    if not isinstance(probe_ref, str):
        raise ContractError(
            f"probe {probe_id!r} callable ref is not transportable: {probe_ref!r}"
        )
    if _CALLABLE_REF_PATTERN.fullmatch(probe_ref) is None:
        raise ContractError(
            f"probe {probe_id!r} callable ref is not transportable: {probe_ref!r}"
        )
    runner = definition.get("runner")
    if runner not in _REVIEWED_PROBE_RUNNERS:
        raise ContractError(
            f"probe {probe_id!r} runner has no reviewed execution path: {runner!r}"
        )


def _check_condition_stage(
    condition: JsonValue,
    *,
    use_stage: str,
    use_label: str,
    decisions: dict[str, dict[str, JsonValue]],
    stage_order: dict[str, int],
) -> None:
    for decision_id in _condition_decision_refs(condition):
        _assert_decision_available(
            decision_id,
            use_stage,
            use_label,
            decisions,
            stage_order,
        )


def _assert_decision_available(
    decision_id: str,
    use_stage: str,
    use_label: str,
    decisions: dict[str, dict[str, JsonValue]],
    stage_order: dict[str, int],
) -> None:
    definition = decisions.get(decision_id)
    if definition is None:
        raise ContractError(f"{use_label} references unknown decision {decision_id!r}")
    resolution_stage = definition.get("resolution_stage_ref")
    if not isinstance(resolution_stage, str):
        raise ContractError(
            f"{use_label} at stage {use_stage!r} uses later decision "
            f"{decision_id!r} from stage {resolution_stage!r}"
        )
    if stage_order[resolution_stage] > stage_order[use_stage]:
        raise ContractError(
            f"{use_label} at stage {use_stage!r} uses later decision "
            f"{decision_id!r} from stage {resolution_stage!r}"
        )


def _validate_available_when_dependency_cycles(
    decisions: dict[str, dict[str, JsonValue]],
) -> None:
    """Reject option availability that requires its own unresolved closure."""

    dependencies = {
        decision_id: _available_when_dependencies(definition)
        for decision_id, definition in decisions.items()
    }
    visited: set[str] = set()
    active: list[str] = []

    def visit(decision_id: str) -> None:
        if decision_id in active:
            cycle = active[active.index(decision_id) :] + [decision_id]
            raise ContractError(
                "available_when decision dependency cycle: " + " -> ".join(cycle)
            )
        if decision_id in visited:
            return
        active.append(decision_id)
        for prerequisite in dependencies[decision_id]:
            visit(prerequisite)
        active.pop()
        visited.add(decision_id)

    for decision_id in decisions:
        visit(decision_id)


def _available_when_dependencies(
    definition: dict[str, JsonValue],
) -> tuple[str, ...]:
    source = definition.get("option_source")
    options = source.get("options") if isinstance(source, dict) else None
    if not isinstance(options, dict):
        return ()
    dependencies: list[str] = []
    for option in options.values():
        if isinstance(option, dict) and "available_when" in option:
            dependencies.extend(_condition_decision_refs(option["available_when"]))
    return tuple(dict.fromkeys(dependencies))


def _condition_decision_refs(value: JsonValue) -> tuple[str, ...]:
    if not isinstance(value, dict):
        return ()
    direct = value.get("decision_ref")
    if isinstance(direct, str):
        return (direct,)
    found: list[str] = []
    for child in value.values():
        if isinstance(child, dict):
            found.extend(_condition_decision_refs(child))
        elif isinstance(child, list):
            for item in child:
                found.extend(_condition_decision_refs(item))
    return tuple(found)


def _stage_sequence(stage_id: str, definition: dict[str, JsonValue]) -> int:
    value = definition.get("sequence")
    if isinstance(value, bool) or not isinstance(value, int):
        raise ContractError(f"stage {stage_id!r} sequence must be an integer")
    return value


def _optional_string_tuple(value: JsonValue, label: str) -> tuple[str, ...]:
    return () if value is None else _string_tuple(value, label)


def _string_tuple(value: JsonValue, label: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ContractError(f"{label} must be a string array")
    return tuple(item for item in value if isinstance(item, str))
