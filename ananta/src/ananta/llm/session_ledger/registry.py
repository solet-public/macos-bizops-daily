"""Source-plugin registry for the LLM session ledger.

Spec §12.1, §12.3. Locates loaded source plugins through the centralized
``ananta.core.plugins.capabilities.collect_llm_session_sources`` helper —
never via direct ``isinstance(plugin, LLMSessionSourceInterface)`` outside
``capabilities.py`` (that module's docstring forbids it).
"""

from __future__ import annotations

import logging
from collections.abc import Mapping

from ananta.core.plugins.capabilities import collect_llm_session_sources
from ananta.interfaces.llm_session_source_interface import (
    LLMSessionSourceInterface,
    PullingSourceMixin,
    PushedSourceMixin,
)
from ananta.llm.session_ledger.types import (
    IngestMode,
    IngestSourceKind,
    SessionSourceDescriptor,
)

logger = logging.getLogger(__name__)


class SessionSourceRegistryError(RuntimeError):
    """Raised when source-plugin discovery violates an invariant.

    Examples: two plugins claim the same ``source_kind``; a plugin whose
    descriptor advertises ``pulling`` mode does not inherit
    ``PullingSourceMixin``.
    """


class SessionSourceRegistry:
    """Snapshot of loaded source plugins, keyed by ``source_kind``.

    Constructed once during ``_init_session_ledger_service`` from the live
    ``plugin_manager.plugins`` mapping. v1 does NOT support late-loaded
    source plugins; rebuilding is out of scope.
    """

    __slots__ = ("_by_kind", "_by_plugin_name")

    def __init__(self, plugins: Mapping[str, object]) -> None:
        discovered = collect_llm_session_sources(plugins)
        by_kind: dict[IngestSourceKind, LLMSessionSourceInterface] = {}
        by_plugin_name: dict[str, LLMSessionSourceInterface] = {}
        for plugin_name, plugin in discovered.items():
            descriptor = plugin.describe()
            self._validate_descriptor_against_plugin(plugin_name, plugin, descriptor)
            if descriptor.source_kind in by_kind:
                existing_plugin = by_kind[descriptor.source_kind]
                existing_name = next(
                    name for name, p in discovered.items() if p is existing_plugin
                )
                raise SessionSourceRegistryError(
                    f"source_kind {descriptor.source_kind.value!r} claimed by "
                    f"both {existing_name!r} and {plugin_name!r}",
                )
            by_kind[descriptor.source_kind] = plugin
            by_plugin_name[plugin_name] = plugin
            logger.debug(
                "registered llm session source plugin=%s source_kind=%s modes=%s",
                plugin_name,
                descriptor.source_kind.value,
                tuple(m.value for m in descriptor.supported_modes),
            )
        self._by_kind = by_kind
        self._by_plugin_name = by_plugin_name

    @staticmethod
    def _validate_descriptor_against_plugin(
        plugin_name: str,
        plugin: LLMSessionSourceInterface,
        descriptor: SessionSourceDescriptor,
    ) -> None:
        modes = set(descriptor.supported_modes)
        if not modes:
            raise SessionSourceRegistryError(
                f"plugin {plugin_name!r} declares no supported modes",
            )
        if IngestMode.PULLING in modes and not isinstance(plugin, PullingSourceMixin):
            raise SessionSourceRegistryError(
                f"plugin {plugin_name!r} advertises pulling mode but does not "
                "implement PullingSourceMixin",
            )
        if IngestMode.PUSHED in modes and not isinstance(plugin, PushedSourceMixin):
            raise SessionSourceRegistryError(
                f"plugin {plugin_name!r} advertises pushed mode but does not "
                "implement PushedSourceMixin",
            )

    def list_sources(self) -> list[SessionSourceDescriptor]:
        return [plugin.describe() for plugin in self._by_kind.values()]

    def get_by_kind(self, source_kind: IngestSourceKind) -> LLMSessionSourceInterface | None:
        return self._by_kind.get(source_kind)

    def require_by_kind(
        self,
        source_kind: IngestSourceKind,
    ) -> LLMSessionSourceInterface:
        plugin = self._by_kind.get(source_kind)
        if plugin is None:
            raise SessionSourceRegistryError(
                f"no source plugin registered for kind {source_kind.value!r}",
            )
        return plugin


__all__ = [
    "SessionSourceRegistry",
    "SessionSourceRegistryError",
]
