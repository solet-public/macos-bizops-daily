"""Claude Code session log parser (spec §17.3, M3).

One Claude Code ``.jsonl`` line is one structural event that may expand
into ZERO OR MORE :class:`RawSessionEvent`s. The parser handles the
flattening; the source plugin's ``normalize`` then reads each raw
event's ``payload`` dict to produce the canonical
:class:`NormalizedSessionEvent`.

Line shape (each JSONL line is one JSON object):

* ``type=user`` — user message. ``message.content`` is either a string or
  a list of content blocks (``text``, ``tool_result``).
* ``type=assistant`` — assistant message. ``message.content`` is a list of
  content blocks (``text``, ``tool_use``, ``thinking``).
* ``type=system`` — system event (rare; surfaces as SYSTEM in the ledger).
* ``type=permission-mode`` / ``file-history-snapshot`` / ``last-prompt`` /
  ``attachment`` / ``summary`` / ``custom-title`` / ``agent-name`` /
  ``bridge-session`` / ``ai-title`` — session-config noise, NOT
  conversation. Skipped. None of these carry a ``timestamp`` field
  because they are not events.

``vendor_event_id`` discipline (load-bearing for the importer's tool-call
projection — see ``importer._maybe_project_tool_call``):

* MESSAGE: ``vendor_event_id = line.uuid``, ``vendor_parent_event_id =
  line.parentUuid``.
* TOOL_CALL: ``vendor_event_id = tool_use.id`` (the ``toolu_...`` value
  from the content block), ``vendor_parent_event_id = line.uuid``. The
  importer matches TOOL_RESULT to TOOL_CALL on ``tool_use.id``, so this
  field MUST equal the tool_use id and nothing else.
* TOOL_RESULT: ``vendor_event_id = line.uuid``, ``vendor_parent_event_id
  = content.tool_use_id``. The parent pointer is the resolution key.
* SYSTEM: ``vendor_event_id = line.uuid``, ``vendor_parent_event_id =
  line.parentUuid``.

``RawSessionEvent.payload`` shape (consumed by ``normalize``):

::

    {
      "kind": "message" | "tool_call" | "tool_result" | "system",
      "role": "user" | "assistant" | "system",
      "text": <str|None>,           # MESSAGE / SYSTEM / TOOL_RESULT body
      "tool_name": <str|None>,      # TOOL_CALL only
      "tool_input": <dict|None>,    # TOOL_CALL only — opaque vendor input
      "tool_use_id": <str|None>,    # TOOL_CALL: the toolu_... id;
                                    # TOOL_RESULT: the linked toolu_... id
      "project_path": <str|None>,   # the line's cwd field
      "git_branch": <str|None>,     # the line's gitBranch field
    }

The payload's ``cwd`` is preserved so the source plugin can populate
``ExternalSessionRef.project_path`` from the first-seen event when the
session is being discovered.

Failure policy: per project critical guidelines, no defensive fallback.
Malformed JSON / unexpected shape / unrecognized content-block kind →
``ValueError``. The importer marks the batch FAILED with
``error_kind='value_error'`` (spec §17.2 acceptance pattern). Skips here
are limited to documented session-config noise types and to
``thinking`` content blocks (internal model state, not part of the
public conversation).
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ananta.llm.session_ledger.types import RawSessionEvent

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Session-metadata promotion
# ---------------------------------------------------------------------------

# Top-level line types whose payload carries session-level metadata that the
# ledger session row promotes BEFORE event parsing (per 2026-05-31 Architect
# ruling §3). Each line is still SKIPPED from the conversational event stream
# by the existing ``_SKIP_LINE_TYPES`` set; the values are extracted in a
# separate pass via :func:`read_session_metadata`. ``ai-title`` is
# deliberately absent — Architect §3 ruling: operator-set ``custom-title``
# is authoritative; the AI-generated title gets dropped.
_SESSION_METADATA_TYPES: frozenset[str] = frozenset(
    {"agent-name", "custom-title", "bridge-session"},
)


@dataclass(frozen=True, slots=True)
class ClaudeCodeSessionMetadata:
    """Promoted session-level metadata extracted from a Claude Code rollout.

    Per 2026-05-31 Architect ruling §3:

    * ``agent_name`` → ``session_ledger__session.vendor_session_label`` AND
      ``originator_session_label``.
    * ``custom_title`` → ``session_ledger__session.summary_text`` (operator-set,
      authoritative over ``ai-title`` which is dropped).
    * ``bridge_session_id`` → ``originator_agent_instance_id`` (this IS the
      durable per-bridge UUID, not a label).

    A "last-write-wins" precedence applies when multiple lines of the same
    type appear in the file (operator may have ``/rename``-cycled within
    the session; the most recent value reflects the final state).
    """

    agent_name: str | None = None
    custom_title: str | None = None
    bridge_session_id: str | None = None


# Per-metadata-type field name on the JSONL payload.
_METADATA_PAYLOAD_FIELD: dict[str, str] = {
    "agent-name": "agentName",
    "custom-title": "customTitle",
    "bridge-session": "bridgeSessionId",
}


def _parse_metadata_line(stripped: str) -> tuple[str, str] | None:
    """Return ``(line_type, field_value)`` for a metadata line, else None."""
    try:
        data = json.loads(stripped)
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict):
        return None
    line_type = data.get("type")
    if not isinstance(line_type, str) or line_type not in _SESSION_METADATA_TYPES:
        return None
    field_name = _METADATA_PAYLOAD_FIELD[line_type]
    field_value = data.get(field_name)
    if not isinstance(field_value, str) or not field_value:
        return None
    return line_type, field_value


def read_session_metadata(path: Path) -> ClaudeCodeSessionMetadata:
    """Extract session-level metadata via a separate file pass.

    Per 2026-05-31 Architect ruling §3 implementation pattern (Option (a),
    locked by Coordinator): a dedicated read-and-extract pass — clean
    separation from ``iter_events_from_path``. The extra IO is a
    filesystem-local read; the type/logic clarity wins over a union-return
    iterator. Skipping the file when missing returns the default empty
    metadata so downstream code stays uniform.

    Last-write-wins per field — operators who ``/rename`` mid-session leave
    multiple ``agent-name`` lines, and the FINAL value is what reflects the
    session's current role.
    """
    if not path.is_file():
        return ClaudeCodeSessionMetadata()
    fields: dict[str, str] = {}
    with path.open("r", encoding="utf-8") as fh:
        for raw_line in fh:
            stripped = raw_line.strip()
            if not stripped:
                continue
            parsed = _parse_metadata_line(stripped)
            if parsed is None:
                continue
            line_type, value = parsed
            fields[line_type] = value
    return ClaudeCodeSessionMetadata(
        agent_name=fields.get("agent-name"),
        custom_title=fields.get("custom-title"),
        bridge_session_id=fields.get("bridge-session"),
    )


@dataclass(frozen=True, slots=True)
class _LineContext:
    """Per-line attributes shared across every event emitted from one JSONL line."""

    session_id: str
    event_at: datetime
    line_uuid: str | None
    parent_uuid: str | None
    project_path: str | None
    git_branch: str | None

    @classmethod
    def from_data(cls, data: dict[str, Any]) -> _LineContext:
        return cls(
            session_id=_require_session_id(data),
            event_at=_parse_timestamp(data),
            line_uuid=_optional_str(data.get("uuid")),
            parent_uuid=_optional_str(data.get("parentUuid")),
            project_path=_optional_str(data.get("cwd")),
            git_branch=_optional_str(data.get("gitBranch")),
        )

# Top-level JSONL line types that are session-config noise, not conversation.
# These are silently skipped (NOT defensive — these types carry no
# ledger-bearing content by design; see docstring).
#
# ``custom-title`` / ``agent-name`` / ``bridge-session`` / ``ai-title`` were
# added in the 2026-05-31 dispatch ``claude_code_timestamp_vendor_fix`` —
# they are session-metadata markers Claude Code writes alongside the
# conversational stream (operator-set custom title, agent role, bridge
# session id, model-generated session title). None carry a ``timestamp``
# field because they are NOT events; without skip-list membership the line
# fell through to :func:`_LineContext.from_data` which raises
# ``ValueError`` on the missing timestamp, and the per-session try/except
# in :class:`SessionLedgerImporter` silently orphaned the entire session.
# This is the same vendor-drift class as the codex
# ``compacted`` / ``web_search_call`` fix earlier on 2026-05-31.
_SKIP_LINE_TYPES = frozenset(
    {
        "permission-mode",
        "file-history-snapshot",
        "last-prompt",
        "attachment",
        "summary",
        "custom-title",
        "agent-name",
        "bridge-session",
        "ai-title",
        # ``mode`` added 2026-06-12 sub-2.5: Claude Code emits a session-mode
        # metadata line (key: ``mode``) alongside ``permission-mode``. No
        # timestamp; matches the documented metadata-marker pattern.
        "mode",
        # ``queue-operation`` added 2026-06-12 Task #14: Claude Code emits
        # queue-operation marker lines (912 occurrences across today's
        # session corpus). HAS a ``timestamp`` field but no event-shape
        # ``message`` dict; documented here for explicit allowlist coverage.
        # The structural tolerance branch below also catches it as
        # forward-compat if a future variant drops the timestamp.
        "queue-operation",
    }
)

# Content-block kinds that are intentionally skipped at the block level.
# ``thinking`` is internal model state (extended-thinking signature) and
# never makes it to the public ledger. ``image`` is out of v1 scope; will
# eventually become an ATTACHMENT event.
_SKIP_BLOCK_KINDS = frozenset({"thinking", "image"})

# Conversational top-level types we handle.
_LINE_TYPE_USER = "user"
_LINE_TYPE_ASSISTANT = "assistant"
_LINE_TYPE_SYSTEM = "system"

# Payload "kind" discriminators read by the source plugin's normalize().
PAYLOAD_KIND_MESSAGE = "message"
PAYLOAD_KIND_TOOL_CALL = "tool_call"
PAYLOAD_KIND_TOOL_RESULT = "tool_result"
PAYLOAD_KIND_SYSTEM = "system"


def parse_line(line_text: str) -> list[RawSessionEvent]:
    """Parse one ``.jsonl`` line into zero or more raw events.

    A trailing newline or surrounding whitespace is tolerated; a blank line
    yields nothing. Anything else that fails to parse as a JSON object
    raises ``ValueError``.
    """
    stripped = line_text.strip()
    if not stripped:
        return []
    try:
        data = json.loads(stripped)
    except json.JSONDecodeError as exc:
        raise ValueError(f"claude_code: line is not valid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError(
            f"claude_code: line is not a JSON object: {type(data).__name__}"
        )
    return list(parse_line_data(data))


def parse_line_data(data: dict[str, Any]) -> Iterator[RawSessionEvent]:
    """Parse one already-decoded JSONL object into raw events.

    Yields zero events for skip-listed line types; one event for plain
    text messages and system lines; N events for assistant lines with
    multiple tool_use blocks; N events for user lines carrying multiple
    tool_result blocks.
    """
    line_type = data.get("type")
    if not isinstance(line_type, str):
        raise ValueError("claude_code: line missing string 'type'")
    if line_type in _SKIP_LINE_TYPES:
        return
    # Vendor-drift tolerance (2026-06-12 sub-2.5 + Task #14): Claude Code
    # occasionally adds new line types (timestamp-absent metadata like
    # ``mode``, ``custom-title``; OR timestamp-present markers like
    # ``queue-operation``). Both are tolerated as long as the line does
    # NOT carry the event-shape signal (a ``message`` dict with a ``role``
    # field). Skip-with-debug-log keeps ingest alive across vendor drift.
    #
    # The fast-fail safety net (per [[fast-fail-development-strategy]]):
    # an unknown line type that DOES carry the event-shape signal is a
    # genuine contract change worth catching loudly — raised explicitly
    # below so we surface real new event kinds rather than silently
    # swallowing them.
    known_event_types = {_LINE_TYPE_SYSTEM, _LINE_TYPE_USER, _LINE_TYPE_ASSISTANT}
    if line_type not in known_event_types:
        message = data.get("message")
        has_event_shape = (
            isinstance(message, dict) and isinstance(message.get("role"), str)
        )
        if has_event_shape:
            raise ValueError(
                f"claude_code: unrecognized event-shape line 'type' "
                f"{line_type!r} (has message dict with role); vendor contract "
                "change?"
            )
        logger.debug(
            "claude_code: tolerating new line type %r (timestamp_present=%s, "
            "no event-shape signal; not in _SKIP_LINE_TYPES; consider adding "
            "for explicit documentation)",
            line_type, "timestamp" in data,
        )
        return
    # Past this point line_type is guaranteed to be in known_event_types
    # ({SYSTEM, USER, ASSISTANT}) by the unknown-type branch above.
    ctx = _LineContext.from_data(data)
    if line_type == _LINE_TYPE_SYSTEM:
        yield _build_system_event(data, ctx)
        return
    yield from _parse_conversation_line(data, ctx)


def _parse_conversation_line(
    data: dict[str, Any],
    ctx: _LineContext,
) -> Iterator[RawSessionEvent]:
    """Yield events for a ``type=user`` or ``type=assistant`` line."""
    message = data.get("message")
    if not isinstance(message, dict):
        raise ValueError("claude_code: line missing 'message' dict")
    role = message.get("role")
    if not isinstance(role, str) or not role:
        raise ValueError("claude_code: message.role missing or empty")
    # T1 usage-capture lane (2026-08-05 ruling): only assistant lines carry
    # a 'usage' dict in practice (MEASURED shape, scratch probe 2026-08-05);
    # read generically here so a future vendor change that adds it
    # elsewhere is picked up for free rather than requiring a role check.
    usage = message.get("usage")
    usage_json = usage if isinstance(usage, dict) else None
    content = message.get("content")
    if isinstance(content, str):
        # Legacy single-string content (early Claude Code versions).
        yield _build_message_event(ctx, role=role, text=content, usage=usage_json)
        return
    if not isinstance(content, list):
        raise ValueError(
            "claude_code: message.content must be string or list of content blocks"
        )
    yield from _parse_message_blocks(content, ctx=ctx, role=role, usage=usage_json)


def _parse_message_blocks(
    content: list[object],
    *,
    ctx: _LineContext,
    role: str,
    usage: dict[str, Any] | None = None,
) -> Iterator[RawSessionEvent]:
    """Walk content blocks and emit events in source-line order.

    Accumulated text is emitted first, then tool_use / tool_result events
    in source order. All events share the line timestamp; the importer's
    sequence allocator preserves emit order, so persisted order mirrors
    the assistant's "intro text → tool call" temporal narrative.

    ``usage`` (T1 usage-capture lane) rides only the accumulated-text
    MESSAGE event this function emits, never the tool_use/tool_result
    events — usage is a per-LINE (per-API-turn) figure the line's message
    carries once, not a per-block figure; attributing it to a tool event
    would double-count or misattribute it.
    """
    text_parts: list[str] = []
    tool_events: list[RawSessionEvent] = []
    for block in content:
        event = _classify_block(block, ctx=ctx, role=role, text_parts=text_parts)
        if event is not None:
            tool_events.append(event)
    # ``or usage``: a tool-only assistant turn (no text blocks at all --
    # common for a pure tool-call turn) must still emit a carrier for the
    # line's usage figure; without this, usage would be silently dropped
    # for every tool-only turn, undercounting real spend (this wave's
    # whole purpose). An empty content_text is a valid MESSAGE event
    # (_validate_message_event only requires content_text OR content_json
    # non-None, and "" is not None).
    if text_parts or usage:
        yield _build_message_event(ctx, role=role, text="\n".join(text_parts), usage=usage)
    yield from tool_events


def _classify_block(
    block: object,
    *,
    ctx: _LineContext,
    role: str,
    text_parts: list[str],
) -> RawSessionEvent | None:
    """Classify one content block.

    Returns a tool event when the block is a tool_use / tool_result; returns
    ``None`` for text (consumed into ``text_parts``) and for skip-listed
    block kinds. Raises ``ValueError`` for malformed or unknown kinds.
    """
    if not isinstance(block, dict):
        raise ValueError(
            f"claude_code: content block is not a dict: {type(block).__name__}"
        )
    block_kind = block.get("type")
    if not isinstance(block_kind, str):
        raise ValueError("claude_code: content block missing string 'type'")
    if block_kind in _SKIP_BLOCK_KINDS:
        return None
    if block_kind == "text":
        text_value = block.get("text")
        if not isinstance(text_value, str):
            raise ValueError("claude_code: text block missing string 'text'")
        text_parts.append(text_value)
        return None
    # Bug H (2026-06-13): server-side built-in tools (WebSearch,
    # ``advisor``, etc.) carry block type ``server_tool_use`` and a paired
    # ``<tool_name>_tool_result`` (e.g. ``advisor_tool_result``,
    # ``web_search_tool_result``) instead of the original ``tool_use`` /
    # ``tool_result`` pair. The shape is symmetric — same ``id`` / ``name``
    # / ``input`` for the call; same ``tool_use_id`` / ``content`` for the
    # result — so they ride the existing event builders unchanged. Pre-
    # fix, the catch-all ``unrecognized content block type`` raise
    # bubbled up to the importer's per-session ValueError catch and
    # truncated the session's ingest at the first affected message.
    if block_kind in ("tool_use", "server_tool_use"):
        return _build_tool_call_event(
            block=block,
            session_id=ctx.session_id,
            role=role,
            event_at=ctx.event_at,
            line_uuid=ctx.line_uuid,
            project_path=ctx.project_path,
            git_branch=ctx.git_branch,
        )
    if block_kind == "tool_result" or block_kind.endswith("_tool_result"):
        return _build_tool_result_event(
            block=block,
            session_id=ctx.session_id,
            role=role,
            event_at=ctx.event_at,
            line_uuid=ctx.line_uuid,
            project_path=ctx.project_path,
            git_branch=ctx.git_branch,
        )
    raise ValueError(
        f"claude_code: unrecognized content block type {block_kind!r}"
    )


def _build_system_event(
    data: dict[str, Any],
    ctx: _LineContext,
) -> RawSessionEvent:
    # `subtype` distinguishes Claude Code's system-line variants — most
    # importantly `away_summary` (operator-facing recap text written by
    # Claude Code when the user goes idle). Per 2026-06-01 D8 hybrid
    # extraction ruling, M6 auto-summarize prefers these recaps over
    # inference for ~74% of claude_code sessions that carry them. The
    # field is additive — existing persisted events without a subtype
    # parse unchanged.
    return RawSessionEvent(
        external_session_id=ctx.session_id,
        payload={
            "kind": PAYLOAD_KIND_SYSTEM,
            "role": "system",
            "text": _extract_system_text(data),
            "project_path": ctx.project_path,
            "git_branch": ctx.git_branch,
            "subtype": data.get("subtype"),
        },
        event_at=ctx.event_at,
        vendor_event_id=ctx.line_uuid,
        vendor_parent_event_id=ctx.parent_uuid,
    )


def _build_message_event(
    ctx: _LineContext,
    *,
    role: str,
    text: str,
    usage: dict[str, Any] | None = None,
) -> RawSessionEvent:
    return RawSessionEvent(
        external_session_id=ctx.session_id,
        payload={
            "kind": PAYLOAD_KIND_MESSAGE,
            "role": role,
            "text": text,
            "project_path": ctx.project_path,
            "git_branch": ctx.git_branch,
            "usage": usage,
        },
        event_at=ctx.event_at,
        vendor_event_id=ctx.line_uuid,
        vendor_parent_event_id=ctx.parent_uuid,
    )


def _build_tool_call_event(
    *,
    block: dict[str, Any],
    session_id: str,
    role: str,
    event_at: datetime,
    line_uuid: str | None,
    project_path: str | None,
    git_branch: str | None,
) -> RawSessionEvent:
    tool_id = block.get("id")
    tool_name = block.get("name")
    tool_input = block.get("input")
    if not isinstance(tool_id, str) or not tool_id:
        raise ValueError("claude_code: tool_use block missing string 'id'")
    if not isinstance(tool_name, str) or not tool_name:
        raise ValueError("claude_code: tool_use block missing string 'name'")
    if not isinstance(tool_input, dict):
        # Anthropic content protocol guarantees a dict; refuse anything else
        # rather than coercing silently.
        raise ValueError("claude_code: tool_use block 'input' must be a dict")
    return RawSessionEvent(
        external_session_id=session_id,
        payload={
            "kind": PAYLOAD_KIND_TOOL_CALL,
            "role": role,
            "tool_use_id": tool_id,
            "tool_name": tool_name,
            "tool_input": tool_input,
            "project_path": project_path,
            "git_branch": git_branch,
        },
        event_at=event_at,
        vendor_event_id=tool_id,
        vendor_parent_event_id=line_uuid,
    )


def _build_tool_result_event(
    *,
    block: dict[str, Any],
    session_id: str,
    role: str,
    event_at: datetime,
    line_uuid: str | None,
    project_path: str | None,
    git_branch: str | None,
) -> RawSessionEvent:
    tool_use_id = block.get("tool_use_id")
    if not isinstance(tool_use_id, str) or not tool_use_id:
        raise ValueError("claude_code: tool_result block missing string 'tool_use_id'")
    text = _extract_tool_result_text(block.get("content"))
    return RawSessionEvent(
        external_session_id=session_id,
        payload={
            "kind": PAYLOAD_KIND_TOOL_RESULT,
            "role": role,
            "tool_use_id": tool_use_id,
            "text": text,
            "project_path": project_path,
            "git_branch": git_branch,
        },
        event_at=event_at,
        vendor_event_id=line_uuid,
        vendor_parent_event_id=tool_use_id,
    )


def _extract_tool_result_text(content: object) -> str:
    """Flatten a tool_result content payload to a single text body.

    Anthropic's tool_result content is either a string, a list of content
    blocks (each ``{"type": "text", "text": "..."}``), or — for the
    server-side built-in tool variants surfaced by Bug H (2026-06-13:
    ``advisor_tool_result`` / ``web_search_tool_result`` / etc.) — a
    single dict carrying ``{"type": "<tool>_result", "text": "..."}``.
    All shapes collapse to a single text body.
    """
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "text":
                value = block.get("text")
                if isinstance(value, str):
                    parts.append(value)
        return "\n".join(parts)
    if isinstance(content, dict):
        # Bug H: server-side tool results inline a single block as a
        # bare dict instead of wrapping it in a list.
        value = content.get("text")
        return value if isinstance(value, str) else ""
    raise ValueError(
        f"claude_code: tool_result content must be str, list, or dict, "
        f"got {type(content).__name__}"
    )


def _extract_system_text(data: dict[str, Any]) -> str:
    """Extract a textual body from a ``type=system`` line.

    System lines carry a ``content`` field (string) or a structured
    payload. We surface the string form; a non-string payload is
    serialized verbatim so the audit trail keeps the full shape.
    """
    content = data.get("content")
    if isinstance(content, str):
        return content
    # Fallback to a deterministic JSON serialization; no defensive
    # coercion of missing fields.
    return json.dumps(data, sort_keys=True, ensure_ascii=False)


def _require_session_id(data: dict[str, Any]) -> str:
    session_id = data.get("sessionId")
    if not isinstance(session_id, str) or not session_id:
        raise ValueError("claude_code: line missing non-empty string 'sessionId'")
    return session_id


def _parse_timestamp(data: dict[str, Any]) -> datetime:
    value = data.get("timestamp")
    if not isinstance(value, str) or not value:
        raise ValueError("claude_code: line missing non-empty string 'timestamp'")
    s = value
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    parsed = datetime.fromisoformat(s)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed


def _optional_str(value: object) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        return value if value else None
    return None


__all__ = [
    "PAYLOAD_KIND_MESSAGE",
    "PAYLOAD_KIND_SYSTEM",
    "PAYLOAD_KIND_TOOL_CALL",
    "PAYLOAD_KIND_TOOL_RESULT",
    "ClaudeCodeSessionMetadata",
    "parse_line",
    "parse_line_data",
    "read_session_metadata",
]
