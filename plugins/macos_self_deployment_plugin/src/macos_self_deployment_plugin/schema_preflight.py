"""§3 preflight DDL-free gate for the true-local blue-green deploy path.

Design ``workbench/2026-06-27_true_local_blue_green_materialized_artifacts_design.md``
§3: the durable code-rollback guarantee only holds when the schema is
*unchanged or additive* — a rolled-back binary ignores an extra column,
but it cannot undo a ``DROP COLUMN`` / type change / a new ``NOT NULL``
column it never learned to populate. So before a deploy under this
mechanism the implementation must **diff the declared SchemaDefinitions
old-vs-new and refuse/flag a non-additive diff** (§8.7), rather than
trust the SQL-lockdown's intent that the change set is code-only.

This module owns the *pure classifier* half of that gate — the part
§8.7 exercises and the part that needs neither a DB nor a running
platform. It operates on a **canonical schema snapshot**: a plain,
JSON-able ``namespace → table → column → attrs`` mapping. Two producers
feed it:

- :func:`schemas_to_snapshot` derives a snapshot from typed
  :class:`~ananta.types.schema_types.SchemaDefinition` objects (used by
  the unit smoke, and available to the release builder if it snapshots
  schemas into ``VERSION`` at build time — the design's preferred
  extraction path).
- the deploy-time extractor (release ``VERSION`` ``schema_snapshot``
  field) reads the same canonical shape straight off disk, so the live
  gate is a pure ``VERSION``-vs-``VERSION`` compare with no boot.

Decoupling the classifier from both the platform ``to_json`` format and
the extraction mechanism keeps it testable today and stable when the
extraction wiring lands.

Additive (rollback-safe → does NOT block):
  added namespace, added table, added column that is nullable or carries
  a default, relaxed ``NOT NULL``, index/description changes.

Non-additive (rollback-unsafe → blocks/flags), fail-closed — anything
not recognised as additive is treated as breaking:
  dropped namespace/table/column, column type (or type-params) change,
  tightened nullability (nullable → ``NOT NULL``), a new ``NOT NULL``
  column without a default, a primary-key change, a newly-added unique
  constraint.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Final

from ananta.types.schema_types import SchemaDefinition

# Canonical snapshot column attribute keys (no magic strings downstream).
ATTR_TYPE: Final[str] = "type"
ATTR_TYPE_PARAMS: Final[str] = "type_params"
ATTR_NOT_NULL: Final[str] = "not_null"
ATTR_HAS_DEFAULT: Final[str] = "has_default"
ATTR_PRIMARY_KEY: Final[str] = "primary_key"
ATTR_UNIQUE: Final[str] = "unique"

# Change-kind tokens for the breaking-change records.
KIND_NAMESPACE_REMOVED: Final[str] = "namespace_removed"
KIND_TABLE_REMOVED: Final[str] = "table_removed"
KIND_COLUMN_REMOVED: Final[str] = "column_removed"
KIND_COLUMN_TYPE_CHANGED: Final[str] = "column_type_changed"
KIND_NULLABILITY_TIGHTENED: Final[str] = "column_nullability_tightened"
KIND_NOT_NULL_NO_DEFAULT_ADDED: Final[str] = "not_null_column_without_default_added"
KIND_PRIMARY_KEY_CHANGED: Final[str] = "column_primary_key_changed"
KIND_UNIQUE_ADDED: Final[str] = "column_unique_constraint_added"
# B1 fail-closed: the candidate carries NO snapshot in steady state (a current
# snapshot exists) — the producer failed, so the change cannot be certified
# rollback-safe and the deploy is refused (NOT treated as additive).
KIND_CANDIDATE_SNAPSHOT_MISSING: Final[str] = "candidate_schema_snapshot_missing"
# B1 fail-closed (defensive, "can't happen"): the candidate HAS a snapshot but
# the CURRENT side is None while a current release EXISTS. The orchestrator
# resolves the old side by deriving it from ``current/code`` and must either
# return a non-None snapshot OR raise (fail-closed) — so a None reaching the
# gate alongside an existing current release means the derive returned None
# (a regression). Refuse rather than fall through to the bootstrap-additive
# cell: this keeps the fail-closed DECISION in the gate even though the derive
# I/O lives in the orchestrator (the exact B1·1 hole, made un-regressable).
KIND_CURRENT_SNAPSHOT_UNRESOLVED: Final[str] = "current_schema_snapshot_unresolved"

# Canonical snapshot type aliases (plain JSON-able structures).
ColumnSnapshot = Mapping[str, object]
TableSnapshot = Mapping[str, "ColumnSnapshot"]
NamespaceSnapshot = Mapping[str, "TableSnapshot"]
SchemaSnapshot = Mapping[str, "NamespaceSnapshot"]


@dataclass(frozen=True, slots=True)
class SchemaChange:
    """One non-additive (rollback-unsafe) difference between two snapshots."""

    kind: str
    namespace: str
    table: str | None
    column: str | None
    detail: str

    def describe(self) -> str:
        loc = self.namespace
        if self.table is not None:
            loc = f"{loc}.{self.table}"
        if self.column is not None:
            loc = f"{loc}.{self.column}"
        return f"[{self.kind}] {loc}: {self.detail}"


@dataclass(frozen=True, slots=True)
class PreflightVerdict:
    """Outcome of the §3 schema-diff classification.

    ``is_additive`` is the gate decision: ``True`` means the diff is
    empty or purely additive (deploy may proceed); ``False`` means at
    least one non-additive change was found (the deploy must be refused
    or explicitly overridden by the operator). ``breaking_changes`` lists
    every non-additive difference for the operator-facing report.
    """

    is_additive: bool
    breaking_changes: tuple[SchemaChange, ...]

    def summary(self) -> str:
        if self.is_additive:
            return "schema diff is additive (rollback-safe)"
        lines = "\n".join(f"  - {c.describe()}" for c in self.breaking_changes)
        return (
            f"schema diff has {len(self.breaking_changes)} non-additive "
            f"change(s) (rollback-UNSAFE):\n{lines}"
        )


def schemas_to_snapshot(
    schemas: Mapping[str, SchemaDefinition],
) -> dict[str, dict[str, dict[str, dict[str, object]]]]:
    """Reduce typed ``SchemaDefinition``s to the canonical comparable snapshot.

    Keyed ``namespace → table → column → attrs``. Only the attributes
    that bear on rollback-safety are retained, so the snapshot is small,
    JSON-able, and stable across cosmetic edits (descriptions, index
    tuning, sensitivity) that do not affect data compatibility.
    """
    snapshot: dict[str, dict[str, dict[str, dict[str, object]]]] = {}
    for namespace, schema in schemas.items():
        tables: dict[str, dict[str, dict[str, object]]] = {}
        for table_key, table in schema.tables.items():
            columns: dict[str, dict[str, object]] = {}
            for column_name, column in table.columns.items():
                columns[column_name] = {
                    ATTR_TYPE: str(column.type),
                    ATTR_TYPE_PARAMS: dict(column.type_params or {}),
                    ATTR_NOT_NULL: bool(column.not_null),
                    ATTR_HAS_DEFAULT: column.default is not None,
                    ATTR_PRIMARY_KEY: bool(column.primary_key),
                    ATTR_UNIQUE: bool(column.unique),
                }
            tables[table_key] = columns
        snapshot[namespace] = tables
    return snapshot


def classify_snapshot_diff(
    old: SchemaSnapshot,
    new: SchemaSnapshot,
) -> PreflightVerdict:
    """Classify ``old → new`` as additive (rollback-safe) or not.

    Pure: no DB, no platform boot. Fail-closed — every difference that is
    not a recognised additive shape is recorded as a breaking change.
    """
    breaking: list[SchemaChange] = []
    for namespace in old:
        if namespace not in new:
            breaking.append(SchemaChange(
                kind=KIND_NAMESPACE_REMOVED, namespace=namespace,
                table=None, column=None,
                detail="namespace (and its tables) no longer declared",
            ))
            continue
        breaking.extend(_diff_namespace(namespace, old[namespace], new[namespace]))
    # Added namespaces are additive — not inspected.
    return PreflightVerdict(is_additive=not breaking, breaking_changes=tuple(breaking))


def _diff_namespace(
    namespace: str, old: NamespaceSnapshot, new: NamespaceSnapshot,
) -> list[SchemaChange]:
    changes: list[SchemaChange] = []
    for table in old:
        if table not in new:
            changes.append(SchemaChange(
                kind=KIND_TABLE_REMOVED, namespace=namespace, table=table,
                column=None, detail="table no longer declared",
            ))
            continue
        changes.extend(_diff_table(namespace, table, old[table], new[table]))
    # Added tables are additive — not inspected.
    return changes


def _diff_table(
    namespace: str, table: str, old: TableSnapshot, new: TableSnapshot,
) -> list[SchemaChange]:
    changes: list[SchemaChange] = []
    for column in old:
        if column not in new:
            changes.append(SchemaChange(
                kind=KIND_COLUMN_REMOVED, namespace=namespace, table=table,
                column=column, detail="column dropped (destructive, irreversible)",
            ))
            continue
        changes.extend(
            _diff_existing_column(namespace, table, column, old[column], new[column])
        )
    for column in new:
        if column not in old:
            change = _classify_added_column(namespace, table, column, new[column])
            if change is not None:
                changes.append(change)
    return changes


def _diff_existing_column(
    namespace: str, table: str, column: str,
    old: ColumnSnapshot, new: ColumnSnapshot,
) -> list[SchemaChange]:
    """Compare a column present in both snapshots."""
    changes: list[SchemaChange] = []
    if old.get(ATTR_TYPE) != new.get(ATTR_TYPE) or old.get(ATTR_TYPE_PARAMS) != new.get(
        ATTR_TYPE_PARAMS
    ):
        changes.append(SchemaChange(
            kind=KIND_COLUMN_TYPE_CHANGED, namespace=namespace, table=table,
            column=column,
            detail=f"type {old.get(ATTR_TYPE)!r} → {new.get(ATTR_TYPE)!r}",
        ))
    if not old.get(ATTR_NOT_NULL) and new.get(ATTR_NOT_NULL):
        changes.append(SchemaChange(
            kind=KIND_NULLABILITY_TIGHTENED, namespace=namespace, table=table,
            column=column, detail="nullable → NOT NULL (existing NULL rows break)",
        ))
    if old.get(ATTR_PRIMARY_KEY) != new.get(ATTR_PRIMARY_KEY):
        changes.append(SchemaChange(
            kind=KIND_PRIMARY_KEY_CHANGED, namespace=namespace, table=table,
            column=column, detail="primary-key flag changed",
        ))
    if not old.get(ATTR_UNIQUE) and new.get(ATTR_UNIQUE):
        changes.append(SchemaChange(
            kind=KIND_UNIQUE_ADDED, namespace=namespace, table=table,
            column=column, detail="unique constraint added (may fail on duplicate rows)",
        ))
    return changes


def _classify_added_column(
    namespace: str, table: str, column: str, new: ColumnSnapshot,
) -> SchemaChange | None:
    """A new column is additive unless it is ``NOT NULL`` without a default."""
    if new.get(ATTR_NOT_NULL) and not new.get(ATTR_HAS_DEFAULT):
        return SchemaChange(
            kind=KIND_NOT_NULL_NO_DEFAULT_ADDED, namespace=namespace, table=table,
            column=column,
            detail="new NOT NULL column without default (existing rows cannot satisfy it)",
        )
    return None


__all__ = [
    "PreflightVerdict",
    "SchemaChange",
    "SchemaSnapshot",
    "classify_snapshot_diff",
    "schemas_to_snapshot",
]
