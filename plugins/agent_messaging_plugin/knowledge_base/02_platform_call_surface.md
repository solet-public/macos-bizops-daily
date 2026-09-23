# Platform Call Surface — Agent Calling The Solet

Article Layer: 1

Article Role: plugin_reference

Tags: knowledge:tag:plugin_reference, knowledge:tag:agent_messaging, knowledge:tag:bridge, knowledge:tag:platform_surface

Article Tags: planning-stage:agent-onboarding, planning-stage:tool-discovery, evidence-category:mcp-tool-contracts, domain:agent-messaging, domain:bridge, domain:platform-surface

Embedding Description: How an external agent (Claude Code, Codex, ChatGPT over the tunnel) talks to the solet through the merged bridge — the four direct process tools (process_search / process_schema / process_call / process_result) for zero-inference invocation, download for blobs, and the MCP notification shapes the solet returns.

## Purpose

Document the agent-facing tools that let an external MCP client
(Claude Code session, Codex CLI session) drive the solet directly. These
are the inbound half of the bridge — the platform surface. For the
outbound assistant-message half (the solet → agent), see `post_message` in
`processes/post_message.json` and the channel-event shapes below.

All of these tools live on the `<server-name>` MCP server with the
`mcp__<server-name>__*` tool prefix. The same server also surfaces the
inter-agent tools (`peer_*`, `agent_*`) documented in
`03_inter_agent_messaging.md`.

## Core surfaces

The platform surface has five tools:

| MCP tool | Purpose |
|---|---|
| `mcp__<server-name>__process_search` | Discover process keys by semantic query |
| `mcp__<server-name>__process_schema` | Fetch the invocation schema for a process |
| `mcp__<server-name>__process_call` | Invoke a process directly, zero inference |
| `mcp__<server-name>__process_result` | Fetch the structured result of a prior `process_call` |
| `mcp__<server-name>__download` | Read a blob by id |

The four `process_*` tools are the zero-inference path for known,
named work — `process_search` to discover a key, `process_schema`
to confirm its arguments, `process_call` to invoke it, and
`process_result` to read the outcome. `download` is the blob
retrieval surface for results that reference blobs by id.

## Operating assumptions

- Ask the solet first about active work — through the knowledge, memory,
  and session-ledger search processes (`process_call`), or
  `peer_send` to a live peer session — before dropping to raw
  inspection. Channel notifications are authoritative platform
  messages.
- If the solet asks a clarification question (delivered as a `<channel>`
  event), answer through `peer_send` to the asking session.
- Keep unrelated work in separate sessions when possible — every
  bridge session has its own bridge_id and its own event queue.

## Current V1 limits

- Text only on `post_message`. No attachments.
- `process_call` does not accept attachments.
- Reconnect is not full conversational resume. If the bridge
  disappears and the client reconnects, a fresh bridge is opened
  and continuity may require briefly restating context.

## The four direct process tools

`process_search`, `process_schema`, `process_call`, and
`process_result` together let an agent invoke a registered solet
process without going through inference. This is the right path
when the agent already knows which process it wants.

### Discovery and schema

```
mcp__<server-name>__process_search(query="search the knowledge base", top_k=8)
  → list of {process_key, description, score}

mcp__<server-name>__process_schema(process_key="service_interface::knowledge_service::search")
  → full JSON Schema for the invocation arguments
```

### Direct invocation

```
mcp__<server-name>__process_call(
  process_key="service_interface::knowledge_service::search",
  arguments={"query": "...", "top_k": 8},
)
  → {action_id: "act-..."}
```

The call returns immediately with an `action_id`. The structured
result arrives on the channel as a `bridge_delivery_result` event
(see "Channel event shapes" below) — **no inference runs**. The
agent can also fetch the result synchronously:

```
mcp__<server-name>__process_result(action_id="act-...")
  → the structured payload, or an in-progress / error status
```

### Bridge-delivery EDGE_SINK pair

The result/error route uses the plugin's own EDGE_SINK pair:

- `plugin::agent_messaging_plugin::deliver_result` — emits the
  `bridge_delivery_result` event with the structured payload from
  the called process.
- `plugin::agent_messaging_plugin::deliver_error` — emits the
  `bridge_delivery_error` event with the platform's fixed failure
  payload (`error_message`, `process_key`, `action_id`,
  `failed_arguments`, `canonical_schema`).

Both are terminal (`edge_sink`, no result/error processors), so the
bridge dispatcher's submission cannot recurse into inference.

### Contract violations

When the bridge-delivery contract itself is violated (missing
`trigger_data`, unregistered `deliver_*` processes, originator
session id not matching the action session id), the platform
routes to inference for a human-readable explanation. This is the
documented escape valve and is the only path under which inference
runs for a direct `process_call`. The agent receives the diagnosis
through a normal `channel_message` event, not as a structured
delivery event.

## MCP notification shapes

The solet returns most work to the agent as `notifications/claude/channel`
events on the MCP transport. The MCP client uses `meta.event_type`
to route:

```text
notifications/claude/channel
  params.content      = stringified payload
                        (JSON for bridge_delivery_*,
                         free text for channel_message)
  params.meta.source           = "solet"
  params.meta.event_type       = "channel_message"          // post_message
                               | "bridge_delivery_result"   // deliver_result
                               | "bridge_delivery_error"    // deliver_error
                               | "peer_message"             // peer_send
                               | "agent_message"            // agent_send completion
  params.meta.flow_id          = originating flow id
  params.meta.cursor           = monotonic event cursor
```

For `bridge_delivery_*` events, `content` decodes to:

```json
{
  "payload": <raw result or error payload from the called process>,
  "source_process_key": "plugin::<ns>::<function>"
}
```

For `channel_message` events, `content` is the prose `post_message`
text — no JSON decode required.

Codex peer wake is the exception to the notification method. When the
recipient bridge is registered as `agent_id="codex"` and the queued
event is `peer_message` or `post_message`, the Python bridge emits:

```text
notifications/solet/peer_message
  params.content      = readable peer envelope + message prose
  params.meta         = full bridge metadata, including thread_id,
                        message_id, sender ids, recipient ids, cursor,
                        and trigger_turn=true
```

The local patched Codex CLI consumes only this narrow solet method, turns
it into Codex `InterAgentCommunication`, and reuses Codex's mailbox
queue. Bridge-delivery results, errors, and non-peer Codex events still
use the regular `notifications/claude/channel` shape.

## Safe defaults

When in doubt:

- Use `process_search` to discover the process key for an
  operation, then `process_call` to invoke it and `process_result`
  to read the outcome.
- If `process_search` finds no process for an operation, the
  capability does not exist on the platform surface — that is a
  capability gap to raise with the operator, not a phrasing problem.
- Interpret outbound messages as collaboration, not background
  logging.

## Where to go next

- Inter-agent messaging (`peer_*`, `agent_*`) →
  `03_inter_agent_messaging.md`.
- `post_message` schema and error tokens →
  `processes/post_message.json`.
- HTTP routes that back each MCP tool → `05_http_reference.md`.
