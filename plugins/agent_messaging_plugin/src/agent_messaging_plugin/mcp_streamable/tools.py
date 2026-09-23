"""Tool descriptors for the Streamable HTTP MCP transport.

Mirrors the MCP tool surface exposed by the stdio bridge subprocess
(:mod:`agent_messaging_plugin.mcp_bridge.__main__`).  Kept here as
plain dicts — the Streamable HTTP transport speaks raw JSON-RPC, not
the Python MCP SDK ``Tool`` type, so a separate descriptor table is
the most direct path.

The Streamable HTTP wording intentionally differs where hosted clients
cannot consume the stdio bridge's ``notifications/claude/channel``
convention. In particular, process calls should be followed with
``process_result`` polling instead of relying on bridge delivery events.
"""

from __future__ import annotations

from typing import Any, Final

# ---------------------------------------------------------------------
# Tool descriptors.  Each entry is the ``tools/list`` row the server
# returns verbatim; ``inputSchema`` is the JSON Schema MCP clients use
# for argument validation.
# ---------------------------------------------------------------------

_PROCESS_CALL_DESCRIPTION: Final[str] = (
    "Direct invocation of a solet process by process_key.  Zero "
    "inference, deterministic, fast.  THE PREFERRED entry point for any "
    "known process — knowledge base searches "
    "(service_interface::knowledge_service::search), memory recall "
    "(service_interface::memory_service::recall), plugin tools "
    "(plugin::<plugin>::<function>), everything.  Returns action_id + "
    "flow_id immediately; then call `process_result` with that action_id "
    "until the result appears or the action reaches an error status.  Use "
    "process_search first if you don't know the process_key and "
    "process_schema to confirm the argument shape."
)

_PEER_SEND_DESCRIPTION: Final[str] = "\n".join(
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
        "  peer_id is the stable agent kind (e.g., \"claude_code\", \"codex\").",
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
)

_PEER_SEND_BY_NAME_DESCRIPTION: Final[str] = "\n".join(
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
)

_PEER_INBOX_DESCRIPTION: Final[str] = "\n".join(
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
)

_PEER_LIST_DESCRIPTION: Final[str] = "\n".join(
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
    ],
)

_PEER_REGISTER_DESCRIPTION: Final[str] = "\n".join(
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
)

TOOLS: Final[list[dict[str, Any]]] = [
    {
        "name": "current_identity",
        "description": (
            "Return identity and routing metadata for the current MCP session, "
            "including transport, solet_name, agent_id, "
            "agent_instance_id, agent_session_id, session_label, bridge_id, "
            "mcp_session_id, roles_held, and identity_trust. Use this to "
            "answer 'who am I?' or verify routing before peer_register, "
            "peer_claim_role, peer_send, or Streamable HTTP peer receive work. "
            "Returns no secrets."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        },
    },
    {
        "name": "download",
        "description": "Download a blob from solet storage to a local file path.",
        "inputSchema": {
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
    },
    {
        "name": "process_search",
        "description": (
            "Search the solet process registry for processes matching a "
            "natural-language query."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Natural-language search query."},
                "max_results": {"type": "integer", "default": 10},
            },
            "required": ["query"],
            "additionalProperties": False,
        },
    },
    {
        "name": "process_schema",
        "description": (
            "Retrieve the invocation schema for a single solet process by "
            "its process_key."
        ),
        "inputSchema": {
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
    },
    {
        "name": "process_call",
        "description": _PROCESS_CALL_DESCRIPTION,
        "inputSchema": {
            "type": "object",
            "properties": {
                "process_key": {"type": "string"},
                "arguments": {"type": "object"},
                "reason": {"type": "string"},
            },
            "required": ["process_key", "arguments"],
            "additionalProperties": False,
        },
    },
    {
        "name": "process_result",
        "description": (
            "Snapshot read of an action_id's current observable state "
            "(status, error_message, latest stored raw result row if "
            "any). For hosted Streamable HTTP clients such as ChatGPT, "
            "this is the follow-up read after process_call returns "
            "action_id + flow_id."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "action_id": {"type": "string"},
            },
            "required": ["action_id"],
            "additionalProperties": False,
        },
    },
    {
        "name": "peer_register",
        "description": _PEER_REGISTER_DESCRIPTION,
        "inputSchema": {
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
    },
    {
        "name": "peer_list",
        "description": _PEER_LIST_DESCRIPTION,
        "inputSchema": {
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        },
    },
    {
        "name": "peer_send",
        "description": _PEER_SEND_DESCRIPTION,
        "inputSchema": {
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
    },
    {
        "name": "peer_send_by_name",
        "description": _PEER_SEND_BY_NAME_DESCRIPTION,
        "inputSchema": {
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
    },
    {
        "name": "peer_inbox",
        "description": _PEER_INBOX_DESCRIPTION,
        "inputSchema": {
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
    },
]


SERVER_VERSION: Final[str] = "1.0.0"
SUPPORTED_PROTOCOL_VERSION: Final[str] = "2025-03-26"

# Fallback used when the solet name is empty / unset (laptop dev
# mode, unit tests).  Never reaches a production phone client — the
# plugin reads $SOLET_NAME at start_interface time and threads it
# through to every layer that emits server identity.
FALLBACK_SOLET_NAME: Final[str] = "solet"


def build_server_name(solet_name: str) -> str:
    """Render the ``serverInfo.name`` advertised in the initialize response.

    Format: ``"<solet>-streamable"`` so the MCP client surfaces
    the actual solet identity rather than a generic platform
    label.  Empty input falls back to ``FALLBACK_SOLET_NAME``.
    """
    return f"{solet_name or FALLBACK_SOLET_NAME}-streamable"


def build_server_instructions(solet_name: str) -> str:
    """Render the ``instructions`` block returned by ``initialize``.

    Embeds the solet name into the lead sentence so a Claude
    Desktop / claude.ai client doesn't refer to a remote solet
    by a generic label.
    """
    name = solet_name or FALLBACK_SOLET_NAME
    return "\n".join(
        [
            f"{name} platform bridge (Streamable HTTP transport).",
            "",
            "Same process and messaging tools as the stdio bridge.  Use "
            "`process_call` for any known process_key (zero inference), then "
            "`process_result` with the returned action_id to read completion.  "
            "Peer messages between you and other live MCP-connected agents flow "
            "through `peer_send`; delivery is unconditional -- wake is a "
            "transport property, not something the sender opts into.",
            "",
            "When acting as an operator control plane, assign work through "
            "`peer_send_by_name` to a durable role such as Coordinator, "
            "Coordinator-Dusk, Architect, or Git-Controller.  Use raw "
            "`peer_send` only for direct replies to a specific sender instance "
            "or when the operator explicitly names a live session.",
            "",
            "Server-pushed peer notifications may arrive on the SSE channel "
            'opened by GET as JSON-RPC notifications named '
            '"notifications/claude/channel". Hosted clients should not rely on '
            "bridge_delivery_result for process_call completion; use "
            "`process_result` instead.",
        ],
    )


__all__ = [
    "FALLBACK_SOLET_NAME",
    "SERVER_VERSION",
    "SUPPORTED_PROTOCOL_VERSION",
    "TOOLS",
    "build_server_instructions",
    "build_server_name",
]
