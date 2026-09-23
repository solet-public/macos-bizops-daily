"""Canonical StateService Protocol.

This module defines the protocol that StateService implements and that consumers
should type-hint against. This enables proper structural subtyping without requiring
concrete class imports.

IMPORTANT: This is the SINGLE SOURCE OF TRUTH for StateService's interface.
Do NOT define local StateServiceProtocol classes in other modules.
"""

from typing import Protocol, runtime_checkable

from ananta.core.domain.types import ActionResult


@runtime_checkable
class StateServiceProtocol(Protocol):
    """Protocol defining the StateService interface.

    This protocol enables structural subtyping for StateService consumers.
    Plugins and services should type-hint state_service parameters as
    StateServiceProtocol rather than the concrete StateService class.

    Usage:
        from ananta.interfaces.state_service_protocol import StateServiceProtocol

        class MyPlugin:
            _state_service: StateServiceProtocol | None = None

            def set_state_service(self, state_service: StateServiceProtocol) -> None:
                self._state_service = state_service
    """

    # Core database operations
    def create_schema(self, namespace: str, schema: dict[str, object]) -> ActionResult:
        """Create schema for namespace."""
        ...

    def read_state(self, namespace: str, query: dict[str, object]) -> ActionResult:
        """Read state from namespace."""
        ...

    def write_state(
        self,
        namespace: str,
        data: dict[str, object],
        calling_service: str | None = None,
        calling_namespace: str | None = None,
    ) -> ActionResult:
        """Write state to namespace."""
        ...

    def update_state(
        self, namespace: str, query: dict[str, object], updates: dict[str, object]
    ) -> ActionResult:
        """Update state in namespace."""
        ...

    def upsert_state(self, namespace: str, data: dict[str, object]) -> ActionResult:
        """Upsert state in namespace."""
        ...

    def delete_records(self, namespace: str, query: dict[str, object]) -> ActionResult:
        """Delete records from namespace."""
        ...

    def query_state(self, namespace: str, filters: dict[str, object]) -> ActionResult:
        """DEPRECATED alias for :meth:`read_state` — prefer ``read_state``.

        ``filters`` is the whole ``{table, filters, limit?, unbounded?}`` query
        envelope, not just a filter mapping: it is forwarded unchanged and
        becomes ``read_state``'s ``query``, so ``limit`` and ``unbounded`` are
        honoured and the ``MAX_READ_ROWS`` bound applies.
        """
        ...

    def query_ordered(self, namespace: str, data: dict[str, object]) -> ActionResult:
        """Ordered, bounded, tie-safe query (filters + order_by + limit + after)."""
        ...

    def count(self, namespace: str, data: dict[str, object]) -> ActionResult:
        """Count rows in a filtered set; the scalar lands at ``data.result.value``.

        The SQL aggregate runs inside the owner plugin and ships a number, not
        rows. Declared here because it is the sanctioned repair for a call site
        that reads a whole table only to call ``len()`` on the result — the
        cheapest of those reads is still the most expensive way to ask "how
        many". See ``StateManagementInterface.count`` for the full ``data``
        contract, including that there is NO automatic ``is_deleted``
        exclusion.
        """
        ...

    def execute_sql(
        self,
        sql_query: str,
        sql_params: list[object] | None = None,
        calling_service: str = "StateService",
        calling_namespace: str = "ananta.services.state_service",
    ) -> ActionResult:
        """Execute SQL query."""
        ...

    def list_namespaces(self) -> ActionResult:
        """List all namespaces."""
        ...

    def describe_schema(self, namespace: str) -> ActionResult:
        """Describe schema for namespace."""
        ...

    def mark_as_read(self, namespace: str, query: dict[str, object]) -> ActionResult:
        """Mark records as read."""
        ...

    def initialize_database(self, config: dict[str, object] | None = None) -> ActionResult:
        """Initialize database."""
        ...

    # String generation
    def generate_unique_string(self, length: int = 13, encoding: str = "base36") -> ActionResult:
        """Generate unique string with full ActionResult response."""
        ...

    def generate_id(self, length: int = 13, prefix: str = "") -> str:
        """Generate a unique ID string.

        Convenience method that returns the string directly instead of ActionResult.
        Raises RuntimeError on failure.

        Args:
            length: Length of random portion (1-64 chars, default: 13)
            prefix: Optional prefix for the ID (e.g., "voice-", "flow-")

        Returns:
            Generated ID string, optionally prefixed
        """
        ...

    # Key-value operations
    def set_key_value(
        self,
        namespace: str,
        key: str,
        value: str | int | float | bool | dict[str, object] | list[object] | None,
        scope: str = "GLOBAL",
        ttl: int | None = None,
    ) -> ActionResult:
        """Set key-value pair."""
        ...

    def get_key_value(self, namespace: str, key: str, scope: str = "GLOBAL") -> ActionResult:
        """Get key-value pair."""
        ...

    def delete_key_value(self, namespace: str, key: str, scope: str = "GLOBAL") -> ActionResult:
        """Delete key-value pair."""
        ...

    def clear_key_values(
        self, namespace: str | None = None, scope: str | None = None
    ) -> ActionResult:
        """Clear key-value pairs."""
        ...

    def list_key_values(
        self,
        namespace: str | None = None,
        scope: str | None = None,
        pattern: str | None = None,
    ) -> ActionResult:
        """List key-value pairs."""
        ...

    # Async job operations
    def create_async_job(
        self,
        job_id: str,
        provider_type: str,
        provider: str,
        action_name: str,
        request_data: dict[str, object],
    ) -> ActionResult:
        """Create async job."""
        ...

    def get_async_job(self, job_id: str) -> ActionResult:
        """Get async job."""
        ...

    def update_async_job(self, job_id: str, updates: dict[str, object]) -> ActionResult:
        """Update async job."""
        ...
