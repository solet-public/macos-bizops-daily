import logging
from abc import ABC, abstractmethod

from ananta.error_handling import FrameworkError

logger = logging.getLogger(__name__)


class BootstrappableServiceInterface(ABC):
    def __init__(self, plugin_manager: object | None = None):
        self.plugin_manager = plugin_manager
        self.bootstrap_mode = plugin_manager is None
        self.operations_log: list[tuple[str, tuple[object, ...], dict[str, object]]] = []

        if self.bootstrap_mode:
            logger.debug(f"{self.__class__.__name__} initializing in bootstrap mode")
            self._init_bootstrap()
        else:
            logger.debug(f"{self.__class__.__name__} initializing in plugin mode")
            self._init_plugin()

    def _log_operation(self, operation: str, *args: object, **kwargs: object) -> None:
        if self.bootstrap_mode:
            self.operations_log.append((operation, args, kwargs))

    def transition_to_plugin(self, plugin_manager: object) -> None:
        if not self.bootstrap_mode:
            raise FrameworkError("Service is already in plugin mode")

        logger.debug(f"Transitioning {self.__class__.__name__} from bootstrap to plugin mode")

        # Capture bootstrap state BEFORE switching modes
        bootstrap_data = self._capture_bootstrap_state()

        # Switch to plugin mode
        self.plugin_manager = plugin_manager
        self.bootstrap_mode = False
        self._init_plugin()

        # Restore data directly (skip operation replay to avoid circular dependencies)
        logger.debug("Skipping operation replay to avoid circular dependencies")
        self._restore_bootstrap_data(bootstrap_data)

        logger.debug(f"{self.__class__.__name__} transition completed successfully")

    @abstractmethod
    def _init_bootstrap(self) -> None: ...

    @abstractmethod
    def _init_plugin(self) -> None: ...

    @abstractmethod
    def _capture_bootstrap_state(self) -> dict[str, object]: ...

    @abstractmethod
    def _restore_bootstrap_data(self, data: dict[str, object]) -> None: ...

    def _replay_operations(self) -> None:
        for operation, args, kwargs in self.operations_log:
            method = getattr(self, operation)
            method(*args, **kwargs)

        self.operations_log.clear()
