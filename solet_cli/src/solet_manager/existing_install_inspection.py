# pyright: reportMissingTypeStubs=false
"""Read-only, descriptor-pinned inspection of an existing Solet checkout.

This module is intentionally separate from the legacy ``solet inspect``
diagnostic.  It has no target execution or lifecycle capability: it only reads
manager-shipped identity data and a pinned Git worktree through a closed set of
Git queries.
"""

from __future__ import annotations

import os
import stat
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path, PurePosixPath
from typing import BinaryIO, Literal, Never, Protocol, cast

from .maintenance_inventory import parse_maintenance_inventory_bytes
from .models import CommandResult, ExitCode, JsonValue
from .paths import ManagerPaths
from .release_lock import SeedLock
from .target_git import run_target_git


class InspectionStatus(StrEnum):
    VERIFIED = "verified"
    MISSING = "missing"
    FAILED = "failed"
    UNKNOWN = "unknown"
    NOT_APPLICABLE = "not_applicable"


class ExistingInstallClass(StrEnum):
    CLEAN_FAST_FORWARD_SEED_CLONE = "clean_fast_forward_seed_clone"
    LOCAL_CHANGES_PRESENT = "local_changes_present"
    BLOCKING_LOCAL_STATE = "blocking_local_state"
    INSPECTION_INCOMPLETE = "inspection_incomplete"
    DIVERGED_SEED_HISTORY = "diverged_seed_history"
    REVIEWED_HISTORICAL_REPOSITORY = "reviewed_historical_repository"
    UNKNOWN_REPOSITORY_CANONICAL_COMMIT = "unknown_repository_canonical_commit"
    SOURCE_IDENTITY_UNPROVEN = "source_identity_unproven"
    LEGACY_PROVENANCE = "legacy_provenance"
    PROVENANCE_UNAVAILABLE = "provenance_unavailable"
    PRE_MANAGER_SEED_CLONE = "pre_manager_seed_clone"
    DEVELOPMENT_CHECKOUT = "development_checkout"


class ProvenanceCondition(StrEnum):
    STRICT = "strict"
    MISSING = "missing"
    MALFORMED = "malformed"
    UNKNOWN = "unknown"


class InspectionAnchorKind(StrEnum):
    CURRENT_CHANNEL = "current_channel"
    LEGACY_PROVENANCE = "legacy_provenance"
    PRE_MANAGER_SEED = "pre_manager_seed"
    DEVELOPMENT_CHECKOUT = "development_checkout"
    NONE = "none"


class RepositoryRelation(StrEnum):
    CANONICAL = "canonical"
    REVIEWED_HISTORICAL = "reviewed_historical"
    OTHER = "other"
    UNKNOWN = "unknown"


class ChannelRelation(StrEnum):
    CURRENT = "current"
    FAST_FORWARD = "fast_forward"
    DIVERGED = "diverged"
    LEGACY_BRIDGE_REQUIRED = "legacy_bridge_required"
    UNKNOWN = "unknown"


class WorkingTreeCondition(StrEnum):
    CLEAN = "clean"
    TRACKED_CHANGES = "tracked_changes"
    UNTRACKED_ONLY = "untracked_only"
    MIXED_CHANGES = "mixed_changes"
    UNKNOWN = "unknown"


class ObservationAvailability(StrEnum):
    OBSERVED = "observed"
    MISSING = "missing"
    UNKNOWN = "unknown"


class ObservedBoolean(StrEnum):
    TRUE = "true"
    FALSE = "false"
    UNKNOWN = "unknown"


class InspectionProbe(StrEnum):
    REPOSITORY_ROOT = "repository_root"
    HEAD_COMMIT = "head_commit"
    HEAD_TREE = "head_tree"
    COMMITTED_PROVENANCE = "committed_provenance"
    HEAD_MESSAGE = "head_message"
    ORIGIN_URLS = "origin_urls"
    BRANCH = "branch"
    UPSTREAM = "upstream"
    STATUS = "status"
    CACHED_DIFF = "cached_diff"
    WORKTREE_DIFF = "worktree_diff"
    RAW_DIFF = "raw_diff"
    INDEX = "index"
    WORKTREES = "worktrees"
    SHALLOW = "shallow"
    ANCESTRY = "ancestry"


class PreservationEffect(StrEnum):
    TARGET_BYTE_WRITE = "target_byte_write"
    MANAGER_STATE_WRITE = "manager_state_write"
    SECRET_VALUE_READ = "secret_value_read"
    SECRET_VALUE_WRITE = "secret_value_write"
    DATABASE_READ = "database_read"
    DATABASE_WRITE = "database_write"
    TARGET_PROCESS_EXECUTION = "target_process_execution"
    PERMISSION_PROMPT = "permission_prompt"


class InspectionBoundaryViolation(RuntimeError):  # noqa: N818
    """Raised before an inspection can claim a forbidden capability was safe."""


@dataclass(frozen=True, slots=True)
class ExistingInstallInspectionRequest:
    target: Path
    channel: str
    manager_paths: ManagerPaths


@dataclass(frozen=True, slots=True)
class ObservedPaths:
    availability: ObservationAvailability
    values: tuple[str, ...]

    def __post_init__(self) -> None:
        if self.availability is not ObservationAvailability.OBSERVED and self.values:
            raise ValueError("inconsistent_existing_install_facts")


@dataclass(frozen=True, slots=True)
class ObservedPathPairs:
    availability: ObservationAvailability
    values: tuple[tuple[str, str], ...]

    def __post_init__(self) -> None:
        if self.availability is not ObservationAvailability.OBSERVED and self.values:
            raise ValueError("inconsistent_existing_install_facts")


@dataclass(frozen=True, slots=True)
class RawRow:
    """One ``git diff --raw -z --no-renames HEAD`` record: the exact shape of a tracked change.

    ``new_id`` is the worktree blob id git reports; it is never used as the
    Manager's digest (the commitment hashes the bytes the Manager reads itself).
    """

    old_mode: str
    new_mode: str
    old_id: str
    new_id: str
    status: str
    path: str

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "old_mode": self.old_mode,
            "new_mode": self.new_mode,
            "old_id": self.old_id,
            "new_id": self.new_id,
            "status": self.status,
            "path": self.path,
        }


@dataclass(frozen=True, slots=True)
class ObservedRawRows:
    """The raw-diff rows of the tracked tree (Step 7 section 6.3), or why they are unavailable."""

    availability: ObservationAvailability
    values: tuple[RawRow, ...]

    def __post_init__(self) -> None:
        if self.availability is not ObservationAvailability.OBSERVED and self.values:
            raise ValueError("inconsistent_existing_install_facts")


@dataclass(frozen=True, slots=True)
class TargetFilesystemIdentity:
    requested: Path
    canonical_display: Path
    parent_device: int
    parent_inode: int
    target_device: int
    target_inode: int


@dataclass(frozen=True, slots=True)
class ExistingInstallContractIdentity:
    flow_id: str
    flow_schema_version: int
    bundle_digest: str


@dataclass(frozen=True, slots=True)
class ChannelInspectionIdentity:
    channel_id: str
    repository: str
    release_tag: str
    commit: str
    tree_hash: str
    profile: str
    provenance_sha256: str
    seed_id: str
    origin_id: str
    manifest_sha256: str
    existing_install_contract: ExistingInstallContractIdentity
    catalog_resource: str
    catalog_sha256: str
    seed_lock_resource: str
    seed_lock_sha256: str
    descriptor_digest: str
    anchor_table_resource: str
    anchor_table_sha256: str


@dataclass(frozen=True, slots=True)
class InspectionTransitionPath:
    path: PurePosixPath
    baseline_kind: str
    candidate_kind: str
    change_kind: str


@dataclass(frozen=True, slots=True)
class InspectionAnchor:
    anchor_id: str
    anchor_kind: InspectionAnchorKind
    channel_id: str
    repository: str
    commit: str
    tree_hash: str
    provenance_sha256: str | None
    seed_id: str
    origin_id: str
    manifest_sha256: str
    channel_relation: ChannelRelation
    transition_paths: tuple[InspectionTransitionPath, ...]


@dataclass(frozen=True, slots=True)
class InstalledInspectionMetadata:
    channel_identity: ChannelInspectionIdentity
    seed_lock: SeedLock
    anchors: tuple[InspectionAnchor, ...]


@dataclass(frozen=True, slots=True)
class InspectionCheck:
    check_id: str
    section: str
    required: bool
    status: InspectionStatus
    reason_code: str | None
    summary: str
    observed: JsonValue
    expected: JsonValue
    source: str

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "check_id": self.check_id,
            "section": self.section,
            "required": self.required,
            "status": self.status.value,
            "reason_code": self.reason_code,
            "summary": self.summary,
            "observed": self.observed,
            "expected": self.expected,
            "source": self.source,
        }


@dataclass(frozen=True, slots=True)
class InspectionPreservationFacts:
    target_byte_writes: int
    manager_state_writes: int
    secret_value_reads: int
    secret_value_writes: int
    database_reads: int
    database_writes: int
    target_process_executions: int
    permission_prompts: int
    invoked_vectors: tuple[tuple[str, ...], ...]
    opened_resources: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ExistingInstallFacts:
    provenance_condition: ProvenanceCondition
    anchor_kind: InspectionAnchorKind
    anchor_id: str | None
    identity_status: InspectionStatus
    repository_relation: RepositoryRelation
    channel_relation: ChannelRelation
    head_commit: str | None
    head_tree: str | None
    working_tree: WorkingTreeCondition
    tracked_paths: ObservedPaths
    untracked_paths: ObservedPaths
    ignored_paths: ObservedPaths
    transition_paths: ObservedPaths
    tracked_transition_overlap: ObservedPaths
    untracked_destination_collisions: ObservedPaths
    casefold_collisions: ObservedPathPairs
    submodules: ObservedPaths
    linked_worktrees: ObservedPaths
    repository_operations: ObservedPaths
    shallow: ObservedBoolean
    detached: ObservedBoolean
    branch: str | None = None
    upstream: str | None = None
    origins: tuple[str, ...] = ()
    hazards: tuple[str, ...] = ()
    #: Step 7 section 6.3: the ``CACHED_DIFF`` path list (staged tracked paths) and the
    #: ``RAW_DIFF`` rows (per-path mode/status), both additive and both read from probes
    #: the inspection already ran.
    staged_paths: ObservedPaths = field(
        default_factory=lambda: ObservedPaths(ObservationAvailability.UNKNOWN, ())
    )
    tracked_entries: ObservedRawRows = field(
        default_factory=lambda: ObservedRawRows(ObservationAvailability.UNKNOWN, ())
    )

    def __post_init__(self) -> None:
        if any(_existing_install_fact_inconsistencies(self)):
            raise ValueError("inconsistent_existing_install_facts")


def _existing_install_fact_inconsistencies(
    facts: ExistingInstallFacts,
) -> tuple[bool, ...]:
    verified = facts.identity_status is InspectionStatus.VERIFIED
    return (
        verified and facts.anchor_kind is InspectionAnchorKind.NONE,
        verified and facts.channel_relation is ChannelRelation.UNKNOWN,
        _verified_anchor_has_invalid_provenance(facts),
        _current_anchor_is_inconsistent(facts),
        _legacy_anchor_is_inconsistent(facts),
    )


def _verified_anchor_has_invalid_provenance(facts: ExistingInstallFacts) -> bool:
    required_condition = (
        ProvenanceCondition.MISSING
        if facts.anchor_kind is InspectionAnchorKind.LEGACY_PROVENANCE
        else ProvenanceCondition.STRICT
    )
    return (
        facts.identity_status is InspectionStatus.VERIFIED
        and facts.provenance_condition is not required_condition
    )


def _current_anchor_is_inconsistent(facts: ExistingInstallFacts) -> bool:
    return facts.anchor_kind is InspectionAnchorKind.CURRENT_CHANNEL and (
        facts.provenance_condition is not ProvenanceCondition.STRICT
        or facts.channel_relation is not ChannelRelation.CURRENT
    )


def _legacy_anchor_is_inconsistent(facts: ExistingInstallFacts) -> bool:
    return facts.anchor_kind is InspectionAnchorKind.LEGACY_PROVENANCE and (
        facts.provenance_condition is not ProvenanceCondition.MISSING
        or facts.channel_relation is not ChannelRelation.LEGACY_BRIDGE_REQUIRED
        or facts.anchor_id is None
    )


@dataclass(frozen=True, slots=True)
class ExistingInstallClassification:
    installation_class: ExistingInstallClass
    reason_codes: tuple[str, ...]
    import_disposition: Literal["allow", "diagnostic_only", "refuse"]
    update_disposition: Literal[
        "allowed_after_import", "legacy_bridge_required", "blocked", "refuse"
    ]
    attention_required: bool


@dataclass(frozen=True, slots=True)
class ExistingInstallInspectionResult:
    request: ExistingInstallInspectionRequest
    target_identity: TargetFilesystemIdentity
    channel_identity: ChannelInspectionIdentity
    facts: ExistingInstallFacts
    classification: ExistingInstallClassification
    checks: tuple[InspectionCheck, ...]
    preservation: InspectionPreservationFacts
    #: Step 7 (CH-3 measured): the anchor that proved the identity when the clone is not at the channel release,
    #: so an enrollment records the identity it proved rather than the installed channel's.
    matched_anchor: InspectionAnchor | None = None
    #: The listed anchor commits already in HEAD's history, in anchor-table (release) order: the realign
    #: target a refused hand-merged clone's repair names is the last of them.
    listed_ancestors: tuple[str, ...] = ()

    def to_command_result(self) -> CommandResult:
        exit_code, status, error_kind = _reduce_exit(self.checks, self.classification)
        required_counts = {
            item.value: sum(check.required and check.status is item for check in self.checks)
            for item in InspectionStatus
        }
        required_counts["total"] = sum(required_counts.values())
        data = cast(
            dict[str, JsonValue],
            {
                "inspection_schema_version": 1,
                "target": {
                    "requested": str(self.target_identity.requested),
                    "canonical_display": str(self.target_identity.canonical_display),
                    "filesystem_identity": {
                        "device": self.target_identity.target_device,
                        "inode": self.target_identity.target_inode,
                    },
                    "parent_filesystem_identity": {
                        "device": self.target_identity.parent_device,
                        "inode": self.target_identity.parent_inode,
                    },
                },
                "channel": _channel_dict(self.channel_identity),
                "classification": {
                    "class": self.classification.installation_class.value,
                    "reason_codes": list(self.classification.reason_codes),
                    "import_disposition": self.classification.import_disposition,
                    "update_disposition": self.classification.update_disposition,
                    "attention_required": self.classification.attention_required,
                },
                "identity": {
                    "status": self.facts.identity_status.value,
                    "conjuncts": [
                        check.check_id
                        for check in self.checks
                        if check.required and check.status is InspectionStatus.VERIFIED
                    ],
                },
                "source": _source_dict(self.facts),
                "checks": [check.to_dict() for check in self.checks],
                "counts": required_counts,
                "preservation": _preservation_dict(self.preservation),
            },
        )
        return CommandResult(
            "existing_install_inspection",
            status,
            "Existing Solet inspection completed.",
            exit_code,
            error_kind,
            import_refusal_repair(self),
            data=data,
        )


@dataclass(frozen=True, slots=True)
class InspectionProbeOutput:
    returncode: int
    stdout: bytes
    stderr: bytes


class PinnedInspectionDirectory(Protocol):
    @property
    def descriptor(self) -> int: ...
    @property
    def device(self) -> int: ...
    @property
    def inode(self) -> int: ...
    def open_readonly(self, relative_path: PurePosixPath) -> BinaryIO: ...


class ReadOnlyInspectionRunner(Protocol):
    def __call__(
        self,
        probe: InspectionProbe,
        target: PinnedInspectionDirectory,
        *,
        other_commit: str | None = None,
    ) -> InspectionProbeOutput: ...


class InspectionEffectTracker(Protocol):
    def record_resource_read(self, resource_id: str) -> None: ...
    def record_probe(self, probe: InspectionProbe, argv: tuple[str, ...]) -> None: ...
    def record_forbidden(self, effect: PreservationEffect) -> Never: ...
    def snapshot(self) -> InspectionPreservationFacts: ...


class InstalledInspectionMetadataLoader(Protocol):
    def __call__(
        self, channel: str, tracker: InspectionEffectTracker
    ) -> InstalledInspectionMetadata: ...


@dataclass
class _ProductionInspectionEffectTracker:
    resources: list[str] = field(default_factory=lambda: [])
    vectors: list[tuple[str, ...]] = field(default_factory=lambda: [])
    forbidden: dict[PreservationEffect, int] = field(
        default_factory=lambda: dict.fromkeys(PreservationEffect, 0)
    )

    def record_resource_read(self, resource_id: str) -> None:
        self.resources.append(resource_id)

    def record_probe(self, probe: InspectionProbe, argv: tuple[str, ...]) -> None:
        self.vectors.append(argv)

    def record_forbidden(self, effect: PreservationEffect) -> Never:
        self.forbidden[effect] += 1
        raise InspectionBoundaryViolation(effect.value)

    def snapshot(self) -> InspectionPreservationFacts:
        return InspectionPreservationFacts(
            self.forbidden[PreservationEffect.TARGET_BYTE_WRITE],
            self.forbidden[PreservationEffect.MANAGER_STATE_WRITE],
            self.forbidden[PreservationEffect.SECRET_VALUE_READ],
            self.forbidden[PreservationEffect.SECRET_VALUE_WRITE],
            self.forbidden[PreservationEffect.DATABASE_READ],
            self.forbidden[PreservationEffect.DATABASE_WRITE],
            self.forbidden[PreservationEffect.TARGET_PROCESS_EXECUTION],
            self.forbidden[PreservationEffect.PERMISSION_PROMPT],
            tuple(self.vectors),
            tuple(self.resources),
        )


@dataclass
class _PinnedDirectory:
    _descriptor: int
    _parent_descriptor: int
    name: str
    _path: Path

    @property
    def descriptor(self) -> int:
        return self._descriptor

    @property
    def device(self) -> int:
        return os.fstat(self._descriptor).st_dev

    @property
    def inode(self) -> int:
        return os.fstat(self._descriptor).st_ino

    def open_readonly(self, relative_path: PurePosixPath) -> BinaryIO:
        if relative_path.is_absolute() or ".." in relative_path.parts:
            raise ValueError("inspection relative path is unsafe")
        fd = os.open(relative_path.as_posix(), os.O_RDONLY | os.O_NOFOLLOW, dir_fd=self._descriptor)
        return os.fdopen(fd, "rb")

    def close(self) -> None:
        os.close(self._descriptor)
        os.close(self._parent_descriptor)

    def namespace_matches(self) -> bool:
        try:
            observed = os.stat(self.name, dir_fd=self._parent_descriptor, follow_symlinks=False)
        except OSError:
            return False
        pinned = os.fstat(self._descriptor)
        return (
            observed.st_dev == pinned.st_dev
            and observed.st_ino == pinned.st_ino
            and stat.S_ISDIR(observed.st_mode)
        )


def _pin_target_directory(
    requested: Path, tracker: InspectionEffectTracker
) -> tuple[_PinnedDirectory, TargetFilesystemIdentity]:
    tracker.record_resource_read("target_directory")
    path = requested.expanduser()
    if not path.is_absolute():
        path = Path.cwd() / path
    parent, name = path.parent, path.name
    try:
        parent_fd = os.open(parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        target_fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent_fd)
    except OSError as exc:
        raise ValueError("target_identity_invalid") from exc
    pinned = _PinnedDirectory(target_fd, parent_fd, name, path)
    parent_stat, target_stat = os.fstat(parent_fd), os.fstat(target_fd)
    identity = TargetFilesystemIdentity(
        requested,
        path,
        parent_stat.st_dev,
        parent_stat.st_ino,
        target_stat.st_dev,
        target_stat.st_ino,
    )
    return pinned, identity


_GIT_ARGS: dict[InspectionProbe, tuple[str, ...]] = {
    InspectionProbe.REPOSITORY_ROOT: ("rev-parse", "--show-toplevel"),
    InspectionProbe.HEAD_COMMIT: ("rev-parse", "HEAD"),
    InspectionProbe.HEAD_TREE: ("rev-parse", "HEAD^{tree}"),
    InspectionProbe.COMMITTED_PROVENANCE: ("show", "HEAD:PROVENANCE.json"),
    InspectionProbe.HEAD_MESSAGE: ("show", "-s", "--format=%B", "HEAD"),
    InspectionProbe.ORIGIN_URLS: ("remote", "get-url", "--all", "origin"),
    InspectionProbe.BRANCH: ("symbolic-ref", "--short", "HEAD"),
    InspectionProbe.UPSTREAM: ("rev-parse", "--abbrev-ref", "@{upstream}"),
    InspectionProbe.STATUS: ("status", "--porcelain=v1", "--untracked-files=all", "--ignored"),
    InspectionProbe.CACHED_DIFF: ("diff", "--cached", "--name-only", "-z"),
    InspectionProbe.WORKTREE_DIFF: ("diff", "--name-only", "-z"),
    InspectionProbe.RAW_DIFF: ("diff", "--raw", "-z", "--no-renames", "HEAD"),
    InspectionProbe.INDEX: ("ls-files", "-s", "-z"),
    InspectionProbe.WORKTREES: ("worktree", "list", "--porcelain"),
    InspectionProbe.SHALLOW: ("rev-parse", "--is-shallow-repository"),
}


def subprocess_read_only_inspection_runner(
    probe: InspectionProbe, target: PinnedInspectionDirectory, *, other_commit: str | None = None
) -> InspectionProbeOutput:
    args = _GIT_ARGS.get(probe)
    if probe is InspectionProbe.ANCESTRY:
        if other_commit is None:
            raise ValueError("ancestry requires a reviewed commit")
        args = ("merge-base", "--is-ancestor", other_commit, "HEAD")
    if args is None:
        raise ValueError("unknown inspection probe")
    before = os.fstat(target.descriptor)
    # iss_836499b3 B1 / iss_6a8d03a3: the target's own fsmonitor, hooks, filters and config never run, and Git is
    # pinned to the pinned directory (R2-1).  An executable or redirecting repository config is raised as
    # ``git_execution_surface_unsafe`` with its own repair (review N6), never folded into an unproven identity.
    result = run_target_git(
        args,
        pass_fds=(target.descriptor,),
        preexec_fn=lambda: os.fchdir(target.descriptor),
    )
    after = os.fstat(target.descriptor)
    if (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino):
        raise ValueError("target_identity_invalid")
    return InspectionProbeOutput(result.returncode, result.stdout, result.stderr)


def inspect_existing_install(
    request: ExistingInstallInspectionRequest,
    *,
    metadata_loader: InstalledInspectionMetadataLoader,
    runner: ReadOnlyInspectionRunner = subprocess_read_only_inspection_runner,
    effect_tracker: InspectionEffectTracker | None = None,
) -> ExistingInstallInspectionResult:
    tracker = effect_tracker if effect_tracker is not None else _ProductionInspectionEffectTracker()
    metadata = metadata_loader(request.channel, tracker)
    return _inspect_existing_install_with_metadata(request, metadata, runner, tracker)


def _inspect_existing_install_with_metadata(
    request: ExistingInstallInspectionRequest,
    metadata: InstalledInspectionMetadata,
    runner: ReadOnlyInspectionRunner,
    tracker: InspectionEffectTracker,
) -> ExistingInstallInspectionResult:
    target, identity = _pin_target_directory(request.target, tracker)
    try:
        checks, values = _target_checks(target, metadata, runner, tracker)
        checks += (_inventory_check(request.manager_paths, tracker),)
        facts = _facts_from_values(values, metadata)
        classification = classify_existing_install(facts)
        if not target.namespace_matches():
            checks += (
                _check(
                    "target_namespace",
                    "target",
                    True,
                    InspectionStatus.FAILED,
                    "target_identity_invalid",
                    "Target name no longer resolves to pinned directory.",
                    None,
                    None,
                    "pinned_descriptor",
                ),
            )
        anchor = values.get("anchor")
        return ExistingInstallInspectionResult(
            request,
            identity,
            metadata.channel_identity,
            facts,
            classification,
            checks,
            tracker.snapshot(),
            anchor if isinstance(anchor, InspectionAnchor) and facts.anchor_kind is not InspectionAnchorKind.CURRENT_CHANNEL else None,
            cast(tuple[str, ...], values.get("listed_ancestors", ())),
        )
    finally:
        target.close()


def _probe(
    runner: ReadOnlyInspectionRunner,
    probe: InspectionProbe,
    target: PinnedInspectionDirectory,
    tracker: InspectionEffectTracker,
    other_commit: str | None = None,
) -> InspectionProbeOutput:
    args = (
        _GIT_ARGS.get(probe)
        if probe is not InspectionProbe.ANCESTRY
        else ("merge-base", "--is-ancestor", other_commit or "", "HEAD")
    )
    tracker.record_probe(probe, ("git", *cast(tuple[str, ...], args)))
    return runner(probe, target, other_commit=other_commit)


def _target_checks(
    target: PinnedInspectionDirectory,
    metadata: InstalledInspectionMetadata,
    runner: ReadOnlyInspectionRunner,
    tracker: InspectionEffectTracker,
) -> tuple[tuple[InspectionCheck, ...], dict[str, object]]:
    from ._existing_install_inspection_target import target_checks

    return target_checks(target, metadata, runner, tracker, _probe)


def _facts_from_values(
    values: dict[str, object], metadata: InstalledInspectionMetadata
) -> ExistingInstallFacts:
    from ._existing_install_inspection_target import facts_from_values

    return facts_from_values(values, metadata)


def import_refusal_repair(result: ExistingInstallInspectionResult) -> str | None:
    from ._existing_install_inspection_repair import import_refusal_repair as _repair

    return _repair(result)


def classify_existing_install(facts: ExistingInstallFacts) -> ExistingInstallClassification:
    from ._existing_install_inspection_classification import (
        classify_existing_install as _classify,
    )

    return _classify(facts)


def _inventory_check(paths: ManagerPaths, tracker: InspectionEffectTracker) -> InspectionCheck:
    tracker.record_resource_read("manager_inventory")
    path = paths.registry_path
    try:
        raw = _read_regular_bytes(path)
    except FileNotFoundError:
        return _check(
            "manager_inventory",
            "inventory",
            False,
            InspectionStatus.MISSING,
            "inventory_absent",
            "No Manager inventory exists.",
            None,
            None,
            "manager_inventory",
        )
    except OSError:
        return _check(
            "manager_inventory",
            "inventory",
            False,
            InspectionStatus.UNKNOWN,
            "inventory_unavailable",
            "Manager inventory cannot be read.",
            None,
            None,
            "manager_inventory",
        )
    try:
        records = parse_maintenance_inventory_bytes(raw)
    except Exception:
        return _check(
            "manager_inventory",
            "inventory",
            False,
            InspectionStatus.FAILED,
            "inventory_malformed",
            "Manager inventory is malformed.",
            None,
            None,
            "manager_inventory",
        )
    return _check(
        "manager_inventory",
        "inventory",
        False,
        InspectionStatus.VERIFIED,
        None,
        "Manager inventory read as informational context.",
        {"records": len(records)},
        None,
        "manager_inventory",
    )


def load_installed_inspection_metadata(
    channel: str, tracker: InspectionEffectTracker
) -> InstalledInspectionMetadata:
    from ._existing_install_inspection_metadata import (
        load_installed_inspection_metadata as _load_metadata,
    )

    return _load_metadata(channel, tracker)


def _read_regular_bytes(path: Path) -> bytes:
    """Read an existing regular file once without following a final symlink."""
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise ValueError("inspection resource is not a regular file")
        chunks: list[bytes] = []
        while True:
            chunk = os.read(descriptor, 1_048_576)
            if not chunk:
                return b"".join(chunks)
            chunks.append(chunk)
    finally:
        os.close(descriptor)


def _check(
    check_id: str,
    section: str,
    required: bool,
    status: InspectionStatus,
    reason: str | None,
    summary: str,
    observed: JsonValue,
    expected: JsonValue,
    source: str,
) -> InspectionCheck:
    return InspectionCheck(
        check_id, section, required, status, reason, summary, observed, expected, source
    )


def _decode_line(value: bytes) -> str | None:  # pyright: ignore[reportUnusedFunction]
    try:
        return value.decode("utf-8").strip() or None
    except UnicodeDecodeError:
        return None


def _status_paths(  # pyright: ignore[reportUnusedFunction]
    value: bytes,
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    tracked: list[str] = []
    untracked: list[str] = []
    for line in value.decode("utf-8", "replace").splitlines():
        if len(line) < 4:
            continue
        if line.startswith("?? "):
            untracked.append(line[3:])
        elif not line.startswith("!! "):
            tracked.append(line[3:])
    return tuple(sorted(tracked)), tuple(sorted(untracked))


def _reduce_exit(
    checks: tuple[InspectionCheck, ...], classification: ExistingInstallClassification
) -> tuple[ExitCode, str, str | None]:
    if any(check.reason_code == "target_identity_invalid" for check in checks):
        return ExitCode.INVALID, "invalid", "target_identity_invalid"
    if any(check.required and check.status is InspectionStatus.FAILED for check in checks):
        return ExitCode.FAILED, "failed", "inspection_failed"
    if (
        any(
            check.required and check.status in {InspectionStatus.MISSING, InspectionStatus.UNKNOWN}
            for check in checks
        )
        or classification.attention_required
    ):
        return ExitCode.HUMAN_ACTION, "attention_required", "inspection_incomplete"
    return ExitCode.OK, "verified", None


def _channel_dict(identity: ChannelInspectionIdentity) -> dict[str, JsonValue]:
    return {
        "channel_id": identity.channel_id,
        "repository": identity.repository,
        "release_tag": identity.release_tag,
        "commit": identity.commit,
        "tree_hash": identity.tree_hash,
        "profile": identity.profile,
        "provenance_sha256": identity.provenance_sha256,
        "seed_id": identity.seed_id,
        "origin_id": identity.origin_id,
        "manifest_sha256": identity.manifest_sha256,
        "existing_install_contract": {
            "flow_id": identity.existing_install_contract.flow_id,
            "flow_schema_version": identity.existing_install_contract.flow_schema_version,
            "bundle_digest": identity.existing_install_contract.bundle_digest,
        },
        "installed_resources": {
            "catalog_sha256": identity.catalog_sha256,
            "seed_lock_sha256": identity.seed_lock_sha256,
            "descriptor_digest": identity.descriptor_digest,
            "anchor_table_sha256": identity.anchor_table_sha256,
        },
    }


def _source_dict(facts: ExistingInstallFacts) -> dict[str, JsonValue]:
    return {
        "head": facts.head_commit,
        "tree": facts.head_tree,
        "branch": facts.branch,
        "upstream": facts.upstream,
        "origins": list(facts.origins),
        "repository_relation": facts.repository_relation.value,
        "channel_relation": facts.channel_relation.value,
        "working_tree": facts.working_tree.value,
        "tracked_changes": {
            "availability": facts.tracked_paths.availability.value,
            "values": list(facts.tracked_paths.values),
        },
        "untracked_paths": {
            "availability": facts.untracked_paths.availability.value,
            "values": list(facts.untracked_paths.values),
        },
        "paths": {
            "staged": {
                "availability": facts.staged_paths.availability.value,
                "values": list(facts.staged_paths.values),
            },
            "tracked_entries": {
                "availability": facts.tracked_entries.availability.value,
                "values": [row.to_dict() for row in facts.tracked_entries.values],
            },
        },
        "hazards": list(facts.hazards),
    }


def _preservation_dict(value: InspectionPreservationFacts) -> dict[str, JsonValue]:
    return {
        "target_byte_writes": value.target_byte_writes,
        "manager_state_writes": value.manager_state_writes,
        "secret_value_reads": value.secret_value_reads,
        "secret_value_writes": value.secret_value_writes,
        "database_reads": value.database_reads,
        "database_writes": value.database_writes,
        "target_process_executions": value.target_process_executions,
        "permission_prompts": value.permission_prompts,
        "invoked_vectors": [list(row) for row in value.invoked_vectors],
        "opened_resources": list(value.opened_resources),
    }
