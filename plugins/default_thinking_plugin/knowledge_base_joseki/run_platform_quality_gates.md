# Run Platform Quality Gates

Article Layer: 2

Article Role: joseki_catalog

Article Tags: planning-stage:post-approval, planning-stage:wbs-execution, evidence-category:joseki, domain:platform-operations, domain:quality-gates


JOSEKI_KEY: run_platform_quality_gates
DESCRIPTION: Run the platform's core quality gates back-to-back as one deterministic pass: the whole-tree code-quality aggregate (ruff, pyright, the coherence trio, whole-tree integration, service-interface AST), the SQL-access lockdown gate, and the gate-eligible smoke suite. Each step records its pass/fail verdict and bounded output on the run flow so a reviewer reads one run instead of invoking three verbs by hand. Use for an at-a-glance platform quality snapshot — a pre-handoff self-check or a scheduled quality sweep — NOT as a substitute for Git-Controller's canonical pre-commit gate run. Fully closed-world: no bindings required.
EMBEDDING_DESCRIPTION: Run the platform code-quality gates in one deterministic sweep: execute the whole-tree code_quality aggregate (ruff plus pyright plus the god-class, radon cc and radon mi coherence checks plus whole-tree integration and the service-interface AST check), then the sql_access lockdown gate, then the gate-eligible smoke suite, recording each gate's pass or fail verdict for review. Routine quality snapshot or scheduled quality sweep over the whole platform source tree with zero inputs.

## Input Contract

- A running local platform serving the quality_service gate surface
- No caller inputs; the card is closed-world with zero binding slots

## Output Contract

- code_quality aggregate verdict recorded on the run flow (pass/fail plus bounded gate output)
- sql_access lockdown verdict recorded on the run flow
- Gate-eligible smoke-suite verdict recorded on the run flow

## Sequence

[ ] 1. Run the whole-tree code-quality aggregate gate
    RESULT_PROCESSOR_KIND: deterministic_continuation
    a) Run the code_quality aggregate gate (service_interface::quality_service::run_gate)
        Arguments:
        {"gate": "code_quality"}

[ ] 2. Run the SQL-access lockdown gate
    RESULT_PROCESSOR_KIND: deterministic_continuation
    a) Run the sql_access gate (service_interface::quality_service::run_gate)
        Arguments:
        {"gate": "sql_access"}

[ ] 3. Run the gate-eligible smoke suite
    RESULT_PROCESSOR_KIND: deterministic_continuation
    a) Run the gate-eligible smoke suite (service_interface::quality_service::run_test)
        Arguments:
        {}

## Expected Step Count

3 steps.

## Binding Guidance

- No binding slots. The card runs as-is; all three steps take fixed arguments by design.
- Step 3 runs the full gate-eligible smoke suite when run_test is called with no `smoke` filter; pass a single smoke name only when narrowing to one target, which is a different (targeted) use than this whole-suite sweep.

## Coherence Obligations

- A green verdict requires ALL THREE: code_quality pass AND sql_access pass AND the smoke suite pass. The steps record each verdict independently; the consumer of the run evidence applies the combined verdict. A single gate failing does not abort the remaining steps — every gate's evidence is captured so one run surfaces the full picture.
- This card is an agent/driver convenience snapshot, not the commit gate: a green run here does NOT authorize a commit. Git-Controller's canonical pre-commit procedure remains the sole gate authority for landing code; it runs the same scripts plus staging-scoped checks this driver surface does not reproduce.
- code_quality is the whole-tree aggregate at canonical scope: on a dirty working tree it reports other lanes' in-flight WIP as failures. Read a fail verdict against the current tree state — a fail here is a snapshot of the whole tree, not necessarily of the caller's own change.

## Next Joseki

Explicitly absent — a failing gate today routes to the owning agent for cleanup; a repair card should be authored when a mechanical remediation procedure stabilizes.

## Repair Joseki

Explicitly absent as a card. On a red gate the remediation is to read the recorded gate output, fix the flagged files, and re-run this card; there is no automated repair verb.
