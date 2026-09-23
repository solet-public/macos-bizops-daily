"""Session source-kind junction backfill mixin for the session-ledger repository.

SQL-lockdown list_sessions slice companion. The Architect-ruled junction
``session_source_kind(canonical_session_id, source_kind)`` backs the migrated
``list_sessions`` source_kind filter (read-then-route). ``upsert_session``
maintains it on the ingest attach-path for every NEW session, but the junction
table starts EMPTY, so rows that predate the migration have no junction entries
— and ``list_sessions(source_kind=K)`` would return ``[]`` for every historical
session until backfilled. This one-shot operator backfill populates it from the
authoritative session→source data.

Design (mirrors ``event_source_denorm_backfill`` — no phases, no confirm-gate):

* **idempotent + additive.** Each ``(canonical_session_id, source_kind)`` is an
  ``upsert_state`` DO-NOTHING on the UNIQUE pair; a re-run inserts nothing new.
  Non-destructive, so — unlike a destructive backfill — it needs no ``confirm``
  gate, and it is race-free with ongoing ingest (new sessions self-populate via
  the attach-path, and DO-NOTHING absorbs any overlap).
* **fail-loud.** A session whose ``(vendor, external_session_id)`` group has no
  canonical row, or whose ``source_id`` resolves to no live ``__source``,
  raises ``LedgerRepositoryError``. A group with no canonical is a duplicate-
  canonical / orphaned-canonical anomaly that ``lift_canonical_pointer_for_
  duplicate_sessions`` must resolve FIRST; silently skipping would leave the
  group unfindable by source_kind.
* **bounded memory.** The source-kind map + the canonical-id-by-group map are
  bounded (sources are few; one entry per canonical session). The full session
  scan is ``id``-keyset paged. Siblings share their canonical's
  ``(vendor, external_session_id)`` (the M18 collision key), so every member —
  canonical or sibling — maps to its group's canonical and contributes its
  source's kind.
"""

from __future__ import annotations

import logging
from datetime import datetime

from ananta.llm.session_ledger.base import (
    LedgerRepositoryError,
    SessionLedgerRepositoryBase,
)
from ananta.llm.session_ledger.schema import (
    ID_PREFIX_SESSION_SOURCE_KIND,
    NAMESPACE,
    TABLE_SESSION,
    TABLE_SESSION_SOURCE_KIND,
    TABLE_SOURCE,
)
from ananta.llm.session_ledger.shared import _new_id

RELOAD_SAFE = True

logger = logging.getLogger(__name__)

# Session keyset page size — sessions are far fewer than events; the ≤100 cap
# matches every other ledger paged read.
_BACKFILL_SESSION_PAGE = 100


class SessionLedgerSessionSourceKindBackfillMixin(SessionLedgerRepositoryBase):
    """Slice list_sessions junction backfill mixin (see module docstring)."""

    __slots__ = ()

    def backfill_session_source_kinds(self) -> dict[str, int]:
        """Populate the ``session_source_kind`` junction from session→source.

        Idempotent (DO-NOTHING on the UNIQUE pair), fail-loud on an unresolvable
        canonical / source, and bounded (two small maps + an id-keyset scan).
        Returns ``{"sessions_scanned", "junction_rows_written"}`` —
        ``junction_rows_written`` is the count of NEW pairs inserted this run
        (0 on a converged ledger; re-run to confirm completeness).
        """
        source_kind_by_id = self._junction_source_kind_by_id()
        canonical_id_by_group = self._canonical_id_by_group()
        now = self._clock()
        sessions_scanned = 0
        junction_rows_written = 0
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
                junction_rows_written += self._attribute_session_kind(
                    session,
                    source_kind_by_id=source_kind_by_id,
                    canonical_id_by_group=canonical_id_by_group,
                    now=now,
                )
            cursor = str(page[-1]["id"])
        logger.info(
            "backfill_session_source_kinds: scanned %d session(s), wrote %d "
            "junction row(s)",
            sessions_scanned, junction_rows_written,
        )
        return {
            "sessions_scanned": sessions_scanned,
            "junction_rows_written": junction_rows_written,
        }

    def _junction_source_kind_by_id(self) -> dict[str, str]:
        """Map every live source id → its ``source_kind`` (bounded — sources are few)."""
        return {
            str(row["id"]): str(row["source_kind"])
            for row in self._query(TABLE_SOURCE, {"is_deleted": 0})
        }

    def _canonical_id_by_group(self) -> dict[tuple[str, str], str]:
        """Map each ``(vendor, external_session_id)`` group → its canonical row id.

        Pages the canonical rows (``canonical_external_session_id IS NULL``) by
        id keyset. Siblings share their canonical's ``(vendor, external_session_id)``
        so this single map resolves every member's group head.
        """
        out: dict[tuple[str, str], str] = {}
        cursor = ""
        while True:
            page = self._query_ordered(
                TABLE_SESSION,
                filters={
                    "id": {"op": "gt", "value": cursor},
                    "canonical_external_session_id": {"op": "is_null"},
                },
                order_by=[["id", "asc"], ["created_at", "asc"]],
                limit=_BACKFILL_SESSION_PAGE,
            )
            if not page:
                break
            for row in page:
                group = (str(row["vendor"]), str(row["external_session_id"]))
                out[group] = str(row["id"])
            cursor = str(page[-1]["id"])
        return out

    def _attribute_session_kind(
        self,
        session: dict[str, object],
        *,
        source_kind_by_id: dict[str, str],
        canonical_id_by_group: dict[tuple[str, str], str],
        now: datetime,
    ) -> int:
        """Record one session's source_kind under its group's canonical; FAIL-LOUD.

        Returns 1 if a NEW junction row was inserted, 0 if the pair already
        existed (DO-NOTHING).
        """
        group = (str(session["vendor"]), str(session["external_session_id"]))
        canonical_id = canonical_id_by_group.get(group)
        if canonical_id is None:
            raise LedgerRepositoryError(
                "backfill_session_source_kinds: session "
                f"{session['id']!r} group {group!r} has no canonical row "
                "(duplicate-/orphaned-canonical anomaly — run "
                "lift_canonical_pointer_for_duplicate_sessions first)",
            )
        source_kind = source_kind_by_id.get(str(session["source_id"]))
        if source_kind is None:
            raise LedgerRepositoryError(
                "backfill_session_source_kinds: session "
                f"{session['id']!r} references source {session['source_id']!r} "
                "with no live __source row (data anomaly)",
            )
        inserted = self._upsert_do_nothing(
            TABLE_SESSION_SOURCE_KIND,
            {
                "id": _new_id(ID_PREFIX_SESSION_SOURCE_KIND),
                "namespace": NAMESPACE,
                "canonical_session_id": canonical_id,
                "source_kind": source_kind,
                "created_at": now,
                "updated_at": now,
            },
            conflict_columns=["canonical_session_id", "source_kind"],
            conflict_predicate=[],
        )
        return 1 if inserted else 0


__all__ = [
    "RELOAD_SAFE",
    "SessionLedgerSessionSourceKindBackfillMixin",
]
