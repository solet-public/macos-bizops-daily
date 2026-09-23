# Dispatch Managed Worker

Article Layer: 2

Article Role: joseki_catalog

Article Tags: planning-stage:post-approval, planning-stage:wbs-execution, evidence-category:joseki, domain:agent-lifecycle, domain:agent-messaging

JOSEKI_KEY: dispatch_managed_worker
DESCRIPTION: Dispatch a bounded worker lane through the durable managed-dispatch composite, prove uptake only with a structured worker ACK, supervise liveness and deadlines from platform state, route blockers to the correct authority, and accept completion only after independent evidence validation.
EMBEDDING_DESCRIPTION: Start and supervise a managed worker through one durable dispatch contract. The platform writes the contract before spawning, records first-turn transport evidence without treating it as uptake, requires structured ACK and progress events, reconciles native host liveness, surfaces blockers and deadlines, and lets only a coordinator acceptance event complete the lane.

## Input Contract

- An immutable brief that exists before dispatch, plus its measured SHA-256.
- A bounded lane, role, role class, work class, budget line, model, effort,
  runtime, host/allowed-host policy, visibility, local name, report/TTL windows,
  and tool/permission/transport policy.
- An exact expected artifact path and structured completion contract.
- Absolute timezone-aware `uptake_due_at`, `report_by`, `watchdog_due_at`, and
  `expires_at` deadlines.
- The spawning coordinator's durable role. The server derives its instance and
  session from authenticated call context; callers never authorize themselves.

## Output Contract

- One server-issued `dispatch_id` whose durable row exists before any host side
  effect, plus one linked current attempt.
- Durable first-turn source, delivered/error outcome, and time. This is
  transport evidence only; it never means the worker understood the brief.
- A structured worker ACK before `active`, then causal milestone, blocker, or
  completion events.
- Tri-state native host liveness (`alive`, `dead`, or `unknown`), bounded
  unknown re-probe/escalation, and explicit supervision conditions for failed
  starts, uptake, report-by, watchdog, blockers, acceptance, and TTL.
- `completed` only after the coordinator re-reads the exact artifact, verifies
  its SHA-256 and contract evidence, and records `accept_completion`.

## Sequence

[ ] 1. Write and hash the exact brief before dispatch
    a) Author the bounded brief, including writable paths, authority limits, required KB/ledger searches, gate procedure, reporting cadence, and completion signal.
    b) Measure the brief SHA-256; do not accept a caller-supplied digest without comparing it to the file.

[ ] 2. Atomically dispatch the managed worker
    RESULT_PROCESSOR_KIND: deterministic_continuation
    a) Call `plugin::agent_messaging_plugin::dispatch_managed_work` with every Input Contract field.
    b) Record the returned `dispatch_id`, attempt identity, state, and first-turn evidence.
    c) Treat `uptake_pending` as "submission confirmed; ACK absent" and `uptake_uncertain` as "submission failed or is unknown". Neither is active work. A host failure returns the durable failed-start dispatch identity/state/version and remains supervised.

[ ] 3. Require a structured model-turn ACK
    RESULT_PROCESSOR_KIND: deterministic_continuation
    a) The worker calls `plugin::agent_messaging_plugin::report_managed_dispatch` with `event_kind="ack"`, a unique `event_id`, current `prior_version`, current attempt identity, exact brief digest, role binding, scope-readback digest, and plan digest. The server derives and verifies the worker instance/session and held role.
    b) Only the accepted ACK transition makes the dispatch `active`. Peer registration, presence, queued delivery, and first-turn-delivered are subordinate receipts.

[ ] 4. Report milestones and blockers causally
    RESULT_PROCESSOR_KIND: deterministic_continuation
    a) The worker reports `milestone`, `blocked`, or `completion` through `report_managed_dispatch`, always with the current causal version and attempt identity.
    b) `blocked_internal` names the coordinator-owned question, evidence, safe options, exact owner, and a timezone-aware future decision deadline before TTL. `blocked_operator` uses owner `operator` and names the authority gap and exact question.
    c) The coordinator answers, retries, cancels, or accepts/rejects through `plugin::agent_messaging_plugin::resolve_managed_dispatch`; a stale causal version is rejected and retained for audit.

[ ] 5. Supervise from durable platform state
    RESULT_PROCESSOR_KIND: deterministic_continuation
    a) Read `plugin::agent_messaging_plugin::managed_dispatch_status` when making a decision.
    b) The platform lifecycle sweep evaluates every nonterminal attempt and dispatch without a coordinator-owned cron: it reconciles native liveness, detects failed-start, uptake, milestone, watchdog, blocker, acceptance, and TTL conditions, and emits one notice per condition and causal version.
    c) `dead` converges the attempt out of a false-live lifecycle state and the dispatch to `worker_lost`. Probe faults remain `unknown` with bounded next-probe/escalation obligations; no sweep silently respawns an ambiguous worker.

[ ] 6. Independently accept exact completion evidence
    RESULT_PROCESSOR_KIND: deterministic_continuation
    a) A worker completion event must name the exact expected path, current artifact SHA-256, completion-contract digest, allowed verdict, every required evidence obligation, and an explicit reason for each skip/not-applicable item.
    b) The coordinator re-reads and re-hashes the artifact, independently checks every declared obligation/verdict, binds acceptance to the exact worker-evidence digest, then calls `resolve_managed_dispatch(action="accept_completion")` with a fresh event ID and current version.
    c) Request scoped Git-Controller landing separately. Dispatch completion never mutates Git and never proves landing, deployment, or publication.

## Binding Guidance

- Use a fresh unique `event_id` for each semantic event; a retry of the same
  event reuses its ID and is idempotent.
- Read the current version immediately before reporting or resolving. A stale
  event must lose loudly; do not overwrite the winning transition.
- Match model and effort to the lane and bound it with explicit deadlines.
  These are dispatch contract fields, not inferred coordinator preferences.
- `expected_path` is an identity and validation target, not a completion
  predicate. Path existence, dirty-tree presence, a draft, a worker claim, or
  a routing receipt cannot complete the dispatch.
- A human plan or task list is a projection for operator readability. The
  dispatch row and append-only events are the control-plane truth; stale plan
  text must not hide or strand a durable dispatch.
- Raw `spawn_session` rejects project-class managed work without a valid
  server-issued preparing `dispatch_id`, and the public raw process refuses
  project work even when one is supplied. The internal retry path compares
  every spawn/lineage field to the immutable row. Do not build a compatibility
  path around that refusal.

## Coherence Obligations

- The brief and contract exist before the worker and remain hash-bound.
- Runtime-specific behavior stays in host/driver adapters. The state machine,
  ACK, blocker, completion, and acceptance semantics are runtime-neutral.
- `registered/live` is session lifecycle evidence, never a dispatch state.
- `unknown` liveness is not folded into alive or dead.
- Worker death, unsupported submission, TTL expiry, and completion rejection
  require an explicit coordinator decision. Replacement attempts are created
  only by `resolve_managed_dispatch(action="request_retry")` under the same
  dispatch, with fresh derived deadlines and an actual linked attempt; no
  watchdog silently creates a second worker.
- This joseki never grants new authority. Capability escalation, destructive
  Git, deploy, and other operator-only actions route to the seat that owns
  them.

## Next Joseki

`request_scoped_landing` is the usual successor after accepted completion.
The worker report and coordinator acceptance are evidence inputs to that
separate Git-Controller exchange, not an implicit landing request.

## Repair Semantics

Use the state and `next_required_action` returned by
`managed_dispatch_status`. `uptake_uncertain`, `failed_start`, `worker_lost`,
and `expired` permit an explicit bounded retry. Internal and operator blockers
stay visibly typed until resolved. Completion evidence can be rejected back to
`active` for repair. Never infer a retry, ACK, resolution, or completion from
silence.
