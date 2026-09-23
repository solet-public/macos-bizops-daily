"""
Placeholder Utilities

Provides shared utilities for placeholder detection, validation, and replacement.
Enforces STRICT format: <<VAR>> only (no {{VAR}}, {VAR}, $VAR, etc.)

NO BACKWARDS COMPATIBILITY: Invalid formats are rejected immediately.

Supports two context types:
    pass
1. dict[str, Any] - Legacy context (existing behavior)
2. ExecutionContext - New runtime context (Phase 1)
"""

import json
import logging
import re
from typing import TYPE_CHECKING, Any, Union, cast

if TYPE_CHECKING:
    from ananta.core.orchestration.execution_context import ExecutionContext

logger = logging.getLogger(__name__)

# Canonical placeholder pattern: <<UPPERCASE_WITH_UNDERSCORES>> or <<step_id.FIELD>> (Phase 2)
# Phase 1: <<PROCESS_COUNT>>
# Phase 2: <<node_0.PROCESS_COUNT>> or <<node_0.RESULT.process_count>>
PLACEHOLDER_PATTERN = re.compile(r"<<([A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*)>>")

# Invalid patterns we explicitly reject (includes dotted paths for Phase 2)
INVALID_PATTERNS = {
    "curly_double": re.compile(r"{{([A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*)}}"),
    "curly_single": re.compile(r"{([A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*)}"),
    "dollar": re.compile(r"\$([A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*)"),
    "dollar_curly": re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*)\}"),
}


def validate_placeholder_format(placeholder_str: str) -> bool:
    """
    Validate that a placeholder string uses the canonical <<VAR>> format.

    Args:
        placeholder_str: The placeholder string to validate (e.g., "<<VAR>>")

    Returns:
        True if format is valid, False otherwise
    """
    return bool(PLACEHOLDER_PATTERN.fullmatch(placeholder_str))


def normalize_placeholder_formats(obj: Any) -> Any:
    """
    Recursively transform invalid placeholder formats to canonical <<VAR>> format.

    LLMs often generate {{VAR}}, {VAR}, $VAR, ${VAR} formats. This function
    normalizes them to <<VAR>> BEFORE validation, allowing graceful handling
    of LLM output variations.

    Args:
        obj: Object to transform (str, dict, list, or primitive)

    Returns:
        Object with all placeholders normalized to <<VAR>> format
    """
    if isinstance(obj, str):
        result = obj
        # Transform each invalid pattern to canonical format
        for _pattern_name, pattern in INVALID_PATTERNS.items():

            def replace_with_canonical(match: re.Match[str]) -> str:
                var_name = match.group(1)
                return f"<<{var_name}>>"

            result = pattern.sub(replace_with_canonical, result)
        return result

    elif isinstance(obj, dict):
        return {key: normalize_placeholder_formats(value) for key, value in obj.items()}

    elif isinstance(obj, list):
        return [normalize_placeholder_formats(item) for item in obj]

    else:
        return obj


def _format_invalid_match(pattern_name: str, match: str) -> str:
    """Format a single invalid match back to its original placeholder form.

    Args:
        pattern_name: The pattern type identifier
        match: The variable name extracted from the match

    Returns:
        The original placeholder string (e.g., "{{VAR}}", "${VAR}")
    """
    format_map: dict[str, str] = {
        "curly_double": f"{{{{{match}}}}}",
        "curly_single": f"{{{match}}}",
        "dollar": f"${match}",
        "dollar_curly": f"${{{match}}}",
    }
    return format_map[pattern_name]


def _detect_invalid_formats_in_string(text: str) -> list[str]:
    """Detect invalid placeholder formats in a string.

    Args:
        text: String to scan for invalid formats

    Returns:
        List of invalid placeholders found
    """
    invalid_found: list[str] = []
    for pattern_name, pattern in INVALID_PATTERNS.items():
        matches = pattern.findall(text)
        for match in matches:
            invalid_found.append(_format_invalid_match(pattern_name, match))
    return invalid_found


def detect_invalid_formats(obj: Any) -> list[str]:
    """
    Recursively scan object for invalid placeholder formats.

    Args:
        obj: Object to scan (str, dict, list, or primitive)

    Returns:
        List of invalid placeholders found (e.g., ["{{VAR}}", "${OTHER}"])
    """
    if isinstance(obj, str):
        return _detect_invalid_formats_in_string(obj)

    if isinstance(obj, dict):
        invalid_found: list[str] = []
        for value in obj.values():
            invalid_found.extend(detect_invalid_formats(value))
        return invalid_found

    if isinstance(obj, list):
        invalid_found = []
        for item in obj:
            invalid_found.extend(detect_invalid_formats(item))
        return invalid_found

    return []


def find_placeholders(obj: Any, exclude_keys: set[str] | None = None) -> list[str]:
    """
    Recursively find all valid <<VAR>> placeholders in an object.

    Args:
        obj: Object to scan (str, dict, list, or primitive)
        exclude_keys: Set of dictionary keys to exclude from scanning (e.g., {"example_responses", "response_format"})

    Returns:
        List of valid placeholders found (e.g., ["<<VAR>>", "<<OTHER>>"])
    """
    if exclude_keys is None:
        exclude_keys = set()

    placeholders: list[str] = []

    if isinstance(obj, str):
        matches = PLACEHOLDER_PATTERN.findall(obj)
        placeholders.extend([f"<<{match}>>" for match in matches])

    elif isinstance(obj, dict):
        for key, value in obj.items():
            # Skip excluded keys (e.g., example_responses which contain placeholder examples)
            if key not in exclude_keys:
                placeholders.extend(find_placeholders(value, exclude_keys))

    elif isinstance(obj, list):
        for item in obj:
            placeholders.extend(find_placeholders(item, exclude_keys))

    return placeholders


def _replace_string_placeholders(
    data: str, context: Union[dict[str, Any], "ExecutionContext"]
) -> str:
    """Replace placeholders in a string using the appropriate context type.

    Args:
        data: String with placeholders
        context: Dictionary OR ExecutionContext instance

    Returns:
        String with placeholders replaced
    """
    if hasattr(context, "resolve_placeholder"):
        return _replace_with_execution_context(data, cast("ExecutionContext", context))

    if not isinstance(context, dict):
        raise TypeError(f"Expected dict context, got {type(context)}")
    return _replace_with_dict_context(data, context)


def _should_stop_at_action_boundary(
    data: dict[str, Any], depth: int, stop_at_action_boundary: bool
) -> bool:
    """Check if recursion should stop at an action boundary.

    Args:
        data: Dictionary to check
        depth: Current recursion depth
        stop_at_action_boundary: Whether to stop at action boundaries

    Returns:
        True if should stop, False otherwise
    """
    if depth <= 0:
        return False
    if not stop_at_action_boundary:
        return False
    return _is_action_definition(data)


def replace_placeholders_recursive(
    data: Any,
    context: Union[dict[str, Any], "ExecutionContext"],
    depth: int = 0,
    stop_at_action_boundary: bool = True,
) -> Any:
    """
    Recursively replace <<PLACEHOLDER>> values with context values.

    STRICT ENFORCEMENT: Only <<VAR>> format is replaced. All other formats are ignored
    (they should have been rejected by compiler validation).

    MODIFIED: Now accepts ExecutionContext in addition to dict context.

    Args:
        data: Data structure to process
        context: Dictionary OR ExecutionContext instance
        depth: Current recursion depth (for action boundary detection)
        stop_at_action_boundary: If True, stop recursion at nested action definitions

    Returns:
        Data with placeholders replaced

    Note:
        - Stops recursion at action definition boundaries to prevent replacing
          placeholders that belong to future executions
        - Each action definition has its own result context
        - ExecutionContext provides type-safe placeholder resolution
    """
    if isinstance(data, str):
        return _replace_string_placeholders(data, context)

    if isinstance(data, dict):
        if _should_stop_at_action_boundary(data, depth, stop_at_action_boundary):
            logger.debug(f"PlaceholderUtils: Stopping recursion at action boundary (depth={depth})")
            return data

        return {
            key: replace_placeholders_recursive(value, context, depth + 1, stop_at_action_boundary)
            for key, value in data.items()
        }

    if isinstance(data, list):
        return [
            replace_placeholders_recursive(item, context, depth, stop_at_action_boundary)
            for item in data
        ]

    return data


def _replace_with_execution_context(text: str, context: "ExecutionContext") -> str:
    """
    Replace placeholders using ExecutionContext.

    Args:
        text: Text with placeholders
        context: ExecutionContext instance

    Returns:
        Text with placeholders replaced by their values

    Behavior:
        - Find all <<PLACEHOLDER>> patterns
        - Check if each placeholder exists in context
        - Resolve and replace with typed value
        - Format complex types as JSON
        - Leave unresolved placeholders as-is (for later resolution)
    """
    # Find all <<PLACEHOLDER>> patterns
    matches = list(PLACEHOLDER_PATTERN.finditer(text))

    if not matches:
        return text

    # Build replacements (process in reverse to preserve string indices)
    replacements: list[tuple[int, int, str]] = []
    for match in matches:
        placeholder_with_delimiters = match.group(0)  # e.g., "<<PROCESS_COUNT>>"

        # Check if placeholder exists in context
        if context.has_placeholder(placeholder_with_delimiters):
            try:
                value = context.resolve_placeholder(placeholder_with_delimiters)
                # Convert to string for replacement
                value_str = _format_value_for_replacement(value)
                replacements.append((match.start(), match.end(), value_str))
            except Exception:
                raise
        else:
            # Placeholder doesn't exist in context - leave as-is
            pass

    # Apply replacements in reverse order to preserve indices
    for start, end, value_str in reversed(replacements):
        text = text[:start] + value_str + text[end:]

    return text


def _replace_with_dict_context(text: str, context: dict[str, Any]) -> str:
    """
    Replace placeholders using dict context (existing logic).

    Args:
        text: Text with placeholders
        context: Dictionary context

    Returns:
        Text with placeholders replaced
    """
    result_str = text

    # Replace specific field placeholders (e.g., <<JOB_ID>>, <<STATUS>>)
    for key, value in context.items():
        placeholder = f"<<{key.upper()}>>"
        if placeholder in result_str:
            result_str = result_str.replace(placeholder, str(value))

    # Replace common result placeholders
    if "<<RESULT>>" in result_str and "result" in context:
        result_str = result_str.replace("<<RESULT>>", str(context["result"]))

    if "<<QUERY_RESULT>>" in result_str and "result" in context:
        result_str = result_str.replace("<<QUERY_RESULT>>", str(context["result"]))

    return result_str


def _format_value_for_replacement(value: Any) -> str:
    """
    Format a Python value for string replacement.

    Args:
        value: Value to format (any Python type)

    Returns:
        String representation suitable for replacement

    Behavior:
        - Strings: Return as-is
        - Numbers/booleans: Convert to string
        - Lists/dicts: Convert to JSON (preserves structure)
        - None: Return empty string
        - Other: Convert to string
    """
    if isinstance(value, str):
        return value
    elif isinstance(value, int | float | bool):
        return str(value)
    elif isinstance(value, list | dict):
        # For complex types, use JSON representation
        try:
            return json.dumps(value, ensure_ascii=False)
        except (TypeError, ValueError) as e:
            logger.error(f"Failed to JSON-serialize value: {e}, falling back to str()")
            return str(value)
    elif value is None:
        return ""
    else:
        return str(value)


def _is_action_definition(data: Any) -> bool:
    """
    Check if a dict looks like an action definition.

    Action definitions have:
        pass
    - (name OR process_key) AND
    - (parameters OR arguments)
    """
    if not isinstance(data, dict):
        return False

    has_identifier = "name" in data or "process_key" in data
    has_params = "parameters" in data or "arguments" in data

    return has_identifier and has_params
