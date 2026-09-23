"""Operation-scoped backup and the exact ``existing::hydration.restore`` handler (design section 6.5).

Legs: every backup writes ``before.bytes`` and ``before.json`` under Manager
state, an absent destination is recorded as absent, a second backup for the
same artifact is the untouched entry state, restore is byte-exact and
mode-exact, restore of an absent-before artifact removes the file, restore
refuses a diverged destination (``restore_target_diverged``) without writing,
and a corrupt record is refused on read.
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
import sys
from collections.abc import Callable
from pathlib import Path
from tempfile import TemporaryDirectory

_ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(_ROOT / "solet_cli" / "src"), str(_ROOT / "solet_setup_contracts" / "src")]
from solet_manager.errors import BackupUnwritableError, RestoreTargetDivergedError, StateError  # noqa: E402
from solet_manager.managed_artifact_backup import BackupRecord, backup_root, file_sha256, read_backup, restore_artifact, write_backup  # noqa: E402
from solet_manager.paths import ManagerPaths  # noqa: E402

_CHECKS = 0
_INSTANCE = "ins_" + "a" * 32
_OPERATION = "opr_" + "b" * 32


def _check(condition: object, label: str) -> None:
    global _CHECKS
    _CHECKS += 1
    if not condition:
        raise AssertionError(label)


def _expect(kind: type[Exception], action: Callable[[], object], label: str) -> None:
    try:
        action()
    except kind:
        _check(True, label)
    else:
        raise AssertionError(f"{label}: no {kind.__name__} raised")


def _fixture_paths(root: Path) -> ManagerPaths:
    paths = ManagerPaths(root / "config", root / "state", root / "cache")
    for directory in (paths.config_dir, paths.state_dir, paths.cache_dir):
        directory.mkdir(parents=True, mode=0o700)
    return paths


def _check_backup_and_restore(paths: ManagerPaths, destination: Path) -> BackupRecord:
    destination.write_text("operator line\n")
    destination.chmod(0o640)
    before_digest = file_sha256(destination)
    record = write_backup(paths, _INSTANCE, _OPERATION, "shell_startup_block", destination)
    directory = backup_root(paths, _INSTANCE, _OPERATION) / "shell_startup_block"
    _check((directory / "before.bytes").read_bytes() == b"operator line\n", "before.bytes holds the exact prior bytes")
    recorded = (json.loads((directory / "before.json").read_text())["sha256"], record.mode, record.absent)
    _check(recorded == (before_digest, 0o640, False), "before.json records digest and mode")
    _check(stat.S_IMODE(os.stat(directory).st_mode) == 0o700, "backup directory is private")
    written = "operator line\n# BEGIN SOLET fixture v12345678\nsource x\n# END SOLET fixture\n"
    destination.write_text(written)
    after = file_sha256(destination)
    again = write_backup(paths, _INSTANCE, _OPERATION, "shell_startup_block", destination)
    _check((again, (directory / "before.bytes").read_bytes()) == (record, b"operator line\n"), "a second backup keeps the entry state")
    _expect(RestoreTargetDivergedError, lambda: restore_artifact(paths, _INSTANCE, _OPERATION, "shell_startup_block", expected_after_sha256="sha256:" + "0" * 64), "restore over a diverged destination refused")
    _check(destination.read_text() == written, "diverged destination is left untouched")
    destination.write_text("operator later edit\n")
    _expect(RestoreTargetDivergedError, lambda: restore_artifact(paths, _INSTANCE, _OPERATION, "shell_startup_block", expected_after_sha256=after), "restore over an operator edit refused")
    _check(destination.read_text() == "operator later edit\n", "an operator edit after the write is never overwritten")
    destination.write_text(written)
    restored = restore_artifact(paths, _INSTANCE, _OPERATION, "shell_startup_block", expected_after_sha256=after)
    outcome = (destination.read_bytes(), stat.S_IMODE(destination.stat().st_mode), restored.sha256)
    _check(outcome == (b"operator line\n", 0o640, before_digest), "restore is byte-exact and mode-exact")
    _check(hashlib.sha256(b"operator line\n").hexdigest() == before_digest.removeprefix("sha256:"), "digest grammar")
    return record


def _check_absent_and_refusals(root: Path, paths: ManagerPaths, destination: Path, record: BackupRecord) -> None:
    absent = root / "home" / "Library" / "LaunchAgents" / "local.solet.fixture.plist"
    absent.parent.mkdir(parents=True)
    record_absent = write_backup(paths, _INSTANCE, _OPERATION, "instance_launchagent_plist", absent)
    _check((record_absent.absent, record_absent.sha256, record_absent.mode) == (True, None, None), "an absent destination is recorded as absent")
    absent.write_bytes(b"<plist/>\n")
    restore_artifact(paths, _INSTANCE, _OPERATION, "instance_launchagent_plist", expected_after_sha256=file_sha256(absent))
    _check(not absent.exists(), "restoring an absent-before artifact removes the file")
    _check(read_backup(paths, _INSTANCE, _OPERATION, "shell_startup_block") == record, "read-back equals the written record")
    corrupt = backup_root(paths, _INSTANCE, _OPERATION) / "shell_startup_block" / "before.bytes"
    corrupt.write_bytes(b"tampered\n")
    _expect(StateError, lambda: read_backup(paths, _INSTANCE, _OPERATION, "shell_startup_block"), "tampered backup bytes are refused on read")
    symlink = root / "home" / "link"
    symlink.symlink_to(destination)
    _expect(BackupUnwritableError, lambda: write_backup(paths, _INSTANCE, _OPERATION, "linked", symlink), "a symlinked destination cannot be backed up")


def main() -> int:
    with TemporaryDirectory() as temporary:
        root = Path(temporary).resolve()
        paths = _fixture_paths(root)
        destination = root / "home" / ".zshrc"
        destination.parent.mkdir()
        record = _check_backup_and_restore(paths, destination)
        _check_absent_and_refusals(root, paths, destination, record)
    print(f"managed_artifact_backup_smoke OK: {_CHECKS} checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
