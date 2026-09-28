"""Inline enrollment of a Manager-created instance by ``solet-manager update`` (iss_836499b3).

A Solet that ``solet create`` installed has a v1 create row and a create transaction but, before
this change, no v2 maintenance row -- so ``update`` refused it as unmanaged.  ``update`` is the
single user-facing verb for it: when no v2 row names the instance and the Manager's own create record
does, ``--dry-run`` proves the instance against that record (``create_origin_enrollment``), renders the
ordinary update preview from the would-be row, and discloses the enrollment; ``--yes`` enrolls and
continues the same apply.  The approval fingerprint binds the enrollment, and an unproven identity is
refused loud, never routed around.  The create transaction need not be ``verified``: one whose install
is over but whose completion checks stayed blocked (the r46-r48 embeddings defect) enrolls, and the
dry-run's ``enrollment.create_transaction`` names those checks (iss_fcbfabb7).

An enrollment interrupted after its row was published but before it was finalized (review B3) leaves a
create-origin row whose active operation is that import.  ``update`` resumes it: the fresh proof must
reproduce the very operation the row carries, the binding and therefore the approval fingerprint are the
ones the interrupted apply was approved under, and the idempotent import stages finish the enrollment
before the update continues.  If the Manager was upgraded in between (review R2-2), the same instance
proves again under new channel facts: the dry-run discloses ``supersede`` and names the stale operation,
and ``--yes`` records it abandoned and enrolls under a successor operation bound into the new fingerprint.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Never

from ._existing_install_inspection_metadata import installed_channel_ids
from .create_origin_enrollment import CreateOriginEligibility, find_create_origin_record, require_update_eligible_create_origin
from .errors import OperationInProgressError, ProbeDriftError, UpdateBlockedError
from .existing_install_inspection import (
    ExistingInstallInspectionResult,
    InspectionBoundaryViolation,
    InspectionEffectTracker,
    InspectionPreservationFacts,
    InspectionProbe,
    InspectionStatus,
    InstalledInspectionMetadata,
    InstalledInspectionMetadataLoader,
    PreservationEffect,
)
from .import_enrollment import (
    ImportPreview,
    ImportRequest,
    enroll_import_holding_instance_lock,
    import_resume_repair,
    inspect_for_import,
    pending_inventory_record,
    preview_from_inspection,
)
from .maintenance_inventory import read_maintenance_inventory_v2
from .models import InstanceInventoryRecordV2, InstanceRecord, JsonValue, MaintenanceOperationKind, ManagementOrigin
from .release_identity_gate import require_manager_seed_pairing
from .update_execution import UpdateRequest, enrollment_binding, load_update_record, probe_update
from .update_local_state import require_instance_interpreter


@dataclass(frozen=True, slots=True)
class PendingEnrollment:
    """A proven, not yet written, create-origin enrollment and the v2 row it would publish."""

    preview: ImportPreview
    record: InstanceInventoryRecordV2
    #: What the create transaction proves; a create blocked only at completion is disclosed, never hidden.
    create: CreateOriginEligibility
    #: The row is already published and this enrollment finishes it (an interrupted apply or create).
    resuming: bool = False

    def binding(self) -> dict[str, JsonValue]:
        return enrollment_binding(self.preview.operation_id, self.preview.fingerprint)

    def disclosure(self) -> dict[str, JsonValue]:
        """What ``--dry-run`` shows: approving this preview enrolls the instance first."""
        superseded = self.preview.superseded_operation_id
        status = "supersede" if superseded is not None else "resume" if self.resuming else "planned"
        return {
            "status": status,
            **self.binding(),
            "superseded_operation_id": superseded,
            "management_origin": "create",
            "instance_id": self.preview.instance_id,
            "channel_id": self.preview.request.channel,
            "proven_release": {"commit": self.record.source_release.commit, "tree": self.record.source_release.tree},
            "create_transaction": self.create.disclosure(),
        }


def plan_create_origin_enrollment(request: UpdateRequest) -> PendingEnrollment | None:
    """``None`` unless an update-eligible create record names an instance with no finished v2 row; refuse loud if unproven."""
    paths = request.manager_paths
    row = next((item for item in read_maintenance_inventory_v2(paths.maintenance_inventory_path) if item.name == request.name), None)
    if row is not None and not _unfinished_create_enrollment(row):
        return None
    created = find_create_origin_record(paths, request.name)
    if created is None:
        return None
    eligibility = require_update_eligible_create_origin(paths, created)
    import_request = ImportRequest(request.name, Path(created.target), _installed_channel(created), paths)
    inspected = inspect_for_import(import_request, installed_loader=_paired_descriptor_loader(request))
    _require_proven(created, inspected.result)
    preview = preview_from_inspection(import_request, inspected)
    if row is not None:
        _require_resumable(row, preview)
    return PendingEnrollment(preview, pending_inventory_record(preview), eligibility, resuming=row is not None)


def _require_proven(created: InstanceRecord, result: ExistingInstallInspectionResult) -> None:
    if result.classification.import_disposition != "allow":
        failed = sorted(check.check_id for check in result.checks if check.required and check.status is InspectionStatus.FAILED)
        raise UpdateBlockedError(
            "create_origin_identity_unproven",
            f"{created.name!r} no longer proves the release the Manager created it at "
            f"({created.seed_commit}): classification {result.classification.installation_class.value}, "
            f"reasons {list(result.classification.reason_codes)}, failed checks {failed}",
            repair=_unproven_repair(created),
        )


def _unfinished_create_enrollment(row: InstanceInventoryRecordV2) -> bool:
    active = row.active_operation
    return row.management_origin is ManagementOrigin.CREATE and active is not None and active.kind is MaintenanceOperationKind.IMPORT


def _require_resumable(row: InstanceInventoryRecordV2, preview: ImportPreview) -> None:
    """The fresh proof reproduces exactly the interrupted enrollment, or nothing is written."""
    active = row.active_operation
    chain = (preview.operation_id, *preview.predecessor_operation_ids)
    if active is None or row.instance_id != preview.instance_id or active.operation_id not in chain:
        raise OperationInProgressError(
            f"an interrupted enrollment of {row.name!r} ({None if active is None else active.operation_id}) does not "
            f"match what the instance proves now ({preview.operation_id})",
            repair=import_resume_repair(row.name, row.target.canonical_path, row.channel.channel_id),
        )


def enroll_pending(request: UpdateRequest, pending: PendingEnrollment, approved: str) -> InstanceInventoryRecordV2:
    """Under the held instance lock: prove the approval still binds this enrollment, then write it."""
    probe = probe_update(request, pending.record, enrollment=pending.binding())
    if probe.fingerprint is None or probe.fingerprint != approved:
        raise ProbeDriftError(
            "approved fingerprint does not match the lock-time preview",
            repair="Run --dry-run again and approve the fingerprint it renders.",
        )
    # Before any Manager write, exactly as the fresh apply orders it (CH-10/11).
    require_instance_interpreter(probe.host)
    enroll_import_holding_instance_lock(pending.preview)
    return load_update_record(request)


def _installed_channel(created: InstanceRecord) -> str:
    channels = installed_channel_ids(_ChannelTracker())
    if len(channels) != 1:
        raise UpdateBlockedError(
            "create_origin_channel_ambiguous",
            f"the installed Manager serves {len(channels)} channels; the create record does not name one",
            repair=f"Enroll it once with `solet-manager import {created.name} --target {created.target} --channel <channel> --dry-run`.",
        )
    return channels[0]


def _paired_descriptor_loader(request: UpdateRequest) -> InstalledInspectionMetadataLoader:
    """The update's own installed descriptor, refused when its seed half does not pair with the Manager."""

    def load(channel: str, tracker: InspectionEffectTracker) -> InstalledInspectionMetadata:
        metadata = request.descriptor_loader(channel, tracker).metadata
        require_manager_seed_pairing(metadata.seed_lock)
        return metadata

    return load


def _unproven_repair(created: InstanceRecord) -> str:
    return (
        f"The Manager created this Solet at {created.seed_commit} and updates only what it can prove; it never "
        f"resets a target.  Restore the checkout at {created.target} to that commit with its committed "
        "PROVENANCE.json, then preview again."
    )


class _ChannelTracker:
    """Effect tracker for the package catalog read; any forbidden effect fails loud."""

    def __init__(self) -> None:
        self.resources: list[str] = []

    def record_resource_read(self, resource_id: str) -> None:
        self.resources.append(resource_id)

    def record_probe(self, probe: InspectionProbe, argv: tuple[str, ...]) -> None:
        raise InspectionBoundaryViolation(f"catalog read ran probe {probe.value}: {argv}")

    def record_forbidden(self, effect: PreservationEffect) -> Never:
        raise InspectionBoundaryViolation(effect.value)

    def snapshot(self) -> InspectionPreservationFacts:
        return InspectionPreservationFacts(0, 0, 0, 0, 0, 0, 0, 0, (), tuple(self.resources))
