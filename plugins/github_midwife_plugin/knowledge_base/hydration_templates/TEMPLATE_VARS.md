# Hydration templates — render contract + rename register

**NOT a KB article.** This directory is excluded from knowledge-base ingestion
(`content.patterns.exclude` in `../manifest.yaml`) — these files are render
sources the driving agent instantiates at genesis time, per the hydration
runbook
(`plugins/github_midwife_plugin/knowledge_base/01_hydration_runbook.md`).

## Placeholder convention (seam contract with the SF-C seed-README generator)

- `{{SOLET_NAME}}` style double-brace tokens are the **machine-render
  tokens**. Substitute by **literal string replacement** — never Python
  `str.format` (it treats `{{` as an escaped brace) and never a shell
  heredoc pass (templates contain live `$VAR` shell references that must
  survive verbatim).
- `<name>`-style angle brackets are **human-instruction prose only** (the
  genesis README ladder convention). They are never render targets.

| Token | Value at render time |
|---|---|
| `{{SOLET_NAME}}` | the newborn's validated name (`^[a-z][a-z0-9_-]{1,62}$`) |
| `{{CLONE_DIR}}` | absolute path of the newborn clone (no trailing slash) |
| `{{CLONE_DIR_ZSH}}` | the same absolute clone path rendered as one single-quoted zsh word; shell templates must use this token, never interpolate `{{CLONE_DIR}}` inside shell quotes |
| `{{HYDRATION_DATE}}` | `YYYY-MM-DD` of the hydration run |
| `{{BACKUP_PATH}}` | the collision-free .zshrc backup path chosen in the runbook's offer step (only in `zshrc.template`) |
| `{{MARKETPLACE_NAME}}` | the Claude Code and Codex marketplace name, **DERIVED from `{{SOLET_NAME}}`** by one documented transform — underscores become hyphens (`acme_corp` → `acme-corp`) — reported at hydration output and recorded in the hydration record, never silent (operator ruling 2026-08-01: *"the name of the seed is the name of the seed… built off a user input — use that"*). Every other kebab-case violation, and the 16 reserved Claude marketplace names, **REFUSE LOUDLY** naming the rule — a silent skip would register no marketplace and the plugin would simply be absent, with no error anywhere. ⚠ The `_`→`-` transform is **non-injective** (`a_b` and `a-b` both derive `a-b`), so hydration also refuses when the derived name is already registered to a different source. Verification legs derive the expected enabled/installed plugin selector from the rendered name, never pin a literal |
| `{{SHELL_FILE_ZSH}}` | the absolute path of `<clone>/client/<name>.zsh` rendered as one single-quoted zsh word (only in `zshrc_block.template`); the render is byte-identical to the unversioned block genesis wrote before managed blocks carried a template digest, which is what lets an existing-install update recognise a legacy block as its own previous render |
| `{{RUNTIME_DIR}}` | `~/.ananta/runtime/<name>`, the LaunchAgent's `WorkingDirectory` (only in `launchagent.plist.template`; XML-escaped by the renderer, never interpolated raw) |
| `{{PATH_ENV}}` | the fixed Homebrew-inclusive `PATH` literal the daemon needs (`autostart._PATH_ENV`; only in `launchagent.plist.template`; XML-escaped by the renderer) |
| `{{GIT_CONTROLLER_NAME}}` | the role name the operator chose as sole git-mutator in the runbook's git-safety conversation (Step 4a), rendered into **all generated coordination launcher templates** — this is the *only* token whose correct render is sometimes **no line at all**: a solo deployment DELETES the whole `GIT_CONTROLLER_NAME=…` line, and that absence IS the exemption (see the `GIT_CONTROLLER_NAME` row below). Never invent a default; a name nobody chose arms a gate against a role that does not exist and blocks every session |

## Per-template notes

⚠ **This is NOT the roster and does not claim to be complete.** The CANONICAL
list of hydration templates — membership, count, and which renders are
conditional — is the render table in `01_hydration_runbook.md`. This section
carries only the *per-template semantics* a renderer needs (why a file exists,
what its schema requires, what silently breaks). If a template is missing a row
here that is a documentation gap; if it is missing from the runbook table, that
is a hydration defect. Never derive a count from this section, and never
reconcile the runbook against it — the dependency runs one way.

| Template | Rendered to | Mode |
|---|---|---|
| `zshrc.template` | `~/.zshrc` (ONLY on accepted offer — see runbook) | 0644 |
| `solet.zsh.template` | `<clone>/client/<name>.zsh` | 0644 |
| `claude_launcher.template` | `<clone>/client/bin/claude-<name>` | 0755 |
| `claude_session_overlay.json.template` | `<clone>/client/claude-session-overlay.json` — token-free literal copy. The `AskUserQuestion` default-deny overlay the Claude launcher passes via `--settings` (settings sources merge, so the deny unions in without touching the user's own files). The launcher skips the flag entirely under `SOLET_ALLOW_ASKUSERQUESTION=1` — that env var is the whole override mechanism, so this file's content stays a bare permissions deny; adding other keys here would smuggle un-overridable configuration into every session | 0644 |
| `codex_launcher.template` | `<clone>/client/bin/codex-<name>` | 0755 |
| `launch.template` | `<clone>/client/bin/launch-<name>` | 0755 |
| `CLAUDE.md.template` | `<clone>/CLAUDE.md` | 0644 |
| `AGENTS.md.template` | `<clone>/AGENTS.md` | 0644 |
| `claude_settings.json.template` | `~/.claude/settings.json` (USER scope, structural merge — operator ruling 2026-07-22: fleet sessions start in other repos, and project-scope hooks never fire there) | 0644 |
| `rename_skill_SKILL.md.template` | `~/.claude/skills/rename/SKILL.md` (USER scope, same ruling — the role-reclaim hook invokes it from any repo) | 0644 |
| `feedback_skill_SKILL.md.template` | `~/.claude/skills/feedback/SKILL.md` (USER scope, same ruling — feedback is filed from wherever the session noticed the defect, which is rarely the deployment directory). Renders `{{SOLET_NAME}}` in exactly one place, the knowledge-search command that points the agent at the feedback runbook; every other command in the body is either plain `git`/`gh` or deliberately unresolved (the channel comes from `git remote get-url origin` at run time, never from a rendered literal, so a moved seed home cannot strand a rendered skill) | 0644 |
| `fleet_functions.zsh.template` | `<clone>/client/<name>-fleet.zsh` (ONLY on accepted Step 4a offer — see runbook) | 0644 |
| `marketplace_json.template` | `<clone>/.claude-plugin/marketplace.json` — the plugin catalogue the user-scope `extraKnownMarketplaces` entry points at. **Rendered, not copied:** the seed `copy:` allowlist is `ananta/`, `plugins/`, `disabled_plugins/`, `root_manifest.yaml`, so a `.claude-plugin/` directory committed at the platform repo root would **not ship** — hydration generates it instead, which also keeps this separate from the platform checkout's own root `.claude-plugin/` question (D-5a.4). The `owner` is a **name only, no `email`** (operator ruling 2026-08-01) — the only identity this lane ships, and it is a name with no person attached. ⚠ **The plugin entry's `source` is a plain STRING, relative to the marketplace root (the directory holding `.claude-plugin/`, i.e. `<clone>`) — NOT an object.** Measured 2026-08-02: Claude Code 2.1.220 rejects `{"source":"directory","path":…}` in a *plugin* entry with `Stubbing unparseable marketplace plugin entry … source: Invalid input`, then `Failed to cache plugin … source type your Claude Code version does not support` — **silently, with no operator-visible error outside `--debug`**, so the plugin is simply never installed while everything downstream looks normal. Corroborated independently: in the live `claude-plugins-official` catalogue every local-directory plugin source is a bare string (`./plugins/<name>`, `./external_plugins/<name>`) and object form appears **only** for remote `git-subdir`/`url` sources. 🚨 **Do NOT propagate this fix to `claude_settings.json.template`** — for the *marketplace registration* (`extraKnownMarketplaces` / `known_marketplaces.json`) the object form `{"source":"directory","path":"<absolute>"}` is the CORRECT and measured-working shape. Two different schema positions; the same-looking change breaks the working one | 0644 |
| `codex_marketplace_json.template` | `<clone>/.agents/plugins/marketplace.json` — stock Codex's repo marketplace catalogue. It resolves the clone-relative `codex_plugin/coordination-hooks` bytes, remains `AVAILABLE` rather than silently installing itself, and requires the explicit `codex plugin marketplace add <clone>` then `codex plugin add coordination-hooks@<marketplace-name>` hydration steps. Hook execution still requires separate review in Codex's `/hooks` surface | 0644 |
| `zshrc_block.template` | the body of the `~/.zshrc` managed block; genesis and the existing-install `existing::hydration.reconcile` adapter both render it under the versioned marker `# BEGIN SOLET <name> v<template-digest8>` … `# END SOLET <name>` (existing-install design section 6.1). The marker's digest is the sha256 of this file, so a later release can tell a previous render from an operator edit; a legacy unversioned marker is recognised, never rewritten silently | 0644 |
| `launchagent.plist.template` | `~/Library/LaunchAgents/local.solet.<name>.plist`, rendered by `autostart.SimpleAutostartRenderer` at genesis and by `existing::autostart.reconcile` on update, with a `<!-- rendered-from: <template_ref>@<sha256> -->` stamp as its second line. The layout is byte-for-byte the plist the renderer used to hand-build (every field is load-bearing on the boot path); values are XML-escaped before substitution | 0644 |
| `user_claude_md_section.template` | `~/.claude/CLAUDE.md` (USER scope, **CREATE-or-merge** of one marker-delimited section) — the production instruction surface (operator ruling 2026-08-01: nobody launches production sessions from the deployment directory, so `<clone>/CLAUDE.md` reaches nobody there). Body is the operator's **ruled minimal bootstrap**: the first sentence plus the ONE action that brings up knowledge-base page zero — deliberately nothing else, because page zero is what orients the session. ⚠ The installer takes this body **from this template**, never from a literal of its own: the pinned query (`"session start orientation"`) is tuned *with* page zero and re-validated by that article's retrieval test, so a baked-in copy would drift out of retrieval silently. ⚠ The marker is **deployment-keyed AND versioned** (`<!-- BEGIN SOLET <name> v1 -->`): the key is what lets a second solet install alongside the first without clobbering it, and the version is what lets a later installer recognise an out-of-date section and re-merge rather than duplicate. This file is one the user owns wholesale — merge the section, never rewrite the file | 0644 |

## Refresh class on update

Hydration renders each template once, at birth; nothing re-renders it when the
solet updates unless the release says so. This table classifies every template
by what an update may safely do to its rendered copy. It carries one row per
template on disk, and `hydration_templates_render_smoke.py` fails if a template
has no row, a row has no template, or the `managed` rows differ from the
artifacts `existing_install_flow.json` declares.

| Class | Meaning |
|---|---|
| `managed` | Declared in `existing_install_flow.json`. `solet-manager update` re-renders it in the approved plan, with a backup, and never overwrites a copy the operator edited. |
| `refresh-safe-manual` | A pure render of tokens an update knows, with no operator input, so re-rendering by hand is safe. Not `managed`, for the reason given. |
| `operator-input-manual` | The render depends on an operator choice recorded only in the rendered file, or the file holds operator additions. Re-render by hand, carrying the choice and the additions over. |
| `user-owned` | The operator edits it or a tool merges into it. Never re-rendered wholesale. |

| Template | Class | Reason |
|---|---|---|
| `launchagent.plist.template` | `managed` | `instance_launchagent_plist` |
| `zshrc_block.template` | `managed` | `shell_startup_block`, a marker-delimited block |
| `user_claude_md_section.template` | `managed` | `user_claude_md_section`, a marker-delimited block |
| `feedback_skill_SKILL.md.template` | `managed` | `feedback_skill`, refresh-only: an edited copy is reported and left, a missing one is not created |
| `rename_skill_SKILL.md.template` | `refresh-safe-manual` | user scope, but its bytes are identical at r43, r56 and this release, so there is nothing to deliver yet |
| `solet.zsh.template` | `refresh-safe-manual` | inside the clone under `client/`; the update plan refuses in-target destinations the clone does not git-ignore, and existing clones do not ignore `client/` |
| `launch.template` | `refresh-safe-manual` | same: `client/bin/` |
| `claude_session_overlay.json.template` | `refresh-safe-manual` | same: `client/` |
| `marketplace_json.template` | `refresh-safe-manual` | same: inside the clone (`.claude-plugin/`) |
| `codex_marketplace_json.template` | `refresh-safe-manual` | same: inside the clone (`.agents/plugins/`) |
| `claude_launcher.template` | `operator-input-manual` | carries the operator's `GIT_CONTROLLER_NAME` choice, or its absence, and operator launcher edits |
| `codex_launcher.template` | `operator-input-manual` | same |
| `fleet_functions.zsh.template` | `operator-input-manual` | same choice, and the operator's role functions live below the `# One function per role` line of the rendered file |
| `CLAUDE.md.template` | `user-owned` | `<clone>/CLAUDE.md` is a preserved-never surface in the bundle contract |
| `AGENTS.md.template` | `user-owned` | same |
| `claude_settings.json.template` | `user-owned` | reference shape only; the live `~/.claude/settings.json` is a structural merge into the user's own file |
| `zshrc.template` | `user-owned` | whole-file replacement of the user's startup file, only on an accepted offer |

The exact manual re-render steps for the fleet file and for an edited feedback
skill are in the seed update runbook, Part C Step 5. The other manual rows are
re-rendered by re-running hydration Step 2, as that step describes.

This directory stays **FLAT** — the KB manifest's single exclude pattern
(`hydration_templates/*`) relies on it (`Path.match` has no recursive `**`).

**`CLAUDE.md.template` and `AGENTS.md.template` are the same knowledge-bootstrap
contract for two tools.** Both render the identical `<!-- BEGIN SOLET
HYDRATION -->` / `<!-- END SOLET HYDRATION -->` managed block, both are
merged with the same insert-or-update rule (see the runbook's Step 2), and
both state the same access-mode contract: the no-MCP `<name> call` CLI is the
default for knowledge/process/session-ledger access, MCP is opt-in only on
explicit operator request, and source-artifact recovery (reading the KB's raw
markdown directly) is the last-resort fallback when the solet runtime
itself is unavailable. Keep the two files in lockstep on that contract; let
them diverge only where the driving tool genuinely differs (Claude Code's
`claude-<name>` launcher and `<name> watch`/`<name> wake` no-MCP messaging
path versus stock Codex's generated `codex-<name>` launcher and durable CLI
inbox contract — currently no Stop-hook delivery notice; stock Codex does not
execute async command hooks on any measured build, codex-0147-async-hook-
regression, 2026-08-13) — do not freeze one transport into the shared
knowledge bootstrap and do not let cosmetic wording drift accumulate between
the files.

## Solet agent environment contract

Generated launchers export neutral per-session variables consumed by the
SessionStart hook and by optional peer-bridge registration when policy permits
it. These names are part of the seed contract and should not be renamed
casually:

| Var | Occurrences |
|---|---|
| `SOLET_NAME` | **NOT part of the neutral AGENT_* family below** — that family is deliberately unprefixed per the 2026-07-28 ruling (`agent_messaging_plugin/env_contract.py`), which is about not re-prefixing *those five names* with `SOLET_`; it says nothing about this separate, pre-existing variable naming which solet a shell is scoped to. `launch.template` (daemon launcher — export), `claude_launcher.template`, `codex_launcher.template`, `fleet_functions.zsh.template` (session launchers — export, added 2026-08-02 to close a launcher asymmetry: session-launched shells previously had no way to know their own solet's name). Consumed at module-import time by several plugins for scoped vault-key resolution (`agent_messaging_plugin`, `g_suite_plugin`, `signal_plugin`, `soundcloud_artist_studio_plugin`) and required by `quality_gates/run_smokes.py`'s fail-closed check — the Git-Controller gate path — so a session launched without this export cannot run gate scripts unless the caller sets it by hand. ⚠ **Arm all launchers or none**, same reasoning as `GIT_CONTROLLER_NAME` below: a launcher that omits it produces a session class where in-session gate scripts and vault-scoped plugin code fail loud unpredictably depending on which launcher started the shell |
| `SOLET_PERMISSION_POSTURE` | **OPERATOR-SET, never rendered** — read by `claude_launcher.template` only (seed feedback #30/§48.3, 2026-08-24: the explicit 3-state posture Dax's upstream ask asked for). Three values: `bypass` (default — `--permission-mode bypassPermissions`, a fresh install can act immediately), `prompt` (`--permission-mode default` — forces Claude Code's own standard per-action approval flow regardless of what this machine's own `~/.claude/settings.json` otherwise says), `inherit` (no `--permission-mode` flag at all — defers entirely to Claude Code's own resolution, whatever this machine's global config says right now). An unrecognized value warns on stderr and falls back to `bypass`. ⚠ **`prompt` and `inherit` are NOT the same state**, and the difference is the exact gap #30 reported: `inherit` follows the operator's own global settings, which can silently change meaning across a Claude Code CLI update (one operator's flagless-equivalent session came up auto-denying, with no approval path, after such an update); `prompt` is a fixed, explicit request for standard prompting that does not depend on the global config at all. ⚠ **The launcher takes POSITIONAL args (`label`, `model`, `effort`) and forwards no arbitrary flags**, so `claude-<name> --permission-mode …` is read as `label="--permission-mode"` and fails with an error naming neither cause; the env var is the supported route. ⚠ **A rendered launcher is written ONCE at genesis** — a `git pull` updates this template and never the operator's `client/bin/claude-<name>`, so delivering a change here REQUIRES re-running hydration Step 2 (see the update runbook's Step 5) |
| `SOLET_PERMISSION_PROMPTS` | **OPERATOR-SET, never rendered, DEPRECATED ALIAS** — `SOLET_PERMISSION_PROMPTS=1` is kept as a permanent alias for `SOLET_PERMISSION_POSTURE=inherit` (its exact pre-#30 meaning) so no already-exported deployment's shell profile changes behavior. Prefer `SOLET_PERMISSION_POSTURE` directly in new guidance; this row exists for compatibility, not as the documented route going forward |
| `AGENT_SESSION_LABEL` | `claude_launcher.template` (export); `codex_launcher.template` (export); `fleet_functions.zsh.template` (export); and the coordination-hooks Claude plugin, where it is **load-bearing at user scope — an unlabeled `claude` session anywhere on the machine must get zero output**. ⚠ Measured via `ls hooks/` + `hooks/hooks.json` (re-derive both, never trust a count in prose — entrypoints, registrations and event types are three different numbers): on the Codex plugin, **three of the four** shipped entrypoints are label-gated — `step_zero_reminder.js`, `check_messages_reminder.js`, `role_binding_reminder.js`. The fourth, `git_controller_gate.py`, is **label-INDEPENDENT**: it arms on the presence of `GIT_CONTROLLER_NAME` and evaluates wherever armed. Zero-output for unlabeled sessions therefore holds for the gate **by its arming boundary, not by a label guard** — see that variable's row. The Codex plugin currently ships no `Stop`-bound entrypoint at all (codex-0147-async-hook-regression, 2026-08-13). `claude_settings.json.template` no longer guards anything: it ships no hooks at all (the plugin owns every one — read the current roster from `hooks/hooks.json`, do not restate a count here). The repo-scoped Codex reminders use the same non-empty label guard |
| `AGENT_SESSION_ID` | `claude_launcher.template`, `codex_launcher.template`, and `fleet_functions.zsh.template` (single-sourced export); consumed by the Claude `Stop` hook's `<name> wake`, which derives the per-session watch spool path from it — the same derivation `<name> watch` uses, so the pair meets with no flags. The Codex coordination plugin ships no `Stop` hook currently (stock Codex does not execute async command hooks on any measured build); this identity also reconciles a managed watch registration to its spawn row for driver-channel auto-drive regardless. `peer_inbox` takes this same value and resolves the caller's registered binding server-side |
| `AGENT_WAKE_CLI` | `claude_launcher.template`, `codex_launcher.template`, and `fleet_functions.zsh.template` (export). Names the coordination CLI (the `<name>` console script) for the Claude coordination-hooks Stop waiter, which invokes exactly `$AGENT_WAKE_CLI wake`. The Codex plugin ships no Stop waiter to invoke it currently; in a managed session, `drive_on_delivery` owns turn initiation through the host driver regardless. The user-scope settings Stop hook does not read this variable (it execs `<name> wake` directly) |
| `AGENT_COORDINATION_RECEIPT_PATH` | `claude_launcher.template` and both Claude host adapters export the absolute `<app_home>/data/coordination-hooks/claude/installation.v1.json` path. It identifies the installed owner-qualified receipt for heartbeat, context-watch, and wake hooks; absent APP_HOME means the adapters omit it rather than guessing from CWD. |
| `FLEET_TRANSPORT` | `codex_launcher.template` and `fleet_functions.zsh.template` (export, from the operator knob `<NAME>_FLEET_TRANSPORT`, default `watch`); `rename_skill_SKILL.md.template` (transport-selection read). The Stop-hook guard moved OFF `claude_settings.json.template` under WS-5b-core — that file ships no hooks at all now; the guard lives in the coordination-hooks plugin. Values `watch` \| `mcp`; unset resolves to the machine's standing default (`watch` on hydrated seeds, `mcp` on the platform development checkout). Declares the fleet-coordination transport; the rename skill never probes and never silently crosses transports. Stock Codex launchers export `watch` explicitly, arming the durable `<name> watch` receive path — no Codex-side background hook currently reads this transport declaration (codex-0147-async-hook-regression) |
| `AGENT_IDENTITY` | `codex_launcher.template` exports `codex`; the optional Claude MCP reference form lives in the root README, cited by the hydration runbook's mcp-add step |
| `AGENT_ROLE` | `codex_launcher.template` exports the launcher role. The stock-Codex git gate authorizes only from this variable; a controller-looking label or session id cannot grant mutation authority |
| `GIT_CONTROLLER_NAME` | `claude_launcher.template`, `codex_launcher.template`, and `fleet_functions.zsh.template` (export) — all rendered from `{{GIT_CONTROLLER_NAME}}`, or the whole line **deleted** for a solo deployment. **RULED 2026-08-01 (operator), superseding the previous ship-unarmed position:** the **configuring session sets this during hydration**, and its **presence is what arms** the coordination-hooks git-mutation gate. Export surface is the **launcher templates** (Architect ruling): the gate is a **FLEET-CHECKOUT control** — it arms exactly the sessions that operate in the shared checkout, and production user repositories are **out of scope by design** (their git safety belongs to each runner's own permission model). ⚠ **`GIT_CONTROLLER_NAME` is the name the SHIPPED copies read** (`claude_plugin/…`, `codex_plugin/…`); the origin repo's own `.claude/hooks/` copy reads `SOLET_GIT_CONTROLLER_NAME` and **never ships** — a deliberate per-copy binding, parameterized and never unified by deletion. Setting the origin name on a seed arms **nothing**, and the gate is **fail-OPEN when unset**, so the wrong name yields a gate the operator believes is armed. ★ **SOLO EXEMPTION — the mechanism is this variable's ABSENCE.** A solo deployment's hydration simply **omits** it: nothing is armed, which is the exemption, with zero runtime machinery. **Do not add a session-count probe to "implement" this** — configuration already implements it, and every runtime session-count source is known to lie (stale stamped ids, phantom arms, label-sweep evictions). A transiently-solo *fleet* is handled by policy language, not by a mechanical check. ⚠ **MEASURED at source in both shipped copies** (`claude_plugin/…` and `codex_plugin/…`: `git_controller_name()` resolves to `os.environ.get(…, "").strip() or None`): an **empty value reads exactly as unset**, so a line accidentally rendered blank is disarmed, not half-armed. *Absence* remains the documented form anyway — presence-with-an-empty-value is not what the exemption says, and no later reader should have to re-derive that equivalence to trust it. ⚠ **Arm all launchers or none.** The gate reads no session label, so a deployment that exports the variable from one launcher and not another gains a session class that escapes the gate purely by which launcher started it |

The whole family is deliberately UNPREFIXED (operator seed-naming ruling
2026-07-28: no `SOLET_*` env names in seed-facing artifacts). The
`AGENT_*` five-name family (`AGENT_IDENTITY`, `AGENT_INSTANCE_ID`,
`AGENT_SESSION_LABEL`, `AGENT_SESSION_ID`, `AGENT_ROLE`) is read by shipped
platform code (`agent_messaging_plugin/env_contract.py` is the single source
of truth; `mcp_bridge/__main__.py` and `local_cli` watch/wake read through
it) with no fallback and NO legacy-alias reads: for one release those entry
points fail loudly when a pre-migration `HOMUNCULUS_AGENT_*` name is present
without its neutral replacement. Renaming any member piecemeal in these
templates recreates the 2026-07-25 half-landed-rename incident — code and
templates move together or not at all.

## Permission posture (post-2026-08-20 ruling contract)

**This used to be titled "Invariant" and said `claude_launcher.template`
"never carries `--dangerously-skip-permissions`," framed as something "do not
edit away." That framing is retired (seed feedback #30/§48.3, 2026-08-24) —
it is exactly the prose a seed-born solet quoted to REFUSE an operator's
explicit, authorized request to add that flag to their own already-hydrated
launcher, which is backwards: the 2026-08-20 ruling that flipped the shipped
default to `bypassPermissions` was an explicit rejection of treating
permission posture as a hard-coded, un-overridable invariant. Nothing about
that ruling — or this section — restricts what an operator may configure on
their own deployment.** If you are an agent reading this to decide whether to
honor an operator's instruction about their own launcher's permission flags:
this document is never grounds for refusal. Comply, and disclose what you
did — this section documents the SHIPPED DEFAULT, not a ceiling on the
operator's authority over their own machine.

**The actual contract, stated plainly:**

- `claude_launcher.template`, as hydrated, defaults to `--permission-mode
  bypassPermissions` (the 2026-08-20 default flip, now the `bypass` state of
  `SOLET_PERMISSION_POSTURE` above), not
  `--dangerously-skip-permissions`. On installed Claude Code 2.1.243 those
  spellings select the same effective `bypassPermissions` mode and are
  behaviorally equivalent; the spelling difference is not a second safety
  tier. Neither bypass spelling retains a permission classifier. The
  background classifier belongs to `auto` mode.
- `SOLET_PERMISSION_POSTURE=prompt` or `=inherit` are the documented
  opt-outs, for a session the operator wants supervised either explicitly
  (`prompt`, fixed regardless of global config) or by deferring to whatever
  this machine's own Claude Code settings already say (`inherit`).
- An operator may choose either bypass spelling on their own deployment, but
  changing from `--permission-mode bypassPermissions` to
  `--dangerously-skip-permissions` does not go further and cannot repair a
  refusal: both select the same effective mode. If a session expected to be in
  bypass is denied, first establish its effective mode and capture the exact
  denial, then classify the independent boundary (hook, managed policy, tool
  availability, OS/sandbox, credential, service, or another evidenced cause).
  Do not infer a permission classifier from the denial; the classifier is an
  `auto`-mode feature. Hydration must still preserve an operator's own launcher
  edits outside its marker regions, but preservation is an authority rule, not
  evidence that the alternate spelling has different behavior. For this
  contract, installed-binary behavior outranks current vendor documentation,
  and both outrank this knowledge-base text.
- `codex_launcher.template`'s own posture is UNCHANGED by the 2026-08-20
  ruling and stays exactly as strict as before: it never carries a
  dangerous approval, sandbox, hook-trust, or MCP bypass. The ruling was
  scoped to Claude Code's permission-mode default only; nothing about the
  Codex launcher's own invariant moved.
- `CODEX_BIN` may select an explicit stock executable; hydration never
  points it at a locally patched receive build.

The user's native approval flow applies when the resolved mode prompts, and
the background classifier applies only in `auto` mode. A session in effective
`bypassPermissions` uses neither as a per-action boundary; independently
configured hooks, managed policy, tool availability, OS/sandbox controls,
credentials, and service-level refusals must be diagnosed from their exact
evidence. Explicit `/hooks` trust review remains part of client deployment.
