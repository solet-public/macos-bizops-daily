#!/usr/bin/env python3
"""Stop hook -- CDX-06 parts A and C: the Codex peer-inbox consumer, and its
own honesty attestation.

Ruling 4 (2026-08-24, operator rulings on the test-seed/dependencies/Codex-
deafness batch): "Yes, fix the Codex so it never goes deaf." Root cause
(Lane W's CDX-06 report, and the project backlog's CDX-06 entry): Codex has
NO consumer for the peer-inbox wake event at all -- `peer_send` reports
success (`queued_watcher`) and the pane never turns. Recommendation (A) is a
SYNCHRONOUS Stop hook that consumes the peer inbox at every turn boundary;
this is it.

MUST run synchronously (no `"async": true` in hooks.json). Measured live
against the actual codex-0.149.0 binary this fleet runs (isolated proof, a
dev-checkout workbench evidence report, not part of this shipped bundle):
an async Stop hook's `decision` output is discarded -- Codex does not wait
for it, so an
async hook can report but can never gate or continue a turn. A synchronous
hook returning `{"decision": "block", "reason": "..."}` DOES force Codex to
continue the turn, feeding `reason` back as the next input -- confirmed live,
nine consecutive forced continuations in one test run.

This hook never parks for a FUTURE delivery. `solet wake --max-wait 0` is a
bounded observation of the ALREADY-ARMED sidecar spool. Native qualified
driving and bounded reconciliation own future delivery. The paired eight-second
registration leaves a small startup margin around the two-second wake and
honesty-report subprocess limits.

`solet wake` is the SAME primitive Claude's `wake_waiter.py` blocks on, read
non-blockingly here instead: it reads the session's ALREADY-ARMED sidecar
watcher's spool (both Codex host drivers, `codex_tmux.py` and
`codex_app_server.py`, arm the same `--no-claim` sidecar `tmux_adapter.py`
arms for Claude, so the spool exists for a managed Codex worker today) and
reports the NEW lines since the last time anything read it. A pending result
forces one continued model turn, which is the sole reader of the durable inbox.
This hook never invokes ``inbox`` or otherwise drains message content.

Part C, the honesty field: `report_inbox_consumption` is called only after a
successful `pending` or `empty` observation. That call, not the block/continue
decision, is what lets
`session_inbox_consumption_status` distinguish "this session's consumer has
never run" (no row, `resolved=False`) from "it ran and found nothing" (a
fresh `checked_at`, null `pending_found_at`) from "it ran and found
something" (both timestamps present) -- so a `queued_watcher` delivery can
never again be mistaken for a delivery that was actually consumed.

Armed ONLY when ALL of the following hold (silent no-op otherwise), same
mechanical-precondition posture as `wake_waiter.py` and
`context_status_reporter.py`'s `_fleet_environment()`:
  - AGENT_INSTANCE_ID is set (the reporting session's own id);
  - AGENT_SESSION_ID is set (the per-session watch spool path derives from
    it, and it is the routing-join field on the honesty report);
  - AGENT_WAKE_CLI is set and points to an executable file (the operator's
    launcher names the CLI to run).
An operator-launched (non-fleet) Codex session or a `host=operator` seat
therefore does nothing here -- expected, not an error.

Exit contract: this hook ALWAYS exits 0. `decision:block` in the JSON stdout
payload is what asks Codex to continue, not a nonzero exit -- unlike
Claude's Stop-hook contract (exit 2 = wake), Codex's is JSON-body-driven
(confirmed live in the isolated proof above). A broken wake or report path
must never trap the session in a failing Stop hook. A wake timeout, exception,
malformed output, or non-contract exit is `unknown`: it is logged to stderr
and does not report a successful check. A report failure never changes a
completed pending/empty observation.

Python since 2026-08-08 (see `wake_waiter.py`'s own note): every runtime
this plugin uses declares `python3` as a guaranteed prerequisite, but Claude
Code / Codex both launch hooks with a bare `python3` resolved from PATH,
which on a stock macOS is frequently the system 3.9. The floor guard below
must stay the FIRST statement after `from __future__ import annotations`,
for the exact reason `wake_waiter.py` documents: an ImportError at module
level cannot be caught by anything inside this file.
"""

from __future__ import annotations

import sys

if sys.version_info < (3, 11):  # noqa: UP036 -- see module docstring; this
    # file ships to an ADOPTER's machine and is launched by whatever
    # `python3` their PATH resolves, not necessarily this repo's floor.
    print("{}")
    raise SystemExit(0)


import json
import os
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

REPORT_PROCESS_KEY = "plugin::agent_messaging_plugin::report_inbox_consumption"
RUNTIME = "codex"

# Keep these values coupled to hooks.json. The Stop hook must remain finite
# even if its configured bridge child hangs.
_WAKE_MAX_WAIT_S = 0
_CHILD_TIMEOUT_S = 2
_WAKE_EXIT_PENDING = 2
_WAKE_EXIT_EMPTY = 0

CheckOutcome = Literal["pending", "empty", "unknown"]

_NUDGE = (
    "Your peer-message inbox has unread deliveries that arrived while this "
    "session was not looking. Call the registered peer_inbox process for "
    "your own agent_session_id (or run the coordination CLI's merged inbox "
    "read) before ending this turn, and act on what you find."
)


def _reporter_surface(path: Path) -> str:
    normalized = path.resolve().as_posix()
    if "/.codex/plugins/cache/" in normalized:
        return "plugin_cache"
    if "/.ananta/releases/" in normalized:
        return "release"
    if "/plugins/github_midwife_plugin/codex_plugin/" in normalized:
        return "vendored"
    if "/.claude/hooks/" in normalized or "/.codex/hooks/" in normalized:
        return "checkout"
    return "unknown"


def _fleet_environment() -> tuple[str, str, Path] | None:
    values = {
        "AGENT_INSTANCE_ID": os.environ.get("AGENT_INSTANCE_ID", "").strip(),
        "AGENT_SESSION_ID": os.environ.get("AGENT_SESSION_ID", "").strip(),
        "AGENT_WAKE_CLI": os.environ.get("AGENT_WAKE_CLI", "").strip(),
    }
    present = {name for name, value in values.items() if value}
    if not present:
        return None
    if len(present) != len(values):
        # Partially armed: something set one fleet var but not the others.
        # Non-fatal by this hook's own exit contract, but worth a stderr
        # note -- silent here would hide a launcher misconfiguration.
        missing = sorted(set(values) - present)
        print(
            f"inbox_consumer: partially armed fleet reporter is missing {', '.join(missing)}",
            file=sys.stderr,
        )
        return None
    cli = Path(values["AGENT_WAKE_CLI"])
    if not cli.is_file() or not os.access(cli, os.X_OK):
        print(
            f"inbox_consumer: AGENT_WAKE_CLI is not an executable file: {cli}",
            file=sys.stderr,
        )
        return None
    return values["AGENT_INSTANCE_ID"], values["AGENT_SESSION_ID"], cli


def _check_pending(cli: Path) -> CheckOutcome:
    """Read the already-present spool once, without waiting for a future wake."""
    try:
        result = subprocess.run(
            [str(cli), "wake", "--max-wait", str(_WAKE_MAX_WAIT_S)],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            check=False,
            text=True,
            timeout=_CHILD_TIMEOUT_S,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        print(f"inbox_consumer: could not run the configured wake CLI: {exc}", file=sys.stderr)
        return "unknown"
    if result.stdout.strip():
        print("inbox_consumer: wake CLI emitted malformed stdout", file=sys.stderr)
        return "unknown"
    if result.returncode == _WAKE_EXIT_PENDING:
        return "pending"
    if result.returncode == _WAKE_EXIT_EMPTY:
        return "empty"
    if result.returncode not in (_WAKE_EXIT_EMPTY, _WAKE_EXIT_PENDING):
        print(
            f"inbox_consumer: wake CLI exited with status {result.returncode}",
            file=sys.stderr,
        )
    return "unknown"


def _report(
    cli: Path,
    *,
    agent_instance_id: str,
    agent_session_id: str,
    checked_at: str,
    pending: bool,
) -> None:
    """Record one successfully observed pending/empty state, best effort.

    a failed report never blocks the hook's own decision, it only leaves the
    honesty row stale, which `session_inbox_consumption_status` surfaces as
    exactly that (an old `checked_at`), not as a crash."""
    parameters: dict[str, Any] = {
        "agent_instance_id": agent_instance_id,
        "agent_session_id": agent_session_id,
        "runtime": RUNTIME,
        "checked_at": checked_at,
        "reporter_surface": _reporter_surface(Path(__file__)),
    }
    if pending:
        parameters["pending_found_at"] = checked_at
        parameters["pending_reason"] = _NUDGE
    try:
        proc = subprocess.run(
            [
                str(cli),
                "call",
                REPORT_PROCESS_KEY,
                json.dumps(parameters, separators=(",", ":"), sort_keys=True),
            ],
            capture_output=True,
            check=False,
            text=True,
            timeout=_CHILD_TIMEOUT_S,
        )
    except Exception as exc:  # noqa: BLE001 -- report faults cannot cancel a pending continuation.
        print(f"inbox_consumer: report_inbox_consumption call failed: {exc}", file=sys.stderr)
        return
    if proc.returncode != 0:
        detail = proc.stderr.strip() or proc.stdout.strip() or f"exit {proc.returncode}"
        print(f"inbox_consumer: report_inbox_consumption call failed: {detail}", file=sys.stderr)


def _run() -> dict[str, Any]:
    fleet = _fleet_environment()
    if fleet is None:
        return {}
    agent_instance_id, agent_session_id, cli = fleet
    try:
        hook_input: Any = json.load(sys.stdin)
    except json.JSONDecodeError as exc:
        print(f"inbox_consumer: hook stdin is malformed JSON: {exc.msg}", file=sys.stderr)
        return {}
    payload = hook_input if isinstance(hook_input, dict) else {}
    if payload.get("hook_event_name") != "Stop":
        # Bound only to Stop; a misconfigured registration should not crash.
        return {}
    checked_at = datetime.now(UTC).isoformat()
    outcome = _check_pending(cli)
    if outcome not in {"pending", "empty"}:
        if outcome != "unknown":
            print(f"inbox_consumer: invalid wake outcome: {outcome!r}", file=sys.stderr)
        return {}
    _report(
        cli,
        agent_instance_id=agent_instance_id,
        agent_session_id=agent_session_id,
        checked_at=checked_at,
        pending=outcome == "pending",
    )
    if outcome == "pending":
        return {"decision": "block", "reason": _NUDGE}
    return {}


def main() -> int:
    try:
        result = _run()
    except Exception as exc:  # noqa: BLE001 -- see module docstring's exit contract:
        # this hook must never trap the session in a failing Stop hook.
        print(f"inbox_consumer: unexpected error: {exc}", file=sys.stderr)
        result = {}
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
