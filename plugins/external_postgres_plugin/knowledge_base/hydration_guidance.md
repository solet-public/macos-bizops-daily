# external_postgres_plugin — Hydration Guidance

Article Layer: 2

Article Role: hydration_guidance

Article Tags: planning-stage:solet-lifecycle, evidence-category:operations-runbook, domain:local-solet, domain:external-postgres

Embedding Description: Operator-facing guidance for external_postgres_plugin explaining that nothing is connected during hydration — connections to foreign Postgres databases (read-only by default over the session characteristic; write-capable only where the registered credential's own database GRANTs allow it) are registered on demand, any number of them, via a repeatable per-connection wizard run whenever the operator wants to reach a database server.

## Pitch

This plugin is different from the one-time connectors: it is a **registry of named connections**, and an empty registry is its normal starting state — not dormancy. There is **nothing to set up during hydration.** Mention the capability in one line and move on: "This solet can query — and, where the registered credential's own database GRANTs allow it, write to — any Postgres database (a Datagrip replacement — introspection, ad-hoc SELECTs, writes, TSV exports). Nothing to configure now; whenever you want to reach a database server, we register that connection on the spot."

Connections are added on demand, any number of them, each scoped to one database server and each with its own credentials. Reads always go over a connection-level read-only characteristic (`conn.read_only`) that refuses writes regardless of what the credential could do — that boundary is unaffected by anything below. Separately, ONE verb (`run_statement`) opens a non-read-only connection instead; what it can actually do there is entirely the registered credential's own server-side Postgres GRANTs — this plugin performs no write-permission check of its own (operator ruling 2026-08-09 + Amendment 1, "vendor RBAC is the control plane"). Consent is per-connection and happens at registration time, when the operator is naming a real host and picking which database role to register it under — not during hydration.

## Setup

This is a **repeatable per-connection wizard** — run it each time the operator asks to connect a database, once per connection. Full detail lives in this plugin's own overview article (`plugins/external_postgres_plugin/knowledge_base/01_external_postgres_overview.md`, "Registering a connection"):

1. **Pick a connection name** — it becomes the address-book entry `external_pg::<name>`. Registration is create-only: an existing name is refused (`NAME_EXISTS`), so pick a fresh name rather than expecting an overwrite.
2. **Collect the connection literals** — `host`, `port`, `dbname`, `user`, `sslmode` — from the operator directly or parsed out of an existing JDBC URL. Which database role to register matters now: the read verbs' session-level read-only characteristic refuses writes regardless of the credential, but `run_statement` opens without it, so what that credential's role is actually GRANTed decides what it can write — ask the operator to register the least-privileged role the intended use needs (read-only if writes are never wanted on this connection; a scoped write role if they are).
3. **Seed the password agent-blind** into vault at `<solet>.default_address_book_plugin.external_pg_<name>_password` — it never enters model context.
4. **Register the `external_pg::<name>` entry** (address_type `database`) with the literals plus `password` = the `vault::` reference from step 3.
5. **Verify** with `test_connection` naming the new connection — expect the resolved host, server version, current user, and `read_only: true` echoed back.

One structural guardrail: the platform's own database instance is refused by host+port+dbname tuple (`external_pg.platform_db_refused`) — other localhost databases are legitimate targets. Steady state with no (or few) connections is fully supported: `list_connections` simply returns what exists, and verbs naming an unregistered connection return `external_pg.connection_unknown`.
