#!/usr/bin/env node
"use strict";

// SessionStart hook. The role label is deliberately NOT interpolated: the
// production reminder stays byte-fixed while still explaining the separation
// between a local label and an external durable role claim.

if (!process.env.AGENT_SESSION_LABEL) {
  process.exit(0);
}

const output = {
  hookSpecificOutput: {
    hookEventName: "SessionStart",
    additionalContext:
      "A session's local label and any external durable role binding are " +
      "separate state and can disagree after a clear, restart, or transport " +
      "reconnect. Presence alone is not evidence that the current session " +
      "holds the role claim.",
  },
};

process.stdout.write(JSON.stringify(output) + "\n");
