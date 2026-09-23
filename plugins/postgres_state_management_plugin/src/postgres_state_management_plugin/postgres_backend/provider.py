"""
PostgreSQL Provider

PostgreSQL database provider implementing state management operations with
connection pooling, schema management, and CRUD operations.
"""

import logging
from collections.abc import Callable, Generator, Iterable, Sequence
from contextlib import contextmanager
from datetime import UTC, date, datetime
from typing import Any, cast
from uuid import uuid4

import psycopg
from ananta.interfaces.state_provider_interface import (
    ActionExecutionRecord,
    StateProviderInterface,
)
from ananta.types.column_types import ColumnType
from ananta.types.schema_types import ColumnDefinition, SchemaDefinition
from psycopg import sql
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from .config import PostgresConfig
from .utils import build_table_name, get_postgres_type

logger = logging.getLogger(__name__)


# DDL for ``platform__plugin_schema_ownership`` — the lifecycle's own metadata
# table. Lives here (provider) rather than in the plugin so the lifecycle
# substrate has a single owner. Caller is ``bootstrap_for_lifecycle``.
_OWNERSHIP_TABLE_DDL = """
    CREATE TABLE IF NOT EXISTS platform__plugin_schema_ownership (
        plugin_namespace TEXT NOT NULL,
        table_name TEXT NOT NULL,
        schema_snapshot_json JSONB NOT NULL,
        status TEXT NOT NULL DEFAULT 'active'
            CHECK (status IN ('active', 'inactive')),
        installed_at TIMESTAMP NOT NULL DEFAULT (NOW() AT TIME ZONE 'UTC'),
        updated_at TIMESTAMP NOT NULL DEFAULT (NOW() AT TIME ZONE 'UTC'),
        uninstalled_at TIMESTAMP,
        PRIMARY KEY (plugin_namespace, table_name)
    )
"""


def _serialize_for_json(value: Any) -> Any:
    """
    Convert datetime objects to ISO format strings for JSON serialization.

    Args:
        value: Value to serialize

    Returns:
        JSON-serializable value
    """
    if isinstance(value, datetime | date):
        return value.isoformat()
    elif isinstance(value, dict):
        return {k: _serialize_for_json(v) for k, v in value.items()}
    elif isinstance(value, list | tuple):
        return [_serialize_for_json(item) for item in value]
    else:
        return value


def _strip_nul_chars(value: Any) -> Any:
    """Recursively strip actual NUL (U+0000) chars from str keys/values.

    PostgreSQL TEXT/JSONB cannot store U+0000. The NUL CHARACTER is removed
    here, BEFORE json.dumps, so the serialized JSON never emits a U+0000
    escape (which JSONB rejects). This MUST NOT be done by textually
    mutating json.dumps' output: that cannot distinguish an embedded NUL
    byte from a legitimate string literally containing the six characters
    backslash-u-0-0-0-0, and would corrupt the latter into invalid JSON
    (Codex 2026-06-20 BLOCKER).
    """
    if isinstance(value, str):
        return value.replace("\x00", "")
    if isinstance(value, dict):
        return {
            _strip_nul_chars(k): _strip_nul_chars(v) for k, v in value.items()
        }
    if isinstance(value, list):
        return [_strip_nul_chars(item) for item in value]
    if isinstance(value, tuple):
        return tuple(_strip_nul_chars(item) for item in value)
    return value


def _serialize_value_for_sql(value: Any) -> Any:
    """
    Serialize Python values for SQL insertion.

    Handles complex types (dict, list) by converting to JSON strings,
    ensuring database-agnostic callers don't need to pre-serialize.

    Args:
        value: Value to serialize for SQL

    Returns:
        SQL-compatible value (strings, numbers, None, or JSON string for complex types)
    """
    import json

    if value is None:
        return None
    elif isinstance(value, str):
        # PostgreSQL TEXT columns cannot contain NUL (0x00) bytes.
        # Parallel to the session_ledger repository-seam strip (merge 35bc88ac).
        return value.replace("\x00", "") if "\x00" in value else value
    elif isinstance(value, int | float | bool):
        return value
    elif isinstance(value, datetime | date):
        return value.isoformat()
    elif isinstance(value, dict | list):
        # Strip actual NUL (U+0000) chars from nested strings/keys BEFORE
        # json.dumps via _strip_nul_chars (JSONB rejects U+0000). NEVER
        # textually mutate json.dumps output: it cannot tell an embedded NUL
        # byte from a legitimate string literally containing the six chars
        # backslash-u-0-0-0-0, and corrupts the latter (Codex 2026-06-20).
        return json.dumps(_serialize_for_json(_strip_nul_chars(value)))
    else:
        rendered = str(value)
        return rendered.replace("\x00", "") if "\x00" in rendered else rendered


def serialize_value_for_txn(value: Any) -> Any:
    """Serialize a value param for the typed ``StateTransaction`` ops.

    Mirrors ``_serialize_value_for_sql`` for dict/list (JSON) and scalars, but a
    **tz-aware** ``datetime`` is normalized to naive UTC (the 2026-06-12 F1
    TZ-storage seam) rather than ISO-formatted to a string — so the stored
    wall-clock is UTC, matching the raw-SQL txn path's ``_strip_tz_from_params``
    and NOT the autocommit path's literal-wall-clock isoformat. Naive datetimes
    pass through unchanged.
    """
    if isinstance(value, datetime):
        if value.tzinfo is not None:
            return value.astimezone(UTC).replace(tzinfo=None)
        return value
    return _serialize_value_for_sql(value)


# Sanctioned AND-range comparison operators for the ``_build_filter_clauses``
# grammar (Gap-A). Keys are the structured filter ops a caller passes
# (``{"op": "gte", "value": X}``); values are pre-built ``sql.SQL`` operator
# literals — a FIXED dict from string LITERALS, never built from caller input,
# so they are injection-safe. Mirrors the rds twin byte-for-byte.
_COMPARISON_OPERATORS = {
    "lt": sql.SQL("<"),
    "lte": sql.SQL("<="),
    "gt": sql.SQL(">"),
    "gte": sql.SQL(">="),
}


def _build_dict_op_clause(
    ident: sql.Identifier,
    col: str,
    val: dict[str, Any],
    ser: Callable[[Any], Any],
) -> tuple[sql.Composable, list[Any]]:
    """Compile one ``{"op": ...}`` filter entry to ``(clause, params)``.

    Split out of ``_build_filter_clauses`` so that method stays rank-A: the
    dict-op grammar (``is_null`` / ``is_not_null`` / the Gap-A AND-range
    ``lt`` / ``lte`` / ``gt`` / ``gte``) lives here. Operators are FIXED-dict
    ``sql.SQL`` literals (injection-safe); a comparison ``value`` binds through
    the caller's ``ser`` so it matches the write-path serialization. Fails loud
    on a missing comparison ``value`` or an unknown op.
    """
    op = val.get("op")
    if op == "is_null":
        return sql.SQL("{} IS NULL").format(ident), []
    if op == "is_not_null":
        return sql.SQL("{} IS NOT NULL").format(ident), []
    if op in _COMPARISON_OPERATORS:
        if "value" not in val:
            raise ValueError(
                f"filter op {op!r} for column {col!r} requires a 'value'"
            )
        clause = sql.SQL("{} {} {}").format(
            ident, _COMPARISON_OPERATORS[op], sql.Placeholder(),
        )
        return clause, [ser(val["value"])]
    raise ValueError(
        f"unsupported filter op {op!r} for column {col!r}; expected "
        "'is_null', 'is_not_null', 'lt', 'lte', 'gt', or 'gte'"
    )


# Sanctioned single-scalar aggregate keywords for ``build_aggregate_query``.
# Keys are the structured ops a caller passes; values are pre-built ``sql.SQL``
# keyword literals — a FIXED dict from string LITERALS, never built from caller
# input, so they are injection-safe. ``count`` is handled separately (COUNT(*),
# no column). Extends to ``sum``/``avg`` by adding an entry. Mirrors the rds
# twin byte-for-byte.
_AGGREGATE_KEYWORDS = {
    "max": sql.SQL("MAX"),
    "min": sql.SQL("MIN"),
}


def _build_aggregate_expr(op: str, column: str | None) -> sql.Composable:
    """Select the aggregate SQL expression from the closed op-map.

    Owns the op/column fail-fast contract so ``build_aggregate_query`` stays
    rank-A: ``count`` → ``COUNT(*)`` and REJECTS a ``column``; ``max``/``min``
    → ``MAX``/``MIN`` of a REQUIRED ``column`` (rendered via ``sql.Identifier``).
    The keyword is a compile-time ``sql.SQL`` literal from the FIXED map — never
    caller text. Raises ``ValueError`` on an unknown op or an op/column
    mismatch.
    """
    if op == "count":
        if column is not None:
            raise ValueError("count does not accept a 'column'")
        return sql.SQL("COUNT(*)")
    keyword = _AGGREGATE_KEYWORDS.get(op)
    if keyword is None:
        raise ValueError(
            f"unsupported aggregate op {op!r}; expected 'count', 'max', or 'min'"
        )
    if not isinstance(column, str) or not column:
        raise ValueError(f"{op} aggregate requires a non-empty 'column'")
    return sql.SQL("{}({})").format(keyword, sql.Identifier(column))


class PostgresProvider(StateProviderInterface):
    """
    PostgreSQL database provider with connection pooling.

    Features:
    - Connection pooling via psycopg ConnectionPool
    - Schema isolation (all tables in configured schema)
    - Parameterized queries for SQL injection prevention
    - Auto-ID generation with table prefixes
    - Auto-timestamp on create and update via triggers
    - Transaction management with context managers
    """

    def __init__(
        self,
        config: PostgresConfig,
        pool_builder: Callable[..., ConnectionPool[Any]] | None = None,
    ) -> None:
        """Initialize PostgreSQL provider.

        Args:
            config: PostgreSQL connection and pool configuration.
            pool_builder: Optional factory that receives *config* and returns a
                ready ``ConnectionPool``.  When provided, ``initialize()`` calls
                this factory instead of building a pool from the local conninfo
                string.  RDS plugins pass ``make_rds_state_pool`` here; local
                plugins leave this ``None``.
        """
        self.config = config
        self._pool: ConnectionPool[Any] | None = None
        self._initialized = False
        self._pool_builder = pool_builder
        # Mapping of full_table_name -> id_prefix for ID generation
        self._table_id_prefixes: dict[str, str] = {}

        logger.debug(
            f"PostgresProvider created (not yet initialized) with host={config.host}, "
            f"port={config.port}, database={config.database}, pg_schema={config.pg_schema}, "
            f"_initialized={self._initialized}"
        )

    def _get_connection_string(self) -> str:
        """
        Build PostgreSQL connection string.

        Returns:
            Connection string for psycopg
        """
        # Set search_path to include the configured schema so unqualified table references work
        search_path = f"{self.config.schema_name},public"
        return (
            f"host={self.config.host} "
            f"port={self.config.port} "
            f"dbname={self.config.database} "
            f"user={self.config.user} "
            f"password={self.config.password} "
            f"connect_timeout={self.config.connection_timeout} "
            f"options=-csearch_path={search_path}"
        )

    def initialize(self) -> None:
        """
        Initialize connection pool and create schema.

        Raises:
            psycopg.OperationalError: If connection fails
        """
        logger.debug(
            f"PostgresProvider.initialize() called, current _initialized={self._initialized}"
        )
        if self._initialized:
            return

        try:
            # Create connection pool — via injected builder (RDS) or local conninfo
            try:
                if self._pool_builder is not None:
                    logger.debug("Building connection pool via injected pool_builder")
                    self._pool = self._pool_builder(self.config)
                else:
                    conninfo = self._get_connection_string()
                    logger.debug(f"Attempting to create connection pool with: {conninfo[:50]}...")
                    self._pool = ConnectionPool(
                        conninfo=conninfo,
                        min_size=1,
                        max_size=self.config.pool_size,
                        timeout=self.config.connection_timeout,
                    )

                logger.debug(f"Connection pool created with size {self.config.pool_size}")

                # Mark as initialized BEFORE creating schema
                # (schema creation needs get_connection())
                self._initialized = True
                logger.debug("PostgresProvider marked as initialized before schema operations")

                # Create schema if not exists
                self._create_schema()

                # Create updated_at trigger function
                self._create_trigger_function()

                logger.debug("PostgresProvider initialization complete")

            except (psycopg.OperationalError, OSError) as conn_error:
                # FAIL FAST: Database connection is REQUIRED for platform operation
                error_msg = (
                    f"FATAL: Cannot connect to PostgreSQL database.\n"
                    f"  Host: {self.config.host}\n"
                    f"  Port: {self.config.port}\n"
                    f"  Database: {self.config.database}\n"
                    f"  Error: {conn_error}\n"
                    f"\n"
                    f"The Ananta platform CANNOT function without database access.\n"
                    f"Please verify:\n"
                    f"  1. PostgreSQL is running\n"
                    f"  2. Connection details are correct in config\n"
                    f"  3. Database exists and is accessible\n"
                    f"  4. User has necessary permissions\n"
                    f"\n"
                    f"Fix the issue and restart the platform."
                )
                logger.critical(error_msg)

                # FAIL FAST AND LOUDLY - DO NOT SUPPRESS
                raise RuntimeError(error_msg) from conn_error

        except Exception as e:
            logger.error(f"Failed to initialize PostgresProvider: {type(e).__name__}: {e}")
            raise

    def _create_schema(self) -> None:
        """Create schema if not exists."""
        with self.get_connection() as conn, conn.cursor() as cur:
            cur.execute(
                sql.SQL("CREATE SCHEMA IF NOT EXISTS {}").format(
                    sql.Identifier(self.config.schema_name)
                )
            )
            # Autocommit enabled - no explicit commit needed
            logger.debug(f"Schema '{self.config.schema_name}' ensured")

    def _create_trigger_function(self) -> None:
        """
        Create trigger function for auto-updating updated_at column.

        This function is applied to all tables with an updated_at column.
        """
        with self.get_connection() as conn, conn.cursor() as cur:
            # Create trigger function
            cur.execute(
                sql.SQL("""
                        CREATE OR REPLACE FUNCTION {}.update_updated_at_column()
                        RETURNS TRIGGER AS $$
                        BEGIN
                            NEW.updated_at = (NOW() AT TIME ZONE 'UTC');
                            RETURN NEW;
                        END;
                        $$ LANGUAGE plpgsql;
                    """).format(sql.Identifier(self.config.schema_name))
            )
            # Autocommit enabled - no explicit commit needed
            logger.debug("Trigger function for updated_at created")

    @contextmanager
    def get_connection(self) -> Generator[psycopg.Connection[Any]]:
        """
        Get database connection from pool.

        Yields:
            Database connection with dict_row factory and autocommit enabled

        Raises:
            RuntimeError: If provider not initialized or pool not available
        """
        if not self._initialized:
            raise RuntimeError("PostgresProvider not initialized. Call initialize() first.")

        if not self._pool:
            raise RuntimeError(
                "PostgreSQL connection pool not available. PostgreSQL server may not be running."
            )

        conn = self._pool.getconn()
        try:
            # CRITICAL FIX: Enable autocommit for immediate transaction visibility
            # This ensures all writes are immediately visible to other connections
            # matching SQLite's default behavior and preventing race conditions
            # in action queue polling and conversation history reads
            conn.autocommit = True

            # Set row factory to return dicts
            conn.row_factory = dict_row  # type: ignore[assignment]
            yield conn
        finally:
            self._pool.putconn(conn)

    @contextmanager
    def get_transactional_connection(self) -> Generator[psycopg.Connection[Any]]:
        """
        Get a database connection with autocommit DISABLED for atomic multi-statement work.

        Use this for the schema-lifecycle DDL+ownership-row apply path: every emitted
        DDL op plus its ownership-table updates must commit together or roll back together.
        On exit, commits if no exception was raised; rolls back otherwise.

        Yields:
            Database connection with autocommit=False and dict_row factory.

        Raises:
            RuntimeError: If provider not initialized or pool not available.
        """
        if not self._initialized:
            raise RuntimeError("PostgresProvider not initialized. Call initialize() first.")

        if not self._pool:
            raise RuntimeError(
                "PostgreSQL connection pool not available. PostgreSQL server may not be running."
            )

        conn = self._pool.getconn()
        try:
            conn.autocommit = False
            conn.row_factory = dict_row  # type: ignore[assignment]
            try:
                yield conn
                conn.commit()
            except Exception:
                conn.rollback()
                raise
        finally:
            self._pool.putconn(conn)

    def get_supported_contracts(self) -> set[str]:
        """
        Get supported provider contracts.

        Returns:
            Set of supported contract names
        """
        return {
            "auto_id_generation",
            "auto_timestamp_on_create",
            "auto_timestamp_on_update",
        }

    def generate_id(self, table_prefix: str) -> str:
        """
        Generate unique ID with table prefix.

        Pattern: {table_prefix}_{uuid4()}

        Args:
            table_prefix: Prefix for the ID

        Returns:
            Generated ID string
        """
        return f"{table_prefix}_{uuid4()}"

    def create_table(
        self,
        namespace: str,
        table: str,
        columns: dict[str, ColumnType | str | ColumnDefinition],
        table_prefix: str = "rec",
    ) -> None:
        """Create table with column definitions."""
        full_table_name = build_table_name(namespace, table)
        col_parts = self._build_column_definitions(columns, table)

        create_sql = sql.SQL("CREATE TABLE IF NOT EXISTS {} ({})").format(
            sql.Identifier(self.config.schema_name, full_table_name), sql.SQL(", ").join(col_parts)
        )

        with self.get_connection() as conn, conn.cursor() as cur:
            cur.execute(create_sql)
            if "updated_at" in columns:
                self._create_updated_at_trigger(cur, full_table_name)
            # Store id_prefix for this table for use in upsert operations
            self._table_id_prefixes[full_table_name] = table_prefix
            logger.debug(f"Table {self.config.schema_name}.{full_table_name} created successfully")

    def apply_schema_change_ops(
        self,
        cursor: psycopg.Cursor[Any],
        declared: SchemaDefinition,
        ops: Iterable[sql.Composed],
    ) -> None:
        """Single chokepoint for executing CREATE/ALTER-shape DDL.

        Any caller that wants to apply rendered DDL to the database goes
        through this method (or the purge sibling below). Executing
        ``sql.Composed`` ops directly via ``cursor.execute(op)`` outside the
        provider is an architectural violation: it bypasses the provider's
        bookkeeping (``_table_id_prefixes``) and produces caches that drift
        from on-disk reality. The 2026-05-31 incident traced to exactly that
        gap — the plugin-schema lifecycle was emitting ``CREATE TABLE`` for
        ``core__process_registry`` via the cursor, the prefix cache stayed
        empty, and the first registry upsert at boot fell over.

        Contract: caller owns the transactional connection and threads in
        its cursor; provider executes ops and updates the prefix cache.
        Bookkeeping happens AFTER all ops succeed, so a mid-op failure that
        rolls back the transaction never leaves the cache out of sync.

        ``declared`` carries the source of truth for ``id_prefix`` per
        table. Tables whose ``id_prefix`` is ``None`` are skipped —
        consistent with ``create_table()``'s explicit-prefix-required
        semantics.
        """
        for op in ops:
            cursor.execute(op)
        for table_name, table_schema in declared.tables.items():
            if table_schema.id_prefix is None:
                continue
            full_table_name = build_table_name(declared.namespace, table_name)
            self._table_id_prefixes[full_table_name] = table_schema.id_prefix

    def apply_schema_purge_ops(
        self,
        cursor: psycopg.Cursor[Any],
        namespace: str,
        table_names: Iterable[str],
        ops: Iterable[sql.Composed],
    ) -> None:
        """Sibling of ``apply_schema_change_ops`` for DROP-shape DDL.

        Executes the rendered DROP ops AND scrubs the corresponding
        ``_table_id_prefixes`` entries — closing the same provider/cache
        drift hole on the destructive side. Caller threads in the cursor;
        provider executes + bookkeeps atomically (cache scrub after ops
        succeed, never on partial rollback).
        """
        for op in ops:
            cursor.execute(op)
        for table_name in table_names:
            full_table_name = build_table_name(namespace, table_name)
            self._table_id_prefixes.pop(full_table_name, None)

    def bootstrap_for_lifecycle(self) -> None:
        """Idempotent lifecycle substrate: ownership table + id_prefix cache.

        Two responsibilities, intentionally fused on one provider verb so the
        plugin's ``start_services`` doesn't reach into the database directly:

        1. **Ownership table bootstrap.** ``platform__plugin_schema_ownership``
           is the lifecycle's own metadata — the chicken-and-egg of the
           plugin-schema lifecycle. ``CREATE TABLE IF NOT EXISTS`` here.
        2. **id_prefix cache hydrate.** ``_table_id_prefixes`` is per-instance
           in-memory state that starts empty on every restart. Every install
           path (``apply_schema_change_ops``, ``create_table``) populates the
           cache for tables it actively writes — but the lifecycle's no-op
           and reactivate branches don't run DDL, so previously-installed
           tables never get their prefix re-registered after a restart.
           Hydrating from ownership rows at bootstrap closes that gap: the
           prefix recorded in each ``schema_snapshot_json`` is the source of
           truth for ID generation on subsequent upserts.

        The 2026-05-31 incident traced to gap #2: ``core__process_registry``
        existed on disk from a previous boot, the lifecycle took the
        ``_touch_updated_at`` no-op path (identical-shape re-install), no
        DDL ran, the prefix cache stayed empty, and the registry-persist
        burst failed 1088 times with "id_prefix not registered".
        """
        with self.get_connection() as conn, conn.cursor() as cur:
            cur.execute(_OWNERSHIP_TABLE_DDL)
            cur.execute(
                "SELECT plugin_namespace, table_name, schema_snapshot_json "
                "FROM platform__plugin_schema_ownership WHERE status = 'active'"
            )
            rows = cur.fetchall()

        for row in rows:
            namespace = row["plugin_namespace"]
            table_name = row["table_name"]
            snapshot = row["schema_snapshot_json"]
            if not isinstance(snapshot, dict):
                continue
            id_prefix = snapshot.get("id_prefix")
            if not isinstance(id_prefix, str) or not id_prefix:
                continue
            full_table_name = build_table_name(namespace, table_name)
            self._table_id_prefixes[full_table_name] = id_prefix
        logger.debug(
            "bootstrap_for_lifecycle: hydrated %d id_prefix entries from ownership",
            len(rows),
        )

    def _build_column_definitions(
        self, columns: dict[str, ColumnType | str | ColumnDefinition], table: str
    ) -> list[sql.Composed]:
        """Build SQL column definitions from column specs."""
        col_parts = []
        for col_name, col_definition in columns.items():
            column_sql = self._resolve_column_sql(col_name, col_definition)
            if not column_sql:
                logger.error(f"Skipping empty column definition for {col_name} on table {table}")
                continue
            col_parts.append(sql.SQL("{} {}").format(sql.Identifier(col_name), sql.SQL(column_sql)))  # type: ignore[arg-type]
        return col_parts

    def _resolve_column_sql(
        self, col_name: str, col_definition: ColumnType | str | ColumnDefinition
    ) -> str:
        """Resolve a column definition to SQL type string."""
        if isinstance(col_definition, ColumnType):
            return self._column_type_to_sql(col_name, col_definition)
        if isinstance(col_definition, ColumnDefinition):
            return self._column_definition_to_sql(col_name, col_definition)
        return str(col_definition).strip()

    def _column_type_to_sql(self, col_name: str, col_type: ColumnType) -> str:
        """Convert simple ColumnType to SQL.

        Per the 2026-06-12 Tier 1.A audit-timestamp design (Option B), the
        auto-default ``NOW() AT TIME ZONE 'UTC'`` applies ONLY to the
        canonical audit pair (``created_at`` / ``updated_at``). Other ``_at``
        columns keep NULL semantics so callers can use ``IS NULL`` guards.
        """
        pg_type = get_postgres_type(col_type)
        if col_type == ColumnType.DATETIME and col_name in {"created_at", "updated_at"}:
            return f"{pg_type} DEFAULT (NOW() AT TIME ZONE 'UTC')"
        return pg_type

    def _column_definition_to_sql(self, col_name: str, col_def: ColumnDefinition) -> str:
        """Convert ColumnDefinition to SQL.

        Per the 2026-06-12 Tier 1.A audit-timestamp design (Option B), the
        canonical audit pair (``created_at`` / ``updated_at``) with no
        declared default gets the auto-stamp; other ``_at`` columns keep
        NULL semantics.
        """
        column_sql = col_def.to_sql(col_name)
        parts = column_sql.split(None, 1)
        column_sql = parts[1] if len(parts) > 1 else self._fallback_type_sql(col_def)

        if (
            col_def.type == ColumnType.DATETIME
            and col_name in {"created_at", "updated_at"}
            and col_def.default is None
        ):
            return f"{get_postgres_type(col_def.type)} DEFAULT (NOW() AT TIME ZONE 'UTC')"
        return column_sql

    def _fallback_type_sql(self, col_def: ColumnDefinition) -> str:
        """Generate fallback type SQL for edge cases."""
        pg_type = get_postgres_type(col_def.type)
        if col_def.type == ColumnType.VECTOR and col_def.type_params:
            dimension = col_def.type_params.get("dimension")
            if dimension:
                return f"{pg_type}({dimension})"
        return pg_type

    def _create_updated_at_trigger(self, cur: Any, full_table_name: str) -> None:
        """Create trigger for auto-updating updated_at column."""
        trigger_name = f"{full_table_name}_update_updated_at"
        cur.execute(
            sql.SQL("""
                DROP TRIGGER IF EXISTS {} ON {};
                CREATE TRIGGER {}
                BEFORE UPDATE ON {}
                FOR EACH ROW
                EXECUTE FUNCTION {}.update_updated_at_column();
            """).format(
                sql.Identifier(trigger_name),
                sql.Identifier(self.config.schema_name, full_table_name),
                sql.Identifier(trigger_name),
                sql.Identifier(self.config.schema_name, full_table_name),
                sql.Identifier(self.config.schema_name),
            )
        )
        logger.debug(f"Created updated_at trigger for {self.config.schema_name}.{full_table_name}")

    def table_exists(self, namespace: str, table: str) -> bool:
        """
        Check if table exists.

        Args:
            namespace: Namespace for table
            table: Table name

        Returns:
            True if table exists, False otherwise
        """
        full_table_name = build_table_name(namespace, table)

        with self.get_connection() as conn, conn.cursor() as cur:
            cur.execute(
                """
                    SELECT EXISTS (
                        SELECT 1 FROM information_schema.tables
                        WHERE table_schema = %s AND table_name = %s
                    )
                    """,
                (self.config.schema_name, full_table_name),
            )
            result = cur.fetchone()
            return bool(result["exists"]) if result else False

    def get_table_info(self, namespace: str, table: str) -> dict[str, Any]:
        """
        Get table schema information.

        Args:
            namespace: Namespace for table
            table: Table name

        Returns:
            Dictionary with table information
        """
        full_table_name = build_table_name(namespace, table)

        with self.get_connection() as conn, conn.cursor() as cur:
            cur.execute(
                """
                    SELECT column_name, data_type, is_nullable, column_default
                    FROM information_schema.columns
                    WHERE table_schema = %s AND table_name = %s
                    ORDER BY ordinal_position
                    """,
                (self.config.schema_name, full_table_name),
            )
            columns = cur.fetchall()

            return {
                "table": full_table_name,
                "schema": self.config.schema_name,
                "columns": columns,
            }

    def insert(
        self,
        namespace: str,
        table: str,
        data: dict[str, Any],
    ) -> str:
        """
        Insert row and return generated ID.

        Args:
            namespace: Namespace for table
            table: Table name
            data: Data to insert

        Returns:
            Generated ID of inserted row

        Raises:
            psycopg.Error: If insert fails
        """
        full_table_name = build_table_name(namespace, table)
        insert_sql, values = self.build_insert_sql(namespace, table, data)

        with self.get_connection() as conn, conn.cursor() as cur:
            cur.execute(insert_sql, values)
            result = cur.fetchone()
            # Autocommit enabled - no explicit commit needed

            if not result:
                raise RuntimeError(f"Insert failed for {self.config.schema_name}.{full_table_name}")

            return cast(str, result["id"])

    def select(
        self,
        namespace: str,
        table: str,
        conditions: dict[str, Any] | None = None,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        """
        Select rows with WHERE conditions.

        Args:
            namespace: Namespace for table
            table: Table name
            conditions: WHERE conditions (column -> value)
            limit: Maximum number of rows to return

        Returns:
            List of rows as dictionaries
        """
        select_sql, params = self.build_select_sql(namespace, table, conditions, limit)

        with self.get_connection() as conn, conn.cursor() as cur:
            cur.execute(select_sql, params)
            results: list[dict[str, Any]] = cur.fetchall()
            # Serialize datetime objects for JSON compatibility
            return [_serialize_for_json(row) for row in results]

    def _build_cursor_clause(
        self, order_columns: tuple[str, ...], direction: str
    ) -> sql.Composable:
        """Row-value comparison clause for the ``after`` cursor.

        The comparison operator (``>`` ascending, ``<`` descending) is a
        compile-time literal selected by the validated ``direction`` enum —
        never built from caller input; every column is rendered via
        ``sql.Identifier``. The caller appends the ``after`` values to
        ``params`` in ``order_columns`` order to match the placeholders here.
        """
        cols_csv = sql.SQL(", ").join(
            sql.Identifier(col) for col in order_columns
        )
        placeholders = sql.SQL(", ").join(
            sql.Placeholder() for _ in order_columns
        )
        if direction == "asc":
            return sql.SQL("({}) > ({})").format(cols_csv, placeholders)
        return sql.SQL("({}) < ({})").format(cols_csv, placeholders)

    @staticmethod
    def _build_filter_clauses(
        conditions: dict[str, Any],
        *,
        serialize: Callable[[Any], Any] | None = None,
    ) -> tuple[list[sql.Composable], list[Any]]:
        """Compile a filter dict to WHERE clauses + ordered params.

        The sanctioned per-value filter grammar (SQL composition confined
        here; no caller-controlled SQL text). For each ``col: val``:

        * scalar value                 -> ``col = %s``, bound via ``serialize``
          when given else RAW. Callers pass the value serializer matching their
          execution path so the WHERE binding matches how that path WROTE the
          value: autocommit ``update`` passes ``_serialize_value_for_sql``
          (datetime->isoformat), the typed txn filter paths pass
          ``serialize_value_for_txn`` (tz-aware->naive UTC, the F1 seam), and
          ``select`` / ``select_ordered`` keep raw (their pre-existing behavior).
          Binding raw here when the write path serialized is the 2026-06-20
          regression Codex caught: a datetime WHERE silently matched 0 rows.
        * list / tuple value           -> ``col = ANY(%s)`` (one bound array;
          each element bound via ``serialize`` when given, else RAW -- mirroring
          the scalar branch so an array filter respects the same write-path
          serialization as a scalar one. The 2026-06-20 follow-up Codex caught:
          binding the array raw while the typed-txn paths wrote tz-aware
          datetimes as naive UTC made ``= ANY`` arrays of aware datetimes
          silently match 0 rows. Empty list matches nothing, unlike ``IN ()``.)
        * ``{"op": "is_null"}``        -> ``col IS NULL``
        * ``{"op": "is_not_null"}``    -> ``col IS NOT NULL``
        * ``{"op": "lt"|"lte"|"gt"|"gte", "value": X}`` -> ``col <op> %s`` (the
          Gap-A AND-range predicate; the ``value`` binds via ``serialize`` like
          the scalar branch). All dict-op compilation lives in
          :func:`_build_dict_op_clause` so this method stays rank-A.

        Every column is rendered via ``sql.Identifier``; every value via
        ``sql.Placeholder``; the operator keywords are compile-time literals.
        """
        ser: Callable[[Any], Any] = (
            serialize if serialize is not None else (lambda value: value)
        )
        clauses: list[sql.Composable] = []
        params: list[Any] = []
        for col, val in conditions.items():
            ident = sql.Identifier(col)
            if isinstance(val, dict):
                clause, extra = _build_dict_op_clause(ident, col, val, ser)
                clauses.append(clause)
                params.extend(extra)
            elif isinstance(val, (list, tuple)):
                clauses.append(
                    sql.SQL("{} = ANY({})").format(ident, sql.Placeholder())
                )
                params.append([ser(item) for item in val])
            else:
                clauses.append(
                    sql.SQL("{} = {}").format(ident, sql.Placeholder())
                )
                params.append(ser(val))
        return clauses, params

    def select_ordered(
        self,
        namespace: str,
        table: str,
        conditions: dict[str, Any],
        order_columns: tuple[str, ...],
        direction: str,
        limit: int,
        after: Sequence[object] | None = None,
        *,
        include_deleted: bool = False,
    ) -> list[dict[str, Any]]:
        """Select rows ordered by a composite key with a tie-safe cursor.

        Backs the ``query_ordered`` state primitive. ``conditions`` is the
        equality filter; ``order_columns`` is the deterministic composite
        (validated + identifier-safe by ``parse_ordered_query`` upstream),
        applied in ``direction`` (``asc``/``desc``); ``after`` is a
        direction-matched row-value cursor over ``order_columns``;
        ``include_deleted=False`` (default) filters ``is_deleted = 0``.

        Every column name is rendered via ``sql.Identifier`` (never string
        interpolation); the comparison + direction keywords are
        compile-time SQL literals chosen by the validated ``direction`` —
        no caller-controlled SQL text.
        """
        full_table_name = build_table_name(namespace, table)

        select_parts: list[sql.Composable] = [
            sql.SQL("SELECT * FROM {}").format(
                sql.Identifier(self.config.schema_name, full_table_name)
            )
        ]
        params: list[Any] = []
        where_clauses: list[sql.Composable] = []

        condition_clauses, condition_params = self._build_filter_clauses(conditions)
        where_clauses.extend(condition_clauses)
        params.extend(condition_params)

        if not include_deleted:
            where_clauses.append(
                sql.SQL("{} = 0").format(sql.Identifier("is_deleted"))
            )

        if after is not None:
            where_clauses.append(
                self._build_cursor_clause(order_columns, direction)
            )
            params.extend(after)

        if where_clauses:
            select_parts.append(sql.SQL(" WHERE "))
            select_parts.append(sql.SQL(" AND ").join(where_clauses))

        direction_kw = sql.SQL("ASC") if direction == "asc" else sql.SQL("DESC")
        order_sql = sql.SQL(", ").join(
            sql.SQL("{} {}").format(sql.Identifier(col), direction_kw)
            for col in order_columns
        )
        select_parts.append(sql.SQL(" ORDER BY "))
        select_parts.append(order_sql)
        select_parts.append(sql.SQL(" LIMIT {}").format(sql.Literal(limit)))

        select_sql = sql.SQL("").join(select_parts)

        with self.get_connection() as conn, conn.cursor() as cur:
            cur.execute(select_sql, params)
            results: list[dict[str, Any]] = cur.fetchall()
            return [_serialize_for_json(row) for row in results]

    def update(
        self,
        namespace: str,
        table: str,
        conditions: dict[str, Any],
        updates: dict[str, Any],
    ) -> int:
        """
        Update rows matching conditions.

        Args:
            namespace: Namespace for table
            table: Table name
            conditions: WHERE conditions
            updates: Column updates

        Returns:
            Number of rows updated
        """
        update_sql, params = self.build_update_sql(namespace, table, conditions, updates)

        with self.get_connection() as conn, conn.cursor() as cur:
            cur.execute(update_sql, params)
            # Autocommit enabled - no explicit commit needed
            row_count: int = cur.rowcount
            return row_count

    def acquire_lease(
        self,
        namespace: str,
        table: str,
        filters: dict[str, Any],
        lease_column: str,
        now: datetime,
        set_values: dict[str, Any],
    ) -> bool:
        """Execute the atomic expiry-fenced lease-acquire CAS (autocommit).

        Composes via ``build_acquire_lease_returning`` and runs the single
        ``UPDATE ... RETURNING id`` on a pooled autocommit connection. Returns
        ``True`` iff a row was claimed (the lease was free or expired), ``False``
        when a live owner still holds it. The single statement makes the row
        lock, the free-or-expired check, and the write atomic — no
        read-then-write TOCTOU.

        A lease is identity-targeted: ``filters`` MUST identify a SINGLE row by
        a scalar primary-key (``id``) equality predicate (additional equality
        guards like ``is_deleted=0`` may narrow it — the PK already bounds the
        match to <=1 row). A filter with no scalar ``id`` (broad / empty), a
        list-valued ``id`` (``= ANY``), or an op-valued ``id`` is REJECTED with
        ``ValueError`` BEFORE any UPDATE — otherwise a broad filter would
        silently acquire the same lease on every matched row. The builder stays
        generic; this exec-wrapper owns the single-row cardinality contract.
        """
        if not isinstance(filters.get("id"), str):
            raise ValueError(
                "acquire_lease requires 'filters' to identify a single row by a "
                "scalar string 'id' (the primary key); a broad/empty filter, a "
                "list-valued id (= ANY), or an op-valued id is rejected "
                f"(got id={filters.get('id')!r})"
            )
        composed, params = self.build_acquire_lease_returning(
            namespace, table, filters, lease_column, now, set_values
        )
        with self.get_connection() as conn, conn.cursor() as cur:
            cur.execute(composed, params)
            return cur.fetchone() is not None

    def aggregate(
        self,
        namespace: str,
        table: str,
        op: str,
        column: str | None,
        filters: dict[str, Any],
    ) -> object:
        """Run a single-scalar aggregate (``count``/``max``/``min``) autocommit.

        Composes via ``build_aggregate_query`` and runs the one-row
        ``SELECT {AGG} AS value`` on a pooled autocommit connection. The result
        is serialized for JSON transport — the AUTOCOMMIT fidelity, matching
        ``select`` / ``query_state``: ``count`` → ``int`` (``>= 0``);
        ``max``/``min`` → the column value with a ``TIMESTAMP`` rendered as an
        ISO-8601 string (so the ActionResult envelope is JSON-safe at the bridge
        boundary), or ``None`` over an empty set (SQL ``NULL``). No rows are
        materialized.

        The TYPED-TXN surface does NOT call this method — it composes via
        ``build_aggregate_query`` directly and returns the RAW naive datetime
        (the F1 seam) for in-process consumers; only this autocommit exec method
        serializes, exactly as ``select`` serializes while txn ``query_state``
        keeps raw rows.
        """
        composed, params = self.build_aggregate_query(
            namespace, table, op, column, filters
        )
        with self.get_connection() as conn, conn.cursor() as cur:
            cur.execute(composed, params)
            row = cur.fetchone()
        if row is None:
            raise RuntimeError(
                f"aggregate {op!r} returned no row for {namespace}.{table}"
            )
        return _serialize_for_json(row["value"])

    # ------------------------------------------------------------------
    # Pure SQL builders — composition only, no connection/execution.
    # Shared by the autocommit methods above and the typed StateTransaction
    # ops, so both execution paths compose SQL at ONE site (no caller SQL).
    # ``serialize`` defaults to the autocommit value serializer; the typed
    # txn ops pass ``serialize_value_for_txn`` (the F1 TZ-storage seam).
    # ------------------------------------------------------------------

    def build_insert_sql(
        self,
        namespace: str,
        table: str,
        data: dict[str, Any],
        *,
        serialize: Callable[[Any], Any] = _serialize_value_for_sql,
    ) -> tuple[sql.Composed, list[Any]]:
        """Compose ``INSERT ... RETURNING id`` (no id generation; matches insert)."""
        full_table_name = build_table_name(namespace, table)
        columns = list(data.keys())
        params = [serialize(data[col]) for col in columns]
        composed = sql.SQL("INSERT INTO {} ({}) VALUES ({}) RETURNING id").format(
            sql.Identifier(self.config.schema_name, full_table_name),
            sql.SQL(", ").join(sql.Identifier(col) for col in columns),
            sql.SQL(", ").join(sql.Placeholder() for _ in columns),
        )
        return composed, params

    def build_update_sql(
        self,
        namespace: str,
        table: str,
        conditions: dict[str, Any],
        updates: dict[str, Any],
        *,
        serialize: Callable[[Any], Any] = _serialize_value_for_sql,
    ) -> tuple[sql.Composed, list[Any]]:
        """Compose ``UPDATE ... SET ... WHERE ...``; WHERE via the shared grammar."""
        full_table_name = build_table_name(namespace, table)
        set_clauses: list[sql.Composable] = []
        params: list[Any] = []
        for col, val in updates.items():
            set_clauses.append(
                sql.SQL("{} = {}").format(sql.Identifier(col), sql.Placeholder())
            )
            params.append(serialize(val))
        where_clauses, where_params = self._build_filter_clauses(
            conditions, serialize=serialize
        )
        params.extend(where_params)
        composed = sql.SQL("UPDATE {} SET {} WHERE {}").format(
            sql.Identifier(self.config.schema_name, full_table_name),
            sql.SQL(", ").join(set_clauses),
            sql.SQL(" AND ").join(where_clauses),
        )
        return composed, params

    def build_select_sql(
        self,
        namespace: str,
        table: str,
        conditions: dict[str, Any] | None = None,
        limit: int | None = None,
        *,
        serialize: Callable[[Any], Any] | None = None,
    ) -> tuple[sql.Composed, list[Any]]:
        """Compose ``SELECT * ... [WHERE ...] [LIMIT ...]``; WHERE via the grammar.

        ``serialize`` defaults to None (raw scalar filters — the autocommit
        ``select`` behavior); the typed txn ``query_state`` passes
        ``serialize_value_for_txn`` so its WHERE honors the F1 seam.
        """
        full_table_name = build_table_name(namespace, table)
        select_parts: list[sql.Composable] = [
            sql.SQL("SELECT * FROM {}").format(
                sql.Identifier(self.config.schema_name, full_table_name)
            )
        ]
        params: list[Any] = []
        if conditions:
            where_clauses, where_params = self._build_filter_clauses(
                conditions, serialize=serialize
            )
            params.extend(where_params)
            select_parts.append(sql.SQL(" WHERE "))
            select_parts.append(sql.SQL(" AND ").join(where_clauses))
        if limit:
            select_parts.append(sql.SQL(" LIMIT {}").format(sql.Literal(limit)))
        return sql.SQL("").join(select_parts), params

    def build_increment_returning(
        self,
        namespace: str,
        table: str,
        filters: dict[str, Any],
        column: str,
        by: int,
        *,
        serialize: Callable[[Any], Any] = serialize_value_for_txn,
    ) -> tuple[sql.Composed, list[Any]]:
        """Compose ``UPDATE {t} SET {c}={c}+%s WHERE {filters} RETURNING {c}``.

        Backs the atomic self-referential cursor allocator (txn-only): ``column``
        via ``sql.Identifier``; ``by`` + every filter value via
        ``sql.Placeholder``; ``+`` / ``RETURNING`` compile-time literals; WHERE
        via the shared ``= ANY``/null grammar (so a list filter fuses the
        Mapping-A status gate). ``serialize`` defaults to
        ``serialize_value_for_txn`` (its only caller is the typed txn path, so
        scalar WHERE filters honor the F1 seam). Does NOT touch ``updated_at`` —
        the BEFORE-UPDATE trigger maintains it.
        """
        full_table_name = build_table_name(namespace, table)
        where_clauses, where_params = self._build_filter_clauses(
            filters, serialize=serialize
        )
        column_ident = sql.Identifier(column)
        composed = sql.SQL(
            "UPDATE {} SET {} = {} + {} WHERE {} RETURNING {}"
        ).format(
            sql.Identifier(self.config.schema_name, full_table_name),
            column_ident,
            column_ident,
            sql.Placeholder(),
            sql.SQL(" AND ").join(where_clauses),
            column_ident,
        )
        return composed, [by, *where_params]

    def build_acquire_lease_returning(
        self,
        namespace: str,
        table: str,
        filters: dict[str, Any],
        lease_column: str,
        now: datetime,
        set_values: dict[str, Any],
        *,
        serialize: Callable[[Any], Any] = serialize_value_for_txn,
    ) -> tuple[sql.Composed, list[Any]]:
        """Compose the atomic expiry-fenced lease-acquire CAS.

        ``UPDATE {t} SET {set} WHERE {filters} AND ({lease} IS NULL OR {lease}
        < %s) RETURNING id`` — the disjunctive availability predicate the flat
        equality / ``= ANY`` / null grammar cannot express. ONE statement, so
        the row lock, the "free OR expired" check, and the write are atomic (no
        read-then-write TOCTOU that a token-fenced lease would re-open).

        ``filters`` is the row-identity equality match (the shared
        ``_build_filter_clauses`` grammar); ``lease_column`` is the expiry
        column, claimed iff it ``IS NULL`` or is strictly older than ``now``;
        ``set_values`` are the columns written on a successful claim (e.g. the
        new expiry window + a fresh fence token). Every column via
        ``sql.Identifier``; every value via ``sql.Placeholder``; the
        ``IS NULL OR <`` keywords are compile-time literals — no
        caller-controlled SQL text.

        ``serialize`` defaults to ``serialize_value_for_txn`` (the F1 naive-UTC
        seam) and is applied to the ``set`` values, the scalar ``filters``, AND
        the ``now`` threshold. This is REQUIRED, not cosmetic: the ``< now``
        comparison must bind the threshold the same way the stored expiry was
        written (naive UTC); the autocommit ``isoformat``-with-offset serializer
        would skew a tz-aware threshold and silently break the CAS (the
        2026-06-20 datetime-CAS class of defect). Param order is
        ``[set..., filters..., now]``.

        Does NOT touch ``updated_at`` — the BEFORE-UPDATE trigger maintains it
        (the caller confirms per-table trigger coverage; if a table lacks the
        trigger it adds ``updated_at`` to ``set_values``).
        """
        full_table_name = build_table_name(namespace, table)
        set_clauses: list[sql.Composable] = []
        params: list[Any] = []
        for col, val in set_values.items():
            set_clauses.append(
                sql.SQL("{} = {}").format(sql.Identifier(col), sql.Placeholder())
            )
            params.append(serialize(val))
        where_clauses, where_params = self._build_filter_clauses(
            filters, serialize=serialize
        )
        params.extend(where_params)
        lease_ident = sql.Identifier(lease_column)
        where_clauses.append(
            sql.SQL("({} IS NULL OR {} < {})").format(
                lease_ident, lease_ident, sql.Placeholder()
            )
        )
        params.append(serialize(now))
        composed = sql.SQL("UPDATE {} SET {} WHERE {} RETURNING id").format(
            sql.Identifier(self.config.schema_name, full_table_name),
            sql.SQL(", ").join(set_clauses),
            sql.SQL(" AND ").join(where_clauses),
        )
        return composed, params

    def build_aggregate_query(
        self,
        namespace: str,
        table: str,
        op: str,
        column: str | None,
        filters: dict[str, Any],
        *,
        serialize: Callable[[Any], Any] | None = None,
    ) -> tuple[sql.Composed, list[Any]]:
        """Compose ``SELECT {AGG} AS value FROM {t} [WHERE {filters}]``.

        ``AGG`` is chosen by ``_build_aggregate_expr`` from the closed op-map
        (``COUNT(*)`` / ``MAX({col})`` / ``MIN({col})``; column via
        ``sql.Identifier``, keyword a compile-time literal — no caller SQL).
        ``filters`` uses the shared ``_build_filter_clauses`` grammar (equality
        / ``= ANY`` / ``is_null`` / ``is_not_null`` / Gap-A range); empty
        filters → no ``WHERE``. Returns exactly one row, one column ``value``.

        NO auto ``is_deleted`` exclusion (mirrors ``query_state``, NOT
        ``query_ordered``) — the caller passes ``is_deleted`` in ``filters`` if
        wanted. ``serialize`` defaults to ``None`` (raw scalar filters, the
        autocommit behavior); the typed-txn path passes ``serialize_value_for_txn``
        so its WHERE honors the F1 seam.
        """
        expr = _build_aggregate_expr(op, column)
        full_table_name = build_table_name(namespace, table)
        select_parts: list[sql.Composable] = [
            sql.SQL("SELECT {} AS value FROM {}").format(
                expr, sql.Identifier(self.config.schema_name, full_table_name)
            )
        ]
        params: list[Any] = []
        if filters:
            where_clauses, where_params = self._build_filter_clauses(
                filters, serialize=serialize
            )
            params.extend(where_params)
            select_parts.append(sql.SQL(" WHERE "))
            select_parts.append(sql.SQL(" AND ").join(where_clauses))
        return sql.SQL("").join(select_parts), params

    def build_delete_sql(
        self,
        namespace: str,
        table: str,
        conditions: dict[str, Any],
        *,
        soft_delete: bool = True,
        serialize: Callable[[Any], Any] = serialize_value_for_txn,
    ) -> tuple[sql.Composed, list[Any]]:
        """Compose a delete; WHERE via the shared grammar (the typed-txn path).

        ``soft_delete=True`` (default) → ``UPDATE ... SET is_deleted = 1 WHERE
        ...`` (reuses ``build_update_sql``, mirroring the autocommit
        ``delete``'s soft path). ``soft_delete=False`` → ``DELETE FROM ... WHERE
        ...``. ``serialize`` defaults to ``serialize_value_for_txn`` (its only
        caller is the typed txn ``delete_records``, so scalar WHERE filters
        honor the F1 seam).
        """
        if soft_delete:
            return self.build_update_sql(
                namespace, table, conditions, {"is_deleted": 1}, serialize=serialize
            )
        full_table_name = build_table_name(namespace, table)
        where_clauses, where_params = self._build_filter_clauses(
            conditions, serialize=serialize
        )
        composed = sql.SQL("DELETE FROM {} WHERE {}").format(
            sql.Identifier(self.config.schema_name, full_table_name),
            sql.SQL(" AND ").join(where_clauses),
        )
        return composed, where_params

    def delete(
        self,
        namespace: str,
        table: str,
        conditions: dict[str, Any],
        soft_delete: bool = True,
    ) -> int:
        """
        Delete rows matching conditions.

        Args:
            namespace: Namespace for table
            table: Table name
            conditions: WHERE conditions
            soft_delete: If True, set is_deleted=1; if False, hard delete

        Returns:
            Number of rows affected
        """
        if soft_delete:
            # Soft delete: set is_deleted flag
            return self.update(namespace, table, conditions, {"is_deleted": 1})
        else:
            # Hard delete
            full_table_name = build_table_name(namespace, table)

            where_clauses = []
            params: list[Any] = []

            for col, val in conditions.items():
                where_clauses.append(
                    sql.SQL("{} = {}").format(sql.Identifier(col), sql.Placeholder())
                )
                params.append(val)

            delete_sql = sql.SQL("DELETE FROM {} WHERE {}").format(
                sql.Identifier(self.config.schema_name, full_table_name),
                sql.SQL(" AND ").join(where_clauses),
            )

            with self.get_connection() as conn, conn.cursor() as cur:
                cur.execute(delete_sql, params)
                # Autocommit enabled - no explicit commit needed
                row_count: int = cur.rowcount
                return row_count

    def execute_query(
        self, sql_query: str, params: tuple[object, ...] | None = None
    ) -> list[list[object]]:
        """
        Execute arbitrary SQL query.

        CRITICAL: Required by ActionQueuePoller for queue management.

        Database Agnostic: Accepts SQLite-style ? placeholders and converts to
        PostgreSQL $1, $2 format.

        Args:
            sql_query: SQL query to execute (supports ? placeholders for database agnosticism)
            params: Optional query parameters for parameterized queries

        Returns:
            List of result rows as lists (for compatibility with SQLite plugin)

        Raises:
            RuntimeError: If provider not initialized
            psycopg.Error: If query execution fails
        """
        if not self._initialized or not self._pool:
            raise RuntimeError("PostgresProvider not initialized. Cannot execute query.")

        # Convert SQLite-style ? placeholders to psycopg %s format
        # This makes the interface database-agnostic
        if "?" in sql_query:
            sql_query = sql_query.replace("?", "%s")

        with self.get_connection() as conn:
            cursor = conn.cursor(row_factory=dict_row)

            try:
                if params:
                    cursor.execute(sql_query, params)  # type: ignore[arg-type]
                else:
                    cursor.execute(sql_query)  # type: ignore[arg-type]

                # For SELECT queries, fetch results
                if cursor.description:
                    # Convert dict rows to lists for compatibility with SQLite plugin
                    # ActionQueuePoller expects list/tuple format, not dict
                    rows = cursor.fetchall()
                    # Serialize datetime objects before converting to list
                    serialized_rows = [_serialize_for_json(row) for row in rows]
                    return [list(row.values()) for row in serialized_rows]

                # For INSERT/UPDATE/DELETE, autocommit is enabled - return empty list
                return []

            except Exception as e:
                logger.error(f"Query execution failed: {sql_query[:100]}...")
                logger.error(f"Error: {e}")
                raise

    def upsert(
        self,
        namespace: str,
        table: str,
        data: dict[str, Any],
        conflict_columns: list[str],
    ) -> str:
        """Insert or update on conflict.

        ID generation rules (FAIL-FAST, no fallbacks):
        - If 'id' is NOT in data: Generate new ID using table's id_prefix
        - If 'id' IS in data: Record MUST exist (this is an update scenario)

        Args:
            namespace: Namespace for table
            table: Table name
            data: Record data to upsert
            conflict_columns: Columns to check for conflicts

        Returns:
            The record ID (generated or existing)

        Raises:
            ValueError: If id_prefix not found for table (schema not registered)
            RuntimeError: If id provided but record doesn't exist
        """
        full_table_name = build_table_name(namespace, table)

        # Determine if this is a new record or update
        if "id" not in data:
            # NEW RECORD: Generate ID using table's id_prefix
            id_prefix = self._table_id_prefixes.get(full_table_name)
            if not id_prefix:
                raise ValueError(
                    f"Cannot generate ID for table '{full_table_name}': "
                    f"id_prefix not registered. Table must be created via create_table() first."
                )
            data = {**data, "id": self.generate_id(id_prefix)}
            logger.debug(f"Generated new ID for {full_table_name}: {data['id']}")

        columns = list(data.keys())
        values = [_serialize_value_for_sql(data[col]) for col in columns]
        upsert_sql = self._build_upsert_sql(full_table_name, columns, conflict_columns)

        with self.get_connection() as conn, conn.cursor() as cur:
            cur.execute(upsert_sql, values)
            result = cur.fetchone()
            if not result:
                raise RuntimeError(f"Upsert failed for {self.config.schema_name}.{full_table_name}")

            return cast(str, result["id"])

    def upsert_conditional(
        self,
        namespace: str,
        table: str,
        data: dict[str, Any],
        conflict_columns: list[str],
        conflict_predicate: list[dict[str, Any]] | None = None,
    ) -> tuple[bool, str | None]:
        """Insert-or-skip with a partial ``ON CONFLICT`` predicate (DO NOTHING).

        Backs ``upsert_state``'s ``on_conflict="do_nothing"`` path. Emits
        ``INSERT ... ON CONFLICT (conflict_columns) [WHERE <predicate>] DO
        NOTHING RETURNING id``; with ``DO NOTHING`` the ``RETURNING`` clause
        yields a row ONLY when the row was actually inserted.

        Returns ``(inserted, id)``: ``(True, <id>)`` on insert, ``(False,
        None)`` when the conflict predicate matched an existing row and the
        insert was skipped. ID generation matches ``upsert`` (FAIL-FAST: a
        missing ``id`` requires a registered ``id_prefix``). SQL composition
        stays in ``_build_upsert_sql`` / ``_build_conflict_predicate``.
        """
        full_table_name = build_table_name(namespace, table)

        if "id" not in data:
            id_prefix = self._table_id_prefixes.get(full_table_name)
            if not id_prefix:
                raise ValueError(
                    f"Cannot generate ID for table '{full_table_name}': "
                    f"id_prefix not registered. Table must be created via "
                    f"create_table() first."
                )
            data = {**data, "id": self.generate_id(id_prefix)}

        columns = list(data.keys())
        values = [_serialize_value_for_sql(data[col]) for col in columns]

        predicate_sql: sql.Composed | None = None
        if conflict_predicate:
            predicate_sql = self._build_conflict_predicate(conflict_predicate)

        insert_sql = self._build_upsert_sql(
            full_table_name,
            columns,
            conflict_columns,
            on_conflict_do_nothing=True,
            conflict_predicate_sql=predicate_sql,
        )

        with self.get_connection() as conn, conn.cursor() as cur:
            cur.execute(insert_sql, values)
            result = cur.fetchone()

        if result is None:
            return (False, None)
        return (True, cast(str, result["id"]))

    def _build_upsert_sql(
        self,
        full_table_name: str,
        columns: list[str],
        conflict_columns: list[str],
        *,
        on_conflict_do_nothing: bool = False,
        conflict_predicate_sql: sql.Composed | None = None,
    ) -> sql.Composed:
        """Build an ``INSERT ... ON CONFLICT`` statement.

        Default path: ``ON CONFLICT (cols) DO UPDATE SET ... RETURNING id`` —
        the idempotent upsert. When ``on_conflict_do_nothing`` is set the
        statement becomes ``ON CONFLICT (cols) [WHERE <predicate>] DO NOTHING
        RETURNING id`` (the partial-unique-index path); ``RETURNING id`` then
        yields a row ONLY on insert, never on the skipped-conflict path.
        """
        placeholders = [sql.Placeholder() for _ in columns]
        col_ids = [sql.Identifier(col) for col in columns]
        conflict_ids = [sql.Identifier(col) for col in conflict_columns]
        insert_head = sql.SQL(
            "INSERT INTO {} ({}) VALUES ({}) ON CONFLICT ({})"
        ).format(
            sql.Identifier(self.config.schema_name, full_table_name),
            sql.SQL(", ").join(col_ids),
            sql.SQL(", ").join(placeholders),
            sql.SQL(", ").join(conflict_ids),
        )

        if on_conflict_do_nothing:
            if conflict_predicate_sql is not None:
                return sql.SQL("{} WHERE {} DO NOTHING RETURNING id").format(
                    insert_head, conflict_predicate_sql
                )
            return sql.SQL("{} DO NOTHING RETURNING id").format(insert_head)

        update_columns = [col for col in columns if col not in conflict_columns]
        update_set = self._build_update_set(update_columns, conflict_columns)
        return sql.SQL("{} DO UPDATE SET {} RETURNING id").format(
            insert_head, update_set
        )

    def _build_update_set(
        self, update_columns: list[str], conflict_columns: list[str]
    ) -> sql.Composed:
        """Build the SET clause for upsert."""
        if update_columns:
            return sql.SQL(", ").join(
                sql.SQL("{} = EXCLUDED.{}").format(sql.Identifier(col), sql.Identifier(col))
                for col in update_columns
            )
        # All columns are conflict columns - use dummy update for RETURNING to work
        dummy_col = conflict_columns[0]
        return sql.SQL("{} = EXCLUDED.{}").format(
            sql.Identifier(dummy_col), sql.Identifier(dummy_col)
        )

    @staticmethod
    def _build_conflict_predicate(
        predicate: list[Any],
    ) -> sql.Composed:
        """Compile a structured conflict predicate to an ON CONFLICT WHERE clause.

        Each entry is ``{"column", "op", "value"?}`` with ``op`` in the fixed
        enum ``{is_null, is_not_null, eq}`` (exactly what the partial-unique
        ledger index needs). Column -> ``sql.Identifier``; ``eq`` value ->
        ``sql.Literal`` (NOT a bind placeholder). The literal is deliberate:
        Postgres' ON CONFLICT arbiter inference matches a partial index's
        ``WHERE`` only against a constant-folded predicate, so a bound
        ``is_deleted = $1`` cannot be proven to imply the index's
        ``is_deleted = 0`` at plan time and inference fails — whereas the
        constant ``is_deleted = 0`` matches. The operator keyword is a
        compile-time literal; no caller-controlled SQL text. Clauses AND-joined.
        """
        clauses: list[sql.Composable] = []
        for entry in predicate:
            if not isinstance(entry, dict):
                raise ValueError(
                    "conflict_predicate entry must be a dict with 'column'/'op'"
                    f"[/'value'], got {type(entry).__name__}"
                )
            column = entry.get("column")
            op = entry.get("op")
            if not isinstance(column, str):
                raise ValueError(
                    f"conflict_predicate entry 'column' must be a str, got {column!r}"
                )
            ident = sql.Identifier(column)
            if op == "is_null":
                clauses.append(sql.SQL("{} IS NULL").format(ident))
            elif op == "is_not_null":
                clauses.append(sql.SQL("{} IS NOT NULL").format(ident))
            elif op == "eq":
                if "value" not in entry:
                    raise ValueError(
                        f"conflict_predicate 'eq' entry for column {column!r} "
                        "requires a 'value'"
                    )
                clauses.append(
                    sql.SQL("{} = {}").format(ident, sql.Literal(entry["value"]))
                )
            else:
                raise ValueError(
                    f"unsupported conflict-predicate op {op!r} for column "
                    f"{column!r}; expected 'is_null', 'is_not_null', or 'eq'"
                )
        return sql.SQL(" AND ").join(clauses)

    # StateProviderInterface implementation

    def record_action_execution(self, execution_record: ActionExecutionRecord) -> bool:
        """Record action execution."""
        try:
            data = {
                "id": execution_record.id,
                "action_name": execution_record.action_name,
                "provider_type": execution_record.provider_type,
                "provider": execution_record.provider,
                "status": execution_record.status,
                "parameters": execution_record.parameters,
                "result": execution_record.result,
                "error": execution_record.error,
                "duration_ms": execution_record.duration_ms,
                "started_at": execution_record.started_at,
                "completed_at": execution_record.completed_at,
                "source_context": execution_record.source_context,
                "external_id": execution_record.external_id,
                "is_deleted": execution_record.is_deleted,
                "tags": execution_record.tags,
            }

            self.insert("core", "action_executions", data)
            return True
        except Exception as e:
            logger.error(f"Failed to record action execution: {e}")
            return False

    def update_action_execution(self, id: str, updates: dict[str, object]) -> bool:
        """Update action execution."""
        try:
            self.update("core", "action_executions", {"id": id}, updates)
            return True
        except Exception as e:
            logger.error(f"Failed to update action execution: {e}")
            return False

    def get_action_execution(self, id: str) -> ActionExecutionRecord | None:
        """Get action execution by ID."""
        try:
            rows = self.select("core", "action_executions", {"id": id}, limit=1)
            if not rows:
                return None

            row = rows[0]
            return ActionExecutionRecord(
                id=row["id"],
                action_name=row["action_name"],
                provider_type=row["provider_type"],
                provider=row["provider"],
                status=row["status"],
                parameters=row.get("parameters"),
                result=row.get("result"),
                error=row.get("error"),
                duration_ms=row.get("duration_ms"),
                started_at=row.get("started_at"),
                completed_at=row.get("completed_at"),
                source_context=row.get("source_context"),
                external_id=row.get("external_id"),
                is_deleted=row.get("is_deleted", False),
                tags=row.get("tags"),
            )
        except Exception as e:
            logger.error(f"Failed to get action execution: {e}")
            return None

    def list_action_executions(
        self,
        filters: dict[str, object] | None = None,
        _limit: int | None = None,
        _order_by: str | None = None,
    ) -> list[ActionExecutionRecord]:
        """List action executions with filters."""
        try:
            rows = self.select(
                "core",
                "action_executions",
                conditions=filters,
                limit=_limit,
            )

            return [
                ActionExecutionRecord(
                    id=row["id"],
                    action_name=row["action_name"],
                    provider_type=row["provider_type"],
                    provider=row["provider"],
                    status=row["status"],
                    parameters=row.get("parameters"),
                    result=row.get("result"),
                    error=row.get("error"),
                    duration_ms=row.get("duration_ms"),
                    started_at=row.get("started_at"),
                    completed_at=row.get("completed_at"),
                    source_context=row.get("source_context"),
                    external_id=row.get("external_id"),
                    is_deleted=row.get("is_deleted", False),
                    tags=row.get("tags"),
                )
                for row in rows
            ]
        except Exception as e:
            logger.error(f"Failed to list action executions: {e}")
            return []

    def create_action_execution_schema(self) -> bool:
        """Create action execution schema if it doesn't exist. Returns True on success."""
        try:
            columns: dict[str, ColumnType | str] = {
                "id": ColumnType.TEXT,
                "action_name": ColumnType.TEXT,
                "provider_type": ColumnType.TEXT,
                "provider": ColumnType.TEXT,
                "status": ColumnType.TEXT,
                "parameters": ColumnType.TEXT,
                "result": ColumnType.TEXT,
                "error": ColumnType.TEXT,
                "duration_ms": ColumnType.INTEGER,
                "started_at": ColumnType.DATETIME,
                "completed_at": ColumnType.DATETIME,
                "source_context": ColumnType.TEXT,
                "external_id": ColumnType.TEXT,
                "is_deleted": ColumnType.BOOLEAN,
                "tags": ColumnType.TEXT,
            }

            self.create_table(
                "core",
                "action_executions",
                cast(dict[str, ColumnType | str | ColumnDefinition], columns),
                table_prefix="ax",
            )
            return True
        except Exception as e:
            logger.error(f"Failed to create action execution schema: {e}")
            return False

    def close(self) -> None:
        """Close connection pool."""
        if self._pool:
            self._pool.close()
            logger.debug("Connection pool closed")
