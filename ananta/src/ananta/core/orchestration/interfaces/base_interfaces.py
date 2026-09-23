from abc import ABC, abstractmethod

from ananta.core.plugins.plugin_contracts import ActionStatus


class ISessionManager(ABC):
    @abstractmethod
    def create_session(
        self, namespace: str, context_type: str, metadata: dict[str, object] | None = None
    ) -> str:
        pass

    @abstractmethod
    def get_session_metadata(self, session_id: str) -> dict[str, object]:
        pass

    @abstractmethod
    def validate_session(self, session_id: str) -> bool:
        pass

    @abstractmethod
    def cleanup_expired_sessions(self) -> int:
        pass

    @abstractmethod
    def update_session_activity(self, session_id: str) -> bool:
        """Update last_activity and extend expires_at for a session."""
        pass

    @abstractmethod
    def get_active_session_for_namespace(self, namespace: str) -> str | None:
        """Find an active session for the given namespace."""
        pass


class IFlowManager(ABC):
    @abstractmethod
    def create_flow(
        self, session_id: str, trigger_type: str, trigger_data: dict[str, object], priority: int = 5
    ) -> str:
        pass

    @abstractmethod
    def get_flow_trigger_data(self, flow_id: str) -> dict[str, object]:
        pass

    @abstractmethod
    def get_next_sequence_in_flow(self, flow_id: str) -> int:
        pass

    @abstractmethod
    def validate_action_name_uniqueness(self, action: dict[str, object]) -> None:
        pass


class IActionQueueManager(ABC):
    @abstractmethod
    async def get_next_pending_action(
        self, state: dict[str, object]
    ) -> tuple[dict[str, object] | None, int | None]:
        pass

    @abstractmethod
    async def update_action_status(
        self, state: dict[str, object], action_index: int, status: ActionStatus
    ) -> None:
        pass

    @abstractmethod
    async def set_action_to_processing(self, state: dict[str, object], action_index: int) -> None:
        pass

    @abstractmethod
    async def process_action_queue(self, state: dict[str, object]) -> dict[str, object]:
        pass


class IActionEventRecorder(ABC):
    @abstractmethod
    def store_action_event(self, action: dict[str, object]) -> str:
        pass

    @abstractmethod
    def update_action_completion(self, action_id: str, result: dict[str, object]) -> None:
        pass

    @abstractmethod
    def update_action_result(self, action_id: str, result: dict[str, object]) -> None:
        pass

    @abstractmethod
    def update_action_error(self, action_id: str, error_message: str) -> None:
        pass

    @abstractmethod
    def calculate_action_depth(self, parent_id: str | None) -> int:
        pass


class IPluginLifecycleManager(ABC):
    @abstractmethod
    def validate_inference_provider(self, provider_name: str, plugin_manager: object) -> None:
        pass

    @abstractmethod
    async def initialize_plugins(self, plugin_manager: object) -> None:
        """Initialize all plugins. Call once at startup."""
        pass

    @abstractmethod
    def verify_plugins_ready(self) -> None:
        """Verify all plugins are ready. Fails fast if not initialized."""
        pass

    @abstractmethod
    async def initialize_plugin_schemas(
        self, plugin_manager: object, schema_manager: object
    ) -> None:
        pass

    @abstractmethod
    def discover_and_initialize_plugins(
        self,
        plugin_manager: object,
        orchestrator_ref: object,
        config_manager: object | None = None,
    ) -> None:
        """Discover with the orchestrator profile manifest as the allowlist.

        Implementations must derive the allowed plugin set from APP_HOME and
        pass it to PluginManager discovery. Omitting the allowlist loads every
        installed entry point and repeats the 2026-05-31 boot incident.
        """
        pass

    @abstractmethod
    def configure_plugin_operational_config(
        self, config: object, plugin_operational_config: dict[str, object]
    ) -> None:
        pass

    @abstractmethod
    def inject_plugin_services(self, service_injector: object, plugin_manager: object) -> None:
        pass

    @abstractmethod
    def setup_plugin_event_bus(self, plugin_manager: object, event_bus: object) -> None:
        pass


class ISystemPlatformManager(ABC):
    @abstractmethod
    def initialize_plugin_system(
        self, config: object, plugin_operational_config: dict[str, object], orchestrator_ref: object
    ) -> object:
        pass

    @abstractmethod
    def get_plugin_manager(self) -> object:
        pass

    @abstractmethod
    def get_plugin_lifecycle_manager(self) -> object:
        pass


class IRuntimePlatformManager(ABC):
    @abstractmethod
    async def execute_action(
        self, state: dict[str, object], action: dict[str, object]
    ) -> dict[str, object]:
        pass

    @abstractmethod
    async def execute_framework_action(
        self, action_name: str, action_parameters: dict[str, object], process_key: str
    ) -> dict[str, object]:
        pass

    @abstractmethod
    async def execute_service_management_action(
        self, function_name: str, parameters: dict[str, object]
    ) -> dict[str, object]:
        pass

    @abstractmethod
    async def process_single_action(
        self, state: dict[str, object], action: dict[str, object], action_index: int
    ) -> tuple[dict[str, object], bool, bool]:
        pass

    @abstractmethod
    async def process_action_queue(self, state: dict[str, object]) -> dict[str, object]:
        pass

    @abstractmethod
    async def process_actions(self) -> dict[str, object] | None:
        pass

    @abstractmethod
    async def run_orchestrator(self) -> None:
        pass
