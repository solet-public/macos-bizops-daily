#!/usr/bin/env python3
"""SessionStart hook. Silent no-op unless AGENT_SESSION_LABEL is set, so
unrelated Claude Code sessions on the same machine never see this reminder.
No shell involved (exec-form invocation from hooks.json). Runtime
dependency: python3, a guaranteed platform prerequisite -- unlike this
hook's prior Node implementation, which depended on a runtime nothing
guaranteed (see `wake_waiter.py`'s docstring; promoted 2026-08-08).

States a property of the environment; it does not instruct. The label is the
only interpolated value and it comes from the process environment, never from
stdin or from any message content.
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
    label = os.environ.get("AGENT_SESSION_LABEL", "").strip()
    if not label:
        return 0
    event_name = _read_stdin_event_name("SessionStart")
    quoted_label = json.dumps(label)
    output = {
        "hookSpecificOutput": {
            "hookEventName": event_name,
            "additionalContext": (
                f"This session was launched with the role label {quoted_label}. "
                "Where sessions are bound to durable role names, that "
                "binding lives outside the session, so the local label and "
                "the external binding are separate things and can "
                "disagree -- a binding made before a /clear, a restart, or "
                "a transport reconnect may still point at a previous "
                "session, and messages addressed to the role would then "
                "route there. Sessions in this environment typically "
                "re-assert their role binding at session start, if this "
                "project provides a mechanism for it. A listing that "
                "merely shows a session as present is evidence of presence, "
                "not of a held claim."
            ),
        },
    }
    print(json.dumps(output))
    return 0


if __name__ == "__main__":
    sys.exit(main())
