"""Plugin capability protocols.

All plugin capabilities are defined here as Protocols.
Each protocol is bound to concrete types from the actual ABCs/interfaces.

IMPORTANT: These protocols don't just check method names - they enforce
return types that bind to real domain types (SchemaDefinition, etc.)

Usage:
    from ananta.core.plugins.protocols import SchemaProvider, LifecycleManaged

    if isinstance(plugin, SchemaProvider):
        schemas = plugin.get_schema_definitions()  # Type-safe!
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol, runtime_checkable

if TYPE_CHECKING:
    from ananta.types.schema_types import SchemaDefinition


# =============================================================================
# Service Interface Type (Union of all service ABCs)
# =============================================================================

ServiceInterfaceType = (
    "StateManagementInterface"
    "| InferenceServiceInterface"
    "| VectorServiceInterface"
    "| BlobStorageServiceInterface"
    "| EmbeddingServiceInterface"
    "| VaultServiceInterface"
    "| AddressBookServiceInterface"
    "| MemoryServiceInterface"
)


# =============================================================================
# Core Capability Protocols
# =============================================================================


@runtime_checkable
class SchemaProvider(Protocol):
    """Plugin that provides database schemas.

    Bound to: SchemaDefinition (ananta.types.schema_types)

    Implementations MUST return actual SchemaDefinition instances,
    not arbitrary dicts or lists.
    """

    def get_schema_definitions(self) -> list[SchemaDefinition]:
        """Return schema definitions for this plugin's tables.

        Returns:
            List of SchemaDefinition instances with proper namespace and tables.

        Raises:
            Any exception will be wrapped in PluginCapabilityError with context.
        """
        ...


@runtime_checkable
class LifecycleManaged(Protocol):
    """Plugin requiring lifecycle management (start/stop).

    Implementations MUST define all lifecycle methods.
    Partial implementation is a contract violation.
    """

    def prepare_for_readiness(self) -> None:
        """Prepare plugin for operation.

        Called after dependency injection, before start_services().
        Validate that all required dependencies are present.

        Raises:
            RuntimeError: If required dependencies not injected.
        """
        ...

    def start_services(self) -> None:
        """Start plugin services.

        Called during orchestrator startup.
        Must be idempotent - calling twice should not cause errors.
        """
        ...

    def stop_services(self) -> None:
        """Stop plugin services and cleanup.

        Called during orchestrator shutdown.
        Must be idempotent - calling twice should not cause errors.
        """
        ...

    def is_running(self) -> bool:
        """Return True if services are currently running."""
        ...

    def set_active(self, active: bool) -> None:
        """Toggle whether the plugin's background work should run.

        Called by the deployment plugin during blue-green color swaps:
        the inactive color receives `set_active(False)` so its
        background loops (schedulers, drainers, message handlers,
        gateway connections) quiesce while the active color continues
        to serve. The active color receives `set_active(True)` on
        startup and stays active until the next swap.

        The default `PluginBase` implementation is a no-op — plugins
        without background work need no override. Plugins with
        background work override to gate at their tick boundary.

        Must be idempotent: calling `set_active(False)` twice in a row
        is a no-op on the second call. Must not perform IO or raise
        — the deployment plugin invokes this synchronously while it
        holds the swap interlock.
        """
        ...

    def get_readiness_error(self) -> str | None:
        """Return error message if not ready, None otherwise."""
        ...

    def set_error(self, error_message: str) -> None:
        """Mark plugin as having an error.

        Args:
            error_message: Description of the error that occurred.
        """
        ...


# NOTE: StateConsumer, EmbeddingConsumer, MemoryConsumer protocols REMOVED (2025-12-06)
# These were code smells - implementation details masquerading as capabilities.
# See: ananta_build/2025-12-06_service_binding_architecture.md
#
# Plugins now request services via orchestrator.get_service() in prepare_for_readiness()
# instead of having services pushed by the platform based on protocol scanning.


@runtime_checkable
class ServiceProvider(Protocol):
    """Plugin that provides one or more service interfaces.

    IMPORTANT: This protocol MUST be combined with actual ABC inheritance.
    The service_interfaces property declares which ABCs this plugin implements.

    Validation at load time ensures:
    1. service_interfaces returns valid service ABCs
    2. Plugin actually inherits from each declared ABC
    3. supported_interface_versions keys match service_interfaces

    Plugins implementing ServiceProvider are registered under the
    service_interface:: namespace instead of plugin:: namespace.
    """

    name: str

    @property
    def service_interfaces(self) -> tuple[type, ...]:
        """Return the service interface classes this plugin provides.

        Returns:
            Tuple of interface ABCs. May include any combination of:
            StateManagementInterface, InferenceServiceInterface,
            VectorServiceInterface, BlobStorageServiceInterface,
            EmbeddingServiceInterface, VaultServiceInterface,
            AddressBookServiceInterface, MemoryServiceInterface,
            KnowledgeServiceInterface

        The plugin MUST inherit from each returned interface class.
        """
        ...

    @property
    def supported_interface_versions(self) -> dict[type, str]:
        """Return per-interface version mapping.

        Returns:
            Dict mapping each interface ABC type to the version string
            the plugin was built against. Keys must match service_interfaces.
        """
        ...


@runtime_checkable
class IOInterface(Protocol):
    """Plugin that provides interactive I/O.

    Must implement full I/O lifecycle.
    Used by console, REST, Telegram, and other user-facing plugins.
    """

    def start_interface(self) -> None:
        """Start listening for input."""
        ...

    def post_message(
        self,
        message: str,
        metadata: dict[str, object] | None = None,
    ) -> None:
        """Send message to connected clients.

        Args:
            message: The message content to send.
            metadata: Optional metadata (session_id, user_id, etc.)
        """
        ...

    def stop_interface(self) -> None:
        """Stop interface and cleanup connections."""
        ...


@runtime_checkable
class AsyncCapable(Protocol):
    """Plugin that supports async operations.

    For plugins with async start_services, stop_services, etc.
    """

    async def start_services_async(self) -> None:
        """Async version of start_services."""
        ...

    async def stop_services_async(self) -> None:
        """Async version of stop_services."""
        ...
