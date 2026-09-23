"""Receipt-authorized managed-tree overlays for reconciled target checkouts.

``managed-tree-v1`` retains the sealed seed as its immutable baseline and
records every approved tracked-tree departure in one target-local ledger.  The
ledger deliberately accepts only target-local M1 terminal receipts: a manager
private receipt cannot prove the bytes visible at the target being verified.
"""

from __future__ import annotations

import hashlib
import re
import stat
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from solet_setup_contracts import canonical_sha256

from .errors import StateConflictError, StateError
from .models import JsonValue
from .seed_tree_verifier import (
    GitQueryRunner,
    SeedTreeDeviation,
    SeedTreeVerification,
    verify_seed_tree,
)
from .state_io import atomic_write_json, ensure_private_directory, load_json_object

__all__ = [
    "ManagedTreeLedger",
    "ManagedTreeStore",
    "ManagedTreeVerification",
    "OverlayEntry",
    "OverlayFile",
    "verify_managed_tree",
]

_SCHEMA = "managed-tree-v1"
_SHA256 = re.compile(r"sha256:[0-9a-f]{64}\Z")
_SEED_TREE_HASH = re.compile(r"[0-9a-f]{40}\Z")
_RECONCILIATION_ID = re.compile(r"rec_[a-zA-Z0-9_-]+\Z")
_LEDGER_KEYS = frozenset({"schema", "baseline_digest", "entries"})
_ENTRY_KEYS = frozenset({"ordinal", "reconciliation_id", "files"})
_FILE_KEYS = frozenset({"path", "before_sha256", "after_sha256"})
_SUCCESSFUL_RECEIPT_STATUSES = frozenset({"reconciled", "already_reconciled"})
_RUNTIME_PREFIXES = (
    ".solet",
    "profile/cache",
    "profile/data",
    "profile/logs",
    "profile/run",
)


def _require_sha256(value: str | None, label: str, *, nullable: bool = False) -> None:
    if value is None and nullable:
        return
    if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
        raise StateConflictError(f"managed tree {label} is not a sha256 identity")


def _require_seed_tree_hash(value: str, label: str) -> None:
    if _SEED_TREE_HASH.fullmatch(value) is None:
        raise StateConflictError(f"managed tree {label} is not a seed_tree_hash")


def _normalize_path(value: str) -> str:
    path = PurePosixPath(value)
    if (
        not value
        or path.is_absolute()
        or any(part in {"", ".", "..", ".git"} for part in path.parts)
        or any(value == prefix or value.startswith(f"{prefix}/") for prefix in _RUNTIME_PREFIXES)
    ):
        raise StateConflictError(f"managed tree overlay path is refused: {value!r}")
    return path.as_posix()


@dataclass(frozen=True)
class OverlayFile:
    """One ordered file replacement, creation, or deletion in an overlay."""

    path: str
    before_sha256: str | None
    after_sha256: str | None

    def __post_init__(self) -> None:
        normalized = _normalize_path(self.path)
        object.__setattr__(self, "path", normalized)
        _require_sha256(self.before_sha256, "overlay before_sha256", nullable=True)
        _require_sha256(self.after_sha256, "overlay after_sha256", nullable=True)
        if self.before_sha256 is None and self.after_sha256 is None:
            raise StateConflictError("managed tree overlay file cannot be absent before and after")

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "path": self.path,
            "before_sha256": self.before_sha256,
            "after_sha256": self.after_sha256,
        }

    @classmethod
    def from_dict(cls, raw: JsonValue) -> OverlayFile:
        if not isinstance(raw, dict) or frozenset(raw) != _FILE_KEYS:
            raise StateError("managed tree overlay file does not match the closed v1 schema")
        path = raw.get("path")
        before = raw.get("before_sha256")
        after = raw.get("after_sha256")
        if not isinstance(path, str) or before is not None and not isinstance(before, str) or after is not None and not isinstance(after, str):
            raise StateError("managed tree overlay file fields are invalid")
        try:
            return cls(path, before, after)
        except StateConflictError as exc:
            raise StateError(str(exc)) from exc


@dataclass(frozen=True)
class OverlayEntry:
    """One append-only target-receipt-authorized tracked-tree delta."""

    ordinal: int
    reconciliation_id: str
    files: tuple[OverlayFile, ...]

    def __post_init__(self) -> None:
        if isinstance(self.ordinal, bool) or self.ordinal < 0:
            raise StateConflictError("managed tree overlay ordinal must be a non-negative integer")
        if _RECONCILIATION_ID.fullmatch(self.reconciliation_id) is None:
            raise StateConflictError("managed tree overlay reconciliation_id is invalid")
        if not self.files or tuple(sorted(self.files, key=lambda item: item.path)) != self.files:
            raise StateConflictError("managed tree overlay files must be non-empty and ordered by path")
        if len({item.path for item in self.files}) != len(self.files):
            raise StateConflictError("managed tree overlay has duplicate paths")

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "ordinal": self.ordinal,
            "reconciliation_id": self.reconciliation_id,
            "files": [item.to_dict() for item in self.files],
        }

    @classmethod
    def from_dict(cls, raw: JsonValue) -> OverlayEntry:
        if not isinstance(raw, dict) or frozenset(raw) != _ENTRY_KEYS:
            raise StateError("managed tree overlay entry does not match the closed v1 schema")
        ordinal = raw.get("ordinal")
        reconciliation_id = raw.get("reconciliation_id")
        files = raw.get("files")
        if isinstance(ordinal, bool) or not isinstance(ordinal, int) or not isinstance(reconciliation_id, str) or not isinstance(files, list):
            raise StateError("managed tree overlay entry fields are invalid")
        try:
            return cls(ordinal, reconciliation_id, tuple(OverlayFile.from_dict(item) for item in files))
        except StateConflictError as exc:
            raise StateError(str(exc)) from exc


@dataclass(frozen=True)
class ManagedTreeLedger:
    """The closed target-local v1 overlay ledger."""

    baseline_digest: str
    entries: tuple[OverlayEntry, ...]

    def __post_init__(self) -> None:
        _require_seed_tree_hash(self.baseline_digest, "baseline_digest")
        if tuple(item.ordinal for item in self.entries) != tuple(range(len(self.entries))):
            raise StateConflictError("managed tree overlay ordinals must be dense and append-only")
        if len({item.reconciliation_id for item in self.entries}) != len(self.entries):
            raise StateConflictError("managed tree overlay reuses a reconciliation receipt")
        _effective_overlay_files(self.entries)

    @property
    def digest(self) -> str:
        """Use the platform's sole canonical JSON hashing convention."""

        return canonical_sha256(self.to_dict())

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "schema": _SCHEMA,
            "baseline_digest": self.baseline_digest,
            "entries": [item.to_dict() for item in self.entries],
        }

    @property
    def effective_files(self) -> tuple[OverlayFile, ...]:
        """Collapse ordered replacements to the target's final managed delta."""

        return _effective_overlay_files(self.entries)

    @classmethod
    def from_dict(cls, raw: dict[str, JsonValue]) -> ManagedTreeLedger:
        if frozenset(raw) != _LEDGER_KEYS or raw.get("schema") != _SCHEMA:
            raise StateError("managed tree ledger does not match the closed v1 schema")
        baseline_digest = raw.get("baseline_digest")
        entries = raw.get("entries")
        if not isinstance(baseline_digest, str) or not isinstance(entries, list):
            raise StateError("managed tree ledger fields are invalid")
        try:
            return cls(baseline_digest, tuple(OverlayEntry.from_dict(item) for item in entries))
        except StateConflictError as exc:
            raise StateError(str(exc)) from exc


class ManagedTreeStore:
    """Target-local durable storage for a single managed-tree ledger."""

    def __init__(self, target: Path) -> None:
        if not target.is_absolute() or target.is_symlink() or not target.is_dir():
            raise StateConflictError("managed tree target is not a real directory")
        self._target = target
        self._path = target / "profile" / "data" / "reconciliations" / "managed_tree.json"

    @property
    def path(self) -> Path:
        return self._path

    def load(self) -> ManagedTreeLedger | None:
        raw = load_json_object(self._path, missing_ok=True)
        return None if raw is None else ManagedTreeLedger.from_dict(raw)

    def append(self, *, baseline_digest: str, entry: OverlayEntry) -> ManagedTreeLedger:
        current = self.load()
        if current is None:
            current = ManagedTreeLedger(baseline_digest, ())
        if current.baseline_digest != baseline_digest:
            raise StateConflictError("managed tree baseline digest changed before overlay append")
        if entry.ordinal != len(current.entries):
            raise StateConflictError("managed tree overlay entry is not the next append ordinal")
        _require_terminal_receipt(self._target, entry.reconciliation_id)
        next_ledger = ManagedTreeLedger(current.baseline_digest, (*current.entries, entry))
        ensure_private_directory(self._path.parent)
        atomic_write_json(self._path, next_ledger.to_dict())
        return next_ledger


@dataclass(frozen=True)
class ManagedTreeVerification:
    """A successful recomputation result, suitable for checkout probes."""

    digest: str
    baseline_digest: str
    entry_count: int

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "schema": _SCHEMA,
            "status": "verified",
            "managed_tree_digest": self.digest,
            "baseline_digest": self.baseline_digest,
            "entry_count": self.entry_count,
        }


def verify_managed_tree(
    target: Path,
    *,
    seed_tree_hash: str,
    expected_digest: str,
    runner: GitQueryRunner | None = None,
) -> ManagedTreeVerification:
    """Refuse every unmanaged, stale, malformed, or unauthenticated departure."""

    _require_sha256(expected_digest, "expected managed_tree_digest")
    store = ManagedTreeStore(target)
    ledger = store.load()
    if ledger is None:
        return _verify_pristine_managed_tree(target, seed_tree_hash, expected_digest, runner)
    return _verify_overlay_managed_tree(target, ledger, seed_tree_hash, expected_digest, runner)


def _verify_pristine_managed_tree(
    target: Path,
    seed_tree_hash: str,
    expected_digest: str,
    runner: GitQueryRunner | None,
) -> ManagedTreeVerification:
    baseline = verify_seed_tree(target, seed_tree_hash, runner=runner)
    if not baseline.baseline_matches:
        raise StateConflictError(f"managed tree baseline mismatch: {baseline.reason}")
    empty = ManagedTreeLedger(seed_tree_hash, ())
    if empty.digest != expected_digest:
        raise StateConflictError("managed tree digest mismatch for pristine seed baseline")
    return ManagedTreeVerification(empty.digest, empty.baseline_digest, 0)


def _verify_overlay_managed_tree(
    target: Path,
    ledger: ManagedTreeLedger,
    seed_tree_hash: str,
    expected_digest: str,
    runner: GitQueryRunner | None,
) -> ManagedTreeVerification:
    if ledger.digest != expected_digest:
        raise StateConflictError("managed tree receipt-chain/digest mismatch")
    baseline = verify_seed_tree(target, seed_tree_hash, runner=runner)
    _require_overlay_baseline(baseline, ledger, seed_tree_hash)
    effective_files = ledger.effective_files
    _verify_overlay_receipts(target, ledger.entries)
    _verify_overlay_files(target, effective_files)
    _require_exact_overlay_paths(baseline.deviations, effective_files)
    return ManagedTreeVerification(ledger.digest, ledger.baseline_digest, len(ledger.entries))


def _require_overlay_baseline(
    baseline: SeedTreeVerification,
    ledger: ManagedTreeLedger,
    seed_tree_hash: str,
) -> None:
    if baseline.query_error is not None or baseline.observed_head_tree_hash != seed_tree_hash:
        raise StateConflictError(f"managed tree baseline mismatch: {baseline.reason}")
    if ledger.baseline_digest != seed_tree_hash:
        raise StateConflictError("managed tree baseline_digest does not match seed_tree_hash")


def _verify_overlay_receipts(target: Path, entries: tuple[OverlayEntry, ...]) -> None:
    for entry in entries:
        _require_terminal_receipt(target, entry.reconciliation_id)


def _verify_overlay_files(target: Path, replacements: tuple[OverlayFile, ...]) -> None:
    for replacement in replacements:
        _verify_overlay_file(target, replacement)


def _require_exact_overlay_paths(
    deviations: tuple[SeedTreeDeviation, ...],
    replacements: tuple[OverlayFile, ...],
) -> None:
    observed_paths = {path for deviation in deviations for path in deviation.paths}
    expected_paths = {replacement.path for replacement in replacements}
    if observed_paths != expected_paths:
        raise StateConflictError(
            "managed tree has undeclared tracked delta or missing/extra managed overlay path"
        )


def _effective_overlay_files(entries: tuple[OverlayEntry, ...]) -> tuple[OverlayFile, ...]:
    """Replay the ordered receipt chain without mistaking historical paths for live ones."""

    states: dict[str, tuple[str | None, str | None]] = {}
    for entry in entries:
        for replacement in entry.files:
            previous = states.get(replacement.path)
            if previous is not None and replacement.before_sha256 != previous[1]:
                raise StateConflictError(
                    "managed tree overlay receipt chain has a discontinuous before_sha256"
                )
            initial_before = replacement.before_sha256 if previous is None else previous[0]
            states[replacement.path] = (initial_before, replacement.after_sha256)
    return tuple(
        OverlayFile(path, before_sha256, after_sha256)
        for path, (before_sha256, after_sha256) in sorted(states.items())
        if before_sha256 != after_sha256
    )


def _receipt_path(target: Path, reconciliation_id: str) -> Path:
    return target / "profile" / "data" / "reconciliations" / "adapter" / "receipts" / f"{reconciliation_id}.json"


def _require_terminal_receipt(target: Path, reconciliation_id: str) -> None:
    raw = load_json_object(_receipt_path(target, reconciliation_id), missing_ok=True)
    if raw is None:
        raise StateConflictError("managed tree overlay receipt is missing at the target")
    if raw.get("kind") != "cutover_terminal_receipt":
        raise StateConflictError("managed tree overlay receipt is not terminal")
    terminal = raw.get("terminal")
    if not isinstance(terminal, dict) or terminal.get("status") not in _SUCCESSFUL_RECEIPT_STATUSES:
        raise StateConflictError("managed tree overlay receipt terminal status is not successful")


def _verify_overlay_file(target: Path, replacement: OverlayFile) -> None:
    path = target / replacement.path
    try:
        info = path.lstat()
    except FileNotFoundError:
        if replacement.after_sha256 is None:
            return
        raise StateConflictError(f"managed tree declared path is missing: {replacement.path}") from None
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise StateConflictError(f"managed tree declared path is not a regular file: {replacement.path}")
    if replacement.after_sha256 is None:
        raise StateConflictError(f"managed tree deleted path still exists: {replacement.path}")
    actual = f"sha256:{hashlib.sha256(path.read_bytes()).hexdigest()}"
    if actual != replacement.after_sha256:
        raise StateConflictError(f"managed tree declared path content is wrong: {replacement.path}")
