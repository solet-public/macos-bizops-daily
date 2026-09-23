"""MCP-pushed Claude Code session events.

Spec §17.3 M3 (pushed variant). The shipper (delivered by
``session_shipper_bootstrap_plugin``) or any equivalent producer (e.g.
a sibling stream off ``session_bridge_plugin``'s spool) pushes chunks
to ``service_interface::session_ledger_service::ingest_raw_chunk`` and
the ledger service dispatches them to ``parse_chunk`` here.

Each chunk is one OR MORE newline-separated Claude Code ``.jsonl`` lines.
Lines parse through the shared platform-owned vendor parser at
``ananta.llm.session_ledger.vendor.claude_code``; per Architect's
2026-05-30 ruling this plugin lives as a SIBLING to
``claude_code_filesystem_session_source_plugin`` and does NOT share
plugin-level Python code with it (mechanical duplication of
``normalize`` is fine; per [[no-shared-plugin-base-class]] /
[[one-plugin-per-scenario]]).
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from typing import Any, cast

from ananta.core.plugins.plugin_base import PluginBase
from ananta.interfaces.llm_session_source_interface import (
    LLMSessionSourceInterface,
    PushedSourceMixin,
)
from ananta.llm.session_ledger.types import (
    EventType,
    IngestMode,
    IngestSourceKind,
    MessageRole,
    NormalizedSessionEvent,
    RawSessionEvent,
    SessionSourceDescriptor,
    SourceVendor,
)
from ananta.llm.session_ledger.vendor.claude_code import (
    PAYLOAD_KIND_MESSAGE,
    PAYLOAD_KIND_SYSTEM,
    PAYLOAD_KIND_TOOL_CALL,
    PAYLOAD_KIND_TOOL_RESULT,
    parse_line,
)

logger = logging.getLogger(__name__)


class ClaudeCodePushedSessionSourcePlugin(
    PluginBase,
    LLMSessionSourceInterface,
    PushedSourceMixin,
):
    """Receives pushed Claude Code session chunks and yields raw events."""

    def __init__(self) -> None:
        super().__init__()
        self.name = "claude_code_pushed_session_source_plugin"

    # ------------------------------------------------------------------
    # PluginBase lifecycle
    # ------------------------------------------------------------------

    def prepare_for_readiness(self) -> None:
        # No upstream services required at readiness time; the ledger
        # service drives parse_chunk on demand.
        self.set_ready()

    # ------------------------------------------------------------------
    # LLMSessionSourceInterface
    # ------------------------------------------------------------------

    def describe(self) -> SessionSourceDescriptor:
        return SessionSourceDescriptor(
            source_kind=IngestSourceKind.CLAUDE_CODE_PUSHED,
            vendor=SourceVendor.CLAUDE_CODE,
            supported_modes=(IngestMode.PUSHED,),
        )

    def normalize(self, raw: RawSessionEvent) -> NormalizedSessionEvent:
        payload = raw.payload
        kind = _require_str(payload, "kind")
        role_str = _require_str(payload, "role")
        if kind == PAYLOAD_KIND_MESSAGE:
            return NormalizedSessionEvent(
                external_session_id=raw.external_session_id,
                event_type=EventType.MESSAGE,
                role=_map_message_role(role_str),
                content_text=_optional_str(payload.get("text")),
                content_json=None,
                event_at=raw.event_at,
                vendor_event_id=raw.vendor_event_id,
                vendor_parent_event_id=raw.vendor_parent_event_id,
                attachment_blob_upload=None,
                attachment_mime_type=None,
                attachment_filename=None,
            )
        if kind == PAYLOAD_KIND_TOOL_CALL:
            tool_name = _require_str(payload, "tool_name")
            tool_use_id = _require_str(payload, "tool_use_id")
            tool_input = payload.get("tool_input")
            if not isinstance(tool_input, dict):
                raise ValueError(
                    "claude_code pushed: tool_call payload missing dict 'tool_input'",
                )
            return NormalizedSessionEvent(
                external_session_id=raw.external_session_id,
                event_type=EventType.TOOL_CALL,
                role=None,
                content_text=None,
                content_json={
                    "tool_name": tool_name,
                    "tool_use_id": tool_use_id,
                    "input": cast(dict[str, Any], tool_input),
                },
                event_at=raw.event_at,
                vendor_event_id=raw.vendor_event_id,
                vendor_parent_event_id=raw.vendor_parent_event_id,
                attachment_blob_upload=None,
                attachment_mime_type=None,
                attachment_filename=None,
            )
        if kind == PAYLOAD_KIND_TOOL_RESULT:
            return NormalizedSessionEvent(
                external_session_id=raw.external_session_id,
                event_type=EventType.TOOL_RESULT,
                role=MessageRole.TOOL,
                content_text=_optional_str(payload.get("text")) or "",
                content_json=None,
                event_at=raw.event_at,
                vendor_event_id=raw.vendor_event_id,
                vendor_parent_event_id=raw.vendor_parent_event_id,
                attachment_blob_upload=None,
                attachment_mime_type=None,
                attachment_filename=None,
            )
        if kind == PAYLOAD_KIND_SYSTEM:
            # Mirrors the filesystem sibling: subtype (e.g. 'away_summary')
            # lifts into content_json so the M6 hybrid extractor can
            # SQL-filter for recap events (operator ruling 2026-06-01 D8).
            subtype = _optional_str(payload.get("subtype"))
            return NormalizedSessionEvent(
                external_session_id=raw.external_session_id,
                event_type=EventType.SYSTEM,
                role=MessageRole.SYSTEM,
                content_text=_optional_str(payload.get("text")) or "",
                content_json={"subtype": subtype} if subtype else None,
                event_at=raw.event_at,
                vendor_event_id=raw.vendor_event_id,
                vendor_parent_event_id=raw.vendor_parent_event_id,
                attachment_blob_upload=None,
                attachment_mime_type=None,
                attachment_filename=None,
            )
        raise ValueError(f"claude_code pushed: unknown payload kind {kind!r}")

    # ------------------------------------------------------------------
    # PushedSourceMixin
    # ------------------------------------------------------------------

    def parse_chunk(self, chunk_text: str) -> Iterator[RawSessionEvent]:
        """Parse one MCP-delivered chunk of newline-separated JSONL lines.

        ``ValueError`` propagates from the vendor parser on a malformed
        line — the ledger service surfaces this as
        ``error_kind='value_error'`` and marks the batch FAILED.
        """
        for line in chunk_text.splitlines():
            yield from parse_line(line)


# ---------------------------------------------------------------------------
# Helpers — duplicated from the filesystem sibling by design; per
# [[no-shared-plugin-base-class]] each plugin owns its own surface.
# ---------------------------------------------------------------------------


def _map_message_role(role: str) -> MessageRole:
    if role == "user":
        return MessageRole.USER
    if role == "assistant":
        return MessageRole.ASSISTANT
    if role == "system":
        return MessageRole.SYSTEM
    raise ValueError(
        f"claude_code pushed: cannot map message role {role!r} to MessageRole",
    )


def _require_str(payload: dict[str, object], field: str) -> str:
    value = payload.get(field)
    if not isinstance(value, str) or not value:
        raise ValueError(
            f"claude_code pushed: payload missing non-empty {field!r}",
        )
    return value


def _optional_str(value: object) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        return value
    return None


__all__ = ["ClaudeCodePushedSessionSourcePlugin"]
