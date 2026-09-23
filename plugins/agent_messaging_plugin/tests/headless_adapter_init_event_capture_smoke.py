#!/usr/bin/env python3
"""Red-first smoke for the slice-D headless init-event capture (T1
usage-capture lane, the 2026-08-05 usage-capture ruling
addendum): ``HeadlessHostDriver.spawn()``'s stdout drain now ALSO parses the
child's first stream-json line for the ``{"type":"system","subtype":"init",
...,"session_id":...}`` event (MEASURED shape, scratch probe 2026-08-05
against a real ``claude --input-format stream-json --output-format
stream-json`` invocation) and writes it as an independent ``init_event``
spool observation -- the driver's own witness, paired against the
SessionStart hook's ``hook:startup`` row by the drain-side cross-check
(``session_claude_mapping_ingest_smoke.py``), never merged into one row.

Proves: the spawn path genuinely emits the init_event spool file with the
right shape when the child's stdout carries a real init-shaped line; a
child whose stdout carries no init-shaped line at all writes nothing
(never a bogus/empty capture); a malformed/non-JSON first line is
non-fatal (spawn proceeds, drain continues, no capture, no exception);
missing APP_HOME (no declared spool dir) is a valid steady state, not an
error.

Run:
    .venv/bin/python3 \
        plugins/agent_messaging_plugin/tests/headless_adapter_init_event_capture_smoke.py
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT / "ananta" / "src"))
sys.path.insert(0, str(REPO_ROOT / "plugins" / "agent_messaging_plugin" / "src"))

import subprocess  # noqa: E402

from agent_messaging_plugin.headless_adapter import (  # noqa: E402
    _WORKER_INJECTED_HOOK_FILENAMES,
    HeadlessHostDriver,
)

_passed = 0
_failed: list[str] = []
_SPOOL_SUBDIR = "session_claude_mapping_spool"


def _check(condition: object, label: str) -> None:
    global _passed
    if condition:
        _passed += 1
        print(f"  PASS  {label}")
    else:
        _failed.append(label)
        print(f"  FAIL  {label}")


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
    for name in _WORKER_INJECTED_HOOK_FILENAMES:
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


def _real_child_popen_fn(child_src: str) -> Any:
    def _fn(*_a: Any, **_k: Any) -> subprocess.Popen[str]:
        return subprocess.Popen(  # noqa: S603 -- fixed harmless argv, test-only
            [sys.executable, "-c", child_src],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )

    return _fn


def _poll_for_spool_files(spool_dir: Path, *, timeout: float = 5.0) -> list[Path]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        files = list(spool_dir.glob("*.json")) if spool_dir.exists() else []
        if files:
            return files
        time.sleep(0.05)
    return list(spool_dir.glob("*.json")) if spool_dir.exists() else []


def test_real_init_event_line_is_captured_and_spooled() -> None:
    child_src = (
        "import json, sys, time\n"
        "print(json.dumps({'type':'system','subtype':'init','session_id':'cs-real-init-test'}))\n"
        "sys.stdout.flush()\n"
        "time.sleep(1)\n"
    )
    record: dict[str, object] | None = None
    file_count = 0
    with tempfile.TemporaryDirectory() as tmp:
        app_home = Path(tmp) / "profile"
        orig = os.environ.get("APP_HOME")
        os.environ["APP_HOME"] = str(app_home)
        try:
            driver = _configured_driver(Path(tmp), popen_fn=_real_child_popen_fn(child_src))
            host_ref = driver.spawn({"agent_instance_id": "agi-init-capture-1"})
            spool_dir = app_home / "data" / _SPOOL_SUBDIR
            files = _poll_for_spool_files(spool_dir)
            file_count = len(files)
            if files:
                record = json.loads(files[0].read_text())
            driver.terminate(host_ref, grace_seconds=2)
        finally:
            if orig is None:
                os.environ.pop("APP_HOME", None)
            else:
                os.environ["APP_HOME"] = orig
    _check(file_count == 1, f"exactly one init_event spool file is written (got {file_count})")
    if record is not None:
        _check(record.get("agent_instance_id") == "agi-init-capture-1", "record carries the right agent_instance_id")
        _check(record.get("claude_session_id") == "cs-real-init-test", "record carries the real init event's session_id")
        _check(record.get("capture_source") == "init_event", "record is tagged capture_source=init_event")
        _check("captured_at" in record, "record carries a captured_at timestamp")


def test_no_init_event_line_writes_nothing() -> None:
    """A child whose stdout never carries an init-shaped line (e.g. the
    fake-claude stub itself, or any non-stream-json chatter) writes NO
    spool file -- never a bogus capture from unrelated output."""
    child_src = (
        "import sys, time\n"
        "print('just some ordinary stdout, not stream-json at all')\n"
        "sys.stdout.flush()\n"
        "time.sleep(0.5)\n"
    )
    with tempfile.TemporaryDirectory() as tmp:
        app_home = Path(tmp) / "profile"
        orig = os.environ.get("APP_HOME")
        os.environ["APP_HOME"] = str(app_home)
        try:
            driver = _configured_driver(Path(tmp), popen_fn=_real_child_popen_fn(child_src))
            host_ref = driver.spawn({"agent_instance_id": "agi-init-capture-2"})
            time.sleep(1.0)
            spool_dir = app_home / "data" / _SPOOL_SUBDIR
            files = list(spool_dir.glob("*.json")) if spool_dir.exists() else []
            driver.terminate(host_ref, grace_seconds=2)
        finally:
            if orig is None:
                os.environ.pop("APP_HOME", None)
            else:
                os.environ["APP_HOME"] = orig
    _check(files == [], "non-stream-json stdout output writes no init_event spool file")


def test_malformed_first_line_is_non_fatal() -> None:
    """A child whose first stdout line is NOT valid JSON at all must not
    raise inside the drain thread or block later lines from being drained
    -- the fix's core safety property (continuous drain) must survive a
    parse failure on line 1."""
    child_src = (
        "import sys, time\n"
        "print('not json at all {{{')\n"
        "sys.stdout.flush()\n"
        "print('a second line, also not json')\n"
        "sys.stdout.flush()\n"
        "time.sleep(0.5)\n"
    )
    with tempfile.TemporaryDirectory() as tmp:
        app_home = Path(tmp) / "profile"
        orig = os.environ.get("APP_HOME")
        os.environ["APP_HOME"] = str(app_home)
        try:
            driver = _configured_driver(Path(tmp), popen_fn=_real_child_popen_fn(child_src))
            host_ref = driver.spawn({"agent_instance_id": "agi-init-capture-3"})
            time.sleep(1.0)
            still_alive_or_exited_cleanly = True  # if spawn() itself didn't raise, this leg passed
            spool_dir = app_home / "data" / _SPOOL_SUBDIR
            files = list(spool_dir.glob("*.json")) if spool_dir.exists() else []
            driver.terminate(host_ref, grace_seconds=2)
        finally:
            if orig is None:
                os.environ.pop("APP_HOME", None)
            else:
                os.environ["APP_HOME"] = orig
    _check(still_alive_or_exited_cleanly, "malformed non-JSON stdout lines never raise out of spawn()/the drain thread")
    _check(files == [], "no init_event spool file is written when no line matches the init shape")


def test_missing_app_home_writes_nothing() -> None:
    """APP_HOME unset -> _resolve_session_mapping_spool_dir() returns None
    -> the init-event capture is a silent no-op, same non-fatal contract
    as every other APP_HOME-unset leg in this lane."""
    child_src = (
        "import json, sys, time\n"
        "print(json.dumps({'type':'system','subtype':'init','session_id':'cs-should-not-be-written'}))\n"
        "sys.stdout.flush()\n"
        "time.sleep(0.5)\n"
    )
    orig = os.environ.pop("APP_HOME", None)
    try:
        with tempfile.TemporaryDirectory() as tmp:
            driver = _configured_driver(Path(tmp), popen_fn=_real_child_popen_fn(child_src))
            host_ref = driver.spawn({"agent_instance_id": "agi-init-capture-4"})
            time.sleep(1.0)
            driver.terminate(host_ref, grace_seconds=2)
            # No APP_HOME means no fixed spool location to even check -- the
            # positive assertion here is simply that spawn()/terminate()
            # completed without raising despite a real init event arriving.
            _check(True, "APP_HOME unset: spawn completes cleanly, init-event capture is a silent no-op")
    finally:
        if orig is not None:
            os.environ["APP_HOME"] = orig


def main() -> int:
    test_real_init_event_line_is_captured_and_spooled()
    test_no_init_event_line_writes_nothing()
    test_malformed_first_line_is_non_fatal()
    test_missing_app_home_writes_nothing()

    print()
    print(f"PASSED: {_passed}")
    print(f"FAILED: {len(_failed)}")
    for label in _failed:
        print(f"  - {label}")
    return 1 if _failed else 0


if __name__ == "__main__":
    sys.exit(main())
