"""Closed parser for the candidate-committed existing-install transition bundle.

The bundle is the single executable authority for every runtime action Step 5
may take after the source fast-forward (design section 2.3).  This module is
the Manager-side reader: it validates the closed shape by hand (the Manager
carries no JSON-schema dependency), enumerates the closed ``existing::``
vocabulary that both the Manager registry and the seed dispatcher must agree
on (section 3.1), and refuses every malformed declaration before any plan is
rendered.  It never touches a target and never executes anything.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Literal

from .contracts import contract_digest_from_bytes, transition_bundle_filenames
from .errors import ContractError, OperationTypeAmbiguousError
from .host_platform import HostPlatform
from .models import JsonValue, OperationType

__all__ = [
    "DECLARABLE_OPERATION_REFS",
    "EXISTING_OPERATIONS",
    "FLOW_ID",
    "IDEMPOTENCY_KEY_TEMPLATE",
    "SEED_SIDE_OPERATION_REFS",
    "STAGE_ORDER",
    "SYNTHESISED_OPERATION_TYPES",
    "DependencyPiece",
    "ExistingOperation",
    "LifecycleDeclaration",
    "ManagedArtifact",
    "RuntimeOperation",
    "SupportedPredecessor",
    "TransitionBundle",
    "operation_type",
    "parse_transition_bundle",
    "transition_bundle_digest",
]

FLOW_ID = "existing-install"
SCHEMA_VERSION = 1
IDEMPOTENCY_KEY_TEMPLATE = "sha256(candidate_commit, operation_id, instance_id)"
SOURCE_OPERATION_REF = "existing::source.fast_forward"
SOURCE_PLANNED_ACTIONS = (
    "manager.acquire_update_candidate",
    "target.fetch_exact_candidate",
    "target.fast_forward_exact_candidate",
)
STAGE_ORDER: dict[str, int] = {
    "dependencies": 0,
    "migrations_pre": 1,
    "hydration": 2,
    "lifecycle": 3,
    "runtime_reconcile": 4,
}
_RUNNERS = frozenset({"bootstrap", "target_adapter", "reconciliation", "instance_bridge", "launchctl", "manager"})
_MUTATION_CLASSES = frozenset(
    {
        "venv",
        "filesystem_managed_artifact",
        "launchd",
        "shell_startup",
        "coding_agent_config",
        "plugin_cache",
        "knowledge_index",
        "process_lifecycle",
        "database_additive",
        "database_forward_only",
    }
)
_ROLLBACK_CLASSES = frozenset({"reversible", "runtime_previous_release", "backup_required", "forward_only"})
_RETRY_POLICIES = frozenset({"retry_safe", "manual"})
_ARTIFACT_KINDS = frozenset({"managed_block", "rendered_whole", "launchd_plist"})
_PRESERVATION_CLASSES = frozenset({"operator_owned_with_managed_block", "manager_generated_whole", "preserved_never"})
_STRATEGIES = frozenset({"router_preferred", "single_color_required"})
_READINESS_SIGNAL = "bridge_health_healthy"
_TEMPLATE_ROOT = "plugins/github_midwife_plugin/knowledge_base/hydration_templates/"
_DESTINATION_ROOTS = ("{HOME}/", "{TARGET}/", "{PROFILE_HOME}/")
# Static preserved-never prefixes (section 6.3).  The install-specific ignore
# and tracked-tree rules live in the runtime plan, where a real target exists.
_PRESERVED_NEVER_PREFIXES = (
    "{TARGET}/profile/config/",
    "{TARGET}/profile/data/",
    "{TARGET}/profile/credentials/",
    "{TARGET}/.git/",
    "{PROFILE_HOME}/config/",
    "{PROFILE_HOME}/data/",
    "{PROFILE_HOME}/credentials/",
    "{HOME}/.codex/sessions/",
    "{HOME}/.codex/archived_sessions/",
    "{HOME}/.claude/projects/",
    "{HOME}/Library/Keychains/",
)
_PRESERVED_NEVER_EXACT = frozenset(
    {
        "{TARGET}/AGENTS.md",
        "{TARGET}/CLAUDE.md",
        "{HOME}/.codex/history.jsonl",
        "{HOME}/.codex/session_index.jsonl",
    }
)
_IDENTIFIER = re.compile(r"^[a-z][a-z0-9_.-]{1,127}$")
_COMMIT = re.compile(r"^[0-9a-f]{40}$")
_SHA256_HEX = re.compile(r"^[0-9a-f]{64}$")
_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
_UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
_REPOSITORY = re.compile(r"^https://github\.com/[^/]+/[^/]+\.git$")
_DISTRIBUTION = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_RELATIVE_PATH = re.compile(r"^(plugins/[a-z][a-z0-9_]*|ananta|solet_setup_contracts)$")
_MACHINE = re.compile(r"^(arm64|x86_64)$")
_MODULE = re.compile(r"^[a-z_][a-z0-9_]*(\.[a-z_][a-z0-9_]*)*$")
_TEMPLATE_NAME = re.compile(r"^[A-Za-z0-9_.-]+$")
_MARKER_BEGIN = "SOLET {NAME} v{TEMPLATE_DIGEST8}"
_MARKER_END = "SOLET {NAME}"
_STAMP = "rendered-from: {TEMPLATE_REF}@{TEMPLATE_DIGEST}"

type Side = Literal["seed", "manager"]


@dataclass(frozen=True, slots=True)
class ExistingOperation:
    """One member of the closed ``existing::`` vocabulary (design section 3.1)."""

    operation_ref: str
    runner: str
    stage: str
    side: Side
    declarable: bool


# The closed vocabulary.  ``declarable`` members may appear in a bundle's
# ``runtime_operations``; the rest are Manager-synthesised at plan time and a
# bundle that names one is refused.  Adding a member is a Manager release plus
# a seed release; a smoke proves the seed-side subset is byte-equal.
EXISTING_OPERATIONS: tuple[ExistingOperation, ...] = (
    ExistingOperation("existing::dependencies.reconcile", "bootstrap", "dependencies", "seed", True),
    ExistingOperation("existing::migration.solet_rename", "target_adapter", "migrations_pre", "seed", True),
    ExistingOperation("existing::migration.export_root_containment", "target_adapter", "migrations_pre", "seed", True),
    ExistingOperation("existing::migration.plugin_transition", "target_adapter", "migrations_pre", "seed", True),
    ExistingOperation("existing::hydration.reconcile", "target_adapter", "hydration", "seed", True),
    ExistingOperation("existing::hydration.restore", "manager", "hydration", "manager", False),
    ExistingOperation("existing::autostart.reconcile", "target_adapter", "hydration", "seed", True),
    ExistingOperation("existing::lifecycle.cutover", "reconciliation", "lifecycle", "manager", False),
    ExistingOperation("existing::lifecycle.restart_single_color", "launchctl", "lifecycle", "manager", False),
    ExistingOperation("existing::runtime.platform_migration", "instance_bridge", "runtime_reconcile", "manager", True),
    ExistingOperation("existing::runtime.knowledge_reinstall", "instance_bridge", "runtime_reconcile", "manager", False),
    ExistingOperation("existing::runtime.plugin_cache_refresh", "target_adapter", "runtime_reconcile", "seed", True),
    ExistingOperation("existing::runtime.readiness", "instance_bridge", "lifecycle", "manager", False),
)
_OPERATIONS_BY_REF = {item.operation_ref: item for item in EXISTING_OPERATIONS}
#: Manager-synthesised rows are typed at synthesis (Step 6 section 4.2); this
#: table is the one place their ``operation_ref -> OperationType`` binding lives.
SYNTHESISED_OPERATION_TYPES: dict[str, OperationType] = {
    "existing::lifecycle.cutover": OperationType.PROCESS_LIFECYCLE,
    "existing::lifecycle.restart_single_color": OperationType.PROCESS_LIFECYCLE,
    "existing::runtime.readiness": OperationType.READINESS,
    "existing::runtime.knowledge_reinstall": OperationType.KNOWLEDGE_REINSTALL,
}
_BACKED_UP_MUTATIONS = frozenset({"filesystem_managed_artifact", "launchd", "shell_startup", "coding_agent_config"})
#: The exact ``(runner, mutation_class, rollback_class, retry_policy)`` rows of Step 6 section 4.2 (T1, T6, T7, T9, T3b).
_EXACT_OPERATION_TYPES: dict[tuple[str, str, str, str], OperationType] = {
    ("bootstrap", "venv", "reversible", "retry_safe"): OperationType.CLOSURE_REPAIR,
    ("target_adapter", "plugin_cache", "reversible", "retry_safe"): OperationType.PLUGIN_CACHE_REFRESH,
    ("instance_bridge", "database_additive", "reversible", "retry_safe"): OperationType.ADDITIVE_PLATFORM_MIGRATION,
    ("instance_bridge", "database_additive", "reversible", "manual"): OperationType.MANUAL_ADDITIVE_PLATFORM_MIGRATION,
    ("instance_bridge", "database_forward_only", "forward_only", "manual"): OperationType.FORWARD_ONLY_MIGRATION,
}
DECLARABLE_OPERATION_REFS = tuple(item.operation_ref for item in EXISTING_OPERATIONS if item.declarable)
SEED_SIDE_OPERATION_REFS = tuple(item.operation_ref for item in EXISTING_OPERATIONS if item.side == "seed")


@dataclass(frozen=True, slots=True)
class SupportedPredecessor:
    """One predecessor identity the existing-install flow recognises.

    A ``legacy_anchor_id`` row can be a reserved, currently-inert anchor
    rather than a known real seed: see the ``supported_predecessor``
    schema description (iss_3272e5f6) for the verified ``stable-pre-manager-
    seed-v1`` case, whose identity fields never match a real
    ``solet-public/macos-bizops`` clone.
    """

    repository: str
    commit: str
    tree: str
    provenance_sha256: str
    seed_id: str
    origin_id: str
    manifest_sha256: str
    legacy_anchor_id: str | None


@dataclass(frozen=True, slots=True)
class RuntimeOperation:
    operation_id: str
    operation_ref: str
    runner: str
    stage: str
    mutation_class: str
    rollback_class: str
    retry_policy: str
    idempotency_key: str
    applies_when: tuple[str, ...] | None
    precondition_probe_refs: tuple[str, ...]
    postcondition_probe_refs: tuple[str, ...]
    public_inputs: tuple[str, ...]
    requires_confirmation: bool

    def applies_to(self, predecessor_commit: str) -> bool:
        return self.applies_when is None or predecessor_commit in self.applies_when

    @property
    def forward_only(self) -> bool:
        return self.mutation_class == "database_forward_only"

    @property
    def operation_type(self) -> OperationType:
        return operation_type(self)


def operation_type(op: RuntimeOperation) -> OperationType:
    """Classify one declared row by its closed fields alone (Step 6 section 4.2, T1-T9).

    Total over the table and nothing else: a ``(runner, mutation_class,
    rollback_class, retry_policy)`` tuple the executor has no resume rule for
    is refused at parse time rather than "interpreted" later.
    """
    key = (op.runner, op.mutation_class, op.rollback_class, op.retry_policy)
    if op.runner == "target_adapter" and op.mutation_class in _BACKED_UP_MUTATIONS and op.rollback_class == "backup_required":
        return OperationType.BACKED_UP_ARTIFACT if op.retry_policy == "retry_safe" else OperationType.MANUAL_TARGET_MIGRATION
    exact = _EXACT_OPERATION_TYPES.get(key)
    if exact is OperationType.FORWARD_ONLY_MIGRATION and not op.requires_confirmation:
        exact = None
    if exact is None:
        raise OperationTypeAmbiguousError(f"runtime_operation {op.operation_id} declares {key!r}, which matches no closed resume-rule type")
    return exact


@dataclass(frozen=True, slots=True)
class ManagedArtifact:
    artifact_id: str
    kind: str
    logical_destination: str
    preservation_class: str
    marker_begin: str | None
    marker_end: str | None
    stamp: str | None
    template_ref: str
    template_digest: str
    previous_template_digests: tuple[str, ...]

    @property
    def in_target(self) -> bool:
        return self.logical_destination.startswith(("{TARGET}/", "{PROFILE_HOME}/"))

    def resolve_destination(self, *, home: str, target: str, name: str, profile_home: str) -> str:
        return (
            self.logical_destination.replace("{HOME}", home)
            .replace("{TARGET}", target)
            .replace("{PROFILE_HOME}", profile_home)
            .replace("{NAME}", name)
        )


@dataclass(frozen=True, slots=True)
class DependencyPiece:
    distribution: str
    relative_path: str
    #: A ``host_profiles`` name this piece needs (iss_6d26db73, rul_385dac24); ``None`` applies on every host.
    requires_host: str | None = None


@dataclass(frozen=True, slots=True)
class HostProfile:
    """A measured host class the release gates pieces on: the one place its threshold is declared."""

    name: str
    macos_major_min: int
    machine: str

    def admits(self, platform: HostPlatform) -> bool:
        return platform.macos_major >= self.macos_major_min and platform.machine == self.machine


@dataclass(frozen=True, slots=True)
class LifecycleDeclaration:
    strategy: str
    readiness_budget_seconds: int
    readiness_release_signal: str
    verification_modules: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class TransitionBundle:
    flow_id: str
    schema_version: int
    supported_predecessors: tuple[SupportedPredecessor, ...]
    source_planned_actions: tuple[str, ...]
    runtime_operations: tuple[RuntimeOperation, ...]
    managed_artifacts: tuple[ManagedArtifact, ...]
    closure_additions: tuple[DependencyPiece, ...]
    closure_removals: tuple[DependencyPiece, ...]
    knowledge_removals: tuple[str, ...]
    lifecycle: LifecycleDeclaration
    host_profiles: tuple[HostProfile, ...] = ()

    def host_profile(self, name: str) -> HostProfile:
        for item in self.host_profiles:
            if item.name == name:
                return item
        raise ContractError(f"transition bundle declares no host profile {name!r}")

    def predecessor_for(self, commit: str, tree: str) -> SupportedPredecessor | None:
        return next((row for row in self.supported_predecessors if row.commit == commit and row.tree == tree), None)

    def artifact(self, artifact_id: str) -> ManagedArtifact:
        for item in self.managed_artifacts:
            if item.artifact_id == artifact_id:
                return item
        raise ContractError(f"transition bundle declares no artifact {artifact_id!r}")

    def operations_in_stage(self, stage: str) -> tuple[RuntimeOperation, ...]:
        return tuple(item for item in self.runtime_operations if item.stage == stage)


def transition_bundle_digest(files: dict[str, bytes]) -> str:
    """Digest exactly the closed transition file set, refusing any other set."""
    if frozenset(files) != frozenset(transition_bundle_filenames()):
        raise ContractError("transition bundle file set is not the closed three-file set")
    return contract_digest_from_bytes(files)


def parse_transition_bundle(raw_bytes: bytes) -> TransitionBundle:
    """Parse and closed-validate one ``existing_install_flow.json`` document."""
    try:
        raw = json.loads(raw_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ContractError(f"transition bundle is unreadable: {exc}") from exc
    root = _object(
        raw,
        {
            "flow_id",
            "schema_version",
            "supported_predecessors",
            "source_operation",
            "runtime_operations",
            "managed_artifacts",
            "dependency_closure",
            "knowledge_removals",
            "lifecycle",
        },
        "bundle",
        optional={"host_profiles"},
    )
    if root["flow_id"] != FLOW_ID or root["schema_version"] != SCHEMA_VERSION or isinstance(root["schema_version"], bool):
        raise ContractError("transition bundle flow identity is invalid")
    predecessors = tuple(_predecessor(item) for item in _array(root["supported_predecessors"], "supported_predecessors", minimum=1))
    _unique([row.commit for row in predecessors], "supported_predecessors.commit")
    source_actions = _source_operation(root["source_operation"])
    operations = _operations(root["runtime_operations"], predecessors)
    artifacts = tuple(_artifact(item) for item in _array(root["managed_artifacts"], "managed_artifacts"))
    _unique([item.artifact_id for item in artifacts], "managed_artifacts.artifact_id")
    profiles = _host_profiles(root.get("host_profiles", {}))
    additions, removals = _closure(root["dependency_closure"], {item.name for item in profiles})
    knowledge = _identifiers(root["knowledge_removals"], "knowledge_removals")
    return TransitionBundle(
        FLOW_ID,
        SCHEMA_VERSION,
        predecessors,
        source_actions,
        operations,
        artifacts,
        additions,
        removals,
        knowledge,
        _lifecycle(root["lifecycle"]),
        profiles,
    )


def _operations(value: JsonValue, predecessors: tuple[SupportedPredecessor, ...]) -> tuple[RuntimeOperation, ...]:
    operations = tuple(_runtime_operation(item, predecessors) for item in _array(value, "runtime_operations"))
    _unique([item.operation_id for item in operations], "runtime_operations.operation_id")
    _require_stage_order(operations)
    for item in operations:
        operation_type(item)
    return operations


def _closure(value: JsonValue, profiles: set[str]) -> tuple[tuple[DependencyPiece, ...], tuple[DependencyPiece, ...]]:
    closure = _object(value, {"additions", "removals"}, "dependency_closure")
    additions = tuple(_piece(item, profiles) for item in _array(closure["additions"], "dependency_closure.additions"))
    removals = tuple(_piece(item, profiles) for item in _array(closure["removals"], "dependency_closure.removals"))
    _unique([item.distribution for item in (*additions, *removals)], "dependency_closure.distribution")
    return additions, removals


def _host_profiles(value: JsonValue) -> tuple[HostProfile, ...]:
    if not isinstance(value, dict):
        raise ContractError("transition bundle host_profiles must be an object")
    profiles: list[HostProfile] = []
    for name, item in sorted(value.items()):
        row = _object(item, {"macos_major_min", "machine"}, "host_profile")
        major = row["macos_major_min"]
        if isinstance(major, bool) or not isinstance(major, int) or not 11 <= major <= 99:
            raise ContractError("host_profile.macos_major_min must be an integer in 11..99")
        profiles.append(HostProfile(_pattern(name, _IDENTIFIER, "host_profile name"), major, _pattern(row["machine"], _MACHINE, "host_profile.machine")))
    return tuple(profiles)


def _predecessor(value: JsonValue) -> SupportedPredecessor:
    row = _object(
        value,
        {"repository", "commit", "tree", "provenance_sha256", "seed_id", "origin_id", "manifest_sha256", "legacy_anchor_id"},
        "supported_predecessor",
    )
    anchor = row["legacy_anchor_id"]
    if anchor is not None and (not isinstance(anchor, str) or not anchor):
        raise ContractError("supported_predecessor.legacy_anchor_id must be a non-empty string or null")
    return SupportedPredecessor(
        _pattern(row["repository"], _REPOSITORY, "supported_predecessor.repository"),
        _pattern(row["commit"], _COMMIT, "supported_predecessor.commit"),
        _pattern(row["tree"], _COMMIT, "supported_predecessor.tree"),
        _pattern(row["provenance_sha256"], _SHA256_HEX, "supported_predecessor.provenance_sha256"),
        _pattern(row["seed_id"], _UUID, "supported_predecessor.seed_id"),
        _pattern(row["origin_id"], _UUID, "supported_predecessor.origin_id"),
        _pattern(row["manifest_sha256"], _SHA256_HEX, "supported_predecessor.manifest_sha256"),
        anchor,
    )


def _source_operation(value: JsonValue) -> tuple[str, ...]:
    row = _object(value, {"operation_ref", "runner", "planned_actions"}, "source_operation")
    if row["operation_ref"] != SOURCE_OPERATION_REF or row["runner"] != "manager":
        raise ContractError("source_operation must be the Step-4 fast-forward operation")
    actions = tuple(_text(item, "source_operation.planned_actions") for item in _array(row["planned_actions"], "source_operation.planned_actions"))
    if actions != SOURCE_PLANNED_ACTIONS:
        raise ContractError("source_operation.planned_actions must be the exact Step-4 action list")
    return actions


def _runtime_operation(value: JsonValue, predecessors: tuple[SupportedPredecessor, ...]) -> RuntimeOperation:
    row = _object(
        value,
        {
            "operation_id",
            "operation_ref",
            "runner",
            "stage",
            "mutation_class",
            "rollback_class",
            "retry_policy",
            "idempotency_key",
            "applies_when",
            "precondition_probe_refs",
            "postcondition_probe_refs",
            "public_inputs",
            "requires_confirmation",
        },
        "runtime_operation",
    )
    ref, member = _declarable_member(row)
    runner, stage = _registry_binding(row, ref, member)
    mutation, rollback, retry, confirmation = _rollback_terms(row)
    if row["idempotency_key"] != IDEMPOTENCY_KEY_TEMPLATE:
        raise ContractError("runtime_operation.idempotency_key must be the one permitted template")
    return RuntimeOperation(
        _identifier(row["operation_id"], "runtime_operation.operation_id"),
        ref,
        runner,
        stage,
        mutation,
        rollback,
        retry,
        IDEMPOTENCY_KEY_TEMPLATE,
        _applies_when(row["applies_when"], predecessors),
        _identifiers(row["precondition_probe_refs"], "runtime_operation.precondition_probe_refs", minimum=1),
        _identifiers(row["postcondition_probe_refs"], "runtime_operation.postcondition_probe_refs", minimum=1),
        _identifiers(row["public_inputs"], "runtime_operation.public_inputs"),
        confirmation,
    )


def _declarable_member(row: dict[str, JsonValue]) -> tuple[str, ExistingOperation]:
    ref = _text(row["operation_ref"], "runtime_operation.operation_ref")
    member = _OPERATIONS_BY_REF.get(ref)
    if member is None or not member.declarable:
        raise ContractError(f"runtime_operation.operation_ref is outside the closed declarable registry: {ref!r}")
    return ref, member


def _registry_binding(row: dict[str, JsonValue], ref: str, member: ExistingOperation) -> tuple[str, str]:
    runner = _text(row["runner"], "runtime_operation.runner")
    stage = _text(row["stage"], "runtime_operation.stage")
    if runner not in _RUNNERS or runner != member.runner:
        raise ContractError(f"runtime_operation {ref} declares runner {runner!r}; the registry binds {member.runner!r}")
    if stage not in STAGE_ORDER or stage != member.stage:
        raise ContractError(f"runtime_operation {ref} declares stage {stage!r}; the registry binds {member.stage!r}")
    return runner, stage


def _rollback_terms(row: dict[str, JsonValue]) -> tuple[str, str, str, bool]:
    mutation = _one_of(row["mutation_class"], _MUTATION_CLASSES, "runtime_operation.mutation_class")
    rollback = _one_of(row["rollback_class"], _ROLLBACK_CLASSES, "runtime_operation.rollback_class")
    retry = _one_of(row["retry_policy"], _RETRY_POLICIES, "runtime_operation.retry_policy")
    confirmation = row["requires_confirmation"]
    if not isinstance(confirmation, bool):
        raise ContractError("runtime_operation.requires_confirmation must be boolean")
    if mutation == "database_forward_only" and (rollback != "forward_only" or retry != "manual" or not confirmation):
        raise ContractError("a database_forward_only operation must be forward_only, manual, and confirmed")
    if rollback == "forward_only" and mutation != "database_forward_only":
        raise ContractError("rollback_class forward_only is reserved for database_forward_only operations")
    return mutation, rollback, retry, confirmation


def _applies_when(value: JsonValue, predecessors: tuple[SupportedPredecessor, ...]) -> tuple[str, ...] | None:
    if value == "any":
        return None
    row = _object(value, {"predecessor_commits"}, "runtime_operation.applies_when")
    commits = tuple(_pattern(item, _COMMIT, "applies_when.predecessor_commits") for item in _array(row["predecessor_commits"], "applies_when.predecessor_commits"))
    _unique(list(commits), "applies_when.predecessor_commits")
    supported = {item.commit for item in predecessors}
    unknown = sorted(set(commits) - supported)
    if unknown:
        raise ContractError(f"applies_when names commits outside supported_predecessors: {unknown}")
    return commits


def _require_stage_order(operations: tuple[RuntimeOperation, ...]) -> None:
    ranks = [STAGE_ORDER[item.stage] for item in operations]
    if ranks != sorted(ranks):
        raise ContractError("runtime_operations stage order must be non-decreasing")
    forward_only = [index for index, item in enumerate(operations) if item.forward_only]
    if forward_only and forward_only[-1] != len(operations) - 1:
        raise ContractError("a database_forward_only operation must be the last runtime operation")
    if len(forward_only) > 1:
        raise ContractError("at most one database_forward_only operation may be declared")


def _artifact(value: JsonValue) -> ManagedArtifact:
    row = _object(
        value,
        {
            "artifact_id",
            "kind",
            "logical_destination",
            "preservation_class",
            "marker",
            "stamp",
            "template_ref",
            "template_digest",
            "previous_template_digests",
        },
        "managed_artifact",
    )
    kind = _one_of(row["kind"], _ARTIFACT_KINDS, "managed_artifact.kind")
    destination = _destination(row)
    preservation = _preservation(row, kind)
    marker_begin, marker_end = _marker(row["marker"], kind)
    stamp = _stamp(row["stamp"], kind)
    template_ref = _text(row["template_ref"], "managed_artifact.template_ref")
    if not template_ref.startswith(_TEMPLATE_ROOT) or _TEMPLATE_NAME.fullmatch(template_ref.removeprefix(_TEMPLATE_ROOT)) is None:
        raise ContractError(f"managed_artifact.template_ref is outside the hydration templates: {template_ref!r}")
    previous = tuple(_pattern(item, _DIGEST, "managed_artifact.previous_template_digests") for item in _array(row["previous_template_digests"], "managed_artifact.previous_template_digests"))
    _unique(list(previous), "managed_artifact.previous_template_digests")
    return ManagedArtifact(
        _identifier(row["artifact_id"], "managed_artifact.artifact_id"),
        kind,
        destination,
        preservation,
        marker_begin,
        marker_end,
        stamp,
        template_ref,
        _pattern(row["template_digest"], _DIGEST, "managed_artifact.template_digest"),
        previous,
    )


def _destination(row: dict[str, JsonValue]) -> str:
    destination = _text(row["logical_destination"], "managed_artifact.logical_destination")
    if not destination.startswith(_DESTINATION_ROOTS) or "/../" in destination or destination.endswith("/"):
        raise ContractError(f"managed_artifact.logical_destination is not rooted at a closed template root: {destination!r}")
    if destination in _PRESERVED_NEVER_EXACT or destination.startswith(_PRESERVED_NEVER_PREFIXES):
        raise ContractError(f"managed_artifact destination is inside a preserved-never surface: {destination!r}")
    return destination


def _preservation(row: dict[str, JsonValue], kind: str) -> str:
    preservation = _one_of(row["preservation_class"], _PRESERVATION_CLASSES, "managed_artifact.preservation_class")
    if preservation == "preserved_never":
        raise ContractError("a managed artifact cannot declare preservation_class preserved_never")
    if kind == "managed_block" and preservation != "operator_owned_with_managed_block":
        raise ContractError("a managed_block artifact must be operator_owned_with_managed_block")
    if kind != "managed_block" and preservation != "manager_generated_whole":
        raise ContractError("a whole-file artifact must be manager_generated_whole")
    return preservation


def _marker(value: JsonValue, kind: str) -> tuple[str | None, str | None]:
    if kind != "managed_block":
        if value is not None:
            raise ContractError("only a managed_block artifact declares a marker")
        return None, None
    row = _object(value, {"begin", "end"}, "managed_artifact.marker")
    begin = _text(row["begin"], "managed_artifact.marker.begin")
    end = _text(row["end"], "managed_artifact.marker.end")
    if _MARKER_BEGIN not in begin or _MARKER_END not in end or "\n" in begin or "\n" in end:
        raise ContractError("managed_artifact.marker must carry the versioned begin and named end templates")
    return begin, end


def _stamp(value: JsonValue, kind: str) -> str | None:
    if kind == "managed_block":
        if value is not None:
            raise ContractError("a managed_block artifact carries its version in the marker, not a stamp")
        return None
    stamp = _text(value, "managed_artifact.stamp")
    if _STAMP not in stamp or "\n" in stamp:
        raise ContractError("managed_artifact.stamp must carry the rendered-from template")
    return stamp


def _piece(value: JsonValue, profiles: set[str]) -> DependencyPiece:
    row = _object(value, {"distribution", "relative_path"}, "dependency_piece", optional={"requires_host"})
    requires = row.get("requires_host")
    if requires is not None and requires not in profiles:
        raise ContractError(f"dependency_piece.requires_host names no declared host profile: {requires!r}")
    return DependencyPiece(
        _pattern(row["distribution"], _DISTRIBUTION, "dependency_piece.distribution"),
        _pattern(row["relative_path"], _RELATIVE_PATH, "dependency_piece.relative_path"),
        requires,
    )


def _lifecycle(value: JsonValue) -> LifecycleDeclaration:
    row = _object(value, {"strategy", "readiness_budget_seconds", "readiness_release_signal", "verification_modules"}, "lifecycle")
    budget = row["readiness_budget_seconds"]
    if isinstance(budget, bool) or not isinstance(budget, int) or not 1 <= budget <= 900:
        raise ContractError("lifecycle.readiness_budget_seconds must be an integer in 1..900")
    if row["readiness_release_signal"] != _READINESS_SIGNAL:
        raise ContractError("lifecycle.readiness_release_signal must be bridge_health_healthy")
    modules = tuple(_pattern(item, _MODULE, "lifecycle.verification_modules") for item in _array(row["verification_modules"], "lifecycle.verification_modules", minimum=1))
    if tuple(sorted(set(modules))) != modules:
        raise ContractError("lifecycle.verification_modules must be unique and sorted")
    return LifecycleDeclaration(_one_of(row["strategy"], _STRATEGIES, "lifecycle.strategy"), budget, _READINESS_SIGNAL, modules)


def _object(value: JsonValue, keys: set[str], label: str, *, optional: frozenset[str] | set[str] = frozenset()) -> dict[str, JsonValue]:
    if not isinstance(value, dict) or not keys <= set(value) <= keys | optional:
        raise ContractError(f"transition bundle {label} does not match its closed key set")
    return value


def _array(value: JsonValue, label: str, *, minimum: int = 0) -> list[JsonValue]:
    if not isinstance(value, list) or len(value) < minimum:
        raise ContractError(f"transition bundle {label} must be an array of at least {minimum} items")
    return value


def _text(value: JsonValue, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise ContractError(f"transition bundle {label} must be a non-empty string")
    return value


def _pattern(value: JsonValue, pattern: re.Pattern[str], label: str) -> str:
    text = _text(value, label)
    if pattern.fullmatch(text) is None:
        raise ContractError(f"transition bundle {label} is malformed: {text!r}")
    return text


def _identifier(value: JsonValue, label: str) -> str:
    return _pattern(value, _IDENTIFIER, label)


def _identifiers(value: JsonValue, label: str, *, minimum: int = 0) -> tuple[str, ...]:
    items = tuple(_identifier(item, label) for item in _array(value, label, minimum=minimum))
    _unique(list(items), label)
    return items


def _one_of(value: JsonValue, allowed: frozenset[str], label: str) -> str:
    text = _text(value, label)
    if text not in allowed:
        raise ContractError(f"transition bundle {label} is outside its closed vocabulary: {text!r}")
    return text


def _unique(values: list[str], label: str) -> None:
    if len(values) != len(set(values)):
        raise ContractError(f"transition bundle {label} contains duplicates")
