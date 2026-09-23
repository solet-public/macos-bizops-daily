# Deploy Local Blue-Green Release

Article Layer: 2

Article Role: joseki_catalog

Article Tags: planning-stage:post-approval, planning-stage:wbs-execution, evidence-category:joseki, domain:self-deployment, domain:platform-operations


JOSEKI_KEY: deploy_local_blue_green_release
DESCRIPTION: Deploy committed code to the local solet via the materialized-release blue-green swap. Checks router swap state, captures the manifest etag with a dry run, commits the cutover under a compare-and-swap etag, confirms the old color drains, and verifies plugin health on the new release. Use after a merge to master when the running platform must pick up new code with zero downtime; not for manifest/plugin-set changes requiring binding review.
EMBEDDING_DESCRIPTION: Deploy new committed code on the local solet with zero downtime using the blue-green release swap: check the router swap status for the active color, read the current manifest etag via an apply-manifest dry run, commit apply_manifest with the CAS etag so a new release color builds and cuts over, confirm cutover completion and old-color drain, verify plugin health on the new release. Routine local code deploy after a merge to master.

## Input Contract

- A merged, pushed commit on master whose code the live platform must adopt
- The live manifest plugin list (pass-through unchanged for pure code deploys)
- No swap already in progress; exactly one active color on the router

## Output Contract

- A new materialized release (current symlink -> rel-<timestamp>-<sha>) serving as the active color
- The prior release preserved as previous (durable rollback path)
- The old color drained and reaped (no drain entries remaining)

## Sequence

[ ] 1. Verify router state and that no swap is in progress
    a) Verify router state and that no swap is in progress (service_interface::local_self_deployment_service::swap_status)

[ ] 2. Read the current manifest etag with a dry run
    a) Read the current manifest etag with a dry run (service_interface::lifecycle_management_service::apply_manifest)

[ ] 3. Commit the cutover under the captured etag
    a) Commit the cutover under the captured etag (service_interface::lifecycle_management_service::apply_manifest)

[ ] 4. Confirm cutover completion and old-color drain
    a) Confirm cutover completion and old-color drain (service_interface::local_self_deployment_service::swap_status)

[ ] 5. Verify plugin health on the new release
    a) Verify plugin health on the new release (service_interface::lifecycle_management_service::list_plugins)

[ ] 6. Record the deploy outcome and step state
    a) Record the deploy outcome and step state (service_interface::thinking_service::record_work_breakdown_structure_step_state)

## Expected Step Count

6 steps.

## Binding Guidance

- Bind step 2 and step 3 `new_manifest` to the LIVE manifest content unchanged for a pure code deploy; any plugin-set change makes this a different (manifest-change) procedure with binding-validation review.
- Bind step 3 `expected_etag` ONLY from step 2's `current_etag` in the same run — never from a stale read.
- Bind step 3 `reason` to carry the merge SHA being deployed, for the release ledger audit trail.
- Bind step 4 as repeat-until: `active_color` flipped AND `drain_entries` empty AND `swap_in_progress` false; the release build plus new-color boot takes minutes — poll patiently, do not resubmit.
- Step 5 passes no filter: review the full roster for `status=error` or `is_running=false` rows.

## Coherence Obligations

- Never commit a cutover without a same-run dry-run etag — the CAS is the guard against racing a concurrent deploy, and it only works if the read is fresh.
- A swap is complete when the OLD color leaves the drain entries (finisher reaped), not merely when `active_color` flips; declaring success at the flip leaves an undrained process holding resources.
- New verbs introduced by the deployed commit are live only after the new color's registry build: verify them by process discovery before starting dependent work, and expect a brief burst of namespace errors from flows caught mid-swap (self-terminating).
- Wait for startup quiescence before submitting model-dependent work to the new color: knowledge-base re-ingest competes for the embedder immediately after boot.

## Next Joseki

Explicitly absent — post-deploy live verification is performed inline today; it should graduate to its own card once the pattern stabilizes.

## Repair Joseki

Explicitly absent as a card. The platform repair verbs are `service_interface::local_self_deployment_service::swap_rollback` (inside the drain window: re-point the router to the prior color) and `service_interface::local_self_deployment_service::rollback_release` (any time after: durable code rollback to the previous materialized release). A dedicated rollback joseki should be authored when these have a proven multi-step procedure around them.
