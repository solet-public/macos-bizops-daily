"""WS-2c V4 (A1) — the reply address a delivery hands its recipient.

MEASURED defect this closes: the bridge route for ``peer_send_by_name`` passed
``reply_to_role=""`` HARDCODED since the route landed (``e02beb860``, 2026-07-26),
and its sender resolver never looked up the sender's own role. So EVERY
role-addressed send originated by an MCP session handed the recipient an
**instance** reply-to — on a healthy fleet, not only after a restart wave. Churn
only decided when it hurt. The platform verb (``plugin.py::peer_send_by_name``)
resolved it correctly the whole time; only the transport caller froze the default.
Pinned by a self-addressed probe over the bridge route, where the caller-attribution
ladder rungs are unreachable by construction.

The direct-send half was the same shape: ``dispatch_peer_send`` had no
``reply_to_role`` parameter at all, so ``adapter.wake`` was called without one and
the wake adapter's default produced the instance hint.

★ Why these tests drive the ROUTE IMPLS and not ``build_wake_reply_hint``: a unit
test over the hint builder passes with the entire route wiring absent — it cannot
see the hardcoded ``""``. Each leg below names the mutation that turns it red, and
those mutations are in the ROUTE, which is where the defect lived.

★ Why the fixture is ``RealShapeState`` and not a hand-rolled stub: the smoke's
predecessor used an EMPTY class as its state service, so ``list_roles_for_agent_instance``
raised ``AttributeError``, the degrade-silent ``except`` swallowed it, and the role
lookup returned ``""`` unconditionally. A leg written against that stub would assert
a role reply-to the fixture can NEVER emit — green only while the fix is absent.
``RealShapeState`` returns the real provider envelope (``action_status='completed'``,
``data.records``) and is schema-enforced, so a phantom column fails here rather than
at cutover.
"""

from __future__ import annotations

import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT / "ananta" / "src"))
sys.path.insert(0, str(REPO_ROOT / "plugins" / "agent_messaging_plugin" / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

if TYPE_CHECKING:
    from ananta.interfaces.state_management_interface import StateManagementInterface

from _real_state_fake import RealShapeState  # noqa: E402
from ananta.llm.agent_messaging.models import RoleMessagePersisted  # noqa: E402
from ananta.llm.agent_messaging.role_binding import (  # noqa: E402
    AGENT_ROLE_BINDING_NAMESPACE,
    COL_AGENT_INSTANCE_ID,
    COL_ROLE,
    TABLE_ROLE_BINDING,
)

from agent_messaging_plugin.bridge_sessions import (  # noqa: E402
    DEFAULT_BINDING_LIVENESS_WINDOW_S,
)
from agent_messaging_plugin.http_routes import (  # noqa: E402
    PeerSendBody,
    PeerSendByNameBody,
    _peer_send_by_name_impl,
    _peer_send_impl,
)
from agent_messaging_plugin.models import BridgeBinding  # noqa: E402
from agent_messaging_plugin.role_binding_store import (  # noqa: E402
    sole_role_for_reply_address,
)

_FAILURES: list[str] = []
_passes: list[int] = []

_SENDER_INSTANCE = "agi-sender"
_HOLDER_INSTANCE = "agi-holder"
_SENDER_ROLE = "Claude-C"
_SECOND_ROLE = "Auditor"
_TARGET_ROLE = "Architect"


def _check(condition: object, label: str) -> None:
    if condition:
        _passes.append(1)
    else:
        _FAILURES.append(label)


# ---------------------------------------------------------------------------
# Harness
# ---------------------------------------------------------------------------


def _binding(instance: str, label: str) -> BridgeBinding:
    return BridgeBinding(
        bridge_id=f"agc-{instance}",
        agent_id="claude_code",
        agent_instance_id=instance,
        session_label=label,
        parent_pid=4242,
    )


class _WakeAdapter:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def wake(self, **kwargs: Any) -> str:
        self.calls.append(kwargs)
        return "agc-woke"


class _Registry:
    """Minimal peer registry standing in for both endpoints of a send."""

    def __init__(self, adapter: _WakeAdapter, sender_instance: str) -> None:
        self._adapter = adapter
        self._sender_instance = sender_instance
        self.touched: list[str] = []

    def resolve(self, agent_id: str, agent_instance_id: str | None) -> BridgeBinding:
        del agent_id, agent_instance_id
        return _binding(_HOLDER_INSTANCE, "holder")

    def wake_adapter_for(self, agent_id: str) -> _WakeAdapter:
        del agent_id
        return self._adapter

    def agent_session_id_for_instance(self, agent_instance_id: str) -> str:
        return "ases-sender" if agent_instance_id == self._sender_instance else ""

    def touch_binding(self, agent_instance_id: str) -> int:
        self.touched.append(agent_instance_id)
        return 1

    def list_agent_ids(self) -> dict[str, list[BridgeBinding]]:
        # ``_lookup_binding_for_bridge`` scans this to find the SENDER's binding
        # from its bridge_id — the exact lookup A1 then feeds to the role
        # resolver, so the fake must expose it rather than shortcut it.
        return {
            "claude_code": [
                _binding(self._sender_instance, _SENDER_ROLE),
                _binding(_HOLDER_INSTANCE, "holder"),
            ],
        }


class _BridgeManager:
    @property
    def binding_liveness_window_s(self) -> int:
        return DEFAULT_BINDING_LIVENESS_WINDOW_S

    def __init__(self) -> None:
        self.events: list[tuple[str, str, str, dict[str, object]]] = []

    def get(self, bridge_id: str) -> object:
        del bridge_id
        return _OpenBridge()

    def append_event(
        self, bridge_id: str, event: str, prose: str, meta: dict[str, object],
    ) -> None:
        self.events.append((bridge_id, event, prose, meta))

    def touch(self, bridge_id: str) -> None:
        del bridge_id


class _OpenBridge:
    closed = False
    # H2 quiet-gap capture reads this off the RECIPIENT's live bridge. The fake
    # carries it rather than letting a getattr default hide the contract.
    last_model_activity_at = ""
    # A4 Amendment 5: binding_is_live (now checked for EVERY recipient kind,
    # not just watchers) reads this off the bridge -- a fresh timestamp keeps
    # this fixture's "live" bridge genuinely live under the generalized check.
    last_seen_at = datetime.now(UTC).isoformat()

    def touch(self) -> None:
        return


class _Service:
    def __init__(self) -> None:
        self.persisted: list[dict[str, Any]] = []

    def persist_role_message(self, **kwargs: Any) -> RoleMessagePersisted:
        self.persisted.append(kwargs)
        return RoleMessagePersisted(
            message_id=str(kwargs["message_id"]),
            # Stands in for the persisted ROW's created_at (see
            # role_dispatch_smoke) — never a clock read taken here.
            created_at="2026-08-01T00:00:00.000001+00:00",
        )

    def peer_send(self, request: Any) -> Any:
        return _SendResult()

    def mark_delivered(self, *, external_id: str) -> None:
        del external_id


class _SendResult:
    thread_id = "agt-x"
    message_id = "agm-x"
    cursor = 0


def _seed_role(state: RealShapeState, *, role: str, instance: str) -> None:
    """One complete v4 ``role_binding`` row.

    Seeded through ``state.rows(...)`` like the sibling role smokes: the row must
    be RESOLVABLE (holder_kind + holder_identity + claim bookkeeping), not merely
    present, or the role-send route fails at recipient resolution and the leg
    would be measuring role vacancy instead of the reply address.
    """
    state.rows(AGENT_ROLE_BINDING_NAMESPACE, TABLE_ROLE_BINDING).append(
        {
            "id": f"rbn-{role}-{instance}",
            "external_id": f"role:{role}",
            COL_ROLE: role,
            "holder_kind": "session",
            COL_AGENT_INSTANCE_ID: instance,
            "agent_session_id": f"ases-{instance}",
            "holder_identity": {"agent_id": "claude_code", "session_label": role},
            "claim_epoch": 1,
            "claimed_at": "2026-08-01T00:00:00+00:00",
            "is_deleted": 0,
        },
    )


def _state_with_roles(*roles: str) -> StateManagementInterface:
    """Seed ``role_binding`` rows for the SENDER, plus the send TARGET's row."""
    state = RealShapeState()
    for role in roles:
        _seed_role(state, role=role, instance=_SENDER_INSTANCE)
    _seed_role(state, role=_TARGET_ROLE, instance=_HOLDER_INSTANCE)
    return cast("StateManagementInterface", state)


def _run_role_send(state: StateManagementInterface) -> _WakeAdapter:
    adapter = _WakeAdapter()
    registry = _Registry(adapter, _SENDER_INSTANCE)
    _peer_send_by_name_impl(
        bridge_id=f"agc-{_SENDER_INSTANCE}",
        body=PeerSendByNameBody(name=_TARGET_ROLE, content="IMPORTANT: ping"),
        bridge_manager=_BridgeManager(),  # type: ignore[arg-type]
        peer_registry=registry,  # type: ignore[arg-type]
        agent_messaging_service=_Service(),
        state_service=state,
    )
    return adapter


def _run_direct_send(state: StateManagementInterface) -> _WakeAdapter:
    adapter = _WakeAdapter()
    registry = _Registry(adapter, _SENDER_INSTANCE)
    _peer_send_impl(
        bridge_id=f"agc-{_SENDER_INSTANCE}",
        body=PeerSendBody(
            peer_id="claude_code",
            peer_agent_instance_id=_HOLDER_INSTANCE,
            content=[{"type": "text", "text": "IMPORTANT: ping"}],
        ),
        bridge_manager=_BridgeManager(),  # type: ignore[arg-type]
        peer_registry=registry,  # type: ignore[arg-type]
        agent_messaging_service=_Service(),
        state_service=state,
    )
    return adapter


# ---------------------------------------------------------------------------
# Fixture self-check — run FIRST, because every leg below depends on it
# ---------------------------------------------------------------------------


def test_fixture_can_emit_a_role() -> None:
    """The fixture must be ABLE to produce the signal the legs assert.

    The predecessor stub could not: an empty class made the lookup raise into a
    degrade-silent ``except`` and return ``""`` forever, so a role-hint assertion
    was unfalsifiable. Measure the fixture before trusting any green built on it.
    """
    _check(
        sole_role_for_reply_address(_state_with_roles(_SENDER_ROLE), _SENDER_INSTANCE)
        == _SENDER_ROLE,
        "fixture: a single seeded role binding IS visible to the reply-address lookup",
    )
    _check(
        sole_role_for_reply_address(_state_with_roles(), _SENDER_INSTANCE) == "",
        "fixture: no seeded role → empty, so the negative control is real",
    )


# ---------------------------------------------------------------------------
# L1 — the pinned defect: bridge-route role send
# ---------------------------------------------------------------------------


def test_l1_role_send_carries_role_reply_to() -> None:
    """MUTATION: restore ``reply_to_role=""`` at the role-send route → RED.

    MUTATION (b): make ``sole_role_for_reply_address`` return ``""`` → RED. (b) is
    the one proving the lookup is load-bearing rather than decorative — (a) alone
    would also pass if the value were sourced from somewhere useless.
    """
    adapter = _run_role_send(_state_with_roles(_SENDER_ROLE))
    _check(len(adapter.calls) == 1, "L1: the role send reached a native wake")
    _check(
        adapter.calls and adapter.calls[0].get("reply_to_role") == _SENDER_ROLE,
        "L1: a role-addressed send from a single-role sender carries the SENDER's "
        "role as the reply address (was hardcoded '' since e02beb860)",
    )


def test_l2_direct_send_carries_role_reply_to() -> None:
    """MUTATION: drop ``reply_to_role`` from ``dispatch_peer_send`` → RED."""
    adapter = _run_direct_send(_state_with_roles(_SENDER_ROLE))
    _check(len(adapter.calls) == 1, "L2: the direct send reached a native wake")
    _check(
        adapter.calls and adapter.calls[0].get("reply_to_role") == _SENDER_ROLE,
        "L2: a direct send from a single-role sender carries the sender's role "
        "(dispatch_peer_send had no such parameter at all)",
    )


# ---------------------------------------------------------------------------
# L3 / L10 — the two ways this fix must REFUSE to answer
# ---------------------------------------------------------------------------


def test_l3_roleless_sender_keeps_instance_reply_to() -> None:
    """Negative control. MUTATION: make the lookup invent a role → RED.

    Without this leg the fix could hand out a fabricated address and still look
    green on L1/L2.
    """
    for label, adapter in (
        ("role send", _run_role_send(_state_with_roles())),
        ("direct send", _run_direct_send(_state_with_roles())),
    ):
        _check(
            adapter.calls and adapter.calls[0].get("reply_to_role") == "",
            f"L3 ({label}): a ROLELESS sender still gets the instance reply-to — "
            f"the fix never invents an address it cannot justify",
        )


def test_l10_multi_role_sender_keeps_instance_reply_to() -> None:
    """★ MUTATION: use ``roles[0]`` unconditionally → RED.

    ``list_roles_for_agent_instance`` returns ``sorted(roles)``, so an
    ``roles[0]`` pick is ALPHABETICAL and silent. That is fine for a flow TAG
    (a wrong answer costs a log line) and wrong for a reply ADDRESS (a wrong
    answer misroutes every reply to a role the sender was not acting in).
    ``stale but correct-session`` beats ``durable but wrong-session``.

    Note ``_SECOND_ROLE`` sorts BEFORE ``_SENDER_ROLE``, so an alphabetical pick
    would return the role this sender was NOT acting in — the leg would be
    toothless if both orderings gave the same answer.
    """
    _check(
        _SECOND_ROLE < _SENDER_ROLE,
        "L10 premise: the second role sorts FIRST, so roles[0] is the WRONG one",
    )
    state = _state_with_roles(_SENDER_ROLE, _SECOND_ROLE)
    _check(
        sole_role_for_reply_address(state, _SENDER_INSTANCE) == "",
        "L10: a two-role holder yields NO reply-address role",
    )
    for label, adapter in (
        ("role send", _run_role_send(_state_with_roles(_SENDER_ROLE, _SECOND_ROLE))),
        ("direct send", _run_direct_send(_state_with_roles(_SENDER_ROLE, _SECOND_ROLE))),
    ):
        _check(
            adapter.calls and adapter.calls[0].get("reply_to_role") == "",
            f"L10 ({label}): a MULTI-ROLE sender keeps the instance reply-to "
            f"rather than an arbitrary alphabetical pick (DEF-3)",
        )


def test_lookup_degrades_silently() -> None:
    """A provenance fault must never break a send (matches the ladder's posture)."""

    class _Exploding:
        def query_state(self, *_a: object, **_k: object) -> dict[str, object]:
            raise RuntimeError("provider down")

    _check(
        sole_role_for_reply_address(
            cast("StateManagementInterface", _Exploding()), _SENDER_INSTANCE,
        )
        == "",
        "degrade: a faulting role lookup yields the instance reply-to, not an exception",
    )
    _check(
        sole_role_for_reply_address(None, _SENDER_INSTANCE) == "",
        "degrade: an unbound state service yields the instance reply-to",
    )


def main() -> int:
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    for failure in _FAILURES:
        sys.stdout.write(f"FAIL: {failure}\n")
    sys.stdout.write(
        f"reply_address_role_smoke: {len(_passes)} passed, {len(_FAILURES)} failed\n",
    )
    return 1 if _FAILURES else 0


if __name__ == "__main__":
    raise SystemExit(main())
