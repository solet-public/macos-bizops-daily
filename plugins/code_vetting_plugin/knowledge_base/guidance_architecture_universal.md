# Universal Architecture Judgment — Coupling, Layering, and Boundary Integrity (L2 lens guidance)

Article Layer: 1
Article Role: platform_constraint
Article Tags: planning-stage:always, evidence-category:constraint, domain:code-vetting, consumer_profile:both

Embedding Description: The judgment standard for architecture_universal, the L2 critic lens restored to a foreign target's roster: closed-world, technology-agnostic structural review of a codebase's own dependency graph, covering four concerns: coupling between modules that should stay independent, layering violations where a dependency runs the wrong direction (a shared or core module importing from a specific feature or entry-point module instead of the reverse), god-components where a single unit accretes multiple unrelated responsibilities judged by responsibility diversity rather than size, and boundary or abstraction integrity where code reaches past a declared boundary such as a service layer, a repository, or a public API to touch the other side's internals directly. Judged entirely from the target's own import and call graph, never from a specific framework or platform's idiomatic conventions, which is open-world knowledge this lens does not carry.

**When you need this**: an AI lens is reviewing a foreign codebase's structural design and has no platform-specific rulebook to judge it against; deciding whether a module dependency, a large class, or a cross-boundary call is a genuine architecture finding versus scale-appropriate simplicity; judging coupling, layering, or circular-dependency evidence on a repo whose conventions and stack the suite does not own.

This is the target guidance article for the architecture_universal L2 critic lens. The rest of this article works through each of the four concerns below in enough detail to judge a real candidate: what to flag, what to leave alone, and how to calibrate on a target this suite does not own. See "Relationship to architecture_conformance" below for how this lens's remit was checked against the platform-tuned lens it complements, rather than assumed to overlap with it.

## What this lens owns

Four structural concerns, each judged from the code and its own internal dependency graph alone, with no external rulebook or stack knowledge required.

**Coupling.** Two modules that should be independent instead share deep, incidental knowledge of each other's internals, such that a change in one routinely forces a change in the other beyond a stable, narrow interface.

**Layering.** A dependency runs the wrong direction: a lower layer (data access, a shared utility, a core domain type) imports from a higher layer (a specific feature, a UI component, an orchestration entry point) that is supposed to depend on it, not the reverse. A layering violation is visible in the import graph itself, not in any single file's content.

**God-components.** A single module, class, or function has accreted multiple unrelated responsibilities that a reasonable design would split, judged by the diversity of reasons the unit would need to change, not by raw size.

**Boundary and abstraction integrity.** Code on one side of a declared or implied boundary, such as a service layer, a repository, or a public API, reaches past it to touch the other side's internal details directly, instead of going through the boundary's own interface.

## Flag these — concrete structural evidence

A finding here names the specific modules, the specific dependency, and the specific responsibility mix, never a general impression. Cite the import or call that crosses the line. Examples: a `core` or `domain` module importing from a `features` or `ui` module (layering, wrong direction); a class whose methods touch three or more unrelated concerns with no shared reason to change together (god-component); two modules that each reach into the other's private state or internal helpers instead of a public surface (coupling); a caller that reaches past a repository or service object to query storage directly, when the codebase otherwise defines that repository or service as the access path (boundary integrity); an import cycle between two modules that both alternately depend on each other. Rank severity by blast radius: a violation on a widely-imported core module outranks the same violation in a leaf feature nobody else depends on.

## Do NOT flag these — scale-appropriate and out-of-remit patterns

Size alone is not a finding. A large file or class is not a god-component by itself; the signal is unrelated responsibilities forced together, not line count or method count. A small or prototype-scale codebase that keeps closely related code in one file or module is not under-layered; layering earns its cost at a scale this lens must judge honestly, not assume. Generated code, vendored or third-party directories, and one-off scripts or tooling are out of scope; do not raise a structural finding there. A framework's own idiomatic pattern that looks like a violation to a context-blind reviewer, such as a routing table that imports from many feature modules by design, is not a layering violation; when a candidate depends on knowing whether a specific framework's convention sanctions the pattern, that judgment is stack-idiomatic and open-world, not this lens's remit, so do not guess at it and do not flag it. If you find yourself about to write a specific framework or platform's name into a finding to justify it, stop; that finding belongs to a later stage, not here.

## Calibrate, never verdict, on a foreign tree

This lens runs on foreign targets that the suite does not own and did not design. Observe with confidence, cite the concrete import or call that is the evidence, and never render a target-wide judgment such as "poorly architected." A finding names one boundary, one dependency, or one component and the specific fix, the same discipline any other lens in this suite owes a target it does not own.

## Relationship to architecture_conformance, and an open question about self

This lens is scoped to foreign targets only, matching the arithmetic of the restoration it fills (foreign roster 4 to 5; self roster unchanged at 6). That scoping is a decision, not a settled coverage fact, and the two lenses' remits only partially overlap: the platform-tuned lens's god-component check (its coherence rule, run against a coherence-aware gate) covers the same ground as this lens's god-component concern, but its remaining rules are platform-specific access and service-boundary conventions, not general coupling, layering-direction, or circular-dependency checks. So a self target today has god-component coverage but no equivalent coupling, layering, or cycle-detection pass under either lens. Whether self should also run architecture_universal for that gap, or whether that gap is closed some other way, is open and is being carried as a finding, not decided here. What is decided: the two lenses never share a name, a prompt, or a finding under the same provenance, so if self ever does gain this lens, each lens's own per-lens survival metric stays legible rather than blending.
