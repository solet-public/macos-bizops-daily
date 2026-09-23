# Session-Start Orientation: Knowledge, Memory, Messaging, and Standing Governance

Tags: knowledge:tag:orientation, knowledge:tag:governance, knowledge:tag:operator_communication, knowledge:tag:knowledge_retrieval, knowledge:tag:agent_messaging

Article Layer: 2

Article Role: workflow_reference

Article Tags: planning-stage:always, evidence-category:workflow, domain:operator_communication, domain:knowledge_retrieval, consumer_profile:both

Embedding Description: The single page-zero article a fresh session lands on to orient itself at the very start: the knowledge-base search habit and its exact commands, why an empty result from one source is never the last word, the standing governance documents that settle direction and process questions, and — for launcher-run fleet sessions — how to check pending messages and confirm a held role after a restart or `/clear`.

**When you need this**: orienting at the very start of a session before doing anything else; deciding what to search next when a first knowledge-base query comes back empty; checking whether a governance or direction question is already settled before escalating it; a launcher-run fleet session confirming its inbox and role binding after a restart, `/clear`, or reconnect.

---

## Search the knowledge base, and do not stop at one empty source

A knowledge-base search is the normal first step for any non-trivial task, run through the deployment's own command line — the same one that just retrieved this article:

```sh
<the deployment's CLI> call service_interface::knowledge_service::search '{"query": "<plain-English description>", "top_k": 8}'
```

Once session history is indexed, the same command line also reaches prior-session decisions, status, and blockers:

```sh
<the deployment's CLI> call service_interface::session_ledger_service::search_event_content '{"query": "<what happened or was decided>", "limit": 8}'
```

There is always more than one knowledge base available to a session, and they are checked in sequence, not traded off against each other. The solet's own knowledge base comes first — it carries platform access and cross-session knowledge. The working directory the session is launched in comes next, regardless of what the first search returned: a project `CLAUDE.md` or `AGENTS.md`, project documentation, and any project-specific knowledge bases the working directory's own tooling exposes govern the task at hand, and neither source preempts the other. The sequence also settles what an empty result means: a search that comes back empty from one source is information about that source, not about the question — a reason to continue to the next source, never a reason to conclude nothing exists and stop looking.

## The four standing governance documents

A fresh session anchors on four standing documents, and each settles a different class of question:

- **The charter** settles what this solet is, its mission and cost boundary, its design values, and how decisions get made. Search the knowledge base for "charter" scoped to this solet, or for the template at `ananta/knowledge_bases/ananta_platform/24_operator_communication/02_solet_charter_template.md` if none exists yet.
- **The standing-positions document** holds the agents' argued, dated, revisable positions on the big picture, so a fresh session inherits a point of view instead of starting blank. Positions are advocacy; operator rulings close arguments and are recorded inline.
- **The decision-brief convention** at `ananta/knowledge_bases/ananta_platform/24_operator_communication/01_operator_decision_briefs.md` governs every request for an operator ruling. Its third rule matters at session start: settled questions do not get re-asked.
- **The collaboration craft conventions** at `ananta/knowledge_bases/ananta_platform/24_operator_communication/03_collaboration_craft.md` govern day-to-day working style with the operator: reporting, autonomy, credential flows, and close-out hygiene.

Before substantive work, a session confirms it is not about to relitigate something these documents already settle. Direction questions go to the charter and standing positions; process questions about operator communication go to the two conventions. If a genuine gap exists, the work proceeds and the gap is recorded where it belongs: a new argued position in the standing-positions document, or a decision brief if only the operator can close it.

## Messaging, role binding, and reattaching after a restart or /clear

Launcher-run fleet sessions can have direct and role-addressed messages waiting at session start.

**A session label is not a role claim.** A labeled session — one launched with a role name attached — holds no role binding by virtue of that label. Until the role is actually claimed, the session is addressable by nobody: a `peer_send_by_name` aimed at it fails as a vacant role, and the messages that would have reached it are simply never delivered. The label is visible and the missing claim is not, which is what makes this worth checking at session start rather than discovering when an expected message never arrives.

**Do not assume something already armed the claim.** Where a deployment's session hooks would normally claim the role automatically, that automation may be absent — a managed or policy-restricted environment can block the plugin that carries it from installing at all, leaving a labeled, role-less session with nothing to signal the gap. Claiming the role by hand (the `rename` skill, with the label as the role name) is a supported path in that situation, not a workaround. Either way the thing to trust is the ownership check below, not the assumption that automation ran.

In a launcher-run fleet session, with `AGENT_SESSION_ID` set in the environment, the catch-up read, on the same deployment command line, is:

```sh
<the deployment's CLI> call plugin::agent_messaging_plugin::peer_inbox "{\"agent_session_id\": \"$AGENT_SESSION_ID\"}"
```

The `agent_session_id` argument is the launcher-exported value, also echoed by `peer_register` and the watcher's armed line — the agent whose mail is read is resolved from that session id server-side, never named directly by the caller. The result carries two independently-paged sections, an instance section and a role section, each with its own cursor; the same command line's `schema` action for `plugin::agent_messaging_plugin::peer_inbox` documents both. A role message already read is not necessarily consumed for replay purposes — the surviving role-replay durability guarantee can still resurface it until its holder acks it directly — so a returned role entry is evidence a message was addressed to this session, not proof it is retired.

For a session that has claimed a durable role, the read-only ownership check, on the same command line, is:

```sh
<the deployment's CLI> call plugin::agent_messaging_plugin::peer_holds_role '{"name": "<role>", "agent_instance_id": "<agent-instance-id>"}'
```

`plugin::agent_messaging_plugin::peer_holds_role` takes the role's `name` and the session's own `agent_instance_id`, and answers whether the claim is still live rather than merely shown by presence in a peer listing. Where a role binding needs to be re-pointed to this session — for example after a `/clear`, restart, or reconnect — the `rename` skill performs that re-pointing. Which transport any of this runs over is read from `FLEET_TRANSPORT`, a stated, declared-never-probed value, never inferred by probing.

## Reference

- `service_interface::knowledge_service::search` — the knowledge-base search this article opens with.
- `service_interface::session_ledger_service::search_event_content` — prior-session decisions, status, and blockers, once indexed.
- `service_interface::memory_service::recall` — decisions, status, and cross-agent facts, distilled rather than searched verbatim.
- `plugin::agent_messaging_plugin::peer_inbox` — the catch-up read for direct and role-addressed messages, in launcher-run fleet sessions.
- `plugin::agent_messaging_plugin::peer_holds_role` — the read-only role-ownership check.
- `ananta/knowledge_bases/ananta_platform/24_operator_communication/01_operator_decision_briefs.md` — the decision-request convention.
- `ananta/knowledge_bases/ananta_platform/24_operator_communication/02_solet_charter_template.md` — the charter template with its platform-constant sections.
- `ananta/knowledge_bases/ananta_platform/24_operator_communication/03_collaboration_craft.md` — the distilled working conventions.
- `plugins/github_midwife_plugin/knowledge_base/04_first_days_runbook.md` — how the charter and standing positions come to exist for a new solet.
- `plugins/github_midwife_plugin/knowledge_base/08_deployment_report_card.md` — its compact session-start readiness table, an optional opening ritual that checks runtime, knowledge, and the role claim above in one pass before work begins. The same article's full report card is the close-out artifact, not a session-start step.
