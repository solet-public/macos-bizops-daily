# Blob and Run-Flow Evidence Retention: No Automatic Deletion

Tags: knowledge:tag:retention_policy, knowledge:tag:blob_storage, knowledge:tag:operator_communication, knowledge:tag:business_data

Article Layer: 2

Article Role: operator_policy_statement

Article Tags: planning-stage:always, evidence-category:policy-statement, domain:blob-storage, domain:business-connectors, consumer_profile:both

Embedding Description: The platform's retention policy for blobs and run-flow evidence — there is no automatic deletion by age, quota, or a seed default TTL; retention is a deliberate human act, the same way a filesystem does not delete files on a schedule; states which verbs exist to inspect and delete blobs, and how an operator makes storage growth legible without a TTL.

**When you need this**: an operator or card author asks how long joseki run-flow evidence
or business-connector blob exports are kept; a card author needs a defensible answer for
"is this retained forever"; disk usage is growing and someone is looking for a cleanup
schedule to configure; deciding whether a new feature needs its own retention/expiry logic.

---

## The ruling

Ruled by the operator 2026-07-29, verbatim:

> "Blobs are going to be pretty important — they will hold binaries that we don't want to
> lose. It is like a file system. You don't willy-nilly set a delete date on files on your
> file system. We have verbs for investigating and deleting blobs when we need to I hope —
> those are needed. Automatic deletion, that is not a thing."

**No age-based expiry, no quota-based eviction, no seed default TTL. Retention is a
deliberate human act.** The filesystem analogy is the governing one: nothing on disk is
removed because a clock or a size threshold said so. See the 2026-07-29 architect ruling
on business-connector data boundaries, filed in this checkout's `workbench/` directory
under that date (§5), for the full record — this article promotes that ruling to an
operator-facing statement; it does not amend it.

This closes two lanes at once: a business-data consumer's ask for a retention/visibility
statement a card author can reason about, and the disk-growth backlog item that had been
waiting on exactly this decision since 2026-07-18.

## What already exists — nothing to build

The inspection and deletion capability the ruling asks for already ships in
`default_blob_storage_plugin`: `search_blobs`, `get_blob_metadata`, `get_blob`,
`delete_blob`, `update_blob_metadata`, `store_blob`, `store_blob_from_file`,
`file_command`. Retention here is not a gap waiting on a build — it is a policy that was
undocumented while the mechanism it depends on already existed.

## What this means for a card author

Run-flow evidence and blob exports are **retained until someone deliberately deletes
them** — not until a TTL expires, not until a quota trips. Write joseki cards and runbooks
on that assumption: evidence you write down now will still be there later unless someone
removes it, and nothing removes it automatically on your behalf either.

## Growth is visible-and-deliberate, not bounded — make it legible

The operator's own analogy carries an obligation the "no TTL" half doesn't discharge by
itself: you don't auto-delete files on a filesystem, but you *can* see what's consuming
it — that's why `du` exists. Under "no automatic deletion," the honest counterpart to a
TTL is not a bound on growth; it's **making growth visible**. `search_blobs` and
`get_blob_metadata` are the primitives an operator or agent uses to see what blob storage
is holding and what's large, before deciding whether to `delete_blob` anything. Surfacing
that in an operator-facing runbook, rather than leaving it to ad hoc verb calls, is the one
remaining open thread — it's a documentation question, not a policy one.

---

## Reference

- The 2026-07-29 architect ruling on business-connector data boundaries, filed in this
  checkout's `workbench/` directory under that date (§5) — the ruling this article
  promotes.
- `plugin::default_blob_storage_plugin::search_blobs`,
  `plugin::default_blob_storage_plugin::get_blob_metadata`,
  `plugin::default_blob_storage_plugin::delete_blob` — the inspection/deletion verbs named
  above.
- `24_operator_communication/08_business_record_classification_convention.md` — the
  companion disclosure on what values end up committed vs. staged.
- `plugins/github_midwife_plugin/knowledge_base/01_hydration_runbook.md`, "What this
  solet ingests and embeds" — the sibling disclosure on session-content ingestion.
