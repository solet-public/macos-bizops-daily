# Request Scoped Landing

Article Layer: 2

Article Role: joseki_catalog

Article Tags: planning-stage:post-approval, planning-stage:wbs-execution, evidence-category:joseki, domain:platform-operations, domain:quality-gates


JOSEKI_KEY: request_scoped_landing
DESCRIPTION: The requester's half of a Git-Controller landing in a shared, dirty checkout: write the lane's evidence record, derive the touched-file list from read-only git status in the spellings git actually tracks, run the pre-handoff gate sweep, send ONE landing request carrying a proposed branch name, the full commit message, the exact file scope, an explicit scoped-staging instruction and a quiescence attestation — then verify the landing independently with ancestor checks instead of believing the reply. Use whenever a session that is not Git-Controller has finished work that must reach master. Not for Git-Controller's own side of the exchange, which is its own gate-and-commit procedure, and not a way to mutate git yourself.
EMBEDDING_DESCRIPTION: Hand finished work to the Git-Controller session for committing and merging, when many sessions share one working tree and other sessions' unrelated edits are dirty in it at the same time. Write the evidence record, list exactly which files you touched using the path spellings git tracks rather than a symlinked spelling, run the quality gates before asking, then send one request naming the branch, the commit message, the file scope, an instruction to stage only those named files, and confirmation that nobody else is editing them. Afterwards prove the work really reached the main branch by checking that both the commit and its merge are ancestors of it, and by reading back which files were actually staged.

## Input Contract

- A finished, quiescent unit of work whose edits are confined to the lane's own declared file surface
- A shared checkout in which other sessions' unrelated edits may be dirty at the same time, and in which only the Git-Controller session may mutate git
- A live role binding for `Git-Controller` that a role-addressed send can resolve
- Bindings: evidence_record_path, branch_name, commit_message, file_scope, landing_request

## Output Contract

- A workbench evidence record the landing request cites, carrying what changed, what was measured, and what was deliberately not done
- One landing request delivered to the current holder of the `Git-Controller` role, carrying branch name, commit message, scope summary, the exact file list, the scoped-staging instruction, and the quiescence attestation
- An independently measured landing verdict: the commit AND its merge confirmed as ancestors of the main branch, plus the actually-staged file set read back from the commit — recorded on the run flow, not inferred from the reply

## Sequence

[ ] 1. Write the lane's evidence record to the workbench
    a) Author the workbench evidence record the landing request will cite — what changed, what was measured with which command, and what was deliberately left undone [agent-executed: the record is authored, not produced by a verb]

[ ] 2. Derive the touched-file list from the working tree, in tracked spellings
    a) Derive the file list from read-only git status over the lane's own surfaces, writing every path in the spelling git itself tracks, and separate any file shared with another lane into its own line item [agent-executed: read-only git; a list written from memory is an ownership claim, not a measurement]

[ ] 3. Run the pre-handoff quality-gate sweep on the current tree
    a) Run the registered pre-completion gate sweep and keep its verdicts as evidence for the request [agent-executed: child joseki run_platform_quality_gates covers the platform-verb half; a green sweep here is a self-check, never a commit authorization]

[ ] 4. Send one scoped landing request along the fleet's landing route
    RESULT_PROCESSOR_KIND: deterministic_continuation
    a) Send the assembled landing request, role-addressed, to whichever role owns landing routing for this fleet (plugin::agent_messaging_plugin::peer_send_by_name)
        Arguments:
        {"name": "<<BIND:landing_recipient>>", "content": "<<BIND:landing_request>>"}

[ ] 5. Verify the landing independently, then record the verdict
    a) With read-only git, confirm the reported commit AND its merge are both ancestors of the main branch, and read back the staged set from the commit itself; record both results as the landing verdict [agent-executed: read-only git; landed is an ancestor check, never a head-log read]

## Expected Step Count

5 steps.

## Binding Guidance

- Bind `evidence_record_path` to the artifact written in step 1 and cite it inside `landing_request`; a request with no citable record forces Git-Controller to reconstruct scope from prose.
- Bind `file_scope` to step 2's derived list, in TRACKED spellings only. Where a repository path is reachable through a symlinked directory, the symlinked spelling returns EMPTY from `git status` and `git log`: a file listed that way is silently dropped from a scoped landing, and the drop is invisible in the reply.
- Bind `branch_name` to a fresh `feature/<descriptive>` or `fix/<descriptive>` name. Git-Controller's procedure asks the requester for it and pings back when it is missing, so omitting it costs a round trip.
- Bind `commit_message` to the complete multi-line message including its "Why" body. The requester owns the message; Git-Controller does not compose one.
- Bind `landing_request` (step 4's `content`) to a single message that carries, at minimum: branch name, full commit message, a two-to-four sentence scope summary naming the work and the session that did it, the exact file list from `file_scope`, the explicit instruction to stage ONLY those files, the gate evidence from step 3, and an attestation that the requesting session is quiescent and no other session is editing the named files. Any pre-stage git operation the landing needs must be stated explicitly — an absent instruction means none, and Git-Controller does not improvise one.
- Bind `landing_recipient` to the role that owns landing ROUTING, which is not always Git-Controller itself. Where a coordinating session routes landings, the request goes there and the coordinator submits it onward; a lane may address Git-Controller directly only when Git-Controller holds a first-party pre-authorization it can read in its OWN inbox that matches this request's shape. A pre-authorization the lane merely cites — an id, a standing grant, a relayed ruling — is declined back to the lane, correctly, because the controller cannot verify it.
- Address step 4 by ROLE, never by remembered instance id: a role-addressed send re-resolves the holder at send time, so the request cannot land on a session that has since been displaced from the role.
- Bind step 5's ancestor checks to the exact commit sha AND the exact merge sha the reply names, both against the main branch. Bind the staged-set read-back to the same commit sha.

## Coherence Obligations

- Scoped staging is not the default. The canonical commit procedure stages every dirty file the ignore rules do not exclude, so in a shared checkout an unscoped request sweeps other lanes' in-flight edits into this lane's commit. The scoped-staging instruction and the exact file list are what make the request safe; a request that omits either has asked for a sweep whether or not it meant to.
- Landed means an ancestor check the requester runs itself, on both the commit and the merge. A reply that says landed, a log line, or a status read of the working tree are each compatible with the work NOT being on the main branch — a working-tree read in particular credits uncommitted edits as landed. Independent verification is a step of this card, not an optional courtesy.
- The staged-set read-back is the other half of that verification: an ancestor-true commit can still carry the wrong files, either because a path was written in a spelling git does not track or because staging was wider than the request. Confirm what was actually committed, not only that something was.
- A file that carries another lane's in-flight edits is not landable by scope alone. Name it in the request as shared, say whose edits are in it, and hold: either the other lane lands first and this lane rebases its own edit on top, or the request waits for a named go from the session that owns the other edits. A relayed or paraphrased go is not a go.
- The requester never mutates git. Read-only inspection is the whole permitted surface for this card; a gate red, a missing branch, or a failed ancestor check is fixed by editing files and re-requesting, never by staging, committing, branching, or resetting locally.
- A green pre-handoff sweep does not authorize the commit. It is a snapshot of the whole tree — on a dirty shared checkout it also reports other lanes' work — and Git-Controller re-runs the gates regardless. Send the verdicts as context, not as a claim of authorization.

## Next Joseki

Explicitly absent as a single successor — a verified landing hands off to whatever the lane exists for (a deployment, a mint, a report to the dispatching session), and no one of those generalizes. The child this card names in step 3 is `run_platform_quality_gates`; expand its sequence inline when instantiating a fragment that runs the sweep as platform verbs rather than as a delegated procedure.

## Repair Joseki

Explicitly absent as a card. Each failure has a different owner and none of them is a retry of step 4. A gate red reported back by Git-Controller is the requester's to fix in the working tree, then re-request with the same branch name. A request declined for missing inputs is completed and re-sent, not escalated. A landing that reports success but fails step 5's ancestor check is NOT re-requested blind: re-read which shas the reply actually named, confirm the role holder that answered, and resolve the discrepancy before any second request — a duplicate request against a partially applied landing is how one change becomes two divergent commits.
