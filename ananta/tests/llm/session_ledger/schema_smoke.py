#!/usr/bin/env python3
"""Smoke test for the session_ledger schema (no pytest).

Coverage:

* All 10 tables register under namespace ``session_ledger``.
* Every protected standard field (id, namespace, created_at, updated_at,
  created_by, updated_by, is_deleted) is auto-injected by SchemaStandardizer
  and survives the standardization pass on every business table.
* No business table declares a protected field as a column (would raise
  in SchemaStandardizer._validate_no_protected_field_overrides).
* Partial unique indexes on source_cursor (discovery vs event_read) and
  on deployment.oauth_client_id are present.
* Schema-debt-external-id lane, 2a (2026-08-06): the standardizer accepts an
  explicit ``external_id: unique=False`` override (leg 1), and ``event``'s
  composite ``idx_event_session_external_unique`` survives standardization
  unchanged alongside that opt-out (leg 3) — see
  workbench/2026-08-06_schema_debt_external_id_findings_schema-debt-impl.md.

Run:
    .venv/bin/python3 ananta/tests/llm/session_ledger/schema_smoke.py
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(REPO_ROOT / "ananta" / "src"))

from ananta.llm.session_ledger.schema import (  # noqa: E402
    NAMESPACE,
    TABLE_ACTIVE_LEASE,
    TABLE_ATTACHMENT,
    TABLE_DEPLOYMENT,
    TABLE_EVENT,
    TABLE_IMPORT_BATCH,
    TABLE_SESSION,
    TABLE_SESSION_SOURCE_KIND,
    TABLE_SOURCE,
    TABLE_SOURCE_CURSOR,
    TABLE_SUMMARY,
    TABLE_TOOL_CALL,
    get_session_ledger_schema,
)
from ananta.types.column_types import ColumnType  # noqa: E402
from ananta.types.schema_standardizer import SchemaStandardizer  # noqa: E402
from ananta.types.schema_types import (  # noqa: E402
    ColumnDefinition,
    SchemaDefinition,
    TableSchema,
)

_passed = 0
_failed: list[str] = []


def _check(condition: object, label: str) -> None:
    global _passed
    if condition:
        _passed += 1
        print(f"  PASS  {label}")
    else:
        _failed.append(label)
        print(f"  FAIL  {label}")


_EXPECTED_TABLES = {
    TABLE_SOURCE,
    TABLE_SOURCE_CURSOR,
    TABLE_IMPORT_BATCH,
    TABLE_SESSION,
    TABLE_EVENT,
    TABLE_TOOL_CALL,
    TABLE_ATTACHMENT,
    TABLE_ACTIVE_LEASE,
    TABLE_SUMMARY,
    TABLE_DEPLOYMENT,
    TABLE_SESSION_SOURCE_KIND,
}

_PROTECTED = SchemaStandardizer.PROTECTED_STANDARD_FIELDS


def test_namespace_and_table_set() -> None:
    schema = get_session_ledger_schema()
    _check(schema.namespace == NAMESPACE, "namespace is session_ledger")
    _check(set(schema.tables.keys()) == _EXPECTED_TABLES, "all 11 tables registered")
    _check(schema.version == "1.0.0", "version is 1.0.0")


def test_no_protected_field_overrides() -> None:
    schema = get_session_ledger_schema()
    for table_name, table in schema.tables.items():
        offenders = set(table.columns.keys()) & _PROTECTED
        _check(
            not offenders,
            f"table {table_name!r} declares no protected fields ({sorted(offenders)} if any)",
        )


def test_standardizer_round_trip() -> None:
    schema = get_session_ledger_schema()
    standardizer = SchemaStandardizer()
    standardized = standardizer.standardize_schema(schema)
    _check(standardized.namespace == NAMESPACE, "standardized namespace preserved")
    for table_name in _EXPECTED_TABLES:
        table = standardized.tables[table_name]
        missing = _PROTECTED - set(table.columns.keys())
        _check(
            not missing,
            f"standardized {table_name!r} has every protected standard field "
            f"(missing: {sorted(missing)} if any)",
        )


def test_partial_unique_indexes_present() -> None:
    schema = get_session_ledger_schema()
    cursor_table = schema.tables[TABLE_SOURCE_CURSOR]
    cursor_index_names = {idx.name: idx for idx in cursor_table.indexes}
    _check(
        "idx_cursor_event_read_unique" in cursor_index_names
        and cursor_index_names["idx_cursor_event_read_unique"].unique
        and cursor_index_names["idx_cursor_event_read_unique"].where
        == "cursor_scope = 'event_read'",
        "source_cursor has partial unique index on event_read scope",
    )
    _check(
        "idx_cursor_discovery_unique" in cursor_index_names
        and cursor_index_names["idx_cursor_discovery_unique"].unique
        and cursor_index_names["idx_cursor_discovery_unique"].where
        == "cursor_scope = 'discovery'",
        "source_cursor has partial unique index on discovery scope",
    )
    deployment_table = schema.tables[TABLE_DEPLOYMENT]
    deployment_index_names = {idx.name: idx for idx in deployment_table.indexes}
    _check(
        "idx_deployment_oauth_client_unique" in deployment_index_names
        and deployment_index_names["idx_deployment_oauth_client_unique"].unique
        and deployment_index_names["idx_deployment_oauth_client_unique"].where
        == "oauth_client_id IS NOT NULL",
        "deployment has partial unique index on non-null oauth_client_id",
    )


def test_standardizer_accepts_external_id_unique_false_override() -> None:
    """Schema-debt-external-id lane, 2a leg (1): the standardizer's relax.

    Reverting ``SchemaStandardizer._validate_constrainable_field``'s
    external_id relax (i.e. restoring the old hard-raise on
    ``unique=False``) reds this leg.
    """
    table = TableSchema(
        table_name="probe",
        columns={
            "external_id": ColumnDefinition(type=ColumnType.TEXT, unique=False),
        },
    )
    schema = SchemaDefinition(namespace="probe_ns", tables={"probe": table})
    standardizer = SchemaStandardizer()
    standardized = standardizer.standardize_schema(schema)  # must not raise
    _check(
        standardized.tables["probe"].columns["external_id"].unique is False,
        "standardizer honors an explicit external_id unique=False override "
        "(does not silently force it back to True)",
    )


def test_event_composite_external_id_index_survives_standardization() -> None:
    """2a leg (3): the composite domain key remains declared and installed.

    Guards against 2a's fix accidentally dropping (or the standardizer
    silently overriding) ``idx_event_session_external_unique`` while opting
    the ``event`` table out of the standalone constraint — nobody should be
    able to drop the real domain key by accident and stay green.
    """
    schema = get_session_ledger_schema()
    event_table = schema.tables[TABLE_EVENT]
    _check(
        event_table.columns["external_id"].unique is False,
        "event.external_id declares unique=False (the 2a opt-out)",
    )
    composite = next(
        (idx for idx in event_table.indexes if idx.name == "idx_event_session_external_unique"),
        None,
    )
    _check(composite is not None, "idx_event_session_external_unique is still declared")
    if composite is not None:
        _check(
            list(composite.columns) == ["session_id", "external_id"] and composite.unique,
            f"idx_event_session_external_unique is UNIQUE(session_id, external_id) "
            f"(got columns={composite.columns!r}, unique={composite.unique!r})",
        )

    standardizer = SchemaStandardizer()
    standardized = standardizer.standardize_schema(schema)
    standardized_event = standardized.tables[TABLE_EVENT]
    _check(
        standardized_event.columns["external_id"].unique is False,
        "unique=False survives standardization (not reset to the platform default)",
    )
    standardized_composite = next(
        (
            idx
            for idx in standardized_event.indexes
            if idx.name == "idx_event_session_external_unique"
        ),
        None,
    )
    _check(
        standardized_composite is not None and standardized_composite.unique,
        "the composite index survives standardization unchanged",
    )


def main() -> int:
    print("=== session_ledger schema_smoke ===")
    test_namespace_and_table_set()
    test_no_protected_field_overrides()
    test_standardizer_round_trip()
    test_partial_unique_indexes_present()
    test_standardizer_accepts_external_id_unique_false_override()
    test_event_composite_external_id_index_survives_standardization()
    print(f"\n{_passed} passed, {len(_failed)} failed")
    if _failed:
        for label in _failed:
            print(f"  FAILED: {label}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
