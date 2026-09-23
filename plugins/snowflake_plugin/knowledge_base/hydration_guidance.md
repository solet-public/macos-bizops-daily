# snowflake_plugin — Hydration Guidance

Article Layer: 2

Article Role: hydration_guidance

Article Tags: planning-stage:solet-lifecycle, evidence-category:operations-runbook, domain:local-solet, domain:snowflake

Embedding Description: Operator-facing pitch and setup steps for connecting a solet's snowflake_plugin to the operator's own Snowflake warehouse account with RSA key-pair auth pinned to a read-only role, surfaced during hydration if this plugin is present but not yet connected.

## Pitch

With `snowflake_plugin` installed but no account registered, every Snowflake verb returns `snowflake.not_configured` — the plugin is present and harmless, but does nothing. Connecting it lets the solet run read-only queries and introspection against the operator's Snowflake warehouse (databases, schemas, tables, ad-hoc SELECTs), export full result sets as workspace TSV files, and (per the operator's 2026-08-10 write reversal) run a single write statement — INSERT/UPDATE/DELETE/DDL — through `run_statement`, gated entirely by whatever the registered role's own grants permit. An operator who wants this connector to stay read-only simply registers (or grants) a role with no write privileges beyond `SELECT`/`USAGE`; the plugin itself performs no separate write gate.

The connector runs as the operator's **own Snowflake user** pinned to whatever role the operator registers, authenticating with an RSA key pair — headless, durable, no login cycles. If the operator already queries Snowflake from local scripts with a key pair, setup is just pointing the plugin at what already exists. Ask before doing any of it: "This solet can run queries against your Snowflake account — as your own user, pinned to whichever role you register. A read-only role keeps it read-only; a role with write grants lets it also run a single write statement per call, gated entirely by that role's own Snowflake permissions, not by the plugin. If you already use key-pair auth locally we reuse it as-is; otherwise it's a one-time key setup. Want to connect it now, or later?"

## Setup

Full step-by-step detail lives in this plugin's own overview article (`plugins/snowflake_plugin/knowledge_base/01_snowflake_overview.md`, "Connecting the operator's account") — this is the condensed version for the hydration conversation:

1. **Key pair.** Ask the operator to log into Snowflake in their browser and provide the Snowsight address-bar URL (`https://app.snowflake.com/<org>/<account>` → account identifier `<org>-<account>`) plus their username. Reuse the operator's existing Snowflake key pair if one exists (4096-bit only — regenerate anything smaller); otherwise the agent generates one (`openssl genrsa 4096 | openssl pkcs8`; RSA 4096 minimum per operator policy 2026-07-16 — applies to all platform keypairs; agent-blind means the key contents never enter context — verify via `wc`/fingerprints, never `cat`) and attaches the public key itself via `tools/attach_public_key.py <account> <user>` — a one-time `externalbrowser` session the operator approves in their browser (proven live 2026-07-16); it also verifies the fingerprint and introspects grants/warehouses/databases for step 4's values.
2. **Role.** Use whichever role the operator already queries with, or a fresh one — `USAGE`/`SELECT` grants only for a read-only connector, or add write grants (INSERT/UPDATE/DELETE/DDL as needed) if the operator wants `run_statement` to actually write. This role IS the boundary (Snowflake has no session-level read-only connection flag, and the plugin performs no separate write gate — vendor RBAC is the entire control plane). Caution: if the user has `DEFAULT_SECONDARY_ROLES = ('ALL')`, unset it so the role pin stays the boundary.
3. **Seed the private key agent-blind** into vault at `<solet>.default_address_book_plugin.snowflake_private_key` (exact multi-line PEM — a flattened key fails loud at config resolution).
4. **Register the `snowflake_account` address-book entry**: literals `account`, `user=<operator's user>`, `warehouse`, `database`, `schema`, `role=<chosen role>`, `auth_method=key_pair`, plus `private_key` = the `vault::` reference from step 3.
5. If the plugin is not in the live manifest, add it via the blue-green manifest deploy (this also auto-ingests the plugin's knowledge base). Then verify with `test_connection` — it echoes the resolved role, so a role that doesn't match the operator's intent (read-only vs write-capable) is visible immediately.

On decline: stop, leave the plugin dormant. `snowflake.not_configured` on every verb is the fully-supported steady state, not a broken one.
