# Google Workspace Plugin (`g_suite_plugin`)

Article Layer: 1

Article Role: plugin_reference

Article Tags: planning-stage:execution, evidence-category:capability-reference, domain:google-workspace, domain:local-solet, domain:cloud-solet

Embedding Description: Google Workspace plugin reference — the one-time operator setup for connecting Gmail, Drive, Sheets, Docs, and Slides to a Workspace account (Desktop-app loopback flow for local solets, Web-application redirect flow for cloud), where the client ID, client secret, and account tokens live (address book vs vault), the 26 verbs with argument shapes, typed gsuite.* errors, recovery for a failed Google token exchange, where created sheets, docs, and slides land in Drive (My Drive root of the connected account) with how to build their URLs from returned ids, and how to create multi-tab spreadsheets from csv/tsv files and apply tab renames or cell formatting via the Sheets batch-update verb.

Solet-native access to Google Workspace — **Gmail, Drive, Sheets, Docs,
Slides** — for the solet, peers, and plans. It is the platform
capability, distinct from the operator's claude.ai-side
`mcp__claude_ai_Gmail`/`Drive`/`Calendar` connectors: this one lets the
solet itself act on Workspace, including on cloud solets.

## Security posture (why these libraries)

- **Official Google client libraries only** — `google-api-python-client`,
  `google-auth`, `google-auth-oauthlib`. No CLI wrapping, no third-party OAuth
  wrappers, and explicitly **not** the deprecated `oauth2client`. Library-first
  per the platform's development guidelines.
- **The library owns the token transport.** The authorization-code exchange
  runs through `google_auth_oauthlib.flow.Flow` (PKCE S256 + the confidential
  client secret); refresh runs through `google.oauth2.credentials.Credentials`.
  Nothing hand-rolls a token POST. `google-auth`'s crypto rides the
  actively-audited `cryptography` package.
- **Transitive footprint** (13 packages, all Google-official or standard auth
  primitives): `google-api-core`, `google-auth-httplib2`,
  `googleapis-common-protos`, `httplib2`, `oauthlib`, `proto-plus`, `pyasn1(+
  modules)`, `requests-oauthlib`, `uritemplate`. The two with historical CVEs
  (`httplib2`, `oauthlib`) resolve to versions well past those fixes, and the
  notable `oauthlib` CVE is provider-side (we are purely a client).

## Auth model (operator-decided)

- **Enterprise-only, single-account, full read/write.** The OAuth app is
  configured **user type = Internal** (locked to one Workspace org — no
  unverified-app consent screen, no test-user cap). Personal-Gmail support is
  out of scope.
- Per-account refresh/access tokens live in **vault** (scoped keys
  `<solet>.g_suite_plugin.refresh_token` / `.access_token`). Google refresh
  tokens are durable and are **not** rotated on each refresh (unlike Schwab), so
  the refresh path updates only the access token.
- App identity (client_id, redirect_uri, and a `vault::` client-secret
  reference) lives in the **address book** entry `google_oauth_app`. The secret
  is chain-consumed by the address book under its own identity — the plugin
  never declares or reads it directly.

## One-time operator setup (runbook)

**This is not the flow for personal Google accounts.** The OAuth app is
Internal-only, which requires a Google Workspace org (see "Auth model" above).

The whole flow hands the solet two things from one Google Cloud OAuth
app: a **client ID** (public — it appears in every consent URL; lives in the
address book) and a **client secret** (confidential — moves by file into the
vault, never through an agent conversation). A one-time browser approval then
vaults the durable account tokens. Local solets use a Desktop-app client
with a loopback redirect (proven live 2026-07-13, origin); cloud solets use a
Web-application client behind the ALB.

### Stage 1 — Google Cloud console (browser)

This stage creates the project, enables the five product APIs, and mints the
OAuth client.

1. Log in: https://console.cloud.google.com
2. Project picker (top bar) → **New project** → name it → **Create** → select it
3. Open each link → click **Enable**:
   - https://console.cloud.google.com/apis/library/gmail.googleapis.com
   - https://console.cloud.google.com/apis/library/drive.googleapis.com
   - https://console.cloud.google.com/apis/library/docs.googleapis.com
   - https://console.cloud.google.com/apis/library/sheets.googleapis.com
   - https://console.cloud.google.com/apis/library/slides.googleapis.com
4. Go to: https://console.cloud.google.com/auth/overview → **Get started** →
   app name + support email → Audience: **Internal** → **Create**
5. Left nav: **Data access** → **Add or remove scopes** → check these five →
   **Update** → **Save**:
   - `/auth/gmail.modify`
   - `/auth/drive`
   - `/auth/documents`
   - `/auth/spreadsheets`
   - `/auth/presentations`
   (`gmail.modify` and `drive` are Google restricted/sensitive scopes — a
   Workspace admin may need to allow them even for an Internal app)
6. Go to: https://console.cloud.google.com/auth/clients → **Create client** →
   pick the type by deployment:
   - **Local solet:** type **Desktop app** → **Create**. No redirect URI
     registration — Google accepts loopback redirects from Desktop clients on
     any port.
   - **Cloud solet:** type **Web application** → redirect URI exactly
     `https://<solet-fqdn>/oauth/google/callback` (the ALB path-routes
     `/oauth/google/*` to the callback port).

### Stage 2 — hand over the app identity

The client ID is safe to paste to the agent driving setup; the secret is not —
it goes into a file, the vault verb reads the file server-side, and the value
never enters model context.

7. Copy **Client ID** → paste it to the agent (it goes in the address book)
8. Copy **Client secret** → in Terminal: `pbpaste > ~/.gsuite_client_secret.txt`
9. Store it: vault `store_from_file` with
   `key=<solet>.default_address_book_plugin.google_client_secret`,
   `file_path=/Users/<user>/.gsuite_client_secret.txt` → then delete the file.
   Sanity-check the file BEFORE storing (35 chars, `GOCSPX-` prefix; check via
   `wc -c` / `grep -c '^GOCSPX-'`, never by printing it) — a stale clipboard is
   the #1 failure in this flow.
10. Register the `google_oauth_app` address-book entry
    (`address_book_service::register`, address_type `api`) with three
    field_type/value entries:
    - `client_id` = the literal ID
    - `client_secret` = `vault::<solet>.default_address_book_plugin.google_client_secret`
    - `redirect_uri` — local: `http://127.0.0.1:<port>/oauth/google/callback`
      (any free port; it only has to match what `start_interface` binds —
      nothing is registered with Google) · cloud: the exact HTTPS URI from
      step 6

### Stage 3 — connect the account

The plugin runs a small callback server; the operator approves once in the
browser; the callback exchanges the code and vaults the tokens. Google refresh
tokens are durable (no rotation), so this never needs re-running.

11. Dispatch `start_interface` with the port from step 10 (local: host
    `127.0.0.1`)
12. Dispatch `connect_account` → open the returned `authorize_url` → approve
13. Verify: `gmail_list_messages` with `{"max": 3}` returns real message ids
    (not `gsuite.not_connected`), and the token keys
    `<solet>.g_suite_plugin.refresh_token` / `.access_token` exist
    (existence-check only)

Note for agent-driven setup: the connector verbs are export-denied on bridge
surfaces (RATIFY-3), so a bridge session cannot `process_call` them directly —
route dispatches through the solet (`submit`) or ask the current `sys:autonomic`
holder session to dispatch and report the action id.

### Recovery — failures seen in the field

- Browser shows `{"detail":"Google token exchange failed."}` and the log shows
  `(invalid_client) The provided client secret is invalid` → the vaulted
  secret is wrong (usually clipboard drift at step 8). The vault is
  write-once: `delete` the key, re-run steps 8–9 with a verified file, then
  re-dispatch `connect_account` — every run mints a fresh state nonce, and a
  spent or stale nonce is harmless.
- `authorize_url` errors `redirect_uri_mismatch` immediately → Web-application
  client whose registered URI doesn't byte-for-byte match the address-book
  `redirect_uri`; align the two.
- Keychain note (macOS vault): the stored value is wrapped as an RFC 2397 data
  URI — comparing the raw Keychain string against the plaintext secret will
  always "mismatch"; decode before comparing.

## Verbs (built — 26 total)

| Verb | Args | Returns |
|---|---|---|
| `connect_account` | — | `{authorize_url, state, redirect_uri, instructions}` |
| `start_interface` | `host?`, `port?` | `{host, port, callback_url}` |
| `stop_interface` | — | `{stopped}` |
| `gmail_list_messages` | `query?`, `max?` (default+cap 500) | `{messages:[{id, thread_id}], count}` |
| `gmail_get_message` | `id` | `{id, thread_id, snippet, headers, body_text, attachments}` |
| `gmail_send` | `to`, `subject?`, `body?`, `attachments?` | `{id, thread_id}` |
| `drive_list_files` | `query?`, `max?`, `acknowledge_default_limit_override?`, `row_limit?` (default 500, cap 1000) | `{files:[{id, name, mime, modified, size}], count}` |
| `drive_download_file` | `id` | `{file_blob_key, name, mime}` |
| `drive_upload_file` | `name`, `blob_key`, `parent?`, `mime?` | `{id, web_view_link}` |
| `drive_create_folder` | `name`, `parent?` | `{id}` |
| `drive_share` | `id`, `email`, `role` | `{ok, permission_id}` |
| `sheets_create` | `title` | `{id}` |
| `sheets_create_from_files` | `title`, `tabs` (`[{name, file_path}]`, absolute `.csv`/`.tsv` paths) | `{id, tabs:[{name, sheet_id}], updated_cells}` |
| `sheets_get_values` | `id`, `range`, `acknowledge_default_limit_override?`, `row_limit?` (default 500, cap 1000, post-fetch fail-loud) | `{values}` |
| `sheets_update_values` | `id`, `range`, `values` | `{updated_cells}` |
| `sheets_append_values` | `id`, `range`, `values` | `{updated_cells}` |
| `sheets_batch_update` | `id`, `requests` | `{replies}` |
| `sheets_export` | `id`, `format?` (csv\|xlsx) | `{sheet_blob_key}` |
| `docs_create` | `title`, `content?` | `{id}` |
| `docs_get` | `id` | `{title, body_text}` |
| `docs_batch_update` | `id`, `requests` | `{replies}` |
| `docs_export` | `id`, `format?` (pdf\|docx\|txt) | `{doc_blob_key}` |
| `slides_create` | `title` | `{id}` |
| `slides_get` | `id` | `{slides:[{object_id, element_count}], count}` |
| `slides_batch_update` | `id`, `requests` | `{replies}` |
| `slides_export` | `id`, `format?` (pdf\|pptx) | `{deck_blob_key}` |

Errors are typed with the `gsuite.*` prefix: `gsuite.not_connected`,
`gsuite.auth_expired`, `gsuite.permission_denied`, `gsuite.rate_limited`,
`gsuite.not_found`, `gsuite.invalid_params`, `gsuite.result_too_large`
(`sheets_get_values` only, see below).

## Retrieving a background job's result without a live listener

Every mutating or long-running verb here is born-async (the D0.3
deferred-completion shape): the dispatch call returns
`{"job_id": ..., "status": "queued"}` in milliseconds, the work runs on the
plugin's background thread, and the finished result is delivered as a message
to a live listening session. A session that invoked the verb through a plain
`<name> call` holds no listener, so the completion has nowhere to land — the
job still finishes; only the delivery is skipped.

From such a session, fetch the finished payload directly (measured 2026-08-14):

```bash
<name> call service_interface::job_service::get_latest_job '{"plugin_name": "g_suite_plugin", "action_name": "sheets_create_from_files"}'
```

- `status: completed` → the job's `result` field carries the verb's payload
  (for a sheet creation, the spreadsheet id/URL).
- `status: pending`/`processing` → poll again; the job has not finished.
- `status: failed` → the job's `error` field carries the failure.
- `job: null` → nothing matched the filters (no such job has been created —
  check `plugin_name`/`action_name` spelling; this is a normal empty answer,
  not an error).

One limit worth knowing: the verb returns the LATEST matching job only, so
two dispatches of the same verb from a no-listener session leave the earlier
job unreachable by THIS route. When the `job_id` from the dispatch envelope
is still in hand, fetch that exact job instead — it cannot be displaced by a
later dispatch:

```bash
<name> call service_interface::job_service::get_job '{"job_id": "<the dispatched job_id>"}'
```

Both verbs attach the job's stored `result` and `error` payloads to the row
they return (measured 2026-08-14 — the ledger row itself has no such
columns; the payloads live in a companion store and are read back for you).

## Business-data limits migration (2026-08-02) — g_suite is LIMITS-ONLY

Unlike jira_plugin/marketo_plugin/zuora_plugin's full data-export treatment
(`workbench/2026-08-02_business_data_limits_and_spill_floor_design_coordinator_day.md`),
g_suite's three previously-ungoverned list/read verbs
(`gmail_list_messages`, `drive_list_files`, `sheets_get_values`) got a
**resource guard only** — operator scope refinement, arm-4f6174762777dfe2fa66b8d409bb373b:
the mass-exposure/PII concern the data-export requirement exists for does not apply to
g_suite. So there is **no containment gate, no caller-supplied-path
requirement, and no inline-branch deletion** here — all three verbs still
return inline, same as before; only the row bound changed.

- **`gmail_list_messages`**: default AND cap both raised to 500 — Gmail's own
  real single-call maximum (Reviewer-D's census), kept explicitly by the
  operator. No `acknowledge_default_limit_override`/`row_limit` pair exists
  on this verb: the default already sits at the vendor's per-call ceiling,
  so there is nothing an override could raise to without building
  `pageToken` pagination across multiple calls (out of this slice's scope).
  A `max` above 500 clamps down silently — not the §5 flag-based friction,
  since there's no ceiling for a flag to unlock here.
- **`drive_list_files`**: default raised to 500 (the general policy number);
  cap raised to 1000 (Drive's own real single-call maximum) — genuinely
  reachable via `acknowledge_default_limit_override=true` + `row_limit`. An
  optional `max` still lets a caller request FEWER within whatever ceiling
  applies, without invoking the override.
- **`sheets_get_values`**: the riskiest of the three pre-migration (no bound
  of any kind). Default 500 / cap 1000 rows, but **OURS-ARBITRARY** — no
  vendor citation exists for a `values.get` row ceiling (Sheets has no
  server-side size parameter for this call at all, unlike `maxResults`/
  `pageSize`). Enforced **POST-FETCH**: a returned grid over the effective
  limit raises `ResultTooLargeError` (`gsuite.result_too_large`), never a
  silent truncation — but this does **not** reduce the underlying vendor
  call's size; narrowing the requested A1 range is still the caller's job
  for that, and the process description says so explicitly.

All three follow the same override-pair discipline as the full-floor
connectors where an override exists (`drive_list_files`): both-or-neither
fails loud, naming which half was missing; a `row_limit` above the hard cap
is refused, never silently clamped.

`drive_download_file` is the **worked blob-export example**: it returns a
`file_blob_key`, and its `get_edge_process_definitions()` entry declares
`("file_blob_key", 0.3)`. Every export/download verb (`sheets_export`,
`docs_export`, `slides_export`) replicates this: each declares
`field_sensitivities` for its `*_blob_key`, or the process registry fails to
build (`edge_process_mismatch`, a FATAL trap — see
`03_writing_plugins/PLUGIN_AUTHORING_TRAPS.md`).

Google-native docs (Sheets/Docs/Slides) cannot be fetched with
`drive_download_file`'s `get_media` — they are rejected with a pointer to the
matching export verb. All three export verbs share one implementation,
`drive_actions.export_media_to_blob`, which calls Drive's `export_media` (not
the product-specific service) regardless of which API created the file.

## Where created files land

Files minted by the create verbs land in the **root of "My Drive" of the
connected account** — the Workspace account that approved consent — owned by
that account, in no folder, shared with nobody. Set these expectations with
the operator whenever the solet creates a Workspace doc:

- **Look in the connected account.** A browser signed into a different Google
  account sees nothing in Drive, and the file's link shows "Request access"
  instead of the document. Switch accounts via the avatar menu on that page.
- **The create verbs return bare ids, not links.** Construct the URL:
  - sheet: `https://docs.google.com/spreadsheets/d/<id>`
  - doc: `https://docs.google.com/document/d/<id>`
  - deck: `https://docs.google.com/presentation/d/<id>`
  - uploaded file: `drive_upload_file` is the exception — it returns
    `web_view_link` directly.
- **Folder placement:** `drive_upload_file` accepts a `parent` folder id
  (mint one with `drive_create_folder`), but the Google-native create verbs
  (`sheets_create` / `docs_create` / `slides_create`) take no parent — those
  files always start in the My Drive root. If folder placement matters,
  move the file in the Drive UI afterward, or extend the verb surface with a
  parent argument (not built as of 2026-07-14).
- **Visibility to others:** nothing is shared by default; grant access with
  `drive_share` (`id`, `email`, `role`).

Default formatting preferences (operator-set 2026-07-14; per-request wishes
always override): hand over the document link immediately after creation;
money columns as Sheets Number → Financial (`#,##0.00;(#,##0.00)`);
percentages at 1 decimal (`0.0%`); auto-fit column widths bounded at roughly
2× the default width. Apply them via `sheets_batch_update` (repeatCell number
formats, updateDimensionProperties/autoResizeDimensions widths) after creating
the sheet, when the user hasn't asked for something else. Multi-tab
spreadsheets from prepared data files: `sheets_create_from_files` — it returns
each tab's Google-assigned `sheet_id`, which is exactly what those
`sheets_batch_update` formatting requests address, so create-then-format needs
no `addSheet`-reply lookup.

## Status

All five products (Gmail, Drive, Sheets, Docs, Slides) are built and
whole-tree gate-clean. Scopes for all five were granted in one consent set at
connect time, so no re-consent was needed when Docs/Slides shipped.
