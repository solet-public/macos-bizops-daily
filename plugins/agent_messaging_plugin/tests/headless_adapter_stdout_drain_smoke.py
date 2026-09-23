#!/usr/bin/env python3
"""Red-first regression smoke for the stdout-never-read fix in
``headless_adapter.HeadlessHostDriver.spawn()`` (T1 usage-capture lane,
the 2026-08-05 usage-capture ruling, Ruling 3).

Before this fix, ``spawn()`` piped the child's stdout (``stdout=PIPE``) but
only ever drained stderr — a chatty child (exactly what a real Claude Code
worker's stream-json traffic is) fills the OS pipe buffer and blocks
forever, with nothing on the platform side ever noticing. Scratch-measured
2026-08-05 (workbench plan doc, same lane): a reproduction of the exact
pre-fix pipe/drain shape hung for the full 8s bound before being killed.

This smoke re-derives that same red-first proof against the REAL
``HeadlessHostDriver.spawn()`` (not a standalone repro): monkeypatches the
module's ``_drain_pipe`` to skip its first invocation (spawn()'s call order
is stdout-then-stderr, so the first call is always the stdout drain) —
reproducing the pre-fix bug exactly — confirms the chatty child hangs, then
restores the real function and confirms the same child completes cleanly.
Ruling 3 condition 3 (non-fatal child death) gets its own dedicated check.

Run:
    .venv/bin/python3 \
        plugins/agent_messaging_plugin/tests/headless_adapter_stdout_drain_smoke.py
"""

from __future__ import annotations

import os
import signal
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT / "ananta" / "src"))
sys.path.insert(0, str(REPO_ROOT / "plugins" / "agent_messaging_plugin" / "src"))

import subprocess  # noqa: E402

from agent_messaging_plugin import headless_adapter  # noqa: E402
from agent_messaging_plugin.headless_adapter import HeadlessHostDriver  # noqa: E402

_passed = 0
_failed: list[str] = []


def _check(condition: object, label: str) -> None:
    global _passed
    if condition:
        _passed += 1
        print(f"  PASS  {label}")
    else:
        _failed.append(label)
        print(f"  FAIL  {label}")


_CHATTY_CHILD_SRC = (
    "import sys\n"
    "for _ in range(4000):\n"
    "    sys.stdout.write('x' * 4096)\n"
    "    sys.stdout.flush()\n"
    "sys.stderr.write('done\\n')\n"
    "sys.stderr.flush()\n"
)


def _real_chatty_popen_fn(*_a: Any, **_k: Any) -> subprocess.Popen[str]:
    """Ignores the incoming cmd/env — spawns a real child that writes ~16MB
    to stdout (far past any OS pipe buffer, 64KB-class on macOS/Linux) plus
    a little stderr, mirroring a real worker's stream-json chatter."""
    return subprocess.Popen(  # noqa: S603 -- fixed harmless argv, test-only
        [sys.executable, "-c", _CHATTY_CHILD_SRC],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )


def _executable_stub(tmp_dir: Path) -> str:
    stub = tmp_dir / "fake-claude"
    stub.write_text("#!/bin/sh\nexit 0\n")
    stub.chmod(0o755)
    return str(stub)


def _stub_worker_hook_files(tmp_dir: Path) -> None:
    """R4 Package C (2026-08-10): populate rung 1 (``.claude/hooks/``) with
    a stub for every file the worker-hook resolution ladder requires --
    matching a real dev checkout's own shape, so this fixture's spawn()
    calls reach the popen_fn under test instead of refusing on the ladder."""
    hooks_dir = tmp_dir / ".claude" / "hooks"
    hooks_dir.mkdir(parents=True, exist_ok=True)
    for name in headless_adapter._WORKER_INJECTED_HOOK_FILENAMES:  # noqa: SLF001
        (hooks_dir / name).write_text("#!/usr/bin/env python3\n")


def _configured_driver(tmp_dir: Path, *, popen_fn: Any) -> HeadlessHostDriver:
    mcp_config = tmp_dir / ".mcp.json"
    mcp_config.write_text("{}")
    _stub_worker_hook_files(tmp_dir)
    return HeadlessHostDriver(
        claude_bin=_executable_stub(tmp_dir),
        solet_name="testhom",
        permission_mode="bypassPermissions",
        mcp_config_path=mcp_config,
        cwd=tmp_dir,
        popen_fn=popen_fn,
    )


def test_spawn_call_order_is_stdout_then_stderr() -> None:
    """Precondition for the monkeypatch trick below: confirm by source
    inspection that spawn() drains stdout (now via
    ``_drain_stdout_with_init_capture``, slice D's init-event capture
    riding the same continuous drain) strictly before draining stderr via
    ``_drain_pipe``, so patching the stdout-side function really does
    target stdout, not stderr."""
    import inspect

    source = inspect.getsource(HeadlessHostDriver.spawn)
    stdout_pos = source.find("_drain_stdout_with_init_capture(proc.stdout")
    stderr_pos = source.find("_drain_pipe(proc.stderr)")
    _check(
        stdout_pos != -1 and stderr_pos != -1 and stdout_pos < stderr_pos,
        "spawn() drains stdout (via _drain_stdout_with_init_capture) before "
        "draining stderr (via _drain_pipe) -- the fix drains both, in that order",
    )


def test_red_undrained_stdout_hangs_the_chatty_child() -> None:
    """RED-FIRST PROOF: force the pre-fix shape (stdout never drained) and
    confirm the chatty child is still blocked (alive, not exited) after a
    bound well past what a drained child needs to finish. Since slice D
    routed stdout through ``_drain_stdout_with_init_capture`` (a distinct
    function from the still-stderr-only ``_drain_pipe``), the pre-fix shape
    is reproduced by replacing THAT function with a no-op -- not by
    skipping ``_drain_pipe``'s first call (stderr no longer shares a call
    counter with stdout)."""
    original = headless_adapter._drain_stdout_with_init_capture

    def _never_drain(_pipe: Any, *, agent_instance_id: str) -> None:  # noqa: ARG001
        return

    headless_adapter._drain_stdout_with_init_capture = _never_drain
    try:
        with tempfile.TemporaryDirectory() as tmp:
            driver = _configured_driver(Path(tmp), popen_fn=_real_chatty_popen_fn)
            host_ref = driver.spawn({"agent_instance_id": "agi-red-chatty-1"})
            time.sleep(2.0)
            _check(
                driver.alive(host_ref),
                "RED: with stdout drain skipped, the chatty child is still "
                "blocked (alive) after 2s -- reproduces the pre-fix hang",
            )
            driver.terminate(host_ref, grace_seconds=2)
            time.sleep(0.2)
            _check(not driver.alive(host_ref), "cleanup: forced-hung child killed and reaped")
    finally:
        headless_adapter._drain_stdout_with_init_capture = original


def test_green_real_drain_lets_the_chatty_child_exit() -> None:
    """GREEN: with the real (fixed) drain wired on both streams, the
    same chatty child finishes writing and exits on its own well within a
    generous bound -- the fix actually resolves the RED case above."""
    with tempfile.TemporaryDirectory() as tmp:
        driver = _configured_driver(Path(tmp), popen_fn=_real_chatty_popen_fn)
        host_ref = driver.spawn({"agent_instance_id": "agi-green-chatty-1"})
        deadline = time.monotonic() + 10.0
        exited = False
        while time.monotonic() < deadline:
            if not driver.alive(host_ref):
                exited = True
                break
            time.sleep(0.1)
        _check(
            exited,
            "GREEN: with both streams drained, the chatty child exits on "
            "its own within 10s -- no hang with the real fix in place",
        )
        if not exited:
            driver.terminate(host_ref, grace_seconds=2)


def test_child_death_mid_write_does_not_propagate() -> None:
    """Ruling 3 condition 3: the drain tolerates child death without
    propagating. Kill the chatty child mid-write (well before it would
    finish on its own) and confirm no exception escapes the drain threads
    into the test's own thread (threading.excepthook capture) and the
    driver still correctly reports the process as dead afterward."""
    thread_errors: list[BaseException] = []
    original_hook = threading.excepthook

    def _capture_hook(args: threading.ExceptHookArgs) -> None:
        thread_errors.append(args.exc_value)

    threading.excepthook = _capture_hook
    try:
        with tempfile.TemporaryDirectory() as tmp:
            driver = _configured_driver(Path(tmp), popen_fn=_real_chatty_popen_fn)
            host_ref = driver.spawn({"agent_instance_id": "agi-killed-midwrite-1"})
            time.sleep(0.3)  # let it get partway into writing
            os.kill(int(host_ref), signal.SIGKILL)
            deadline = time.monotonic() + 5.0
            while driver.alive(host_ref) and time.monotonic() < deadline:
                time.sleep(0.05)
            _check(not driver.alive(host_ref), "killed-mid-write child is reaped as dead")
            time.sleep(0.2)  # let drain threads observe EOF/close and exit
            _check(
                thread_errors == [],
                "no exception escaped the drain threads when the child died mid-write "
                "-- non-fatal by design",
            )
    finally:
        threading.excepthook = original_hook


def main() -> int:
    test_spawn_call_order_is_stdout_then_stderr()
    test_red_undrained_stdout_hangs_the_chatty_child()
    test_green_real_drain_lets_the_chatty_child_exit()
    test_child_death_mid_write_does_not_propagate()

    print()
    print(f"PASSED: {_passed}")
    print(f"FAILED: {len(_failed)}")
    for label in _failed:
        print(f"  - {label}")
    return 1 if _failed else 0


if __name__ == "__main__":
    sys.exit(main())
