# File: ananta/src/ananta/core/service_transition_coordinator.py

import logging
from typing import Protocol

from ananta.core.plugins.plugin_base import PluginBase
from ananta.core.plugins.plugin_manager import (
    ConfigManagerProtocol,
)
from ananta.core.plugins.profile_manifest import load_manifest_plugin_set


class HasDiscoverPlugins(Protocol):
    """Protocol for plugin manager with discover_plugins method."""

    plugins: dict[str, PluginBase]

    def discover_plugins(
        self,
        config_manager: ConfigManagerProtocol | None = None,
        *,
        allowed_plugins: set[str] | None = None,
    ) -> None: ...

    def initialize_all_plugins(
        self, config_manager: ConfigManagerProtocol | None = None
    ) -> dict[str, bool]: ...


class HasProcessRegistryManager(Protocol):
    """Protocol for process registry manager."""

    discovery_service: object | None

    def get_registry_data(self) -> dict[str, object] | None: ...

    def set_discovery_service(self, discovery_service: object) -> None: ...

    def _do_process_registry_persistence(self) -> None: ...


class HasServicesCollection(Protocol):
    """Protocol for orchestrator with services collection."""

    services_collection: dict[str, object]
    _process_registry_manager: HasProcessRegistryManager | None


class ServiceTransitionCoordinator:
    """Phase 2: Manages service transitions from bootstrap to plugin mode"""

    def __init__(
        self,
        orchestrator: HasServicesCollection,
        plugin_manager: HasDiscoverPlugins,
        config_manager: ConfigManagerProtocol | None = None,
    ) -> None:
        self.orchestrator = orchestrator
        self.plugin_manager = plugin_manager
        self.config_manager = config_manager

    def execute_full_transition_sync(self) -> dict[str, object]:
        """Complete transition from bootstrap to plugin-backed services - SYNCHRONOUS version"""
        import logging

        logger = logging.getLogger(__name__)

        # The manifest-gating rationale is retained on this sole transition path.
        self.plugin_manager.discover_plugins(
            allowed_plugins=self._load_allowed_plugins(),
        )

        # Transition each service atomically - services are now available in orchestrator
        services = self.orchestrator.services_collection
        for service in services.values():
            transition_func = getattr(service, "transition_to_plugin", None)
            if callable(transition_func):
                # Check if service is already in plugin mode (EventOrchestrator creates services in plugin mode)
                bootstrap_mode_attr = getattr(service, "bootstrap_mode", None)
                if bootstrap_mode_attr is not None and not bootstrap_mode_attr:
                    pass
                else:
                    transition_func(self.plugin_manager)
            else:
                pass

        # Schema initialization is handled by startup_sequence step 8 via the
        # plugin-schema lifecycle. See this method's discovery path for context.
        self._initialize_process_registry()

        self._bind_services_for_plugin_initialization(logger)
        init_results = self.plugin_manager.initialize_all_plugins(self.config_manager)
        self._log_plugin_initialization_summary(init_results, logger)

        return dict(services)

    def _load_allowed_plugins(self) -> set[str] | None:
        """Resolve the profile-manifest plugin allowlist from APP_HOME.

        Returns the allowed set when ``<APP_HOME>/config/manifest.yaml`` is
        present, ``None`` otherwise (legacy / dev-box "no gating" behavior).
        Mirrors what ``startup_sequence._init_plugin_manager`` does on the
        orchestrator's own plugin_manager; we apply it here too because the
        bootstrap plugin_manager is a separate instance whose allowlist
        cache is empty at this point.
        """
        app_home = getattr(self.orchestrator, "APP_HOME", None)
        if app_home is None:
            return None
        return load_manifest_plugin_set(app_home)

    def _log_plugin_initialization_summary(
        self, init_results: dict[str, bool], logger: object
    ) -> None:
        """Log summary of plugin initialization outcomes."""
        try:
            import logging

            if not isinstance(logger, logging.Logger):
                logger = logging.getLogger(__name__)
        except Exception:
            import logging

            logger = logging.getLogger(__name__)

        if not init_results:
            return

        failed_plugins = [name for name, success in init_results.items() if not success]
        if failed_plugins:
            pass
        else:
            pass

    def _bind_services_for_plugin_initialization(self, logger: object) -> None:
        """Ensure plugins have required service dependencies before initialization."""
        logger = self._ensure_logger(logger)
        services = getattr(self.orchestrator, "services_collection", {})

        self._set_orchestrator_ref_if_available()
        self._bind_services_to_plugins(services)

    def _ensure_logger(self, logger: object) -> logging.Logger:
        """Ensure we have a valid logger instance."""
        if isinstance(logger, logging.Logger):
            return logger
        return logging.getLogger(__name__)

    def _set_orchestrator_ref_if_available(self) -> None:
        """Set orchestrator reference on plugin manager if method exists."""
        setter = getattr(self.plugin_manager, "set_orchestrator_ref", None)
        if not callable(setter):
            return
        try:
            setter(self.orchestrator)
        except Exception:
            pass

    def _bind_services_to_plugins(self, services: dict[str, object]) -> None:
        """Bind services to each plugin that supports them."""
        state_service = services.get("state_service")
        file_service = services.get("file_service")

        for plugin in self.plugin_manager.plugins.values():
            self._try_set_plugin_service(plugin, "set_state_service", state_service)
            self._try_set_plugin_service(plugin, "set_file_service", file_service)

    def _try_set_plugin_service(
        self, plugin: PluginBase, setter_name: str, service: object | None
    ) -> None:
        """Try to set a service on a plugin if both are available."""
        if service is None or not hasattr(plugin, setter_name):
            return
        try:
            setter = getattr(plugin, setter_name)
            setter(service)
        except Exception:
            pass

    def _initialize_process_registry(self) -> None:
        """Initialize process registry - synchronous database operations"""
        import logging

        logging.getLogger(__name__)

        services = self.orchestrator.services_collection

        # CRITICAL FIX: Reuse the existing ProcessRegistryManager from orchestrator instead of creating a new one
        # This prevents the double registry building that was destroying plugin registrations
        registry_manager = self.orchestrator._process_registry_manager
        if registry_manager is not None:
            # Only set discovery service if not already set
            if registry_manager.discovery_service is None:
                discovery_service_obj = services.get("discovery_service")
                if discovery_service_obj is not None:
                    registry_manager.set_discovery_service(discovery_service_obj)
                else:
                    pass
            else:
                pass

            # VALIDATION: Ensure orchestrator actually built the registry
            registry_data = registry_manager.get_registry_data()
            process_count = 0
            if registry_data is not None:
                processes_obj = registry_data.get("processes", {})
                # Type narrow: ensure processes is a dict-like object with len()
                if isinstance(processes_obj, dict):
                    process_count = len(processes_obj)

            # FAIL-FAST: If registry is empty, orchestrator feature flag was disabled
            if (
                process_count <= 10
            ):  # Only service_interface processes = orchestrator skipped building
                raise RuntimeError(
                    "ProcessRegistryManager exists but registry is empty. "
                    "Orchestrator feature flag ANANTA_USE_PROCESS_REGISTRY_MANAGER must be enabled for proper system function."
                )

            # CRITICAL FIX: DO NOT persist to database in Phase 2!
            # Database is not initialized until Phase 3, which happens AFTER this code runs.
            # Previous persistence attempt here caused 80x "Database not initialized" errors.
            # Phase 3 will handle persistence via action_coordinator.complete_database_initialization()
        else:
            raise RuntimeError(
                "ProcessRegistryManager not found in orchestrator - system initialization failed"
            )
