"""
Process Key Resolver Service

Responsibility: Handle process key resolution logic for action execution
Dependencies: ProcessRegistryUtil
Complexity: Medium - focused on determining the correct process key for actions

Extracted from ActionManager god class during Phase 2 refactoring
"""

import logging
from collections.abc import Callable

logger = logging.getLogger(__name__)


class ProcessKeyResolver:
    """
    Service for resolving process keys during action execution.

    ARCHITECTURAL ROLE: Supporting service that extracts process key resolution logic
    from ActionManager while maintaining action execution integrity.

    This service handles:
        pass
    - Determining the correct process key for action execution
    - Handling fallback strategies for process key resolution
    - Managing plugin-specific and action-specific process key formats
    """

    def __init__(self, process_registry_util: object):
        """Initialize ProcessKeyResolver with required dependencies."""
        self.process_registry_util = process_registry_util

    def resolve_process_key(
        self,
        action_name: str,
        action_parameters: dict[str, object],
        action_def_or_parameters: dict[str, object],
        prepared_action_def: dict[str, object] | None,
        process_key: str | None,
        state: dict[str, object],
        action_definition_getter: (
            Callable[[str, dict[str, object] | None, str | None], dict[str, object] | None] | None
        ) = None,
    ) -> str:
        """
        Resolve the process key for action execution with multiple fallback strategies.

        Args:
            action_name: Name of the action being executed
            action_parameters: Parameters passed to the action
            action_def_or_parameters: Combined definition or parameters dict
            prepared_action_def: Pre-prepared action definition if available
            process_key: Explicit process key if provided
            state: Current execution state
            action_definition_getter: Callback to get action definition (avoids circular deps)

        Returns:
            Resolved process key string
        """
        # Try each resolution strategy in priority order
        result = self._try_explicit_process_key(process_key)
        if result:
            return result

        result = self._try_action_parameters_key(action_parameters)
        if result:
            return result

        result = self._try_prepared_action_def_key(prepared_action_def)
        if result:
            return result

        result = self._try_action_def_or_parameters_key(action_def_or_parameters)
        if result:
            return result

        result = self._try_action_definition_callback_key(
            action_name, state, action_definition_getter
        )
        if result:
            return result

        return self._resolve_fallback_process_key(action_name)

    def _try_explicit_process_key(self, process_key: str | None) -> str | None:
        """Priority 1: Use explicitly provided process_key."""
        if process_key:
            return process_key
        return None

    def _try_action_parameters_key(self, action_parameters: dict[str, object]) -> str | None:
        """Priority 2: Extract from action parameters."""
        if action_parameters and "process_key" in action_parameters:
            resolved_key = action_parameters["process_key"]
            if isinstance(resolved_key, str):
                return resolved_key
        return None

    def _try_prepared_action_def_key(
        self, prepared_action_def: dict[str, object] | None
    ) -> str | None:
        """Priority 3: Extract from prepared action definition."""
        if prepared_action_def and "process_key" in prepared_action_def:
            resolved_key = prepared_action_def["process_key"]
            if isinstance(resolved_key, str):
                return resolved_key
        return None

    def _try_action_def_or_parameters_key(
        self, action_def_or_parameters: dict[str, object]
    ) -> str | None:
        """Priority 4: Extract from action_def_or_parameters."""
        if action_def_or_parameters and "process_key" in action_def_or_parameters:
            resolved_key = action_def_or_parameters["process_key"]
            if isinstance(resolved_key, str):
                return resolved_key
        return None

    def _try_action_definition_callback_key(
        self,
        action_name: str,
        state: dict[str, object],
        action_definition_getter: Callable[
            [str, dict[str, object] | None, str | None], dict[str, object] | None
        ]
        | None,
    ) -> str | None:
        """Priority 5: Try to get from action definition via callback."""
        if not action_definition_getter:
            return None

        try:
            action_def = action_definition_getter(action_name, state, None)
            if action_def and "process_key" in action_def:
                resolved_key = action_def["process_key"]
                if isinstance(resolved_key, str):
                    return resolved_key
        except Exception:
            pass
        return None

    def _resolve_fallback_process_key(self, action_name: str) -> str:
        """Priority 6-7: Fallback resolution using action name."""
        # Priority 6: Check if action name contains plugin prefix
        if "." in action_name:
            # Format: plugin_name.action_name
            return action_name

        # Priority 7: Default to action_name
        return action_name
