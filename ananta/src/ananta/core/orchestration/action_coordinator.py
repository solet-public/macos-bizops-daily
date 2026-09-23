import logging
import threading
from typing import TYPE_CHECKING

from ananta.constants import NOTES_MAX_LENGTH
from ananta.core.actions.action_factory import ActionFactory
from ananta.core.actions.action_manager import ActionManager
from ananta.core.actions.action_processor import ActionProcessor, OrchestratorProtocol
from ananta.core.actions.action_queue_poller import ActionQueuePoller
from ananta.core.events import EventProcessor
from ananta.core.orchestration.execution_context import ExecutionContextManager
from ananta.core.plugins.plugin_manager import PluginManager
from ananta.core.state.state_manager import StateManager
from ananta.services.action_preparation_service import ActionPreparationService
from ananta.services.state_service import StateService

if TYPE_CHECKING:
    from ananta.core.orchestration.managers.process_registry_manager import ProcessRegistryManager

logger = logging.getLogger(__name__)


class ActionCoordinator:
    """
    Coordinates all action processing components.
    Extracted from EventOrchestrator to reduce complexity.
    """

    def __init__(self) -> None:
        # Action processing components
        self.action_processor: ActionProcessor | None = None
        self.action_manager: ActionManager | None = None
        self.action_preparation_service: ActionPreparationService | None = None
        self.action_factory: ActionFactory | None = None
        self.action_queue_poller: ActionQueuePoller | None = None
        self.event_processor: EventProcessor | None = None

        # Phase 1: ExecutionContext for runtime placeholder resolution
        self.execution_context_manager: ExecutionContextManager = ExecutionContextManager()
        logger.debug("ActionCoordinator: ExecutionContextManager created")

        # Process registry
        self._process_registry_manager: ProcessRegistryManager | None = None
        self._process_registry: dict[str, object] = {}

        # FRG token management
        self._flow_runtime_graph: object | None = None

        # Flags
        self._framework_services_initialized = False
        self._plugins_ready = False

    def _extract_services_from_dict(self, services_dict: dict[str, object]) -> dict[str, object]:
        """Extract and return all needed services from the services dictionary."""
        return {
            "app_home": services_dict["app_home"],
            "plugin_manager": services_dict["plugin_manager"],
            "state_service": services_dict["state_service"],
            "state_manager": services_dict["state_manager"],
            "async_job_manager": services_dict["async_job_manager"],
            "event_bus": services_dict["event_bus"],
            "event_handler_registry": services_dict["event_handler_registry"],
            "unified_metadata_registry": services_dict["unified_metadata_registry"],
            "action_recorder": services_dict["action_recorder"],
            "session_manager": services_dict["session_manager"],
            "flow_manager": services_dict["flow_manager"],
            "flow_runtime_graph": services_dict.get("flow_runtime_graph"),
            "blob_storage_service": services_dict.get("blob_storage_service"),
            "inference_service": services_dict.get("inference_service"),
            "embedding_service": services_dict.get("embedding_service"),
            "vector_service": services_dict.get("vector_service"),
            "discovery_service": services_dict.get("discovery_service"),
            "memory_service": services_dict.get("memory_service"),
            "knowledge_service": services_dict.get("knowledge_service"),
            "io_interface_service": services_dict.get("io_interface_service"),
        }

    def _initialize_core_action_services(
        self, orchestrator_ref: OrchestratorProtocol, services: dict[str, object]
    ) -> None:
        """Initialize the core action processing services."""
        # Type narrow the services before passing to initialization methods
        plugin_manager = services["plugin_manager"]
        state_service = services["state_service"]
        app_home = services["app_home"]

        if not isinstance(plugin_manager, PluginManager):
            raise TypeError(f"plugin_manager must be PluginManager, got {type(plugin_manager)}")
        if not isinstance(state_service, StateService):
            raise TypeError(f"state_service must be StateService, got {type(state_service)}")

        # Use the existing DiscoveryService from services (created in startup_sequence.py)
        # DO NOT create a new DiscoveryService here - that would cause duplicate embeddings
        # because _load_processes() would store all processes again to the same vector database.
        # The existing discovery_service already has all processes stored via build_and_populate_registry().
        discovery_service_arg = services.get("discovery_service")
        if discovery_service_arg is not None:
            logger.debug("ActionCoordinator: Using existing DiscoveryService from services")
        else:
            logger.error("ActionCoordinator: No discovery_service in services dict")

        # Initialize ActionProcessor with orchestrator for dynamic service resolution
        self._initialize_action_processor(
            plugin_manager,
            state_service,
            orchestrator_ref,
            discovery_service_arg,
            services.get("blob_storage_service"),
            services.get("memory_service"),
            services.get("knowledge_service"),
            services.get("io_interface_service"),
        )

        # Type narrow for ActionManager
        state_manager = services["state_manager"]
        if not isinstance(state_manager, StateManager):
            raise TypeError(f"state_manager must be StateManager, got {type(state_manager)}")

        # Initialize ActionManager with DiscoveryService and MemoryService
        self._initialize_action_manager(
            app_home,
            plugin_manager,
            state_manager,
            state_service,
            services["async_job_manager"],
            services["event_bus"],
            orchestrator_ref,
            services["unified_metadata_registry"],
            discovery_service_arg,  # Pass the same DiscoveryService instance
            services.get("memory_service"),  # Pass memory_service for template function execution
            services.get("knowledge_service"),
        )

        # Initialize ActionPreparationService with DiscoveryService and MemoryService
        self._initialize_action_preparation_service(
            app_home,
            state_service,
            plugin_manager,
            services["unified_metadata_registry"],
            discovery_service_arg,  # Pass the same DiscoveryService instance
            services.get("memory_service"),  # Pass memory_service for template function execution
            services.get("knowledge_service"),
        )

    def _initialize_event_and_plugin_services(
        self, orchestrator_ref: OrchestratorProtocol, services: dict[str, object]
    ) -> None:
        """Initialize event processor and plugin injection services."""
        # Type narrow services
        plugin_manager = services["plugin_manager"]
        state_service = services["state_service"]
        state_manager = services["state_manager"]

        if not isinstance(plugin_manager, PluginManager):
            raise TypeError(f"plugin_manager must be PluginManager, got {type(plugin_manager)}")
        if not isinstance(state_service, StateService):
            raise TypeError(f"state_service must be StateService, got {type(state_service)}")
        if not isinstance(state_manager, StateManager):
            raise TypeError(f"state_manager must be StateManager, got {type(state_manager)}")

        # Initialize EventProcessor
        self._initialize_event_processor(
            services["event_handler_registry"],
            state_service,
            state_manager,
            services["action_recorder"],
            services["session_manager"],
            services["flow_manager"],
        )

        # Inject services into plugins
        self._inject_plugin_services(
            plugin_manager,
            state_service,
            orchestrator_ref,
            services["blob_storage_service"],
            services.get("embedding_service"),
        )

        # Initialize ActionQueuePoller with FRG support
        self._flow_runtime_graph = services.get("flow_runtime_graph")
        self._initialize_action_queue_poller(
            state_service,
            services["event_bus"],
            self._flow_runtime_graph,
            services.get("memory_service"),
            str(services["app_home"]),
            services.get("inference_service"),
            services.get("blob_storage_service"),
            services.get("discovery_service"),
            services.get("io_interface_service"),
        )

    def initialize_action_components(
        self, orchestrator_ref: OrchestratorProtocol, services_dict: dict[str, object]
    ) -> None:
        """Initialize all action processing components in the correct order."""
        logger.debug("ActionCoordinator: Starting action component initialization")

        services = self._extract_services_from_dict(services_dict)

        # Step 1: Initialize process registry FIRST (needed for DiscoveryService)
        plugin_manager = services["plugin_manager"]
        state_service = services["state_service"]
        state_manager = services["state_manager"]

        if not isinstance(plugin_manager, PluginManager):
            raise TypeError(f"plugin_manager must be PluginManager, got {type(plugin_manager)}")
        if not isinstance(state_service, StateService):
            raise TypeError(f"state_service must be StateService, got {type(state_service)}")
        if not isinstance(state_manager, StateManager):
            raise TypeError(f"state_manager must be StateManager, got {type(state_manager)}")

        self._initialize_process_registry(
            plugin_manager, state_service, state_manager, services.get("discovery_service")
        )

        # Step 2: Initialize core action services (now process_registry is available)
        self._initialize_core_action_services(orchestrator_ref, services)

        # Step 3: Initialize factory services (uses action_manager that was just created)
        self._initialize_action_factory(
            state_service,
            services["action_recorder"],
            state_manager,
            str(services["app_home"]),
        )

        # PHASE 2 COMPLIANCE: Do NOT save to database during initialization
        # Database operations deferred to Phase 3 via complete_database_initialization()

        self._initialize_event_and_plugin_services(orchestrator_ref, services)

        logger.debug("ActionCoordinator: Phase 2 action components initialized successfully")
        logger.debug("ActionCoordinator: Database operations deferred to Phase 3")

    def _initialize_action_processor(
        self,
        plugin_manager: PluginManager,
        state_service: StateService,
        orchestrator_ref: OrchestratorProtocol,
        discovery_service: object | None = None,
        blob_storage_service: object | None = None,
        memory_service: object | None = None,
        knowledge_service: object | None = None,
        io_interface_service: object | None = None,
    ) -> None:
        """Initialize ActionProcessor with orchestrator for service-agnostic architecture."""
        logger.debug("ActionCoordinator: Initializing ActionProcessor")
        logger.debug(
            f"ActionCoordinator: discovery_service available: {discovery_service is not None}"
        )
        logger.debug(
            f"ActionCoordinator: blob_storage_service available: {blob_storage_service is not None}"
        )
        logger.debug(f"ActionCoordinator: memory_service available: {memory_service is not None}")
        logger.debug("ActionCoordinator: orchestrator provided")

        # Extract IO interface registry from service for plugin post_message OUTPUT event detection
        io_registry = None
        if io_interface_service is not None:
            io_registry = getattr(io_interface_service, "registry", None)

        try:
            self.action_processor = ActionProcessor(
                plugin_manager=plugin_manager,
                state_service=state_service,
                discovery_service=discovery_service,
                execution_context_manager=self.execution_context_manager,  # Phase 1
                blob_storage_service=blob_storage_service,
                orchestrator=orchestrator_ref,  # Pass orchestrator for dynamic service resolution
                process_registry=self._process_registry,  # Pass process registry for service method validation
                memory_service=memory_service,  # Pass memory service for template function resolution
                knowledge_service=knowledge_service,
                io_interface_registry=io_registry,
            )
            logger.debug(
                "ActionCoordinator: ActionProcessor initialized successfully with service-agnostic architecture"
            )
        except Exception as e:
            logger.error(f"ActionCoordinator: Failed to initialize ActionProcessor: {e}")
            raise

    def _initialize_action_manager(
        self,
        app_home: object,
        plugin_manager: PluginManager,
        state_manager: StateManager[dict[str, object]],
        state_service: StateService,
        async_job_manager: object,
        event_bus: object,
        orchestrator_ref: OrchestratorProtocol,
        unified_metadata_registry: object,
        discovery_service: object | None = None,
        memory_service: object | None = None,
        knowledge_service: object | None = None,
    ) -> None:
        """Initialize ActionManager for EventProcessor integration."""
        logger.debug("ActionCoordinator: Initializing ActionManager")
        logger.debug(
            f"ActionCoordinator: discovery_service passed to ActionManager: {discovery_service is not None}"
        )
        logger.debug(
            f"ActionCoordinator: memory_service passed to ActionManager: {memory_service is not None}"
        )
        try:
            # Type narrowing for Protocol compatibility
            state_manager_arg: object = state_manager
            state_service_arg: object = state_service
            async_job_manager_arg: object = async_job_manager
            event_bus_arg: object = event_bus
            orchestrator_ref_arg: object = orchestrator_ref
            unified_metadata_registry_arg: object = unified_metadata_registry

            self.action_manager = ActionManager(
                str(app_home),  # positional: APP_HOME
                plugin_manager,  # positional: plugin_manager
                state_manager_arg,  # type: ignore[arg-type]
                state_service=state_service_arg,  # type: ignore[arg-type]
                async_job_manager=async_job_manager_arg,  # type: ignore[arg-type]
                event_bus=event_bus_arg,  # type: ignore[arg-type]
                orchestrator=orchestrator_ref_arg,  # type: ignore[arg-type]
                unified_metadata_registry=unified_metadata_registry_arg,  # type: ignore[arg-type]
                discovery_service=discovery_service,  # type: ignore[arg-type]
                memory_service=memory_service,
                knowledge_service=knowledge_service,
            )
            logger.debug("ActionCoordinator: ActionManager initialized successfully")
        except Exception as e:
            logger.error(f"ActionCoordinator: Failed to initialize ActionManager: {e}")
            raise

    def _initialize_action_preparation_service(
        self,
        app_home: object,
        state_service: StateService,
        plugin_manager: PluginManager,
        unified_metadata_registry: object,
        discovery_service: object | None = None,
        memory_service: object | None = None,
        knowledge_service: object | None = None,
    ) -> None:
        """Initialize ActionPreparationService for template resolution."""
        logger.debug("ActionCoordinator: Initializing ActionPreparationService")
        logger.debug(
            f"ActionCoordinator: discovery_service passed to ActionPreparationService: {discovery_service is not None}"
        )
        logger.debug(
            f"ActionCoordinator: memory_service passed to ActionPreparationService: {memory_service is not None}"
        )
        try:
            plugin_manager_arg: object = plugin_manager
            self.action_preparation_service = ActionPreparationService(
                APP_HOME=str(app_home),
                state_service=state_service,
                action_manager=None,  # Will be set after
                discovery_service=discovery_service,  # type: ignore[arg-type]
                plugin_manager=plugin_manager_arg,  # type: ignore[arg-type]
                unified_metadata_registry=unified_metadata_registry,
                memory_service=memory_service,
                knowledge_service=knowledge_service,
            )
            self.action_preparation_service.action_manager = self.action_manager
            logger.debug("ActionCoordinator: ActionPreparationService initialized successfully")
        except Exception as e:
            logger.error(f"ActionCoordinator: Failed to initialize ActionPreparationService: {e}")
            raise

    def _initialize_process_registry(
        self,
        plugin_manager: PluginManager,
        state_service: StateService,
        state_manager: StateManager[dict[str, object]],
        discovery_service: object | None = None,
    ) -> None:
        """Initialize process registry with ProcessRegistryManager."""
        logger.debug("ActionCoordinator: Initializing process registry")
        from ananta.core.orchestration.managers.process_registry_manager import (
            ProcessRegistryManager,
        )
        from ananta.services.discovery_service import DiscoveryService

        manager = ProcessRegistryManager(
            plugin_manager=plugin_manager,
            state_service=state_service,
            state_manager=state_manager,
        )
        self._process_registry_manager = manager

        # Inject discovery_service if available so it can populate the vector database
        if discovery_service is not None:
            if isinstance(discovery_service, DiscoveryService):
                process_count_before = discovery_service.get_process_count()
                logger.debug(
                    f"ActionCoordinator: Discovery service has {process_count_before} processes before injection"
                )
                manager.set_discovery_service(discovery_service)
                logger.debug(
                    f"ActionCoordinator: Discovery service injected into ProcessRegistryManager (id={id(discovery_service)})"
                )
            else:
                logger.error(
                    f"ActionCoordinator: discovery_service is not a DiscoveryService instance: {type(discovery_service)}"
                )
        else:
            logger.error(
                "ActionCoordinator: No discovery_service provided - vector database will not be populated"
            )

        logger.debug("ActionCoordinator: Calling build_and_populate_registry()")
        manager.build_and_populate_registry(populate_discovery=False)

        # Verify discovery service was populated
        if discovery_service is not None and isinstance(discovery_service, DiscoveryService):
            process_count_after = discovery_service.get_process_count()
            logger.debug(
                f"ActionCoordinator: Discovery service has {process_count_after} processes after build_and_populate_registry"
            )
        registry_data = manager.get_registry_data()
        if registry_data is not None:
            self._process_registry = registry_data
        else:
            logger.error(
                "ActionCoordinator: Failed to get registry data from ProcessRegistryManager"
            )
            self._process_registry = {"processes": {}}

    def populate_discovery_after_registration(self) -> None:
        """Schedule the inference-dependent discovery rebuild after activation."""
        manager = self._process_registry_manager
        if manager is None:
            raise RuntimeError("Process registry manager unavailable after registration")
        threading.Thread(
            target=manager.populate_discovery_after_registration,
            name="post-registration-discovery-index",
            daemon=True,
        ).start()

    def _initialize_action_factory(
        self,
        state_service: StateService,
        action_recorder: object,
        state_manager: StateManager[dict[str, object]],
        app_home: str = "",
    ) -> None:
        """Initialize ActionFactory with template engine."""
        logger.debug("ActionCoordinator: Initializing ActionFactory")

        # Pass template engine from action_manager to enable complex template resolution
        template_engine = getattr(self.action_manager, "template_engine", None)
        logger.debug(f"ActionCoordinator: Template engine available: {template_engine is not None}")

        self.action_factory = ActionFactory(
            self._process_registry,
            template_engine,
            state_service,  # type: ignore[arg-type]
            action_recorder,  # type: ignore[arg-type]
            app_home=app_home,
        )
        state_manager.set_action_factory(self.action_factory)
        logger.debug("ActionCoordinator: ActionFactory initialized successfully")

    def _initialize_event_processor(
        self,
        event_handler_registry: object,
        state_service: StateService,
        state_manager: StateManager[dict[str, object]],
        action_recorder: object,
        session_manager: object,
        flow_manager: object,
    ) -> None:
        """Initialize EventProcessor with correlation managers and ActionFactory."""
        logger.debug("ActionCoordinator: Initializing EventProcessor")

        event_handler_registry_arg: object = event_handler_registry
        action_recorder_arg: object = action_recorder
        action_factory_arg: object = self.action_factory
        self.event_processor = EventProcessor(
            action_manager=self.action_manager,
            event_handler_registry=event_handler_registry_arg,  # type: ignore[arg-type]
            state_service=state_service,
            action_preparation_service=self.action_preparation_service,
            state_manager=state_manager,
            # action_recorder for update operations (completion, error)
            action_recorder=action_recorder_arg,  # type: ignore[arg-type]
            # action_factory for action creation (routes through ActionFactory validation)
            action_factory=action_factory_arg,  # type: ignore[arg-type]
            session_manager=session_manager,
            flow_manager=flow_manager,
        )

        logger.debug("ActionCoordinator: EventProcessor initialized successfully")

    def _inject_service_into_plugin(
        self,
        plugin: object,
        plugin_name: str,
        setter_name: str,
        service: object | None,
        service_name: str,
    ) -> None:
        """Inject a single service into a plugin if it has the setter method."""
        if service is None:
            return
        if not hasattr(plugin, setter_name):
            return
        getattr(plugin, setter_name)(service)
        logger.debug(f"ActionCoordinator: {service_name} injected into {plugin_name}")

    def _inject_all_services_into_plugin(
        self,
        plugin: object,
        plugin_name: str,
        state_service: StateService,
        blob_storage_service: object | None,
        embedding_service: object | None,
    ) -> None:
        """Inject all available services into a single plugin."""
        logger.debug(f"ActionCoordinator: Injecting services into plugin: {plugin_name}")

        self._inject_service_into_plugin(
            plugin, plugin_name, "set_state_service", state_service, "state_service"
        )
        self._inject_service_into_plugin(
            plugin,
            plugin_name,
            "set_blob_storage_service",
            blob_storage_service,
            "blob_storage_service",
        )
        self._inject_service_into_plugin(
            plugin, plugin_name, "set_embedding_service", embedding_service, "embedding_service"
        )
        self._inject_service_into_plugin(
            plugin, plugin_name, "set_action_factory", self.action_factory, "action_factory"
        )
        self._inject_service_into_plugin(
            plugin, plugin_name, "set_action_manager", self.action_manager, "action_manager"
        )

    def _inject_plugin_services(
        self,
        plugin_manager: PluginManager,
        state_service: StateService,
        orchestrator_ref: OrchestratorProtocol,
        blob_storage_service: object | None,
        embedding_service: object | None = None,
    ) -> None:
        """Inject services into plugins for action processing."""
        logger.debug("ActionCoordinator: Injecting services into plugins")
        try:
            for plugin_name, plugin in plugin_manager.plugins.items():
                self._inject_all_services_into_plugin(
                    plugin, plugin_name, state_service, blob_storage_service, embedding_service
                )
                if not hasattr(plugin, "submit_action_definition"):
                    self._inject_submit_action_definition(plugin, plugin_name, orchestrator_ref)

            self._plugins_ready = True
            logger.debug("ActionCoordinator: Plugin service injection complete - plugins ready")
        except Exception as e:
            logger.error(f"ActionCoordinator: Failed to inject services into plugins: {e}")
            raise

    def _inject_submit_action_definition(
        self, plugin: object, plugin_name: str, orchestrator_ref: OrchestratorProtocol
    ) -> None:
        """Inject submit_action_definition method into plugin."""
        logger.debug(
            f"ActionCoordinator: Injecting submit_action_definition method into {plugin_name}"
        )

        # Create a method that submits action definitions through ActionFactory → Database
        def submit_action_definition(
            action_definition: dict[str, object], context: dict[str, object] | None = None
        ) -> object:
            """Submit action definition through ActionFactory → Database (database-first approach)

            Args:
                action_definition: The action definition to submit
                context: Optional context for template substitution (e.g., conversation_history, session data)
            """
            try:
                logger.debug(
                    f"ActionCoordinator: Processing action definition: {action_definition.get('name')}"
                )
                logger.debug(f"ActionCoordinator: Context provided: {context is not None}")

                # Add session_id and flow_id for database correlation
                action_definition_with_correlation = action_definition.copy()
                # Type narrow: get attributes safely
                current_session_id = (
                    getattr(orchestrator_ref, "current_session_id", None)
                    if hasattr(orchestrator_ref, "current_session_id")
                    else None
                )
                current_flow_id = (
                    getattr(orchestrator_ref, "current_flow_id", None)
                    if hasattr(orchestrator_ref, "current_flow_id")
                    else None
                )
                action_definition_with_correlation["session_id"] = current_session_id
                action_definition_with_correlation["flow_id"] = current_flow_id

                notes_value = action_definition_with_correlation.get("notes")
                if not isinstance(notes_value, str) or not notes_value.strip():
                    process_key = action_definition_with_correlation.get("process_key", "unknown")
                    action_definition_with_correlation["notes"] = (
                        f"Coordinator submitting {process_key} on behalf of {plugin_name}."[
                            :NOTES_MAX_LENGTH
                        ]
                    )

                # Use ActionFactory.submit_action_definition() for database-first approach with context
                if self.action_factory is None:
                    raise RuntimeError(
                        "ActionFactory not initialized - ensure _initialize_action_system was called"
                    )
                # submit_action_definition returns action_id str and raises on failure
                action_id = self.action_factory.submit_action_definition(
                    action_definition_with_correlation, context
                )
                logger.debug(f"ActionCoordinator: Action stored in database with ID: {action_id}")

                # If action has result_processor, store it in database with action
                if "result_processor" in action_definition:
                    action_recorder = orchestrator_ref.action_recorder
                    if action_recorder is not None:
                        action_recorder.update_action_result(
                            action_id,
                            {"result_processor": action_definition["result_processor"]},
                        )
                        logger.debug(
                            f"ActionCoordinator: result_processor stored in database for action {action_id}"
                        )
                    else:
                        raise RuntimeError(
                            "ActionCoordinator: orchestrator_ref missing action_recorder attribute"
                        )

                return action_id

            except Exception as e:
                logger.error(f"ActionCoordinator: FAIL-FAST action storage failed: {e}")
                raise  # FAIL-FAST: No fallback mechanisms

        # Use setattr to dynamically add the method to the plugin object
        plugin.submit_action_definition = submit_action_definition  # type: ignore[attr-defined]
        logger.debug(
            f"ActionCoordinator: submit_action_definition method injected into {plugin_name}"
        )

    def _initialize_action_queue_poller(
        self,
        state_service: StateService,
        _event_bus: object,  # Reserved for interface compatibility
        flow_runtime_graph: object,
        memory_service: object | None = None,
        app_home: str = "",
        inference_service: object | None = None,
        blob_storage_service: object | None = None,
        discovery_service: object | None = None,
        io_interface_service: object | None = None,
    ) -> None:
        """Initialize ActionQueuePoller with ExecutionContext lifecycle and FRG token support."""
        logger.debug("ActionCoordinator: Initializing ActionQueuePoller")
        try:
            # Type narrow action_processor - it must be initialized at this point
            if self.action_processor is None:
                raise RuntimeError("ActionProcessor must be initialized before ActionQueuePoller")

            if flow_runtime_graph is None:
                raise RuntimeError("FlowRuntimeGraph is required for ActionQueuePoller")

            # Fail fast: action_factory is required for ActionQueuePoller
            if self.action_factory is None:
                raise RuntimeError("ActionFactory must be initialized before ActionQueuePoller")

            # Get inference model name for error routing (FAIL-FAST: required)
            inference_model_name: str | None = None
            if inference_service is not None:
                get_model_fn = getattr(inference_service, "get_configured_model_name", None)
                if callable(get_model_fn):
                    try:
                        result = get_model_fn()
                        inference_model_name = str(result) if result is not None else None
                        logger.debug(
                            f"ActionCoordinator: ActionQueuePoller using model: {inference_model_name}"
                        )
                    except Exception as e:
                        logger.warning(
                            f"ActionCoordinator: Could not get inference model for ActionQueuePoller: {e}"
                        )

            state_service_arg: object = state_service
            action_processor_arg: object = self.action_processor
            action_factory_arg: object = self.action_factory
            execution_context_manager_arg: object = self.execution_context_manager  # Phase 1

            # Get template_registry from action_processor for resolving <<<:...>>> template functions
            template_registry_arg: object | None = None
            if hasattr(self.action_processor, "template_registry"):
                template_registry_arg = self.action_processor.template_registry

            self.action_queue_poller = ActionQueuePoller(
                state_service=state_service_arg,  # type: ignore[arg-type]
                action_processor=action_processor_arg,  # type: ignore[arg-type]
                flow_runtime_graph=flow_runtime_graph,  # type: ignore[arg-type]
                action_factory=action_factory_arg,  # type: ignore[arg-type]
                execution_context_manager=execution_context_manager_arg,  # type: ignore[arg-type]
                template_registry=template_registry_arg,  # type: ignore[arg-type]
                memory_service=memory_service,  # type: ignore[arg-type]
                blob_storage_service=blob_storage_service,  # type: ignore[arg-type]
                discovery_service=discovery_service,  # type: ignore[arg-type]
                io_interface_service=io_interface_service,  # type: ignore[arg-type]
                app_home=app_home,
                poll_interval=1.0,  # Poll every second
                max_actions_per_poll=10,
                inference_model_name=inference_model_name,
            )
            logger.debug(
                "ActionCoordinator: ActionQueuePoller initialized with ExecutionContext and FRG"
            )
        except Exception as e:
            logger.error(f"ActionCoordinator: Failed to initialize ActionQueuePoller: {e}")
            raise

    def complete_database_initialization(self) -> None:
        """PHASE 3: Complete database operations after database is online."""
        logger.debug("ActionCoordinator: Starting Phase 3 database operations")

        # CRITICAL FIX: Persist process registry AFTER database is initialized
        # This fixes the race condition where Phase 2 tried to persist before database was ready
        # causing 80x "Database not initialized" errors
        if self._process_registry_manager:
            logger.debug("ActionCoordinator: Persisting process registry to database (Phase 3)")
            try:
                # Call the sync method directly since we're in a sync context
                # The async persist_registry() just wraps this method anyway
                self._process_registry_manager._do_process_registry_persistence()
                logger.debug(
                    "ActionCoordinator: Process registry persistence completed successfully"
                )
            except Exception as e:
                logger.error(
                    f"ActionCoordinator: Failed to persist process registry: {e}", exc_info=True
                )
        else:
            logger.error(
                "ActionCoordinator: No process registry manager available for persistence"
            )

        logger.debug("ActionCoordinator: Phase 3 database initialization completed")

    def apply_knowledge_base_updates(
        self, updates: dict[str, dict[str, object]],
    ) -> dict[str, object]:
        """Apply knowledge base text updates and sync all runtime references."""
        if self._process_registry_manager is None:
            msg = "Cannot apply knowledge base updates: process registry manager not initialized"
            raise RuntimeError(msg)
        result = self._process_registry_manager.apply_knowledge_base_updates(updates)
        # Sync runtime references
        updated = self._process_registry_manager.get_registry_data()
        if updated is not None:
            self._process_registry = updated
        if self.action_factory is not None:
            self.action_factory.update_process_registry(self._process_registry)
        if self.action_processor is not None:
            self.action_processor.process_registry = self._process_registry
        return result
