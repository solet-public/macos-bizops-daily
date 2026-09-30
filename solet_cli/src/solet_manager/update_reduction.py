"""The update preview's reduction (Step 4 section 6.3, Step 7 section 6.3): every reason the preview reports, from one candidate against one target.

Topology reasons come from ``analyze_update_topology``; baseline identity,
detached HEAD and origin reasons from the inspection; ``history_diverged`` from
a merge-base proof inside the candidate cache; the landed collision rows and the
Step 7 local-state reasons (overlap, executed code, shape, staged, metadata)
from the exact transition set against the target's porcelain status.  Git
runs through the two callables the caller supplies, so the executor's closed
vectors -- and the tests that patch them -- stay the only Git surface.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from subprocess import CompletedProcess

from .errors import SourceError, UpdateBlockedError
from .executed_code import RosterUnreadableError, executed_code_roots
from .existing_install_inspection import ExistingInstallInspectionResult, InspectionStatus
from .installer_pins import installer_pinned_paths
from .local_state import ObservedLocalState, observe_local_state
from .models import InstanceInventoryRecordV2
from .paths import ManagerPaths, update_candidate_cache
from .update_candidate import UpdateCandidate
from .update_hydration_carry import UNCARRIABLE_REASON, CarryPlan, plan_hydration_carry
from .update_topology import (
    CollisionRow,
    analyze_update_topology,
    collision_rows,
    config_flag,
    git_metadata_paths,
    local_state_reasons,
    origin_reason,
    parse_local_entries,
    parse_transition_paths,
    shape_changed_rows,
)

GitRead = Callable[[Path, tuple[str, ...], str], bytes]
GitRun = Callable[[Path, tuple[str, ...]], CompletedProcess[bytes]]
_STATUS_ARGS = ("status", "--porcelain=v1", "-z", "--untracked-files=normal", "--ignored=traditional")


@dataclass(frozen=True, slots=True)
class Reduction:
    """The preview's reduction of one candidate against one target: reasons, landed collisions, the observed local state, blocked paths."""

    reasons: tuple[str, ...]
    collisions: tuple[CollisionRow, ...]
    local_state: ObservedLocalState | None
    blocked_paths: dict[str, list[str]]
    #: iss_f1d8cfc2: the tracked modifications proved to be exactly the installer's interpreter pin (Class T).
    installer_pins: tuple[str, ...] = ()
    #: iss_f89ab692: the overlapped files whose only local change is the create's hydration block.
    hydration: CarryPlan = CarryPlan()


def reduce_update(
    paths: ManagerPaths,
    record: InstanceInventoryRecordV2,
    candidate: UpdateCandidate,
    baseline: ExistingInstallInspectionResult,
    entries: tuple[tuple[str, str, str], ...],
    source_mode: str = "advance",
    *,
    git: GitRead,
    run_git: GitRun,
    cache_git: GitRead,
    cache_run_git: GitRun,
) -> Reduction:
    """Section 6.3: topology reasons, identity/origin reasons, history proof against the candidate cache, then the local-state reduction."""
    facts = baseline.facts
    release = record.source_release
    reasons = set(analyze_update_topology(facts, baseline_commit=release.commit, candidate_commit=candidate.fields.commit, source_mode=source_mode).reasons)
    if facts.identity_status is not InspectionStatus.VERIFIED or facts.head_tree != release.tree:
        reasons.add("baseline_identity_unproven")
    if facts.branch is None:
        reasons.add("detached_head")
    origin = origin_reason(facts.origins, record.channel.canonical_repository)
    if origin is not None:
        reasons.add(origin)
    collisions: tuple[CollisionRow, ...] = ()
    local_state: ObservedLocalState | None = None
    blocked: dict[str, list[str]] = {}
    pins: tuple[str, ...] = ()
    hydration = CarryPlan()
    if "already_current" not in reasons or source_mode == "verify":
        cache = update_candidate_cache(paths, candidate.descriptor_digest).repository
        # A baseline the channel repository has never seen is divergent history, not an error (Step 6, n5).
        # The target and the bare candidate cache are pinned differently (iss_836499b3 R2-1), hence two readers.
        if _object_exists(cache_run_git, cache, release.commit) and is_ancestor(cache_run_git, cache, release.commit, candidate.fields.commit):
            collisions, local_state, blocked, pins, hydration = _local_state_reduction(paths, git, cache_git, cache, record, candidate, baseline, entries)
        else:
            reasons.add("history_diverged")
    reasons.update(row.reason for row in collisions)
    reasons.update(blocked)
    return Reduction(tuple(sorted(reasons)), collisions, local_state, blocked, pins, hydration)


def _local_state_reduction(
    paths: ManagerPaths,
    git: GitRead,
    cache_git: GitRead,
    cache: Path,
    record: InstanceInventoryRecordV2,
    candidate: UpdateCandidate,
    baseline: ExistingInstallInspectionResult,
    entries: tuple[tuple[str, str, str], ...],
) -> tuple[tuple[CollisionRow, ...], ObservedLocalState, dict[str, list[str]], tuple[str, ...], CarryPlan]:
    """Section 6.3: the exact transition set, the landed collision proof, the Step 7 reasons, the commitment, the installer pins, and the hydration carry."""
    target = Path(record.target.canonical_path)
    transition = parse_transition_paths(
        cache_git(
            cache,
            ("diff-tree", "-r", "-z", "--no-renames", "--name-status", record.source_release.commit, candidate.fields.commit),
            "candidate transition set is unreadable",
        )
    )
    local = parse_local_entries(git(target, _STATUS_ARGS, "target status is unreadable"))
    case_insensitive = config_flag(entries, "core.ignorecase")
    collisions = collision_rows(transition, local, case_insensitive=case_insensitive)
    try:
        roots = executed_code_roots(target, candidate)
    except RosterUnreadableError as exc:
        raise UpdateBlockedError("profile_manifest_unreadable", f"the executed-code roots cannot be derived: {exc}", repair="Restore profile/config/manifest.yaml, then preview again.") from exc
    pins = installer_pinned_paths(target, baseline.facts, lambda spec: git(target, ("cat-file", "blob", spec), "committed hook manifest is unreadable"))
    hydration = plan_hydration_carry(
        paths,
        name=record.name,
        target=target,
        facts=baseline.facts,
        transition=transition,
        read_target=lambda spec: git(target, ("cat-file", "blob", spec), "committed agent instructions are unreadable"),
        read_candidate=lambda spec: cache_git(cache, ("cat-file", "blob", spec), "candidate agent instructions are unreadable"),
        candidate_commit=candidate.fields.commit,
    )
    reasons = local_state_reasons(baseline.facts, transition, roots, case_insensitive=case_insensitive, installer_pins=pins, hydration_blocks=hydration.paths)
    blocked = {reason: list(reason_paths) for reason, reason_paths in reasons}
    if hydration.uncarriable:
        blocked[UNCARRIABLE_REASON] = list(hydration.uncarriable)
    for reason in ("staged_changes_present", "tracked_shape_changed", "git_metadata_present"):
        fact_paths = _fact_reason_paths(baseline, reason)
        if fact_paths:
            blocked[reason] = fact_paths
    return collisions, observe_local_state(target, baseline.facts), blocked, pins, hydration


def _fact_reason_paths(baseline: ExistingInstallInspectionResult, reason: str) -> list[str]:
    facts = baseline.facts
    if reason == "staged_changes_present":
        return list(facts.staged_paths.values)
    if reason == "tracked_shape_changed":
        return sorted(row.path for row in shape_changed_rows(facts.tracked_entries.values))
    return list(git_metadata_paths(facts.tracked_paths.values, facts.untracked_paths.values))


def _object_exists(run_git: GitRun, repository: Path, commit: str) -> bool:
    return run_git(repository, ("cat-file", "-e", f"{commit}^{{commit}}")).returncode == 0


def is_ancestor(run_git: GitRun, repository: Path, ancestor: str, descendant: str) -> bool:
    completed = run_git(repository, ("merge-base", "--is-ancestor", ancestor, descendant))
    if completed.returncode in {0, 1}:
        return completed.returncode == 0
    raise SourceError(f"ancestry could not be proved: {completed.stderr.decode('utf-8', 'replace').strip()}")
