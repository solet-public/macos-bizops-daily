"""Python MCP stdio bridge for the merged agent_messaging_plugin.

Replaces the two Node `server.mjs` bridges with a single Python
subprocess. Launched by MCP clients as `python -m
agent_messaging_plugin.mcp_bridge`. Discovers the solet HTTP API via the
runtime port file (`{solet}.bridge.port`) and forwards every MCP
tool call to the consolidated `/api/v1/bridge/*` route table.
"""

from __future__ import annotations

from .__main__ import main

__all__ = ["main"]
