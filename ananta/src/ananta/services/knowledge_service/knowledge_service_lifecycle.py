"""Knowledge-service wrapper sub-mixin for KB lifecycle delegates (W5.S).

Six 1-3 line passthroughs to the bound plugin satisfying the W5.R-decomposed
:class:`KnowledgeLifecycleInterface`: install, uninstall, update, list_installed,
activate, and deactivate. Each delegate is lifted byte-for-byte from the
W5.S-pre-decomposition ``KnowledgeService.__init__.py`` (206 LOC, 19 delegate
methods). Per the W5.S §4.1 invariant, no behavior changes — only relocation.
"""

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from ananta.interfaces.knowledge_service_interface_lifecycle import (
        KnowledgeLifecycleInterface,
    )


class KnowledgeLifecycleWrapper:
    """Lifecycle delegate methods. Inherited by :class:`KnowledgeService` via MI."""

    if TYPE_CHECKING:
        def _get_backend(self) -> "KnowledgeLifecycleInterface": ...

    def install(self, name: str, source: str | None = None) -> dict[str, Any]:
        return self._get_backend().install(name=name, source=source)

    def ingest(self, name: str) -> dict[str, Any]:
        return self._get_backend().ingest(name=name)

    def uninstall(self, name: str, remove_files: bool = False) -> dict[str, Any]:
        return self._get_backend().uninstall(name=name, remove_files=remove_files)

    def update(self, name: str) -> dict[str, Any]:
        return self._get_backend().update(name=name)

    def list_installed(self, active_only: bool = False) -> dict[str, Any]:
        return self._get_backend().list_installed(active_only=active_only)

    def activate(self, name: str) -> dict[str, Any]:
        return self._get_backend().activate(name=name)

    def deactivate(self, name: str) -> dict[str, Any]:
        return self._get_backend().deactivate(name=name)
