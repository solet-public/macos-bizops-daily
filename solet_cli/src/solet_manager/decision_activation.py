"""Recursive activation of flow-declared setup decisions."""

from __future__ import annotations

from typing import Protocol

from .decision_state import selected_value
from .errors import ContractError
from .models import JsonValue


class DecisionCatalog(Protocol):
    @property
    def decisions(self) -> dict[str, dict[str, JsonValue]]: ...


def active_decision_ids(
    bundle: DecisionCatalog,
    decisions: dict[str, JsonValue],
) -> frozenset[str]:
    """Return the one recursive follow-up closure used by every consumer."""

    active = {"setup_profile"} & set(bundle.decisions)
    pending = list(active)
    while pending:
        decision_id = pending.pop()
        selected = selected_value(decisions.get(decision_id))
        if selected is None:
            continue
        for followup in _selected_followups(
            decision_id,
            selected,
            bundle.decisions[decision_id],
        ):
            if followup not in active:
                active.add(followup)
                pending.append(followup)
    return frozenset(active)


def _selected_followups(
    decision_id: str,
    selected: JsonValue,
    definition: dict[str, JsonValue],
) -> tuple[str, ...]:
    source = definition.get("option_source")
    options = source.get("options") if isinstance(source, dict) else None
    if not isinstance(options, dict):
        return ()
    values = selected if isinstance(selected, list) else [selected]
    followups: list[str] = []
    for option_id in values:
        option = options.get(str(option_id))
        if isinstance(option, dict):
            followups.extend(_followup_refs(decision_id, option_id, option))
    return tuple(followups)


def _followup_refs(
    decision_id: str,
    option_id: JsonValue,
    option: dict[str, JsonValue],
) -> tuple[str, ...]:
    raw = option.get("followup_decision_refs", [])
    if not isinstance(raw, list) or not all(isinstance(item, str) for item in raw):
        raise ContractError(
            f"decision {decision_id!r} option {option_id!r} has invalid follow-up refs"
        )
    return tuple(item for item in raw if isinstance(item, str))
