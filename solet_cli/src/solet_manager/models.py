"""Typed public and durable manager records."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import IntEnum, StrEnum
from typing import Self

MANAGER_VERSION = "0.1.0"
SCHEMA_VERSION = 1

type JsonValue = None | bool | int | float | str | list[JsonValue] | dict[str, JsonValue]


class ExitCode(IntEnum):
    """Stable CLI exit contract."""

    OK = 0
    FAILED = 1
    INVALID = 2
    HUMAN_ACTION = 3


class CheckpointStatus(StrEnum):
    """The complete canonical setup-flow checkpoint vocabulary."""

    PENDING = "pending"
    AWAITING_USER = "awaiting_user"
    CONSENTED = "consented"
    APPLYING = "applying"
    APPLIED = "applied"
    VERIFIED = "verified"
    DECLINED = "declined"
    BLOCKED = "blocked"
    FAILED = "failed"
    NOT_APPLICABLE = "not_applicable"


class TransactionStatus(StrEnum):
    """Deterministic transaction-level roll-up vocabulary."""

    PENDING = "pending"
    AWAITING_USER = "awaiting_user"
    APPLYING = "applying"
    BLOCKED = "blocked"
    FAILED = "failed"
    VERIFIED = "verified"


class ManagementOrigin(StrEnum):
    """Closed ownership vocabulary for the maintenance inventory."""

    CREATE = "create"
    IMPORT = "import"


class ManagementState(StrEnum):
    """Steady-state maintenance condition; active work is a separate axis."""

    DIAGNOSTIC = "diagnostic"
    VERIFIED = "verified"
    NEEDS_ATTENTION = "needs_attention"


class UpdateEligibilityState(StrEnum):
    CURRENT = "current"
    AVAILABLE = "available"
    BLOCKED = "blocked"


class MaintenanceOperationKind(StrEnum):
    IMPORT = "import"
    UPDATE = "update"
    DOCTOR = "doctor"


class DoctorContractKind(StrEnum):
    """The three contracts ``solet-manager doctor`` can select (Step 6 design section 3.1)."""

    CANDIDATE = "candidate"
    VERIFIED = "verified"
    DIAGNOSTIC = "diagnostic"


class OperationType(StrEnum):
    """Closed resume-rule type of one runtime operation (Step 6 design section 4.2).

    Every declared row is classified from its closed fields at parse time and
    every Manager-synthesised row is typed at synthesis; the executor dispatches
    on this value and on nothing else.
    """

    CLOSURE_REPAIR = "closure_repair"  # T1
    BACKED_UP_ARTIFACT = "backed_up_artifact"  # T2
    MANUAL_TARGET_MIGRATION = "manual_target_migration"  # T3
    MANUAL_ADDITIVE_PLATFORM_MIGRATION = "manual_additive_platform_migration"  # T3b
    PROCESS_LIFECYCLE = "process_lifecycle"  # T4
    READINESS = "readiness"  # T5
    ADDITIVE_PLATFORM_MIGRATION = "additive_platform_migration"  # T6
    FORWARD_ONLY_MIGRATION = "forward_only_migration"  # T7
    KNOWLEDGE_REINSTALL = "knowledge_reinstall"  # T8
    PLUGIN_CACHE_REFRESH = "plugin_cache_refresh"  # T9


class MaintenanceOperationStatus(StrEnum):
    PREPARED = "prepared"
    BUNDLE_CACHED = "bundle_cached"
    INVENTORY_PUBLISHED = "inventory_published"
    VERIFIED = "verified"
    BLOCKED = "blocked"
    FAILED = "failed"
    ABANDONED = "abandoned"


class MaintenanceStageStatus(StrEnum):
    PENDING = "pending"
    APPLYING = "applying"
    VERIFIED = "verified"
    BLOCKED = "blocked"
    FAILED = "failed"


@dataclass(frozen=True)
class Evidence:
    """Non-secret structured observation returned by a probe or adapter."""

    id: str
    kind: str
    status: str
    summary: str
    observed: JsonValue
    expected: JsonValue
    source: str
    digest: str
    captured_at: str
    sensitivity: str = "public"

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "id": self.id,
            "kind": self.kind,
            "status": self.status,
            "summary": self.summary,
            "observed": self.observed,
            "expected": self.expected,
            "source": self.source,
            "digest": self.digest,
            "captured_at": self.captured_at,
            "sensitivity": self.sensitivity,
        }

    @classmethod
    def from_dict(cls, value: dict[str, JsonValue]) -> Self:
        return cls(
            id=str(value["id"]),
            kind=str(value["kind"]),
            status=str(value["status"]),
            summary=str(value["summary"]),
            observed=value.get("observed"),
            expected=value.get("expected"),
            source=str(value["source"]),
            digest=str(value["digest"]),
            captured_at=str(value["captured_at"]),
            sensitivity=str(value.get("sensitivity", "public")),
        )


@dataclass(frozen=True)
class CommandResult:
    """Single semantic result consumed by both human and JSON renderers."""

    kind: str
    status: str
    message: str
    exit_code: ExitCode
    error_kind: str | None = None
    repair: str | None = None
    data: dict[str, JsonValue] = field(default_factory=dict[str, JsonValue])
    evidence: tuple[Evidence, ...] = ()

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "schema_version": SCHEMA_VERSION,
            "kind": self.kind,
            "status": self.status,
            "message": self.message,
            "exit_code": int(self.exit_code),
            "error_kind": self.error_kind,
            "repair": self.repair,
            "data": self.data,
            "evidence": [item.to_dict() for item in self.evidence],
        }


@dataclass(frozen=True)
class InstanceRecord:
    """Non-secret manager registry entry."""

    name: str
    target: str
    launcher: str
    seed_repository: str
    seed_tag: str | None
    seed_commit: str
    seed_tree_hash: str
    profile: str
    flow_id: str
    flow_source_revision: str
    flow_contract_digest: str
    created_at: str
    updated_at: str
    lifecycle_state: str = "verified"
    input_fingerprint: str = ""
    expected_router_name: str | None = None
    expected_router_socket: str | None = None
    expected_router_port_range: str | None = None

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "name": self.name,
            "target": self.target,
            "launcher": self.launcher,
            "seed_repository": self.seed_repository,
            "seed_tag": self.seed_tag,
            "seed_commit": self.seed_commit,
            "seed_tree_hash": self.seed_tree_hash,
            "profile": self.profile,
            "flow_id": self.flow_id,
            "flow_source_revision": self.flow_source_revision,
            "flow_contract_digest": self.flow_contract_digest,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "lifecycle_state": self.lifecycle_state,
            "input_fingerprint": self.input_fingerprint,
            "expected_router_name": self.expected_router_name,
            "expected_router_socket": self.expected_router_socket,
            "expected_router_port_range": self.expected_router_port_range,
        }

    @classmethod
    def from_dict(cls, value: dict[str, JsonValue]) -> Self:
        seed_tag = value["seed_tag"]
        if seed_tag is not None and not isinstance(seed_tag, str):
            raise TypeError("seed_tag must be a string or null")
        lifecycle_state = _lifecycle_state(value.get("lifecycle_state", "verified"))
        input_fingerprint = _string_or_empty(value.get("input_fingerprint", ""))
        return cls(
            name=str(value["name"]),
            target=str(value["target"]),
            launcher=str(value["launcher"]),
            seed_repository=str(value["seed_repository"]),
            seed_tag=seed_tag,
            seed_commit=str(value["seed_commit"]),
            seed_tree_hash=str(value["seed_tree_hash"]),
            profile=str(value["profile"]),
            flow_id=str(value["flow_id"]),
            flow_source_revision=str(value["flow_source_revision"]),
            flow_contract_digest=str(value["flow_contract_digest"]),
            created_at=str(value["created_at"]),
            updated_at=str(value["updated_at"]),
            lifecycle_state=lifecycle_state,
            input_fingerprint=input_fingerprint,
            expected_router_name=_optional_string(value.get("expected_router_name")),
            expected_router_socket=_optional_string(value.get("expected_router_socket")),
            expected_router_port_range=_optional_string(value.get("expected_router_port_range")),
        )


@dataclass(frozen=True, slots=True)
class FilesystemIdentity:
    device: int
    inode: int

    def to_dict(self) -> dict[str, JsonValue]:
        return {"device": self.device, "inode": self.inode}


@dataclass(frozen=True, slots=True)
class TargetIdentity:
    canonical_path: str
    filesystem_identity: FilesystemIdentity
    parent_filesystem_identity: FilesystemIdentity


@dataclass(frozen=True, slots=True)
class ServiceIdentity:
    service_cli_path: str
    bridge_cli_path: str
    named_launcher_path: str
    named_launcher_target: str | None
    profile_id: str | None
    app_home: str | None
    launchagent_label: str
    router_label: str | None
    router_socket: str | None


@dataclass(frozen=True, slots=True)
class ChannelIdentity:
    channel_id: str
    descriptor_digest: str
    canonical_repository: str


@dataclass(frozen=True, slots=True)
class ObservedProvenanceIdentity:
    condition: str
    provenance_sha256: str | None
    seed_id: str
    origin_id: str
    manifest_sha256: str
    anchor_id: str | None


@dataclass(frozen=True, slots=True)
class ReleaseIdentity:
    repository: str
    commit: str
    tree: str
    tag: str | None


@dataclass(frozen=True, slots=True)
class ContractIdentities:
    diagnostic_contract_digest: str
    current_contract_digest: str | None
    source_contract_digest: str | None
    runtime_contract_digest: str | None
    verified_contract_digest: str | None


@dataclass(frozen=True, slots=True)
class UpdateEligibility:
    state: UpdateEligibilityState
    reason_codes: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ActiveOperation:
    kind: MaintenanceOperationKind
    operation_id: str


@dataclass(frozen=True, slots=True)
class InstanceInventoryRecordV2:
    instance_id: str
    name: str
    target: TargetIdentity
    management_origin: ManagementOrigin
    management_state: ManagementState
    update_eligibility: UpdateEligibility
    service_identity: ServiceIdentity
    channel: ChannelIdentity
    observed_provenance: ObservedProvenanceIdentity
    source_release: ReleaseIdentity
    runtime_release: ReleaseIdentity | None
    verified_release: ReleaseIdentity | None
    contract_identities: ContractIdentities
    inspection_bundle_digest: str
    active_operation: ActiveOperation | None
    last_verified_operation_id: str | None
    created_at: str
    updated_at: str
    last_inspected_at: str
    last_verified_at: str | None


@dataclass(frozen=True, slots=True)
class MaintenanceOperationInput:
    name: str
    canonical_target: str
    target_filesystem_identity: FilesystemIdentity
    channel_id: str


@dataclass(frozen=True, slots=True)
class MaintenanceCurrentIdentity:
    provenance_seed_id: str
    head_commit: str
    head_tree: str
    inspection_bundle_digest: str


@dataclass(frozen=True, slots=True)
class MaintenanceApproval:
    fingerprint: str
    recorded_at: str


@dataclass(frozen=True, slots=True)
class MaintenancePreservationInventory:
    non_touch_surfaces: tuple[str, ...]
    target_byte_writes: int
    manager_state_writes: int
    manager_write_paths: tuple[str, ...]
    secret_value_reads: int
    secret_value_writes: int
    database_reads: int
    database_writes: int
    target_process_executions: int
    permission_prompts: int
    invoked_vectors: tuple[tuple[str, ...], ...]
    opened_resources: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class MaintenanceOperationAttempt:
    attempt: int
    stage_id: str
    stage_key: str
    status: MaintenanceStageStatus
    started_at: str
    finished_at: str | None
    evidence: tuple[dict[str, JsonValue], ...]
    error_kind: str | None


@dataclass(frozen=True, slots=True)
class MaintenanceOperationResult:
    kind: str
    inventory_instance_id: str | None = None
    reason_code: str | None = None
    repair: str | None = None


@dataclass(frozen=True, slots=True)
class MaintenanceOperation:
    operation_id: str
    instance_id: str
    kind: MaintenanceOperationKind
    idempotency_key: str
    status: MaintenanceOperationStatus
    input: MaintenanceOperationInput
    current_identity: MaintenanceCurrentIdentity
    candidate_identity: None
    contract_digests: dict[str, str | None]
    approval: MaintenanceApproval
    stage_statuses: dict[str, MaintenanceStageStatus]
    attempts: tuple[MaintenanceOperationAttempt, ...]
    evidence: tuple[dict[str, JsonValue], ...]
    preservation_inventory: MaintenancePreservationInventory
    rollback_class: str
    result: MaintenanceOperationResult | None
    created_at: str
    updated_at: str


@dataclass(frozen=True, slots=True)
class DoctorRun:
    """One appended run of the closed doctor journal (Step 6 design section 3.5)."""

    doctor_operation_id: str
    run: int
    contract: DoctorContractKind
    status: str
    exit_code: ExitCode
    evidence_digest: str
    head_observed: str | None
    sections: tuple[dict[str, JsonValue], ...]
    counts: dict[str, JsonValue]
    preservation: dict[str, JsonValue]
    service_check_verified: bool


# --- Step-5 runtime plan records (design sections 4-8) ---------------------


@dataclass(frozen=True, slots=True)
class DeclaredClosurePiece:
    """One editable distribution the target environment must carry."""

    distribution: str
    relative_path: str
    origin: str  # required | roster_plugin | release_addition


@dataclass(frozen=True, slots=True)
class RuntimeOperationPlan:
    """One release-declared or Manager-synthesised runtime operation after probing."""

    operation_id: str
    operation_ref: str
    runner: str
    stage: str
    mutation_class: str
    rollback_class: str
    retry_policy: str
    idempotency_key: str
    applies: bool
    postcondition_now: str
    planned_actions: tuple[str, ...]
    planned_targets: tuple[str, ...]
    public_inputs: dict[str, JsonValue]
    requires_confirmation: bool
    backup_checkpoint_id: str | None = None
    #: The adapter's own repair text when its probe blocked or failed.  Display only: a blocked plan is never approved or journaled, so it is not in the fingerprint.
    blocked_repair: str | None = None


@dataclass(frozen=True, slots=True)
class ManagedArtifactState:
    """The probed three-way state of one declared managed artifact (section 6.2)."""

    artifact_id: str
    kind: str
    destination: str
    state: str
    action: str
    stamped_digest: str | None
    template_digest: str
    expected_sha256: str | None
    current_sha256: str | None
    conflict: str | None
    operation_id: str
    adopt_diff: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class LifecycleObservation:
    """The lifecycle strategy the plan fixed and the observations that back it (section 7)."""

    strategy: str
    launch_topology: str | None
    launchagent_label: str
    plist_path: str
    plist_expected_sha256: str | None
    current_release_id: str | None
    adapter_module_sha256: str | None
    adapter_module_replaced: bool
    verification_modules: tuple[str, ...]
    readiness_budget_seconds: int
    cutover_fingerprint: str | None
    zero_downtime_rollback: bool
    attestation: dict[str, JsonValue] | None
    cutover_probe_receipt: dict[str, JsonValue] | None
    unproven_reason: str | None
    #: Step 7 section 7.3: the single-colour service observation (``loaded``, ``pid``, ``health``) taken
    #: before the strategy was fixed; ``None`` for a router cutover.
    pre_transition: dict[str, JsonValue] | None = None


@dataclass(frozen=True, slots=True)
class RuntimePlan:
    """The closed, probed runtime plan the second approval binds (section 8)."""

    operation_id: str
    instance_id: str
    source_commit: str
    source_tree: str
    source_tag: str | None
    runtime_release_commit: str | None
    runtime_contract_digest: str | None
    declared_closure: tuple[DeclaredClosurePiece, ...]
    operations: tuple[RuntimeOperationPlan, ...]
    managed_artifacts: tuple[ManagedArtifactState, ...]
    lifecycle: LifecycleObservation
    forward_only_boundary: str | None
    ignore_sources_digest: str | None
    target_process_executions: int
    blocked: tuple[tuple[str, str], ...]
    fingerprint: str | None
    operator_selections: dict[str, JsonValue]
    knowledge_removed_articles: tuple[tuple[str, str, str], ...] = ()

    @property
    def actionable(self) -> bool:
        return not self.blocked and self.lifecycle.unproven_reason is None


def _optional_string(value: JsonValue) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise TypeError("optional instance-record field must be a string or null")
    return value


def _string_or_empty(value: JsonValue) -> str:
    if not isinstance(value, str):
        raise TypeError("instance-record fingerprint must be a string")
    return value


def _lifecycle_state(value: JsonValue) -> str:
    if not isinstance(value, str) or value not in {"setup_incomplete", "verified"}:
        raise TypeError("instance-record lifecycle_state must be setup_incomplete or verified")
    return value
