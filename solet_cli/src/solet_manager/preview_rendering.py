"""Canonical preview rendering and approval-fingerprint assembly."""

from __future__ import annotations

from pathlib import Path
from typing import cast

from .adapters import OperationResult
from .condition_evaluator import condition_matches
from .contracts import ContractBundle, active_decision_ids
from .decision_resolution import decision_order
from .errors import ContractError
from .models import JsonValue
from .permission_preflight import permission_preflight_fingerprint_content
from .plan_builder import SetupPlan, find_string_refs
from .release_lock import SeedLock
from .transaction import canonical_sha256


def active_probe_ids(
    bundle: ContractBundle,
    decisions: dict[str, JsonValue],
    candidate_ids: tuple[str, ...],
) -> tuple[str, ...]:
    """Filter probe refs through the selected requirement/plugin graph."""

    activated = _activated_probe_ids(bundle, decisions)
    return tuple(
        probe_id
        for probe_id in candidate_ids
        if probe_id in activated
        and _probe_condition_matches(bundle.probes[probe_id], decisions)
    )


def _activated_probe_ids(
    bundle: ContractBundle,
    decisions: dict[str, JsonValue],
) -> set[str]:
    activated: set[str] = set()
    for requirement in bundle.requirements.values():
        if requirement.get("necessity") == "required":
            activated.update(find_string_refs(requirement, "probe_refs"))
    for plugin in bundle.plugins.values():
        if _is_required_bundled_plugin(plugin):
            activated.update(find_string_refs(plugin, "verification_probe_refs"))
    for decision_id, selected in decisions.items():
        _add_selected_option_probes(
            bundle,
            decision_id,
            selected,
            activated,
        )
    return activated


def _is_required_bundled_plugin(plugin: dict[str, JsonValue]) -> bool:
    return (
        plugin.get("installation_policy") == "bundled_install_now"
        and plugin.get("cardinality") == "exactly_one"
    )


def _add_selected_option_probes(
    bundle: ContractBundle,
    decision_id: str,
    selected: JsonValue,
    activated: set[str],
) -> None:
    definition = bundle.decisions.get(decision_id)
    source = None if definition is None else definition.get("option_source")
    options = source.get("options") if isinstance(source, dict) else None
    if not isinstance(options, dict):
        return
    selected_values = selected if isinstance(selected, list) else [selected]
    for option_id in selected_values:
        option = options.get(str(option_id))
        if isinstance(option, dict):
            _add_option_probes(bundle, option, activated)


def _add_option_probes(
    bundle: ContractBundle,
    option: dict[str, JsonValue],
    activated: set[str],
) -> None:
    activated.update(find_string_refs(option, "probe_refs"))
    activated.update(find_string_refs(option, "verification_probe_refs"))
    for plugin_id in find_string_refs(option, "plugin_refs"):
        plugin = bundle.plugins.get(plugin_id)
        if plugin is not None:
            activated.update(find_string_refs(plugin, "verification_probe_refs"))


def _probe_condition_matches(
    definition: dict[str, JsonValue],
    decisions: dict[str, JsonValue],
) -> bool:
    condition = definition.get("required_when")
    return condition is None or condition_matches(condition, decisions)


def render_decisions(bundle: ContractBundle, plan: SetupPlan) -> list[JsonValue]:
    """Render every resolved or active reviewed decision."""

    decisions = plan.answers.get("decisions")
    evidence = plan.answers.get("resolution_evidence")
    if not isinstance(decisions, dict) or not isinstance(evidence, list):
        raise ContractError("normalized decision rendering inputs are invalid")
    typed_decisions = cast(dict[str, JsonValue], decisions)
    sources = _resolution_sources(evidence)
    activated = active_decision_ids(bundle, typed_decisions)
    rendered: list[JsonValue] = []
    for decision_id in decision_order(bundle):
        item = _render_decision(
            decision_id,
            bundle.decisions[decision_id],
            typed_decisions,
            sources,
            activated,
        )
        if item is not None:
            rendered.append(item)
    return rendered


def _resolution_sources(evidence: list[JsonValue]) -> dict[str, str]:
    return {
        str(item["id"]): str(item["source"])
        for item in evidence
        if isinstance(item, dict) and "id" in item and "source" in item
    }


def _render_decision(
    decision_id: str,
    definition: dict[str, JsonValue],
    decisions: dict[str, JsonValue],
    sources: dict[str, str],
    activated: frozenset[str],
) -> JsonValue | None:
    if decision_id not in activated:
        return None
    selected = decisions.get(decision_id)
    required = definition.get("required") is True
    condition = definition.get("required_when")
    active = required and (
        condition is None or condition_matches(condition, decisions)
    )
    if selected is None and not active:
        return None
    return {
        "id": decision_id,
        "title": str(definition["title"]),
        "selection_mode": str(definition["selection_mode"]),
        "review_required": definition.get("review_required") is True,
        "selected": selected,
        "source": sources.get(decision_id),
        "status": "resolved" if selected is not None else "awaiting_user",
    }


def approval_fingerprint(
    *,
    bundle: ContractBundle,
    seed: SeedLock,
    name: str,
    target: Path,
    plan: SetupPlan,
    probe_observations: dict[str, JsonValue],
    consent_states: dict[str, JsonValue],
    planned_actions: list[JsonValue],
    permission_preflight: dict[str, JsonValue],
) -> str:
    value: dict[str, JsonValue] = {
        "flow_id": bundle.flow_id,
        "flow_source_revision": bundle.source_revision,
        "seed_identity": seed.identity_dict(),
        "name": name,
        "target": str(target),
        "answers": plan.answers,
        "answers_fingerprint": canonical_sha256(plan.answers),
        "operations": [operation.to_dict() for operation in plan.operations],
        "probe_observations": probe_observations,
        "consents": consent_states,
        "planned_actions": planned_actions,
        "permission_preflight": permission_preflight_fingerprint_content(
            permission_preflight
        ),
    }
    return canonical_sha256(value)


def canonical_planned_actions(
    results: dict[str, OperationResult],
) -> list[JsonValue]:
    """Canonicalize adapter-supplied host actions for review and approval."""

    canonical: list[JsonValue] = []
    seen: set[str] = set()
    for operation_id, result in sorted(results.items()):
        for action in result.planned_actions:
            stable_id = f"{operation_id}:{action.id}"
            if stable_id in seen:
                raise ContractError(f"duplicate planned action identity: {stable_id}")
            seen.add(stable_id)
            canonical.append({"operation_id": operation_id, **action.to_dict()})
    return canonical


def render_consents(
    bundle: ContractBundle,
    plan: SetupPlan,
    planned_actions: list[JsonValue],
) -> list[JsonValue]:
    """Render exact prospective consent terms and host mutations."""

    rendered: list[JsonValue] = []
    for consent_id, granted in sorted(consent_map(plan.answers).items()):
        item = _render_consent(
            bundle,
            plan,
            planned_actions,
            consent_id,
            granted,
        )
        if item is not None:
            rendered.append(item)
    return rendered


def _render_consent(
    bundle: ContractBundle,
    plan: SetupPlan,
    planned_actions: list[JsonValue],
    consent_id: str,
    granted: bool,
) -> JsonValue | None:
    operation_ids = _consent_operation_ids(bundle, plan, consent_id)
    if not operation_ids:
        return None
    definition = bundle.consents[consent_id]
    decline = definition.get("decline")
    if not isinstance(decline, dict) or not isinstance(
        decline.get("consequence"), str
    ):
        raise ContractError(
            f"consent {consent_id!r} lacks an exact decline consequence"
        )
    scope = definition.get("scope")
    if not isinstance(scope, list) or not all(isinstance(item, str) for item in scope):
        raise ContractError(f"consent {consent_id!r} lacks an exact public scope")
    mutations = [
        action
        for action in planned_actions
        if isinstance(action, dict) and action.get("operation_id") in operation_ids
    ]
    return cast(
        JsonValue,
        {
            "id": consent_id,
            "title": str(definition["title"]),
            "scope": list(scope),
            "mutations": mutations,
            "decline_consequence": str(decline["consequence"]),
            "prospective_value": granted,
        },
    )


def _consent_operation_ids(
    bundle: ContractBundle,
    plan: SetupPlan,
    consent_id: str,
) -> set[str]:
    return {
        operation.operation_id
        for operation in plan.operations
        if consent_id
        in _string_tuple(
            bundle.operations[operation.operation_id].get("consent_refs"),
            allow_missing=True,
        )
    }


def consent_map(answers: dict[str, JsonValue]) -> dict[str, bool]:
    raw = answers.get("consents")
    if not isinstance(raw, dict) or not all(
        isinstance(value, bool) for value in raw.values()
    ):
        raise ContractError("normalized consent answers must be a boolean map")
    return {key: value for key, value in raw.items() if isinstance(value, bool)}


def _string_tuple(value: JsonValue, *, allow_missing: bool) -> tuple[str, ...]:
    if value is None and allow_missing:
        return ()
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ContractError("expected a string reference array")
    return tuple(item for item in value if isinstance(item, str))
