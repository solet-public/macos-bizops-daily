"""Repository — persistence adapter for agent thread/message storage.

The repository is a pure persistence boundary: no policy, no validation,
no service-resolution logic.  All of that lives in the service.

All persistence goes through the state-management interface — reads via
``query_state`` / ``query_ordered``; writes via ``write_state`` /
``update_state`` (incl. the ``status = ANY`` compare-and-set) /
``increment_and_return`` (the atomic cursor allocator) plus the typed-txn ops
inside ``state_service.transactional()`` for the multi-statement
``append_message``. No raw SQL strings remain (SQL-lockdown W1–W5 complete).
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import cast

from ananta.interfaces.state_management_interface import (
    StateManagementInterface,
    StateTransaction,
)

from .models import (
    ID_PREFIX_MESSAGE,
    ID_PREFIX_THREAD,
    AgentMessageRow,
    AgentThreadRow,
    ArtifactRef,
    MessageContent,
    MessageKind,
    MessageRole,
    OriginatorType,
    TextPart,
    ThreadStatus,
)
from .schema import TABLE_AGENT_MESSAGE, TABLE_AGENT_THREAD

_NAMESPACE = "core"

_MAX_LIST_LIMIT = 100


class RepositoryError(RuntimeError):
    """Raised when a repository invariant is violated.

    The service layer is responsible for translating these into
    structured user-facing error payloads.
    """


@dataclass(frozen=True, slots=True)
class NewMessage:
    """Inputs to :meth:`AgentMessagingRepository.append_message`."""

    role: MessageRole
    kind: MessageKind
    content: MessageContent
    action_id: str | None = None
    backend_session_id: str | None = None
    error: dict[str, object] | None = None
    artifacts: tuple[ArtifactRef, ...] = ()
    metadata: dict[str, object] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ThreadStatusUpdate:
    """Optional thread-status mutation to bundle with a message append.

    All fields are applied atomically with the cursor allocation and
    message insert, so the post-mutation state is observable as soon as
    the transaction commits.
    """

    status: ThreadStatus | None = None
    active_action_id: str | None = None
    active_flow_id: str | None = None
    backend_session_id: str | None = None
    clear_active_action: bool = False
    set_closed_at: bool = False


class AgentMessagingRepository:
    """SQL-backed persistence for agent threads and messages."""

    def __init__(
        self,
        state_service: StateManagementInterface,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._state = state_service
        self._clock = clock or (lambda: datetime.now(UTC))

    # ------------------------------------------------------------------
    # Thread reads
    # ------------------------------------------------------------------

    def get_thread(self, thread_id: str) -> AgentThreadRow | None:
        # Phase-0 migration (SQL-lockdown): single-namespace equality read via
        # the state interface, replacing the raw `SELECT … WHERE id = %s`.
        # No is_deleted filter (the raw read had none; query_state does not
        # auto-exclude soft-deleted), so the row set is byte-identical.
        result = self._state.query_state(
            _NAMESPACE,
            {"table": TABLE_AGENT_THREAD, "filters": {"id": thread_id}},
        )
        records = _records(result)
        return _row_to_thread(records[0]) if records else None

    def find_peer_thread(
        self,
        *,
        originator_bridge_id: str,
        peer_agent_id: str,
        peer_agent_instance_id: str,
    ) -> AgentThreadRow | None:
        """Look up the persistent thread for a (sender, peer-instance) pair.

        Peer threads use ``target_backend = 'peer:<agent_id>'`` plus
        the ``recipient_agent_instance_id`` column to disambiguate
        when multiple instances of the same agent_id exist.  Strict
        equality on both — there is no fuzzy fallback.
        """
        # Phase-0 migration (SQL-lockdown): ordered + limited read via the
        # state interface, replacing the raw `… ORDER BY created_at DESC
        # LIMIT 1`. The (id, desc) tie-break is FORCED by query_ordered's
        # >=2-order-col contract and is a safe STRENGTHENING: it picks a
        # deterministic member of the raw query's previously-UNDEFINED
        # created_at-tie ordering — any row it returns was already a valid
        # result of the bare `created_at DESC`, and the caller (peer-send)
        # treats any matching peer thread as valid. The implicit `is_deleted=0`
        # is a no-op (peer threads are never soft-deleted).
        result = self._state.query_ordered(
            _NAMESPACE,
            {
                "table": TABLE_AGENT_THREAD,
                "filters": {
                    "originator_bridge_id": originator_bridge_id,
                    "target_backend": f"peer:{peer_agent_id}",
                    "recipient_agent_instance_id": peer_agent_instance_id,
                },
                "order_by": [["created_at", "desc"], ["id", "desc"]],
                "limit": 1,
            },
        )
        records = _records(result)
        return _row_to_thread(records[0]) if records else None

    def list_threads(
        self,
        *,
        after: tuple[datetime, str] | None,
        limit: int,
        include_deleted: bool,
    ) -> list[AgentThreadRow]:
        """Cursor-paginated GLOBAL enumeration of threads, ``(created_at, id)`` asc.

        The GAP-5/D1 substrate read: instead of a consumer hand-rolling a raw
        ``SELECT … FROM core__agent_thread WHERE created_at > … ORDER BY
        created_at`` scan, it pages the whole table through the owning
        interface. Ordered by the tie-safe composite ``(created_at, id)``
        (``query_ordered``'s >=2-order-col contract gives a total order across
        equal ``created_at`` — a created_at-only cursor would split a
        same-timestamp group at a page boundary and silently drop rows). The
        ``after`` cursor is the previous page's last ``(created_at, id)``; its
        ``datetime`` is normalized to naive UTC by the shared ``query_ordered``
        parse path (the storage seam), so a tz-aware cursor still binds a
        type-matched ``timestamp`` param. ``include_deleted`` threads through
        to ``query_ordered``'s ``is_deleted`` gate (default excludes
        soft-deleted). The page is capped at ``_MAX_LIST_LIMIT``; since the
        read is cursor-paginated the cap is the page size, NOT silent
        truncation (the caller pages on with the returned cursor).
        """
        capped = max(1, min(limit, _MAX_LIST_LIMIT))
        query: dict[str, object] = {
            "table": TABLE_AGENT_THREAD,
            "filters": {},
            "order_by": [["created_at", "asc"], ["id", "asc"]],
            "limit": capped,
            "include_deleted": include_deleted,
        }
        if after is not None:
            query["after"] = [after[0], after[1]]
        result = self._state.query_ordered(_NAMESPACE, query)
        records = _records(result)
        return [_row_to_thread(record) for record in records]

    def list_peer_messages_for(
        self,
        *,
        recipient_agent_id: str,
        recipient_agent_instance_id: str,
        recipient_agent_session_id: str = "",
        after_created_at: datetime | None,
        limit: int,
        silent_only: bool = True,
    ) -> tuple[list[AgentMessageRow], bool]:
        """Return originator peer messages addressed to a specific caller.

        Two-phase implementation (resolve peer thread ids, then fetch
        messages) instead of a JOIN — the state interface is single-namespace
        and does not express joins.

        Inboxes stay strictly isolated: codex_A never returns codex_B's
        messages. **REL-08 read-side (Fork-1a):** the visible thread set is the
        UNION of two disjuncts — (i) threads keyed to the caller's CURRENT
        ``recipient_agent_instance_id`` (legacy NULL-session rows + this session's
        own), and (ii) threads carrying the caller's STABLE
        ``recipient_agent_session_id`` — so a thread whose recipient instance
        rotated on reconnect stays visible under the successor. ``query_state``
        has no OR, so two single-namespace reads + a thread-id dedup merge. This
        is READ-ONLY: the thread rows are never re-keyed, so
        ``recipient_agent_instance_id`` stays the ``find_peer_thread`` dedup key
        (the collision from a write-side re-home stays shut). An empty session id
        skips disjunct (ii) — pure legacy instance-only visibility, no regression.

        ``silent_only=True`` restricts results to messages the sender did NOT
        mark IMPORTANT. Public ``peer_inbox`` now passes ``silent_only=False``
        by default so the inbox works as a durable catch-up view; callers that
        want intentional silent-only status checks opt into this filter.
        """
        capped = max(1, min(limit, _MAX_LIST_LIMIT))
        lookahead_limit = capped + 1
        # R3a (SQL-lockdown) + REL-08 UNION: two single-namespace 2-eq reads (no
        # OR in query_state), merged + deduped on thread id. Disjunct (i) = the
        # caller's current instance (legacy NULL-session rows + own); (ii) = the
        # caller's stable session id (survives instance rotation). Downstream
        # orders by (created_at, id), so the thread-id order here is immaterial.
        target = f"peer:{recipient_agent_id}"
        thread_ids: list[str] = [
            str(r["id"])
            for r in _records(
                self._state.query_state(
                    _NAMESPACE,
                    {
                        "table": TABLE_AGENT_THREAD,
                        "filters": {
                            "target_backend": target,
                            "recipient_agent_instance_id": recipient_agent_instance_id,
                        },
                    },
                ),
            )
        ]
        if recipient_agent_session_id:
            thread_ids.extend(
                str(r["id"])
                for r in _records(
                    self._state.query_state(
                        _NAMESPACE,
                        {
                            "table": TABLE_AGENT_THREAD,
                            "filters": {
                                "target_backend": target,
                                "recipient_agent_session_id": recipient_agent_session_id,
                            },
                        },
                    ),
                )
            )
        thread_ids = list(dict.fromkeys(thread_ids))  # dedup, order-preserving
        if not thread_ids:
            return [], True

        # R3b (SQL-lockdown): per-thread query_ordered + Python k-way merge.
        # SCALAR filters per thread, NOT a `thread_id = ANY(...)` list filter:
        # = ANY lives only in the postgres provider, so a list filter is
        # unfaithful to the in-memory query_ordered backend (Option A; see
        # playbook §9). Peer-thread count per instance is ~1. limit=capped
        # (<=100) per thread is k-way-merge-sufficient (any global top-capped
        # row is in its own thread's top-capped). The `important` column (GAP-2)
        # replaces the raw metadata->>'important' predicate. Both inbox
        # sections are newest-first: a cursor-less read must surface pending
        # work rather than strand it behind a historical prefix. The
        # timestamp-only direct cursor deliberately retains its existing
        # duplicate-``created_at`` limitation; the empty-id sentinel skips the
        # equal-timestamp group while walking backward.
        # query_ordered implicitly excludes is_deleted=1 (the raw SQL did not) —
        # a no-op today (core__agent_message has no soft-delete write path,
        # grep-confirmed), matching the R2 thread-read reconciliation. STILL a
        # no-op after GAU-06 (2026-08-19) added the ONLY delete path this table
        # has: rotation self-notice retention hard-deletes (soft_delete=False),
        # so it removes rows outright rather than parking them behind the flag
        # this filter would have to skip. See the table's own description in
        # schema.py for the scope of that exception. The
        # migration smoke pins this exclusion so a future message-soft-delete
        # path can't silently drop peer-inbox rows undetected.
        filters: dict[str, object] = {"role": MessageRole.ORIGINATOR.value}
        if silent_only:
            filters["important"] = False
        after: list[object] | None = (
            [after_created_at, ""]
            if after_created_at is not None
            else None
        )
        merged: list[AgentMessageRow] = []
        for thread_id in thread_ids:
            query: dict[str, object] = {
                "table": TABLE_AGENT_MESSAGE,
                "filters": {"thread_id": thread_id, **filters},
                "order_by": [["created_at", "desc"], ["id", "desc"]],
                # Fetch one extra row so the service can expose honest
                # exhaustion while preserving its newest-first, backward
                # timestamp cursor. ``query_ordered`` caps ordinary pages at
                # 100, so the 101-row seam requires this explicit consent;
                # the public result remains capped below.
                "limit": lookahead_limit,
                "unbounded": True,
            }
            if after is not None:
                query["after"] = after
            merged.extend(
                _row_to_message(r)
                for r in _records(self._state.query_ordered(_NAMESPACE, query))
            )
        merged.sort(key=lambda m: (m.created_at, m.id), reverse=True)
        return merged[:capped], len(merged) <= capped

    # ------------------------------------------------------------------
    # Thread writes
    # ------------------------------------------------------------------

    def create_thread(
        self,
        *,
        originator_type: OriginatorType,
        target_backend: str,
        target_plugin_name: str,
        status: ThreadStatus,
        originator_id: str | None = None,
        originator_session_id: str | None = None,
        originator_bridge_id: str | None = None,
        title: str | None = None,
        working_directory: str | None = None,
        metadata: dict[str, object] | None = None,
        recipient_agent_instance_id: str | None = None,
        recipient_agent_session_id: str | None = None,
        originator_session_label: str | None = None,
        originator_agent_instance_id: str | None = None,
        recipient_session_label: str | None = None,
    ) -> AgentThreadRow:
        """Create a thread row; snapshot the per-peer label fields ONCE here.

        Per 2026-05-31 Architect ruling §2: ``originator_session_label`` /
        ``originator_agent_instance_id`` / ``recipient_session_label`` are
        snapshotted at creation time and NEVER updated by ``append_message``
        or any later mutation. A live ``/rename`` on a session with an open
        peer thread does NOT propagate to this row.
        """
        thread_id = _new_id(ID_PREFIX_THREAD)
        # W1 (SQL-lockdown): autocommit write_state. metadata is passed as a
        # Python dict — the write layer serializes it to JSONB (no caller
        # json.dumps / ::jsonb cast). created_at/updated_at are OMITTED: the
        # schema renders them DEFAULT (NOW() AT TIME ZONE 'UTC'), statement-
        # stable so created_at == updated_at exactly as the raw INSERT produced,
        # and leaning on the DB default dodges the F1 tz-offset hazard of binding
        # an aware-ISO datetime through the autocommit path. (Diverges from the
        # ledger writes, which supply them for an injected-clock determinism this
        # path does not need — the row is re-read below.)
        _require_success(
            self._state.write_state(
                _NAMESPACE,
                {
                    "table": TABLE_AGENT_THREAD,
                    "record": {
                        "id": thread_id,
                        "namespace": _NAMESPACE,
                        "originator_type": str(originator_type),
                        "originator_id": originator_id,
                        "originator_session_id": originator_session_id,
                        "originator_bridge_id": originator_bridge_id,
                        "target_backend": target_backend,
                        "target_plugin_name": target_plugin_name,
                        "title": title,
                        "working_directory": working_directory,
                        "status": str(status),
                        "last_message_cursor": 0,
                        "metadata": metadata or {},
                        "recipient_agent_instance_id": recipient_agent_instance_id,
                        "recipient_agent_session_id": recipient_agent_session_id,
                        "originator_session_label": originator_session_label,
                        "originator_agent_instance_id": originator_agent_instance_id,
                        "recipient_session_label": recipient_session_label,
                    },
                },
            ),
            "create_thread",
        )
        fetched = self.get_thread(thread_id)
        if fetched is None:
            raise RepositoryError(
                f"create_thread: row {thread_id} not visible after insert",
            )
        return fetched

    def update_thread(
        self,
        thread_id: str,
        update: ThreadStatusUpdate,
    ) -> AgentThreadRow:
        """Apply a status/active-action mutation; return the updated row."""
        # W2 (SQL-lockdown): autocommit update_state off the shared dict-builder.
        # An empty update (all-None) skips the write — the raw path's
        # no-assignments early-return; the get_thread re-read is the existence
        # gate (a 0-row update on a missing thread surfaces as not-found, exactly
        # as the raw txn path did).
        updates = self._build_thread_updates(update)
        if updates:
            _require_success(
                self._state.update_state(
                    _NAMESPACE,
                    {"table": TABLE_AGENT_THREAD, "filters": {"id": thread_id}},
                    updates,
                ),
                "update_thread",
            )
        fetched = self.get_thread(thread_id)
        if fetched is None:
            raise RepositoryError(
                f"update_thread: thread {thread_id} not found",
            )
        return fetched

    def conditional_update_thread(
        self,
        thread_id: str,
        update: ThreadStatusUpdate,
        *,
        require_status_in: Sequence[ThreadStatus],
    ) -> AgentThreadRow:
        """Apply ``update`` only if the thread's current status is allowed.

        W3 (SQL-lockdown): a single atomic ``update_state`` compare-and-set
        fuses the status gate into the WHERE (``id`` AND ``status = ANY(allowed)``),
        replacing the ``SELECT … FOR UPDATE`` + apply. The one statement is
        atomic exactly where the lock-then-apply was, so a concurrent transition
        (e.g. OPEN -> QUEUED) cannot slip between check and write — required for
        ``close_thread`` to refuse closing a thread that raced into
        QUEUED/RUNNING. A 0-row result means the precondition was unmet at CAS
        time (status not allowed) OR the thread is gone: the conditional did NOT
        apply either way, so we re-read only to raise the precise cause (a status
        that flips back to allowed after the miss is irrelevant — it still did
        not apply).
        """
        updates = self._build_thread_updates(update)
        if not updates:
            raise RepositoryError(
                "conditional_update_thread requires a non-empty update",
            )
        affected = _rows_affected(
            self._state.update_state(
                _NAMESPACE,
                {
                    "table": TABLE_AGENT_THREAD,
                    "filters": {
                        "id": thread_id,
                        "status": [str(s) for s in require_status_in],
                    },
                },
                updates,
            ),
            "conditional_update_thread",
        )
        if affected == 0:
            observed = self._current_status_or_none(thread_id)
            if observed is None:
                raise RepositoryError(f"thread {thread_id} not found")
            raise RepositoryError(
                f"thread {thread_id} precondition not met (status "
                f"{observed.value!r}; required one of "
                f"{[s.value for s in require_status_in]}); not applied",
            )
        fetched = self.get_thread(thread_id)
        if fetched is None:
            raise RepositoryError(
                f"conditional_update_thread: thread {thread_id} not found",
            )
        return fetched

    # ------------------------------------------------------------------
    # Atomic cursor allocation + message append
    # ------------------------------------------------------------------

    def append_message(
        self,
        *,
        thread_id: str,
        message: NewMessage,
        require_status_in: Sequence[ThreadStatus] | None = None,
        update: ThreadStatusUpdate | None = None,
    ) -> AgentMessageRow:
        """Allocate a cursor, insert the message, and apply optional updates.

        W4 (SQL-lockdown): all steps run in one transaction over the typed-txn
        ops. The optional ``require_status_in`` gate is FUSED into the cursor
        allocation's ``increment_and_return`` WHERE (``status = ANY``), so the
        status check + the increment + the row lock are one atomic statement —
        callers can rely on the appended message being consistent with the
        observed status, and a disallowed status consumes no cursor.
        """
        now = self._clock()
        message_id = _new_id(ID_PREFIX_MESSAGE)

        with self._state.transactional() as txn:
            cursor = self._allocate_cursor(txn, thread_id, require_status_in)
            self._insert_message(
                txn,
                message_id=message_id,
                thread_id=thread_id,
                cursor=cursor,
                message=message,
                created_at=now,
            )
            if update is not None:
                thread_updates = self._build_thread_updates(update)
                if thread_updates:
                    txn.update_state(
                        _NAMESPACE,
                        {"table": TABLE_AGENT_THREAD, "filters": {"id": thread_id}},
                        thread_updates,
                    )

        return AgentMessageRow(
            id=message_id,
            thread_id=thread_id,
            cursor=cursor,
            role=message.role,
            kind=message.kind,
            content=message.content,
            created_at=now,
            action_id=message.action_id,
            backend_session_id=message.backend_session_id,
            error=message.error,
            artifacts=message.artifacts,
            metadata=dict(message.metadata),
        )

    def merge_message_metadata(
        self, message_id: str, patch: dict[str, object],
    ) -> None:
        """Shallow-merge ``patch`` into ``core__agent_message.metadata``.

        Used by ``execute_turn`` to stamp the assembled prompt onto
        the originator message after assembly so it appears in
        ``agent_messages`` output (workbench doc §13).  Postgres'
        ``jsonb_set`` would lose any keys not in the patch; this
        helper does the read-modify-write under a transaction so
        existing keys (e.g. ``timeout_seconds``) are preserved.

        W5 (SQL-lockdown): the typed-txn ops replace the raw SELECT/UPDATE — the
        merged dict serializes to JSONB without a caller ``::jsonb`` cast. The
        read is non-locking, matching the original (its ``fetch_one`` had no
        ``FOR UPDATE``); the single transaction still makes the merge atomic.
        """
        if not patch:
            return
        with self._state.transactional() as txn:
            rows = txn.query_state(
                _NAMESPACE,
                {"table": TABLE_AGENT_MESSAGE, "filters": {"id": message_id}},
            )
            if not rows:
                raise RepositoryError(
                    f"merge_message_metadata: message {message_id} not found",
                )
            current = _coerce_json_dict(rows[0].get("metadata"))
            current.update(patch)
            txn.update_state(
                _NAMESPACE,
                {"table": TABLE_AGENT_MESSAGE, "filters": {"id": message_id}},
                {"metadata": current},
            )

    # ------------------------------------------------------------------
    # Message reads
    # ------------------------------------------------------------------

    def list_messages(
        self,
        thread_id: str,
        after_cursor: int,
        limit: int,
    ) -> list[AgentMessageRow]:
        capped = max(1, min(limit, _MAX_LIST_LIMIT))
        # Phase-0 migration (SQL-lockdown): cursor-paginated read via
        # query_ordered with a HEX-AWARE `after` sentinel `{ID_PREFIX_MESSAGE}_g`
        # ('g' > any uuid-hex char) preserving strict `cursor > after_cursor`
        # collation-independently (cursor unique per thread). See playbook §8.
        result = self._state.query_ordered(
            _NAMESPACE,
            {
                "table": TABLE_AGENT_MESSAGE,
                "filters": {"thread_id": thread_id},
                "order_by": [["cursor", "asc"], ["id", "asc"]],
                "after": [after_cursor, f"{ID_PREFIX_MESSAGE}_g"],
                "limit": capped,
            },
        )
        records = _records(result)
        return [_row_to_message(r) for r in records]

    def recent_messages(
        self, thread_id: str, *, limit: int,
    ) -> list[AgentMessageRow]:
        """Return the last ``limit`` messages, newest-first.

        Phase-0 migration (SQL-lockdown): uncapped ``query_state`` +
        Python numeric sort (the ledger Gap-C sidestep), NOT
        ``query_ordered`` — a caller requests ``limit=128`` and
        ``query_ordered`` caps at 100, which would silently truncate.
        cursor is per-thread-unique so the int-keyed DESC sort is
        total + collation-free; is_deleted exclusion is a no-op.
        See playbook §9(2).
        """
        result = self._state.query_state(
            _NAMESPACE,
            {"table": TABLE_AGENT_MESSAGE, "filters": {"thread_id": thread_id}},
        )
        records = _records(result)
        records.sort(key=lambda r: int(cast(int, r["cursor"])), reverse=True)
        return [_row_to_message(r) for r in records[: max(0, limit)]]

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _allocate_cursor(
        self,
        txn: StateTransaction,
        thread_id: str,
        require_status_in: Sequence[ThreadStatus] | None,
    ) -> int:
        """Atomically allocate the next cursor, fusing the optional status gate.

        ``increment_and_return`` compiles to ``UPDATE … SET last_message_cursor =
        last_message_cursor + 1 WHERE id = %s [AND status = ANY(%s)] RETURNING
        last_message_cursor``, taking a row lock held to commit. The ``=ANY``
        list filter folds ``require_status_in`` into the WHERE so the status
        check + the increment + the lock are ONE atomic statement — replacing the
        ``SELECT … FOR UPDATE`` + ``UPDATE … RETURNING`` pair. A 0-row match
        (thread missing, or status not allowed when gated) makes
        ``increment_and_return`` raise; we surface it as the ``RepositoryError``
        callers expect. On a status miss the WHERE excludes the row, so no cursor
        is consumed (no gap in the sequence). ``updated_at`` is trigger-maintained
        (``increment_and_return`` does not touch it).
        """
        filters: dict[str, object] = {"id": thread_id}
        if require_status_in is not None:
            filters["status"] = [str(s) for s in require_status_in]
        try:
            return txn.increment_and_return(
                _NAMESPACE,
                {
                    "table": TABLE_AGENT_THREAD,
                    "filters": filters,
                    "column": "last_message_cursor",
                    "by": 1,
                },
            )
        except RuntimeError as exc:
            if require_status_in is None:
                raise RepositoryError(
                    f"append_message: thread {thread_id} not found",
                ) from exc
            raise RepositoryError(
                f"append_message: thread {thread_id} not found or status not in "
                f"{[s.value for s in require_status_in]}",
            ) from exc

    def _insert_message(
        self,
        txn: StateTransaction,
        *,
        message_id: str,
        thread_id: str,
        cursor: int,
        message: NewMessage,
        created_at: datetime,
    ) -> None:
        # W4 (SQL-lockdown): typed-txn write_state. content / artifacts / error /
        # metadata pass as Python objects — the txn serializer renders them to
        # JSONB (no caller json.dumps / ::jsonb cast). `important` projects
        # metadata.important onto the first-class column (R3b's silent-bucket
        # filter). created_at is SUPPLIED (not DB-defaulted) because
        # append_message returns the row WITHOUT re-reading, so the returned
        # created_at must match what is stored (the txn serializer normalizes it
        # to naive UTC); updated_at is left to the DB default.
        txn.write_state(
            _NAMESPACE,
            {
                "table": TABLE_AGENT_MESSAGE,
                "record": {
                    "id": message_id,
                    "namespace": _NAMESPACE,
                    "thread_id": thread_id,
                    "cursor": cursor,
                    "role": str(message.role),
                    "kind": str(message.kind),
                    "content": [_part_to_json(p) for p in message.content],
                    "action_id": message.action_id,
                    "backend_session_id": message.backend_session_id,
                    "error": message.error,
                    "artifacts": [_artifact_to_json(a) for a in message.artifacts],
                    "metadata": dict(message.metadata),
                    "important": bool(message.metadata.get("important", False)),
                    "created_at": created_at,
                },
            },
        )

    def _current_status_or_none(self, thread_id: str) -> ThreadStatus | None:
        """Read a thread's current status; ``None`` if the row is absent.

        Used only to disambiguate a 0-row conditional CAS (status-not-allowed vs
        not-found) for the error message — informational, so non-locking.
        """
        records = _records(
            self._state.query_state(
                _NAMESPACE,
                {"table": TABLE_AGENT_THREAD, "filters": {"id": thread_id}},
            ),
        )
        if not records:
            return None
        return ThreadStatus(str(records[0]["status"]))

    def _build_thread_updates(
        self, update: ThreadStatusUpdate,
    ) -> dict[str, object]:
        """Project a ThreadStatusUpdate to an ``update_state`` column dict.

        The single column-dict builder for thread mutations, shared by
        ``update_thread`` (autocommit ``update_state``), ``conditional_update_thread``
        (the ``status = ANY`` CAS), and ``append_message`` (typed-txn
        ``update_state``). ``updated_at`` is deliberately absent — the
        BEFORE-UPDATE trigger maintains it; ``closed_at`` is normalized to naive
        UTC (the F1 write seam) because the autocommit value serializer keeps an
        aware datetime's offset rather than normalizing it.
        """
        updates: dict[str, object] = {}
        if update.status is not None:
            updates["status"] = str(update.status)
        if update.active_action_id is not None:
            updates["active_action_id"] = update.active_action_id
        if update.active_flow_id is not None:
            updates["active_flow_id"] = update.active_flow_id
        if update.backend_session_id is not None:
            updates["backend_session_id"] = update.backend_session_id
        if update.clear_active_action:
            updates["active_action_id"] = None
            updates["active_flow_id"] = None
        if update.set_closed_at:
            updates["closed_at"] = _naive_utc(self._clock())
        return updates



# ---------------------------------------------------------------------------
# Row marshalling
# ---------------------------------------------------------------------------


def _require_success(action_result: object, op: str) -> dict[str, object]:
    """Assert a state-interface ActionResult succeeded; return its ``data`` dict.

    Shared fail-loud gate for every migrated read AND write. A state op does NOT
    raise on a provider error — it returns a non-success envelope — so swallowing
    that as empty/zero is a silent-data-loss bug class. Raises ``RepositoryError``
    on any non-success status; returns the ``data`` dict (empty when absent) for
    callers that read records/result off it.
    """
    if not isinstance(action_result, dict):
        raise RepositoryError(f"unexpected state-service return: {action_result!r}")
    if action_result.get("action_status") not in {"completed", "success", None}:
        raise RepositoryError(
            f"state-service {op} failed: {action_result.get('error_message')}",
        )
    data = action_result.get("data")
    return data if isinstance(data, dict) else {}


def _rows_affected(action_result: object, op: str) -> int:
    """Rows-affected from a migrated ``update_state`` — the compare-and-set signal.

    ``data.result.updated`` (NESTED under ``result``). RAISES on an absent /
    non-int / bool value — a completed-but-empty envelope must NOT coerce to 0,
    which would read a failed write as a legit CAS miss; a real int 0 IS a legit
    miss (the precondition was unmet, as the conditional CAS relies on).
    """
    data = _require_success(action_result, op)
    inner = data.get("result")
    if not isinstance(inner, dict):
        raise RepositoryError(
            f"state-service {op}: result envelope missing 'result' dict",
        )
    updated = inner.get("updated")
    if isinstance(updated, bool) or not isinstance(updated, int):
        raise RepositoryError(
            f"state-service {op}: 'updated' is not an int: {updated!r}",
        )
    return updated


def _naive_utc(value: datetime) -> datetime:
    """Normalize a (possibly tz-aware) datetime to naive UTC (the F1 write seam).

    The autocommit value serializer ISO-formats a datetime KEEPING any offset; a
    TIMESTAMP (no-tz) column then applies the session tz to that offset. Binding
    naive UTC at the write boundary makes the stored wall-clock UTC regardless of
    session tz (parallels the raw txn path's ``_strip_tz_from_params``).
    """
    return value.astimezone(UTC).replace(tzinfo=None)


def _records(action_result: object) -> list[dict[str, object]]:
    """Normalize a state-interface ActionResult to a list of dict rows.

    The migrated reads (``query_state`` / ``query_ordered``) return
    ``data.records`` as dicts keyed by column name (the postgres provider
    uses psycopg ``dict_row``), so no positional column zip is needed.
    """
    rows = _require_success(action_result, "query").get("records", [])
    if not isinstance(rows, list):
        return []
    return [cast(dict[str, object], row) for row in rows if isinstance(row, dict)]


def _row_to_thread(row: Mapping[str, object]) -> AgentThreadRow:
    metadata = _coerce_json_dict(row.get("metadata"))
    return AgentThreadRow(
        id=str(row["id"]),
        originator_type=OriginatorType(str(row["originator_type"])),
        target_backend=str(row["target_backend"]),
        target_plugin_name=str(row["target_plugin_name"]),
        status=ThreadStatus(str(row["status"])),
        created_at=_coerce_datetime(row["created_at"]),
        updated_at=_coerce_datetime(row["updated_at"]),
        last_message_cursor=int(cast(int, row["last_message_cursor"])),
        originator_id=_optional_str(row.get("originator_id")),
        originator_session_id=_optional_str(row.get("originator_session_id")),
        originator_bridge_id=_optional_str(row.get("originator_bridge_id")),
        title=_optional_str(row.get("title")),
        working_directory=_optional_str(row.get("working_directory")),
        backend_session_id=_optional_str(row.get("backend_session_id")),
        active_action_id=_optional_str(row.get("active_action_id")),
        active_flow_id=_optional_str(row.get("active_flow_id")),
        closed_at=_coerce_optional_datetime(row.get("closed_at")),
        metadata=metadata,
        recipient_agent_instance_id=_optional_str(
            row.get("recipient_agent_instance_id"),
        ),
        originator_session_label=_optional_str(
            row.get("originator_session_label"),
        ),
        originator_agent_instance_id=_optional_str(
            row.get("originator_agent_instance_id"),
        ),
        recipient_session_label=_optional_str(
            row.get("recipient_session_label"),
        ),
    )


def _row_to_message(row: Mapping[str, object]) -> AgentMessageRow:
    content_raw = _coerce_json_list(row.get("content"))
    artifacts_raw = _coerce_json_list(row.get("artifacts"))
    error_raw = row.get("error")
    error: dict[str, object] | None = (
        _coerce_json_dict(error_raw) if error_raw is not None else None
    )
    metadata = _coerce_json_dict(row.get("metadata"))
    return AgentMessageRow(
        id=str(row["id"]),
        thread_id=str(row["thread_id"]),
        cursor=int(cast(int, row["cursor"])),
        role=MessageRole(str(row["role"])),
        kind=MessageKind(str(row["kind"])),
        content=[_part_from_json(p) for p in content_raw],
        created_at=_coerce_datetime(row["created_at"]),
        action_id=_optional_str(row.get("action_id")),
        backend_session_id=_optional_str(row.get("backend_session_id")),
        error=error,
        artifacts=tuple(_artifact_from_json(a) for a in artifacts_raw),
        metadata=metadata,
    )


def _part_to_json(part: TextPart) -> dict[str, object]:
    return {"type": part.type, "text": part.text}


def _part_from_json(part: object) -> TextPart:
    if not isinstance(part, dict):
        raise RepositoryError(f"unexpected message part: {part!r}")
    return TextPart(type=str(part.get("type", "text")), text=str(part.get("text", "")))


def _artifact_to_json(artifact: ArtifactRef) -> dict[str, object]:
    payload: dict[str, object] = {"blob_id": artifact.blob_id}
    if artifact.filename is not None:
        payload["filename"] = artifact.filename
    if artifact.mime_type is not None:
        payload["mime_type"] = artifact.mime_type
    if artifact.size_bytes is not None:
        payload["size_bytes"] = artifact.size_bytes
    if artifact.metadata:
        payload["metadata"] = artifact.metadata
    return payload


def _artifact_from_json(artifact: object) -> ArtifactRef:
    if not isinstance(artifact, dict):
        raise RepositoryError(f"unexpected artifact: {artifact!r}")
    metadata_raw = artifact.get("metadata", {})
    metadata = (
        cast(dict[str, object], metadata_raw)
        if isinstance(metadata_raw, dict)
        else {}
    )
    return ArtifactRef(
        blob_id=str(artifact["blob_id"]),
        filename=_optional_str(artifact.get("filename")),
        mime_type=_optional_str(artifact.get("mime_type")),
        size_bytes=_optional_int(artifact.get("size_bytes")),
        metadata=metadata,
    )


def _coerce_json_dict(value: object) -> dict[str, object]:
    if value is None:
        return {}
    if isinstance(value, dict):
        return cast(dict[str, object], value)
    if isinstance(value, str):
        loaded = json.loads(value)
        if isinstance(loaded, dict):
            return cast(dict[str, object], loaded)
    raise RepositoryError(f"expected JSON dict, got {value!r}")


def _coerce_json_list(value: object) -> list[object]:
    if value is None:
        return []
    if isinstance(value, list):
        return cast(list[object], value)
    if isinstance(value, str):
        loaded = json.loads(value)
        if isinstance(loaded, list):
            return cast(list[object], loaded)
    raise RepositoryError(f"expected JSON list, got {value!r}")


def _coerce_datetime(value: object) -> datetime:
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        return datetime.fromisoformat(value)
    raise RepositoryError(f"unexpected datetime value: {value!r}")


def _coerce_optional_datetime(value: object) -> datetime | None:
    return None if value is None else _coerce_datetime(value)


def _optional_str(value: object) -> str | None:
    return None if value is None else str(value)


def _optional_int(value: object) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        return int(value)
    raise RepositoryError(f"unexpected int value: {value!r}")


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex}"


__all__ = [
    "AgentMessagingRepository",
    "NewMessage",
    "RepositoryError",
    "ThreadStatusUpdate",
]
