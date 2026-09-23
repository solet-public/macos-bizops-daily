"""Secure target-parent creation and identity validation."""

from __future__ import annotations

import os
import stat
from pathlib import Path

from .errors import SourceError

type ParentIdentity = tuple[int, int]


def ensure_target_parent(path: Path) -> ParentIdentity:
    missing, ancestor = _missing_ancestry(path)
    _validate_ancestor(ancestor)
    for directory in reversed(missing):
        directory.mkdir(mode=0o700)
    info = _target_parent_info(path)
    _validate_target_parent(path, info)
    return info.st_dev, info.st_ino


def _missing_ancestry(path: Path) -> tuple[list[Path], Path]:
    missing: list[Path] = []
    cursor = path
    while not cursor.exists():
        missing.append(cursor)
        parent = cursor.parent
        if parent == cursor:
            raise SourceError(f"target parent has no existing ancestor: {path}")
        cursor = parent
    return missing, cursor


def _validate_ancestor(path: Path) -> None:
    try:
        info = path.lstat()
    except OSError as exc:
        raise SourceError(
            f"target parent ancestor cannot be inspected: {path}: {exc}"
        ) from exc
    if not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode):
        raise SourceError(
            f"target parent ancestry crosses a symlink or non-directory: {path}"
        )


def _target_parent_info(path: Path) -> os.stat_result:
    try:
        return path.lstat()
    except OSError as exc:
        raise SourceError(f"target parent cannot be inspected: {path}: {exc}") from exc


def _validate_target_parent(path: Path, info: os.stat_result) -> None:
    if not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode):
        raise SourceError(f"target parent is not a real directory: {path}")
    if info.st_uid != os.getuid():
        raise SourceError(f"target parent is not owned by the current user: {path}")
    if stat.S_IMODE(info.st_mode) & 0o022:
        raise SourceError(
            f"target parent is group/world writable and unsafe for staging: {path}"
        )
    if not os.access(path, os.W_OK | os.X_OK):
        raise SourceError(f"target parent is not writable and searchable: {path}")
