"""
Provider Manager Service

Responsibility: Handle provider/plugin management for action execution
Dependencies: PluginManager, StateService, DiscoveryService
Complexity: Medium - manages plugin/service provider resolution and instantiation

Extracted from ActionManager god class during refactoring phases
"""

import logging
from datetime import UTC, datetime
from typing import Protocol

from ananta.constants import FRAMEWORK_NAMESPACE, ProviderType
from ananta.core.plugins.plugin_base import PluginBase

logger = logging.getLogger(__name__)


class PluginManagerProtocol(Protocol):
    """Protocol for PluginManager interface."""

    def get_plugin(self, plugin_name: str) -> PluginBase: ...


class OrchestratorProtocol(Protocol):
    """Protocol for Orchestrator interface."""

    def get_service(self, service_name: str) -> object: ...


class ProviderManager:
    """
    Service for managing action providers (plugins and services).

    ARCHITECTURAL ROLE: Supporting service that extracts provider management logic
    from ActionManager while maintaining action execution integrity.

    This service handles:
    - Resolving action provider information
    - Getting plugin and service instances
    - Preparing plugin action objects
    - Managing provider type determination
    """

    def __init__(
        self,
        plugin_manager: PluginManagerProtocol,
        state_service: object | None = None,
        orchestrator: OrchestratorProtocol | None = None,
        discovery_service: object | None = None,
        action_definition_getter: object | None = None,
        async_job_manager: object | None = None,
    ) -> None:
        """Initialize ProviderManager with required dependencies.

        All services are resolved dynamically via orchestrator.get_service() to maintain
        service-agnostic architecture.
        """
        self.plugin_manager = plugin_manager
        self.state_service = state_service
        self.orchestrator = orchestrator
        self.discovery_service = discovery_service
        self.action_definition_getter = action_definition_getter
        self.async_job_manager = async_job_manager

    def resolve_action_provider_info(
        self, action_name: str, action_def: dict[str, object], plugin_override: str | None = None
    ) -> tuple[ProviderType, object, str]:
        """
        Resolve provider information for action execution using priority-based strategies.

        Args:
            action_name: Name of the action to execute
            action_def: Action definition dictionary
            plugin_override: Optional plugin name override

        Returns:
            Tuple of (provider_type, provider_instance, function_name)
        """
        # Try each resolution strategy in priority order
        provider_info = (
            self._try_plugin_override(plugin_override, action_name, action_def)
            or self._try_action_name_prefix(action_name, action_def)
            or self._try_action_definition(action_name, action_def)
            or self._try_direct_plugin_lookup(action_name, action_def)
        )

        if provider_info:
            return provider_info

        # Default: Return None provider (will be handled by caller)
        # Using a sentinel value since UNKNOWN is not defined in ProviderType enum
        # The caller should handle the None provider case
        return ProviderType.PLUGIN, None, "execute_action"

    def get_plugin_instance(self, provider: str | None, action_name: str) -> PluginBase | None:
        """
        Get plugin instance by name.

        Args:
            provider: Plugin name or identifier
            action_name: Action name for context

        Returns:
            Plugin instance or None if not found
        """
        if not provider:
            return None

        try:
            plugin = self.plugin_manager.get_plugin(provider)
            return plugin
        except Exception:
            return None

    def get_service_instance(
        self, provider: str | None, action_name: str, _function_name: str
    ) -> object | None:  # Reserved for interface compatibility
        """
        Get service instance by name.

        Args:
            provider: Service name or identifier
            action_name: Action name for context
            function_name: Function name to execute

        Returns:
            Service instance or None if not found
        """
        if not provider:
            return None

        # Get service dynamically from orchestrator (service-agnostic architecture)
        if self.orchestrator and hasattr(self.orchestrator, "get_service"):
            service = self.orchestrator.get_service(provider)
            if service:
                return service

        logger.error(
            f"Service '{provider}' not found for action '{action_name}' - orchestrator may not have registered it"
        )
        return None

    def prepare_plugin_action_object(
        self,
        action: dict[str, object],
        prepared_action_def: dict[str, object],
        merged_parameters: dict[str, object],
        state: dict[str, object],
        function_name: str,
    ) -> tuple[dict[str, object], datetime]:
        """
        Prepare action object for plugin execution.

        Args:
            action: Original action data
            prepared_action_def: Prepared action definition
            merged_parameters: Merged parameters for execution
            state: Current execution state
            function_name: Function name to execute

        Returns:
            Tuple of (action_object, timestamp)
        """
        timestamp = datetime.now(UTC)

        # Build the action object for plugin execution
        action_object = {
            "action": prepared_action_def.get("action_name", action.get("action_name")),
            "parameters": merged_parameters,
            "function_name": function_name,
            "correlation": {
                "action_id": state.get("action_id"),
                "flow_id": state.get("flow_id"),
                "session_id": state.get("session_id"),
                "parent_action_id": state.get("parent_action_id"),
            },
            "metadata": {
                "timestamp": timestamp.isoformat(),
                "framework_namespace": FRAMEWORK_NAMESPACE,
                "source": action.get("source", "unknown"),
                "execution_context": state.get("execution_context", {}),
            },
        }

        # Add any custom fields from the action definition
        custom_fields = prepared_action_def.get("custom_fields", {})
        if custom_fields:
            action_object["custom_fields"] = custom_fields

        return action_object, timestamp

    def _try_plugin_override(
        self, plugin_override: str | None, action_name: str, action_def: dict[str, object]
    ) -> tuple[ProviderType, object, str] | None:
        """
        Priority 1: Check for explicit plugin override.

        Returns:
            Provider info tuple if override found, None otherwise
        """
        if not plugin_override:
            return None

        plugin = self.get_plugin_instance(plugin_override, action_name)
        if plugin:
            function_name_obj = action_def.get("function_name", "execute_action")
            function_name = (
                function_name_obj if isinstance(function_name_obj, str) else "execute_action"
            )
            return ProviderType.PLUGIN, plugin, function_name

        return None

    def _try_action_name_prefix(
        self, action_name: str, action_def: dict[str, object]
    ) -> tuple[ProviderType, object, str] | None:
        """
        Priority 2: Check if action_name contains plugin prefix.

        Returns:
            Provider info tuple if plugin found via prefix, None otherwise
        """
        if "." not in action_name:
            return None

        plugin_name = action_name.split(".")[0]
        plugin = self.get_plugin_instance(plugin_name, action_name)
        if plugin:
            function_name_obj = action_def.get("function_name", "execute_action")
            function_name = (
                function_name_obj if isinstance(function_name_obj, str) else "execute_action"
            )
            return ProviderType.PLUGIN, plugin, function_name

        return None

    def _extract_function_name(self, action_def: dict[str, object]) -> str:
        """Extract function name from action definition with type narrowing."""
        function_name_obj = action_def.get("function_name", "execute_action")
        if isinstance(function_name_obj, str):
            return function_name_obj
        return "execute_action"

    def _extract_provider_name(self, action_def: dict[str, object]) -> str | None:
        """Extract provider name from action definition with type narrowing."""
        provider_name_obj = action_def.get("provider")
        if isinstance(provider_name_obj, str):
            return provider_name_obj
        return None

    def _is_plugin_provider_type(self, provider_type_obj: object) -> bool:
        """Check if provider type indicates a plugin."""
        return provider_type_obj == ProviderType.PLUGIN or provider_type_obj == "plugin"

    def _is_service_provider_type(self, provider_type_obj: object) -> bool:
        """Check if provider type indicates a service."""
        return provider_type_obj == ProviderType.SERVICE_INTERFACE or provider_type_obj == "service"

    def _try_action_definition(
        self, action_name: str, action_def: dict[str, object]
    ) -> tuple[ProviderType, object, str] | None:
        """
        Priority 3: Check action definition for provider info.

        Returns:
            Provider info tuple if definition specifies provider, None otherwise
        """
        if not action_def:
            return None

        provider_type_obj = action_def.get("provider_type")
        function_name = self._extract_function_name(action_def)
        provider_name = self._extract_provider_name(action_def)

        if self._is_plugin_provider_type(provider_type_obj):
            return self._resolve_plugin_provider(provider_name, action_name, function_name)

        if self._is_service_provider_type(provider_type_obj):
            return self._resolve_service_provider(provider_name, action_name, function_name)

        return None

    def _resolve_plugin_provider(
        self, provider_name: str | None, action_name: str, function_name: str
    ) -> tuple[ProviderType, object, str] | None:
        """Resolve plugin provider from action definition."""
        plugin = self.get_plugin_instance(provider_name, action_name)
        if not plugin:
            return None
        return ProviderType.PLUGIN, plugin, function_name

    def _resolve_service_provider(
        self, provider_name: str | None, action_name: str, function_name: str
    ) -> tuple[ProviderType, object, str] | None:
        """Resolve service provider from action definition."""
        service = self.get_service_instance(provider_name, action_name, function_name)
        if not service:
            return None
        return ProviderType.SERVICE_INTERFACE, service, function_name

    def _try_direct_plugin_lookup(
        self, action_name: str, action_def: dict[str, object] | None
    ) -> tuple[ProviderType, object, str] | None:
        """
        Priority 4: Try to find plugin by action name.

        Returns:
            Provider info tuple if plugin found by name, None otherwise
        """
        plugin = self.get_plugin_instance(action_name, action_name)
        if plugin:
            if action_def:
                function_name_obj = action_def.get("function_name", "execute_action")
                function_name = (
                    function_name_obj if isinstance(function_name_obj, str) else "execute_action"
                )
            else:
                function_name = "execute_action"
            return ProviderType.PLUGIN, plugin, function_name

        return None
