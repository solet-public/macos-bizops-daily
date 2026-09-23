# marketo_plugin — Hydration Guidance

Article Layer: 2

Article Role: hydration_guidance

Article Tags: planning-stage:solet-lifecycle, evidence-category:operations-runbook, domain:local-solet, domain:marketo

Embedding Description: Operator-facing pitch and setup steps for connecting a solet's marketo_plugin to a Marketo Engage instance with OAuth client-credentials (lead query/CRUD including delete, campaign trigger, static list membership), surfaced during hydration if this plugin is present but not yet connected. Includes checking for and, if needed, walking the operator through the API-only Role/User/LaunchPoint setup Marketo requires.

## Pitch

With `marketo_plugin` installed but no instance registered, every Marketo verb
returns `marketo.not_configured` — the plugin is present and harmless, but
does nothing. Connecting it lets the solet read and act on a Marketo
Engage instance directly: leads, campaigns, lists — including permanently
deleting lead records (`delete_leads`) and irreversibly merging lead records
(`merge_leads` — with `merge_in_crm=true`, the merge also reaches and
permanently combines the synced CRM record). Both are genuinely destructive,
unlike `zuora_plugin`'s no-delete posture. Ask before doing any of it: "This
solet can read and modify your Marketo instance — leads, campaigns,
lists — including permanently deleting or merging lead records (a merge can
also permanently combine the synced CRM record). That needs a Marketo
API-only user (may already exist) and about 5 minutes in the Marketo admin
UI. Want to set this up now, or later?"

Auth is OAuth client-credentials — the simplest in the connector suite: one
client_id + client_secret pair, no browser consent screen, no callback
server, no token expiry ceremony.

## Setup

Marketo REST access needs three admin-console objects, in order: an
**API-only Role** (specific "Access API" permission checkboxes), an
**API-only User** assigned that Role, and a **LaunchPoint Custom Service**
bound to that user (which mints the client_id/client_secret). **Check first**
— many instances already have this from a prior integration (Bizible, a
Salesforce sync, another marketing-ops tool): ask the operator to open Admin
→ LaunchPoint and say whether there's an existing Custom Service to reuse
before building a new one.

Full step-by-step detail (exact permission checkboxes, console paths, the
standard agent-blind secret-ingestion procedure) lives in this plugin's own
overview article
(`plugins/marketo_plugin/knowledge_base/01_marketo_overview.md`, "Registering
the Role, API-only User, and LaunchPoint service") — this is the condensed
version for the hydration conversation:

1. **New service?** Role (Access API: `Read-Write Person`, `Read-Only
   Activity`, `Read-Only Campaign`, `Execute Campaign`) → API-only User →
   LaunchPoint Custom Service → Get Token.
2. **Reusing an existing service?** Skip straight to Get Token on it.
3. **Copy** the client secret (the operator's only browser act beyond
   clicking through the above) — the agent harvests it via `pbpaste` into a
   temp file (never displayed), seeds it through `vault_service::
   store_from_file`, then deletes the file and clears the clipboard. Never a
   bespoke seed script.
4. Register the `marketo_instance` address-book entry (`base_url`,
   `client_id`, `client_secret` = the vault reference).
5. If not in the live manifest, add it via blue-green deploy.

**Round-trip count: 2** — "copied" after Get Token, and confirming
new-vs-reuse for the LaunchPoint service. Everything else is the agent's.

## Why this is secure

The client secret never enters model context or logs: clipboard harvest goes
straight to a file the model never reads back, verified only structurally
(byte count), then `store_from_file` puts it server-side into the
OS-encrypted vault under the address book's own namespace — this plugin
never reads the raw secret directly. Temp file deleted and clipboard cleared
immediately after seeding. Honest residual: the clipboard holds it in
cleartext for the few seconds between copy and harvest, and a clipboard
manager or cross-device sync could in principle capture it in that window.

## Verify — `check_setup` is the "check" step

Once `marketo_instance` is registered: run `test_connection` (credentials
valid?) then `check_setup` (what does the Role actually grant?).
`check_setup` runs six safe read-only probes and names the exact missing
Access API permission for anything that fails, plus which admin screen fixes
it. It **cannot** check write/execute permissions (`create_or_update_leads`,
`delete_leads`, `merge_leads`, `add_leads_to_list`, `remove_leads_from_list`,
`trigger_campaign`) without performing them — those are listed as
`writes_unverified` and will surface as `marketo.permission_denied`, naming
the gap, on first real use if the Role is short one.

There is a **second** limit worth saying to the operator, because it is the one
they alone can settle: all six probes are Lead-API reads, so a green
`check_setup` says nothing about Marketo's separate **`Read-Only Asset`**
entitlement, which gates the `/rest/asset/v1/…` surface (programs, emails,
landing pages, smart-campaign smart lists). No verb here touches that surface
today, so there is nothing for the plugin to probe and nothing to 403 — the
answer lives on the operator's own Admin → Users & Roles screen. Do not add it
to the four checkboxes above on this plugin's account; just never assume an
existing API role carries it, and say it is a requirement to verify rather than
something this setup has confirmed.

On decline: stop, leave the plugin dormant. `marketo.not_configured` on every
verb is the fully-supported steady state, not a broken one.
