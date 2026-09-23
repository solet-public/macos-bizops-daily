"""Canonical dataclasses and enums for the LLM session ledger.

Per spec §9. All frozen + slots. ``RawSessionEvent`` is the vendor-shaped
input to ``LLMSessionSourceInterface.normalize(...)``; ``NormalizedSessionEvent``
is the canonical form persisted into ``session_ledger__event``.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any


class EventType(StrEnum):
    MESSAGE = "MESSAGE"
    TOOL_CALL = "TOOL_CALL"
    TOOL_RESULT = "TOOL_RESULT"
    SYSTEM = "SYSTEM"
    ATTACHMENT = "ATTACHMENT"


class IngestSourceKind(StrEnum):
    AGENT_MESSAGING = "agent_messaging"
    CODEX_LOCAL = "codex_local"
    CODEX_CLOUD = "codex_cloud"
    CODEX_PUSHED = "codex_pushed"
    CODEX_STATE = "codex_state"
    CODEX_HISTORY = "codex_history"
    CODEX_GOALS = "codex_goals"
    CODEX_MEMORIES = "codex_memories"
    CODEX_AMBIENT = "codex_ambient"
    CLAUDE_CODE_LOCAL = "claude_code_local"
    CLAUDE_CODE_CLOUD = "claude_code_cloud"
    CLAUDE_CODE_PUSHED = "claude_code_pushed"
    CLAUDE_CODE_HISTORY = "claude_code_history"
    CLAUDE_CODE_TASKS = "claude_code_tasks"
    CLAUDE_AI_EXPORT = "claude_ai_export"
    CHATGPT_EXPORT = "chatgpt_export"


class IngestMode(StrEnum):
    PULLING = "pulling"
    PUSHED = "pushed"


class SourceVendor(StrEnum):
    AGENT_MESSAGING = "agent_messaging"
    CODEX = "codex"
    CLAUDE_CODE = "claude_code"
    CLAUDE_AI = "claude_ai"
    CHATGPT = "chatgpt"


class CursorScope(StrEnum):
    DISCOVERY = "discovery"
    EVENT_READ = "event_read"


class SessionsOrderBy(StrEnum):
    """M17 §2.3 canonical ORDER BY tokens for list_sessions.

    Supersedes the magic-string ``_LIST_SESSIONS_ORDER_BY`` dict Day added
    pre-M17. Repository whitelist enforcement is replaced by enum
    validation at the service entry-point (str → enum coerce); arbitrary
    strings can no longer reach the SQL builder.
    """

    LAST_EVENT_AT_DESC = "last_event_at_desc"
    LAST_EVENT_AT_ASC = "last_event_at_asc"
    FIRST_EVENT_AT_DESC = "first_event_at_desc"
    FIRST_EVENT_AT_ASC = "first_event_at_asc"


class ImportBatchStatus(StrEnum):
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"


class PairingStatus(StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    PAIRED = "paired"
    REVOKED = "revoked"


class MessageRole(StrEnum):
    USER = "user"
    ASSISTANT = "assistant"
    SYSTEM = "system"
    TOOL = "tool"


@dataclass(frozen=True, slots=True)
class SessionSourceDescriptor:
    """What a source plugin advertises to the registry.

    ``default_pulling_root_uri`` is the sentinel/path the source can be
    auto-registered with at boot when no ``session_ledger__source`` row exists
    yet. PULLING-mode plugins that can self-bootstrap populate it; sources
    that need an operator-supplied path (e.g. filesystem roots) leave it
    ``None`` so the operator-bridge ``register_source`` flow stays load-bearing.
    """

    source_kind: IngestSourceKind
    vendor: SourceVendor
    supported_modes: tuple[IngestMode, ...]
    default_lease_ttl_seconds: int | None = None
    default_pulling_root_uri: str | None = None


@dataclass(frozen=True, slots=True)
class ExternalSessionRef:
    """Vendor-side handle to a session before it is normalized.

    Per 2026-05-31 Architect ruling §3: per-peer distinction fields
    surface here so the source plugin can flow them into the session
    repository's first-write at session-row creation time. All four
    new fields are ``Optional[str] = None`` for backward compat — every
    existing session source plugin (codex_filesystem, codex_pushed,
    agent_messaging, chatgpt_export, claude_code_pushed) populates them
    as ``None`` by virtue of not opting in. claude_code_filesystem is
    the first plugin that sets them.
    """

    external_session_id: str
    vendor_session_label: str | None
    project_path: str | None
    first_seen_at: datetime
    originator_session_label: str | None = None
    originator_agent_instance_id: str | None = None
    recipient_session_label: str | None = None
    recipient_agent_instance_id: str | None = None
    summary_text_seed: str | None = None


@dataclass(frozen=True, slots=True)
class RawSessionEvent:
    """Vendor-shaped event prior to normalize().

    ``payload`` is opaque to the platform; the source plugin owns its
    interpretation. ``event_at`` is vendor-supplied wall-clock time.
    """

    external_session_id: str
    payload: dict[str, Any]
    event_at: datetime
    vendor_event_id: str | None
    vendor_parent_event_id: str | None


@dataclass(frozen=True, slots=True)
class NormalizedSessionEvent:
    """Canonical event form persisted into ``session_ledger__event``.

    Per-event-type field-presence contract is enforced by
    ``SessionLedgerRepository.append_event`` (spec §9 table); violations
    raise ``ValueError`` and abort the import. No defensive coercion.
    """

    external_session_id: str
    event_type: EventType
    role: MessageRole | None
    content_text: str | None
    content_json: dict[str, Any] | None
    event_at: datetime
    vendor_event_id: str | None
    vendor_parent_event_id: str | None
    attachment_blob_upload: bytes | None
    attachment_mime_type: str | None
    attachment_filename: str | None
    # Per 2026-05-31 Architect ruling §1: event-row actor snapshot,
    # taken at the time this specific event was written. Defaults to
    # None for sources / event-types without an actor identity.
    actor_session_label: str | None = None
    actor_agent_instance_id: str | None = None
    # T1 usage-capture lane (2026-08-05 ruling): per-message token usage,
    # persisted verbatim from the vendor's own raw shape (no re-derivation
    # or field renaming) rather than a rigid typed schema -- vendors vary
    # (Claude Code's cache_creation/cache_read fields have no Codex
    # equivalent). None for every event the source vendor doesn't (yet)
    # populate usage for -- widened additively, not a required field.
    usage_json: dict[str, Any] | None = None


@dataclass(frozen=True, slots=True)
class ImporterReport:
    """One full poll-pass tally returned by ``SessionLedgerImporter.poll_once()``."""

    sources_polled: int
    sessions_seen: int
    events_persisted: int
    batches_failed: int


@dataclass(frozen=True, slots=True)
class AuthenticatedPrincipal:
    """Verified, server-side caller identity at the point of process execution.

    Sourced from the bridge session's bearer claim. Service-process handlers
    use this to make authz decisions; they MUST NOT trust caller-supplied
    arguments for identity.

    Every bridge session has a bearer claim and therefore a ``client_id`` (per
    spec §14.3 — first-party callers do NOT use the bridge, so no
    AuthenticatedPrincipal is ever constructed without a client_id).
    """

    client_id: str
    agent_id: str
    agent_instance_id: str
    bridge_id: str
    session_id: str
