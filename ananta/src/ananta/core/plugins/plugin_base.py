"""Plugin base class — slim lifecycle + service-injection surface.

Decomposed during Step 9.B
(design record, Step 9.2, dev-checkout workbench — not part of the shipped tree):

  - Action discovery → `action_discovery.py` (module-level functions).
  - Action execution → `action_execution.py` (module-level functions).
  - Template-function capability → `template_function_provider.py`
    (opt-in mixin; plugins that DO support template functions inherit it).
  - Validator-hook capability → `validation_hook_provider.py` (opt-in mixin).

What stays here:
  - Lifecycle state (`PluginReadiness` + `is_ready` / `set_ready` /
    `set_error` / `initialize` / `prepare_for_readiness`).
  - Service-injection setters (`set_orchestrator_ref` / `set_event_bus` /
    `set_action_factory` / `set_config_provider` / `set_validation_registry`)
    — these are called by `_setup_plugin_context` via `hasattr`/`setattr`
    and are part of the protocol surface.
  - Configuration introspection (`get_default_config`, `get_config_schema`).
  - Action lifecycle delegates (`execute`, `get_available_actions`) — one
    line each, forwarding to the extracted modules.
  - `_execute_action` (abstract method subclasses override).
  - `_resolve_io_process_key` / `_process_follow_ups` (small private helpers
    kept here per the design doc).
  - `is_service_plugin: ClassVar[bool]` (replaces the old method form).

Renamed attributes (private → public) — the rename was swept across
`plugins/` and `ananta/` in the same commit; callers now use direct
attribute access instead of the previously dropped getters:
  `config_provider`, `orchestrator_ref`, `event_bus`,
  `readiness_state`, `readiness_error`, `validation_registry`.
"""

from __future__ import annotations

from collections.abc import Sequence
from enum import Enum
from typing import TYPE_CHECKING, Any, ClassVar, Protocol, TypeVar

from ananta.constants import CONTEXT_KEY_FLOW_ID
from ananta.core.actions.action_metadata import ActionMetadata
from ananta.core.config.config_provider import ConfigProvider
from ananta.core.domain.types import ActionResult
from ananta.core.plugins import action_discovery, action_execution
from ananta.core.plugins.plugin_validation import PluginValidationRegistry

if TYPE_CHECKING:
    from ananta.core.actions.action_factory import ActionFactory

T = TypeVar("T")


class ServiceBindingsProtocol(Protocol):
    """Protocol for ServiceBindings to avoid circular imports."""

    def is_plugin_bound_to_service(self, plugin_name: str) -> bool: ...
    def get_services_for_plugin(self, plugin_name: str) -> Sequence[str]: ...
    def get_plugin_name(self, service_name: str) -> str | None: ...


class OrchestratorProtocol(Protocol):
    """Protocol for Orchestrator to avoid circular imports."""

    service_bindings: ServiceBindingsProtocol

    @property
    def APP_HOME(self) -> str:  # noqa: N802
        """Application home directory."""
        ...

    @property
    def async_job_manager(self) -> object: ...

    def get_service(self, service_name: str) -> object | None:
        """Get service instance by name."""
        ...

    def get_process_registry(self) -> dict[str, object]:
        """Return the current process registry data."""
        ...

    def apply_knowledge_base_updates(
        self, updates: dict[str, dict[str, object]],
    ) -> dict[str, object]:
        """Apply knowledge base text updates to the process registry at runtime."""
        ...


class EventBusProtocol(Protocol):
    """Protocol for EventBus to avoid circular imports."""

    def publish(self, event: object) -> bool: ...


class PluginReadiness(Enum):
    UNINITIALIZED = "uninitialized"
    READY = "ready"
    ERROR = "error"


class PluginBase:
    # Capability flag — class attribute (was previously an `is_service_plugin()`
    # method returning False). `ServicePlugin` overrides to True.
    is_service_plugin: ClassVar[bool] = False

    def __init__(self) -> None:
        self.name: str = self.__class__.__name__
        self._current_action_name: str = "unknown"
        self.config_provider: ConfigProvider | None = None
        self.orchestrator_ref: OrchestratorProtocol | None = None
        self.event_bus: EventBusProtocol | None = None
        self.validation_registry: PluginValidationRegistry | None = None
        self.action_factory: ActionFactory | None = None
        self.readiness_state: PluginReadiness = PluginReadiness.UNINITIALIZED
        self.readiness_error: str | None = None

    # ------------------------------------------------------------------
    # Readiness lifecycle
    # ------------------------------------------------------------------

    def is_ready(self) -> bool:
        """Check if plugin is ready for action processing."""
        return self.readiness_state == PluginReadiness.READY

    def set_ready(self) -> None:
        """Mark plugin as ready for action processing."""
        self.readiness_state = PluginReadiness.READY
        self.readiness_error = None

    def set_error(self, error_message: str) -> None:
        """Mark plugin as having an error."""
        self.readiness_state = PluginReadiness.ERROR
        self.readiness_error = error_message

    def get_readiness_error(self) -> str | None:
        return self.readiness_error

    def prepare_for_readiness(self) -> None:
        pass

    def initialize(self, config: dict[str, object]) -> None:
        """Initialize the plugin with configuration.

        Called after plugin instantiation with plugin-specific configuration.
        Subclasses can override to perform initialization based on config.

        Args:
            config: Plugin configuration dictionary.
        """
        pass

    # ------------------------------------------------------------------
    # Action lifecycle — slim delegates to action_discovery / action_execution
    # ------------------------------------------------------------------

    async def execute(
        self, action_name: str, parameters: dict[str, object]
    ) -> dict[str, object]:
        """Run an action by name. Delegates to `action_execution.execute`."""
        return await action_execution.execute(self, action_name, parameters)

    def get_available_actions(self) -> list[ActionMetadata]:
        """Discover all @platform_process actions on this plugin.

        Delegates to `action_discovery.discover_actions`.
        """
        return action_discovery.discover_actions(self)

    def _execute_action(
        self,
        params: dict[str, object],
        state: dict[str, object],
        APP_HOME: str,
        plugin_config: dict[str, object],
    ) -> dict[str, object]:
        """Execute an action with the given parameters.

        This is the internal method called by PluginManager.execute_action().
        Subclasses should override this to implement action execution.

        Raises:
            NotImplementedError: If subclass does not implement this method.
        """
        raise NotImplementedError(f"Plugin {self.name} must implement _execute_action method")

    # ------------------------------------------------------------------
    # Configuration
    # ------------------------------------------------------------------

    def get_default_config(self) -> dict[str, object]:
        return {
            "name": self.name,
            "version": "0.1.0",
            "enabled": True,
            "log_level": "info",
            "timeout": 30,
            "retry_count": 3,
        }

    def get_config_schema(self) -> dict[str, object]:
        """Declare configuration schema for this plugin.

        Override this method to declare the plugin's configuration schema.
        The schema follows JSON Schema draft-07 format and is used by the
        setup flow to:
        - Generate setup UI/prompts automatically
        - Validate user input before saving
        - Provide defaults and descriptions

        Custom extensions:
        - "x-secret": true - Indicates a sensitive value (mask in UI, don't log)
        - "x-group": "string" - Group related settings in UI
        - "x-order": int - Display order within group

        Returns:
            JSON Schema dict defining config keys, types, defaults, descriptions.
            Empty dict means no declared configuration (plugin uses hardcoded defaults
            or doesn't require configuration).
        """
        return {}

    # ------------------------------------------------------------------
    # Service injection setters — called by _setup_plugin_context via
    # hasattr/setattr at plugin wire-up time. Keep all five.
    # ------------------------------------------------------------------

    def set_orchestrator_ref(self, orchestrator: OrchestratorProtocol) -> None:
        self.orchestrator_ref = orchestrator

    def set_event_bus(self, event_bus: EventBusProtocol) -> None:
        self.event_bus = event_bus

    def set_action_factory(self, action_factory: ActionFactory) -> None:
        """Inject ActionFactory for action submission."""
        self.action_factory = action_factory

    def set_config_provider(self, config_provider: ConfigProvider) -> None:
        self.config_provider = config_provider

    def set_validation_registry(self, registry: PluginValidationRegistry) -> None:
        """Register this plugin's parameter + action validators with the registry.

        Validator hooks are produced via `get_parameter_validators` /
        `get_action_validators` on the optional `ValidationHookProvider`
        mixin; plugins that don't inherit the mixin fall back to no-op
        defaults via `getattr` with empty-dict defaults.
        """
        if not self.validation_registry:
            self.validation_registry = registry

        # Import here to avoid circular imports
        from ananta.core.plugins.plugin_validation import PluginValidationHook, ValidationPhase

        # Register parameter validators (no-op if the plugin doesn't inherit
        # ValidationHookProvider — getattr returns empty dicts).
        parameter_validators = getattr(self, "get_parameter_validators", lambda: {})()
        for action_name, validator in parameter_validators.items():
            hook = PluginValidationHook(
                plugin_name=self.name,
                action_name=action_name,
                validator_function=validator,
                validation_phase=ValidationPhase.PARAMETER,
                description=f"Parameter validation for {action_name}",
            )
            self.validation_registry.register_validator(hook)

        # Register action validators
        action_validators = getattr(self, "get_action_validators", lambda: {})()
        for action_name, validator in action_validators.items():
            hook = PluginValidationHook(
                plugin_name=self.name,
                action_name=action_name,
                validator_function=validator,
                validation_phase=ValidationPhase.FINAL,
                description=f"Action validation for {action_name}",
            )
            self.validation_registry.register_validator(hook)

    # ------------------------------------------------------------------
    # Action-result follow-up plumbing
    # ------------------------------------------------------------------

    def _process_follow_ups(
        self, results: dict[str, object], result_processor: dict[str, object] | None = None
    ) -> None:
        """
        Standard fail-fast processing of actions array and result_processor templates.
        Can be overridden by subclasses if custom behavior needed.

        Args:
            results: Plugin method results containing potential 'actions' array
            result_processor: Template for follow-up action generation with variable substitution

        Raises:
            ValueError: If results structure is invalid or action missing flow_id
            RuntimeError: If action submission fails
        """
        # Process inference-generated actions array
        if "actions" in results:
            if not isinstance(results["actions"], list):
                raise ValueError(f"results['actions'] must be list, got {type(results['actions'])}")

            for i, action_def in enumerate(results["actions"]):
                # Fail fast: flow_id is required for all actions
                if not isinstance(action_def, dict):
                    raise ValueError(f"Action {i} must be dict, got {type(action_def)}")
                if not action_def.get(CONTEXT_KEY_FLOW_ID):
                    raise ValueError(
                        f"Action {i} missing {CONTEXT_KEY_FLOW_ID} - all actions require flow context"
                    )
                if not self.action_factory:
                    raise RuntimeError("ActionFactory not available for action submission")
                self.action_factory.submit_action_definition(action_def)

        # Process result_processor template
        if result_processor:
            if not self.action_factory:
                raise RuntimeError("ActionFactory not available for template submission")
            self.action_factory.submit_result_with_template(results, result_processor)

    # ------------------------------------------------------------------
    # IO-process-key resolution helper
    # ------------------------------------------------------------------

    def _resolve_io_process_key(self, state: dict[str, object]) -> str:
        """Resolve the active IO plugin's post_message process key from flow trigger_data.

        Looks up the flow's source_namespace (set by the originating IO plugin) and
        constructs the plugin-addressed process key for post_message.

        Args:
            state: Runtime state dict containing flow_id.

        Returns:
            Process key like ``plugin::<namespace>::post_message``.

        Raises:
            RuntimeError: If flow_id, orchestrator, flow_service, or
                source_namespace is unavailable.
        """
        flow_id = state.get(CONTEXT_KEY_FLOW_ID)
        if not flow_id:
            raise RuntimeError("Cannot resolve IO process key: no flow_id in state")

        if not self.orchestrator_ref:
            raise RuntimeError("Cannot resolve IO process key: no orchestrator reference")

        flow_service: Any = self.orchestrator_ref.get_service("flow_service")
        if flow_service is None:
            raise RuntimeError("Cannot resolve IO process key: flow_service not available")
        result = flow_service.get_flow_input(str(flow_id))
        source_namespace: str = result["data"]["result"].get("source_namespace", "")
        if not source_namespace:
            raise RuntimeError(
                f"Empty source_namespace in flow trigger_data for flow {flow_id}"
            )
        return f"plugin::{source_namespace}::post_message"


class ServicePlugin(PluginBase):
    """Base class for plugins requiring lifecycle management.

    Contract:
    1. IDEMPOTENCY: start/stop must be safe to call multiple times
    2. INTERFACE AWARENESS: Track which interfaces plugin supports
    3. ACTIONRESULT: Return ActionResult, not exceptions
    4. CLEANUP: Release all resources on stop

    All lifecycle methods (start_services, stop_services) MUST return ActionResult
    to enable proper error routing and handling through the platform.
    """

    # Override the base ClassVar — every ServicePlugin subclass is a service plugin.
    is_service_plugin: ClassVar[bool] = True

    def __init__(self) -> None:
        super().__init__()
        # Lifecycle state tracking
        self._services_started: bool = False
        self._service_started_at: str | None = None
        self._service_error: str | None = None
        # Interface provider tracking
        self._supporting_interfaces: set[str] = set()

    # Interface awareness methods
    def set_as_active_provider(self, interface_name: str) -> None:
        """Services call this when selecting plugin as interface provider.

        Args:
            interface_name: Name of the interface this plugin is providing
        """
        self._supporting_interfaces.add(interface_name)

    def unset_as_active_provider(self, interface_name: str) -> None:
        """Services call this when switching to different provider.

        Args:
            interface_name: Name of the interface to remove
        """
        self._supporting_interfaces.discard(interface_name)

    def is_active_interface_provider(self) -> bool:
        """Check if plugin currently supports any active interfaces.

        Returns:
            True if plugin provides at least one interface
        """
        return len(self._supporting_interfaces) > 0

    def get_supported_interfaces(self) -> set[str]:
        """Get set of interfaces currently being supported.

        Returns:
            Copy of supported interfaces set
        """
        return self._supporting_interfaces.copy()

    # Lifecycle methods (MUST return ActionResult)
    async def start_services(self) -> ActionResult:
        """Start services. MUST be idempotent. MUST return ActionResult.

        Implementations MUST:
        - Check _services_started and return early if already started
        - Return ActionResult with action_status='completed' on success
        - Return ActionResult with action_status='error' on failure
        - Set _services_started=True only after successful startup

        Returns:
            ActionResult with status and optional error details
        """
        raise NotImplementedError(f"Service plugin {self.name} must implement start_services()")

    async def stop_services(self) -> ActionResult:
        """Stop services. MUST check is_active_interface_provider(). MUST return ActionResult.

        Implementations MUST:
        - Check _services_started and return early if already stopped
        - Check is_active_interface_provider() and refuse to stop if True
        - Return ActionResult with action_status='completed' on success
        - Return ActionResult with action_status='error' on failure
        - Clean up ALL resources before returning success
        - Set _services_started=False after cleanup

        Returns:
            ActionResult with status and optional error details
        """
        raise NotImplementedError(f"Service plugin {self.name} must implement stop_services()")

    # Status methods
    def is_running(self) -> bool:
        """Check if services are currently running.

        Returns:
            True if services started, False otherwise
        """
        return self._services_started

    def set_active(self, active: bool) -> None:
        """Default no-op for plugins without background work.

        Lives on ServicePlugin (not PluginBase) because LifecycleManaged
        conformance — which set_active is part of — already requires
        start_services / stop_services / is_running, all of which only
        live on ServicePlugin. Direct PluginBase subclasses are not
        lifecycle-managed, so they neither need set_active nor would
        their behavior change without it.

        Plugins with background loops (schedulers, drainers, gateway
        handlers, message dispatchers) override this to gate at their
        tick boundary. See `LifecycleManaged.set_active` for the full
        contract; the per-plugin override pattern is documented in
        `knowledge_bases/ananta_platform/03_writing_plugins/`.
        """

    def get_service_status(self) -> dict[str, object]:
        """Get detailed service status.

        Returns:
            Dictionary with running status, timestamps, errors, and metadata
        """
        return {
            "running": self._services_started,
            "started_at": self._service_started_at,
            "error": self._service_error,
            "metadata": {"supporting_interfaces": list(self._supporting_interfaces)},
        }

    def get_service_metadata(self) -> dict[str, object]:
        """Get service metadata for discovery and introspection.

        Returns:
            Dictionary with service name, description, and configuration
        """
        return {
            "name": self.name,
            "description": f"{self.name} service",
            "config_schema": {},
            "input_types": [],
        }
