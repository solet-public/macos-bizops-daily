#!/usr/bin/env python3
"""Plugin-owned normalization coverage for Claude Code usage-capture events."""

from __future__ import annotations

import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT / "ananta" / "src"))
sys.path.insert(
    0, str(REPO_ROOT / "plugins" / "claude_code_filesystem_session_source_plugin" / "src")
)

from ananta.llm.session_ledger.types import EventType  # noqa: E402
from ananta.llm.session_ledger.vendor import claude_code as vendor  # noqa: E402
from claude_code_filesystem_session_source_plugin.plugin import (  # noqa: E402
    ClaudeCodeFilesystemSessionSourcePlugin,
)

_USAGE = {
    "input_tokens": 12,
    "output_tokens": 34,
    "cache_creation_input_tokens": 5,
    "cache_read_input_tokens": 6,
}
_FAILED: list[str] = []


def _check(condition: object, label: str) -> None:
    if condition:
        print(f"  PASS  {label}")
    else:
        _FAILED.append(label)
        print(f"  FAIL  {label}")


def _line(content: object, usage: dict[str, object] | None) -> str:
    message: dict[str, object] = {"role": "assistant", "content": content}
    if usage is not None:
        message["usage"] = usage
    return json.dumps(
        {
            "type": "assistant",
            "uuid": "u-usage-1",
            "sessionId": "s-usage-1",
            "timestamp": "2026-08-05T00:00:00.000Z",
            "message": message,
        }
    )


def test_normalize_usage() -> None:
    plugin = ClaudeCodeFilesystemSessionSourcePlugin()
    raw = vendor.parse_line(_line([{"type": "text", "text": "hi"}], _USAGE))[0]
    normalized = plugin.normalize(raw)
    _check(normalized.event_type is EventType.MESSAGE, "text usage event normalizes to MESSAGE")
    _check(normalized.usage_json == _USAGE, "normalize preserves usage verbatim")


def test_tool_only_usage_carrier() -> None:
    plugin = ClaudeCodeFilesystemSessionSourcePlugin()
    raw_events = vendor.parse_line(
        _line([{"type": "tool_use", "id": "tu-1", "name": "Bash", "input": {}}], _USAGE)
    )
    normalized = [plugin.normalize(event) for event in raw_events]
    messages = [event for event in normalized if event.event_type is EventType.MESSAGE]
    tools = [event for event in normalized if event.event_type is EventType.TOOL_CALL]
    _check(
        len(messages) == 1 and messages[0].usage_json == _USAGE,
        "tool-only turn has one usage MESSAGE carrier",
    )
    _check(len(tools) == 1 and tools[0].usage_json is None, "tool event does not duplicate usage")


def main() -> int:
    print("=== claude_code_usage_normalize_smoke ===")
    test_normalize_usage()
    test_tool_only_usage_carrier()
    if _FAILED:
        print(f"FAILED: {_FAILED}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
