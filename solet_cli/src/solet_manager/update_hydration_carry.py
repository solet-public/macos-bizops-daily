"""Carrying the create's own hydration block across a source update (iss_f89ab692).

``solet create`` merges the solet hydration block into the seed's root ``AGENTS.md`` and ``CLAUDE.md``.
A release that changes either file overlapped that edit, and ``tracked_overlap_present`` refused every
created solet's update (the r61 stable update round, ``iev_0ff7999636c247089bb1eef708376d5c``).

The block is recognised by content, never by path alone.  A candidate transition row that modifies one of
the two files is carriable when the raw-diff row is a content-only edit of a regular file, the local bytes are
exactly the committed file plus one delimited block (``is_hydrated``), and one of two bases attests the block:

* ``create_ledger`` -- the create-applied-edits ledger still accepts the file (its bytes and mode are what
  the create's apply left there, ``accepted_edit_paths``), for a solet created by an r61-or-later Manager;
* ``genesis_render`` -- the local bytes equal Genesis's own merge of the committed file with the committed
  template rendered for this instance, for a solet created before that ledger existed.

Anything else -- a user line inside or outside the block, a changed mode, any other path -- keeps
``tracked_overlap_present``.  A recognised block whose candidate cannot hold it (the file is deleted or
retyped, or its text leaves the block no anchor) is ``hydration_block_uncarriable``, never a silent drop.

The carry is one planned source action, ``target.carry_hydration_block``, whose rows the approval
fingerprint binds.  Around the fast-forward it is crash-safe: each file's exact bytes are backed up under
the operation (``managed_artifact_backup``) before the committed bytes are put back for Git; after the
fast-forward the block is merged onto the candidate's bytes from that backup.  A crash before the
fast-forward is undone on resume or abandon (``put_back``); a crash after it is completed
on resume (``carry_into_journal``).  The ledger row follows the carried bytes, so the doctor still
accepts the file.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import cast

from solet_setup_contracts.agent_instruction_block import (
    AGENT_INSTRUCTION_FILES,
    agent_template_path,
    carry_agent_block,
    is_hydrated,
    merge_agent_block,
    render_agent_instructions,
)

from .create_applied_edits import accepted_edit_paths, rebind_applied_edit
from .errors import RestoreTargetDivergedError, SourceError, StateError, UpdateBlockedError
from .existing_install_inspection import ExistingInstallFacts, RawRow
from .installer_pins import BlobReader, read_target_blob, worktree_bytes
from .managed_artifact_backup import backup_exists, backup_root, file_sha256, read_backup, read_backup_bytes, restore_artifact, write_backup
from .models import InstanceInventoryRecordV2, JsonValue
from .paths import ManagerPaths
from .state_io import atomic_replace_bytes
from .transaction import load_transaction
from .update_journal import record_local_state_revision, write_update_journal

__all__ = [
    "CARRY_ACTION",
    "UNCARRIABLE_REASON",
    "CarryPlan",
    "HydrationCarry",
    "carry_into_journal",
    "plan_hydration_carry",
    "put_back",
    "set_aside",
    "uncarriable_repair",
    "with_carry",
]

CARRY_ACTION = "target.carry_hydration_block"
UNCARRIABLE_REASON = "hydration_block_uncarriable"
#: The one ``NON_TOUCH_SURFACES`` member a planned carry touches: it rewrites the tracked edits it names.
_TOUCHED_SURFACE = "tracked_local_modifications"
_REGULAR_MODES = frozenset({"100644", "100755"})
_UNCARRIABLE_REPAIR = (
    "The candidate release changes {paths} so that this solet's hydration block (the section between "
    "`<!-- BEGIN SOLET HYDRATION -->` and `<!-- END SOLET HYDRATION -->`) cannot be merged back onto it; the Manager "
    "never drops the block or overwrites the file. Recover by hand, in the solet: save the block (`git diff -- {paths} > "
    "~/solet-hydration.patch`), restore the shipped bytes (`git checkout -- {paths}`), run `solet-manager update {name} "
    "--dry-run` and approve it, then copy the saved block back to the end of each file."
)

type Moved = dict[str, tuple[str, str, int]]


@dataclass(frozen=True, slots=True)
class HydrationCarry:
    """One file whose hydration block the update carries: its bare sha256 before, and the carried bytes' digest and size."""

    path: str
    basis: str
    before_sha256: str
    after_sha256: str
    after_size: int

    def to_dict(self) -> dict[str, JsonValue]:
        return {"path": self.path, "basis": self.basis, "before_sha256": self.before_sha256, "after_sha256": self.after_sha256, "after_size": self.after_size}


@dataclass(frozen=True, slots=True)
class CarryPlan:
    """What the preview proved: the carriable files, and the recognised blocks the candidate cannot hold."""

    carries: tuple[HydrationCarry, ...] = ()
    uncarriable: tuple[str, ...] = ()

    @property
    def paths(self) -> frozenset[str]:
        return frozenset((*(item.path for item in self.carries), *self.uncarriable))

    def rows(self) -> list[JsonValue]:
        return [item.to_dict() for item in self.carries]

    def bound(self, surfaces: tuple[str, ...]) -> dict[str, JsonValue]:
        """The approval-bound disclosure: nothing without a carry, so every other fingerprint is unchanged."""
        if not self.carries:
            return {}
        return {"hydration_carry": self.rows(), "non_touch_surfaces": list(self.untouched(surfaces))}

    def untouched(self, surfaces: tuple[str, ...]) -> tuple[str, ...]:
        """The non-touch surfaces this plan still honours: a carry rewrites the tracked local modifications it names."""
        return tuple(item for item in surfaces if not (self.carries and item == _TOUCHED_SURFACE))


@dataclass(frozen=True, slots=True)
class _Inputs:
    """The shared inputs of one preview's recognition, read once."""

    name: str
    target: Path
    head: str
    ledger: frozenset[str]
    read_target: BlobReader
    read_candidate: BlobReader
    candidate_commit: str


def with_carry(planned: tuple[str, ...], hydration: CarryPlan) -> tuple[str, ...]:
    """The source actions, with the carry appended when the plan carries a block, so the approval binds it."""
    return (*planned, CARRY_ACTION) if hydration.carries else planned


def plan_hydration_carry(
    paths: ManagerPaths,
    *,
    name: str,
    target: Path,
    facts: ExistingInstallFacts,
    transition: tuple[tuple[str, str], ...],
    read_target: BlobReader,
    read_candidate: BlobReader,
    candidate_commit: str,
) -> CarryPlan:
    """Every overlapped ``AGENTS.md``/``CLAUDE.md`` whose only local change is the create's hydration block."""
    if facts.head_commit is None:
        return CarryPlan()
    inputs = _Inputs(name, target, facts.head_commit, _ledger_accepted(paths, name, target), read_target, read_candidate, candidate_commit)
    planned = [_plan_one(path, status, inputs) for path, status in _overlapped_edits(facts, transition)]
    return CarryPlan(tuple(item for item in planned if isinstance(item, HydrationCarry)), tuple(item for item in planned if isinstance(item, str)))


def _overlapped_edits(facts: ExistingInstallFacts, transition: tuple[tuple[str, str], ...]) -> list[tuple[str, str]]:
    """``(path, candidate status)`` for each of the two files with a content-only local edit the candidate's own row touches."""
    modified = frozenset(facts.tracked_paths.values)
    rows = {row.path: row for row in facts.tracked_entries.values}
    statuses = {path: status for status, path in transition}
    return [(path, statuses[path]) for path in sorted(AGENT_INSTRUCTION_FILES) if path in modified and path in statuses and _content_only(rows.get(path))]


def _plan_one(path: str, status: str, inputs: _Inputs) -> HydrationCarry | str | None:
    """A carry, the path when its recognised block is uncarriable, or ``None`` when the change is not the block."""
    local_raw = worktree_bytes(inputs.target, path)
    local, committed = _text(local_raw), _text(inputs.read_target(f"{inputs.head}:{path}"))
    if local_raw is None or local is None or committed is None or not is_hydrated(committed, local):
        return None
    basis = "create_ledger" if path in inputs.ledger else _render_basis(path, local, committed, inputs)
    if basis is None:
        return None
    candidate = _text(inputs.read_candidate(f"{inputs.candidate_commit}:{path}")) if status == "M" else None
    carried = None if candidate is None else carry_agent_block(local, candidate)
    if carried is None:
        return path
    raw = carried.encode("utf-8")
    return HydrationCarry(path, basis, _sha256(local_raw), _sha256(raw), len(raw))


def uncarriable_repair(paths: list[str], name: str) -> str:
    return _UNCARRIABLE_REPAIR.format(paths=" ".join(paths), name=name)


def set_aside(paths: ManagerPaths, record: InstanceInventoryRecordV2, operation_id: str, carries: tuple[HydrationCarry, ...]) -> None:
    """Before the fast-forward: back each carried file up byte-exact, then put its committed bytes back for Git."""
    target = Path(record.target.canonical_path)
    read_target = read_target_blob(target)
    for carry in carries:
        destination = target / carry.path
        backup = write_backup(paths, record.instance_id, operation_id, _backup_id(carry.path, carry.before_sha256), destination)
        if backup.sha256 != f"sha256:{carry.before_sha256}" or backup.mode is None:
            raise UpdateBlockedError("probe_drift", f"{carry.path} changed after the lock-time revalidation", repair="Inspect the target; do not reset it.")
        atomic_replace_bytes(destination, read_target(f"{record.source_release.commit}:{carry.path}"), mode=backup.mode)


def put_back(paths: ManagerPaths, record: InstanceInventoryRecordV2, journal: dict[str, JsonValue]) -> tuple[str, ...]:
    """At the baseline: every set-aside file still at its committed bytes gets its backed-up bytes back; returns those paths.

    Runs on a refused fast-forward, on resume below the fast-forward and on abandon, so a crash between the
    set-aside and the fast-forward never leaves the block off the file.
    """
    if CARRY_ACTION not in cast(list[str], journal["planned_actions"]):
        return ()
    target, operation_id = Path(record.target.canonical_path), cast(str, journal["operation_id"])
    baseline = cast(str, cast(dict[str, JsonValue], journal["baseline"])["commit"])
    restored: list[str] = []
    for path, digest in sorted(_journal_digests(journal).items()):
        artifact = _backup_id(path, digest)
        if not backup_exists(paths, record.instance_id, operation_id, artifact) or file_sha256(target / path) == read_backup(paths, record.instance_id, operation_id, artifact).sha256:
            continue
        committed = f"sha256:{_sha256(read_target_blob(target)(f'{baseline}:{path}'))}"
        try:
            restore_artifact(paths, record.instance_id, operation_id, artifact, expected_after_sha256=committed)
        except RestoreTargetDivergedError as exc:
            raise _uncarriable(path, backup_root(paths, record.instance_id, operation_id), "holds neither its backed-up bytes nor the committed bytes the update put there") from exc
        restored.append(path)
    return tuple(restored)


def carry_into_journal(paths: ManagerPaths, record: InstanceInventoryRecordV2, journal_path: Path, journal: dict[str, JsonValue], executed: list[str]) -> dict[str, JsonValue]:
    """At the candidate: merge each set-aside block back, move the ledger, and write one carry revision; returns the journal.

    The journal comes back unchanged when the approval planned no carry or its revision is already
    journaled (a resume past it); otherwise ``target.carry_hydration_block`` is appended to ``executed``.
    """
    local_state = cast(dict[str, JsonValue], journal["local_state"])
    revisions = (cast(dict[str, JsonValue], row) for row in cast(list[JsonValue], local_state["revisions"]))
    if CARRY_ACTION not in cast(list[str], journal["planned_actions"]) or any(row.get("operation_id") == CARRY_ACTION for row in revisions):
        return journal
    candidate_commit = cast(str, cast(dict[str, JsonValue], journal["candidate"])["commit"])
    moved = _carry(paths, record, cast(str, journal["operation_id"]), _journal_digests(journal), candidate_commit)
    if not moved:
        raise StateError("the approved hydration carry has no set-aside file to carry")
    current = _carried_snapshot(cast(dict[str, JsonValue], local_state["current"]), moved)
    next_value = record_local_state_revision(journal, revision=_revision(moved), current=current)
    write_update_journal(journal_path, journal, next_value)
    executed.append(CARRY_ACTION)
    return next_value


def _carry(paths: ManagerPaths, record: InstanceInventoryRecordV2, operation_id: str, digests: dict[str, str], candidate_commit: str) -> Moved:
    """Merge each backed-up block onto the candidate's bytes; returns ``path -> (before, after, size)``."""
    target = Path(record.target.canonical_path)
    saved = backup_root(paths, record.instance_id, operation_id)
    moved: Moved = {}
    for path, digest in sorted(digests.items()):
        artifact = _backup_id(path, digest)
        if not backup_exists(paths, record.instance_id, operation_id, artifact):
            continue
        candidate_raw = read_target_blob(target)(f"{candidate_commit}:{path}")
        local, candidate = _text(read_backup_bytes(paths, record.instance_id, operation_id, artifact)), _text(candidate_raw)
        carried = None if local is None or candidate is None else carry_agent_block(local, candidate)
        if carried is None:
            raise _uncarriable(path, saved, "cannot take the block back after the fast-forward")
        raw = carried.encode("utf-8")
        _write_carried(target / path, candidate_raw, raw, saved)
        after = _sha256(raw)
        rebind_applied_edit(paths, name=record.name, path=path, before=f"sha256:{digest}", after=f"sha256:{after}")
        moved[path] = (digest, after, len(raw))
    return moved


def _write_carried(destination: Path, candidate_raw: bytes, raw: bytes, saved: Path) -> None:
    """The fast-forward left the candidate's bytes: write the carried ones; a carried file is already done; anything else stops."""
    current = worktree_bytes(destination.parent, destination.name)
    if current == candidate_raw:
        atomic_replace_bytes(destination, raw, mode=destination.lstat().st_mode & 0o7777)
    elif current != raw:
        raise _uncarriable(destination.name, saved, "changed during the fast-forward")


def _journal_digests(journal: dict[str, JsonValue]) -> dict[str, str]:
    """The journal's current bare digest of each of the two files, which keys its carry backup."""
    current = cast(dict[str, JsonValue], cast(dict[str, JsonValue], journal["local_state"])["current"])
    rows = (cast(dict[str, JsonValue], row) for row in cast(list[JsonValue], current["preserved_tracked_paths"]))
    return {row["path"]: cast(str, row["sha256"]) for row in rows if row["path"] in AGENT_INSTRUCTION_FILES}


def _revision(moved: Moved) -> dict[str, JsonValue]:
    """The journaled ``local_state`` re-baseline the carry records: the carried files, their digests before and after."""
    before: dict[str, JsonValue] = {path: row[0] for path, row in moved.items()}
    after: dict[str, JsonValue] = {path: row[1] for path, row in moved.items()}
    return {"operation_id": CARRY_ACTION, "paths": list[JsonValue](sorted(moved)), "before": before, "after": after}


def _carried_snapshot(current: dict[str, JsonValue], moved: Moved) -> dict[str, JsonValue]:
    """The journal's ``current`` local state with each carried file at its carried digest and size."""
    rows: list[JsonValue] = []
    for raw in cast(list[JsonValue], current["preserved_tracked_paths"]):
        row = cast(dict[str, JsonValue], raw)
        path = cast(str, row["path"])
        rows.append({"path": path, "sha256": moved[path][1], "size": moved[path][2]} if path in moved else row)
    return {**current, "preserved_tracked_paths": rows}


def _ledger_accepted(paths: ManagerPaths, name: str, target: Path) -> frozenset[str]:
    transaction = load_transaction(paths.transaction_path(name))
    if transaction is None:
        return frozenset()
    return accepted_edit_paths(paths, name=name, target=target, create_operation_id=transaction.operation_id)


def _render_basis(path: str, local: str, committed: str, inputs: _Inputs) -> str | None:
    """``genesis_render`` when the local bytes are exactly Genesis's merge of this instance's render of the committed template."""
    try:
        template = _text(inputs.read_target(f"{inputs.head}:{agent_template_path(path)}"))
    except SourceError:
        return None
    if template is None:
        return None
    rendered = render_agent_instructions(template, name=inputs.name, clone_dir=str(inputs.target))
    return "genesis_render" if merge_agent_block(committed, rendered) == local else None


def _uncarriable(path: str, saved: Path, detail: str) -> UpdateBlockedError:
    return UpdateBlockedError(
        UNCARRIABLE_REASON,
        f"{path} {detail}; its bytes from before the update, hydration block included, are saved under {saved}",
        repair=f"Do not reset. Compare {path} with the saved `before.bytes` under {saved}, put the hydration block back by hand, then resume the update.",
    )


def _backup_id(path: str, digest: str) -> str:
    return f"hydration_carry.{path}.{digest[:16]}"


def _content_only(row: RawRow | None) -> bool:
    return row is not None and row.status == "M" and row.old_mode == row.new_mode and row.new_mode in _REGULAR_MODES


def _text(raw: bytes | None) -> str | None:
    if raw is None:
        return None
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return None


def _sha256(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()
