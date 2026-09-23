"""Closed recursive evaluation for flow ``required_when`` expressions."""

from __future__ import annotations

from .decision_state import selected_value
from .errors import ContractError
from .models import JsonValue


def condition_is_inactive(
    condition: JsonValue,
    decisions: dict[str, JsonValue],
    *,
    inactive_decision_ids: frozenset[str] = frozenset(),
) -> bool:
    if condition is None:
        return False
    refs = condition_refs(condition)
    unresolved = tuple(item for item in refs if item not in decisions)
    if unresolved:
        return all(item in inactive_decision_ids for item in unresolved)
    return not condition_matches(condition, decisions)


def condition_refs(value: JsonValue) -> tuple[str, ...]:
    if not isinstance(value, dict):
        return ()
    direct = value.get("decision_ref")
    if isinstance(direct, str):
        return (direct,)
    refs: list[str] = []
    for child in value.values():
        refs.extend(_child_condition_refs(child))
    return tuple(refs)


def _child_condition_refs(value: JsonValue) -> tuple[str, ...]:
    if isinstance(value, dict):
        return condition_refs(value)
    if isinstance(value, list):
        return tuple(ref for item in value for ref in condition_refs(item))
    return ()


def condition_matches(
    value: JsonValue,
    decisions: dict[str, JsonValue],
) -> bool:
    if not isinstance(value, dict):
        raise ContractError("required_when must be an object")
    if any(not _has_resolved_selection(decisions.get(ref)) for ref in condition_refs(value)):
        return False
    compound = _compound_result(value, decisions)
    if compound is not None:
        return compound
    return _leaf_result(value, decisions)


def _has_resolved_selection(value: JsonValue | None) -> bool:
    selected = selected_value(value)
    return selected is not None and selected is not False and selected != ""


def _compound_result(
    value: dict[str, JsonValue],
    decisions: dict[str, JsonValue],
) -> bool | None:
    if "all" in value:
        items = _condition_array(value["all"], "required_when.all")
        return all(condition_matches(item, decisions) for item in items)
    if "any" in value:
        items = _condition_array(value["any"], "required_when.any")
        return any(condition_matches(item, decisions) for item in items)
    if "not" in value:
        return not condition_matches(value["not"], decisions)
    return None


def _condition_array(value: JsonValue, label: str) -> list[JsonValue]:
    if not isinstance(value, list):
        raise ContractError(f"{label} must be an array")
    return value


def _leaf_result(
    value: dict[str, JsonValue],
    decisions: dict[str, JsonValue],
) -> bool:
    decision_id = value.get("decision_ref")
    if not isinstance(decision_id, str) or decision_id not in decisions:
        return False
    actual = selected_value(decisions[decision_id])
    expected = value.get("value")
    operator = value.get("operator")
    if operator == "equals":
        return actual == expected
    if operator == "not_equals":
        return actual != expected
    if operator == "contains" and isinstance(actual, list):
        return expected in actual
    if operator == "not_contains" and isinstance(actual, list):
        return expected not in actual
    raise ContractError(f"unsupported setup condition operator: {operator!r}")
