"""Interactive selection for target-discovered decision candidates."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import cast

from .decision_state import declined_answer
from .errors import ConfigError
from .models import CommandResult, JsonValue


def prompt_discovered_decisions(
    preview: CommandResult,
    existing: dict[str, JsonValue],
) -> dict[str, JsonValue]:
    raw_prompts = preview.data.get("decision_prompts")
    if not isinstance(raw_prompts, list):
        return existing
    selected = dict(existing)
    for raw_prompt in raw_prompts:
        choice = _prompt_one(raw_prompt, selected)
        if choice is not None:
            decision_id, value = choice
            selected[decision_id] = value
    return selected


def _prompt_one(
    raw_prompt: JsonValue,
    selected: dict[str, JsonValue],
) -> tuple[str, JsonValue] | None:
    if not isinstance(raw_prompt, dict):
        return None
    decision_id = raw_prompt.get("id")
    candidates = raw_prompt.get("candidates")
    if (
        not isinstance(decision_id, str)
        or decision_id in selected
        or not isinstance(candidates, list)
    ):
        return None
    valid = [item for item in candidates if isinstance(item, dict)]
    if not valid:
        return None
    _print_candidates(raw_prompt, decision_id, valid)
    mode = raw_prompt.get("selection_mode", "single")
    indexes = _selection_indexes(decision_id, mode, len(valid))
    if indexes is None:
        return decision_id, declined_answer(
            decided_at=datetime.now(UTC).isoformat(), decided_by="interactive"
        )
    return decision_id, _selected_values(decision_id, mode, valid, indexes)


def _print_candidates(
    prompt: dict[str, JsonValue],
    decision_id: str,
    candidates: list[dict[str, JsonValue]],
) -> None:
    print(f"{prompt.get('title', decision_id)}: {prompt.get('prompt', '')}")
    for index, candidate in enumerate(candidates, start=1):
        print(
            f"  {index}. {candidate.get('label', candidate.get('value'))} "
            f"[{candidate.get('value')}] {candidate.get('metadata', {})}"
        )


def _selection_indexes(
    decision_id: str,
    mode: JsonValue,
    candidate_count: int,
) -> list[int] | None:
    multiple = mode in {"multiple", "ordered_multiple"}
    prompt_text = (
        "Select candidate numbers in order, comma-separated (blank to decline): "
        if multiple
        else "Select candidate number (blank to decline): "
    )
    answer = input(prompt_text).strip()
    if not answer:
        return None
    raw_indexes = answer.split(",")
    if not all(item.strip().isdigit() for item in raw_indexes):
        raise ConfigError(f"invalid candidate selection for {decision_id!r}")
    indexes = [int(item.strip()) for item in raw_indexes]
    if not _indexes_are_valid(indexes, candidate_count, multiple):
        raise ConfigError(f"invalid candidate selection for {decision_id!r}")
    return indexes


def _indexes_are_valid(
    indexes: list[int],
    candidate_count: int,
    multiple: bool,
) -> bool:
    return bool(
        indexes
        and all(1 <= index <= candidate_count for index in indexes)
        and len(indexes) == len(set(indexes))
        and (multiple or len(indexes) == 1)
    )


def _selected_values(
    decision_id: str,
    mode: JsonValue,
    candidates: list[dict[str, JsonValue]],
    indexes: list[int],
) -> JsonValue:
    values = [candidates[index - 1].get("value") for index in indexes]
    if not all(isinstance(value, str) for value in values):
        raise ConfigError(f"candidate for {decision_id!r} has no exact value")
    strings = cast(list[str], values)
    return strings[0] if mode == "single" else cast(list[JsonValue], strings)
