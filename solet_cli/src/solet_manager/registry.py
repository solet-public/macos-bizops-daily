"""Manager-created instance registry and unmanaged-instance discovery."""

from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path

from .errors import InstanceUnmanagedError, RegistryUniquenessError, StateConflictError, StateError
from .maintenance_inventory import read_maintenance_inventory_v2
from .models import SCHEMA_VERSION, InstanceInventoryRecordV2, InstanceRecord, JsonValue, ManagementOrigin
from .state_io import atomic_write_json, instance_lock, load_json_object

_REGISTRY_KEYS = frozenset({"schema_version", "instances"})
_INSTANCE_KEYS = frozenset(
    {
        "name",
        "target",
        "launcher",
        "seed_repository",
        "seed_tag",
        "seed_commit",
        "seed_tree_hash",
        "profile",
        "flow_id",
        "flow_source_revision",
        "flow_contract_digest",
        "created_at",
        "updated_at",
        "lifecycle_state",
        "input_fingerprint",
        "expected_router_name",
        "expected_router_socket",
        "expected_router_port_range",
    }
)
_LEGACY_INSTANCE_KEYS = _INSTANCE_KEYS - {
    "lifecycle_state",
    "input_fingerprint",
    "expected_router_name",
    "expected_router_socket",
    "expected_router_port_range",
}
_FORMULA_PATH_MARKERS = ("/Cellar/solet/", "/homebrew/Cellar/solet/")


def require_unique_identity(
    indexes: dict[str, dict[object, str]],
    *,
    requested: dict[str, object],
) -> None:
    """Fail closed on any of the five Manager registry identity collisions."""
    for key_name in ("name", "target", "filesystem_identity", "launchagent_label", "named_launcher_path"):
        value = requested[key_name]
        incumbent = indexes.get(key_name, {}).get(value)
        if incumbent is not None:
            error = RegistryUniquenessError(
                f"registry uniqueness collision on {key_name}: {value!r} (held by {incumbent})"
            )
            error.key_name = key_name
            error.requested_value = value
            error.incumbent_identity = incumbent
            raise error


@dataclass(frozen=True)
class RegistryIdentityKeys:
    name: str
    target: str
    filesystem_identity: tuple[int, int] | None
    launchagent_label: str
    named_launcher_path: str


@dataclass(frozen=True)
class CombinedRegistrySnapshot:
    v1_records: tuple[InstanceRecord, ...]
    v2_records: tuple[InstanceInventoryRecordV2, ...]
    indexes: dict[str, dict[object, str]]


class MaintenanceInventoryRegistry:
    """Read-only v2 inventory facade used by cross-registry collision checks."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def list(self) -> tuple[InstanceInventoryRecordV2, ...]:
        return read_maintenance_inventory_v2(self.path)


def build_combined_registry_snapshot(
    v1_records: tuple[InstanceRecord, ...], v2_records: tuple[InstanceInventoryRecordV2, ...]
) -> CombinedRegistrySnapshot:
    indexes: dict[str, dict[object, str]] = {key: {} for key in ("name", "target", "filesystem_identity", "launchagent_label", "named_launcher_path")}
    for record in v1_records:
        _insert_identity(indexes, _v1_identity_keys(record), record.name)
    seen_v2: set[str] = set()
    for record in v2_records:
        keys = _v2_identity_keys(record)
        legacy = next((item for item in v1_records if item.name == record.name), None)
        if record.name not in seen_v2 and legacy is not None and is_create_origin_alias(legacy, record):
            # Release/provenance axes advance during updates; only immutable keys identify this alias.
            seen_v2.add(record.name)
            continue
        _insert_identity(indexes, keys, record.instance_id)
        seen_v2.add(record.name)
    return CombinedRegistrySnapshot(v1_records, v2_records, indexes)


def _v2_identity_keys(record: InstanceInventoryRecordV2) -> RegistryIdentityKeys:
    target = record.target
    return RegistryIdentityKeys(
        record.name, target.canonical_path,
        (target.filesystem_identity.device, target.filesystem_identity.inode),
        record.service_identity.launchagent_label, str(Path(record.service_identity.named_launcher_path).resolve(strict=False)),
    )


def is_create_origin_alias(legacy: InstanceRecord, record: InstanceInventoryRecordV2) -> bool:
    """Coalesce only a complete create-origin identity, never mutable release fields."""
    keys = _v1_identity_keys(legacy)
    return (
        record.management_origin is ManagementOrigin.CREATE
        and keys.filesystem_identity is not None
        and keys == _v2_identity_keys(record)
    )


def find_managed_instance(snapshot: CombinedRegistrySnapshot, name: str) -> object | None:
    identity = snapshot.indexes["name"].get(name)
    if identity is None:
        return None
    return next((record for record in (*snapshot.v1_records, *snapshot.v2_records) if record.name == name), None)


def _insert_identity(indexes: dict[str, dict[object, str]], keys: RegistryIdentityKeys, identity: str) -> None:
    for key, value in (("name", keys.name), ("target", keys.target), ("filesystem_identity", keys.filesystem_identity), ("launchagent_label", keys.launchagent_label), ("named_launcher_path", keys.named_launcher_path)):
        if value is not None:
            if value in indexes[key]:
                raise RegistryUniquenessError(f"duplicate persisted {key}: {value!r}")
            indexes[key][value] = identity


def _v1_identity_keys(record: InstanceRecord) -> RegistryIdentityKeys:
    """Project only comparison keys from v1 without altering its persisted bytes."""
    target = Path(record.target).resolve(strict=False)
    filesystem_identity: tuple[int, int] | None = None
    try:
        info = target.stat()
    except OSError:
        pass
    else:
        filesystem_identity = (info.st_dev, info.st_ino)
    return RegistryIdentityKeys(
        record.name,
        str(target),
        filesystem_identity,
        f"local.solet.{record.name}",
        _v1_named_launcher_identity(record),
    )


def _v1_named_launcher_identity(record: InstanceRecord) -> str:
    """Project the create-time client entry point onto its public named slot."""
    launcher = Path(record.launcher)
    if launcher == Path(record.target) / "client" / "bin" / record.name:
        launcher = Path.home() / ".local" / "bin" / record.name
    return str(launcher.resolve(strict=False))


class InstanceRegistry:
    """Closed registry persisted as one atomic private JSON object."""

    def __init__(
        self,
        path: Path,
        *,
        maintenance_inventory_path: Path | None = None,
        registry_lock_path: Path | None = None,
    ) -> None:
        self.path = path
        self.maintenance_inventory_path = maintenance_inventory_path
        self.registry_lock_path = registry_lock_path

    def list(self) -> tuple[InstanceRecord, ...]:
        return tuple(sorted(self._read().values(), key=lambda item: item.name))

    def get(self, name: str) -> InstanceRecord | None:
        return self._read().get(name)

    def require(self, name: str, *, candidate_target: Path | None = None) -> InstanceRecord:
        record = self.get(name)
        if record is not None:
            return record
        if candidate_target is not None and candidate_target.exists():
            raise InstanceUnmanagedError(
                f"{candidate_target} exists but {name!r} is not manager-created",
                repair=(f"Resume the same reviewed transaction with 'solet create {name}'. Do not edit the registry by hand or adopt an arbitrary target."),
            )
        raise StateConflictError(f"managed instance {name!r} does not exist")

    def add(self, record: InstanceRecord) -> None:
        if self.registry_lock_path is None:
            self._add(record)
            return
        with instance_lock(self.registry_lock_path, create=True):
            self._add(record)

    def _add(self, record: InstanceRecord) -> None:
        _reject_formula_paths(record)
        _validate_lifecycle_record(record)
        records = self._read()
        existing = records.get(record.name)
        if existing is not None and _is_provisional_upgrade(existing, record):
            records[record.name] = record
            self._write(records)
            return
        if existing is not None and existing != record:
            raise StateConflictError(f"registry already contains a different record for {record.name!r}")
        if existing == record:
            return
        if self.maintenance_inventory_path is not None:
            snapshot = build_combined_registry_snapshot(
                tuple(records.values()), MaintenanceInventoryRegistry(self.maintenance_inventory_path).list()
            )
            require_unique_identity(snapshot.indexes, requested={"name": record.name, "target": record.target, "filesystem_identity": None, "launchagent_label": f"local.solet.{record.name}", "named_launcher_path": record.launcher})
        records[record.name] = record
        self._write(records)

    def discard_orphan(self, expected: InstanceRecord) -> None:
        """Remove a record only when the caller proved its journal is absent."""

        records = self._read()
        if records.get(expected.name) != expected:
            raise StateConflictError(
                f"registry changed before orphan reconciliation for {expected.name!r}"
            )
        del records[expected.name]
        self._write(records)

    def reconcile_contract(
        self,
        *,
        expected: InstanceRecord,
        flow_contract_digest: str,
        updated_at: str,
    ) -> InstanceRecord:
        """Compare-and-swap only a declared managed contract identity."""

        records = self._read()
        current = records.get(expected.name)
        if current != expected:
            raise StateConflictError(f"registry changed before contract reconciliation for {expected.name!r}")
        updated = self.reconciled_contract_record(
            expected,
            flow_contract_digest=flow_contract_digest,
            updated_at=updated_at,
        )
        records[updated.name] = updated
        self._write(records)
        return updated

    def reconciled_contract_record(
        self,
        expected: InstanceRecord,
        *,
        flow_contract_digest: str,
        updated_at: str,
    ) -> InstanceRecord:
        """Build the only permitted registry record identity replacement."""

        updated = replace(
            expected,
            flow_contract_digest=flow_contract_digest,
            updated_at=updated_at,
        )
        _reject_formula_paths(updated)
        _validate_lifecycle_record(updated)
        return updated

    def reconciled_identity_record(
        self,
        expected: InstanceRecord,
        *,
        flow_source_revision: str,
    ) -> InstanceRecord:
        """Build the only permitted in-field flow revision replacement."""

        if self.require(expected.name) != expected:
            raise StateConflictError(f"registry changed before identity reconciliation for {expected.name!r}")
        updated = replace(expected, flow_source_revision=flow_source_revision)
        _reject_formula_paths(updated)
        _validate_lifecycle_record(updated)
        return updated

    def _read(self) -> dict[str, InstanceRecord]:
        raw = load_json_object(self.path, missing_ok=True)
        if raw is None:
            return {}
        if frozenset(raw) != _REGISTRY_KEYS or raw.get("schema_version") != SCHEMA_VERSION:
            raise StateError(f"registry at {self.path} does not match the closed v1 schema")
        values = raw.get("instances")
        if not isinstance(values, dict):
            raise StateError(f"registry instances must be an object at {self.path}")
        parsed: dict[str, InstanceRecord] = {}
        for name, value in values.items():
            if not isinstance(value, dict):
                raise StateError(f"registry entry {name!r} is invalid")
            if frozenset(value) not in {_INSTANCE_KEYS, _LEGACY_INSTANCE_KEYS}:
                raise StateError(f"registry entry {name!r} does not match the closed v1 schema")
            try:
                record = InstanceRecord.from_dict(value)
            except (KeyError, TypeError, ValueError) as exc:
                raise StateError(f"registry entry {name!r} is incomplete: {exc}") from exc
            if record.name != name:
                raise StateError(f"registry key/name mismatch for {name!r}")
            _reject_formula_paths(record)
            _validate_lifecycle_record(record)
            parsed[name] = record
        return parsed

    def _write(self, records: dict[str, InstanceRecord]) -> None:
        payload: dict[str, JsonValue] = {
            "schema_version": SCHEMA_VERSION,
            "instances": {name: record.to_dict() for name, record in sorted(records.items())},
        }
        atomic_write_json(self.path, payload)


def _reject_formula_paths(record: InstanceRecord) -> None:
    for field_name, value in (("target", record.target), ("launcher", record.launcher)):
        if any(marker in value for marker in _FORMULA_PATH_MARKERS):
            raise StateError(f"registry {field_name} may not persist a formula-keg path: {value}")


def _validate_lifecycle_record(record: InstanceRecord) -> None:
    if record.lifecycle_state not in {"setup_incomplete", "verified"}:
        raise StateError("registry lifecycle_state is invalid")
    if record.lifecycle_state != "setup_incomplete":
        return
    if not record.input_fingerprint:
        raise StateError("setup-incomplete registry record requires an input fingerprint")
    if None in {
        record.expected_router_name,
        record.expected_router_socket,
        record.expected_router_port_range,
    }:
        raise StateError("setup-incomplete registry record requires expected router identity")


def _is_provisional_upgrade(existing: InstanceRecord, record: InstanceRecord) -> bool:
    return all(
        (
            _is_verified_transition(existing, record),
            _same_flow_identity(existing, record),
            _same_seed_identity(existing, record),
        )
    )


def _is_verified_transition(existing: InstanceRecord, record: InstanceRecord) -> bool:
    return existing.lifecycle_state == "setup_incomplete" and record.lifecycle_state == "verified" and existing.input_fingerprint == record.input_fingerprint


def _same_flow_identity(existing: InstanceRecord, record: InstanceRecord) -> bool:
    return existing.target == record.target and existing.flow_id == record.flow_id and existing.flow_source_revision == record.flow_source_revision and existing.flow_contract_digest == record.flow_contract_digest and existing.launcher == record.launcher and existing.created_at == record.created_at


def _same_seed_identity(existing: InstanceRecord, record: InstanceRecord) -> bool:
    return existing.seed_repository == record.seed_repository and existing.seed_tag == record.seed_tag and existing.seed_commit == record.seed_commit and existing.seed_tree_hash == record.seed_tree_hash and existing.profile == record.profile
