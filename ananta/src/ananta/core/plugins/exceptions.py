"""Plugin capability exceptions.

All capability-related errors go through these exceptions.
Provides full diagnostic context for operators.

Fail-fast with context: We crash immediately but provide enough
information to diagnose the issue without debugging.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class PluginCapabilityError(Exception):
    """Raised when a plugin capability operation fails.

    Carries full context for diagnosis:
    - plugin_name: Which plugin failed
    - capability: Which capability was being used (SchemaProvider, LifecycleManaged, etc.)
    - operation: Which operation failed (get_schema_definitions, start_services, etc.)
    - original_error: The underlying exception

    Example:
        Plugin 'macos_vault_plugin' failed during SchemaProvider.get_schema_definitions():
        ImportError: No module named 'schema'
    """

    plugin_name: str
    capability: str
    operation: str
    original_error: Exception

    def __str__(self) -> str:
        return (
            f"Plugin '{self.plugin_name}' failed during "
            f"{self.capability}.{self.operation}(): "
            f"{type(self.original_error).__name__}: {self.original_error}"
        )


@dataclass(frozen=True)
class PluginContractViolationError(Exception):
    """Raised when a plugin violates its declared contract.

    Example: Plugin passes isinstance(SchemaProvider) check but
    get_schema_definitions() is broken or returns wrong type.

    This is a programming error in the plugin, not a runtime issue.
    """

    plugin_name: str
    protocol: str
    missing_or_broken: str
    details: str

    def __str__(self) -> str:
        return (
            f"Plugin '{self.plugin_name}' violates {self.protocol} contract: "
            f"{self.missing_or_broken} - {self.details}"
        )


@dataclass(frozen=True)
class ServiceInterfaceMismatchError(Exception):
    """Raised when a ServiceProvider doesn't match its declared interface.

    Example: Plugin declares service_interface=VaultServiceInterface but
    doesn't actually inherit from VaultServiceInterface.

    This is caught at plugin load time, not runtime.
    """

    plugin_name: str
    declared_interface: str
    actual_bases: list[str]

    def __str__(self) -> str:
        return (
            f"Plugin '{self.plugin_name}' declares service_interface="
            f"{self.declared_interface} but inherits from: "
            f"{', '.join(self.actual_bases)}"
        )
