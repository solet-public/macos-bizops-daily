"""Knowledge-service wrapper sub-mixin for process-registry refresh delegates (W5.S).

Two delegates satisfying the W5.R-decomposed :class:`KnowledgeRefreshInterface`:
refresh_plugin_processes and refresh_plugin_process. Lifted byte-for-byte from
the W5.S-pre-decomposition ``KnowledgeService.__init__.py``.
"""

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from ananta.interfaces.knowledge_service_interface_refresh import (
        KnowledgeRefreshInterface,
    )


class KnowledgeRefreshWrapper:
    """Process-registry refresh delegate methods. Inherited via MI."""

    if TYPE_CHECKING:
        def _get_backend(self) -> "KnowledgeRefreshInterface": ...

    def refresh_plugin_processes(self, plugin_name: str) -> dict[str, Any]:
        return self._get_backend().refresh_plugin_processes(plugin_name=plugin_name)

    def refresh_plugin_process(self, plugin_name: str, process_key: str) -> dict[str, Any]:
        return self._get_backend().refresh_plugin_process(
            plugin_name=plugin_name, process_key=process_key,
        )
