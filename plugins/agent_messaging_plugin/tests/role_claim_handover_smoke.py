#!/usr/bin/env python3
"""Unit smoke for REL-04 — role-claim handover notifications (no pytest, no DB).

``peer_claim_role`` previously displaced a prior holder SILENTLY. REL-04 adds
best-effort handover notifications, folded onto the same wake machinery:

  * a DISPLACED prior holder bound to a DIFFERENT instance gets an immediate
    "you have been displaced from role <name> by <agi>" notice (best-effort wake
    + durable persist);
  * the claim MUST NEVER fail if the displaced holder is unreachable — loud log
    instead (displacement often happens BECAUSE the holder is dead);
  * the NEW holder gets a "you now hold role <name> — check your messages"
    confirmation prompting a peer_inbox drain of the role backlog.

Role names are treated as OPAQUE, operator-defined strings throughout (arbitrary
role assertions). Drives the real orchestration (``_notify_role_handover``) and
the real best-effort sender (``_send_handover_notice``) with stubs.

Run:
    .venv/bin/python3 plugins/agent_messaging_plugin/tests/role_claim_handover_smoke.py
"""

from __future__ import annotations

import json
import logging
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT / "ananta" / "src"))
sys.path.insert(0, str(REPO_ROOT / "plugins" / "agent_messaging_plugin" / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _real_state_fake import RealShapeState  # noqa: E402
from ananta.llm.agent_messaging.role_binding import (  # noqa: E402
    AGENT_ROLE_BINDING_NAMESPACE,
    HOLDER_KIND_INFERENCE_PROVIDER,
    HOLDER_KIND_SESSION,
    TABLE_AGENT_ROLE_BINDING,
)

from agent_messaging_plugin import role_claim as role_claim_module  # noqa: E402
from agent_messaging_plugin.bridge_sessions import (  # noqa: E402
    DEFAULT_BINDING_LIVENESS_WINDOW_S,
)
from agent_messaging_plugin.models import BridgeBinding  # noqa: E402
from agent_messaging_plugin.peer_registry import PeerUnreachableError  # noqa: E402
from agent_messaging_plugin.plugin import AgentMessagingPlugin  # noqa: E402
from agent_messaging_plugin.role_binding_store import (  # noqa: E402
    HolderClaim,
    ResolvedRole,
    claim_role_binding_v4,
)
from agent_messaging_plugin.role_claim import (  # noqa: E402
    displaced_prose as _displaced_prose,
)
from agent_messaging_plugin.role_claim import (  # noqa: E402
    new_holder_prose as _new_holder_prose,
)
from agent_messaging_plugin.role_claim import (  # noqa: E402
    notify_role_handover,
    settle_role_handover,
)

# Arbitrary, operator-defined-shaped role — proves opacity (never special-cased).
_ARBITRARY_ROLE = "zz-Ω arbitrary/role #7!"

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


class _Prior:
    """resolve_role_binding-shaped prior holder (v4 adds the stable session id +
    the holder_kind/holder_identity discriminator)."""

    def __init__(
        self,
        agent_id: str,
        agent_instance_id: str,
        agent_session_id: str = "",
        holder_kind: str = HOLDER_KIND_SESSION,
        holder_identity: dict[str, object] | None = None,
    ) -> None:
        self.agent_id = agent_id
        self.agent_instance_id = agent_instance_id
        self.agent_session_id = agent_session_id
        self.session_label = "prior"
        self.holder_kind = holder_kind
        self.holder_identity = holder_identity or {}


class _ListHandler(logging.Handler):
    """Captures emitted LogRecords so a smoke can prove a path LOGGED (not silent)."""

    def __init__(self, sink: list[logging.LogRecord]) -> None:
        super().__init__()
        self._sink = sink

    def emit(self, record: logging.LogRecord) -> None:
        self._sink.append(record)


class _CurrentBinding:
    """A live BridgeBinding the prior session has rotated to (new agi)."""

    def __init__(self, agent_id: str, agent_instance_id: str) -> None:
        self.agent_id = agent_id
        self.agent_instance_id = agent_instance_id


class _FakeRegistry:
    """Resolves a prior holder's stable session id to its CURRENT bridge (§5.4)."""

    def __init__(self, mapping: dict[str, _CurrentBinding]) -> None:
        self._mapping = mapping

    def resolve_by_agent_session_id(self, agent_session_id: str) -> _CurrentBinding | None:
        return self._mapping.get(agent_session_id)


# ---------------------------------------------------------------------------
# Orchestration — _notify_role_handover (real), _send_handover_notice recorded
# ---------------------------------------------------------------------------


class _RecordingNotifier:
    """Records who WOULD be notified, driving the real orchestration.

    ``notify_role_handover`` is a module-level function shared by the
    ``peer_claim_role`` verb and the ``peer/claim_role`` bridge route, so the
    recording seam is the sender it calls rather than a plugin subclass. Both
    transports reach exactly this code — that is the point of the shared body.
    """

    def __init__(self, registry: Any = None) -> None:
        self.notices: list[dict[str, str]] = []
        self._registry = registry

    def _record(
        self,
        *,
        bridge_manager: Any,  # noqa: ARG002
        peer_registry: Any,  # noqa: ARG002
        agent_messaging_service: Any,  # noqa: ARG002
        state_service: Any = None,  # noqa: ARG002
        peer_id: str,
        peer_agent_instance_id: str,
        prose: str,
        kind: str,
        peer_agent_session_id: str = "",
    ) -> bool:
        self.notices.append(
            {
                "peer_id": peer_id,
                "agi": peer_agent_instance_id,
                "session_id": peer_agent_session_id,
                "kind": kind,
                "prose": prose,
            },
        )
        return True

    def notify(self, **kwargs: Any) -> None:
        original = role_claim_module.send_handover_notice
        role_claim_module.send_handover_notice = self._record
        try:
            notify_role_handover(
                bridge_manager=None,
                peer_registry=self._registry,
                agent_messaging_service=None,
                state_service=None,
                **kwargs,
            )
        finally:
            role_claim_module.send_handover_notice = original

    def settle(self, **kwargs: Any) -> dict[str, str]:
        """Drive the real settle and return the PUBLIC payload both transports send.

        ``settle_role_handover`` returns a ``RoleClaimSuccess``; ``to_public()``
        is the one definition of the published shape, so asserting on it covers
        the verb and the bridge route at once.
        """
        original = role_claim_module.send_handover_notice
        role_claim_module.send_handover_notice = self._record
        try:
            return settle_role_handover(
                bridge_manager=None,
                peer_registry=self._registry,
                agent_messaging_service=None,
                state_service=None,
                **kwargs,
            ).to_public()
        finally:
            role_claim_module.send_handover_notice = original


def test_displacement_notifies_both() -> None:
    plugin = _RecordingNotifier()
    plugin.notify(
        name=_ARBITRARY_ROLE,
        new_agent_id="claude_code",
        new_agent_instance_id="agi-new",
        new_agent_session_id="sess-new",
        prior=_Prior("claude_code", "agi-old", "sess-old"),
    )
    kinds = [n["kind"] for n in plugin.notices]
    _check(kinds == ["displaced-holder", "new-holder"], "displacement → displaced + new-holder, in order")
    displaced = plugin.notices[0]
    _check(
        displaced["agi"] == "agi-old",
        "displaced notice targets the PRIOR holder's recorded instance (no live registry → fallback)",
    )
    _check(
        _ARBITRARY_ROLE in displaced["prose"] and "agi-new" in displaced["prose"],
        "displaced notice names the opaque role + the displacing instance",
    )
    _check(plugin.notices[1]["agi"] == "agi-new", "new-holder notice targets the claiming instance")
    _check(
        [notice["session_id"] for notice in plugin.notices] == ["sess-old", "sess-new"],
        "handover notices carry the stable recipient session keys",
    )


def test_first_claim_only_new_holder() -> None:
    plugin = _RecordingNotifier()
    plugin.notify(
        name=_ARBITRARY_ROLE,
        new_agent_id="claude_code",
        new_agent_instance_id="agi-new",
        new_agent_session_id="sess-new",
        prior=None,
    )
    _check(
        [n["kind"] for n in plugin.notices] == ["new-holder"],
        "no prior holder → only the new-holder confirmation",
    )


def test_reconnect_same_session_no_displaced() -> None:
    """REL-07(2): a reconnect ROTATES agent_instance_id but keeps agent_session_id.

    The old instance-id-keyed check saw agi-old != agi-new and fired a spurious
    displaced-notice at the SAME session (the observed self-notify noise). Keyed on
    the stable session id, a same-session re-claim is NOT a displacement.
    """
    plugin = _RecordingNotifier()
    plugin.notify(
        name=_ARBITRARY_ROLE,
        new_agent_id="claude_code",
        new_agent_instance_id="agi-new",
        new_agent_session_id="sess-x",
        prior=_Prior("claude_code", "agi-old", "sess-x"),  # different instance, SAME session
    )
    _check(
        [n["kind"] for n in plugin.notices] == ["new-holder"],
        "reconnect same session (rotated agi) → NO displaced notice (session-id-keyed)",
    )


def test_displaced_notice_routes_to_current_bridge() -> None:
    """§5.4: the displaced-notice targets the prior holder's CURRENT bridge.

    The role binding records the agi as of the claim; by displace time the prior
    session has reconnected under a new agi. Routing via its stable session id
    reaches the live bridge, not the stale recorded instance.
    """
    registry = _FakeRegistry({"sess-old": _CurrentBinding("claude_code", "agi-old-current")})
    plugin = _RecordingNotifier(registry)
    plugin.notify(
        name=_ARBITRARY_ROLE,
        new_agent_id="claude_code",
        new_agent_instance_id="agi-new",
        new_agent_session_id="sess-new",
        prior=_Prior("claude_code", "agi-stale-recorded", "sess-old"),
    )
    displaced = plugin.notices[0]
    _check(
        displaced["agi"] == "agi-old-current",
        "displaced notice routes to the prior session's CURRENT bridge (not the stale recorded agi)",
    )


def test_provider_displacement_logs_loud_no_wake() -> None:
    """§5.4: a displaced inference_provider has NO wake target → LOG-LOUD, not silent.

    A provider consumes no messages, so there is nothing to wake. The transition
    must be audited with a loud log instead of silently dropped (the pre-fix
    behaviour: _is_genuine_displacement returned False for a provider → no notice
    AND no log). The new-holder (a session claimant) confirmation still fires.
    """
    plugin = _RecordingNotifier()
    records: list[logging.LogRecord] = []
    handler = _ListHandler(records)
    plugin_logger = logging.getLogger("agent_messaging_plugin.role_claim")
    plugin_logger.addHandler(handler)
    try:
        plugin.notify(
            name=_ARBITRARY_ROLE,
            new_agent_id="claude_code",
            new_agent_instance_id="agi-new",
            new_agent_session_id="sess-new",
            prior=_Prior(
                "", "", "",
                holder_kind=HOLDER_KIND_INFERENCE_PROVIDER,
                holder_identity={"provider_kind": "anthropic", "provider_ref": "claude-opus"},
            ),
        )
    finally:
        plugin_logger.removeHandler(handler)
    _check(
        [n["kind"] for n in plugin.notices] == ["new-holder"],
        "provider displaced holder → NO displaced wake (no target); new-holder confirmation still fires",
    )
    _check(
        any(r.levelno >= logging.WARNING and "inference_provider" in r.getMessage() for r in records),
        "provider displaced holder → LOG-LOUD (a warning audits the untargetable transition), never silent",
    )


def test_displaced_peer_id_falls_back_to_new_agent() -> None:
    plugin = _RecordingNotifier()
    plugin.notify(
        name="R",
        new_agent_id="codex",
        new_agent_instance_id="agi-new",
        new_agent_session_id="sess-new",
        prior=_Prior("", "agi-old", "sess-old"),  # prior with empty agent_id
    )
    _check(
        plugin.notices[0]["peer_id"] == "codex",
        "displaced notice peer_id falls back to the claiming agent_id when prior agent_id is empty",
    )


# ---------------------------------------------------------------------------
# Best-effort sender — _send_handover_notice (real dispatch, never raises)
# ---------------------------------------------------------------------------


class _PeerResult:
    thread_id = "agt-h"
    message_id = "agm-h"
    cursor = 1


class _DirectService:
    def __init__(self) -> None:
        self.sent: list[Any] = []

    def peer_send(self, request: Any) -> _PeerResult:
        self.sent.append(request)
        return _PeerResult()


def _recip_binding(*, watcher: bool = False) -> BridgeBinding:
    # The REAL binding type, not a hand-rolled stub — dispatch reads binding
    # surface beyond raw fields (``is_watcher``), and a stub silently drifts.
    return BridgeBinding(
        bridge_id="agc-r",
        agent_id="claude_code",
        agent_instance_id="agi-watch-recipient" if watcher else "agi-recipient",
        session_label="lbl watch" if watcher else "lbl",
        parent_pid=99,
        agent_session_id="ases-recipient",
        watcher_declared=watcher,
    )


class _WakeAdapter:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def wake(self, **kwargs: Any) -> str:
        self.calls.append(kwargs)
        return "agc-woke"


class _DirectRegistry:
    def __init__(
        self,
        *,
        online: bool,
        adapter: _WakeAdapter | None,
        session_fallback: bool = False,
    ) -> None:
        self._online = online
        self._adapter = adapter
        self._session_fallback = session_fallback

    def resolve(self, peer_id: str, peer_agent_instance_id: str | None) -> BridgeBinding:
        if not self._online or self._session_fallback:
            raise PeerUnreachableError(f"no binding for {peer_id}/{peer_agent_instance_id}")
        return _recip_binding()

    def resolve_by_agent_session_id(self, agent_session_id: str) -> BridgeBinding | None:
        if self._session_fallback and agent_session_id == "ases-recipient":
            return _recip_binding(watcher=True)
        return None

    def wake_adapter_for(self, agent_id: str) -> _WakeAdapter | None:
        return self._adapter

    def agent_session_id_for_instance(self, agent_instance_id: str) -> str:
        del agent_instance_id
        return "ases-sender"

    def touch_binding(self, agent_instance_id: str) -> int:
        return 0


class _LiveBridge:
    """A4 Amendment 5: binding_is_live now reads ``closed``/``last_seen_at``
    off the bridge for EVERY recipient kind (was watcher-only), so a stand-in
    that ``get()`` returns must carry both — a bare ``None`` return used to be
    safe here (only watcher bindings were checked) and would now
    false-negative every dispatch as unreachable."""

    closed = False
    last_seen_at = datetime.now(UTC).isoformat()
    # H2 quiet-gap capture reads this off the RECIPIENT's live bridge.
    last_model_activity_at = ""

    def touch(self) -> None:
        return


class _DirectManager:

    # WS-2a W3: the dispatch liveness gate reads this off its bridge_manager
    # collaborator, so a stand-in must carry it too.
    @property
    def binding_liveness_window_s(self) -> int:
        return DEFAULT_BINDING_LIVENESS_WINDOW_S
    def get(self, bridge_id: str) -> _LiveBridge:
        del bridge_id
        return _LiveBridge()

    def append_event(
        self, bridge_id: str, event: str, prose: str, meta: dict[str, object],
    ) -> None:
        return None


class _NoticePlugin(AgentMessagingPlugin):
    def __init__(self, registry: Any, manager: Any, service: Any) -> None:
        self._peer_registry = registry
        self._bridge_manager = manager
        self._service = service


def test_notice_delivers_when_live() -> None:
    adapter = _WakeAdapter()
    service = _DirectService()
    plugin = _NoticePlugin(
        _DirectRegistry(online=True, adapter=adapter), _DirectManager(), service,
    )
    delivered = plugin._send_handover_notice(
        peer_id="claude_code",
        peer_agent_instance_id="agi-old",
        prose=_displaced_prose(_ARBITRARY_ROLE, "agi-new"),
        kind="displaced-holder",
    )
    _check(delivered is True, "live recipient → notice delivered (True)")
    _check(len(adapter.calls) == 1, "live recipient → native wake fired once")
    _check(len(service.sent) == 1, "live recipient → message persisted durably (Layer A)")


def test_notice_reaches_watcher_via_stable_session_fallback() -> None:
    """A role-claim notice must reach a parked watcher without a stale-id retry."""
    service = _DirectService()
    delivered = role_claim_module.send_handover_notice(
        bridge_manager=_DirectManager(),  # type: ignore[arg-type]
        peer_registry=_DirectRegistry(  # type: ignore[arg-type]
            online=True,
            adapter=None,
            session_fallback=True,
        ),
        agent_messaging_service=service,
        state_service=RealShapeState(),  # type: ignore[arg-type]
        peer_id="claude_code",
        peer_agent_instance_id="agi-stale-managed",
        peer_agent_session_id="ases-recipient",
        prose="IMPORTANT: role handover",
        kind="new-holder",
    )
    _check(delivered is True, "handover notice resolves a parked watcher by stable session key")
    _check(
        service.sent and service.sent[0].peer_agent_instance_id == "agi-watch-recipient",
        "handover notice persists against the live watcher instance",
    )


def test_notice_unreachable_never_raises_claim_survives() -> None:
    plugin = _NoticePlugin(
        _DirectRegistry(online=False, adapter=None), _DirectManager(), _DirectService(),
    )
    raised = False
    delivered: bool | None = None
    try:
        delivered = plugin._send_handover_notice(
            peer_id="claude_code",
            peer_agent_instance_id="agi-dead",
            prose="IMPORTANT: displaced",
            kind="displaced-holder",
        )
    except Exception:  # noqa: BLE001 — the whole point is it must NOT propagate
        raised = True
    _check(not raised, "unreachable displaced holder → _send_handover_notice does NOT raise")
    _check(delivered is False, "unreachable → returns False (loud log); the claim proceeds")


def test_notice_skipped_when_bridge_not_started() -> None:
    plugin = _NoticePlugin(None, None, _DirectService())
    raised = False
    delivered: bool | None = None
    try:
        delivered = plugin._send_handover_notice(
            peer_id="claude_code",
            peer_agent_instance_id="agi-x",
            prose="IMPORTANT: hi",
            kind="new-holder",
        )
    except Exception:  # noqa: BLE001
        raised = True
    _check(not raised, "bridge not started → no raise")
    _check(delivered is False, "bridge not started → returns False (skipped, loud log)")


# ---------------------------------------------------------------------------
# Role-name opacity — the prose carries arbitrary role strings verbatim
# ---------------------------------------------------------------------------


def test_prose_is_role_agnostic() -> None:
    disp = _displaced_prose(_ARBITRARY_ROLE, "agi-z")
    conf = _new_holder_prose(_ARBITRARY_ROLE)
    _check(_ARBITRARY_ROLE in disp, "displaced prose carries the arbitrary role verbatim")
    _check(_ARBITRARY_ROLE in conf, "new-holder prose carries the arbitrary role verbatim")


def test_new_holder_notice_names_the_pull_contract() -> None:
    """Walkback Fix (B), §7/§10.2 — the notice text is the load-bearing guard.

    (B) makes the role section seed silently on an empty ``role_high_water``;
    that is only safe because a new role holder is told, at claim time, to
    pull its own backlog. The Architect's ruling
    (workbench/2026-08-01_architect_walkback_per_section_seeding_ruling.md
    §10.2) retired the previously-cited guard —
    ``handover_notice_runnable_smoke`` has never existed in the tree or the
    gate register (Claude-B, repo-wide ``rg -F`` sweep, zero test-file hits) —
    and required a NEW content-level leg in its place.

    Asserted on the STRING ``notify_role_handover`` actually dispatches
    through the real orchestration (via ``_RecordingNotifier``, which
    monkeypatches the shared ``send_handover_notice`` seam both the verb and
    the bridge route call), never a source grep: the prose is assembled from
    f-string fragments spanning more than one literal in ``new_holder_prose``,
    so a literal-string search over source would miss a wrapped instruction.
    """
    plugin = _RecordingNotifier()
    plugin.notify(
        name=_ARBITRARY_ROLE,
        new_agent_id="claude_code",
        new_agent_instance_id="agi-new",
        new_agent_session_id="sess-new",
        prior=None,
    )
    prose = plugin.notices[-1]["prose"]
    _check("peer_inbox" in prose, "notice names the peer_inbox process key")
    _check(
        "agent_session_id" in prose,
        "notice tells the holder to pass their own agent_session_id",
    )
    _check(
        "include_important" not in prose,
        "A4 (2026-08-04): notice no longer mentions the retired include_important "
        "parameter",
    )
    _check(
        "role_after" in prose and "next_role_cursor" in prose,
        "notice tells the holder to page role_after until next_role_cursor is null",
    )


def test_new_holder_notice_states_the_attestation_closing_step() -> None:
    """rotation-systematization fix loop (2026-08-08, ``workbench/2026-08-07_
    rotation_systematization_findings_rotation-impl.md``) — the covered-mark
    floor was found permanently inert for every role because nothing ever
    called ``peer_mark_role_covered`` in practice: the verb is registered-
    route-only BY DESIGN, so only the role holder's own live turn can ever
    attest. This notice is the ONE place that instruction can live and reach
    every deployment. Content-level, not a source grep, matching this file's
    own convention (the prose is f-string fragments, not one literal)."""
    plugin = _RecordingNotifier()
    plugin.notify(
        name=_ARBITRARY_ROLE,
        new_agent_id="claude_code",
        new_agent_instance_id="agi-new",
        new_agent_session_id="sess-new",
        prior=None,
    )
    prose = plugin.notices[-1]["prose"]
    _check(
        "peer_mark_role_covered" in prose,
        "notice names the attestation verb by process key",
    )
    _check(
        "message_id" in prose,
        "notice tells the holder to attest by message_id",
    )
    _check(
        "never a computed or assumed" in prose or "never the newest message in the whole backlog" in prose,
        "notice states the correctness condition IN the prose, not merely documented elsewhere "
        "(a safety rule that lives outside the instruction the reader receives gets followed unsafely)",
    )
    _check(
        "registered bridge" in prose,
        "notice names the registered-route requirement for attestation",
    )
    _check(
        "cannot attest" in prose or "cannot perform this step" in prose,
        "notice states plainly, for a session with no registered bridge, that it cannot attest — "
        "never implying a capability the reader may not have",
    )
    _check(
        "attest" in prose.lower(),
        "notice uses a pull/self-attestation verb, not a push framing",
    )


def test_new_holder_notice_is_pull_shaped_never_a_push_claim() -> None:
    """(B) bindings — pin as a leg, not a commit-time grep (Claude-B, binding 5).

    Checked CLEAN at the time: the notice makes no push claim in either
    direction — every verb is pull-shaped (drain, page, waiting) — and (B)'s
    entire safety argument depends on that staying true, since (B) itself
    pushes nothing for an empty role mark. A grep run once is not a guard;
    this pins it.
    """
    plugin = _RecordingNotifier()
    plugin.notify(
        name=_ARBITRARY_ROLE,
        new_agent_id="claude_code",
        new_agent_instance_id="agi-new",
        new_agent_session_id="sess-new",
        prior=None,
    )
    prose = plugin.notices[-1]["prose"]
    _check("drain" in prose.lower(), "notice uses a pull verb (drain)")
    _check(
        not any(
            phrase in prose.lower()
            for phrase in ("will be pushed", "you will receive", "will be sent to you")
        ),
        "notice makes no push-delivery claim",
    )


# ---------------------------------------------------------------------------
# §9 cutover fix-round (Codex): BLOCKER-1 json-serializable public result +
# BLOCKER-2 retired legacy backfill (no post-marker legacy write)
# ---------------------------------------------------------------------------


def test_settle_public_result_is_json_serializable() -> None:
    """BLOCKER-1: the v4 claim outcome carries a ResolvedRole ``prior`` for the notify;
    it must NOT reach the public ActionResult (result-persistence json.dumps would
    TypeError on a real displace). _settle_role_handover returns a plain dict."""
    plugin = _RecordingNotifier()
    outcome = {
        "action": "displaced",
        "name": _ARBITRARY_ROLE,
        "agent_instance_id": "agi-new",
        "prior": ResolvedRole(
            name=_ARBITRARY_ROLE, agent_id="claude_code", agent_instance_id="agi-old",
            session_label="prior", agent_session_id="sess-old",
        ),
    }
    result = plugin.settle(
        name=_ARBITRARY_ROLE, agent_id="claude_code",
        agent_instance_id="agi-new", agent_session_id="sess-new", outcome=outcome,
    )
    _check("prior" not in result, "displace: the ResolvedRole `prior` is STRIPPED from the public result")
    ok = True
    try:
        json.dumps(result)
    except TypeError:
        ok = False
    _check(ok, "the public result is json.dumps-able (survives result persistence — no TypeError)")
    _check(
        result == {
            "action": "displaced", "name": _ARBITRARY_ROLE,
            "agent_instance_id": "agi-new", "agent_session_id": "sess-new",
        },
        "the public result is schema-shaped {action, name, agent_instance_id, agent_session_id}",
    )


def test_settle_public_result_covers_every_declared_schema_property() -> None:
    """``ExecutionContext.store_result`` raises ``PlaceholderResolutionError`` when a
    property declared in ``return_value_schema`` is ABSENT from the result — failing the
    action AFTER the binding write landed, so a claim that actually succeeded is
    reported to the caller as a failure. That is exactly how declaring
    ``agent_session_id`` without adding it to the public envelope broke every
    ``/rename``. This guard is generic: it reads the declared properties off the verb's
    own decorator metadata, so ANY future schema addition that the envelope does not
    carry fails here instead of in production.
    """
    declared = set(
        AgentMessagingPlugin.peer_claim_role._platform_process_metadata.return_value_schema.properties
    )
    plugin = _RecordingNotifier()
    for action in ("claimed", "displaced", "refreshed"):
        outcome = {
            "action": action,
            "name": _ARBITRARY_ROLE,
            "agent_instance_id": "agi-new",
            "prior": ResolvedRole(
                name=_ARBITRARY_ROLE, agent_id="claude_code", agent_instance_id="agi-old",
                session_label="prior", agent_session_id="sess-old",
            ),
        }
        result = plugin.settle(
            name=_ARBITRARY_ROLE, agent_id="claude_code",
            agent_instance_id="agi-new", agent_session_id="sess-new", outcome=outcome,
        )
        missing = declared - set(result)
        _check(
            not missing,
            f"outcome '{action}': the public result carries every declared "
            f"return_value_schema property (missing: {sorted(missing)})",
        )


class _OrchStub:
    """Orchestrator stub: state_service = a RealShapeState; address_book BOUND (so a
    PRE-retirement _run_startup_backfills WOULD have run the legacy role seed)."""

    def __init__(self, state: Any) -> None:
        self._state = state

    def get_service(self, name: str) -> Any:
        if name == "state_service":
            return self._state
        if name == "address_book_service":
            return object()
        return None


class _BackfillPlugin(AgentMessagingPlugin):
    def __init__(self, state: Any) -> None:
        self.orchestrator_ref = _OrchStub(state)
        self.name = "agent_messaging_plugin"


def test_startup_backfills_no_legacy_role_write() -> None:
    """BLOCKER-2: the Control #2 legacy address-book→agent_role_binding seed is RETIRED
    at the §9 cutover. Even with address_book BOUND (which pre-retirement triggered it),
    _run_startup_backfills writes NO legacy row — so it can never strand rows out of v4
    after the migration marker is set."""
    state = RealShapeState()
    _BackfillPlugin(state)._run_startup_backfills()
    legacy = state.rows(AGENT_ROLE_BINDING_NAMESPACE, TABLE_AGENT_ROLE_BINDING)
    _check(
        len(legacy) == 0,
        "startup backfill writes NO legacy agent_role_binding row (the Control #2 role seed is retired at cutover)",
    )


# ---------------------------------------------------------------------------
# The VERB's collaborator binding — production service resolution, end to end
# ---------------------------------------------------------------------------


class _SentinelService:
    """Stands in for the built AgentMessagingService; identity is all we assert."""


class _LazyServicePlugin(AgentMessagingPlugin):
    """A plugin that resolves its service the way PRODUCTION does: lazily.

    ``__init__`` leaves ``_service`` as ``None`` and ``_require_service`` BUILDS
    it on first use. ``_build_service`` is overridden only to avoid standing up a
    whole service graph — ``_require_service`` itself, the method under test, is
    the real one.
    """

    def __init__(self, state: Any, registry: Any) -> None:
        self.orchestrator_ref = _OrchStub(state)
        self.name = "agent_messaging_plugin"
        self._service = None
        self._bridge_manager = cast("Any", object())
        self._peer_registry = registry
        self.builds = 0

    def _build_service(self) -> Any:
        self.builds += 1
        return _SentinelService()


class _SessionIdRegistry:
    """Minimal registry: answers the claimant's session id and prior-holder lookup."""

    def __init__(self, session_id: str) -> None:
        self._session_id = session_id

    def agent_session_id_for_instance(self, agent_instance_id: str) -> str:
        del agent_instance_id
        return self._session_id

    def resolve_by_agent_session_id(self, agent_session_id: str) -> None:
        del agent_session_id
        return None


def test_verb_resolves_its_service_the_way_production_does() -> None:
    """REGRESSION GUARD for a bug that shipped green through every other test here.

    When the claim body was extracted into ``role_claim``, the verb handed it
    ``self._service`` — the raw attribute. That attribute is ``None`` until
    ``_require_service`` BUILDS the service, so the claim would land, report
    SUCCESS, and then silently skip both handover notices with a "bridge not
    started" log. A displaced holder would never learn it lost the role, with no
    error anywhere: the phantom-handover class the two-transport split exists to
    remove, arriving through a different door.

    Nothing else in this file could catch it. The orchestration tests patch the
    sender seam, so the service is never consulted; the cross-transport test in
    peer_claim_role_route_smoke injects a service object directly. Both drive the
    real code and both stay green. So this asserts the ONE thing they cannot:
    that the verb consults the ACCESSOR, and that a live service actually reaches
    the notice sender on a real displacement.
    """
    state = RealShapeState()
    # Seed a DIFFERENT session as the incumbent, so the claim genuinely displaces.
    claim_role_binding_v4(
        cast("Any", state),
        name=_ARBITRARY_ROLE,
        claim=HolderClaim(
            holder_kind=HOLDER_KIND_SESSION,
            holder_identity={"agent_id": "claude_code", "session_label": "prior"},
            agent_instance_id="agi-old",
            agent_session_id="sess-old",
            session_label="prior",
        ),
    )
    plugin = _LazyServicePlugin(state, _SessionIdRegistry("sess-new"))
    seen: list[Any] = []
    original = role_claim_module.send_handover_notice

    def _capture(**kwargs: Any) -> bool:
        seen.append(kwargs.get("agent_messaging_service"))
        return True

    role_claim_module.send_handover_notice = _capture
    try:
        result = plugin.peer_claim_role(
            {
                "name": _ARBITRARY_ROLE,
                "agent_id": "claude_code",
                "agent_instance_id": "agi-new",
                "agent_session_id": "sess-new",
            },
            {},
        )
    finally:
        role_claim_module.send_handover_notice = original

    _check(
        result.get("action_status") == "completed",
        "the displacing claim succeeds through the verb",
    )
    _check(
        len(seen) == 2,
        f"a displacement fires BOTH notices through the verb (fired {len(seen)})",
    )
    _check(
        seen and all(svc is not None for svc in seen),
        "every notice receives a NON-None service — the raw `_service` attribute "
        "would have delivered None here and silently dropped the wake",
    )
    _check(
        plugin.builds == 1 and all(isinstance(svc, _SentinelService) for svc in seen),
        "the verb consulted _require_service (which BUILT the service), not the "
        "unpopulated attribute",
    )


def main() -> int:
    print("=== REL-04 role-claim handover notifications smoke ===")
    test_displacement_notifies_both()
    test_first_claim_only_new_holder()
    test_reconnect_same_session_no_displaced()
    test_displaced_notice_routes_to_current_bridge()
    test_provider_displacement_logs_loud_no_wake()
    test_displaced_peer_id_falls_back_to_new_agent()
    test_notice_delivers_when_live()
    test_notice_reaches_watcher_via_stable_session_fallback()
    test_notice_unreachable_never_raises_claim_survives()
    test_notice_skipped_when_bridge_not_started()
    test_prose_is_role_agnostic()
    test_new_holder_notice_names_the_pull_contract()
    test_new_holder_notice_states_the_attestation_closing_step()
    test_new_holder_notice_is_pull_shaped_never_a_push_claim()
    test_settle_public_result_is_json_serializable()
    test_settle_public_result_covers_every_declared_schema_property()
    test_startup_backfills_no_legacy_role_write()
    test_verb_resolves_its_service_the_way_production_does()
    print(f"\n{_passed} passed, {len(_failed)} failed")
    if _failed:
        for label in _failed:
            print(f"  FAILED: {label}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
