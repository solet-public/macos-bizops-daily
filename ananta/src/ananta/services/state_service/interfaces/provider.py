"""State Service Provider Interface - Internal implementation contract."""

from abc import ABC, abstractmethod
from typing import Any

from ananta.core.domain.types import ActionResult


class StateProvider(ABC):
    """Internal state provider contract - NOT AI-discoverable.

    This interface defines write operations, lifecycle methods, and internal
    utilities required by state management plugin implementations.
    """

    # Write Operations (NOT AI-discoverable - internal only)

    @abstractmethod
    def create_schema(self, namespace: str, schema: dict[str, Any]) -> ActionResult:
        """INTERNAL - Create database schema for namespace."""
        ...

    @abstractmethod
    def write_state(
        self,
        namespace: str,
        data: dict[str, Any],
        calling_service: str | None = None,
        calling_namespace: str | None = None,
    ) -> ActionResult:
        """INTERNAL - Write data to state database."""
        ...

    @abstractmethod
    def update_state(
        self, namespace: str, query: dict[str, Any], updates: dict[str, Any]
    ) -> ActionResult:
        """INTERNAL - Update existing records in state database."""
        ...

    @abstractmethod
    def delete_records(self, namespace: str, query: dict[str, Any]) -> ActionResult:
        """INTERNAL - Delete records from state database."""
        ...

    @abstractmethod
    def query_state(self, namespace: str, filters: dict[str, Any]) -> ActionResult:
        """INTERNAL - DEPRECATED alias for ``read_state``; prefer ``read_state``.

        ``filters`` is the whole ``{table, filters, limit?, unbounded?}`` query
        envelope, forwarded unchanged; ``limit`` / ``unbounded`` are honoured
        and the ``MAX_READ_ROWS`` bound applies.
        """
        ...

    @abstractmethod
    def mark_as_read(self, namespace: str, query: dict[str, Any]) -> ActionResult:
        """INTERNAL - Mark records as read/processed."""
        ...

    # Key-Value Operations (NOT AI-discoverable)

    @abstractmethod
    def set_key_value(
        self,
        namespace: str,
        key: str,
        value: str | int | float | bool | dict[str, Any] | list[Any] | None,
        scope: str = "GLOBAL",
        ttl: int | None = None,
    ) -> ActionResult:
        """INTERNAL - Set key-value pair."""
        ...

    @abstractmethod
    def get_key_value(self, namespace: str, key: str, scope: str = "GLOBAL") -> ActionResult:
        """INTERNAL - Get key-value pair."""
        ...

    @abstractmethod
    def delete_key_value(self, namespace: str, key: str, scope: str = "GLOBAL") -> ActionResult:
        """INTERNAL - Delete key-value pair."""
        ...

    @abstractmethod
    def clear_key_values(
        self, namespace: str | None = None, scope: str | None = None
    ) -> ActionResult:
        """INTERNAL - Clear multiple key-value pairs."""
        ...

    @abstractmethod
    def list_key_values(
        self, namespace: str | None = None, scope: str | None = None, pattern: str | None = None
    ) -> ActionResult:
        """INTERNAL - List key-value pairs matching criteria."""
        ...

    # Framework Lifecycle (NOT AI-discoverable)

    @abstractmethod
    def initialize_database(self, config: dict[str, Any] | None = None) -> ActionResult:
        """Framework lifecycle - Initialize database."""
        ...

    @abstractmethod
    def is_ready(self) -> bool:
        """Framework lifecycle - Check if provider is ready."""
        ...

    @abstractmethod
    def get_readiness_error(self) -> str | None:
        """Framework lifecycle - Get readiness error details."""
        ...

