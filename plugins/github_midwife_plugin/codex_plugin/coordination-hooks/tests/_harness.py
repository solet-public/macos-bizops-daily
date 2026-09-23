#!/usr/bin/env python3
"""Controlled-environment subprocess harness for stock-Codex hook smokes."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

sys.dont_write_bytecode = True

MIN_PYTHON = (3, 9)
PLUGIN_ROOT = Path(__file__).resolve().parent.parent
HOOKS_DIR = PLUGIN_ROOT / "hooks"
TESTS_DIR = PLUGIN_ROOT / "tests"

GUARD_VARS = (
    "AGENT_IDENTITY",
    "AGENT_INSTANCE_ID",
    "AGENT_SESSION_ID",
    "AGENT_SESSION_LABEL",
    "AGENT_ROLE",
    "AGENT_WAKE_CLI",
    "FLEET_TRANSPORT",
    "GIT_CONTROLLER_NAME",
    "PLUGIN_ROOT",
    "PLUGIN_DATA",
    "CLAUDE_PLUGIN_ROOT",
    "CLAUDE_PLUGIN_DATA",
)
_PASSTHROUGH = ("PATH", "HOME", "TMPDIR", "LANG", "LC_ALL", "SystemRoot")


def base_env() -> dict[str, str]:
    env = {name: os.environ[name] for name in _PASSTHROUGH if name in os.environ}
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    return env


# The automake/Meson/CTest SKIP_RETURN_CODE, matching quality_gates/run_smokes.py's
# own _SKIP_EXIT_CODE so the runner reports these as SKIPPED rather than passed.
SKIP_EXIT_CODE = 77


def preflight() -> None:
    """Fail loudly on a real problem; SKIP DISCLOSED when only node is missing.

    This plugin is the CODEX coordination-hooks copy — an opt-in capability an
    adopter enables only if they use Codex. Its hooks are Node scripts, and
    nothing in the documented install path requires or installs node, so a
    machine without node legitimately cannot exercise them. Hard-failing there
    made an optional capability mandatory at test time: every adopter, Codex user
    or not, failed their own gate run on a runtime they may never use.

    A disclosed skip is honest HERE and would NOT be on the claude copy, where the
    hooks are enabled by default for every adopter — announcing a skip for a
    default-on capability would disclose that it silently does not work. That copy
    resolves the dependency instead of skipping it, and its preflight is not this
    one's to change.

    ORDERING IS LOAD-BEARING. Real problems are collected and raised BEFORE the
    node check, so a genuine defect on a node-less machine still fails loudly
    instead of being masked by the skip. A preflight that skipped on every problem
    would be indistinguishable from this one on any passing run — see
    ``preflight_skip_contract_smoke.py``, which drives all three combinations.
    """
    problems: list[str] = []
    if sys.version_info < MIN_PYTHON:
        problems.append("python 3.9 or newer is required")
    if not HOOKS_DIR.is_dir():
        problems.append(f"hooks directory not found: {HOOKS_DIR}")
    if problems:
        for problem in problems:
            print(f"FAIL: {problem}")
        raise SystemExit(1)

    if shutil.which("node") is None:
        print(
            "SKIP: node is not on PATH. The Codex context hooks are Node scripts and "
            "this is an opt-in Codex capability, so a machine without node cannot "
            "exercise them and is not expected to."
        )
        raise SystemExit(SKIP_EXIT_CODE)


def run_hook(
    script: str,
    *,
    env: dict[str, str] | None = None,
    stdin: str = "",
    timeout: int = 30,
) -> subprocess.CompletedProcess[str]:
    path = HOOKS_DIR / script
    if not path.is_file():
        raise SystemExit(f"hook not found: {path}")
    requested = dict(env or {})
    child_env = base_env()
    child_env.update(requested)
    leaked = [name for name in GUARD_VARS if name in child_env and name not in requested]
    if leaked:
        raise SystemExit(f"guard variables leaked into controlled child: {leaked}")
    argv = ["node", str(path)] if path.suffix == ".js" else [sys.executable, "-B", str(path)]
    return subprocess.run(
        argv,
        input=stdin,
        env=child_env,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


class Results:
    def __init__(self, title: str) -> None:
        self.title = title
        self.passed = 0
        self.failures: list[str] = []

    def check(self, condition: bool, label: str, detail: str = "") -> bool:
        if condition:
            self.passed += 1
            return True
        self.failures.append(f"{label}: {detail}" if detail else label)
        return False

    def finish(self) -> int:
        if self.failures:
            print(f"FAIL: {self.title}: {self.passed} passed, {len(self.failures)} failed")
            for failure in self.failures:
                print(f"  - {failure}")
            return 1
        print(f"PASS: {self.passed} {self.title} checks")
        return 0
