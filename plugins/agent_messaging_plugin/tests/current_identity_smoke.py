#!/usr/bin/env python3
"""Smoke coverage for native MCP ``current_identity``.

Run:
    SOLET_NAME=<name>-test .venv/bin/python3 \
        plugins/agent_messaging_plugin/tests/current_identity_smoke.py
"""

from __future__ import annotations

import asyncio
import json
import re
import sys
from pathlib import Path
from typing import Any, cast

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT / "ananta" / "src"))
sys.path.insert(0, str(REPO_ROOT / "plugins" / "agent_messaging_plugin" / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _real_state_fake import RealShapeState  # noqa: E402
from ananta.interfaces.state_management_interface import (  # noqa: E402
    StateManagementInterface,
)
from ananta.llm.agent_messaging.role_binding import (  # noqa: E402
    HOLDER_KIND_SESSION,
)
from ananta.services.store import Store, open_store  # noqa: E402
from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from agent_messaging_plugin.bridge_sessions import BridgeSessionManager  # noqa: E402
from agent_messaging_plugin.http_routes import register_routes  # noqa: E402
from agent_messaging_plugin.mcp_bridge.forwarder import Forwarder  # noqa: E402
from agent_messaging_plugin.mcp_streamable.dispatch import (  # noqa: E402
    DispatchContext,
    JsonRpcRequest,
    dispatch_request,
)
from agent_messaging_plugin.mcp_streamable.session import (  # noqa: E402
    StreamableSession,
)
from agent_messaging_plugin.mcp_streamable.tools import TOOLS  # noqa: E402
from agent_messaging_plugin.models import BridgeBinding  # noqa: E402
from agent_messaging_plugin.peer_registry import PeerRegistry  # noqa: E402
from agent_messaging_plugin.role_binding_store import (  # noqa: E402
    HolderClaim,
    claim_role_binding_v4,
)
from agent_messaging_plugin.schema import (  # noqa: E402
    PEER_BINDING_NAMESPACE,
    get_peer_binding_schema,
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


def _state() -> tuple[RealShapeState, StateManagementInterface]:
    fake = RealShapeState()
    return fake, cast(StateManagementInterface, fake)


def _fresh_peer_registry() -> PeerRegistry:
    store: Store = open_store(
        get_peer_binding_schema(),
        namespace=PEER_BINDING_NAMESPACE,
        backend="in_memory",
    )
    return PeerRegistry(bindings_store=store)


def _bridge_manager() -> BridgeSessionManager:
    return BridgeSessionManager(
        session_id_factory=lambda _name: "ags-http",
        idle_timeout_s=3600,
        max_pending_events=20,
        long_poll_timeout_s=1,
    )


def _binding(
    *,
    bridge_id: str,
    agent_id: str,
    agent_instance_id: str,
    session_label: str,
    agent_session_id: str = "",
) -> BridgeBinding:
    return BridgeBinding(
        bridge_id=bridge_id,
        agent_id=agent_id,
        agent_instance_id=agent_instance_id,
        session_label=session_label,
        parent_pid=123,
        agent_session_id=agent_session_id,
    )


def _claim_role(
    state: StateManagementInterface, *, role: str, agent_instance_id: str,
) -> None:
    # §9 CUTOVER: current_identity's roles_held reads the v4 role_binding table
    # (list_roles_for_agent_instance), so the fixture must seed via the live v4
    # claim path — the now-removed legacy claim_role_binding (v3) wrote a shape the
    # reverse lookup no longer reads.
    claim_role_binding_v4(
        state,
        name=role,
        claim=HolderClaim(
            holder_kind=HOLDER_KIND_SESSION,
            holder_identity={"agent_id": "codex", "session_label": role},
            agent_instance_id=agent_instance_id,
            agent_session_id="ags-role",
            session_label="role-label-does-not-drive-current-identity",
        ),
    )


def test_http_identity_uses_role_binding_not_label() -> None:
    manager = _bridge_manager()
    bridge = manager.open(solet_name="", parent_pid=123)
    registry = _fresh_peer_registry()
    registry.register(
        _binding(
            bridge_id=bridge.bridge_id,
            agent_id="codex",
            agent_instance_id="agi-http",
            session_label="Coordinator",
        ),
    )
    _fake, state = _state()
    _claim_role(state, role="Architect", agent_instance_id="agi-http")

    app = FastAPI()
    register_routes(
        app,
        bridge_manager=manager,
        peer_registry=registry,
        platform_surface=cast(Any, object()),
        agent_messaging_service=cast(Any, object()),
        config={"long_poll_timeout_seconds": 1},
        state_service=state,
    )
    with TestClient(app) as client:
        response = client.get(f"/api/v1/bridge/{bridge.bridge_id}/current_identity")
    payload = response.json()
    _check(response.status_code == 200, "HTTP current_identity returns 200")
    _check(payload.get("session_label") == "Coordinator", "HTTP preserves label")
    _check(
        payload.get("roles_held") == ["Architect"],
        "HTTP roles_held comes from agent_role_binding, not session_label",
    )


class _ForwarderProbe(Forwarder):
    def __init__(self) -> None:
        super().__init__(
            base_url="http://127.0.0.1:1",
            solet_name="example-test",
            agent_id="codex",
            agent_instance_id="agi-stdio",
            agent_session_id="ags-stdio",
            session_label="Codex-Reviewer",
            parent_pid=123,
            # INF-01 §D.9 client half: the Forwarder now declares its
            # inference capability on every register POST. codex does NOT
            # provide inference (the sys:autonomic provider is claude_code
            # only), so this probe faithfully carries provides_inference=False.
            provides_inference=False,
        )
        self._bridge_id = "agc-stdio"
        self.requested_path = ""

    async def _get(self, path: str) -> dict[str, Any]:
        self.requested_path = path
        return {
            "transport": "bridge_http",
            "solet_name": "",
            "agent_id": "codex",
            "agent_instance_id": "agi-stdio",
            "agent_session_id": "",
            "session_label": "Codex-Reviewer",
            "bridge_id": "agc-stdio",
            "mcp_session_id": "",
            "roles_held": ["Codex-Reviewer"],
            "identity_trust": "bridge_registered",
            "streamable_no_auth": False,
        }

    async def close_client(self) -> None:
        await self._client.aclose()


async def _stdio_identity() -> tuple[dict[str, Any], str]:
    forwarder = _ForwarderProbe()
    try:
        payload = await forwarder.current_identity()
        return payload, forwarder.requested_path
    finally:
        await forwarder.close_client()


def test_stdio_current_identity_merges_transport_fields() -> None:
    payload, path = asyncio.run(_stdio_identity())
    _check(
        path == "/api/v1/bridge/agc-stdio/current_identity",
        "stdio current_identity calls bridge-scoped HTTP route",
    )
    _check(payload.get("transport") == "stdio", "stdio transport is reported")
    _check(
        payload.get("solet_name") == "example-test",
        "stdio solet_name comes from forwarder",
    )
    _check(
        payload.get("agent_session_id") == "ags-stdio",
        "stdio agent_session_id comes from forwarder",
    )
    _check(payload.get("identity_trust") == "stdio_bridge", "stdio trust field")


def _streamable_session(
    *,
    agent_id: str,
    agent_instance_id: str,
    session_label: str,
    agent_session_id: str = "",
) -> StreamableSession:
    binding = _binding(
        bridge_id="agc-stream",
        agent_id=agent_id,
        agent_instance_id=agent_instance_id,
        session_label=session_label,
        agent_session_id=agent_session_id,
    )
    return StreamableSession(
        mcp_session_id="mcp-stream",
        bridge_id="agc-stream",
        session_id="ags-stream",
        agent_id=agent_id,
        agent_instance_id=agent_instance_id,
        session_label=session_label,
        binding=binding,
        agent_session_id=agent_session_id,
    )


def _call_streamable_current_identity(
    *, session: StreamableSession, state: StateManagementInterface,
) -> dict[str, Any]:
    context = DispatchContext(
        bridge_manager=cast(Any, object()),
        peer_registry=cast(Any, object()),
        platform_surface=cast(Any, object()),
        agent_messaging_service=cast(Any, object()),
        state_service=state,
        solet_name="example-test",
    )
    response = dispatch_request(
        JsonRpcRequest(
            method="tools/call",
            params={"name": "current_identity", "arguments": {}},
            id=1,
        ),
        session=session,
        context=context,
    )
    result = cast(dict[str, Any], response.result if response is not None else {})
    content = cast(list[dict[str, str]], result["content"])
    return cast(dict[str, Any], json.loads(content[0]["text"]))


def test_streamable_current_identity() -> None:
    _fake, state = _state()
    _claim_role(state, role="Architect", agent_instance_id="agi-stream")
    payload = _call_streamable_current_identity(
        session=_streamable_session(
            agent_id="claude_phone",
            agent_instance_id="agi-stream",
            session_label="Coordinator",
            agent_session_id="ases-stream",
        ),
        state=state,
    )
    _check(
        payload.get("transport") == "streamable_http",
        "streamable transport is reported",
    )
    _check(
        payload.get("identity_trust") == "shared_oauth_bearer_verified",
        "streamable trust names only shared OAuth bearer verification",
    )
    _check(
        payload.get("sender_transport_principal") == "",
        "manually constructed streamable fixture does not invent a transport principal",
    )
    _check(
        payload.get("agent_session_id") == "ases-stream",
        "streamable agent_session_id is surfaced",
    )
    _check(
        payload.get("roles_held") == ["Architect"],
        "streamable roles_held comes from binding state, not label",
    )


def test_streamable_no_auth_sentinel() -> None:
    _fake, state = _state()
    payload = _call_streamable_current_identity(
        session=_streamable_session(
            agent_id="tunnel_passthrough",
            agent_instance_id="tunnel_passthrough",
            session_label="streamable_no_auth",
        ),
        state=state,
    )
    _check(
        payload.get("agent_id") == "tunnel_passthrough",
        "no-auth sentinel agent_id is surfaced",
    )
    _check(
        payload.get("agent_instance_id") == "tunnel_passthrough",
        "no-auth sentinel agent_instance_id is surfaced",
    )
    _check(
        payload.get("identity_trust") == "outer_boundary_only",
        "no-auth sentinel reports outer-boundary trust",
    )
    _check(payload.get("streamable_no_auth") is True, "no-auth boolean is true")


def _descriptor_text(tool: dict[str, Any]) -> str:
    return f"{tool.get('name', '')} {tool.get('description', '')}".lower()


def _score_tool(query: str, tool: dict[str, Any]) -> int:
    terms = [
        term
        for term in re.findall(r"[a-z_]+", query.lower())
        if len(term) > 2 and term not in {"the", "and", "for", "what", "does"}
    ]
    text = _descriptor_text(tool)
    return sum(text.count(term.replace("_", " ")) + text.count(term) for term in terms)


def test_model_natural_current_identity_selection_proxy() -> None:
    query = "who am I current identity roles held routing metadata"
    ranked = sorted(
        ((tool.get("name"), _score_tool(query, tool)) for tool in TOOLS),
        key=lambda item: item[1],
        reverse=True,
    )
    _check(
        ranked[0][0] == "current_identity",
        "descriptor-only selector chooses current_identity for who-am-I query",
    )
    peer_register = next(tool for tool in TOOLS if tool.get("name") == "peer_register")
    _check(
        "use current_identity" in str(peer_register.get("description")),
        "peer_register descriptor redirects identity introspection",
    )


def main() -> int:
    print("=== current_identity smoke ===")
    test_http_identity_uses_role_binding_not_label()
    test_stdio_current_identity_merges_transport_fields()
    test_streamable_current_identity()
    test_streamable_no_auth_sentinel()
    test_model_natural_current_identity_selection_proxy()
    print(f"\n{_passed} passed, {len(_failed)} failed")
    if _failed:
        for label in _failed:
            print(f"  FAILED: {label}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
