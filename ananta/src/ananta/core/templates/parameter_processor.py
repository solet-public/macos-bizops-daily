"""
Parameter Processing Service

Responsibility: Handle parameter parsing, file reference resolution, and action argument extraction
Dependencies: VariableResolver
Complexity: Low - focused parameter processing operations

Extracted from ActionManager god class during Phase 4 decomposition
"""

import logging
from typing import Protocol

logger = logging.getLogger(__name__)


class VariableResolverProtocol(Protocol):
    """Protocol for VariableResolver dependency."""

    def load_file_reference(
        self, filename: str
    ) -> dict[str, object] | list[object] | str | None: ...  # noqa: E501


class ParameterProcessor:
    """
    Service for processing action parameters and resolving file references.

    ARCHITECTURAL ROLE: Supporting service that extracts parameter processing logic
    from ActionManager to create cleaner separation of concerns.

    This service handles:
    - File reference resolution (____@filename.json____ patterns)
    - Action parameter parsing (prepared actions vs raw parameters)
    - Parameter structure validation and normalization
    """

    def __init__(self, variable_resolver: VariableResolverProtocol) -> None:
        """Initialize ParameterProcessor with variable resolver dependency."""
        self.variable_resolver = variable_resolver

    def resolve_file_references(self, action_def: dict[str, object]) -> dict[str, object]:
        """
        Recursively resolve file references in action definition.

        File references use the pattern __@filename.json__ and are replaced
        with the actual file content loaded via VariableResolver.

        Args:
            action_def: Action definition potentially containing file references

        Returns:
            Action definition with file references resolved
        """
        logger.debug(
            f"FILE_REFERENCE: Resolving references in action: {action_def.get('name', 'unknown')}"
        )

        def resolve_value(value: object) -> object:
            if isinstance(value, str) and value.startswith("__@") and value.endswith("__"):
                # Extract filename from __@filename.json__
                filename = value[3:-2]  # Remove __@ and __
                logger.debug(f"FILE_REFERENCE: Found reference to {filename}")

                file_content = self.variable_resolver.load_file_reference(filename)
                if file_content is not None:
                    logger.debug(f"FILE_REFERENCE: Successfully resolved {filename}")
                    return file_content
                else:
                    logger.error(
                        f"FILE_REFERENCE: Could not resolve {filename}, keeping original value"
                    )
                    return value
            elif isinstance(value, dict):
                return {k: resolve_value(v) for k, v in value.items()}
            elif isinstance(value, list):
                return [resolve_value(item) for item in value]
            else:
                return value

        resolved = resolve_value(action_def)
        # Type narrowing: ensure return type is dict[str, object]
        if not isinstance(resolved, dict):
            logger.error(f"FILE_REFERENCE: Expected dict but got {type(resolved)}")
            return action_def
        return resolved

    def parse_action_parameters(
        self, action_name: str, action_def_or_parameters: dict[str, object]
    ) -> tuple[dict[str, object], dict[str, object] | None, bool]:
        """
        Parse action definition or parameters into structured format.

        Handles both prepared action definitions (with name/arguments structure)
        and legacy parameter-only inputs.

        Args:
            action_name: Name of the action being processed
            action_def_or_parameters: Either full action definition or just parameters

        Returns:
            tuple: (action_parameters, prepared_action_def, is_prepared_action)
        """
        # Determine if we received a full action definition or just parameters
        if "name" in action_def_or_parameters and "arguments" in action_def_or_parameters:
            # Full prepared action definition received
            prepared_action_def = action_def_or_parameters
            arguments_value = prepared_action_def.get("arguments", {})
            # Type narrowing: ensure arguments is dict[str, object]
            if not isinstance(arguments_value, dict):
                logger.error(f"Expected arguments to be dict but got {type(arguments_value)}")
                action_parameters: dict[str, object] = {}
            else:
                action_parameters = arguments_value
            is_prepared_action = True
            logger.debug(f"Received full prepared action definition for '{action_name}'")
        else:
            # Legacy: received just parameters
            action_parameters = action_def_or_parameters
            prepared_action_def = None
            is_prepared_action = False

        return action_parameters, prepared_action_def, is_prepared_action

    def create_source_context(
        self, state: dict[str, object], execution_id: str
    ) -> dict[str, object]:
        """
        Create source context for tracking and routing.

        Args:
            state: Current execution state
            execution_id: Unique execution identifier

        Returns:
            Source context dictionary for action tracking
        """
        return {
            "plugin_level": getattr(state, "current_plugin", "unknown"),
            "request_level": execution_id,
            "action_level": "execute_action",
            "chain_depth": getattr(state, "action_chain_depth", 1),
            "trigger_type": getattr(state, "trigger_type", "direct_execution"),
            "session_id": getattr(state, "session_id", None),
            "parent_action_id": getattr(state, "parent_action_id", None),
        }
