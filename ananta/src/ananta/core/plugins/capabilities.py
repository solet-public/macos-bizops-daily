"""Centralized capability detection.

ALL capability checks go through this module.
No hasattr or isinstance checks elsewhere in codebase.

Every operation wraps exceptions in PluginCapabilityError with full context.
This is fail-fast WITH context - we crash immediately but provide enough
information to diagnose without debugging.

Usage:
    from ananta.core.plugins.capabilities import (
        is_schema_provider,
        collect_schemas,
        start_lifecycle_plugins,
    )

    # Check capability
    if is_schema_provider(plugin):
        ...

    # Batch operations with error context
    schemas = collect_schemas(plugin_manager.plugins)
"""

from __future__ import annotations

import asyncio
import inspect
import logging
from collections.abc import Mapping
from typing import TYPE_CHECKING, TypeGuard

from ananta.core.plugins.exceptions import (
    PluginCapabilityError,
    PluginContractViolationError,
    ServiceInterfaceMismatchError,
)
from ananta.core.plugins.protocols import (
    AsyncCapable,
    IOInterface,
    LifecycleManaged,
    SchemaProvider,
    ServiceProvider,
)

if TYPE_CHECKING:
    from ananta.core.plugins.plugin_base import PluginBase
    from ananta.interfaces.llm_session_source_interface import LLMSessionSourceInterface
    from ananta.types.schema_types import SchemaDefinition

logger = logging.getLogger(__name__)


# =============================================================================
# TypeGuard Functions
# =============================================================================


def is_schema_provider(plugin: object) -> TypeGuard[SchemaProvider]:
    """Check if plugin provides database schemas."""
    return isinstance(plugin, SchemaProvider)


def is_lifecycle_managed(plugin: PluginBase) -> TypeGuard[LifecycleManaged]:
    """Check if plugin requires lifecycle management."""
    return isinstance(plugin, LifecycleManaged)


# NOTE: is_state_consumer, is_embedding_consumer, is_memory_consumer REMOVED (2025-12-06)
# These were code smells - plugins now request services via orchestrator.get_service()
# See: ananta_build/2025-12-06_service_binding_architecture.md


def is_service_provider(plugin: PluginBase) -> TypeGuard[ServiceProvider]:
    """Check if plugin provides a service interface."""
    return isinstance(plugin, ServiceProvider)


def is_io_interface(plugin: PluginBase) -> TypeGuard[IOInterface]:
    """Check if plugin is an I/O interface."""
    return isinstance(plugin, IOInterface)


def is_llm_session_source(plugin: object) -> TypeGuard[LLMSessionSourceInterface]:
    """Check if plugin implements LLMSessionSourceInterface (LLM session ledger source)."""
    from ananta.interfaces.llm_session_source_interface import (
        LLMSessionSourceInterface,
    )

    return isinstance(plugin, LLMSessionSourceInterface)


def collect_llm_session_sources(
    plugins: Mapping[str, object],
) -> dict[str, LLMSessionSourceInterface]:
    """Return every loaded plugin that implements LLMSessionSourceInterface, keyed by name.

    Mirrors ``collect_schemas``. Used by ``SessionLedgerService.Registry`` so
    discovery flows through this centralized capability path rather than bare
    ``isinstance`` at the call site (per this module's docstring).
    """
    discovered: dict[str, LLMSessionSourceInterface] = {}
    for name, plugin in plugins.items():
        if is_llm_session_source(plugin):
            discovered[name] = plugin
    return discovered


def is_async_capable(plugin: PluginBase) -> TypeGuard[AsyncCapable]:
    """Check if plugin supports async operations."""
    return isinstance(plugin, AsyncCapable)


# =============================================================================
# Validation Functions
# =============================================================================


def validate_service_provider(plugin: ServiceProvider) -> None:
    """Validate that a ServiceProvider plugin properly implements its interfaces.

    Checks:
    1. service_interfaces property returns a valid tuple of types
    2. Plugin actually inherits from each declared interface
    3. supported_interface_versions keys match service_interfaces

    Raises:
        ServiceInterfaceMismatchError: If plugin doesn't inherit from a declared interface.
        PluginContractViolationError: If service_interfaces property is broken.
    """
    try:
        declared_set = plugin.service_interfaces
    except Exception as e:
        raise PluginContractViolationError(
            plugin_name=plugin.name,
            protocol="ServiceProvider",
            missing_or_broken="service_interfaces property",
            details=str(e),
        ) from e

    # Validate the plugin actually inherits from each declared interface
    for declared in declared_set:
        if not isinstance(plugin, declared):
            actual_bases = [base.__name__ for base in type(plugin).__mro__[1:10]]
            raise ServiceInterfaceMismatchError(
                plugin_name=plugin.name,
                declared_interface=declared.__name__,
                actual_bases=actual_bases,
            )

    # Validate supported_interface_versions keys match service_interfaces
    try:
        version_map = plugin.supported_interface_versions
    except Exception as e:
        raise PluginContractViolationError(
            plugin_name=plugin.name,
            protocol="ServiceProvider",
            missing_or_broken="supported_interface_versions property",
            details=str(e),
        ) from e

    declared_names = {iface.__name__ for iface in declared_set}
    versioned_names = {iface.__name__ for iface in version_map}
    if declared_names != versioned_names:
        raise PluginContractViolationError(
            plugin_name=plugin.name,
            protocol="ServiceProvider",
            missing_or_broken="supported_interface_versions keys",
            details=(
                f"service_interfaces declares {declared_names} but "
                f"supported_interface_versions has keys {versioned_names}"
            ),
        )


# =============================================================================
# Capability Operations (With Error Context)
# =============================================================================


def collect_schemas(plugins: Mapping[str, object]) -> list[SchemaDefinition]:
    """Collect all schemas from schema-providing plugins.

    Single source of truth for schema collection.

    Raises:
        PluginCapabilityError: If any plugin's get_schema_definitions() fails.
            Contains plugin name, capability, and original error.
    """
    schemas: list[SchemaDefinition] = []

    for name, plugin in plugins.items():
        if not is_schema_provider(plugin):
            continue

        try:
            plugin_schemas = plugin.get_schema_definitions()
            schemas.extend(plugin_schemas)
        except Exception as e:
            raise PluginCapabilityError(
                plugin_name=name,
                capability="SchemaProvider",
                operation="get_schema_definitions",
                original_error=e,
            ) from e

    return schemas


def start_lifecycle_plugins(plugins: dict[str, PluginBase]) -> None:
    """Start all lifecycle-managed plugins.

    Single source of truth for lifecycle startup.

    Raises:
        PluginCapabilityError: If any plugin's start_services() fails.
    """
    for name, plugin in plugins.items():
        if not is_lifecycle_managed(plugin):
            continue

        try:
            plugin.start_services()
            logger.debug(f"Started services for {name}")
        except Exception as e:
            raise PluginCapabilityError(
                plugin_name=name,
                capability="LifecycleManaged",
                operation="start_services",
                original_error=e,
            ) from e


def stop_lifecycle_plugins(plugins: dict[str, PluginBase]) -> None:
    """Stop all lifecycle-managed plugins.

    Single source of truth for lifecycle shutdown.

    Awaitable ``stop_services`` implementations are completed before this
    function returns.  Startup is synchronous, but several lifecycle plugins
    own non-daemon workers behind async stop methods; merely constructing those
    coroutines leaves the workers alive during interpreter shutdown.

    Raises:
        PluginCapabilityError: If any plugin's stop_services() fails. Every
            lifecycle plugin is still offered shutdown before the error is
            raised, so one faulty cleanup cannot strand a later worker.
    """
    failures: list[tuple[str, Exception]] = []
    for name, plugin in plugins.items():
        if not is_lifecycle_managed(plugin):
            continue

        try:
            result = plugin.stop_services()
            if inspect.isawaitable(result):
                asyncio.run(result)
            logger.debug(f"Stopped services for {name}")
        except Exception as e:
            failures.append((name, e))
            logger.exception("Failed to stop services for %s", name)

    if failures:
        name, error = failures[0]
        exception = PluginCapabilityError(
            plugin_name=name,
            capability="LifecycleManaged",
            operation="stop_services",
            original_error=error,
        )
        if len(failures) > 1:
            exception.add_note(
                "Additional lifecycle cleanup failures: "
                + ", ".join(
                    f"{failed_name}: {type(failed_error).__name__}"
                    for failed_name, failed_error in failures[1:]
                )
            )
        raise exception from error


def prepare_lifecycle_plugins(plugins: dict[str, PluginBase]) -> None:
    """Prepare all lifecycle-managed plugins for readiness.

    Raises:
        PluginCapabilityError: If any plugin's prepare_for_readiness() fails.
    """
    for name, plugin in plugins.items():
        if not is_lifecycle_managed(plugin):
            continue

        try:
            plugin.prepare_for_readiness()
        except Exception as e:
            raise PluginCapabilityError(
                plugin_name=name,
                capability="LifecycleManaged",
                operation="prepare_for_readiness",
                original_error=e,
            ) from e


# NOTE: inject_state_service, inject_embedding_service, inject_memory_service REMOVED (2025-12-06)
# These were platform-driven dependency injection - now plugins request services themselves.
# See: ananta_build/2025-12-06_service_binding_architecture.md
