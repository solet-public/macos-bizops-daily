import json
import logging
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol, TypedDict

from ananta.constants import (
    FRAMEWORK_NAMESPACE,
)
from ananta.core.actions.action_definition_processor import ActionDefinitionProcessor
from ananta.core.actions.action_execution_engine import ActionExecutionEngine
from ananta.core.actions.action_validation_manager import ActionValidationManager
from ananta.core.actions.action_validator import ActionValidator, ValidationResult
from ananta.core.config.config_manager import get_config
from ananta.core.domain.enums import ErrorSeverity
from ananta.core.domain.protocols import PluginInterface
from ananta.core.domain.status import is_status_match
from ananta.core.orchestration.feature_flags import OrchestrationFeatureFlags
from ananta.core.plugins.plugin_contracts import ActionStatus, ErrorCode
from ananta.core.process_registry.registry_manager import RegistryManager
from ananta.core.providers.provider_manager import (
    OrchestratorProtocol,
    PluginManagerProtocol,
    ProviderManager,
)
from ananta.core.templates.parameter_processor import ParameterProcessor
from ananta.core.templates.template_exceptions import (
    TemplateResolutionError,
    UnresolvedTemplateVariablesError,
)
from ananta.core.templates.variable_resolver import VariableResolver
from ananta.core.tracking.execution_tracking_manager import ExecutionTrackingManager
from ananta.core.tracking.performance_stats_manager import (
    PerformanceStatsManager,
)
from ananta.core.tracking.result_processing_manager import (
    FlowBasedIOProcessKeyResolver,
    ResultProcessingManager,
)
from ananta.core.validation.validation import validate_action
from ananta.core.validation.validation_coordinator import ValidationCoordinator
from ananta.core.validation.validation_service import ValidationService
from ananta.error_handling import AnantaError, FrameworkError, PluginError
from ananta.interfaces.state_service_protocol import StateServiceProtocol
from ananta.services.action_event_bus import ActionRequestEvent, create_action_correction_event

# TemplateEngine imported conditionally in __init__ based on feature flags

logger = logging.getLogger(__name__)


# TypedDict definitions for complex data structures
class ActionDefinition(TypedDict, total=False):
    name: str
    description: str
    process: dict[str, str]
    parameters: dict[str, object]
    properties: dict[str, object]
    enabled: bool
    version: str


class ProcessInfo(TypedDict):
    plugin: str
    function: str
    external_id: str


class ActionRecord(TypedDict, total=False):
    action_name: str
    action_status: str
    timestamp: str
    execution_id: str
    process_key: str
    parameters: dict[str, object]
    result: dict[str, object]
    error: dict[str, object]
    performance_stats: dict[str, object]


class ExecutionContext(TypedDict, total=False):
    action_name: str
    execution_id: str
    timestamp: str
    source: dict[str, object]
    state: dict[str, object]
    parameters: dict[str, object]


class PerformanceStats(TypedDict):
    execution_time_ms: float
    memory_usage_mb: float
    plugin_load_time_ms: float
    validation_time_ms: float


class PluginExecutionResult(TypedDict, total=False):
    action_status: str
    result: dict[str, object]
    error: dict[str, object]
    timestamp: str
    execution_time_ms: float


class ActionValidationContext(TypedDict):
    action_name: str
    action_definition: ActionDefinition
    parameters: dict[str, object]
    validation_errors: list[str]


# Protocol interfaces for service dependencies (only non-imported ones)
class StateManagerProtocol(Protocol):
    def get_state(self) -> dict[str, object]: ...
    def update_state(self, updates: dict[str, object]) -> None: ...
    async def save(self, state: dict[str, object]) -> None: ...


class TemplateEnginePluginManagerProtocol(Protocol):
    """Extended protocol for PluginManager that satisfies both provider_manager and template_functions requirements."""

    plugins: dict[str, object]

    def get_plugin(self, plugin_name: str) -> object: ...


class AsyncJobManagerProtocol(Protocol):
    def create_job(
        self,
        plugin_name: str,
        action_name: str,
        request_data: dict[str, object] | None = None,
        job_id: str | None = None,
        name: str | None = None,
        description: str | None = None,
    ) -> dict[str, object]: ...
    def get_job(self, job_id: str) -> dict[str, object]: ...


class EventBusProtocol(Protocol):
    def publish(self, event: object) -> bool: ...
    def subscribe(
        self, event_type: str, handler: Callable[[dict[str, object]], object]
    ) -> None: ...


class UnifiedMetadataRegistryProtocol(Protocol):
    def get_metadata(self, key: str) -> dict[str, object] | None: ...
    def set_metadata(self, key: str, value: dict[str, object]) -> None: ...


class DiscoveryServiceProtocol(Protocol):
    def query_process_registry(
        self,
        query: str,
        max_results: int = 10,
        state: dict[str, object] | None = None,
    ) -> dict[str, object]: ...

    def execute_embeddings_search(
        self,
        query: str,
        original_input: str,
        max_results: int = 10,
    ) -> dict[str, object]: ...


class ActionManager:
    discovery_service: DiscoveryServiceProtocol | None = (
        None  # Optional discovery service for process lookup
    )

    def __init__(
        self,
        APP_HOME: str,
        plugin_manager: PluginManagerProtocol,
        state_manager: StateManagerProtocol,
        state_service: StateServiceProtocol | None = None,
        async_job_manager: AsyncJobManagerProtocol | None = None,
        event_bus: EventBusProtocol | None = None,
        orchestrator: OrchestratorProtocol | None = None,
        unified_metadata_registry: UnifiedMetadataRegistryProtocol | None = None,
        discovery_service: DiscoveryServiceProtocol | None = None,
        memory_service: object | None = None,
        knowledge_service: object | None = None,
    ) -> None:
        self._initialize_core_dependencies(
            APP_HOME,
            plugin_manager,
            state_manager,
            state_service,
            async_job_manager,
            event_bus,
            orchestrator,
            discovery_service,
            memory_service,
            knowledge_service,
        )
        self._initialize_service_components(state_service, state_manager)
        self._initialize_execution_engine(state_service, event_bus)
        self._initialize_template_engine(unified_metadata_registry, state_service, plugin_manager)

    def _initialize_core_dependencies(
        self,
        APP_HOME: str,
        plugin_manager: PluginManagerProtocol,
        state_manager: StateManagerProtocol,
        state_service: StateServiceProtocol | None,
        async_job_manager: AsyncJobManagerProtocol | None,
        event_bus: EventBusProtocol | None,
        orchestrator: OrchestratorProtocol | None,
        discovery_service: DiscoveryServiceProtocol | None,
        memory_service: object | None,
        knowledge_service: object | None,
    ) -> None:
        """Initialize core dependencies."""
        self.APP_HOME = Path(APP_HOME)
        self.plugin_manager = plugin_manager
        self.state_manager = state_manager
        self.state_service = state_service
        self.async_job_manager = async_job_manager
        self.event_bus = event_bus
        self.orchestrator = orchestrator  # Reference for service lifecycle management
        self.discovery_service = discovery_service  # Optional discovery service for process lookup
        self.memory_service = memory_service  # Memory service for template function execution
        self.knowledge_service = knowledge_service  # Knowledge service for template function execution

    def _initialize_service_components(
        self, state_service: StateServiceProtocol | None, state_manager: StateManagerProtocol
    ) -> None:
        """Initialize validation, tracking, and processing service components."""
        # Validate state_service before passing to ActionValidator
        # ActionValidator expects StateService | None but we have StateServiceProtocol | None
        # Since StateServiceProtocol is a duck-typed interface, we can safely pass it
        self.validator = ActionValidator(state_service)  # type: ignore[arg-type]
        self.validation_service = ValidationService(
            validator=self.validator, state_service=state_service
        )

        # EXTRACTED: Registry management service for all registry operations
        # Type narrowing: registry_manager_state_service is compatible with RegistryManager's Protocol
        registry_manager_state_service: object = state_service
        self.registry_manager = RegistryManager(
            state_service=registry_manager_state_service,  # type: ignore[arg-type]
            validation_service=self.validation_service,
            discovery_service=self.discovery_service,
        )

        # ProviderManager will get all services dynamically from orchestrator
        self.provider_manager = ProviderManager(
            plugin_manager=self.plugin_manager,
            state_service=state_service,
            orchestrator=self.orchestrator,
            discovery_service=self.discovery_service,
            action_definition_getter=self._get_action_definition_for_process_key_resolution,
            async_job_manager=self.async_job_manager,
        )

        # EXTRACTED: Validation coordination service for plugin and service validation
        self.validation_coordinator = ValidationCoordinator(
            validation_service=self.validation_service
        )

        # EXTRACTED: Execution tracking and performance monitoring service
        self.execution_tracking_manager = ExecutionTrackingManager(state_service=state_service)

        # EXTRACTED: Performance statistics computation and analysis service
        self.performance_stats_manager = PerformanceStatsManager(state_service=state_service)

        # EXTRACTED: Action validation service
        self.action_validation_manager = ActionValidationManager(
            execution_tracking_manager=self.execution_tracking_manager
        )

        # EXTRACTED: Result processing and formatting service
        # Build IO process key resolver from orchestrator (for no_matches shortcut)
        io_resolver = (
            FlowBasedIOProcessKeyResolver(self.orchestrator)
            if self.orchestrator
            else None
        )
        self.result_processing_manager = ResultProcessingManager(
            template_engine=None,
            state_manager=state_manager,  # Will be set after template engine initialization
            io_process_key_resolver=io_resolver,
        )

        self.variable_resolver = VariableResolver(
            app_home=self.APP_HOME,
            state_service=state_service,
            validation_service=self.validation_service,
            async_job_manager=self.async_job_manager,
        )

        # EXTRACTED: Parameter processing and file reference resolution service
        self.parameter_processor = ParameterProcessor(variable_resolver=self.variable_resolver)

        # EXTRACTED: Action definition processing and preparation service
        self.action_definition_processor = ActionDefinitionProcessor(
            validation_service=self.validation_service,
            parameter_processor=self.parameter_processor,
            variable_resolver=self.variable_resolver,
        )

        # Set the process external ID getter for the registry manager
        self.registry_manager.set_process_external_id_getter(self._get_process_external_id)

    def _initialize_execution_engine(
        self, state_service: StateServiceProtocol | None, event_bus: EventBusProtocol | None
    ) -> None:
        """Initialize the action execution engine with required dependencies."""
        self.execution_engine = ActionExecutionEngine(
            state_service=state_service,
            validation_service=self.validation_service,
            provider_manager=self.provider_manager,
            process_key_resolver=self.registry_manager.process_key_resolver,
            validator=self.validator,
            action_event_bus=event_bus,
            template_engine=None,  # Will be set after template engine initialization
        )

    def _initialize_template_engine(
        self,
        unified_metadata_registry: UnifiedMetadataRegistryProtocol | None,
        state_service: StateServiceProtocol | None,
        plugin_manager: PluginManagerProtocol,
    ) -> None:
        """Initialize template engine with fail-fast validation."""
        if OrchestrationFeatureFlags.use_new_template_engine() and unified_metadata_registry:
            self._setup_new_template_engine(
                unified_metadata_registry, state_service, plugin_manager, self.knowledge_service
            )
        else:
            # FAIL-FAST: No fallback to legacy TemplateEngine
            raise RuntimeError(
                "Unified metadata registry not available and fallback to legacy TemplateEngine "
                "is prohibited. Feature flags must enable NewTemplateEngine."
            )

    def _setup_new_template_engine(
        self,
        unified_metadata_registry: UnifiedMetadataRegistryProtocol,
        state_service: StateServiceProtocol | None,
        plugin_manager: PluginManagerProtocol,
        knowledge_service: object | None = None,
    ) -> None:
        """Setup NewTemplateEngine with proper error handling."""
        try:
            from ananta.platform.new_template_engine import NewTemplateEngine

            # Type narrowing: Verify plugin_manager has plugins attribute for template engine
            template_plugin_manager: TemplateEnginePluginManagerProtocol | None = None
            if hasattr(plugin_manager, "plugins") and hasattr(plugin_manager, "get_plugin"):
                # Use isinstance-style check to narrow type
                template_plugin_manager = plugin_manager  # type: ignore[assignment]

            # UnifiedMetadataRegistryProtocol is compatible with UnifiedMetadataRegistry interface
            # Type narrowing: template_engine_state_service is compatible with NewTemplateEngine's Protocol
            template_engine_state_service: object = state_service
            self.template_engine = NewTemplateEngine(
                unified_metadata_registry,  # type: ignore[arg-type]
                state_service=template_engine_state_service,  # type: ignore[arg-type]
                action_manager=self,
                plugin_manager=template_plugin_manager,
                discovery_service=self.discovery_service,
                memory_service=self.memory_service,  # type: ignore[arg-type]
                knowledge_service=knowledge_service,
            )
            self.template_engine.initialize()
            self._using_new_template_engine = True

            # Set template engine in execution engine and result processing manager
            self.execution_engine.template_engine = self.template_engine
            self.result_processing_manager.template_engine = self.template_engine

        except Exception as e:
            # FAIL-FAST: No fallback to legacy TemplateEngine
            raise RuntimeError(
                f"NewTemplateEngine failed to initialize and fallback to legacy TemplateEngine "
                f"is prohibited. Original error: {e}"
            ) from e

    def _resolve_file_references(self, action_def: ActionDefinition) -> ActionDefinition:
        """
        Delegate file reference resolution to ParameterProcessor service.

        REFACTORED: Extracted file reference resolution logic to ParameterProcessor - maintaining API contract.
        """
        # Convert ActionDefinition to dict for processing
        action_def_dict: dict[str, object] = dict(action_def)
        result = self.parameter_processor.resolve_file_references(action_def_dict)
        # Return as ActionDefinition TypedDict (runtime compatible)
        return result  # type: ignore[return-value]

    def get_action_definition(
        self,
        action_name: str,
        state: dict[str, object] | None = None,
        process_key: str | None = None,
    ) -> ActionDefinition | None:
        """
        Delegate action definition retrieval to RegistryManager service.

        REFACTORED: Extracted registry operations to RegistryManager - maintaining API contract.
        """
        result = self.registry_manager.get_action_definition(action_name, state, process_key)
        # Registry manager returns dict[str, object] | None, which is compatible with ActionDefinition
        return result  # type: ignore[return-value]

    # EXTRACTED TO: RegistryDefinitionManager.create_definition_from_discovery_service() - B(7) complexity
    # EXTRACTED TO: RegistryDefinitionManager.build_action_definition() - A complexity helper

    # EXTRACTED TO: RegistryDefinitionManager.create_definition_from_registry() - B(10) complexity

    # EXTRACTED TO: RegistryDefinitionManager.get_stored_action_definition() - B(6) complexity

    async def register_action(self, action_def: dict[str, object]) -> bool:
        """
        Delegate action registration to RegistryManager service.

        REFACTORED: Extracted registry operations to RegistryManager - maintaining API contract.
        """
        return await self.registry_manager.register_action(action_def)

    def _validate_action_definition_for_registration(
        self, action_def: ActionDefinition
    ) -> dict[str, object]:
        """Validate action definition and return validation result with action name."""
        # Validate action definition using unified validator
        # Convert ActionDefinition TypedDict to dict for validation
        action_def_dict: dict[str, object] = dict(action_def)
        valid, validation_error = self.validator.definition_manager.validate_action_definition(
            action_def_dict
        )
        if not valid:
            logger.error(f"Action definition validation failed: {validation_error}")
            return {"valid": False}

        action_name = action_def.get("name")
        if not action_name:
            logger.error("Action definition missing required 'name' field")
            return {"valid": False}

        return {"valid": True, "action_name": action_name}

    def _extract_and_validate_process_info(
        self, action_def: dict[str, object], action_name: str
    ) -> dict[str, object]:
        """
        Delegate process info extraction and validation to ActionDefinitionProcessor.

        REFACTORED: Extracted B(7) complexity method to ActionDefinitionProcessor - maintaining API contract.
        """
        return self.action_definition_processor.extract_and_validate_process_info(
            action_def=action_def,
            action_name=action_name,
            process_external_id_getter=self._get_process_external_id,
        )

    def _store_action_definition_record(
        self, action_name: str, process_external_id: str, action_def: dict[str, object]
    ) -> bool:
        """Store action definition record in persistent registry and return success status."""
        if self.state_service is None:
            logger.error("State service not available for storing action definition")
            return False

        # Store action definition using normalized schema
        result = self.state_service.write_state(
            namespace=FRAMEWORK_NAMESPACE,
            data={
                "table": "action_definitions",
                "record": {
                    "action_name": action_name,
                    "process_external_id": process_external_id,
                    "description": action_def.get("description", ""),
                    "default_parameters": json.dumps(action_def.get("parameters", {})),
                    "is_enabled": 1,
                },
            },
        )

        if is_status_match(result.get("action_status"), ActionStatus.COMPLETED):
            return True
        else:
            logger.error(f"Failed to register action '{action_name}': {result}")
            return False

    async def update_action(self, action_name: str, updates: dict[str, object]) -> bool:
        """
        Delegate action updates to RegistryManager service.

        REFACTORED: Extracted registry operations to RegistryManager - maintaining API contract.
        """
        return await self.registry_manager.update_action(action_name, updates)

    def _validate_update_prerequisites(
        self, _action_name: str
    ) -> bool:  # Reserved for interface compatibility
        """Validate that prerequisites for action update are met."""
        if not self.state_service:
            logger.error("State service not available for action update")
            return False
        return True

    def _normalize_action_updates(
        self, updated_action: dict[str, object], action_name: str
    ) -> dict[str, object] | None:
        """Convert updated action definition to normalized schema format."""
        normalized_updates = {}

        # Handle standard field updates
        if "description" in updated_action:
            normalized_updates["description"] = updated_action["description"]
        if "parameters" in updated_action:
            normalized_updates["default_parameters"] = json.dumps(updated_action["parameters"])

        # Handle process changes (requires lookup of new process_external_id)
        if "process" in updated_action:
            process_obj = updated_action["process"]
            if not isinstance(process_obj, dict):
                logger.error(
                    f"Invalid process specification for action '{action_name}': expected dict"
                )
                return None
            process_external_id = self._resolve_process_external_id_for_update(
                process_obj, action_name
            )
            if process_external_id is None:
                return None  # Process lookup failed
            normalized_updates["process_external_id"] = process_external_id

        return normalized_updates

    def _resolve_process_external_id_for_update(
        self, process: dict[str, object], action_name: str
    ) -> str | None:
        """Resolve process external ID for action update process changes."""
        provider_type = process.get("provider_type", "plugin")
        provider = process.get("provider") or process.get("plugin")
        function_name = process.get("function_name")

        if provider and function_name:
            process_key = f"{provider_type}::{provider}::{function_name}"
            process_external_id = self._get_process_external_id(process_key)
            if process_external_id:
                return process_external_id
            else:
                logger.error(
                    f"Process '{process_key}' not found when updating action '{action_name}'"
                )
                return None

        logger.error(f"Invalid process specification in action update for '{action_name}'")
        return None

    def _persist_action_updates(
        self, action_name: str, normalized_updates: dict[str, object]
    ) -> bool:
        """Persist action updates to the registry and return success status."""
        if self.state_service is None:
            logger.error("State service not available for persisting action updates")
            return False

        # StateServiceProtocol doesn't have update_state, but the actual implementation does
        # We need to use write_state with a query pattern or call the underlying method
        if not hasattr(self.state_service, "update_state"):
            logger.error("State service does not support update_state operation")
            return False

        result = self.state_service.update_state(
            namespace=FRAMEWORK_NAMESPACE,
            query={"table": "action_definitions", "filters": {"action_name": action_name}},
            updates=normalized_updates,
        )

        if is_status_match(result.get("action_status"), ActionStatus.COMPLETED):
            return True
        else:
            logger.error(f"Failed to update action '{action_name}': {result}")
            return False

    async def deregister_action(self, action_name: str) -> bool:
        """
        Delegate action deregistration to RegistryManager service.

        REFACTORED: Extracted registry operations to RegistryManager - maintaining API contract.
        """
        return await self.registry_manager.deregister_action(action_name)

    def _get_process_external_id(self, process_key: str) -> str | None:
        """
        Delegate process key resolution to RegistryManager service.

        REFACTORED: Extracted registry operations to RegistryManager - maintaining API contract.
        """
        return self.registry_manager.get_process_external_id(process_key)

    def _get_process_info(self, process_external_id: str) -> dict[str, str]:
        """
        Delegate process info lookup to RegistryManager service.

        REFACTORED: Extracted registry operations to RegistryManager - maintaining API contract.
        """
        return self.registry_manager.get_process_info(process_external_id)

    def _load_file_reference(self, filename: str) -> dict[str, object] | list[object] | str | None:
        """Delegate file reference loading to VariableResolver."""
        return self.variable_resolver.load_file_reference(filename)

    async def get_registered_actions(self) -> dict[str, object]:
        """
        Delegate registered actions retrieval to RegistryManager service.

        REFACTORED: Extracted registry operations to RegistryManager - maintaining API contract.
        """
        return await self.registry_manager.get_registered_actions()

    async def sync_process_registry(self) -> bool:
        """
        Delegate process registry synchronization to RegistryManager service.

        REFACTORED: Extracted registry operations to RegistryManager - maintaining API contract.
        """
        return await self.registry_manager.sync_process_registry()

    async def _track_action_execution_start(
        self,
        execution_id: str,
        action_name: str,
        parameters: dict[str, object],
        start_time: datetime,
        source_context: dict[str, object] | None = None,
    ) -> None:
        """
        Delegate execution start tracking to ExecutionTrackingManager service.

        REFACTORED: Extracted B(6) complexity method to ExecutionTrackingManager - maintaining API contract.
        """
        await self.execution_tracking_manager.track_action_execution_start(
            execution_id, action_name, parameters, start_time, source_context
        )

    async def _update_execution_provider(
        self, execution_id: str, provider_type: str, provider: str
    ) -> None:
        """
        Delegate execution provider update to ExecutionTrackingManager service.

        REFACTORED: Extracted A complexity method to ExecutionTrackingManager - maintaining API contract.
        """
        await self.execution_tracking_manager.update_execution_provider(
            execution_id, provider_type, provider
        )

    async def _track_action_execution_end(
        self,
        execution_id: str,
        start_time: datetime,
        success: bool,
        result: dict[str, object] | None = None,
        error: dict[str, object] | None = None,
    ) -> None:
        """
        Delegate execution end tracking to ExecutionTrackingManager service.

        REFACTORED: Extracted B(8) complexity method to ExecutionTrackingManager - maintaining API contract.
        """
        await self.execution_tracking_manager.track_action_execution_end(
            execution_id, start_time, success, result, error
        )

    async def get_action_performance_stats(
        self, action_name: str | None = None, hours: int = 24
    ) -> dict[str, object]:
        """
        Delegate performance statistics retrieval to PerformanceStatsManager service.

        REFACTORED: Extracted B(6) complexity method to PerformanceStatsManager - maintaining API contract.
        """
        return await self.performance_stats_manager.get_action_performance_stats(action_name, hours)

    def _calculate_performance_stats(
        self, records: list[dict[str, object]], action_name: str | None = None
    ) -> dict[str, object]:
        """
        Delegate performance statistics calculation to PerformanceStatsManager service.

        REFACTORED: Extracted B(6) complexity method to PerformanceStatsManager - maintaining API contract.
        """
        return self.performance_stats_manager.calculate_performance_stats(records, action_name)

    def execute_plugin(
        self,
        plugin: PluginInterface,
        function_name: str,
        action_name: str,
        params: dict[str, object],
        state: dict[str, object],
        _plugin_config: dict[str, object],  # Reserved for interface compatibility
    ) -> dict[str, object]:
        """
        Execute a plugin function with proper validation and error handling.

        REFACTORED: Extracted helper methods to reduce complexity from C(12).

        This method provides centralized plugin execution with validation,
        action object preparation, and comprehensive error handling.
        """
        # Phase 1: Pre-execution validation
        self.validation_service.validate_plugin_execution_prerequisites(plugin, action_name)

        try:
            result = self._execute_plugin_phases(plugin, function_name, action_name, params, state)
            # Result is dict[str, object] which is compatible with PluginExecutionResult
            return result
        except (AttributeError, TypeError, AnantaError, Exception) as e:
            raise self._handle_plugin_execution_error(e, action_name) from e

    def _execute_plugin_phases(
        self,
        plugin: PluginInterface,
        function_name: str,
        action_name: str,
        params: dict[str, object],
        state: dict[str, object],
    ) -> dict[str, object]:
        """Execute the plugin through preparation, validation, and execution phases."""
        # Phase 2: Prepare action object and validate plugin function
        action: dict[str, object] = {"name": action_name, "parameters": params}
        prepared_action_def: dict[str, object] = {
            "name": action_name,
            "function_name": function_name,
        }
        action_object, _timestamp = self.provider_manager.prepare_plugin_action_object(
            action=action,
            prepared_action_def=prepared_action_def,
            merged_parameters=params,
            state=state,
            function_name=function_name,
        )

        plugin_function = self.validation_service.validate_and_get_plugin_function(
            plugin, function_name, action_name
        )

        # Phase 3: Execute plugin function and validate result
        return self.validation_service.execute_and_validate_plugin_result(
            plugin_function, action_name, action_object
        )

    def _handle_plugin_execution_error(self, error: Exception, action_name: str) -> Exception:
        """Handle different types of plugin execution errors with appropriate error types."""
        if isinstance(error, AttributeError):
            return self._handle_attribute_error(error, action_name)
        elif isinstance(error, TypeError):
            return self._handle_type_error(error, action_name)
        elif isinstance(error, AnantaError):
            return error  # Re-raise AnantaError as-is
        else:
            return self._handle_generic_error(error, action_name)

    def _handle_attribute_error(self, error: AttributeError, action_name: str) -> PluginError:
        """Handle AttributeError during plugin execution."""
        logger.error(f"Plugin method error for '{action_name}': {str(error)}", exc_info=True)
        return PluginError(
            message=f"Plugin missing required method: {str(error)}",
            error_code=ErrorCode.PLUGIN_MISSING_METHOD,
            original_error=error,
        )

    def _handle_type_error(self, error: TypeError, action_name: str) -> FrameworkError:
        """Handle TypeError during plugin execution."""
        logger.error(f"Type error executing action '{action_name}': {str(error)}", exc_info=True)
        return FrameworkError(
            message=f"Invalid parameter types: {str(error)}",
            error_code=ErrorCode.ACTION_INVALID_PARAMETER_TYPE,
            original_error=error,
        )

    def _handle_generic_error(self, error: Exception, action_name: str) -> FrameworkError:
        """Handle generic exceptions during plugin execution."""
        logger.error(f"Error executing action '{action_name}': {str(error)}", exc_info=True)
        return FrameworkError(
            message=str(error),
            error_code=ErrorCode.ACTION_EXECUTION_FAILED,
            original_error=error,
        )

    def _validate_plugin_execution_prerequisites(
        self, plugin: PluginInterface, action_name: str
    ) -> None:
        """
        Delegate plugin execution prerequisite validation to ValidationCoordinator.

        REFACTORED: Extracted validation logic to ValidationCoordinator - maintaining API contract.
        """
        return self.validation_coordinator.validate_plugin_execution_prerequisites(
            plugin, action_name
        )

    def _validate_and_get_plugin_function(
        self, plugin: PluginInterface, function_name: str, action_name: str
    ) -> Callable[[dict[str, object], dict[str, object], str, dict[str, object]], object]:
        """
        Delegate plugin function validation to ValidationCoordinator.

        REFACTORED: Extracted validation logic to ValidationCoordinator - maintaining API contract.
        """
        return self.validation_coordinator.validate_and_get_plugin_function(
            plugin, function_name, action_name
        )

    def _execute_and_validate_plugin_result(
        self,
        plugin_function: Callable[
            [dict[str, object], dict[str, object], str, dict[str, object]], object
        ],
        action_object: dict[str, object],
        state: dict[str, object],
        plugin_config: dict[str, object],
        action_name: str,
        timestamp: str,
    ) -> dict[str, object]:
        """
        Delegate plugin result execution and validation to ValidationCoordinator.

        REFACTORED: Extracted validation logic to ValidationCoordinator - maintaining API contract.
        """
        return self.validation_coordinator.execute_and_validate_plugin_result(
            plugin_function=plugin_function,
            action_object=action_object,
            state=state,
            app_home=str(self.APP_HOME),
            plugin_config=plugin_config,
            action_name=action_name,
            timestamp=timestamp,
        )

    def _parse_action_parameters(
        self, action_name: str, action_def_or_parameters: dict[str, object]
    ) -> tuple[dict[str, object], dict[str, object] | None, bool]:
        """
        Delegate action parameter parsing to ParameterProcessor service.

        REFACTORED: Extracted parameter parsing logic to ParameterProcessor - maintaining API contract.
        """
        return self.parameter_processor.parse_action_parameters(
            action_name, action_def_or_parameters
        )

    def _create_source_context(
        self, state: dict[str, object], execution_id: str
    ) -> dict[str, object]:
        """
        Delegate source context creation to ParameterProcessor service.

        REFACTORED: Extracted source context creation logic to ParameterProcessor - maintaining API contract.
        """
        return self.parameter_processor.create_source_context(state, execution_id)

    def _resolve_process_key(
        self,
        action_name: str,
        action_parameters: dict[str, object],
        action_def_or_parameters: dict[str, object],
        prepared_action_def: dict[str, object] | None,
        process_key: str | None,
        state: dict[str, object],
    ) -> str:
        """
        Delegate process key resolution to RegistryManager service.

        REFACTORED: Extracted registry operations to RegistryManager - maintaining API contract.
        """
        return self.registry_manager.process_key_resolver.resolve_process_key(
            action_name=action_name,
            action_parameters=action_parameters,
            action_def_or_parameters=action_def_or_parameters,
            prepared_action_def=prepared_action_def,
            process_key=process_key,
            state=state,
            action_definition_getter=self._get_action_definition_for_process_key_resolution,
        )

    def _get_action_definition_for_process_key_resolution(
        self, action_name: str, state: dict[str, object] | None, process_key: str | None
    ) -> dict[str, object] | None:
        """
        Delegate safe action definition retrieval to RegistryManager service.

        REFACTORED: Extracted registry operations to RegistryManager - maintaining API contract.
        """
        return self.registry_manager.get_safe_action_definition(action_name, state, process_key)

    def _validate_and_prepare_process_record(
        self, process_key: str, process_data: dict[str, object]
    ) -> dict[str, object] | None:
        """
        Delegate process record validation to RegistryManager service.

        REFACTORED: Extracted registry operations to RegistryManager - maintaining API contract.
        """
        return self.registry_manager.validate_and_prepare_process_record(process_key, process_data)

    def _compute_action_stats(self, action_records: list[dict[str, object]]) -> dict[str, object]:
        """
        Delegate action statistics computation to PerformanceStatsManager service.

        REFACTORED: Extracted B(8) complexity method to PerformanceStatsManager - maintaining API contract.
        """
        return self.performance_stats_manager.compute_action_stats(action_records)

    async def _validate_action_completeness(
        self,
        action_def: dict[str, object],
        action_name: str,
        is_prepared_action: bool,
        is_runtime_generated: bool,
        execution_id: str,
        start_time: datetime,
    ) -> None:
        """Validate that action is complete (no template variables) before execution.

        EXTRACTED TO: ActionValidationManager.validate_action_completeness() - B(7) complexity
        """
        await self.action_validation_manager.validate_action_completeness(
            action_def=action_def,
            action_name=action_name,
            is_prepared_action=is_prepared_action,
            is_runtime_generated=is_runtime_generated,
            execution_id=execution_id,
            start_time=start_time,
        )

    async def execute_action(
        self,
        action_name: str,
        state: dict[str, object],
        action_def_or_parameters: dict[str, object],
        plugin_override: str | None = None,
        process_key: str | None = None,
    ) -> dict[str, object]:
        """Execute an action with comprehensive error handling and tracking."""
        result = await self.execution_engine.execute_action(
            action_name, state, action_def_or_parameters, plugin_override, process_key
        )

        # Handle result_processor if specified in action definition
        # Note: This functionality may need to be moved to ActionExecutionEngine in future refactoring
        if result.get("result_processor"):
            await self._handle_result_processor(result, action_name, result, state)

        return result

    async def _resolve_process_key_with_error_handling(
        self,
        action_name: str,
        action_parameters: dict[str, object],
        action_def_or_parameters: dict[str, object],
        prepared_action_def: dict[str, object] | None,
        process_key: str | None,
        state: dict[str, object],
        execution_id: str,
        start_time: datetime,
    ) -> str:
        """Resolve process_key with proper error handling and tracking."""
        try:
            return self._resolve_process_key(
                action_name,
                action_parameters,
                action_def_or_parameters,
                prepared_action_def,
                process_key,
                state,
            )
        except FrameworkError as error:
            await self._track_action_execution_end(
                execution_id, start_time, success=False, error=error.to_dict()
            )
            raise

    async def _prepare_action_definition(
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
    ) -> tuple[dict[str, object], dict[str, object]]:
        """
        Delegate action definition preparation to ActionDefinitionProcessor.

        REFACTORED: Extracted B(8) complexity method to ActionDefinitionProcessor - maintaining API contract.
        """
        return await self.action_definition_processor.prepare_action_definition(
            action_name=action_name,
            action_def_or_parameters=action_def_or_parameters,
            prepared_action_def=prepared_action_def,
            is_prepared_action=is_prepared_action,
            action_parameters=action_parameters,
            state=state,
            process_key=process_key,
            execution_id=execution_id,
            start_time=start_time,
            legacy_definition_getter=self._get_legacy_action_definition,
        )

    def _get_legacy_action_definition(
        self,
        action_name: str,
        action_parameters: dict[str, object],
        state: dict[str, object],
        process_key: str,
    ) -> dict[str, object]:
        """Get action definition for legacy path with parameter merging."""
        retrieved_action_def = self.get_action_definition(action_name, state, process_key)
        if not retrieved_action_def:
            logger.error(
                f"No action definition found for '{action_name}' - using runtime parameters only"
            )
            return {"name": action_name, "arguments": action_parameters.copy()}
        else:
            # Merge runtime parameters with action definition arguments
            arguments_obj = retrieved_action_def.get("arguments", {})
            if not isinstance(arguments_obj, dict):
                merged_arguments = {}
            else:
                merged_arguments = arguments_obj.copy()
            merged_arguments.update(action_parameters)
            action_def = dict(retrieved_action_def)  # Create copy to avoid modifying original
            action_def["arguments"] = merged_arguments
            return action_def

    async def _validate_prepared_action(
        self,
        resolved_action: dict[str, object],
        action_name: str,
        state: dict[str, object],
        execution_id: str,
        start_time: datetime,
    ) -> None:
        """Validate prepared action before execution."""

        try:
            source_context: dict[str, object] = {
                "plugin_level": getattr(state, "current_plugin", "action_manager"),
                "request_level": execution_id,
                "action_level": "action_manager_execute_action",
                "chain_depth": getattr(state, "action_chain_depth", 1),
                "trigger_type": getattr(state, "trigger_type", "action_manager_execution"),
                "session_id": getattr(state, "session_id", None),
                "parent_action_id": getattr(state, "parent_action_id", None),
            }

            validation_result = self.validator.validate_with_routing(
                dict(resolved_action), dict(resolved_action), source_context
            )
            if not validation_result.success:
                validation_error = FrameworkError(
                    message=f"Action validation failed for '{action_name}': {validation_result.error_message}",
                    error_code=ErrorCode.ACTION_INVALID_FORMAT,
                    details={
                        "action_name": action_name,
                        "validation_error": validation_result.error_message,
                    },
                    severity=ErrorSeverity.ERROR,
                )
                logger.error(f"VALIDATION_FAILED: {validation_error.message}")
                await self._track_action_execution_end(
                    execution_id, start_time, success=False, error=validation_error.to_dict()
                )
                raise validation_error

        except (TemplateResolutionError, UnresolvedTemplateVariablesError) as e:
            logger.error(f"Template resolution failed for action '{action_name}': {e}")
            template_error = FrameworkError(
                message=f"Template resolution failed for action '{action_name}': {str(e)}",
                error_code=ErrorCode.ACTION_INVALID_FORMAT,
                details={"action_name": action_name, "template_error": str(e)},
                original_error=e,
            )
            await self._track_action_execution_end(
                execution_id, start_time, success=False, error=template_error.to_dict()
            )
            raise template_error from None

    async def _execute_action_by_provider(
        self,
        action_name: str,
        process_key: str,
        merged_params: dict[str, object],
        state: dict[str, object],
        plugin_override: str | None,
        execution_id: str,
    ) -> dict[str, object]:
        """Execute action based on provider type after parsing process_key."""
        # Parse process_key to get provider information
        provider_type, provider, function_name = self._parse_process_key(process_key, action_name)

        # Apply plugin override handling
        provider = self._apply_plugin_override(
            provider_type, provider, plugin_override, action_name
        )

        # Route to appropriate execution method
        if provider_type == "service_interface":
            return await self._execute_service_interface_action(
                action_name, provider, function_name, merged_params, state, execution_id
            )
        elif provider_type == "plugin":
            return await self._execute_plugin_action(
                action_name, provider, function_name, merged_params, state, execution_id
            )
        else:
            raise FrameworkError(
                message=f"Unsupported provider_type '{provider_type}' for action '{action_name}'",
                error_code=ErrorCode.ACTION_MISSING_PLUGIN,
                details={
                    "action_name": action_name,
                    "provider_type": provider_type,
                    "supported_types": ["service_interface", "plugin"],
                },
            )

    def _parse_process_key(self, process_key: str, action_name: str) -> tuple[str, str, str]:
        """Parse process_key into provider components."""

        try:
            provider_type, provider, function_name = process_key.split("::", 2)
            return provider_type, provider, function_name
        except ValueError as e:
            logger.error(
                f'Invalid process_key format: "{process_key}" - expected "provider_type::provider::function_name"'
            )
            raise FrameworkError(
                message=f"Invalid process_key format: {process_key}",
                error_code=ErrorCode.ACTION_INVALID_FORMAT,
                details={"action_name": action_name, "process_key": process_key},
            ) from e

    def _apply_plugin_override(
        self, provider_type: str, provider: str, plugin_override: str | None, action_name: str
    ) -> str:
        """Apply plugin override only for plugin provider types."""
        if plugin_override and provider_type == "plugin":
            return plugin_override
        elif plugin_override and provider_type != "plugin":
            logger.error(
                f"Ignoring plugin override '{plugin_override}' for action '{action_name}' - process_key specifies provider_type '{provider_type}', not 'plugin'"
            )
        return provider

    async def _execute_service_interface_action(
        self,
        action_name: str,
        provider: str,
        function_name: str,
        merged_params: dict[str, object],
        state: dict[str, object],
        execution_id: str,
    ) -> dict[str, object]:
        """Execute service interface action."""

        # Handle service interface provider
        service = self.provider_manager.get_service_instance(provider, action_name, function_name)

        # Update execution record with service provider name
        await self._update_execution_provider(execution_id, "service_interface", provider)

        result = await self.execute_service(
            service, function_name, action_name, merged_params, state
        )
        return result

    async def _execute_plugin_action(
        self,
        action_name: str,
        provider: str,
        function_name: str,
        merged_params: dict[str, object],
        state: dict[str, object],
        execution_id: str,
    ) -> dict[str, object]:
        """Execute plugin action."""
        # Handle plugin provider (legacy and new format)
        plugin_obj = self.provider_manager.get_plugin_instance(provider, action_name)

        # Narrow type: ensure plugin is PluginInterface (must be runtime_checkable)
        # Since PluginInterface is imported from protocols, check if it has required attributes
        if not (hasattr(plugin_obj, "name") and hasattr(plugin_obj, "execute_action")):
            raise FrameworkError(
                message=f"Plugin '{provider}' does not implement PluginInterface",
                error_code=ErrorCode.PLUGIN_MISSING_METHOD,
            )
        plugin: PluginInterface = plugin_obj  # type: ignore[assignment]

        # Update execution record with actual plugin name
        await self._update_execution_provider(execution_id, "plugin", provider)

        plugin_config = get_config().get_plugin_config(plugin.name)

        result = self.execute_plugin(
            plugin, function_name, action_name, merged_params, state, plugin_config
        )
        # execute_plugin returns dict[str, object] compatible with PluginExecutionResult
        return result

    def _validate_action_parameters(
        self, action_name: str, action_parameters: dict[str, object], action_def: dict[str, object]
    ) -> None:
        # ARCHITECTURAL SEPARATION: Validation layer uses 'parameters' (processed data for validation)
        action: dict[str, object] = {"name": action_name, "parameters": action_parameters}
        validate_action(action, action_def)

    def _prepare_action_execution(
        self, action_name: str, action_def: dict[str, object], plugin_override: str | None = None
    ) -> tuple[str, str, str]:
        """
        Prepare action execution parameters with validation and provider resolution.

        REFACTORED: Extracted helper methods to reduce complexity from C(12).

        This method provides centralized action execution preparation with
        plugin override handling, legacy support, and comprehensive validation.
        """
        process_obj = action_def.get("process", {})
        if not isinstance(process_obj, dict):
            raise FrameworkError(
                message=f"Invalid process specification for action '{action_name}': expected dict",
                error_code=ErrorCode.ACTION_INVALID_FORMAT,
            )
        process: dict[str, object] = process_obj

        # Phase 1: Resolve provider information (with plugin override support)
        provider_type, provider, function_name = self.provider_manager.resolve_action_provider_info(
            action_name, process, plugin_override
        )

        # Phase 2: Validate all required execution parameters
        # Convert provider_type to string for validation
        provider_type_str = str(provider_type)
        provider_str = str(provider) if not isinstance(provider, str) else provider
        self.validation_service.validate_action_execution_parameters(
            action_name, provider_type_str, provider_str, function_name, process
        )

        return provider_type_str, provider_str, function_name

    def _validate_action_execution_parameters(
        self,
        action_name: str,
        provider_type: str,
        provider: str,
        function_name: str,
        process: dict[str, object],
    ) -> None:
        """Validate all required action execution parameters are present and valid.

        EXTRACTED TO: ActionValidationManager.validate_action_execution_parameters() - B(6) complexity
        """
        self.action_validation_manager.validate_action_execution_parameters(
            action_name=action_name,
            provider_type=provider_type,
            provider=provider,
            function_name=function_name,
            process=process,
        )

    async def execute_service(
        self,
        service: object,  # Keep as object since service types vary
        function_name: str,
        action_name: str,
        params: dict[str, object],
        _state: dict[str, object],  # Reserved for interface compatibility
    ) -> dict[str, object]:
        """
        Execute service interface action with comprehensive validation and formatting.

        REFACTORED: Extracted helper methods to reduce complexity from C(13).
        """

        try:
            # Phase 1: Validate service and function
            function_validated = self.validation_service.validate_service_function(
                service, function_name, action_name
            )
            # Cast to proper type after validation - validation ensures it returns dict
            function: Callable[[dict[str, object]], dict[str, object]] = function_validated  # type: ignore[assignment]
            timestamp = datetime.now(UTC).isoformat()

            # Phase 2: Execute service function
            result = await self._execute_service_function(service, function, function_name, params)

            # Phase 3: Format and return result
            return self._format_service_result(result, timestamp)

        except Exception as e:
            logger.error(f"Error executing service action '{action_name}': {str(e)}", exc_info=True)
            raise FrameworkError(
                message=str(e),
                error_code=ErrorCode.ACTION_EXECUTION_FAILED,
                original_error=e,
            ) from e

    def _validate_service_function(
        self, service: object, function_name: str, action_name: str
    ) -> Callable[[dict[str, object]], dict[str, object]]:
        """
        Delegate service function validation to ValidationCoordinator.

        REFACTORED: Extracted validation logic to ValidationCoordinator - maintaining API contract.
        """
        func = self.validation_coordinator.validate_service_function(
            service, function_name, action_name
        )
        # Cast validated function to expected type
        return func  # type: ignore[return-value]

    async def _execute_service_function(
        self,
        service: object,
        function: Callable[[dict[str, object]], dict[str, object]],
        function_name: str,
        params: dict[str, object],
    ) -> dict[str, object]:
        """Execute the service function with proper routing and parameter handling."""

        result = await self._route_service_function_call(service, function, function_name, params)
        self._log_service_function_result(result)

        return result

    async def _route_service_function_call(
        self,
        service: object,
        function: Callable[[dict[str, object]], dict[str, object]],
        function_name: str,
        params: dict[str, object],
    ) -> dict[str, object]:
        """Route the service function call to the appropriate handler."""
        if function_name in ["start_service_via_interface", "stop_service"]:
            return await self._execute_special_service_method(service, function_name, params)
        else:
            return await self._execute_generic_service_method(function, function_name, params)

    async def _execute_special_service_method(
        self, service: object, function_name: str, params: dict[str, object]
    ) -> dict[str, object]:
        """Execute special service interface methods with custom routing."""
        if function_name == "start_service_via_interface":
            method = getattr(service, "start_service_via_interface", None)
            if method is None:
                raise FrameworkError(
                    message="Service does not have method 'start_service_via_interface'",
                    error_code=ErrorCode.ACTION_EXECUTION_FAILED,
                )
            result_obj: object = await method(params)
            if not isinstance(result_obj, dict):
                raise FrameworkError(
                    message=f"Expected dict result, got {type(result_obj)}",
                    error_code=ErrorCode.ACTION_EXECUTION_FAILED,
                )
            return result_obj
        elif function_name == "stop_service":
            method = getattr(service, "stop_service_via_interface", None)
            if method is None:
                raise FrameworkError(
                    message="Service does not have method 'stop_service_via_interface'",
                    error_code=ErrorCode.ACTION_EXECUTION_FAILED,
                )
            result_obj2: object = await method(params)
            if not isinstance(result_obj2, dict):
                raise FrameworkError(
                    message=f"Expected dict result, got {type(result_obj2)}",
                    error_code=ErrorCode.ACTION_EXECUTION_FAILED,
                )
            return result_obj2
        else:
            raise FrameworkError(
                message=f"Unknown special service method: {function_name}",
                error_code=ErrorCode.ACTION_EXECUTION_FAILED,
            )

    async def _execute_generic_service_method(
        self,
        function: Callable[[dict[str, object]], dict[str, object]],
        function_name: str,
        params: dict[str, object],
    ) -> dict[str, object]:
        """Execute generic service interface functions."""
        import inspect

        result: dict[str, object]
        if inspect.iscoroutinefunction(function):
            # Function signature is (**params) but type says (dict). Call with dict.
            result_obj = await function(params)
            if not isinstance(result_obj, dict):
                raise FrameworkError(
                    message=f"Expected dict result from {function_name}, got {type(result_obj)}",
                    error_code=ErrorCode.ACTION_EXECUTION_FAILED,
                )
            result = result_obj
        else:
            # Function signature is (**params) but type says (dict). Call with dict.
            result = function(params)

        return result

    def _log_service_function_result(self, result: dict[str, object]) -> None:
        """Log information about the service function result."""
        pass

    def _format_service_result(
        self, result: dict[str, object], timestamp: str
    ) -> dict[str, object]:
        """Format service result into proper action response format.

        EXTRACTED TO: ResultProcessingManager.format_service_result() - B(6) complexity
        """
        formatted = self.result_processing_manager.format_service_result(result, timestamp)
        # format_service_result returns dict[str, object] compatible with PluginExecutionResult
        return formatted

    async def _route_for_correction(
        self,
        action_name: str,
        validation_result: ValidationResult,
        execution_id: str,
        start_time: datetime,
    ) -> None:
        """Route invalid action back to originating inference provider for correction."""
        try:
            self._log_correction_routing_info(action_name, validation_result)
            success = await self._attempt_correction_routing(action_name, validation_result)
            await self._track_correction_routing_result(
                execution_id, start_time, success, validation_result
            )
        except Exception as e:
            await self._handle_correction_routing_error(action_name, execution_id, start_time, e)

    def _log_correction_routing_info(
        self, action_name: str, validation_result: ValidationResult
    ) -> None:
        """Log information about the correction routing attempt."""
        pass

    async def _attempt_correction_routing(
        self, action_name: str, validation_result: ValidationResult
    ) -> bool:
        """Attempt to route the correction event through the event bus."""
        if not self.event_bus:
            logger.error(
                f"Event bus not available - cannot route correction for action '{action_name}'"
            )
            return False

        correction_event = self._create_correction_event(action_name, validation_result)
        # EventBusProtocol.publish returns bool indicating success
        success = self.event_bus.publish(correction_event)

        self._log_correction_event_result(action_name, validation_result, success)
        return success

    def _create_correction_event(
        self, action_name: str, validation_result: ValidationResult
    ) -> ActionRequestEvent:
        """Create an action correction event."""
        return create_action_correction_event(
            action_name=action_name,
            original_action_data={
                "name": action_name,
                "parameters": {},
            },  # Basic action data for validation context
            validation_error=validation_result.error_message or "Validation failed",
            source_plugin="action_manager",  # Source is the ActionManager
            target_plugin=validation_result.route_to_plugin or "unknown",
            suggested_actions=validation_result.suggested_actions,
            correction_attempt=validation_result.correction_attempt,
            original_context=validation_result.original_context,
        )

    def _log_correction_event_result(
        self, action_name: str, validation_result: ValidationResult, success: bool
    ) -> None:
        """Log the result of the correction event publication."""
        if success:
            pass
        else:
            logger.error(f"Failed to publish correction event for action '{action_name}'")

    async def _track_correction_routing_result(
        self,
        execution_id: str,
        start_time: datetime,
        success: bool,
        validation_result: ValidationResult,
    ) -> None:
        """Track the routing attempt result for debugging."""
        await self._track_action_execution_end(
            execution_id,
            start_time,
            success=success,
            error={
                "type": "routing_for_correction",
                "route_to_plugin": validation_result.route_to_plugin,
                "validation_error": validation_result.error_message,
                "correction_attempt": validation_result.correction_attempt,
                "event_bus_available": self.event_bus is not None,
            },
        )

    async def _handle_correction_routing_error(
        self, action_name: str, execution_id: str, start_time: datetime, error: Exception
    ) -> None:
        """Handle errors during correction routing."""
        logger.error(f"Error in routing for correction for action '{action_name}': {error}")
        await self._track_action_execution_end(
            execution_id,
            start_time,
            success=False,
            error={"type": "routing_error", "error": str(error)},
        )

    async def _handle_result_processor(
        self,
        action_def: dict[str, object],
        action_name: str,
        result: dict[str, object],
        state: dict[str, object],
    ) -> None:
        """Handle result processor action with template resolution and state queuing.

        EXTRACTED TO: ResultProcessingManager.handle_result_processor() - B(8) complexity
        """
        await self.result_processing_manager.handle_result_processor(
            action_def=action_def, action_name=action_name, result=result, state=state
        )
