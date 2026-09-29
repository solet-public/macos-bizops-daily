# Seed Update Runbook — Updating a Live Solet From a Re-Minted Seed

Tags: knowledge:tag:planning_reference

Article Layer: 2

Article Role: operations_runbook

Article Tags: planning-stage:solet-lifecycle, evidence-category:operations-runbook, domain:local-solet, domain:client-deployment, consumer_profile:both

Embedding Description: Agent-facing runbook for applying a newer seed release to an ALREADY-LIVE seed-born solet without losing its state, now led by the Solet Manager path — install the Manager from its Homebrew tap, classify the clone with `solet-manager inspect`, enroll it once with `solet-manager import`, then for every release run `solet-manager update --dry-run` to read the preview (which lists the genesis-written files and untracked genesis paths it will preserve, the exact fast-forward, the dependency, migration, hydration and lifecycle operations it will perform) and `--yes --approval-fingerprint` to apply it through the final doctor and promotion, with `solet-manager doctor` as the read-only oracle and `solet-manager reconcile` as the one answer to a terminal update — including the exact refusal vocabulary (`history_diverged`, `tracked_overlap_present`, `staged_changes_present`, `tracked_shape_changed`, `executed_code_modified`, `git_metadata_present`, `host_requirement_missing`, `source_identity_unproven`) and what each exit code means, the six steps that stay manual and why (the target `AGENTS.md`/`CLAUDE.md` hydration block and launchers, the first-time export-root answer, quitting Claude Code before the rename migration can rewrite `~/.claude.json`, relaunching open clients and re-arming watchers, adding a release-added plugin to the profile manifest, connector three-read and feedback-item checks), how an adopter learns a new re-mint exists (the formula upgrade plus the GitHub release subscription, re-pointed when the seed moves homes), and the legacy manual procedure retained verbatim as recovery for clones the Manager classifies as not importable (diverged history, development checkout) or for an instance the operator chooses to repair by hand.

> **Status (2026-09-19, existing-solet import/update Step 7):** the Manager
> path (Part A) is the update procedure. The manual procedure this document
> used to lead with is retained verbatim as Part C, **legacy recovery**: it
> applies only to a clone `solet-manager inspect` classifies as not
> `allowed_after_import` (`diverged_seed_history`, `development_checkout`),
> or to a `needs_attention` instance the operator chooses to repair by hand.
> Part B names the steps no Manager surface owns yet and why. Part D is the
> release history, narrative only.

## When to use this runbook

Use this when a solet is ALREADY alive and healthy on this machine and a
newer seed release has been published. Do not use it for first-time setup
(that is the hydration runbook) or for a broken instance that will not boot
(that is teardown plus re-birth).

## How you learn a new release exists

The Manager learns about releases through its own formula: `brew upgrade
solet-public/tap/solet` installs the seed lock naming the new release, and
`solet-manager update <name> --dry-run` then reports the candidate as
`available` (the doctor's section 16, `installed_descriptor_release`, says
the same thing from the read-only side). The GitHub subscription remains
the human notification channel that tells you to run that upgrade at all:

**Subscribe to the seed repository's releases on GitHub**: **Watch → Custom
→ Releases** at the repository this clone was born from, not the default
"All Activity" setting. A re-mint publishes as a dated release
(`release-YYYY-MM-DD`) carrying a `RELEASE_NOTES.md`; that notification is
the trigger for this runbook. Do not wait to be told by a human, and do not
poll the repository by hand — the subscription is the mechanism.

**When the seed repository moves to a new home, re-subscribe at the new
repository in the same sitting.** A subscription still pointed at the old
repository goes silent the moment the move takes effect — no error, no
missed-notification signal, just nothing arriving again. That is a worse
failure than never subscribing at all, because it looks identical to "no new
releases have shipped" from where you sit. The Manager performs the
`origin` re-point itself, as a fingerprinted canonical-channel action, only
when the new release's descriptor declares the old URL in
`allowed_repository_migrations`; the subscription it cannot move for you.

## The one decision: Manager path or re-birth

**The Manager path is the default.** Seed releases are append-only: a
re-mint adds a commit to the SAME seed repository the clone was born from,
fast-forward only, never rewriting history. `solet-manager inspect --target
<clone> --channel stable` classifies the clone; every real clone born by
genesis or `solet create` classifies as `local_changes_present` with
`allowed_after_import`, and its reason codes (`tracked_changes`,
`untracked_paths`) name the local state the preview will weigh, not a
problem. `solet-manager update <name> --dry-run` is the actual gate:
`preview_ready`, or an exact reason with the paths it applies to.

**Re-birth is the fallback, not the routine.** Choose teardown plus re-birth
only when `inspect` answers `diverged_seed_history` or
`development_checkout`, the release notes explicitly require a fresh birth,
or the operator wants a clean-slate instance. Re-birth resets accumulated
state; say that plainly to the operator before choosing it. A refusal from
`update --dry-run` is not a re-birth signal: it names a repair.

## Part A — the Manager path

One block of six commands. Every command is target-read-only except
`import --yes` (Manager state only; no target byte) and `update --yes`
(the journaled operations the preview listed, nothing else).

**A solet `solet create` installed skips `inspect` and `import`.** The
Manager proves it against its own create record (the v1 registry row and
the create transaction): the checkout must be at the exact
recorded commit with its committed `PROVENANCE.json`, from the channel's
repository, profile and `origin_id`. The create transaction must show the
install over, not verified: every install stage final and every completion
check answered. A create whose completion checks stayed `blocked` or
`failed` (every r46–r48 install, whose `coreai_embedding_request_succeeds`,
`knowledge_retrieval_succeeds` and `plugin_roster_matches_plan` never
passed) is eligible; the dry-run's `enrollment` block names those checks
under `create_transaction`, and the update's final doctor runs its own
checks against the new release. A create still in flight (an install stage
not final, a completion check never run) refuses `operation_in_progress`
with the repair `Resume with: solet create <name>`, and `import` refuses it
the same way at `--dry-run` and at `--yes`. A current `solet create` enrolls the
instance itself (`maintenance_enrollment` in its result). One created by an
earlier Manager (r46–r48) has no v2 row yet, so `update <name> --dry-run`
renders the update from the would-be row plus an `enrollment` block
(`status: planned`) and binds it into the fingerprint, and `update --yes`
enrolls and continues in the same command. A failed proof is
`create_origin_identity_unproven` (with the failed checks) or
`managed_identity_drift`: a repair, never a reason to import or re-birth.
`import` below is for a plain-clone solet the Manager did not create.

The proof is of content, not of the directory: the create record keeps
the target path but no inode, so any byte-identical checkout at that path
proves the same. The path itself is closed. `solet create` records a
resolved path, so the recorded target must still be a real directory at
exactly that path; a symbolic link at or above it (to a moved checkout
or to another copy) is `managed_identity_drift`, never followed. Put the
checkout back at the recorded path. An operator `--target` that reaches
the real directory through a link is fine; the recorded path is what is
inspected. An enrollment interrupted before it finished (a crash inside
`update --yes` or `solet create`) resumes: `update <name> --dry-run` shows
`enrollment.status: resume` with the same fingerprint, and `--yes` finishes
it and continues. If the Manager was upgraded in between, the same solet
proves again under the new channel, so the dry-run shows
`enrollment.status: supersede`, names the stale operation
(`superseded_operation_id`) and prints a new fingerprint. The old
fingerprint is refused. `--yes` with the new one records the stale
operation `abandoned`, enrolls under its successor and continues, all in
the same command. `solet create`'s `maintenance_enrollment_failed` repair
names the exact `import` commands and `update --dry-run` then `--yes`;
each finishes it.

Every Manager Git command against the checkout (`inspect`, `import`,
`update`, the doctor) runs with fsmonitor, hooks, replace refs, external
diff and system/global Git config disabled. It is pinned to the checkout
itself (`GIT_DIR`, `GIT_WORK_TREE`), so the checkout's own config cannot
move where the Manager reads or writes. A checkout whose own
`.git/config` configures code that Git would run (a clean/smudge filter,
a textconv or diff/merge driver, fsmonitor, `core.hooksPath`, an include,
and similar), or that redirects its work tree (`core.worktree`, or
`core.bare` set to true), is refused, never executed. `inspect`, `import`
and `update` all refuse with `git_execution_surface_unsafe` and the
repair: remove that configuration from the checkout, then retry.

These are `solet-manager` verbs. `solet --help` does not list `import`, `update`
or this `inspect`, and `solet update` / `solet import` refuse with a pointer to
the `solet-manager` command. `solet inspect` is a different, active probe.

```bash
brew install solet-public/tap/solet                       # once; brew upgrade for every later release
solet-manager inspect --target <clone> --channel stable   # classify; exit 3 attention_required is the normal answer for a real clone
solet-manager import <name> --target <clone> --channel stable --dry-run
solet-manager import <name> --target <clone> --channel stable --yes --approval-fingerprint <fingerprint from the dry-run>
solet-manager doctor <name>                               # exit 0 verified means the enrollment is sound
solet-manager update <name> --dry-run                     # the source preview: exact fast-forward, preserved local state
solet-manager update <name> --yes --approval-fingerprint <fingerprint>            # source stage: fetch, fast-forward exact candidate
solet-manager update <name> --dry-run                     # the runtime preview: dependencies, migrations, hydration, lifecycle
solet-manager update <name> --yes --approval-fingerprint <runtime fingerprint>    # runtime stage through the final doctor and promotion
solet-manager doctor <name>                               # exit 0 verified: the update is over
```

The update is over only when the journal reads `promoted` and the
inventory row is `verified`; `update --yes` continues past the runtime
stages through the final doctor and promotion in the same invocation, and
tells you the readiness result itself — there is no separate "restart and
wait".

**What the preview discloses, and what it refuses.** The `local_state`
group of the source preview lists three things a real clone always carries:
*preserved local modifications* (the genesis rewrite of
`root_manifest.yaml`, the hydration blocks in `AGENTS.md`/`CLAUDE.md`,
`NOTICE`, and the installer's interpreter pin in the two coordination-hook
manifests `plugins/github_midwife_plugin/claude_plugin/coordination-hooks/hooks/hooks.json`
and `plugins/github_midwife_plugin/codex_plugin/coordination-hooks/hooks/hooks.json`
— unstaged, content-only edits to tracked files the candidate does not
touch), *committed local state* (the untracked `.gitignore`, `.solet/`,
`knowledge_bases/*` symlinks, `client/` — paths the update commits to leave
byte-identical, verified after every operation), and the *preserved
surface* (`profile/**`, disclosed with kind, mode and size, never digested,
never committed). None of these is a refusal.

The two hook manifests sit under a roster plugin, an executed-code root, so
the Manager admits them only as the installer wrote them: every bare
`python3` hook command bound to `<target>/.venv/bin/python3` and nothing
else, re-derived byte-for-byte from the committed file (`local_state.installer_pins`
lists them). Never restore these files to the shipped `python3`: the
coordination hooks need the pin, and the fast-forward carries the pinned
bytes onto the new release. Any further edit to either file, or a pin to a
different interpreter path, is refused as `executed_code_modified`, the same
as a hand edit elsewhere under a roster plugin. The preview refuses only:

| Reason (`data.topology.reasons`, exit 3) | Meaning | Repair |
|---|---|---|
| `history_diverged` | the clone's HEAD is not an ancestor of the candidate | re-birth, or Part C if the operator wants to hand-merge |
| `tracked_overlap_present` | the candidate changes a file this installation modified locally | keep your lines by hand: `git diff <baseline>..<candidate> -- <path>`, then preview again; the Manager never overwrites, stashes or resets a local change. If the named file is a pinned coordination-hook manifest (listed in `local_state.installer_pins`), do not edit or restore it: upgrade the Manager (`brew upgrade solet`) and preview again — carrying the pin across a changed manifest is a Manager capability (`iss_c1a7df20`) |
| `staged_changes_present` | something is in the index | `git restore --staged <paths>` is the operator's call; the Manager never runs it |
| `tracked_shape_changed` | a tracked path was deleted, retyped, mode-changed or symlinked | restore it to a content-only edit of the shipped regular file |
| `executed_code_modified` | an edit under `bootstrap.py`, `bootstrap_adapter/`, an editable-installed distribution or a roster plugin, other than the installer's own interpreter pin | `git diff -- <path>` in the clone shows the local edit: undo only that edit (keep the installer's interpreter pin in a hook manifest) or move the change out of the tree, then preview again; the Manager will not execute a modified target |
| `git_metadata_present` | `.gitattributes`/`.gitmodules` anywhere, or an edited tracked root `.gitignore` | remove it; it changes how the fast-forward writes files |
| `preserved_surface_in_transition` | the candidate ships something under `profile/` | seed-side regression; file feedback, do not repair the clone |
| `source_identity_unproven` | `origin` names a URL the descriptor does not declare as an allowed migration | Part C, Step 2a (manual re-point) |
| `host_requirement_missing` / `host_requirement_unknown` | the host lacks Python 3.13 (`data.host` names the row) | `brew install python@3.13`, then preview again |

**Exit codes.** `0` — `preview_ready`, `imported`, `source_advanced`,
`promoted`, `verified`: proceed. `3` — `awaiting_user`/`attention_required`/
`incomplete`: read `data.topology.reasons` (preview), `data.classification`
(inspect) or the first `failed`/`missing`/`unknown` row (doctor); nothing
was written to the target. The doctor also answers `3` with
`managed_identity_drift` when the clone's HEAD is not where the Manager left
it, with the full report still rendered. `1` — a required doctor check
failed on evidence (the row names the reason). `2` — the target is not a
Solet checkout at all (`target_identity_invalid`, an identity substituted
under the enrolled path): stop and ask. A terminal update (`blocked`, `failed`) has one named answer:
`solet-manager reconcile <name> --dry-run` plans the successor, `--yes
--approval-fingerprint` mints it, `--abandon --yes` abandons or retires
before the fast-forward, `--release-pointer --yes` releases a stale pointer.
`reconcile` after a promotion answers `no_active_update` (exit 3): that is
the healthy state, not an error.

**What each stage owns** (the manual steps of Part C, by owner):

- Step 1 probe → `solet-manager doctor <name>` (enrolled) or `inspect` (not
  yet): sections 2 (topology and local state), 9 (service), 16 (release
  availability). "Hydration-generated files showing as modified is normal"
  becomes: the Manager lists them as preserved local modifications and the
  untracked genesis files as committed local state, and refuses only the
  table above.
- Step 2 pull → `update --dry-run`, review, `--yes`: the exact candidate
  fast-forward; `history_diverged` and `tracked_overlap_present` are the
  refusals, with paths.
- Step 2a re-point origin → performed as a fingerprinted canonical-channel
  action only when the descriptor's `allowed_repository_migrations`
  declares the old URL; otherwise `source_identity_unproven`, and Part C's
  manual block is the recovery.
- Step 3 venv → `dependencies_reconcile` probes the closure and installs
  exactly the missing declared pieces; the runtime preview lists them.
- Step 3a rename migration → `migration_solet_rename` (a `backup_required`
  migration; pass `--backup-checkpoint`), which also backfills the plist
  log redirection through `autostart_reconcile` and rewrites the
  `HOMUNCULUS_*` keys in `~/.claude.json` when no Claude Code process is
  running — while one runs it returns `blocked coding_agent_running` with
  "Quit Claude Code, then re-run --yes".
- Step 4 restart and wait → `lifecycle.cutover` (router present) or
  `lifecycle.restart_single_color`; readiness is `bridge_health_healthy`
  within the bundle's budget. A release the old preflight cannot install is
  expressed by the candidate bundle declaring `single_color_required`; you
  no longer need to know that exception.
- Step 4a export root → `migration_export_root_containment` propagates an
  already-configured root to newly installed connectors (first-time answer:
  Part B).
- Plugin transitions (r52, iss_6d26db73) → `migration_plugin_transition`
  applies the release's declared plugin replacements
  (`plugins/github_midwife_plugin/knowledge_base/plugin_transitions.json`) to a solet whose profile still
  carries the predecessor plugin. See "Plugin transitions: LM Studio solets
  move to the Apple-native stack" below.
- Step 5 hydration re-run → `hydration_reconcile` for the six declared
  managed artifacts: the instance LaunchAgent plist, the `~/.zshrc` block,
  the `~/.claude/CLAUDE.md` section, the `/feedback` skill
  (`~/.claude/skills/feedback/SKILL.md`), the block that makes the clone
  ignore `client/` (`<clone>/.git/info/exclude`), and the fleet launcher
  (`<clone>/client/<name>-fleet.zsh`) (the rest: Part B). The skill is
  **refresh-only**: the update replaces it only when it is byte-identical to a
  render of a previous release's template (which is every solet that still has
  the r56 skill, whose filing steps fail for an account without repository
  access), and it backs the old file up first like every managed artifact. A skill you edited is not
  overwritten and does not stop the update: the preview lists it with
  `state: unknown_origin` (or `locally_modified`) and `action: none`, the
  final doctor reports `managed_artifact:feedback_skill` as verified-and-left
  as it is, and the manual step in Part C Step 5 is how to bring the fix into
  it. A missing skill is not created.
  The fleet launcher is refreshed **above the line `# One function per role
  the operator chose in Step 4a.` only** (r58). Your role functions, your
  `GIT_CONTROLLER_NAME` choice and everything from that line down stay
  byte-for-byte. The update replaces the launcher section only when it is
  byte-identical to a render of the r56 template with your own
  `GIT_CONTROLLER_NAME` value, and it backs the file up first. A section you
  edited, or one from any other release, is not overwritten and does not stop
  the update: the preview lists it with `state: unknown_origin` (or
  `locally_modified`) and `action: none`, and the manual steps in Part C
  Step 5 are how to bring the change into it. A clone with no fleet file is
  left without one. Because `client/` sits inside the clone and existing
  clones do not ignore it, the preview also lists an action that adds `client/`
  to the clone's `.git/info/exclude`, a local file Git never commits; it is a
  marked block, backed up like the rest, and the files already in `client/`
  are not touched. Without that ignore rule the update plan would refuse
  the fleet file and stop the update for every solet (design section 6.3).
- Step 6.1 plugin cache → `plugin_cache_refresh` (diff-based;
  `plugin_cache_current` in doctor section 11).
- Step 6.4 KB re-install → `runtime.knowledge_reinstall` for every
  `knowledge_removals` entry, then `knowledge_negative_search_<kb>` is a
  required row of the final doctor.
- Step 7 verify → the final doctor inside `update --yes` and `solet-manager
  doctor <name>` afterwards. The router `no_active_color` race described in
  Part C reads as `service_offline` in the doctor's section 9 while it
  heals; re-probe.

### Plugin transitions: LM Studio solets move to the Apple-native stack

A release can declare that one plugin replaces another for a service; r52
declares two, for solets born before the Apple-native stack (r46):

| Transition | Profiles | From → to |
|---|---|---|
| `embedding_service.openai_to_coreai.v1` | macos-bizops, macos-free-solet, macos-samantha-solet | `openai_embeddings_plugin` (LM Studio) → `coreai_embeddings_plugin` |
| `inference_service.lmstudio_to_apple.v1` | macos-bizops | `default_inference_plugin` (LM Studio) → `macos_inference_plugin` |

The runtime preview's `migrations_pre` stage lists, per transition, the
pinned Core AI asset download, the readiness proof, and the exact file
writes (the new plugin config, the roster line in the profile's
manifest.yaml, the binding in the profile's service_bindings.json).
Each written file is backed up
before the apply, like every `backup_required` migration.

What the transition guarantees:

- **Only where the host can run it.** Each replacement names a host profile
  in the flow's `host_profiles` (`apple_embeddings` for Core AI, `apple_fm`
  for Apple FM; both macOS 27 on arm64 in r52), measured with `sw_vers` and
  `uname -m`. On a host below it (macOS 26 Tahoe), the release's closure
  leaves that package out, the transition reports `host_unsupported`
  (healthy, not pending), and the binding stays on LM Studio. Once the host
  qualifies, the doctor row says the switch comes with the next release's
  update (`update_at_next_release`).
- **Ready before switched.** The replacement is proven first: the Core AI
  asset is acquired and one real embedding must return 768 normalized
  dimensions; Apple FM must import. Only then does the binding move.
  Existing vectors are kept: nomic v1.5 on Core AI embeds the same space
  LM Studio's nomic v1.5 did (measured, see the design record on
  iss_6d26db73).
- **Not ready is not a failure.** If the replacement is not ready (asset
  download failed, readiness proof wrong or timed out, or the step's share
  of the adapter timeout spent), nothing is written,
  the old binding stays active, the row is journaled `deferred` with
  `plugin_transition_pending`, and the update still promotes, but to
  `needs_attention` rather than `verified`, with `data.deferred_operations`
  naming the transition and its repair. Run `solet-manager update <name>`
  again: it selects `verify` mode at the same release and retries only what
  is still pending.
- **Edited configuration is refused, not overwritten.** The embedding
  transition matches the predecessor config exactly; the inference
  transition matches on `base_url` only (setup chose the model). A solet
  whose old config was edited reports `conflict`; the row defers with
  `plugin_transition_conflict`, and the preview names the refusal. Restore
  the shipped config or move the binding by hand.
- **Everything else is preserved.** Extra plugins keep their roster place,
  the old plugin's config file stays on disk (inert), and `profile/data` is
  untouched. LM Studio and its models are left exactly as they were: no
  path uninstalls LM Studio or deletes, moves or changes a model
  (rul_ef0363a2). This solet simply stops using them for the transitioned
  services.
- **Interruptions resume.** Every intermediate write state boots (the new
  plugin enters the roster before the binding moves). A resumed update
  completes the transition, or reverts it to the predecessor bytes when the
  replacement is no longer ready. A repeat apply is a byte-identical no-op,
  and an already-Apple-native solet verifies with no plan.

The final doctor reports the transitions as the advisory
`plugin_transitions` row in the plugin-roster section. A pending transition
never fails the doctor; the prior plugin stays active and runnable.

## Part B — manual steps that remain

Each of these stays manual because no Manager surface owns it; the reason
is given with the step so the next release can close it deliberately.

1. **Target-root `AGENTS.md`/`CLAUDE.md` hydration block, `client/bin/*`
   launchers, `~/.claude/settings.json` hooks, the rename skill.** Not
   declared managed artifacts in the shipped bundle; `hydration_reconcile`
   owns only the six it declares (the `/feedback` skill and the fleet
   launcher are two of them; see Part A). Re-run Part C Step 5 for these, by
   hand (seed-side artifact declarations are follow-up D7). The rest of
   `client/` stays manual for a stated reason: it sits inside the clone, and
   no artifact declares it. The update ignores `client/` in the clone only so
   it can refresh the fleet launcher. Part C Step 5 also gives the exact
   steps for a hand-edited feedback skill and for a fleet launcher the
   update left alone.
2. **The first-time export/workspace root answer** (Part C Step 4a). No CLI
   carrier for the answer exists; `migration_export_root_containment`
   blocks with `export_root_ambiguous` or `none` and that text, and
   propagates once a root is configured.
3. **Quitting Claude Code before `migration_solet_rename` can rewrite
   `~/.claude.json`.** The Manager never kills a process; doctor section 11
   `stale_target_processes` names the pid. The `--scan-stale` review of Part
   C Step 3a is report-only and also stays yours.
4. **Relaunching open clients and re-arming watchers** (Part C Steps 6.2 and
   6.3). Same principle: the doctor names them (`relaunch_client_session`,
   `rearm_watcher`); you perform them.
5. **Adding a release-added plugin to the clone's profile manifest** (the
   genesis-written `manifest.yaml` under the profile's `config` directory).
   Everything under `profile/` is the preserved-never surface; the Manager
   discloses it and never writes it. Part C Step 5's paragraph on activation
   applies.
6. **Marketo/Zuora three-read connector checks and reconciling your open
   feedback items against the release notes.** Connector behaviour and
   upstream feedback are not Manager checks.

## Part C — legacy recovery: the manual procedure

> **Status:** this procedure is retained verbatim as recovery. It applies
> only to a clone `solet-manager inspect` classifies as not
> `allowed_after_import` (`diverged_seed_history`, `development_checkout`),
> or to a `needs_attention` instance the operator chooses to repair by hand.
> For every other clone, Part A performs each step below with a journaled
> postcondition; the step-by-owner list in Part A says which operation.

## Step 1 — probe before touching anything

- `<name> health` answers (the instance is alive now; if it is not, this is
  repair or re-birth territory, not an update).
- `git -C <clone> remote get-url origin` points at the seed repository.
- `git -C <clone> status --short` — hydration-generated files (`AGENTS.md`,
  `CLAUDE.md`, `client/`, workbench notes) showing as untracked or modified is NORMAL and
  harmless; the seed never ships those paths, so they cannot conflict. Only a
  conflict during the pull itself is a stop condition.

## Step 2 — pull, fast-forward only

```bash
OLD=$(git -C <clone> rev-parse HEAD)   # keep this: Step 3 diffs against it
git -C <clone> pull --ff-only
```

If this refuses ("not possible to fast-forward"), STOP. The clone's history
and the seed repo have diverged — that is not a normal update state. Present
the facts to the operator; the usual resolution is re-birth from the current
seed. Never force, never rebase, never merge — a factory re-mint always
fast-forwards.

## Step 2a — re-point `origin` to the seed's new home (ONE TIME, this update only)

The seed has moved. The repository this clone was born from published its
FINAL update as the release you just pulled; every future re-mint is published
at the new home only. Nothing about your clone's history changes — the new
repository was seeded with the old repository's history before this release
published there, so the two share a common ancestor and your next pull is an
ordinary fast-forward, not a re-birth and not a divergence.

Do this AFTER Step 2's pull, not before. Pulling from the old URL still works
for this release (it published to both), so the safe order is: take the update
from the remote you can already reach, then move the remote.

```bash
git -C <clone> remote get-url origin                        # note the old URL first
git -C <clone> remote set-url origin <new-repository-URL>   # from this release's RELEASE_NOTES.md
git -C <clone> remote get-url origin                        # confirm it took
git -C <clone> fetch origin                                 # proves the new home is reachable
```

The exact new URL is in this release's `RELEASE_NOTES.md`; take it from there
rather than from memory, and do not guess at it.

**If `git fetch origin` fails with an access or not-found error, put the old
URL back** (`git remote set-url origin <old-URL>`) and tell the operator before
going further. Access to the new home is granted per-deployment, and a clone
pointed at a repository it cannot read has no update path at all — that is a
worse state than the one you started in. A failed fetch here is an access
question for the operator, not something to work around by re-pointing
somewhere else.

Verify at the end of the update: `git -C <clone> remote -v` shows the new URL
for both fetch and push, and `git -C <clone> status -sb` shows the branch
tracking a branch on it.

**Also re-subscribe to releases at the new repository now** (see "How you
learn a new release exists" above) — an existing GitHub Watch subscription
does not follow a `remote set-url`; it stays pointed at the old repository
and goes silent from here on unless you re-subscribe at the new one.

## Step 3 — virtual environment: usually nothing

The clone's `.venv` was built with EDITABLE installs (`pip install -e` per
package), so pulled code changes are live the moment the solet restarts —
no reinstall for ordinary updates, including new CLI subcommands.

The exception is structural: the release added a NEW plugin, or a plugin's
dependencies changed. The release notes (the re-mint commit message) say so
when it matters. Then, for each new plugin:

```bash
<clone>/.venv/bin/python -m pip install --no-build-isolation -e <clone>/plugins/<new_plugin>
```

If in doubt, this install form is idempotent — re-running it on an
already-installed plugin is harmless.

**Local packages are not only `plugins/<name>`.** A plugin can depend on a
package that lives in the clone but outside `plugins/` — `solet_setup_contracts/`
is the one today — and a plugin installed without it fails to import. Do not
work from a list of names. Ask each `pyproject.toml` the pull changed which of
its `dependencies` name a project that lives in this clone:

```bash
<clone>/.venv/bin/python - <clone> "$OLD" <<'EOF'
import re, subprocess, sys, tomllib
from pathlib import Path

clone, old = Path(sys.argv[1]).resolve(), sys.argv[2]
canon = lambda name: re.sub(r"[-_.]+", "-", name).lower()
local: dict[str, Path] = {}  # project name -> its directory, for every pyproject.toml in the clone
for pyproject in [*clone.glob("*/pyproject.toml"), *clone.glob("plugins/*/pyproject.toml")]:
    project = tomllib.loads(pyproject.read_text()).get("project")
    if project:
        local[canon(project["name"])] = pyproject.parent
changed = subprocess.run(
    ["git", "-C", str(clone), "diff", "--name-only", f"{old}..HEAD", "--", "*/pyproject.toml"],
    check=True, capture_output=True, text=True).stdout.split()
for rel in changed:
    for requirement in tomllib.loads((clone / rel).read_text())["project"].get("dependencies", []):
        name = canon(re.split(r"[<>=!~;\[ ]", requirement, maxsplit=1)[0])
        if name in local:
            print(f"{rel}: needs {local[name].relative_to(clone)}")
EOF
```

Every directory it prints gets the same install form as a plugin, run BEFORE
the plugin that needs it (for example
`pip install --no-build-isolation -e <clone>/solet_setup_contracts`). An empty
output means no changed plugin gained a local dependency. Editable installs
are idempotent, so installing a directory that was already present is harmless.

## Step 3b — renamed commands (the bridge console script, `solet` → `solet-bridge`)

The `agent_messaging_plugin` console script was renamed from `solet` to
`solet-bridge` (2026-08-26). There is no compatibility alias: the old name is
gone from the venv once the plugin is reinstalled, so any operator symlink or
script that still points at `<clone>/.venv/bin/solet` is now dangling.
`solet call …`, `solet health` and `solet watch …` are spelled
`solet-bridge call …`, `solet-bridge health` and `solet-bridge watch …`; the
bare name `solet` is the Manager, which has no `call` subcommand at all.

A clone born before the rename has a named launcher symlink at
`~/.local/bin/<name>` that targets the old script. Reinstall the messaging
plugin so the new console script exists, then re-point the link:

```bash
<clone>/.venv/bin/python -m pip install --no-build-isolation -e <clone>/plugins/agent_messaging_plugin
readlink ~/.local/bin/<name>            # …/.venv/bin/solet means it still needs the fix
ln -sfn <clone>/.venv/bin/solet-bridge ~/.local/bin/<name>
test -x ~/.local/bin/<name> && <name> health
```

`ln -sfn` replaces only the link and leaves the clone untouched. Update any
shell function, LaunchAgent argument or note of your own that spells
`.venv/bin/solet` the same way.

## Step 3a — the solet rename migration (MANDATORY when the update crosses 2026-08-13)

Releases minted from 2026-08-13 onward name every live identifier `solet`:
the CLI console script, the `SOLET_*` environment family, the `solet_name:`
root-manifest key, launchd labels `local.solet.<name>`, and the `--solet`
flag on the router/ingress entry points. There are NO compatibility aliases.
An installation born before that boundary still carries the old names in its
LaunchAgent plists, launcher shell functions, venv console script, and root
manifest — state a code pull does not rewrite. Restarting across the boundary
without migrating fails loud by design: the platform refuses to boot when it
finds `HOMUNCULUS_NAME` in its environment or the old manifest key, and the
error names this step.

Run the migration BEFORE Step 4's restart. It is self-executing — dry run
first, then apply:

```bash
<clone>/.venv/bin/python3 <clone>/deployment/scripts/migrate_to_solet.py
<clone>/.venv/bin/python3 <clone>/deployment/scripts/migrate_to_solet.py --apply
```

The script boots out the affected LaunchAgents (with a bounded wait for the
old label to actually clear `launchctl print` before bootstrapping — never
an unbounded spin, fails loud if it doesn't clear in time), rewrites their
labels, filenames, environment keys, and arguments, flips the launcher shell
functions, reinstalls the messaging plugin so `<clone>/.venv/bin/solet-bridge`
replaces the old console script (the old shim is removed, not aliased), and
bootstraps the agents back. Idempotent: re-running reports already-migrated
pieces and changes nothing — re-running `--apply` against an already-migrated
surface produces byte-identical files, never a spurious rewrite. Updates that
do not cross the boundary find nothing to do — the dry run prints an empty
plan and you move on.

After the platform is back (Step 4), run the residual guard: enumerate the
plugin-config store and fail loud on any `homunculus_name` key it still
carries (`<solet-name> call` a config listing if the release provides one, or ask
the solet directly to enumerate its plugin-config keys). A hit means a
plugin carried deployment-local config this script does not know about —
stop and repair before continuing, do not rename it ad hoc.

**Post-apply, in the order you'll hit them:**

1. **Plist log redirection is a durable-path issue, not a one-shot patch.**
   `migrate_to_solet.py` backfills `StandardOutPath`/`StandardErrorPath` on a
   solet-owned plist that predates them (pre-June installs), pointed at the
   same `<log_dir>/<name>_autostart.log` `autostart_manager` itself derives —
   but a field patch is a stopgap. The DURABLE path is re-running the
   autostart install verb: `_classify_install_prior` re-renders a
   `present_but_stale` plist wholesale, which a field patch never will. Prefer
   that over trusting the migration script's patch to have caught everything.

2. **`~/.claude.json`'s MCP server env keys need Claude Code closed.** The
   migration script renames `HOMUNCULUS_*` → `SOLET_*` inside every
   `mcpServers.*.env` block, but refuses outright while any Claude Code
   process is running — detected by process, not by a lockfile, and fails
   safe (refuses on an ambiguous detection too, never a false "not running").
   **Quit every Claude Code window/session on this machine before this
   surface applies**, or its renames silently no-op this run; Claude Code
   holds the file in memory and rewrites it on exit regardless of what the
   script wrote moments earlier.

3. **Run `--scan-stale` as the last manual-sweep step.**
   ```bash
   <clone>/.venv/bin/python3 <clone>/deployment/scripts/migrate_to_solet.py --scan-stale
   ```
   Report-only, case-insensitive, over `~/.claude/`, this clone's own
   `.claude/`, and `CLAUDE*.md`. Three categories, because the fix differs by
   category — never auto-rewritten, in any of them:
   - **historical** (a transcript, log, or other append-only record —
     `projects/*/*.jsonl`, `file-history/`, `history.jsonl`, `jobs/`,
     `paste-cache/`, `backups/`, `*.pre-*`, and siblings): leave alone,
     always — rewriting one falsifies the record.
   - **live process state** (`~/.claude/sessions/<pid>.json`): not a
     transcript, but still never hand-edited — a hit here is fixed by
     relaunching that session, not by editing its state file.
   - **fixable** (everything else): a plain edit is safe, EXCEPT a memory
     fact — any `.md` under a `memory/` directory — where the filename, its
     frontmatter `name:` slug, and every `[[link]]` pointing at it must move
     together. A blind substitution breaks that triple and leaves dangling
     links; fix those by hand, one fact at a time, never with a bulk
     find-and-replace.

## Step 4 — restart and WAIT

**Prefer `apply_manifest` over a bare restart whenever this profile has a router.** Check
whether `macos_self_deployment_plugin` is in the clone's own plugin roster
(`<name> call service_interface::lifecycle_management_service::list_plugins`). If it is,
`apply_manifest` swaps in the pulled code through the per-solet blue-green router —
verifying before cutover, leaving `previous` as an intact rollback target, zero downtime for
this no-MCP CLI path. Search the knowledge base for "picking up new code without a bare
restart apply_manifest" via Step Zero for the exact call shape (`new_manifest` + `reason`). A
bare LaunchAgent restart is a full stop that drops in-memory state (blob storage by default);
it is not wrong, just the higher-cost path when a router is available.

**Expect this once, for a release whose own notes describe a change to the deploy
preflight or the swap path itself: the bare-restart path is required first, and
`apply_manifest` cannot be the one to install that release.** `apply_manifest`'s cutover
preflight runs INSIDE the still-running old process — it validates the candidate release
using the OLD code's preflight logic, not the new code it is about to activate. A release
that fixes a defect in that preflight logic ships a fix the old preflight is not running
yet, so the very call that would install the fix is evaluated by the broken logic the fix
replaces. This is not a new failure mode to diagnose: fall back to the bare LaunchAgent
restart below, once, for this release only; `apply_manifest` clears normally on every
release after it. Read this release's `RELEASE_NOTES.md` before Step 4 to know whether it
applies before you hit the failure.

If that plugin is absent, this solet has no router and no blue-green path
(single-color by design) — restart the LaunchAgent directly:

```bash
launchctl unload ~/Library/LaunchAgents/local.solet.<name>.plist
launchctl load ~/Library/LaunchAgents/local.solet.<name>.plist
```

The blue-green router (if this profile has one) is a separate KeepAlive
LaunchAgent and is deliberately left alone regardless of which restart path you took. Then
wait for startup to finish before ANY query: `wc -l <newest log> && sleep 5 && wc -l <newest
log>` until the two counts match. Startup is also when changed knowledge bases re-ingest
automatically (content-hash comparison) — no manual re-ingest step exists or is needed.

## Step 4a — configure the export/workspace root (required if this release adds business-connector containment)

**Do this before any business-connector use, on ANY already-hydrated install.** A release
that adds or extends business-connector containment (export-root validation, per the
architect ruling on business-connector data boundaries, filed in this checkout's
`workbench/` directory under that date; §3) flips every covered connector's default from
"works" to "refuses until a workspace root is configured" the moment this update lands —
an already-hydrated clone never goes through the hydration runbook's export-root prompt
again on its own, so this step is the only thing that closes that gap for an EXISTING
install. Skipping it does not mean a smaller safety margin; it means every covered
business-connector read fails loud on first use post-update, for an operator who was never
told why. The release notes say when a given update actually touches this (not every
release does); when they do, treat this step as a prerequisite, not a recommendation.

If a workspace root was already configured (an earlier hydration or a previous run of this
step), this step is a no-op check, not a re-ask — skip straight to verification below.
Otherwise, ask the operator in plain words: "Where do you keep the folders you work in day
to day — the parent directory, not any one project?" (a `~/Workspace`-style directory:
stable, singular, covers every future job folder). Then call the real validator rather than
hand-rolling the check — it rejects a root that is, contains, or is contained by `app_home`
(naming which direction failed), and otherwise persists the root into every business-connector
plugin actually installed in this clone, additively and idempotently:

```bash
<clone>/.venv/bin/python3 -c "
from pathlib import Path
from github_midwife_plugin.export_root_validation import configure_export_root
written = configure_export_root(Path('<clone>'), '<clone>/profile', '<operator's answer>')
print(written)
"
```

Verify: the printed dict names every connector plugin that got the new root, and is never
empty on a clone that ships at least one business-connector plugin — an empty result means
none of the connector plugin directories were found under `<clone>/plugins/`, which is worth
a second look, not a silent pass.

## Step 5 — re-run the hydration steps the release changed

Updates that only change platform code end here. Updates that change the
OPERATOR-SIDE artifacts — the generated `AGENTS.md` / `CLAUDE.md` blocks, the
user-scope `~/.claude/settings.json` hooks, the rename skill, the fleet
functions, **and the session launchers themselves (`client/bin/claude-<name>`,
`client/bin/codex-<name>`)** — need the matching hydration steps re-run once.

⚠ **The launchers were added to that list 2026-08-20, and the omission had
teeth.** A launcher is rendered ONCE at genesis from
`claude_launcher.template`; a `git pull` updates the TEMPLATE in the clone and
touches the rendered launcher never. So a release whose entire operator-visible
value is a launcher change — this release's permission-mode default flip is
exactly that — lands, deploys, seals, publishes, gets pulled, and still leaves
the operator running the byte-identical launcher they had before, with nothing
anywhere reporting a gap. If you are updating across a release that changed a
launcher template, **re-running hydration Step 2 is the delivery, not a
formality.** The hydration runbook's steps are
idempotent by design: probes first, marker-based structural merges, never
clobber. Re-run its Step 2 (and Step 4a if the operator uses fleet roles);
the markers replace the old solet-owned pieces in place and leave
everything else alone.

**Evaluated, not built: can the update flow detect a stale rendered launcher
itself, rather than relying on this paragraph being read?** (seed feedback
#30/§48.3, 2026-08-24). The shape that would work: stamp each rendered
launcher with a comment naming a content hash of the template it was
rendered from (`# rendered-from: claude_launcher.template@<sha256>`), then a
Step 7 check re-hashes the CURRENT template and compares against the
deployed launcher's stamped value — a mismatch means the template moved and
the launcher didn't follow. **Deliberately not implemented this pass**,
for three reasons that together move this past "small": (1) it requires
changing `_rendered_files`/`_render` in
`plugins/github_midwife_plugin/src/github_midwife_plugin/setup_shell_operations.py`
— genesis-path code shared
by every render this runbook's Step 5 also depends on, not a leaf function;
(2) every EXISTING install predates the stamp and would need a defined
"unknown, assume stale, recommend re-hydration" reading rather than a false
"not stale" default — the failure-mode cost of getting that fallback wrong
is an operator trusting a launcher that silently is not the one they think;
(3) the comment must land somewhere `_merge_block`/marker-based re-render
won't treat as an operator edit to preserve across the NEXT hydration run,
which is exactly the kind of interaction this doc's own marker-merge
machinery has previously gotten subtly wrong. **Recommendation:** worth
building once another release needs the same genesis-render touch point
(amortizing the shared-function risk across two reasons to touch it), not
as a standalone change justified by this one gap alone. Until then, this
paragraph — read at update time, not detected at runtime — is the
mechanism.

**Exact re-render of the fleet file and of a hand-edited feedback skill (r57).**
Since r58 the Manager path in Part A refreshes the fleet launcher itself; these
steps stay as the fallback, for a launcher the preview listed as edited or of
unknown origin, and for the manual path. Both are plain renders you can do yourself with the solet's name in place of
`<name>` and `<clone>` for its directory. Replace tokens by literal text
substitution only, never `str.format` and never a shell heredoc (the templates
carry live `$VAR` references). Take each backup path from `test -f`, adding
`-HHMMSS` if it already exists; never overwrite a backup.

*The `/feedback` skill.* The Manager path in Part A already replaces the r56
skill; do this only when the preview listed it as edited, or when you are on
the manual path.

1. Read `~/.claude/skills/feedback/SKILL.md`. It is the broken r56 copy if it
   has no heading `What you must NOT attempt`; the fixed skill always has one.
2. Copy it to `~/.claude/skills/feedback/SKILL.md.pre-r57-<YYYYMMDD>`. If you
   had edited it, show the operator what they changed (a diff against the
   r56 template with the name filled in) so they can say what to keep.
3. Read `<clone>/plugins/github_midwife_plugin/knowledge_base/hydration_templates/feedback_skill_SKILL.md.template`,
   replace its one `{{SOLET_NAME}}` with `<name>`, write the result to
   `~/.claude/skills/feedback/SKILL.md` (mode 0644), and re-apply any kept
   edit on top.
4. Check: the file's first line is `---`, and
   `grep -c 'What you must NOT attempt' ~/.claude/skills/feedback/SKILL.md`
   prints `1`.

*The fleet file*, only if `<clone>/client/<name>-fleet.zsh` exists. The file
holds two things: the launcher functions from the template, and the role
functions you (or the operator) wrote below the line `# One function per role
the operator chose in Step 4a.` Replace the first and keep the second.

1. Read the file's `GIT_CONTROLLER_NAME="..."` line. The quoted value is the
   operator's Step 4a choice. No such line means a solo deployment.
2. Copy the file to `<clone>/client/<name>-fleet.zsh.pre-r57-<YYYYMMDD>`.
3. Render `<clone>/plugins/github_midwife_plugin/knowledge_base/hydration_templates/fleet_functions.zsh.template`
   in memory: replace every `{{SOLET_NAME}}` with `<name>`; replace
   `{{GIT_CONTROLLER_NAME}}` with the value from step 1, or, for a solo
   deployment, delete the whole line `  GIT_CONTROLLER_NAME="{{GIT_CONTROLLER_NAME}}" \`.
4. In the existing file, replace everything above the line `# One function
   per role the operator chose in Step 4a.` with the same region of the
   render, and leave that line and everything below it untouched.
5. For each existing `claude-<name>-restart-<role>` function, add the line
   `tmux kill-session -t =<Role> 2>/dev/null` before its final
   `_claude_for_<name> <Role>` call, as the template's commented restart
   example shows. Role functions themselves need no change.
6. Check: `zsh -n <clone>/client/<name>-fleet.zsh` prints nothing, and
   `grep -c '_tmux_host_for_<name>' <clone>/client/<name>-fleet.zsh` prints
   at least `2`. Tell the operator to open a new terminal: a shell that
   already sourced the old file keeps the old functions.

A release that ADDS a plugin needs one more route. Step 3's editable install
puts the new code in the venv, but the pull never touches the clone's
genesis-written `profile/config/manifest.yaml`, so the plugin stays
installed-but-inert until it is listed there. Add it to that manifest's
`plugins:` list, restart again (Step 4), then run the hydration runbook's
Step 4c for it — the `hydration_guidance.md` glob picks up the new plugin's
activation work and first-use credential contract.

A newly added plugin also has no config file: the pull never writes
`profile/config/plugins/<plugin>.json`. The checked-in starting point is
`plugins/github_midwife_plugin/knowledge_base/profile_baseline/<plugin>.json`
(a baseline exists only for the plugins listed there). Copy it into place only
if the live file is absent — never over an existing one:

```bash
cp -n <clone>/plugins/github_midwife_plugin/knowledge_base/profile_baseline/<plugin>.json \
  <clone>/profile/config/plugins/<plugin>.json
```

A plugin whose `plugin.yaml` declares a default for every field it requires
activates without the file (`macos_inference_plugin` does from r57; before
that, activation failed on `context.attachment_scan_limit`). A field with no
default anywhere — `coreai_embeddings_plugin`'s `asset_root` — still needs the
operator's answer: without it the plugin raises `asset_root must be an
absolute directory path` when it prepares.

## Step 6 — the four stale copies a restart alone does not refresh

A restart makes the platform run the new code. It does not make every
already-running client execute it. Four separate copies sit downstream of
"pulled and restarted," and each needs its own refresh.

**1. The installed Claude Code plugin runs from a CACHE COPY, not the source
tree.** Per vendor docs (`code.claude.com/docs/en/plugin-marketplaces`):
*"when users install a plugin, Claude Code copies the plugin directory to a
cache location."* `${CLAUDE_PLUGIN_ROOT}` resolves to that cache copy:

```
~/.claude/plugins/installed_plugins.json     # installPath, version, installedAt, lastUpdated
~/.claude/plugins/cache/<marketplace>/<plugin>/<version>/   # the copied bytes
~/.claude/plugins/marketplaces/<marketplace>/               # the catalogue clone
```

A fix can land, deploy, assemble, seal, publish, and be pulled onto the
target machine — and that machine can still run the OLD hook. Every
upstream artifact reports success; the failure is silent and invisible from
our side, because nothing local can observe which copy a remote machine
executes.

Verify by diffing the executing bytes against the pulled deployment source —
never trust that a refresh happened:

```bash
python3 -c 'import json,sys; d=json.load(open(sys.argv[1]));
print([e["installPath"] for k,v in d["plugins"].items() if k.startswith("coordination-hooks@") for e in v])' \
  ~/.claude/plugins/installed_plugins.json

diff -r -x '__pycache__' -x '*.pyc' \
  "<installPath-from-above>/hooks" \
  "<clone>/plugins/github_midwife_plugin/claude_plugin/coordination-hooks/hooks"
```

Empty output = the cache copy's hooks match the shipped hooks. Any output =
the fix is dormant regardless of what the pulled seed says. Scope the diff
to `hooks/`, not the whole plugin directory: Claude Code injects its own
`.claude-plugin` / `.in_use` markers at the plugin root and omits some
source-only files (e.g. `LICENSE`), so a whole-directory diff fires even on
a correctly-refreshed install.

✅ **CONFIRMED, direct measurement (2026-08-02, isolated scratch clone, global
state restored byte-identical afterward):** the cache path is version-keyed
(`cache/<plugin>/<version>/`), and neither `claude plugin install` on an
already-installed plugin nor `claude plugin update` re-copies changed hook
content when `plugin.json`'s `version` is unchanged — `install` reports
"already installed," `update` reports "already at the latest version," and
in both cases the cache bytes are untouched. Only `claude plugin uninstall`
followed by a fresh `claude plugin install` forces a real re-copy regardless
of version. **Named fix: bump `plugin.json`'s `version` on every shipped
hook change**, as a step in the *release* procedure — a refresh the operator
cannot trigger from their own side is not their step to own. Recovery
without a version bump: `claude plugin uninstall
coordination-hooks@<marketplace-name> --scope local && claude plugin
install coordination-hooks@<marketplace-name> --scope local` — the diff
check above is what tells you whether this is actually needed.

⚠ **SUPERSEDED, 2026-08-16.** An earlier revision of this section stated that
`coordination-hooks/.claude-plugin/plugin.json`'s `version` "has never moved
past `0.1.0`." That was true when measured on 2026-08-02 and is false now:
the version-bump fix was adopted and has been followed diligently — **13
bumps, `0.2.0` (2026-08-02) through `0.5.6` (2026-08-14)**, verified from
commit history. Do not plan against the old claim.

**The correction matters more than the date, because the bump discipline was
impeccable and the machine was still two versions behind.** Measured
2026-08-16 on the development host: `installed_plugins.json` pinned `0.5.4`
with `lastUpdated` 2026-08-13, while the source manifest read `0.5.6` and the
pinned `gitCommitSha` was **146 commits behind master**. An update *had* run
on 08-13 and correctly picked up `0.5.4` — so the mechanism is proven to work
on that host. Then two further bumps landed with no update behind them, and
the diff check reported **five** stale hook files, not one.

**A VERSION BUMP ARMS THE REFRESH; IT DOES NOT PERFORM IT.** Bumping only
makes the next `claude plugin update` willing to re-copy. Somebody still has
to run it. A release procedure that ends at the bump records an *armed*
state as though it were a *fired* one — and the gap is silent, because the
repo reads correct at every version while the installed process keeps
executing old code. **Every shipped hook change therefore needs three
steps, not one:**

1. **BUMP** `plugin.json`'s `version` (release-side).
2. **FIRE** the refresh on each affected host — `claude plugin update
   coordination-hooks@<marketplace-name>`, or the uninstall-then-install
   pair above when the version did not move.
3. **VERIFY IT FIRED** — never infer it from step 1 or 2 completing:

```bash
python3 -c 'import json,sys; d=json.load(open(sys.argv[1]));
print([(e["version"], e.get("gitCommitSha")) for k,v in d["plugins"].items()
       if k.startswith("coordination-hooks@") for e in v])' \
  ~/.claude/plugins/installed_plugins.json
```

The reported `version` must match the source manifest, and the reported
`gitCommitSha` must be the commit you expect to be running — check it with
`git merge-base --is-ancestor <sha> master` plus a commit-count, not by
reading the repo file you just edited. Then re-run the `diff -r` check above
and require empty output. A bump whose version the install never picked up
looks identical, from the repo side, to one it did.

**Cut the new version AFTER the hook change lands, not before.** A version
already bumped before the edit does not cover it: on 2026-08-16 the `0.5.6`
bump (2026-08-14) predated two later commits that both modified the vendored
hook, so a reinstall at `0.5.6` would have shipped a hook missing both. Check
with `git log <bump-commit>..master -- <vendored-hook-path>` and require empty
output before treating a version as covering.

**Reinstall only from a clean tree.** When the marketplace source is a
`directory` entry pointing at a working checkout — the common local
arrangement — the install snapshots whatever the working tree contains at
that moment, uncommitted and unrelated dirty files included. Confirm a clean
tree first, or install from a source that cannot carry work in progress.

⚠ **Declarative `extraKnownMarketplaces` + `enabledPlugins` alone — the
mechanism `01_hydration_runbook.md` currently relies on, with no explicit
`plugin install` step — behaves THREE different ways depending on exact
invocation shape, all three directly measured, none of them the CLI-install
path Reviewer-B originally confirmed works.** Named precisely, because
collapsing these into one "declarative doesn't work" claim would itself be
false precision:

| Invocation (exact flags measured) | Outcome |
|---|---|
| Headless (`-p`), `--setting-sources local`/`project` reading `.claude/settings.local.json`/`settings.json` from the cwd (Reviewer-B's method) | Marketplace never registers — `installPluginsForHeadless: no marketplaces declared`. `enabledPlugins` entry left "orphaned." Nothing installs, nothing loads. |
| Real TTY, interactive, trust dialog accepted, `--setting-sources local` (this session's probe) | `extraKnownMarketplaces` self-registers the marketplace. `enabledPlugins` produces a **broken** `installed_plugins.json` entry claiming a cache `installPath` that is never actually created on disk — confirmed after both an abrupt kill and a graceful `/exit`, ruling out a race. Hooks at a nonexistent path cannot execute; failure is silent. |
| Headless (`-p`), `--settings <externally-supplied file>` instead of `--setting-sources` alone (Reviewer-D's original method, independently reproduced this session against the current, fixed template) | Marketplace registers via a distinct `installPluginsForHeadless` reconcile pass (confirmed in `--debug` log: *"Added marketplace source"*, *"Read hooks.json for plugin coordination-hooks... from"* the **source** path, not a cache path). Plugin loads and hooks execute for that session — but ephemerally: no cache directory is created, and no persistent `installed_plugins.json` entry survives. This is Reviewer-D's "hooks execute directly from source, no cache" finding, now independently confirmed rather than just cited — and it explains her result cleanly: her method never goes through the cache-copy code path at all, so there was nothing for the cache to go stale. |

**None of these three is a clean stand-in for what a real operator's first
session actually does** — every measurement here (including Reviewer-D's
and Reviewer-B's) deliberately scoped `--setting-sources` or substituted
`--settings` specifically to avoid touching this machine's real global
`~/.claude/settings.json`, which is necessary test hygiene but means true
default, all-scopes-merged, no-special-flags resolution — the actual
production path when the operator's rendered `~/.claude/settings.json` is
just sitting there and they launch `claude` normally — **remains untested,
labeled OPEN, not resolved.** Testing it directly is blocked on the same
constraint already on record: overriding `$HOME` to sandbox a user-scope
test breaks Claude Code login on this machine
(`reference_a_scoped_home_override_breaks_claude_code_login`).

Only the explicit `claude plugin marketplace add <clone> --scope local` +
`claude plugin install coordination-hooks@<marketplace-name> --scope local`
CLI sequence (Reviewer-B's originally measured path) reliably produces a
complete, persistent, working cache copy in every configuration tested.
**This is a defect in the runbook's current install mechanism, not just an
update-refresh gap — flagged for whoever owns `01_hydration_runbook.md`'s
install step next; not fixed here, out of this article's scope.** The
version-bump fix above is method-independent of all of this and does not
wait on it: it only matters once a working install exists to go stale in
the first place.

**2. The MCP bridge subprocess a client already has open.** A blue-green
swap reconnects the bridge to the new colour; it does not respawn the
client-side subprocess that spawned it. A client already running keeps its
OLD subprocess until the client itself relaunches.

**3. An armed watcher (`<name> watch`).** Not re-armed by a swap — relaunch
the session that ran it. Verify against the live session id (the watcher
spool path matches it), never against the fact that a watch command was run
at some point.

**4. Knowledge-base chunks indexed from files the release REMOVED.** The
startup auto-install pass re-indexes a knowledge base when a surviving
file's mtime moves past the install record's `indexed_at` — it walks the
files that exist NOW, so a release that only deletes content from a KB is
invisible to it: the pull removes the files, the restart finds nothing
modified, and the old chunks keep answering searches indefinitely. The
`update` verb has the same blind spot — it collects changed files from
disk, and a vanished file is not on disk to collect. The deterministic
re-ingestion step, for every knowledge base the release notes name as
having content removed, is a re-install:

```bash
<solet-name> call service_interface::knowledge_service::install '{"name": "<kb-name>"}'
```

Re-install is the documented idempotent path: it drops the KB's entire
chunk set by the KB's own tag (embeddings included) and re-indexes from
the files now on disk, so a removed article cannot survive it. Verify
with a negative search afterward — a query that used to retrieve the
removed content must no longer surface it. "The restart re-indexed" is
not evidence for a deletion-only change; nothing in the restart path can
see one.

Until this wave runs — plugin cache refresh, client relaunch, watcher
re-arm, KB re-install where the release removed content — no observation
from an old session or an old plugin cache measures the new code. A green
result taken before the wave completes is scope-class false: it is
evidence about the copy that no longer matters.

## Step 7 — verify

- `<name> health` answers and a KB search returns content from the new
  release (search for a phrase the release notes mention).

Known post-restart state on blue-green-router profiles: `<name> health`
returns HTTP 503 `no_active_color` through the router while the platform
itself is healthy (a direct `curl` of the platform's own ephemeral bridge
port answers `200`). That is the router activation race: the new instance
registered before the router's 30s heartbeat GC expired the outgoing one,
so the one-shot cold-start auto-activate declined, and the GC then cleared
the active binding. On releases carrying the steady-state re-assert fix
(2026-07-23 and later) this heals itself within ~10 seconds — just re-probe.
On earlier releases, bounce `local.solet.<name>.plist` once more (the
second boot sees no active binding and self-activates); leave the router
LaunchAgent alone either way.
- A labeled session's rename skill arms `<name> watch`; its first line is
  `"watch": "armed"`. Ground truth for the role claim:
  `<name> call plugin::agent_messaging_plugin::peer_holds_role` with the role
  name AND the `agent_instance_id` from the armed line — never a raw
  peer-list entry.
- `git -C <clone> log --oneline -1` shows the release commit the operator
  expected.
- When this update re-pointed the remote (Step 2a): `git -C <clone> remote -v`
  shows the new URL for fetch and push, and a plain `git -C <clone> fetch
  origin` succeeds. Do this before declaring the update finished — a re-point
  that silently failed looks exactly like a healthy clone until the next
  release never arrives.
- Any feedback items you have open upstream: check this release's
  `RELEASE_NOTES.md` against them. Under the current feedback model an item is
  answered when its issue closes, and the release notes cite the issue numbers
  closed in that release — so the notes plus your own open-item list are enough
  to tell answered from still-open without asking anyone.
- When the release removed KB content: a search for a distinctive phrase
  from the removed material returns nothing from the affected KB. Run this
  AFTER Step 6's re-install — before it, a hit is the expected stale-copy
  signal, not evidence the update failed.

## Part D — release history

The sections below are narrative: what each release changed and why, kept
as history. They are not executable support — Part A's preview lists what
an update will do to this installation, and the doctor verifies it did.

## What changed in this release — LM Studio solets move to the Apple-native stack (r52)

- **Plugin transitions.** `solet-manager update` now moves a pre-r46 solet
  off LM Studio on macOS 27: embeddings to on-device Core AI, and summaries
  to Apple Foundation Models on macos-bizops. On macOS 26 both stay on LM
  Studio until the next release's update after the Mac reaches macOS 27. LM Studio and
  its models are never removed or changed. See "Plugin transitions" in Part
  A for the guarantees; a not-ready replacement defers to `needs_attention`
  and the next `update` retries it.
- **A verify-mode update now clears `needs_attention`.** Before r52, a
  `verify`-mode update at the already-verified release promoted without
  publishing, so a row once set to `needs_attention` stayed there; it now
  lands `verified` when the final doctor passes.
- **Embedding inputs fit the provider (iss_9166af93).** Core AI refuses any
  input over 2048 tokens where LM Studio accepted an 8192-character window.
  Every caller now splits to the provider's declared budget, and the
  embedding service refuses (loudly, counted) anything that still arrives
  over it. Once embeddings run on Core AI, the first ledger drain re-embeds
  every event whose stored chunks differ from the new policy's, which covers
  every event the old window embedded head-only or could not embed.
- **Bootstrap closure repair** resolves the vendored `apple-fm-sdk` wheel
  with the same `--find-links` rule genesis uses.

## What changed in this release — worker hooks now also fire as plugin hooks (`coordination-hooks` 0.8.0, 2026-08-24 update)

Closes seed feedback #40 (§51.1): a spawned worker on a host whose managed
policy sets `strictPluginOnlyCustomization: ["hooks"]` previously got a
worker that spawned healthy-looking and silently never registered, never
heartbeat, and never captured its session mapping — the policy strips the
host adapter's own `--settings`-injected copy of
`headless_tool_allowlist_gate.py` and `capture_session_mapping.py`, and
neither hook was registered anywhere else. `0.8.0` registers both directly
in `plugins/github_midwife_plugin/claude_plugin/coordination-hooks/hooks/hooks.json`
(`PreToolUse` for the allowlist gate, `SessionStart` for the session-mapping
capture, both unconditional —
no matcher), which is what survives that exact policy for a plugin already
listed in the operator's `strictKnownMarketplaces` (§43.1/#8's Case C
already proved this route for this plugin's other hooks).

**This is a plugin-cache update like any other — Step 6's bump/fire/verify
sequence applies as written, target version `0.8.0`.** After pulling this
release and refreshing the plugin cache (`claude plugin update
coordination-hooks@<marketplace-name>`, or the uninstall/install pair if the
version didn't move), re-run the spawn-time probe below to confirm the gap
is actually closed for your policy, not just that the version string moved.

**What a currently-running, already-spawned worker sees: nothing, either
way.** A live worker's hooks were fixed into its own `--settings` blob (or,
under the strict policy, silently absent) at spawn time; refreshing the
plugin cache underneath it does not reach into a process that is already
running and does not change what that process does before it next respawns.
The benefit of this update applies to workers spawned AFTER the refresh —
there is no in-place remediation for one already spawned degraded, only a
respawn.

**On whether the refresh itself can disrupt an unrelated live worker (a
worker running fine, on a host that never carried the strict policy, that
happens to be using this SAME plugin's already-registered hooks — e.g. the
git-mutation gate): measured on this checkout's own cache as of this
writing, no.** `~/.claude/plugins/cache/<marketplace>/coordination-hooks/`
carries eight prior version directories going back to `0.3.0`
(oldest orphan mark 13 days old at measurement time), each marked with an
`.orphaned_at` timestamp but **not deleted** — the superseded version's
files stay on disk. This is offered as a direct measurement on one
deployment, not a guaranteed platform contract: if your own cache shows
different retention behavior (an orphaned version actually removed), that
is worth a fresh report rather than assuming this note still holds — Claude
Code's own retention/GC policy for orphaned plugin cache versions is not
documented anywhere this runbook cites, and this note does not assert one.

**Verify the fix actually reaches your policy** (host adapters, not just the
installed cache): re-run §43.1/#8's own policy probe against a real spawn on
the affected host after the refresh lands, per the hydration runbook's Step
4a-ii. A `hooks.json` diff (Step 6 point 1's usual check) confirms the
CACHE holds the new registration; it does not confirm a spawned worker on
your specific managed-policy host actually benefits — those are different
questions, and only the spawn-time probe answers the second one.

Full detail: this release's `RELEASE_NOTES.md` at the repo root; closed
issues #40 (§51.1) and #28 (§48.1, the `degraded_hooks_acknowledged`
parameter-stripping rider that shipped in the same release).

## What changed in this release — the seed's new home and a new feedback channel (2026-08-13 final update at the old repository)

Two adopter-facing changes ride along with this release, neither of which
touches platform code.

**1. The seed moved.** This is the last release published at the repository
this clone was born from. **Step 2a above is the action this requires** — one
`git remote set-url`, taken after the pull, with the new URL read from this
release's `RELEASE_NOTES.md`. Skipping it costs you nothing today and every
future release after that: a clone still pointed at the old URL will keep
reporting "already up to date" forever, which is indistinguishable from "no new
releases" and is the failure mode this step exists to prevent. If access to the
new home is not working, that is an operator conversation, not a workaround.

**2. Feedback is filed as GitHub issues now, not as a pull request carrying a
numbered document.** The item vocabulary you already use is unchanged — rounds
are still `Part N`, items are still `§N.M`, the four item classes are the same,
and the evidence and content rules are the same. What changed is where an item
goes: each item is filed through the repository's issue form for its class,
a multi-item round gets a parent issue whose body carries a checklist of the items,
and a design proposal — an RFC-shaped document — is a feature-request issue
carrying the design in its body. **The repository accepts no pull requests and
no patches** (see `CONTRIBUTING.md`); Apache-2.0 already lets you fork and
change your own clone, so nothing about that policy limits what you can build.
Answers come back as issue closures and GitHub releases; subscribe to the new
repository's releases rather than polling it.
The full procedure is in the upstream feedback runbook, which was rewritten in
this release; read it before your next round rather than working from memory of
the old convention.

This one is worth a verification, because the guidance it replaces is guidance
your solet may still be answering searches with. The feedback runbook was
MODIFIED rather than deleted, so its file mtime moves and the startup pass
re-indexes it normally — this is not the deletion-blind case Step 6's fourth
stale copy describes, and no `install` call is required. Confirm rather than
assume: after the restart, search your own knowledge base for the old
convention (a phrase like "seed feedback pull request convention numbered
parts"). You should get the CURRENT issues-only guidance back — every item,
design proposals included, filed through an issue form. If you get either of
the superseded conventions instead — pull-request-per-round, or the
2026-08-13 hybrid where designs alone travelled as a pull request — the
re-index did not take, and that is when Step 6's re-install applies:

```bash
<solet-name> call service_interface::knowledge_service::install '{"name": "github_midwife_plugin"}'
```

## What changed in this release — the solet rename identifier cutover (2026-08-13 update)

Every live identifier renamed from its homunculus-era form: the CLI is now
`solet` (`solet call …`, `solet health`), the environment family is
`SOLET_*` (`SOLET_NAME` foremost), the root manifest key is `solet_name:`,
launchd labels are `local.solet.<name>`, the lifecycle verbs are
`birth_solet` / `teardown_solet` / `provision_solet`, and result envelopes,
error codes, and process JSONs follow. No aliases ship. **Step 3a is
mandatory for this update** — run the migration before restarting, or the
platform will refuse to boot (by design, with an error pointing back here).
Two spellings are deliberately unchanged: the MCP wire route
`notifications/homunculus/peer_message` and the channel `source="homunculus"`
attribute are frozen protocol names (a rename there is a versioned protocol
change, explicitly deferred), and history — ledgers, memories, old
`homunculus_decommissioned:` tag rows, workbench notes — is never rewritten.
Knowledge-base articles were renamed in place; because a rename is a
delete-plus-add, the startup staleness check cannot see the deletions —
Step 6's knowledge-service re-install with a negative-search verification
applies to this update.

## What changed in this release — origin working-corpus removal from the shipped thinking KBs (2026-08-12 update)

The seed no longer ships the minting origin's own pre-product working
corpus inside `default_thinking_plugin`: composition designs, sketch
packets, dated working plans, WBS specifications, and one legacy
creative-domain plan template — material from the origin's earlier
creative work that was never part of the business-ops product surface.
Two shipped knowledge bases lose indexed articles and gain nothing:
`thinking_plans` (its `plans/` and `wbs/` articles) and `plan_templates`
(its single legacy template). The same release also stops shipping two
never-indexed artifact directories in the same plugin. The KB
registrations and the thinking system prompt still ship; an empty
article set is a store's normal newborn state (a sibling thinking KB
already ships registration-only).

This is exactly the deletion-only KB change Step 6's fourth stale copy
describes: pull plus restart leaves every previously-indexed article
still answering searches. After the restart, run the re-install pair,
then the negative check:

```bash
<solet-name> call service_interface::knowledge_service::install '{"name": "thinking_plans"}'
<solet-name> call service_interface::knowledge_service::install '{"name": "plan_templates"}'
```

Then search for a phrase only the removed corpus contained (any
composition-specific phrase your deployment used to retrieve). A hit
from either KB means the re-install has not yet run against the store
actually serving your searches.

## What changed in this release — multi-session self-management adoption, connector write reversal (2026-08-10 update)

This release makes the full fleet-lifecycle stack from this seed's multi-agent
session management (see the 2026-08-08 delta below) something an ALREADY-LIVE
solet can actually operate, not just receive as dormant capability.
Three things this update needs beyond the base Steps 1–7, if this
solet is going to spawn or manage other sessions of itself:

**1. Refresh the `coordination-hooks` Claude Code plugin and verify you
land on `0.5.0` — that is the version this update targets, not an
intermediate one.** The version story, in one line: `0.3.1` = the
bounded-wait fix bump; `0.4.0` = heartbeat + rotation-due vendored;
`0.4.1` = memory-passthrough + origin ladder vendored; `0.5.0` = the two
remaining spawn-injected worker hooks plus the fail-loud resolution
ladder that makes a *programmatically spawned* worker's hooks actually
resolve on a born clone. **"After updating, verify `0.5.0`" is the
single instruction.** In full: `0.2.0` (pre-release baseline) → `0.3.0`
(four reminder hooks ported from `node` to `python3`, closing an
undeclared-runtime defect — see this release's `RELEASE_NOTES.md`) →
`0.3.1` (bounded the `Stop`-bound idle-wake waiter's wait, default 2400s
via `AGENT_WAKE_MAX_WAIT_S`, fails loud on a bad override rather than
silently) → `0.4.0` (vendors the heartbeat and the rotation-due watcher
into the plugin) → `0.4.1` (vendors the memory-passthrough sync set and
the origin-resolution ladder into this plugin, `6f840d7d7`) →
**`0.5.0`, the version this update is actually about**: ships
`headless_tool_allowlist_gate.py` and `capture_session_mapping.py` — the
two hooks a **programmatically spawned** worker needs (`spawn_session`,
distinct from every hook discussed so far, which cover interactively-launched
fleet sessions only) — and, in the same landing (`248a1294a`, merged
`a83a4baa2`), a two-rung resolution ladder in both host adapters
(`headless_adapter.py`/`tmux_adapter.py`) that fixes a real defect: a
born clone ships no `.claude/hooks/` directory at all, so a spawned
worker's generated hook settings previously pointed at a nonexistent
path and every one of that worker's tool calls was silently blocked from
its first turn. The ladder now resolves the checkout copy first, falls
back to this plugin's own shipped copy on a clone with no checkout
`.claude/hooks/`, and **refuses the spawn outright, loudly**, if neither
resolves — see the hydration runbook's Step 4a-ii for the full mechanism
and a zero-risk verification recipe. Step 6 above ("the three stale
copies a restart alone does not refresh") is exactly what makes reaching
`0.5.0` real rather than theoretical: every one of `0.3.0` through
`0.5.0` is dormant on an existing install until its cache copy is
explicitly refreshed — an operator who pulls this update and only
restarts keeps running whatever pre-`0.5.0` copy their cache already
held, with no local signal anything is out of date. Run Step 6's diff
check against `0.5.0` specifically, not just "a newer version than
before," before trusting any of this update's self-management hooks —
interactive or spawned — are actually live.

**1a. Before first launch: export `SOLET_NAME` in the environment
Claude Code is launched from.** One export does double duty for this
release's self-management stack — set it once, in the same shell/terminal
you launch Claude Code from (the same convention this checkout's own
`CLAUDE.md` uses for the platform's own foreground launch: `SOLET_NAME=<name>
python -m ananta.cli --app-home <profile>`), and both of the following
resolve correctly without further setup:

- **Memory-passthrough origin resolution, rung 1.** The origin-resolution
  ladder that decides which solet a session's memory writes belong to
  checks `SOLET_NAME` first; without it, resolution falls through to
  rung 2 (`root_manifest.yaml`, only valid once genesis has rewritten it)
  or rung 3 (`CLAUDE_PROJECT_DIR`'s basename) — both work, but rung 1 is
  the reliable one and doesn't depend on either.
- **The heartbeat and rotation-due watch hooks' identity.** Both hooks
  (new in `0.4.0`/`0.4.1`) call into other platform verbs that also read
  `SOLET_NAME` for identity. (Separately: those two hooks additionally
  need `AGENT_INSTANCE_ID` exported to arm at all — without it they are
  silent no-ops, by design, not a bug; see `SECURITY.md`'s Configuration
  surface section, "Adopter setup note," for the exact arming variables
  and the full default-off/default-on table across all thirteen hooks.)

`SOLET_NAME` is a soft dependency for the origin ladder (rungs 2/3
still resolve it) but the only path to a reliable heartbeat/rotation-due
identity — export it before first launch rather than discovering the gap
later.

**2. This checkout's own project-scope `.claude/hooks/` stack is being
folded INTO the coordination-hooks plugin by `0.4.0`, not layered
alongside it.** Earlier in this release cycle, the heartbeat
(`heartbeat_report_alive.py`), the rotation-due watcher
(`rotation_due_watch.py`, now also feeding the context-gauge verb pair
below), and the memory-passthrough sync wrapper lived only as this
checkout's own operator-scope hooks, rendered by the hydration runbook
(`01_hydration_runbook.md`) rather than shipped in the plugin bundle. As
of `0.4.0` they vendor into the plugin itself — Step 1 above is what
delivers them to an existing solet, not a separate hydration re-run,
though Step 5 ("re-run the hydration steps the release changed") may
still matter if this release also changes anything on the operator-scope
side that `0.4.0` does NOT absorb; check the final hooks manifest below
before assuming Step 5 is a no-op here. **Verification that everything in
this hook stack actually ships in this seed's own bundle and manifest is
tracked separately, in this same overnight wave (`R4` seed-packaging
audit) — do not assume shipped from this document alone; confirm against
that lane's own report or by reading `capability_bundles.yaml` /
`seed_manifest.yaml` / the `assemble()` allowlist directly before relying
on it.**

**2a. After updating: verify your hooks actually fire.** Table below is
read directly from the **landed** `hooks.json`
(`plugins/github_midwife_plugin/claude_plugin/coordination-hooks/hooks/hooks.json`,
master HEAD `a83a4baa2`) — every row verified against that file, not
taken from any summary. **`0.5.0` changed this file's own `description`
field only** (disclosing the two spawn-injected hooks below) — the
event/hook wiring table itself is byte-identical to `0.4.1`, verified by
diff (`git diff 6f840d7d7 248a1294a -- .../hooks/hooks.json`).

| Event | Hook(s) | Confirms |
|---|---|---|
| `UserPromptSubmit` | `step_zero_reminder.py`, `check_messages_reminder.py`, `session_context.py` (3 independent hook entries) | The KB-first reminder, the check-your-messages reminder, and per-prompt session context all fire on every prompt you submit. |
| `SessionStart` | matcher `startup\|resume\|clear`: `check_messages_reminder.py`, `role_binding_reminder.py`; unconditional (any matcher): `session_context.py` | The check-your-messages and role-binding reminders fire on a startup/resume/`/clear`; session context fires on every `SessionStart`, including other matcher values not listed above (none currently defined). |
| `Stop` | `wake_waiter.py` (`asyncRewake: true`, `timeout: 86400`) | The bounded idle-wake waiter, `0.3.1`+, is wired and its wait is capped rather than open-ended. |
| `PreToolUse` | matcher `^(Bash\|Edit\|Write\|MultiEdit\|NotebookEdit\|Task\|Agent)$`: `git_controller_gate.py` | The git-mutation safety gate fires before every tool call in that matcher set — present since before this release, unaffected by `0.4.0`/`0.4.1`. |
| `PostToolUse` | unconditional: `rotation_due_watch.py`, `heartbeat_report_alive.py` (2 independent hook entries, fire after every tool call); matcher `Write\|Edit\|MultiEdit`: `capture.py` | The rotation-due watcher and the heartbeat both fire on every tool call, new in `0.4.0`. `capture.py` (the memory-passthrough capture hook, new in `0.4.1`) fires only on file-mutating tool calls. |

**Four CLI utilities ship in the same `hooks/` directory but are
deliberately NOT hook-wired** — `drain.py`, `hydrate_render.py`,
`index_render.py`, `sync.py` are agent-invoked directly (see the
memory-passthrough hydrate/drain procedure in `CLAUDE.md`/`AGENTS.md`),
not triggered by any Claude Code event. Do not expect them in the table
above or treat their absence as a wiring gap.

**`headless_tool_allowlist_gate.py` and `capture_session_mapping.py`
(new in `0.5.0`) will never appear in the table above either — by
design, not omission.** Both ship in `hooks/` as this plugin's shipped
fallback copy, but neither is registered in `hooks.json` — they are
never fired by an interactively-launched fleet session at all. A
**programmatically spawned** worker (`spawn_session`) gets them, plus
six other hooks already in the table above, wired into its own
per-spawn generated `--settings` blob instead — a completely separate
delivery path from everything else in this table. See the hydration
runbook's Step 4a-ii for the full mechanism, the two-rung resolution
ladder that decides which copy a given spawn actually runs, and a
zero-risk verification recipe. Verifying THIS table (an installed
`hooks.json` diff) tells you nothing about whether a spawned worker's
hooks are wired correctly — that needs the spawn-time probe Step 4a-ii
describes, not a cache-copy diff.

⚠ **SUPERSEDED, `0.8.0` (2026-08-24, seed feedback #40/§51.1).** The
paragraph above was true for `0.5.0` through `0.7.0` and is false from
`0.8.0` on: both hooks are now ALSO registered in `hooks.json` (`SessionStart`
for `capture_session_mapping.py`, `PreToolUse` for
`headless_tool_allowlist_gate.py`, both unconditional — no matcher). The
reason is a gap `0.5.0`'s design didn't anticipate: a managed policy setting
`strictPluginOnlyCustomization: ["hooks"]` strips a spawned worker's own
`--settings`-injected copy of every hook, these two included, so a worker
spawned on a host carrying that policy got silent no-ops — no registration,
no heartbeat, no session mapping, ever (§43.1/#8's original report). A
plugin already listed in the operator's `strictKnownMarketplaces` keeps
firing its OWN `hooks.json`-registered hooks under that same policy
(proven for this plugin's other hooks in §43.1/#8's Case C), so registering
these two closes the gap for exactly the host class that needed it. See the
0.8.0 entry below for the full table and the double-fire note for hosts
where BOTH routes are still live.

A quick self-check any adopter can run against their own installed cache
copy, not just the source tree: `python3 -c 'import json; d=json.load(open("<installed cache
path>/hooks/hooks.json")); [print(e) for v in d["hooks"].values() for g in
v for e in g["hooks"]]'` against their own installed cache path (found via
Step 6's `installed_plugins.json` lookup above) shows every command their
own copy will actually run.

**3. The maintenance-verbs joseki cards ARE the operating manual for the
fleet verbs below — read them before improvising a sequence.** Search this
solet's own knowledge base for "maintenance verbs joseki cards"
(source: `ananta_platform`, `24_operator_communication/09_maintenance_verbs_joseki_cards.md`).
It carries the exact ordered-call sequence, verify step, and known traps
for worker rotation, worker restart, memory-passthrough sync, memory-head
curation, KB/process refresh, and context-gauge checks. Follow the card;
do not re-derive the sequence from the verb names alone — several of these
verbs have a non-obvious trap (see the quickstart below) that the card
exists specifically to prevent.

**Fleet-verbs quickstart**, all under `plugin::agent_messaging_plugin::`,
all documented in depth by the joseki cards above — this is an index, not
a substitute for reading them:

| Need | Verb(s) | One trap worth knowing before you call it |
|---|---|---|
| Spawn a new worker session | `spawn_session` | Already drives one automatic first turn — do not also hand-drive a first turn unless no lane charter is on file. |
| Rotate a worker in place (clear + redrive, same process) | `peer_send_by_name` (pickup pointer) → `clear_session` (`park=False`) → `drive_session` | Rotate uses the spawn-time LEDGER `agent_instance_id`, never the `agi-watch-*` id from a role-thread message — they are different values for the same worker. |
| Restart a dead/hung worker (kill + fresh process) | `session_status` → `terminate_session` → `spawn_session` | A hand relaunch that bypasses `spawn_session` (e.g. raw pane injection into a terminal) does NOT get the automatic first-turn drive for free and needs one explicitly. |
| Park a worker (clear and leave idle, deliberately) | `clear_session` with `park=True` | Its role binding and any armed watcher survive a park — a parked worker is not a terminated one. |
| Check a session's context-window occupancy | `session_context_status` | A `resolved: false` result is the expected shape for an operator-hosted seat (a disclosed gap, not a bug) — never estimate a number in its place. |
| Curate the ambient `MEMORY.md` head at a rotation boundary | `generate_curation_report` → (seat-ratified) `reinforce_by_slug` | Every demotion is a seat judgment call — there is no auto-trim, by design. |

Verify any of the above actually took effect the way the joseki card's own
"Verify — do not skip" step says to — a queued delivery receipt confirms a
message was accepted, never that a turn actually ran.

**4. Postgres AND Snowflake connections can now write, if the registered
credential's own grants allow it.** One new verb per connector —
`run_statement` on `external_postgres_plugin`, `run_statement` on
`snowflake_plugin` (landed `2d562767b`, ancestor-verified against master
by this worker) — opens a non-read-only connection; every existing read
verb on both connectors is unaffected and stays strictly read-only.
Neither plugin performs a write-permission check of its own — the
connected database's own RBAC/grants are the entire control plane
(operator ruling 2026-08-09 + Amendment 1). Full detail is in each connector's
own overview article — `01_external_postgres_overview.md` and
`01_snowflake_overview.md`, both under "Read/write posture" — in the
corresponding plugin's knowledge base, if this seed carries those connectors.
**Two things about Snowflake's write verb
are open, not yet answered by measurement:** `RETURNING`-equivalent
clause support is object-dependent and uncharacterized here (the
connector rolls back rather than silently discarding rows if one produces
output with no export path given); and this release ships with no live
write smoke against a real Snowflake account — every registered
connection here is pinned to a read-only role, so a write was never
exercised end-to-end, only against a fake client. Confirm against
`01_snowflake_overview.md` before telling an operator either caveat has
been resolved.

Full detail: this release's `RELEASE_NOTES.md` at the repo root.

## What changed in this release — multi-agent session management, fleet transport default, and ledger fixes (2026-08-08 update)

This release (source commit `71159c02e1a4ce373db6561a30a1b4b00d0b0b91` through
`837ad3e359ce2b42041594f12cde98a78468148c`) needs **no extra steps beyond
Steps 1–7 above.** No plugin was added or removed, no dependency changed,
and every new database table installs itself automatically the next time
the platform starts (the standard idempotent schema-install path every
table already goes through — nothing to run by hand, nothing to verify
beyond the normal Step 7 health check).

What it actually contains, in case it's relevant to you:

- **A fuller multi-agent session management surface** — spawning, listing,
  checking on, dispatching follow-up work into, and retiring other agent
  sessions from your own, plus a durable per-session lifecycle record and
  automatic sweeps for sessions that go quiet. This is new *capability*,
  not a change to how a single, non-spawning session behaves. If you don't
  spawn sessions from this solet, nothing here affects you.
- **`default_fleet_transport` config knob, defaulting to `watch`.** This
  only governs how a *newly spawned* session receives messages going
  forward — it does not change how an already-running session you started
  before this update behaves. No action needed unless you spawn sessions
  and want a different transport; see
  `ananta/knowledge_bases/ananta_platform/24_operator_communication/06_fleet_launcher_session_configuration.md`
  for the knob if you need it.
- **`root_manifest.yaml` gains a `sanctioned:` entry for `.claude-plugin`.**
  Purely informational — it tells your own root-strictness check that this
  directory is allowed to exist (written by local Claude Code plugin setup,
  never shipped as content), so a fresh clone's first cutover gate doesn't
  block on its absence. You get this automatically with the pull in Step 1;
  no separate step.
- **Session-ledger duplicate/concurrency fixes.** Internal correctness
  fixes for how the ledger resolves two rows describing the same external
  event. Nothing you need to do; if you have your own tooling reading that
  ledger directly, behavior around duplicate rows is now deterministic
  rather than read-order-dependent.

**One known issue, still open** — running the full gate suite on a
freshly-born clone will show two smoke entries fail: they require local
paths (`.claude`, `.agents`) that this platform's own root-strictness
contract says a born clone never has. This is a pre-existing gap in the
gate register, not something this update caused or something an update
step here can fix. Expect 247/249 on a healthy clone; those two entries
are the exception, not a broken install.

**A second known issue, still open** — the `coordination-hooks` plugin
ships in the bundle and is enabled by default via the hydration path.
Four of its five hooks (the reminder hooks) are invoked via `node`; this
platform documents Python 3.13 as its dependency and never requires,
installs, or mentions node. On a host without node on PATH, those four
hooks do not run. **You will see most of this happen:** the two
`UserPromptSubmit` reminder hooks and the two `SessionStart` reminder
hooks fail to launch with a visible, on-screen error naming the missing
command, and the session continues normally either way. **The exception
is the `Stop`-bound idle-wake waiter — it fails without any indication at
all**, since its normal job is to wait quietly in the background, so a
failed launch and correct operation look identical from outside.
(Measured: three repetitions per hook-event type against a validated
harness with a positive control, on Claude Code v2.1.226; covers
hook-launch failure specifically, not a hook that launches and then
errors internally, times out, or hangs.) The fifth hook — the PreToolUse
git-mutation safety gate — is implemented in `python3`, not `node`, and
is unaffected either way; do not assume a missing node also removes your
git guard. Not fixed by this release, and no fix is scheduled by it.

**Check for a newly-untracked `.gitignore` after updating.** A cloned
seed previously never received one (the birth-time write was skipped
whenever a `.git` directory was already present, which is always true for
a GitHub-cloned seed) — this is now fixed. On an existing clone the file
arrives untracked, since genesis must never touch your git history;
commit it yourself when convenient. If you already had your own, this
does not touch it.

Full detail: this release's `RELEASE_NOTES.md` at the repo root.

## What changed in this release — business-connector reads now export to file by default, with limits

Business-system reads no longer return record-level data directly into agent context.
Postgres, Snowflake, Salesforce, Marketo, and Zuora reads now always write results to
the caller-supplied path configured in Step 4a — inspection is a deliberate act, not an
automatic side effect of the call. Every read across all eight business connectors (the five
above plus G Suite, Jira, and Schwab where applicable) defaults to 500 records per fetch, with
an informed-override path for callers who genuinely need more; within that limit, a connector
pages internally across the vendor's own per-call ceiling and delivers one complete result —
paging is never exposed as a caller-visible token or continuation parameter.

**G Suite and Jira are deliberately NOT part of the export-by-default change** — the operator
scoped the data-export requirement to connectors handling mass record exposure, and ruled that neither
G Suite reads nor Jira's company-internal-account data carry that risk the same way. Both
still get the 500-record default and override; they simply keep returning results directly
rather than exporting to a file. This is a design choice stated in this release's own migration
record, not an inconsistency to work around.

If this update reaches an already-hydrated install, **Step 4a above is the action this
change requires** — an unconfigured workspace root means every affected connector read fails
loud on first use post-update. See
`plugins/github_midwife_plugin/knowledge_base/01_hydration_runbook.md`, "What this
solet ingests and embeds," for what happens to results once they do reach a session.

## Reference

- `plugins/github_midwife_plugin/knowledge_base/06_seed_update_operator_guide.md`
  — the same procedure written directly to the solet's owner, for
  running the update at a terminal without a coding agent driving every
  step. Points back here for Part B's manual steps, which do need an agent.
- `plugins/github_midwife_plugin/knowledge_base/01_hydration_runbook.md` —
  the hydration steps this runbook re-runs selectively after an update.
- `plugins/github_midwife_plugin/knowledge_base/07_upstream_feedback_runbook.md`
  — how to report what this update got wrong, ask a question about it, or
  confirm a fix landed. Rewritten in this release to the issue-form model; it
  is also where the "subscribe to releases" instruction lives, which is how you
  learn a future update exists at all now that the seed has moved.
- `solet_cli/homebrew/README.md` — the Manager's Homebrew payload, the seed
  lock the formula installs (how `update --dry-run` knows a release exists),
  and the lifecycle acceptance that measures Part A on a cold host.
- `plugins/seed_factory_plugin/knowledge_base/02_seed_publish_runbook.md` —
  why re-mints are append-only fast-forward commits (the property the
  exact-candidate fast-forward and Part C Step 2 rely on).
- `plugins/github_midwife_plugin/src/github_midwife_plugin/venv_provision.py`
  — the editable-install provisioner (the property `dependencies_reconcile`
  and Part C Step 3 rely on).
- `plugins/github_midwife_plugin/claude_plugin/coordination-hooks/.claude-plugin/plugin.json`
  — the `version` field Step 6's open question is about.
- `code.claude.com/docs/en/plugin-marketplaces` — vendor documentation for the
  installed-plugin cache-copy behavior Step 6 cites.
- The original declarative-install, hooks-execute-from-source measurement that
  Step 6's three-way invocation table reconciles is a dated note in the
  ORIGINATING checkout's `workbench/` directory, which is never shipped in a
  seed. Step 6's table states every outcome it established, so a deployment
  needs no copy of the note.
- `RELEASE_NOTES.md` at the repo root — the full changelog for every
  release, including the ones summarized above.
