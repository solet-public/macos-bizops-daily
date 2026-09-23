# Salesforce Plugin (`salesforce_plugin`)

Article Layer: 1

Article Role: plugin_reference

Article Tags: planning-stage:execution, evidence-category:capability-reference, domain:salesforce, domain:local-solet, domain:cloud-solet

Embedding Description: Salesforce plugin reference — the one-time operator setup for connecting a Salesforce org via the operator's sf CLI login (standalone CLI bundle install, one browser login via sf org login web, two-field salesforce_org address-book registration with a pinned instance host), the thirteen verbs with argument shapes (SOQL query, record get/describe/list/create/update/delete, test_connection, and Bulk API v2 ingest submit/status/results/abort), typed sf.* errors with recovery when the CLI has no live session, and the full-CRUD-including-delete posture under full CLI delegation.

A full read/write connector over the operator's Salesforce org. Executor:
full CLI delegation — every verb shells out to the operator's `sf` CLI
directly (operator-ratified 2026-07-14, replacing the sf-CLI session-borrow
client factory, which is dead on current CLI releases: `sf org display
--json` now redacts `accessToken` unconditionally, verified live against CLI
2.142.7 — there is no longer a way to borrow a usable token from CLI output
at all). This is the enterprise-governance-friendly mode — it rides the
org's already-blessed Salesforce CLI Connected App, so enabling the connector requires
NO new org artifacts, no change request, and no admin ceremony. The solet acts as
the operator's own user; its access dies with the CLI's login (logout/
revocation) — both the feature and the operational caveat. Single org v1:
the "salesforce_org" address-book entry.

## Read/write posture — FULL CRUD INCLUDING DELETE (operator-ratified, RATIFY-2)

The operator's dividing line: *"document/ticket deletion is an acceptable
loss class, database destruction is not."* Salesforce is a SaaS workflow
tool, not the developer database connectors are — so, unlike
`snowflake_plugin`/`external_postgres_plugin`, this connector ships **full
CRUD including `delete_record`**, and — as of 2026-08-24 — **Bulk API v2
ingest** (`bulk_ingest_submit`/`bulk_job_status`/`bulk_job_results`/
`bulk_job_abort`; insert/update/upsert/delete, never `hardDelete` — see
"Bulk API v2 ingest" below). `run_apex`, bulk **query**, and ContentVersion
file-upload verbs remain **out of v1 for build-effort reasons only** (not a
risk exclusion) — pullable into v1 on operator request.

## Executor model — full CLI delegation, no token ever enters this process

The durable credential is the **sf CLI's own keychain-backed refresh token**,
established once by the operator via `sf org login web` — the platform
stores NO Salesforce secret of its own (no vault key exists for this
plugin). Every verb shells out to the `sf` CLI directly with `--target-org
<alias>` — never `simple-salesforce`, never a Python HTTP client, and no
Salesforce SDK dependency at all. Three CLI surfaces are in play:

- **Envelope commands** (`data query/get/delete`, `sobject describe`, `org
  display`) — the stable, non-beta CLI surface, invoked with `--json` for a
  clean `{status, result}` envelope. Used for every verb that only needs an
  id or a fixed path (`get_record`, `delete_record`, `describe_sobject`,
  `soql_query`, plus the org-binding verification call).
- **`api request rest`** (beta) — a generic authenticated REST passthrough
  with a JSON body file (`--body @file`). Used for `create_record` and
  `update_record` because the stable `data create/update record --values`
  command's field-value mini-language (`stringToDictionary` in the CLI's own
  `dataUtils.js`) has a genuine correctness bug: it silently coerces any
  field value that case-insensitively equals `"true"`/`"false"` into a
  boolean, and attempts a bare `JSON.parse` on any value containing both `{`
  and `}` — both are real data-corruption hazards for ordinary business data
  (e.g. an Account literally named "True Value Hardware"), not an escaping
  inconvenience a caller can quote around. `list_sobjects` also uses this
  surface (GET the global describe endpoint) because the stable `sobject
  list` command returns bare object names with no `label` field.
- **File-based SOQL** — `soql_query` writes the query text to a tempfile and
  runs `data query --file <path>` (the operator's proven work-script
  pattern), keeping SOQL text out of the process argv `ps` would otherwise
  expose. No manual pagination: `sf data query` runs on jsforce with
  `autoFetch: true` internally, so it already collects every page up to a
  fetch cap before returning; `SF_ORG_MAX_QUERY_LIMIT` is passed as a
  subprocess env override to cap that fetch at the effective row limit
  server-side (500 by default, up to 1000 with an acknowledged override —
  see the verb map below), and the result is sliced to that same limit
  again client-side as defense-in-depth.

The org binding (host pin) is verified exactly ONCE per process lifetime,
lazily, on first call (`client.py::SalesforceCliExecutor._verify_org_binding`
via `org display --json`) and cached — there is no rebuild-on-expiry
mechanism, because the CLI refreshes its own credential transparently inside
every invocation. A fault is therefore always either a CLI-level failure
(binary missing, timeout, no live session for the alias) or a classified
REST-level rejection (`SalesforceCliCallError`, built from the CLI's `--json`
error envelope or the `api request rest` raw error array) — never a
mid-flight expired session this process must detect and retry.

## One-time operator setup (runbook — proven live 2026-07-14, origin -> Branch org)

**This needs a Salesforce user account, nothing more.** No new Connected
App, no certificates, no admin change request: the flow rides the org's
already-blessed Salesforce CLI app, exactly like the operator's own `sf`
usage. The solet acts as the operator's user — its permissions are
that user's permissions, and its audit trail shows that user.

### Stage 1 — provision the sf CLI on consent (agent-executed)

1. Call `plugin::salesforce_plugin::provision_cli` with
   `acknowledge_system_change: true`. It probes first; when sf is missing it
   authorizes only `brew install sf` and Homebrew's declared `node` closure,
   refusing upgrades, reinstalls, removals, unlinks, and extra packages.
2. The process re-probes `sf --version`, persists the resolved absolute path
   as `sf_cli_path`, and rebinds the running plugin. It explicitly reports
   that this configuration update has no hidden reload or restart boundary.
   A compliant sf executable is not reinstalled.

### Stage 2 — one browser login (operator)

3. Run (agent may launch it; the browser opens for the operator):
   ```
   sf org login web --alias <alias>
   ```
   The operator signs in once (SSO/MFA fine). The CLI stores its
   keychain-backed refresh token; nothing re-prompts until logout or
   revocation. Add `--instance-url https://<org>.my.salesforce.com` only if
   the org mandates My Domain login.
4. Verify token-safely (NEVER `sf org display` bare — its output includes
   the access token; filter to safe fields):
   ```
   sf org display --target-org <alias> --json | python3 -c \
     "import json,sys; r=json.load(sys.stdin)['result']; \
      print(r['instanceUrl'], r['username'], r['connectedStatus'])"
   ```

### Stage 3 — register + enable + verify

5. Register the `salesforce_org` address-book entry
   (`address_book_service::register`, address_type `api`) with two
   field_type/value entries — no secrets:
   - `target_org` = the CLI alias from step 3
   - `instance_host` = the host from step 4 (e.g. `<org>.my.salesforce.com`)
     — the foreign-target pin; the plugin refuses any alias resolving
     elsewhere
6. If `salesforce_plugin` is not in the live manifest, add it via the
   blue-green manifest deploy (`apply_manifest` dry-run for the etag → CAS
   commit → wait for the swap; the entry point ships installed).
7. Dispatch `test_connection` → expect `{ok: true, org_id, username,
   api_version}` with the operator's own username.
8. Agent-driven setup note: `process_call` this plugin's verbs directly like
   any other process — they are not export-denied (the prior RATIFY-3 deny
   was retired by operator ruling 2026-07-15: friction, not security, on a
   single-user substrate — see
   workbench/2026-07-15_result_error_processing_architecture_deep_dive.md).

### Recovery — expected failure modes

- `sf.auth_failed` — no live CLI session for the registered alias (never
  logged in, `sf org logout`, or an admin revoked the token): re-run
  `sf org login web --alias <alias>`. The error message carries this hint.
- `sf.not_configured` naming `sf_cli_path` — the CLI binary is missing or
  the platform can't see it: reinstall the standalone bundle and/or fix the
  pinned path in the plugin config.
- `sf.not_configured` naming both instance hosts — the CLI alias resolves
  to a different org than the registered `instance_host` pin: fix whichever
  is wrong; the plugin refuses to connect until they match.
- `sf.session_expired` — rare under full CLI delegation (the CLI refreshes
  its own credential inside every invocation); it indicates the org itself
  rejected an in-flight call, most likely because the CLI's refresh token
  died mid-session (org-side revocation or password change). One browser
  re-login restores everything; nothing platform-side to rotate.
- Credential lifecycle caveat (by design): the solet's Salesforce access
  is bound to the operator's login on THIS machine. Operator logout or
  org-side revocation turns the connector off until the next login — the
  fully-supported dormant state, not a breakage.

## Verbs (built — 13 total)

| Verb | Args | Returns |
|---|---|---|
| `test_connection` | — | `{ok, org_id, username, api_version}` |
| `soql_query` | `query`, `output_tsv_path`, `acknowledge_default_limit_override=false`, `row_limit?` | `{path, columns, row_count, total_size, truncated}` — written as ONE `.tsv` file at the caller's ABSOLUTE path, never records inline; defaults to 500 records, up to 1000 with an acknowledged override |
| `export_soql` | `query`, `output_tsv_path`, `acknowledge_default_limit_override=false`, `row_limit?` | `{path, columns, row_count, total_size, truncated}` — the N>>500 route: same shape as soql_query, defaults to 500 records, up to 50000 with an acknowledged override |
| `get_record` | `sobject`, `id`, `fields?` | `{record}` |
| `describe_sobject` | `sobject` | `{fields:[{name, type, label, nillable, updateable}]}` |
| `list_sobjects` | — | `{sobjects:[{name, label}]}` |
| `create_record` | `sobject`, `fields` | `{id, success}` |
| `update_record` | `sobject`, `id`, `fields` | `{success}` |
| `delete_record` | `sobject`, `id` | `{success}` (permanent — see read/write posture) |
| `bulk_ingest_submit` | `operation`, `sobject`, `csv_path`, `external_id_field?` | `{job_id, state, processed_records, failed_records, error_message}` — submits a Bulk v2 ingest job `--async`; `csv_path` is read-contained under `import_allowed_roots` |
| `bulk_job_status` | `job_id` | `{job_id, state, processed_records, failed_records, error_message}` — the poll target for a submitted job |
| `bulk_job_results` | `job_id`, `successful_results_path`, `failed_results_path`, `unprocessed_results_path` | `{successful_results_path, failed_results_path, unprocessed_results_path}` — three CSVs written under `export_allowed_roots` |
| `bulk_job_abort` | `job_id` | `{job_id, state, processed_records, failed_records, error_message}` |

Both `soql_query` and `export_soql` ALWAYS write their result to the
caller's `output_tsv_path` — never records inline, at any size
(business-data limits + data-export migration, 2026-08-02; the former
inline-return/byte-cap branch is deleted, not lowered). Neither destination
is platform blob storage (operator ruling 2026-07-15); the path must be
absolute, end in `.tsv`, and lie under an operator-configured
`export_allowed_roots` entry in this plugin's config — realpath +
`commonpath` containment mirroring `ledger_allowed_roots`; the default `[]`
REFUSES every write. Nested relationship objects serialize as JSON text in
their cells. Each defaults to 500 records absent an acknowledged override;
`soql_query`'s override ceiling is 1000, `export_soql`'s is 50000 (the
N>>500 route). `acknowledge_default_limit_override=true` together with an
explicit `row_limit` requests more than the default — both are required
together, and a `row_limit` above the verb's hard cap is refused, never
silently clamped. Neither verb has a vendor-imposed ceiling to defer to:
this plugin never executes Apex, so the 50,000-record Apex governor limit
does not apply; the actual Salesforce fact for this call path is a
2,000-record REST query batch size with no vendor total ceiling, and
jsforce's `autoFetch` already pages past that internally.

## Bulk API v2 ingest (2026-08-24 — wave 1: ingest only, bulk query is wave 2)

Four verbs cover the write side of Bulk API v2 — submitting large insert/
update/upsert/delete jobs from a CSV file, polling them, fetching their
result CSVs, and aborting them. All four are, like every verb in this
plugin, on the D0.3 deferred-completion shape: the dispatch returns a
plugin-level `job_id`/`status: queued` in milliseconds, and the *Salesforce*
Bulk job's own id/state is delivered separately when that dispatch's job
completes — two independent async layers, never conflate the two ids.

- **`bulk_ingest_submit`** runs `sf data <operation> bulk --async --json` and
  returns the instant Salesforce ACKNOWLEDGES the job (state `Open` or
  similar) — never `--wait`, because a Bulk v2 job can run minutes, well past
  this connector's 30s per-call subprocess bound (`SF_CLI_TIMEOUT_SECONDS`).
  `operation` is one of `insert`/`update`/`upsert`/`delete`. **`hardDelete` is
  explicitly excluded** — it bypasses the recycle bin and needs a separate org
  permission, and is not obviously inside the standing delete ratification
  (RATIFY-2); add it later only on explicit operator say-so. `external_id_field`
  is required if, and only if, `operation` is `upsert` — supplying it for any
  other operation is refused, not silently ignored. `csv_path` must be an
  ABSOLUTE path already on disk, contained under an operator-configured
  `import_allowed_roots` entry (empty by default = refuse every ingest).
- **`bulk_job_status`** is the poll target: `GET /jobs/ingest/{id}` via
  `run_rest`, an honest passthrough of the Salesforce job's `state`
  (`Open` → `UploadComplete` → `InProgress` → `JobComplete`/`Failed`/
  `Aborted`) plus processed/failed record counts.
- **`bulk_job_results`** fetches the job's three result CSVs — successful,
  failed, and unprocessed records — and writes each to a caller-supplied
  ABSOLUTE path under `export_allowed_roots` (the same config `export_soql`
  uses, admitted with a `.csv` suffix instead of `.tsv`). The CSV bodies come
  back via `run_rest`'s raw-text mode rather than JSON-decoded — the measured
  design choice over the CLI's own `sf data bulk results --job-id`, which
  writes into its own resolved cwd outside this plugin's containment
  boundary; REST + our own contained file write keeps every byte this verb
  writes inside the same gate `export_soql` already uses.
- **`bulk_job_abort`** does `PATCH /jobs/ingest/{id}` with `state: Aborted` —
  the recovery lever for a job submitted in error. Records already processed
  before the abort are NOT rolled back.

Errors are typed with the `sf.*` prefix: `sf.not_configured`,
`sf.invalid_params`, `sf.auth_failed`, `sf.session_expired`,
`sf.permission_denied`, `sf.not_found`, `sf.malformed_query`,
`sf.rate_limited`, `sf.api_error`, `sf.export_path_refused`,
`sf.import_path_refused`.

## Security posture (mirrors the Jira/Snowflake/external-Postgres wave)

- **Foreign-target invariant.** No `org`/`domain` parameter on any verb —
  every CLI call is made with the single registered `salesforce_org` entry's
  `target_org` alias, and that alias's resolved instance host must equal the
  registered `instance_host` pin exactly or the org-binding verification
  refuses (the CLI's alias cache can hold many orgs; the pin guarantees this
  plugin only ever talks to the registered one).
- **No export deny — direct `process_call` is the normal path.** Retired by
  operator ruling 2026-07-15: on this single-user substrate every MCP session
  is the operator, so the deny was friction, not security — it forced the
  manual `sf --json` shell-out that routed 200 records through context in the
  2026-07-14 Blocked_Email_Domain incident. See the ruling doc above.
- **Generic error messages for topology-leaking classes.** Auth, session
  expiry, and permission errors NEVER embed the raw driver exception string
  (it can carry the org's my-domain host). Not-found and malformed-query
  errors describe the caller's own record/query and may keep response-body
  detail.
- **Secrets hygiene.** The platform stores NO Salesforce secret at all: the
  durable credential is the sf CLI's own keychain-backed refresh token, and
  the `salesforce_org` entry carries only two literals (`target_org`,
  `instance_host`). This plugin declares no vault keys
  (`get_required_vault_keys`/`get_declared_vault_keys` both return `[]`);
  full CLI delegation means no access token of any kind ever enters this
  process — every verb shells out to `sf` and parses only its `--json`
  output, and the org-binding verification step reads only `instanceUrl`/
  `username` from `org display --json`, never the (now unconditionally
  redacted) `accessToken` field. Operators inspect the CLI's own session
  state via `sf org display --target-org <alias> --json` filtered to safe
  fields, same as always — never echoed raw.

## SQL-lockdown gate note

SOQL strings like `SELECT Id, Name FROM Account` match the gate's S2
verb+FROM heuristic even though they target the FOREIGN Salesforce org — a
false positive the gate cannot resolve statically. One whole-file
`sanctioned-exempt` entry in `quality_gates/sql_access_allowlist.txt` covers
`soql_actions.py` (the only module composing SOQL-shaped text); `record_actions.py`,
`client.py`, `errors.py`, and `plugin.py` stay fully gated.

## Key files

| File | Purpose |
|---|---|
| `src/salesforce_plugin/constants.py` | Every magic value: address-book field names, error codes, result types, caps. |
| `src/salesforce_plugin/app_config.py` | Resolves `salesforce_org` (target_org + instance_host, both literal) from the address book. |
| `src/salesforce_plugin/client.py` | `SalesforceCliExecutor` — `run_json()` (envelope commands) + `run_rest()` (`api request rest` with a JSON body file, or `raw_response=True` for a CSV body — `bulk_job_results`); verifies the org-binding host pin once, lazily, via `org display --json`. No rebuild-on-expiry — the CLI manages its own credential refresh. |
| `src/salesforce_plugin/errors.py` | Topology-safe error classification (`classify_salesforce_error`) over `SalesforceCliCallError` (error_code + detail_message from the CLI's `--json` error envelope or the `api request rest` raw error array). |
| `src/salesforce_plugin/soql_actions.py` | `soql_query` — file-based query, `SF_ORG_MAX_QUERY_LIMIT` env cap + client-side slice; no manual pagination (the CLI autofetches). |
| `src/salesforce_plugin/record_actions.py` | `get_record`/`describe_sobject`/`delete_record` via stable ID-based commands; `create_record`/`update_record`/`list_sobjects` via `run_rest` (the `--values` mini-language's `stringToDictionary` parser has a proven correctness bug — see the executor model section above). |
| `plugins/salesforce_plugin/src/salesforce_plugin/bulk_actions.py` | Bulk API v2 ingest — `bulk_ingest_submit` (`data <op> bulk --async`), `bulk_job_status`/`bulk_job_abort` (`run_rest` against `/jobs/ingest/{id}`), `bulk_job_results` (raw-text `run_rest` → three contained CSV writes). |
| `plugins/salesforce_plugin/src/salesforce_plugin/import_containment.py` | Read-side mirror of `export_containment.py` for `bulk_ingest_submit`'s `csv_path` — `import_allowed_roots`, refuse-all default, plus an existence check (an ingest source must already be a real file). |
| `src/salesforce_plugin/plugin.py` | The `SalesforcePlugin` EDGE provider — error mapping, EDGE registration. No retry wrapper: a CLI invocation either succeeds or fails classified, never a stale-session mid-flight fault to retry. |
