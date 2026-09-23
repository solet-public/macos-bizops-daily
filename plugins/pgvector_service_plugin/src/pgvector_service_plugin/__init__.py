"""PGVector Service Plugin for Ananta Platform.

PostgreSQL/pgvector-backed vector storage and similarity search.
Machinery lives in pgvector_service_plugin.postgres_backend.vector.
"""

from pgvector_service_plugin.postgres_backend.vector.config import PGVectorConfig
from pgvector_service_plugin.postgres_backend.vector.provider import PGVectorProvider

from .plugin import PGVectorServicePlugin

__all__ = [
    "PGVectorServicePlugin",
    "PGVectorProvider",
    "PGVectorConfig",
]

__version__ = "1.0.0"
