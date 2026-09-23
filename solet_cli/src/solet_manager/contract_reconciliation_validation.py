"""Closed-schema validation for reconciliation-manifest entries."""

from __future__ import annotations

from typing import cast

from .errors import ContractError
from .models import JsonValue

_RECONCILIATION_ENTRY_KEYS = frozenset(
    {
        "migration_id",
        "source",
        "destination",
        "stage_probe_mappings",
        "first_use_inactive_probe_migrations",
        "operation_statuses_to_reset",
        "answer_value_migrations",
    }
)


def reconciliation_entry(value: JsonValue) -> dict[str, JsonValue]:
    """Normalize one closed-schema reconciliation entry, including legacy defaults."""

    optional = {"operation_statuses_to_reset", "answer_value_migrations"}
    if not isinstance(value, dict) or frozenset(value) - optional != (
        _RECONCILIATION_ENTRY_KEYS - optional
    ):
        raise ContractError("contract reconciliation entry does not match closed v1 schema")
    return {"operation_statuses_to_reset": [], "answer_value_migrations": [], **value}


def parse_operation_statuses_to_reset(value: JsonValue) -> tuple[str, ...]:
    """Parse the declared operation ids that reconciliation invalidates."""

    if not isinstance(value, list) or any(
        not isinstance(item, str) or not item for item in value
    ):
        raise ContractError("contract reconciliation operation status resets must be an array of ids")
    values = cast(tuple[str, ...], tuple(value))
    if len(set(values)) != len(values):
        raise ContractError("contract reconciliation operation status resets must be unique")
    return values
