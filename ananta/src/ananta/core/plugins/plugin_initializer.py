"""Plugin instance lifecycle: construct, inject services, query readiness.

Extracted from `PluginManager` during the Step 9.C decomposition
(design record, Step 9.C, dev-checkout workbench — not part of the shipped tree).

Responsibility: own the per-plugin lifecycle once the plugin classes have
been discovered. Instantiates each class, wires the validation registry,
runs `prepare_for_readiness`, dispatches the config-driven `initialize`
call, and exposes readiness query methods.

Takes the shared `plugins: dict[str, PluginBase]` registry by reference;
construction populates the dict in place (via `create_plugin_instance`)
and the readiness queries iterate it directly.
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ananta.core.config.plugin_yaml_loader import load_plugin_yaml_defaults
from ananta.core.domain.enums import ErrorSeverity
from ananta.core.domain.error_codes import ErrorCode
from ananta.core.plugins.plugin_base import PluginBase, PluginReadiness
from ananta.core.plugins.plugin_validation import PluginValidationRegistry
from ananta.error_handling import FrameworkError, PluginError

if TYPE_CHECKING:
    from ananta.types.schema_types import SchemaDefinition

    from .plugin_manager import ConfigManagerProtocol

logger = logging.getLogger(__name__)


class PluginInitializer:
    """Instantiate + lifecycle for the plugins registry.

    Construction is cheap; the initializer holds references to the shared
    `plugins` dict and the validation registry. All mutations happen on
    the shared dict, so PluginManager observes them immediately.
    """

    def __init__(
        self,
        plugins: dict[str, PluginBase],
        validation_registry: PluginValidationRegistry,
    ) -> None:
        self._plugins = plugins
        self._validation_registry = validation_registry

    # ------------------------------------------------------------------
    # Instance construction
    # ------------------------------------------------------------------

    def create_plugin_instance(
        self, plugin_class: type[PluginBase], plugin_name: str,
    ) -> PluginBase:
        """Initialize a plugin instance - bootstrap compatible."""
        try:
            plugin_instance = plugin_class()
            plugin_instance.name = plugin_name

            # Set validation registry if available
            if hasattr(plugin_instance, "set_validation_registry"):
                plugin_instance.set_validation_registry(self._validation_registry)

            return plugin_instance

        except Exception as e:
            logger.error(f"Failed to initialize plugin {plugin_name}: {e}")
            raise PluginError(
                message=f"Plugin initialization failed: {plugin_name}",
                error_code=ErrorCode.PLUGIN_INSTANTIATION_ERROR,
                details={"plugin_name": plugin_name, "error": str(e)},
                original_error=e,
                severity=ErrorSeverity.ERROR,
            ) from e

    # ------------------------------------------------------------------
    # Service injection + readiness preparation
    # ------------------------------------------------------------------

    def inject_services(
        self,
        _state_service: object | None = None,
        _file_service: object | None = None,  # Reserved for interface compatibility
    ) -> dict[str, bool]:
        readiness_results: dict[str, bool] = {}

        for _plugin_index, (plugin_name, plugin) in enumerate(self._plugins.items(), 1):
            try:
                plugin.prepare_for_readiness()

                logger.debug(f"🔍 READINESS_PLUGIN_CHECK: {plugin_name} checking is_ready() status")
                if plugin.is_ready():
                    readiness_results[plugin_name] = True
                    logger.debug(f"✅ READINESS_PLUGIN_SUCCESS: {plugin_name} is ready")
                else:
                    readiness_results[plugin_name] = False
                    error_msg = plugin.readiness_error or "Unknown readiness failure"
                    logger.error(
                        f"❌ READINESS_PLUGIN_FAILED: {plugin_name} failed readiness - {error_msg}"
                    )

            except Exception as e:
                readiness_results[plugin_name] = False
                logger.error(
                    f"💥 READINESS_PLUGIN_EXCEPTION: {plugin_name} threw exception during readiness preparation: {e}"
                )
                import traceback

                logger.error(
                    f"💥 READINESS_PLUGIN_TRACEBACK: {plugin_name} traceback: {traceback.format_exc()}"
                )

        ready_count = sum(readiness_results.values())
        total_count = len(readiness_results)
        logger.debug(
            f"🏁 READINESS_COMPLETE: Plugin readiness preparation completed: {ready_count}/{total_count} plugins ready"
        )

        return readiness_results

    def prepare_all_plugins_for_readiness(self) -> dict[str, object]:
        """Prepare all plugins for readiness verification."""
        from ananta.core.plugins.capabilities import is_lifecycle_managed

        readiness_results: dict[str, object] = {}

        for plugin_name, plugin in self._plugins.items():
            try:
                # Skip lifecycle-managed plugins - they will be prepared/started by startup sequence
                if is_lifecycle_managed(plugin):
                    readiness_results[plugin_name] = "lifecycle_managed_skipped"
                    continue

                # Non-lifecycle plugins: mark as ready immediately
                readiness_results[plugin_name] = "no_preparation_needed"

                # Mark plugin as ready after successful preparation
                plugin.set_ready()

            except Exception as e:
                logger.error(f"Failed to prepare plugin {plugin_name}: {e}")
                plugin.set_error(str(e))
                readiness_results[plugin_name] = f"preparation_failed: {e}"

        return readiness_results

    def initialize_all_plugins(
        self, config_manager: ConfigManagerProtocol | None = None,
    ) -> dict[str, bool]:
        """
        Initialize all loaded plugins after services are ready.

        Args:
            config_manager: Configuration manager that implements ConfigManagerProtocol.

        Returns:
            Mapping of plugin name to initialization success (True/False).
        """

        if config_manager is None:
            return {}

        # Fail-fast: Verify config_manager implements the required protocol
        if not hasattr(config_manager, "get_plugin_config"):
            error_msg = (
                f"config_manager must implement ConfigManagerProtocol with get_plugin_config method. "
                f"Got type: {type(config_manager).__name__}"
            )
            raise FrameworkError(
                message=error_msg,
                error_code=ErrorCode.CONFIGURATION_ERROR,
                details={"config_manager_type": type(config_manager).__name__},
                severity=ErrorSeverity.CRITICAL,
            )

        results: dict[str, bool] = {}

        for plugin_name, plugin_instance in self._plugins.items():
            # Per the 2026-05-30 plugin-config-defaults unification, yaml's
            # ``config:`` block is the authoritative source of declared
            # defaults; load them per plugin and pass as the lowest merge
            # layer of get_plugin_config.
            yaml_defaults = _load_yaml_defaults_for_instance(plugin_instance)
            plugin_config = config_manager.get_plugin_config(
                plugin_name, default_config=yaml_defaults,
            )
            if plugin_config.get("enabled") is False:
                logger.info(
                    f"Skipping initialization of plugin '{plugin_name}': enabled=false in config"
                )
                results[plugin_name] = False
                continue

            if not hasattr(plugin_instance, "initialize"):
                results[plugin_name] = True
                continue

            try:
                # Per Q1.1 (2026-05-30): always call initialize() with the
                # merged dict, never skip on empty. The prior ``if
                # plugin_config:`` short-circuit was the root of the
                # ``config_provider`` stays None bug — yaml defaults +
                # eager merge mean plugin_config is rarely empty now,
                # but the unconditional call is the correctness fix.
                plugin_instance.initialize(plugin_config)
                results[plugin_name] = True
            except Exception:
                results[plugin_name] = False

        return results

    # ------------------------------------------------------------------
    # Readiness queries
    # ------------------------------------------------------------------

    def get_plugin_readiness_status(self) -> dict[str, PluginReadiness]:
        """Get readiness status for all plugins."""
        return {
            plugin_name: PluginReadiness.READY if plugin.is_ready() else PluginReadiness.ERROR
            for plugin_name, plugin in self._plugins.items()
        }

    def get_unready_plugins(self) -> list[str]:
        return [
            plugin_name for plugin_name, plugin in self._plugins.items() if not plugin.is_ready()
        ]

    def are_all_plugins_ready(self) -> bool:
        """Check if all plugins are ready for action processing."""
        return all(plugin.is_ready() for plugin in self._plugins.values())

    def get_all_plugin_names(self) -> list[str]:
        """Get list of all loaded plugin names."""
        return list(self._plugins.keys())

    def get_all_plugin_schemas(self) -> list[SchemaDefinition]:
        """Collect schema definitions from all SchemaProvider plugins.

        Delegates to centralized collect_schemas() function.
        Fails fast with PluginCapabilityError if any plugin fails.
        """
        from ananta.core.plugins.capabilities import collect_schemas

        return collect_schemas(self._plugins)


# ----------------------------------------------------------------------
# plugin.yaml defaults helpers (2026-05-30 plugin-config-defaults work)
# ----------------------------------------------------------------------


def _load_yaml_defaults_for_instance(plugin_instance: PluginBase) -> dict[str, Any]:
    """Locate the plugin's ``plugin.yaml`` and return its ``config:`` defaults.

    Returns empty when no ``plugin.yaml`` is found — the merge layer in
    ``ConfigManager.get_plugin_config`` becomes a no-op rather than a
    boot-blocker.
    """
    plugin_root = _find_plugin_root(plugin_instance)
    return load_plugin_yaml_defaults(plugin_root)


def _find_plugin_root(plugin_instance: PluginBase) -> Path | None:
    """Walk up from a plugin instance's class source file looking for plugin.yaml.

    Bounded walk (5 levels max). Robust across src-layout and flat
    layouts. Returns ``None`` if no ``plugin.yaml`` is found within
    reach — the loader treats that as 'no yaml defaults', which is
    correct for plugins that don't ship a yaml.
    """
    module_name = plugin_instance.__class__.__module__
    module = sys.modules.get(module_name)
    if module is None:
        return None
    module_file = getattr(module, "__file__", None)
    if module_file is None:
        return None
    candidate = Path(module_file).resolve().parent
    for _ in range(5):
        if (candidate / "plugin.yaml").is_file():
            return candidate
        parent = candidate.parent
        if parent == candidate:
            return None
        candidate = parent
    return None
