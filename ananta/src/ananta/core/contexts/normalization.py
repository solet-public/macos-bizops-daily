"""ID normalization utilities.

This module provides functions to normalize flow_id and session_id values,
ensuring that invalid values (empty strings, "None", whitespace) are
converted to None rather than being propagated as corrupt data.

The key insight from Codex's analysis:
> The recurring malfunction is not that flow IDs are missing -- it is that
> *invalid* values are being treated as valid. The fix is not just to
> "propagate flow_id better," but to enforce a strict contract:
> **flow_id is either a valid non-empty ID or it is absent**.
"""

import logging
import re

from ananta.constants import (
    ID_PREFIX_FLOW,
    ID_PREFIX_SESSION,
    INVALID_ID_VALUES,
)

logger = logging.getLogger(__name__)

# Valid ID patterns - flow IDs start with "{prefix}-", session IDs with "{prefix}-"
# These use the constants from ananta.constants to avoid magic strings
FLOW_ID_PATTERN = re.compile(rf"^{ID_PREFIX_FLOW}-[a-zA-Z0-9_-]+$")
SESSION_ID_PATTERN = re.compile(rf"^{ID_PREFIX_SESSION}-[a-zA-Z0-9_-]+$")


def normalize_flow_id(value: object) -> str | None:
    """Normalize a flow_id value, returning None for invalid values.

    This function enforces the contract that flow_id is either a valid
    non-empty string starting with "flow-" or it is None. Empty strings,
    whitespace, "None", and other invalid values are normalized to None.

    Args:
        value: The raw flow_id value (may be str, None, or other types).

    Returns:
        A valid flow_id string, or None if the value is invalid.

    Examples:
        >>> normalize_flow_id("flow-abc123")
        'flow-abc123'
        >>> normalize_flow_id("")
        None
        >>> normalize_flow_id("None")
        None
        >>> normalize_flow_id(None)
        None
        >>> normalize_flow_id("FLOW_ID")  # Unresolved placeholder
        None
    """
    if value is None:
        return None

    if not isinstance(value, str):
        logger.error(f"flow_id is not a string: {type(value).__name__}")
        return None

    # Strip whitespace
    stripped = value.strip()

    # Check for known invalid values
    if stripped in INVALID_ID_VALUES:
        return None

    # Strictly validate pattern - no legacy tolerance
    # Valid flow_id: "flow-" followed by alphanumeric, underscore, or hyphen
    if not FLOW_ID_PATTERN.match(stripped):
        logger.error(f"flow_id '{stripped}' does not match expected pattern ^flow-[a-zA-Z0-9_-]+$")
        return None

    return stripped


def normalize_session_id(value: object) -> str | None:
    """Normalize a session_id value, returning None for invalid values.

    This function enforces the contract that session_id is either a valid
    non-empty string starting with "sess-" or it is None.

    Args:
        value: The raw session_id value (may be str, None, or other types).

    Returns:
        A valid session_id string, or None if the value is invalid.

    Examples:
        >>> normalize_session_id("sess-abc123")
        'sess-abc123'
        >>> normalize_session_id("")
        None
        >>> normalize_session_id("None")
        None
    """
    if value is None:
        return None

    if not isinstance(value, str):
        logger.error(f"session_id is not a string: {type(value).__name__}")
        return None

    # Strip whitespace
    stripped = value.strip()

    # Check for known invalid values
    if stripped in INVALID_ID_VALUES:
        return None

    # Strictly validate pattern - no legacy tolerance
    # Valid session_id: "sess-" followed by alphanumeric, underscore, or hyphen
    if not SESSION_ID_PATTERN.match(stripped):
        logger.error(f"session_id '{stripped}' does not match expected pattern ^sess-[a-zA-Z0-9_-]+$")
        return None

    return stripped


def is_valid_flow_id(value: object) -> bool:
    """Check if a value is a valid flow_id.

    Args:
        value: The value to check.

    Returns:
        True if the value is a valid, non-empty flow_id string.
    """
    return normalize_flow_id(value) is not None


def is_valid_session_id(value: object) -> bool:
    """Check if a value is a valid session_id.

    Args:
        value: The value to check.

    Returns:
        True if the value is a valid, non-empty session_id string.
    """
    return normalize_session_id(value) is not None
