"""Flow-ordered decision resolution, defaults, and prompt construction."""

from __future__ import annotations

from typing import Protocol, cast

from .answer_validation import static_option_available
from .condition_evaluator import condition_matches
from .contracts import ContractBundle, active_decision_ids
from .decision_state import is_declined_answer
from .errors import ContractError
from .models import JsonValue


class DecisionPromptPlan(Protocol):
    @property
    def answers(self) -> dict[str, JsonValue]: ...

    @property
    def unresolved_decisions(self) -> tuple[str, ...]: ...


def active_discovered_decision_ids(
    bundle: ContractBundle,
    decisions: dict[str, JsonValue],
    *,
    resolution_stage_ids: set[str] | None = None,
) -> tuple[str, ...]:
    activated = active_decision_ids(bundle, decisions)
    active = {
        decision_id
        for decision_id, definition in bundle.decisions.items()
        if _is_active_discovered(
            decision_id,
            definition,
            decisions,
            activated,
            resolution_stage_ids,
        )
    }
    return tuple(item for item in decision_order(bundle) if item in active)


def _is_active_discovered(
    decision_id: str,
    definition: dict[str, JsonValue],
    decisions: dict[str, JsonValue],
    activated: frozenset[str],
    resolution_stage_ids: set[str] | None,
) -> bool:
    if decision_id not in activated:
        return False
    if resolution_stage_ids is not None:
        if definition.get("resolution_stage_ref") not in resolution_stage_ids:
            return False
    source = definition.get("option_source")
    if not isinstance(source, dict) or source.get("mode") != "discovered":
        return False
    if definition.get("required") is not True:
        return False
    condition = definition.get("required_when")
    return condition is None or condition_matches(condition, decisions)


def decision_order(bundle: ContractBundle) -> tuple[str, ...]:
    """Return the wizard's declared decision order, then any unpaged ids."""

    ordered: list[str] = []
    wizard = bundle.flow.get("wizard")
    pages = wizard.get("pages") if isinstance(wizard, dict) else None
    if isinstance(pages, list):
        for page in pages:
            _append_page_decisions(page, bundle.decisions, ordered)
    ordered.extend(
        decision_id
        for decision_id in bundle.decisions
        if decision_id not in ordered
    )
    return tuple(ordered)


def _append_page_decisions(
    page: JsonValue,
    decisions: dict[str, dict[str, JsonValue]],
    ordered: list[str],
) -> None:
    item_refs = page.get("item_refs") if isinstance(page, dict) else None
    if not isinstance(item_refs, list):
        return
    for item in item_refs:
        decision_id = _decision_item_ref(item)
        if decision_id is not None and decision_id in decisions:
            if decision_id not in ordered:
                ordered.append(decision_id)


def _decision_item_ref(item: JsonValue) -> str | None:
    if not isinstance(item, dict) or item.get("kind") != "decision":
        return None
    decision_id = item.get("ref")
    return decision_id if isinstance(decision_id, str) else None


def decision_ids_for_stages(
    bundle: ContractBundle,
    decisions: dict[str, JsonValue],
    stage_ids: set[str],
) -> tuple[str, ...]:
    activated = active_decision_ids(bundle, decisions)
    return tuple(
        decision_id
        for decision_id in decision_order(bundle)
        if decision_id in activated
        and bundle.decisions[decision_id].get("resolution_stage_ref") in stage_ids
    )


def unresolved_decision_ids_for_stages(
    bundle: ContractBundle,
    answers: dict[str, JsonValue],
    stage_ids: set[str],
) -> tuple[str, ...]:
    return unresolved_required_decisions(
        bundle,
        _answer_decisions(answers),
        resolution_stage_ids=stage_ids,
    )


def static_decision_prompts(
    bundle: ContractBundle,
    plan: DecisionPromptPlan,
) -> list[JsonValue]:
    """Render unresolved static choices without inventing defaults."""

    prompts: list[JsonValue] = []
    for decision_id in plan.unresolved_decisions:
        prompt = _static_decision_prompt(
            decision_id,
            bundle.decisions[decision_id],
            _answer_decisions(plan.answers),
        )
        if prompt is not None:
            prompts.append(prompt)
    return prompts


def _static_decision_prompt(
    decision_id: str,
    definition: dict[str, JsonValue],
    decisions: dict[str, JsonValue],
) -> JsonValue | None:
    source = definition.get("option_source")
    if not isinstance(source, dict) or source.get("mode") != "static":
        return None
    options = source.get("options")
    recommendations = definition.get("recommended_option_refs")
    if not isinstance(options, dict) or not isinstance(recommendations, list):
        raise ContractError(
            f"static decision {decision_id!r} has an invalid option contract"
        )
    return {
        "id": decision_id,
        "title": str(definition["title"]),
        "prompt": str(definition["prompt"]),
        "selection_mode": str(definition["selection_mode"]),
        "minimum_selections": definition.get("minimum_selections"),
        "maximum_selections": definition.get("maximum_selections"),
        "review_required": definition.get("review_required") is True,
        "selected": None,
        "sort_by": "recommended_first",
        "candidates": _static_candidates(decision_id, options, recommendations, decisions),
    }


def _static_candidates(
    decision_id: str,
    options: dict[str, JsonValue],
    recommendations: list[JsonValue],
    decisions: dict[str, JsonValue],
) -> list[JsonValue]:
    order = {
        str(option_id): rank
        for rank, option_id in enumerate(recommendations)
        if isinstance(option_id, str)
    }
    candidates = [
        _static_candidate(decision_id, option_id, option, order, fallback_rank)
        for fallback_rank, (option_id, option) in enumerate(
            options.items(), start=len(recommendations)
        )
        if static_option_available(option, decisions)
    ]
    candidates.sort(
        key=lambda item: (
            cast(int, item["recommendation_rank"]),
            str(item["value"]),
        )
    )
    return [cast(JsonValue, candidate) for candidate in candidates]


def _static_candidate(
    decision_id: str,
    option_id: str,
    option: JsonValue,
    recommendation_order: dict[str, int],
    fallback_rank: int,
) -> dict[str, JsonValue]:
    if not isinstance(option, dict):
        raise ContractError(
            f"static decision {decision_id!r} option {option_id!r} is invalid"
        )
    return {
        "value": option_id,
        "label": str(option["label"]),
        "recommendation_rank": recommendation_order.get(option_id, fallback_rank),
        "metadata": {
            "description": option.get("description"),
            "availability": option.get("availability"),
            "implications": option.get("implications"),
        },
    }


def order_decision_prompts(
    bundle: ContractBundle,
    *groups: list[JsonValue],
) -> list[JsonValue]:
    positions = {
        decision_id: index
        for index, decision_id in enumerate(decision_order(bundle))
    }
    combined = [item for group in groups for item in group]
    return sorted(combined, key=lambda item: _prompt_position(item, positions))


def _prompt_position(item: JsonValue, positions: dict[str, int]) -> int:
    if not isinstance(item, dict):
        return len(positions)
    return positions.get(str(item.get("id")), len(positions))


def normalize_selection_shape(
    decision_id: str,
    selected: JsonValue,
    definition: dict[str, JsonValue],
    source: str,
) -> JsonValue:
    if is_declined_answer(selected):
        if source != "interactive":
            raise ContractError("declined decision answers are written only by interactive review")
        return selected
    selection_mode = definition.get("selection_mode")
    if selection_mode == "single":
        if not isinstance(selected, str):
            raise ContractError(
                f"single decision {decision_id!r} requires one string"
            )
        return selected
    if selection_mode in {"multiple", "ordered_multiple"}:
        return _normalize_multiple_selection(
            decision_id, selected, str(selection_mode), source
        )
    raise ContractError(
        f"decision {decision_id!r} has unsupported selection_mode "
        f"{selection_mode!r}"
    )


def _normalize_multiple_selection(
    decision_id: str,
    selected: JsonValue,
    selection_mode: str,
    source: str,
) -> JsonValue:
    if isinstance(selected, str) and source == "flag":
        return [selected]
    if not isinstance(selected, list) or not all(
        isinstance(item, str) for item in selected
    ):
        raise ContractError(
            f"{selection_mode} decision {decision_id!r} requires a string array"
        )
    if len(selected) != len(set(cast(list[str], selected))):
        raise ContractError(f"decision {decision_id!r} contains duplicate selections")
    return list(selected)


def unresolved_required_decisions(
    bundle: ContractBundle,
    decisions: dict[str, JsonValue],
    *,
    resolution_stage_ids: set[str] | None,
) -> tuple[str, ...]:
    activated = active_decision_ids(bundle, decisions)
    unresolved = {
        decision_id
        for decision_id, definition in bundle.decisions.items()
        if _is_unresolved_required(
            decision_id,
            definition,
            decisions,
            activated,
            resolution_stage_ids,
        )
    }
    return tuple(item for item in decision_order(bundle) if item in unresolved)


def _is_unresolved_required(
    decision_id: str,
    definition: dict[str, JsonValue],
    decisions: dict[str, JsonValue],
    activated: frozenset[str],
    resolution_stage_ids: set[str] | None,
) -> bool:
    if decision_id not in activated or decision_id in decisions:
        return False
    if resolution_stage_ids is not None:
        if definition.get("resolution_stage_ref") not in resolution_stage_ids:
            return False
    if definition.get("required") is not True:
        return False
    condition = definition.get("required_when")
    return condition is None or condition_matches(condition, decisions)


def apply_reviewed_static_defaults(
    bundle: ContractBundle,
    decisions: dict[str, JsonValue],
    *,
    resolution_stage_ids: set[str] | None,
) -> list[JsonValue]:
    evidence: list[JsonValue] = []
    for decision_id in decision_order(bundle):
        selected = _reviewed_default(
            decision_id,
            bundle.decisions[decision_id],
            bundle,
            decisions,
            resolution_stage_ids,
        )
        if selected is None:
            continue
        decisions[decision_id] = selected
        evidence.append(_default_evidence(decision_id, selected))
    return evidence


def _reviewed_default(
    decision_id: str,
    definition: dict[str, JsonValue],
    bundle: ContractBundle,
    decisions: dict[str, JsonValue],
    resolution_stage_ids: set[str] | None,
) -> JsonValue | None:
    if not _default_is_eligible(
        decision_id,
        definition,
        bundle,
        decisions,
        resolution_stage_ids,
    ):
        return None
    source = definition.get("option_source")
    if not isinstance(source, dict) or source.get("mode") != "static":
        return None
    recommendations = definition.get("recommended_option_refs")
    if not isinstance(recommendations, list) or not all(
        isinstance(item, str) for item in recommendations
    ):
        return None
    selected = _recommended_selection(definition.get("selection_mode"), recommendations)
    options = source.get("options")
    if selected is None or not isinstance(options, dict):
        return None
    if not _recommended_options_available(selected, options, decisions):
        return None
    return selected


def _recommended_options_available(
    selected: JsonValue,
    options: dict[str, JsonValue],
    decisions: dict[str, JsonValue],
) -> bool:
    values = selected if isinstance(selected, list) else [selected]
    return all(static_option_available(options[str(value)], decisions) for value in values)


def _default_is_eligible(
    decision_id: str,
    definition: dict[str, JsonValue],
    bundle: ContractBundle,
    decisions: dict[str, JsonValue],
    resolution_stage_ids: set[str] | None,
) -> bool:
    if decision_id not in active_decision_ids(bundle, decisions):
        return False
    if resolution_stage_ids is not None:
        if definition.get("resolution_stage_ref") not in resolution_stage_ids:
            return False
    return decision_id not in decisions and definition.get("required") is True


def _recommended_selection(
    selection_mode: JsonValue | None,
    recommendations: list[JsonValue],
) -> JsonValue | None:
    if selection_mode == "multiple" and recommendations:
        return list(recommendations)
    if selection_mode == "single" and len(recommendations) == 1:
        return recommendations[0]
    return None


def _default_evidence(decision_id: str, selected: JsonValue) -> JsonValue:
    summary = (
        ",".join(str(item) for item in selected)
        if isinstance(selected, list)
        else selected
    )
    return {"id": decision_id, "source": "flow_default", "summary": summary}


def _answer_decisions(answers: dict[str, JsonValue]) -> dict[str, JsonValue]:
    decisions = answers.get("decisions")
    if not isinstance(decisions, dict):
        raise ContractError("normalized decisions are not an object")
    return cast(dict[str, JsonValue], decisions)
