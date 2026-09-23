"""Single source for the wire shape of a ``peer_inbox`` page.

Three surfaces render the SAME ``PeerInbox`` dataclass for a caller:

- the localhost bridge route ``GET .../peer/inbox`` (``http_routes``) — what
  the no-MCP ``solet-bridge watch`` drain reads;
- the Streamable-HTTP MCP tool dispatch (``mcp_streamable.dispatch``);
- the ``plugin::agent_messaging_plugin::peer_inbox`` platform process
  (``plugin.peer_inbox_process``) — the pull path for sessions with no MCP
  and no long-lived watcher.

Before this module the shape was copied per surface. A copied shape is a
shape that drifts, and the drift is invisible until a consumer reads a key
that one surface stopped emitting — exactly the v10
``<<ADDRESS_ID>> not found`` class of bug, where a ``return_value_schema``
outlived the return it described. The process's declared
``return_value_schema`` is written against THIS module, so the schema and
every surface's payload move together or not at all.

Field-for-field faithful to ``ananta.llm.agent_messaging.models``: enums are
emitted as their ``.value`` (``role_section_status`` is ``"ok"`` / ``"error"``,
lowercase — the serialized token, not the Python member name), datetimes as
ISO-8601 strings, and ``None`` stays ``None`` so an exhausted cursor is
distinguishable from an empty string.
"""

from __future__ import annotations

import dataclasses
from typing import Any


def serialize_message(message: Any) -> dict[str, Any]:
    """Render one ``AgentMessageRow`` as JSON-safe primitives."""
    return {
        "id": message.id,
        "cursor": message.cursor,
        "role": message.role.value,
        "kind": message.kind.value,
        "content": [{"type": p.type, "text": p.text} for p in message.content],
        "action_id": message.action_id,
        "backend_session_id": message.backend_session_id,
        "error": message.error,
        "artifacts": [dataclasses.asdict(a) for a in message.artifacts],
        "metadata": message.metadata,
        "created_at": message.created_at.isoformat(),
    }


def serialize_peer_inbox_entry(entry: Any) -> dict[str, Any]:
    """Render one ``PeerInboxEntry`` — sender identity plus the message row."""
    return {
        "thread_id": entry.thread_id,
        "sender_agent_id": entry.sender_agent_id,
        "sender_agent_instance_id": entry.sender_agent_instance_id,
        "sender_session_label": entry.sender_session_label,
        "message": serialize_message(entry.message),
    }


def serialize_peer_inbox_page(
    page: Any, recipient_agent_instance_id: str,
) -> dict[str, Any]:
    """Render a ``PeerInbox`` page, both sections, with their two cursors.

    ``instance_exhausted`` is the instance section's progress signal. It means
    exhausted under the existing newest-first timestamp cursor only; that legacy
    cursor can skip equal-``created_at`` rows at a page boundary, so it is not
    a claim of globally lossless observation. The role section (``role_entries`` + ``next_role_cursor``) is emitted
    ADDITIVELY; the instance section keys are byte-for-byte unchanged.
    ``role_section_status`` / ``role_section_error`` carry the v10 Q1
    fault-domain outcome so a caller can tell an empty role section (no role
    messages) from a failed one — status ``"ok"`` means the section was
    COMPUTED without error, never that it is drained. Drained is
    ``next_role_cursor is None``.

    A successful role page is not a drain; continue until
    ``next_role_cursor`` is null. ``role_limit`` is the effective limit and
    the additive truncation fields disclose whether a continuation was caused
    by that row limit or the role-page byte ceiling.
    """
    return {
        "recipient_agent_id": page.recipient_agent_id,
        "recipient_agent_instance_id": recipient_agent_instance_id,
        "entries": [serialize_peer_inbox_entry(entry) for entry in page.entries],
        "next_after_created_at": (
            page.next_after_created_at.isoformat()
            if page.next_after_created_at is not None
            else None
        ),
        "instance_exhausted": page.instance_exhausted,
        "role_entries": [
            serialize_peer_inbox_entry(entry) for entry in page.role_entries
        ],
        "next_role_cursor": page.next_role_cursor,
        "role_section_status": page.role_section_status.value,
        "role_section_error": page.role_section_error,
        # Pull-surface boundary (design §5): False / None until a session
        # calls peer_mark_role_covered at least once for a held role — no
        # behavior change for a caller that ignores these two keys.
        # role_floor_applied=True means the default drain's floor removed at
        # least one already-covered row this call; role_history_cursor is
        # populated ONLY on a genuine floor-stop (next_role_cursor is None
        # AND role_floor_applied) — echo it back as role_after for a
        # deliberate pre-mark read.
        "role_floor_applied": page.role_floor_applied,
        "role_history_cursor": page.role_history_cursor,
        "role_limit": page.role_limit,
        "role_page_truncated": page.role_page_truncated,
        "role_truncation_reason": (
            page.role_truncation_reason.value
            if page.role_truncation_reason is not None
            else None
        ),
        "role_byte_ceiling": page.role_byte_ceiling,
        "role_read_page_token": page.role_read_page_token,
        "role_read_page_status": page.role_read_page_status,
    }
