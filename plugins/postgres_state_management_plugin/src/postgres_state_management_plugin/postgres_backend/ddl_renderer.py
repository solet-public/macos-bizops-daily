"""
Postgres-native DDL renderer for the plugin-schema lifecycle.

Single source of truth for CREATE / index / trigger DDL emitted from a
``TableSchema``. Returns ``psycopg.sql.Composed`` ops the caller applies in
whichever transaction mode it wants (autocommit for the legacy path,
explicit transaction for the new lifecycle service).

Design notes (physical-layout fidelity):
  * Physical names use the ``{namespace}__{table}`` double-underscore
    convention that matches the platform's real Postgres storage layout.
  * Column DDL via ``ColumnDefinition.to_sql()`` runs through
    ``ColumnSqlGenerator``, which historically encoded SQLite storage hints
    in the enum value. Postgres-native rendering goes through
    ``COLUMN_TYPE_MAP`` here.
  * Index physical names are resolved to ``{namespace}__{table}__{idx_name}``
    so multiple tables can share a logical name like ``idx_status`` without
    collision.

What is intentionally out of scope here:
  * Apply / transaction management — caller's responsibility.
  * Ownership-table writes — the lifecycle service handles those alongside
    the ops returned here.
  * SQLite or any non-Postgres dialect.
"""

from __future__ import annotations

import hashlib
import logging

from ananta.types.column_types import ColumnType
from ananta.types.schema_types import ColumnDefinition, IndexDefinition, TableSchema
from ananta.types.sql_function_detector import SqlFunctionDetector
from psycopg import sql

from .utils import build_table_name, get_postgres_type

_SQL_FUNCTION_DETECTOR = SqlFunctionDetector()

# Postgres's default NAMEDATALEN is 64, giving a max identifier length of 63
# characters. Longer identifiers are silently truncated on storage but their
# *literal* form is what IF NOT EXISTS and identifier equality compare against,
# so two distinct declared names that both truncate to the same 63 chars become
# a collision at storage time even when each call's IF NOT EXISTS check says
# "doesn't exist." resolve_index_name folds the over-length case to a
# deterministic hash suffix to keep names unique and within the limit.
POSTGRES_MAX_IDENTIFIER_LENGTH = 63

logger = logging.getLogger(__name__)


def emit_create_table_ops(
    namespace: str,
    table: TableSchema,
    schema_name: str,
) -> list[sql.Composed]:
    """Emit ordered DDL ops to create one table, its trigger, and its indexes.

    Args:
        namespace: Plugin namespace (e.g. ``"core"``, ``"audio_processing_plugin"``).
        table: Standardized ``TableSchema`` to render.
        schema_name: Postgres schema (database namespace) to qualify identifiers
            with — typically ``provider.config.schema_name``.

    Returns:
        Ordered list of ``psycopg.sql.Composed`` statements:
        ``CREATE TABLE`` first, then the ``updated_at`` trigger if applicable,
        then each ``CREATE INDEX`` with a resolved physical name.

    Notes:
        * ``with_history=True`` is logged as a warning and otherwise ignored.
          The platform does not currently emit history tables (preserving the
          pre-v6 behavior). Real history-table generation is a tracked follow-up.
        * Physical index names are ``{namespace}__{table}__{idx_name}``.
    """
    ops: list[sql.Composed] = []
    full_table_name = build_table_name(namespace, table.table_name)

    if table.with_history:
        logger.warning(
            "with_history=True ignored for %s.%s — history-table generation is not "
            "implemented yet (tracked follow-up). The flag is recorded in the "
            "schema snapshot for a future migration.",
            namespace,
            table.table_name,
        )

    ops.append(_emit_create_table(schema_name, full_table_name, table))

    if "updated_at" in table.columns:
        ops.append(_emit_updated_at_trigger(schema_name, full_table_name))

    for index in table.indexes:
        ops.append(_emit_create_index(schema_name, namespace, table.table_name, index))

    return ops


def emit_drop_table_op(
    namespace: str,
    table_name: str,
    schema_name: str,
) -> sql.Composed:
    """Emit ``DROP TABLE IF EXISTS`` for one table. Used by the purge path."""
    full_table_name = build_table_name(namespace, table_name)
    return sql.SQL("DROP TABLE IF EXISTS {}").format(
        sql.Identifier(schema_name, full_table_name)
    )


def emit_add_column_op(
    namespace: str,
    table_name: str,
    column_name: str,
    column_def: ColumnDefinition,
    schema_name: str,
) -> sql.Composed:
    """Emit ``ALTER TABLE … ADD COLUMN`` using the same Postgres-native
    column renderer the CREATE path uses. Reuses ``_emit_column`` so
    type/default/check formatting is identical to fresh-create.
    """
    full_table_name = build_table_name(namespace, table_name)
    column_fragment = _emit_column(column_name, column_def)
    return sql.SQL("ALTER TABLE {} ADD COLUMN {}").format(
        sql.Identifier(schema_name, full_table_name), column_fragment
    )


def emit_drop_column_op(
    namespace: str,
    table_name: str,
    column_name: str,
    schema_name: str,
) -> sql.Composed:
    """Emit ``ALTER TABLE … DROP COLUMN`` for one column."""
    full_table_name = build_table_name(namespace, table_name)
    return sql.SQL("ALTER TABLE {} DROP COLUMN {}").format(
        sql.Identifier(schema_name, full_table_name), sql.Identifier(column_name)
    )


def build_default_check_constraint_name(
    namespace: str, table_name: str, column_name: str,
) -> str:
    """Default Postgres-assigned constraint name for an inline column CHECK.

    Postgres auto-names an inline ``CHECK (...)`` constraint declared as
    part of a column definition as ``<full_table>_<column>_check``. The
    schema-diff DROP path needs this name to remove the existing CHECK
    before issuing the replacement ``ADD CONSTRAINT … CHECK (…)``. Per
    M21-RCA Fix 1 (2026-06-11 PT) the additive-enum-expansion diff path
    relies on this convention; if a future schema declares its own named
    CHECK via a different surface, a parallel resolver is needed.
    """
    full_table = build_table_name(namespace, table_name)
    return f"{full_table}_{column_name}_check"


def emit_drop_constraint_op(
    namespace: str,
    table_name: str,
    constraint_name: str,
    schema_name: str,
) -> sql.Composed:
    """Emit ``ALTER TABLE … DROP CONSTRAINT <name>`` for one named constraint.

    Used by the additive-CHECK-expansion path in ``schema_diff`` to drop
    the existing CHECK before re-adding it under the expanded enum
    (M21-RCA Fix 1).
    """
    full_table_name = build_table_name(namespace, table_name)
    return sql.SQL("ALTER TABLE {} DROP CONSTRAINT {}").format(
        sql.Identifier(schema_name, full_table_name),
        sql.Identifier(constraint_name),
    )


def emit_add_check_constraint_op(
    namespace: str,
    table_name: str,
    constraint_name: str,
    check_expr: str,
    schema_name: str,
) -> sql.Composed:
    """Emit ``ALTER TABLE … ADD CONSTRAINT <name> CHECK (<expr>)``.

    Used by the additive-CHECK-expansion path in ``schema_diff`` to
    re-add a CHECK that was just dropped, under the new enum-expanded
    expression. ``check_expr`` is the raw expression body (NOT wrapped
    in ``CHECK (...)``); mirrors the contract of
    ``ColumnDefinition.check`` itself. Existing rows trivially satisfy
    the new CHECK because the old CHECK's value set is a strict subset
    of the new one (validated by ``schema_diff`` before this op is
    emitted), so no ``NOT VALID``/``VALIDATE CONSTRAINT`` dance is
    required.
    """
    full_table_name = build_table_name(namespace, table_name)
    return sql.SQL("ALTER TABLE {} ADD CONSTRAINT {} CHECK ({})").format(
        sql.Identifier(schema_name, full_table_name),
        sql.Identifier(constraint_name),
        sql.SQL(check_expr),  # type: ignore[arg-type]
    )


def build_default_unique_constraint_name(
    namespace: str, table_name: str, column_name: str,
) -> str:
    """Default Postgres-assigned constraint name for an inline column UNIQUE.

    Postgres auto-names an inline ``UNIQUE`` constraint declared as part of a
    column definition (``<col> ... UNIQUE``) as ``<full_table>_<column>_key``
    — the same convention ``adoption._is_constraint_backed`` relies on. The
    schema-diff unique-flip path needs this name to DROP the constraint when a
    column's ``unique`` flag flips to False (and to name the ADD when it flips
    to True). If a future schema declares its own named UNIQUE via a different
    surface, a parallel resolver is needed (mirrors the CHECK caveat above).
    """
    full_table = build_table_name(namespace, table_name)
    return f"{full_table}_{column_name}_key"


def emit_add_unique_constraint_op(
    namespace: str,
    table_name: str,
    constraint_name: str,
    column_name: str,
    schema_name: str,
) -> sql.Composed:
    """Emit ``ALTER TABLE … ADD CONSTRAINT <name> UNIQUE (<column>)``.

    Used by the unique-flip diff path in ``schema_diff`` to re-add a column
    UNIQUE constraint when a column's ``unique`` flag flips True. Postgres
    builds the backing unique index as part of the constraint; existing
    duplicate values fail the ADD loudly (no silent skip), which is the
    correct fast-fail behavior — unlike the additive-CHECK path, an
    ADD UNIQUE is NOT trivially satisfied by existing rows.
    """
    full_table_name = build_table_name(namespace, table_name)
    return sql.SQL("ALTER TABLE {} ADD CONSTRAINT {} UNIQUE ({})").format(
        sql.Identifier(schema_name, full_table_name),
        sql.Identifier(constraint_name),
        sql.Identifier(column_name),
    )


def emit_create_index_op(
    namespace: str,
    table_name: str,
    index: IndexDefinition,
    schema_name: str,
) -> sql.Composed:
    """Emit ``CREATE INDEX`` with the resolved physical name. Public wrapper
    around the internal helper so the diff layer can call it directly without
    going through the full ``emit_create_table_ops`` path.
    """
    return _emit_create_index(schema_name, namespace, table_name, index)


def emit_drop_index_op(
    physical_index_name: str,
    schema_name: str,
) -> sql.Composed:
    """Emit ``DROP INDEX IF EXISTS`` by *resolved physical name*.

    Always pass the resolved name from the ownership snapshot — never the
    bare logical ``IndexDefinition.name``. Multiple tables in a namespace can
    share a logical index name, so the bare name is ambiguous.
    """
    return sql.SQL("DROP INDEX IF EXISTS {}").format(
        sql.Identifier(schema_name, physical_index_name)
    )


def resolve_index_name(namespace: str, table_name: str, logical_index_name: str) -> str:
    """Return the resolved physical index name.

    The legacy path emitted indexes by their bare logical name (e.g.
    ``idx_status``), which collides across tables that share a logical name.
    The lifecycle path always uses the namespace-and-table-prefixed form so
    the physical name is unique per Postgres schema.

    Postgres truncates identifiers to ``POSTGRES_MAX_IDENTIFIER_LENGTH``
    characters on storage. When the full prefixed name would exceed that,
    return ``{head}_{digest}`` where digest is the first 8 hex chars of the
    SHA-1 of the untruncated name; the head is sized so the total is exactly
    the limit. The mapping is deterministic, so the same logical declaration
    always resolves to the same physical name, and the hash suffix prevents
    two long names from colliding when truncated to a common prefix.
    """
    full = f"{namespace}__{table_name}__{logical_index_name}"
    if len(full) <= POSTGRES_MAX_IDENTIFIER_LENGTH:
        return full
    digest = hashlib.sha1(full.encode("utf-8"), usedforsecurity=False).hexdigest()[:8]
    head_len = POSTGRES_MAX_IDENTIFIER_LENGTH - len(digest) - 1  # 1 for underscore
    return f"{full[:head_len]}_{digest}"


# --- private helpers -------------------------------------------------------


def _emit_create_table(
    schema_name: str, full_table_name: str, table: TableSchema
) -> sql.Composed:
    """Render the CREATE TABLE statement for ``table``."""
    column_parts: list[sql.Composed] = []
    for col_name, col_def in table.columns.items():
        column_parts.append(_emit_column(col_name, col_def))

    for constraint in table.check_constraints:
        column_parts.append(
            sql.SQL("CHECK ({})").format(sql.SQL(constraint))  # type: ignore[arg-type]
        )

    body = sql.SQL(", ").join(column_parts)
    return sql.SQL("CREATE TABLE IF NOT EXISTS {} ({})").format(
        sql.Identifier(schema_name, full_table_name), body
    )


def _emit_column(col_name: str, col_def: ColumnDefinition) -> sql.Composed:
    """Render one column definition as ``"col_name TYPE [constraints]"``.

    Type comes from the Postgres-native ``COLUMN_TYPE_MAP`` (so BOOLEAN is
    BOOLEAN, JSON is JSONB, DATETIME is TIMESTAMP). DEFAULT rendering is
    type-aware: BOOLEAN integer 0/1 → FALSE/TRUE; auto-timestamp on _at
    columns gets the standard ``NOW() AT TIME ZONE 'UTC'`` clause; otherwise
    the declared default is emitted as-is.
    """
    type_sql = _render_type(col_def)
    parts: list[sql.Composable] = [
        sql.Identifier(col_name),
        sql.SQL(type_sql),  # type: ignore[arg-type]
    ]

    if col_def.primary_key:
        parts.append(sql.SQL("PRIMARY KEY"))
    if col_def.not_null and not col_def.primary_key:
        parts.append(sql.SQL("NOT NULL"))
    if col_def.unique and not col_def.primary_key:
        parts.append(sql.SQL("UNIQUE"))

    default_clause = _render_default(col_name, col_def)
    if default_clause is not None:
        parts.append(default_clause)

    if col_def.check:
        parts.append(
            sql.SQL("CHECK ({})").format(sql.SQL(col_def.check))  # type: ignore[arg-type]
        )

    return sql.SQL(" ").join(parts)


def _render_type(col_def: ColumnDefinition) -> str:
    """Render the type fragment for a column.

    VECTOR with a ``dimension`` type_param renders as ``vector(384)``; all
    other types use the plain ``COLUMN_TYPE_MAP`` value.
    """
    base = get_postgres_type(col_def.type)
    if col_def.type == ColumnType.VECTOR and col_def.type_params:
        dimension = col_def.type_params.get("dimension")
        if dimension:
            return f"vector({dimension})"
    return base


_CONTRACT_DEFAULTS: dict[str, str | None] = {
    # Mirrors ColumnSqlGenerator._handle_contract_placeholder. Kept here so
    # the new renderer doesn't reach into the generic helper that emits
    # SQLite-flavored types.
    "__CONTRACT:auto_id_with_prefix__": None,  # ID is generated app-side, no DEFAULT
    "__CONTRACT:auto_timestamp_on_insert__": "DEFAULT (NOW() AT TIME ZONE 'UTC')",
    "__CONTRACT:auto_timestamp_on_update__": "DEFAULT (NOW() AT TIME ZONE 'UTC')",
}


def _render_default(col_name: str, col_def: ColumnDefinition) -> sql.Composable | None:
    """Render the DEFAULT clause for a column with type-aware coercion.

    Each helper handles one rule and returns ``None`` for "not my case", so the
    top-level dispatcher stays linear. Order matters: contract placeholders win
    over auto-timestamp; type-specific coercion wins over the generic literal path.
    """
    contract_clause = _render_contract_default(col_def.default)
    if contract_clause is not None:
        return contract_clause if contract_clause is not _NO_DEFAULT else None

    auto_ts = _render_auto_timestamp(col_name, col_def)
    if auto_ts is not None:
        return auto_ts

    if col_def.default is None:
        return None

    typed = _render_typed_literal(col_def.type, col_def.default)
    if typed is not None:
        return typed

    if isinstance(col_def.default, str) and _SQL_FUNCTION_DETECTOR.is_sql_function(col_def.default):
        return sql.SQL("DEFAULT " + col_def.default)  # type: ignore[arg-type]

    return sql.SQL("DEFAULT {}").format(sql.Literal(col_def.default))


# Sentinel: a contract placeholder said "no DEFAULT clause at all" (e.g. ID columns).
# Distinguished from None ("this rule didn't apply, try the next one").
_NO_DEFAULT: sql.Composable = sql.SQL("")


def _render_contract_default(default: object) -> sql.Composable | None:
    """Map ``__CONTRACT:*__`` placeholder strings to platform contract clauses.

    Returns the rendered clause for a known contract; ``_NO_DEFAULT`` when the
    contract resolves to "no DEFAULT" (ID columns); ``None`` when the value
    isn't a contract placeholder.
    """
    if not (isinstance(default, str) and default.startswith("__CONTRACT:") and default.endswith("__")):
        return None
    contract_clause = _CONTRACT_DEFAULTS.get(default)
    if contract_clause is None:
        if default not in _CONTRACT_DEFAULTS:
            logger.warning("Unrecognized contract placeholder default: %s", default)
        return _NO_DEFAULT
    return sql.SQL(contract_clause)  # type: ignore[arg-type]


def _render_auto_timestamp(col_name: str, col_def: ColumnDefinition) -> sql.Composable | None:
    """Apply the audit-timestamp convention: the canonical audit pair
    (``created_at`` / ``updated_at``) with undeclared default gets
    ``NOW() AT TIME ZONE 'UTC'``. Other ``_at`` columns are NOT auto-defaulted
    so nullable-semantic columns (e.g. ``acknowledged_at``, ``closed_at``,
    ``restoration_at``) can land NULL when the row is created. Per the
    2026-06-12 Tier 1.A audit-timestamp design (Option B): the implicit
    name-based broadening to every ``_at`` column was a silent footgun that
    backfilled ADD COLUMN migrations with the migration moment and stamped
    every INSERT with ``now()`` regardless of operational intent. Explicit
    opt-in via ``__CONTRACT:auto_timestamp_on_insert__`` /
    ``__CONTRACT:auto_timestamp_on_update__`` placeholders remains the way
    for non-pair columns to request the auto-stamp.
    """
    if (
        col_def.type == ColumnType.DATETIME
        and col_name in {"created_at", "updated_at"}
        and col_def.default is None
    ):
        return sql.SQL("DEFAULT (NOW() AT TIME ZONE 'UTC')")
    return None


def _render_typed_literal(col_type: ColumnType, default: object) -> sql.Composable | None:
    """Render type-specific literal defaults (BOOLEAN coercion, JSON cast).

    Returns ``None`` when no type-specific rule applies.
    """
    if col_type == ColumnType.BOOLEAN:
        return _render_boolean_default(default)
    if col_type == ColumnType.JSON and isinstance(default, str):
        return sql.SQL("DEFAULT {}::jsonb").format(sql.Literal(default))
    return None


def _render_boolean_default(default: object) -> sql.Composable | None:
    """Coerce 0/1/bool to ``DEFAULT FALSE``/``DEFAULT TRUE``.

    Plugin schemas historically declare boolean defaults as integer 0/1;
    Postgres rejects ``DEFAULT 0`` on a true BOOLEAN column.
    """
    if isinstance(default, bool):
        return sql.SQL("DEFAULT TRUE") if default else sql.SQL("DEFAULT FALSE")
    if isinstance(default, int) and default in (0, 1):
        return sql.SQL("DEFAULT TRUE") if default == 1 else sql.SQL("DEFAULT FALSE")
    return None


def _emit_updated_at_trigger(schema_name: str, full_table_name: str) -> sql.Composed:
    """Render the standard ``updated_at`` BEFORE-UPDATE trigger.

    The trigger function ``update_updated_at_column()`` is created once at
    provider initialization; here we just bind a per-table trigger to it.
    Drops any existing trigger of the same name first so the op is idempotent.
    """
    trigger_name = f"{full_table_name}_update_updated_at"
    return sql.SQL(
        "DROP TRIGGER IF EXISTS {trig} ON {tbl}; "
        "CREATE TRIGGER {trig} "
        "BEFORE UPDATE ON {tbl} "
        "FOR EACH ROW "
        "EXECUTE FUNCTION {fn}();"
    ).format(
        trig=sql.Identifier(trigger_name),
        tbl=sql.Identifier(schema_name, full_table_name),
        fn=sql.Identifier(schema_name, "update_updated_at_column"),
    )


def _emit_create_index(
    schema_name: str,
    namespace: str,
    table_name: str,
    index: IndexDefinition,
) -> sql.Composed:
    """Render ``CREATE INDEX`` with the resolved physical name.

    Honors three IndexDefinition fields that earlier renderer revisions
    silently dropped:

    * ``index.using`` — index method (``btree`` (default), ``gin``,
      ``gist``, ``brin``, ``hash``). Emitted as ``USING <method>``.
    * ``index.column_operator_classes`` — per-column operator-class
      annotation (e.g. ``{"content_text": "gin_trgm_ops"}``). Emitted
      inline against the column in the column list.
    * ``index.where`` — partial-index predicate (already supported).

    The M21 trigram-search ``idx_event_content_text_trgm`` IndexDefinition
    was the first to exercise ``using`` + ``column_operator_classes``;
    pre-fix the renderer emitted a plain btree on a wide TEXT column
    (``content_text`` runs to ~310 KB on the operator's live ledger),
    which exceeded the 8191-byte btree row limit and aborted apply.
    """
    full_table_name = build_table_name(namespace, table_name)
    physical_name = resolve_index_name(namespace, table_name, index.name)

    unique = sql.SQL("UNIQUE ") if index.unique else sql.SQL("")
    using = (
        sql.SQL(" USING {}").format(sql.SQL(index.using))  # type: ignore[arg-type]
        if index.using
        else sql.SQL("")
    )
    opclasses = index.column_operator_classes or {}
    col_terms: list[sql.Composable] = []
    for c in index.columns:
        if c in opclasses:
            col_terms.append(
                sql.SQL("{} {}").format(
                    sql.Identifier(c),
                    sql.SQL(opclasses[c]),  # type: ignore[arg-type]
                )
            )
        else:
            col_terms.append(sql.Identifier(c))
    column_list = sql.SQL(", ").join(col_terms)

    # W5.E §5.2 G2: render ``WITH (k = v, ...)`` index reloptions when
    # the IndexDefinition carries build-time tuning (HNSW ``m`` /
    # ``ef_construction``; BRIN ``pages_per_range``; etc.). Values are
    # emitted as bare SQL — pgvector + Postgres accept int/float/bool
    # via ``str()``; keys are bare identifiers per the Postgres reloption
    # grammar (no quoting).
    with_opts = index.index_with_options or {}
    if with_opts:
        with_pairs = sql.SQL(", ").join(
            sql.SQL("{} = {}").format(
                sql.SQL(k),  # type: ignore[arg-type]
                sql.SQL(str(v)),  # type: ignore[arg-type]
            )
            for k, v in with_opts.items()
        )
        with_clause = sql.SQL(" WITH ({})").format(with_pairs)
    else:
        with_clause = sql.SQL("")

    if index.where:
        where_sql = sql.SQL(index.where)  # type: ignore[arg-type]
        return sql.SQL(
            "CREATE {uniq}INDEX IF NOT EXISTS {idx} ON {tbl}{using} "
            "({cols}){with_opts} WHERE {where}"
        ).format(
            uniq=unique,
            idx=sql.Identifier(physical_name),
            tbl=sql.Identifier(schema_name, full_table_name),
            using=using,
            cols=column_list,
            with_opts=with_clause,
            where=where_sql,
        )

    return sql.SQL(
        "CREATE {uniq}INDEX IF NOT EXISTS {idx} ON {tbl}{using} "
        "({cols}){with_opts}"
    ).format(
        uniq=unique,
        idx=sql.Identifier(physical_name),
        tbl=sql.Identifier(schema_name, full_table_name),
        using=using,
        cols=column_list,
        with_opts=with_clause,
    )
