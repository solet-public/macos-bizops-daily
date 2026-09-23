# Release-Pointer Paths: Surviving a Blue-Green Cutover

Article Layer: 1

Article Role: plugin_reference

Tags: knowledge:tag:plugin_reference, knowledge:tag:agent_messaging, knowledge:tag:blue_green, knowledge:tag:release_management, knowledge:tag:cli_resolution

Article Tags: planning-stage:server-side-internals, planning-stage:operator-runbook, evidence-category:invariant-contract, evidence-category:local-ops-contract, domain:agent-messaging, domain:blue-green-deployment, domain:release-management

Embedding Description: Diagnosing and preventing the failure where a long-running process stops reporting, stops waking, and drops out of the fleet listing shortly after a deploy, with no error in any log. Blue-green cutover garbage-collects old releases, so an executable path pointing inside a versioned release directory is a time bomb: when the release is reaped the binary vanishes and every non-fatal caller of it fails silently rather than loudly. The remedy is to express the path through the deployment's atomically-swapped current symlink, and to leave that symlink UNRESOLVED — resolving it yields a path that works today and re-pins the version for tomorrow, a failure no liveness check can see. Also covers refusing the rewrite during a mid-cutover version skew, falling through unchanged whenever the rewrite does not qualify, and reading a live process's environment to confirm which form of the path it actually holds.

## Purpose

A fleet worker cannot coordinate without the CLI. Its heartbeat, its idle wake,
and the watch process that keeps it listed in `peer_list` are all subprocess
calls to that binary, and every one of them is deliberately non-fatal — so when
the path goes bad, the worker does not crash. It goes quiet. Reach for this
article when a worker is productive but has vanished from `peer_list`, when a
blue-green cutover is followed by workers dropping out of the fleet, when a
heartbeat stops landing with nothing in any log, or before changing how a host
adapter builds a worker's environment.

The subject here is reachability of the binary — whether the executable a
worker was handed still exists after the platform underneath it has been
replaced. Questions about who a worker is, what it is called, or what it is
allowed to do are answered elsewhere in this knowledge base and are out of
scope for this contract.

## Symptom: a worker stops reporting after a deploy

The presentation is distinctive and worth recognizing directly, because it does
not look like a bug in anything. A worker that was reporting normally stops
reporting shortly after a deploy. Its heartbeat no longer lands, so its
liveness row stops advancing and the overdue sweep eventually reaps it. It no
longer wakes on delivery, so messages sit unread until someone looks. It is
gone from `peer_list`, so sends addressed to it fail to resolve. The worker
itself is fine — still running, still working, still producing — which is
exactly why the report reads as "the worker went dark" rather than as any
error.

Nothing is logged, because nothing failed in the sense any of these callers
check for. Each is a subprocess invocation that is deliberately non-fatal, so a
missing executable is caught, ignored, and exited zero. If the timing lines up
with a deploy, and several workers went quiet together, the CLI path is the
first thing to check — not the messaging layer, not the workers themselves.

## One resolution point, three consuming surfaces

`resolve_solet_bin` in `solet_cli` is the single place a worker's CLI path is
decided. It tries the ambient `PATH` first, then the console script sitting
beside the active interpreter — the deterministic second rung, because a
materialized release runs with a minimal `PATH` that excludes its own
`venv/bin`. It returns an empty string when neither rung resolves, and callers
decide whether that is fatal or merely degrading.

What matters for correctness is that the returned path does not stay in one
place. It reaches three surfaces with different lifetimes:

**The spawn environment.** `expose_worker_cli` writes the absolute binary to
`AGENT_WAKE_CLI` and prepends its directory to `PATH`. The absolute value
serves consumers that exec the binary directly; the prepend serves consumers
that invoke a bare command name. Both halves are needed, because a worker's own
tool-issued shell commands may be initialized from a login profile that rebuilds
`PATH` and discards the prepend.

**The presence sidecar's argv.** `watch_sidecar_argv` puts the same path in
`argv[0]` of the long-lived watch process — the process that actually holds the
worker's registered presence, and therefore the reason it appears in
`peer_list`. This surface is the one most often forgotten, and it has the
largest consequence: if it breaks, the worker is not merely un-woken, it
disappears from the fleet listing entirely and every addressed send to it
fails to resolve.

**The Codex configuration triple.** A managed Codex worker additionally
receives the path through `worker_path` as its
`shell_environment_policy.set.PATH`, and through its MCP server environment as
`AGENT_WAKE_CLI`. Three surfaces, one resolved value.

Because all three derive from one function, a correctness property belongs at
that function. Applying it at the environment builder alone is the tempting
mistake: it fixes wake and leaves registration broken, which is strictly worse
than leaving both broken, because the fleet then looks healthy.

## Why a versioned path is not a durable answer

Under blue-green deployment the platform runs from a materialized release
directory named for its timestamp and commit, and the release manager keeps only
the last few releases — old ones are garbage-collected on cutover. Every path
either resolution rung returns therefore points inside a directory with a
limited lifetime.

A worker that outlives a deploy is the normal case, not the exception. When its
release is reaped, its `AGENT_WAKE_CLI`, its `PATH` prepend, and its watch
sidecar all dangle at the same instant. Nothing raises. The heartbeat hook
catches the file-not-found and exits zero, the wake waiter finds no CLI to
block on, and the sidecar's process is simply gone — so the worker drops out of
`peer_list` and stops stamping liveness in the same moment. The fleet's view of
that worker goes dark while the worker itself keeps working, unaware, and the
symptom presents as an unexplained disappearance shortly after a cutover rather
than as any error anyone can find.

## The rule: express the path through the stable pointer

Alongside the versioned release directories, the deployment maintains a
`current` symlink that cutover swaps atomically. `stable_release_path` rewrites
a resolved binary path onto that pointer: it walks the path's ancestors for a
release directory whose sibling `current` link currently names that same
release, then substitutes the pointer for the release directory, preserving the
rest of the path, and confirms the result stats as an executable file.

`resolve_solet_bin` applies this once, so all three surfaces above inherit it
with no call-site duplication.

### The path must land unresolved

This is the requirement that is easy to satisfy accidentally and easy to break
accidentally. Comparison uses the raw link text and path normalization, and the
rewrite never resolves the symlink.

A rewrite that resolves it produces a path that still names a real file today.
Every ordinary check passes: the file exists, it is executable, it runs. And
the version pin is fully intact, so the next cutover dangles it exactly as
before. The failure is invisible to any check that asks whether the path works
now; it is only visible to a check that asks whether the path contains a
release identifier. State the property that way when testing it: the release
identifier must be textually absent from what reaches the worker.

### Mid-cutover skew is refused, not substituted

If `current` already names a different release than the path came from, the
rewrite does not happen and the versioned path is returned unchanged. During a
cutover the pointer moves before every process has caught up, and silently
redirecting a worker to a release its spawner is not running would be a version
substitution nobody asked for. Declining is honest and costs nothing: the
worker keeps the behaviour it would have had anyway.

### Refusals fall through, they never raise

Every case that does not qualify returns the original path: a path outside any
release layout, a release directory with no pointer beside it, a pointer whose
target has already been reaped, a pointer that vanishes in the rename window
during cutover. This preserves the standing guard on the consuming side, where
a hook widens its `PATH` search by the CLI's directory only when that directory
genuinely contains the binary. A dangling value contributes nothing anywhere,
so a session that would have resolved the CLI from its ambient `PATH` is never
made worse by any of this — and no spawn is ever refused for a reason this
rewrite invented.

## Observing whether the stable path took

The property is directly observable on a live worker, and reading the value is
the check — not whether the worker seems healthy, which it will right up until
the next cutover.

Read the coordination CLI variable out of a spawned worker's environment. For a
tmux-hosted worker, `tmux show-environment -t <session> AGENT_WAKE_CLI` is the
correct instrument; reading the variable from inside a tool-issued shell is
not, because that shell may be re-initialized from a login profile. The value
must contain the `current` pointer segment and must contain no release
identifier. A release identifier in that value means the spawn path did not
apply the rewrite.

The same observation is the acceptance test for the mechanism as a whole: after
a cutover, workers spawned since the fix should keep their heartbeat, their
registered presence, and their wake, where previously all three went dark
together.

## What this does not do

The rewrite happens at spawn time. A worker that is already running keeps
whatever path it was spawned with, and stays exposed to the next reap until it
is rotated or respawned. Landing this changes what new spawns inherit; it does
not reach backwards into live sessions. Clearing existing exposure across a
standing fleet is a separate, deliberate act.
