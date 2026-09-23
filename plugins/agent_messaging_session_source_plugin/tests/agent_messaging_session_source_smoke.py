#!/usr/bin/env python3
"""Smoke test for the agent_messaging_session_source_plugin (no pytest).

Pure-logic coverage. The DB-backed discover_sessions / read_events paths moved
to live smokes after the SQL-lockdown rewire (D1/GAP-5) — they now go through
the OWNING agent_messaging list_threads / read_thread_messages verbs, not raw
SQL, so they are exercised end-to-end against real Postgres in
``discover_sessions_list_threads_live_smoke.py`` and
``read_events_read_thread_messages_live_smoke.py``.

Coverage here:

* Descriptor reports source_kind='agent_messaging', vendor='agent_messaging',
  supported_modes=('pulling',).
* normalize maps:
    - kind='message' + role='originator' → MESSAGE + MessageRole.USER
    - kind='message' + role='agent'      → MESSAGE + MessageRole.ASSISTANT
    - kind='error'                       → SYSTEM + structured error
    - kind='result'                      → TOOL_RESULT
  Unrecognized kind raises ValueError (no fallback coercion).
* event_read_cursor returns {"cursor_high_water": <last cursor>}.
* session_discovery_cursor returns the opaque (created_at, id) thread_cursor.

Run:
    .venv/bin/python3 plugins/agent_messaging_session_source_plugin/tests/agent_messaging_session_source_smoke.py
"""

from __future__ import annotations

import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT / "ananta" / "src"))
sys.path.insert(0, str(REPO_ROOT / "plugins" / "agent_messaging_session_source_plugin" / "src"))

from agent_messaging_session_source_plugin.plugin import (  # noqa: E402
    AgentMessagingSessionSourcePlugin,
)
from ananta.llm.agent_messaging.thread_cursor import encode_thread_cursor  # noqa: E402
from ananta.llm.session_ledger.types import (  # noqa: E402
    EventType,
    ExternalSessionRef,
    IngestMode,
    IngestSourceKind,
    MessageRole,
    RawSessionEvent,
    SourceVendor,
)

_passed = 0
_failed: list[str] = []


# agent_messaging is a state-backed pulling source: its canonical root_uri is
# the "local:agent_messaging" sentinel, accepted by the P1.1.E contract but
# unused (the plugin reads from state_service, not a filesystem path).
_ROOT_URI = "local:agent_messaging"


def _check(condition: object, label: str) -> None:
    global _passed
    if condition:
        _passed += 1
        print(f"  PASS  {label}")
    else:
        _failed.append(label)
        print(f"  FAIL  {label}")


# ─── Descriptor ─────────────────────────────────────────────────────────────


def test_descriptor() -> None:
    plugin = AgentMessagingSessionSourcePlugin()
    desc = plugin.describe()
    _check(
        desc.source_kind is IngestSourceKind.AGENT_MESSAGING,
        "descriptor source_kind = agent_messaging",
    )
    _check(desc.vendor is SourceVendor.AGENT_MESSAGING, "descriptor vendor = agent_messaging")
    _check(
        desc.supported_modes == (IngestMode.PULLING,),
        "descriptor supported_modes = (pulling,)",
    )


# ─── normalize ──────────────────────────────────────────────────────────────


def _raw(kind: str, role: str, content: list[dict[str, Any]]) -> RawSessionEvent:
    return RawSessionEvent(
        external_session_id="agt_aaa",
        payload={"kind": kind, "role": role, "content": content, "cursor": 1},
        event_at=datetime(2026, 5, 24, 12, 0, tzinfo=UTC),
        vendor_event_id="agm_x",
        vendor_parent_event_id=None,
    )


def test_normalize_message_originator() -> None:
    plugin = AgentMessagingSessionSourcePlugin()
    raw = _raw("message", "originator", [{"type": "text", "text": "hi"}])
    n = plugin.normalize(raw)
    _check(n.event_type is EventType.MESSAGE, "normalize → MESSAGE")
    _check(n.role is MessageRole.USER, "originator → MessageRole.USER")
    _check(n.content_text == "hi", "text content preserved")


def test_normalize_message_agent() -> None:
    plugin = AgentMessagingSessionSourcePlugin()
    raw = _raw("message", "agent", [{"type": "text", "text": "ok"}])
    n = plugin.normalize(raw)
    _check(n.role is MessageRole.ASSISTANT, "agent → MessageRole.ASSISTANT")


def test_normalize_error_to_system() -> None:
    plugin = AgentMessagingSessionSourcePlugin()
    payload = _raw("error", "system", [])
    # Stuff a structured error payload in
    payload.payload["error"] = {"code": "x", "detail": "y"}
    n = plugin.normalize(payload)
    _check(n.event_type is EventType.SYSTEM, "kind=error → SYSTEM event")
    _check(n.content_json == {"code": "x", "detail": "y"}, "error dict preserved as content_json")


def test_normalize_result_to_tool_result() -> None:
    plugin = AgentMessagingSessionSourcePlugin()
    raw = _raw("result", "agent", [{"type": "text", "text": "tool output"}])
    n = plugin.normalize(raw)
    _check(n.event_type is EventType.TOOL_RESULT, "kind=result → TOOL_RESULT")
    _check(n.role is MessageRole.TOOL, "TOOL_RESULT carries MessageRole.TOOL")


def test_normalize_unrecognized_kind_raises() -> None:
    plugin = AgentMessagingSessionSourcePlugin()
    raw = _raw("unknown_kind", "agent", [{"type": "text", "text": "x"}])
    try:
        plugin.normalize(raw)
    except ValueError as e:
        _check("unknown_kind" in str(e), "unrecognized kind raises ValueError")
        return
    _check(False, "expected ValueError on unrecognized kind")


# ─── Cursor producers ───────────────────────────────────────────────────────


def test_event_read_cursor_returns_high_water() -> None:
    plugin = AgentMessagingSessionSourcePlugin()
    ref = ExternalSessionRef(
        external_session_id="agt_x",
        vendor_session_label=None,
        project_path=None,
        first_seen_at=datetime(2026, 5, 24, 10, 0, tzinfo=UTC),
    )
    raw = _raw("message", "originator", [{"type": "text", "text": "x"}])
    raw.payload["cursor"] = 42
    cursor = plugin.event_read_cursor(_ROOT_URI, ref, raw)
    _check(cursor == {"cursor_high_water": 42}, "event_read_cursor returns int cursor high-water")


def test_session_discovery_cursor_returns_thread_cursor() -> None:
    plugin = AgentMessagingSessionSourcePlugin()
    ref = ExternalSessionRef(
        external_session_id="agt_x",
        vendor_session_label=None,
        project_path=None,
        first_seen_at=datetime(2026, 5, 24, 11, 0, tzinfo=UTC),
    )
    cursor = plugin.session_discovery_cursor(_ROOT_URI, ref)
    # Post-migration (D1/GAP-5): the opaque (created_at, id) token list_threads
    # paginates by — NOT the pre-migration created_at-only ISO high-water.
    expected = encode_thread_cursor(
        created_at_iso="2026-05-24T11:00:00+00:00", row_id="agt_x",
    )
    _check(
        cursor == {"thread_cursor": expected},
        "session_discovery_cursor returns the opaque (created_at, id) thread_cursor",
    )


# ─── Shape contract sanity check on normalized output ───────────────────────


def test_normalized_satisfies_repository_shape() -> None:
    """The shape NormalizedSessionEvent produced here must pass
    SessionLedgerRepository._validate_event_shape (MESSAGE needs role + content)."""
    plugin = AgentMessagingSessionSourcePlugin()
    raw = _raw("message", "originator", [{"type": "text", "text": "hello"}])
    n = plugin.normalize(raw)
    _check(n.role is not None and n.content_text is not None, "MESSAGE has role + content_text")


def main() -> int:
    print("=== agent_messaging_session_source_smoke ===")
    test_descriptor()
    test_normalize_message_originator()
    test_normalize_message_agent()
    test_normalize_error_to_system()
    test_normalize_result_to_tool_result()
    test_normalize_unrecognized_kind_raises()
    test_event_read_cursor_returns_high_water()
    test_session_discovery_cursor_returns_thread_cursor()
    test_normalized_satisfies_repository_shape()
    print(f"\n{_passed} passed, {len(_failed)} failed")
    if _failed:
        for label in _failed:
            print(f"  FAILED: {label}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
