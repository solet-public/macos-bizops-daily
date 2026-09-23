# Fleet Liveness Check

Article Layer: 2

Article Role: joseki_catalog

Article Tags: planning-stage:post-approval, planning-stage:wbs-execution, evidence-category:joseki, domain:agent-lifecycle, domain:fleet-session-management


JOSEKI_KEY: fleet_liveness_check
DESCRIPTION: Phase A of the fleet steward procedure: a fast, mechanical, objective-agnostic pass over the WHOLE fleet (every registered session, not scoped to any one coordinator's own dispatches) that determines whether anything is stuck — an overdue lane, a stalled Git-Controller authorization queue, a stranded drive, an unexpected host sleep, or an undrained role inbox — and either logs a clean pass or surfaces a concrete, specific finding to the operator. Runs frequently (target: every ~20 minutes), standalone; fleet-progress-review (Phase B) reads this card's latest persisted run rather than re-running it. Runnable by any registered agent regardless of runtime (Claude Code, Codex, or the joseki engine's own deterministic-continuation steps with no LLM turn at all) — every bound step below names a real solet process key, none of them a runtime-specific tool. Use on a scheduled fleet-liveness wake, or whenever asked for a fleet health check-in.
EMBEDDING_DESCRIPTION: Run a fast, mechanical, whole-fleet liveness sweep. Snapshot who is registered, pull the register's open facts, verify every actively-dispatched lane's real host state directly with session_status rather than trusting a cached coverage label, drain the caller's own role inbox against a durable cursor, check for an unexpected host sleep, triage anything unhealthy into a drive, a restart, or a register-hygiene fix, and persist the pass so later review and the next pass have continuity. Does not evaluate whether the fleet is making progress toward any goal, only whether anything is stuck right now.

## Input Contract

- Read access to this solet's peer registry (`peer_list`) and managed_session ledger (`session_status`, `list_sessions`) for the whole fleet — this card's scope is every registered session, not one coordinator's own dispatches (contrast `coordinator_fleet_check_in`, which is scoped to sessions the calling coordinator itself dispatched).
- Read access to the project-solet register: in this checkout the register is a project-solet-backed database reached via the `psolet` CLI. It has no `service_interface` process binding inside this solet, so this card's register-reads (`psolet resume`, `psolet db unit who-has-work`, `psolet db issue show`, `psolet db issue record-event`) are plain CLI instructions, not bound process calls.
- Read/write access to two durable local cursor files this checkout keeps for exactly this card: `workbench/.peer_inbox_last_check` (ISO-8601 UTC) and `workbench/.sleep_monitor_last_check` (local time, `%Y-%m-%d %H:%M:%S` — the two formats are not interchangeable and must not be conflated).
- Read access to `/usr/bin/log show` and `pmset -g` on the host machine, for the unexpected-sleep check. This machine is meant to never sleep; a match is a live incident, not routine telemetry.
- The caller's own repo-boundary and register-write authority for any register-hygiene fix in steps 6-10 applies directly (closing an issue, retiring a now-idle session, restarting a dead one). This card grants no new authority.
- Git mutation stays exclusively with this checkout's Git-Controller session; this card never stages, commits, or otherwise mutates git, even when a triage step finds unpreserved worktree content.

## Output Contract

- Every actively-dispatched lane in scope classified by its real `lifecycle_state` and `host_liveness` from `session_status`, never from `peer_list` registration or the register's `coverage` label alone — both are documented to lag or mislead in opposite directions.
- Every unhealthy finding triaged into exactly one of: a register-hygiene fix (independently verified landing + stale issue disposition), a specific `drive_session` status request, a `terminate_session` + `spawn_session` restart with role-continuity reverified, or an explicit escalation to the operator — never a vague "check in" and never a silent skip.
- The caller's own role inbox drained against its durable cursor, with any row genuinely new (nothing in this session's history shows it was already seen) treated as a live message arriving now, not historical replay.
- An unexpected host sleep, if any, recorded as a full evidence event on the standing incident ticket and escalated unconditionally, regardless of anything else this pass found.
- Exactly one persisted `record_fleet_liveness_run` call for this pass, even when everything is clean — its structured fields (`observed_at`, `checked`, `stuck`, `actions`, `outcome`, `role_inbox_drain`, `sleep_check`, `escalations`) always present with empty collections standing in for "nothing found," never an omitted field.
- A quiet log entry on a clean pass, or a concrete surfaced finding — never both silence on a real finding, nor noise re-surfacing an already-known stale row.

## Sequence

[ ] 1. Snapshot who is registered
    a) Read the whole fleet's live peer registry. Do not treat `registered_at`/`updated_at` as a session-start timestamp — it is a ~200s reconnect heartbeat that resets on its own and says nothing about how long a session has actually been working.
        RESULT_PROCESSOR_KIND: deterministic_continuation
        Call: plugin::agent_messaging_plugin::peer_list
        Arguments:
        {}

[ ] 2. Pull the register's open facts
    a) `psolet resume --solet` followed by this deployment's own solet name (never hardcoded — this card ships across solet identities) for `open_decisions`/`changed_issues`/`active_rulings`, and `psolet db unit who-has-work` for current units and their declared `coverage`. Plain CLI — no service binding in this checkout.
    b) Check Git-Controller's held-authorization queue for anything aged. This queue has no silent TTL by design; the caller judges staleness directly from `created_at`.
        RESULT_PROCESSOR_KIND: deterministic_continuation
        Call: plugin::agent_messaging_plugin::list_held_authorizations
        Arguments:
        {"owed_by_role": "Git-Controller"}

[ ] 3. Verify every actively-dispatched lane's real state
    a) For each distinct worker `agent_instance_id` currently dispatched (cross-reference step 1's peer snapshot against step 2's who-has-work — the register's `actor` on a unit is often the coordinating owner, not the actual doer; resolve to the real worker), call session_status. Trust only this result, never the register's `coverage` label, which can read `active-owned`/`dispatched` for hours after the real worker has gone `overdue`.
        RESULT_PROCESSOR_KIND: deterministic_continuation
        Call: plugin::agent_messaging_plugin::session_status
        Arguments:
        {"agent_instance_id": "<<BIND:agent_instance_id>>"}

[ ] 4. Self-drain the caller's own role inbox
    a) Read the cursor from `workbench/.peer_inbox_last_check` (ISO-8601 UTC; this is a `peer_inbox` `after` argument, not a `log show` cursor — do not conflate it with step 5's cursor file or format). Missing file: use 20 minutes before now.
    b) Call peer_inbox. Its `after` argument only filters the instance section; the role section (what matters here) has no time-filter argument at all and must be filtered client-side against the cursor. Role entries return newest-first; page further with `role_after` only if the oldest row on the current page is still newer than the cursor.
        RESULT_PROCESSOR_KIND: deterministic_continuation
        Call: plugin::agent_messaging_plugin::peer_inbox
        Arguments:
        {"after": "<<BIND:peer_inbox_cursor>>"}
    c) For each role row newer than the cursor, check whether it was already acted on this session. A row genuinely new — nothing in this session's own history shows it was seen — is exactly the failure mode this step exists to catch: treat it as a live message arriving now.
    d) Advance the cursor to now (`date -u +"%Y-%m-%dT%H:%M:%SZ"`) regardless of outcome, and write it back to `workbench/.peer_inbox_last_check`.

[ ] 5. Check for an unexpected host sleep since the last pass
    a) Read the last-checked cursor from `workbench/.sleep_monitor_last_check` — local time, `%Y-%m-%d %H:%M:%S`, the literal format `/usr/bin/log show --start` requires (an ISO-8601 cursor fails closed with a conversion error, not a silently wrong query). Missing file: use 20 minutes before now — do not scan further back, that is fleet-progress-review's job.
    b) Run `/usr/bin/log show --start` with that cursor value quoted as its argument, plus `--predicate 'eventMessage contains "Entering Sleep state due to" AND process != "log"'` — the full path, not the zsh `log` builtin (which fails with a cryptic "too many arguments"); omit `--end` entirely to mean "now" (the literal string "now" fails closed); exclude `process == "log"` because the tool's own invocation is logged and otherwise self-matches. Plain CLI — no service binding.
    c) Any match: append a full record to this deployment's own local sleep-event log if it keeps one (a per-checkout convention, not a path guaranteed to exist in every shipped profile — never assume a specific file is present), record an `evidence` event on the standing host-sleep incident ticket via `psolet db issue record-event` citing the new occurrence, and treat this as an unconditional step 12 escalation regardless of anything else this pass found — a recurrence of an already-flagged, still-unexplained anomaly is never "already known, don't re-report."
    d) No match: write the new cursor (local time) to `workbench/.sleep_monitor_last_check` and say nothing further beyond "sleep check: clear" in this pass's log entry.

[ ] 6. Close a verified landing and retire the now-idle lane
    a) When step 3 shows `lifecycle_state: overdue` + `host_liveness: alive` and the lane's own unit/issue history shows a self-reported landing: before driving for a redundant status check, independently verify the landing commit is a real ancestor of the current branch tip, then check the linked issue's own `disposition` directly (`psolet db issue show`). If the commit is confirmed and the issue is still `open`/`new`/`unclassified`, close it out (citing what was verified), then retire the now-idle lane — a register-hygiene fix, not a drive. Only do this for a lane the calling coordinator itself dispatched; for another coordinator's lane, flag the finding to that coordinator instead. Skip this step when no lane in scope matches this pattern this pass.
        RESULT_PROCESSOR_KIND: deterministic_continuation
        Call: plugin::agent_messaging_plugin::retire_session
        Arguments:
        {"agent_instance_id": "<<BIND:agent_instance_id>>"}

[ ] 7. Drive a stale-but-alive lane for a concrete status
    a) When step 3 shows `lifecycle_state: overdue` + `host_liveness: alive` with no landing evidence to check under step 6: drive with a concrete, specific status request naming the exact staleness and asking for the exact blocker, never a vague "check in." Skip this step when no lane in scope matches this pattern this pass.
        RESULT_PROCESSOR_KIND: deterministic_continuation
        Call: plugin::agent_messaging_plugin::drive_session
        Arguments:
        {"agent_instance_id": "<<BIND:agent_instance_id>>", "text": "<<BIND:drive_text>>"}
    b) If the result comes back `drive_unverified`/stranded (text sitting in the composer, never confirmed sent): stop, do not retry. A second attempt deposits an equally-unconfirmable duplicate; this lane needs the operator's direct attention.

[ ] 8. Restart a lane whose host has died
    a) When step 3 shows `lifecycle_state: overdue` + `host_liveness` not alive (dead): capture the dying row's own `lane_id`/`brief_ref`/`work_class`/`budget_line`/`model`/`effort`/`host` from its `session_status` result before it goes fully terminal, then terminate it (a restart, not a drive). Skip this step when no lane in scope matches this pattern this pass.
        RESULT_PROCESSOR_KIND: deterministic_continuation
        Call: plugin::agent_messaging_plugin::terminate_session
        Arguments:
        {"agent_instance_id": "<<BIND:agent_instance_id>>"}

[ ] 9. Respawn the restarted lane onto its captured configuration
    a) Using exactly the `lane_id`/`brief_ref`/`work_class`/`budget_line`/`model`/`effort`/`host` captured in step 8, spawn the replacement. Only reached when step 8 actually ran this pass.
        RESULT_PROCESSOR_KIND: deterministic_continuation
        Call: plugin::agent_messaging_plugin::spawn_session
        Arguments:
        {"role_class": "<<BIND:role_class>>", "lane_id": "<<BIND:lane_id>>", "brief_ref": "<<BIND:brief_ref>>", "work_class": "<<BIND:work_class>>", "budget_line": "<<BIND:budget_line>>", "dispatch_kind": "<<BIND:dispatch_kind>>"}

[ ] 10. Reverify role continuity after a restart
    a) If the session terminated in step 8 was holding a durable role, confirm the role is actually bound to a live holder after step 9's respawn. Only reached when step 8 ran this pass and the dying row held a role.
        RESULT_PROCESSOR_KIND: deterministic_continuation
        Call: plugin::agent_messaging_plugin::peer_holds_role
        Arguments:
        {"name": "<<BIND:role_name>>", "agent_instance_id": "<<BIND:agent_instance_id>>"}

[ ] 11. Persist this run
    a) Call record_fleet_liveness_run once, even on a fully clean pass. Empty lists/objects stand in for "nothing found," never an omission.
        RESULT_PROCESSOR_KIND: deterministic_continuation
        Call: plugin::agent_messaging_plugin::record_fleet_liveness_run
        Arguments:
        {"observed_at": "<<BIND:observed_at>>", "checked": <<BIND:checked>>, "stuck": <<BIND:stuck>>, "actions": <<BIND:actions>>, "outcome": "<<BIND:outcome>>", "role_inbox_drain": <<BIND:role_inbox_drain>>, "sleep_check": <<BIND:sleep_check>>, "escalations": <<BIND:escalations>>}
    b) The last N passes are read with `recent_fleet_liveness_runs`, used by fleet-progress-review rather than re-run here.

[ ] 12. Report or stay quiet
    a) Surface to the operator only if: an open register decision, a lane found genuinely overdue this pass (cross-check against the last log entry — not an already-known stale row), a stranded/undeliverable drive from step 7, an aged held authorization from step 2, a new host sleep event from step 5 (always surface, never suppressed as "already known"), or step 4's drain surfacing a genuinely new row nothing in this session ever acted on (a live delivery-loss incident, not routine telemetry). Otherwise: log it, say nothing.

## Expected Step Count

12 steps.

## Binding Guidance

- Bind step 1's snapshot to the whole live fleet every pass; this card's scope is everyone registered, not a per-coordinator subset (that narrower duty is `coordinator_fleet_check_in`).
- Bind step 3's `agent_instance_id` fresh for every actively-dispatched lane in scope, once per session, every pass. A cached `session_status` result from an earlier pass is not a re-verification, and the register's `actor` column names the coordinating owner, not necessarily the worker to check.
- Bind step 4's cursor strictly to `workbench/.peer_inbox_last_check` in ISO-8601 UTC, and step 5's cursor strictly to `workbench/.sleep_monitor_last_check` in local `%Y-%m-%d %H:%M:%S` — these two cursor files and formats are never interchangeable.
- Bind steps 6-10's branch to what step 3 actually returned this pass, not a remembered prior state — a lane can change state between passes and the triage must track the current one. Each of steps 6, 7, and 8 applies only when its own named pattern matches at least one lane this pass; none of them fire on a pass where nothing matches.
- Bind step 6's register-hygiene fix only to a lane the calling coordinator itself dispatched; a finding on another coordinator's lane is a flag to that coordinator, never a unilateral fix.
- Bind step 9's spawn arguments exactly to what step 8 captured from the dying row's own `session_status` result, never to a remembered or assumed default configuration.
- Bind step 11's `checked`/`stuck`/`actions`/`role_inbox_drain`/`sleep_check`/`escalations` to concrete structured values from a live process-schema lookup before the first run of a pass — this card's own text is not a substitute for the live schema, since required fields have drifted before (`role_inbox_drain` and `sleep_check` objects and an `escalations` array, confirmed live 2026-09-16, are not implied by an older description of this verb).

## Coherence Obligations

- This card never mutates git. A worktree finding is flagged or handed to Git-Controller; it is never committed, stashed, or pushed by this card itself.
- A session's own self-reported closure is evidence, never the closing fact. Step 6 requires independent verification of both commit ancestry and issue disposition before anything closes — a lane's "closure complete" report has been measured, repeatedly, to leave the actual issue row untouched.
- Determinative and inferential triage are never the same branch. A fact that follows mechanically from a verified state (dead host, verified-landed commit, an unexplained sleep recurrence) is acted on directly; a judgment call about salvage, reassignment, or priority is escalated, never resolved silently on this card's own initiative.
- Silence is only correct when nothing changed since the last logged pass. Re-surfacing an already-known stale row every pass is noise, not stewardship; suppressing a genuinely new finding as "probably already known" is the opposite failure and is never acceptable for a host-sleep recurrence or a genuinely new inbox row.
- This is the whole-fleet occasional Phase-A sweep. `coordinator_fleet_check_in`'s narrower per-coordinator standing duty does not replace it, and this card does not replace that one either.

## Next Joseki

`coordinator_fleet_check_in` for the narrower per-coordinator standing duty over sessions the caller itself dispatched, reusing this card's `session_status` triangulation rather than reinventing it. `fleet-progress-review` (Phase B) for whether the fleet is actually making progress, reading this card's latest persisted run rather than re-running these steps.

## Repair Joseki

Explicitly absent as a card. A `drive_unverified`/stranded result is a documented failure mode requiring operator attention, not a repair to automate around. A misclassified triage branch is corrected by re-running this card's step 3 and the relevant step 6-10 branch against the session's current real state, not by an ad hoc patch.
