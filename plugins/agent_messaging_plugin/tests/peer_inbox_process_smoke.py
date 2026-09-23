#!/usr/bin/env python3
"""Smoke coverage for the ``peer_inbox`` platform process.

Covers the layer the process itself owns — caller-identity resolution, the two
independent cursors, the page bound, the loud-failure set, and the serialized
shape — plus the two advertised strings that instruct sessions to call it.
The service's own ``peer_inbox`` query is covered by
``peer_inbox_session_visibility_smoke``; this file does not re-prove it.

Every check below names the mutation that turns it red:

- identity resolution      → return the caller's args instead of the binding's
- inactive plugin          → delegate to ``AgentMessagingPlugin.peer_inbox``
                             (which returns an EMPTY page when inactive, making
                             "messaging is off" read as "you have no mail")
- unknown / duplicate id   → fall back to an empty page instead of failing
- ``after`` parse          → swallow the ValueError and page from the start
- ``role_after`` pass-thru → forward ``after`` into the role cursor
- limit bound              → drop the clamp (a 50/50 page measured 422,513 chars)
- serialized shape         → emit the enum member instead of its ``.value``
- advertised strings       → revert either literal to its identity-less form

Run:
    SOLET_NAME=<name>-test .venv/bin/python3 \
        plugins/agent_messaging_plugin/tests/peer_inbox_process_smoke.py
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT / "ananta" / "src"))
sys.path.insert(0, str(REPO_ROOT / "plugins" / "agent_messaging_plugin" / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _real_state_fake import RealShapeState  # noqa: E402
from ananta.interfaces.state_management_interface import (  # noqa: E402, TC002
    StateManagementInterface,
)
from ananta.llm.agent_messaging.models import (  # noqa: E402
    AgentMessageRow,
    MessageKind,
    MessageRole,
    PeerInbox,
    PeerInboxEntry,
    PeerInboxRequest,
    RoleSectionStatus,
    RoleTruncationReason,
    TextPart,
)
from ananta.llm.agent_messaging.role_binding import (  # noqa: E402
    HOLDER_KIND_SESSION,
)
from ananta.llm.agent_messaging.service import (  # noqa: E402
    AgentRequestInvalidError,
)
from ananta.services.store import Store, open_store  # noqa: E402

from agent_messaging_plugin import role_claim  # noqa: E402
from agent_messaging_plugin.local_cli.wake import (  # noqa: E402
    WakeTarget,
    _compose_wake_packet,
)
from agent_messaging_plugin.models import BridgeBinding  # noqa: E402
from agent_messaging_plugin.peer_dispatch import IMPORTANT_MARKER_RE  # noqa: E402
from agent_messaging_plugin.peer_registry import PeerRegistry  # noqa: E402
from agent_messaging_plugin.plugin import (  # noqa: E402
    PEER_INBOX_DEFAULT_LIMIT,
    PEER_INBOX_MAX_LIMIT,
    PEER_INBOX_MIN_LIMIT,
    AgentMessagingPlugin,
)
from agent_messaging_plugin.role_binding_store import (  # noqa: E402
    HolderClaim,
    claim_role_binding_v4,
)
from agent_messaging_plugin.role_claim import new_holder_prose  # noqa: E402
from agent_messaging_plugin.schema import (  # noqa: E402
    PEER_BINDING_NAMESPACE,
    get_peer_binding_schema,
)

_passed = 0
_failed: list[str] = []

_SESSION_ID = "ases-1785431733-69343-11170"
_INSTANCE_ID = "agi-525b262263df6f378c0e0cade61e3306"
_AGENT_ID = "claude_code"
_ROLE = "zz-Ω arbitrary/role #7!"


def _check(condition: object, label: str) -> None:
    global _passed
    if condition:
        _passed += 1
        print(f"  PASS  {label}")
        return
    _failed.append(label)
    print(f"  FAIL  {label}")


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


class _RecordingService:
    """Stands in for ``AgentMessagingService``, capturing the request it got.

    The process's job is to build a correct ``PeerInboxRequest`` from caller
    args plus the resolved binding; capturing the request is how that job is
    observed. ``raises`` models the service rejecting a malformed role cursor.
    """

    def __init__(self, page: PeerInbox, raises: Exception | None = None) -> None:
        self.page = page
        self.raises = raises
        self.seen: PeerInboxRequest | None = None

    def peer_inbox(self, request: PeerInboxRequest) -> PeerInbox:
        self.seen = request
        if self.raises is not None:
            raise self.raises
        return self.page


def _message(message_id: str, text: str, created_at: datetime) -> AgentMessageRow:
    return AgentMessageRow(
        id=message_id,
        thread_id="agt-1",
        cursor=1,
        role=MessageRole.ORIGINATOR,
        kind=MessageKind.MESSAGE,
        content=[TextPart(type="text", text=text)],
        created_at=created_at,
    )


def _entry(message_id: str, text: str, created_at: datetime) -> PeerInboxEntry:
    return PeerInboxEntry(
        thread_id="agt-1",
        sender_agent_id="codex",
        sender_agent_instance_id="agi-sender",
        sender_session_label="Coordinator-Dawn",
        message=_message(message_id, text, created_at),
    )


_CREATED_AT = datetime(2026, 8, 1, 0, 4, 0, tzinfo=UTC)


def _page(
    *,
    next_role_cursor: str | None = "rc-2",
    status: RoleSectionStatus = RoleSectionStatus.OK,
    role_limit: int = 5,
    role_page_truncated: bool = True,
    role_truncation_reason: RoleTruncationReason | None = (
        RoleTruncationReason.ROW_LIMIT
    ),
    role_byte_ceiling: int | None = None,
) -> PeerInbox:
    return PeerInbox(
        recipient_agent_id=_AGENT_ID,
        entries=(_entry("msg-i", "instance mail", _CREATED_AT),),
        next_after_created_at=_CREATED_AT,
        instance_exhausted=False,
        role_entries=(_entry("msg-r", "role mail", _CREATED_AT),),
        next_role_cursor=next_role_cursor,
        role_section_status=status,
        role_limit=role_limit,
        role_page_truncated=role_page_truncated,
        role_truncation_reason=role_truncation_reason,
        role_byte_ceiling=role_byte_ceiling,
    )


def _registry() -> PeerRegistry:
    store: Store = open_store(
        get_peer_binding_schema(),
        namespace=PEER_BINDING_NAMESPACE,
        backend="in_memory",
    )
    return PeerRegistry(bindings_store=store)


def _binding(
    *,
    bridge_id: str = "agc-1",
    agent_instance_id: str = _INSTANCE_ID,
    agent_session_id: str = _SESSION_ID,
    session_label: str = "Claude-B",
) -> BridgeBinding:
    return BridgeBinding(
        bridge_id=bridge_id,
        agent_id=_AGENT_ID,
        agent_instance_id=agent_instance_id,
        session_label=session_label,
        parent_pid=69343,
        agent_session_id=agent_session_id,
    )


def _plugin(
    service: _RecordingService | None,
    *,
    registry: PeerRegistry | None = None,
    active: bool = True,
) -> AgentMessagingPlugin:
    plugin = AgentMessagingPlugin()
    plugin._active = active  # noqa: SLF001
    plugin._peer_registry = registry  # noqa: SLF001
    if service is not None:
        plugin._service = cast("Any", service)  # noqa: SLF001
    return plugin


def _call(plugin: AgentMessagingPlugin, **args: object) -> dict[str, Any]:
    return plugin.peer_inbox_action(dict(args), {})


def _state() -> tuple[RealShapeState, StateManagementInterface]:
    fake = RealShapeState()
    return fake, cast("StateManagementInterface", fake)


# ---------------------------------------------------------------------------
# Identity — the caller may only ever read its OWN inbox
# ---------------------------------------------------------------------------


def test_identity_comes_from_the_binding_not_the_caller() -> None:
    registry = _registry()
    registry.register(_binding())
    service = _RecordingService(_page())
    result = _call(
        _plugin(service, registry=registry), agent_session_id=_SESSION_ID,
    )
    _check(result["action_status"] == "completed", "resolved identity succeeds")
    seen = service.seen
    _check(seen is not None, "the service was actually called")
    assert seen is not None
    _check(
        seen.recipient_agent_id == _AGENT_ID
        and seen.recipient_agent_instance_id == _INSTANCE_ID
        and seen.recipient_agent_session_id == _SESSION_ID,
        "recipient triple is read off the binding row",
    )
    _check(
        result["data"]["recipient_agent_instance_id"] == _INSTANCE_ID,
        "the resolved instance id is echoed back to the caller",
    )


def test_unregistered_session_fails_loud() -> None:
    service = _RecordingService(_page())
    result = _call(
        _plugin(service, registry=_registry()), agent_session_id=_SESSION_ID,
    )
    _check(result["action_status"] == "failed", "unknown session id fails")
    _check(
        result["error"]["code"] == "identity_not_registered",
        "unknown session id is identity_not_registered",
    )
    _check(
        service.seen is None,
        "no inbox read is attempted for an unresolvable identity",
    )


def test_duplicate_binding_for_one_session_fails_loud() -> None:
    registry = _registry()
    # ``register`` sweeps by bridge_id / agent_instance_id / session_label but
    # NOT by agent_session_id, so distinct labels are what actually produce the
    # duplicate this verb has to survive.
    registry.register(
        _binding(bridge_id="agc-1", agent_instance_id="agi-a", session_label="A"),
    )
    registry.register(
        _binding(bridge_id="agc-2", agent_instance_id="agi-b", session_label="B"),
    )
    result = _call(
        _plugin(_RecordingService(_page()), registry=registry),
        agent_session_id=_SESSION_ID,
    )
    _check(
        result["action_status"] == "failed"
        and result["error"]["code"] == "peer_session_ambiguous",
        "two live bindings for one session id is a loud data fault",
    )


def test_missing_session_id_fails_loud() -> None:
    registry = _registry()
    registry.register(_binding())
    result = _call(_plugin(_RecordingService(_page()), registry=registry))
    _check(
        result["action_status"] == "failed"
        and result["error"]["code"] == "missing_argument",
        "an absent agent_session_id is missing_argument",
    )


def test_inactive_plugin_errors_rather_than_reporting_an_empty_inbox() -> None:
    """The named red mutation for the silent-empty trap.

    ``AgentMessagingPlugin.peer_inbox`` (the typed interface delegation)
    returns an EMPTY ``PeerInbox`` when the plugin is inactive. Routing this
    process through it would make "the messaging system is off" indistinguish-
    able from "you have no mail" — a silent false negative on the one verb
    whose whole job is not missing messages.
    """
    registry = _registry()
    registry.register(_binding())
    result = _call(
        _plugin(_RecordingService(_page()), registry=registry, active=False),
        agent_session_id=_SESSION_ID,
    )
    _check(
        result["action_status"] == "failed",
        "an inactive bridge fails instead of returning a page",
    )
    _check(
        result["error"]["code"] == "bridge.not_running",
        "an inactive bridge is bridge.not_running",
    )
    _check(
        result["data"] == {},
        "an inactive bridge returns NO entries key to be misread as empty mail",
    )
    # The trap itself, asserted directly: the delegation really does swallow.
    inactive = _plugin(None, registry=registry, active=False)
    swallowed = inactive.peer_inbox(
        PeerInboxRequest(
            recipient_agent_id=_AGENT_ID,
            recipient_agent_instance_id=_INSTANCE_ID,
        ),
    )
    _check(
        swallowed.entries == (),
        "the typed delegation DOES return an empty page when inactive "
        "(the reason this process must not route through it)",
    )


# ---------------------------------------------------------------------------
# The two independent cursors
# ---------------------------------------------------------------------------


def test_cursors_are_independent_and_never_mixed() -> None:
    registry = _registry()
    registry.register(_binding())
    service = _RecordingService(_page())
    _call(
        _plugin(service, registry=registry),
        agent_session_id=_SESSION_ID,
        after="2026-08-01T00:04:00+00:00",
        role_after="rc-1",
    )
    seen = service.seen
    assert seen is not None
    _check(
        seen.after_created_at == _CREATED_AT,
        "'after' parses to the instance-section datetime cursor",
    )
    _check(
        seen.role_after == "rc-1",
        "'role_after' is forwarded verbatim as the role cursor",
    )


def test_absent_cursors_are_none_not_empty_strings() -> None:
    registry = _registry()
    registry.register(_binding())
    service = _RecordingService(_page())
    _call(_plugin(service, registry=registry), agent_session_id=_SESSION_ID)
    seen = service.seen
    assert seen is not None
    _check(
        seen.after_created_at is None and seen.role_after is None,
        "omitted cursors mean 'first page', not an empty-string cursor",
    )


def test_malformed_after_fails_loud() -> None:
    registry = _registry()
    registry.register(_binding())
    service = _RecordingService(_page())
    result = _call(
        _plugin(service, registry=registry),
        agent_session_id=_SESSION_ID,
        after="last tuesday",
    )
    _check(
        result["action_status"] == "failed"
        and result["error"]["code"] == "invalid_after",
        "a malformed 'after' fails rather than silently re-reading page one",
    )
    _check(service.seen is None, "no read is attempted on a broken cursor")


def test_a_service_level_rejection_surfaces_as_an_error() -> None:
    """If the service raises, the verb reports it — it never returns a half page.

    NOTE, measured live on 2026-08-01 and NOT what this lane first assumed: a
    malformed ``role_after`` does NOT take this path. The role section has its
    own fault domain, so a bad role cursor comes back as a SUCCESSFUL call
    carrying ``role_section_status="error"`` while the instance section is
    still served (see ``test_a_failed_role_section_still_serves_instance_mail``).
    The card originally attributed the role-cursor case to ``peer_inbox_rejected``
    and was corrected. This test keeps the generic raise mapped, because the
    service can still raise for other reasons.
    """
    registry = _registry()
    registry.register(_binding())
    service = _RecordingService(
        _page(), raises=AgentRequestInvalidError("service-level rejection"),
    )
    result = _call(
        _plugin(service, registry=registry),
        agent_session_id=_SESSION_ID,
    )
    _check(
        result["action_status"] == "failed"
        and result["error"]["code"] == "peer_inbox_rejected",
        "a service rejection surfaces as peer_inbox_rejected, not as a page",
    )


def test_a_failed_role_section_still_serves_instance_mail() -> None:
    """The v10 fault-domain boundary, as the card now describes it.

    A caller that reads only the envelope sees success and an empty
    ``role_entries`` and concludes "no role mail". The truth is "the role read
    FAILED and those messages are unread". Red mutation: drop
    ``role_section_status`` / ``role_section_error`` from the serialized page —
    the call still succeeds and the distinction becomes unrecoverable.
    """
    registry = _registry()
    registry.register(_binding())
    failed = PeerInbox(
        recipient_agent_id=_AGENT_ID,
        entries=(_entry("msg-i", "instance mail", _CREATED_AT),),
        next_after_created_at=_CREATED_AT,
        instance_exhausted=False,
        role_entries=(),
        next_role_cursor=None,
        role_section_status=RoleSectionStatus.ERROR,
        role_section_error="AgentRequestInvalidError(\"role cursor is not valid base64\")",
    )
    data = _call(
        _plugin(_RecordingService(failed), registry=registry),
        agent_session_id=_SESSION_ID,
        role_after="forged-token",
    )["data"]
    _check(
        data["role_section_status"] == "error",
        "a failed role section reports 'error', on an otherwise successful call",
    )
    _check(
        data["role_section_error"] is not None,
        "the failure detail is carried, not swallowed",
    )
    _check(
        len(data["entries"]) == 1,
        "the instance section is STILL served when the role section fails",
    )
    _check(
        data["role_entries"] == [] and data["next_role_cursor"] is None,
        "an empty role section with status 'error' is unread mail, NOT no mail",
    )


# ---------------------------------------------------------------------------
# The page bound — measured 2026-08-01 at ~4KB per entry
# ---------------------------------------------------------------------------


def test_limit_defaults_and_clamps() -> None:
    registry = _registry()
    registry.register(_binding())
    cases: list[tuple[dict[str, object], int, str]] = [
        ({}, PEER_INBOX_DEFAULT_LIMIT, "omitted limit uses the modest default"),
        ({"limit": 20}, 20, "an in-range limit passes through"),
        ({"limit": 5000}, PEER_INBOX_MAX_LIMIT, "an oversized limit clamps down"),
        ({"limit": 0}, PEER_INBOX_MIN_LIMIT, "a zero limit clamps up to one"),
        ({"limit": -3}, PEER_INBOX_MIN_LIMIT, "a negative limit clamps up to one"),
        (
            {"limit": "lots"},
            PEER_INBOX_DEFAULT_LIMIT,
            "a non-numeric limit falls back to the default",
        ),
        (
            {"limit": True},
            PEER_INBOX_DEFAULT_LIMIT,
            "a bool limit is not read as 1 (bool is an int in Python)",
        ),
    ]
    for args, expected, label in cases:
        service = _RecordingService(_page())
        _call(
            _plugin(service, registry=registry),
            agent_session_id=_SESSION_ID,
            **args,
        )
        seen = service.seen
        assert seen is not None
        _check(seen.limit == expected, f"{label} ({seen.limit} == {expected})")
    _check(
        PEER_INBOX_DEFAULT_LIMIT < PEER_INBOX_MAX_LIMIT,
        "the default is a page size, well under the ceiling",
    )


def test_include_important_is_always_true_now() -> None:
    """A4 (2026-08-04): the silent/important split at send time is retired,
    so the catch-up view is the only meaningful one. include_important is no
    longer read from the caller — a caller-supplied False is silently
    ignored (extra key, no effect), never honoured."""
    registry = _registry()
    registry.register(_binding())
    for args, label in (
        ({}, "omitted include_important -> the catch-up view (unchanged)"),
        (
            {"include_important": False},
            "A4: a caller-supplied include_important=False is now IGNORED "
            "(never read) — the request still carries True",
        ),
    ):
        service = _RecordingService(_page())
        _call(
            _plugin(service, registry=registry),
            agent_session_id=_SESSION_ID,
            **cast("dict[str, object]", args),
        )
        seen = service.seen
        assert seen is not None
        _check(seen.include_important is True, label)


# ---------------------------------------------------------------------------
# The serialized shape the return_value_schema declares
# ---------------------------------------------------------------------------


_EXPECTED_KEYS = {
    "recipient_agent_id",
    "recipient_agent_instance_id",
    "entries",
    "next_after_created_at",
    "instance_exhausted",
    "role_entries",
    "next_role_cursor",
    "role_section_status",
    "role_section_error",
    "role_limit",
    "role_page_truncated",
    "role_truncation_reason",
    "role_byte_ceiling",
    # Pull-surface boundary (design workbench/2026-08-02_pull_surface_boundary_design_claude_d.md
    # §5) — additive, False/None until a session calls peer_mark_role_covered.
    "role_floor_applied",
    "role_history_cursor",
    "role_read_page_token",
    "role_read_page_status",
}


def test_serialized_page_matches_the_declared_schema() -> None:
    registry = _registry()
    registry.register(_binding())
    plugin = _plugin(_RecordingService(_page()), registry=registry)
    data = _call(plugin, agent_session_id=_SESSION_ID)["data"]
    _check(
        set(data) == _EXPECTED_KEYS,
        "the returned keys are exactly the declared ones",
    )
    declared = plugin.peer_inbox_action._platform_process_metadata  # noqa: SLF001
    schema_keys = set(declared.return_value_schema.properties)
    _check(
        schema_keys == _EXPECTED_KEYS,
        "return_value_schema declares exactly the keys the code returns",
    )
    _check(
        declared.name == "peer_inbox",
        "the decorator registers the verb as 'peer_inbox' (not the method name)",
    )


def test_role_section_status_serializes_to_its_lowercase_value() -> None:
    registry = _registry()
    registry.register(_binding())
    data = _call(
        _plugin(_RecordingService(_page()), registry=registry),
        agent_session_id=_SESSION_ID,
    )["data"]
    _check(
        data["role_section_status"] == "ok",
        "role_section_status is the token 'ok', not the enum member",
    )
    _check(
        data["next_role_cursor"] == "rc-2",
        "a non-null next_role_cursor means the role section is NOT drained "
        "even though status is 'ok'",
    )
    drained = _call(
        _plugin(
            _RecordingService(_page(next_role_cursor=None)), registry=registry,
        ),
        agent_session_id=_SESSION_ID,
    )["data"]
    _check(
        drained["next_role_cursor"] is None,
        "exhaustion is next_role_cursor == null",
    )
    _check(
        drained["role_section_status"] == "ok",
        "the SAME 'ok' status covers both drained and undrained — proving "
        "status cannot be read as progress",
    )


def test_role_page_truncation_metadata_serializes() -> None:
    registry = _registry()
    registry.register(_binding())
    row_limited = _call(
        _plugin(_RecordingService(_page()), registry=registry),
        agent_session_id=_SESSION_ID,
    )["data"]
    _check(
        row_limited["role_limit"] == 5
        and row_limited["role_page_truncated"] is True
        and row_limited["role_truncation_reason"] == "row_limit"
        and row_limited["role_byte_ceiling"] is None,
        "a row-limited role page carries its effective limit and reason",
    )
    byte_limited = _call(
        _plugin(
            _RecordingService(
                _page(
                    role_truncation_reason=RoleTruncationReason.BYTE_CEILING,
                    role_byte_ceiling=200_000,
                ),
            ),
            registry=registry,
        ),
        agent_session_id=_SESSION_ID,
    )["data"]
    _check(
        byte_limited["role_truncation_reason"] == "byte_ceiling"
        and byte_limited["role_byte_ceiling"] == 200_000,
        "a byte-limited role page carries the applicable ceiling",
    )


def test_entries_carry_sender_identity_and_isoformat_timestamps() -> None:
    registry = _registry()
    registry.register(_binding())
    data = _call(
        _plugin(_RecordingService(_page()), registry=registry),
        agent_session_id=_SESSION_ID,
    )["data"]
    entry = data["entries"][0]
    _check(
        entry["sender_agent_instance_id"] == "agi-sender",
        "an entry carries the sender instance id a reply needs",
    )
    _check(
        entry["message"]["content"] == [{"type": "text", "text": "instance mail"}],
        "message content survives serialization as parts",
    )
    _check(
        entry["message"]["created_at"] == _CREATED_AT.isoformat(),
        "timestamps serialize as ISO-8601 strings",
    )
    _check(
        data["next_after_created_at"] == _CREATED_AT.isoformat(),
        "the instance cursor is a round-trippable ISO-8601 string",
    )
    _check(
        data["instance_exhausted"] is False,
        "instance_exhausted is emitted as a boolean progress signal",
    )
    _check(
        data["role_entries"][0]["message"]["id"] == "msg-r",
        "the role section is emitted separately from the instance section",
    )


def test_read_touches_the_callers_binding() -> None:
    registry = _registry()
    registry.register(_binding())
    before = registry.resolve_by_agent_session_id(_SESSION_ID)
    assert before is not None
    _call(
        _plugin(_RecordingService(_page()), registry=registry),
        agent_session_id=_SESSION_ID,
    )
    after = registry.resolve_by_agent_session_id(_SESSION_ID)
    assert after is not None
    _check(
        after.updated_at >= before.updated_at,
        "reading the inbox keeps the binding's liveness in step",
    )


# ---------------------------------------------------------------------------
# 24.3 — delivery_route_attached on peer_holds_role
# ---------------------------------------------------------------------------


def _claim(state: StateManagementInterface, *, agent_instance_id: str) -> None:
    claim_role_binding_v4(
        state,
        name=_ROLE,
        claim=HolderClaim(
            holder_kind=HOLDER_KIND_SESSION,
            holder_identity={"agent_id": _AGENT_ID},
            agent_instance_id=agent_instance_id,
            agent_session_id=_SESSION_ID,
            session_label="Claude-B",
        ),
    )


class _FakeBridgeManager:
    def __init__(self, bridges: dict[str, object]) -> None:
        self._bridges = bridges

    def get(self, bridge_id: str) -> object | None:
        return self._bridges.get(bridge_id)


class _FakeBridge:
    def __init__(self, *, closed: bool) -> None:
        self.closed = closed


def _holds_role_plugin(
    state: StateManagementInterface,
    registry: PeerRegistry,
    bridges: dict[str, object],
) -> AgentMessagingPlugin:
    plugin = _plugin(None, registry=registry)
    plugin._bridge_manager = cast("Any", _FakeBridgeManager(bridges))  # noqa: SLF001
    plugin._get_state_service = lambda: state  # type: ignore[method-assign]  # noqa: SLF001
    return plugin


def test_delivery_route_attached_reports_the_holders_route() -> None:
    _, state = _state()
    _claim(state, agent_instance_id=_INSTANCE_ID)
    registry = _registry()
    registry.register(_binding())

    live = _holds_role_plugin(
        state, registry, {"agc-1": _FakeBridge(closed=False)},
    )
    result = live.peer_holds_role(
        {"name": _ROLE, "agent_instance_id": _INSTANCE_ID}, {},
    )
    _check(
        result["data"]["delivery_route_attached"] is True,
        "a holder with an open bridge has a delivery route attached",
    )
    _check(result["data"]["holds"] is True, "the holder still holds the role")

    closed = _holds_role_plugin(
        state, registry, {"agc-1": _FakeBridge(closed=True)},
    )
    _check(
        closed.peer_holds_role(
            {"name": _ROLE, "agent_instance_id": _INSTANCE_ID}, {},
        )["data"]["delivery_route_attached"]
        is False,
        "a closed bridge is NOT a delivery route (the claim outlived it)",
    )

    gone = _holds_role_plugin(state, registry, {})
    outcome = gone.peer_holds_role(
        {"name": _ROLE, "agent_instance_id": _INSTANCE_ID}, {},
    )
    _check(
        outcome["data"]["delivery_route_attached"] is False,
        "a holder whose bridge is gone has no route",
    )
    _check(
        outcome["data"]["holds"] is True,
        "holds=True with delivery_route_attached=False is the 24.3 case — "
        "the two are reported independently",
    )


def test_holds_survives_every_fault_the_route_lookup_can_raise() -> None:
    """``holds`` is Step-9.5's safety answer; the additive field must not eat it.

    ``resolve_by_agent_session_id`` raises ``PeerSessionAmbiguousError`` on a
    duplicate binding — and the duplicate is constructible (``register`` sweeps
    by bridge_id / agent_instance_id / session_label, NOT by session id, as
    ``test_duplicate_binding_for_one_session_fails_loud`` shows). Before the
    helper was made total, that exception escaped from inside the success dict,
    after ``holds`` had been computed and before anything was returned: an
    "additive" field turning Git-Controller's pre-commit ownership check into a
    crash. Red mutation: drop ``PeerSessionAmbiguousError`` from the helper's
    except clause.
    """
    _, state = _state()
    _claim(state, agent_instance_id=_INSTANCE_ID)
    registry = _registry()
    # The caller's own row (so _claimant_session_id resolves and `holds` is
    # genuinely True), plus two siblings sharing its session id so the ROUTE
    # lookup is the thing that faults — not the ownership comparison.
    registry.register(_binding())
    registry.register(
        _binding(bridge_id="agc-2", agent_instance_id="agi-a", session_label="A"),
    )
    registry.register(
        _binding(bridge_id="agc-3", agent_instance_id="agi-b", session_label="B"),
    )
    plugin = _holds_role_plugin(
        state, registry, {"agc-1": _FakeBridge(closed=False)},
    )
    result = plugin.peer_holds_role(
        {"name": _ROLE, "agent_instance_id": _INSTANCE_ID}, {},
    )
    _check(
        result["action_status"] == "completed",
        "a duplicate binding does not crash the ownership re-check",
    )
    _check(
        result["data"]["holds"] is True,
        "the safety answer survives a fault in the advisory lookup",
    )
    _check(
        result["data"]["delivery_route_attached"] is False,
        "an unresolvable route reports False rather than raising",
    )


def test_delivery_route_attached_is_false_for_a_vacant_role() -> None:
    _, state = _state()
    registry = _registry()
    registry.register(_binding())
    plugin = _holds_role_plugin(
        state, registry, {"agc-1": _FakeBridge(closed=False)},
    )
    result = plugin.peer_holds_role(
        {"name": "never-claimed", "agent_instance_id": _INSTANCE_ID}, {},
    )
    _check(
        result["data"]["delivery_route_attached"] is False,
        "a vacant role has no holder and therefore no route",
    )


# ---------------------------------------------------------------------------
# The seed-to-newest dependency (census D1 §4a) — pinned, not just documented
# ---------------------------------------------------------------------------


def test_handover_notice_is_the_pinned_backlog_dependency() -> None:
    """D1's seed-to-newest is SAFE ONLY BECAUSE this notice exists and runs.

    Once the watcher's arm-time drain seeds to newest and spools nothing, a
    brand-new role holder is never woken about the backlog waiting for its
    role. The only thing standing between that and silence is this notice:
    live-delivered at claim time, and naming a call the holder can actually
    run. "If either regresses it goes dark silently" is the sentence that
    means a guard is owed — this is the guard.

    Three legs, because there are three ways to lose it:

    (a) it FIRES     — the claim path dispatches it at all
    (b) it is COSMETICALLY MARKED — the notice still leads with IMPORTANT
        for a human reader's benefit; A4 (2026-08-04) retired the marker as a
        wake gate, so this is no longer load-bearing for delivery (dispatch
        is now unconditional — leg (a) alone guarantees the wake attempt),
        but a regression here would still be a readability loss worth
        catching.
    (c) it is RUNNABLE — the key it names is the verb's OWN registered name and
        every REQUIRED parameter is mentioned

    Leg (c) binds to the DECORATOR METADATA rather than a copied string, so it
    catches the WS-1a defect class recurring: someone adds a required argument
    and a shipped instruction silently becomes unrunnable. It asserts every
    required parameter, NOT merely that the key appears — a key-only check
    passes straight through that mutation.

    Deliberately NOT asserted: the English wording. Word-matching
    "backlog"/"waiting" is brittle — it fails on a legitimate rewrite while
    passing every mutation that matters.
    """
    prose = new_holder_prose(_ROLE)

    # (a) it FIRES — asserted BEHAVIOURALLY by driving the real claim-settle
    # path, not by reading the leaf function's source. The source check this
    # replaces was too weak for the scenario it guards: it would have stayed
    # green if `settle_role_handover` simply STOPPED CALLING
    # `notify_role_handover`, or if the idempotent-self-re-claim early return
    # broadened to swallow genuine claims. A live contention capture on
    # 2026-08-01 stranded 5 role messages and the notice was the only thing
    # that recovered them, so this leg has to bind the CALL PATH.
    sent: list[dict[str, object]] = []

    def _record(**kwargs: object) -> bool:
        sent.append(kwargs)
        return True

    with_recorder = patch.object(role_claim, "send_handover_notice", _record)
    with with_recorder:
        role_claim.settle_role_handover(
            bridge_manager=cast("Any", object()),
            peer_registry=cast("Any", object()),
            agent_messaging_service=cast("Any", object()),
            state_service=cast("Any", object()),
            name=_ROLE,
            agent_id=_AGENT_ID,
            agent_instance_id=_INSTANCE_ID,
            agent_session_id=_SESSION_ID,
            # A genuine contended displacement — the capture's shape.
            outcome={"action": "displaced", "prior": None},
        )
    new_holder = [c for c in sent if c.get("kind") == "new-holder"]
    _check(
        len(new_holder) == 1,
        f"a genuine claim DISPATCHES exactly one new-holder notice (got {len(sent)})",
    )
    _check(
        bool(new_holder) and _ROLE in str(new_holder[0].get("prose", "")),
        "the dispatched notice carries this role's own prose",
    )

    # ...and the self-re-claim branch still fires NOTHING, so a steady-state
    # re-assert cannot wake its own session forever. Both halves matter: this is
    # the branch whose broadening would silence (a) without touching any prose.
    quiet: list[dict[str, object]] = []
    with patch.object(
        role_claim, "send_handover_notice",
        lambda **kw: quiet.append(kw) or True,  # type: ignore[func-returns-value]
    ):
        role_claim.settle_role_handover(
            bridge_manager=cast("Any", object()),
            peer_registry=cast("Any", object()),
            agent_messaging_service=cast("Any", object()),
            state_service=cast("Any", object()),
            name=_ROLE,
            agent_id=_AGENT_ID,
            agent_instance_id=_INSTANCE_ID,
            agent_session_id=_SESSION_ID,
            outcome={"action": "refreshed"},
        )
    _check(
        not quiet,
        "an idempotent self-re-claim fires NO notice (the refresh contract)",
    )

    # (b) it is COSMETICALLY MARKED — A4 retired the marker as a wake gate
    # (dispatch is unconditional; leg (a) alone now guarantees delivery is
    # attempted), so this no longer guards a silent-delivery failure mode.
    # Kept as a readability regression guard only.
    _check(
        IMPORTANT_MARKER_RE.match(prose) is not None,
        "the notice still leads with IMPORTANT (cosmetic; not wake-gating since A4)",
    )

    # (c) it is RUNNABLE — against the live decorator, not a copied literal.
    declared = AgentMessagingPlugin.peer_inbox_action._platform_process_metadata  # noqa: SLF001
    key = f"plugin::agent_messaging_plugin::{declared.name}"
    _check(key in prose, f"the notice names the verb's own registered key ({key})")
    required = sorted(
        name for name, meta in declared.parameters.items() if meta.required
    )
    missing = [name for name in required if name not in prose]
    _check(
        not missing,
        "the notice names every REQUIRED parameter of the verb it advertises "
        f"(required={required}, missing={missing})",
    )


# ---------------------------------------------------------------------------
# The advertised strings — a shipped instruction must be runnable
# ---------------------------------------------------------------------------


def test_wake_footer_advertises_a_runnable_command() -> None:
    """The footer is an instruction a model executes, so run it, don't read it.

    Before Part 24 this literal advertised the process key with NO arguments;
    that command 500'd (unregistered) and would now fail ``missing_argument``.
    Substring-matching the key would have passed in both worlds — so the check
    that matters is that a shell expands the printed argument into the JSON the
    process actually accepts.
    """
    tmp = Path(tempfile.mkdtemp())
    packet = _compose_wake_packet(
        WakeTarget(
            solet_name="testhome",
            role="Claude-B",
            spool=tmp / "spool",
            offset_file=tmp / "offset",
            lock_file=tmp / "lock",
        ),
        ["{}"],
    )
    _check(
        "plugin::agent_messaging_plugin::peer_inbox" in packet,
        "the footer names the full process key",
    )
    match = re.search(r"peer_inbox ('.*?}')`", packet)
    _check(match is not None, "the footer carries a quoted JSON argument")
    assert match is not None
    expanded = subprocess.run(  # noqa: S603
        ["/bin/sh", "-c", f"printf %s {match.group(1)}"],  # noqa: S607
        capture_output=True,
        text=True,
        check=True,
        env={"AGENT_SESSION_ID": _SESSION_ID},
    ).stdout
    parsed = json.loads(expanded)
    _check(
        parsed.get("agent_session_id") == _SESSION_ID,
        "a shell expands the advertised argument to the caller's real session "
        f"id (got {parsed.get('agent_session_id')!r})",
    )
    _check(
        "include_important" not in parsed,
        "A4: the footer no longer advertises the retired include_important "
        "argument — the catch-up view is the only one peer_inbox serves now",
    )
    # And the process accepts exactly that payload.
    registry = _registry()
    registry.register(_binding())
    result = _call(
        _plugin(_RecordingService(_page()), registry=registry), **parsed,
    )
    _check(
        result["action_status"] == "completed",
        "the advertised payload is accepted by the process it advertises",
    )


def test_role_handover_prose_is_copy_runnable() -> None:
    prose = new_holder_prose(_ROLE)
    _check(
        "plugin::agent_messaging_plugin::peer_inbox" in prose,
        "the handover notice names the full process key, not a bare verb",
    )
    _check(_ROLE in prose, "the handover notice stays role-agnostic")
    _check(
        "next_role_cursor" in prose,
        "the handover notice states the real exhaustion condition",
    )


# ---------------------------------------------------------------------------


def main() -> int:
    for test in (
        test_identity_comes_from_the_binding_not_the_caller,
        test_unregistered_session_fails_loud,
        test_duplicate_binding_for_one_session_fails_loud,
        test_missing_session_id_fails_loud,
        test_inactive_plugin_errors_rather_than_reporting_an_empty_inbox,
        test_cursors_are_independent_and_never_mixed,
        test_absent_cursors_are_none_not_empty_strings,
        test_malformed_after_fails_loud,
        test_a_service_level_rejection_surfaces_as_an_error,
        test_a_failed_role_section_still_serves_instance_mail,
        test_limit_defaults_and_clamps,
        test_include_important_is_always_true_now,
        test_serialized_page_matches_the_declared_schema,
        test_role_section_status_serializes_to_its_lowercase_value,
        test_role_page_truncation_metadata_serializes,
        test_entries_carry_sender_identity_and_isoformat_timestamps,
        test_read_touches_the_callers_binding,
        test_delivery_route_attached_reports_the_holders_route,
        test_holds_survives_every_fault_the_route_lookup_can_raise,
        test_delivery_route_attached_is_false_for_a_vacant_role,
        test_wake_footer_advertises_a_runnable_command,
        test_role_handover_prose_is_copy_runnable,
        test_handover_notice_is_the_pinned_backlog_dependency,
    ):
        print(f"\n{test.__name__}")
        test()
    print(f"\n{_passed} passed, {len(_failed)} failed")
    for label in _failed:
        print(f"  FAILED: {label}")
    return 1 if _failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
