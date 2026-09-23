#!/usr/bin/env python3
"""Behavioral proof that Codex reminder output is fixed and default-off."""

from __future__ import annotations

import json
import sys
from typing import Any

sys.dont_write_bytecode = True

from _harness import HOOKS_DIR, Results, preflight, run_hook  # noqa: E402

# "event" is each script's compiled-in DEFAULT (used when stdin is absent or
# junk) — a fallback, not the authority. The authoritative tag-vs-wiring
# assertion is `check_manifest_bound_events_echo`, which derives the expected
# events from hooks.json itself (§41, 2026-08-11: step_zero's hardcoded tag
# silently desynced when the cadence move rebound it to SessionStart).
REMINDERS = {
    "step_zero_reminder.js": {
        "event": "SessionStart",
        "context": (
            "For non-trivial work, checking two context sources in sequence "
            "before other work is usually faster than re-deriving an answer "
            "partway through: first any persistent knowledge base available "
            "to this session (via a local CLI or a connected tool, if any), "
            "then the current project's own instruction files. The sequence "
            "is not a substitution -- the knowledge base carries platform "
            "and cross-session knowledge, the instruction files govern the "
            "task at hand, and neither replaces the other."
        ),
    },
    "check_messages_reminder.js": {
        "event": "UserPromptSubmit",
        "context": (
            "Unread coordination messages from other sessions may be pending, "
            "if this project uses a peer-messaging or shared-inbox mechanism."
        ),
    },
    "role_binding_reminder.js": {
        "event": "SessionStart",
        "context": (
            "A session's local label and any external durable role binding are "
            "separate state and can disagree after a clear, restart, or transport "
            "reconnect. Presence alone is not evidence that the current session "
            "holds the role claim."
        ),
    },
}

DYNAMIC_SENTINELS = (
    "PROMPT-CONTENT-MUST-NOT-LEAK",
    "MESSAGE-CONTENT-MUST-NOT-LEAK",
    "ROLE-CONTENT-MUST-NOT-LEAK",
    "LABEL-CONTENT-MUST-NOT-LEAK",
)


def _parse(res: Results, stdout: str, label: str) -> dict[str, Any] | None:
    try:
        value = json.loads(stdout)
    except json.JSONDecodeError as error:
        res.check(False, label, f"invalid JSON: {error}")
        return None
    if not res.check(isinstance(value, dict), label, f"got {type(value).__name__}"):
        return None
    return value


def _armed(label: str) -> dict[str, str]:
    """An env that arms every reminder that HAS an arm condition.

    §7 parity re-key 2026-08-02 (mirrors claude_plugin's 2fb49dbf2): the
    three reminders no longer share one switch. step_zero is always armed (no
    condition at all), check_messages arms on AGENT_SESSION_ID (identity —
    the inbox is keyed on it), and role_binding still arms on
    AGENT_SESSION_LABEL because the label IS its content. Both variables are
    supplied here so "armed" means armed for all of them; the per-hook
    preconditions are asserted separately by the disarm legs.
    """
    return {"AGENT_SESSION_LABEL": label, "AGENT_SESSION_ID": f"ases-{label}"}


def _context(res: Results, script: str, *, label: str, stdin: str) -> tuple[str, str] | None:
    proc = run_hook(script, env=_armed(label), stdin=stdin)
    res.check(proc.returncode == 0, f"{script} exits 0", f"exit {proc.returncode}")
    res.check(proc.stderr == "", f"{script} writes no stderr", proc.stderr[:120])
    payload = _parse(res, proc.stdout, f"{script} output object")
    if payload is None:
        return None
    res.check(set(payload) == {"hookSpecificOutput"}, f"{script} has one top-level key")
    inner = payload.get("hookSpecificOutput")
    if not res.check(isinstance(inner, dict), f"{script} has hookSpecificOutput object"):
        return None
    assert isinstance(inner, dict)
    res.check(
        set(inner) == {"hookEventName", "additionalContext"},
        f"{script} emits measured two-field envelope",
        f"got {sorted(inner)}",
    )
    event = inner.get("hookEventName")
    context = inner.get("additionalContext")
    if not res.check(isinstance(event, str) and isinstance(context, str), f"{script} fields are strings"):
        return None
    assert isinstance(event, str) and isinstance(context, str)
    return event, context


#: Reminders that still have an arm condition, and the variable that arms them.
#: step_zero is deliberately ABSENT — it has no condition at all (§7).
_CONDITIONAL_REMINDERS = {
    "check_messages_reminder.js": "AGENT_SESSION_ID",
    "role_binding_reminder.js": "AGENT_SESSION_LABEL",
}


def check_disarmed_is_silent(res: Results) -> None:
    """Each CONDITIONAL reminder is silent without ITS OWN variable.

    ★ §7 parity 2026-08-02 (mirrors claude_plugin's 2fb49dbf2) — this leg no
    longer covers step_zero, and the omission is the point: step_zero is now
    unconditional, so a "disarmed step_zero" case would assert the opposite
    of the contract. Its inverted leg is `check_step_zero_fires_everywhere`
    below.
    """
    for script, var in _CONDITIONAL_REMINDERS.items():
        proc = run_hook(script, env={}, stdin="{not-json")
        res.check(proc.returncode == 0, f"{script} disarmed exits 0")
        res.check(proc.stdout == "", f"{script} disarmed has no stdout ({var} absent)", proc.stdout[:120])
        res.check(proc.stderr == "", f"{script} disarmed has no stderr", proc.stderr[:120])


def check_step_zero_fires_everywhere(res: Results) -> None:
    """★ INVERTED LEG (§7 parity): step_zero fires with NO environment whatsoever.

    The old leg asserted silence when unlabelled; that emission is now the
    CONTRACT. RED MUTATION for this leg: re-add ANY env condition to
    step_zero_reminder.js.

    Byte-identical bare vs armed is asserted rather than merely "non-empty",
    because a hook that emitted DIFFERENT text without fleet context would
    still be leaking a deployment assumption into the literal.
    """
    script = "step_zero_reminder.js"
    bare = run_hook(script, env={})
    res.check(bare.returncode == 0, f"{script} exits 0 with no env", f"exit {bare.returncode}")
    res.check(
        bare.stdout != "",
        f"{script} FIRES with no env at all (awareness is unconditional)",
        "emitted nothing — a re-added env condition is the likely cause",
    )
    res.check(bare.stderr == "", f"{script} writes no stderr with no env", f"got {bare.stderr[:120]!r}")
    armed = run_hook(script, env=_armed("Coordinator-Codex"))
    res.check(
        bare.stdout == armed.stdout,
        f"{script} output is BYTE-IDENTICAL armed vs unarmed",
        "output differs — the literal is carrying fleet context",
    )


#: Scripts that echo stdin's hook_event_name (two-value allowlist); the rest
#: emit a fixed tag. step_zero joined 2026-08-11 (§41).
ECHO_AWARE = ("step_zero_reminder.js", "check_messages_reminder.js")


def check_exact_fixed_output(res: Results) -> None:
    hostile_payload = json.dumps(
        {
            "hook_event_name": "UserPromptSubmit",
            "prompt": DYNAMIC_SENTINELS[0],
            "message": DYNAMIC_SENTINELS[1],
            "role": DYNAMIC_SENTINELS[2],
        }
    )
    for script, expected in REMINDERS.items():
        first = _context(res, script, label="Coordinator-Codex", stdin=hostile_payload)
        second = _context(res, script, label=DYNAMIC_SENTINELS[3], stdin="{not-json")
        if first is None or second is None:
            continue
        res.check(first[1] == second[1], f"{script} context is invariant across payload and label")
        res.check(second[0] == expected["event"], f"{script} emits the documented default event")
        if script in ECHO_AWARE:
            res.check(
                first[0] == "UserPromptSubmit",
                f"{script} echoes the declared supported event",
            )
        else:
            res.check(first[0] == expected["event"], f"{script} emits its fixed event")
        res.check(first[1] == expected["context"], f"{script} emits the exact reviewed literal")
        for sentinel in DYNAMIC_SENTINELS:
            res.check(sentinel not in first[1], f"{script} does not relay {sentinel.split('-')[0].lower()} data")


def check_step_zero_async_clause_deliberately_absent(res: Results) -> None:
    """§33.1 parity ruling (Dawn, 2026-08-02): the Claude sibling hook carries an
    async-non-blocking clause because MCP's process_call is genuinely
    asynchronous; this hook omits it because the Codex-side lookup path (CLI
    `solet call`, see AGENTS.md's Step Zero) blocks/polls for its result
    before returning (agent_messaging_plugin/local_cli/cli.py's
    call_and_wait) -- a real mechanism difference, not drift. Named explicitly
    so a future parity audit does not re-flag the asymmetry.

    RED MUTATION: add any "block"/"wait"/"asynchronous" language back here
    without the CLI path itself becoming non-blocking.
    """
    context = REMINDERS["step_zero_reminder.js"]["context"]
    res.check(
        "before other work" in context,
        "step_zero (codex) carries the ordering-primacy claim",
        f"got: {context!r}",
    )
    for term in ("async", "block", "wait"):
        res.check(
            term not in context.lower(),
            f"step_zero (codex) omits {term!r} -- the CLI lookup path is synchronous",
            f"got: {context!r}",
        )


def check_shared_event_echo(res: Results) -> None:
    for script in ECHO_AWARE:
        expected = REMINDERS[script]["context"]
        default = REMINDERS[script]["event"]
        for event in ("UserPromptSubmit", "SessionStart"):
            got = _context(
                res,
                script,
                label="Coordinator-Codex",
                stdin=json.dumps({"hook_event_name": event, "prompt": DYNAMIC_SENTINELS[0]}),
            )
            if got is not None:
                res.check(got == (event, expected), f"{script} echoes only supported event {event}")
        unknown = _context(
            res,
            script,
            label="Coordinator-Codex",
            stdin=json.dumps({"hook_event_name": "Stop"}),
        )
        if unknown is not None:
            res.check(
                unknown[0] == default,
                f"{script} degrades an unsupported event to its own default {default}",
            )


def check_manifest_bound_events_echo(res: Results) -> None:
    """★ §41 (2026-08-11): the emitted tag must match the WIRING — derived, not asserted.

    The cadence move rebound step_zero to SessionStart while its output still
    hardcoded "UserPromptSubmit"; a host that validates the declared event
    name against the firing event discards the output silently, and this
    suite stayed green because REMINDERS asserted the stale literal as
    correct. This leg derives each reminder's bound events from hooks.json
    itself and asserts the script emits exactly the event that fired, so the
    assertion can never desync from the wiring again.

    RED MUTATION for this leg: hardcode any hookEventName a reminder's own
    hooks.json entry does not wire it to, or rebind one to an event it
    cannot echo.
    """
    for script, events in _manifest_bound_events(tuple(REMINDERS)).items():
        res.check(bool(events), f"{script} is wired in hooks.json", "no binding found")
        for event in events:
            _assert_echoes_bound_event(res, script, event)


def _manifest_bound_events(scripts: tuple[str, ...]) -> dict[str, list[str]]:
    """Derive {script: [bound events]} from hooks.json — the wiring is the authority."""
    manifest = json.loads((HOOKS_DIR / "hooks.json").read_text())
    bound: dict[str, list[str]] = {script: [] for script in scripts}
    for event, groups in manifest.get("hooks", {}).items():
        for command in _group_command_strings(groups):
            for script in scripts:
                if command.endswith(f"/{script}"):
                    bound[script].append(event)
    return bound


def _group_command_strings(groups: list[dict[str, Any]]) -> list[str]:
    """Flatten one event's hook groups into the command strings that name scripts."""
    commands: list[str] = []
    for group in groups:
        for entry in group.get("hooks", []):
            command = entry.get("command", "")
            if isinstance(command, str) and command:
                commands.append(command)
    return commands


def _assert_echoes_bound_event(res: Results, script: str, event: str) -> None:
    got = _context(
        res,
        script,
        label="Coordinator-Codex",
        stdin=json.dumps({"hook_event_name": event}),
    )
    if got is not None:
        res.check(
            got[0] == event,
            f"{script} emits its manifest-bound event {event}",
            f"got {got[0]!r} — the emitted tag has desynced from hooks.json wiring",
        )


def main() -> int:
    preflight()
    res = Results("Codex reminder")
    check_disarmed_is_silent(res)
    check_step_zero_fires_everywhere(res)
    check_exact_fixed_output(res)
    check_step_zero_async_clause_deliberately_absent(res)
    check_shared_event_echo(res)
    check_manifest_bound_events_echo(res)
    return res.finish()


if __name__ == "__main__":
    raise SystemExit(main())
