"""
Startup Sequence Manager

Responsibility: Deterministic startup sequence for EventOrchestrator
Replaces: Three-phase initialization (Phase1, Phase2, Phase3)
Critical Fix: Service plugins initialized BEFORE service wrappers created

Design Principles:
    pass
- Single Responsibility: Startup sequence management only
- Deterministic Order: Explicit dependencies between steps
- Fail Fast: Critical step failures halt startup immediately
- Readiness Contracts: Verify plugin readiness before proceeding
"""

import asyncio
import inspect
import json
import logging
import os
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol, cast
from zoneinfo import ZoneInfo

from ananta.constants import STATE_MANAGEMENT_PLUGINS

logger = logging.getLogger(__name__)


# W-PLUGIN-LAUNCH-KEYS (P0 Tier 2 sub-1, 2026-06-07): runtime readiness
# gate for plugin-declared vault keys. Mode constant gates the failure
# behavior across the migration window:
#
#   "warn"  — sub-1 landing default. Iterate declared required keys,
#             log a WARNING line per missing key, and let the plugin
#             load. Migration window where live vault rows may still be
#             at flat names + plugins have not yet had their full
#             required-key declarations exercised against a fully
#             migrated vault. Today's "lazy fail at first use" behavior
#             is preserved.
#
#   "fail"  — sub-2 landing default. Iterate declared required keys,
#             raise ``MissingVaultKeyError`` at the plugin's readiness
#             boundary, mark plugin unhealthy, fail the readiness
#             probe per master plan §1.3.
#
# W-VAULT-CALLER-ENFORCE (sub-2) flips this constant to "fail" in the
# same commit that activates namespace + operator-only enforcement at
# the VaultService layer. No other call site reads this constant; the
# flip is a single-line change.
VAULT_KEYS_GATE_MODE: str = "fail"

_KNOWLEDGE_HYDRATION_MAX_ATTEMPTS = 3
_KNOWLEDGE_HYDRATION_RETRY_INITIAL_SECONDS = 1.0
_KNOWLEDGE_HYDRATION_RETRY_MAX_SECONDS = 4.0


def _get_build_marker() -> str:
    """Generate build marker with current UTC and PST timestamps.

    Returns a timestamp string showing both UTC and PST times for clarity.
    """
    now_utc = datetime.now(UTC)
    now_pst = now_utc.astimezone(ZoneInfo("America/Los_Angeles"))

    utc_str = now_utc.strftime("%Y-%m-%d %H:%M:%S UTC")
    pst_str = now_pst.strftime("%Y-%m-%d %H:%M:%S %Z")

    return f"{utc_str} / {pst_str}"


class OrchestratorProtocol(Protocol):
    """Protocol for orchestrator reference during startup."""

    APP_HOME: str
    config_manager: Any
    plugin_manager: Any
    event_bus: Any
    state_service: Any = None
    vector_service: Any = None
    embedding_service: Any = None
    inference_service: Any = None
    discovery_service: Any = None
    at_command_processor: Any = None
    action_coordinator: Any = None
    memory_service: Any = None
    io_interface_service: Any = None


class StartupError(Exception):
    """Exception raised when startup sequence fails.

    ``step_name`` carries the failing step's name as structured data so
    callers (notably the L2 probe-failure writer in
    :mod:`initialization_manager`) can emit a parseable
    ``probe_failure.json`` without scraping the message. Empty string
    when the error is raised from outside any step (e.g. dependency
    check before the first step runs).
    """

    def __init__(self, message: str, *, step_name: str = "") -> None:
        super().__init__(message)
        self.step_name = step_name


@dataclass
class StartupStep:
    """Represents a single step in the startup sequence."""

    name: str
    function: Callable[[Any], None]
    dependencies: list[str]
    critical: bool = True


class StartupSequenceRunner:
    """
    Runs the startup sequence with dependency checking.

    Each step is executed in order, with dependencies verified before execution.
    Critical step failures halt the startup process.
    """

    def __init__(self, sequence: list[StartupStep]):
        self.sequence = sequence
        self.completed: set[str] = set()

    def run(self, orchestrator: Any) -> None:
        """Execute the startup sequence."""
        logger.info(f"STARTUP: Build marker = {_get_build_marker()}")
        logger.debug("STARTING DETERMINISTIC STARTUP SEQUENCE")

        for step in self.sequence:
            missing = [d for d in step.dependencies if d not in self.completed]
            if missing:
                error_msg = f"{step.name}: missing dependencies {missing}"
                logger.error(f"❌ {error_msg}")
                raise StartupError(error_msg, step_name=step.name)

            try:
                # Handle both sync and async functions
                if inspect.iscoroutinefunction(step.function):
                    asyncio.run(step.function(orchestrator))
                else:
                    step.function(orchestrator)

                self.completed.add(step.name)

            except Exception as e:
                logger.error(f"❌ {step.name}: {e}", exc_info=True)
                if step.critical:
                    raise StartupError(
                        f"{step.name} failed: {e}", step_name=step.name,
                    ) from e
                else:
                    logger.debug(f"Non-critical step {step.name} failed, continuing...")


# ==============================================================================
# STEP IMPLEMENTATIONS
# ==============================================================================


def _load_config(orch: Any) -> None:
    """Load system configuration."""
    from ananta.core.config.config_manager import get_config

    orch.config_manager = get_config()
    orch.APP_HOME = orch.config_manager.APP_HOME


def _create_event_system(orch: Any) -> None:
    """Create core event system components."""
    from ananta.core.events import EventHandlerRegistry
    from ananta.core.events.event_queue import EventQueue

    orch.event_queue = EventQueue()
    orch.event_handler_registry = EventHandlerRegistry()
    orch.event_bus = orch.event_handler_registry


def _init_plugin_manager(orch: Any) -> None:
    """Initialize plugin manager and discover plugins.

    Profile manifest gating: if ``<APP_HOME>/config/manifest.yaml`` exists,
    only plugins listed in its ``plugins:`` array are loaded. Absent manifest
    falls back to "load every installed entry point" (the solet itself, pre-A5
    solets, dev boxes).
    """
    from ananta.core.plugins.plugin_manager import PluginManager
    from ananta.core.plugins.profile_manifest import load_manifest_plugin_set

    allowed_plugins = load_manifest_plugin_set(orch.APP_HOME)
    if allowed_plugins is not None:
        logger.info(
            f"Profile manifest gates discovery to {len(allowed_plugins)} plugins"
        )

    orch.plugin_manager = PluginManager()
    orch.plugin_manager.discover_plugins(
        orch.config_manager, allowed_plugins=allowed_plugins
    )
    _report_plugin_discovery_completeness(orch.plugin_manager, allowed_plugins)
    orch.plugin_manager.set_orchestrator_ref(orch)
    orch.plugin_manager.set_event_bus_ref(orch.event_bus)
    logger.debug(
        f"Plugin manager initialized, discovered {len(orch.plugin_manager.plugins)} plugins"
    )


def _report_plugin_discovery_completeness(
    plugin_manager: Any, allowed_plugins: set[str] | None
) -> None:
    """Log an incomplete manifest-governed roster without changing boot outcome."""
    if allowed_plugins is None:
        return
    missing = sorted(allowed_plugins - set(plugin_manager.plugins))
    if not missing:
        return
    logger.error(
        "Plugin discovery incomplete: manifest plugins were not loaded: %s; "
        "continuing boot per ruling rul_a4b34267",
        missing,
    )


def _initialize_plugin_configs(orch: Any) -> None:
    """Run ``plugin.initialize(plugin_config)`` for every discovered plugin.

    Binds ``config_provider`` (and any other state plugins set up at
    initialize-time) on the orchestrator's plugin instances. Without this
    step, plugins that follow the ``def initialize(self, config)`` contract
    end up with ``config_provider = None`` on the orchestrator's instance —
    even though ``ServiceTransitionCoordinator.execute_full_transition_sync``
    runs the same call on its bootstrap plugin_manager. The bootstrap
    instances and the orchestrator's instances are SEPARATE objects per
    the current two-plugin_manager architecture; initializing one set
    doesn't bind state on the other.

    Symptom this fixes (2026-05-31 incident, downstream of the
    ServiceTransitionCoordinator gating fix): the ledger periodic poll
    fires ~5 min after boot, calls into ``codex_filesystem_session_source
    _plugin.discover_sessions`` on the orchestrator's plugin instance,
    that path calls ``_require_root_dir`` → ``config_provider`` is None
    on this instance → ``FrameworkError`` cascades through error
    processing. The deeper fix is collapsing the two plugin_managers into
    one, but adding the symmetric initialize call here closes the gap
    without changing the broader architecture.
    """
    orch.plugin_manager.initialize_all_plugins(orch.config_manager)


def _load_service_bindings(orch: Any) -> None:
    """Load service bindings from config and environment.

    See: ananta_build/2025-12-06_service_binding_architecture.md

    Resolution order (highest priority first):
        pass
    1. Environment variables: ANANTA_{SERVICE_NAME}
    2. Legacy env vars: ANANTA_{SERVICE}_PLUGIN
    3. Config file: config/service_bindings.json
    """
    from ananta.core.orchestration.service_bindings import ServiceBindings

    orch.service_bindings = ServiceBindings(orch.APP_HOME)
    orch.service_bindings.load()

    # Validate required services have bindings
    orch.service_bindings.validate_required_services()

    # Validate every bound plugin was actually loaded during discovery.
    # Fail immediately here rather than surfacing as a confusing downstream
    # error when some other plugin calls get_service() for the missing one.
    bindings = orch.service_bindings.get_all_bindings()
    for service_name, binding in bindings.items():
        plugin_name = binding.plugin_name
        if plugin_name not in orch.plugin_manager.plugins:
            raise StartupError(
                f"Service '{service_name.value}' is bound to plugin '{plugin_name}', "
                f"but that plugin was not loaded during discovery. "
                f"Check that the plugin package is installed and its entry point is registered. "
                f"Loaded plugins: {sorted(orch.plugin_manager.plugins.keys())}"
            )


def _inject_state_vault_service(orch: Any) -> None:
    """Inject the state plugin's caller-bound VaultServiceProxy BEFORE its pool-open.

    The state plugin is foundational: ``_start_state_plugin`` calls its
    ``prepare_for_readiness`` (which opens the first authorized Postgres
    pool) as the very first plugin step — long before the general
    ``_inject_vault_service`` step runs. Operator mandate is interface-only
    credential access, so the state plugin reads its DB password through
    ``vault_service`` rather than a direct keyring call. Its proxy must
    therefore be injected here, between ``load_service_bindings`` and
    ``start_state_plugin``.

    Resolution is safe this early:

    * ``service_bindings`` exists (loaded one step prior).
    * the vault plugin INSTANCE exists from discovery (``init_plugin_manager``).
    * ``orch.get_service`` routes the ``get_plugin`` lookup through
      ``event_orchestrator.py`` — on the dispatcher frame-guard allowlist —
      so the service-provider guard does not block it.

    The proxy forwards ``retrieve`` to the vault's Keychain substrate (built
    in the vault's ``__init__``), which needs neither the vault's
    ``prepare_for_readiness`` nor ``state_service`` — so there is no startup
    cycle: state depends on the vault's keychain at boot; the vault depends
    on ``state_service`` only at its own (later) readiness.

    The raw vault is NEVER handed to the state plugin directly — it MUST be
    wrapped in a ``VaultServiceProxy`` with the state plugin's name baked in,
    or the bound ``CallContext`` is absent and vault ``enforce_namespace``
    denies every retrieve.

    Skips silently when no ``vault_service`` is bound (mock-vault test
    profiles); the state plugin then fails loud at its own ``_initialize``
    when it finds ``self._vault_service is None``. Also skips when the state
    plugin or its setter is absent — ``_start_state_plugin`` (the next step)
    owns the authoritative missing-state-plugin error.
    """
    from ananta.core.orchestration.service_bindings import ServiceName  # noqa: PLC0415
    from ananta.interfaces.vault_service_interface import VaultServiceInterface  # noqa: PLC0415
    from ananta.services.vault_service.vault_service_proxy import VaultServiceProxy  # noqa: PLC0415

    state_plugin_name = orch.service_bindings.get_plugin_name(ServiceName.STATE_SERVICE)
    if state_plugin_name is None:
        logger.debug("No state_service binding yet; deferring state vault injection")
        return
    state_plugin = orch.plugin_manager.plugins.get(state_plugin_name)
    if state_plugin is None:
        logger.debug(
            "State plugin %s not yet loaded; deferring state vault injection",
            state_plugin_name,
        )
        return

    raw_vault = orch.get_service("vault_service")
    if raw_vault is None:
        logger.debug(
            "No vault_service bound; skipping state-plugin vault proxy injection "
            "(state plugin fails loud at _initialize if it requires the vault)"
        )
        return

    setter = getattr(state_plugin, "set_vault_service", None)
    if not callable(setter):
        logger.debug(
            "State plugin %s exposes no set_vault_service; skipping injection",
            state_plugin_name,
        )
        return

    proxy = VaultServiceProxy(
        cast(VaultServiceInterface, raw_vault), caller_plugin=state_plugin_name
    )
    setter(proxy)
    logger.debug("Injected VaultServiceProxy into state plugin %s", state_plugin_name)


def _start_state_plugin(orch: Any) -> None:
    """Start state management plugin first (required by other service plugins).

    Uses service bindings to determine which plugin provides state_service.
    """
    from ananta.core.orchestration.service_bindings import ServiceName

    # Get state plugin from service bindings (validated in _load_service_bindings)
    state_plugin_name = orch.service_bindings.get_plugin_name(ServiceName.STATE_SERVICE)

    if state_plugin_name is None:
        raise StartupError(
            "No plugin bound to state_service. "
            "Set ANANTA_STATE_SERVICE or ANANTA_STATE_PLUGIN environment variable, "
            "or configure in config/service_bindings.json"
        )

    # Store the state plugin name for later use
    orch._state_plugin_name = state_plugin_name

    # Direct registry lookup — startup bootstrapping runs before service wrappers
    # exist, so get_plugin()'s service-provider guard would block this call.
    # Same pattern used in _validate_service_interfaces.
    state_plugin = orch.plugin_manager.plugins.get(state_plugin_name)
    if state_plugin is None:
        raise StartupError(
            f"State plugin '{state_plugin_name}' not found in loaded plugins. "
            f"Available: {list(orch.plugin_manager.plugins.keys())}"
        )

    # Load config for state plugin
    config_path = Path(orch.APP_HOME) / "config" / "plugins" / f"{state_plugin_name}.json"
    if not config_path.exists():
        raise StartupError(f"State plugin config not found: {config_path}")

    with open(config_path) as f:
        _ = json.load(f)  # Config file validated but not used - plugin handles its own config

    # Lock down direct Postgres access — only substrate-providing plugins
    # (state + vector) may call psycopg.connect / ConnectionPool directly.
    # Installed BEFORE state plugin's prepare_for_readiness (which opens the
    # first authorized connection) and BEFORE any consumer plugin runs.
    from .postgres_authorization_guard import install_postgres_authorization_guard
    install_postgres_authorization_guard()

    # Initialize state plugin
    if hasattr(state_plugin, "prepare_for_readiness"):
        state_plugin.prepare_for_readiness()

    # Start state plugin services
    if hasattr(state_plugin, "start_services"):
        # Handle async start_services
        if inspect.iscoroutinefunction(state_plugin.start_services):
            asyncio.run(state_plugin.start_services())
        else:
            state_plugin.start_services()

    # Verify state plugin is ready
    if not state_plugin.is_ready():
        error = state_plugin.readiness_error or "Unknown error"
        raise StartupError(f"State plugin not ready: {error}")

    logger.debug(f"✅ State plugin {state_plugin_name} started and ready")


def _create_state_service_wrapper(orch: Any) -> None:
    """Create StateService wrapper (after state plugin is ready)."""
    from ananta.services.state_service import StateService

    state_plugin_name = getattr(orch, "_state_plugin_name", None)
    orch.state_service = StateService(
        orch.plugin_manager, orch.APP_HOME, state_plugin_name=state_plugin_name
    )


def _inject_dependencies(orch: Any) -> None:
    """NO-OP: Dependency injection removed (2025-12-06).

    Plugins now request services via orchestrator.get_service() in prepare_for_readiness().
    See: ananta_build/2025-12-06_service_binding_architecture.md

    This function is kept as a no-op to maintain startup sequence ordering.
    """
    del orch


def _inject_at_command_processor(orch: Any) -> None:
    """Inject AtCommandProcessor into IOInterfacePlugin instances that need it."""

    injected_count = 0
    for name, plugin in orch.plugin_manager.plugins.items():
        # Inject at_command_processor into IOInterfacePlugin instances
        if hasattr(plugin, "set_at_command_processor"):
            if hasattr(orch, "at_command_processor"):
                plugin.set_at_command_processor(orch.at_command_processor)
                injected_count += 1
            else:
                logger.error(
                    f"Plugin {name} requires at_command_processor but it's not available yet"
                )


def _inject_flow_manager(orch: Any) -> None:
    """Inject FlowManager into IOInterfacePlugin instances that need it."""

    injected_count = 0
    for name, plugin in orch.plugin_manager.plugins.items():
        # Inject flow_manager into IOInterfacePlugin instances
        if hasattr(plugin, "set_flow_manager"):
            if hasattr(orch, "flow_manager") and orch.flow_manager is not None:
                plugin.set_flow_manager(orch.flow_manager)
                injected_count += 1
            else:
                logger.debug(f"Plugin {name} requires flow_manager but it's not available yet")


def _inject_session_manager(orch: Any) -> None:
    """Inject SessionManager into IOInterfacePlugin instances that need it."""

    injected_count = 0
    for name, plugin in orch.plugin_manager.plugins.items():
        # Inject session_manager into IOInterfacePlugin instances
        if hasattr(plugin, "set_session_manager"):
            if hasattr(orch, "session_manager") and orch.session_manager is not None:
                plugin.set_session_manager(orch.session_manager)
                injected_count += 1
            else:
                logger.debug(f"Plugin {name} requires session_manager but it's not available yet")


def _inject_context_management_service(orch: Any) -> None:
    """Inject ContextManagementService into IOInterfacePlugin instances.

    CRITICAL: IO plugins need context_management_service to resolve context_id
    when submitting actions. Without this, discovery actions fail with
    "Discovery requires context_id but none provided".

    This is injected AFTER init_service_manager since that's where
    context_management_service is created on the ServiceManager.
    """
    # Get context_management_service from service_manager
    context_management_service = None
    if hasattr(orch, "service_manager") and orch.service_manager is not None:
        context_management_service = getattr(
            orch.service_manager, "context_management_service", None
        )

    if context_management_service is None:
        logger.error(
            "context_management_service not available - IO plugins will fail on action submission"
        )
        return

    injected_count = 0
    for plugin in orch.plugin_manager.plugins.values():
        if hasattr(plugin, "set_context_management_service"):
            plugin.set_context_management_service(context_management_service)
            injected_count += 1

    logger.debug(f"Injected context_management_service into {injected_count} plugins")


def _inject_memory_service(orch: Any) -> None:
    """Inject memory service into plugins that need it. Fails fast - memory is required."""
    if not hasattr(orch, "memory_service") or orch.memory_service is None:
        raise StartupError(
            "Cannot inject memory service: orch.memory_service is not set. "
            "Memory service is a required platform service."
        )

    for plugin_name, plugin in orch.plugin_manager.plugins.items():
        setter = getattr(plugin, "set_memory_service", None)
        if callable(setter):
            setter(orch.memory_service)
            logger.debug(f"Injected memory_service into {plugin_name}")


def _inject_vault_service(orch: Any) -> None:
    """Inject caller-bound VaultServiceProxy into plugins that need it.

    W-VAULT-INTERFACE-EXTEND Phase D-1 (P0 Tier 1, state-service
    consolidation campaign): each consuming plugin receives its OWN
    ``VaultServiceProxy`` instance with the plugin's name baked into
    the proxy's ``call_context`` at construction time. This is the
    binding mechanism; enforcement (namespace ownership + operator-only
    method gating) activates at Tier 2 W-VAULT-CALLER-ENFORCE.

    The raw vault service is resolved from the orchestrator's service
    registry one time and shared across all per-plugin proxies. The
    raw vault is NEVER exposed to consumer plugins directly — the proxy
    is the only handle. Caller-supplied ``call_context`` arguments
    flowing through the proxy are dropped server-side; the bound
    context is authoritative.

    Runs BEFORE ``start_service_plugins`` so consumer plugins can
    receive their proxy via ``set_vault_service`` and resolve
    ``self._vault_service`` lookups during their own
    ``prepare_for_readiness``. The vault plugin's
    ``prepare_for_readiness`` itself runs inside
    ``start_service_plugins`` — the proxy holds a strong reference to
    the vault plugin instance (which exists from plugin discovery), so
    method calls forwarded through the proxy resolve to the underlying
    plugin regardless of which plugin's ``prepare_for_readiness``
    runs first in the loop.

    Skips silently when no ``vault_service`` is bound (e.g., mock-vault
    test profiles). A profile that intentionally omits vault won't
    surface a setter-without-vault error here.
    """
    from ananta.interfaces.vault_service_interface import VaultServiceInterface  # noqa: PLC0415
    from ananta.services.vault_service.vault_service_proxy import VaultServiceProxy  # noqa: PLC0415

    get_service = getattr(orch, "get_service", None)
    if not callable(get_service):
        logger.debug("Orchestrator exposes no get_service; skipping vault proxy injection")
        return
    raw_vault = get_service("vault_service")
    if raw_vault is None:
        logger.debug("No vault_service bound; skipping vault proxy injection")
        return

    raw_vault_typed = cast(VaultServiceInterface, raw_vault)
    injected_count = 0
    for plugin_name, plugin in orch.plugin_manager.plugins.items():
        setter = getattr(plugin, "set_vault_service", None)
        if callable(setter):
            proxy = VaultServiceProxy(raw_vault_typed, caller_plugin=plugin_name)
            setter(proxy)
            injected_count += 1
            logger.debug(f"Injected VaultServiceProxy into {plugin_name}")
    logger.debug(f"vault_service proxy injected into {injected_count} plugins")


def _inject_compilation_context_builder(orch: Any) -> None:
    """Inject CompilationContextBuilder into IOInterfacePlugin instances that need it."""
    from ananta.core.services.compilation_context_builder import CompilationContextBuilder

    # Create a shared instance
    compilation_context_builder = CompilationContextBuilder()

    injected_count = 0
    for plugin in orch.plugin_manager.plugins.values():
        # Inject compilation_context_builder into IOInterfacePlugin instances
        if hasattr(plugin, "set_compilation_context_builder"):
            plugin.set_compilation_context_builder(compilation_context_builder)
            injected_count += 1


def _resolve_embedding_dimensions(orch: Any) -> int:
    """Resolve the embedding service's default output dimension at schema-init time.

    Looks up the embedding_service binding and routes plugin access through a
    transient EmbeddingService wrapper instance. Going through the wrapper —
    rather than calling plugin_manager.get_plugin() directly from startup
    code — satisfies the plugin dispatcher's service-provider-access
    enforcement, which restricts direct service-provider plugin access to
    the service wrapper's own file path. The wrapper's get_default_dimensions()
    is synchronous and does not require plugin readiness, so this resolver
    can run at step 8 before ServiceManager has instantiated the long-lived
    embedding_service wrapper.

    Raises:
        RuntimeError: If embedding_service is not bound, or the bound plugin
            doesn't expose get_default_dimensions(). Both are misconfigurations
            the platform must surface — silently falling back to a hardcoded
            default would re-introduce the dimension-mismatch failure mode
            this resolver exists to prevent.
    """
    from ananta.core.orchestration.service_bindings import ServiceName
    from ananta.services.embedding_service import EmbeddingService

    embedding_plugin_name = orch.service_bindings.get_plugin_name(ServiceName.EMBEDDING_SERVICE)
    if embedding_plugin_name is None:
        raise RuntimeError(
            "embedding_service has no binding; cannot resolve discovery vector dimension. "
            "Add an embedding_service entry to the profile's service_bindings."
        )
    embedding_service = EmbeddingService(
        plugin_manager=orch.plugin_manager,
        embedding_plugin_name=embedding_plugin_name,
    )
    dim = embedding_service.get_default_dimensions()
    if dim <= 0:
        raise RuntimeError(
            f"embedding plugin '{embedding_plugin_name}'.get_default_dimensions() "
            f"returned {dim!r} (expected positive int)."
        )
    logger.info(
        "Resolved embedding dimension %d from %s for discovery schema init",
        dim,
        embedding_plugin_name,
    )
    return dim


def _initialize_schemas(orch: Any) -> None:
    """Initialize core schemas and plugin schemas before they start using tables."""
    from ananta.config.core_schemas import CoreSchemaDefinitions
    from ananta.core.plugins.capabilities import collect_schemas
    from ananta.llm.session_ledger.schema import (
        build_session_ledger_event_embeddings_schema,
    )
    from ananta.services.discovery_service import get_discovery_schema_definitions
    from ananta.services.schema_manager import SchemaManager

    # CRITICAL: Initialize core schemas FIRST (required by framework and plugins)
    all_schemas = []
    try:
        core_schemas = CoreSchemaDefinitions.get_all_core_schemas()
        all_schemas.extend(core_schemas)
    except Exception as e:
        logger.error(f"Failed to get core schemas: {e}", exc_info=True)
        raise  # Core schemas are CRITICAL - fail fast if they can't be loaded

    # Collect schemas from all plugins that provide them
    # Uses SchemaProvider protocol - fails fast with PluginCapabilityError if any plugin fails
    plugin_schemas = collect_schemas(orch.plugin_manager.plugins)
    all_schemas.extend(plugin_schemas)

    # Add discovery service schemas (static registration before service wrapper exists).
    # The discovery_processes__embeddings table needs the embedding service's
    # output dimension to declare its vector column shape; resolve the bound
    # embedding plugin synchronously via the binding + plugin_manager. The
    # plugin INSTANCE exists from step 3, even though its full prepare_for_readiness
    # runs later in step 9 — get_default_dimensions() returns the plugin's
    # declared default without doing network I/O.
    try:
        embedding_dimensions = _resolve_embedding_dimensions(orch)
        discovery_schemas = get_discovery_schema_definitions(embedding_dimensions)
        all_schemas.extend(discovery_schemas)
        # LED-01 event-content vector store. Shares the embedder-resolved
        # dimension (never hardcoded) so its vector(N) column + HNSW index
        # match the deployed model, exactly like the discovery table above.
        # Owned here (not in get_all_core_schemas) because the dimension is
        # only knowable once the embedding binding is resolved.
        all_schemas.append(
            build_session_ledger_event_embeddings_schema(embedding_dimensions)
        )
    except Exception as e:
        logger.error(f"Failed to get discovery schemas: {e}", exc_info=True)
        raise  # Discovery schemas are also critical

    if all_schemas:
        # The plugin-schema lifecycle service is the state plugin's OWN interface
        # (bound at _start_state_plugin, before this step), so it is structurally
        # guaranteed present here. An unresolved binding is a fatal misconfig, not
        # a fallback case — fail loud. The legacy direct-create path was removed.
        plugin_schema_service = orch.get_service("plugin_schema_service")
        if plugin_schema_service is None:
            raise StartupError(
                "plugin_schema_service binding unresolved at schema init; the "
                "state plugin must provide it (no legacy fallback path exists)",
                step_name="initialize_schemas",
            )

        schema_manager = SchemaManager(orch.state_service, plugin_schema_service)
        schema_manager.initialize_schemas(all_schemas)

        # Wire up SchemaManager to StateService for id_prefix lookups
        orch.state_service.set_schema_manager(schema_manager)

        logger.debug(f"✅ Initialized {len(all_schemas)} schemas (core + plugins + discovery)")
    else:
        raise StartupError("No schemas to initialize - this should never happen")


def _should_skip_plugin(name: str) -> bool:
    """Check if plugin should be skipped during startup."""
    return name in STATE_MANAGEMENT_PLUGINS


def _validate_scoped_key_shape(name: str, key: str) -> None:
    """Raise MalformedVaultKeyDeclarationError if shape doesn't match."""
    from ananta.interfaces.vault_keys_provider import (  # noqa: PLC0415
        MalformedVaultKeyDeclarationError,
    )
    parts = key.split(".", 2)
    if len(parts) < 3 or parts[1] != name:
        raise MalformedVaultKeyDeclarationError(
            f"plugin {name!r} declared key {key!r} which is not in "
            f"<solet>.{name}.<credential> form (plugin segment "
            f"must match the plugin's own name)",
        )


def _check_single_key_exists(name: str, key: str, proxy: Any) -> bool:
    """Return True iff vault reports the key exists; raise if vault errors."""
    from ananta.interfaces.vault_keys_provider import (  # noqa: PLC0415
        VaultServiceUnavailableError,
    )
    try:
        result = proxy.exists(key)
    except Exception as e:  # noqa: BLE001 — vault-substrate failures arrive as any exception
        raise VaultServiceUnavailableError(
            f"plugin {name!r} launch-key gate: vault_service.exists({key!r}) "
            f"raised {type(e).__name__}: {e}",
        ) from e
    data = result.get("data") if isinstance(result, dict) else None
    return bool(data and data.get("exists"))


def _report_missing_keys(name: str, missing: list[str]) -> None:
    """Apply VAULT_KEYS_GATE_MODE policy to the missing-key set."""
    from ananta.interfaces.vault_keys_provider import (  # noqa: PLC0415
        MissingVaultKeyError,
    )
    if VAULT_KEYS_GATE_MODE == "fail":
        raise MissingVaultKeyError(name, missing)
    for key in missing:
        logger.warning(
            "Plugin %r declared required vault key %r but vault has no "
            "matching entry. Launch-key gate is in WARN mode (sub-1 "
            "landing); plugin will load but the next runtime call that "
            "consumes this key will surface a not_found error. Flip to "
            "FAIL at sub-2 landing per W-VAULT-CALLER-ENFORCE.",
            name, key,
        )


def _check_vault_keys_for_plugin(name: str, plugin: Any) -> None:
    """Validate a plugin's declared required vault keys exist at readiness.

    W-PLUGIN-LAUNCH-KEYS (P0 Tier 2 sub-1, 2026-06-07):
    plugins opt in to launch-key gating by implementing
    ``get_required_vault_keys()`` from
    ``ananta.interfaces.vault_keys_provider.VaultKeysProvider``. The
    gate iterates the returned scoped keys, validates each declaration
    against the plugin's own name (per master plan §3.3.1 the plugin
    segment of ``<solet>.<plugin>.<credential>`` must match), and
    calls ``vault_service.exists()`` via the plugin's pre-injected
    caller-bound proxy.

    Mode (``VAULT_KEYS_GATE_MODE``):
      - ``"warn"`` (sub-1 default): missing keys log WARNING per key
        and the plugin loads. Today's lazy-fail semantics preserved.
      - ``"fail"`` (sub-2 default after W-VAULT-CALLER-ENFORCE): missing
        keys raise ``MissingVaultKeyError`` at the readiness boundary
        and the plugin is marked unhealthy.

    Raises:
        MalformedVaultKeyDeclarationError: declared key isn't in
            ``<solet>.{plugin.name}.<credential>`` shape (BOTH
            modes — declaration bugs are always fatal).
        MissingVaultKeyError: declared required key is absent in vault
            (FAIL mode only).
        VaultServiceUnavailableError: vault subsystem failed in a non-
            "missing key" way (BOTH modes — vault-down is always
            fatal).
    """
    required_method = getattr(plugin, "get_required_vault_keys", None)
    if not callable(required_method):
        return
    raw_required: Any = required_method()
    if not raw_required:
        return
    required: list[str] = [str(k) for k in raw_required]

    proxy = getattr(plugin, "_vault_service", None)
    if proxy is None:
        logger.debug(
            "Plugin %r declared required vault keys but has no "
            "_vault_service proxy injected; skipping launch-key gate.",
            name,
        )
        return

    missing: list[str] = []
    for key in required:
        _validate_scoped_key_shape(name, key)
        if not _check_single_key_exists(name, key, proxy):
            missing.append(key)

    if missing:
        _report_missing_keys(name, missing)


def _prepare_plugin_for_readiness(name: str, plugin: Any) -> None:
    """Prepare a single plugin for readiness.

    Raises PluginCapabilityError if preparation fails.
    """
    from ananta.core.plugins.exceptions import PluginCapabilityError

    if not hasattr(plugin, "prepare_for_readiness"):
        return

    try:
        plugin.prepare_for_readiness()
    except Exception as e:
        raise PluginCapabilityError(
            plugin_name=name,
            capability="prepare_for_readiness",
            operation="prepare_for_readiness",
            original_error=e,
        ) from e

    # W-PLUGIN-LAUNCH-KEYS: gate plugin readiness on declared required
    # vault keys. Plugin's own prepare_for_readiness ran first so any
    # dependency validation it performs surfaces ahead of the launch-
    # key check. Mode is warn-only at sub-1 landing; sub-2 flips to
    # fail.
    try:
        _check_vault_keys_for_plugin(name, plugin)
    except Exception as e:
        raise PluginCapabilityError(
            plugin_name=name,
            capability="vault_keys_provider",
            operation="check_required_vault_keys",
            original_error=e,
        ) from e


def _start_single_plugin_services(name: str, plugin: Any) -> None:
    """Start services for a single LifecycleManaged plugin.

    Raises PluginCapabilityError if start_services fails.
    """
    from ananta.core.plugins.exceptions import PluginCapabilityError

    try:
        if inspect.iscoroutinefunction(plugin.start_services):
            asyncio.run(plugin.start_services())
        else:
            plugin.start_services()

        _mark_plugin_ready_if_needed(name, plugin)
        logger.debug(f"Service plugin {name} started")
    except PluginCapabilityError:
        raise
    except Exception as e:
        raise PluginCapabilityError(
            plugin_name=name,
            capability="LifecycleManaged",
            operation="start_services",
            original_error=e,
        ) from e


def _mark_plugin_ready_if_needed(name: str, plugin: Any) -> None:
    """Mark plugin as ready if it didn't do so itself."""
    del name
    if not hasattr(plugin, "is_ready"):
        return
    if plugin.is_ready():
        return
    if hasattr(plugin, "set_ready"):
        plugin.set_ready()


def _start_service_plugins(orch: Any) -> None:
    """Start all service plugins (synchronously).

    Two-phase initialization:
        pass
    1. Call prepare_for_readiness() on ALL plugins that have it
    2. Call start_services() only on LifecycleManaged plugins

    Fails fast with PluginCapabilityError if any plugin fails.
    """
    from ananta.core.plugins.capabilities import is_lifecycle_managed

    # Phase 1: Prepare ALL plugins that have prepare_for_readiness
    for name, plugin in orch.plugin_manager.plugins.items():
        if _should_skip_plugin(name):
            continue
        _prepare_plugin_for_readiness(name, plugin)

    # Phase 2: Start services only for LifecycleManaged plugins
    started_count = 0
    for name, plugin in orch.plugin_manager.plugins.items():
        if _should_skip_plugin(name):
            continue
        if not is_lifecycle_managed(plugin):
            continue
        _start_single_plugin_services(name, plugin)
        started_count += 1

    logger.debug(f"Service plugins started: {started_count}")


def _verify_readiness(orch: Any) -> None:
    """Verify all service plugins are ready.

    Uses LifecycleManaged protocol to detect plugins requiring verification.
    """
    from ananta.core.plugins.capabilities import is_lifecycle_managed

    # Get active state plugin name - skip other state plugins from readiness check
    active_state_plugin = getattr(orch, "_state_plugin_name", None)

    unready = []
    for name, plugin in orch.plugin_manager.plugins.items():
        # Use LifecycleManaged protocol instead of hasattr check
        if not is_lifecycle_managed(plugin):
            continue

        # Skip non-active state plugins (they won't be initialized)
        if name in STATE_MANAGEMENT_PLUGINS and name != active_state_plugin:
            continue

        # Check is_running() from LifecycleManaged protocol
        if not plugin.is_running():
            error = getattr(plugin, "get_readiness_error", lambda: "Unknown error")()
            unready.append((name, error))
            logger.error(f"❌ Service plugin {name} not ready: {error}")

    if unready:
        error_details = ", ".join([f"{name}: {error}" for name, error in unready])
        raise StartupError(f"Service plugins not ready: {error_details}")

    logger.debug("✅ All service plugins verified ready")


def _validate_service_interfaces(orch: Any) -> None:
    """Validate all plugin-backed service bindings declare the correct interfaces.

    Iterates service bindings, resolves each plugin, and calls
    validate_plugin_interface() which checks:
    - Plugin declares expected interface in service_interfaces tuple
    - Plugin inherits from expected interface
    - Plugin's supported_interface_versions matches INTERFACE_VERSION

    Uses orch.plugin_manager.plugins[name] directly — NOT get_plugin(),
    which has a caller allowlist guard that blocks startup_sequence.py.
    """
    from ananta.core.orchestration.service_bindings import SERVICE_INTERFACE_MAP

    for service_name in SERVICE_INTERFACE_MAP:
        plugin_name = orch.service_bindings.get_plugin_name(service_name)
        if plugin_name is None:
            continue
        plugin = orch.plugin_manager.plugins.get(plugin_name)
        if plugin is None:
            raise RuntimeError(
                f"Service binding error: service '{service_name.value}' is bound to plugin "
                f"'{plugin_name}', but that plugin is not loaded. "
                f"Either install the plugin or remove the binding from service_bindings.json."
            )
        orch.service_bindings.validate_plugin_interface(service_name, plugin)

    logger.debug("All service plugin interfaces validated")


def _create_service_wrappers(orch: Any) -> None:
    """Create service wrappers (AFTER all service plugins are ready).

    Uses service bindings to resolve plugin names.
    See: ananta_build/2025-12-06_service_binding_architecture.md
    """
    from ananta.core.orchestration.service_bindings import ServiceName
    from ananta.services.discovery_service import DiscoveryService
    from ananta.services.embedding_service import EmbeddingService
    from ananta.services.inference_service import InferenceService
    from ananta.services.knowledge_service import KnowledgeService
    from ananta.services.memory_service import MemoryService
    from ananta.services.thinking_service import ThinkingService
    from ananta.services.vector_service import VectorService

    # VectorService - use service binding
    vector_plugin_name = orch.service_bindings.get_plugin_name(ServiceName.VECTOR_SERVICE)
    orch.vector_service = VectorService(
        orch.plugin_manager, vector_plugin_name=vector_plugin_name, state_service=orch.state_service
    )

    # EmbeddingService - use service binding
    embedding_plugin_name = orch.service_bindings.get_plugin_name(ServiceName.EMBEDDING_SERVICE)
    orch.embedding_service = EmbeddingService(
        orch.plugin_manager, embedding_plugin_name=embedding_plugin_name
    )

    # InferenceService - use service binding
    inference_plugin_name = orch.service_bindings.get_plugin_name(ServiceName.INFERENCE_SERVICE)
    orch.inference_service = InferenceService(
        orch.plugin_manager,
        inference_plugin_name=inference_plugin_name,
        app_home=orch.APP_HOME,
        state_service=orch.state_service,
        orchestrator=orch,
    )

    # DiscoveryService (embeddings-based process discovery)
    context_config = orch.inference_service.get_context_management_config()
    orch.discovery_service = DiscoveryService(
        app_home=orch.APP_HOME,
        state_service=orch.state_service,
        plugin_manager=orch.plugin_manager,
        embedding_service=orch.embedding_service,
        vector_service=orch.vector_service,
        min_similarity_threshold=context_config.discovery_min_similarity_threshold,
    )

    # ContextService (Phase 2 — read-only agent-context briefing; no inference).
    # Core service mirroring DiscoveryService: resolved via _DIRECT_ATTR_SERVICES →
    # orchestrator.get_service("context_service"), not a plugin binding. Reuses the
    # context_config computed above; its runtime path never touches inference.
    from ananta.services.context_service import ContextService

    orch.context_service = ContextService(
        app_home=orch.APP_HOME,
        context_config=context_config,
        orchestrator=orch,
        state_service=orch.state_service,
    )

    # AtCommandProcessor (shared @ command handler for all I/O interfaces)
    from ananta.core.services.at_command_processor import AtCommandProcessor

    orch.at_command_processor = AtCommandProcessor(app_home=Path(orch.APP_HOME))

    # MemoryService wrapper - required service (fails if not bound)
    memory_plugin_name = orch.service_bindings.get_plugin_name(ServiceName.MEMORY_SERVICE)
    orch.memory_service = MemoryService(
        orch.plugin_manager, memory_plugin_name=memory_plugin_name
    )

    # KnowledgeService wrapper - optional (None if not bound)
    knowledge_plugin_name = orch.service_bindings.get_plugin_name(ServiceName.KNOWLEDGE_SERVICE)
    if knowledge_plugin_name:
        orch.knowledge_service = KnowledgeService(
            orch.plugin_manager, knowledge_plugin_name=knowledge_plugin_name
        )
    else:
        orch.knowledge_service = None

    # ThinkingService wrapper - optional (None if not bound)
    thinking_plugin_name = orch.service_bindings.get_plugin_name(ServiceName.THINKING_SERVICE)
    if thinking_plugin_name:
        orch.thinking_service = ThinkingService(
            orch.plugin_manager, thinking_plugin_name=thinking_plugin_name
        )
    else:
        orch.thinking_service = None


def _init_service_manager(orch: Any) -> None:
    """Initialize ServiceManager with all core services.

    Uses service bindings to resolve plugin names.
    See: ananta_build/2025-12-06_service_binding_architecture.md
    """
    from ananta.core.orchestration.service_bindings import ServiceName
    from ananta.core.orchestration.service_manager import ServiceManager

    # Get plugin_config from orchestrator
    plugin_config = getattr(orch, "plugin_operational_config", None)
    default_inference_provider = getattr(orch, "default_inference_provider", None)
    session_timeout_hours = getattr(orch, "_session_timeout_hours", 1)

    # Get state plugin name from service bindings
    state_plugin_name = orch.service_bindings.get_plugin_name(ServiceName.STATE_SERVICE)

    orch.service_manager = ServiceManager(
        plugin_config=plugin_config,
        default_inference_provider=default_inference_provider,
        session_timeout_hours=session_timeout_hours,
        state_plugin_name=state_plugin_name,
    )

    # Initialize all services
    orch.service_manager.initialize_all_services(orch, orch.event_bus)

    # Delegate service attributes for compatibility
    orch.state_service = orch.service_manager.state_service
    orch.state_manager = orch.service_manager.state_manager
    orch.async_job_manager = orch.service_manager.async_job_manager
    orch.blob_storage_service = orch.service_manager.blob_storage_service
    orch.session_manager = orch.service_manager.session_manager
    orch.flow_manager = orch.service_manager.flow_manager
    orch.action_recorder = orch.service_manager.action_recorder
    orch.unified_metadata_registry = orch.service_manager.unified_metadata_registry

    # Wire memory service to context management (required)
    context_management_service = getattr(orch.service_manager, "context_management_service", None)
    if context_management_service:
        context_management_service.set_memory_service(orch.memory_service)

    logger.debug("✅ ServiceManager initialized")


_PROBE_MODE_ENV_VAR = "SOLET_PROBE_MODE"
_PROBE_READY_FILENAME = "probe_ready"


def _probe_exit_if_in_probe_mode(orch: Any) -> None:
    """L2 probe checkpoint: terminate cleanly if running as a boot probe.

    When ``SOLET_PROBE_MODE=1`` is set in the environment, the platform is
    running as a sandboxed boot probe spawned by
    ``macos_self_deployment_plugin`` (Architect's local blue/green
    design §3). At this point every service plugin has been started,
    every wiring step has completed, and every plugin's
    ``initialize(config)`` has run — the boot graph is structurally
    validated. Anything beyond ``init_actions`` (action queue pollers,
    IO interfaces, background work) would either bind the canonical
    bridge port or spawn long-lived threads that would prevent a clean
    process exit; the probe MUST exit before those.

    Side effects on probe success:
    * Write ``<APP_HOME>/probe_ready`` so the parent
      ``macos_self_deployment_plugin`` swap orchestrator observes the
      success marker.
    * ``os._exit(0)`` — NOT ``SystemExit`` — because
      ``start_service_plugins`` already spawned LifecycleManaged plugin
      threads/pools. ``SystemExit`` would walk those threads' cleanup
      paths and can hang on long-running pools; ``_exit`` bypasses them
      cleanly, which is what we want for a sandboxed scratch process.

    No-op when ``SOLET_PROBE_MODE`` is unset — production boots continue
    through ``init_actions`` unchanged.
    """
    if os.environ.get(_PROBE_MODE_ENV_VAR) != "1":
        return
    marker = Path(orch.APP_HOME) / _PROBE_READY_FILENAME
    marker.write_text(
        f"probe_ready_at={datetime.now(UTC).isoformat()}\n",
        encoding="utf-8",
    )
    logger.info(
        "L2 probe reached init_actions threshold cleanly; "
        "writing %s and exiting before action processing begins.",
        marker,
    )
    os._exit(0)


def _init_actions(orch: Any) -> None:
    """Initialize action coordination system with full dependency injection."""
    from ananta.core.orchestration.action_coordinator import ActionCoordinator

    orch.action_coordinator = ActionCoordinator()

    # Build services dict for ActionCoordinator
    services_dict = {
        "app_home": orch.APP_HOME,
        "plugin_manager": orch.plugin_manager,
        "state_service": orch.state_service,
        "state_manager": orch.state_manager,
        "async_job_manager": orch.async_job_manager,
        "event_bus": orch.event_bus,
        "event_handler_registry": orch.event_handler_registry,
        "unified_metadata_registry": orch.unified_metadata_registry,
        "action_recorder": orch.action_recorder,
        "session_manager": orch.session_manager,
        "flow_manager": orch.flow_manager,
        "flow_runtime_graph": orch.service_manager.flow_runtime_graph,
        "blob_storage_service": orch.blob_storage_service,
        "inference_service": orch.service_manager.inference_service,
        "embedding_service": orch.service_manager.embedding_service,
        "vector_service": orch.service_manager.vector_service,
        "discovery_service": orch.discovery_service,
        "memory_service": orch.memory_service,
        "knowledge_service": orch.knowledge_service,
        "io_interface_service": orch.io_interface_service,
    }

    # Fully initialize ActionCoordinator including action_factory injection into plugins
    orch.action_coordinator.initialize_action_components(
        orchestrator_ref=orch, services_dict=services_dict
    )

    # Delegate action attributes for compatibility
    orch.action_factory = orch.action_coordinator.action_factory
    orch.action_manager = orch.action_coordinator.action_manager
    orch.action_queue_poller = orch.action_coordinator.action_queue_poller
    orch.event_processor = orch.action_coordinator.event_processor

    # Wire AsyncJobManager to ActionFactory for automatic completion routing
    async_job_manager = getattr(orch, "async_job_manager", None)
    if async_job_manager and hasattr(async_job_manager, "set_action_factory"):
        action_factory = getattr(orch, "action_factory", None)
        if action_factory is not None:
            async_job_manager.set_action_factory(action_factory)

    _verify_action_factory_injected_into_scheduling_plugin(orch)

    logger.debug("✅ ActionCoordinator initialized and action_factory injected into all plugins")


def _verify_action_factory_injected_into_scheduling_plugin(orch: Any) -> None:
    """Fail-loud if `default_scheduling_plugin._action_executor` is None post-injection.

    W5.K backstop: between 2026-06-12 20:35 PT and 2026-06-13 11:51 PT (PT) the live
    the solet had `default_scheduling_plugin._action_executor=None`, causing every cron
    callback to fail silently (only an ERROR log line; nothing surfaced to operator
    sessions). The condition was state corruption from a prior blue-green cycle and
    self-healed at the 11:51 cutover, but the silent-failure window was 22 hours.
    This assertion converts the failure mode from "silent for 22 hours" to
    "platform startup fails loudly" so the next occurrence is caught immediately.
    """
    scheduling_plugin = orch.plugin_manager.plugins.get("default_scheduling_plugin")
    if scheduling_plugin is None:
        return  # plugin not in this manifest; nothing to verify
    action_executor = getattr(scheduling_plugin, "_action_executor", None)
    if action_executor is None:
        raise StartupError(
            "default_scheduling_plugin._action_executor is None after init_actions. "
            "Cron callbacks would silently fail (SCHEDULER-CALLBACK-ERROR for every fire). "
            "The action_factory injection chain in ActionCoordinator._inject_plugin_services "
            "did not reach the scheduling plugin. See workbench/"
            "2026-06-13_scheduler_callback_actionfactory_not_injected_p0.md."
        )


def _health_report(orch: Any) -> None:
    """Log a per-plugin health line for every LifecycleManaged plugin.

    Final non-critical step in the startup sequence. Uses the LifecycleManaged
    protocol for detection and logs at INFO so the operator can confirm at-a-glance
    which plugins came up cleanly.
    """
    from ananta.core.plugins.capabilities import is_lifecycle_managed

    for name, plugin in orch.plugin_manager.plugins.items():
        if not is_lifecycle_managed(plugin):
            continue
        running = plugin.is_running()
        status = "✅ RUNNING" if running else "❌ NOT RUNNING"
        error = ""
        if not running and hasattr(plugin, "get_readiness_error"):
            error = f" ({plugin.readiness_error or 'Unknown error'})"
        logger.info(f"{name}: {status}{error}")

    hydration_status = getattr(orch, "knowledge_hydration_status", None)
    if hydration_status is not None:
        logger.info("knowledge-base hydration: %s", hydration_status)


def _seed_identity_memories(orch: Any) -> None:
    """Seed identity memories from config if not already seeded.

    Reads identity strings from profile/config/identity.json and stores them
    in memory service with the identity tag. This ensures identity memory
    has meaningful content for prompt context.

    Fails fast if identity.json is missing or empty.
    """
    from ananta.core.services.prompt_context_builder import IDENTITY_TAG

    identity_config_path = Path(orch.APP_HOME) / "config" / "identity.json"
    identity_items = _load_identity_config(identity_config_path)

    if not hasattr(orch, "memory_service") or not orch.memory_service:
        raise StartupError("memory_service not available for identity seeding")

    # Always clear and reseed to ensure identity.json changes take effect
    _clear_identity_memories(orch.memory_service, IDENTITY_TAG)

    # Seed identity memories
    logger.info(f"Seeding {len(identity_items)} identity memories from {identity_config_path}")
    seeded_count = _seed_identity_items(orch.memory_service, identity_items, IDENTITY_TAG)

    if seeded_count == 0:
        raise StartupError(
            f"Seeded 0 identity memories from {identity_config_path}.\n"
            f"Items in config: {len(identity_items)}\n"
            "This should not happen - validation should have caught invalid items."
        )

    _verify_identity_seeding(orch.memory_service, IDENTITY_TAG, seeded_count)
    logger.info(f"✅ Seeded {seeded_count} identity memories")


def _load_identity_config(config_path: Path) -> list[Any]:
    """Load and validate identity config file.

    Args:
        config_path: Path to identity.json

    Returns:
        List of identity items (runtime validated as strings in _seed_identity_items)

    Raises:
        StartupError: If config missing or empty
    """
    if not config_path.exists():
        raise StartupError(
            f"Identity config not found: {config_path}. "
            "Create profile/config/identity.json with identity strings."
        )

    with open(config_path) as f:
        config = json.load(f)

    identity_items = config.get("identity", [])
    if not identity_items:
        raise StartupError(
            f"Identity config is empty: {config_path}. "
            "Add identity strings to the 'identity' array."
        )

    if not isinstance(identity_items, list):
        raise StartupError(
            f"Identity config 'identity' must be a list: {config_path}"
        )

    return identity_items


def _clear_identity_memories(memory_service: Any, identity_tag: str) -> int:
    """Clear all existing identity memories before reseeding.

    This ensures identity.json changes always take effect on startup.
    Uses delete_memories_by_tag() for hard deletion of ALL memories with the
    identity tag, including archived ones that can't be found via recall().

    Args:
        memory_service: MemoryService for delete_memories_by_tag
        identity_tag: Tag to filter identity memories

    Returns:
        Count of cleared memories
    """
    result = memory_service.delete_memories_by_tag(identity_tag)

    if "error" in result:
        logger.warning(f"Failed to clear identity memories: {result['error']}")
        return 0

    cleared_count: int = result.get("deleted_count", 0)

    if cleared_count > 0:
        logger.info(f"Cleared {cleared_count} existing identity memories")
    else:
        logger.debug("No existing identity memories to clear")

    return cleared_count


def _seed_identity_items(
    memory_service: Any, items: list[Any], identity_tag: str
) -> int:
    """Seed identity items into memory service.

    Args:
        memory_service: MemoryService for remember()
        items: List of identity items (validated at runtime)
        identity_tag: Tag to apply to memories

    Returns:
        Count of successfully seeded items

    Raises:
        StartupError: If any item is invalid (not a non-empty string)
    """
    seeded_count = 0
    for i, item in enumerate(items):
        # Validate item is a non-empty string - fail fast with precise error
        if not isinstance(item, str):
            raise StartupError(
                f"identity.json item[{i}] must be a string, got {type(item).__name__}: {item!r}"
            )
        if not item.strip():
            raise StartupError(
                f"identity.json item[{i}] is empty or whitespace-only: {item!r}"
            )

        result = memory_service.remember(
            content=item,
            tags=[identity_tag],
        )
        if "error" in result:
            raise StartupError(
                f"Failed to store identity.json item[{i}] in memory service.\n"
                f"Content: {item!r}\n"
                f"Error: {result['error']}"
            )
        if "memory_id" in result:
            seeded_count += 1
            logger.debug(f"Seeded identity: {item[:50]}...")

    return seeded_count


def _verify_identity_seeding(
    memory_service: Any, identity_tag: str, seeded_count: int
) -> None:
    """Verify that identity seeding succeeded using direct tag lookup.

    Uses get_memories_by_tag for deterministic verification instead of
    semantic search, which can fail if the query doesn't match content.

    Args:
        memory_service: MemoryService for get_memories_by_tag
        identity_tag: Tag to filter identity memories
        seeded_count: Expected count of seeded items

    Raises:
        StartupError: If verification fails (with precise diagnostic info)
    """
    verify = memory_service.get_memories_by_tag(tag=identity_tag)

    # Check for error
    if "error" in verify:
        raise StartupError(
            f"Identity verification failed.\n"
            f"Tag: '{identity_tag}'\n"
            f"Error: {verify['error']}"
        )

    memories = verify.get("memories", [])
    if not memories:
        raise StartupError(
            f"Identity verification failed: no memories found with tag '{identity_tag}'.\n"
            f"Seeded: {seeded_count} items\n"
            f"Result: {verify}\n"
            "\n"
            "This means memories were stored but not retrievable.\n"
            "Check database connectivity and state service configuration."
        )


def _reindex_orphaned_memories(orch: Any) -> None:
    """Reindex memories that exist but have no embeddings. Fails fast."""
    from ananta.core.domain.enums import ActionStatus

    if not hasattr(orch, "memory_service") or orch.memory_service is None:
        raise StartupError("memory_service not available for reindexing")

    result = orch.memory_service.reindex_orphaned_vectors()

    if result.get("action_status") == ActionStatus.COMPLETED.value:
        data = result.get("data", {})
        reindexed = data.get("reindex_count", 0)
        message = data.get("message")

        if message:
            logger.info(message)
        elif reindexed > 0:
            logger.info(f"Reindexed {reindexed} orphaned memories")
    else:
        raise StartupError(f"Reindex failed: {result.get('error', 'Unknown')}")


def _handle_clean_restart(orch: Any) -> None:
    """Purge all memories if ANANTA_CLEAN_RESTART=true. Fails fast."""
    import os

    from ananta.core.domain.enums import ActionStatus

    clean_restart = os.environ.get("ANANTA_CLEAN_RESTART", "").lower() == "true"
    if not clean_restart:
        return

    logger.warning("ANANTA_CLEAN_RESTART=true - purging all memories and embeddings")

    if not hasattr(orch, "memory_service") or orch.memory_service is None:
        raise StartupError("Cannot perform clean restart: memory_service not available")

    result = orch.memory_service.purge_memories(confirm=True)

    if result.get("action_status") != ActionStatus.COMPLETED.value:
        raise StartupError(f"Clean restart purge failed: {result.get('error', 'Unknown')}")

    purged = result.get("data", {}).get("purged_count", 0)
    logger.info(f"Clean restart complete: purged {purged} memories and embeddings")

    # Clear the env var so subsequent restarts don't re-purge
    os.environ.pop("ANANTA_CLEAN_RESTART", None)


def _auto_install_knowledge_bases(orch: Any) -> None:
    """Start knowledge-base hydration after clean restart has completed.

    Runs AFTER handle_clean_restart so that embeddings aren't purged
    immediately after creation. The installation itself is background work:
    first-boot ingestion must not delay the runtime's router-registration
    heartbeat or make setup's target-health wait expire while the child is
    otherwise live. Accesses the knowledge plugin directly through
    plugin_manager.

    Skipped when ``SOLET_PROBE_MODE=1``: the L2 probe shares the live
    Postgres per Architect's 2026-05-30 design §3.2, so install
    records, embeddings, and KB chunks set by the running solet are
    already visible. Re-running auto-install in the probe duplicates
    that work and currently pushes boot past the 120s probe ceiling
    (2026-06-01 investigation; compositions KB alone takes ~109s when
    ``has_valid_install`` falsely returns False — tracked separately
    as coordinator_plan.md §5 task #18, parallel dispatch). The probe's
    purpose is boot-graph validation, not data hydration.
    """
    if os.environ.get(_PROBE_MODE_ENV_VAR) == "1":
        logger.info(
            "auto_install_knowledge_bases skipped: SOLET_PROBE_MODE=1 "
            "(probe shares live Postgres; install records already present)",
        )
        return

    from ananta.core.orchestration.service_bindings import ServiceName

    knowledge_plugin_name = orch.service_bindings.get_plugin_name(
        ServiceName.KNOWLEDGE_SERVICE
    )
    if not knowledge_plugin_name:
        return

    plugin = orch.plugin_manager.plugins.get(knowledge_plugin_name)
    if plugin is None:
        return

    if hasattr(plugin, "auto_install_knowledge_bases"):
        # Forward the manifest plugin set so the lifecycle helper can
        # auto-uninstall plugin-owned KBs whose plugin has left the
        # manifest. ``load_manifest_plugin_set`` returns ``None`` when
        # the manifest is absent — that's the "load everything
        # installed" default and disables the auto-uninstall pass
        # symmetrically (no manifest → no filter).
        from ananta.core.plugins.profile_manifest import load_manifest_plugin_set
        manifest_plugin_set = load_manifest_plugin_set(orch.APP_HOME)
        hydration_thread = threading.Thread(
            target=_run_auto_install_knowledge_bases,
            args=(orch, plugin, manifest_plugin_set),
            name="knowledge-base-auto-install",
            daemon=True,
        )
        hydration_thread.start()


def _run_auto_install_knowledge_bases(
    orch: Any,
    plugin: Any,
    manifest_plugin_set: set[str] | None,
    *,
    sleep: Callable[[float], None] = time.sleep,
) -> None:
    """Retry optional KB hydration without withdrawing startup readiness."""
    retry_seconds = _KNOWLEDGE_HYDRATION_RETRY_INITIAL_SECONDS
    for attempt in range(1, _KNOWLEDGE_HYDRATION_MAX_ATTEMPTS + 1):
        orch.knowledge_hydration_status = {"state": "pending", "attempt": attempt}
        try:
            plugin.auto_install_knowledge_bases(manifest_plugin_set=manifest_plugin_set)
        except Exception as exc:
            if attempt == _KNOWLEDGE_HYDRATION_MAX_ATTEMPTS:
                orch.knowledge_hydration_status = {
                    "state": "failed",
                    "attempt": attempt,
                    "error": str(exc),
                }
                logger.exception(
                    "Knowledge-base auto-install failed after %d attempts; "
                    "startup readiness remains available",
                    attempt,
                )
                return
            logger.warning(
                "Knowledge-base auto-install attempt %d/%d failed: %s; retrying in %.1fs",
                attempt,
                _KNOWLEDGE_HYDRATION_MAX_ATTEMPTS,
                exc,
                retry_seconds,
            )
            sleep(retry_seconds)
            retry_seconds = min(
                retry_seconds * 2,
                _KNOWLEDGE_HYDRATION_RETRY_MAX_SECONDS,
            )
        else:
            orch.knowledge_hydration_status = {"state": "complete", "attempt": attempt}
            logger.info("Knowledge-base auto-install completed on attempt %d", attempt)
            return


def _defer_inference_dependent_startup_work(orch: Any) -> None:
    """Declare work that must wait until this process is router-active."""
    orch.post_registration_work_status = {"state": "pending"}


def start_post_registration_work(orch: Any) -> None:
    """Start inference-dependent startup work exactly once after activation."""
    lock = getattr(orch, "_post_registration_work_lock", None)
    if lock is None:
        lock = threading.Lock()
        orch._post_registration_work_lock = lock
    with lock:
        if getattr(orch, "_post_registration_work_started", False):
            return
        orch._post_registration_work_started = True
        threading.Thread(
            target=_run_post_registration_work,
            args=(orch,),
            name="post-registration-startup-work",
            daemon=True,
        ).start()


def _run_post_registration_work(orch: Any) -> None:
    """Sequence local-model work after activation without delaying router health."""
    try:
        plugins = orch.plugin_manager.plugins
        embeddings = plugins.get("openai_embeddings_plugin")
        if embeddings is not None:
            start_qualification = getattr(embeddings, "start_post_registration_qualification", None)
            wait_qualification = getattr(embeddings, "wait_for_post_registration_qualification", None)
            if not callable(start_qualification) or not callable(wait_qualification):
                raise RuntimeError("embedding plugin lacks post-registration qualification hooks")
            start_qualification()
            wait_qualification()

        action_coordinator = getattr(orch, "action_coordinator", None)
        if action_coordinator is not None:
            action_coordinator.populate_discovery_after_registration()
        _seed_identity_memories(orch)
        _reindex_orphaned_memories(orch)
        _auto_install_knowledge_bases(orch)

        inference = plugins.get("default_inference_plugin")
        if inference is not None:
            start_inference = getattr(inference, "start_post_registration_work", None)
            if not callable(start_inference):
                raise RuntimeError("inference plugin lacks post-registration hook")
            start_inference()
    except Exception as exc:
        orch.post_registration_work_status = {"state": "failed", "error": str(exc)}
        logger.exception("Post-registration startup work failed")
    else:
        orch.post_registration_work_status = {"state": "started"}


def _auto_register_declared_pulling_sources(service: Any) -> None:
    """Boot-register every plugin-declared PULLING source that ships a default
    root_uri and is not already registered.

    A filesystem ``root_uri`` denied by the secure-default ledger authz gate
    (P1.1.E — it needs an operator-configured ``ledger_allowed_roots`` entry AND
    an operator-equivalent principal, neither of which a FRESH solet boot
    has for a seed's ``~/.claude/*`` sources) is EXPECTED and skipped NON-fatally:
    the ledger ingests nothing from that source until the operator opts it in via
    ``ledger_allowed_roots`` during hydration. A hard raise here would crash-loop
    the newborn's whole boot. The origin solet is unaffected — its sources are already
    registered (the existing-kind skip below).
    """
    from ananta.interfaces.llm_session_source_interface import PullingSourceMixin
    from ananta.llm.session_ledger.types import IngestMode
    # Defensive double-guard vs register_source's own idempotency (Coordinator Q1
    # ruling 2026-05-31): skip a source_kind whose row already exists.
    existing_kinds = {row.source_kind for row in service._repository.list_sources(  # noqa: SLF001
        enabled_only=False,
    )}
    for descriptor in service.registry.list_sources():
        if IngestMode.PULLING not in descriptor.supported_modes:
            continue
        if descriptor.default_pulling_root_uri is None:
            continue
        if descriptor.source_kind in existing_kinds:
            continue
        plugin = service.registry.get_by_kind(descriptor.source_kind)
        if plugin is None or not isinstance(plugin, PullingSourceMixin):
            continue
        result = service._register_source_internal(  # noqa: SLF001
            source_kind=descriptor.source_kind.value,
            root_uri=descriptor.default_pulling_root_uri,
            account_label=None,
            config_json=None,
        )
        logger.info(
            "auto-registered ledger source kind=%s source_id=%s root_uri=%s outcome=%s",
            descriptor.source_kind.value,
            result.get("source_id"),
            descriptor.default_pulling_root_uri,
            result.get("outcome"),
        )


def _init_session_ledger_service(orch: Any) -> None:
    """Construct SessionLedgerService once, attach to orchestrator.

    Depends on ``create_service_wrappers`` (so ``orch.blob_storage_service``
    has been delegated from the service_manager) and ``start_service_plugins``
    (so source plugins are loaded + ready before the Registry collects them).

    Spec §12.2 (Codex HIGH 8 ordering fix). M1.v2 (Architect Q2 ruling):
    auto-register every self-bootstrapping PULLING source so the M1 acceptance
    surface (list_sources / trigger_poll) is meaningful without an operator
    register_source call. Filesystem-rooted sources advertise no
    ``default_pulling_root_uri`` and stay operator-bridge-driven.
    """
    from ananta.services.session_ledger_service import SessionLedgerService

    # M6 services are optional — cloud profiles can skip embedding / vector
    # bindings and still get M1-M5 surface. ``SummaryWriter`` fails closed
    # when these are None.
    embedding_service = orch.get_service("embedding_service")
    vector_service = orch.get_service("vector_service")
    # M5.C deferral #4: optional scheduling_service for periodic_poll auto-pacing.
    # ``ensure_periodic_poll_schedule`` raises cleanly when not bound; profiles
    # without scheduling skip the boot starting_action.
    scheduling_service = orch.get_service("scheduling_service")
    # 2026-05-31 Gap 2(A): inference_service for the auto-summarize cron.
    # ``summarize_quiescent_sessions`` raises cleanly when not bound.
    inference_service = orch.get_service("inference_service")
    # P1.1.E authz containment: operator-configured allow-list for filesystem
    # ``root_uri`` registrations through the public ``register_source`` verb.
    # Absent → empty list → every filesystem registration through the public
    # verb is denied (the boot auto-register + export paths use the trusted
    # internal seam and are unaffected).
    ledger_config = orch.config_manager.get_plugin_config("session_ledger_service")
    raw_allowed_roots = ledger_config.get("ledger_allowed_roots", [])
    ledger_allowed_roots = (
        [str(root) for root in raw_allowed_roots]
        if isinstance(raw_allowed_roots, list)
        else []
    )
    service = SessionLedgerService(
        state_service=orch.state_service,
        blob_storage_service=orch.blob_storage_service,
        plugin_manager=orch.plugin_manager,
        embedding_service=embedding_service,
        vector_service=vector_service,
        scheduling_service=scheduling_service,
        inference_service=inference_service,
        ledger_allowed_roots=ledger_allowed_roots,
    )
    orch.session_ledger_service = service

    _auto_register_declared_pulling_sources(service)

    logger.debug("✅ session_ledger_service initialized")


def _init_io_interface_service(orch: Any) -> None:
    """Initialize IO Interface Service for session-based message routing.

    Uses IOInterface protocol for detection.
    """
    from ananta.core.plugins.capabilities import is_io_interface
    from ananta.services.io_interface_service import IOInterfaceRegistry, IOInterfaceService

    registry = IOInterfaceRegistry()

    # Register all IO interface plugins using IOInterface protocol
    # Note: IOInterface and IOInterfacePluginProtocol have different signatures
    # but plugins implement both interfaces, so cast is safe at runtime
    from ananta.services.io_interface_service.registry import IOInterfacePluginProtocol

    for plugin in orch.plugin_manager.plugins.values():
        if is_io_interface(plugin):
            registry.register(cast(IOInterfacePluginProtocol, plugin))

    orch.io_interface_service = IOInterfaceService(
        registry=registry,
        state_service=orch.state_service,
        app_home=orch.APP_HOME,
        async_job_manager=getattr(orch, "async_job_manager", None),
    )

    logger.debug(
        f"✅ IO Interface Service initialized with {len(registry.get_namespaces())} plugins"
    )


# ==============================================================================
# STARTUP SEQUENCE DEFINITION
# ==============================================================================

STARTUP_SEQUENCE = [
    StartupStep("load_config", _load_config, []),
    StartupStep("create_event_system", _create_event_system, ["load_config"]),
    StartupStep("init_plugin_manager", _init_plugin_manager, ["create_event_system"]),
    StartupStep("load_service_bindings", _load_service_bindings, ["init_plugin_manager"]),
    # Inject the state plugin's vault proxy BEFORE its foundational pool-open
    # (operator mandate: interface-only credential access). Must precede
    # start_state_plugin, whose prepare_for_readiness opens the first pool
    # using the DB password read through vault_service. The general
    # inject_vault_service step (later) re-injects every consumer including
    # the state plugin — idempotent and harmless (pool already open).
    StartupStep(
        "inject_state_vault_service",
        _inject_state_vault_service,
        ["load_service_bindings"],
    ),
    StartupStep(
        "start_state_plugin",
        _start_state_plugin,
        ["load_service_bindings", "inject_state_vault_service"],
    ),
    StartupStep(
        "create_state_service_wrapper", _create_state_service_wrapper, ["start_state_plugin"]
    ),
    StartupStep("inject_dependencies", _inject_dependencies, ["create_state_service_wrapper"]),
    StartupStep("initialize_schemas", _initialize_schemas, ["inject_dependencies"]),
    # W-VAULT-INTERFACE-EXTEND Phase D-1: inject caller-bound VaultServiceProxy
    # into every plugin that defines ``set_vault_service``. Runs BEFORE
    # ``start_service_plugins`` so consumers can resolve ``self._vault_service``
    # during their own ``prepare_for_readiness``.
    StartupStep("inject_vault_service", _inject_vault_service, ["initialize_schemas"]),
    StartupStep(
        "start_service_plugins",
        _start_service_plugins,
        ["initialize_schemas", "inject_vault_service"],
    ),
    StartupStep("verify_readiness", _verify_readiness, ["start_service_plugins"]),
    StartupStep("validate_service_interfaces", _validate_service_interfaces, ["verify_readiness"]),
    StartupStep("create_service_wrappers", _create_service_wrappers, ["validate_service_interfaces"]),
    StartupStep(
        "initialize_plugin_configs",
        _initialize_plugin_configs,
        ["create_service_wrappers"],
    ),
    StartupStep("handle_clean_restart", _handle_clean_restart, ["create_service_wrappers"]),
    StartupStep(
        "defer_inference_dependent_startup_work",
        _defer_inference_dependent_startup_work,
        ["handle_clean_restart"],
    ),
    StartupStep(
        "inject_at_command_processor", _inject_at_command_processor, ["create_service_wrappers"]
    ),
    StartupStep(
        "inject_compilation_context_builder",
        _inject_compilation_context_builder,
        ["create_service_wrappers"],
    ),
    StartupStep("init_service_manager", _init_service_manager, ["create_service_wrappers"]),
    # inject_memory_service must run AFTER init_service_manager so context_management_service exists
    StartupStep("inject_memory_service", _inject_memory_service, ["init_service_manager"]),
    StartupStep("inject_flow_manager", _inject_flow_manager, ["init_service_manager"]),
    StartupStep("inject_session_manager", _inject_session_manager, ["init_service_manager"]),
    StartupStep(
        "inject_context_management_service",
        _inject_context_management_service,
        ["init_service_manager"],
    ),
    StartupStep(
        "init_io_interface_service", _init_io_interface_service, ["create_service_wrappers"]
    ),
    StartupStep(
        "init_session_ledger_service",
        _init_session_ledger_service,
        ["create_service_wrappers", "start_service_plugins"],
    ),
    # L2 probe checkpoint — runs only when SOLET_PROBE_MODE=1. Probe spawns
    # via macos_self_deployment_plugin (Architect's local blue/green
    # design §3); on production boots this step no-ops and the sequence
    # continues into init_actions normally.
    StartupStep(
        "probe_exit_if_in_probe_mode",
        _probe_exit_if_in_probe_mode,
        [
            "init_service_manager",
            "inject_at_command_processor",
            "inject_flow_manager",
            "inject_session_manager",
            "inject_compilation_context_builder",
            "inject_memory_service",
            "init_io_interface_service",
            "initialize_plugin_configs",
        ],
    ),
    StartupStep(
        "init_actions",
        _init_actions,
        [
            "init_service_manager",
            "inject_at_command_processor",
            "inject_flow_manager",
            "inject_session_manager",
            "inject_compilation_context_builder",
            "inject_memory_service",
            "init_io_interface_service",
            # plugin.initialize(config) binds config_provider on every
            # orchestrator-owned plugin instance; gate action processing
            # behind it so handlers that consult config_provider at
            # dispatch time (e.g. session-source root_dir lookup) find it
            # populated rather than racing the post-boot init step.
            "initialize_plugin_configs",
            "probe_exit_if_in_probe_mode",
        ],
    ),
    StartupStep("health_report", _health_report, ["init_actions"], critical=False),
]
