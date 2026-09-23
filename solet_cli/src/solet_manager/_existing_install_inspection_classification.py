"""Classification rules for the public existing-install inspection surface."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from typing import Literal

from .existing_install_inspection import (
    ChannelRelation,
    ExistingInstallClass,
    ExistingInstallClassification,
    ExistingInstallFacts,
    InspectionAnchorKind,
    InspectionStatus,
    ObservationAvailability,
    ObservedBoolean,
    ProvenanceCondition,
    RepositoryRelation,
    WorkingTreeCondition,
)


def classify_existing_install(facts: ExistingInstallFacts) -> ExistingInstallClassification:
    matches = tuple(result for predicate, result in _ROWS if predicate(facts))
    if len(matches) != 1:
        raise ValueError("inconsistent_existing_install_facts")
    result = matches[0]
    if result.installation_class in _FACETED_CLASSES:
        return replace(result, reason_codes=(*result.reason_codes, *local_state_facets(facts)))
    return result


def _incomplete(facts: ExistingInstallFacts) -> bool:
    paths = (
        facts.tracked_paths,
        facts.untracked_paths,
        facts.ignored_paths,
        facts.transition_paths,
        facts.tracked_transition_overlap,
        facts.untracked_destination_collisions,
        facts.submodules,
        facts.linked_worktrees,
        facts.repository_operations,
    )
    return (
        facts.identity_status is InspectionStatus.UNKNOWN
        or facts.repository_relation is RepositoryRelation.UNKNOWN
        or facts.shallow is ObservedBoolean.UNKNOWN
        or facts.detached is ObservedBoolean.UNKNOWN
        or facts.casefold_collisions.availability is not ObservationAvailability.OBSERVED
        or any(item.availability is not ObservationAvailability.OBSERVED for item in paths)
    )


def _hazard(facts: ExistingInstallFacts) -> bool:
    """A7's blocking hazards.  Untracked presence is a facet of ``local_changes_present``
    (Step 7 section 6.4), not a hazard: every seed-born clone carries genesis-written
    untracked paths, and the candidate-aware collision proof lives in the update preview."""
    return bool(
        facts.tracked_transition_overlap.values
        or facts.untracked_destination_collisions.values
        or facts.casefold_collisions.values
        or facts.submodules.values
        or facts.linked_worktrees.values
        or facts.repository_operations.values
        or facts.shallow is ObservedBoolean.TRUE
        or facts.detached is ObservedBoolean.TRUE
    )


def _development_anchor(facts: ExistingInstallFacts) -> bool:
    return facts.anchor_kind is InspectionAnchorKind.DEVELOPMENT_CHECKOUT


def _provenance_unavailable(facts: ExistingInstallFacts) -> bool:
    unavailable = {
        ProvenanceCondition.MISSING,
        ProvenanceCondition.MALFORMED,
        ProvenanceCondition.UNKNOWN,
    }
    return (
        facts.provenance_condition in unavailable and facts.anchor_kind is InspectionAnchorKind.NONE
    )


def _identity_failed(facts: ExistingInstallFacts) -> bool:
    return (
        facts.provenance_condition is ProvenanceCondition.STRICT
        and facts.identity_status is InspectionStatus.FAILED
    )


def _legacy_provenance(facts: ExistingInstallFacts) -> bool:
    return facts.anchor_kind is InspectionAnchorKind.LEGACY_PROVENANCE


def _diverged_seed_history(facts: ExistingInstallFacts) -> bool:
    return facts.channel_relation is ChannelRelation.DIVERGED


def _reviewed_historical_repository(facts: ExistingInstallFacts) -> bool:
    return facts.repository_relation is RepositoryRelation.REVIEWED_HISTORICAL


def _unknown_repository(facts: ExistingInstallFacts) -> bool:
    return facts.repository_relation is RepositoryRelation.OTHER


def _local_changes(facts: ExistingInstallFacts) -> bool:
    return facts.working_tree in {
        WorkingTreeCondition.TRACKED_CHANGES,
        WorkingTreeCondition.UNTRACKED_ONLY,
        WorkingTreeCondition.MIXED_CHANGES,
    }


def _pre_manager_seed(facts: ExistingInstallFacts) -> bool:
    return facts.anchor_kind is InspectionAnchorKind.PRE_MANAGER_SEED


def _complete_verified(facts: ExistingInstallFacts) -> bool:
    return not _incomplete(facts) and facts.identity_status is InspectionStatus.VERIFIED


def _current_or_fast_forward(facts: ExistingInstallFacts) -> bool:
    return facts.channel_relation in {ChannelRelation.CURRENT, ChannelRelation.FAST_FORWARD}


def _observed_working_tree(facts: ExistingInstallFacts) -> bool:
    """Every observed working-tree condition; ``UNKNOWN`` is row 4's by ``_incomplete``.

    Rows 8 and 9 admit an untracked-only or mixed tree on a reviewed-historical or
    OTHER origin (Step 7 B4): they block for update regardless, and without this
    widening those shapes would match no row once ``_hazard`` stopped treating
    untracked presence as a hazard.
    """
    return facts.working_tree in {
        WorkingTreeCondition.CLEAN,
        WorkingTreeCondition.TRACKED_CHANGES,
        WorkingTreeCondition.UNTRACKED_ONLY,
        WorkingTreeCondition.MIXED_CHANGES,
    }


def local_state_facets(facts: ExistingInstallFacts) -> tuple[str, ...]:
    """The local-state facets an operator reads off ``inspect``, in the closed order."""
    facets: list[str] = []
    if facts.tracked_paths.values:
        facets.append("tracked_changes")
    if facts.untracked_paths.values:
        facets.append("untracked_paths")
    if facts.staged_paths.values:
        facets.append("staged_changes")
    return tuple(facets)


def _row_development(facts: ExistingInstallFacts) -> bool:
    return _development_anchor(facts)


def _row_provenance_unavailable(facts: ExistingInstallFacts) -> bool:
    return not _row_development(facts) and _provenance_unavailable(facts)


def _row_source_identity_unproven(facts: ExistingInstallFacts) -> bool:
    return (
        not _row_development(facts)
        and not _provenance_unavailable(facts)
        and _identity_failed(facts)
    )


def _row_incomplete(facts: ExistingInstallFacts) -> bool:
    return (
        not _row_development(facts)
        and not _provenance_unavailable(facts)
        and not _identity_failed(facts)
        and _incomplete(facts)
    )


def _row_legacy(facts: ExistingInstallFacts) -> bool:
    return (
        _complete_verified(facts)
        and _legacy_provenance(facts)
        and facts.provenance_condition is ProvenanceCondition.MISSING
        and facts.channel_relation is ChannelRelation.LEGACY_BRIDGE_REQUIRED
    )


def _row_diverged(facts: ExistingInstallFacts) -> bool:
    return _complete_verified(facts) and _diverged_seed_history(facts) and not _row_legacy(facts)


def _row_hazard(facts: ExistingInstallFacts) -> bool:
    return (
        _complete_verified(facts)
        and facts.channel_relation
        not in {ChannelRelation.DIVERGED, ChannelRelation.LEGACY_BRIDGE_REQUIRED}
        and _hazard(facts)
    )


def _row_historical(facts: ExistingInstallFacts) -> bool:
    return (
        _complete_verified(facts)
        and _current_or_fast_forward(facts)
        and not _hazard(facts)
        and _observed_working_tree(facts)
        and _reviewed_historical_repository(facts)
    )


def _row_other_remote(facts: ExistingInstallFacts) -> bool:
    return (
        _complete_verified(facts)
        and _current_or_fast_forward(facts)
        and not _hazard(facts)
        and _observed_working_tree(facts)
        and _unknown_repository(facts)
    )


def _row_local_changes(facts: ExistingInstallFacts) -> bool:
    """Row 10 (Step 7 section 6.4): a canonical, current-or-fast-forward clone with local
    state.  ``inspect`` has no candidate and cannot know "disjoint"; the update preview is
    the gate that weighs the facets this row reports."""
    return (
        _complete_verified(facts)
        and facts.repository_relation is RepositoryRelation.CANONICAL
        and _current_or_fast_forward(facts)
        and _local_changes(facts)
        and not _hazard(facts)
    )


def _row_pre_manager(facts: ExistingInstallFacts) -> bool:
    return (
        _complete_verified(facts)
        and _pre_manager_seed(facts)
        and facts.repository_relation is RepositoryRelation.CANONICAL
        and _current_or_fast_forward(facts)
        and facts.working_tree is WorkingTreeCondition.CLEAN
        and not _hazard(facts)
    )


def _row_clean_current(facts: ExistingInstallFacts) -> bool:
    return (
        _complete_verified(facts)
        and facts.anchor_kind is InspectionAnchorKind.CURRENT_CHANNEL
        and facts.provenance_condition is ProvenanceCondition.STRICT
        and facts.repository_relation is RepositoryRelation.CANONICAL
        and facts.channel_relation is ChannelRelation.CURRENT
        and facts.working_tree is WorkingTreeCondition.CLEAN
        and not _hazard(facts)
    )


def _classification(
    kind: ExistingInstallClass,
    import_disposition: Literal["allow", "diagnostic_only", "refuse"],
    update_disposition: Literal[
        "allowed_after_import", "legacy_bridge_required", "blocked", "refuse"
    ],
    attention: bool,
    reason: str,
) -> ExistingInstallClassification:
    return ExistingInstallClassification(
        kind, (reason,), import_disposition, update_disposition, attention
    )


#: Rows whose ``reason_codes`` carry the local-state facets after the row's own reason
#: (Step 7 section 6.4): row 10 by definition, rows 8/9 so the operator sees why the tree
#: is dirty as well as why the origin is not canonical.
_FACETED_CLASSES = frozenset(
    {
        ExistingInstallClass.LOCAL_CHANGES_PRESENT,
        ExistingInstallClass.REVIEWED_HISTORICAL_REPOSITORY,
        ExistingInstallClass.UNKNOWN_REPOSITORY_CANONICAL_COMMIT,
    }
)

_ROWS: tuple[tuple[Callable[[ExistingInstallFacts], bool], ExistingInstallClassification], ...] = (
    (
        _row_development,
        _classification(
            ExistingInstallClass.DEVELOPMENT_CHECKOUT,
            "refuse",
            "refuse",
            True,
            "development_anchor",
        ),
    ),
    (
        _row_provenance_unavailable,
        _classification(
            ExistingInstallClass.PROVENANCE_UNAVAILABLE,
            "refuse",
            "blocked",
            True,
            "provenance_unavailable",
        ),
    ),
    (
        _row_source_identity_unproven,
        _classification(
            ExistingInstallClass.SOURCE_IDENTITY_UNPROVEN,
            "diagnostic_only",
            "blocked",
            True,
            "source_identity_unproven",
        ),
    ),
    (
        _row_incomplete,
        _classification(
            ExistingInstallClass.INSPECTION_INCOMPLETE,
            "diagnostic_only",
            "blocked",
            True,
            "inspection_incomplete",
        ),
    ),
    (
        _row_legacy,
        _classification(
            ExistingInstallClass.LEGACY_PROVENANCE,
            "allow",
            "legacy_bridge_required",
            True,
            "legacy_bridge_required",
        ),
    ),
    (
        _row_diverged,
        _classification(
            ExistingInstallClass.DIVERGED_SEED_HISTORY,
            "allow",
            "blocked",
            True,
            "diverged_seed_history",
        ),
    ),
    (
        _row_hazard,
        _classification(
            ExistingInstallClass.BLOCKING_LOCAL_STATE,
            "allow",
            "blocked",
            True,
            "blocking_local_state",
        ),
    ),
    (
        _row_historical,
        _classification(
            ExistingInstallClass.REVIEWED_HISTORICAL_REPOSITORY,
            "allow",
            "blocked",
            True,
            "reviewed_historical_repository",
        ),
    ),
    (
        _row_other_remote,
        _classification(
            ExistingInstallClass.UNKNOWN_REPOSITORY_CANONICAL_COMMIT,
            "allow",
            "blocked",
            True,
            "unknown_repository_canonical_commit",
        ),
    ),
    (
        _row_local_changes,
        _classification(
            ExistingInstallClass.LOCAL_CHANGES_PRESENT,
            "allow",
            "allowed_after_import",
            True,
            "local_changes_present",
        ),
    ),
    (
        _row_pre_manager,
        _classification(
            ExistingInstallClass.PRE_MANAGER_SEED_CLONE,
            "allow",
            "allowed_after_import",
            True,
            "pre_manager_seed",
        ),
    ),
    (
        _row_clean_current,
        _classification(
            ExistingInstallClass.CLEAN_FAST_FORWARD_SEED_CLONE,
            "allow",
            "allowed_after_import",
            False,
            "clean_current_seed",
        ),
    ),
)
