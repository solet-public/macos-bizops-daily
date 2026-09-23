#!/usr/bin/env python3
"""Build and check an exact future candidate tree without mutating Git.

The snapshot starts from a committed Git tree and overlays only the explicitly
named working-tree paths.  Every Git blob is materialized with its recorded
mode intact — executables keep the exec bit and symlink blobs become real
symlinks — so the candidate is a faithful copy of its base and can host
behavioural suites, not only static gates.  Symlinks are represented by their
Git ``120000`` target-string blobs and never dereferenced: candidate validation
checks snapshot membership and bytes, not whether a target is reachable inside
the snapshot.  This preserves tracked aggregation links to sibling repositories.
The emitted manifest is canonical and complete: its bytes describe exactly
every candidate entry, excluding the generated artifacts (bytecode, build
output) a battery deposits into the tree it is measuring.
"""

from __future__ import annotations

import argparse
import os
import stat
import subprocess
import sys
import unicodedata
from collections.abc import Generator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from tempfile import TemporaryDirectory

_REPO_IMPORT_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_IMPORT_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_IMPORT_ROOT))

from quality_gates.source_root import (  # noqa: E402
    SourceRootError,
    gate_script_path,
    resolve_source_root,
)


class CandidateTreeError(RuntimeError):
    """Base class for actionable candidate-tree failures."""


class CandidatePathError(CandidateTreeError):
    """An input path is not a safe, unambiguous repository-relative path."""


class CandidateGitError(CandidateTreeError):
    """The committed base tree could not be read completely."""


class CandidateOverlayError(CandidateTreeError):
    """The explicit working-tree overlay is unresolved or non-regular."""


class CandidateManifestError(CandidateTreeError):
    """A candidate manifest is malformed or incomplete."""


@dataclass(frozen=True)
class Rename:
    old: str
    new: str


@dataclass(frozen=True)
class CandidateTree:
    root: Path
    manifest: Path
    paths: tuple[str, ...]


@dataclass(frozen=True)
class FrozenEntry:
    """One verified final entry supplied by a landing-wave manifest.

    ``data=None`` is an explicit deletion and therefore cannot also carry a
    mode.  The public type deliberately contains bytes, not a source worktree
    path: composition has already frozen those bytes before this materializer
    runs, so it cannot silently reread a dirty lane.
    """

    path: str
    data: bytes | None
    mode: str | None


@dataclass(frozen=True)
class _GitBlob:
    path: str
    object_id: str
    mode: str


@dataclass(frozen=True)
class _Content:
    """Materializable bytes plus the Git file mode that must survive with them."""

    data: bytes
    mode: str


_REGULAR_MODE = "100644"
_EXECUTABLE_MODE = "100755"
_SYMLINK_MODE = "120000"
_SUPPORTED_MODES = frozenset({_REGULAR_MODE, _EXECUTABLE_MODE, _SYMLINK_MODE})

# Generated artifacts a battery deposits into the tree it is measuring.  None of
# these is tracked in any committed base (asserted in `_materialize_git_blobs`),
# so omitting them from the census cannot mask a real content difference.
_GENERATED_COMPONENTS = frozenset({"__pycache__", ".venv", ".pytest_cache", ".mypy_cache"})
_GENERATED_SUFFIXES = (".pyc", ".pyo")


def _is_generated_artifact(relpath: str) -> bool:
    parts = PurePosixPath(relpath).parts
    if any(part in _GENERATED_COMPONENTS or part.endswith(".egg-info") for part in parts):
        return True
    return relpath.endswith(_GENERATED_SUFFIXES)


def _validate_relative_path(raw_path: str, *, field: str) -> str:
    if not raw_path:
        raise CandidatePathError(f"{field} path is empty")
    if "\0" in raw_path or "\n" in raw_path or "\r" in raw_path:
        raise CandidatePathError(f"{field} path contains a manifest delimiter: {raw_path!r}")
    path = PurePosixPath(raw_path)
    if path.is_absolute():
        raise CandidatePathError(f"{field} path must be repository-relative: {raw_path!r}")
    if raw_path != path.as_posix() or any(part in {"", ".", ".."} for part in path.parts):
        raise CandidatePathError(
            f"{field} path must be normalized and traversal-free: {raw_path!r}"
        )
    if path.parts[0] == ".git":
        raise CandidatePathError(f"{field} path may not address .git: {raw_path!r}")
    return raw_path


def _validated_scope(paths: Sequence[str]) -> tuple[str, ...]:
    normalized = tuple(_validate_relative_path(path, field="scope") for path in paths)
    duplicates = sorted(path for path in set(normalized) if normalized.count(path) > 1)
    if duplicates:
        raise CandidatePathError(f"duplicate scope paths: {duplicates}")
    return tuple(sorted(normalized))


def _validated_renames(
    renames: Sequence[Rename], scope: tuple[str, ...]
) -> tuple[Rename, ...]:
    normalized = tuple(
        Rename(
            old=_validate_relative_path(rename.old, field="rename old"),
            new=_validate_relative_path(rename.new, field="rename new"),
        )
        for rename in renames
    )
    if any(rename.old == rename.new for rename in normalized):
        raise CandidatePathError("a rename must have different old and new paths")
    sides = tuple(side for rename in normalized for side in (rename.old, rename.new))
    duplicates = sorted(path for path in set(sides) if sides.count(path) > 1)
    if duplicates:
        raise CandidatePathError(f"duplicate or chained rename sides: {duplicates}")
    missing_scope = sorted(set(sides) - set(scope))
    if missing_scope:
        raise CandidatePathError(
            f"every rename side must be present in the exact scope: {missing_scope}"
        )
    return tuple(sorted(normalized, key=lambda rename: (rename.old, rename.new)))


def _git_output(repo_root: Path, arguments: Sequence[str], *, input_bytes: bytes = b"") -> bytes:
    result = subprocess.run(
        ["git", *arguments],
        cwd=repo_root,
        check=False,
        capture_output=True,
        input=input_bytes,
    )
    if result.returncode != 0:
        detail = result.stderr.decode(errors="replace").strip()
        raise CandidateGitError(
            f"git {' '.join(arguments)} failed: {detail or f'exit {result.returncode}'}"
        )
    return result.stdout


def _parse_git_entries(raw: bytes, *, source: str) -> tuple[_GitBlob, ...]:
    blobs: list[_GitBlob] = []
    paths: set[str] = set()
    for raw_record in (record for record in raw.split(b"\0") if record):
        try:
            metadata, raw_path = raw_record.split(b"\t", maxsplit=1)
            raw_mode, raw_type, raw_object_id = metadata.split()
            mode = raw_mode.decode("ascii")
            object_type = raw_type.decode("ascii")
            object_id = raw_object_id.decode("ascii")
            relpath = raw_path.decode("utf-8")
        except (UnicodeError, ValueError) as exc:
            raise CandidateGitError(f"malformed {source} entry: {raw_record!r}") from exc
        relpath = _validate_relative_path(relpath, field=source)
        if relpath in paths:
            raise CandidateGitError(f"{source} repeats path: {relpath}")
        paths.add(relpath)
        if mode in _SUPPORTED_MODES and object_type == "blob":
            blobs.append(_GitBlob(path=relpath, object_id=object_id, mode=mode))
        elif mode == "160000" and object_type == "commit":
            continue
        else:
            raise CandidateGitError(
                f"{source} contains unsupported entry {mode} {object_type}: {relpath}"
            )
    return tuple(sorted(blobs, key=lambda blob: blob.path))


def _git_tree_blobs(repo_root: Path, base_ref: str) -> tuple[_GitBlob, ...]:
    if not base_ref or base_ref.startswith("-"):
        raise CandidateGitError(f"base ref is invalid: {base_ref!r}")
    return _parse_git_entries(
        _git_output(repo_root, ("ls-tree", "-rz", "--full-tree", base_ref)),
        source=f"committed base {base_ref!r}",
    )


def _git_index_blobs(repo_root: Path) -> tuple[_GitBlob, ...]:
    raw = _git_output(repo_root, ("ls-files", "--stage", "-z"))
    normalized = bytearray()
    for record in (item for item in raw.split(b"\0") if item):
        try:
            metadata, raw_path = record.split(b"\t", maxsplit=1)
            raw_mode, raw_object_id, raw_stage = metadata.split()
        except ValueError as exc:
            raise CandidateGitError(f"malformed staged index entry: {record!r}") from exc
        if raw_stage != b"0":
            path = raw_path.decode("utf-8", errors="replace")
            raise CandidateGitError(f"staged index is unmerged at {path!r}")
        object_type = b"commit" if raw_mode == b"160000" else b"blob"
        normalized.extend(b" ".join((raw_mode, object_type, raw_object_id)))
        normalized.extend(b"\t")
        normalized.extend(raw_path)
        normalized.extend(b"\0")
    return _parse_git_entries(bytes(normalized), source="staged index")


def _read_batch_blob(
    raw: bytes, offset: int, expected_object_id: str
) -> tuple[bytes, int]:
    header_end = raw.find(b"\n", offset)
    if header_end < 0:
        raise CandidateGitError(
            f"git cat-file omitted a header for {expected_object_id}"
        )
    header = raw[offset:header_end]
    try:
        returned_object_id, object_type, raw_size = header.decode("ascii").split()
        size = int(raw_size)
    except (UnicodeError, ValueError) as exc:
        raise CandidateGitError(f"malformed git cat-file header: {header!r}") from exc
    if returned_object_id != expected_object_id or object_type != "blob":
        raise CandidateGitError(
            f"git cat-file returned {header!r} for blob {expected_object_id}"
        )
    content_start = header_end + 1
    content_end = content_start + size
    if content_end >= len(raw) or raw[content_end : content_end + 1] != b"\n":
        raise CandidateGitError(f"git cat-file truncated blob {expected_object_id}")
    return raw[content_start:content_end], content_end + 1


def _git_blob_contents(repo_root: Path, blobs: Sequence[_GitBlob]) -> dict[str, bytes]:
    object_ids = tuple(dict.fromkeys(blob.object_id for blob in blobs))
    if not object_ids:
        return {}
    raw = _git_output(
        repo_root,
        ("cat-file", "--batch"),
        input_bytes="".join(f"{object_id}\n" for object_id in object_ids).encode("ascii"),
    )
    contents: dict[str, bytes] = {}
    offset = 0
    for expected_object_id in object_ids:
        contents[expected_object_id], offset = _read_batch_blob(
            raw, offset, expected_object_id
        )
    if offset != len(raw):
        raise CandidateGitError("git cat-file returned unexpected trailing bytes")
    return contents


def _portable_path_key(relpath: str) -> str:
    return unicodedata.normalize("NFC", relpath).casefold()


def _assert_portable_path_set(paths: Sequence[str], *, source: str) -> None:
    owners: dict[str, str] = {}
    for relpath in paths:
        key = _portable_path_key(relpath)
        previous = owners.setdefault(key, relpath)
        if previous != relpath:
            raise CandidateGitError(
                f"{source} contains case-fold or Unicode-normalization collision: "
                f"{previous!r} and {relpath!r}"
            )


def _write_snapshot_file(snapshot_root: Path, relpath: str, content: _Content) -> None:
    """Materialize one entry with its Git mode intact.

    A candidate that drops the executable bit cannot exec its own tracked
    scripts, and one that flattens a symlink into its target text breaks every
    traversal through it.  Either way the tree stops being a faithful copy of
    its base and can host static gates only.
    """

    destination = snapshot_root / relpath
    try:
        destination.parent.mkdir(parents=True, exist_ok=True)
        # Never write THROUGH an existing entry: if the base materialized a
        # symlink here, `write_bytes` would follow it and corrupt its target.
        if destination.is_symlink() or destination.exists():
            destination.unlink()
        if content.mode == _SYMLINK_MODE:
            try:
                target = content.data.decode("utf-8")
            except UnicodeError as exc:
                raise CandidateOverlayError(
                    f"candidate symlink target is not UTF-8: {relpath!r}"
                ) from exc
            # Git symlink blobs are target strings, not files to resolve.  In
            # particular, a tracked aggregation link may deliberately point to
            # a sibling repository.  This materializer and `_snapshot_paths`
            # use lstat/readlink only, so accepting that target cannot traverse
            # or write outside the candidate snapshot.
            destination.symlink_to(target)
            return
        destination.write_bytes(content.data)
        destination.chmod(0o755 if content.mode == _EXECUTABLE_MODE else 0o644)
    except OSError as exc:
        raise CandidateOverlayError(f"cannot materialize candidate file {relpath!r}: {exc}") from exc


def _materialize_git_blobs(
    repo_root: Path, snapshot_root: Path, blobs: Sequence[_GitBlob], *, source: str
) -> tuple[str, ...]:
    paths = tuple(blob.path for blob in blobs)
    _assert_portable_path_set(paths, source=source)
    tracked_artifacts = sorted(path for path in paths if _is_generated_artifact(path))
    if tracked_artifacts:
        # The census exclusion below is only sound while no committed base
        # tracks a generated artifact.  If one ever does, stop rather than
        # silently drop real content out of the manifest.
        raise CandidateGitError(
            f"{source} tracks generated artifacts the candidate census excludes: "
            f"{tracked_artifacts}"
        )
    contents = _git_blob_contents(repo_root, blobs)
    for blob in sorted(blobs, key=lambda blob: (len(PurePosixPath(blob.path).parts), blob.path)):
        try:
            content = contents[blob.object_id]
        except KeyError as exc:
            raise CandidateGitError(
                f"{source} blob was not returned: {blob.object_id} {blob.path}"
            ) from exc
        _write_snapshot_file(
            snapshot_root, blob.path, _Content(data=content, mode=blob.mode)
        )
    return paths


def _working_path(
    repo_root: Path, relpath: str, scope: frozenset[str]
) -> Path | None:
    current = repo_root
    for part in PurePosixPath(relpath).parts[:-1]:
        current /= part
        try:
            mode = current.lstat().st_mode
        except FileNotFoundError:
            return repo_root / relpath
        if stat.S_ISLNK(mode):
            raise CandidateOverlayError(
                f"scope path traverses a working-tree symlink: {relpath!r}"
            )
        if not stat.S_ISDIR(mode):
            current_relpath = current.relative_to(repo_root).as_posix()
            if current_relpath in scope and stat.S_ISREG(mode):
                return None
            raise CandidateOverlayError(
                f"scope path has a non-directory parent {current.relative_to(repo_root)!s}"
            )
    return repo_root / relpath


def _read_working_regular_file(
    repo_root: Path, relpath: str, scope: frozenset[str]
) -> _Content | None:
    source = _working_path(repo_root, relpath, scope)
    if source is None:
        return None
    return _read_working_entry(source, relpath, scope)


def _read_working_entry(
    source: Path, relpath: str, scope: frozenset[str]
) -> _Content | None:
    try:
        mode = source.lstat().st_mode
    except FileNotFoundError:
        return None
    if stat.S_ISDIR(mode) and any(
        candidate.startswith(f"{relpath}/") for candidate in scope
    ):
        return None
    if stat.S_ISLNK(mode):
        return _read_working_symlink(source, relpath)
    if not stat.S_ISREG(mode):
        raise CandidateOverlayError(
            f"scoped working-tree path must be a regular file, symlink, or an absent deletion: {relpath!r}"
        )
    return _read_regular_file(source, relpath)


def _read_regular_file(source: Path, relpath: str) -> _Content:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(source, flags)
        with os.fdopen(descriptor, "rb") as stream:
            opened = os.fstat(stream.fileno())
            if not stat.S_ISREG(opened.st_mode):
                raise CandidateOverlayError(
                    f"scoped path changed away from a regular file while reading: {relpath!r}"
                )
            return _Content(
                data=stream.read(),
                mode=_EXECUTABLE_MODE if opened.st_mode & stat.S_IXUSR else _REGULAR_MODE,
            )
    except OSError as exc:
        raise CandidateOverlayError(f"cannot read scoped file {relpath!r}: {exc}") from exc


def _read_working_symlink(source: Path, relpath: str) -> _Content:
    """Return a scoped symlink's Git 120000 target-string payload."""

    # The snapshot gate validates the scoped entry itself; target reachability
    # is intentionally outside its contract so sibling-repository aggregation
    # links remain faithful candidate content.
    try:
        return _Content(data=os.fsencode(os.readlink(source)), mode=_SYMLINK_MODE)
    except OSError as exc:
        raise CandidateOverlayError(f"cannot read scoped symlink {relpath!r}: {exc}") from exc


def _remove_candidate_path(snapshot_root: Path, relpath: str) -> None:
    destination = snapshot_root / relpath
    try:
        mode = destination.lstat().st_mode
    except FileNotFoundError:
        return
    if not (stat.S_ISREG(mode) or stat.S_ISLNK(mode)):
        raise CandidateOverlayError(
            f"candidate deletion target is not a regular file or symlink: {relpath!r}"
        )
    destination.unlink()
    parent = destination.parent
    while parent != snapshot_root:
        try:
            parent.rmdir()
        except OSError:
            break
        parent = parent.parent


def _overlay_scope(
    repo_root: Path,
    snapshot_root: Path,
    base_paths: tuple[str, ...],
    scope: tuple[str, ...],
    renames: tuple[Rename, ...],
) -> tuple[str, ...]:
    base_set = set(base_paths)
    scope_set = frozenset(scope)
    working_contents = {
        relpath: _read_working_regular_file(repo_root, relpath, scope_set)
        for relpath in scope
    }
    for rename in renames:
        if rename.old not in base_set:
            raise CandidateOverlayError(
                f"rename old path is absent from the committed base: {rename.old!r}"
            )
        if working_contents[rename.old] is not None:
            raise CandidateOverlayError(
                f"rename old path still exists in the working tree: {rename.old!r}"
            )
        if working_contents[rename.new] is None:
            raise CandidateOverlayError(
                f"rename new path is absent from the working tree: {rename.new!r}"
            )

    return _apply_content_overlay(snapshot_root, base_paths, working_contents)


def _apply_content_overlay(
    snapshot_root: Path,
    base_paths: Sequence[str],
    contents: Mapping[str, _Content | None],
) -> tuple[str, ...]:
    base_set = set(base_paths)

    deletions = tuple(
        relpath for relpath, content in contents.items() if content is None
    )
    missing = sorted(set(deletions) - base_set)
    if missing:
        raise CandidateOverlayError(
            f"scoped paths are absent from both base and working tree: {missing}"
        )
    additions = {
        relpath: content
        for relpath, content in contents.items()
        if content is not None
    }
    expected = tuple(sorted((base_set - set(deletions)) | set(additions)))
    _assert_portable_path_set(expected, source="final candidate path set")

    for relpath in sorted(
        deletions, key=lambda path: (-len(PurePosixPath(path).parts), path)
    ):
        _remove_candidate_path(snapshot_root, relpath)
    for relpath in sorted(
        additions, key=lambda path: (len(PurePosixPath(path).parts), path)
    ):
        _write_snapshot_file(snapshot_root, relpath, additions[relpath])
    return expected


def _snapshot_paths(snapshot_root: Path) -> tuple[str, ...]:
    paths: list[str] = []
    for path in sorted(snapshot_root.rglob("*")):
        mode = path.lstat().st_mode
        relpath = path.relative_to(snapshot_root).as_posix()
        if PurePosixPath(relpath).parts[0] == ".git":
            # A private candidate repository provisions `.git` AFTER
            # materialization so gates needing Git can run inside the tree.  It
            # is infrastructure, never candidate content, and every other path
            # check in this module already refuses to address it.
            continue
        if _is_generated_artifact(relpath):
            # A battery run deposits bytecode and build output into the very
            # tree it is measuring.  Excluding it keeps the census a statement
            # about candidate content rather than about the battery's exhaust.
            continue
        if stat.S_ISDIR(mode):
            continue
        if stat.S_ISLNK(mode):
            # Validation is deliberately about entries and their target-string
            # bytes, never target reachability.  Do not resolve this link:
            # sibling-repository aggregation links are valid tracked content.
            os.readlink(path)
        elif not stat.S_ISREG(mode):
            raise CandidateManifestError(
                f"candidate snapshot contains a special file: {relpath!r}"
            )
        paths.append(_validate_relative_path(relpath, field="snapshot"))
    return tuple(sorted(paths))


def _assert_snapshot_matches(
    expected: Sequence[str], actual: Sequence[str], *, source: str
) -> None:
    if tuple(expected) == tuple(actual):
        return
    missing = sorted(set(expected) - set(actual))
    unexpected = sorted(set(actual) - set(expected))
    raise CandidateManifestError(
        f"{source} differs from the materialized snapshot: "
        f"missing={missing} unexpected={unexpected}"
    )


def _manifest_bytes(paths: Sequence[str]) -> bytes:
    return "".join(f"{path}\n" for path in paths).encode()


def validate_candidate_manifest(snapshot_root: Path, manifest: Path) -> tuple[str, ...]:
    """Return the canonical complete manifest or raise a typed failure."""

    try:
        raw = manifest.read_bytes()
        rendered = raw.decode("utf-8")
    except (OSError, UnicodeError) as exc:
        raise CandidateManifestError(f"candidate manifest is unreadable UTF-8: {exc}") from exc
    listed = tuple(rendered.splitlines())
    for relpath in listed:
        _validate_relative_path(relpath, field="manifest")
    duplicates = sorted(path for path in set(listed) if listed.count(path) > 1)
    if duplicates:
        raise CandidateManifestError(f"candidate manifest contains duplicates: {duplicates}")
    if raw != _manifest_bytes(tuple(sorted(listed))):
        raise CandidateManifestError(
            "candidate manifest bytes are not the canonical sorted newline-delimited form"
        )
    actual = _snapshot_paths(snapshot_root)
    if listed != actual:
        missing = sorted(set(actual) - set(listed))
        unexpected = sorted(set(listed) - set(actual))
        raise CandidateManifestError(
            f"candidate manifest is incomplete: missing={missing} unexpected={unexpected}"
        )
    return listed


def censusable_manifest_paths(snapshot_root: Path, manifest: Path) -> tuple[str, ...]:
    """The validated manifest, minus the entries no content census can read.

    A faithful candidate preserves symlinks as symlinks, so a complete manifest
    of one legitimately lists paths that hold no readable content of their own.
    Git's own index records those as mode 120000 and content censuses skip them
    for that reason; before candidates preserved symlinks every path arrived
    flattened into an ordinary file, so no consumer had to say so.

    Acquisition integrity is unaffected: the manifest must still be canonical
    and equal to the snapshot exactly, and it is validated in full here. Only
    the reading view narrows. An absent or otherwise unreadable path is not
    filtered — it is left in for the consumer to fail closed on.
    """

    listed = validate_candidate_manifest(snapshot_root, manifest)
    return tuple(
        relpath for relpath in listed if not (snapshot_root / relpath).is_symlink()
    )


def materialize_candidate_tree(
    repo_root: Path,
    destination: Path,
    scope_paths: Sequence[str],
    *,
    renames: Sequence[Rename] = (),
    base_ref: str = "HEAD",
) -> CandidateTree:
    """Materialize a committed-base-plus-exact-overlay candidate under ``destination``.

    ``destination`` is the caller's to create and to clean up.  It must already
    be a physical path — every consumer of a candidate (pointer files, shebangs,
    ``GIT_WORK_TREE``, the gate's working directory) has to agree on ONE
    spelling, and a path reached through a symlink silently supplies two.
    """

    root = repo_root.resolve()
    if not root.is_dir():
        raise CandidatePathError(f"repository root is not a directory: {root}")
    if str(destination) != str(destination.resolve()):
        raise CandidatePathError(
            "candidate destination must already be its own physical path: "
            f"{destination} resolves to {destination.resolve()}"
        )
    scope = _validated_scope(scope_paths)
    if not scope:
        raise CandidatePathError("candidate scope must contain at least one explicit path")
    checked_renames = _validated_renames(renames, scope)
    blobs = _git_tree_blobs(root, base_ref)
    snapshot_root = destination / "tree"
    snapshot_root.mkdir(parents=True)
    base_paths = _materialize_git_blobs(
        root,
        snapshot_root,
        blobs,
        source=f"committed base {base_ref!r}",
    )
    expected = _overlay_scope(root, snapshot_root, base_paths, scope, checked_renames)
    paths = _snapshot_paths(snapshot_root)
    _assert_snapshot_matches(expected, paths, source="final candidate path set")
    manifest = destination / "candidate-manifest.txt"
    manifest.write_bytes(_manifest_bytes(paths))
    validated = validate_candidate_manifest(snapshot_root, manifest)
    return CandidateTree(root=snapshot_root, manifest=manifest, paths=validated)


def _frozen_entry_contents(
    entries: Sequence[FrozenEntry], paths: Sequence[str]
) -> dict[str, _Content | None]:
    """Translate closed public frozen entries into the internal overlay form."""
    by_path = {entry.path: entry for entry in entries}
    if len(by_path) != len(entries):
        raise CandidatePathError("frozen-entry composition contains duplicate paths")
    contents: dict[str, _Content | None] = {}
    for path in paths:
        entry = by_path[path]
        if entry.data is None:
            if entry.mode is not None:
                raise CandidateOverlayError(f"deleted frozen entry has a mode: {path!r}")
            contents[path] = None
            continue
        if entry.mode not in _SUPPORTED_MODES:
            raise CandidateOverlayError(
                f"frozen entry has an unsupported mode at {path!r}: {entry.mode!r}"
            )
        contents[path] = _Content(data=entry.data, mode=entry.mode)
    return contents


def materialize_frozen_entries(
    repo_root: Path,
    destination: Path,
    entries: Sequence[FrozenEntry],
    *,
    base_ref: str,
) -> CandidateTree:
    """Materialize a pinned base plus immutable final entry bytes.

    This is the composition primitive for a landing wave.  It performs no Git
    mutation and does not inspect source worktrees; callers must have already
    verified source quiescence, hashes, modes, and base-entry equality.
    """
    root = repo_root.resolve()
    if not root.is_dir():
        raise CandidatePathError(f"repository root is not a directory: {root}")
    if str(destination) != str(destination.resolve()):
        raise CandidatePathError(
            "candidate destination must already be its own physical path: "
            f"{destination} resolves to {destination.resolve()}"
        )
    if not entries:
        raise CandidatePathError("frozen-entry composition requires at least one entry")
    paths = _validated_scope(tuple(entry.path for entry in entries))
    contents = _frozen_entry_contents(entries, paths)

    snapshot_root = destination / "tree"
    snapshot_root.mkdir(parents=True)
    base_paths = _materialize_git_blobs(
        root,
        snapshot_root,
        _git_tree_blobs(root, base_ref),
        source=f"committed base {base_ref!r}",
    )
    expected = _apply_content_overlay(snapshot_root, base_paths, contents)
    actual = _snapshot_paths(snapshot_root)
    _assert_snapshot_matches(expected, actual, source="frozen-entry candidate path set")
    manifest = destination / "candidate-manifest.txt"
    manifest.write_bytes(_manifest_bytes(actual))
    return CandidateTree(
        root=snapshot_root,
        manifest=manifest,
        paths=validate_candidate_manifest(snapshot_root, manifest),
    )


@contextmanager
def build_candidate_tree(
    repo_root: Path,
    scope_paths: Sequence[str],
    *,
    renames: Sequence[Rename] = (),
    base_ref: str = "HEAD",
) -> Generator[CandidateTree]:
    """Yield an ephemeral committed-base-plus-exact-overlay candidate tree."""

    with TemporaryDirectory(prefix="candidate-tree-") as temporary:
        yield materialize_candidate_tree(
            repo_root,
            Path(temporary).resolve(),
            scope_paths,
            renames=renames,
            base_ref=base_ref,
        )


def materialize_staged_tree(
    repo_root: Path,
    destination: Path,
    scope_paths: Sequence[str] | None = None,
    *,
    base_ref: str = "HEAD",
) -> CandidateTree:
    """Materialize the full index, or a committed base plus exact staged scope.

    Staged content is the right source whenever the working tree carries a
    DIFFERENT lane's edits on top of the same path — a working-tree overlay
    would silently pull those in, and path membership would stop meaning
    content ownership.  ``destination`` is the caller's to create and clean up.
    """

    root = repo_root.resolve()
    if not root.is_dir():
        raise CandidatePathError(f"repository root is not a directory: {root}")
    if str(destination) != str(destination.resolve()):
        raise CandidatePathError(
            "candidate destination must already be its own physical path: "
            f"{destination} resolves to {destination.resolve()}"
        )
    scope = None if scope_paths is None else _validated_scope(scope_paths)
    if scope is not None and not scope:
        raise CandidatePathError("staged candidate scope must not be empty")
    index_blobs = _git_index_blobs(root)
    snapshot_root = destination / "tree"
    snapshot_root.mkdir(parents=True)
    if scope is None:
        expected = _materialize_git_blobs(
            root, snapshot_root, index_blobs, source="staged index"
        )
    else:
        base_blobs = _git_tree_blobs(root, base_ref)
        base_paths = _materialize_git_blobs(
            root,
            snapshot_root,
            base_blobs,
            source=f"committed base {base_ref!r}",
        )
        index_contents = _git_blob_contents(root, index_blobs)
        index_by_path = {blob.path: blob for blob in index_blobs}
        staged_contents = {
            relpath: (
                _Content(
                    data=index_contents[index_by_path[relpath].object_id],
                    mode=index_by_path[relpath].mode,
                )
                if relpath in index_by_path
                else None
            )
            for relpath in scope
        }
        expected = _apply_content_overlay(snapshot_root, base_paths, staged_contents)
    paths = _snapshot_paths(snapshot_root)
    _assert_snapshot_matches(expected, paths, source="staged index")
    manifest = destination / "candidate-manifest.txt"
    manifest.write_bytes(_manifest_bytes(paths))
    validated = validate_candidate_manifest(snapshot_root, manifest)
    return CandidateTree(root=snapshot_root, manifest=manifest, paths=validated)


@contextmanager
def build_staged_tree(
    repo_root: Path,
    scope_paths: Sequence[str] | None = None,
    *,
    base_ref: str = "HEAD",
) -> Generator[CandidateTree]:
    """Yield the full index, or a committed base plus exact staged scope."""

    with TemporaryDirectory(prefix="staged-tree-") as temporary:
        yield materialize_staged_tree(
            repo_root,
            Path(temporary).resolve(),
            scope_paths,
            base_ref=base_ref,
        )


def read_scope_file(path: Path) -> tuple[str, ...]:
    try:
        content = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise CandidatePathError(f"scope file is unreadable UTF-8: {exc}") from exc
    paths = tuple(content.splitlines())
    if any(not path for path in paths):
        raise CandidatePathError("scope file may not contain blank lines")
    return paths


def read_rename_file(path: Path) -> tuple[Rename, ...]:
    try:
        content = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise CandidatePathError(f"rename file is unreadable UTF-8: {exc}") from exc
    renames: list[Rename] = []
    for line_number, line in enumerate(content.splitlines(), start=1):
        fields = line.split("\t")
        if len(fields) != 2 or not all(fields):
            raise CandidatePathError(
                f"rename file line {line_number} must be OLD<TAB>NEW"
            )
        renames.append(Rename(old=fields[0], new=fields[1]))
    return tuple(renames)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", type=Path, default=Path.cwd())
    parser.add_argument("--base-ref", default="HEAD")
    parser.add_argument("--scope-file", type=Path)
    parser.add_argument("--rename-file", type=Path)
    parser.add_argument(
        "--staged",
        action="store_true",
        help="gate the exact current Git index instead of a working-tree overlay",
    )
    parser.add_argument(
        "--run-identity-gate",
        dest="run_identity_gate",
        action="store_true",
        help="run the canonical product-identity gate against the candidate",
    )
    args = parser.parse_args(argv)
    try:
        if not args.run_identity_gate:
            raise CandidatePathError("the candidate identity check must be selected")
        if args.staged:
            if args.rename_file is not None:
                raise CandidatePathError(
                    "--staged may not be combined with a rename file"
                )
            scope = (
                None
                if args.scope_file is None
                else read_scope_file(args.scope_file)
            )
            tree = build_staged_tree(
                args.repo_root, scope, base_ref=args.base_ref
            )
            summary = (
                "candidate_tree staged snapshot"
                if scope is None
                else f"candidate_tree staged snapshot: scope={len(scope)}"
            )
        else:
            if args.scope_file is None or args.rename_file is None:
                raise CandidatePathError(
                    "candidate overlay requires --scope-file and --rename-file"
                )
            scope = read_scope_file(args.scope_file)
            renames = read_rename_file(args.rename_file)
            tree = build_candidate_tree(
                args.repo_root,
                scope,
                renames=renames,
                base_ref=args.base_ref,
            )
            summary = f"candidate_tree snapshot: base={args.base_ref} scope={len(scope)}"
        with tree as candidate:
            print(
                f"{summary} files={len(candidate.paths)}"
            )
            # The gate SCRIPT must come from the candidate's own materialized
            # copy, never from wherever this process happens to be invoked —
            # the invoking checkout can be a stale or behind-master worktree
            # whose on-disk gate code predates the very fix under review
            # (iss_ec0db9c7 / iss_77fe09ad). `candidate.root` already holds a
            # full copy of the base-ref tree, so it is always the right root.
            gate_name = "macos_" + "bizops_identity_gate.py"
            source_root = resolve_source_root(candidate.root)
            script = gate_script_path(source_root, f"quality_gates/{gate_name}")
            command = (
                sys.executable,
                str(script),
                "--repo-root",
                str(candidate.root),
                "--candidate-manifest",
                str(candidate.manifest),
            )
            result = subprocess.run(command, cwd=source_root, check=False)
            return result.returncode
    except (CandidateTreeError, SourceRootError) as exc:
        print(f"candidate_tree CRASH: {exc}", file=sys.stderr)
        return 70


if __name__ == "__main__":
    raise SystemExit(main())
