"""Safe source-checkout snapshots for untracked workbench briefs."""

from __future__ import annotations

import errno
import hashlib
import os
import stat
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Never

from .lane_worktrees import LaneWorktreeRepoRootError, resolve_lane_repo_root

_WORKBENCH_BRIEF_MAX_BYTES: Final[int] = 256 * 1024
_SAFE_BRIEF_CONTROL_BYTES = frozenset({9, 10, 13})
__all__ = ("BriefSnapshot", "VerbError", "_workbench_brief_snapshot", "render")


@dataclass(frozen=True, slots=True)
class BriefSnapshot:
    ref: str
    sha256: str
    byte_length: int
    text: str


class VerbError(Exception):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(f"{code}: {message}")


def render(snapshot: BriefSnapshot) -> str:
    return (
        "\n\n=== WORKBENCH BRIEF SNAPSHOT BEGIN ===\n"
        f"ref: {snapshot.ref}\nsha256: {snapshot.sha256}\nbytes: {snapshot.byte_length}\n"
        "=== WORKBENCH BRIEF CONTENT BEGIN ===\n"
        f"{snapshot.text}\n=== WORKBENCH BRIEF SNAPSHOT END ==="
    )


def _workbench_brief_snapshot(
    brief_ref: str, repository_root: str, dispatch_row: Mapping[str, object] | None, *, has_charter: bool,
) -> BriefSnapshot | None:
    parts = _workbench_brief_parts(brief_ref, has_charter=has_charter)
    if parts is None:
        return None
    snapshot = _brief_snapshot_from_bytes(
        brief_ref,
        _read_workbench_brief_bytes(_repository_root(repository_root), parts, brief_ref),
    )
    _require_prepared_brief_digest(snapshot, dispatch_row)
    return snapshot


def _repository_root(repository_root: str) -> Path:
    app_home = os.environ.get("APP_HOME", "").strip()
    if not repository_root.strip() and not app_home:
        _refuse("lane_worktree_app_home_required", "lane worktree provisioning requires an explicit APP_HOME-derived checkout")
    try:
        return resolve_lane_repo_root(repository_root, app_home)
    except LaneWorktreeRepoRootError as exc:
        raise VerbError(exc.code, str(exc)) from exc


def _workbench_brief_parts(brief_ref: str, *, has_charter: bool) -> tuple[str, ...] | None:
    if has_charter or not brief_ref:
        return None
    path = Path(brief_ref)
    if path.is_absolute() or not path.parts or path.parts[0] != "workbench":
        return None
    if ".." in path.parts:
        _refuse("brief_ref_path_escape", "workbench brief_ref must not contain '..'.")
    return path.parts


def _read_workbench_brief_bytes(root: Path, parts: tuple[str, ...], brief_ref: str) -> bytes:
    directory_fd = _open_brief_directory(root, None, brief_ref)
    try:
        for part in parts[:-1]:
            child_fd = _open_brief_directory(part, directory_fd, brief_ref)
            os.close(directory_fd)
            directory_fd = child_fd
        with os.fdopen(_open_brief_file(parts[-1], directory_fd, brief_ref), "rb") as source:
            return source.read(_WORKBENCH_BRIEF_MAX_BYTES + 1)
    finally:
        os.close(directory_fd)


def _open_brief_directory(path: Path | str, parent_fd: int | None, brief_ref: str) -> int:
    return _open_brief_descriptor(path, parent_fd, brief_ref, directory=True)


def _open_brief_file(name: str, parent_fd: int, brief_ref: str) -> int:
    return _open_brief_descriptor(name, parent_fd, brief_ref, directory=False)


def _open_brief_descriptor(
    path: Path | str, parent_fd: int | None, brief_ref: str, *, directory: bool,
) -> int:
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    directory_flag = getattr(os, "O_DIRECTORY", 0) if directory else 0
    if not nofollow or directory and not directory_flag:
        _refuse("brief_ref_unreadable", "platform cannot safely open workbench briefs.")
    try:
        descriptor = os.open(
            path, os.O_RDONLY | nofollow | directory_flag | (0 if directory else os.O_NONBLOCK), dir_fd=parent_fd,
        )
    except OSError as exc:
        _raise_brief_open_error(exc, brief_ref)
    expected = stat.S_ISDIR if directory else stat.S_ISREG
    if not expected(os.fstat(descriptor).st_mode):
        os.close(descriptor)
        _refuse("brief_not_regular_file", f"brief_ref is not a regular file: {brief_ref}")
    return descriptor


def _raise_brief_open_error(exc: OSError, brief_ref: str) -> Never:
    if exc.errno == errno.ENOENT:
        _refuse("brief_not_found", f"brief_ref does not exist: {brief_ref}")
    if exc.errno in (errno.ELOOP, errno.ENOTDIR):
        _refuse("brief_ref_symlink", f"workbench brief_ref is a symlink: {brief_ref}")
    _refuse("brief_ref_unreadable", f"cannot open brief_ref {brief_ref!r}.")


def _brief_snapshot_from_bytes(brief_ref: str, payload: bytes) -> BriefSnapshot:
    if len(payload) > _WORKBENCH_BRIEF_MAX_BYTES:
        _refuse("brief_too_large", f"brief_ref exceeds {_WORKBENCH_BRIEF_MAX_BYTES} bytes: {brief_ref}")
    try:
        text = payload.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise VerbError("brief_not_utf8", f"brief_ref is not UTF-8: {brief_ref}") from exc
    if any(byte not in _SAFE_BRIEF_CONTROL_BYTES and (byte < 32 or byte == 127) for byte in payload):
        _refuse("brief_contains_control_byte", f"brief_ref contains a control byte unsafe for driver delivery: {brief_ref}")
    return BriefSnapshot(brief_ref, hashlib.sha256(payload).hexdigest(), len(payload), text)


def _require_prepared_brief_digest(snapshot: BriefSnapshot, dispatch_row: Mapping[str, object] | None) -> None:
    if dispatch_row is not None and snapshot.sha256 != str(dispatch_row.get("brief_sha256") or ""):
        _refuse("brief_digest_mismatch", "workbench brief_ref does not match the prepared dispatch brief_sha256.")


def _refuse(code: str, message: str) -> Never:
    raise VerbError(code, message)
