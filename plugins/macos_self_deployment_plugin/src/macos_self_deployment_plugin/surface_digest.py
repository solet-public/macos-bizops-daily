"""Deterministic code-surface digests for reconciliation attestation.

The digest is deliberately defined over immutable code facts rather than file
metadata: sorted ``(relative_path, mode, sha256)`` entries encoded as canonical
JSON.  It is the one implementation used by runtime attestation and the later
cutover controller; callers must not reimplement the construction.
"""

from __future__ import annotations

import hashlib
import json
import stat
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class SurfaceEntry:
    """One regular-file contribution to a code-surface digest."""

    relative_path: str
    mode: int
    sha256: str


def surface_digest(root: Path, relative_roots: Iterable[Path]) -> str:
    """Return the canonical SHA-256 digest for regular files under ``relative_roots``.

    ``relative_roots`` are relative to ``root``.  Symlinks, missing roots, and
    files escaping the real root are rejected: an attestation must not turn an
    ambiguous release tree into a confident digest.
    """
    root_status = root.lstat()
    if stat.S_ISLNK(root_status.st_mode) or not stat.S_ISDIR(root_status.st_mode):
        raise ValueError(f"surface root is not a real directory: {root}")
    real_root = root.resolve(strict=True)
    entries = [
        entry
        for relative_root in relative_roots
        for entry in _surface_entries_for_root(real_root, relative_root)
    ]
    payload = [
        {"mode": entry.mode, "relative_path": entry.relative_path, "sha256": entry.sha256}
        for entry in sorted(entries, key=lambda entry: entry.relative_path)
    ]
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return f"sha256:{hashlib.sha256(encoded).hexdigest()}"


def _surface_entries_for_root(real_root: Path, relative_root: Path) -> list[SurfaceEntry]:
    candidate = _resolve_surface_root(real_root, relative_root)
    entries: list[SurfaceEntry] = []
    for path in sorted(candidate.rglob("*")):
        if path.is_symlink():
            raise ValueError(f"surface contains a non-regular file: {path}")
        if path.is_dir():
            continue
        entries.append(_surface_entry(real_root, path))
    return entries


def _resolve_surface_root(real_root: Path, relative_root: Path) -> Path:
    if relative_root.is_absolute() or ".." in relative_root.parts:
        raise ValueError(f"surface root must be a safe relative path: {relative_root}")
    candidate = real_root
    for component in relative_root.parts:
        candidate /= component
        status = candidate.lstat()
        if stat.S_ISLNK(status.st_mode) or not stat.S_ISDIR(status.st_mode):
            raise ValueError(f"surface root is not a real directory: {candidate}")
    return candidate


def _surface_entry(real_root: Path, path: Path) -> SurfaceEntry:
    if path.is_symlink() or not path.is_file():
        raise ValueError(f"surface contains a non-regular file: {path}")
    resolved = path.resolve(strict=True)
    if not resolved.is_relative_to(real_root):
        raise ValueError(f"surface file escapes root: {path}")
    return SurfaceEntry(
        relative_path=resolved.relative_to(real_root).as_posix(),
        mode=stat.S_IMODE(resolved.stat().st_mode),
        sha256=_file_sha256(resolved),
    )


def reconciliation_surface_digest(code_root: Path) -> str:
    """Digest the reconcile allowlist frozen into an immutable release."""
    plugins_root = code_root / "plugins"
    _require_real_directory(plugins_root)
    plugin_roots = [
        _reconciliation_plugin_src_root(code_root, plugin_root)
        for plugin_root in sorted(plugins_root.iterdir())
    ]
    return surface_digest(code_root, (*plugin_roots, Path("solet_setup_contracts/src")))


def _reconciliation_plugin_src_root(code_root: Path, plugin_root: Path) -> Path:
    _require_real_directory(plugin_root)
    src_root = plugin_root / "src"
    _require_real_directory(src_root)
    return src_root.relative_to(code_root)


def _require_real_directory(path: Path) -> None:
    status = path.lstat()
    if stat.S_ISLNK(status.st_mode) or not stat.S_ISDIR(status.st_mode):
        raise ValueError(f"surface root is not a real directory: {path}")


def release_surface_digest(code_root: Path) -> str:
    """Digest every subtree frozen by :class:`ReleaseManager`."""
    return surface_digest(
        code_root,
        (Path("ananta"), Path("plugins"), Path("solet_cli"), Path("solet_setup_contracts")),
    )


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()
