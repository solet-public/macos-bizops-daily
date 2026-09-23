#!/usr/bin/env python3
"""Offline smoke for the Claude Code Cloud session-source plugin's pure logic.

Run:

    .venv/bin/python3 plugins/claude_code_cloud_session_source_plugin/tests/normalize_smoke.py

Covers the plugin's chunk-decode + envelope-adapt + normalize surface — the
parts that run with no network, no keychain, and no live ledger — leaving the
walker's actual HTTP fetch (``walker.py``) untested here, as it has no
offline-safe subject. Everything is driven through the plugin's own public
surface (``parse_chunk``, ``normalize``, ``describe``) — no module-private
helper is imported directly, so every check here is also a check that the
public contract itself behaves correctly. Exercises:

* The ``cse_``/``session_`` prefix collapse (module comment at
  ``_CLOUD_SESSION_ID_PREFIXES`` — the two vendor-side ID namespaces for one
  conversation) as observed on ``parse_chunk``'s output
  ``external_session_id``.
* ``parse_chunk`` end-to-end — decoding a cloud envelope, skipping plumbing
  event types, and adapting a conversational event into the local-JSONL
  shape before it round-trips through the real
  ``vendor.claude_code.parse_line_data``.
* ``normalize()`` for each raw payload kind (message / tool_call /
  tool_result / system), including the unknown-kind fail-closed path.
* ``parse_chunk``'s malformed-input rejections (non-JSON, non-dict, missing
  required envelope keys) — the same fail-closed posture as every other
  ingest source in this family.
"""

from __future__ import annotations

import json
import sys
from datetime import UTC, datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT / "ananta" / "src"))
sys.path.insert(
    0, str(REPO_ROOT / "plugins" / "claude_code_cloud_session_source_plugin" / "src"),
)

from ananta.llm.session_ledger.types import (  # noqa: E402
    EventType,
    IngestMode,
    IngestSourceKind,
    MessageRole,
    RawSessionEvent,
    SourceVendor,
)
from claude_code_cloud_session_source_plugin.plugin import (  # noqa: E402
    ClaudeCodeCloudSessionSourcePlugin,
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


# ─── parse_chunk: cse_/session_ id-prefix collapse ───────────────────────────


def _minimal_user_event() -> dict[str, object]:
    return {
        "event_type": "user",
        "created_at": "2026-09-22T00:00:00Z",
        "payload": {
            "parentUuid": None,
            "type": "user",
            "message": {"role": "user", "content": "hi"},
            "uuid": "evt-1",
        },
    }


def test_parse_chunk_strips_cse_prefix_from_external_session_id() -> None:
    plugin = ClaudeCodeCloudSessionSourcePlugin()
    raw_events = list(plugin.parse_chunk(_chunk("cse_abc123", [_minimal_user_event()])))
    _check(
        len(raw_events) == 1 and raw_events[0].external_session_id == "abc123",
        f"cse_ prefix stripped (got {(raw_events[0].external_session_id if raw_events else None)!r})",
    )


def test_parse_chunk_strips_session_prefix_from_external_session_id() -> None:
    plugin = ClaudeCodeCloudSessionSourcePlugin()
    raw_events = list(plugin.parse_chunk(_chunk("session_abc123", [_minimal_user_event()])))
    _check(
        len(raw_events) == 1 and raw_events[0].external_session_id == "abc123",
        f"session_ prefix stripped (got {(raw_events[0].external_session_id if raw_events else None)!r})",
    )


def test_parse_chunk_passes_through_bare_id_unchanged() -> None:
    plugin = ClaudeCodeCloudSessionSourcePlugin()
    raw_events = list(plugin.parse_chunk(_chunk("abc123", [_minimal_user_event()])))
    _check(
        len(raw_events) == 1 and raw_events[0].external_session_id == "abc123",
        "bare UUID-shape id passed through unchanged (no prefix to strip)",
    )


def test_parse_chunk_collapses_both_id_namespaces_to_the_same_key() -> None:
    plugin = ClaudeCodeCloudSessionSourcePlugin()
    a = list(plugin.parse_chunk(_chunk("cse_shared-id", [_minimal_user_event()])))
    b = list(plugin.parse_chunk(_chunk("session_shared-id", [_minimal_user_event()])))
    _check(
        len(a) == 1 and len(b) == 1 and a[0].external_session_id == b[0].external_session_id == "shared-id",
        "cse_X and session_X collapse to the same bare suffix",
    )


# ─── parse_chunk: malformed-envelope rejections ──────────────────────────────


def test_parse_chunk_rejects_non_json() -> None:
    plugin = ClaudeCodeCloudSessionSourcePlugin()
    try:
        list(plugin.parse_chunk("not json at all"))
        _check(False, "non-JSON chunk raises ValueError")
    except ValueError:
        _check(True, "non-JSON chunk raises ValueError")


def test_parse_chunk_rejects_non_dict() -> None:
    plugin = ClaudeCodeCloudSessionSourcePlugin()
    try:
        list(plugin.parse_chunk("[1, 2, 3]"))
        _check(False, "non-dict JSON raises ValueError")
    except ValueError:
        _check(True, "non-dict JSON raises ValueError")


def test_parse_chunk_rejects_missing_external_session_id() -> None:
    plugin = ClaudeCodeCloudSessionSourcePlugin()
    try:
        list(plugin.parse_chunk('{"events": []}'))
        _check(False, "missing external_session_id raises ValueError")
    except ValueError:
        _check(True, "missing external_session_id raises ValueError")


def test_parse_chunk_rejects_missing_events() -> None:
    plugin = ClaudeCodeCloudSessionSourcePlugin()
    try:
        list(plugin.parse_chunk('{"external_session_id": "cse_x"}'))
        _check(False, "missing events raises ValueError")
    except ValueError:
        _check(True, "missing events raises ValueError")


def test_parse_chunk_accepts_well_formed_empty_events() -> None:
    plugin = ClaudeCodeCloudSessionSourcePlugin()
    raw_events = list(plugin.parse_chunk('{"external_session_id": "cse_x", "events": []}'))
    _check(raw_events == [], "well-formed envelope with no events yields zero raw events, no error")


# ─── parse_chunk end-to-end ────────────────────────────────────────────────────


def _chunk(external_session_id: str, events: list[dict[str, object]]) -> str:
    return json.dumps({"external_session_id": external_session_id, "events": events})


def test_parse_chunk_skips_plumbing_events() -> None:
    plugin = ClaudeCodeCloudSessionSourcePlugin()
    chunk = _chunk(
        "cse_s1",
        [
            {"event_type": "result", "payload": {}},
            {"event_type": "control_request", "payload": {}},
            {"event_type": "control_response", "payload": {}},
        ],
    )
    raw_events = list(plugin.parse_chunk(chunk))
    _check(raw_events == [], f"plumbing-only chunk yields zero raw events (got {len(raw_events)})")


def test_parse_chunk_skips_unknown_event_types() -> None:
    plugin = ClaudeCodeCloudSessionSourcePlugin()
    chunk = _chunk("cse_s1", [{"event_type": "some_future_kind", "payload": {}}])
    raw_events = list(plugin.parse_chunk(chunk))
    _check(raw_events == [], "unrecognized event_type is silently skipped, not raised")


def test_parse_chunk_adapts_conversational_event_and_surfaces_session_id() -> None:
    # payload is the FULL local-JSONL line shape (type/message/uuid/...) —
    # parse_chunk's adapter only renames/surfaces sessionId/timestamp/parentUuid
    # before handing off to the real vendor.claude_code.parse_line_data, which
    # expects that shape verbatim (design v3 §2.3a: reused as-is).
    plugin = ClaudeCodeCloudSessionSourcePlugin()
    chunk = _chunk(
        "cse_s1",
        [
            {
                "event_type": "user",
                "created_at": "2026-09-22T00:00:00Z",
                "payload": {
                    "parentUuid": None,
                    "type": "user",
                    "message": {"role": "user", "content": "hello from the cloud"},
                    "uuid": "evt-1",
                },
            },
        ],
    )
    raw_events = list(plugin.parse_chunk(chunk))
    _check(len(raw_events) == 1, f"one conversational event yields one raw event (got {len(raw_events)})")
    if raw_events:
        _check(
            raw_events[0].external_session_id == "s1",
            f"external_session_id surfaced with the cse_ prefix stripped (got {raw_events[0].external_session_id!r})",
        )


# ─── normalize() per payload kind ────────────────────────────────────────────


def _raw(payload: dict[str, object]) -> RawSessionEvent:
    return RawSessionEvent(
        external_session_id="s1",
        payload=payload,
        event_at=datetime(2026, 9, 22, tzinfo=UTC),
        vendor_event_id="evt-1",
        vendor_parent_event_id=None,
    )


def test_normalize_message() -> None:
    plugin = ClaudeCodeCloudSessionSourcePlugin()
    normalized = plugin.normalize(
        _raw({"kind": "message", "role": "assistant", "text": "hi there"}),
    )
    _check(normalized.event_type == EventType.MESSAGE, "message kind -> EventType.MESSAGE")
    _check(normalized.role == MessageRole.ASSISTANT, "role=assistant -> MessageRole.ASSISTANT")
    _check(normalized.content_text == "hi there", "message text carried through")


def test_normalize_tool_call() -> None:
    plugin = ClaudeCodeCloudSessionSourcePlugin()
    normalized = plugin.normalize(
        _raw(
            {
                "kind": "tool_call",
                "tool_name": "Read",
                "tool_use_id": "tu-1",
                "tool_input": {"path": "/tmp/x"},
            },
        ),
    )
    _check(normalized.event_type == EventType.TOOL_CALL, "tool_call kind -> EventType.TOOL_CALL")
    _check(
        normalized.content_json == {"tool_name": "Read", "tool_use_id": "tu-1", "input": {"path": "/tmp/x"}},
        f"tool_call content_json carries name/id/input (got {normalized.content_json!r})",
    )


def test_normalize_tool_result() -> None:
    plugin = ClaudeCodeCloudSessionSourcePlugin()
    normalized = plugin.normalize(_raw({"kind": "tool_result", "text": "output here"}))
    _check(normalized.event_type == EventType.TOOL_RESULT, "tool_result kind -> EventType.TOOL_RESULT")
    _check(normalized.role == MessageRole.TOOL, "tool_result role -> MessageRole.TOOL")


def test_normalize_system() -> None:
    plugin = ClaudeCodeCloudSessionSourcePlugin()
    normalized = plugin.normalize(
        _raw({"kind": "system", "text": "session started", "subtype": "init"}),
    )
    _check(normalized.event_type == EventType.SYSTEM, "system kind -> EventType.SYSTEM")
    _check(
        normalized.content_json == {"subtype": "init"},
        f"system subtype carried in content_json (got {normalized.content_json!r})",
    )


def test_normalize_unknown_kind_raises() -> None:
    plugin = ClaudeCodeCloudSessionSourcePlugin()
    try:
        plugin.normalize(_raw({"kind": "future_kind"}))
        _check(False, "unknown payload kind raises ValueError (fail-closed)")
    except ValueError:
        _check(True, "unknown payload kind raises ValueError (fail-closed)")


def test_describe_declares_pushed_mode_only() -> None:
    plugin = ClaudeCodeCloudSessionSourcePlugin()
    descriptor = plugin.describe()
    _check(
        descriptor.vendor == SourceVendor.CLAUDE_CODE
        and descriptor.source_kind == IngestSourceKind.CLAUDE_CODE_CLOUD
        and descriptor.supported_modes == (IngestMode.PUSHED,),
        f"describe() declares claude_code/claude_code_cloud, pushed-only (got {descriptor!r})",
    )


def main() -> int:
    print("=== normalize_smoke (claude_code_cloud_session_source_plugin) ===")
    test_parse_chunk_strips_cse_prefix_from_external_session_id()
    test_parse_chunk_strips_session_prefix_from_external_session_id()
    test_parse_chunk_passes_through_bare_id_unchanged()
    test_parse_chunk_collapses_both_id_namespaces_to_the_same_key()
    test_parse_chunk_rejects_non_json()
    test_parse_chunk_rejects_non_dict()
    test_parse_chunk_rejects_missing_external_session_id()
    test_parse_chunk_rejects_missing_events()
    test_parse_chunk_accepts_well_formed_empty_events()
    test_parse_chunk_skips_plumbing_events()
    test_parse_chunk_skips_unknown_event_types()
    test_parse_chunk_adapts_conversational_event_and_surfaces_session_id()
    test_normalize_message()
    test_normalize_tool_call()
    test_normalize_tool_result()
    test_normalize_system()
    test_normalize_unknown_kind_raises()
    test_describe_declares_pushed_mode_only()
    print(f"\n{_passed} passed, {len(_failed)} failed")
    if _failed:
        for label in _failed:
            print(f"  FAILED: {label}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
