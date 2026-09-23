# Dead-Symbols Judgment — Adjudicating the Candidate Class (L2 targeting guidance)

Article Layer: 1
Article Role: platform_constraint
Article Tags: planning-stage:always, evidence-category:constraint, domain:code-vetting, consumer_profile:both

Embedding Description: The judgment standard for the candidate-dead-symbols evidence class — the unused-function, class, method, variable, attribute, and property family that vulture assigns 60% confidence and that the suite records as L2-targeting evidence rather than findings. Covers why the whole 60% family is evidence and never a deterministic finding (a library's exported public API is legitimately unused in-repo, and a 60%-confidence finding row on a foreign tree is the hit-job the calibrate-not-verdict posture forbids), the false-positive classes an AI lens must clear before flagging a candidate (dynamic dispatch and getattr-style invocation, registry and decorator-dispatched handlers, framework entry points and callbacks, re-exported public API and package init surface, test-only helpers, and serialization or dataclass fields read reflectively), and how to turn a genuine candidate into a specific finding naming the symbol, the reason nothing reaches it, and the concrete removal or wiring fix.

**When you need this**: an AI lens is handed the candidate-dead-symbols table and must decide which entries are truly dead versus reachable through indirection; judging whether an unused public function is dead code or an exported API; calibrating a dead-code observation on a foreign repo whose entry points and dynamic dispatch cannot be fully seen.

This guidance is NOT a new lens. It is universal-tier rulebook source, applied by the existing correctness and test_adequacy lenses. The deterministic dead-code scanner emits only the provable classes as findings — the live report renders `Verdict: confirmed (L1_deterministic)` on unreachable code and unused imports outside re-export surfaces — while the entire 60% family of function, class, method, variable, attribute, and property carries the explicit framing "evidence for the AI-critic layer, NOT findings" in its Candidate Dead Symbols section. That family is evidence because it sits below any clean confidence threshold and false-positives on exactly the code a foreign repo owes the suite no configuration to exonerate.

## Why the 60% family is evidence, not a finding

Static analysis calls a symbol unused when it sees no in-tree reference. But a library's public API is unused BY the library and used by its consumers; a registry handler is dispatched by string, not by a visible call; a framework callback is invoked by the framework. Emitting a 60%-confidence dead-code finding on a foreign tree, where the entry points and dispatch tables are least visible, is precisely the presumptuous verdict the calibrate-not-verdict posture forbids. So the family is handed to an AI lens to judge with the whole codebase in view, rather than asserted as a fact.

## Clear these false-positive classes before flagging

A candidate is only a finding after the lens rules out every path that reaches it invisibly. Dynamic dispatch and getattr or reflection call it by a computed name. Registry and decorator-dispatched patterns wire it without a visible call — the platform's own registry-dispatched methods are the canonical case, structurally unused to static analysis and alive at runtime, which is why the self-vet ignores those decorators. Framework entry points, route handlers, CLI commands, and callbacks are invoked externally. Re-exported public API and a package's init surface are used by importers, not in-repo. Test-only helpers, fixtures, and dataclass or serialization fields are read reflectively or by a framework. Any of these makes the symbol reachable, so it is not dead.

## Turn a genuine candidate into a specific finding

When a candidate survives that screen, the finding names three things: the symbol and its location; WHY nothing reaches it, meaning no caller, no registry entry, no export, and no dynamic path; and the concrete fix, which is delete it, wire the missing call, or export it if it is meant to be API. A dead-symbol finding without the reason-nothing-reaches-it is not verified — it is a tool row an AI lens failed to adjudicate. On a foreign repo, calibrate: name the specific symbol and the specific missing reference, at a severity ceiling that reflects the residual uncertainty about entry points that cannot be seen.
