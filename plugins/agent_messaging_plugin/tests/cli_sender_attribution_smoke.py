#!/usr/bin/env python3
"""§34.6 — CLI-originated sends carry the caller's identity (no pytest, no DB).

Every message sent through the local CLI arrived attributed to
``System (Scheduler)``: the CLI opens a ONE-SHOT bridge and deliberately never
registers a peer identity, so ``bridge.agent_instance_id`` was empty, the flow's
``inference_vertex_*`` tags were empty, and the sender ladder fell to its
scheduler sentinel. Recipients could not tell who wrote a message and the
auto-generated reply hint pointed at ``system``, which reaches nobody.

The fix does NOT register the caller. Registering this bridge would be actively
destructive — ``PeerRegistry.register`` hard-deletes any row sharing the incoming
``session_label`` (the single-active-session-per-name invariant) and
``close_bridge`` unregisters by ``bridge_id``, so an ordinary ``solet call``
would evict its own session's registry row and never restore it. Instead the
caller supplies its opaque launcher-exported ``agent_session_id`` and the SERVER
derives the identity from the registered binding that key resolves to: the
caller names a routing key, the registry binds the content.

Drives the REAL chain, one seam per section, no reimplementations:

  1. ``bridge/open`` route (real FastAPI + real ``PeerRegistry`` over an
     in-memory store) parks the key and writes NOTHING to the registry;
  2. ``PlatformSurface._resolve_caller_attribution`` derives identity from the
     registry, and ``_build_process_call_trigger_data`` stamps it under a key
     family SEPARATE from ``inference_vertex_*``;
  3. ``ActionProcessor._lift_inference_vertex_identity`` (the real method) lifts
     it into the handler ``state``;
  4. ``_resolve_role_send_sender`` (the real ladder) stamps an honest sender;
  5. ``send_peer_message`` (the real verb body) does the same — it used to
     hardcode the sentinel on EVERY transport, including a registered MCP
     caller, so this leg fails even with the whole CLI plumbing present.

Negative controls carried here because they are what keeps the design honest:
the registry is byte-identical across an attributed call; a REGISTERED bridge
ignores attribution entirely; an unresolvable or ambiguous key degrades to the
sentinel rather than promoting an unverifiable claim; and
``inference_vertex_session_id`` stays empty so an attributed call can never
re-point the flow's inference vertex at the attributed session.

Run:
    SOLET_NAME=<name>-test .venv/bin/python3 \
        plugins/agent_messaging_plugin/tests/cli_sender_attribution_smoke.py
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, cast

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT / "ananta" / "src"))
sys.path.insert(0, str(REPO_ROOT / "plugins" / "agent_messaging_plugin" / "src"))

from ananta.core.actions.action_processor import ActionProcessor  # noqa: E402
from ananta.services.store import Store, open_store  # noqa: E402
from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from agent_messaging_plugin.bridge_sessions import BridgeSessionManager  # noqa: E402
from agent_messaging_plugin.http_routes import API_PREFIX, register_routes  # noqa: E402
from agent_messaging_plugin.models import BridgeBinding, BridgeSessionState  # noqa: E402
from agent_messaging_plugin.peer_registry import (  # noqa: E402
    PeerRegistry,
    PeerSessionAmbiguousError,
)
from agent_messaging_plugin.platform_surface import (  # noqa: E402
    CallerAttribution,
    PlatformSurface,
)
from agent_messaging_plugin.plugin import (  # noqa: E402
    AgentMessagingPlugin,
    _resolve_role_send_sender,
)
from agent_messaging_plugin.schema import (  # noqa: E402
    PEER_BINDING_NAMESPACE,
    get_peer_binding_schema,
)

# The live session doing the sending: registered over its own transport, holding
# a role. The CLI command it runs inherits ONLY the opaque session id.
_LIVE_AGENT_ID = "claude_code"
_LIVE_INSTANCE = "agi-live-holder"
_LIVE_LABEL = "Claude-C"
_LIVE_SESSION_ID = "ases-live-1785431766"
_LIVE_ROLE = "Claude-C"

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


# ---------------------------------------------------------------------------
# Harness — real registry over an in-memory store; real-shape state service
# ---------------------------------------------------------------------------


def _registry_with_live_session() -> PeerRegistry:
    """A real ``PeerRegistry`` holding the sending session's live binding."""
    store: Store = open_store(
        get_peer_binding_schema(),
        namespace=PEER_BINDING_NAMESPACE,
        backend="in_memory",
    )
    registry = PeerRegistry(bindings_store=store)
    registry.register(
        BridgeBinding(
            bridge_id="agc-live-bridge",
            agent_id=_LIVE_AGENT_ID,
            agent_instance_id=_LIVE_INSTANCE,
            session_label=_LIVE_LABEL,
            parent_pid=4242,
            agent_session_id=_LIVE_SESSION_ID,
        ),
    )
    return registry


def _registry_snapshot(registry: PeerRegistry) -> list[tuple[str, ...]]:
    """Comparable snapshot of every binding — the negative-control fixture."""
    return sorted(
        (
            binding.agent_id,
            binding.agent_instance_id,
            binding.session_label,
            binding.bridge_id,
            binding.agent_session_id,
        )
        for bindings in registry.list_agent_ids().values()
        for binding in bindings
    )


class _RoleStateService:
    """Real-SHAPE state service answering the reverse role lookup.

    ``list_roles_for_agent_instance`` reads ``data.records`` off a completed
    ``query_state`` envelope, so the fake returns that exact shape rather than a
    convenient one — a fake with a flatter envelope would read zero roles and
    mask a real extraction break.
    """

    def __init__(self, roles_by_instance: dict[str, list[str]]) -> None:
        self._roles = roles_by_instance

    def query_state(
        self, _namespace: str, query: dict[str, Any],
    ) -> dict[str, Any]:
        filters = query.get("filters", {})
        instance = str(filters.get("agent_instance_id", ""))
        return {
            "action_status": "completed",
            "data": {
                "records": [
                    {"role": role} for role in self._roles.get(instance, [])
                ],
            },
        }


class _RaisingRegistry:
    """A registry whose session-id resolution always raises (degrade path)."""

    def resolve_by_agent_session_id(self, agent_session_id: str) -> None:
        raise PeerSessionAmbiguousError(agent_session_id, ["agi-a", "agi-b"])


def _surface(
    peer_registry: object, state_service: object | None = None,
) -> PlatformSurface:
    return PlatformSurface(
        action_factory=cast(Any, object()),
        flow_manager=cast(Any, object()),
        compilation_context_builder=cast(Any, object()),
        bridge_manager=cast(Any, object()),
        peer_registry=cast(Any, peer_registry),
        state_service=cast(
            Any,
            state_service
            if state_service is not None
            else _RoleStateService({_LIVE_INSTANCE: [_LIVE_ROLE]}),
        ),
    )


def _cli_bridge(caller_agent_session_id: str) -> BridgeSessionState:
    """A bridge shaped like the CLI's: opened, never registered."""
    return BridgeSessionState(
        bridge_id="agc-cli-oneshot",
        session_id="ags-cli",
        caller_agent_session_id=caller_agent_session_id,
    )


# ---------------------------------------------------------------------------
# 1 — the bridge/open route parks the key and writes NOTHING to the registry
# ---------------------------------------------------------------------------


def _app_with(
    registry: PeerRegistry, manager: BridgeSessionManager,
) -> FastAPI:
    app = FastAPI()
    register_routes(
        app,
        bridge_manager=manager,
        peer_registry=registry,
        platform_surface=cast(Any, object()),
        agent_messaging_service=cast(Any, object()),
        config={"long_poll_timeout_seconds": 1},
        state_service=cast(Any, object()),
    )
    return app


def test_open_route_parks_key_without_registering() -> None:
    registry = _registry_with_live_session()
    manager = BridgeSessionManager(
        session_id_factory=lambda _n: "ags-cli",
        idle_timeout_s=3600,
        max_pending_events=50,
        long_poll_timeout_s=1,
    )
    before = _registry_snapshot(registry)
    with TestClient(_app_with(registry, manager)) as http:
        response = http.post(
            f"{API_PREFIX}/open",
            json={"parent_pid": 999, "caller_agent_session_id": _LIVE_SESSION_ID},
        )
        _check(response.status_code == 200, "open: route accepted the attribution key")
        bridge_id = response.json()["bridge_id"]
        bridge = manager.get(bridge_id)
        _check(
            bridge is not None
            and bridge.caller_agent_session_id == _LIVE_SESSION_ID,
            "open: the opaque session key is parked on the in-memory bridge state",
        )
        _check(
            bridge is not None and not bridge.agent_instance_id,
            "open: attribution does NOT set agent_instance_id (no delivery route)",
        )
    _check(
        _registry_snapshot(registry) == before,
        "open NEGATIVE CONTROL: registry byte-identical — the caller's own row "
        "survives (the rejected 'CLI registers' design evicts it here)",
    )


def test_open_route_without_key_is_unchanged() -> None:
    registry = _registry_with_live_session()
    manager = BridgeSessionManager(
        session_id_factory=lambda _n: "ags-cli",
        idle_timeout_s=3600,
        max_pending_events=50,
        long_poll_timeout_s=1,
    )
    with TestClient(_app_with(registry, manager)) as http:
        response = http.post(f"{API_PREFIX}/open", json={"parent_pid": 1})
        bridge = manager.get(response.json()["bridge_id"])
    _check(
        bridge is not None and bridge.caller_agent_session_id == "",
        "open: a caller that asserts no key carries none (nothing is invented)",
    )


# ---------------------------------------------------------------------------
# 2 — the surface derives identity from the REGISTRY, under its own key family
# ---------------------------------------------------------------------------


def test_attribution_resolved_from_registry() -> None:
    attribution = _surface(_registry_with_live_session())._resolve_caller_attribution(
        _cli_bridge(_LIVE_SESSION_ID),
    )
    _check(
        attribution.agent_id == _LIVE_AGENT_ID
        and attribution.agent_instance_id == _LIVE_INSTANCE
        and attribution.session_label == _LIVE_LABEL,
        "attribution: identity read out of the registered binding, not the request",
    )
    _check(
        attribution.role == _LIVE_ROLE,
        "attribution: the caller's DURABLE role is resolved too (reconnect-surviving)",
    )


_WATCH_AGENT_ID = "claude_code"
_WATCH_INSTANCE = "agi-watch-livecaller00000000000000000001"
_WATCH_LABEL = "Watch-Registered-Caller"
_WATCH_SESSION_ID = "ases-watch-1785431900"
_WATCH_ROLE = "Watch-Registered-Caller"


def _registry_with_live_watch_session() -> PeerRegistry:
    """Same shape as :func:`_registry_with_live_session`, but for a session
    registered over the WATCH transport (an ``agi-watch-``-prefixed
    instance id, matching the rename skill's watcher-path claim result --
    see e.g. ``agi-watch-92a6ae0e3e134e5e11774007`` observed live,
    fleet-watch-transport-migration phase 1). ``BridgeBinding`` carries no
    transport field at all -- this fixture exists to make that fact an
    explicit, pinned assertion rather than something inferred from the
    generic MCP-shaped fixture never mentioning transport."""
    store: Store = open_store(
        get_peer_binding_schema(),
        namespace=PEER_BINDING_NAMESPACE,
        backend="in_memory",
    )
    registry = PeerRegistry(bindings_store=store)
    registry.register(
        BridgeBinding(
            bridge_id="agc-watch-bridge",
            agent_id=_WATCH_AGENT_ID,
            agent_instance_id=_WATCH_INSTANCE,
            session_label=_WATCH_LABEL,
            parent_pid=5252,
            agent_session_id=_WATCH_SESSION_ID,
        ),
    )
    return registry


def test_attribution_resolved_from_registry_for_watch_registered_caller() -> None:
    """fleet-watch-transport-migration phase 2 slice 1 (2026-08-06): the
    CLI-send identity path -- verified, not assumed -- for a WATCH-
    registered caller. resolve_by_agent_session_id keys purely on
    agent_session_id; a watch-registered session's binding resolves
    exactly like an MCP-registered one, because the identity-drop this
    mechanism guards against applies only to a genuinely UNREGISTERED
    caller (the bare one-shot CLI bridge), never to a caller registered
    over a different transport."""
    surface = _surface(
        _registry_with_live_watch_session(),
        _RoleStateService({_WATCH_INSTANCE: [_WATCH_ROLE]}),
    )
    attribution = surface._resolve_caller_attribution(_cli_bridge(_WATCH_SESSION_ID))
    _check(
        attribution.agent_id == _WATCH_AGENT_ID
        and attribution.agent_instance_id == _WATCH_INSTANCE
        and attribution.session_label == _WATCH_LABEL,
        "attribution: a watch-registered caller's identity resolves out of the "
        "registered binding exactly like an MCP-registered caller's does",
    )
    _check(
        attribution.role == _WATCH_ROLE,
        "attribution: the watch-registered caller's durable role resolves too",
    )


def test_attribution_degrades_to_empty() -> None:
    surface = _surface(_registry_with_live_session())
    unknown = surface._resolve_caller_attribution(_cli_bridge("ases-never-registered"))
    _check(
        unknown == CallerAttribution(),
        "attribution: an unresolvable key yields NOTHING — an unverifiable claim "
        "is never promoted",
    )
    registered = _cli_bridge(_LIVE_SESSION_ID)
    registered.agent_instance_id = "agi-its-own"
    _check(
        surface._resolve_caller_attribution(registered) == CallerAttribution(),
        "attribution: a REGISTERED bridge ignores the key — its own identity is "
        "strictly better evidence",
    )


def test_attribution_survives_registry_fault() -> None:
    raised = False
    try:
        attribution = _surface(_RaisingRegistry())._resolve_caller_attribution(
            _cli_bridge(_LIVE_SESSION_ID),
        )
    except Exception:  # noqa: BLE001 — the whole point is it must NOT propagate
        raised = True
        attribution = CallerAttribution()
    _check(
        not raised,
        "attribution: an ambiguous/faulted lookup does NOT raise — this runs "
        "outside the caller's error envelope on EVERY process_call",
    )
    _check(
        attribution == CallerAttribution(),
        "attribution: a faulted lookup degrades to unattributed (sentinel follows)",
    )


def test_trigger_data_keeps_inference_vertex_empty() -> None:
    bridge = _cli_bridge(_LIVE_SESSION_ID)
    surface = _surface(_registry_with_live_session())
    trigger = PlatformSurface._build_process_call_trigger_data(
        bridge=bridge,
        process_key="plugin::agent_messaging_plugin::peer_send_by_name",
        reason="cli send",
        inference_vertex_role="",
        operator_equivalent=False,
        caller_attribution=surface._resolve_caller_attribution(bridge),
    )
    _check(
        trigger.get("caller_attribution_instance_id") == _LIVE_INSTANCE
        and trigger.get("caller_attribution_role") == _LIVE_ROLE,
        "trigger_data: attribution stamped under caller_attribution_*",
    )
    _check(
        trigger.get("inference_vertex_session_id") == ""
        and trigger.get("inference_vertex_role") == "",
        "trigger_data: inference_vertex_* stays EMPTY — an attributed CLI call "
        "can never re-point the flow's inference vertex at the attributed session",
    )


# ---------------------------------------------------------------------------
# 3 — the real lift carries the family into the handler state
# ---------------------------------------------------------------------------


class _FakeAction:
    """Minimal QueuedAction-shaped stub carrying only what the lift reads."""

    def __init__(self, flow_id: str) -> None:
        self.flow_id = flow_id


class _LiftProc(ActionProcessor):
    """ActionProcessor with a stubbed flow lookup — exercises the REAL lift."""

    def __init__(self, trigger_data: dict[str, Any] | None) -> None:
        self._trigger_data = trigger_data

    def _get_flow_trigger_data(self, flow_id: str) -> dict[str, Any] | None:  # noqa: ARG002
        return self._trigger_data


def _lift(trigger: dict[str, Any]) -> dict[str, object]:
    state: dict[str, object] = {"session_id": "s", "flow_id": "f"}
    _LiftProc(trigger)._lift_inference_vertex_identity(  # type: ignore[arg-type]
        state, _FakeAction("f"),
    )
    return state


def test_lift_carries_attribution_family() -> None:
    state = _lift(
        {
            "caller_attribution_agent_id": _LIVE_AGENT_ID,
            "caller_attribution_instance_id": _LIVE_INSTANCE,
            "caller_attribution_label": _LIVE_LABEL,
            "caller_attribution_role": _LIVE_ROLE,
        },
    )
    _check(
        state.get("caller_attribution_role") == _LIVE_ROLE
        and state.get("caller_attribution_instance_id") == _LIVE_INSTANCE
        and state.get("caller_attribution_agent_id") == _LIVE_AGENT_ID
        and state.get("caller_attribution_label") == _LIVE_LABEL,
        "lift: all four caller_attribution_* keys reach the handler state",
    )
    _check(
        _lift({"authenticated_principal": {"client_id": "x"}}) == {
            "session_id": "s", "flow_id": "f",
        },
        "lift guard: an unattributed flow adds no keys (degrade-silent preserved)",
    )


# ---------------------------------------------------------------------------
# 4 + 5 — the ladder and BOTH send verbs stamp the honest sender
# ---------------------------------------------------------------------------


_ATTRIBUTED_STATE: dict[str, Any] = {
    "caller_attribution_agent_id": _LIVE_AGENT_ID,
    "caller_attribution_instance_id": _LIVE_INSTANCE,
    "caller_attribution_label": _LIVE_LABEL,
    "caller_attribution_role": _LIVE_ROLE,
}


class _NoRoleStateService:
    """State service whose role resolution finds nothing (instance-rung path).

    ``query_state`` is the minimal real-provider shape (no records) — the
    drive-on-delivery seam (2026-08-04) now reads ``managed_session`` off
    whatever ``state_service`` the sender resolves, and this double must
    answer "no managed_session row" honestly rather than crash with an
    AttributeError (widened-interface trap: a fake standing in for a
    narrower old contract must widen alongside it, same-commit)."""

    def query_state(self, namespace: str, query: dict[str, Any]) -> dict[str, Any]:
        del namespace, query
        return {"action_status": "completed", "data": {"records": [], "count": 0}}


def test_ladder_prefers_attributed_role() -> None:
    sender = _resolve_role_send_sender(
        dict(_ATTRIBUTED_STATE), _NoRoleStateService(),
    )
    _check(
        sender.reply_to_role == _LIVE_ROLE,
        "ladder: an attributed caller's ROLE becomes the durable reply-to "
        "(a reply now reaches the sender, not 'system')",
    )
    _check(
        sender.agent_instance_id == _LIVE_INSTANCE
        and sender.agent_id != "system",
        "ladder: sender stamped with the attributed identity, NOT the sentinel",
    )


def test_ladder_attributed_instance_without_role() -> None:
    state = dict(_ATTRIBUTED_STATE)
    state["caller_attribution_role"] = ""
    sender = _resolve_role_send_sender(state, _NoRoleStateService())
    _check(
        sender.agent_id == _LIVE_AGENT_ID
        and sender.agent_instance_id == _LIVE_INSTANCE
        and sender.session_label == _LIVE_LABEL,
        "ladder: a roleless attributed caller is still honestly labelled",
    )


def test_ladder_registered_caller_and_sentinel_unchanged() -> None:
    both = dict(_ATTRIBUTED_STATE)
    both["inference_vertex_session_id"] = "agi-mcp-origin"
    sender = _resolve_role_send_sender(both, _NoRoleStateService())
    _check(
        sender.agent_instance_id == "agi-mcp-origin",
        "ladder: a REGISTERED caller's own identity still outranks attribution",
    )
    _check(
        _resolve_role_send_sender({}, _NoRoleStateService()).agent_instance_id
        == "system:scheduler",
        "ladder: a genuine scheduler-originated send still stamps the sentinel",
    )


def test_peer_identity_reports_one_shot_attribution() -> None:
    """The process reads the instance attribution field lifted into state."""
    plugin = object.__new__(AgentMessagingPlugin)
    result = plugin.peer_identity({}, {"caller_attribution_instance_id": _LIVE_INSTANCE})
    data = result.get("data")
    _check(
        isinstance(data, dict)
        and data.get("caller_identity_available") is True
        and data.get("registered_bridge") is False
        and data.get("bridge_identity") == "one_shot_attributed",
        "peer_identity: one-shot caller attribution reports its available identity",
    )


class _CapturingService:
    """Captures the PeerSendRequest ``send_peer_message`` hands the service."""

    def __init__(self) -> None:
        self.request: Any = None

    def peer_send(self, request: Any) -> Any:
        self.request = request

        class _Result:
            thread_id = "th-1"
            message_id = "msg-1"
            cursor = 1

        return _Result()


_UNSET = object()


def _send_peer_message(
    state: dict[str, Any],
    state_service: object = _UNSET,
    *,
    recipient_instance_id: str = "agi-recipient",
    recipient_session_id: str = "ases-recipient",
    recipient_is_watcher: bool = False,
    requested_instance_id: str = "agi-recipient",
    requested_session_id: str | None = None,
) -> Any:
    """Drive the REAL ``send_peer_message`` body against a captured service."""
    registry = _registry_with_live_session()
    bridge_manager = BridgeSessionManager(
        session_id_factory=lambda _n: "ags-x",
        idle_timeout_s=3600,
        max_pending_events=50,
        long_poll_timeout_s=1,
    )
    # A4 Amendment 5: binding_is_live is now checked for every recipient, so
    # the recipient needs a REAL opened bridge (last_seen_at fresh at open),
    # not just a registered binding pointing at an id nothing ever opened.
    recipient_bridge_id = bridge_manager.open(
        solet_name="test", parent_pid=None,
    ).bridge_id
    registry.register(
        BridgeBinding(
            bridge_id=recipient_bridge_id,
            agent_id="codex",
            agent_instance_id=recipient_instance_id,
            session_label="Recipient",
            parent_pid=None,
            agent_session_id=recipient_session_id,
            watcher_declared=recipient_is_watcher,
        ),
    )
    service = _CapturingService()
    plugin = object.__new__(AgentMessagingPlugin)
    plugin._peer_registry = registry
    plugin._bridge_manager = bridge_manager
    plugin._require_service = lambda: service
    resolved = _NoRoleStateService() if state_service is _UNSET else state_service
    plugin._get_state_service = lambda: resolved
    params: dict[str, Any] = {
        "peer_id": "codex",
        "peer_agent_instance_id": requested_instance_id,
        "content": "hello",
    }
    if requested_session_id is not None:
        params["peer_agent_session_id"] = requested_session_id
    result = plugin.send_peer_message(params, state)
    _check(
        result.get("action_status") == "completed",
        "send_peer_message: the send still succeeds",
    )
    return service.request


def test_send_peer_message_stamps_attributed_sender() -> None:
    request = _send_peer_message(dict(_ATTRIBUTED_STATE))
    _check(
        request is not None and request.sender_agent_id == _LIVE_AGENT_ID,
        "send_peer_message: sender_agent_id is the caller, not 'system' — this "
        "verb hardcoded the sentinel on EVERY transport, MCP included",
    )
    _check(
        request is not None
        and request.sender_agent_instance_id == _LIVE_INSTANCE
        and request.sender_session_label == _LIVE_LABEL,
        "send_peer_message: instance + label carry the caller's identity",
    )
    _check(
        request is not None and request.sender_bridge_id == "system:scheduler",
        "send_peer_message: sender_bridge_id UNCHANGED — peer threads are keyed "
        "on it, so only the identity triple moves",
    )


def test_send_peer_message_survives_unbound_state_service() -> None:
    """``_get_state_service()`` can return None — the role rung must not raise.

    ``send_peer_message`` did not consult ``state`` at all before §34.6, so this
    path is new: an attributed caller with a role sends ``None`` into
    ``resolve_role_binding``. The degrade must happen inside ``_sender_from_role``'s
    best-effort guard, not as an exception out of the verb.

    Mutation that turns this red: narrow ``_sender_from_role``'s ``except`` to a
    specific exception type that does not cover the ``None`` attribute access.
    """
    raised = ""
    try:
        request = _send_peer_message(dict(_ATTRIBUTED_STATE), state_service=None)
    except Exception as exc:  # noqa: BLE001 — the point is that it must NOT propagate
        raised = f"{type(exc).__name__}: {exc}"
        request = None
    _check(not raised, f"send_peer_message: unbound state service does not raise ({raised})")
    _check(
        request is not None
        and request.sender_agent_instance_id == _LIVE_INSTANCE
        and request.sender_agent_id == _LIVE_AGENT_ID,
        "send_peer_message: with no state service the attributed identity still "
        "stands (the registry already resolved it; only the role re-read fails)",
    )


def test_send_peer_message_sentinel_when_unattributed() -> None:
    request = _send_peer_message({})
    _check(
        request is not None
        and request.sender_agent_id == "system"
        and request.sender_agent_instance_id == "system:scheduler",
        "send_peer_message: a genuinely scheduler-originated send is still "
        "honestly the sentinel (no regression)",
    )


def test_send_peer_message_reaches_no_claim_watcher_by_stable_session() -> None:
    request = _send_peer_message(
        dict(_ATTRIBUTED_STATE),
        recipient_instance_id="agi-watch-codex-lane",
        recipient_session_id="ases-codex-lane",
        recipient_is_watcher=True,
        requested_instance_id="agi-ledger-codex-lane",
        requested_session_id="ases-codex-lane",
    )
    _check(
        request is not None
        and request.peer_agent_instance_id == "agi-watch-codex-lane"
        and request.peer_agent_session_id == "ases-codex-lane",
        "send_peer_message: stable-session fallback reaches a no-claim watcher binding",
    )
    metadata = getattr(
        AgentMessagingPlugin.send_peer_message,
        "_platform_process_metadata",
        None,
    )
    parameters = getattr(metadata, "parameters", {})
    _check(
        "peer_agent_session_id" in parameters,
        "send_peer_message: process schema exposes the watcher-safe session fallback",
    )


def main() -> None:
    print("§34.6 — CLI sender identity attribution")
    for name, obj in sorted(globals().items()):
        if name.startswith("test_") and callable(obj):
            print(f"\n{name}")
            obj()
    print(f"\n{_passed} passed, {len(_failed)} failed")
    if _failed:
        for label in _failed:
            print(f"  FAILED: {label}")
        sys.exit(1)


if __name__ == "__main__":
    main()
