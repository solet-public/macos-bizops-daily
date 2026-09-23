"""
Action Process Key Service

Responsibility: Handle all process key resolution and parsing operations for ActionExecutionEngine
Dependencies: ProviderType, ErrorCode, FrameworkError, logging
Complexity: Medium - focused on process key resolution with comprehensive error handling

Extracted from ActionExecutionEngine god class (1 method with B(7) complexity)
"""

import logging
from datetime import datetime

from ananta.constants import ProviderType
from ananta.core.plugins.plugin_contracts import ErrorCode
from ananta.error_handling import FrameworkError

logger = logging.getLogger(__name__)


class ActionProcessKeyService:
    """
    Service for managing process key resolution and parsing operations.

    ARCHITECTURAL ROLE: Supporting service that extracts process key logic
    from ActionExecutionEngine while maintaining execution engine integrity.

    This service handles:
    - Resolving process keys from various sources (provided, prepared action, fallback)
    - Parsing process keys into provider type, provider, and function name components
    - Comprehensive error handling and validation
    - Provider type validation and normalization
    """

    def __init__(self) -> None:
        """Initialize ActionProcessKeyService."""

    async def resolve_process_key_with_error_handling(
        self,
        action_name: str,
        _action_parameters: dict[str, object],  # Reserved for interface compatibility
        _action_def_or_parameters: dict[str, object],  # Reserved for interface compatibility
        prepared_action_def: dict[str, object] | None,
        process_key: str | None,
        _state: dict[str, object],  # Reserved for interface compatibility
        _execution_id: str,  # Reserved for interface compatibility
        _start_time: datetime,  # Reserved for interface compatibility
    ) -> str:
        """
        Resolve process key with comprehensive error handling.

        EXTRACTED FROM: ActionExecutionEngine._resolve_process_key_with_error_handling() - B(7) complexity

        Args:
            action_name: Name of the action being executed
            action_parameters: Parameters provided for the action
            action_def_or_parameters: Action definition or parameters
            prepared_action_def: Pre-prepared action definition if available
            process_key: Explicitly provided process key
            state: Current state context
            execution_id: Unique execution identifier
            start_time: When execution started

        Returns:
            Resolved process key string

        Raises:
            FrameworkError: If process key cannot be resolved from any source
        """
        if process_key:
            return process_key

        if prepared_action_def and "process" in prepared_action_def:
            # Extract from prepared action definition
            process = prepared_action_def["process"]
            if not isinstance(process, dict):
                logger.error(
                    f"Invalid process value in prepared action definition for '{action_name}': expected dict, got {type(process).__name__}"
                )
            else:
                provider_type = process.get("provider_type", "plugin")
                provider = process.get("provider") or process.get("plugin")
                function_name = process.get("function_name")

                if provider and function_name:
                    resolved_key = f"{provider_type}::{provider}::{function_name}"
                    return resolved_key

        # Use process key resolver as fallback
        raise FrameworkError(
            message=f"Unable to resolve process key for action '{action_name}' - no process information available",
            error_code=ErrorCode.ACTION_MISSING_PLUGIN,
            details={"action_name": action_name},
        )

    def parse_process_key(self, process_key: str, action_name: str) -> tuple[str, str, str]:
        """
        Parse process key into components with proper validation.

        EXTRACTED FROM: ActionExecutionEngine._parse_process_key() - A complexity

        Args:
            process_key: Process key string to parse (format: "provider_type::provider::function_name")
            action_name: Name of the action for error context

        Returns:
            Tuple of (provider_type, provider, function_name)

        Raises:
            FrameworkError: If process key format is invalid
        """
        try:
            parts = process_key.split("::")
            if len(parts) != 3:
                raise ValueError(f"Invalid process key format: {process_key}")

            provider_type, provider, function_name = parts

            # Validate provider_type
            if provider_type not in [
                ProviderType.PLUGIN.value,
                ProviderType.SERVICE_INTERFACE.value,
            ]:
                raise ValueError(
                    f"Unknown provider_type '{provider_type}' in process key "
                    f"'{process_key}' for action '{action_name}'. "
                    f"Must be '{ProviderType.PLUGIN.value}' or "
                    f"'{ProviderType.SERVICE_INTERFACE.value}'."
                )

            return provider_type, provider, function_name

        except Exception as e:
            logger.error(
                f"Error parsing process key '{process_key}' for action '{action_name}': {e}"
            )
            raise FrameworkError(
                message=f"Invalid process key format for action '{action_name}': {process_key}",
                error_code=ErrorCode.ACTION_MISSING_PLUGIN,
                details={"action_name": action_name, "process_key": process_key},
            ) from e
