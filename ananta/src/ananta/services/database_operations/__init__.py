"""Database Operations Service Package.

This package provides focused database operation functionality extracted from StateService.
Implements Strategy pattern for bootstrap vs plugin mode operations with proper CRUD encapsulation.
"""

from .bootstrap_database_storage import BootstrapDatabaseStorage
from .database_operation_service import DatabaseOperationService
from .database_storage_strategy import DatabaseStorageStrategy
from .plugin_database_storage import PluginDatabaseStorage

__all__ = [
    "DatabaseOperationService",
    "DatabaseStorageStrategy",
    "BootstrapDatabaseStorage",
    "PluginDatabaseStorage",
]
