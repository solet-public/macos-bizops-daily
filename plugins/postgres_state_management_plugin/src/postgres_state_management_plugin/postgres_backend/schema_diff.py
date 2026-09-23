"""Schema diff for the plugin-schema lifecycle update / purge paths.

Compares the standardized ``SchemaDefinition`` recorded in ownership against
the standardized ``SchemaDefinition`` declared by the plugin, and returns an
ordered list of DDL ops that reconcile current → declared.

Order (per v8 plan step 8):
  create new tables → add columns → drop removed/mutated indexes →
  create added/mutated indexes → drop removed columns → (purge only) drop tables.

Drops-before-creates for indexes is intentional: v1 treats any
``IndexDefinition`` mutation as drop+readd, and the resolved physical index
name is a function of ``<namespace>__<table>__<idx_name>`` — for in-place
mutations the new physical name equals the old, so ``CREATE`` would collide
with ``DROP``-not-yet-run otherwise.

Refusals (v1):
  * Removing a table from declared schema in ``mode="update"`` — operator
    must call ``uninstall`` (data-preserving) or ``purge`` (destructive).
  * Type changes on an existing column — ``NotImplementedError``. Adoption
    has its own four-step subroutine for the whitelisted legacy
    normalizations; active updates have no whitelist.
  * Check-constraint mutations on existing columns / table — out of scope.
  * ``with_history`` toggles — out of scope.
  * ``id_prefix`` changes on an existing table — out of scope.
"""

from __future__ import annotations

import logging
import re
from typing import Any, Literal

from ananta.types.schema_types import (
    ColumnDefinition,
    IndexDefinition,
    SchemaDefinition,
    TableSchema,
)
from psycopg import sql

from .ddl_renderer import (
    build_default_check_constraint_name,
    build_default_unique_constraint_name,
    emit_add_check_constraint_op,
    emit_add_column_op,
    emit_add_unique_constraint_op,
    emit_create_index_op,
    emit_create_table_ops,
    emit_drop_column_op,
    emit_drop_constraint_op,
    emit_drop_index_op,
    emit_drop_table_op,
    resolve_index_name,
)

logger = logging.getLogger(__name__)

DiffMode = Literal["update", "purge"]


def diff_schema(
    namespace: str,
    current: SchemaDefinition,
    declared: SchemaDefinition,
    mode: DiffMode,
    schema_name: str,
    *,
    current_index_physical_names: dict[str, dict[str, str]] | None = None,
) -> list[sql.Composed]:
    """Compute the ordered DDL ops that reconcile ``current`` → ``declared``.

    Args:
        namespace: Plugin namespace (logical, not the Postgres schema).
        current: The recorded snapshot from the ownership table (already
            standardized).
        declared: The standardized declaration the plugin just submitted.
        mode: ``"update"`` (refuse table removal) or ``"purge"`` (drop everything).
        schema_name: Postgres schema (database namespace) for identifier qualification.
        current_index_physical_names: Optional per-table mapping of logical
            ``IndexDefinition.name`` → recorded physical index name. The lifecycle
            service supplies this from the ownership snapshot so DROP INDEX targets
            the actual on-disk name. If omitted, falls back to the standard
            ``resolve_index_name(namespace, table, idx_name)`` formula — which is
            correct for indexes the lifecycle itself created, but **wrong for
            legacy bare-name indexes** that adoption may not yet have renamed.

    Returns:
        Ordered list of ``psycopg.sql.Composed`` ops.

    Raises:
        NotImplementedError: For unsupported transitions (table removal in
            ``update`` mode; column type changes; check-constraint mutations;
            ``with_history`` / ``id_prefix`` toggles).
    """
    current_index_physical_names = current_index_physical_names or {}

    create_table_ops: list[sql.Composed] = []
    add_column_ops: list[sql.Composed] = []
    drop_index_ops: list[sql.Composed] = []
    create_index_ops: list[sql.Composed] = []
    drop_column_ops: list[sql.Composed] = []
    drop_table_ops: list[sql.Composed] = []

    declared_table_names = set(declared.tables)
    current_table_names = set(current.tables)

    # New tables
    for table_name in declared_table_names - current_table_names:
        create_table_ops.extend(
            emit_create_table_ops(namespace, declared.tables[table_name], schema_name)
        )

    # Removed tables: refuse in update, drop in purge
    removed_table_names = current_table_names - declared_table_names
    if removed_table_names and mode == "update":
        raise NotImplementedError(
            f"diff_schema: declared schema for {namespace} omits tables "
            f"{sorted(removed_table_names)} that are recorded in ownership. "
            "Active update cannot drop tables. Call uninstall_plugin_schema "
            "(data-preserving) or purge_plugin_schema (destructive) to remove a table."
        )
    if mode == "purge":
        for table_name in removed_table_names:
            drop_table_ops.append(emit_drop_table_op(namespace, table_name, schema_name))

    # Per-table column + index diff (tables that exist in both current and declared)
    for table_name in declared_table_names & current_table_names:
        current_table = current.tables[table_name]
        declared_table = declared.tables[table_name]

        _refuse_table_level_changes(namespace, table_name, current_table, declared_table)

        add_column_ops.extend(
            _diff_columns_add(namespace, table_name, current_table, declared_table, schema_name)
        )
        drop_column_ops.extend(
            _diff_columns_drop(namespace, table_name, current_table, declared_table, schema_name)
        )
        # Per M21-RCA Fix 1 (2026-06-11 PT): additive-CHECK-enum-expansion
        # is the one supported in-place column mutation. The dispatcher
        # also raises NotImplementedError on every other shape change
        # with the extended error message (Fix 2) that names `check`,
        # `primary_key`, and `type_params` so a future debugger sees the
        # actual delta. The returned ops list contains DROP CONSTRAINT
        # + ADD CONSTRAINT pairs for each additive expansion; we slot
        # them into the existing add_column_ops bucket so they emit
        # AFTER any newly-added columns and BEFORE the drop-index ops
        # (constraint mutations are column-shape ops; they belong
        # alongside add-column in the ordering).
        add_column_ops.extend(
            _diff_or_refuse_column_changes(
                namespace, table_name, current_table, declared_table, schema_name,
            )
        )

        idx_drops, idx_creates = _diff_indexes(
            namespace,
            table_name,
            current_table,
            declared_table,
            schema_name,
            current_index_physical_names.get(table_name, {}),
        )
        drop_index_ops.extend(idx_drops)
        create_index_ops.extend(idx_creates)

    # Purge-mode: ALSO drop removed tables' indexes implicitly (CASCADE on DROP TABLE)
    # so we don't need to drop them separately.

    return [
        *create_table_ops,
        *add_column_ops,
        *drop_index_ops,
        *create_index_ops,
        *drop_column_ops,
        *drop_table_ops,
    ]


# --- per-aspect diffs --------------------------------------------------------


def _diff_columns_add(
    namespace: str,
    table_name: str,
    current_table: TableSchema,
    declared_table: TableSchema,
    schema_name: str,
) -> list[sql.Composed]:
    """Columns in ``declared`` but not in ``current`` → ADD COLUMN."""
    new_cols = set(declared_table.columns) - set(current_table.columns)
    return [
        emit_add_column_op(
            namespace, table_name, col_name, declared_table.columns[col_name], schema_name
        )
        for col_name in sorted(new_cols)
    ]


def _diff_columns_drop(
    namespace: str,
    table_name: str,
    current_table: TableSchema,
    declared_table: TableSchema,
    schema_name: str,
) -> list[sql.Composed]:
    """Columns in ``current`` but not in ``declared`` → DROP COLUMN."""
    removed_cols = set(current_table.columns) - set(declared_table.columns)
    return [
        emit_drop_column_op(namespace, table_name, col_name, schema_name)
        for col_name in sorted(removed_cols)
    ]


def _diff_or_refuse_column_changes(
    namespace: str,
    table_name: str,
    current_table: TableSchema,
    declared_table: TableSchema,
    schema_name: str,
) -> list[sql.Composed]:
    """Reconcile columns common to both schemas.

    Emits ops for the safe in-place mutations the diff path supports
    and raises ``NotImplementedError`` for unsupported shape changes:

    * **Additive CHECK enum expansion** (M21-RCA Fix 1, 2026-06-11 PT):
      a column whose ONLY difference is its ``check`` constraint, AND
      whose live value-set is a strict subset of the declared
      value-set, gets a ``DROP CONSTRAINT`` + ``ADD CONSTRAINT`` pair.
      No table scan is needed — existing rows trivially satisfy the
      new superset CHECK. This unblocks the canonical
      ``IngestSourceKind`` (and similar enum) expansions that Tier-B/C/D
      session-source plugins introduce.
    * Any other column shape change → ``NotImplementedError`` per the
      pre-Fix-1 discipline. The error message names every comparison
      field on ``ColumnDefinition`` so future debuggers see the actual
      delta (Fix 2): historically only four fields were printed and a
      ``check``-only delta showed as four identical-looking values.
    """
    ops: list[sql.Composed] = []
    common = set(current_table.columns) & set(declared_table.columns)
    for col_name in sorted(common):
        current_col = current_table.columns[col_name]
        declared_col = declared_table.columns[col_name]
        if _columns_equivalent(current_col, declared_col):
            continue
        if _is_additive_check_only_change(current_col, declared_col):
            constraint_name = build_default_check_constraint_name(
                namespace, table_name, col_name,
            )
            ops.append(emit_drop_constraint_op(
                namespace, table_name, constraint_name, schema_name,
            ))
            assert declared_col.check is not None  # narrowed by additive helper
            ops.append(emit_add_check_constraint_op(
                namespace, table_name, constraint_name,
                declared_col.check, schema_name,
            ))
            continue
        if _is_unique_only_change(current_col, declared_col):
            constraint_name = build_default_unique_constraint_name(
                namespace, table_name, col_name,
            )
            if current_col.unique and not declared_col.unique:
                ops.append(emit_drop_constraint_op(
                    namespace, table_name, constraint_name, schema_name,
                ))
            else:
                ops.append(emit_add_unique_constraint_op(
                    namespace, table_name, constraint_name, col_name, schema_name,
                ))
            continue
        raise NotImplementedError(
            f"diff_schema: column {namespace}.{table_name}.{col_name} changed shape "
            f"(type={current_col.type.name}->{declared_col.type.name}, "
            f"primary_key={current_col.primary_key}->{declared_col.primary_key}, "
            f"not_null={current_col.not_null}->{declared_col.not_null}, "
            f"default={current_col.default!r}->{declared_col.default!r}, "
            f"unique={current_col.unique}->{declared_col.unique}, "
            f"check={current_col.check!r}->{declared_col.check!r}, "
            f"type_params={current_col.type_params!r}->{declared_col.type_params!r}). "
            "Active-update column mutations are not implemented in v1 "
            "outside the additive-CHECK-enum-expansion path "
            "(M21-RCA Fix 1, 2026-06-11). "
            "Drop the column and re-add it with the new shape, or wait for "
            "the retype-with-data-preservation path."
        )
    return ops


_CHECK_IN_PATTERN = re.compile(
    r"^\s*"
    r"(?P<col>[A-Za-z_][A-Za-z0-9_]*)"
    r"\s+IN\s*\(\s*"
    r"(?P<values>.+?)"
    r"\s*\)\s*$",
    re.DOTALL | re.IGNORECASE,
)


def _parse_check_in_values(check: str) -> tuple[str, frozenset[str]] | None:
    """Parse a ``<col> IN ('v1', 'v2', ...)`` CHECK body.

    Returns ``(column_name, value_set)`` if the expression matches the
    canonical ``<col> IN (<csv of single-quoted strings>)`` shape used
    by ``schema.py:_enum_csv``. Returns ``None`` if the expression's
    shape differs (e.g. arithmetic / disjunction / unquoted values) —
    such CHECKs cannot be safely diffed as additive enum expansions,
    so the caller refuses the change per the pre-Fix-1 discipline.
    """
    match = _CHECK_IN_PATTERN.match(check.strip())
    if match is None:
        return None
    col = match.group("col")
    values_csv = match.group("values")
    parsed_values: set[str] = set()
    for raw in values_csv.split(","):
        token = raw.strip()
        if len(token) < 2 or token[0] != "'" or token[-1] != "'":
            return None
        parsed_values.add(token[1:-1])
    return col, frozenset(parsed_values)


def _check_is_additive_enum_expansion(
    current_check: str | None,
    declared_check: str | None,
) -> bool:
    """True iff ``declared_check`` is a strict superset of ``current_check``.

    Both expressions must parse against the ``<col> IN ('v1', ...)``
    shape AND name the same column. The new value set must STRICTLY
    contain the old (``current_values < declared_values``) — equal sets
    aren't an expansion (they signal an unrelated bug if the caller
    routed here) and shrunk sets are non-additive (a removed value
    could leave existing rows violating the new CHECK).
    """
    if current_check is None or declared_check is None:
        return False
    current_parsed = _parse_check_in_values(current_check)
    declared_parsed = _parse_check_in_values(declared_check)
    if current_parsed is None or declared_parsed is None:
        return False
    current_col, current_values = current_parsed
    declared_col, declared_values = declared_parsed
    if current_col != declared_col:
        return False
    return current_values < declared_values


def _is_additive_check_only_change(
    current_col: ColumnDefinition,
    declared_col: ColumnDefinition,
) -> bool:
    """True iff the two columns differ ONLY in their ``check`` constraint
    AND that change is an additive enum expansion.

    Every other field on ``ColumnDefinition`` MUST match; otherwise we
    refuse the change because the additive-CHECK path can't safely
    handle compound mutations.
    """
    return (
        current_col.type == declared_col.type
        and current_col.primary_key == declared_col.primary_key
        and current_col.not_null == declared_col.not_null
        and current_col.default == declared_col.default
        and current_col.unique == declared_col.unique
        and current_col.type_params == declared_col.type_params
        and current_col.check != declared_col.check
        and _check_is_additive_enum_expansion(
            current_col.check, declared_col.check,
        )
    )


def _is_unique_only_change(
    current_col: ColumnDefinition,
    declared_col: ColumnDefinition,
) -> bool:
    """True iff the two columns differ ONLY in their ``unique`` flag.

    Every other DDL-affecting field MUST match; a compound mutation (e.g.
    ``unique`` AND ``type``) still refuses, matching the additive-CHECK path's
    single-axis discipline. A pure ``unique`` flip is realized as a single
    ``DROP CONSTRAINT`` (True->False) or ``ADD CONSTRAINT ... UNIQUE``
    (False->True) on the inline-unique constraint name.
    """
    return (
        current_col.type == declared_col.type
        and current_col.primary_key == declared_col.primary_key
        and current_col.not_null == declared_col.not_null
        and current_col.default == declared_col.default
        and current_col.check == declared_col.check
        and current_col.type_params == declared_col.type_params
        and current_col.unique != declared_col.unique
    )


def _columns_equivalent(a: ColumnDefinition, b: ColumnDefinition) -> bool:
    """Two ColumnDefinitions are equivalent if all DDL-affecting fields match.

    Annotation-only fields (``description``, ``data_sensitivity``) don't drive
    DDL and are ignored here. Snapshot updates carry them; emission doesn't.
    """
    return (
        a.type == b.type
        and a.primary_key == b.primary_key
        and a.not_null == b.not_null
        and a.default == b.default
        and a.unique == b.unique
        and a.check == b.check
        and a.type_params == b.type_params
    )


def _refuse_table_level_changes(
    namespace: str,
    table_name: str,
    current_table: TableSchema,
    declared_table: TableSchema,
) -> None:
    """Raise on with_history / id_prefix / check_constraint mutations."""
    if current_table.with_history != declared_table.with_history:
        raise NotImplementedError(
            f"diff_schema: with_history toggle on {namespace}.{table_name} "
            f"({current_table.with_history} -> {declared_table.with_history}) "
            "is not implemented. History-table generation itself is also out of scope in v1."
        )
    if current_table.id_prefix != declared_table.id_prefix:
        raise NotImplementedError(
            f"diff_schema: id_prefix change on {namespace}.{table_name} "
            f"({current_table.id_prefix!r} -> {declared_table.id_prefix!r}) is not implemented."
        )
    if list(current_table.check_constraints) != list(declared_table.check_constraints):
        raise NotImplementedError(
            f"diff_schema: check_constraints mutation on {namespace}.{table_name} "
            "is not implemented."
        )


def _diff_indexes(
    namespace: str,
    table_name: str,
    current_table: TableSchema,
    declared_table: TableSchema,
    schema_name: str,
    current_physical_names: dict[str, str],
) -> tuple[list[sql.Composed], list[sql.Composed]]:
    """Diff indexes between two table snapshots.

    Returns ``(drops, creates)`` — caller orders them as drops-before-creates.

    Mutation strategy: any change to an ``IndexDefinition`` (columns, where,
    unique) is treated as drop+readd on the same logical name. The resolved
    physical name doesn't change for in-place mutations, which is exactly why
    drops must precede creates.
    """
    drops: list[sql.Composed] = []
    creates: list[sql.Composed] = []

    current_by_name = {idx.name: idx for idx in current_table.indexes}
    declared_by_name = {idx.name: idx for idx in declared_table.indexes}

    # Drop indexes that are gone or have changed shape
    for logical_name, current_idx in current_by_name.items():
        declared_idx = declared_by_name.get(logical_name)
        if declared_idx is None or not _indexes_equivalent(current_idx, declared_idx):
            physical_name = current_physical_names.get(logical_name) or resolve_index_name(
                namespace, table_name, logical_name
            )
            drops.append(emit_drop_index_op(physical_name, schema_name))

    # Create indexes that are new or have changed shape
    for logical_name, declared_idx in declared_by_name.items():
        current_idx = current_by_name.get(logical_name)
        if current_idx is None or not _indexes_equivalent(current_idx, declared_idx):
            creates.append(emit_create_index_op(namespace, table_name, declared_idx, schema_name))

    return drops, creates


def _indexes_equivalent(a: IndexDefinition, b: IndexDefinition) -> bool:
    """Two IndexDefinitions are equivalent for diff purposes if shape matches.

    W5.E §5.2 G2: equivalence now covers ``using`` (index method),
    ``column_operator_classes`` (per-column opclass map), and
    ``index_with_options`` (build-time reloptions like HNSW m/
    ef_construction). Without these, a declared HNSW (vector_cosine_ops
    + m=16/ef_construction=64) is falsely matched against a live
    default btree on the same column, schema_diff emits no recreate,
    and HNSW never lands. Per the W5.E design memo §5.2:
    ``index_with_options`` comparison normalizes both sides to
    ``{key: str(value)}`` form so int/float/bool/string entries match
    semantically regardless of source-type (declared dict has typed
    values; live introspection surfaces strings).
    """
    return (
        a.name == b.name
        and list(a.columns) == list(b.columns)
        and a.unique == b.unique
        and a.where == b.where
        and a.using == b.using
        and (a.column_operator_classes or {}) == (b.column_operator_classes or {})
        and _normalize_with_options(a.index_with_options)
        == _normalize_with_options(b.index_with_options)
    )


def _normalize_with_options(opts: dict[str, object] | None) -> dict[str, str]:
    """Coerce index reloptions to ``{key: str(value)}`` for type-safe diff.

    Live introspection (``pg_class.reloptions``) surfaces reloptions as
    ``TEXT[]`` strings (``["m=16", "ef_construction=64"]``); declared
    IndexDefinitions carry native Python types (``{"m": 16,
    "ef_construction": 64}``). Normalizing both sides to string-form
    makes equality robust across the typed-vs-introspected boundary
    per the W5.E §5.2 options-comparison invariant.
    """
    if not opts:
        return {}
    return {k: str(v) for k, v in opts.items()}


def extract_index_physical_names_from_snapshot(
    snapshot: dict[str, Any],
) -> dict[str, str]:
    """Pull the ``physical_name`` field from an ownership-snapshot's index list.

    The lifecycle records resolved physical names alongside each declared
    index in the snapshot (one of the v8 invariants). When the snapshot
    predates that field — or for adoption snapshots — physical names may be
    absent; the diff falls back to the resolved-name formula in that case.

    This helper centralizes the lookup so the lifecycle service doesn't have
    to know the snapshot's internal shape.
    """
    result: dict[str, str] = {}
    for idx_entry in snapshot.get("indexes", []):
        if not isinstance(idx_entry, dict):
            continue
        name = idx_entry.get("name")
        physical = idx_entry.get("physical_name")
        if name and physical:
            result[name] = physical
    return result
