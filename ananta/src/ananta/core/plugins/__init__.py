"""Plugin Infrastructure - Plugin loading, lifecycle, and validation.

This package provides the plugin system infrastructure including:
- PluginManager: Plugin discovery, loading, and lifecycle management
- PluginBase: Base class for all plugins
- Plugin contracts: ActionStatus, ErrorCode, ErrorSeverity enums
- Capability protocols: SchemaProvider, LifecycleManaged, ServiceProvider, etc.
- Capability detection: TypeGuard functions and batch operations
- Validation: Plugin validation registry and phases
- Utilities: Helper functions for plugin operations

The plugins package handles all plugin-related concerns including discovery,
loading, validation, and lifecycle management.

NOTE (2025-12-06): StateConsumer, EmbeddingConsumer, MemoryConsumer protocols REMOVED.
Plugins now request services via orchestrator.get_service() in prepare_for_readiness().
See: ananta_build/2025-12-06_service_binding_architecture.md
"""

from ananta.core.plugins.capabilities import (
    collect_schemas,
    is_async_capable,
    is_io_interface,
    is_lifecycle_managed,
    is_schema_provider,
    is_service_provider,
    prepare_lifecycle_plugins,
    start_lifecycle_plugins,
    stop_lifecycle_plugins,
    validate_service_provider,
)
from ananta.core.plugins.exceptions import (
    PluginCapabilityError,
    PluginContractViolationError,
    ServiceInterfaceMismatchError,
)
from ananta.core.plugins.plugin_base import PluginBase
from ananta.core.plugins.plugin_contracts import (
    ActionStatus,
    ErrorCode,
    ErrorSeverity,
)
from ananta.core.plugins.plugin_manager import PluginManager
from ananta.core.plugins.plugin_utils import extract_actions_from_data
from ananta.core.plugins.plugin_validation import (
    PluginValidationRegistry,
    ValidationPhase,
)
from ananta.core.plugins.protocols import (
    AsyncCapable,
    IOInterface,
    LifecycleManaged,
    SchemaProvider,
    ServiceProvider,
)

__all__ = [
    # Plugin Base
    "PluginBase",
    # Plugin Manager
    "PluginManager",
    # Plugin Contracts (widely used enums)
    "ActionStatus",
    "ErrorCode",
    "ErrorSeverity",
    # Capability Protocols (TRUE platform contracts)
    "SchemaProvider",
    "LifecycleManaged",
    "ServiceProvider",
    "IOInterface",
    "AsyncCapable",
    # Capability Detection (TypeGuards)
    "is_schema_provider",
    "is_lifecycle_managed",
    "is_service_provider",
    "is_io_interface",
    "is_async_capable",
    # Capability Operations
    "collect_schemas",
    "start_lifecycle_plugins",
    "stop_lifecycle_plugins",
    "prepare_lifecycle_plugins",
    "validate_service_provider",
    # Capability Exceptions
    "PluginCapabilityError",
    "PluginContractViolationError",
    "ServiceInterfaceMismatchError",
    # Validation
    "PluginValidationRegistry",
    "ValidationPhase",
    # Utilities
    "extract_actions_from_data",
]
