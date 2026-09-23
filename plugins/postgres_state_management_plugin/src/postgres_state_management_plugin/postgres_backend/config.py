"""
PostgreSQL State Plugin Configuration

Pydantic models for plugin configuration with validation.
"""

from typing import Self

from pydantic import BaseModel, Field, field_validator, model_validator

# Port validation constants
MIN_PORT = 1
MAX_PORT = 65535


def _get_default_schema() -> str:
    """The per-solet identity default (SOLET_NAME, the single source
    of truth). Shared by BOTH pg_schema and the database name -- each solet
    has its own database AND schema, both named after it (operator ruling,
    2026-07-11). Name kept as-is for cross-plugin consistency (the same helper
    exists in the rds/pgvector siblings).
    """
    from ananta.core.config.environment_config import EnvironmentConfig

    return EnvironmentConfig.solet_name()


class PostgresConfig(BaseModel):
    """PostgreSQL connection and pool configuration."""

    model_config = {"populate_by_name": True}

    host: str = Field(default="localhost", description="PostgreSQL server host")
    port: int = Field(default=5432, description="PostgreSQL server port")
    database: str = Field(
        default_factory=_get_default_schema,
        description="Database name (defaults to SOLET_NAME for"
        " per-solet isolation, mirroring pg_schema)",
    )
    user: str = Field(default="ananta_user", description="Database user")
    password: str = Field(default="change_me", description="Database password")
    # pg_schema is guaranteed non-None after model_validator runs
    # Type is str | None only because pydantic Field needs to accept None initially
    pg_schema: str | None = Field(
        default=None,
        description="Schema name (defaults to SOLET_NAME for isolation)",
        alias="schema",
    )

    @property
    def schema_name(self) -> str:
        """Get the schema name (guaranteed non-None after construction)."""
        if self.pg_schema is None:
            raise RuntimeError("pg_schema should be set by model_validator")
        return self.pg_schema

    pool_size: int = Field(default=20, description="Connection pool size")
    connection_timeout: int = Field(default=30, description="Connection timeout in seconds")

    @model_validator(mode="after")
    def set_schema_from_solet_name(self) -> Self:
        """Default pg_schema to solet name if not explicitly set."""
        if self.pg_schema is None:
            self.pg_schema = _get_default_schema()
        return self

    @field_validator("port")
    @classmethod
    def validate_port(cls, v: int) -> int:
        """Validate port is in valid range."""
        if not MIN_PORT <= v <= MAX_PORT:
            raise ValueError(f"Port must be between {MIN_PORT} and {MAX_PORT}, got {v}")
        return v

    @field_validator("pool_size")
    @classmethod
    def validate_pool_size(cls, v: int) -> int:
        """Validate pool size is positive."""
        if v <= 0:
            raise ValueError(f"pool_size must be greater than 0, got {v}")
        return v

    @field_validator("connection_timeout")
    @classmethod
    def validate_connection_timeout(cls, v: int) -> int:
        """Validate connection timeout is positive."""
        if v <= 0:
            raise ValueError(f"connection_timeout must be greater than 0, got {v}")
        return v
