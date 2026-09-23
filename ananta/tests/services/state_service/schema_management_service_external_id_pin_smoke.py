#!/usr/bin/env python3
"""Behavior-pin smoke for ``SchemaManagementService``'s ``external_id`` rule (no pytest).

Schema-debt-external-id lane, 2a (2026-08-06): the ColumnDefinition-based
path (``SchemaStandardizer``) was relaxed to let a table declare
``external_id`` with ``unique=False``, opting out of the platform-wide
standalone uniqueness. This service's own, independent, raw-dict-dialect
validator (``_validate_no_standard_field_overrides`` /
``_enhance_table_with_standard_fields``) was DELIBERATELY left unrelaxed —
see the pointed comment in ``schema_management_service.py`` and the
"Path 2 asymmetry" section of
``workbench/2026-08-06_schema_debt_external_id_findings_schema-debt-impl.md``.

This is a TRIPWIRE, not a defect regression test: if a future change relaxes
this validator to match Path 1 without reading the follow-on record, this
leg goes red. That is the intended signal — land on the follow-on's design
question (unify the two validators vs. a second parallel relax) instead of
letting the two copies drift further apart silently.

Coverage:
* ``external_id_override_without_unique_still_rejected`` — a raw-dict
  ``external_id`` override lacking ``UNIQUE`` still raises ``ValueError``
  through the live ``create_schema`` call path (``state_manager.py`` /
  ``event_persistence_manager.py`` / ``service_manager.py`` /
  ``discovery_service/service.py`` all reach this).
* ``external_id_override_with_unique_still_accepted`` — the one value this
  path has always accepted (``UNIQUE`` present) is unaffected by 2a.

Run:
    .venv/bin/python3 ananta/tests/services/state_service/schema_management_service_external_id_pin_smoke.py
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT / "ananta" / "src"))

from ananta.services.schema_management.bootstrap_schema_storage import (  # noqa: E402
    BootstrapSchemaStorage,
)
from ananta.services.schema_management.schema_management_service import (  # noqa: E402
    SchemaManagementService,
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


def _service() -> SchemaManagementService:
    return SchemaManagementService(storage_strategy=BootstrapSchemaStorage())


def external_id_override_without_unique_still_rejected() -> None:
    print("external_id_override_without_unique_still_rejected:")
    service = _service()
    schema = {"tables": {"thing": {"external_id": "TEXT"}}}
    try:
        service.create_schema("pin_test_ns", schema)
    except ValueError as exc:
        _check(
            "external_id" in str(exc) and "UNIQUE" in str(exc),
            f"raises ValueError naming external_id/UNIQUE (got: {exc})",
        )
    else:
        _check(
            False,
            "expected ValueError for external_id override without UNIQUE — "
            "did the Path-2 validator get relaxed without reading the "
            "follow-on record? See the pointed comment in "
            "schema_management_service.py.",
        )


def external_id_override_with_unique_still_accepted() -> None:
    print("external_id_override_with_unique_still_accepted:")
    service = _service()
    schema = {"tables": {"thing": {"external_id": "TEXT UNIQUE NOT NULL"}}}
    try:
        result = service.create_schema("pin_test_ns_2", schema)
    except ValueError as exc:
        _check(False, f"unexpected ValueError for a UNIQUE override: {exc}")
    else:
        _check(
            result.get("action_status") == "completed",
            f"UNIQUE override still accepted (got: {result})",
        )


def main() -> int:
    print("=== schema_management_service_external_id_pin_smoke ===")
    external_id_override_without_unique_still_rejected()
    external_id_override_with_unique_still_accepted()
    print(f"\n{_passed} passed, {len(_failed)} failed")
    if _failed:
        for label in _failed:
            print(f"  FAILED: {label}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
