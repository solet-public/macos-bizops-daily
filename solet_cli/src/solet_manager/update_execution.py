"""Step-4 update execution: preview orchestration, lock-time revalidation, exact fetch, fast-forward.

The source stage stops at ``source_advanced``: the source axis names N+1, the
update stays active, and the runtime/verified axes keep their prior values.
From there ``_apply_runtime`` hands the journal to the Step-5 executor, which
now continues through the Step 6 final doctor and promotion.  Step 6 also adds
here: the ``verify`` source mode (zero-delta path, section 4.8), the durable
contract copies written at apply (section 3.2) and read first on resume
(section 4.5), and the routing of every terminal journal to ``reconcile``.

Every target Git invocation goes through ``target_git.run_target_git``: a closed
argument vector with fsmonitor, hooks and signature verification disarmed, no
system/global configuration, no replace refs, no external diff and no prompt,
and a refusal of any repository-scoped configuration that could execute
target-supplied code before a content vector runs (iss_836499b3 B1).  The two
target writes (private-ref fetch, ``merge --ff-only``) pin ``core.hooksPath`` to
an empty Manager-owned directory instead of ``/dev/null``.
"""

from __future__ import annotations

import re
import subprocess
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Never, cast

from solet_setup_contracts import canonical_sha256

from ._existing_install_inspection_metadata import (
    InstalledUpdateDescriptor,
    anchors_document,
    descriptor_from_copy,
    load_installed_update_descriptor,
)
from .contract_copies import (
    descriptor_copy_exists,
    read_transition_contract,
    transition_contract_exists,
    write_descriptor_copy,
    write_transition_contract,
)
from .errors import (
    ApprovalFingerprintMalformedError,
    ApprovalFingerprintRequiredError,
    CandidateCopyMissingError,
    CandidateRefDriftError,
    InstanceUnmanagedError,
    InventoryChannelDescriptorMisbindingError,
    ManagedIdentityDriftError,
    ManagerError,
    OperationInProgressError,
    ProbeDriftError,
    SourceError,
    SourceTransitionIncompleteError,
    StateConflictError,
    StateError,
    TransitionContractMismatchError,
    UpdateBlockedError,
)
from .existing_install_adapters import ExistingInstallAdapterRegistry
from .existing_install_inspection import (
    ChannelInspectionIdentity,
    ExistingInstallContractIdentity,
    ExistingInstallInspectionRequest,
    ExistingInstallInspectionResult,
    InspectionAnchorKind,
    InspectionBoundaryViolation,
    InspectionEffectTracker,
    InspectionPreservationFacts,
    InspectionProbe,
    InspectionStatus,
    InstalledInspectionMetadata,
    ObservedBoolean,
    PreservationEffect,
    inspect_existing_install,
)
from .host_software import base_python_or_none
from .import_enrollment import import_resume_repair
from .local_state import ObservedLocalState, observe_local_state
from .maintenance_inventory import (
    publish_active_update,
    publish_needs_attention,
    publish_source_advance,
    read_maintenance_inventory_v2,
)
from .maintenance_journal import read_maintenance_operation
from .models import (
    MANAGER_VERSION,
    ActiveOperation,
    ChannelIdentity,
    CommandResult,
    ExitCode,
    InstanceInventoryRecordV2,
    JsonValue,
    MaintenanceOperationKind,
    ManagementOrigin,
    ManagementState,
    ObservedProvenanceIdentity,
    ReleaseIdentity,
)
from .paths import ManagerPaths, update_candidate_cache
from .release_lock import seed_lock_from_fields
from .seed_lock_parser import SeedLockFields
from .state_io import ensure_private_directory, instance_lock
from .target_git import GitLayout, run_target_git
from .transaction import utc_now
from .update_candidate import TRANSITION_BUNDLE_DIRECTORY, UpdateCandidate, acquire_update_candidate
from .update_journal import (
    DOCTOR_STATUSES,
    FRONTIER_STATUSES,
    RUNTIME_STATUSES,
    TERMINAL_UPDATE_STATUSES,
    advance_update_journal,
    create_update_journal,
    read_update_journal,
    write_update_journal,
)
from .update_local_state import LocalStateReport, host_preflight, rebaseline_revision, require_instance_interpreter, verify_local_state
from .update_pointer_repair import release_terminal_pointer
from .update_preview import (
    VERIFY_PLANNED_ACTIONS,
    UpdatePreview,
    preview_update,
)
from .update_reduction import is_ancestor, reduce_update
from .update_runtime_execution import RuntimeExecution
from .update_runtime_plan import PlanContext, RuntimeSeams
from .update_topology import (
    CollisionRow,
    UpdateTopology,
    actionable_reasons,
    parse_git_config_entries,
    parse_local_entries,
    unsafe_config_keys,
)

RESULT_KIND = "existing_install_update"
PREVIEW_KIND = "update_preview"
PLANNED_ACTIONS = (
    "manager.acquire_update_candidate",
    "target.fetch_exact_candidate",
    "target.fast_forward_exact_candidate",
)
STEP4_CAPABILITIES = ("candidate_cache_acquire", "target_fetch_private_ref", "source_fast_forward_only")
NON_TOUCH_SURFACES = (
    "credentials",
    "dependencies_and_venv",
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
_FINGERPRINT = re.compile(r"^sha256:[0-9a-f]{64}$")
_STAGE_IDS = {
    "operation_published": "operation_published",
    "target_fetched": "target_fetch_verified",
    "source_applying": "source_fast_forward",
    "source_advanced": "source_identity_verified",
}
_VERIFY_NOTES = {
    "target_fetched": "verify mode: no fetch; the target already holds the candidate",
    "source_applying": "verify mode: no merge; the exact candidate identity is verified in place",
}
_FLOW_PATH = f"{TRANSITION_BUNDLE_DIRECTORY}/existing_install_flow.json"
_PREVIOUS = {"target_fetched": "operation_published", "source_applying": "target_fetched"}
_STATUS_TRACKED_ARGS = ("status", "--porcelain=v1", "-z", "--untracked-files=no")
REPAIR_RECONCILE_DRY_RUN = "Run `solet-manager reconcile {name} --dry-run` to plan a successor operation."
REPAIR_RECONCILE_ABANDON = "Run `solet-manager reconcile {name} --abandon --yes` to retire the terminal operation; no target byte changed."
REPAIR_DOCTOR = "Inspect the target with `solet-manager doctor {name}`; do not reset it."
REPAIR_RELEASE_POINTER = "Run `solet-manager reconcile {name} --release-pointer --yes`."
#: Step 7 section 6.6 (B7): the running solet's own ``knowledge_bases/`` symlink creation is admitted as a
#: disclosed service write only once the lifecycle stage has restarted the service.

type DescriptorLoader = Callable[[str, InspectionEffectTracker], InstalledUpdateDescriptor]


class _LoaderTracker:
    """Effect tracker for descriptor loading; any forbidden effect fails loud."""

    def __init__(self) -> None:
        self.resources: list[str] = []
        self.vectors: list[tuple[str, ...]] = []

    def record_resource_read(self, resource_id: str) -> None:
        self.resources.append(resource_id)

    def record_probe(self, probe: InspectionProbe, argv: tuple[str, ...]) -> None:
        self.vectors.append(argv)

    def record_forbidden(self, effect: PreservationEffect) -> Never:
        raise InspectionBoundaryViolation(effect.value)

    def snapshot(self) -> InspectionPreservationFacts:
        return InspectionPreservationFacts(0, 0, 0, 0, 0, 0, 0, 0, tuple(self.vectors), tuple(self.resources))


@dataclass(frozen=True, slots=True)
class UpdateRequest:
    """One ``solet-manager update <name>`` invocation.

    ``transport_url`` is a test seam for offline fixtures.  The CLI never sets
    it: the descriptor's canonical repository is the only production source.
    """

    name: str
    manager_paths: ManagerPaths
    descriptor_loader: DescriptorLoader = load_installed_update_descriptor
    transport_url: str | None = None
    runtime_seams: RuntimeSeams | None = None
    #: Step 6 D7: the closed CLI carrier is ``--backup-checkpoint``; a fixture may
    #: also supply ``process_key`` for a platform migration, which the CLI cannot.
    operator_selections: dict[str, JsonValue] = field(default_factory=lambda: {})


@dataclass(frozen=True, slots=True)
class UpdateProbe:
    """Everything a preview or a lock-time revalidation observed."""

    record: InstanceInventoryRecordV2
    descriptor: InstalledUpdateDescriptor
    candidate: UpdateCandidate
    baseline: ExistingInstallInspectionResult
    reasons: tuple[str, ...]
    collisions: tuple[CollisionRow, ...]
    preview: UpdatePreview
    operation_id: str
    journal_path: Path
    source_mode: str = "advance"
    recovers: str | None = None
    #: Step 7: the local-state observation (section 6.3), the paths each blocking reason names, and the host group (section 7.2).
    local_state: ObservedLocalState | None = None
    blocked_paths: dict[str, list[str]] = field(default_factory=lambda: {})
    host: dict[str, JsonValue] = field(default_factory=lambda: {})
    #: iss_f1d8cfc2: the tracked hook manifests proved to carry exactly the installer's interpreter pin.
    installer_pins: tuple[str, ...] = ()

    @property
    def fingerprint(self) -> str | None:
        return self.preview.approval_fingerprint

    @property
    def planned_actions(self) -> tuple[str, ...]:
        return VERIFY_PLANNED_ACTIONS if self.source_mode == "verify" else PLANNED_ACTIONS


def preview_update_instance(request: UpdateRequest) -> CommandResult:
    """Render the closed update preview; may write only the Manager candidate cache."""
    from .update_preview_render import preview_instance  # noqa: PLC0415 - the renderer imports this module's executor

    return preview_instance(request)


def apply_update(request: UpdateRequest, approved_fingerprint: str | None) -> CommandResult:
    """Revalidate the approved preview under the instance lock and advance source once."""
    if approved_fingerprint is None:
        raise ApprovalFingerprintRequiredError("--yes requires --approval-fingerprint")
    if _FINGERPRINT.fullmatch(approved_fingerprint) is None:
        raise ApprovalFingerprintMalformedError("approval fingerprint must be sha256:<64 hex>")
    paths = request.manager_paths
    with instance_lock(paths.lock_path(request.name), create=True):
        # iss_836499b3: a Manager-created instance with no v2 row is enrolled inline, under this lock,
        # only once the approved fingerprint (which binds the enrollment) is proven current.
        from .update_enrollment import enroll_pending, plan_create_origin_enrollment  # noqa: PLC0415 - it imports this module

        pending = plan_create_origin_enrollment(request)
        if pending is not None:
            return _apply_fresh(request, enroll_pending(request, pending, approved_fingerprint), approved_fingerprint)
        record = load_update_record(request)
        # Step 6 section 4.4: a pointer naming a promoted, abandoned, retired, or
        # verified-import journal is released on sight before anything else.
        record = release_terminal_pointer(paths, record) or record
        if record.active_operation is None:
            return _apply_fresh(request, record, approved_fingerprint)
        return _apply_resume(request, record, approved_fingerprint)


def load_update_record(request: UpdateRequest) -> InstanceInventoryRecordV2:
    """Load the exact v2 row for ``name`` and refuse misbound or busy rows."""
    records = read_maintenance_inventory_v2(request.manager_paths.maintenance_inventory_path)
    record = next((item for item in records if item.name == request.name), None)
    if record is None:
        raise InstanceUnmanagedError(
            f"no v2 inventory record is named {request.name!r}",
            repair="Enroll the existing Solet first with `solet-manager import`.",
        )
    if record.channel.descriptor_digest == record.contract_identities.diagnostic_contract_digest:
        raise InventoryChannelDescriptorMisbindingError(
            "inventory channel descriptor digest equals the transition-contract digest",
            repair="Re-enroll with a Manager whose import binds the exact descriptor digest.",
        )
    active = record.active_operation
    if active is not None and active.kind is MaintenanceOperationKind.IMPORT:
        # Review B3: a create-origin row resumes inline (update_enrollment) before reaching here.
        raise OperationInProgressError(
            f"the enrollment of {record.name!r} (import operation {active.operation_id}) was interrupted",
            repair=import_resume_repair(record.name, record.target.canonical_path, record.channel.channel_id),
        )
    if active is not None and active.kind is not MaintenanceOperationKind.UPDATE:
        raise OperationInProgressError(f"{active.kind.value} operation {active.operation_id} is active")
    return record


def compute_update_operation_id(instance_id: str, baseline_commit: str, candidate: UpdateCandidate, *, recovers: str | None = None) -> str:
    """Derive the stable update key and operation id from exact identities only.

    A successor operation (Step 6 section 4.4) names the terminal operation it
    recovers in its immutable input, so it can never collide with the original.
    """
    identities: list[JsonValue] = [
        "update",
        instance_id,
        baseline_commit,
        candidate.fields.commit,
        candidate.descriptor_digest,
        _contract_digest(candidate),
    ]
    if recovers is not None:
        identities.append(f"recovers={recovers}")
    update_key = canonical_sha256(identities)
    return "opr_" + canonical_sha256(["operation", update_key]).removeprefix("sha256:")[:32]


def select_source_mode(record: InstanceInventoryRecordV2, candidate: UpdateCandidate) -> str:
    """Step 6 section 4.8: ``verify`` is selected by state, never by a flag.

    An enrolled or ``needs_attention`` instance that is already source-current
    but not verified gets the zero-delta path; a current AND verified instance
    keeps the landed ``already_current`` result.
    """
    current = record.source_release.commit == candidate.fields.commit
    if current and record.management_state is not ManagementState.VERIFIED:
        return "verify"
    return "advance"


def probe_update(
    request: UpdateRequest,
    record: InstanceInventoryRecordV2,
    *,
    recovers: str | None = None,
    enrollment: dict[str, JsonValue] | None = None,
) -> UpdateProbe:
    """Acquire the exact candidate and observe the target freshly.

    The execution-surface scan runs before any other target probe so a hostile
    ``core.fsmonitor`` or hook cannot be triggered by the inspection itself.
    ``enrollment`` is a pending create-origin enrollment's binding (iss_836499b3); an enrolled row
    derives the identical binding from its durable import operation instead.
    """
    paths = request.manager_paths
    binding = enrollment if enrollment is not None else recorded_enrollment_binding(paths, record)
    target = _target_path(record)
    entries = parse_git_config_entries(_git(target, ("config", "--list", "--show-scope", "-z"), "target configuration is unreadable"))
    unsafe = unsafe_config_keys(entries)
    if unsafe:
        raise UpdateBlockedError(
            "git_execution_surface_unsafe",
            f"target Git configuration can execute target-supplied code: {', '.join(unsafe)}",
            repair="Remove the executable Git configuration from the target, then preview again.",
        )
    host = host_preflight(request.runtime_seams or RuntimeSeams(), target)
    descriptor = request.descriptor_loader(record.channel.channel_id, _LoaderTracker())
    candidate = acquire_update_candidate(paths, descriptor.descriptor_bytes, transport_url=request.transport_url)
    _require_candidate_identity(record, descriptor, candidate)
    baseline = _inspect_baseline(request, record, candidate)
    source_mode = "verify" if recovers is not None else select_source_mode(record, candidate)
    reduction = reduce_update(paths, record, candidate, baseline, entries, source_mode, git=_git, run_git=_run_git, cache_git=_cache_git, cache_run_git=_cache_run_git)
    operation_id = compute_update_operation_id(record.instance_id, record.source_release.commit, candidate, recovers=recovers)
    journal_path = paths.operation_path(record.instance_id, operation_id)
    planned = VERIFY_PLANNED_ACTIONS if source_mode == "verify" else PLANNED_ACTIONS
    actionable = actionable_reasons(reduction.reasons, source_mode)
    preview = preview_update(
        candidate,
        UpdateTopology(reduction.reasons, actionable, None if reduction.local_state is None else reduction.local_state.state),
        baseline_commit=record.source_release.commit,
        bound_identity=_bound_identity(record, candidate, baseline, reduction.collisions, paths, journal_path, planned, recovers, reduction.local_state, binding),
        planned_actions=planned,
        source_mode=source_mode,
    )
    return UpdateProbe(record, descriptor, candidate, baseline, reduction.reasons, reduction.collisions, preview, operation_id, journal_path, source_mode, recovers, reduction.local_state, reduction.blocked_paths, host, reduction.installer_pins)


def enrollment_binding(operation_id: str, approval_fingerprint: str) -> dict[str, JsonValue]:
    """The create-origin enrollment an update approval binds (iss_836499b3)."""
    return {"kind": "create_origin", "operation_id": operation_id, "approval_fingerprint": approval_fingerprint}


def recorded_enrollment_binding(paths: ManagerPaths, record: InstanceInventoryRecordV2) -> dict[str, JsonValue] | None:
    """The binding of the verified import that enrolled a create-origin row, until its first promotion.

    It equals the pending binding the enrolling ``--dry-run`` rendered, so an inline enrollment's
    apply, resume and reconcile all reproduce the approved fingerprint.  Import-origin rows and
    promoted rows bind nothing, so their fingerprints are unchanged.
    """
    if record.management_origin is not ManagementOrigin.CREATE or record.verified_release is not None:
        return None
    operation_id = record.last_verified_operation_id
    if operation_id is None:
        return None
    document = read_maintenance_operation(paths.operation_path(record.instance_id, operation_id))
    approval = cast(dict[str, JsonValue], document["approval"])
    if document["kind"] != "import" or document["status"] != "verified":
        raise StateError("a create-origin row's last verified operation is not its verified import")
    return enrollment_binding(operation_id, cast(str, approval["fingerprint"]))


def _require_candidate_identity(
    record: InstanceInventoryRecordV2, descriptor: InstalledUpdateDescriptor, candidate: UpdateCandidate
) -> None:
    identity = descriptor.metadata.channel_identity
    fields = candidate.fields
    checks = (
        fields.channel_id == record.channel.channel_id,
        candidate.descriptor_digest == identity.descriptor_digest,
        fields.repository == record.channel.canonical_repository,
        _contract_digest(candidate) != candidate.descriptor_digest,
    )
    if not all(checks):
        raise UpdateBlockedError(
            "candidate_identity_mismatch",
            "the installed descriptor does not match the enrolled channel identity",
            repair="Verify the installed Manager formula serves the enrolled channel.",
        )


def _inspect_baseline(
    request: UpdateRequest, record: InstanceInventoryRecordV2, candidate: UpdateCandidate
) -> ExistingInstallInspectionResult:
    """Re-prove the enrolled baseline with the Step-2 identity conjunction."""
    return _inspect(request, record, enrolled_metadata(record, candidate.fields))


def enrolled_metadata(record: InstanceInventoryRecordV2, fields: SeedLockFields) -> InstalledInspectionMetadata:
    """The enrolled release as an inspection contract: the record's identities, the descriptor's seed lock."""
    provenance = record.observed_provenance
    if provenance.condition != "strict" or provenance.provenance_sha256 is None:
        raise UpdateBlockedError(
            "baseline_identity_unproven",
            "the enrolled baseline carries no strict provenance identity to re-prove",
            repair="Legacy-bridge baselines need the reviewed predecessor path.",
        )
    contract = cast(dict[str, object], fields.existing_install_contract)
    release = record.source_release
    identity = ChannelInspectionIdentity(
        record.channel.channel_id,
        record.channel.canonical_repository,
        release.tag or "baseline",
        release.commit,
        release.tree,
        fields.profile,
        _bare(provenance.provenance_sha256),
        provenance.seed_id,
        provenance.origin_id,
        _bare(provenance.manifest_sha256),
        ExistingInstallContractIdentity(
            str(contract["flow_id"]),
            cast(int, contract["flow_schema_version"]),
            record.contract_identities.diagnostic_contract_digest,
        ),
        "maintenance_inventory",
        _bare(record.inspection_bundle_digest),
        "maintenance_inventory",
        _bare(record.channel.descriptor_digest),
        record.channel.descriptor_digest,
        "maintenance_inventory",
        _bare(record.inspection_bundle_digest),
    )
    return InstalledInspectionMetadata(identity, seed_lock_from_fields(fields), ())


def inspect_with_metadata(
    request: UpdateRequest, record: InstanceInventoryRecordV2, metadata: InstalledInspectionMetadata
) -> ExistingInstallInspectionResult:
    """Public Step-2 inspection against a chosen contract, with the target identity re-proved."""
    return _inspect(request, record, metadata)


def _inspect(
    request: UpdateRequest, record: InstanceInventoryRecordV2, metadata: InstalledInspectionMetadata
) -> ExistingInstallInspectionResult:
    result = inspect_existing_install(
        ExistingInstallInspectionRequest(_target_path(record), record.channel.channel_id, request.manager_paths),
        metadata_loader=lambda channel, tracker: metadata,
    )
    _require_target_identity(result, record)
    return result


def _require_target_identity(result: ExistingInstallInspectionResult, record: InstanceInventoryRecordV2) -> None:
    observed = result.target_identity
    target = record.target
    matches = (
        str(observed.canonical_display) == target.canonical_path
        and (observed.target_device, observed.target_inode) == (target.filesystem_identity.device, target.filesystem_identity.inode)
        and (observed.parent_device, observed.parent_inode) == (target.parent_filesystem_identity.device, target.parent_filesystem_identity.inode)
    )
    if not matches:
        raise ManagedIdentityDriftError(
            "target filesystem identity differs from the enrolled record",
            repair="Re-inspect the target; the enrolled path no longer names the same directory.",
        )


def _bound_identity(
    record: InstanceInventoryRecordV2,
    candidate: UpdateCandidate,
    baseline: ExistingInstallInspectionResult,
    collisions: tuple[CollisionRow, ...],
    paths: ManagerPaths,
    journal_path: Path,
    planned_actions: tuple[str, ...] = PLANNED_ACTIONS,
    recovers: str | None = None,
    local_state: ObservedLocalState | None = None,
    enrollment: dict[str, JsonValue] | None = None,
) -> dict[str, JsonValue]:
    identity = _bound_identity_fields(record, candidate, baseline, collisions, paths, journal_path, planned_actions, recovers, local_state)
    if enrollment is not None:
        identity["enrollment"] = enrollment
    return identity


def _bound_identity_fields(
    record: InstanceInventoryRecordV2,
    candidate: UpdateCandidate,
    baseline: ExistingInstallInspectionResult,
    collisions: tuple[CollisionRow, ...],
    paths: ManagerPaths,
    journal_path: Path,
    planned_actions: tuple[str, ...],
    recovers: str | None,
    local_state: ObservedLocalState | None,
) -> dict[str, JsonValue]:
    facts = baseline.facts
    fields = candidate.fields
    provenance = record.observed_provenance
    return {
        "recovers": recovers,
        # Step 7 section 6.3: the tracked digests and the aggregate commitment bind the approval; the preserved surface does not.
        "local_state": None if local_state is None else local_state.state.preimage(),
        "instance": {
            "instance_id": record.instance_id,
            "name": record.name,
            "canonical_target": record.target.canonical_path,
            "filesystem_identity": record.target.filesystem_identity.to_dict(),
            "parent_filesystem_identity": record.target.parent_filesystem_identity.to_dict(),
        },
        "baseline": {
            "commit": record.source_release.commit,
            "tree": record.source_release.tree,
            "branch": facts.branch,
            "origins": list(facts.origins),
            "provenance_sha256": provenance.provenance_sha256,
            "seed_id": provenance.seed_id,
            "origin_id": provenance.origin_id,
            "manifest_sha256": provenance.manifest_sha256,
            "diagnostic_contract_digest": record.contract_identities.diagnostic_contract_digest,
            "current_contract_digest": record.contract_identities.current_contract_digest,
        },
        "candidate": _candidate_dict(candidate),
        "collisions": [[row.reason, row.path] for row in collisions],
        "candidate_ref": candidate.candidate_ref,
        "planned_actions": list(planned_actions),
        "non_touch_surfaces": list(NON_TOUCH_SURFACES),
        "capabilities": list(STEP4_CAPABILITIES),
        "rollback_class": "forward_only_source",
        "manager_paths": {
            "journal": str(journal_path),
            "candidate_repository": str(update_candidate_cache(paths, candidate.descriptor_digest).repository),
            "candidate_receipt": str(update_candidate_cache(paths, candidate.descriptor_digest).receipt),
        },
        "provenance_fields": cast(dict[str, JsonValue], dict(fields.provenance or {})),
    }


def _candidate_dict(candidate: UpdateCandidate) -> dict[str, JsonValue]:
    fields = candidate.fields
    return {
        "channel_id": fields.channel_id,
        "descriptor_digest": candidate.descriptor_digest,
        "repository": fields.repository,
        "tag": fields.release_tag,
        "commit": fields.commit,
        "tree": fields.tree_hash,
        "archive_sha256": fields.archive_sha256,
        "profile": fields.profile,
        "contract_digest": _contract_digest(candidate),
        "receipt_digest": candidate.receipt_digest,
    }


def terminal_repair(name: str, journal: dict[str, JsonValue], head: str) -> str:
    """The exact next command for a terminal ``blocked``/``failed`` journal (Step 6 section 4.1)."""
    if head == cast(dict[str, JsonValue], journal["candidate"])["commit"]:
        return REPAIR_RECONCILE_DRY_RUN.format(name=name)
    if head == cast(dict[str, JsonValue], journal["baseline"])["commit"]:
        return REPAIR_RECONCILE_ABANDON.format(name=name)
    return REPAIR_DOCTOR.format(name=name)


def _candidate_execution(request: UpdateRequest, record: InstanceInventoryRecordV2, journal_path: Path, journal: dict[str, JsonValue], approved: str) -> _Execution:
    """Rebuild the executor from the durable copies first (Step 6 section 4.5), backfilling them once."""
    paths = request.manager_paths
    candidate_row = cast(dict[str, JsonValue], journal["candidate"])
    descriptor_digest = cast(str, candidate_row["descriptor_digest"])
    if descriptor_copy_exists(paths, descriptor_digest):
        descriptor = descriptor_from_copy(paths, descriptor_digest)
    else:
        descriptor = request.descriptor_loader(record.channel.channel_id, _LoaderTracker())
        if descriptor.metadata.channel_identity.descriptor_digest != descriptor_digest:
            raise CandidateCopyMissingError(
                "neither a durable descriptor copy nor the installed descriptor reproduces the journaled candidate",
                repair=f"Reinstall the Manager release that shipped {candidate_row['tag']} or run `solet-manager reconcile {record.name} --dry-run`.",
            )
    candidate = acquire_update_candidate(paths, descriptor.descriptor_bytes, transport_url=request.transport_url)
    if candidate.fields.commit != candidate_row["commit"] or candidate.descriptor_digest != descriptor_digest or candidate.bundle_digest != candidate_row["contract_digest"]:
        raise CandidateCopyMissingError("the reproduced candidate does not match the journaled identity", repair=f"Run `solet-manager reconcile {record.name} --dry-run`.")
    persist_contract_copies(paths, descriptor, candidate)
    return _Execution(request, record, descriptor, candidate, journal_path, journal, approved)


def persist_contract_copies(paths: ManagerPaths, descriptor: InstalledUpdateDescriptor, candidate: UpdateCandidate) -> None:
    """Write (or re-prove) the two durable copies of Step 6 section 3.2; idempotent, refuse-on-mismatch."""
    identity = descriptor.metadata.channel_identity
    if not descriptor_copy_exists(paths, candidate.descriptor_digest):
        write_descriptor_copy(
            paths,
            descriptor_bytes=descriptor.descriptor_bytes,
            anchors_document=anchors_document(descriptor.metadata),
            catalog_path=identity.catalog_resource,
            catalog_sha256=identity.catalog_sha256,
            channel_id=identity.channel_id,
            manager_version=MANAGER_VERSION,
        )
    if not transition_contract_exists(paths, candidate.bundle_digest):
        write_transition_contract(paths, candidate.bundle_digest, candidate.bundle_files)
    elif read_transition_contract(paths, candidate.bundle_digest) != candidate.bundle_files:
        raise TransitionContractMismatchError("the durable transition contract copy differs from the proven candidate bundle")


def _apply_fresh(request: UpdateRequest, record: InstanceInventoryRecordV2, approved: str) -> CommandResult:
    probe = probe_update(request, record)
    if probe.fingerprint is None and "already_current" in probe.reasons and record.management_state is ManagementState.VERIFIED:
        raise ProbeDriftError("the instance is already promoted at the installed channel release", repair="Nothing to apply; already promoted; preview again when a new release is installed.")
    if probe.fingerprint is None or probe.fingerprint != approved:
        raise ProbeDriftError(
            "approved fingerprint does not match the lock-time preview",
            repair="Run --dry-run again and approve the fingerprint it renders.",
        )
    # Commit time, still before any Manager or target write (CH-10/11): an approved preview whose instance
    # interpreter is missing cannot be carried into a runtime no stage could execute.
    require_instance_interpreter(probe.host)
    if probe.journal_path.exists():
        journal = read_update_journal(probe.journal_path)
        _require_journal_binding(journal, probe, approved)
    else:
        # Section 3.2: no journal may exist without its durable copies.
        persist_contract_copies(request.manager_paths, probe.descriptor, probe.candidate)
        journal = _new_journal(probe, approved)
        write_update_journal(probe.journal_path, None, journal)
    execution = _Execution(request, record, probe.descriptor, probe.candidate, probe.journal_path, journal, approved)
    if probe.source_mode == "verify":
        return execution.run_verify()
    return execution.run_from_baseline(probe)


def _apply_resume(request: UpdateRequest, record: InstanceInventoryRecordV2, approved: str) -> CommandResult:
    active = cast(ActiveOperation, record.active_operation)
    journal_path = request.manager_paths.operation_path(record.instance_id, active.operation_id)
    journal = read_update_journal(journal_path)
    approval = cast(dict[str, JsonValue], journal["approval"])
    status = cast(str, journal["status"])
    target = _target_path(record)
    _refuse_terminal(request, record, journal, status, target)
    if status in RUNTIME_STATUSES or status in DOCTOR_STATUSES or (status == "source_advanced" and approval["fingerprint"] != approved):
        return _apply_runtime(request, record, journal_path, journal, approved)
    if approval["fingerprint"] != approved:
        raise ProbeDriftError("approved fingerprint does not match the active update's recorded approval")
    # Step 6 n3: in verify mode baseline == candidate, so the mode is checked
    # BEFORE the HEAD comparison or ``run_from_baseline`` would fetch and merge.
    if journal["source_mode"] == "verify":
        return _candidate_execution(request, record, journal_path, journal, approved).run_verify()
    return _resume_source(request, record, journal_path, journal, approved, target)


def _resume_source(request: UpdateRequest, record: InstanceInventoryRecordV2, journal_path: Path, journal: dict[str, JsonValue], approved: str, target: Path) -> CommandResult:
    """Advance-mode resume below ``source_advanced``: one HEAD read decides baseline, candidate, or drift."""
    head = _head(target)
    status = cast(str, journal["status"])
    if head == cast(dict[str, JsonValue], journal["baseline"])["commit"]:
        _refuse_interrupted_merge(record, status, target, journal)
        probe = probe_update(request, record)
        if probe.fingerprint != approved or probe.operation_id != journal["operation_id"]:
            raise ProbeDriftError("the target no longer reproduces the approved preview", repair="Inspect the target; do not reset it.")
        return _Execution(request, record, probe.descriptor, probe.candidate, journal_path, journal, approved).run_from_baseline(probe)
    if head == cast(dict[str, JsonValue], journal["candidate"])["commit"] and status in {"source_applying", "source_advanced"}:
        return _candidate_execution(request, record, journal_path, journal, approved).run_from_candidate()
    raise ManagedIdentityDriftError("target HEAD is neither the approved baseline nor the approved candidate", repair=REPAIR_DOCTOR.format(name=record.name))


def _refuse_terminal(request: UpdateRequest, record: InstanceInventoryRecordV2, journal: dict[str, JsonValue], status: str, target: Path) -> None:
    if status not in TERMINAL_UPDATE_STATUSES:
        return
    result = cast(dict[str, JsonValue], journal["result"])
    _repair_needs_attention(request, record, journal, cast(str, result["reason_code"]))
    repair = terminal_repair(record.name, journal, _head(target))
    raise UpdateBlockedError(cast(str, result["reason_code"]), f"the active update is terminal ({status}); {repair}", repair=repair)


def _repair_needs_attention(request: UpdateRequest, record: InstanceInventoryRecordV2, journal: dict[str, JsonValue], reason_code: str) -> None:
    """Section 5.4 (D8) on sight: a terminal past the source boundary whose CAS a crash skipped is published now."""
    crossed = any(cast(dict[str, JsonValue], item)["status"] in RUNTIME_STATUSES for item in cast(list[JsonValue], journal["attempts"]))
    if not crossed or record.management_state is ManagementState.NEEDS_ATTENTION:
        return
    paths = request.manager_paths
    with instance_lock(paths.registry_lock_path, create=True):
        publish_needs_attention(paths.maintenance_inventory_path, record, reason_codes=(reason_code,), now=utc_now())


def _refuse_interrupted_merge(record: InstanceInventoryRecordV2, status: str, target: Path, journal: dict[str, JsonValue]) -> None:
    """Step 6 section 4.1 (M4), bounded by Step 7 section 6.6: a partial fast-forward is tracked dirt OUTSIDE the
    preserved set, or a preserved path whose bytes no longer match its recorded digest -- on a real clone the
    preserved modifications are dirty on every resume and are not an interruption."""
    if status != "source_applying":
        return
    preserved = {cast(str, cast(dict[str, JsonValue], row)["path"]): cast(str, cast(dict[str, JsonValue], row)["sha256"]) for row in cast(list[JsonValue], cast(dict[str, JsonValue], cast(dict[str, JsonValue], journal["local_state"])["current"])["preserved_tracked_paths"])}
    dirty = dirty_tracked_paths(target)
    interrupted = sorted(path for path in dirty if path not in preserved or _file_sha256(target / path) != preserved[path])
    if interrupted:
        raise UpdateBlockedError(
            "merge_interrupted",
            f"the tracked tree carries a partial fast-forward at the approved baseline: {', '.join(interrupted)}",
            repair=f"Complete or revert it by hand (`git status`), then `solet-manager update {record.name} --yes --approval-fingerprint <fingerprint>` to retry, or stop: {REPAIR_RECONCILE_ABANDON.format(name=record.name)}",
        )


def _file_sha256(path: Path) -> str | None:
    import hashlib  # noqa: PLC0415 - one digest helper for the resume check

    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None


def dirty_tracked_paths(target: Path) -> tuple[str, ...]:
    """Every tracked path with staged or unstaged dirt, from the closed ``--untracked-files=no`` status vector."""
    return tuple(sorted(path for _, _, path in parse_local_entries(_git(target, _STATUS_TRACKED_ARGS, "target status is unreadable"))))


def tracked_tree_dirty(target: Path) -> bool:
    return bool(dirty_tracked_paths(target))


def _head(target: Path) -> str:
    return _git(target, ("rev-parse", "HEAD"), "target HEAD is unreadable").decode("utf-8", "strict").strip()


def _apply_runtime(request: UpdateRequest, record: InstanceInventoryRecordV2, journal_path: Path, journal: dict[str, JsonValue], approved: str) -> CommandResult:
    """Execute or resume the runtime plan under its own approval (design sections 8-9)."""
    status = cast(str, journal["status"])
    runtime_approval = journal["runtime_approval"]
    if status in RUNTIME_STATUSES or status in DOCTOR_STATUSES:
        if cast(dict[str, JsonValue], runtime_approval)["fingerprint"] != approved:
            raise ProbeDriftError("approved fingerprint does not match the active update's recorded runtime approval", repair="Resume with the recorded runtime fingerprint.")
    elif runtime_approval is not None:
        raise StateError("a source_advanced journal already carries a runtime approval")
    source_fingerprint = cast(str, cast(dict[str, JsonValue], journal["approval"])["fingerprint"])
    execution = _candidate_execution(request, record, journal_path, journal, source_fingerprint)
    execution.verify_reentry()
    if status == "source_advanced":
        # Section 4.1: a crash between the source_advanced journal write and the
        # inventory CAS is closed here too, whichever fingerprint resumes it.
        execution.publish_source_axis()
    return RuntimeExecution(
        request.manager_paths,
        execution.record,
        execution.plan_context("pre_apply"),
        journal_path,
        journal,
        approved,
        execution.verify_reentry,
        request=request,
        source=execution,
    ).run()


def _new_journal(probe: UpdateProbe, approved: str) -> dict[str, JsonValue]:
    fields = probe.candidate.fields
    return create_update_journal(
        operation_id=probe.operation_id,
        instance_id=probe.record.instance_id,
        fingerprint=approved,
        baseline_commit=probe.record.source_release.commit,
        baseline_tree=probe.record.source_release.tree,
        branch=cast(str, probe.baseline.facts.branch),
        candidate_descriptor_digest=probe.candidate.descriptor_digest,
        candidate_commit=fields.commit,
        candidate_tree=fields.tree_hash,
        candidate_tag=cast(str, fields.release_tag),
        candidate_contract_digest=_contract_digest(probe.candidate),
        receipt_digest=probe.candidate.receipt_digest,
        planned_actions=probe.planned_actions,
        source_mode=probe.source_mode,
        recovers=probe.recovers,
        local_state=None if probe.local_state is None else probe.local_state.snapshot(),
    )


def _require_journal_binding(journal: dict[str, JsonValue], probe: UpdateProbe, approved: str) -> None:
    expected = _new_journal(probe, approved)
    for key in ("operation_id", "instance_id", "baseline", "candidate", "planned_actions", "source_mode", "recovers"):
        if journal[key] != expected[key]:
            raise StateError("existing update journal does not bind the approved identities")
    if cast(dict[str, JsonValue], journal["local_state"])["baseline"] != cast(dict[str, JsonValue], expected["local_state"])["baseline"]:
        raise StateError("existing update journal records a different local-state baseline")
    if cast(dict[str, JsonValue], journal["approval"])["fingerprint"] != approved:
        raise StateError("existing update journal records a different approval")
    if journal["status"] != "prepared":
        raise StateConflictError("an orphan update journal is past preparation without an inventory pointer")


@dataclass
class _Execution:
    """Stage-driven apply that continues from whatever the journal already proved."""

    request: UpdateRequest
    record: InstanceInventoryRecordV2
    descriptor: InstalledUpdateDescriptor
    candidate: UpdateCandidate
    journal_path: Path
    journal: dict[str, JsonValue]
    approved: str
    executed: list[str] = field(default_factory=lambda: [])
    #: Step 7 section 6.6: the last observation the commitment was verified equal to (per-entry digests in
    #: memory, never journaled) and the report of the last re-entry check, read by the runtime executor.
    last_observed: ObservedLocalState | None = None
    local_state_report: LocalStateReport | None = None

    @property
    def status(self) -> str:
        return cast(str, self.journal["status"])

    @property
    def operation_id(self) -> str:
        return cast(str, self.journal["operation_id"])

    def run_from_baseline(self, probe: UpdateProbe) -> CommandResult:
        try:
            self._publish_pointer()
            self._fetch()
            self._fast_forward()
            return self._finish()
        except ManagerError as exc:
            self._block(exc)
            raise

    def run_from_candidate(self) -> CommandResult:
        try:
            return self._finish()
        except ManagerError as exc:
            self._block(exc)
            raise

    def run_verify(self) -> CommandResult:
        """Step 6 section 4.8: the zero-delta path publishes the pointer and verifies in place.

        The landed graph is walked nominally (n2) -- ``target_fetched`` and
        ``source_applying`` are recorded with verify-mode notes -- so ``_finish``
        and every later stage stay byte-identical for both modes.
        """
        try:
            self._publish_pointer()
            for status in ("target_fetched", "source_applying"):
                if self.status == _PREVIOUS[status]:
                    self._advance(status, _VERIFY_NOTES[status])
            return self._finish()
        except ManagerError as exc:
            self._block(exc)
            raise

    def verify_reentry(self, journal: dict[str, JsonValue] | None = None) -> ExistingInstallInspectionResult:
        """Step 6 section 4.6 steps 5-6 on top of the landed re-entry invariant."""
        result = self._verify_advanced(journal)
        copy = read_transition_contract(self.request.manager_paths, self.candidate.bundle_digest)
        blob = _git(_target_path(self.record), ("show", f"HEAD:{_FLOW_PATH}"), "the target's committed transition bundle is unreadable")
        if blob != copy["existing_install_flow.json"]:
            raise TransitionContractMismatchError(
                "the target's committed transition bundle differs from the durable contract copy",
                repair=REPAIR_DOCTOR.format(name=self.record.name),
            )
        return result

    def _publish_pointer(self) -> None:
        paths = self.request.manager_paths
        with instance_lock(paths.registry_lock_path, create=True):
            self.record = publish_active_update(paths.maintenance_inventory_path, self.record, self.operation_id, utc_now())
        if self.status == "prepared":
            self._advance("operation_published", "active update pointer published and read back")

    def _fetch(self) -> None:
        target = _target_path(self.record)
        fields = self.candidate.fields
        ref = self.candidate.candidate_ref
        resolved = _run_git(target, ("rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}"))
        if resolved.returncode == 0:
            if resolved.stdout.decode("utf-8", "strict").strip() != fields.commit:
                raise CandidateRefDriftError("the private candidate ref resolves to a different commit")
        else:
            source = fields.repository if self.request.transport_url is None else self.request.transport_url
            _git(target, ("fetch", "--no-tags", "--no-write-fetch-head", source, f"refs/tags/{fields.release_tag}:{ref}"), "candidate fetch failed", hooks_dir=self._hooks_dir())
            self.executed.append("target.fetch_exact_candidate")
        commit = _git(target, ("rev-parse", f"{ref}^{{commit}}"), "candidate ref is unreadable").decode("utf-8", "strict").strip()
        tree = _git(target, ("rev-parse", f"{commit}^{{tree}}"), "candidate tree is unreadable").decode("utf-8", "strict").strip()
        if commit != fields.commit or tree != fields.tree_hash:
            raise CandidateRefDriftError("the fetched candidate object does not match the approved identity")
        if not is_ancestor(_run_git, target, self.record.source_release.commit, commit):
            raise UpdateBlockedError("history_diverged", "the approved baseline is not an ancestor of the candidate inside the target", repair=f"No fast-forward exists; {REPAIR_RECONCILE_ABANDON.format(name=self.record.name)}")
        if self.status == "operation_published":
            self._advance("target_fetched", "private candidate ref verified against the approved commit and tree")

    def _fast_forward(self) -> None:
        fresh = probe_update(self.request, self.record)
        if fresh.fingerprint != self.approved or fresh.operation_id != self.operation_id:
            raise UpdateBlockedError("probe_drift", "the target no longer reproduces the approved preview", repair="Inspect the target; do not reset it.")
        if self.status == "target_fetched":
            self._advance("source_applying", "fast-forward started after lock-time revalidation")
        target = _target_path(self.record)
        completed = _run_git(target, ("merge", "--ff-only", "--no-edit", self.candidate.fields.commit), hooks_dir=self._hooks_dir())
        self.executed.append("target.fast_forward_exact_candidate")
        if completed.returncode:
            raise UpdateBlockedError("fast_forward_refused", f"git refused the fast-forward: {completed.stderr.decode('utf-8', 'replace').strip()}", repair="Inspect the target; no destructive recovery is attempted.")

    def _finish(self) -> CommandResult:
        verified = self._verify_advanced()
        if self.status == "source_applying":
            self._advance("source_advanced", "exact candidate release identity verified after fast-forward")
        self._publish_advance()
        self._read_back()
        return self._result(verified)

    def _verify_advanced(self, journal: dict[str, JsonValue] | None = None) -> ExistingInstallInspectionResult:
        """The post-merge and re-entry postcondition (Step 7 section 6.6): exact identity plus the local-state commitment.

        The CLEAN conjunct is gone; in its place the recomputed local state must
        equal ``journal.local_state.current`` -- the per-operation baseline, never
        the source-boundary one.  ``journal`` is the executor's live document when
        it has moved past this object's own copy.
        """
        result = _inspect(self.request, self.record, self.descriptor.metadata)
        facts = result.facts
        fields = self.candidate.fields
        current_journal = self.journal if journal is None else journal
        branch = cast(dict[str, JsonValue], current_journal["baseline"])["branch"]
        exact = (
            facts.head_commit == fields.commit
            and facts.head_tree == fields.tree_hash
            and facts.identity_status is InspectionStatus.VERIFIED
            and facts.anchor_kind is InspectionAnchorKind.CURRENT_CHANNEL
            and facts.detached is ObservedBoolean.FALSE
            and facts.branch == branch
            and facts.origins == (self.record.channel.canonical_repository,)
        )
        if not exact:
            raise SourceTransitionIncompleteError(
                "target is not at the exact candidate release identity",
                repair="Do not reset; inspect the target and resume once its bytes match the approved candidate.",
            )
        self.local_state_report = self._local_state_matches(result, current_journal)
        return result

    def _local_state_matches(self, result: ExistingInstallInspectionResult, journal: dict[str, JsonValue]) -> LocalStateReport:
        report = verify_local_state(_target_path(self.record), result.facts, journal, self.last_observed)
        if not report.pending_rebaseline:
            self.last_observed = report.observed
        return report

    def rebaseline_local_state(self, journal: dict[str, JsonValue], operation_id: str, declared: frozenset[str]) -> tuple[dict[str, JsonValue], dict[str, JsonValue]] | None:
        """Section 6.6, the per-operation carve-out (see :func:`rebaseline_revision`); re-inspects the target first."""
        result = _inspect(self.request, self.record, self.descriptor.metadata)
        observed = observe_local_state(_target_path(self.record), result.facts)
        revision = rebaseline_revision(journal, observed, self.last_observed, operation_id, declared, self.record.name)
        self.last_observed = observed
        return revision

    def publish_source_axis(self) -> None:
        """Idempotent source-axis CAS plus read-back, for a resume that arrives past the journal write."""
        self._publish_advance()
        self._read_back()

    def _publish_advance(self) -> None:
        fields = self.candidate.fields
        identity = self.descriptor.metadata.channel_identity
        paths = self.request.manager_paths
        with instance_lock(paths.registry_lock_path, create=True):
            self.record = publish_source_advance(
                paths.maintenance_inventory_path,
                self.record,
                operation_id=self.operation_id,
                source_release=ReleaseIdentity(fields.repository, fields.commit, fields.tree_hash, fields.release_tag),
                source_contract_digest=_contract_digest(self.candidate),
                channel=ChannelIdentity(cast(str, fields.channel_id), self.candidate.descriptor_digest, fields.repository),
                observed_provenance=ObservedProvenanceIdentity("strict", _prefixed(identity.provenance_sha256), identity.seed_id, identity.origin_id, _prefixed(identity.manifest_sha256), None),
                now=utc_now(),
            )

    def _read_back(self) -> None:
        if read_update_journal(self.journal_path) != self.journal:
            raise StateError("update journal read-back mismatch")
        records = read_maintenance_inventory_v2(self.request.manager_paths.maintenance_inventory_path)
        current = next((item for item in records if item.instance_id == self.record.instance_id), None)
        pointer = ActiveOperation(MaintenanceOperationKind.UPDATE, self.operation_id)
        if current is None or current != self.record or current.active_operation != pointer:
            raise StateError("maintenance inventory does not cross-link the advanced update")

    def _result(self, verified: ExistingInstallInspectionResult) -> CommandResult:
        record = self.record
        return CommandResult(
            RESULT_KIND,
            "source_advanced",
            "Source at the exact candidate; the update remains active pending the runtime plan, final doctor, and promotion.",
            ExitCode.OK,
            data={
                "instance_id": record.instance_id,
                "operation_id": self.operation_id,
                "journal_status": self.status,
                "management_state": record.management_state.value,
                "update_eligibility": {"state": record.update_eligibility.state.value, "reason_codes": list(record.update_eligibility.reason_codes)},
                "source_release": _release_dict(record.source_release),
                "runtime_release": _release_dict(record.runtime_release),
                "verified_release": _release_dict(record.verified_release),
                "channel": {"channel_id": record.channel.channel_id, "descriptor_digest": record.channel.descriptor_digest, "canonical_repository": record.channel.canonical_repository},
                "source_contract_digest": record.contract_identities.source_contract_digest,
                "runtime_contract_digest": record.contract_identities.runtime_contract_digest,
                "verified_contract_digest": record.contract_identities.verified_contract_digest,
                "branch": verified.facts.branch,
                "candidate_ref": self.candidate.candidate_ref,
                "target_actions_executed": list(self.executed),
                "planned_actions": list(cast(list[str], self.journal["planned_actions"])),
                "source_mode": self.journal["source_mode"],
                "target_byte_writes": len(self.executed),
                "local_state": None if self.local_state_report is None else self.local_state_report.to_dict(),
            },
        )

    def _advance(self, status: str, note: str) -> None:
        next_value = advance_update_journal(self.journal, status=status, stage_id=_STAGE_IDS[status], note=note)
        write_update_journal(self.journal_path, self.journal, next_value)
        self.journal = next_value

    def plan_context(self, probe_purpose: str) -> PlanContext:
        """Assemble the Step-5 plan context from the journal's exact identities."""
        record = self.record
        journal_baseline = cast(dict[str, JsonValue], self.journal["baseline"])
        seams = self.request.runtime_seams or RuntimeSeams()
        registry = ExistingInstallAdapterRegistry(
            record.name,
            _target_path(record),
            Path(record.service_identity.bridge_cli_path),
            base_python_or_none(seams),
        )
        return PlanContext(
            record,
            self.candidate,
            self.operation_id,
            cast(str, cast(dict[str, JsonValue], self.journal["approval"])["fingerprint"]),
            cast(str, journal_baseline["commit"]),
            cast(str, journal_baseline["tree"]),
            registry,
            update_candidate_cache(self.request.manager_paths, self.candidate.descriptor_digest).repository,
            seams,
            probe_purpose,
            dict(self.request.operator_selections),
        )

    def _block(self, exc: ManagerError) -> None:
        if self.status in TERMINAL_UPDATE_STATUSES or self.status in FRONTIER_STATUSES:
            return
        result: dict[str, JsonValue] = {"kind": "blocked", "reason_code": exc.error_kind, "repair": exc.repair or REPAIR_RECONCILE_ABANDON.format(name=self.record.name)}
        next_value = advance_update_journal(self.journal, status="blocked", stage_id="blocked", note=str(exc)[:200], result=result)
        write_update_journal(self.journal_path, self.journal, next_value)
        self.journal = next_value

    def _hooks_dir(self) -> Path:
        path = update_candidate_cache(self.request.manager_paths, self.candidate.descriptor_digest).no_hooks
        ensure_private_directory(path)
        return path


def _run_git(cwd: Path, args: tuple[str, ...], *, hooks_dir: Path | None = None) -> subprocess.CompletedProcess[bytes]:
    """Run one closed Git vector through the shared hardened surface; ``hooks_dir`` marks the two approved target writes."""
    return run_target_git(("-c", "advice.diverging=false", *args), cwd=cwd, read_only=hooks_dir is None, hooks_dir=hooks_dir)


def _cache_run_git(repository: Path, args: tuple[str, ...]) -> subprocess.CompletedProcess[bytes]:
    """A read of the Manager-owned bare candidate cache, pinned as bare (R2-1)."""
    return run_target_git(args, cwd=repository, layout=GitLayout.BARE)


def _cache_git(repository: Path, args: tuple[str, ...], error: str) -> bytes:
    completed = _cache_run_git(repository, args)
    if completed.returncode:
        raise SourceError(f"{error}: {completed.stderr.decode('utf-8', 'replace').strip()}")
    return completed.stdout


def _git(cwd: Path, args: tuple[str, ...], error: str, *, hooks_dir: Path | None = None) -> bytes:
    completed = _run_git(cwd, args, hooks_dir=hooks_dir)
    if completed.returncode:
        raise SourceError(f"{error}: {completed.stderr.decode('utf-8', 'replace').strip()}")
    return completed.stdout


def git_read(cwd: Path, args: tuple[str, ...], error: str) -> bytes:
    """One closed read-only Git vector for callers outside this module (the doctor, reconcile)."""
    return _git(cwd, args, error)


def target_head(record: InstanceInventoryRecordV2) -> str:
    return _head(_target_path(record))


def _target_path(record: InstanceInventoryRecordV2) -> Path:
    return Path(record.target.canonical_path)


def _contract_digest(candidate: UpdateCandidate) -> str:
    return str(cast(dict[str, object], candidate.fields.existing_install_contract)["bundle_digest"])


def _bare(digest: str) -> str:
    return digest.removeprefix("sha256:")


def _prefixed(digest: str) -> str:
    return digest if digest.startswith("sha256:") else "sha256:" + digest


def _release_dict(release: ReleaseIdentity | None) -> dict[str, JsonValue] | None:
    if release is None:
        return None
    return {"repository": release.repository, "commit": release.commit, "tree": release.tree, "tag": release.tag}
