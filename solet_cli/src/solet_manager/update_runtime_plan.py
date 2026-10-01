"""Runtime plan: declared closure, preservation validation, probes, strategy, fingerprint.

At ``source_advanced`` the Manager has proven the target's source is the exact
candidate release.  This module renders what the runtime transition would do
on THIS host (design section 8): it computes the declared dependency closure,
refuses every preserved or tracked destination before any probe, runs each
applicable operation's probe against the N+1 tree, fixes exactly one
lifecycle strategy, and binds the probed plan into the second, narrower
approval fingerprint.  It writes nothing to the target and nothing to Manager
state; the only side effect is the disclosed target process executions.
"""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import subprocess
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import cast

from solet_setup_contracts import canonical_sha256

from .adapter_protocol import EXISTING_INSTALL_FLOW_ID, OperationRequest, OperationResult
from .base_python import resolve_base_python
from .colour_census import COLOUR_OUTSIDE_LAUNCHAGENT, ProcessTableReader, read_colour_census
from .cutover_fingerprint import cutover_fingerprint
from .cutover_receipts import CutoverTerms
from .errors import AdapterError, AdapterProtocolError, SourceError, UpdateBlockedError
from .existing_install_adapters import (
    ATTEST_PROCESS_KEY,
    ExistingInstallAdapterRegistry,
    bridge_health,
    invoke_existing_adapter,
    invoke_instance_bridge,
    invoke_reconciliation,
    run_launchctl,
    run_ps,
    run_security_metadata,
)
from .existing_install_bundle import CLONE_EXCLUDE_DESTINATION, STAGE_ORDER, DependencyPiece, ManagedArtifact, RuntimeOperation
from .host_platform import HostPlatform, HostPlatformError, read_host_platform
from .launch_topology import (
    LEGACY_DIRECT,
    SUPPORTED_TOPOLOGIES,
    derive_launch_topology,
    launchagent_plist_path,
    plist_sha256,
)
from .managed_artifact_backup import file_sha256
from .models import (
    CheckpointStatus,
    DeclaredClosurePiece,
    InstanceInventoryRecordV2,
    JsonValue,
    LifecycleObservation,
    ManagedArtifactState,
    RuntimeOperationPlan,
    RuntimePlan,
)
from .reconciliation_request import ReconciliationOutcome, build_reconciliation_envelope
from .target_git import GitLayout, run_target_git
from .update_adopt_diff import adopt_diffs
from .update_candidate import UpdateCandidate
from .update_clone_exclude import planned_exclude_covers

__all__ = [
    "REQUIRED_DISTRIBUTIONS",
    "STEP5_CAPABILITIES",
    "STEP5_MANAGED_SUB_SURFACES",
    "STEP5_NON_TOUCH_SURFACES",
    "AttestationObservation",
    "PlanContext",
    "RuntimeSeams",
    "build_runtime_plan",
    "decode_facts",
    "declared_closure_for",
    "encode_facts",
    "idempotency_key",
    "knowledge_removed_articles",
    "operation_request",
    "roster_plugins",
    "runtime_fingerprint",
]

#: Manager-side copy of the bootstrap adapter's fixed closure; a smoke proves
#: byte-equality with ``bootstrap_adapter.dependency.REQUIRED_DISTRIBUTIONS``.
REQUIRED_DISTRIBUTIONS: tuple[tuple[str, str], ...] = (
    ("solet-setup-contracts", "solet_setup_contracts"),
    ("ananta", "ananta"),
    ("macos-vault-plugin", "plugins/macos_vault_plugin"),
    ("github_midwife_plugin", "plugins/github_midwife_plugin"),
    ("agent_messaging_plugin", "plugins/agent_messaging_plugin"),
)
#: ``profile_config`` and ``plugin_roster_selection`` stay non-touch except for the exact files a
#: release-declared plugin transition names in its planned actions (``declared_plugin_transitions``).
STEP5_NON_TOUCH_SURFACES = (
    "credentials",
    "documents",
    "keychain_and_vault",
    "local_session_history",
    "logs_and_runtime_state",
    "memories_and_knowledge_state",
    "operator_authored_files",
    "postgresql_roles_databases_and_passwords",
    "profile_config",
    "profile_data",
    "tracked_local_modifications",
    "router_launchagent",
    "plugin_roster_selection",
)
STEP5_MANAGED_SUB_SURFACES = (
    "dependencies_and_venv",
    "instance_launchagent_plist",
    "named_launchers",
    "shell_startup_managed_block",
    "coding_agent_config_managed_entries",
    "declared_pre_runtime_migration_targets",
    "platform_state_via_running_service",
    "knowledge_index_declared_removals",
    "coding_agent_plugin_cache",
    "instance_process_lifecycle",
    "declared_plugin_transitions",
)
STEP5_CAPABILITIES = (
    "dependency_closure_repair",
    "pre_runtime_migration",
    "managed_artifact_hydration",
    "managed_artifact_backup",
    "managed_artifact_restore",
    "reconciliation_cutover",
    "single_color_restart",
    "post_runtime_platform_migration",
    "knowledge_reinstall",
    "plugin_cache_refresh",
    "declared_plugin_transition",
)
ROUTER_PLUGIN = "macos_self_deployment_plugin"
ADAPTER_MODULE_PATH = "plugins/github_midwife_plugin/src/github_midwife_plugin/setup_adapter.py"
_PROBE_TIMEOUT_SECONDS = 300
_ATTESTATION_KEYS = (
    "current_release_id",
    "release_id",
    "router_active_instance_id",
    "router_active_color",
    "self_start_token",
    "manifest_etag",
    "source_surface_sha256",
    "release_surface_sha256",
)
_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")

type AdapterInvoker = Callable[[ExistingInstallAdapterRegistry, OperationRequest], OperationResult]
type ReconciliationInvoker = Callable[[ExistingInstallAdapterRegistry, dict[str, JsonValue], int], ReconciliationOutcome]
type BridgeInvoker = Callable[[ExistingInstallAdapterRegistry, str, dict[str, JsonValue], str, int], dict[str, JsonValue]]
type HealthReader = Callable[[ExistingInstallAdapterRegistry, int], dict[str, JsonValue]]
type LaunchctlRunner = Callable[[ExistingInstallAdapterRegistry, str, tuple[str, ...], int], subprocess.CompletedProcess[str]]
type KeychainMetadataReader = Callable[[str, str], subprocess.CompletedProcess[str]]
type BasePythonResolver = Callable[[], Path | None]
type WhichResolver = Callable[[str], str | None]


def _real_reconciliation(registry: ExistingInstallAdapterRegistry, envelope: dict[str, JsonValue], timeout: int) -> ReconciliationOutcome:
    return invoke_reconciliation(registry, envelope, timeout_seconds=timeout)


def _real_bridge(registry: ExistingInstallAdapterRegistry, key: str, arguments: dict[str, JsonValue], kind: str, timeout: int) -> dict[str, JsonValue]:
    return invoke_instance_bridge(registry, key, arguments, kind=kind, timeout_seconds=timeout)


def _real_health(registry: ExistingInstallAdapterRegistry, timeout: int) -> dict[str, JsonValue]:
    return bridge_health(registry, timeout_seconds=timeout)


def _real_launchctl(registry: ExistingInstallAdapterRegistry, verb: str, arguments: tuple[str, ...], timeout: int) -> subprocess.CompletedProcess[str]:
    return run_launchctl(registry, verb, *arguments, timeout_seconds=timeout)


@dataclass(frozen=True, slots=True)
class RuntimeSeams:
    """Injectable host seams for the runtime plan and executor; production uses the real vectors."""

    home: Path = field(default_factory=Path.home)
    invoke_adapter: AdapterInvoker = invoke_existing_adapter
    invoke_reconciliation: ReconciliationInvoker = _real_reconciliation
    invoke_bridge: BridgeInvoker = _real_bridge
    read_health: HealthReader = _real_health
    launchctl: LaunchctlRunner = _real_launchctl
    uid: int = field(default_factory=os.getuid)
    run_ps: ProcessTableReader = run_ps
    run_security: KeychainMetadataReader = run_security_metadata
    #: Step 7 section 4.1: host-software resolution is a closed vector with a production default, so a
    #: fixture can measure "host Python absent" without deleting host binaries or environment tricks.
    resolve_base_python: BasePythonResolver = resolve_base_python
    which: WhichResolver = shutil.which
    #: iss_6d26db73 / rul_385dac24: sw_vers + uname, measured only when a closure piece requires a host profile.
    host_platform: Callable[[], HostPlatform] = read_host_platform


@dataclass(frozen=True, slots=True)
class AttestationObservation:
    """The bounded read of the running process's identity (``attest_runtime_code``)."""

    current_release_id: str
    release_id: str
    active_instance_id: str
    active_color: str
    active_start_token: str
    manifest_etag: str
    source_surface_sha256: str
    release_surface_sha256: str
    served_by_self: bool

    @classmethod
    def from_payload(cls, payload: dict[str, JsonValue]) -> AttestationObservation:
        values: list[str] = []
        for key in _ATTESTATION_KEYS:
            value = payload.get(key)
            if not isinstance(value, str) or not value:
                raise AdapterProtocolError(f"attestation payload lacks a non-empty {key!r}")
            values.append(value)
        served = payload.get("served_by_self")
        if not isinstance(served, bool):
            raise AdapterProtocolError("attestation payload lacks served_by_self")
        return cls(values[0], values[1], values[2], values[3], values[4], values[5], values[6], values[7], served)

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "current_release_id": self.current_release_id,
            "release_id": self.release_id,
            "active_instance_id": self.active_instance_id,
            "active_color": self.active_color,
            "active_start_token": self.active_start_token,
            "manifest_etag": self.manifest_etag,
            "source_surface_sha256": self.source_surface_sha256,
            "release_surface_sha256": self.release_surface_sha256,
            "served_by_self": self.served_by_self,
        }


@dataclass(frozen=True, slots=True)
class PlanContext:
    """Everything the plan reads; assembled by the executor after identity proof."""

    record: InstanceInventoryRecordV2
    candidate: UpdateCandidate
    operation_id: str
    source_fingerprint: str
    baseline_commit: str
    baseline_tree: str
    registry: ExistingInstallAdapterRegistry
    cache_repository: Path
    seams: RuntimeSeams
    probe_purpose: str = "preview"
    operator_selections: dict[str, JsonValue] = field(default_factory=lambda: {})
    #: How ``cache_repository`` is pinned (iss_836499b3 R2-1): the update reads the Manager's bare candidate
    #: cache; the standalone doctor reads the promoted target itself, whose history holds the candidate.
    cache_layout: GitLayout = GitLayout.BARE


def encode_facts(facts: dict[str, str | int | bool | None]) -> list[str]:
    """Encode public facts as a sorted unique ``key=value`` string array (the evidence grammar)."""
    return [f"{key}={_fact_text(value)}" for key, value in sorted(facts.items())]


def decode_facts(observed: JsonValue) -> dict[str, str]:
    if not isinstance(observed, list) or not all(isinstance(item, str) and "=" in item for item in observed):
        raise AdapterProtocolError("adapter evidence facts must be a key=value string array")
    facts: dict[str, str] = {}
    for item in cast(list[str], observed):
        key, _, value = item.partition("=")
        if key in facts:
            raise AdapterProtocolError(f"adapter evidence repeats fact {key!r}")
        facts[key] = value
    return facts


def _fact_text(value: str | int | bool | None) -> str:
    if value is None:
        return "none"
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def idempotency_key(candidate_commit: str, operation_id: str, instance_id: str) -> str:
    """The one permitted idempotency template, realised: recorded, never trusted."""
    return "sha256:" + hashlib.sha256(f"{candidate_commit}\0{operation_id}\0{instance_id}".encode()).hexdigest()


def operation_request(
    context: PlanContext,
    operation: RuntimeOperation,
    *,
    phase: str,
    probe_purpose: str | None,
    approval_fingerprint: str | None,
    attempt: int,
    public_inputs: dict[str, JsonValue],
) -> OperationRequest:
    """Bind one ``existing::`` request to the existing-install flow (design section 3.3)."""
    return OperationRequest(
        request_id=str(uuid.uuid4()),
        operation_id=operation.operation_id,
        operation_ref=operation.operation_ref,
        phase=phase,
        probe_purpose=probe_purpose,
        attempt=attempt,
        name=context.record.name,
        target=context.record.target.canonical_path,
        flow_id=EXISTING_INSTALL_FLOW_ID,
        flow_source_revision=context.candidate.fields.commit,
        answers_fingerprint=canonical_sha256({}),
        approval_fingerprint=approval_fingerprint,
        dry_run=phase == "probe",
        timeout_seconds=_PROBE_TIMEOUT_SECONDS,
        public_inputs=public_inputs,
    )


def build_runtime_plan(context: PlanContext) -> RuntimePlan:
    """Render the closed runtime plan for this host; refusals are rows, not exceptions."""
    bundle = context.candidate.bundle
    record = context.record
    blocked: list[tuple[str, str]] = []
    executions = 0
    predecessor = bundle.predecessor_for(context.baseline_commit, context.baseline_tree)
    if predecessor is None and not zero_delta(context):
        blocked.append(("predecessor", "predecessor_unsupported"))
    closure = _declared_closure(context, blocked)
    artifacts = _validate_destinations(context, blocked)
    ignore_digest = _ignore_sources_digest(context, artifacts)
    operations, artifact_states, probe_executions = _probe_operations(context, closure, artifacts, blocked)
    executions += probe_executions
    lifecycle, lifecycle_executions = _lifecycle(context, artifact_states)
    executions += lifecycle_executions
    if lifecycle.unproven_reason is not None:
        blocked.append(("lifecycle", lifecycle.unproven_reason))
    forward_only = next((item.operation_id for item in bundle.runtime_operations if item.forward_only), None)
    plan = RuntimePlan(
        context.operation_id,
        record.instance_id,
        context.candidate.fields.commit,
        context.candidate.fields.tree_hash,
        context.candidate.fields.release_tag,
        None if record.runtime_release is None else record.runtime_release.commit,
        record.contract_identities.runtime_contract_digest,
        closure,
        tuple(operations),
        tuple(artifact_states),
        lifecycle,
        forward_only,
        ignore_digest,
        executions,
        tuple(blocked),
        None,
        dict(context.operator_selections),
        knowledge_removed_articles(context),
    )
    if not plan.actionable:
        return plan
    return _with_fingerprint(plan, context)


def knowledge_removed_articles(context: PlanContext) -> tuple[tuple[str, str, str], ...]:
    """``(knowledge_base, removed_path, baseline_title)`` per article the candidate delta removed (Step 6 T8).

    Derived from the cache repository with ``diff --diff-filter=D`` between the
    journaled baseline and the candidate; the title comes from the BASELINE
    blob (the candidate no longer carries it).  A zero-delta operation removes
    nothing.
    """
    removals = context.candidate.bundle.knowledge_removals
    baseline, candidate = context.baseline_commit, context.candidate.fields.commit
    if not removals or baseline == candidate:
        return ()
    listing = _git(context.cache_repository, ("diff", "--diff-filter=D", "--name-only", "--no-renames", baseline, candidate), "candidate removal set is unreadable", layout=context.cache_layout)
    rows: list[tuple[str, str, str]] = []
    for path in sorted(line for line in listing.decode("utf-8", "strict").splitlines() if line.endswith(".md")):
        parts = path.split("/")
        knowledge_base = next((kb for kb in removals if kb in parts), None)
        if knowledge_base is None:
            continue
        blob = _git(context.cache_repository, ("show", f"{baseline}:{path}"), "baseline article is unreadable", layout=context.cache_layout)
        rows.append((knowledge_base, path, _article_title(blob, path)))
    return tuple(rows)


def _article_title(blob: bytes, path: str) -> str:
    text = blob.decode("utf-8", "replace")
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("title:"):
            return stripped.removeprefix("title:").strip().strip("\"'") or path
        if stripped.startswith("# "):
            return stripped.removeprefix("# ").strip() or path
    return path


def declared_closure_for(context: PlanContext) -> tuple[tuple[DeclaredClosurePiece, ...], tuple[tuple[str, str], ...]]:
    """The declared closure and any roster refusal, for callers outside the plan (the doctor)."""
    blocked: list[tuple[str, str]] = []
    closure = _declared_closure(context, blocked)
    return closure, tuple(blocked)


def roster_plugins(target: Path) -> tuple[str, ...]:
    """The operator's plugin roster from ``profile/config/manifest.yaml`` (read, never written)."""
    return _selected_plugins(target / "profile" / "config" / "manifest.yaml")


def zero_delta(context: PlanContext) -> bool:
    """Step 6 section 4.8: the source delta is empty (a successor or an already-current import); every
    declared operation is probed and its postcondition decides, since no predecessor row can bind it."""
    return context.baseline_commit == context.candidate.fields.commit


def _probe_operations(
    context: PlanContext,
    closure: tuple[DeclaredClosurePiece, ...],
    artifacts: tuple[tuple[ManagedArtifact, str], ...],
    blocked: list[tuple[str, str]],
) -> tuple[tuple[RuntimeOperationPlan, ...], tuple[ManagedArtifactState, ...], int]:
    """Probe every applicable declared operation in bundle order (section 8.2 step 3)."""
    operations: list[RuntimeOperationPlan] = []
    artifact_states: list[ManagedArtifactState] = []
    executions = 0
    probe_all = zero_delta(context)
    for operation in context.candidate.bundle.runtime_operations:
        if not probe_all and not operation.applies_to(context.baseline_commit):
            operations.append(_not_applicable(context, operation))
            continue
        if operation.runner == "instance_bridge":
            # A platform migration is probed by its own bridge dry-run at apply time
            # (Step 6, T6/T7), never through a target adapter; the plan rows it pending.
            operations.append(_bridge_row(context, operation))
            continue
        inputs = public_inputs_for(context, operation, closure, artifacts)
        result = context.seams.invoke_adapter(context.registry, operation_request(context, operation, phase="probe", probe_purpose=context.probe_purpose, approval_fingerprint=None, attempt=1, public_inputs=inputs))
        executions += 1
        plan_row, states = _operation_from_probe(context, operation, inputs, result, artifacts)
        operations.append(plan_row)
        artifact_states.extend(states)
        if plan_row.postcondition_now in {"blocked", "failed"}:
            blocked.append((plan_row.operation_id, result.error_kind or plan_row.postcondition_now))
    return tuple(operations), tuple(artifact_states), executions


def _with_fingerprint(plan: RuntimePlan, context: PlanContext) -> RuntimePlan:
    return RuntimePlan(
        plan.operation_id,
        plan.instance_id,
        plan.source_commit,
        plan.source_tree,
        plan.source_tag,
        plan.runtime_release_commit,
        plan.runtime_contract_digest,
        plan.declared_closure,
        plan.operations,
        plan.managed_artifacts,
        plan.lifecycle,
        plan.forward_only_boundary,
        plan.ignore_sources_digest,
        plan.target_process_executions,
        plan.blocked,
        runtime_fingerprint(plan, context),
        plan.operator_selections,
        plan.knowledge_removed_articles,
    )


def runtime_fingerprint(plan: RuntimePlan, context: PlanContext) -> str:
    """``canonical_sha256`` over the closed approval-bound preimage (design section 8.3)."""
    lifecycle = plan.lifecycle
    preimage: dict[str, JsonValue] = {
        "kind": "runtime_plan",
        "operation_id": plan.operation_id,
        "source_approval_fingerprint": context.source_fingerprint,
        "candidate_descriptor_digest": context.candidate.descriptor_digest,
        "candidate_bundle_digest": context.candidate.bundle_digest,
        "source": {"commit": plan.source_commit, "tree": plan.source_tree, "tag": plan.source_tag},
        "declared_closure": [[piece.distribution, piece.relative_path, piece.origin] for piece in plan.declared_closure],
        "operations": [
            {
                "operation_id": item.operation_id,
                "operation_ref": item.operation_ref,
                "applies": item.applies,
                "idempotency_key": item.idempotency_key,
                "planned_actions": list(item.planned_actions),
                "planned_targets": list(item.planned_targets),
                "rollback_class": item.rollback_class,
                "backup_checkpoint_id": item.backup_checkpoint_id,
            }
            for item in plan.operations
        ],
        "managed_artifacts": [
            {
                "artifact_id": item.artifact_id,
                "destination": item.destination,
                "state": item.state,
                "stamped_digest": item.stamped_digest,
                "template_digest": item.template_digest,
                "action": item.action,
                "expected_sha256": item.expected_sha256,
                "current_sha256": item.current_sha256,
            }
            for item in plan.managed_artifacts
        ],
        "lifecycle": {
            "strategy": lifecycle.strategy,
            "current_release_id": lifecycle.current_release_id,
            "launch_topology": lifecycle.launch_topology,
            "launchagent_label": lifecycle.launchagent_label,
            "plist_expected_sha256": lifecycle.plist_expected_sha256,
            "adapter_module_sha256": lifecycle.adapter_module_sha256,
            "adapter_module_replaced": lifecycle.adapter_module_replaced,
            "verification_modules": list(lifecycle.verification_modules),
            "readiness_budget_seconds": lifecycle.readiness_budget_seconds,
            "cutover_fingerprint": lifecycle.cutover_fingerprint,
        },
        "forward_only_boundary": plan.forward_only_boundary,
        "ignore_sources_digest": plan.ignore_sources_digest,
        "non_touch_surfaces": list(STEP5_NON_TOUCH_SURFACES),
        "managed_sub_surfaces": list(STEP5_MANAGED_SUB_SURFACES),
        "capabilities": list(STEP5_CAPABILITIES),
        "operator_selections": plan.operator_selections,
        "knowledge_removed_articles": [list(row) for row in plan.knowledge_removed_articles],
    }
    return canonical_sha256(preimage)


def public_inputs_for(
    context: PlanContext,
    operation: RuntimeOperation,
    closure: tuple[DeclaredClosurePiece, ...],
    artifacts: tuple[tuple[ManagedArtifact, str], ...],
) -> dict[str, JsonValue]:
    """Build exactly the public inputs the bundle allows for one operation."""
    inputs: dict[str, JsonValue] = {}
    allowed = set(operation.public_inputs)
    if "declared_closure" in allowed:
        inputs["declared_closure"] = [f"{piece.distribution}={piece.relative_path}" for piece in closure]
    if "artifact_ids" in allowed:
        kinds = {"launchd_plist"} if operation.operation_ref == "existing::autostart.reconcile" else {"managed_block", "rendered_whole"}
        selected = [(artifact, destination) for artifact, destination in artifacts if artifact.kind in kinds]
        inputs["artifact_ids"] = [artifact.artifact_id for artifact, _ in selected]
        if "planned_destinations" in allowed:
            inputs["planned_destinations"] = [f"{artifact.artifact_id}={destination}" for artifact, destination in selected]
    return inputs


def _declared_closure(context: PlanContext, blocked: list[tuple[str, str]]) -> tuple[DeclaredClosurePiece, ...]:
    """closure = REQUIRED ∪ (roster ∩ candidate plugins) ∪ additions (design section 4.1)."""
    target = Path(context.record.target.canonical_path)
    roster = _selected_plugins(target / "profile" / "config" / "manifest.yaml")
    candidate_plugins = _candidate_plugins(context)
    pieces: dict[str, DeclaredClosurePiece] = {}
    for distribution, relative in REQUIRED_DISTRIBUTIONS:
        pieces[relative] = DeclaredClosurePiece(distribution, relative, "required")
    for plugin in roster:
        relative = f"plugins/{plugin}"
        if plugin not in candidate_plugins:
            blocked.append(("closure", "roster_plugin_absent_in_candidate"))
            continue
        pieces.setdefault(relative, DeclaredClosurePiece(plugin, relative, "roster_plugin"))
    for piece in _release_additions(context, candidate_plugins, blocked):
        pieces.setdefault(piece.relative_path, DeclaredClosurePiece(piece.distribution, piece.relative_path, "release_addition"))
    return tuple(pieces[key] for key in sorted(pieces))


def _release_additions(context: PlanContext, candidate_plugins: frozenset[str], blocked: list[tuple[str, str]]) -> list[DependencyPiece]:
    """The release's additions that belong on THIS candidate and THIS host.

    One flow ships to every bundle, so an addition naming a plugin this candidate
    does not ship is another bundle's (iss_3e5a14f7).  An addition that requires a
    host profile applies only on a host measured to meet it (rul_385dac24: a
    macOS 26 host keeps its LM Studio closure and never installs Apple-only
    packages); a host that cannot be measured blocks the plan instead of guessing.
    """
    bundle = context.candidate.bundle
    selected: list[DependencyPiece] = []
    platform: HostPlatform | None = None
    for piece in bundle.closure_additions:
        if piece.relative_path.startswith("plugins/") and piece.relative_path.removeprefix("plugins/") not in candidate_plugins:
            continue
        if piece.requires_host is not None:
            if platform is None:
                try:
                    platform = context.seams.host_platform()
                except HostPlatformError:
                    blocked.append(("host", "host_platform_unknown"))
                    return selected
            if not bundle.host_profile(piece.requires_host).admits(platform):
                continue
        selected.append(piece)
    return selected


def _selected_plugins(path: Path) -> tuple[str, ...]:
    """Read the operator's roster; the manifest is a preserved surface, read and never written."""
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise UpdateBlockedError("profile_manifest_unreadable", f"the target's plugin roster is unreadable: {exc}", repair="Restore profile/config/manifest.yaml, then preview again.") from exc
    plugins: list[str] = []
    in_plugins = False
    for line in lines:
        if line == "plugins:":
            in_plugins = True
            continue
        if not in_plugins:
            continue
        if line.startswith("- "):
            plugin = line.removeprefix("- ").strip()
            if not plugin:
                raise UpdateBlockedError("profile_manifest_invalid", "the plugin roster names an empty plugin", repair="Repair profile/config/manifest.yaml, then preview again.")
            plugins.append(plugin)
            continue
        if line and not line.startswith((" ", "#")):
            break
    if len(plugins) != len(set(plugins)):
        raise UpdateBlockedError("profile_manifest_invalid", "the plugin roster repeats a plugin", repair="Repair profile/config/manifest.yaml, then preview again.")
    return tuple(plugins)


def _candidate_plugins(context: PlanContext) -> frozenset[str]:
    listing = _git(context.cache_repository, ("ls-tree", "--name-only", f"{context.candidate.fields.commit}:plugins"), "candidate plugin tree is unreadable", layout=context.cache_layout)
    return frozenset(line.strip() for line in listing.decode("utf-8", "strict").splitlines() if line.strip())


def _candidate_tree_paths(context: PlanContext) -> frozenset[str]:
    listing = _git(context.cache_repository, ("ls-tree", "-r", "--name-only", context.candidate.fields.commit), "candidate tree is unreadable", layout=context.cache_layout)
    return frozenset(line for line in listing.decode("utf-8", "strict").splitlines() if line)


def _validate_destinations(context: PlanContext, blocked: list[tuple[str, str]]) -> tuple[tuple[ManagedArtifact, str], ...]:
    """Section 6.3: refuse in-target destinations the target does not ignore or the candidate tracks."""
    record = context.record
    target = Path(record.target.canonical_path)
    tree_paths: frozenset[str] | None = None
    resolved: list[tuple[ManagedArtifact, str]] = []
    for artifact in context.candidate.bundle.managed_artifacts:
        destination = artifact.resolve_destination(
            home=str(context.seams.home), target=str(target), name=record.name, profile_home=str(target / "profile")
        )
        resolved.append((artifact, destination))
        relative = _relative_to_target(destination, target)
        if relative is None or artifact.logical_destination == CLONE_EXCLUDE_DESTINATION:
            continue
        if not _is_ignored(target, relative) and not _planned_exclude_covers(context, relative):
            blocked.append((artifact.artifact_id, "in_target_destination_not_ignored"))
            continue
        if tree_paths is None:
            tree_paths = _candidate_tree_paths(context)
        ancestors = {relative, *(str(parent) for parent in Path(relative).parents if str(parent) != ".")}
        if ancestors & tree_paths:
            blocked.append((artifact.artifact_id, "in_target_destination_tracked"))
    return tuple(resolved)


def _planned_exclude_covers(context: PlanContext, relative: str) -> bool:
    commit = context.candidate.fields.commit

    def read(template_ref: str) -> str:
        return _git(context.cache_repository, ("show", f"{commit}:{template_ref}"), "candidate clone-exclude template is unreadable", layout=context.cache_layout).decode("utf-8", "strict")

    return planned_exclude_covers(context.candidate.bundle.managed_artifacts, read, relative)


def _relative_to_target(destination: str, target: Path) -> str | None:
    try:
        return str(Path(destination).relative_to(target))
    except ValueError:
        return None


def _is_ignored(target: Path, relative: str) -> bool:
    completed = _run_git(target, ("check-ignore", "-q", "--no-index", "--", relative))
    if completed.returncode == 0:
        return True
    if completed.returncode == 1:
        return False
    raise SourceError(f"ignore rules could not be evaluated: {completed.stderr.decode('utf-8', 'replace').strip()}")


def _ignore_sources_digest(context: PlanContext, artifacts: tuple[tuple[ManagedArtifact, str], ...]) -> str | None:
    """Digest every ignore source the evaluation consulted, so a later change is ``probe_drift``."""
    target = Path(context.record.target.canonical_path)
    in_target = [_relative_to_target(destination, target) for _, destination in artifacts]
    relatives = [item for item in in_target if item is not None]
    if not relatives:
        return None
    digest = hashlib.sha256()
    for source in sorted(_consulted_ignore_sources(target, relatives)):
        digest.update(str(source).encode())
        digest.update(b"\0")
        digest.update(source.read_bytes() if source.is_file() else b"<absent>")
        digest.update(b"\0")
    return f"sha256:{digest.hexdigest()}"


def _consulted_ignore_sources(target: Path, relatives: list[str]) -> set[Path]:
    """Every ignore source ``check-ignore`` consulted, plus the two repository-scoped ones."""
    sources: set[Path] = {target / ".git" / "info" / "exclude"}
    for relative in relatives:
        completed = _run_git(target, ("check-ignore", "-v", "--no-index", "--", relative))
        for line in completed.stdout.decode("utf-8", "replace").splitlines():
            source = line.split(":", 1)[0].strip()
            if source and not source.startswith("<"):
                sources.add(target / source)
    excludes = _run_git(target, ("config", "--local", "--get", "core.excludesFile"))
    if excludes.returncode == 0 and excludes.stdout.strip():
        sources.add(Path(os.path.expanduser(excludes.stdout.decode("utf-8", "replace").strip())))
    return sources


def _not_applicable(context: PlanContext, operation: RuntimeOperation) -> RuntimeOperationPlan:
    return RuntimeOperationPlan(
        operation.operation_id,
        operation.operation_ref,
        operation.runner,
        operation.stage,
        operation.mutation_class,
        operation.rollback_class,
        operation.retry_policy,
        idempotency_key(context.candidate.fields.commit, operation.operation_id, context.record.instance_id),
        False,
        "not_applicable",
        (),
        (),
        {},
        operation.requires_confirmation,
    )


def _bridge_row(context: PlanContext, operation: RuntimeOperation) -> RuntimeOperationPlan:
    key = context.operator_selections.get("process_key")
    checkpoint = context.operator_selections.get("backup_checkpoint_id")
    action = f"bridge.{key}" if isinstance(key, str) else "bridge.operator_selection_required"
    return RuntimeOperationPlan(
        operation.operation_id,
        operation.operation_ref,
        operation.runner,
        operation.stage,
        operation.mutation_class,
        operation.rollback_class,
        operation.retry_policy,
        idempotency_key(context.candidate.fields.commit, operation.operation_id, context.record.instance_id),
        True,
        "pending",
        (action,),
        ("instance_bridge",),
        {},
        operation.requires_confirmation,
        checkpoint if isinstance(checkpoint, str) and operation.forward_only else None,
    )


def _operation_from_probe(
    context: PlanContext,
    operation: RuntimeOperation,
    inputs: dict[str, JsonValue],
    result: OperationResult,
    artifacts: tuple[tuple[ManagedArtifact, str], ...],
) -> tuple[RuntimeOperationPlan, tuple[ManagedArtifactState, ...]]:
    status = result.checkpoint_status
    now = {
        CheckpointStatus.VERIFIED: "verified",
        CheckpointStatus.PENDING: "pending",
        CheckpointStatus.BLOCKED: "blocked",
        CheckpointStatus.AWAITING_USER: "blocked",
        CheckpointStatus.FAILED: "failed",
        CheckpointStatus.NOT_APPLICABLE: "not_applicable",
    }.get(status)
    if now is None:
        raise AdapterProtocolError(f"probe for {operation.operation_ref} reported a mutation status")
    states = artifact_states(operation, result, artifacts) if operation.stage == "hydration" else ()
    row = RuntimeOperationPlan(
        operation.operation_id,
        operation.operation_ref,
        operation.runner,
        operation.stage,
        operation.mutation_class,
        operation.rollback_class,
        operation.retry_policy,
        idempotency_key(context.candidate.fields.commit, operation.operation_id, context.record.instance_id),
        True,
        now,
        tuple(action.id for action in result.planned_actions),
        tuple(action.target for action in result.planned_actions),
        inputs,
        operation.requires_confirmation,
    )
    return row, states


def artifact_states(
    operation: RuntimeOperation, result: OperationResult, artifacts: tuple[tuple[ManagedArtifact, str], ...]
) -> tuple[ManagedArtifactState, ...]:
    """Decode the seed's per-artifact evidence into the closed three-way state rows."""
    by_id = {artifact.artifact_id: (artifact, destination) for artifact, destination in artifacts}
    diffs = adopt_diffs(result, tuple(by_id))
    states: list[ManagedArtifactState] = []
    for item in result.evidence:
        evidence_id = str(item["id"])
        if not evidence_id.startswith("artifact."):
            continue
        facts = decode_facts(item["observed"])
        artifact_id = facts.get("artifact_id", "")
        if artifact_id not in by_id:
            raise AdapterProtocolError(f"adapter reported an undeclared artifact {artifact_id!r}")
        artifact, destination = by_id[artifact_id]
        if facts.get("destination") != destination:
            raise AdapterProtocolError(f"adapter resolved {artifact_id} to a destination the plan did not name")
        conflict = facts.get("conflict", "none")
        stamped = facts.get("stamped_digest", "none")
        expected = facts.get("expected_sha256", "none")
        current = facts.get("current_sha256", "none")
        states.append(
            ManagedArtifactState(
                artifact_id,
                artifact.kind,
                destination,
                facts.get("state", "unknown"),
                facts.get("action", "none"),
                None if stamped == "none" else stamped,
                artifact.template_digest,
                None if expected == "none" else expected,
                None if current == "none" else current,
                None if conflict == "none" else conflict,
                operation.operation_id,
                diffs.get(artifact_id, ()),
            )
        )
    return tuple(states)


@dataclass(frozen=True, slots=True)
class _LifecycleFacts:
    label: str
    plist_path: Path
    raw_plist: bytes | None
    topology: str | None
    plist_expected: str | None
    modules: tuple[str, ...]
    budget: int

    def unproven(self, reason: str = "lifecycle_strategy_unproven") -> LifecycleObservation:
        return _unproven(self.label, self.plist_path, self.plist_expected, self.modules, self.budget, reason)


def _lifecycle(context: PlanContext, artifact_states: tuple[ManagedArtifactState, ...]) -> tuple[LifecycleObservation, int]:
    """Section 7.1: probe, then fix exactly one strategy; anything unprovable blocks."""
    facts = _lifecycle_facts(context, artifact_states)
    if facts.raw_plist is None or facts.topology not in SUPPORTED_TOPOLOGIES:
        return facts.unproven(), 0
    if facts.topology == LEGACY_DIRECT:
        # A legacy_direct process is not behind a materialized release, so ``SOLET_RELEASE_ID`` is never set and it can
        # never attest: router cutover is structurally unreachable for it, whatever the roster or the record say (iss_6fd900ab).
        return _single_color_observation(context, facts)
    identity = context.record.service_identity
    roster = _selected_plugins(Path(context.record.target.canonical_path) / "profile" / "config" / "manifest.yaml")
    router_declared = identity.router_label is not None and identity.router_socket is not None
    router_on_roster = ROUTER_PLUGIN in roster
    if context.candidate.bundle.lifecycle.strategy == "single_color_required" or (not router_declared and not router_on_roster):
        return _single_color_observation(context, facts)
    if router_declared != router_on_roster:
        return facts.unproven(), 0
    printed = context.seams.launchctl(context.registry, "print", (f"gui/{context.seams.uid}/{identity.router_label}",), 30)
    if printed.returncode != 0:
        return facts.unproven(), 1
    observation, executions = _router_observation(context, facts)
    return observation, executions + 1


def _single_color_observation(context: PlanContext, facts: _LifecycleFacts) -> tuple[LifecycleObservation, int]:
    """Section 7.3: observe the service before fixing the single-colour strategy.

    A label that is not loaded, or a health other than ``healthy``, is
    ``unproven(service_offline_before_transition)``: an update is not the
    repair path for a dead service (the runbook's own Step 1 rule), and a
    ``bootstrap`` of a broken service would otherwise publish ``needs_attention``
    for a pre-existing condition.  A healthy service is single-colour only when
    the colour census (the process table) is exactly the launchd pid: a dormant
    job beside a serving sidecar, or an idle launchd colour beside one, is
    ``unproven(colour_outside_launchagent)`` because the restart would replace
    a process that is not serving (iss_75b87670).  The observation is journaled
    with the plan; its pid is the ``pid_before`` the post-restart check compares
    against and its ``colour_pids`` the census the restart starts from.
    """
    printed = context.seams.launchctl(context.registry, "print", (f"gui/{context.seams.uid}/{facts.label}",), 30)
    loaded = printed.returncode == 0
    pid = _printed_pid(printed.stdout) if loaded else None
    try:
        health = str(context.seams.read_health(context.registry, 30).get("status", ""))
    except (AdapterError, AdapterProtocolError):
        health = "unreachable"
    census = read_colour_census(context.seams.run_ps, Path(context.record.target.canonical_path))
    observation: dict[str, JsonValue] = {"loaded": loaded, "pid": pid, "health": health, "colour_pids": None if census is None else list(census)}
    reason = _single_color_refusal(loaded, pid, health, census)
    if reason is not None:
        return _unproven(facts.label, facts.plist_path, facts.plist_expected, facts.modules, facts.budget, reason, observation), 3
    return LifecycleObservation("single_color_restart", facts.topology, facts.label, str(facts.plist_path), facts.plist_expected, None, None, False, facts.modules, facts.budget, None, False, None, None, None, observation), 3


def _single_color_refusal(loaded: bool, pid: int | None, health: str, census: tuple[int, ...] | None) -> str | None:
    """The reason a single-colour restart is unproven, or ``None`` when launchd's pid is the one colour serving."""
    if not loaded or health != "healthy":
        return "service_offline_before_transition"
    if census is None:
        return "lifecycle_strategy_unproven"
    if not pid:
        # A dormant job with no colour anywhere is an offline service; one with colours is served from outside launchd.
        return COLOUR_OUTSIDE_LAUNCHAGENT if census else "service_offline_before_transition"
    return None if census == (pid,) else COLOUR_OUTSIDE_LAUNCHAGENT


def _printed_pid(stdout: str) -> int | None:
    for line in stdout.splitlines():
        stripped = line.strip()
        if stripped.startswith("pid = "):
            value = stripped.removeprefix("pid = ").strip()
            return int(value) if value.isdigit() else None
    return 0


def _lifecycle_facts(context: PlanContext, artifact_states: tuple[ManagedArtifactState, ...]) -> _LifecycleFacts:
    label = context.record.service_identity.launchagent_label
    plist_path = launchagent_plist_path(context.seams.home, label)
    plist_state = next((item for item in artifact_states if item.kind == "launchd_plist"), None)
    plist_expected = plist_state.expected_sha256 if plist_state is not None else file_sha256(plist_path)
    raw_plist = plist_path.read_bytes() if plist_path.is_file() else None
    topology = None if raw_plist is None else derive_launch_topology(raw_plist)
    lifecycle = context.candidate.bundle.lifecycle
    return _LifecycleFacts(label, plist_path, raw_plist, topology, plist_expected, lifecycle.verification_modules, lifecycle.readiness_budget_seconds)


def _router_observation(context: PlanContext, facts: _LifecycleFacts) -> tuple[LifecycleObservation, int]:
    """Attest the running process, bind the preview-time terms, and probe the seed's reconciliation contract."""
    record = context.record
    try:
        attestation = attest(context, "rec_" + uuid.uuid4().hex)
    except (AdapterError, AdapterProtocolError):
        return facts.unproven(), 1
    module_sha256, replaced = adapter_module_identity(context)
    # The probe runs against the plist as it is NOW (the seed measures it); the
    # approval separately binds ``plist_expected_sha256``, the digest hydration
    # leaves behind, which the apply-phase terms carry (section 7.3).
    terms = cutover_terms(attestation, cast(str, facts.topology), facts.label, plist_sha256(cast(bytes, facts.raw_plist)), module_sha256, replaced, facts.modules)
    fingerprint = cutover_fingerprint(name=record.name, target=record.target.canonical_path, seed=_seed_identity(context), files=_cutover_files(context), terms=terms)
    envelope = build_reconciliation_envelope(phase="probe", name=record.name, target_realpath=record.target.canonical_path, reconciliation_id="rec_" + uuid.uuid4().hex, approved_fingerprint=fingerprint, terms=terms, timeout_seconds=facts.budget)
    try:
        outcome = context.seams.invoke_reconciliation(context.registry, envelope, facts.budget)
    except (AdapterError, AdapterProtocolError):
        return facts.unproven(), 2
    receipt: dict[str, JsonValue] = {"status": outcome.status, "error_kind": outcome.error_kind, "observed_launch_topology": outcome.observed_launch_topology, "observed_launchagent_plist_sha256": outcome.observed_launchagent_plist_sha256, "probed": outcome.probed, "mutated": outcome.mutated}
    if outcome.refused or not outcome.probed or outcome.mutated is not False:
        return facts.unproven(), 2
    observation = LifecycleObservation("router_cutover", facts.topology, facts.label, str(facts.plist_path), facts.plist_expected, attestation.current_release_id, module_sha256, replaced, facts.modules, facts.budget, fingerprint, True, attestation.to_dict(), receipt, None)
    return observation, 2


def _unproven(label: str, plist_path: Path, expected: str | None, modules: tuple[str, ...], budget: int, reason: str, pre_transition: dict[str, JsonValue] | None = None) -> LifecycleObservation:
    return LifecycleObservation("unproven", None, label, str(plist_path), expected, None, None, False, modules, budget, None, False, None, None, reason, pre_transition)


def attest(context: PlanContext, reconciliation_id: str) -> AttestationObservation:
    """Read the running process's identity over the bridge (read-only, allowlisted)."""
    payload = context.seams.invoke_bridge(
        context.registry,
        ATTEST_PROCESS_KEY,
        {"reconciliation_id": reconciliation_id, "verification_modules": list(context.candidate.bundle.lifecycle.verification_modules)},
        "self_deployment.attest",
        60,
    )
    return AttestationObservation.from_payload(payload)


def cutover_terms(
    attestation: AttestationObservation,
    topology: str,
    label: str,
    plist_sha256_value: str,
    module_sha256: str,
    replaced: bool,
    modules: tuple[str, ...],
) -> CutoverTerms:
    return CutoverTerms(
        current_release_id=attestation.current_release_id,
        active_color=attestation.active_color,
        active_instance_id=attestation.active_instance_id,
        active_start_token=attestation.active_start_token,
        manifest_etag=attestation.manifest_etag,
        launch_topology=topology,
        launchagent_label=label,
        launchagent_plist_sha256=plist_sha256_value,
        adapter_module_sha256=module_sha256,
        adapter_module_replaced=replaced,
        source_surface_sha256=attestation.source_surface_sha256,
        release_surface_sha256=attestation.release_surface_sha256,
        verification_modules=modules,
    )


def adapter_module_identity(context: PlanContext) -> tuple[str, bool]:
    """Blob digest of the setup adapter at the candidate and whether the transition touched it."""
    blob = _git(context.cache_repository, ("show", f"{context.candidate.fields.commit}:{ADAPTER_MODULE_PATH}"), "candidate adapter module is unreadable", layout=context.cache_layout)
    digest = f"sha256:{hashlib.sha256(blob).hexdigest()}"
    baseline = context.baseline_commit
    if baseline == context.candidate.fields.commit:
        return digest, False
    completed = _run_git(context.cache_repository, ("diff-tree", "-r", "--name-only", "--no-renames", baseline, context.candidate.fields.commit), layout=context.cache_layout)
    if completed.returncode != 0:
        return digest, True
    touched = set(completed.stdout.decode("utf-8", "replace").splitlines())
    return digest, ADAPTER_MODULE_PATH in touched


def _seed_identity(context: PlanContext) -> dict[str, JsonValue]:
    fields = context.candidate.fields
    return {"repository": fields.repository, "commit": fields.commit, "tree": fields.tree_hash, "tag": fields.release_tag, "descriptor_digest": context.candidate.descriptor_digest}


def _cutover_files(context: PlanContext) -> tuple[dict[str, JsonValue], ...]:
    rows: list[dict[str, JsonValue]] = [{"path": "existing_install_flow.json", "sha256": context.candidate.bundle_digest}]
    rows.extend({"path": artifact.template_ref, "sha256": artifact.template_digest} for artifact in context.candidate.bundle.managed_artifacts)
    return tuple(rows)


def _run_git(cwd: Path, args: tuple[str, ...], *, layout: GitLayout = GitLayout.WORKTREE) -> subprocess.CompletedProcess[bytes]:
    """One closed read-only vector through the shared hardened, pinned Git surface (iss_836499b3 B1, R2-1)."""
    return run_target_git(args, cwd=cwd, layout=layout)


def _git(cache_repository: Path, args: tuple[str, ...], error: str, *, layout: GitLayout) -> bytes:
    """A read of the candidate's history, pinned as ``PlanContext.cache_layout`` says."""
    completed = _run_git(cache_repository, args, layout=layout)
    if completed.returncode:
        raise SourceError(f"{error}: {completed.stderr.decode('utf-8', 'replace').strip()}")
    return completed.stdout


def stage_rank(stage: str) -> int:
    return STAGE_ORDER[stage]
