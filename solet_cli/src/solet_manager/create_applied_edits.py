"""The tracked-file edits a create's own apply operations made, recorded by digest at apply time (iss_9cd4359a).

A fresh ``solet create`` edits five tracked files on purpose: Genesis writes the hydration block into
``AGENTS.md`` and ``CLAUDE.md`` and the instance name into ``root_manifest.yaml``, and the coding-agent
stage pins both coordination-hook manifests to the target venv.  The seed-tree baseline counts every
tracked difference, so every healthy created solet reported ``tracked_tree_deviation`` and
``working_tree_dirty`` for the Manager's own writes.

This module is the simplest mechanism that accepts exactly those writes and nothing else.  Around each
approved apply operation the executor measures the tracked worktree modifications before and after; a
path whose bytes the operation changed is recorded with the sha256 of the post-edit file and the
operation that wrote it, in a Manager-private ledger bound to the create transaction.  The doctor then
accepts a tracked modification only while the file's current bytes equal that recorded digest AND its
Git mode still equals ``HEAD``'s: none of the create's writes changes a mode, so a ``chmod`` before or
after the apply is never absorbed (review of r61).  A path list is never trusted on its own: a later
user edit to a recorded file changes its digest and warns again, and a change to any path no apply
operation wrote was never recorded and warns as before.

What it deliberately does not do: it is not ``managed-tree-v1`` (no overlay receipts, no reconciliation
chain), it records nothing when the checkout cannot be measured (before source acquisition, or when Git
refuses the target), and an apply interrupted between the adapter's write and the ledger write leaves
that edit unrecorded.  Each of those gaps shows up as the doctor's ordinary warning, never as a silent
accept.
"""

from __future__ import annotations

import hashlib
import stat
from dataclasses import dataclass
from pathlib import Path

from .errors import StateError
from .models import JsonValue
from .paths import ManagerPaths
from .private_json import load_json_object
from .seed_tree_verifier import SeedTreeDeviation, SeedTreeVerification, query_target_git, verify_seed_tree
from .state_io import atomic_write_json

__all__ = [
    "AppliedEdit",
    "AppliedEditLedger",
    "accept_applied_edits",
    "accepted_edit_paths",
    "applied_edits_path",
    "load_applied_edits",
    "record_applied_edits",
    "tracked_edit_digests",
]

_SCHEMA = "create-applied-edits-v1"
_LEDGER_KEYS = frozenset({"schema", "create_operation_id", "target", "edits"})
_EDIT_KEYS = frozenset({"path", "sha256", "operation_id"})

#: Worktree path -> ``sha256:<hex>`` of its current bytes, for every tracked content modification.
type EditDigests = dict[str, str]


@dataclass(frozen=True)
class AppliedEdit:
    """One tracked file an apply operation left at these exact bytes."""

    path: str
    sha256: str
    operation_id: str

    def to_dict(self) -> dict[str, JsonValue]:
        return {"path": self.path, "sha256": self.sha256, "operation_id": self.operation_id}


@dataclass(frozen=True)
class AppliedEditLedger:
    """Every recorded edit of one create transaction, one row per path, ordered by path."""

    create_operation_id: str
    target: str
    edits: tuple[AppliedEdit, ...]

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "schema": _SCHEMA,
            "create_operation_id": self.create_operation_id,
            "target": self.target,
            "edits": [item.to_dict() for item in self.edits],
        }


def applied_edits_path(paths: ManagerPaths, name: str) -> Path:
    """Manager-private, beside the create transaction it is bound to."""
    return paths.state_dir / "create-applied-edits" / f"{name}.json"


def tracked_edit_digests(target: Path, seed_tree_hash: str) -> EditDigests | None:
    """Digest every content-only tracked worktree modification, or ``None`` when the checkout cannot be measured."""
    verification = verify_seed_tree(target, seed_tree_hash)
    if verification.query_error is not None:
        return None
    digests: EditDigests = {}
    for deviation in verification.deviations:
        if _content_modification(deviation):
            digest = _regular_file_sha256(target / deviation.paths[0])
            if digest is not None:
                digests[deviation.paths[0]] = digest
    return digests


def record_applied_edits(
    paths: ManagerPaths,
    *,
    name: str,
    target: str,
    create_operation_id: str,
    operation_id: str,
    before: EditDigests | None,
    after: EditDigests | None,
) -> None:
    """Record the paths one applied operation changed; a new create transaction starts a fresh ledger."""
    if before is None or after is None:
        return
    changed = {path: digest for path, digest in after.items() if before.get(path) != digest}
    if not changed:
        return
    ledger_path = applied_edits_path(paths, name)
    rows = _bound_rows(load_applied_edits(ledger_path), create_operation_id, target)
    rows.update({path: AppliedEdit(path, digest, operation_id) for path, digest in changed.items()})
    ledger = AppliedEditLedger(create_operation_id, target, tuple(rows[path] for path in sorted(rows)))
    atomic_write_json(ledger_path, ledger.to_dict())


def _bound_rows(ledger: AppliedEditLedger | None, create_operation_id: str, target: str) -> dict[str, AppliedEdit]:
    """The rows to keep: this create's own, never a previous create's under the same name."""
    if ledger is None or ledger.create_operation_id != create_operation_id or ledger.target != target:
        return {}
    return {item.path: item for item in ledger.edits}


def load_applied_edits(path: Path) -> AppliedEditLedger | None:
    raw = load_json_object(path, missing_ok=True)
    if raw is None:
        return None
    if frozenset(raw) != _LEDGER_KEYS or raw["schema"] != _SCHEMA:
        raise StateError(f"create applied-edits ledger does not match the closed v1 schema: {path}")
    operation, target, edits = raw["create_operation_id"], raw["target"], raw["edits"]
    if not isinstance(operation, str) or not isinstance(target, str) or not isinstance(edits, list):
        raise StateError(f"create applied-edits ledger fields are invalid: {path}")
    return AppliedEditLedger(operation, target, tuple(_edit(item, path) for item in edits))


def accepted_edit_paths(paths: ManagerPaths, *, name: str, target: Path, create_operation_id: str) -> frozenset[str]:
    """The recorded paths whose current bytes and Git mode are still exactly what the create's apply left there."""
    ledger = load_applied_edits(applied_edits_path(paths, name))
    if ledger is None or ledger.create_operation_id != create_operation_id or Path(ledger.target) != target:
        return frozenset()
    matching = [item.path for item in ledger.edits if _regular_file_sha256(target / item.path) == item.sha256]
    head_modes = _head_modes(target, matching)
    return frozenset(path for path in matching if head_modes.get(path) == _worktree_mode(target / path))


def accept_applied_edits(verification: SeedTreeVerification, accepted: frozenset[str]) -> SeedTreeVerification:
    """Move each accepted content-only worktree modification out of the deviations; everything else stays."""
    kept = tuple(item for item in verification.deviations if not (_content_modification(item) and item.paths[0] in accepted))
    moved = tuple(item for item in verification.deviations if item not in kept)
    return SeedTreeVerification(
        expected_tree_hash=verification.expected_tree_hash,
        observed_head_tree_hash=verification.observed_head_tree_hash,
        deviations=kept,
        query_error=verification.query_error,
        accepted_manager_edits=moved,
    )


def _head_modes(target: Path, paths: list[str]) -> dict[str, str]:
    """``HEAD``'s Git mode per path; an unanswerable query accepts nothing, so the doctor's warning stands."""
    if not paths:
        return {}
    listed = query_target_git(target, "ls-tree", "-z", "HEAD", "--", *paths)
    if isinstance(listed, str):
        return {}
    modes: dict[str, str] = {}
    for entry in filter(None, listed.stdout.split("\0")):
        header, _tab, path = entry.partition("\t")
        modes[path] = header.split(" ", 1)[0]
    return modes


def _worktree_mode(path: Path) -> str:
    """Git's regular-file mode for the worktree bytes: the owner execute bit is all it records."""
    return "100755" if path.lstat().st_mode & stat.S_IXUSR else "100644"


def _content_modification(deviation: SeedTreeDeviation) -> bool:
    return deviation.location == "worktree" and deviation.status == "M" and len(deviation.paths) == 1


def _regular_file_sha256(path: Path) -> str | None:
    try:
        info = path.lstat()
    except FileNotFoundError:
        return None
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        return None
    return f"sha256:{hashlib.sha256(path.read_bytes()).hexdigest()}"


def _edit(raw: JsonValue, path: Path) -> AppliedEdit:
    if not isinstance(raw, dict) or frozenset(raw) != _EDIT_KEYS:
        raise StateError(f"create applied-edits row does not match the closed v1 schema: {path}")
    edit_path, digest, operation = raw["path"], raw["sha256"], raw["operation_id"]
    if not isinstance(edit_path, str) or not isinstance(digest, str) or not isinstance(operation, str):
        raise StateError(f"create applied-edits row fields are invalid: {path}")
    return AppliedEdit(edit_path, digest, operation)
