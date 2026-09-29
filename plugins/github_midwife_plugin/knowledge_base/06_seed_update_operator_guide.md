# Updating Your Solet — A Step-by-Step Operator Guide

Tags: knowledge:tag:seed_update, knowledge:tag:operator_guide, knowledge:tag:solet_lifecycle

Article Layer: 2

Article Role: operations_runbook

Article Tags: planning-stage:solet-lifecycle, evidence-category:operations-runbook, domain:local-solet, domain:client-deployment, consumer_profile:both

Embedding Description: Plain-language, no-jargon walkthrough for the solet's OWNER to bring an already-running solet up to date with a newer published seed release using the Solet Manager at a terminal — `brew upgrade` to get the update, `solet-manager doctor <name>` as the go/no-go before and after, `solet-manager update <name> --dry-run` to read what will change (it lists the component it will install, the files on your machine it will preserve, and refuses with the exact file names when it will not proceed — never force anything), then `--yes` with the fingerprint from the preview, which restarts and waits itself and tells you when it is verified; what the three exit codes mean and `solet-manager reconcile <name> --dry-run` when an update stops part way; the few things still done by hand (your own project's `AGENTS.md`/`CLAUDE.md`, shell integration and Claude Code hooks re-rendered by a coding agent, the first-time business-connector export/workspace root answer, quitting Claude Code before a rename migration, and the read/override/refusal checks for Marketo and Zuora that are only verifiable on your machine); and a closing note on shaping newly-authored joseki cards now that connector reads never return record values inline. Companion to `05_seed_update_runbook.md`, which is written to the coding agent performing the same update and carries the full technical detail and the legacy manual procedure this guide deliberately leaves out.

> **Status (2026-09-19, existing-solet import/update Step 7):** this guide
> is the Manager path. If your solet was never imported into the Manager,
> do that once first (the "Before you start" section says how); the manual
> `git pull` procedure this guide used to describe is kept only in the
> agent-facing runbook, as recovery for a checkout the Manager will not
> import.

## When to use this guide

You've been told a new version of your solet is available and want to
bring it up to date without losing anything it has learned or remembers.
This guide assumes no prior knowledge of how the update was built — just
that you have a solet already running on your computer.

**When NOT to use this:** if your solet won't start at all, or you
want a completely fresh instance, this is the wrong guide — ask whoever
gave you this document for the alternative.

**If you have a coding agent (Claude Code or Codex) available and you'd
rather have it drive the whole process:** point it at
`05_seed_update_runbook.md` in your solet's own knowledge base instead
— search for "seed update runbook" — that version is written directly to
the agent and covers everything below in more technical depth. This guide
is for running the update yourself, at a terminal, when that isn't how
you'd rather do it.

## Before you start

- You need the Solet Manager installed: `brew install
  solet-public/tap/solet`. Replace `<name>` with your solet's name and
  `<folder>` with the folder it lives in, throughout this guide.
- **If you installed your solet with `solet create`** (the usual way),
  there is nothing to enroll: go straight to Step 1. The first update
  recognises the solet the Manager installed and enrolls it as part of the
  same command; its preview says so in an `enrollment` section. If that
  first update is interrupted, run it again: the preview shows
  `enrollment: resume` with the same fingerprint, and `--yes` finishes it.
  If you upgraded the Manager in between, the preview shows
  `enrollment: supersede` and a new fingerprint instead; approve that one
  with `--yes`.
  The solet must still be in the folder it was created in, as a real
  folder. A symbolic link left in its place is refused; move the folder
  back.
- **If your solet was set up without the Manager** (a plain clone, before
  the Manager existed), enroll it once:

  ```bash
  solet-manager import <name> --target <folder> --channel stable --dry-run
  solet-manager import <name> --target <folder> --channel stable --yes --approval-fingerprint <the fingerprint the dry-run printed>
  ```

  The dry-run shows what it found and changes nothing; the second command
  records your solet with the Manager and does not touch your solet's
  files. You do this once, ever.
- Then run `solet-manager doctor <name>`. **"verified" means go.** Anything
  else: read the first line of the report — it names the one thing that
  needs attention and what to do about it — and sort that out before
  updating. A solet created by an earlier Manager that has not been updated
  yet is reported as a create instance; check it with `solet doctor <name>`
  instead. On a solet created before r49, `solet doctor` usually says
  `blocked` on its knowledge-search and model checks
  (`coreai_embedding_request_succeeds`, `knowledge_retrieval_succeeds`,
  `plugin_roster_matches_plan`). That alone does not stop the update: its
  preview lists those checks under `create_transaction`, and the update's
  own final check runs them again on the new release. If they still fail
  there, the update stops before it finishes, your solet is marked as
  needing attention, and nothing is rolled back; follow the repair it
  prints and run the same `--yes` command again
  (`solet-manager reconcile <name> --dry-run` tells you the same). If the
  preview refuses with `operation_in_progress`, the install itself never
  finished; run `solet create <name>` to finish it first.
- Once a solet has been updated, check it with `solet-manager doctor
  <name>`. The older `solet status <name>` may keep showing `blocked`, and
  `solet doctor <name>` may refuse with a contract error; neither changes
  anything on your machine (iss_0a10e6db).
- You'll need a terminal window and about 10 minutes, most of which is
  waiting.

## Step 1 — get the update

Every command here is `solet-manager`, not `solet`: `solet --help` does not list
update, and `solet update` only points you back to `solet-manager update`.

```bash
brew upgrade solet-public/tap/solet
solet-manager update <name> --dry-run
```

The Manager learns about new releases from its own Homebrew package, so
the upgrade comes first. The second command is a preview: it changes
nothing, and prints exactly what the update would do — which release it
will move your solet to, which of the files on your machine it will
preserve untouched (your generated `AGENTS.md`/`CLAUDE.md`, the
`knowledge_bases` links, your `profile` folder, and the two Claude/Codex
coordination-hook `hooks.json` files that setup pointed at your solet's own
Python — these are listed as preserved, not as problems), and any component
it will install.

**If it says `preview_ready`:** continue to Step 2. It also prints an
approval fingerprint — you'll pass that back in the next command, which is
how the Manager knows you approved *this* preview and not some other.

**If it says "already current":** there is nothing to do; you are done.

**If it refuses:** the report names the reason and the exact files. Do not
force anything. The most common reasons: a file the release changes that
you also changed locally (the report tells you how to keep your version by
hand), or something staged in git that you or an agent left behind. Fix the
named thing, or ask for help with the report in hand, then run the preview
again. One exception: never "restore" a coordination-hook `hooks.json` file
to fix a refusal. Setup deliberately points those hooks at your solet's own
Python, and undoing that breaks them. If the report names one of them, it
says what to do instead: usually `git diff -- <file>` to find an extra edit,
or upgrading the Manager with `brew upgrade solet`.

## Step 2 — apply the update

```bash
solet-manager update <name> --yes --approval-fingerprint <fingerprint from Step 1>
```

This performs the first half — bringing the new release's files onto your
computer — and then stops and prints a second preview for the second half:
installing any component the release needs, running any migration, and
restarting your solet. Read it, then approve it the same way:

```bash
solet-manager update <name> --dry-run
solet-manager update <name> --yes --approval-fingerprint <the runtime fingerprint it printed>
```

The Manager restarts your solet, waits for it to be healthy, checks it
over, and tells you when it is verified. You do not need to wait a fixed
time, restart anything yourself, or refresh the Claude Code plugin by hand
— that copy is refreshed as part of this step. If the release includes a
rename migration and the preview says you must **quit Claude Code first**,
do that and run the command again; the Manager never closes a program for
you.

## Step 3 — refresh your generated files (only if you were told to)

Some updates change more than your solet's own code — they also change
files that live on *your* side: your project's `AGENTS.md`/`CLAUDE.md`,
your shell setup, or your Claude Code hooks. The update refreshes the
three of these it owns (your solet's LaunchAgent, the block in your
`~/.zshrc`, and the section in your `~/.claude/CLAUDE.md`) and preserves
the rest; those only update if someone re-runs the setup steps that
originally created them, and that work needs a coding agent's help (it
isn't a plain command you can type — the agent has to compare your current
files against what changed and merge carefully, not overwrite blindly).

Skip this step unless whoever gave you this guide said this update touches
those files. If they did: open a **fresh** Claude Code (or Codex) session
inside your solet's folder and ask it to "re-run step 2 of the seed
hydration runbook" (add "and step 4a too" if you use named multi-session
roles). Let the agent walk you through it — it will show you what's
changing before it touches anything.

To confirm the instruction section in your user-scope `~/.claude/CLAUDE.md`
— the one place your coding agent actually reads its operating
instructions from day to day — is present, run `solet-manager doctor
<name>` and look for `user_claude_md_section` under the *files* section:
"verified" means your solet's own section is installed. Anything else means
it was never installed or got lost — ask your agent to render and install
it now, before continuing.

## Step 3a — configure the export/workspace root (only if you were told to)

Some updates change what your solet requires before it will read from
business systems (Jira, Salesforce, and similar) — specifically, requiring
a folder on your computer where results are allowed to be saved, so records
never land directly in a conversation. If you have already answered this
once, the update carries your answer to any newly installed connector by
itself. Skip this step unless whoever gave you this guide said this update
adds that requirement for the first time.

If it does: tell your coding agent where you keep the folders you work in
day to day (the parent folder, not any single project — something like
`~/Workspace`, not `~/Workspace/some-specific-project`) and ask it to
configure that as your workspace root for business-connector results. Your
agent will validate the folder and confirm what it did — if it refuses
your answer, that's expected behavior protecting your solet's own
files, not an error to work around; give it a different folder instead.

## Step 4 — confirm everything actually updated

```bash
solet-manager doctor <name>
```

Exit code 0 and "verified" means the update is complete: your solet is
running the new release, the Claude Code plugin copy is current (the
*coding-agent* section says `plugin_cache_current: verified`), and any
knowledge the release removed is gone from search. Then open a **brand
new** Claude Code window (closing and reopening an existing one is not
enough — it needs to be a fresh start) inside your solet's folder, and run:

```bash
<name> health
<name> call service_interface::knowledge_service::search '{"query": "seed update runbook", "top_k": 3}'
```

Both should respond normally. If either one errors, or the second command
comes back empty, get in touch with whoever gave you this guide before
using your solet for anything important.

**If this update mentioned a newly-activated plugin** (something you were
told is now live on your solet that wasn't before), confirm its
knowledge is actually searchable, not just installed. Run two or three
searches for things that plugin should know about — for example, if it's a
marketing-data plugin, try queries like `"list campaigns"` or the plugin's
own name — and confirm the results include content from that plugin, not
just unrelated hits. A plugin can be correctly installed and still return
nothing until this is checked, and nothing else in this guide would catch
that.

**If you use Marketo or Zuora specifically**, the retrieval check above
isn't enough by itself — it only proves the plugin's own documentation is
searchable, not that reading real records through it behaves correctly.
These two are only verifiable on YOUR machine, not before this update ships
to you, so this is the one place these checks actually happen. Ask your
coding agent to run three reads through the connector and confirm:

1. **A normal, everyday read** (whatever you'd do day to day, without
   asking for anything unusual) — the results should land in a file on
   your computer, and the response you see should NOT contain the actual
   record values, just a description of where they were saved.
2. **A read where you explicitly ask for more than the default amount** —
   this should work, and should visibly fetch more than the normal read
   did.
3. **A read where you ask for an unreasonably large amount** (more than
   the connector allows even with the override) — this should be refused
   outright, with a message naming the limit, not silently trimmed down to
   something smaller.

If any of these three doesn't behave as described, stop and get in touch
with whoever gave you this guide rather than continuing to use that
connector.

## If something goes wrong

Every Manager command ends one of three ways, and the number it exits with
tells you which:

- **0** — it did what it said. Carry on.
- **3** — it stopped before touching anything and needs you: the report's
  first line names the reason and the files. A refused preview is this
  kind. Fix the named thing or ask for help; nothing is half-done.
- **1** — a check found something wrong with your solet on evidence (the
  report names the check). Ask for help with the report in hand.

If an update stopped part way (the doctor says an update is still open, or
`update` says one is), run `solet-manager reconcile <name> --dry-run`: it
tells you the one next step — continue, or set the stopped update aside —
and changes nothing until you approve it the same way as an update. If it
answers "no active update", nothing is stuck.

- **Right after the restart, `<name> health` briefly errors with
  something mentioning "no active color":** this is a known transient
  state — wait about 10 more seconds and try again before assuming
  something broke; the doctor shows it as "service offline" while it
  heals.
- **Everything ran without errors, but something still seems off:** run
  `solet-manager doctor <name>` again. It's always safe to repeat and never
  changes your solet.

## Questions

If anything here doesn't match what you're seeing on your screen, stop and
reach out rather than guessing — a quick question now is much easier to
answer than untangling a problem later.

## If you (or your agent) author your own joseki cards

Not an update step — a note for later, since it follows directly from what
this update changes. Business-connector reads no longer return record
values directly; a card that used to expect a full record back needs a
different shape now. Two patterns cover most cases, and the choice isn't
arbitrary:

- **IDs and distinguishing fields** fit a card whose job is picking ONE
  thing out of several candidates before a follow-up action — listing
  campaigns before triggering one, searching issues before transitioning
  one. The card mostly needs enough to tell candidates apart, not full
  records.
- **Reading the whole result file** fits a card whose job is processing an
  entire result set — an audit pass, a bulk export, anything that was
  always going to consume everything returned.

If a card's real use doesn't cleanly match either — sometimes picking one
thing, sometimes wanting the whole set — narrow the request itself (ask for
specific fields, or a bounded range) rather than forcing one shape as a
blanket rule.

## What changed in this release — your solet stops needing LM Studio (r52)

If your solet was set up before r46, it still uses LM Studio for embeddings
(and, on macos-bizops, for summaries). On a Mac running macOS 27, this
update moves it to Apple's on-device models:

- **Embeddings move to Core AI.** Your existing memories and search results
  are kept. Only very long past messages are re-indexed, once, in the
  background, so they can be searched in full.
- **Summaries move to Apple Foundation Models** on macos-bizops.
- **LM Studio and its models are left exactly as they were.** The update
  never uninstalls LM Studio and never deletes, moves or changes a model;
  your solet simply stops using them for what moved.

**On a Mac running macOS 26 (Tahoe), nothing moves.** The update still
brings everything else in this release, and your solet keeps using LM
Studio for both embeddings and summaries. Keep LM Studio installed and
running. `solet-manager doctor` says so in plain words. Once the Mac is on
macOS 27, the switch comes with the next release's update; updating again at
the release your solet already runs changes nothing.

A new macos-bizops solet created on macOS 26 does not use LM Studio. Setup
runs Homebrew llama.cpp for its summaries and embeddings instead; the
Homebrew install troubleshooting runbook's macOS 26 section describes it.

(On the Samantha profile, summaries keep using LM Studio on any macOS, so
keep LM Studio installed and running there too.)

The update downloads the Core AI model files and checks them before
switching anything. If that check cannot finish (for example, the download
fails or the update runs out of time), nothing changes. Your solet keeps
working exactly as before, the update finishes with a "needs attention"
note, and running `solet-manager update <name>` again later completes the
move.

If you edited your LM Studio embedding settings by hand, the update will not
overwrite them. It leaves that part as it is and says so; ask your agent to
walk you through the choice.

## What changed in this release — you can now run a small fleet of sessions from this solet (2026-08-10 update)

If you already start extra agent sessions from this solet (or want to
start), this update is the one that makes that practical rather than
manual. Two things matter for you specifically:

**Check the plugin copy actually refreshed.** A small companion tool (the
`coordination-hooks` plugin) got a real update in this release, including
a fix to a background helper that used to be able to wait forever without
telling anyone. Claude Code keeps its own separate copy of that tool, and
at the time this release shipped the refresh was a manual command people
skipped thinking "it probably updated with everything else" — it did not.
Today the Manager refreshes that copy as part of Step 2, and Step 4's
doctor shows `plugin_cache_current: verified` when it did.

**A new "operating manual" now ships with your solet** for anyone
running more than one session of it at once — how to hand off work between
sessions, pause one, bring back a stuck one, and check whether one is
running low on its own working memory. If you (or a coding agent working
on your behalf) manage multiple sessions, ask your agent to search your
solet's knowledge base for "maintenance verbs joseki cards" — that's
the manual. If you only ever talk to one session at a time, none of this
changes anything for you.

**One more thing, only if you use a connected Postgres or Snowflake
database:** both can now write to your database, but only if the login
you registered is itself allowed to — your solet does not add or
remove any permission on its own; your database's own access rules
decide. If you never want a particular connection to be able to write,
register it under a read-only login, same as you would for any other
tool. Snowflake's write path is brand-new this release and hasn't been
tested against a live account yet — if you turn it on, you're among the
first to actually use it live.

## What changed in this release — mostly behind-the-scenes (2026-08-08 update)

This update is almost entirely internal — you can follow Steps 1–6 above
exactly as written, with nothing extra. There's no new dependency to
install, and nothing new to configure.

The one thing worth knowing: if you (or a coding agent working on your
behalf) ever start OTHER agent sessions from this solet — not just the
one you're talking to — this release gives those sessions more ways to
coordinate with each other (starting, watching, and handing off work
between them). If that's not something you do, you can ignore this
entirely; it doesn't change how your solet behaves for ordinary use.

**One thing to know about, not something this update fixes:** a plugin
your solet ships and enables by default includes a few small reminder
hooks that depend on a program called `node` being present on your
machine. If it isn't, those specific reminders simply do not run. Most of
the time you'll notice — you'll see an on-screen error naming the missing
program, both at the start of a session and when you submit a prompt, and
everything keeps working normally either way. One of the reminders (an
idle-wake helper that's meant to run quietly in the background) is the
exception: if it fails to start, there's no visible sign of it at all.
Your git-safety protection is a separate hook, built differently, and
keeps working either way. This isn't fixed yet.

See the root `RELEASE_NOTES.md` file in your solet's folder for the
full list of what changed, in more detail than this guide covers.

## Reference

- `05_seed_update_runbook.md` — the same update procedure written to a
  coding agent, with the full technical detail (the exact refusal
  vocabulary, which Manager operation owns each former manual step, the
  measured plugin-cache behavior, and the legacy manual procedure kept as
  recovery) that this guide leaves out on purpose.
- `01_hydration_runbook.md` — the first-time setup steps Step 3 above
  re-runs selectively.
- `RELEASE_NOTES.md` at the repo root — the full changelog for every
  release, including the ones summarized above.
