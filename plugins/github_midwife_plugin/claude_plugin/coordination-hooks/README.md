# coordination-hooks

Fleet coordination hooks for multi-session Claude Code workflows:

- **Knowledge-base-first reminder** (`SessionStart`) — suggests checking
  this project's documented conventions before non-trivial work. A reminder,
  not an enforced gate: the underlying search this suggests is asynchronous,
  so the hook cannot block on its result. Unconditionally armed — no
  environment variable disarms it, unlike every other hook below — so a
  session never silently loses awareness that the mechanism exists.
- **Check-your-messages reminder** (`SessionStart`) —
  a plain nudge to check for unread coordination messages. Intentionally has
  no backend dependency: it always fires the same reminder and lets the
  model's own tool call determine whether anything is actually there.
- **Role-binding reminder** (`SessionStart`) — notes that a session's local
  label and any external, durable role binding are separate things that can
  disagree, so a binding made before a `/clear`, a restart, or a transport
  reconnect may still point at a previous session. Performs no lookup: it
  cannot tell whether a binding is actually stale, and deliberately does not
  try. It echoes the session's own `AGENT_SESSION_LABEL` and leaves both
  verification and re-assertion to the session's own tools.
- **Idle-wake waiter** (`Stop`) — opt-in per session (armed by environment
  variables, see Configuration), but the fleet this plugin ships with runs
  on it as the PRIMARY idle-wake mechanism as of 2026-08-06, not a
  fallback — see `SECURITY.md`'s "Operational status" note. Nudge-only.
  When a session with
  an `AGENT_SESSION_ID` goes idle, invokes exactly
  `$AGENT_WAKE_CLI wake --max-wait <seconds>` (fixed argv, no
  shell) — the operator-configured coordination CLI's blocking wait verb,
  bounded so the quiet case releases the turn boundary (see Configuration) —
  and **discards its output entirely**. On the wake signal (exit 2) it
  emits its own fixed nudge: deliveries are pending in the peer inbox. The
  session then fetches the actual messages itself via the normal tool
  path. The hook conveys exactly one bit of dynamic information —
  "deliveries pending" — plus timing; it never relays message content.
- **Git-mutation guard** (`PreToolUse`) — opt-in, nameable gate that blocks
  git mutations (commit, push, checkout, merge, rebase, etc.) and subagent
  spawning (the `Agent` tool, and `Task` under its former name) from any
  session other than the designated *git controller* session, regardless of
  which directory or repository a command targets. Scope is peer *mistake*
  prevention, not adversarial security — a session that wants to bypass the
  hook can edit it. See `SECURITY.md` for the cross-repository reach this
  implies and the disclosed residual.

  The refusal text carries a **single-active-session exemption**: where only
  one session is active, the policy does not apply. That exemption is
  language-level and the gate implements no part of it — it detects nothing
  new. A solo deployment's hydration simply never sets the arming variable,
  so the gate never arms at all; the clause exists for the transiently solo
  *fleet*, where a session relying on it must have a checkable basis and must
  cite that basis in band. An operator instruction to proceed always
  overrides.
- **Heartbeat report-alive** (`PostToolUse`, unconditional) — opt-in per
  session via `AGENT_INSTANCE_ID`. After a tool call completes, throttled to
  at most once per ~180s via a local marker file, it shells out to the
  fleet's `report_alive` CLI verb so a fleet-managed session's liveness stays
  current without a standalone background loop — the loop shape this hook
  replaced could survive a session `/clear` and keep reporting a dead
  session as live for hours, since it had no coupling to whether the process
  it vouched for was still doing anything. This hook has no persistent
  process of its own; it dies with the tool-call lifecycle it fires inside.
  Conveys no dynamic information back to the session — it never prints a
  `hookSpecificOutput` block, only an occasional stderr diagnostic on
  failure. See `SECURITY.md` for the full contract.
- **Rotation-due watch** (`PostToolUse`, unconditional) — opt-in per session
  via `AGENT_INSTANCE_ID`. The host-independent sibling of the heartbeat:
  reads the session's own transcript usage, compares it against a declared
  context ceiling, and on a threshold crossing sends ONE notification (via
  `peer_send_by_name` to the session's steward, or a local marker file when
  no steward is resolvable) — at most once per session generation. It never
  acts on the signal itself (never clears or rotates the session); rotation
  timing stays a steward/operator decision. Also opportunistically caches a
  lightweight context-status reading on every un-throttled tick, independent
  of the threshold check. Like the heartbeat, it prints nothing to stdout;
  diagnostics are stderr-only. See `SECURITY.md` for the full contract.
- **Installed-owner receipt guard** (`coordination_owner.py`, imported by the
  heartbeat, rotation-due, and wake entrypoints) — validates an immutable,
  owner-qualified installation receipt before any marker, lock, watcher, or
  bridge side effect. Missing, foreign, stale, corrupt, or undesignated roots
  refuse explicitly and inertly; the absolute receipt path is supplied as
  `AGENT_COORDINATION_RECEIPT_PATH`, never guessed from CWD.
- **Rotation-due notice** (`UserPromptSubmit`, unconditional) — the delivery
  half of the hook above, for the session it cannot deliver to. When the
  watch hook finds no steward to notify (an operator-present session, which
  has none), it writes the notice to a local marker instead of dropping it;
  this hook surfaces that marker as prompt context on the session's own next
  turn, once per session generation, then stamps the marker so it never
  repeats. It is the only hook here that prints a `hookSpecificOutput`
  block, so what it will inject is bounded: content comes from a marker file
  this hook checks is owned by the current user and not group/world-writable,
  and is otherwise carried verbatim. On the ordinary path — no marker — it
  reads stdin, globs at most two directories, and emits nothing. See
  `SECURITY.md` for the full contract.
- **Memory-passthrough capture** (`PostToolUse`, `Write|Edit|MultiEdit`) —
  when the touched file is under this agent's own memory directory,
  appends one journal record (path, hash, timestamp) locally. No content
  is emitted or transmitted; this is a local append-only log entry, never
  a network call.
- **Memory-passthrough session context** (`SessionStart` + `UserPromptSubmit`)
  — when the agent's memory directory exists, prints a fixed instruction
  template telling the agent exactly what to run to hydrate its local
  memory projection from canonical storage, or to drain locally-captured
  edits back to it. A hook subprocess has no platform bridge of its own,
  so this is how the loop reaches the agent at all — the agent then runs
  the commands itself via Bash.
- **Memory-passthrough CLI utilities** (`drain.py`, `hydrate_render.py`,
  `index_render.py`, `sync.py`) — NOT Claude Code hooks; none fire
  automatically. Agent-invoked directly via Bash, on the agent's own
  judgment, following the session-context hook's own printed
  instructions. `drain.py` reads the pending local journal and prints the
  exact upsert arguments for each entry; `hydrate_render.py` regenerates
  the local memory projection from an already-exported snapshot;
  `index_render.py` renders the budget-fitted `MEMORY.md` index (shared
  library for `hydrate_render.py`, plus its own standalone entry point);
  `sync.py` wraps hydrate/drain into one Bash call per direction and is
  the one CLI utility in this group that shells out, via the same
  PATH-resolved `solet` convention as the heartbeat/rotation-due
  watch hooks. See `SECURITY.md` for the full per-file contract.
- **Spawn-injected worker hooks** (`headless_tool_allowlist_gate.py`,
  `capture_session_mapping.py`) — NOT wired into this plugin's own
  `hooks/hooks.json` at all; a spawned headless/tmux worker's own host
  adapter (`agent_messaging_plugin`) references these files by path in a
  generated Claude Code `--settings` blob at spawn time, and they ship
  here purely as the fallback copy a born clone (no `.claude/hooks/` of
  its own) still carries. `headless_tool_allowlist_gate.py`
  (`PreToolUse`) enforces a spawned worker's spawn-time tool allowlist —
  unarmed by default, and FAIL-CLOSED (blocks on any parse error) when a
  caller does supply one, since this is the actual safety boundary for an
  unattended worker with no human to catch a hook bug.
  `capture_session_mapping.py` (`SessionStart`) writes one file-per-firing
  spool record mapping this worker's `AGENT_INSTANCE_ID` to its Claude
  Code `session_id`, for the platform's own session-ledger reconciliation
  — non-fatal by design, like the memory-passthrough capture hook. See
  `SECURITY.md` for the full contract of each.

## Cadence ruling (2026-08-11)

The knowledge-base-first and check-your-messages reminders fire on
`SessionStart` (`startup`, `resume`, `clear`) only, not `UserPromptSubmit` —
both used to re-fire on every prompt turn, which accumulated one copy per
turn in the transcript. This changes CADENCE ONLY: the 2026-08-01 ruling
that the knowledge-base-first reminder is unconditionally armed (no
environment gate) is unchanged and still enforced by
`tests/reminder_hooks_smoke.py`'s `check_step_zero_fires_everywhere`.

**Tag-echo fix (2026-08-11, §41):** the cadence move initially shipped with
`step_zero_reminder.py` still hardcoding `"hookEventName":
"UserPromptSubmit"` — Claude Code rejects a hook output whose declared
event name mismatches the firing event (debug-level only), so the reminder
silently never landed after the rebinding. All three reminders now read
`hook_event_name` off stdin and echo it back, and
`check_manifest_bound_events_echo` derives each reminder's expected events
from `hooks.json` itself, so a hardcoded tag can never silently desync from
the wiring again.

The
memory-passthrough session-context hook still fires on both
`SessionStart` and `UserPromptSubmit` — it was never in scope for this
move, since it is not a reminder, it is the context-gauge/hydrate-drain
instruction surface.

## Configuration

Fourteen of the fifteen hooks are default-off, each guarded on either an
environment variable or a filesystem-presence check, and are silent
no-ops when their guard is absent — so unrelated Claude Code sessions on
the same machine, or a session in a project with no memory directory, are
unaffected by them. The knowledge-base-first reminder is the one
exception: it is unconditionally armed. `check_messages_reminder.py` and
the idle-wake waiter key on `AGENT_SESSION_ID`; the role-binding reminder
keys on `AGENT_SESSION_LABEL`; the heartbeat and rotation-due watch hooks
key on `AGENT_INSTANCE_ID` (throttling additionally uses
`AGENT_HEARTBEAT_MARKER_DIR` when set — see `SECURITY.md` for the fail-open
behavior when it is absent but the instance id is present); the
memory-passthrough capture and session-context hooks key on whether this
agent's own memory directory exists at all — no environment variable
arms or disarms either (a bare session in a project with no memory
directory sees zero output/effect from both). The two spawn-injected
worker hooks key on their own adapter-supplied variables:
`headless_tool_allowlist_gate.py` enforces only when
`FLEET_HEADLESS_TOOL_ALLOWLIST` is set (even to an empty string);
`capture_session_mapping.py` writes only when both
`ANANTA_SESSION_MAPPING_SPOOL_DIR` and `AGENT_INSTANCE_ID` are set —
neither is meaningful outside a spawned worker's own generated settings,
so an ordinary interactive session never arms either.

The git-mutation guard is separately opt-in via `GIT_CONTROLLER_NAME`: unset,
the guard is fully OFF (every session may run git and spawn subagents).
Set it to the session `name` (per Claude Code's `/rename`) that should be
allowed to perform git mutations and spawn subagents; every other session is
blocked.

The idle-wake waiter is separately opt-in via `AGENT_WAKE_CLI`: unset, it is
fully OFF. Set it to the coordination CLI the hook should invoke; the hook
runs exactly that executable with a fixed argument vector — the literal
subcommand `wake`, the literal flag `--max-wait`, and the bounded wait in
seconds (the compiled-in default, or `AGENT_WAKE_MAX_WAIT_S` when it parses
as a positive integer; anything else is announced on stderr and falls back,
so raw environment text never reaches the argv) — no shell involved. The
bound is what lets the harness stamp the session idle at all; a delivery
still wakes the session immediately (see `wake_waiter.py`'s module
docstring for the measured incident behind it). A declared non-watch transport (`FLEET_TRANSPORT` set to any
**non-empty** value other than `watch`) disarms it even when the CLI variable
is set, so deployments whose live bridge connection already does the waking
never double-arm. An unset **or empty** `FLEET_TRANSPORT` is not a
declaration and leaves the waiter armed — a hydration bug that exports an
empty value must not silently kill wakes on a watch deployment.

## Design notes

- Every hook uses exec-form invocation (`command` + a separate `args` array)
  — no shell string interpolation, no injection surface.
- Every hook in this plugin is stdlib-only Python (`python3`) — no
  interpreter discovery, no vendored binary, no runtime install step,
  except one disclosed, narrow exception: `rotation_due_watch.py` imports
  `agent_messaging_plugin.rotation_thresholds`, a zero-third-party-
  dependency SAME-PLATFORM module (not a PyPI package) that ships alongside
  this plugin in every capability bundle, resolved via `CLAUDE_PROJECT_DIR`
  and imported inside a `try`/`except` that degrades gracefully (skips the
  threshold computation, never crashes) if it is ever absent. The
  memory-passthrough CLI utilities import each other directly
  (`hydrate_render.py` imports `index_render`; `sync.py` imports `drain`
  and `hydrate_render`) to avoid an extra process layer — local modules
  within this same plugin, not a new external dependency, same reasoning
  as any hook importing its own underscore-prefixed sibling. `python3`
  itself is a guaranteed platform prerequisite, unlike this plugin's prior
  Node implementation of four of these hooks, which depended on a runtime
  nothing guaranteed and could silently fail to launch with no signal
  Claude Code surfaces to the user (measured 2026-08-08; retired the same
  day).
- The git-mutation guard never executes target-controlled code — it only
  inspects the tool-call payload Claude Code already passes on stdin.
- No hook calls out to any external service or database directly. The
  three reminders and the gate execute nothing at all. Four hooks/
  utilities are disclosed exceptions with a bounded, fixed-shape
  contract: the wake waiter executes exactly one operator-configured
  local command (`$AGENT_WAKE_CLI wake --max-wait <seconds>`, fixed
  argv, no shell), discards its output, and emits only its own
  compiled-in text; the heartbeat and rotation-due watch hooks each
  invoke the local `solet-bridge` CLI with a fixed `["solet-bridge", "call",
  "<fixed process_key>", <JSON payload>]` argv (no shell) and write only
  a small, secret-free timestamp marker file for throttling; `sync.py`
  (memory-passthrough, agent-invoked, never auto-fired) invokes the same
  `solet` CLI convention once per export and once per pending
  journal entry on drain. Four memory-passthrough files write more than
  a marker by design — `capture.py` appends one journal record,
  `hydrate_render.py`/`index_render.py` write this agent's own per-fact
  files and `MEMORY.md`, `_journal.py` is the shared library both use —
  but every write target is this agent's own already-resolved memory
  path, sourced from an already-exported snapshot or this agent's own
  journal, never a path or content built from raw tool-call/message
  data. Every string this plugin can ever inject into a session is a
  fixed literal visible in its source, with two disclosed exceptions:
  the role-binding reminder interpolates the session's own
  `AGENT_SESSION_LABEL` into its text (JSON-escaped, read from the
  process environment — never from stdin or message content), and the
  memory-passthrough session-context hook interpolates this session's
  own already-resolved paths and a pending-count integer into its fixed
  instruction template. The heartbeat, rotation-due watch, and
  memory-passthrough capture hooks inject nothing at all, ever — none
  has a `hookSpecificOutput` stdout path, only occasional stderr
  diagnostics. They are guardrails and plumbing, not a security
  boundary — see the hooks' own docstrings for the precise scope
  of each one.
- Reminder text is phrased as factual statements about project state
  ("unread messages may be pending"), never as imperative commands
  ("check your messages now"). The Claude Code hooks documentation
  ("Add context for Claude") states that text framed as out-of-band
  system commands can trigger Claude's prompt-injection defenses, which
  causes the text to be surfaced to the user instead of treated as
  context — factual framing avoids that failure mode.
- Reminder text names no project-specific mechanism (no fixed knowledge-base
  tool, no fixed messaging system) and is written as a condition ("if this
  project uses X") rather than an assumption that X exists — a project that
  has no such mechanism gets a harmless no-op statement instead of a
  reference to something Claude has no way to act on.
- The gate resolves the calling session's name from
  `~/.claude/sessions/<pid>.json`, a user-writable file — deliberate at
  mistake-prevention scope, and stated plainly in `SECURITY.md`.

## Security notes

`SECURITY.md` (same directory) enumerates the complete data-flow surface —
inputs, outputs, no-network/no-secrets/no-subprocess guarantees, the
threat-model boundary and its accepted bypasses, failure modes with the
allow-on-error rationale, and the supply-chain posture. It is written to be
read by a security reviewer before the code.
