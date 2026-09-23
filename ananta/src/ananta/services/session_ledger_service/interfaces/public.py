"""Session ledger service public API — @service_interface_process declarations.

Spec §5.4. Per the session-ledger-API god-class-split design record
(dev-checkout workbench — not part of the shipped tree) the former 4-ABC
layout split into EIGHT coherent ABCs along domain axes
(read / ingest / polling-driver / canonical-pointer-repair /
inverted-bounds-repair / summarize / deployment / search). The
``SessionLedgerAnnotationAPI`` (list_quarantined_events,
acknowledge_quarantine, acknowledge_quarantines_by_source_kind) was
removed by the 2026-06-14 content-scan-gate eradication campaign.

* :class:`SessionLedgerReadAPI` — read/query surface (list_sources,
  list_sessions, list_active_sessions, get_session_timeline, list_tool_calls).
* :class:`SessionLedgerIngestAPI` — ingest/write surface
  (register_source, ingest_raw_chunk, get_import_status).
* :class:`SessionLedgerPollingDriverAPI` — polling-loop control
  (trigger_poll, ensure_periodic_poll_schedule, reset_ingest_state).
* :class:`SessionLedgerCanonicalPointerRepairAPI` — one-shot operator
  verb (lift_canonical_pointer_for_duplicate_sessions).
* :class:`SessionLedgerInvertedBoundsRepairAPI` — one-shot operator
  verb (backfill_first_last_event_at_repair).
* :class:`SessionLedgerSummarizeAPI` — M6 summarization
  (lift_codex_stage1_summaries, summarize_quiescent_sessions,
  ensure_periodic_summarize_schedule).
* :class:`SessionLedgerDeploymentAPI` — M5 shipper-bootstrap + pairing
  (generate_ingest_setup, approve_pairing, shipper_self_revoke).
* :class:`SessionLedgerSearchAPI` — M6 caller-side summarizer push +
  semantic search (push_session_summary_chunk, search_sessions,
  list_events_by_source_window).

Process keys unchanged across the split (every verb keeps its
``service_interface::session_ledger_service::<name>`` key because the
key is derived from the decorator's ``provider=_PROVIDER`` argument).
:class:`SessionLedgerService` implements all eight ABCs via multiple
inheritance; :class:`ServiceInterfaceScanner` walks every class in this
module and discovers @service_interface_process methods by attribute
marker (not by Protocol membership), so every ABC registers its methods
automatically.

Discoverability follows Task #47: every model-callable method declares
``is_discoverable=True`` explicitly; operator-bridge-only / boot-only
methods (e.g. ``get_import_status``, ``ingest_raw_chunk``,
``trigger_poll``, ``ensure_periodic_poll_schedule``,
``shipper_self_revoke``) declare it ``False``.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime
from typing import Any

from ananta.core.actions.action_metadata import (
    MergeErrorProcessorCustomizations,
    MergeResultProcessorCustomizations,
    ParameterMetadata,
    ParameterType,
    ReturnValueSchema,
)
from ananta.core.domain.enums import ProcessorPolicyCategory
from ananta.core.services.call_context import CallContext
from ananta.core.services.service_interface_decorator import service_interface_process
from ananta.llm.session_ledger.types import (
    IngestSourceKind,
    SessionsOrderBy,
    SourceVendor,
)

_PROVIDER = "session_ledger_service"


class SessionLedgerReadAPI(ABC):
    """Session-ledger READ-PATH operations exposed to model and operator.

    Implemented by ``ananta.services.session_ledger_service.service.SessionLedgerService``.
    Split out from former ``SessionLedgerServiceAPI`` per the same
    god-class-split design record (dev-checkout workbench — not part of
    the shipped tree) along the read / ingest / triage domain axes. Process keys unchanged
    (``service_interface::session_ledger_service::<verb>``); the
    ServiceInterfaceScanner walks every class in this module and discovers
    @service_interface_process methods by attribute marker.
    """

    @service_interface_process(
        name="list_sources",
        is_discoverable=True,
        provider=_PROVIDER,
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        parameters={},
        return_value_schema=ReturnValueSchema(
            description="Registered ingest sources joined with their plugin descriptors.",
            type=ParameterType.OBJECT,
            properties={
                "sources": ParameterMetadata(
                    type=ParameterType.LIST,
                    description=(
                        "List of entries each carrying descriptor fields "
                        "(source_kind, vendor, supported_modes, "
                        "default_lease_ttl_seconds, default_pulling_root_uri) "
                        "plus DB-row fields (source_id, root_uri, "
                        "account_label, enabled, config_json). Loaded "
                        "plugins with no registered row surface "
                        "descriptor-only (DB fields null); registered rows "
                        "with no loaded plugin surface DB-only (descriptor "
                        "fields null)."
                    ),
                ),
            },
        ),
        result_processor_customizations=MergeResultProcessorCustomizations(
            action_label="List Session Sources",
            result_type="ledger_sources",
            result_description="Configured ingest sources for the session ledger.",
        ),
        error_processor_customizations=MergeErrorProcessorCustomizations(retryable=True),
    )
    @abstractmethod
    def list_sources(self) -> dict[str, Any]:
        """Return ingest source descriptors joined with their DB-registered rows."""


    @service_interface_process(
        name="census",
        is_discoverable=True,
        provider=_PROVIDER,
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        parameters={},
        return_value_schema=ReturnValueSchema(
            description=(
                "SQL-aggregated read-only inventory of the entire ledger: per "
                "source counts, batch health, normalized root_uri, and an "
                "order-independent row-identity fingerprint, plus corpus totals "
                "and the duplicate-source-group count."
            ),
            type=ParameterType.OBJECT,
            properties={
                "sources": ParameterMetadata(
                    type=ParameterType.LIST,
                    description=(
                        "One entry per live source: {source_id, source_kind, "
                        "root_uri, normalized_root_uri, session_count, "
                        "canonical_count, sibling_count, event_count, "
                        "tool_call_count, row_identity_fingerprint, "
                        "owned_running_batches, unclaimed_route_batches, "
                        "oldest_running_batch_age_seconds}."
                    ),
                ),
                "source_count": ParameterMetadata(
                    type=ParameterType.INTEGER,
                    description="Number of live source rows.",
                ),
                "duplicate_source_groups": ParameterMetadata(
                    type=ParameterType.INTEGER,
                    description=(
                        "Count of (source_kind, normalized root_uri) keys with "
                        "more than one live row (the dedup invariant: must be 0)."
                    ),
                ),
                "total_sessions": ParameterMetadata(
                    type=ParameterType.INTEGER, description="Live sessions across all sources.",
                ),
                "total_events": ParameterMetadata(
                    type=ParameterType.INTEGER, description="Live events across all sources.",
                ),
                "total_tool_calls": ParameterMetadata(
                    type=ParameterType.INTEGER, description="Live tool calls across all sources.",
                ),
                "total_owned_running_batches": ParameterMetadata(
                    type=ParameterType.INTEGER,
                    description="Lease-owned running batches (must be 0 after a synchronous ingest).",
                ),
                "total_unclaimed_route_batches": ParameterMetadata(
                    type=ParameterType.INTEGER,
                    description="Unclaimed route batches (orphans once the legacy push survivor is swept).",
                ),
            },
        ),
        result_processor_customizations=MergeResultProcessorCustomizations(
            action_label="Ledger Census",
            result_type="ledger_census",
            result_description="Whole-ledger aggregated inventory.",
        ),
        error_processor_customizations=MergeErrorProcessorCustomizations(retryable=True),
    )
    @abstractmethod
    def census(self) -> dict[str, Any]:
        """Return a whole-ledger SQL-aggregated read-only inventory."""

    @service_interface_process(
        name="list_sessions",
        is_discoverable=True,
        provider=_PROVIDER,
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        parameters={
            "limit": ParameterMetadata(
                type=ParameterType.INTEGER,
                description="Max sessions to return (1..200; default 50).",
                required=False,
            ),
            "since": ParameterMetadata(
                type=ParameterType.STRING,
                description="ISO-8601 lower bound on last_event_at (UTC).",
                required=False,
            ),
            "until": ParameterMetadata(
                type=ParameterType.STRING,
                description="ISO-8601 upper bound on last_event_at (UTC).",
                required=False,
            ),
            "first_event_since": ParameterMetadata(
                type=ParameterType.STRING,
                description=(
                    "ISO-8601 lower bound on first_event_at (UTC). Use to find "
                    "sessions that STARTED on or after a date, regardless of "
                    "their last activity."
                ),
                required=False,
            ),
            "first_event_until": ParameterMetadata(
                type=ParameterType.STRING,
                description=(
                    "ISO-8601 upper bound on first_event_at (UTC). Pairs with "
                    "first_event_since for a start-window search."
                ),
                required=False,
            ),
            "project_path": ParameterMetadata(
                type=ParameterType.STRING,
                description="Exact-match filter on session.project_path.",
                required=False,
            ),
            "vendor": ParameterMetadata(
                type=ParameterType.STRING,
                description=(
                    "SourceVendor StrEnum value — one of 'codex', 'claude_code', "
                    "'agent_messaging', 'chatgpt'. Exact-match filter on "
                    "session.vendor."
                ),
                required=False,
            ),
            "source_kind": ParameterMetadata(
                type=ParameterType.STRING,
                description=(
                    "IngestSourceKind StrEnum value (e.g. 'claude_code_history', "
                    "'codex_state', 'codex_ambient'). W5.B §3.7.A cross-source "
                    "dedupe: the filter matches the canonical row when ANY "
                    "contributor in its (vendor, external_session_id) group "
                    "has source_kind=K — including siblings. SQL-lockdown: now "
                    "resolved via a read-then-route over the session_source_kind "
                    "junction (query the junction by source_kind → the canonical "
                    "ids whose group has a kind-K contributor → restrict the "
                    "session read), byte-equivalent to the pre-lockdown "
                    "EXISTS-over-the-group. Pre-W5.B the filter joined __source "
                    "directly and hid canonicals whose own source had a different "
                    "kind from K (Codex C2 regression)."
                ),
                required=False,
            ),
            "order_by": ParameterMetadata(
                type=ParameterType.STRING,
                description=(
                    "SessionsOrderBy StrEnum value — 'last_event_at_desc' (default), "
                    "'last_event_at_asc', 'first_event_at_desc', "
                    "'first_event_at_asc'. Use first_event_at_asc + "
                    "first_event_since to find the EARLIEST session in a window."
                ),
                required=False,
            ),
            "include_siblings": ParameterMetadata(
                type=ParameterType.BOOLEAN,
                description=(
                    "Operator-debug forensic flag (W5.B §3.7.A). Default "
                    "False filters to canonical rows only "
                    "(canonical_external_session_id IS NULL) so cross-source "
                    "dedup is automatic. True returns BOTH canonical AND "
                    "sibling rows side-by-side for audits of the dedupe "
                    "state itself. With a source_kind filter, True returns the "
                    "FULL membership (canonical + siblings) of each matching "
                    "group via the junction group-expansion, not just the "
                    "canonical (SQL-lockdown faithful-to-the-EXISTS fix)."
                ),
                required=False,
            ),
        },
        return_value_schema=ReturnValueSchema(
            description="Ingested sessions, ordered per order_by (default last_event_at DESC).",
            type=ParameterType.OBJECT,
            properties={
                "sessions": ParameterMetadata(
                    type=ParameterType.LIST,
                    description="List of session row dicts.",
                ),
            },
        ),
        result_processor_customizations=MergeResultProcessorCustomizations(
            action_label="List Sessions",
            result_type="ledger_sessions",
            result_description="Sessions in the LLM session ledger.",
        ),
        error_processor_customizations=MergeErrorProcessorCustomizations(retryable=True),
    )
    @abstractmethod
    def list_sessions(
        self,
        limit: int = 50,
        since: datetime | None = None,
        until: datetime | None = None,
        first_event_since: datetime | None = None,
        first_event_until: datetime | None = None,
        project_path: str | None = None,
        vendor: SourceVendor | None = None,
        source_kind: IngestSourceKind | None = None,
        order_by: SessionsOrderBy = SessionsOrderBy.LAST_EVENT_AT_DESC,
        include_siblings: bool = False,
    ) -> dict[str, Any]:
        """Paginated session list with structured filters + ordering (M17 §2.2)."""

    @service_interface_process(
        name="list_active_sessions",
        is_discoverable=True,
        provider=_PROVIDER,
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        parameters={},
        return_value_schema=ReturnValueSchema(
            description="Sessions whose lease has not expired.",
            type=ParameterType.OBJECT,
            properties={
                "sessions": ParameterMetadata(
                    type=ParameterType.LIST,
                    description="List of active session rows joined to lease.",
                ),
            },
        ),
        result_processor_customizations=MergeResultProcessorCustomizations(
            action_label="List Active Sessions",
            result_type="ledger_active_sessions",
            result_description="Sessions still receiving lease pings.",
        ),
        error_processor_customizations=MergeErrorProcessorCustomizations(retryable=True),
    )
    @abstractmethod
    def list_active_sessions(self) -> dict[str, Any]:
        """Sessions whose lease has not expired."""

    @service_interface_process(
        name="get_session_timeline",
        is_discoverable=True,
        provider=_PROVIDER,
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        parameters={
            "session_id": ParameterMetadata(
                type=ParameterType.STRING,
                description="Session id (les-...).",
                required=True,
            ),
            "after_sequence": ParameterMetadata(
                type=ParameterType.INTEGER,
                description="Return events with sequence > this value (default 0).",
                required=False,
            ),
            "limit": ParameterMetadata(
                type=ParameterType.INTEGER,
                description=(
                    "Max events to return (1..100; default 100). Page a longer "
                    "timeline by advancing after_sequence (keyset pagination)."
                ),
                required=False,
            ),
        },
        return_value_schema=ReturnValueSchema(
            description="Events from a single session, ordered by sequence.",
            type=ParameterType.OBJECT,
            properties={
                "events": ParameterMetadata(
                    type=ParameterType.LIST,
                    description="Event row dicts.",
                ),
            },
        ),
        result_processor_customizations=MergeResultProcessorCustomizations(
            action_label="Get Session Timeline",
            result_type="ledger_session_timeline",
            result_description="Chronological event timeline for one ledger session.",
        ),
        error_processor_customizations=MergeErrorProcessorCustomizations(retryable=True),
    )
    @abstractmethod
    def get_session_timeline(
        self,
        session_id: str,
        after_sequence: int = 0,
        limit: int = 100,
    ) -> dict[str, Any]:
        """Per-session ordered event timeline."""

    @service_interface_process(
        name="list_tool_calls",
        is_discoverable=True,
        provider=_PROVIDER,
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        parameters={
            "session_id": ParameterMetadata(
                type=ParameterType.STRING,
                description="Restrict to a single session (optional).",
                required=False,
            ),
            "tool_name": ParameterMetadata(
                type=ParameterType.STRING,
                description="Exact-match filter on tool_name.",
                required=False,
            ),
            "status": ParameterMetadata(
                type=ParameterType.STRING,
                description="One of pending / succeeded / errored.",
                required=False,
            ),
            "since_iso": ParameterMetadata(
                type=ParameterType.STRING,
                description="ISO-8601 lower bound on called_at (UTC).",
                required=False,
            ),
            "limit": ParameterMetadata(
                type=ParameterType.INTEGER,
                description="Max tool-call rows to return (1..100; default 50).",
                required=False,
            ),
        },
        return_value_schema=ReturnValueSchema(
            description="Tool-call projection rows.",
            type=ParameterType.OBJECT,
            properties={
                "tool_calls": ParameterMetadata(
                    type=ParameterType.LIST,
                    description="Projection rows from session_ledger__tool_call.",
                ),
            },
        ),
        result_processor_customizations=MergeResultProcessorCustomizations(
            action_label="List Tool Calls",
            result_type="ledger_tool_calls",
            result_description="Tool-call rows from the ledger.",
        ),
        error_processor_customizations=MergeErrorProcessorCustomizations(retryable=True),
    )
    @abstractmethod
    def list_tool_calls(
        self,
        session_id: str | None = None,
        tool_name: str | None = None,
        status: str | None = None,
        since_iso: str | None = None,
        limit: int = 50,
    ) -> dict[str, Any]:
        """Tool-call projection query."""

    @service_interface_process(
        name="list_canonical_contributors",
        is_discoverable=True,
        provider=_PROVIDER,
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        parameters={
            "session_id": ParameterMetadata(
                type=ParameterType.STRING,
                description=(
                    "Session id (les-...). May be EITHER the canonical "
                    "session id OR a sibling pointing at the canonical "
                    "via canonical_external_session_id. The verb resolves "
                    "to the canonical's (vendor, external_session_id) "
                    "pair and returns every contributing source row."
                ),
                required=True,
            ),
        },
        return_value_schema=ReturnValueSchema(
            description=(
                "Per-source contributor rows for a canonical session's "
                "logical conversation (W5.B §3.3)."
            ),
            type=ParameterType.OBJECT,
            properties={
                "canonical_session_id": ParameterMetadata(
                    type=ParameterType.STRING,
                    description=(
                        "Canonical row's ledger session id (les-...). "
                        "None when the canonical row has been soft-deleted "
                        "(orphaned-canonical case); inspect "
                        "``orphaned_canonical`` to discriminate."
                    ),
                ),
                "canonical_external_session_id": ParameterMetadata(
                    type=ParameterType.STRING,
                    description=(
                        "Shared vendor external_session_id (the dedupe "
                        "key value). Always present even when the canonical "
                        "ledger row is orphaned, because siblings carry the "
                        "same external_session_id as the canonical."
                    ),
                ),
                "vendor": ParameterMetadata(
                    type=ParameterType.STRING,
                    description=(
                        "SourceVendor enum value, uniform across every "
                        "contributor in the (vendor, external_session_id) "
                        "group."
                    ),
                ),
                "orphaned_canonical": ParameterMetadata(
                    type=ParameterType.BOOLEAN,
                    description=(
                        "True iff no contributor has ``is_canonical=True`` "
                        "(the canonical row is soft-deleted while siblings "
                        "remain). Operator detects the anomaly via this "
                        "flag rather than scanning the contributors list."
                    ),
                ),
                "contributors": ParameterMetadata(
                    type=ParameterType.LIST,
                    description=(
                        "Per-source-row entries: {session_id, source_id, "
                        "source_kind, first_event_at, last_event_at, "
                        "contributed_event_count, is_canonical}. Canonical "
                        "first (when present), then siblings ordered by "
                        "source_kind ASC."
                    ),
                ),
            },
        ),
        result_processor_customizations=MergeResultProcessorCustomizations(
            action_label="List Canonical Contributors",
            result_type="ledger_canonical_contributors",
            result_description=(
                "Provenance projection: every __source row that contributed "
                "to a canonical session's logical conversation."
            ),
        ),
        error_processor_customizations=MergeErrorProcessorCustomizations(retryable=True),
    )
    @abstractmethod
    def list_canonical_contributors(self, session_id: str) -> dict[str, Any]:
        """Per-canonical-group provenance projection (W5.B §3.3)."""


class SessionLedgerQualificationAPI(ABC):
    """Bounded setup qualification backed by the authoritative manager journal."""

    @service_interface_process(
        name="qualify_selected_sources",
        is_discoverable=True,
        provider=_PROVIDER,
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        parameters={
            "target": ParameterMetadata(
                type=ParameterType.STRING,
                description="Absolute target path named by the doctor probe.",
                required=True,
            ),
            "name": ParameterMetadata(
                type=ParameterType.STRING,
                description="Managed solet name named by the doctor probe.",
                required=True,
            ),
            "answers_fingerprint": ParameterMetadata(
                type=ParameterType.STRING,
                description="Canonical fingerprint carried by the doctor request.",
                required=True,
            ),
        },
        return_value_schema=ReturnValueSchema(
            description=(
                "Bounded per-source setup facts from the manager transaction and "
                "source-local ledger observations; never returns answer or event content."
            ),
            type=ParameterType.OBJECT,
            properties={
                "record_source": ParameterMetadata(
                    type=ParameterType.STRING,
                    description="Authoritative record provenance; currently manager_transaction.",
                ),
                "target_identity_matched": ParameterMetadata(
                    type=ParameterType.BOOLEAN,
                    description="Whether transaction target/name matches the doctor request.",
                ),
                "answers_fingerprint_matched": ParameterMetadata(
                    type=ParameterType.BOOLEAN,
                    description="Whether the loaded answers fingerprint matches the doctor request.",
                ),
                "qualification_reason": ParameterMetadata(
                    type=ParameterType.STRING,
                    description="Null on trusted proof, otherwise a bounded negative reason.",
                ),
                "sources": ParameterMetadata(
                    type=ParameterType.LIST,
                    description=(
                        "At most codex_local and claude_code_local rows: selected, consented, "
                        "registered, backfill_count, retrieval_ok. Empty when answers are untrusted."
                    ),
                ),
            },
        ),
        result_processor_customizations=MergeResultProcessorCustomizations(
            action_label="Qualify Selected Session Sources",
            result_type="selected_session_sources_qualification",
            result_description="Bounded selected-source registration, backfill, and retrieval proof.",
        ),
        error_processor_customizations=MergeErrorProcessorCustomizations(retryable=True),
    )
    @abstractmethod
    def qualify_selected_sources(
        self,
        target: str,
        name: str,
        answers_fingerprint: str,
    ) -> dict[str, Any]:
        """Qualify selected local session sources against manager-recorded answers."""


class SessionLedgerIngestAPI(ABC):
    """Session-ledger INGEST/WRITE operations.

    Source-row INSERT, chunk-text ingest, and batch-status diagnostic.
    Split out from former ``SessionLedgerServiceAPI`` per the v1 god-class
    split design. Process keys unchanged.
    """

    @service_interface_process(
        name="get_import_status",
        is_discoverable=False,  # operator-bridge diagnostic
        provider=_PROVIDER,
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        parameters={
            "batch_id": ParameterMetadata(
                type=ParameterType.STRING,
                description="Import batch id (imb-...).",
                required=True,
            ),
        },
        return_value_schema=ReturnValueSchema(
            description="Import batch diagnostics row.",
            type=ParameterType.OBJECT,
        ),
        result_processor_customizations=MergeResultProcessorCustomizations(
            action_label="Get Import Status",
            result_type="ledger_import_status",
            result_description="Per-batch ingest diagnostics.",
        ),
        error_processor_customizations=MergeErrorProcessorCustomizations(retryable=True),
    )
    @abstractmethod
    def get_import_status(self, batch_id: str) -> dict[str, Any]:
        """Per-batch ingest diagnostics."""

    # ------------------------------------------------------------------
    # Write surface
    # ------------------------------------------------------------------

    @service_interface_process(
        name="register_source",
        is_discoverable=True,
        provider=_PROVIDER,
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        parameters={
            "source_kind": ParameterMetadata(
                type=ParameterType.STRING,
                description="StrEnum IngestSourceKind value.",
                required=True,
            ),
            "root_uri": ParameterMetadata(
                type=ParameterType.STRING,
                description="Filesystem path, blob id, or sentinel.",
                required=True,
            ),
            "account_label": ParameterMetadata(
                type=ParameterType.STRING,
                description="Operator-supplied label.",
                required=False,
            ),
            "config_json": ParameterMetadata(
                type=ParameterType.DICT,
                description="Source-private config payload (default empty dict).",
                required=False,
            ),
        },
        return_value_schema=ReturnValueSchema(
            description="Source row id, plus an outcome token discriminating new INSERT from idempotent hit.",
            type=ParameterType.OBJECT,
            properties={
                "source_id": ParameterMetadata(
                    type=ParameterType.STRING,
                    description="Source id (src-...); newly minted on 'registered', existing on 'existed'.",
                ),
                "outcome": ParameterMetadata(
                    type=ParameterType.STRING,
                    description=(
                        "'registered' when a new row was inserted; 'existed' "
                        "when an existing row matched (source_kind, root_uri). "
                        "Field name avoids 'action' / 'status' so it doesn't "
                        "collide with the platform's result-contract validator "
                        "(matches ensure_periodic_poll_schedule precedent)."
                    ),
                ),
            },
        ),
        result_processor_customizations=MergeResultProcessorCustomizations(
            action_label="Register Ledger Source",
            result_type="ledger_source_registered",
            result_description="Ingest source registration outcome (registered or existed).",
        ),
        error_processor_customizations=MergeErrorProcessorCustomizations(retryable=False),
        requires_call_context=True,
    )
    @abstractmethod
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

        Returns ``{source_id, outcome}`` where ``outcome`` is ``'registered'``
        on a new INSERT or ``'existed'`` on an idempotent hit against a row
        already matching ``(source_kind, root_uri)``. ``account_label`` and
        ``config_json`` are applied only on the registered path. (M17 §2.5
        docstring fix — the impl returns ``outcome``, not ``action``.)

        **Authorization (P1.1.E):** a filesystem ``root_uri`` requires an
        operator/operator-equivalent ``call_context`` (injected server-side via
        ``requires_call_context``) AND containment under a configured
        ``ledger_allowed_roots`` entry. Blob-id / pushed / symbolic ``root_uri``
        values skip the path check.
        """

    @service_interface_process(
        name="ingest_raw_chunk",
        is_discoverable=False,  # M1: operator-bridge only. M5: also ingest bridge.
        provider=_PROVIDER,
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        parameters={
            "source_kind": ParameterMetadata(
                type=ParameterType.STRING,
                description="StrEnum IngestSourceKind value identifying the push source plugin.",
                required=True,
            ),
            "chunk_text": ParameterMetadata(
                type=ParameterType.STRING,
                description="UTF-8 text of one push chunk.",
                required=True,
            ),
            "source_id": ParameterMetadata(
                type=ParameterType.STRING,
                description=(
                    "Optional source row id to bind the push to (A2 export "
                    "bind). When omitted the legacy first-enabled-by-kind "
                    "resolve-or-create path runs (shipper push)."
                ),
                required=False,
            ),
        },
        return_value_schema=ReturnValueSchema(
            description="Per-chunk import report.",
            type=ParameterType.OBJECT,
            properties={
                "events_persisted": ParameterMetadata(
                    type=ParameterType.INTEGER,
                    description="Clean events written.",
                ),
                "batch_id": ParameterMetadata(
                    type=ParameterType.STRING,
                    description="Id of the push batch this chunk was written under.",
                ),
            },
        ),
        result_processor_customizations=MergeResultProcessorCustomizations(
            action_label="Ingest Raw Chunk",
            result_type="ledger_ingest_chunk",
            result_description="One push chunk was ingested.",
        ),
        error_processor_customizations=MergeErrorProcessorCustomizations(retryable=False),
    )
    @abstractmethod
    def ingest_raw_chunk(
        self,
        source_kind: str,
        chunk_text: str,
        *,
        source_id: str | None = None,
    ) -> dict[str, Any]:
        """Push one chunk through the persistence pipeline.

        When ``source_id`` is given the push binds to that row and returns its
        real ``batch_id`` (A2 export bind); otherwise the legacy
        first-enabled-by-kind resolve-or-create path runs.
        """

    @service_interface_process(
        name="backfill_export_blob_identity",
        is_discoverable=False,  # operator maintenance verb, not model-discoverable
        provider=_PROVIDER,
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        parameters={
            "confirm": ParameterMetadata(
                type=ParameterType.BOOLEAN,
                description=(
                    "False (default) previews counts and mutates nothing; True "
                    "executes Phase 0 (tag) -> 1 (key/repoint) -> 2 (orphan "
                    "sweep). FORWARD-ONLY once confirmed — recovery is re-run, "
                    "not revert."
                ),
                required=False,
            ),
        },
        return_value_schema=ReturnValueSchema(
            description=(
                "Dry-run preview (confirmed=false) or executed counts "
                "(confirmed=true) for the export-blob content-digest backfill."
            ),
            type=ParameterType.OBJECT,
            properties={
                "confirmed": ParameterMetadata(
                    type=ParameterType.BOOLEAN,
                    description="Whether mutations were executed.",
                ),
                "sources_scanned": ParameterMetadata(
                    type=ParameterType.INTEGER,
                    description="Live export sources examined.",
                ),
                "blobs_needing_key": ParameterMetadata(
                    type=ParameterType.INTEGER,
                    description="Dry-run: blobs not yet content-keyed.",
                ),
                "sources_needing_repoint": ParameterMetadata(
                    type=ParameterType.INTEGER,
                    description="Dry-run: sources whose blob has a content twin.",
                ),
                "orphan_blobs_candidate": ParameterMetadata(
                    type=ParameterType.INTEGER,
                    description="Dry-run: tagged export blobs no live source references.",
                ),
                "blobs_keyed": ParameterMetadata(
                    type=ParameterType.INTEGER,
                    description="Confirmed: blobs assigned a content-digest external_id.",
                ),
                "sources_repointed": ParameterMetadata(
                    type=ParameterType.INTEGER,
                    description="Confirmed: sources repointed onto a content twin.",
                ),
                "export_blobs_deleted": ParameterMetadata(
                    type=ParameterType.INTEGER,
                    description="Confirmed: orphan export blobs reclaimed.",
                ),
                "skipped": ParameterMetadata(
                    type=ParameterType.INTEGER,
                    description="Confirmed: sources already content-keyed (no-op).",
                ),
                "error_count": ParameterMetadata(
                    type=ParameterType.INTEGER,
                    description="Confirmed: always 0 (any failure raises instead).",
                ),
            },
        ),
        result_processor_customizations=MergeResultProcessorCustomizations(
            action_label="Backfill Export Blob Identity",
            result_type="ledger_export_blob_backfill",
            result_description="Export-blob content-digest identity convergence.",
        ),
        error_processor_customizations=MergeErrorProcessorCustomizations(retryable=False),
        requires_call_context=True,
    )
    @abstractmethod
    def backfill_export_blob_identity(
        self, confirm: bool = False, *, call_context: CallContext | None = None,
    ) -> dict[str, Any]:
        """Converge export blobs onto content-digest identity (A3; operator-only)."""

    @service_interface_process(
        name="set_source_enabled",
        is_discoverable=False,  # operator-only; not model-discoverable
        provider=_PROVIDER,
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        parameters={
            "source_id": ParameterMetadata(
                type=ParameterType.STRING,
                description="Source row id (``src_...``) to toggle.",
                required=True,
            ),
            "enabled": ParameterMetadata(
                type=ParameterType.BOOLEAN,
                description=(
                    "The desired ``enabled`` state. ``False`` blocks both "
                    "the periodic poller (``list_sources(enabled_only=True)``) "
                    "and a targeted ``poll_source`` call (which raises "
                    "immediately on a disabled source) — the load-bearing "
                    "half of the duplicate-source quiesce protocol."
                ),
                required=True,
            ),
        },
        return_value_schema=ReturnValueSchema(
            description="Source enabled-flag toggle outcome.",
            type=ParameterType.OBJECT,
            properties={
                "source_id": ParameterMetadata(
                    type=ParameterType.STRING,
                    description="Echo of the supplied source id.",
                ),
                "prior_enabled": ParameterMetadata(
                    type=ParameterType.BOOLEAN,
                    description="The row's ``enabled`` value before this call.",
                ),
                "new_enabled": ParameterMetadata(
                    type=ParameterType.BOOLEAN,
                    description="The row's ``enabled`` value after this call.",
                ),
                "changed": ParameterMetadata(
                    type=ParameterType.BOOLEAN,
                    description=(
                        "``False`` when the requested state already matched "
                        "the current state — an idempotent no-op, zero "
                        "writes fired, not an error."
                    ),
                ),
            },
        ),
        result_processor_customizations=MergeResultProcessorCustomizations(
            action_label="Set Source Enabled",
            result_type="ledger_source_enabled_toggled",
            result_description=(
                "One session_ledger__source row's enabled flag toggled "
                "(or confirmed idempotently unchanged)."
            ),
        ),
        error_processor_customizations=MergeErrorProcessorCustomizations(retryable=False),
    )
    @abstractmethod
    def set_source_enabled(
        self,
        source_id: str,
        enabled: bool,
    ) -> dict[str, Any]:
        """Toggle one ``source`` row's ``enabled`` flag; operator-only.

        Schema-debt-external-id lane, 2b-S1 quiesce-protocol prerequisite
        (2026-08-07) — the duplicate-source-repair protocol's own "disable
        BOTH pair members" step had no executable verb until this one; full
        rationale in the schema-debt external-id findings record
        (dev-checkout workbench — not part of the shipped tree),
        "Canary attempt (2026-08-07)".

        Homed here (not alongside :meth:`retire_duplicate_source` on
        ``SessionLedgerInvertedBoundsRepairAPI``, which was the first
        semantic instinct) because that class was already at 566
        non-process LOC (>500, god-class violation) — measured, not
        assumed, per the seat's own warning. ``SessionLedgerIngestAPI`` is
        the next-best semantic fit (this toggles the same ``enabled``
        column :meth:`register_source` writes at creation time) and had
        240 lines of headroom.

        Idempotent: a call whose ``enabled`` already matches the row's
        current value fires zero writes and reports ``changed=False`` rather
        than erroring. Writes via ``StateManagementInterface`` primitives
        only, matching :meth:`retire_duplicate_source`'s discipline.

        Raises ``ValueError`` naming the id when ``source_id`` does not
        resolve to a live (non-soft-deleted) source row — fails loud rather
        than silently no-op-ing on a typo'd id.
        """


# ───────────────────────────────────────────────────────────────────────────
# M5.C — polling orchestration surface (split out from SessionLedgerServiceAPI)
# ───────────────────────────────────────────────────────────────────────────
#
# Polling-orchestration surface split (per v1 god-class split design)
# into PollingDriver + CanonicalPointerRepair + InvertedBoundsRepair +
# Summarize sibling ABCs. The ServiceInterfaceScanner walks every ABC
# in this module via ``inspect.getmembers(module, inspect.isclass)``
# and discovers @service_interface_process methods by attribute marker,
# so adding siblings does not require any scanner change.


class SessionLedgerPollingDriverAPI(ABC):
    """Polling-loop control: one-shot poll + idempotent periodic schedule + state-reset.

    Implemented by :class:`SessionLedgerService`. Verbs are operator-bridge /
    boot-only (``is_discoverable=False``); the autonomous-poll cron
    registered by :meth:`ensure_periodic_poll_schedule` fires
    :meth:`trigger_poll` on cadence without model involvement.
    """

    @service_interface_process(
        name="trigger_poll",
        is_discoverable=False,  # operator-bridge + cron-fired only; not model-discoverable
        provider=_PROVIDER,
        # EDGE_SINK per the canonical scheduler cron-action contract
        # enforced at ``create_cron_schedule`` registration (see the
        # scheduler cron-action-contract design record, dev-checkout
        # workbench — not part of the shipped tree, for the validator
        # design) + the canonical KB article at
        # ``knowledge_bases/ananta_platform/21_scheduling_service/
        # 01_template_flow_record_lifecycle.md``): terminal node — flow
        # ends here.
        # Result/error customizations are optional (EDGE_SINK never carried
        # them; since the 2026-07-15 relax EDGE may omit them too), so
        # ``action_queue_poller`` skips result-processing dispatch via the
        # EDGE_SINK_SKIP branch
        # (``result_processor_kind is None and result_processor is None``).
        # See the canonical example
        # at ``ananta/src/ananta/services/thinking_service/interfaces/public.py``
        # ``upsert_plan``.
        processor_policy_category=ProcessorPolicyCategory.EDGE_SINK,
        parameters={},
        return_value_schema=ReturnValueSchema(
            description=(
                "Heartbeat receipt for the singleton importer-poll drainer: whether this fire "
                "started it or found it already running. Pass counts are logged at drain "
                "completion, not returned."
            ),
            type=ParameterType.OBJECT,
            properties={
                "poller": ParameterMetadata(
                    type=ParameterType.STRING,
                    description="'started' when this fire launched the drainer; 'already_running' when the slot was held (no-op).",
                ),
            },
        ),
        # Terminal/headless cron-fired action per the canonical scheduler
        # cron-action contract: no ``result_processor_customizations``; no
        # ``error_processor_customizations``.
        # ``action_queue_poller._dispatch_*`` short-circuits via the
        # EDGE_SINK_SKIP branch (``result_processor_kind is None and
        # result_processor is None`` → terminal action, no dispatch); the
        # success-path inference scaffold + the error-path
        # ``submit_error_inference`` both stay off-path so the cron never
        # enters ``_resolve_io_process_key``.
    )
    @abstractmethod
    def trigger_poll(self) -> dict[str, Any]:
        """Cron heartbeat: (re)start the singleton importer-poll drainer."""

    @service_interface_process(
        name="poll_source",
        is_discoverable=False,  # operator-bridge + in-process callers (export kickoff, repair)
        provider=_PROVIDER,
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        parameters={
            "source_id": ParameterMetadata(
                type=ParameterType.STRING,
                description="Id of the single pulling source to poll synchronously.",
                required=True,
            ),
        },
        return_value_schema=ReturnValueSchema(
            description=(
                "One importer poll pass over exactly the named source. Raises "
                "(LedgerPollError) on a missing/deleted/disabled source, a "
                "non-pulling source, lease contention, or any source-pass "
                "failure — a single failure channel, never a silent zero."
            ),
            type=ParameterType.OBJECT,
            properties={
                "sources_polled": ParameterMetadata(
                    type=ParameterType.INTEGER,
                    description="Always 1 on success (the named source).",
                ),
                "sessions_seen": ParameterMetadata(
                    type=ParameterType.INTEGER,
                    description="External sessions yielded by discover_sessions().",
                ),
                "events_persisted": ParameterMetadata(
                    type=ParameterType.INTEGER,
                    description="Clean events written.",
                ),
                "batches_failed": ParameterMetadata(
                    type=ParameterType.INTEGER,
                    description="Always 0 on success (failure raises instead).",
                ),
            },
        ),
        result_processor_customizations=MergeResultProcessorCustomizations(
            action_label="Poll Ledger Source",
            result_type="ledger_source_polled",
            result_description="One source was polled synchronously.",
        ),
        error_processor_customizations=MergeErrorProcessorCustomizations(retryable=False),
    )
    @abstractmethod
    def poll_source(self, source_id: str) -> dict[str, Any]:
        """Poll one source by id synchronously; raise on any failure."""

    @service_interface_process(
        name="ensure_periodic_poll_schedule",
        is_discoverable=False,  # boot-only; invoked by starting_actions, not the model
        provider=_PROVIDER,
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        parameters={
            "cadence_minutes": ParameterMetadata(
                type=ParameterType.INTEGER,
                description=(
                    "Poll cadence in minutes (1..59 inclusive; default 5). "
                    "Matches the ensure_global_heartbeat shape."
                ),
                required=False,
            ),
            "tag": ParameterMetadata(
                type=ParameterType.STRING,
                description="Schedule tag (default ``ledger:periodic_poll``).",
                required=False,
            ),
        },
        return_value_schema=ReturnValueSchema(
            description="Periodic-poll schedule ensure result.",
            type=ParameterType.OBJECT,
            properties={
                "status": ParameterMetadata(
                    type=ParameterType.STRING,
                    description="One of: created, already_present, normalized.",
                ),
                "schedule_id": ParameterMetadata(
                    type=ParameterType.STRING,
                    description="Active schedule id (may be empty when normalize-only).",
                ),
                "tag": ParameterMetadata(
                    type=ParameterType.STRING,
                    description="Schedule tag in use.",
                ),
                "cadence_minutes": ParameterMetadata(
                    type=ParameterType.INTEGER,
                    description="The cadence applied.",
                ),
                "cleared_count": ParameterMetadata(
                    type=ParameterType.INTEGER,
                    description="Stale/duplicate schedules cleared during normalization.",
                ),
            },
        ),
        result_processor_customizations=MergeResultProcessorCustomizations(
            action_label="Ensure Periodic Ledger Poll",
            result_type="ledger_periodic_poll_ensured",
            result_description="Periodic trigger_poll schedule ensured.",
        ),
        error_processor_customizations=MergeErrorProcessorCustomizations(retryable=True),
    )
    @abstractmethod
    def ensure_periodic_poll_schedule(
        self,
        cadence_minutes: int = 5,
        tag: str = "ledger:periodic_poll",
    ) -> dict[str, Any]:
        """Idempotently ensure a single periodic trigger_poll schedule exists.

        Mirrors :func:`scheduling_service.ensure_global_heartbeat` for the
        ledger surface: invokes ``clear_scheduled_actions_by_tag(tag)``
        then ``create_cron_schedule`` with a cron expression derived from
        ``cadence_minutes`` and an action that calls
        ``service_interface::session_ledger_service::trigger_poll``.

        Boot-only — wired via the profile's ``starting_actions`` so a
        fresh solet auto-polls the moment her services come up.
        """

    @service_interface_process(
        name="reset_ingest_state",
        is_discoverable=False,  # operator-only; not model-discoverable
        provider=_PROVIDER,
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        parameters={
            "confirm": ParameterMetadata(
                type=ParameterType.BOOLEAN,
                description=(
                    "MUST be ``True`` for the verb to actually reset the "
                    "source cursors. Default ``False`` returns a dry-run: the "
                    "active cursor count that WOULD be cleared per source plus "
                    "the live row counts of the content tables the reset "
                    "PRESERVES (NOTHING is deleted on the dry-run, and no "
                    "content is ever deleted). A confirmed reset is REFUSED "
                    "(raises) while any legacy null-``external_id`` events "
                    "remain — the dry-run reports that precondition."
                ),
                required=False,
            ),
        },
        return_value_schema=ReturnValueSchema(
            description=(
                "Non-destructive cursor-reset outcome. The reset clears every "
                "source's ``__source_cursor`` rows so the next poll replays + "
                "the ``(session_id, external_id)`` upsert reconverges; no "
                "content is deleted."
            ),
            type=ParameterType.OBJECT,
            properties={
                "confirmed": ParameterMetadata(
                    type=ParameterType.BOOLEAN,
                    description="True when the cursor reset actually ran.",
                ),
                "action": ParameterMetadata(
                    type=ParameterType.STRING,
                    description=(
                        "Always ``cursor_reset_replay`` — names the "
                        "non-destructive replay-and-reconverge semantics."
                    ),
                ),
                "content_preserved": ParameterMetadata(
                    type=ParameterType.BOOLEAN,
                    description=(
                        "Always True — content tables (session/event/tool_call/"
                        "attachment/import_batch) and leases are never deleted."
                    ),
                ),
                "sources_total": ParameterMetadata(
                    type=ParameterType.INTEGER,
                    description="Number of sources processed (all sources).",
                ),
                "active_cursor_count_before": ParameterMetadata(
                    type=ParameterType.INTEGER,
                    description=(
                        "Total active ``__source_cursor`` rows across all "
                        "sources at the start of the call — the cursors that "
                        "were (confirm) / would be (dry-run) cleared."
                    ),
                ),
                "deleted_count": ParameterMetadata(
                    type=ParameterType.INTEGER,
                    description=(
                        "Total cursor rows cleared across all sources. Zero on "
                        "the dry-run path; equals ``active_cursor_count_before`` "
                        "on a confirmed run."
                    ),
                ),
                "per_source": ParameterMetadata(
                    type=ParameterType.LIST,
                    description=(
                        "Per-source breakdown; each entry carries "
                        "{source_id, active_cursor_count_before, deleted_count}."
                    ),
                ),
                "preserved_content": ParameterMetadata(
                    type=ParameterType.LIST,
                    description=(
                        "Dry-run path only — live row counts of the content "
                        "tables the reset PRESERVES; each entry carries "
                        "{table, rows_preserved}."
                    ),
                ),
                "null_external_id_count": ParameterMetadata(
                    type=ParameterType.INTEGER,
                    description=(
                        "Dry-run path only — count of live events still carrying "
                        "a NULL ``external_id``. A confirmed reset is REFUSED "
                        "while this is > 0 (a re-walk would duplicate those "
                        "legacy rows); run ``backfill_event_external_ids`` first."
                    ),
                ),
                "precondition_met": ParameterMetadata(
                    type=ParameterType.BOOLEAN,
                    description=(
                        "Dry-run path only — True iff ``null_external_id_count`` "
                        "is 0, i.e. a confirmed reset is safe to run now."
                    ),
                ),
            },
        ),
        result_processor_customizations=MergeResultProcessorCustomizations(
            action_label="Reset Ledger Ingest State",
            result_type="ledger_ingest_state_reset",
            result_description=(
                "Every source's ingest cursor reset; next poll replays + the "
                "upsert reconverges. No content deleted."
            ),
        ),
        error_processor_customizations=MergeErrorProcessorCustomizations(retryable=False),
    )
    @abstractmethod
    def reset_ingest_state(self, confirm: bool = False) -> dict[str, Any]:
        """Reset every source's ingest cursor so the next poll replays + reconverges.

        GAP-5 slice 3 (idempotent-ingest design §4): NON-DESTRUCTIVE. Clears
        each source's ``session_ledger__source_cursor`` rows (the shipped
        per-source :meth:`reset_source_cursor`); the next poll pass re-walks
        every source from the start and the live ``(session_id, external_id)``
        upsert dedups the replayed events, so the ledger reconverges WITHOUT
        deleting any content. All content tables (``session``/``event``/
        ``tool_call``/``attachment``/``import_batch``) and the leases are
        PRESERVED — replay + upsert makes the old wipe unnecessary, and
        historical rows stay forensically intact.

        Use ``confirm=True`` to actually run. Default returns a dry-run with
        the per-source cursors that would be cleared plus the content the
        reset preserves, so the operator can size the reset before committing.

        NOTE — this no longer repopulates or corrects existing rows. Under the
        landed DO-NOTHING upsert a replayed event that already exists is a
        no-op, so the pre-GAP-5 "reset + restart to re-walk and repopulate
        schema columns" vendor-drift recovery use case is RETIRED: reset only
        dedups; it never rewrites the rows it replays.

        PRECONDITION (``confirm=True``): every event must already carry a
        non-null ``external_id``. A legacy null-``external_id`` row is not
        covered by the ``(session_id, external_id)`` unique (NULLs are DISTINCT
        in Postgres), so a post-reset re-walk would derive a non-null id that
        does not conflict with it and INSERT A DUPLICATE. The verb REFUSES
        (raises) while any null-``external_id`` events remain — run
        ``backfill_event_external_ids`` to 0 nulls first. The dry-run reports
        ``null_external_id_count`` + ``precondition_met``.
        """

    @service_interface_process(
        name="reset_source_cursor",
        is_discoverable=False,  # operator-only; not model-discoverable
        provider=_PROVIDER,
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        parameters={
            "source_id": ParameterMetadata(
                type=ParameterType.STRING,
                description=(
                    "Source row id (``src_...``) whose ``__source_cursor`` "
                    "rows should be reset. Verb is source-scoped; "
                    "``source_kind`` is intentionally NOT accepted because "
                    "it is not unique (chatgpt_export has one source row "
                    "per uploaded ZIP)."
                ),
                required=True,
            ),
            "confirm": ParameterMetadata(
                type=ParameterType.BOOLEAN,
                description=(
                    "MUST be ``True`` for the verb to actually hard-delete "
                    "the cursor rows. Default ``False`` returns a dry-run "
                    "with the active-cursor count so the operator can size "
                    "the reset before committing. A confirmed reset is REFUSED "
                    "(raises) while any legacy null-``external_id`` events "
                    "remain — the dry-run reports that precondition."
                ),
                required=False,
            ),
        },
        return_value_schema=ReturnValueSchema(
            description="Cursor-reset outcome.",
            type=ParameterType.OBJECT,
            properties={
                "confirmed": ParameterMetadata(
                    type=ParameterType.BOOLEAN,
                    description="True when the hard-delete actually ran.",
                ),
                "deleted_count": ParameterMetadata(
                    type=ParameterType.INTEGER,
                    description=(
                        "Number of ``__source_cursor`` rows hard-deleted. "
                        "Zero on dry-run paths and on idempotent reruns."
                    ),
                ),
                "source_id": ParameterMetadata(
                    type=ParameterType.STRING,
                    description="Echo of the supplied source id.",
                ),
                "active_cursor_count_before": ParameterMetadata(
                    type=ParameterType.INTEGER,
                    description=(
                        "Active cursor row count at the start of the verb "
                        "call (dry-run path only — included so the "
                        "operator can size the reset before confirming)."
                    ),
                ),
                "null_external_id_count": ParameterMetadata(
                    type=ParameterType.INTEGER,
                    description=(
                        "Dry-run path only — count of live events still "
                        "carrying a NULL ``external_id``. A confirmed reset is "
                        "REFUSED while this is > 0 (a re-walk would duplicate "
                        "those legacy rows); run ``backfill_event_external_ids`` "
                        "first."
                    ),
                ),
                "precondition_met": ParameterMetadata(
                    type=ParameterType.BOOLEAN,
                    description=(
                        "Dry-run path only — True iff ``null_external_id_count`` "
                        "is 0, i.e. a confirmed reset is safe to run now."
                    ),
                ),
            },
        ),
        result_processor_customizations=MergeResultProcessorCustomizations(
            action_label="Reset Source Cursor",
            result_type="ledger_source_cursor_reset",
            result_description=(
                "Cursor rows for one source hard-deleted; next poll pass "
                "re-walks from scratch."
            ),
        ),
        error_processor_customizations=MergeErrorProcessorCustomizations(retryable=False),
    )
    @abstractmethod
    def reset_source_cursor(
        self,
        source_id: str,
        confirm: bool = False,
    ) -> dict[str, Any]:
        """Hard-delete every active ``__source_cursor`` row for one source.

        Operator recovery verb for the case where a source's
        discovery / event-read cursors have advanced past a point the
        operator wants to re-walk from (chatgpt-export ZIP re-walk being
        the immediate case). Hard-delete (not soft) per the operator's
        soft-delete-is-opt-out principle — a cursor reset has no recovery
        path, and the re-creation path (``write_cursor``) inserts a fresh
        row when none exists.

        Source-scoped (NOT source-kind-scoped) per design v4 §D13:
        ``source_kind`` is not unique — chatgpt_export has one
        ``__source`` row per uploaded ZIP, and every other source-scoped
        operation in :mod:`service` takes ``source_id``, so the new
        verb follows that canonical shape.

        Use ``confirm=True`` to actually delete. Default ``confirm=False``
        returns a dry-run with the active-cursor count so the operator
        can size the reset before committing. Idempotent: re-running on
        a source with no active cursors returns ``deleted_count=0``.

        PRECONDITION (``confirm=True``): every event must already carry a
        non-null ``external_id`` — the same exposure as
        :meth:`reset_ingest_state`. Re-walking a source with legacy
        null-``external_id`` events would DUPLICATE them (NULLs are DISTINCT
        in the ``(session_id, external_id)`` unique), so the verb REFUSES
        (raises) while any remain — run ``backfill_event_external_ids`` to 0
        nulls first. The dry-run reports ``null_external_id_count`` +
        ``precondition_met``.
        """


class SessionLedgerCanonicalPointerRepairAPI(ABC):
    """One-shot operator verb: lift canonical pointer for duplicate (vendor, external_session_id) groups.

    Pre-flight repair for the M18 partial-unique index. Pause-resume envelope
    around the importer-poll cron prevents concurrent canonical-sibling
    INSERTs mid-repair. Split out from former ``SessionLedgerPollingAPI``
    per the v1 god-class split design.
    """

    @service_interface_process(
        name="lift_canonical_pointer_for_duplicate_sessions",
        is_discoverable=False,  # operator-only; not model-discoverable
        provider=_PROVIDER,
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        parameters={
            "confirm": ParameterMetadata(
                type=ParameterType.BOOLEAN,
                description=(
                    "MUST be ``True`` for the verb to actually lift "
                    "sibling rows. Default ``False`` returns a dry-run "
                    "with the duplicate-group count so the operator can "
                    "see the size of the repair before committing."
                ),
                required=False,
            ),
            "tag": ParameterMetadata(
                type=ParameterType.STRING,
                description=(
                    "Importer-poll schedule tag to pause+resume around "
                    "the repair (default ``ledger:periodic_poll`` to "
                    "match :meth:`ensure_periodic_poll_schedule`). The "
                    "pause prevents a concurrent poll from INSERTing a "
                    "third canonical sibling into a group mid-repair."
                ),
                required=False,
            ),
            "cadence_minutes": ParameterMetadata(
                type=ParameterType.INTEGER,
                description=(
                    "Cadence to re-ensure the importer-poll schedule at "
                    "in the finally-block (1..59 inclusive; default 5 "
                    "to match :meth:`ensure_periodic_poll_schedule`)."
                ),
                required=False,
            ),
        },
        return_value_schema=ReturnValueSchema(
            description=(
                "Lift result for duplicate canonical "
                "(vendor, external_session_id) groups."
            ),
            type=ParameterType.OBJECT,
            properties={
                "confirmed": ParameterMetadata(
                    type=ParameterType.BOOLEAN,
                    description="True when the lift actually ran.",
                ),
                "duplicate_group_count": ParameterMetadata(
                    type=ParameterType.INTEGER,
                    description=(
                        "Number of (vendor, external_session_id) groups "
                        "with ≥ 2 rows where "
                        "``canonical_external_session_id IS NULL AND "
                        "is_deleted = 0``, at start of the verb call."
                    ),
                ),
                "demoted_count": ParameterMetadata(
                    type=ParameterType.INTEGER,
                    description=(
                        "Number of sibling rows demoted from canonical-"
                        "NULL to canonical-pointer (one less than the "
                        "per-group row count, summed across groups)."
                    ),
                ),
                "pause_tag": ParameterMetadata(
                    type=ParameterType.STRING,
                    description="Importer-poll tag that was cleared and re-ensured.",
                ),
                "resume_outcome": ParameterMetadata(
                    type=ParameterType.STRING,
                    description=(
                        "Outcome of the finally-block "
                        "``ensure_periodic_poll_schedule`` re-ensure: "
                        "``created`` / ``normalized`` / ``skipped`` "
                        "(``skipped`` only on dry-run paths that never "
                        "paused)."
                    ),
                ),
            },
        ),
        result_processor_customizations=MergeResultProcessorCustomizations(
            action_label="Lift Canonical Pointer For Duplicate Sessions",
            result_type="ledger_canonical_duplicate_lift",
            result_description=(
                "Duplicate canonical (vendor, external_session_id) "
                "groups resolved by demoting siblings to the "
                "chronologically-first row."
            ),
        ),
        error_processor_customizations=MergeErrorProcessorCustomizations(retryable=False),
    )
    @abstractmethod
    def lift_canonical_pointer_for_duplicate_sessions(
        self,
        confirm: bool = False,
        tag: str = "ledger:periodic_poll",
        cadence_minutes: int = 5,
    ) -> dict[str, Any]:
        """One-shot pre-flight repair for the M18 partial-unique index landing.

        The M18 partial-unique index
        ``idx_session_canonical_one_per_vendor_pair`` will be created on
        the next blue-green cutover (after the schema_diff
        machinery fix). If existing ``__session`` rows have duplicate
        ``(vendor, external_session_id)`` pairs where BOTH rows have
        ``canonical_external_session_id IS NULL AND is_deleted = 0``,
        the CREATE UNIQUE INDEX op aborts and the green spawn FATALs.

        This verb resolves the data BEFORE the index lands: keeps the
        chronologically-first row per group as canonical and lifts every
        sibling to point at it via ``canonical_external_session_id =
        <first row's external_session_id>``. The pause-resume envelope
        around the importer-poll cron prevents a concurrent poll from
        INSERTing a third canonical sibling mid-repair.

        Use ``confirm=True`` to actually run. Default returns a dry-run
        with the duplicate-group count.
        """


class SessionLedgerInvertedBoundsRepairAPI(ABC):
    """One-shot operator verb: backfill repair ``__session`` rows with inverted event-at bounds.

    Per M6.5 Bug 2: after the upsert path's ``LEAST/GREATEST`` fix, this
    verb backfills any rows already inverted from pre-fix ingestion.
    Split out from former ``SessionLedgerPollingAPI`` per the v1
    god-class split design.
    """

    @service_interface_process(
        name="backfill_first_last_event_at_repair",
        is_discoverable=False,  # operator-only; not model-discoverable
        provider=_PROVIDER,
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        parameters={
            "confirm": ParameterMetadata(
                type=ParameterType.BOOLEAN,
                description=(
                    "MUST be ``True`` for the verb to actually repair "
                    "rows. Default ``False`` returns a dry-run with "
                    "the inverted-row count so the operator can see "
                    "the size of the repair before committing."
                ),
                required=False,
            ),
            "tag": ParameterMetadata(
                type=ParameterType.STRING,
                description=(
                    "Importer-poll schedule tag to pause+resume around "
                    "the repair (default ``ledger:periodic_poll`` to "
                    "match :meth:`ensure_periodic_poll_schedule`)."
                ),
                required=False,
            ),
            "cadence_minutes": ParameterMetadata(
                type=ParameterType.INTEGER,
                description=(
                    "Cadence to re-ensure the importer-poll schedule at "
                    "in the finally-block (1..59 inclusive; default 5 to "
                    "match :meth:`ensure_periodic_poll_schedule`)."
                ),
                required=False,
            ),
        },
        return_value_schema=ReturnValueSchema(
            description="Backfill repair result for inverted-bound sessions.",
            type=ParameterType.OBJECT,
            properties={
                "confirmed": ParameterMetadata(
                    type=ParameterType.BOOLEAN,
                    description="True when the repair actually ran.",
                ),
                "inverted_count": ParameterMetadata(
                    type=ParameterType.INTEGER,
                    description=(
                        "Number of ``__session`` rows whose "
                        "``last_event_at < first_event_at`` at start of "
                        "the verb call."
                    ),
                ),
                "repaired_count": ParameterMetadata(
                    type=ParameterType.INTEGER,
                    description=(
                        "Number of sessions actually repaired. Lower than "
                        "``inverted_count`` when some inverted rows had "
                        "zero ``__event`` rows (no event-side data to "
                        "compute MIN/MAX from); those are left in place "
                        "and surfaced via WARN-level log entries."
                    ),
                ),
                "pause_tag": ParameterMetadata(
                    type=ParameterType.STRING,
                    description="Importer-poll tag that was cleared and re-ensured.",
                ),
                "resume_outcome": ParameterMetadata(
                    type=ParameterType.STRING,
                    description=(
                        "Outcome of the finally-block "
                        "``ensure_periodic_poll_schedule`` re-ensure: one "
                        "of ``created`` / ``normalized`` / ``skipped`` "
                        "(``skipped`` only on dry-run paths that never "
                        "paused)."
                    ),
                ),
            },
        ),
        result_processor_customizations=MergeResultProcessorCustomizations(
            action_label="Backfill Repair Inverted Event-At Bounds",
            result_type="ledger_first_last_event_at_repair",
            result_description=(
                "Inverted-bound ``__session`` rows repaired from "
                "canonical MIN/MAX over ``__event`` rows."
            ),
        ),
        error_processor_customizations=MergeErrorProcessorCustomizations(retryable=False),
    )
    @abstractmethod
    def backfill_first_last_event_at_repair(
        self,
        confirm: bool = False,
        tag: str = "ledger:periodic_poll",
        cadence_minutes: int = 5,
    ) -> dict[str, Any]:
        """One-shot repair for ``__session`` rows with inverted event-at bounds.

        After the upsert path's ``LEAST/GREATEST`` fix lands (repository
        ``upsert_session``), this verb backfills any rows already inverted
        from pre-fix ingestion: recomputes ``first_event_at = MIN(event_at)``
        and ``last_event_at = MAX(event_at)`` over each session's ``__event``
        rows.

        Flow (wrap in try/finally so the importer-poll cron resumes even
        on exception):

        1. Pause the importer-poll cron via
           ``scheduling_service.clear_scheduled_actions_by_tag(tag)``.
        2. Loop over inverted-bound ``__session`` rows; per-row recompute
           MIN/MAX inside its own ``transactional()`` block (defense-in-
           depth row lock).
        3. ``finally``: re-ensure the importer-poll cron via
           :meth:`ensure_periodic_poll_schedule` so a partial-loop
           exception still resumes ingest.

        Use ``confirm=True`` to actually run. Default returns a dry-run
        response with the inverted-row count so the operator can size the
        repair before committing.
        """

    @service_interface_process(
        name="backfill_summary_embedding_vector_ids",
        is_discoverable=False,  # operator-only; not model-discoverable
        provider=_PROVIDER,
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        parameters={
            "confirm": ParameterMetadata(
                type=ParameterType.BOOLEAN,
                description=(
                    "MUST be ``True`` for the verb to actually rewrite "
                    "rows. Default ``False`` returns a dry-run with the "
                    "broken-pointer count so the operator can size the "
                    "repair before committing."
                ),
                required=False,
            ),
        },
        return_value_schema=ReturnValueSchema(
            description=(
                "Backfill result for ``__summary`` rows whose "
                "``embedding_vector_id`` was written as the pgvector "
                "internal id instead of the deterministic external_id."
            ),
            type=ParameterType.OBJECT,
            properties={
                "confirmed": ParameterMetadata(
                    type=ParameterType.BOOLEAN,
                    description="True when the repair actually ran.",
                ),
                "updated_count": ParameterMetadata(
                    type=ParameterType.INTEGER,
                    description=(
                        "Number of ``__summary`` rows whose "
                        "``embedding_vector_id`` was rewritten from a "
                        "pgvector internal id (``emb-...``) to the "
                        "matching deterministic external_id."
                    ),
                ),
                "skipped_count": ParameterMetadata(
                    type=ParameterType.INTEGER,
                    description=(
                        "Number of rows STILL carrying an internal "
                        "``emb-`` pointer at completion. The repair "
                        "recomputes ``external_id`` ledger-side from each "
                        "row's own ``session_id`` + ``chunk_index``, so a "
                        "row is left in place (and WARN-logged) only when "
                        "it lacks those columns."
                    ),
                ),
                "total_rows_now_correct": ParameterMetadata(
                    type=ParameterType.INTEGER,
                    description=(
                        "Total active ``__summary`` rows whose "
                        "``embedding_vector_id`` is now in the canonical "
                        "external_id shape (i.e. addressable by the ANN "
                        "search join)."
                    ),
                ),
            },
        ),
        result_processor_customizations=MergeResultProcessorCustomizations(
            action_label="Backfill Summary Embedding Vector IDs",
            result_type="ledger_summary_embedding_vector_id_repair",
            result_description=(
                "Stale ``__summary.embedding_vector_id`` values rewritten "
                "from pgvector internal ids to canonical external_ids so "
                "the ANN search join reaches them."
            ),
        ),
        error_processor_customizations=MergeErrorProcessorCustomizations(retryable=False),
    )
    @abstractmethod
    def backfill_summary_embedding_vector_ids(
        self,
        confirm: bool = False,
    ) -> dict[str, Any]:
        """One-shot repair for ``__summary`` rows whose ``embedding_vector_id``
        was written with the pgvector internal id instead of the
        deterministic ``{session_id}:{chunk_index}`` external_id.

        Per commit ``4ea5eda81`` (2026-06-10): pre-fix ``_store_vector``
        returned ``inserted_ids[0]`` (pgvector internal id); persist_summary
        wrote that into ``embedding_vector_id``. The ANN search path keys
        its score map by ``embedding.external_id`` and
        :meth:`list_summaries_by_external_ids` looks up summary rows via
        ``WHERE embedding_vector_id IN (external_ids)`` — the IN clause
        never matched and ``search_sessions`` silently returned ``[]``.

        Use ``confirm=True`` to actually run. Default returns a dry-run
        response with the broken-pointer count so the operator can size
        the repair before committing.
        """

    @service_interface_process(
        name="backfill_orphan_running_batches_for_source",
        is_discoverable=False,  # operator-only; not model-discoverable
        provider=_PROVIDER,
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        parameters={
            "source_id": ParameterMetadata(
                type=ParameterType.STRING,
                description=(
                    "Source row id (``src_...``) whose stale "
                    "``__import_batch`` orphans should be repaired."
                ),
                required=True,
            ),
            "source_kind": ParameterMetadata(
                type=ParameterType.STRING,
                description=(
                    "Optional ``IngestSourceKind`` StrEnum value used as "
                    "a defense-in-depth cross-check against the source "
                    "row's actual kind. A mismatch returns all-zero counts "
                    "(no UPDATE) so an operator pointing at the wrong "
                    "source by id sees an honest no-op."
                ),
                required=False,
            ),
            "confirm": ParameterMetadata(
                type=ParameterType.BOOLEAN,
                description=(
                    "MUST be ``True`` for the verb to actually rewrite "
                    "rows. Default ``False`` returns a dry-run with the "
                    "orphan counts so the operator can size the repair "
                    "before committing."
                ),
                required=False,
            ),
            "stale_threshold_seconds": ParameterMetadata(
                type=ParameterType.INTEGER,
                description=(
                    "Age (seconds) above which a running batch is "
                    "considered stale and gets ``status='failed'`` + "
                    "``error_kind='orphan_repair'``. Default 86400 (24 h)."
                ),
                required=False,
            ),
        },
        return_value_schema=ReturnValueSchema(
            description="Orphan-batch repair result for one source.",
            type=ParameterType.OBJECT,
            properties={
                "confirmed": ParameterMetadata(
                    type=ParameterType.BOOLEAN,
                    description="True when the repair actually ran.",
                ),
                "repaired_count": ParameterMetadata(
                    type=ParameterType.INTEGER,
                    description=(
                        "Net reduction in RUNNING batches for the source "
                        "(``total_orphan_count_before - untouched_count``), "
                        "measured from a post-repair recount. In the "
                        "single-writer case these are exactly the stale "
                        "rows this repair marked failed with "
                        "``error_kind='orphan_repair'``; the post-state "
                        "count also absorbs any a separate owner completed "
                        "out of RUNNING mid-repair."
                    ),
                ),
                "untouched_count": ParameterMetadata(
                    type=ParameterType.INTEGER,
                    description=(
                        "Number of batch rows STILL RUNNING for the source "
                        "after the repair — primarily those within the "
                        "stale-grace window (``started_at`` >= ``now - "
                        "stale_threshold_seconds``), left in place."
                    ),
                ),
                "total_orphan_count_before": ParameterMetadata(
                    type=ParameterType.INTEGER,
                    description=(
                        "Total running batch count for the source at "
                        "the start of the verb call. Invariant: "
                        "``total_orphan_count_before == repaired_count "
                        "+ untouched_count``."
                    ),
                ),
                "source_id": ParameterMetadata(
                    type=ParameterType.STRING,
                    description="Echo of the supplied source id.",
                ),
            },
        ),
        result_processor_customizations=MergeResultProcessorCustomizations(
            action_label="Backfill Orphan Running Batches for Source",
            result_type="ledger_orphan_running_batch_repair",
            result_description=(
                "Stale running ``__import_batch`` rows for one source "
                "marked failed with ``error_kind='orphan_repair'``."
            ),
        ),
        error_processor_customizations=MergeErrorProcessorCustomizations(retryable=False),
    )
    @abstractmethod
    def backfill_orphan_running_batches_for_source(
        self,
        source_id: str,
        source_kind: str | None = None,
        confirm: bool = False,
        stale_threshold_seconds: int = 86400,
    ) -> dict[str, Any]:
        """One-shot repair for stale orphan ``__import_batch`` rows on one source.

        Targets the orphan-running-batch class first surfaced
        by the chatgpt upload route's kickoff-failure path and reinforced
        by Wave-4a's crash-after-claim path: batches whose owner never
        terminated them sit at ``status='running'`` indefinitely.

        Per Codex impl note: this verb does NOT filter on
        ``polling_lease_token`` so token-owned batches whose owner
        crashed after claim are still reachable.

        Defense-in-depth: ``source_kind``, when supplied, is cross-checked
        against the source row's actual kind. A mismatch returns
        all-zero counts so an operator pointing at the wrong source by
        id sees an honest no-op rather than touching the wrong rows.

        Use ``confirm=True`` to actually run. Default ``confirm=False``
        returns a structured dry-run with the orphan counts so the
        operator can size the repair before committing.
        """

    @service_interface_process(
        name="retire_duplicate_source",
        is_discoverable=False,  # operator-only; not model-discoverable
        provider=_PROVIDER,
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        parameters={
            "winner_source_id": ParameterMetadata(
                type=ParameterType.STRING,
                description=(
                    "Source row id (``src_...``) to KEEP — every child row "
                    "currently pointing at ``loser_source_id`` is re-pointed "
                    "here."
                ),
                required=True,
            ),
            "loser_source_id": ParameterMetadata(
                type=ParameterType.STRING,
                description=(
                    "Source row id (``src_...``) to RETIRE — soft-deleted "
                    "once every child row has been re-pointed to "
                    "``winner_source_id`` and it is confirmed orphaned. "
                    "Must be ``enabled=false`` (quiesce protocol) and carry "
                    "no active polling lease before ``confirm=True`` — the "
                    "verb refuses otherwise."
                ),
                required=True,
            ),
            "confirm": ParameterMetadata(
                type=ParameterType.BOOLEAN,
                description=(
                    "MUST be ``True`` for the verb to actually re-point "
                    "child rows and soft-delete the loser. Default "
                    "``False`` returns a dry-run with the per-table child "
                    "counts that would move plus the loser's ``enabled`` "
                    "flag, so the operator can size and quiesce-check the "
                    "repair before committing."
                ),
                required=False,
            ),
        },
        return_value_schema=ReturnValueSchema(
            description="Duplicate-source retirement outcome.",
            type=ParameterType.OBJECT,
            properties={
                "confirmed": ParameterMetadata(
                    type=ParameterType.BOOLEAN,
                    description="True when the re-point + soft-delete actually ran.",
                ),
                "winner_source_id": ParameterMetadata(
                    type=ParameterType.STRING,
                    description="Echo of the supplied winner id.",
                ),
                "loser_source_id": ParameterMetadata(
                    type=ParameterType.STRING,
                    description="Echo of the supplied loser id.",
                ),
                "children_to_move": ParameterMetadata(
                    type=ParameterType.OBJECT,
                    description=(
                        "Dry-run path only — per-table live row count "
                        "currently referencing the loser "
                        "(session/import_batch/source_cursor/active_lease)."
                    ),
                ),
                "loser_enabled": ParameterMetadata(
                    type=ParameterType.BOOLEAN,
                    description=(
                        "Dry-run path only — the loser's current ``enabled`` "
                        "flag. Must be ``false`` before ``confirm=True`` will "
                        "proceed."
                    ),
                ),
                "children_moved": ParameterMetadata(
                    type=ParameterType.OBJECT,
                    description=(
                        "Confirmed path only — per-table rows-affected by "
                        "the re-point."
                    ),
                ),
                "loser_retired": ParameterMetadata(
                    type=ParameterType.BOOLEAN,
                    description=(
                        "Confirmed path only — True iff the loser source row "
                        "was soft-deleted (False only on an already-deleted "
                        "row, which is otherwise unreachable given the "
                        "verb's own not-found check)."
                    ),
                ),
            },
        ),
        result_processor_customizations=MergeResultProcessorCustomizations(
            action_label="Retire Duplicate Source",
            result_type="ledger_duplicate_source_retired",
            result_description=(
                "One duplicate source row's children re-pointed to its "
                "canonical winner; the loser row soft-deleted."
            ),
        ),
        error_processor_customizations=MergeErrorProcessorCustomizations(retryable=False),
    )
    @abstractmethod
    def retire_duplicate_source(
        self,
        winner_source_id: str,
        loser_source_id: str,
        confirm: bool = False,
    ) -> dict[str, Any]:
        """Retire a duplicate ``source`` row into its canonical winner.

        Schema-debt-external-id lane, 2b-S1 (2026-08-06). Operator-only
        repair for the duplicate-source-registration defect measured live
        (8 duplicated ``source_kind``s at diagnosis time) — full rationale,
        winner-selection rules, and the required per-pair quiesce sequence
        (disable both rows, wait out any active polling lease, repair,
        re-enable the winner) are in the schema-debt external-id findings
        record (dev-checkout workbench — not part of the shipped tree).

        Use ``confirm=True`` to actually write. Default ``confirm=False``
        returns a structured dry-run.

        Soft-delete only, never hard — the retired row's rollback lever.

        Refuses (raises ``ValueError``) on ``confirm=True`` when: the two
        ids are identical; either source row is missing/deleted; the two
        rows have different ``source_kind``s; OR the loser is still
        ``enabled`` OR still shows an active polling lease — both are
        quiesce-protocol preconditions enforced HERE, not merely trusted to
        an external caller, so a repair cannot race the live poller even if
        the quiesce steps were skipped upstream. Refuses (raises
        ``LedgerRepositoryError``) if any child row still references the
        loser after the re-point.
        """


class SessionLedgerEventSourceDenormBackfillAPI(ABC):
    """One-shot operator verb: backfill ``__event.session_vendor`` + ``source_kind``.

    SQL-lockdown #0 Slice 7 companion. The Architect-ruled denormalization adds
    ``session_vendor`` + ``source_kind`` to ``__event`` so
    ``list_events_by_source_window`` reads one table instead of a 3-table JOIN.
    ``append_event`` writes both on every NEW event, so this verb only fills the
    pre-migration rows. Idempotent + fill-only (touches only
    ``session_vendor IS NULL`` events) so — unlike a destructive backfill — it
    needs no ``confirm`` gate, and it is race-free with ongoing ingest (new
    events are born non-NULL, so the NULL filter never overlaps a live writer).
    """

    @service_interface_process(
        name="backfill_event_source_denormalization",
        is_discoverable=False,  # operator-only; not model-discoverable
        provider=_PROVIDER,
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        parameters={},
        return_value_schema=ReturnValueSchema(
            description=(
                "Counts from the one-shot event source-denormalization backfill."
            ),
            type=ParameterType.OBJECT,
            properties={
                "sessions_scanned": ParameterMetadata(
                    type=ParameterType.INTEGER,
                    description="Number of __session rows visited (id-keyset paged).",
                ),
                "events_denormalized": ParameterMetadata(
                    type=ParameterType.INTEGER,
                    description=(
                        "Number of __event rows whose NULL session_vendor + "
                        "source_kind were filled this run (0 on a converged "
                        "ledger — re-run to confirm completeness)."
                    ),
                ),
            },
        ),
        result_processor_customizations=MergeResultProcessorCustomizations(
            action_label="Backfill Event Source Denormalization",
            result_type="ledger_event_source_denorm_backfill",
            result_description=(
                "Pre-migration __event rows had their session_vendor + "
                "source_kind backfilled from the authoritative session→source "
                "join (Slice 7)."
            ),
        ),
        error_processor_customizations=MergeErrorProcessorCustomizations(retryable=False),
    )
    @abstractmethod
    def backfill_event_source_denormalization(self) -> dict[str, Any]:
        """Fill the Slice-7 denormalized ``__event`` columns on pre-migration rows.

        Fails loud (``LedgerRepositoryError``) if any session's ``source_id``
        resolves to no live ``__source`` row — the original INNER JOIN dropped
        those events, so a NULL ``source_kind`` fill would wrongly leak them into
        vendor-only pulls (a data anomaly that must stop the backfill, not be
        silently skipped). Returns ``{sessions_scanned, events_denormalized}``.
        """


class SessionLedgerEventExternalIdBackfillAPI(ABC):
    """One-shot operator verb: backfill GAP-5 ``external_id`` on legacy events.

    GAP-5 idempotent-ingest slice 2. Slice 1 added the live ``external_id``
    derivation + the ``(session_id, external_id)`` unique index; rows that
    predate it carry NULL ``external_id``. This verb stamps every legacy row with
    the SAME derivation the live importer uses so a historical re-ingest dedups,
    then the operator can land the ``external_id`` NOT-NULL constraint (slice 2b).
    Idempotent + fill-only (no ``confirm`` gate); a live-window collision (a
    historical event re-ingested after slice-1 deploy already owns the derived
    id) is skip-and-counted, never overwritten.
    """

    @service_interface_process(
        name="backfill_event_external_ids",
        is_discoverable=False,  # operator-only; not model-discoverable
        provider=_PROVIDER,
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        parameters={},
        return_value_schema=ReturnValueSchema(
            description="Counts from the one-shot external_id backfill.",
            type=ParameterType.OBJECT,
            properties={
                "sessions_scanned": ParameterMetadata(
                    type=ParameterType.INTEGER,
                    description="Number of __session rows visited (id-keyset paged).",
                ),
                "events_stamped": ParameterMetadata(
                    type=ParameterType.INTEGER,
                    description=(
                        "Number of __event rows whose NULL external_id was "
                        "stamped this run (0 on a converged ledger — re-run to "
                        "confirm completeness)."
                    ),
                ),
                "collisions_skipped": ParameterMetadata(
                    type=ParameterType.INTEGER,
                    description=(
                        "Number of legacy null rows whose derived id already "
                        "belonged to a live-window duplicate; left NULL (the "
                        "operator disposes of them before the slice-2b NOT-NULL)."
                    ),
                ),
            },
        ),
        result_processor_customizations=MergeResultProcessorCustomizations(
            action_label="Backfill Event External IDs",
            result_type="ledger_event_external_id_backfill",
            result_description=(
                "Pre-slice-1 __event rows had their GAP-5 external_id stamped "
                "with the live importer's derivation so historical re-ingest "
                "dedups (slice 2)."
            ),
        ),
        error_processor_customizations=MergeErrorProcessorCustomizations(retryable=False),
    )
    @abstractmethod
    def backfill_event_external_ids(self) -> dict[str, Any]:
        """Stamp GAP-5 ``external_id`` on pre-slice-1 null-``external_id`` rows.

        Reproduces the live importer derivation (``vendor_event_id`` ?? a
        content-addressed ``derv:`` hash over the source-order occurrence
        ordinal), fetching offloaded blob content when the stored
        ``content_text`` is NULL. Idempotent, session-keyset paged, skip-and-count
        on a live-window collision. Returns
        ``{sessions_scanned, events_stamped, collisions_skipped}``.
        """


class SessionLedgerSessionSourceKindBackfillAPI(ABC):
    """One-shot operator verb: backfill the ``session_source_kind`` junction.

    SQL-lockdown list_sessions companion. The Architect-ruled junction backs
    ``list_sessions``' source_kind filter (read-then-route). ``upsert_session``
    maintains it on the ingest attach-path for NEW sessions, so this verb only
    fills the pre-migration rows — until it runs, ``list_sessions(source_kind=K)``
    returns ``[]`` for every historical session. Idempotent + additive (DO-NOTHING
    on the UNIQUE ``(canonical_session_id, source_kind)``) so it needs no
    ``confirm`` gate and is race-free with ongoing ingest. Run
    ``lift_canonical_pointer_for_duplicate_sessions`` FIRST — a group with no
    canonical row fails the backfill loud.
    """

    @service_interface_process(
        name="backfill_session_source_kinds",
        is_discoverable=False,  # operator-only; not model-discoverable
        provider=_PROVIDER,
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        parameters={},
        return_value_schema=ReturnValueSchema(
            description=(
                "Counts from the one-shot session_source_kind junction backfill."
            ),
            type=ParameterType.OBJECT,
            properties={
                "sessions_scanned": ParameterMetadata(
                    type=ParameterType.INTEGER,
                    description="Number of __session rows visited (id-keyset paged).",
                ),
                "junction_rows_written": ParameterMetadata(
                    type=ParameterType.INTEGER,
                    description=(
                        "Number of NEW (canonical_session_id, source_kind) pairs "
                        "inserted this run (0 on a converged ledger — re-run to "
                        "confirm completeness)."
                    ),
                ),
            },
        ),
        result_processor_customizations=MergeResultProcessorCustomizations(
            action_label="Backfill Session Source Kind Junction",
            result_type="ledger_session_source_kind_backfill",
            result_description=(
                "Pre-migration session groups had their distinct source_kinds "
                "recorded in the session_source_kind junction (keyed by canonical "
                "session id) so list_sessions' source_kind filter finds them."
            ),
        ),
        error_processor_customizations=MergeErrorProcessorCustomizations(retryable=False),
    )
    @abstractmethod
    def backfill_session_source_kinds(self) -> dict[str, Any]:
        """Populate the ``session_source_kind`` junction for pre-migration groups.

        Fails loud (``LedgerRepositoryError``) if a group has no canonical row
        (run ``lift_canonical_pointer_for_duplicate_sessions`` first) or a session
        references a missing source. Returns
        ``{sessions_scanned, junction_rows_written}``.
        """


class SessionLedgerSummarizeAPI(ABC):
    """M6 summarization surface: codex-stage1 seed lift + auto-summarize cron + idempotent installer.

    Split out from former ``SessionLedgerPollingAPI`` per the v1 god-class
    split design.
    """

    @service_interface_process(
        name="lift_codex_stage1_summaries",
        is_discoverable=False,  # operator-only; not model-discoverable
        provider=_PROVIDER,
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        parameters={
            "confirm": ParameterMetadata(
                type=ParameterType.BOOLEAN,
                description=(
                    "MUST be ``True`` for the verb to actually rewrite "
                    "rows. Default ``False`` returns a dry-run with the "
                    "candidate counts so the operator can size the lift "
                    "before committing."
                ),
                required=False,
            ),
            "tag": ParameterMetadata(
                type=ParameterType.STRING,
                description=(
                    "M6 auto-summarize schedule tag to pause+resume "
                    "around the lift (default "
                    "``ledger:periodic_summarize`` to match "
                    ":meth:`ensure_periodic_summarize_schedule`). The "
                    "lift PAUSES the SUMMARIZE cron, NOT the poll cron."
                ),
                required=False,
            ),
            "cadence_minutes": ParameterMetadata(
                type=ParameterType.INTEGER,
                description=(
                    "Cadence to re-ensure the summarize schedule at in "
                    "the finally-block (1..59 inclusive; default 10 to "
                    "match :meth:`ensure_periodic_summarize_schedule`)."
                ),
                required=False,
            ),
        },
        return_value_schema=ReturnValueSchema(
            description=(
                "Codex stage1 summary-lift outcome — G8 race-mitigated "
                "one-shot rewrite pass."
            ),
            type=ParameterType.OBJECT,
            properties={
                "confirmed": ParameterMetadata(
                    type=ParameterType.BOOLEAN,
                    description="True when the rewrite actually ran.",
                ),
                "stage1_row_count": ParameterMetadata(
                    type=ParameterType.INTEGER,
                    description=(
                        "``stage1_outputs`` rows considered "
                        "(``selected_for_phase2 = 1``)."
                    ),
                ),
                "candidate_count": ParameterMetadata(
                    type=ParameterType.INTEGER,
                    description=(
                        "Stage1 rows that match an existing ``__session`` "
                        "row by ``external_session_id = thread_id``. The "
                        "candidate-set is the universe the lift operates "
                        "on."
                    ),
                ),
                "lifted_count": ParameterMetadata(
                    type=ParameterType.INTEGER,
                    description=(
                        "Sessions actually rewritten this call. On "
                        "``confirm=False`` this is 0 (dry-run)."
                    ),
                ),
                "pause_tag": ParameterMetadata(
                    type=ParameterType.STRING,
                    description="M6 summarize cron tag cleared + re-ensured.",
                ),
                "resume_outcome": ParameterMetadata(
                    type=ParameterType.STRING,
                    description=(
                        "Outcome of the finally-block "
                        "``ensure_periodic_summarize_schedule`` re-ensure: "
                        "one of ``created`` / ``normalized`` / ``skipped``."
                    ),
                ),
            },
        ),
        result_processor_customizations=MergeResultProcessorCustomizations(
            action_label="Lift Codex Stage1 Summaries Into Ledger",
            result_type="ledger_codex_stage1_lift",
            result_description=(
                "Codex stage1_outputs rollout_summary text lifted into "
                "matching ``__session.summary_text`` rows with "
                "``internal:auto_summarize:codex_stage1_seed`` attribution."
            ),
        ),
        error_processor_customizations=MergeErrorProcessorCustomizations(retryable=False),
    )
    @abstractmethod
    def lift_codex_stage1_summaries(
        self,
        confirm: bool = False,
        tag: str = "ledger:periodic_summarize",
        cadence_minutes: int = 10,
    ) -> dict[str, Any]:
        """G8-mitigated one-shot rewrite of ``__session.summary_text`` from Codex stage1.

        Reads ``stage1_outputs`` (default filter
        ``selected_for_phase2 = 1`` — the Codex phase-2 algorithm's
        canonical-input subset), joins to existing ``__session`` rows by
        ``external_session_id = thread_id``, and rewrites each match's
        ``summary_text`` to the stage1 ``rollout_summary`` with
        ``internal:auto_summarize:codex_stage1_seed`` attribution on the
        chunk push.

        Flow (wrap in ``try/finally`` so the M6 cron resumes even on
        exception):

        1. Pause the M6 summarize cron via
           ``scheduling_service.clear_scheduled_actions_by_tag(tag)``
           (tag defaults to ``ledger:periodic_summarize`` — NOT the
           poll cron tag).
        2. Per candidate row in ONE transaction: SELECT FOR UPDATE the
           ``__session`` row, UPDATE ``summary_text`` to rollout_summary,
           DELETE stale ``__summary`` ``chunk_index = 0`` row to avoid
           UPSERT-vs-INSERT-OR-FAIL ambiguity, then
           ``push_summary_chunk`` for fresh embedding generation.
        3. ``finally``: re-ensure the M6 summarize cron via
           :meth:`ensure_periodic_summarize_schedule` so a partial-loop
           exception still resumes the cron.

        Idempotent: re-running after a successful lift is a no-op
        because the second pass sees ``summary_text`` already equals the
        ``rollout_summary`` (UPDATE writes the same value; DELETE +
        push_summary_chunk regenerates the embedding harmlessly).

        Use ``confirm=True`` to actually rewrite. Default returns a
        dry-run with candidate counts so the operator can size the
        lift before committing.
        """

    @service_interface_process(
        name="summarize_quiescent_sessions",
        is_discoverable=False,  # cron-fired only; not model-discoverable
        provider=_PROVIDER,
        # EDGE_SINK per the canonical scheduler cron-action contract — same
        # rationale as ``trigger_poll`` above. Terminal action; no
        # result/error processor attached.
        processor_policy_category=ProcessorPolicyCategory.EDGE_SINK,
        parameters={
            "quiescence_minutes": ParameterMetadata(
                type=ParameterType.INTEGER,
                description=(
                    "A session is eligible for auto-summarization only when "
                    "its ``last_event_at`` is older than this many minutes "
                    "(default 10). Picks up the 'session has settled' "
                    "signal without trying to summarize mid-stream."
                ),
                required=False,
            ),
            "batch_size": ParameterMetadata(
                type=ParameterType.INTEGER,
                description=(
                    "Per-iteration page size the drainer pulls from "
                    "``list_quiescent_sessions`` (default 50; clamped by the "
                    "read to 1..50). NOT a per-fire cap — the drainer loops "
                    "until nothing eligible remains."
                ),
                required=False,
            ),
        },
        return_value_schema=ReturnValueSchema(
            description=(
                "Drainer disposition for this cron fire. The drain runs "
                "asynchronously on a background thread, so per-session counts "
                "are logged when it completes, not returned here."
            ),
            type=ParameterType.OBJECT,
            properties={
                "drainer": ParameterMetadata(
                    type=ParameterType.STRING,
                    description=(
                        "``started`` if this fire launched the singleton "
                        "drain-until-empty pass, or ``already_running`` if a "
                        "drainer was already active (this fire was a no-op)."
                    ),
                ),
                "quiescence_minutes": ParameterMetadata(
                    type=ParameterType.INTEGER,
                    description="Quiescence cutoff (minutes) applied by the drain.",
                ),
                "batch_size": ParameterMetadata(
                    type=ParameterType.INTEGER,
                    description="Per-iteration page size the drain used.",
                ),
            },
        ),
        # Terminal/headless cron-fired action per the canonical scheduler
        # cron-action contract: same rationale as ``trigger_poll`` above. No
        # success-path scaffold; no error-path inference.
    )
    @abstractmethod
    def summarize_quiescent_sessions(
        self,
        quiescence_minutes: int = 10,
        batch_size: int = 50,
    ) -> dict[str, Any]:
        """Cron heartbeat: (re)start the singleton drain-until-empty summarizer.

        Each cron fire is a heartbeat that does NO summarization on the
        action-queue thread — it only tries to start ONE background drainer
        (returning ``{"drainer": "started"}``) or no-ops if one is already
        running (``{"drainer": "already_running"}``). The drainer loops over
        quiescent sessions (``last_event_at`` older than ``quiescence_minutes``
        and not yet embedded), summarizing each in series until none remain:
        for every session it assembles a bounded event timeline, produces a
        compact summary (custom-title seed / away_summary recap inline, else a
        synchronous ``inference_service`` completion), and pushes it through
        :meth:`SummaryWriter.push_summary_chunk` (embedding + vector store, and
        per Gap 2(B) the denormalize back into ``session_ledger__session``).

        Cron-fired via :meth:`ensure_periodic_summarize_schedule`. The
        ``generated_by_client_id`` is the constant ``"internal:auto_summarize"``
        (per-branch discriminator suffix) because no authenticated principal
        accompanies a system-driven cron firing. ``batch_size`` is the drainer's
        per-iteration page size, NOT a per-fire cap.
        """

    @service_interface_process(
        name="ensure_periodic_summarize_schedule",
        is_discoverable=False,  # boot-only; invoked by starting_actions, not the model
        provider=_PROVIDER,
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        parameters={
            "cadence_minutes": ParameterMetadata(
                type=ParameterType.INTEGER,
                description=(
                    "Cron cadence in minutes (1..59 inclusive; default 10). "
                    "Each fire runs :meth:`summarize_quiescent_sessions`."
                ),
                required=False,
            ),
            "tag": ParameterMetadata(
                type=ParameterType.STRING,
                description="Schedule tag (default ``ledger:periodic_summarize``).",
                required=False,
            ),
        },
        return_value_schema=ReturnValueSchema(
            description="Periodic summarize-schedule ensure result.",
            type=ParameterType.OBJECT,
            properties={
                "status": ParameterMetadata(
                    type=ParameterType.STRING,
                    description="One of: created, already_present, normalized.",
                ),
                "schedule_id": ParameterMetadata(
                    type=ParameterType.STRING,
                    description="Active schedule id.",
                ),
                "tag": ParameterMetadata(
                    type=ParameterType.STRING,
                    description="Schedule tag in use.",
                ),
                "cadence_minutes": ParameterMetadata(
                    type=ParameterType.INTEGER,
                    description="Cadence applied.",
                ),
                "cleared_count": ParameterMetadata(
                    type=ParameterType.INTEGER,
                    description="Stale/duplicate schedules cleared.",
                ),
            },
        ),
        result_processor_customizations=MergeResultProcessorCustomizations(
            action_label="Ensure Periodic Ledger Auto-Summarize",
            result_type="ledger_periodic_summarize_ensured",
            result_description="Periodic auto-summarize cron ensured.",
        ),
        error_processor_customizations=MergeErrorProcessorCustomizations(retryable=True),
    )
    @abstractmethod
    def ensure_periodic_summarize_schedule(
        self,
        cadence_minutes: int = 10,
        tag: str = "ledger:periodic_summarize",
    ) -> dict[str, Any]:
        """Idempotently install a cron firing :meth:`summarize_quiescent_sessions`.

        Mirrors :meth:`ensure_periodic_poll_schedule` for the M6
        auto-summarize path. Boot-only — wired via the profile's
        ``starting_actions`` so a fresh solet starts summarizing
        her own sessions the moment she comes up (operator ruling
        2026-05-31 Gap 2(A): summaries auto-flow, no external caller
        required).
        """


# ───────────────────────────────────────────────────────────────────────────
# M5 — shipper bootstrap / pairing surface (spec §13)
# ───────────────────────────────────────────────────────────────────────────
#
# Sibling ABC for M5 shipper-bootstrap/pairing concerns (kept distinct
# from the M1 ingest/read/triage ABCs because the authz model differs).
# The ServiceInterfaceScanner walks every class in this module by
# attribute marker, so adding siblings does not require any scanner
# change.


class SessionLedgerDeploymentAPI(ABC):
    """M5 shipper-bootstrap + pairing surface (spec §13).

    Three operations whose authz model differs from the M1 ingest
    surface — pairing flow + ownership-binding + server-derived target
    for self-revoke. Kept as a sibling ABC because the concerns are
    distinct enough that mixing them into the read/ingest/triage ABCs
    would blur responsibility.
    """

    @service_interface_process(
        name="generate_ingest_setup",
        is_discoverable=True,
        provider=_PROVIDER,
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        parameters={
            "operating_system": ParameterMetadata(
                type=ParameterType.STRING,
                description="Target OS for the installer ('macos' | 'linux').",
                required=True,
            ),
            "sources": ParameterMetadata(
                type=ParameterType.LIST,
                description=(
                    "List of source_kind strings the shipper is authorized to "
                    "ingest into (Codex item 10 binding)."
                ),
                required=True,
            ),
            "machine_id": ParameterMetadata(
                type=ParameterType.STRING,
                description="Operator-supplied installation identifier (NOT MAC-derived).",
                required=True,
            ),
            "state": ParameterMetadata(
                type=ParameterType.OBJECT,
                description=(
                    "Server-injected. Carries authenticated_principal; the "
                    "caller's client_id is persisted as initiating_client_id "
                    "for the §13 ownership-binding check on approve_pairing."
                ),
                required=False,
            ),
        },
        return_value_schema=ReturnValueSchema(
            description="Deployment registration for the shipper installer.",
            type=ParameterType.OBJECT,
            properties={
                "deployment_id": ParameterMetadata(
                    type=ParameterType.STRING,
                    description="New deployment row id (dep-...).",
                ),
                "machine_id": ParameterMetadata(
                    type=ParameterType.STRING,
                    description="Echo of the operator-supplied machine_id.",
                ),
                "authorized_source_kinds": ParameterMetadata(
                    type=ParameterType.LIST,
                    description="The source_kinds bound to this deployment.",
                ),
            },
        ),
        result_processor_customizations=MergeResultProcessorCustomizations(
            action_label="Generate Ingest Setup",
            result_type="ledger_ingest_setup",
            result_description="A pending shipper deployment was created.",
        ),
        error_processor_customizations=MergeErrorProcessorCustomizations(retryable=False),
    )
    @abstractmethod
    def generate_ingest_setup(
        self,
        operating_system: str,
        sources: list[str],
        machine_id: str,
        state: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Create a pending shipper deployment row + return its registration."""

    @service_interface_process(
        name="approve_pairing",
        is_discoverable=True,
        provider=_PROVIDER,
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        parameters={
            "deployment_id": ParameterMetadata(
                type=ParameterType.STRING,
                description="Pending deployment id (dep-...) from generate_ingest_setup.",
                required=True,
            ),
            "user_code": ParameterMetadata(
                type=ParameterType.STRING,
                description=(
                    "Plaintext user_code the shipper displayed after "
                    "pairing/initiate; matched against the stored value."
                ),
                required=True,
            ),
            "state": ParameterMetadata(
                type=ParameterType.OBJECT,
                description=(
                    "Server-injected. Ownership-binding check requires "
                    "state['authenticated_principal']['client_id'] to match "
                    "deployment.initiating_client_id OR to be operator_equivalent."
                ),
                required=False,
            ),
        },
        return_value_schema=ReturnValueSchema(
            description="Approval confirmation (no credentials).",
            type=ParameterType.OBJECT,
            properties={
                "status": ParameterMetadata(
                    type=ParameterType.STRING,
                    description="Always 'approved' on success.",
                ),
                "deployment_id": ParameterMetadata(
                    type=ParameterType.STRING,
                    description="The deployment that was approved.",
                ),
            },
        ),
        result_processor_customizations=MergeResultProcessorCustomizations(
            action_label="Approve Shipper Pairing",
            result_type="ledger_pairing_approved",
            result_description="A shipper deployment transitioned pending→approved.",
        ),
        error_processor_customizations=MergeErrorProcessorCustomizations(retryable=False),
    )
    @abstractmethod
    def approve_pairing(
        self,
        deployment_id: str,
        user_code: str,
        state: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Ownership-bound approve pending→approved (does NOT mint credentials)."""

    @service_interface_process(
        name="shipper_self_revoke",
        is_discoverable=False,  # ingest-bridge only; not in model-callable surface.
        provider=_PROVIDER,
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        parameters={
            "state": ParameterMetadata(
                type=ParameterType.OBJECT,
                description=(
                    "Server-injected. Target deployment is derived SERVER-SIDE "
                    "from state['authenticated_principal']['client_id']; spec "
                    "§14.1 pin 2 — the handler accepts NO caller-supplied "
                    "deployment_id argument."
                ),
                required=False,
            ),
        },
        return_value_schema=ReturnValueSchema(
            description="Revocation confirmation.",
            type=ParameterType.OBJECT,
            properties={
                "status": ParameterMetadata(
                    type=ParameterType.STRING,
                    description="Always 'revoked' on success.",
                ),
                "deployment_id": ParameterMetadata(
                    type=ParameterType.STRING,
                    description="The deployment that was revoked.",
                ),
            },
        ),
        result_processor_customizations=MergeResultProcessorCustomizations(
            action_label="Shipper Self-Revoke",
            result_type="ledger_pairing_revoked",
            result_description="A paired shipper revoked its own deployment.",
        ),
        error_processor_customizations=MergeErrorProcessorCustomizations(retryable=False),
    )
    @abstractmethod
    def shipper_self_revoke(
        self,
        state: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Revoke the caller's own deployment. Target is server-derived."""


# ───────────────────────────────────────────────────────────────────────────
# M6 — semantic search / summary intake (spec §17.6 + §10.10.3)
# ───────────────────────────────────────────────────────────────────────────
#
# Split into its own sibling ABC per Architect 2026-05-31: M6's caller-side
# summarizer push and ANN-based search are a distinct concern from the M1
# ingest/read surface and the M5 deployment surface. The
# ``ServiceInterfaceScanner`` discovers methods on every ABC declared in this
# module via ``inspect.getmembers``, so adding this sibling does not require
# any scanner change. ``SessionLedgerService`` implements all three ABCs.


class SessionLedgerSearchAPI(ABC):
    """M6 caller-side summary push + semantic search (spec §17.6).

    Two methods. ``push_session_summary_chunk`` generates an embedding
    via ``embedding_service`` and stores it via ``vector_service``, then
    inserts the ``session_ledger__summary`` row. ``search_sessions``
    runs ANN over the same namespace and joins back to sessions.
    """

    @service_interface_process(
        name="push_session_summary_chunk",
        is_discoverable=False,  # caller-side summarizer surface; not LLM-discoverable
        provider=_PROVIDER,
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        parameters={
            "session_id": ParameterMetadata(
                type=ParameterType.STRING,
                description="Ledger session id (les-...) to attach the summary to.",
                required=True,
            ),
            "chunk_index": ParameterMetadata(
                type=ParameterType.INTEGER,
                description=(
                    "Caller-assigned chunk ordering within the session. The "
                    "(session_id, chunk_index) pair is UNIQUE in "
                    "session_ledger__summary."
                ),
                required=True,
            ),
            "summary_text": ParameterMetadata(
                type=ParameterType.STRING,
                description="The summary body.",
                required=True,
            ),
            "state": ParameterMetadata(
                type=ParameterType.OBJECT,
                description=(
                    "Server-injected. Provides authenticated_principal; "
                    "state['authenticated_principal']['client_id'] is "
                    "persisted as generated_by_client_id (spec §8.10 — "
                    "NOT NULL). Callers MUST NOT supply."
                ),
                required=False,
            ),
        },
        return_value_schema=ReturnValueSchema(
            description="Summary persistence + embedding-store confirmation.",
            type=ParameterType.OBJECT,
            properties={
                "summary_id": ParameterMetadata(
                    type=ParameterType.STRING,
                    description="New summary row id (sum-...).",
                ),
                "embedding_vector_id": ParameterMetadata(
                    type=ParameterType.STRING,
                    description="Reference into the vector store for this summary.",
                ),
                "chunk_index": ParameterMetadata(
                    type=ParameterType.INTEGER,
                    description="Echo of the caller-supplied chunk_index.",
                ),
            },
        ),
        result_processor_customizations=MergeResultProcessorCustomizations(
            action_label="Push Session Summary Chunk",
            result_type="ledger_summary_pushed",
            result_description="One summary chunk landed in session_ledger__summary.",
        ),
        error_processor_customizations=MergeErrorProcessorCustomizations(retryable=False),
    )
    @abstractmethod
    def push_session_summary_chunk(
        self,
        session_id: str,
        chunk_index: int,
        summary_text: str,
        state: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Embed + store-vector + insert one summary chunk row."""

    @service_interface_process(
        name="search_sessions",
        is_discoverable=True,
        provider=_PROVIDER,
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        parameters={
            "query": ParameterMetadata(
                type=ParameterType.STRING,
                description="Natural-language search query.",
                required=True,
            ),
            "limit": ParameterMetadata(
                type=ParameterType.INTEGER,
                description="Top-k result count (1..50; default 10).",
                required=False,
            ),
            "vendor": ParameterMetadata(
                type=ParameterType.STRING,
                description=(
                    "SourceVendor StrEnum value (codex / claude_code / "
                    "agent_messaging / chatgpt). Optional vendor filter; "
                    "the ANN result set is post-filtered to matching rows."
                ),
                required=False,
            ),
            "source_kind": ParameterMetadata(
                type=ParameterType.STRING,
                description=(
                    "IngestSourceKind StrEnum value. Optional filter on the "
                    "__source row joined via session.source_id."
                ),
                required=False,
            ),
            "since": ParameterMetadata(
                type=ParameterType.STRING,
                description=(
                    "Optional ISO-8601 lower bound on session.last_event_at "
                    "(UTC). Narrows the ANN result set to recent sessions."
                ),
                required=False,
            ),
        },
        return_value_schema=ReturnValueSchema(
            description=(
                "Top-k summary chunks joined to their sessions, ordered "
                "by similarity score descending."
            ),
            type=ParameterType.OBJECT,
            properties={
                "results": ParameterMetadata(
                    type=ParameterType.LIST,
                    description=(
                        "Per-match envelope: {session_id, chunk_index, "
                        "summary_text, score, session}."
                    ),
                ),
            },
        ),
        result_processor_customizations=MergeResultProcessorCustomizations(
            action_label="Search Session Ledger",
            result_type="ledger_search_results",
            result_description="Semantic search over session summaries.",
        ),
        error_processor_customizations=MergeErrorProcessorCustomizations(retryable=True),
    )
    @abstractmethod
    def search_sessions(
        self,
        query: str,
        limit: int = 10,
        vendor: SourceVendor | None = None,
        source_kind: IngestSourceKind | None = None,
        since: datetime | None = None,
    ) -> dict[str, Any]:
        """ANN search over summaries (M17 §2.2)."""

    @service_interface_process(
        name="list_events_by_source_window",
        is_discoverable=True,
        provider=_PROVIDER,
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        parameters={
            "source_kind": ParameterMetadata(
                type=ParameterType.STRING,
                description=(
                    "Optional ``IngestSourceKind`` StrEnum value to filter "
                    "by (codex_local / claude_code_local / chatgpt_export "
                    "/ agent_messaging / ...). W5.E §5.5 G5 relaxation: "
                    "now optional — supply either ``source_kind`` OR "
                    "``vendor`` (or both); the service rejects calls "
                    "with neither so the verb's scope-is-intentional "
                    "contract is preserved."
                ),
                required=False,
            ),
            "since": ParameterMetadata(
                type=ParameterType.STRING,
                description=(
                    "Optional ISO-8601 lower bound on ``event_at`` (UTC, "
                    "timezone-aware required per :func:`_parse_iso`). "
                    "W5.E §5.5 G5 relaxation: now optional — omit to "
                    "list ALL events back to the corpus start. Pair with "
                    "``limit`` to bound the candidate set on large vendors."
                ),
                required=False,
            ),
            "until": ParameterMetadata(
                type=ParameterType.STRING,
                description=(
                    "Optional ISO-8601 upper bound on ``event_at`` (UTC, "
                    "timezone-aware required). W5.E §5.5 G5 relaxation: "
                    "now optional — defaults to ``datetime.now(UTC)`` at "
                    "the service-layer entry when omitted, so vendor-/"
                    "source_kind-only audit pulls return the most recent "
                    "events without an explicit upper bound."
                ),
                required=False,
            ),
            "limit": ParameterMetadata(
                type=ParameterType.INTEGER,
                description=(
                    "Top-k result count (1..100; default 20). Clamped "
                    "to the upper bound (SQL-lockdown Slice 7 narrowed the "
                    "former 200 to the ``query_ordered`` ≤100 cap; page via "
                    "``until`` for longer spans)."
                ),
                required=False,
            ),
            "vendor": ParameterMetadata(
                type=ParameterType.STRING,
                description=(
                    "Optional ``SourceVendor`` StrEnum value (codex / "
                    "claude_code / agent_messaging / chatgpt / claude_ai). "
                    "Filter rides the ``session_vendor`` column denormalized "
                    "onto each ``__event`` row (SQL-lockdown Slice 7) so the "
                    "read is a single table — no JOIN. W5.E §5.5 G5: "
                    "supply ``vendor`` alone to pull recent events "
                    "across ALL of a vendor's source_kinds (e.g., "
                    "``vendor='codex'`` returns events from "
                    "codex_local + codex_history + codex_state + "
                    "codex_goals + codex_memories + codex_ambient + "
                    "codex_pushed combined)."
                ),
                required=False,
            ),
        },
        return_value_schema=ReturnValueSchema(
            description=(
                "Events of a source kind in the ``[since, until]`` "
                "event_at window, ordered ``event_at DESC`` (newest "
                "first), bounded by ``limit``."
            ),
            type=ParameterType.OBJECT,
            properties={
                "events": ParameterMetadata(
                    type=ParameterType.LIST,
                    description=(
                        "Per-row envelope: {event_id, session_id, "
                        "sequence, event_at (ISO), role, content_text, "
                        "vendor, source_kind}."
                    ),
                ),
            },
        ),
        result_processor_customizations=MergeResultProcessorCustomizations(
            action_label="List Session Ledger Events By Source Window",
            result_type="ledger_event_window_results",
            result_description=(
                "Source/time window listing over ``__event`` rows "
                "without a content-text predicate."
            ),
        ),
        error_processor_customizations=MergeErrorProcessorCustomizations(retryable=True),
    )
    @abstractmethod
    def list_events_by_source_window(
        self,
        source_kind: str | None = None,
        since: str | None = None,
        until: str | None = None,
        limit: int = 20,
        vendor: str | None = None,
    ) -> dict[str, Any]:
        """List events of a source kind / vendor in an optional event_at window.

        A content-text-free event window list over ``__event`` filtered to a
        source kind / vendor + time window, with NO content-text predicate (an
        operator wanting "events of source kind K in [T1, T2]" needs no search
        query). SQL-lockdown Slice 7 (Architect-ruled denormalize) retired the
        former 3-table JOIN (event → session → source) onto a single-table read
        of the ``session_vendor`` + ``source_kind`` columns denormalized onto
        each event at append time (faithful-forever — both are INSERT-only on
        their source rows and events are never re-parented; populate
        pre-migration rows once via ``backfill_event_source_denormalization``).

        W5.E §5.5 G5 signature relaxation: ``source_kind``, ``since``,
        and ``until`` are now optional. The verb accepts vendor-only
        audit pulls (``vendor='codex'``) and source_kind-only listings
        without an explicit time window. Callers MUST supply at least
        one of ``source_kind`` / ``vendor`` (the service raises
        ``ValueError`` on neither, preserving the verb's
        scope-is-intentional semantic). Existing triple-arg call sites
        keep working — positional ORDER is preserved.

        Returns events ordered by ``event_at DESC`` (newest first),
        bounded by ``limit`` (clamped to ``[1, 100]``).
        """

    @service_interface_process(
        name="search_event_content",
        is_discoverable=True,
        provider=_PROVIDER,
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        parameters={
            "query": ParameterMetadata(
                type=ParameterType.STRING,
                description="Natural-language search query.",
                required=True,
            ),
            "limit": ParameterMetadata(
                type=ParameterType.INTEGER,
                description="Top-k result count (1..50; default 10).",
                required=False,
            ),
        },
        return_value_schema=ReturnValueSchema(
            description=(
                "Top-k event chunks joined to their ``__event`` rows, "
                "ordered by similarity score descending."
            ),
            type=ParameterType.OBJECT,
            properties={
                "results": ParameterMetadata(
                    type=ParameterType.LIST,
                    description=(
                        "Per-match envelope: {event_id, session_id, "
                        "sequence, event_at, role, content_text, vendor, "
                        "source_kind, chunk_index, score}."
                    ),
                ),
            },
        ),
        result_processor_customizations=MergeResultProcessorCustomizations(
            action_label="Search Session Ledger Event Content",
            result_type="ledger_event_search_results",
            result_description=(
                "Semantic search over event-content embeddings (LED-01): "
                "raw user/assistant message text, not session summaries."
            ),
        ),
        error_processor_customizations=MergeErrorProcessorCustomizations(retryable=True),
    )
    @abstractmethod
    def search_event_content(
        self,
        query: str,
        limit: int = 10,
    ) -> dict[str, Any]:
        """ANN search over event-content embeddings (LED-01).

        Complements ``search_sessions`` (which rides lossy M6 summaries of
        quiescent sessions): this verb searches the raw USER/ASSISTANT
        message text itself, chunk-granular, in the
        ``session_ledger_event`` vector namespace. Only events already
        embedded (by ``embed_missing_event_content`` / the scheduled drain)
        are reachable. Results carry the ``list_events_by_source_window``
        event envelope plus ``chunk_index`` + ``score`` (cosine similarity,
        descending); ``limit`` is clamped to ``[1, 50]``.
        """

    @service_interface_process(
        name="event_embedding_coverage",
        is_discoverable=True,
        provider=_PROVIDER,
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        parameters={},
        return_value_schema=ReturnValueSchema(
            description=(
                "LED-01 event-embedding coverage: drain frontier + embedded-chunk "
                "count; a deterministic 'is the backfill caught up?' read with no "
                "full-corpus scan."
            ),
            type=ParameterType.OBJECT,
            properties={
                "caught_up": ParameterMetadata(
                    type=ParameterType.BOOLEAN,
                    description="True when the drain cursor has reached the newest in-scope event (a frontier signal, not per-row proof).",
                ),
                "cursor_imported_at": ParameterMetadata(
                    type=ParameterType.STRING,
                    description="Drain frontier: imported_at at/before which every embeddable event is embedded; None if never advanced.",
                ),
                "newest_in_scope_imported_at": ParameterMetadata(
                    type=ParameterType.STRING,
                    description="imported_at of the newest embeddable event (SQL scope legs); None when nothing is in scope.",
                ),
                "embedded_chunk_count": ParameterMetadata(
                    type=ParameterType.INTEGER,
                    description="Live vectors in the session_ledger_event namespace (CHUNKS, not events); 0 when nothing embedded.",
                ),
            },
        ),
        result_processor_customizations=MergeResultProcessorCustomizations(
            action_label="Ledger Event-Embedding Coverage",
            result_type="ledger_event_embedding_coverage",
            result_description="Deterministic LED-01 backfill-coverage read: drain frontier plus embedded-chunk count.",
        ),
        error_processor_customizations=MergeErrorProcessorCustomizations(retryable=True),
    )
    @abstractmethod
    def event_embedding_coverage(self) -> dict[str, Any]:
        """Read-only LED-01 event-embedding coverage (frontier + chunk count).

        Compares the durable drain cursor to the newest in-scope event's
        ``imported_at`` (``caught_up``) and reports the live embedded-chunk count
        — a deterministic "is the backfill caught up?" read with no O(N) scan.
        Complements ``drain_event_embeddings`` (which advances the frontier).
        """

    @service_interface_process(
        name="embed_missing_event_content",
        is_discoverable=False,  # operator/maintenance producer; cron rides it later
        provider=_PROVIDER,
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        is_long_running=True,
        parameters={
            "batch_limit": ParameterMetadata(
                type=ParameterType.INTEGER,
                description=(
                    "Maximum events to embed this call (1..200; default "
                    "25). A bounded batch, NOT drain-until-empty — the "
                    "caller loops while the returned ``exhausted`` is "
                    "false."
                ),
                required=False,
            ),
        },
        return_value_schema=ReturnValueSchema(
            description=(
                "Batch disposition: how far the newest-first candidate "
                "walk got and what it embedded."
            ),
            type=ParameterType.OBJECT,
            properties={
                "candidates_scanned": ParameterMetadata(
                    type=ParameterType.INTEGER,
                    description="Candidate rows read from ``__event`` this call.",
                ),
                "events_embedded": ParameterMetadata(
                    type=ParameterType.INTEGER,
                    description="Events newly embedded this call.",
                ),
                "chunks_stored": ParameterMetadata(
                    type=ParameterType.INTEGER,
                    description="Vector rows written across embedded events.",
                ),
                "events_skipped_existing": ParameterMetadata(
                    type=ParameterType.INTEGER,
                    description="Candidates already embedded (chunk-0 present).",
                ),
                "events_skipped_filtered": ParameterMetadata(
                    type=ParameterType.INTEGER,
                    description=(
                        "Candidates dropped by the Python scope leg "
                        "(reasoning subtype / blank content)."
                    ),
                ),
                "events_truncated": ParameterMetadata(
                    type=ParameterType.INTEGER,
                    description=(
                        "Embedded events whose chunk list hit the "
                        "per-event bound (tail dropped, logged)."
                    ),
                ),
                "exhausted": ParameterMetadata(
                    type=ParameterType.BOOLEAN,
                    description=(
                        "True when the candidate walk reached the end of "
                        "the corpus this call; false means more work "
                        "remains — call again."
                    ),
                ),
                "batch_limit": ParameterMetadata(
                    type=ParameterType.INTEGER,
                    description="Echo of the applied (clamped) batch limit.",
                ),
            },
        ),
        result_processor_customizations=MergeResultProcessorCustomizations(
            action_label="Embed Missing Event Content",
            result_type="ledger_event_embedding_batch",
            result_description=(
                "One bounded newest-first batch of the LED-01 event-content "
                "embedding producer."
            ),
        ),
        error_processor_customizations=MergeErrorProcessorCustomizations(retryable=True),
    )
    @abstractmethod
    def embed_missing_event_content(
        self,
        batch_limit: int = 25,
    ) -> dict[str, Any]:
        """Embed a bounded batch of not-yet-embedded events (LED-01 producer).

        Walks embed-scope candidates newest-first (MESSAGE + user/assistant
        + inline content, minus the Codex ``reasoning`` subtype), skips
        events whose chunk-0 vector already exists, and embeds the rest —
        up to ``batch_limit`` per call (clamped to ``[1, 200]``). Serves
        the operator subset backfill now and the Lane-1 scheduled drain
        later; embedding runs synchronously in this call, hence
        ``is_long_running`` and the bounded batch.
        """


class SessionLedgerEmbeddingDrainAPI(ABC):
    """LED-01 event-embedding drain lane — the cron drainer + its schedule.

    A sibling ABC (not folded into the search/producer surface) so the
    autonomous drain heartbeat and its boot-time schedule installer form one
    coherent, self-contained group. The ServiceInterfaceScanner walks every
    class in this module by attribute marker, so adding this sibling requires no
    scanner change; ``SessionLedgerService`` inherits it alongside the other
    surfaces.
    """

    @service_interface_process(
        name="drain_event_embeddings",
        is_discoverable=False,  # cron-fired only; not model-discoverable
        provider=_PROVIDER,
        # EDGE_SINK per the canonical scheduler cron-action contract — same
        # rationale as ``summarize_quiescent_sessions``. Terminal action; no
        # result/error processor attached.
        processor_policy_category=ProcessorPolicyCategory.EDGE_SINK,
        parameters={
            "page_size": ParameterMetadata(
                type=ParameterType.INTEGER,
                description=(
                    "Per-iteration candidate page the drainer walks (default "
                    "100; clamped to 1..100). NOT a per-fire cap — the drainer "
                    "walks the durable cursor forward until the backlog is "
                    "caught up."
                ),
                required=False,
            ),
        },
        return_value_schema=ReturnValueSchema(
            description=(
                "Drainer disposition for this cron fire. The drain runs "
                "asynchronously on a background thread, so per-event counts "
                "are logged when it completes, not returned here."
            ),
            type=ParameterType.OBJECT,
            properties={
                "drainer": ParameterMetadata(
                    type=ParameterType.STRING,
                    description=(
                        "``started`` if this fire launched the singleton "
                        "cursor-forward drain, or ``already_running`` if a "
                        "drainer was already active (this fire was a no-op)."
                    ),
                ),
                "page_size": ParameterMetadata(
                    type=ParameterType.INTEGER,
                    description="Per-iteration page the drain used.",
                ),
            },
        ),
        # Terminal/headless cron-fired action; no success scaffold, no error
        # inference — same as ``summarize_quiescent_sessions``.
    )
    @abstractmethod
    def drain_event_embeddings(self, page_size: int = 100) -> dict[str, Any]:
        """Cron heartbeat: (re)start the singleton event-embedding drainer.

        Each fire is a heartbeat that does NO embedding on the action-queue
        thread — it only tries to start ONE background drainer (returning
        ``{"drainer": "started"}``) or no-ops if one is already running
        (``{"drainer": "already_running"}``). The drainer
        (``EventEmbeddingWriter.drain_missing_events``) walks a durable
        ARRIVAL-order (``imported_at``) cursor forward, embedding every
        not-yet-embedded USER/ASSISTANT message event until the backlog is
        caught up, then persists the advanced cursor so the next fire embeds
        only what arrived since. Arrival order (NOT vendor ``event_at``) is what
        lets a historical session imported after the cursor still be caught, and
        a periodic reconciliation full-sweep (every Nth fire, cursor ignored)
        re-checks the whole corpus so a row that committed into visibility below
        the advanced cursor — ``imported_at`` is pre-assigned, not
        commit-monotonic — is still embedded (hard eventual completeness).
        This is the LED-01 steady-state that mirrors
        ``summarize_quiescent_sessions`` for the EVENT corpus: cron-fired via
        :meth:`ensure_periodic_embed_schedule`, embedding runs on the local
        embedder (no inference), and the whole historical backlog fills in the
        background without an operator loop. ``page_size`` is the drainer's
        per-iteration page (clamped 1..100), NOT a per-fire cap.
        """

    @service_interface_process(
        name="ensure_periodic_embed_schedule",
        is_discoverable=False,  # boot-only; invoked by starting_actions, not the model
        provider=_PROVIDER,
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        parameters={
            "cadence_minutes": ParameterMetadata(
                type=ParameterType.INTEGER,
                description=(
                    "Cron cadence in minutes (1..59 inclusive; default 10). "
                    "Each fire runs :meth:`drain_event_embeddings`."
                ),
                required=False,
            ),
            "tag": ParameterMetadata(
                type=ParameterType.STRING,
                description="Schedule tag (default ``ledger:periodic_embed``).",
                required=False,
            ),
        },
        return_value_schema=ReturnValueSchema(
            description="Periodic event-embedding-drain schedule ensure result.",
            type=ParameterType.OBJECT,
            properties={
                "outcome": ParameterMetadata(
                    type=ParameterType.STRING,
                    description=(
                        "``created`` (fresh) or ``normalized`` "
                        "(stale/duplicate schedules cleared first)."
                    ),
                ),
                "schedule_id": ParameterMetadata(
                    type=ParameterType.STRING,
                    description="Active schedule id.",
                ),
                "tag": ParameterMetadata(
                    type=ParameterType.STRING,
                    description="Schedule tag in use.",
                ),
                "cadence_minutes": ParameterMetadata(
                    type=ParameterType.INTEGER,
                    description="Cadence applied.",
                ),
                "cleared_count": ParameterMetadata(
                    type=ParameterType.INTEGER,
                    description="Stale/duplicate schedules cleared.",
                ),
            },
        ),
        result_processor_customizations=MergeResultProcessorCustomizations(
            action_label="Ensure Periodic Ledger Event-Embedding Drain",
            result_type="ledger_periodic_embed_ensured",
            result_description="Periodic event-embedding-drain cron ensured.",
        ),
        error_processor_customizations=MergeErrorProcessorCustomizations(retryable=True),
    )
    @abstractmethod
    def ensure_periodic_embed_schedule(
        self,
        cadence_minutes: int = 10,
        tag: str = "ledger:periodic_embed",
    ) -> dict[str, Any]:
        """Idempotently install a cron firing :meth:`drain_event_embeddings`.

        Mirrors :meth:`ensure_periodic_summarize_schedule` for the LED-01
        event-embedding path. Boot-only — wired via the profile's
        ``starting_actions`` so a fresh solet starts embedding her own
        event content the moment she comes up (the operator directive: the
        embedding runs automatically, the same manner as auto-summarize).
        """


__all__ = [
    "SessionLedgerCanonicalPointerRepairAPI",
    "SessionLedgerDeploymentAPI",
    "SessionLedgerEmbeddingDrainAPI",
    "SessionLedgerEventExternalIdBackfillAPI",
    "SessionLedgerEventSourceDenormBackfillAPI",
    "SessionLedgerIngestAPI",
    "SessionLedgerInvertedBoundsRepairAPI",
    "SessionLedgerPollingDriverAPI",
    "SessionLedgerReadAPI",
    "SessionLedgerSearchAPI",
    "SessionLedgerSessionSourceKindBackfillAPI",
    "SessionLedgerSummarizeAPI",
]
