"""LLM session source interface — marker + per-mode mixins.

Spec §5.1–§5.3. A concrete source plugin ALWAYS inherits both
``LLMSessionSourceInterface`` (marker) and exactly one of
``PullingSourceMixin`` / ``PushedSourceMixin``. The platform's
``SessionLedgerService.Registry`` dispatches by isinstance on the mixins.

Capability detection in ``ananta.core.plugins.capabilities`` provides
``is_llm_session_source`` and ``collect_llm_session_sources``; concrete
plugins MUST be located through that path, never bare ``isinstance``.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Iterator
from typing import ClassVar

from ananta.llm.session_ledger.types import (
    ExternalSessionRef,
    NormalizedSessionEvent,
    RawSessionEvent,
    SessionSourceDescriptor,
)


class LLMSessionSourceInterface(ABC):
    """Marker ABC for any plugin that ingests LLM conversation data.

    Concrete plugins ALSO inherit one of :class:`PullingSourceMixin` /
    :class:`PushedSourceMixin`.
    """

    INTERFACE_VERSION: ClassVar[str] = "1.0.0"

    @abstractmethod
    def describe(self) -> SessionSourceDescriptor:
        """Return descriptor: source_kind, supported_modes, vendor, default lease ttl."""

    @abstractmethod
    def normalize(self, raw: RawSessionEvent) -> NormalizedSessionEvent:
        """Convert a vendor-shaped raw event to the canonical normalized form.

        Raises ``ValueError`` on unrecognized payload types. No fallback coercion.
        """


class PullingSourceMixin(ABC):
    """Sources that actively read data (filesystem, DB, blob).

    P1.1.E: every method takes a leading ``root_uri: str`` — the ``root_uri`` of
    the ``__source`` row being polled — so the walk root is per-source, NOT a
    plugin-config singleton. The importer passes ``source_row.root_uri`` into
    all four. Filesystem/sqlite plugins resolve it via
    ``ananta.llm.session_ledger.root_uri.root_uri_to_path``; plugins whose
    method genuinely does not resolve a root (pure cursor math, symbolic or
    seed-path sources) accept the param and ignore it.
    """

    @abstractmethod
    def discover_sessions(
        self,
        root_uri: str,
        cursor_payload: dict[str, object] | None,
    ) -> Iterator[ExternalSessionRef]:
        """Yield session refs newer than the cursor, under ``root_uri``.

        Implementations MUST be deterministic for a given
        ``(root_uri, cursor_payload)``. Raises ``ValueError`` on malformed
        cursor.
        """

    @abstractmethod
    def read_events(
        self,
        root_uri: str,
        session: ExternalSessionRef,
        cursor_payload: dict[str, object] | None,
    ) -> Iterator[RawSessionEvent]:
        """Yield events newer than the cursor for the given session, under ``root_uri``.

        Same determinism + ValueError contract as ``discover_sessions``.
        """

    @abstractmethod
    def session_discovery_cursor(
        self,
        root_uri: str,
        last_seen: ExternalSessionRef | None,
    ) -> dict[str, object]:
        """Produce the cursor_payload to persist after a discovery pass."""

    @abstractmethod
    def event_read_cursor(
        self,
        root_uri: str,
        session: ExternalSessionRef,
        last_event: RawSessionEvent | None,
    ) -> dict[str, object]:
        """Produce the cursor_payload to persist after an event-read pass."""


class PushedSourceMixin(ABC):
    """Sources that receive data via MCP push."""

    @abstractmethod
    def parse_chunk(self, chunk_text: str) -> Iterator[RawSessionEvent]:
        """Parse one MCP-delivered chunk into raw events.

        ``chunk_text`` is decoded UTF-8 text. The MCP boundary takes ``str`` (not
        bytes); decoding happens in the ingest endpoint before this is called.

        Raises ``ValueError`` on malformed input — no recovery, no skip.
        """


__all__ = [
    "LLMSessionSourceInterface",
    "PullingSourceMixin",
    "PushedSourceMixin",
]
