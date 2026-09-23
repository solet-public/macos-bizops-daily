#!/usr/bin/env python3
"""Cycle B (D-IF7/D-IF8/D-IF11) smoke for coding-agent inference v4.

Covers the v4 design's 8 acceptance cases:

1. SessionInferenceProvider.process_error emits a bridge_delivery_error event.
2. SessionInferenceProvider.process_results emits a bridge_delivery_result event.
3. SessionInferenceProvider.propose_name raises NotImplementedError per v4 §2.2.
4. _register_inference_provider populates the sidecar.
5. _clear_inference_providers_for_bridge clears the sidecar via list_by_bridge.
6. Re-register replaces the prior sidecar entry without leak.
7. _build_process_call_trigger_data tags inference_vertex_session_id.
8. get_inference_provider returns None after unregister (default fallback path).

Plus the INF-01 bridge-open gate: a live provider on an OPEN bridge is
returned (resolver routes PROVIDER); a stale provider on a CLOSED or SWEPT
(popped-from-_bridges) bridge returns None so the resolver DEFERs instead of
letting append_event raise BridgeNotFoundError.

Run:

    SOLET_NAME=<name> .venv/bin/python3 \
        plugins/agent_messaging_plugin/tests/session_inference_provider_smoke.py
"""

from __future__ import annotations

import json
import logging
import sys
import threading
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT / "plugins" / "agent_messaging_plugin" / "src"))
sys.path.insert(0, str(REPO_ROOT / "ananta" / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

# AgentMessagingPlugin module-level init resolves a scoped vault key from
# SOLET_NAME and therefore fails closed when unset.

from _real_state_fake import RealShapeState  # noqa: E402
from ananta.llm.agent_messaging.role_binding import (  # noqa: E402
    HOLDER_KIND_SESSION,
    SYS_AUTONOMIC_SLOT,
)

from agent_messaging_plugin.role_binding_store import (  # noqa: E402
    HolderClaim,
    claim_role_binding_v4,
)
from agent_messaging_plugin.session_inference_provider import (  # noqa: E402
    SessionInferenceProvider,
)

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


@dataclass
class _RecordedEvent:
    bridge_id: str
    event_type: str
    content: str
    meta: dict[str, object]


@dataclass
class _StubBridge:
    """Minimal stand-in for BridgeSessionState — the gate reads ``.closed``."""

    closed: bool = False


class _StubBridgeManager:
    """Records every append_event call; serves bridge-open state for the gate.

    ``get`` models ``BridgeSessionManager.get``: a registered provider's bridge
    is OPEN by default (mirrors reality — the bridge exists before the inference
    provider is registered), unless a test explicitly closes it (``close_bridge``
    — the ``.closed`` flag path) or drops it (``drop_bridge`` — the popped-from-
    ``_bridges`` path, which is what ``sweep_idle``/``close`` actually do).
    """

    def __init__(self) -> None:
        self.events: list[_RecordedEvent] = []
        self._closed: set[str] = set()
        self._dropped: set[str] = set()

    def append_event(
        self,
        bridge_id: str,
        event_type: str,
        content: str,
        meta: dict[str, object] | None = None,
    ) -> object:
        self.events.append(
            _RecordedEvent(
                bridge_id=bridge_id,
                event_type=event_type,
                content=content,
                meta=dict(meta or {}),
            ),
        )
        return None

    def get(self, bridge_id: str) -> _StubBridge | None:
        if bridge_id in self._dropped:
            return None
        return _StubBridge(closed=bridge_id in self._closed)

    def close_bridge(self, bridge_id: str) -> None:
        self._closed.add(bridge_id)

    def drop_bridge(self, bridge_id: str) -> None:
        self._dropped.add(bridge_id)


# ─── Test 1: process_error emits bridge_delivery_error ──────────────────


def test_process_error_emits_bridge_delivery_error() -> None:
    mgr = _StubBridgeManager()
    provider = SessionInferenceProvider(
        bridge_id="agc-1",
        agent_instance_id="agi-A",
        agent_id="claude_code",
        session_label="Test-Session",
        bridge_manager=mgr,  # type: ignore[arg-type]
    )
    outcome = provider.process_error({"error": "boom"}, {"flow_id": "flow-1"})

    _check(
        outcome.get("action_status") == "completed",
        "process_error returns action_status=completed",
    )
    _check(
        len(mgr.events) == 1,
        f"process_error appends exactly one bridge event (got {len(mgr.events)})",
    )
    if mgr.events:
        evt = mgr.events[0]
        _check(
            evt.event_type == "bridge_delivery_error",
            f"event_type is bridge_delivery_error (got {evt.event_type})",
        )
        _check(
            evt.bridge_id == "agc-1",
            "bridge_id propagated from provider",
        )
        _check(
            evt.meta.get("delivered_to_vertex") == "agi-A",
            f"meta.delivered_to_vertex = agi-A (got {evt.meta.get('delivered_to_vertex')!r})",
        )
        decoded = json.loads(evt.content)
        _check(
            decoded.get("params") == {"error": "boom"},
            "content.params carries forward",
        )


# ─── Test 2: process_results emits bridge_delivery_result ───────────────


def test_process_results_emits_bridge_delivery_result() -> None:
    mgr = _StubBridgeManager()
    provider = SessionInferenceProvider(
        bridge_id="agc-2",
        agent_instance_id="agi-B",
        agent_id="claude_code",
        session_label=None,
        bridge_manager=mgr,  # type: ignore[arg-type]
    )
    outcome = provider.process_results({"result": "ok"}, {})

    _check(
        outcome.get("action_status") == "completed",
        "process_results returns action_status=completed",
    )
    if mgr.events:
        evt = mgr.events[0]
        _check(
            evt.event_type == "bridge_delivery_result",
            f"event_type is bridge_delivery_result (got {evt.event_type})",
        )


# ─── Test 3: propose_name raises NotImplementedError per v4 §2.2 ────────


def test_propose_name_raises_not_implemented() -> None:
    provider = SessionInferenceProvider(
        bridge_id="agc-3",
        agent_instance_id="agi-C",
        agent_id="claude_code",
        session_label="X",
        bridge_manager=_StubBridgeManager(),  # type: ignore[arg-type]
    )
    raised = False
    try:
        provider.propose_name({}, {})
    except NotImplementedError as exc:
        raised = True
        _check(
            "propose_name is NOT vertex-routed" in str(exc),
            f"NotImplementedError message cites v4 §2.2 contract (got {exc!s})",
        )
    _check(raised, "propose_name raises NotImplementedError per v4 §2.2")


# ─── Tests 4-6 + 8: sidecar lifecycle on AgentMessagingPlugin ────────────


class _SidecarHarness:
    """Minimal stand-in for AgentMessagingPlugin's sidecar mutation surface.

    We invoke the actual ``_register_inference_provider`` /
    ``_clear_inference_providers_for_bridge`` / ``get_inference_provider``
    method bodies by binding them to a minimal harness instance — this
    sidesteps the ~300-LOC plugin __init__ that pulls in PluginManager,
    state_service, etc. Pure functional contract; same code paths.
    """

    def __init__(self) -> None:
        self._inference_providers: dict[str, SessionInferenceProvider] = {}
        self._inference_providers_lock = threading.Lock()
        self._inference_provider_tombstones: OrderedDict[str, None] = OrderedDict()
        self._bridge_manager = _StubBridgeManager()
        self._peer_registry = _StubPeerRegistry()
        self._state = RealShapeState()

    def _get_state_service(self) -> object:
        # Mirrors AgentMessagingPlugin._get_state_service so bound methods
        # (resolve_role_to_instance) reach an offline agent_role_binding store.
        return self._state


class _StubPeerRegistry:
    def __init__(self) -> None:
        self._by_bridge: dict[str, list[Any]] = {}

    def register_binding(self, bridge_id: str, agent_instance_id: str) -> None:
        @dataclass
        class _Binding:
            agent_instance_id: str
        self._by_bridge.setdefault(bridge_id, []).append(
            _Binding(agent_instance_id=agent_instance_id),
        )

    def list_by_bridge(self, bridge_id: str) -> list[Any]:
        return list(self._by_bridge.get(bridge_id, []))


def _make_harness() -> _SidecarHarness:
    return _SidecarHarness()


def _bind_method(harness: _SidecarHarness, name: str) -> Any:
    from agent_messaging_plugin.plugin import AgentMessagingPlugin  # noqa: PLC0415
    return getattr(AgentMessagingPlugin, name).__get__(harness)


def test_register_populates_sidecar() -> None:
    harness = _make_harness()
    register = _bind_method(harness, "_register_inference_provider")
    accessor = _bind_method(harness, "get_inference_provider")

    register(
        bridge_id="agc-X",
        agent_instance_id="agi-X",
        agent_id="claude_code",
        session_label="X",
    )
    provider = accessor("agi-X")
    _check(
        provider is not None,
        "get_inference_provider returns the registered provider",
    )
    if provider is not None:
        _check(
            isinstance(provider, SessionInferenceProvider),
            "registered object is a SessionInferenceProvider",
        )


def test_clear_removes_via_list_by_bridge() -> None:
    harness = _make_harness()
    register = _bind_method(harness, "_register_inference_provider")
    clear = _bind_method(harness, "_clear_inference_providers_for_bridge")
    accessor = _bind_method(harness, "get_inference_provider")

    register(
        bridge_id="agc-CLR",
        agent_instance_id="agi-CLR",
        agent_id="claude_code",
        session_label="X",
    )
    # Wire the stub peer_registry so list_by_bridge returns the binding.
    harness._peer_registry.register_binding("agc-CLR", "agi-CLR")  # type: ignore[attr-defined]
    cleared = clear("agc-CLR")
    _check(cleared == 1, f"clear returns count=1 (got {cleared})")
    _check(
        accessor("agi-CLR") is None,
        "post-clear accessor returns None (default fallback path)",
    )


def test_reregister_replaces_without_leak() -> None:
    harness = _make_harness()
    register = _bind_method(harness, "_register_inference_provider")
    accessor = _bind_method(harness, "get_inference_provider")

    register(
        bridge_id="agc-A",
        agent_instance_id="agi-R",
        agent_id="claude_code",
        session_label="A",
    )
    first = accessor("agi-R")
    register(
        bridge_id="agc-B",
        agent_instance_id="agi-R",
        agent_id="claude_code",
        session_label="B",
    )
    second = accessor("agi-R")
    _check(
        first is not None and second is not None and first is not second,
        "re-register replaces the prior provider entry (pop-then-insert)",
    )
    _check(
        len(harness._inference_providers) == 1,  # type: ignore[attr-defined]
        f"sidecar carries exactly one entry under agi-R (got "
        f"{len(harness._inference_providers)})",  # type: ignore[attr-defined]
    )
    if second is not None:
        _check(
            second.bridge_id == "agc-B",
            f"latest provider bound to bridge agc-B (got {second.bridge_id})",
        )


def test_accessor_returns_none_for_unknown_agent_instance() -> None:
    harness = _make_harness()
    accessor = _bind_method(harness, "get_inference_provider")
    _check(
        accessor("agi-never-registered") is None,
        "accessor returns None for unknown agent_instance_id (fallback path)",
    )


# ─── INF-01 bridge-open gate: swept/closed holder DEFERs, live holder routes ─


def test_bridge_open_gate_returns_provider_for_open_bridge() -> None:
    harness = _make_harness()
    register = _bind_method(harness, "_register_inference_provider")
    accessor = _bind_method(harness, "get_inference_provider")

    register(
        bridge_id="agc-OPEN", agent_instance_id="agi-OPEN",
        agent_id="claude_code", session_label="X",
    )
    _check(
        accessor("agi-OPEN") is not None,
        "bridge-open gate: live provider on an OPEN bridge is returned "
        "(resolver routes PROVIDER)",
    )


def test_bridge_open_gate_defers_on_closed_bridge() -> None:
    harness = _make_harness()
    register = _bind_method(harness, "_register_inference_provider")
    accessor = _bind_method(harness, "get_inference_provider")

    register(
        bridge_id="agc-CLOSED", agent_instance_id="agi-CLOSED",
        agent_id="claude_code", session_label="X",
    )
    # The sidecar is NOT cleared (that is the unregister path); the bridge is
    # merely marked closed — the pre-REL-09 stale-entry condition.
    harness._bridge_manager.close_bridge("agc-CLOSED")  # type: ignore[attr-defined]  # noqa: SLF001
    _check(
        accessor("agi-CLOSED") is None,
        "bridge-open gate: stale provider on a CLOSED bridge returns None "
        "(resolver DEFERs, no BridgeNotFoundError)",
    )


def test_bridge_open_gate_defers_on_swept_bridge() -> None:
    harness = _make_harness()
    register = _bind_method(harness, "_register_inference_provider")
    accessor = _bind_method(harness, "get_inference_provider")

    register(
        bridge_id="agc-SWEPT", agent_instance_id="agi-SWEPT",
        agent_id="claude_code", session_label="X",
    )
    # sweep_idle/close pop the bridge from _bridges → get() returns None while
    # the sidecar entry lingers. This is the exact BridgeNotFoundError class.
    harness._bridge_manager.drop_bridge("agc-SWEPT")  # type: ignore[attr-defined]  # noqa: SLF001
    _check(
        accessor("agi-SWEPT") is None,
        "bridge-open gate: stale provider on a SWEPT (popped) bridge returns "
        "None (resolver DEFERs, no BridgeNotFoundError)",
    )


def test_autonomic_vertex_lane_defers_while_peer_session_holds() -> None:
    """D2-window ruling 2026-08-04 (pulled forward on measured runaway): a
    peer-session holder cannot serve raw vertex turns, so
    ``get_autonomic_provider`` answers ``None`` for a HELD slot too — the
    resolver's ``None`` → DEFER flip parks the turn in the durable queue.

    Named failing mutation: reverting the method's tail to the pre-ruling
    ``return self.get_inference_provider(resolved.agent_instance_id)`` reds
    the held-slot leg below — the live stub provider planted on the harness
    would be returned instead of ``None``.
    """
    harness = _make_harness()
    autonomic = _bind_method(harness, "get_autonomic_provider")

    _check(
        autonomic() is None,
        "autonomic vertex lane: VACANT slot -> None (vacancy DEFER flip, unchanged)",
    )

    claim_role_binding_v4(
        cast("Any", harness._state),  # noqa: SLF001
        name=SYS_AUTONOMIC_SLOT,
        claim=HolderClaim(
            holder_kind=HOLDER_KIND_SESSION,
            holder_identity={"agent_id": "claude_code", "session_label": "Seat"},
            agent_instance_id="agi-HELD",
            agent_session_id="ases-HELD",
            session_label="Seat",
        ),
    )
    # A live provider IS present for the holder — under the pre-ruling body
    # get_inference_provider would return it; the ruled body must not consult
    # it for the vertex lane at all.
    harness.get_inference_provider = lambda _agi: object()  # type: ignore[attr-defined]
    _check(
        autonomic() is None,
        "autonomic vertex lane: HELD-by-peer-session slot with a live "
        "provider -> None (vertex turns DEFER; D2-window ruling 2026-08-04)",
    )


# ─── Test 7: trigger-data carries inference_vertex_session_id ────────────


def test_trigger_data_tags_inference_vertex_session_id() -> None:
    from agent_messaging_plugin.platform_surface import (  # noqa: PLC0415
        PlatformSurface,
    )

    @dataclass
    class _BridgeState:
        bridge_id: str
        session_id: str
        agent_instance_id: str | None
        client_id: str | None

    bridge = _BridgeState(
        bridge_id="agc-T",
        session_id="sess-T",
        agent_instance_id="agi-T",
        client_id=None,
    )
    trigger = PlatformSurface._build_process_call_trigger_data(
        bridge=cast("Any", bridge),
        process_key="service_interface::knowledge_service::search",
        reason="test",
        inference_vertex_role="Claude-B",
        operator_equivalent=False,  # B1 Finding-B: not under test here
    )
    _check(
        trigger.get("inference_vertex_session_id") == "agi-T",
        f"trigger_data carries inference_vertex_session_id=agi-T "
        f"(got {trigger.get('inference_vertex_session_id')!r})",
    )
    _check(
        trigger.get("inference_vertex_role") == "Claude-B",
        f"trigger_data carries inference_vertex_role=Claude-B (◆R2) "
        f"(got {trigger.get('inference_vertex_role')!r})",
    )

    # Empty / pre-register bridge — falls through to empty string so
    # downstream wrapper treats it identically to "unknown".
    bridge_empty = _BridgeState(
        bridge_id="agc-E",
        session_id="sess-E",
        agent_instance_id=None,
        client_id=None,
    )
    trigger_empty = PlatformSurface._build_process_call_trigger_data(
        bridge=cast("Any", bridge_empty),
        process_key="x",
        reason="y",
        inference_vertex_role="",
        operator_equivalent=False,  # B1 Finding-B: not under test here
    )
    _check(
        trigger_empty.get("inference_vertex_session_id") == "",
        "pre-register bridge tags inference_vertex_session_id='' (fallback)",
    )
    _check(
        trigger_empty.get("inference_vertex_role") == "",
        "roleless bridge tags inference_vertex_role='' (fallback)",
    )


# ─── Test 9: _resolve_originating_role reverse-looks-up the role ─────────


def test_resolve_originating_role_reverse_lookup() -> None:
    from agent_messaging_plugin.platform_surface import (  # noqa: PLC0415
        PlatformSurface,
    )

    @dataclass
    class _BridgeState:
        bridge_id: str
        session_id: str
        agent_instance_id: str | None
        client_id: str | None

    class _SurfaceHarness:
        def __init__(self) -> None:
            self._state_service = RealShapeState()

    harness = _SurfaceHarness()
    claim_role_binding_v4(
        cast("Any", harness._state_service),  # noqa: SLF001
        name="Claude-B",
        claim=HolderClaim(
            holder_kind=HOLDER_KIND_SESSION,
            holder_identity={"agent_id": "claude_code", "session_label": "Claude-B"},
            agent_instance_id="agi-role",
            agent_session_id="sess-B",
            session_label="Claude-B",
        ),
    )
    resolve = PlatformSurface._resolve_originating_role.__get__(harness)

    bound = _BridgeState(
        bridge_id="agc-r", session_id="s", agent_instance_id="agi-role",
        client_id=None,
    )
    _check(
        resolve(cast("Any", bound)) == "Claude-B",
        "_resolve_originating_role → durable role held by the instance",
    )

    roleless = _BridgeState(
        bridge_id="agc-x", session_id="s", agent_instance_id="agi-none",
        client_id=None,
    )
    _check(
        resolve(cast("Any", roleless)) == "",
        "_resolve_originating_role → '' for a roleless instance",
    )


# ─── Test 10: resolve_role_to_instance (role → current instance) ─────────


def test_resolve_role_to_instance() -> None:
    harness = _make_harness()
    claim_role_binding_v4(
        cast("Any", harness._state),  # noqa: SLF001
        name="Claude-B",
        claim=HolderClaim(
            holder_kind=HOLDER_KIND_SESSION,
            holder_identity={"agent_id": "claude_code", "session_label": "Claude-B"},
            agent_instance_id="agi-live",
            agent_session_id="sess-B",
            session_label="Claude-B",
        ),
    )
    resolve = _bind_method(harness, "resolve_role_to_instance")
    _check(
        resolve("Claude-B") == "agi-live",
        "resolve_role_to_instance → current instance for a claimed role",
    )
    _check(
        resolve("Ghost") is None,
        "resolve_role_to_instance → None for a vacant role",
    )


# ─── Test 11: tombstone lifecycle (case 3b detectability) ────────────────


def test_tombstone_lifecycle() -> None:
    harness = _make_harness()
    register = _bind_method(harness, "_register_inference_provider")
    clear = _bind_method(harness, "_clear_inference_providers_for_bridge")
    was_bound = _bind_method(harness, "was_inference_provider_bound")

    register(
        bridge_id="agc-TB", agent_instance_id="agi-TB",
        agent_id="claude_code", session_label="B",
    )
    _check(
        not was_bound("agi-TB"),
        "live provider is NOT tombstoned",
    )
    harness._peer_registry.register_binding("agc-TB", "agi-TB")  # type: ignore[attr-defined]  # noqa: SLF001
    clear("agc-TB")
    _check(
        was_bound("agi-TB"),
        "disconnected instance is tombstoned → resolver DEFERs (3b), not default",
    )
    # Re-register (reconnect) clears the tombstone — live again.
    register(
        bridge_id="agc-TB2", agent_instance_id="agi-TB",
        agent_id="claude_code", session_label="B",
    )
    _check(
        not was_bound("agi-TB"),
        "re-register clears the tombstone (live again)",
    )


# ─── Test 12 (B1): tag-write role lookup degrades, never breaks dispatch ──


def test_resolve_originating_role_degrades_on_read_error() -> None:
    from agent_messaging_plugin.platform_surface import (  # noqa: PLC0415
        PlatformSurface,
    )

    @dataclass
    class _BridgeState:
        bridge_id: str
        session_id: str
        agent_instance_id: str | None
        client_id: str | None

    class _RaisingState:
        """query_state raises — models a transient PoolTimeout under scram."""

        def query_state(self, namespace: str, query: dict[str, Any]) -> Any:
            del namespace, query
            raise RuntimeError("simulated agent_role_binding read fault")

    class _SurfaceHarness:
        def __init__(self) -> None:
            self._state_service = _RaisingState()

    harness = _SurfaceHarness()
    resolve = PlatformSurface._resolve_originating_role.__get__(harness)
    bound = _BridgeState(
        bridge_id="agc-b1", session_id="s", agent_instance_id="agi-b1",
        client_id=None,
    )
    raised = False
    result = "SENTINEL"
    try:
        result = resolve(cast("Any", bound))
    except Exception:  # noqa: BLE001 — B1 requires NO propagation
        raised = True
    _check(
        not raised,
        "B1: _resolve_originating_role does NOT propagate a read fault "
        "(process_call dispatch would proceed)",
    )
    _check(
        result == "",
        "B1: read fault degrades to roleless tag '' (resolver → instance path)",
    )


# ─── Test 13 (N1): tombstone eviction past cap is LOUD ───────────────────


class _ListHandler(logging.Handler):
    def __init__(self) -> None:
        super().__init__()
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)


def test_tombstone_eviction_is_loud() -> None:
    from agent_messaging_plugin import plugin as plugin_mod  # noqa: PLC0415

    harness = _make_harness()
    register = _bind_method(harness, "_register_inference_provider")
    clear = _bind_method(harness, "_clear_inference_providers_for_bridge")
    was_bound = _bind_method(harness, "was_inference_provider_bound")
    cap = plugin_mod._INFERENCE_TOMBSTONE_CAP  # noqa: SLF001

    handler = _ListHandler()
    handler.setLevel(logging.WARNING)
    plugin_mod.logger.addHandler(handler)
    try:
        for i in range(cap + 1):
            inst = f"agi-evict-{i}"
            bridge = f"agc-evict-{i}"
            register(
                bridge_id=bridge, agent_instance_id=inst,
                agent_id="claude_code", session_label="X",
            )
            harness._peer_registry.register_binding(bridge, inst)  # type: ignore[attr-defined]  # noqa: SLF001
            clear(bridge)
    finally:
        plugin_mod.logger.removeHandler(handler)

    _check(
        not was_bound("agi-evict-0"),
        "oldest tombstone (agi-evict-0) evicted once cap exceeded",
    )
    _check(
        was_bound(f"agi-evict-{cap}"),
        "newest tombstone retained after eviction",
    )
    evicted_warnings = [
        r for r in handler.records
        if r.levelno >= logging.WARNING and "tombstone evicted" in r.getMessage()
    ]
    _check(
        len(evicted_warnings) >= 1,
        "N1: tombstone eviction emits a LOUD WARNING (not silent)",
    )


# ─── Driver ─────────────────────────────────────────────────────────────


def main() -> int:
    print("plugins/agent_messaging_plugin/tests/session_inference_provider_smoke.py")
    test_process_error_emits_bridge_delivery_error()
    test_process_results_emits_bridge_delivery_result()
    test_propose_name_raises_not_implemented()
    test_register_populates_sidecar()
    test_clear_removes_via_list_by_bridge()
    test_reregister_replaces_without_leak()
    test_accessor_returns_none_for_unknown_agent_instance()
    test_bridge_open_gate_returns_provider_for_open_bridge()
    test_bridge_open_gate_defers_on_closed_bridge()
    test_bridge_open_gate_defers_on_swept_bridge()
    test_autonomic_vertex_lane_defers_while_peer_session_holds()
    test_trigger_data_tags_inference_vertex_session_id()
    test_resolve_originating_role_reverse_lookup()
    test_resolve_role_to_instance()
    test_tombstone_lifecycle()
    test_resolve_originating_role_degrades_on_read_error()
    test_tombstone_eviction_is_loud()

    print()
    print(f"passed: {_passed}")
    if _failed:
        print(f"failed: {len(_failed)}")
        for label in _failed:
            print(f"  - {label}")
        return 1
    print("all green")
    return 0


if __name__ == "__main__":
    sys.exit(main())
