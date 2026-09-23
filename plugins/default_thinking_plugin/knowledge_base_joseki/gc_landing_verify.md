# GC Landing Verify

Article Layer: 2

Article Role: joseki_catalog

Article Tags: planning-stage:post-approval, planning-stage:wbs-execution, evidence-category:landing-verification, domain:platform-operations, domain:quality-gates


JOSEKI_KEY: gc_landing_verify
DESCRIPTION: Give Git-Controller independently callable, read-only evidence before a scoped landing: rehash the declared file manifest at the actual worktree/candidate-tree root, predict whether gate_smokes.txt or another append-only tracked-debt register merges cleanly with current master, and identify changed paths excluded from the normal per-file static-gate surface. This card reports findings only. It never stages, commits, creates branches, merges, or authorizes a landing.
EMBEDDING_DESCRIPTION: Verify a Git-Controller landing without changing Git. Hash the declared files in the named worktree or candidate tree and disclose editable-install worktree shadowing; predict a gate_smokes or tracked-debt register merge against master; then identify code paths outside code_quality_check static analysis so manual ruff, pyright, or radon checks can be added before landing.

## Input Contract

- Bindings: unit_id, root_path, manifest, base_ref, lane_root_path, current_master, register_path, paths
- `root_path` and `lane_root_path` are existing absolute directories; `manifest`, `register_path`, and `paths` use the repo-relative spellings Git tracks
- The caller has already chosen the candidate/worktree and revisions to inspect; this card neither materializes nor mutates them

## Output Contract

- Per-file SHA-256 exact/mismatch/missing evidence plus an editable-install shadowing warning where the inspected .venv resolves source outside the declared root
- A no-write gate-register merge candidate, duplicate/missing registration findings, and a clean/conflict verdict
- The exact list of paths outside `code_quality_check.py`'s per-file static-gate predicate, so Git-Controller can run a manual supplement before relying on whole-tree evidence

## Sequence

[ ] 1. Verify the declared landing hashes at the named root
    RESULT_PROCESSOR_KIND: deterministic_continuation
    a) Rehash every declared path and inspect editable-install pointers (service_interface::quality_service::verify_hash_manifest)
        Arguments:
        {"unit_id": "<<BIND:unit_id>>", "root_path": "<<BIND:root_path>>", "manifest": <<BIND:manifest>>}

[ ] 2. Predict the append-only tracked-debt register merge
    RESULT_PROCESSOR_KIND: deterministic_continuation
    a) Read base, lane, and current-master register content and emit the no-write merge prediction (service_interface::quality_service::predict_gate_smokes_merge)
        Arguments:
        {"base_ref": "<<BIND:base_ref>>", "lane_root_path": "<<BIND:lane_root_path>>", "current_master": "<<BIND:current_master>>", "register_path": "<<BIND:register_path>>"}

[ ] 3. Detect changed paths that evade the normal per-file static gates
    RESULT_PROCESSOR_KIND: deterministic_continuation
    a) Classify the tracked changed paths against code_quality_check's exact predicate (service_interface::quality_service::detect_scope_regex_gaps)
        Arguments:
        {"paths": <<BIND:paths>>}

## Expected Step Count

3 steps.

## Binding Guidance

- Bind `manifest` exactly to the declared `{path: sha256}` surface. A passing hash result is proof of those bytes only, not approval to stage or commit them.
- Bind `root_path` to the actual candidate/worktree whose source a gate will read. Treat `editable_install_shadowing_detected=true` as a HOLD until the gate interpreter resolves code under that same root; an ambient editable venv can make a scratch gate measure another checkout.
- Bind `base_ref` and `current_master` to explicit immutable revisions where possible. The register prediction reads them but never invokes `git merge` or another mutating Git command.
- Bind `register_path` to `quality_gates/gate_smokes.txt` normally, or another concrete append-only tracked-debt register when that is the shared surface under review. `duplicate_registrations_on_master` means master already has a lane entry; `missing_registrations_on_master` names entries the lane would add.
- Bind `paths` from the exact tracked landing scope, not a remembered list. When `manual_static_analysis_needed=true`, Git-Controller must capture an explicit ruff/pyright/radon supplement for every `out_of_scope` Python path before treating whole-tree Step 7 as adequate.

## Coherence Obligations

- All three process keys are advisory/reporting surfaces. A green result cannot authorize staging, committing, merging, or a landing; those actions remain exclusively with Git-Controller.
- The merge predictor's candidate content and verdict are a no-write three-way text-merge observation. A conflict is evidence to resolve, never permission to overwrite either branch's register.
- `all_exact=true` does not clear an editable-install warning. A file can hash correctly in the candidate while Python imports the same package from another checkout; the warning exists specifically to prevent that false-positive gate evidence.
- Scope gaps are about per-file static analysis, not behavioral smoke registration. A path outside the predicate needs a disclosed manual supplement even if its smoke is gate-registered.

## Next Joseki

Use `request_scoped_landing` only after the report is clean or its findings are explicitly resolved and the normal pre-handoff gates have run. This card does not replace that landing procedure.

## Repair Joseki

Explicitly absent. Hash mismatch, shadowed editable install, register conflict, and static-scope gap have different repairs; fix the evidence subject, rerun this reporting card, and keep Git mutation with Git-Controller.
