"""Base class + cross-cutting primitives for the session-ledger repository.

W5.O cycle 1 (`workbench/2026-06-13_w5o_session_ledger_repository_decomposition_design.md`):
the monolithic ``SessionLedgerRepository`` is decomposed into 9 domain-aligned
mixin modules. Each mixin inherits from :class:`SessionLedgerRepositoryBase`
(this module); the concrete ``SessionLedgerRepository`` inherits from all 9
mixins via concrete-via-MI composition, mirroring the 2026-06-11
``SessionLedgerService`` ABC split.

What lives here:

- :class:`LedgerRepositoryError` — the canonical "repository raised" exception
  every mixin's error path references.
- :class:`SessionLedgerRepositoryBase` — the diamond-root base. Owns the
  ``state_service`` reference + clock, plus the cross-cutting primitives
  used by every domain mixin:

  * :meth:`_query` / :meth:`_query_ordered` — the typed-primitive read seam
    (``query_state`` / ``query_ordered``) that replaced the retired raw-SQL
    ``_fetch_all`` positional-row adapter when the ledger's reads migrated off
    raw SQL (SQL-lockdown).
  * :meth:`_increment_batch_counters` — Ingest ``append_event``'s per-event
    batch ``event_count`` bump (W5.O C2 fold kept it on the base for any future
    cross-mixin caller; ``finish_batch`` does its own terminal-status UPDATE and
    does not increment). Rides the ``increment_and_return`` typed-txn primitive
    (the SQL-lockdown Slice-5 migration).
  * :meth:`count_rows_per_table` — cross-cutting test/utility surface for the
    ``reset_ingest_state`` dry-run path; routes through the sanctioned
    autocommit ``count`` aggregate primitive (one call per table) so the
    dry-run can report the content the now-non-destructive reset PRESERVES.

What does NOT live here:

- Domain-axis read / ingest / annotation / polling_driver / canonical_pointer
  / inverted_bounds / summarize / deployment / search verbs — each in its own
  domain mixin module (cycles 2-10).
- SQL primitives / sanitization / type-coercion helpers — in ``shared.py``
  (cycle 1 §3.10).
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from typing import cast

from ananta.core.domain.enums import ActionStatus
from ananta.core.domain.types import ActionResult
from ananta.interfaces.state_management_interface import (
    StateManagementInterface,
    StateTransaction,
)
from ananta.llm.session_ledger.schema import (
    NAMESPACE,
    TABLE_EVENT,
    TABLE_IMPORT_BATCH,
)
from ananta.services.state_service.bounded_read import iter_table_rows
from ananta.services.state_service.read_bounds import MAX_READ_ROWS

# Module-level RELOAD_SAFE marker — no module-level mutable state, no
# background threads, no held service references. Pure class adapter.
RELOAD_SAFE = True


class LedgerRepositoryError(RuntimeError):
    """Raised when a ledger-repository invariant is violated.

    Callers translate this into structured user-facing error payloads.
    The repository never silently swallows or coerces.
    """


def _naive_utc(value: object) -> object:
    """Normalize a tz-aware ``datetime`` to naive UTC — the F1 TZ-storage seam.

    The platform stores timestamps as ``timestamp without time zone`` in naive
    UTC. The autocommit ``write_state`` / ``update_state`` value serializer
    (unlike the typed-txn ``serialize_value_for_txn`` path) binds a tz-aware
    value's offset ISO string verbatim, so a non-UTC repository clock would
    store the local wall-clock instead of its UTC instant. Normalizing every
    written value here keeps the clock contract UTC-correct regardless of the
    injected clock's ``tzinfo``. Non-datetime values pass through untouched.
    """
    if isinstance(value, datetime) and value.tzinfo is not None:
        return value.astimezone(UTC).replace(tzinfo=None)
    return value


def count_rows(
    state: StateManagementInterface, table: str, filters: dict[str, object]
) -> int:
    """Scalar ``COUNT(*)`` under ``filters`` — the no-rows-shipped ledger read.

    The right primitive whenever a call site reads rows only to call ``len`` on
    them. ``count`` materializes nothing, so it is not subject to the read cap
    and cannot be refused however large the matching set is.

    Same filter grammar as the repository's ``_query``, and — like it, unlike
    ``query_ordered`` — NO automatic ``is_deleted`` exclusion. Pass
    ``is_deleted: 0`` explicitly.

    A free function rather than a repository method on purpose: it needs nothing
    from the instance but the state interface, and the repository base is at the
    god-class bound (2026-08-16, lane-ak — adding these two as methods pushed it
    from 468 to 517 LOC).
    """
    result = state.count(NAMESPACE, {"table": table, "filters": filters})
    if result.get("action_status") != ActionStatus.COMPLETED.value:
        return 0
    data = result.get("data")
    inner = data.get("result") if isinstance(data, dict) else None
    if not isinstance(inner, dict):
        return 0
    return int(cast(int, inner.get("value", 0)))


def walk_table(
    state: StateManagementInterface,
    table: str,
    filters: dict[str, object],
    *,
    ceiling: int,
    reason: str,
    include_deleted: bool = False,
) -> Iterator[dict[str, object]]:
    """Yield EVERY ledger row matching ``filters``, paged — the row-capped read.

    The seam for a ledger read that genuinely needs the whole matching set over a
    table that grows. The repository's ``_query`` carries the provider's row bound
    for the WHOLE read and is refused above it; this walks tie-safe
    ``(created_at, id)`` pages instead, so it is complete at any table size, never
    consents to a full scan, and holds one page in memory rather than the result.

    Prefer this over ``_query(..., unbounded=True)`` for anything that grows:
    ``unbounded=True`` is consent to materialize the whole table on the shared
    process, correct only for a set that CANNOT grow past what the process holds.

    **The ``is_deleted`` asymmetry, which is the trap in this migration.**
    ``query_state`` (behind ``_query``) applies NO soft-delete filter — callers
    pass ``is_deleted: 0`` themselves. ``query_ordered`` (behind this walk)
    applies one BY DEFAULT. So moving a site from one to the other changes its
    deleted-row semantics unless the filter is adjusted in the same edit: drop
    the caller's explicit ``is_deleted: 0`` (it is now the default and passing
    both is the way to get this wrong), and pass ``include_deleted=True`` where
    the site deliberately wanted deleted rows too. Note that ``is_deleted = 0``
    and the default both EXCLUDE a NULL ``is_deleted``, so a site whose original
    filter was the explicit ``{"is_deleted": 0}`` is unchanged — but a site whose
    Python fold was the OR-predicate ``is_deleted IS NULL OR = 0`` is NOT, and
    needs ``include_deleted=True`` plus its fold kept.

    Safe to write to rows mid-walk: the cursor columns are immutable after
    insert. See :func:`~ananta.services.state_service.bounded_read.iter_table_rows`
    for the full contract, including why the ceiling boundary is ``>``.
    """
    return iter_table_rows(
        state,
        namespace=NAMESPACE,
        table=table,
        filters=filters,
        ceiling=ceiling,
        reason=reason,
        include_deleted=include_deleted,
    )


class SessionLedgerRepositoryBase:
    """Diamond-root base for every ``SessionLedger*Mixin``.

    Concrete ``SessionLedgerRepository`` inherits from all 9 domain mixins;
    each mixin inherits from this base. C3 linearization is deterministic
    because all mixins share exactly one parent (this class).
    """

    __slots__ = ("_state", "_clock")

    def __init__(
        self,
        state_service: StateManagementInterface,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._state = state_service
        self._clock = clock or (lambda: datetime.now(UTC))

    # ------------------------------------------------------------------
    # Cross-cutting test/utility surface (reset_ingest_state dry-run
    # content-preserved counts; smoke-test fixtures)
    # ------------------------------------------------------------------

    def count_rows_per_table(
        self,
        tables: tuple[str, ...],
    ) -> dict[str, int]:
        """Count live rows per table for the reset_ingest_state dry-run.

        Per 2026-05-31 Architect ruling §4: each table is counted with
        ``is_deleted = 0`` (live rows only). Tables that haven't been
        created yet (fresh solet pre-schema-init) return 0; the verb
        stays valid in that case too.

        SQL-lockdown migration: the per-table raw ``SELECT count(*) ...
        WHERE is_deleted = 0`` (formerly on the typed-txn ``fetch_one``
        seam) is replaced by the sanctioned autocommit ``count`` aggregate
        primitive — the count runs index-backed INSIDE the owner plugin and
        ships only the scalar. ``count`` applies NO auto ``is_deleted``
        exclusion (it mirrors ``query_state``, not ``query_ordered``), so
        the live-only filter is passed explicitly. The inputs are
        fully-qualified physical names (``session_ledger__<table>``); the
        primitive re-composes ``{namespace}__{table}``, so the
        ``{NAMESPACE}__`` prefix is stripped back to the bare table name.
        Each table is its own autocommit call (no shared txn — the dry-run
        wants a per-table snapshot, not one atomic read).
        """
        counts: dict[str, int] = {}
        for table in tables:
            bare = table.removeprefix(f"{NAMESPACE}__")
            result = self._state.count(
                NAMESPACE,
                {"table": bare, "filters": {"is_deleted": 0}},
            )
            counts[table] = self._count_scalar(result)
        return counts

    @staticmethod
    def _count_scalar(result: ActionResult) -> int:
        """Extract the ``data.result.value`` scalar from a ``count`` envelope.

        A non-success envelope — e.g. the table is absent on a fresh
        pre-schema-init solet, which ``run_aggregate`` surfaces as a
        ``state.count_failed`` error result rather than a raise — yields 0,
        keeping the documented pre-schema-init tolerance. This is in fact
        MORE robust than the retired raw-SQL path: that ran all tables in ONE
        ``transactional()``, so a missing table raised ``UndefinedTable`` and
        aborted the whole txn — every SUBSEQUENT (even existing) table then hit
        ``InFailedSqlTransaction`` and also fell through to 0. The per-table
        autocommit count here isolates each table, so a missing one zeroes only
        itself; existing tables still report their true count.

        The gate is the strict ``!= COMPLETED`` fail-fast (the count envelope
        only ever emits ``completed`` or ``error`` — a missing table is the
        latter, served by ``error → 0``); a non-``completed`` status is treated
        as no-count rather than maybe-success.
        """
        if result.get("action_status") != ActionStatus.COMPLETED.value:
            return 0
        data = result.get("data")
        inner = data.get("result") if isinstance(data, dict) else None
        if not isinstance(inner, dict):
            return 0
        return int(cast(int, inner.get("value", 0)))

    def count_events_missing_external_id(self) -> int:
        """Count live ``__event`` rows with a NULL ``external_id`` (reset precondition).

        GAP-5 slice 3: ``reset_ingest_state``'s non-destructive replay only
        dedups rows that HAVE an ``external_id``. A legacy null-``external_id``
        row is NOT covered by the ``(session_id, external_id)`` unique (Postgres
        treats NULLs as DISTINCT), so a post-reset re-walk derives a non-null id
        that does not conflict with the null legacy row and INSERTS A DUPLICATE.
        ``reset_ingest_state`` therefore refuses while any remain — this is the
        precondition count it gates on. Routed through the scalar ``count``
        aggregate with an ``is_null`` filter so the (potentially ~100k-row) null
        backlog never materializes on the shared process.
        """
        result = self._state.count(
            NAMESPACE,
            {
                "table": TABLE_EVENT,
                "filters": {"external_id": {"op": "is_null"}, "is_deleted": 0},
            },
        )
        return self._count_scalar(result)

    # GAP-5 slice 3: ``hard_delete_rows_atomic`` (the raw-SQL ``DELETE FROM
    # <table>`` truncation that backed the destructive ``reset_ingest_state``)
    # was REMOVED. ``reset_ingest_state`` is now a non-destructive per-source
    # ``__source_cursor`` reset — the next poll replays each source and the live
    # ``(session_id, external_id)`` upsert reconverges, so the wipe (and its
    # last raw-SQL site here) is gone. ``count_rows_per_table`` above stays: the
    # reset's dry-run still uses it to report the content the reset PRESERVES.

    # ------------------------------------------------------------------
    # Cross-mixin private primitives
    # ------------------------------------------------------------------

    def _increment_batch_counters(
        self,
        txn: StateTransaction,
        *,
        batch_id: str,
        event_delta: int,
    ) -> None:
        """Apply the per-event ``event_count`` delta to an in-flight batch.

        Lives on the base so any mixin can reach it via
        ``self._increment_batch_counters(...)`` without cross-module imports
        (W5.O C2 fold); ``append_event`` (Ingest) is the only live caller today.
        Rides the ``increment_and_return`` typed-txn primitive — the atomic
        ``event_count = event_count + by`` UPDATE takes a row lock held to
        commit and ``updated_at`` is maintained by the BEFORE-UPDATE trigger
        (so no explicit ``now`` write). The primitive RAISES if 0 rows match
        (a missing batch is a contract violation — fail fast, not a silent
        no-op); the post-increment value is unused here.
        """
        txn.increment_and_return(
            NAMESPACE,
            {
                "table": TABLE_IMPORT_BATCH,
                "filters": {"id": batch_id},
                "column": "event_count",
                "by": event_delta,
            },
        )

    # ------------------------------------------------------------------
    # The shared typed-primitive read seam (the retired _fetch_all's heir)
    # ------------------------------------------------------------------

    def _records_from_result(
        self, result: ActionResult, *, context: str,
    ) -> list[dict[str, object]]:
        """Extract dict rows from a typed state-primitive query envelope.

        The ``query_state`` / ``query_ordered`` primitives return rows as
        ``dict`` records under ``data.records`` — already dict-shaped, so
        (unlike the retired raw-SQL ``_fetch_all`` path with its
        ``_columns_from_select_sql`` positional-row parser) no SELECT-column
        parsing is needed. This is the shared read seam every migrated domain
        mixin uses; it fail-fast-raises on a non-success envelope rather than
        coercing.
        """
        if result.get("action_status") not in {"completed", "success", None}:
            raise LedgerRepositoryError(
                f"state-service {context} failed: {result.get('error')!r}",
            )
        data = result.get("data")
        records = data.get("records", []) if isinstance(data, dict) else []
        if not isinstance(records, list):
            return []
        out: list[dict[str, object]] = []
        for row in records:
            if not isinstance(row, dict):
                raise LedgerRepositoryError(
                    f"state-service {context} returned a non-dict row "
                    f"{type(row).__name__!r}; the typed query primitives must "
                    "yield dict records — fail-fast (no silent skip / partial "
                    "result) so an upstream contract violation cannot masquerade "
                    "as an empty/short read",
                )
            out.append(cast(dict[str, object], row))
        return out

    def _query_membership_chunked(
        self,
        table: str,
        *,
        column: str,
        values: list[str],
        extra_filters: dict[str, object] | None = None,
    ) -> list[dict[str, object]]:
        """A ``col = ANY(values)`` read, chunked so each query is bounded.

        D9 shape (2026-08-15): a membership read's result size is bounded by
        the CALLER's list length, not by anything the query states. When that
        list is derived from a whole-table scan it can exceed the state
        service's row cap and the read is refused. Chunking makes every query
        bounded BY CONSTRUCTION — a chunk matches at most its own length — so
        this needs no ``unbounded`` opt-in and holds however large the caller's
        list grows. Chunk size is DERIVED from the cap so the two cannot drift.

        **"A chunk matches at most its own length" holds ONLY for a UNIQUE
        ``column``** (2026-08-16, lane-ak). For a non-unique one it is false and
        the read is still refused: ``__session.external_session_id`` is shared by
        a canonical and every sibling BY DESIGN, and a 100-value chunk of those
        returned 200 rows against the 100-row cap. Chunking bounds the ``= ANY``
        parameter list; it does not bound the RESULT unless one value maps to at
        most one row. A non-unique column needs chunk-AND-page — see
        ``read_support._walk_membership_chunked``.
        """
        if not values:
            return []
        base_filters = dict(extra_filters or {})
        out: list[dict[str, object]] = []
        for start in range(0, len(values), MAX_READ_ROWS):
            chunk = values[start : start + MAX_READ_ROWS]
            out.extend(self._query(table, {**base_filters, column: chunk}))
        return out

    def _query(
        self, table: str, filters: dict[str, object], *, unbounded: bool = False,
    ) -> list[dict[str, object]]:
        """Equality / ``= ANY`` / ``is_null`` / Gap-A range read via ``query_state``.

        ``filters`` is the sanctioned per-column grammar: scalar → ``col = %s``,
        list → ``col = ANY(%s)``, ``{"op": "is_null"|"is_not_null"}``, and the
        Gap-A AND-range comparators ``{"op": "lt"|"lte"|"gt"|"gte", "value": X}``
        → ``col <op> %s``. Callers must include ``is_deleted: 0`` explicitly —
        ``query_state`` (unlike ``query_ordered``) does not inject the soft-delete
        filter.

        ``unbounded=True`` is the sanctioned opt-in to the state service's row
        cap, for a read that deliberately wants every matching row. It is a
        consent, not a bypass: pass it only where the caller genuinely needs the
        complete set and the alternative is a refused read.
        """
        query: dict[str, object] = {"table": table, "filters": filters}
        if unbounded:
            query["unbounded"] = True
        result = self._state.query_state(NAMESPACE, query)
        return self._records_from_result(result, context=table)

    def _query_ordered(
        self,
        table: str,
        *,
        filters: dict[str, object],
        order_by: list[list[str]],
        limit: int,
        after: tuple[object, ...] | None = None,
    ) -> list[dict[str, object]]:
        """Ordered / bounded / tie-safe read via the ``query_ordered`` primitive.

        ``order_by`` is a composite ``[[column, direction], …]`` with ≥ 2 columns
        sharing one direction (the last is the total-order tie-break);
        ``is_deleted = 0`` is applied by the primitive's ``include_deleted``
        default. ``limit`` must be ≤ the primitive's cap (``_MAX_ORDERED_LIMIT``,
        100) — a larger value is **refused** (the Gap-C fail-loud, not silently
        clamped). Every ledger read caps at ≤ 100 and pages longer spans via a
        keyset cursor, so the primitive's ``unbounded`` opt-out is intentionally
        never used here. ``after`` is the primitive's native row-value cursor:
        a tuple matching ``order_by``'s columns in arity and direction, from
        which the next page strictly continues (compiled as one row-value
        comparison, so a composite keyset needs no OR-expansion in the flat
        filter grammar).
        """
        payload: dict[str, object] = {
            "table": table,
            "filters": filters,
            "order_by": order_by,
            "limit": limit,
        }
        if after is not None:
            payload["after"] = list(after)
        result = self._state.query_ordered(NAMESPACE, payload)
        return self._records_from_result(result, context=table)

    def _write(self, table: str, record: dict[str, object]) -> str:
        """INSERT a record via the ``write_state`` primitive; return the row id.

        The provider's INSERT does NOT generate ids (``build_insert_sql``:
        "no id generation; matches insert"), so the caller supplies ``id`` +
        every NOT-NULL column in ``record``; dict/list values are serialized to
        JSONB by the write layer (no caller cast). Fail-fast on a non-success
        envelope or a missing generated_id (same discipline as the read seam).
        """
        normalized = {key: _naive_utc(value) for key, value in record.items()}
        result = self._state.write_state(
            NAMESPACE, {"table": table, "record": normalized},
        )
        if result.get("action_status") not in {"completed", "success", None}:
            raise LedgerRepositoryError(
                f"state-service write {table} failed: {result.get('error')!r}",
            )
        data = result.get("data")
        inner = data.get("result") if isinstance(data, dict) else None
        row_id = inner.get("generated_id") if isinstance(inner, dict) else None
        if not isinstance(row_id, str):
            raise LedgerRepositoryError(
                f"state-service write {table} returned no generated_id "
                f"(got {row_id!r})",
            )
        return row_id

    def _update(
        self,
        table: str,
        filters: dict[str, object],
        updates: dict[str, object],
    ) -> int:
        """UPDATE … WHERE via the ``update_state`` primitive.

        Returns rows-affected — the native compare-and-set signal (a predicated
        ``filters`` that matches 0 rows is a no-op returning 0). Fail-fast on a
        non-success envelope. ``None`` values in ``updates`` bind as SQL NULL.
        """
        normalized_filters = {key: _naive_utc(value) for key, value in filters.items()}
        normalized_updates = {key: _naive_utc(value) for key, value in updates.items()}
        result = self._state.update_state(
            NAMESPACE,
            {"table": table, "filters": normalized_filters},
            normalized_updates,
        )
        if result.get("action_status") not in {"completed", "success", None}:
            raise LedgerRepositoryError(
                f"state-service update {table} failed: {result.get('error')!r}",
            )
        data = result.get("data")
        inner = data.get("result") if isinstance(data, dict) else None
        affected = inner.get("updated") if isinstance(inner, dict) else None
        # Fail-fast (mirroring _write): a REAL integer 0 is the intended CAS miss,
        # but an absent / non-int / bool 'updated' is a malformed envelope (e.g.
        # StateService emits completed+empty mid-transition) and must NOT be
        # silently coerced to 0 — a caller whose compensation only fires on an
        # exception (the vault-first pairing flow) would otherwise mis-commit.
        if not isinstance(affected, int) or isinstance(affected, bool):
            raise LedgerRepositoryError(
                f"state-service update {table} returned no integer 'updated' "
                f"rows-affected (got {affected!r}); absent / non-int means a "
                "malformed envelope, not a compare-and-set miss",
            )
        return affected

    def _delete(
        self,
        table: str,
        filters: dict[str, object],
        *,
        soft: bool = True,
    ) -> int:
        """DELETE … WHERE via the ``delete_records`` primitive (autocommit only).

        ``soft=True`` (default) sets ``is_deleted = 1``; ``soft=False`` issues a
        HARD ``DELETE``. Returns rows-affected. Fail-fast on a non-success
        envelope (same discipline as ``_update``). There is NO transactional
        counterpart — ``StateTransaction`` exposes no delete op — so any delete
        that must be atomic with a sibling write is reasoned about for
        recoverability at the call site (see
        ``overwrite_summary_text_for_codex_stage1``).
        """
        normalized_filters = {key: _naive_utc(value) for key, value in filters.items()}
        result = self._state.delete_records(
            NAMESPACE,
            {"table": table, "filters": normalized_filters, "soft_delete": soft},
        )
        if result.get("action_status") not in {"completed", "success", None}:
            raise LedgerRepositoryError(
                f"state-service delete {table} failed: {result.get('error')!r}",
            )
        data = result.get("data")
        inner = data.get("result") if isinstance(data, dict) else None
        deleted = inner.get("deleted") if isinstance(inner, dict) else None
        if not isinstance(deleted, int) or isinstance(deleted, bool):
            raise LedgerRepositoryError(
                f"state-service delete {table} returned no integer 'deleted' "
                f"rows-affected (got {deleted!r}); absent / non-int means a "
                "malformed envelope",
            )
        return deleted

    def _acquire_lease(
        self,
        table: str,
        filters: dict[str, object],
        *,
        lease_column: str,
        now: datetime,
        set_values: dict[str, object],
    ) -> bool:
        """Atomic expiry-fenced lease-acquire CAS via the ``acquire_lease`` primitive.

        Claims the single row identified by ``filters`` (a scalar ``id`` PK plus
        optional equality guards, e.g. ``is_deleted: 0``) iff ``lease_column``
        IS NULL or is strictly older than ``now`` — the disjunctive availability
        predicate the flat equality / ``= ANY`` / ``is_null`` grammar cannot
        express. Returns True iff the row was claimed. The primitive serializes
        ``set_values`` / ``filters`` / ``now`` via the F1 naive-UTC txn seam, so
        callers pass tz-aware datetimes RAW (NOT via ``_naive_utc``). ``updated_at``
        is left to the BEFORE-UPDATE trigger — do NOT include it in ``set_values``.
        Fail-fast on a non-success envelope or a non-bool ``acquired``.
        """
        result = self._state.acquire_lease(
            NAMESPACE,
            {
                "table": table,
                "filters": filters,
                "lease_column": lease_column,
                "now": now,
                "set": set_values,
            },
        )
        if result.get("action_status") not in {"completed", "success", None}:
            raise LedgerRepositoryError(
                f"state-service acquire_lease {table} failed: {result.get('error')!r}",
            )
        data = result.get("data")
        inner = data.get("result") if isinstance(data, dict) else None
        acquired = inner.get("acquired") if isinstance(inner, dict) else None
        if not isinstance(acquired, bool):
            raise LedgerRepositoryError(
                f"state-service acquire_lease {table} returned no bool 'acquired' "
                f"(got {acquired!r})",
            )
        return acquired

    def _upsert_do_nothing(
        self,
        table: str,
        record: dict[str, object],
        *,
        conflict_columns: list[str],
        conflict_predicate: list[dict[str, object]],
    ) -> bool:
        """Conditional ``INSERT … ON CONFLICT (cols) WHERE <pred> DO NOTHING`` (autocommit).

        Compiles to the partial-unique two-phase upsert's Phase 1 via the
        ``upsert_state`` ``do_nothing`` mode: returns ``True`` iff the row was
        inserted, ``False`` when the partial-unique conflict fired (nothing
        written). The ``conflict_predicate`` is the structured (NOT SQL) AST
        — a list of ``{column, op, value?}`` (``op`` in ``is_null`` /
        ``is_not_null`` / ``eq``) — that MUST mirror the target partial index's
        ``WHERE``; the provider compiles it (named-constraint refs don't work
        because the DDL renderer hash-suffixes partial-index names). Record JSON
        cols pass as native dict/list; timestamps normalize via the F1 naive-UTC
        seam. There is NO transactional counterpart (``StateTransaction`` exposes
        no upsert). Fail-fast on a non-success envelope or a non-bool ``inserted``.
        """
        normalized = {key: _naive_utc(value) for key, value in record.items()}
        result = self._state.upsert_state(
            NAMESPACE,
            {
                "table": table,
                "record": normalized,
                "conflict_columns": conflict_columns,
                "on_conflict": "do_nothing",
                "conflict_predicate": conflict_predicate,
            },
        )
        if result.get("action_status") not in {"completed", "success", None}:
            raise LedgerRepositoryError(
                f"state-service upsert {table} failed: {result.get('error')!r}",
            )
        data = result.get("data")
        inner = data.get("result") if isinstance(data, dict) else None
        inserted = inner.get("inserted") if isinstance(inner, dict) else None
        if not isinstance(inserted, bool):
            raise LedgerRepositoryError(
                f"state-service upsert {table} returned no bool 'inserted' "
                f"(got {inserted!r})",
            )
        return inserted

    # ------------------------------------------------------------------
    # The raw-SQL ``_fetch_all`` positional-row adapter was retired here
    # (SQL-lockdown): the last consumer, ``list_canonical_contributors``,
    # migrated onto the typed ``_query`` read seam, closing the ledger's
    # raw-SQL read surface. The ``shared._columns_from_select_sql`` /
    # ``_split_select_pieces`` SELECT-column parser it depended on was
    # removed with it.
    # ------------------------------------------------------------------
