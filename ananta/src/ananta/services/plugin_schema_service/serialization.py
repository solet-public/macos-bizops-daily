"""Canonical JSON shape for ``SchemaDefinition`` lifecycle calls.

Channel callers reach the lifecycle verbs through the action processor's
JSON-argument path, which cannot construct Python dataclasses. The verbs
accept a JSON dict shape mirroring the dataclass fields; the implementation
hydrates back into ``SchemaDefinition`` / ``TableSchema`` / ``ColumnDefinition`` /
``IndexDefinition`` at the boundary.

``ColumnType`` serializes via ``.name`` (the symbolic logical token), not
``.value``. After the ``BOOLEAN = "BOOLEAN"`` enum fix the two strings are
equal in content, but ``.name`` is the stable surface — future enum value
changes (e.g., a Postgres-specific value override) won't leak into the wire.
"""

from __future__ import annotations

from typing import Any

from ananta.types.column_types import ColumnType
from ananta.types.schema_types import (
    ColumnDefinition,
    IndexDefinition,
    SchemaDefinition,
    TableSchema,
)


def to_json(schema_def: SchemaDefinition) -> dict[str, Any]:
    """Serialize a ``SchemaDefinition`` to its canonical JSON shape."""
    return {
        "namespace": schema_def.namespace,
        "version": schema_def.version,
        "description": schema_def.description,
        "tables": {
            table_name: _table_to_json(table)
            for table_name, table in schema_def.tables.items()
        },
    }


def from_json(data: dict[str, Any]) -> SchemaDefinition:
    """Hydrate a canonical JSON shape into a ``SchemaDefinition``."""
    return SchemaDefinition(
        namespace=data["namespace"],
        tables={
            table_name: _table_from_json(t)
            for table_name, t in data.get("tables", {}).items()
        },
        version=data.get("version", "1.0.0"),
        description=data.get("description"),
    )


def _table_to_json(table: TableSchema) -> dict[str, Any]:
    return {
        "table_name": table.table_name,
        "columns": {
            col_name: _column_to_json(col) for col_name, col in table.columns.items()
        },
        "indexes": [_index_to_json(idx) for idx in table.indexes],
        "check_constraints": list(table.check_constraints),
        "with_history": table.with_history,
        "description": table.description,
        "id_prefix": table.id_prefix,
        "data_sensitivity": table.data_sensitivity,
    }


def _table_from_json(data: dict[str, Any]) -> TableSchema:
    return TableSchema(
        table_name=data["table_name"],
        columns={
            col_name: _column_from_json(c)
            for col_name, c in data.get("columns", {}).items()
        },
        indexes=[_index_from_json(i) for i in data.get("indexes", [])],
        check_constraints=list(data.get("check_constraints", [])),
        with_history=data.get("with_history", False),
        description=data.get("description"),
        id_prefix=data.get("id_prefix"),
        data_sensitivity=data.get("data_sensitivity", 1.0),
    )


def _column_to_json(col: ColumnDefinition) -> dict[str, Any]:
    return {
        "type": col.type.name,  # symbolic, not .value — see module docstring
        "primary_key": col.primary_key,
        "not_null": col.not_null,
        "default": col.default,
        "unique": col.unique,
        "check": col.check,
        "description": col.description,
        "type_params": col.type_params,
        "data_sensitivity": col.data_sensitivity,
    }


def _column_from_json(data: dict[str, Any]) -> ColumnDefinition:
    return ColumnDefinition(
        type=ColumnType[data["type"]],  # by name, not by value
        primary_key=data.get("primary_key", False),
        not_null=data.get("not_null", False),
        default=data.get("default"),
        unique=data.get("unique", False),
        check=data.get("check"),
        description=data.get("description"),
        type_params=data.get("type_params"),
        data_sensitivity=data.get("data_sensitivity", 1.0),
    )


def _index_to_json(idx: IndexDefinition) -> dict[str, Any]:
    return {
        "name": idx.name,
        "columns": list(idx.columns),
        "unique": idx.unique,
        "where": idx.where,
        "using": idx.using,
        "column_operator_classes": idx.column_operator_classes,
        # W5.E §5.2 G2: per-index reloptions (HNSW m / ef_construction;
        # BRIN pages_per_range; etc.) round-trip with the schema
        # snapshot so blue-green adoption + RDS mirroring preserve them.
        "index_with_options": idx.index_with_options,
    }


def _index_from_json(data: dict[str, Any]) -> IndexDefinition:
    return IndexDefinition(
        name=data["name"],
        columns=list(data["columns"]),
        unique=data.get("unique", False),
        where=data.get("where"),
        using=data.get("using"),
        column_operator_classes=data.get("column_operator_classes"),
        # Defensive .get() for legacy snapshots written before the
        # W5.E §5.2 field landed — they deserialize with
        # ``index_with_options=None`` (= no reloptions) so older
        # snapshots stay valid through the round-trip.
        index_with_options=data.get("index_with_options"),
    )
