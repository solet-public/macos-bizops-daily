"""Bounded subprocess execution — server-side argv, timeout, no silent caps.

Shared by both service interfaces (``quality_service`` gate/smoke runs;
``repo_service`` read-only git). Every argv is built server-side by the
caller — this module never accepts a shell string or caller-supplied flags.

Output is captured, size-capped, and returned with an EXPLICIT ``truncated``
marker plus the true total length: the platform's "no silent truncation"
rule means a trimmed response must always announce that it was trimmed and
how much was dropped. The TAIL is kept (gate verdicts + smoke failure
summaries land at the end of the stream).
"""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from pathlib import Path

# Timeout sentinel exit code (mirrors the shell 128+SIGKILL convention for a
# killed process; distinct from any gate's real 0/1/2/64 verdict codes).
TIMEOUT_EXIT_CODE = 124

_DEFAULT_MAX_OUTPUT_CHARS = 16_000


@dataclass(frozen=True)
class SubprocessResult:
    """Outcome of one bounded subprocess run."""

    exit_code: int
    output: str
    truncated: bool
    output_chars_total: int
    timed_out: bool


def _bound_output(combined: str, max_output_chars: int) -> tuple[str, bool, int]:
    """Cap ``combined`` to its trailing ``max_output_chars``; report the total."""
    total = len(combined)
    if total <= max_output_chars:
        return combined, False, total
    return combined[-max_output_chars:], True, total


def run_bounded(
    argv: list[str],
    cwd: Path,
    timeout: int,
    extra_env: dict[str, str] | None = None,
    max_output_chars: int = _DEFAULT_MAX_OUTPUT_CHARS,
) -> SubprocessResult:
    """Run ``argv`` under ``cwd`` with a hard ``timeout``; return a bounded result.

    ``extra_env`` is overlaid on the inherited environment (used to pass
    ``SOLET_NAME`` through explicitly for the gate runs). On timeout the
    child is killed and a ``timed_out`` result with ``TIMEOUT_EXIT_CODE`` is
    returned — never a partial verdict masquerading as a pass.
    """
    env = dict(os.environ)
    if extra_env:
        env.update(extra_env)
    try:
        proc = subprocess.run(
            argv,
            cwd=str(cwd),
            capture_output=True,
            text=True,
            timeout=timeout,
            env=env,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return SubprocessResult(
            exit_code=TIMEOUT_EXIT_CODE,
            output=f"TIMEOUT after {timeout}s",
            truncated=False,
            output_chars_total=0,
            timed_out=True,
        )
    combined = (proc.stdout or "") + (proc.stderr or "")
    bounded, truncated, total = _bound_output(combined, max_output_chars)
    return SubprocessResult(
        exit_code=proc.returncode,
        output=bounded,
        truncated=truncated,
        output_chars_total=total,
        timed_out=False,
    )
