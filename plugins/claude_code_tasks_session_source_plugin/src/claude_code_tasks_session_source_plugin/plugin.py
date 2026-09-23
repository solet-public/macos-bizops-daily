"""Claude Code `~/.claude/tasks/<cli-session-uuid>/<task-id>.json` ledger source (M10).

Spec §17.3 M10 / architect v2 §6.2. Layout:

::

    <root>/
      <cli-session-uuid>/
        17.json
        18.json
        19.json
        .highwatermark     ← session-management noise, filtered by *.json glob
        .lock              ←   "
      <another-uuid>/
        1.json

Each session subdirectory is one ledger session. Filename-stem of the
SUBDIR is the `external_session_id` (same UUID space as
``IngestSourceKind.CLAUDE_CODE_LOCAL`` — when both sources land for the
same UUID, the M6 canonical-pointer dedupe handles cross-source merging
per v2 §5.4).

Each task file inside emits ONE SYSTEM event with
``content_json={"subtype": "task_state", "id": ..., "subject": ...}``
per the hybrid-extractor / subtype-lift pattern documented in
``knowledge_bases/ananta_platform/19_session_ledger_01_system_event_subtype_lift.md``.

Cursor semantics:

* Discovery cursor — ``{"mtime_high_water_iso": "<ISO>"}``. A subdir
  whose newest *.json file's mtime is strictly greater than the high
  water re-emerges; unchanged subdirs never re-emerge (idempotent
  re-poll).
* Per-session read cursor — ``{"task_index": int}``. Count of task
  files already drained from this session's subdir (lexically sorted).
  New task files added later resume at ``task_index = N``.

Idempotency: ``vendor_event_id = f"task_{filename_stem}_{task_id}"``
where filename_stem is the SUBDIR's UUID. The importer's
``_persist_normalized_for_session`` short-circuits duplicates by
``(session_id, vendor_event_id)``.

Failure policy (KB "Critical Development Guidelines v2"): any malformed JSON in a task file
raises ``ValueError`` from the parser; the importer's per-session catch
(``importer._poll_one_pulling_source``) marks the offending session as
skipped and continues. NUL strip is handled at the repository TEXT-write
seam (per `[[nul-byte-sanitization-seam]]`); the plugin emits raw text.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from datetime import UTC, datetime
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
from ananta.llm.session_ledger.vendor import claude_code_tasks as tasks_vendor

logger = logging.getLogger(__name__)

_CONFIG_GLOB = "glob"
_CURSOR_FIELD_MTIME = "mtime_high_water_iso"
_CURSOR_FIELD_TASK_INDEX = "task_index"


class ClaudeCodeTasksSessionSourcePlugin(
    PluginBase,
    LLMSessionSourceInterface,
    PullingSourceMixin,
):
    """Walks ``~/.claude/tasks/`` and surfaces each task file as a SYSTEM event."""

    def __init__(self) -> None:
        super().__init__()
        self.name = "claude_code_tasks_session_source_plugin"

    # ------------------------------------------------------------------
    # PluginBase lifecycle
    # ------------------------------------------------------------------

    def prepare_for_readiness(self) -> None:
        # root_uri resolved lazily on first discover/read; the directory
        # may not exist on a freshly-cloned operator machine.
        self.set_ready()

    def initialize(self, config: dict[str, object]) -> None:
        from ananta.core.config.config_provider import ConfigProvider  # noqa: PLC0415

        self.config_provider = ConfigProvider(self.name, config)

    # ------------------------------------------------------------------
    # LLMSessionSourceInterface
    # ------------------------------------------------------------------

    def describe(self) -> SessionSourceDescriptor:
        return SessionSourceDescriptor(
            source_kind=IngestSourceKind.CLAUDE_CODE_TASKS,
            vendor=SourceVendor.CLAUDE_CODE,
            supported_modes=(IngestMode.PULLING,),
            default_pulling_root_uri="~/.claude/tasks",
        )

    def normalize(self, raw: RawSessionEvent) -> NormalizedSessionEvent:
        payload = raw.payload
        kind = payload.get("kind")
        if kind != tasks_vendor.PAYLOAD_KIND_TASK:
            raise ValueError(
                f"claude_code tasks: unexpected payload kind {kind!r}; "
                f"only {tasks_vendor.PAYLOAD_KIND_TASK!r} is emitted by this source",
            )
        # Lift every task field into content_json so a future SQL filter
        # (`content_json::jsonb->>'subtype' = 'task_state'` plus
        # `->>'status' = 'pending'`, etc.) can find them without
        # re-parsing text. SYSTEM events have no content_text in M10 —
        # the entire task is structured.
        content_json: dict[str, object] = {
            "subtype": tasks_vendor.SUBTYPE_TASK_STATE,
            "id": payload["task_id"],
            "subject": payload["subject"],
            "description": payload["description"],
            "activeForm": payload["activeForm"],
            "status": payload["status"],
            "blocks": payload["blocks"],
            "blockedBy": payload["blockedBy"],
        }
        return NormalizedSessionEvent(
            external_session_id=raw.external_session_id,
            event_type=EventType.SYSTEM,
            role=MessageRole.SYSTEM,
            # SYSTEM events require content_text OR content_json per
            # repository._validate_system_event; content_json covers the
            # invariant.
            content_text=None,
            content_json=content_json,
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
        """Yield one ExternalSessionRef per session subdir whose contents are newer than the cursor."""
        root = self._resolve_root_uri(root_uri)
        if not root.is_dir():
            return
        high_water = _parse_cursor_dt(cursor_payload, _CURSOR_FIELD_MTIME)
        glob_pat = self._resolve_glob()
        for subdir in sorted(p for p in root.iterdir() if p.is_dir()):
            ref = _build_session_ref(subdir, glob_pat, high_water)
            if ref is not None:
                yield ref

    def read_events(
        self,
        root_uri: str,
        session: ExternalSessionRef,
        cursor_payload: dict[str, object] | None,
    ) -> Iterator[RawSessionEvent]:
        """Emit one RawSessionEvent per task file from `task_index` onwards."""
        root = self._resolve_root_uri(root_uri)
        subdir = root / session.external_session_id
        if not subdir.is_dir():
            return
        glob_pat = self._resolve_glob()
        task_files = sorted(subdir.glob(glob_pat))
        start_index = _parse_cursor_int(cursor_payload, _CURSOR_FIELD_TASK_INDEX) or 0
        for index, task_file in enumerate(task_files):
            if index < start_index:
                continue
            try:
                parsed = tasks_vendor.parse_task_file(task_file)
            except ValueError as exc:
                # Per-session catch in the importer escalates this; logged
                # here so the smoke trail is greppable on real ingest.
                logger.warning(
                    "%s: skipping malformed task file %s: %s",
                    self.name, task_file, exc,
                )
                raise
            payload = tasks_vendor.to_payload(parsed)
            # Embed the post-read task_index so event_read_cursor can
            # recover it without re-listing the directory.
            payload["_task_index"] = index + 1
            yield RawSessionEvent(
                external_session_id=session.external_session_id,
                payload=payload,
                event_at=parsed.event_at,
                vendor_event_id=f"task_{session.external_session_id}_{parsed.task_id}",
                vendor_parent_event_id=None,
            )

    def session_discovery_cursor(
        self,
        root_uri: str,
        last_seen: ExternalSessionRef | None,
    ) -> dict[str, object]:
        """Return the newest task-file mtime across the most recently discovered subdir."""
        if last_seen is None:
            return {_CURSOR_FIELD_MTIME: None}
        root = self._resolve_root_uri(root_uri)
        subdir = root / last_seen.external_session_id
        if not subdir.is_dir():
            return {_CURSOR_FIELD_MTIME: last_seen.first_seen_at.isoformat()}
        glob_pat = self._resolve_glob()
        mtimes = [m for m in (_safe_mtime(f) for f in subdir.glob(glob_pat)) if m is not None]
        if not mtimes:
            return {_CURSOR_FIELD_MTIME: last_seen.first_seen_at.isoformat()}
        return {_CURSOR_FIELD_MTIME: max(mtimes).isoformat()}

    def event_read_cursor(
        self,
        root_uri: str,
        session: ExternalSessionRef,
        last_event: RawSessionEvent | None,
    ) -> dict[str, object]:
        del root_uri, session
        if last_event is None:
            return {_CURSOR_FIELD_TASK_INDEX: 0}
        embedded = last_event.payload.get("_task_index")
        if not isinstance(embedded, int):
            raise ValueError(
                "claude_code tasks: last_event payload missing internal '_task_index'",
            )
        return {_CURSOR_FIELD_TASK_INDEX: embedded}

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _resolve_root_uri(self, root_uri: str) -> Path:
        """Resolve the polled source row's ``root_uri`` to the walk root (P1.1.E)."""
        return root_uri_to_path(root_uri)

    def _resolve_glob(self) -> str:
        provider = self.config_provider
        if provider is None:
            raise RuntimeError(
                f"{self.name}: config_provider not injected; cannot resolve glob",
            )
        raw = provider.get(_CONFIG_GLOB)
        if not isinstance(raw, str) or not raw:
            raise RuntimeError(
                f"{self.name}: yaml-default config 'glob' missing or empty",
            )
        return raw


def _build_session_ref(
    subdir: Path,
    glob_pat: str,
    high_water: datetime | None,
) -> ExternalSessionRef | None:
    """Classify one ``<subdir>`` per discovery rules; None to skip."""
    task_files = sorted(subdir.glob(glob_pat))
    if not task_files:
        return None
    mtimes = [m for m in (_safe_mtime(f) for f in task_files) if m is not None]
    if not mtimes:
        return None
    if high_water is not None and max(mtimes) <= high_water:
        return None
    return ExternalSessionRef(
        external_session_id=subdir.name,
        vendor_session_label=None,
        project_path=None,
        first_seen_at=min(mtimes),
    )


def _safe_mtime(path: Path) -> datetime | None:
    try:
        return datetime.fromtimestamp(path.stat().st_mtime, tz=UTC)
    except OSError:
        return None


def _parse_cursor_dt(
    cursor_payload: dict[str, object] | None, field: str
) -> datetime | None:
    if cursor_payload is None:
        return None
    value = cursor_payload.get(field)
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value).astimezone(UTC)
    except ValueError:
        return None


def _parse_cursor_int(
    cursor_payload: dict[str, object] | None, field: str
) -> int | None:
    if cursor_payload is None:
        return None
    value = cursor_payload.get(field)
    if not isinstance(value, int):
        return None
    return value


__all__ = ["ClaudeCodeTasksSessionSourcePlugin"]
