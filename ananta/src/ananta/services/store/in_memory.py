"""Process-local in-memory backend for :class:`Store`.

Mirrors observable Postgres-backend behavior so consumers don't branch
by backend.  Concretely:

* schema runs through :class:`SchemaStandardizer` at construction, so
  all nine platform-standard fields are present
* a single :class:`threading.RLock` guards every mutation
* ``insert`` auto-generates ``id`` from ``schema.id_prefix``, fills
  ``namespace`` + ``created_at`` + ``updated_at`` + ``is_deleted=0``,
  and rejects rows that miss ``not_null=True`` columns
* ``unique=True`` constraints reject duplicates across ALL rows —
  including soft-deleted ones, matching Postgres ``UNIQUE`` index
  semantics
* ``read`` excludes ``is_deleted=1`` rows by default
* ``touch`` is a real primitive: writes ``updated_at = now()`` directly
* :class:`~datetime.datetime` / :class:`~datetime.date` values are
  serialized to ISO-8601 strings on insert/update, matching what
  ``psycopg`` returns to the Postgres backend
"""

from __future__ import annotations

import threading
from datetime import UTC, date, datetime
from typing import Any
from uuid import uuid4

from ananta.types.schema_standardizer import SchemaStandardizer
from ananta.types.schema_types import (
    ColumnDefinition,
    SchemaDefinition,
    TableSchema,
)

from .errors import (
    EmptyUpdateError,
    NotNullViolationError,
    UniqueViolationError,
)
from .protocol import Row

# Standard fields the platform auto-fills.  Callers don't supply these
# on ``insert`` — except ``external_id`` and ``name``, which are
# caller-optional, so we don't auto-fill them.
_AUTO_INSERT_FIELDS = frozenset(
    {"id", "namespace", "created_at", "updated_at", "is_deleted"},
)


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _normalize_value(value: Any) -> Any:
    """Coerce values to JSON-compatible shape, matching psycopg returns.

    ``datetime`` / ``date`` -> ISO-8601 string; everything else passes
    through unchanged.  Mirrors ``postgres_state_management_plugin``'s
    ``_serialize_for_json`` so callers see the same shape from both
    backends.
    """
    if isinstance(value, datetime | date):
        return value.isoformat()
    return value


class InMemoryStore:
    """Process-local :class:`Store` implementation.

    One instance per consumer per (namespace, schema).  Two callers
    that ask for separate :class:`InMemoryStore` instances against the
    same schema get independent dicts — consumers either hold a single
    instance themselves or share it explicitly.  No global registry
    keeps stores alive across factory calls.
    """

    def __init__(self, schema: TableSchema, namespace: str) -> None:
        """Build an in-memory store over a ``TableSchema``.

        Runs the schema through :class:`SchemaStandardizer` so the same
        protected/constrainable rules apply as the Postgres path, and
        so the nine standard fields are known to the auto-fill logic.
        """
        if not namespace:
            raise ValueError("namespace must be non-empty")
        standardized = SchemaStandardizer().standardize_schema(
            SchemaDefinition(
                namespace=namespace,
                tables={schema.table_name: schema},
            ),
        )
        self._schema: TableSchema = standardized.tables[schema.table_name]
        self._namespace = namespace
        self._lock = threading.RLock()
        self._rows: dict[str, Row] = {}
        # Per-column value -> id index for unique-constraint enforcement.
        # Includes soft-deleted rows: Postgres UNIQUE indexes don't know
        # about ``is_deleted``, and callers must see the same rejection
        # shape from both backends.
        self._unique_indexes: dict[str, dict[Any, str]] = {
            col_name: {}
            for col_name, col_def in self._schema.columns.items()
            if col_def.unique
        }
        self._id_prefix: str = schema.id_prefix or "row"

    # ------------------------------------------------------------------
    # Mutations
    # ------------------------------------------------------------------

    def insert(self, data: Row) -> str:
        with self._lock:
            row = self._build_inserted_row(data)
            self._check_not_null(row)
            self._check_unique(row, exclude_id=None)
            self._rows[row["id"]] = row
            self._index_unique(row)
            return str(row["id"])

    def update(self, conditions: Row, updates: Row) -> int:
        if not updates:
            raise EmptyUpdateError(
                "update() requires a non-empty updates dict; "
                "use touch() to bump updated_at only",
            )
        normalized = {k: _normalize_value(v) for k, v in updates.items()}
        with self._lock:
            count = 0
            for row_id, row in self._iter_matching(conditions):
                # Build the post-update row to test unique against
                proposed = {**row, **normalized}
                proposed["updated_at"] = _now_iso()
                self._check_not_null(proposed)
                self._check_unique(proposed, exclude_id=row_id)
                self._deindex_unique(row)
                self._rows[row_id] = proposed
                self._index_unique(proposed)
                count += 1
            return count

    def upsert(self, data: Row, conflict_columns: list[str]) -> str:
        if not conflict_columns:
            raise ValueError("upsert() requires non-empty conflict_columns")
        with self._lock:
            match = self._find_by_conflict(data, conflict_columns)
            if match is None:
                return self.insert(data)
            row_id = match["id"]
            updates = {k: v for k, v in data.items() if k != "id"}
            self.update({"id": row_id}, updates)
            return str(row_id)

    def delete(self, conditions: Row, soft_delete: bool = True) -> int:
        with self._lock:
            if soft_delete:
                return self.update(
                    conditions,
                    {"is_deleted": 1},
                )
            count = 0
            for row_id, row in list(self._iter_matching(conditions)):
                self._deindex_unique(row)
                del self._rows[row_id]
                count += 1
            return count

    def touch(self, conditions: Row) -> int:
        now = _now_iso()
        with self._lock:
            count = 0
            for row_id, row in self._iter_matching(conditions):
                row["updated_at"] = now
                self._rows[row_id] = row
                count += 1
            return count

    # ------------------------------------------------------------------
    # Reads
    # ------------------------------------------------------------------

    def read(
        self,
        conditions: Row | None = None,
        *,
        include_deleted: bool = False,
    ) -> list[Row]:
        with self._lock:
            results: list[Row] = []
            effective = dict(conditions) if conditions else {}
            for _, row in self._iter_matching(effective):
                if not include_deleted and row.get("is_deleted"):
                    continue
                results.append(dict(row))
            return results

    def read_one(
        self,
        conditions: Row,
        *,
        include_deleted: bool = False,
    ) -> Row | None:
        rows = self.read(conditions, include_deleted=include_deleted)
        if not rows:
            return None
        if len(rows) > 1:
            raise LookupError(
                f"read_one matched {len(rows)} rows for conditions={conditions!r}",
            )
        return rows[0]

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _build_inserted_row(self, data: Row) -> Row:
        row: Row = {k: _normalize_value(v) for k, v in data.items()}
        now = _now_iso()
        if "id" not in row:
            row["id"] = f"{self._id_prefix}_{uuid4()}"
        row.setdefault("namespace", self._namespace)
        row.setdefault("created_at", now)
        row.setdefault("updated_at", now)
        row.setdefault("is_deleted", 0)
        # Fill caller-omitted columns with column-default or ``None`` so
        # the row shape matches the schema's column set.
        for col_name, col_def in self._schema.columns.items():
            if col_name in row:
                continue
            if col_name in _AUTO_INSERT_FIELDS:
                continue
            row[col_name] = _coerce_default(col_def)
        return row

    def _check_not_null(self, row: Row) -> None:
        for col_name, col_def in self._schema.columns.items():
            if not col_def.not_null:
                continue
            value = row.get(col_name)
            if value is None or value == "":
                raise NotNullViolationError(
                    col_name, table=self._schema.table_name,
                )

    def _check_unique(self, row: Row, *, exclude_id: str | None) -> None:
        for col_name, index in self._unique_indexes.items():
            value = row.get(col_name)
            if value is None:
                continue
            existing_id = index.get(value)
            if existing_id is None or existing_id == exclude_id:
                continue
            raise UniqueViolationError(
                col_name, value, table=self._schema.table_name,
            )

    def _index_unique(self, row: Row) -> None:
        for col_name, index in self._unique_indexes.items():
            value = row.get(col_name)
            if value is None:
                continue
            index[value] = str(row["id"])

    def _deindex_unique(self, row: Row) -> None:
        for col_name, index in self._unique_indexes.items():
            value = row.get(col_name)
            if value is None:
                continue
            if index.get(value) == row.get("id"):
                index.pop(value, None)

    def _iter_matching(self, conditions: Row) -> list[tuple[str, Row]]:
        if not conditions:
            return [(row_id, row) for row_id, row in self._rows.items()]
        normalized = {k: _normalize_value(v) for k, v in conditions.items()}
        return [
            (row_id, row)
            for row_id, row in self._rows.items()
            if _row_matches(row, normalized)
        ]

    def _find_by_conflict(
        self, data: Row, conflict_columns: list[str],
    ) -> Row | None:
        conditions = {
            col: _normalize_value(data[col])
            for col in conflict_columns
            if col in data
        }
        if len(conditions) != len(conflict_columns):
            return None
        for _, row in self._iter_matching(conditions):
            return row
        return None


def _row_matches(row: Row, conditions: Row) -> bool:
    for col, expected in conditions.items():
        if row.get(col) != expected:
            return False
    return True


def _coerce_default(col_def: ColumnDefinition) -> Any:
    """Resolve a ColumnDefinition's default to a Python value.

    The schema standardizer encodes some defaults as platform contract
    strings (``__CONTRACT:auto_id_with_prefix__``, etc.).  Those are
    auto-handled by ``_build_inserted_row`` ahead of this call, so any
    contract strings remaining here belong to columns the caller chose
    not to populate.  Treat them as ``None`` rather than literal text.
    """
    default = col_def.default
    if isinstance(default, str) and default.startswith("__CONTRACT:"):
        return None
    return default


__all__ = ["InMemoryStore"]
