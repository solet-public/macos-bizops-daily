"""Adoption of pre-existing live tables into the plugin-schema lifecycle.

When ``install_plugin_schema`` finds tables on disk for a namespace that has
no ownership row, the lifecycle adopts them rather than failing or silently
ignoring divergence. This module provides the introspection + reconciliation
primitives the lifecycle uses to do so safely:

  1. **Column-type adoption.** Compare each live column's physical Postgres
     type against the declared ``ColumnType``. Apply the whitelisted legacy
     normalizations (``INTEGER`` → ``BOOLEAN``, ``JSON`` → ``JSONB``) via the
     four-step subroutine: preflight-validate the data, drop the existing
     default, ``ALTER COLUMN TYPE`` with the safe ``USING`` clause, re-add
     the declared default in the new type's native form. Anything outside
     the whitelist is a divergence: fail-fast with a recovery message.

  2. **Index adoption.** Introspect ``pg_indexes`` per table. For each
     declared index, look up an on-disk index that matches by shape
     (columns + ``unique`` + ``where``):
       * Match found, name already follows the resolved convention → no-op.
       * Match found, legacy bare logical name → ``ALTER INDEX … RENAME``.
       * No match → ``CREATE INDEX`` (covers the legacy "duplicate logical
         name across tables silently skipped" case).
       * Match found by name with different shape → divergence, fail-fast.

  3. **Snapshot.** After the DDL ops above (run in the same transaction as
     the ownership-row INSERTs), record per-table snapshots whose declared
     index list carries the resolved physical name alongside each entry.

This module is pure-emission and introspection. The lifecycle owns the
transactional apply + ownership writes.
"""

from __future__ import annotations

import logging
import re
from typing import Any

from ananta.types.column_types import ColumnType
from ananta.types.schema_types import (
    ColumnDefinition,
    IndexDefinition,
    TableSchema,
)
from psycopg import sql
from psycopg.rows import dict_row

from .ddl_renderer import emit_create_index_op, resolve_index_name
from .utils import build_table_name

logger = logging.getLogger(__name__)


# Logical type → set of acceptable live Postgres data_type strings.
# Used to decide whether a live column is "already correct" for the declared
# type. Keep loose where Postgres has multiple aliases.
_DECLARED_TO_LIVE_OK: dict[ColumnType, set[str]] = {
    ColumnType.TEXT: {"text"},
    ColumnType.INTEGER: {"integer", "bigint", "smallint"},
    ColumnType.REAL: {"double precision", "real"},
    ColumnType.BLOB: {"bytea"},
    ColumnType.DATETIME: {"timestamp without time zone", "timestamp with time zone"},
    ColumnType.BOOLEAN: {"boolean"},
    ColumnType.VECTOR: {"USER-DEFINED"},  # pgvector extension type
    ColumnType.JSON: {"jsonb"},
}

# Legacy → declared transitions the adoption path knows how to normalize.
# Each entry: declared logical type → set of live physical types that are
# "legacy" (still acceptable per pre-v6 emission) and need to be normalized.
_LEGACY_NORMALIZATIONS: dict[ColumnType, set[str]] = {
    ColumnType.BOOLEAN: {"integer"},
    ColumnType.JSON: {"json", "text"},
}


class AdoptionDivergenceError(RuntimeError):
    """Live shape diverges from declared in a way the lifecycle won't fix automatically."""


def introspect_table_columns(
    conn: Any, schema_name: str, full_table_name: str
) -> dict[str, dict[str, Any]]:
    """Read live column types + defaults for one table.

    Returns ``{column_name: {"data_type": <pg_type>, "column_default": <text|None>}}``.
    """
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            "SELECT column_name, data_type, column_default "
            "FROM information_schema.columns "
            "WHERE table_schema = %s AND table_name = %s",
            (schema_name, full_table_name),
        )
        return {
            row["column_name"]: {
                "data_type": row["data_type"],
                "column_default": row["column_default"],
            }
            for row in cur.fetchall()
        }


def introspect_table_indexes(
    conn: Any, schema_name: str, full_table_name: str
) -> list[dict[str, Any]]:
    """Read live indexes for one table from ``pg_indexes`` + ``pg_index``.

    Returns a list of ``{"name": physical_name, "columns": [...],
    "unique": bool, "where": text|None, "is_primary": bool, "using":
    str|None, "column_operator_classes": {col: opclass, ...},
    "index_with_options": {key: str, ...}}`` dicts. Excludes
    constraint-backed indexes the platform manages implicitly (PRIMARY
    KEY, UNIQUE constraint on ``external_id``).

    W5.E §5.2 G2: the `using` / `column_operator_classes` /
    `index_with_options` fields are extracted so adoption can match a
    declared HNSW index against the live one without falsely matching
    it against a default-btree on the same column. `column_exprs`
    already carries the per-column opclass when non-default (e.g.
    ``content_text gin_trgm_ops``); we split it. `reloptions` comes
    from ``pg_class.reloptions`` as a TEXT[] of ``key=value`` strings.
    """
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            """
            SELECT
                i.indexname AS name,
                i.indexdef AS def,
                idx.indisunique AS is_unique,
                idx.indisprimary AS is_primary,
                pg_get_expr(idx.indpred, idx.indrelid) AS where_clause,
                c.reloptions AS reloptions,
                array(
                    SELECT pg_get_indexdef(idx.indexrelid, k + 1, true)
                    FROM generate_subscripts(idx.indkey, 1) AS k
                ) AS column_exprs
            FROM pg_indexes i
            JOIN pg_class c ON c.relname = i.indexname
                AND c.relnamespace = (
                    SELECT oid FROM pg_namespace WHERE nspname = i.schemaname
                )
            JOIN pg_index idx ON idx.indexrelid = c.oid
            WHERE i.schemaname = %s AND i.tablename = %s
            ORDER BY i.indexname
            """,
            (schema_name, full_table_name),
        )
        rows = cur.fetchall()
    result: list[dict[str, Any]] = []
    for row in rows:
        columns, opclasses = _split_column_exprs(list(row["column_exprs"]))
        result.append(
            {
                "name": row["name"],
                "columns": columns,
                "unique": bool(row["is_unique"]),
                "where": row["where_clause"],
                "is_primary": bool(row["is_primary"]),
                "using": _parse_using_method(row["def"]),
                "column_operator_classes": opclasses,
                "index_with_options": _parse_reloptions(row["reloptions"]),
            }
        )
    return result


def _parse_using_method(indexdef: str | None) -> str | None:
    """Extract the ``USING <method>`` clause from a ``pg_indexes.indexdef`` row.

    Returns the method name lowercased (e.g. ``btree`` / ``gin`` /
    ``hnsw``), or ``None`` when the indexdef is missing or does not
    name a method (very old Postgres rows).
    """
    if not indexdef:
        return None
    match = re.search(r"\bUSING\s+(\w+)", indexdef, re.IGNORECASE)
    return match.group(1).lower() if match else None


def _split_column_exprs(
    column_exprs: list[str],
) -> tuple[list[str], dict[str, str]]:
    """Split ``[\"col1\", \"col2 opclass\"]`` into bare columns + opclass map.

    ``pg_get_indexdef(indexrelid, k+1, true)`` returns the per-column
    indexed expression with its operator class appended when the
    opclass is NON-default for the index method. So
    ``content_text gin_trgm_ops`` becomes ``("content_text",
    "gin_trgm_ops")``; a default-btree column like ``event_at`` stays
    bare.
    """
    columns: list[str] = []
    opclasses: dict[str, str] = {}
    for expr in column_exprs:
        parts = expr.strip().split()
        if len(parts) >= 2:
            col = parts[0]
            opclass = parts[1]
            columns.append(col)
            opclasses[col] = opclass
        else:
            columns.append(expr.strip())
    return columns, opclasses


def _parse_reloptions(reloptions: list[str] | None) -> dict[str, str]:
    """Parse ``pg_class.reloptions`` (``["k1=v1", "k2=v2"]``) into a dict.

    Returns ``{}`` when the live index has no reloptions set. Values
    surface as strings — the W5.E options-comparison invariant
    documented in the design memo §5.2 normalizes BOTH sides to string
    form at comparison time so declared ``{"m": 16}`` matches live
    ``["m=16"]``.
    """
    if not reloptions:
        return {}
    parsed: dict[str, str] = {}
    for entry in reloptions:
        if "=" not in entry:
            continue
        key, _, value = entry.partition("=")
        parsed[key.strip()] = value.strip()
    return parsed


def plan_column_normalizations(
    namespace: str,
    table: TableSchema,
    live_columns: dict[str, dict[str, Any]],
    schema_name: str,
) -> tuple[list[sql.Composed], list[tuple[str, ColumnType, str]]]:
    """Plan column-type adoption: ops + preflight specs.

    Returns ``(ops, preflight_specs)``:

      * ``ops``: ordered DDL ops for the four-step normalization subroutine
        (one ``DROP DEFAULT`` + one ``ALTER COLUMN TYPE`` + optional ``SET DEFAULT``
        per legacy column).
      * ``preflight_specs``: list of ``(col_name, declared_type, live_type)``
        tuples — caller iterates and runs ``preflight_normalization`` for each
        on the transactional connection before applying the ops.

    Raises :class:`AdoptionDivergenceError` for column-level mismatches outside the
    whitelisted legacy normalizations.
    """
    ops: list[sql.Composed] = []
    preflights: list[tuple[str, ColumnType, str]] = []

    for col_name, declared_col in table.columns.items():
        live = live_columns.get(col_name)
        if live is None:
            # Declared column missing on disk — adoption-time gap. The diff
            # path would handle this as ADD COLUMN in update mode, but
            # adoption is for "live matches declared shape" — surface as
            # divergence and let the operator decide.
            raise AdoptionDivergenceError(
                f"adoption: declared column {namespace}.{table.table_name}.{col_name} "
                "is not present on disk. Run uninstall_plugin_schema then re-install, "
                "or add the column manually before retry."
            )

        live_type = (live["data_type"] or "").lower()
        if live_type in {t.lower() for t in _DECLARED_TO_LIVE_OK[declared_col.type]}:
            continue  # already correct

        legacy_ok = _LEGACY_NORMALIZATIONS.get(declared_col.type, set())
        if live_type not in legacy_ok:
            raise AdoptionDivergenceError(
                f"adoption: column {namespace}.{table.table_name}.{col_name} has "
                f"physical type {live_type!r} but declared type is "
                f"{declared_col.type.name} (expected one of "
                f"{sorted(_DECLARED_TO_LIVE_OK[declared_col.type])}). "
                "Outside the whitelisted legacy normalizations "
                "(INTEGER→BOOLEAN, JSON→JSONB). Reconcile manually before retry."
            )

        ops.extend(
            _emit_normalize_column_ops(
                namespace, table.table_name, col_name, declared_col, live, schema_name
            )
        )
        preflights.append((col_name, declared_col.type, live_type))

    # Detect extra columns on disk that aren't in the declared schema.
    standard_extra = set(live_columns) - set(table.columns)
    if standard_extra:
        # SchemaStandardizer should have added the same standard columns
        # whether we're adopting or fresh-installing, so a mismatch here is
        # genuine divergence — not noise.
        raise AdoptionDivergenceError(
            f"adoption: live table {namespace}.{table.table_name} has columns "
            f"{sorted(standard_extra)} that are not in the declared schema. "
            "(Standard fields should have been added by SchemaStandardizer; if "
            "they're missing from declared, that's a SchemaStandardizer bug.) "
            "Reconcile manually before retry."
        )

    if preflights:
        logger.info(
            "adoption: %s.%s — %d legacy column-type normalization(s) planned",
            namespace, table.table_name, len(preflights),
        )
    return ops, preflights


def _emit_normalize_column_ops(
    namespace: str,
    table_name: str,
    col_name: str,
    declared_col: ColumnDefinition,
    live: dict[str, Any],
    schema_name: str,
) -> list[sql.Composed]:
    """Emit the four-step normalization for one column.

    For BOOLEAN: preflight ``WHERE col NOT IN (0, 1)`` (raise on dirty data),
    DROP DEFAULT, ALTER COLUMN TYPE BOOLEAN USING (col::int::bool),
    optionally re-add typed default.

    For JSONB: preflight by attempting cast inside savepoint (only if live
    type is ``text`` — ``json`` parseability is guaranteed by the column type
    already), DROP DEFAULT, ALTER COLUMN TYPE JSONB USING (col::jsonb),
    optionally re-add typed default.

    Note: preflight is enforced by the lifecycle (which holds the connection)
    just before applying these ops — see ``preflight_normalization``. This
    function emits only the apply ops; preflight checks return errors out-of-band.
    """
    full_table_name = build_table_name(namespace, table_name)
    table_id = sql.Identifier(schema_name, full_table_name)
    col_id = sql.Identifier(col_name)

    drop_default = sql.SQL("ALTER TABLE {} ALTER COLUMN {} DROP DEFAULT").format(
        table_id, col_id
    )

    if declared_col.type == ColumnType.BOOLEAN:
        type_change = sql.SQL(
            "ALTER TABLE {} ALTER COLUMN {} TYPE BOOLEAN USING ({}::int::bool)"
        ).format(table_id, col_id, col_id)
    elif declared_col.type == ColumnType.JSON:
        type_change = sql.SQL(
            "ALTER TABLE {} ALTER COLUMN {} TYPE JSONB USING ({}::jsonb)"
        ).format(table_id, col_id, col_id)
    else:  # pragma: no cover — guarded by caller
        raise AssertionError(f"no normalization rule for {declared_col.type.name}")

    ops: list[sql.Composed] = [drop_default, type_change]

    new_default_clause = _render_typed_default_for_normalize(declared_col)
    if new_default_clause is not None:
        ops.append(
            sql.SQL("ALTER TABLE {} ALTER COLUMN {} SET ").format(table_id, col_id)
            + new_default_clause
        )

    return ops


def _render_typed_default_for_normalize(
    col_def: ColumnDefinition,
) -> sql.Composable | None:
    """Render the post-normalization DEFAULT in the new type's native form.

    Mirrors ``ddl_renderer._render_default`` for the BOOLEAN / JSONB cases.
    Returns ``None`` if there's no declared default (column ends up with no
    DEFAULT clause, which is correct).
    """
    default = col_def.default
    if default is None:
        return None

    if col_def.type == ColumnType.BOOLEAN:
        if isinstance(default, bool):
            return sql.SQL("DEFAULT TRUE") if default else sql.SQL("DEFAULT FALSE")
        if isinstance(default, int) and default in (0, 1):
            return sql.SQL("DEFAULT TRUE") if default == 1 else sql.SQL("DEFAULT FALSE")

    if col_def.type == ColumnType.JSON and isinstance(default, str):
        return sql.SQL("DEFAULT {}::jsonb").format(sql.Literal(default))

    return sql.SQL("DEFAULT {}").format(sql.Literal(default))


def preflight_normalization(
    conn: Any,
    namespace: str,
    table_name: str,
    col_name: str,
    declared_type: ColumnType,
    live_type: str,
    schema_name: str,
) -> None:
    """Validate that the legacy → declared cast won't lose data.

    For ``INTEGER → BOOLEAN``: assert no row has a non-{0,1} value.
    For ``text → JSONB``: attempt the cast for all rows; raise on any
    invalid_text_representation. (For ``json → JSONB``, parseability is
    already guaranteed by the column type — no preflight needed.)

    Raises :class:`AdoptionDivergenceError` on dirty data with a recovery message.
    """
    full_table_name = build_table_name(namespace, table_name)
    table_id = sql.Identifier(schema_name, full_table_name)
    col_id = sql.Identifier(col_name)

    if declared_type == ColumnType.BOOLEAN and live_type == "integer":
        with conn.cursor() as cur:
            cur.execute(
                sql.SQL(
                    "SELECT COUNT(*) AS dirty FROM {} WHERE {} IS NOT NULL AND {} NOT IN (0, 1)"
                ).format(table_id, col_id, col_id)
            )
            row = cur.fetchone()
        # Tolerate either dict (dict_row) or tuple (default) row factory
        dirty_count = row["dirty"] if isinstance(row, dict) else row[0]
        if dirty_count:
            raise AdoptionDivergenceError(
                f"adoption preflight failed: {namespace}.{table_name}.{col_name} "
                f"has {dirty_count} row(s) with values not in {{0, 1}}; cannot "
                "safely cast INTEGER → BOOLEAN. Reconcile data manually before retry."
            )
        return

    if declared_type == ColumnType.JSON and live_type == "text":
        with conn.cursor() as cur:
            try:
                cur.execute("SAVEPOINT _adoption_preflight")
                cur.execute(
                    sql.SQL("SELECT {}::jsonb FROM {} WHERE {} IS NOT NULL").format(
                        col_id, table_id, col_id
                    )
                )
            except Exception as exc:
                cur.execute("ROLLBACK TO SAVEPOINT _adoption_preflight")
                raise AdoptionDivergenceError(
                    f"adoption preflight failed: {namespace}.{table_name}.{col_name} "
                    f"has values that don't parse as JSON ({type(exc).__name__}). "
                    "Reconcile data manually before retry."
                ) from exc
            else:
                cur.execute("RELEASE SAVEPOINT _adoption_preflight")
        return

    # No preflight needed for other (or no-op) cases
    return


def plan_index_reconciliation(
    namespace: str,
    table: TableSchema,
    live_indexes: list[dict[str, Any]],
    schema_name: str,
) -> tuple[list[sql.Composed], dict[str, str]]:
    """Decide rename + create + (refuse) for adoption-time index reconciliation.

    Returns ``(ops, physical_names)``: the ordered ``ALTER INDEX … RENAME`` and
    ``CREATE INDEX`` statements to reconcile to the resolved-name convention,
    plus the ``{logical_name: physical_name}`` map recorded in the snapshot.

    Raises :class:`AdoptionDivergenceError` if a live index has the resolved name
    but a different shape than declared.
    """
    user_indexes = _filter_user_indexes(live_indexes)
    ops: list[sql.Composed] = []
    physical_names: dict[str, str] = {}

    for declared_idx in table.indexes:
        resolved_name = resolve_index_name(namespace, table.table_name, declared_idx.name)
        physical_names[declared_idx.name] = resolved_name
        op = _plan_one_index(namespace, table.table_name, declared_idx, user_indexes, schema_name)
        if op is not None:
            ops.append(op)

    _check_resolved_name_divergences(namespace, table, user_indexes, schema_name)
    return ops, physical_names


def _filter_user_indexes(live_indexes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Drop primary-key and constraint-backed indexes — adoption shouldn't touch those."""
    return [idx for idx in live_indexes if not idx["is_primary"] and not _is_constraint_backed(idx)]


def _plan_one_index(
    namespace: str,
    table_name: str,
    declared_idx: IndexDefinition,
    user_indexes: list[dict[str, Any]],
    schema_name: str,
) -> sql.Composed | None:
    """Decide the single op (or no-op) for one declared index.

    Returns the ``ALTER INDEX … RENAME`` or ``CREATE INDEX`` op, or ``None``
    when the live state already matches.
    """
    resolved_name = resolve_index_name(namespace, table_name, declared_idx.name)
    match = _find_index_by_shape(declared_idx, user_indexes)

    if match is None:
        # Legacy duplicate-name skip (the pre-v6 CREATE IF NOT EXISTS no-op'd
        # because another table already had that bare logical name) — create it.
        logger.info(
            "adoption: %s.%s — creating missing declared index %r as %r",
            namespace, table_name, declared_idx.name, resolved_name,
        )
        return emit_create_index_op(namespace, table_name, declared_idx, schema_name)

    if match["name"] == resolved_name:
        return None  # Already correctly named

    # Live index matches shape but uses legacy bare name → rename
    logger.info(
        "adoption: %s.%s — renaming legacy index %r → %r",
        namespace, table_name, match["name"], resolved_name,
    )
    return sql.SQL("ALTER INDEX {} RENAME TO {}").format(
        sql.Identifier(schema_name, match["name"]),
        sql.Identifier(resolved_name),
    )


def _check_resolved_name_divergences(
    namespace: str,
    table: TableSchema,
    user_indexes: list[dict[str, Any]],
    schema_name: str,
) -> None:
    """Raise if a live index uses a declared resolved-name but the wrong shape.

    A real divergence — operator must drop the index manually before retry.
    """
    full_table_name = build_table_name(namespace, table.table_name)
    declared_by_resolved = {
        resolve_index_name(namespace, table.table_name, idx.name): idx for idx in table.indexes
    }
    for live in user_indexes:
        declared_idx = declared_by_resolved.get(live["name"])
        if declared_idx is None:
            continue
        if not _index_matches_shape(declared_idx, live):
            raise AdoptionDivergenceError(
                f"adoption: live index {schema_name}.{live['name']} on "
                f"{full_table_name} has the resolved name for declared "
                f"index {declared_idx.name!r} but a different shape "
                f"(live columns={live['columns']}, unique={live['unique']}, "
                f"where={live['where']!r}). Drop the index manually and retry."
            )


def _find_index_by_shape(
    declared: IndexDefinition, live_indexes: list[dict[str, Any]]
) -> dict[str, Any] | None:
    """Return the first live index whose shape matches the declared one."""
    for live in live_indexes:
        if _index_matches_shape(declared, live):
            return live
    return None


def _index_matches_shape(declared: IndexDefinition, live: dict[str, Any]) -> bool:
    """Compare declared IndexDefinition shape against an introspected live index.

    W5.E §5.2 G2: shape comparison now covers `using` (index method),
    `column_operator_classes` (per-column opclass map), and
    `index_with_options` (build-time reloptions). Without these,
    declared HNSW would falsely match a live default btree on the
    same column. Method match is case-insensitive (pg_indexes lowercases
    it; declarations might use mixed-case). Reloptions normalize to
    ``{key: str(value)}`` form on BOTH sides — declared values may be
    typed (``{"m": 16}``); live values surface as strings
    (``["m=16"]``).
    """
    return (
        list(declared.columns) == list(live["columns"])
        and bool(declared.unique) == bool(live["unique"])
        and _where_clauses_match(declared.where, live.get("where"))
        and _methods_match(declared.using, live.get("using"))
        and (declared.column_operator_classes or {})
        == (live.get("column_operator_classes") or {})
        and _normalize_index_options(declared.index_with_options)
        == _normalize_index_options(live.get("index_with_options"))
    )


def _where_clauses_match(declared: str | None, live: str | None) -> bool:
    """Both empty or both non-empty matches; one-sided WHERE diverges.

    Postgres normalizes WHERE expressions in pg_get_expr output, so
    exact string match is fragile — operator validates semantic
    equivalence when both sides carry a WHERE clause.
    """
    return ((declared or "").strip() == "") == ((live or "").strip() == "")


def _methods_match(declared: str | None, live: str | None) -> bool:
    """Compare index methods, treating declared None as btree default."""
    declared_method = (declared or "btree").lower()
    live_method = (live or "btree").lower()
    return declared_method == live_method


def _normalize_index_options(opts: dict[str, object] | None) -> dict[str, str]:
    """Coerce index reloptions to ``{key: str(value)}`` for type-safe match."""
    if not opts:
        return {}
    return {k: str(v) for k, v in opts.items()}


def _is_constraint_backed(live_index: dict[str, Any]) -> bool:
    """Heuristic: skip indexes that back UNIQUE/PK constraints from standard fields.

    These names follow Postgres's auto-generated convention (``<table>_pkey``,
    ``<table>_<col>_key``). Not all of those are constraints — some are
    user-declared with conflicting names — but for the platform's standardized
    fields the convention holds.
    """
    name = live_index["name"]
    return name.endswith("_pkey") or name.endswith("_key")


# ---------------------------------------------------------------------------
# Foreign-key reconciliation (W5.P §3.2)
# ---------------------------------------------------------------------------

def _fk_constraint_name(namespace: str, table_name: str, col_name: str) -> str:
    """Constraint-naming convention: ``<plugin_namespace>__<table>__<col>_fkey``.

    Mirrors the index-naming pattern from W5.D §2.1; keeps the constraint
    grep-able and avoids name collisions across plugins that happen to use
    the same column name.
    """
    return f"{namespace}__{table_name}__{col_name}_fkey"


def introspect_table_foreign_keys(
    conn: Any, schema_name: str, full_table_name: str,
) -> list[dict[str, Any]]:
    """Read live FK constraints on one table.

    Returns ``[{constraint_name, column_name, target_table, target_column,
    on_delete, on_update}, ...]``. Column-level FKs only; composite FKs are
    not modeled by ``ColumnDefinition.foreign_key`` (would surface as
    multiple rows here and trigger a shape mismatch on reconciliation).
    """
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            """
            SELECT
                rc.constraint_name AS constraint_name,
                kcu.column_name AS column_name,
                ccu.table_name AS target_table,
                ccu.column_name AS target_column,
                rc.delete_rule AS on_delete,
                rc.update_rule AS on_update
            FROM information_schema.referential_constraints rc
            JOIN information_schema.key_column_usage kcu
                ON kcu.constraint_name = rc.constraint_name
                AND kcu.constraint_schema = rc.constraint_schema
            JOIN information_schema.constraint_column_usage ccu
                ON ccu.constraint_name = rc.constraint_name
                AND ccu.constraint_schema = rc.constraint_schema
            WHERE kcu.table_schema = %s
              AND kcu.table_name = %s
            ORDER BY rc.constraint_name, kcu.ordinal_position
            """,
            (schema_name, full_table_name),
        )
        return [dict(row) for row in cur.fetchall()]


def plan_fk_reconciliation(
    namespace: str,
    table: TableSchema,
    live_constraints: list[dict[str, Any]],
    schema_name: str,
) -> list[sql.Composed]:
    """Diff declared vs live FK constraints; emit ADD CONSTRAINT ops for adds.

    Per W5.P §3.2:

    * Declared FK matches a live constraint with same target + same
      ON DELETE: no-op (already correct).
    * Declared FK matches by column but different target or ON DELETE:
      raise :class:`AdoptionDivergenceError` (operator must drop and
      re-add manually).
    * Declared FK has no matching live constraint: emit
      ``ALTER TABLE … ADD CONSTRAINT … FOREIGN KEY … REFERENCES …``.
    * Live constraint NOT in the declared set: log warning; v1 does NOT
      drop (symmetric refusal of unauthorized drift, per W5.D §2.1
      Codex re-review §15).

    Returns the ordered list of ALTER TABLE ops to apply. Caller runs
    them inside the same transaction as the ownership-row writes.
    """
    full_table_name = build_table_name(namespace, table.table_name)
    table_id = sql.Identifier(schema_name, full_table_name)
    live_by_column = _index_live_fks_by_column(live_constraints)
    ops: list[sql.Composed] = []
    declared_columns: set[str] = set()

    for col_name, col_def in table.columns.items():
        if col_def.foreign_key is None:
            continue
        declared_columns.add(col_name)
        target_table, target_column = col_def.foreign_key
        live = live_by_column.get(col_name)
        if live is not None:
            _check_fk_shape_match(
                namespace, table.table_name, col_name,
                target_table, target_column,
                col_def.on_delete, col_def.on_update,
                live,
            )
            continue
        constraint_name = _fk_constraint_name(namespace, table.table_name, col_name)
        ops.append(
            sql.SQL(
                "ALTER TABLE {table} ADD CONSTRAINT {constraint} "
                "FOREIGN KEY ({col}) REFERENCES {target_table}({target_col}) "
                "ON DELETE {on_delete} ON UPDATE {on_update}",
            ).format(
                table=table_id,
                constraint=sql.Identifier(constraint_name),
                col=sql.Identifier(col_name),
                target_table=sql.Identifier(schema_name, target_table),
                target_col=sql.Identifier(target_column),
                on_delete=sql.SQL(col_def.on_delete),
                on_update=sql.SQL(col_def.on_update),
            )
        )
        logger.info(
            "adoption: %s.%s — adding FK %r on column %r → %s(%s) "
            "ON DELETE %s ON UPDATE %s",
            namespace, table.table_name, constraint_name, col_name,
            target_table, target_column, col_def.on_delete, col_def.on_update,
        )

    _warn_undeclared_live_fks(
        namespace, table.table_name, live_constraints, declared_columns,
    )
    return ops


def _index_live_fks_by_column(
    live_constraints: list[dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    """Group live FK rows by column_name (composite FKs are flagged below)."""
    by_column: dict[str, dict[str, Any]] = {}
    for live in live_constraints:
        col = live["column_name"]
        if col in by_column:
            # A second row with the same column means a composite FK whose
            # first column was the same; ColumnDefinition.foreign_key models
            # only single-column FKs, so reconciliation would surface this
            # as a shape mismatch at the per-column check below. Stash both
            # for inspection (the check will refuse).
            by_column[col]["_composite"] = True
            continue
        by_column[col] = dict(live)
    return by_column


def _check_fk_shape_match(
    namespace: str,
    table_name: str,
    col_name: str,
    declared_target_table: str,
    declared_target_column: str,
    declared_on_delete: str,
    declared_on_update: str,
    live: dict[str, Any],
) -> None:
    """Raise :class:`AdoptionDivergenceError` when live FK shape differs from declared."""
    if live.get("_composite"):
        raise AdoptionDivergenceError(
            f"adoption FK: {namespace}.{table_name}.{col_name} has a composite "
            f"live FK constraint; ColumnDefinition.foreign_key models only "
            "single-column FKs. Reconcile manually before retry."
        )
    live_target_table = str(live["target_table"])
    live_target_column = str(live["target_column"])
    live_on_delete = str(live["on_delete"]).upper()
    live_on_update = str(live["on_update"]).upper()
    declared_on_delete_upper = declared_on_delete.upper()
    declared_on_update_upper = declared_on_update.upper()
    if (
        live_target_table != declared_target_table
        or live_target_column != declared_target_column
        or live_on_delete != declared_on_delete_upper
        or live_on_update != declared_on_update_upper
    ):
        raise AdoptionDivergenceError(
            f"adoption FK: {namespace}.{table_name}.{col_name} has live FK "
            f"target={live_target_table}({live_target_column}) "
            f"ON DELETE {live_on_delete} ON UPDATE {live_on_update}; "
            f"declared target={declared_target_table}({declared_target_column}) "
            f"ON DELETE {declared_on_delete_upper} ON UPDATE {declared_on_update_upper}. "
            "Drop the live constraint manually and retry."
        )


def _warn_undeclared_live_fks(
    namespace: str,
    table_name: str,
    live_constraints: list[dict[str, Any]],
    declared_columns: set[str],
) -> None:
    """Log a warning for each live FK whose column isn't in the declared set.

    v1 does NOT drop undeclared constraints — symmetric refusal of
    unauthorized drift (W5.D §2.1 Codex re-review §15). Operator-visible
    so undeclared FKs aren't silently tolerated forever.
    """
    for live in live_constraints:
        col = live["column_name"]
        if col not in declared_columns:
            logger.warning(
                "adoption: %s.%s — live FK %r on column %r is NOT declared; "
                "leaving in place (v1 does not drop). Reconcile by declaring "
                "or dropping manually.",
                namespace, table_name, live["constraint_name"], col,
            )
