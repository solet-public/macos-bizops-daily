from abc import ABC, abstractmethod


class BaseProvider(ABC):
    def __init__(self) -> None:
        self.provider_name = self.__class__.__name__
        self._initialized = False

    def initialize(self) -> bool:
        self._initialized = False
        return False

    @property
    @abstractmethod
    def is_initialized(self) -> bool: ...
