#!/usr/bin/env python3
"""SessionStart + UserPromptSubmit hook. Reads the hook_event_name Claude Code
passes on stdin so the same script serves both events without guessing which
one fired.

Armed on AGENT_SESSION_ID -- identity, not label, since the inbox this
reminder points at is keyed on identity. Silent no-op unless it is set, so
unrelated Claude Code sessions on the same machine never see this reminder.

Python since 2026-08-08 (promoted from a project-vendored port; see
`wake_waiter.py`'s docstring for the full runtime-dependency rationale
shared by all four reminder/wake hooks in this plugin).
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
    session_id = os.environ.get("AGENT_SESSION_ID", "").strip()
    if not session_id:
        return 0
    event_name = _read_stdin_event_name("UserPromptSubmit")
    output = {
        "hookSpecificOutput": {
            "hookEventName": event_name,
            "additionalContext": (
                "Unread coordination messages from other sessions may be "
                "pending, if this project uses a peer-messaging or "
                "shared-inbox mechanism."
            ),
        },
    }
    print(json.dumps(output))
    return 0


if __name__ == "__main__":
    sys.exit(main())
