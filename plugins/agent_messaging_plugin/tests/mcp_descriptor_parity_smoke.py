#!/usr/bin/env python3
"""Smoke: stdio and Streamable MCP descriptors stay aligned.

Schemas and tool names are exact-parity. Descriptions are exact-parity except
for process_call/process_result, where hosted Streamable clients must poll
process_result and stdio clients receive bridge_delivery_result notifications.

Run:
    SOLET_NAME=<name>-test .venv/bin/python3 \
        plugins/agent_messaging_plugin/tests/mcp_descriptor_parity_smoke.py
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT / "ananta" / "src"))
sys.path.insert(0, str(REPO_ROOT / "plugins" / "agent_messaging_plugin" / "src"))

from agent_messaging_plugin.mcp_bridge.__main__ import (  # noqa: E402
    _TOOL_DISPATCH as STDIO_DISPATCH,
)
from agent_messaging_plugin.mcp_bridge.__main__ import (  # noqa: E402
    TOOLS as STDIO_TOOLS,
)
from agent_messaging_plugin.mcp_streamable.dispatch import (  # noqa: E402
    _TOOL_HANDLERS as STREAMABLE_HANDLERS,
)
from agent_messaging_plugin.mcp_streamable.tools import (  # noqa: E402
    TOOLS as STREAMABLE_TOOLS,
)

_passed = 0
_failed: list[str] = []

_DESCRIPTION_EXCEPTIONS: frozenset[str] = frozenset(
    {"process_call", "process_result"},
)


def _check(condition: object, label: str) -> None:
    global _passed
    if condition:
        _passed += 1
        print(f"  PASS  {label}")
        return
    _failed.append(label)
    print(f"  FAIL  {label}")


def _stdio_descriptor_map() -> dict[str, dict[str, object]]:
    return {
        tool.name: {
            "description": tool.description,
            "inputSchema": tool.inputSchema,
        }
        for tool in STDIO_TOOLS
    }


def _streamable_descriptor_map() -> dict[str, dict[str, object]]:
    descriptors: dict[str, dict[str, object]] = {}
    for tool in STREAMABLE_TOOLS:
        name = str(tool.get("name") or "")
        descriptors[name] = {
            "description": tool.get("description"),
            "inputSchema": tool.get("inputSchema"),
        }
    return descriptors


def _names(mapping: dict[str, Any]) -> set[str]:
    return {name for name in mapping if name}


def main() -> int:
    print("=== MCP descriptor parity smoke ===")
    stdio = _stdio_descriptor_map()
    streamable = _streamable_descriptor_map()
    stdio_names = _names(stdio)
    streamable_names = _names(streamable)

    _check(stdio_names == streamable_names, "stdio and streamable descriptor sets match")
    _check(
        stdio_names == set(STDIO_DISPATCH),
        "stdio descriptor set matches _TOOL_DISPATCH",
    )
    _check(
        streamable_names == set(STREAMABLE_HANDLERS),
        "streamable descriptor set matches _TOOL_HANDLERS",
    )

    for name in sorted(stdio_names & streamable_names):
        if name in _DESCRIPTION_EXCEPTIONS:
            _check(
                stdio[name]["description"] != streamable[name]["description"],
                f"{name}: intentional transport-specific description",
            )
        else:
            _check(
                stdio[name]["description"] == streamable[name]["description"],
                f"{name}: description parity",
            )
        _check(
            stdio[name]["inputSchema"] == streamable[name]["inputSchema"],
            f"{name}: inputSchema parity",
        )

    print(f"\n{_passed} passed, {len(_failed)} failed")
    if _failed:
        for label in _failed:
            print(f"  FAILED: {label}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
