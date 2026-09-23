# Business Record Classification Convention: Stable Identifiers Over Values

Tags: knowledge:tag:data_classification, knowledge:tag:business_data, knowledge:tag:git_hygiene, knowledge:tag:operator_communication

Article Layer: 2

Article Role: operator_policy_statement

Article Tags: planning-stage:always, evidence-category:policy-statement, domain:business-connectors, domain:git-hygiene, consumer_profile:both

Embedding Description: The platform-adopted convention for keeping record-level business data (customer names, addresses, PII field values) out of committed git history — stable system identifiers go in every log line, report, and checkpoint file that might be committed; addresses and values live only in a gitignored staging location; destructive verbs are exercised against synthetic records, never live ones.

**When you need this**: writing a script, report, checkpoint, or log line that touches
business-connector data and might end up in a git-tracked directory; deciding whether a
value belongs in a committed artifact or a staging file; reviewing a diff that contains
what looks like a real customer record; onboarding a new project directory that will do
connector work.

---

## The convention, adopted from the consumer already holding it

Ruled 2026-07-29 (the architect ruling on business-connector data boundaries, filed in
this checkout's `workbench/` directory under that date; §6): the platform adopts the
classification discipline an existing business-data consumer already holds, rather than
inventing a parallel one. Three rules:

1. **Stable system identifiers — not values — go in every log line, report, and
   checkpoint file.** A record ID, an external system ID, a run ID: fine to commit.
   A name, an email address, a phone number, a physical address, or any other field value
   that identifies a real person: not fine to commit.
2. **Addresses and values live in a gitignored staging location.** That's where a human
   joins an identifier back to the person it belongs to, deliberately, outside version
   control.
3. **Destructive verbs (delete, update, bulk-modify) are exercised against synthetic
   records that are created and deleted for the purpose** — never against a live
   record, even in a test or a demo.

## Why this matters more than it looks like it should

The platform does not choose where business-connector results land on disk — the caller
supplies the destination path, and the platform hard-codes nothing (the
caller-supplies-the-path architecture ruled the same day, same document, §3). That means
there is no platform-owned, gitignore-by-construction location for connector output. **A
documented convention is the only thing standing between record-level data and a git
repository** for any script, report, or checkpoint a caller points at a path inside a
checkout. This convention is that thing. It matters independent of whatever else the
wider data-boundary ruling does or doesn't ship, and it's useful starting now, not
conditional on any other build.

It also matters for a second reason: an agent orchestrating business-data work writes
scripts and points them at data — the agent does not read gigabyte-scale results itself,
a subprocess does. That subprocess's stdout is a channel the platform cannot govern by any
mechanism. This convention — stable IDs in what gets printed, logged, and written to a
checkpoint — is the actual control for that channel. There is no code-level substitute for
it.

## What the platform ships to support this

- **A gitignore entry for a staging location.** The genesis git-init step
  (`plugins/github_midwife_plugin/src/github_midwife_plugin/git_init.py`) writes a
  `staging/` entry into every newly-initialized solet worktree's `.gitignore` at
  birth (never clobbering an existing `.gitignore` — the same idempotent-and-preserving
  rule genesis applies everywhere else). Treat `staging/` as the default name for this
  convention's staging location in a fresh project directory; rename it if you already
  have a different convention, and update the ignore entry to match — the platform can
  ship a default, not enforce a name.
- **Platform-written artifacts default to stable identifiers over values.** Anything the
  platform itself generates — logs, reports, checkpoints — follows rule 1 above by
  default. This is the half a caller cannot hold on the platform's behalf, and the half
  that matters once the platform is used outside this operator's own install.

## What the platform does not do

The platform cannot impose this convention on a caller's own scripts — it can only
encourage it. Nothing stops a script from printing a value to stdout or writing one into
a report at a path inside a checkout; the destination argument is caller-supplied by
design (the 2026-07-29 architect ruling on business-connector data boundaries, filed in
this checkout's `workbench/` directory under that date; §3), and the platform picks no
path on the caller's behalf. This convention is the control; it is not enforced by a
gate.

---

## Reference

- The 2026-07-29 architect ruling on business-connector data boundaries, filed in this
  checkout's `workbench/` directory under that date (§3, §6) — the architecture and
  convention this article promotes.
- `plugins/github_midwife_plugin/src/github_midwife_plugin/git_init.py` — the genesis
  step that writes the `staging/` gitignore entry into every newborn worktree.
- `24_operator_communication/07_business_data_retention_and_deletion.md` — the companion
  disclosure on how long anything committed or staged is kept.
- `plugins/github_midwife_plugin/knowledge_base/01_hydration_runbook.md`, "What this
  solet ingests and embeds" — states why keeping values out of session content
  matters upstream of this convention: anything that does reach a session is ingested at
  full fidelity, by design.
