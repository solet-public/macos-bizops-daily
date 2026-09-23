from abc import ABC, abstractmethod


class IProcessRegistryManager(ABC):
    @abstractmethod
    def build_and_populate_registry(self) -> None:
        pass

    @abstractmethod
    async def persist_registry(self) -> None:
        pass

    @abstractmethod
    async def load_into_discovery_service(self) -> None:
        pass

    @abstractmethod
    async def save_registry_to_state(self, state: dict[str, object]) -> None:
        """Save the process registry to state. Fails if registry not initialized."""
        pass

    @abstractmethod
    def get_registry_data(self) -> dict[str, object] | None:
        pass
