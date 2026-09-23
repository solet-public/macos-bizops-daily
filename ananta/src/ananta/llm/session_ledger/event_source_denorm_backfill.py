"""Event source-denormalization backfill mixin for the session-ledger repository.

SQL-lockdown #0, Slice 7 companion. The Architect-ruled denormalization adds
``session_vendor`` + ``source_kind`` to ``__event`` so
``list_events_by_source_window`` reads a single table instead of a 3-table
JOIN (event → session → source). ``append_event`` writes both columns on every
NEW event, but rows that predate the migration carry NULLs — and the migrated
single-table read filters on those columns, so an un-backfilled historical
event would be silently dropped from every ``source_kind`` / ``vendor`` pull.
This one-shot operator backfill fills them from the authoritative join.

Design (deliberately NOT the heavy ``ExportBlobIdentityBackfill`` shape — no
phases, no confirm-gate, no orphan sweep):

* **idempotent + fill-only.** Each pass updates ONLY events whose
  ``session_vendor IS NULL``; a re-run is a no-op. It never overwrites a
  populated value, so — unlike a destructive backfill — it is inherently safe
  to run and needs no ``confirm`` gate.
* **fail-loud.** A session whose ``source_id`` resolves to no live ``__source``
  row (or whose ``vendor`` is NULL) raises ``LedgerRepositoryError``. The
  original INNER JOIN would have DROPPED those events; filling a NULL
  ``source_kind`` would wrongly leak them into vendor-only pulls, so an
  unresolvable session is a data anomaly that must STOP the backfill rather
  than be silently skipped. (Unreachable in practice — ``__source`` rows are
  never soft-deleted and ``vendor`` is NOT NULL — so this is a tripwire.)
* **bounded memory.** Driven by SESSIONS (far fewer than events), paged by an
  ``id`` keyset; each session is ONE predicated ``update_state`` over its own
  events (server-side, no row materialization), so the multi-million-row event
  corpus is never loaded.
"""

from __future__ import annotations

import logging
from datetime import datetime

from ananta.llm.session_ledger.base import (
    LedgerRepositoryError,
    SessionLedgerRepositoryBase,
)
from ananta.llm.session_ledger.schema import TABLE_EVENT, TABLE_SESSION, TABLE_SOURCE

RELOAD_SAFE = True

logger = logging.getLogger(__name__)

# Session keyset page size. Sessions are orders of magnitude fewer than events
# and each page is one ``query_ordered`` read; the ≤100 cap matches every other
# ledger paged read.
_BACKFILL_SESSION_PAGE = 100


class SessionLedgerEventSourceDenormBackfillMixin(SessionLedgerRepositoryBase):
    """Slice-7 event source-denormalization backfill mixin (see module docstring)."""

    __slots__ = ()

    def backfill_event_source_denormalization(self) -> dict[str, int]:
        """Fill ``__event.session_vendor`` + ``source_kind`` from session→source.

        Idempotent (touches only ``session_vendor IS NULL`` rows), fail-loud on
        an unresolvable source, and bounded (session-driven, id-keyset paged,
        one server-side ``update_state`` per session). Returns
        ``{"sessions_scanned", "events_denormalized"}`` — re-run to convergence
        is the completeness check (a clean run reports ``events_denormalized`` 0).
        """
        source_kind_by_id = self._source_kind_by_id()
        now = self._clock()
        sessions_scanned = 0
        events_denormalized = 0
        cursor = ""
        while True:
            page = self._query_ordered(
                TABLE_SESSION,
                filters={"id": {"op": "gt", "value": cursor}},
                order_by=[["id", "asc"], ["created_at", "asc"]],
                limit=_BACKFILL_SESSION_PAGE,
            )
            if not page:
                break
            for session in page:
                sessions_scanned += 1
                events_denormalized += self._denormalize_session_events(
                    session, source_kind_by_id=source_kind_by_id, now=now,
                )
            cursor = str(page[-1]["id"])
        logger.info(
            "backfill_event_source_denormalization: scanned %d session(s), "
            "denormalized %d event(s)",
            sessions_scanned, events_denormalized,
        )
        return {
            "sessions_scanned": sessions_scanned,
            "events_denormalized": events_denormalized,
        }

    def _source_kind_by_id(self) -> dict[str, str]:
        """Map every live source id → its ``source_kind`` (bounded — sources are few)."""
        rows = self._query(TABLE_SOURCE, {"is_deleted": 0})
        return {str(row["id"]): str(row["source_kind"]) for row in rows}

    def _denormalize_session_events(
        self,
        session: dict[str, object],
        *,
        source_kind_by_id: dict[str, str],
        now: datetime,
    ) -> int:
        """Fill one session's un-denormalized events; FAIL-LOUD on an unresolved source.

        One predicated ``update_state`` over the session's ``session_vendor
        IS NULL`` events; returns rows-affected. ``vendor`` is read from the
        session row (the same value ``append_event`` snapshots) and
        ``source_kind`` from the session's immutable ``source_id``.
        """
        session_id = str(session["id"])
        source_id = str(session["source_id"])
        source_kind = source_kind_by_id.get(source_id)
        if source_kind is None:
            raise LedgerRepositoryError(
                "backfill_event_source_denormalization: session "
                f"{session_id} references source {source_id!r} with no live "
                "__source row — cannot denormalize source_kind (data anomaly; "
                "the original INNER JOIN would have dropped these events, so a "
                "NULL fill would wrongly leak them into vendor-only pulls)",
            )
        vendor = session.get("vendor")
        if vendor is None:
            raise LedgerRepositoryError(
                "backfill_event_source_denormalization: session "
                f"{session_id} has a NULL vendor (NOT-NULL column; data anomaly)",
            )
        return self._update(
            TABLE_EVENT,
            {"session_id": session_id, "session_vendor": {"op": "is_null"}},
            {
                "session_vendor": str(vendor),
                "source_kind": source_kind,
                "updated_at": now,
            },
        )


__all__ = [
    "RELOAD_SAFE",
    "SessionLedgerEventSourceDenormBackfillMixin",
]
