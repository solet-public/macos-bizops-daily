> **LIFECYCLE: ARCHIVED** — this joseki is retired; do not use it for new work.

# Promote Workbench Artifact to Knowledge Base

Article Layer: 2

Article Role: joseki_catalog

Article Tags: planning-stage:post-approval, planning-stage:wbs-execution, evidence-category:joseki, domain:knowledge-management, domain:platform-operations


JOSEKI_KEY: promote_workbench_artifact_to_kb
DESCRIPTION: Promote a settled workbench artifact into a durable knowledge base as a first-class document: read the source artifact, place its content at a target path in the destination knowledge base (chunked and indexed on write), confirm the new document is retrievable by search, then archive the source workbench artifact as superseded so the promoted copy becomes the single live version. Use when a workbench doc has stabilized into reference material that belongs in a searchable KB. A deterministic move-and-index promotion — the caller supplies the source and destination; the card does not rewrite the content, so the source must already be KB-shaped. Bind the five slots to run.
EMBEDDING_DESCRIPTION: Move a finished workbench document into a knowledge base as a durable indexed article: read the workbench source file, create it at a chosen path in the destination knowledge base so it is chunked and searchable, verify by search that the promoted document is retrievable, then archive the original workbench file as superseded by the new copy. Routine promotion of stabilized workbench reference material into a searchable knowledge base with caller-supplied source and destination.

## Known Blockers — NOT PROVABLE AS AUTHORED (do not use for new work)

This card is NOT mechanizable as authored; `run_joseki` fail-fast-rejects it at WBS validation (nothing is stored — safe). It will not earn a clean proving run until repaired as a fresh card. Two blockers, found 2026-07-17 (Claude-C empirical proving run + Codex review):

1. **Content-carry is not deterministic.** Step 2 create_file needs the source's content, but a `deterministic_continuation` step cannot pipe a prior step's runtime RESULT into a later step's argument (`Composed:` references resolve prior steps' DECLARED arguments only; work-product injection covers file/blob output slots, not an inline `content` return). Repair: add a `<<BIND:content>>` slot (caller supplies the finalized content; step 1 read becomes a pre-archive readability gate), or make the carry an inference step.
2. **Verify-before-archive is unenforceable (DATA-LOSS risk).** Step 4 archives the source, but step 3's `search` returns hits, not a pass/fail retrieval assertion — so the "archive only after confirmed retrievable" coherence rule is aspirational prose, not an enforced gate. Archiving before the promoted copy is proven present risks losing the document from both locations. Repair: scope the search `name=target_kb` AND add a real retrieval-ASSERTION verb (returns pass/fail) before archive — that primitive does not exist today and must be built.

Additional gap: `superseded_by` is a bare `target_path`, which loses `target_kb` for a cross-KB promotion. The clean, fully-automatic version (read → model carries/reshapes content → create → assert-retrievable → archive) is the INFERENCE variant, tracked as a future card `smart_promote_workbench_artifact_to_kb`. Author the repaired card fresh when the retrieval-assertion primitive lands rather than forcing this one deterministic.

## Input Contract

- A source workbench artifact that has stabilized and is already KB-shaped (carries a valid document/metadata block; this card does NOT reshape content)
- A destination knowledge base and target path where the promoted document should live
- A verification query expected to retrieve the promoted document once indexed
- Bindings: source_kb, source_path, target_kb, target_path, verification_query

## Output Contract

- The promoted document created and indexed at target_kb / target_path, its content byte-identical to the source
- Search evidence for the verification query recorded on the run flow (the promoted document among the hits)
- The source workbench artifact archived under the source KB's archive subdirectory, its metadata block stamped Superseded_by the promoted path

## Sequence

[ ] 1. Read the source workbench artifact
    RESULT_PROCESSOR_KIND: deterministic_continuation
    a) Read the source artifact (service_interface::knowledge_service::read_file)
        Arguments:
        {"name": "<<BIND:source_kb>>", "path": "<<BIND:source_path>>"}

[ ] 2. Create the promoted document in the destination knowledge base
    RESULT_PROCESSOR_KIND: deterministic_continuation
    a) Create the promoted document (service_interface::knowledge_service::create_file)
        Arguments:
        {"name": "<<BIND:target_kb>>", "path": "<<BIND:target_path>>"}

[ ] 3. Verify the promoted document is retrievable
    RESULT_PROCESSOR_KIND: deterministic_continuation
    a) Search the destination for the promoted document (service_interface::knowledge_service::search)
        Arguments:
        {"query": "<<BIND:verification_query>>", "top_k": 8}

[ ] 4. Archive the source workbench artifact as superseded
    RESULT_PROCESSOR_KIND: deterministic_continuation
    a) Archive the source artifact (service_interface::knowledge_service::archive_file)
        Arguments:
        {"name": "<<BIND:source_kb>>", "path": "<<BIND:source_path>>", "superseded_by": "<<BIND:target_path>>"}

## Expected Step Count

4 steps.

## Binding Guidance

- Bind step 2 `content` to step 1's returned file content, verbatim and unmodified — this card is a byte-faithful promotion, not a rewrite. If the destination KB requires content reshaping (a different metadata block, a re-authored embedding description), that is a different (authoring) procedure with an inference step, not this card.
- Bind `source_kb` / `source_path` to the live workbench artifact being promoted; bind `target_kb` / `target_path` to the destination KB and the path the promoted document should occupy. `target_path` must not already exist — create_file places a NEW document and does not overwrite.
- Bind `verification_query` to a phrase distinctive to the promoted document (its title or a defining sentence) so step 3's search is a real retrievability check, not a broad match.
- Bind step 4 `superseded_by` to the same `target_path` used in step 2 so the archived source points at its promoted successor.

## Coherence Obligations

- Archive the source ONLY after the promoted copy is confirmed created and retrievable — steps 2 and 3 must succeed before step 4 runs, or the artifact is lost from both locations. The step order is the safety invariant; do not reorder archive ahead of verify.
- A byte-faithful promotion assumes the source is already KB-shaped. A workbench doc lacking a valid document/metadata block will be rejected by create_file at step 2; that rejection means the doc needs authoring first (out of this card's scope), not a retry.
- Retrievability is eventual: the index write at step 2 is synchronous for this KB, but if step 3 returns no hit, re-run step 3 before concluding failure rather than archiving the source.
- source_kb and target_kb may be the same knowledge base (promotion to a new path within one KB) or different; the card is agnostic, but a same-KB promotion still archives the source so exactly one live copy remains.

## Next Joseki

Explicitly absent — a promoted document that needs follow-on cross-linking or re-tagging routes to a KB-authoring pass, which is an inference procedure rather than a deterministic card.

## Repair Joseki

Explicitly absent as a card. If step 2 or 3 fails after a partial promotion, the manual remediation is: archive the half-created target document with archive_file (or leave the source un-archived if step 4 never ran), then diagnose the create/index failure before re-running. There is no automated rollback verb.
