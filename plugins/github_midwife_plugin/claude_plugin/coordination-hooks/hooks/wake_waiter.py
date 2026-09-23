#!/usr/bin/env python3
"""Stop hook -- the idle-wake half of session coordination, nudge-only.

When this session goes idle, this hook invokes the operator-configured
coordination CLI's blocking wait verb: exactly `$AGENT_WAKE_CLI wake`, fixed
argv, no shell. That command blocks (at zero model cost) until a
coordination delivery arrives for this session, then exits with the Claude
Code hook wake code (2).

DELIBERATELY DISCARDS the CLI's output. The child's stdout/stderr are
dropped unread; on the wake signal this hook emits its own compiled-in
fixed nudge instead. The hook therefore conveys exactly ONE BIT of dynamic
information -- "deliveries are pending" -- plus timing. Message CONTENT
enters the session only when the model explicitly fetches it from the peer
inbox (the durable store; the watcher's spool is a tee of notifications, so
discarding it here loses nothing).

Armed ONLY when ALL of the following hold (silent no-op otherwise). Each is
a MECHANICAL precondition -- something the waiter needs in order to do
anything at all -- never a protection:
  - AGENT_SESSION_ID is set: the per-session watch spool path derives from
    it, so without one there is no spool to wait on and the waiter would
    block for ~24h on nothing;
  - AGENT_WAKE_CLI is set (the operator's launcher names the CLI to run);
  - FLEET_TRANSPORT is unset or "watch" (a declared non-watch transport,
    e.g. "mcp", disarms this hook even if the CLI variable is exported --
    on those transports the live bridge connection does the waking).

Exit contract: child exit 2 (wake) -> fixed nudge on stderr + exit 2. Child
exit 0 (idle expiry / not a fleet session / another waker armed) -> silent
exit 0. Anything else (including spawn failure) -> exit 0 with a one-line
fixed-format note carrying only the numeric status -- a broken wake path
must never trap the session in a failing Stop hook.

BOUNDED WAIT (2026-08-09). The wait is capped at `_DEFAULT_MAX_WAIT_S` and
NOT left at the CLI's own ~23.9h default. Rationale, measured: a Stop hook
that blocks holds the harness session-status file at "shell", and "idle" is
therefore never stamped. The seat_idle_watcher's only idle gate is
status == "idle", so an unbounded waiter structurally kills BOTH of that
watcher's legs -- the keep-warm poke and the context rotation. Live evidence
from the 2026-08-08 overnight stall: 578 watcher ticks, 531 "shell" / 34
"busy" / ZERO "idle" readings across a 9h37m life. Capping the wait inside
the prompt-cache window lets the hook release the turn boundary, so the
harness can stamp "idle" and the watcher can act on it. A delivery still
wakes the session immediately -- the cap only bounds the QUIET case.

The cap is a coordination constant, not a protection: it must stay under the
prompt-cache TTL (~1h) so a session cannot go cache-cold while the waiter is
still holding the boundary. Override per-session with
`AGENT_WAKE_MAX_WAIT_S` when a lane needs a different window.

Requires `asyncRewake: true` (or the older synchronous shape) on the Stop
hook entry that invokes this script in `hooks.json` -- the wake CLI blocks
for up to the cap; without that hook flag, Claude Code would treat this as
an ordinary short-timeout synchronous hook and kill it.

Python since 2026-08-08 (promoted from a project-vendored port built for
spawned fleet workers, retiring this plugin's prior Node implementation of
the same contract): every runtime this plugin now uses (`python3`) is a
declared, guaranteed prerequisite -- unlike Node, which was never guaranteed
by Claude Code or any consuming platform and could silently fail to launch
this hook with no visible signal (see this checkout's deaf-wake findings,
2026-08-08). See `git_controller_gate.py` for the sibling hook that already
proved this exact python3 + `${CLAUDE_PLUGIN_ROOT}` invocation shape works
in this manifest.
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


import os
import subprocess

_OWNER_DIRECTORY = os.path.dirname(os.path.abspath(__file__))
if _OWNER_DIRECTORY not in sys.path:
    sys.path.insert(0, _OWNER_DIRECTORY)
import coordination_owner  # noqa: E402 -- sibling path follows runtime floor

_WAKE_EXIT_SIGNAL = 2
# 40 minutes -- inside the ~1h prompt-cache TTL with margin. See the module
# docstring: this is the constant that keeps "idle" reachable at all.
_DEFAULT_MAX_WAIT_S = 2400
_MAX_WAIT_ENV = "AGENT_WAKE_MAX_WAIT_S"
_NUDGE = (
    "While this session was idle, its coordination watcher received one or "
    "more new peer-message deliveries. Durable copies are preserved in this "
    "session's peer-message inbox and have not yet been read here."
)


def resolve_max_wait_s() -> int:
    """The bounded wait in seconds, honouring the per-session override.

    A malformed or non-positive override is reported LOUDLY and then falls
    back to the default -- never silently. Refusing to wait at all would
    strand the session with no wake coverage, which is the worse failure of
    the two, so the fallback is deliberate and announced.
    """
    raw = os.environ.get(_MAX_WAIT_ENV, "").strip()
    if not raw:
        return _DEFAULT_MAX_WAIT_S
    try:
        value = int(raw)
    except ValueError:
        print(
            f"[coordination-hooks wake] {_MAX_WAIT_ENV}={raw!r} is not an integer; "
            f"using {_DEFAULT_MAX_WAIT_S}s",
            file=sys.stderr,
        )
        return _DEFAULT_MAX_WAIT_S
    if value <= 0:
        print(
            f"[coordination-hooks wake] {_MAX_WAIT_ENV}={value} is not positive; "
            f"using {_DEFAULT_MAX_WAIT_S}s",
            file=sys.stderr,
        )
        return _DEFAULT_MAX_WAIT_S
    return value


def _has_verified_runtime_state() -> bool:
    ownership = coordination_owner.verify("wake", __file__)
    if not ownership.eligible:
        if ownership.managed:
            coordination_owner.report_refusal(ownership)
        return False
    if coordination_owner.runtime_identity_directory(ownership) is None:
        print("[wake_waiter] verified owner has no receipt-derived runtime state root", file=sys.stderr)
        return False
    return True


def main() -> int:
    if not _has_verified_runtime_state():
        return 0
    session_id = os.environ.get("AGENT_SESSION_ID", "").strip()
    cli = os.environ.get("AGENT_WAKE_CLI", "").strip()
    transport = os.environ.get("FLEET_TRANSPORT", "").strip()
    if not session_id or not cli or (transport and transport != "watch"):
        return 0
    try:
        result = subprocess.run(
            [cli, "wake", "--max-wait", str(resolve_max_wait_s())],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            check=False,
        )
    except OSError as exc:
        print(f"[coordination-hooks wake] could not run the configured wake CLI: {exc}", file=sys.stderr)
        return 0
    if result.returncode == _WAKE_EXIT_SIGNAL:
        print(_NUDGE, file=sys.stderr)
        return _WAKE_EXIT_SIGNAL
    if result.returncode != 0:
        print(f"[coordination-hooks wake] wake CLI exited with status {result.returncode}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
