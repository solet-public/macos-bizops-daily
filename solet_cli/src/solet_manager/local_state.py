"""The local-state commitment: observation, comparison, and the per-operation re-baseline (Step 7 sections 6.3, 6.6).

A seed-born clone is never a clean tree: genesis rewrites tracked files and
leaves untracked ones (32 entries on ``macos-bizops``, 19 of them symlinks),
and ``solet create`` merges two more.  A7.2 admits untracked operator files
only with their bytes fingerprinted and rechecked under lock; A7.5 admits
disjoint tracked modifications only on a real-git proof that the fast-forward
preserves every local byte.  This module is that commitment:

* ``observe_local_state`` reads the tracked modifications (sha256 of the bytes
  the Manager reads itself, never git's blob id), partitions the untracked
  entries by the ``profile/`` prefix into the *committed* tier (inventoried and
  digested; symlinks by their stored target string) and the *preserved surface*
  (inventoried only -- the running solet and the Manager's own declared
  migrations write there, so its bytes are disclosed, never committed);
* ``compare`` names what moved between a journaled snapshot and a fresh
  observation, distinguishing the hard tiers (a violation) from the surface (a
  disclosure);
* ``allowed_service_write`` is the one hard-tier allowance across the lifecycle
  stage (B7): a *creation* directly under ``knowledge_bases/`` of a relative
  symlink ``<plugin>`` -> ``../plugins/<plugin>/knowledge_base`` -- exactly what
  the running solet's ``_create_kb_symlink`` writes and nothing else.

Nothing here executes target code or runs git; the paths come from the Step-2
facts and the digests from the Manager's own reads.
"""

from __future__ import annotations

import hashlib
import os
import stat
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import cast

from solet_setup_contracts import canonical_sha256

from .existing_install_inspection import ExistingInstallFacts
from .models import JsonValue
from .update_topology import LocalState, is_preserved_surface

__all__ = [
    "EMPTY_SNAPSHOT",
    "KNOWLEDGE_BASES_PREFIX",
    "UNREAD_DIGEST",
    "LocalStateDelta",
    "ObservedEntry",
    "ObservedLocalState",
    "allowed_service_write",
    "compare",
    "observe_local_state",
    "snapshot_from_journal",
]

UNREAD_DIGEST = "unread"
KNOWLEDGE_BASES_PREFIX = "knowledge_bases/"
#: A committed entry whose bytes are secrets is inventoried but never read (section 6.3).
_SECRET_SUFFIXES = (".enc", ".pem", ".key")
_SECRET_BASENAMES = frozenset({"passphrase"})
_SECRET_PREFIXES = ("profile/config/vault/", "profile/credentials/")
EMPTY_SNAPSHOT: dict[str, JsonValue] = {"preserved_tracked_paths": [], "committed_inventory": [], "local_state_commitment": None, "preserved_surface": []}


@dataclass(frozen=True, slots=True)
class ObservedEntry:
    """One local entry with everything the commitment hashes."""

    path: str
    kind: str
    mode: str
    size: int
    digest: str

    def inventory(self) -> tuple[str, str, str, int]:
        return (self.path, self.kind, self.mode, self.size)


@dataclass(frozen=True, slots=True)
class ObservedLocalState:
    """The full observation: the rendered ``LocalState`` plus the per-entry digests it never renders."""

    state: LocalState
    tracked: tuple[ObservedEntry, ...]
    committed: tuple[ObservedEntry, ...]
    surface: tuple[ObservedEntry, ...]
    staged: tuple[str, ...]

    def snapshot(self) -> dict[str, JsonValue]:
        return self.state.snapshot()


@dataclass(frozen=True, slots=True)
class LocalStateDelta:
    """What moved between a snapshot and a fresh observation, by tier."""

    tracked: tuple[str, ...]
    committed: tuple[str, ...]
    surface: tuple[str, ...]
    staged: tuple[str, ...]

    @property
    def hard(self) -> tuple[str, ...]:
        return tuple(sorted({*self.tracked, *self.committed}))


def observe_local_state(target: Path, facts: ExistingInstallFacts) -> ObservedLocalState:
    """Read every local entry the Step-2 facts name and compute the two hard tiers plus the surface."""
    tracked = tuple(_observe(target, path) for path in sorted(facts.tracked_paths.values))
    committed: list[ObservedEntry] = []
    surface: list[ObservedEntry] = []
    for path in sorted(facts.untracked_paths.values):
        entry = _observe(target, path)
        (surface if is_preserved_surface(path) else committed).append(entry)
    state = LocalState(
        tuple((entry.path, entry.digest, entry.size) for entry in tracked),
        tuple(entry.inventory() for entry in committed),
        commitment(tuple(committed)),
        tuple(entry.inventory() for entry in surface),
    )
    return ObservedLocalState(state, tracked, tuple(committed), tuple(surface), tuple(sorted(facts.staged_paths.values)))


def commitment(entries: tuple[ObservedEntry, ...]) -> str | None:
    """sha256 over the canonical JSON of ``[(path, kind, mode, size, digest)]``; ``None`` for an empty committed set."""
    if not entries:
        return None
    rows: list[JsonValue] = [[entry.path, entry.kind, entry.mode, entry.size, entry.digest] for entry in sorted(entries, key=lambda item: item.path)]
    return canonical_sha256(rows)


def _observe(target: Path, relative: str) -> ObservedEntry:
    path = target / relative
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        # A deleted tracked path (a shape change the reducer refuses); observed as absent, never read.
        return ObservedEntry(relative, "absent", "0000", 0, UNREAD_DIGEST)
    mode = f"{stat.S_IMODE(info.st_mode):04o}"
    if stat.S_ISLNK(info.st_mode):
        link = os.readlink(path)
        return ObservedEntry(relative, "symlink", mode, len(link.encode("utf-8", "surrogateescape")), _sha256(link.encode("utf-8", "surrogateescape")))
    if stat.S_ISDIR(info.st_mode):
        # ``--untracked-files=all`` lists leaves; a directory here is a tracked-path retype and is refused by the shape rule.
        return ObservedEntry(relative, "directory", mode, 0, UNREAD_DIGEST)
    if _is_secret(relative):
        return ObservedEntry(relative, "file", mode, info.st_size, UNREAD_DIGEST)
    return ObservedEntry(relative, "file", mode, info.st_size, _sha256(_read_nofollow(path)))


def _is_secret(relative: str) -> bool:
    name = relative.rsplit("/", 1)[-1]
    return name in _SECRET_BASENAMES or name.endswith(_SECRET_SUFFIXES) or relative.startswith(_SECRET_PREFIXES)


def _read_nofollow(path: Path) -> bytes:
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        chunks: list[bytes] = []
        while True:
            chunk = os.read(descriptor, 1_048_576)
            if not chunk:
                return b"".join(chunks)
            chunks.append(chunk)
    finally:
        os.close(descriptor)


def _sha256(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def snapshot_from_journal(value: dict[str, JsonValue]) -> LocalState:
    """A journaled ``local_state`` snapshot (``baseline``/``current``) as a ``LocalState`` (per-entry digests are not journaled)."""
    tracked = tuple((cast(str, cast(dict[str, JsonValue], row)["path"]), cast(str, cast(dict[str, JsonValue], row)["sha256"]), cast(int, cast(dict[str, JsonValue], row)["size"])) for row in cast(list[JsonValue], value["preserved_tracked_paths"]))
    surface = tuple(_inventory_row(row) for row in cast(list[JsonValue], value["preserved_surface"]))
    commitment_value = value["local_state_commitment"]
    inventory = tuple(_inventory_row(row) for row in cast(list[JsonValue], value["committed_inventory"]))
    return LocalState(tracked, inventory, None if commitment_value is None else cast(str, commitment_value), surface)


def _inventory_row(raw: JsonValue) -> tuple[str, str, str, int]:
    row = cast(dict[str, JsonValue], raw)
    return (cast(str, row["path"]), cast(str, row["kind"]), cast(str, row["mode"]), cast(int, row["size"]))


def compare(expected: LocalState, observed: ObservedLocalState, previous: ObservedLocalState | None = None) -> LocalStateDelta:
    """Name every entry that differs between ``expected`` (a snapshot) and ``observed``.

    Tracked paths compare by set and by journaled digest.  The committed tier
    compares by aggregate commitment; when the aggregate differs the entries
    named are those whose inventory row moved and, when ``previous`` (the
    in-process observation the snapshot was verified equal to) is available,
    those whose per-entry digest moved.  With neither, the whole committed
    inventory is reported as ``commitment_mismatch`` rather than a guess.
    """
    expected_tracked = {path: (digest, size) for path, digest, size in expected.preserved_tracked_paths}
    observed_tracked = {entry.path: (entry.digest, entry.size) for entry in observed.tracked}
    tracked = sorted(path for path in set(expected_tracked) | set(observed_tracked) if expected_tracked.get(path) != observed_tracked.get(path))
    committed: list[str] = []
    if expected.local_state_commitment != observed.state.local_state_commitment:
        committed = _committed_differences(expected, observed, previous)
    expected_surface = {row[0]: row for row in expected.preserved_surface}
    observed_surface = {entry.path: entry.inventory() for entry in observed.surface}
    surface = sorted(path for path in set(expected_surface) | set(observed_surface) if expected_surface.get(path) != observed_surface.get(path))
    return LocalStateDelta(tuple(tracked), tuple(committed), tuple(surface), observed.staged)


def _committed_differences(expected: LocalState, observed: ObservedLocalState, previous: ObservedLocalState | None) -> list[str]:
    if previous is not None:
        return _changed_keys({entry.path: entry for entry in previous.committed}, {entry.path: entry for entry in observed.committed})
    named = _changed_keys({row[0]: row for row in expected.committed_inventory}, {entry.path: entry.inventory() for entry in observed.committed})
    return named or ["commitment_mismatch"]


def _changed_keys(before: Mapping[str, object], after: Mapping[str, object]) -> list[str]:
    return sorted(path for path in set(before) | set(after) if before.get(path) != after.get(path))


def allowed_service_write(target: Path, path: str, previous_paths: frozenset[str]) -> str | None:
    """The B7 allowance: the target digest of a new ``knowledge_bases/<plugin>`` symlink the running solet created, else ``None``.

    Admitted iff the entry is new (not in ``previous_paths``), directly under
    ``knowledge_bases/``, a symlink whose stored target is exactly the relative
    ``../plugins/<plugin>/knowledge_base`` for the link's own name (what
    ``kb_lifecycle._create_kb_symlink`` writes, and nothing wider), and that
    target resolves inside the target tree.  A re-pointed or deleted link, a
    regular file, a link elsewhere or a wider target stays a violation.
    """
    if path in previous_paths or not path.startswith(KNOWLEDGE_BASES_PREFIX):
        return None
    name = path.removeprefix(KNOWLEDGE_BASES_PREFIX)
    if not name or "/" in name:
        return None
    link = target / path
    try:
        info = os.lstat(link)
    except OSError:
        return None
    if not stat.S_ISLNK(info.st_mode):
        return None
    stored = os.readlink(link)
    if stored != f"../plugins/{name}/knowledge_base":
        return None
    resolved = (link.parent / stored).resolve(strict=False)
    try:
        resolved.relative_to(target.resolve(strict=False))
    except ValueError:
        return None
    return _sha256(stored.encode("utf-8", "surrogateescape"))
