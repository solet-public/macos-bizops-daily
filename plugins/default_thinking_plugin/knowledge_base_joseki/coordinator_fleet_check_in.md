# Coordinator Fleet Check-In

Article Layer: 2

Article Role: joseki_catalog

Article Tags: planning-stage:post-approval, planning-stage:wbs-execution, evidence-category:joseki, domain:agent-lifecycle, domain:fleet-session-management


JOSEKI_KEY: coordinator_fleet_check_in
DESCRIPTION: A coordinator's own standing, scheduled check-in over every session it personally dispatched: determine each one's real lifecycle and host state with session_status (never peer_list registration or register coverage labels alone, both of which lag and mislead in opposite directions), reconcile every register record that reports on or depends on that state, and on winding a session down, preserve any uncommitted worktree content before retiring it and close out every ticket it touched. Use on a recurring per-coordinator cadence, scoped to sessions that coordinator itself dispatched. Reuses fleet-liveness-check's proven session_status triangulation rather than reinventing it; that card remains the whole-fleet occasional Phase-A sweep, this one is the narrower standing duty of ownership.
EMBEDDING_DESCRIPTION: Run a coordinator's scheduled check-in over the sessions it personally dispatched, to keep the database honest about what is actually running. Determine each session's true lifecycle state and host liveness from session_status directly, never from peer_list registration or a register coverage label, both of which can show a dead session as live or a live session as stalled. Update every unit and issue record that depends on a session's status: apply determinative facts such as lifecycle state and commit ancestry directly, and flag inferential judgments such as whether a stalled session's partial work is worth salvaging for the coordinator's own decision instead of auto-applying them. Before retiring any session, check its worktree for uncommitted or unpushed work and preserve it, then close out or correct every ticket that session touched so nothing keeps pointing at a session that no longer exists.

## Input Contract

- The coordinator's own durable record of every session it dispatched: `dispatch_id`s from managed-dispatch composites, or `agent_instance_id`s from direct `spawn_session` calls it made itself. This is the scope. Never rediscovered from a role-name guess or a register `actor` column, which names the minting/coordinating seat, not the session that actually did the work.
- Read access to this solet's managed_session ledger (`session_status`, `list_sessions`) for every session in scope.
- Read/write access to whichever register(s) this coordinator's sessions can affect. In this checkout the register is a project-solet-backed database reached via the `psolet` CLI — it has no `service_interface` process binding inside this solet, so this card's register-reconciliation steps are plain CLI instructions, not bound process calls.
- The coordinator's own existing repo-boundary and register-write authority. This card grants no new authority; it exercises exactly what the coordinator already holds.

## Output Contract

- Every dispatched session in scope classified by its real `lifecycle_state` and `host_liveness`, not an inferred, cached, or self-reported label.
- Every register unit/issue tied to a classified session reflects that session's true state: a session confirmed dead whose fix is a verified ancestor of the branch tip has its linked issue closed and its own now-idle successor rows retired; a session confirmed dead with no verified landing stays open and flagged, never silently closed on a self-report alone.
- No worktree content lost: every session wound down in this pass had its worktree checked for uncommitted or unpushed changes before `retire_session` ran, and anything found was preserved rather than discarded by the retirement.
- A persisted record of this pass, so the next pass and any fleet-progress review have continuity instead of starting cold.

## Sequence

[ ] 1. Enumerate this coordinator's own dispatched sessions
    a) Read the coordinator's own dispatch log — the `dispatch_id`s and `agent_instance_id`s it holds from its own spawn/dispatch calls, current session and any durable record it kept across sessions. This defines scope.
    b) Cross-check scope against current live registration (service key below) to see which sessions still appear registered at all — this is scoping only, never the state read itself, since a session can be off the live roster while still holding unretired register rows, or on the roster while already dead.
        RESULT_PROCESSOR_KIND: deterministic_continuation
        Call: plugin::agent_messaging_plugin::list_sessions
        Arguments:
        {"live_only": true}

[ ] 2. Determine each in-scope session's real state
    a) For every session in scope from step 1, call session_status individually. Never substitute list_sessions's own classification, or the register's `coverage`/state label, for this call when they disagree — session_status is the documented ground truth; the others are known to lag in both directions.
        RESULT_PROCESSOR_KIND: deterministic_continuation
        Call: plugin::agent_messaging_plugin::session_status
        Arguments:
        {"agent_instance_id": "<<BIND:agent_instance_id>>"}

[ ] 3. Reconcile register records against each session's real state
    a) For any session whose session_status result disagrees with what the register currently shows for its linked unit(s)/issue(s) — a dead/terminated/retired session whose unit still reads dispatched/working, or whose linked issue still reads open/new despite a self-reported landing — reconcile the register.
    b) Before closing anything on a landing claim, independently verify the landing commit is a real ancestor of the current branch tip yourself; do not trust the self-report alone. Then check the linked issue's own disposition directly — commit ancestry and issue disposition are two different facts and both must be checked.
    c) Apply determinative reconciliation directly: correct unit state events and issue dispositions that plainly follow from a verified fact (dead session, verified-landed commit, register still stale). Do not apply inferential judgments the same way — whether a stalled session's partial work is salvageable, whether a blocked ticket should be reassigned versus closed, and similar judgment calls are recorded in this pass's output for the coordinator's own explicit decision, never auto-resolved.

[ ] 4. Preserve worktree content before winding down any session
    a) For every session this pass is about to retire, first inspect its lane worktree for uncommitted or unpushed changes with read-only git commands (status/diff against its upstream) — never a git mutation performed by this card itself; git mutation stays exclusively with the checkout's Git-Controller session per standing repo policy.
    b) Anything found uncommitted or unpushed is never discarded by proceeding to retirement. Either request a scoped Git-Controller landing for it first, or explicitly hand the coordinator the exact worktree path and diff so a human or coordinator decision preserves it before retirement proceeds. A session that still holds unpreserved work is not eligible for step 5 yet.

[ ] 5. Retire sessions confirmed dead with nothing left to preserve
    a) Only sessions that are either already dead/terminated with step 4 cleared, or explicitly wound down by the coordinator with step 4 cleared, reach this step.
        RESULT_PROCESSOR_KIND: deterministic_continuation
        Call: plugin::agent_messaging_plugin::retire_session
        Arguments:
        {"agent_instance_id": "<<BIND:agent_instance_id>>"}

[ ] 6. Record this pass
    a) Persist the pass so the next check-in and any fleet-progress review has continuity: what was checked, what was found stuck or stale, what was reconciled, what was flagged for the coordinator's own decision, and what was retired.
        RESULT_PROCESSOR_KIND: deterministic_continuation
        Call: plugin::agent_messaging_plugin::record_fleet_liveness_run
        Arguments:
        {"observed_at": "<<BIND:observed_at>>", "checked": <<BIND:checked>>, "stuck": <<BIND:stuck>>, "actions": <<BIND:actions>>, "outcome": "<<BIND:outcome>>", "role_inbox_drain": <<BIND:role_inbox_drain>>, "sleep_check": <<BIND:sleep_check>>, "escalations": <<BIND:escalations>>}

## Expected Step Count

6 steps.

## Binding Guidance

- Bind step 1's scope to the coordinator's OWN dispatch records, never to a `peer_list`/`list_sessions` filter alone. `list_sessions(live_only=true)` in step 1b returns the whole live fleet; intersect it client-side with the coordinator's own dispatch log, do not treat it as the scope by itself.
- Bind step 2's `agent_instance_id` fresh for every session in scope, once per session, every pass. A cached session_status result from an earlier pass is not a re-verification.
- Bind step 3's reconciliation to what step 2 actually returned this pass, not to a remembered prior state — a session can change state between passes and the register must track the current one, not the last one this coordinator happened to notice.
- Bind step 4's worktree check to the lane's actual `lane_root_path`/worktree, not the shared checkout root — a lane's own worktree is what can hold its unpreserved work, and checking the wrong path silently clears a session that still has real diffs sitting elsewhere.
- Bind step 5 only after step 4 is explicitly cleared for that exact session. Retiring ahead of the worktree check is the failure this card exists to prevent, not an acceptable ordering shortcut under time pressure.
- Bind step 6's `checked`/`stuck`/`actions`/`role_inbox_drain`/`sleep_check`/`escalations` to concrete structured values, never prose summaries standing in for them — an empty pass still submits empty lists/objects, not an omission. `checked` and `role_inbox_drain`/`sleep_check` are free-form objects, not arrays; `stuck`/`actions`/`escalations` are arrays of objects, not strings. This card's own text is not a substitute for reading the live schema — call the platform's process-schema lookup for this verb before the first run of a pass, since this step's exact required fields (confirmed live 2026-09-16: `role_inbox_drain` and `sleep_check` objects and an `escalations` array are required alongside `observed_at`/`checked`/`stuck`/`actions`/`outcome`) can drift from what any card documents.

## Coherence Obligations

- This card never mutates git. Step 4 finds and flags unpreserved work; it never commits, stashes, or pushes it. That action routes to the checkout's Git-Controller session, same as any other landing.
- This card never grants new register-write authority. A coordinator reconciling its own sessions' records still operates within whatever write authority it already holds for that repo; a finding that needs authority the coordinator lacks is escalated, not forced through.
- A session's own self-reported closure is evidence, never the closing fact. Step 3 requires the coordinator's own independent verification of both commit ancestry and issue disposition before anything closes — the same two-fact check fleet-liveness-check's own documented trap requires, because a lane's "closure complete" report has been measured, repeatedly, to leave the actual issue row untouched.
- Determinative and inferential reconciliation are never the same step. A fact that follows mechanically from a verified state (dead session, verified-landed commit) is applied directly; a judgment call about salvage, reassignment, or priority is recorded for the coordinator to decide, never resolved silently on this card's own initiative.
- This is a per-coordinator standing duty over sessions that coordinator itself dispatched, not a replacement for the whole-fleet occasional sweep. Do not use this card to justify skipping or narrowing fleet-liveness-check's own broader pass.

## Next Joseki

`request_scoped_landing` when step 4 finds unpreserved work that should reach master rather than sit hand-off-pending. `fleet-liveness-check` (this checkout's whole-fleet Phase-A skill) remains the broader periodic sweep this card's per-coordinator scope does not replace.

## Repair Joseki

Explicitly absent as a card. If step 5's `retire_session` is interrupted mid-composite, it is documented as re-drivable by construction — re-run it on the same `agent_instance_id` rather than authoring a repair around it. If step 3's reconciliation itself was wrong (a determinative fact was misread), the register-issue-lifecycle procedure for this checkout is the correction path, not an ad hoc edit.
