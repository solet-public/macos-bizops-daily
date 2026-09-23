from abc import ABC, abstractmethod
from collections.abc import Sequence
from contextlib import AbstractContextManager
from typing import ClassVar

from ananta.core.domain.types import ActionResult


class StateTransaction(ABC):
    """Handle for an in-progress state-service transaction.

    Yielded by :meth:`StateManagementInterface.transactional`.  All
    statements executed against the same handle commit or roll back
    together when the surrounding context manager exits.

    Reads return ``dict[str, object]`` rows so callers don't depend on a
    specific DB driver's row factory.
    """

    @abstractmethod
    def execute(
        self, sql: str, params: Sequence[object] | None = None,
    ) -> None:
        """Run a write/DDL statement.  No return value."""
        ...

    @abstractmethod
    def executemany(
        self, sql: str, params_seq: Sequence[Sequence[object]],
    ) -> None:
        """Run a write statement once per parameter tuple."""
        ...

    @abstractmethod
    def fetch_one(
        self, sql: str, params: Sequence[object] | None = None,
    ) -> dict[str, object] | None:
        """Run a SELECT (or ``UPDATE … RETURNING``) and return one row."""
        ...

    @abstractmethod
    def fetch_all(
        self, sql: str, params: Sequence[object] | None = None,
    ) -> list[dict[str, object]]:
        """Run a SELECT and return every row."""
        ...

    # Typed, non-SQL ops — mirror the top-level StateManagementInterface method
    # shapes 1:1, but execute on the open txn connection and return plain values
    # (NOT ActionResult) and RAISE on failure so a failed step rolls the whole
    # transaction back rather than riding through as a silent partial commit.

    @abstractmethod
    def write_state(self, namespace: str, data: dict[str, object]) -> str:
        """INSERT within the txn. ``data = {table, record}``. Returns the row id.

        Raises on failure (→ the surrounding context manager rolls back).
        """
        ...

    @abstractmethod
    def update_state(
        self, namespace: str, query: dict[str, object], updates: dict[str, object]
    ) -> int:
        """UPDATE … WHERE within the txn. ``query = {table, filters}``.

        Returns rows-affected (the compare-and-set signal). Raises on failure.
        """
        ...

    @abstractmethod
    def query_state(
        self, namespace: str, filters: dict[str, object]
    ) -> list[dict[str, object]]:
        """Non-locking SELECT within the txn, ROW-BOUNDED.

        ``filters = {table, filters, limit?, unbounded?}``. Returns the rows.
        Raises on failure (→ the surrounding context manager rolls back).

        NOT deprecated, unlike the autocommit ``query_state`` alias: this
        interface exposes no ``read_state``, so within a transaction this is the
        only row-returning read primitive and has no bounded alternative to
        defer to. It therefore carries the bound itself — same
        ``MAX_READ_ROWS`` policy as the autocommit ``read_state`` (see
        ``ananta.services.state_service.read_bounds``): an explicit ``limit`` is
        honoured, over the cap without ``unbounded=True`` is refused before any
        SQL runs, and with no limit an over-cap result raises ``ReadBoundError``
        rather than returning a silent prefix.
        """
        ...

    @abstractmethod
    def increment_and_return(self, namespace: str, data: dict[str, object]) -> int:
        """Atomic self-referential increment with RETURNING — the cursor allocator.

        ``data = {table, filters, column, by (default 1)}``. Compiles to
        ``UPDATE {table} SET {column}={column}+{by} WHERE {filters} RETURNING
        {column}``; the UPDATE takes a row lock held to commit (concurrent
        allocators serialize). Returns the post-increment value. Raises if 0
        rows matched. Deliberately narrow — ``{column, by}`` only.
        """
        ...

    @abstractmethod
    def delete_records(self, namespace: str, query: dict[str, object]) -> int:
        """DELETE within the txn. ``query = {table, filters, soft_delete?}``.

        Mirrors the autocommit ``delete_records`` semantics: ``soft_delete``
        defaults to ``True`` (sets ``is_deleted = 1``); ``soft_delete=False``
        hard-deletes the matched rows. Same-namespace only. Returns
        rows-affected. Raises on failure (→ the surrounding context manager
        rolls back).
        """
        ...

    @abstractmethod
    def count(self, namespace: str, data: dict[str, object]) -> int:
        """Aggregate row COUNT within the txn. ``data = {table, filters}``.

        Returns the count (``>= 0``; empty set → ``0``). Same filter grammar
        as :meth:`query_state` — NO auto ``is_deleted`` exclusion (the caller
        passes it). Raises on failure (rolls back).
        """
        ...

    @abstractmethod
    def max_value(self, namespace: str, data: dict[str, object]) -> object:
        """Aggregate MAX within the txn. ``data = {table, column, filters}``.

        Returns the column's max as a RAW scalar (the same type-fidelity as a
        :meth:`query_state` cell — a ``TIMESTAMP`` column yields a NAIVE
        datetime, the F1 seam; the caller normalizes), or ``None`` over an
        empty set. NO auto ``is_deleted`` exclusion. Raises on failure
        (rolls back).
        """
        ...

    @abstractmethod
    def min_value(self, namespace: str, data: dict[str, object]) -> object:
        """Aggregate MIN within the txn. ``data = {table, column, filters}``.

        Returns the column's min as a RAW scalar (``None`` over an empty set);
        same type-fidelity, F1 TZ seam, and no-auto-``is_deleted``-exclusion
        contract as :meth:`max_value`. Raises on failure (rolls back).
        """
        ...


class StateManagementInterface(ABC):
    """Complete contract for state management plugins."""

    INTERFACE_VERSION: ClassVar[str] = "2.1.0"

    # Bootstrap mode flag - when True, schema creation is deferred to avoid circular dependencies
    bootstrap_mode: bool = False

    @abstractmethod
    def create_schema(self, namespace: str, schema: dict[str, object]) -> ActionResult: ...

    @abstractmethod
    def read_state(self, namespace: str, query: dict[str, object]) -> ActionResult: ...

    @abstractmethod
    def write_state(self, namespace: str, data: dict[str, object]) -> ActionResult: ...

    @abstractmethod
    def update_state(
        self, namespace: str, query: dict[str, object], updates: dict[str, object]
    ) -> ActionResult: ...

    @abstractmethod
    def upsert_state(self, namespace: str, data: dict[str, object]) -> ActionResult:
        """Insert or update a record based on conflict columns.

        Two conflict-resolution modes, selected by ``data``:

        **DO UPDATE (default).** If a record with matching conflict columns
        exists, updates it; otherwise inserts. ``data`` must contain:
            - table: Target table name
            - record: Record data to insert/update
            - conflict_columns: Columns checked for conflicts (e.g., ["id"])
        Returns ``ActionResult`` whose ``result`` is
        ``{"generated_id": <id>, "upserted": 1}``.

        **DO NOTHING (partial ``ON CONFLICT`` predicate).** When
        ``on_conflict="do_nothing"`` (optionally with ``conflict_predicate``)
        is present, the insert is skipped on conflict rather than updated. This
        supports partial-unique indexes whose ``WHERE`` predicate is
        load-bearing. ``data`` adds:
            - on_conflict: ``"do_nothing"``
            - conflict_predicate: optional structured (NOT SQL) AST — a list of
              ``{"column", "op", "value"?}`` with ``op`` in
              ``{"is_null", "is_not_null", "eq"}``. It is compiled to the
              ``ON CONFLICT (...) WHERE <predicate>`` clause and MUST match the
              target partial index's predicate (``eq`` is constant-folded so it
              matches a literal index predicate at plan time).
        Returns ``ActionResult`` whose ``result`` is
        ``{"inserted": bool, "id": str | None}`` — ``inserted`` is True with the
        new id on insert, False with ``id=None`` when the conflict was skipped.

        Args:
            namespace: Namespace identifier
            data: Mode-dependent keys as described above.

        Returns:
            ActionResult; ``result`` shape depends on the mode (see above).
        """
        ...

    @abstractmethod
    def acquire_lease(self, namespace: str, data: dict[str, object]) -> ActionResult:
        """Atomically acquire an expiry-fenced lease on a single row (CAS).

        The one sanctioned primitive for the disjunctive "claim if free OR
        expired" predicate the flat equality / ``= ANY`` / ``is_null`` filter
        grammar cannot express. Compiles to ONE atomic statement::

            UPDATE {table} SET {set} WHERE {filters}
              AND ({lease_column} IS NULL OR {lease_column} < {now})
            RETURNING id

        so the row lock, the free-or-expired check, and the write all happen
        together — no read-then-write TOCTOU (the failure mode a token-fenced
        lease re-opens). SQL composition is confined to the provider.

        ``data`` keys:
            - ``table``: target table (identifier-safe)
            - ``filters``: MUST identify a SINGLE row by a scalar primary-key
              (``id``) equality predicate, optionally narrowed by additional
              equality guards (e.g. ``{"id": source_id, "is_deleted": 0}``). A
              filter with no scalar ``id`` (broad / empty), a list-valued
              ``id`` (``= ANY``), or an op-valued ``id`` is rejected with an
              error result before any write — a lease is an identity-targeted
              single-resource claim, never claim-all-matching.
            - ``lease_column``: the expiry column; the row is claimable iff it
              ``IS NULL`` or is strictly older than ``now``
            - ``now``: the freshness threshold (a ``datetime`` — the injected
              clock). Bound the same naive-UTC way the stored expiry was
              written (the F1 TZ-storage seam) so the ``< now`` comparison is
              correct against stored values.
            - ``set``: columns written on a successful claim (e.g. the new
              expiry window + a fresh fence token). ``updated_at`` is left to
              the BEFORE-UPDATE trigger.

        Returns:
            ActionResult whose ``result`` is ``{"acquired": bool}`` — ``True``
            when this caller claimed the lease, ``False`` when a live owner
            still holds it.
        """
        ...

    @abstractmethod
    def count(self, namespace: str, data: dict[str, object]) -> ActionResult:
        """Count rows in a filtered set — the SQL aggregate runs INSIDE the
        owner plugin (index-backed) and returns the SCALAR; no rows shipped.

        ``data`` keys:
            - ``table``: target table (identifier-safe)
            - ``filters``: same grammar as :meth:`query_state` (equality /
              ``= ANY`` / ``{"op": "is_null"|"is_not_null"}`` / the Gap-A
              range ops). NO auto ``is_deleted`` exclusion (mirrors
              ``query_state``, NOT ``query_ordered``) — a caller wanting
              live-only rows passes ``is_deleted=0`` itself; assuming
              auto-exclude yields a silently-wrong count. A ``column`` key is
              REJECTED (count takes no column).

        Returns:
            ActionResult whose ``result`` is ``{"value": <int >= 0>}`` — the
            scalar lives at ``data.result.value``. Empty set → ``0``.
        """
        ...

    @abstractmethod
    def max_value(self, namespace: str, data: dict[str, object]) -> ActionResult:
        """Largest value of a column over a filtered set — SQL ``MAX`` inside
        the owner plugin; returns the SCALAR, no rows shipped.

        ``data`` keys: ``table``, ``column`` (REQUIRED — the column to
        aggregate, identifier-safe), and ``filters`` (same grammar + same
        NO-auto-``is_deleted``-exclusion contract as :meth:`count`).

        F1 TZ seam (path-dependent, matching :meth:`query_state`): on THIS
        autocommit surface a ``MAX`` over a ``TIMESTAMP`` column is serialized
        to an ISO-8601 string (the autocommit ``query_state`` fidelity, so the
        envelope is JSON-safe at the bridge boundary). The TYPED-TXN
        :meth:`StateTransaction.max_value` instead returns the RAW naive
        datetime — the seam the in-txn summarize-MAX consumer normalizes.

        Returns:
            ActionResult whose ``result`` is
            ``{"value": <column-typed scalar | None>}`` (at
            ``data.result.value``; a datetime as an ISO-8601 string). Empty set
            → ``None`` (SQL ``NULL``); never a fabricated ``0``.
        """
        ...

    @abstractmethod
    def min_value(self, namespace: str, data: dict[str, object]) -> ActionResult:
        """Smallest value of a column over a filtered set — SQL ``MIN`` inside
        the owner plugin; returns the SCALAR. Same ``data`` keys, filter
        grammar, F1 TZ seam, and ``None``-over-empty contract as
        :meth:`max_value`.
        """
        ...

    @abstractmethod
    def delete_records(self, namespace: str, query: dict[str, object]) -> ActionResult: ...

    @abstractmethod
    def query_state(self, namespace: str, filters: dict[str, object]) -> ActionResult:
        """DEPRECATED alias for :meth:`read_state` — prefer ``read_state``.

        ``filters`` is the whole ``{table, filters, limit?, unbounded?}`` query
        envelope, forwarded unchanged to ``read_state``; ``limit`` and
        ``unbounded`` are honoured, and the ``MAX_READ_ROWS`` bound applies.
        Use :meth:`read_state` for a complete filtered set, or
        :meth:`query_ordered` for a page.
        """
        ...

    @abstractmethod
    def query_ordered(self, namespace: str, data: dict[str, object]) -> ActionResult:
        """Ordered, bounded, tie-safe query (the one sanctioned widening).

        The single approved superset of :meth:`query_state`: equality
        ``filters`` plus a deterministic composite ``order_by``, a capped
        ``limit``, and an optional tie-safe ``after`` cursor. Lets callers
        get genuine top-N / oldest-first / recent-N pages over the state
        interface without reaching for raw SQL — the SQL composition is
        confined to the provider implementations.

        ``data`` keys:
            - ``table``: target table (identifier-validated)
            - ``filters``: per-column match dict (same grammar as
              ``query_state``): scalar → ``col = %s``; list → ``col = ANY``;
              ``{"op": "is_null"|"is_not_null"}`` → NULL test; and the Gap-A
              AND-range op ``{"op": "lt"|"lte"|"gt"|"gte", "value": X}`` →
              ``col <op> %s`` (a half-open range, e.g. ``called_at >= since``)
            - ``order_by``: composite ``[(column, dir), …]`` with a single
              shared ``dir ∈ {asc, desc}`` and ≥ 2 columns (the last is the
              total-order tie-break)
            - ``limit``: max rows. A request at or under
              ``_MAX_ORDERED_LIMIT`` is used as-is; a request OVER the cap is
              REFUSED (``OrderedQueryError``) unless ``unbounded=True`` is
              passed — silent clamp-to-cap is gone (Gap-C). Floors at 1.
            - ``unbounded`` (optional, default ``False``): opt into a page
              larger than ``_MAX_ORDERED_LIMIT``; the caller owns the
              larger-scan cost
            - ``after`` (optional): direction-matched row-value cursor
              tuple ``(v1, v2, …)`` of the same arity as ``order_by``
            - ``include_deleted`` (optional, default ``False``): when
              ``False``, ``is_deleted`` rows are excluded

        Returns:
            ActionResult with ``data.records`` (the ordered page).
        """
        ...

    @abstractmethod
    def set_key_value(
        self, namespace: str, key: str, value: object, scope: str = "GLOBAL", ttl: int | None = None
    ) -> ActionResult: ...

    @abstractmethod
    def get_key_value(self, namespace: str, key: str, scope: str = "GLOBAL") -> ActionResult: ...

    @abstractmethod
    def delete_key_value(self, namespace: str, key: str, scope: str = "GLOBAL") -> ActionResult: ...

    @abstractmethod
    def clear_key_values(
        self, namespace: str | None = None, scope: str | None = None
    ) -> ActionResult: ...

    @abstractmethod
    def list_key_values(
        self,
        namespace: str | None = None,
        scope: str | None = None,
        pattern: str | None = None,
    ) -> ActionResult: ...

    @abstractmethod
    def execute_sql(self, sql_query: str, sql_params: list[object] | None = None) -> ActionResult:
        """Execute direct SQL query.

        CRITICAL: Required by ActionQueuePoller for queue management.
        """
        ...

    @abstractmethod
    def transactional(self) -> AbstractContextManager[StateTransaction]:
        """Open an atomic multi-statement transaction.

        Yields a :class:`StateTransaction` whose ``execute`` /
        ``executemany`` / ``fetch_one`` / ``fetch_all`` calls all commit
        or roll back together when the context exits (commit on clean
        exit, rollback on exception).

        Use for any operation that needs read-modify-write atomicity —
        the autocommit :meth:`execute_sql` API commits each call
        independently and is unsafe for ``UPDATE … RETURNING`` followed
        by a dependent ``INSERT``.
        """
        ...

    @abstractmethod
    def describe_schema(self, namespace: str) -> ActionResult:
        """Get schema definition for namespace.

        Returns table and column information.
        """
        ...

    @abstractmethod
    def list_namespaces(self) -> ActionResult:
        """List all available namespaces.

        Namespaces are derived from table name prefixes.
        """
        ...

    @abstractmethod
    def mark_as_read(self, namespace: str, query: dict[str, object]) -> ActionResult:
        """Mark records as read/processed.

        Updates records matching query to set read_at timestamp.
        """
        ...

    @abstractmethod
    def initialize_database(self, config: dict[str, object] | None = None) -> ActionResult:
        """Initialize database (Phase 3 Database Operations).

        Called during system startup to ensure database is ready.
        Must be idempotent - safe to call multiple times.
        """
        ...

    @abstractmethod
    def is_ready(self) -> bool:
        """Check if the state management implementation is ready for use."""
        ...

    @abstractmethod
    def get_readiness_error(self) -> str | None:
        """Get the error message if not ready, None if ready."""
        ...
