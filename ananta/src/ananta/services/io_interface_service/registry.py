"""IO Interface Registry - Maps namespaces to IO interface plugins.

This registry enables the IO Interface Service to route messages to the
correct IO interface plugin based on session namespace.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Protocol

from ananta.interfaces.io_capabilities import IOCapability

if TYPE_CHECKING:
    from typing import Any


logger = logging.getLogger(__name__)


class IOInterfacePluginProtocol(Protocol):
    """Protocol defining the interface for IO plugins.

    Plugins get APP_HOME from self.orchestrator_ref.APP_HOME in prepare_for_readiness()
    and store it as self._app_home. This is NOT passed as a method parameter.
    """

    @property
    def name(self) -> str:
        """Plugin name (used as namespace)."""
        ...

    def post_message(
        self,
        params: dict[str, Any],
        state: dict[str, Any],
    ) -> dict[str, Any]:
        """Send a message to the client."""
        ...

    def get_supported_capabilities(self) -> set[IOCapability]:
        """Return supported delivery capabilities."""
        ...


class IOInterfaceRegistry:
    """Registry mapping namespaces to IO interface plugins.

    Provides lookup functionality for the IO Interface Service to route
    messages to the correct plugin based on session namespace.
    """

    def __init__(self) -> None:
        """Initialize empty registry."""
        self._by_namespace: dict[str, IOInterfacePluginProtocol] = {}

    def register(self, plugin: IOInterfacePluginProtocol) -> None:
        """Register an IO interface plugin.

        Args:
            plugin: Plugin implementing IOInterfacePluginProtocol
        """
        namespace = plugin.name
        self._by_namespace[namespace] = plugin
        logger.debug(f"Registered IO interface plugin: {namespace}")

    def resolve(self, namespace: str) -> IOInterfacePluginProtocol | None:
        """Resolve namespace to plugin.

        Args:
            namespace: Plugin name / namespace to look up

        Returns:
            Plugin instance if found, None otherwise
        """
        return self._by_namespace.get(namespace)

    def get_all(self) -> list[IOInterfacePluginProtocol]:
        """Get all registered plugins.

        Returns:
            List of all registered IO interface plugins
        """
        return list(self._by_namespace.values())

    def get_namespaces(self) -> list[str]:
        """Get all registered namespaces.

        Returns:
            List of all registered namespace strings
        """
        return list(self._by_namespace.keys())

    def is_registered(self, namespace: str) -> bool:
        """Check if namespace is registered.

        Args:
            namespace: Namespace to check

        Returns:
            True if namespace has a registered plugin
        """
        return namespace in self._by_namespace
