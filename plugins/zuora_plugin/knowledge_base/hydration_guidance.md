# zuora_plugin — Hydration Guidance

Article Layer: 2

Article Role: hydration_guidance

Article Tags: planning-stage:solet-lifecycle, evidence-category:operations-runbook, domain:local-solet, domain:zuora

Embedding Description: Operator-facing pitch and setup steps for connecting a solet's zuora_plugin to a Zuora billing tenant with OAuth client-credentials (subscriptions, invoices, data queries, create/update but never delete), surfaced during hydration if this plugin is present but not yet connected.

## Pitch

With `zuora_plugin` installed but no tenant registered, every Zuora verb returns `zuora.not_configured` — the plugin is present and harmless, but does nothing. Connecting it lets the solet work a Zuora billing tenant directly: subscriptions, invoices, object reads, ZOQL data queries, bulk exports, and — because this connector is create/update (operator-ratified) — new and modified billing objects. There is deliberately **no delete verb**: billing records are voided or cancelled through Zuora's own workflow, never deleted through this tool.

Auth is OAuth client-credentials — the simplest in the connector suite: one client_id + client_secret pair, no browser flow, no callback server, no token expiry ceremony (short-lived bearers are re-minted on demand and held only in memory). Ask before doing any of it: "This solet can read and modify your Zuora billing tenant — subscriptions, invoices, billing objects — though it structurally cannot delete anything. That needs one OAuth client created in the Zuora admin UI (~2 minutes). Want to set this up now, or later?"

## Setup

Full step-by-step detail lives in this plugin's own overview article (`plugins/zuora_plugin/knowledge_base/01_zuora_overview.md`, "Registering the tenant") — this is the condensed version for the hydration conversation:

1. **OAuth client.** Operator creates one in the Zuora admin UI against an API-appropriate user, and copies the `client_id` and `client_secret` immediately — Zuora shows the secret exactly once at creation.
2. **Pick the `base_url` — it IS the environment selector** (no separate prod/sandbox flag): `https://rest.zuora.com` (US production), `https://rest.eu.zuora.com` (EU production), or `https://rest.apisandbox.zuora.com` (sandbox). Point at the sandbox first if one exists.
3. **Seed the client secret agent-blind** into vault at `<solet>.default_address_book_plugin.zuora_client_secret` — it never enters model context.
4. **Register the `zuora_tenant` address-book entry**: literals `base_url` and `client_id`, plus `client_secret` = the `vault::` reference from step 3.
5. If the plugin is not in the live manifest, add it via the blue-green manifest deploy. Then verify with `test_connection` — expect tenant identity back, confirming the entry points at the environment you intended.

On decline: stop, leave the plugin dormant. `zuora.not_configured` on every verb is the fully-supported steady state, not a broken one.
