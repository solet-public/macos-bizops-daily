# Snowflake Plugin (`snowflake_plugin`)

A query (and, per the operator's 2026-08-09 posture reversal + Amendment 1,
write) connector over the operator's Snowflake warehouse account.
Executor: `snowflake-connector-python`. Auth: key-pair (RSA JWT) as the
operator's own Snowflake user — no PAT, no browser flow, no callback server
(the platform's durable-non-interactive-auth principle: a solet runs
headless and unattended, so no connector should ever need a human to click
through a login). Most operators already query Snowflake exactly this way
from local scripts — same user, same key pair, same read-only role — and the
connector reuses that setup unchanged, mirroring the Salesforce connector's
ride-the-operator's-own-login pattern.

Single account v1: the "snowflake_account" address-book entry. Per-account
entries (`snowflake::<name>`) are a v2 extension if a second account appears.

## Read/write posture (reversed for write 2026-08-10, operator ruling
## 2026-08-09 + Amendment 1 — "vendor RBAC is the control plane")

The original posture (RATIFY-2) was READ-ONLY, HARD, on the operator's
dividing line: *"if someone deleted a database that would be a big deal."*
That ban is REVERSED (user complaints about connectors that cannot
manage/write the services they integrate — same ruling that reversed
`external_postgres_plugin`'s posture). What survives from the original
reasoning is an engineering constraint, not an access-control invention:
Amendment 1 withdrew the idea of any plugin-side write gate ("Don't invent
problems to solve — especially ones that the systems we are connecting to
already have solutions built in"). So: every READ verb stays read-only, hard,
exactly as designed; ONE write verb (`run_statement`) exists, and it performs
NO access-control classification of its own — the registered role's own
server-side grants are the entire control plane for what it can do.

**The asymmetry with `external_postgres_plugin` matters here.** Postgres has a
session-level `conn.read_only` connection characteristic that is LOAD-BEARING
for its read verbs — it structurally refuses any write at the server, even for
an over-privileged credential — and its write verb flips that characteristic
off per-call. **Snowflake has no equivalent session-level read-only flag at
all**, for either read or write verbs. So:

1. **The connector-side statement-leader guard is FAST-FAIL ONLY, read-verbs
   only** (belt, not boundary). It admits the Datagrip-parity read family
   `{SELECT, SHOW, DESCRIBE/DESC, EXPLAIN, WITH}` on `run_query`/`export_query`/
   the introspection verbs; everything else fails fast with
   `snowflake.read_only_violation`. It CANNOT, by itself, stop an
   over-privileged role from writing, and `run_statement` never calls it at
   all — no read/write classification exists on the write path, by design.
2. **The TRUE developer-proof boundary for the READ verbs is the read-only
   ROLE** the connection is pinned to: `GRANT USAGE ON WAREHOUSE/DATABASE/
   SCHEMA` + `SELECT ON ALL/FUTURE TABLES` — no `INSERT`/`UPDATE`/`DELETE`/
   `MERGE`/DDL grants, if the operator wants those verbs to stay genuinely
   read-only. `test_connection` surfaces the resolved role so a misconfigured
   write-capable role is visible immediately. **For `run_statement`, this is
   inverted**: whatever that same role IS granted (write or not) is exactly
   what `run_statement` can do — there is no separate, more-privileged
   credential and no plugin-side gate standing between them. An operator who
   wants `run_statement` to actually write registers (or grants) a role with
   the needed privileges; an operator who wants every verb to stay read-only
   simply never grants this connector's role anything beyond `SELECT`/`USAGE`
   — the boundary is entirely the operator's registration-time decision, in
   Snowflake's own terms.
3. **Single-statement is native, for every verb including the write one.**
   Snowflake's `MULTI_STATEMENT_COUNT` defaults to 1 and this plugin never
   calls `execute_string`, so no statement-splitting parser is needed
   anywhere (unlike Postgres's `sqlparse` belt, which its own `run_statement`
   still reuses as a shape guard) — verified live against the operator's own
   account: a two-statement string submitted through `run_query` was refused
   by the driver itself, not by any plugin code.
4. **`run_statement` manages its own transaction, not a connection
   characteristic.** Snowflake sessions default to `AUTOCOMMIT=TRUE` (every
   other verb here is unaffected by this — a single read statement completes
   or fails on its own either way). `run_statement` explicitly disables
   autocommit on its own connection before executing, so a statement that
   turns out to produce a result set (e.g. a RETURNING clause, where the
   target object supports one) but arrives with no `output_tsv_path` can be
   rolled back instead of silently committing while discarding the returned
   rows — the same always-TSV, never-inline contract every export verb here
   already follows.

**v1 limitation (read verbs only):** scratch/temp writes
(`CREATE TEMPORARY TABLE`) are refused by a read-only role, same as the
Postgres connector's `CREATE TEMP` limitation on ITS read verbs. CTEs are the
covered path for scratch computation on the read side. `run_statement` has no
such limitation — a `CREATE TEMPORARY TABLE` there is just another statement
the registered role's grants either permit or refuse.

## Connecting the operator's account (one-time)

The connector runs as **the operator's own Snowflake user** pinned to a
**read-only role**. If your local scripts already connect with a key pair and
a read-only role, reuse exactly that — no new Snowflake objects, steps 1–2
collapse to "point the address-book entry at what you already have."

1. **Account identifier + username (operator does browser acts only).** Ask
   the operator to log into Snowflake in their browser and provide the
   Snowsight address-bar URL plus their username. The URL
   `https://app.snowflake.com/<orgname>/<account_name>` maps to account
   identifier `<orgname>-<account_name>` (driver host
   `<orgname>-<account_name>.snowflakecomputing.com`). The username shows in
   Snowsight's bottom-left profile menu or via `SELECT CURRENT_USER();`.
2. **Key pair — RSA 4096, no smaller** (operator policy, 2026-07-16: every
   platform keypair is 4096-bit minimum). If your user doesn't have key-pair
   auth yet, generate one (the driving agent runs this; agent-blind means the
   key CONTENTS never enter context — verify structurally via
   `wc`/fingerprints/`openssl rsa -noout -text | head -1`, never `cat`):
   ```
   openssl genrsa 4096 | openssl pkcs8 -topk8 -nocrypt -out rsa_key.p8
   openssl rsa -in rsa_key.p8 -pubout -out rsa_key.pub
   ```
3. **Attach + verify via a one-time browser-auth session** (proven live
   2026-07-16, origin -> branchmetrics). The agent runs
   `tools/attach_public_key.py <account> <user>` — it connects with
   `authenticator="externalbrowser"` (the operator approves the login in
   their own browser; SSO/MFA fine; no credential enters agent context),
   executes `ALTER USER <you> SET RSA_PUBLIC_KEY_2='<pub>'` (slot 2, so an
   existing slot-1 key on another machine keeps working), verifies the
   `RSA_PUBLIC_KEY_2_FP` fingerprint against the local
   `openssl ... -pubout -outform DER | sha256` value, and introspects
   `SHOW GRANTS TO USER` / `SHOW WAREHOUSES` / `SHOW DATABASES` so step 5's
   entry values come from real grants. Snowflake permits self-service
   `ALTER USER` on your own RSA key properties; if your org has disabled
   that, fall back to pasting the same `ALTER USER` statement in a
   worksheet (or your admin runs it).
4. **Read-only role** (the load-bearing boundary, §1): use the read-only role
   you already query with — `USAGE`/`SELECT` grants only, never
   `INSERT`/`UPDATE`/`DELETE`/`MERGE`/DDL. If you need to create one:
   ```sql
   CREATE ROLE IF NOT EXISTS <readonly_role>;
   GRANT USAGE ON WAREHOUSE <wh> TO ROLE <readonly_role>;
   GRANT USAGE ON DATABASE <db> TO ROLE <readonly_role>;
   GRANT USAGE ON ALL SCHEMAS IN DATABASE <db> TO ROLE <readonly_role>;
   GRANT SELECT ON ALL TABLES IN DATABASE <db> TO ROLE <readonly_role>;
   GRANT SELECT ON FUTURE TABLES IN DATABASE <db> TO ROLE <readonly_role>;
   GRANT ROLE <readonly_role> TO USER <you>;
   ```
   Personal-account caution: if your user has
   `DEFAULT_SECONDARY_ROLES = ('ALL')`, sessions authorize through ALL your
   granted roles, not just the pinned read-only one — unset it so the role
   pin stays the boundary (the step-3 tool prints this property so the
   exposure is visible immediately).
5. **Register the address-book entry** `snowflake_account` with the literal
   fields (`account`, `user=<you>`, `warehouse`, `database`, `schema`,
   `role=<readonly_role>`, `auth_method=key_pair`) and a `private_key` field
   holding `vault::<solet>.default_address_book_plugin.snowflake_private_key`.
   Ingest the PEM contents of `rsa_key.p8` into that vault key agent-blind
   via `vault_service::store_from_file` (the exact multi-line PEM text — a
   flattened key fails loud at config resolution, not at first connect; see
   `app_config.py`'s eager PEM parse). Replacing an existing vault key is
   `delete` + `store_from_file` — overwrite is refused (`vault.key_exists`)
   and `rotate` would move the plaintext through agent context.
6. **Zero-downtime key rotation** (when needed): Snowflake supports a second
   key slot — `ALTER USER <you> SET RSA_PUBLIC_KEY_2='<new key>'`, rotate
   the vault secret to the new private key, verify with `test_connection`,
   then `ALTER USER <you> UNSET RSA_PUBLIC_KEY` to drop the old slot.

## Security posture (mirrors the Jira/external-Postgres wave)

- **Foreign-target invariant.** No `account` parameter on any verb — every
  connection is built from the single registered `snowflake_account` entry.
- **No export deny.** Not denied from the MCP surface — call `process_call` on
  these verbs directly. The prior RATIFY-3 deny was retired by operator ruling
  2026-07-15: friction, not security, on a single-user substrate where every
  MCP session is the operator (see
  `workbench/2026-07-15_result_error_processing_architecture_deep_dive.md`).
- **Generic error messages for topology-leaking classes.** Auth, connection,
  permission, timeout, and warehouse-suspended errors NEVER embed the raw
  driver exception string (it carries account/user/warehouse topology).
  Object-not-found and query-syntax errors describe the caller's own
  query/object and may keep driver detail.
- **Secrets hygiene.** The private key is chain-consumed through the address
  book's `resolve_with_secrets` — this plugin declares no vault keys of its
  own (`get_required_vault_keys`/`get_declared_vault_keys` both return `[]`).

## SQL-lockdown gate note

`snowflake.connector` is not a recognized SQL-driver import root, so the
gate's S0 driver-import class does not fire — no S0 allowlist entry is
needed. The S2 raw-SQL-string class DOES fire on `query_actions.py`'s
`SHOW`/`DESCRIBE`-composed strings and the caller's own `run_query` text —
one whole-file `sanctioned-exempt` entry in `quality_gates/sql_access_allowlist.txt`
covers it (same mechanism as `external_postgres_plugin`'s and
`salesforce_plugin`'s SQL/SOQL-composing modules). `statement_guard.py`
contains no SQL string literals and stays fully gated.

## Key files

| File | Purpose |
|---|---|
| `src/snowflake_plugin/constants.py` | Every magic value: address-book field names, read-leader set, result caps, error codes. |
| `src/snowflake_plugin/app_config.py` | Resolves `snowflake_account` from the address book; eagerly parses the PEM private key to DER (fail-loud on a flattened key). |
| `src/snowflake_plugin/connection.py` | `snowflake.connector.connect(...)` with key-pair auth; session hardening (`ALTER SESSION SET STATEMENT_TIMEOUT_IN_SECONDS`); topology-safe error classification. |
| `src/snowflake_plugin/statement_guard.py` | The read-leader guard (fast-fail belt on the READ verbs only — NOT the write boundary; see the posture section above). Never called by `run_statement`. |
| `src/snowflake_plugin/query_actions.py` | Pure verb implementations: `run_query`, `run_statement` (the write verb), `list_databases`, `list_schemas`, `list_tables`, `describe_table`, `export_query`, `test_connection`. |
| `src/snowflake_plugin/plugin.py` | The `SnowflakePlugin` EDGE provider — connection lifecycle, error mapping, EDGE registration, D0.3 dispatch. |
| `src/snowflake_plugin/async_jobs.py` | D0.3 deferred-completion machinery: the single lazily-started background worker thread that does the real connect + query/statement I/O for every verb. |
| `src/snowflake_plugin/completion_templates.py` | Builds the `completion_handlers` block each async job's metadata carries, routing job completion back into the originating flow. |
