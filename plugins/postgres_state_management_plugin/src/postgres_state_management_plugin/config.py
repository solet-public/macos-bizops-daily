"""Configuration schema for the PostgreSQL state management plugin."""


def get_plugin_config_schema() -> dict[str, object]:
    """Declare configuration schema for the PostgreSQL state management plugin.

    Returns JSON Schema for setup flow to generate UI/prompts.
    """
    return {
        "$schema": "http://json-schema.org/draft-07/schema#",
        "title": "PostgreSQL State Management Plugin",
        "description": "Configuration for PostgreSQL database connection (requires pgvector extension)",
        "type": "object",
        "required": ["pg_host", "pg_database", "pg_user", "pg_password"],
        "properties": {
            "pg_host": {
                "type": "string",
                "title": "Database Host",
                "description": "PostgreSQL server hostname or IP address",
                "default": "localhost",
                "examples": ["localhost", "host.docker.internal", "postgres", "192.168.1.100"],
                "x-group": "connection",
                "x-order": 1,
            },
            "pg_port": {
                "type": "integer",
                "title": "Database Port",
                "description": "PostgreSQL server port",
                "default": 5432,
                "minimum": 1,
                "maximum": 65535,
                "x-group": "connection",
                "x-order": 2,
            },
            "pg_database": {
                "type": "string",
                "title": "Database Name",
                "description": "Name of the PostgreSQL database",
                "default": "ananta_dev",
                "x-group": "connection",
                "x-order": 3,
            },
            "pg_user": {
                "type": "string",
                "title": "Database User",
                "description": "PostgreSQL username",
                "default": "ananta",
                "x-group": "connection",
                "x-order": 4,
            },
            "pg_password": {
                "type": "string",
                "title": "Database Password",
                "description": "PostgreSQL password",
                "x-secret": True,
                "x-group": "security",
                "x-order": 1,
            },
            "pg_schema": {
                "type": "string",
                "title": "Schema Name",
                "description": "PostgreSQL schema name (namespace for this solet)",
                "default": "ananta",
                "pattern": "^[a-z][a-z0-9_]*$",
                "x-group": "connection",
                "x-order": 5,
            },
            "pool_size": {
                "type": "integer",
                "title": "Connection Pool Size",
                "description": "Maximum number of database connections in the pool",
                "default": 5,
                "minimum": 1,
                "maximum": 50,
                "x-group": "advanced",
                "x-order": 1,
            },
            "pool_timeout": {
                "type": "integer",
                "title": "Pool Timeout",
                "description": "Timeout in seconds waiting for a connection from the pool",
                "default": 30,
                "minimum": 1,
                "maximum": 300,
                "x-group": "advanced",
                "x-order": 2,
            },
        },
        "x-test-method": "test_connection",
        "x-requires-extension": "pgvector",
    }
