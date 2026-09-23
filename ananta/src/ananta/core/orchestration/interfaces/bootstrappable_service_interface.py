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
            self._init_bootstrap()
        else:
            self._init_plugin()

    def _log_operation(self, operation: str, *args: object, **kwargs: object) -> None:
        if self.bootstrap_mode:
            self.operations_log.append((operation, args, kwargs))

    def transition_to_plugin(self, plugin_manager: object) -> None:
        if not self.bootstrap_mode:
            raise FrameworkError("Service is already in plugin mode")

        # Capture bootstrap state
        bootstrap_data = self._capture_bootstrap_state()

        # Switch to plugin mode
        self.plugin_manager = plugin_manager
        self.bootstrap_mode = False
        self._init_plugin()

        # Replay operations atomically
        self._replay_operations()

        # Restore data
        self._restore_bootstrap_data(bootstrap_data)

    @abstractmethod
    def _init_bootstrap(self) -> None:
        pass

    @abstractmethod
    def _init_plugin(self) -> None:
        pass

    @abstractmethod
    def _capture_bootstrap_state(self) -> dict[str, object]:
        pass

    @abstractmethod
    def _restore_bootstrap_data(self, data: dict[str, object]) -> None:
        pass

    def _replay_operations(self) -> None:
        for _i, (operation, args, kwargs) in enumerate(self.operations_log):
            try:
                method = getattr(self, operation)
                method(*args, **kwargs)
            except Exception as e:
                raise FrameworkError(f"Operation replay failed: {operation}") from e
