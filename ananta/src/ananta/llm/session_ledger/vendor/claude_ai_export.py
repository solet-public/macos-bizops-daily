"""Claude.ai web export ZIP parser (spec §17.3 M9, architect v2 §6.3).

Operator uploads a ZIP from claude.ai's data-export feature. The ZIP
contains (empirically verified against operator's 2026-06-11 export
``data-043817f1-65f6-4e4a-922e-bbf40752bdb6-1781211388-ed604189-batch-0000.zip``,
69 conversations / 609 messages, 2025-05-24 → 2026-05-21):

* ``conversations.json`` — JSON list of conversations (the M9 content).
* ``users.json`` — 162B account info; not session content. Out of M9 scope.
* ``memories.json`` — 8.7KB; Claude Memory feature output. Future M-section.
* ``projects/<uuid>.json`` — project-bound artifacts. Future M-section.

**Conversation shape** (empirically verified — schema RESOLVED per
brief §1, no reverse-engineering required):

::

    {
      "uuid": str,
      "name": str,
      "summary": str,
      "account": dict,           # READ but NOT PERSISTED
      "created_at": ISO-8601,
      "updated_at": ISO-8601,
      "chat_messages": [
        {
          "uuid": str,
          "sender": "human" | "assistant",
          "text": str,           # short-form
          "content": list | str, # canonical content (structured blocks)
          "created_at": ISO-8601,
          "updated_at": ISO-8601,
          "attachments": list,   # 27/609 non-empty in operator's data
          "files": list,         # 30/609 non-empty in operator's data
          "parent_message_uuid": str  # ROOT_SENTINEL for top-level
        },
        ...
      ]
    }

**M9 emits conversation TEXT only.** Tool-use / tool-result blocks (if
any — rare in claude.ai web UI vs claude_code CLI) are DROPPED at
M9 ingest. Operator wanting the tool-call surface from Claude.ai
conversations is a future M-section (M9.5+).

**Probe (c) outcome (attachment representation)** — empirically scanned
operator's 609-message corpus 2026-06-11:

* 27/609 messages have non-empty ``attachments`` (4.4%).
* 30/609 messages have non-empty ``files`` (4.9%).
* The export ZIP contains ONLY 8 entries (``conversations.json``,
  ``users.json``, ``memories.json``, ``projects/*.json``) — **the
  actual file bytes are NOT in the export**, only metadata (file_uuid
  + file_name + sometimes type/size).

Decision: M9 **defers __attachment event emission behind the
``EMIT_ATTACHMENT_EVENTS`` flag (default False)**. Without file bytes
we can only emit metadata-only ATTACHMENT events; a future M-section
either (a) flips the flag for metadata-only emission, or (b) adds a
Claude.ai API fetch mechanism for the actual bytes.

**Probe (b) outcome (cross-vendor UUID consistency)** — INFORMATIONAL
ONLY. Vendor ``claude_ai`` is namespace-separated
from vendor ``claude_code`` per M18 spec §8.4 partial-unique on
``(vendor, external_session_id)``. Even if Claude.ai conv.uuid values
happen to match claude_code_local rows, no cross-vendor promotion
mechanism fires. Probe is logged for operator awareness only.
"""

from __future__ import annotations

import json
import logging
import zipfile
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from ananta.llm.session_ledger.types import (
    EventType,
    ExternalSessionRef,
    MessageRole,
    NormalizedSessionEvent,
    RawSessionEvent,
)

logger = logging.getLogger(__name__)

# Anthropic's synthetic-root marker for top-level messages.
ROOT_SENTINEL = "00000000-0000-4000-8000-000000000000"

# Filename of the conversations payload inside the export ZIP.
CONVERSATIONS_ENTRY = "conversations.json"

# M9 default: defer __attachment event emission per probe (c) outcome.
# Future M-section flips this to True for metadata-only emission OR
# wires a file-bytes retrieval mechanism.
EMIT_ATTACHMENT_EVENTS = False

# Vendor payload keys for the normalize() dispatch.
PAYLOAD_KIND_MESSAGE = "claude_ai_message"


@dataclass(frozen=True, slots=True)
class _AttachmentMeta:
    """Per-message attachment/file metadata (no bytes; the ZIP doesn't carry them)."""

    kind: str  # "attachment" or "file"
    file_uuid: str | None
    file_name: str | None
    raw: dict[str, object]


@dataclass(frozen=True, slots=True)
class _ParsedMessage:
    """A parsed Claude.ai message ready to become a RawSessionEvent."""

    msg_uuid: str
    sender: str
    content_text: str
    event_at: datetime
    parent_uuid: str | None
    attachment_metas: tuple[_AttachmentMeta, ...]


@dataclass(frozen=True, slots=True)
class ConversationPayload:
    """One parsed conversation: session ref + per-message events.

    Yielded by ``parse_export_zip`` so the source plugin can iterate
    sessions + events from one streaming open of the ZIP without
    decompressing the full archive into memory.
    """

    session_ref: ExternalSessionRef
    messages: tuple[_ParsedMessage, ...]
    summary_seed: str | None


def _parse_iso(value: str) -> datetime:
    """Parse Claude.ai's ISO-8601 timestamps (UTC-anchored with Z suffix).

    Raises ``ValueError`` on naïve / unparseable input (KB "Critical Development Guidelines v2").
    """
    if value.endswith("Z"):
        value = value[:-1] + "+00:00"
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None or parsed.tzinfo.utcoffset(parsed) is None:
        raise ValueError(
            f"claude_ai_export: naïve datetime not accepted; require explicit "
            f"timezone offset (Z or +00:00). Got: {value!r}",
        )
    return parsed.astimezone(UTC)


def _coerce_str(value: object) -> str:
    return value if isinstance(value, str) else ""


def _sender_to_role(sender: str) -> MessageRole:
    """human → USER; assistant → ASSISTANT; anything else falls back to USER.

    Fast-fail on truly unrecognized senders so the operator finds out
    rather than silently mis-attributing events. Empty / unknown senders
    are a schema anomaly worth surfacing.
    """
    if sender == "human":
        return MessageRole.USER
    if sender == "assistant":
        return MessageRole.ASSISTANT
    raise ValueError(
        f"claude_ai_export: unrecognized sender {sender!r}; expected "
        f"'human' or 'assistant'",
    )


def extract_text(msg: dict[str, object]) -> str:
    """Canonicalize message content to a single content_text string.

    Claude.ai messages may have
    ``content`` as either:

    * ``str``: legacy/simple-message shape; return as-is.
    * ``list``: structured-block shape; fold to text-only blocks.

    Tool-use and tool-result blocks (if any) are DROPPED at M9 ingest.
    Fallback chain: structured content's text → ``msg['text']``
    short-form → ``''``.
    """
    content = msg.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        text_blocks = [
            block.get("text", "")
            for block in content
            if isinstance(block, dict) and block.get("type") == "text"
        ]
        joined = "\n\n".join(t for t in text_blocks if t)
        if joined:
            return joined
    short_form = msg.get("text")
    return short_form if isinstance(short_form, str) else ""


def _classify_parent(parent_value: object) -> str | None:
    """Map ``parent_message_uuid`` to vendor_parent_event_id or None.

    ROOT_SENTINEL → None (top-level message); other UUIDs → string.
    Returns None for missing / non-string / sentinel values.
    """
    if not isinstance(parent_value, str):
        return None
    if parent_value == ROOT_SENTINEL or not parent_value:
        return None
    return parent_value


def _parse_attachments(msg: dict[str, object]) -> tuple[_AttachmentMeta, ...]:
    """Build the per-message attachment metadata tuple (metadata only)."""
    metas: list[_AttachmentMeta] = []
    for kind, key in (("attachment", "attachments"), ("file", "files")):
        raw_list = msg.get(key)
        if not isinstance(raw_list, list):
            continue
        for entry in raw_list:
            if not isinstance(entry, dict):
                continue
            metas.append(
                _AttachmentMeta(
                    kind=kind,
                    file_uuid=_coerce_str(entry.get("file_uuid")) or None,
                    file_name=_coerce_str(entry.get("file_name")) or None,
                    raw=entry,
                )
            )
    return tuple(metas)


def _parse_message(msg: dict[str, object]) -> _ParsedMessage | None:
    """Parse one chat_message dict; None on a skip-worthy shape mismatch."""
    msg_uuid = msg.get("uuid")
    if not isinstance(msg_uuid, str) or not msg_uuid:
        logger.warning("claude_ai_export: message missing 'uuid'; skip")
        return None
    sender = msg.get("sender")
    if not isinstance(sender, str):
        logger.warning(
            "claude_ai_export: message %s missing 'sender'; skip", msg_uuid
        )
        return None
    created_at = msg.get("created_at")
    if not isinstance(created_at, str):
        logger.warning(
            "claude_ai_export: message %s missing 'created_at'; skip", msg_uuid
        )
        return None
    try:
        event_at = _parse_iso(created_at)
    except ValueError as exc:
        logger.warning(
            "claude_ai_export: message %s invalid timestamp %s: %s; skip",
            msg_uuid, created_at, exc,
        )
        return None
    return _ParsedMessage(
        msg_uuid=msg_uuid,
        sender=sender,
        content_text=extract_text(msg),
        event_at=event_at,
        parent_uuid=_classify_parent(msg.get("parent_message_uuid")),
        attachment_metas=_parse_attachments(msg),
    )


def _extract_conv_first_seen(
    conv: dict[str, object], conv_uuid: str
) -> datetime | None:
    """Pull + parse the conversation's ``created_at`` timestamp; None on missing/invalid."""
    created_at = conv.get("created_at")
    if not isinstance(created_at, str):
        logger.warning(
            "claude_ai_export: conversation %s missing 'created_at'; skip",
            conv_uuid,
        )
        return None
    try:
        return _parse_iso(created_at)
    except ValueError as exc:
        logger.warning(
            "claude_ai_export: conversation %s invalid created_at: %s; skip",
            conv_uuid, exc,
        )
        return None


def _parse_chat_messages(
    chat_messages: object, conv_uuid: str
) -> tuple[_ParsedMessage, ...] | None:
    """Validate + parse the ``chat_messages`` list; None on shape mismatch."""
    if not isinstance(chat_messages, list):
        logger.warning(
            "claude_ai_export: conversation %s 'chat_messages' not a list; skip",
            conv_uuid,
        )
        return None
    parsed: list[_ParsedMessage] = []
    for m in chat_messages:
        if not isinstance(m, dict):
            continue
        msg = _parse_message(m)
        if msg is not None:
            parsed.append(msg)
    return tuple(parsed)


def _parse_conversation(conv: dict[str, object]) -> ConversationPayload | None:
    """Parse one conversation; None when the shape is unusable."""
    conv_uuid = conv.get("uuid")
    if not isinstance(conv_uuid, str) or not conv_uuid:
        logger.warning("claude_ai_export: conversation missing 'uuid'; skip")
        return None
    first_seen = _extract_conv_first_seen(conv, conv_uuid)
    if first_seen is None:
        return None
    messages = _parse_chat_messages(conv.get("chat_messages"), conv_uuid)
    if messages is None:
        return None
    name = _coerce_str(conv.get("name"))
    summary = _coerce_str(conv.get("summary"))
    summary_seed = summary if summary else None
    session_ref = ExternalSessionRef(
        external_session_id=conv_uuid,
        vendor_session_label=name if name else None,
        project_path=None,
        first_seen_at=first_seen,
        summary_text_seed=summary_seed,
    )
    return ConversationPayload(
        session_ref=session_ref,
        messages=messages,
        summary_seed=summary_seed,
    )


def parse_export_zip(zip_path: Path) -> Iterator[ConversationPayload]:
    """Yield one ConversationPayload per conversation in the ZIP.

    Opens ``conversations.json`` from inside the ZIP without
    decompressing the rest of the archive. ZIP is opened streamingly;
    the JSON is fully loaded into memory once (operator's 4MB sample is
    well below any practical cap, but larger exports may need a
    streaming JSON reader — flagged as future-work).
    """
    if not zip_path.is_file():
        raise ValueError(f"claude_ai_export: cannot read {zip_path}: not a file")
    try:
        with zipfile.ZipFile(zip_path) as zf:
            try:
                with zf.open(CONVERSATIONS_ENTRY) as f:
                    data = json.load(f)
            except KeyError as exc:
                raise ValueError(
                    f"claude_ai_export: {zip_path}: no '{CONVERSATIONS_ENTRY}' "
                    f"entry in archive ({exc})",
                ) from exc
    except zipfile.BadZipFile as exc:
        raise ValueError(
            f"claude_ai_export: {zip_path}: not a valid ZIP ({exc})",
        ) from exc
    if not isinstance(data, list):
        raise ValueError(
            f"claude_ai_export: {zip_path}/{CONVERSATIONS_ENTRY}: "
            f"top-level value is not a list ({type(data).__name__})",
        )
    for conv in data:
        if not isinstance(conv, dict):
            continue
        payload = _parse_conversation(conv)
        if payload is not None:
            yield payload


def to_raw_event(parsed: _ParsedMessage, conv_uuid: str) -> RawSessionEvent:
    """Convert a parsed message into a RawSessionEvent ready for the importer."""
    return RawSessionEvent(
        external_session_id=conv_uuid,
        payload={
            "kind": PAYLOAD_KIND_MESSAGE,
            "sender": parsed.sender,
            "content_text": parsed.content_text,
            "attachment_count": len(parsed.attachment_metas),
        },
        event_at=parsed.event_at,
        vendor_event_id=parsed.msg_uuid,
        vendor_parent_event_id=parsed.parent_uuid,
    )


def to_normalized_event(parsed: _ParsedMessage, conv_uuid: str) -> NormalizedSessionEvent:
    """Convert a parsed message directly to a NormalizedSessionEvent.

    Used by the source plugin's ``normalize`` when the importer prefers
    the RawSessionEvent → normalize path. The role mapping happens here
    so the plugin's normalize() body stays a one-liner.
    """
    return NormalizedSessionEvent(
        external_session_id=conv_uuid,
        event_type=EventType.MESSAGE,
        role=_sender_to_role(parsed.sender),
        content_text=parsed.content_text,
        content_json=None,
        event_at=parsed.event_at,
        vendor_event_id=parsed.msg_uuid,
        vendor_parent_event_id=parsed.parent_uuid,
        attachment_blob_upload=None,
        attachment_mime_type=None,
        attachment_filename=None,
    )


__all__ = [
    "CONVERSATIONS_ENTRY",
    "ConversationPayload",
    "EMIT_ATTACHMENT_EVENTS",
    "PAYLOAD_KIND_MESSAGE",
    "ROOT_SENTINEL",
    "extract_text",
    "parse_export_zip",
    "to_normalized_event",
    "to_raw_event",
]
