# Remint And Respond

Article Layer: 2

Article Role: joseki_catalog

Article Tags: planning-stage:post-approval, planning-stage:wbs-execution, evidence-category:joseki, domain:solet-lifecycle, domain:platform-operations


JOSEKI_KEY: remint_and_respond
DESCRIPTION: The full release cycle for a seed repository that already has adopters, including one published to MORE THAN ONE HOME: read the published predecessor's provenance stamp without cloning, assemble from the landed ref with that predecessor so the lineage chains, confirm the deployed factory governs this mint by comparing release-governed CONTENT rather than the release's name, census the assembled bundle by asserting every release-critical path BY NAME, re-check the notes against the commit range because a ladder can grow fixes mid-run, seal it, put it through the born-clone publication gate under a PINNED reference environment with the tolerance read from the prior mint's verdict file, publish the sealed commit to each home with that home's visibility asserted from a read taken in the same run, cut a release per home, and — in the same session, never deferred — close the outstanding feedback items after verifying by content that the cited landing actually fixed each one. The successor to mint_seed: mint_seed produces and publishes a bundle, this card runs a release to people who are waiting on one. Use for every update to a live seed repository. Not for a first mint with no predecessor and no audience.
EMBEDDING_DESCRIPTION: Ship an update to a seed repository that people are already running, and answer them in the same pass — including a seed published to MORE THAN ONE REPOSITORY, where the same sealed commit is appended to every one of its repositories in the same run. Read the currently published provenance stamp straight from the remote so the new build chains onto it, build from a committed reference, and check that the machine doing the building is running current policy by comparing the files that actually govern it rather than trusting a version label. Check the built folder by name for every file the release depends on rather than eyeballing a count, re-read the written notes against what actually landed, then seal. Prove a fresh clone can pass its own checks on a machine carrying only what the bundle guarantees, with the checking environment spelled out rather than inherited from whoever happened to run it. Publish to each repository with that repository's public-or-private state read fresh from the remote and asserted rather than remembered, cut a release on each, and close the reported issues only after reading the change that fixed each one. Publishing and answering are one act here, because a release nobody is told about is not a response.

## Input Contract

- An existing published seed repository with adopters, whose current head carries a readable provenance stamp
- A landed, committed ref carrying everything the release claims, and a capability selection (a named bundle or an explicit plugin list) for the assemble
- A written release-notes entry for this update, and the outstanding feedback items this release answers, each with the landing that fixed it identified
- A census list: every release-critical path this update depends on, named exactly as it must appear inside the bundle
- The set of HOMES this seed is published to — one entry per repository, each carrying its owner and repository name. A seed with one home is the ordinary case; a seed with two is not an exception to be improvised around
- The prior published mint's born-clone verdict file, named as the tolerance BASELINE for this run's verdict
- A pinned reference environment for the gate: the interpreter the seed itself declares, and the PATH entries the minting host needs for its reference pass
- Bindings: predecessor_stamp_path, bundle_name, output_dir, ref, homes, census_paths, release_notes_entry, feedback_items, baseline_verdict_path, gate_interpreter, gate_reference_path

## Output Contract

- A sealed bundle whose provenance chains onto the published predecessor's stamp, with the census recorded as an assert-by-name result rather than a count
- A born-clone gate verdict keyed to that bundle's sealed sha, written only on a pass — its absence is a refusal, and publish re-verifies it
- The sealed commit published to EVERY home as a create or an append-only re-mint, each home's visibility asserted from a read taken in this run, plus a release cut per home; every home's post-publish head and tree read back AT THE REMOTE rather than taken from the publish envelope
- Every outstanding feedback item this release answers, closed in the same session with the landing that fixed it cited and verified by content, and the release notes standing as the response surface; items whose fix the shipped artifact itself describes as incomplete are answered with a comment and left open

## Sequence

[ ] 1. Read the published predecessor's provenance stamp without cloning
    a) Fetch the provenance stamp from the CANONICAL home's current head over the repository API and write it to a scratch file, so the lineage source is what adopters actually have rather than what a local folder remembers [agent-executed: ambient repository authority, no clone, no token into the runtime]

[ ] 2. Assemble the bundle from the landed ref, chaining the predecessor
    RESULT_PROCESSOR_KIND: deterministic_continuation
    a) Assemble a clean seed bundle from the committed ref, declaring the predecessor stamp (plugin::seed_factory_plugin::assemble_seed)
        Arguments:
        {"bundle_name": "<<BIND:bundle_name>>", "output_dir": "<<BIND:output_dir>>", "ref": "<<BIND:ref>>", "predecessor": "<<BIND:predecessor_stamp_path>>"}

[ ] 3. Confirm the deployed factory governs this mint, by CONTENT
    a) Compare the release-governed inputs between the deployed release and the ref being minted — the factory plugin's source, the seed manifest, the capability bundles, and the profile templates — and proceed only when they agree. Do NOT compare the release's NAME to the ref: newer shipped content on an unchanged factory is the normal shape of an update, so a name comparison refuses correct mints [agent-executed: four path comparisons; equality on all four is the pass]

[ ] 4. Census the assembled bundle by asserting every release-critical path by name
    a) Assert the presence of EVERY release-critical path inside the assembled bundle, by exact path, and fail the mint on the first absence [agent-executed: an assert-by-name list; a file count or an eyeballed listing is not a census]

[ ] 5. Re-check the release notes against what actually landed
    a) Compare the notes entry inside the assembled bundle against the commit range this release ships, and account for every landing: either it appears in the notes, or it is named in the release BODY as included beyond the notes entry. The notes are written before the ladder finishes, so a ladder that grew fixes mid-run ships notes that omit them [agent-executed: the release body is the declared catch-up surface because it is editable without a re-mint; the notes are not]

[ ] 6. Validate and seal the assembled bundle
    RESULT_PROCESSOR_KIND: deterministic_continuation
    a) Re-validate the actual folder and seal it into a fresh neutral-identity commit (plugin::seed_factory_plugin::validate_and_seal_seed_bundle)
        Arguments:
        {"output_dir": "<<BIND:output_dir>>", "expected_ref": "<<BIND:ref>>", "manifest_hash": "<<BIND:manifest_hash>>"}

[ ] 7. Run the born-clone publication gate under a PINNED reference environment
    a) Birth a throwaway clone of the sealed bundle under a short workdir and run its shipped register twice — once under the declared minimum environment, once under the minting host's reference environment — with the interpreter and the reference PATH supplied EXPLICITLY rather than inherited from whoever invoked the card [agent-executed: run it with the DEPLOYED release's gate module, so the verdict lands where publish re-reads it]
    b) Write the verdict keyed to the sealed sha only if the run passed with no failures, nothing missing, no unclassifiable skips, AND its tolerated set a SUBSET of the baseline verdict's. Encode those conditions in the runner; a twelve-item list compared by eye is not a check [agent-executed: absence of a verdict is a refusal, and a verdict is never written for a run that did not satisfy all four]

[ ] 8. Read each home's visibility at the remote, in this run
    a) For every home, read its current visibility from the repository API now, and bind that value for its publish. The operator sets these by hand and they are not the card's to change; a remembered value or a constant in a script is not a read [agent-executed: one read per home, in this run, immediately before publishing]

[ ] 9. Publish the sealed commit to every home
    RESULT_PROCESSOR_KIND: deterministic_continuation
    a) Publish the sealed commit to each home in turn — create the repository or append a re-mint commit — asserting that home's visibility as read in step 8 (plugin::seed_factory_plugin::publish_seed)
        Arguments:
        {"repo_path": "<<BIND:repo_path>>", "sealed_commit_sha": "<<BIND:sealed_commit_sha>>", "repo_name": "<<BIND:repo_name>>", "owner": "<<BIND:owner>>", "visibility": "<<BIND:visibility>>"}
    b) Read each home's new head and tree back AT THE REMOTE and confirm the tree equals the sealed tree hash. The publish envelope reports what was asked for; the remote reports what happened [agent-executed: one read per home after its publish]

[ ] 10. Cut the release from the written notes entry, on every home
    a) Create the release on each home from this update's notes entry, tagged per the release convention, carrying any landings named in step 5 as included beyond the notes and any environment corrections disclosed in step 7 [agent-executed: ambient repository authority; a home that receives the artifact and no release leaves its adopters untold]

[ ] 11. Verify and close the feedback items this release answers, in the same session
    a) For EACH item, read the cited landing's content and confirm it fixes what the item reported, before writing any closure. A commit whose subject matches is not evidence; a commit that names the item and describes its mechanism is [agent-executed: verification precedes the closure, never follows it]
    b) Close each verified item citing its landing, comment without closing on any item the shipped artifact itself describes as incompletely fixed, and close a parent round only when every child has closed [agent-executed: the response half, never deferred to a later session]

## Expected Step Count

11 steps for a full remint-and-respond cycle, and none is skipped. Steps 8 through 10 repeat per home: a two-home seed runs two publishes, two remote read-backs and two releases inside those single steps.

## Binding Guidance

- Bind `predecessor_stamp_path` to the stamp fetched in step 1, read from the PUBLISHED head rather than from a local copy of a previous mint. The predecessor input may point at a seed directory or at the stamp file itself; a missing or malformed stamp fails closed rather than quietly minting an unchained lineage.
- Bind `bundle_name` (or an explicit plugin list — exactly one of the two) and `ref` to the LANDED commit that carries everything this release claims. Assemble reads the committed ref, never the working tree, so anything still dirty is simply absent from the bundle and any claim about it in the notes is false.
- Bind `census_paths` to the paths this release actually depends on, and assert each by exact name inside the assembled bundle. The class this catches is a runbook step that names a file the copy allowlist never shipped: the instruction resolves for whoever wrote it and resolves to nothing in an adopter's clone, and no gate that runs on source can see it.
- Bind step 4's `output_dir` to step 2's returned bundle path, `expected_ref` to step 2's `ref`, and `manifest_hash` to step 2's returned manifest hash — all from the same run, so the seal's provenance describes what was actually assembled. A manifest hash identical to the predecessor's where a payload change was expected is the tell that the assemble ran against stale deployed policy.
- Bind step 7's workdir to a SHORT path. The gate refuses a workdir too long to hold a unix socket, and it refuses up front rather than letting socket-binding entries fail and be reported as bundle defects.
- Bind `gate_interpreter` to the interpreter the SEED declares, not to whatever `python3` resolves to on the minting host. The gate builds its throwaway venv with a bare interpreter name, so a host whose `python3` has moved to an unsupported version cannot install the seed's own packages and the gate RAISES rather than failing — and a gate that cannot measure is not a stricter gate, it is no gate. Supplying the declared interpreter moves the host toward the adopter's environment; it is a correction, not a loosening.
- Bind `gate_reference_path` to the minting HOST's real PATH for the reference pass — including any user-local tool directory and any keg-only package directory the host keeps outside its default path. The reference pass exists to CLASSIFY skips, and it inherits the environment of whoever invoked the card: a session with a stripped PATH understates the host, so known live-dependency skips become unclassifiable and the gate blocks on leanness the machine does not actually have. Pin it; do not inherit it.
- Bind `baseline_verdict_path` to the PRIOR PUBLISHED MINT's verdict file, and compare this run's tolerated set as a SUBSET of it. Do not hardcode a count: the tolerated set legitimately shrinks as environment dependencies are removed, and it has — thirteen tolerated entries in one earlier mint, twelve in the next. Subset is the right relation because shrinking is improvement and growing is a new tolerance nobody ratified.
- Bind step 9's `repo_path` and `sealed_commit_sha` to step 6's returned values — same-run provenance, mirroring the step 2 to step 6 binding rule. Bind `repo_name` per home to one of the two accepted forms: the dated-artifact convention, or the durable-home form, a stable kebab-case product name for a long-lived seed repository that accumulates releases. Underscores always require the full dated form, so a typo'd dated name still fails loud instead of passing as a stable one. It is the product name, never the solet's own name.
- Bind each home's `visibility` to the value READ IN STEP 8, in this run. Private remains the default for a seed that has no published home yet, and there is no default-public path for a new repository. For a home that already exists, the value is not a policy choice the card makes — it is a fact about the repository the operator has already set by hand, and the publish asserts it so that a mismatch stops the card. Never carry a visibility from a previous run, a constant, or memory.
- Bind `homes` to every address this seed is published to, canonical first. The canonical home is the one whose provenance stamp seeds step 1's lineage; the others receive the same sealed artifact. A seed with a second, older home does not get a second improvised procedure.
- Bind `feedback_items` to the items this release actually answers, each paired with the landing that fixed it. Verify each landing is an ancestor of the main branch AND read its content before writing it into a closure — a closure citing work that is not on the branch is a false report to the person who raised it, and a closure citing work that is on the branch but fixes something else is a worse one, because it is unfalsifiable from the outside. A remembered mapping of item to commit is a starting point for verification, never a substitute: one such mapping named the wrong commit entirely while naming the right lanes.

## Coherence Obligations

- Publish and respond are ONE act. The response step runs in the same session as the publish, never queued for a later one: a release that ships without its closures leaves the people who reported the defects with no disposition, and "we will answer next session" reliably becomes silence. The release notes are the response surface; the item closures point at them.
- Census failure means no mint. A missing release-critical path is not a note in the release notes and not a follow-up item — it is a stop, because the release's own instructions would then name a file the adopter does not have. Re-assemble after fixing what ships, and re-census.
- A census is an assertion by name, not a count. Counting files, eyeballing a listing, or confirming that the directory "looks right" cannot distinguish a bundle missing exactly the one path this release depends on from a bundle that has it.
- The gate verdict is keyed to the sealed sha and never transfers. A verdict for a different sha is a verdict about a different artifact; absence of a verdict is a refusal, not a neutral state, and publish re-verifies it rather than trusting a verdict handed to it. Passing a gate once does not authorize a later, differently sealed bundle.
- The gate's environment is the whole point: the throwaway runs under what the bundle itself declares an adopter needs, not under the minting machine's own environment. A leaner minting host yields a stricter verdict, never a looser one — so a mint that blocks where a previous one published is that property working, and the reference environment is what to check before calling it a regression. That property holds only when the reference environment REPRESENTS the host. Inherited from a session with a stripped path it reports leanness the machine does not have, and the block is then about the invoker rather than the artifact — indistinguishable from the outside, and the opposite of informative.
- A FLAKY REGISTER ENTRY IS A RELEASE BLOCKER IN ITS OWN RIGHT. The gate is all-or-nothing against an intermittent, and there is no tolerance file for one the way there is for cited paths — so the only moves are to fix it or to re-run until it passes, and re-running until it passes converts the gate into a rubber stamp while shipping the flake to every adopter whose first act is running that same register. Fix it, re-seal, re-gate. An entry that fails one run in six is a defect found here rather than by the person who downloaded it.
- A closure must never contradict the artifact the same release ships. Before closing an item, read what the release notes INSIDE the bundle say about it, and read the fixing commit's own scope statement: if the notes record the item as still open, or the commit scopes itself to making a problem observable rather than removing it, the honest act is a comment explaining what shipped and what did not, with the item LEFT OPEN. Closing it anyway puts the tracker in direct contradiction with the notes shipped beside it — the same surface-contradiction defect these release surfaces exist to remove, recreated on the day it is fixed.
- Publish to a second home is the same act, not a variation on it. Every home receives the SAME sealed commit and the same tree; if two homes end a run at different trees, one was published from something else. Read both back at the remote and compare each to the sealed tree hash — the envelope reports the request, the remote reports the outcome, and only the second is evidence.
- Never work around a publish refusal with hand-run repository commands. Each refusal is a distinct gate doing its job: a name matching neither accepted form, a repository that is not factory-sealed, an unsealed bundle, a lineage that does not chain onto the published head, a visibility that differs from the request. Updates are append-only and fast-forward; there is no force path and no clobber path. A visibility mismatch in particular STOPS the card: it means the read of the operator's setting is stale, and re-running with the other value would override by guess a state they set deliberately. Re-read; if the reading was right, stop and ask.
- Nothing in the shipped release identifies a person. Adopter reports are credited by their substance, not by handle; issue text from an adopter is re-derived generically before any of it reaches a shipped surface, and machine paths, deployment names, and employer or policy names never cross into release notes or knowledge-base content.

## Next Joseki

Explicitly absent — a completed release ends the cycle, and what follows depends on what the next change turns out to be. The predecessor card is `mint_seed`, which covers the narrower act of producing and publishing a bundle with no adopters to answer; this card supersedes it whenever the seed repository already has them.

## Repair Joseki

Explicitly absent as a card. Each stage fails closed in its own way and none of them is repaired by re-running the whole ladder. An assemble failure names its own cause and leaves no partial bundle — read the code and re-run. A census failure is fixed in what ships, then re-assembled and re-censused; it is never waived. A failed seal leaves the target untouched: fix the source, re-assemble, re-seal. A gate that could not run is not a gate that failed — a workdir refusal or a bundle with no usable register means the verification never happened, so publication stays refused until it does. A failed publish is one code per refusal, and transient network codes are safely re-invoked because an identical re-mint short-circuits as unchanged. A failure AFTER a successful publish — release creation or a closure — is finished in the same session rather than deferred: the artifact is already in adopters' hands and the response is what is missing.
