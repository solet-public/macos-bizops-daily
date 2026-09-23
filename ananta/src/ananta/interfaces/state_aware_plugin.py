from abc import ABC, abstractmethod

from ananta.interfaces.state_service_protocol import StateServiceProtocol
from ananta.types.schema_types import SchemaDefinition


class StateAwarePlugin(ABC):
    """Base class for plugins that require state service access."""

    # State service is set after initialization via set_state_service
    _state_service: StateServiceProtocol | None = None

    @abstractmethod
    def get_schema_definitions(self) -> list[SchemaDefinition]:
        pass

    def set_state_service(self, state_service: StateServiceProtocol) -> None:
        self._state_service = state_service
