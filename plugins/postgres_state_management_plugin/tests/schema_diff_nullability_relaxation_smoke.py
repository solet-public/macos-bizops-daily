#!/usr/bin/env python3
"""Typed-schema smoke for the F1 managed-dispatch nullability bridge."""

from __future__ import annotations

import dataclasses
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT / "ananta" / "src"))
sys.path.insert(
    0,
    str(REPO_ROOT / "plugins" / "agent_messaging_plugin" / "src"),
)
sys.path.insert(
    0,
    str(REPO_ROOT / "plugins" / "macos_self_deployment_plugin" / "src"),
)
sys.path.insert(
    0,
    str(REPO_ROOT / "plugins" / "postgres_state_management_plugin" / "src"),
)

from ananta.llm.agent_messaging.role_binding import (  # noqa: E402
    AGENT_ROLE_BINDING_NAMESPACE,
)
from ananta.types.schema_types import SchemaDefinition  # noqa: E402
from macos_self_deployment_plugin.schema_preflight import (  # noqa: E402
    classify_snapshot_diff,
    schemas_to_snapshot,
)
from postgres_state_management_plugin.postgres_backend.schema_diff import (  # noqa: E402
    _is_nullability_relaxation_only,
    diff_schema,
)

from agent_messaging_plugin.schema import (  # noqa: E402
    TABLE_MANAGED_DISPATCH,
    get_managed_dispatch_schema,
)

_LEGACY_TTL_COLUMNS = (
    "ttl_seconds",
    "expires_at",
    "expires_at_window_seconds",
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


def _schemas() -> tuple[SchemaDefinition, SchemaDefinition]:
    declared_table = get_managed_dispatch_schema()
    current_columns = dict(declared_table.columns)
    for name in _LEGACY_TTL_COLUMNS:
        current_columns[name] = dataclasses.replace(
            current_columns[name],
            not_null=True,
        )
    current_table = dataclasses.replace(declared_table, columns=current_columns)
    return (
        SchemaDefinition(
            namespace=AGENT_ROLE_BINDING_NAMESPACE,
            tables={TABLE_MANAGED_DISPATCH: current_table},
        ),
        SchemaDefinition(
            namespace=AGENT_ROLE_BINDING_NAMESPACE,
            tables={TABLE_MANAGED_DISPATCH: declared_table},
        ),
    )


def test_exact_f1_relaxation() -> None:
    current, declared = _schemas()
    declared_table = declared.tables[TABLE_MANAGED_DISPATCH]
    _check(
        all(not declared_table.columns[name].not_null for name in _LEGACY_TTL_COLUMNS),
        "the three legacy managed-dispatch TTL columns are declared nullable",
    )
    _check(
        all(
            _is_nullability_relaxation_only(
                current.tables[TABLE_MANAGED_DISPATCH].columns[name],
                declared_table.columns[name],
            )
            for name in _LEGACY_TTL_COLUMNS
        ),
        "all three changes are single-axis nullability relaxations",
    )
    operations = diff_schema(
        namespace=AGENT_ROLE_BINDING_NAMESPACE,
        current=current,
        declared=declared,
        mode="update",
        schema_name="example",
        current_index_physical_names={},
    )
    rendered = [operation.as_string() for operation in operations]
    _check(len(rendered) == 3, f"exactly three typed DDL operations emitted; got {rendered!r}")
    _check(
        all("DROP NOT NULL" in operation for operation in rendered)
        and all(any(f'"{name}"' in operation for operation in rendered) for name in _LEGACY_TTL_COLUMNS),
        "typed schema diff emits DROP NOT NULL for exactly the three bridge columns",
    )

    old_snapshot = schemas_to_snapshot({AGENT_ROLE_BINDING_NAMESPACE: current})
    new_snapshot = schemas_to_snapshot({AGENT_ROLE_BINDING_NAMESPACE: declared})
    verdict = classify_snapshot_diff(old_snapshot, new_snapshot)
    _check(
        verdict.is_additive and not verdict.breaking_changes,
        "blue-green schema preflight classifies the exact relaxation as additive",
    )


def test_tightening_and_compound_changes_still_refuse() -> None:
    current, declared = _schemas()
    nullable = declared.tables[TABLE_MANAGED_DISPATCH].columns["ttl_seconds"]
    required = current.tables[TABLE_MANAGED_DISPATCH].columns["ttl_seconds"]
    _check(
        not _is_nullability_relaxation_only(nullable, required),
        "nullable to NOT NULL is not misclassified as a relaxation",
    )
    compound = dataclasses.replace(nullable, type=required.type, unique=True)
    _check(
        not _is_nullability_relaxation_only(required, compound),
        "nullability plus another shape change remains outside the relaxation path",
    )


def main() -> int:
    test_exact_f1_relaxation()
    test_tightening_and_compound_changes_still_refuse()
    print(f"\nschema diff nullability relaxation smoke: {_passed} passed, {len(_failed)} failed")
    return 1 if _failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
