#!/usr/bin/env python3
"""Offline regression coverage for reconnect cursor-domain isolation.

This smoke exercises the actual ``Forwarder`` with only its HTTP and write
stream boundaries stubbed.  It pins three independent failure boundaries:

* recipient-scoped 404s do not reconnect the sender bridge;
* a late old-generation events page cannot advance the new bridge cursor;
* Claude wire cursors outlive bridge-local event cursor resets.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import Any

import httpx

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT / "ananta" / "src"))
sys.path.insert(0, str(REPO_ROOT / "plugins" / "agent_messaging_plugin" / "src"))

from agent_messaging_plugin.mcp_bridge.forwarder import (  # noqa: E402
    BridgeHTTPError,
    Forwarder,
    _is_bridge_gone,
)
from agent_messaging_plugin.mcp_bridge.owed_delivery import _drain_row_to_event  # noqa: E402


class _CaptureStream:
    def __init__(self) -> None:
        self.sent: list[Any] = []

    async def send(self, message: Any) -> None:
        self.sent.append(message)


def _forwarder() -> Forwarder:
    return Forwarder(
        base_url="http://stub.invalid",
        solet_name="smoke",
        agent_id="claude_code",
        agent_instance_id="agi-cursor-smoke",
        session_label="cursor-smoke",
        parent_pid=1,
        provides_inference=False,
    )


async def _close(forwarder: Forwarder) -> None:
    await forwarder._client.aclose()  # noqa: SLF001


def _wire_cursor(message: Any) -> str:
    return str(message.message.root.params["meta"]["cursor"])


def test_recipient_404_fails_closed() -> None:
    recipient_gone = BridgeHTTPError(
        "recipient disappeared",
        status_code=404,
        path="/api/v1/bridge/agc-sender/peer/send",
        response_code="peer_unreachable",
    )
    own_bridge_gone = BridgeHTTPError(
        "sender bridge disappeared",
        status_code=404,
        path="/api/v1/bridge/agc-sender/peer/send",
        response_code="bridge_not_found",
    )
    unknown_shape = BridgeHTTPError(
        "ambiguous 404",
        status_code=404,
        path="/api/v1/bridge/agc-sender/peer/send",
        response_code="future_error",
    )
    assert not _is_bridge_gone(recipient_gone)
    assert _is_bridge_gone(own_bridge_gone)
    assert not _is_bridge_gone(unknown_shape)
    try:
        Forwarder._unwrap(  # noqa: SLF001
            httpx.Response(404, json={"code": "bridge_not_found", "message": "gone"}),
            "/api/v1/bridge/agc-sender/events",
        )
    except BridgeHTTPError as exc:
        assert exc.response_code == "bridge_not_found"
        assert _is_bridge_gone(exc)
    else:
        raise AssertionError("404 response must raise BridgeHTTPError")
    print("  recipient 404 and unknown 404 fail closed; own bridge 404 reconnects")


async def test_late_old_page_cannot_advance_new_cursor() -> None:
    forwarder = _forwarder()
    started = asyncio.Event()
    release = asyncio.Event()
    stream = _CaptureStream()
    forwarder.bind_write_stream(stream)  # type: ignore[arg-type]
    forwarder._bridge_id = "agc-old"  # noqa: SLF001
    forwarder._cursor = 54  # noqa: SLF001
    forwarder._generation = 1  # noqa: SLF001

    async def _late_old_page(bridge_id: str, after: int) -> dict[str, Any]:
        assert (bridge_id, after) == ("agc-old", 54)
        started.set()
        await release.wait()
        return {
            "events": [
                {"cursor": 54, "event_type": "post_message", "content": "stale"},
            ],
            "next_cursor": 54,
        }

    forwarder._fetch_events = _late_old_page  # type: ignore[method-assign]
    pending = asyncio.create_task(forwarder._drain_once("agc-old", 1))  # noqa: SLF001
    await started.wait()
    forwarder._bridge_id = "agc-new"  # noqa: SLF001
    forwarder._cursor = -1  # noqa: SLF001
    forwarder._generation = 2  # noqa: SLF001
    release.set()
    await pending
    assert forwarder._cursor == -1  # noqa: SLF001
    assert stream.sent == []
    print("  late old-generation page neither emits nor advances new cursor")
    await _close(forwarder)


async def test_claude_wire_cursor_survives_reconnect() -> None:
    forwarder = _forwarder()
    stream = _CaptureStream()
    forwarder.bind_write_stream(stream)  # type: ignore[arg-type]
    forwarder._bridge_id = "agc-old"  # noqa: SLF001
    forwarder._generation = 1  # noqa: SLF001

    await forwarder._emit_event(  # noqa: SLF001
        {"cursor": 54, "event_type": "post_message", "content": "old"},
        generation=1,
    )
    forwarder._bridge_id = "agc-new"  # noqa: SLF001
    forwarder._cursor = -1  # noqa: SLF001
    forwarder._generation = 2  # noqa: SLF001
    await forwarder._emit_event(  # noqa: SLF001
        {"cursor": 0, "event_type": "post_message", "content": "new-0"},
        generation=2,
    )
    await forwarder._emit_event(  # noqa: SLF001
        {"cursor": 1, "event_type": "post_message", "content": "new-1"},
        generation=2,
    )
    assert [_wire_cursor(message) for message in stream.sent] == ["0", "1", "2"]
    assert forwarder._cursor == 1  # noqa: SLF001
    print("  Claude wire cursor remains monotonic while new bridge poll cursor resets")
    await _close(forwarder)


async def test_late_old_emit_cannot_advance_new_cursor() -> None:
    forwarder = _forwarder()
    started = asyncio.Event()
    release = asyncio.Event()

    class _BlockingStream:
        async def send(self, message: Any) -> None:
            del message
            started.set()
            await release.wait()

    forwarder.bind_write_stream(_BlockingStream())  # type: ignore[arg-type]
    forwarder._bridge_id = "agc-old"  # noqa: SLF001
    forwarder._cursor = 54  # noqa: SLF001
    forwarder._generation = 1  # noqa: SLF001
    pending = asyncio.create_task(
        forwarder._emit_event(  # noqa: SLF001
            {"cursor": 54, "event_type": "post_message", "content": "stale"},
            generation=1,
        ),
    )
    await started.wait()
    forwarder._bridge_id = "agc-new"  # noqa: SLF001
    forwarder._cursor = -1  # noqa: SLF001
    forwarder._generation = 2  # noqa: SLF001
    release.set()
    await pending
    assert forwarder._cursor == -1  # noqa: SLF001
    print("  late old-generation event cannot advance new cursor after stream send")
    await _close(forwarder)


async def test_replay_flow_id_matches_native_wake_convention() -> None:
    forwarder = _forwarder()
    stream = _CaptureStream()
    forwarder.bind_write_stream(stream)  # type: ignore[arg-type]
    event = _drain_row_to_event(
        {"content": "replay", "message_id": "arm-replay"},
        external_id="role:Coordinator:arm-replay",
        recipient_key="Coordinator",
    )
    await forwarder.emit_event(event)
    meta = stream.sent[0].message.root.params["meta"]
    assert meta["flow_id"] == "peer-wake-arm-replay"
    print("  replay flow_id uses native peer-wake convention")
    await _close(forwarder)


def main() -> int:
    print("=== Forwarder reconnect cursor smoke ===")
    test_recipient_404_fails_closed()
    asyncio.run(test_late_old_page_cannot_advance_new_cursor())
    asyncio.run(test_claude_wire_cursor_survives_reconnect())
    asyncio.run(test_late_old_emit_cannot_advance_new_cursor())
    asyncio.run(test_replay_flow_id_matches_native_wake_convention())
    print("FORWARDER RECONNECT CURSOR SMOKE: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
