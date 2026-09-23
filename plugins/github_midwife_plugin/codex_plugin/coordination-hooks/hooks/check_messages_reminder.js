#!/usr/bin/env node
"use strict";

// SessionStart + UserPromptSubmit hook. It reads only hook_event_name so the
// same fixed reminder can use Codex's measured hookSpecificOutput envelope for
// either event. No prompt or message content is copied into the output.
//
// ARM CONDITION: AGENT_SESSION_ID present — IDENTITY, not label. This is a
// FUNCTIONAL precondition, never a protection: the inbox is keyed on identity,
// so without one there is no addressable inbox and the reminder would advertise
// an action that cannot resolve. Re-keyed from AGENT_SESSION_LABEL on
// 2026-08-02 (§7 parity with the claude_plugin copy, 2fb49dbf2); the label was
// never what made the inbox reachable.

if (!process.env.AGENT_SESSION_ID) {
  process.exit(0);
}

let raw = "";
try {
  raw = require("fs").readFileSync(0, "utf8");
} catch {
  raw = "";
}

let eventName = "UserPromptSubmit";
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
  // Malformed or absent stdin degrades to the harmless prompt-event default.
}

const output = {
  hookSpecificOutput: {
    hookEventName: eventName,
    additionalContext:
      "Unread coordination messages from other sessions may be pending, " +
      "if this project uses a peer-messaging or shared-inbox mechanism.",
  },
};

process.stdout.write(JSON.stringify(output) + "\n");
