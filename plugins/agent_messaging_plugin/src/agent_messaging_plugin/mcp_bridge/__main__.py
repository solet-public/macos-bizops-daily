# pyright: reportUnusedFunction=false
"""Entry point for the Python MCP stdio bridge subprocess.

Launched by MCP clients via:

    [mcp_servers.<name>]
    command = "/path/to/<name>/.venv/bin/python3"
    args    = ["-m", "agent_messaging_plugin.mcp_bridge"]

    [mcp_servers.<name>.env]
    SOLET_NAME = "<solet>"

Discovers the solet HTTP API via the runtime port file
(`~/.ananta/runtime/{solet_name}.bridge.port`), opens a bridge
session, registers peer identity, and runs the MCP stdio server loop.
The poll loop forwards bridge events as `notifications/claude/channel`.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import secrets
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final

from ananta.core.runtime.port_manager import read_port_file
from mcp.server.lowlevel import NotificationOptions, Server
from mcp.server.models import InitializationOptions
from mcp.server.stdio import stdio_server
from mcp.types import TextContent, Tool

from ..env_contract import (
    AGENT_IDENTITY_ENV,
    AGENT_INSTANCE_ID_ENV,
    AGENT_ROLE_ENV,
    AGENT_SESSION_ID_ENV,
    AGENT_SESSION_LABEL_ENV,
    enforce_no_legacy_agent_env,
)
from .forwarder import Forwarder

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

SERVER_NAME: Final[str] = "solet"
SERVER_VERSION: Final[str] = "1.0.0"
BRIDGE_SERVICE_NAME: Final[str] = "bridge"
DEFAULT_AGENT_ID: Final[str] = "claude_code"
PORT_DISCOVERY_RETRY_S: Final[float] = 2.0

# Discriminator for the Codex agent kind. A Codex session spawns the bridge as
# its child, so CODEX_THREAD_ID is inherited.
CODEX_AGENT_ID: Final[str] = "codex"
AGENT_ROLE_AUTOBIND_ENV: Final[str] = "AGENT_ROLE_AUTOBIND"

# Per-agent-kind ordered carriers for the stable logical-session key
# (`agent_session_id`), highest precedence first. Resolution is keyed on the
# bridge's agent_id so one agent kind can NEVER adopt another's session id:
# `refresh_role_binding_cas` re-points roles filtered on `agent_session_id`
# ALONE, so a bridge nested under a foreign-kind parent that inherited the
# parent's carrier would re-point the WRONG session's roles (the hazard is
# bidirectional). A Codex bridge prefers its own `CODEX_THREAD_ID` —
# authoritative, definitionally this Codex conversation and never stale for a
# Codex child — then an exported `AGENT_SESSION_ID`. EVERY OTHER kind
# uses `AGENT_SESSION_ID` only and NEVER adopts `CODEX_THREAD_ID`.
# Unknown agent_ids take the default (non-codex) chain. All-absent → ""
# preserves the degraded, self-refresh-disabled binding.
SESSION_ID_ENV_VARS_BY_AGENT: Final[dict[str, tuple[str, ...]]] = {
    CODEX_AGENT_ID: (
        "CODEX_THREAD_ID",
        AGENT_SESSION_ID_ENV,
    ),
}
DEFAULT_SESSION_ID_ENV_VARS: Final[tuple[str, ...]] = (
    AGENT_SESSION_ID_ENV,
)

SERVER_INSTRUCTIONS: Final[str] = "\n".join(
    [
        "Solet platform bridge. Use the solet as the primary collaborator for active work.",
        "",
        "Two surfaces are exposed:",
        "",
        "== Platform-call surface ==",
        "",
        "The solet sends messages back as channel notifications tagged with",
        '   source "homunculus". These appear as <channel> elements.',
        "",
        "Ask the solet first for questions about current work, status, blockers,",
        "decisions, plan changes, or what happened in the active session.",
        "Use raw DB/log/file inspection for debugging,",
        "evidence gathering, or discrepancy verification.",
        "",
        "Direct process tools (process_search/_schema/_call/_result) let you",
        "invoke registered solet processes without going through inference. Use",
        "them when you already know which process you want.",
        "",
        "== Peer messaging (peer_send / peer_inbox) ==",
        "",
        "Talk to other LIVE MCP-connected agents (humans, Codex, Claude,",
        "whatever is registered in peer_list). Wake is a transport property,",
        "not a sender-declared one: every peer_send is delivery-attempted",
        "against the resolved recipient's live binding, waking it when a",
        "native adapter is registered, unconditionally -- there is no",
        "silent-vs-notifying mode to choose. A leading 'IMPORTANT:' in prose",
        "is stripped as input hygiene only; it no longer changes delivery.",
        "",
        "When you receive a peer_message notification, reply with substance,",
        "or stay silent if a reply adds no value (silence is always allowed).",
        'Do NOT reply with acknowledgements, thanks, or "got it" -- those',
        "create loops.",
        "",
        "Current limits: text-only channel, no attachments on direct calls,",
        "and reconnect may require briefly restating context if the prior",
        "bridge session was lost.",
    ],
)

# Tool descriptors. Descriptions are lifted from the Node bridges
# (claude_code_channel/mcp/server.mjs and agent_channel/mcp/server.mjs)
# verbatim where possible -- they are model-facing and have been tuned.

TOOLS: Final[list[Tool]] = [
    Tool(
        name="current_identity",
        description=(
            "Return identity and routing metadata for the current MCP session, "
            "including transport, solet_name, agent_id, "
            "agent_instance_id, agent_session_id, session_label, bridge_id, "
            "mcp_session_id, roles_held, and identity_trust. Use this to "
            "answer 'who am I?' or verify routing before peer_register, "
            "peer_claim_role, peer_send, or Streamable HTTP peer receive work. "
            "Returns no secrets."
        ),
        inputSchema={
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        },
    ),
    Tool(
        name="download",
        description="Download a blob from solet storage to a local file path.",
        inputSchema={
            "type": "object",
            "properties": {
                "blob_id": {
                    "type": "string",
                    "description": 'Blob ID to download (e.g., "bmd-abc123def456").',
                },
                "output_path": {
                    "type": "string",
                    "description": "Local file path to write the downloaded file to.",
                },
            },
            "required": ["blob_id", "output_path"],
            "additionalProperties": False,
        },
    ),
    Tool(
        name="process_search",
        description=(
            "Search the solet process registry for processes matching a "
            "natural-language query."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Natural-language search query."},
                "max_results": {"type": "integer", "default": 10},
            },
            "required": ["query"],
            "additionalProperties": False,
        },
    ),
    Tool(
        name="process_schema",
        description=(
            "Retrieve the invocation schema for a single solet process by "
            "its process_key."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "process_key": {
                    "type": "string",
                    "description": "provider_type::provider::function_name",
                },
            },
            "required": ["process_key"],
            "additionalProperties": False,
        },
    ),
    Tool(
        name="process_call",
        description=(
            "Direct invocation of a solet process by process_key.  Zero "
            "inference, deterministic, fast.  THE PREFERRED entry point "
            "for any known process — knowledge base searches "
            "(service_interface::knowledge_service::search), memory "
            "recall (service_interface::memory_service::recall), plugin "
            "tools (plugin::<plugin>::<function>), everything.  Returns "
            "action_id + flow_id immediately; the result arrives as a "
            "bridge_delivery_result channel notification — do not poll.  "
            "Use process_search first if you don't know the process_key "
            "and process_schema to confirm the argument shape."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "process_key": {"type": "string"},
                "arguments": {"type": "object"},
                "reason": {"type": "string"},
            },
            "required": ["process_key", "arguments"],
            "additionalProperties": False,
        },
    ),
    Tool(
        name="process_result",
        description=(
            "Snapshot read of an action_id's current observable state "
            "(status, error_message, latest stored raw result row if "
            "any). This is for diagnostics -- completion is delivered "
            "via channel notifications, not by polling here."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "action_id": {"type": "string"},
            },
            "required": ["action_id"],
            "additionalProperties": False,
        },
    ),
    Tool(
        name="peer_register",
        description="\n".join(
            [
                "Register or relabel this MCP session in the peer registry.",
                "",
                "Auto-registration: each transport registers this MCP session",
                "when it opens. Stdio uses $AGENT_IDENTITY when set and",
                "otherwise generates a durable agent_instance_id; Streamable",
                "HTTP uses the bearer-token claim. Call this tool manually",
                "only to change agent_id or session_label; the durable",
                "agent_instance_id stays the same so the registry replaces",
                "(not duplicates) the existing binding for this session.",
                "",
                "session_label is purely a human-facing display field (peer_list,",
                "envelope text). It is never used as a routing key.",
                "For identity introspection, use current_identity.",
            ],
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "agent_id": {
                    "type": "string",
                    "description": (
                        'Stable handle (e.g. "claude_code", "codex"); regex '
                        "[A-Za-z0-9._-]{1,64}."
                    ),
                },
                "session_label": {
                    "type": "string",
                    "description": (
                        'Optional human label for this session ("codex on '
                        'baroque-suite"). Defaults to the auto-inferred label.'
                    ),
                },
            },
            "required": ["agent_id"],
            "additionalProperties": False,
        },
    ),
    Tool(
        name="peer_list",
        description="\n".join(
            [
                "List currently-registered agent_ids (across all live bridges).",
                "",
                "Each instance entry carries agent_instance_id, session_label,",
                "parent_pid, created_at, and updated_at (all ISO-8601 UTC).",
                "created_at is the timestamp the bridge connected.",
                "updated_at advances on every dispatch operation (peer_send,",
                "peer_inbox, native wake) — it carries 'last active'",
                "semantics, so the newest updated_at points at the most",
                "recently active instance when multiple are registered",
                "concurrently.",
                "",
                "registered_at is also present as a deprecated alias for",
                "created_at; new code should read created_at + updated_at.",
            ]
        ),
        inputSchema={
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        },
    ),
    Tool(
        name="peer_send",
        description="\n".join(
            [
                "Send a peer message to another live MCP session.",
                "",
                "Operator management:",
                "  For fleet/task management, prefer peer_send_by_name with a durable",
                "  role name such as Coordinator, Coordinator-Dusk, Architect, or",
                "  Git-Controller. Use raw peer_send only for replies to a specific",
                "  sender instance or when the operator explicitly names a live",
                "  session. Do not fan one task out to many peer_list entries.",
                "",
                "Addressing -- multi-instance:",
                '  peer_id is the stable agent kind (e.g., "claude_code", "codex").',
                "  Multiple instances of the same agent_id can be registered",
                "  concurrently. peer_agent_instance_id picks a specific one:",
                "    - omit when only one instance of peer_id is registered",
                "      (single-binding default); the call delivers to it.",
                "    - supply when multiple are registered; otherwise the call",
                "      fails with peer_ambiguous, listing the candidate",
                "      instance_ids and session_labels.",
                "  Discover candidates via peer_list. For replies, take the",
                "  sender_agent_instance_id from the inbox entry or the",
                "  peer_message notification meta and pass it as",
                "  peer_agent_instance_id here. When the reply hint also",
                "  supplies peer_agent_session_id, pass both: peer_send resolves",
                "  the exact instance FIRST and consults the stable session key",
                "  only after that instance is peer_unreachable. A live instance",
                "  always wins, even if the session key points elsewhere.",
                "",
                "Delivery contract:",
                "  Messages are ALWAYS persisted in the (sender_bridge, peer_instance)",
                "  agent_thread, and ALWAYS delivery-attempted against the resolved",
                "  recipient's live binding -- wake is a transport property, not",
                "  something the sender opts into. Claude Code wakes through its",
                "  registered native adapter; locally patched Codex wakes through",
                "  notifications/homunculus/peer_message. A leading 'IMPORTANT:' in",
                "  the prose is stripped as input hygiene only; it no longer gates",
                "  delivery.",
                "",
                "Fails (HTTP 400) if the peer is not currently registered, the",
                "peer_id is ambiguous without an instance hint (peer_ambiguous),",
                "or the supplied peer_agent_instance_id has no matching binding",
                "(peer_unreachable).",
            ],
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "peer_id": {
                    "type": "string",
                    "description": "Target agent_id (must be currently registered).",
                },
                "peer_agent_instance_id": {
                    "type": "string",
                    "description": (
                        "Specific instance to address when multiple instances "
                        "of peer_id are registered. Omit when only one "
                        "instance exists."
                    ),
                },
                "peer_agent_session_id": {
                    "type": "string",
                    "description": (
                        "Stable logical-session fallback from a reply hint. "
                        "Used only after peer_agent_instance_id is unreachable; "
                        "never overrides a live instance."
                    ),
                },
                "content": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "type": {"type": "string", "enum": ["text"]},
                            "text": {"type": "string"},
                        },
                        "required": ["type", "text"],
                        "additionalProperties": False,
                    },
                    "minItems": 1,
                },
            },
            "required": ["peer_id", "content"],
            "additionalProperties": False,
        },
    ),
    Tool(
        name="peer_send_by_name",
        description="\n".join(
            [
                "Send a peer message to the current holder of a durable solet role.",
                "",
                "Use this for ChatGPT/operator fleet management. The role binding is",
                "resolved at send time, so reconnects and bridge churn do not require",
                "ChatGPT to choose an agent_instance_id from peer_list.",
                "",
                "Examples of role names are Coordinator, Coordinator-Dusk, Architect,",
                "Git-Controller, and Codex-Reviewer, depending on which roles are",
                "currently claimed in the solet.",
                "",
                "Delivery contract:",
                "  Every send is persisted to the role's durable inbox AND",
                "  delivery-attempted against the current holder's live binding --",
                "  wake is a transport property, not something the sender opts into.",
                "  A leading 'IMPORTANT:' in the content is stripped as input",
                "  hygiene only; it no longer gates delivery.",
                "",
                "This is the preferred task-assignment tool. Use peer_send only for",
                "direct replies or an operator-requested exact live instance.",
            ],
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": (
                        "Durable role name registered in the solet role binding "
                        "table, such as Coordinator-Dusk or Architect."
                    ),
                },
                "content": {
                    "type": "string",
                    "description": (
                        "Message text. Delivery is a transport property, not a "
                        "sender-declared one: every send is delivery-attempted "
                        "against the resolved recipient's live binding."
                    ),
                },
            },
            "required": ["name", "content"],
            "additionalProperties": False,
        },
    ),
    Tool(
        name="peer_inbox",
        description="\n".join(
            [
                "Pull peer messages addressed to your agent_id.",
                "",
                "Returns the durable catch-up view of every message addressed to",
                "you -- delivery is a transport property now, not a sender-declared",
                "one, so there is no silent-vs-notified split to filter on here.",
                "Use an `after` timestamp when polling during an active incident so",
                "old history does not flood the context window.",
                "",
                "Spans every peer thread targeting you, regardless of which bridge owns",
                "the thread. The instance section is oldest-first; page by echoing the",
                "previous page's next_after_created_at and stop only when",
                "instance_exhausted is true. Omit after only for the first page, not a",
                "full inbox. This signal applies to the existing timestamp-only cursor",
                "and does not claim duplicate-timestamp rows are globally lossless.",
                "",
                "Reading the inbox does NOT obligate you to reply.",
            ],
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "after": {
                    "type": "string",
                    "description": "ISO-8601 forward cursor; omit only for the first page.",
                },
                "limit": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 100,
                    "default": 50,
                },
                "role_after": {
                    "type": "string",
                    "description": (
                        "Opaque cursor for the role_entries section (messages "
                        "addressed to a role you hold). Echo back the previous "
                        "page's next_role_cursor verbatim; omit for the first "
                        "page. Distinct from 'after' (the instance section)."
                    ),
                },
                "observer": {
                    "type": "boolean",
                    "description": (
                        "Read-only observation: preserve pending rows and do not issue "
                        "display acknowledgements."
                    ),
                },
            },
            "additionalProperties": False,
        },
    ),
]


def _log(msg: str) -> None:
    """Write to stderr; stdout is reserved for MCP JSON-RPC framing."""
    print(f"[solet-bridge] {msg}", file=sys.stderr, flush=True)


def _generate_agent_instance_id() -> str:
    """Generate a durable per-subprocess routing id (`agi-<hex>`)."""
    return f"agi-{secrets.token_hex(16)}"


def _compute_session_label(agent_id: str) -> str:
    """Use $AGENT_SESSION_LABEL when present, else infer from cwd."""
    explicit = os.environ.get(AGENT_SESSION_LABEL_ENV)
    if explicit:
        return explicit
    cwd_base = Path.cwd().name
    if cwd_base:
        return f"{agent_id} on {cwd_base}"
    return ""


def _compute_session_role(session_label: str) -> str:
    """Resolve the standing role this bridge must claim after registration.

    An explicit role may differ from the human-readable session label. When a
    launcher supplies only ``AGENT_SESSION_LABEL``, that explicit label is also
    the role by convention. Managed launchers set
    ``AGENT_ROLE_AUTOBIND=0`` to preserve the label as display metadata while
    leaving authority unbound until a model-initiated claim. Cwd-inferred labels
    never claim roles.
    """
    autobind = os.environ.get(AGENT_ROLE_AUTOBIND_ENV, "1").strip()
    if autobind not in {"0", "1"}:
        msg = f"{AGENT_ROLE_AUTOBIND_ENV} must be '0' or '1' when set"
        raise RuntimeError(msg)
    explicit_role = os.environ.get(AGENT_ROLE_ENV)
    if autobind == "0":
        if explicit_role is not None:
            msg = (
                f"{AGENT_ROLE_AUTOBIND_ENV}=0 conflicts with explicit "
                f"{AGENT_ROLE_ENV}; omit one"
            )
            raise RuntimeError(msg)
        return ""
    if explicit_role is not None:
        role = explicit_role.strip()
        if not role:
            msg = f"{AGENT_ROLE_ENV} must be non-empty when set"
            raise RuntimeError(msg)
        return role
    if os.environ.get(AGENT_SESSION_LABEL_ENV):
        return session_label
    return ""


def _resolve_agent_session_id(agent_id: str) -> str:
    """Resolve the stable logical-session key from this bridge's per-agent-kind
    carrier chain.

    Keyed on `agent_id` (`SESSION_ID_ENV_VARS_BY_AGENT`, else
    `DEFAULT_SESSION_ID_ENV_VARS`) so cross-agent id adoption cannot happen:
    `refresh_role_binding_cas` re-points roles filtered on `agent_session_id`
    ALONE, so a bridge that adopted a foreign kind's inherited/leaked session id
    would re-point the WRONG session's roles. A Codex bridge prefers its own
    `CODEX_THREAD_ID` (authoritative — this Codex conversation, never stale for a
    Codex child), then `AGENT_SESSION_ID`; every other kind uses
    `AGENT_SESSION_ID` only and NEVER adopts `CODEX_THREAD_ID`. Returns the
    first non-empty carrier, else `""` — the degraded, self-refresh-disabled
    binding (server logs the S1.5 warning). The resolved value flows unchanged
    into `Forwarder` and thus every register POST, so any accepted carrier
    enables reconnect self-refresh.
    """
    carriers = SESSION_ID_ENV_VARS_BY_AGENT.get(agent_id, DEFAULT_SESSION_ID_ENV_VARS)
    for env_var in carriers:
        value = os.environ.get(env_var, "")
        if value:
            return value
    return ""


def _enforce_session_id_for_managed_registration(
    *, agent_instance_id_injected: bool, agent_session_id: str,
) -> None:
    """Fail loud rather than register a managed spawn with an empty session id.

    A managed spawner (``macos_coding_agent_session_plugin``'s
    ``bridge_tracker``, v10 Control #2.D) injects a caller-chosen
    ``AGENT_INSTANCE_ID`` so the registry replaces this subprocess's binding
    in place across reconnects — but that injection path forwards no
    ``AGENT_SESSION_ID`` alongside it (``default_spawn`` copies ITS OWN
    process env, never the calling session's), so ``_resolve_agent_session_id``
    deterministically reads the long-running solet server's own environment —
    never the managed session's — and resolves ``""``. Registering that
    silently persists a ``peer_binding`` row that ``peer_holds_role`` /
    ``peer_claim_role`` can never confirm for this instance id (R2 — measured
    live, 2026-08-18: a spawned worker's LEDGER-id row carried an empty
    ``agent_session_id`` while its WATCH-id row, registered through the
    separate CLI path, was correctly populated).

    The degrade-to-empty stays intentional and untouched for a genuinely
    interactive/un-managed bridge (``agent_instance_id_injected`` is ``False``
    there, e.g. the operator ``.mcp.json`` path with a self-minted instance
    id) — this check narrows to the managed case only, mirroring
    :func:`agent_messaging_plugin.env_contract.enforce_no_legacy_agent_env`'s
    fail-loud convention.
    """
    if agent_instance_id_injected and not agent_session_id:
        msg = (
            "managed MCP bridge spawn: AGENT_INSTANCE_ID was injected by a "
            "managed spawner but no AGENT_SESSION_ID carrier resolved — "
            "registering would silently write a peer_binding row that "
            "peer_holds_role/peer_claim_role can never confirm for this "
            "instance id (R2). Fix the spawner to also inject "
            "AGENT_SESSION_ID alongside AGENT_INSTANCE_ID."
        )
        raise RuntimeError(msg)


async def _discover_port(solet_name: str) -> int:
    """Poll the runtime port file until the solet has written it."""
    attempt = 0
    while True:
        attempt += 1
        port = read_port_file(BRIDGE_SERVICE_NAME, solet_name)
        if port is not None:
            _log(
                f"discovered bridge port {port} for solet "
                f"{solet_name!r} after {attempt} attempt(s)",
            )
            return port
        if attempt == 1 or attempt % 10 == 0:
            _log(
                f"waiting for {solet_name}.{BRIDGE_SERVICE_NAME}.port "
                f"(attempt {attempt})",
            )
        await asyncio.sleep(PORT_DISCOVERY_RETRY_S)


def _build_server(forwarder: Forwarder) -> Server[Any, Any]:
    """Construct the lowlevel Server with our 15 tools wired to forwarder."""
    server: Server[Any, Any] = Server(SERVER_NAME, instructions=SERVER_INSTRUCTIONS)

    @server.list_tools()  # type: ignore[misc, no-untyped-call]
    async def list_tools() -> list[Tool]:
        return list(TOOLS)

    @server.call_tool()  # type: ignore[misc]
    async def call_tool(name: str, arguments: dict[str, Any]) -> list[TextContent]:
        result = await _dispatch_tool(forwarder, name, arguments or {})
        return [TextContent(type="text", text=_json_dumps(result))]

    return server


def _json_dumps(payload: Any) -> str:
    """Stable JSON encoding for tool responses; matches the Node bridge shape."""
    return json.dumps(payload, ensure_ascii=False)


async def _tool_current_identity(fw: Forwarder, _a: dict[str, Any]) -> dict[str, Any]:
    return await fw.current_identity()


async def _tool_download(fw: Forwarder, a: dict[str, Any]) -> dict[str, Any]:
    return await fw.download(
        blob_id=str(a["blob_id"]),
        output_path=str(a["output_path"]),
    )


async def _tool_process_search(fw: Forwarder, a: dict[str, Any]) -> dict[str, Any]:
    return await fw.process_search(
        query=str(a["query"]),
        max_results=int(a.get("max_results", 10)),
    )


async def _tool_process_schema(fw: Forwarder, a: dict[str, Any]) -> dict[str, Any]:
    return await fw.process_schema(process_key=str(a["process_key"]))


async def _tool_process_call(fw: Forwarder, a: dict[str, Any]) -> dict[str, Any]:
    return await fw.process_call(
        process_key=str(a["process_key"]),
        arguments=dict(a.get("arguments") or {}),
        reason=a.get("reason"),
    )


async def _tool_process_result(fw: Forwarder, a: dict[str, Any]) -> dict[str, Any]:
    return await fw.process_result(action_id=str(a["action_id"]))


async def _tool_peer_register(fw: Forwarder, a: dict[str, Any]) -> dict[str, Any]:
    return await fw.peer_register(
        agent_id=str(a["agent_id"]),
        session_label=a.get("session_label"),
    )


async def _tool_peer_list(fw: Forwarder, _a: dict[str, Any]) -> dict[str, Any]:
    return await fw.peer_list()


async def _tool_peer_send(fw: Forwarder, a: dict[str, Any]) -> dict[str, Any]:
    return await fw.peer_send(
        peer_id=str(a["peer_id"]),
        content=list(a.get("content") or []),
        peer_agent_instance_id=a.get("peer_agent_instance_id"),
        peer_agent_session_id=a.get("peer_agent_session_id"),
    )


async def _tool_peer_send_by_name(
    fw: Forwarder, a: dict[str, Any],
) -> dict[str, Any]:
    return await fw.peer_send_by_name(
        name=str(a["name"]),
        content=str(a["content"]),
    )


async def _tool_peer_inbox(fw: Forwarder, a: dict[str, Any]) -> dict[str, Any]:
    # A4 (2026-08-04): include_important retired from the tool schema -- the
    # catch-up view is the only meaningful one now that delivery is
    # unconditional. Always True; never read from the caller.
    return await fw.peer_inbox(
        after=a.get("after"),
        limit=a.get("limit"),
        include_important=True,
        role_after=a.get("role_after"),
        observer=bool(a.get("observer", False)),
    )


# Single-pass tool dispatch; new tools register here and stay out of any
# god-function.
_TOOL_DISPATCH: Final[
    dict[str, Callable[[Forwarder, dict[str, Any]], Awaitable[dict[str, Any]]]]
] = {
    "current_identity": _tool_current_identity,
    "download": _tool_download,
    "process_search": _tool_process_search,
    "process_schema": _tool_process_schema,
    "process_call": _tool_process_call,
    "process_result": _tool_process_result,
    "peer_register": _tool_peer_register,
    "peer_list": _tool_peer_list,
    "peer_send": _tool_peer_send,
    "peer_send_by_name": _tool_peer_send_by_name,
    "peer_inbox": _tool_peer_inbox,
}


async def _dispatch_tool(
    forwarder: Forwarder,
    name: str,
    args: dict[str, Any],
) -> dict[str, Any]:
    """Route an MCP tool call to the matching Forwarder method."""
    handler = _TOOL_DISPATCH.get(name)
    if handler is None:
        msg = f"Unknown tool: {name}"
        raise ValueError(msg)
    return await handler(forwarder, args)


async def _run() -> None:
    """Bridge entry point: discover, connect, register, serve."""
    enforce_no_legacy_agent_env()
    solet_name = os.environ.get("SOLET_NAME")
    if not solet_name:
        msg = "SOLET_NAME env var is required to discover the bridge port"
        raise RuntimeError(msg)
    agent_id = os.environ.get(AGENT_IDENTITY_ENV) or DEFAULT_AGENT_ID
    # v10 Control #2.D: honor an injected AGENT_INSTANCE_ID so a managed
    # spawner (macos bridge_tracker sets it per session) keeps a STABLE
    # agent_instance_id across bridge reconnects — the registry replaces in
    # place instead of accreting a fresh id every reconnect. Absent (operator
    # .mcp.json path) → mint a durable id as before. Recorded here (rather
    # than re-derived) because it is also the discriminator the R2 guard below
    # uses to tell a managed spawn from a genuinely interactive one.
    agent_instance_id_injected = bool(os.environ.get(AGENT_INSTANCE_ID_ENV))
    agent_instance_id = (
        os.environ.get(AGENT_INSTANCE_ID_ENV)
        or _generate_agent_instance_id()
    )
    # v10 Control #2.D (read-defensively): the stable logical-session key the
    # role-binding CAS self-refresh keys on to re-point a rotated
    # agent_instance_id without an explicit re-claim. Resolved through this
    # bridge's PER-AGENT-KIND carrier chain (keyed on agent_id): a codex bridge
    # prefers its own CODEX_THREAD_ID then AGENT_SESSION_ID; every other kind
    # uses AGENT_SESSION_ID only and never adopts CODEX_THREAD_ID (the CAS
    # filters on agent_session_id alone, so cross-agent adoption would re-point
    # the wrong session's roles). Any carrier MUST be a per-logical-session id
    # (a launcher-minted UUID, or Codex's own thread id) — NEVER derived from
    # parent_pid (shared across app-hosted siblings). Absent all, for a
    # genuinely interactive/un-managed bridge → "" → the CAS fails closed to
    # explicit re-claim (server logs S1.5); for a MANAGED spawn (injected
    # instance id) the same emptiness is no longer tolerated — see the R2
    # guard immediately below, which fails loud instead (2026-08-18: this
    # combination measurably persisted a peer_binding row that
    # peer_holds_role/peer_claim_role can never confirm).
    agent_session_id = _resolve_agent_session_id(agent_id)
    _enforce_session_id_for_managed_registration(
        agent_instance_id_injected=agent_instance_id_injected,
        agent_session_id=agent_session_id,
    )
    session_label = _compute_session_label(agent_id)
    session_role = _compute_session_role(session_label)
    parent_pid = os.getppid()

    port = await _discover_port(solet_name)
    base_url = f"http://127.0.0.1:{port}"
    _log(
        f"starting MCP bridge: agent_id={agent_id} "
        f"agent_instance_id={agent_instance_id} parent_pid={parent_pid}",
    )

    # INF-01 §D.9: a coding-agent session serves the sys:autonomic lane
    # (vertex forwards + completion requests) only if its bridge surfaces
    # non-native event types into the host conversation. That holds for
    # Claude Code (claude-channel notifications render in-session); the
    # patched Codex CLI consumes ONLY notifications/homunculus/peer_message
    # (forwarder._notification_method_for), so a codex holder would be
    # deaf — organism turns would park until the serve-timeout sweep.
    provides_inference = agent_id == DEFAULT_AGENT_ID
    # codex-watch-migration wake_capable design (2026-08-06): declared here,
    # never probed — true only for the agent kind with a registered native
    # wake adapter (Claude Code). Codex's bridge has no turn-injection
    # surface once the patched build retires, so it declares false. A Codex
    # delivery still reaches the recipient through the watch/durable-inbox
    # path (durable + observable regardless of wake_capable): an unmanaged
    # Codex session drains it on its own next user turn (no live Stop-hook
    # notice exists — codex-0147-async-hook-regression, 2026-08-13, removed
    # the async Stop binding because stock Codex does not execute async
    # command hooks), while a spawn_session-managed worker gets an
    # independent driver-channel nudge from drive_on_delivery, unaffected by
    # wake_capable or the Stop-hook removal. See models.py's
    # BridgeBinding.wake_capable for the full rationale.
    wake_capable = agent_id == DEFAULT_AGENT_ID
    forwarder = Forwarder(
        base_url=base_url,
        solet_name=solet_name,
        agent_id=agent_id,
        agent_instance_id=agent_instance_id,
        agent_session_id=agent_session_id,
        session_label=session_label,
        parent_pid=parent_pid,
        provides_inference=provides_inference,
        wake_capable=wake_capable,
        session_role=session_role,
    )
    server = _build_server(forwarder)

    async with stdio_server() as (read_stream, write_stream):
        # Capture the write stream so the background poll loop can emit
        # notifications/claude/channel outside any request context.
        forwarder.bind_write_stream(write_stream)
        # Open the bridge against the solet concurrently with serving stdio --
        # MCP clients (Codex in particular) enforce a startup timeout
        # on the initialize handshake, so we cannot block on the solet being
        # reachable before returning to the client.
        opener = asyncio.create_task(_open_bridge_safely(forwarder))
        try:
            await server.run(
                read_stream,
                write_stream,
                InitializationOptions(
                    server_name=SERVER_NAME,
                    server_version=SERVER_VERSION,
                    capabilities=server.get_capabilities(
                        notification_options=NotificationOptions(),
                        experimental_capabilities={"claude/channel": {}},
                    ),
                    instructions=SERVER_INSTRUCTIONS,
                ),
            )
        finally:
            opener.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await opener
            await forwarder.close()


async def _open_bridge_safely(forwarder: Forwarder) -> None:
    """Fire-and-forget bridge open; logs failures rather than propagating."""
    try:
        await forwarder.open_bridge()
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001
        _log(f"background bridge open failed: {exc}")


def main() -> None:
    """Synchronous entry point for `python -m agent_messaging_plugin.mcp_bridge`."""
    try:
        asyncio.run(_run())
    except KeyboardInterrupt:
        _log("interrupted; shutting down")
    except Exception as exc:  # noqa: BLE001
        _log(f"FATAL: {exc}")
        sys.exit(1)


if __name__ == "__main__":
    main()
