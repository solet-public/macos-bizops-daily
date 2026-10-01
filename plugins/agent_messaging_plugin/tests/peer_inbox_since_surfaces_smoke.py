#!/usr/bin/env python3
"""``since`` reaches the service from every ``peer_inbox`` surface (iss_17aefd54).

``peer_inbox_since_cursor_smoke`` pins the semantics through the platform
process. The same page is also served by the localhost ``/peer/inbox`` route,
the Streamable-HTTP MCP tool and the stdio MCP bridge tool (which forwards to
the route), and each parses its own arguments: a surface that dropped ``since``
would hand its callers the newest-first page back with no error. Each surface is
driven end to end against the real service and rows, and the advertised tool
text is checked against the behavior it describes. Hermetic.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from typing import Any, cast

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT / "ananta" / "src"))
sys.path.insert(0, str(REPO_ROOT / "plugins" / "agent_messaging_plugin" / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _real_state_fake import RealShapeState  # noqa: E402
from ananta.interfaces.state_management_interface import StateManagementInterface  # noqa: E402, TC002
from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from peer_inbox_instance_exhaustion_smoke import _AGENT_ID, _INSTANCE_ID, _SESSION_ID, _plugin, _seed  # noqa: E402
from peer_inbox_since_cursor_smoke import _at  # noqa: E402

from agent_messaging_plugin.bridge_sessions import BridgeSessionManager  # noqa: E402
from agent_messaging_plugin.http_routes import register_routes  # noqa: E402
from agent_messaging_plugin.mcp_bridge import __main__ as stdio_bridge  # noqa: E402
from agent_messaging_plugin.mcp_bridge.forwarder import Forwarder  # noqa: E402
from agent_messaging_plugin.mcp_streamable.dispatch import (  # noqa: E402
    DispatchContext,
    JsonRpcError,
    JsonRpcRequest,
    dispatch_request,
)
from agent_messaging_plugin.mcp_streamable.session import StreamableSession  # noqa: E402
from agent_messaging_plugin.mcp_streamable.tools import TOOLS  # noqa: E402
from agent_messaging_plugin.models import BridgeBinding  # noqa: E402

_passed = 0
_failed: list[str] = []


def _check(condition: object, label: str) -> None:
    global _passed
    if condition:
        _passed += 1
        print(f"  PASS  {label}")
        return
    _failed.append(label)
    print(f"  FAIL  {label}")


def _cursors(page: dict[str, Any]) -> list[int]:
    return [entry["message"]["cursor"] for entry in page["entries"]]


def _route_client(state: RealShapeState) -> tuple[TestClient, str]:
    plugin = _plugin(state)
    registry = plugin._peer_registry  # noqa: SLF001
    manager = BridgeSessionManager(
        session_id_factory=lambda _n: "ags-since",
        idle_timeout_s=3600,
        max_pending_events=50,
        long_poll_timeout_s=1,
    )
    bridge_id = manager.open(solet_name="", parent_pid=77).bridge_id
    registry.register(
        BridgeBinding(
            bridge_id=bridge_id,
            agent_id=_AGENT_ID,
            agent_instance_id=_INSTANCE_ID,
            session_label="Instance-Reader",
            parent_pid=77,
            agent_session_id=_SESSION_ID,
        ),
    )
    app = FastAPI()
    register_routes(
        app,
        bridge_manager=manager,
        peer_registry=registry,
        platform_surface=cast(Any, object()),
        agent_messaging_service=cast(Any, plugin._service),  # noqa: SLF001
        config={"long_poll_timeout_seconds": 1},
        state_service=cast(StateManagementInterface, state),
    )
    return TestClient(app), bridge_id


def test_route() -> None:
    state = RealShapeState()
    _seed(state, count=6, threads=2)
    client, bridge_id = _route_client(state)
    base = f"/api/v1/bridge/{bridge_id}/peer/inbox?limit=50"
    since = client.get(f"{base}&since={_at(2)}")
    page = since.json()
    _check(since.status_code == 200 and _cursors(page) == [3, 4, 5], "route: since returns the rows newer than T, oldest-first")
    _check(page["next_since_created_at"] == _at(5) and page["next_after_created_at"] is None, "route: a since page carries only the forward cursor")
    after = client.get(f"{base}&after={_at(2)}").json()
    _check(_cursors(after) == [1, 0] and after["next_since_created_at"] is None, "route: after still walks backward")
    both = client.get(f"{base}&after={_at(2)}&since={_at(1)}")
    _check(both.status_code == 400, "route: naming both cursors is refused")
    bad = client.get(f"{base}&since=last-tuesday")
    _check(bad.status_code == 400 and bad.json()["code"] == "invalid_since", "route: a malformed since is a 400 invalid_since")


def _streamable_call(state: RealShapeState, arguments: dict[str, Any]) -> Any:
    plugin = _plugin(state)
    binding = BridgeBinding(
        bridge_id="agc-stream",
        agent_id=_AGENT_ID,
        agent_instance_id=_INSTANCE_ID,
        session_label="Instance-Reader",
        parent_pid=1,
        agent_session_id=_SESSION_ID,
    )
    session = StreamableSession(
        mcp_session_id="mcp-since",
        bridge_id="agc-stream",
        session_id="ags-stream",
        agent_id=_AGENT_ID,
        agent_instance_id=_INSTANCE_ID,
        session_label="Instance-Reader",
        binding=binding,
        agent_session_id=_SESSION_ID,
    )
    context = DispatchContext(
        bridge_manager=cast(Any, object()),
        peer_registry=plugin._peer_registry,  # noqa: SLF001
        platform_surface=cast(Any, object()),
        agent_messaging_service=cast(Any, plugin._service),  # noqa: SLF001
        state_service=cast(StateManagementInterface, state),
        solet_name="example-test",
    )
    return dispatch_request(
        JsonRpcRequest(method="tools/call", params={"name": "peer_inbox", "arguments": arguments}, id=1),
        session=session,
        context=context,
    )


def test_streamable_dispatch() -> None:
    state = RealShapeState()
    _seed(state, count=6, threads=2)
    response = _streamable_call(state, {"since": _at(2), "limit": 50})
    payload = json.loads(response.result["content"][0]["text"])
    _check(_cursors(payload) == [3, 4, 5], "streamable: since returns the rows newer than T, oldest-first")
    _check(payload["next_since_created_at"] == _at(5), "streamable: the forward cursor is returned")
    for label, arguments in (
        ("naming both cursors", {"since": _at(1), "after": _at(2)}),
        ("a malformed since", {"since": "last-tuesday"}),
        ("a non-string since", {"since": 12345}),
    ):
        try:
            _streamable_call(state, arguments)
        except JsonRpcError as exc:
            _check("since" in str(exc), f"streamable: {label} is refused with a JSON-RPC error naming since")
            if label == "a non-string since":
                _check("must be a string" in str(exc), "streamable: a non-string since is refused as a type error, not parsed")
        else:
            _check(False, f"streamable: {label} is refused")


class _CapturingForwarder(Forwarder):
    def __init__(self) -> None:
        super().__init__(
            base_url="http://127.0.0.1:1",
            solet_name="example-test",
            agent_id=_AGENT_ID,
            agent_instance_id="agi-stdio",
            agent_session_id="ags-stdio",
            session_label="Stdio-Reader",
            parent_pid=123,
            provides_inference=False,
        )
        self._bridge_id = "agc-stdio"
        self.requested_path = ""

    async def _get(self, path: str) -> dict[str, Any]:
        self.requested_path = path
        return {}


def test_stdio_bridge_forwards_since() -> None:
    forwarder = _CapturingForwarder()
    try:
        asyncio.run(stdio_bridge._tool_peer_inbox(forwarder, {"since": _at(2), "limit": 5}))  # noqa: SLF001
    finally:
        asyncio.run(forwarder._client.aclose())  # noqa: SLF001
    _check(
        "since=" in forwarder.requested_path and "after=" not in forwarder.requested_path,
        "stdio bridge: the since argument reaches the /peer/inbox query",
    )


def _tool_text(tools: list[Any]) -> tuple[str, dict[str, Any]]:
    entry = next(t for t in tools if (t["name"] if isinstance(t, dict) else t.name) == "peer_inbox")
    if isinstance(entry, dict):
        return entry["description"], entry["inputSchema"]["properties"]
    return entry.description, entry.inputSchema["properties"]


def test_advertised_text_matches_behavior() -> None:
    for label, tools in (("streamable", TOOLS), ("stdio", stdio_bridge.TOOLS)):
        description, props = _tool_text(list(tools))
        _check("since" in props, f"{label}: the tool schema offers the forward cursor")
        _check("forward cursor; omit only for the first page" not in props["after"]["description"], f"{label}: after is no longer advertised as the forward cursor")
        _check("instance section is oldest-first" not in description, f"{label}: the description no longer calls the instance section oldest-first")
        _check("BACKWARD" in props["after"]["description"] and "FORWARD" in props["since"]["description"], f"{label}: each cursor names its direction")


def main() -> None:
    print("=== peer_inbox since surfaces smoke ===")
    test_route()
    test_streamable_dispatch()
    test_stdio_bridge_forwards_since()
    test_advertised_text_matches_behavior()
    print(f"\n{_passed} passed, {len(_failed)} failed")
    if _failed:
        for label in _failed:
            print(f"  FAILED: {label}")
        sys.exit(1)


if __name__ == "__main__":
    main()
