# Upstream Feedback Runbook — Reporting Defects and Requesting Features From Your Seed's Maintainers

Tags: knowledge:tag:solet_lifecycle, knowledge:tag:upstream_feedback

Article Layer: 2

Article Role: operations_runbook

Article Tags: planning-stage:solet-lifecycle, evidence-category:operations-runbook, domain:local-solet, domain:client-deployment, consumer_profile:both

Embedding Description: How a deployed solet's driving agent reports platform defects, asks questions, requests features, proposes a design, and confirms fixes upstream to the maintainers of the seed it was born from — filing every item as a GitHub issue through the seed repository's issue forms, why the seed accepts bug reports but not pull requests or patches, what to do when you are carrying a local fix or divergence and want to know whether upstream wants it, sending an RFC-shaped design proposal as a feature-request issue carrying the design in its body, grouping a round under a parent issue with sub-issues, the numbered item vocabulary that keeps every item individually answerable, the evidence and content rules for what may and may not appear in an outbound report, and how answers come back as issue closures and GitHub releases.

**When you need this**: you hit a platform defect, a confusing behavior, or a missing capability and want the maintainers to know; the operator asks "can we get this fixed upstream?"; you are carrying a local fix or divergence and need to know whether upstream wants it; you are wondering whether to open a pull request or send a patch; you sent feedback in a previous round and need to check what happened to it; you want to propose a design rather than report a problem; deciding what is safe to include in an outbound report.

---

## Why this channel exists

The seed you were born from is a living product. Its maintainers re-mint it as the platform improves, and — this is the part worth internalizing — **field reports from deployed solets are a primary input to what gets fixed and built next**. Several of the platform's real shipped fixes originated as adopter feedback, including defects that were invisible from the origin because they only occur on a born clone. A deployed solet that hits a wall and works around it silently helps one deployment; the same observation, reported, helps every deployment.

Feedback is the driving agent's job, done on the operator's behalf and with the operator's knowledge. The operator should never need to write a bug report themselves; they should only need to say yes to sending one.

## The channel — the seed repository, one door

Your clone came from a GitHub repository, and that repository is the channel. `git remote get-url origin` names it; never hardcode a repository URL, because the seed's home can move and a hardcoded target sends your report nowhere.

**Every outbound item is a GitHub issue.** There is one door, and the form you pick is what distinguishes the classes:

| What you have | Where it goes |
|---|---|
| A defect, a question, a feature request, or a closure confirmation | A **GitHub issue**, filed through the repository's issue form for that class |
| An RFC-shaped design proposal — a worked-through design with alternatives considered and a recommendation | A **feature-request issue carrying the design in its body** |
| A security vulnerability | **Not an issue** — the private channel in `.github/SECURITY.md` |

### Why there is no pull-request door

The seed accepts bug reports and does not accept code. That is a deliberate posture, and the reason is what this software does: a solet provisions a database, writes credentials to the system keychain, installs a background service, and modifies the shell environment on every machine that installs it. Merging code the maintainers cannot review to that standard would make the seed repository a supply-chain path into every downstream install. So pull requests, patches and design proposals are all declined — see `CONTRIBUTING.md` in your own clone, which is the authoritative statement.

This is a redirect, not a brush-off. **The description is the part the maintainers can act on**, and it is genuinely wanted: a defect report with the command, the output and the release is worth more to them than a patch, because they can implement the fix in a form they are able to stand behind and ship to every deployment.

It also does not constrain you. This code is Apache-2.0: you already have the right to modify and redistribute it, and a solet is meant to be grown rather than collaboratively edited. Carry whatever divergence you need locally — and tell upstream about it, per the local-divergence rule below, so the loop closes.

### Filing an item as an issue

1. **File non-interactively with `gh issue create --body-file`.** The issue forms are a browser UI; mirror the matching form's required visible field labels in the draft, add the checked content-gate acknowledgement, and apply that form's label. The API does not enforce the fields, so verify the structured draft before sending rather than relying on a chooser.
2. **One issue per item.** Resist the urge to fold three defects into one issue; each needs its own disposition, and a merged issue can only be closed once.
3. **A multi-item round gets a parent issue.** File a parent issue for the round, then attach each item's issue to it as a sub-issue. The parent carries the round's context and gives both sides one place to see what is still open; the children carry the individually answerable items.
4. **A single-item round needs no parent.** File the one issue.

Outbound sends are external publication: get the operator's explicit yes before filing, and let them see what is being sent. This is one conversation, not a standing burden — an operator can grant standing approval for feedback that passes the content gate below.

### Filing a design offer

A worked-through design still travels, it just travels as an issue. Use the **feature request** form and put the design in the body: the outcome you need, the alternatives you considered, the tradeoffs, and your recommendation. Long is fine — a design that needs to be read in order reads perfectly well in an issue body, and the issue thread is where the discussion lands.

Draft it under `workbench/` in your clone first if that helps you think (create the directory if it does not exist — it is deliberately not shipped), then paste it into the form. Do not open a pull request carrying it: the maintainers cannot merge it, so it would sit unanswered while the same content in an issue gets a disposition.

A design offer is **accepted as information, not as a roadmap commitment**. The maintainers own the method, and a design that reframes the problem well is valuable even when the implementation they choose looks nothing like the one you proposed.

## The item convention — make every item individually answerable

These conventions come from a live adopter relationship that has run for dozens of rounds; they are what made that relationship work. Moving to issues changed where items live, not how they are identified.

- **Number the rounds.** Keep one monotonically increasing part number per deployment (`Part 1`, `Part 2`, …), never reused. Number items within a round as `§N.M` (part number, item number).
- **Carry the number in the issue title**, ahead of the summary: `§42.3 — peer role binding survives a restart but stops routing`. The number is what lets a later round, a release note, or a maintainer's reply cite the item precisely and unambiguously. GitHub gives you an issue number too; both are useful and they are not interchangeable — the issue number addresses the issue, the `§N.M` addresses the item across rounds, documents, and releases where issue numbers are not in front of the reader.
- **Classify every item** as one of: **defect** (something behaves wrongly, with evidence), **question** (something is unclear and you need an answer to proceed), **feature request** (a capability you want, with the workflow that needs it), or **closure confirmation** (a previous item you verified fixed — these are genuinely valuable, send them). The class picks the form.
- **Evidence discipline.** For defects: the exact command or action, the observed output, the expected behavior, and the release you measured against (your seed repository's HEAD commit and, when present, the version named in `RELEASE_NOTES.md`). Say what you measured and how; mark any inference as inference. A report that says "X is broken" without the command that shows it costs a round-trip; a report with the command is often fixable the day it arrives.
- **Batch rounds, don't drip items.** Collect items and file the round together rather than filing one issue whenever something irritates you. One issue per item is the filing rule; batching is about cadence, not about merging items. Small rounds are fine; a trickle of unrelated issues across a week is not.
- **Carrying a local fix or divergence? Say so explicitly, and ask for an explicit yes or no.** This case is wanted, and it is information rather than a patch — file it as a defect and describe the defect first, your local change second, in prose. Do not attach or offer the diff: the maintainers cannot take it, and their tree has usually moved anyway, so a change against your older base can be behind their current fix without either side noticing. What helps them is knowing the behaviour was wrong, what you did about it, and that you are still carrying it. If the divergence is deliberate and permanent, say so and ask directly whether upstream wants the underlying change or declines it. "We will stop raising it if this is a deliberate no" is a complete and welcome sentence; it lets both sides close the loop.

## Feature requests — outcome first, workflow evidence second

State the outcome you need, not the implementation you imagine. The strongest feature request names the workflow that hit the gap ("every time we do X we must hand-do Y"), what it costs you, and what you are doing as a workaround. An implementation sketch is welcome as an appendix, never as the headline — the maintainers own the method, and a stated goal gives them room to solve it better than the sketch would. A sketch that has outgrown an appendix is the signal that you have a design offer; that is still a feature-request issue, just one whose body is the design (see "Filing a design offer" above).

## The content gate — run it before every send, no exceptions

The feedback surface is outside your deployment. An issue on a repository is published the moment you submit it, and editing it afterwards does not unpublish what was there. Before filing, verify the issue body — or the document — contains:

- **No personal identifiers** — no names of people, no email addresses, no GitHub handles beyond your own deployment's.
- **No employer or business specifics** — no company names, no tenant URLs, no internal project names, no business-record data from any connector. Describe findings generically: "a business-connector read of ~40k rows", never the rows or whose they are.
- **No credentials or secret-looking values** — nothing from Keychain, config, or environment. If evidence output contains one, redact it and say so.
- **No transcript excerpts containing any of the above.** Re-read pasted command output specifically — that is where identifiers hide.

The issue forms carry this gate as a required checkbox. In the non-interactive
filing path, include its checked Markdown acknowledgement in the structured
body. It is a reminder, not the check: check it because you re-read the body,
never to get past a form or API request.

If an item cannot be reported without violating this gate, report the generic shape of it and offer detail through whatever private channel the operator and the maintainers separately share, if any.

## What to expect back

- **Issue closure is the per-item disposition.** An item is answered when its issue closes, and the closing comment or linked commit says which it was: fixed, declined-with-reason, already-fixed-since-your-release, or answered. A maintainer landing a fix links it with `Fixes #N`, so the commit that closed your item is reachable from the issue itself — that is the shortest path from "it says fixed" to "here is the code that fixed it".
- **Releases are the primary response surface.** Each re-mint ships a `RELEASE_NOTES.md` and a corresponding GitHub release, and the release notes cite the issue numbers closed in that release. **Subscribe to the repository's releases** (watch the repository, releases only) — that subscription, not polling, is how you learn a round was answered. After updating from a re-mint (see the seed update runbook), check the release notes against your open items.
- **Silence is not a disposition.** If a round has open items with no response by the next re-mint you adopt, re-raise them by number in your next round — briefly, as a list, not re-argued. Reference the still-open issues rather than filing duplicates. The convention of asking "is this a deliberate no?" exists precisely so that deprioritized and overlooked stay distinguishable.

## Reference

- `plugins/github_midwife_plugin/knowledge_base/05_seed_update_runbook.md` — adopting a re-mint, which is where answered feedback comes back to you.
- `plugins/github_midwife_plugin/knowledge_base/08_deployment_report_card.md` — the deployed-state report card; gaps it cannot close locally are feedback candidates.
- `ananta/knowledge_bases/ananta_platform/24_operator_communication/01_operator_decision_briefs.md` — the decision-brief shape, which a good feedback item resembles: decidable from the document alone.
- `CONTRIBUTING.md` at your clone's root — the authoritative contributor policy this runbook operationalizes: bug reports wanted, code contributions declined, and why.
- `.github/SECURITY.md` at your clone's root — the private vulnerability channel. A security finding never goes in a public issue.
