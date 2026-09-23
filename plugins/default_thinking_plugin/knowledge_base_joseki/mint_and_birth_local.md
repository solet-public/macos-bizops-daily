# Mint And Birth Local

Article Layer: 2

Article Role: joseki_catalog

Article Tags: planning-stage:post-approval, planning-stage:wbs-execution, evidence-category:joseki, domain:solet-lifecycle, domain:platform-operations


JOSEKI_KEY: mint_and_birth_local
DESCRIPTION: Mint a clean seed from committed code and birth it into a live local solet in one local platform chain — no GitHub, no network publish. Assembles an allowlisted, credential-free seed bundle into a target folder, then births it in existing-clone mode with the venv provisioned explicitly (the source-only seed folder has no .venv). Use to stand up a new local solet from the current committed code; use mint_seed instead when the goal is a shareable GitHub repo rather than a running solet.
EMBEDDING_DESCRIPTION: Mint a seed and birth a live local solet in one local platform chain: assemble a clean allowlisted seed bundle from a committed ref into a target folder, then birth it in existing-clone mode with provision_venv=True so the source-only seed folder gets its virtual environment built explicitly before genesis runs. No GitHub, no network, no credential — the newborn self-seeds its own isolated Postgres role. Use to stand up a new local solet from committed code.

## Input Contract

- A capability selection: EITHER a named bundle (`bundle_name`, e.g. `macos_free_minimal`) OR an explicit `plugins` list — exactly one
- A committed git ref to assemble from (default `HEAD`)
- A target directory for the newborn (absent or empty; it becomes the seed folder and then the live clone)
- A solet `name` (lowercase `[a-z][a-z0-9_-]{1,62}`) and a `profile_template` for genesis
- WIZARD STEP 1 already done for the newborn (its own Postgres role + database + pgvector + localhost scram gate) — a pre-launch operator/agent step genesis assumes, not this card's scope

## Output Contract

- A clean seed bundle assembled at the target folder (source-only, no `.git`)
- A live local solet born from it: `.venv` built, genesis 6-step spine complete, credential self-seeded in the newborn's own Keychain namespace, LaunchAgent autostart installed
- Nothing published anywhere; no GitHub repo, no network egress

## Sequence

[ ] 1. Assemble a clean seed bundle into the target folder
    a) Assemble a clean seed bundle from the committed ref into the target folder (plugin::seed_factory_plugin::assemble_seed)

[ ] 2. Birth the seed folder into a live solet, provisioning the venv explicitly
    a) Birth the assembled seed folder in existing-clone mode with provision_venv=True (plugin::github_midwife_plugin::birth_solet)

## Expected Step Count

2 steps.

## Binding Guidance

- Bind step 1 `bundle_name` XOR `plugins` (exactly one), `ref` (default `HEAD`), and `output_dir` to the target folder that step 2 will birth.
- Bind step 2 `environment_config.target` to step 1's returned `bundle_path` (the same folder), `name` to the newborn's chosen solet name, `profile_template` to the boot profile, and `provision_venv` to **True** — this is the §7 birth variant that builds the source-only seed folder's `.venv` explicitly and unconditionally before genesis. Omitting it (default False) would fail loud downstream because an assembled seed folder ships without a `.venv`.

## Coherence Obligations

- `provision_venv=True` is REQUIRED here, not optional: `assemble_seed` produces a source-only tree (no `.venv` by design — a seed ships source, not an environment), and standard existing-clone birth skips venv creation by contract. The variant places the venv build explicitly; do not rely on birth to create it implicitly (it will not — there is no lazy create-if-absent path).
- This chain is entirely local and credential-free at the platform layer: no GitHub, no `gh`, no push. The only credential in play is the newborn's OWN Postgres password, which the newborn self-seeds in its own venv subprocess (no cross-solet copy). If the intent is to SHARE the seed rather than run it, use `mint_seed` (which publishes) instead.
- Genesis assumes WIZARD STEP 1 was completed for the newborn (its role/database/pgvector/scram gate). Birth runs a pre-seed negative-auth probe and fails loud if that gate is missing; complete the wizard step, then re-run — every underlying slice is idempotent (probe-first, skip-if-healthy).
- Never point the target at an occupied non-clone directory: `assemble_seed` refuses a non-empty `output_dir` (never clobbers), and birth refuses a non-clone target.

## Next Joseki

Explicitly absent — post-birth environment setup (the operator's launch tooling, MCP bridge registration) is the seed hydration runbook's territory, performed in conversation under the user's tool-approval flow, not a deterministic card.

## Repair Joseki

Explicitly absent as a card. A failed assemble is fail-closed (read the error and re-run). A failed birth returns a `BirthResult` whose `message` names the failing stage (venv/seed install, the pre-seed scram probe, the 6-step spine, or autostart); the underlying slices are independently idempotent, so fix the named cause (usually an incomplete WIZARD STEP 1) and re-run against the same folder.
