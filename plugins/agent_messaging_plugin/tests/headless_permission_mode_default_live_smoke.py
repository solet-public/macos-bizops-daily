#!/usr/bin/env python3
"""Live-``claude`` behavioral smoke for the D2 headless-permission-mode
default flip (follow-up #1, appended to the D2 dispatch brief 2026-08-04).

Pins the exact bug this lane fixed: an unattended (headless/tmux) spawn
running Claude Code's own ``"default"`` interactive-approval permission mode
has EMPTY effective grants — there is no human to approve a tool-use prompt,
so a mutating tool call is denied, not paused. ``plugin.yaml``'s
``headless_permission_mode`` shipped default was flipped from ``"default"``
to ``"bypassPermissions"`` to fix this; this smoke drives the REAL ``claude``
CLI under both values (same ``--setting-sources project`` isolation the
spawned drivers use) and asserts the RED (denied under the old default) and
GREEN (succeeds under the new default) shapes are what actually happens —
not a theoretical claim, not an argv-string check.

MEASURED once manually before this file was written (2026-08-04, scratch
dir ``/tmp/d2_perm_mode_probe``, since deleted): ``--permission-mode
default`` returned a ``permission_denials`` entry for the ``Write`` tool
and created no file; ``--permission-mode bypassPermissions`` returned an
empty ``permission_denials`` list and created the file with the requested
content. This smoke reproduces that exact probe as a re-runnable artifact.

Costs a real inference call each run (non-trivial token spend + wall time)
-- env-gated behind ``HEADLESS_PERMISSION_MODE_LIVE_SMOKE=1``, never part of
the gate-eligible smoke suite (not registered in ``gate_smokes.txt``, same
posture as the ``actr_memory_plugin`` live-Postgres smokes it mirrors the
opt-in convention from).

Run::

    HEADLESS_PERMISSION_MODE_LIVE_SMOKE=1 \\
      .venv/bin/python3 plugins/agent_messaging_plugin/tests/headless_permission_mode_default_live_smoke.py
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

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


def _run_write_probe(*, permission_mode: str, tmp_dir: Path, filename: str) -> dict[str, object]:
    claude_bin = shutil.which("claude")
    assert claude_bin is not None, "claude binary not found on PATH -- cannot run this live smoke"
    prompt = (
        f"Use the Write tool to create a file named {filename} in the current "
        "directory with the content OK. Do nothing else."
    )
    result = subprocess.run(  # noqa: S603 -- fixed argv, live smoke, opt-in only
        [
            claude_bin, "-p", prompt,
            "--permission-mode", permission_mode,
            "--setting-sources", "project",
            "--output-format", "json",
        ],
        capture_output=True, text=True, timeout=60, cwd=str(tmp_dir),
    )
    _check(result.returncode == 0, f"claude -p exits 0 under --permission-mode {permission_mode}")
    return json.loads(result.stdout)


def test_default_mode_denies_the_mutating_write() -> None:
    """RED: the OLD shipped default. A real claude spawn under
    --permission-mode default, with no human present to approve, must
    deny the Write tool -- proving the bug this lane fixed was real, not
    theoretical."""
    with tempfile.TemporaryDirectory() as tmp:
        tmp_dir = Path(tmp)
        payload = _run_write_probe(
            permission_mode="default", tmp_dir=tmp_dir, filename="probe_default.txt",
        )
        denials = payload.get("permission_denials") or []
        _check(
            len(denials) > 0 and any(d.get("tool_name") == "Write" for d in denials),
            "RED: --permission-mode default denies the Write tool call "
            f"(permission_denials={denials!r})",
        )
        _check(
            not (tmp_dir / "probe_default.txt").exists(),
            "RED: no file was actually created under the denied permission mode",
        )


def test_bypass_permissions_mode_allows_the_mutating_write() -> None:
    """GREEN: the NEW shipped default (plugin.yaml's flipped
    headless_permission_mode). The same write, same isolation, succeeds --
    proving the fix actually closes the gap, not just changes a string."""
    with tempfile.TemporaryDirectory() as tmp:
        tmp_dir = Path(tmp)
        payload = _run_write_probe(
            permission_mode="bypassPermissions", tmp_dir=tmp_dir, filename="probe_bypass.txt",
        )
        denials = payload.get("permission_denials") or []
        _check(
            denials == [],
            f"GREEN: --permission-mode bypassPermissions denies nothing (got {denials!r})",
        )
        written = tmp_dir / "probe_bypass.txt"
        _check(written.exists(), "GREEN: the file was actually created")
        if written.exists():
            _check(
                written.read_text().strip() == "OK",
                f"GREEN: the file contains the requested content (got {written.read_text()!r})",
            )


def main() -> int:
    if os.environ.get("HEADLESS_PERMISSION_MODE_LIVE_SMOKE") != "1":
        print(
            "  SKIP  set HEADLESS_PERMISSION_MODE_LIVE_SMOKE=1 to run; makes "
            "real inference calls (non-trivial cost + wall time).",
        )
        return 0

    test_default_mode_denies_the_mutating_write()
    test_bypass_permissions_mode_allows_the_mutating_write()

    print()
    print(f"PASSED: {_passed}")
    print(f"FAILED: {len(_failed)}")
    for label in _failed:
        print(f"  - {label}")
    return 1 if _failed else 0


if __name__ == "__main__":
    sys.exit(main())
