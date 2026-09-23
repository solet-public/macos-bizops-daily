"""Concrete :class:`SessionLedgerService` — thin facade over the foundation modules.

Spec §11.1 / §14.9. Composition:

* ``SessionSourceRegistry`` — discovered via ``collect_llm_session_sources``.
* ``SessionLedgerRepository`` — SQL adapter over ``state_service``.
* ``SessionLedgerBlobAdapter`` — large-content / attachment staging via
  ``blob_storage_service``.
* ``SessionLedgerImporter`` — poll loop + push dispatch.

The service implements its split ABCs (``SessionLedgerReadAPI`` /
``SessionLedgerIngestAPI`` /
``SessionLedgerPollingDriverAPI`` / ``SessionLedgerCanonicalPointerRepairAPI``
/ ``SessionLedgerInvertedBoundsRepairAPI`` / ``SessionLedgerDeploymentAPI`` /
``SessionLedgerSearchAPI`` / plus the backfill ABCs) directly, and two more
(``SessionLedgerSummarizeAPI``, ``SessionLedgerEmbeddingDrainAPI``) via the
per-ABC-family mixins in ``summarize.py`` / ``embedding_drain.py`` (schema-debt
service.py decomposition seam, 2026-08-07 — mirrors the repository layer's own
mixin split) — all via multiple inheritance, constructed once
by ``startup_sequence._init_session_ledger_service`` after the platform
state + blob wrappers exist and the source plugins have been started.

"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from ananta.llm.session_ledger.blob_adapter import SessionLedgerBlobAdapter
from ananta.llm.session_ledger.event_embeddings import (
    EventEmbeddingServicesUnavailableError,
    EventEmbeddingWriter,
)
from ananta.llm.session_ledger.importer import SessionLedgerImporter
from ananta.llm.session_ledger.registry import SessionSourceRegistry
from ananta.llm.session_ledger.repository import (
    LedgerRepositoryError,
    SessionLedgerRepository,
)
from ananta.llm.session_ledger.root_uri import (
    canonicalize_root_uri_for_storage,
    normalize_root_uri,
)
from ananta.llm.session_ledger.summarization import (
    SearchResultEnvelope,
    SummaryServicesUnavailableError,
    SummaryWriter,
)
from ananta.llm.session_ledger.trigger_data import extract_authenticated_principal
from ananta.llm.session_ledger.types import (
    IngestSourceKind,
    SessionsOrderBy,
    SourceVendor,
)
from ananta.services.session_ledger_service.blob_identity_backfill import (
    ExportBlobIdentityBackfill,
)
from ananta.services.session_ledger_service.duplicate_source_repair import (
    check_duplicate_source_quiesced,
    insert_source_or_absorb_race,
    resolve_duplicate_source_pair,
)
from ananta.services.session_ledger_service.embedding_drain import (
    SessionLedgerEmbeddingDrainMixin,
)
from ananta.services.session_ledger_service.enforcement import (
    assert_operator_principal,
    assert_register_source_authorized,
)
from ananta.services.session_ledger_service.interfaces.public import (
    SessionLedgerCanonicalPointerRepairAPI,
    SessionLedgerDeploymentAPI,
    SessionLedgerEventExternalIdBackfillAPI,
    SessionLedgerEventSourceDenormBackfillAPI,
    SessionLedgerIngestAPI,
    SessionLedgerInvertedBoundsRepairAPI,
    SessionLedgerPollingDriverAPI,
    SessionLedgerQualificationAPI,
    SessionLedgerReadAPI,
    SessionLedgerSearchAPI,
    SessionLedgerSessionSourceKindBackfillAPI,
)
from ananta.services.session_ledger_service.periodic_cron import (
    extract_schedule_id,
)
from ananta.services.session_ledger_service.poll_drain import (
    start_importer_poll_drain,
)
from ananta.services.session_ledger_service.selected_sources import (
    SelectedSourceRecord,
    SelectedSourceRecordError,
    load_selected_source_record,
    selected_and_consented_source_kinds,
)
from ananta.services.session_ledger_service.selected_sources import (
    qualify_selected_sources as build_selected_sources_qualification,
)
from ananta.services.session_ledger_service.summarize import (
    SessionLedgerSummarizeMixin,
)
from ananta.services.session_ledger_service.summary_executor import (
    BoundedSummaryExecutor,
    SummaryExecutor,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from ananta.core.plugins.plugin_manager import PluginManager
    from ananta.core.services.call_context import CallContext
    from ananta.interfaces.blob_storage_service_interface import (
        BlobStorageServiceInterface,
    )
    from ananta.interfaces.embedding_service_interface import EmbeddingServiceInterface
    from ananta.interfaces.state_management_interface import StateManagementInterface
    from ananta.interfaces.vector_service_interface import VectorServiceInterface

logger = logging.getLogger(__name__)

# GAP-5 slice 3 — ``reset_ingest_state`` is now a NON-DESTRUCTIVE cursor reset.
# It clears every source's ``__source_cursor`` rows so the next poll replays
# each source and the live ``(session_id, external_id)`` upsert reconverges; no
# content is deleted. ``_RESET_ACTION`` namespaces the new semantics in the verb
# result (no magic string); ``_PRESERVED_CONTENT_TABLES`` are the data tables the
# reset KEEPS — their live counts are surfaced on the dry-run so the operator can
# see the content the reset preserves (in contrast to the pre-GAP-5 hard-delete).
_RESET_ACTION = "cursor_reset_replay"


def _selected_source_failure(reason: str) -> dict[str, Any]:
    """Return the fail-closed public shape when manager proof cannot load."""

    return {
        "record_source": "manager_transaction",
        "target_identity_matched": False,
        "answers_fingerprint_matched": False,
        "qualification_reason": reason,
        "sources": [],
    }


def _registered_source_kinds(source_rows: object) -> set[str]:
    """Extract only durable registered source kinds from public source rows."""

    if not isinstance(source_rows, list):
        return set()
    return {
        source_kind
        for row in source_rows
        if isinstance(row, dict)
        and isinstance(row.get("source_id"), str)
        and isinstance((source_kind := row.get("source_kind")), str)
    }


def _backfill_counts(census_rows: object) -> dict[str, int]:
    """Extract bounded per-kind session counts from the ledger census."""

    if not isinstance(census_rows, list):
        return {}
    return {
        source_kind: session_count
        for row in census_rows
        if isinstance(row, dict)
        and isinstance((source_kind := row.get("source_kind")), str)
        and isinstance((session_count := row.get("session_count")), int)
    }


def _retrieval_by_selected_kind(
    service: Any,
    record: SelectedSourceRecord,
    registered_source_kinds: set[str],
) -> dict[str, bool]:
    """Probe one bounded event window only for trusted, registered selections."""

    return {
        source_kind: bool(
            service.list_events_by_source_window(source_kind=source_kind, limit=1).get(
                "events"
            )
        )
        for source_kind in selected_and_consented_source_kinds(record)
        if source_kind in registered_source_kinds
    }
_PRESERVED_CONTENT_TABLES = (
    "session_ledger__session",
    "session_ledger__event",
    "session_ledger__tool_call",
    "session_ledger__attachment",
    "session_ledger__import_batch",
)

# System-owned scheduler identifiers for the periodic-poll cron. Mirrors the
# pattern at default_scheduling_plugin.constants.HEARTBEAT_FLOW_ID — the
# scheduler-fired action_definition needs a stable flow_id/session_id at fire
# time (action_factory._enforce_flow_id refuses an absent flow_id with no
# context fallback). Distinct from the heartbeat constants per Architect
# 2026-05-31 so the periodic-poll fires are independently attributable in
# audit logs.
_LEDGER_PERIODIC_POLL_FLOW_ID = "flow-ledger-periodic-poll"
_LEDGER_PERIODIC_POLL_SESSION_ID = "sess-ledger-periodic-poll"


class SessionLedgerService(
    SessionLedgerReadAPI,
    SessionLedgerQualificationAPI,
    SessionLedgerIngestAPI,
    SessionLedgerPollingDriverAPI,
    SessionLedgerCanonicalPointerRepairAPI,
    SessionLedgerInvertedBoundsRepairAPI,
    SessionLedgerEventExternalIdBackfillAPI,
    SessionLedgerEventSourceDenormBackfillAPI,
    SessionLedgerSessionSourceKindBackfillAPI,
    SessionLedgerSummarizeMixin,
    SessionLedgerDeploymentAPI,
    SessionLedgerEmbeddingDrainMixin,
    SessionLedgerSearchAPI,
):
    """Thin delegation facade — every public method maps to one repository call.

    No business logic in the service itself beyond:
    * argument coercion (str → enum / ISO timestamp → datetime),
    * envelope shaping (dict[str, list[...]] return type), and
    * principal extraction for authz-bearing methods.

    Importer + repository do all heavy lifting.
    """

    __slots__ = (
        "_registry",
        "_repository",
        "_state_service",
        "_blob_adapter",
        "_importer",
        "_summary_writer",
        "_event_embedding_writer",
        "_operator_equivalent_check",
        "_scheduling_service",
        "_inference_service",
        "_ledger_allowed_roots",
    )

    def __init__(
        self,
        *,
        state_service: StateManagementInterface,
        blob_storage_service: BlobStorageServiceInterface,
        plugin_manager: PluginManager,
        embedding_service: EmbeddingServiceInterface | None = None,
        vector_service: VectorServiceInterface | None = None,
        scheduling_service: Any = None,
        inference_service: Any = None,
        summary_executor: SummaryExecutor | None = None,
        polling_lease_ttl_seconds: int = 600,
        ledger_allowed_roots: list[str] | None = None,
    ) -> None:
        self._state_service = state_service
        # P1.1.E authz containment: filesystem ``root_uri`` registrations
        # through the public ``register_source`` verb must be contained under
        # one of these operator-configured roots. Empty = deny every filesystem
        # registration (secure default); the export/pushed/boot paths use the
        # trusted internal seam and are unaffected.
        self._ledger_allowed_roots: list[str] = list(ledger_allowed_roots or [])
        self._repository = SessionLedgerRepository(state_service)
        self._blob_adapter = SessionLedgerBlobAdapter(blob_storage_service)
        self._registry = SessionSourceRegistry(plugin_manager.plugins)
        # v8 §D14.F: polling-lease TTL is operator-tunable via the
        # session_ledger_service plugin config (default 600s).
        self._importer = SessionLedgerImporter(
            registry=self._registry,
            repository=self._repository,
            blob_adapter=self._blob_adapter,
            polling_lease_ttl_seconds=polling_lease_ttl_seconds,
        )
        self._summary_writer, self._event_embedding_writer = _build_semantic_writers(
            repository=self._repository,
            embedding_service=embedding_service,
            vector_service=vector_service,
        )
        # M5 §13.3: bridge plugin wires this to vault.is_operator_equivalent
        # after the vault registry is available (M5.C). Default None means
        # only direct ownership-binding admits approve_pairing.
        self._operator_equivalent_check: Callable[[str], bool] | None = None
        # M5.C deferral #4: scheduling_service is bound via service_bindings
        # and looked up at startup. ``ensure_periodic_poll_schedule`` raises
        # cleanly if this is None (e.g., a profile that omits scheduling).
        self._scheduling_service = scheduling_service
        # 2026-05-31 Gap 2(A): inference_service for auto-summarize cron.
        # ``summarize_quiescent_sessions`` raises cleanly if None.
        self._inference_service = inference_service
        # Singleton drain guard — full contract on the ABC docstring of
        # ``summarize_quiescent_sessions``: one background drainer across
        # overlapping cron fires (second ``submit`` no-ops); injectable for
        # tests. Phase 5 will route drain inference through the resolver.
        self._summary_executor, self._embedding_executor, self._poll_executor = _build_drain_executors(summary_executor)
        logger.debug(
            "session_ledger_service constructed: %d source plugin(s) registered",
            len(self._registry.list_sources()),
        )

    # ------------------------------------------------------------------
    # Inspection helpers (used by startup smoke and operator diagnostics)
    # ------------------------------------------------------------------

    @property
    def registry(self) -> SessionSourceRegistry:
        return self._registry

    @property
    def blob_storage_service(self) -> BlobStorageServiceInterface:
        """The bound blob_storage_service wrapper — used by startup smoke."""
        return self._blob_adapter.blob_storage_service

    @property
    def importer(self) -> SessionLedgerImporter:
        """Exposed for the operator-bridge poll trigger and integration smoke."""
        return self._importer

    # ------------------------------------------------------------------
    # Read surface
    # ------------------------------------------------------------------

    def qualify_selected_sources(
        self,
        target: str,
        name: str,
        answers_fingerprint: str,
    ) -> dict[str, Any]:
        """Qualify only manager-recorded choices against bounded ledger observations."""

        target_path = Path(target)
        try:
            record = load_selected_source_record(target=target_path, name=name)
        except SelectedSourceRecordError as exc:
            return _selected_source_failure(str(exc))
        registered_source_kinds = _registered_source_kinds(self.list_sources().get("sources"))
        backfill_counts = _backfill_counts(self.census().get("sources"))
        retrieval_by_kind = _retrieval_by_selected_kind(
            self, record, registered_source_kinds
        )
        return build_selected_sources_qualification(
            target=target_path,
            name=name,
            requested_answers_fingerprint=answers_fingerprint,
            record=record,
            registered_source_kinds=registered_source_kinds,
            backfill_counts=backfill_counts,
            retrieval_by_kind=retrieval_by_kind,
        )

    def list_sources(self) -> dict[str, Any]:
        """Return registered ingest sources joined with their plugin descriptors.

        Per Coordinator Q1 ruling 2026-05-31 Option A: each result entry
        carries both the descriptor surface (source_kind, vendor,
        supported_modes, default_lease_ttl_seconds, default_pulling_root_uri)
        AND the ``session_ledger__source`` DB row surface (source_id, root_uri,
        account_label, enabled, config_json), matched on ``source_kind``.

        Three shapes appear:

        * Plugin loaded AND row registered → one entry per (source_kind,
          root_uri) row, all fields populated.
        * Plugin loaded, no row registered yet → one descriptor-only entry
          (DB fields null) so the operator can see what *could* be registered.
        * Row registered, no plugin loaded → one DB-only entry (descriptor
          fields null) so the operator sees the orphaned row.
        """
        descriptors = self._registry.list_sources()
        rows = self._repository.list_sources(enabled_only=False)
        descriptor_by_kind = {d.source_kind: d for d in descriptors}
        rows_by_kind: dict[IngestSourceKind, list[Any]] = {}
        for row in rows:
            rows_by_kind.setdefault(row.source_kind, []).append(row)
        sources: list[dict[str, Any]] = []
        for descriptor in descriptors:
            matching_rows = rows_by_kind.get(descriptor.source_kind, [])
            if not matching_rows:
                sources.append(_descriptor_only_entry(descriptor))
                continue
            for row in matching_rows:
                sources.append(_joined_entry(descriptor, row))
        for kind, kind_rows in rows_by_kind.items():
            if kind in descriptor_by_kind:
                continue
            for row in kind_rows:
                sources.append(_row_only_entry(row))
        return {"sources": sources}

    def census(self) -> dict[str, Any]:
        """SQL-aggregated read-only inventory of the whole ledger.

        One call answers "which sources, how much content, any duplicate source
        rows, any orphan batches" over the entire corpus — the read APIs clamp
        ``limit`` and cannot count a ~1M-event corpus. Per source it returns
        counts (sessions split canonical/sibling, events, tool_calls), batch
        health (owned-running vs unclaimed-route, oldest-running age), the
        normalized ``root_uri``, and the order-independent row-identity
        fingerprint. ``duplicate_source_groups`` counts ``(source_kind,
        normalized root_uri)`` keys with more than one live row.
        """
        rows = self._repository.census_source_rows()
        sources: list[dict[str, Any]] = []
        group_counts: dict[tuple[str, str], int] = {}
        totals = {
            "sessions": 0, "events": 0, "tool_calls": 0,
            "owned_running_batches": 0, "unclaimed_route_batches": 0,
        }
        for row in rows:
            normalized = normalize_root_uri(str(row["root_uri"]))
            fingerprint_a = row.get("fingerprint_a")
            fingerprint_b = row.get("fingerprint_b")
            fingerprint = (
                f"{fingerprint_a}:{fingerprint_b}"
                if fingerprint_a is not None
                else None
            )
            age_raw = row.get("oldest_running_batch_age_seconds")
            sources.append({
                "source_id": row["source_id"],
                "source_kind": row["source_kind"],
                "root_uri": row["root_uri"],
                "normalized_root_uri": normalized,
                "session_count": int(row["session_count"]),  # type: ignore[arg-type]
                "canonical_count": int(row["canonical_count"]),  # type: ignore[arg-type]
                "sibling_count": int(row["sibling_count"]),  # type: ignore[arg-type]
                "event_count": int(row["event_count"]),  # type: ignore[arg-type]
                "tool_call_count": int(row["tool_call_count"]),  # type: ignore[arg-type]
                "row_identity_fingerprint": fingerprint,
                "owned_running_batches": int(row["owned_running_batches"]),  # type: ignore[arg-type]
                "unclaimed_route_batches": int(row["unclaimed_route_batches"]),  # type: ignore[arg-type]
                "oldest_running_batch_age_seconds": (
                    int(age_raw) if age_raw is not None else None  # type: ignore[arg-type]
                ),
            })
            key = (str(row["source_kind"]), normalized)
            group_counts[key] = group_counts.get(key, 0) + 1
            totals["sessions"] += int(row["session_count"])  # type: ignore[arg-type]
            totals["events"] += int(row["event_count"])  # type: ignore[arg-type]
            totals["tool_calls"] += int(row["tool_call_count"])  # type: ignore[arg-type]
            totals["owned_running_batches"] += int(row["owned_running_batches"])  # type: ignore[arg-type]
            totals["unclaimed_route_batches"] += int(row["unclaimed_route_batches"])  # type: ignore[arg-type]
        return {
            "sources": sources,
            "source_count": len(sources),
            "duplicate_source_groups": sum(1 for n in group_counts.values() if n > 1),
            "total_sessions": totals["sessions"],
            "total_events": totals["events"],
            "total_tool_calls": totals["tool_calls"],
            "total_owned_running_batches": totals["owned_running_batches"],
            "total_unclaimed_route_batches": totals["unclaimed_route_batches"],
        }

    def list_sessions(
        self,
        limit: int = 50,
        since: datetime | str | None = None,
        until: datetime | str | None = None,
        first_event_since: datetime | str | None = None,
        first_event_until: datetime | str | None = None,
        project_path: str | None = None,
        vendor: SourceVendor | str | None = None,
        source_kind: IngestSourceKind | str | None = None,
        order_by: SessionsOrderBy | str = SessionsOrderBy.LAST_EVENT_AT_DESC,
        include_siblings: bool = False,
    ) -> dict[str, Any]:
        # M17 §2.2: ABC declares enum-typed params; the action_processor's
        # JSON-channel passes strings, so coerce at the service-entry seam.
        # First-party Python callers can pass typed enums directly and the
        # isinstance check is a no-op.
        # W5.B §3.7.A: include_siblings flows through to the repository's
        # canonical-only-by-default filter + EXISTS-over-canonical-group
        # source_kind handling (Codex C2 cross-source dedupe correction).
        rows = self._repository.list_sessions(
            limit=limit,
            since=_coerce_datetime(since),
            until=_coerce_datetime(until),
            first_event_since=_coerce_datetime(first_event_since),
            first_event_until=_coerce_datetime(first_event_until),
            project_path=project_path,
            vendor=_coerce_enum(vendor, SourceVendor),
            source_kind=_coerce_enum(source_kind, IngestSourceKind),
            order_by=_coerce_enum(order_by, SessionsOrderBy)
            or SessionsOrderBy.LAST_EVENT_AT_DESC,
            include_siblings=include_siblings,
        )
        return {"sessions": rows}

    def list_active_sessions(self) -> dict[str, Any]:
        return {"sessions": self._repository.list_active_sessions()}

    def get_session_timeline(
        self,
        session_id: str,
        after_sequence: int = 0,
        limit: int = 100,
    ) -> dict[str, Any]:
        events = self._repository.get_session_timeline(
            session_id=session_id,
            after_sequence=after_sequence,
            limit=limit,
        )
        return {"events": events}

    def list_tool_calls(
        self,
        session_id: str | None = None,
        tool_name: str | None = None,
        status: str | None = None,
        since_iso: str | None = None,
        limit: int = 50,
    ) -> dict[str, Any]:
        since = _parse_iso(since_iso) if since_iso is not None else None
        rows = self._repository.list_tool_calls(
            session_id=session_id,
            tool_name=tool_name,
            status=status,
            since=since,
            limit=limit,
        )
        return {"tool_calls": rows}

    def list_canonical_contributors(self, session_id: str) -> dict[str, Any]:
        """W5.B §3.3 provenance projection wrapper.

        Delegates to the repository's CTE+ (vendor, external_session_id)
        SELECT. Per Codex C3 the envelope carries three top-level fields
        plus the ``contributors`` list (canonical-input + sibling-input +
        orphaned-canonical cases handled in the repository layer) — those
        three top-level fields pass through unchanged, but each
        contributor's ``first_event_at`` / ``last_event_at`` is serialized
        here: the repository layer deliberately returns NAIVE datetimes
        (matching the pre-migration raw ``_fetch_all`` return type — see
        ``list_canonical_contributors_migration_smoke.py::
        test_datetime_return_type_parsed_back``, a locked-in Python-internal
        contract this wrapper must not disturb), but a raw ``datetime`` hits
        the EDGE process's JSON envelope with no serialization step of its
        own. ``_naive_utc_to_iso`` makes the naive-for-comparison (repository)
        vs aware-for-output (this seam) split explicit rather than leaving a
        caller to guess which one it has.
        """
        result = self._repository.list_canonical_contributors(
            session_id=session_id,
        )
        contributors = cast("list[dict[str, Any]]", result["contributors"])
        result["contributors"] = [
            {
                **row,
                "first_event_at": _naive_utc_to_iso(cast("datetime", row["first_event_at"])),
                "last_event_at": _naive_utc_to_iso(cast("datetime", row["last_event_at"])),
            }
            for row in contributors
        ]
        return result

    def get_import_status(self, batch_id: str) -> dict[str, Any]:
        row = self._repository.get_import_status(batch_id)
        if row is None:
            raise LedgerRepositoryError(f"no import batch with id {batch_id!r}")
        return row

    # ------------------------------------------------------------------
    # Write surface
    # ------------------------------------------------------------------

    def register_source(
        self,
        source_kind: str,
        root_uri: str,
        account_label: str | None = None,
        config_json: dict[str, Any] | None = None,
        *,
        call_context: CallContext | None = None,
    ) -> dict[str, Any]:
        """Register an ingest source; idempotent on ``(source_kind, root_uri)``.

        On hit, returns the existing row with ``outcome='existed'`` — never
        re-INSERTs. On miss, inserts a fresh row with
        ``outcome='registered'``. Mirrors the schema description's
        documented invariant (``session_ledger__source`` is
        one-row-per-``(source_kind, root_uri)``).

        ``account_label`` / ``config_json`` are applied only when a new row is
        inserted; an existing row's values are NOT overwritten. Operators who
        need to update those fields use a future ``update_source`` verb
        (out of M1 scope).

        **Authorization (P1.1.E).** A **filesystem** ``root_uri`` requires an
        operator/operator-equivalent ``call_context`` AND containment under a
        configured ``ledger_allowed_roots`` entry — because once the pulling
        plugins honor a per-source ``root_uri`` this verb can point the ledger
        at arbitrary local files. Blob-id / pushed / symbolic ``root_uri``
        values (exports/pushed) skip the path check. The boot auto-register and
        the export verbs use the trusted internal seam
        (:meth:`_register_source_internal`) and are NOT subject to this gate.

        NOTE: field name is ``outcome`` not ``action`` because the platform's
        result-contract validator treats a top-level ``action`` key as
        action_status; ``existed``/``registered`` are not valid ActionStatus
        values and trip ``result_status_not_completed`` (same pitfall the
        ``ensure_periodic_poll_schedule`` comment above flags for
        ``status``). 2026-05-31: Coordinator traced the ledger boot
        cascade to this exact footgun.
        """
        assert_register_source_authorized(
            root_uri=root_uri,
            call_context=call_context,
            allowed_roots=self._ledger_allowed_roots,
        )
        return self._register_source_internal(
            source_kind=source_kind,
            root_uri=root_uri,
            account_label=account_label,
            config_json=config_json,
        )

    def _register_source_internal(
        self,
        *,
        source_kind: str,
        root_uri: str,
        account_label: str | None = None,
        config_json: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Idempotent registration seam — check-then-insert, absorb-on-race.

        NOT a ``@service_interface`` verb. ``root_uri`` is canonicalized so
        ``~/x`` / ``file:///x`` / a symlink alias collapse to one row. The
        insert step's race handling is in
        :func:`duplicate_source_repair.insert_source_or_absorb_race`.
        """
        kind = IngestSourceKind(source_kind)
        canonical_root_uri = canonicalize_root_uri_for_storage(root_uri)
        existing_id = self._repository.find_source_id_by_kind_and_root_uri(
            source_kind=kind, root_uri=canonical_root_uri,
        )
        if existing_id is not None:
            return {"source_id": existing_id, "outcome": "existed"}
        return insert_source_or_absorb_race(
            self._repository,
            kind=kind,
            canonical_root_uri=canonical_root_uri,
            account_label=account_label,
            config_json=config_json,
        )

    def ingest_raw_chunk(
        self,
        source_kind: str,
        chunk_text: str,
        *,
        source_id: str | None = None,
    ) -> dict[str, Any]:
        kind = IngestSourceKind(source_kind)
        result = self._importer.dispatch_pushed(
            source_kind=kind, chunk_text=chunk_text, source_id=source_id,
        )
        return {
            "events_persisted": result.events_persisted,
            "batch_id": result.batch_id,
        }

    def poll_source(self, source_id: str) -> dict[str, Any]:
        """Poll one source by id synchronously; raise on any failure.

        Service-layer delegate for :meth:`SessionLedgerImporter.poll_source`
        (single raising failure channel). Used by the synchronous export-ingest
        kickoff (A1) and the duplicate-source repair completeness step — both
        need "one source polled, zero failed" to be a hard guarantee, not a
        re-interpreted silent zero. Raises
        :class:`~ananta.llm.session_ledger.importer.LedgerPollError`.
        """
        report = self._importer.poll_source(source_id)
        return {
            "sources_polled": report.sources_polled,
            "sessions_seen": report.sessions_seen,
            "events_persisted": report.events_persisted,
            "batches_failed": report.batches_failed,
        }

    def trigger_poll(self) -> dict[str, Any]:
        """Cron heartbeat: (re)start the singleton importer-poll drainer (full contract on the ABC)."""
        return start_importer_poll_drain(self._poll_executor, self._importer)

    def ensure_periodic_poll_schedule(
        self,
        cadence_minutes: int = 5,
        tag: str = "ledger:periodic_poll",
    ) -> dict[str, Any]:
        """Idempotently install a cron that fires trigger_poll every N minutes.

        Mirrors :meth:`scheduling_service.ensure_global_heartbeat`: clear by
        tag, then create one fresh cron schedule whose action invokes
        ``service_interface::session_ledger_service::trigger_poll``.

        Fails fast if scheduling_service was not bound at construction (a
        profile that omits scheduling has no business booting this verb).
        """
        if self._scheduling_service is None:
            raise RuntimeError(
                "ensure_periodic_poll_schedule requires scheduling_service "
                "to be bound at session_ledger_service construction",
            )
        if not 1 <= int(cadence_minutes) <= 59:
            raise ValueError(
                f"cadence_minutes must be between 1 and 59 (got {cadence_minutes})",
            )
        cron_expression = f"*/{int(cadence_minutes)} * * * *"

        clear_result = self._scheduling_service.clear_scheduled_actions_by_tag(
            tag=tag,
        )
        cleared_count = int(
            (clear_result or {}).get("data", {}).get("cleared_count", 0)
        ) if isinstance(clear_result, dict) else 0

        # The cron-fired action is TERMINAL/HEADLESS per the canonical
        # scheduler cron-action contract enforced at ``create_cron_schedule``
        # registration (see the scheduler cron-action-contract design
        # record, dev-checkout workbench — not part of the shipped tree,
        # for the validator design + the canonical KB article at
        # ``knowledge_bases/ananta_platform/21_scheduling_service/
        # 01_template_flow_record_lifecycle.md``): the action runs
        # synchronously, returns its dict envelope, and exits at
        # ``action_queue_poller._dispatch_*`` via the EDGE_SINK_SKIP branch
        # (``result_processor_kind is None and result_processor is None`` →
        # terminal action, no dispatch). No ``result_processor_kind``
        # declared at schedule level; no ``result_processor_customizations``
        # on the action definition (see ``interfaces/public.py``
        # ``trigger_poll`` ABC). No ``core.flows`` preseed needed — the
        # inference scaffold's ``<<get_flow_input>>`` macro never runs for a
        # terminal-skip action.
        create_result = self._scheduling_service.create_cron_schedule(
            cron_expression=cron_expression,
            actions=[{
                "process_key": "service_interface::session_ledger_service::trigger_poll",
                "arguments": {},
            }],
            label="Ledger periodic poll",
            tags=[tag],
            # Schedule-level flow_id/session_id are propagated into the
            # fired action_definition by action_executor._apply_context_to_definition;
            # action_factory._enforce_flow_id refuses an absent flow_id with no
            # context fallback. Use system-owned identifiers so the cron does
            # not couple to any caller session (mirrors heartbeat pattern;
            # bug detected 2026-05-31 against `dispatch:in_flight:ledger_e2e_functional_bugs`).
            state={
                "flow_id": _LEDGER_PERIODIC_POLL_FLOW_ID,
                "session_id": _LEDGER_PERIODIC_POLL_SESSION_ID,
            },
        )
        # The plugin wraps its envelope as {"data": {...}, "action_status": ...};
        # the service-interface call surface may also return the raw inner
        # data dict. Accept both shapes by extracting schedule_id from
        # whichever level it lives at.
        schedule_id = extract_schedule_id(create_result)

        outcome = "normalized" if cleared_count > 0 else "created"
        logger.info(
            "session_ledger periodic poll schedule %s: schedule_id=%s tag=%s cadence=%dm",
            outcome, schedule_id, tag, cadence_minutes,
        )
        # NOTE: field name is ``outcome`` not ``status`` because the platform's
        # result-status invariant treats a top-level ``status`` key as the
        # action_status field; ``created``/``normalized`` are not valid
        # ActionStatus values and trip ``result_status_not_completed``.
        # Match the pattern in sibling methods (trigger_poll, ingest_raw_chunk,
        # register_source) which return raw inner dicts without a top-level
        # ``status`` field.
        return {
            "outcome": outcome,
            "schedule_id": schedule_id,
            "tag": tag,
            "cadence_minutes": int(cadence_minutes),
            "cleared_count": cleared_count,
        }

    def reset_ingest_state(self, confirm: bool = False) -> dict[str, Any]:
        """Reset every source's ingest cursor so the next poll replays + reconverges.

        GAP-5 slice 3 (idempotent-ingest design §4) — the verb is now
        NON-DESTRUCTIVE. It clears each source's ``__source_cursor`` rows (via
        the shipped per-source :meth:`reset_source_cursor`); the next poll pass
        re-walks every source from the start and the live
        ``(session_id, external_id)`` upsert dedups the replayed events, so the
        ledger reconverges WITHOUT deleting any content. All content tables
        (``session``/``event``/``tool_call``/``attachment``/``import_batch``)
        and the leases are PRESERVED — replay + upsert makes the old wipe
        unnecessary, and historical rows stay intact + forensically available.

        ``confirm=False`` (default) returns a dry-run: the active cursor count
        that WOULD be cleared per source, plus the live row counts of the
        content tables the reset PRESERVES (so the operator sees the data that
        is KEPT, not deleted). ``confirm=True`` performs the cursor reset and
        returns the per-source cleared-cursor counts.

        NOTE — this does NOT repopulate or correct existing rows. Under the
        landed DO-NOTHING upsert a replayed event that already exists is a
        no-op, so the pre-GAP-5 "reset + restart to re-walk and repopulate
        schema columns" recovery use case is RETIRED: reset only dedups; it
        never rewrites the rows it replays.

        PRECONDITION (``confirm=True``): every event must already carry a
        non-null ``external_id``. A legacy null-``external_id`` row is NOT
        covered by the ``(session_id, external_id)`` unique (NULLs are DISTINCT
        in Postgres), so a post-reset re-walk would derive a non-null id that
        does not conflict with it and INSERT A DUPLICATE. The verb therefore
        REFUSES (raises ``ValueError``) while any null-``external_id`` events
        remain — run ``backfill_event_external_ids`` to 0 nulls first. The
        ``confirm=False`` dry-run reports ``null_external_id_count`` +
        ``precondition_met`` so the operator can see this before committing.
        """
        null_external_id_count = self._repository.count_events_missing_external_id()
        if confirm and null_external_id_count > 0:
            raise ValueError(
                f"reset_ingest_state refused: {null_external_id_count} legacy "
                "event(s) still have a null external_id; run "
                "backfill_event_external_ids to 0 nulls first, else a post-reset "
                "re-walk would DUPLICATE them (NULLs are DISTINCT in the "
                "(session_id, external_id) unique, so a re-derived non-null id "
                "would not conflict with the null legacy row).",
            )
        sources = self._repository.list_sources(enabled_only=False)
        per_source: list[dict[str, object]] = []
        active_before = 0
        cleared_total = 0
        for source in sources:
            before = (
                self._repository.reset_source_cursor(source.id)
                if confirm
                else self._repository.count_active_source_cursors(source.id)
            )
            cleared = before if confirm else 0
            active_before += before
            cleared_total += cleared
            per_source.append(
                {
                    "source_id": source.id,
                    "active_cursor_count_before": before,
                    "deleted_count": cleared,
                },
            )
        result: dict[str, Any] = {
            "confirmed": confirm,
            "action": _RESET_ACTION,
            "content_preserved": True,
            "sources_total": len(sources),
            "active_cursor_count_before": active_before,
            "deleted_count": cleared_total,
            "per_source": per_source,
        }
        if confirm:
            logger.warning(
                "session_ledger reset_ingest_state confirmed (NON-DESTRUCTIVE "
                "cursor reset): cleared %d cursor row(s) across %d source(s); "
                "content preserved — next poll replays + the "
                "(session_id, external_id) upsert reconverges",
                cleared_total, len(sources),
            )
        else:
            counts = self._repository.count_rows_per_table(_PRESERVED_CONTENT_TABLES)
            result["preserved_content"] = [
                {"table": table, "rows_preserved": counts.get(table, 0)}
                for table in _PRESERVED_CONTENT_TABLES
            ]
            result["null_external_id_count"] = null_external_id_count
            result["precondition_met"] = null_external_id_count == 0
        return result

    def lift_canonical_pointer_for_duplicate_sessions(
        self,
        confirm: bool = False,
        tag: str = "ledger:periodic_poll",
        cadence_minutes: int = 5,
    ) -> dict[str, Any]:
        """One-shot pre-flight repair for the M18 partial-unique index landing.

        Mirrors the try/finally pause-resume pattern of
        :meth:`backfill_first_last_event_at_repair`: pauses the importer-
        poll cron so no concurrent poll INSERTs a third canonical sibling
        into a duplicate group mid-repair, then per-group picks the
        chronologically-first canonical survivor and demotes its siblings.
        """
        if not 1 <= int(cadence_minutes) <= 59:
            raise ValueError(
                f"cadence_minutes must be between 1 and 59 (got {cadence_minutes})",
            )
        duplicate_group_count = self._repository.count_canonical_duplicate_sessions()
        if not confirm:
            return {
                "confirmed": False,
                "duplicate_group_count": duplicate_group_count,
                "demoted_count": 0,
                "pause_tag": tag,
                "resume_outcome": "skipped",
            }
        if self._scheduling_service is None:
            raise RuntimeError(
                "lift_canonical_pointer_for_duplicate_sessions requires "
                "scheduling_service to be bound at "
                "session_ledger_service construction (to pause+resume "
                "the importer-poll cron around the repair)",
            )
        demoted_count = 0
        resume_outcome = "skipped"
        try:
            self._scheduling_service.clear_scheduled_actions_by_tag(tag=tag)
            demoted_count = (
                self._repository.lift_canonical_pointer_for_duplicate_sessions()
            )
            logger.info(
                "lift_canonical_pointer_for_duplicate_sessions: "
                "duplicate_group_count=%d demoted_count=%d",
                duplicate_group_count, demoted_count,
            )
        finally:
            resume_result = self.ensure_periodic_poll_schedule(
                cadence_minutes=int(cadence_minutes),
                tag=tag,
            )
            resume_outcome = str(resume_result.get("outcome", ""))
        return {
            "confirmed": True,
            "duplicate_group_count": duplicate_group_count,
            "demoted_count": demoted_count,
            "pause_tag": tag,
            "resume_outcome": resume_outcome,
        }

    def backfill_first_last_event_at_repair(
        self,
        confirm: bool = False,
        tag: str = "ledger:periodic_poll",
        cadence_minutes: int = 5,
    ) -> dict[str, Any]:
        """One-shot repair for ``__session`` rows with inverted event-at bounds.

        After the upsert path's ``LEAST/GREATEST`` fix landed, any rows already
        inverted from pre-fix ingestion need a one-time recompute from the
        canonical ``MIN(event_at)`` / ``MAX(event_at)`` over their
        ``__event`` rows. Wraps the repair in a try/finally pause-resume
        envelope around the importer-poll cron so a partial-loop exception
        still re-ensures ingest.
        """
        if not 1 <= int(cadence_minutes) <= 59:
            raise ValueError(
                f"cadence_minutes must be between 1 and 59 (got {cadence_minutes})",
            )
        inverted_count = self._repository.count_inverted_first_last_event_at_sessions()
        if not confirm:
            return {
                "confirmed": False,
                "inverted_count": inverted_count,
                "repaired_count": 0,
                "pause_tag": tag,
                "resume_outcome": "skipped",
            }
        if self._scheduling_service is None:
            raise RuntimeError(
                "backfill_first_last_event_at_repair requires "
                "scheduling_service to be bound at "
                "session_ledger_service construction (to pause+resume "
                "the importer-poll cron around the repair)",
            )
        repaired_count = 0
        resume_outcome = "skipped"
        try:
            self._scheduling_service.clear_scheduled_actions_by_tag(tag=tag)
            repaired_count = self._repository.repair_inverted_first_last_event_at()
            logger.info(
                "backfill_first_last_event_at_repair: "
                "inverted_count=%d repaired_count=%d",
                inverted_count, repaired_count,
            )
        finally:
            # MUST run even on exception so the importer-poll cron resumes.
            resume_result = self.ensure_periodic_poll_schedule(
                cadence_minutes=int(cadence_minutes),
                tag=tag,
            )
            resume_outcome = str(resume_result.get("outcome", ""))
        return {
            "confirmed": True,
            "inverted_count": inverted_count,
            "repaired_count": repaired_count,
            "pause_tag": tag,
            "resume_outcome": resume_outcome,
        }

    def backfill_summary_embedding_vector_ids(
        self,
        confirm: bool = False,
    ) -> dict[str, Any]:
        """One-shot repair for pre-fix ``__summary.embedding_vector_id`` rows.

        Per commit ``4ea5eda81`` (2026-06-10): pre-fix ``_store_vector``
        returned the pgvector internal id; persist_summary wrote that into
        ``embedding_vector_id``. The ANN search join keys by
        ``embedding.external_id`` so the IN clause never matched and
        ``search_sessions`` silently returned ``[]``. This verb rewrites
        each stale pointer to the deterministic ``{session_id}:{chunk_index}``
        external_id, recomputed ledger-side from each ``__summary`` row's own
        columns (SQL-lockdown Slice 3b — no pgvector access, no cross-table
        join). Idempotent: post-fix pointers no longer carry the internal
        ``emb-`` prefix the repair targets.

        No cron pause needed: post-fix writes carry external_id-shaped
        pointers that don't carry the internal ``emb-`` prefix, so any
        concurrent M6 cron firing is invisible to the repair.
        """
        broken_count = (
            self._repository.count_summary_rows_with_pgvector_internal_id_pointer()
        )
        if not confirm:
            return {
                "confirmed": False,
                "updated_count": 0,
                "skipped_count": broken_count,
                "total_rows_now_correct": 0,
            }
        result = self._repository.repair_summary_embedding_vector_ids()
        logger.info(
            "backfill_summary_embedding_vector_ids: "
            "broken_count=%d updated_count=%d skipped_count=%d "
            "total_rows_now_correct=%d",
            broken_count,
            int(result.get("updated_count", 0)),
            int(result.get("skipped_count", 0)),
            int(result.get("total_rows_now_correct", 0)),
        )
        return {
            "confirmed": True,
            "updated_count": int(result.get("updated_count", 0)),
            "skipped_count": int(result.get("skipped_count", 0)),
            "total_rows_now_correct": int(result.get("total_rows_now_correct", 0)),
        }

    def backfill_orphan_running_batches_for_source(
        self,
        source_id: str,
        source_kind: str | None = None,
        confirm: bool = False,
        stale_threshold_seconds: int = 86400,
    ) -> dict[str, Any]:
        """One-shot repair for stale orphan ``__import_batch`` rows on one source.

        Cycle 4b D11. Operator-facing diagnostic verb that targets the
        orphan-running-batch class first surfaced by the chatgpt upload
        route + the Wave-4a crash-after-claim path: batches whose owner
        never terminated them. Any running batch older than
        ``stale_threshold_seconds`` (default 24 h) is marked failed with
        ``error_kind='orphan_repair'``.

        Per Codex impl note: this verb does NOT filter on
        ``polling_lease_token``, so token-owned batches whose owner
        crashed after claim are still reachable.

        Use ``confirm=True`` to actually rewrite rows. Default
        ``confirm=False`` returns a structured dry-run with the orphan
        counts so the operator can size the repair before committing.
        """
        if not confirm:
            return {
                "confirmed": False,
                "repaired_count": 0,
                "untouched_count": 0,
                "total_orphan_count_before": 0,
                "source_id": source_id,
            }
        result = self._repository.backfill_orphan_running_batches_for_source(
            source_id,
            source_kind=source_kind,
            stale_threshold_seconds=int(stale_threshold_seconds),
        )
        logger.info(
            "backfill_orphan_running_batches_for_source: "
            "source_id=%s source_kind=%s "
            "total_orphan_count_before=%d repaired_count=%d untouched_count=%d",
            source_id, source_kind,
            int(result.get("total_orphan_count_before", 0)),
            int(result.get("repaired_count", 0)),
            int(result.get("untouched_count", 0)),
        )
        return {
            "confirmed": True,
            "repaired_count": int(result.get("repaired_count", 0)),
            "untouched_count": int(result.get("untouched_count", 0)),
            "total_orphan_count_before": int(
                result.get("total_orphan_count_before", 0)
            ),
            "source_id": source_id,
        }

    def reset_source_cursor(
        self,
        source_id: str,
        confirm: bool = False,
    ) -> dict[str, Any]:
        """Hard-delete every active cursor row for one source.

        Cycle 4b D13. Operator recovery verb for the case where a
        source's discovery/event-read cursors have advanced past a point
        the operator wants to re-walk from (chatgpt-export ZIP re-walk
        being the immediate case). Hard-delete (not soft) per the operator's
        soft-delete-is-opt-out principle — a cursor reset has no recovery
        path, and ``write_cursor`` re-inserts a fresh row when none exists.

        Use ``confirm=True`` to actually delete. Default ``confirm=False``
        returns a structured dry-run with the active-cursor count so the
        operator can size the reset before committing.

        Idempotent: re-running on a source with no active cursors
        returns ``deleted_count=0``.

        PRECONDITION (``confirm=True``): every event must already carry a
        non-null ``external_id`` — the same exposure as
        :meth:`reset_ingest_state`. Re-walking a source with legacy
        null-``external_id`` events would DUPLICATE them (NULLs are DISTINCT
        in the ``(session_id, external_id)`` unique), so the verb REFUSES
        (raises ``ValueError``) while any remain — run
        ``backfill_event_external_ids`` to 0 nulls first. The dry-run reports
        ``null_external_id_count`` + ``precondition_met``. (The guard lives on
        this SERVICE verb — the direct operator path — NOT the repository
        method, so ``reset_ingest_state``'s loop, which calls the repository
        method and already guards globally upfront, is not double-guarded.)
        """
        null_external_id_count = self._repository.count_events_missing_external_id()
        if confirm and null_external_id_count > 0:
            raise ValueError(
                f"reset_source_cursor refused: {null_external_id_count} legacy "
                "event(s) still have a null external_id; run "
                "backfill_event_external_ids to 0 nulls first, else a re-walk "
                "would DUPLICATE them (NULLs are DISTINCT in the "
                "(session_id, external_id) unique, so a re-derived non-null id "
                "would not conflict with the null legacy row).",
            )
        if not confirm:
            active_count = self._repository.count_active_source_cursors(source_id)
            return {
                "confirmed": False,
                "deleted_count": 0,
                "source_id": source_id,
                "active_cursor_count_before": active_count,
                "null_external_id_count": null_external_id_count,
                "precondition_met": null_external_id_count == 0,
            }
        deleted_count = self._repository.reset_source_cursor(source_id)
        logger.info(
            "reset_source_cursor: source_id=%s deleted_count=%d",
            source_id, deleted_count,
        )
        return {
            "confirmed": True,
            "deleted_count": deleted_count,
            "source_id": source_id,
        }

    def retire_duplicate_source(
        self,
        winner_source_id: str,
        loser_source_id: str,
        confirm: bool = False,
    ) -> dict[str, Any]:
        """Retire a duplicate ``source`` row into its canonical winner.

        Schema-debt-external-id lane, 2b-S1 (2026-08-06). Full contract
        (refusal conditions, quiesce protocol, soft-delete rationale) is on
        the ABC declaration (``interfaces/public.py``) and in the
        schema-debt external-id findings record (dev-checkout workbench —
        not part of the shipped tree) — not repeated here to keep this
        concrete implementation lean.
        """
        _winner, loser = resolve_duplicate_source_pair(
            self._repository, winner_source_id, loser_source_id,
        )
        children_before = self._repository.count_source_children(loser_source_id)
        if not confirm:
            return {
                "confirmed": False,
                "winner_source_id": winner_source_id,
                "loser_source_id": loser_source_id,
                "children_to_move": children_before,
                "loser_enabled": loser.enabled,
            }
        check_duplicate_source_quiesced(
            self._repository, loser, loser_source_id,
        )
        moved = self._repository.repoint_source_children(
            loser_source_id=loser_source_id, winner_source_id=winner_source_id,
        )
        remaining = self._repository.count_source_children(loser_source_id)
        if any(remaining.values()):
            raise LedgerRepositoryError(
                f"retire_duplicate_source: {remaining} child row(s) still "
                f"reference {loser_source_id!r} after re-point — refusing to "
                "soft-delete a non-orphaned source row",
            )
        retired = self._repository.retire_source_row(loser_source_id)
        logger.info(
            "retire_duplicate_source: winner=%s loser=%s moved=%r retired=%d",
            winner_source_id, loser_source_id, moved, retired,
        )
        return {
            "confirmed": True,
            "winner_source_id": winner_source_id,
            "loser_source_id": loser_source_id,
            "children_moved": moved,
            "loser_retired": retired == 1,
        }

    def set_source_enabled(
        self,
        source_id: str,
        enabled: bool,
    ) -> dict[str, Any]:
        """Toggle one source row's ``enabled`` flag. Full contract on the ABC
        declaration (``interfaces/public.py``).
        """
        result = self._repository.set_source_enabled(source_id, enabled)
        if result is None:
            raise ValueError(
                f"set_source_enabled: source {source_id!r} not found or deleted",
            )
        return {
            "source_id": source_id,
            "prior_enabled": result["prior_enabled"],
            "new_enabled": result["new_enabled"],
            "changed": result["changed"],
        }

    def list_events_by_source_window(
        self,
        source_kind: str | None = None,
        since: str | None = None,
        until: str | None = None,
        limit: int = 20,
        vendor: str | None = None,
    ) -> dict[str, Any]:
        """List events of a source kind / vendor in an optional event_at window.

        Cycle 4b D14b. Operator-facing diagnostic verb. Returns events
        ordered by ``event_at DESC`` (newest first) bounded by ``limit``
        (clamped to ``[1, 100]`` — SQL-lockdown Slice 7 narrowed the former
        200 to the ``query_ordered`` ≤100 cap). This verb has NO content-text
        predicate so callers can list a source kind's recent events without
        a search term.

        ``source_kind`` is the ``IngestSourceKind`` StrEnum value
        (codex_local / claude_code_local / chatgpt_export /
        agent_messaging / ...). ``since`` and ``until`` are ISO-8601
        timestamps (UTC). ``vendor``, when supplied, narrows to one
        ``SourceVendor`` (codex / claude_code / chatgpt /
        agent_messaging). SQL-lockdown Slice 7 (Architect-ruled denormalize):
        the read is a single-table ``query_ordered`` over ``__event`` using
        the ``session_vendor`` + ``source_kind`` columns denormalized onto
        each event — no JOIN.

        W5.E §5.5 G5 signature relaxation: all three filter args
        (``source_kind``, ``since``, ``until``) are now optional. At
        least one of ``source_kind`` / ``vendor`` MUST be supplied —
        unbounded scans across the entire ledger are not the verb's
        contract. When ``until`` is omitted, defaults to
        ``datetime.now(UTC)`` at the service-layer entry so vendor-only
        audit pulls return the most recent events without an explicit
        upper bound.
        """
        if source_kind is None and vendor is None:
            raise ValueError(
                "list_events_by_source_window: at least one of "
                "source_kind / vendor must be supplied (the verb's "
                "scope is intentional; unbounded ledger scans are not "
                "supported here)."
            )
        since_dt = _parse_iso(since) if since else None
        until_dt = _parse_iso(until) if until else datetime.now(UTC)
        rows = self._repository.list_events_by_source_window(
            source_kind=source_kind,
            since=since_dt,
            until=until_dt,
            limit=int(limit),
            vendor=vendor,
        )
        return {
            "events": [_public_event_envelope(row) for row in rows],
        }

    def backfill_event_source_denormalization(self) -> dict[str, Any]:
        """One-shot operator backfill of the Slice-7 ``__event`` denorm columns.

        Thin delegation to
        :meth:`SessionLedgerRepository.backfill_event_source_denormalization`.
        No pause-resume envelope (unlike the repair verbs): once the Slice-7
        ingest write-path landed, every NEW event is born with ``session_vendor``
        set, so the backfill's ``session_vendor IS NULL`` filter only ever
        touches pre-migration rows and never overlaps a live writer. Idempotent
        + fail-loud (see the repository docstring); returns
        ``{sessions_scanned, events_denormalized}``.
        """
        return self._repository.backfill_event_source_denormalization()

    def backfill_event_external_ids(self) -> dict[str, Any]:
        """One-shot operator backfill of ``__event.external_id`` on legacy rows.

        Thin delegation to
        :meth:`SessionLedgerRepository.backfill_event_external_ids`. Stamps the
        GAP-5 idempotent-ingest ``external_id`` on every pre-slice-1 null-
        ``external_id`` row, reproducing the live importer's derivation so
        historical re-ingest dedups. The service supplies ``fetch_blob_text`` from
        its blob adapter so OFFLOADED rows recompute the content-addressed key
        from the stored blob. Idempotent + fill-only (no ``confirm`` gate);
        skip-and-count on a live-window collision. Returns
        ``{sessions_scanned, events_stamped, collisions_skipped}``.
        """
        return self._repository.backfill_event_external_ids(
            fetch_blob_text=self._blob_adapter.fetch_event_text,
        )

    def backfill_session_source_kinds(self) -> dict[str, Any]:
        """One-shot operator backfill of the list_sessions source_kind junction.

        Thin delegation to
        :meth:`SessionLedgerRepository.backfill_session_source_kinds`. Populates
        the ``session_source_kind`` junction for pre-migration session groups so
        ``list_sessions(source_kind=K)`` finds historical sessions (the ingest
        attach-path maintains it going forward). Idempotent + additive (DO-NOTHING
        on the UNIQUE pair) so no pause-resume envelope is needed; fail-loud on a
        group with no canonical (run ``lift_canonical_pointer_for_duplicate_
        sessions`` first) or a missing source. Returns
        ``{sessions_scanned, junction_rows_written}``.
        """
        return self._repository.backfill_session_source_kinds()

    # ------------------------------------------------------------------
    # M5 — shipper bootstrap / pairing (spec §13)
    # ------------------------------------------------------------------

    def generate_ingest_setup(
        self,
        operating_system: str,
        sources: list[str],
        machine_id: str,
        state: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Create a pending shipper deployment row (spec §13 step 1).

        ``state`` carries the authenticated_principal; the caller's
        client_id becomes ``initiating_client_id`` for the §13.3
        ownership-binding check on ``approve_pairing``.

        The ``operating_system`` parameter is currently informational
        (operator-side install machinery materializes per-OS file
        artifacts; v1 ships the deployment registration only). The
        installer-package render path is M5 follow-on.
        """
        if state is None:
            raise PermissionError(
                "generate_ingest_setup requires server-injected state with "
                "authenticated_principal — refusing to act on caller-supplied identity"
            )
        principal = extract_authenticated_principal(state)
        # Validate sources up front so the deployment row carries only
        # IngestSourceKind values. Codex item 10: each is bound to the
        # deployment and later checked at ingest_blob / ingest_raw_chunk.
        validated: list[str] = []
        for source_kind in sources:
            kind = IngestSourceKind(source_kind)
            validated.append(kind.value)
        if not validated:
            raise ValueError("generate_ingest_setup requires non-empty sources")
        if not machine_id:
            raise ValueError("generate_ingest_setup requires non-empty machine_id")
        # operating_system kept for forward-compat with the installer
        # renderer (M5 follow-on); v1 doesn't materialize templates yet.
        _ = operating_system
        deployment_id = self._repository.insert_pending_deployment(
            machine_id=machine_id,
            initiating_client_id=principal.client_id,
            authorized_source_kinds=validated,
        )
        logger.info(
            "ledger pairing: deployment created deployment_id=%s "
            "initiating_client_id=%s sources=%s",
            deployment_id, principal.client_id, validated,
        )
        return {
            "deployment_id": deployment_id,
            "machine_id": machine_id,
            "authorized_source_kinds": validated,
        }

    def approve_pairing(
        self,
        deployment_id: str,
        user_code: str,
        state: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Ownership-bound transition pending→approved (spec §13.3)."""
        if state is None:
            raise PermissionError(
                "approve_pairing requires server-injected state with "
                "authenticated_principal — refusing to act on caller-supplied identity"
            )
        principal = extract_authenticated_principal(state)
        row = self._repository.get_deployment(deployment_id)
        if row is None:
            raise LedgerRepositoryError(
                f"no deployment with id {deployment_id!r}"
            )
        if row.get("pairing_status") != "pending":
            raise LedgerRepositoryError(
                f"deployment {deployment_id!r} is not in pending state "
                f"(got {row.get('pairing_status')!r})"
            )
        stored_user_code = row.get("user_code")
        if not isinstance(stored_user_code, str) or stored_user_code != user_code:
            raise PermissionError(
                f"approve_pairing: user_code mismatch for deployment {deployment_id!r}"
            )
        initiating = row.get("initiating_client_id")
        is_operator_eq = self._is_caller_operator_equivalent(principal.client_id)
        if principal.client_id != initiating and not is_operator_eq:
            raise PermissionError(
                f"approve_pairing forbidden: caller client_id {principal.client_id!r} "
                f"does not match deployment.initiating_client_id and is not "
                "operator_equivalent (§13.3)"
            )
        self._repository.transition_deployment_to_approved(
            deployment_id=deployment_id
        )
        logger.info(
            "ledger pairing: deployment approved deployment_id=%s by client_id=%s",
            deployment_id, principal.client_id,
        )
        return {"status": "approved", "deployment_id": deployment_id}

    def shipper_self_revoke(
        self,
        state: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Revoke the caller's own deployment (spec §14.1 pin 2).

        Target is server-derived from the authenticated principal's
        client_id — the handler accepts NO caller-supplied deployment_id.
        """
        if state is None:
            raise PermissionError(
                "shipper_self_revoke requires server-injected state with "
                "authenticated_principal — refusing to act on caller-supplied identity"
            )
        principal = extract_authenticated_principal(state)
        row = self._repository.get_deployment_by_oauth_client_id(
            principal.client_id
        )
        if row is None:
            raise PermissionError(
                f"shipper_self_revoke: no paired deployment for caller "
                f"client_id {principal.client_id!r}"
            )
        deployment_id = str(row["id"])
        self._repository.transition_deployment_to_revoked(
            deployment_id=deployment_id
        )
        logger.info(
            "ledger pairing: deployment revoked deployment_id=%s by self "
            "(client_id=%s)",
            deployment_id, principal.client_id,
        )
        return {"status": "revoked", "deployment_id": deployment_id}

    # ------------------------------------------------------------------
    # M6 — semantic search / summary intake (spec §17.6)
    # ------------------------------------------------------------------

    def push_session_summary_chunk(
        self,
        session_id: str,
        chunk_index: int,
        summary_text: str,
        state: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Embed + store-vector + insert a summary chunk row.

        ``state`` carries the authenticated_principal; the caller's
        client_id is persisted as ``generated_by_client_id`` (spec §8.10
        NOT NULL).
        """
        if state is None:
            raise PermissionError(
                "push_session_summary_chunk requires server-injected state with "
                "authenticated_principal — refusing to act on caller-supplied identity",
            )
        principal = extract_authenticated_principal(state)
        try:
            result = self._summary_writer.push_summary_chunk(
                session_id=session_id,
                chunk_index=chunk_index,
                summary_text=summary_text,
                generated_by_client_id=principal.client_id,
            )
        except SummaryServicesUnavailableError as exc:
            raise RuntimeError(
                "push_session_summary_chunk: M6 services unavailable in this profile",
            ) from exc
        return {
            "status": "completed",
            "summary_id": result["summary_id"],
            "embedding_vector_id": result["embedding_vector_id"],
            "chunk_index": result["chunk_index"],
        }

    def search_sessions(
        self,
        query: str,
        limit: int = 10,
        vendor: SourceVendor | str | None = None,
        source_kind: IngestSourceKind | str | None = None,
        since: datetime | str | None = None,
    ) -> dict[str, Any]:
        """Top-k summaries joined to sessions; re-scans content on the way out (M17 §2.2).

        M17 adds vendor / source_kind / since filters. The ANN search
        returns candidate envelopes; this impl post-filters them against
        the joined session row's vendor + source_kind + last_event_at.
        The post-filter passes through M18 §3.4's COALESCE seam (only
        canonical rows have summaries, so the result set is dedup'd by
        construction).
        """
        bounded_limit = max(1, min(int(limit), 50))
        vendor_typed = _coerce_enum(vendor, SourceVendor)
        source_kind_typed = _coerce_enum(source_kind, IngestSourceKind)
        since_typed = _coerce_datetime(since)
        try:
            envelopes = self._summary_writer.search(
                query=query, limit=bounded_limit,
            )
        except SummaryServicesUnavailableError as exc:
            raise RuntimeError(
                "search_sessions: M6 services unavailable in this profile",
            ) from exc
        if vendor_typed is not None or source_kind_typed is not None or since_typed is not None:
            envelopes = _filter_envelopes(
                envelopes,
                vendor=vendor_typed,
                source_kind=source_kind_typed,
                since=since_typed,
            )
        return {
            "results": [
                {
                    "session_id": env.session_id,
                    "chunk_index": env.chunk_index,
                    "summary_text": env.summary_text,
                    "score": env.score,
                    "session": env.session,
                }
                for env in envelopes
            ],
        }

    def search_event_content(
        self,
        query: str,
        limit: int = 10,
    ) -> dict[str, Any]:
        """ANN search over event-content embeddings (LED-01).

        Chunk-granular search over the raw USER/ASSISTANT message text in
        the ``session_ledger_event`` vector namespace — the content-level
        complement to ``search_sessions``' summary-level search. Only
        already-embedded events are reachable (the producer below fills the
        namespace). Results reuse the ``list_events_by_source_window``
        public event envelope plus ``chunk_index`` + ``score``.
        """
        bounded_limit = max(1, min(int(limit), 50))
        try:
            hits = self._event_embedding_writer.search(
                query=query, limit=bounded_limit,
            )
        except EventEmbeddingServicesUnavailableError as exc:
            raise RuntimeError(
                "search_event_content: embedding/vector services "
                "unavailable in this profile",
            ) from exc
        return {
            "results": [
                {
                    **_public_event_envelope(hit),
                    "chunk_index": hit.get("chunk_index"),
                    "score": hit.get("score"),
                }
                for hit in hits
            ],
        }

    def embed_missing_event_content(
        self,
        batch_limit: int = 25,
    ) -> dict[str, Any]:
        """Embed a bounded batch of not-yet-embedded events (LED-01 producer).

        Thin delegation to
        :meth:`EventEmbeddingWriter.embed_missing_events` with the verb's
        ``[1, 200]`` clamp. Synchronous within this call (bounded batch +
        ``is_long_running`` on the declaration); the caller loops while the
        returned ``exhausted`` is false.
        """
        bounded_batch_limit = max(1, min(int(batch_limit), 200))
        try:
            return self._event_embedding_writer.embed_missing_events(
                batch_limit=bounded_batch_limit,
            )
        except EventEmbeddingServicesUnavailableError as exc:
            raise RuntimeError(
                "embed_missing_event_content: embedding/vector services "
                "unavailable in this profile",
            ) from exc

    def event_embedding_coverage(self) -> dict[str, Any]:
        """Read-only LED-01 event-embedding coverage (frontier + chunk count).

        Thin delegation to :meth:`EventEmbeddingWriter.coverage`. Works even
        without embedding/vector bindings — an absent vector_service yields a
        0 ``embedded_chunk_count`` rather than failing, so the frontier read
        (cursor vs newest in-scope arrival) is always available.
        """
        return self._event_embedding_writer.coverage()

    def _is_caller_operator_equivalent(self, client_id: str) -> bool:
        """Dispatch through the bridge-wired callback.

        Production: the bridge plugin sets the callback via
        :meth:`set_operator_equivalent_check` once the vault registry
        is available. Until set (M1 default), returns False — only
        direct ownership-binding (caller.client_id ==
        initiating_client_id) admits an approve_pairing call.
        """
        if self._operator_equivalent_check is None:
            return False
        return bool(self._operator_equivalent_check(client_id))

    def set_operator_equivalent_check(
        self, callback: Callable[[str], bool],
    ) -> None:
        """Production wiring hook for the vault is_operator_equivalent lookup."""
        self._operator_equivalent_check = callback

    # ------------------------------------------------------------------
    # M4 — ChatGPT export (kept here from M4)
    # ------------------------------------------------------------------

    def register_chatgpt_export_source(
        self, *, blob_id: str, account_label: str | None
    ) -> dict[str, str]:
        """Register an uploaded export blob and start its running batch.

        Spec §10.10.1 step 5. Idempotent on the blob id (A1): routes through
        the trusted internal registration seam (so a re-upload of the same
        export reuses the existing ``session_ledger__source`` row, not a fresh
        one) and reuses the open ROUTE batch instead of orphaning a new running
        batch per call. Returns ``{"source_id": str, "batch_id": str}``.
        """
        registered = self._register_source_internal(
            source_kind=IngestSourceKind.CHATGPT_EXPORT.value,
            root_uri=blob_id,
            account_label=account_label,
            config_json=None,
        )
        source_id = registered["source_id"]
        # Per v8 D14.D: route batches carry polling_lease_token=NULL because the
        # HTTP handler holds no source lease. They are adoptable by any
        # importer's adopt_route_batch_for_source call. Idempotent (A1): reuse
        # the running route batch rather than orphan a fresh one per upload.
        batch_id = self._repository.ensure_open_route_batch_for_source(source_id)
        return {"source_id": source_id, "batch_id": batch_id}

    def register_claude_ai_export_source(
        self, *, blob_id: str, account_label: str | None
    ) -> dict[str, str]:
        """Register an uploaded Claude.ai export blob (M9; A2 asymmetric).

        Spec §10.10.1 step 5. ``claude_ai_export`` is a **PUSHED** source, so —
        unlike the PULLING ``register_chatgpt_export_source`` — this verb opens
        **no** batch: the push (``ingest_raw_chunk`` → ``dispatch_pushed``) owns
        and finalizes its own batch. Routes through the trusted internal
        registration seam (idempotent on the blob id) and returns
        ``{"source_id": str}`` only. The plugin's ``_trigger_import`` then binds
        the push to this ``source_id`` and surfaces the real push ``batch_id``.
        """
        registered = self._register_source_internal(
            source_kind=IngestSourceKind.CLAUDE_AI_EXPORT.value,
            root_uri=blob_id,
            account_label=account_label,
            config_json=None,
        )
        return {"source_id": registered["source_id"]}

    def backfill_export_blob_identity(
        self, confirm: bool = False, *, call_context: CallContext | None = None,
    ) -> dict[str, Any]:
        """Converge export blobs onto content-digest identity (A3; operator-only).

        ``confirm=False`` previews counts and mutates nothing. ``confirm=True``
        tags every referenced export blob with its durable ``kind`` (Phase 0),
        keys each blob by content digest / repoints each source onto the
        content-canonical blob (Phase 1), then deletes the now-unreferenced
        export blobs (Phase 2). Operator-only — it mutates blob metadata,
        repoints ``__source`` rows, and deletes blobs. **Forward-only**: once a
        confirmed run mutates, recovery is re-run-to-reconverge, not revert.

        **Sequencing guardrail (do NOT run standalone).** The Phase-1 repoint
        deliberately makes a duplicate source's ``root_uri`` collide with its
        content-twin on ``(source_kind, root_uri)`` — that collision is the
        INTENDED precondition for source-row dedup, NOT a finished state. A
        confirmed run MUST be followed by the source-row collapse step
        (``repair_duplicate_sources`` / the Deploy-3 ``(source_kind, root_uri)``
        unique index + ON-CONFLICT). In the pre-collapse window the two sources
        walk the same blob, but the M18 ``(vendor, external_session_id)``
        canonical index prevents any permanent session duplication.
        """
        assert_operator_principal(call_context, "backfill_export_blob_identity")
        backfill = ExportBlobIdentityBackfill(
            blob_storage_service=self._blob_adapter.blob_storage_service,
            repository=self._repository,
        )
        return backfill.run(confirm=confirm)

def _coerce_datetime(value: datetime | str | None) -> datetime | None:
    """M17 §2.2 / W5.E cycle-4 C3: coerce str/datetime payload at the entry.

    Routes both typed datetimes and string payloads through the shared
    ``_normalize_event_at`` helper so the typed-naïve path is also
    rejected at service entry (pre-W5.E the typed-datetime branch
    returned the value unchecked, leaving naïve ``since=`` callers
    silently accepted). First-party Python callers that pass typed
    aware datetimes get the same UTC-normalization the str-path does.
    """
    if value is None:
        return None
    return _normalize_event_at(value)


def _coerce_enum[E: StrEnum](value: E | str | None, enum_cls: type[E]) -> E | None:
    """M17 §2.2: coerce str payload to StrEnum at the entry; typed input passes through."""
    if value is None:
        return None
    if isinstance(value, enum_cls):
        return value
    return enum_cls(value)


def _envelope_session_row(env: SearchResultEnvelope) -> dict[str, object]:
    """Extract the envelope's denormalized session row dict; empty if absent."""
    return env.session if isinstance(env.session, dict) else {}


def _envelope_matches_filters(
    env: SearchResultEnvelope,
    *,
    vendor: SourceVendor | None,
    source_kind: IngestSourceKind | None,
    since: datetime | None,
) -> bool:
    """Predicate per envelope; conservative on missing fields when filter is set.

    W5.E G4 cross-pollination (Codex cycle-3 C6 PROMOTED): the ``since``
    branch routes ``last_event_at`` through the shared
    ``_normalize_event_at`` helper. Pre-W5.E this branch checked
    ``isinstance(row_last, datetime)`` — but ``PostgresProvider`` returns
    ISO-string rows (per the G4 root cause), so the check always failed
    and the envelope was DROPPED. The false-negative DROP meant
    ``search_sessions(since=...)`` returned 0 results when matching
    sessions existed. The strict helper closes the same seam at the
    filter path that the envelope seam closes.
    """
    row = _envelope_session_row(env)
    if vendor is not None and row.get("vendor") != vendor.value:
        return False
    if source_kind is not None and row.get("source_kind") != source_kind.value:
        return False
    if since is not None:
        row_last_raw = row.get("last_event_at")
        if row_last_raw is None:
            return False
        try:
            row_last = _normalize_event_at(row_last_raw)
        except ValueError:
            return False
        if row_last < since:
            return False
    return True


def _filter_envelopes(
    envelopes: list[SearchResultEnvelope],
    *,
    vendor: SourceVendor | None,
    source_kind: IngestSourceKind | None,
    since: datetime | None,
) -> list[SearchResultEnvelope]:
    """M17 §2.2: post-filter ANN envelopes against vendor / source_kind / since.

    Operates on envelope dicts produced by SummaryWriter.search; each
    envelope's ``session`` field carries the denormalized session row
    columns (vendor, source_id, last_event_at, ...). When a filter is
    requested but the envelope lacks the relevant field, the envelope is
    dropped (conservative — prevents leaking unfiltered results).
    """
    return [
        env
        for env in envelopes
        if _envelope_matches_filters(
            env, vendor=vendor, source_kind=source_kind, since=since
        )
    ]


def _build_semantic_writers(
    *,
    repository: SessionLedgerRepository,
    embedding_service: EmbeddingServiceInterface | None,
    vector_service: VectorServiceInterface | None,
) -> tuple[SummaryWriter, EventEmbeddingWriter]:
    """Assemble the two semantic-index policy writers over one collaborator set.

    M6's summary writer and LED-01's event-content writer wrap the same
    repository + optional embedding/vector bindings and share the same
    fail-closed posture when those bindings are absent (cloud profiles keep
    the M1-M5 surface; embedding surfaces raise their
    ``*ServicesUnavailableError`` on use).
    """
    summary_writer = SummaryWriter(
        repository=repository,
        embedding_service=embedding_service,
        vector_service=vector_service,
    )
    event_embedding_writer = EventEmbeddingWriter(
        repository=repository,
        embedding_service=embedding_service,
        vector_service=vector_service,
    )
    return summary_writer, event_embedding_writer


def _public_event_envelope(row: dict[str, Any]) -> dict[str, Any]:
    """Map a projected ``__event`` row to the public per-event envelope.

    Shared by ``list_events_by_source_window`` and ``search_event_content``
    so the two event-listing surfaces present one shape: the repository
    projection's ``session_vendor`` becomes the public ``vendor`` field and
    a ``datetime`` ``event_at`` is ISO-ified (rows read through the real
    provider arrive as ISO strings already; offline shims may hand back
    ``datetime`` values directly).
    """
    return {
        "event_id": row.get("event_id"),
        "session_id": row.get("session_id"),
        "sequence": row.get("sequence"),
        "event_at": (
            row["event_at"].isoformat()
            if isinstance(row.get("event_at"), datetime)
            else row.get("event_at")
        ),
        "role": row.get("role"),
        "content_text": row.get("content_text"),
        "vendor": row.get("session_vendor"),
        "source_kind": row.get("source_kind"),
    }


def _naive_utc_to_iso(value: datetime) -> str:
    """Naive-UTC ``datetime`` -> explicit-offset ISO string, for the EDGE
    output seam.

    Platform storage convention (service.py's own ``_normalize_event_at``
    docstring): naive-stored timestamps ARE semantically UTC; this attaches
    ``tzinfo=UTC`` (not ``astimezone`` — the value is already UTC, only
    untagged) before ``.isoformat()``, so the output carries an explicit
    ``+00:00`` rather than a bare naive isoformat (the defect class this
    helper exists to close — see ``list_canonical_contributors``).
    """
    return value.replace(tzinfo=UTC).isoformat()


def _parse_iso(s: str) -> datetime:
    """Strict ISO-8601 parse. Always returns a UTC-aware ``datetime``.

    Bare ``Z`` suffix is normalized to ``+00:00`` for ``fromisoformat``.

    Naïve (timezone-unaware) inputs are REJECTED with ``ValueError`` per
    ``[[fast-fail-development-strategy]]``. Pre-2026-06-11 the helper
    silently coerced naïve datetimes to whatever local-or-no zone
    ``fromisoformat`` returned, which propagated through the
    ``list_sessions`` filter chain as a timezone offset slip: a
    UTC-aware filter value compared against a possibly-naïve persisted
    value let pre-cutoff sessions through (operator empirical evidence
    2026-06-11 PT: filter ``2025-11-01T00:00:00Z`` returned a session
    whose ``first_event_at`` is ``2025-10-31T18:31:59``).

    Aware inputs in any zone are accepted and normalized to UTC so all
    downstream SQL comparisons run on a single time axis.
    """
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    parsed = datetime.fromisoformat(s)
    if parsed.tzinfo is None or parsed.tzinfo.utcoffset(parsed) is None:
        raise ValueError(
            f"_parse_iso: naïve datetime not accepted; require an explicit "
            f"timezone offset (e.g. 'Z' or '+00:00'). Got: {s!r}",
        )
    return parsed.astimezone(UTC)


def _parse_iso_with_naive_as_utc(s: str) -> datetime:
    """ISO-8601 parse for repository-row datetime ISO strings.

    The ledger ``timestamp without time zone`` columns surface as naïve
    ISO strings (no ``Z`` / ``+00:00``) through
    ``PostgresProvider._serialize_for_json``. The platform stores those
    naïve values as semantic UTC (DDL default
    ``(now() AT TIME ZONE 'UTC'::text)``), so the row-path parser
    attaches ``tzinfo=UTC`` rather than raising. Aware inputs are
    routed through ``_parse_iso`` so the strict-aware contract holds
    for values that DO carry an offset.

    Used by ``_normalize_event_at`` on the str-input path (live row
    dicts). First-party Python callers that supply typed datetimes
    still go through the strict-aware contract on the typed path
    inside ``_normalize_event_at`` itself.
    """
    if s.endswith("Z") or "+" in s or s[10:].count("-") > 0:
        return _parse_iso(s)
    parsed = datetime.fromisoformat(s)
    if parsed.tzinfo is None or parsed.tzinfo.utcoffset(parsed) is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _normalize_event_at(value: object) -> datetime:
    """W5.E §5.3 shared strict-parse-at-seam helper for event_at /
    last_event_at across both repository-row and typed-API surfaces.

    Single source of truth for the rejection contract: both the
    ``_build_event_envelope`` envelope seam and the
    ``_envelope_matches_filters`` post-filter use this helper so the
    string-vs-typed-datetime asymmetry that fired G4 + C6 in production
    cannot recur.

    Two parse paths with different strictness, per the platform's actual
    storage convention (W5.E live-DB calibration 2026-06-14):

    * **Str path** — values arrive as naïve ISO strings from
      ``PostgresProvider._serialize_for_json``, because the ledger
      timestamp columns are declared ``timestamp without time zone``
      with ``DEFAULT (now() AT TIME ZONE 'UTC'::text)``. The platform
      convention is "stored naïve, semantically UTC." The str path
      parses via ``datetime.fromisoformat`` and attaches ``tzinfo=UTC``
      when the parsed value is naïve; aware ISO strings (``Z`` /
      ``+00:00``) flow through ``_parse_iso``.
    * **Typed-datetime path** — Codex cycle-4 C3 full-strictness:
      reject when ``tzinfo is None`` OR ``tzinfo.utcoffset(value)``
      returns None. First-party Python callers MUST pass aware
      datetimes; the typed path catches naïve-since-param regressions
      at service entry.

    G4 root cause (confirmed via reading
    ``plugins/postgres_state_management_plugin/src/.../provider.py:870-873``):
    ``PostgresProvider.execute_query`` calls ``_serialize_for_json``
    over every fetched row, which converts every ``datetime|date`` to
    its ISO ``isoformat()`` string. By the time the row dict reaches
    the envelope seam, ``event_at`` is a ``str``, not a ``datetime``.
    Pre-W5.E ``_build_event_envelope`` checked ``isinstance(..., datetime)``,
    failed silently, and fell back to ``datetime.now(UTC)`` — every
    result row carried wrong timestamps matching wall-clock-at-query,
    not the real event time. The helper closes that seam without
    breaking on the platform's naïve-stored-UTC convention.
    """
    if isinstance(value, str):
        return _parse_iso_with_naive_as_utc(value)
    if isinstance(value, datetime):
        if value.tzinfo is None or value.tzinfo.utcoffset(value) is None:
            raise ValueError(
                f"_normalize_event_at: naïve or pseudo-aware typed "
                f"datetime not accepted; require explicit timezone "
                f"offset. Got: {value!r}",
            )
        return value.astimezone(UTC)
    raise ValueError(
        f"_normalize_event_at: expected str or datetime, got "
        f"{type(value).__name__}={value!r}",
    )


def _build_drain_executors(
    summary_executor: SummaryExecutor | None,
) -> tuple[SummaryExecutor, SummaryExecutor, SummaryExecutor]:
    """Three independent single-slot drain guards (summarize + event-embedding + importer poll).

    Each is a singleton across overlapping cron fires (a second ``submit`` no-ops); the three are
    SEPARATE slots so the auto-summarize, LED-01 event-embedding, and importer-poll heartbeats run
    concurrently over their different corpora — none blocks another. ``summary_executor`` is
    injectable for tests.
    """
    return (
        summary_executor or BoundedSummaryExecutor(),
        BoundedSummaryExecutor(name="ledger-embedding"),
        BoundedSummaryExecutor(name="ledger-poll"),
    )


def _descriptor_only_entry(descriptor: Any) -> dict[str, Any]:
    """list_sources entry for a loaded plugin with no registered DB row."""
    return {
        "source_kind": descriptor.source_kind.value,
        "vendor": descriptor.vendor.value,
        "supported_modes": [m.value for m in descriptor.supported_modes],
        "default_lease_ttl_seconds": descriptor.default_lease_ttl_seconds,
        "default_pulling_root_uri": descriptor.default_pulling_root_uri,
        "source_id": None,
        "root_uri": None,
        "account_label": None,
        "enabled": None,
        "config_json": None,
    }


def _joined_entry(descriptor: Any, row: Any) -> dict[str, Any]:
    """list_sources entry for a loaded plugin paired with a registered DB row."""
    return {
        "source_kind": descriptor.source_kind.value,
        "vendor": descriptor.vendor.value,
        "supported_modes": [m.value for m in descriptor.supported_modes],
        "default_lease_ttl_seconds": descriptor.default_lease_ttl_seconds,
        "default_pulling_root_uri": descriptor.default_pulling_root_uri,
        "source_id": row.id,
        "root_uri": row.root_uri,
        "account_label": row.account_label,
        "enabled": row.enabled,
        "config_json": row.config_json,
    }


def _row_only_entry(row: Any) -> dict[str, Any]:
    """list_sources entry for a registered DB row whose plugin is not loaded."""
    return {
        "source_kind": row.source_kind.value,
        "vendor": None,
        "supported_modes": None,
        "default_lease_ttl_seconds": None,
        "default_pulling_root_uri": None,
        "source_id": row.id,
        "root_uri": row.root_uri,
        "account_label": row.account_label,
        "enabled": row.enabled,
        "config_json": row.config_json,
    }


__all__ = ["SessionLedgerService"]
