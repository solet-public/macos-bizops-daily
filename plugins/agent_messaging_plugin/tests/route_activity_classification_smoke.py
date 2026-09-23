#!/usr/bin/env python3
"""REL-05 S4 — model-activity route total-classification + F1 register pins (no DB).

The consumption signal is only sound if EVERY bridge route is deliberately
classified as model-initiated (stamp) or forwarder/infra (never stamp). This
smoke iterates the ACTUAL FastAPI route table produced by ``register_routes``
and asserts each route under the bridge prefix is in EXACTLY one of the two
:mod:`route_activity` sets — a future route added without classifying it returns
``None`` here and the smoke goes RED (kills the drift class permanently).

F1 (existential): ``peer/register`` is INFRA. The forwarder auto-invokes it on
every attach AND every reconnect with no model turn; had it stamped, every
deploy-flap reconnect would auto-consume every owed row. Pinned here from BOTH
the open-auto-register and the reconnect-re-register angle (they hit the SAME
``/peer/register`` path, so one classification governs both). ``touch()`` (the
idle-reap timestamp the forwarder's long-poll bumps) must ALSO not stamp
model activity — else a deaf session's polling would read as a live turn.

Run:
    SOLET_NAME=<name>-test .venv/bin/python3 \
        plugins/agent_messaging_plugin/tests/route_activity_classification_smoke.py
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, cast

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT / "ananta" / "src"))
sys.path.insert(0, str(REPO_ROOT / "plugins" / "agent_messaging_plugin" / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from ananta.services.store import Store, open_store  # noqa: E402
from fastapi import FastAPI  # noqa: E402
from fastapi.routing import APIRoute  # noqa: E402

from agent_messaging_plugin.bridge_sessions import BridgeSessionManager  # noqa: E402
from agent_messaging_plugin.http_routes import API_PREFIX, register_routes  # noqa: E402
from agent_messaging_plugin.models import BridgeBinding  # noqa: E402
from agent_messaging_plugin.peer_registry import PeerRegistry  # noqa: E402
from agent_messaging_plugin.route_activity import (  # noqa: E402
    INFRA_ROUTES,
    MODEL_INITIATED_ROUTES,
    classify_route,
    is_model_initiated_path,
    stamp_model_activity_for_bridge,
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


def _peer_registry() -> PeerRegistry:
    store: Store = open_store(
        get_peer_binding_schema(),
        namespace=PEER_BINDING_NAMESPACE,
        backend="in_memory",
    )
    return PeerRegistry(bindings_store=store)


def _bridge_manager() -> BridgeSessionManager:
    return BridgeSessionManager(
        session_id_factory=lambda _n: "ags-http",
        idle_timeout_s=3600,
        max_pending_events=50,
        long_poll_timeout_s=1,
    )


def _app() -> FastAPI:
    app = FastAPI()
    register_routes(
        app,
        bridge_manager=_bridge_manager(),
        peer_registry=_peer_registry(),
        platform_surface=cast(Any, object()),
        agent_messaging_service=cast(Any, object()),
        config={"long_poll_timeout_seconds": 1},
        state_service=cast(Any, object()),
    )
    return app


def _bridge_routes(app: FastAPI) -> list[str]:
    return sorted(
        {
            route.path
            for route in app.routes
            if isinstance(route, APIRoute) and route.path.startswith(API_PREFIX)
        },
    )


def test_route_table_total_classification() -> None:
    routes = _bridge_routes(_app())
    unclassified = [path for path in routes if classify_route(path) is None]
    _check(
        unclassified == [],
        f"S4: EVERY actual bridge route is classified (unclassified: {unclassified})",
    )
    _check(len(routes) >= 15, f"S4: the route table was actually enumerated ({len(routes)} routes)")


def test_sets_are_disjoint_partition() -> None:
    overlap = MODEL_INITIATED_ROUTES & INFRA_ROUTES
    _check(overlap == set(), f"S4: model / infra sets are disjoint (overlap: {overlap})")


def test_f1_register_is_infra() -> None:
    reg_path = f"{API_PREFIX}/{{bridge_id}}/peer/register"
    _check(
        classify_route(reg_path) == "infra",
        "S4/F1: peer/register is INFRA (auto-invoked on attach + reconnect, no model turn)",
    )
    # F1 pins from BOTH angles: an open-auto-register AND a reconnect-re-register
    # both hit the SAME concrete /peer/register path → neither stamps.
    _check(
        not is_model_initiated_path("/api/v1/bridge/agc-open/peer/register"),
        "S4/F1: open-auto-register path does NOT stamp",
    )
    _check(
        not is_model_initiated_path("/api/v1/bridge/agc-reconnect/peer/register"),
        "S4/F1: reconnect-re-register path does NOT stamp",
    )


def test_claim_role_route_is_infra() -> None:
    """The forwarder's housekeeping claim must not read as a model turn.

    Pinned explicitly, exactly as peer/register is: the total-classification
    test above would still pass if this route were classified MODEL_INITIATED
    by mistake, and that mistake is the original defect restored -- a claim
    every ~176s stamping last_model_activity_at with no model turn behind it.

    The paired assertion is deliberate. /peer/claim_role must NOT stamp, while
    /process/call MUST keep stamping, because a genuine /rename claim IS a model
    turn. Splitting the transports is only meaningful if both halves hold, so
    both are asserted here rather than trusting one and inferring the other.
    """
    claim_path = f"{API_PREFIX}/{{bridge_id}}/peer/claim_role"
    _check(
        classify_route(claim_path) == "infra",
        "S4: peer/claim_role is INFRA (forwarder open / reconnect / re-assert, no model turn)",
    )
    _check(
        not is_model_initiated_path("/api/v1/bridge/agc-reassert/peer/claim_role"),
        "S4: a steady-state re-assert claim does NOT stamp model activity",
    )
    _check(
        is_model_initiated_path("/api/v1/bridge/agc-rename/process/call"),
        "S4: the model-facing /process/call claim path STILL stamps (a /rename is a real turn)",
    )


def test_forwarder_infra_routes_never_stamp() -> None:
    for suffix in ("events", "peer/drain", "peer/delivered"):
        path = f"/api/v1/bridge/agc-x/{suffix}"
        _check(
            not is_model_initiated_path(path),
            f"S4: forwarder route {suffix} does NOT stamp",
        )


def test_model_routes_do_stamp() -> None:
    for suffix in ("process/call", "peer/send", "peer/inbox", "agent/thread/open"):
        path = f"/api/v1/bridge/agc-x/{suffix}"
        _check(
            is_model_initiated_path(path),
            f"S4: model route {suffix} DOES stamp",
        )


def test_touch_does_not_stamp_but_activity_does() -> None:
    mgr = _bridge_manager()
    bridge = mgr.open(solet_name="", parent_pid=9)
    _check(
        bridge.last_model_activity_at == "",
        "S4: a fresh bridge has NO model-activity stamp",
    )
    bridge.touch()
    _check(
        bridge.last_model_activity_at == "",
        "S4: touch() (idle-reap timestamp) does NOT stamp model activity",
    )
    stamp = bridge.stamp_model_activity()
    _check(
        bridge.last_model_activity_at == stamp and stamp != "",
        "S4: stamp_model_activity() DOES set the model-activity timestamp",
    )


def test_stamp_mirrors_to_binding_only_for_model_route() -> None:
    mgr = _bridge_manager()
    reg = _peer_registry()
    bridge = mgr.open(solet_name="", parent_pid=9)
    bridge.agent_instance_id = "agi-R"
    reg.register(
        BridgeBinding(
            bridge_id=bridge.bridge_id,
            agent_id="claude_code",
            agent_instance_id="agi-R",
            session_label="R",
            parent_pid=9,
        ),
    )
    # A model route stamps in-memory + mirrors to the binding.
    stamp_model_activity_for_bridge(mgr, reg, bridge.bridge_id)
    row = reg._bindings.read_one({"agent_instance_id": "agi-R"})  # noqa: SLF001
    _check(
        bridge.last_model_activity_at != ""
        and row is not None
        and str(row.get("last_model_activity_at") or "") != "",
        "S4: the stamp writes the in-memory session AND mirrors to the peer_binding row",
    )


def main() -> None:
    print("=== REL-05 S4 route-activity classification smoke ===")
    test_route_table_total_classification()
    test_sets_are_disjoint_partition()
    test_f1_register_is_infra()
    test_claim_role_route_is_infra()
    test_forwarder_infra_routes_never_stamp()
    test_model_routes_do_stamp()
    test_touch_does_not_stamp_but_activity_does()
    test_stamp_mirrors_to_binding_only_for_model_route()
    print(f"\n{_passed} passed, {len(_failed)} failed")
    if _failed:
        for label in _failed:
            print(f"  FAILED: {label}")
        sys.exit(1)


if __name__ == "__main__":
    main()
