"""Closed persisted decision carriers and their derived dispositions."""

from __future__ import annotations

from typing import Protocol

from .models import JsonValue

DECISION_DISPOSITIONS = frozenset(
    {
        "selected",
        "pending_newly_introduced",
        "pending_unanswered",
        "skipped_unreached",
        "skipped_condition_false",
        "declined",
    }
)
DECLINED_ANSWER_KEYS = frozenset({"selected", "declined", "declined_at", "declined_by"})


class DecisionCatalog(Protocol):
    @property
    def decisions(self) -> dict[str, dict[str, JsonValue]]: ...


def declined_answer(*, decided_at: str, decided_by: str) -> dict[str, JsonValue]:
    """Build the sole negative answer carrier written by decision review."""

    return {
        "selected": None,
        "declined": True,
        "declined_at": decided_at,
        "declined_by": decided_by,
    }


def is_declined_answer(value: JsonValue) -> bool:
    return (
        isinstance(value, dict)
        and frozenset(value) == DECLINED_ANSWER_KEYS
        and value.get("selected") is None
        and value.get("declined") is True
        and isinstance(value.get("declined_at"), str)
        and bool(value.get("declined_at"))
        and isinstance(value.get("declined_by"), str)
        and bool(value.get("declined_by"))
    )


def selected_value(value: JsonValue) -> JsonValue:
    """Return a usable selection; explicit decline deliberately selects nothing."""

    return None if is_declined_answer(value) else value


def derive_decision_dispositions(
    bundle: DecisionCatalog,
    decisions: dict[str, JsonValue],
    *,
    prior_decision_ids: frozenset[str] = frozenset(),
) -> dict[str, str]:
    """Classify every declared decision without inferring decline from absence."""

    from .condition_evaluator import condition_is_inactive
    from .decision_activation import active_decision_ids

    active = active_decision_ids(bundle, decisions)
    inactive = frozenset(bundle.decisions) - active
    result: dict[str, str] = {}
    for decision_id, definition in bundle.decisions.items():
        value = decisions.get(decision_id)
        if is_declined_answer(value):
            result[decision_id] = "declined"
        elif decision_id not in active:
            result[decision_id] = "skipped_unreached"
        elif condition_is_inactive(
            definition.get("required_when"), decisions, inactive_decision_ids=inactive
        ):
            result[decision_id] = "skipped_condition_false"
        elif value is not None:
            result[decision_id] = "selected"
        elif decision_id not in prior_decision_ids:
            result[decision_id] = "pending_newly_introduced"
        else:
            result[decision_id] = "pending_unanswered"
    return result
