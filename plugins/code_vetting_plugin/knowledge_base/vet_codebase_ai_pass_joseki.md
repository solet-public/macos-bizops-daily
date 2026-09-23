# Vet Codebase — AI Critic Pass (L2 lenses + L3 adversarial verify over an L1 run)

Article Layer: 2
Article Role: joseki_catalog
Article Tags: planning-stage:post-approval, planning-stage:wbs-execution, evidence-category:joseki, domain:code-vetting

Embedding Description: The joseki card that drives the AI layer of the code-vetting suite over a completed deterministic run — dispatching the tier-aware L2 critic lenses as inference steps whose prompts and judgment standards are retrieved from the plugin knowledge base, then the L3 adversarial verification that re-stamps each candidate confirmed or refuted, over the L1 evidence payloads routed by run_id through the read-verb. Covers the single-card phase sequence (the L1 deterministic run carrying the rulebook-integrity gate, the L2 lens fan-out, the L3 verify, the report persist), the substrate split by which a local run is fully self-serve on platform inference while a subscription run is agent-driven from the same card, the target-class-aware lens roster where a foreign target skips one remaining platform-local lens with a coverage record, and the run_id-through-read-verb payload-routing constraint that deterministic steps cannot pipe a prior step's runtime result.

**When you need this**: running the full AI critic pass over a codebase after the deterministic vet_codebase run; understanding how the L2 lenses retrieve their judgment guidance and consume the L1 evidence payloads; how a foreign-target run trims its lens roster; why a stale rulebook blocks the AI pass; how the local-substrate and subscription-substrate runs share one card.

`JOSEKI_KEY: vet_codebase_ai_pass`

`DESCRIPTION:` Drive the AI critic layer over a completed deterministic vetting run. Runs the L1 vet_codebase pass for a run_id, gate-checks rulebook integrity (a stale rulebook blocks the AI pass — every AI verdict would be computed against a moat that no longer matches canon), dispatches the tier-aware L2 critic lenses as inference steps that retrieve their prompt and judgment guidance from the plugin KB and consume the L1 evidence payloads by run_id through the read-verb, then runs the L3 adversarial verification that re-stamps each candidate confirmed or refuted, and persists the report. Not the commit gate — advisory, report-not-gate (R5). Local substrate is fully self-serve on platform inference; subscription substrate is agent-driven from the same card. Use for the on-demand full-AI vet of a self or foreign target; use the bare vet_codebase verb alone for a fast deterministic-only scan.

`EMBEDDING_DESCRIPTION:` Run the AI critic pass of the code-vetting suite over a codebase: execute the deterministic L1 scanners for a run, verify the assembled rulebook is not stale (blocking the AI pass if it is), fan out the tier-aware L2 critic lenses as inference steps that pull their judgment guidance from the knowledge base and read the L1 evidence payloads by run id, adversarially verify each candidate finding through perspective-diverse refutation, and persist the ranked report. The on-demand full-AI vet as opposed to the fast deterministic-only scan; a foreign target trims the platform-local lenses with an honest coverage record; local runs are self-serve on platform inference and subscription runs are agent-driven from the same card.

## Input Contract

- A target the L1 verb can vet: the local git worktree (self-vet) or, once foreign-target support lands, a registered read-only foreign target. `target_class` (self | foreign) is DERIVED from the target, not passed.
- Substrate selection (local | subscription) binds from the caller's run-profile ONCE at card start and stays fixed for the whole card — no mid-card substrate switching (the metrics substrate-provenance field would lie).
- No payloads are passed between steps as arguments: everything downstream is fetched by `run_id` through the read-verb (the deterministic-steps-cannot-pipe-a-runtime-result constraint).

## Output Contract

- A persisted `vetting_runs` record + a severity-ranked report for `run_id`, with L2 candidates re-stamped confirmed/refuted by L3, an integrity banner if the rulebook was stale, and per-lens coverage records (including the skipped platform-local lens, kb_doc_fidelity, on a foreign run).
- No commit is authorized (advisory; the commit gate is untouched).

## Sequence (single card, phases inline)

[ ] 1. Run the deterministic L1 vetting pass over the target — the RULEBOOK-INTEGRITY GATE rides this step's result
    RESULT_PROCESSOR_KIND: deterministic_continuation
    a) Run the L1 scanner pipeline, returning the run_id + bounded report + L1 evidence payloads (plugin::code_vetting_plugin::vet_codebase)
        Arguments:
        {"scope": "<<binding: whole-tree | diff-scoped | subsystem>>"}
    # Emits: run_id, report, and the L1 evidence payloads (candidate dead symbols, literal-frequency
    # table, structural metrics, test-reach) persisted on the run record for the read-verb below.
    # GATE FAILURE POLICY (the rulebook-integrity gate — no separate step, per the ReAct
    # contract a result-evaluation gate is this step's failure policy, not a phantom step):
    #   - SELF target: rulebook_sync ran inside this step's roster; any `stale_rulebook`
    #     finding (or the report integrity banner) in the result → blocks_continuation:
    #     true. Both CORRUPT and STALE covered.
    #   - FOREIGN target: rulebook_sync is not_applicable on the target, but CORRUPT is
    #     still structurally gated INSIDE this step — the verb's in-process L3-heuristic
    #     loads the assembled rulebook (pipeline.py: load_rulebook() builds the
    #     DNF screen/verifier) and W3C-1a verifies the whole-artifact hash FAIL-LOUD at
    #     every load, so a corrupt artifact cannot produce a step-1 result at all.
    #     Residual foreign-STALE risk is bounded by the daily self-vet heartbeat (W3C-3)
    #     — see Coherence Obligations.
    #   BLOCKING, not waiverable. Repair = re-run the assembler
    #   (`python -m code_vetting_plugin.rulebook.assembler`), land the artifact diff via
    #   the 5-step Git-Controller handoff, re-run this card from step 1. Two failed
    #   repair attempts → stop for an operator decision.

[ ] 2. Fetch the run's L1 evidence payloads by run_id (read-verb; NO argument-piping)
    RESULT_PROCESSOR_KIND: inference
    a) Read the persisted run record (report + evidence payloads) for the run_id (plugin::code_vetting_plugin::get_vetting_run)
        Arguments:
        {"run_id": "<<step_1.result.run_id>>"}
    # PINNED (C3a landed contract) — the return-field paths the L2 steps consume, on the
    # verb's `data`:
    #   report → data.report (string; null on a metrics-only run)
    #   structural metrics → data.structural_metrics (object)
    #   literal table → data.structural_metrics.literals[]
    #   worst offenders → data.structural_metrics.worst_offenders[]
    #   candidate dead symbols → data.dead_symbols.candidates[]
    #     (each: {file, line, name, kind, confidence, dead_lines})
    #   NOTE: "test_reach" is NOT a payload key — it is the L2-judgment FRAMING the
    #   test_adequacy lens applies over data.dead_symbols.candidates[] (the 60% class).
    # get_vetting_run is a fast-return READ-only EDGE verb; unknown run_id → typed
    # run_not_found (fail-loud). It reads DRIVER-persisted runs — see step 1's
    # persistence note in Binding Guidance.

[ ] 3. Dispatch the tier-aware L2 critic lenses (one inference step per lens in the roster)
    RESULT_PROCESSOR_KIND: inference
    # Roster is TARGET-CLASS aware: a SELF target runs all six of its lenses (unchanged by this
    # restoration); a FOREIGN target runs the FIVE universal lenses (the four pre-existing generic
    # lenses plus architecture_universal, restored 2026-07-31 per operator RULING A and the
    # programme brief's §6.4 gap-2 split) and SKIPS the ONE remaining platform-local lens
    # (kb_doc_fidelity) WITH a coverage record per skip (skip is recorded, never silently dropped;
    # protects the per-lens L2→L3 survival metric). The platform-tuned architecture_conformance
    # lens stays SELF-ONLY — it judges conformance to the platform's own ratified rulebook (RB-STATE,
    # RB-NAMESPACE, RB-SERVICE, ...), which has no meaning on a target the suite does not own.
    # architecture_universal is a DISTINCT lens with distinct judgment content, retrieved under
    # its own name — never a reframing of architecture_conformance (the 2026-07-20 foreign-target
    # ruling's explicit bar: reusing a lens name with different content poisons that lens's
    # per-lens precision metric). architecture_universal is scoped foreign-only, matching this
    # restoration's own arithmetic (foreign 4→5, self unchanged); whether self should ALSO gain it
    # is OPEN — architecture_conformance's remit overlaps it only on god-components, so self has no
    # coupling/layering/circular-dependency coverage under either lens today — see
    # guidance_architecture_universal.md's own "Relationship to architecture_conformance" section.
    # Each lens is ONE inference step of the shape:
    #   a) Retrieve this lens's prompt + judgment guidance from the plugin KB (lens prompt BY
    #      REFERENCE): a knowledge_service::search inference step over the assembled-rulebook
    #      universal tier + the lens's guidance article, then the model applies it to the run's
    #      evidence (from step 2) and emits F1 candidate findings naming the rule each breaks.
    #   The judgment guidance each lens retrieves (most ride the rulebook + existing lenses, NOT a
    #   new lens; architecture_universal below is the one exception — a genuinely new, distinctly
    #   named lens, not guidance folded into an existing one):
    #     - ai_slop / correctness lens over the literal-frequency table → retrieve
    #       "Magic-Strings Judgment — Which Repeated Literals Are Real" (guidance_magic_strings.md):
    #       which repeated literals are config/enum/constant candidates vs i18n/test/path noise.
    #     - correctness / test_adequacy lens over the candidate-dead-symbols table → retrieve
    #       "Dead-Symbols Judgment — Adjudicating the Candidate Class" (guidance_dead_symbols.md):
    #       clear the false-positive screens (dynamic dispatch / registry / framework entry /
    #       re-export / test-helper / reflective field) before flagging.
    #     - test_adequacy lens over the test-reach / zero-reach framing → the test_adequacy critic's
    #       own guidance: name the specific untested behavior + the regression it misses, never a %.
    #     - security / architecture lenses over the L1 SAST + structural evidence → the security /
    #       architecture-conformance guidance.
    #     - architecture_universal lens (FOREIGN TARGET ONLY) over the L1 structural metrics +
    #       import/dependency graph → retrieve "Universal Architecture Judgment — Coupling,
    #       Layering, and Boundary Integrity" (guidance_architecture_universal.md): closed-world,
    #       technology-agnostic coupling / layering / god-component / boundary-integrity judgment
    #       that holds for any codebase; never a stack-idiomatic ("in React you should...") claim,
    #       which is open-world and belongs to a later Ruling-C-flow stage, not this lens.
    #   Each lens's retrieval step binds its search query to the lens's target article + tier; the
    #   card cites the article TITLES so lens-prompt evolution never re-authors the card.
    # Sub-step count = size(roster): 6 self, 5 foreign. Emitted candidates carry dimension +
    # constraint_violated + provenance{critic_lens} + verdict=candidate.

[ ] 4. L3 adversarial verification — re-stamp each L2 candidate confirmed | refuted
    RESULT_PROCESSOR_KIND: inference
    # Perspective-diverse refute-skeptics; a candidate survives only if not refuted; every
    # candidate ends CONFIRMED or REFUTED with a ledger line — no silent drops. Dispatch by
    # the substrate bound at card start:
    #   - LOCAL: assemble the in-process driver through the B3c seam —
    #     `build_substrate_verifier` / `assemble_inference_driver` with
    #     `LocalInferenceSkepticTransport` over inference_service.generate_completion
    #     (vacant service → fail-loud, no fallback). Pass `active_tiers` DERIVED from
    #     step 1's target_class (FT-2: the tier stack is derived, never an independent
    #     axis) so the DNF screen + POLICY directive render from the assembled stack at
    #     the correct tier. Fully self-serve platform-side.
    #   - SUBSCRIPTION: the live fleet session driving this card performs the skeptic
    #     dispatch itself via `SubprocessSkepticTransport` (read-only `claude -p`,
    #     GC-safe). B1 redaction applies before any off-machine forward; the PRIVACY
    #     stakes profile HARD-REFUSES off-machine transports — never overridden by the
    #     card. The metered-key structural ban holds on every path.
    # Candidates re-stamped by preserved finding id for the same run_id.

[ ] 5. Persist the AI-pass report + metrics row
    RESULT_PROCESSOR_KIND: deterministic_continuation
    # Write the confirmed + zero-FP findings into the run's report + the vetting_runs metrics row
    # (survival_rate feeds the R4-d rot tripwire). Integrity banner rendered if step 1's gate flagged stale.
    # Persist path (C3a): reuses the driver's existing metrics StateWriter (metrics_writer.persist),
    # which as of C3a also writes the report column — no separate AI-pass write seam exists or is needed.

## Expected Step Count

5 phases. Sub-step count varies with the L2 roster: 6 lens sub-steps on a self target, 5 on a foreign target (the one remaining platform-local lens, kb_doc_fidelity, is skipped-with-coverage-record, not omitted from the count silently).

## Binding Guidance

- Step 1 `scope` binds from the run-profile (whole-tree standing dogfood; diff-scoped pre-land). Step 1 must be a PERSISTING invocation — the bare sync verb persists nothing; the opt-in `persist: true` param on vet_codebase is the invocation the card binds, so the read-verb in step 2 has a run to read.
- Step 2 `run_id` binds from step 1's returned run_id via WBS templating (`<<step_1.result.run_id>>`) — the ONLY cross-step value, and it flows through the read-verb, never as a piped payload (the deterministic-steps-cannot-carry-runtime-results constraint).
- Step 3 each lens sub-step binds its retrieval query to its guidance article title + the assembled-rulebook universal tier; the model consumes step 2's evidence fields for that lens's payload (dead-symbols table for the dead-symbols judgment, literal table for magic-strings, etc.). Lens prompts are referenced, never inlined.
- The rulebook gate binds its verdict to step 1's own result (self: stale_rulebook finding / banner; foreign: the in-verb fail-loud load already gated CORRUPT) — no new verb, no new read, no separate step.
- Step 4 binds substrate from the card-start run-profile (fixed); LOCAL passes `active_tiers(target_class)` into the B3c driver; SUBSCRIPTION binds the skeptic set per candidate (perspective-diverse: correctness / policy / reproduction lenses minimum).
- Step 5 persist path: reuses the driver's existing metrics StateWriter (`metrics_writer.persist`), which as of C3a also writes the report column; no separate AI-pass write seam exists or is needed.

## Coherence Obligations

- The AI pass runs ONLY on a fresh L1 run's evidence for the same run_id — never a stale or foreign run_id (the read-verb is the single source; no step re-derives L1 output).
- A stale rulebook HALTS at step 1's gate: no L2/L3 step runs, because every AI verdict would be computed against a moat that no longer matches canon (the run's trust chain is broken). Do not waiver the rulebook gate to "just get the AI verdicts" — a stale-moat verdict is worse than no verdict.
- A foreign target's skipped platform-local lens (kb_doc_fidelity) is RECORDED as a coverage row, never silently dropped — the report's coverage-honesty matrix shows it as not-applicable, and the per-lens survival metric is not polluted by a phantom zero.
- L3 creates no findings — it only re-stamps L2 candidates by preserved id. A candidate with no L3 verdict is unverified and is not rendered as confirmed.
- Report-not-gate: a confirmed blocker here does NOT block a commit; it informs. The Git-Controller gate run remains the sole commit authority.
- Foreign-target residual: STALE (as opposed to CORRUPT) rulebook drift is only detectable by rulebook_sync on a self tree; a foreign AI pass therefore trusts the artifact within the window since the last self run — the W3C-3 daily L1-only heartbeat bounds that window to ≤1 day. If the heartbeat is not yet live, run a self L1 pass first when moat-trust matters.
- The DO-NOT-FLAG screen and POLICY directive reach L3 refuters through the assembled stack ONLY — quoting DNF rules inline into skeptic prompts re-introduces exactly the drift the assembler + rulebook_sync exist to kill.

## Substrate Split (JOS-05 resolution)

- LOCAL substrate: the card is fully self-serve platform-side — L2 as inference steps on platform inference, L3 via the B3c `LocalInferenceSkepticTransport` (vacant inference binding fails loud; the repair is operator-side service binding, never a card fallback). No bridge dispatch needed.
- SUBSCRIPTION substrate: a live fleet session runs the SAME card and performs the dispatch itself (`SubprocessSkepticTransport` where GC-safe) — agent-driven by construction, because platform-side execution cannot address bridge-bound agent threads and does not need to.
- Selection surface: the run-profile's substrate field, bound ONCE at card start, immutable for the card's duration; the L3 metrics row records it as substrate provenance (which engine reviewed/refuted — the B2 field).
- Privacy interaction: the stakes profile composes orthogonally — PRIVACY hard-refuses any off-machine transport regardless of substrate; B1 redaction (sensitive-dim evidence + raw code withheld) applies before every off-operator forward.

## Repair Joseki

- Step 1 stale_rulebook → run the rulebook assembler, commit via the Git-Controller handoff, re-run this card (the AI pass resumes from step 1 against the fresh moat). Not a waiverable gate.
- A lens or verify step that errors (transport down, inference timeout) → re-invoke that step; the run_id + persisted evidence are stable, so the AI pass resumes without re-running L1. L3-specific: a TRANSPORT failure re-invokes the same step with the same transport; a VACANT local-inference binding is not a retry case — it fails loud and the repair is operator-side service binding (B3c) before the card resumes. Partially-verified candidate sets resume cleanly: re-stamping is idempotent by preserved finding id, and an unverified candidate is never rendered confirmed.
- The card follows the authored-joseki PROVING-RUN discipline: it is NOT trusted as mechanized until ONE clean proving run completes (the promote-workbench card is the cautionary precedent of a card trusted before proving).

## Next Joseki

Explicitly absent — the AI-pass report is the terminal artifact; a human or coordinator reads it. A scheduled cadence is the C5/W3C-3 concern (a cron cannot fire an inference step, so the daily lane is L1-only), not this card.
