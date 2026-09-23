"""Action extraction — parse LLM response text into action dicts.

Pure functions for extracting action arrays from structured LLM
responses.  Handles ``actions``, ``plan.steps``, and ``reasoning.steps``
key formats, plus markdown-fenced JSON.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

logger = logging.getLogger(__name__)


def parse_llm_response_for_actions(completion_text: str) -> list[dict[str, Any]]:
    """Parse LLM response to extract actions array.

    Empty list is valid — model signals 'no more work'.
    Returns None if the response is malformed (caller should raise).
    """
    actions = extract_actions_from_string(completion_text)
    if not actions and has_explicit_actions_key(completion_text):
        logger.info("Model returned empty actions array — flow will complete")
        return []
    return actions


def _try_parse_actions(text: str, label: str) -> list[dict[str, Any]] | None:
    """Attempt to parse JSON text as an actions dict; return None on failure."""
    try:
        parsed = json.loads(text)
    except (json.JSONDecodeError, ValueError):
        return None
    if not isinstance(parsed, dict):
        return None
    if label:
        logger.info("JSON_REPAIR: %s", label)
    return extract_actions_from_dict(parsed)


def _strip_markdown_fences(text: str) -> str:
    """Remove markdown code fences wrapping JSON content."""
    fence_match = re.search(r"```(?:json)?\s*\n?(.*?)\n?```", text, re.DOTALL)
    if fence_match:
        return fence_match.group(1).strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*\n?", "", text)
        text = re.sub(r"\n?```\s*$", "", text)
        return text.strip()
    return text


def extract_actions_from_string(content_data: str) -> list[dict[str, Any]]:
    """Extract actions from string LLM response by parsing JSON."""
    cleaned = _strip_markdown_fences(content_data.strip())

    # Direct parse
    result = _try_parse_actions(cleaned, "")
    if result is not None:
        return result

    # Trailing-content repair: extract first complete JSON object, ignoring
    # anything after it (e.g. a metadata trailer the model appended by mistake).
    result = _try_parse_first_json_object(cleaned)
    if result is not None:
        return result

    # Sanitize literal control characters inside JSON string values
    sanitized = _sanitize_json_control_chars(cleaned)
    if sanitized != cleaned:
        result = _try_parse_actions(sanitized, "escaped control chars in strings")
        if result is not None:
            return result
        cleaned = sanitized

    # Brace repair: append missing trailing closers
    repaired = _try_brace_repair(cleaned)
    if repaired is not None:
        result = _try_parse_actions(repaired, "appended missing closing braces/brackets")
        if result is not None:
            return result

    # Extra-closer repair: remove surplus closing braces/brackets
    repaired = _try_extra_closer_repair(cleaned)
    if repaired is not None:
        result = _try_parse_actions(repaired, "removed extra closing brace/bracket")
        if result is not None:
            return result

    return []


def extract_actions_from_dict(content_data: dict[str, Any]) -> list[dict[str, Any]]:
    """Extract actions from dict-structured LLM response."""
    actions = _extract_from_actions_key(content_data)
    if actions is not None:
        return actions
    actions = _extract_from_plan_key(content_data)
    if actions:
        return actions
    actions = _extract_from_reasoning_key(content_data)
    if actions:
        return actions
    return []


def has_explicit_actions_key(completion_text: str) -> bool:
    """Check if the raw response contains an explicit 'actions' key."""
    parsed = _parse_first_json_dict(completion_text.strip())
    return isinstance(parsed, dict) and isinstance(parsed.get("actions"), list)


def extract_reasoning_text(completion_text: str) -> str | None:
    """Extract the reasoning/step_summary string from LLM completion JSON."""
    parsed = _parse_first_json_dict(completion_text.strip())
    if not isinstance(parsed, dict):
        return None
    # Try step_summary first (current schema), then reasoning (legacy)
    for key in ("step_summary", "reasoning"):
        value = parsed.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _parse_first_json_dict(text: str) -> dict[str, Any] | None:
    """Parse the first complete JSON object from text, ignoring trailing content."""
    stripped = text.strip()
    try:
        parsed = json.loads(stripped)
        return parsed if isinstance(parsed, dict) else None
    except json.JSONDecodeError:
        pass
    if not stripped.startswith("{"):
        return None
    try:
        parsed, _ = json.JSONDecoder().raw_decode(stripped)
        return parsed if isinstance(parsed, dict) else None
    except json.JSONDecodeError:
        return None


def validate_actions_found(
    actions: list[dict[str, Any]],
    content_data: Any,
    raw_response: str | None = None,
) -> str | None:
    """Validate that actions were found. Returns error message or None."""
    if actions:
        return None

    error_msg = (
        "LLM response parsing failed: No valid action structure found. "
        "Expected 'actions', 'plan.steps', or 'reasoning.steps'."
    )

    if isinstance(content_data, dict):
        actual_keys = list(content_data.keys())
        error_msg += f"\n\nLLM response keys: {actual_keys}"

        if "plan" in content_data:
            plan = content_data["plan"]
            if isinstance(plan, dict):
                plan_keys = list(plan.keys())
                error_msg += f"\nPlan keys: {plan_keys}"
                if "processes" in plan_keys:
                    error_msg += (
                        "\n\nDETECTED MISTAKE: LLM used 'plan.processes' "
                        "instead of 'plan.steps'. The correct structure is: "
                        '{"plan": {"steps": [...]}}'
                    )
                else:
                    error_msg += f"\n\nEXPECTED: 'steps' in plan, GOT: {plan_keys}"
    else:
        error_msg += f"\n\nLLM response type: {type(content_data).__name__}"

    # Include the raw response and JSON parse error so the error
    # processor can show the model exactly what it wrote and where the
    # parse failed.  This enables single-retry JSON repair.
    if raw_response is not None:
        error_msg += f"\n\nRaw LLM response:\n{raw_response}"
        try:
            json.loads(raw_response)
        except json.JSONDecodeError as exc:
            error_msg += f"\n\nJSON parse error: {exc}"

    return error_msg


# ── Internal helpers ───────────────────────────────────────────────


def _extract_from_actions_key(
    content_data: dict[str, Any],
) -> list[dict[str, Any]] | None:
    """Extract actions from 'actions' key."""
    if "actions" in content_data:
        actions_list = content_data.get("actions")
        if isinstance(actions_list, list):
            return actions_list
    return None


def _extract_from_plan_key(
    content_data: dict[str, Any],
) -> list[dict[str, Any]] | None:
    """Extract actions from 'plan' key (nested or array format)."""
    if "plan" not in content_data:
        return None
    plan = content_data.get("plan")
    if isinstance(plan, dict) and "steps" in plan:
        steps = plan.get("steps")
        if isinstance(steps, list):
            return steps
    elif isinstance(plan, list):
        return plan
    return None


def _extract_from_reasoning_key(
    content_data: dict[str, Any],
) -> list[dict[str, Any]] | None:
    """Extract actions from 'reasoning.steps' key."""
    if "reasoning" in content_data:
        reasoning = content_data.get("reasoning")
        if isinstance(reasoning, dict) and "steps" in reasoning:
            steps = reasoning.get("steps")
            if isinstance(steps, list):
                return steps
    return None


def _try_parse_first_json_object(text: str) -> list[dict[str, Any]] | None:
    """Extract actions from the first complete JSON object, ignoring trailing content.

    Handles the case where the model appends a metadata trailer after its
    primary action JSON object (e.g. a second ``{"namespace": ...}`` blob).
    """
    stripped = text.strip()
    if not stripped.startswith("{"):
        return None
    try:
        parsed, end = json.JSONDecoder().raw_decode(stripped)
    except json.JSONDecodeError:
        return None
    if not isinstance(parsed, dict) or end == len(stripped):
        return None  # No trailing content — already tried by direct parse
    logger.info("JSON_REPAIR: extracted first JSON object (ignored %d trailing chars)", len(stripped) - end)
    return extract_actions_from_dict(parsed)


_CONTROL_ESCAPE: dict[str, str] = {"\n": "\\n", "\t": "\\t", "\r": "\\r"}


def _sanitize_json_control_chars(text: str) -> str:
    """Escape literal control characters inside JSON string values.

    LLMs under unconstrained decoding sometimes emit literal newlines
    or tabs inside JSON string values instead of ``\\n`` / ``\\t``.
    Walks the text tracking string boundaries and replaces literal
    control characters found inside strings with their escape sequences.
    """
    result: list[str] = []
    in_string = False
    escape = False

    for ch in text:
        if escape:
            escape = False
            result.append(ch)
            continue
        if ch == "\\" and in_string:
            escape = True
            result.append(ch)
            continue
        if ch == '"':
            in_string = not in_string
            result.append(ch)
            continue
        if in_string and ch in _CONTROL_ESCAPE:
            result.append(_CONTROL_ESCAPE[ch])
            continue
        result.append(ch)

    return "".join(result)


_OPEN_TO_CLOSE: dict[str, str] = {"{": "}", "[": "]"}
_CLOSERS: frozenset[str] = frozenset(("}", "]"))


def _iter_structural_chars(text: str) -> list[str]:
    """Extract JSON structural characters, skipping string literals."""
    chars: list[str] = []
    in_string = False
    escape = False
    for ch in text:
        if escape:
            escape = False
        elif ch == "\\" and in_string:
            escape = True
        elif ch == '"':
            in_string = not in_string
        elif not in_string:
            chars.append(ch)
    return chars


def _count_unmatched_openers(text: str) -> list[str]:
    """Return stack of expected closing chars for unmatched openers."""
    stack: list[str] = []
    for ch in _iter_structural_chars(text):
        if ch in _OPEN_TO_CLOSE:
            stack.append(_OPEN_TO_CLOSE[ch])
        elif ch in _CLOSERS and stack and stack[-1] == ch:
            stack.pop()
    return stack


def _try_brace_repair(text: str) -> str | None:
    """Attempt to repair JSON with missing trailing braces/brackets.

    Returns ``None`` if the text doesn't start with ``{`` or has no
    unmatched openers — the caller should not retry in that case.
    """
    stripped = text.strip()
    if not stripped.startswith("{"):
        return None

    stack = _count_unmatched_openers(stripped)
    if not stack:
        return None

    repaired = stripped + "".join(reversed(stack))
    return repaired


def _next_string_state(
    ch: str, in_string: bool, escape: bool,
) -> tuple[bool, bool, bool]:
    """Return (in_string, escape, is_structural) after consuming one character.

    is_structural is True only when the character is outside any string and
    was not consumed by escape or quote handling — i.e. it may be a bracket.
    """
    if escape:
        return in_string, False, False
    if ch == "\\" and in_string:
        return in_string, True, False
    if ch == '"':
        return not in_string, False, False
    return in_string, False, not in_string


def _find_surplus_closer_positions(text: str) -> list[int]:
    """Walk JSON text and return positions of unmatched closing brackets."""
    surplus: list[int] = []
    stack: list[str] = []
    in_string = False
    escape = False
    for idx, ch in enumerate(text):
        in_string, escape, is_structural = _next_string_state(ch, in_string, escape)
        if is_structural:
            if ch in _OPEN_TO_CLOSE:
                stack.append(_OPEN_TO_CLOSE[ch])
            elif ch in _CLOSERS:
                if stack and stack[-1] == ch:
                    stack.pop()
                else:
                    surplus.append(idx)
    return surplus


def _try_extra_closer_repair(text: str) -> str | None:
    """Remove a single surplus ``}`` or ``]`` that causes a parse error.

    The model sometimes emits one extra closing brace — e.g.
    ``...,"anchor_step_number":29}}}]}`` instead of ``...":29}}]}``.
    When the structural character balance shows exactly one more closer
    than opener, remove the last unmatched closer and return the result.

    Returns ``None`` when the text has no surplus closer or when the
    surplus is not exactly one character.
    """
    stripped = text.strip()
    if not stripped.startswith("{"):
        return None

    surplus_positions = _find_surplus_closer_positions(stripped)
    if len(surplus_positions) != 1:
        return None

    pos = surplus_positions[0]
    return stripped[:pos] + stripped[pos + 1:]
