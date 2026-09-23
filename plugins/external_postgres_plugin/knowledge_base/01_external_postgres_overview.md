# External Postgres Plugin (`external_postgres_plugin`)

A **"super Datagrip"** over **foreign** Postgres databases the operator
registers — cloud RDS or local dev Postgres. It is deliberately NOT
`postgres_state_management_plugin` (the platform's own state-DB owner): the words
"external" vs "state_management" keep the roles disjoint in registry listings, KB
retrieval, and gate reports. This plugin never touches the platform's own
database; the containment guard refuses it role-independently, for every verb
including the write verb.

Every verb takes a connection **NAME** (never a DSN). Adding a database is a
one-time operator **registration** — no code change.

## Read/write posture — READ-only-hard reads, one write verb (RATIFY-2, reversed 2026-08-09)

**History, for context, not current policy:** the plugin originally shipped
READ-ONLY, HARD (RATIFY-2) — the operator's dividing line at the time was *"if
someone deleted a database that would be a big deal,"* so no write verb existed
at all. **The operator reversed that standing ban on 2026-08-09** ("users have
been complaining" about connectors that cannot manage what they integrate),
with **Amendment 1** the same day settling HOW: *"these systems all have RBAC
and we do not [need] to try to re-implement controls in our plugins."* So the
write verb (`run_statement`) exists, and the control plane for what it can
actually do is the registered credential's own server-side Postgres **GRANTs**
— never a plugin-side permission check, consent gate, or refusal default (that
whole class was explicitly withdrawn by Amendment 1).

**Every READ verb stays exactly as read-only-hard as before** — nothing about
the reversal touches them:

1. **LOAD-BEARING — the psycopg3 connection read-only characteristic.**
   `conn.read_only = True` is set BEFORE the first `execute()`, so psycopg3 emits
   the read-only characteristic at the `BEGIN` of *every* transaction including
   the first implicit one — there is **no write-capable window**. Every write
   (INSERT/UPDATE/DELETE/MERGE/CREATE/**CREATE TEMP**/DDL) fails at the server
   with **SQLSTATE 25006** on the first statement, regardless of statement
   leader, count, or smuggling — even `EXPLAIN ANALYZE <write>`, and even for an
   over-privileged registered credential. This is NOT a post-connect
   `SET default_transaction_read_only` (that would leave the first implicit
   transaction write-capable — the Codex BLOCKER this design fixed).
2. **BELT — the read-leader guard.** Admits only the Datagrip read family
   `{SELECT, WITH, EXPLAIN, SHOW, VALUES, TABLE}`; everything else fails fast with
   `external_pg.read_only_violation`. Defense-in-depth + UX, NOT the boundary.
   **This guard is READ-VERB-ONLY** — `run_statement` never calls it.
3. **BELT — single-statement per call** via a real SQL parser (`sqlparse`, BSD).
   The parser is **result-shape hygiene** (bounds `SET`-injection + multi-result),
   NOT write containment: a second statement that slips `sqlparse` still cannot
   write on a READ verb, because the read-only connection refuses it (25006).
   `sqlparse` was ratified over `pglast` (GPL-3.0) because the repo is publicly
   distributed and the parser is not the destruction boundary. `run_statement`
   reuses this SAME shape-only check (exactly one statement — an engineering
   convention for predictable commit semantics, not a permission gate) but never
   the read-leader guard above.

**v1 limitation (read verbs):** `CREATE TEMP TABLE` / scratch writes are refused
on a READ verb (they are writes). CTEs are the covered path for scratch
computation on a read connection. Use `run_statement` if an actual write is
needed.

**The write verb (`run_statement`):** opens with `read_only=False` instead of
the characteristic above. It performs NO plugin-side classification of what the
statement does — a DELETE is not refused, a syntactically-write-shaped
statement is not inspected — because that decision belongs entirely to the
server. A statement with no result set (the common INSERT/UPDATE/DELETE/DDL
case) commits and returns `rowcount` inline; a statement WITH a result set (a
`RETURNING` clause) routes through the same always-TSV export path as
`run_query` — rows are never inline, at any size — and its absence when the
statement DOES return rows rolls the whole write back rather than silently
discarding them while still committing.

Registration should still recommend the **least-privileged DB user the task
needs** (a read-only role for read-only registrations; a scoped write role
only where write access is actually intended) — this is the operator's own
registration-time decision in Postgres's own terms, not a plugin gate.
`test_connection` surfaces the connecting role so a misconfigured credential
is visible either way.

## Containment — never the platform's own database (§8.4)

The plugin can reach ONLY DSNs that exist as `external_pg::*` address-book
entries (no DSN/URL parameter on any verb). On top of that, `assert_foreign_target`
refuses the platform's own DB **instance** — keyed on `(host, port, dbname)`,
**role-independently**:

- refused: `dbname == SOLET_NAME` **AND** host ∈ {`localhost`, `127.0.0.1`, `::1`,
  the unix socket} **AND** `port == platform_pg_port`. This catches every role
  bound to the platform database — the refusal keys on the instance, not the role.
- allowed: a dev DB on `localhost` with a different dbname, or a same-named DB
  on a different host/port. localhost is a legitimate target *class*.

`_normalize_host` collapses a blank host **and** any absolute-path unix-socket
spelling (`/tmp`, `/var/run/postgresql`) to the socket sentinel, and a blank
port defaults to 5432 — so neither a blank-host nor a blank-port registration can
slip the guard. **Accepted v1 residual:** a non-loopback *alias* that resolves to
the local platform host would slip the literal host check; it requires a contrived
mis-registration, the read-only connection bounds any reach to reads, and the
registered-entries-only invariant is the primary containment. DNS/host-resolution
hardening + the cloud RDS-instance tuple are a v2 item.

### The `platform_pg_port` footgun

The guard compares against the platform's own Postgres port, read from THIS
plugin's own config (`plugin.yaml` `platform_pg_port`, default **5432**) — never
from the state plugin's config (that coupling is what the design avoids). **If the
platform ever runs Postgres off 5432, update `platform_pg_port` in this plugin's
config** or the guard will not recognize the platform instance.

## Exposure — direct `process_call`, same as any other plugin

These verbs are NOT export-denied. The prior RATIFY-3 deny that gated this
plugin (and jira/snowflake/salesforce/zuora) was retired by operator ruling
2026-07-15: on this single-user substrate the deny was friction, not security
— every MCP session is the operator — and bulk row data belongs in workspace
TSV exports rather than context (see
`workbench/2026-07-15_result_error_processing_architecture_deep_dive.md`).
Call `process_call` on these verbs directly, same as any other registered
process.

## Topology-safe errors

Driver exception strings embed host/port/db/user topology. So connection / auth /
permission / timeout error classes carry a **generic fixed message** and NEVER
`str(exc)`. Only the caller's-own-query classes (syntax, undefined object) carry
the server's primary diagnostic message (which describes the caller's SQL, not our
topology). Row payloads and result-blob keys carry `data_sensitivity` 0.5.

## Registering a connection (operator runbook)

Each connection is one address-book entry `external_pg::<name>` with literal
`host`/`port`/`dbname`/`user`/`sslmode` fields plus a `vault::` password
reference. The password goes into vault **agent-blind** under the address-book
resolver's own namespace
(`<solet>.default_address_book_plugin.external_pg_<name>_password`) and is
chain-consumed via `resolve_with_secrets` — this plugin owns no vault keys and
needs no vault binding.

```
process_call service_interface::address_book_service::register {
  "name": "external_pg::analytics",
  "address_type": "database",
  "description": "Read-only analytics RDS",
  "entries": [
    {"field_type": "host",     "value": "analytics.abc123.us-west-2.rds.amazonaws.com"},
    {"field_type": "port",     "value": "5432"},
    {"field_type": "dbname",   "value": "analytics"},
    {"field_type": "user",     "value": "analytics_readonly"},
    {"field_type": "sslmode",  "value": "require"},
    {"field_type": "password", "value": "vault::<solet>.default_address_book_plugin.external_pg_analytics_password"}
  ]
}
```

- **Registration is create-only.** `register` fails on a duplicate name
  (`NAME_EXISTS`) — re-registering does NOT silently repoint an existing
  connection; changing a target is a deliberate delete-then-register (operator
  action).
- **JDBC-URL convenience.** The `app_config.parse_jdbc_url` helper decomposes a
  `jdbc:postgresql://user:pass@host:port/dbname?sslmode=require` (or plain
  `postgresql://…`) URL into the discrete fields for registration. The password is
  extracted to its own field and **never logged** — both the resolved DSN and the
  parsed-registration objects redact the password in `repr`, and parse failures
  scrub the URL.
- `test_connection` returns the resolved `server_version`, `current_user`, and
  `read_only` so the operator can confirm a connection points where they expect
  before trusting it.

## Verb map (all EDGE)

Every verb below except `list_connections` is **born-async** (D0.3
deferred-completion shape, 2026-08-09/10): the dispatch call returns
`{job_id, status: "queued"}` in milliseconds, and the "Returns" column below
describes what the JOB delivers when it completes — NOT this call's own
immediate return value. Conflating "the dispatch returned" with "the job
finished" is the doctrine's named trap; see
`workbench/2026-08-09_sync_verb_d03_deferred_completion_doctrine_syncverb-doctrine.md`.

| Verb | Args | Job-completion result |
|---|---|---|
| `run_query` | `connection_name, sql, output_tsv_path, acknowledge_default_limit_override=false, row_limit` | `{path, columns, row_count, truncated}` — written as ONE `.tsv` file at the caller's ABSOLUTE path, never rows inline; defaults to 500 rows, up to 1000 with an acknowledged override |
| `run_statement` **(WRITE)** | `connection_name, sql, output_tsv_path (only if the statement has a RETURNING clause), acknowledge_default_limit_override=false, row_limit` | No RETURNING: `{rowcount, has_result_set: false}`. RETURNING: `{rowcount, has_result_set: true, path, columns, row_count, truncated}` — same always-TSV shape as run_query for the returned rows |
| `list_connections` | — | `{connections: [name]}` (names only, never secrets) — still SYNCHRONOUS, not async: a single address-book scan, not a foreign-DB round trip |
| `list_schemas` | `connection_name` | `{schemas: [name]}` |
| `list_tables` | `connection_name, schema` | `{tables: [{name, kind}]}` |
| `describe_table` | `connection_name, schema, table` | `{columns: [{name, type, nullable, default}]}` |
| `export_query` | `connection_name, sql, output_tsv_path, acknowledge_default_limit_override=false, row_limit` | `{path, columns, row_count, truncated}` — the N>>500 route: same shape as run_query, defaults to 500 rows, up to 50000 with an acknowledged override |
| `test_connection` | `connection_name` | `{ok, server_version, current_user, read_only}` |

`export_query` writes to the operator's OWN workspace, never platform blob
storage (operator ruling 2026-07-15: bulk business data belongs in workspace
files). The path must be absolute, end in `.tsv`, and lie under an
operator-configured `export_allowed_roots` entry in this plugin's config —
realpath + `commonpath` containment mirroring `ledger_allowed_roots`. The
default `[]` REFUSES every export until the operator opts roots in; refusals
name the config key. There is no session-cwd inference and no `Path.cwd()` —
the caller supplies the path, the config supplies the containment.

Both `run_query` and `export_query` ALWAYS write their result to the caller's
`output_tsv_path` — never rows inline, at any size (business-data limits +
data-export migration, 2026-08-02; the former inline-return/byte-cap branch
is deleted, not lowered). Each defaults to 500 rows absent an acknowledged
override; `run_query`'s override ceiling is 1000, `export_query`'s is 50000
(the N>>500 route). `acknowledge_default_limit_override=true` together with
an explicit `row_limit` requests more than the default — both are required
together, and a `row_limit` above the verb's hard cap is refused, never
silently clamped. Neither verb has a vendor-imposed ceiling to defer to: this
connection is an arbitrary customer database, so both limits are entirely
our own policy. Columns are written to the `.tsv` header in query order, with
duplicate column names (a JOIN's `SELECT a.id, b.id`) preserved positionally
rather than collapsed. Non-primitive column values (timestamps, Decimals,
UUIDs) are coerced to their string form before writing.

## Dependencies + delivery

`psycopg[binary]>=3.2` (the executor; binary wheel avoids a libpq build dep) and
`sqlparse>=0.5` (the single-statement parser). Delivered **INERT**: after landing,
the operator runs `pip install -e plugins/external_postgres_plugin` into the shared
`.venv`. Until installed the plugin is dormant tree-ware.

The hermetic smokes run without a database. The three LIVE smokes
(`smoke_readonly.py`, `smoke_multistatement.py`, `smoke_write.py`) prove the
read-only boundary — and, for `smoke_write.py`, the write reversal actually
working end-to-end, including a commit visible from a SEPARATE connection —
against a local **scratch** Postgres database (`epg_smoke_scratch` on the
Homebrew cluster — NEVER the platform database), and skip cleanly when that
fixture is unreachable.
