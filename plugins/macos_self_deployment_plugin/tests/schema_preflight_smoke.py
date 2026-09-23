#!/usr/bin/env python3
"""§8.7 schema-preflight classifier smoke (no pytest, pure — no DB, no boot).

Design ``2026-06-27_true_local_blue_green_materialized_artifacts_design.md``
§3 / §8.7: the durable code-rollback guarantee only holds over an
unchanged/additive schema, so a deploy whose declared schema diff is
**non-additive** must be refused/flagged. This exercises the pure diff
classifier (``schema_preflight.classify_snapshot_diff``) that backs the
gate, plus the typed-``SchemaDefinition`` → canonical-snapshot reducer
(``schemas_to_snapshot``).

Additive (rollback-safe → ``is_additive=True``):
  added namespace / table / nullable-or-defaulted column, relaxed NOT NULL.
Non-additive (rollback-unsafe → ``is_additive=False``), fail-closed:
  dropped namespace/table/column, type change, tightened nullability, a new
  NOT NULL column without default, a primary-key change, a new unique.

Run:
    .venv/bin/python3 plugins/macos_self_deployment_plugin/tests/schema_preflight_smoke.py
"""

from __future__ import annotations

import sys
from pathlib import Path

_PLUGIN_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_PLUGIN_SRC) not in sys.path:
    sys.path.insert(0, str(_PLUGIN_SRC))

from ananta.types.column_types import ColumnType  # noqa: E402
from ananta.types.schema_types import (  # noqa: E402
    ColumnDefinition,
    SchemaDefinition,
    TableSchema,
)
from macos_self_deployment_plugin.schema_preflight import (  # noqa: E402
    SchemaSnapshot,
    classify_snapshot_diff,
    schemas_to_snapshot,
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


def _col(
    type_: str = "TEXT",
    *,
    not_null: bool = False,
    default: object | None = None,
    primary_key: bool = False,
    unique: bool = False,
    type_params: dict[str, object] | None = None,
) -> dict[str, object]:
    """A canonical-snapshot column entry (matching ``schemas_to_snapshot``)."""
    return {
        "type": type_,
        "type_params": type_params or {},
        "not_null": not_null,
        "has_default": default is not None,
        "primary_key": primary_key,
        "unique": unique,
    }


def _snap(
    columns: dict[str, dict[str, object]],
) -> dict[str, dict[str, dict[str, dict[str, object]]]]:
    """A one-namespace, one-table snapshot around ``columns``."""
    return {"plugin": {"thing": columns}}


def _additive_cases() -> None:
    base = _snap({"id": _col("INTEGER", not_null=True, primary_key=True), "name": _col()})

    _check(
        classify_snapshot_diff(base, base).is_additive,
        "identical schema → additive",
    )
    _check(
        classify_snapshot_diff(base, {**base, "plugin2": {"t": {}}}).is_additive,
        "added namespace → additive",
    )
    new_table = {"plugin": {"thing": base["plugin"]["thing"], "extra": {}}}
    _check(
        classify_snapshot_diff(base, new_table).is_additive,
        "added table → additive",
    )
    add_nullable = _snap({**base["plugin"]["thing"], "note": _col()})
    _check(
        classify_snapshot_diff(base, add_nullable).is_additive,
        "added nullable column → additive",
    )
    add_defaulted = _snap(
        {**base["plugin"]["thing"], "flag": _col("BOOLEAN", not_null=True, default=False)}
    )
    _check(
        classify_snapshot_diff(base, add_defaulted).is_additive,
        "added NOT NULL column WITH default → additive",
    )
    relax = _snap({"id": _col("INTEGER", not_null=True, primary_key=True), "name": _col()})
    tighten_base = _snap(
        {"id": _col("INTEGER", not_null=True, primary_key=True), "name": _col(not_null=True)}
    )
    _check(
        classify_snapshot_diff(tighten_base, relax).is_additive,
        "relaxed NOT NULL → nullable → additive",
    )


def _non_additive_cases() -> None:
    base = _snap({"id": _col("INTEGER", not_null=True, primary_key=True), "name": _col()})

    cases: list[tuple[str, SchemaSnapshot]] = [
        ("dropped namespace", {}),
        ("dropped table", {"plugin": {}}),
        ("dropped column", _snap({"id": base["plugin"]["thing"]["id"]})),
        (
            "column type change",
            _snap({**base["plugin"]["thing"], "name": _col("INTEGER")}),
        ),
        (
            "type_params change",
            _snap(
                {
                    **base["plugin"]["thing"],
                    "name": _col(type_params={"length": 255}),
                }
            ),
        ),
        (
            "tightened nullability (nullable → NOT NULL)",
            _snap({**base["plugin"]["thing"], "name": _col(not_null=True)}),
        ),
        (
            "added NOT NULL column WITHOUT default",
            _snap({**base["plugin"]["thing"], "req": _col(not_null=True)}),
        ),
        (
            "primary-key change",
            _snap(
                {
                    "id": _col("INTEGER", not_null=True),
                    "name": _col(primary_key=True),
                }
            ),
        ),
        (
            "added unique constraint",
            _snap({**base["plugin"]["thing"], "name": _col(unique=True)}),
        ),
    ]
    for label, new in cases:
        verdict = classify_snapshot_diff(base, new)
        _check(
            not verdict.is_additive and bool(verdict.breaking_changes),
            f"{label} → non-additive (refused)",
        )


def _producer_case() -> None:
    """``schemas_to_snapshot`` reduces typed SchemaDefinitions to the canonical shape."""
    schema = SchemaDefinition(
        namespace="demo_plugin",
        tables={
            "widget": TableSchema(
                table_name="widget",
                columns={
                    "id": ColumnDefinition(
                        type=ColumnType.INTEGER, primary_key=True, not_null=True
                    ),
                    "label": ColumnDefinition(type=ColumnType.TEXT),
                },
            )
        },
    )
    snap = schemas_to_snapshot({"demo_plugin": schema})
    _check(
        set(snap) == {"demo_plugin"}
        and set(snap["demo_plugin"]) == {"widget"}
        and set(snap["demo_plugin"]["widget"]) == {"id", "label"}
        and snap["demo_plugin"]["widget"]["id"]["primary_key"] is True
        and snap["demo_plugin"]["widget"]["id"]["not_null"] is True
        and snap["demo_plugin"]["widget"]["label"]["not_null"] is False,
        "schemas_to_snapshot produces the canonical namespace→table→column shape",
    )
    # round-trip: a schema vs itself (via the producer) is additive.
    _check(
        classify_snapshot_diff(snap, snap).is_additive,
        "producer output diffed against itself → additive",
    )


def run_smoke() -> int:
    print("=== schema_preflight_smoke (§8.7: §3 DDL-free gate classifier) ===")
    _additive_cases()
    _non_additive_cases()
    _producer_case()
    print(f"\nschema_preflight_smoke: {_passed} passed, {len(_failed)} failed")
    for label in _failed:
        print(f"  FAILED: {label}")
    return 1 if _failed else 0


if __name__ == "__main__":
    sys.exit(run_smoke())
