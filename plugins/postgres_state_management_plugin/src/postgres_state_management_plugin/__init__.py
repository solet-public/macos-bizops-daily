"""
PostgreSQL State Plugin

PostgreSQL-based state management implementation with connection pooling,
schema isolation, and full CRUD operations.
"""

from ananta.interfaces.state_provider_interface import SetupResult

from postgres_state_management_plugin.postgres_backend.config import PostgresConfig
from postgres_state_management_plugin.postgres_backend.database_setup import (
    create_database,
    create_schema,
    database_exists,
    setup_database,
    test_connection,
)
from postgres_state_management_plugin.postgres_backend.provider import PostgresProvider

from .plugin import PostgresStatePlugin

__all__ = [
    "PostgresConfig",
    "PostgresStatePlugin",
    "PostgresProvider",
    # Database setup utilities (for start_build wizard)
    "SetupResult",
    "create_database",
    "create_schema",
    "database_exists",
    "setup_database",
    "test_connection",
]
