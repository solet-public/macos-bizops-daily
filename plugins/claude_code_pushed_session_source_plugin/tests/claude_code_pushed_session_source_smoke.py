#!/usr/bin/env python3
"""Smoke test for claude_code_pushed_session_source_plugin (no pytest).

Coverage:

* Descriptor reports source_kind='claude_code_pushed', vendor='claude_code',
  supported_modes=('pushed',).
* parse_chunk with a single Claude Code JSONL line yields the same raw
  events as the filesystem sibling (parser is shared via the platform-owned
  ``vendor.claude_code`` module; the plugin is a thin envelope).
* parse_chunk with a multi-line chunk yields flattened events in order.
* parse_chunk drops session-config noise lines (permission-mode,
  file-history-snapshot) without affecting subsequent lines.
* parse_chunk surfaces vendor parser ValueErrors verbatim — the importer
  marks the batch FAILED with ``error_kind='value_error'``.
* normalize round-trip preserves vendor_event_id / vendor_parent_event_id
  discipline (parent-child UUID linkage acceptance §17.3).
* TOOL_CALL normalize carries ``tool_name`` in content_json so the
  importer's ``_extract_tool_name`` projection succeeds.

Run:
    .venv/bin/python3 plugins/claude_code_pushed_session_source_plugin/tests/claude_code_pushed_session_source_smoke.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT / "ananta" / "src"))
sys.path.insert(
    0,
    str(REPO_ROOT / "plugins" / "claude_code_pushed_session_source_plugin" / "src"),
)

from ananta.llm.session_ledger.types import (  # noqa: E402
    EventType,
    IngestMode,
    IngestSourceKind,
    MessageRole,
    SourceVendor,
)
from claude_code_pushed_session_source_plugin.plugin import (  # noqa: E402
    ClaudeCodePushedSessionSourcePlugin,
)

_passed = 0
_failed: list[str] = []


def _check(condition: object, label: str) -> None:
    global _passed
    if condition:
        _passed += 1
        print(f"  PASS  {label}")
    else:
        _failed.append(label)
        print(f"  FAIL  {label}")


_SID = "01234567-89ab-cdef-0123-456789abcdef"
_USER_UUID = "u" + "1" * 35
_ASSISTANT_UUID = "a" + "2" * 35
_TOOL_USE_ID = "toolu_pushed" + "x" * 14


def _user_text_line() -> str:
    return json.dumps(
        {
            "parentUuid": None,
            "type": "user",
            "message": {"role": "user", "content": "hi"},
            "uuid": _USER_UUID,
            "timestamp": "2026-05-30T12:00:00.000Z",
            "sessionId": _SID,
        }
    )


def _assistant_tool_use_line() -> str:
    return json.dumps(
        {
            "parentUuid": _USER_UUID,
            "type": "assistant",
            "message": {
                "role": "assistant",
                "content": [
                    {"type": "text", "text": "let me try"},
                    {
                        "type": "tool_use",
                        "id": _TOOL_USE_ID,
                        "name": "Read",
                        "input": {"file_path": "/tmp/x"},
                    },
                ],
            },
            "uuid": _ASSISTANT_UUID,
            "timestamp": "2026-05-30T12:00:01.000Z",
            "sessionId": _SID,
        }
    )


def _tool_result_line() -> str:
    return json.dumps(
        {
            "parentUuid": _ASSISTANT_UUID,
            "type": "user",
            "message": {
                "role": "user",
                "content": [
                    {
                        "tool_use_id": _TOOL_USE_ID,
                        "type": "tool_result",
                        "content": [{"type": "text", "text": "file body"}],
                    }
                ],
            },
            "uuid": "u" + "3" * 35,
            "timestamp": "2026-05-30T12:00:02.000Z",
            "sessionId": _SID,
        }
    )


def _permission_mode_line() -> str:
    return json.dumps(
        {
            "type": "permission-mode",
            "permissionMode": "bypassPermissions",
            "sessionId": _SID,
        }
    )


def _make_plugin() -> ClaudeCodePushedSessionSourcePlugin:
    plugin = ClaudeCodePushedSessionSourcePlugin()
    plugin.prepare_for_readiness()
    return plugin


# ---------------------------------------------------------------------------
# Descriptor
# ---------------------------------------------------------------------------


def test_descriptor() -> None:
    plugin = _make_plugin()
    desc = plugin.describe()
    _check(
        desc.source_kind is IngestSourceKind.CLAUDE_CODE_PUSHED,
        "descriptor source_kind = claude_code_pushed",
    )
    _check(desc.vendor is SourceVendor.CLAUDE_CODE, "descriptor vendor = claude_code")
    _check(
        desc.supported_modes == (IngestMode.PUSHED,),
        "descriptor supported_modes = (pushed,)",
    )


# ---------------------------------------------------------------------------
# parse_chunk
# ---------------------------------------------------------------------------


def test_parse_chunk_single_user_text() -> None:
    plugin = _make_plugin()
    events = list(plugin.parse_chunk(_user_text_line()))
    _check(len(events) == 1, "single user text line → 1 raw event")
    _check(events[0].payload.get("kind") == "message", "kind = message")
    _check(events[0].vendor_event_id == _USER_UUID, "vendor_event_id = line.uuid")


def test_parse_chunk_multi_line_flattens() -> None:
    plugin = _make_plugin()
    chunk = "\n".join(
        [
            _user_text_line(),
            _assistant_tool_use_line(),
            _tool_result_line(),
        ]
    )
    events = list(plugin.parse_chunk(chunk))
    # 1 user message + (1 assistant text + 1 tool_call) + 1 tool_result = 4
    _check(len(events) == 4, f"multi-line chunk → 4 events (got {len(events)})")
    kinds = [e.payload.get("kind") for e in events]
    _check(
        kinds == ["message", "message", "tool_call", "tool_result"],
        f"event order preserved across lines (got {kinds})",
    )


def test_parse_chunk_skips_session_config_lines() -> None:
    plugin = _make_plugin()
    chunk = "\n".join([_permission_mode_line(), _user_text_line()])
    events = list(plugin.parse_chunk(chunk))
    _check(
        len(events) == 1 and events[0].payload.get("kind") == "message",
        "permission-mode noise dropped; surrounding message survives",
    )


def test_parse_chunk_raises_on_malformed_line() -> None:
    plugin = _make_plugin()
    try:
        list(plugin.parse_chunk('{"not": "closed"'))
    except ValueError as exc:
        _check(
            "valid JSON" in str(exc),
            "malformed line raises ValueError (importer marks batch FAILED)",
        )
        return
    _check(False, "expected ValueError on malformed chunk line")


# ---------------------------------------------------------------------------
# normalize() — repository shape + parent-child linkage
# ---------------------------------------------------------------------------


def test_normalize_user_message() -> None:
    plugin = _make_plugin()
    raw = list(plugin.parse_chunk(_user_text_line()))[0]
    n = plugin.normalize(raw)
    _check(n.event_type is EventType.MESSAGE, "user line → MESSAGE")
    _check(n.role is MessageRole.USER, "role = USER")
    _check(n.content_text == "hi", "text body preserved")


def test_normalize_tool_call_emits_content_json_with_tool_name() -> None:
    plugin = _make_plugin()
    events = list(plugin.parse_chunk(_assistant_tool_use_line()))
    tool_call_raw = next(e for e in events if e.payload.get("kind") == "tool_call")
    n = plugin.normalize(tool_call_raw)
    _check(n.event_type is EventType.TOOL_CALL, "tool_use → TOOL_CALL")
    _check(n.content_text is None, "TOOL_CALL has no content_text")
    _check(
        isinstance(n.content_json, dict) and n.content_json.get("tool_name") == "Read",
        "TOOL_CALL.content_json carries tool_name (importer projection key)",
    )
    _check(
        n.vendor_event_id == _TOOL_USE_ID,
        "TOOL_CALL.vendor_event_id = toolu_... (load-bearing for resolution)",
    )


def test_normalize_tool_result_preserves_parent_link() -> None:
    plugin = _make_plugin()
    raw = list(plugin.parse_chunk(_tool_result_line()))[0]
    n = plugin.normalize(raw)
    _check(n.event_type is EventType.TOOL_RESULT, "tool_result → TOOL_RESULT")
    _check(
        n.vendor_parent_event_id == _TOOL_USE_ID,
        "TOOL_RESULT.vendor_parent_event_id = tool_use_id "
        "(parent-child linkage; spec §17.3 acceptance)",
    )
    _check(n.role is MessageRole.TOOL, "TOOL_RESULT role = TOOL")


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------


def main() -> int:
    print("=== claude_code_pushed_session_source_smoke ===")
    test_descriptor()
    test_parse_chunk_single_user_text()
    test_parse_chunk_multi_line_flattens()
    test_parse_chunk_skips_session_config_lines()
    test_parse_chunk_raises_on_malformed_line()
    test_normalize_user_message()
    test_normalize_tool_call_emits_content_json_with_tool_name()
    test_normalize_tool_result_preserves_parent_link()
    print(f"\n{_passed} passed, {len(_failed)} failed")
    if _failed:
        for label in _failed:
            print(f"  FAILED: {label}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
