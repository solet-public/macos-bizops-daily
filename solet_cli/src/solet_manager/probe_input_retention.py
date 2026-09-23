"""Retain reviewed public inputs still consumed by active completion probes."""

from __future__ import annotations

from .condition_evaluator import condition_matches
from .contracts import ContractBundle
from .errors import ContractError
from .models import JsonValue
from .probe_input_projection import probe_recorded_input_refs


def retain_probe_inputs(
    bundle: ContractBundle,
    decisions: dict[str, JsonValue],
    recorded_answers: dict[str, JsonValue] | None,
    public_inputs: dict[str, JsonValue],
) -> list[JsonValue]:
    """Carry only admitted values and their original resolution evidence.

    Never synthesize an input absent from the recorded answers. Normalized
    answer validation still checks retained values against the pinned schema.
    Operation requests continue to project their own declared input refs.
    """

    if recorded_answers is None:
        return []
    recorded = recorded_answers.get("public_inputs")
    if not isinstance(recorded, dict):
        raise ContractError("recorded public inputs are not an object")
    retained: list[JsonValue] = []
    for input_id in sorted(_active_probe_inputs(bundle, decisions) - public_inputs.keys()):
        if input_id not in recorded:
            continue
        definition = bundle.inputs.get(input_id)
        if definition is None or definition.get("sensitive") is True:
            raise ContractError(f"probe input {input_id!r} is not a declared public input")
        retained.append(_reviewed_evidence(recorded_answers, input_id))
        public_inputs[input_id] = recorded[input_id]
    return retained


def _active_probe_inputs(bundle: ContractBundle, decisions: dict[str, JsonValue]) -> set[str]:
    required: set[str] = set()
    for probe_id in bundle.completion_probe_ids:
        definition = bundle.probes[probe_id]
        condition = definition.get("required_when")
        if condition is not None and not condition_matches(condition, decisions):
            continue
        required.update(probe_recorded_input_refs(str(definition["probe_ref"])))
    return required


def _reviewed_evidence(answers: dict[str, JsonValue], input_id: str) -> dict[str, JsonValue]:
    raw = answers.get("resolution_evidence")
    if not isinstance(raw, list):
        raise ContractError(f"recorded probe input {input_id!r} has no resolution evidence")
    matches = [item for item in raw if isinstance(item, dict) and item.get("id") == input_id]
    if len(matches) != 1 or matches[0].get("source") not in {"flow_default", "flag", "config", "interactive", "preapproved"}:
        raise ContractError(f"recorded probe input {input_id!r} lacks one reviewed resolution receipt")
    return dict(matches[0])
