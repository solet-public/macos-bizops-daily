"""Inverted-bounds-repair domain mixin for the session-ledger repository.

W5.O cycle 7 §3.6: 5 public one-shot operator-repair verbs covering inverted
``first_event_at`` / ``last_event_at`` bounds, stale pgvector-internal
``__summary`` pointers, and orphan running ``__import_batch`` rows.

SQL-lockdown #0, Slice 3b: migrated off raw ``transactional()`` SQL onto the
state-interface primitives (``query_state`` / ``query_ordered`` / ``update_state``
via the ``base.py`` ``_query`` / ``_query_ordered`` / ``_update`` seams). Every
rewrite is ledger-side per the Architect ruling (``workbench/2026-06-20_ledger_
migration_slice_plan.md`` → ``★ 3b GAPS RESOLVED``) — NO new primitives, verbs,
or exceptions:

* **GAP-i** ``last_event_at < first_event_at`` is a CROSS-COLUMN predicate (not
  the col-vs-value filter grammar) → **Python-fold** over a ``query_state`` read.
  The per-session ``min/max(event_at)`` recompute → **2× ``query_ordered``
  limit-1** (asc→min, desc→max, ``(event_at, id)`` tie-break). The
  ``SELECT … FOR UPDATE`` row-lock is dropped: the recompute is deterministic and
  the verb runs inside the importer-poll pause/resume envelope (single writer),
  so the outer inverted-row re-read alone establishes idempotency (a repaired row
  is no longer inverted and is not re-selected on a re-run).
* **GAP-ii** ``embedding_vector_id LIKE 'emb-%'`` is a LIKE pattern (not in the
  grammar) → **Python ``str.startswith('emb-')``** over a ``query_state`` read.
* **GAP-iii** the cross-table ``UPDATE … FROM <pgvector>__embeddings`` join is
  replaced by **PATH-1 ledger-side recompute**: each broken ``__summary`` row's
  deterministic ``external_id`` is ``f"{session_id}:{chunk_index}"`` (the id
  ``SummaryWriter._store_vector`` supplies at ``summarization.py:234``), recomputed
  from the row's OWN columns and written back with a conditional ``update_state``
  keyed on the current internal pointer — zero pgvector access, no cross-namespace
  read, no join primitive.

The ``started_at < cutoff`` orphan-batch range predicate (also not in the
grammar) is handled the same way: the running-batch set is read by equality and
Python-filtered on the cutoff, then the stale ids are failed in one ``=ANY``
``update_state`` whose ``status = RUNNING`` predicate IS the compare-and-set.

Read-cap sweep, 2026-08-16 (lane-ak). Every ``query_state`` read above was
UNBOUNDED — no ``limit``, no ``unbounded``, whole matching set in one shot. That
survived only because the cap was 10,000; ``dcb1722c7`` lowered it to 100 and all
six became refusals. Two distinct urgencies, kept distinct on purpose:

* ``__session`` at 27,208 rows was ALREADY over the old 10,000 cap — pre-existing
  breakage, not a regression this deploy introduces.
* ``__summary`` (4,913) and the per-source RUNNING ``__import_batch`` set (94 for
  the largest source) both WORKED under the old cap and would be newly broken by
  the new one. Those are regressions.

The Python folds themselves are not the defect and are not removed: each encodes
a predicate the flat filter grammar genuinely cannot express — a cross-column
comparison, a LIKE prefix, an AND-composed range. What changed is that they now
fold over a PAGED walk (``base.walk_table``, one page in memory, tie-safe
``(created_at, id)`` cursor) instead of a materialized whole table, and the two
folds that only ever produced a NUMBER became scalar ``count`` aggregates
(``base.count_rows``) that ship no rows at all.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

from ananta.llm.session_ledger.base import (
    SessionLedgerRepositoryBase,
    count_rows,
    walk_table,
)
from ananta.llm.session_ledger.schema import (
    TABLE_EVENT,
    TABLE_IMPORT_BATCH,
    TABLE_SESSION,
    TABLE_SUMMARY,
)
from ananta.llm.session_ledger.shared import _strip_nuls
from ananta.llm.session_ledger.types import ImportBatchStatus

RELOAD_SAFE = True

logger = logging.getLogger(__name__)

_EMB_INTERNAL_PREFIX = "emb-"

# ── Walk ceilings ────────────────────────────────────────────────────────────
#
# Every whole-table read in this module was a single unbounded ``_query``. That
# was correct against the pre-2026-08-15 10,000-row cap for the SUMMARY reads
# and already wrong for the SESSION ones; at the 100-row cap all of them are
# refused. They are now paged walks, and a paged walk still has to say how far
# it is willing to go — the ceiling is that statement.
#
# A ceiling is NOT a claim that the table is small. It is a claim about how large
# a walk THIS call site was designed to perform, so that a table which has grown
# past the design gets a loud error naming the assumption instead of a repair
# pass that quietly runs for an hour.

#: One row per ingested LLM session, across every source. Measured 27,208 on
#: 2026-08-16; the ledger ingests a few thousand sessions a month, so this is
#: roughly a decade of headroom. These are one-shot operator repair verbs — if a
#: repair ever needs to walk half a million sessions, the right answer is a
#: set-based statement, not a longer row loop.
_SESSION_WALK_CEILING = 500_000

#: One row per summary chunk (a small multiple of summarized sessions).
#: Measured 4,913 on 2026-08-16. Same reasoning, same order of headroom.
_SUMMARY_WALK_CEILING = 200_000

#: RUNNING ``__import_batch`` rows for ONE source. These accumulate only when a
#: poll dies mid-batch, and draining them is what this verb is for — so the set
#: is bounded by the failure rate, not by the ledger's size. Measured 94 for the
#: largest source (``agent_messaging``) on 2026-08-16 — SIX rows under the
#: 100-row cap, which is why this read is in the sweep at all rather than filed
#: as theoretical.
_RUNNING_BATCH_WALK_CEILING = 100_000


def _as_dt(value: object) -> datetime:
    """Coerce a state-primitive timestamp cell to an aware-UTC ``datetime``.

    The ledger stores timestamps as Postgres ``TIMESTAMP`` (no tz) holding the
    naive-UTC wall-clock (the 2026-06-12 F1 TZ-storage seam), so ``query_state`` /
    ``query_ordered`` surface them as **naive** datetimes (or naive ISO strings via
    ``provider._serialize_for_json``); an aware ``datetime`` can still arrive from
    an in-memory test double on a ``timestamptz`` column. Both normalize to an
    **aware UTC** ``datetime`` — naive cells are taken as UTC — so the cross-column
    and ``started_at < cutoff`` comparisons (the cutoff being an aware
    ``datetime.now(UTC)``) never mix naive and aware operands and are
    instant-correct rather than lexicographic. Fail fast on any other cell type —
    a non-timestamp here is an upstream contract violation, not something to
    silently coerce.
    """
    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, str):
        dt = datetime.fromisoformat(value)
    else:
        raise TypeError(
            f"expected a datetime or ISO-8601 string timestamp cell, got "
            f"{type(value).__name__!r}",
        )
    return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt.astimezone(UTC)


def _is_inverted(row: dict[str, object]) -> bool:
    """True iff a ``__session`` row's ``last_event_at`` precedes its ``first_event_at``.

    A row missing either bound is NOT inverted — mirroring the pre-migration SQL
    ``last_event_at < first_event_at`` whose ``<`` yields NULL (unmatched) on a
    NULL operand.
    """
    first = row.get("first_event_at")
    last = row.get("last_event_at")
    if first is None or last is None:
        return False
    return _as_dt(last) < _as_dt(first)


def _has_internal_pointer(row: dict[str, object]) -> bool:
    """True iff a ``__summary`` row still carries an internal ``emb-`` pointer."""
    pointer = row.get("embedding_vector_id")
    return isinstance(pointer, str) and pointer.startswith(_EMB_INTERNAL_PREFIX)


def _summary_external_id(row: dict[str, object]) -> str | None:
    """Recompute the deterministic ``external_id`` from a ``__summary`` row's own columns.

    Mirrors ``SummaryWriter._store_vector`` (``summarization.py:234``):
    ``f"{session_id}:{chunk_index}"``. Returns ``None`` when either column is
    absent so the caller can skip (rather than mint a malformed pointer).
    ``chunk_index`` is checked with ``is None`` — ``0`` is a valid first chunk.
    """
    session_id = row.get("session_id")
    chunk_index = row.get("chunk_index")
    if session_id is None or chunk_index is None:
        return None
    return f"{session_id}:{chunk_index}"


def _started_before(row: dict[str, object], cutoff: datetime) -> bool:
    """True iff an ``__import_batch`` row started strictly before ``cutoff``.

    A row with no ``started_at`` is NOT stale — mirroring the pre-migration SQL
    ``started_at < cutoff``, whose ``<`` yields NULL (unmatched) on a NULL
    operand. Same NULL convention as :func:`_is_inverted`.
    """
    started_at = row.get("started_at")
    return started_at is not None and _as_dt(started_at) < cutoff


def _orphan_result(*, total: int, remaining: int) -> dict[str, int]:
    """Build the orphan-batch return envelope from before/after RUNNING counts.

    Post-state contract (matches the pre-migration SQL, which re-counted RUNNING
    after its UPDATE): ``repaired`` is the net reduction in RUNNING rows
    (``total - remaining``) and ``untouched`` is the rows STILL RUNNING
    (``remaining``) — the meaning the public metadata documents. A row a
    concurrent owner completed out of RUNNING is therefore neither lost from the
    repaired tally nor mis-counted as untouched.
    """
    return {
        "repaired_count": total - remaining,
        "untouched_count": remaining,
        "total_orphan_count_before": total,
    }


class SessionLedgerInvertedBoundsRepairMixin(SessionLedgerRepositoryBase):
    """Inverted-bounds-repair domain mixin (one-shot operator repairs)."""

    __slots__ = ()

    # ── shared walks ─────────────────────────────────────────────────────────

    def _walk_live_sessions(self) -> Iterator[dict[str, object]]:
        """Every live ``__session`` row, paged.

        ``walk_table`` applies ``is_deleted = 0`` itself (it pages through
        ``query_ordered``), so — unlike the ``_query`` it replaces — the filter
        is empty rather than carrying an explicit ``is_deleted: 0``. Passing both
        is the one way to get this wrong.
        """
        return walk_table(
            self._state,
            TABLE_SESSION,
            {},
            ceiling=_SESSION_WALK_CEILING,
            reason=(
                "one row per ingested LLM session across every source "
                "(measured 27,208 on 2026-08-16); these are one-shot operator "
                "repairs, and past this size the repair belongs in a set-based "
                "statement rather than a row loop."
            ),
        )

    def _walk_live_summaries(self) -> Iterator[dict[str, object]]:
        """Every live ``__summary`` row, paged. Same ``is_deleted`` note as above."""
        return walk_table(
            self._state,
            TABLE_SUMMARY,
            {},
            ceiling=_SUMMARY_WALK_CEILING,
            reason=(
                "one row per summary chunk, a small multiple of the summarized "
                "session count (measured 4,913 on 2026-08-16)."
            ),
        )

    # ── GAP-i: inverted first/last event_at bounds ───────────────────────────

    def count_inverted_first_last_event_at_sessions(self) -> int:
        """Count live ``__session`` rows where ``last_event_at < first_event_at``.

        Cross-column predicate (GAP-i): ``last_event_at < first_event_at``
        compares two COLUMNS, and the filter grammar is column-vs-value only, so
        the predicate cannot be pushed to the provider and cannot be answered by
        the scalar ``count`` aggregate either. The rows have to be folded in
        Python — but only a counter is kept, never the rows, so the walk holds
        one page at a time regardless of how many sessions exist.
        """
        return sum(1 for row in self._walk_live_sessions() if _is_inverted(row))

    def repair_inverted_first_last_event_at(self) -> int:
        """Recompute ``first_event_at`` / ``last_event_at`` for inverted rows.

        For each inverted session the new bounds are the live-event extremes,
        each read as a single indexed ``query_ordered`` row (asc→min, desc→max)
        so a 32K-event session is never materialized. A session with no live
        events keeps its bounds (logged) — there is nothing to recompute from.

        The pre-migration ``SELECT … FOR UPDATE`` row-lock is gone: the verb runs
        inside the importer-poll pause/resume envelope (single writer) and the
        recompute is deterministic, so the ``update_state`` is filtered on ``id``
        alone — the outer inverted-row re-read makes a re-run a no-op (a repaired
        row is no longer inverted). Returns the rows-affected total.

        The outer scan WRITES to the rows it is walking. That is safe here and
        not by luck: the walk's cursor is ``(created_at, id)``, neither of which
        this update touches, and it is a row-value cursor rather than an offset,
        so a repaired row cannot move across it or shift the rows ahead of it.
        The repair also cannot drop a row out of the walk's predicate — it never
        writes ``is_deleted``.
        """
        repaired = 0
        now = self._clock()
        for row in self._walk_live_sessions():
            if not _is_inverted(row):
                continue
            session_id = str(row["id"])
            new_first = self._extreme_event_at(session_id, direction="asc")
            new_last = self._extreme_event_at(session_id, direction="desc")
            if new_first is None or new_last is None:
                logger.warning(
                    "repair_inverted_first_last_event_at: session %s has no "
                    "events; leaving inverted bounds in place",
                    session_id,
                )
                continue
            repaired += self._update(
                TABLE_SESSION,
                {"id": session_id, "is_deleted": 0},
                {
                    "first_event_at": new_first,
                    "last_event_at": new_last,
                    "updated_at": now,
                },
            )
        return repaired

    def _extreme_event_at(
        self, session_id: str, *, direction: str
    ) -> datetime | None:
        """Read the min (``asc``) or max (``desc``) ``event_at`` for a session.

        One ``query_ordered`` limit-1 read with an ``(event_at, id)`` composite so
        the order is total + tie-safe; ``query_ordered`` applies ``is_deleted = 0``.
        Returns ``None`` when the session has no live events.
        """
        rows = self._query_ordered(
            TABLE_EVENT,
            filters={"session_id": session_id},
            order_by=[["event_at", direction], ["id", direction]],
            limit=1,
        )
        if not rows:
            return None
        event_at = rows[0].get("event_at")
        return _as_dt(event_at) if event_at is not None else None

    # ── GAP-ii / GAP-iii: stale pgvector-internal summary pointers ────────────

    def count_summary_rows_with_pgvector_internal_id_pointer(self) -> int:
        """Count active ``__summary`` rows whose ``embedding_vector_id`` is an internal ``emb-`` id.

        LIKE pattern (GAP-ii) → Python ``str.startswith('emb-')`` over a paged
        walk; NULL / non-str pointers do not match, mirroring SQL ``LIKE``. Only
        a counter is kept, never the rows.

        A prefix match CAN in principle be pushed down as the half-open range
        ``embedding_vector_id >= 'emb-' AND < 'emb.'`` using the Gap-A ``gte`` /
        ``lt`` comparators, which would ship only the matching rows. It is not
        done here on purpose: that rewrite is correct only under a collation
        where ``'.'`` sorts immediately after ``'-'``, so it silently returns the
        wrong set under a different one. A predicate whose correctness depends on
        the database's collation is not worth the pages it saves on a repair verb
        that runs by hand.
        """
        return sum(1 for row in self._walk_live_summaries() if _has_internal_pointer(row))

    def repair_summary_embedding_vector_ids(self) -> dict[str, int]:
        """Rewrite stale ``__summary.embedding_vector_id`` values to the deterministic external_id.

        GAP-iii PATH-1: each broken row's ``external_id`` is recomputed from its
        OWN ``session_id`` + ``chunk_index`` and written back with a conditional
        ``update_state`` keyed on ``(id, the-current-emb-pointer)`` — both plain
        strings, no datetime in the WHERE — so a row a concurrent pass already
        rewrote matches 0 rows (no double-write). Zero pgvector access; the
        pre-migration cross-table ``UPDATE … FROM`` join is gone.

        The return shape preserves the pre-migration contract, which is a
        **post-state** report — so the counts are derived from a fresh recount
        AFTER the writes (the original SQL re-`SELECT count`-ed after its UPDATE),
        NOT from this pass's own rows-affected. That matters under concurrency: a
        pointer-CAS we LOSE to a concurrent repair returns 0 rows-affected, but
        the row is now CORRECT — counting our own hits would mis-report it as
        skipped and undercount the correct rows. Deriving from before/after broken
        counts keeps each field's documented post-state meaning:

        * ``updated_count`` — net reduction in internal-pointer rows
          (``broken_before - broken_after``);
        * ``skipped_count`` — rows STILL carrying an internal pointer at
          completion (``broken_after`` — missing ``session_id`` / ``chunk_index``,
          genuinely unfixed);
        * ``total_rows_now_correct`` — active rows no longer internal
          (``active_after - broken_after``).

        Both passes are paged walks. The first counts and repairs in ONE pass
        (the pre-migration code materialized the whole table, counted it, then
        iterated the same list) — identical arithmetic, but nothing larger than a
        page is ever held. Writing during that pass is safe for the same reason
        as in :meth:`repair_inverted_first_last_event_at`: the pointer rewrite
        touches neither cursor column nor ``is_deleted``, so a repaired row
        neither moves across the cursor nor drops out of the walk.
        """
        now = self._clock()
        broken_before = 0
        for row in self._walk_live_summaries():
            if not _has_internal_pointer(row):
                continue
            broken_before += 1
            external_id = _summary_external_id(row)
            if external_id is None:
                logger.warning(
                    "repair_summary_embedding_vector_ids: summary %s lacks "
                    "session_id/chunk_index; cannot recompute external_id, "
                    "leaving internal pointer in place",
                    row.get("id"),
                )
                continue
            self._update(
                TABLE_SUMMARY,
                {
                    "id": str(row["id"]),
                    "embedding_vector_id": row["embedding_vector_id"],
                    "is_deleted": 0,
                },
                {"embedding_vector_id": external_id, "updated_at": now},
            )
        active_after = 0
        broken_after = 0
        for row in self._walk_live_summaries():
            active_after += 1
            if _has_internal_pointer(row):
                broken_after += 1
        return {
            "updated_count": broken_before - broken_after,
            "skipped_count": broken_after,
            "total_rows_now_correct": active_after - broken_after,
        }

    # ── orphan running import batches (started_at < cutoff range) ─────────────

    def backfill_orphan_running_batches_for_source(
        self,
        source_id: str,
        *,
        source_kind: str | None = None,
        stale_threshold_seconds: int = 86400,
    ) -> dict[str, int]:
        """Mark stale running ``__import_batch`` rows for a source as failed.

        ``started_at < cutoff`` is a range predicate: the per-source running-batch
        set is read by equality (``source_id`` + ``status = RUNNING``) and
        Python-filtered on the cutoff. The stale ids are failed in one ``=ANY``
        ``update_state`` guarded on ``status = RUNNING`` — the predicate IS the
        compare-and-set, so a batch a concurrent pass already failed matches 0
        rows.

        The pre-2026-08-16 code called this set "small + bounded" and read it
        with one unbounded ``_query``. Measured: 94 RUNNING rows for the
        ``agent_messaging`` source, six under the 100-row cap, and the set grows
        precisely when polls are failing — i.e. it is *unbounded in the direction
        that matters*, and the read would have started refusing at the moment the
        verb became necessary. It is a paged walk now, and the two ``len()``
        calls are scalar ``count`` aggregates that ship no rows at all.

        Return shape preserves the pre-migration **post-state** contract: the
        counts come from a fresh RUNNING recount AFTER the CAS (not the status-CAS
        rows-affected). ``repaired_count`` = net reduction in RUNNING
        (``total - remaining``), ``untouched_count`` = rows STILL RUNNING
        (``remaining``), ``total_orphan_count_before`` = RUNNING rows seen before.
        Deriving from the recount means a batch a concurrent owner completes out
        of RUNNING mid-repair is not mis-reported as ``untouched`` (it is no
        longer running) even though our ``status = RUNNING`` CAS missed it.
        """
        if source_kind is not None:
            source_row = self.get_source(source_id)  # type: ignore[attr-defined]
            if source_row is None or source_row.source_kind.value != source_kind:
                return _orphan_result(total=0, remaining=0)
        now = self._clock()
        cutoff = now - timedelta(seconds=stale_threshold_seconds)
        total = self._running_batch_count(source_id)
        if total == 0:
            return _orphan_result(total=0, remaining=0)
        stale_ids = [
            str(row["id"])
            for row in self._walk_running_batches(source_id)
            if _started_before(row, cutoff)
        ]
        if stale_ids:
            self._update(
                TABLE_IMPORT_BATCH,
                {
                    "id": stale_ids,
                    "status": ImportBatchStatus.RUNNING.value,
                    "is_deleted": 0,
                },
                {
                    "status": ImportBatchStatus.FAILED.value,
                    "finished_at": now,
                    "error_message": _strip_nuls(
                        "orphan_repair: stale running batch repaired"
                    ),
                    "error_kind": _strip_nuls("orphan_repair"),
                    "updated_at": now,
                },
            )
        remaining = self._running_batch_count(source_id)
        return _orphan_result(total=total, remaining=remaining)

    def _running_batch_count(self, source_id: str) -> int:
        """How many live RUNNING ``__import_batch`` rows one source has.

        Both the before-total and the after-recount only ever needed a NUMBER;
        the pre-2026-08-16 code got it by reading every row and calling ``len``.
        ``count`` is a scalar aggregate — no rows cross the process boundary, so
        it is outside the read cap entirely and cannot be refused at any size.
        """
        return count_rows(
            self._state,
            TABLE_IMPORT_BATCH,
            {
                "source_id": source_id,
                "status": ImportBatchStatus.RUNNING.value,
                "is_deleted": 0,
            },
        )

    def _walk_running_batches(self, source_id: str) -> Iterator[dict[str, object]]:
        """Live RUNNING ``__import_batch`` rows for one source, paged.

        Only the cutoff fold needs the rows themselves — the equality half of the
        predicate is pushed down to the provider and only ``started_at <
        cutoff`` is evaluated in Python, because the flat filter grammar has no
        range comparator that composes with the ``=`` terms here. ``walk_table``
        supplies ``is_deleted = 0``, so it is absent from ``filters``.
        """
        return walk_table(
            self._state,
            TABLE_IMPORT_BATCH,
            {"source_id": source_id, "status": ImportBatchStatus.RUNNING.value},
            ceiling=_RUNNING_BATCH_WALK_CEILING,
            reason=(
                "RUNNING import batches for ONE source — rows that started and "
                "never finished (measured 94 for the largest source on "
                "2026-08-16). Draining them is what this verb does, so a source "
                "holding this many means polls have been failing unnoticed for a "
                "very long time and the backlog needs looking at, not walking."
            ),
        )


__all__ = ["RELOAD_SAFE", "SessionLedgerInvertedBoundsRepairMixin"]
