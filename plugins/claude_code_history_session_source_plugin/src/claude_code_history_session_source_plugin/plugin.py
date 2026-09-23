"""Claude Code `~/.claude/history.jsonl` as an LLM session ledger source (M7).

Spec §17.3 M7 / architect v2 §6.1. ONE file (per-user history), append-only,
one JSONL line per user prompt. Each line yields one MESSAGE event with
``role=USER``, ``content_text=line.display``, ``event_at=line.timestamp/1000``.

Cursor semantics:

* Discovery cursor — ``{"byte_offset": int}`` at the SOURCE level. The
  file's post-last-fully-drained-line offset. ``session_discovery_cursor``
  returns this AFTER discover_sessions has scanned the new bytes.
* Per-session read cursor — informational; the source-level byte_offset
  is authoritative because all sessions share one file. ``event_read_cursor``
  returns ``{}`` for sessions seen in the current pass.

Multi-session-per-file pattern: ``discover_sessions`` scans new bytes ONCE
per poll, groups parsed lines by external_session_id, stashes them in
``self._pending_events_by_session``, and yields one ExternalSessionRef per
unique session. ``read_events`` drains that map. The plugin's in-memory
state lives for one poll-pass duration — discover and read are guaranteed
sequential within ``SessionLedgerImporter._poll_one_pulling_source``.

External-session keying (path (ii) hybrid):

* ``sessionId`` present + non-empty → ``external_session_id = sessionId``
  (~93% of operator's lines per 2026-06-11 probe).
* Else → ``external_session_id = f"history_orphan_{ts_ms}_{sha256(project)[:16]}"``
  (~7%; concentrated at file head, pre-sessionId era).

Idempotency: each yielded RawSessionEvent carries
``vendor_event_id = f"history_{byte_offset}"``. The importer's
``_persist_normalized_for_session`` short-circuits duplicates by
``(session_id, vendor_event_id)`` so re-polls after a partial crash
don't double-insert.

NUL strip: handled centrally at the repository TEXT-write seam
(``ananta.llm.session_ledger.repository._strip_nuls``); the plugin emits
raw display text.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from pathlib import Path

from ananta.core.plugins.plugin_base import PluginBase
from ananta.interfaces.llm_session_source_interface import (
    LLMSessionSourceInterface,
    PullingSourceMixin,
)
from ananta.llm.session_ledger.root_uri import root_uri_to_path
from ananta.llm.session_ledger.types import (
    EventType,
    ExternalSessionRef,
    IngestMode,
    IngestSourceKind,
    MessageRole,
    NormalizedSessionEvent,
    RawSessionEvent,
    SessionSourceDescriptor,
    SourceVendor,
)
from ananta.llm.session_ledger.vendor import claude_code_history as history_vendor

logger = logging.getLogger(__name__)

_CURSOR_FIELD_OFFSET = "byte_offset"


class ClaudeCodeHistorySessionSourcePlugin(
    PluginBase,
    LLMSessionSourceInterface,
    PullingSourceMixin,
):
    """Tails ``~/.claude/history.jsonl`` and surfaces each line as a MESSAGE event."""

    def __init__(self) -> None:
        super().__init__()
        self.name = "claude_code_history_session_source_plugin"
        # Per-poll-pass workspace populated by ``discover_sessions`` and
        # drained by ``read_events``. The importer guarantees these run
        # sequentially within one ``_poll_one_pulling_source`` invocation.
        self._pending_events_by_session: dict[str, list[RawSessionEvent]] = {}
        self._first_seen_by_session: dict[str, ExternalSessionRef] = {}
        self._latest_byte_offset: int = 0

    # ------------------------------------------------------------------
    # PluginBase lifecycle
    # ------------------------------------------------------------------

    def prepare_for_readiness(self) -> None:
        # root_uri is resolved lazily on first discover_sessions; the file
        # may not exist yet on a freshly-cloned operator machine.
        self.set_ready()

    def initialize(self, config: dict[str, object]) -> None:
        """Bind config_provider so yaml defaults + per-deployment overrides take effect."""
        from ananta.core.config.config_provider import ConfigProvider  # noqa: PLC0415

        self.config_provider = ConfigProvider(self.name, config)

    # ------------------------------------------------------------------
    # LLMSessionSourceInterface
    # ------------------------------------------------------------------

    def describe(self) -> SessionSourceDescriptor:
        return SessionSourceDescriptor(
            source_kind=IngestSourceKind.CLAUDE_CODE_HISTORY,
            vendor=SourceVendor.CLAUDE_CODE,
            supported_modes=(IngestMode.PULLING,),
            default_pulling_root_uri="~/.claude/history.jsonl",
        )

    def normalize(self, raw: RawSessionEvent) -> NormalizedSessionEvent:
        payload = raw.payload
        kind = payload.get("kind")
        if kind != history_vendor.PAYLOAD_KIND_MESSAGE:
            raise ValueError(
                f"claude_code history: unexpected payload kind {kind!r}; "
                f"only {history_vendor.PAYLOAD_KIND_MESSAGE!r} is emitted by this source",
            )
        display = payload.get("display")
        if not isinstance(display, str):
            raise ValueError(
                f"claude_code history: payload missing 'display' string (got {type(display).__name__})",
            )
        return NormalizedSessionEvent(
            external_session_id=raw.external_session_id,
            event_type=EventType.MESSAGE,
            role=MessageRole.USER,
            content_text=display,
            content_json=None,
            event_at=raw.event_at,
            vendor_event_id=raw.vendor_event_id,
            vendor_parent_event_id=raw.vendor_parent_event_id,
            attachment_blob_upload=None,
            attachment_mime_type=None,
            attachment_filename=None,
        )

    # ------------------------------------------------------------------
    # PullingSourceMixin
    # ------------------------------------------------------------------

    def discover_sessions(
        self,
        root_uri: str,
        cursor_payload: dict[str, object] | None,
    ) -> Iterator[ExternalSessionRef]:
        """Scan new bytes once, group lines by external_session_id, yield one ref each.

        Mutates ``self._pending_events_by_session`` /
        ``self._first_seen_by_session`` / ``self._latest_byte_offset``.
        The importer calls ``read_events`` immediately after for each
        yielded ref; ``session_discovery_cursor`` advances the file-level
        byte_offset only AFTER the importer has flushed every session.
        """
        file_path = self._resolve_root_path(root_uri)
        if not file_path.is_file():
            return
        start_offset = _parse_byte_offset(cursor_payload) or 0
        # Reset per-poll workspace
        self._pending_events_by_session = {}
        self._first_seen_by_session = {}
        self._latest_byte_offset = start_offset
        try:
            handle = file_path.open("rb")
        except OSError as exc:
            raise ValueError(
                f"claude_code history: cannot open {file_path}: {exc}"
            ) from exc
        with handle:
            handle.seek(start_offset)
            for parsed in history_vendor.parse_file_from_offset(handle, start_offset):
                self._latest_byte_offset = parsed.byte_offset
                raw = history_vendor.to_raw_event(parsed)
                self._pending_events_by_session.setdefault(
                    parsed.external_session_id, []
                ).append(raw)
                if parsed.external_session_id not in self._first_seen_by_session:
                    self._first_seen_by_session[parsed.external_session_id] = (
                        ExternalSessionRef(
                            external_session_id=parsed.external_session_id,
                            vendor_session_label=None,
                            project_path=parsed.project,
                            first_seen_at=parsed.event_at,
                        )
                    )
        yield from self._first_seen_by_session.values()

    def read_events(
        self,
        root_uri: str,
        session: ExternalSessionRef,
        cursor_payload: dict[str, object] | None,
    ) -> Iterator[RawSessionEvent]:
        """Drain pending events for this session-id captured during discovery.

        ``cursor_payload`` is informational only; the source-level
        byte_offset cursor is authoritative because all sessions share one
        underlying file.
        """
        del root_uri, cursor_payload
        events = self._pending_events_by_session.pop(session.external_session_id, [])
        yield from events

    def session_discovery_cursor(
        self,
        root_uri: str,
        last_seen: ExternalSessionRef | None,
    ) -> dict[str, object]:
        del root_uri, last_seen  # cursor is file-level, not session-level
        return {_CURSOR_FIELD_OFFSET: self._latest_byte_offset}

    def event_read_cursor(
        self,
        root_uri: str,
        session: ExternalSessionRef,
        last_event: RawSessionEvent | None,
    ) -> dict[str, object]:
        """Per-session cursor is informational only.

        Returns the post-last-line byte offset from the most recent raw event
        so the importer's per-session ``event_read`` cursor row stays
        non-empty (the repository's cursor write contract rejects ``None``).
        The source-level discovery cursor is the load-bearing progress
        marker for this single-file source.
        """
        del root_uri, session
        if last_event is None:
            return {_CURSOR_FIELD_OFFSET: 0}
        offset = last_event.payload.get("_byte_offset")
        if not isinstance(offset, int):
            raise ValueError(
                "claude_code history: last_event missing internal '_byte_offset'",
            )
        return {_CURSOR_FIELD_OFFSET: offset}

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _resolve_root_path(self, root_uri: str) -> Path:
        """Resolve the polled source row's ``root_uri`` to the history file (P1.1.E)."""
        return root_uri_to_path(root_uri)


def _parse_byte_offset(cursor_payload: dict[str, object] | None) -> int | None:
    if cursor_payload is None:
        return None
    value = cursor_payload.get(_CURSOR_FIELD_OFFSET)
    if not isinstance(value, int):
        return None
    return value


__all__ = ["ClaudeCodeHistorySessionSourcePlugin"]
