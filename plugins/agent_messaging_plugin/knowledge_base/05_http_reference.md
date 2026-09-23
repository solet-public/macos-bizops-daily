# HTTP Reference

Article Layer: 1

Article Role: plugin_reference

Tags: knowledge:tag:plugin_reference, knowledge:tag:agent_messaging, knowledge:tag:bridge, knowledge:tag:http_surface

Article Tags: planning-stage:server-side-internals, planning-stage:verifier-authoring, evidence-category:http-route-contract, evidence-category:bridge-event-shapes, domain:agent-messaging, domain:bridge, domain:http-surface

Embedding Description: The localhost FastAPI route table exposed by agent_messaging_plugin under /api/v1/bridge, covering bridge lifecycle, the platform-call surface (process_*, download), peer messaging, the bridge-delivery EDGE_SINK processes that feed channel events back to MCP clients, and the Codex-specific solet peer-message notification used by the local patched Codex CLI.

## Purpose

Document the localhost HTTP API exposed by `agent_messaging_plugin`.
This surface is consumed by the in-tree Python MCP stdio bridge
(`python -m agent_messaging_plugin.mcp_bridge`), by verifier scripts
that exercise the bridge directly without an MCP client, and by any
future native HTTP integration.

Every MCP tool the agent sees (`mcp__<server-name>__*`) maps to one of the
routes below; the MCP bridge subprocess forwards the call, polls
the event queue, and bridges results back as MCP notifications. Most
events use `notifications/claude/channel`; Codex peer wake events use
`notifications/solet/peer_message`.

## Bind point

```
PortManager: bridge
Runtime port file: ~/.ananta/runtime/<solet>.bridge.port
API prefix: /api/v1/bridge
Bridge id prefix: agc-
```

The port is allocated dynamically at `start_interface`; **no port
literal appears anywhere** — not in profile config, not in MCP
client config. Callers must read the runtime port file
(`~/.ananta/runtime/<solet>.bridge.port`) rather than
hard-coding a value. The MCP bridge subprocess discovers the port
via `SOLET_NAME` env var plus the port file.

**Writer, by topology (D11 ruling, 2026-07-13,
`workbench/2026-07-13_d11_bridge_port_discovery_routerless_ruling.md`):**
router-owned when a router is deployed (the blue-green router's
installer writes it); bridge-plugin-owned in router-less topologies
(`agent_messaging_plugin.start_interface` calls
`write_routerless_bridge_port_file` after confirming its own bind,
gated on a manifest-declared — never runtime-probed — router-presence
check). One writer per topology; the read-side contract above is
identical either way.

## Full route table

```
# Bridge lifecycle
POST   /api/v1/bridge/open
POST   /api/v1/bridge/{bridge_id}/close
GET    /api/v1/bridge/{bridge_id}/events?after=N    (long-poll)

# Platform call surface (agent → solet)
POST   /api/v1/bridge/{bridge_id}/process/search
POST   /api/v1/bridge/{bridge_id}/process/schema
POST   /api/v1/bridge/{bridge_id}/process/call
GET    /api/v1/bridge/{bridge_id}/process/result/{action_id}
GET    /api/v1/bridge/{bridge_id}/download/{blob_id}

# Peer messaging (agent → other live agents)
POST   /api/v1/bridge/{bridge_id}/peer/register
GET    /api/v1/bridge/{bridge_id}/peer/list
POST   /api/v1/bridge/{bridge_id}/peer/send
POST   /api/v1/bridge/{bridge_id}/peer/send_by_name
GET    /api/v1/bridge/{bridge_id}/peer/inbox

# Health
GET    /api/v1/bridge/health
```

All `bridge_id`-scoped routes enforce strict bridge ownership: a
request whose URL `bridge_id` does not match the resource's
recorded owner returns an analogous 403/404 code for peer and
platform-call paths.

## Bridge lifecycle

```
POST /api/v1/bridge/open
  body: {}
  resp: { "bridge_id": "agc-…", "long_poll_timeout_seconds": 25 }

GET  /api/v1/bridge/{bridge_id}/events?after={cursor}
  long-poll; returns queued events in cursor order. Each event is
  one of: channel_message (post_message), bridge_delivery_result,
  bridge_delivery_error, peer_message. The cursor also
  ACKS: events at or below `after` are drained server-side, the caller's
  peer binding is touched (liveness), and — for a role-addressed delivery
  to a no-MCP watcher (`agi-watch-*`) — the ack advances the role-replay
  consumption mark (`mark_role_consumed_on_ack`), so the pull recipient
  having provably streamed it retires that role message from replay.

POST /api/v1/bridge/{bridge_id}/close
  body: {}
  resp: { "status": "closed" }
```

Each bridge owns a platform session id
(`session_manager.create_session` under the
`agent_messaging_plugin` namespace). The session id appears on
every thread row's `originator_session_id` and in every
bridge-delivery `trigger_data["session_id"]`.

Idle bridges are reaped by the bridge-lifecycle sweeper (REL-09): a
plugin-owned daemon thread runs `BridgeSessionManager.sweep_idle` every
`bridge_sweep_interval_seconds` (default 300), expiring bridges idle past
`bridge_idle_timeout_seconds` (default 3600). Every expired bridge gets
the SAME full cleanup as an explicit close — inference-provider sidecar
clear plus tombstone, the sys:autonomic Trigger-2 succession hook, and
the peer-registry unregister — so an idle-swept session is
indistinguishable from a cleanly-closed one. At startup the plugin also
purges every persisted peer_binding row (pre-restart zombies; live
sessions re-register on reconnect).

## Platform call surface

These routes back the four direct `process_*` tools and
`download`. See `02_platform_call_surface.md` for the
agent-facing semantics.

```
POST /api/v1/bridge/{bridge_id}/process/search
  body: { "query": "...", "top_k": 8 }
  resp: { "processes": [ { "process_key", "description", "score" }, ... ] }

POST /api/v1/bridge/{bridge_id}/process/schema
  body: { "process_key": "service_interface::..." }
  resp: { "invocation_schema": { ... full JSON Schema ... } }

POST /api/v1/bridge/{bridge_id}/process/call
  body: { "process_key": "...", "arguments": { ... } }
  resp: { "action_id": "act-..." }   # 202 Accepted; result via events

GET  /api/v1/bridge/{bridge_id}/process/result/{action_id}
  resp: { "status": "in_progress" | "completed" | "failed",
          "payload": <structured result on completed/failed> }

GET  /api/v1/bridge/{bridge_id}/download/{blob_id}
  resp: streamed blob bytes with Content-Type and Content-Disposition
        from the blob metadata
```

`process_call` is the zero-inference path: the action is submitted
to the orchestrator and the structured result is bridged back via
`plugin::agent_messaging_plugin::deliver_result` (or
`...::deliver_error`), emitted on the channel as
`bridge_delivery_result` / `bridge_delivery_error`. No inference
runs unless the bridge-delivery contract itself is violated.

## Peer messaging

```
POST /api/v1/bridge/{bridge_id}/peer/register
  body: { "agent_id": "codex", "agent_instance_id": "agi-...",
          "session_label": "codex on baroque-suite" }

GET  /api/v1/bridge/{bridge_id}/peer/list
  resp: { "agent_ids": ["claude_code", ...],
          "instances": { "<agent_id>": [ {agent_instance_id, session_label, ...}, ... ] } }

POST /api/v1/bridge/{bridge_id}/peer/send
  body: { "peer_id": "claude_code",
          "peer_agent_instance_id"?: "agi-...",   # required if N>1
          "content": [ {"type": "text", "text": "please review ..."} ] }
  resp: { "delivery": "queued_notification" | "queued_wake"
                    | "queued_watcher",
          "thread_id": "...", "message_id": "..." }
  # A4 (2026-08-04): delivery is unconditional — every send is
  # delivery-attempted against the resolved recipient's live binding, no
  # sender-declared marker gates it. A resolved-but-stale binding fails the
  # call loud with peer_unreachable instead of returning a delivery kind
  # that will never drain; the message is still durably persisted.
  # A queued_* delivery means the platform placed a turn-triggering event on the
  # recipient's live bridge queue; emission to the client happens at the
  # forwarder's next drain, and whether it becomes a turn is client-side and is
  # NOT confirmed by this field (REL-06). There is no per-message
  # consumption-insurance apparatus anymore (the REL-05 direct-wake outbox
  # retired with A4) — staleness is caught at the session level instead,
  # via the recipient's own report_by / sweep_overdue_sessions contract.
  # queued_watcher = the recipient is a no-MCP `<name> watch` binding: the same
  # queued event streams into the watch output (an idle session with the
  # `<name> wake` Stop hook installed starts a turn on it; a busy or hook-less
  # one sees it at its next look), and the watcher's /events cursor ack stamps
  # it consumed.

POST /api/v1/bridge/{bridge_id}/peer/send_by_name
  body: { "name": "Git-Controller",
          "content": "please commit ..." }
  resp: { "delivery": "queued_notification" | "queued_wake" | "queued_watcher"
                    | "queued_for_replay",
          "thread_id": "role:Git-Controller",
          "message_id": "...",
          "resolved_agent_id": "claude_code",
          "resolved_agent_instance_id": "agi-..." }
  # queued_for_replay: the role's current holder has no live, polling
  # binding — the envelope is durable and replays on that holder's next
  # attach via the surviving role-replay path.

GET  /api/v1/bridge/{bridge_id}/peer/inbox?after=<recent-ISO-time>
  resp: { "entries": [ {sender_agent_id, sender_agent_instance_id,
                        sender_session_label, message, ...}, ... ],
          "role_entries": [ {sender_agent_id, sender_agent_instance_id,
                              sender_session_label, message, ...}, ... ],
          "next_after_created_at": "...",
          "instance_exhausted": false,
          "next_role_cursor": "..." }
  # Instance mail is exhausted only when instance_exhausted is true under its
  # existing forward timestamp cursor; equal-timestamp tie safety is not implied.
```

See `03_inter_agent_messaging.md` for the delivery
contract, multi-instance addressing rules
(`peer_ambiguous` / `peer_unreachable`), and native-channel wake
routing for Claude Code and the locally patched Codex CLI.

For Codex bridges, `peer_message` and `post_message` events are emitted
on the MCP transport as:

```text
notifications/solet/peer_message
  params.content = readable peer envelope + message prose
  params.meta    = bridge metadata, including thread/message ids,
                   sender ids, recipient ids, cursor, and
                   trigger_turn=true
```

This method is intentionally narrow. Bridge-delivery result/error events
and non-peer Codex events still use `notifications/claude/channel`.

## post_message (solet → agent)

This is the IO interface surface. Direct HTTP submission of
`post_message` is not supported — the model authors
`plugin::agent_messaging_plugin::post_message` as a normal EDGE_SINK
action, the action queue executes it, and the plugin enqueues a
`channel_message` event onto the bound bridge:

```
notifications/claude/channel
  params.content      = the message text (plain prose)
  params.meta.source           = "solet"
  params.meta.event_type       = "channel_message"
  params.meta.flow_id          = originating flow id
  params.meta.cursor           = monotonic event cursor
```

See `processes/post_message.json` for the schema and error tokens.

## Bridge delivery (EDGE_SINK pair)

`agent_messaging_plugin` exposes two EDGE_SINK processes that the
platform's bridge-delivery dispatcher submits on behalf of any
direct `process_call`:

- `plugin::agent_messaging_plugin::deliver_result` — appends a
  `bridge_delivery_result` event with the structured payload from
  the called process.
- `plugin::agent_messaging_plugin::deliver_error` — appends a
  `bridge_delivery_error` event with the platform's fixed failure
  payload.

Both are terminal (no result/error processors) so the dispatcher's
submission cannot recurse into inference.

These keys are NOT denied from direct MCP exposure — the export-deny list
(including the enumerated agent_messaging "private surface") was emptied by
operator ruling 2026-07-15 (see
`workbench/2026-07-15_result_error_processing_architecture_deep_dive.md`).
In normal operation only the platform submits them as a side effect of
completing a `process_call`'d action; a caller invoking them directly would
just be appending a delivery event to its own bridge, which is harmless since
the delivery target is server-derived from the caller's own identity, never
caller-supplied.

## Error code reference

| HTTP | code | When |
|---|---|---|
| 400 | `agent_request_invalid` | malformed `peer_send`/`peer_inbox` body, oversize content, etc. |
| 400 | `peer_ambiguous` | `peer_send` to a kind with multiple registered instances and no `peer_agent_instance_id` hint |
| 404 | `bridge_not_found` | URL `bridge_id` is unknown |
| 404 | `peer_unreachable` | targeted `peer_agent_instance_id` is not registered |
| 503 | `agent_messaging_disabled` | plugin config `enabled: false` |

`bridge_id` ownership is the canonical authorization model. There
is no user-principal reclaim across bridge reconnects; historical
thread rows (including retired backend-dispatch threads) remain
queryable via state SQL but are not addressable through the public
HTTP surface.
