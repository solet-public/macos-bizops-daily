"""Embedding Service Provider Interface - Internal implementation contract."""

from abc import ABC, abstractmethod


class EmbeddingProvider(ABC):
    """Internal embedding provider contract - NOT AI-discoverable.

    This interface defines lifecycle methods required by embedding plugin implementations.
    """

    # Framework Lifecycle (NOT AI-discoverable)

    @abstractmethod
    def is_ready(self) -> bool:
        """Framework lifecycle - Check if provider is ready."""
        ...

    @abstractmethod
    def get_readiness_error(self) -> str | None:
        """Framework lifecycle - Get readiness error details."""
        ...

