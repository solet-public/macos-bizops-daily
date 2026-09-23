"""Non-MCP local-invocation CLI for a running solet.

`solet` drives a running solet over its localhost bridge HTTP
surface (`/api/v1/bridge/*`) — the same zero-inference `process_call`
contract the stdio MCP bridge forwards to, but as a one-shot command any
local process can run. This is the invocation path for clients whose
MCP-server allowlist blocks the stdio MCP bridge: the allowlist governs
MCP servers, not a script hitting a localhost port.

Design and rationale: `workbench/2026-07-18_mcp_free_local_invocation_cli_design.md`
+ `workbench/2026-07-21_homunculus_cli_no_mcp_first_interaction_design.md`.
"""

from __future__ import annotations

from typing import Final

__version__: Final[str] = "0.1.0"
