"""Closed, candidate-aware safety reduction for Step-4 source updates.

Two layers live here.  ``analyze_update_topology`` reduces fresh Step-2 facts
to the Step 7 local-state frontier (design section 6.2): unstaged, disjoint,
shape-preserving tracked modifications outside every executed-code root
(Class T, A7.5) and untracked non-ignored paths that collide with nothing in
the exact candidate tree (Class U, A7.2) are admitted; staged changes, shape
changes, git-metadata files, executed-code paths and every collision class
block with their paths.  The pure probe parsers, ``collision_rows`` and
``tracked_overlap`` below are fed by Git output that ``update_execution``
gathers with hardened argument vectors; they exist because Step 2's
candidate-dependent collision fields are empty placeholders, and a Git
fast-forward silently overwrites an *ignored* local file at a candidate-added
path.
"""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass

from .existing_install_inspection import ExistingInstallFacts, ObservationAvailability, ObservedBoolean, RawRow
from .models import JsonValue

# The executable-config scan and its parser live in ``target_git``, the one hardened target Git surface.
from .target_git import parse_git_config_entries as parse_git_config_entries
from .target_git import unsafe_config_keys as unsafe_config_keys

_TRANSITION_STATUSES = frozenset({"A", "D", "M", "T"})
_CREATED_STATUSES = frozenset({"A", "T"})
#: Class T (Step 7 section 6.2): content-only modifications of a regular file whose mode is unchanged.
_REGULAR_MODES = frozenset({"100644", "100755"})
#: Git metadata that changes how a fast-forward writes candidate paths (attributes: eol/text/filters;
#: modules: gitlinks) blocks at any depth, tracked or untracked; a *tracked-modified* root ``.gitignore``
#: blocks too, while the untracked root ``.gitignore`` every genesis writes is admitted (section 6.2).
_GIT_METADATA_BASENAMES = frozenset({".gitattributes", ".gitmodules"})
_ROOT_GITIGNORE = ".gitignore"
#: The seed's ``never_copy`` surface: no sealed release may carry a path under it, and the running
#: solet owns it (section 6.1, U-config).
PRESERVED_SURFACE_PREFIX = "profile/"


@dataclass(frozen=True, slots=True)
class LocalState:
    """The Manager-side local-state observation (Step 7 section 6.3).

    ``preserved_tracked_paths`` are ``(path, sha256, size)`` per tracked
    modification; ``committed_inventory`` is ``(path, kind, mode, size)`` per
    untracked entry outside ``profile/``; ``local_state_commitment`` is the
    aggregate over that inventory *with* its per-entry digests (which are never
    rendered or journaled); ``preserved_surface`` is ``(path, kind, mode, size)``
    per untracked entry under ``profile/`` (inventoried, never committed).
    """

    preserved_tracked_paths: tuple[tuple[str, str, int], ...]
    committed_inventory: tuple[tuple[str, str, str, int], ...]
    local_state_commitment: str | None
    preserved_surface: tuple[tuple[str, str, str, int], ...]

    def snapshot(self) -> dict[str, JsonValue]:
        """The closed journal projection (per-entry committed digests excluded, Step 4 section 5.3)."""
        return {
            "preserved_tracked_paths": [{"path": path, "sha256": digest, "size": size} for path, digest, size in self.preserved_tracked_paths],
            "committed_inventory": self.committed_rows(),
            "local_state_commitment": self.local_state_commitment,
            "preserved_surface": [{"path": path, "kind": kind, "mode": mode, "size": size} for path, kind, mode, size in self.preserved_surface],
        }

    def preimage(self) -> dict[str, JsonValue]:
        """What the source approval binds (section 6.3): the tracked digests and the aggregate commitment only.

        The preserved surface is deliberately outside the preimage: a service
        write between preview and apply must not invalidate an approval it
        cannot affect.
        """
        return {
            "preserved_tracked_paths": [{"path": path, "sha256": digest, "size": size} for path, digest, size in self.preserved_tracked_paths],
            "local_state_commitment": self.local_state_commitment,
        }

    def committed_rows(self) -> list[JsonValue]:
        return [{"path": path, "kind": kind, "mode": mode, "size": size} for path, kind, mode, size in self.committed_inventory]


@dataclass(frozen=True, slots=True)
class UpdateTopology:
    """The only collision vocabulary permitted to reach an update preview."""

    reasons: tuple[str, ...]
    actionable: bool
    local_state: LocalState | None = None


@dataclass(frozen=True, slots=True)
class CollisionRow:
    """One stable reason with the exact non-secret candidate path it names."""

    reason: str
    path: str


@dataclass(frozen=True, slots=True)
class OverlapRow:
    """One locally modified tracked path a candidate transition row touches (section 6.3, B3b)."""

    reason: str
    path: str
    candidate_path: str
    status: str


def analyze_update_topology(facts: ExistingInstallFacts, *, baseline_commit: str, candidate_commit: str, source_mode: str = "advance") -> UpdateTopology:
    """Reduce fresh probes to the Step 7 local-state frontier (candidate-free half).

    The reasons a bare inspection can decide -- ``local_state_unobserved``,
    ``staged_changes_present``, ``tracked_shape_changed``, ``git_metadata_present``
    -- are produced here; the candidate-dependent reasons (tracked overlap,
    executed-code roots, the preserved-surface transition rule, collisions) are
    added by ``local_state_reasons`` once the baseline is proved an ancestor of
    the candidate.  ``source_mode="verify"`` (Step 6 section 4.8) is the
    zero-delta path: the ``already_current`` reason is still recorded but no
    longer blocks, while every A7 refusal keeps blocking in both modes.
    """
    reasons: list[str] = []
    if facts.head_commit != baseline_commit or facts.head_tree is None:
        reasons.append("managed_identity_drift")
    if candidate_commit == baseline_commit:
        reasons.append("already_current")
    reasons.extend(local_state_fact_reasons(facts))
    if facts.detached is ObservedBoolean.TRUE:
        reasons.append("detached_head")
    if facts.shallow is ObservedBoolean.TRUE:
        reasons.append("shallow_history")
    for paths, reason in ((facts.submodules, "submodule_present"), (facts.linked_worktrees, "linked_worktree_present"), (facts.repository_operations, "repository_operation_present"), (facts.untracked_destination_collisions, "untracked_destination_collision"), (facts.casefold_collisions, "casefold_collision")):
        if paths.values:
            reasons.append(reason)
    return UpdateTopology(tuple(sorted(set(reasons))), actionable_reasons(reasons, source_mode))


def local_state_fact_reasons(facts: ExistingInstallFacts) -> tuple[str, ...]:
    """Section 6.2's four candidate-free reasons, from the Step-2 facts alone."""
    reasons: list[str] = []
    observed = ObservationAvailability.OBSERVED
    if facts.tracked_paths.availability is not observed or facts.staged_paths.availability is not observed or facts.tracked_entries.availability is not observed:
        reasons.append("local_state_unobserved")
    if facts.staged_paths.values:
        reasons.append("staged_changes_present")
    if shape_changed_rows(facts.tracked_entries.values):
        reasons.append("tracked_shape_changed")
    if git_metadata_paths(facts.tracked_paths.values, facts.untracked_paths.values):
        reasons.append("git_metadata_present")
    return tuple(reasons)


def local_state_reasons(
    facts: ExistingInstallFacts,
    transition: tuple[tuple[str, str], ...],
    executed_code_roots: tuple[str, ...],
    *,
    case_insensitive: bool,
    installer_pins: tuple[str, ...],
    hydration_blocks: frozenset[str] = frozenset(),
) -> tuple[tuple[str, tuple[str, ...]], ...]:
    """Section 6.2's candidate-dependent reasons, each with the exact paths it names.

    ``installer_pins`` are the tracked modifications ``installer_pins`` proved
    byte-exact installer writes (iss_f1d8cfc2): they are Class T, never
    ``executed_code_modified``, but still overlap a candidate that changes them.
    ``hydration_blocks`` are the files ``update_hydration_carry`` proved to differ
    only by the create's hydration block (iss_f89ab692): the candidate's own row
    for such a file is the carry's to answer, never ``tracked_overlap_present``.
    """
    rows: list[tuple[str, tuple[str, ...]]] = []
    overlap = _uncarried_overlap(tracked_overlap(facts.tracked_paths.values, transition, case_insensitive=case_insensitive), hydration_blocks)
    for reason in ("tracked_overlap_present", "casefold_collision"):
        paths = tuple(sorted({row.path for row in overlap if row.reason == reason}))
        if paths:
            rows.append((reason, paths))
    local = tuple(path for path in (*facts.tracked_paths.values, *facts.untracked_paths.values) if path not in installer_pins)
    executed = executed_code_overlap(local, executed_code_roots)
    if executed:
        rows.append(("executed_code_modified", executed))
    surface = preserved_surface_transition(transition)
    if surface:
        rows.append(("preserved_surface_in_transition", surface))
    return tuple(rows)


def _uncarried_overlap(rows: tuple[OverlapRow, ...], hydration_blocks: frozenset[str]) -> tuple[OverlapRow, ...]:
    """Every overlap row but the candidate's own row for a file whose only local change is the hydration block (iss_f89ab692)."""
    return tuple(row for row in rows if not (row.path in hydration_blocks and row.candidate_path == row.path))


def shape_changed_rows(rows: tuple[RawRow, ...]) -> tuple[RawRow, ...]:
    """Every tracked entry that is not a content-only ``M`` of a regular file with an unchanged mode (section 6.2)."""
    return tuple(row for row in rows if row.status != "M" or row.old_mode != row.new_mode or row.new_mode not in _REGULAR_MODES)


def git_metadata_paths(tracked: tuple[str, ...], untracked: tuple[str, ...]) -> tuple[str, ...]:
    """The git-metadata paths section 6.2 refuses: attributes/modules at any depth, a tracked-modified root ``.gitignore``."""
    found = {path for path in tracked if path.rsplit("/", 1)[-1] in _GIT_METADATA_BASENAMES or path == _ROOT_GITIGNORE}
    found.update(path for path in untracked if path.rsplit("/", 1)[-1] in _GIT_METADATA_BASENAMES)
    return tuple(sorted(found))


def executed_code_overlap(paths: tuple[str, ...], roots: tuple[str, ...]) -> tuple[str, ...]:
    """Every local path (tracked or untracked) under an executed-code root (section 6.5)."""
    return tuple(sorted({path for path in paths if any(path == root or (root.endswith("/") and path.startswith(root)) for root in roots)}))


def preserved_surface_transition(transition: tuple[tuple[str, str], ...]) -> tuple[str, ...]:
    """Every candidate transition path under ``profile/`` (section 6.3, ``preserved_surface_in_transition``)."""
    return tuple(sorted({path for _, path in transition if path.startswith(PRESERVED_SURFACE_PREFIX)}))


def is_preserved_surface(path: str) -> bool:
    return path.startswith(PRESERVED_SURFACE_PREFIX)


def actionable_reasons(reasons: list[str] | tuple[str, ...], source_mode: str) -> bool:
    """Mode-aware actionability: ``verify`` tolerates exactly ``already_current`` and nothing else."""
    if source_mode not in {"advance", "verify"}:
        raise ValueError(f"source_mode must be advance or verify, got {source_mode!r}")
    blocking = set(reasons) - ({"already_current"} if source_mode == "verify" else set())
    return not blocking


def analyze_update_inspection(inspection: object, *, baseline_commit: str, candidate_commit: str) -> UpdateTopology:
    """Bind the reducer to a freshly-produced Step-2 inspection result."""
    facts = getattr(inspection, "facts", None)
    if not isinstance(facts, ExistingInstallFacts):
        raise ValueError("update topology requires a fresh existing-install inspection")
    return analyze_update_topology(facts, baseline_commit=baseline_commit, candidate_commit=candidate_commit)


def config_flag(entries: tuple[tuple[str, str, str], ...], key: str) -> bool:
    """Return the repository-scoped boolean for ``key`` (false when absent)."""
    values = [value for scope, entry_key, value in entries if scope in {"local", "worktree"} and entry_key.lower() == key]
    return bool(values) and values[-1].strip().lower() in {"true", "yes", "on", "1"}


def parse_transition_paths(raw: bytes) -> tuple[tuple[str, str], ...]:
    """Parse ``git diff-tree -r -z --no-renames --name-status`` into (status, path) rows."""
    tokens = raw.split(b"\0")
    if tokens[-1] != b"":
        raise ValueError("diff-tree listing is not NUL-terminated")
    body = tokens[:-1]
    if len(body) % 2:
        raise ValueError("diff-tree listing has an odd token count")
    rows: list[tuple[str, str]] = []
    for status, path in zip(body[::2], body[1::2], strict=True):
        text = status.decode("utf-8", "strict")
        if text not in _TRANSITION_STATUSES or not path:
            raise ValueError("diff-tree listing has an unsupported entry")
        rows.append((text, path.decode("utf-8", "surrogateescape")))
    return tuple(rows)


def parse_local_entries(raw: bytes) -> tuple[tuple[str, str, str], ...]:
    """Parse ``git status --porcelain=v1 -z`` into (kind, xy, path) rows.

    ``kind`` is ``untracked``, ``ignored``, or ``tracked``; ``xy`` is the
    two-letter status code the record carries (Step 7 keeps it: the shape rule
    reads the raw-diff modes, but the code is on the record already parsed).
    A rename entry's second path is consumed with its record so token
    alignment never drifts.
    """
    tokens = raw.split(b"\0")
    if tokens[-1] != b"":
        raise ValueError("status listing is not NUL-terminated")
    rows: list[tuple[str, str, str]] = []
    pending = iter(tokens[:-1])
    for token in pending:
        if len(token) < 4 or token[2:3] != b" ":
            raise ValueError("status listing has a malformed record")
        code, path = token[:2], token[3:].decode("utf-8", "surrogateescape")
        if code[:1] in b"RC":
            next(pending, None)
        rows.append(({b"??": "untracked", b"!!": "ignored"}.get(code, "tracked"), code.decode("ascii", "replace"), path))
    return tuple(rows)


def parse_raw_diff(raw: bytes) -> tuple[RawRow, ...]:
    """Parse ``git diff --raw -z --no-renames HEAD`` into :class:`RawRow` records.

    The wire shape is ``:<old_mode> <new_mode> <old_id> <new_id> <status> NUL <path> NUL``;
    an unmerged (``::``) record, a missing path, or any other malformation fails loud.
    """
    tokens = raw.split(b"\0")
    if tokens[-1] != b"":
        raise ValueError("raw diff listing is not NUL-terminated")
    body = tokens[:-1]
    if len(body) % 2:
        raise ValueError("raw diff listing has an odd token count")
    rows: list[RawRow] = []
    for header, path in zip(body[::2], body[1::2], strict=True):
        text = header.decode("utf-8", "strict")
        if not text.startswith(":") or text.startswith("::"):
            raise ValueError("raw diff listing has an unsupported record")
        fields = text[1:].split(" ")
        if len(fields) != 5 or not path or not all(fields):
            raise ValueError("raw diff listing has a malformed record")
        old_mode, new_mode, old_id, new_id, status = fields
        if status not in {"A", "D", "M", "T"}:
            raise ValueError("raw diff listing has an unsupported status")
        rows.append(RawRow(old_mode, new_mode, old_id, new_id, status, path.decode("utf-8", "surrogateescape")))
    return tuple(rows)


def collision_rows(
    transition: tuple[tuple[str, str], ...],
    local_entries: tuple[tuple[str, str, str], ...],
    *,
    case_insensitive: bool,
) -> tuple[CollisionRow, ...]:
    """Intersect candidate-created paths with local untracked/ignored paths.

    A collapsed local directory (``dir/``) is treated as occupying every
    descendant: the conservative reading, because its contents were not
    enumerated.  Local symlinks are plain entries here and collide like files.
    """
    created = tuple(path for status, path in transition if status in _CREATED_STATUSES)
    local = tuple(path for kind, _, path in local_entries if kind in {"untracked", "ignored"})
    rows: set[CollisionRow] = set()
    for path in created:
        for local_path in local:
            reason = _collision_reason(path, local_path)
            if reason is not None:
                rows.add(CollisionRow(reason, path))
            elif case_insensitive and _collision_reason(_fold(path), _fold(local_path)) is not None:
                rows.add(CollisionRow("casefold_collision", path))
    return tuple(sorted(rows, key=lambda row: (row.reason, row.path)))


def tracked_overlap(tracked: tuple[str, ...], transition: tuple[tuple[str, str], ...], *, case_insensitive: bool) -> tuple[OverlapRow, ...]:
    """Intersect locally modified tracked paths with EVERY transition status (section 6.3, B3b).

    Exact equality is the case git itself refuses ("would be overwritten"); the
    two prefix relations cover a candidate that writes under, or retypes or
    deletes an ancestor of, a modified path.  A candidate deletion of a modified
    path is the case git refuses loudest and a candidate modification is CH-31,
    so every status participates, not only the created ones.
    """
    rows: set[OverlapRow] = set()
    for local_path in tracked:
        for status, candidate_path in transition:
            if status not in _TRANSITION_STATUSES:
                continue
            if _overlaps(candidate_path, local_path):
                rows.add(OverlapRow("tracked_overlap_present", local_path, candidate_path, status))
            elif case_insensitive and _overlaps(_fold(candidate_path), _fold(local_path)):
                rows.add(OverlapRow("casefold_collision", local_path, candidate_path, status))
    return tuple(sorted(rows, key=lambda row: (row.reason, row.path, row.candidate_path, row.status)))


def _overlaps(candidate: str, local: str) -> bool:
    return candidate == local or candidate.startswith(local + "/") or local.startswith(candidate + "/")


def _collision_reason(candidate: str, local: str) -> str | None:
    local_is_dir = local.endswith("/")
    stem = local.rstrip("/")
    if stem == candidate:
        return "untracked_destination_collision"
    if candidate.startswith(stem + "/"):
        return "untracked_destination_collision" if local_is_dir else "file_directory_prefix_collision"
    if stem.startswith(candidate + "/"):
        return "file_directory_prefix_collision"
    return None


def _fold(path: str) -> str:
    return unicodedata.normalize("NFC", path).casefold()


def origin_reason(origins: tuple[str, ...], canonical_repository: str) -> str | None:
    """Return ``origin_unapproved`` unless ``origin`` is exactly the canonical URL.

    The reviewed historical-origin migration (``target.repoint_origin``) is not
    executed by this increment, so a declared migration source blocks too.
    """
    return None if origins == (canonical_repository,) else "origin_unapproved"
