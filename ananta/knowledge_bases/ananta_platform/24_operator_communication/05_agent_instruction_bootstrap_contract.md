# Agent Instruction Bootstrap: AGENTS.md/CLAUDE.md Precedence and the Knowledge-Access Profile Contract

Tags: knowledge:tag:agent_instruction_bootstrap, knowledge:tag:operator_communication, knowledge:tag:knowledge_retrieval

Article Layer: 1

Article Role: platform_constraint

Article Tags: planning-stage:always, evidence-category:constraint, domain:operator_communication, domain:knowledge_retrieval, consumer_profile:both

Embedding Description: The binding contract for how an agent session bootstraps its operating instructions from AGENTS.md and CLAUDE.md, which file governs which agent runner and what to do when the two disagree, how the universal no-MCP local-command default relates to an explicitly selected MCP profile and a degraded source-artifact recovery, what each profile does on a failed call versus an empty result, what belongs in the root instruction files instead of the knowledge base, and the exact managed-block markers and merge rules a hydration or upgrade pass uses to update existing AGENTS.md, CLAUDE.md, or settings files without disturbing project-specific content.

**When you need this**: starting a session and deciding whether `AGENTS.md` or `CLAUDE.md` governs it; choosing between the local `<name> call` command, an explicitly configured MCP surface, and source-artifact recovery for a knowledge-base search; deciding whether a failed or empty search is a stop condition or a documented gap; reviewing or authoring a hydration/upgrade pass that touches an existing `AGENTS.md`, `CLAUDE.md`, or `~/.claude/settings.json`.

---

## Which file governs this session

`CLAUDE.md` and `AGENTS.md` can both exist at a repo's root, and both describe overlapping ground such as knowledge access and repository safety. They are companions, not replicas. Precedence follows the reader, not the file's position or age: Claude Code loads `CLAUDE.md` as its project instruction; a different agent runner following the AGENTS.md convention loads `AGENTS.md` instead and may have no visibility into `CLAUDE.md`. Neither file is "the real one" — each is the entrypoint for its own runner, in the same repo, at the same time.

This contract describes the paired repository-root files only. It does not
override system, developer, harness, or explicit operator instructions. A
runner may also layer user-scope, ancestor, or nested-directory instructions;
apply that runner's documented precedence for those layers rather than
inventing a cross-runner precedence rule here.

The shared bootstrap contract must stay equivalent in both files: the same Step Zero service and arguments, the same default access profile, the same failure ladder, and the same pointers into the knowledge base. Tool-specific launch, hook, or wake mechanics may differ because the runners differ. Copying one file byte-for-byte into the other is therefore wrong, but letting their shared contract drift is also wrong.

When the two files describe the same topic differently, that is reconciliation debt, not a choice to make on the fly: follow the file your own runner loads, and name the specific mismatch (the exact sentence in each file, not a paraphrase of the whole document) to whoever owns the two files, rather than silently picking whichever side is convenient in the moment.

## Which knowledge-access profile applies here

The rendered files always advertise the no-MCP local command as the universal default. Merely finding an MCP registration does not override that default. A project or operator may explicitly select MCP as this machine's primary profile; absent that selection, use the local command.

| Situation | Profile |
|---|---|
| The normal case in any hydrated clone; no explicit machine policy selects another route | **No-MCP local-command profile** |
| The operator or project instructions explicitly select a registered MCP server for knowledge/process access | **MCP profile** |
| The solet is unhealthy or both configured live routes fail | **Source-artifact recovery** |

The first two are live access profiles. Source-artifact recovery is degraded evidence collection, not a live profile and not a preference between equals. A worker that lacks MCP still uses the local command if the local bridge is reachable; lack of MCP alone does not force degraded recovery.

## Annotated complete examples: one command per profile

**No-MCP profile** — the same search, no MCP server involved:
```bash
<name> call service_interface::knowledge_service::search '{"query": "<plain-English description>", "top_k": 8}'
```
Talks to the solet over its local host bridge and blocks until the result is ready. Registering, debugging, or reaching for an MCP server is never the prerequisite here — the client-deployment pattern this profile belongs to is built to need none.

For prior-session recall after history ingestion:
```bash
<name> call service_interface::session_ledger_service::search_event_content '{"query": "<what happened or was decided>", "limit": 8}'
```

**MCP profile** — optional, only when the operator or project selected it:
```
mcp__<name>__process_call(
  process_key="service_interface::knowledge_service::search",
  arguments={"query": "<plain-English description>", "top_k": 8}
)
```
The completion shape is client-specific. Some clients surface the result as a notification; others expose a follow-up result snapshot. Follow the tool schema presented by the active client. Do not busy-poll and do not copy one client's notification convention into a universal root instruction.

**Source-artifact recovery** — there is no universal command because seed and development checkouts ship different surfaces:

- In a generated seed, inspect the shipped Markdown under `ananta/knowledge_bases/`, `plugins/*/knowledge_base/`, and any real directories under `knowledge_bases/`. State explicitly that this is unranked source inspection and may not reflect the live indexed corpus.
- In a full platform development checkout, first probe whether `.venv/bin/python3` and `plugins/default_knowledge_plugin/tools/query_knowledge_base.py` exist. If both do, the offline Postgres+pgvector query is:

  ```bash
  .venv/bin/python3 plugins/default_knowledge_plugin/tools/query_knowledge_base.py search "<query text>" --top-k 8
  ```

  Do not put that command into a seed template: seed assembly excludes plugin `tools/`, so a generated clone cannot rely on it.

## Failure semantics per profile

**Any live profile**: report the exact failure. Use the configured alternate live route when project policy permits it; otherwise use the source-artifact recovery above and label the evidence as degraded. Never fabricate an answer from a root instruction file or a random source fragment. A transport failure does not require abandoning unrelated local work when authoritative source artifacts are sufficient for that work, but it does prohibit claiming that degraded inspection is a successful live retrieval.

**Empty knowledge search**: distinguish a successful call with no relevant result from a failed call. Confirm the gap before inventing a convention. If a new convention is implemented, document it in the appropriate knowledge base and add a retrieval canary.

**Empty session-ledger search**: this may mean no matching event, that the requested source has not been ingested, or that session-history ingestion is not configured. Report which of those facts you actually established. Offer the hydration guidance when configuration is the missing piece; do not blame absent MCP tooling.

**Potentially stale indexed content**: compare live results with source artifacts when freshness matters. Report the suspected ingestion gap. Reingestion, restart, or deployment is a separate state-changing operation and still requires the authority normally applicable to that operation.

## Root-file content boundary

Root instruction files are bootstraps, not platform handbooks. They contain only what a newly started session must know before it can retrieve anything else:

- which file governs this runner and how the paired file relates to it;
- the exact local Step Zero and session-ledger commands;
- an explicitly selected alternate profile, if this particular repo has one;
- the failure and degraded-source ladder;
- a small set of safety constraints that must bind before retrieval, such as protected history paths or who may mutate shared git state;
- search phrases or canonical article titles for the remaining operating rules.

Volatile architecture descriptions, inventories, quality-gate internals, deployment recipes, plugin contracts, messaging topology, long Git procedures, and historical rationale belong in knowledge-base articles or task-specific skills. If a source-code comment cites a detailed rule, it should cite that canonical article or executable gate, not a root bootstrap file. This prevents a root-file edit from silently orphaning the rule a production module thought it referenced.

### Canonical homes for content formerly embedded in root files

| Former root-file content | Canonical home | Availability |
|---|---|---|
| AGENTS.md/CLAUDE.md relationship, knowledge-access profiles, failure and empty-result semantics | This article; the paired hydration templates are the complete rendered samples | Every seed |
| Hydration, managed-block merge, launchers, shell hooks, and later seed updates | `github_midwife_plugin` — `01_hydration_runbook.md` and `05_seed_update_runbook.md` | Every seed |
| Charter, agent stance, decision briefs, collaboration craft, and session orientation | `ananta_platform/01_platform_overview/` and `24_operator_communication/` | The operator-communication subset ships in every seed; broader platform material is development-checkout-only |
| Python 3.13, fast-fail, typing, and craftsmanship rules | `Critical Development Guidelines v2` | Full platform development checkout |
| State-interface-only database access and filter grammar | `State Interface Filter Grammar`; SQL-access gate for executable enforcement | Full platform development checkout |
| Quality-gate commands, scope, and tracked-debt rules | `Peer Pre-Completion Gate Procedure`, `Gate Allowlist Conventions`, and `quality_gates/` | Full platform development checkout; selected executable gates ship in seeds |
| Session-ledger recall discipline | `Recall Before Work` plus the exact local command in both root templates | Command ships in every hydrated clone; the broader article is development-checkout-only |
| Messaging, role binding, delegation, and runner-specific wake behavior | The active messaging plugin's `Inter-Agent Messaging` material and runner-specific runbook, when that runbook is actually shipped | Capability-dependent; report a missing contract instead of guessing |
| Solet birth, teardown, deployment, and topology details | `13_solet_setup/`, the selected midwife/undertaker plugin runbook, and the applicable deployment skill | Capability- and environment-dependent |
| Shared-worktree git coordination | The root file's minimal pre-retrieval safety rule, `Peer Pre-Completion Gate Procedure`, and the repository's Git-Controller skill | Repository-specific |
| Historical rationale and one-time design analysis | The relevant versioned Workbench record | Development checkout only; never a runtime bootstrap dependency |

### Shared-checkout destructive-git boundary

A generated seed does not select a single-session or Git-Controller policy
automatically. When a repository does designate one sole git mutator, its root
bootstrap must name that role before retrieval because another session's
checkout, stash, or reset can destroy uncommitted work.

The designation does not authorize destructive operations. A controller must
stop for explicit operator confirmation before:

- `git push --force` or `git push -f`;
- `git reset --hard`;
- `git clean -fd`;
- rebasing a shared branch;
- deleting a branch with `git branch -d` or `git branch -D`;
- bypassing hooks with `--no-verify`; or
- discarding path contents with `git checkout -- <path>` or
  `git restore <path>`.

Branches and stashes remain recovery artifacts and are not deleted as part of
normal landing or cleanup. Repository-specific controller procedures may add
stricter confirmation or verification steps, but may not silently weaken the
root safety boundary.

## The content boundary this section is held to

`24_operator_communication/` ships in every seed, and nothing operator-personal may ever be placed in it. This article, its siblings, and any future addition here describe platform mechanics generic to any solet — no operator name, credential, or solet-specific path belongs here, including in examples.

## Managed-block upgrade rules

Two merge mechanisms exist for the files a hydration or re-hydration pass touches, and they are not interchangeable.

Both rendered templates carry the same HTML-comment-bounded block:
```markdown
<!-- BEGIN SOLET HYDRATION -->
...
<!-- END SOLET HYDRATION -->
```
Apply the rule independently to `AGENTS.md` and `CLAUDE.md`. On a first merge, preserve project-specific instructions and insert the rendered block after the first top-level heading if one exists, otherwise at the top. On a re-run, replace that file's existing block exactly and leave everything outside it untouched. Never overwrite one file with the other, and never skip an existing file because it looks "probably fine": the managed block is what keeps the shared bootstrap contract synchronized while preserving runner-specific and repository-specific instructions.

**`~/.claude/settings.json`** — no text markers; a structural JSON merge keyed by marker strings embedded in each hook's rendered command (for example `SOLET_STEP_ZERO_HOOK=<name>`). For each hook event, remove any existing command containing that solet's marker, then append the freshly rendered one. Unrelated top-level settings, unrelated hook events, and other solets' marker-bearing hooks are left in place, because this file governs every session on the machine, not just one solet's work. Back the file up before writing, and validate the written result as JSON before moving on.

Both mechanisms share the same intent despite the different syntax: idempotent on re-run, additive rather than replacing, and never silent about what changed.

## Reference

- `AGENTS.md` and `CLAUDE.md` (repo root) — the current clone's thin, runner-specific entrypoints.
- `plugins/github_midwife_plugin/knowledge_base/hydration_templates/AGENTS.md.template` — complete sample and render source for AGENTS.md-reading runners.
- `plugins/github_midwife_plugin/knowledge_base/hydration_templates/CLAUDE.md.template` — complete sample and render source for Claude Code.
- `plugins/github_midwife_plugin/knowledge_base/01_hydration_runbook.md` — the full hydration and settings-merge procedure this article distills into a contract.
- `knowledge_bases/ananta_platform/14_knowledge_retrieval/04_recall_before_work.md` — the full platform checkout's companion discipline for session-ledger recall; minimal seeds do not ship that broader platform-KB section.
