"""
Action Event Validation Service

Responsibility: Handle all event validation operations for ActionEventBus
Dependencies: ActionRequestEvent, ActionEventType, logging
Complexity: Medium - focused on event validation and constraint checking

Extracted from ActionEventBus god class (650 lines)
"""

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ananta.services.action_event_bus import ActionRequestEvent

logger = logging.getLogger(__name__)


class ActionEventValidator:
    """
    Service for managing action event validation and rule checking.

    ARCHITECTURAL ROLE: Supporting service that extracts event validation logic
    from ActionEventBus while maintaining event bus integrity.

    This service handles:
    - Event structure and field validation
    - Action data format validation
    - Plugin authentication validation
    - Rate limiting checks
    - Priority constraint validation
    """

    def __init__(self) -> None:
        """Initialize ActionEventValidator."""

        # Initialize required fields mapping to avoid circular imports
        self.REQUIRED_FIELDS = {
            "ACTION_REQUESTED": ["event_id", "action_data", "source_plugin"],
            "ACTION_ACCEPTED": ["event_id", "correlation_id"],
            "ACTION_COMPLETED": ["event_id", "correlation_id"],
            "ACTION_FAILED": ["event_id", "correlation_id", "error_info"],
            "ACTION_CORRECTION_REQUESTED": [
                "event_id",
                "action_data",
                "source_plugin",
                "target_plugin",
                "error_info",
            ],
        }

    def validate_event(self, event: "ActionRequestEvent") -> list[str]:
        """
        Validate an action event for required fields and constraints.

        EXTRACTED FROM: ActionEventValidator.validate_event() - B(9) complexity

        Args:
            event: ActionRequestEvent to validate

        Returns:
            List of validation error messages (empty if valid)
        """
        errors: list[str] = []
        event_type_str = self._get_event_type_str(event)

        self._check_required_fields(event, event_type_str, errors)
        self._check_action_data(event, event_type_str, errors)
        self._check_priority(event, errors)

        return errors

    def _get_event_type_str(self, event: "ActionRequestEvent") -> str:
        """Get event type as string."""
        return (
            event.event_type.value if hasattr(event.event_type, "value") else str(event.event_type)
        )

    def _check_required_fields(
        self, event: "ActionRequestEvent", event_type_str: str, errors: list[str]
    ) -> None:
        """Check required fields for this event type."""
        required = self.REQUIRED_FIELDS.get(event_type_str, [])
        for field in required:
            if not getattr(event, field, None):
                errors.append(f"Missing required field: {field}")

    def _check_action_data(
        self, event: "ActionRequestEvent", event_type_str: str, errors: list[str]
    ) -> None:
        """Validate ACTION_REQUESTED events have proper action_data."""
        if not event.action_data or event_type_str != "ACTION_REQUESTED":
            return

        if "name" not in event.action_data:
            errors.append("action_data missing name field")
            return

        action_name = event.action_data.get("name", "")
        if isinstance(action_name, str):
            if not action_name.replace("_", "").replace("-", "").isalnum():
                errors.append(f"Invalid action name format: {action_name}")
        else:
            errors.append(
                f"Invalid action name type: expected str, got {type(action_name).__name__}"
            )

    def _check_priority(self, event: "ActionRequestEvent", errors: list[str]) -> None:
        """Validate priority constraints."""
        if event.priority < 1 or event.priority > 10:
            errors.append("Priority must be between 1 and 10")

    def validate_plugin_auth(self, event: "ActionRequestEvent", plugin_name: str) -> bool:
        """
        Validate that a plugin is authorized to send an event.

        EXTRACTED FROM: ActionEventValidator.validate_plugin_auth() - A(1) complexity

        Args:
            event: ActionRequestEvent to validate
            plugin_name: Name of the plugin requesting authorization

        Returns:
            True if plugin is authorized, False otherwise
        """
        return event.source_plugin == plugin_name

    def check_rate_limit(self, plugin_name: str) -> bool:
        """
        Check if a plugin is within its rate limits.

        EXTRACTED FROM: ActionEventValidator.check_rate_limit() - A(1) complexity

        Args:
            plugin_name: Name of the plugin to check

        Returns:
            True if within rate limits, False if rate limited
        """
        # TODO: Implement rate limiting logic to track request counts per plugin
        # Currently always returns True - rate limiting logic can be added here
        _ = plugin_name  # Acknowledge parameter is part of public API
        return True
