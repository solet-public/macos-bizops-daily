#!/usr/bin/env python3
"""Regression coverage for the shared "battery ran under contention" signal.

Covers ``quality_gates/contention_signal.py`` directly, plus its integration
into ``run_smokes.py``'s battery receipt — the mechanism gates-cluster-A5
(part 1) adds for iss_35dbc06e/342, iss_dfca317c/357, iss_c35292ca/358,
iss_cf3a897d/756, and iss_0ecb55dc/760. See ``contention_signal.py``'s module
docstring for how each issue maps onto what this file checks; the
holder-liveness-specific coverage for 357/358 lives beside the mechanism it
tests, in ``run_smokes_host_lock_smoke.py``.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO_ROOT / "quality_gates"))
import contention_signal  # noqa: E402


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def _dead_pid() -> int:
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait()
    return proc.pid


def _assert_liveness_probe() -> int:
    """``holder_pid_is_alive`` / ``parse_lock_holder_pid`` behave as the
    holder-liveness fix (iss_dfca317c/357) and the concurrent-count filter
    (iss_cf3a897d/756, iss_0ecb55dc/760) both depend on."""
    alive_pid = os.getpid()
    dead_pid = _dead_pid()
    _require(contention_signal.holder_pid_is_alive(alive_pid), "this process's own pid must read alive")
    _require(not contention_signal.holder_pid_is_alive(dead_pid), "an already-exited child's pid must read dead")
    _require(
        contention_signal.parse_lock_holder_pid(f"pid={alive_pid} cwd=/somewhere") == alive_pid,
        "well-formed holder text must parse the pid",
    )
    _require(
        contention_signal.parse_lock_holder_pid("cwd=/somewhere no-pid-token-here") is None,
        "text with no pid= token must parse to None, not a wrong guess",
    )
    return 4


def _assert_receipt_counting_excludes_dead_finished_and_self() -> int:
    """A stale ("running" but crashed), a finished, and self's own receipt
    must all be excluded — only a genuinely live OTHER battery counts."""
    with tempfile.TemporaryDirectory() as temp_dir:
        directory = Path(temp_dir)
        (directory / "live.json").write_text(
            json.dumps({"result": "running", "pid": os.getpid()}), encoding="utf-8"
        )
        (directory / "dead.json").write_text(
            json.dumps({"result": "running", "pid": _dead_pid()}), encoding="utf-8"
        )
        (directory / "finished.json").write_text(
            json.dumps({"result": "passed", "pid": os.getpid()}), encoding="utf-8"
        )
        self_path = directory / "self.json"
        self_path.write_text(json.dumps({"result": "running", "pid": os.getpid()}), encoding="utf-8")

        count = contention_signal.count_concurrent_live_batteries(directory, exclude_path=self_path)
        _require(count == 1, f"expected exactly the live sibling counted, got {count}")
    return 1


def _assert_battery_receipt_carries_contention() -> int:
    """A matched-default-register run's own receipt gets a ``contention`` key
    shaped as the mechanism promises — the "surfaced on the run's own
    result/receipt" half of the brief, checked structurally rather than
    asserted-and-hoped."""
    with tempfile.TemporaryDirectory() as temp_dir:
        directory = Path(temp_dir)
        register = directory / "register.txt"
        register.write_text("quality_gates/tests/run_smokes_git_environment_smoke.py\n", encoding="utf-8")
        receipts = directory / "receipts"
        environment = {
            "SOLET_NAME": "contention-signal-fixture",
            "RUN_SMOKES_BATTERY_RECEIPT_DIR": str(receipts),
        }
        result = subprocess.run(
            [
                str(_REPO_ROOT / ".venv" / "bin" / "python3"),
                "quality_gates/run_smokes.py",
                "--register",
                str(register),
                "--jobs",
                "1",
                "--campaign",
                "contention-signal-fixture",
            ],
            cwd=_REPO_ROOT,
            env={**os.environ, **environment, "RUN_SMOKES_DEFAULT_REGISTER_PATH": str(register)},
            capture_output=True,
            text=True,
            timeout=60,
        )
        _require(result.returncode == 0, f"fixture battery did not pass: {result.stdout}{result.stderr}")
        receipt_files = list(receipts.glob("*.json"))
        _require(len(receipt_files) == 1, f"expected exactly one receipt, got {receipt_files}")
        receipt = json.loads(receipt_files[0].read_text(encoding="utf-8"))
        _require(isinstance(receipt.get("pid"), int), receipt)
        contention = receipt.get("contention")
        _require(isinstance(contention, dict), receipt)
        for key in (
            "measured_at",
            "concurrent_live_batteries_start",
            "concurrent_live_batteries_finish",
            "concurrent_live_batteries_peak",
            "host_lock_wait_seconds",
            "host_lock_contended",
        ):
            _require(key in contention, f"receipt contention block missing {key!r}: {contention}")
        _require(contention["concurrent_live_batteries_start"] == 0, contention)
        _require(contention["host_lock_contended"] is False, contention)
    return 7


_CONCURRENT_BATTERY_RUNNER_SCRIPT = (
    "import sys\n"
    "sys.path.insert(0, {repo_root!r})\n"
    "from pathlib import Path\n"
    "from unittest.mock import patch\n"
    "from quality_gates import run_smokes\n"
    "root = Path({root!r})\n"
    "register = Path({register!r})\n"
    "with patch.object(run_smokes, '_REPO_ROOT', root), "
    "patch.object(run_smokes, '_DEFAULT_REGISTER', register), "
    "patch.object(run_smokes, '_venv_python', return_value=Path(sys.executable)), "
    "patch.object(sys, 'argv', ['run_smokes.py', '--register', str(register), "
    "'--jobs', '1', '--campaign', {campaign!r}]):\n"
    "    raise SystemExit(run_smokes.main())\n"
)


def _launch_candidate_battery(
    *, repo_root: Path, root: Path, register: Path, campaign: str, receipts: Path
) -> subprocess.Popen[str]:
    script = _CONCURRENT_BATTERY_RUNNER_SCRIPT.format(
        repo_root=str(repo_root), root=str(root), register=str(register), campaign=campaign
    )
    environment = {
        **os.environ,
        "SOLET_NAME": f"contention-signal-{campaign}",
        "RUN_SMOKES_BATTERY_RECEIPT_DIR": str(receipts),
    }
    return subprocess.Popen(
        [sys.executable, "-c", script],
        cwd=repo_root,
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


def _wait_for_running_receipt(receipts: Path, *, exclude: set[Path], timeout: float = 10.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        for candidate in receipts.glob("*.json"):
            if candidate in exclude:
                continue
            try:
                loaded = json.loads(candidate.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if loaded.get("result") == "running":
                return
        time.sleep(0.05)
    raise AssertionError(f"no running receipt appeared under {receipts} within {timeout}s")


def _assert_concurrent_subprocess_batteries_are_detected() -> int:
    """Two REAL, independently-running full-battery OS processes: the second
    must see the first as live concurrent contention (iss_cf3a897d/756,
    iss_0ecb55dc/760 — telling "this red ran alongside real fleet load" apart
    from a clean, isolated run). Each battery gets its own disposable
    candidate root so neither reads nor writes the real repository tree.
    """
    with tempfile.TemporaryDirectory() as temp_dir:
        directory = Path(temp_dir)
        receipts = directory / "receipts"

        slow_root = directory / "slow_candidate"
        slow_root.mkdir()
        (slow_root / "fixture.py").write_text("import time\ntime.sleep(2.5)\n", encoding="utf-8")
        slow_register = slow_root / "register.txt"
        slow_register.write_text("fixture.py\n", encoding="utf-8")

        fast_root = directory / "fast_candidate"
        fast_root.mkdir()
        (fast_root / "fixture.py").write_text("pass\n", encoding="utf-8")
        fast_register = fast_root / "register.txt"
        fast_register.write_text("fixture.py\n", encoding="utf-8")

        slow = _launch_candidate_battery(
            repo_root=_REPO_ROOT, root=slow_root, register=slow_register, campaign="slow", receipts=receipts
        )
        try:
            _wait_for_running_receipt(receipts, exclude=set())
            fast = _launch_candidate_battery(
                repo_root=_REPO_ROOT, root=fast_root, register=fast_register, campaign="fast", receipts=receipts
            )
            fast_stdout, fast_stderr = fast.communicate(timeout=30)
            _require(fast.returncode == 0, f"fast battery did not pass: {fast_stdout}{fast_stderr}")
        finally:
            slow_stdout, slow_stderr = slow.communicate(timeout=30)
            _require(slow.returncode == 0, f"slow battery did not pass: {slow_stdout}{slow_stderr}")

        fast_receipt_path = next(
            candidate
            for candidate in receipts.glob("*.json")
            if json.loads(candidate.read_text(encoding="utf-8")).get("campaign") == "fast"
        )
        fast_receipt = json.loads(fast_receipt_path.read_text(encoding="utf-8"))
        contention = fast_receipt["contention"]
        _require(
            contention["concurrent_live_batteries_start"] >= 1,
            f"the fast battery did not observe the still-sleeping slow battery as live "
            f"contention: {contention}",
        )
    return 1


def main() -> None:
    checks = 0
    checks += _assert_liveness_probe()
    checks += _assert_receipt_counting_excludes_dead_finished_and_self()
    checks += _assert_battery_receipt_carries_contention()
    checks += _assert_concurrent_subprocess_batteries_are_detected()
    print(f"contention signal smoke: {checks} checks passed")


if __name__ == "__main__":
    main()
