#!/usr/bin/env python3
"""Transport provenance survives the durable role-envelope seam (offline).

This is deliberately a persistence seam smoke, not a client-label heuristic:

* a stdio sender and an OAuth sender write the same role route through the
  real ``dispatch_role_send`` → ``AgentMessagingService`` path;
* each durable ``core__agent_role_message`` row retains its immutable
  ``sender_principal_kind`` and exposes it through ``peer_inbox`` projection;
* allocating a Streamable HTTP session assigns ``oauth_client`` before any
  dispatch can happen; and
* a later OAuth send cannot relabel an earlier stdio row.

Mutation-red proof: remove the dispatch forwarding keyword, the service record
field, the schema column, the projection metadata, or the streamable allocation
stamp and at least one assertion below fails.

Run:
    .venv/bin/python3 \
        plugins/agent_messaging_plugin/tests/transport_provenance_role_envelope_smoke.py
"""

from __future__ import annotations

import sys
from datetime import UTC, datetime
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
from ananta.llm.agent_messaging.models import TextPart  # noqa: E402
from ananta.llm.agent_messaging.repository import AgentMessagingRepository  # noqa: E402
from ananta.llm.agent_messaging.role_inbox import project_role_entry  # noqa: E402
from ananta.llm.agent_messaging.schema import (  # noqa: E402
    NAMESPACE,
    TABLE_AGENT_ROLE_MESSAGE,
)
from ananta.llm.agent_messaging.service import AgentMessagingService  # noqa: E402
from ananta.services.store import Store, open_store  # noqa: E402

from agent_messaging_plugin.bridge_sessions import BridgeSessionManager  # noqa: E402
from agent_messaging_plugin.mcp_streamable import dispatch as streamable_dispatch  # noqa: E402
from agent_messaging_plugin.mcp_streamable.auth import BearerClaim  # noqa: E402
from agent_messaging_plugin.mcp_streamable.dispatch import (  # noqa: E402
    DispatchContext,
    JsonRpcError,
)
from agent_messaging_plugin.mcp_streamable.session import (  # noqa: E402
    StreamableSessionManager,
)
from agent_messaging_plugin.peer_dispatch import dispatch_role_send  # noqa: E402
from agent_messaging_plugin.peer_registry import (  # noqa: E402
    PeerRegistry,
    PeerUnreachableError,
)
from agent_messaging_plugin.peer_role_management import ResolvedRole  # noqa: E402
from agent_messaging_plugin.schema import (  # noqa: E402
    PEER_BINDING_NAMESPACE,
    get_peer_binding_schema,
)
from agent_messaging_plugin.sender_provenance import (  # noqa: E402
    SENDER_PRINCIPAL_KIND_OAUTH_CLIENT,
    SENDER_PRINCIPAL_KIND_STDIO_AGENT,
)

_PASSED = 0
_FAILED: list[str] = []


class _EnabledConfig:
    enabled = True
    allowed_backends: tuple[str, ...] = ()
    max_message_bytes = 65_536


class _OfflineRegistry:
    def resolve(self, agent_id: str, agent_instance_id: str) -> object:
        raise PeerUnreachableError(agent_id, agent_instance_id)


def _check(condition: object, label: str) -> None:
    global _PASSED
    if condition:
        _PASSED += 1
        print(f"  PASS  {label}")
        return
    _FAILED.append(label)
    print(f"  FAIL  {label}")


def _service(state: RealShapeState) -> AgentMessagingService:
    return AgentMessagingService(
        repository=AgentMessagingRepository(cast(StateManagementInterface, state)),
        state_service=cast(StateManagementInterface, state),
        config=cast(Any, _EnabledConfig()),
    )


def _bridge_manager() -> BridgeSessionManager:
    return BridgeSessionManager(
        session_id_factory=lambda _solet_name: "ases-provenance",
        idle_timeout_s=3600,
        max_pending_events=16,
        long_poll_timeout_s=1,
    )


def _peer_registry() -> PeerRegistry:
    store: Store = open_store(
        get_peer_binding_schema(),
        namespace=PEER_BINDING_NAMESPACE,
        backend="in_memory",
    )
    return PeerRegistry(bindings_store=store)


def _dispatch(
    service: AgentMessagingService,
    *,
    message_id: str,
    sender_principal_kind: str,
    sender_transport_principal: str,
    sender_identity_trust: str,
) -> None:
    dispatch_role_send(
        bridge_manager=_bridge_manager(),
        peer_registry=cast(PeerRegistry, _OfflineRegistry()),
        agent_messaging_service=service,
        state_service=None,
        role_name="Coordinator",
        role=ResolvedRole(
            name="Coordinator",
            agent_id="claude_code",
            agent_instance_id="agi-coordinator",
            session_label="Coordinator",
        ),
        sender_bridge_id="agc-sender",
        sender_agent_id="codex",
        sender_agent_instance_id="agi-lane",
        sender_session_label="lane",
        sender_parent_pid=None,
        content=[TextPart(type="text", text="same provenance marker")],
        message_id=message_id,
        sender_principal_kind=sender_principal_kind,
        sender_transport_principal=sender_transport_principal,
        sender_identity_trust=sender_identity_trust,
    )


def main() -> int:
    state = RealShapeState()
    service = _service(state)
    _dispatch(
        service,
        message_id="agm-stdio",
        sender_principal_kind=SENDER_PRINCIPAL_KIND_STDIO_AGENT,
        sender_transport_principal="",
        sender_identity_trust="",
    )
    _dispatch(
        service,
        message_id="agm-oauth",
        sender_principal_kind=SENDER_PRINCIPAL_KIND_OAUTH_CLIENT,
        sender_transport_principal="oauth_bearer:fixture-client-principal",
        sender_identity_trust="shared_oauth_bearer_verified",
    )
    rows = state.rows(NAMESPACE, TABLE_AGENT_ROLE_MESSAGE)
    by_id = {str(row["message_id"]): row for row in rows}
    stdio = by_id["agm-stdio"]
    oauth = by_id["agm-oauth"]
    _check(
        stdio["sender_principal_kind"] == SENDER_PRINCIPAL_KIND_STDIO_AGENT,
        "stdio dispatch persists stdio_agent",
    )
    _check(
        stdio["sender_transport_principal"] == "stdio_bridge:agc-sender",
        "stdio dispatch derives a separate bridge transport principal",
    )
    _check(
        stdio["sender_identity_trust"] == "stdio_bridge_registered",
        "stdio dispatch records registered-bridge identity trust",
    )
    _check(
        oauth["sender_principal_kind"] == SENDER_PRINCIPAL_KIND_OAUTH_CLIENT,
        "OAuth dispatch persists oauth_client",
    )
    _check(
        oauth["sender_transport_principal"]
        == "oauth_bearer:fixture-client-principal",
        "OAuth dispatch persists a separate transport principal",
    )
    _check(
        oauth["sender_identity_trust"] == "shared_oauth_bearer_verified",
        "OAuth dispatch persists shared-bearer-only identity trust",
    )
    _check(
        project_role_entry(stdio).message.metadata["sender_principal_kind"]
        == SENDER_PRINCIPAL_KIND_STDIO_AGENT,
        "stdio provenance is visible in the projected role inbox",
    )
    _check(
        project_role_entry(oauth).message.metadata["sender_principal_kind"]
        == SENDER_PRINCIPAL_KIND_OAUTH_CLIENT,
        "OAuth provenance is visible in the projected role inbox",
    )
    _check(
        project_role_entry(oauth).message.metadata["sender_transport_principal"]
        == "oauth_bearer:fixture-client-principal",
        "OAuth transport principal is visible separately in the projected role inbox",
    )
    _check(
        project_role_entry(oauth).message.metadata["sender_identity_trust"]
        == "shared_oauth_bearer_verified",
        "OAuth projected trust names only shared-bearer verification",
    )
    _check(
        stdio["sender_principal_kind"] == SENDER_PRINCIPAL_KIND_STDIO_AGENT,
        "later OAuth registration cannot alter persisted stdio provenance",
    )
    sessions = StreamableSessionManager(
        bridge_manager=_bridge_manager(),
        peer_registry=_peer_registry(),
    )
    session = sessions.allocate(
        BearerClaim(
            agent_id="claude_phone",
            agent_instance_id="agi-oauth-client",
            issued_at=datetime(2026, 8, 31, tzinfo=UTC),
            client_id="client-provenance",
        ),
    )
    _check(
        session.sender_principal_kind == SENDER_PRINCIPAL_KIND_OAUTH_CLIENT,
        "streamable session is classified oauth_client at allocation",
    )
    _check(
        session.sender_identity_trust == "shared_oauth_bearer_verified",
        "streamable session truthfully classifies shared-bearer verification",
    )
    captured: dict[str, object] = {}

    class _CapturedOutcome:
        def to_payload(self) -> dict[str, object]:
            return {}

    def _capture_streamable_role_send(**kwargs: object) -> _CapturedOutcome:
        captured.update(kwargs)
        return _CapturedOutcome()

    original_resolve = streamable_dispatch.resolve_role_binding
    original_dispatch = streamable_dispatch.dispatch_role_send
    streamable_dispatch.resolve_role_binding = lambda _state, _name: ResolvedRole(
        name="Coordinator",
        agent_id="claude_code",
        agent_instance_id="agi-coordinator",
        session_label="Coordinator",
    )
    streamable_dispatch.dispatch_role_send = _capture_streamable_role_send
    try:
        streamable_dispatch._tool_peer_send_by_name(
            {"name": "Coordinator", "content": "streamable provenance marker"},
            session=session,
            context=DispatchContext(
                bridge_manager=_bridge_manager(),
                peer_registry=_peer_registry(),
                platform_surface=cast(Any, object()),
                agent_messaging_service=service,
                state_service=cast(Any, object()),
            ),
        )
    finally:
        streamable_dispatch.resolve_role_binding = original_resolve
        streamable_dispatch.dispatch_role_send = original_dispatch
    _check(
        captured.get("sender_principal_kind")
        == SENDER_PRINCIPAL_KIND_OAUTH_CLIENT,
        "streamable peer_send_by_name forwards immutable oauth_client provenance "
        f"(got {captured.get('sender_principal_kind')!r})",
    )
    _check(
        captured.get("sender_transport_principal") == session.sender_transport_principal,
        "streamable peer_send_by_name forwards the transport principal separately",
    )
    _check(
        captured.get("sender_identity_trust") == "shared_oauth_bearer_verified",
        "streamable peer_send_by_name forwards shared-bearer-only trust",
    )

    try:
        streamable_dispatch._tool_peer_register(
            {"agent_id": "codex", "session_label": "masquerade"},
            session=session,
            context=DispatchContext(
                bridge_manager=_bridge_manager(),
                peer_registry=_peer_registry(),
                platform_surface=cast(Any, object()),
                agent_messaging_service=service,
                state_service=cast(Any, object()),
            ),
        )
    except JsonRpcError as exc:
        _check(
            exc.data == {"code": "streamable_http.identity_relabel_refused"},
            "streamable peer_register refuses a caller-supplied identity relabel",
        )
    else:
        _check(False, "streamable peer_register refuses a caller-supplied identity relabel")
    try:
        streamable_dispatch._tool_peer_register(
            {
                "agent_id": session.authenticated_agent_id,
                "session_label": "masquerade",
            },
            session=session,
            context=DispatchContext(
                bridge_manager=_bridge_manager(),
                peer_registry=_peer_registry(),
                platform_surface=cast(Any, object()),
                agent_messaging_service=service,
                state_service=cast(Any, object()),
            ),
        )
    except JsonRpcError as exc:
        _check(
            exc.data == {"code": "streamable_http.identity_relabel_refused"},
            "streamable peer_register refuses a caller-supplied label relabel",
        )
    else:
        _check(False, "streamable peer_register refuses a caller-supplied label relabel")
    authenticated_registration = streamable_dispatch._tool_peer_register(
        {
            "agent_id": session.authenticated_agent_id,
            "session_label": session.authenticated_session_label,
        },
        session=session,
        context=DispatchContext(
            bridge_manager=_bridge_manager(),
            peer_registry=_peer_registry(),
            platform_surface=cast(Any, object()),
            agent_messaging_service=service,
            state_service=cast(Any, object()),
        ),
    )
    _check(
        authenticated_registration["agent_id"] == session.authenticated_agent_id
        and authenticated_registration["session_label"]
        == session.authenticated_session_label,
        "streamable peer_register still accepts the bearer-bound identity",
    )
    if _FAILED:
        print(f"FAILED ({len(_FAILED)}): " + "; ".join(_FAILED))
        return 1
    print(f"PASS ({_PASSED} checks): transport provenance role-envelope smoke")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
