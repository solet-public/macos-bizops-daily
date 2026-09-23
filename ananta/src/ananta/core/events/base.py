import logging
import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import UTC, datetime

from .utils import generate_action_name

logger = logging.getLogger(__name__)


@dataclass
class Event(ABC):
    event_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    timestamp: datetime = field(default_factory=lambda: datetime.now(UTC))
    source: str = field(default="unknown")
    data: dict[str, object] = field(default_factory=dict)
    correlation_id: str | None = None

    def __post_init__(self) -> None:
        if self.data:
            pass

    @abstractmethod
    def event_type(self) -> str:
        pass

    def to_dict(self) -> dict[str, object]:
        return {
            "event_id": self.event_id,
            "timestamp": self.timestamp.isoformat(),
            "source": self.source,
            "data": self.data,
            "correlation_id": self.correlation_id,
            "event_type": self.event_type(),
        }


@dataclass
class ActionEvent(Event):
    action_name: str = field(default="unknown")
    parameters: dict[str, object] = field(default_factory=dict)
    provider: str = field(default="unknown")
    session_id: str | None = None
    flow_id: str | None = None
    parent_event_id: str | None = None
    action_definition: dict[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        super().__post_init__()

    def event_type(self) -> str:
        return f"action.{self.action_name}"

    @classmethod
    def from_dict(cls, action_dict: dict[str, object]) -> "ActionEvent":
        # DRY: Use centralized action name generation
        action_name = generate_action_name(action_dict, "from_dict_conversion")

        # Extract and validate parameters
        raw_parameters = action_dict.get("parameters", action_dict.get("arguments", {}))
        if not isinstance(raw_parameters, dict):
            raw_parameters = {}
        parameters: dict[str, object] = raw_parameters

        # Extract and validate provider
        raw_provider = action_dict.get("provider", "unknown")
        provider: str = raw_provider if isinstance(raw_provider, str) else "unknown"

        # Extract and validate session_id
        raw_session_id = action_dict.get("session_id")
        session_id: str | None = raw_session_id if isinstance(raw_session_id, str) else None

        # Extract and validate flow_id
        raw_flow_id = action_dict.get("flow_id")
        flow_id: str | None = raw_flow_id if isinstance(raw_flow_id, str) else None

        return cls(
            action_name=action_name,
            parameters=parameters,
            provider=provider,
            session_id=session_id,
            flow_id=flow_id,
            source="legacy_conversion",
            action_definition=action_dict,  # CRITICAL: Preserve complete original action
        )


@dataclass
class SystemEvent(Event):
    system_event_type: str = field(default="unknown")
    context: dict[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        super().__post_init__()

    def event_type(self) -> str:
        return f"system.{self.system_event_type}"


@dataclass
class EventResult:
    success: bool = True
    resulting_events: list[Event] = field(default_factory=list)
    error: Exception | None = None
    data: dict[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.error:
            pass
