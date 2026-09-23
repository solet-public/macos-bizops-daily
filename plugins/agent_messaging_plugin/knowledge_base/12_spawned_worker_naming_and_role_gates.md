# Spawned-Worker Naming, the Mutation Guard, and the Three Role Gates

Article Layer: 1

Article Role: plugin_reference

Tags: knowledge:tag:plugin_reference, knowledge:tag:agent_messaging, knowledge:tag:session_lifecycle, knowledge:tag:role_model, knowledge:tag:host_driver

Article Tags: planning-stage:agent-to-agent-coordination, planning-stage:server-side-internals, evidence-category:identity-model, evidence-category:invariant-contract, domain:agent-messaging, domain:fleet-session-lifecycle

Embedding Description: What a spawned fleet worker is NAMED on its own machine and what that name does and does not buy — the local_name spawn field feeding both host drivers' --name, why a role-named worker passes the Git-Controller mutation guard on its first turn but is still unreachable by role-addressed peer_send_by_name until it claims explicitly, and the three separate gates that govern role collisions (spawn refuses a second live local name, peer_claim_role refuses a live different-session holder, a dead holder stays cheaply claimable on purpose).

## Purpose

Three behaviours in this plugin all look like "the platform stops you taking
someone else's role", and they are NOT the same gate. Reading any one of them
in isolation teaches a wrong general rule. This article states all three in one
place, plus the naming mechanism underneath them, so the shape is legible.

Companion: `09_role_model_act_time_recheck.md` is the CONTRACT for role
resolution and the claim/displace mechanic. This article is about the SPAWN
path and the machine-local name — a different question that happens to touch
the same vocabulary.

## `local_name` — the name the worker answers to on its own machine

`spawn_session` carries `local_name`: the name the spawned process is launched
under. Both host drivers consume it — the headless driver's `--name`, and the
tmux driver's `--name` plus the label its tmux session name derives from.

Resolution, when the caller omits it:

- a **project**-class role → `role_name`
- anything else → `lane_id`

The project class is the one whose entire point is that the worker *is* the
role. Every other class is lane work, and `lane_id` is what those sessions were
already labelled with, so the default is a no-op for them.

## What the local name buys: the mutation guard, immediately

The Git-Controller gate is a `PreToolUse` hook that resolves its caller by
reading `~/.claude/sessions/<parent_pid>.json` and taking the `name` field,
then comparing it to the configured controller name **exactly**
(`session_name == controller`).

Two consequences follow directly from that exactness:

1. A worker spawned with `local_name="Git-Controller"` passes the guard on its
   FIRST turn. Before this, a spawned Git-Controller could not mutate git until
   a second driven turn had it claim the name — the gap `#13` reported.
2. **A uniquifying suffix is not available.** `Git-Controller-2` is not a
   slightly-different Git-Controller; it is a session that cannot mutate git at
   all. This is why the collision below is a refusal rather than a rename.

Historical note worth keeping: the guard reads a file that, before 2026-08-14,
only the HEADLESS driver ever populated — it alone passed `--name`. A tmux
worker's name was always auto-derived (`nameSource: "derived"`), so the tmux
host had never been guard-nameable at all. That asymmetry is the latent defect
underneath `#13`; naming the tmux *session* would not have fixed it, because
the guard never reads the tmux session name.

## What the local name does NOT buy: routing

Passing the mutation guard and being reachable by name are different
mechanisms, and conflating them is the easy mistake here.

**Spawning does not claim the durable role binding.** This is deliberate
(operator ruling, 2026-08-14). Claiming a role adds a name and never releases
the incumbent's, so an automatic claim at spawn would EVICT whoever currently
holds that role — a destructive act triggered by a routine one.

So a freshly spawned `Git-Controller`:

- passes the git mutation guard immediately (local name), **and**
- is NOT yet a `peer_send_by_name` destination for `Git-Controller` (no claim).

The worker claims its role explicitly once it is up; only then does
role-addressed routing reach it. `role_name` is recorded on the
`managed_session` row as the spawn's stated INTENT, never as a claim.

## The three gates

### 1. `spawn_session` — refuses a second LIVE local name

A spawn whose resolved `local_name` matches a NON-TERMINAL `managed_session`
row is refused with `local_name_already_held`, naming the incumbent's
`agent_instance_id`, `lifecycle_state`, `host`, `host_ref` and `lane_id`, plus
the `terminate_session` call that frees the name. Never a silent suffix (see
above), never a silent eviction.

The ordering is **terminate the holder, then spawn the replacement.**

The gate keys on the session LEDGER rather than the role binding because the
collision it prevents is two local PROCESSES answering to one name on one
machine — which is exactly what the guard's exact compare would let both of
them do.

### 2. `peer_claim_role` — refuses a LIVE different-session holder

Independent of the above, and **already shipped** — it is not new, and a second
gate must not be built on top of it. A claim against a live holder from a
different session returns `role_held_live`, naming the incumbent, unless an
explicit `takeover` is passed after operator confirmation.

### 3. A DEAD holder stays cheaply claimable — on purpose

This one reads like an oversight to anyone who did not watch it get decided, so
the reason matters more than the behaviour:

A session that crashed can no longer release its own role. A gate that also
refused dead holders would strand that role permanently behind a session that
no longer exists — a worse failure than the collision gates 1 and 2 prevent.
So crash succession stays cheap by design: a holder that reads dead by the
liveness window is claimable with no explicit terminate.

The same posture arrives at gate 1 by construction rather than by coordination:
a crashed worker's row is swept to `terminated`, and the spawn refusal only
looks at non-terminal rows, so a replacement spawns with no operator in the
loop.

**The general rule, stated once:** the platform refuses collisions between LIVE
holders and never refuses succession from a dead one.

## Spawning onto a host where hooks cannot run

`degraded_hooks_acknowledged` (default false) is the explicit opt-in to spawn
onto a host whose preflight can PROVE the worker's hooks will not run — a
managed Claude Code policy listing `hooks` in `strictPluginOnlyCustomization`
strips hooks from every non-plugin source, including the `--settings` blob both
drivers inject worker hooks through.

Without the flag, such a spawn is refused (`host_cannot_spawn`) naming the
policy file and the offending key. With it, the spawn proceeds, the choice is
recorded on the ledger row, and it is logged loudly — so "running degraded" is
a stated decision rather than something discovered later from the silence.

Note the two gates in this path fail in OPPOSITE directions, which is correct
and worth understanding rather than "fixing":

- The spawn refusal fails **closed**: a policy we can read and understand
  proves the worker will be deaf, so we refuse by name.
- The policy READER fails **open**: an unreadable or malformed policy file is
  not evidence that hooks are stripped, so refusing on it would be a refusal we
  cannot justify.

The registration watchdog is the backstop under both: any row still `spawning`
past its registration bound is marked `registration_overdue_at` with an
attribution, whatever the cause — a policy shape nobody has reported yet, a
crashed hook, a read-only spool. The preflight is a narrow prover; the watchdog
is mechanism-independent.
