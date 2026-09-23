"""A finalized import reruns without a second inventory publication."""

from __future__ import annotations

import sys
from pathlib import Path
from tempfile import TemporaryDirectory

_ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(_ROOT / "solet_cli" / "src"), str(_ROOT / "solet_setup_contracts" / "src")]

import solet_manager.import_enrollment as enrollment  # noqa: E402
from solet_manager.existing_install_inspection import (  # noqa: E402
    ChannelInspectionIdentity,
    ChannelRelation,
    ExistingInstallClass,
    ExistingInstallClassification,
    ExistingInstallContractIdentity,
    ExistingInstallFacts,
    ExistingInstallInspectionRequest,
    ExistingInstallInspectionResult,
    InspectionAnchorKind,
    InspectionCheck,
    InspectionPreservationFacts,
    InspectionStatus,
    ObservationAvailability,
    ObservedBoolean,
    ObservedPathPairs,
    ObservedPaths,
    ProvenanceCondition,
    RepositoryRelation,
    TargetFilesystemIdentity,
    WorkingTreeCondition,
)
from solet_manager.import_enrollment import (  # noqa: E402
    ImportEnrollmentResult,
    ImportRequest,
    preview_import,
)
from solet_manager.maintenance_inventory import read_maintenance_inventory_v2  # noqa: E402
from solet_manager.models import InstanceInventoryRecordV2  # noqa: E402
from solet_manager.paths import ManagerPaths  # noqa: E402


def _inspection(request: ExistingInstallInspectionRequest) -> ExistingInstallInspectionResult:
    target_stat = request.target.stat()
    parent_stat = request.target.parent.stat()
    observed = ObservedPaths(ObservationAvailability.OBSERVED, ())
    facts = ExistingInstallFacts(
        ProvenanceCondition.STRICT,
        InspectionAnchorKind.CURRENT_CHANNEL,
        None,
        InspectionStatus.VERIFIED,
        RepositoryRelation.CANONICAL,
        ChannelRelation.CURRENT,
        "a" * 40,
        "b" * 40,
        WorkingTreeCondition.CLEAN,
        observed,
        observed,
        observed,
        observed,
        observed,
        observed,
        ObservedPathPairs(ObservationAvailability.OBSERVED, ()),
        observed,
        observed,
        observed,
        ObservedBoolean.FALSE,
        ObservedBoolean.FALSE,
    )
    channel = ChannelInspectionIdentity(
        "stable",
        "https://example.invalid/seed.git",
        "r1",
        "a" * 40,
        "b" * 40,
        "profile",
        "sha256:" + "c" * 64,
        "123e4567-e89b-12d3-a456-426614174001",
        "123e4567-e89b-12d3-a456-426614174002",
        "sha256:" + "d" * 64,
        ExistingInstallContractIdentity("existing-install", 1, "sha256:" + "e" * 64),
        "catalog",
        "sha256:" + "f" * 64,
        "seed",
        "sha256:" + "0" * 64,
        "sha256:" + "2" * 64,
        "anchors",
        "sha256:" + "1" * 64,
    )
    return ExistingInstallInspectionResult(
        request,
        TargetFilesystemIdentity(
            request.target,
            request.target.resolve(strict=True),
            parent_stat.st_dev,
            parent_stat.st_ino,
            target_stat.st_dev,
            target_stat.st_ino,
        ),
        channel,
        facts,
        ExistingInstallClassification(
            ExistingInstallClass.CLEAN_FAST_FORWARD_SEED_CLONE,
            (),
            "allow",
            "allowed_after_import",
            False,
        ),
        (
            InspectionCheck(
                "identity",
                "fixture",
                True,
                InspectionStatus.VERIFIED,
                None,
                "fixture identity",
                None,
                None,
                "fixture",
            ),
        ),
        InspectionPreservationFacts(0, 0, 0, 0, 0, 0, 0, 0, (), ()),
    )


def _assert_idempotent_rerun(request: ImportRequest, target_marker: Path) -> None:
    first_preview = preview_import(request)
    first = enrollment.enroll_import(request, first_preview.fingerprint)
    assert first.status == "imported"
    paths = request.manager_paths
    inventory_before = paths.maintenance_inventory_path.read_bytes()
    journal_path = paths.operation_path(first.preview.instance_id, first.preview.operation_id)
    journal_before = journal_path.read_bytes()
    second_preview = preview_import(request)
    second = enrollment.enroll_import(request, second_preview.fingerprint)
    assert second.status == "already_managed"
    _assert_finalized_record(read_maintenance_inventory_v2(paths.maintenance_inventory_path), first)
    assert paths.maintenance_inventory_path.read_bytes() == inventory_before
    assert journal_path.read_bytes() == journal_before
    assert target_marker.read_bytes() == b"unchanged"


def _assert_finalized_record(
    records: tuple[InstanceInventoryRecordV2, ...], first: ImportEnrollmentResult
) -> None:
    """The single finalized row binds the exact descriptor digest, not the contract digest."""
    assert len(records) == 1
    record = records[0]
    assert record.instance_id == first.preview.instance_id
    assert record.active_operation is None
    assert record.channel.descriptor_digest == "sha256:" + "2" * 64
    assert record.channel.descriptor_digest != "sha256:" + "e" * 64
    assert record.last_verified_operation_id == first.preview.operation_id


def main() -> int:
    with TemporaryDirectory() as temporary:
        root = Path(temporary)
        target = root / "existing"
        target.mkdir()
        target_marker = target / "operator-owned.txt"
        target_marker.write_bytes(b"unchanged")
        paths = ManagerPaths(root / "config", root / "state", root / "cache")
        for directory in (paths.config_dir, paths.state_dir, paths.cache_dir):
            directory.mkdir(mode=0o700)
        request = ImportRequest("fixture", target, "stable", paths)
        original = enrollment.inspect_existing_install
        enrollment.inspect_existing_install = lambda request, metadata_loader: _inspection(request)
        try:
            _assert_idempotent_rerun(request, target_marker)
        finally:
            enrollment.inspect_existing_install = original
    print("import_enrollment_rerun_smoke OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
