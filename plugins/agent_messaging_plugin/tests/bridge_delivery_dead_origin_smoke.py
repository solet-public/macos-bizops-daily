#!/usr/bin/env python3
"""Dead-origin bridge delivery is a terminal drop, never an inference retry.

Regression for the self-feeding loop where ``deliver_result`` /
``deliver_error`` returned ``bridge.no_active_bridge`` after the originating
MCP bridge closed. The action poller treated that as an EDGE failure, invoked
``process_error``, routed it to ``sys:autonomic``, and INF-06 persisted and
re-drove the resulting forwarded vertex.

The transport fact is irreversible: a closed origin cannot receive the
original payload or an inference-formatted explanation. Both EDGE_SINK verbs
must complete with a structured terminal-drop status, return zero continuation
actions, and emit one loud warning.
"""

from __future__ import annotations

import logging
import sys
from logging.handlers import BufferingHandler
from pathlib import Path
from typing import Any, cast

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT / "ananta" / "src"))
sys.path.insert(0, str(REPO_ROOT / "plugins" / "agent_messaging_plugin" / "src"))

from agent_messaging_plugin.bridge_sessions import BridgeNotFoundError  # noqa: E402
from agent_messaging_plugin.plugin import AgentMessagingPlugin  # noqa: E402

_BRIDGE_ID = "agc-gone-smoke"


class _GoneBridgeManager:
    """Bridge-manager seam whose target disappeared before terminal delivery."""

    def append_event(
        self,
        bridge_id: str,
        event_type: str,
        content: str,
        meta: dict[str, object] | None = None,
    ) -> None:
        del event_type, content, meta
        raise BridgeNotFoundError(bridge_id)


def _plugin() -> AgentMessagingPlugin:
    plugin = AgentMessagingPlugin()
    plugin._bridge_manager = cast(Any, _GoneBridgeManager())  # noqa: SLF001
    return plugin


def _assert_terminal_drop(
    result: dict[str, Any],
    records: list[logging.LogRecord],
    *,
    event_type: str,
) -> None:
    assert result["action_status"] == "completed", result
    assert result["data"] == {"status": "dropped_bridge_gone"}, result
    assert result["actions"] == [], (
        "terminal delivery must return no continuation action; a process_error "
        "or forwarded vertex would re-open the dead-origin loop"
    )
    messages = [record.getMessage() for record in records]
    assert len(messages) == 1, messages
    assert event_type in messages[0], messages
    assert _BRIDGE_ID in messages[0], messages


def _drive(
    plugin: AgentMessagingPlugin,
    handler: BufferingHandler,
    *,
    name: str,
) -> None:
    if name == "deliver_result":
        result = plugin.deliver_result(
            {
                "result_payload": {"answer": 42},
                "source_process_key": "plugin::agent_messaging_plugin::peer_holds_role",
                "bridge_id": _BRIDGE_ID,
            },
            {},
        )
        event_type = "bridge_delivery_result"
    else:
        result = plugin.deliver_error(
            {
                "error_payload": {"code": "example.failed"},
                "source_process_key": "plugin::agent_messaging_plugin::peer_holds_role",
                "bridge_id": _BRIDGE_ID,
            },
            {},
        )
        event_type = "bridge_delivery_error"
    records = list(handler.buffer)
    handler.buffer.clear()
    _assert_terminal_drop(result, records, event_type=event_type)
    print(f"  ok  {name}_dead_origin_is_terminal")


def main() -> int:
    handler = BufferingHandler(capacity=10)
    logger = logging.getLogger("agent_messaging_plugin.plugin")
    previous_level = logger.level
    logger.setLevel(logging.WARNING)
    logger.addHandler(handler)
    try:
        plugin = _plugin()
        _drive(plugin, handler, name="deliver_result")
        _drive(plugin, handler, name="deliver_error")
    finally:
        logger.removeHandler(handler)
        logger.setLevel(previous_level)
    print("bridge_delivery_dead_origin_smoke: 2/2 passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
