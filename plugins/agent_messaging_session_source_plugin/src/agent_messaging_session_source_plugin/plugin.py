"""LLM session ledger source for the platform's own agent_messaging tables.

Reads ``core__agent_thread`` + ``core__agent_message`` as ingest sessions
so peer/agent threads land in the unified session ledger alongside
filesystem and pushed sources.

Discovery (threads) reads through the OWNING agent_messaging interface
(``list_threads``, the unscoped global enumeration) per D1/GAP-5 — no raw
``core__agent_thread`` SQL. Event-read (messages) still reads raw pending the
Architect's unscoped message-read verb (``list_messages`` is bridge-ownership-
scoped, so it cannot serve this cross-bridge ledger projection).

Cursor semantics:

* Discovery cursor: ``{"thread_cursor": "<opaque>"}`` — ``list_threads``'
  opaque ``(created_at, id)`` page token. Reconstructed from the last
  discovered ref (``encode_thread_cursor(first_seen_at, external_session_id)``),
  a tie-safety improvement over the pre-migration created_at-only high-water
  (which dropped same-``created_at`` threads at a page boundary). A
  pre-migration payload (no ``thread_cursor`` key) starts a one-time re-walk —
  ``vendor_event_id`` idempotency dedupes, so no event is re-imported.
* Event-read cursor (per session): ``{"cursor_high_water": N}`` — latest
  ``core__agent_message.cursor`` we have already imported.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any, cast

from ananta.core.plugins.plugin_base import PluginBase
from ananta.interfaces.llm_session_source_interface import (
    LLMSessionSourceInterface,
    PullingSourceMixin,
)
from ananta.llm.agent_messaging.models import (
    ListAgentThreadsRequest,
    ReadThreadMessagesRequest,
)
from ananta.llm.agent_messaging.thread_cursor import encode_thread_cursor
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

logger = logging.getLogger(__name__)


# Discovery pages through list_threads at the primitive's max page size (fewest
# round-trips); the generator loops pages until a short one (drained).
_DISCOVER_PAGE_LIMIT = 100

# read_events pages through read_thread_messages at the verb's max page size
# (read_thread_messages caps each page at _MAX_LIST_LIMIT); the generator loops
# pages until a short one (drained).
_MESSAGE_PAGE_LIMIT = 100

# Cursor key for the discovery pass: the opaque (created_at, id) token from
# list_threads (supersedes the pre-migration ``created_at_high_water_iso``).
_THREAD_CURSOR_KEY = "thread_cursor"


class AgentMessagingSessionSourcePlugin(
    PluginBase,
    LLMSessionSourceInterface,
    PullingSourceMixin,
):
    """Ingest source over the platform's agent_messaging tables.

    State-service is fetched lazily through ``self.orchestrator_ref.get_service``
    rather than via a setter, matching the post-2025-12-06 service-binding
    architecture where plugins request services themselves.
    """

    def __init__(self) -> None:
        super().__init__()
        self.name = "agent_messaging_session_source_plugin"
        self._initialized = False

    # ------------------------------------------------------------------
    # PluginBase lifecycle
    # ------------------------------------------------------------------

    def prepare_for_readiness(self) -> None:
        if self.orchestrator_ref is None:
            raise RuntimeError(
                f"{self.name}: orchestrator_ref not injected before prepare_for_readiness",
            )
        # Probe state_service availability; defer real reads to discover/read.
        state = self.orchestrator_ref.get_service("state_service")
        if state is None:
            raise RuntimeError(
                f"{self.name}: state_service is unavailable; ledger source cannot operate",
            )
        self._initialized = True
        self.set_ready()

    # ------------------------------------------------------------------
    # LLMSessionSourceInterface
    # ------------------------------------------------------------------

    def describe(self) -> SessionSourceDescriptor:
        return SessionSourceDescriptor(
            source_kind=IngestSourceKind.AGENT_MESSAGING,
            vendor=SourceVendor.AGENT_MESSAGING,
            supported_modes=(IngestMode.PULLING,),
            default_pulling_root_uri="local:agent_messaging",
        )

    def normalize(self, raw: RawSessionEvent) -> NormalizedSessionEvent:
        """Convert one agent_message row payload to a canonical event.

        Maps:
        * ``role='originator' | 'agent' | 'system'`` and ``kind`` →
          ``EventType.MESSAGE`` with ``MessageRole``.
        * ``kind='result'`` rows that carry tool output → ``EventType.TOOL_RESULT``.
        * ``kind='error'`` rows → ``EventType.SYSTEM`` with the error payload.

        Raises ``ValueError`` on unrecognized shapes — no fallback coercion.
        """
        payload = raw.payload
        kind = self._require_str(payload, "kind")
        role = self._require_str(payload, "role")
        content = payload.get("content")
        if not isinstance(content, list):
            raise ValueError(
                "agent_message payload missing list-shaped 'content' field",
            )
        text = _extract_text_parts(content)
        actor_session_label, actor_agent_instance_id = _extract_actor_snapshot(
            payload.get("metadata"),
        )
        if kind == "error":
            error_obj = payload.get("error")
            if not isinstance(error_obj, dict):
                raise ValueError("agent_message kind='error' requires dict 'error' payload")
            return NormalizedSessionEvent(
                external_session_id=raw.external_session_id,
                event_type=EventType.SYSTEM,
                role=MessageRole.SYSTEM,
                content_text=text,
                content_json=cast(dict[str, Any], error_obj),
                event_at=raw.event_at,
                vendor_event_id=raw.vendor_event_id,
                vendor_parent_event_id=raw.vendor_parent_event_id,
                attachment_blob_upload=None,
                attachment_mime_type=None,
                attachment_filename=None,
                actor_session_label=actor_session_label,
                actor_agent_instance_id=actor_agent_instance_id,
            )
        if kind == "result":
            return NormalizedSessionEvent(
                external_session_id=raw.external_session_id,
                event_type=EventType.TOOL_RESULT,
                role=MessageRole.TOOL,
                content_text=text or None,
                content_json={"role": role, "raw_content": content},
                event_at=raw.event_at,
                vendor_event_id=raw.vendor_event_id,
                vendor_parent_event_id=raw.vendor_parent_event_id,
                attachment_blob_upload=None,
                attachment_mime_type=None,
                attachment_filename=None,
                actor_session_label=actor_session_label,
                actor_agent_instance_id=actor_agent_instance_id,
            )
        if kind in {"message", "status", "artifact"}:
            mapped_role = _map_role(role)
            return NormalizedSessionEvent(
                external_session_id=raw.external_session_id,
                event_type=EventType.MESSAGE,
                role=mapped_role,
                content_text=text or None,
                content_json=None if text else {"role": role, "raw_content": content},
                event_at=raw.event_at,
                vendor_event_id=raw.vendor_event_id,
                vendor_parent_event_id=raw.vendor_parent_event_id,
                attachment_blob_upload=None,
                attachment_mime_type=None,
                attachment_filename=None,
                actor_session_label=actor_session_label,
                actor_agent_instance_id=actor_agent_instance_id,
            )
        raise ValueError(f"unrecognized agent_message kind {kind!r}")

    # ------------------------------------------------------------------
    # PullingSourceMixin
    # ------------------------------------------------------------------

    def discover_sessions(
        self,
        root_uri: str,  # pyright: ignore[reportUnusedParameter]  # noqa: ARG002
        cursor_payload: dict[str, object] | None,
    ) -> Iterator[ExternalSessionRef]:
        # P1.1.E: symbolic source (``local:agent_messaging``) — reads the DB,
        # not a filesystem path, so root_uri is accepted for contract
        # uniformity and ignored.
        # D1/GAP-5: enumerate threads through the OWNING agent_messaging
        # interface (list_threads — unscoped, cursor-paginated) instead of raw
        # core__agent_thread SQL. AgentThreadRow already carries the 4 per-peer
        # snapshot columns (2026-05-31 Architect ruling §2), snapshotted by
        # service.peer_send at create_thread time; nothing here computes them.
        # list_threads is unbounded-equivalent via the page-loop: it caps each
        # page at 100, so we walk pages (echoing the opaque next_cursor) until a
        # short page signals drained.
        after_cursor = _discovery_after_cursor(cursor_payload)
        service = self._agent_messaging_service()
        while True:
            page = service.list_threads(
                ListAgentThreadsRequest(
                    after_cursor=after_cursor,
                    limit=_DISCOVER_PAGE_LIMIT,
                ),
            )
            for thread in page.threads:
                yield ExternalSessionRef(
                    external_session_id=thread.id,
                    vendor_session_label=thread.title,
                    project_path=thread.working_directory,
                    first_seen_at=thread.created_at,
                    originator_session_label=thread.originator_session_label,
                    originator_agent_instance_id=thread.originator_agent_instance_id,
                    recipient_session_label=thread.recipient_session_label,
                    recipient_agent_instance_id=thread.recipient_agent_instance_id,
                )
            if len(page.threads) < _DISCOVER_PAGE_LIMIT:
                return
            after_cursor = page.next_cursor

    def read_events(
        self,
        root_uri: str,  # pyright: ignore[reportUnusedParameter]  # noqa: ARG002
        session: ExternalSessionRef,
        cursor_payload: dict[str, object] | None,
    ) -> Iterator[RawSessionEvent]:
        # Per 2026-05-31 Architect ruling §1: also surface metadata so
        # normalize() can extract ``sender_session_label`` /
        # ``sender_agent_instance_id`` onto the per-event actor snapshot.
        # The peer_send pipeline already stamps these on every message's
        # metadata column at write time (service.py:432-434).
        #
        # D1/GAP-5: read ONE thread's messages through the OWNING
        # agent_messaging interface (read_thread_messages -- the unscoped,
        # int-cursor-paginated message read) instead of raw core__agent_message
        # SQL. read_thread_messages returns strictly cursor > after_cursor and
        # caps each page at _MAX_LIST_LIMIT, so we walk pages (advancing the int
        # next_cursor) until a short page signals drained -- the unbounded
        # equivalent of the old single ``ORDER BY cursor ASC`` scan. Cursors are
        # 1-based (create_thread seeds last_message_cursor=0; the allocator
        # returns +1), so after_cursor=0 reads from the first message. The cursor
        # key/semantics are unchanged (``cursor_high_water`` int) so -- unlike
        # discover_sessions, whose token FORMAT changed -- no re-baseline is
        # needed; ``event_read_cursor`` is untouched.
        high_water = _parse_cursor_int(cursor_payload, "cursor_high_water")
        after_cursor = high_water if high_water is not None else 0
        service = self._agent_messaging_service()
        while True:
            page = service.read_thread_messages(
                ReadThreadMessagesRequest(
                    thread_id=session.external_session_id,
                    after_cursor=after_cursor,
                    limit=_MESSAGE_PAGE_LIMIT,
                ),
            )
            for message in page.messages:
                yield RawSessionEvent(
                    external_session_id=session.external_session_id,
                    payload={
                        "thread_id": message.thread_id,
                        "cursor": message.cursor,
                        "role": message.role.value,
                        "kind": message.kind.value,
                        "content": [
                            {"type": part.type, "text": part.text}
                            for part in message.content
                        ],
                        "error": message.error,
                        "metadata": message.metadata,
                    },
                    event_at=_as_utc(message.created_at),
                    vendor_event_id=message.id,
                    vendor_parent_event_id=message.action_id,
                )
            if len(page.messages) < _MESSAGE_PAGE_LIMIT:
                return
            after_cursor = page.next_cursor

    def session_discovery_cursor(
        self,
        root_uri: str,  # pyright: ignore[reportUnusedParameter]  # noqa: ARG002
        last_seen: ExternalSessionRef | None,
    ) -> dict[str, object]:
        if last_seen is None:
            return {_THREAD_CURSOR_KEY: None}
        # Reconstruct list_threads' opaque (created_at, id) token from the last
        # ref. The ref carries both halves (first_seen_at == thread.created_at,
        # external_session_id == thread.id), so this is byte-identical to the
        # next_cursor list_threads emits (normalize_sort_value(dt) == dt.isoformat()).
        # Tie-safe: the composite never drops same-created_at threads across a
        # page boundary, unlike the old created_at-only high-water.
        return {
            _THREAD_CURSOR_KEY: encode_thread_cursor(
                created_at_iso=last_seen.first_seen_at.isoformat(),
                row_id=last_seen.external_session_id,
            ),
        }

    def event_read_cursor(
        self,
        root_uri: str,  # pyright: ignore[reportUnusedParameter]  # noqa: ARG002
        session: ExternalSessionRef,
        last_event: RawSessionEvent | None,
    ) -> dict[str, object]:
        if last_event is None:
            return {"cursor_high_water": None}
        cursor = last_event.payload.get("cursor")
        if not isinstance(cursor, int):
            raise ValueError(
                "agent_message payload missing integer 'cursor' for event_read cursor",
            )
        return {"cursor_high_water": cursor}

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _agent_messaging_service(self) -> Any:
        """The agent_messaging plugin, via structural typing.

        agent_messaging is deliberately NOT a bound service (see
        ``AgentMessagingServiceInterface`` — binding it would hide its
        ``plugin::*::*`` EDGE processes), so it is consumed through
        ``orchestrator.plugin_manager.plugins[...]``, not ``get_service``.
        Fails fast if the orchestrator or the plugin is unavailable.
        """
        if self.orchestrator_ref is None:
            raise RuntimeError(
                f"{self.name}: orchestrator_ref unavailable during discovery",
            )
        plugins = cast("Any", self.orchestrator_ref).plugin_manager.plugins
        service = plugins.get("agent_messaging_plugin")
        if service is None:
            raise RuntimeError(
                f"{self.name}: agent_messaging_plugin not loaded; cannot discover threads",
            )
        return service

    @staticmethod
    def _require_str(payload: dict[str, Any], field: str) -> str:
        value = payload.get(field)
        if not isinstance(value, str) or not value:
            raise ValueError(f"agent_message payload missing non-empty {field!r}")
        return value


def _extract_actor_snapshot(
    metadata: object,
) -> tuple[str | None, str | None]:
    """Pull ``sender_session_label`` + ``sender_agent_instance_id`` from message metadata.

    Per 2026-05-31 Architect ruling §1: peer-thread events alternate senders
    within one session, so the per-event actor snapshot must come from
    ``core__agent_message.metadata`` (where the AgentMessagingService
    stamps them on every message at write time). Returns (None, None) for
    any shape that doesn't carry the expected fields.
    """
    if not isinstance(metadata, dict):
        return None, None
    label_raw = metadata.get("sender_session_label")
    label = label_raw if isinstance(label_raw, str) and label_raw else None
    instance_id_raw = metadata.get("sender_agent_instance_id")
    instance_id = (
        instance_id_raw if isinstance(instance_id_raw, str) and instance_id_raw else None
    )
    return label, instance_id


def _map_role(role: str) -> MessageRole:
    if role == "originator":
        return MessageRole.USER
    if role == "agent":
        return MessageRole.ASSISTANT
    if role == "system":
        return MessageRole.SYSTEM
    raise ValueError(f"agent_message role {role!r} cannot be mapped to MessageRole")


def _extract_text_parts(content_parts: list[object]) -> str:
    text_chunks: list[str] = []
    for part in content_parts:
        if not isinstance(part, dict):
            continue
        if part.get("type") == "text":
            text_value = part.get("text")
            if isinstance(text_value, str):
                text_chunks.append(text_value)
    return "\n".join(text_chunks)


def _as_utc(value: datetime) -> datetime:
    """Stamp UTC on a naive datetime so the projected event_at stays tz-aware.

    read_thread_messages' ``created_at`` comes back NAIVE — the DATETIME column
    renders as ``TIMESTAMP`` (not ``timestamptz``), stored ``NOW() AT TIME ZONE
    'UTC'`` — and the repository's ``_coerce_datetime`` passes datetimes through
    unchanged. The pre-migration raw read stamped UTC here, and the ledger's
    time-window queries compare tz-aware values, so we preserve that.
    """
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def _discovery_after_cursor(cursor_payload: dict[str, object] | None) -> str | None:
    """Extract ``list_threads``' opaque ``after_cursor`` from the persisted
    payload, or ``None`` to (re-)walk from the beginning.

    A pre-migration payload (the old ``created_at_high_water_iso``, with no
    ``thread_cursor`` key) → ``None`` → a one-time re-walk (``vendor_event_id``
    idempotency dedupes, so no event is re-imported). Raises ``ValueError`` on a
    malformed (non-string) token — the interface's malformed-cursor contract; a
    structurally-bad opaque token is rejected downstream by ``list_threads``'
    fail-closed decoder.
    """
    if not cursor_payload:
        return None
    token = cursor_payload.get(_THREAD_CURSOR_KEY)
    if token is None:
        return None
    if not isinstance(token, str):
        raise ValueError(
            f"{_THREAD_CURSOR_KEY!r} must be a string or null, got {token!r}",
        )
    return token


def _parse_cursor_int(
    cursor_payload: dict[str, object] | None,
    key: str,
) -> int | None:
    if cursor_payload is None:
        return None
    value = cursor_payload.get(key)
    if value is None:
        return None
    if not isinstance(value, int):
        raise ValueError(f"cursor field {key!r} must be int or null, got {value!r}")
    return value


__all__ = ["AgentMessagingSessionSourcePlugin"]
