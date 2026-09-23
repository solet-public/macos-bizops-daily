#!/usr/bin/env python3
"""Controlled-environment process harness for the coordination-hooks suite.

Every hook in this plugin is default-OFF behind environment variables, so the
suite's negative controls are only meaningful if a child process cannot inherit
those variables from whatever launched the tests. A control that merely declines
to SET a variable is invalid here: a fleet launcher exports several of them, and
an inherited value would let a "disarmed" case run against a hook that is
actually ARMED and still report PASS.

This harness therefore builds each child environment from an explicit, minimal
base, and refuses to run at all if a guard variable reaches a child by any route
other than the calling case asking for it.

Hooks are exercised as PROCESSES, never imported. That is the contract Claude
Code actually uses -- environment and stdin in, stdout/stderr and an exit code
out -- and it is the only contract there is: every hook is a top-level script
that exports nothing.

Self-contained by construction: nothing here resolves a path outside this
plugin directory, so the suite runs from the plugin as an isolated artifact.
"""

from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
import sysconfig
from pathlib import Path

# Every smoke imports this module first, so setting the flag here keeps a
# directly-invoked smoke (`python3 tests/reminder_hooks_smoke.py`) from leaving
# .pyc files inside the artifact under review. run_all.py additionally passes -B
# to the children it spawns; this covers the case where it is not the caller.
sys.dont_write_bytecode = True

# The plugin ships to whatever machine reviews it, so the suite targets the
# oldest interpreter a reviewer is likely to have rather than the version the
# project that develops it uses. macOS still ships 3.9 as /usr/bin/python3.
MIN_PYTHON = (3, 9)

PLUGIN_ROOT = Path(__file__).resolve().parent.parent
HOOKS_DIR = PLUGIN_ROOT / "hooks"
TESTS_DIR = PLUGIN_ROOT / "tests"

# Every variable that arms, disarms, or configures a hook. None of these may
# reach a child process unless the calling case passes it explicitly.
GUARD_VARS = (
    "AGENT_SESSION_LABEL",
    # ⚠ LOAD-BEARING for every identity-keyed negative control. The fleet
    # launcher EXPORTS AGENT_SESSION_ID, so a "negative" case that merely
    # declines to set it would INHERIT the parent session's real id, arm the
    # hook it meant to disarm, and pass vacuously. Scrubbing it here is the
    # `env -u` discipline applied at the harness rather than per-case.
    # Added 2026-08-01 with the §7 re-key, which moved check_messages and
    # wake_waiter off the label and onto this variable.
    "AGENT_SESSION_ID",
    "AGENT_WAKE_CLI",
    "FLEET_TRANSPORT",
    "GIT_CONTROLLER_NAME",
    "SOLET_GIT_CONTROLLER_NAME",
    "CLAUDE_PROJECT_DIR",
    "CLAUDE_PLUGIN_ROOT",
)

# Passed through so the interpreters can start and temporary files land
# somewhere sane. Deliberately disjoint from GUARD_VARS.
_PASSTHROUGH = ("PATH", "HOME", "TMPDIR", "LANG", "LC_ALL", "SystemRoot")


def base_env() -> dict[str, str]:
    """A minimal child environment with no hook-arming variable in it."""
    env = {key: os.environ[key] for key in _PASSTHROUGH if key in os.environ}
    # The plugin discloses that running the Python gate leaves __pycache__ beside
    # the scripts. A reviewer running this suite should not watch new files
    # appear inside the artifact they are reviewing.
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    return env


def preflight() -> None:
    """Fail loud on a missing interpreter rather than reporting a vacuous pass."""
    problems: list[str] = []
    if sys.version_info < MIN_PYTHON:
        have = ".".join(str(part) for part in sys.version_info[:3])
        want = ".".join(str(part) for part in MIN_PYTHON)
        problems.append(f"python {want}+ required to run this suite, found {have}")
    if not HOOKS_DIR.is_dir():
        problems.append(f"hooks directory not found: {HOOKS_DIR}")
    # GUARD_VARS and _PASSTHROUGH being disjoint is proved statically by the type
    # checker (they are literal tuples), so there is no runtime check for it here.
    if problems:
        for line in problems:
            print(f"  FAIL  {line}")
        raise SystemExit(1)


def run_hook(
    script: str,
    *,
    env: dict[str, str] | None = None,
    stdin: str = "",
    timeout: int = 60,
) -> subprocess.CompletedProcess[str]:
    """Run one hook as Claude Code would, with a fully controlled environment."""
    path = HOOKS_DIR / script
    if not path.is_file():
        raise SystemExit(f"harness: hook not found: {path}")

    requested = dict(env or {})
    child_env = base_env()
    child_env.update(requested)

    leaked = [var for var in GUARD_VARS if var in child_env and var not in requested]
    if leaked:
        raise SystemExit(f"harness: guard variable(s) leaked into the child environment: {leaked}")

    argv = [sys.executable, "-B", str(path)]

    return subprocess.run(
        argv,
        input=stdin,
        env=child_env,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


def is_stdlib_module(module: str) -> bool:
    """True if `module` is part of the Python standard library.

    ``sys.stdlib_module_names`` is the direct answer but only exists on 3.10+,
    and a reviewer running macOS's own ``/usr/bin/python3`` gets 3.9. The
    fallback resolves the module and checks it lives under the interpreter's
    stdlib path and outside site-packages.
    """
    names = getattr(sys, "stdlib_module_names", None)
    if names is not None:
        return module in names
    if module in sys.builtin_module_names:
        return True
    try:
        spec = importlib.util.find_spec(module)
    except (ImportError, ValueError):
        return False
    if spec is None:
        return False
    origin = spec.origin
    if origin in (None, "built-in", "frozen"):
        return True
    stdlib_path = sysconfig.get_paths().get("stdlib", "")
    return bool(stdlib_path) and origin.startswith(stdlib_path) and "site-packages" not in origin


class Results:
    """Plain-script pass/fail recorder, matching this project's smoke convention."""

    def __init__(self, title: str) -> None:
        self.title = title
        self.passed: list[str] = []
        self.failures: list[str] = []
        print(title)
        print("=" * 68)

    def ok(self, label: str) -> None:
        self.passed.append(label)

    def fail(self, label: str, detail: str = "") -> None:
        message = f"{label}: {detail}" if detail else label
        self.failures.append(message)
        print(f"  FAIL  {message}")

    def check(self, condition: bool, label: str, detail: str = "") -> bool:
        if condition:
            self.ok(label)
        else:
            self.fail(label, detail)
        return condition

    def finish(self) -> int:
        passed = len(self.passed)
        total = passed + len(self.failures)
        print("-" * 68)
        if self.failures:
            print(f"{self.title}: {passed}/{total} checks passed, {len(self.failures)} FAILED")
            return 1
        print(f"{self.title}: {passed}/{total} checks passed")
        return 0
