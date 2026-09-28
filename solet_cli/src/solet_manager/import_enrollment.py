"""Manager-only enrollment of an already inspected existing Solet."""

from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path
from typing import cast

from solet_setup_contracts import canonical_sha256

from .create_origin_enrollment import (
    CreateOriginMetadataLoader,
    find_create_origin_record,
    recorded_target_identity,
    require_create_origin_target,
    require_update_eligible_create_origin,
)
from .errors import ManagedIdentityDriftError
from .existing_install_inspection import (
    ChannelInspectionIdentity,
    ExistingInstallInspectionRequest,
    ExistingInstallInspectionResult,
    InspectionEffectTracker,
    InstalledInspectionMetadata,
    InstalledInspectionMetadataLoader,
    inspect_existing_install,
    load_installed_inspection_metadata,
)
from .maintenance_inventory import read_maintenance_inventory_v2, write_maintenance_inventory_v2
from .models import (
    ActiveOperation,
    ChannelIdentity,
    CommandResult,
    ContractIdentities,
    ExitCode,
    FilesystemIdentity,
    InstanceInventoryRecordV2,
    InstanceRecord,
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
from .paths import ManagerPaths
from .registry import (
    InstanceRegistry,
    MaintenanceInventoryRegistry,
    build_combined_registry_snapshot,
    require_unique_identity,
)
from .release_identity_gate import require_manager_seed_pairing
from .state_io import instance_lock, write_content_addressed_json
from .transaction import (
    append_maintenance_attempt,
    create_import_maintenance_operation,
    maintenance_evidence,
    read_maintenance_operation,
    transition_maintenance_operation,
    utc_now,
    write_maintenance_operation,
)

_NON_TOUCH = (
    "credentials",
    "documents",
    "keychain_and_vault",
    "launchagents_and_routers",
    "local_session_history",
    "logs_and_runtime_state",
    "memories_and_knowledge_state",
    "named_launchers",
    "operator_authored_files",
    "postgresql",
    "profile_config",
    "profile_data",
    "shell_startup",
    "tracked_local_modifications",
)


@dataclass(frozen=True, slots=True)
class ImportRequest:
    name: str
    target: Path
    channel: str
    manager_paths: ManagerPaths


@dataclass(frozen=True, slots=True)
class ImportPreview:
    request: ImportRequest
    fingerprint: str
    bundle: dict[str, JsonValue]
    instance_id: str
    operation_id: str
    idempotency_key: str
    inspection: object
    management_origin: ManagementOrigin = ManagementOrigin.IMPORT
    #: The installed channel's release commit when the identity was proven against the Manager's own
    #: create record (iss_836499b3): the proven release may be behind it.  ``None`` otherwise.
    channel_release_commit: str | None = None
    #: Review R2-2: an unfinished import of this same instance whose Manager-side facts (channel, contract,
    #: inspection bundle) are no longer current.  Approving this preview records it abandoned and enrolls
    #: under ``operation_id``, which names it as its predecessor.  ``None`` when there is nothing to supersede.
    superseded_operation_id: str | None = None
    #: Every earlier import operation of this instance in the supersede chain, oldest first.
    predecessor_operation_ids: tuple[str, ...] = ()

    def to_command_result(self) -> CommandResult:
        return CommandResult(
            "import_preview",
            "preview_ready",
            "Existing Solet import preview completed.",
            ExitCode.OK,
            data={
                "approval_fingerprint": self.fingerprint,
                "instance_id": self.instance_id,
                "operation_id": self.operation_id,
                "target_byte_writes": 0,
                "manager_state_writes": 0,
                "non_touch_surfaces": list(_NON_TOUCH),
            },
        )


@dataclass(frozen=True, slots=True)
class ImportEnrollmentResult:
    preview: ImportPreview
    status: str
    management_origin: ManagementOrigin = ManagementOrigin.IMPORT
    managed_instance_id: str | None = None

    def to_command_result(self) -> CommandResult:
        return CommandResult(
            "existing_install_import",
            self.status,
            "Existing Solet import is not yet enabled for target mutation.",
            ExitCode.OK,
            data={
                "instance_id": self.managed_instance_id or self.preview.instance_id,
                "operation_id": self.preview.operation_id,
                "management_origin": self.management_origin.value,
                "target_byte_writes": 0,
            },
        )


def compute_import_key(
    *,
    target_device: int,
    target_inode: int,
    canonical_target: str,
    solet_name: str,
    committed_provenance_seed_id: str,
    committed_head: str,
) -> str:
    return canonical_sha256(
        [
            "import",
            {"device": target_device, "inode": target_inode},
            canonical_target,
            solet_name,
            committed_provenance_seed_id,
            committed_head,
        ]
    )


def compute_operation_id(import_key: str, *, supersedes: str | None = None) -> str:
    """The import operation id; a successor names the operation it supersedes, so the two never collide."""
    identities: list[JsonValue] = ["operation", import_key]
    if supersedes is not None:
        identities.append(f"supersedes={supersedes}")
    return "opr_" + canonical_sha256(identities).removeprefix("sha256:")[:32]


_MAX_SUPERSEDED_IMPORTS = 16
_UNFINISHED_IMPORT_STATUSES = frozenset({"prepared", "bundle_cached", "inventory_published"})


def _import_fingerprint(bundle_digest: str, instance_id: str, operation_id: str) -> str:
    return canonical_sha256([bundle_digest, instance_id, operation_id, list(_NON_TOUCH)])


def _current_import_operation(
    paths: ManagerPaths, instance_id: str, import_key: str, bundle_digest: str
) -> tuple[str, tuple[str, ...], str | None]:
    """The operation this preview enrolls under, its predecessors, and the stale one it must abandon.

    An unfinished journal approved under different Manager-side facts is stale: the same instance proves
    again, but its channel, contract or inspection bundle changed (a Manager upgrade between an interrupted
    enrollment and its retry).  Its successor is derived from it; an abandoned journal is simply passed.
    """
    predecessors: list[str] = []
    operation_id = compute_operation_id(import_key)
    for _ in range(_MAX_SUPERSEDED_IMPORTS):
        path = paths.operation_path(instance_id, operation_id)
        if not path.exists():
            return operation_id, tuple(predecessors), None
        journal = read_maintenance_operation(path)
        status = journal["status"]
        approval = cast(dict[str, JsonValue], journal["approval"])
        stale = status in _UNFINISHED_IMPORT_STATUSES and approval["fingerprint"] != _import_fingerprint(
            bundle_digest, instance_id, operation_id
        )
        if status != "abandoned" and not stale:
            return operation_id, tuple(predecessors), None
        predecessors.append(operation_id)
        operation_id = compute_operation_id(import_key, supersedes=operation_id)
        if stale:
            return operation_id, tuple(predecessors), predecessors[-1]
    raise ValueError("corrupt_state")


def compute_stage_key(operation_id: str, stage_id: str, stage_inputs: JsonValue) -> str:
    return canonical_sha256(["stage", operation_id, stage_id, canonical_sha256(stage_inputs)])


def observe_service_identity(request: ImportRequest) -> dict[str, JsonValue]:
    """Passive path projection only; no launcher is followed or executed."""
    root = request.target.resolve(strict=True)
    return {
        "service_cli_path": str(root / ".venv/bin/solet"),
        "bridge_cli_path": str(root / ".venv/bin/solet-bridge"),
        "named_launcher_path": str(Path.home() / ".local/bin" / request.name),
        "named_launcher_target": None,
        "profile_id": None,
        "app_home": str(root / "profile"),
        "launchagent_label": f"local.solet.{request.name}",
        "router_label": None,
        "router_socket": None,
    }


def build_inspection_bundle(request: ImportRequest, inspection: object) -> dict[str, JsonValue]:
    result = inspection.to_command_result()
    return {
        "schema_version": 1,
        "kind": "existing_install_import_inspection",
        "request": {
            "name": request.name,
            "canonical_target": str(request.target.resolve(strict=True)),
            "channel_id": request.channel,
        },
        "target_identity": result.data["target"],
        "channel_identity": result.data["channel"],
        "facts": result.data["source"],
        "classification": result.data["classification"],
        "checks": result.data["checks"],
        "service_identity": observe_service_identity(request),
        "preservation": result.data["preservation"],
    }


@dataclass(frozen=True, slots=True)
class ImportInspection:
    """One import inspection and, for a create-origin instance, the installed channel it bound to."""

    result: ExistingInstallInspectionResult
    management_origin: ManagementOrigin
    channel_release_commit: str | None


def preview_import(
    request: ImportRequest, *, installed_loader: InstalledInspectionMetadataLoader | None = None
) -> ImportPreview:
    inspected = inspect_for_import(request, installed_loader=installed_loader)
    if inspected.result.classification.import_disposition != "allow":
        raise ValueError("import_not_allowed")
    return preview_from_inspection(request, inspected)


def inspect_for_import(
    request: ImportRequest, *, installed_loader: InstalledInspectionMetadataLoader | None = None
) -> ImportInspection:
    """Inspect ``request.target``; a Manager-created name is proven against its create record (iss_836499b3)."""
    if not request.name.islower() or not request.name.replace("-", "").isalnum():
        raise ValueError("invalid_import_name")
    loader = _paired_installed_metadata if installed_loader is None else installed_loader
    created = find_create_origin_record(request.manager_paths, request.name)
    if created is None:
        return ImportInspection(_inspect(request, loader), ManagementOrigin.IMPORT, None)
    # The one create-origin gate (iss_fcbfabb7); apply re-runs it under the registry lock
    # (``_matching_v1_managed_import``), so an import preview and its apply cannot disagree.
    require_update_eligible_create_origin(request.manager_paths, created)
    recorded = require_create_origin_target(created, request.target)
    proof = CreateOriginMetadataLoader(created, recorded.path, loader)
    result = _inspect(replace(request, target=recorded.path), proof)
    recorded.require_inspected(result.target_identity)
    installed = proof.installed
    return ImportInspection(
        result, ManagementOrigin.CREATE, None if installed is None else installed.channel_identity.commit
    )


def _inspect(request: ImportRequest, loader: InstalledInspectionMetadataLoader) -> ExistingInstallInspectionResult:
    return inspect_existing_install(
        ExistingInstallInspectionRequest(request.target, request.channel, request.manager_paths),
        metadata_loader=loader,
    )


def preview_from_inspection(request: ImportRequest, inspected: ImportInspection) -> ImportPreview:
    """The deterministic preview of an import the inspection already allows."""
    inspection = inspected.result
    bundle = build_inspection_bundle(request, inspection)
    target = inspection.target_identity
    seed = inspection.channel_identity.seed_id
    head = inspection.facts.head_commit
    if head is None:
        raise ValueError("import_identity_unproven")
    key = compute_import_key(
        target_device=target.target_device,
        target_inode=target.target_inode,
        canonical_target=str(target.canonical_display),
        solet_name=request.name,
        committed_provenance_seed_id=seed,
        committed_head=head,
    )
    instance_id = "ins_" + key.removeprefix("sha256:")[:32]
    bundle_digest = canonical_sha256(bundle)
    operation_id, predecessors, superseded = _current_import_operation(
        request.manager_paths, instance_id, key, bundle_digest
    )
    fingerprint = _import_fingerprint(bundle_digest, instance_id, operation_id)
    return ImportPreview(
        request,
        fingerprint,
        bundle,
        instance_id,
        operation_id,
        key,
        inspection,
        inspected.management_origin,
        inspected.channel_release_commit,
        superseded,
        predecessors,
    )


def _paired_installed_metadata(channel: str, tracker: InspectionEffectTracker) -> InstalledInspectionMetadata:
    """The installed descriptor, refused at selection when its seed half does not pair with the manager (design §7.3)."""
    metadata = load_installed_inspection_metadata(channel, tracker)
    require_manager_seed_pairing(metadata.seed_lock)
    return metadata


def enroll_import(request: ImportRequest, approved_fingerprint: str) -> ImportEnrollmentResult:
    with instance_lock(request.manager_paths.lock_path(request.name), create=True):
        preview = preview_import(request)
        if preview.fingerprint != approved_fingerprint:
            raise ValueError("probe_drift")
        with instance_lock(request.manager_paths.registry_lock_path, create=True):
            return _enroll_import_locked(preview)


def import_resume_repair(name: str, target: str, channel: str) -> str:
    """The exact command that finishes an interrupted enrollment (review B3); every import stage is idempotent."""
    return (
        f"Finish the interrupted enrollment with `solet-manager import {name} --target {target} --channel {channel} "
        "--dry-run`, then rerun it with --yes --approval-fingerprint <the fingerprint it prints>."
    )


def enroll_import_holding_instance_lock(preview: ImportPreview) -> ImportEnrollmentResult:
    """Enroll a preview the caller computed while already holding the instance lock.

    ``update`` enrolls a create-origin instance inline under its own instance lock (iss_836499b3);
    the lock is not re-entrant, so this takes only the registry lock.  The caller has already
    proven the preview current by an approved fingerprint that binds it.
    """
    with instance_lock(preview.request.manager_paths.registry_lock_path, create=True):
        return _enroll_import_locked(preview)


def _enroll_import_locked(preview: ImportPreview) -> ImportEnrollmentResult:
    """Enroll only while both Manager registry identities are stable."""
    v1_match = _matching_v1_managed_import(preview)
    if v1_match is not None:
        preview = replace(preview, management_origin=ManagementOrigin.CREATE)
    if preview.superseded_operation_id is not None:
        _abandon_superseded_import(preview, preview.superseded_operation_id)
    _require_unclaimed_import_identity(preview)
    if _is_finalized_matching_import(preview):
        return _enrollment_result(preview, "already_managed", v1_match)
    bundle_digest = canonical_sha256(preview.bundle)
    already_managed = _write_prepared_import_journal(preview, bundle_digest)
    write_content_addressed_json(
        preview.request.manager_paths.contract_cache_path(bundle_digest), preview.bundle, bundle_digest
    )
    _advance_journal(preview, "inspection_bundle_cached", "bundle_cached", "bundle_cached")
    _publish_inventory(preview, bundle_digest)
    _advance_journal(preview, "inventory_published", "inventory_published", "inventory_published")
    _finalize_import(preview)
    return _enrollment_result(
        preview, "already_managed" if already_managed or v1_match else "imported", v1_match
    )


def _enrollment_result(
    preview: ImportPreview, status: str, v1_match: InstanceRecord | None
) -> ImportEnrollmentResult:
    return ImportEnrollmentResult(
        preview, status, preview.management_origin, None if v1_match is None else v1_match.name
    )


def _matching_v1_managed_import(preview: ImportPreview) -> InstanceRecord | None:
    """Return an exact create-origin match, or fail closed on a partial one."""
    records = InstanceRegistry(preview.request.manager_paths.registry_path).list()
    record = next((item for item in records if item.name == preview.request.name), None)
    if record is None:
        return None
    inspection = cast(ExistingInstallInspectionResult, preview.inspection)
    require_update_eligible_create_origin(preview.request.manager_paths, record)
    if not _v1_record_matches_inspection(record, inspection):
        raise ManagedIdentityDriftError("create-origin identity does not match inspected import")
    return record


def _v1_record_matches_inspection(record: InstanceRecord, inspection: ExistingInstallInspectionResult) -> bool:
    facts = inspection.facts
    return (
        _v1_target_matches(record, inspection)
        and facts.head_commit == record.seed_commit
        and facts.head_tree == record.seed_tree_hash
        and _v1_seed_id(record, inspection) == inspection.channel_identity.seed_id
    )


def _v1_target_matches(record: InstanceRecord, inspection: ExistingInstallInspectionResult) -> bool:
    """The inspected directory is the one at the literal v1 target path, reached through no link (review B2)."""
    recorded = recorded_target_identity(record)
    inspected = inspection.target_identity
    return inspected.canonical_display == recorded.path and (recorded.device, recorded.inode) == (
        inspected.target_device,
        inspected.target_inode,
    )


def _v1_seed_id(record: InstanceRecord, inspection: ExistingInstallInspectionResult) -> str | None:
    """Derive the v1 seed identity from its immutable seed fields and channel."""
    channel = inspection.channel_identity
    facts = inspection.facts
    if (
        record.seed_repository != channel.repository
        or record.seed_commit != channel.commit
        or record.seed_tree_hash != channel.tree_hash
        or facts.provenance_condition.value != "strict"
    ):
        return None
    return channel.seed_id


def _require_unclaimed_import_identity(preview: ImportPreview) -> None:
    """Apply the complete combined-registry uniqueness rule before writing."""
    paths = preview.request.manager_paths
    snapshot = build_combined_registry_snapshot(
        InstanceRegistry(paths.registry_path).list(),
        MaintenanceInventoryRegistry(paths.maintenance_inventory_path).list(),
    )
    record = _inventory_record(preview, canonical_sha256(preview.bundle), utc_now())
    incumbent = next(
        (item for item in snapshot.v2_records if item.instance_id == preview.instance_id),
        None,
    )
    if incumbent is not None:
        if not _same_import_identity(incumbent, record) and not _superseded_incumbent(preview, incumbent, record):
            raise ValueError("managed_identity_drift")
        return
    if preview.management_origin is ManagementOrigin.CREATE:
        build_combined_registry_snapshot(snapshot.v1_records, (*snapshot.v2_records, record))
        return
    require_unique_identity(
        snapshot.indexes,
        requested={
            "name": record.name,
            "target": record.target.canonical_path,
            "filesystem_identity": (
                record.target.filesystem_identity.device,
                record.target.filesystem_identity.inode,
            ),
            "launchagent_label": record.service_identity.launchagent_label,
            "named_launcher_path": record.service_identity.named_launcher_path,
        },
    )


def _write_prepared_import_journal(preview: ImportPreview, bundle_digest: str) -> bool:
    """Durably establish the deterministic Manager-only import recovery point."""
    inspection = preview.inspection
    target = inspection.target_identity
    operation_path = preview.request.manager_paths.operation_path(
        preview.instance_id, preview.operation_id
    )
    inspection = cast(ExistingInstallInspectionResult, preview.inspection)
    facts = inspection.facts
    if facts.head_commit is None or facts.head_tree is None:
        raise ValueError("import_identity_unproven")
    paths = preview.request.manager_paths
    document = create_import_maintenance_operation(
        operation_id=preview.operation_id,
        instance_id=preview.instance_id,
        idempotency_key=preview.idempotency_key,
        name=preview.request.name,
        canonical_target=str(target.canonical_display),
        target_device=target.target_device,
        target_inode=target.target_inode,
        channel_id=preview.request.channel,
        provenance_seed_id=inspection.channel_identity.seed_id,
        head_commit=facts.head_commit,
        head_tree=facts.head_tree,
        inspection_bundle_digest=bundle_digest,
        diagnostic_contract_digest=inspection.channel_identity.existing_install_contract.bundle_digest,
        approval_fingerprint=preview.fingerprint,
        manager_write_paths=tuple(
            sorted(
                (
                    str(paths.contract_cache_path(bundle_digest)),
                    str(paths.lock_path(preview.request.name)),
                    str(paths.registry_lock_path),
                    str(paths.maintenance_inventory_path),
                    str(operation_path),
                )
            )
        ),
        non_touch_surfaces=_NON_TOUCH,
    )
    if not operation_path.exists():
        write_maintenance_operation(operation_path, None, document)
        return False
    existing = read_maintenance_operation(operation_path)
    for key in (
        "operation_id",
        "instance_id",
        "kind",
        "idempotency_key",
        "input",
        "current_identity",
        "contract_digests",
    ):
        if existing[key] != document[key]:
            raise ValueError("corrupt_state")
    existing_approval = cast(dict[str, JsonValue], existing["approval"])
    document_approval = cast(dict[str, JsonValue], document["approval"])
    if existing_approval["fingerprint"] != document_approval["fingerprint"]:
        raise ValueError("corrupt_state")
    if existing["status"] not in {"prepared", "bundle_cached", "inventory_published", "verified"}:
        raise ValueError("corrupt_state")
    return True


def _publish_inventory(preview: ImportPreview, bundle_digest: str) -> None:
    record = _inventory_record(preview, bundle_digest, utc_now())
    records = read_maintenance_inventory_v2(preview.request.manager_paths.maintenance_inventory_path)
    incumbent = next((item for item in records if item.instance_id == preview.instance_id), None)
    if incumbent is not None:
        if _same_import_identity(incumbent, record):
            return
        if not _superseded_incumbent(preview, incumbent, record):
            raise ValueError("corrupt_state")
        # Review R2-2: the row the abandoned import published is republished under the successor.
        successor = replace(record, created_at=incumbent.created_at)
        write_maintenance_inventory_v2(
            preview.request.manager_paths.maintenance_inventory_path,
            tuple(successor if item.instance_id == preview.instance_id else item for item in records),
        )
        return
    write_maintenance_inventory_v2(
        preview.request.manager_paths.maintenance_inventory_path,
        tuple(sorted((*records, record), key=lambda item: item.instance_id)),
    )


def _finalize_import(preview: ImportPreview) -> None:
    now = utc_now()
    _advance_journal(preview, "enrollment_verified", "verified", "enrollment_verified")
    records = read_maintenance_inventory_v2(preview.request.manager_paths.maintenance_inventory_path)
    finalized = tuple(replace(record, active_operation=None, last_verified_operation_id=preview.operation_id, last_verified_at=now, updated_at=now) if record.instance_id == preview.instance_id else record for record in records)
    write_maintenance_inventory_v2(preview.request.manager_paths.maintenance_inventory_path, finalized)


def _advance_journal(
    preview: ImportPreview, stage_id: str, operation_status: str, evidence_code: str
) -> None:
    """Record one verified import stage and its matching operation transition."""
    path = preview.request.manager_paths.operation_path(preview.instance_id, preview.operation_id)
    previous = read_maintenance_operation(path)
    order = {"prepared": 0, "bundle_cached": 1, "inventory_published": 2, "verified": 3}
    current_status = previous["status"]
    if not isinstance(current_status, str) or current_status not in order:
        raise ValueError("corrupt_state")
    if current_status == operation_status or order[current_status] > order[operation_status]:
        return
    attempt = len(cast(list[JsonValue], previous["attempts"]))
    next_value = append_maintenance_attempt(
        previous,
        stage_id=stage_id,
        status="verified",
        evidence=(
            maintenance_evidence(
                preview.operation_id,
                attempt,
                "state",
                evidence_code,
                preview.fingerprint,
            ),
        ),
    )
    result: dict[str, JsonValue] | None = (
        {"kind": "imported", "inventory_instance_id": preview.instance_id}
        if operation_status == "verified"
        else None
    )
    next_value = transition_maintenance_operation(
        next_value,
        status=operation_status,
        result=result,
    )
    write_maintenance_operation(path, previous, next_value)


def pending_inventory_record(preview: ImportPreview) -> InstanceInventoryRecordV2:
    """The finalized v2 row this preview would publish, in memory only (no active operation).

    ``update --dry-run`` renders a create-origin instance's update from it before any Manager write
    (iss_836499b3); timestamps are not part of any fingerprint.
    """
    return replace(_inventory_record(preview, canonical_sha256(preview.bundle), utc_now()), active_operation=None)


def _inventory_record(
    preview: ImportPreview, bundle_digest: str, timestamp: str
) -> InstanceInventoryRecordV2:
    """Build the field-complete v2 import record from the Step-2 result."""
    inspection = cast(ExistingInstallInspectionResult, preview.inspection)
    target = inspection.target_identity
    channel = inspection.channel_identity
    facts = inspection.facts
    if facts.head_commit is None or facts.head_tree is None:
        raise ValueError("import_identity_unproven")
    eligibility = _update_eligibility(preview, facts.head_commit)
    return InstanceInventoryRecordV2(
        preview.instance_id,
        preview.request.name,
        TargetIdentity(
            str(target.canonical_display),
            FilesystemIdentity(target.target_device, target.target_inode),
            FilesystemIdentity(target.parent_device, target.parent_inode),
        ),
        preview.management_origin,
        ManagementState.DIAGNOSTIC,
        UpdateEligibility(eligibility, ()),
        ServiceIdentity(
            str(target.canonical_display / ".venv/bin/solet"),
            str(target.canonical_display / ".venv/bin/solet-bridge"),
            str(Path.home() / ".local/bin" / preview.request.name),
            None,
            None,
            str(target.canonical_display / "profile"),
            f"local.solet.{preview.request.name}",
            None,
            None,
        ),
        ChannelIdentity(
            channel.channel_id,
            channel.descriptor_digest,
            channel.repository,
        ),
        # Step 7 (CH-3 measured): the identity the inspection PROVED -- the matched anchor's for a clone behind the
        # channel release, the channel's at it -- with the ``sha256:`` prefix the inventory parser requires; the
        # landed row named the channel's provenance for every clone and wrote bare digests the Manager could not read back.
        _proved_provenance(inspection, channel),
        ReleaseIdentity(channel.repository, facts.head_commit, facts.head_tree, None),
        None,
        None,
        ContractIdentities(channel.existing_install_contract.bundle_digest, None, None, None, None),
        bundle_digest,
        ActiveOperation(MaintenanceOperationKind.IMPORT, preview.operation_id),
        None,
        timestamp,
        timestamp,
        timestamp,
        None,
    )


def _update_eligibility(preview: ImportPreview, head_commit: str) -> UpdateEligibilityState:
    """Current only at the channel release; a create-origin proof inspects against its own recorded release."""
    inspection = cast(ExistingInstallInspectionResult, preview.inspection)
    if preview.channel_release_commit is not None:
        current = head_commit == preview.channel_release_commit
    else:
        current = inspection.facts.channel_relation.value == "current"
    return UpdateEligibilityState.CURRENT if current else UpdateEligibilityState.AVAILABLE


def _proved_provenance(inspection: ExistingInstallInspectionResult, channel: ChannelInspectionIdentity) -> ObservedProvenanceIdentity:
    facts = inspection.facts
    anchor = inspection.matched_anchor
    if anchor is not None:
        return ObservedProvenanceIdentity(facts.provenance_condition.value, _prefixed(anchor.provenance_sha256), anchor.seed_id, anchor.origin_id, _prefixed_text(anchor.manifest_sha256), facts.anchor_id)
    return ObservedProvenanceIdentity(facts.provenance_condition.value, _prefixed(channel.provenance_sha256), channel.seed_id, channel.origin_id, _prefixed_text(channel.manifest_sha256), facts.anchor_id)


def _prefixed(digest: str | None) -> str | None:
    return None if digest is None else _prefixed_text(digest)


def _prefixed_text(digest: str) -> str:
    return digest if digest.startswith("sha256:") else "sha256:" + digest


def _is_finalized_matching_import(preview: ImportPreview) -> bool:
    """Return true only for the exact completed enrollment identity.

    A pre-existing inventory row is not evidence of management by itself: all
    source and filesystem identity fields, plus the finalized operation link,
    must agree with the fresh Step-2 inspection.
    """
    records = read_maintenance_inventory_v2(preview.request.manager_paths.maintenance_inventory_path)
    record = next((item for item in records if item.instance_id == preview.instance_id), None)
    if record is None:
        return False
    expected = _inventory_record(preview, canonical_sha256(preview.bundle), record.created_at)
    if _superseded_incumbent(preview, record, expected):
        return False
    if not _same_import_identity(record, expected):
        raise ValueError("managed_identity_drift")
    if record.active_operation is not None:
        return False
    if record.last_verified_operation_id != preview.operation_id:
        raise ValueError("corrupt_state")
    path = preview.request.manager_paths.operation_path(preview.instance_id, preview.operation_id)
    if not path.exists() or read_maintenance_operation(path)["status"] != "verified":
        raise ValueError("corrupt_state")
    return True


def _abandon_superseded_import(preview: ImportPreview, superseded: str) -> None:
    """Record the stale import abandoned before its successor writes anything (idempotent)."""
    path = preview.request.manager_paths.operation_path(preview.instance_id, superseded)
    previous = read_maintenance_operation(path)
    if previous["status"] == "abandoned":
        return
    if previous["status"] not in _UNFINISHED_IMPORT_STATUSES:
        raise ValueError("corrupt_state")
    result: dict[str, JsonValue] = {
        "kind": "abandoned",
        "reason_code": "superseded_by_manager_facts",
        "repair": f"Superseded by {preview.operation_id}: the Manager's channel facts changed before this enrollment finished. Nothing to do.",
    }
    write_maintenance_operation(path, previous, transition_maintenance_operation(previous, status="abandoned", result=result))


def _superseded_incumbent(
    preview: ImportPreview, incumbent: InstanceInventoryRecordV2, expected: InstanceInventoryRecordV2
) -> bool:
    """The row an abandoned predecessor of this preview published: the same instance, stale Manager-side facts."""
    active = incumbent.active_operation
    return (
        active is not None
        and active.kind is MaintenanceOperationKind.IMPORT
        and active.operation_id in preview.predecessor_operation_ids
        and incumbent.instance_id == expected.instance_id
        and incumbent.name == expected.name
        and incumbent.target == expected.target
        and incumbent.management_origin is expected.management_origin
        and incumbent.observed_provenance == expected.observed_provenance
        and incumbent.source_release == expected.source_release
    )


def _same_import_identity(
    actual: InstanceInventoryRecordV2, expected: InstanceInventoryRecordV2
) -> bool:
    return (
        actual.instance_id == expected.instance_id
        and actual.name == expected.name
        and actual.target == expected.target
        and actual.management_origin is expected.management_origin
        and actual.channel == expected.channel
        and actual.observed_provenance == expected.observed_provenance
        and actual.source_release == expected.source_release
        and actual.contract_identities == expected.contract_identities
        and actual.inspection_bundle_digest == expected.inspection_bundle_digest
    )


def reconcile_import_operation(
    request: ImportRequest, approved_fingerprint: str
) -> ImportEnrollmentResult:
    return enroll_import(request, approved_fingerprint)
