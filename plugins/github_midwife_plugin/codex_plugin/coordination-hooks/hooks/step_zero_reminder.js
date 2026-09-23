#!/usr/bin/env node
"use strict";

// SessionStart hook. ALWAYS ARMED: installed means armed, with no
// environment condition of any kind.
//
// The solet is a system-wide resource, so awareness of it is not
// fleet-only. This hook previously no-op'd unless AGENT_SESSION_LABEL was set;
// that gate was removed deliberately (operator ruling 2026-08-01), and the
// FAILURE DIRECTION INVERTED WITH IT: a silently disarmed awareness reminder
// means a session never learns the platform exists, which is the silent-absence
// class. Re-adding any env condition here is the red mutation for this hook's
// smoke leg.
//
// The emitted hookEventName is read off stdin and echoed back (two-value
// allowlist, mirroring check_messages_reminder.js), with the compiled-in
// default matching this hook's own manifest binding. It was previously a
// hardcoded literal, which silently desynced when the 2026-08-11 cadence
// move rebound this hook from UserPromptSubmit to SessionStart: a host that
// validates the declared event name against the event that invoked the hook
// discards the output, so the reminder never lands -- the same
// silent-absence class as an env gate. Found 2026-08-11, confirmed
// independently by an adopter (feedback Part 41); the red mutation for this
// is re-hardcoding any event name the manifest does not wire this hook to
// (`check_manifest_bound_events_echo`).
//
// The literal below is true wherever the plugin is installed: it names no
// deployment-relative path and no fleet-specific command, so it reads
// correctly in an arbitrary directory with zero fleet context. The emitted
// context is one compiled-in literal: it never includes the prompt, a
// message, a lookup result, or an environment value.
//
// No async-non-blocking clause here, unlike the Claude sibling hook: this
// platform's Codex-side lookup path is the CLI `solet call` form (see
// AGENTS.md's Step Zero), which blocks/polls for its result before
// returning (agent_messaging_plugin/local_cli/cli.py's call_and_wait) — a
// genuinely synchronous mechanism, not drift from the Claude wording.

let raw = "";
try {
  raw = require("fs").readFileSync(0, "utf8");
} catch {
  raw = "";
}

let eventName = "SessionStart";
try {
  const payload = JSON.parse(raw);
  if (
    payload &&
    (payload.hook_event_name === "UserPromptSubmit" ||
      payload.hook_event_name === "SessionStart")
  ) {
    eventName = payload.hook_event_name;
  }
} catch {
  // Malformed or absent stdin degrades to this hook's own bound event.
}

const output = {
  hookSpecificOutput: {
    hookEventName: eventName,
    additionalContext:
      "For non-trivial work, checking two context sources in sequence " +
      "before other work is usually faster than re-deriving an answer " +
      "partway through: first any persistent knowledge base available " +
      "to this session (via a local CLI or a connected tool, if any), " +
      "then the current project's own instruction files. The sequence " +
      "is not a substitution -- the knowledge base carries platform " +
      "and cross-session knowledge, the instruction files govern the " +
      "task at hand, and neither replaces the other.",
  },
};

process.stdout.write(JSON.stringify(output) + "\n");
