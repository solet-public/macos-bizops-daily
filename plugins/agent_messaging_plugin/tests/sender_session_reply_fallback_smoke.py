#!/usr/bin/env python3
"""A2: direct-reply routing survives sender instance rotation.

Evidence class: HERMETIC, NOT LIVE. The fixture constructs the registry state a
natural subprocess relaunch produces (old instance absent, stable session key
bound to the replacement). It does not manufacture a fleet restart for proof.

The acceptance contract is deliberately end-to-end across the reachable peer_send
surface: exact instance first; stable session only after peer_unreachable; unknown
keys preserve the original error; duplicate stable keys fail loud with candidates;
and both MCP schemas teach the same order.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT / "ananta" / "src"))
sys.path.insert(0, str(REPO_ROOT / "plugins" / "agent_messaging_plugin" / "src"))

from ananta.services.store import open_store  # noqa: E402

from agent_messaging_plugin.bridge_sessions import BridgeSessionManager  # noqa: E402
from agent_messaging_plugin.http_routes import (  # noqa: E402
    PeerSendBody,
    _peer_send_impl,
)
from agent_messaging_plugin.mcp_bridge.__main__ import (  # noqa: E402
    TOOLS as STDIO_TOOLS,
)
from agent_messaging_plugin.mcp_bridge.forwarder import Forwarder  # noqa: E402
from agent_messaging_plugin.mcp_streamable.dispatch import (  # noqa: E402
    _tool_peer_send as _streamable_peer_send,
)
from agent_messaging_plugin.mcp_streamable.tools import (  # noqa: E402
    TOOLS as STREAMABLE_TOOLS,
)
from agent_messaging_plugin.models import BridgeBinding  # noqa: E402
from agent_messaging_plugin.peer_dispatch import (  # noqa: E402
    build_peer_message_meta,
    build_wake_reply_hint,
)
from agent_messaging_plugin.peer_registry import PeerRegistry  # noqa: E402
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


class _PeerResult:
    thread_id = "agt-a2"
    message_id = "agm-a2"
    cursor = 1


class _Service:
    def __init__(self) -> None:
        self.requests: list[Any] = []

    def peer_send(self, request: Any) -> _PeerResult:
        self.requests.append(request)
        return _PeerResult()


def _registry() -> PeerRegistry:
    store = open_store(
        get_peer_binding_schema(),
        namespace=PEER_BINDING_NAMESPACE,
        backend="in_memory",
    )
    return PeerRegistry(bindings_store=store)


def _binding(
    *, bridge_id: str, agent_id: str, instance_id: str, session_id: str, label: str,
) -> BridgeBinding:
    return BridgeBinding(
        bridge_id=bridge_id,
        agent_id=agent_id,
        agent_instance_id=instance_id,
        session_label=label,
        parent_pid=None,
        agent_session_id=session_id,
    )


class _Harness:
    def __init__(self) -> None:
        self.manager = BridgeSessionManager(
            session_id_factory=lambda _name: "ags-a2",
            idle_timeout_s=3600,
            max_pending_events=20,
            long_poll_timeout_s=1,
        )
        self.registry = _registry()
        self.service = _Service()
        sender = self.manager.open("")
        self.sender_bridge_id = sender.bridge_id
        self.registry.register(
            _binding(
                bridge_id=sender.bridge_id,
                agent_id="codex",
                instance_id="agi-sender",
                session_id="ases-sender",
                label="Sender",
            ),
        )

    def add_target(self, *, instance_id: str, session_id: str, label: str) -> None:
        bridge = self.manager.open("")
        self.registry.register(
            _binding(
                bridge_id=bridge.bridge_id,
                agent_id="claude_code",
                instance_id=instance_id,
                session_id=session_id,
                label=label,
            ),
        )

    def send(self, *, instance_id: str, session_id: str) -> tuple[int, dict[str, Any]]:
        response = _peer_send_impl(
            bridge_id=self.sender_bridge_id,
            body=PeerSendBody(
                peer_id="claude_code",
                peer_agent_instance_id=instance_id,
                peer_agent_session_id=session_id,
                content=[{"type": "text", "text": "silent reply"}],
            ),
            bridge_manager=self.manager,
            peer_registry=self.registry,
            agent_messaging_service=self.service,
        )
        payload = json.loads(bytes(response.body))
        assert isinstance(payload, dict)
        return response.status_code, payload


def test_l4a_live_instance_wins_over_a_different_session_key() -> None:
    h = _Harness()
    h.add_target(instance_id="agi-live", session_id="ases-live", label="Live")
    h.add_target(instance_id="agi-other", session_id="ases-other", label="Other")
    status, payload = h.send(instance_id="agi-live", session_id="ases-other")
    _check(
        status == 200 and payload.get("delivered_to_agent_instance_id") == "agi-live",
        "L4a exact live instance wins even when the fallback key points elsewhere",
    )


def test_l4b_rotated_instance_falls_back_to_the_stable_session() -> None:
    h = _Harness()
    h.add_target(instance_id="agi-replacement", session_id="ases-stable", label="New")
    # Fixture precondition from the ruled plan: the REAL PeerRegistry filter must
    # match the right stable key and reject a wrong one before this can count.
    correct = h.registry.resolve_by_agent_session_id("ases-stable")
    wrong = h.registry.resolve_by_agent_session_id("ases-not-this-session")
    _check(
        correct is not None
        and correct.agent_instance_id == "agi-replacement"
        and wrong is None,
        "fixture precondition: real registry session filter matches X and rejects not-X",
    )
    status, payload = h.send(instance_id="agi-rotated-away", session_id="ases-stable")
    _check(
        status == 200
        and payload.get("delivered_to_agent_instance_id") == "agi-replacement",
        "L4b absent instance plus valid stable key delivers to the replacement",
    )


def test_l4c_unknown_session_key_preserves_the_original_error() -> None:
    h = _Harness()
    status, payload = h.send(
        instance_id="agi-never-registered", session_id="ases-unknown",
    )
    expected = (
        "peer_unreachable: no binding for "
        "'claude_code'/'agi-never-registered'"
    )
    _check(
        status == 404
        and payload.get("code") == "peer_unreachable"
        and payload.get("message") == expected
        and not h.service.requests,
        "L4c unknown stable key returns the original instance error without delivery",
    )


def test_l5_ambiguous_session_key_fails_with_candidate_ids() -> None:
    h = _Harness()
    h.add_target(instance_id="agi-dup-a", session_id="ases-dup", label="A")
    h.add_target(instance_id="agi-dup-b", session_id="ases-dup", label="B")
    status, payload = h.send(instance_id="agi-rotated-away", session_id="ases-dup")
    candidates = payload.get("candidate_instance_ids")
    _check(
        status == 404
        and payload.get("code") == "peer_unreachable"
        and set(candidates or []) == {"agi-dup-a", "agi-dup-b"}
        and "agi-dup-a" in str(payload.get("message"))
        and "agi-dup-b" in str(payload.get("message"))
        and not h.service.requests,
        "L5 ambiguous stable key is peer_unreachable with candidates and no delivery",
    )


def test_l6_hint_and_meta_keep_both_sender_keys() -> None:
    hint = build_wake_reply_hint(
        reply_to_role="",
        sender_agent_id="codex",
        sender_agent_instance_id="agi-sender",
        sender_agent_session_id="ases-sender",
        thread_id="agt-a2",
        message_id="agm-a2",
    )
    meta = build_peer_message_meta(
        sender_agent_id="codex",
        sender_agent_instance_id="agi-sender",
        sender_agent_session_id="ases-sender",
        sender_session_label="Sender",
        sender_parent_pid=None,
        sender_bridge_id="agc-sender",
        recipient_agent_id="claude_code",
        recipient_agent_instance_id="agi-target",
        thread_id="agt-a2",
        message_id="agm-a2",
        thread_cursor=1,
    )
    _check(
        "peer_agent_instance_id=agi-sender" in hint
        and "peer_agent_session_id=ases-sender" in hint
        and meta.get("from_agent_instance_id") == "agi-sender"
        and meta.get("from_agent_session_id") == "ases-sender",
        "L6 reply hint and event meta carry stable and instance keys together",
    )


def _tool_declares_instance_first_fallback(
    description: object, schema: dict[str, Any] | None,
) -> bool:
    text = str(description)
    properties = (schema or {}).get("properties", {})
    return (
        "exact instance FIRST" in text
        and "only after that instance is peer_unreachable" in text
        and "live instance" in text
        and "peer_agent_session_id" in properties
    )


def test_l7_both_tool_schemas_state_instance_first_order() -> None:
    stdio = next(tool for tool in STDIO_TOOLS if tool.name == "peer_send")
    streamable = next(tool for tool in STREAMABLE_TOOLS if tool.get("name") == "peer_send")
    _check(
        _tool_declares_instance_first_fallback(
            stdio.description, stdio.inputSchema,
        )
        and _tool_declares_instance_first_fallback(
            streamable.get("description"), streamable.get("inputSchema"),
        ),
        "L7 stdio and streamable schemas expose the key and teach instance-first fallback",
    )


def test_stdio_forwarder_carries_the_session_key_to_http() -> None:
    forwarder = object.__new__(Forwarder)
    forwarder._bridge_id = "agc-sender"  # noqa: SLF001
    captured: list[tuple[str, dict[str, Any]]] = []

    async def post(path: str, body: dict[str, Any]) -> dict[str, Any]:
        captured.append((path, body))
        return {"ok": True}

    async def call_with_reconnect(
        _operation: str, call: Any,
    ) -> dict[str, Any]:
        return await call()

    forwarder._post = post  # type: ignore[method-assign]  # noqa: SLF001
    forwarder._call_with_reconnect = call_with_reconnect  # type: ignore[method-assign]  # noqa: SLF001
    asyncio.run(
        forwarder.peer_send(
            peer_id="claude_code",
            peer_agent_instance_id="agi-old",
            peer_agent_session_id="ases-stable",
            content=[{"type": "text", "text": "reply"}],
        ),
    )
    _check(
        captured
        and captured[0][0] == "/api/v1/bridge/agc-sender/peer/send"
        and captured[0][1].get("peer_agent_session_id") == "ases-stable",
        "stdio forwarder preserves peer_agent_session_id on the HTTP body",
    )


def test_streamable_handler_threads_the_session_key_into_shared_dispatch() -> None:
    h = _Harness()
    h.add_target(
        instance_id="agi-stream-replacement",
        session_id="ases-stream-stable",
        label="Stream replacement",
    )
    session = SimpleNamespace(
        bridge_id=h.sender_bridge_id,
        agent_id="codex",
        agent_instance_id="agi-sender",
        session_label="Sender",
    )
    context = SimpleNamespace(
        bridge_manager=h.manager,
        peer_registry=h.registry,
        agent_messaging_service=h.service,
        state_service=None,
    )
    payload = _streamable_peer_send(
        {
            "peer_id": "claude_code",
            "peer_agent_instance_id": "agi-stream-rotated-away",
            "peer_agent_session_id": "ases-stream-stable",
            "content": [{"type": "text", "text": "stream reply"}],
        },
        session=session,  # type: ignore[arg-type]
        context=context,  # type: ignore[arg-type]
    )
    _check(
        payload.get("delivered_to_agent_instance_id") == "agi-stream-replacement",
        "streamable peer_send threads the stable key into shared fallback dispatch",
    )


def main() -> None:
    print("A2 sender-session reply fallback (HERMETIC, NOT LIVE)\n")
    for name, obj in sorted(globals().items()):
        if name.startswith("test_") and callable(obj):
            print(name)
            obj()
            print()
    print(f"{_passed} passed, {len(_failed)} failed")
    if _failed:
        for label in _failed:
            print(f"  FAILED: {label}")
        raise SystemExit(1)


if __name__ == "__main__":
    main()
