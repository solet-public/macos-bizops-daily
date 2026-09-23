#!/usr/bin/env python3
"""SessionStart hook. ALWAYS ARMED: installed means armed, with no
environment condition of any kind.

The solet is a system-wide resource, so awareness of it is not
fleet-only. This hook previously no-op'd unless AGENT_SESSION_LABEL was set;
that gate was removed deliberately (operator ruling 2026-08-01), and the
FAILURE DIRECTION INVERTED WITH IT: a silently disarmed awareness reminder
means a session never learns the platform exists, which is the silent-absence
class. Re-adding any env condition here is the red mutation for this hook's
smoke leg.

The emitted hookEventName is read off stdin and echoed back, mirroring
check_messages_reminder.py, with the compiled-in default matching this
hook's own manifest binding. It was previously a hardcoded literal, which
silently desynced when the 2026-08-11 cadence move rebound this hook from
UserPromptSubmit to SessionStart: Claude Code rejects (at debug level only)
a hook whose declared event name does not match the event that invoked it,
so the reminder was discarded on every session start -- the same
silent-absence class as an env gate. Found 2026-08-11, confirmed
independently by an adopter (feedback Part 41); the red mutation for this
is re-hardcoding any event name the manifest does not wire this hook to
(`check_manifest_bound_events_echo`).

The literal below is true wherever the plugin is installed: it names no
deployment-relative path and no fleet-specific command, so it reads correctly
in an arbitrary directory with zero fleet context. Deployment-specific
how-and-where lives in the user-scope instructions section, not here.

No shell involved (exec-form invocation from hooks.json). Runtime
dependency: python3, a guaranteed platform prerequisite -- unlike this
hook's prior Node implementation, which depended on a runtime nothing
guaranteed (see `wake_waiter.py`'s docstring; promoted 2026-08-08).
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

_CONTEXT = (
    "For non-trivial work, checking two context sources in sequence "
    "before other work is usually faster than re-deriving an answer "
    "partway through: first any persistent knowledge base available "
    "to this session (via a local CLI or a connected MCP tool, if "
    "any), then the current working directory's own docs (e.g. "
    "CLAUDE.md/AGENTS.md, if present). The sequence is not a "
    "substitution -- the knowledge base carries platform and "
    "cross-session knowledge, the working directory's docs govern "
    "the task at hand, and neither replaces the other. Such a lookup "
    "may run asynchronously -- its result can arrive after other work "
    "has already started, so there is no need to block on it once it "
    "is under way."
)


def _read_stdin_event_name(default: str) -> str:
    try:
        raw = sys.stdin.read()
        payload = json.loads(raw) if raw.strip() else {}
    except Exception:  # noqa: BLE001 -- malformed/absent stdin, fall through
        return default
    if isinstance(payload, dict):
        event_name = payload.get("hook_event_name")
        if isinstance(event_name, str) and event_name:
            return event_name
    return default


def main() -> int:
    event_name = _read_stdin_event_name("SessionStart")
    output = {
        "hookSpecificOutput": {
            "hookEventName": event_name,
            "additionalContext": _CONTEXT,
        },
    }
    print(json.dumps(output))
    return 0


if __name__ == "__main__":
    sys.exit(main())
