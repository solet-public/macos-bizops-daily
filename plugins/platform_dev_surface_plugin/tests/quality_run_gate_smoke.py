#!/usr/bin/env python3
"""quality_service.run_gate acceptance smoke (no pytest, offline).

Covers B3 §5's ``quality_run_gate_smoke`` row: run_gate is allowlist-only, an
unknown / coherence-only name is a typed rejection, the server-side argv is
fixed (no caller flags), output is bounded with an explicit truncation marker
(no silent cap), and a real gate runs end-to-end through the bounded
subprocess path.

RED-FIRST security pin: if the name-allowlist guard in
``QualityOperations.run_gate`` were removed (accept any name), the
``run_gate('___bogus___')`` assertion below would stop raising and this smoke
would fail. The allowlist IS the security boundary.

Run from repo root:
    .venv/bin/python3 plugins/platform_dev_surface_plugin/tests/quality_run_gate_smoke.py
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[3]
# app_home anchor for locate_repo_root: <worktree>/profile (its parent is the
# worktree root == _REPO_ROOT). Mirrors the runtime APP_HOME anchor.
_APP_HOME = _REPO_ROOT / "profile"
sys.path.insert(0, str(_REPO_ROOT / "ananta" / "src"))
sys.path.insert(0, str(_REPO_ROOT / "plugins" / "platform_dev_surface_plugin" / "src"))

from platform_dev_surface_plugin.bounded_subprocess import run_bounded  # noqa: E402
from platform_dev_surface_plugin.quality import gate_registry  # noqa: E402
from platform_dev_surface_plugin.quality.operations import (  # noqa: E402
    QualityGateError,
    QualityOperations,
)
from platform_dev_surface_plugin.repo_root import locate_repo_root  # noqa: E402

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


def _test_registry_allowlist() -> None:
    _check(
        gate_registry.runnable_gate("code_quality") is not None,
        "code_quality is directly runnable",
    )
    _check(
        gate_registry.runnable_gate("___bogus___") is None,
        "unknown gate name resolves to None (rejected)",
    )
    # The coherence trio must run via the aggregate, not directly.
    _check(
        gate_registry.runnable_gate("god_class") is None,
        "god_class is NOT directly runnable (coherence → via code_quality)",
    )


def _test_argv_is_server_fixed() -> None:
    repo_root = locate_repo_root(_APP_HOME)
    venv = repo_root / ".venv" / "bin" / "python3"
    warn = gate_registry.runnable_gate("wint2_driver_import")
    assert warn is not None
    argv = gate_registry.build_gate_argv(warn, repo_root, venv)
    _check(argv[0] == str(venv), "argv[0] is the repo venv interpreter")
    _check(
        argv[1].endswith("quality_gates/wint2_driver_import_check.py"),
        "argv[1] is the fixed gate script path",
    )
    _check("--allowlist" in argv, "argv carries the server-side --allowlist")
    _check("--warn-only" in argv, "warn-mode gate carries --warn-only server-side")
    # No caller-controllable free-form tokens: every entry is a known flag,
    # the interpreter, the script, or an in-repo allowlist path.
    allowed_flags = {"--allowlist", "--warn-only"}
    stray = [
        tok
        for tok in argv[2:]
        if tok not in allowed_flags and not tok.startswith(str(repo_root))
    ]
    _check(not stray, f"no stray/free-form argv tokens (found {stray})")


def _test_run_gate_rejects_unknown() -> None:
    ops = QualityOperations(locate_repo_root(_APP_HOME), os.environ["SOLET_NAME"])
    try:
        ops.run_gate("___bogus___")
    except QualityGateError:
        _check(True, "run_gate('___bogus___') raises QualityGateError (allowlist pin)")
        return
    _check(False, "run_gate('___bogus___') raises QualityGateError (allowlist pin)")


def _test_bounded_output() -> None:
    repo_root = locate_repo_root(_APP_HOME)
    venv = repo_root / ".venv" / "bin" / "python3"
    # 50k chars of output, capped to 1000 → truncated, tail kept, true total reported.
    result = run_bounded(
        [str(venv), "-c", "print('B' * 50000)"],
        cwd=repo_root,
        timeout=30,
        max_output_chars=1000,
    )
    _check(result.truncated, "oversized output is marked truncated=True")
    _check(len(result.output) <= 1000, "truncated output respects the cap")
    _check(
        result.output_chars_total >= 50000,
        "output_chars_total reports the true pre-cap length (no silent cap)",
    )
    _check(result.output.rstrip("\n").endswith("B"), "the TAIL of the output is kept")
    _check(result.exit_code == 0, "exit_code propagates from the subprocess")

    fail = run_bounded(
        [str(venv), "-c", "import sys; sys.exit(3)"],
        cwd=repo_root,
        timeout=30,
    )
    _check(fail.exit_code == 3, "non-zero exit code propagates verbatim")


def _test_real_gate_runs() -> None:
    """Integration: run a real, fast gate end-to-end through run_gate."""
    ops = QualityOperations(locate_repo_root(_APP_HOME), os.environ["SOLET_NAME"])
    result = ops.run_gate("service_interface_ast")
    expected_keys = {
        "gate",
        "passed",
        "exit_code",
        "timed_out",
        "summary",
        "output",
        "truncated",
        "output_chars_total",
    }
    _check(
        expected_keys <= set(result.keys()),
        "run_gate returns the full structured verdict shape",
    )
    _check(result["gate"] == "service_interface_ast", "verdict names the gate run")
    _check(isinstance(result["exit_code"], int), "exit_code is an int")
    _check(bool(result["output"]), "real gate produced captured output")
    _check(
        result["passed"] == (result["exit_code"] == 0),
        "passed is derived from exit_code == 0",
    )
    _check(not result["timed_out"], "the fast gate did not time out")


def main() -> int:
    print("quality_service.run_gate acceptance smoke")
    _test_registry_allowlist()
    _test_argv_is_server_fixed()
    _test_run_gate_rejects_unknown()
    _test_bounded_output()
    _test_real_gate_runs()

    print(f"\n{_passed} passed, {len(_failed)} failed")
    if _failed:
        for label in _failed:
            print(f"  FAILED: {label}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
