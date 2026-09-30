"""Stable manager error taxonomy."""

from __future__ import annotations

from .models import CheckpointStatus, JsonValue


class ManagerError(RuntimeError):
    """Base class for failures with a stable public error kind."""

    error_kind = "manager_error"
    exit_code = 1

    def __init__(self, message: str, *, repair: str | None = None) -> None:
        super().__init__(message)
        self.repair = repair


class InvocationError(ManagerError):
    """Invalid command or closed configuration."""

    error_kind = "invalid_invocation"
    exit_code = 2


class ConfigError(InvocationError):
    """Configuration could not be parsed or validated."""

    error_kind = "invalid_config"


class ApprovalFingerprintRequiredError(InvocationError):
    """The --yes/fingerprint approval carrier pair is incomplete."""

    error_kind = "approval_fingerprint_required"


class ApprovalFingerprintMalformedError(InvocationError):
    """The supplied approval fingerprint does not use the closed syntax."""

    error_kind = "approval_fingerprint_malformed"


class ContractError(InvocationError):
    """A shared flow or protocol contract is invalid."""

    error_kind = "invalid_contract"


class StateError(ManagerError):
    """Durable state is unsafe or inconsistent."""

    error_kind = "corrupt_state"


class OperationAttemptMismatch(StateError):  # noqa: N818 - contract name is pinned.
    """One operation status disagrees with its latest valid attempt record."""

    def __init__(
        self,
        *,
        operation: str,
        current: CheckpointStatus,
        latest: dict[str, JsonValue],
    ) -> None:
        super().__init__(f"operation status disagrees with latest attempt: {operation!r}")
        self.operation = operation
        self.current = current
        self.latest = latest


class StateConflictError(ManagerError):
    """Existing durable identity conflicts with the requested operation."""

    error_kind = "state_conflict"
    exit_code = 3


class RegistryUniquenessError(StateConflictError):
    error_kind = "registry_uniqueness_violation"


class ManagedIdentityDriftError(StateConflictError):
    error_kind = "managed_identity_drift"


class ImportNotAllowedError(ManagerError):
    """The inspection classification refuses import; the repair is the inspection's own way out."""

    error_kind = "import_not_allowed"


class OperationInProgressError(StateConflictError):
    error_kind = "operation_in_progress"


class ReopenUnsafeAppliedStateError(StateConflictError):
    """A new active probe cannot safely reopen already-applied work."""

    error_kind = "reopen_unsafe_applied_state"


class HostPlatformUnknownError(StateConflictError):
    """A host-conditioned setup choice cannot be made because this Mac could not be measured."""

    error_kind = "host_platform_unknown"


class VenvIncompatibleError(StateConflictError):
    """The target venv cannot import authenticated locked-seed code."""

    error_kind = "venv_incompatible"


class InventoryChannelDescriptorMisbindingError(StateConflictError):
    """A v2 row still carries the transition-contract digest as its descriptor."""

    error_kind = "inventory_channel_descriptor_misbinding"


class CandidateRefDriftError(StateConflictError):
    """The private candidate ref resolves to something other than the approved commit."""

    error_kind = "candidate_ref_drift"


class SourceTransitionIncompleteError(StateConflictError):
    """HEAD, index, worktree, or provenance sit between exact baseline and exact candidate."""

    error_kind = "source_transition_incomplete"


class UpdateBlockedError(StateConflictError):
    """An approved update stopped at a closed Step-4/Step-5 reason before or after target writes."""

    def __init__(self, reason_code: str, message: str, *, repair: str | None = None) -> None:
        super().__init__(message, repair=repair)
        self.error_kind = reason_code


class HostRequirementError(UpdateBlockedError):
    """A host requirement no runtime stage can repair is missing or unknown before the source boundary (Step 7 section 7.2)."""

    def __init__(self, reason_code: str, message: str, *, repair: str | None = None, host: dict[str, JsonValue] | None = None) -> None:
        super().__init__(reason_code, message, repair=repair)
        self.host: dict[str, JsonValue] = {} if host is None else host


class UpdateFailedError(ManagerError):
    """A Step-5 stage executed and then contradicted itself; the journal goes terminal ``failed``.

    ``failed`` is distinct from ``blocked``: an apply ran and either lied to its
    own postcondition probe or the runtime candidate did not take.  The exit
    code is 1 because a target mutation already happened.
    """

    exit_code = 1

    def __init__(self, reason_code: str, message: str, *, repair: str | None = None) -> None:
        super().__init__(message, repair=repair)
        self.error_kind = reason_code


class TransitionContractMismatchError(StateConflictError):
    """The candidate commit's transition bundle does not digest to the seed-lock declaration."""

    error_kind = "transition_contract_mismatch"


class RestoreTargetDivergedError(StateConflictError):
    """A managed-artifact destination no longer holds the journaled post-write bytes."""

    error_kind = "restore_target_diverged"


class BackupUnwritableError(StateError):
    """The operation-scoped backup could not be written before a managed-file write."""

    error_kind = "backup_unwritable"


class OperationTypeAmbiguousError(ContractError):
    """A declared runtime operation matches no closed resume-rule type (Step 6 section 4.2)."""

    error_kind = "operation_type_ambiguous"


class CandidateCopyMissingError(StateConflictError):
    """Neither the durable descriptor copy nor the installed descriptor reproduces the journaled candidate."""

    error_kind = "candidate_copy_missing"


class InstanceUnmanagedV2Error(ManagerError):
    """The name is a v1 create instance; the v2 doctor does not apply to it."""

    error_kind = "instance_unmanaged_v2"
    exit_code = 3


class AbandonRefusedError(StateConflictError):
    """The journal is past the boundary at which abandon or retirement is legal."""

    error_kind = "abandon_refused"


class BackupMissingError(StateConflictError):
    """A backed-up artifact row re-entered ``applying`` without its operation-scoped backup record."""

    error_kind = "backup_missing"


# Every stable Step 7 reason code the design names (sections 6-7), so a test can prove the set is closed.
# The Step-4 "tracked state present" reason is retired: no landed path produces it after section 6.2.
STEP7_REASON_CODES = (
    "executed_code_modified",
    "git_metadata_present",
    "host_requirement_missing",
    "host_requirement_unknown",
    "hydration_block_uncarriable",
    "instance_requirement_missing",
    "local_state_unobserved",
    "preservation_violated",
    "preserved_surface_in_transition",
    "service_offline_before_transition",
    "staged_changes_present",
    "tracked_overlap_present",
    "tracked_shape_changed",
)


# Every stable Step 6 reason code the design names (sections 3-5), so a test can prove the set is closed.
STEP6_REASON_CODES = (
    "abandon_refused",
    "backup_missing",
    "candidate_copy_missing",
    "doctor_interrupted",
    "forward_only_migration_incomplete",
    "instance_unmanaged_v2",
    "knowledge_removal_not_applied",
    "merge_interrupted",
    "migration_postcondition_contradiction",
    "operation_type_ambiguous",
    "pointer_release_pending",
    "promotion_interrupted",
)


# Every stable Step-5 reason code the design names, so a test can prove the set is closed.
STEP5_REASON_CODES = (
    "adapter_missing",
    "backup_unwritable",
    "coding_agent_running",
    "corrupt_state",
    "dependency_postcondition_contradiction",
    "duplicate_managed_block",
    "forward_only_migration_failed",
    "hydration_partial_unrestorable",
    "in_target_destination_not_ignored",
    "in_target_destination_tracked",
    "launchagent_start_failed",
    "launchagent_stop_timeout",
    "lifecycle_strategy_unproven",
    "managed_block_conflict",
    "managed_block_unknown_origin",
    "managed_file_locally_modified",
    "managed_identity_drift",
    "migration_incomplete",
    "migration_postcondition_contradiction",
    "preserved_surface_declared",
    "preserved_surface_write_refused",
    "probe_drift",
    "readiness_timeout",
    "restore_target_diverged",
    "roster_plugin_absent_in_candidate",
    "runtime_candidate_failed",
    "runtime_candidate_not_serving",
    "transition_contract_mismatch",
)


class SourceError(ManagerError):
    """The locked seed source cannot be trusted or materialized."""

    error_kind = "source_error"


class SourceIdentityError(SourceError):
    """Fetched seed identity differs from the formula lock."""

    error_kind = "source_identity_mismatch"


class AdapterError(ManagerError):
    """Target-local adapter invocation failed."""

    error_kind = "adapter_error"


class AdapterMissingError(AdapterError):
    """No reviewed adapter implements the requested operation."""

    error_kind = "adapter_missing"
    exit_code = 3


class AdapterProtocolError(AdapterError):
    """An adapter violated the shared JSON protocol."""

    error_kind = "adapter_protocol_error"


class ProbeDriftError(ManagerError):
    """Action-driving state changed after preview approval."""

    error_kind = "probe_drift"
    exit_code = 3


class InstanceUnmanagedError(ManagerError):
    """A target exists but has no manager registry record."""

    error_kind = "instance_unmanaged"
    exit_code = 3
