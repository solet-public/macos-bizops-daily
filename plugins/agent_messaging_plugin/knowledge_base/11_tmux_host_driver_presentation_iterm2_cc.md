# tmux Host Driver Presentation — iTerm2 `-CC` Attach Is Optional, Not On The Spawn Path

Tags: knowledge:tag:plugin_reference, knowledge:tag:agent_messaging, knowledge:tag:session_lifecycle, knowledge:tag:host_driver, knowledge:tag:tmux, knowledge:tag:iterm2

Article Layer: 1

Article Role: plugin_reference

Article Tags: planning-stage:agent-to-agent-coordination, planning-stage:server-side-internals, evidence-category:host-adapter-contract, evidence-category:operations-runbook, domain:agent-messaging, domain:fleet-session-management

Embedding Description: How an operator visually attaches to a tmux-hosted fleet session via iTerm2's -CC control mode — a presentation-only layer that renders a detached tmux session as a native iTerm2 window through a dedicated tmux profile, completely separate from spawning or driving the session, with no iTerm2 Python-API code anywhere on the spawn/lifecycle path; covers the role-tag inspectability caveat (the terminal tag is live-only and never replays to a client that attaches later), the lingering-dead-window cleanup gap, and the profile prerequisite that this layer degrades gracefully without. Also documents the tmux host driver's swap-durability property — a tmux-hosted worker survives a blue-green deploy swap because its pane belongs to the independent tmux daemon, contrasted with the headless driver's workers, which are swap-mortal by construction (killed on every platform stop, including mid-swap, by the plugin's own shutdown path). Covers the driver channel's honest fire-and-forget, no-submission-ack contract (why a proposed capture-pane confirm-and-retry hardening was measured and parked, and why a lost nudge is non-fatal by design), and how to tell real unsubmitted pane input apart from the Claude Code TUI's own dimmed suggestion/ghost text at an idle prompt (transcript queue-operation records vs. `capture-pane -e` styling) when `capture-pane -p` alone cannot.

## Purpose

The `tmux` host driver (`tmux_adapter.py`, fleet session-management Phase B
§5) spawns and drives sessions entirely through the `tmux` CLI — detached
`new-session`, `has-session`, `kill-session`, literal-keystroke
`send-keys`. iTerm2's control-mode attach (`tmux -CC attach -t <host_ref>`)
is a separate, optional PRESENTATION step an operator takes afterward to
see a tmux-hosted session rendered as a native iTerm2 window instead of a
plain terminal pane. This article documents that layer. Per the D2 dispatch
scope, it is documented here, not automated — no iTerm2 Python-API code
ships in this lane; `-CC` attach is something an operator (or a human at a
terminal) runs by hand.

## What `-CC` attach does, and does not, touch

`tmux -CC attach -t <host_ref>` is a human action, run from a terminal
already configured with the dedicated iTerm2 `tmux` profile. It:

- Renders the tmux session's windows/panes as native iTerm2 windows/tabs
  (MEASURED, `workbench/2026-08-03_r1_tmux_single_substrate_spike/
  FINDINGS.md`, on the flagship machine with tmux 3.7b and iTerm2's Python
  API enabled).
- Does **not** touch `TmuxHostDriver.spawn()`, `alive()`, `terminate()`,
  or `driver_channel()` in any way — those all operate purely through the
  `tmux` CLI, whether or not anyone ever attaches with `-CC`. A session
  spawned, driven, and terminated by the adapter behaves identically
  whether zero or several operators attach to watch it.
- Is not required for the session to work. Attaching is purely for a human
  to *look at* the session; the adapter's own `send-keys`-based driver
  channel (`drive_session`, `clear_session`, `compact_session`) never needs
  a client attached to deliver text into the pane.

## Prerequisite: the dedicated `tmux` profile

Native rendering under `-CC` depends on iTerm2 having a profile — measured
here literally named `tmux` — with tmux integration enabled (iTerm2's own
"tmux Integration" preference). `TmuxHostDriver.verify_config()` does
**not** check for this profile: verify_config only gates what the spawn
path actually needs (the `tmux` binary, its version) — profile existence is
presentation-layer state, unobservable without the iTerm2 Python API, which
is out of scope for this lane. Consequence: on a machine where the profile
doesn't exist or isn't configured, `spawn`/`alive`/`terminate`/
`driver_channel` all work exactly as normal — only the native-window
rendering is unavailable, and the operator falls back to a plain
`tmux attach -t <host_ref>` (still fully functional, just not iTerm2-native).
This is the intended degradation: a rich-adapter dependency that fails
soft, never a hard requirement for the spawn/lifecycle contract (per the
platform's "no-iTerm2 UX contract" — every L1 verb works identically on
every adapter; adapters vary only in topology and inspectability).

## Role-tag inspectability caveat

The wrapped OSC 1337 `SetUserVar=role` tag (`tmux_support/emit_role_tag.sh`,
emitted once at spawn, before the pane execs into `claude`) is **live-only**
— MEASURED (FINDINGS.md phase 2/3): a tag emitted with no client attached
does not replay to a client that attaches later. Do not treat the terminal
tag as a source of truth for which session is which. The `managed_session`
ledger (L0) is the authoritative identity record; the terminal tag is a
convenience label for a human glancing at iTerm2 tabs while already
attached, nothing more. A tmux hook that re-emits the tag on
`client-attached` would close this gap but is not implemented in this
lane — it is presentation-layer automation of the same shape as `-CC`
itself, named as a follow-up in the D2 land report rather than solved here.

## Swap-durability: a tmux-hosted worker survives a blue-green deploy

A tmux-hosted session's pane belongs to the independent tmux daemon, not to
`agent_messaging_plugin`'s own process. Nothing on the platform's stop/swap
path (a blue-green deploy's quiesce step) touches the tmux daemon or its
panes, so a tmux-hosted worker is **swap-durable by construction** — it
survives a deploy that swaps the platform process out from under it.

This is a measured contrast (D2 live-acceptance evidence, 2026-08-04
13:07-08Z), not a theoretical claim: the `headless` host driver's workers are
**swap-mortal by construction** instead — each one is a direct child of the
platform process holding its stream-json stdin pipe, and the plugin's own
`HeadlessHostDriver.shutdown()` (wired into `plugin.py.stop_services`)
SIGTERM-then-SIGKILLs every tracked headless worker unconditionally whenever
the platform stops, including mid-swap. A headless worker observed this
directly at 13:07Z (pid 96001, idle with fully-landed work) — it exited the
moment the deploy's quiesce step ran, with no work lost only because its
lane tail was already landed before the swap.

Consequence for lane-scoped workers: prefer the `tmux` driver as the default
host for any worker expected to outlive a single release, once the
PTY-confirm fix (`TmuxHostDriver.spawn()`'s bounded expect loop for the
`--dangerously-load-development-channels` confirmation prompt) has landed.
A deploy procedure spawning or holding live headless workers needs an
explicit quiesce-or-accept decision, recorded in the deploy reason, rather
than assuming they survive.

## Known caveat: lingering dead windows on teardown

`TmuxHostDriver.terminate()` always runs `tmux kill-session`, which
reliably tears down the tmux side (the "native-session cleanup on
teardown" adapter requirement, satisfied on the tmux side). What it does
**not** do is close an iTerm2 window that was rendering that session under
`-CC` — iTerm2 leaves a "session ended" native window behind until either
the operator closes it by hand or something calls the iTerm2 Python API to
force-close it (`FINDINGS.md`'s own spike scripts did this via
`session.async_close(force=True)`). Because iTerm2 Python-API code is out
of scope for this lane, that cleanup step is a manual operator action (or a
future presentation-layer plugin's job) — not silently solved, not silently
ignored: named here so it isn't rediscovered as a surprise.

## The driver channel's honest contract: fire-and-forget, no submission ack

`_TmuxSendKeysDriverChannel.send()` (the `DriverChannel` behind
`drive_session`/`clear_session`/`compact_session`, and drive-on-delivery's
own notice) loads the payload into a private tmux buffer and BRACKETED-pastes
it, followed by a separate `send-keys Enter`. It injected literal keystrokes
via `tmux send-keys -l` until 2026-09-07; see the amendment below for the
measurement that retired that mechanism. tmux exposes no submission
acknowledgement for either call — there is no protocol-level way to confirm the target pane's
application actually consumed and submitted what was sent. This is stated
plainly rather than papered over: **submission failure at this layer is
undetectable, by construction, not by omission.**

A drive-on-delivery scoping proposal to add a bounded capture-pane
confirm-and-retry loop on top of `send()` was assessed and PARKED entirely
(2026-08-04) after empirical measurement, not merely discussed: the
confirm predicate (matching the sent text's prefix against the pane's
recent lines) cannot distinguish "still sitting unsubmitted in the input
box" from "already submitted and now showing because the receiving process
echoed or displayed it" — a minimal test fixture that just echoes what it
reads was enough to trigger a false positive on every single healthy send,
adding real latency (measured 1.786s) and sending spurious extra `Enter`
keystrokes into a live, non-empty session on the common case, not just an
edge case. Mechanism analysis also found no actual swallow path for
`send-keys -l` (raw literal key events plus a separate `Enter` key event —
not a paste buffer, so there is no bracketed-paste boundary to lose an
`Enter` at) and **zero confirmed field failures as of 2026-08-04**.

### AMENDMENT 2026-09-07 — two of those three findings are now retired

The park on **confirm-and-retry** STANDS, and on its original merits: a retry
deposits a second copy of real text into a live input buffer and still cannot
confirm itself, which is why `session_lifecycle_verbs._verify_drive_effect`
carries its own NO RETRY, EVER rule. Nothing below reopens that.

The two supporting claims underneath it do not survive measurement, and a
reader leaning on this section needs to know which parts are still load-
bearing:

* **"Zero confirmed field failures" is retired.** Near-identical
  text-not-delivered reports were recorded on 2026-08-24, 08-30, 09-02,
  09-06 and three times on 09-07.
* **"No actual swallow path for `send-keys -l`" is retired — measured
  false.** The reasoning was sound about the `Enter`, and wrong about the
  PAYLOAD. Against a real Claude Code TUI pane on 2026-09-07, a 10.8KB /
  142-line payload sent with `send-keys -l` arrived HEAD-TRUNCATED: the
  receiving model, asked for the first and last 25 characters of what it had
  received, reported filler from the middle of the payload plus the intact
  tail, and the head marker appeared nowhere in the pane's full scrollback.
  The identical payload delivered by `load-buffer` plus `paste-buffer -p`
  arrived intact. The loss is in the receiving TUI's raw-mode reader, not in
  tmux: the same `send-keys -l` burst into a plain `cat` delivered all 10829
  bytes, and into a Codex TUI pane arrived intact — which is why the
  bracketed-paste change is scoped to the Claude channel and `codex_tmux`
  was deliberately left on `send-keys -l` rather than changed by symmetry.

So the mitigation that was actually missing was never a confirm loop. It was
(a) an insert mechanism that does not lose the payload, and (b) a verifier
able to SEE a realistic payload — `verify_driven`'s population anchor required
the composer row to be a prefix of the driven text, which a collapsed
multi-line paste never is, so every large drive polled its full 120s deadline
and then reported a delivered drive as `drive_unverified`. Both landed
2026-09-07 with their own killing regressions. The confirm-loop objections
that parked the 2026-08-04 proposal do not apply to either: there is no added
latency (a confirmed drive now returns in ~2s where it previously burned the
full 120s timeout before failing), and no new false-positive surface (the
collapsed-paste anchor is keyed on the payload's OWN line count, with a
control regression proving it rejects another writer's paste).

The channel ships with one `load-buffer` plus one `paste-buffer -p` plus one
`send-keys Enter`: no confirm loop, no retry.

**Why this is acceptable — the safety net is the design, not a confirm
loop:**

1. Every drive-on-delivery send is a *waker carrying zero information* — the
   durable thread/role entry is the single source of truth regardless of
   whether the nudge lands (see `03_inter_agent_messaging.md`'s
   drive-on-delivery section). A lost nudge never loses data.
2. A lost nudge degrades to **pre-lane latency, never to silence** — the
   recipient still has every pre-existing delivery path (the durable inbox,
   the ordinary `queued_wake`/`queued_notification` mechanism), so the
   worst case is "no faster than before this lane existed," not "the
   message vanished."
3. The `report_by` / `sweep_overdue_sessions` staleness contract bounds how
   long a genuinely-stalled recipient can go unnoticed, independent of any
   single send's success — this is the platform's actual safety mechanism
   for silence, and it does not depend on this channel's send confirming
   anything.

If a real Enter-loss (or any other submission-failure) incident is
eventually confirmed with a live specimen, re-open this as a fresh
measurement — do not assume the parked design above still applies without
re-deriving it; the confirm-predicate flaw that killed it is a property of
*any* text-matching approach over `capture-pane`, not just the one that was
tried. A structurally different signal (e.g. cursor position via `tmux
display-message -p '#{cursor_x} #{cursor_y}'`, unverified) was named as a
research follow-on, not pursued.

## Diagnosing a pane: TUI ghost/suggestion text vs. real unsubmitted input

`tmux capture-pane -p` flattens styling — it cannot tell you whether text
sitting on the pane is something a human (or an injected `send-keys` call)
actually typed and left unsubmitted, or the Claude Code TUI's own
context-aware suggested/hint text rendered dimmed at an idle prompt.
Measured live (2026-08-04, two independent sessions, same day): a plain
`capture-pane -p` read of an idle pane showed a plausible, in-voice
sentence about that session's own pending work sitting where typed input
would sit — in both cases it was TUI-generated suggestion text, not
anything anyone or anything sent, and `-p`'s flattened output was
genuinely indistinguishable from real stranded input by inspection alone.

Two structural discriminators exist; use them before concluding a send was
lost or that a stray line is real:

1. **The session's own transcript queue-operation records are
   authoritative.** Real submitted input — whether typed by a human or
   injected via `send-keys` — leaves a record in that session's own
   transcript at the point it entered the submission pipeline. TUI-
   generated suggestion/ghost text leaves no such record anywhere, because
   it was never submitted — it is rendered, not entered. Checking "did this
   text actually reach the input pipeline" from the transcript side settles
   the question the pane capture cannot.
2. **`capture-pane -e`** preserves ANSI/styling escape codes (`-p` alone
   strips them). Genuine unsubmitted typed input renders in the pane's
   normal foreground; TUI suggestion text is typically dimmed. Re-capturing
   with `-e` and inspecting for a dim/faint SGR attribute around the
   suspect text is a fast, pane-local corroborating check when transcript
   access isn't convenient.

Do not treat a bare `capture-pane -p` reading of "unexpected text at an
idle prompt" as evidence of a lost send, a stuck input, or an injection —
confirm via one of the two discriminators above first.

## Operator quick reference

```sh
# Attach with native iTerm2 rendering (requires the dedicated tmux profile):
tmux -CC attach -t <host_ref>

# Attach without iTerm2 involvement at all (always works):
tmux attach -t <host_ref>

# See it without attaching (host_ref is the managed_session row's host_ref):
tmux capture-pane -t <host_ref> -p
```
