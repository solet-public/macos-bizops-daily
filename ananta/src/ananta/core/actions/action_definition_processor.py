"""
Action Definition Processing Service

Responsibility: Handle action definition preparation, validation and processing logic
Dependencies: ValidationService, ParameterProcessor, VariableResolver
Complexity: Medium - focused action definition coordination

Extracted from ActionManager god class during Phase 4 decomposition
"""

import logging
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Protocol

from ananta.core.plugins.plugin_contracts import ActionStatus
from ananta.error_handling import FrameworkError

logger = logging.getLogger(__name__)


class ValidationServiceProtocol(Protocol):
    """Protocol for ValidationService dependency."""

    async def validate_action_completeness(
        self,
        action_def: dict[str, object],
        action_name: str,
        is_prepared_action: bool,
        is_runtime_generated: bool,
        execution_id: str,
        start_time: datetime,
    ) -> None: ...


class ParameterProcessorProtocol(Protocol):
    """Protocol for ParameterProcessor dependency."""

    pass


class VariableResolverProtocol(Protocol):
    """Protocol for VariableResolver dependency."""

    pass


class ActionDefinitionProcessor:
    """
    Service for processing and preparing action definitions.

    ARCHITECTURAL ROLE: Supporting service that extracts action definition processing logic
    from ActionManager to create cleaner separation of concerns.

    This service handles:
    - Action definition preparation and validation
    - Process information extraction and validation
    - Legacy action definition handling
    - Parameter merging and validation
    """

    def __init__(
        self,
        validation_service: ValidationServiceProtocol,
        parameter_processor: ParameterProcessorProtocol,
        variable_resolver: VariableResolverProtocol | None = None,
    ) -> None:
        """Initialize ActionDefinitionProcessor with service dependencies."""
        self.validation_service: ValidationServiceProtocol = validation_service
        self.parameter_processor: ParameterProcessorProtocol = parameter_processor
        self.variable_resolver: VariableResolverProtocol | None = variable_resolver

    async def prepare_action_definition(
        self,
        action_name: str,
        action_def_or_parameters: dict[str, object],
        prepared_action_def: dict[str, object] | None,
        is_prepared_action: bool,
        action_parameters: dict[str, object],
        state: dict[str, object],
        process_key: str,
        execution_id: str,
        start_time: datetime,
        legacy_definition_getter: Callable[
            [str, dict[str, object], dict[str, object], str], dict[str, object]
        ],
    ) -> tuple[dict[str, object], dict[str, object]]:
        """Prepare action definition and extract merged parameters.

        Args:
            action_name: Name of the action
            action_def_or_parameters: Raw action definition or parameters
            prepared_action_def: Pre-processed action definition
            is_prepared_action: Whether action is already prepared
            action_parameters: Extracted action parameters
            state: Current execution state
            process_key: Process key for action lookup
            execution_id: Unique execution identifier
            start_time: Action start time
            legacy_definition_getter: Function to get legacy definitions

        Returns:
            tuple: (action_definition, merged_parameters)
        """
        is_runtime_generated = bool(
            action_def_or_parameters.get("_runtime_generated")
            or action_def_or_parameters.get("_is_result_processor")
            or (prepared_action_def and prepared_action_def.get("process_key"))
        )

        # Get action definition
        if is_prepared_action or is_runtime_generated:
            action_def = prepared_action_def or action_def_or_parameters
        else:
            action_def = legacy_definition_getter(
                action_name, action_parameters, state, process_key
            )

        # Validate action completeness
        await self.validation_service.validate_action_completeness(
            action_def,
            action_name,
            is_prepared_action,
            is_runtime_generated,
            execution_id,
            start_time,
        )

        # Prepare resolved action
        resolved_action = action_def.copy()
        resolved_action["action_status"] = ActionStatus.PROCESSING.value
        resolved_action["timestamp"] = datetime.now(UTC).isoformat()

        # Extract merged parameters
        merged_params = self._extract_merged_parameters(resolved_action)
        logger.debug("✅ EXECUTION_PATH: Action validated and ready for execution")

        return action_def, merged_params

    def extract_and_validate_process_info(
        self,
        action_def: dict[str, object],
        action_name: str,
        process_external_id_getter: Callable[[str], str | None],
    ) -> dict[str, object]:
        """Extract and validate process information from action definition.

        Args:
            action_def: Action definition to process
            action_name: Action name for error context
            process_external_id_getter: Function to get process external ID

        Returns:
            dict: Validation result with process information

        Raises:
            FrameworkError: If deprecated fields are used
        """
        process = self._extract_process_dict(action_def, action_name)
        if process is None:
            return {"valid": False}

        provider_type, provider, function_name = self._extract_process_fields(process)
        self._validate_no_deprecated_fields(process, function_name, action_name)

        if not provider or not function_name:
            logger.error(f"Action '{action_name}' missing required provider or function")
            return {"valid": False}

        return self._lookup_process_external_id(
            provider_type, provider, function_name, action_name, process_external_id_getter
        )

    def _extract_process_dict(
        self, action_def: dict[str, object], action_name: str
    ) -> dict[str, object] | None:
        """Extract and validate process dict from action definition."""
        process_raw = action_def.get("process", {})
        if not isinstance(process_raw, dict):
            logger.error(f"Action '{action_name}' has invalid process field (not a dict)")
            return None
        return process_raw

    def _extract_process_fields(
        self, process: dict[str, object]
    ) -> tuple[str, str | None, str | None]:
        """Extract provider_type, provider, and function_name from process dict."""
        provider_type_raw = process.get("provider_type", "plugin")
        provider_type = str(provider_type_raw) if provider_type_raw is not None else "plugin"

        provider_raw = process.get("provider") or process.get("plugin")
        provider = str(provider_raw) if provider_raw is not None else None

        function_name_raw = process.get("function_name")
        function_name = str(function_name_raw) if function_name_raw is not None else None

        return provider_type, provider, function_name

    def _validate_no_deprecated_fields(
        self, process: dict[str, object], function_name: str | None, action_name: str
    ) -> None:
        """Validate that deprecated 'function' field is not used."""
        if not function_name and process.get("function"):
            raise FrameworkError(
                message=f"Action definition for '{action_name}' uses deprecated \"function\" field. Use 'function_name' instead.",
                error_code="action_manager.deprecated_function_field",
                details={"action_name": action_name, "process": process},
            )

    def _lookup_process_external_id(
        self,
        provider_type: str,
        provider: str,
        function_name: str,
        action_name: str,
        process_external_id_getter: Callable[[str], str | None],
    ) -> dict[str, object]:
        """Look up process external ID from registry."""
        process_key = f"{provider_type}::{provider}::{function_name}"
        process_external_id = process_external_id_getter(process_key)

        if not process_external_id:
            logger.error(
                f"Process '{process_key}' not found in process registry for action '{action_name}'"
            )
            return {"valid": False}

        return {"valid": True, "process_external_id": process_external_id}

    def _extract_merged_parameters(self, resolved_action: dict[str, object]) -> dict[str, object]:
        """Extract merged parameters from resolved action, handling double-nested structure.

        Args:
            resolved_action: Resolved action definition

        Returns:
            dict: Merged parameters for execution
        """
        arguments_raw = resolved_action.get("arguments", {})

        # Type narrow to dict
        if not isinstance(arguments_raw, dict):
            logger.error(
                f"Arguments field is not a dict, using empty dict. Type: {type(arguments_raw)}"
            )
            return {}

        arguments: dict[str, object] = arguments_raw

        # Check for double-nested structure
        if "arguments" in arguments:
            nested_args = arguments["arguments"]
            if isinstance(nested_args, dict):
                # Extract from double-nested: arguments.arguments
                merged_params: dict[str, object] = nested_args
            else:
                # Invalid nested structure, use outer arguments
                merged_params = arguments
        else:
            # Use normal structure: arguments
            merged_params = arguments

        return merged_params
