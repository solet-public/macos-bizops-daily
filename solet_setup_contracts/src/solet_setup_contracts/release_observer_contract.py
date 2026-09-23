"""Owner-neutral stdlib release observer wire records and admission checks.

This module has no filesystem/process/database effects. Paths are lexically
validated here; owners must prove realpath, object identity and artifact bytes.
A wire-valid receipt is evidence to compare, not proof of a qualified producer.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

from .release_observer_codec import (
    PROTOCOL as PROTOCOL,
)
from .release_observer_codec import (
    AbsolutePath,
    Contract,
    Digest,
    FailureCode,
    Identifier,
    Natural,
    Positive,
    Timestamp,
    absolute_path,
    facts_target,
    require,
    timestamp,
    unique,
    validate_allocation_intent,
    validate_capture_times,
    validate_command_deadline,
    validate_commands,
    validate_interval,
    validate_manager_header,
    validate_observation_binding,
    validate_outcome,
    validate_phase_facts,
    validate_retention_reservation,
    validate_retention_result,
)
from .release_observer_codec import (
    observation_artifacts as observation_artifacts,
)
from .release_observer_codec import (
    reservation_identity as reservation_identity,
)
from .release_observer_codec import (
    validate_history as validate_history,
)
from .release_observer_codec import (
    validate_identity as validate_identity,
)
from .release_observer_codec import (
    validate_observation as validate_observation,
)
from .release_observer_codec import (
    validate_replay as validate_replay,
)
from .release_observer_codec import (
    validate_request as validate_request,
)

type Phase = Literal["WAIT_VM", "CLONE", "MEMORY", "BOOT", "INSTALL", "SETUP", "DOCTOR", "HEALTH"]
type Status = Literal["absent", "waiting", "succeeded", "failed", "timeout", "unknown", "conflicting"]


@dataclass(frozen=True, slots=True)
class GitOID(Contract):
    kind = "git_oid"
    algorithm: Literal["sha1", "sha256"]
    value: str

    def validate(self) -> None:
        width = 40 if self.algorithm == "sha1" else 64
        require(re.fullmatch(rf"[0-9a-f]{{{width}}}", self.value) is not None, "full lowercase Git ID required")


@dataclass(frozen=True, slots=True)
class ArtifactRef(Contract):
    kind = "artifact_ref"
    artifact_id: Identifier
    uri: str
    sha256: Digest
    byte_count: Natural
    media_type: str
    producer: Identifier

    def validate(self) -> None:
        require(self.uri.startswith("file:///"), "host-local file URI required")
        absolute_path(self.uri[7:])


@dataclass(frozen=True, slots=True)
class ProtectedIdentity(Contract):
    kind = "protected_identity"
    host_id: Identifier
    name: Identifier
    storage_root: AbsolutePath
    content_manifest_sha256: Digest
    snapshot_id: Identifier


@dataclass(frozen=True, slots=True)
class TargetReservation(Contract):
    """Stable pre-materialization identity; never a claim that a disk exists."""

    kind = "target_reservation"
    host_id: Identifier
    name: Identifier
    app_home: AbsolutePath
    storage_root: AbsolutePath
    target_token: Identifier
    lease_id: Identifier
    identity_sha256: Digest


@dataclass(frozen=True, slots=True)
class MaterializedTarget(Contract):
    """Owner-measured storage identity; boot_id is absent only before boot."""

    kind = "materialized_target"
    reservation: TargetReservation
    device: Natural
    inode: Positive
    native_config_sha256: Digest
    content_manifest: ArtifactRef
    boot_id: Identifier | None


@dataclass(frozen=True, slots=True)
class Header(Contract):
    """Complete admitted context; request/phase identity is separate from proof_key."""

    kind = "header"
    request_id: Identifier
    operation_id: Identifier
    intent_sha256: Digest | None
    run_identity_sha256: Digest
    proof_key: Digest
    admitted_nonce: Identifier
    repository: Identifier
    release_id: Identifier
    membership_revision: Positive
    completion_sha256: Digest
    assembly_sha256: Digest
    source_pin: GitOID
    source_tree: GitOID
    source_manifest: ArtifactRef
    phase: Phase
    logical_ordinal: Positive
    attempt: Positive
    policy_sha256: Digest
    capability_contract_sha256: Digest
    protected_pristine: ProtectedIdentity
    forbidden_r33: ProtectedIdentity
    protected_snapshot_manifest: ArtifactRef
    allocation_id: Identifier
    allocated_target: TargetReservation
    manager_instance_name: Identifier | None
    manager_transaction_id: Identifier | None
    release_operation_id: Identifier | None
    prior_phase_receipt_sha256: Digest | None
    prior_observation_id: Identifier | None
    prior_observation_sha256: Digest | None
    created_at: Timestamp
    deadline: Timestamp

    def validate(self) -> None:
        require(timestamp(self.created_at) < timestamp(self.deadline), "request interval reversed", "reversed_time")
        require((self.prior_observation_id is None) == (self.prior_observation_sha256 is None), "partial predecessor")
        require(self.logical_ordinal == 1 or self.prior_phase_receipt_sha256 is not None, "missing prior phase receipt")
        require(self.protected_pristine != self.forbidden_r33, "protected identities collide")
        validate_manager_header(self)
        self._target_separation()
        require(self.allocated_target.identity_sha256 == reservation_identity(self.allocated_target, self.run_identity_sha256, self.admitted_nonce), "reservation run/nonce binding differs", "identity_mismatch")

    def _target_separation(self) -> None:
        target = self.allocated_target
        for protected in (self.protected_pristine, self.forbidden_r33):
            require(target.host_id == protected.host_id, "custody host differs")
            require(target.name != protected.name, "protected target name")
            left, right = target.storage_root.rstrip("/") + "/", protected.storage_root.rstrip("/") + "/"
            require(not left.startswith(right) and not right.startswith(left), "protected storage overlap")
        require(self.protected_pristine.storage_root != self.forbidden_r33.storage_root, "protected storage alias")


@dataclass(frozen=True, slots=True)
class ModuleBinding(Contract):
    kind = "module_binding"
    name: Identifier
    path: AbsolutePath
    sha256: Digest


@dataclass(frozen=True, slots=True)
class NativeBinding(Contract):
    kind = "native_binding"
    executable: AbsolutePath
    executable_sha256: Digest
    version: str
    command_storage_contract_sha256: Digest


@dataclass(frozen=True, slots=True)
class ObserverBinding(Contract):
    """Exact independently admitted Python producer, namespace and generation."""

    kind = "observer_binding"
    producer_id: Identifier
    producer_generation: Identifier
    executable: AbsolutePath
    executable_sha256: Digest
    interpreter: AbsolutePath
    prefix: AbsolutePath
    modules: tuple[ModuleBinding, ...]
    package: Identifier
    release_pin: GitOID
    host_id: Identifier
    namespace: Literal["host", "guest"]
    capability_contract_sha256: Digest
    installed_receipt: ArtifactRef

    def validate(self) -> None:
        require(bool(self.modules), "module provenance required")
        unique(tuple(item.name for item in self.modules))
        unique(tuple(item.path for item in self.modules))


@dataclass(frozen=True, slots=True)
class EnvironmentEntry(Contract):
    kind = "environment_entry"
    name: Identifier
    value: str


@dataclass(frozen=True, slots=True)
class OutputLocation(Contract):
    kind = "output_location"
    path: AbsolutePath
    byte_limit: Positive


@dataclass(frozen=True, slots=True)
class CommandPlan(Contract):
    kind = "command_plan"
    binding: ObserverBinding | NativeBinding
    argv: tuple[str, ...]
    cwd: AbsolutePath
    environment: tuple[EnvironmentEntry, ...]
    environment_policy_sha256: Digest
    stdin: ArtifactRef
    timeout_ms: Positive
    stdout: OutputLocation
    stderr: OutputLocation
    result: OutputLocation

    def validate(self) -> None:
        require(bool(self.argv) and self.argv[0] == self.binding.executable, "argv executable differs")
        unique(tuple(item.name for item in self.environment))
        unique((self.stdout.path, self.stderr.path, self.result.path, self.stdin.uri[7:]))


@dataclass(frozen=True, slots=True)
class NoCommand(Contract):
    kind = "no_command"
    reason: Literal["reserve_only", "passive_read", "retain_in_place"]


@dataclass(frozen=True, slots=True)
class RetentionReservation(Contract):
    kind = "retention_reservation"
    reservation_id: Identifier
    target: TargetReservation
    retained_root: AbsolutePath
    hold_id: Identifier
    non_expiring: Literal[True]


@dataclass(frozen=True, slots=True)
class AllocationRequest(Contract):
    """Pre-intent reservation only. The request cannot execute its desired action."""

    kind = "allocation_request"
    digest_field = "request_sha256"
    header: Header
    desired_action: Phase
    proposed_outputs: tuple[OutputLocation, ...]
    predecessor_evidence: tuple[ArtifactRef, ...]

    def validate(self) -> None:
        require(self.header.intent_sha256 is None, "allocation is pre-intent only")
        require(self.desired_action == self.header.phase, "allocation action differs")
        require(bool(self.proposed_outputs), "output population required")
        unique(tuple(item.path for item in self.proposed_outputs))
        inputs = {item.uri[7:] for item in self.predecessor_evidence}
        require(not inputs.intersection(item.path for item in self.proposed_outputs), "input/output alias")


@dataclass(frozen=True, slots=True)
class AllocationResult(Contract):
    """Immutable reservation and plan; the intent subsequently binds its digest."""

    kind = "allocation_result"
    digest_field = "allocation_sha256"
    request: AllocationRequest
    producer_id: Identifier
    producer_generation: Identifier
    materialization: Literal["reserved"]
    command: CommandPlan | NoCommand
    observation_selectors: tuple[Phase, ...]
    retention: RetentionReservation

    def validate(self) -> None:
        validate_retention_reservation(self.retention, self.request.header)
        require(bool(self.observation_selectors), "observation selector required")
        unique(self.observation_selectors)
        if isinstance(self.command, CommandPlan):
            outputs = (self.command.stdout, self.command.stderr, self.command.result)
            require(outputs == self.request.proposed_outputs, "command output population differs")
            validate_command_deadline(self.command, self.request.header)


@dataclass(frozen=True, slots=True)
class CommandReceipt(Contract):
    kind = "command_receipt"
    command: CommandPlan
    outcome: Literal["exited", "timeout", "unknown", "not_submitted"]
    exit_code: int | None
    stdout: ArtifactRef
    stderr: ArtifactRef
    result: ArtifactRef
    started_at: Timestamp
    finished_at: Timestamp

    def validate(self) -> None:
        require((self.outcome == "exited") == (self.exit_code is not None), "exit code branch differs")
        require(timestamp(self.started_at) <= timestamp(self.finished_at), "command time reversed", "reversed_time")
        for artifact, output in zip((self.stdout, self.stderr, self.result), (self.command.stdout, self.command.stderr, self.command.result), strict=True):
            require(artifact.uri == "file://" + output.path and artifact.byte_count <= output.byte_limit, "command artifact mapping/limit differs", "artifact_mismatch")


@dataclass(frozen=True, slots=True)
class PhaseIntent(Contract):
    kind = "phase_intent"
    digest_field = "intent_sha256"
    operation_id: Identifier
    allocation_sha256: Digest
    phase: Phase
    attempt: Positive
    command: CommandPlan | NoCommand


@dataclass(frozen=True, slots=True)
class ExecutionRequest(Contract):
    kind = "execution_request"
    digest_field = "request_sha256"
    header: Header
    allocation: AllocationResult
    intent: PhaseIntent

    def validate(self) -> None:
        validate_allocation_intent(self.header, self.allocation, self.intent)


@dataclass(frozen=True, slots=True)
class FailureEvidence(Contract):
    kind = "failure_evidence"
    code: FailureCode
    detail: str
    evidence: tuple[ArtifactRef, ...]

    def validate(self) -> None:
        require(bool(self.evidence), "non-success evidence required")
        unique(self.evidence)


@dataclass(frozen=True, slots=True)
class ExecutionResult(Contract):
    kind = "execution_result"
    request: ExecutionRequest
    producer_id: Identifier
    producer_generation: Identifier
    submission: Literal["not_submitted", "submitted", "unknown"]
    status: Status
    command_receipts: tuple[CommandReceipt, ...]
    reason: FailureEvidence | None

    def validate(self) -> None:
        validate_outcome(self.status, self.reason)
        require(self.status != "succeeded" or self.submission == "submitted", "unsubmitted success")
        validate_commands(self.command_receipts, self.request.intent.command)
        if self.status == "succeeded" and isinstance(self.request.intent.command, CommandPlan):
            require(all(item.outcome == "exited" and item.exit_code == 0 for item in self.command_receipts), "failed execution cannot succeed")


@dataclass(frozen=True, slots=True)
class ObserveRequest(Contract):
    """Comparison constraints and references only; accepts no measured facts."""

    kind = "observe_request"
    digest_field = "request_sha256"
    header: Header
    allocation: AllocationResult
    intent: PhaseIntent
    command_artifacts: tuple[ArtifactRef, ...]
    expected_measurement: Phase
    expected_target: MaterializedTarget | None
    prior_sample_sequence: Natural
    transport_challenge: Identifier

    def validate(self) -> None:
        validate_allocation_intent(self.header, self.allocation, self.intent)
        require(self.expected_measurement == self.header.phase, "measurement differs")
        require((self.prior_sample_sequence == 0) == (self.header.prior_observation_id is None), "missing chain predecessor")
        unique(self.command_artifacts)
        if self.expected_target is not None:
            require(self.expected_target.reservation == self.header.allocated_target, "expected target differs", "identity_mismatch")
        if self.header.phase in ("INSTALL", "SETUP", "DOCTOR", "HEALTH"):
            require(self.expected_target is not None and self.expected_target.boot_id is not None, "post-boot request needs admitted epoch")


@dataclass(frozen=True, slots=True)
class LeaseFacts(Contract):
    kind = "lease_facts"
    reservation: TargetReservation
    inventory: ArtifactRef


@dataclass(frozen=True, slots=True)
class CloneFacts(Contract):
    kind = "clone_facts"
    target: MaterializedTarget
    pristine_before_sha256: Digest
    pristine_after_sha256: Digest
    source_relationship: ArtifactRef
    inventory: ArtifactRef

    def validate(self) -> None:
        require(self.pristine_before_sha256 == self.pristine_after_sha256, "pristine content changed", "identity_mismatch")


@dataclass(frozen=True, slots=True)
class MemoryFacts(Contract):
    kind = "memory_facts"
    clone: CloneFacts
    memory_mib: Literal[24576]
    configuration: ArtifactRef


@dataclass(frozen=True, slots=True)
class BootFacts(Contract):
    kind = "boot_facts"
    clone: CloneFacts
    memory_mib: Literal[24576]
    guest_memory_bytes: Literal[25769803776]
    process_identity: ArtifactRef
    guest_kernel_evidence: ArtifactRef

    def validate(self) -> None:
        require(self.clone.target.boot_id is not None, "boot epoch required")


@dataclass(frozen=True, slots=True)
class InstallFacts(Contract):
    kind = "install_facts"
    target: MaterializedTarget
    publication_request_sha256: Digest
    formula: ArtifactRef
    tap_commit: GitOID
    manager: ObserverBinding
    manager_lock: ArtifactRef
    seed_commit: GitOID
    seed_tree: GitOID
    seed_manifest: ArtifactRef
    source_pin: GitOID
    source_tree: GitOID
    source_manifest: ArtifactRef
    source_status: Literal["CLEAN"]


@dataclass(frozen=True, slots=True)
class ManagerAttempt(Contract):
    """Release, Manager transaction and adapter identifiers never share a slot."""

    kind = "manager_attempt"
    release_operation_id: Identifier
    transaction_id: Identifier
    adapter_operation_id: Identifier
    adapter_probe_id: Identifier
    adapter_request_id: Identifier
    attempt: Positive
    evidence: ArtifactRef

    def validate(self) -> None:
        pattern = r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}"
        require(re.fullmatch(pattern, self.transaction_id) is not None, "Manager transaction UUID required")
        require(re.fullmatch(pattern, self.adapter_request_id) is not None, "adapter request UUID required")


@dataclass(frozen=True, slots=True)
class SetupFrontier(Contract):
    kind = "setup_frontier"
    owner_attempt: ManagerAttempt
    preview: ArtifactRef
    fingerprint: Digest
    asked_keys: tuple[Identifier, ...]
    submissions: tuple[ArtifactRef, ...]
    transaction_receipt: ArtifactRef

    def validate(self) -> None:
        unique(self.asked_keys)
        require(len(self.asked_keys) == len(self.submissions), "submission population differs")


@dataclass(frozen=True, slots=True)
class SetupFacts(Contract):
    kind = "setup_facts"
    profile: ArtifactRef
    target: MaterializedTarget
    flow_contract_sha256: Digest
    frontiers: tuple[SetupFrontier, ...]
    required_operations: tuple[Identifier, ...]
    completed_operations: tuple[Identifier, ...]
    transaction_status: Literal["verified"]
    completion_verified: Literal[True]
    transaction_receipt: ArtifactRef

    def validate(self) -> None:
        require(bool(self.frontiers) and bool(self.required_operations), "setup population required")
        unique(tuple(item.fingerprint for item in self.frontiers))
        unique(self.required_operations)
        require(self.required_operations == self.completed_operations, "incomplete setup population")


@dataclass(frozen=True, slots=True)
class DoctorCheck(Contract):
    kind = "doctor_check"
    check_id: Identifier
    owner_attempt: ManagerAttempt
    required: Literal[True]
    status: Literal["verified"]
    read_only_evidence: ArtifactRef


@dataclass(frozen=True, slots=True)
class DoctorFacts(Contract):
    kind = "doctor_facts"
    target: MaterializedTarget
    command: CommandReceipt
    raw_result: ArtifactRef
    checks: tuple[DoctorCheck, ...]
    required_check_ids: tuple[Identifier, ...]
    flow_contract_sha256: Digest
    transaction_status: Literal["verified"]
    completion_verified: Literal[True]

    def validate(self) -> None:
        require(bool(self.checks), "doctor checks required")
        unique(self.required_check_ids)
        require(tuple(item.check_id for item in self.checks) == self.required_check_ids, "doctor population differs")
        require(self.command.outcome == "exited" and self.command.exit_code == 0, "doctor command failed")


@dataclass(frozen=True, slots=True)
class HealthFacts(Contract):
    kind = "health_facts"
    target: MaterializedTarget
    command: CommandReceipt
    raw_result: ArtifactRef
    status: Literal["healthy"]
    instance_id: Identifier
    bootstrap_identity_sha256: Digest
    source_pin: GitOID
    seed_tree: GitOID
    manager_binding_sha256: Digest

    def validate(self) -> None:
        require(self.command.outcome == "exited" and self.command.exit_code == 0, "health command failed")
        home = self.target.reservation.app_home
        require(self.command.command.argv == (home + "/.venv/bin/solet-bridge", "health"), "target-local bridge required")
        require(self.command.command.cwd == home, "health cwd differs")


type Facts = LeaseFacts | CloneFacts | MemoryFacts | BootFacts | InstallFacts | SetupFacts | DoctorFacts | HealthFacts


@dataclass(frozen=True, slots=True)
class ObservationResult(Contract):
    """Original immutable capture. Request nesting preserves every header binding."""

    kind = "observation_result"
    digest_field = "observation_sha256"
    request: ObserveRequest
    observation_id: Identifier
    producer_id: Identifier
    producer_generation: Identifier
    observer_binding: ObserverBinding
    sample_sequence: Positive
    sample_started_at: Timestamp
    sample_finished_at: Timestamp
    state_generation_before: Identifier
    state_generation_after: Identifier
    status: Status
    facts: Facts | None
    command_receipts: tuple[CommandReceipt, ...]
    source_artifacts: tuple[ArtifactRef, ...]
    transport_challenge: Identifier
    reason: FailureEvidence | None

    def validate(self) -> None:
        validate_outcome(self.status, self.reason)
        require((self.status == "succeeded") == (self.facts is not None), "facts outside success branch")
        require(self.transport_challenge == self.request.transport_challenge, "capture challenge differs", "binding_mismatch")
        require(self.sample_sequence == self.request.prior_sample_sequence + 1, "noncontiguous sample", "sequence_gap")
        validate_capture_times(self)
        validate_observation_binding(self)
        if self.command_receipts or self.status == "succeeded":
            validate_commands(self.command_receipts, self.request.intent.command)
        if self.facts is not None:
            require(self.state_generation_before == self.state_generation_after, "incoherent successful sample", "generation_changed")
            require(bool(self.source_artifacts), "source evidence required")
            validate_phase_facts(self.facts, self.request.header)
            if isinstance(self.facts, SetupFacts):
                retained = self.source_artifacts
                for frontier in self.facts.frontiers:
                    require(frontier.preview in retained, "setup preview not retained", "artifact_mismatch")
                    require(all(submission in retained for submission in frontier.submissions), "setup submission not retained", "artifact_mismatch")
            target = facts_target(self.facts)
            if self.request.expected_target is not None:
                require(target == self.request.expected_target, "observed object/boot differs", "identity_mismatch")
        unique(self.source_artifacts)


@dataclass(frozen=True, slots=True)
class ReadObservationRequest(Contract):
    kind = "read_observation_request"
    digest_field = "request_sha256"
    header: Header
    observation_id: Identifier
    observation_sha256: Digest
    transport_challenge: Identifier

    def validate(self) -> None:
        require(self.header.intent_sha256 is not None, "historical query needs original intent")


@dataclass(frozen=True, slots=True)
class ReadAttestation(Contract):
    kind = "read_attestation"
    schema = "read_attestation.v1"
    query_sha256: Digest
    observation_id: Identifier
    observation_sha256: Digest
    producer_id: Identifier
    producer_generation: Identifier
    observer_binding: ObserverBinding
    verified_at: Timestamp
    verified_artifacts: tuple[ArtifactRef, ...]
    transport_challenge: Identifier

    def validate(self) -> None:
        unique(self.verified_artifacts)


@dataclass(frozen=True, slots=True)
class ReadObservationResult(Contract):
    """Fresh owner attestation around unchanged historical capture bytes."""

    kind = "read_observation_result"
    request: ReadObservationRequest
    observation: ObservationResult
    attestation: ReadAttestation

    def validate(self) -> None:
        query, original, att = self.request, self.observation, self.attestation
        require((att.query_sha256, att.observation_id, att.observation_sha256, att.transport_challenge) == (query.digest, query.observation_id, query.observation_sha256, query.transport_challenge), "read attestation query differs", "binding_mismatch")
        require((original.observation_id, original.digest) == (query.observation_id, query.observation_sha256), "historical observation differs", "digest_mismatch")
        validate_identity(original.request.header, query.header)
        require(att.producer_id == original.producer_id, "historical owner differs", "binding_mismatch")
        require((att.producer_id, att.producer_generation) == (att.observer_binding.producer_id, att.observer_binding.producer_generation), "read producer differs from binding", "binding_mismatch")
        require(att.verified_artifacts == observation_artifacts(original), "historical artifact population differs", "artifact_mismatch")
        validate_interval(att.verified_at, query.header)


@dataclass(frozen=True, slots=True)
class RetentionRequest(Contract):
    """Failed release operation stays distinct from the new retention operation."""

    kind = "retention_request"
    digest_field = "request_sha256"
    header: Header
    failed_operation_id: Identifier
    failed_phase_receipt_sha256: Digest
    failed_observation_id: Identifier
    failed_observation_sha256: Digest
    failed_at: Timestamp
    target: MaterializedTarget | None
    custody: RetentionReservation
    retention_operation_id: Identifier
    required_class: Literal["disk_and_logs", "saved_state", "absence_and_logs"]
    command: CommandPlan | NoCommand
    command_artifacts: tuple[ArtifactRef, ...]

    def validate(self) -> None:
        require(self.header.intent_sha256 is not None, "retention intent required")
        require(self.failed_operation_id == self.header.operation_id, "failed release operation differs", "identity_mismatch")
        validate_retention_reservation(self.custody, self.header)
        require(timestamp(self.failed_at) <= timestamp(self.header.created_at), "failure time is in future", "future_sample")
        require((self.required_class == "absence_and_logs") == (self.target is None), "retention target branch differs")
        if self.target is not None:
            require(self.target.reservation == self.custody.target, "foreign retained target", "identity_mismatch")
        if isinstance(self.command, CommandPlan):
            validate_command_deadline(self.command, self.header)


@dataclass(frozen=True, slots=True)
class RetentionResult(Contract):
    """Preservation evidence; complete byte capture never claims replay qualification."""

    kind = "retention_result"
    digest_field = "retention_result_sha256"
    request: RetentionRequest
    retention_intent_sha256: Digest
    observer_binding: ObserverBinding
    producer_id: Identifier
    producer_generation: Identifier
    sample_sequence: Positive
    action_started_at: Timestamp
    capture_started_at: Timestamp
    capture_finished_at: Timestamp
    status: Status
    classification: Literal["complete", "partial", "absent"]
    retained_manifest: ArtifactRef
    retained_location: AbsolutePath
    saved_state_available: bool
    custodian_hold: ArtifactRef
    verification_evidence: tuple[ArtifactRef, ...]
    command_receipts: tuple[CommandReceipt, ...]
    reason: FailureEvidence | None

    def validate(self) -> None:
        validate_retention_result(self)


MESSAGE_TYPES: tuple[type[Contract], ...] = (
    AllocationRequest, AllocationResult, ExecutionRequest, ExecutionResult,
    ObserveRequest, ObservationResult, ReadObservationRequest, ReadObservationResult,
    RetentionRequest, RetentionResult,
)
