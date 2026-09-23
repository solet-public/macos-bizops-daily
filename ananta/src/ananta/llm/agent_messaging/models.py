"""Typed dataclasses for the agent-messaging service surface.

These models are the public contract of ``AgentMessagingService``:
``agent_messaging_plugin``'s bridge surface constructs requests, calls
the service, and serializes the responses to its HTTP clients.  They
are also used by the repository for SQL row marshalling.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum

ID_PREFIX_THREAD = "agt-"
ID_PREFIX_MESSAGE = "agm-"


class ThreadStatus(StrEnum):
    """Lifecycle states for an agent thread."""

    OPEN = "open"
    QUEUED = "queued"
    RUNNING = "running"
    IDLE = "idle"
    INTERRUPTED = "interrupted"
    ERROR = "error"
    CLOSED = "closed"


class MessageRole(StrEnum):
    """Who produced an agent message."""

    ORIGINATOR = "originator"
    AGENT = "agent"
    SYSTEM = "system"


class MessageKind(StrEnum):
    """What kind of payload an agent message carries."""

    MESSAGE = "message"
    STATUS = "status"
    RESULT = "result"
    ERROR = "error"
    ARTIFACT = "artifact"


class OriginatorType(StrEnum):
    """Where the originator of a thread lives."""

    MCP_BRIDGE = "mcp_bridge"


class RoleSectionStatus(StrEnum):
    """v10 Q1 fault-domain status of the ``peer_inbox`` role section.

    The role section is computed independently of the instance section so a
    role-side fault (malformed ``role_after``, k-way-merge edge, transient role
    query error) cannot deny a caller its instance messages. ``ERROR`` signals
    that the role section failed and was returned empty (the error detail rides
    ``PeerInbox.role_section_error``); the instance section is still valid.
    """

    OK = "ok"
    ERROR = "error"


class RoleTruncationReason(StrEnum):
    """Why a successful role-inbox page has a continuation cursor."""

    ROW_LIMIT = "row_limit"
    BYTE_CEILING = "byte_ceiling"


@dataclass(frozen=True, slots=True)
class TextPart:
    """A single text fragment of message content.

    Multi-modal parts (images, files) are not in the first slice.
    """

    type: str
    text: str


# Public alias so callers don't have to know the part-type taxonomy.
MessageContent = list[TextPart]


@dataclass(frozen=True, slots=True)
class ArtifactRef:
    """Blob-only artifact reference returned alongside an agent response."""

    blob_id: str
    filename: str | None = None
    mime_type: str | None = None
    size_bytes: int | None = None
    metadata: dict[str, object] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class AgentMessageRow:
    """One row of ``core__agent_message`` rendered as a public payload."""

    id: str
    thread_id: str
    cursor: int
    role: MessageRole
    kind: MessageKind
    content: MessageContent
    created_at: datetime
    action_id: str | None = None
    backend_session_id: str | None = None
    error: dict[str, object] | None = None
    artifacts: tuple[ArtifactRef, ...] = ()
    metadata: dict[str, object] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ListAgentThreadsRequest:
    """Inputs to ``AgentMessagingService.list_threads``.

    A global, UNSCOPED enumeration of threads — unlike ``list_messages``
    (bound to one owned thread via ``bridge_id``), this is an internal
    substrate read (D1/GAP-5): consumers project the whole ``agent_thread``
    set into downstream stores (e.g. the session ledger) through the owning
    interface instead of reading ``core__agent_thread`` directly. It is NOT
    on the bridge HTTP surface and carries no ``bridge_id`` — it is consumed
    only by structural typing (``plugin_manager.plugins['agent_messaging_plugin']``),
    so the lack of per-thread ownership scoping is intentional.

    Cursor-paginated by the tie-safe composite ``(created_at, id)`` carried as
    an OPAQUE token (``after_cursor``): the caller echoes back the previous
    page's ``next_cursor`` verbatim and never constructs it; ``None`` starts at
    the beginning. The token is decoded fail-closed — a malformed cursor is
    rejected, not silently restarted at the beginning. A created_at-only cursor
    is deliberately NOT used — it would silently drop rows sharing a
    ``created_at`` across a page boundary.
    """

    after_cursor: str | None = None
    limit: int = 50
    include_deleted: bool = False


@dataclass(frozen=True, slots=True)
class AgentThreadsPage:
    """Response from ``list_threads``.

    ``next_cursor`` is the opaque composite ``(created_at, id)`` token of the
    last returned row (or the request's ``after_cursor`` when the page is
    empty), fed back verbatim as the next request's ``after_cursor``. A short
    page (fewer than ``limit`` rows) signals the enumeration is drained.
    """

    threads: tuple[AgentThreadRow, ...]
    next_cursor: str | None


@dataclass(frozen=True, slots=True)
class ReadThreadMessagesRequest:
    """Inputs to ``AgentMessagingService.read_thread_messages``.

    The UNSCOPED message-read counterpart to ``list_messages`` (D1/GAP-5): an
    internal substrate read for the session-ledger projection that reads ONE
    thread's messages WITHOUT the bridge-ownership check — it carries no
    ``bridge_id`` because the ledger source does not own the threads it
    projects. Consumed only by structural typing (not on the bridge HTTP
    surface, not a discoverable process — mirrors ``list_threads``). Paginated
    by the message's monotonic int ``cursor`` (strictly greater than
    ``after_cursor``).
    """

    thread_id: str
    after_cursor: int = 0
    limit: int = 50


@dataclass(frozen=True, slots=True)
class AgentThreadMessagesPage:
    """Response from ``read_thread_messages``.

    A minimal page (``messages`` + the int ``next_cursor``) — deliberately
    WITHOUT ``AgentMessagesPage``'s thread ``status``, which is sourced from the
    owned-thread fetch this unscoped read does not perform.
    """

    thread_id: str
    messages: tuple[AgentMessageRow, ...]
    next_cursor: int


# ---------------------------------------------------------------------------
# Peer messaging (live MCP-session ↔ MCP-session)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PeerSendRequest:
    """Inputs to ``AgentMessagingService.peer_send``.

    Peer messaging is the cross-bridge addressing extension: live
    MCP sessions address one another by ``(agent_id, agent_instance_id)``
    pairs.  ``agent_id`` is the stable kind ("claude_code", "codex",
    ...); ``agent_instance_id`` is the durable per-bridge UUID
    generated by the bridge subprocess at startup.

    The plugin's bridge surface resolves the recipient from its
    in-memory peer registry BEFORE constructing this request.  The
    service is therefore never given an ambiguous identity on either
    side — both sender and recipient identities are concrete.
    """

    sender_bridge_id: str
    sender_agent_id: str
    sender_agent_instance_id: str
    sender_session_label: str
    peer_agent_id: str
    peer_agent_instance_id: str
    content: MessageContent
    # Resolved at the bridge boundary from ``peer_registry.resolve(...)``
    # so the service can snapshot the recipient's session_label onto
    # ``core__agent_thread`` at thread-creation time. Default "" so
    # legacy test fixtures and any non-peer-registry caller paths stay
    # valid. Per 2026-05-31 Architect ruling §2: when this is empty,
    # the title formatter falls back to ``peer_agent_id``.
    peer_session_label: str = ""
    # Resolved at the bridge boundary from ``peer_registry.resolve(...)`` — the
    # recipient's STABLE per-logical-session key, stamped onto the peer thread at
    # creation so inbox VISIBILITY survives the recipient's instance rotation
    # (REL-08 read-side / Fork-1a). Default "" for legacy / non-registry callers
    # (their threads stay instance-keyed, exactly as visible as today).
    peer_agent_session_id: str = ""
    # The SENDER's stable per-logical-session key — the symmetric half of
    # ``peer_agent_session_id`` above (WS-2c V4 / A2). Resolved SERVER-SIDE from
    # the sender's registered binding, never accepted from the caller, and carried
    # into the delivered reply hint ALONGSIDE (never replacing) the instance id, so
    # a reply still resolves after the sender's instance has rotated. Default ""
    # for callers with no registered binding — their hints stay instance-only,
    # exactly as churnable as today.
    sender_agent_session_id: str = ""
    # True when the sender used the IMPORTANT marker (the bridge has
    # already detected and stripped it from ``content`` before calling
    # the service).  Persisted as ``metadata.important`` so peer_inbox
    # can return silent-only by filtering.
    important: bool = False


@dataclass(frozen=True, slots=True)
class PeerSendResult:
    """Response from ``AgentMessagingService.peer_send``."""

    thread_id: str
    message_id: str
    cursor: int
    delivered_to_bridge_id: str


@dataclass(frozen=True, slots=True)
class PeerInboxRequest:
    """Inputs to ``AgentMessagingService.peer_inbox``.

    ``include_important`` controls whether messages whose sender used
    the IMPORTANT marker are returned alongside silent-bucket messages.
    Default True makes ``peer_inbox`` the durable catch-up view; callers
    that intentionally want silent-only status checks must pass False.
    """

    recipient_agent_id: str
    recipient_agent_instance_id: str
    # REL-08 read-side: the caller's STABLE session key (from its own live
    # binding). The inbox thread resolution UNIONs (session_id == this) OR
    # (instance_id == current) so pre-reconnect threads stay visible across the
    # caller's instance rotation, WITHOUT re-homing the durable thread rows.
    # Default "" → session disjunct skipped (legacy instance-only visibility).
    recipient_agent_session_id: str = ""
    after_created_at: datetime | None = None
    limit: int = 50
    include_important: bool = True
    # v10 Control #1a: the opaque, scope-bound cursor for the role-inbox
    # section (a global (created_at, id) k-way merge across held roles).
    # Independent of ``after_created_at`` (the instance section's raw
    # newest-first timestamp cursor) — the two are never mixed. Default None = first
    # role page; existing instance-only callers are unaffected.
    role_after: str | None = None
    # Observers (watch/status/hidden-output callers) are read-only: they must
    # neither issue a page token nor create a display receipt.
    observer: bool = False


@dataclass(frozen=True, slots=True)
class RoleMessagePersisted:
    """The durable handles a role-message write hands back to its dispatcher.

    ``created_at`` is the PERSISTED ROW's timestamp, read back after the
    upsert rather than synthesised at call time, because it is the exact
    quantity the role-inbox section sorts and pages on
    (``order_by (created_at, id) desc``) and therefore the only value a
    watcher may compare its ``role_high_water`` mark against.

    Reading it back is deliberate. A clock reading taken beside the write
    (``datetime.now(UTC)``) is a DIFFERENT quantity: if it lands even
    fractionally ahead of the row, a client marking with it skips a message
    whose ``created_at`` is later — silent loss, in the one direction the
    watcher-mark contract must never move. Same-quantity-by-construction is
    worth one extra read on the IMPORTANT role path.
    """

    message_id: str
    created_at: str
    role_row_id: str = ""


@dataclass(frozen=True, slots=True)
class RoleCoveredMark:
    """The durable 'covered through' mark for one role (pull-surface boundary
    design §2). Returned by ``peer_mark_role_covered`` — the stored value on
    a genuine advance, the PRE-EXISTING value on a monotonic no-op, so a
    caller can always confirm what "covered" now means for the role.
    """

    recipient_key: str
    covered_created_at: str
    covered_id: str
    covered_message_id: str
    attested_at: str


@dataclass(frozen=True, slots=True)
class PeerInboxEntry:
    """One peer-inbox row: a sender's message addressed to the recipient.

    ``thread_id`` is the sender's outgoing thread (recipient does not
    own it but is allowed to read peer-targeted threads via the
    inbox surface).  ``message`` is the persisted originator row.

    ``sender_agent_instance_id`` and ``sender_session_label`` are
    surfaced from the persisted metadata so a receiver pulling a
    missed IMPORTANT message via ``peer_inbox(include_important=True)``
    can construct a targeted reply (peer_send needs
    ``peer_agent_instance_id``).  Empty strings if the persisted row
    predates multi-instance support.
    """

    thread_id: str
    sender_agent_id: str
    sender_agent_instance_id: str
    sender_session_label: str
    message: AgentMessageRow


@dataclass(frozen=True, slots=True)
class PeerInbox:
    """Response from ``AgentMessagingService.peer_inbox``.

    Both sections are newest-first. The instance section keeps its
    timestamp-only backward cursor contract. ``instance_exhausted`` says
    whether that cursor has reached the end of the currently readable instance
    section. It does **not** prove every durable row was observed: the legacy
    timestamp-only cursor deliberately retains its pre-existing
    duplicate-``created_at`` skip at a page boundary.

    v10 adds the role section ADDITIVELY:
    ``role_entries`` is the global ``(created_at, id)`` k-way merge across
    the holder's roles, paged by the opaque ``next_role_cursor`` (fed back
    as ``PeerInboxRequest.role_after``). Both new fields default empty, so
    the two existing construction sites and any caller that ignores the
    role section keep working unchanged.

    ``role_section_status`` / ``role_section_error`` are the v10 Q1 fault-domain
    boundary: when the role section fails it comes back ``ERROR`` + empty (with
    the error repr) while the instance section is still served, so a role-side
    fault never denies a caller its instance messages. Both default to the
    healthy state, preserving the existing construction sites.

    Pull-surface boundary (design §5) adds ``role_floor_applied`` and
    ``role_history_cursor`` ADDITIVELY, same non-breaking shape: ``False`` /
    ``None`` when no ``role_covered_mark`` exists for any held role (today's
    unbounded behavior, byte-for-byte, until a session calls
    ``peer_mark_role_covered`` at least once). ``role_floor_applied=True``
    means the default drain's floor removed at least one already-covered row
    from this page; ``role_history_cursor`` is populated ONLY on a genuine
    floor-stop (``next_role_cursor is None and role_floor_applied``) — echo it
    back as ``role_after`` for a deliberate pre-mark read.

    ``role_limit`` records the effective page limit. A successful role page
    with a continuation cursor is explicitly bounded: ``role_page_truncated``
    is true and ``role_truncation_reason`` identifies either the row limit or
    page byte ceiling. ``role_byte_ceiling`` is populated only for the latter.
    """

    recipient_agent_id: str
    entries: tuple[PeerInboxEntry, ...]
    next_after_created_at: datetime | None
    instance_exhausted: bool
    role_entries: tuple[PeerInboxEntry, ...] = ()
    next_role_cursor: str | None = None
    role_section_status: RoleSectionStatus = RoleSectionStatus.OK
    role_section_error: str | None = None
    role_floor_applied: bool = False
    role_history_cursor: str | None = None
    role_limit: int | None = None
    role_page_truncated: bool = False
    role_truncation_reason: RoleTruncationReason | None = None
    role_byte_ceiling: int | None = None
    role_read_page_token: str | None = None
    role_read_page_status: str = "disabled"


@dataclass(frozen=True, slots=True)
class AgentThreadRow:
    """Repository projection of a ``core__agent_thread`` row.

    Originally an internal projection (the service composes message
    responses from this row plus message data); it is ALSO the row type
    surfaced directly by ``list_threads`` (the GAP-5 substrate enumeration),
    which returns these projections to internal consumers verbatim.
    """

    id: str
    originator_type: OriginatorType
    target_backend: str
    target_plugin_name: str
    status: ThreadStatus
    created_at: datetime
    updated_at: datetime
    last_message_cursor: int
    originator_id: str | None = None
    originator_session_id: str | None = None
    originator_bridge_id: str | None = None
    title: str | None = None
    working_directory: str | None = None
    backend_session_id: str | None = None
    active_action_id: str | None = None
    active_flow_id: str | None = None
    closed_at: datetime | None = None
    metadata: dict[str, object] = field(default_factory=dict)
    # Peer-thread recipient disambiguation key.  NULL for non-peer
    # threads (target_backend NOT LIKE 'peer:%') and for legacy peer
    # threads predating multi-instance support.
    recipient_agent_instance_id: str | None = None
    # Per-peer label snapshots taken at thread-creation time per the
    # 2026-05-31 ledger-per-peer-distinction ruling §2. NULL for
    # non-peer threads. Never mutated after create — operator /rename
    # on a session with open peer threads does NOT touch these.
    originator_session_label: str | None = None
    originator_agent_instance_id: str | None = None
    recipient_session_label: str | None = None


__all__ = [
    "ID_PREFIX_MESSAGE",
    "ID_PREFIX_THREAD",
    "AgentMessageRow",
    "AgentThreadMessagesPage",
    "AgentThreadRow",
    "AgentThreadsPage",
    "ArtifactRef",
    "ListAgentThreadsRequest",
    "MessageContent",
    "MessageKind",
    "MessageRole",
    "ReadThreadMessagesRequest",
    "OriginatorType",
    "PeerSendRequest",
    "PeerSendResult",
    "TextPart",
    "ThreadStatus",
]
