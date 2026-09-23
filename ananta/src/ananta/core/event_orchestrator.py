import asyncio
import logging
import signal
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Protocol, cast

from ananta.constants import CONTEXT_KEY_FLOW_ID, CONTEXT_KEY_SESSION_ID
from ananta.core.events import ActionEvent, EventResult, SystemEvent
from ananta.core.orchestration.event_handler_manager import EventHandlerManager
from ananta.core.orchestration.initialization_manager import InitializationManager
from ananta.core.orchestration.runtime_manager import RuntimeManager
from ananta.core.process_registry.util import ProcessRegistryUtil
from ananta.core.runtime import is_draining
from ananta.error_handling import FrameworkError

# Graceful-teardown budget on SIGTERM. Hard-bounded BELOW the swap finisher's
# SIGKILL grace (macos_self_deployment ``DEFAULT_PRIOR_TERM_GRACE_SECONDS`` =
# 10.0s): a drained color MUST reach a clean ``exit 0`` before that SIGKILL,
# because a SIGKILL'd process can't exit 0 → launchd ``KeepAlive{Crashed}``
# would ghost-respawn it. The runtime loop breaks within ~1s of the handler, so
# 6s of teardown headroom leaves a comfortable margin under 10s.
_GRACEFUL_TEARDOWN_TIMEOUT_SECONDS = 6.0


class EventHandler(Protocol):
    """Protocol for event handler functions."""

    def __call__(self, event: SystemEvent) -> EventResult | None:
        """Handle a system event."""
        ...

    def handle(self, event: SystemEvent) -> EventResult | None:
        """Alternative handle method for handlers."""
        ...


if TYPE_CHECKING:
    from ananta.core.actions.action_factory import ActionFactory
    from ananta.core.actions.action_manager import ActionManager
    from ananta.core.config.config_manager import ConfigManager
    from ananta.core.events.event_queue import EventQueue
    from ananta.core.events.processor import EventHandlerRegistry, EventProcessor
    from ananta.core.orchestration.action_coordinator import ActionCoordinator
    from ananta.core.orchestration.managers.flow_manager import FlowManager
    from ananta.core.orchestration.managers.plugin_lifecycle_manager import PluginLifecycleManager
    from ananta.core.orchestration.managers.session_manager import SessionManager
    from ananta.core.orchestration.service_bindings import ServiceBindings
    from ananta.core.orchestration.service_manager import ServiceManager
    from ananta.core.plugins.plugin_manager import PluginManager
    from ananta.core.state.async_job_manager import AsyncJobManager
    from ananta.core.state.state_manager import StateManager
    from ananta.services.action_preparation_service import ActionPreparationService
    from ananta.services.blob_storage_service import BlobStorageService
    from ananta.services.state_service import StateService

logger = logging.getLogger(__name__)


class EventOrchestrator:
    action_factory: "ActionFactory | None" = None  # Action factory for template processing
    service_manager: "ServiceManager"  # Initialized by startup sequence
    action_coordinator: "ActionCoordinator"  # Initialized by startup sequence
    service_bindings: "ServiceBindings"  # Initialized by startup sequence

    def __init__(
        self,
        starting_prompt: str,
        max_consecutive_errors: int | None = None,
        max_actions_per_cycle: int | None = None,
        plugin_config: dict[str, dict[str, object]] | None = None,
        default_inference_provider: str | None = None,
    ) -> None:
        # Delegate complex initialization to InitializationManager
        initialization_manager = InitializationManager()
        init_result = initialization_manager.initialize_orchestrator(
            orchestrator=self,
            starting_prompt=starting_prompt,
            max_consecutive_errors=max_consecutive_errors,
            max_actions_per_cycle=max_actions_per_cycle,
            plugin_config=plugin_config,
            default_inference_provider=default_inference_provider,
        )

        # Set all basic attributes from initialization result
        self.config: ConfigManager = init_result.config  # type: ignore[assignment]
        self.APP_HOME: str = init_result.APP_HOME
        self.starting_prompt: str = init_result.starting_prompt
        self.max_consecutive_errors: int | None = init_result.max_consecutive_errors
        self.max_actions_per_cycle: int | None = init_result.max_actions_per_cycle
        self.plugin_operational_config: dict[str, dict[str, object]] = (
            init_result.plugin_operational_config
        )
        self.default_inference_provider: str | None = init_result.default_inference_provider
        self.current_session_id: str | None = init_result.current_session_id
        self.current_flow_id: str | None = init_result.current_flow_id
        self._session_timeout_hours: int = init_result.session_timeout_hours
        self.event_queue: EventQueue = init_result.event_queue  # type: ignore[assignment]
        self.event_handler_registry: EventHandlerRegistry = init_result.event_handler_registry  # type: ignore[assignment]
        self.event_bus: object = init_result.event_bus
        self.event: asyncio.Event = init_result.event
        self.shutdown_event: asyncio.Event = init_result.shutdown_event
        self.ready_event: asyncio.Event = init_result.ready_event
        self._main_loop: object | None = init_result.main_loop

        # L3 blue-green Slice D: color-active flag consulted by platform-level
        # workers (ActionQueuePoller and anything else that ticks against
        # the orchestrator). True at startup so single-color (legacy /
        # no-router) deployments behave identically; the deployment plugin
        # flips this on the inactive color during a swap.
        self.is_active_color: bool = True

        # SIGTERM drain-exit bookkeeping (blue-green respawn suppression). Set by
        # ``_on_sigterm`` (installed in ``run``): whether a SIGTERM arrived and
        # whether the drain sentinel was present AT receipt. ``sigterm_exit_code``
        # derives the process exit code from these — an intentionally-drained
        # color exits 0 (launchd must NOT respawn it), a stray SIGTERM of a live
        # launchd-managed color exits non-zero (launchd respawns = supervision).
        self._sigterm_received: bool = False
        self._draining_at_sigterm: bool = False

        # ServiceManager and ActionCoordinator now initialized in STARTUP_SEQUENCE
        # Verify they were initialized properly
        assert hasattr(self, "service_manager"), (
            "ServiceManager must be initialized by startup sequence"
        )
        assert hasattr(self, "action_coordinator"), (
            "ActionCoordinator must be initialized by startup sequence"
        )
        assert hasattr(self, "action_factory"), "action_factory must be set by startup sequence"

        # Delegate attributes for backward compatibility
        self._delegate_service_attributes()
        self._delegate_action_attributes()

        # Create ActionProcessor for legacy action processing
        # Note: The ActionProcessor in action_coordinator is different - it's the ActionProcessor
        # from ananta.core.actions.action_processor. This is the orchestration ActionProcessor.
        from ananta.core.orchestration.action_processor import (
            ActionProcessor as OrchActionProcessor,
        )

        assert self.action_factory is not None, (
            "ActionFactory must be initialized before ActionProcessor"
        )
        self.action_processor = OrchActionProcessor(
            state_manager=self.state_manager,
            action_factory=self.action_factory,
            session_id=self.current_session_id,
            flow_id=self.current_flow_id,
        )

        # Register core event handlers
        self._register_core_event_handlers()

        logger.debug(
            "EventOrchestrator: Three-phase initialization complete via InitializationManager"
        )

    def _delegate_service_attributes(self) -> None:
        """Delegate service attributes from ServiceManager for compatibility."""
        self._validate_service_manager_attributes()
        self._delegate_core_services()
        self._delegate_correlation_managers()
        self.services_collection = self.service_manager.services_collection

    def _validate_service_manager_attributes(self) -> None:
        """Validate that all required ServiceManager attributes are initialized."""
        required_attrs: list[str] = [
            "config",
            "app_home",
            "plugin_manager",
            "state_service",
            "state_manager",
            "async_job_manager",
            "blob_storage_service",
            "session_manager",
            "flow_manager",
            "action_recorder",
        ]
        for attr in required_attrs:
            assert getattr(self.service_manager, attr) is not None, (
                f"ServiceManager.{attr} must be initialized"
            )

    def _delegate_core_services(self) -> None:
        """Delegate core service attributes from ServiceManager.

        Note: _validate_service_manager_attributes() was called before this method,
        which asserts all required attributes are not None. We use assertions here
        to satisfy mypy's type narrowing requirements.
        """
        # These assertions satisfy mypy after _validate_service_manager_attributes() confirms non-None
        assert self.service_manager.config is not None
        assert self.service_manager.app_home is not None
        assert self.service_manager.plugin_manager is not None
        assert self.service_manager.state_service is not None
        assert self.service_manager.state_manager is not None
        assert self.service_manager.async_job_manager is not None
        assert self.service_manager.blob_storage_service is not None

        self.config = self.service_manager.config
        _ = (
            self.service_manager.app_home
        )  # Verify app_home is set (self.APP_HOME already set in __init__)
        self._system_platform_manager = self.service_manager._system_platform_manager
        self.plugin_manager: PluginManager = self.service_manager.plugin_manager
        self.state_service: StateService = self.service_manager.state_service
        self.process_registry_util = ProcessRegistryUtil(self.state_service)
        self.state_manager: StateManager[dict[str, object]] = self.service_manager.state_manager
        self.async_job_manager: AsyncJobManager = self.service_manager.async_job_manager
        self.blob_storage_service: BlobStorageService = self.service_manager.blob_storage_service
        self.platform_services_manager = self.service_manager.platform_services_manager
        self.unified_metadata_registry = self.service_manager.unified_metadata_registry

    def _delegate_correlation_managers(self) -> None:
        """Delegate correlation manager attributes from ServiceManager."""
        self.session_manager: SessionManager = self.service_manager.session_manager  # type: ignore[assignment]
        self.flow_manager: FlowManager = self.service_manager.flow_manager  # type: ignore[assignment]
        self.action_recorder = self.service_manager.action_recorder

    def _delegate_action_attributes(self) -> None:
        """Delegate action attributes from ActionCoordinator for compatibility."""
        assert self.action_coordinator.action_manager is not None
        assert self.action_coordinator.action_factory is not None
        assert self.action_coordinator.event_processor is not None

        # Action processing components - narrowed with assertions above
        # Note: action_processor is created separately in __init__ with session/flow IDs
        self.action_manager: ActionManager = self.action_coordinator.action_manager
        self.action_preparation_service: ActionPreparationService | None = (
            self.action_coordinator.action_preparation_service
        )
        self.action_factory = self.action_coordinator.action_factory
        self.action_queue_poller = self.action_coordinator.action_queue_poller
        # L3 Slice D: bind the poller's color-active getter to this orchestrator's
        # is_active_color flag. Done here (not in ActionCoordinator) because the
        # orchestrator owns is_active_color and ActionCoordinator doesn't have
        # an orchestrator ref.
        if self.action_queue_poller is not None:
            self.action_queue_poller.set_is_active_color_getter(
                lambda: self.is_active_color,
            )
            # Deterministic-continuation plan advancement: the poller is
            # built before plugin bindings exist, so it resolves the
            # plan-lifecycle service lazily through this orchestrator (the
            # same late-binding shape as the vertex path's per-transaction
            # _get_service_optional). Without this wiring the deterministic
            # advancer was a silent no-op — every multi-hop deterministic
            # chain died on completed_key_not_declared_by_current_step at
            # hop 2 (Track-A first production run, 2026-07-05).
            self.action_queue_poller.set_plan_lifecycle_resolver(
                lambda: self.get_service("plan_lifecycle_service"),
            )
        self.event_processor: EventProcessor = self.action_coordinator.event_processor

        # Process registry
        self._process_registry_manager = self.action_coordinator._process_registry_manager
        self._process_registry: dict[str, object] = self.action_coordinator._process_registry

        # Flags
        self._framework_services_initialized: bool = (
            self.action_coordinator._framework_services_initialized
        )
        self._plugins_ready: bool = self.action_coordinator._plugins_ready

    def _validate_inference_provider(self) -> None:
        pass

    def _get_plugin_service(self, plugin_name: str) -> object | None:
        """Get a plugin instance as a service provider.

        This enables plugin-provided services to be accessed via service_interface
        routing (e.g., service_interface::vault_service::store).

        Args:
            plugin_name: Name of the plugin to get (e.g., 'macos_vault_plugin')

        Returns:
            Plugin instance if found and loaded, None otherwise
        """
        return self.plugin_manager.get_plugin(plugin_name)

    # Services available as direct attributes on the orchestrator
    _DIRECT_ATTR_SERVICES: frozenset[str] = frozenset({
        "state_service", "blob_storage_service", "discovery_service",
        "context_service",
        "io_interface_service", "knowledge_service", "thinking_service",
        "session_ledger_service",
    })

    # Services that live on service_manager (created later in startup)
    _SERVICE_MANAGER_SERVICES: frozenset[str] = frozenset({
        "inference_service",
        "job_service", "flow_service", "lifecycle_management_service",
        "scheduling_service", "context_management_service",
        "prompt_assembly_service",
        "plan_lifecycle_service", "wbs_lifecycle_service",
    })

    def get_service(self, service_name: str) -> object | None:
        """Get service instance by name for dynamic service routing.

        See: ananta_build/2025-12-06_service_binding_architecture.md

        Three tiers, checked in order:
        1. Direct attributes on orchestrator (state, blob, discovery, IO, knowledge, thinking)
        2. service_manager attributes (job, flow, lifecycle, scheduling, context_management)
        3. memory_service wrapper with plugin fallback
        4. Plugin-backed services via service_bindings (everything else)
        """
        if service_name in self._DIRECT_ATTR_SERVICES:
            return getattr(self, service_name, None)

        if service_name in self._SERVICE_MANAGER_SERVICES:
            if not hasattr(self, "service_manager"):
                return None
            return getattr(self.service_manager, service_name, None)

        if service_name == "memory_service":
            wrapper = getattr(self, "memory_service", None)
            return wrapper if wrapper is not None else self._get_bound_plugin_service(service_name)

        return self._get_bound_plugin_service(service_name)

    def _get_bound_plugin_service(self, service_name: str) -> object | None:
        """Get plugin-backed service using service bindings.

        Args:
            service_name: Service name (e.g., 'vault_service')

        Returns:
            Plugin instance if bound, None if not bound or service_bindings not yet initialized
        """
        from ananta.core.orchestration.service_bindings import ServiceName

        # service_bindings may not be initialized yet during early startup
        # Return None - callers handle this gracefully
        if not hasattr(self, "service_bindings"):
            return None

        try:
            svc_name = ServiceName(service_name)
        except ValueError:
            # Unknown service name - not a plugin-backed service
            return None

        plugin_name = self.service_bindings.get_plugin_name(svc_name)
        if plugin_name is None:
            return None

        return self._get_plugin_service(plugin_name)

    async def _initialize_plugins(self) -> None:
        """Initialize all plugins through PluginLifecycleManager.

        Called once at startup. Delegates to PluginLifecycleManager.initialize_plugins().
        """
        if self._system_platform_manager is None:
            raise RuntimeError("SystemPlatformManager not initialized")
        # get_plugin_lifecycle_manager returns object (per protocol) but actually returns
        # PluginLifecycleManager - cast to use its methods
        plugin_lifecycle_manager = cast(
            "PluginLifecycleManager", self._system_platform_manager.get_plugin_lifecycle_manager()
        )
        await plugin_lifecycle_manager.initialize_plugins(self.plugin_manager)

    def _load_starting_action_definitions(
        self, config_path: Path
    ) -> list[dict[str, object]] | None:
        """Load starting action definitions from config file.

        Returns:
            List of action definitions, or None if file doesn't exist.
        Raises:
            FrameworkError if JSON is invalid or format is wrong.
        """
        import json

        if not config_path.exists():
            logger.error(f"No starting_action_definitions.json found at {config_path}")
            return None

        try:
            with open(config_path) as f:
                action_definitions = json.load(f)
        except json.JSONDecodeError as e:
            raise FrameworkError(
                message=f"Invalid JSON in starting_action_definitions.json: {e}",
                error_code="event_orchestrator.invalid_starting_actions_config",
                details={"config_path": str(config_path)},
            ) from e

        if not isinstance(action_definitions, list):
            raise FrameworkError(
                message="starting_action_definitions.json must contain an array of action definitions",
                error_code="event_orchestrator.invalid_starting_actions_format",
                details={
                    "config_path": str(config_path),
                    "got_type": type(action_definitions).__name__,
                },
            )

        return action_definitions

    def _build_starting_context(self) -> dict[str, object]:
        """Build context with session and flow IDs for starting actions."""
        context: dict[str, object] = {}
        if self.current_session_id:
            context[CONTEXT_KEY_SESSION_ID] = self.current_session_id
        if self.current_flow_id:
            context[CONTEXT_KEY_FLOW_ID] = self.current_flow_id
        return context

    def _submit_starting_actions(
        self, action_definitions: list[dict[str, object]], context: dict[str, object]
    ) -> None:
        """Submit each action definition through the standard pipeline.

        Injects flow_id/session_id into each action_def before submission since
        ActionFactory now requires flow_id in action_definition (no context fallback).

        Raises:
            FrameworkError: If flow_id is not available for injection
        """
        assert self.action_factory is not None, "ActionFactory not initialized"
        self.action_factory.update_process_registry(self._process_registry)

        # Fail fast: flow_id is required for all actions
        if not self.current_flow_id:
            raise FrameworkError(
                message="Cannot submit starting actions without flow_id",
                error_code="event_orchestrator.flow_id_required",
                details={"action_count": len(action_definitions)},
            )

        for action_def in action_definitions:
            # Inject flow_id/session_id into action_def (required by ActionFactory)
            action_def[CONTEXT_KEY_FLOW_ID] = self.current_flow_id
            if self.current_session_id:
                action_def[CONTEXT_KEY_SESSION_ID] = self.current_session_id

            name = action_def.get("name") or action_def.get("process_key", "unknown")
            logger.debug(f"EventOrchestrator: Submitting starting action: {name}")
            # submit_action_definition raises on failure
            self.action_factory.submit_action_definition(action_def, context)

    def _initialize_actions(self, state: dict[str, object]) -> dict[str, object]:
        """Initialize starting actions from config file.

        Loads action definitions from starting_action_definitions.json and submits
        them through the standard action submission pipeline (same path as VERTEX outputs).
        """
        config_path = Path(self.APP_HOME) / "config" / "starting_action_definitions.json"
        action_definitions = self._load_starting_action_definitions(config_path)

        if action_definitions is None:
            return state

        context = self._build_starting_context()
        self._submit_starting_actions(action_definitions, context)
        return state

    def _register_core_event_handlers(self) -> None:
        """Register core event handlers using EventHandlerManager for clean delegation."""
        logger.debug(
            "EventOrchestrator: Delegating event handler registration to EventHandlerManager"
        )

        # Delegate complex event handler registration to EventHandlerManager
        event_handler_manager = EventHandlerManager(orchestrator_ref=self)
        event_handler_manager.register_all_core_handlers()

    # === BACKWARDS COMPATIBLE INTERFACE METHODS ===

    async def process_actions(self) -> dict[str, object] | None:
        """Process actions using ActionProcessor for cleaner delegation."""
        logger.debug("EventOrchestrator: process_actions() called - delegating to ActionProcessor")

        if self.shutdown_event.is_set():
            logger.debug("EventOrchestrator: Shutdown requested, stopping event processing")
            return None

        try:
            # Load state
            state = await self.state_manager.load()
            legacy_actions_obj = state.get("actions", [])
            # Type narrow: ensure legacy_actions is a list
            if not isinstance(legacy_actions_obj, list):
                legacy_actions_obj = []
            legacy_actions: list[object] = legacy_actions_obj
            logger.debug(f"EventOrchestrator: State loaded, pending actions: {len(legacy_actions)}")

            # Update ActionProcessor with current session/flow IDs BEFORE initializing actions
            # This ensures starting actions have proper session context
            self.action_processor.update_session_flow(self.current_session_id, self.current_flow_id)

            # Check if we need to initialize actions
            if not legacy_actions:
                logger.debug(
                    f"EventOrchestrator: No pending actions, initializing with starting prompt: {self.starting_prompt}"
                )
                state = self._initialize_actions(state)
                await self.state_manager.save(state)
                logger.debug("EventOrchestrator: Starting prompt action initialized and saved")
                # Update legacy_actions after initialization
                legacy_actions_obj = state.get("actions", [])
                if not isinstance(legacy_actions_obj, list):
                    legacy_actions_obj = []
                legacy_actions = legacy_actions_obj

            # Process legacy actions using ActionProcessor
            processed_count = await self.action_processor.process_legacy_actions(state)
            logger.debug(f"EventOrchestrator: ActionProcessor processed {processed_count} actions")

            # Clear legacy actions from state
            if legacy_actions:
                state["actions"] = []
                await self.state_manager.save(state)

            # Ensure we return the state dict directly
            return dict(state)

        except Exception:
            raise

    async def run(self) -> None:
        """Execute Post-Phase-3 + Runtime operations via RuntimeManager."""
        # Install the SIGTERM handler on THIS (main-thread) loop before the
        # runtime loop blocks, so an intentional blue-green drain exits 0.
        self._install_sigterm_handler()
        # Delegate complex runtime operations to RuntimeManager
        runtime_manager = RuntimeManager(orchestrator_ref=self)
        try:
            await runtime_manager.execute_runtime_operations()
        finally:
            # On any exit from the runtime loop (SIGTERM-driven shutdown or an
            # internal stop) run a bounded graceful teardown so a drained color
            # releases cleanly and exits inside the SIGKILL grace window.
            await self._graceful_teardown()

    def _install_sigterm_handler(self) -> None:
        """Install an asyncio SIGTERM handler for a graceful, respawn-aware exit.

        ``loop.add_signal_handler`` (NOT ``signal.signal``) so the callback runs
        on the main event loop — no re-entrancy hazard with the bridge /
        streamable uvicorn servers, which run ``server.serve()`` on per-thread
        loops in daemon threads where uvicorn installs NO process signal handler
        (``install_signal_handlers`` is a main-thread-only no-op). This is
        therefore the sole process SIGTERM handler. Best-effort: a platform
        without ``add_signal_handler`` (or a non-main-thread loop) keeps the
        default disposition.
        """
        try:
            loop = asyncio.get_running_loop()
            loop.add_signal_handler(signal.SIGTERM, self._on_sigterm)
        except (NotImplementedError, RuntimeError, ValueError) as exc:
            logger.warning("SIGTERM handler not installed (%s); default disposition", exc)

    def _on_sigterm(self) -> None:
        """Cache the drain state AT RECEIPT and wake the runtime loop to shut down.

        Read-at-receipt (not post-teardown) is the robust ordering: the swap
        finisher holds the drain sentinel across the whole SIGTERM+wait window
        (``drain_sentinel.held()`` wraps ``_signal_and_wait``), so a drained
        color reads it present here and will exit 0. Minimal work — cache the
        bools and signal shutdown; teardown runs on the loop after the runtime
        loop unblocks on ``shutdown_event``.
        """
        self._sigterm_received = True
        self._draining_at_sigterm = is_draining()
        logger.info(
            "SIGTERM received (draining=%s) — initiating graceful shutdown",
            self._draining_at_sigterm,
        )
        self.shutdown_event.set()
        self.event.set()

    async def _graceful_teardown(self) -> None:
        """Bounded best-effort teardown so a drained color exits inside the grace.

        Hard-bounded by ``_GRACEFUL_TEARDOWN_TIMEOUT_SECONDS`` (< the swap
        finisher's SIGKILL grace): if ``shutdown``/``cleanup`` hang, the process
        still exits in time to be a clean ``exit 0`` (no SIGKILL → no ghost).
        """
        try:
            await asyncio.wait_for(
                self._run_teardown(), timeout=_GRACEFUL_TEARDOWN_TIMEOUT_SECONDS
            )
        except TimeoutError:
            logger.warning("graceful teardown exceeded budget; exiting anyway")
        except Exception as exc:  # noqa: BLE001 — teardown is best-effort; exit regardless
            logger.warning("graceful teardown error (%s); exiting anyway", exc)

    async def _run_teardown(self) -> None:
        await self.shutdown()
        await self.cleanup()

    @property
    def sigterm_exit_code(self) -> int:
        """Process exit code reflecting the SIGTERM disposition.

        ``0`` for normal completion or an INTENTIONAL drain (sentinel present →
        launchd must NOT respawn). Non-zero ONLY for a stray SIGTERM of a live
        color with no drain sentinel (→ launchd respawns = correct supervision).
        """
        if self._sigterm_received and not self._draining_at_sigterm:
            return 1
        return 0

    async def shutdown(self) -> None:
        """Gracefully shutdown the EventOrchestrator and all services"""

        try:
            if hasattr(self, "action_queue_poller") and self.action_queue_poller:
                await self.action_queue_poller.stop()
        except Exception:
            pass

        try:
            automation_gateway = getattr(self, "automation_gateway", None)
            if automation_gateway:
                await automation_gateway.stop()
        except Exception:
            pass

    def trigger_event(self) -> None:
        try:
            # Set the legacy event for backwards compatibility
            self.event.set()

            # Also add a system event to the queue
            system_event = SystemEvent(
                system_event_type="orchestrator_triggered",
                context={"trigger_time": datetime.now(UTC).isoformat()},
                source="orchestrator_trigger",
            )

            # Enqueue the system event (use asyncio.create_task for async from sync)
            asyncio.create_task(self.event_queue.enqueue(system_event))

        except Exception:
            pass

    def create_session(
        self, namespace: str, context_type: str, metadata: dict[str, object] | None = None
    ) -> str:
        # FIX: Use SessionManager to persist to database instead of creating unused events
        session_id = self.session_manager.create_session(namespace, context_type, metadata)
        self.current_session_id = session_id

        return str(session_id)

    def create_flow(
        self, session_id: str, trigger_type: str, trigger_data: dict[str, object], priority: int = 5
    ) -> str:
        # FIX: Use FlowManager to persist to database instead of creating unused events
        flow_id = self.flow_manager.create_flow(session_id, trigger_type, trigger_data, priority)
        self.current_flow_id = flow_id

        return str(flow_id)

    def complete_service_transitions_sync(self) -> None:
        pass

    async def complete_service_transitions(self) -> None:
        pass

    async def cleanup(self) -> None:
        self.shutdown_event.set()

        # Process any remaining events
        remaining_events_count = self.event_queue.size()
        if remaining_events_count > 0:
            # Quick drain of remaining events
            for _ in range(min(remaining_events_count, 10)):  # Process up to 10 remaining events
                event = await self.event_queue.dequeue(timeout=0.1)
                if event:
                    await self.event_processor.process_event(event)

    # === EVENT SYSTEM SPECIFIC METHODS ===

    async def submit_action_event(self, action_event: ActionEvent) -> str:
        await self.event_queue.enqueue(action_event)

        return action_event.event_id

    async def submit_system_event(self, system_event: SystemEvent) -> str:
        await self.event_queue.enqueue(system_event)

        return system_event.event_id

    def register_event_handler(self, event_type: str, handler: EventHandler) -> None:
        # EventHandlerRegistry expects Callable[..., object] which is compatible with EventHandler
        self.event_handler_registry.register(event_type, handler)

    def get_event_queue_size(self) -> int:
        return int(self.event_queue.size())

    # === COORDINATION HELPER METHODS (EXTRACTED FROM LEGACY ORCHESTRATOR) ===

    async def _process_actions_from_result(
        self,
        state: dict[str, object],
        result: dict[str, object],
        parent_action_id: str | None = None,
    ) -> None:
        """Delegate action processing from results to ActionProcessor."""
        await self.action_processor.process_actions_from_result(state, result, parent_action_id)

    async def _normalize_action_for_events(self, action: object) -> dict[str, object] | None:
        """Normalize action for event processing."""
        from ananta.core.orchestration.action_normalization_service import (
            ActionNormalizationService,
        )

        return await ActionNormalizationService.normalize_action_for_events(action)

    async def _update_action_status_in_state(
        self,
        _state: dict[str, object],  # Reserved for interface compatibility
        _action_name: str,  # Reserved for interface compatibility
        _status: str,  # Reserved for interface compatibility
        _error: dict[str, object] | None = None,  # Reserved for interface compatibility
        _result: dict[str, object] | None = None,  # Reserved for interface compatibility
    ) -> None:
        """
        OBSOLETE: Action status is now managed via database-first ActionFactory.
        This method is kept for backward compatibility but does nothing.
        Legacy in-memory state tracking has been replaced by database persistence.
        """

    def _convert_process_registry_to_records(self) -> list[dict[str, object]]:
        """Convert internal process registry format to database records."""
        registry_records: list[dict[str, object]] = []
        processes_obj = self._process_registry.get("processes", {})

        # Type narrow: ensure processes is a dict
        if not isinstance(processes_obj, dict):
            return registry_records

        processes: dict[str, object] = processes_obj

        for process_key, process_info_obj in processes.items():
            # Type narrow: ensure process_info is a dict
            # Note: process_key is always str since it's a dict key
            if not isinstance(process_info_obj, dict):
                continue

            registry_records.append(
                {
                    "process_key": process_key,
                    "provider_type": str(process_info_obj.get("provider_type", "")),
                    "provider": str(process_info_obj.get("provider", "")),
                    "function_name": str(process_info_obj.get("function_name", "")),
                    "description": str(process_info_obj.get("description", "")),
                    "is_inference_capable": bool(
                        process_info_obj.get("is_inference_capable", False)
                    ),
                }
            )

        return registry_records

    async def _save_process_registry_to_state(self) -> None:
        try:
            registry_records = self._convert_process_registry_to_records()

            # Use ProcessRegistryUtil for centralized process registry operations
            success = self.process_registry_util.sync_records(registry_records)

            if success:
                pass
            else:
                pass

        except Exception:
            pass

    def _save_process_registry_to_state_sync(self) -> None:
        try:
            registry_records = self._convert_process_registry_to_records()

            # Use ProcessRegistryUtil for centralized process registry operations
            success = self.process_registry_util.sync_records(registry_records)

            if success:
                pass
            else:
                raise RuntimeError("Process registry sync failed")

        except Exception:
            raise

    def get_process_registry(self) -> dict[str, object]:
        """Return the current process registry data."""
        return self._process_registry

    def apply_knowledge_base_updates(
        self, updates: dict[str, dict[str, object]],
    ) -> dict[str, object]:
        """Apply knowledge base text updates to the process registry at runtime."""
        result = self.action_coordinator.apply_knowledge_base_updates(updates)
        self._process_registry = self.action_coordinator._process_registry
        return result

    def initialize_database_after_schema_creation(self) -> None:
        """Initialize database operations AFTER core schemas are created.

        CRITICAL: This must be called after startup_sequence step 8
        (`_initialize_schemas`, the plugin-schema lifecycle) so the
        ``core__action_events`` table exists before any database operations.

        This method fixes the race condition where database operations happened before
        the target tables existed in the database.
        """
        logger.debug("=== DATABASE INITIALIZATION ===")

        # Complete ActionCoordinator database initialization (process registry persistence)
        self.action_coordinator.complete_database_initialization()

        logger.debug("Database initialization completed successfully")
