"""Knowledge Service — wrapper over bound knowledge plugin.

Concrete-via-MI composition of 5 domain sub-Wrapper mixins post-W5.S. Each
sub-Wrapper holds the delegate methods for one domain (Lifecycle / Search /
FileOps / Refresh / Maintenance) per the W5.Q + W5.R + W5.S three-layer
5-domain split. Backward-compat preserved:
``from ananta.services.knowledge_service import KnowledgeService`` continues
to resolve; every existing delegate method is reachable on the concrete class
via MI; ``isinstance(ks, KnowledgeServiceInterface)`` still returns True.

Follows the standard platform pattern (like MemoryService):
- Thin wrapper that delegates to bound plugin
- Plugin provides the single implementation
- Eager initialization — fails fast in _init_plugin(), not lazily
"""

import logging

from ananta.core.plugins.plugin_manager import PluginManager
from ananta.error_handling import FrameworkError
from ananta.interfaces.knowledge_service_interface import KnowledgeServiceInterface

from .knowledge_service_file_ops import KnowledgeFileOpsWrapper
from .knowledge_service_lifecycle import KnowledgeLifecycleWrapper
from .knowledge_service_maintenance import KnowledgeMaintenanceWrapper
from .knowledge_service_refresh import KnowledgeRefreshWrapper
from .knowledge_service_search import KnowledgeSearchWrapper

logger = logging.getLogger(__name__)


class KnowledgeService(
    KnowledgeLifecycleWrapper,
    KnowledgeSearchWrapper,
    KnowledgeFileOpsWrapper,
    KnowledgeRefreshWrapper,
    KnowledgeMaintenanceWrapper,
    KnowledgeServiceInterface,
):
    """Service wrapper for knowledge plugin providers.

    Concrete-via-MI composition of 5 sub-Wrapper mixins (Lifecycle / Search /
    FileOps / Refresh / Maintenance) per the W5.Q+W5.R+W5.S 5-domain split.
    Each mixin holds 2-6 delegate methods that call ``self._get_backend()`` to
    reach the bound plugin; MRO resolves ``_get_backend`` to the concrete class
    below.

    Initialization is EAGER, not lazy:
    - Plugin is resolved and validated in _init_plugin()
    - _get_backend() assumes initialization is complete
    - NO _ensure_ready() lazy initialization pattern

    Backward-compat preserved: every callsite using
    ``ks.install(...)`` / ``ks.search(...)`` / ``ks.purge_orphaned_chunks(...)``
    etc. continues to work; the verbs are now contributed by the sub-Wrappers
    rather than declared directly on this class, but MI inheritance makes them
    transparently reachable on every concrete instance.
    """

    def __init__(
        self,
        plugin_manager: PluginManager,
        knowledge_plugin_name: str,
    ) -> None:
        if not knowledge_plugin_name:
            raise FrameworkError(
                "knowledge_plugin_name is required. "
                "Ensure KNOWLEDGE_SERVICE is bound in config/service_bindings.json."
            )

        self._knowledge_plugin_name = knowledge_plugin_name
        self._knowledge_plugin: KnowledgeServiceInterface | None = None
        self._plugin_manager = plugin_manager

        self._init_plugin()

    def _init_plugin(self) -> None:
        """Eagerly initialize the knowledge plugin. Fails fast on any error."""
        logger.debug(f"KnowledgeService initializing with plugin: {self._knowledge_plugin_name}")

        plugin = self._plugin_manager.get_plugin(self._knowledge_plugin_name)

        if not isinstance(plugin, KnowledgeServiceInterface):
            raise FrameworkError(
                f"Knowledge plugin '{self._knowledge_plugin_name}' does not implement "
                f"KnowledgeServiceInterface. Plugin type: {type(plugin)}"
            )

        if not plugin.is_ready():
            error = plugin.readiness_error or "Unknown"
            raise FrameworkError(f"Knowledge plugin not ready: {error}")

        setter = getattr(plugin, "set_as_active_provider", None)
        if callable(setter):
            setter("KnowledgeServiceInterface")

        self._knowledge_plugin = plugin
        logger.debug("KnowledgeService initialization complete")

    def _get_backend(self) -> KnowledgeServiceInterface:
        if self._knowledge_plugin is None:
            raise FrameworkError(
                "Knowledge plugin not initialized. "
                "This indicates _init_plugin() was not called — programming error."
            )
        return self._knowledge_plugin


__all__ = ["KnowledgeService"]
