import logging
from collections.abc import Awaitable
from typing import Protocol, cast, runtime_checkable

from ananta.core.plugins.profile_manifest import load_manifest_plugin_set
from ananta.error_handling import FrameworkError
from ananta.types.schema_types import SchemaDefinition

from ..interfaces import IPluginLifecycleManager

logger = logging.getLogger(__name__)


@runtime_checkable
class PluginManagerProtocol(Protocol):
    """Protocol for PluginManager interface."""

    plugins: dict[str, object]

    def get_all_plugin_names(self) -> list[str]: ...
    def get_plugin(self, name: str) -> object: ...
    def prepare_all_plugins_for_readiness(self) -> dict[str, object]: ...
    def are_all_plugins_ready(self) -> bool: ...
    def get_unready_plugins(self) -> list[str]: ...
    def get_plugin_readiness_status(self) -> dict[str, object]: ...
    def discover_plugins(
        self,
        config_manager: object | None = None,
        *,
        allowed_plugins: set[str] | None = None,
    ) -> None: ...
    def set_orchestrator_ref(self, orchestrator_ref: object) -> None: ...
    def set_event_bus_ref(self, event_bus: object) -> None: ...


@runtime_checkable
class ConfigManagerProtocol(Protocol):
    """Protocol for ConfigManager interface."""

    def update_plugin_cli_args(self, plugin_cli_args: dict[str, dict[str, object]]) -> None: ...


@runtime_checkable
class ServiceInjectorProtocol(Protocol):
    """Protocol for ServiceInjector interface."""

    def inject_services(self, plugins: dict[str, object]) -> None: ...


@runtime_checkable
class SchemaManagerProtocol(Protocol):
    """Protocol for SchemaManager interface."""

    def initialize_schemas(self, schema_definitions: list[SchemaDefinition]) -> Awaitable[None]: ...


class PluginLifecycleManager(IPluginLifecycleManager):
    def __init__(self) -> None:
        self._plugins_ready = False

    def validate_inference_provider(self, provider_name: str, plugin_manager: object) -> None:
        # Type narrowing for plugin_manager
        if not hasattr(plugin_manager, "get_all_plugin_names"):
            raise TypeError("plugin_manager must have get_all_plugin_names method")
        typed_pm = cast(PluginManagerProtocol, plugin_manager)
        if not callable(typed_pm.get_all_plugin_names):
            raise TypeError("plugin_manager must have get_all_plugin_names method")
        if not hasattr(plugin_manager, "get_plugin") or not callable(typed_pm.get_plugin):
            raise TypeError("plugin_manager must have get_plugin method")

        available_plugins = typed_pm.get_all_plugin_names()

        if provider_name not in available_plugins:
            raise FrameworkError(
                message=f"Default inference provider '{provider_name}' not found",
                error_code="orchestrator.inference_provider_not_found",
                details={
                    "requested_provider": provider_name,
                    "available_plugins": available_plugins,
                    "hint": "Check plugin installation and configuration",
                },
            )

        typed_pm.get_plugin(provider_name)

    def verify_plugins_ready(self) -> None:
        """Verify all plugins are ready. Fails fast if not initialized."""
        if not self._plugins_ready:
            raise FrameworkError(
                message="Plugins not initialized. Call initialize_plugins() first.",
                error_code="plugin_lifecycle.not_initialized",
                severity="CRITICAL",
            )

    def _validate_plugin_manager_for_initialization(self, plugin_manager: object) -> None:
        """Validate plugin_manager has required methods for initialization.

        Args:
            plugin_manager: Object to validate

        Raises:
            TypeError: If required methods are missing
        """
        required_methods = [
            "prepare_all_plugins_for_readiness",
            "are_all_plugins_ready",
            "get_unready_plugins",
            "get_plugin_readiness_status",
        ]
        for method_name in required_methods:
            if not (
                hasattr(plugin_manager, method_name)
                and callable(getattr(plugin_manager, method_name))
            ):
                raise TypeError(f"plugin_manager must have {method_name} method")

    def _handle_unready_plugins(
        self,
        plugin_manager: PluginManagerProtocol,
        readiness_results: dict[str, object],
    ) -> None:
        """Handle case where not all plugins are ready.

        Args:
            plugin_manager: Plugin manager instance
            readiness_results: Results from prepare_all_plugins_for_readiness

        Raises:
            FrameworkError: Always raised with details about unready plugins
        """
        unready_plugins = plugin_manager.get_unready_plugins()
        raise FrameworkError(
            message=f"Not all plugins are ready for action processing: {unready_plugins}",
            error_code="plugin_lifecycle.plugins_not_ready",
            details={
                "unready_plugins": unready_plugins,
                "readiness_results": readiness_results,
                "plugin_status": plugin_manager.get_plugin_readiness_status(),
            },
            severity="CRITICAL",
        )

    async def initialize_plugins(self, plugin_manager: object) -> None:
        """Initialize all plugins. Call once at startup."""
        if self._plugins_ready:
            return

        self._validate_plugin_manager_for_initialization(plugin_manager)
        assert isinstance(plugin_manager, PluginManagerProtocol)

        try:
            readiness_results = plugin_manager.prepare_all_plugins_for_readiness()

            if plugin_manager.are_all_plugins_ready():
                self._plugins_ready = True
                return

            self._handle_unready_plugins(plugin_manager, readiness_results)

        except FrameworkError:
            raise
        except Exception as e:
            raise FrameworkError(
                message="Plugin readiness verification failed - cannot begin action processing",
                error_code="plugin_lifecycle.readiness_failed",
                details={"error": str(e)},
                original_error=e,
                severity="CRITICAL",
            ) from e

    async def initialize_plugin_schemas(
        self, plugin_manager: object, schema_manager: object
    ) -> None:
        """Initialize schemas from all SchemaProvider plugins.

        Uses SchemaProvider protocol for detection.
        Fails fast with PluginCapabilityError if any plugin fails.
        """
        from ananta.core.plugins.capabilities import collect_schemas

        # Type narrowing for plugin_manager
        if not hasattr(plugin_manager, "plugins"):
            raise TypeError("plugin_manager must have plugins attribute")
        typed_pm = cast(PluginManagerProtocol, plugin_manager)

        # Type narrowing for schema_manager
        if not hasattr(schema_manager, "initialize_schemas"):
            raise TypeError("schema_manager must have initialize_schemas method")
        typed_sm = cast(SchemaManagerProtocol, schema_manager)
        if not callable(typed_sm.initialize_schemas):
            raise TypeError("schema_manager must have initialize_schemas method")

        # Collect all schemas using SchemaProvider protocol
        all_schemas = collect_schemas(typed_pm.plugins)
        if all_schemas:
            await typed_sm.initialize_schemas(all_schemas)

    def discover_and_initialize_plugins(
        self, plugin_manager: object, orchestrator_ref: object, config_manager: object | None = None
    ) -> None:
        # Type narrowing for plugin_manager
        if not hasattr(plugin_manager, "discover_plugins"):
            raise TypeError("plugin_manager must have discover_plugins method")
        typed_pm = cast(PluginManagerProtocol, plugin_manager)
        if not callable(typed_pm.discover_plugins):
            raise TypeError("plugin_manager must have discover_plugins method")
        if not hasattr(plugin_manager, "set_orchestrator_ref") or not callable(
            typed_pm.set_orchestrator_ref
        ):
            raise TypeError("plugin_manager must have set_orchestrator_ref method")

        # Mirror ServiceTransitionCoordinator: the 2026-05-31 incident showed
        # that an uninitialized manager without this argument loads every entry
        # point, including plugins outside the profile manifest.
        app_home = getattr(orchestrator_ref, "APP_HOME", None)
        allowed_plugins = (
            load_manifest_plugin_set(app_home) if app_home is not None else None
        )
        typed_pm.discover_plugins(config_manager, allowed_plugins=allowed_plugins)

        typed_pm.set_orchestrator_ref(orchestrator_ref)

    def configure_plugin_operational_config(
        self, config: object, plugin_operational_config: dict[str, object]
    ) -> None:
        if plugin_operational_config:
            # Type narrowing for config
            if not hasattr(config, "update_plugin_cli_args"):
                raise TypeError("config must have update_plugin_cli_args method")
            typed_config_mgr = cast(ConfigManagerProtocol, config)
            if not callable(typed_config_mgr.update_plugin_cli_args):
                raise TypeError("config must have update_plugin_cli_args method")

            # Validate that plugin_operational_config values are dicts
            typed_config: dict[str, dict[str, object]] = {}
            for key, value in plugin_operational_config.items():
                if not isinstance(value, dict):
                    raise TypeError(
                        f"plugin_operational_config[{key}] must be a dict, got {type(value)}"
                    )
                # After isinstance check, mypy knows value is dict
                typed_config[key] = value

            typed_config_mgr.update_plugin_cli_args(typed_config)
        else:
            pass

    def inject_plugin_services(self, service_injector: object, plugin_manager: object) -> None:
        # Type narrowing for service_injector
        if not hasattr(service_injector, "inject_services"):
            raise TypeError("service_injector must have inject_services method")
        typed_si = cast(ServiceInjectorProtocol, service_injector)
        if not callable(typed_si.inject_services):
            raise TypeError("service_injector must have inject_services method")

        # Type narrowing for plugin_manager
        if not hasattr(plugin_manager, "plugins"):
            raise TypeError("plugin_manager must have plugins attribute")
        typed_pm = cast(PluginManagerProtocol, plugin_manager)

        typed_si.inject_services(typed_pm.plugins)

    def setup_plugin_event_bus(self, plugin_manager: object, event_bus: object) -> None:
        # Type narrowing for plugin_manager
        if not hasattr(plugin_manager, "set_event_bus_ref"):
            raise TypeError("plugin_manager must have set_event_bus_ref method")
        typed_pm = cast(PluginManagerProtocol, plugin_manager)
        if not callable(typed_pm.set_event_bus_ref):
            raise TypeError("plugin_manager must have set_event_bus_ref method")

        typed_pm.set_event_bus_ref(event_bus)
