"""Closed maintenance-operation journal schema and transitions."""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

from solet_setup_contracts import canonical_sha256

from .errors import StateConflictError, StateError
from .models import JsonValue, MaintenanceOperationStatus, MaintenanceStageStatus
from .state_io import atomic_write_json

_MAINTENANCE_KEYS = frozenset(
    {
        "schema_version",
        "operation_id",
        "instance_id",
        "kind",
        "idempotency_key",
        "status",
        "input",
        "current_identity",
        "candidate_identity",
        "contract_digests",
        "approval",
        "stage_statuses",
        "attempts",
        "evidence",
        "preservation_inventory",
        "rollback_class",
        "result",
        "created_at",
        "updated_at",
    }
)
_IMPORT_STAGE_IDS = (
    "inspection_revalidated",
    "inspection_bundle_cached",
    "inventory_published",
    "enrollment_verified",
)
_TERMINAL_MAINTENANCE_STATUSES = frozenset(
    {
        MaintenanceOperationStatus.VERIFIED.value,
        MaintenanceOperationStatus.BLOCKED.value,
        MaintenanceOperationStatus.FAILED.value,
        MaintenanceOperationStatus.ABANDONED.value,
    }
)
_OPERATION_TRANSITIONS = {
    MaintenanceOperationStatus.PREPARED.value: frozenset(
        {
            MaintenanceOperationStatus.BUNDLE_CACHED.value,
            MaintenanceOperationStatus.BLOCKED.value,
            MaintenanceOperationStatus.FAILED.value,
            MaintenanceOperationStatus.ABANDONED.value,
        }
    ),
    MaintenanceOperationStatus.BUNDLE_CACHED.value: frozenset(
        {
            MaintenanceOperationStatus.INVENTORY_PUBLISHED.value,
            MaintenanceOperationStatus.BLOCKED.value,
            MaintenanceOperationStatus.FAILED.value,
            MaintenanceOperationStatus.ABANDONED.value,
        }
    ),
    MaintenanceOperationStatus.INVENTORY_PUBLISHED.value: frozenset(
        {
            MaintenanceOperationStatus.VERIFIED.value,
            MaintenanceOperationStatus.BLOCKED.value,
            MaintenanceOperationStatus.FAILED.value,
            MaintenanceOperationStatus.ABANDONED.value,
        }
    ),
}
_STAGE_TRANSITIONS = {
    MaintenanceStageStatus.PENDING.value: frozenset(
        {
            MaintenanceStageStatus.APPLYING.value,
            MaintenanceStageStatus.VERIFIED.value,
            MaintenanceStageStatus.BLOCKED.value,
            MaintenanceStageStatus.FAILED.value,
        }
    ),
    MaintenanceStageStatus.APPLYING.value: frozenset(
        {
            MaintenanceStageStatus.VERIFIED.value,
            MaintenanceStageStatus.BLOCKED.value,
            MaintenanceStageStatus.FAILED.value,
        }
    ),
    MaintenanceStageStatus.BLOCKED.value: frozenset({MaintenanceStageStatus.APPLYING.value}),
    MaintenanceStageStatus.FAILED.value: frozenset({MaintenanceStageStatus.APPLYING.value}),
    MaintenanceStageStatus.VERIFIED.value: frozenset(),
}
_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
_OPERATION_ID = re.compile(r"^opr_[0-9a-f]{32}$")
_INSTANCE_ID = re.compile(r"^ins_[0-9a-f]{32}$")
_COMMIT = re.compile(r"^[0-9a-f]{40}$")
_EVIDENCE_ID = re.compile(r"^evd_[0-9a-f]{32}$")

# The maintenance journal is deliberately a closed, Manager-state-only record.
# Its fixed envelope ties one generated operation id to one inventory id.
# Input identity is the inspected target, filesystem tuple, and channel.
# Current identity binds the strict provenance seed and observed Git head.
# The approval fingerprint records the preview that is eligible for application.
# Stage entries are monotonic and their derived keys bind the immutable inputs.
# Attempt history is append-only, numbered, and projects non-secret evidence.
# Operation status cannot move ahead of the verified stage frontier.
# Preservation counters explicitly prove that no target, secret, database, or
# process vector was used while enrolling an existing installation.
# Terminal results are typed separately from nonterminal journal states.
# Atomic writes reread the exact parsed document before returning to callers.


def parse_maintenance_operation_bytes(raw_bytes: bytes) -> dict[str, JsonValue]:
    """Parse the closed Manager-only maintenance journal envelope."""
    try:
        raw = json.loads(raw_bytes.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise StateError("maintenance operation is unreadable") from exc
    return _validated_maintenance_document(raw)


def read_maintenance_operation(path: Path) -> dict[str, JsonValue]:
    return parse_maintenance_operation_bytes(path.read_bytes())


def create_import_maintenance_operation(
    *,
    operation_id: str,
    instance_id: str,
    idempotency_key: str,
    name: str,
    canonical_target: str,
    target_device: int,
    target_inode: int,
    channel_id: str,
    provenance_seed_id: str,
    head_commit: str,
    head_tree: str,
    inspection_bundle_digest: str,
    diagnostic_contract_digest: str,
    approval_fingerprint: str,
    manager_write_paths: tuple[str, ...],
    non_touch_surfaces: tuple[str, ...],
    timestamp: str | None = None,
) -> dict[str, JsonValue]:
    """Construct the sole Step-3 maintenance-operation initializer."""
    now = utc_now() if timestamp is None else timestamp
    non_touch_values: list[JsonValue] = [*sorted(non_touch_surfaces)]
    manager_path_values: list[JsonValue] = [*sorted(manager_write_paths)]
    preservation_inventory: dict[str, JsonValue] = {
        "non_touch_surfaces": non_touch_values,
        "target_byte_writes": 0,
        "manager_state_writes": 1,
        "manager_write_paths": manager_path_values,
        "secret_value_reads": 0,
        "secret_value_writes": 0,
        "database_reads": 0,
        "database_writes": 0,
        "target_process_executions": 0,
        "permission_prompts": 0,
        "invoked_vectors": [],
        "opened_resources": [],
    }
    base: dict[str, JsonValue] = {
        "schema_version": 1,
        "operation_id": operation_id,
        "instance_id": instance_id,
        "kind": "import",
        "idempotency_key": idempotency_key,
        "status": MaintenanceOperationStatus.PREPARED.value,
        "input": {
            "name": name,
            "canonical_target": canonical_target,
            "target_filesystem_identity": {"device": target_device, "inode": target_inode},
            "channel_id": channel_id,
        },
        "current_identity": {
            "provenance_seed_id": provenance_seed_id,
            "head_commit": head_commit,
            "head_tree": head_tree,
            "inspection_bundle_digest": inspection_bundle_digest,
        },
        "candidate_identity": None,
        "contract_digests": {
            "diagnostic": diagnostic_contract_digest,
            "current": None,
            "candidate": None,
        },
        "approval": {"fingerprint": approval_fingerprint, "recorded_at": now},
        "stage_statuses": {
            "inspection_revalidated": "verified",
            "inspection_bundle_cached": "pending",
            "inventory_published": "pending",
            "enrollment_verified": "pending",
        },
        "attempts": [],
        "evidence": [],
        "preservation_inventory": preservation_inventory,
        "rollback_class": "manager_state_only",
        "result": None,
        "created_at": now,
        "updated_at": now,
    }
    evidence = maintenance_evidence(
        operation_id, 0, "digest", "inspection_fingerprint_match", approval_fingerprint
    )
    return append_maintenance_attempt(
        base,
        stage_id="inspection_revalidated",
        status=MaintenanceStageStatus.VERIFIED.value,
        evidence=(evidence,),
        timestamp=now,
    )


def append_maintenance_attempt(
    operation: dict[str, JsonValue],
    *,
    stage_id: str,
    status: str,
    evidence: tuple[dict[str, JsonValue], ...],
    error_kind: str | None = None,
    timestamp: str | None = None,
) -> dict[str, JsonValue]:
    """Append exactly one immutable closed attempt and derive public evidence."""
    current = _validated_maintenance_document(operation)
    _validate_attempt_request(stage_id, status, error_kind)
    stages = cast(dict[str, JsonValue], current["stage_statuses"])
    previous_status = stages[stage_id]
    attempts = list(cast(list[JsonValue], current["attempts"]))
    _validate_attempt_transition(stage_id, status, previous_status, attempts)
    now = utc_now() if timestamp is None else timestamp
    attempt_number = len(attempts)
    expected_key = _stage_key(current, stage_id)
    finished_at: JsonValue = None if status == "applying" else now
    attempt: dict[str, JsonValue] = {
        "attempt": attempt_number,
        "stage_id": stage_id,
        "stage_key": expected_key,
        "status": status,
        "started_at": now,
        "finished_at": finished_at,
        "evidence": list(evidence),
        "error_kind": error_kind,
    }
    attempts.append(attempt)
    next_value = _copy_json_object(current)
    next_stages = cast(dict[str, JsonValue], next_value["stage_statuses"])
    next_stages[stage_id] = status
    next_value["attempts"] = attempts
    next_value["evidence"] = _public_evidence(attempts)
    next_value["updated_at"] = now
    return _validated_maintenance_document(next_value)


def _validate_attempt_request(stage_id: str, status: str, error_kind: str | None) -> None:
    if stage_id not in _IMPORT_STAGE_IDS or status not in {item.value for item in MaintenanceStageStatus}:
        raise StateError("maintenance attempt has an unknown stage or status")
    if (status in {"blocked", "failed"}) != (error_kind is not None):
        raise StateError("maintenance attempt error_kind does not match status")


def _validate_attempt_transition(
    stage_id: str, status: str, previous_status: JsonValue, attempts: list[JsonValue]
) -> None:
    if _is_initial_revalidation(stage_id, status, previous_status, attempts):
        return
    if not isinstance(previous_status, str) or status not in _STAGE_TRANSITIONS[previous_status]:
        raise StateConflictError("maintenance stage transition is illegal")


def _is_initial_revalidation(
    stage_id: str, status: str, previous_status: JsonValue, attempts: list[JsonValue]
) -> bool:
    return (
        not attempts
        and stage_id == "inspection_revalidated"
        and previous_status == MaintenanceStageStatus.VERIFIED.value
        and status == MaintenanceStageStatus.VERIFIED.value
    )


def transition_maintenance_operation(
    operation: dict[str, JsonValue],
    *,
    status: str,
    result: dict[str, JsonValue] | None = None,
    timestamp: str | None = None,
) -> dict[str, JsonValue]:
    """Move a journal through its closed operation-status graph."""
    current = _validated_maintenance_document(operation)
    old_status = current["status"]
    if not isinstance(old_status, str) or status not in _OPERATION_TRANSITIONS.get(old_status, ()):
        raise StateConflictError("maintenance operation transition is illegal")
    next_value = _copy_json_object(current)
    next_value["status"] = status
    next_value["result"] = result
    next_value["updated_at"] = utc_now() if timestamp is None else timestamp
    return _validated_maintenance_document(next_value)


def write_maintenance_operation(
    path: Path, previous: dict[str, JsonValue] | None, next_value: dict[str, JsonValue]
) -> None:
    """Atomically persist only an identity-preserving maintenance transition."""
    next_document = _validated_maintenance_document(next_value)
    if previous is None:
        if next_document["status"] != MaintenanceOperationStatus.PREPARED.value:
            raise StateConflictError("maintenance operation must begin prepared")
    else:
        previous_document = _validated_maintenance_document(previous)
        _validate_maintenance_transition(previous_document, next_document)
    atomic_write_json(path, next_document)
    if read_maintenance_operation(path) != next_document:
        raise StateError("maintenance operation read-back mismatch")


def _validate_maintenance_transition(
    previous: dict[str, JsonValue], next_value: dict[str, JsonValue]
) -> None:
    for key in (
        "operation_id",
        "instance_id",
        "kind",
        "idempotency_key",
        "input",
        "current_identity",
        "candidate_identity",
        "contract_digests",
        "approval",
        "preservation_inventory",
        "rollback_class",
        "created_at",
    ):
        if previous[key] != next_value[key]:
            raise StateConflictError("maintenance operation immutable identity changed")
    previous_attempts = cast(list[JsonValue], previous["attempts"])
    next_attempts = cast(list[JsonValue], next_value["attempts"])
    if next_attempts[: len(previous_attempts)] != previous_attempts or len(next_attempts) - len(previous_attempts) not in {0, 1}:
        raise StateConflictError("maintenance operation attempts are not an immutable prefix")
    old_status = cast(str, previous["status"])
    new_status = cast(str, next_value["status"])
    if old_status != new_status and new_status not in _OPERATION_TRANSITIONS.get(old_status, ()):
        raise StateConflictError("maintenance operation transition is illegal")
    old_stages = cast(dict[str, JsonValue], previous["stage_statuses"])
    new_stages = cast(dict[str, JsonValue], next_value["stage_statuses"])
    for stage_id in _IMPORT_STAGE_IDS:
        old_stage = cast(str, old_stages[stage_id])
        new_stage = cast(str, new_stages[stage_id])
        if old_stage != new_stage and new_stage not in _STAGE_TRANSITIONS[old_stage]:
            raise StateConflictError("maintenance stage transition is illegal")


def _validated_maintenance_document(raw: object) -> dict[str, JsonValue]:
    if not isinstance(raw, dict) or frozenset(raw) != _MAINTENANCE_KEYS:
        raise StateError("maintenance operation does not match the closed schema")
    document = cast(dict[str, JsonValue], raw)
    _exact(document, "schema_version", 1)
    _identifier(document["operation_id"], _OPERATION_ID, "operation id")
    _identifier(document["instance_id"], _INSTANCE_ID, "instance id")
    _one_of(document["kind"], {"import", "update", "doctor"}, "kind")
    _digest_value(document["idempotency_key"], "idempotency key")
    status = _one_of(document["status"], {item.value for item in MaintenanceOperationStatus}, "status")
    _input(document["input"])
    _current_identity(document["current_identity"])
    if document["candidate_identity"] is not None:
        raise StateError("maintenance import candidate identity must be null")
    _contract_digests(document["contract_digests"])
    _approval(document["approval"])
    stages = _stage_statuses(document["stage_statuses"])
    attempts = _attempts(document, document["attempts"])
    if document["evidence"] != _public_evidence(attempts):
        raise StateError("maintenance evidence must be the ordered attempt projection")
    _preservation_inventory(document["preservation_inventory"])
    _exact(document, "rollback_class", "manager_state_only")
    _result(status, document["instance_id"], document["result"])
    _timestamp(document["created_at"])
    _timestamp(document["updated_at"])
    _status_stage_invariants(status, stages)
    return _copy_json_object(document)


def _exact(document: dict[str, JsonValue], key: str, expected: JsonValue) -> None:
    if document[key] != expected:
        raise StateError(f"maintenance operation {key} is invalid")


def _identifier(value: JsonValue, expression: re.Pattern[str], label: str) -> str:
    if not isinstance(value, str) or expression.fullmatch(value) is None:
        raise StateError(f"maintenance operation {label} is invalid")
    return value


def _text(value: JsonValue, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise StateError(f"maintenance operation {label} is invalid")
    return value


def _digest_value(value: JsonValue, label: str) -> str:
    value = _text(value, label)
    if _DIGEST.fullmatch(value) is None:
        raise StateError(f"maintenance operation {label} is invalid")
    return value


def _one_of(value: JsonValue, allowed: set[str], label: str) -> str:
    value = _text(value, label)
    if value not in allowed:
        raise StateError(f"maintenance operation {label} is invalid")
    return value


def _object(value: JsonValue, keys: set[str], label: str) -> dict[str, JsonValue]:
    if not isinstance(value, dict) or set(value) != keys:
        raise StateError(f"maintenance operation {label} has invalid keys")
    return value


def _nonnegative_integer(value: JsonValue, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise StateError(f"maintenance operation {label} is invalid")
    return value


def _timestamp(value: JsonValue) -> str:
    value = _text(value, "timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise StateError("maintenance operation timestamp is invalid") from exc
    if not value.endswith("Z") or parsed.tzinfo is None:
        raise StateError("maintenance operation timestamp is invalid")
    return value


def _input(value: JsonValue) -> None:
    item = _object(
        value,
        {"name", "canonical_target", "target_filesystem_identity", "channel_id"},
        "input",
    )
    _text(item["name"], "name")
    _text(item["canonical_target"], "canonical target")
    _text(item["channel_id"], "channel id")
    identity = _object(item["target_filesystem_identity"], {"device", "inode"}, "filesystem identity")
    _nonnegative_integer(identity["device"], "device")
    _nonnegative_integer(identity["inode"], "inode")


def _current_identity(value: JsonValue) -> None:
    item = _object(
        value,
        {"provenance_seed_id", "head_commit", "head_tree", "inspection_bundle_digest"},
        "current identity",
    )
    _text(item["provenance_seed_id"], "provenance seed id")
    _identifier(item["head_commit"], _COMMIT, "head commit")
    _identifier(item["head_tree"], _COMMIT, "head tree")
    _digest_value(item["inspection_bundle_digest"], "inspection bundle digest")


def _contract_digests(value: JsonValue) -> None:
    item = _object(value, {"diagnostic", "current", "candidate"}, "contract digests")
    _digest_value(item["diagnostic"], "diagnostic contract digest")
    if item["current"] is not None or item["candidate"] is not None:
        raise StateError("maintenance import contract identities must be null")


def _approval(value: JsonValue) -> None:
    item = _object(value, {"fingerprint", "recorded_at"}, "approval")
    _digest_value(item["fingerprint"], "approval fingerprint")
    _timestamp(item["recorded_at"])


def _stage_statuses(value: JsonValue) -> dict[str, JsonValue]:
    item = _object(value, set(_IMPORT_STAGE_IDS), "stage statuses")
    for stage_id in _IMPORT_STAGE_IDS:
        _one_of(item[stage_id], {member.value for member in MaintenanceStageStatus}, "stage status")
    return item


def _attempts(document: dict[str, JsonValue], value: JsonValue) -> list[JsonValue]:
    if not isinstance(value, list):
        raise StateError("maintenance operation attempts are invalid")
    parsed: list[JsonValue] = []
    for index, item in enumerate(value):
        attempt = _object(
            item,
            {"attempt", "stage_id", "stage_key", "status", "started_at", "finished_at", "evidence", "error_kind"},
            "attempt",
        )
        if _nonnegative_integer(attempt["attempt"], "attempt number") != index:
            raise StateError("maintenance attempt number is invalid")
        stage_id = _one_of(attempt["stage_id"], set(_IMPORT_STAGE_IDS), "attempt stage")
        if attempt["stage_key"] != _stage_key(document, stage_id):
            raise StateError("maintenance attempt stage key is invalid")
        status = _one_of(attempt["status"], {member.value for member in MaintenanceStageStatus}, "attempt status")
        _timestamp(attempt["started_at"])
        if (attempt["finished_at"] is None) != (status == MaintenanceStageStatus.APPLYING.value):
            raise StateError("maintenance attempt finished_at is invalid")
        if attempt["finished_at"] is not None:
            _timestamp(attempt["finished_at"])
        if (attempt["error_kind"] is not None) != (
            status in {MaintenanceStageStatus.BLOCKED.value, MaintenanceStageStatus.FAILED.value}
        ):
            raise StateError("maintenance attempt error_kind is invalid")
        if attempt["error_kind"] is not None:
            _text(attempt["error_kind"], "attempt error kind")
        _maintenance_attempt_evidence(document["operation_id"], index, attempt["evidence"])
        parsed.append(attempt)
    return parsed


def _maintenance_attempt_evidence(operation_id: JsonValue, attempt: int, value: JsonValue) -> None:
    if not isinstance(value, list):
        raise StateError("maintenance attempt evidence is invalid")
    for item in value:
        evidence = _object(item, {"evidence_id", "kind", "code", "value", "source"}, "evidence")
        kind = _one_of(evidence["kind"], {"digest", "check", "state"}, "evidence kind")
        code = _text(evidence["code"], "evidence code")
        observed = _text(evidence["value"], "evidence value")
        source = _text(evidence["source"], "evidence source")
        if _secret_like(code) or _secret_like(observed) or _secret_like(source):
            raise StateError("maintenance evidence may not contain secret-like content")
        expected = "evd_" + canonical_sha256(
            [operation_id, attempt, kind, code, observed, source]
        ).removeprefix("sha256:")[:32]
        if evidence["evidence_id"] != expected or _EVIDENCE_ID.fullmatch(expected) is None:
            raise StateError("maintenance evidence id is invalid")


def _secret_like(value: str) -> bool:
    lowered = value.lower()
    return any(marker in lowered for marker in ("password", "secret", "token", "keychain", "://"))


def _public_evidence(attempts: list[JsonValue]) -> list[JsonValue]:
    projection: list[JsonValue] = []
    ids: set[str] = set()
    for item in attempts:
        attempt = cast(dict[str, JsonValue], item)
        for evidence in cast(list[JsonValue], attempt["evidence"]):
            evidence_id = cast(dict[str, JsonValue], evidence)["evidence_id"]
            if not isinstance(evidence_id, str) or evidence_id in ids:
                continue
            ids.add(evidence_id)
            projection.append(evidence)
    return projection


def _preservation_inventory(value: JsonValue) -> None:
    item = _object(
        value,
        {
            "non_touch_surfaces",
            "target_byte_writes",
            "manager_state_writes",
            "manager_write_paths",
            "secret_value_reads",
            "secret_value_writes",
            "database_reads",
            "database_writes",
            "target_process_executions",
            "permission_prompts",
            "invoked_vectors",
            "opened_resources",
        },
        "preservation inventory",
    )
    _zero_preservation_counters(item)
    _preservation_lists(item)
    if not isinstance(item["invoked_vectors"], list) or item["invoked_vectors"]:
        raise StateError("maintenance import invoked vectors are invalid")


def _zero_preservation_counters(item: dict[str, JsonValue]) -> None:
    for key in (
        "target_byte_writes",
        "secret_value_reads",
        "secret_value_writes",
        "database_reads",
        "database_writes",
        "target_process_executions",
        "permission_prompts",
    ):
        if _nonnegative_integer(item[key], key) != 0:
            raise StateError("maintenance preservation counter is not zero")
    _nonnegative_integer(item["manager_state_writes"], "manager state writes")


def _preservation_lists(item: dict[str, JsonValue]) -> None:
    for key in ("non_touch_surfaces", "manager_write_paths", "opened_resources"):
        values = item[key]
        if not isinstance(values, list) or any(not isinstance(entry, str) or not entry for entry in values):
            raise StateError("maintenance preservation list is invalid")
        strings = cast(list[str], values)
        if strings != sorted(set(strings)):
            raise StateError("maintenance preservation list must be sorted and unique")


def _result(status: str, instance_id: JsonValue, value: JsonValue) -> None:
    if status not in _TERMINAL_MAINTENANCE_STATUSES:
        if value is not None:
            raise StateError("nonterminal maintenance operation has a result")
        return
    if status == MaintenanceOperationStatus.VERIFIED.value:
        item = _object(value, {"kind", "inventory_instance_id"}, "result")
        if item["kind"] != "imported" or item["inventory_instance_id"] != instance_id:
            raise StateError("maintenance verified result is invalid")
        return
    item = _object(value, {"kind", "reason_code", "repair"}, "result")
    if item["kind"] != status:
        raise StateError("maintenance terminal result kind is invalid")
    _text(item["reason_code"], "result reason code")
    _text(item["repair"], "result repair")


def _status_stage_invariants(status: str, stages: dict[str, JsonValue]) -> None:
    required = {
        MaintenanceOperationStatus.PREPARED.value: ("inspection_revalidated",),
        MaintenanceOperationStatus.BUNDLE_CACHED.value: (
            "inspection_revalidated",
            "inspection_bundle_cached",
        ),
        MaintenanceOperationStatus.INVENTORY_PUBLISHED.value: (
            "inspection_revalidated",
            "inspection_bundle_cached",
            "inventory_published",
        ),
        MaintenanceOperationStatus.VERIFIED.value: _IMPORT_STAGE_IDS,
    }.get(status, ())
    if any(stages[stage_id] != MaintenanceStageStatus.VERIFIED.value for stage_id in required):
        raise StateError("maintenance operation status is ahead of its stages")


def _stage_key(document: dict[str, JsonValue], stage_id: str) -> str:
    inputs: dict[str, JsonValue] = {
        "input": document["input"],
        "current_identity": document["current_identity"],
        "contract_digests": document["contract_digests"],
        "approval": document["approval"],
    }
    return canonical_sha256(
        ["stage", document["operation_id"], stage_id, canonical_sha256(inputs)]
    )


def maintenance_evidence(
    operation_id: str, attempt: int, kind: str, code: str, value: str, source: str = "manager:existing_install_inspection"
) -> dict[str, JsonValue]:
    return {
        "evidence_id": "evd_" + canonical_sha256(
            [operation_id, attempt, kind, code, value, source]
        ).removeprefix("sha256:")[:32],
        "kind": kind,
        "code": code,
        "value": value,
        "source": source,
    }

def _copy_json_object(value: dict[str, JsonValue]) -> dict[str, JsonValue]:
    copied = json.loads(json.dumps(value, sort_keys=True))
    if not isinstance(copied, dict):
        raise AssertionError("JSON object copy unexpectedly changed shape")
    return cast(dict[str, JsonValue], copied)


def utc_now() -> str:
    return datetime.now(tz=UTC).isoformat().replace("+00:00", "Z")
