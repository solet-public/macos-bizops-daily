import logging
from pathlib import Path
from typing import Any, Protocol

from ananta.core.config.config_manager import ConfigManager, get_config
from ananta.core.domain.types import ActionResult
from ananta.core.plugins.plugin_manager import PluginManager
from ananta.core.state.async_job_manager import AsyncJobManager
from ananta.core.state.flow_runtime_graph import FlowRuntimeGraph
from ananta.core.state.state_manager import StateManager
from ananta.error_handling import FrameworkError
from ananta.services.blob_storage_service import BlobStorageService
from ananta.services.context_management import ContextManagementService
from ananta.services.embedding_service import EmbeddingService
from ananta.services.flow_service import FlowService
from ananta.services.inference_service import InferenceService
from ananta.services.job_service import JobService
from ananta.services.lifecycle_management_service import LifecycleManagementService
from ananta.services.scheduling_service import SchedulingService
from ananta.services.state_service import StateService
from ananta.services.vector_service import VectorService

logger = logging.getLogger(__name__)


class ServiceBindingsProtocol(Protocol):
    """Protocol for ServiceBindings to avoid circular imports."""

    def get_plugin_name(self, service_name: str) -> str | None: ...


class OrchestratorProtocol(Protocol):
    """Protocol for orchestrator reference with service resolution."""

    service_bindings: ServiceBindingsProtocol
    plugin_manager: PluginManager | None

    def get_service(self, service_name: str) -> object | None:
        """Get service instance by name."""
        ...


class EventBusProtocol(Protocol):
    """Protocol for event bus."""

    pass


class PlatformServicesManagerProtocol(Protocol):
    """Protocol for platform services manager."""

    def initialize(self) -> bool: ...
    def is_initialized(self) -> bool: ...
    @property
    def unified_metadata_registry(self) -> object | None: ...


class SystemPlatformManagerProtocol(Protocol):
    """Protocol for system platform manager."""

    def initialize_plugin_system(
        self, config: object, plugin_operational_config: dict[str, object], orchestrator_ref: object
    ) -> PluginManager: ...
    def get_plugin_lifecycle_manager(self) -> object: ...


class ServiceManager:
    """
    Manages initialization and configuration of all core services.
    Extracted from EventOrchestrator to reduce complexity.
    """

    def __init__(
        self,
        plugin_config: dict[str, dict[str, object]] | None = None,
        default_inference_provider: str | None = None,
        session_timeout_hours: int = 1,
        state_plugin_name: str | None = None,
    ):
        self.plugin_operational_config = plugin_config or {}
        self.default_inference_provider = default_inference_provider
        self._session_timeout_hours = session_timeout_hours
        self._state_plugin_name = state_plugin_name  # Explicit plugin assignment from launch script

        # Services to be initialized
        self.config: ConfigManager | None = None
        self.app_home: str | None = None
        self._system_platform_manager: SystemPlatformManagerProtocol | None = None
        self.plugin_manager: PluginManager | None = None
        self.state_service: StateService | None = None
        self.state_manager: StateManager[dict[str, object]] | None = None
        self.async_job_manager: AsyncJobManager | None = None
        self.blob_storage_service: BlobStorageService | None = None
        self.scheduling_service: SchedulingService | None = None
        self.job_service: object | None = None
        self.flow_service: object | None = None
        self.lifecycle_management_service: object | None = None
        self.context_management_service: ContextManagementService | None = None
        self.prompt_assembly_service: object | None = None
        self.plan_lifecycle_service: object | None = None
        self.wbs_lifecycle_service: object | None = None
        self.platform_services_manager: PlatformServicesManagerProtocol | None = None
        self.unified_metadata_registry: object | None = None

        # Correlation managers
        self.session_manager: object | None = None
        self.flow_manager: object | None = None
        self.action_recorder: object | None = None
        self.flow_runtime_graph: FlowRuntimeGraph | None = None

        # Services collection for legacy compatibility
        self.services_collection: dict[str, object] = {}

    def initialize_all_services(
        self, orchestrator_ref: OrchestratorProtocol, event_bus: EventBusProtocol
    ) -> None:
        """Initialize all services in the correct dependency order."""
        logger.debug("ServiceManager: Starting service initialization")

        # Store orchestrator reference for services that need it
        self._orchestrator_ref = orchestrator_ref

        self._initialize_configuration()
        self._initialize_plugin_system(orchestrator_ref)
        self._initialize_state_services()
        self._initialize_additional_services()
        self._initialize_correlation_managers()
        self._create_services_collection(event_bus)
        self._initialize_platform_services()

        logger.debug("ServiceManager: All services initialized successfully")

    def _initialize_configuration(self) -> None:
        """Initialize configuration with fail-fast error handling."""
        logger.debug("ServiceManager: Initializing configuration")
        try:
            self.config = get_config()
            self.app_home = self.config.APP_HOME
        except Exception as config_error:
            logger.error(f"ServiceManager: get_config() failed: {config_error}")
            raise FrameworkError(
                message=f"ServiceManager failed during config initialization: {config_error}",
                error_code="SERVICE_MANAGER_CONFIG_INIT_FAILED",
                details={
                    "original_error": str(config_error),
                    "initialization_phase": "_initialize_configuration",
                    "fix_required": "Call initialize_config() before ServiceManager creation",
                },
                original_error=config_error,
                severity="CRITICAL",
            ) from config_error

    def _initialize_plugin_system(self, orchestrator_ref: OrchestratorProtocol) -> None:
        """Initialize plugin system - use existing PluginManager from orchestrator.

        CRITICAL: The orchestrator already created a PluginManager during startup sequence.
        We MUST reuse that exact instance to ensure all plugins have services injected properly.
        Creating a new PluginManager here would create duplicate plugin instances that don't
        have services injected, causing failures when plugins try to use those services.
        """

        # Use the PluginManager that was already created by startup_sequence._init_plugin_manager()
        if (
            not hasattr(orchestrator_ref, "plugin_manager")
            or orchestrator_ref.plugin_manager is None
        ):
            raise RuntimeError(
                "PluginManager not found on orchestrator. "
                "Ensure startup_sequence._init_plugin_manager() runs before ServiceManager initialization."
            )

        self.plugin_manager = orchestrator_ref.plugin_manager

        # CRITICAL: Prepare all plugins for readiness BEFORE schema initialization
        # This ensures StateAwarePlugins like default_inference_plugin can initialize
        # their providers and return schema definitions
        from ananta.core.plugins.capabilities import is_lifecycle_managed, is_service_provider

        readiness_results = self.plugin_manager.prepare_all_plugins_for_readiness()

        # Check readiness excluding lifecycle-managed and service provider plugins
        unready_plugins = []
        for plugin_name, plugin in self.plugin_manager.plugins.items():
            # Skip lifecycle-managed plugins - they're started by startup sequence
            if is_lifecycle_managed(plugin):
                continue
            # Skip service providers - they're validated during service wrapper creation
            if is_service_provider(plugin):
                continue
            if not plugin.is_ready():
                unready_plugins.append(plugin_name)

        if unready_plugins:
            logger.error(f"ServiceManager: Not all non-service plugins ready: {unready_plugins}")
            raise RuntimeError(
                f"Not all non-service plugins are ready for operation: {unready_plugins}. "
                f"Readiness results: {readiness_results}"
            )
        logger.debug(
            "ServiceManager: All non-service plugins ready (service plugins will be started during runtime)"
        )

        if self.default_inference_provider:
            self._validate_inference_provider()

    def _validate_inference_provider(self) -> None:
        """Validate the default inference provider if specified."""
        # Add validation logic here if needed

    def _initialize_state_services(self) -> None:
        """Initialize state services.

        CRITICAL: We MUST reuse the existing StateService from the orchestrator.
        The orchestrator's StateService already has the SchemaManager set (from _initialize_schemas).
        Creating a new StateService would lose the SchemaManager reference, causing id_prefix
        lookups to fail with 'schema_manager is None' errors.
        """
        logger.debug("ServiceManager: Initializing state services")
        try:
            # CRITICAL: Reuse the existing StateService from orchestrator
            # It already has SchemaManager wired up from _initialize_schemas step
            existing_state_service = self._orchestrator_ref.get_service("state_service")
            if existing_state_service is None:
                raise ValueError(
                    "StateService not found on orchestrator. "
                    "Ensure startup_sequence._create_state_service_wrapper() runs before ServiceManager."
                )

            # Type-safe assignment - get_service returns object, we need StateService
            if not isinstance(existing_state_service, StateService):
                raise TypeError(
                    f"Expected StateService, got {type(existing_state_service).__name__}"
                )

            self.state_service = existing_state_service

            # Create adapter to match StateManagementInterface from StateService
            # StateService has compatible methods but different signature for set_key_value
            from ananta.interfaces.state_management_interface import StateManagementInterface

            # Create an adapter class inline to satisfy the interface
            class StateServiceAdapter(StateManagementInterface):
                """Adapter to make StateService compatible with StateManagementInterface."""

                def __init__(self, state_service: StateService) -> None:
                    self._state_service = state_service
                    # Copy bootstrap_mode attribute if it exists
                    bootstrap_attr = getattr(state_service, "bootstrap_mode", False)
                    if isinstance(bootstrap_attr, bool):
                        self.bootstrap_mode: bool = bootstrap_attr

                def create_schema(self, namespace: str, schema: dict[str, object]) -> ActionResult:
                    return self._state_service.create_schema(namespace, schema)

                def read_state(self, namespace: str, query: dict[str, object]) -> ActionResult:
                    return self._state_service.read_state(namespace, query)

                def write_state(self, namespace: str, data: dict[str, object]) -> ActionResult:
                    return self._state_service.write_state(namespace, data)

                def update_state(
                    self, namespace: str, query: dict[str, object], updates: dict[str, object]
                ) -> ActionResult:
                    return self._state_service.update_state(namespace, query, updates)

                def upsert_state(self, namespace: str, data: dict[str, object]) -> ActionResult:
                    return self._state_service.upsert_state(namespace, data)

                def acquire_lease(
                    self, namespace: str, data: dict[str, object]
                ) -> ActionResult:
                    return self._state_service.acquire_lease(namespace, data)

                def count(self, namespace: str, data: dict[str, object]) -> ActionResult:
                    return self._state_service.count(namespace, data)

                def max_value(self, namespace: str, data: dict[str, object]) -> ActionResult:
                    return self._state_service.max_value(namespace, data)

                def min_value(self, namespace: str, data: dict[str, object]) -> ActionResult:
                    return self._state_service.min_value(namespace, data)

                def delete_records(self, namespace: str, query: dict[str, object]) -> ActionResult:
                    return self._state_service.delete_records(namespace, query)

                def query_state(self, namespace: str, filters: dict[str, object]) -> ActionResult:
                    return self._state_service.query_state(namespace, filters)

                def query_ordered(
                    self, namespace: str, data: dict[str, object]
                ) -> ActionResult:
                    return self._state_service.query_ordered(namespace, data)

                def set_key_value(
                    self,
                    namespace: str,
                    key: str,
                    value: object,
                    scope: str = "GLOBAL",
                    ttl: int | None = None,
                ) -> ActionResult:
                    # Adapt the value parameter - StateService expects specific types
                    # Use isinstance checks for type narrowing
                    if isinstance(value, str):
                        return self._state_service.set_key_value(namespace, key, value, scope, ttl)
                    elif isinstance(value, int):
                        return self._state_service.set_key_value(namespace, key, value, scope, ttl)
                    elif isinstance(value, float):
                        return self._state_service.set_key_value(namespace, key, value, scope, ttl)
                    elif isinstance(value, bool):
                        return self._state_service.set_key_value(namespace, key, value, scope, ttl)
                    elif isinstance(value, dict):
                        return self._state_service.set_key_value(namespace, key, value, scope, ttl)
                    elif isinstance(value, list):
                        return self._state_service.set_key_value(namespace, key, value, scope, ttl)
                    elif value is None:
                        return self._state_service.set_key_value(namespace, key, value, scope, ttl)
                    else:
                        raise ValueError(f"Unsupported value type: {type(value)}")

                def get_key_value(
                    self, namespace: str, key: str, scope: str = "GLOBAL"
                ) -> ActionResult:
                    return self._state_service.get_key_value(namespace, key, scope)

                def delete_key_value(
                    self, namespace: str, key: str, scope: str = "GLOBAL"
                ) -> ActionResult:
                    return self._state_service.delete_key_value(namespace, key, scope)

                def clear_key_values(
                    self, namespace: str | None = None, scope: str | None = None
                ) -> ActionResult:
                    return self._state_service.clear_key_values(namespace, scope)

                def list_key_values(
                    self,
                    namespace: str | None = None,
                    scope: str | None = None,
                    pattern: str | None = None,
                ) -> ActionResult:
                    return self._state_service.list_key_values(namespace, scope, pattern)

                def execute_sql(
                    self, sql_query: str, sql_params: list[object] | None = None
                ) -> ActionResult:
                    return self._state_service.execute_sql(sql_query, sql_params)

                def transactional(self) -> Any:
                    # Delegate to the underlying state service if it has
                    # the method (PostgresStatePlugin does); otherwise raise.
                    fn = getattr(self._state_service, "transactional", None)
                    if fn is None:
                        raise NotImplementedError(
                            "Underlying state service does not provide a transactional() context manager",
                        )
                    return fn()

                def describe_schema(self, namespace: str) -> ActionResult:
                    return self._state_service.describe_schema(namespace)

                def list_namespaces(self) -> ActionResult:
                    return self._state_service.list_namespaces()

                def mark_as_read(self, namespace: str, query: dict[str, object]) -> ActionResult:
                    return self._state_service.mark_as_read(namespace, query)

                def initialize_database(
                    self, config: dict[str, object] | None = None
                ) -> ActionResult:
                    return self._state_service.initialize_database(config)

                def is_ready(self) -> bool:
                    """Check if the state service is ready for use."""
                    # Delegate to underlying state service if it has the method
                    if hasattr(self._state_service, "is_ready"):
                        return self._state_service.is_ready()  # type: ignore[no-any-return]
                    # Otherwise assume ready if service is initialized
                    return True

                def get_readiness_error(self) -> str | None:
                    """Get the error message if not ready, None if ready."""
                    # Delegate to underlying state service if it has the method
                    if hasattr(self._state_service, "get_readiness_error"):
                        return self._state_service.readiness_error  # type: ignore[no-any-return]
                    # Otherwise return None (ready)
                    return None

            adapter = StateServiceAdapter(self.state_service)
            self.state_manager = StateManager(adapter)

            logger.debug("ServiceManager: State services initialized successfully")
        except Exception as e:
            logger.error(f"ServiceManager: Failed to initialize state services: {e}")
            raise

    def _initialize_additional_services(self) -> None:
        """Initialize additional services like job manager and file service."""
        logger.debug("ServiceManager: Initializing additional services")
        try:
            if not self.state_service:
                raise ValueError("State service must be initialized before additional services")

            # StateService implements StateServiceProtocol via structural typing
            self.async_job_manager = AsyncJobManager(self.state_service)

            if not self.app_home:
                raise ValueError("APP_HOME must be set before initializing blob storage service")
            if not self.plugin_manager:
                raise ValueError("Plugin manager must be initialized before blob storage service")
            from ananta.core.orchestration.service_bindings import ServiceName as _ServiceName

            blob_plugin_name = self._orchestrator_ref.service_bindings.get_plugin_name(
                _ServiceName.BLOB_STORAGE_SERVICE
            )
            self.blob_storage_service = BlobStorageService(
                plugin_manager=self.plugin_manager,
                blob_storage_plugin_name=blob_plugin_name,
                app_home=self.app_home,
            )

            # Initialize job service
            # AsyncJobManager (constructed above) is passed so the staleness
            # sweep can terminate a dead job through the manager's own
            # update_job path — completion routing + FRG token resolution
            # included — instead of writing a terminal status behind its back.
            self.job_service = JobService(self.state_service, self.async_job_manager)

            # Initialize lifecycle management service
            self.lifecycle_management_service = LifecycleManagementService(
                orchestrator_ref=self._orchestrator_ref
            )
            logger.debug("ServiceManager: LifecycleManagementService initialized")

            # Initialize inference service (wraps default_inference_plugin)
            # Plugin names resolved via service bindings in orchestrator
            from ananta.core.orchestration.service_bindings import ServiceName

            service_bindings = self._orchestrator_ref.service_bindings

            inference_plugin_name = service_bindings.get_plugin_name(ServiceName.INFERENCE_SERVICE)
            self.inference_service = InferenceService(
                plugin_manager=self.plugin_manager,
                inference_plugin_name=inference_plugin_name,
                app_home=self.app_home,
                state_service=self.state_service,
                orchestrator=self._orchestrator_ref,
            )
            logger.debug(
                f"ServiceManager: InferenceService initialized (plugin: {inference_plugin_name})"
            )

            # PromptAssemblyService is the same InferenceService instance —
            # it implements PromptAssemblyServiceInterface via assemble_prompt().
            self.prompt_assembly_service = self.inference_service

            # Lifecycle services delegate to the thinking plugin (Slice 11A
            # transitional — the thinking plugin structurally satisfies both
            # PlanLifecycleServiceInterface and WbsLifecycleServiceInterface).
            # Resolved lazily since the thinking plugin may not be ready yet.
            thinking_plugin_name = service_bindings.get_plugin_name(
                ServiceName.THINKING_SERVICE,
            )
            if thinking_plugin_name and self.plugin_manager:
                thinking_plugin = self.plugin_manager.get_plugin(thinking_plugin_name)
                self.plan_lifecycle_service = thinking_plugin
                self.wbs_lifecycle_service = thinking_plugin
                logger.debug(
                    "ServiceManager: Lifecycle services bound to %s",
                    thinking_plugin_name,
                )

            # Initialize embedding service (wraps local_embeddings_plugin)
            embedding_plugin_name = service_bindings.get_plugin_name(ServiceName.EMBEDDING_SERVICE)
            self.embedding_service = EmbeddingService(
                plugin_manager=self.plugin_manager, embedding_plugin_name=embedding_plugin_name
            )
            logger.debug(
                f"ServiceManager: EmbeddingService initialized (plugin: {embedding_plugin_name})"
            )

            # Initialize vector service (wraps pgvector_service_plugin, requires state_service)
            vector_plugin_name = service_bindings.get_plugin_name(ServiceName.VECTOR_SERVICE)
            self.vector_service = VectorService(
                plugin_manager=self.plugin_manager,
                vector_plugin_name=vector_plugin_name,
                state_service=self.state_service,
            )
            logger.debug(f"ServiceManager: VectorService initialized (plugin: {vector_plugin_name})")

            self.scheduling_service = SchedulingService(
                plugin_manager=self.plugin_manager,
                app_home=self.app_home,
            )
            logger.debug("ServiceManager: SchedulingService initialized")

            # Initialize context management service with shared content storage
            self.context_management_service = ContextManagementService(
                state_service=self.state_service,
                app_home=self.app_home,
            )
            logger.debug("ServiceManager: ContextManagementService initialized")

            # Wire inference service to context management for compaction
            # InferenceService inherits from InferenceServiceInterface, satisfying the type contract.
            # InferenceService handles lazy initialization via _ensure_ready() on first use.
            if self.inference_service:
                self.context_management_service.set_inference_service(self.inference_service)
                logger.debug("ServiceManager: InferenceService wired to ContextManagementService")

            logger.debug("ServiceManager: Additional services initialized")
        except Exception as e:
            logger.error(f"ServiceManager: Failed to initialize additional services: {e}")
            raise

    def _initialize_correlation_managers(self) -> None:
        """Initialize correlation managers for session and flow management."""
        logger.debug("ServiceManager: Initializing correlation managers")
        try:
            from ananta.core.orchestration.managers.action_event_recorder import (
                ActionEventRecorder,
            )
            from ananta.core.orchestration.managers.flow_manager import FlowManager
            from ananta.core.orchestration.managers.session_manager import (
                SessionManager,
            )

            if not self.state_service:
                raise ValueError("State service must be initialized before correlation managers")
            # Convert hours to minutes for backward compatibility with existing config
            # Default is 1 hour (60 minutes) but can be overridden
            timeout_minutes = self._session_timeout_hours * 60
            self.session_manager = SessionManager(
                self.state_service, session_timeout_minutes=timeout_minutes
            )
            self.flow_manager = FlowManager(self.state_service)

            # Initialize FlowRuntimeGraph for FRG token management
            self.flow_runtime_graph = FlowRuntimeGraph(self.state_service)
            logger.debug("ServiceManager: FlowRuntimeGraph initialized")

            # Wire FlowRuntimeGraph to AsyncJobManager for job token resolution
            if self.async_job_manager:
                self.async_job_manager._flow_runtime_graph = self.flow_runtime_graph

            # ActionEventRecorder now requires FlowRuntimeGraph for token creation
            self.action_recorder = ActionEventRecorder(
                self.state_service, self.flow_manager, self.flow_runtime_graph
            )

            # Initialize flow service (requires FlowManager)
            if self.flow_manager:
                self.flow_service = FlowService(self.flow_manager, self.state_service)
                logger.debug("ServiceManager: FlowService initialized")
            else:
                raise ValueError("FlowManager not available for flow_service initialization")

            logger.debug("ServiceManager: Correlation managers initialized successfully")
        except Exception as e:
            logger.error(f"ServiceManager: Failed to initialize correlation managers: {e}")
            raise

    def _create_services_collection(self, event_bus: EventBusProtocol) -> None:
        """Create services collection for legacy compatibility."""
        self.services_collection = {
            "state_service": self.state_service,
            "blob_storage_service": self.blob_storage_service,
            "scheduling_service": self.scheduling_service,
            "event_bus": event_bus,
        }

    def _initialize_platform_services(self) -> None:
        """Initialize platform services."""
        logger.debug("ServiceManager: Initializing platform services")
        try:
            from ananta.platform import PlatformServicesManager

            if not self.app_home:
                raise ValueError("APP_HOME must be set before initializing platform services")
            app_home_path = Path(self.app_home)
            metadata_folder = str(app_home_path / "metadata")
            output_folder = str(app_home_path / "generated")

            # PlatformServicesManager structurally matches PlatformServicesManagerProtocol
            platform_manager_instance = PlatformServicesManager(metadata_folder, output_folder)

            # Create an adapter class to match the protocol
            class PlatformServicesAdapter:
                """Adapter for PlatformServicesManager to match protocol."""

                def __init__(self, manager: PlatformServicesManager) -> None:
                    self._manager = manager

                def initialize(self) -> bool:
                    return self._manager.initialize()

                def is_initialized(self) -> bool:
                    return self._manager.is_initialized()

                @property
                def unified_metadata_registry(self) -> object | None:
                    return self._manager.unified_metadata_registry

            # Create adapter and initialize platform services
            adapter = PlatformServicesAdapter(platform_manager_instance)
            self.platform_services_manager = adapter

            platform_init_success = self.platform_services_manager.initialize()

            if platform_init_success and self.platform_services_manager.is_initialized():
                self.unified_metadata_registry = (
                    self.platform_services_manager.unified_metadata_registry
                )
                logger.debug("ServiceManager: Platform services initialized with metadata registry")
            else:
                logger.error("ServiceManager: Platform services initialization failed")
        except Exception as e:
            logger.error(f"ServiceManager: Failed to initialize platform services: {e}")
            raise
