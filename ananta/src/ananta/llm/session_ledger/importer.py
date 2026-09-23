"""Poll loop that drives ``SessionLedgerService``.

Spec §11.1. The Importer owns the per-poll-pass orchestration:

1. List enabled sources from the repository.
2. For each pulling source: read its discovery cursor, iterate sessions,
   for each session read its event cursor, normalize, persist, advance
   cursors.
3. For each push: ``_dispatch_pushed(chunk_text)`` is called externally
   by ``SessionLedgerService.ingest_raw_chunk(...)``; we keep the same
   dispatch path so the persistence semantics are identical.

All persistence routes through ``SessionLedgerRepository``. Events always
land with full ``content_text`` — the importer never filters content at
ingest time. Future secret-identification is a periodic-search problem
(see ``workbench/2026-06-14_secretgate_full_eradication_design.md``),
not an ingest-validation problem.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, replace
from datetime import UTC, datetime

from ananta.interfaces.llm_session_source_interface import (
    LLMSessionSourceInterface,
    PullingSourceMixin,
    PushedSourceMixin,
)
from ananta.llm.session_ledger.blob_adapter import (
    BlobAdapterError,
    SessionLedgerBlobAdapter,
)
from ananta.llm.session_ledger.lease import LeaseHeartbeat
from ananta.llm.session_ledger.registry import SessionSourceRegistry
from ananta.llm.session_ledger.repository import (
    LeaseLostError,
    LedgerRepositoryError,
    PollingLeaseHandle,
    SessionLedgerRepository,
    SourceRow,
)
from ananta.llm.session_ledger.shared import (
    _strip_nuls,
    derive_event_external_id,
)
from ananta.llm.session_ledger.types import (
    CursorScope,
    EventType,
    ExternalSessionRef,
    ImportBatchStatus,
    ImporterReport,
    IngestSourceKind,
    NormalizedSessionEvent,
    RawSessionEvent,
    SourceVendor,
)

logger = logging.getLogger(__name__)

# Module-level RELOAD_SAFE marker — pure class adapter, no module-level
# mutable state, no background threads, no held service references.
RELOAD_SAFE = True


_VENDOR_FROM_SOURCE_KIND: dict[IngestSourceKind, SourceVendor] = {
    IngestSourceKind.AGENT_MESSAGING: SourceVendor.AGENT_MESSAGING,
    IngestSourceKind.CODEX_LOCAL: SourceVendor.CODEX,
    IngestSourceKind.CODEX_CLOUD: SourceVendor.CODEX,
    IngestSourceKind.CODEX_PUSHED: SourceVendor.CODEX,
    IngestSourceKind.CODEX_STATE: SourceVendor.CODEX,
    IngestSourceKind.CODEX_HISTORY: SourceVendor.CODEX,
    IngestSourceKind.CODEX_GOALS: SourceVendor.CODEX,
    IngestSourceKind.CODEX_MEMORIES: SourceVendor.CODEX,
    IngestSourceKind.CODEX_AMBIENT: SourceVendor.CODEX,
    IngestSourceKind.CLAUDE_CODE_LOCAL: SourceVendor.CLAUDE_CODE,
    IngestSourceKind.CLAUDE_CODE_CLOUD: SourceVendor.CLAUDE_CODE,
    IngestSourceKind.CLAUDE_CODE_PUSHED: SourceVendor.CLAUDE_CODE,
    IngestSourceKind.CLAUDE_CODE_HISTORY: SourceVendor.CLAUDE_CODE,
    IngestSourceKind.CLAUDE_CODE_TASKS: SourceVendor.CLAUDE_CODE,
    IngestSourceKind.CLAUDE_AI_EXPORT: SourceVendor.CLAUDE_AI,
    IngestSourceKind.CHATGPT_EXPORT: SourceVendor.CHATGPT,
}

_DEFAULT_POLLING_LEASE_TTL_SECONDS = 600
_ADOPT_RECENCY_WINDOW_MINUTES = 10

# GAP-5 idempotent ingest: the per-ingest-pass source-order occurrence counter
# for the null-``vendor_event_id`` fallback ordinal. Keyed by
# ``(session_id, event_type, role, content_key, event_at)`` so ONE counter dict
# serves both the per-session polling loop AND the multi-session push chunk loop;
# the value is the next ordinal (0-based) for that group. Re-ingest replays the
# same source order → same ordinals → the derived external_id dedups.
_OrdinalCounter = dict[tuple[str, str, str | None, str, datetime], int]


def _event_external_id(
    *,
    normalized: NormalizedSessionEvent,
    session_id: str,
    ordinals: _OrdinalCounter,
) -> str:
    """The event's ``external_id`` for the idempotent upsert (importer-side).

    ``vendor_event_id`` when present; else the deterministic ``derv:`` hash over
    the source-order occurrence ordinal. The ordinal counter increments ONLY for
    null-vendor events — vendor-present events are keyed by ``vendor_event_id``
    and must NOT consume an ordinal, so the live counting matches the slice-2
    backfill (which ranks only the null-vendor rows in each group). ``content_key``
    = the NUL-stripped ``content_text`` (?? ``""``), hashed at FULL size —
    CONTENT-addressed, NOT keyed on ``content_blob_id``: the blob id is a random
    state-generated pointer minted fresh on every (unconditional) re-offload, so
    keying on it would diverge the ``external_id`` across re-ingests of an
    OFFLOADED event and silently DUPLICATE the very re-ingest this dedups.
    ``normalized.content_text`` is still present here (the offload nulls only the
    PERSISTED row at ``ingest.py``, not ``normalized``); sha256 absorbs any size.
    Stripped ONCE here so the counter key and the hash agree. The slice-2 backfill
    recomputes the SAME key from stored ``content_text`` (inline) or the FETCHED
    blob content (offloaded).
    """
    if normalized.vendor_event_id is not None:
        return normalized.vendor_event_id
    role = normalized.role.value if normalized.role is not None else None
    content_key = _strip_nuls(normalized.content_text) or ""
    key = (session_id, normalized.event_type.value, role, content_key, normalized.event_at)
    ordinal = ordinals.get(key, 0)
    ordinals[key] = ordinal + 1
    return derive_event_external_id(
        vendor_event_id=None,
        session_id=session_id,
        event_type=normalized.event_type.value,
        role=role,
        content_key=content_key,
        event_at=normalized.event_at,
        ordinal=ordinal,
    )


class LedgerPollError(Exception):
    """Raised by :meth:`SessionLedgerImporter.poll_source` (single failure channel).

    The targeted single-source poll has exactly ONE failure channel — it raises
    — instead of the three the batch poller exposes (a silent ``(0,0,0)`` on
    lease contention, an ``ImporterReport.batches_failed`` count, and an
    in-walk exception). Raised on: lease contention, a missing / deleted /
    disabled source, a non-PULLING source, or any source-pass failure. Callers
    (the A1 export kickoff, the duplicate-source repair completeness step)
    branch on raise / no-raise.
    """


@dataclass(frozen=True, slots=True)
class PushDispatchResult:
    """Result of one :meth:`SessionLedgerImporter.dispatch_pushed` call.

    Push-specific so the real push ``batch_id`` can be surfaced without
    widening :class:`ImporterReport` (whose contract means a full poll pass).
    """

    events_persisted: int
    batch_id: str


class SessionLedgerImporter:
    """Drives one poll pass over all enabled pulling sources.

    Pushed sources are driven by ``SessionLedgerService.ingest_raw_chunk``,
    which calls :meth:`dispatch_pushed` directly. Both paths reuse
    ``_persist_normalized`` so the persistence + blob-stage semantics
    are identical.
    """

    __slots__ = (
        "_registry",
        "_repository",
        "_blob_adapter",
        "_polling_lease_ttl_seconds",
    )

    def __init__(
        self,
        *,
        registry: SessionSourceRegistry,
        repository: SessionLedgerRepository,
        blob_adapter: SessionLedgerBlobAdapter,
        polling_lease_ttl_seconds: int = _DEFAULT_POLLING_LEASE_TTL_SECONDS,
    ) -> None:
        self._registry = registry
        self._repository = repository
        self._blob_adapter = blob_adapter
        # Operator-tunable polling-lease TTL per v8 §D14.F. Read once at
        # construction so each poll pass uses a consistent value.
        self._polling_lease_ttl_seconds = polling_lease_ttl_seconds

    # ------------------------------------------------------------------
    # Public entry points
    # ------------------------------------------------------------------

    def poll_once(self) -> ImporterReport:
        """Run one poll pass over every enabled pulling source."""
        sources_polled = 0
        sessions_seen = 0
        events_persisted = 0
        batches_failed = 0
        for source_row in self._repository.list_sources(enabled_only=True):
            plugin = self._registry.get_by_kind(source_row.source_kind)
            if plugin is None or not isinstance(plugin, PullingSourceMixin):
                continue
            sources_polled += 1
            seen, persisted, failed = self._poll_one_pulling_source(
                source_row, plugin
            )
            sessions_seen += seen
            events_persisted += persisted
            batches_failed += failed
        return ImporterReport(
            sources_polled=sources_polled,
            sessions_seen=sessions_seen,
            events_persisted=events_persisted,
            batches_failed=batches_failed,
        )

    def dispatch_pushed(
        self,
        *,
        source_kind: IngestSourceKind,
        chunk_text: str,
        source_id: str | None = None,
    ) -> PushDispatchResult:
        """Drive one push payload through the same persistence path as polling.

        When ``source_id`` is given (A2 — the claude_ai export bind) the push is
        dispatched against THAT row (validated to match ``source_kind`` and be
        enabled) instead of the first-enabled-by-kind row, and the row's own
        batch is opened and returned. When ``source_id`` is ``None`` (the
        codex / claude_code shipper push) the legacy resolve-or-create path
        runs. Returns a :class:`PushDispatchResult` carrying the real push
        ``batch_id``; raises on any parse / persist failure (the batch is
        finalized FAILED first).
        """
        plugin = self._registry.require_by_kind(source_kind)
        if not isinstance(plugin, PushedSourceMixin):
            raise ValueError(
                f"source_kind {source_kind.value!r} does not support pushed mode"
            )
        source_row = _resolve_pushed_source(
            self._repository, plugin, source_kind, source_id,
        )
        # Push path is single-caller; ephemeral per-call token satisfies
        # finish_batch's contract without needing a real source lease.
        push_token = uuid.uuid4().hex
        batch_id = self._repository.start_batch(
            source_row.id, polling_lease_token=push_token,
        )
        persisted = 0
        ordinals: _OrdinalCounter = {}  # source-order fallback counter, this chunk
        try:
            for raw in plugin.parse_chunk(chunk_text):
                normalized = plugin.normalize(raw)
                persisted += self._persist_normalized(
                    source=source_row,
                    raw=raw,
                    normalized=normalized,
                    batch_id=batch_id,
                    ordinals=ordinals,
                )
            self._repository.finish_batch(
                batch_id,
                polling_lease_token=push_token,
                status=ImportBatchStatus.COMPLETED,
            )
        except ValueError as e:
            self._repository.finish_batch(
                batch_id,
                polling_lease_token=push_token,
                status=ImportBatchStatus.FAILED,
                error_message=str(e),
                error_kind="value_error",
            )
            raise
        except (LedgerRepositoryError, BlobAdapterError) as e:
            self._repository.finish_batch(
                batch_id,
                polling_lease_token=push_token,
                status=ImportBatchStatus.FAILED,
                error_message=str(e),
                error_kind=type(e).__name__,
            )
            raise
        return PushDispatchResult(events_persisted=persisted, batch_id=batch_id)

    # ------------------------------------------------------------------
    # Pulling-source orchestration
    # ------------------------------------------------------------------

    def poll_source(self, source_id: str) -> ImporterReport:
        """Poll exactly one source by id, with a single (raising) failure channel.

        Targeted counterpart to :meth:`poll_once`. Used by the synchronous
        export-ingest kickoff (A1) and the duplicate-source repair (so the
        caller can prove "one source polled, zero failed" or see a raise — not
        re-interpret a silent zero). Raises :class:`LedgerPollError` on a
        missing / deleted / disabled source, a non-PULLING source, lease
        contention, OR any source-pass failure. On success returns an
        :class:`ImporterReport` with ``sources_polled=1, batches_failed=0``.
        """
        source_row = self._repository.get_source(source_id)
        if source_row is None:
            raise LedgerPollError(
                f"poll_source: source {source_id!r} not found or deleted",
            )
        if not source_row.enabled:
            raise LedgerPollError(
                f"poll_source: source {source_id!r} is disabled",
            )
        plugin = self._registry.get_by_kind(source_row.source_kind)
        if plugin is None or not isinstance(plugin, PullingSourceMixin):
            raise LedgerPollError(
                f"poll_source: source {source_id!r} "
                f"({source_row.source_kind.value}) is not a pulling source",
            )
        handle = self._repository.try_acquire_polling_lease(
            source_row.id, ttl_seconds=self._polling_lease_ttl_seconds,
        )
        if handle is None:
            raise LedgerPollError(
                f"poll_source: source {source_id!r} lease held by another poller",
            )
        seen, persisted, batches_failed = self._drive_acquired_pulling_source(
            source_row, plugin, handle,
        )
        if batches_failed:
            raise LedgerPollError(
                f"poll_source: poll pass for source {source_id!r} "
                f"({source_row.source_kind.value}) failed; see batch error",
            )
        return ImporterReport(
            sources_polled=1,
            sessions_seen=seen,
            events_persisted=persisted,
            batches_failed=0,
        )

    def _poll_one_pulling_source(
        self,
        source_row: SourceRow,
        plugin: LLMSessionSourceInterface,
    ) -> tuple[int, int, int]:
        if not isinstance(plugin, PullingSourceMixin):  # narrowed by caller
            raise TypeError(  # pragma: no cover - defensive
                f"plugin for {source_row.source_kind.value!r} is not PullingSourceMixin"
            )
        handle = self._repository.try_acquire_polling_lease(
            source_row.id, ttl_seconds=self._polling_lease_ttl_seconds,
        )
        if handle is None:
            # Lease contention is a silent skip for the batch poller — one
            # contended source must not abort the whole pass. poll_source
            # RAISES instead (it polls exactly one source on purpose).
            logger.info(
                "ledger poll: source %s lease held by another poller; skipping",
                source_row.id,
            )
            return 0, 0, 0
        return self._drive_acquired_pulling_source(source_row, plugin, handle)

    def _drive_acquired_pulling_source(
        self,
        source_row: SourceRow,
        plugin: PullingSourceMixin,
        handle: PollingLeaseHandle,
    ) -> tuple[int, int, int]:
        """Drive one pulling source on an already-acquired lease.

        Shared by :meth:`_poll_one_pulling_source` (batch poller) and
        :meth:`poll_source` (targeted). Returns ``(seen, persisted,
        batches_failed)``; finalizes the batch FAILED + logs on any in-walk
        exception (it does NOT re-raise — the failure surfaces via the
        ``batches_failed`` count, which the two callers translate differently).
        """
        ttl_seconds = self._polling_lease_ttl_seconds
        # v8 D14.C: adopt BEFORE start_batch so route-uploaded batches
        # become this importer's owned batch atomically. The claim sets the
        # batch row's polling_lease_token to handle.lease_token, fencing
        # subsequent finish_batch calls against any stale owner.
        batch_id = self._repository.adopt_route_batch_for_source(
            source_row.id,
            polling_lease_token=handle.lease_token,
            recency_window_minutes=_ADOPT_RECENCY_WINDOW_MINUTES,
        )
        if batch_id is None:
            batch_id = self._repository.start_batch(
                source_row.id, polling_lease_token=handle.lease_token,
            )
        heartbeat = LeaseHeartbeat(self._repository, handle, ttl_seconds)
        batches_failed = 0
        try:
            seen, persisted = self._run_pulling_pass(
                source_row=source_row,
                plugin=plugin,
                batch_id=batch_id,
                heartbeat=heartbeat,
            )
        except LeaseLostError as exc:
            batches_failed = 1
            seen = persisted = 0
            _finalize_lease_lost(
                self._repository, batch_id, heartbeat, exc, source_row.id,
            )
        except ValueError as e:
            batches_failed = 1
            seen = persisted = 0
            _finalize_failed(
                self._repository, batch_id, heartbeat, e, source_row,
                "value_error",
            )
        except (LedgerRepositoryError, BlobAdapterError) as e:
            batches_failed = 1
            seen = persisted = 0
            _finalize_failed(
                self._repository, batch_id, heartbeat, e, source_row,
                type(e).__name__,
            )
        finally:
            # Owner-aware release: silent no-op when our token no longer
            # matches the persisted row (a successor has taken over).
            self._repository.release_polling_lease(heartbeat.handle)
        return seen, persisted, batches_failed

    def _run_pulling_pass(
        self,
        *,
        source_row: SourceRow,
        plugin: PullingSourceMixin,
        batch_id: str,
        heartbeat: LeaseHeartbeat,
    ) -> tuple[int, int]:
        """Walk discover_sessions + per-session event reads on a fresh batch."""
        sessions_seen = 0
        events_persisted = 0
        sessions_failed = 0
        discovery_cursor = self._repository.read_cursor(
            source_id=source_row.id, scope=CursorScope.DISCOVERY
        )
        last_session_ref = None
        for session_ref in plugin.discover_sessions(source_row.root_uri, discovery_cursor):
            heartbeat.check()
            sessions_seen += 1
            last_session_ref = session_ref
            try:
                events_persisted += self._poll_one_session(
                    source_row=source_row,
                    session_ref=session_ref,
                    batch_id=batch_id,
                    plugin=plugin,
                    heartbeat=heartbeat,
                )
            except (ValueError, LedgerRepositoryError, BlobAdapterError) as exc:
                sessions_failed += 1
                logger.warning(
                    "ledger poll: skipping session %s (%s) on %s: %s",
                    session_ref.external_session_id,
                    source_row.source_kind.value,
                    type(exc).__name__,
                    exc,
                )
                continue
        if last_session_ref is not None:
            self._repository.write_cursor(
                source_id=source_row.id,
                scope=CursorScope.DISCOVERY,
                cursor_payload=plugin.session_discovery_cursor(
                    source_row.root_uri, last_session_ref,
                ),
            )
        self._repository.finish_batch(
            batch_id,
            polling_lease_token=heartbeat.handle.lease_token,
            status=ImportBatchStatus.COMPLETED,
        )
        if sessions_failed > 0:
            logger.info(
                "ledger poll completed with %d per-session failures (source %s, kind %s)",
                sessions_failed, source_row.id, source_row.source_kind.value,
            )
        return sessions_seen, events_persisted

    # ------------------------------------------------------------------
    # Persistence helpers
    # ------------------------------------------------------------------

    def _persist_normalized(
        self,
        *,
        source: SourceRow,
        raw: RawSessionEvent,
        normalized: NormalizedSessionEvent,
        batch_id: str,
        ordinals: _OrdinalCounter,
    ) -> int:
        vendor = _VENDOR_FROM_SOURCE_KIND[source.source_kind]
        session_id = self._repository.upsert_session(
            source_id=source.id,
            external_session_id=normalized.external_session_id,
            vendor=vendor,
            source_kind=source.source_kind,
            vendor_session_label=None,
            project_path=None,
            first_event_at=raw.event_at,
            last_event_at=normalized.event_at,
        )
        return self._persist_normalized_for_session(
            session_id=session_id,
            normalized=normalized,
            batch_id=batch_id,
            session_vendor=vendor,
            source_kind=source.source_kind,
            ordinals=ordinals,
        )

    def _poll_one_session(
        self,
        *,
        source_row: SourceRow,
        session_ref: ExternalSessionRef,
        batch_id: str,
        plugin: PullingSourceMixin,
        heartbeat: LeaseHeartbeat,
    ) -> int:
        """Process events for one session; advance its event_read cursor on success.

        ``heartbeat.check()`` fires once per yielded event so a slow
        single-conversation walk extends the polling lease without depending
        on session boundaries. The check raises ``LeaseLostError`` which the
        outer ``_poll_one_pulling_source`` catches (NOT the per-session
        ``except (ValueError, LedgerRepositoryError, BlobAdapterError)``
        block — lease-loss is a batch-terminal signal).
        """
        event_cursor = self._repository.read_cursor(
            source_id=source_row.id,
            scope=CursorScope.EVENT_READ,
            scope_key=session_ref.external_session_id,
        )
        events_persisted = 0
        last_raw: RawSessionEvent | None = None
        ordinals: _OrdinalCounter = {}  # source-order fallback counter, this session walk
        for raw in plugin.read_events(source_row.root_uri, session_ref, event_cursor):
            heartbeat.check()
            last_raw = raw
            normalized = plugin.normalize(raw)
            vendor = _VENDOR_FROM_SOURCE_KIND[source_row.source_kind]
            session_id = self._repository.upsert_session(
                source_id=source_row.id,
                external_session_id=session_ref.external_session_id,
                vendor=vendor,
                source_kind=source_row.source_kind,
                vendor_session_label=session_ref.vendor_session_label,
                project_path=session_ref.project_path,
                first_event_at=session_ref.first_seen_at,
                last_event_at=normalized.event_at,
                originator_session_label=session_ref.originator_session_label,
                originator_agent_instance_id=session_ref.originator_agent_instance_id,
                recipient_session_label=session_ref.recipient_session_label,
                recipient_agent_instance_id=session_ref.recipient_agent_instance_id,
                summary_text_seed=session_ref.summary_text_seed,
            )
            # Per 2026-05-31 Architect ruling §2: single-actor sessions
            # denormalize the event's actor from the session row. The plugin
            # may also set ``normalized.actor_*`` directly (peer-thread case
            # where each event has a different sender_session_label).
            if normalized.actor_session_label is None and normalized.actor_agent_instance_id is None:
                normalized = replace(
                    normalized,
                    actor_session_label=session_ref.originator_session_label,
                    actor_agent_instance_id=session_ref.originator_agent_instance_id,
                )
            events_persisted += self._persist_normalized_for_session(
                session_id=session_id,
                normalized=normalized,
                batch_id=batch_id,
                session_vendor=vendor,
                source_kind=source_row.source_kind,
                ordinals=ordinals,
            )
        if last_raw is not None:
            self._repository.write_cursor(
                source_id=source_row.id,
                scope=CursorScope.EVENT_READ,
                cursor_payload=plugin.event_read_cursor(
                    source_row.root_uri, session_ref, last_raw,
                ),
                scope_key=session_ref.external_session_id,
            )
        return events_persisted

    def _persist_normalized_for_session(
        self,
        *,
        session_id: str,
        normalized: NormalizedSessionEvent,
        batch_id: str,
        session_vendor: SourceVendor,
        source_kind: IngestSourceKind,
        ordinals: _OrdinalCounter,
    ) -> int:
        # vendor_event_id fast-path dedup (bug 2026-05-31): a cursor that failed
        # to persist re-yields events on the next poll. KEPT alongside the GAP-5
        # (session_id, external_id) upsert: it still dedups vendor-present
        # re-ingests of LEGACY rows (null external_id until the slice-2 backfill)
        # the upsert can't yet catch; for new rows the upsert is authoritative.
        if normalized.vendor_event_id is not None:
            existing_id = self._repository.find_event_id_by_vendor_id(
                session_id=session_id,
                vendor_event_id=normalized.vendor_event_id,
            )
            if existing_id is not None:
                logger.debug(
                    "ledger persist: skipping duplicate session_id=%s vendor_event_id=%s "
                    "(existing event_id=%s)",
                    session_id, normalized.vendor_event_id, existing_id,
                )
                return 0
        content_blob_id: str | None = None
        if (
            normalized.content_text is not None
            and self._blob_adapter.should_offload_text(normalized.content_text)
        ):
            content_blob_id = self._blob_adapter.store_event_text(
                content_text=normalized.content_text,
                session_id=session_id,
                external_session_id=normalized.external_session_id,
                sequence=-1,
            )
        external_id = _event_external_id(
            normalized=normalized,
            session_id=session_id,
            ordinals=ordinals,
        )
        result = self._repository.append_event(
            session_id=session_id,
            normalized=normalized,
            batch_id=batch_id,
            content_blob_id=content_blob_id,
            session_vendor=session_vendor,
            source_kind=source_kind,
            external_id=external_id,
        )
        if result.deduped:
            # Upsert hit an existing row — event + children already persisted on
            # the first ingest; skip the child projection (no double-write).
            return 0
        _project_tool_call(
            self._repository,
            normalized=normalized,
            session_id=session_id,
            event_id=result.event_id,
        )
        return 1


def _project_tool_call(
    repository: SessionLedgerRepository,
    *,
    normalized: NormalizedSessionEvent,
    session_id: str,
    event_id: str,
) -> None:
    """Project a TOOL_CALL / TOOL_RESULT event into ``session_ledger__tool_call``.

    Module-level (mirrors :func:`_finalize_failed`) so the importer class stays a
    focused orchestrator — it takes ``repository`` rather than ``self``.
    """
    if normalized.event_type is EventType.TOOL_CALL:
        tool_name = _extract_tool_name(normalized)
        if tool_name is None:
            return
        repository.record_tool_call(
            session_id=session_id,
            call_event_id=event_id,
            tool_name=tool_name,
            called_at=normalized.event_at,
        )
        return
    if normalized.event_type is EventType.TOOL_RESULT:
        # Spec §17.3 M3 acceptance: TOOL_CALL/TOOL_RESULT projection
        # populates session_ledger__tool_call with status='succeeded'.
        # The result's vendor_parent_event_id is the tool_use_id;
        # the matching call's vendor_event_id equals that same id.
        tool_use_id = normalized.vendor_parent_event_id
        if tool_use_id is None:
            return
        call_event_id = repository.find_call_event_id_for_resolution(
            session_id=session_id,
            tool_use_vendor_id=tool_use_id,
        )
        if call_event_id is None:
            # No matching call — surface for debugging but don't fail
            # the batch; the call may yet arrive in a later poll pass
            # (out-of-order discovery on a sibling file). The status
            # will stay 'pending' until a subsequent result lands.
            logger.warning(
                "tool_result has no matching tool_call in session %s "
                "(tool_use_id=%s); leaving projection pending",
                session_id,
                tool_use_id,
            )
            return
        repository.resolve_tool_call(
            call_event_id=call_event_id,
            result_event_id=event_id,
            status="succeeded",
            resolved_at=normalized.event_at,
        )


def _extract_tool_name(normalized: NormalizedSessionEvent) -> str | None:
    content = normalized.content_json
    if not isinstance(content, dict):
        return None
    name = content.get("tool_name")
    return name if isinstance(name, str) else None


def _resolve_pushed_source(
    repository: SessionLedgerRepository,
    plugin: LLMSessionSourceInterface,
    source_kind: IngestSourceKind,
    source_id: str | None,
) -> SourceRow:
    """Resolve the ``__source`` row a push dispatches against.

    ``source_id`` given → resolve THAT row and validate it matches
    ``source_kind`` + is enabled (A2 export bind — avoids dispatching against
    the wrong row when more than one exists). ``source_id`` None → legacy
    first-enabled-by-kind resolve-or-create (the shipper push path).
    """
    if source_id is None:
        return _resolve_or_create_pushed_source(repository, plugin, source_kind)
    row = repository.get_source(source_id)
    if row is None:
        raise ValueError(
            f"dispatch_pushed: source {source_id!r} not found or deleted",
        )
    if row.source_kind is not source_kind:
        raise ValueError(
            f"dispatch_pushed: source {source_id!r} is "
            f"{row.source_kind.value!r}, not {source_kind.value!r}",
        )
    if not row.enabled:
        raise ValueError(
            f"dispatch_pushed: source {source_id!r} is disabled",
        )
    return row


def _resolve_or_create_pushed_source(
    repository: SessionLedgerRepository,
    plugin: LLMSessionSourceInterface,
    source_kind: IngestSourceKind,
) -> SourceRow:
    for row in repository.list_sources(enabled_only=True):
        if row.source_kind is source_kind:
            return row
    descriptor = plugin.describe()
    source_id = repository.insert_source(
        source_kind=source_kind,
        root_uri=f"pushed:{source_kind.value}",
        account_label=descriptor.vendor.value,
    )
    row = repository.get_source(source_id)
    if row is None:
        raise LedgerRepositoryError(
            f"failed to read back inserted source {source_id}",
        )
    return row


def _finalize_lease_lost(
    repository: SessionLedgerRepository,
    batch_id: str,
    heartbeat: LeaseHeartbeat,
    exc: LeaseLostError,
    source_id: str,
) -> None:
    """Finalize a batch the heartbeat declared lease-lost.

    Caller is the outer ``_poll_one_pulling_source`` exception handler;
    owner-aware ``finish_batch`` is what makes the late-finish safe.
    """
    finished = repository.finish_batch(
        batch_id,
        polling_lease_token=heartbeat.handle.lease_token,
        status=ImportBatchStatus.FAILED,
        error_message=str(exc),
        error_kind="lease_lost",
    )
    if not finished:
        # v8 invariant: this path is unreachable for adopted/owned batches
        # because a successor cannot adopt a batch whose token != NULL. Log
        # defensively if it ever fires.
        logger.warning(
            "ledger poll: lease lost on source %s; finish_batch "
            "returned no rows for batch %s (rare race; likely no harm)",
            source_id, batch_id,
        )
    logger.warning(
        "ledger poll: lease lost during walk for source %s batch %s; "
        "aborted (other poller continues)",
        source_id, batch_id,
    )


def _finalize_failed(
    repository: SessionLedgerRepository,
    batch_id: str,
    heartbeat: LeaseHeartbeat,
    exc: Exception,
    source_row: SourceRow,
    error_kind: str,
) -> None:
    """Finalize a batch terminated by an in-walk exception (non-lease)."""
    repository.finish_batch(
        batch_id,
        polling_lease_token=heartbeat.handle.lease_token,
        status=ImportBatchStatus.FAILED,
        error_message=str(exc),
        error_kind=error_kind,
    )
    logger.exception(
        "%s during poll of source %s (%s)",
        error_kind,
        source_row.id,
        source_row.source_kind.value,
    )


def utcnow_iso() -> str:
    """Helper for log timestamps. Importer is otherwise clock-free."""
    return datetime.now(UTC).isoformat(timespec="seconds")


__all__ = ["SessionLedgerImporter"]
