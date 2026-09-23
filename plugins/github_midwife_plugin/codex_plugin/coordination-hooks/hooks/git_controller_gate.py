#!/usr/bin/env python3
"""Codex adapter for the shared Git-Controller mistake-prevention policy.

Stock Codex hook probes establish the only routed shape in this unit:
``PreToolUse`` with ``tool_name == "Bash"`` and ``tool_input.command``.  The
launcher-owned ``AGENT_ROLE`` environment variable is the sole identity input;
Codex ``session_id`` is a thread UUID and is deliberately ignored.

The gate is enabled only when ``GIT_CONTROLLER_NAME`` names the controller
role.  Exit 2 blocks under the measured synchronous Codex hook contract, while
every other exit permits the tool.  This is a trusted-peer mistake-prevention
guard, not an adversarial security boundary.

Stdlib-only by design.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
# ruff: noqa: I001, E402
# pyright: reportMissingImports=false
import _git_policy as policy


GIT_CONTROLLER_ENV = "GIT_CONTROLLER_NAME"
AGENT_ROLE_ENV = "AGENT_ROLE"
CODEX_GATED_TOOL_NAMES = frozenset({policy.BASH_TOOL_NAME})
POLICY_MESSAGE = policy.POLICY_MESSAGE


def _read_role(env_name: str) -> str | None:
    """Read a non-empty role binding from one explicitly named variable."""
    value = os.environ.get(env_name, "").strip()
    return value or None


def git_controller_name() -> str | None:
    """Return the configured controller role, or None when the gate is off."""
    return _read_role(GIT_CONTROLLER_ENV)


def agent_role() -> str | None:
    """Return the launcher-owned Codex role binding."""
    return _read_role(AGENT_ROLE_ENV)


def _main_inner() -> tuple[bool, str, str]:
    """Return ``(block, reason, identity)`` for one Codex hook payload."""
    payload = json.loads(sys.stdin.read())
    if not isinstance(payload, dict):
        return False, "", "<unknown>"

    tool_name_raw = payload.get("tool_name", "")
    tool_name = tool_name_raw if isinstance(tool_name_raw, str) else ""
    tool_input_raw = payload.get("tool_input", {}) or {}
    tool_input: dict[str, object] = (
        tool_input_raw if isinstance(tool_input_raw, dict) else {}
    )

    session_role = agent_role()
    identity = session_role or "<unknown>"
    if tool_name in CODEX_GATED_TOOL_NAMES:
        block, reason = policy.check_bash(
            tool_input,
            session_role,
            git_controller_name(),
        )
        return block, reason, identity
    return False, "", identity


def main() -> int:
    """Return 2 to block and 0 to allow under the measured Codex contract."""
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
            f"[git-controller-gate] BLOCKED: role {identity!r} attempted "
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
