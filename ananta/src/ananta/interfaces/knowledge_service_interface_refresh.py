"""Process-registry refresh plugin-contract methods (W5.R).

Reload one or all process JSON definitions from a plugin's knowledge
base and merge the changes into the live process registry without a
restart. Lifted byte-for-byte from the W5.R-pre-decomposition
``KnowledgeServiceInterface``.
"""

from abc import ABC, abstractmethod
from typing import Any


class KnowledgeRefreshInterface(ABC):
    """Process-registry refresh abstract methods."""

    @abstractmethod
    def refresh_plugin_processes(self, plugin_name: str) -> dict[str, Any]:
        """Reload all process JSON files and update the live process registry."""
        ...

    @abstractmethod
    def refresh_plugin_process(self, plugin_name: str, process_key: str) -> dict[str, Any]:
        """Reload a single process JSON file and update the live registry entry."""
        ...

