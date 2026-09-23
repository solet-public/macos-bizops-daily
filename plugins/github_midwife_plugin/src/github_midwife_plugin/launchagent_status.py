"""LaunchAgent health parsing for installation readiness probes."""

from __future__ import annotations

import re

from .setup_adapter_runtime import CommandOutcome


def launchagent_health(outcome: CommandOutcome) -> tuple[bool, str, str | None]:
    """Read the launchd fields that distinguish a loaded crash loop."""

    if not outcome.ok:
        return False, "launchctl_print_failed", "launchagent_unavailable"
    state = _launchctl_field(outcome.stdout, "state")
    last_exit = _launchctl_field(outcome.stdout, "last exit (?:code|status)")
    runs = _launchctl_field(outcome.stdout, "(?:runs|run count)")
    if state != "running" or last_exit is None or runs is None:
        return False, "launchctl_status_unreadable", "launchagent_status_unreadable"
    try:
        run_count = int(runs)
    except ValueError:
        return False, "launchctl_status_unreadable", "launchagent_status_unreadable"
    if last_exit == "(never exited)":
        observed = f"state={state},last_exit={last_exit},runs={run_count}"
        return True, observed, None
    try:
        last_exit_code = int(last_exit)
    except ValueError:
        return False, "launchctl_status_unreadable", "launchagent_status_unreadable"
    observed = f"state={state},last_exit_code={last_exit_code},runs={run_count}"
    if last_exit_code != 0 and run_count > 1:
        return False, observed, "launchagent_crash_loop"
    return True, observed, None


def _launchctl_field(output: str, name: str) -> str | None:
    match = re.search(rf"(?m)^\s*{name}\s*=\s*([^\n]+?)\s*$", output)
    return match.group(1) if match is not None else None
