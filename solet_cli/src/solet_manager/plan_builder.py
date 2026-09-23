"""Declarative setup-plan compilation from pinned flow and normalized answers."""

from __future__ import annotations

import json
from dataclasses import dataclass, replace
from pathlib import Path
from typing import cast

from .answer_validation import _INDEPENDENT_CARRIERS, validate_decision_selections
from .config import CreateConfig
from .contracts import ContractBundle, active_decision_ids, validate_normalized_answers
from .decision_resolution import (
    apply_reviewed_static_defaults,
    decision_order,
    normalize_selection_shape,
    unresolved_required_decisions,
)
from .errors import ContractError
from .models import JsonValue
from .probe_input_retention import retain_probe_inputs
from .release_lock import SeedLock


@dataclass(frozen=True)
class PlannedOperation:
    stage_id: str
    operation_id: str
    operation_ref: str
    runner: str
    risk: str
    requires_confirmation: bool
    precondition_probe_ids: tuple[str, ...]
    postcondition_probe_ids: tuple[str, ...]
    public_inputs: dict[str, JsonValue]
    apply_timeout_seconds: int = 300

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "stage_id": self.stage_id,
            "operation_id": self.operation_id,
            "operation_ref": self.operation_ref,
            "runner": self.runner,
            "risk": self.risk,
            "requires_confirmation": self.requires_confirmation,
            "precondition_probe_ids": list(self.precondition_probe_ids),
            "postcondition_probe_ids": list(self.postcondition_probe_ids),
            "public_inputs": self.public_inputs,
            "apply_timeout_seconds": self.apply_timeout_seconds,
        }


@dataclass(frozen=True)
class SetupPlan:
    answers: dict[str, JsonValue]
    operations: tuple[PlannedOperation, ...]
    unresolved_decisions: tuple[str, ...]
    unresolved_consents: tuple[str, ...]

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "answers": self.answers,
            "operations": [operation.to_dict() for operation in self.operations],
            "unresolved_decisions": list(self.unresolved_decisions),
            "unresolved_consents": list(self.unresolved_consents),
        }


def build_setup_plan(
    *,
    bundle: ContractBundle,
    config: CreateConfig,
    seed: SeedLock,
    journal_path: Path,
    prospective_consents: bool = False,
    decision_selections: dict[str, JsonValue] | None = None,
    recorded_answers: dict[str, JsonValue] | None = None,
    decision_source: str = "flag",
    decision_sources: dict[str, str] | None = None,
    resolution_stage_ids: set[str] | None = None,
    operation_stage_ids: set[str] | None = None,
) -> SetupPlan:
    decisions = _initial_decisions(config, seed)
    recorded_ids = _merge_recorded_decisions(bundle, recorded_answers, decisions)
    selection_evidence = _recorded_selection_evidence(recorded_answers, recorded_ids)
    _apply_explicit_selections(
        bundle,
        decisions,
        decision_selections or {},
        selection_evidence,
        decision_source,
        decision_sources or {},
        resolution_stage_ids,
    )
    default_evidence = apply_reviewed_static_defaults(
        bundle,
        decisions,
        resolution_stage_ids=resolution_stage_ids,
    )
    public_inputs = _public_inputs(config, seed, journal_path)
    flow_default_evidence = _resolve_flow_default_inputs(
        bundle,
        decisions,
        public_inputs,
        operation_stage_ids,
    )
    flow_default_evidence.extend(retain_probe_inputs(bundle, decisions, recorded_answers, public_inputs))
    operations = _plan_operations(
        bundle,
        decisions,
        public_inputs,
        operation_stage_ids,
    )
    consents, consent_evidence, referenced_consents = _plan_consents(
        bundle,
        operations,
        recorded_answers,
        prospective_consents,
    )
    answers = _normalized_answers(
        bundle,
        config,
        seed,
        public_inputs,
        decisions,
        consents,
        default_evidence,
        flow_default_evidence,
        selection_evidence,
        consent_evidence,
    )
    validate_normalized_answers(bundle, answers)
    unresolved_decisions = unresolved_required_decisions(
        bundle,
        decisions,
        resolution_stage_ids=resolution_stage_ids,
    )
    unresolved_consents = _unresolved_consents(
        referenced_consents,
        consents,
        prospective_consents,
    )
    return SetupPlan(
        answers,
        operations,
        unresolved_decisions,
        unresolved_consents,
    )


def _initial_decisions(
    config: CreateConfig,
    seed: SeedLock,
) -> dict[str, JsonValue]:
    return {
        "setup_profile": seed.profile,
        "autostart": "enabled" if config.autostart else "disabled",
    }


def _merge_recorded_decisions(
    bundle: ContractBundle,
    recorded_answers: dict[str, JsonValue] | None,
    decisions: dict[str, JsonValue],
) -> set[str]:
    raw = None if recorded_answers is None else recorded_answers.get("decisions")
    if raw is None:
        return set()
    if not isinstance(raw, dict):
        raise ContractError("recorded normalized decisions are not an object")
    recorded_ids: set[str] = set()
    for decision_id, selected in raw.items():
        if decision_id in bundle.decisions:
            decisions[decision_id] = selected
            recorded_ids.add(decision_id)
    return recorded_ids


def _recorded_selection_evidence(
    recorded_answers: dict[str, JsonValue] | None,
    recorded_ids: set[str],
) -> list[JsonValue]:
    raw = (
        None
        if recorded_answers is None
        else recorded_answers.get("resolution_evidence")
    )
    if not isinstance(raw, list):
        return []
    retained_ids = recorded_ids - {"setup_profile", "autostart"}
    return [
        item
        for item in raw
        if isinstance(item, dict) and item.get("id") in retained_ids
    ]


def _apply_explicit_selections(
    bundle: ContractBundle,
    decisions: dict[str, JsonValue],
    selections: dict[str, JsonValue],
    evidence: list[JsonValue],
    default_source: str,
    sources: dict[str, str],
    resolution_stage_ids: set[str] | None,
) -> None:
    unknown = sorted(set(selections) - set(bundle.decisions))
    if unknown:
        raise ContractError(
            f"decision overrides are not declared by the pinned flow: {unknown}"
        )
    prospective = _prospective_decisions(
        bundle,
        decisions,
        selections,
        default_source,
        sources,
        resolution_stage_ids,
    )
    inactive = sorted(
        decision_id
        for decision_id in selections
        if decision_id not in active_decision_ids(bundle, prospective)
    )
    if inactive:
        profile = decisions.get("setup_profile")
        raise ContractError(
            f"decision_not_active: {inactive}; "
            f"setup_profile {profile!r} does not activate them"
        )
    for decision_id in decision_order(bundle):
        if decision_id in selections:
            _apply_one_selection(
                bundle,
                decisions,
                selections,
                evidence,
                decision_id,
                sources.get(decision_id, default_source),
                resolution_stage_ids,
            )


def _prospective_decisions(
    bundle: ContractBundle,
    decisions: dict[str, JsonValue],
    selections: dict[str, JsonValue],
    default_source: str,
    sources: dict[str, str],
    resolution_stage_ids: set[str] | None,
) -> dict[str, JsonValue]:
    """Resolve the effective activation map without changing recorded answers."""

    prospective = dict(decisions)
    for decision_id in decision_order(bundle):
        if decision_id not in selections:
            continue
        definition = bundle.decisions[decision_id]
        if not _resolves_in_stage(definition, resolution_stage_ids):
            continue
        prospective[decision_id] = normalize_selection_shape(
            decision_id,
            selections[decision_id],
            definition,
            sources.get(decision_id, default_source),
        )
    apply_reviewed_static_defaults(
        bundle,
        prospective,
        resolution_stage_ids=resolution_stage_ids,
    )
    return prospective


def _apply_one_selection(
    bundle: ContractBundle,
    decisions: dict[str, JsonValue],
    selections: dict[str, JsonValue],
    evidence: list[JsonValue],
    decision_id: str,
    source: str,
    resolution_stage_ids: set[str] | None,
) -> None:
    definition = bundle.decisions.get(decision_id)
    if definition is None:
        raise ContractError(
            f"decision override {decision_id!r} is not declared by the pinned flow"
        )
    if decision_id in {"setup_profile", "autostart"}:
        raise ContractError(f"decision override {decision_id!r} is reserved")
    if not _resolves_in_stage(definition, resolution_stage_ids):
        return
    decisions[decision_id] = normalize_selection_shape(
        decision_id,
        selections[decision_id],
        definition,
        source,
    )
    evidence[:] = [
        item
        for item in evidence
        if not isinstance(item, dict) or item.get("id") != decision_id
    ]
    evidence.append(_selection_evidence(decision_id, decisions[decision_id], source))


def _resolves_in_stage(
    definition: dict[str, JsonValue],
    resolution_stage_ids: set[str] | None,
) -> bool:
    return (
        resolution_stage_ids is None
        or definition.get("resolution_stage_ref") in resolution_stage_ids
    )


def _selection_evidence(
    decision_id: str,
    selected: JsonValue,
    source: str,
) -> JsonValue:
    summary = (
        ",".join(str(item) for item in selected)
        if isinstance(selected, list)
        else str(selected)
    )
    return {"id": decision_id, "source": source, "summary": summary}


def _public_inputs(
    config: CreateConfig,
    seed: SeedLock,
    journal_path: Path,
) -> dict[str, JsonValue]:
    return {
        "repository_ref": seed.source_ref(),
        "solet_name": config.name,
        "clone_directory": str(config.target),
        "setup_journal_path": str(journal_path),
    }


def _resolve_flow_default_inputs(
    bundle: ContractBundle,
    decisions: dict[str, JsonValue],
    public_inputs: dict[str, JsonValue],
    operation_stage_ids: set[str] | None,
) -> list[JsonValue]:
    evidence: list[JsonValue] = []
    decision_controlled_inputs = _decision_option_input_refs(bundle)
    activated_inputs = _selected_option_input_refs(bundle, decisions)
    operation_ids = _selected_operation_ids_for_stages(
        bundle,
        decisions,
        operation_stage_ids,
    )
    for operation_id in operation_ids:
        operation = bundle.operations[operation_id]
        input_ids = _string_tuple(operation.get("input_refs"), allow_missing=True)
        for input_id in input_ids:
            if (
                input_id in decision_controlled_inputs
                and input_id not in activated_inputs
            ):
                continue
            if input_id in public_inputs:
                continue
            definition = bundle.inputs.get(input_id)
            if definition is None:
                raise ContractError(
                    f"operation {operation_id!r} references unknown input {input_id!r}"
                )
            if definition.get("sensitive") is True or "default" not in definition:
                continue
            default = definition["default"]
            summary = _flow_default_summary(input_id, default)
            public_inputs[input_id] = default
            evidence.append(
                {
                    "id": input_id,
                    "source": "flow_default",
                    "summary": summary,
                }
            )
    return evidence


def _flow_default_summary(input_id: str, value: JsonValue) -> str:
    summary = (
        value
        if isinstance(value, str)
        else json.dumps(value, sort_keys=True, separators=(",", ":"))
    )
    if len(summary) > 512:
        raise ContractError(
            f"flow default summary for input {input_id!r} exceeds 512 characters"
        )
    return summary


def _selected_operation_ids_for_stages(
    bundle: ContractBundle,
    decisions: dict[str, JsonValue],
    operation_stage_ids: set[str] | None,
) -> tuple[str, ...]:
    selected = selected_operation_ids(bundle, decisions)
    operation_ids: list[str] = []
    stages = sorted(bundle.stages.items(), key=lambda item: _sequence(item[1]))
    for stage_id, stage in stages:
        if operation_stage_ids is not None and stage_id not in operation_stage_ids:
            continue
        operation_ids.extend(
            operation_id
            for operation_id in _string_tuple(
                stage.get("operation_refs"),
                allow_missing=False,
            )
            if operation_id in selected
        )
    return tuple(operation_ids)


def _plan_operations(
    bundle: ContractBundle,
    decisions: dict[str, JsonValue],
    public_inputs: dict[str, JsonValue],
    operation_stage_ids: set[str] | None,
) -> tuple[PlannedOperation, ...]:
    selected = selected_operation_ids(bundle, decisions)
    operations = ordered_operations(bundle, selected, public_inputs, decisions)
    if operation_stage_ids is None:
        return operations
    return tuple(
        operation
        for operation in operations
        if operation.stage_id in operation_stage_ids
    )


def _plan_consents(
    bundle: ContractBundle,
    operations: tuple[PlannedOperation, ...],
    recorded_answers: dict[str, JsonValue] | None,
    prospective: bool,
) -> tuple[dict[str, JsonValue], list[JsonValue], tuple[str, ...]]:
    referenced = _referenced_consents(bundle, operations)
    recorded = (
        None if recorded_answers is None else recorded_answers.get("consents")
    )
    consents = dict(recorded) if isinstance(recorded, dict) else {}
    if prospective:
        consents.update(dict.fromkeys(referenced, True))
    evidence: list[JsonValue] = (
        [
            cast(
                JsonValue,
                {
                    "id": consent_id,
                    "source": "preapproved",
                    "summary": "true after approval of the exact rendered preview",
                },
            )
            for consent_id in referenced
        ]
        if prospective
        else []
    )
    return consents, evidence, referenced


def _referenced_consents(
    bundle: ContractBundle,
    operations: tuple[PlannedOperation, ...],
) -> tuple[str, ...]:
    return tuple(
        sorted(
            {
                consent_id
                for operation in operations
                for consent_id in _string_tuple(
                    bundle.operations[operation.operation_id].get("consent_refs"),
                    allow_missing=True,
                )
            }
        )
    )


def _normalized_answers(
    bundle: ContractBundle,
    config: CreateConfig,
    seed: SeedLock,
    public_inputs: dict[str, JsonValue],
    decisions: dict[str, JsonValue],
    consents: dict[str, JsonValue],
    default_evidence: list[JsonValue],
    flow_default_evidence: list[JsonValue],
    selection_evidence: list[JsonValue],
    consent_evidence: list[JsonValue],
) -> dict[str, JsonValue]:
    return {
        "schema_version": 1,
        "flow_id": bundle.flow_id,
        "flow_source_revision": bundle.source_revision,
        "name": config.name,
        "target": str(config.target),
        "public_inputs": public_inputs,
        "decisions": decisions,
        "consents": consents,
        "resolution_evidence": [
            {"id": "setup_profile", "source": "seed_lock", "summary": seed.profile},
            {
                "id": "autostart",
                "source": "config",
                "summary": str(config.autostart).lower(),
            },
            *default_evidence,
            *flow_default_evidence,
            *selection_evidence,
            *consent_evidence,
        ],
    }


def _unresolved_consents(
    referenced: tuple[str, ...],
    consents: dict[str, JsonValue],
    prospective: bool,
) -> tuple[str, ...]:
    if prospective:
        return ()
    return tuple(item for item in referenced if consents.get(item) is not True)


def selected_operation_ids(
    bundle: ContractBundle,
    decisions: dict[str, JsonValue],
) -> set[str]:
    validate_decision_selections(bundle, decisions)
    selected = required_operation_ids(bundle)
    active = active_decision_ids(bundle, decisions)
    for decision_id, answer in decisions.items():
        if not _is_selection_eligible(active, decision_id):
            continue
        definition = bundle.decisions[decision_id]
        source = definition.get("option_source")
        options = source.get("options") if isinstance(source, dict) else None
        if not isinstance(source, dict) or source.get("mode") != "static":
            continue
        if not isinstance(options, dict):
            continue
        values = answer if isinstance(answer, list) else [answer]
        for option_id in values:
            option = options.get(str(option_id))
            if isinstance(option, dict):
                _add_option_operations(bundle, option, selected)
    return selected


def _decision_option_input_refs(bundle: ContractBundle) -> set[str]:
    input_ids: set[str] = set()
    for definition in bundle.decisions.values():
        source = definition.get("option_source")
        options = source.get("options") if isinstance(source, dict) else None
        if not isinstance(options, dict):
            continue
        for option in options.values():
            if isinstance(option, dict):
                input_ids.update(_option_input_refs(option))
    return input_ids


def _selected_option_input_refs(
    bundle: ContractBundle,
    decisions: dict[str, JsonValue],
) -> set[str]:
    input_ids: set[str] = set()
    active = active_decision_ids(bundle, decisions)
    for decision_id, answer in decisions.items():
        if not _is_selection_eligible(active, decision_id):
            continue
        definition = bundle.decisions[decision_id]
        source = definition.get("option_source")
        options = source.get("options") if isinstance(source, dict) else None
        if not isinstance(options, dict):
            continue
        values = answer if isinstance(answer, list) else [answer]
        for option_id in values:
            option = options.get(str(option_id))
            if isinstance(option, dict):
                input_ids.update(_option_input_refs(option))
    return input_ids


def _is_selection_eligible(active: frozenset[str], decision_id: str) -> bool:
    return decision_id in active or decision_id in _INDEPENDENT_CARRIERS


def _option_input_refs(option: dict[str, JsonValue]) -> tuple[str, ...]:
    activates = option.get("activates")
    if not isinstance(activates, dict):
        return ()
    return _string_tuple(activates.get("input_refs"), allow_missing=True)


def _add_option_operations(
    bundle: ContractBundle,
    option: dict[str, JsonValue],
    selected: set[str],
) -> None:
    selected.update(find_string_refs(option, "operation_refs"))
    for dependency_id in find_string_refs(option, "dependency_refs"):
        dependency = bundle.dependencies.get(dependency_id)
        if dependency is not None:
            selected.update(find_string_refs(dependency, "install_operation_refs"))
    for plugin_id in find_string_refs(option, "plugin_refs"):
        plugin = bundle.plugins.get(plugin_id)
        if plugin is not None:
            selected.update(find_string_refs(plugin, "setup_operation_refs"))


def required_operation_ids(bundle: ContractBundle) -> set[str]:
    selected: set[str] = set()
    for requirement in bundle.requirements.values():
        _add_requirement_operations(bundle, requirement, selected)
    for plugin in bundle.plugins.values():
        if _is_required_bundled_plugin(plugin):
            selected.update(find_string_refs(plugin, "setup_operation_refs"))
    return selected


def _add_requirement_operations(
    bundle: ContractBundle,
    requirement: dict[str, JsonValue],
    selected: set[str],
) -> None:
    if requirement.get("necessity") != "required":
        return
    satisfaction = requirement.get("satisfaction")
    if not isinstance(satisfaction, dict):
        return
    dependency_id = satisfaction.get("dependency_ref")
    if isinstance(dependency_id, str):
        _add_dependency_operations(bundle, dependency_id, selected)
    component_id = satisfaction.get("component_ref")
    if isinstance(component_id, str):
        _add_component_operations(bundle, component_id, selected)


def _add_dependency_operations(
    bundle: ContractBundle,
    dependency_id: str,
    selected: set[str],
) -> None:
    dependency = bundle.dependencies.get(dependency_id)
    if dependency is not None:
        selected.update(find_string_refs(dependency, "install_operation_refs"))


def _add_component_operations(
    bundle: ContractBundle,
    component_id: str,
    selected: set[str],
) -> None:
    component = bundle.components.get(component_id)
    if component is None:
        return
    selected.update(find_string_refs(component, "install_operation_refs"))
    for dependency_id in find_string_refs(component, "dependency_refs"):
        _add_dependency_operations(bundle, dependency_id, selected)


def _is_required_bundled_plugin(plugin: dict[str, JsonValue]) -> bool:
    return (
        plugin.get("installation_policy") == "bundled_install_now"
        and plugin.get("cardinality") == "exactly_one"
    )


def find_string_refs(value: JsonValue, key: str) -> set[str]:
    found: set[str] = set()
    if isinstance(value, dict):
        for child_key, child in value.items():
            if child_key == key and isinstance(child, list):
                found.update(item for item in child if isinstance(item, str))
            else:
                found.update(find_string_refs(child, key))
    elif isinstance(value, list):
        for child in value:
            found.update(find_string_refs(child, key))
    return found


def ordered_operations(
    bundle: ContractBundle,
    selected_ids: set[str],
    public_inputs: dict[str, JsonValue],
    decisions: dict[str, JsonValue],
) -> tuple[PlannedOperation, ...]:
    stages = sorted(bundle.stages.items(), key=lambda item: _sequence(item[1]))
    planned: list[PlannedOperation] = []
    for stage_id, stage in stages:
        for operation_id in _string_tuple(
            stage.get("operation_refs"), allow_missing=False
        ):
            if operation_id in selected_ids:
                planned.append(
                    _planned_operation(
                        bundle,
                        stage_id,
                        operation_id,
                        public_inputs,
                        decisions,
                    )
                )
    return tuple(planned)


def _planned_operation(
    bundle: ContractBundle,
    stage_id: str,
    operation_id: str,
    public_inputs: dict[str, JsonValue],
    decisions: dict[str, JsonValue],
) -> PlannedOperation:
    definition = bundle.operations[operation_id]
    idempotency = definition.get("idempotency")
    if not isinstance(idempotency, dict):
        raise ContractError(f"operation {operation_id!r} lacks idempotency")
    safe_inputs = _operation_public_inputs(
        bundle,
        operation_id,
        definition,
        public_inputs,
        decisions,
    )
    return PlannedOperation(
        stage_id=stage_id,
        operation_id=operation_id,
        operation_ref=str(definition["operation_ref"]),
        runner=str(definition["runner"]),
        risk=str(definition["risk"]),
        requires_confirmation=definition.get("requires_confirmation") is True,
        precondition_probe_ids=_string_tuple(
            idempotency.get("precondition_probe_refs"), allow_missing=True
        ),
        postcondition_probe_ids=_string_tuple(
            idempotency.get("postcondition_probe_refs"), allow_missing=True
        ),
        public_inputs=safe_inputs,
        apply_timeout_seconds=_apply_timeout_seconds(definition),
    )


def _apply_timeout_seconds(definition: dict[str, JsonValue]) -> int:
    value = definition.get("apply_timeout_seconds", 300)
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 900:
        raise ContractError("operation apply_timeout_seconds must be an integer from 1 through 900")
    return value


def remediation_operations(
    *,
    bundle: ContractBundle,
    stage_id: str,
    operation_ids: tuple[str, ...],
    public_inputs: dict[str, JsonValue],
    decisions: dict[str, JsonValue],
) -> tuple[PlannedOperation, ...]:
    """Materialize only flow-declared remediations for one failed boundary."""

    unknown = sorted(set(operation_ids) - set(bundle.operations))
    if unknown:
        raise ContractError(
            "boundary remediation operations are not declared by the pinned flow: "
            f"{unknown}"
        )
    return tuple(
        _planned_operation(
            bundle,
            stage_id,
            operation_id,
            public_inputs,
            decisions,
        )
        for operation_id in dict.fromkeys(operation_ids)
    )


def remediation_plan(
    *,
    bundle: ContractBundle,
    stage_id: str,
    operation_ids: tuple[str, ...],
    plan: SetupPlan,
) -> SetupPlan:
    """Replace an interrupted stage plan with its closed remediation operations."""

    raw_inputs = plan.answers.get("public_inputs")
    raw_decisions = plan.answers.get("decisions")
    if not isinstance(raw_inputs, dict) or not isinstance(raw_decisions, dict):
        raise ContractError("normalized answers lack remediation planning inputs")
    return replace(
        plan,
        operations=remediation_operations(
            bundle=bundle,
            stage_id=stage_id,
            operation_ids=operation_ids,
            public_inputs=cast(dict[str, JsonValue], raw_inputs),
            decisions=cast(dict[str, JsonValue], raw_decisions),
        ),
        unresolved_consents=(),
    )


def _operation_public_inputs(
    bundle: ContractBundle,
    operation_id: str,
    definition: dict[str, JsonValue],
    public_inputs: dict[str, JsonValue],
    decisions: dict[str, JsonValue],
) -> dict[str, JsonValue]:
    input_ids = _string_tuple(definition.get("input_refs"), allow_missing=True)
    raw_parameters = definition.get("parameters")
    parameters = raw_parameters if isinstance(raw_parameters, dict) else {}
    collisions = sorted(set(input_ids) & set(parameters))
    if collisions:
        raise ContractError(
            f"operation {operation_id!r} parameter/input key collision: {collisions}"
        )
    resolved = {key: public_inputs[key] for key in input_ids if key in public_inputs}
    resolved.update(_resolved_decision_parameters(bundle, parameters, decisions))
    return resolved


def _resolved_decision_parameters(
    bundle: ContractBundle,
    parameters: dict[str, JsonValue],
    decisions: dict[str, JsonValue],
) -> dict[str, JsonValue]:
    active = active_decision_ids(bundle, decisions)
    resolved: dict[str, JsonValue] = {}
    for parameter_id, source in parameters.items():
        decision_id = source.get("decision_ref") if isinstance(source, dict) else None
        if (
            isinstance(decision_id, str)
            and _is_selection_eligible(active, decision_id)
            and decision_id in decisions
        ):
            resolved[parameter_id] = decisions[decision_id]
    return resolved


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
