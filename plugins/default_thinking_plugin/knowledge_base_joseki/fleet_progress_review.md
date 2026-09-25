# Fleet Progress Review

Article Layer: 2

Article Role: joseki_catalog

Article Tags: planning-stage:post-approval, planning-stage:wbs-execution, evidence-category:joseki, domain:agent-lifecycle, domain:fleet-session-management


JOSEKI_KEY: fleet_progress_review
DESCRIPTION: Phase B of the fleet steward procedure: a slower, judgment-heavy review of whether the fleet is actually making progress toward its stated objectives, not just whether anyone is stuck (that narrower question is fleet_liveness_check's Phase A). Builds on fleet_liveness_check's latest persisted run rather than re-deriving it, measures each active work stream against its own prior-cycle numbers (a delta, never a single point-in-time read), writes an honest evidence-cited assessment naming specific landings and stalls, periodically dispatches an independent critique on a strong model so self-review is never the only check, and persists every cycle's record so the next cycle has a real baseline. Runs on a slower cadence than fleet_liveness_check (target: hourly). Runnable by any registered agent regardless of runtime — every bound step below names a real solet process key, none of them a runtime-specific tool. Use on a scheduled fleet-progress wake, or whenever asked to assess real progress on the fleet's objectives.
EMBEDDING_DESCRIPTION: Run the slower, judgment-heavy fleet progress review. Read the latest liveness-check run as this cycle's Phase-A input, state which objectives and work streams are currently in force citing standing rulings by id, gather each stream's current concrete metrics, retrieve the prior cycle's numbers for the same stream and compute what actually changed, write an honest assessment naming specific landings and stalls rather than rounding up busy work into progress, persist the review, periodically dispatch an independent critique on a strong model so self-assessment is never the only check, and surface to the operator only what needs a decision.

## Input Contract

- Read access to `recent_fleet_liveness_runs` for this cycle's Phase-A input. Never invent a clear Phase-A result when none is returned; say the coverage is absent instead.
- Read access to `recent_fleet_progress_runs` (filtered by `workstream_id`) for each active work stream's own prior-cycle baseline — this is the authoritative history, not a memory tag or a markdown log parse.
- The current work-stream roster and per-stream narrative history: in this checkout that living index is the fleet-progress-review skill's own "Known work streams" section (a dev-tooling path, not part of any shipped product bundle), kept current there rather than duplicated into this card. This is a known gap against this checkout's own register-first philosophy (the roster is really an index of register project ids and would be more durable as register state itself); flag it rather than silently accepting a markdown roster as authoritative forever.
- Standing operator rulings, cited by id, for each work stream's governing authority — never re-derived from impression.
- Authority to dispatch a review-class lane on a strong, independent model for the periodic critique step. This card grants no new dispatch authority beyond what the caller already holds.

## Output Contract

- Exactly one persisted `record_fleet_progress_run` call per reviewed work stream this cycle, citing `phase_a_run_id` when step 1 returned a run, with concrete `metrics`/`delta`/`assessment`/`recommendation` — never a rounded-up or self-serving assessment where the evidence says otherwise.
- A stream with no real movement is reported as such plainly, with the evidence that shows it, never silently folded into an overall "busy" impression.
- On a cycle where an independent critique is due: a separate lane dispatched on a strong, independent model, handed the raw evidence directly (not a summary of it), required to re-verify at least a couple of underlying facts itself and state plainly whether the self-assessment reads as honest or self-serving. A critique that agrees with everything is reported as exactly that, not treated as confirmation.
- Surfaced to the operator only what needs a decision or can't wait — a delta or critique finding that should change what the fleet is doing, or anything Phase A already flagged this cycle. Otherwise: logged, and nothing said.

## Sequence

[ ] 1. Read the latest Phase-A liveness run
    a) This cycle's Phase-A input. Fold its structured findings, actions, outcome, inbox drain, sleep check, and escalations into step 5's assessment. If it returns no entries, say Phase-A coverage is absent — never invent a clear result. Cite the returned run `id` as `phase_a_run_id` in step 6.
        RESULT_PROCESSOR_KIND: deterministic_continuation
        Call: plugin::agent_messaging_plugin::recent_fleet_liveness_runs
        Arguments:
        {"limit": 1}

[ ] 2. State the objective(s) and work stream(s) currently in force
    a) Cite standing rulings by id, never re-derive from impression. Read the current work-stream roster from this checkout's living index (see Input Contract) and update that index in place when a stream opens, closes, or its relationship to another stream becomes clear — do not let that question go stale silently. Plain CLI/prose reasoning — no service binding.

[ ] 3. Gather each active work stream's current concrete metrics
    a) Pull current numbers per the per-stream sources the living index names for each work stream in force — register project state via `psolet db project show`, issue counts via `psolet db issue list`, landed-code confirmation via `git log --grep` (never assumed from a change's member count alone, since a change can carry several minted units while still being pure design/fix-in-flight). Plain CLI — no service binding.

[ ] 4. Retrieve each stream's prior-cycle baseline
    a) For each work stream gathered in step 3, retrieve its own last recorded review. A single point-in-time read from step 3 is never "progress" by itself — only the delta against this baseline is.
        RESULT_PROCESSOR_KIND: deterministic_continuation
        Call: plugin::agent_messaging_plugin::recent_fleet_progress_runs
        Arguments:
        {"workstream_id": "<<BIND:workstream_id>>", "limit": 1}

[ ] 5. Write an honest, evidence-cited assessment
    a) Name the specific landings, the specific stalls, the specific numbers, for each stream independently — progress on one stream is never evidence of progress on another, so never blend streams into one impression. No rounding up, no treating "busy" as "productive." If a stream shows no real movement, say so plainly and say why, citing evidence, never a guess.

[ ] 6. Persist this cycle's review
    a) Call record_fleet_progress_run once per reviewed work stream. Required fields are `reviewed_at`, `workstream_id`, `objective_citation`, `metrics`, `delta`, `assessment`, `recommendation`, `independent_critique`, and `escalations`; pass `phase_a_run_id` from step 1 when present. When step 7's critique for this stream is already back, its result goes in `independent_critique` on this same call; when it is not yet back, this call's `independent_critique` records that it is pending, and a later cycle's own step 6 call for the same stream carries the actual critique once it returns — the record is append-only, so a pending critique is attached by a new record, never by editing this one.
        RESULT_PROCESSOR_KIND: deterministic_continuation
        Call: plugin::agent_messaging_plugin::record_fleet_progress_run
        Arguments:
        {"reviewed_at": "<<BIND:reviewed_at>>", "workstream_id": "<<BIND:workstream_id>>", "objective_citation": "<<BIND:objective_citation>>", "metrics": <<BIND:metrics>>, "delta": <<BIND:delta>>, "assessment": "<<BIND:assessment>>", "recommendation": "<<BIND:recommendation>>", "independent_critique": <<BIND:independent_critique>>, "escalations": <<BIND:escalations>>}

[ ] 7. Dispatch an independent critique when this cycle is due for one
    a) This is coarser than every review — not every hourly pass needs one. When due: dispatch a separate lane on a strong, independent model (the standing sol/opus thorough-review pattern), handing it the raw evidence from steps 3-5 directly, not a summary. Require it to independently re-verify at least a couple of the underlying facts itself and state plainly whether the self-assessment reads as honest or self-serving — real progress? what's working? what's not? what should change? Skip this step entirely on a cycle where no critique is due.
        RESULT_PROCESSOR_KIND: deterministic_continuation
        Call: plugin::agent_messaging_plugin::spawn_session
        Arguments:
        {"role_class": "ephemeral", "lane_id": "<<BIND:lane_id>>", "brief_ref": "<<BIND:brief_ref>>", "work_class": "analysis_deliverable", "budget_line": "<<BIND:budget_line>>", "dispatch_kind": "review", "reviewed_report_vendor": "<<BIND:reviewed_report_vendor>>"}

[ ] 8. Report or stay quiet
    a) Surface to the operator only what needs a decision or can't wait — a delta or critique finding that should change what the fleet is doing, or anything Phase A already flagged this cycle. Otherwise: it's logged, say nothing.

## Expected Step Count

8 steps.

## Binding Guidance

- Bind step 1 to `limit: 1` every cycle; this card reads fleet_liveness_check's persisted output, it never re-runs that card's own steps.
- Bind step 3's per-stream metric sources to whatever the living work-stream index (Input Contract) currently names for that stream — the concrete sources differ per stream and drift as streams open, close, or change shape, so this card's own text is never a substitute for reading that index fresh each cycle.
- Bind step 4's `workstream_id` to the register project id from step 2/3, never to the prose mnemonic alone (a mnemonic like "WS1" has no register-side resolution by itself).
- Bind step 6's `independent_critique` field structure to a live process-schema lookup before the first run of a pass, the same discipline `fleet_liveness_check` applies to its own persistence call — required fields have drifted before without every card's own text catching up.
- Bind step 7's dispatch to the operator's standing dispatch-discipline rule (declared model and effort matched to the work, with independent report obligations, never inherited silently) and to whichever vendor/model pairing the current budget policy allows for `dispatch_kind: review` — do not assume a specific model string without checking current policy first.

## Coherence Obligations

- This card never re-derives Phase-A's own findings; it reads `recent_fleet_liveness_runs` and treats an empty result as absent coverage, never as an inferred clean pass.
- A metric with no stored baseline is not a measurement. Step 4's retrieval is mandatory before step 5's assessment claims any delta; a raw current number alone is a snapshot, not progress.
- Self-review is not review. Step 7's independent critique exists specifically to catch what this card's own operator/author cannot see about its own assessment — a critique that never pushes back is reported as that fact, not credited as confirmation.
- Progress on one work stream is never evidence of progress on another. Step 5 and step 6 both operate per-stream; no cross-stream blending into one overall number.
- This card never mutates git and never authorizes a landing; a finding that names a concrete defect is escalated or dispatched as its own fix unit through the ordinary dispatch path, not resolved inside this review.

## Next Joseki

`fleet_liveness_check` (Phase A) supplies this card's own step 1 input on the faster cadence; this card never replaces it. A concrete, actionable defect this review surfaces gets its own fix dispatch through the ordinary register/dispatch path, not a bespoke repair inside this card.

## Repair Joseki

Explicitly absent as a card. A rubber-stamp critique or a stalled work stream is a finding for the operator to act on, not a mechanical repair this card performs on itself.
