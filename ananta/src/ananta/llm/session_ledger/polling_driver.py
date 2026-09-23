"""Polling-driver domain mixin for the session-ledger repository.

W5.O cycle 5: lease/cursor/batch primitives that back the polling loop, plus
``LeaseLostError`` and ``PollingLeaseHandle`` type declarations (§3.11.2 C8).

SQL-lockdown (Slice 2): all 15 txn writes + 3 reads are off raw SQL onto the
``StateManagementInterface`` primitives. Single-statement writes ride the
AUTOCOMMIT seams (``_write`` / ``_update`` / ``_delete`` / ``_query`` /
``_query_ordered``); the lease-acquire disjunctive CAS rides the dedicated
``_acquire_lease`` primitive (the ``(lease IS NULL OR lease < now)`` predicate
the flat equality grammar cannot express); the two read-then-write upserts
(``write_cursor`` / ``record_lease_ping``) stay in a real ``transactional()``
(typed-txn ``query_state`` + ``write_state`` / ``update_state``) because their
conflict key is nullable (``scope_key``) or must preserve an immutable
``created_at`` — ``upsert_state`` DO-UPDATE would clobber it.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from ananta.llm.session_ledger.base import (
    LedgerRepositoryError,
    SessionLedgerRepositoryBase,
)
from ananta.llm.session_ledger.schema import (
    ID_PREFIX_ACTIVE_LEASE,
    ID_PREFIX_IMPORT_BATCH,
    ID_PREFIX_SOURCE_CURSOR,
    NAMESPACE,
    TABLE_ACTIVE_LEASE,
    TABLE_IMPORT_BATCH,
    TABLE_SOURCE,
    TABLE_SOURCE_CURSOR,
)
from ananta.llm.session_ledger.shared import (
    _as_aware_utc,
    _coerce_json_dict,
    _new_id,
    _strip_nuls,
)
from ananta.llm.session_ledger.types import (
    CursorScope,
    ImportBatchStatus,
)

RELOAD_SAFE = True

# The curated public column set ``get_import_status`` projects to. ``query_state``
# returns ``SELECT *``, but this row is surfaced verbatim by the operator/MCP
# ``get_import_status`` verb, so it must NOT leak the internal
# ``polling_lease_token`` fence (the pre-migration SELECT deliberately omitted it)
# or the bookkeeping columns (namespace / is_deleted / created_at / updated_at).
_IMPORT_STATUS_PUBLIC_COLUMNS = (
    "id",
    "source_id",
    "started_at",
    "finished_at",
    "status",
    "event_count",
    "error_message",
    "error_kind",
)


class LeaseLostError(RuntimeError):
    """Raised when the polling-lease heartbeat finds the lease no longer owned.

    Surfaces from :class:`LeaseHeartbeat`'s ``check`` when
    :meth:`SessionLedgerRepository.refresh_polling_lease` returns ``None`` —
    i.e. a successor poller has acquired the source's lease and the current
    walker's token no longer matches. The importer's ``_poll_one_pulling_source``
    exception handler catches this, marks the in-flight batch failed with
    ``error_kind='lease_lost'``, and releases the (now-stale) lease in
    a ``finally`` block (silent no-op).
    """


@dataclass(frozen=True, slots=True)
class PollingLeaseHandle:
    """Per-acquisition handle returned by ``try_acquire_polling_lease``."""

    source_id: str
    lease_token: str
    lease_until: datetime


class SessionLedgerPollingDriverMixin(SessionLedgerRepositoryBase):
    """Polling-driver domain mixin: lease + cursor + batch primitives."""

    __slots__ = ()

    # ------------------------------------------------------------------
    # Source cursors
    # ------------------------------------------------------------------

    def read_cursor(
        self,
        *,
        source_id: str,
        scope: CursorScope,
        scope_key: str | None = None,
    ) -> dict[str, Any] | None:
        if scope is CursorScope.DISCOVERY:
            filters: dict[str, object] = {
                "source_id": source_id,
                "cursor_scope": scope.value,
                "scope_key": {"op": "is_null"},
                "is_deleted": 0,
            }
        else:
            if scope_key is None:
                raise LedgerRepositoryError(
                    "event_read cursor requires non-null scope_key (external_session_id)",
                )
            filters = {
                "source_id": source_id,
                "cursor_scope": scope.value,
                "scope_key": scope_key,
                "is_deleted": 0,
            }
        rows = self._query(TABLE_SOURCE_CURSOR, filters)
        if not rows:
            return None
        return _coerce_json_dict(rows[0].get("cursor_payload"))

    def write_cursor(
        self,
        *,
        source_id: str,
        scope: CursorScope,
        cursor_payload: dict[str, Any],
        scope_key: str | None = None,
    ) -> None:
        if scope is CursorScope.EVENT_READ and scope_key is None:
            raise LedgerRepositoryError(
                "event_read cursor requires non-null scope_key",
            )
        now = self._clock()
        # ``scope_key`` is the nullable half of the cursor identity, so the
        # existence check matches the original (no is_deleted filter — a
        # soft-deleted cursor is revived via the update's ``is_deleted = 0``).
        # ``cursor_payload`` is passed as a native dict → JSONB (no caller
        # ``json.dumps`` / ``::jsonb`` cast). Kept transactional because the
        # nullable conflict key rules out ``upsert_state``.
        identity_filters: dict[str, object] = {
            "source_id": source_id,
            "cursor_scope": scope.value,
            "scope_key": {"op": "is_null"} if scope_key is None else scope_key,
        }
        with self._state.transactional() as txn:
            existing = txn.query_state(
                NAMESPACE,
                {"table": TABLE_SOURCE_CURSOR, "filters": identity_filters},
            )
            if not existing:
                txn.write_state(
                    NAMESPACE,
                    {
                        "table": TABLE_SOURCE_CURSOR,
                        "record": {
                            "id": _new_id(ID_PREFIX_SOURCE_CURSOR),
                            "namespace": NAMESPACE,
                            "source_id": source_id,
                            "cursor_scope": scope.value,
                            "cursor_payload": cursor_payload,
                            "scope_key": scope_key,
                            "created_at": now,
                            "updated_at": now,
                        },
                    },
                )
            else:
                txn.update_state(
                    NAMESPACE,
                    {
                        "table": TABLE_SOURCE_CURSOR,
                        "filters": {"id": str(existing[0]["id"])},
                    },
                    {"cursor_payload": cursor_payload, "is_deleted": 0, "updated_at": now},
                )

    # ------------------------------------------------------------------
    # Import batches
    # ------------------------------------------------------------------

    def start_batch(
        self,
        source_id: str,
        *,
        polling_lease_token: str | None,
    ) -> str:
        """Create a new ``running`` ``__import_batch`` row for the source."""
        batch_id = _new_id(ID_PREFIX_IMPORT_BATCH)
        now = self._clock()
        self._write(
            TABLE_IMPORT_BATCH,
            {
                "id": batch_id,
                "namespace": NAMESPACE,
                "source_id": source_id,
                "started_at": now,
                "status": ImportBatchStatus.RUNNING.value,
                "event_count": 0,
                "polling_lease_token": polling_lease_token,
                "created_at": now,
                "updated_at": now,
            },
        )
        return batch_id

    def ensure_open_route_batch_for_source(self, source_id: str) -> str:
        """Return the source's open ROUTE batch id, creating one if none exists.

        A route batch carries ``polling_lease_token IS NULL`` (no importer lease
        holds it — adoptable by any poll pass). Idempotent at the registration
        seam (A1) so re-registering an export reuses the running route batch
        instead of orphaning a fresh one each call. Single-writer-local safe in
        1A (SELECT-then-INSERT); 1B deploy-2 hardens this with
        ``INSERT … ON CONFLICT`` once the one-open-route-per-source index exists.
        """
        rows = self._query_ordered(
            TABLE_IMPORT_BATCH,
            filters={
                "source_id": source_id,
                "status": ImportBatchStatus.RUNNING.value,
                "polling_lease_token": {"op": "is_null"},
            },
            order_by=[["created_at", "asc"], ["id", "asc"]],
            limit=1,
        )
        if rows:
            return str(rows[0]["id"])
        return self.start_batch(source_id, polling_lease_token=None)

    def finish_batch(
        self,
        batch_id: str,
        *,
        polling_lease_token: str,
        status: ImportBatchStatus,
        error_message: str | None = None,
        error_kind: str | None = None,
    ) -> bool:
        """Mark a batch terminal — ONLY if the caller's token still matches.

        The token + still-RUNNING + live equality guards ARE the compare-and-set:
        a stale owner's late finish matches 0 rows and is silently dropped
        (returns False) so handed-off batches cannot be clobbered.
        """
        now = self._clock()
        affected = self._update(
            TABLE_IMPORT_BATCH,
            {
                "id": batch_id,
                "polling_lease_token": polling_lease_token,
                "status": ImportBatchStatus.RUNNING.value,
                "is_deleted": 0,
            },
            {
                "status": status.value,
                "finished_at": now,
                "error_message": _strip_nuls(error_message),
                "error_kind": _strip_nuls(error_kind),
                "updated_at": now,
            },
        )
        return affected > 0

    def get_import_status(self, batch_id: str) -> dict[str, object] | None:
        """Return the public status projection for one batch, or None.

        Projects the ``query_state`` ``SELECT *`` row down to
        ``_IMPORT_STATUS_PUBLIC_COLUMNS`` so the operator/MCP-facing status verb
        keeps its pre-migration shape and never leaks the internal
        ``polling_lease_token`` fence or bookkeeping columns.
        """
        rows = self._query(TABLE_IMPORT_BATCH, {"id": batch_id, "is_deleted": 0})
        if not rows:
            return None
        row = rows[0]
        return {col: row.get(col) for col in _IMPORT_STATUS_PUBLIC_COLUMNS}

    # ------------------------------------------------------------------
    # Polling lease
    # ------------------------------------------------------------------

    def try_acquire_polling_lease(
        self,
        source_id: str,
        *,
        ttl_seconds: int = 600,
    ) -> PollingLeaseHandle | None:
        """Atomic conditional CAS: claim the source's polling lease.

        Rides the ``_acquire_lease`` primitive — the disjunctive
        ``(polling_lease_until IS NULL OR polling_lease_until < now)``
        availability predicate the flat equality grammar cannot express. The
        single statement makes the row lock, the free-or-expired check, and the
        write atomic (no read-then-write TOCTOU). ``updated_at`` is maintained by
        the BEFORE-UPDATE trigger (omitted from ``set``, per the primitive).
        """
        now = self._clock()
        new_token = uuid.uuid4().hex
        new_until = now + timedelta(seconds=ttl_seconds)
        acquired = self._acquire_lease(
            TABLE_SOURCE,
            {"id": source_id, "is_deleted": 0},
            lease_column="polling_lease_until",
            now=now,
            set_values={
                "polling_lease_until": new_until,
                "polling_lease_token": new_token,
            },
        )
        if not acquired:
            return None
        return PollingLeaseHandle(
            source_id=source_id,
            lease_token=new_token,
            lease_until=new_until,
        )

    def refresh_polling_lease(
        self,
        handle: PollingLeaseHandle,
        *,
        ttl_seconds: int = 600,
    ) -> PollingLeaseHandle | None:
        """Extend the current lease window — only if the caller still owns it.

        Plain equality CAS (``id`` + matching ``polling_lease_token``):
        rows-affected 0 means a successor stole the lease → return None so the
        heartbeat raises ``LeaseLostError``.
        """
        now = self._clock()
        new_until = now + timedelta(seconds=ttl_seconds)
        affected = self._update(
            TABLE_SOURCE,
            {
                "id": handle.source_id,
                "is_deleted": 0,
                "polling_lease_token": handle.lease_token,
            },
            {"polling_lease_until": new_until, "updated_at": now},
        )
        if affected <= 0:
            return None
        return PollingLeaseHandle(
            source_id=handle.source_id,
            lease_token=handle.lease_token,
            lease_until=new_until,
        )

    def release_polling_lease(self, handle: PollingLeaseHandle) -> None:
        """Clear the lease — only if the caller still owns it (token-fenced CAS)."""
        now = self._clock()
        self._update(
            TABLE_SOURCE,
            {
                "id": handle.source_id,
                "is_deleted": 0,
                "polling_lease_token": handle.lease_token,
            },
            {
                "polling_lease_until": None,
                "polling_lease_token": None,
                "updated_at": now,
            },
        )

    def adopt_route_batch_for_source(
        self,
        source_id: str,
        *,
        polling_lease_token: str,
        recency_window_minutes: int = 10,
    ) -> str | None:
        """Find AND atomically claim a recent route-created batch.

        The original single-statement ``UPDATE … WHERE id = (SELECT … ORDER BY
        started_at DESC LIMIT 1) AND polling_lease_token IS NULL`` is decomposed
        into (1) a ``query_ordered`` for the MOST-RECENT adoptable batch +
        (2) an ``update_state`` CAS guarded on ``polling_lease_token IS NULL``.
        Equivalent: ordering DESC means the most-recent adoptable IS the most
        recent within the recency window (if it is older than the cutoff, all
        are, → None); the CAS's ``token IS NULL`` re-check reproduces the
        original outer guard exactly (rows-affected 0 = a racer claimed it
        first → None). The cutoff compare coerces both operands to aware-UTC
        (``started_at`` reads back naive from the timestamp column).
        """
        now = self._clock()
        cutoff = now - timedelta(minutes=recency_window_minutes)
        candidates = self._query_ordered(
            TABLE_IMPORT_BATCH,
            filters={
                "source_id": source_id,
                "status": ImportBatchStatus.RUNNING.value,
                "polling_lease_token": {"op": "is_null"},
            },
            order_by=[["started_at", "desc"], ["id", "desc"]],
            limit=1,
        )
        if not candidates:
            return None
        candidate = candidates[0]
        if _as_aware_utc(candidate["started_at"]) <= _as_aware_utc(cutoff):
            return None
        candidate_id = str(candidate["id"])
        affected = self._update(
            TABLE_IMPORT_BATCH,
            {
                "id": candidate_id,
                "polling_lease_token": {"op": "is_null"},
                "status": ImportBatchStatus.RUNNING.value,
                "is_deleted": 0,
            },
            {"polling_lease_token": polling_lease_token, "updated_at": now},
        )
        if affected <= 0:
            return None
        return candidate_id

    # ------------------------------------------------------------------
    # Active-lease ping + source-cursor diagnostics
    # ------------------------------------------------------------------

    def record_lease_ping(
        self,
        *,
        session_id: str,
        source_id: str,
        ttl_seconds: int,
    ) -> None:
        now = self._clock()
        expires_at = datetime.fromtimestamp(now.timestamp() + ttl_seconds, tz=now.tzinfo)
        # Kept transactional (read-then-write upsert): the conflict key is
        # ``session_id`` but ``upsert_state`` DO-UPDATE would clobber the
        # immutable ``created_at`` on the refresh branch, so the existence check
        # + branch is reproduced explicitly.
        with self._state.transactional() as txn:
            existing = txn.query_state(
                NAMESPACE,
                {
                    "table": TABLE_ACTIVE_LEASE,
                    "filters": {"session_id": session_id, "is_deleted": 0},
                },
            )
            if not existing:
                txn.write_state(
                    NAMESPACE,
                    {
                        "table": TABLE_ACTIVE_LEASE,
                        "record": {
                            "id": _new_id(ID_PREFIX_ACTIVE_LEASE),
                            "namespace": NAMESPACE,
                            "session_id": session_id,
                            "source_id": source_id,
                            "last_seen_at": now,
                            "expires_at": expires_at,
                            "lease_ttl_seconds": ttl_seconds,
                            "created_at": now,
                            "updated_at": now,
                        },
                    },
                )
            else:
                txn.update_state(
                    NAMESPACE,
                    {
                        "table": TABLE_ACTIVE_LEASE,
                        "filters": {"id": str(existing[0]["id"])},
                    },
                    {
                        "last_seen_at": now,
                        "expires_at": expires_at,
                        "lease_ttl_seconds": ttl_seconds,
                        "updated_at": now,
                    },
                )

    def count_active_source_cursors(self, source_id: str) -> int:
        """Return the active ``__source_cursor`` row count for one source.

        ``query_state`` is uncapped and returns ``SELECT *``; the count is
        ``len`` of the live rows (a rare diagnostic — pulling the rows to count
        them is an acceptable tradeoff for staying off raw ``count(*)`` SQL).
        """
        return len(self._query(TABLE_SOURCE_CURSOR, {"source_id": source_id, "is_deleted": 0}))

    def reset_source_cursor(self, source_id: str) -> int:
        """Hard-delete every active cursor row for the named source.

        Counts the live cursor rows first (``query_state`` + ``len``) and
        early-returns 0 when there are none (preserves the "no write on an empty
        source" behavior); otherwise HARD-deletes via ``_delete(soft=False)`` and
        returns the before-count. Hard (not soft) per the operator's
        soft-delete-is-opt-out principle -- a cursor reset has no recovery path,
        so the rows are removed outright rather than left as ``is_deleted = 1``
        ghosts. Safe: the re-creation path (:meth:`write_cursor`) inserts a fresh
        row when none exists, so it no longer needs a soft-deleted row to revive.
        """
        before = len(
            self._query(TABLE_SOURCE_CURSOR, {"source_id": source_id, "is_deleted": 0})
        )
        if before == 0:
            return 0
        self._delete(
            TABLE_SOURCE_CURSOR,
            {"source_id": source_id, "is_deleted": 0},
            soft=False,
        )
        return before


__all__ = [
    "LeaseLostError",
    "PollingLeaseHandle",
    "RELOAD_SAFE",
    "SessionLedgerPollingDriverMixin",
]
