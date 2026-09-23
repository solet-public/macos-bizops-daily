# Magic-Strings Judgment — Which Repeated Literals Are Real (L2 targeting guidance)

Article Layer: 1
Article Role: platform_constraint
Article Tags: planning-stage:always, evidence-category:constraint, domain:code-vetting, consumer_profile:both

Embedding Description: The judgment standard for repeated string literals in a codebase, consumed two ways — assembled into the vetting rulebook's universal tier and used by the existing AI critic lenses such as ai_slop and correctness as targeting guidance over the literal-frequency evidence table. Covers which repeated literals are genuine config, enum, or constant candidates worth flagging (a status string, a process key, a magic number threading through branches, a table or column name) versus which are legitimately repeated and must NOT be flagged (internationalization prose, test-fixture values, file paths and URLs that are inherently string-shaped, format specifiers, log-message wording), how to weigh a literal's repetition count against its stability and its blast radius if it drifts, and the framing rule that this rides the universal rulebook tier because the judgment is language-agnostic and not platform-specific.

**When you need this**: an AI lens is handed the literal-frequency table and must decide which repeated literals are real magic-string findings; deciding whether a repeated literal is a constant or enum candidate versus legitimate repetition; calibrating a magic-string observation on a foreign repo without turning inherent string repetition into a finding.

This guidance is NOT a new lens. It is universal-tier rulebook source: the standard for judging repeated literals is not platform-specific, so the rulebook assembler folds it into the universal tier and the existing ai_slop and correctness lenses apply it. The evidence is the repeated-literals table, which the live report renders inside the Structural Quality Metrics section: each repeated literal, its occurrence count, and its locations. That table structurally emits no findings; deciding which entries matter is this article's job.

## Flag these — real config, enum, or constant candidates

A repeated literal is a candidate finding when extracting it to a named constant, enum, or config key would remove a real drift or correctness hazard. The clearest cases are a status or state string compared in more than one place, where a typo in one copy is a silent bug; a process key, route, or dispatch string threaded through branches; a magic number that is a threshold or limit repeated across call sites; a database table or column name repeated in queries; and a shape or key name that a producer and a consumer must agree on. The tell is that a single change to the value must reach every copy, so a missed copy is a defect.

## Do NOT flag these — legitimate repetition

Repetition alone is not a finding. Internationalization prose and user-facing message text are legitimately repeated and belong as strings, not constants. Test-fixture values are deliberately literal and local. File paths, URLs, and glob patterns are inherently string-shaped, and their repetition is not a magic-string smell. Format specifiers and log-message wording read best inline. A literal that appears many times but never needs to change together, because each occurrence is independent, is not a magic string.

## Weigh count against stability and blast radius

The finding strength is repetition count times drift-hazard times blast radius, not raw count. A value that appears three times but MUST stay in lockstep, such as a shared envelope key, outranks a value that appears twenty times independently, such as a common word in log lines. On a foreign repo, calibrate: name the specific literal and the concrete drift it risks, never emit "many repeated strings" as a finding, and never turn a codebase's inherent string density into a verdict.
