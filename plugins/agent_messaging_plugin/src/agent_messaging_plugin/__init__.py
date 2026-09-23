"""Agent messaging plugin — schema + peer messaging + service-binding host.

The server plugin is re-exported LAZILY (PEP 562): importing the bare package
must not pull in `.plugin`, which resolves a scoped vault name (and so requires
``SOLET_NAME``) at import time. The platform loader is unaffected — the
``ananta.plugins`` entry point is module-qualified
(``agent_messaging_plugin.plugin:AgentMessagingPlugin``) — but the
``solet`` console script lives under this package (``local_cli/``) and
must work bare on the operator's PATH with no ambient env (the no-MCP-first
install-location-identity contract, design 2026-07-21 §3.A).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .plugin import AgentMessagingPlugin

__all__ = ["AgentMessagingPlugin"]


def __getattr__(name: str) -> object:
    if name == "AgentMessagingPlugin":
        from .plugin import AgentMessagingPlugin

        return AgentMessagingPlugin
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
