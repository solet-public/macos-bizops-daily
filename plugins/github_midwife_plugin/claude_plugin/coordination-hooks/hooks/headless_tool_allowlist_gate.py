#!/usr/bin/env python3
"""Fleet session-management Phase B, §6 permission-mode ruling (2026-08-03,
coordinator seat/operator authority) — PreToolUse hook enforcing a headless
worker's spawn-time tool allowlist.

Design, as of the 2026-08-03 posture (operator ruling, "we don't have any
restrictions now"): `bypassPermissions` is no longer forbidden for spawned
headless workers — the earlier same-night posture that extended the
launcher's own bypass-forbidden invariant to spawned sessions was reversed;
that invariant still stands for the interactively-launched fleet, it just no
longer extends here. This gate itself ships UNARMED BY DEFAULT (the adapter
only sets `FLEET_HEADLESS_TOOL_ALLOWLIST` when a caller supplies
`allowed_tools`) — when it IS armed, an out-of-list call is DENIED WITH A
CLEAN ERROR (not a hang) — the worker gets a refusal it can report/reason
about; the report-or-die sweep is the backstop for genuine wedges, not the
primary mechanism. The mechanism stays landed as shelf capability for
whenever usage data argues for arming it again.

Root-cause context for why this hook exists at all (empirically verified,
2026-08-03, bounded /tmp probes, never touching the live checkout): the
CLI's own `--allowedTools` flag is ADDITIVE ONLY — it does not restrict
anything beyond whatever the permission mode already allows, verified even
with `--setting-sources project` (excluding both user-scope AND
local-scope settings, both of which independently carry broad permissive
rules on this machine — the user's own `~/.claude/settings.json`
`permissions.defaultMode: bypassPermissions` and this checkout's
`.claude/settings.local.json`'s broad `Bash(...)` allow list). Only
`--disallowedTools` (subtractive) and a PreToolUse hook actually restrict.
A hook is chosen over an inverse `--disallowedTools` enumeration because an
enumeration is allow-by-default for any tool added in a future Claude Code
version — the wrong direction for a fail-closed design.

Opt-in + nameable, mirroring `git_controller_gate.py`'s contract: the gate
enforces ONLY when `FLEET_HEADLESS_TOOL_ALLOWLIST` is SET (even to an empty
string — an explicit blank means "this IS a gated headless worker, with
NOTHING extra allowed," distinct from unset, "not a gated session at all,"
the same explicit-blank-vs-None distinction this design uses throughout,
e.g. `resolve_host_adapter`'s host param). UNSET -> the gate is OFF (every
ordinary interactive/operator session, and any session this mechanism was
never injected into, is unaffected).

Hook contract: invoked by Claude Code as a PreToolUse handler. `exit(2)`
BLOCKS the tool; any OTHER exit is non-blocking per Anthropic docs.

UNLIKE `git_controller_gate.py` (disclosed mistake-prevention scope,
allow-on-error): this IS the actual safety boundary for an UNATTENDED
worker with no human to catch a hook bug, so this gate is FAIL-CLOSED —
any parse/exception path also returns 2, never 0.

Stdlib-only by design — fires outside the venv, no third-party imports.

This is a spawn-injected worker hook: a spawned headless/tmux worker's own
host adapter (`agent_messaging_plugin`) ALSO references this file by path in
a generated Claude Code `--settings` blob at spawn time, independent of the
`hooks.json` registration below. Both routes are deliberate, not redundant
by accident (seed feedback #40/S51.1, 2026-08-24): the adapter's own
`--settings` injection is what a managed policy's
`strictPluginOnlyCustomization: ["hooks"]` strips, silently, on every host
that carries that policy; the `hooks.json` registration is what survives it,
because a plugin already listed in the operator's `strictKnownMarketplaces`
keeps firing its own manifest-registered hooks under that exact policy
(proven empirically for this plugin's other hooks — see §43.1/#8's Case C).
A session under a policy that does NOT strip `--settings` gets this hook
fired from both routes for one PreToolUse call — harmless by design: `_decide`
is a pure function of the environment and the tool-call payload, so a second
firing with the same inputs reaches the identical block/allow verdict; there
is no state to double-write.
It ships here as the fallback copy a born clone (no `.claude/hooks/` at all)
still carries; the origin checkout's own
`.claude/hooks/headless_tool_allowlist_gate.py` is the primary copy and this
file must stay behaviorally byte-identical to it.
"""

from __future__ import annotations

import json
import os
import sys

ALLOWLIST_ENV = "FLEET_HEADLESS_TOOL_ALLOWLIST"


def _parse_allowlist(raw: str) -> frozenset[str]:
    """Comma-or-whitespace-separated tool names, e.g.
    ``"mcp__<solet>__peer_register,mcp__<solet>__process_call"``.
    An empty string parses to an empty frozenset (nothing extra allowed),
    never ``None``."""
    tokens: list[str] = []
    for chunk in raw.replace(",", " ").split():
        chunk = chunk.strip()
        if chunk:
            tokens.append(chunk)
    return frozenset(tokens)


def _decide(env: dict[str, str], payload: dict[str, object]) -> tuple[bool, str]:
    """Return ``(block, reason)``. Gate OFF (``False, ""``) when
    ``ALLOWLIST_ENV`` is entirely absent from ``env`` — the ordinary,
    non-headless session case."""
    if ALLOWLIST_ENV not in env:
        return False, ""
    allowlist = _parse_allowlist(env[ALLOWLIST_ENV])
    tool_name_raw = payload.get("tool_name", "")
    tool_name = tool_name_raw if isinstance(tool_name_raw, str) else ""
    if tool_name in allowlist:
        return False, ""
    return True, (
        f"tool {tool_name!r} is not in this headless worker's spawn-time "
        f"allowlist ({sorted(allowlist)!r}). This gate is armed for this "
        "session because a caller supplied an explicit allowlist (2026-08-04 "
        "posture: the gate ships unarmed by default and bypassPermissions is "
        "a permitted value — arming is opt-in per spawn, not a standing "
        "prohibition), so anything outside the supplied allowlist is denied "
        "rather than executed or silently skipped."
    )


def main() -> int:
    """Entry point. Returns ``2`` to BLOCK, ``0`` to ALLOW.

    FAIL-CLOSED: unlike the git-controller gate's mistake-prevention scope,
    this hook is the actual safety boundary for an unattended worker with
    no human to notice a hook bug — any parse/exception path blocks."""
    try:
        payload = json.loads(sys.stdin.read())
        if not isinstance(payload, dict):
            payload = {}
        block, reason = _decide(dict(os.environ), payload)
    except Exception:  # noqa: BLE001 — fail-closed scope: any fault blocks
        block, reason = True, "headless_tool_allowlist_gate raised while deciding (fail-closed)"

    if block:
        try:
            print(
                f"[headless-tool-allowlist-gate] BLOCKED: {reason}",
                file=sys.stderr,
            )
        except Exception:  # noqa: BLE001 — telemetry strictly best-effort
            pass
        return 2

    return 0


if __name__ == "__main__":
    sys.exit(main())
