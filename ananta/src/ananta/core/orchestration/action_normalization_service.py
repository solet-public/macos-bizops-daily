"""
Action Normalization Service

Responsibility: Normalize action dictionaries for event processing
Dependencies: None (pure data transformation)
Complexity: Low - stateless data normalization

Extracted from Phase2MemoryOperations to remove phase coordinator dependencies.
"""

import logging
from datetime import UTC, datetime

from ananta.core.events.utils import generate_action_name
from ananta.core.plugins.plugin_contracts import ActionStatus

logger = logging.getLogger(__name__)


def _is_valid_action_dict(action: object) -> bool:
    """Check if action is a valid dict with required fields.

    Args:
        action: Object to check

    Returns:
        True if action is a dict with 'name' or 'process_key'
    """
    if not isinstance(action, dict):
        return False
    return "name" in action or "process_key" in action


def _log_action_details(action: dict[str, object]) -> None:
    """Log action details for debugging.

    Args:
        action: Action dictionary to log
    """
    _action_name = generate_action_name(action, "action_normalization")


def _remove_deprecated_parameters_field(action_dict: dict[str, object]) -> None:
    """Remove deprecated 'parameters' field from action dict.

    Args:
        action_dict: Action dictionary to modify in-place
    """
    if "parameters" not in action_dict:
        return

    action_identifier = action_dict.get("name") or action_dict.get("process_key", "unknown")
    logger.error(
        f"ActionNormalizationService: Removing deprecated 'parameters' field from action '{action_identifier}'"
    )
    action_dict.pop("parameters", None)


def _add_runtime_metadata(action_dict: dict[str, object]) -> None:
    """Add runtime metadata fields to action dict.

    Args:
        action_dict: Action dictionary to modify in-place
    """
    action_dict["_runtime_generated"] = True
    action_dict["_validation_context"] = "runtime"
    action_dict["_source"] = "event_result_processor"


def _set_default_status_and_timestamp(action_dict: dict[str, object], timestamp: str) -> None:
    """Set default status and timestamp if not present.

    Args:
        action_dict: Action dictionary to modify in-place
        timestamp: ISO format timestamp string
    """
    if "action_status" not in action_dict:
        action_dict["action_status"] = ActionStatus.QUEUED.value
    if "timestamp" not in action_dict:
        action_dict["timestamp"] = timestamp


class ActionNormalizationService:
    """
    Service for normalizing action dictionaries for event processing.

    Design Principles:
    - Single Responsibility: Action normalization only
    - Stateless: No instance state, pure data transformation
    - Fail Fast: Invalid actions return None
    - Type Safety: Proper type checking and validation
    """

    @staticmethod
    async def normalize_action_for_events(action: object) -> dict[str, object] | None:
        """Normalize action for event processing (pure data transformation).

        Args:
            action: Action object to normalize (dict, str, or other)

        Returns:
            Normalized action dict, or None if invalid
        """

        if isinstance(action, dict):
            _log_action_details(action)

        if isinstance(action, str):
            logger.error(f"ActionNormalizationService: String action '{action}' not supported")
            return None

        if not _is_valid_action_dict(action):
            logger.error(
                "ActionNormalizationService: Action rejected - missing both 'name' and 'process_key'"
            )
            return None

        # Type narrowing: we know action is a dict at this point (validated by _is_valid_action_dict)
        # mypy doesn't understand that _is_valid_action_dict narrows the type
        assert isinstance(action, dict)
        action_dict: dict[str, object] = dict(action)

        # Use centralized action name generation
        _action_name = generate_action_name(
            action_dict, "action_normalization_validation", modify_dict=True
        )

        timestamp = datetime.now(UTC).isoformat()

        _remove_deprecated_parameters_field(action_dict)
        _add_runtime_metadata(action_dict)
        _set_default_status_and_timestamp(action_dict, timestamp)

        return action_dict
