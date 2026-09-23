from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime
from typing import Any


@dataclass
class SetupResult:
    """Result of a database setup operation.

    Used by start_build wizard to test connections and create databases
    before the plugin is initialized.
    """

    success: bool
    message: str
    details: dict[str, Any]


@dataclass
class ActionExecutionRecord:
    id: str
    action_name: str
    provider_type: str
    provider: str
    status: str
    parameters: str | None = None  # JSON string
    result: str | None = None  # JSON string
    error: str | None = None
    duration_ms: int | None = None
    started_at: datetime | None = None
    completed_at: datetime | None = None
    source_context: str | None = None  # JSON string
    external_id: str | None = None  # Business identifier
    is_deleted: bool = False  # Business soft delete flag
    tags: str | None = None  # Business categorization


class StateProviderInterface(ABC):
    @abstractmethod
    def record_action_execution(self, execution_record: ActionExecutionRecord) -> bool: ...

    @abstractmethod
    def update_action_execution(self, id: str, updates: dict[str, object]) -> bool: ...

    @abstractmethod
    def get_action_execution(self, id: str) -> ActionExecutionRecord | None: ...

    @abstractmethod
    def list_action_executions(
        self,
        filters: dict[str, object] | None = None,
        _limit: int | None = None,
        _order_by: str | None = None,
    ) -> list[ActionExecutionRecord]: ...

    @abstractmethod
    def create_action_execution_schema(self) -> bool:
        """Create action execution schema if it doesn't exist. Returns True on success."""
        ...
