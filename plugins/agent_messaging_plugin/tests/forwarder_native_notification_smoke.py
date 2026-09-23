#!/usr/bin/env python3
"""Smoke test for Forwarder native notification method selection (no pytest).

Verifies the Phase 1 Codex-native peer wake bridge contract:

1. Codex ``peer_message`` events emit ``notifications/homunculus/peer_message``.
2. Codex native peer notifications preserve full bridge-event metadata.
3. Codex ``post_message`` events use the same solet peer-message method.
4. Non-peer events still use the legacy Claude channel method.
5. Claude sessions still receive the legacy ``notifications/claude/channel``
   method and the canonical 5-key metadata shape.

Run:

    .venv/bin/python3 plugins/agent_messaging_plugin/tests/forwarder_native_notification_smoke.py
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT / "plugins" / "agent_messaging_plugin" / "src"))

from agent_messaging_plugin.mcp_bridge.forwarder import (  # noqa: E402
    CLAUDE_CHANNEL_NOTIFICATION_METHOD,
    SOLET_PEER_MESSAGE_NOTIFICATION_METHOD,
    Forwarder,
)


class CaptureStream:
    """Minimal async send-stream stand-in for Forwarder unit coverage."""

    def __init__(self) -> None:
        self.messages: list[Any] = []

    async def send(self, message: Any) -> None:
        self.messages.append(message)


PEER_EVENT: dict[str, Any] = {
    "cursor": 7,
    "event_type": "peer_message",
    "content": "Please review",
    "flow_id": "flow-top",
    "meta": {
        "thread_id": "agt-1",
        "message_id": "agm-1",
        "thread_cursor": 3,
        "from_agent_id": "claude_code",
        "from_agent_instance_id": "agi-sender",
        "from_session_label": "Coordinator",
        "to_agent_id": "codex",
        "to_agent_instance_id": "agi-codex",
        "important": True,
        "sent_at": "2026-05-30T00:00:00+00:00",
        "extra_nested": {"kept": True},
        "flow_id": "flow-meta",
    },
}


def _fail(label: str, detail: str) -> None:
    print(f"FAIL: {label}: {detail}", file=sys.stderr)
    sys.exit(1)


def _ok(label: str) -> None:
    print(f"  OK: {label}")


def _new_forwarder(agent_id: str) -> Forwarder:
    return Forwarder(
        "http://127.0.0.1:9",
        "smoke",
        agent_id=agent_id,
        agent_instance_id=f"agi-{agent_id}",
        session_label=f"{agent_id} smoke",
        parent_pid=12345,
        provides_inference=agent_id == "claude_code",
    )


async def _emit(agent_id: str, event: dict[str, Any]) -> Any:
    forwarder = _new_forwarder(agent_id)
    stream = CaptureStream()
    forwarder.bind_write_stream(stream)  # type: ignore[arg-type]
    try:
        await forwarder._emit_event(event)  # noqa: SLF001
    finally:
        await forwarder.close()
    if len(stream.messages) != 1:
        _fail("_emit_event", f"expected 1 notification, got {len(stream.messages)}")
    return stream.messages[0].message.root


async def case_codex_peer_message_uses_solet_method() -> None:
    notification = await _emit("codex", PEER_EVENT)
    if notification.method != SOLET_PEER_MESSAGE_NOTIFICATION_METHOD:
        _fail(
            "codex peer_message method",
            f"expected {SOLET_PEER_MESSAGE_NOTIFICATION_METHOD}, got {notification.method}",
        )
    params = notification.params
    if params["content"] != (
        '[peer:claude_code "Coordinator" instance=agi-sender] Please review'
    ):
        _fail("codex peer_message content", f"unexpected content {params['content']!r}")
    _ok("codex peer_message emits notifications/homunculus/peer_message")


async def case_codex_peer_message_preserves_metadata() -> None:
    notification = await _emit("codex", PEER_EVENT)
    meta = notification.params["meta"]
    expected_preserved = {
        "thread_id": "agt-1",
        "message_id": "agm-1",
        "thread_cursor": 3,
        "from_agent_id": "claude_code",
        "from_agent_instance_id": "agi-sender",
        "from_session_label": "Coordinator",
        "to_agent_id": "codex",
        "to_agent_instance_id": "agi-codex",
        "important": True,
        "sent_at": "2026-05-30T00:00:00+00:00",
        "extra_nested": {"kept": True},
    }
    for key, expected in expected_preserved.items():
        if meta.get(key) != expected:
            _fail(
                "codex metadata preservation",
                f"{key}: expected {expected!r}, got {meta.get(key)!r}",
            )
    expected_added = {
        "source": "homunculus",
        "event_type": "peer_message",
        "source_event_type": "peer_message",
        "flow_id": "flow-top",
        "cursor": 7,
        "bridge_cursor": 7,
        "recipient_agent_id": "codex",
        "recipient_agent_instance_id": "agi-codex",
        "trigger_turn": True,
    }
    for key, expected in expected_added.items():
        if meta.get(key) != expected:
            _fail(
                "codex metadata additions",
                f"{key}: expected {expected!r}, got {meta.get(key)!r}",
            )
    _ok("codex peer_message preserves full bridge metadata + wake fields")


async def case_codex_post_message_uses_solet_method() -> None:
    notification = await _emit(
        "codex",
        {
            "cursor": 8,
            "event_type": "post_message",
            "content": "Solet says hi",
            "meta": {"flow_id": "flow-meta"},
        },
    )
    if notification.method != SOLET_PEER_MESSAGE_NOTIFICATION_METHOD:
        _fail(
            "codex post_message method",
            f"expected {SOLET_PEER_MESSAGE_NOTIFICATION_METHOD}, got {notification.method}",
        )
    if notification.params["content"] != "Solet says hi":
        _fail("codex post_message content", repr(notification.params["content"]))
    _ok("codex post_message emits notifications/homunculus/peer_message")


async def case_codex_non_peer_event_keeps_legacy_channel() -> None:
    notification = await _emit(
        "codex",
        {
            "cursor": 9,
            "event_type": "bridge_delivery_result",
            "content": '{"status":"ok"}',
            "meta": {"flow_id": "flow-result"},
        },
    )
    if notification.method != CLAUDE_CHANNEL_NOTIFICATION_METHOD:
        _fail(
            "codex bridge_delivery_result method",
            f"expected {CLAUDE_CHANNEL_NOTIFICATION_METHOD}, got {notification.method}",
        )
    _ok("codex non-peer events keep notifications/claude/channel")


async def case_claude_peer_message_keeps_legacy_shape() -> None:
    notification = await _emit("claude_code", PEER_EVENT)
    if notification.method != CLAUDE_CHANNEL_NOTIFICATION_METHOD:
        _fail(
            "claude peer_message method",
            f"expected {CLAUDE_CHANNEL_NOTIFICATION_METHOD}, got {notification.method}",
        )
    params = notification.params
    if params["content"] != "[peer_message] Please review":
        _fail("claude peer_message content", repr(params["content"]))
    meta = params["meta"]
    expected_meta = {
        "source": "homunculus",
        "event_type": "post_message",
        "source_event_type": "peer_message",
        "flow_id": "flow-top",
        "cursor": "0",
    }
    if meta != expected_meta:
        _fail("claude legacy metadata shape", f"expected {expected_meta!r}, got {meta!r}")
    _ok("claude peer_message keeps notifications/claude/channel + 5-key meta")


async def main() -> int:
    print("=== Forwarder native notification smoke ===")
    await case_codex_peer_message_uses_solet_method()
    await case_codex_peer_message_preserves_metadata()
    await case_codex_post_message_uses_solet_method()
    await case_codex_non_peer_event_keeps_legacy_channel()
    await case_claude_peer_message_keeps_legacy_shape()
    print()
    print("FORWARDER NATIVE NOTIFICATION SMOKE: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
