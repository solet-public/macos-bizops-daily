"""Operation-scoped backup writer and the exact ``existing::hydration.restore`` handler.

Every managed-file write in Step 5 is preceded by a backup and paired with one
restore handler, unconditionally (design section 6.5).  The backup lives under
Manager state — private, outside every protected session/history path and
outside the target — and is journaled by the caller together with the
post-write digest.  The restore handler refuses to write over a destination
whose bytes are not the journaled post-write bytes: restoring over an
operator's later edit would be a silent choice of side.
"""

from __future__ import annotations

import hashlib
import os
import re
import stat
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import cast

from .errors import BackupUnwritableError, RestoreTargetDivergedError, StateError
from .models import JsonValue
from .paths import ManagerPaths
from .private_json import load_json_object
from .state_io import atomic_write_json, ensure_private_directory
from .transaction import utc_now

__all__ = ["BackupRecord", "action_backup_key", "backup_root", "file_sha256", "legacy_indexed_backup_covers", "read_backup", "restore_artifact", "write_backup"]

_ACTION_ID = re.compile(r"^[a-z][a-z0-9_.-]{1,127}$")
_LEGACY_INDEX = re.compile(r"^[0-9]+$")
_MAX_SEGMENT = 200
_BEFORE_BYTES = "before.bytes"
_BEFORE_JSON = "before.json"
_RECORD_KEYS = frozenset({"artifact_id", "destination", "mode", "sha256", "captured_at", "absent"})


@dataclass(frozen=True, slots=True)
class BackupRecord:
    artifact_id: str
    destination: str
    mode: int | None
    sha256: str | None
    captured_at: str
    absent: bool

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "artifact_id": self.artifact_id,
            "destination": self.destination,
            "mode": self.mode,
            "sha256": self.sha256,
            "captured_at": self.captured_at,
            "absent": self.absent,
        }


def backup_root(paths: ManagerPaths, instance_id: str, operation_id: str) -> Path:
    journal = paths.operation_path(instance_id, operation_id)
    return journal.parent / operation_id / "backups"


def action_backup_key(operation_id: str, action_id: str) -> str:
    """The single path segment that keys a planned action's backup: ``<operation_id>.<action_id>``.

    Action ids are unique within one probe result (the adapter protocol refuses
    duplicates) and match the closed id grammar, so the key is already a safe
    segment.  Unlike a list position it survives a re-plan after a crash.  A
    combined key past the 255-byte segment limit is folded to a digest.
    """
    if _ACTION_ID.fullmatch(action_id) is None:
        raise StateError(f"planned action id is not a safe backup key: {action_id!r}")
    key = f"{operation_id}.{action_id}"
    if len(key) <= _MAX_SEGMENT:
        return key
    return f"{operation_id[:64]}.{hashlib.sha256(key.encode()).hexdigest()[:32]}"


def legacy_indexed_backup_covers(paths: ManagerPaths, instance_id: str, operation_id: str, row_id: str, target: Path) -> bool:
    """Whether an r56-era ``<row_id>.<index>`` backup (under ``operation_id``) already records ``target``'s entry state.

    An operation begun by an r56 Manager keyed its backups by list position.
    Such a record is matched by its recorded destination, never by index, so a
    shifted plan cannot alias it onto another target; every legacy record is
    read through the closed-shape check and a corrupt one fails loudly.
    """
    root = backup_root(paths, instance_id, operation_id)
    if not root.is_dir():
        return False
    for directory in sorted(root.glob(f"{row_id}.*")):
        if _LEGACY_INDEX.fullmatch(directory.name.removeprefix(f"{row_id}.")) is None:
            continue
        if read_backup(paths, instance_id, operation_id, directory.name).destination == str(target):
            return True
    return False


def file_sha256(path: Path) -> str | None:
    """``sha256:<hex>`` of a regular file's bytes, or ``None`` when it is absent."""
    try:
        info = path.lstat()
    except FileNotFoundError:
        return None
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise StateError(f"managed artifact destination is not a regular file: {path}")
    return f"sha256:{hashlib.sha256(path.read_bytes()).hexdigest()}"


def write_backup(paths: ManagerPaths, instance_id: str, operation_id: str, artifact_id: str, destination: Path) -> BackupRecord:
    """Capture the destination's exact prior bytes before the first byte is written.

    Idempotent per operation and artifact: a backup that already exists is the
    entry state of the stage and is returned untouched, never overwritten, so a
    re-entry after a partial stage still restores to the true entry bytes.
    """
    directory = backup_root(paths, instance_id, operation_id) / artifact_id
    record_path = directory / _BEFORE_JSON
    if record_path.exists():
        return read_backup(paths, instance_id, operation_id, artifact_id)
    try:
        ensure_private_directory(directory)
        info = _lstat(destination)
        if info is None:
            record = BackupRecord(artifact_id, str(destination), None, None, utc_now(), True)
            _write_bytes(directory / _BEFORE_BYTES, b"")
        else:
            if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
                raise BackupUnwritableError(f"managed artifact destination is not a regular file: {destination}")
            content = destination.read_bytes()
            record = BackupRecord(artifact_id, str(destination), stat.S_IMODE(info.st_mode), f"sha256:{hashlib.sha256(content).hexdigest()}", utc_now(), False)
            _write_bytes(directory / _BEFORE_BYTES, content)
        atomic_write_json(record_path, record.to_dict())
    except OSError as exc:
        raise BackupUnwritableError(f"backup for {artifact_id} could not be written: {exc}") from exc
    readback = read_backup(paths, instance_id, operation_id, artifact_id)
    if readback != record:
        raise BackupUnwritableError(f"backup for {artifact_id} did not read back")
    return record


def read_backup(paths: ManagerPaths, instance_id: str, operation_id: str, artifact_id: str) -> BackupRecord:
    directory = backup_root(paths, instance_id, operation_id) / artifact_id
    raw = load_json_object(directory / _BEFORE_JSON)
    if raw is None or frozenset(raw) != _RECORD_KEYS:
        raise StateError(f"backup record for {artifact_id} does not match the closed shape")
    mode, digest, absent = _consistent_record(raw, artifact_id)
    content = (directory / _BEFORE_BYTES).read_bytes()
    if not absent and f"sha256:{hashlib.sha256(content).hexdigest()}" != digest:
        raise StateError(f"backup bytes for {artifact_id} do not match their record")
    return BackupRecord(
        str(raw["artifact_id"]),
        str(raw["destination"]),
        mode,
        digest,
        str(raw["captured_at"]),
        absent,
    )


def _consistent_record(raw: dict[str, JsonValue], artifact_id: str) -> tuple[int | None, str | None, bool]:
    mode = raw["mode"]
    digest = raw["sha256"]
    absent = raw["absent"]
    if not isinstance(absent, bool) or (absent != (mode is None)) or (absent != (digest is None)):
        raise StateError(f"backup record for {artifact_id} is internally inconsistent")
    if mode is not None and (isinstance(mode, bool) or not isinstance(mode, int)):
        raise StateError(f"backup record for {artifact_id} has an invalid mode")
    if digest is not None and not isinstance(digest, str):
        raise StateError(f"backup record for {artifact_id} has an invalid digest")
    return mode, digest, absent


def restore_artifact(
    paths: ManagerPaths,
    instance_id: str,
    operation_id: str,
    artifact_id: str,
    *,
    expected_after_sha256: str | None,
) -> BackupRecord:
    """``existing::hydration.restore``: put the journaled entry bytes back, byte-exact.

    Preconditions: a backup record exists for the artifact and the caller
    supplies the journaled post-write digest.  The destination must currently
    hold exactly those post-write bytes (an absent file corresponds to a
    ``None`` digest); anything else is ``restore_target_diverged`` and nothing
    is written.  After the restore the destination is re-digested and must
    equal the record.
    """
    record = read_backup(paths, instance_id, operation_id, artifact_id)
    destination = Path(record.destination)
    current = file_sha256(destination)
    if current != expected_after_sha256:
        raise RestoreTargetDivergedError(
            f"destination {destination} no longer holds the journaled post-write bytes",
            repair="Inspect the destination; the operator changed it after the update wrote it.",
        )
    directory = backup_root(paths, instance_id, operation_id) / artifact_id
    if record.absent:
        if destination.exists():
            destination.unlink()
            _fsync_directory(destination.parent)
        return record
    content = (directory / _BEFORE_BYTES).read_bytes()
    mode = cast(int, record.mode)
    temporary = destination.parent / f".{destination.name}.{uuid.uuid4().hex}.restore"
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, mode)
        os.replace(temporary, destination)
        os.chmod(destination, mode)
        _fsync_directory(destination.parent)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
    if file_sha256(destination) != record.sha256:
        raise StateError(f"restored destination {destination} does not digest to its backup record")
    return record


def _lstat(path: Path) -> os.stat_result | None:
    try:
        return path.lstat()
    except FileNotFoundError:
        return None


def _write_bytes(path: Path, content: bytes) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "wb") as handle:
        handle.write(content)
        handle.flush()
        os.fsync(handle.fileno())
    _fsync_directory(path.parent)


def _fsync_directory(directory: Path) -> None:
    descriptor = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
