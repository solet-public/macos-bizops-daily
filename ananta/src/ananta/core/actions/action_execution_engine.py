"""
Action Execution Engine

Responsibility: Handle all action execution logic for ActionManager
Dependencies: ValidationService, ProviderManager, ProcessKeyResolver, ActionValidator
Complexity: High - focused on action execution pipeline and performance tracking

Extracted from ActionManager god class (25 methods)
"""

import inspect
import logging
import uuid
from datetime import UTC, datetime
from typing import Protocol

from ananta.constants import (
    FRAMEWORK_ACTION_EXECUTIONS_TABLE,
    FRAMEWORK_NAMESPACE,
    ProviderType,
)
from ananta.core.actions.action_definition_service import ActionDefinitionService
from ananta.core.actions.action_process_key_service import ActionProcessKeyService
from ananta.core.actions.action_stats_service import ActionStatsService
from ananta.core.actions.action_validator import ActionValidator
from ananta.core.domain.protocols import PluginInterface
from ananta.core.domain.status import is_status_match
from ananta.core.plugins.plugin_contracts import ActionStatus, ErrorCode
from ananta.core.process_registry.key_resolver import ProcessKeyResolver
from ananta.core.providers.provider_manager import ProviderManager
from ananta.core.validation.validation_service import ValidationService
from ananta.error_handling import FrameworkError
from ananta.interfaces.state_service_protocol import StateServiceProtocol

logger = logging.getLogger(__name__)


class ServiceFunctionProtocol(Protocol):
    """Protocol for service function signatures."""

    def __call__(self, **kwargs: object) -> object:
        """Service function callable signature."""
        ...


class ActionExecutionEngine:
    """
    Service for handling all action execution operations.

    Design Principles:
    - Single Responsibility: Action execution pipeline only
    - Performance Tracking: Comprehensive execution metrics and tracking
    - Provider Routing: Support for both plugin and service interface execution
    - Error Handling: Robust error handling with detailed context
    """

    def __init__(
        self,
        state_service: StateServiceProtocol | None,
        validation_service: ValidationService,
        provider_manager: ProviderManager,
        process_key_resolver: ProcessKeyResolver,
        validator: ActionValidator,
        action_event_bus: object | None = None,
        template_engine: object | None = None,
    ) -> None:
        """Initialize ActionExecutionEngine with required dependencies."""
        self.state_service = state_service
        self.validation_service = validation_service
        self.provider_manager = provider_manager
        self.process_key_resolver = process_key_resolver
        self.validator = validator
        self.action_event_bus = action_event_bus
        self.template_engine = template_engine

        # EXTRACTED: Action statistics and performance tracking service
        self.stats_service = ActionStatsService(state_service)

        # EXTRACTED: Action definition retrieval and management service
        self.definition_service = ActionDefinitionService(state_service)

        # EXTRACTED: Process key resolution and parsing service
        self.process_key_service = ActionProcessKeyService()

    async def execute_action(
        self,
        action_name: str,
        state: dict[str, object],
        action_def_or_parameters: dict[str, object],
        plugin_override: str | None = None,
        process_key: str | None = None,
    ) -> dict[str, object]:
        """Execute an action with comprehensive error handling and tracking.

        REFACTORED: Extracted helper methods to reduce complexity from D(26).
        """
        execution_id = str(uuid.uuid4())[:8]
        start_time = datetime.now(UTC)

        action_parameters, prepared_action_def, is_prepared_action = self._parse_action_parameters(
            action_name, action_def_or_parameters
        )

        source_context = self._create_source_context(state, execution_id)
        await self.stats_service.track_action_execution_start(
            execution_id, action_name, action_parameters, start_time, source_context
        )

        try:
            # Resolve process_key using helper method
            process_key = await self.process_key_service.resolve_process_key_with_error_handling(
                action_name,
                action_parameters,
                action_def_or_parameters,
                prepared_action_def,
                process_key,
                state,
                execution_id,
                start_time,
            )

            # Get and prepare action definition
            action_def, merged_params = await self._prepare_action_definition(
                action_name,
                action_def_or_parameters,
                prepared_action_def,
                is_prepared_action,
                action_parameters,
                state,
                process_key,
                execution_id,
                start_time,
            )

            # Validate the prepared action
            await self.validation_service.validate_prepared_action(
                action_name, action_def, {}, None, None
            )

            # Execute the action based on provider type
            result = await self._execute_action_by_provider(
                action_name, process_key, merged_params, state, plugin_override, execution_id
            )

            # Track successful execution
            await self.stats_service.track_action_execution_end(
                execution_id, ActionStatus.COMPLETED, result, None, start_time
            )

            return result

        except Exception as e:
            # Track failed execution
            await self.stats_service.track_action_execution_end(
                execution_id, ActionStatus.ERROR, {}, str(e), start_time
            )
            raise

    def execute_plugin(
        self,
        plugin: PluginInterface,
        action_name: str,
        action_object: dict[str, object],
        function_name: str = "execute_action",
    ) -> dict[str, object]:
        """Execute an action using a plugin with validation and error handling.

        This method provides the core execution logic for plugin-based actions,
        including plugin validation, function resolution, and result validation.
        """
        self.validation_service.validate_plugin_execution_prerequisites(plugin, action_name)

        plugin_function = self.validation_service.validate_and_get_plugin_function(
            plugin, function_name, action_name
        )

        return self.validation_service.execute_and_validate_plugin_result(
            plugin_function, action_name, action_object
        )

    def _parse_action_parameters(
        self, action_name: str, action_def_or_parameters: dict[str, object]
    ) -> tuple[dict[str, object], dict[str, object] | None, bool]:
        """Parse action parameters and determine action definition format.

        Returns:
            Tuple of (action_parameters, prepared_action_def, is_prepared_action)
        """
        if "arguments" in action_def_or_parameters:
            # This is a prepared action definition (has both name, arguments, process)
            arguments = action_def_or_parameters.get("arguments", {})
            if not isinstance(arguments, dict):
                arguments = {}
            action_parameters = arguments
            prepared_action_def = action_def_or_parameters
            is_prepared_action = True
        else:
            # This is raw action parameters
            action_parameters = action_def_or_parameters
            prepared_action_def = None
            is_prepared_action = False

        return action_parameters, prepared_action_def, is_prepared_action

    def _create_source_context(
        self, state: dict[str, object], execution_id: str
    ) -> dict[str, object]:
        """Create source context for action tracking."""
        return {
            "execution_id": execution_id,
            "state_keys": list(state.keys()) if state else [],
            "timestamp": datetime.now(UTC).isoformat(),
        }

    async def _update_execution_provider(
        self, execution_id: str, provider_type: str, provider: str
    ) -> None:
        """Update execution record with provider information."""
        try:
            if not self.state_service:
                return

            update_data: dict[str, object] = {"provider_type": provider_type, "provider": provider}

            result = self.state_service.write_state(
                namespace=FRAMEWORK_NAMESPACE,
                data={
                    "table": FRAMEWORK_ACTION_EXECUTIONS_TABLE,
                    "record": update_data,
                    "filters": {"execution_id": execution_id},
                    "operation": "update",
                },
            )

            if not is_status_match(result.get("action_status"), ActionStatus.COMPLETED):
                logger.error(f"Failed to update execution provider for {execution_id}: {result}")

        except Exception as e:
            logger.error(f"Error updating execution provider for {execution_id}: {e}")

    async def get_action_performance_stats(
        self, action_name: str | None = None
    ) -> dict[str, object]:
        """
        Get performance statistics for actions.

        DELEGATED TO: ActionStatsService.get_action_performance_stats() - B(7) complexity
        """
        return await self.stats_service.get_action_performance_stats(action_name)

    async def _prepare_action_definition(
        self,
        action_name: str,
        _action_def_or_parameters: dict[
            str, object
        ],  # Contains top-level context (session_id, flow_id)
        prepared_action_def: dict[str, object] | None,
        is_prepared_action: bool,
        action_parameters: dict[str, object],
        _state: dict[str, object],  # Reserved for interface compatibility
        process_key: str,
        execution_id: str,
        start_time: datetime,
    ) -> tuple[dict[str, object], dict[str, object]]:
        """Prepare action definition for execution."""
        if is_prepared_action and prepared_action_def:
            # Use the already prepared definition
            action_def = prepared_action_def
            arguments = action_def.get("arguments", {})
            if not isinstance(arguments, dict):
                arguments = {}
            merged_params = dict(arguments)  # Create copy to avoid mutating original

            # Validate that the action is complete before execution
            await self.validation_service.validate_action_completeness(
                action_def, action_name, is_prepared_action, False, execution_id, start_time
            )
        else:
            # Get action definition and merge parameters
            action_def = await self._get_action_definition_for_execution(action_name, process_key)
            merged_params = self.definition_service.extract_merged_parameters(
                action_def, action_parameters
            )

            # Create complete action definition for execution
            action_def = {
                "name": action_name,
                "arguments": merged_params,
                "process": action_def.get("process", {}),
                "description": action_def.get("description", ""),
            }

        # Merge top-level context (session_id, flow_id) into params
        # Context is set at top level by AtCommandProcessor, merged here for plugins
        merged_params = self._merge_context_into_params(merged_params, _action_def_or_parameters)

        return action_def, merged_params

    def _merge_context_into_params(
        self,
        params: dict[str, object],
        action_def: dict[str, object],
    ) -> dict[str, object]:
        """Merge top-level context fields into params for plugin execution.

        Context fields (session_id, flow_id) are stored at the top level of action
        definitions (single source of truth). This method merges them into params
        so plugins can access them via params.get("session_id"), etc.
        """
        # Context fields to merge from top-level to params
        context_fields = ("session_id", "flow_id")

        for field in context_fields:
            if field in action_def and field not in params:
                params[field] = action_def[field]

        return params

    async def _get_action_definition_for_execution(
        self, action_name: str, process_key: str
    ) -> dict[str, object]:
        """Get action definition for execution, with fallback to legacy format."""
        # Try to get from registry first
        action_def = await self.definition_service.get_action_definition_from_registry(action_name)
        if action_def:
            return action_def

        # Fallback to legacy action definition format
        return self.definition_service.get_legacy_action_definition(
            action_name, process_key, self.process_key_service.parse_process_key
        )

    async def _execute_action_by_provider(
        self,
        action_name: str,
        process_key: str,
        merged_params: dict[str, object],
        state: dict[str, object],
        plugin_override: str | None,
        execution_id: str,
    ) -> dict[str, object]:
        """Execute action based on provider type."""
        provider_type, provider, function_name = self.process_key_service.parse_process_key(
            process_key, action_name
        )

        # Update execution tracking with provider info
        await self._update_execution_provider(execution_id, provider_type, provider)

        # Validate execution parameters
        process: dict[str, object] = {
            "provider_type": provider_type,
            "provider": provider,
            "function_name": function_name,
        }
        self.validation_service.validate_action_execution_parameters(
            action_name, provider_type, provider, function_name, process
        )

        if provider_type == ProviderType.SERVICE_INTERFACE.value:
            return await self._execute_service_interface_action(
                action_name, provider, function_name, merged_params, state
            )
        else:  # plugin
            return await self._execute_plugin_action(
                action_name, provider, function_name, merged_params, state, plugin_override
            )

    async def _execute_service_interface_action(
        self,
        action_name: str,
        service_interface: str,
        _function_name: str,  # Reserved for interface compatibility
        action_parameters: dict[str, object],
        _state: dict[str, object],  # Reserved for interface compatibility
    ) -> dict[str, object]:
        """Execute action using service interface."""
        service = self.provider_manager.get_service_instance(
            service_interface, action_name, _function_name
        )
        if not service:
            raise FrameworkError(
                message=f"Service interface '{service_interface}' not found for action '{action_name}'",
                error_code=ErrorCode.ACTION_MISSING_PLUGIN,
                details={"action_name": action_name, "service_interface": service_interface},
            )

        service_function_obj = self.validation_service.validate_service_function(
            service, _function_name, action_name
        )
        if not callable(service_function_obj):
            raise FrameworkError(
                message=f"Service function '{_function_name}' is not callable",
                error_code=ErrorCode.ACTION_EXECUTION_FAILED,
                details={
                    "action_name": action_name,
                    "service_interface": service_interface,
                    "function_name": _function_name,
                },
            )

        # Prepare parameters for service call
        prepared_params = self._prepare_action_execution(action_parameters, _state)

        # Execute service function - pass the callable directly with service_interface for parameter filtering
        result = await self._execute_service_function(
            service, service_function_obj, _function_name, prepared_params, service_interface
        )

        # Format service result
        return self._format_service_result(result, datetime.now(UTC).isoformat())

    async def _execute_plugin_action(
        self,
        action_name: str,
        plugin_name: str,
        _function_name: str,  # Reserved for interface compatibility
        action_parameters: dict[str, object],
        _state: dict[str, object],  # Reserved for interface compatibility
        plugin_override: str | None,
    ) -> dict[str, object]:
        """Execute action using plugin."""
        # Apply plugin override if provided
        effective_plugin_name = self._apply_plugin_override(
            plugin_name, plugin_override, action_name
        )

        plugin_base = self.provider_manager.plugin_manager.get_plugin(effective_plugin_name)
        if not plugin_base:
            raise FrameworkError(
                message=f"Plugin '{effective_plugin_name}' not found for action '{action_name}'",
                error_code=ErrorCode.PLUGIN_NOT_FOUND,
                details={"action_name": action_name, "plugin_name": effective_plugin_name},
            )

        # PluginBase uses a different interface (execute method), not PluginInterface
        # All plugins in the system should be PluginBase instances
        # Prepare parameters with state (same as service interface execution)
        prepared_params = self._prepare_action_execution(action_parameters, _state)
        # Use PluginBase.execute which returns dict[str, object]
        result = await plugin_base.execute(action_name, prepared_params)
        return result

    def _apply_plugin_override(
        self, original_plugin: str, plugin_override: str | None, action_name: str
    ) -> str:
        """Apply plugin override if provided and valid."""
        if not plugin_override:
            return original_plugin

        # Validate that override plugin exists
        if self.provider_manager.plugin_manager.get_plugin(plugin_override):
            return plugin_override
        else:
            logger.error(
                f"Plugin override '{plugin_override}' not found for action '{action_name}', using original '{original_plugin}'"
            )
            return original_plugin

    def _prepare_action_execution(
        self, action_parameters: dict[str, object], state: dict[str, object]
    ) -> dict[str, object]:
        """Prepare parameters for action execution."""
        return {**action_parameters, "state": state}

    def _validate_action_parameters(
        self, action_name: str, action_parameters: dict[str, object], action_def: dict[str, object]
    ) -> None:
        """Validate action parameters against action definition."""
        self.validation_service.validate_action_parameters(
            action_name, action_parameters, action_def
        )

    async def execute_service(
        self, service_interface: str, function_name: str, parameters: dict[str, object]
    ) -> dict[str, object]:
        """Execute a service interface function directly."""
        service = self.provider_manager.get_service_instance(
            service_interface, f"{service_interface}.{function_name}", function_name
        )
        if not service:
            raise FrameworkError(
                message=f"Service interface '{service_interface}' not found",
                error_code=ErrorCode.ACTION_MISSING_PLUGIN,
                details={"service_interface": service_interface, "function_name": function_name},
            )

        service_function_obj = self.validation_service.validate_service_function(
            service, function_name, f"{service_interface}.{function_name}"
        )
        if not callable(service_function_obj):
            raise FrameworkError(
                message=f"Service function '{function_name}' is not callable",
                error_code=ErrorCode.ACTION_EXECUTION_FAILED,
                details={"service_interface": service_interface, "function_name": function_name},
            )

        result = await self._execute_service_function(
            service, service_function_obj, function_name, parameters, service_interface
        )
        return self._format_service_result(result, datetime.now(UTC).isoformat())

    async def _execute_service_function(
        self,
        service: object,
        function: object,
        function_name: str,
        params: dict[str, object],
        _service_interface: str,  # Reserved for interface compatibility
    ) -> object:
        """Execute service function with proper error handling and parameter filtering."""
        try:
            # Use params directly - filtering is done elsewhere in the pipeline
            # The process registry is built from decorators and parameters are validated there
            filtered_params = params

            # Verify function is callable
            if not callable(function):
                raise FrameworkError(
                    message=f"Service function '{function_name}' is not callable",
                    error_code=ErrorCode.ACTION_EXECUTION_FAILED,
                    details={
                        "function_name": function_name,
                        "function_type": type(function).__name__,
                    },
                )

            if function_name == "store_vectors":
                logger.error(
                    "SERVICE_PARAM_DEBUG: store_vectors params keys=%s payload=%s",
                    list(filtered_params.keys()),
                    filtered_params,
                )

            # Handle both sync and async functions
            if inspect.iscoroutinefunction(function):
                result = await function(**filtered_params)
                return result
            else:
                result = function(**filtered_params)
                return result

        except Exception as e:
            service_class = service.__class__.__name__
            logger.error(f"Error executing service function '{service_class}.{function_name}': {e}")
            raise FrameworkError(
                message=f"Error executing service function '{service_class}.{function_name}': {e}",
                error_code=ErrorCode.ACTION_EXECUTION_FAILED,
                details={
                    "service_class": service_class,
                    "function_name": function_name,
                    "error": str(e),
                },
            ) from e

    def _format_service_result(self, result: object, timestamp: str) -> dict[str, object]:
        """Format service execution result into standard format."""
        if isinstance(result, dict) and "action_status" in result:
            # Result is already in action result format
            return result

        return {
            "action_status": ActionStatus.COMPLETED.value,
            "data": result if result is not None else {},
            "actions": [],
            "error": None,
            "timestamp": timestamp,
            "provider_type": "service_interface",
        }
