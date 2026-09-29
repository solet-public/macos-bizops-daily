# Seed Hydration Runbook — Operator Environment Setup After Genesis

Tags: knowledge:tag:planning_reference

Article Layer: 2

Article Role: operations_runbook

Article Tags: planning-stage:solet-lifecycle, evidence-category:operations-runbook, domain:local-solet, domain:client-deployment, consumer_profile:both

Embedding Description: Agent-facing runbook for setting up the user's own environment around a solet birth, covering per-solet database provisioning, launchers, optional multi-role fleets, additive shell integration, plugin hydration, generated CLAUDE.md and AGENTS.md knowledge-bootstrap files, and the default no-MCP CLI/watch transport. It distinguishes runner-specific delivery behavior: Claude may re-wake from its Stop hook; stock Codex currently ships no Stop-hook delivery mechanism at all (async command hooks are unsupported on measured stock builds), while managed spawn_session Codex workers are auto-driven through their host channel regardless. Also covers MCP as an explicit opt-in, bridge/router/role connectivity, session-ledger ingestion, verification, and the deployment report card.

## When to use this runbook

Run this ladder after genesis completes and the newborn solet boots, when the user's own environment still lacks the launch tooling. Genesis provisions the solet side only, so a fresh clone gives the user no command to start a named Claude Code session, no project `CLAUDE.md` or `AGENTS.md`, and no Claude Code hooks. This runbook is written TO the driving coding agent, the same audience as the genesis section of the seed README, and applies equally whether that agent is Claude Code or Codex. Everything here follows the genesis ladder shape: probe first, offer, act only under the user's normal tool-approval flow, verify, and stop and ask rather than guess.

Hydration is deliberately NOT genesis code. The files this ladder writes belong to the user's side of the genesis boundary, just like optional client registration does: the agent performs them with its own tools, in conversation, under the user's approval.

Updating an existing install is not a hydration concern and not a manual `git pull`: install the Manager (`brew install solet-public/tap/solet`), then `solet-manager import <name> --target <clone> --channel stable` once, and `solet-manager update <name> --dry-run` / `--yes` for every release after that — the Manager preserves the files this runbook writes and discloses them in its preview. The procedure, the refusal vocabulary, and the steps that remain manual are in `05_seed_update_runbook.md`.

## Three fixed rules

These rules are operator requirements, not tunable defaults. Every step below is shaped by them.

1. Shell startup files may be inspected to understand structure, but never echo, transmit, summarize, or persist secret-looking values. Treat tokens, keys, passwords, tenant URLs, and private hostnames as sensitive: if they appear, say only that secret-looking or private values were present and avoided. Integrate additively when possible; whole-file replacement is the fallback, not the default.
2. Any user-owned shell change is an OFFER, but the analysis is not optional. Before asking, inspect the current startup file, the shipped `zshrc.template`, the rendered `<clone>/client/<name>.zsh`, and any existing local launcher/fleet pattern you find. Then recommend a concrete plan in plain language. Do not ask the user to choose between raw implementation details before doing that review.
3. The user always ends up with a command-line way to start Claude Code with a properly named session, whether or not they accepted shell integration.

## Connectivity stance — no-MCP primary path

Treat the operator's machine policy as authoritative. If the operator says MCP
servers are restricted, the bridge is unavailable, or Claude Code cannot use
MCP, do not inspect `.mcp.json`, run `claude mcp`, search for `mcp__<name>__*`
tools, or try to prove the operator wrong. The primary interface is the
genesis-installed `<name>` command:

```bash
<name> health
<name> call service_interface::knowledge_service::search '{"query": "hydration runbook", "top_k": 8}'
```

That command uses the solet's localhost bridge internally, but it is not an
MCP server and needs no client-side MCP registration. Use it for knowledge-base
search, process calls, session-history setup, and — via `<name> watch` — peer
registration, role claiming, and message receive. The no-MCP path is the
DEFAULT and the complete path on every machine, not a managed-machine fallback.
Never pitch MCP proactively during hydration: if you notice the environment
permits MCP, you may mention once that an optional MCP bridge exists, and act
on it only if the operator explicitly asks for it.

## Glossary — distinct layers that all get called "the bridge"

Sessions repeatedly get lost by conflating these. When you explain or debug
connectivity, name the layer you mean:

- **Bridge** — the solet's localhost HTTP process-call surface; what the
  `<name>` command talks to. Needs no MCP.
- **Blue-green router** — a separate KeepAlive LaunchAgent (blue-green-capable
  profiles only) holding the stable public port while solet instances swap
  behind it. Not a message path; a port-stability device.
- **MCP bridge registration** — the optional `claude mcp add` client wiring
  that exposes `mcp__<name>__*` tools where policy permits MCP.
- **Development channel / remote-control** — fleet-launcher flags: the
  development channel is optional MCP-transport idle-wake (gated OFF by
  default in the fleet template; the flag alone is inert — it also needs a
  registered MCP server via `claude mcp add` AND Anthropic-direct auth, so
  it cannot function on Bedrock or on an MCP-blocked machine; the default
  wake path is the `<name> wake` Stop hook, which needs neither); Claude
  Code remote-control lets a human drive a session from another device via
  claude.ai — it is not a session-to-session wake transport.
- **Peer registry vs. durable role binding** — the registry is ephemeral
  connection presence; the role binding is the durable state row that
  `peer_send_by_name` / `peer_holds_role` resolve against. "Am I registered?"
  is answered by `peer_holds_role` resolution (role name + agent_instance_id),
  never by an entry in raw peer-list output. Claiming a binding requires a
  live registered peer connection; the DEFAULT holder is the
  `<name> watch` registered-presence watcher (register + claim + drain missed
  messages + stream deliveries, zero MCP), which the generated rename skill
  arms per session. The one-shot `<name>` command opens and closes a
  connection per command, so it invokes capabilities and sends outbound but
  never holds a role — that is the watcher's job. The optional MCP bridge is
  merely an alternative holder where an operator chose it.

## Render contract

All artifacts render from the flat template directory shipped in this plugin's knowledge base. The directory is `plugins/github_midwife_plugin/knowledge_base/hydration_templates/`. Substitute the double-brace tokens (`{{SOLET_NAME}}`, `{{CLONE_DIR}}`, `{{HYDRATION_DATE}}`, `{{BACKUP_PATH}}`) by literal string replacement only. Do not use Python `str.format`, which mangles double braces, and do not pass templates through a shell heredoc, which would expand the live `$VAR` references they contain. Angle-bracket forms like `<name>` in prose are human-facing instructions, never render targets. The full token table and file map live in `plugins/github_midwife_plugin/knowledge_base/hydration_templates/TEMPLATE_VARS.md`.

## Step 0 — before the birth: provision the role and database (wizard step 1)

Skip this section when `bootstrap.py` ran on this machine for this solet, because its role-and-db step already did everything here. It is REQUIRED before a verb-mode or existing-clone birth, and for any second solet on a machine that already runs one — the birth verb assumes it done and fail-louds if the auth line is missing. Canonical reference implementation: `bootstrap.py`'s `role_and_db` step (this section explains WHEN and WHY and gives the by-hand sequence; if the two ever disagree, `bootstrap.py` is the WHAT). Throughout, `<login-user>` is your macOS login user (the Homebrew-Postgres trust admin — run `whoami`).

Per-solet role isolation (operator override): each solet gets its OWN non-superuser PostgreSQL role, its OWN database, and its OWN schema — all named after it (`<name>` = `SOLET_NAME`) — so no solet can access anything another created. There is NO shared `ananta` role, and NO credential ever crosses a solet namespace. The database must exist, be owned by the solet's own role, carry the `pgvector` extension (per-database), have PUBLIC connect revoked, and be password-gated over localhost. Only the scram gate is INVISIBLE if missing (a missing db fails the launch; a missing extension crash-loops at readiness; a missing revoke surfaces as an isolation-probe failure) — so this step ends with two explicit probes, the same ones the birth verb re-runs.

Instance topology is YOUR decision as the driving agent, never runtime code: a shared local Postgres instance is the DEFAULT. Choose a separate instance ONLY for a concrete reason — an incompatible existing Postgres major version, an extension conflict, a port or ownership conflict, or a customer policy that mandates separation. Honest boundary: on one machine under one OS user a cluster split adds no real isolation (the same user can read both data directories); full local standalone means a separate OS user or machine, which is out of scope here. When unsure, use the shared instance.

1. **Probe (never clobber):** `psql -U <login-user> -d postgres -tAc "SELECT datdba::regrole FROM pg_database WHERE datname='<name>'"`. Empty output → continue. A row → the database EXISTS: STOP and present the facts (name, printed owner) to the user — resume-of-an-interrupted-birth vs foreign-name-collision is the user's call, made in conversation.
2. **Create role + database (this solet's OWN, non-superuser):** `createuser -U <login-user> <name>` (plain — NO `-s`/`--createdb`/`--createrole`, so the role is non-superuser, which is load-bearing for the revoke below), then `createdb -U <login-user> -O <name> <name>`, then `psql -U <login-user> -d <name> -c 'CREATE EXTENSION IF NOT EXISTS "vector"'` (the `vector` extension is non-trusted, so the `<login-user>` superuser must create it — the newborn's own non-superuser role cannot).
3. **Revoke PUBLIC (per-solet isolation, R4):** `psql -U <login-user> -d postgres -c 'REVOKE CONNECT, TEMP ON DATABASE "<name>" FROM PUBLIC'`. The owner role keeps implicit ALL and the `<login-user>` superuser bypasses; every OTHER role — including every sibling solet's role — is then refused at the front door with `permission denied for database`.
4. **Gate (first newborn on this machine only):** find the auth config with `psql -U <login-user> -d postgres -tAc 'SHOW hba_file'`; if the default-scram block is not already present, insert these lines immediately ABOVE the blanket `trust` block (first-match-wins), substituting your `<login-user>` for the admin — the `all all` scram lines gate EVERY per-solet role at once, so this is one-time machine setup, never a per-birth edit:
   ```
   local   all     <login-user>                            trust
   host    all     <login-user>    127.0.0.1/32            trust
   host    all     <login-user>    ::1/128                 trust
   local   all     all                                     scram-sha-256
   host    all     all             127.0.0.1/32            scram-sha-256
   host    all     all             ::1/128                 scram-sha-256
   ```
   The three `<login-user>` trust lines re-assert passwordless admin access above the scram block. Insert-only: any existing legacy scram lines stay byte-identical. Reload: `psql -U <login-user> -d postgres -c 'SELECT pg_reload_conf()'`.
5. **Verify — two probes.** (a) NEGATIVE scram: `PGPASSWORD=wrong psql "host=127.0.0.1 dbname=<name> user=<name>" -c 'SELECT 1'` MUST fail with `password authentication failed`. If it CONNECTS, the scram gate isn't taking effect (position? reload?) — fix and re-run; never proceed while a wrong password is accepted. (b) ISOLATION (verb-mode, after the credential is seeded): the newborn's own role, with its REAL password, connecting to a SIBLING database (the parent's) must fail with `permission denied for database`, NOT `password authentication failed` — the birth verb's seed subprocess runs this probe itself. On the FIRST solet on a machine there is no sibling, so (b) is recorded as not-applicable.

**No credential copy.** Under per-solet isolation the newborn seeds its OWN role's password — a fresh value, generated in its own venv and `ALTER ROLE`-d onto its own role. There is NO pipe of another solet's password into this one; the old same-machine "credential pipe" sub-step is RETIRED, because reading another solet's Keychain entry is the exact cross-namespace access the isolation ruling exists to prevent.

**Resuming after Step 0 — which path called you here decides the next move.** On the README/`bootstrap.py` path (a cloned seed): re-run `bootstrap.py` — its `role_and_db` probe reads the reconciled state (role + your new database both present, revoke applied, auth line verified) as healthy and resumes at the next step. On the verb-mode path (an already-running solet births this one): proceed to the birth verb (`plugin::github_midwife_plugin::birth_solet`), which re-runs the negative-auth probe, seeds the newborn's OWN role in the newborn's OWN venv, and runs the isolation probe.

Full derivation and rationale: the sequence relocated out of `github_midwife_plugin/venv_provision.py` (renamed from `acquisition.py` when acquisition mode was retired) when database creation became a wizard step, and shifted to per-solet role isolation. The verb keeps `venv_provision.verify_newborn_db_scram_gated` (the negative scram probe) plus the post-seed isolation probe. `bootstrap.py`'s `role_and_db` step is the canonical WHAT.

## Step 1 — probe

Check each expected-good state read-only before writing anything.

- `<clone>/client/` absent means hydration has not run; present means a re-run, so compare intended content before overwriting and prefer updating over clobbering.
- `<clone>/CLAUDE.md` and `<clone>/AGENTS.md` may exist from a prior hydration; same re-run rule for both. Probe `~/.claude/settings.json` for the `SOLET_STEP_ZERO_HOOK=` / `SOLET_ROLE_RECLAIM_HOOK=` markers (this solet's or another's) and `~/.claude/skills/rename/SKILL.md` plus `~/.claude/skills/feedback/SKILL.md` for prior installs; a pre-2026-07-22 hydration may have left hook copies in `<clone>/.claude/settings.json` — migrate those to user scope on a re-run rather than leaving both.
- Probe the GitHub CLI read-only: `gh --version`. The `/feedback` skill this ladder installs files upstream feedback non-interactively through `gh issue create --body-file`, so a machine without `gh` files nothing itself and falls back to a saved Markdown round the operator submits in a browser. Record present/absent for Step 2's offer; do not install anything during the probe.
- Read the shell templates that drive the integration: `zshrc.template`, `solet.zsh.template`, and any fleet/sample launcher file referenced by the clone or already present in the operator's startup file. Then inspect `~/.zshrc` or the active shell's startup file only far enough to classify its structure and choose an additive integration point. Do not print or copy secret-looking values into the transcript or generated files.
- Confirm genesis actually finished: the newborn's LaunchAgent plist exists and the manifest marker `<clone>/profile/data/github_midwife/attempt.json` is present. If not, stop; hydration follows genesis, it does not replace it.

## Step 2 — install the clone-side and user-scope artifacts

Render the artifacts in the canonical table below — **count the table, never this
sentence.** Most live inside the deployment directory; three install at USER
scope (`~/.claude/`) by operator ruling 2026-07-22, and two render only on an
accepted operator offer (marked ⚙ and handled at their own steps). The reason for user
scope is load-bearing, so understand it before deviating: Claude Code loads
project settings, skills, and project instructions from the directory a session
starts in, and sessions that coordinate through the solet routinely start
in OTHER repositories (a work repo, a knowledge-base repo). Anything installed
only in the deployment directory never reaches those sessions — verified in the
field: a fleet session in another repo got no role-reclaim prompt and its role
binding sat vacant with nobody told. **Operator ruling 2026-08-01 sharpens
this: nobody launches production sessions from the deployment directory at
all**, which makes the user-scope surfaces the only ones production reads.

**The session hooks are no longer written into the settings file — they ship as
a Claude Code plugin.** Step 2's settings artifact now installs two keys, an
`extraKnownMarketplaces` entry naming the deployment's own catalogue and an
`enabledPlugins` entry switching on `coordination-hooks`; the hooks themselves
(knowledge-base-first reminder, check-your-messages reminder, role-binding
reminder, idle-wake waiter, git-mutation gate) live in that plugin, are reviewed
as code rather than as JSON strings, and update with the deployment. Every
shipped entrypoint but one no-ops unless `AGENT_SESSION_LABEL` is set — so a
plain `claude` session anywhere on the machine gets zero output and zero
errors — and that one, the git gate, is label-independent by design: it arms on
`GIT_CONTROLLER_NAME`'s presence instead (Step 4a). Read the roster from the
plugin's own `hooks/hooks.json`; do not restate a count here.

The wake half of messaging is one of those plugin hooks: a `Stop` hook running
`$AGENT_WAKE_CLI wake` (`asyncRewake` shape — it arms in the background when a
labeled session goes idle, blocks at zero token cost on the watcher's
delivery spool, and exits with the hook wake code when a message lands, so
the delivery becomes a session turn). This is what closes the loop on
`queued_watcher` deliveries without MCP and independent of inference
provider; without it an idle session only sees messages at its next look.

Writing inside the deployment directory needs no offer beyond the user's normal
tool-approval flow. The three Claude user-scope writes touch the user's own
Claude configuration; the Codex marketplace/plugin commands below separately
update Codex's user configuration through its supported CLI. Back up an existing
`~/.claude/settings.json` before first modification, and merge structurally —
never whole-file-replace it. If a destination path is absent, write the rendered template directly. If
`CLAUDE.md`, `AGENTS.md`, `~/.claude/settings.json`, or `~/.claude/CLAUDE.md`
already exists, do the guided merge below instead of skipping the file or
replacing the whole thing.

**This table is the CANONICAL roster of hydration templates.** Every other
document references it; none restates its membership or its count. If you are
about to write "N artifacts" anywhere else, write a pointer to this table
instead — a restated count is a future stale arity.

| Template | Destination | Mode |
|---|---|---|
| `solet.zsh.template` | `<clone>/client/<name>.zsh` | 0644 |
| `claude_launcher.template` | `<clone>/client/bin/claude-<name>` | 0755 |
| `codex_launcher.template` | `<clone>/client/bin/codex-<name>` | 0755 |
| `launch.template` | `<clone>/client/bin/launch-<name>` | 0755 |
| `CLAUDE.md.template` | `<clone>/CLAUDE.md` | 0644 |
| `AGENTS.md.template` | `<clone>/AGENTS.md` | 0644 |
| `marketplace_json.template` | `<clone>/.claude-plugin/marketplace.json` | 0644 |
| `codex_marketplace_json.template` | `<clone>/.agents/plugins/marketplace.json` | 0644 |
| `claude_settings.json.template` | `~/.claude/settings.json` (reference shape only — the install commands below write the live file; see the coordination-hooks install step) | 0644 |
| `rename_skill_SKILL.md.template` | `~/.claude/skills/rename/SKILL.md` | 0644 |
| `feedback_skill_SKILL.md.template` | `~/.claude/skills/feedback/SKILL.md` | 0644 |
| `claude_session_overlay.json.template` | `<clone>/client/claude-session-overlay.json` | 0644 |
| `user_claude_md_section.template` | `~/.claude/CLAUDE.md` (create-or-merge, one marker-delimited section) | 0644 |
| `zshrc_block.template` | the body of the `~/.zshrc` managed block (rendered in code by the shell-integration operation under the versioned `# BEGIN SOLET <name> v<digest8>` marker; not a whole-file render) | 0644 |
| `launchagent.plist.template` | `~/Library/LaunchAgents/local.solet.<name>.plist` (rendered in code by the autostart step, with a `rendered-from` stamp; not an agent-driven render) | 0644 |
| ⚙ `zshrc.template` | the user's startup file — **CONDITIONAL: only on an accepted shell-integration offer** (Step 2); additive integration is preferred and whole-file replacement is the fallback, rendered with `{{BACKUP_PATH}}` | 0644 |
| ⚙ `fleet_functions.zsh.template` | `<clone>/client/<name>-fleet.zsh` — **CONDITIONAL: only on an accepted Step 4a git-safety offer**; sourced from the user's startup file | 0644 |

⚙ = **conditional render.** The two conditional rows are listed here on purpose:
an operator who renders only the unconditional rows and stops has an incomplete
deployment, and would never learn of the remaining two from a table that omitted
them. Their own steps carry the offer wording and the accept/decline handling —
this table exists so nothing is invisible, not to replace those steps.

### GitHub CLI (`gh`) — the `/feedback` skill's filing tool

The `/feedback` skill rendered above files upstream feedback non-interactively
through the seed repository's issue endpoint, with a structured body that
mirrors the required issue-form fields. The GitHub CLI is therefore a real
dependency of a capability this ladder installs — not an optional nicety.
If Step 1's probe found `gh` absent, offer its install now, under the user's
normal tool-approval flow like every other action in this ladder:

```bash
brew install gh
```

Then verify `gh --version` answers. Authentication is not part of hydration,
but filing later requires a non-interactive authenticated `gh` session. An
operator who wants to enable filing can arrange an approved token-backed CLI
login (for example, `gh auth login --with-token` with the token supplied by the
secret broker); never substitute browser authentication. That account is an
ordinary read-only account on the seed repository, and the filing path is built
for it: no labels, no sub-issue links, no collaborator access. If the user
declines the install, or `gh` is not signed in, record it and move on — the
skill's filing step then saves the round as a Markdown file and hands the
operator the new-issue URL, so nothing fails silently later.

### Structured-choice prompts (`AskUserQuestion`) — default deny, launcher-enforced

The Claude launcher rendered above passes
`--settings <clone>/client/claude-session-overlay.json`, whose only content is
a permissions deny of the `AskUserQuestion` structured-choice tool. This is an
operator-ruled default (2026-08-14): the picker blocks its session until a
human answers — its auto-continue timeout defaults to `"never"` — so in
unattended, worker, or peer-driven sessions it strands the session and breaks
inter-agent messaging flows. Settings sources merge in Claude Code, so the
overlay unions the deny into launcher-started sessions without editing any
file the user owns. Two overrides, tell the user both exist:

- Per-launch: `SOLET_ALLOW_ASKUSERQUESTION=1 claude-<name> <label>` skips the
  overlay for that attended session.
- If the user says they actually want structured-choice prompts as their
  default, remove the overlay flag lines from `<clone>/client/bin/claude-<name>`
  and record the choice; a softer alternative to offer is keeping the tool but
  setting `askUserQuestionTimeout` in their user settings so an unanswered
  dialog eventually auto-continues instead of hanging.

Codex has no equivalent structured-choice tool, so the Codex launcher renders
without an overlay — that asymmetry is correct, not an omission.

⚠️ **Precondition — tell the user this while you are rendering the overlay, not
after they report the picker firing.** This mechanism is wholly inoperative on
a machine whose enterprise-managed Claude Code policy sets
`"allowManagedPermissionRulesOnly": true`. Under that flag Claude Code honours
only the permission rules inside the managed settings file itself; user- and
project-level `allow`/`ask`/`deny` rules do not apply at all, in every
permission mode. Both the overlay above and any deny in the user's own
`~/.claude/settings.json` are dropped before permission evaluation, and
**nothing announces it** — the picker simply fires as though no deny existed.
Adopter-reported (§45.1), confirmed against Claude Code's own permissions
documentation. It is not a stale-session effect: permission settings reload
live in-session, so it reproduces in a brand-new session.

The same flag silently voids **three other denies** in a hydrated deployment,
which an operator reading only this section would not learn: the `Agent`/`Task`
deny in every spawned tmux worker (`tmux_adapter.py`, which also carries
`AskUserQuestion`), the same deny in every spawned headless worker
(`headless_adapter.py`), and the project-scope `<clone>/.claude/settings.json`
deny in the clone itself. That prohibition still holds at the instruction
layer (the rendered `CLAUDE.md`), so it degrades from two layers to one, not
to zero.

Frame it as hygiene, never as a security control: a deny that a local policy
file can switch off was never a boundary. The problem is the silence, not a
bypass. Both spawn host drivers detect the flag and log it loudly at spawn
time — they do not refuse — and either driver's `capability_report()` answers
the question without spawning anything, via `permission_denies_operative` and
`permission_policy_path`.

**Install the stock-Codex plugin through Codex, not by copying cache state.**
After rendering `.agents/plugins/marketplace.json`, run these from a normal
shell with `<clone>` and `<marketplace-name>` replaced by the exact rendered
values:

```bash
codex plugin marketplace add <clone>
codex plugin add coordination-hooks@<marketplace-name>
codex plugin list
```

The final listing must show the plugin installed and enabled from this clone.
Then start `codex-<name>`, open `/hooks`, inspect the commands, matchers, and
timeouts, and trust only the expected definitions. Trust is a distinct review
step: installation does not authorize hooks. To revoke one later, disable that
definition in `/hooks`; uninstall alone retains its prior enabled/trusted state
and an identical reinstall can reactivate it. Do not copy another installation's
cache, trust hash, or private config stanza.

The final `/hooks` listing shows no `Stop` definition — this plugin currently
ships none (codex-0147-async-hook-regression, 2026-08-13). Stock Codex does
not execute async command hooks on any measured build (0.141.0, 0.147.0); a
prior `async: true` `Stop` binding never fired and was removed rather than
left registered-but-dead, and a synchronous one is not an acceptable
substitute (it would block the turn boundary). Autonomous Codex workers must
be launched through `spawn_session`, where `drive_on_delivery` uses the host
driver channel independent of this plugin's hooks; an operator-launched Codex
TUI has no per-turn delivery notice and drains the durable inbox on its own
initiative.

Known limitation, state it rather than papering over it: on a machine running
MORE THAN ONE solet, the per-deployment surfaces coexist cleanly — each
deployment registers its own marketplace and plugin under its own name in
`~/.claude/settings.json`, and each owns its own name-keyed section in
`~/.claude/CLAUDE.md` — but every labeled session then loads every
deployment's Claude plugin, so those reminders are per-deployment and they add
up. Codex marketplaces remain separately named, but an operator can likewise
install several coordination plugins into one Codex home and should enable only
the deployment(s) intended for that session. The user-scope `rename` skill is a
singleton, so the LAST hydrated solet owns it. Single-solet machines — the normal case — are unaffected. If the
operator runs several solets and this bites, surface it as a decision rather
than inventing a scheme ad hoc.

### Generating the knowledge-bootstrap files: `CLAUDE.md` for Claude Code, `AGENTS.md` for a Codex session

Step 2 renders both files for the newborn clone from templates: `CLAUDE.md.template` produces the generated `CLAUDE.md` a Claude Code session reads, and `AGENTS.md.template` produces the generated `AGENTS.md` a Codex session reads — the same knowledge-bootstrap contract, rendered once per runner.

**Existing `CLAUDE.md` / `AGENTS.md` guidance.** The rendered `CLAUDE.md.template` and `AGENTS.md.template` each contain a solet-owned block bounded by the identical markers:

```markdown
<!-- BEGIN SOLET HYDRATION -->
...
<!-- END SOLET HYDRATION -->
```

If `<clone>/CLAUDE.md` or `<clone>/AGENTS.md` already exists, preserve its project-specific instructions and insert or update only that managed block — the same rule, applied independently to each file. On a re-run, replace the existing block exactly. On a first merge, insert the block after the first top-level heading when one exists; otherwise insert it at the top. Leave the rest of the file in place. Do not decide that an existing `CLAUDE.md` or `AGENTS.md` is "probably fine" and leave it untouched: the solet operating block is how future Claude Code and Codex sessions learn the access-mode contract — the no-MCP `<name> call` CLI as the default for knowledge/process/session-ledger access, MCP as strictly opt-in on explicit operator request, and source-artifact recovery (reading the KB's raw markdown directly) as the last-resort fallback when the solet runtime itself is unavailable — plus the implementation/debugging requirement to search the solet's own knowledge base first and the hook/messaging expectations for that tool.

Both templates carry the SAME access-mode contract in near-identical wording;
they diverge only on genuinely tool-specific mechanics: Claude Code's
`claude-<name>` integration versus stock Codex's generated `codex-<name>`
launcher and exact durable-inbox command (currently no Stop-hook delivery
notice — see the Stock Codex durable inbox section of `AGENTS.md.template`).
If you find yourself rewriting one file's access-mode section without the
other, stop and update both — this is exactly the drift the shared contract
exists to prevent.

**Installing `coordination-hooks` — run the explicit CLI commands, never a
hand-merged declaration alone.** `claude_settings.json.template` still
renders as part of this step's file table above, as the reference for what
the resulting `~/.claude/settings.json` should contain, and the render
smoke still checks it — but do not hand-merge that rendered JSON into
`~/.claude/settings.json` as the install mechanism.

**Measured 2026-08-02** (`05_seed_update_runbook.md` Step 6 has the full
three-way table): a hand-written `extraKnownMarketplaces` +
`enabledPlugins` declaration, with no explicit install step, either fails to
register at all (any non-interactive session — headless `-p`, or any
session whose stdout is not a TTY, skips Claude Code's workspace-trust
dialog by design, and the declaration is silently orphaned) or, even in a
genuinely interactive trusted session, registers the marketplace but leaves
the plugin itself in a broken state: `installed_plugins.json` claims a
cache `installPath` that is never actually created on disk, so the hooks
cannot execute. Both failure modes are silent — nothing in a normal session
says so.

Run these two commands instead, from a normal shell, with `<clone>` and
`<marketplace-name>` replaced by the exact rendered values:

```bash
claude plugin marketplace add <clone>
claude plugin install coordination-hooks@<marketplace-name>
```

Both default to `--scope user`, matching this deployment's per-deployment,
all-sessions-on-this-machine target — no `--scope` flag needed. Each
command reports explicit success or failure (`✔ Successfully installed
plugin…` / a named error) rather than failing silently, and — confirmed by
direct measurement, not inferred — `marketplace add` writes the identical
`extraKnownMarketplaces` object-form entry the rendered template above
shows, and `install` writes the identical `enabledPlugins` entry, so that
declarative shape still ends up in `~/.claude/settings.json`; the commands
are how it gets there reliably, not a hand-edit in addition to them. If either command fails, fix the reported cause and re-run — never
paper over a failure by hand-writing the JSON anyway, since that reproduces
exactly the broken-but-silent state this replaces.

**If the existing file still carries this deployment's own pre-consolidation inline hooks** — a `SOLET_STEP_ZERO_HOOK=<name>` command, a `SOLET_ROLE_RECLAIM_HOOK=<name>` command, or a `SOLET_WAKE_HOOK=<name>` `Stop` entry (the shape shipped before WS-5b-core moved these into the plugin) — back up `~/.claude/settings.json` first (same collision-probed backup naming as Step 3's zshrc backup), then remove them by hand before running the commands above. They are this deployment's own superseded configuration, not another tool's, and leaving them in place double-fires every reminder and races two wakers on one spool lock now that the plugin owns all three.

**Correction, measured on a live deployment (fleet-watch-transport-migration phase 3,
2026-08-06): the three literal marker strings above are not reliable search keys on every
deployment.** On that machine's actual `~/.claude/settings.json`, only the `Stop`/wake entry
carried its literal `SOLET_WAKE_HOOK=` marker; the KB-first, check-messages, and
role-reclaim hooks were present but rendered from an older or differently-templated form that
did not carry the other two marker strings at all. **Identify the four hooks to remove by ROLE
and CONTENT (a `Stop` entry running the wake command; `UserPromptSubmit`/`SessionStart` entries
matching the KB-first / check-messages / role-reclaim reminder text), not by grepping for the
marker strings alone** — a marker-only search can silently undercount on a deployment whose
`settings.json` predates the exact template render this runbook assumes.

**Second correction: `<clone>` in the two install commands above must be a clone that carries
`.claude-plugin/marketplace.json` at its root.** A hydrated seed clone gets this file from seed
assembly; an ORIGIN dev checkout (one that was never assembled from a seed — e.g. the solet's
own primary development repo) does not, and `claude plugin marketplace add <clone>` fails
against it with no `.claude-plugin/marketplace.json` present. If `<clone>` is such an origin
checkout, render the file first: `deployment/scripts/setup_clone.sh` (run once per clone) now
includes this render step, or render
`hydration_templates/marketplace_json.template` by hand
(`{{MARKETPLACE_NAME}}` → the marketplace name from D-5a.2's derivation) to
`<clone>/.claude-plugin/marketplace.json` before running the two commands above.

**Then verify, explicitly — the command's own success report is not the
verification.** Confirm the marketplace appears in
`~/.claude/plugins/known_marketplaces.json` and the plugin in
`~/.claude/plugins/installed_plugins.json` with an `installPath` that
actually exists on disk (`ls` it, do not just read the JSON) — the broken
registered-but-uncached state above is exactly what a JSON-only check would
miss. If the marketplace or plugin is absent, or the `installPath` doesn't
exist, re-run the two commands rather than assuming a slow or partial
install; if they still fail, re-read the rendered `marketplace.json` for a
malformed catalogue (missing `owner`, unparseable JSON, a name colliding
with a Claude Code reserved name) before assuming anything else.

**`~/.claude/CLAUDE.md` — the production instruction surface (create-or-merge).** This is the file production sessions actually read, and on a fresh machine **it usually does not exist yet** — so this step CREATES it when absent and merges into it when present. Never rewrite it wholesale: it is the user's own file, and a solet owns exactly one marker-delimited section in it.

Render `user_claude_md_section.template` and install the result between its own markers:

```markdown
<!-- BEGIN SOLET <name> v1 -->
…rendered section body…
<!-- END SOLET <name> -->
```

The rules are the same shape as the deployment-directory managed block, with two differences that matter:

- **The marker is keyed to this deployment's name, not to a generic `SOLET HYDRATION` label.** One `~/.claude/CLAUDE.md` may host sections from several solets; a generic marker would make each install clobber the last. On a re-run, replace this deployment's section exactly, in place; leave other deployments' sections and all of the user's own content untouched. On a first merge, append the section at the end of the file rather than at the top — the user's own instructions come first in their own file.
- **The version in the opening marker is load-bearing.** A later installer shipping a newer section body reads `v1` and knows to re-merge rather than duplicate or silently leave a stale section in place. Uninstalling the deployment removes exactly this marker pair and everything between it.

**Take the body from the template, not from memory.** The section names one process key and one pinned search query, and that query is tuned together with the knowledge-base orientation article it retrieves — the article's own retrieval test is what keeps them in agreement. A body retyped from an example, or copied from an older runbook, drifts out of retrieval silently: the command still looks right, still runs, and returns the wrong page or nothing.

### Launcher permission mode

`claude-<name>` passes `--permission-mode bypassPermissions` by default, so a
fresh install can do work without an approval prompt on every action. That is
the shipped default, not a fixed posture — **surface the choice explicitly
rather than letting it pass unremarked** (seed feedback #30/§48.3, 2026-08-24:
the operator who never heard this tradeoff is the one who later fights a CLI
update that silently changes what an unset posture means). Ask, in plain
words: "New sessions from this launcher start able to act without asking —
want that, or should they ask before every tool use?" Three states exist,
set via `SOLET_PERMISSION_POSTURE` in the operator's shell profile (see
`TEMPLATE_VARS.md` for the full contract):

```bash
export SOLET_PERMISSION_POSTURE=bypass    # default — act immediately, no per-action prompt
export SOLET_PERMISSION_POSTURE=prompt    # standard approval flow, forced regardless of global settings
export SOLET_PERMISSION_POSTURE=inherit   # defer entirely to this machine's own Claude Code settings
```

State the distinction between the last two plainly, because it is the one an
operator cannot guess: `prompt` is a fixed request that does not depend on
anything else on the machine; `inherit` follows whatever this machine's own
global Claude Code configuration currently says, which can change meaning
across a CLI update with no local signal that it did (the exact failure #30
reported — a flagless-equivalent session came up auto-denying sanctioned
work, with no approval path, after such an update). An operator who wants
predictable behavior across updates wants `prompt`, not `inherit`, even
though `inherit` sounds like the more conservative choice.

`SOLET_PERMISSION_PROMPTS=1` still works (permanent alias for
`SOLET_PERMISSION_POSTURE=inherit`, its exact pre-#30 meaning) — mention it
only if the operator already has it set somewhere, not as the primary route
for a new setup.

`codex-<name>` is unchanged and still passes no dangerous approval, sandbox,
hook-trust, or MCP bypass. `CODEX_BIN` may select an explicit stock executable;
never point it at a locally patched receive build.

**Do not tell an operator to pass the flag to the launcher.** `claude-<name>`
takes POSITIONAL arguments (`label`, `model`, `effort`) and forwards no
arbitrary flags, so `claude-<name> --permission-mode bypassPermissions` is read
as `label="--permission-mode"`, `model="bypassPermissions"` — a garbage session
name and an invalid model, whose error message names neither cause. Use the
environment variable.

The coordination hooks no-op silently for bare sessions, and at user scope this guard is load-bearing for the user's ENTIRE Claude Code use: a plain `claude` session anywhere on the machine has no session-label environment variable set, and it must get zero output and zero errors. **The rule, not a count:** every shipped entrypoint but one (`step_zero_reminder.js`, `check_messages_reminder.js`, `role_binding_reminder.js`) returns nothing unless `AGENT_SESSION_LABEL` is set. Read the roster from the plugin's own `hooks/hooks.json` rather than trusting a number in prose: entrypoints, registrations, and matcher groups are three different counts, and all three move as hooks land (the Codex plugin currently ships no `Stop`-bound entrypoint at all — codex-0147-async-hook-regression, 2026-08-13). The exception, `git_controller_gate.py`, reads no label at all: it holds the same invariant by a different boundary — it does nothing unless `GIT_CONTROLLER_NAME` is present in the environment (Step 4a), which on a deployment that never sets it is never. If you edit a hook, keep whichever of the two boundaries it already has; a hook that gains output on an unlabeled, unarmed session is a defect against this paragraph, not a feature.

## Step 3 — review, recommend, then offer shell integration

Do not start this step by asking a jargon question such as "source `client/<name>.zsh` from `.zshrc`, or use the absolute path?" That question is not grounded enough for an operator. First do the review from Step 1, then present a recommendation that explains the actual outcome.

The normal recommendation is additive shell integration: put a small managed source block in the operator's startup file so new terminals can run `claude-<name>` and related helper commands directly. Prefer this whenever the existing file can accept one more source block without changing unrelated content. Whole-file replacement is only for a missing, empty, or trivially generated file, or when the user explicitly chooses that route after seeing why additive integration is not coherent.

Before asking for approval, prepare these facts for yourself:

- which startup file you inspected, and whether it was missing, simple, already managed, or complex;
- whether any existing Claude/solet launcher pattern was present, described only structurally and without quoting private values;
- the exact source line or managed block you intend to add or update;
- the backup path you will create before writing;
- the operator-visible result: new terminals can run `claude-<name>` directly, while the absolute path `<clone>/client/bin/claude-<name>` remains available if they decline.

The user-facing offer should be specific and plain. Use this shape, adapted to the facts you found:

```
I checked your shell startup file and the <name> shell template. I recommend
adding a small managed block to ~/.zshrc that loads <name>'s launcher from this
clone. I will save a backup first, will not print or change any existing
secrets or unrelated settings, and existing aliases/functions will stay in
place. After that, new terminals can run claude-<name> directly. Should I make
that additive change?
```

If the startup file is missing, say that and recommend creating the minimal file. If the file is complex or already has a conflicting managed block, explain only the structure and the consequence, then ask the narrow decision you cannot safely make. Act only on an explicit yes.

On accept:

1. Choose the backup path `~/.zshrc.pre-<name>-hydration-<YYYYMMDD>` and collision-probe it with `test -f`. If it already exists, append `-HHMMSS`, then `-2`, `-3`, and so on until the path is free. An existing backup is never overwritten under any circumstances.
2. Copy the existing startup file to the backup path before changing it. If the file is absent, say so and create it.
3. Inspect the current structure and append or update a small managed block that sources `<clone>/client/<name>.zsh`. Do not duplicate the block on rerun.
4. If additive integration is not coherent, explain why and offer the whole-file replacement rendered from `zshrc.template` with `{{BACKUP_PATH}}` set to the backup path.

On decline: stop. No partial edits, no retry next session. Tell the user the launcher still works by absolute path: `<clone>/client/bin/claude-<name>`, and the newborn's `<name>` CLI should already be on PATH via genesis's `~/.local/bin/<name>` symlink.

If `~/.zshrc` did not exist at probe time, write a minimal file directly and say so; there is nothing to back up.

### Step 3-verify — prove the paths resolve IN A NEW SHELL (required, added 2026-08-20)

**Do not skip this because the edit "obviously worked."** Every failure this step
catches was found the hard way, on real installs, by an operator who had been
told hydration succeeded:

- `.zshrc` carrying no solet wiring at all, because the Step 3 offer was declined
  or never made — so `claude-<name>` was simply not a command.
- Hooks resolving `python3` to the macOS system **3.9**, while the shipped hooks
  are 3.13 source that imports `datetime.UTC` (needs ≥ 3.11). Every tool call
  produced an ImportError traceback surfaced as a hook error. 8 of 20 shipped
  hook modules failed to import that way.

**Verify in a FRESH LOGIN SHELL, never the one you just edited** — the shell you
made the change in may already carry state (a hand-set PATH, an exported
variable) that the operator's next terminal will not have. That is the whole
point of the check, and running it in the current shell reliably reports a green
that the next terminal will not reproduce.

```bash
zsh -lic '
  echo "source line : $(grep -c "client/<name>.zsh" ~/.zshrc)  (expect 1)"
  echo "launcher    : $(command -v claude-<name> || echo MISSING)"
  echo "solet CLI   : $(command -v <name> || echo MISSING)"
  echo "python3     : $(command -v python3 || echo MISSING) -> $(python3 -V 2>&1)"
  echo "clone venv  : $(<clone>/.venv/bin/python3 -V 2>&1)"
'
```

Read the results against these bars, and report every line to the operator
rather than only the failures — a silent pass teaches them nothing about what
their machine now depends on:

| line | required | if it fails |
|---|---|---|
| source line | `1` | Step 3 was declined or did not land. Either add the managed block or tell the operator the absolute-path form is now their supported route — do not leave it ambiguous. |
| launcher | resolves | the `client/bin` PATH export is not reaching new shells. |
| solet CLI | resolves | genesis's `~/.local/bin/<name>` symlink is missing or `~/.local/bin` is not on PATH. |
| python3 | **≥ 3.11**, ideally 3.13 | the coordination hooks go silently INERT below 3.11 — no heartbeat, no rotation notice, no memory capture. Not an error and not a crash, but the operator must be TOLD, because inert-and-silent is indistinguishable from working unless someone says so. |
| clone venv | 3.13 | the venv is the platform's own interpreter; a wrong version here is a genesis defect, not a PATH one. |

**`python3` resolving to 3.9 is expected on a stock macOS.** It is no longer a
crash: each shipped hook carries an interpreter-floor guard that exits 0 silently
below 3.11, placed before the first 3.11+ import because an ImportError at module
level cannot be caught from inside the file. Before that guard existed, the
system interpreter produced a traceback on EVERY tool call, surfaced to the
operator as a hook error on a correctly-installed machine. **Report the version
you found even when nothing is broken** — a 3.9 machine has working sessions and
dead coordination, and only this line distinguishes that from a healthy install.

## Step 4 — named-session start

Requirement 3 is satisfied by the launcher that step 2 installed on the user's machine. With the accepted zshrc (new terminal window, or `source ~/.zshrc`), the command `claude-<name>` is on PATH; declined, the absolute path form works identically. Optional arguments: a session label (default `Operator`), then a model alias, then an effort level. The launcher exports the session-label and stable session-id environment variables and execs `claude --name <label>`, which is what makes the session properly named in both Claude Code and the solet's peer registry.

## Step 4a — offer: multi-role fleet coordination (optional)

Fleet coordination needs NO MCP by default: each session's SessionStart hook
invokes the rename skill, which follows the session's declared transport
(`FLEET_TRANSPORT`, exported by the fleet launcher; unset means
`watch`) and on the default arms that session's `<name> watch`
registered-presence watcher — register, claim the role as the durable
binding, drain missed messages, stream deliveries. Sessions are then
addressable by role name (`peer_send_by_name`) and receive role-addressed
messages, on any machine, including MCP-policy-blocked ones. Do not attempt
`claude mcp add` against policy and do not build ad-hoc registration
mechanisms — on the default transport the watcher is the mechanism.

**The transport is a declared choice, offered in one sentence, never a
questionnaire.** Both mechanisms ship with every seed: the watcher path
(default, works everywhere) and the MCP-bridge path (explicit opt-in where
policy permits). When offering Step 4a, state the default rather than asking
a question — "Fleet coordination will run on the `<name> watch` CLI watcher;
no MCP needed, and it works on policy-locked machines too. If you'd rather
attach sessions through MCP, say so and I'll wire that instead." A zero-cost
probe may inform the phrasing (a managed policy file such as
`/Library/Application Support/ClaudeCode/managed-settings.json` usually
means MCP registration is restricted, so say the watcher is likely the only
permitted path) — the probe informs wording, never the decision. If the
operator pushes back and asks for MCP: run Step 5's explicit-request
`claude mcp add` registration, set `{{SOLET_NAME}}_FLEET_TRANSPORT=mcp`
plus `{{SOLET_NAME}}_FLEET_MCP_CHANNELS=1` where the fleet file is
sourced, and have them relaunch fleet sessions. If `claude mcp add` fails
against policy, report exactly that and state the watcher default stands —
on such a machine that IS the supported design, not a degraded state.
Whichever transport is declared, the rename skill and the user-scope hooks
follow it and never silently switch to the other (fail-loud rule; design
record: platform repo `workbench/2026-07-28_fleet_transport_parity_design.md`).

**Target work repos get the guidance too.** Launcher-started Claude Code
sessions carry the solet guidance from the user-scope hooks in every
prompt, from any directory — but a BARE `claude` session in a work repo gets
nothing (the label guard is deliberate), and Codex sessions have no
equivalent hook injection at all. So when the operator names the repos the
fleet (Claude Code and/or Codex) will work in, OFFER the same managed-block
merge Step 2 does for the clone's `CLAUDE.md` and `AGENTS.md` into EACH target
repo's matching file — Claude-driven repos get `CLAUDE.md`, Codex-driven
repos get `AGENTS.md`, a repo used by both gets both files (same
`BEGIN/END SOLET HYDRATION` markers, same insert-or-update rule,
preserve everything else in the file). That block is what tells any future
session in that repo — launcher-started or not — that the solet exists,
how to reach it (`<name> call`, and for Claude Code `<name> watch`), and that
MCP is not needed for knowledge or process access.

Hook scope matters for fleets and is already settled (operator ruling
2026-07-22): Step 2 installs the Step Zero and role-reclaim hooks at USER
scope (`~/.claude/settings.json`), guarded on the session-label environment
variable, precisely so fleet sessions working in OTHER repos still get them —
Claude Code loads project settings from the directory a session starts in, so
clone-local hooks would never fire there. If you find the hooks only in the
clone's `.claude/settings.json`, that is a pre-ruling install: migrate them to
user scope per Step 2 rather than debugging why fleet sessions get no
role-reclaim prompt.

Everything through Step 4 gives the operator ONE properly-named session. Some operators want to run SEVERAL Claude Code (and/or Codex) sessions concurrently against the same solet, each independently addressable — a coordinator session, a session that owns all git mutations, an architect/reviewer session, worker sessions. This is exactly the pattern this platform's own development runs on (see this repo's own `.zshrc` and `.claude/settings.json` for a live, battle-tested example — `claude-coordinator-dawn`, `claude-git-controller`, `claude-architect`, and so on). Offer this explicitly; do not build it unasked, and do not build it as part of the Step 2 baseline.

**Ask first, don't prescribe.** "Do you want to run just one session with {{SOLET_NAME}}, or several at once, each with its own role?" If several: "How many, and what should each be called?" Offer this platform's own roster as a concrete starting point, scaled to what the operator actually needs — most operators need far fewer than this repo's ~14 roles:

- A **coordinator** role (drives the work, delegates, holds the plan).
- A **git-controller** role, if more than one session will edit files in the same clone — see the git-safety paragraph below for why this matters.
- An **architect** or **reviewer** role, if the operator wants a second opinion / design-ruling session distinct from the one doing the work.
- Plain worker roles (`worker-1`, `worker-2`, ...) for parallel task lanes.

Let the operator pick names and count; do not invent roles they did not ask for.

**What accepting builds.** Render `fleet_functions.zsh.template` to `<clone>/client/{{SOLET_NAME}}-fleet.zsh` and add `source "<clone>/client/{{SOLET_NAME}}-fleet.zsh"` to `<clone>/client/{{SOLET_NAME}}.zsh` (the file Step 2 already installed). The template gives one shared `_claude_for_{{SOLET_NAME}}(role, model, effort)` launcher plus ONE example role function to copy per role the operator chose (a restart variant is included in the same comment block). This is a genuinely different trust level than the Step 2 launcher — read the template's own header comment and carry that reasoning into the conversation with the operator, don't just render it silently:

- **Every fleet session runs inside a tmux session named exactly its role.** The template's `_tmux_host_for_{{SOLET_NAME}}` creates a detached tmux session per role, re-enters the launcher inside it (arguments quoted with `${(q)…}`, so an empty model or effort stays empty), then attaches. Detach with `C-b d`; the session keeps running. Attach later with `tmux attach -t =<role>` (list with `tmux ls`). Launching a role whose tmux session already exists attaches to it instead of starting a second copy, and a machine without tmux refuses loudly with no bare-terminal fallback. This is not optional polish: headless sessions are unsupported, and the solet drives and wakes a session through its tmux pane, so a session started in a plain terminal tab cannot be reached. tmux is provisioned by the setup flow's `setup::tmux.install` step (Homebrew `tmux`); if `command -v tmux` finds nothing, install it before rendering this file. **Existing solets:** the fleet file is a rendered copy in `<clone>/client/{{SOLET_NAME}}-fleet.zsh`, not part of `~/.zshrc` and not a Manager-managed artifact, so a seed update does not rewrite it. An operator who accepted this offer before the tmux host existed re-runs this step: re-render `fleet_functions.zsh.template` over the file (keeping the operator's own per-role functions, which sit outside the shared launcher) and open a new terminal. Check for the gap with `grep -c _tmux_host_for_{{SOLET_NAME}} <clone>/client/{{SOLET_NAME}}-fleet.zsh`; `0` means the old launcher.
- `--remote-control "$role"` makes the session addressable by Claude Code's remote-control feature from another session — what lets a coordinator drive or monitor workers. A Claude Code feature; no MCP involved.
- `--dangerously-skip-permissions` is opt-in and separately gated in the template (an env var the operator sets, not a hardcoded flag). When enabled, it selects the same effective `bypassPermissions` mode as `--permission-mode bypassPermissions`; on installed Claude Code 2.1.243 the two spellings are behaviorally equivalent. Neither spelling retains a permission classifier that the other removes — the background classifier belongs to `auto` mode, not bypass. The fleet flag still matters because the fleet launcher otherwise inherits this machine's resolved permission mode, while the Step 2 single-session launcher explicitly defaults to bypass; enabling the flag may therefore change a fleet session from its inherited mode to bypass, but it is never “more permissive than bypassPermissions.” If a session already in effective `bypassPermissions` is denied, do not switch spellings and call that a repair. Establish the effective mode, capture the exact independent denial, and classify the actual boundary (hook, managed policy, tool availability, OS/sandbox, credential, service, or another evidenced cause). For permission-mode facts, use this evidence order: installed-binary behavior first, current vendor documentation second, and this knowledge-base text last. Do not default the fleet opt-in on.
- Peer messaging, role binding, AND idle wake come from each session's watcher (armed by the SessionStart hook → rename skill) plus the user-scope `<name> wake` Stop hook — not from any launcher flag — on the default transport. The template's `{{SOLET_NAME}}_FLEET_TRANSPORT` knob (default `watch`) declares the transport per Step 4a's offer paragraph; setting it to `mcp` re-points the rename skill to the MCP bridge and disarms the wake Stop hook via its guard. The optional MCP development-channel flag (`{{SOLET_NAME}}_FLEET_MCP_CHANNELS=1`) is the matching wake half and exists in the template gated OFF; the flag alone is inert (it also needs a registered MCP server via `claude mcp add` AND Anthropic-direct auth — unusable on Bedrock, impossible on an MCP-blocked machine), and it is only for an operator who explicitly asks for MCP and whose policy permits it — never suggest it unprompted.

**Git safety when more than one session shares this deployment directory — a nameable gate that hydration arms, and whose ONLY exemption is leaving it unset.** If the operator is choosing more than one role, ask directly: will more than one of these sessions ever run git commands (commit, push, checkout, stash, branch) against the SAME directory? If yes, concurrent mutating git from several sessions is a real collision risk — a stash or checkout from one session can silently clobber another session's in-progress work. The gate for exactly this ships in the `coordination-hooks` plugin Step 2 installs (`hooks/git_controller_gate.py`, registered on `PreToolUse`); the operator names the controller themselves: designate ONE role — **any name they choose** (`Git-Controller`, `gitops`, whatever) — as the sole git-mutator, and every other session is blocked from mutating git.

**How it arms, and who writes that (RULED 2026-08-01, operator — this REVERSES the previous ship-unarmed position).** The gate reads exactly one variable, **`GIT_CONTROLLER_NAME`**, and its **presence is the arming boundary**. You — the configuring session — set it during hydration by rendering `{{GIT_CONTROLLER_NAME}}` into **all three** coordination launchers (`claude_launcher.template`, `codex_launcher.template`, and `fleet_functions.zsh.template`). Two consequences worth stating to the operator in these words:

- ⚠ **`GIT_CONTROLLER_NAME` is the name the SHIPPED gate reads.** The platform ORIGIN repository's own private copy (`.claude/hooks/git_controller_gate.py`) reads `SOLET_GIT_CONTROLLER_NAME` and **never ships** — a deliberate per-copy binding. Setting the origin's name on a seed-born deployment arms **nothing**, and because the gate is **fail-OPEN when its variable is unset**, the result is a gate the operator believes is armed and which silently allows everything. Do not carry the origin name into this conversation.
- ⚠ **There is no project-scope fallback on a seed-born deployment.** The seed's copy allowlist is `ananta/`, `plugins/`, `disabled_plugins/`, `root_manifest.yaml` — `.claude/` is not in it, so the plugin copy is the *only* copy of this gate a deployment has. Do not tell the operator that protection also "travels with the directory"; here it does not.

**SOLO EXEMPTION — the mechanism is the variable's ABSENCE.** A deployment where only one session ever runs simply omits the `GIT_CONTROLLER_NAME=` line from all three launchers: nothing is armed, day-one setup is unobstructed, and there is zero runtime machinery to go wrong. **Do not add a session-count probe to "implement" this** — configuration already implements it, and every runtime session-count source available here is known to lie. A *transiently*-solo fleet (a fleet deployment that happens to have one session up right now) is handled by policy language, not by deleting the export: the session claiming soloness cites a checkable basis for it (a just-run peer listing, or an operator statement), which makes the claim auditable afterwards.

**The policy is a default, not an absolute.** An operator instruction to do it anyway overrides the gate — that is a command, not a failure, and it does not need a mechanism.

**Scope, stated plainly rather than implied — this is a FLEET-CHECKOUT control.** It arms exactly the sessions launched by this deployment's launchers, which are the sessions that operate in the shared directory the gate exists to protect. Sessions the operator starts in their own work repositories are **out of scope by design**: their git safety belongs to Claude Code's own permission model, not to this gate. Say that, rather than leaving the operator to infer machine-wide protection this does not provide.

**State the tradeoff up front, plainly:** an armed gate BLOCKS the Task tool (Anthropic subagents) for every non-controller session — **and that block is the gate's PRIMARY PURPOSE, not a side effect.** Those subagents spawn their own git worktrees and run git, which is exactly how in-progress work gets lost. So the choice is honest and either-way: an operator running several git-mutating sessions against one directory names a controller and gives up subagents; an operator who wants subagents leaves the variable unset and works some other way (a separate directory per session, or a single git-mutating session). Do NOT skip this conversation when more than one session will share the directory.

**Then prove ONE real block before telling the operator it is on.** Because the gate is fail-open when unset, an unarmed gate and a correctly-disarmed one produce the identical observable: nothing happens. From a session launched under a NON-controller role, attempt a gated git mutation and confirm it is refused with the gate's policy message. Use a **throwaway repository** as the target (`git init` a scratch directory, then `git commit --allow-empty -m probe` inside it) rather than the operator's real work: the gate's decision is read from the *command shape and the session's role*, with **no repository-path scoping at all** (verified at source in `hooks/_git_policy.py`), so a scratch target proves the same block while an allowed command harms nothing. "No error appeared" is not evidence of protection.

**Operational guidance: leave sessions running, use `/clear`.** Once a role's session is started, tell the operator to leave it running in its own iTerm2 (or other terminal) tab rather than closing it between conversations. Quitting a session loses its process and its role's live identity until it is manually relaunched; the `/clear` slash command instead resets the conversation while the process (and its stable `AGENT_SESSION_ID`) keeps running — the coordination-hooks plugin's `SessionStart` matcher already covers `clear` (`hooks/hooks.json`, `startup|resume|clear`), so the role-binding reminder fires automatically and the session is addressable again within moments. This is the operational rhythm this platform runs on: long-lived tabs, `/clear` between tasks, restart only when a session is genuinely stuck.

## Step 4a-ii — programmatically spawned workers (new in `coordination-hooks` `0.5.0`)

Everything above in Step 4a covers sessions an operator starts by hand. A coordinator role can also spawn worker sessions directly (`plugin::agent_messaging_plugin::spawn_session` — see the maintenance-verbs joseki cards referenced in the seed update runbook). This is a genuinely different delivery path from the plugin's own `hooks.json`, worth understanding before running any fleet where a session spawns others.

**What ships where.** `coordination-hooks`'s static `hooks/hooks.json` is unchanged in shape by this release — the same nine hook entries across five events an interactively-launched fleet session gets via its own installed plugin copy (Step 2 above). A **programmatically spawned** worker gets none of that from `hooks.json` at all. Instead, the host adapter that spawns it (`agent_messaging_plugin`'s `headless_adapter.py` or `tmux_adapter.py`) builds a **generated Claude Code `--settings` blob for that one spawn**, scoped to that worker alone, wiring eight hook files across `PreToolUse`/`SessionStart`/`UserPromptSubmit`/`PostToolUse`/`Stop` — `headless_tool_allowlist_gate.py`, `capture_session_mapping.py`, `heartbeat_report_alive.py`, `rotation_due_watch.py`, `wake_waiter.py`, `check_messages_reminder.py`, `step_zero_reminder.py`, `role_binding_reminder.py` — plus a `permissions.deny: ["Agent", "Task"]` rule enforcing the Claude-Task-tool-forbidden invariant for the spawn itself. None of these eight are registered in `hooks.json` for this purpose; a spawned worker also runs with `--setting-sources project`, which deliberately excludes both user-scope and local-scope settings, so it does not inherit the operator's own permissive defaults or the seat's copy of these same hooks either — re-wiring them per spawn is what keeps a headless worker's wake, reminders, and heartbeat working without double-firing them for the launching seat.

**How a spawned worker resolves those eight files — the two-rung ladder, and the bug it closes.** A born clone ships **no `.claude/hooks/` directory at all** (it is this dev checkout's own; it is not in the seed's copy allowlist). Before this release, both adapters built every worker-hook path as a bare `<cwd>/.claude/hooks/<file>` with no existence check — on a born clone this generated a `--settings` blob whose `PreToolUse` entry pointed at a file that did not exist, and Claude Code treats a `python3 <missing file>` `PreToolUse` failure as a blocking error: every one of that spawned worker's tool calls was refused from its first turn, with no local signal to the operator why. The fix is a two-rung resolution, run once per file at spawn time: **rung 1** — this checkout's own `.claude/hooks/<file>`, when present (every dev checkout, unaffected by this fix); **rung 2** — this plugin's own shipped `hooks/<file>`, the fallback every clone carries even with no `.claude/hooks/` at all, read from the checkout copy of the plugin tree directly rather than the version-keyed installed plugin cache (the same cache-staleness trap Step 6 below documents — a checkout copy can't go stale the way an installed cache copy can). **If neither rung resolves, the spawn itself is refused loudly** (`WorkerHookResolutionError`, surfaced to the caller as `HostCannotSpawnError`) rather than starting a worker that will silently fail its first tool call — a clear, attributable refusal instead of an unexplained hang.

**Zero-risk setup + verify, for the session doing the deploying:**
1. Run this plugin's own test suite green before trusting any of this (`tests/run_all.py` under the installed/checkout `coordination-hooks` path) — both new hooks and the resolution ladder ship with dedicated smokes; confirm they exist in your own install, not just the source tree.
2. **The fail-closed/fail-open contracts, stated plainly.** `headless_tool_allowlist_gate.py` ships UNARMED — it only enforces when the spawning call supplied an explicit `allowed_tools` list (`FLEET_HEADLESS_TOOL_ALLOWLIST` set, even to an empty string); an ordinary spawn is unaffected. Once armed, it is the one hook in this stack that is FAIL-CLOSED — any parse or exception path also blocks (`exit 2`), because this is the actual safety boundary for an unattended worker with no human present to catch a hook bug. `capture_session_mapping.py` is non-fatal in every failure mode (missing env var, bad stdin, unwritable spool dir all warn and exit `0`) and writes only to the one spool directory its adapter declared — never anywhere else, never a credential.
3. **Bounded post-update verification.** Reuse Step 6's cache-copy hooks diff below, scoped to `hooks/`, to confirm this plugin's shipped fallback copy (rung 2) actually reached the installed cache. Then spawn one worker with a minimal `allowed_tools` list and confirm a call outside that list is refused with the gate's clean stderr reason — not a hang, not a silent allow. "The worker started fine" is not evidence the resolution ladder or the allowlist gate are actually working — prove one real block, the same discipline the git-controller-gate paragraph above already uses for the fleet-wide git safety gate.

## Step 4b — configure the export/workspace root (required before any business-connector activation)

**Do this before Step 4c.** Business-connector reads (Jira, Salesforce, Snowflake, Postgres,
and similar) never return record-level data inline — results are exported to a file the operator
supplies, and every business-connector verb refuses outright until a workspace root is
configured (an empty allow-list is the secure default: no export destination, no read).
Skipping this step does not mean "business connectors work with a smaller safety margin" —
it means every one of them fails loud on first use, for an operator who was never told why.
Measured 2026-07-29 (the architect ruling on business-connector data boundaries, filed in
this checkout's `workbench/` directory under that date; §3).

**Two directories, asked separately, never conflated:**

1. *Where does the solet itself live?* — `app_home` (`<clone>/profile`). Already fixed;
   nothing to ask here.
2. *Where do you keep the projects you work in?* — the export root. Ask in plain words: "Where
   do you keep the folders you work in day to day — the parent directory, not any one project?"
   (a `~/Workspace`-style directory: stable, singular, and it covers every future job folder
   without per-job reconfiguration). Do not accept a specific project folder as the answer — it
   will pass today and fail on the operator's very next job.

**Validate, don't just record.** The natural answer to question 2 can accidentally contain
`app_home` (a `~/Workspace`-style root nested one level above this very clone is the *default*
case on a developer's own machine, not an edge case) — admitting it as an export root would let
exports land back inside the platform's own managed tree, the opposite of what this closes. Call
the real validator rather than hand-rolling the check:

```bash
<clone>/.venv/bin/python3 -c "
from pathlib import Path
from github_midwife_plugin.export_root_validation import configure_export_root
written = configure_export_root(Path('<clone>'), '<clone>/profile', '<operator's answer>')
print(written)
"
```

`configure_export_root` rejects a root that is, contains, or is contained by `app_home` (naming
which direction failed), and otherwise persists the validated root into `export_allowed_roots`
for every business-connector plugin actually installed in this clone — additive and idempotent,
so re-running it (a second job-folder root added later, or simply re-running this step) never
duplicates or clobbers an already-configured root. Show the operator the rejection message
verbatim if it fires; do not silently retry with a different guess.

## Step 4c — activate shipped plugins; configure credentials on first use

Shipped profile plugins should be installed and manifest-active now. Do not pitch each plugin as an optional birth-time decision. The operator already chose the seed/profile; hydration's job is to make that selected profile usable.

Some plugins still need real operator-specific setup before first useful invocation: a Jira site URL, a Snowflake account, a Salesforce connected app, Google OAuth, session-history access, and similar tenant/credential facts. Those are first-use configuration flows. The correct behavior is install-all-now, configure-on-first-use.

**Discover.** Glob `<clone>/plugins/*/knowledge_base/hydration_guidance.md` directly on the filesystem — NOT a knowledge-base search. A plugin can be present but not yet useful without credentials; a raw filesystem glob over the actual clone finds every guidance file that exists in it right now, regardless of manifest/ingestion state.

**Record the first-use contract.** For each guidance file found, read its `## Setup` section and keep the setup trigger attached to that plugin. Do not interrupt birth to collect every possible credential. When the operator first asks to use that plugin, follow its setup steps then, with the operator present and the concrete target known.

**Safe activation now.** If a guidance file contains non-secret, local-only activation work required for the shipped plugin to register or start, do it now after probing state and showing the exact action. If it asks for external tenant identity, OAuth, API tokens, file-system consent beyond the current clone, or any credential, defer to first use.

**Authoring convention, for the next plugin that needs this:** a `hydration_guidance.md` is a normal KB article (proper `Article Layer`/`Article Role: hydration_guidance`/`Article Tags`/`Embedding Description` header, so it is also findable by normal KB search once the plugin is active). Keep `## Setup` concrete enough that a first-use agent can run it without rediscovering the plugin's credential model.

## Step 4d — session-ledger ingestion (core capability, consent-gated — not a connector)

Do this as its own step. Ledger ingestion is NOT one of Step 4c's
configure-on-first-use connectors, and treating it as one is the most common
deployment gap measured in the field: nothing visibly breaks without it, so
newborn deployments have skipped it as "optional" and silently lost
cross-session memory — no prior-session search, no "what did we decide last
week", no history-grounded answers. The platform's stance is settled: ledger
functionality is core platform correctness, not a privacy knob (operator
ruling 2026-08-02, quoted in full in the ingestion disclosure section below).

Consent is still required — ingestion reads the operator's own transcript
files, so this step runs as an offer with an explicit yes, like everything
else in this ladder. The setup procedures live with the session-source
plugins themselves — one `hydration_guidance.md` inside each, at
`plugins/*_filesystem_session_source_plugin/knowledge_base/hydration_guidance.md`:
the Claude Code plugin for Claude Code transcripts, the Codex plugin where the
operator also uses Codex. Cover EVERY coding agent the operator actually uses,
not just the one driving this hydration.

That path is written as a PATTERN rather than spelled out because **which
session-source plugins your seed carries depends on its capability bundle** —
this article is birth-spine and ships in every profile, while those plugins do
not. List the directory to see which you have; a seed provisioned without any
session source has no such file, and this whole step does not apply to it.

Verify by retrieval, not by registration: after the initial backfill, a
`session_ledger_service::search_event_content` query for a topic from a real
prior session must return content. `list_sources` showing rows proves
registration; only a successful search proves ingestion.

On decline: respect it — no partial wiring — and record the decline. The
deployment report card (`08_deployment_report_card.md`) carries this as an
unconfigured CORE row on every future card, with what it costs stated
plainly; it is re-offered at natural moments, never silently reclassified as
optional.

## Step 5 — optional MCP bridge, ONLY on explicit operator request

Skip this step by default — it is not part of baseline hydration on any
machine. Nothing requires MCP: process calls, knowledge-base search,
session-history ingestion, role claiming, and message receive all run through
the `<name>` command (`<name> call`, `<name> watch`). If you notice the
environment permits MCP, you may mention once that an optional MCP bridge
exists for tool-native access, then move on; act only if the operator
explicitly asks for it. When asked: follow the seed README's registration
ladder. Reference form: `claude mcp add --scope user -e SOLET_NAME=<name>
-e AGENT_IDENTITY=claude_code <name> --
<clone>/.venv/bin/python3 -m agent_messaging_plugin.mcp_bridge`. Probe
`claude mcp add --help` before trusting that form; CLI flags drift.

## Step 6 — verify

Verify each of the following before declaring hydration complete.

### Checklist: is the newborn solet actually alive?

First, that the solet itself is alive (not just reachable):

- `launchctl list | grep <label>` shows the SAME process id across two checks a minute apart — a changing id means a crash-loop, not a running solet.
- The newest log under `<clone>/profile/data/logs/` is quiescent: `wc -l <logfile> && sleep 5 && wc -l <logfile>` prints equal counts.
- Optional: the log names the bridge port it bound (allocated dynamically at start; also in `~/.ananta/runtime/<name>.bridge.port`); `curl -s http://127.0.0.1:<port>/api/v1/bridge/health` answers.

Then, that the no-MCP command and session tooling work:

- `<name> health` answers.
- `<name> call service_interface::knowledge_service::search '{"query": "hydration runbook", "top_k": 3}'` returns a successful action/result path.
- A fresh session started via `claude-<name>` carries the intended label and can use the `<name>` command without a venv activation.
- `CLAUDE.md` and `AGENTS.md` each contain the `BEGIN SOLET HYDRATION` / `END SOLET HYDRATION` block, including the no-MCP Step Zero command, the access-mode contract (no-MCP CLI default, MCP by explicit operator request only, source-artifact recovery when the runtime is unavailable), the implementation/debugging KB-first rule, and the pointer to the router-vs-bridge distinction.
- `~/.claude/settings.json` parses as JSON and contains exactly one `extraKnownMarketplaces` entry keyed to the rendered marketplace name (pointing at `<clone>` as a `directory` source) and exactly one `enabledPlugins.coordination-hooks@<marketplace-name>: true` entry — the session hooks moved into that plugin (Step 2), so this file no longer carries them and its own `hooks/hooks.json` roster is what determines which hooks execute, not a literal env-var-keyed command string here; `~/.claude/skills/rename/SKILL.md` and `~/.claude/skills/feedback/SKILL.md` both exist.
- `gh --version` answers, unless the operator declined the Step 2 GitHub CLI offer — the `/feedback` skill files non-interactively with `gh issue create --body-file`, and a recorded decline is the only passing state other than presence (without `gh` the skill saves the round for the operator to submit in a browser).
- `<clone>/client/claude-session-overlay.json` exists, parses as JSON, and contains exactly the `AskUserQuestion` permissions deny; the rendered `claude-<name>` launcher carries the `--settings` overlay flag gated on `SOLET_ALLOW_ASKUSERQUESTION` — both halves, because an overlay file without the flag (or the reverse) is the prohibition in name only.
- **The machine carries no managed policy that voids the deny.** Check the four canonical locations, remote-pulled first — on a machine that has that one, the system paths may not exist at all, so checking only the system paths reports "no managed policy" on exactly the deployments where one is in force:
  ```bash
  for f in ~/.claude/remote-settings.json \
           "/Library/Application Support/ClaudeCode/managed-settings.json" \
           /etc/claude-code/managed-settings.json; do
    [ -f "$f" ] && echo "== $f" && grep -o 'allowManagedPermissionRulesOnly[^,}]*' "$f"
  done
  ```
  No output means no managed policy is in force and the deny is live. `allowManagedPermissionRulesOnly: true` in the output means **this deny, and every other `permissions.deny` in the deployment, is inert on this machine** — record it and tell the user, because there is no local signal at run time and the only place a deny is honoured is that file's own `permissions.deny`, which their organisation administers rather than they do. This is a passing state for hydration: nothing is misconfigured, the mechanism simply does not apply here, and a discovered-later version of this fact costs an adopter a support round (§45.1 is exactly that round).
- `~/.claude/plugins/installed_plugins.json` names `coordination-hooks@<marketplace-name>` with an `installPath` that **exists on disk** — `ls` it, do not stop at the JSON parsing. A present JSON entry with a missing path is the exact broken-but-registered state the install step's CLI commands exist to prevent; a JSON-only check would report this as passing.
- The UserPromptSubmit hook in that session points the agent at `<name> call`, not MCP.
- A bare `claude` session in an unrelated directory starts with no hook errors and no injected solet context (the label guard covers both hooks).
- A LABELED session started in a different repo (not the clone) DOES receive the Step Zero and role-reclaim context — this is the fleet case the user-scope ruling exists for.
- In a labeled session, the rename skill arms `<name> watch` and its first output line is `"watch": "armed"` with a `claimed`/`updated`/`displaced` result — role-addressed receive is live with zero MCP.
- Role-claim ground truth: `<name> call plugin::agent_messaging_plugin::peer_holds_role` with the role name and the watcher's `agent_instance_id` resolves the claim to this session. An entry in raw peer-list output is connection presence, not a claim — never verify registration from a peer list.
- Only if the operator explicitly requested MCP in Step 5: `claude mcp list` shows `<name>` and a fresh session can call one `mcp__<name>__*` tool. Absence of MCP is not a finding.

Then, that session-ledger ingestion (Step 4d) actually ingests, for every
coding agent the operator uses:

- `<name> call service_interface::session_ledger_service::list_sources '{}'` shows registered rows for each agent in use (Claude Code and, where used, Codex).
- A `search_event_content` query about a topic from a real prior session returns that content — registration without retrieval is not a pass.

**Close out by delivering the deployment report card.** Step 6's checks verify
the deployment for YOU; the report card (`08_deployment_report_card.md`)
converts them into something the OPERATOR sees and keeps: what is configured,
what remains, what each remaining item would give them, and one recommended
next step. A hydration that ends without delivering the card has not finished —
the card is what keeps consent-gated or deferred components (the ledger above,
fleet coordination, tmux worker hosting) visible instead of silently optional.

## Your blue-green router (auto-installed at birth)

If your solet runs a **blue-green-capable profile** (its plugin allowlist includes `macos_self_deployment_plugin` — the `macos-bizops` tier), genesis **automatically installs a per-solet blue-green router** as the last birth step, right after the main autostart LaunchAgent. No operator action is needed: the router picks a free port in 8800-8999, writes its `<name>.router.port` + `<name>.bridge.port` discovery files, and loads under the label `local.solet.<name>.router`. That router is what lets the solet adopt new code with **zero downtime** via `apply_manifest` (blue-green swap). It is a separate KeepAlive LaunchAgent by design — it runs independently of Ananta and survives swaps untouched.

**Free-tier solets** (the `macos_free_minimal` profile, no self-deployment plugin) are **single-color by design** and have no router; their update path is a plain restart, not a blue-green swap. Genesis records the router step as `skipped` for them — that is expected, not a failure.

**One-line repair** if a capable solet ever loses its router (crash that KeepAlive didn't restore, manual removal): re-run the idempotent installer from the clone —

```
.venv/bin/python3 plugins/macos_self_deployment_plugin/src/macos_self_deployment_plugin/blue_green_router/install_router.py <name>
```

It is a no-op success when the router is already healthy. `uninstall_router.py <name>` is the symmetric teardown.

## Your git worktree (auto-created at birth)

A seed never ships `.git` — the "no contaminated history travels" invariant is exactly why `.git/` is `never_copy` in the seed manifest — so a freshly-hydrated seed clone arrives as a plain source tree, **not** a git worktree. Genesis fixes this as its final step: it `git init`s the born tree with a **fresh, empty history of its own** (never the minting solet's `.git`), writes a sensible `.gitignore`, sets a **local** git identity (repo config only — your global git config is never touched), and makes one initial commit. No operator action is needed.

This is what lets `platform_dev_surface_plugin` come ready (its readiness probe runs `git rev-parse --is-inside-work-tree`, which a plain source tree fails) and makes any git-based workflow in the clone work from first boot. The initial local identity (`<name>` / `<name>@localhost`) is a placeholder you are free to change with `git config`.

The step is **idempotent, and the two halves are idempotent differently** — worth stating precisely, because the imprecise version of this paragraph hid a real defect for several releases:

- **History is conditional.** A tree that is already a worktree keeps its history untouched; genesis reports `status="skipped"` and never runs `git init`, `git add`, or `git commit`. So if you cloned the seed from a GitHub repo (which arrives WITH a `.git`), your existing history is left strictly alone.
- **The `.gitignore` is unconditional.** It is written whenever the file is ABSENT, in either shape of tree — including the already-a-worktree case. An existing `.gitignore` is still never clobbered, so your own edits survive.

That second point is the correction. The `.gitignore` write used to sit behind the already-a-worktree check, which meant a clone from GitHub — the normal way to take a seed, and therefore every adopter — got **no `.gitignore` at all**. The visible symptom is a clone where `git status` is full of `__pycache__` and the whole `.venv`; the real risk is that runtime state and secrets the ignore list exists to exclude are one `git add -A` away from being committed. Reported by an external adopter and fixed 2026-08-08.

One consequence to expect on a cloned seed: because genesis must not touch your history, a `.gitignore` written into an existing worktree is left **untracked** rather than committed. Commit it yourself when convenient.

## What this solet ingests and embeds

In one sentence: what your solet captures and embeds from your sessions is
everything — the full transcript of every coding-agent session it ingests, plus every
peer-coordination message, embedded in full fidelity with no filtering and no opt-out.
The rest of this section discloses exactly how.

### No opt-out, no redaction: everything a session says gets captured and embedded

**Session ingestion is full-fidelity by design — a deliberate platform property, not a
gap awaiting a fix.** The operator has ruled directly on this point (2026-08-02),
verbatim: "I'm more concerned with transcript ingestion working and the ledger being
fully functional than anything else. This is super critical. This idea of opting out or
redacting, I don't understand what it's about, and I don't like it." There is no
per-session exclusion or content-redaction mechanism, and none is planned — ingestion
completeness and ledger functionality are treated as core platform correctness, not as
an optional privacy knob. What follows discloses how that ingestion actually works, so an
operator deciding whether to run business-connector work through this solet knows
what it means before they do. Measured 2026-07-29 (the architect ruling on
business-connector data boundaries, filed in this checkout's `workbench/` directory under
that date; §2, §4(a)).

**The control for record-level business data sits on the caller's side, not the ledger's.**
Once content reaches a session (a business-connector read, a script's output, a peer
message), it will be ingested — that is what "fully functional" means above. The way to
keep record-level values from accumulating in a durable, embedded, searchable store is to
keep them from reaching a session in the first place, or to follow a values-stay-staged
convention for anything that does. See
`24_operator_communication/08_business_record_classification_convention.md` for the
platform's adopted convention on that half.

### Two ingestion routes: session transcripts and peer messaging both get embedded into the knowledge base

**Two independent ingest routes exist, and disclosing only one is a false assurance:**

- **Transcript tailing.** `claude_code_filesystem_session_source_plugin` and its Codex
  sibling tail the agent's own transcript file on disk
  (`.claude/projects/<encoded_cwd>/<session_id>.jsonl`) once its root is registered under
  `ledger_allowed_roots` — the authorization rules for that setting are an origin-side
  platform reference that does not ship in a seed clone; search the knowledge base for
  the ledger's allowed-roots authorization if you need the detail on a deployment.
- **Direct table ingestion — yes, peer messaging gets ingested into the knowledge base
  too.** `agent_messaging_session_source_plugin` reads the platform's own
  `core__agent_thread` / `core__agent_message` tables directly — peer coordination
  messages are ingested and embedded without ever passing through a transcript file. An
  operator who reads only about transcript files will believe peer traffic is out of
  scope. It is not.

**What's captured is event-level content, not a summary.** The session ledger runs a
periodic event-level embedding drain (cron `ledger:periodic_embed`; the registered
verb `service_interface::session_ledger_service::drain_event_embeddings` runs the same
job on demand) — individual transcript and message content is embedded and becomes
searchable via knowledge-base search, not merely a rolled-up digest.

**Where the embedding is sent depends on your bind.** The shipped Apple-native
profiles' default binds a **local** embedding endpoint (`coreai_embeddings_plugin`,
a pinned Nomic v1.5 model served on-host through Apple's Core AI on macOS 27 Apple
Silicon). `openai_embeddings_plugin` — OpenAI-*compatible*, not OpenAI; typically an
LM Studio + nomic model on this machine, taking no API key — remains an
optional/legacy provider, still what the `local` profile template and a manual
`bootstrap.py` genesis provision. A macos-bizops solet created on macOS 26 binds
`openai_embeddings_plugin` to its own loopback llama.cpp server, which serves the
same pinned Nomic v1.5 model (the Homebrew install troubleshooting runbook's
macOS 26 section). On any of these local defaults, ingested content never
leaves the machine. Re-binding to a cloud embedding provider (for example
`titanv2_embeddings_plugin`) sends ingested content — including anything a business
connector has already read into a session, and peer message bodies regardless of
what any verb returns — to that provider. Check your own bind before assuming either
way; this runbook states the default, not your deployment.

**Do not rely on a hand-listed source count here — it drifts.** The set of local
session-source plugins a solet ships changes release to release, and a static list
in this document would go stale the first time it did. To see exactly which sources
THIS install is actually ingesting from, run
`<name> call service_interface::session_ledger_service::list_sources` — it joins
loaded-plugin descriptors against registered database rows and answers "which sources is
this solet actually ingesting from" without raw SQL. Re-run it after enabling or
disabling any plugin; do not carry forward a count from a previous session or from this
document.

## What the user should expect afterward

With a session's watcher armed (the rename skill does this at session start), peer and role-addressed messages stream into the watch task's output and surface when the session next looks at it — not as live interruptions. Without a watcher, messages queue durably and drain when one next arms. This is designed behavior, not a defect; the generated CLAUDE.md states the same expectation to the user directly.

Senders see it named honestly: every send to a watcher-held session is delivery-attempted unconditionally (A4, 2026-08-04 — there is no sender-declared marker to opt into a wake) and returns `delivery="queued_watcher"` — delivered into the watch output, not proof of a turn. Claude's re-wake hook may convert an idle delivery into a fresh turn. Codex currently ships no Stop-hook delivery mechanism at all (codex-0147-async-hook-regression, 2026-08-13: stock Codex does not execute async command hooks on any measured build) — an unmanaged Codex session drains the delivery on its own next user turn, while a managed `spawn_session` Codex worker is independently driven through its host channel regardless. The watcher's own event-stream ack marks the message consumed platform-side. There is no per-message escalation apparatus watching for a missed wake anymore; staleness is caught at the session level instead, through the recipient's own `report_by` promise (`sweep_overdue_sessions` / `_notify_steward_of_overdue`) — if you suspect a watcher is dead or its output is never being read, re-arm it (`/rename <Role>` or a fresh `<name> watch`) and resend rather than wait on an automatic re-queue. On a harness without hook support, a plain background `<name> watch` task still receives everything; deliveries then wait for the next look.

## Reference

- `bootstrap.py` (repo root) — the canonical reference implementation of the step 0 sequence, in its `role_and_db` step.
- `plugins/github_midwife_plugin/src/github_midwife_plugin/venv_provision.py` — `verify_newborn_db_scram_gated`, the negative-auth probe the birth verb re-runs against step 0's work.
- `plugins/github_midwife_plugin/knowledge_base/hydration_templates/TEMPLATE_VARS.md` — render tokens, file map, and the pending env-var rename register.
- The platform deployment pattern hydration instantiates: in-clone package, agent-installed, zero secrets. **Origin-side reference — the platform knowledge-base section holding it does not ship in a seed clone**, so this is background for someone working in the originating checkout, not something to look up on a deployment.
- The birth runbook whose genesis this runbook follows. Same caveat: origin-side only, not present in a seed clone.
- `plugin::github_midwife_plugin::birth_solet` — the genesis verb that precedes hydration.
- `service_interface::knowledge_service::search` — the Step Zero search the generated `CLAUDE.md` and `AGENTS.md` both instruct the newborn's driving agent to run through `<name> call`.
- `<name> watch` (`plugins/agent_messaging_plugin/src/agent_messaging_plugin/local_cli/cli.py`) — the registered-presence watcher the generated rename skill arms: register, claim, drain, stream, reconnect.
- `plugin::agent_messaging_plugin::peer_claim_role` — the role-claim process the watcher dispatches over its registered bridge.
- `plugins/github_midwife_plugin/knowledge_base/08_deployment_report_card.md` — the fully-deployed-state roster and the operator-facing card Step 6 closes out with.
- `plugins/github_midwife_plugin/knowledge_base/07_upstream_feedback_runbook.md` — how this deployment reports defects and requests features upstream once it is running.
