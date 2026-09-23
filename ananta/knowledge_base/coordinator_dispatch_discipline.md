Tags: coordinator, managed-dispatch, watchdog, multi-agent, supervision, blockers, completion, evidence
Article Layer: 2

# Coordinator Dispatch Discipline — Durable Managed Work

## Local operating model

This installation is a single-user system. Its local agent sessions are
cooperating parts of the same system. Use the existing session identity and
live binding for attribution, routing, work ownership and lifecycle tracking.
Do not invent an adversarial relationship between these sessions or add
credentials, security reviews or permission loops to routine internal
coordination. When identity or a binding is missing, diagnose and repair the
tracking gap; do not turn it into an unrequested security project. Apply the
operator's actual task and standing rulings before adding requirements. Source:
operator ruling `rul_86c9e495-e1fe-47a7-b288-2ba20c92014e`; recurring defect
`iss_10342093-3279-4a83-b769-9fe0fbd1d196`.

Coordinator-issued work with a deliverable uses the managed-dispatch control
plane. A raw peer message, host spawn, routing receipt, coordinator plan item,
or scheduled memory is not enough: each can disappear, drift, or describe
progress that the worker never actually took up.

The durable `managed_dispatch` row is the work contract. Its linked
`managed_session` rows are host attempts. Append-only dispatch events record
transport evidence, worker ACK/milestone/blocker/completion reports,
coordinator decisions, rejected race losers, and deduplicated supervision
notices. Human plans and task lists are projections for readability; they are
never the control-plane truth.

**Standing retirement handoff convention:** every dispatch brief to a spawned
lane must instruct the worker, as its final action once it determines there is
no further work on its assigned unit, to report that fact to its spawning seat
via `peer_send_by_name` to the dispatching role (or in its final report if
synchronous) and state that it is ready to be retired. The spawning seat then
calls `retire_session`. This is a standing dispatch requirement, not a
per-dispatch judgment call to remember; terminating live sessions remains a
seat-native action.

## 1. Failure boundary

The managed contract closes five distinct gaps:

- A host accepted a first turn but the worker never understood the brief.
- A session registered or appeared live while its native process had died.
- A worker hit a blocker and waited silently for a coordinator or operator.
- A report deadline or TTL passed while the coordinator's plan stayed stale.
- A path appeared on disk or a peer claimed completion without exact evidence
  or independent acceptance.

Keep these gaps separate. A routing receipt is not uptake; registration is not
native liveness; a worker completion report is not coordinator acceptance; and
accepted dispatch completion is not Git landing, deployment, or publication.

## 2. Atomic dispatch envelope

Every bounded managed lane declares and validates all of the following before
any host side effect:

```text
lane_id, role_name, role_class, work_class, budget_line
brief_ref, brief_sha256
expected_path, completion_contract
model, effort, agent_runtime, host, allowed_hosts
visibility, local_name, report_by_seconds, ttl_seconds
allowed_tools, permission_mode, transport, allow_askuserquestion
degraded_hooks_acknowledged
spawned_by_instance_id, spawned_by_role, directed_by
uptake_due_at, report_by, watchdog_due_at, expires_at
```

The brief exists first. Measure its SHA-256 and bind the exact digest. The
completion contract names the required evidence and gates, including how a
skip must be reported. All deadlines are absolute, timezone-aware, future
timestamps, and TTL follows the other supervision deadlines.

Coordinator and worker identity comes from the server-provided call context and
current session binding, rather than names written in the brief. The local CLI
uses its existing caller attribution; registered bridge calls retain their
existing identity. Resolve the current binding and assigned role to attribute
work and avoid stale or misrouted updates. This is work tracking within our
single-user system, not a requirement for new inter-session credentials. Worker
and resolution processes continue to receive the actor from call context rather
than an additional caller-supplied actor parameter.

`expected_path` is an identity and revalidation target. It is never a
completion predicate. A pre-created path, draft, dirty-tree entry, queued
message, or path-exists check cannot advance a dispatch to `completed`.

Required pre-flight searches remain part of the brief:

- `kb_search_required`: exact plain-English queries run through
  `service_interface::knowledge_service::search` before implementation.
- `ledger_search_required`: exact prior-session queries run through
  `service_interface::session_ledger_service::search_event_content` (and
  `search_sessions` when summary granularity is enough).

The worker reports what those searches surfaced and how they affected the
work. A successful search with no relevant result is a measured knowledge gap,
not permission to invent a convention.

## 3. Dispatch only through the composite

Call:

```text
plugin::agent_messaging_plugin::dispatch_managed_work
```

The composite validates the complete envelope, writes
`managed_dispatch(state=preparing)` first, creates the linked attempt, spawns
through the runtime-specific host adapter, persists first-turn evidence, and
enrolls the row in platform-wide supervision by leaving it nonterminal.

The return states are deliberately conservative:

- `uptake_pending`: host submission was confirmed; worker ACK is absent.
- `uptake_uncertain`: submission failed, is unsupported, or is unknown.
- `failed_start`: the host spawn itself failed after the dispatch row existed.

A failed-start result carries the durable `dispatch_id`, state, version, and
next action. The supervisor emits one coordinator decision notice for it, so a
start failure is addressable rather than orphaned.

No transport result returns `active`. Raw `spawn_session` hard-refuses
project-class managed work without a valid server-issued `dispatch_id` in
`preparing`; there is no compatibility fallback.

## 4. ACK and worker reporting protocol

Workers call:

```text
plugin::agent_messaging_plugin::report_managed_dispatch
```

Every event carries a unique `event_id`, current `dispatch_id`, current attempt
identity, current `prior_version`, and structured payload. The server supplies
and verifies the author identity from trusted context. Retrying the same
semantic event reuses its event ID and is idempotent. A stale causal version
loses loudly and remains in the audit trail.

The four worker event kinds are:

- `ack`: exact brief digest, held role binding, scope-readback digest, and plan
  digest. This model-authored event is the only transition to `active`.
- `milestone`: completed work, current evidence, exact next action, and a new
  future report deadline that precedes TTL.
- `blocked`: typed authority class, exact question, non-empty evidence/safe
  options, exact owner, and (for internal blockers) timezone-aware future
  decision deadline before TTL; operator blockers also name the authority gap.
- `completion`: exact artifact path and SHA-256, completion-contract digest,
  allowed verdict, and every declared evidence obligation. A skipped or
  not-applicable obligation requires an explicit reason.

Presence, peer registration, `first_turn_delivered`, `queued_notification`,
`queued_wake`, `queued_watcher`, and `queued_for_replay` are receipts. None is
an ACK and none proves a model turn occurred.

## 5. Blocker ownership

`blocked_internal` is for decisions already within coordinator authority:
shared-checkout isolation, suite ordering, file/lane sequencing, interpretation
under an existing ruling, dead-worker retry, and acceptance against the fixed
completion contract. It requires an exact question, evidence, enumerated safe
options, coordinator owner, and `decision_due_at`.

`blocked_operator` is for authority or product choices the coordinator cannot
make: capability escalation, destructive Git, deploy/live-state mutation,
scope expansion, unsettled policy, secrets, or external operator-presence
actions. It names the authority gap and exact question. The row remains visibly
blocked; it is never presented as active progress.

The coordinator answers through:

```text
plugin::agent_messaging_plugin::resolve_managed_dispatch
```

Legal actions include resolving a blocker, requesting one bounded replacement
attempt, accepting or rejecting completion, and cancellation within authority.
`request_retry` is the only replacement edge: it derives fresh deadlines from
immutable windows, clears attempt-local evidence, reconstructs every spawn
field from the prepared row, and starts the replacement. No sweep silently
respawns; ambiguous submission or liveness can otherwise create two workers on
one brief.

## 6. Platform-wide supervision

A nonterminal dispatch row is durable enrollment. Correctness no longer
depends on a coordinator-owned cron, scheduled memory, or up-to-date human
plan. The platform lifecycle sweep is uncapped across the fleet and:

1. probes every linked nonterminal attempt in spawning/live/idle/overdue/parked;
2. records native `host_liveness=alive|dead|unknown` and observation time;
3. moves definitively dead attempts out of false-live state and dispatches to
   `worker_lost` within one supervisor interval;
4. preserves probe faults and unsupported observations as `unknown`, persists
   a bounded next-probe/escalation obligation, and clears it on recovery;
5. detects failed-start decisions, overdue uptake, milestone/report-by,
   watchdog review, internal blocker decision, completion acceptance, and TTL;
6. emits at most one steward notice per `(dispatch_id, condition,
   causal_version)`.

TTL becomes the explicit terminal error state `expired` with a named
retry-or-cancel decision. It cannot leave a row indefinitely active and cannot
silently mint a replacement.

Read the decision-oriented aggregate with:

```text
plugin::agent_messaging_plugin::managed_dispatch_status
```

It returns dispatch/current-attempt identity, runtime/host, first-turn evidence,
ACK and milestone ages, tri-state native liveness, blocker/deadline state,
completion validation state, exact next action and owner, causal version, and
raw receipts as subordinate audit detail. `session_status` separately exposes
ledger lifecycle, tri-state liveness, `state_consistent`, and the explicit
`managed` / `legacy_unsupervised` / `legacy_unmanaged` coordination projection;
session and dispatch state must not be collapsed.

## 7. Completion truth

A worker can only reach `completion_reported`. The coordinator must
independently:

1. read the exact expected artifact;
2. recompute and compare its SHA-256;
3. compare the declared completion-contract digest;
4. validate all required evidence and every explicit skip, allowed verdict,
   and the digest of the exact worker evidence being accepted;
5. call `resolve_managed_dispatch(action="accept_completion")` with the current
   version and fresh decision event.

Only that transition produces `completed`. Reject invalid or incomplete
evidence back to `active` with a concrete repair action. Never complete from a
draft file, routing receipt, path existence, worker self-assertion, stale
attempt, stale causal version, or coordinator status query.

Once the coordinator has independently verified a worker's completion (or, for
the lighter `spawn_session` + `peer_send` lane pattern, has verified a
completion report with nothing further outstanding), it promptly retires or
terminates that worker's session in the same turn that accepts completion, not
in a later separate check. Retiring a worker's own session is tier-1 and stays
seat-native: the coordinator/seat calls `retire_session` or `terminate_session`,
never the worker on itself. This closes the measured 2026-09-15/16 gap in which
19 of 21 fleet lanes finished their work and correctly reported it, then sat
idle (some over an hour) until an external session noticed and retired them by
hand (iss_8e9e78d3).

After accepted dispatch completion, request Git-Controller landing separately
with the exact scoped paths and the registered gate evidence. Later ancestry,
deployment, publication, and cold-host proof remain distinct boundaries.

## 8. Coordinator plan is a projection

The coordinator may maintain a plan or task list for operator readability, but
updates it from `managed_dispatch_status` and durable events. The plan does not
enroll supervision, authorize a transition, prove ACK, resolve a blocker, or
complete a lane. If plan text and the dispatch row disagree, report projection
drift and follow the durable row.

A context clear, coordinator handover, bridge reconnect, or stale plan must not
strand the work. The successor reads managed status and role/direct inboxes,
then continues from the named `next_required_action` and `responsible_role`.

## 9. Communication discipline

Use role-addressed peer delivery for substantive decisions and worker notices,
but report delivery honestly. A queued or delivered transport outcome proves
only persistence/routing. The worker's structured event proves ACK, milestone,
blocker, or completion reporting; coordinator acceptance proves completion.

Avoid ACK loops. Send when the recipient must act, decide, or change course.
Do not send a pure thanks merely to acknowledge a persisted completion report.

## 10. Scope and exceptions

The managed composite is for coordinator-issued bounded work with a deliverable.
One-way FYIs, pure acknowledgements, and operator-paced interaction with no
agent-owned deliverable do not create a dispatch. Host-only/operator sessions
may use their explicitly named unmanaged lifecycle path, but project-class work
cannot bypass the composite.

This discipline tracks work; it never broadens authority. Operator-only actions
remain seat-native, Git mutation remains Git-Controller-only, and the worker
continues safe in-scope work while a separable ruling is pending.

## 11. Landing-wave cut and send loop

For eligible, reviewed, scope-disjoint units, the coordinator may request a
deterministic `landing-wave.v1` plan. It first verifies every offer, source
base, exact path set, scope conflict edge, portable path collision, review, and
unit state; unknown or stale inputs are blockers, never empty sets. The plan
contains the ordered member/offer tuples, one pinned base, one supplied
integration worktree and branch, and the canonical manifest digest.

After `prepare` reserves that exact plan, the coordinator sends its one framed
payload through this solet's `peer_send_by_name` to the repository's closed
mutating role. A charter, worker statement, register event, relayed message, or
collection of per-unit messages is not a wave authorization. Any offer/base/
membership change requires a fresh plan, manifest, and direct first-party
message.

The controller records the one sweep, each required staged/hook proof, common
merge, and atomic completion receipt. A failed member commit leaves master
unchanged and preserves the integration branch; a post-merge record failure is
reported as such, never described as a rollback. Coordinators report physical
sweeps separately from member commits and merges: throughput is verified units
per completed sweep, not the number of landing rows.

## Related mechanisms

- `dispatch_managed_worker.md` — executable joseki for the same contract.
- `knowledge_bases/agent_messaging_plugin/03_inter_agent_messaging.md` — peer
  routing, role binding, replay, and delivery-outcome vocabulary.
- `Peer Pre-Completion Gate Procedure` — exact per-path and registered gates
  required before a review/landing handoff.
- `State Interface Filter Grammar` — state access contract; no raw SQL and no
  bare null filters.
