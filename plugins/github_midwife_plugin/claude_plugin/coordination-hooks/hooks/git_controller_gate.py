#!/usr/bin/env python3
"""Claude Code adapter for the shared Git-Controller policy.

The policy is a trusted-peer mistake-prevention guard, not an adversarial
security boundary.  It is enabled only when ``GIT_CONTROLLER_NAME`` names the
controller role.  Claude identity continues to come from the runner-owned
``~/.claude/sessions/<parent_pid>.json`` binding.

Hook contract: invoked as a Claude Code ``PreToolUse`` handler.  Exit 2 blocks
the tool and every other exit is non-blocking.  The adapter therefore owns the
wire payload, identity resolution, routing, and block presentation while
``_git_policy`` owns the runner-neutral decisions.

Stdlib-only by design.
"""


from __future__ import annotations

# INTERPRETER FLOOR. These hooks are Python 3.13 source and use datetime.UTC
# (3.11+). Claude Code launches them with a bare `python3`, which resolves from
# PATH -- on a stock macOS that is frequently the system 3.9, and the resulting
# ImportError traceback surfaced to the operator as a hook error on EVERY tool
# call. Measured 2026-08-20: 8 of 20 shipped hook modules failed to import.
#
# Placed AFTER `from __future__` (which must stay the first statement) and
# BEFORE the first 3.11+ import, because an ImportError at module level cannot
# be caught by anything inside this file. Exits 0 and SILENTLY: the shipped
# contract is that a session which cannot run these hooks gets zero output and
# zero errors, and a diagnostic here would reproduce the very symptom it fixes.
# A floor, not a compatibility shim -- nothing is emulated or back-ported.
import sys

if sys.version_info < (3, 11):  # noqa: UP036 -- see above; ruff assumes
    # the project's py313 target, but this file ships to an ADOPTER's machine
    # and is launched by whatever `python3` their PATH resolves.
    raise SystemExit(0)


import json
import os
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
# ruff: noqa: I001, E402
# pyright: reportMissingImports=false
# Direct sibling imports keep the standalone artifact's complete helper graph
# visible to its source-level security review; _git_policy uses both transitively.
import _git_controller_lex  # noqa: F401
import _git_controller_walker  # noqa: F401
import _git_policy as policy

_SECURITY_REVIEW_SIBLINGS = (_git_controller_lex, _git_controller_walker)

# Compatibility exports retained for the established hook and smoke contract.
ALLOWED_NO_FLAG_CHECK = policy.ALLOWED_NO_FLAG_CHECK
BASH_TOOL_NAME = policy.BASH_TOOL_NAME
FILE_PATH_TOOL_NAMES = policy.FILE_PATH_TOOL_NAMES
GATED_TOOL_NAMES = policy.GATED_TOOL_NAMES
SUBAGENT_TOOL_NAMES = policy.SUBAGENT_TOOL_NAMES
POLICY_MESSAGE = policy.POLICY_MESSAGE


GIT_CONTROLLER_ENV = "GIT_CONTROLLER_NAME"


def git_controller_name() -> str | None:
    """Return the configured controller role, or None when the gate is off."""
    name = os.environ.get(GIT_CONTROLLER_ENV, "").strip()
    return name or None


def find_session_name() -> str | None:
    """Resolve the calling Claude Code session's runner-owned name binding."""
    parent_pid = os.getppid()
    session_file = Path.home() / ".claude" / "sessions" / f"{parent_pid}.json"
    for _ in range(2):
        try:
            raw = json.loads(session_file.read_text())
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            continue
        if isinstance(raw, dict):
            name = raw.get("name")
            if isinstance(name, str):
                return name
        return None
    return None


def is_invocation_allowed(invocation: list[str]) -> tuple[bool, str]:
    """Compatibility export for the established Claude hook smoke suite."""
    return policy.is_invocation_allowed(invocation)


def check_bash(
    tool_input: dict[str, object], session_name: str | None,
) -> tuple[bool, str]:
    """Adapt a Claude Bash payload to the shared policy."""
    return policy.check_bash(tool_input, session_name, git_controller_name())


def check_file_path(
    tool_input: dict[str, object], session_name: str | None,
) -> tuple[bool, str]:
    """Adapt a Claude file-tool payload to the shared policy."""
    return policy.check_file_path(
        tool_input,
        session_name,
        git_controller_name(),
        os.environ.get("CLAUDE_PROJECT_DIR"),
    )


def check_task(
    tool_input: dict[str, object], session_name: str | None,
) -> tuple[bool, str]:
    """Adapt a Claude sub-agent payload to the shared policy."""
    return policy.check_task(tool_input, session_name, git_controller_name())


def _main_inner() -> tuple[bool, str, str]:
    """Return ``(block, reason, identity)`` for one Claude hook payload."""
    payload = json.loads(sys.stdin.read())
    if not isinstance(payload, dict):
        return False, "", "<unknown>"

    tool_name_raw = payload.get("tool_name", "")
    tool_name = tool_name_raw if isinstance(tool_name_raw, str) else ""
    tool_input_raw = payload.get("tool_input", {}) or {}
    tool_input: dict[str, object] = (
        tool_input_raw if isinstance(tool_input_raw, dict) else {}
    )

    session_name = find_session_name()
    identity = session_name or "<unknown>"

    if tool_name == BASH_TOOL_NAME:
        block, reason = check_bash(tool_input, session_name)
        return block, reason, identity
    if tool_name in FILE_PATH_TOOL_NAMES:
        block, reason = check_file_path(tool_input, session_name)
        return block, reason, identity
    if tool_name in SUBAGENT_TOOL_NAMES:
        block, reason = check_task(tool_input, session_name)
        return block, reason, identity
    return False, "", identity


def main() -> int:
    """Return 2 to block and 0 to allow under Claude's hook contract."""
    try:
        block, reason, identity = _main_inner()
    except json.JSONDecodeError:
        return 0
    except Exception:  # noqa: BLE001 -- trusted-peer mistake-prevention scope
        return 0

    if not block:
        return 0
    try:
        print(
            f"[git-controller-gate] BLOCKED: session {identity!r} attempted "
            f"a restricted operation.\n"
            f"Reason: {reason}\n\n"
            f"{POLICY_MESSAGE}",
            file=sys.stderr,
        )
    except Exception:  # noqa: BLE001 -- telemetry is best-effort
        pass
    return 2


if __name__ == "__main__":
    sys.exit(main())
