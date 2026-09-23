"""Classifier coverage over the producible fact space (Step 7 design section 6.4, M3; criterion 15).

Enumerates every dataclass-consistent ``ExistingInstallFacts`` shape --
``working_tree`` (5) x ``repository_relation`` (4) x ``anchor_kind`` (5) x
``channel_relation`` (5) x ``provenance_condition`` (4) x ``identity_status`` (5)
x ``detached`` (3) x ``shallow`` (3) x untracked-present x linked-worktree-present
x tracked-present, filtered to what the builder can produce -- and runs it through
the LANDED (Step 2 r6) row set, reproduced here as the reference, and the Step-7
row set.  Measured at ``c33c11f32``: 91 080 shapes; landed classifies 84 266
exactly once, 6 528 match zero rows and 286 match two (section 12.9, frozen).

Criteria (section 6.4): (a) every shape the landed rows classified exactly once
is classified exactly once by the new rows; (b) no shape matches two rows that
did not before; (c) the zero/multi set is frozen as a fixture --
``fixtures/existing_install_inspection/classification_frozen_shapes.json`` holds
the exact counts, a sha256 over the canonical sorted listing of every zero and
multi shape, and a readable histogram by (identity, anchor, channel,
provenance) -- so any movement is a deliberate, reviewed edit of that file.
(Section 12.9's prose characterisation is only approximate: measured, the zero
set also holds 1 440 ``failed``-identity shapes with a non-strict provenance and
96 ``verified`` ones on a ``legacy_bridge_required`` channel under a
``pre_manager_seed`` anchor; the fixture, not the prose, is the freeze.)
Also: exactly 18 shapes change class, all from
``blocking_local_state`` (6 -> ``local_changes_present``, 6 ->
``reviewed_historical_repository``, 6 -> ``unknown_repository_canonical_commit``),
and the round-2 review's 12 zero-match regressions reappear when rows 8/9 are
NOT widened (B4), proving the widening is load-bearing.
"""

from __future__ import annotations

import hashlib
import itertools
import json
import sys
from collections import Counter
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(_ROOT / "solet_cli" / "src"), str(_ROOT / "solet_setup_contracts" / "src"), str(_ROOT / "solet_cli" / "tests")]
from existing_install_inspection_contract_smoke import _classification_witnesses  # noqa: E402
from solet_manager import _existing_install_inspection_classification as classification  # noqa: E402
from solet_manager.existing_install_inspection import (  # noqa: E402
    ChannelRelation,
    ExistingInstallClass,
    ExistingInstallFacts,
    InspectionAnchorKind,
    InspectionStatus,
    ObservationAvailability,
    ObservedBoolean,
    ObservedPaths,
    ProvenanceCondition,
    RepositoryRelation,
    WorkingTreeCondition,
)

_LANDED_SHAPES = 91080
_LANDED_ONCE = 84266
_LANDED_ZERO = 6528
_LANDED_MULTI = 286
_EXPECTED_MOVES = {
    ("blocking_local_state", "local_changes_present"): 6,
    ("blocking_local_state", "reviewed_historical_repository"): 6,
    ("blocking_local_state", "unknown_repository_canonical_commit"): 6,
    # D13: the landed row-10 class is RENAMED, not moved -- the three tracked-only shapes keep their row under the new name.
    ("disjoint_local_tracked_changes", "local_changes_present"): 3,
}
_B4_REGRESSIONS = 12
_FROZEN = _ROOT / "solet_cli" / "tests" / "fixtures" / "existing_install_inspection" / "classification_frozen_shapes.json"
Shape = tuple[object, ...]
Predicate = Callable[[ExistingInstallFacts], bool]


def _shape_facts(base: ExistingInstallFacts, shape: Shape) -> ExistingInstallFacts | None:
    working_tree, repository, anchor, channel, provenance, identity, detached, shallow, untracked, linked, tracked = shape
    availability = ObservationAvailability.UNKNOWN if working_tree is WorkingTreeCondition.UNKNOWN else ObservationAvailability.OBSERVED
    observed = ObservationAvailability.OBSERVED
    try:
        return replace(
            base,
            working_tree=working_tree,  # type: ignore[arg-type]
            repository_relation=repository,  # type: ignore[arg-type]
            anchor_kind=anchor,  # type: ignore[arg-type]
            channel_relation=channel,  # type: ignore[arg-type]
            provenance_condition=provenance,  # type: ignore[arg-type]
            identity_status=identity,  # type: ignore[arg-type]
            detached=detached,  # type: ignore[arg-type]
            shallow=shallow,  # type: ignore[arg-type]
            untracked_paths=ObservedPaths(availability, (".gitignore",) if (untracked and availability is observed) else ()),
            tracked_paths=ObservedPaths(availability, ("NOTICE",) if (tracked and availability is observed) else ()),
            linked_worktrees=ObservedPaths(observed, ("wt",) if linked else ()),
        )
    except ValueError:
        return None


def _consistent(shape: Shape) -> bool:
    working_tree, *_, untracked, _linked, tracked = shape
    if working_tree is WorkingTreeCondition.UNKNOWN:
        return not tracked and not untracked
    derived = {(False, False): WorkingTreeCondition.CLEAN, (True, False): WorkingTreeCondition.TRACKED_CHANGES, (False, True): WorkingTreeCondition.UNTRACKED_ONLY, (True, True): WorkingTreeCondition.MIXED_CHANGES}
    return derived[(bool(tracked), bool(untracked))] is working_tree


def _space(base: ExistingInstallFacts) -> list[tuple[Shape, ExistingInstallFacts]]:
    product = itertools.product(list(WorkingTreeCondition), list(RepositoryRelation), list(InspectionAnchorKind), list(ChannelRelation), list(ProvenanceCondition), list(InspectionStatus), [ObservedBoolean.TRUE, ObservedBoolean.FALSE, ObservedBoolean.UNKNOWN], [ObservedBoolean.TRUE, ObservedBoolean.FALSE, ObservedBoolean.UNKNOWN], [True, False], [True, False], [True, False])
    rows: list[tuple[Shape, ExistingInstallFacts]] = []
    for shape in product:
        if not _consistent(shape):
            continue
        facts = _shape_facts(base, shape)
        if facts is not None:
            rows.append((shape, facts))
    return rows


# --- the landed (Step 2 r6) rows, reproduced as the reference -------------------------------------------


def _landed_hazard(facts: ExistingInstallFacts) -> bool:
    return bool(facts.untracked_paths.values) or classification._hazard(facts)  # noqa: SLF001 - the reference row set reads the module's own conjuncts


def _landed_disjoint_or_clean(facts: ExistingInstallFacts) -> bool:
    return facts.working_tree in {WorkingTreeCondition.CLEAN, WorkingTreeCondition.TRACKED_CHANGES}


def _landed_row_hazard(facts: ExistingInstallFacts) -> bool:
    return classification._complete_verified(facts) and facts.channel_relation not in {ChannelRelation.DIVERGED, ChannelRelation.LEGACY_BRIDGE_REQUIRED} and _landed_hazard(facts)  # noqa: SLF001


def _landed_row_historical(facts: ExistingInstallFacts) -> bool:
    return classification._complete_verified(facts) and classification._current_or_fast_forward(facts) and not _landed_hazard(facts) and _landed_disjoint_or_clean(facts) and facts.repository_relation is RepositoryRelation.REVIEWED_HISTORICAL  # noqa: SLF001


def _landed_row_other(facts: ExistingInstallFacts) -> bool:
    return classification._complete_verified(facts) and classification._current_or_fast_forward(facts) and not _landed_hazard(facts) and _landed_disjoint_or_clean(facts) and facts.repository_relation is RepositoryRelation.OTHER  # noqa: SLF001


def _landed_row_disjoint_tracked(facts: ExistingInstallFacts) -> bool:
    return (
        classification._complete_verified(facts)  # noqa: SLF001
        and facts.repository_relation is RepositoryRelation.CANONICAL
        and classification._current_or_fast_forward(facts)  # noqa: SLF001
        and facts.working_tree is WorkingTreeCondition.TRACKED_CHANGES
        and facts.transition_paths.availability is ObservationAvailability.OBSERVED
        and not facts.tracked_transition_overlap.values
        and not _landed_hazard(facts)
    )


def _landed_row_pre_manager(facts: ExistingInstallFacts) -> bool:
    return classification._complete_verified(facts) and facts.anchor_kind is InspectionAnchorKind.PRE_MANAGER_SEED and facts.repository_relation is RepositoryRelation.CANONICAL and classification._current_or_fast_forward(facts) and facts.working_tree is WorkingTreeCondition.CLEAN and not _landed_hazard(facts)  # noqa: SLF001


def _landed_row_clean_current(facts: ExistingInstallFacts) -> bool:
    return classification._complete_verified(facts) and facts.anchor_kind is InspectionAnchorKind.CURRENT_CHANNEL and facts.provenance_condition is ProvenanceCondition.STRICT and facts.repository_relation is RepositoryRelation.CANONICAL and facts.channel_relation is ChannelRelation.CURRENT and facts.working_tree is WorkingTreeCondition.CLEAN and not _landed_hazard(facts)  # noqa: SLF001


_LANDED_ROWS: tuple[tuple[Predicate, str], ...] = (
    (classification._row_development, "development_checkout"),  # noqa: SLF001
    (classification._row_provenance_unavailable, "provenance_unavailable"),  # noqa: SLF001
    (classification._row_source_identity_unproven, "source_identity_unproven"),  # noqa: SLF001
    (classification._row_incomplete, "inspection_incomplete"),  # noqa: SLF001
    (classification._row_legacy, "legacy_provenance"),  # noqa: SLF001
    (classification._row_diverged, "diverged_seed_history"),  # noqa: SLF001
    (_landed_row_hazard, "blocking_local_state"),
    (_landed_row_historical, "reviewed_historical_repository"),
    (_landed_row_other, "unknown_repository_canonical_commit"),
    (_landed_row_disjoint_tracked, "disjoint_local_tracked_changes"),
    (_landed_row_pre_manager, "pre_manager_seed_clone"),
    (_landed_row_clean_current, "clean_fast_forward_seed_clone"),
)


def _unwidened_rows() -> tuple[tuple[Predicate, str], ...]:
    """The Step-7 rows with rows 8/9 as landed (the round-2 review's B4 table)."""

    def historical(facts: ExistingInstallFacts) -> bool:
        return classification._complete_verified(facts) and classification._current_or_fast_forward(facts) and not classification._hazard(facts) and _landed_disjoint_or_clean(facts) and facts.repository_relation is RepositoryRelation.REVIEWED_HISTORICAL  # noqa: SLF001

    def other(facts: ExistingInstallFacts) -> bool:
        return classification._complete_verified(facts) and classification._current_or_fast_forward(facts) and not classification._hazard(facts) and _landed_disjoint_or_clean(facts) and facts.repository_relation is RepositoryRelation.OTHER  # noqa: SLF001

    rows: list[tuple[Predicate, str]] = []
    for predicate, result in classification._ROWS:  # noqa: SLF001
        if predicate is classification._row_historical:  # noqa: SLF001
            rows.append((historical, result.installation_class.value))
        elif predicate is classification._row_other_remote:  # noqa: SLF001
            rows.append((other, result.installation_class.value))
        else:
            rows.append((predicate, result.installation_class.value))
    return tuple(rows)


def _run(rows: tuple[tuple[Predicate, str], ...], space: list[tuple[Shape, ExistingInstallFacts]]) -> dict[Shape, tuple[str, ...]]:
    return {shape: tuple(name for predicate, name in rows if predicate(facts)) for shape, facts in space}


def _shape_text(shape: Shape) -> str:
    return "|".join(getattr(item, "value", str(item)) for item in shape)


def _freeze(shapes: set[Shape]) -> dict[str, object]:
    listing = sorted(_shape_text(shape) for shape in shapes)
    histogram = Counter("|".join(getattr(shape[index], "value", str(shape[index])) for index in (5, 2, 3, 4)) for shape in shapes)
    return {"count": len(listing), "sha256": hashlib.sha256("\n".join(listing).encode()).hexdigest(), "by_identity_anchor_channel_provenance": dict(sorted(histogram.items()))}


def _assert_frozen(zero: set[Shape], multi: set[Shape]) -> None:
    """(c): the zero/multi set equals the fixture; on a mismatch the differing histogram rows are named."""
    expected = json.loads(_FROZEN.read_text(encoding="utf-8"))
    observed = {"schema_version": 1, "zero": _freeze(zero), "multi": _freeze(multi)}
    for key in ("zero", "multi"):
        want, have = expected[key], observed[key]
        if want != have:
            moved = {row: (want["by_identity_anchor_channel_provenance"].get(row), have["by_identity_anchor_channel_provenance"].get(row)) for row in set(want["by_identity_anchor_channel_provenance"]) | set(have["by_identity_anchor_channel_provenance"]) if want["by_identity_anchor_channel_provenance"].get(row) != have["by_identity_anchor_channel_provenance"].get(row)}
            raise AssertionError(f"(c) the frozen {key}-match set moved: count {want['count']} -> {have['count']}, histogram deltas {moved}; edit {_FROZEN.name} deliberately if intended")


def _partition(matches: dict[Shape, tuple[str, ...]]) -> tuple[set[Shape], set[Shape], set[Shape]]:
    """(once, zero, multi) by how many rows matched each shape."""
    once = {shape for shape, rows in matches.items() if len(rows) == 1}
    zero = {shape for shape, rows in matches.items() if not rows}
    multi = {shape for shape, rows in matches.items() if len(rows) > 1}
    return once, zero, multi


def _assert_landed_baseline(space: list[tuple[Shape, ExistingInstallFacts]]) -> tuple[dict[Shape, tuple[str, ...]], set[Shape], set[Shape], set[Shape]]:
    """The landed (pre-Step-7) table over the space: 84,266 once / 6,528 zero / 286 multi."""
    landed = _run(_LANDED_ROWS, space)
    once, zero, multi = _partition(landed)
    assert (len(once), len(zero), len(multi)) == (_LANDED_ONCE, _LANDED_ZERO, _LANDED_MULTI), (len(once), len(zero), len(multi))
    return landed, once, zero, multi


def _assert_criteria(current: dict[Shape, tuple[str, ...]], once: set[Shape], zero: set[Shape], multi: set[Shape]) -> None:
    """6.4 (a)-(c): no once-shape lost its row or gained a second; the zero/multi set is frozen."""
    regressions = [shape for shape in once if not current[shape]]
    new_multi = [shape for shape in once if len(current[shape]) > 1]
    assert not regressions, f"(a) {len(regressions)} shapes lost their row"
    assert not new_multi, f"(b) {len(new_multi)} shapes gained a second row"
    once_now, zero_now, multi_now = _partition(current)
    newly = once_now - once
    assert not newly, f"(c) {len(newly)} zero/multi shapes moved; the frozen set changed"
    assert zero_now == zero and multi_now == multi, "(c) the zero/multi set is not frozen"
    _assert_frozen(zero_now, multi_now)
    assert all(shape[2] is InspectionAnchorKind.DEVELOPMENT_CHECKOUT and shape[5] is InspectionStatus.VERIFIED for shape in multi), "the multi set is no longer exactly the development anchor with a verified identity"


def _assert_moves(landed: dict[Shape, tuple[str, ...]], current: dict[Shape, tuple[str, ...]], once: set[Shape]) -> Counter[tuple[str, str]]:
    """The 18 class transitions, all from blocking_local_state and all on the two local-state tree conditions."""
    moved = [shape for shape in once if landed[shape] != current[shape]]
    moves = Counter((landed[shape][0], current[shape][0]) for shape in moved)
    assert dict(moves) == _EXPECTED_MOVES, dict(moves)
    from_blocking = [shape for shape in moved if landed[shape][0] == "blocking_local_state"]
    assert all(shape[0] in {WorkingTreeCondition.UNTRACKED_ONLY, WorkingTreeCondition.MIXED_CHANGES} for shape in from_blocking)
    return moves


def _assert_b4(space: list[tuple[Shape, ExistingInstallFacts]], once: set[Shape]) -> int:
    """B4: without the rows-8/9 widening the round-2 review's 12 regressions reappear."""
    unwidened = _run(_unwidened_rows(), space)
    b4 = [shape for shape in once if not unwidened[shape]]
    assert len(b4) == _B4_REGRESSIONS, len(b4)
    expected = {(tree, relation) for tree in ("untracked_only", "mixed_changes") for relation in ("reviewed_historical", "other")}
    assert {(shape[0].value, shape[1].value) for shape in b4} == expected  # type: ignore[attr-defined]
    return len(b4)


def main() -> int:
    base = _classification_witnesses()[ExistingInstallClass.CLEAN_FAST_FORWARD_SEED_CLONE]
    space = _space(base)
    assert len(space) == _LANDED_SHAPES, len(space)
    landed, once, zero, multi = _assert_landed_baseline(space)
    current_rows = tuple((predicate, result.installation_class.value) for predicate, result in classification._ROWS)  # noqa: SLF001
    current = _run(current_rows, space)
    _assert_criteria(current, once, zero, multi)
    moves = _assert_moves(landed, current, once)
    b4 = _assert_b4(space, once)
    # The real-clone shapes classify as local_changes_present, and the enum no longer carries the retired name.
    assert "DISJOINT_LOCAL_TRACKED_CHANGES" not in ExistingInstallClass.__members__
    print(f"existing_install_classification_coverage_smoke OK: {len(space)} shapes, {len(once)} classified once, {len(zero)} zero, {len(multi)} multi, {sum(moves.values()) - 3} moved (+3 renamed), B4 regressions unwidened={b4}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
