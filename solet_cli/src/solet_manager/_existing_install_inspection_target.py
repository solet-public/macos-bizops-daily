"""Read-only Git probe projection for existing-install inspection."""

from __future__ import annotations

import os
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import cast

from solet_setup_contracts.provenance_v1 import (
    ProvenanceV1,
    ProvenanceV1Error,
    canonical_provenance_sha256,
    parse_provenance_v1,
    verify_seal_trailers,
)

from .existing_install_inspection import (
    ChannelRelation,
    ExistingInstallFacts,
    InspectionAnchor,
    InspectionAnchorKind,
    InspectionCheck,
    InspectionEffectTracker,
    InspectionProbe,
    InspectionProbeOutput,
    InspectionStatus,
    InstalledInspectionMetadata,
    JsonValue,
    ObservationAvailability,
    ObservedBoolean,
    ObservedPathPairs,
    ObservedPaths,
    ObservedRawRows,
    PinnedInspectionDirectory,
    ProvenanceCondition,
    RawRow,
    ReadOnlyInspectionRunner,
    RepositoryRelation,
    WorkingTreeCondition,
    _check,
    _decode_line,
    _status_paths,
)
from .update_topology import parse_raw_diff


def target_checks(
    target: PinnedInspectionDirectory,
    metadata: InstalledInspectionMetadata,
    runner: ReadOnlyInspectionRunner,
    tracker: InspectionEffectTracker,
    probe: Callable[..., InspectionProbeOutput],
) -> tuple[tuple[InspectionCheck, ...], dict[str, object]]:
    root = probe(runner, InspectionProbe.REPOSITORY_ROOT, target, tracker)
    if root.returncode != 0:
        return (_not_git_worktree_check(),), {}
    if not _root_is_pinned_target(root, target):
        return (_redirected_root_check(_successful_line(root)),), {}
    values = _target_probe_values(target, metadata, runner, tracker, probe)
    return _target_identity_checks(values, metadata), values


def _not_git_worktree_check() -> InspectionCheck:
    return _check(
        "repository_root",
        "git",
        True,
        InspectionStatus.FAILED,
        "not_git_worktree",
        "Pinned target is not a Git worktree.",
        None,
        "git worktree",
        "git",
    )


def _root_is_pinned_target(root: InspectionProbeOutput, target: PinnedInspectionDirectory) -> bool:
    """Review R2-1: the work tree Git reports is the pinned directory itself (same device and inode)."""
    reported = _successful_line(root)
    if reported is None:
        return False
    try:
        observed = os.stat(reported)
    except OSError:
        return False
    return (observed.st_dev, observed.st_ino) == (target.device, target.inode)


def _redirected_root_check(reported: str | None) -> InspectionCheck:
    return _check(
        "repository_root",
        "git",
        True,
        InspectionStatus.FAILED,
        "repository_root_redirected",
        "Git reports a work tree other than the pinned target.",
        reported,
        "pinned target directory",
        "git",
    )


def _target_probe_values(
    target: PinnedInspectionDirectory,
    metadata: InstalledInspectionMetadata,
    runner: ReadOnlyInspectionRunner,
    tracker: InspectionEffectTracker,
    probe: Callable[..., InspectionProbeOutput],
) -> dict[str, object]:
    outputs = {
        inspection_probe: probe(runner, inspection_probe, target, tracker)
        for inspection_probe in (
            InspectionProbe.HEAD_COMMIT,
            InspectionProbe.HEAD_TREE,
            InspectionProbe.ORIGIN_URLS,
            InspectionProbe.BRANCH,
            InspectionProbe.UPSTREAM,
            InspectionProbe.STATUS,
            InspectionProbe.CACHED_DIFF,
            InspectionProbe.WORKTREE_DIFF,
            InspectionProbe.RAW_DIFF,
            InspectionProbe.INDEX,
            InspectionProbe.WORKTREES,
            InspectionProbe.SHALLOW,
            InspectionProbe.COMMITTED_PROVENANCE,
            InspectionProbe.HEAD_MESSAGE,
        )
    }
    (
        head,
        tree,
        origins,
        branch,
        upstream,
        status,
        cached_diff,
        worktree_diff,
        raw_diff,
        index,
        worktrees,
        shallow,
        provenance,
        message,
    ) = (
        outputs[inspection_probe]
        for inspection_probe in (
            InspectionProbe.HEAD_COMMIT,
            InspectionProbe.HEAD_TREE,
            InspectionProbe.ORIGIN_URLS,
            InspectionProbe.BRANCH,
            InspectionProbe.UPSTREAM,
            InspectionProbe.STATUS,
            InspectionProbe.CACHED_DIFF,
            InspectionProbe.WORKTREE_DIFF,
            InspectionProbe.RAW_DIFF,
            InspectionProbe.INDEX,
            InspectionProbe.WORKTREES,
            InspectionProbe.SHALLOW,
            InspectionProbe.COMMITTED_PROVENANCE,
            InspectionProbe.HEAD_MESSAGE,
        )
    )
    head_text, tree_text = _successful_line(head), _successful_line(tree)
    origin_values = _origin_values(origins)
    tracked, untracked = _successful_status(status)
    condition, digest = _provenance_values(provenance)
    stamp = _strict_stamp(provenance)
    working_condition, working_digest, working_bytes = _working_provenance_values(target, tracker)
    committed_bytes = provenance.stdout if provenance.returncode == 0 else None
    working_matches_committed = working_bytes == committed_bytes and committed_bytes is not None
    trailers_verified = _trailers_verified(committed_bytes, message)
    current_identity = all(
        (
            head_text == metadata.channel_identity.commit,
            tree_text == metadata.channel_identity.tree_hash,
            condition is ProvenanceCondition.STRICT,
            digest == metadata.channel_identity.provenance_sha256,
            working_condition is ProvenanceCondition.STRICT,
            working_digest == metadata.channel_identity.provenance_sha256,
            working_matches_committed,
            trailers_verified,
            _channel_stamp_matches(stamp, metadata),
        )
    )
    anchor, anchor_identity = _matching_anchor(
        metadata.anchors,
        head_text,
        tree_text,
        digest,
        origin_values,
        condition,
        working_condition,
        working_matches_committed,
        trailers_verified,
        stamp,
    )
    anchor_outputs = {
        item.anchor_id: probe(runner, InspectionProbe.ANCESTRY, target, tracker, item.commit)
        for item in metadata.anchors
    }
    relation = _anchor_relation(anchor, anchor_outputs)
    identity_ok = current_identity or anchor_identity
    effective_anchor = (
        InspectionAnchorKind.CURRENT_CHANNEL
        if current_identity
        else (
            anchor.anchor_kind
            if anchor_identity and anchor is not None
            else InspectionAnchorKind.NONE
        )
    )
    return {
        "head": head_text,
        "tree": tree_text,
        "provenance": condition,
        "provenance_digest": digest,
        "working_provenance": working_condition,
        "working_provenance_digest": working_digest,
        "working_matches_committed": working_matches_committed,
        "trailers_verified": trailers_verified,
        "identity": identity_ok,
        "anchor": anchor,
        "anchor_kind": effective_anchor,
        "anchor_relation": relation,
        "repository_relation": _repository_relation(origins, origin_values, metadata),
        "origins": origin_values,
        "tracked": tracked,
        "untracked": untracked,
        "status_observed": status.returncode == 0,
        "cached_observed": cached_diff.returncode == 0,
        "staged": _nul_paths(cached_diff),
        "worktree_diff_observed": worktree_diff.returncode == 0,
        "raw_diff_observed": raw_diff.returncode == 0,
        "tracked_entries": _raw_rows(raw_diff),
        "index_observed": index.returncode == 0,
        "branch": _successful_line(branch),
        "upstream": _successful_line(upstream),
        "detached": _detached(branch),
        "shallow": _shallow(shallow),
        "linked_worktrees": _linked_worktrees(worktrees),
        "worktrees_observed": worktrees.returncode == 0,
    }


def _successful_line(output: InspectionProbeOutput) -> str | None:
    return _decode_line(output.stdout) if output.returncode == 0 else None


def _origin_values(output: InspectionProbeOutput) -> tuple[str, ...]:
    if output.returncode != 0:
        return ()
    return tuple(
        sorted(line for line in output.stdout.decode("utf-8", "replace").splitlines() if line)
    )


def _successful_status(output: InspectionProbeOutput) -> tuple[tuple[str, ...], tuple[str, ...]]:
    return _status_paths(output.stdout) if output.returncode == 0 else ((), ())


def _nul_paths(output: InspectionProbeOutput) -> tuple[str, ...]:
    """The NUL-separated path list of a successful ``--name-only -z`` probe (Step 7: the staged set)."""
    if output.returncode != 0:
        return ()
    return tuple(sorted(token.decode("utf-8", "surrogateescape") for token in output.stdout.split(b"\0") if token))


def _raw_rows(output: InspectionProbeOutput) -> tuple[RawRow, ...]:
    """The parsed ``diff --raw -z`` rows of a successful probe; a malformed listing fails loud."""
    if output.returncode != 0:
        return ()
    return parse_raw_diff(output.stdout)


def _detached(output: InspectionProbeOutput) -> ObservedBoolean:
    if output.returncode == 0:
        return ObservedBoolean.FALSE
    # ``symbolic-ref`` documents exit status 1 for a detached HEAD; Git has
    # also returned 128 for this condition on real repositories.
    if output.returncode in {1, 128}:
        return ObservedBoolean.TRUE
    return ObservedBoolean.UNKNOWN


def _shallow(output: InspectionProbeOutput) -> ObservedBoolean:
    value = _successful_line(output)
    if value == "true":
        return ObservedBoolean.TRUE
    if value == "false":
        return ObservedBoolean.FALSE
    return ObservedBoolean.UNKNOWN


def _linked_worktrees(output: InspectionProbeOutput) -> tuple[str, ...]:
    if output.returncode != 0:
        return ()
    roots = tuple(
        line.removeprefix("worktree ")
        for line in output.stdout.decode("utf-8", "replace").splitlines()
        if line.startswith("worktree ")
    )
    return roots[1:]


def _matching_anchor(
    anchors: tuple[InspectionAnchor, ...],
    head: str | None,
    tree: str | None,
    provenance_digest: str | None,
    origins: tuple[str, ...],
    provenance: ProvenanceCondition,
    working_provenance: ProvenanceCondition,
    working_matches: bool,
    trailers_verified: bool,
    stamp: ProvenanceV1 | None,
) -> tuple[InspectionAnchor | None, bool]:
    matches = tuple(
        anchor
        for anchor in anchors
        if _anchor_matches(
            anchor,
            head,
            tree,
            provenance_digest,
            origins,
            provenance,
            working_provenance,
            working_matches,
            trailers_verified,
            stamp,
        )
    )
    return (matches[0], True) if len(matches) == 1 else (None, False)


def _anchor_matches(
    anchor: InspectionAnchor,
    head: str | None,
    tree: str | None,
    provenance_digest: str | None,
    origins: tuple[str, ...],
    provenance: ProvenanceCondition,
    working_provenance: ProvenanceCondition,
    working_matches: bool,
    trailers_verified: bool,
    stamp: ProvenanceV1 | None,
) -> bool:
    if not _anchor_identity_matches(anchor, head, tree, provenance_digest, origins, stamp):
        return False
    if anchor.anchor_kind is InspectionAnchorKind.LEGACY_PROVENANCE:
        return provenance is ProvenanceCondition.MISSING
    return _strict_provenance_matches(
        provenance, working_provenance, working_matches, trailers_verified
    )


def _anchor_identity_matches(
    anchor: InspectionAnchor,
    head: str | None,
    tree: str | None,
    provenance_digest: str | None,
    origins: tuple[str, ...],
    stamp: ProvenanceV1 | None,
) -> bool:
    return (
        anchor.commit == head
        and anchor.tree_hash == tree
        and anchor.repository in origins
        and anchor.provenance_sha256 == provenance_digest
        and _anchor_stamp_matches(anchor, stamp)
    )


def _anchor_stamp_matches(anchor: InspectionAnchor, stamp: ProvenanceV1 | None) -> bool:
    if anchor.anchor_kind is InspectionAnchorKind.LEGACY_PROVENANCE:
        return stamp is None
    return stamp is not None and (
        anchor.seed_id == stamp.seed_id
        and anchor.origin_id == stamp.origin_id
        and anchor.manifest_sha256 == stamp.manifest_sha256
    )


def _channel_stamp_matches(
    stamp: ProvenanceV1 | None, metadata: InstalledInspectionMetadata
) -> bool:
    identity = metadata.channel_identity
    return stamp is not None and (
        stamp.seed_id == identity.seed_id
        and stamp.origin_id == identity.origin_id
        and stamp.manifest_sha256 == identity.manifest_sha256
    )


def _strict_provenance_matches(
    provenance: ProvenanceCondition,
    working_provenance: ProvenanceCondition,
    working_matches: bool,
    trailers_verified: bool,
) -> bool:
    return (
        provenance is ProvenanceCondition.STRICT
        and working_provenance is ProvenanceCondition.STRICT
        and working_matches
        and trailers_verified
    )


def _anchor_relation(
    anchor: InspectionAnchor | None,
    ancestry: dict[str, InspectionProbeOutput],
) -> ChannelRelation:
    if anchor is None:
        return ChannelRelation.UNKNOWN
    output = ancestry.get(anchor.anchor_id)
    if output is None or output.returncode != 0:
        return ChannelRelation.UNKNOWN
    return anchor.channel_relation


def _provenance_values(output: InspectionProbeOutput) -> tuple[ProvenanceCondition, str | None]:
    if output.returncode != 0:
        return ProvenanceCondition.MISSING, None
    try:
        return ProvenanceCondition.STRICT, canonical_provenance_sha256(output.stdout)
    except ProvenanceV1Error:
        return ProvenanceCondition.MALFORMED, None


def _strict_stamp(output: InspectionProbeOutput) -> ProvenanceV1 | None:
    if output.returncode != 0:
        return None
    try:
        return parse_provenance_v1(output.stdout)
    except ProvenanceV1Error:
        return None


def _working_provenance_values(
    target: PinnedInspectionDirectory, tracker: InspectionEffectTracker
) -> tuple[ProvenanceCondition, str | None, bytes | None]:
    tracker.record_resource_read("target:PROVENANCE.json")
    try:
        with target.open_readonly(PurePosixPath("PROVENANCE.json")) as source:
            payload = source.read()
    except FileNotFoundError:
        return ProvenanceCondition.MISSING, None, None
    except OSError:
        return ProvenanceCondition.UNKNOWN, None, None
    condition, digest = _provenance_values(InspectionProbeOutput(0, payload, b""))
    return condition, digest, payload


def _trailers_verified(provenance: bytes | None, message: InspectionProbeOutput) -> bool:
    if provenance is None or message.returncode != 0:
        return False
    try:
        verify_seal_trailers(
            parse_provenance_v1(provenance),
            _trailer_values(message.stdout.decode("utf-8", "strict")),
        )
    except (UnicodeDecodeError, ProvenanceV1Error):
        return False
    return True


def _trailer_values(message: str) -> dict[str, list[str]]:
    lines = message.splitlines()
    values: dict[str, list[str]] = {"Subject": [lines[0]] if lines else []}
    keys = {
        "Seed-Id",
        "Origin-Id",
        "Manifest-SHA256",
        "Assembled-Ref",
        "License-Policy",
        "Minted-At",
        "Lineage-Parent",
    }
    for line in lines[1:]:
        if ": " not in line:
            continue
        key, value = line.split(": ", 1)
        if key in keys:
            values.setdefault(key, []).append(value)
    return values


def _repository_relation(
    output: InspectionProbeOutput,
    origins: tuple[str, ...],
    metadata: InstalledInspectionMetadata,
) -> RepositoryRelation:
    if output.returncode != 0:
        return RepositoryRelation.UNKNOWN
    if metadata.channel_identity.repository in origins:
        return RepositoryRelation.CANONICAL
    reviewed = {
        migration["from_repository"]
        for migration in metadata.seed_lock.allowed_repository_migrations
    }
    return (
        RepositoryRelation.REVIEWED_HISTORICAL
        if reviewed & set(origins)
        else RepositoryRelation.OTHER
    )


def _target_identity_checks(
    values: dict[str, object], metadata: InstalledInspectionMetadata
) -> tuple[InspectionCheck, ...]:
    """The identity conjuncts against the release the identity was PROVED against.

    A clone one release behind matches a ``pre_manager_seed`` anchor, not the
    installed channel release; comparing its HEAD to the channel would report
    ``failed`` (exit 1) for the runbook's own primary case (Step 7, CH-1
    measured), so the expected commit, tree and provenance are the matched
    anchor's when an anchor proved the identity and the channel's otherwise.
    """
    anchor = cast(InspectionAnchor | None, values["anchor"])
    channel = metadata.channel_identity
    expected_commit, expected_tree, expected_provenance = channel.commit, channel.tree_hash, channel.provenance_sha256
    if anchor is not None and cast(InspectionAnchorKind, values["anchor_kind"]) is not InspectionAnchorKind.CURRENT_CHANNEL:
        expected_commit, expected_tree = anchor.commit, anchor.tree_hash
        expected_provenance = anchor.provenance_sha256 or expected_provenance
    return (
        _identity_check(
            "head_commit",
            "HEAD commit inspected.",
            values["head"],
            expected_commit,
            "head_commit_mismatch",
        ),
        _identity_check(
            "head_tree",
            "HEAD tree inspected.",
            values["tree"],
            expected_tree,
            "head_tree_mismatch",
        ),
        _provenance_check(values, expected_provenance),
        _working_provenance_check(values, expected_provenance),
        _trailer_check(values),
        _origin_check(values, metadata),
        _working_tree_check(values),
    )


def _identity_check(
    check_id: str, summary: str, observed: object, expected: str, reason: str
) -> InspectionCheck:
    matches = observed == expected
    return _check(
        check_id,
        "identity",
        True,
        InspectionStatus.VERIFIED if matches else InspectionStatus.FAILED,
        None if matches else reason,
        summary,
        cast(JsonValue, observed),
        expected,
        "git",
    )


def _provenance_check(values: dict[str, object], expected_provenance: str) -> InspectionCheck:
    condition = cast(ProvenanceCondition, values["provenance"])
    digest = cast(str | None, values["provenance_digest"])
    verified = condition is ProvenanceCondition.STRICT and digest == expected_provenance
    status = (
        InspectionStatus.VERIFIED
        if verified
        else (
            InspectionStatus.MISSING
            if condition is ProvenanceCondition.MISSING
            else InspectionStatus.FAILED
        )
    )
    reason = (
        "provenance_digest_mismatch"
        if condition is ProvenanceCondition.STRICT
        else "provenance_unavailable"
    )
    return _check(
        "committed_provenance",
        "identity",
        True,
        status,
        reason,
        "Committed provenance inspected.",
        digest,
        expected_provenance,
        "git",
    )


def _working_provenance_check(values: dict[str, object], expected_provenance: str) -> InspectionCheck:
    condition = cast(ProvenanceCondition, values["working_provenance"])
    digest = cast(str | None, values["working_provenance_digest"])
    matches = bool(values["working_matches_committed"])
    verified = condition is ProvenanceCondition.STRICT and digest == expected_provenance and matches
    return _check(
        "working_provenance",
        "identity",
        True,
        InspectionStatus.VERIFIED if verified else InspectionStatus.FAILED,
        None if verified else "working_provenance_mismatch",
        "Working provenance is strict and byte-identical to committed HEAD.",
        digest,
        expected_provenance,
        "pinned_descriptor",
    )


def _trailer_check(values: dict[str, object]) -> InspectionCheck:
    verified = bool(values["trailers_verified"])
    return _check(
        "seal_trailers",
        "identity",
        True,
        InspectionStatus.VERIFIED if verified else InspectionStatus.FAILED,
        None if verified else "seal_trailers_mismatch",
        "HEAD seal trailers bind the strict provenance.",
        verified,
        True,
        "git",
    )


def _origin_check(
    values: dict[str, object], metadata: InstalledInspectionMetadata
) -> InspectionCheck:
    relation = cast(RepositoryRelation, values["repository_relation"])
    return _check(
        "origin_urls",
        "git",
        True,
        (
            InspectionStatus.UNKNOWN
            if relation is RepositoryRelation.UNKNOWN
            else InspectionStatus.VERIFIED
        ),
        None,
        "Origin URLs inspected.",
        list(cast(tuple[str, ...], values["origins"])),
        metadata.channel_identity.repository,
        "git",
    )


def _working_tree_check(values: dict[str, object]) -> InspectionCheck:
    observed = {
        "tracked": list(cast(tuple[str, ...], values["tracked"])),
        "untracked": list(cast(tuple[str, ...], values["untracked"])),
    }
    return _check(
        "working_tree",
        "git",
        True,
        InspectionStatus.VERIFIED if values["status_observed"] else InspectionStatus.UNKNOWN,
        None,
        "Working tree inspected.",
        cast(JsonValue, observed),
        None,
        "git",
    )


def facts_from_values(
    values: dict[str, object], metadata: InstalledInspectionMetadata
) -> ExistingInstallFacts:
    if not values:
        return _unavailable_existing_install_facts()
    identity = _identity_observations(values)
    paths = _path_observations(values)
    return ExistingInstallFacts(
        identity.condition,
        identity.anchor_kind,
        identity.anchor_id,
        identity.status,
        identity.repository_relation,
        identity.relation,
        cast(str | None, values["head"]),
        cast(str | None, values["tree"]),
        _working_tree_condition(paths.tracked, paths.untracked),
        ObservedPaths(paths.path_availability, paths.tracked),
        ObservedPaths(paths.path_availability, paths.untracked),
        paths.empty,
        paths.empty,
        paths.empty,
        paths.empty,
        ObservedPathPairs(paths.topology_availability, ()),
        paths.empty,
        paths.linked,
        paths.empty,
        cast(ObservedBoolean, values["shallow"]),
        cast(ObservedBoolean, values["detached"]),
        cast(str | None, values["branch"]),
        cast(str | None, values["upstream"]),
        cast(tuple[str, ...], values["origins"]),
        paths.hazards,
        paths.staged,
        paths.tracked_entries,
    )


@dataclass(frozen=True)
class _IdentityObservations:
    condition: ProvenanceCondition
    anchor_kind: InspectionAnchorKind
    anchor_id: str | None
    status: InspectionStatus
    repository_relation: RepositoryRelation
    relation: ChannelRelation


def _identity_observations(values: dict[str, object]) -> _IdentityObservations:
    identity_ok = cast(bool, values["identity"])
    anchor = cast(InspectionAnchor | None, values["anchor"])
    anchor_kind = cast(InspectionAnchorKind, values["anchor_kind"])
    return _IdentityObservations(
        cast(ProvenanceCondition, values["provenance"]),
        anchor_kind,
        anchor.anchor_id if anchor is not None else None,
        InspectionStatus.VERIFIED if identity_ok else InspectionStatus.FAILED,
        cast(RepositoryRelation, values["repository_relation"]),
        _channel_relation(values, anchor_kind, identity_ok),
    )


def _channel_relation(
    values: dict[str, object], anchor_kind: InspectionAnchorKind, identity_ok: bool
) -> ChannelRelation:
    if anchor_kind is InspectionAnchorKind.CURRENT_CHANNEL and identity_ok:
        return ChannelRelation.CURRENT
    return cast(ChannelRelation, values["anchor_relation"])


@dataclass(frozen=True)
class _PathObservations:
    tracked: tuple[str, ...]
    untracked: tuple[str, ...]
    path_availability: ObservationAvailability
    topology_availability: ObservationAvailability
    empty: ObservedPaths
    linked: ObservedPaths
    hazards: tuple[str, ...]
    staged: ObservedPaths
    tracked_entries: ObservedRawRows


def _path_observations(values: dict[str, object]) -> _PathObservations:
    topology_observed = all(
        bool(values[item])
        for item in (
            "cached_observed",
            "worktree_diff_observed",
            "index_observed",
            "worktrees_observed",
        )
    )
    topology = (
        ObservationAvailability.OBSERVED if topology_observed else ObservationAvailability.UNKNOWN
    )
    linked_values = cast(tuple[str, ...], values["linked_worktrees"])
    cached = ObservationAvailability.OBSERVED if values["cached_observed"] else ObservationAvailability.UNKNOWN
    raw = ObservationAvailability.OBSERVED if values["raw_diff_observed"] else ObservationAvailability.UNKNOWN
    return _PathObservations(
        cast(tuple[str, ...], values["tracked"]),
        cast(tuple[str, ...], values["untracked"]),
        ObservationAvailability.OBSERVED
        if values["status_observed"]
        else ObservationAvailability.UNKNOWN,
        topology,
        ObservedPaths(topology, ()),
        ObservedPaths(topology, linked_values),
        _hazard_codes(values, linked_values),
        ObservedPaths(cached, cast(tuple[str, ...], values["staged"])),
        ObservedRawRows(raw, cast(tuple[RawRow, ...], values["tracked_entries"])),
    )


def _hazard_codes(values: dict[str, object], linked: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(
        name
        for name, present in (
            ("shallow_history", values["shallow"] is ObservedBoolean.TRUE),
            ("detached_head", values["detached"] is ObservedBoolean.TRUE),
            ("linked_worktree", bool(linked)),
        )
        if present
    )


def _unavailable_existing_install_facts() -> ExistingInstallFacts:
    unknown = ObservedPaths(ObservationAvailability.UNKNOWN, ())
    return ExistingInstallFacts(
        ProvenanceCondition.UNKNOWN,
        InspectionAnchorKind.NONE,
        None,
        InspectionStatus.FAILED,
        RepositoryRelation.UNKNOWN,
        ChannelRelation.UNKNOWN,
        None,
        None,
        WorkingTreeCondition.UNKNOWN,
        unknown,
        unknown,
        unknown,
        unknown,
        unknown,
        unknown,
        ObservedPathPairs(ObservationAvailability.UNKNOWN, ()),
        unknown,
        unknown,
        unknown,
        ObservedBoolean.UNKNOWN,
        ObservedBoolean.UNKNOWN,
    )


def _working_tree_condition(
    tracked: tuple[str, ...], untracked: tuple[str, ...]
) -> WorkingTreeCondition:
    return {
        (False, False): WorkingTreeCondition.CLEAN,
        (True, False): WorkingTreeCondition.TRACKED_CHANGES,
        (False, True): WorkingTreeCondition.UNTRACKED_ONLY,
        (True, True): WorkingTreeCondition.MIXED_CHANGES,
    }[(bool(tracked), bool(untracked))]
