"""Configuration model for pgvector-backed plugins."""

from typing import Self

from pydantic import BaseModel, Field, model_validator


def _get_default_schema() -> str:
    from ananta.core.config.environment_config import EnvironmentConfig

    return EnvironmentConfig.solet_name()


class PGVectorConfig(BaseModel):
    """Configuration for a pgvector-backed plugin.

    Attributes:
        host: PostgreSQL server host address
        port: PostgreSQL server port
        database: Database name
        user: Database user
        password: Database password
        db_schema: PostgreSQL schema for vector tables (defaults to SOLET_NAME)
        pool_size: Connection pool size
        hnsw_m: HNSW index parameter - max number of connections per layer
        hnsw_ef_construction: HNSW index parameter - size of dynamic candidate list
    """

    host: str = Field(default="localhost", description="PostgreSQL host")
    port: int = Field(default=5432, ge=1, le=65535, description="PostgreSQL port")
    database: str = Field(default="ananta_db", description="Database name")
    user: str = Field(default="ananta_user", description="Database user")
    password: str = Field(default="change_me", description="Database password")
    db_schema: str | None = Field(
        default=None,
        description="Schema for vector tables (defaults to SOLET_NAME)",
    )

    @property
    def schema_name(self) -> str:
        """Get the schema name (guaranteed non-None after construction)."""
        if self.db_schema is None:
            raise RuntimeError("db_schema should be set by model_validator")
        return self.db_schema

    pool_size: int = Field(default=10, ge=1, description="Connection pool size")
    hnsw_m: int = Field(default=16, ge=1, description="HNSW max connections per layer")
    hnsw_ef_construction: int = Field(
        default=64, ge=1, description="HNSW construction candidate list size"
    )

    @model_validator(mode="after")
    def set_schema_from_solet_name(self) -> Self:
        """Default db_schema to solet name if not explicitly set."""
        if self.db_schema is None:
            self.db_schema = _get_default_schema()
        return self
