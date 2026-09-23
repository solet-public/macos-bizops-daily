"""Per-probe evidence carried by v2 contract reconciliation receipts."""

from __future__ import annotations

from typing import cast

from .contracts import ContractReconciliation
from .errors import StateError
from .models import CheckpointStatus, JsonValue
from .transaction import Transaction

_ENTRY_KEYS = frozenset(
    {
        "identity",
        "prior_status",
        "new_status",
        "rule_citation",
        "manifest_entry_id",
        "source_digest",
        "destination_digest",
        "migration_reason",
        "evidence_disposition",
    }
)
_CITATION_KEYS = (
    "rule_citation",
    "manifest_entry_id",
    "source_digest",
    "destination_digest",
    "migration_reason",
)


def per_probe_migration_data(
    before: Transaction,
    after: Transaction,
    reconciliation: ContractReconciliation,
) -> list[JsonValue]:
    """Render every declared status-map supersession for the recovery receipt."""

    entries: list[JsonValue] = []
    for rule in reconciliation.first_use_inactive_probe_migrations:
        stage_id, boundary, probe_id = rule.destination
        prior = before.stage_probe_statuses[stage_id][boundary][probe_id]
        current = after.stage_probe_statuses[stage_id][boundary][probe_id]
        if prior is current:
            continue
        _validate_transition(prior, current)
        entries.append(
            {
                "identity": [stage_id, boundary, probe_id],
                "prior_status": prior.value,
                "new_status": current.value,
                "rule_citation": rule.rule_id,
                "manifest_entry_id": rule.manifest_entry_id,
                "source_digest": rule.source_digest,
                "destination_digest": rule.destination_digest,
                "migration_reason": rule.migration_reason,
                "evidence_disposition": "preserve",
            }
        )
    return entries


def validate_per_probe_migrations(value: JsonValue | None) -> None:
    """Fail closed unless a v2 receipt carries exact supersession evidence."""

    if not isinstance(value, list):
        raise StateError("contract reconciliation receipt per-probe migrations are invalid")
    identities: set[tuple[str, str, str]] = set()
    for entry in value:
        identity = _parse_entry(entry)
        if identity in identities:
            raise StateError("contract reconciliation receipt per-probe identity is invalid")
        identities.add(identity)


def _parse_entry(value: JsonValue) -> tuple[str, str, str]:
    if not isinstance(value, dict) or frozenset(value) != _ENTRY_KEYS:
        raise StateError("contract reconciliation receipt per-probe migration is invalid")
    identity = _parse_identity(value.get("identity"))
    _validate_transition_values(value)
    _validate_citation_values(value)
    return identity


def _parse_identity(value: JsonValue | None) -> tuple[str, str, str]:
    if (
        not isinstance(value, list)
        or len(value) != 3
        or not all(isinstance(item, str) and item for item in value)
        or value[1] not in {"entry", "exit"}
    ):
        raise StateError("contract reconciliation receipt per-probe identity is invalid")
    return cast(str, value[0]), value[1], cast(str, value[2])


def _validate_transition(
    prior: CheckpointStatus,
    current: CheckpointStatus,
) -> None:
    if prior is not CheckpointStatus.BLOCKED or current is not CheckpointStatus.PENDING:
        raise StateError("first-use migration produced an unexpected status transition")


def _validate_transition_values(value: dict[str, JsonValue]) -> None:
    """Accept the two closed schema-v2 reader dialects, never a field-wise mix."""

    # bcd24d21a wrote the legacy BLOCKED -> NOT_APPLICABLE/preserve dialect.
    # 9b0bf3e55 replaced it with BLOCKED -> PENDING/preserve without changing
    # the on-disk schema version, so v2 readers must retain both exact tuples.
    transition = (
        value.get("prior_status"),
        value.get("new_status"),
        value.get("evidence_disposition"),
    )
    if transition not in {
        (CheckpointStatus.BLOCKED.value, CheckpointStatus.PENDING.value, "preserve"),
        (CheckpointStatus.BLOCKED.value, CheckpointStatus.NOT_APPLICABLE.value, "preserve"),
    }:
        raise StateError("contract reconciliation receipt per-probe transition is invalid")


def _validate_citation_values(value: dict[str, JsonValue]) -> None:
    if not all(isinstance(value.get(key), str) and value[key] for key in _CITATION_KEYS):
        raise StateError("contract reconciliation receipt per-probe citation is invalid")
