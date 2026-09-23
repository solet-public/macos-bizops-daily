# Zuora Plugin (`zuora_plugin`)

A subscription-billing connector over the operator's Zuora tenant. Executor:
a plain synchronous `httpx.Client` (no official Zuora SDK, no CLI). Auth:
OAuth 2.0 client-credentials — the simplest auth model of the platform's
connector suite. Single tenant v1: the "zuora_tenant" address-book entry.

## Read/write posture — CREATE/UPDATE, NO DELETE

Unlike `snowflake_plugin`/`external_postgres_plugin` (read-only hard) or
`jira_plugin`/`salesforce_plugin` (full CRUD including delete), Zuora ships
**no delete verb at all**. Billing records (accounts, subscriptions,
invoices, payments) are voided or cancelled through Zuora's own workflow, not
deleted through this tool — deletion isn't a meaningful operation on most of
these object types in Zuora's own data model, so the connector simply
doesn't expose one.

## Session model — re-mintable bearer, in-memory only

The OAuth `client_secret` is the durable credential (chain-consumed through
the address book, never vaulted under this plugin's own identity). The
bearer token it mints is short-lived (~1h), re-mintable, and held ONLY in
process memory (`http_client.py::ZuoraClient`) — never persisted. A 401
triggers exactly ONE re-fetch-and-retry; a second 401 classifies as
`zuora.auth_failed`.

## Registering the OAuth client (operator console runbook, one-time)

1. **Create an OAuth client** in the Zuora tenant (Zuora admin console →
   Administration → Manage OAuth Clients) on a platform/API user with the
   permissions this connector needs (Data Query, Object API, Billing).
2. **Copy the client_id and client_secret** — Zuora surfaces the secret only
   once at creation.
3. **Register the address-book entry** `zuora_tenant` with the literal
   fields `base_url` (the environment selector — see below) and `client_id`,
   and a `client_secret` field holding
   `vault::<solet>.default_address_book_plugin.zuora_client_secret`.
   Ingest the secret agent-blind.

**`base_url` IS the environment selector** — there is no separate
prod/sandbox flag:

| Environment | `base_url` |
|---|---|
| US Production | `https://rest.zuora.com` |
| EU Production | `https://rest.eu.zuora.com` |
| Sandbox | `https://rest.apisandbox.zuora.com` |

## Business-data limits + data-export migration (2026-08-02)

`data_query`, `bulk_export`, `list_subscriptions`, and `list_invoices` ALWAYS
write their result to the caller's `output_tsv_path` — never records inline,
at any size (`workbench/2026-08-02_business_data_limits_and_spill_floor_design_coordinator_day.md`;
the former blob-export/`INLINE_BYTE_CAP` branch on `data_query`/`bulk_export`
is deleted, not lowered). Neither destination is platform blob storage — the
path must be absolute, end in `.tsv`, and lie under an operator-configured
`export_allowed_roots` entry in this plugin's config (`export_containment.py`,
realpath + `commonpath` containment mirroring `ledger_allowed_roots`; the
default `[]` REFUSES every write). `get_object`/`get_invoice` (single-record
fetch-by-id) are unaffected and stay inline.

Each of the four defaults to 500 records absent an acknowledged override:

| Verb | Params | Override ceiling | Vendor mechanism |
|---|---|---|---|
| `data_query` | `zoql`, `output_tsv_path`, `acknowledge_default_limit_override=false`, `row_limit?` | 1000 | Single `POST /v1/action/query` call (vendor cap 2000/call) |
| `bulk_export` | `zoql`, `output_tsv_path`, `acknowledge_default_limit_override=false`, `row_limit?` | 50000 (the N>>500 route) | `POST /v1/action/query` + `POST /v1/action/queryMore` continuation loop, following the vendor's own `queryLocator`/`done` signal past the 2000/call ceiling |
| `list_subscriptions` | `account_id`, `output_tsv_path`, `acknowledge_default_limit_override=false`, `row_limit?` | 5000 | Internal `page`/`pageSize` pagination against `GET /v1/subscriptions/accounts/{account_id}` (vendor cap 40/call, documented `GET_SubscriptionsByAccount` operation) |
| `list_invoices` | `account_id`, `output_tsv_path`, `acknowledge_default_limit_override=false`, `row_limit?` | 5000 | Single call to `GET /v1/invoices/accounts/{account_id}`; `row_limit` caps what is WRITTEN, not a confirmed pre-fetch ceiling — see the note below |

`acknowledge_default_limit_override=true` together with an explicit
`row_limit` requests more than the default — both are required together, and
a `row_limit` above the verb's hard cap is refused, never silently clamped.
Nested objects (e.g. a subscription's rate plans) serialize as JSON text in
their TSV cells.

**`data_query`/`bulk_export` fixed a pre-existing, independently-confirmed
defect**, not just added the override mechanism: `bulk_export`'s
`BULK_EXPORT_ROW_CAP` (50,000) was vacuous — the verb posted to the same
synchronous query endpoint as `data_query` and never called `queryMore`, so
the vendor's own ~2,000-record ceiling was hit first every time and the
50,000 figure could never be reached. Both verbs now call the documented,
current Zuora Actions pair (`POST /v1/action/query` / `POST /v1/action/queryMore`,
operationIds `Action_POSTquery`/`Action_POSTqueryMore`) rather than the
undocumented legacy `/v1/query` alias this module used previously — the same
underlying ZOQL mechanism, but with a citable current contract for the
queryMore continuation this fix depends on.

**`list_subscriptions` fixed a second pre-existing defect: silent
truncation.** `GET /v1/subscriptions/accounts/{account_id}` is a documented,
page/pageSize-paginated endpoint (component `GLOBAL_REQUEST_pageSize`: max
40, default 20) — the prior implementation passed neither parameter, so it
silently returned at most 20 subscriptions with no signal more existed. This
build pages internally up to the effective row limit.

**`list_invoices`' endpoint is NOT independently confirmed.**
`GET /v1/invoices/accounts/{account_id}` (like the legacy `/v1/query` alias
above) is absent from Zuora's current published OpenAPI bundle, but unlike
`/v1/query` — which is independently verified live via `data_query`'s own
2,000-record-cap behavior — there is no equivalent live-behavior evidence for
this endpoint, and no documented pagination contract to build against.
Zuora's documented CURRENT account-scoped billing-document listing,
`GET /v1/billing-documents`, mixes invoices with credit/debit memos and has
no `documentType` query filter — migrating to it is a verb-contract change
outside this wave's approved scope (flagged for a ruling separately, not
built silently). `list_invoices` therefore still issues a single call and
applies `row_limit` as a cap on what is WRITTEN, disclosed as such in the
process description rather than presented as a confirmed guarantee.

New error code: `zuora.export_path_refused` (path not absolute / not `.tsv` /
not contained under any configured `export_allowed_roots` entry).

## Security posture (mirrors the Jira/Snowflake/Salesforce/external-Postgres wave)

- **Foreign-target invariant.** No `base_url`/environment parameter on any
  verb — every request is built from the single registered `zuora_tenant`
  entry.
- **No export deny.** Not denied from the MCP surface — call `process_call` on
  these verbs directly. The prior RATIFY-3 deny was retired by operator ruling
  2026-07-15: friction, not security, on a single-user substrate where every
  MCP session is the operator (see
  `workbench/2026-07-15_result_error_processing_architecture_deep_dive.md`).
- **Generic error messages for topology-leaking classes.** Auth and
  rate-limit errors NEVER embed the raw response body or request URL.
  Object-not-found, validation-failed, and query-failed errors describe the
  caller's own object/query via the response body's `reasons` list only.
- **Secrets hygiene.** The OAuth client_secret is chain-consumed through the
  address book's `resolve_with_secrets` — this plugin declares no vault keys
  of its own (`get_required_vault_keys`/`get_declared_vault_keys` both
  return `[]`). The re-minted bearer token is never vaulted.
- **Financial-data sensitivity.** Zuora records (invoices, payments,
  subscriptions) carry real billing PII — this connector's field
  sensitivities are set at the DB-connector floor (0.5), one notch above the
  generic-SaaS floor (0.3) used for Jira/Salesforce records, reflecting the
  higher consequence of a leaked invoice or payment row.

## SQL-lockdown gate note

`src/` composes NO SQL-shaped strings anywhere — ZOQL queries flow through as
a caller-supplied JSON body field (`queryString`), never a literal string the
gate's S2 heuristic would match, and there is no database driver import
(`S0`). The gate's S2 heuristic DOES fire on the ZOQL-shaped literal test
fixtures in `tests/smoke_data_query.py` (query strings like `"SELECT Id, Name
FROM Account"` standing alone as a Python string literal) — foreign-tenant
ZOQL text, never platform SQL, allowlisted in
`quality_gates/sql_access_allowlist.txt`.

## Design provenance

This plugin predates the 2026-07-09 umbrella hardening pass that ratified
Snowflake/Salesforce/Jira/external-Postgres (`workbench/2026-07-09_enterprise_connectors_design.md`,
which explicitly marks Zuora "out of scope, not in the operator's four").
The original design (`workbench/2026-06-20_zuora_plugin_design.md`) is a
single, lighter-weight revision. This build applies the umbrella's since-
established security posture (generic topology-safe error messages,
chain-consumed secrets, sync verb shape) on top of that original design
rather than re-litigating it from scratch. (The umbrella's process_export
deny posture was removed platform-wide 2026-07-15 — see the Security
posture section above.)

## Key files

| File | Purpose |
|---|---|
| `src/zuora_plugin/constants.py` | Every magic value: address-book field names, known base_url environments, error codes, caps. |
| `src/zuora_plugin/app_config.py` | Resolves `zuora_tenant` from the address book; the client_secret is chain-consumed. |
| `src/zuora_plugin/http_client.py` | `ZuoraClient` — synchronous httpx client with cached, re-mintable bearer auth (client-credentials grant). |
| `src/zuora_plugin/errors.py` | Topology-safe error classification (`classify_zuora_response`) from the response body's `reasons` list. |
| `src/zuora_plugin/export_containment.py` | Own-copy workspace-root containment gate (`assert_export_path_allowed`) binding `export_allowed_roots` — admits `output_tsv_path` for `data_query`/`bulk_export`/`list_subscriptions`/`list_invoices`. |
| `src/zuora_plugin/billing_actions.py` | Pure verb implementations: `data_query`, `get_object`, `create_object`, `update_object`, `list_subscriptions`, `get_invoice`, `list_invoices`, `bulk_export` — the latter four write to `output_tsv_path` under the §5 override mechanism (see the migration section above). |
| `src/zuora_plugin/plugin.py` | The `ZuoraPlugin` EDGE provider — client lifecycle, error mapping, EDGE registration, `_export_path_gate`. |
