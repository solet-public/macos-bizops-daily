# Mint Seed

Article Layer: 2

Article Role: joseki_catalog

Article Tags: planning-stage:post-approval, planning-stage:wbs-execution, evidence-category:joseki, domain:solet-lifecycle, domain:platform-operations


JOSEKI_KEY: mint_seed
DESCRIPTION: Mint a clean, shareable solet seed from committed code and optionally publish it as a GitHub repository. Assembles an allowlisted, credential-free seed bundle from a committed ref, then — only when publishing — seals the bundle into a fresh neutral-identity commit and publishes it with the publish_seed verb, which creates the repo (PRIVATE by default) or appends a re-mint commit to an existing seed repo, never clobbering and never force-pushing. No GitHub token ever enters the runtime — publish_seed exercises the ambient gh authority under the Ambient-Shell Invariant (shell-only, value-never). Use to produce a distributable seed repo, or (omitting the publish steps) just to leave a clean local seed folder. Not for birthing a live solet — that is mint_and_birth_local.
EMBEDDING_DESCRIPTION: Mint and publish a shareable solet seed repository: assemble a clean allowlisted seed bundle from a committed ref into a local folder, then to publish — seal the bundle with the validate_and_seal_seed_bundle verb into a neutral-identity commit, confirm private-by-default visibility (public only on an explicit operator order), and publish with the publish_seed verb, which creates the GitHub repo or append-updates an existing seed repo (append-only re-mint commit, fast-forward only, never clobber, never force) with no platform-held token. Use to produce a distributable seed repo or just a clean local seed folder.

## Input Contract

- A capability selection: EITHER a named bundle (`bundle_name`, e.g. `macos_free_minimal`) OR an explicit `plugins` list — exactly one
- A committed git ref to assemble from (default `HEAD`); only committed code is seedable
- A destination `output_dir` (a scratch dir outside the shared worktree by default) that is absent or empty
- To publish: a product repo name following the seed naming convention `<yyyy-mm-dd>_<platform>_<consumer>_<short-commit>`, all lowercase (a strict subset of GitHub's `[A-Za-z0-9._-]{1,100}` rule, with `ananta` banned as a substring; NOT the solet name), an optional `owner` (defaults to the authenticated `gh` login), and an explicit visibility choice (PRIVATE default; public only on an explicit operator order)

## Output Contract

- A clean, validated, `.git`-free seed bundle at `output_dir` (always)
- When publishing: the same bundle sealed into a fresh independent `.git` with exactly one neutral-identity commit, and a GitHub repository whose `main` branch carries the sealed content — either a newly created repo whose sole commit IS the sealed commit, or an existing seed repo with an appended neutral-identity re-mint commit that preserves prior history (append-only, fast-forward)
- No secret, no operator identity, and no origin history in the published contents (clean-by-construction + fail-closed seal gate; publish re-verifies the seal before pushing)

## Sequence

[ ] 1. Assemble a clean seed bundle from the committed ref
    RESULT_PROCESSOR_KIND: deterministic_continuation
    a) Assemble a clean seed bundle from the committed ref into output_dir (plugin::seed_factory_plugin::assemble_seed)

[ ] 2. (Publish only) Re-validate and SEAL the assembled bundle
    RESULT_PROCESSOR_KIND: deterministic_continuation
    a) Re-validate the actual folder and seal it into a fresh neutral-identity commit, returning {repo_path, commit_sha, tree_hash} (plugin::seed_factory_plugin::validate_and_seal_seed_bundle)

[ ] 3. (Publish only, PUBLIC only) Obtain explicit operator confirmation for public visibility
    a) Obtain explicit operator confirmation before publishing public — private is the default; public is never chosen by omission [agent-executed: surface the choice via post_message and wait for an explicit operator go]

[ ] 4. (Publish only) Publish the sealed commit — create or append-update, never clobber, never force
    RESULT_PROCESSOR_KIND: deterministic_continuation
    a) Publish the sealed commit to GitHub: create the repo (private by default) or append a re-mint commit to an existing seed repo (plugin::seed_factory_plugin::publish_seed)

## Expected Step Count

4 steps for a full publish; 1 step (assemble only) for a local mint with no publish.

## Binding Guidance

- Bind step 1 `bundle_name` XOR `plugins` (exactly one) and `ref` (default `HEAD`, or a tag/branch for a reproducible seed); bind `output_dir` to a scratch path outside the shared worktree.
- Bind step 2 `output_dir` to step 1's returned `bundle_path`, `expected_ref` to step 1's `ref`, and `manifest_hash` to step 1's `manifest_hash` — all from the same run, so the seal provenance matches what was assembled.
- Bind step 4 `repo_path` to step 2's returned `repo_path` and `sealed_commit_sha` to step 2's `commit_sha` — same-run provenance, mirroring the step-1→2 binding rule. Bind `repo_name` to a name following the seed naming convention `<yyyy-mm-dd>_<platform>_<consumer>_<short-commit>`, all lowercase (validated as a strict subset of GitHub's `[A-Za-z0-9._-]{1,100}` rule, with `ananta` banned as a substring; a non-conforming name → `repo_name_invalid`; explicitly NOT the solet name); leave `owner` unset to publish under the authenticated `gh` login, or set it to target an org.
- Bind step 4's `visibility` to `private` unless step 3's explicit operator confirmation selected `public`. There is no default-public path.
- Steps 1, 2, and 4 are platform verbs (`process_call`). Step 3 is agent-executed — a HUMAN consent gate (operator confirmation), not a credential step; it binds step 4's `visibility="public"` and is skipped entirely for the private default.

## Coherence Obligations

- The sealing (step 2) is what makes publish trustworthy: the platform — not the agent — creates the commit over the just-validated tree, and `publish_seed` RE-VERIFIES that seal (HEAD == sealed sha, neutral identity, clean tree) before any network op. A post-assemble hand-edit to `output_dir` is uncommitted and cannot reach GitHub. Never `git add`/`git commit`/`git push` the bundle by hand.
- If step 2 fails validation (a secret, operator identity, symlink, or gitlink was found), do NOT publish — surface the failing checks via `post_message` and stop. Never sanitize-then-publish around the fail-closed gate; re-assemble a clean bundle instead.
- Private is the invariant, not a preference: never pass `visibility="public"` without step 3's explicit operator confirmation, and never infer public from silence.
- Never work around a `publish_seed` refusal with manual `gh`/`git` — the refusal IS the never-clobber / never-force gate working. `existing_repo_not_a_seed` means the name belongs to a repo that is not factory-sealed: pick a different `repo_name`, the verb never clobbers. Updates are append-only re-mint commits pushed fast-forward-only; there is no force path.
- The publish steps are OPTIONAL: `mint_seed` without steps 2–4 just leaves the clean seed folder for local birth (mint_and_birth_local) or hand-off.

## Next Joseki

`remint_and_respond` — the successor card for the case this one does not cover: updating a seed repository that already has adopters. It wraps this ladder in the obligations a release to real users carries — predecessor lineage read from the published head, an assert-by-name census of the assembled bundle, the born-clone publication gate, a release cut from the notes, and the feedback closures in the same session. Use this card for a first mint or a local bundle with no audience; use `remint_and_respond` once there is one. Alternatively `mint_and_birth_local` — when the intent is to birth a live solet from the assembled seed rather than publish it. All three share step 1 (`assemble_seed`) and diverge after: publish (this card), release-to-adopters (`remint_and_respond`), or local birth (`mint_and_birth_local`).

## Repair Joseki

Explicitly absent as a card. A failed assemble is fail-closed (no partial bundle) — read the error (`seed_path_not_in_ref` means commit the named path first; `capability_subset_incoherent` names the missing binding) and re-run. A failed seal leaves the target untouched (no `.git`, no commit) — fix the source, re-assemble, re-seal. A failed publish is fail-closed and one code per refusal: `remote_moved`/`gh_network_failed` are transient — re-invoke step 4 (idempotent; it re-fetches and short-circuits to `unchanged` if the tree already published); `existing_repo_not_a_seed` — pick a different `repo_name`; `not_sealed` — re-run step 2; `visibility_mismatch` — the repo's visibility differs from the request (the verb never flips it; change it as a separate operator act, then re-publish); `gh_unauthenticated` — run `gh auth login` (never pass a token to the verb).
