"""Summarize domain mixin for the session-ledger repository.

W5.O cycle 8 §3.7 (with C5 fold: ``mark_session_summary_text`` is here per
KB-article 03 coherence — all 4 summary-write paths cluster together).

SQL-lockdown (summarize slice): all four paths are off raw SQL onto the
``StateManagementInterface`` typed primitives — autocommit ``_update`` /
``_delete`` and the typed-txn ``write_state`` / ``query_state`` /
``update_state`` ops. The single correlated ``MAX(chunk_index)`` SQL subquery
guard in ``persist_summary`` is recomputed in Python from an in-txn read (the
state-interface grammar cannot emit ``COALESCE((SELECT MAX(...)))``); it is
equivalent under single-writer-per-session (the serial action-queue / cron
summarize path — same fence the ingest slice documents).
"""

from __future__ import annotations

from datetime import datetime
from typing import cast

from ananta.llm.session_ledger.base import SessionLedgerRepositoryBase
from ananta.llm.session_ledger.schema import (
    ID_PREFIX_SUMMARY,
    NAMESPACE,
    TABLE_SESSION,
    TABLE_SUMMARY,
)
from ananta.llm.session_ledger.shared import _new_id, _strip_nuls

RELOAD_SAFE = True


class SessionLedgerSummarizeMixin(SessionLedgerRepositoryBase):
    """Summarize domain mixin (4 summary-write/read paths)."""

    __slots__ = ()

    def overwrite_summary_text_for_codex_stage1(
        self,
        *,
        session_id: str,
        new_summary_text: str,
    ) -> None:
        """Replace ``__session.summary_text`` and hard-delete stale ``__summary`` chunk0.

        Two AUTOCOMMIT writes (NOT one transaction): the typed-txn surface
        (``StateTransaction``) exposes no delete op, and the chunk0 removal must
        be a HARD delete — Postgres UNIQUE indexes ignore ``is_deleted``, so a
        soft-delete would leave a zombie chunk0 row that blocks the caller's
        chunk0 re-insert with a ``UniqueViolation``. The lost atomicity is
        acceptable on recoverability grounds: the sole caller
        (``SessionLedgerService._lift_stage1_candidates``) is an operator-gated,
        cron-paused rewrite that re-pushes a fresh chunk0 immediately after this
        call and is idempotent on re-run — a partial failure (UPDATE done but
        DELETE not, or vice versa) is fully repaired by re-invoking the lift
        verb. The UPDATE keeps the original ``is_deleted = 0`` guard; the hard
        DELETE matches the original ``WHERE session_id AND chunk_index = 0`` (no
        ``is_deleted`` filter — chunk0 is removed whether live or soft-deleted).
        """
        clean_summary = _strip_nuls(new_summary_text)
        now = self._clock()
        self._update(
            TABLE_SESSION,
            {"id": session_id, "is_deleted": 0},
            {"summary_text": clean_summary, "updated_at": now},
        )
        self._delete(
            TABLE_SUMMARY,
            {"session_id": session_id, "chunk_index": 0},
            soft=False,
        )

    def persist_summary(
        self,
        *,
        session_id: str,
        chunk_index: int,
        summary_text: str,
        embedding_vector_id: str,
        generated_by_client_id: str,
        generated_at: datetime,
    ) -> str:
        """Insert one session_ledger__summary row. Returns the new ``sum_...`` id.

        One transaction: INSERT the chunk, then keep ``__session.summary_text``
        tracking the LATEST chunk. The original guard
        ``summary_text IS NULL OR %(chunk_index)s >= COALESCE((SELECT
        MAX(chunk_index) FROM __summary WHERE session_id AND is_deleted=0), -1)``
        is recomputed in Python: read the live chunk set + the session row, then
        fire the denorm UPDATE iff the session's ``summary_text`` is still NULL
        OR this chunk is the highest-indexed one. The correlated subquery ran
        AFTER the INSERT, but in-txn read-visibility of the just-written row does
        NOT change the boolean — its ``chunk_index`` value is exactly
        ``chunk_index``, so ``chunk_index >= MAX`` reduces to ``chunk_index >=
        MAX(existing)`` either way. Equivalent under single-writer-per-session.
        """
        summary_id = _new_id(ID_PREFIX_SUMMARY)
        now = self._clock()
        clean_summary_text = _strip_nuls(summary_text) or ""
        with self._state.transactional() as txn:
            # ``summary_text`` is passed as a plain str; datetimes go RAW through
            # the typed-txn serializer (the F1 TZ seam), not ``_naive_utc``.
            txn.write_state(
                NAMESPACE,
                {
                    "table": TABLE_SUMMARY,
                    "record": {
                        "id": summary_id,
                        "namespace": NAMESPACE,
                        "session_id": session_id,
                        "chunk_index": chunk_index,
                        "summary_text": clean_summary_text,
                        "embedding_vector_id": embedding_vector_id,
                        "generated_at": generated_at,
                        "generated_by_client_id": generated_by_client_id,
                        "created_at": now,
                        "updated_at": now,
                    },
                },
            )
            chunks = txn.query_state(
                NAMESPACE,
                {
                    "table": TABLE_SUMMARY,
                    "filters": {"session_id": session_id, "is_deleted": 0},
                },
            )
            max_chunk = max(
                (int(cast(int, row["chunk_index"])) for row in chunks),
                default=-1,
            )
            # The original UPDATE's WHERE is just ``id = %s`` (no is_deleted) +
            # the OR guard; read the session the same way to evaluate the NULL arm.
            sessions = txn.query_state(
                NAMESPACE,
                {"table": TABLE_SESSION, "filters": {"id": session_id}},
            )
            summary_text_is_null = (
                bool(sessions) and sessions[0].get("summary_text") is None
            )
            if summary_text_is_null or chunk_index >= max_chunk:
                txn.update_state(
                    NAMESPACE,
                    {"table": TABLE_SESSION, "filters": {"id": session_id}},
                    {"summary_text": clean_summary_text, "updated_at": now},
                )
        return summary_id

    def list_summaries_by_external_ids(
        self,
        embedding_vector_ids: list[str],
    ) -> list[dict[str, object]]:
        """Read summary rows whose ``embedding_vector_id`` is in the input list.

        The raw ``embedding_vector_id IN (...)`` becomes the sanctioned list →
        ``= ANY`` grammar; ``is_deleted: 0`` is passed explicitly (``query_state``
        does not inject the soft-delete filter). The primitive returns ``SELECT
        *`` — a superset of the columns the caller (``search``) reads, which is
        harmless.
        """
        if not embedding_vector_ids:
            return []
        return self._query(
            TABLE_SUMMARY,
            {"embedding_vector_id": list(embedding_vector_ids), "is_deleted": 0},
        )

    def mark_session_summary_text(
        self,
        *,
        session_id: str,
        summary_text: str,
    ) -> None:
        """Force-set ``session.summary_text`` without inserting a summary chunk.

        Used by the M6 auto-summarize cron to mark trivial sessions
        (operator ruling 2026-06-01) so ``list_quiescent_sessions`` stops
        re-picking them. C5 fold: this lives in Summarize (not Ingest) per
        KB-article 03 coherence — ``summary_text`` is a Summarize-domain
        concept across all four origin paths.

        The original guard ``WHERE id = %s AND summary_text IS NULL`` maps to the
        ``{"op": "is_null"}`` filter grammar (a write-once compare-and-set: a row
        whose ``summary_text`` is already set matches 0 rows and is left
        untouched). The rows-affected count is discarded.
        """
        now = self._clock()
        self._update(
            TABLE_SESSION,
            {"id": session_id, "summary_text": {"op": "is_null"}},
            {"summary_text": _strip_nuls(summary_text), "updated_at": now},
        )


__all__ = ["RELOAD_SAFE", "SessionLedgerSummarizeMixin"]
