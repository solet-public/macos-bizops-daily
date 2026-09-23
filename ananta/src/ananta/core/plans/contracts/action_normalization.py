"""Action normalization — build action definitions from raw LLM output.

Converts raw action dicts from the LLM into normalized action definitions
with proper process_key, arguments, and context fields.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


def extract_process_parts(action: dict[str, Any]) -> list[str] | None:
    """Extract [provider_type, provider, function_name] from action's process object.

    Returns None if the process object is missing or invalid.
    """
    process_obj = action.get("process")
    if not isinstance(process_obj, dict):
        return None
    return extract_parts_from_process_object(process_obj)


def extract_parts_from_process_object(
    process_obj: dict[str, Any],
) -> list[str] | None:
    """Extract parts from structured process object."""
    provider_type = process_obj.get("provider_type")
    provider = process_obj.get("provider")
    function_name = process_obj.get("function_name")

    if not all([provider_type, provider, function_name]):
        return None
    if not all(isinstance(v, str) for v in [provider_type, provider, function_name]):
        return None
    return [str(provider_type), str(provider), str(function_name)]


def build_action_definition(
    action: dict[str, Any],
    parts: list[str],
    session_id: str | None,
    flow_id: str | None,
    context_id: str | None = None,
) -> dict[str, Any]:
    """Build a normalized action definition from raw action and parsed parts."""
    semantic_name = action.get("name") or parts[2]
    process_key = "::".join(parts[:3])

    action_def: dict[str, Any] = {
        "name": semantic_name,
        "description": action.get("description", f"Action from inference: {semantic_name}"),
        "process_key": process_key,
        "process": {
            "provider_type": parts[0],
            "provider": parts[1],
            "function_name": parts[2],
        },
        "arguments": dict(action.get("arguments", {})),
    }

    if session_id:
        action_def["session_id"] = session_id
    if flow_id:
        action_def["flow_id"] = flow_id
    if context_id:
        action_def["context_id"] = context_id
    if "notes" in action:
        action_def["notes"] = action["notes"]
    if "result_processor" in action:
        action_def["result_processor"] = action["result_processor"]

    return action_def


def _resolve_string_placeholders(text: str, context: dict[str, Any]) -> str:
    """Resolve all <<PLACEHOLDER>> patterns in a string."""
    for placeholder, replacement in context.items():
        text = text.replace(f"<<{placeholder}>>", str(replacement))
    return text


def resolve_placeholders_in_dict(
    data: dict[str, Any], context: dict[str, Any],
) -> dict[str, Any]:
    """Recursively resolve placeholders like <<USER_INPUT>> in a dictionary."""
    result: dict[str, Any] = {}
    for key, value in data.items():
        if isinstance(value, str):
            for placeholder, replacement in context.items():
                value = value.replace(f"<<{placeholder}>>", str(replacement))
            result[key] = value
        elif isinstance(value, dict):
            result[key] = resolve_placeholders_in_dict(value, context)
        elif isinstance(value, list):
            result[key] = [
                resolve_placeholders_in_dict(item, context)
                if isinstance(item, dict)
                else _resolve_string_placeholders(item, context)
                if isinstance(item, str) and context
                else item
                for item in value
            ]
        else:
            result[key] = value
    return result


def inject_observation_into_create_extended_plan(
    actions: list[dict[str, Any]],
    observation: str | None,
) -> None:
    """Inject the current tool observation into create_extended_plan actions."""
    if not observation:
        return
    for action in actions:
        process = action.get("process", {})
        if not isinstance(process, dict):
            continue
        if process.get("function_name") != "create_extended_plan":
            continue
        arguments = action.get("arguments", {})
        if isinstance(arguments, dict):
            arguments["context"] = observation
            logger.info(
                "Injected observation (%d chars) into create_extended_plan context",
                len(observation),
            )


def inject_job_context(
    actions: list[dict[str, Any]], job_id: str | None,
) -> None:
    """Ensure post_message actions carry job_result_ref for attachment routing."""
    if not job_id:
        return
    for action_def in actions:
        process_key = action_def.get("process_key", "")
        if not process_key.endswith("::post_message"):
            continue
        if "job_result_ref" not in action_def:
            action_def["job_result_ref"] = job_id
