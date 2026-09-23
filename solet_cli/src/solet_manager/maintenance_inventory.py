"""Closed v2 maintenance inventory, kept separate from the create registry."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path
from typing import cast

from .errors import StateConflictError, StateError
from .models import (
    ActiveOperation,
    ChannelIdentity,
    ContractIdentities,
    FilesystemIdentity,
    InstanceInventoryRecordV2,
    JsonValue,
    MaintenanceOperationKind,
    ManagementOrigin,
    ManagementState,
    ObservedProvenanceIdentity,
    ReleaseIdentity,
    ServiceIdentity,
    TargetIdentity,
    UpdateEligibility,
    UpdateEligibilityState,
)
from .state_io import atomic_write_json, load_json_object

CREATE_REGISTRY_SCHEMA_VERSION = 1
MAINTENANCE_INVENTORY_SCHEMA_VERSION = 2
_V1_KEYS = frozenset({"schema_version", "instances"})
_DOC_KEYS = frozenset({"schema_version", "records"})
_RECORD_KEYS = frozenset(
    {
        "instance_id",
        "name",
        "target",
        "management_origin",
        "management_state",
        "update_eligibility",
        "service_identity",
        "channel",
        "observed_provenance",
        "source_release",
        "runtime_release",
        "verified_release",
        "contract_identities",
        "inspection_bundle_digest",
        "active_operation",
        "last_verified_operation_id",
        "created_at",
        "updated_at",
        "last_inspected_at",
        "last_verified_at",
    }
)
_HEX64 = re.compile(r"^sha256:[0-9a-f]{64}$")
_HEX40 = re.compile(r"^[0-9a-f]{40}$")
_ID = re.compile(r"^(?:ins|opr)_[0-9a-f]{32}$")


class LegacyCreateInventoryProjection:
    """Lossless legacy projection; retained for the Step-2 union reader."""

    def __init__(self, create_record: dict[str, JsonValue]) -> None:
        self.create_record = create_record
        self.management_origin = "create"
        self.management_state = "unknown"
        self.active_operation = self.update_eligibility = self.verified_release = None
        self.source_release = self.runtime_release = self.contract_identities = None


MaintenanceInventoryRecord = InstanceInventoryRecordV2


def parse_maintenance_inventory_bytes(
    raw_bytes: bytes,
) -> tuple[LegacyCreateInventoryProjection | InstanceInventoryRecordV2, ...]:
    raw = _json(raw_bytes)
    if raw.get("schema_version") == CREATE_REGISTRY_SCHEMA_VERSION:
        return _project_v1(raw)
    if raw.get("schema_version") == MAINTENANCE_INVENTORY_SCHEMA_VERSION:
        return parse_maintenance_inventory_v2_bytes(raw_bytes)
    raise StateError("maintenance inventory schema_version must be exactly 1 or 2")


def parse_maintenance_inventory_v2_bytes(raw_bytes: bytes) -> tuple[InstanceInventoryRecordV2, ...]:
    raw = _json(raw_bytes)
    records_value = raw.get("records")
    if (
        frozenset(raw) != _DOC_KEYS
        or raw.get("schema_version") != 2
        or not isinstance(records_value, list)
    ):
        raise StateError("maintenance inventory does not match the closed v2 shape")
    records = tuple(_parse_record(value) for value in records_value)
    ids = tuple(record.instance_id for record in records)
    if ids != tuple(sorted(ids)) or len(set(ids)) != len(ids):
        raise StateError("maintenance inventory records must be uniquely sorted by instance_id")
    return records


def serialize_maintenance_inventory_v2(
    records: tuple[InstanceInventoryRecordV2, ...],
) -> dict[str, JsonValue]:
    ids = tuple(record.instance_id for record in records)
    if ids != tuple(sorted(ids)) or len(set(ids)) != len(ids):
        raise StateError("maintenance inventory records must be uniquely sorted by instance_id")
    return {"schema_version": 2, "records": [_record_dict(record) for record in records]}


def read_maintenance_inventory(
    path: Path,
) -> tuple[LegacyCreateInventoryProjection | InstanceInventoryRecordV2, ...]:
    try:
        return parse_maintenance_inventory_bytes(path.read_bytes())
    except OSError as exc:
        raise StateError(f"maintenance inventory is unreadable: {exc}") from exc


def read_maintenance_inventory_v2(path: Path) -> tuple[InstanceInventoryRecordV2, ...]:
    raw = load_json_object(path, missing_ok=True)
    return (
        ()
        if raw is None
        else parse_maintenance_inventory_v2_bytes(json.dumps(raw, sort_keys=True).encode())
    )


def write_maintenance_inventory_v2(
    path: Path, records: tuple[InstanceInventoryRecordV2, ...]
) -> None:
    atomic_write_json(path, serialize_maintenance_inventory_v2(records))


def publish_active_update(
    path: Path, expected: InstanceInventoryRecordV2, operation_id: str, now: str
) -> InstanceInventoryRecordV2:
    """Compare-and-swap the active update pointer without touching release axes.

    The caller holds ``registry.lock``.  The row must still equal ``expected``;
    the same pointer already published is returned unchanged.
    """
    records = read_maintenance_inventory_v2(path)
    current = _require_same_record(records, expected)
    wanted = ActiveOperation(MaintenanceOperationKind.UPDATE, operation_id)
    if current.active_operation == wanted:
        return current
    if current.active_operation is not None:
        raise StateConflictError("another maintenance operation is active for this instance")
    return _swap_record(path, records, replace(current, active_operation=wanted, updated_at=now))


def publish_source_advance(
    path: Path,
    expected: InstanceInventoryRecordV2,
    *,
    operation_id: str,
    source_release: ReleaseIdentity,
    source_contract_digest: str,
    channel: ChannelIdentity,
    observed_provenance: ObservedProvenanceIdentity,
    now: str,
) -> InstanceInventoryRecordV2:
    """Publish the exact N+1 source axis while preserving runtime/verified axes.

    The caller holds ``registry.lock``.  The active pointer stays on the same
    update; ``last_verified_*`` never move here (Step 6 owns them).  A row that
    already carries this exact source axis is returned unchanged.
    """
    records = read_maintenance_inventory_v2(path)
    current = _require_same_record(records, expected)
    if current.active_operation != ActiveOperation(MaintenanceOperationKind.UPDATE, operation_id):
        raise StateConflictError("source advance requires the same active update pointer")
    contracts = replace(current.contract_identities, source_contract_digest=source_contract_digest)
    published = replace(
        current,
        source_release=source_release,
        contract_identities=contracts,
        channel=channel,
        observed_provenance=observed_provenance,
        update_eligibility=UpdateEligibility(UpdateEligibilityState.BLOCKED, ("update_in_progress",)),
        updated_at=now,
        last_inspected_at=now,
    )
    if replace(current, updated_at=now, last_inspected_at=now) == published:
        return current
    return _swap_record(path, records, published)


def publish_runtime_advance(
    path: Path,
    expected: InstanceInventoryRecordV2,
    *,
    operation_id: str,
    runtime_release: ReleaseIdentity,
    runtime_contract_digest: str,
    now: str,
) -> InstanceInventoryRecordV2:
    """Publish the exact N+1 runtime axis and nothing else (design section 7.5).

    The caller holds ``registry.lock`` and has already verified the running
    process's attestation.  Only ``runtime_release`` and
    ``runtime_contract_digest`` move; the active pointer, ``verified_*``,
    ``management_state``, ``last_verified_*`` and the blocked eligibility are
    untouched.  The runtime axis may only advance to the row's own source axis:
    a runtime release that is not the published source release is a caller
    error, never a silent divergence.  A row already carrying this exact
    runtime axis is returned unchanged (idempotent compare-and-swap).
    """
    records = read_maintenance_inventory_v2(path)
    current = _require_same_record(records, expected)
    if current.active_operation != ActiveOperation(MaintenanceOperationKind.UPDATE, operation_id):
        raise StateConflictError("runtime advance requires the same active update pointer")
    if current.source_release != runtime_release or current.contract_identities.source_contract_digest != runtime_contract_digest:
        raise StateConflictError("runtime advance must publish exactly the row's source axis")
    if current.update_eligibility != UpdateEligibility(UpdateEligibilityState.BLOCKED, ("update_in_progress",)):
        raise StateConflictError("runtime advance requires the update-in-progress eligibility")
    contracts = replace(current.contract_identities, runtime_contract_digest=runtime_contract_digest)
    published = replace(current, runtime_release=runtime_release, contract_identities=contracts, updated_at=now)
    if replace(current, updated_at=now) == published:
        return current
    return _swap_record(path, records, published)


@dataclass(frozen=True, slots=True)
class TerminalProof:
    """What a caller must have read before it may release the active pointer (Step 6 section 5.3 step 4).

    ``status`` is the terminal journal status at the pointer, ``result_digest``
    the canonical digest of its result object, and ``retired`` whether a
    retirement record is present (required for ``blocked``/``failed``).  A
    ``verified`` import journal releases with ``status="verified"``.
    """

    operation_id: str
    kind: MaintenanceOperationKind
    status: str
    result_digest: str | None
    retired: bool

    @property
    def releasable(self) -> bool:
        if self.kind is MaintenanceOperationKind.IMPORT:
            return self.status == "verified"
        if self.status in {"promoted", "abandoned"}:
            return True
        return self.status in {"blocked", "failed"} and self.retired


def publish_promotion(
    path: Path,
    expected: InstanceInventoryRecordV2,
    *,
    operation_id: str,
    verified_release: ReleaseIdentity,
    verified_contract_digest: str,
    eligibility: UpdateEligibility,
    doctor_evidence_digest: str,
    now: str,
) -> InstanceInventoryRecordV2:
    """Make the candidate the active verified release contract (Step 6 section 5.3 step 2).

    The caller holds ``registry.lock`` and has a ``doctor_verified`` journal
    whose evidence digest it cites.  The pointer stays; ``verified_release``,
    ``verified_``/``current_contract_digest``, ``management_state``,
    ``update_eligibility`` and ``last_verified_*`` move together.  A row
    already carrying exactly these values is returned unchanged.
    """
    if not doctor_evidence_digest.startswith("sha256:"):
        raise StateConflictError("promotion requires the final doctor's evidence digest")
    records = read_maintenance_inventory_v2(path)
    current = _require_same_record(records, expected)
    if current.active_operation != ActiveOperation(MaintenanceOperationKind.UPDATE, operation_id):
        raise StateConflictError("promotion requires the same active update pointer")
    if current.source_release != verified_release or current.runtime_release != verified_release:
        raise StateConflictError("promotion requires source and runtime axes at the candidate")
    identities = current.contract_identities
    if identities.source_contract_digest != verified_contract_digest or identities.runtime_contract_digest != verified_contract_digest:
        raise StateConflictError("promotion requires source and runtime contract digests at the candidate bundle")
    contracts = replace(identities, verified_contract_digest=verified_contract_digest, current_contract_digest=verified_contract_digest)
    published = replace(
        current,
        verified_release=verified_release,
        contract_identities=contracts,
        management_state=ManagementState.VERIFIED,
        update_eligibility=eligibility,
        last_verified_operation_id=operation_id,
        last_verified_at=now,
        updated_at=now,
    )
    if replace(current, last_verified_at=now, updated_at=now) == published:
        return current
    return _swap_record(path, records, published)


def publish_needs_attention(
    path: Path,
    expected: InstanceInventoryRecordV2,
    *,
    reason_codes: tuple[str, ...],
    now: str,
) -> InstanceInventoryRecordV2:
    """Set ``management_state=needs_attention`` and fold the reason codes into a blocked eligibility (Step 6 section 5.4).

    The pointer and every release axis are untouched.  With no active pointer
    (doctor write W2 under contract 2) the eligibility becomes
    ``blocked(["needs_attention", ...])`` so the inventory stays truthful;
    with a pointer the ``update_in_progress`` code is kept.  Idempotent.
    """
    if not reason_codes:
        raise StateConflictError("needs_attention requires at least one reason code")
    records = read_maintenance_inventory_v2(path)
    current = _require_same_record(records, expected)
    existing = current.update_eligibility.reason_codes if current.update_eligibility.state is UpdateEligibilityState.BLOCKED else ()
    base = ("needs_attention",) if current.active_operation is None else ()
    codes = tuple(sorted({*existing, *base, *reason_codes}))
    published = replace(
        current,
        management_state=ManagementState.NEEDS_ATTENTION,
        update_eligibility=UpdateEligibility(UpdateEligibilityState.BLOCKED, codes),
        updated_at=now,
    )
    if replace(current, updated_at=now) == published:
        return current
    return _swap_record(path, records, published)


def release_active_operation(
    path: Path,
    expected: InstanceInventoryRecordV2,
    *,
    proof: TerminalProof,
    now: str,
) -> InstanceInventoryRecordV2:
    """Clear the active pointer only against a terminal proof the caller read (Step 6 section 5.3 step 4)."""
    records = read_maintenance_inventory_v2(path)
    current = _require_same_record(records, expected)
    if current.active_operation is None:
        return current
    if current.active_operation != ActiveOperation(proof.kind, proof.operation_id):
        raise StateConflictError("pointer release proof names a different operation than the active pointer")
    if not proof.releasable:
        raise StateConflictError("pointer release requires a promoted, abandoned, retired, or verified-import journal as proof")
    eligibility = current.update_eligibility
    if proof.kind is MaintenanceOperationKind.UPDATE and proof.status != "promoted":
        eligibility = _without_in_progress(eligibility)
    return _swap_record(path, records, replace(current, active_operation=None, update_eligibility=eligibility, updated_at=now))


def _without_in_progress(eligibility: UpdateEligibility) -> UpdateEligibility:
    """An abandoned or retired update no longer blocks eligibility on ``update_in_progress``."""
    if eligibility.state is not UpdateEligibilityState.BLOCKED or "update_in_progress" not in eligibility.reason_codes:
        return eligibility
    codes = tuple(code for code in eligibility.reason_codes if code != "update_in_progress")
    return UpdateEligibility(UpdateEligibilityState.BLOCKED, codes) if codes else UpdateEligibility(UpdateEligibilityState.AVAILABLE, ())


def replace_active_update(
    path: Path,
    expected: InstanceInventoryRecordV2,
    *,
    old_operation_id: str,
    old_status: str,
    new_operation_id: str,
    now: str,
) -> InstanceInventoryRecordV2:
    """Swap the pointer old->new only when the old journal is terminal and the successor is prepared (Step 6 section 4.4)."""
    if old_status not in {"blocked", "failed"}:
        raise StateConflictError("a successor may only replace a terminal blocked or failed update")
    records = read_maintenance_inventory_v2(path)
    current = _require_same_record(records, expected)
    wanted = ActiveOperation(MaintenanceOperationKind.UPDATE, new_operation_id)
    if current.active_operation == wanted:
        return current
    if current.active_operation != ActiveOperation(MaintenanceOperationKind.UPDATE, old_operation_id):
        raise StateConflictError("the active pointer does not name the operation the successor recovers")
    return _swap_record(path, records, replace(current, active_operation=wanted, updated_at=now))


def _require_same_record(
    records: tuple[InstanceInventoryRecordV2, ...], expected: InstanceInventoryRecordV2
) -> InstanceInventoryRecordV2:
    current = next((item for item in records if item.instance_id == expected.instance_id), None)
    if current is None or current != expected:
        raise StateConflictError("maintenance inventory record changed under the caller")
    return current


def _swap_record(
    path: Path,
    records: tuple[InstanceInventoryRecordV2, ...],
    published: InstanceInventoryRecordV2,
) -> InstanceInventoryRecordV2:
    write_maintenance_inventory_v2(
        path,
        tuple(published if item.instance_id == published.instance_id else item for item in records),
    )
    readback = next(
        (item for item in read_maintenance_inventory_v2(path) if item.instance_id == published.instance_id),
        None,
    )
    if readback != published:
        raise StateError("maintenance inventory read-back mismatch")
    return published


def read_combined_inventory(
    paths: object,
) -> tuple[LegacyCreateInventoryProjection | InstanceInventoryRecordV2, ...]:
    registry_path = cast(Path, paths.registry_path)
    v1 = read_maintenance_inventory(registry_path) if registry_path.exists() else ()
    return (*v1, *read_maintenance_inventory_v2(cast(Path, paths.maintenance_inventory_path)))


def _json(raw_bytes: bytes) -> dict[str, object]:
    try:
        raw = json.loads(raw_bytes.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise StateError(f"maintenance inventory is unreadable: {exc}") from exc
    if not isinstance(raw, dict):
        raise StateError("maintenance inventory must be an object")
    return cast(dict[str, object], raw)


def _project_v1(raw: dict[str, object]) -> tuple[LegacyCreateInventoryProjection, ...]:
    if frozenset(raw) != _V1_KEYS or not isinstance(raw["instances"], dict):
        raise StateError("create registry does not match the closed v1 shape")
    result: list[LegacyCreateInventoryProjection] = []
    for name, value in sorted(raw["instances"].items()):
        if not isinstance(name, str) or not isinstance(value, dict) or value.get("name") != name:
            raise StateError("create registry has an invalid v1 instance")
        result.append(LegacyCreateInventoryProjection(cast(dict[str, JsonValue], value.copy())))
    return tuple(result)


def _parse_record(value: object) -> InstanceInventoryRecordV2:
    if not isinstance(value, dict) or frozenset(value) != _RECORD_KEYS:
        raise StateError("maintenance inventory record does not match the closed v2 shape")
    d = cast(dict[str, object], value)
    return InstanceInventoryRecordV2(
        _id(d["instance_id"], "ins_"),
        _text(d["name"], "name"),
        _target(d["target"]),
        cast(ManagementOrigin, _enum(ManagementOrigin, d["management_origin"], "management_origin")),
        cast(ManagementState, _enum(ManagementState, d["management_state"], "management_state")),
        _eligibility(d["update_eligibility"]),
        _service(d["service_identity"]),
        _channel(d["channel"]),
        _provenance(d["observed_provenance"]),
        _release(d["source_release"]),
        _nullable_release(d["runtime_release"]),
        _nullable_release(d["verified_release"]),
        _contracts(d["contract_identities"]),
        _digest(d["inspection_bundle_digest"]),
        _active(d["active_operation"]),
        _nullable_id(d["last_verified_operation_id"], "opr_"),
        _timestamp(d["created_at"]),
        _timestamp(d["updated_at"]),
        _timestamp(d["last_inspected_at"]),
        _nullable_timestamp(d["last_verified_at"]),
    )


def _record_dict(r: InstanceInventoryRecordV2) -> dict[str, JsonValue]:
    return {
        "instance_id": r.instance_id,
        "name": r.name,
        "target": {
            "canonical_path": r.target.canonical_path,
            "filesystem_identity": r.target.filesystem_identity.to_dict(),
            "parent_filesystem_identity": r.target.parent_filesystem_identity.to_dict(),
        },
        "management_origin": r.management_origin.value,
        "management_state": r.management_state.value,
        "update_eligibility": {
            "state": r.update_eligibility.state.value,
            "reason_codes": list(r.update_eligibility.reason_codes),
        },
        "service_identity": {
            "service_cli_path": r.service_identity.service_cli_path,
            "bridge_cli_path": r.service_identity.bridge_cli_path,
            "named_launcher_path": r.service_identity.named_launcher_path,
            "named_launcher_target": r.service_identity.named_launcher_target,
            "profile_id": r.service_identity.profile_id,
            "app_home": r.service_identity.app_home,
            "launchagent_label": r.service_identity.launchagent_label,
            "router_label": r.service_identity.router_label,
            "router_socket": r.service_identity.router_socket,
        },
        "channel": {
            "channel_id": r.channel.channel_id,
            "descriptor_digest": r.channel.descriptor_digest,
            "canonical_repository": r.channel.canonical_repository,
        },
        "observed_provenance": {
            "condition": r.observed_provenance.condition,
            "provenance_sha256": r.observed_provenance.provenance_sha256,
            "seed_id": r.observed_provenance.seed_id,
            "origin_id": r.observed_provenance.origin_id,
            "manifest_sha256": r.observed_provenance.manifest_sha256,
            "anchor_id": r.observed_provenance.anchor_id,
        },
        "source_release": _release_dict(r.source_release),
        "runtime_release": _nullable_release_dict(r.runtime_release),
        "verified_release": _nullable_release_dict(r.verified_release),
        "contract_identities": {
            "diagnostic_contract_digest": r.contract_identities.diagnostic_contract_digest,
            "current_contract_digest": r.contract_identities.current_contract_digest,
            "source_contract_digest": r.contract_identities.source_contract_digest,
            "runtime_contract_digest": r.contract_identities.runtime_contract_digest,
            "verified_contract_digest": r.contract_identities.verified_contract_digest,
        },
        "inspection_bundle_digest": r.inspection_bundle_digest,
        "active_operation": None
        if r.active_operation is None
        else {
            "kind": r.active_operation.kind.value,
            "operation_id": r.active_operation.operation_id,
        },
        "last_verified_operation_id": r.last_verified_operation_id,
        "created_at": r.created_at,
        "updated_at": r.updated_at,
        "last_inspected_at": r.last_inspected_at,
        "last_verified_at": r.last_verified_at,
    }


def _mapping(v: object, keys: set[str]) -> dict[str, object]:
    if not isinstance(v, dict) or set(v) != keys:
        raise StateError("maintenance inventory nested record has invalid keys")
    return cast(dict[str, object], v)


def _text(v: object, name: str) -> str:
    if not isinstance(v, str) or not v:
        raise StateError(f"maintenance inventory {name} must be a non-empty string")
    return v


def _nullable_text(v: object, name: str) -> str | None:
    return None if v is None else _text(v, name)


def _digest(v: object) -> str:
    v = _text(v, "digest")
    if not _HEX64.fullmatch(v):
        raise StateError("maintenance inventory digest is invalid")
    return v


def _nullable_digest(v: object) -> str | None:
    return None if v is None else _digest(v)


def _id(v: object, prefix: str) -> str:
    v = _text(v, "id")
    if not v.startswith(prefix) or not _ID.fullmatch(v):
        raise StateError("maintenance inventory id is invalid")
    return v


def _nullable_id(v: object, prefix: str) -> str | None:
    return None if v is None else _id(v, prefix)


def _integer(v: object) -> int:
    if isinstance(v, bool) or not isinstance(v, int) or v < 0:
        raise StateError("filesystem identity must be a non-negative integer")
    return v


def _identity(v: object) -> FilesystemIdentity:
    d = _mapping(v, {"device", "inode"})
    return FilesystemIdentity(_integer(d["device"]), _integer(d["inode"]))


def _target(v: object) -> TargetIdentity:
    d = _mapping(v, {"canonical_path", "filesystem_identity", "parent_filesystem_identity"})
    return TargetIdentity(
        _text(d["canonical_path"], "canonical_path"),
        _identity(d["filesystem_identity"]),
        _identity(d["parent_filesystem_identity"]),
    )


def _enum(
    kind: type[ManagementOrigin]
    | type[ManagementState]
    | type[UpdateEligibilityState]
    | type[MaintenanceOperationKind],
    v: object,
    name: str,
):
    try:
        return kind(_text(v, name))
    except ValueError as exc:
        raise StateError(f"maintenance inventory {name} is invalid") from exc


def _eligibility(v: object) -> UpdateEligibility:
    d = _mapping(v, {"state", "reason_codes"})
    if not isinstance(d["reason_codes"], list) or any(
        not isinstance(x, str) or not x for x in d["reason_codes"]
    ):
        raise StateError("maintenance inventory reason codes are invalid")
    codes = tuple(cast(list[str], d["reason_codes"]))
    if codes != tuple(sorted(codes)):
        raise StateError("maintenance inventory reason codes must be sorted")
    return UpdateEligibility(
        cast(UpdateEligibilityState, _enum(UpdateEligibilityState, d["state"], "eligibility state")),
        codes,
    )


def _service(v: object) -> ServiceIdentity:
    d = _mapping(
        v,
        {
            "service_cli_path",
            "bridge_cli_path",
            "named_launcher_path",
            "named_launcher_target",
            "profile_id",
            "app_home",
            "launchagent_label",
            "router_label",
            "router_socket",
        },
    )
    return ServiceIdentity(
        _text(d["service_cli_path"], "service_cli_path"),
        _text(d["bridge_cli_path"], "bridge_cli_path"),
        _text(d["named_launcher_path"], "named_launcher_path"),
        _nullable_text(d["named_launcher_target"], "named_launcher_target"),
        _nullable_text(d["profile_id"], "profile_id"),
        _nullable_text(d["app_home"], "app_home"),
        _text(d["launchagent_label"], "launchagent_label"),
        _nullable_text(d["router_label"], "router_label"),
        _nullable_text(d["router_socket"], "router_socket"),
    )


def _channel(v: object) -> ChannelIdentity:
    d = _mapping(v, {"channel_id", "descriptor_digest", "canonical_repository"})
    return ChannelIdentity(
        _text(d["channel_id"], "channel_id"),
        _digest(d["descriptor_digest"]),
        _text(d["canonical_repository"], "canonical_repository"),
    )


def _provenance(v: object) -> ObservedProvenanceIdentity:
    d = _mapping(
        v,
        {"condition", "provenance_sha256", "seed_id", "origin_id", "manifest_sha256", "anchor_id"},
    )
    condition = _text(d["condition"], "condition")
    if condition not in {"strict", "missing"}:
        raise StateError("maintenance inventory provenance condition is invalid")
    return ObservedProvenanceIdentity(
        condition,
        _nullable_digest(d["provenance_sha256"]),
        _text(d["seed_id"], "seed_id"),
        _text(d["origin_id"], "origin_id"),
        _digest(d["manifest_sha256"]),
        _nullable_text(d["anchor_id"], "anchor_id"),
    )


def _release(v: object) -> ReleaseIdentity:
    d = _mapping(v, {"repository", "commit", "tree", "tag"})
    commit = _text(d["commit"], "commit")
    tree = _text(d["tree"], "tree")
    if not _HEX40.fullmatch(commit) or not _HEX40.fullmatch(tree):
        raise StateError("maintenance inventory release identity is invalid")
    return ReleaseIdentity(
        _text(d["repository"], "repository"), commit, tree, _nullable_text(d["tag"], "tag")
    )


def _nullable_release(v: object) -> ReleaseIdentity | None:
    return None if v is None else _release(v)


def _release_dict(v: ReleaseIdentity) -> dict[str, JsonValue]:
    return {"repository": v.repository, "commit": v.commit, "tree": v.tree, "tag": v.tag}


def _nullable_release_dict(v: ReleaseIdentity | None) -> dict[str, JsonValue] | None:
    return None if v is None else _release_dict(v)


def _contracts(v: object) -> ContractIdentities:
    d = _mapping(
        v,
        {
            "diagnostic_contract_digest",
            "current_contract_digest",
            "source_contract_digest",
            "runtime_contract_digest",
            "verified_contract_digest",
        },
    )
    return ContractIdentities(
        _digest(d["diagnostic_contract_digest"]),
        *(
            _nullable_digest(d[k])
            for k in (
                "current_contract_digest",
                "source_contract_digest",
                "runtime_contract_digest",
                "verified_contract_digest",
            )
        ),
    )


def _active(v: object) -> ActiveOperation | None:
    if v is None:
        return None
    d = _mapping(v, {"kind", "operation_id"})
    return ActiveOperation(
        cast(MaintenanceOperationKind, _enum(MaintenanceOperationKind, d["kind"], "operation kind")),
        _id(d["operation_id"], "opr_"),
    )


def _timestamp(v: object) -> str:
    v = _text(v, "timestamp")
    try:
        parsed = datetime.fromisoformat(v.replace("Z", "+00:00"))
    except ValueError as exc:
        raise StateError("maintenance inventory timestamp is invalid") from exc
    if not v.endswith("Z") or parsed.tzinfo is None:
        raise StateError("maintenance inventory timestamp must be RFC3339 UTC")
    return v


def _nullable_timestamp(v: object) -> str | None:
    return None if v is None else _timestamp(v)
