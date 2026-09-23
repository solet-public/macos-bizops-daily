#!/usr/bin/env python3
"""The INFRA ``peer/claim_role`` bridge route, end-to-end (FastAPI TestClient, no DB).

Companion to ``route_activity_classification_smoke`` (S4), which pins the route's
CLASSIFICATION. This one pins its BEHAVIOUR, and in particular the two properties
that made a shared implementation mandatory rather than optional.

WHY THIS FILE EXISTS. The 2026-07-29 Architect ruling split role claiming across
two transports: ``/process/call`` for a genuine model turn (``/rename``) and this
route for the forwarder's housekeeping claim. The obvious way to build the second
one — copy the ``peer_send_by_name`` precedent's parallel ``_impl`` — was rejected,
because a duplicate would have to reproduce the handover contract exactly and a
subtly wrong copy fires PHANTOM HANDOVER WAKES, which is the same defect class the
split exists to remove. So both transports call one shared body
(``role_claim.claim_role_for_session``).

That design is only worth anything if it is TESTED THROUGH BOTH CALL SITES. A test
that drives only the route proves nothing about the verb that was rewritten
underneath it, and vice versa. ``test_both_transports_agree_on_the_public_shape``
is the assertion that earns the refactor; the rest pin the route itself.

Run:
    .venv/bin/python3 plugins/agent_messaging_plugin/tests/peer_claim_role_route_smoke.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT / "ananta" / "src"))
sys.path.insert(0, str(REPO_ROOT / "plugins" / "agent_messaging_plugin" / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

if TYPE_CHECKING:
    from ananta.interfaces.state_management_interface import StateManagementInterface

from _real_state_fake import RealShapeState  # noqa: E402
from ananta.services.store import Store, open_store  # noqa: E402
from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from agent_messaging_plugin import role_claim as role_claim_module  # noqa: E402
from agent_messaging_plugin.bridge_sessions import BridgeSessionManager  # noqa: E402
from agent_messaging_plugin.http_routes import register_routes  # noqa: E402
from agent_messaging_plugin.peer_registry import PeerRegistry  # noqa: E402
from agent_messaging_plugin.role_binding_store import resolve_role_binding  # noqa: E402
from agent_messaging_plugin.role_claim import (  # noqa: E402
    RoleClaimOrigin,
    claim_role_for_session,
)
from agent_messaging_plugin.schema import (  # noqa: E402
    PEER_BINDING_NAMESPACE,
    get_peer_binding_schema,
)

# Opaque, operator-defined-shaped role — role names are never special-cased.
_ROLE = "zz-Ω arbitrary/role #7!"

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


def _state() -> StateManagementInterface:
    return cast("StateManagementInterface", RealShapeState())


def _fresh_peer_registry() -> PeerRegistry:
    store: Store = open_store(
        get_peer_binding_schema(), namespace=PEER_BINDING_NAMESPACE, backend="in_memory",
    )
    return PeerRegistry(bindings_store=store)


def _bridge_manager() -> BridgeSessionManager:
    return BridgeSessionManager(
        session_id_factory=lambda _name: "ags-http",
        idle_timeout_s=3600,
        max_pending_events=20,
        long_poll_timeout_s=1,
    )


class _NoOwedDirectWakeService:
    """Minimal route stub; these smokes assert claiming, not delivery.

    A4 (2026-08-04): used to carry rehome_owed_direct_wakes/rehome_owed_role_wakes
    because the /peer/register route called both on every register; both calls
    retired with the escalation/consumption-reconcile apparatus they served, so
    this fake is now a pure placeholder for the ``agent_messaging_service``
    param the route still requires.
    """


class _Harness:
    """A live bridge + registry + state, with handover notices RECORDED.

    Notices are recorded at ``role_claim.send_handover_notice`` — the seam both
    transports funnel through — so "did a wake fire?" is answerable without a
    messaging service. That question is the point of several tests below: the
    steady-state re-assert case must fire NOTHING.
    """

    def __init__(self) -> None:
        self.manager = _bridge_manager()
        self.registry = _fresh_peer_registry()
        self.state = _state()
        self.notices: list[dict[str, str]] = []
        self._original = role_claim_module.send_handover_notice
        role_claim_module.send_handover_notice = self._record
        app = FastAPI()
        register_routes(
            app,
            bridge_manager=self.manager,
            peer_registry=self.registry,
            platform_surface=cast("Any", object()),
            agent_messaging_service=cast("Any", _NoOwedDirectWakeService()),
            config={"long_poll_timeout_seconds": 1},
            state_service=self.state,
        )
        self.client = TestClient(app)

    def _record(self, **kwargs: Any) -> bool:
        self.notices.append(
            {
                "agi": str(kwargs.get("peer_agent_instance_id", "")),
                "kind": str(kwargs.get("kind", "")),
            },
        )
        return True

    def close(self) -> None:
        role_claim_module.send_handover_notice = self._original

    def session(self, *, agi: str, session_id: str, label: str = "lbl") -> str:
        """Open a bridge and register a peer identity on it; return the bridge_id."""
        bridge_id = self.manager.open(solet_name="", parent_pid=123).bridge_id
        resp = self.client.post(
            f"/api/v1/bridge/{bridge_id}/peer/register",
            json={
                "agent_id": "claude_code",
                "agent_instance_id": agi,
                "agent_session_id": session_id,
                "session_label": label,
            },
        )
        assert resp.status_code == 200, resp.text
        return bridge_id

    def claim(self, bridge_id: str, name: str = _ROLE) -> Any:
        return self.client.post(
            f"/api/v1/bridge/{bridge_id}/peer/claim_role", json={"name": name},
        )

    def claim_with_takeover(self, *, agi: str, session_id: str, name: str = _ROLE) -> Any:
        """Drive the SHARED claim body with an explicit takeover.

        This older smoke remains scoped to the shared MODEL_TURN body. The
        caller-reachable INFRA route and ``solet watch --takeover`` escape
        hatch are pinned separately by ``takeover_route_reachability_smoke``;
        keeping this helper direct avoids duplicating that route fixture here.
        """
        return role_claim_module.claim_role_for_session(
            origin=role_claim_module.RoleClaimOrigin.MODEL_TURN,
            name=name,
            agent_id="claude_code",
            agent_instance_id=agi,
            agent_session_id=session_id,
            session_label="lbl",
            state_service=self.state,
            bridge_manager=self.manager,
            peer_registry=self.registry,
            agent_messaging_service=cast("Any", _NoOwedDirectWakeService()),
            call_context=None,
            takeover=True,
        )


def test_fresh_claim_returns_the_outcome_synchronously() -> None:
    """The response IS the outcome — no receipt, no EDGE_SINK notification.

    This is Condition 1 of the ruling, and the half most easily lost: adding a
    route that merely QUEUES would strip the model-activity stamp but leave the
    ``bridge_delivery_result`` firing every tick, which is the other half of the
    defect. A ``status``/``action_id`` receipt in this body means that happened.
    """
    h = _Harness()
    try:
        bridge_id = h.session(agi="agi-A", session_id="sess-A")
        resp = h.claim(bridge_id)
        _check(resp.status_code == 200, "fresh claim returns 200")
        body = resp.json()
        _check(body.get("action") == "claimed", "fresh claim reports action='claimed'")
        _check(
            "status" not in body and "action_id" not in body,
            "the response carries the OUTCOME, not a queued receipt (no EDGE_SINK fan-out)",
        )
        _check(
            resolve_role_binding(h.state, _ROLE).agent_instance_id == "agi-A",
            "the claim actually landed the binding in the state table",
        )
    finally:
        h.close()


def test_self_reclaim_reports_updated_and_wakes_nobody() -> None:
    """The steady-state case, and the one that must stay silent.

    An idempotent self-re-claim is what the forwarder issues on a re-assert. It
    must surface as ``updated`` and fire NO handover notice — a duplicate
    implementation that reported ``claimed`` here would wake the session about a
    role it already held, every time, forever.
    """
    h = _Harness()
    try:
        bridge_id = h.session(agi="agi-A", session_id="sess-A")
        h.claim(bridge_id)
        h.notices.clear()
        resp = h.claim(bridge_id)
        _check(
            resp.json().get("action") == "updated",
            "self-re-claim reports action='updated' (the /rename refresh contract)",
        )
        _check(h.notices == [], "self-re-claim fires NO handover notice — nobody is woken")
    finally:
        h.close()


def test_live_holder_is_refused_without_takeover() -> None:
    """WS-2e §4.3.2 — a LIVE different session cannot be displaced silently.

    This is the row of the claim decision table that refuses, and the reason
    `role_held_live` exists. Before it, ANY different session claiming took the
    role and the incumbent learned only from the displacement notice — which is
    how a mistaken second launch split a brain.

    The refusal must NAME the holder: a refusal an operator cannot act on just
    moves the confusion.
    """
    h = _Harness()
    try:
        first = h.session(agi="agi-A", session_id="sess-A")
        h.claim(first)
        h.notices.clear()
        second = h.session(agi="agi-B", session_id="sess-B")
        resp = h.claim(second)
        _check(
            resp.status_code == 409,
            "a LIVE different session is REFUSED with 409",
        )
        body = resp.json()
        _check(
            body.get("code") == "role_held_live",
            "refusal carries the stable role_held_live code",
        )
        _check("agi-A" in body.get("message", ""), "refusal names the live holder")
        _check(
            h.notices == [],
            "a REFUSED claim wakes nobody — no displacement happened",
        )
    finally:
        h.close()


def test_displacement_notifies_prior_and_new_holder() -> None:
    """A real displacement must still wake both parties — the skip is narrow.

    Post-§4.3.2 this path requires an explicit takeover: the INFRA route never
    passes it, so the harness drives the same shared body the MODEL_TURN verb
    uses. What is asserted is unchanged — displacement still notifies both
    parties; only the way you GET here is now deliberate.
    """
    h = _Harness()
    try:
        first = h.session(agi="agi-A", session_id="sess-A")
        h.claim(first)
        h.notices.clear()
        h.session(agi="agi-B", session_id="sess-B")
        outcome = h.claim_with_takeover(agi="agi-B", session_id="sess-B")
        _check(
            getattr(outcome, "action", None) == "displaced",
            "an explicit takeover reports action='displaced'",
        )
        _check(
            [n["kind"] for n in h.notices] == ["displaced-holder", "new-holder"],
            "displacement notifies the displaced prior AND confirms to the new holder",
        )
        _check(
            h.notices[0]["agi"] == "agi-A" and h.notices[1]["agi"] == "agi-B",
            "the notices target the prior holder and the claimant respectively",
        )
    finally:
        h.close()


def test_response_is_json_serializable_and_never_leaks_prior() -> None:
    """Codex BLOCKER-1, on the route side.

    The v4 outcome carries ``prior`` (a ``ResolvedRole``) for the notify only. It
    is not json-serializable, and this transport serializes its result straight
    into an HTTP body, so a leak is an immediate 500 rather than a latent one.
    """
    h = _Harness()
    try:
        h.claim(h.session(agi="agi-A", session_id="sess-A"))
        resp = h.claim(h.session(agi="agi-B", session_id="sess-B"))
        body = resp.json()
        _check("prior" not in body, "the ResolvedRole `prior` never reaches the response body")
        ok = True
        try:
            json.dumps(body)
        except TypeError:
            ok = False
        _check(ok, "the response body is json.dumps-able")
    finally:
        h.close()


def test_system_slot_claims_are_refused_on_this_transport() -> None:
    """Fail-closed: no plugin principal reaches this route, so no ``sys:`` name may.

    The route passes ``call_context=None`` because a bridge is not a plugin
    principal. §6.1 then refuses every reserved name — which is exactly right:
    the forwarder claims standing roles, never system slots. Asserted because
    "we pass None" only looks safe until someone checks what None DOES.
    """
    h = _Harness()
    try:
        bridge_id = h.session(agi="agi-A", session_id="sess-A")
        resp = h.claim(bridge_id, name="sys:autonomic")
        _check(
            resp.status_code == 403,
            f"a sys: slot claim over the bridge route is REFUSED (got {resp.status_code})",
        )
        _check(
            resp.json().get("code") == "system_slot_claim_denied",
            "the refusal names the system-slot gate",
        )
    finally:
        h.close()


def test_identity_comes_from_the_binding_not_the_body() -> None:
    """Only the role name is accepted; identity is server-side.

    The forwarder's identity IS the bridge's identity, so reading it from the
    body would add a spoofing surface for no benefit. A body that tries to name a
    different instance must not change who the role binds to.
    """
    h = _Harness()
    try:
        bridge_id = h.session(agi="agi-A", session_id="sess-A")
        resp = h.client.post(
            f"/api/v1/bridge/{bridge_id}/peer/claim_role",
            json={
                "name": _ROLE,
                "agent_instance_id": "agi-IMPOSTOR",
                "agent_session_id": "sess-IMPOSTOR",
                "agent_id": "somebody_else",
            },
        )
        _check(resp.status_code == 200, "extra body fields are ignored, not fatal")
        _check(
            resp.json().get("agent_instance_id") == "agi-A",
            "the claim binds the CALLING bridge's instance, not the body's",
        )
        _check(
            resolve_role_binding(h.state, _ROLE).agent_session_id == "sess-A",
            "the stored binding keys on the bridge's own session id",
        )
    finally:
        h.close()


def test_unregistered_bridge_cannot_claim() -> None:
    """An identity-less bridge has nothing to bind a role to — refuse, don't guess."""
    h = _Harness()
    try:
        bridge_id = h.manager.open(solet_name="", parent_pid=7).bridge_id
        resp = h.claim(bridge_id)
        _check(resp.status_code == 400, "claiming before peer/register is refused")
        _check(
            resp.json().get("code") == "peer_identity_unregistered",
            "the refusal says the identity is missing (not a vague 400)",
        )
    finally:
        h.close()


def test_both_transports_agree_on_the_public_shape() -> None:
    """THE assertion that earns the shared helper.

    The verb and the route are two callers of one body. If they ever disagree
    about the published payload, one of them is lying to its clients — and the
    verb's schema contract makes that failure land AFTER the binding write, i.e.
    reported as a failed claim that actually succeeded. Driving the shared body
    at both origins and comparing keys is what keeps them from drifting.
    """
    h = _Harness()
    try:
        bridge_id = h.session(agi="agi-A", session_id="sess-A")
        route_body = h.claim(bridge_id).json()
        # Same body, MODEL_TURN origin — what the /process/call verb runs.
        verb_result = claim_role_for_session(
            origin=RoleClaimOrigin.MODEL_TURN,
            name=_ROLE,
            agent_id="claude_code",
            agent_instance_id="agi-A",
            agent_session_id="sess-A",
            session_label="lbl",
            state_service=h.state,
            bridge_manager=h.manager,
            peer_registry=h.registry,
            agent_messaging_service=cast("Any", _NoOwedDirectWakeService()),
            call_context=None,
        )
        verb_body = verb_result.to_public()  # type: ignore[union-attr]
        _check(
            set(route_body) == set(verb_body),
            f"both transports publish the SAME keys (route={sorted(route_body)}, "
            f"verb={sorted(verb_body)})",
        )
        _check(
            verb_body["action"] == "updated",
            "the verb's self-re-claim of the same session also reports 'updated'",
        )
        _check(
            route_body["agent_session_id"] == verb_body["agent_session_id"] == "sess-A",
            "both echo the RESOLVED session id the binding is keyed on",
        )
    finally:
        h.close()


def main() -> int:
    tests = [
        test_fresh_claim_returns_the_outcome_synchronously,
        test_self_reclaim_reports_updated_and_wakes_nobody,
        test_displacement_notifies_prior_and_new_holder,
        test_response_is_json_serializable_and_never_leaks_prior,
        test_system_slot_claims_are_refused_on_this_transport,
        test_identity_comes_from_the_binding_not_the_body,
        test_unregistered_bridge_cannot_claim,
        test_both_transports_agree_on_the_public_shape,
    ]
    for test in tests:
        test()
    print(f"\n{_passed} passed, {len(_failed)} failed")
    for label in _failed:
        print(f"  FAILED: {label}")
    return 1 if _failed else 0


if __name__ == "__main__":
    sys.exit(main())
