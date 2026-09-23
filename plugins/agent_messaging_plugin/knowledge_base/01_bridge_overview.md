# Bridge Overview — The Three Hats Of `agent_messaging_plugin`

Article Layer: 1

Article Role: plugin_reference

Tags: knowledge:tag:plugin_reference, knowledge:tag:agent_messaging, knowledge:tag:bridge, knowledge:tag:overview

Article Tags: planning-stage:orientation, evidence-category:plugin-architecture, domain:agent-messaging, domain:bridge

Embedding Description: One-page map of agent_messaging_plugin and the three responsibilities it owns after the 2026-05 bridge consolidation — IO interface (post_message, start/stop_interface), bridge service (FastAPI surface, peer registry, bridge-delivery EDGE_SINKs, Python MCP stdio bridge, Claude-shaped notifications, and patched Codex peer wake), and durable agent-messaging core (core__agent_thread/core__agent_message schema, peer messaging, session-ledger substrate reads).

## Purpose

One-page map of `agent_messaging_plugin`. The plugin owns three
distinct contracts that historically lived in three plugins
(`claude_code_channel_plugin`, `agent_channel_plugin`, the original
`agent_messaging_plugin`). They were collapsed in 2026-05 into one
Python plugin with one in-tree Python MCP stdio bridge. The same
process surface (`mcp__<server-name>__*`) covers every responsibility.

## The three hats

### Hat 1 — IO interface

The plugin implements `ananta.interfaces.io_interface_plugin.IOInterfacePlugin`
alongside `discord_plugin` and `signal_plugin` (and, as the consolidated
localhost HTTP surface, replaces the retired `default_rest_plugin`).
Outbound traffic to the agent on the other end of the bridge is the IO
surface:

- `plugin::agent_messaging_plugin::post_message` — deliver a persisted
  assistant message to the bound MCP bridge session as a
  `channel_message` event.
- `plugin::agent_messaging_plugin::start_interface` —
  `plugin::agent_messaging_plugin::stop_interface` — lifecycle pair.
- `get_supported_capabilities() → {IOCapability.TEXT}` (text-only).

These follow the platform IO contract verbatim: standard error tokens
(`session_not_bound`, plain `ValidationError`, `APIError`), no
transport-specific namespace. See `02_platform_call_surface.md` for the
matching inbound surface (the direct process tools).

### Hat 2 — Bridge service

The bridge is the FastAPI + Python MCP stdio surface that backs every
`mcp__<server-name>__*` tool an agent sees. It owns:

- bridge-session lifecycle (`bridge_id` `agc-<hex>` per MCP client
  connection, idle sweep, event queues),
- peer registry (live MCP-connected agents addressable through
  `peer_send`/`peer_list`/`peer_inbox`),
- the bridge-delivery EDGE_SINK processes
  `plugin::agent_messaging_plugin::deliver_result` and
  `plugin::agent_messaging_plugin::deliver_error`, both in the
  `bridge.*` error namespace,
- the Python MCP stdio entry point at
  `python -m agent_messaging_plugin.mcp_bridge`, which discovers the
  solet HTTP port via `~/.ananta/runtime/<solet>.bridge.port` (no
  hardcoded port anywhere in client configs).

See `05_http_reference.md` for the full route table and
`02_platform_call_surface.md` for the agent-facing tool semantics.

### Hat 3 — Agent messaging core

The lowest layer hosts the durable schema and the live peer-messaging
surface:

- durable schema `core__agent_thread` (per-thread state) and
  `core__agent_message` (append-only cursor-addressable messages),
- `peer_send`/`peer_inbox` — live agent-to-agent talk between MCP
  sessions, documented in `03_inter_agent_messaging.md`,
- `list_threads`/`read_thread_messages` — the unscoped GAP-5/D1
  substrate reads the session-ledger projection consumes.

The dormant backend-dispatch surface that used to live here
(`open_thread`/`send_message`/`list_messages`/`get_status`/
`close_thread`, the `run_turn` EDGE process, the `GuardedAgentInterface`
backend) was retired in the D3 dormant-head retirement — it had zero
implementing plugins.

## Single FastAPI surface, single MCP server

Everything mounts under `/api/v1/bridge` on a dynamically-allocated
localhost port. MCP clients see one server (`<server-name>`) and one tool prefix
(`mcp__<server-name>__*`); the Python stdio bridge forwards each tool call to the
right `/api/v1/bridge/*` endpoint and bridges long-poll events back as
transport-specific MCP notifications. The default and Claude Code
method is `notifications/claude/channel`. For Codex sessions registered
with `AGENT_IDENTITY=codex`, peer-message wake events use
`notifications/solet/peer_message` so the locally patched Codex CLI can
route the message into its inter-agent mailbox and start a normal turn.
Port discovery is fully runtime: the bridge subprocess reads
`SOLET_NAME` from its environment, opens
`~/.ananta/runtime/<solet>.bridge.port`, and connects. That file
has exactly one writer per solet topology — the blue-green router
when this solet has one, `agent_messaging_plugin` itself (via
`write_routerless_bridge_port_file`, gated on a manifest-declared
router-presence check) when it does not. Minimal-bundle solets ship
without the router, so their bridge is discoverable only because of this
router-less writer path (D11 ruling, 2026-07-13,
`workbench/2026-07-13_d11_bridge_port_discovery_routerless_ruling.md`).
The discovery contract from the client's side is unchanged either way —
one filename, read the same way.

The MCP client caches this subprocess and its tool descriptors for the
life of the Codex session. If the solet restarts or a bridge source edit needs
to be loaded, refresh the live session with `/mcp reconnect` when
available, or restart through the configured Codex launcher. The local
patched-Codex runbook covers stale `agc-*` bridge ids and update procedure.

## Where to go next

- Agent calling the solet (`process_*`, `download`) →
  `02_platform_call_surface.md`.
- Agent calling another live agent (`peer_*`) → `03_inter_agent_messaging.md`.
- HTTP route table behind every MCP tool → `05_http_reference.md`.
- Per-process schemas, error tokens, recovery guidance →
  `processes/*.json`.
