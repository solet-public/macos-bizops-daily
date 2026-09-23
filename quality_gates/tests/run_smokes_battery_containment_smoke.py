#!/usr/bin/env python3
"""Regression coverage for full-battery receipts and the fork-rate fuse."""

from __future__ import annotations

import contextlib
import io
import json
import os
import signal
import subprocess
import sys
import tempfile
from pathlib import Path
from unittest.mock import call, patch

_REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO_ROOT))

from quality_gates import run_smokes  # noqa: E402


@contextlib.contextmanager
def _runner_root(root: Path):
    """Run the production runner against a disposable candidate tree."""
    with patch.object(run_smokes, "_REPO_ROOT", root):
        yield


def _call_main(arguments: list[str]) -> tuple[int, str]:
    stderr = io.StringIO()
    with contextlib.redirect_stderr(stderr), patch.object(sys, "argv", arguments):
        result = run_smokes.main()
    return result, stderr.getvalue()


def _assert_duplicate_candidate_receipt_refuses() -> int:
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary) / "candidate"
        root.mkdir()
        fixture = root / "fixture.py"
        fixture.write_text("print('fixture pass')\n", encoding="utf-8")
        register = root / "register.txt"
        register.write_text("fixture.py\n", encoding="utf-8")
        receipt_directory = Path(temporary) / "receipts"
        base_arguments = [
            "run_smokes.py",
            "--register",
            str(register),
            "--campaign",
            "receipt-fixture",
            "--jobs",
            "1",
        ]
        environment = {
            "SOLET_NAME": "run-smokes-containment-fixture",
            "RUN_SMOKES_BATTERY_RECEIPT_DIR": str(receipt_directory),
        }
        with (
            _runner_root(root),
            patch.object(run_smokes, "_DEFAULT_REGISTER", register),
            patch.object(run_smokes, "_venv_python", return_value=Path(sys.executable)),
            patch.dict(os.environ, environment, clear=False),
        ):
            first, first_stderr = _call_main(base_arguments)
            second, second_stderr = _call_main(base_arguments)
            third, third_stderr = _call_main([*base_arguments, "--reauthorize"])

        assert first == 0, first_stderr
        assert second == 2, second_stderr
        assert "already ran" in second_stderr, second_stderr
        assert "prior result=passed" in second_stderr, second_stderr
        assert third == 0, third_stderr
        receipts = list(receipt_directory.glob("*.json"))
        assert len(receipts) == 1, receipts
        receipt = json.loads(receipts[0].read_text(encoding="utf-8"))
        assert receipt["result"] == "passed", receipt
        assert receipt["reauthorized_from"]["result"] == "passed", receipt
    return 7


def _fixture_source(records: Path) -> str:
    return (
        "import os\n"
        "import subprocess\n"
        "import sys\n"
        "child = subprocess.Popen(\n"
        "    [sys.executable, '-c', 'import time; time.sleep(30)'],\n"
        "    stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,\n"
        ")\n"
        f"with open({str(records)!r}, 'a', encoding='utf-8') as handle:\n"
        "    handle.write(f'{os.getpid()} {os.getpgrp()} {child.pid} {os.getpgid(child.pid)}\\n')\n"
        "    handle.flush()\n"
    )


def _kill_group_if_present(process_group: int) -> None:
    try:
        os.killpg(process_group, signal.SIGKILL)
    except (PermissionError, ProcessLookupError):
        return


def _run_rate_fixture(root: Path, entries: list[str]) -> tuple[list[str], list[str], list[str]]:
    with (
        _runner_root(root),
        patch.object(run_smokes, "_MAX_SMOKE_STARTS_PER_WINDOW", 2),
    ):
        skipped, failures, missing, _host_lock_wait_seconds = run_smokes._run_suite(
            Path(sys.executable),
            entries,
            timeout=10,
            jobs=2,
            serial_only=frozenset(),
        )
        return skipped, failures, missing


def _recorded_groups(records: Path) -> list[tuple[int, int, int, int]]:
    return [
        tuple(int(field) for field in row.split())
        for row in records.read_text(encoding="utf-8").splitlines()
    ]


def _assert_groups_terminated(
    rows: list[tuple[int, int, int, int]], cleanup_groups: set[int]
) -> None:
    assert 1 <= len(rows) <= 2, rows
    for parent_pid, parent_group, child_pid, child_group in rows:
        assert parent_pid == parent_group, rows
        assert child_group == parent_group, (
            "fixture descendant escaped the runner-created process group",
            rows,
        )
        try:
            os.killpg(parent_group, 0)
        except (PermissionError, ProcessLookupError):
            continue
        cleanup_groups.add(parent_group)
        raise AssertionError(
            "start-rate abort left a fixture process group alive: "
            f"parent={parent_pid} child={child_pid} group={parent_group}"
        )


def _assert_rate_fuse_aborts_process_groups() -> int:
    """The third start must be refused and kill descendants of starts one/two.

    The fixture parent exits after spawning a sleeping descendant.  That forces
    the pool to schedule the third entry while the first session's child still
    exists, proving the fuse kills the process *group*, not merely the runner
    process whose exit code it can observe.
    """
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary) / "candidate"
        root.mkdir()
        records = root / "groups.txt"
        entries = [f"fixture-{number}.py" for number in range(3)]
        for entry in entries:
            (root / entry).write_text(_fixture_source(records), encoding="utf-8")

        cleanup_groups: set[int] = set()
        try:
            skipped, failures, missing = _run_rate_fixture(root, entries)
            assert not skipped, skipped
            assert not missing, missing
            assert failures, "the start-rate fuse did not fail the battery"
            _assert_groups_terminated(_recorded_groups(records), cleanup_groups)
        finally:
            for process_group in cleanup_groups:
                _kill_group_if_present(process_group)
    return 9


def _assert_recorded_group_probe_ignores_eperm_esrch() -> int:
    """Treat EPERM/ESRCH as gone without signalling an inaccessible group."""
    row = (101, 101, 102, 101)
    for error in (PermissionError(1, "fixture EPERM"), ProcessLookupError(3, "fixture ESRCH")):
        cleanup_groups: set[int] = set()
        with patch.object(os, "killpg", side_effect=error) as killpg:
            _assert_groups_terminated([row], cleanup_groups)
        assert not cleanup_groups, cleanup_groups
        killpg.assert_called_once_with(row[1], 0)
    return 2


def _assert_live_recorded_group_fails_and_is_cleaned_up() -> int:
    """Retain the live-group failure and clean up its recorded group."""
    row = (101, 101, 102, 101)
    cleanup_groups = set()
    with patch.object(os, "killpg") as killpg:
        try:
            _assert_groups_terminated([row], cleanup_groups)
        except AssertionError as error:
            assert "start-rate abort left a fixture process group alive" in str(error), error
        else:
            raise AssertionError("a genuinely live fixture group was accepted as terminated")
        for process_group in cleanup_groups:
            _kill_group_if_present(process_group)

    assert cleanup_groups == {row[1]}, cleanup_groups
    assert killpg.call_args_list == [
        call(row[1], 0),
        call(row[1], signal.SIGKILL),
    ], killpg.call_args_list
    return 2


def _assert_recorded_group_probe_handles_eperm_esrch_and_live_control() -> int:
    """Treat inaccessible groups as gone while retaining live-group detection."""
    return (
        _assert_recorded_group_probe_ignores_eperm_esrch()
        + _assert_live_recorded_group_fails_and_is_cleaned_up()
    )


def _assert_group_cleanup_ignores_eperm_and_esrch() -> int:
    """A cleanup race or reused inaccessible group must not escape the smoke."""
    for error in (PermissionError(1, "fixture EPERM"), ProcessLookupError(3, "fixture ESRCH")):
        with patch.object(os, "killpg", side_effect=error) as killpg:
            _kill_group_if_present(101)
        killpg.assert_called_once_with(101, signal.SIGKILL)
    return 2


def _assert_deleted_index_path_is_not_candidate_source() -> int:
    """An unstaged deletion must not make receipt hashing unreadable."""
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary) / "candidate"
        root.mkdir()
        deleted = root / "deleted.py"
        deleted.write_text("print('removed')\n", encoding="utf-8")
        initialized = subprocess.run(
            ["git", "init", "-q"], cwd=root, capture_output=True, text=True, check=False,
        )
        assert initialized.returncode == 0, initialized.stderr
        indexed = subprocess.run(
            ["git", "add", "deleted.py"], cwd=root, capture_output=True, text=True, check=False,
        )
        assert indexed.returncode == 0, indexed.stderr
        deleted.unlink()
        paths = run_smokes._candidate_paths_for_digest(root)
        assert deleted not in paths, paths
        assert len(run_smokes._candidate_digest(root)) == 64
    return 2


class _TimeoutThenPassProcess:
    """Minimal process fixture for cleanup-error battery containment."""

    def __init__(self, pid: int, *, times_out: bool) -> None:
        self.pid = pid
        self.returncode = 0
        self._times_out = times_out

    def communicate(self, timeout: int | None = None) -> tuple[str, str]:
        if self._times_out:
            self._times_out = False
            raise subprocess.TimeoutExpired(["fixture"], timeout if timeout is not None else 0)
        return "fixture pass\n", ""


def _assert_sigterm_permission_error_contains_one_smoke_failure() -> int:
    """An EPERM cleanup must not prevent later smoke verdicts from reporting."""
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary) / "candidate"
        root.mkdir()
        timeout_entry = "timeout.py"
        succeeding_entry = "after-timeout.py"
        for entry in (timeout_entry, succeeding_entry):
            (root / entry).write_text("# fixture\n", encoding="utf-8")
        processes = {
            timeout_entry: _TimeoutThenPassProcess(101, times_out=True),
            succeeding_entry: _TimeoutThenPassProcess(102, times_out=False),
        }
        permission_denied_calls: list[tuple[int, signal.Signals]] = []

        def _fixture_popen(command: list[str], **_: object) -> _TimeoutThenPassProcess:
            return processes[Path(command[-1]).name]

        def _permission_denied(process_group: int, signum: signal.Signals) -> None:
            permission_denied_calls.append((process_group, signum))
            raise PermissionError(1, "fixture EPERM")

        with (
            _runner_root(root),
            patch.object(run_smokes.subprocess, "Popen", side_effect=_fixture_popen),
            patch.object(run_smokes.os, "killpg", side_effect=_permission_denied),
        ):
            skipped, failures, missing, _host_lock_wait_seconds = run_smokes._run_suite(
                Path(sys.executable),
                [timeout_entry, succeeding_entry],
                timeout=1,
                jobs=1,
                serial_only=frozenset(),
            )

    assert skipped == [], skipped
    assert failures == [timeout_entry], failures
    assert missing == [], missing
    assert permission_denied_calls == [(101, signal.SIGTERM)], permission_denied_calls
    return 4


def main() -> None:
    checks = _assert_duplicate_candidate_receipt_refuses()
    checks += _assert_rate_fuse_aborts_process_groups()
    checks += _assert_recorded_group_probe_handles_eperm_esrch_and_live_control()
    checks += _assert_group_cleanup_ignores_eperm_and_esrch()
    checks += _assert_deleted_index_path_is_not_candidate_source()
    checks += _assert_sigterm_permission_error_contains_one_smoke_failure()
    print(f"run-smokes battery containment smoke: {checks} checks passed")


if __name__ == "__main__":
    main()
