"""ChatGPT export-archive parser + normalizer (vendor='chatgpt').

OpenAI's "Export data" produces a ZIP containing conversation data at the
archive root in one of two formats:

- **Legacy single-file**: ``conversations.json`` (a JSON array of conversations).
- **Current sharded** (verified empirically on a 2026-06-11 export):
  ``conversations-000.json``, ``conversations-001.json``, ... Each shard is
  a JSON array of conversations; shards walked in lexicographic =
  chronological order.

Either format yields the same per-conversation shape::

    {
      "title": "...",
      "create_time": 1700000000.0,
      "update_time": 1700000100.0,
      "conversation_id": "uuid",
      "mapping": {
        "<node_id>": {
          "id": "<node_id>",
          "message": {
            "id": "<node_id>",
            "author": {"role": "user" | "assistant" | "system" | "tool"},
            "create_time": 1700000000.0,
            "content": {"content_type": "text", "parts": ["..."]},
          },
          "parent": "<parent_id>" | null,
          "children": ["<child_id>", ...]
        },
        ...
      }
    }

The plugin's :class:`PullingSourceMixin` adapter walks the conversation set
once per upload (cursor advances by ``conversation_index``, which counts
across all shards in chronological order).

``shared_conversations.json`` is intentionally NOT walked — it carries
share-link metadata, not full transcripts.

ValueError on unrecognized shapes. Skip-set is empty: every node in the
mapping that carries a ``message`` payload is emitted as one event.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from zipfile import BadZipFile, ZipFile

from ananta.llm.session_ledger.types import (
    EventType,
    MessageRole,
    NormalizedSessionEvent,
    RawSessionEvent,
)

LEGACY_CONVERSATIONS_MEMBER = "conversations.json"
SHARD_PATTERN = re.compile(r"^conversations-\d+\.json$")


# ───────────────────────────────────────────────────────────────────────────
# ZIP walking
# ───────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class ChatGPTConversation:
    """One conversation entry from ``conversations.json``."""

    conversation_index: int
    conversation_id: str
    title: str | None
    create_time: datetime
    update_time: datetime
    raw: dict[str, Any]


def open_archive(blob_path: Path) -> ZipFile:
    """Open the export ZIP. Fail-fast on unreadable archive.

    Caller closes via context manager. Spec §10.10.1 step 3.
    """
    try:
        return ZipFile(blob_path, mode="r")
    except (BadZipFile, OSError) as exc:
        raise ValueError(f"chatgpt export at {blob_path} is not a readable ZIP: {exc}") from exc


def _enumerate_conversation_members(zf: ZipFile) -> list[str]:
    """Return the list of conversation-bearing JSON members to walk, in order.

    OpenAI's ChatGPT export shifted from a single root-level
    ``conversations.json`` to sharded ``conversations-NNN.json`` files
    (000, 001, ...). This helper detects which format the archive uses
    by examining its actual member list:

    - Single-file: returns ``[LEGACY_CONVERSATIONS_MEMBER]``.
    - Sharded: returns every ``conversations-NNN.json`` member in
      lexicographic order (matches OpenAI's chronological numbering).
    - Neither: raises ``ValueError`` listing what WAS in the archive.

    ``shared_conversations.json`` is intentionally excluded — it carries
    share-link metadata, not full transcripts, and its per-entry shape
    omits ``mapping`` and ``create_time``.
    """
    names = zf.namelist()
    if LEGACY_CONVERSATIONS_MEMBER in names:
        return [LEGACY_CONVERSATIONS_MEMBER]
    shards = sorted(n for n in names if SHARD_PATTERN.match(n))
    if shards:
        return shards
    json_members = [n for n in names if n.endswith(".json")][:10]
    raise ValueError(
        f"chatgpt export ZIP has neither {LEGACY_CONVERSATIONS_MEMBER!r} "
        f"nor any conversations-NNN.json shards (sample json members: "
        f"{json_members})"
    )


def load_conversations_member(zf: ZipFile) -> list[Any]:
    """Read every conversation-bearing member and return a flat list.

    Accepts both the legacy single-file format (returns the JSON array
    as-is) and the current sharded format (concatenates shards in
    chronological order). Raises ``ValueError`` if neither format is
    present, any member is malformed JSON, or any top-level shape is
    not a JSON array.
    """
    members = _enumerate_conversation_members(zf)
    flat: list[Any] = []
    for member in members:
        with zf.open(member) as fh:
            raw = fh.read()
        try:
            payload = json.loads(raw.decode("utf-8"))
        except json.JSONDecodeError as exc:
            raise ValueError(f"{member} is not valid JSON: {exc}") from exc
        if not isinstance(payload, list):
            raise ValueError(f"{member} top-level must be a JSON array")
        flat.extend(payload)
    return flat


def walk_conversations(blob_path: Path) -> Iterator[ChatGPTConversation]:
    """Yield every conversation in chronological order (by index in the file)."""
    with open_archive(blob_path) as zf:
        rows = load_conversations_member(zf)
    for idx, row in enumerate(rows):
        if not isinstance(row, dict):
            raise ValueError(f"conversations.json[{idx}] is not a JSON object")
        cid = row.get("conversation_id") or row.get("id")
        if not isinstance(cid, str) or not cid:
            raise ValueError(
                f"conversations.json[{idx}] missing non-empty 'conversation_id'"
            )
        title = row.get("title") if isinstance(row.get("title"), str) else None
        create_time = _coerce_unix_time(row.get("create_time"), label=f"[{idx}].create_time")
        update_time = _coerce_unix_time(row.get("update_time"), label=f"[{idx}].update_time")
        yield ChatGPTConversation(
            conversation_index=idx,
            conversation_id=cid,
            title=title,
            create_time=create_time,
            update_time=update_time,
            raw=row,
        )


# ───────────────────────────────────────────────────────────────────────────
# Per-conversation event extraction
# ───────────────────────────────────────────────────────────────────────────


def iter_events_for_conversation(
    conv: ChatGPTConversation,
) -> Iterator[RawSessionEvent]:
    """Yield one RawSessionEvent per message node in chronological order.

    Walks the ``mapping`` tree depth-first from the root, then secondary-sorts
    by ``message.create_time`` to yield chronologically. Empty / metadata
    nodes (no ``message`` payload, no role, or empty parts) are skipped.

    Raises ``ValueError`` on a malformed mapping shape.
    """
    mapping = conv.raw.get("mapping")
    if not isinstance(mapping, dict):
        raise ValueError(
            f"conversation {conv.conversation_id!r} missing dict 'mapping'"
        )
    ordered_nodes = _ordered_message_nodes(mapping)
    for node_id, node in ordered_nodes:
        msg = node.get("message")
        if not isinstance(msg, dict):
            continue  # navigation node; no message payload
        author = msg.get("author")
        if not isinstance(author, dict):
            continue
        role_str = author.get("role")
        if not isinstance(role_str, str):
            continue
        text = _extract_text(msg)
        if text is None:
            continue  # empty / non-text payload — skip silently
        create_time = _coerce_unix_time(
            msg.get("create_time"),
            label=f"{conv.conversation_id}/{node_id}/create_time",
            default=conv.update_time,
        )
        parent_id = node.get("parent") if isinstance(node.get("parent"), str) else None
        yield RawSessionEvent(
            external_session_id=conv.conversation_id,
            payload={
                "_chatgpt_role": role_str,
                "text": text,
                "raw_message": msg,
            },
            event_at=create_time,
            vendor_event_id=node_id,
            vendor_parent_event_id=parent_id,
        )


# ───────────────────────────────────────────────────────────────────────────
# Normalization
# ───────────────────────────────────────────────────────────────────────────


def normalize_raw(raw: RawSessionEvent) -> NormalizedSessionEvent:
    role_str = raw.payload.get("_chatgpt_role")
    if not isinstance(role_str, str):
        raise ValueError(
            "chatgpt RawSessionEvent missing '_chatgpt_role' (was the parser bypassed?)"
        )
    role = _map_chatgpt_role(role_str)
    text = raw.payload.get("text")
    if not isinstance(text, str):
        raise ValueError("chatgpt RawSessionEvent missing string 'text'")
    return NormalizedSessionEvent(
        external_session_id=raw.external_session_id,
        event_type=EventType.MESSAGE,
        role=role,
        content_text=text,
        content_json=None,
        event_at=raw.event_at,
        vendor_event_id=raw.vendor_event_id,
        vendor_parent_event_id=raw.vendor_parent_event_id,
        attachment_blob_upload=None,
        attachment_mime_type=None,
        attachment_filename=None,
    )


# ───────────────────────────────────────────────────────────────────────────
# Internals
# ───────────────────────────────────────────────────────────────────────────


def _map_chatgpt_role(role: str) -> MessageRole:
    if role == "user":
        return MessageRole.USER
    if role == "assistant":
        return MessageRole.ASSISTANT
    if role == "system":
        return MessageRole.SYSTEM
    if role == "tool":
        return MessageRole.TOOL
    raise ValueError(f"chatgpt role {role!r} cannot be mapped to MessageRole")


def _extract_text(msg: dict[str, Any]) -> str | None:
    """Extract the visible text from a ChatGPT message node.

    ``content.content_type`` is usually ``"text"`` with ``parts: [str, ...]``;
    other content types (multimodal image references, code interpreter, etc.)
    may carry text in ``parts`` or be skipped entirely.
    """
    content = msg.get("content")
    if not isinstance(content, dict):
        return None
    parts = content.get("parts")
    if not isinstance(parts, list):
        return None
    chunks: list[str] = []
    for part in parts:
        if isinstance(part, str):
            chunks.append(part)
        elif isinstance(part, dict):
            text = part.get("text")
            if isinstance(text, str):
                chunks.append(text)
    joined = "\n".join(c for c in chunks if c)
    return joined or None


def _ordered_message_nodes(
    mapping: dict[str, Any],
) -> list[tuple[str, dict[str, Any]]]:
    """Stable chronological ordering by message.create_time, then node_id.

    The mapping is a tree, but real ChatGPT exports often have multiple roots
    (system + user) and branches. Time-ordering across the flat mapping is
    the simplest reproducible flatten — and matches what the operator sees
    when reading the export.
    """
    items: list[tuple[float, str, dict[str, Any]]] = []
    for node_id, node in mapping.items():
        if not isinstance(node, dict):
            continue
        msg = node.get("message")
        create_time: float
        if isinstance(msg, dict):
            ct = msg.get("create_time")
            create_time = float(ct) if isinstance(ct, (int, float)) else 0.0
        else:
            create_time = 0.0
        items.append((create_time, str(node_id), node))
    items.sort(key=lambda triple: (triple[0], triple[1]))
    return [(node_id, node) for _, node_id, node in items]


def _coerce_unix_time(
    value: object,
    *,
    label: str,
    default: datetime | None = None,
) -> datetime:
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(float(value), tz=UTC)
    if default is not None:
        return default
    raise ValueError(f"{label}: expected numeric UNIX timestamp, got {value!r}")


__all__ = [
    "LEGACY_CONVERSATIONS_MEMBER",
    "SHARD_PATTERN",
    "ChatGPTConversation",
    "iter_events_for_conversation",
    "load_conversations_member",
    "normalize_raw",
    "open_archive",
    "walk_conversations",
]
