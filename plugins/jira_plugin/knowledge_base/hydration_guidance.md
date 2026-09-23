# jira_plugin — Hydration Guidance

Article Layer: 2

Article Role: hydration_guidance

Article Tags: planning-stage:solet-lifecycle, evidence-category:operations-runbook, domain:local-solet, domain:jira

Embedding Description: Operator-facing pitch and setup steps for connecting a solet's jira_plugin to a Jira Cloud site with the operator's Atlassian API token (issues, JQL search, comments, transitions, attachments), surfaced during hydration if this plugin is present but not yet connected.

## Pitch

With `jira_plugin` installed but no site registered, every Jira verb returns `jira.not_configured` — the plugin is present and harmless, but does nothing. Connecting it lets the solet read and act on a Jira Cloud site: issue reads and JQL search, plus — because this connector is full read/write (operator-ratified) — creating, updating, transitioning, commenting on, and **permanently deleting** issues.

Auth is one Atlassian API token — either the operator's own token (actions are then attributed to the operator's Jira user; the sanctioned personal-token path) or a dedicated service account's. No browser flow, no callback server. If the operator already uses a Jira CLI with a stored token, that same token works here. Ask before doing any of it: "This solet can read and modify your Jira site as you — including creating, transitioning, and permanently deleting issues under your username. That needs one Atlassian API token (about 2 minutes to mint, or reuse an existing one if you know its expiry). Want to set this up now, or later?"

## Setup

Full step-by-step detail lives in this plugin's own overview article (`plugins/jira_plugin/knowledge_base/01_jira_overview.md`, "Credentials — the `jira_site` address-book entry") — this is the condensed version for the hydration conversation:

1. **API token.** The agent hands the operator the direct URL (`https://id.atlassian.com/manage-profile/security/api-tokens`); the operator's ONLY acts are Create (default ~1-year expiry is fine — the agent computes `expires_at` from it) and **Copy**. Prefer minting fresh over reusing a token with unknown expiry.
2. **Harvest and seed the token agent-blind.** The moment the operator says "copied," the agent pulls the clipboard itself — `pbpaste` redirected straight into a temp file, contents never displayed — seeds it via `vault_service::store_from_file` (strip whitespace) into `<solet>.default_address_book_plugin.jira_api_token`, then deletes the temp file and clears the clipboard. Never ask the operator to run terminal commands, manage files, or paste the token into chat — browser clicks are the operator's only acts (same UX bar as the g_suite consent flow).
3. **Register the `jira_site` address-book entry**: literals `base_url` (`https://<org>.atlassian.net`), `email` (the token owner's), `expires_at` (ISO-8601 — validated at config-load, fails loud there rather than at first connect), `scope_note`, plus `api_token` = the `vault::` reference from step 2.
4. If the plugin is not in the live manifest, add it via the blue-green manifest deploy. Then verify with a cheap read (fetch one known issue or a one-row JQL search) — expect real issue data back, attributed to the token's user.

Token rotation is agent-blind and yearly (or on the recorded `expires_at`): mint a new token, re-seed the vault key, update `expires_at` in the entry. On decline: stop, leave the plugin dormant. `jira.not_configured` on every verb is the fully-supported steady state, not a broken one.
