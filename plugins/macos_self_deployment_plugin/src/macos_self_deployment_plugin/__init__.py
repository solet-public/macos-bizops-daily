"""macos_self_deployment_plugin package.

The plugin class is re-exported LAZILY (PEP 562): importing the package no
longer eagerly pulls in ``plugin.py`` and its full graph (swap orchestration,
release manager, schema preflight, …). This keeps the Option-B supervisor —
which runs as ``-m macos_self_deployment_plugin.supervisor`` and is the
process keeping the solet alive — importing only the narrow submodules it needs,
undragged by the plugin-class machinery. Plugin discovery is unaffected: the
entry point references the submodule path
(``macos_self_deployment_plugin.plugin:MacosSelfDeploymentPlugin``), so it
never relied on this re-export.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from macos_self_deployment_plugin.plugin import MacosSelfDeploymentPlugin

__all__ = ["MacosSelfDeploymentPlugin"]


def __getattr__(name: str) -> object:
    if name == "MacosSelfDeploymentPlugin":
        from macos_self_deployment_plugin.plugin import MacosSelfDeploymentPlugin

        return MacosSelfDeploymentPlugin
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
