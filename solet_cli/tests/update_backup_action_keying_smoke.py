"""Update backups are keyed by the planned action's id, never by its list position (iss_42ec593b).

A handler whose planned-action list changes between the first entry and the
re-entry after a crash used to shift the ``<operation_id>.<index>`` keys and
refuse the resume with ``BackupMissingError``.  Legs: a plan that shrinks past
a non-absolute action resumes; the backup a target resolves to is its own, not
a neighbour's; an r56 Manager's index-keyed backup is honoured by its recorded
destination (and never re-captured over the mutated file); a legacy record for
another target, or a corrupt one, still refuses loudly; the key is a bounded
single path segment.
"""

from __future__ import annotations

import sys
from collections.abc import Callable
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from typing import Any, cast

_ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(_ROOT / "solet_cli" / "src"), str(_ROOT / "solet_setup_contracts" / "src")]
from solet_manager import update_runtime_stages as stages  # noqa: E402
from solet_manager.adapter_protocol import PlannedAction  # noqa: E402
from solet_manager.errors import BackupMissingError, StateError  # noqa: E402
from solet_manager.managed_artifact_backup import action_backup_key, backup_root, file_sha256, read_backup, write_backup  # noqa: E402
from solet_manager.paths import ManagerPaths  # noqa: E402

_CHECKS = 0
_INSTANCE = "ins_" + "a" * 32
_UPDATE = "opr_" + "b" * 32
_ROW = "existing.shell_startup"


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


def _action(action_id: str, target: str) -> PlannedAction:
    return PlannedAction(action_id, action_id, "write_file", target, False, "smoke")


def _execution(root: Path) -> Any:
    paths = ManagerPaths(root / "config", root / "state", root / "cache")
    for directory in (paths.config_dir, paths.state_dir, paths.cache_dir):
        directory.mkdir(parents=True, mode=0o700)
    return SimpleNamespace(paths=paths, record=SimpleNamespace(instance_id=_INSTANCE, name="smoke"), operation_id=_UPDATE, _row=lambda _row_id: {"status": "applying"}, _record=lambda *args, **kwargs: None)


def _enter(execution: Any, actions: tuple[PlannedAction, ...]) -> None:
    probe = SimpleNamespace(planned_actions=actions)
    operation = SimpleNamespace(operation_id=_ROW)
    stages.require_backups_on_reentry(execution, cast(Any, operation), cast(Any, probe))
    stages.backup_targets(cast(Any, execution), cast(Any, operation), cast(Any, probe))


def _check_shrinking_plan_resumes(root: Path) -> None:
    execution = _execution(root)
    target = root / "profile.zsh"
    target.write_text("entry bytes\n")
    first = (_action("note", "note-only"), _action("shell-block", str(target)))
    _execution_first_entry(execution, first)
    target.write_text("mutated by the crashed apply\n")
    _enter(execution, (_action("shell-block", str(target)),))
    _check(True, "a re-entry whose plan dropped a leading non-absolute action resumes without BackupMissingError")
    kept = read_backup(execution.paths, _INSTANCE, _UPDATE, action_backup_key(_ROW, "shell-block"))
    _check(kept.destination == str(target) and kept.sha256 is not None, "the backup a target resolves to records that target")
    _check(kept.sha256 == "sha256:" + __import__("hashlib").sha256(b"entry bytes\n").hexdigest(), "the entry-state backup is kept, not re-captured from the mutated file")


def _execution_first_entry(execution: Any, actions: tuple[PlannedAction, ...]) -> None:
    operation = SimpleNamespace(operation_id=_ROW)
    stages.backup_targets(cast(Any, execution), cast(Any, operation), cast(Any, SimpleNamespace(planned_actions=actions)))


def _check_dropped_absolute_action_does_not_alias(root: Path) -> None:
    execution = _execution(root)
    first_target, second_target = root / "a.conf", root / "b.conf"
    first_target.write_text("a entry\n")
    second_target.write_text("b entry\n")
    _execution_first_entry(execution, (_action("write-a", str(first_target)), _action("write-b", str(second_target))))
    _enter(execution, (_action("write-b", str(second_target)),))
    kept = read_backup(execution.paths, _INSTANCE, _UPDATE, action_backup_key(_ROW, "write-b"))
    _check(kept.destination == str(second_target), "a dropped leading absolute action does not hand its backup to the next target")
    _expect(BackupMissingError, lambda: _enter(execution, (_action("write-c", str(root / "c.conf")),)), "an action with no backup of its own is refused, not aliased to a neighbour")


def _check_legacy_layout(root: Path) -> None:
    execution = _execution(root)
    target = root / "legacy.conf"
    target.write_text("r56 entry bytes\n")
    write_backup(execution.paths, _INSTANCE, _UPDATE, f"{_ROW}.3", target)  # what an r56 Manager left behind
    target.write_text("mutated after the r56 crash\n")
    _enter(execution, (_action("write-legacy", str(target)),))
    _check(True, "an r56 index-keyed backup satisfies re-entry by its recorded destination")
    _check(not (backup_root(execution.paths, _INSTANCE, _UPDATE) / action_backup_key(_ROW, "write-legacy")).exists(), "the mutated file is never re-captured over the legacy entry state")
    _check(read_backup(execution.paths, _INSTANCE, _UPDATE, f"{_ROW}.3").sha256 != file_sha256(target), "the legacy record still holds the entry bytes")
    _expect(BackupMissingError, lambda: _enter(execution, (_action("write-other", str(root / "other.conf")),)), "a legacy record for a different target does not cover this one")


def _check_corrupt_legacy_refused(root: Path) -> None:
    execution = _execution(root)
    target = root / "corrupt.conf"
    target.write_text("entry\n")
    write_backup(execution.paths, _INSTANCE, _UPDATE, f"{_ROW}.0", target)
    (backup_root(execution.paths, _INSTANCE, _UPDATE) / f"{_ROW}.0" / "before.bytes").write_bytes(b"tampered")
    _expect(StateError, lambda: _enter(execution, (_action("write-corrupt", str(target)),)), "a corrupt legacy backup fails loudly instead of being skipped")


def _check_key_is_a_bounded_segment() -> None:
    _check(action_backup_key(_ROW, "shell-block") == f"{_ROW}.shell-block", "the key is the operation id and the action id")
    long_key = action_backup_key("x" * 128, "y" * 128)
    _check(len(long_key) <= 255 and "/" not in long_key, "an oversized key folds to a bounded single segment")
    _check(long_key == action_backup_key("x" * 128, "y" * 128), "the folded key is deterministic")
    _expect(StateError, lambda: action_backup_key(_ROW, "../escape"), "an action id outside the closed grammar is refused")


def main() -> int:
    for leg in (_check_shrinking_plan_resumes, _check_dropped_absolute_action_does_not_alias, _check_legacy_layout, _check_corrupt_legacy_refused):
        with TemporaryDirectory() as temporary:
            leg(Path(temporary))
    _check_key_is_a_bounded_segment()
    print(f"update_backup_action_keying_smoke: {_CHECKS} checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
