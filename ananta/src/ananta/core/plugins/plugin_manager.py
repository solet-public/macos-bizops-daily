"""Plugin registry orchestrator.

Decomposed during Step 9.C
(design record, Step 9.C, dev-checkout workbench — not part of the shipped tree)
into three focused collaborators:

  - `PluginDiscovery` — finds plugin classes from entry points.
  - `PluginInitializer` — instantiates classes, injects services, runs
    readiness preparation + the config-driven `initialize` call, and
    exposes readiness query methods.
  - `PluginDispatcher` — serves `get_plugin` lookups and `execute_action`
    dispatches, enforcing that service-provider plugins are accessed
    only through their service interfaces.

`PluginManager` keeps the canonical `plugins: dict[str, PluginBase]`
registry (passed by reference into each collaborator), the
`PluginValidationRegistry` instance, and the orchestrator / event-bus /
config-manager / allowed-plugins state. All 15 public methods preserved
as one- or two-line delegates so callers (`service_manager.py`,
`chat_interface_support.py`, etc.) need no changes.
"""

from __future__ import annotations

import inspect
import logging
from typing import TYPE_CHECKING, Protocol

from ananta.core.plugins.capabilities import is_lifecycle_managed
from ananta.core.plugins.plugin_base import (
    EventBusProtocol,
    OrchestratorProtocol,
    PluginBase,
    PluginReadiness,
)
from ananta.core.plugins.plugin_discovery import PluginDiscovery
from ananta.core.plugins.plugin_dispatcher import PluginDispatcher
from ananta.core.plugins.plugin_initializer import PluginInitializer
from ananta.core.plugins.plugin_installer import PluginInstaller, PluginInstallError

# Removed: from ananta.core.plugins.plugin_contracts import get_required_interface
# Now using explicit isinstance() checks instead of magic string patterns
from ananta.core.plugins.plugin_validation import PluginValidationRegistry

if TYPE_CHECKING:
    from ananta.types.schema_types import SchemaDefinition

logger = logging.getLogger(__name__)


class ConfigManagerProtocol(Protocol):
    """Protocol defining the interface for config managers with get_plugin_config method."""

    def get_plugin_config(
        self, plugin_name: str, default_config: dict[str, object] | None = None
    ) -> dict[str, object]:
        """Get plugin configuration by name."""
        ...


class PluginManager:
    """Orchestrator wiring three plugin-lifecycle collaborators.

    Construction is cheap; the manager constructs `PluginDiscovery`,
    `PluginInitializer`, and `PluginDispatcher` once and threads the
    shared `plugins` registry + validation registry + late-bound
    orchestrator-ref accessor through.
    """

    def __init__(self) -> None:
        self.plugins: dict[str, PluginBase] = {}
        self._orchestrator_ref: OrchestratorProtocol | None = None
        self._event_bus_ref: EventBusProtocol | None = None
        self._config_manager: ConfigManagerProtocol | None = None
        self._allowed_plugins: set[str] | None = None
        self._is_serving = False
        self.plugin_validation_registry = PluginValidationRegistry()

        # Collaborators (Step 9.C). Each receives the shared `plugins` dict
        # by reference so mutations are observable across boundaries.
        self._initializer = PluginInitializer(
            plugins=self.plugins,
            validation_registry=self.plugin_validation_registry,
        )
        self._discovery = PluginDiscovery()
        self._dispatcher = PluginDispatcher(
            plugins=self.plugins,
            get_orchestrator_ref=lambda: self._orchestrator_ref,
        )
        # Fourth Step-9.C collaborator (C1 atomicity): the ONLY sanctioned
        # runtime mutator of `self.plugins`. Exposed as a public attribute
        # (not a delegate method) so PluginManager's non-process public-method
        # count stays at its god-class threshold; the accessor lambdas mirror
        # the PluginDispatcher precedent above.
        self.installer = PluginInstaller(
            plugins=self.plugins,
            discovery=self._discovery,
            initializer=self._initializer,
            get_config_manager=lambda: self._config_manager,
            get_allowed_plugins=lambda: self._allowed_plugins,
            set_allowed_plugins=self._set_allowed_plugins,
        )

    # ------------------------------------------------------------------
    # Service injection setters — these mutate the manager's own state AND
    # propagate to every loaded plugin (kept inline because there's no
    # collaborator-side mutation; just iterate self.plugins).
    # ------------------------------------------------------------------

    def set_orchestrator_ref(self, orchestrator: OrchestratorProtocol) -> None:
        self._orchestrator_ref = orchestrator
        for plugin in self.plugins.values():
            plugin.set_orchestrator_ref(orchestrator)

    @property
    def orchestrator_ref(self) -> OrchestratorProtocol | None:
        """Get the orchestrator reference."""
        return self._orchestrator_ref

    def set_event_bus_ref(self, event_bus: EventBusProtocol) -> None:
        self._event_bus_ref = event_bus
        for plugin in self.plugins.values():
            plugin.set_event_bus(event_bus)

    def get_validation_registry(self) -> PluginValidationRegistry:
        return self.plugin_validation_registry

    def _set_allowed_plugins(self, allowed: set[str] | None) -> None:
        """Set the allowed-plugins manifest cache.

        Private hook threaded into `PluginInstaller` so allowlist mutation on
        an atomic install-commit / remove lives with the roster owner rather
        than the lifecycle service reaching into `_allowed_plugins` directly.
        """
        self._allowed_plugins = allowed

    def _mark_serving(self) -> None:
        """Seal this roster after the orchestrator starts action dispatch."""
        self._is_serving = True

    # ------------------------------------------------------------------
    # Discovery — delegates to PluginDiscovery, with the coordinating
    # instantiation loop owned by the manager (per the §9.C design's
    # "discovery returns classes; manager merges into plugins" pattern).
    # ------------------------------------------------------------------

    def discover_plugins(
        self,
        config_manager: ConfigManagerProtocol | None = None,
        *,
        allowed_plugins: set[str] | None = None,
    ) -> None:
        """Pre-serving plugin discovery method.

        ``allowed_plugins`` (the solet's profile manifest) restricts
        loading to entry points whose name appears in the set. ``None``
        means "no gating" (legacy / dev-box behavior). The first call's
        ``allowed_plugins`` is remembered for subsequent boot-phase
        re-discoveries, so the manifest stays the source of truth. Once action
        dispatch starts, this method refuses rather than clearing a live roster.
        """
        if self._is_serving:
            raise RuntimeError(
                "Plugin discovery refused: this PluginManager is serving actions; "
                "use PluginInstaller for a runtime roster mutation"
            )

        remembered_allowed = (
            allowed_plugins if allowed_plugins is not None else self._allowed_plugins
        )
        self._teardown_replaced_plugins()
        self._config_manager = config_manager
        self._allowed_plugins = remembered_allowed

        plugin_classes = self._discovery.discover(
            self._allowed_plugins, self._config_manager,
        )
        for plugin_name, plugin_class in plugin_classes.items():
            try:
                plugin_instance = self._initializer.create_plugin_instance(
                    plugin_class, plugin_name,
                )
                self._discovery.validate_plugin_contract(plugin_instance)
                self.plugins[plugin_name] = plugin_instance
            except Exception as e:
                logger.error(
                    f"Exception loading plugin {plugin_name}: {e}", exc_info=True,
                )

    def _teardown_replaced_plugins(self) -> None:
        """Remove pre-serving instances in the installer teardown order."""
        for plugin_name in list(self.plugins):
            try:
                self.installer.remove(
                    plugin_name,
                    stop=self._stop_replaced_plugin,
                    deregister=self._deregister_replaced_plugin_processes,
                )
            except PluginInstallError as exc:
                logger.error(
                    "Pre-serving replacement teardown was partial for plugin %r: %s",
                    plugin_name,
                    exc,
                )

    @staticmethod
    def _stop_replaced_plugin(plugin: PluginBase) -> None:
        """Stop a replaced plugin before its process keys leave the registry."""
        if not is_lifecycle_managed(plugin):
            return
        result = plugin.stop_services()
        if not inspect.isawaitable(result):
            return

        import asyncio

        try:
            asyncio.get_running_loop()
        except RuntimeError:
            asyncio.run(result)
            return
        raise RuntimeError(
            "Cannot synchronously stop a replaced plugin while an event loop is running"
        )

    def _deregister_replaced_plugin_processes(self, process_keys: list[str]) -> None:
        """Remove replaced process keys before deleting its roster entry."""
        if self._orchestrator_ref is None:
            return
        registry_manager = getattr(self._orchestrator_ref, "_process_registry_manager", None)
        if registry_manager is None:
            return
        deregister = getattr(registry_manager, "unregister_dynamic_processes", None)
        if not callable(deregister):
            raise RuntimeError(
                "Plugin replacement requires a process registry manager with "
                "unregister_dynamic_processes"
            )
        deregister(process_keys)

    # ------------------------------------------------------------------
    # Initialization + readiness — one-line delegates to PluginInitializer.
    # ------------------------------------------------------------------

    def inject_services(
        self,
        _state_service: object | None = None,
        _file_service: object | None = None,
    ) -> dict[str, bool]:
        return self._initializer.inject_services(_state_service, _file_service)

    def prepare_all_plugins_for_readiness(self) -> dict[str, object]:
        """Prepare all plugins for readiness verification."""
        return self._initializer.prepare_all_plugins_for_readiness()

    def initialize_all_plugins(
        self, config_manager: ConfigManagerProtocol | None = None,
    ) -> dict[str, bool]:
        """Initialize all loaded plugins after services are ready."""
        return self._initializer.initialize_all_plugins(config_manager)

    def get_plugin_readiness_status(self) -> dict[str, PluginReadiness]:
        """Get readiness status for all plugins."""
        return self._initializer.get_plugin_readiness_status()

    def get_unready_plugins(self) -> list[str]:
        return self._initializer.get_unready_plugins()

    def are_all_plugins_ready(self) -> bool:
        """Check if all plugins are ready for action processing."""
        return self._initializer.are_all_plugins_ready()

    def get_all_plugin_names(self) -> list[str]:
        """Get list of all loaded plugin names."""
        return self._initializer.get_all_plugin_names()

    def get_all_plugin_schemas(self) -> list[SchemaDefinition]:
        """Collect schema definitions from all SchemaProvider plugins."""
        return self._initializer.get_all_plugin_schemas()

    # ------------------------------------------------------------------
    # Dispatch — delegates to PluginDispatcher with caller-frame threading
    # so the dispatcher's service-provider access enforcement sees the
    # ORIGINAL external caller and not this delegate (which would always
    # be plugin_manager.py and miss the service-wrapper allowlist).
    # ------------------------------------------------------------------

    def get_plugin(self, plugin_name: str) -> PluginBase:
        """Get a specific plugin by name.

        Raises:
            PluginError: If plugin not found
            FrameworkError: If attempting direct access to service provider plugin
        """
        current = inspect.currentframe()
        caller_frame = current.f_back if current else None
        return self._dispatcher.get_plugin(plugin_name, caller_frame=caller_frame)

    def execute_action(
        self,
        plugin_name: str,
        params: dict[str, object],
        state: dict[str, object],
        APP_HOME: str,  # noqa: N803
        plugin_config: dict[str, object],
    ) -> dict[str, object]:
        """Execute an action on a specific plugin."""
        return self._dispatcher.execute_action(
            plugin_name, params, state, APP_HOME, plugin_config,
        )
