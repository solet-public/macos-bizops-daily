# Inspecting a Marketo Campaign Before Triggering It

Article Layer: 1

Article Role: capability_reference

Article Tags: planning-stage:execution, evidence-category:capability-reference, domain:marketo, domain:campaign-safety

Embedding Description: What can and cannot be discovered about a Marketo Smart Campaign before `trigger_campaign` runs it — the flow steps (what the campaign DOES to people: send email, alert sales, change score, move program status) are exposed by no Marketo REST endpoint and there is no dry-run or preview, while the campaign's metadata and its smart-list triggers and filters (who qualifies, what fires it) sit behind the separate Read-Only Asset permission this plugin does not use; includes the recommended caller posture of triggering only self-authored campaigns, why the flow is readable only by a human in the Marketo UI, and why a plugin-side simulated dry-run is refused.

`trigger_campaign` runs a Smart Campaign's flow against people you supply. That
flow may send email, alert sales, change scoring, or move program status —
irreversible, externally visible effects on real humans. **Marketo's REST API
provides no way to read that flow first.** This article records why, so callers
learn it from the plugin rather than from an abandoned test, and so nobody
re-derives it.

Established 2026-07-29 against Adobe's published Marketo Developer Guide, in
response to a field report from a live Marketo instance.

## The flow is not readable, and this is permanent

Adobe's Asset API endpoint reference publishes **133 distinct
`/rest/asset/v1/…` paths. None of them is a flow endpoint.** `Get Smart
Campaign by ID` returns a `flowId` integer, and no published endpoint resolves
a `flowId` into anything at all. The identifier dangles by construction.

There is also **no dry-run, preview, or simulate** operation for Request
Campaign anywhere in the reference.

Do not read this as "not implemented yet in the plugin." It is a vendor
boundary. Treat a request for a flow-reading verb as answered: the answer is
that the data is not served.

## What IS readable — and why it is not a safety check

The asset surface does expose the campaign's shape. Availability is the exact
inverse of what a caller wants:

| question | readable? | endpoint |
|---|---|---|
| WHO it will act on (filters) | yes | `GET /rest/asset/v1/smartCampaign/{id}/smartList.json?includeRules=true` → `rules.filters[]` |
| WHAT FIRES it (triggers) | yes | same call → `rules.triggers[]` |
| WHETHER it is API-requestable | yes | `GET /rest/asset/v1/smartCampaign/{id}.json` → `type`, `isActive`, `isRequestable`, `isSystem` |
| WHAT IT DOES to those people | **no** | nothing resolves `flowId` |

**Reading the filters tells you the scope of an action whose consequence is
unreadable.** A campaign with no filters and a "Campaign is Requested — Web
Service API" trigger may mail every lead passed to it; nothing in the readable
set distinguishes that from a campaign that only stamps a field. Presenting any
of the above as "the campaign has been inspected" would report a state that is
not the state — the same defect class as a write that returns success for a
discarded update.

Two limits on the readable set:

- Every endpoint above requires the **`Read-Only Asset`** permission, which is
  distinct from the `Read-Only Campaigns` / `Read-Only Lead` /
  `Read-Only Activity` permissions this plugin's verbs use. The plugin calls no
  `/rest/asset/v1/…` path today, and `check_setup` — whose probes are all
  Lead-API reads — cannot detect whether an API user has it. Never assume an
  operator's existing API role carries it; state the requirement and let them
  verify.
- The smart-list endpoints support only user-created smart lists, not built-in
  or system ones.

## The recommended caller posture

**Trigger only campaigns the caller authored.** This is guidance, not an
enforced predicate: there is no authorship field on any campaign response, so a
gate claiming to check it would degrade to "whatever the caller asserts."

When a campaign's flow genuinely must be understood first, the only honest route
is a **human reading it in the Marketo UI**. The campaign's own
`computedUrl` (form: `#SC<campaignId>A1` on the instance's app host) is that
deep link.

After triggering, `get_activities` reports what the run actually caused. That is
an after-the-fact audit, not a preview — and it is the only observation of
consequence this API offers.

One compounding trap in how a caller reaches this verb: `trigger_campaign` needs
a `campaign_id`, and `list_campaigns` is the way to find one — but on a large
instance the full set can exceed the effective row limit (500 default, 5,000
with an acknowledged override — `list_campaigns` pages internally across
Marketo's 300-per-call ceiling and hides that from the caller (2026-08-03
hide-paging change), and even a complete listing means the caller is reading the
result back out of a workspace TSV file, not an inline list. So the
documented route asks a caller to pick from a list they may not be able to
read in full, in order to fire a flow they cannot inspect. Prefer a campaign
whose id is already known and whose flow the caller authored, over discovery
by enumeration.

## Refused: a plugin-side dry-run

Synthesizing a preview by inferring likely effects would be a fabricated
preview — reporting a state that is not the state. It is named here as refused,
with the reason, so it is not proposed again.

One near-miss to reject explicitly: `Schedule Campaign`
(`POST /rest/v1/campaigns/{id}/schedule.json`) accepts a `runAt` and always
waits at least five minutes, which can look like a safe rehearsal window. **The
endpoint reference contains no cancel or unschedule operation.** A scheduled
campaign is a delayed real run that cannot be recalled — a worse hazard than
`trigger_campaign`, not a safer one.

## The one route that would expose flow steps

Adobe's **Marketo Engage MCP Server** (`https://marketo-mcp.adobe.io/mcp`)
documents a smart-campaign surface that includes flow steps. It is not a design
input for this plugin: limited availability (beta, access by form submission), a
separate host with a separate credential path, and — unresolved — whether its
flow-step operation reads a given campaign's configured flow or returns the
instance's catalog of available step types for authoring. Adopting it would mean
embedding an MCP client in a plugin, which is an architecture decision and an
entitlement question for the operator, not an implementation detail.
