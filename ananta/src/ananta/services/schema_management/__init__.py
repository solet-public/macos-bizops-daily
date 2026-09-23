"""Schema Management Service Package.

This package provides focused schema management functionality extracted from StateService.
Implements Strategy pattern for bootstrap vs plugin mode operations.
"""

from .bootstrap_schema_storage import BootstrapSchemaStorage
from .namespace_validator import NamespaceValidator
from .plugin_schema_storage import PluginSchemaStorage
from .schema_management_service import SchemaManagementService
from .schema_registry_service import SchemaRegistryService
from .schema_storage_strategy import SchemaStorageStrategy

__all__ = [
    "SchemaManagementService",
    "SchemaStorageStrategy",
    "BootstrapSchemaStorage",
    "PluginSchemaStorage",
    "NamespaceValidator",
    "SchemaRegistryService",
]
