"""Shared external-tool runner for the deterministic scanners.

Centralizes subprocess invocation, version capture, and tool-availability
detection so no scanner reimplements it. A tool that is not installed is a
*coverage gap* (surfaced in the run's ``coverage_gaps``), not a masked failure:
the scanner's job is to report what it could and could not examine.

Note (shared gotcha): ``timeout`` is not installed on this Mac — we bound runs
with ``subprocess.run(timeout=...)`` instead of wrapping the command.
"""

from __future__ import annotations

import logging
import os
import platform
import shlex
import shutil
import subprocess
from dataclasses import dataclass

_DEFAULT_TIMEOUT_S = 900
_QUALIFICATION_TIMEOUT_S = 2
_logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class ToolOutcome:
    """Result of one external-tool invocation."""

    returncode: int
    stdout: str
    stderr: str
    timed_out: bool = False


@dataclass(frozen=True, slots=True)
class ToolQualification:
    """One process-lifetime usability verdict for a PATH-resolved external tool."""

    name: str
    path: str | None
    usable: bool
    reason: str


_TOOL_QUALIFICATIONS: dict[str, ToolQualification] = {}


def _quarantine_detail(path: str) -> str | None:
    """Return the operator remedy when macOS has quarantined ``path``; never alter it."""
    if platform.system() != "Darwin":
        return None
    try:
        checked = subprocess.run(
            ["xattr", "-p", "com.apple.quarantine", path],
            capture_output=True,
            text=True,
            timeout=_QUALIFICATION_TIMEOUT_S,
            check=False,
            stdin=subprocess.DEVNULL,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if checked.returncode != 0:
        return None
    return (
        f"com.apple.quarantine is present on {path}; remedy: xattr -d "
        f"com.apple.quarantine {shlex.quote(path)}"
    )


def _unusable(name: str, path: str, reason: str) -> ToolQualification:
    detail = _quarantine_detail(path)
    return ToolQualification(name, path, False, f"{reason}; {detail}" if detail else reason)


def _qualify_path(name: str, path: str) -> ToolQualification:
    try:
        completed = subprocess.run(
            [path, "--version"],
            capture_output=True,
            text=True,
            timeout=_QUALIFICATION_TIMEOUT_S,
            check=False,
            stdin=subprocess.DEVNULL,
        )
    except subprocess.TimeoutExpired:
        return _unusable(name, path, f"{name} unusable: hung past {_QUALIFICATION_TIMEOUT_S}s during qualification")
    except OSError as exc:
        return _unusable(name, path, f"{name} unusable: failed to start ({exc})")
    if completed.returncode != 0:
        return _unusable(name, path, f"{name} unusable: exit {completed.returncode} during qualification")
    return ToolQualification(name, path, True, f"{name} usable")


def qualify_tool(name: str) -> ToolQualification:
    """Trial-invoke a PATH tool once and cache an honest, bounded verdict."""
    cached = _TOOL_QUALIFICATIONS.get(name)
    if cached is not None:
        return cached
    path = shutil.which(name)
    verdict = ToolQualification(name, None, False, f"{name} absent from PATH") if path is None else _qualify_path(name, path)
    _TOOL_QUALIFICATIONS[name] = verdict
    if not verdict.usable:
        _logger.warning("external tool qualification: %s", verdict.reason)
    return verdict


def tool_unavailable_reason(name: str) -> str:
    """The cached absence/unusability reason for a coverage-gap disclosure."""
    return qualify_tool(name).reason


def tool_available(name: str) -> bool:
    """True only when ``name`` resolves and answers a bounded trial invocation."""
    return qualify_tool(name).usable


def tool_version(name: str, version_arg: str = "--version") -> str | None:
    """First line of the tool's version banner, or None if it cannot be read."""
    if not tool_available(name):
        return None
    outcome = run([name, version_arg], timeout_s=30)
    line = outcome.stdout.strip() or outcome.stderr.strip()
    return line.splitlines()[0] if line else None


def run(
    argv: list[str],
    *,
    cwd: str | None = None,
    timeout_s: int = _DEFAULT_TIMEOUT_S,
    env_overrides: dict[str, str] | None = None,
    raise_on_timeout: bool = True,
) -> ToolOutcome:
    """Run ``argv`` and capture output.

    Fast-fail policy: a missing executable, a non-zero exit, or a security
    finding are all legitimate observations the caller interprets — this helper
    does not swallow them. It raises on the two conditions that mean the scan
    itself is broken (executable absent, or it hung past the bound), so the
    failure is loud rather than silently producing an empty finding set.

    ``raise_on_timeout=False`` is for network-dependent tools (semgrep registry,
    pip-audit advisory DB) where a timeout is an *environmental* condition the
    caller converts to a coverage gap, not a defect: the timeout returns a
    ``ToolOutcome`` with ``timed_out=True`` instead of raising.

    ``env_overrides`` are layered onto the current environment (e.g. the platform
    gates want ``SOLET_NAME`` set).
    """
    env: dict[str, str] | None = None
    if env_overrides is not None:
        env = dict(os.environ)
        env.update(env_overrides)
    try:
        completed = subprocess.run(
            argv,
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=timeout_s,
            check=False,
            env=env,
            # Non-interactive tools never read stdin; without this they inherit
            # the parent's (piped) stdin and an interactive-ish tool (e.g. a
            # `claude -p` reviewer subprocess) blocks forever waiting on it.
            stdin=subprocess.DEVNULL,
        )
    except FileNotFoundError as exc:
        raise RuntimeError(f"tool not found on PATH: {argv[0]}") from exc
    except subprocess.TimeoutExpired as exc:
        if raise_on_timeout:
            raise RuntimeError(f"tool timed out after {timeout_s}s: {' '.join(argv)}") from exc
        return ToolOutcome(returncode=-1, stdout="", stderr=f"timed out after {timeout_s}s", timed_out=True)
    return ToolOutcome(
        returncode=completed.returncode,
        stdout=completed.stdout,
        stderr=completed.stderr,
    )
