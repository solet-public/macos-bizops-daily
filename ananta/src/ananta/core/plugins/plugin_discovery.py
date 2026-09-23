"""Entry-point discovery + class validation for plugins.

Extracted from `PluginManager` during the Step 9.C decomposition
(design record, Step 9.C, dev-checkout workbench — not part of the shipped tree).

Responsibility: walk the `ananta.plugins` entry-point group, filter by
the solet's profile manifest (`allowed_plugins`), order by priority
(per-plugin config first, then `SERVICE_PLUGIN_PRIORITY` for foundational
plugins, then default), and return a `dict[str, type[PluginBase]]` of
plugin name → class. Instantiation, validation-registry wiring, and
post-instance contract checks happen in `PluginInitializer` / on the
manager's coordinating loop — this module's surface stops at "find me
the classes."
"""

from __future__ import annotations

import inspect
import logging
from importlib import metadata
from typing import TYPE_CHECKING

from ananta.constants import (
    DEFAULT_ADDRESS_BOOK_PLUGIN,
    DEFAULT_BLOB_STORAGE_PLUGIN,
    DEFAULT_STATE_MANAGEMENT_PLUGIN,
    SERVICE_PLUGIN_PRIORITY,
)
from ananta.core.plugins.plugin_base import PluginBase

if TYPE_CHECKING:
    from .plugin_manager import ConfigManagerProtocol

logger = logging.getLogger(__name__)


class PluginDiscovery:
    """Stateless plugin-discovery collaborator.

    No constructor args — every call passes the inputs it needs. The
    discovery returns plugin CLASSES; instantiation is `PluginInitializer`'s
    job, and the coordinating loop on `PluginManager` ties them together.
    """

    def discover(
        self,
        allowed_plugins: set[str] | None,
        config_manager: ConfigManagerProtocol | None,
    ) -> dict[str, type[PluginBase]]:
        """Find + prioritize installed plugin entry points.

        ``allowed_plugins`` restricts loading to entry points whose name
        appears in the set (the solet's profile manifest). ``None``
        means "no gating" (legacy / dev-box behavior).

        Returns ``{plugin_name: plugin_class}`` in priority order. Plugin
        instantiation is delegated to `PluginInitializer.create_plugin_instance`
        on the coordinating loop in `PluginManager.discover_plugins`.
        """
        result: dict[str, type[PluginBase]] = {}

        try:
            entry_points = list(metadata.entry_points().select(group="ananta.plugins"))

            if not entry_points:
                return result

            if allowed_plugins is not None:
                before = len(entry_points)
                entry_points = [ep for ep in entry_points if ep.name in allowed_plugins]
                skipped = before - len(entry_points)
                if skipped:
                    logger.debug(
                        f"Profile manifest excluded {skipped} of {before} installed entry points"
                    )
                missing = allowed_plugins - {ep.name for ep in entry_points}
                if missing:
                    logger.warning(
                        f"Profile manifest declares plugins not installed as entry points: "
                        f"{sorted(missing)}"
                    )

            # Sort entry points by priority - per-plugin config first, then defaults
            prioritized_entry_points = sorted(
                entry_points,
                key=lambda ep: self._get_plugin_priority(ep.name, config_manager),
            )

            for entry_point in prioritized_entry_points:
                self._load_plugin_class_from_entry_point(entry_point, result)

        except Exception as e:
            logger.error(f"Error during plugin discovery: {e}", exc_info=True)
            raise

        return result

    def validate_plugin_contract(self, plugin: PluginBase) -> None:
        """Validate that plugin implements required interface contract.

        Uses the ServiceProvider protocol to check if plugin declares a service_interface.
        If so, validates that the plugin actually inherits from its declared interface.

        Raises:
            PluginContractError: If contract validation fails.
            ServiceInterfaceMismatchError: If plugin doesn't inherit from declared interface.
        """
        from ananta.core.plugins.capabilities import (
            is_service_provider,
            validate_service_provider,
        )

        # If plugin declares service_interfaces, validate inheritance
        if is_service_provider(plugin):
            # validate_service_provider checks inheritance + version key consistency
            validate_service_provider(plugin)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _get_plugin_priority(
        self, plugin_name: str, config_manager: ConfigManagerProtocol | None,
    ) -> int:
        """Get plugin loading priority.

        Resolution order:
        1. ``priority`` field in the per-plugin config JSON (any non-negative int).
        2. ``SERVICE_PLUGIN_PRIORITY`` for foundational plugins (state, blob, address book).
        3. ``100`` for everything else.
        """
        configured = self._get_configured_priority(plugin_name, config_manager)
        if configured is not None:
            return configured

        foundational_plugins = [
            DEFAULT_STATE_MANAGEMENT_PLUGIN,
            DEFAULT_BLOB_STORAGE_PLUGIN,
            DEFAULT_ADDRESS_BOOK_PLUGIN,
        ]
        if any(service_plugin in plugin_name.lower() for service_plugin in foundational_plugins):
            return SERVICE_PLUGIN_PRIORITY
        return 100  # Default priority for non-service plugins

    def _get_configured_priority(
        self, plugin_name: str, config_manager: ConfigManagerProtocol | None,
    ) -> int | None:
        """Read ``priority`` from a plugin's config, returning ``None`` if unset."""
        if config_manager is None:
            return None
        plugin_config = config_manager.get_plugin_config(plugin_name)
        configured = plugin_config.get("priority")
        if isinstance(configured, int) and not isinstance(configured, bool):
            return configured
        return None

    def _load_plugin_class_from_entry_point(
        self,
        entry_point: metadata.EntryPoint,
        result: dict[str, type[PluginBase]],
    ) -> None:
        """Load a plugin class from an entry point and add to the result dict.

        Skips silently if the class fails validation; logs and continues
        on exception so a single broken plugin doesn't block discovery.
        """
        plugin_name = entry_point.name

        if plugin_name in result:
            return

        try:
            plugin_class = entry_point.load()

            if not self._validate_plugin_class(plugin_class, plugin_name):
                return

            result[plugin_name] = plugin_class

        except Exception as e:
            logger.error(f"Exception loading plugin {plugin_name}: {e}", exc_info=True)

    def _validate_plugin_class(self, plugin_class: object, plugin_name: str) -> bool:
        """Validate that the plugin class meets requirements."""
        if not inspect.isclass(plugin_class):
            logger.error(f"Plugin {plugin_name} entry point is not a class")
            return False

        if not issubclass(plugin_class, PluginBase):
            logger.error(f"Plugin {plugin_name} does not inherit from PluginBase")
            return False

        return True
