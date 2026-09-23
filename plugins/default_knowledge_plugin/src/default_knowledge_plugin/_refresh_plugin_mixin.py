"""DefaultKnowledgePlugin process-registry refresh sub-Mixin (W5.T).

Two KSI process-registry refresh methods (refresh_plugin_processes /
refresh_plugin_process) lifted byte-for-byte from the W5.T-pre-decomposition
``DefaultKnowledgePlugin``. Inherited via MI from the residual class.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

from .constants import PLUGIN_NAME
from .kb_process_registry import do_refresh_plugin_process, do_refresh_plugin_processes


class KnowledgeRefreshPluginMixin:
    """Process-registry refresh verb implementations. Inherited via MI."""

    if TYPE_CHECKING:
        # Service-state attributes owned by DefaultKnowledgePlugin.__init__ + prepare_for_readiness.
        # orchestrator_ref is inherited from PluginBase on the residual class.
        _kb_root: Path | None
        orchestrator_ref: Any

    def refresh_plugin_processes(self, plugin_name: str) -> dict[str, Any]:
        """Reload all process JSON files and update the live process registry."""
        if self.orchestrator_ref is None:
            raise RuntimeError(f"{PLUGIN_NAME}: orchestrator_ref not set")
        if self._kb_root is None:
            raise RuntimeError(f"{PLUGIN_NAME}: knowledge_base_root not configured")
        return do_refresh_plugin_processes(plugin_name, self._kb_root, self.orchestrator_ref)

    def refresh_plugin_process(
        self, plugin_name: str, process_key: str,
    ) -> dict[str, Any]:
        """Reload a single process JSON file and update the live registry entry."""
        if self.orchestrator_ref is None:
            raise RuntimeError(f"{PLUGIN_NAME}: orchestrator_ref not set")
        if self._kb_root is None:
            raise RuntimeError(f"{PLUGIN_NAME}: knowledge_base_root not configured")
        return do_refresh_plugin_process(
            plugin_name, process_key, self._kb_root, self.orchestrator_ref,
        )

