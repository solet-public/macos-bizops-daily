#!/usr/bin/env python3
"""Retired backend-dispatch tools fail CLEAN for stale clients (D3 main slice).

The five backend-dispatch bridge tools (``agent_thread_open`` / ``agent_send``
/ ``agent_messages`` / ``agent_status`` / ``agent_close``) and their HTTP
routes were removed along with the dormant ``GuardedAgentInterface`` head.
A session that connected before this landed still holds the old tool
descriptors in its MCP client cache until its bridge restarts/reconnects —
this smoke proves a call against one of those stale names fails with a
clear, typed error on every surface (HTTP 404, JSON-RPC ``METHOD_NOT_FOUND``,
stdio ``ValueError``), never a 500-class flood or an unhandled exception
that would crash the bridge subprocess.

RED-FIRST: before the D3 main-slice removal, every case below did the
OPPOSITE of what it asserts — the HTTP routes existed (200/202, not 404),
the streamable dispatch table had these five keys (a successful tool call,
not ``JsonRpcError``), and the stdio dispatch table had them too (a
``Forwarder`` call, not ``ValueError``). Re-adding any one of the five
tool names to any one of the three dispatch surfaces (or its HTTP route)
is the failing mutation this smoke exists to catch.

Run:
    SOLET_NAME=<name>-test .venv/bin/python3 \
        plugins/agent_messaging_plugin/tests/retired_backend_dispatch_stale_client_smoke.py
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import Any, cast

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT / "ananta" / "src"))
sys.path.insert(0, str(REPO_ROOT / "plugins" / "agent_messaging_plugin" / "src"))

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from agent_messaging_plugin.http_routes import register_routes  # noqa: E402
from agent_messaging_plugin.mcp_bridge.__main__ import (  # noqa: E402
    _TOOL_DISPATCH as STDIO_DISPATCH,
)
from agent_messaging_plugin.mcp_bridge.__main__ import _dispatch_tool  # noqa: E402
from agent_messaging_plugin.mcp_streamable.dispatch import (  # noqa: E402
    _METHOD_NOT_FOUND,
    DispatchContext,
    JsonRpcError,
    JsonRpcRequest,
    dispatch_request,
)
from agent_messaging_plugin.mcp_streamable.dispatch import (  # noqa: E402
    _TOOL_HANDLERS as STREAMABLE_HANDLERS,
)
from agent_messaging_plugin.mcp_streamable.session import (  # noqa: E402
    StreamableSession,
)
from agent_messaging_plugin.mcp_streamable.tools import TOOLS as STREAMABLE_TOOLS  # noqa: E402
from agent_messaging_plugin.models import BridgeBinding  # noqa: E402

_RETIRED_TOOLS: tuple[str, ...] = (
    "agent_thread_open",
    "agent_send",
    "agent_messages",
    "agent_status",
    "agent_close",
)

_RETIRED_HTTP_ROUTES: tuple[tuple[str, str], ...] = (
    ("POST", "/agent/thread/open"),
    ("POST", "/agent/agt-stale/send"),
    ("GET", "/agent/agt-stale/messages"),
    ("GET", "/agent/agt-stale/status"),
    ("POST", "/agent/agt-stale/close"),
)

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


def test_retired_tools_absent_from_both_dispatch_tables() -> None:
    """Precondition: the removal actually happened on both MCP surfaces."""
    for name in _RETIRED_TOOLS:
        _check(
            name not in STDIO_DISPATCH,
            f"{name!r} is not a stdio _TOOL_DISPATCH entry",
        )
        _check(
            name not in STREAMABLE_HANDLERS,
            f"{name!r} is not a streamable _TOOL_HANDLERS entry",
        )
        _check(
            name not in {tool.get("name") for tool in STREAMABLE_TOOLS},
            f"{name!r} is not advertised in the streamable TOOLS descriptor list",
        )


def test_streamable_stale_call_is_clean_method_not_found() -> None:
    """A stale streamable client's tools/call for a retired name gets a
    typed JsonRpcError(METHOD_NOT_FOUND), not a 500-class crash."""
    binding = BridgeBinding(
        bridge_id="agc-stale",
        agent_id="claude_code",
        agent_instance_id="agi-stale",
        session_label="stale-cached-client",
        parent_pid=1,
    )
    session = StreamableSession(
        mcp_session_id="mcp-stale",
        bridge_id="agc-stale",
        session_id="ags-stale",
        agent_id="claude_code",
        agent_instance_id="agi-stale",
        session_label="stale-cached-client",
        binding=binding,
    )
    context = DispatchContext(
        bridge_manager=cast("Any", object()),
        peer_registry=cast("Any", object()),
        platform_surface=cast("Any", object()),
        agent_messaging_service=cast("Any", object()),
        state_service=cast("Any", object()),
        solet_name="example-test",
    )
    for name in _RETIRED_TOOLS:
        raised: JsonRpcError | None = None
        try:
            dispatch_request(
                JsonRpcRequest(
                    method="tools/call",
                    params={"name": name, "arguments": {}},
                    id=1,
                ),
                session=session,
                context=context,
            )
        except JsonRpcError as exc:
            raised = exc
        _check(
            raised is not None,
            f"streamable tools/call({name!r}) raises JsonRpcError, not a crash",
        )
        if raised is not None:
            _check(
                raised.code == _METHOD_NOT_FOUND,
                f"streamable {name!r} error code is METHOD_NOT_FOUND "
                f"(-32601), got {raised.code}",
            )
            _check(
                name in raised.message,
                f"streamable {name!r} error message names the unknown tool",
            )


def test_stdio_stale_call_is_clean_value_error() -> None:
    """A stale stdio client's call_tool for a retired name raises a typed
    ValueError inside the MCP SDK's call_tool handler (which the SDK turns
    into a clean tool-error result) — never a KeyError/AttributeError from
    trying to actually reach a Forwarder method that no longer exists."""

    async def _call(name: str) -> Exception | None:
        try:
            # forwarder is never touched for an unknown name (the dispatch
            # table lookup fails before any Forwarder attribute access), so
            # None stands in safely here.
            await _dispatch_tool(cast("Any", None), name, {})
        except Exception as exc:  # noqa: BLE001 — capturing the exact type IS the assertion
            return exc
        return None

    for name in _RETIRED_TOOLS:
        exc = asyncio.run(_call(name))
        _check(
            isinstance(exc, ValueError),
            f"stdio call_tool({name!r}) raises ValueError, not a crash "
            f"or a Forwarder network attempt (got {type(exc).__name__ if exc else None})",
        )
        if exc is not None:
            _check(
                name in str(exc),
                f"stdio {name!r} error message names the unknown tool",
            )


def _build_app() -> FastAPI:
    app = FastAPI()
    stub = object()
    register_routes(
        app,
        bridge_manager=stub,  # type: ignore[arg-type]
        peer_registry=stub,  # type: ignore[arg-type]
        platform_surface=stub,  # type: ignore[arg-type]
        agent_messaging_service=stub,
        config={"long_poll_timeout_seconds": 1},
    )
    return app


def test_http_stale_routes_are_clean_404_not_500() -> None:
    """A stale client (or a stale bookmarked URL) hitting one of the five
    removed HTTP routes gets FastAPI's standard 404, never a 500 — the
    routes are gone from the table, not present-but-broken."""
    app = _build_app()
    with TestClient(app) as client:
        for method, path in _RETIRED_HTTP_ROUTES:
            url = f"/api/v1/bridge/agc-stale{path}"
            response = client.request(method, url)
            _check(
                response.status_code == 404,
                f"{method} {path} returns 404 (not 500), got "
                f"{response.status_code}",
            )
            _check(
                response.status_code < 500,
                f"{method} {path} does not 500-flood a stale client",
            )


def main() -> int:
    print("=== Retired backend-dispatch stale-client failure-shape smoke ===")
    test_retired_tools_absent_from_both_dispatch_tables()
    test_streamable_stale_call_is_clean_method_not_found()
    test_stdio_stale_call_is_clean_value_error()
    test_http_stale_routes_are_clean_404_not_500()
    print(f"\n{_passed} passed, {len(_failed)} failed")
    if _failed:
        for label in _failed:
            print(f"  FAILED: {label}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
