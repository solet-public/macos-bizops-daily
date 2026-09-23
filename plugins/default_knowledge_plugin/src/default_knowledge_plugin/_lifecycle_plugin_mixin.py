"""DefaultKnowledgePlugin lifecycle sub-Mixin (W5.T).

Six KSI lifecycle methods (install / uninstall / update / list_installed /
activate / deactivate) lifted byte-for-byte from the W5.T-pre-decomposition
``DefaultKnowledgePlugin``. Inherited via MI from the residual class.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

from .constants import PLUGIN_NAME
from .kb_lifecycle import (
    activate_kb,
    deactivate_kb,
    ingest_kb,
    install_kb,
    list_installed_kbs,
    uninstall_kb,
    update_kb,
)


class KnowledgeLifecyclePluginMixin:
    """KB lifecycle verb implementations. Inherited via MI from DefaultKnowledgePlugin."""

    if TYPE_CHECKING:
        # Service-state attributes owned by DefaultKnowledgePlugin.__init__ + prepare_for_readiness.
        # Declared here for pyright strict per-class attribute analysis; no runtime cost
        # (TYPE_CHECKING is False at runtime).
        _address_book_service: Any
        _kb_root: Path | None
        _memory_service: Any
        _state_service: Any

    def install(self, name: str, source: str | None = None) -> dict[str, Any]:
        """Index a knowledge base directory."""
        if self._kb_root is None:
            raise RuntimeError(f"{PLUGIN_NAME}: knowledge_base_root not configured")
        return install_kb(
            name, source, self._kb_root,
            self._state_service, self._memory_service, self._address_book_service,
        )

    def ingest(self, name: str) -> dict[str, Any]:
        """Content-hash-gated idempotent (re)ingest of one KB, or all when name='all'."""
        if self._kb_root is None:
            raise RuntimeError(f"{PLUGIN_NAME}: knowledge_base_root not configured")
        return ingest_kb(
            name, self._kb_root,
            self._state_service, self._memory_service, self._address_book_service,
        )

    def uninstall(self, name: str, remove_files: bool = False) -> dict[str, Any]:
        """Hard-delete all chunks and remove install record."""
        return uninstall_kb(
            name, remove_files, self._state_service, self._memory_service,
        )

    def update(self, name: str) -> dict[str, Any]:
        """Pull upstream changes (git) or reindex changed files (local)."""
        return update_kb(
            name, self._state_service, self._memory_service, self._address_book_service,
        )

    def list_installed(self, active_only: bool = False) -> dict[str, Any]:
        """List indexed knowledge bases with metadata."""
        return list_installed_kbs(active_only, self._state_service)

    def activate(self, name: str) -> dict[str, Any]:
        """Activate a knowledge base for search inclusion."""
        return activate_kb(name, self._state_service)

    def deactivate(self, name: str) -> dict[str, Any]:
        """Deactivate a knowledge base from search results."""
        return deactivate_kb(name, self._state_service)
