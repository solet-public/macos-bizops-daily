"""``AgentMessagingService`` — orchestrates threads, messages, and peer mail.

The service composes:

- :class:`AgentMessagingRepository` for SQL-backed thread/message
  persistence.
- :class:`AgentMessagingConfig` for policy (allowed backends, message
  size caps, timeouts).

``list_threads``/``read_thread_messages`` are the unscoped GAP-5/D1
substrate reads the session-ledger projection consumes; ``peer_send``/
``peer_inbox`` and the role-addressed delivery methods are the live
peer-messaging surface. This module is **not** an HTTP layer.
Validation errors raise typed exceptions; the calling plugin maps them
to HTTP responses.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime

from ananta.interfaces.state_management_interface import StateManagementInterface
from ananta.services.state_service.ordered_query import normalize_sort_value

from .models import (
    AgentThreadMessagesPage,
    AgentThreadRow,
    AgentThreadsPage,
    ListAgentThreadsRequest,
    MessageContent,
    MessageKind,
    MessageRole,
    OriginatorType,
    PeerInbox,
    PeerInboxEntry,
    PeerInboxRequest,
    PeerSendRequest,
    PeerSendResult,
    ReadThreadMessagesRequest,
    RoleCoveredMark,
    RoleMessagePersisted,
    RoleSectionStatus,
    RoleTruncationReason,
    ThreadStatus,
)
from .repository import (
    AgentMessagingRepository,
    NewMessage,
    ThreadStatusUpdate,
)
from .role_binding import (
    AGENT_ROLE_BINDING_NAMESPACE,
    COL_AGENT_INSTANCE_ID,
    COL_ROLE,
    TABLE_ROLE_BINDING,
)
from .role_cursor import (
    RoleCursorOutcome,
    RoleCursorRejectedError,
    RoleCursorScope,
    decode_role_cursor,
)
from .role_inbox import (
    ROLE_SECTION_BYTE_CEILING,
    build_role_section,
    merge_undelivered_oldest_first,
)
from .role_read_models import RoleReadAck, RoleReadPage, RoleReadReceiptResult
from .role_read_pages import (
    held_role_scope_hash,
    page_external_id,
    page_item_digest,
    page_item_external_id,
    page_token,
    token_digest,
)
from .role_read_receipts import receipt_external_id, watermark_external_id
from .schema import (
    COL_ACTIVITY_AT_EMISSION,
    COL_CONSUMED,
    COL_CONSUMED_AT,
    COL_EMIT_COUNT,
    COL_EMITTED_TO_AGENT_INSTANCE_ID,
    COL_EMITTED_TO_AGENT_SESSION_ID,
    COL_ESCALATED,
    COL_LAST_EMITTED_AT,
    RECIPIENT_KIND_ROLE,
    ROLE_THREAD_PREFIX,
    TABLE_AGENT_ROLE_MESSAGE,
    TABLE_ROLE_COVERED_MARK,
    TABLE_ROLE_READ_PAGE,
    TABLE_ROLE_READ_PAGE_ITEM,
    TABLE_ROLE_READ_RECEIPT,
    TABLE_ROLE_READ_WATERMARK,
    role_covered_mark_external_id,
)
from .schema import NAMESPACE as _ROLE_NAMESPACE
from .state_results import require_completed, require_records, require_updated
from .thread_cursor import decode_thread_cursor, encode_thread_cursor

logger = logging.getLogger(__name__)

# Receipt filtering may need to scan past a fully acknowledged prefix.  Keep
# that repair bounded; an exhausted budget is an explicit incomplete read, not
# a false "no older rows" answer that strands unserved mail.
_ROLE_RECEIPT_REFILL_MAX_PAGES = 100


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class AgentMessagingError(Exception):
    """Base error for the service surface.

    ``code`` is a stable string suitable for inclusion in HTTP error
    payloads (e.g. ``"agent_thread_busy"``).  ``http_status`` is a
    suggestion to the calling HTTP layer; the layer is free to remap.
    """

    code: str = "agent_messaging_error"
    http_status: int = 400


class AgentMessagingDisabledError(AgentMessagingError):
    code = "agent_messaging_disabled"
    http_status = 503


class AgentRequestInvalidError(AgentMessagingError):
    code = "agent_request_invalid"
    http_status = 400


# ---------------------------------------------------------------------------
# Config + dependencies
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class AgentMessagingConfig:
    """Policy snapshot bound at service construction time."""

    enabled: bool = True
    allowed_backends: tuple[str, ...] = ("codex", "claude_code")
    allowed_working_directory_roots: tuple[str, ...] = ()
    max_message_bytes: int = 65_536
    max_thread_messages: int = 1_000
    default_timeout_seconds: int = 600
    max_timeout_seconds: int = 1_800


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------


def role_message_external_id(
    recipient_kind: str, recipient_key: str, message_id: str,
) -> str:
    """Deterministic idempotency key for a role-addressed envelope.

    ``message_id`` is stable per logical send (not per transport attempt),
    so transport retries of the same send collapse to one row while
    distinct sends get distinct keys.
    """
    return f"{recipient_kind}:{recipient_key}:{message_id}"


def _serialize_role_content(content: MessageContent) -> list[dict[str, object]]:
    """Render the typed message parts to JSON-storable dicts for the envelope."""
    return [{"type": part.type, "text": part.text} for part in content]


def _result_records(result: object) -> list[dict[str, object]]:
    """Records from a COMPLETED query ActionResult — fails loud on a provider error.

    Delegates to :func:`require_records` (Codex BLOCKER-1): a non-completed
    ``action_status`` RAISES ``StateOperationError`` rather than masquerading as
    an empty result (which let a DB fault read as an empty-but-healthy section).
    """
    return require_records(result)


def _role_row_key(record: dict[str, object]) -> tuple[str, str]:
    """The tie-safe ``(created_at, id)`` sort key for one role-message row.

    Same shape and comparator (``normalize_sort_value``) as
    ``role_inbox._merge_sort_key`` — deliberately duplicated rather than
    imported, since importing it would put a private ``role_inbox`` symbol on
    a cross-module boundary for a two-line function.
    """
    return (
        normalize_sort_value(record.get("created_at")),
        normalize_sort_value(record.get("id")),
    )


def _apply_role_floor(
    records: list[dict[str, object]], mark: tuple[str, str] | None,
) -> tuple[list[dict[str, object]], bool]:
    """Drop rows at/below ``mark`` (design §5b.i — a post-fetch filter, not a
    query-level ``after`` bound; see the design doc for why those are NOT the
    same mechanism). Returns ``(kept, truncated)`` — ``truncated`` is True
    iff at least one row was dropped, the signal ``list_silent_for_roles``
    threads into ``build_role_section``'s floor-stop mint predicate.

    ``mark is None`` (no attestation yet for this role) is a no-op — §12.3's
    fail-direction: absent mark, unchanged query, over-page never under-page.
    """
    if mark is None:
        return records, False
    kept = [record for record in records if _role_row_key(record) > mark]
    return kept, len(kept) < len(records)


def _max_role_key(marks: Iterable[tuple[str, str]]) -> tuple[str, str] | None:
    """The MAX (newest) ``(created_at, id)`` pair across a set of marks.

    Design §5b.iii: the shared per-call ``role_after`` cursor is a single
    value applied to every held role's query, so a role-specific floor can't
    be resumed with one shared seed. MAX over-includes (a role whose own
    mark is older resurfaces some already-covered rows on the resume walk);
    MIN would under-include (a role whose own mark is newer would have
    not-yet-resumed pre-mark history silently skipped). Over-page is
    §12.3's fail direction, so MAX is the only safe choice.
    """
    values = list(marks)
    if not values:
        return None
    return max(
        values,
        key=lambda pair: (normalize_sort_value(pair[0]), normalize_sort_value(pair[1])),
    )


def _project_role_covered_mark(record: dict[str, object]) -> RoleCoveredMark:
    """Project a raw ``role_covered_mark`` row to the public dataclass."""
    return RoleCoveredMark(
        recipient_key=str(record.get("recipient_key", "")),
        covered_created_at=str(record.get("covered_created_at", "")),
        covered_id=str(record.get("covered_id", "")),
        covered_message_id=str(record.get("covered_message_id", "")),
        attested_at=str(record.get("attested_at", "")),
    )


# REL-05 re-emit window + cap defaults (plugin-config-surfaced; Q1). The window
# is the minimum gap between emissions of the same owed message (most sessions
# turn within it, so the first re-emit is genuine deafness, not impatience); the
# cap bounds total emissions (original + re-emits) before escalation.
DEFAULT_RE_EMIT_WINDOW_S = 300.0
DEFAULT_RE_EMIT_CAP = 3


def _parse_iso(value: object) -> datetime | None:
    """Parse a stored ISO-8601 timestamp cell to an aware (UTC) datetime, or ``None``.

    State-store ``DATETIME`` columns are ``timestamp without time zone``, so every
    stored cell reads back offset-NAIVE (UTC wall-clock) while this module's live
    datetimes (``_clock`` = ``datetime.now(UTC)``, the bridge activity stamp) are
    offset-AWARE UTC. Coerce a naive parse to UTC at this single boundary so every
    comparison in the module is aware-vs-aware (all values are UTC) and the
    owed-delivery sweep/drain never hits a naive-vs-aware ``TypeError``.
    """
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def _as_int(value: object) -> int:
    """A state-row integer cell coerced to ``int`` (0 for a missing/other cell)."""
    return value if isinstance(value, int) else 0


def _within_reemit_window(
    last_emitted_at: object, now: datetime, re_emit_window_s: float,
) -> bool:
    """True iff an emission happened too recently to re-emit yet.

    A row never emitted (``last_emitted_at`` NULL) is NOT within the window — it
    is eligible immediately (the role original's first drain). Otherwise the row
    must wait ``re_emit_window_s`` from its last emission before re-emit.
    """
    emitted = _parse_iso(last_emitted_at)
    if emitted is None:
        return False
    return (now - emitted).total_seconds() < re_emit_window_s


def _owed_after_window_and_cap(
    rows: list[dict[str, object]],
    *,
    now: datetime,
    re_emit_window_s: float,
    cap: int,
) -> list[dict[str, object]]:
    """Keep only rows eligible for (re-)emit — the window/cap inequalities the
    equality-only state filter cannot express.

    A row is dropped when it has already been emitted ``cap`` times, or when its
    last emission is still inside the re-emit window. Order-preserving.
    """
    eligible: list[dict[str, object]] = []
    for row in rows:
        if _as_int(row.get(COL_EMIT_COUNT)) >= cap:
            continue
        if _within_reemit_window(row.get(COL_LAST_EMITTED_AT), now, re_emit_window_s):
            continue
        eligible.append(row)
    return eligible


class AgentMessagingService:
    """Public façade for the agent-messaging layer.

    Construct once at plugin readiness; reused for the lifetime of the
    plugin.  All methods are synchronous; the underlying turn execution
    runs in the action queue (so HTTP callers see a queued response,
    not a blocked socket).
    """

    def __init__(
        self,
        *,
        repository: AgentMessagingRepository,
        state_service: StateManagementInterface,
        config: AgentMessagingConfig,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._repo = repository
        # The v10 role-addressed delivery store (core__agent_role_message)
        # is written + read ONLY through the high-level state interface
        # (upsert_state / query_ordered / update_state) — never raw SQL.
        self._state = state_service
        self._config = config
        self._clock = clock or (lambda: datetime.now(UTC))

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def list_threads(
        self, request: ListAgentThreadsRequest,
    ) -> AgentThreadsPage:
        """Enumerate threads globally, cursor-paginated by ``(created_at, id)``.

        Unscoped by design (no ``bridge_id`` / ``_require_owned_thread``): this
        is the GAP-5/D1 substrate read consumed by structural typing for
        downstream projection (e.g. the session ledger), not a per-bridge
        owned-thread read like ``list_messages``. Only the enablement gate
        applies. The composite cursor is carried verbatim across pages; an
        empty page returns the request's own cursor (the caller stops when a
        page is short or empty).
        """
        self._require_enabled()
        after = (
            decode_thread_cursor(request.after_cursor)
            if request.after_cursor is not None
            else None
        )
        rows = self._repo.list_threads(
            after=after,
            limit=request.limit,
            include_deleted=request.include_deleted,
        )
        if rows:
            last = rows[-1]
            next_cursor: str | None = encode_thread_cursor(
                created_at_iso=normalize_sort_value(last.created_at),
                row_id=last.id,
            )
        else:
            next_cursor = request.after_cursor
        return AgentThreadsPage(
            threads=tuple(rows),
            next_cursor=next_cursor,
        )

    def read_thread_messages(
        self, request: ReadThreadMessagesRequest,
    ) -> AgentThreadMessagesPage:
        """Unscoped per-thread message read (no bridge-ownership gate).

        Identical to ``list_messages``'s repository read — same
        ``self._repo.list_messages`` (the repo layer is pure persistence, NOT
        ownership-scoped) and the same int-cursor pagination — but WITHOUT
        ``_require_owned_thread``: the session-ledger projection (the only
        consumer) reads threads it does not own. Returns a minimal page (no
        thread ``status``: the owned-thread fetch is intentionally skipped).
        Only the enablement gate applies.
        """
        self._require_enabled()
        rows = self._repo.list_messages(
            request.thread_id,
            after_cursor=max(0, request.after_cursor),
            limit=request.limit,
        )
        next_cursor = rows[-1].cursor if rows else request.after_cursor
        return AgentThreadMessagesPage(
            thread_id=request.thread_id,
            messages=tuple(rows),
            next_cursor=next_cursor,
        )

    def peer_send(self, request: PeerSendRequest) -> PeerSendResult:
        """Persist a peer-message and return what the caller should deliver.

        Cross-bridge addressing (live MCP session ↔ MCP session)
        layers on top of the existing thread/message tables: one
        durable thread per ``(sender_bridge, peer_agent_id)`` pair,
        identified by ``target_backend = 'peer:<peer_agent_id>'``.
        Each ``peer_send`` allocates a cursor and appends a message
        the same way ``send_message`` does.

        The actual event enqueue on the *target* bridge happens in
        ``agent_messaging_plugin``'s bridge surface: this method just
        records the outgoing side and returns the structured payload
        the plugin should fan out.  Service-level routing keeps things
        decoupled from the bridge's in-memory peer registry.
        """
        self._require_enabled()
        if not request.sender_bridge_id:
            raise AgentRequestInvalidError("sender_bridge_id is required")
        if not request.sender_agent_id:
            raise AgentRequestInvalidError("sender_agent_id is required")
        if not request.sender_agent_instance_id:
            raise AgentRequestInvalidError("sender_agent_instance_id is required")
        if not request.peer_agent_id:
            raise AgentRequestInvalidError("peer_agent_id is required")
        if not request.peer_agent_instance_id:
            raise AgentRequestInvalidError("peer_agent_instance_id is required")
        # Same-instance self-send is invalid regardless of agent_id.
        # The instance_id is the durable UUID; if both sides resolve to
        # the same instance it is a self-send no matter what agent_id
        # either side currently advertises (e.g. after a manual
        # peer_register that mutated agent_id).  Same-kind cross-instance
        # sends (codex_A -> codex_B) are valid.
        if (
            request.sender_agent_instance_id
            == request.peer_agent_instance_id
        ):
            raise AgentRequestInvalidError(
                "cannot peer_send to your own instance "
                f"({request.sender_agent_instance_id})",
            )
        self._validate_content(request.content)

        thread = self._repo.find_peer_thread(
            originator_bridge_id=request.sender_bridge_id,
            peer_agent_id=request.peer_agent_id,
            peer_agent_instance_id=request.peer_agent_instance_id,
        )
        if thread is None:
            thread = self._create_peer_thread(request)
        appended = self._repo.append_message(
            thread_id=thread.id,
            message=NewMessage(
                role=MessageRole.ORIGINATOR,
                kind=MessageKind.MESSAGE,
                content=request.content,
                metadata={
                    "peer": True,
                    "sender_agent_id": request.sender_agent_id,
                    "sender_agent_instance_id": (
                        request.sender_agent_instance_id
                    ),
                    "sender_session_label": request.sender_session_label,
                    "peer_agent_id": request.peer_agent_id,
                    "peer_agent_instance_id": request.peer_agent_instance_id,
                    # ``important`` discriminates wake-bound messages
                    # (sender used the IMPORTANT marker) from silent FYIs.
                    # peer_inbox uses it for explicit silent-only filters while
                    # leaving the public default as durable catch-up.
                    "important": request.important,
                },
            ),
            require_status_in=(
                ThreadStatus.OPEN,
                ThreadStatus.IDLE,
                ThreadStatus.INTERRUPTED,
            ),
            update=ThreadStatusUpdate(status=ThreadStatus.IDLE),
        )
        return PeerSendResult(
            thread_id=thread.id,
            message_id=appended.id,
            cursor=appended.cursor,
            # The plugin overrides this after looking up the target's
            # current bridge_id from the registry; we don't track it
            # here (service stays bridge-agnostic).
            delivered_to_bridge_id="",
        )

    def peer_inbox(self, request: PeerInboxRequest) -> PeerInbox:
        """Return originator peer messages addressed to ``recipient_agent_id``.

        Lets a receiver pull messages that were persisted silently
        (no IMPORTANT marker, so no notification fired).  Spans
        every peer thread targeting this agent regardless of which
        bridge owns the thread — peer threads get a small carve-out
        from strict bridge-bound ownership because the recipient is,
        by definition, not the originator.

        Pagination uses ``after_created_at`` as the backward cursor
        (per-thread cursor wouldn't span multiple threads cleanly).
        Caller passes the previous page's ``next_after_created_at``
        back on the next call. Both direct and role sections return
        newest-first pages and walk toward older rows.
        """
        self._require_enabled()
        if not request.recipient_agent_id:
            raise AgentRequestInvalidError("recipient_agent_id is required")
        if not request.recipient_agent_instance_id:
            raise AgentRequestInvalidError(
                "recipient_agent_instance_id is required",
            )
        rows, instance_exhausted = self._repo.list_peer_messages_for(
            recipient_agent_id=request.recipient_agent_id,
            recipient_agent_instance_id=(
                request.recipient_agent_instance_id
            ),
            recipient_agent_session_id=request.recipient_agent_session_id,
            after_created_at=request.after_created_at,
            limit=request.limit,
            silent_only=not request.include_important,
        )
        entries = tuple(
            PeerInboxEntry(
                thread_id=message.thread_id,
                sender_agent_id=str(
                    message.metadata.get("sender_agent_id", ""),
                ),
                sender_agent_instance_id=str(
                    message.metadata.get("sender_agent_instance_id", ""),
                ),
                sender_session_label=str(
                    message.metadata.get("sender_session_label", ""),
                ),
                message=message,
            )
            for message in rows
        )
        next_at = rows[-1].created_at if rows else request.after_created_at
        # v10 Control #1a: the role section is ADDITIVE — the instance section
        # above (entries + next_after_created_at + its raw-SQL read) is
        # untouched, so existing peer messaging is byte-for-byte unaffected.
        #
        # v10 Q1 fault-domain boundary (Architect-ruled 2026-06-19): the role
        # section is computed AFTER + INDEPENDENTLY of the instance section, and
        # ONLY its single seam (enumerate-held-roles + per-role query + k-way
        # merge) is wrapped. A role-side fault — a client's malformed
        # ``role_after`` (fails closed by contract), a k-way-merge edge, a
        # transient role-table read error — must NOT cost the caller its already
        # computed INSTANCE messages. This is NOT defensive swallowing: it is
        # loud (ERROR + traceback) and surfaced (``role_section_status=error`` +
        # the error repr) — correct partial-failure semantics for a two-section
        # response. A genuine SHARED fault (state store down) still fails the
        # instance read above first, so this never masks a total outage.
        (
            role_entries, next_role_cursor, role_floor_applied,
            role_history_cursor, role_status, role_error,
        ) = self._collect_role_section(request)
        role_page_truncated = (
            role_status is RoleSectionStatus.OK and next_role_cursor is not None
        )
        role_truncation_reason: RoleTruncationReason | None = None
        role_byte_ceiling: int | None = None
        if role_page_truncated:
            if len(role_entries) < request.limit:
                role_truncation_reason = RoleTruncationReason.BYTE_CEILING
                role_byte_ceiling = ROLE_SECTION_BYTE_CEILING
            else:
                role_truncation_reason = RoleTruncationReason.ROW_LIMIT
        role_read_page_token, role_read_page_status = self._role_read_page_outcome(
            request=request, role_status=role_status, role_entries=role_entries,
        )
        return PeerInbox(
            recipient_agent_id=request.recipient_agent_id,
            entries=entries,
            next_after_created_at=next_at,
            instance_exhausted=instance_exhausted,
            role_entries=role_entries,
            next_role_cursor=next_role_cursor,
            role_section_status=role_status,
            role_section_error=role_error,
            role_floor_applied=role_floor_applied,
            role_history_cursor=role_history_cursor,
            role_limit=request.limit,
            role_page_truncated=role_page_truncated,
            role_truncation_reason=role_truncation_reason,
            role_byte_ceiling=role_byte_ceiling,
            role_read_page_token=role_read_page_token,
            role_read_page_status=role_read_page_status,
        )

    def _role_read_page_outcome(
        self, *, request: PeerInboxRequest, role_status: RoleSectionStatus,
        role_entries: tuple[PeerInboxEntry, ...],
    ) -> tuple[str | None, str]:
        """Issue a pending page only for a successful non-observer role page."""
        if role_status is RoleSectionStatus.ERROR:
            return None, "error"
        if request.observer or not request.recipient_agent_session_id:
            return None, "disabled"
        if not role_entries:
            return None, "no_entries"
        try:
            if self._is_role_history_read(request):
                return None, "disabled"
            issued = self.issue_role_read_page(
                agent_session_id=request.recipient_agent_session_id,
                agent_instance_id=request.recipient_agent_instance_id,
                held_roles=tuple(self._enumerate_held_roles(request.recipient_agent_instance_id)),
                include_important=request.include_important,
                entries=role_entries,
            )
        except Exception:  # noqa: BLE001 - rows remain visible on issuance fault
            logger.exception("role read page issuance failed; preserving inbox rows")
            return None, "error"
        return issued.token, issued.status

    def _is_role_history_read(self, request: PeerInboxRequest) -> bool:
        if request.role_after is None:
            return False
        held = tuple(self._enumerate_held_roles(request.recipient_agent_instance_id))
        scope = RoleCursorScope(
            include_important=request.include_important,
            held_roles=held,
            agent_instance_id=request.recipient_agent_instance_id,
        )
        _, history = self._decode_role_after(request.role_after, scope)
        return history

    def _collect_role_section(
        self, request: PeerInboxRequest,
    ) -> tuple[
        tuple[PeerInboxEntry, ...], str | None, bool, str | None,
        RoleSectionStatus, str | None,
    ]:
        """Compute the role section inside the Q1 fault-domain boundary.

        Returns ``(role_entries, next_role_cursor, role_floor_applied,
        role_history_cursor, status, error)``. On success: the merged page +
        cursor + the pull-surface-boundary floor signal (design §3/§5b) +
        ``OK`` + ``None``. On ANY role-side failure: empty page, no cursor, no
        floor signal, ``ERROR`` + ``repr(exc)`` (logged at ERROR with
        traceback). The broad ``except`` is the deliberate fault-domain boundary
        — its scope is this one seam, so it cannot hide a fault anywhere else.
        """
        try:
            role_entries, next_role_cursor, role_floor_applied, role_history_cursor = (
                self.list_silent_for_roles(
                    agent_instance_id=request.recipient_agent_instance_id,
                    include_important=request.include_important,
                    limit=request.limit,
                    role_after=request.role_after,
                    observer=request.observer,
                )
            )
        except Exception as exc:  # noqa: BLE001 — the Q1 fault-domain boundary
            logger.exception(
                "peer_inbox role section failed for instance %s (role_after=%r); "
                "serving instance section only",
                request.recipient_agent_instance_id,
                request.role_after,
            )
            return (), None, False, None, RoleSectionStatus.ERROR, repr(exc)
        return (
            role_entries, next_role_cursor, role_floor_applied,
            role_history_cursor, RoleSectionStatus.OK, None,
        )

    # ------------------------------------------------------------------
    # Role-addressed delivery store (v10 Control #1) — state-interface ONLY
    #
    # core__agent_role_message is the single authoritative delivery envelope
    # for role-addressed peer messages. Every access below goes through the
    # high-level state interface (upsert_state / query_ordered / update_state);
    # there is deliberately NO raw SQL and NO core__agent_message projection.
    # ------------------------------------------------------------------

    def persist_role_message(
        self,
        *,
        recipient_kind: str,
        recipient_key: str,
        message_id: str,
        sender_agent_id: str,
        sender_agent_instance_id: str,
        sender_session_label: str | None,
        important: bool,
        content: MessageContent,
        sender_principal_kind: str = "unknown",
        sender_transport_principal: str = "unknown",
        sender_identity_trust: str = "unknown",
    ) -> RoleMessagePersisted:
        """Single authoritative write for a role-addressed message (B2 keystone).

        Exactly ONE persistence write — an idempotent ``upsert_state`` into
        ``core__agent_role_message`` keyed on the deterministic
        ``external_id``. No second table, no thread row, no
        ``core__agent_message`` projection — so there is no non-atomic
        dual-write and no "durable message with no delivery record" gap.

        Returns the ``message_id`` (the durable handle the caller surfaces)
        AND the persisted row's ``created_at``, read back after the write.
        The read-back is required, not incidental: ``upsert_state`` returns
        only ``{"generated_id", "upserted"}``, the column is set by the state
        standardizer rather than written here, and ``created_at`` is the exact
        quantity the role-inbox section orders and pages on — so it is the only
        value a watcher may advance its ``role_high_water`` mark against.
        See :class:`RoleMessagePersisted` for why a clock reading taken beside
        the write is NOT an acceptable substitute.
        """
        self._require_enabled()
        external_id = role_message_external_id(
            recipient_kind, recipient_key, message_id,
        )
        record: dict[str, object] = {
            "external_id": external_id,
            "recipient_kind": recipient_kind,
            "recipient_key": recipient_key,
            "message_id": message_id,
            "sender_agent_id": sender_agent_id,
            "sender_agent_instance_id": sender_agent_instance_id,
            "sender_session_label": sender_session_label,
            "sender_principal_kind": sender_principal_kind,
            "sender_transport_principal": sender_transport_principal,
            "sender_identity_trust": sender_identity_trust,
            # Synthetic, deterministic role-channel handle — display only,
            # never dereferenced as a live thread.
            "thread_id": f"{ROLE_THREAD_PREFIX}{recipient_key}",
            "important": important,
            "delivered": False,
            # REL-05 (Q5): a fresh role message is un-consumed. Written EXPLICITLY
            # (like ``delivered``) so the consumed-gated drain predicate matches it
            # under an equality filter, not only via the column's DB default.
            COL_CONSUMED: False,
            # RIDER-1: a fresh role message is not escalated. Explicit (same
            # reason as ``consumed``) so the drain's ``escalated=false`` filter
            # matches it under the equality grammar.
            COL_ESCALATED: False,
            "content": _serialize_role_content(content),
        }
        require_completed(
            self._state.upsert_state(
                _ROLE_NAMESPACE,
                {
                    "table": TABLE_AGENT_ROLE_MESSAGE,
                    "record": record,
                    "conflict_columns": ["external_id"],
                },
            ),
            "upsert agent_role_message",
        )
        row = self._read_owed_row(TABLE_AGENT_ROLE_MESSAGE, external_id)
        if row is None:
            # The row was just written under this exact external_id; its absence
            # means the write did not land as reported. Fail loudly rather than
            # dispatching with an empty timestamp — a caller that received ""
            # would silently stop advancing the watcher mark, which is a wake
            # storm rather than a visible error.
            raise AgentMessagingError(
                f"role message {external_id!r} not readable immediately after "
                "its own upsert reported success",
            )
        created_at = normalize_sort_value(row.get("created_at"))
        role_row_id = str(row.get("id", ""))
        if not role_row_id:
            raise AgentMessagingError("role message row omitted its durable envelope id")
        return RoleMessagePersisted(
            message_id=message_id, created_at=created_at, role_row_id=role_row_id,
        )

    def list_undelivered_for(
        self,
        *,
        recipient_kind: str,
        recipient_key: str,
        limit: int,
        after: tuple[object, object] | None = None,
    ) -> list[dict[str, object]]:
        """Oldest-first page of un-CONSUMED IMPORTANT messages for a recipient.

        The at-least-once drain (Control #5) calls this repeatedly until the
        page is empty. REL-05 (Q5): the stop predicate is ``consumed = false``
        (equality) — NOT ``delivered = false``. ``delivered`` stays the
        emission-bookkeeping bit (flipped on emission), but a message that was
        emitted yet never entered a turn context (Vector B) has
        ``consumed=false`` and is re-owed here. The re-emit WINDOW + CAP
        inequalities (which the equality-only state filter cannot express) are
        applied in Python by the caller (:meth:`list_undelivered_for_instance`).
        The composite ``(created_at, id)`` ascending order is genuinely
        oldest-first (the primitive rejects a non-composite order), so an old
        row's wait is bounded by its FIFO position and never starves under
        newer arrivals. A role recipient keys on ``recipient_key`` only →
        cross-kind takeover.
        """
        query: dict[str, object] = {
            "table": TABLE_AGENT_ROLE_MESSAGE,
            "filters": {
                "recipient_kind": recipient_kind,
                "recipient_key": recipient_key,
                "important": True,
                COL_CONSUMED: False,
                # RIDER-1 (starvation fix): terminal-escalated rows drop from
                # the owed query, so a capped-dormant row can no longer fill
                # the oldest limit-page and starve a genuinely-owed newer row.
                COL_ESCALATED: False,
            },
            "order_by": [("created_at", "asc"), ("id", "asc")],
            "limit": limit,
        }
        if after is not None:
            query["after"] = after
        result = self._state.query_ordered(_ROLE_NAMESPACE, query)
        return _result_records(result)

    def mark_delivered(self, *, external_id: str) -> None:
        """Flip ``delivered = true`` once the holder's transport emitted it.

        A single atomic ``update_state`` — idempotent (re-setting ``true`` is a
        no-op), so a dedup-suppressed re-encounter that still re-marks is
        safe. The row PERSISTS (audit); it is never deleted to signal delivery.
        REL-05 note: this flips ONLY ``delivered`` (the emission bit); the
        consumption stop (``consumed``) is stamped separately by
        :meth:`reconcile_role_consumption`, and the emit bookkeeping (F3 +
        cap) rides :meth:`mark_delivered_for_instance`.
        """
        require_completed(
            self._state.update_state(
                _ROLE_NAMESPACE,
                {
                    "table": TABLE_AGENT_ROLE_MESSAGE,
                    "filters": {"external_id": external_id},
                },
                {"delivered": True},
            ),
            "update agent_role_message.delivered",
        )

    def mark_delivered_for_instance(
        self,
        *,
        external_id: str,
        recipient_key: str,
        agent_instance_id: str,
        agent_session_id: str,
        activity_at_emission: str | None,
    ) -> bool:
        """Ownership-fenced emission confirm (Control #5 fence + REL-05 F3).

        Records an emission ONLY if ``agent_instance_id`` currently holds
        ``recipient_key`` — a displaced/zombie holder cannot mark deliveries or
        suppress another holder's drain. Flips ``delivered=true`` (the emission
        bit) AND records the REL-05 bookkeeping: ``emit_count += 1``,
        ``last_emitted_at = now``, and (F3) ``emitted_to_agent_instance_id =
        agent_instance_id`` — so the consumption stamp later requires activity
        from THIS instance, and displacement re-owes to the new holder. Returns
        ``True`` when recorded, ``False`` when the fence rejected.

        QUIET-GAP binding (a): ``activity_at_emission`` — the recipient's
        ``last_model_activity_at`` as of THIS emission — is re-captured on every
        confirm, so each emission is tested against its own quiet gap rather than
        against a pair that keeps sliding forward after the fact.

        emit_count precision note: the count is incremented per confirmed
        emission. A rare live-event-vs-repair-drain race for the same id can
        double-count by 1 (both settle callers reach the confirm while the
        single-flight ledger emits once) — bounded, and it biases toward FEWER
        re-emits (safe: never a false-positive loss).
        """
        if recipient_key not in self._enumerate_held_roles(agent_instance_id):
            return False
        row = self._read_owed_row(TABLE_AGENT_ROLE_MESSAGE, external_id)
        emit_count = _as_int(row.get(COL_EMIT_COUNT)) if row else 0
        require_completed(
            self._state.update_state(
                _ROLE_NAMESPACE,
                {
                    "table": TABLE_AGENT_ROLE_MESSAGE,
                    "filters": {"external_id": external_id},
                },
                {
                    "delivered": True,
                    COL_EMIT_COUNT: emit_count + 1,
                    COL_LAST_EMITTED_AT: self._clock().isoformat(),
                    COL_EMITTED_TO_AGENT_INSTANCE_ID: agent_instance_id,
                    COL_EMITTED_TO_AGENT_SESSION_ID: agent_session_id,
                    COL_ACTIVITY_AT_EMISSION: activity_at_emission,
                },
            ),
            "confirm agent_role_message emission",
        )
        return True

    def list_undelivered_for_instance(
        self,
        *,
        agent_instance_id: str,
        limit: int,
        now: datetime | None = None,
        re_emit_window_s: float = DEFAULT_RE_EMIT_WINDOW_S,
        cap: int = DEFAULT_RE_EMIT_CAP,
    ) -> list[dict[str, object]]:
        """Oldest-first repair-drain page across every role the instance holds.

        The Control #5 repair loop (and the ``POST /peer/drain`` route) call
        this each pass: enumerate the roles bound to ``agent_instance_id`` (the
        fence — only roles this instance currently holds), gather their
        un-CONSUMED IMPORTANT rows oldest-first, and global-merge to the oldest
        ``limit``. REL-05 (Q5): a row is owed while ``consumed=false``; the
        re-emit WINDOW (no re-emit sooner than ``re_emit_window_s`` after the
        last emission) and the CAP (``emit_count < cap``) are applied in Python
        here (the equality-only state filter cannot express those inequalities),
        PER ROLE before the merge so a within-window row never occupies a merge
        slot ahead of an eligible one. Returns ``[]`` for a holder with no roles.
        """
        held_roles = self._enumerate_held_roles(agent_instance_id)
        if not held_roles:
            return []
        cutoff = now or self._clock()
        per_role = [
            self._eligible_undelivered_for_role(
                role=role,
                limit=limit,
                now=cutoff,
                re_emit_window_s=re_emit_window_s,
                cap=cap,
            )
            for role in held_roles
        ]
        return merge_undelivered_oldest_first(per_role, limit)

    def _eligible_undelivered_for_role(
        self,
        *,
        role: str,
        limit: int,
        now: datetime,
        re_emit_window_s: float,
        cap: int,
    ) -> list[dict[str, object]]:
        """Collect one role's first ``limit`` eligible owed rows by keyset scan.

        The cap/window predicate cannot be expressed by the equality-only
        state interface. Applying a raw ``limit`` before that Python filter
        would therefore re-read the same capped prefix forever and hide a
        newer eligible row. Advance on the raw oldest-first ``(created_at,
        id)`` key until enough eligible rows are found or the role is
        exhausted; only then does the existing global merge apply its limit.
        """
        eligible: list[dict[str, object]] = []
        after: tuple[object, object] | None = None
        while len(eligible) < limit:
            raw_page = self.list_undelivered_for(
                recipient_kind=RECIPIENT_KIND_ROLE,
                recipient_key=role,
                limit=limit,
                after=after,
            )
            if not raw_page:
                break
            eligible.extend(
                _owed_after_window_and_cap(
                    raw_page,
                    now=now,
                    re_emit_window_s=re_emit_window_s,
                    cap=cap,
                )
            )
            if len(raw_page) < limit:
                break
            last = raw_page[-1]
            after = (last.get("created_at"), last.get("id"))
        return eligible[:limit]

    def mark_role_consumed_on_ack(self, *, external_id: str) -> bool:
        """Watcher events-ack consumption for ONE role row.

        A no-MCP watcher acknowledged the bridge event carrying this role
        delivery — its long-poll cursor moved past it, so the bytes are
        provably streamed into the watch output. That is the pull-recipient
        equivalent of entering a turn: stamp ``consumed`` (drops the row from
        the owed drain and the escalation sweep) and flip ``delivered`` (the
        ack IS the emission confirm a forwarder would otherwise POST).
        Predicated on ``important=true AND consumed=false`` so re-acks and
        silent rows are no-ops. Returns True when a row was stamped.
        """
        return 0 < require_updated(
            self._state.update_state(
                _ROLE_NAMESPACE,
                {
                    "table": TABLE_AGENT_ROLE_MESSAGE,
                    "filters": {
                        "external_id": external_id,
                        "important": True,
                        COL_CONSUMED: False,
                    },
                },
                {
                    "delivered": True,
                    COL_CONSUMED: True,
                    COL_CONSUMED_AT: self._clock().isoformat(),
                },
            ),
        )

    # A4 (2026-08-04): the REL-05 direct-IMPORTANT-send outbox
    # (core__agent_direct_wake) and the ESCALATION half of the role delivery
    # apparatus (list_escalatable_direct/role, mark_direct_escalated,
    # mark_role_escalated, _stamp_consumed_rows, reconcile_direct_consumption,
    # persist_direct_wake, rehome_owed_direct_wakes, list_owed_direct_for_instance,
    # mark_direct_emitted_for_instance, mark_direct_consumed_on_ack) retired
    # here — Guard 1 ("no model-initiated call since emission") was blind to
    # local-only work; sweep_overdue_sessions + _notify_steward_of_overdue
    # (session_sweep.py, D1) is the sole successor. The ROLE delivery
    # durability guarantee (list_undelivered_for/list_undelivered_for_instance,
    # mark_delivered_for_instance, mark_role_consumed_on_ack, persist_role_message)
    # is untouched. core__agent_direct_wake and the role message's
    # escalation-only columns (escalated/escalated_at/escalation_reason,
    # emitted_to_agent_instance_id/emitted_to_agent_session_id) are left in
    # the schema, code-only retirement per operator ruling — tracked as
    # follow-on debt, not dropped this slice.

    def _read_owed_row(
        self, table: str, external_id: str,
    ) -> dict[str, object] | None:
        """Read one row by ``external_id`` from ``table`` (or ``None``)."""
        result = self._state.query_state(
            _ROLE_NAMESPACE,
            {"table": table, "filters": {"external_id": external_id}},
        )
        rows = require_records(result)
        return rows[0] if rows else None

    def list_silent_for_roles(
        self,
        *,
        agent_instance_id: str,
        include_important: bool,
        limit: int,
        role_after: str | None,
        observer: bool = False,
    ) -> tuple[tuple[PeerInboxEntry, ...], str | None, bool, str | None]:
        """The role-inbox section — a global ``(created_at, id)`` k-way merge.

        Enumerates the roles ``agent_instance_id`` currently holds from
        ``agent_role_binding`` (NOT ``resolve_role``, which is per-name and
        cannot enumerate — Codex check #2), runs a per-role recent-first
        ``query_ordered`` over ``agent_role_message``, applies the
        pull-surface-boundary floor (design §3/§5b — post-fetch, per role,
        against ``role_covered_mark``), k-way merges into the global
        top-``limit`` (with the byte ceiling, §4), and pages by an opaque
        scope-bound cursor.

        Default ``include_important=True`` is the catch-up/recovery view: it
        OMITS both the ``important`` and ``delivered`` filters so already-
        delivered IMPORTANT rows resurface (there is deliberately no
        ``core__agent_message`` projection to fall back on).
        ``include_important=False`` is an explicit silent-only status view.
        Returns ``((), None, False, None)`` for a holder with no roles.
        Re-readable + durable (rows are never consumed on read).
        """
        held_roles = self._enumerate_held_roles(agent_instance_id)
        if not held_roles:
            return (), None, False, None
        scope = RoleCursorScope(
            include_important=include_important,
            held_roles=tuple(held_roles),
            agent_instance_id=agent_instance_id,
        )
        after, skip_floor = self._decode_role_after(role_after, scope)
        marks = {} if skip_floor else self._read_role_covered_marks(held_roles)
        per_role_records: list[list[dict[str, object]]] = []
        any_floor_truncated = False
        for role in held_roles:
            records = self._query_visible_role_page(
                recipient_key=role,
                include_important=include_important,
                limit=limit,
                after=after,
                agent_instance_id=agent_instance_id,
                filter_receipts=not observer and not skip_floor,
            )
            records, truncated = _apply_role_floor(records, marks.get(role))
            any_floor_truncated = any_floor_truncated or truncated
            per_role_records.append(records)
        resume_seed = _max_role_key(marks.values()) if marks else None
        return build_role_section(
            per_role_records,
            scope=scope,
            limit=limit,
            any_floor_truncated=any_floor_truncated,
            resume_seed=resume_seed,
        )

    def _query_visible_role_page(
        self, *, recipient_key: str, include_important: bool, limit: int,
        after: tuple[object, ...] | None, agent_instance_id: str,
        filter_receipts: bool,
    ) -> list[dict[str, object]]:
        """Refill through acknowledged rows without crossing an unserved row."""
        visible: list[dict[str, object]] = []
        scan_after = after
        scanned_pages = 0
        while len(visible) < limit + 1:
            if scanned_pages >= _ROLE_RECEIPT_REFILL_MAX_PAGES:
                raise AgentMessagingError("role receipt refill exceeded bounded scan budget")
            raw = self._query_role_page(
                recipient_key=recipient_key, include_important=include_important,
                limit=limit, after=scan_after,
            )
            scanned_pages += 1
            for record in raw:
                if not filter_receipts or not self._has_role_read_receipt(
                    recipient_key=recipient_key, agent_instance_id=agent_instance_id,
                    role_row_id=str(record.get("id", "")),
                ):
                    visible.append(record)
                    if len(visible) == limit + 1:
                        return visible
            if len(raw) < limit + 1:
                return visible
            scan_after = (raw[-1].get("created_at"), raw[-1].get("id"))
        return visible

    def _enumerate_held_roles(self, agent_instance_id: str) -> list[str]:
        """Roles currently bound to ``agent_instance_id`` (single state read).

        Touches the plugin-owned ``agent_role_binding`` namespace — the state
        interface is namespace-per-call, so one ``peer_inbox`` legitimately
        spans the ``core`` envelope namespace and this binding namespace.
        """
        result = self._state.query_state(
            AGENT_ROLE_BINDING_NAMESPACE,
            {
                # §9 CUTOVER: the role-inbox enumeration reads the v4 table.
                "table": TABLE_ROLE_BINDING,
                "filters": {COL_AGENT_INSTANCE_ID: agent_instance_id},
            },
        )
        roles: list[str] = []
        for record in _result_records(result):
            role = record.get(COL_ROLE)
            if isinstance(role, str) and role:
                roles.append(role)
        return roles

    def _has_role_read_receipt(
        self, *, recipient_key: str, agent_instance_id: str, role_row_id: str,
    ) -> bool:
        """Receipt existence is the sole weak-suppression authority."""
        if not role_row_id:
            return False
        row = self._read_owed_row(
            TABLE_ROLE_READ_RECEIPT,
            receipt_external_id(recipient_key, agent_instance_id, role_row_id),
        )
        return row is not None

    def issue_role_read_page(
        self,
        *,
        agent_session_id: str,
        agent_instance_id: str,
        held_roles: tuple[str, ...],
        include_important: bool,
        entries: tuple[PeerInboxEntry, ...],
    ) -> RoleReadPage:
        """Persist a pending exact page before returning its opaque token.

        This writes no receipts.  It is intentionally called only after the
        normal role page has been assembled, so cursor input can never choose
        page item content.
        """
        if not agent_session_id:
            raise AgentRequestInvalidError("recipient_agent_session_id is required for role page issuance")
        item_specs = [self._role_read_item_spec(entry) for entry in entries]
        if len(item_specs) > 1000:
            raise AgentRequestInvalidError("role read page exceeds 1000 issued items")
        token = page_token()
        digest = token_digest(token)
        external_id = page_external_id(digest)
        now = self._clock().isoformat()
        require_completed(self._state.upsert_state(
            _ROLE_NAMESPACE,
            {"table": TABLE_ROLE_READ_PAGE, "record": {
                "external_id": external_id, "token_hash": digest,
                "agent_session_id": agent_session_id,
                "agent_instance_id": agent_instance_id,
                "held_role_scope_hash": held_role_scope_hash(held_roles),
                "include_important": include_important, "status": "pending",
                "expected_item_count": len(item_specs),
                "item_digest": page_item_digest(item_specs),
                "issued_at": now,
            }, "conflict_columns": ["external_id"]},
        ), "issue role_read_page")
        for recipient_key, role_row_id, message_id, created_at in item_specs:
            require_completed(self._state.upsert_state(
                _ROLE_NAMESPACE,
                {"table": TABLE_ROLE_READ_PAGE_ITEM, "record": {
                    "external_id": page_item_external_id(external_id, role_row_id),
                    "page_external_id": external_id,
                    "recipient_key": recipient_key,
                    "role_row_id": role_row_id,
                    "role_message_id": message_id,
                    "role_created_at": created_at,
                }, "conflict_columns": ["external_id"]},
            ), "issue role_read_page_item")
        return RoleReadPage(token=token, status="pending_ack", item_count=len(item_specs))

    @staticmethod
    def _role_read_item_spec(entry: PeerInboxEntry) -> tuple[str, str, str, str]:
        metadata = entry.message.metadata
        recipient_key = str(metadata.get("recipient_key", ""))
        role_row_id = str(metadata.get("role_row_id", ""))
        created_at = str(metadata.get("role_created_at", ""))
        if not recipient_key or not role_row_id or not created_at:
            raise AgentMessagingError("role page entry omitted immutable envelope identity")
        return recipient_key, role_row_id, entry.message.id, created_at

    def acknowledge_role_read_page(
        self, *, agent_session_id: str, agent_instance_id: str, token: str,
    ) -> RoleReadAck:
        """Convert immutable server-issued items to exact display receipts."""
        if not token:
            raise AgentRequestInvalidError("page_token is required")
        page = self._read_owed_row(TABLE_ROLE_READ_PAGE, page_external_id(token_digest(token)))
        if page is None:
            raise AgentRequestInvalidError("unknown role read page token")
        if (page.get("agent_session_id") != agent_session_id
                or page.get("agent_instance_id") != agent_instance_id):
            raise AgentRequestInvalidError("role read page token belongs to another session or instance")
        current_roles = tuple(self._enumerate_held_roles(agent_instance_id))
        if page.get("held_role_scope_hash") != held_role_scope_hash(current_roles):
            raise AgentRequestInvalidError("role read page scope changed since issuance")
        items = self._read_complete_role_read_page_items(page)
        if str(page.get("status")) == "acked":
            return RoleReadAck(status="already_acked", receipt_count=len(items))
        for item in items:
            recipient_key = str(item.get("recipient_key", ""))
            row_id = str(item.get("role_row_id", ""))
            if not self._has_role_read_receipt(
                recipient_key=recipient_key, agent_instance_id=agent_instance_id,
                role_row_id=row_id,
            ):
                require_completed(self._state.upsert_state(
                    _ROLE_NAMESPACE,
                    {"table": TABLE_ROLE_READ_RECEIPT, "record": {
                        "external_id": receipt_external_id(recipient_key, agent_instance_id, row_id),
                        "recipient_key": recipient_key, "agent_instance_id": agent_instance_id,
                        "role_row_id": row_id,
                        "role_message_id": str(item.get("role_message_id", "")),
                        "role_created_at": str(item.get("role_created_at", "")),
                        "acknowledged_at": self._clock().isoformat(),
                    }, "conflict_columns": ["external_id"], "on_conflict": "do_nothing"},
                ), "insert role_read_receipt")
            self._advance_role_read_watermark(
                recipient_key=recipient_key, agent_instance_id=agent_instance_id,
                created_at=str(item.get("role_created_at", "")), row_id=row_id,
                message_id=str(item.get("role_message_id", "")),
            )
        require_completed(self._state.update_state(
            _ROLE_NAMESPACE,
            {"table": TABLE_ROLE_READ_PAGE, "filters": {"external_id": str(page.get("external_id", "")), "status": "pending"}},
            {"status": "acked", "acked_at": self._clock().isoformat()},
        ), "ack role_read_page")
        return RoleReadAck(status="acked", receipt_count=len(items))

    def _read_complete_role_read_page_items(
        self, page: dict[str, object],
    ) -> list[dict[str, object]]:
        expected_count = page.get("expected_item_count")
        expected_digest = page.get("item_digest")
        if not isinstance(expected_count, int) or not isinstance(expected_digest, str):
            raise AgentMessagingError("role read page is missing completeness metadata")
        if expected_count < 0 or expected_count > 1000:
            raise AgentMessagingError("role read page item count is out of bounds")
        items = require_records(self._state.query_ordered(
            _ROLE_NAMESPACE,
            {"table": TABLE_ROLE_READ_PAGE_ITEM,
             "filters": {"page_external_id": str(page.get("external_id", ""))},
             "order_by": [("role_row_id", "asc"), ("external_id", "asc")],
             "limit": 1001, "unbounded": True},
        ))
        item_specs = [
            (str(item.get("recipient_key", "")), str(item.get("role_row_id", "")),
             str(item.get("role_message_id", "")), str(item.get("role_created_at", "")))
            for item in items
        ]
        if len(items) != expected_count or page_item_digest(item_specs) != expected_digest:
            raise AgentMessagingError("role read page items are incomplete or malformed")
        return items

    def _advance_role_read_watermark(
        self, *, recipient_key: str, agent_instance_id: str, created_at: str,
        row_id: str, message_id: str,
    ) -> None:
        """Bounded compare-and-swap update; receipts remain authoritative."""
        external_id = watermark_external_id(recipient_key, agent_instance_id)
        proposed = (created_at, row_id)
        for _ in range(8):
            old = self._read_owed_row(TABLE_ROLE_READ_WATERMARK, external_id)
            if old is None:
                require_completed(self._state.upsert_state(
                    _ROLE_NAMESPACE,
                    {"table": TABLE_ROLE_READ_WATERMARK, "record": {
                        "external_id": external_id, "recipient_key": recipient_key,
                        "agent_instance_id": agent_instance_id, "read_created_at": created_at,
                        "read_id": row_id, "read_message_id": message_id,
                        "observed_at": self._clock().isoformat(),
                    }, "conflict_columns": ["external_id"], "on_conflict": "do_nothing"},
                ), "insert role_read_watermark")
                # A lower concurrent writer may have won the insert. Re-read
                # before deciding the proposed boundary is already stored.
                old = self._read_owed_row(TABLE_ROLE_READ_WATERMARK, external_id)
                if old is None:
                    raise AgentMessagingError("role read watermark vanished after insert")
            current = (str(old.get("read_created_at", "")), str(old.get("read_id", "")))
            if current >= proposed:
                return
            updated = require_updated(self._state.update_state(
                _ROLE_NAMESPACE,
                {"table": TABLE_ROLE_READ_WATERMARK, "filters": {
                    "external_id": external_id, "read_created_at": current[0], "read_id": current[1],
                }},
                {"read_created_at": created_at, "read_id": row_id,
                 "read_message_id": message_id, "observed_at": self._clock().isoformat()},
            ))
            if updated == 1:
                return
        raise AgentMessagingError("role read watermark compare-and-swap exhausted")

    def role_read_receipts(
        self, *, agent_instance_id: str, candidates: list[tuple[str, str]],
    ) -> tuple[RoleReadReceiptResult, ...]:
        """Read-only exact receipt lookup for a caller's currently held roles."""
        held = set(self._enumerate_held_roles(agent_instance_id))
        if len(candidates) > 1000:
            raise AgentRequestInvalidError("too many role receipt candidates")
        if len(json.dumps(candidates, separators=(",", ":")).encode()) > 256 * 1024:
            raise AgentRequestInvalidError("role receipt candidates exceed 256KiB")
        results: list[RoleReadReceiptResult] = []
        for recipient_key, role_row_id in candidates:
            if recipient_key not in held:
                raise AgentRequestInvalidError("receipt candidate is outside the caller's held roles")
            results.append(RoleReadReceiptResult(
                recipient_key=recipient_key, role_row_id=role_row_id,
                served=self._has_role_read_receipt(
                    recipient_key=recipient_key, agent_instance_id=agent_instance_id,
                    role_row_id=role_row_id,
                ),
            ))
        return tuple(results)

    def _decode_role_after(
        self, role_after: str | None, scope: RoleCursorScope,
    ) -> tuple[tuple[object, ...] | None, bool]:
        """Decode the inbound ``role_after`` cursor against the current scope.

        Returns ``(after_cursor, skip_floor)``. ``None`` (no cursor) or a
        scope change (held-role set / visibility mode differs from issue
        time) → first page (``after=None``), so a newly-visible row is never
        silently skipped. A malformed/forged token fails closed as
        :class:`AgentRequestInvalidError`.

        ``skip_floor`` is True ONLY when ``role_after`` decodes to a VALID
        history token for the CURRENT scope (design §5b.ii) — the sole route
        by which a caller disables the pull-surface-boundary floor. An
        absent cursor, an ordinary continuation cursor, and a scope-changed
        reset all leave the floor enabled: fail toward the floor being
        applied (over-page-protected), never toward silently skipping it.
        """
        if role_after is None:
            return None, False
        try:
            decoded = decode_role_cursor(role_after, scope)
        except RoleCursorRejectedError as exc:
            raise AgentRequestInvalidError(str(exc)) from exc
        if decoded.outcome is RoleCursorOutcome.SCOPE_CHANGED:
            return None, False
        return (
            decoded.created_at_iso or decoded.created_at,
            decoded.row_id,
        ), decoded.is_history_token

    def _query_role_page(
        self,
        *,
        recipient_key: str,
        include_important: bool,
        limit: int,
        after: tuple[object, ...] | None,
    ) -> list[dict[str, object]]:
        """One per-role newest-first page plus a truthful-exhaustion lookahead."""
        filters: dict[str, object] = {
            "recipient_kind": RECIPIENT_KIND_ROLE,
            "recipient_key": recipient_key,
        }
        if not include_important:
            # Silent bucket only. include_important=True omits BOTH the
            # important and delivered filters (catch-up resurfaces delivered
            # IMPORTANT rows).
            filters["important"] = False
        query: dict[str, object] = {
            "table": TABLE_AGENT_ROLE_MESSAGE,
            "filters": filters,
            "order_by": [("created_at", "desc"), ("id", "desc")],
            # Each role supplies one extra row.  The global merge needs only
            # the top ``limit`` rows plus one lookahead to know whether a
            # continuation actually exists; limit+1 per role is sufficient
            # for that global top-(limit+1) result.
            "limit": limit + 1,
            "unbounded": True,
        }
        if after is not None:
            query["after"] = after
        result = self._state.query_ordered(_ROLE_NAMESPACE, query)
        return _result_records(result)

    def _read_role_covered_marks(
        self, held_roles: list[str],
    ) -> dict[str, tuple[str, str]]:
        """Point-lookup of the covered mark for each held role, by
        ``external_id`` (the design §1 index: one row per role, point
        lookups only — no range/IN-list query, so this reuses
        ``_read_owed_row`` exactly as every other single-row read on this
        service does, rather than a list-valued filter the state interface's
        ``query_state`` grammar has no established contract for here).

        A role absent from the returned dict has no mark — the floor applies
        no bound for it (§12.3 fail-direction: absent mark ⇒ query unchanged
        from today, over-page never under-page).
        """
        marks: dict[str, tuple[str, str]] = {}
        for role in held_roles:
            row = self._read_owed_row(
                TABLE_ROLE_COVERED_MARK, role_covered_mark_external_id(role),
            )
            if row is None:
                continue
            created_at = row.get("covered_created_at")
            row_id = row.get("covered_id")
            if isinstance(created_at, str) and isinstance(row_id, str):
                marks[role] = (created_at, row_id)
        return marks

    def mark_role_covered(
        self,
        *,
        recipient_key: str,
        message_id: str,
        attested_by_agent_instance_id: str,
        attested_by_agent_session_id: str,
        attested_by_session_label: str,
    ) -> RoleCoveredMark:
        """Attest ``recipient_key`` covered through ``message_id`` (design §2).

        Attest-BY-``message_id``, never by a caller-asserted ``(created_at,
        id)`` pair: looks the row up via ``role_message_external_id`` — the
        same deterministic key ``_read_owed_row`` already uses for the drain
        — and stores THAT ROW's own pair. A caller can only ever name a pair
        that corresponds to a row that exists; this is structural, not a
        validation branch that could be skipped or gotten wrong later.

        Monotonic: an attestation at or below the stored mark is a no-op,
        returning the PRE-EXISTING mark unchanged. Compared via
        ``normalize_sort_value`` on the ``(created_at, id)`` tuple — the same
        comparator ``_merge_sort_key``/the floor filter already use, so this
        can never disagree with them on an edge value.

        Identity fencing (R1) is the CALLER's responsibility — this method
        trusts its three ``attested_by_*`` arguments as already server-
        sourced from the calling route's live ``peer_binding``, never from a
        caller-supplied argument. See
        ``plugin::agent_messaging_plugin::peer_mark_role_covered`` for the
        registered-route-only fence and the live role-ownership re-check.
        """
        external_id = role_message_external_id(
            RECIPIENT_KIND_ROLE, recipient_key, message_id,
        )
        row = self._read_owed_row(TABLE_AGENT_ROLE_MESSAGE, external_id)
        if row is None:
            raise AgentRequestInvalidError(
                f"no role message {message_id!r} found for role "
                f"{recipient_key!r} — cannot attest a row that does not exist",
            )
        new_key = (
            normalize_sort_value(row.get("created_at")),
            normalize_sort_value(row.get("id")),
        )
        mark_external_id = role_covered_mark_external_id(recipient_key)
        existing = self._read_owed_row(TABLE_ROLE_COVERED_MARK, mark_external_id)
        if existing is not None:
            existing_key = (
                normalize_sort_value(existing.get("covered_created_at")),
                normalize_sort_value(existing.get("covered_id")),
            )
            if existing_key >= new_key:
                return _project_role_covered_mark(existing)
        attested_at = self._clock().isoformat()
        record: dict[str, object] = {
            "external_id": mark_external_id,
            "recipient_key": recipient_key,
            "covered_created_at": new_key[0],
            "covered_id": new_key[1],
            "covered_message_id": message_id,
            "attested_by_agent_instance_id": attested_by_agent_instance_id,
            "attested_by_agent_session_id": attested_by_agent_session_id,
            "attested_by_session_label": attested_by_session_label,
            "attested_at": attested_at,
        }
        require_completed(
            self._state.upsert_state(
                _ROLE_NAMESPACE,
                {
                    "table": TABLE_ROLE_COVERED_MARK,
                    "record": record,
                    "conflict_columns": ["external_id"],
                },
            ),
            "upsert role_covered_mark",
        )
        return RoleCoveredMark(
            recipient_key=recipient_key,
            covered_created_at=new_key[0],
            covered_id=new_key[1],
            covered_message_id=message_id,
            attested_at=attested_at,
        )

    # ------------------------------------------------------------------
    # Internals — validation / lookup
    # ------------------------------------------------------------------

    def _require_enabled(self) -> None:
        if not self._config.enabled:
            raise AgentMessagingDisabledError(
                "agent_messaging is disabled in plugin config",
            )

    def _create_peer_thread(self, request: PeerSendRequest) -> AgentThreadRow:
        """Snapshot session_labels + agent_instance_ids onto a new peer thread.

        Per 2026-05-31 Architect ruling §2: the 4 actor-snapshot columns
        and the title format are written ONCE at create_thread time and
        NEVER repeated on append_message. A later ``/rename`` on either
        endpoint does NOT propagate to historical rows.
        """
        sender_label = (
            request.sender_session_label or request.sender_agent_id
        )
        peer_label = (
            request.peer_session_label or request.peer_agent_id
        )
        return self._repo.create_thread(
            originator_type=OriginatorType.MCP_BRIDGE,
            originator_bridge_id=request.sender_bridge_id,
            originator_session_id=request.sender_bridge_id,
            target_backend=f"peer:{request.peer_agent_id}",
            target_plugin_name="agent_messaging_plugin",
            status=ThreadStatus.IDLE,
            title=f"peer: {sender_label} -> {peer_label}",
            metadata={
                "sender_agent_id": request.sender_agent_id,
                "peer_agent_id": request.peer_agent_id,
            },
            recipient_agent_instance_id=request.peer_agent_instance_id,
            recipient_agent_session_id=request.peer_agent_session_id,
            originator_session_label=(
                request.sender_session_label or None
            ),
            originator_agent_instance_id=(
                request.sender_agent_instance_id or None
            ),
            recipient_session_label=(
                request.peer_session_label or None
            ),
        )

    def _validate_content(self, content: MessageContent) -> None:
        if not content:
            raise AgentRequestInvalidError("content must contain at least one part")
        size = sum(
            len(part.text.encode("utf-8")) for part in content
        )
        if size > self._config.max_message_bytes:
            raise AgentRequestInvalidError(
                f"content exceeds max_message_bytes "
                f"({size} > {self._config.max_message_bytes})",
            )


__all__ = [
    "AgentMessagingConfig",
    "AgentMessagingDisabledError",
    "AgentMessagingError",
    "AgentMessagingService",
    "AgentRequestInvalidError",
    "role_message_external_id",
]
