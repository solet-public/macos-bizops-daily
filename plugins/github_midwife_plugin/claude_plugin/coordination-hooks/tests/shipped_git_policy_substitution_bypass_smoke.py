#!/usr/bin/env python3
"""Regression smoke for shipped Git-policy parsing across all materialized copies."""

from __future__ import annotations

import sys
from pathlib import Path

sys.dont_write_bytecode = True

_HOOK_DIR = Path(__file__).resolve().parent.parent / "hooks"
sys.path.insert(0, str(_HOOK_DIR))

# ruff: noqa: I001, E402
import _git_policy as policy  # noqa: E402


_OTHER = "SomeOtherSession"
_CONTROLLER = "Git-Controller"
_HEREDOC_SUBSTITUTION = "x=$(cat <<EOF\nignored\nEOF\n); git commit -m x"
_UNPARSEABLE_GIT = (
    'git commit -m "unterminated',
    "git commit -m 'unterminated",
    'echo "oops && git push',
)
_HEREDOC_PROSE = "cat > note.md <<'EOF'\ngit commit -m prose\nEOF"
_UNPARSEABLE_HEREDOC_PROSE = _HEREDOC_PROSE + '\necho "unterminated'
_UNPARSEABLE_SUBSTITUTION = 'echo "$(git push -m \'unterminated)"'
_UNPARSEABLE_SCRIPT_SOURCE_HEREDOC = "bash <<'EOF'\ngit push -m 'unterminated\nEOF"

_passed = 0
_failed: list[str] = []


def _check(condition: bool, label: str) -> None:
    """Record one assertion without hiding later failures."""
    global _passed
    if condition:
        _passed += 1
    else:
        _failed.append(label)
        print(f"  FAIL  {label}")


def _decision(command: str, controller: str | None = _CONTROLLER) -> tuple[bool, str]:
    """Run the common shipped policy as a non-controller session."""
    return policy.check_bash({"command": command}, _OTHER, controller)


def case_plain_and_assignment_prefixed_commits_block() -> None:
    """An assignments-only segment cannot end the scan before later git."""
    for command, label in (
        ("git commit -m x", "plain commit"),
        ("x=1; git commit -m x", "assignment-prefixed commit"),
        ("x=1 y=2; git commit -m x", "two-assignment-prefixed commit"),
        ("a=$(true); git commit -m x", "substitution-assignment-prefixed commit"),
        (_HEREDOC_SUBSTITUTION, "heredoc-in-substitution commit"),
    ):
        blocked, reason = _decision(command)
        _check(blocked, f"{label} blocks (got {blocked}, {reason!r})")
        _check("commit" in reason, f"{label} names commit in refusal (got {reason!r})")


def case_unparseable_git_fails_closed_when_armed() -> None:
    """A non-controller cannot use malformed shell around a visible git token."""
    for command in _UNPARSEABLE_GIT:
        blocked, reason = _decision(command)
        _check(blocked, f"unparseable git blocks: {command!r}")
        _check("could not be safely inspected" in reason, f"unparseable reason: {command!r}")


def case_heredoc_prose_stays_data_when_unparseable() -> None:
    """A malformed outer line cannot turn heredoc prose into an invocation."""
    for command, label in (
        (_HEREDOC_PROSE, "parseable heredoc prose"),
        (_UNPARSEABLE_HEREDOC_PROSE, "unparseable heredoc prose"),
        ('echo "unterminated', "unparseable source without git"),
    ):
        _check(_decision(command) == (False, ""), f"{label} is allowed")
    blocked, reason = _decision('git commit -m "unterminated')
    _check(blocked, "unparseable real git blocks")
    _check("could not be safely inspected" in reason, "unparseable real git names inspection")


def case_unparseable_nested_source_fails_closed() -> None:
    """Nested shell source preserves parse failure and scans executable bodies."""
    for command, label in (
        (_UNPARSEABLE_SUBSTITUTION, "unparseable substitution"),
        (_UNPARSEABLE_SCRIPT_SOURCE_HEREDOC, "unparseable script-source heredoc"),
    ):
        blocked, reason = _decision(command)
        _check(blocked, f"{label} blocks for non-controller")
        _check("could not be safely inspected" in reason, f"{label} names inspection")
        _check(
            policy.check_bash({"command": command}, _CONTROLLER, _CONTROLLER) == (False, ""),
            f"{label} remains allowed for Git-Controller",
        )


def case_gate_off_stays_open() -> None:
    """The controller-None gate-off contract remains an explicit allow."""
    _check(
        _decision('git commit -m "unterminated', None) == (False, ""),
        "gate-off unparseable git is allowed",
    )


def main() -> int:
    """Run every regression leg and return one smoke verdict."""
    for case in (
        case_plain_and_assignment_prefixed_commits_block,
        case_unparseable_git_fails_closed_when_armed,
        case_heredoc_prose_stays_data_when_unparseable,
        case_unparseable_nested_source_fails_closed,
        case_gate_off_stays_open,
    ):
        case()
    print(f"{_passed} passed, {len(_failed)} failed")
    return 1 if _failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
