"""Identity-preserving compare-and-swap helpers for the active update pointer and source axis."""

from __future__ import annotations

import sys
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory

_ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(_ROOT / "solet_cli" / "src"), str(_ROOT / "solet_setup_contracts" / "src")]
from solet_manager.errors import StateConflictError  # noqa: E402
from solet_manager.maintenance_inventory import (  # noqa: E402
    publish_active_update,
    publish_source_advance,
    read_maintenance_inventory_v2,
    write_maintenance_inventory_v2,
)
from solet_manager.models import (  # noqa: E402
    ActiveOperation,
    ChannelIdentity,
    ContractIdentities,
    FilesystemIdentity,
    InstanceInventoryRecordV2,
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

_NOW = "2026-09-18T00:00:00Z"
_LATER = "2026-09-18T00:00:01Z"
_REPOSITORY = "https://github.com/example/seed.git"
_OPERATION = "opr_" + "9" * 32
_ADVANCED_RELEASE = ReleaseIdentity(_REPOSITORY, "b" * 40, "c" * 40, "r2")
_ADVANCED_CHANNEL = ChannelIdentity("stable", "sha256:" + "d" * 64, _REPOSITORY)
_ADVANCED_PROVENANCE = ObservedProvenanceIdentity("strict", "sha256:" + "e" * 64, "seed2", "origin", "sha256:" + "f" * 64, None)


def _record() -> InstanceInventoryRecordV2:
    return InstanceInventoryRecordV2(
        "ins_" + "a" * 32,
        "fixture",
        TargetIdentity("/tmp/fixture", FilesystemIdentity(1, 2), FilesystemIdentity(1, 3)),
        ManagementOrigin.IMPORT,
        ManagementState.DIAGNOSTIC,
        UpdateEligibility(UpdateEligibilityState.AVAILABLE, ()),
        ServiceIdentity("/tmp/fixture/.venv/bin/solet", "/tmp/fixture/.venv/bin/solet-bridge", "/tmp/bin/fixture", None, None, "/tmp/fixture/profile", "local.solet.fixture", None, None),
        ChannelIdentity("stable", "sha256:" + "1" * 64, _REPOSITORY),
        ObservedProvenanceIdentity("strict", "sha256:" + "2" * 64, "seed", "origin", "sha256:" + "3" * 64, None),
        ReleaseIdentity(_REPOSITORY, "4" * 40, "5" * 40, "r1"),
        ReleaseIdentity(_REPOSITORY, "4" * 40, "5" * 40, "r1"),
        None,
        ContractIdentities("sha256:" + "6" * 64, None, None, "sha256:" + "6" * 64, None),
        "sha256:" + "7" * 64,
        None,
        "opr_" + "8" * 32,
        _NOW,
        _NOW,
        _NOW,
        _NOW,
    )


def _expect_conflict(action: Callable[[], object], label: str) -> None:
    try:
        action()
    except StateConflictError:
        return
    raise AssertionError(label)


def _advance(path: Path, expected: InstanceInventoryRecordV2, now: str) -> InstanceInventoryRecordV2:
    return publish_source_advance(
        path,
        expected,
        operation_id=_OPERATION,
        source_release=_ADVANCED_RELEASE,
        source_contract_digest="sha256:" + "a" * 64,
        channel=_ADVANCED_CHANNEL,
        observed_provenance=_ADVANCED_PROVENANCE,
        now=now,
    )


def _assert_pointer(path: Path, record: InstanceInventoryRecordV2) -> InstanceInventoryRecordV2:
    _expect_conflict(lambda: publish_active_update(path, replace(record, name="other"), _OPERATION, _LATER), "drifted expected accepted")
    active = publish_active_update(path, record, _OPERATION, _LATER)
    assert active.active_operation == ActiveOperation(MaintenanceOperationKind.UPDATE, _OPERATION)
    assert active.source_release == record.source_release
    assert active.runtime_release == record.runtime_release
    assert publish_active_update(path, active, _OPERATION, "2026-09-18T00:00:02Z") == active
    _expect_conflict(lambda: publish_active_update(path, active, "opr_" + "0" * 32, _LATER), "second pointer accepted")
    _expect_conflict(lambda: _advance(path, record, _LATER), "stale expected accepted for source advance")
    return active


def _assert_advance(path: Path, record: InstanceInventoryRecordV2, active: InstanceInventoryRecordV2) -> None:
    advanced = _advance(path, active, "2026-09-18T00:00:03Z")
    assert advanced.source_release == _ADVANCED_RELEASE
    assert advanced.runtime_release == record.runtime_release
    assert advanced.verified_release is None
    assert advanced.contract_identities == ContractIdentities("sha256:" + "6" * 64, None, "sha256:" + "a" * 64, "sha256:" + "6" * 64, None)
    assert advanced.update_eligibility == UpdateEligibility(UpdateEligibilityState.BLOCKED, ("update_in_progress",))
    assert advanced.management_state is ManagementState.DIAGNOSTIC
    assert advanced.active_operation == ActiveOperation(MaintenanceOperationKind.UPDATE, _OPERATION)
    assert (advanced.last_verified_operation_id, advanced.last_verified_at) == (record.last_verified_operation_id, _NOW)
    _assert_advance_readback(path, advanced)


def _assert_advance_readback(path: Path, advanced: InstanceInventoryRecordV2) -> None:
    assert read_maintenance_inventory_v2(path) == (advanced,)
    assert _advance(path, advanced, "2026-09-18T00:00:04Z") == advanced
    assert read_maintenance_inventory_v2(path) == (advanced,)


def main() -> int:
    with TemporaryDirectory() as temporary:
        path = Path(temporary) / "maintenance-inventory.json"
        record = _record()
        write_maintenance_inventory_v2(path, (record,))
        active = _assert_pointer(path, record)
        _assert_advance(path, record, active)
    print("update_inventory_publication_smoke OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
