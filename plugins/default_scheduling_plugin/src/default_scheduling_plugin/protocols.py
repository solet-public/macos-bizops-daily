"""Type protocols for dependency injection.

This module consolidates all Protocol definitions used across the plugin
to avoid duplication and ensure consistent typing.

NOTE: StateServiceProtocol is imported from the canonical location:
    from ananta.interfaces.state_service_protocol import StateServiceProtocol
"""

from typing import Any, Protocol

RELOAD_SAFE = True


class ActionFactoryProtocol(Protocol):
    """Protocol for ActionFactory dependency."""

    def submit_action_definition(
        self,
        action_definition: dict[str, Any],
        context: dict[str, Any] | None = None,
    ) -> str:
        """Submit an action definition for execution.

        Returns:
            str: The action_id of the submitted action

        Raises:
            FrameworkError: If submission fails
        """
        ...
