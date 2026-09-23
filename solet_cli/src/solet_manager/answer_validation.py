"""Closed normalized-answer and decision-selection validation."""

from __future__ import annotations

import re
from typing import Protocol, cast

from .condition_evaluator import condition_matches, condition_refs
from .decision_activation import DecisionCatalog, active_decision_ids
from .decision_state import is_declined_answer, selected_value
from .errors import ContractError
from .models import JsonValue

_ANSWER_KEYS = {
    "schema_version",
    "flow_id",
    "flow_source_revision",
    "name",
    "target",
    "public_inputs",
    "decisions",
    "consents",
    "resolution_evidence",
}
_INDEPENDENT_CARRIERS = {"setup_profile", "autostart"}


class AnswerContract(DecisionCatalog, Protocol):
    @property
    def flow_id(self) -> str: ...

    @property
    def inputs(self) -> dict[str, dict[str, JsonValue]]: ...

    @property
    def consents(self) -> dict[str, dict[str, JsonValue]]: ...


def validate_normalized_answers(
    bundle: AnswerContract,
    answers: dict[str, JsonValue],
) -> None:
    _validate_answer_identity(bundle, answers)
    public_inputs = _answer_object(answers, "public_inputs")
    decisions = _answer_object(answers, "decisions")
    consents = _answer_object(answers, "consents")
    _validate_known_carriers(bundle, public_inputs, decisions, consents)
    _validate_public_inputs(bundle, public_inputs)
    _validate_active_decisions(bundle, decisions)
    validate_decision_selections(bundle, decisions)
    _validate_consents(consents)


def validate_decision_selections(
    bundle: DecisionCatalog,
    decisions: dict[str, JsonValue],
) -> None:
    """Validate selected options against the complete resolved decision context."""

    for decision_id, selected in decisions.items():
        if is_declined_answer(selected):
            continue
        validate_decision_selection(
            decision_id,
            selected_value(selected),
            bundle.decisions[decision_id],
            decisions,
        )


def _validate_answer_identity(
    bundle: AnswerContract,
    answers: dict[str, JsonValue],
) -> None:
    if set(answers) != _ANSWER_KEYS:
        difference = sorted(set(answers) ^ _ANSWER_KEYS)
        raise ContractError(
            f"normalized answers fields differ from v1: {difference}"
        )
    if answers.get("schema_version") != 1 or answers.get("flow_id") != bundle.flow_id:
        raise ContractError("normalized answer identity differs from the pinned flow")


def _answer_object(
    answers: dict[str, JsonValue],
    key: str,
) -> dict[str, JsonValue]:
    value = answers.get(key)
    if not isinstance(value, dict):
        raise ContractError(f"{key} must be an object")
    return cast(dict[str, JsonValue], value)


def _validate_known_carriers(
    bundle: AnswerContract,
    public_inputs: dict[str, JsonValue],
    decisions: dict[str, JsonValue],
    consents: dict[str, JsonValue],
) -> None:
    unknown_inputs = sorted(set(public_inputs) - set(bundle.inputs))
    unknown_decisions = sorted(set(decisions) - set(bundle.decisions))
    unknown_consents = sorted(set(consents) - set(bundle.consents))
    if unknown_inputs or unknown_decisions or unknown_consents:
        raise ContractError(
            "normalized answers reference unknown ids: "
            f"inputs={unknown_inputs}, decisions={unknown_decisions}, "
            f"consents={unknown_consents}"
        )


def _validate_public_inputs(
    bundle: AnswerContract,
    public_inputs: dict[str, JsonValue],
) -> None:
    sensitive = {
        key for key, value in bundle.inputs.items() if value.get("sensitive") is True
    }
    leaked = sorted(set(public_inputs) & sensitive)
    if leaked:
        raise ContractError(
            f"secret inputs are forbidden from normalized answers: {leaked}"
        )
    for input_id, value in public_inputs.items():
        _validate_public_input_definition(input_id, bundle.inputs[input_id], value)


def _validate_public_input_definition(
    input_id: str,
    definition: dict[str, JsonValue],
    value: JsonValue,
) -> None:
    """Apply the flow's declared public-input type and optional regex pattern."""

    value_type = definition.get("value_type")
    if value_type not in {"string", "path", "url", "email"}:
        raise ContractError(f"input {input_id!r} has unsupported public value type: {value_type!r}")
    if not isinstance(value, str):
        raise ContractError(f"input {input_id!r} must be a {value_type} string")
    validation = definition.get("validation")
    if validation is None:
        return
    if not isinstance(validation, dict):
        raise ContractError(f"input {input_id!r} validation must be an object")
    pattern = validation.get("pattern")
    if not isinstance(pattern, str):
        raise ContractError(f"input {input_id!r} validation.pattern must be a string")
    if re.fullmatch(pattern, value) is None:
        raise ContractError(f"input {input_id!r} fails declared validation pattern")


def _validate_active_decisions(
    bundle: AnswerContract,
    decisions: dict[str, JsonValue],
) -> None:
    active = active_decision_ids(bundle, decisions)
    inactive = sorted(set(decisions) - active - _INDEPENDENT_CARRIERS)
    if inactive:
        profile = decisions.get("setup_profile")
        raise ContractError(
            f"decision_not_active: {inactive}; "
            f"setup_profile selection {profile!r} does not activate them"
        )


def validate_decision_selection(
    decision_id: str,
    selected: JsonValue,
    definition: dict[str, JsonValue],
    decisions: dict[str, JsonValue],
) -> None:
    selection_mode = definition.get("selection_mode")
    _validate_selection_shape(decision_id, selected, selection_mode, definition)
    source = definition.get("option_source")
    if not isinstance(source, dict):
        raise ContractError(f"decision {decision_id!r} has no option_source")
    if source.get("mode") == "discovered":
        _validate_discovered_selection(decision_id, selected, selection_mode)
        return
    _validate_static_selection(decision_id, selected, source, decisions)


def _validate_selection_shape(
    decision_id: str,
    selected: JsonValue,
    selection_mode: JsonValue | None,
    definition: dict[str, JsonValue],
) -> None:
    if selection_mode == "single":
        if not isinstance(selected, str):
            raise ContractError(
                f"single decision {decision_id!r} must select one string"
            )
        return
    if selection_mode not in {"multiple", "ordered_multiple"}:
        return
    if not isinstance(selected, list) or not all(
        isinstance(value, str) for value in selected
    ):
        raise ContractError(
            f"{selection_mode} decision {decision_id!r} must select a string array"
        )
    _validate_selection_bounds(decision_id, selected, definition)


def _validate_selection_bounds(
    decision_id: str,
    selected: list[JsonValue],
    definition: dict[str, JsonValue],
) -> None:
    if len(selected) != len(set(cast(list[str], selected))):
        raise ContractError(f"decision {decision_id!r} contains duplicate selections")
    minimum = definition.get("minimum_selections", 0)
    maximum = definition.get("maximum_selections")
    if isinstance(minimum, int) and len(selected) < minimum:
        raise ContractError(
            f"decision {decision_id!r} has fewer than {minimum} selections"
        )
    if isinstance(maximum, int) and len(selected) > maximum:
        raise ContractError(
            f"decision {decision_id!r} has more than {maximum} selections"
        )


def _validate_discovered_selection(
    decision_id: str,
    selected: JsonValue,
    selection_mode: JsonValue | None,
) -> None:
    if selection_mode == "single" and not isinstance(selected, str):
        raise ContractError(
            f"discovered decision {decision_id!r} must select one string id"
        )


def _validate_static_selection(
    decision_id: str,
    selected: JsonValue,
    source: dict[str, JsonValue],
    decisions: dict[str, JsonValue],
) -> None:
    options = source.get("options")
    if not isinstance(options, dict):
        raise ContractError(f"static decision {decision_id!r} has no options")
    values = selected if isinstance(selected, list) else [selected]
    valid = all(isinstance(value, str) and value in options for value in values)
    if not valid:
        raise ContractError(
            f"decision {decision_id!r} selects an unknown option: {selected!r}"
        )
    for value in values:
        if not static_option_available(options[str(value)], decisions):
            raise ContractError(
                f"decision {decision_id!r} option {value!r} is not available "
                "under the resolved decisions"
            )


def static_option_available(
    option: JsonValue,
    decisions: dict[str, JsonValue],
) -> bool:
    """Require supported declared status and every input to an option condition."""

    if not isinstance(option, dict):
        raise ContractError("static decision option must be an object")
    if "availability" in option and option["availability"] != "supported":
        return False
    if "available_when" not in option:
        return True
    condition = option["available_when"]
    if any(ref not in decisions for ref in condition_refs(condition)):
        return False
    return condition_matches(condition, decisions)


def _validate_consents(consents: dict[str, JsonValue]) -> None:
    for consent_id, selected in consents.items():
        if not isinstance(selected, bool):
            raise ContractError(f"consent {consent_id!r} must be a boolean")
