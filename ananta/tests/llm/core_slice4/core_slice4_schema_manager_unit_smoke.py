#!/usr/bin/env python3
"""Unit smoke for core Slice-4 #3 (schema_manager fallback removal, SQL lockdown).

The operator-approved (A) change deletes SchemaManager's legacy direct-create
fallback and makes ``plugin_schema_service`` MANDATORY. This pins the structural
behavior (no live DB needed — the change is routing/contract, not DB-behavioral):

* FAIL-LOUD on a missing binding: ``plugin_schema_service`` is now in
  ``REQUIRED_SERVICES``, so ``validate_required_services`` RAISES
  ``ServiceBindingError`` when it is unbound — the FIRST-line surface (runs at
  startup before schema init; the inline ``StartupError`` in ``_initialize_schemas``
  is the belt-and-suspenders backstop for the now-impossible bound-but-None case).
* LIFECYCLE-ONLY schema init still works: ``initialize_schemas`` routes EVERY
  namespace through ``plugin_schema_service.install_plugin_schema`` and NEVER calls
  ``state_service.create_schema`` (the deleted legacy path).
* DEAD CODE removed: the 5 legacy SchemaManager methods + ``TableSchema.get_index_sqls``
  / ``to_create_sql`` + ``IndexDefinition.to_sql`` (and its private renderers) are gone.

No external dependencies — runs unconditionally.

Run::

    .venv/bin/python3 ananta/tests/llm/core_slice4/core_slice4_schema_manager_unit_smoke.py
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, cast

REPO_ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(REPO_ROOT / "ananta" / "src"))

from ananta.core.orchestration.service_bindings import (  # noqa: E402
    REQUIRED_SERVICES,
    ServiceBindingError,
    ServiceBindings,
    ServiceName,
)
from ananta.services.schema_manager import SchemaManager  # noqa: E402
from ananta.types.schema_types import (  # noqa: E402
    ColumnDefinition,
    ColumnType,
    IndexDefinition,
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


class _RecordingLifecycle:
    """Records install_plugin_schema calls; returns a completed envelope."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, object]]] = []

    def install_plugin_schema(
        self, namespace: str, schema_payload: dict[str, object]
    ) -> dict[str, object]:
        self.calls.append((namespace, schema_payload))
        return {"status": "installed"}


class _ExplodingStateService:
    """create_schema MUST NOT be called — the legacy direct-create path is gone."""

    def create_schema(self, namespace: str, schema: dict[str, object]) -> dict[str, object]:
        raise AssertionError(
            f"state_service.create_schema called for {namespace} (schema={schema!r}) — the "
            "legacy fallback should be deleted; init must route through plugin_schema_service"
        )


def _schema_def() -> SchemaDefinition:
    return SchemaDefinition(
        namespace="slice4_probe",
        tables={
            "thing": TableSchema(
                table_name="thing",
                columns={"label": ColumnDefinition(type=ColumnType.TEXT, description="a label")},
            )
        },
    )


def test_plugin_schema_service_is_required() -> None:
    """plugin_schema_service is in REQUIRED_SERVICES → missing binding fails loud."""
    _check(ServiceName.PLUGIN_SCHEMA_SERVICE in REQUIRED_SERVICES,
           "PLUGIN_SCHEMA_SERVICE is in REQUIRED_SERVICES")

    sb = ServiceBindings(app_home="/tmp")
    sb._loaded = True  # noqa: SLF001 — white-box: bypass load() to inject bindings
    present = cast("Any", object())
    sb._bindings = {  # noqa: SLF001
        ServiceName.STATE_SERVICE: present,
        ServiceName.BLOB_STORAGE_SERVICE: present,
        ServiceName.MEMORY_SERVICE: present,
        # EMBEDDING_SERVICE joined REQUIRED_SERVICES (POR §1.3 ◆R2: the embedder is
        # a required, inference-INDEPENDENT service). Bind it here so
        # plugin_schema_service is the SOLE missing service the first validate
        # names, and so the second validate passes once every required is bound.
        ServiceName.EMBEDDING_SERVICE: present,
    }
    raised = False
    try:
        sb.validate_required_services()
    except ServiceBindingError as e:
        raised = "plugin_schema_service" in str(e)
    _check(raised, "validate_required_services RAISES (naming plugin_schema_service) when it is unbound")

    sb._bindings[ServiceName.PLUGIN_SCHEMA_SERVICE] = present  # noqa: SLF001
    passed = True
    try:
        sb.validate_required_services()
    except ServiceBindingError:
        passed = False
    _check(passed, "validate_required_services PASSES once plugin_schema_service is bound")


def test_initialize_schemas_routes_through_lifecycle_only() -> None:
    """initialize_schemas installs every namespace via the lifecycle, never create_schema."""
    lifecycle = _RecordingLifecycle()
    mgr = SchemaManager(
        state_service=cast("Any", _ExplodingStateService()),
        plugin_schema_service=cast("Any", lifecycle),
    )
    mgr.initialize_schemas([_schema_def()])  # _ExplodingStateService raises if legacy path runs
    _check(len(lifecycle.calls) == 1 and lifecycle.calls[0][0] == "slice4_probe",
           f"install_plugin_schema called once for the namespace; got {[c[0] for c in lifecycle.calls]}")
    _check(lifecycle.calls and "thing" in str(lifecycle.calls[0][1]),
           "install_plugin_schema received the serialized schema payload (canonical dict incl. the table)")
    _check("slice4_probe" in mgr.list_schemas(),
           "namespace recorded in the schema registry after lifecycle install")


def test_legacy_dead_code_removed() -> None:
    """The legacy fallback methods + orphaned DDL-string renderers are gone."""
    for attr in (
        "_create_namespace_schema", "_create_table_schema", "_create_table_indexes",
        "_register_schema_metadata", "_convert_table_schema_to_dict",
    ):
        _check(not hasattr(SchemaManager, attr), f"SchemaManager.{attr} removed")
    _check(not hasattr(TableSchema, "get_index_sqls"), "TableSchema.get_index_sqls removed")
    _check(not hasattr(TableSchema, "to_create_sql"), "TableSchema.to_create_sql removed")
    _check(not hasattr(IndexDefinition, "to_sql"), "IndexDefinition.to_sql removed")
    # The IndexDefinition DATA fields the live ddl_renderer consumes must remain.
    idx = IndexDefinition(name="i", columns=["a"], unique=True, using="gin")
    _check(idx.unique and idx.using == "gin",
           "IndexDefinition dataclass + fields (consumed by the live ddl_renderer) preserved")


def main() -> int:
    print("=== core_slice4_schema_manager_unit_smoke ===")
    test_plugin_schema_service_is_required()
    test_initialize_schemas_routes_through_lifecycle_only()
    test_legacy_dead_code_removed()
    print(f"\n{_passed} passed, {len(_failed)} failed")
    if _failed:
        for label in _failed:
            print(f"  FAILED: {label}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
