"""Schema definitions for the LLM session ledger.

Ten tables in namespace ``session_ledger`` (spec §8):

* ``source`` / ``source_cursor`` / ``import_batch`` — ingest plumbing.
* ``session`` / ``event`` / ``tool_call`` / ``attachment`` — content rows.
* ``active_lease`` — push-side keepalive.
* ``summary`` — M6 semantic-search payloads (table exists in M1, populated later).
* ``deployment`` — M5 shipper pairing state.

Standard fields (``id``, ``namespace``, ``created_at``, ``updated_at``,
``created_by``, ``updated_by``, ``is_deleted``) are auto-injected by the
``SchemaStandardizer`` and MUST NOT be declared here.

Foreign keys are repository-enforced (spec §8 preamble). The platform's
``ColumnDefinition`` / ``TableSchema`` have no foreign-key field; each
cross-table reference is declared NOT NULL with a ``repository_fk: ...``
note in the column description.
"""

from __future__ import annotations

from enum import StrEnum

from ananta.llm.session_ledger.types import (
    CursorScope,
    EventType,
    ImportBatchStatus,
    IngestSourceKind,
    PairingStatus,
    SourceVendor,
)
from ananta.types.column_types import ColumnType
from ananta.types.schema_types import (
    ColumnDefinition,
    IndexDefinition,
    SchemaDefinition,
    TableSchema,
)

NAMESPACE = "session_ledger"

# Per-namespace embeddings-table convention from pgvector_service_plugin:
# physical table name is composed `<namespace>__embeddings`. The session
# ledger declares its own namespace so summary-vector storage is isolated
# from memory_service vectors (which live under namespace
# `pgvector_service_plugin`). Operator-confirmed 2026-05-31 ruling:
# consumer declares its own per-namespace embeddings table; pgvector
# plugin stays generic.
SUMMARY_VECTOR_NAMESPACE = "session_ledger_summary"
TABLE_SUMMARY_EMBEDDINGS = "embeddings"

# LED-01 event-content vector store. Separate namespace from the summary
# store so semantic event search never pollutes `search_sessions` (which
# rides SUMMARY_VECTOR_NAMESPACE). Physical table
# `session_ledger_event__embeddings`. Unlike the summary table, this one
# carries a fixed vector dimension + an HNSW index because the event
# corpus is ~1M+ vectors (seq-scan ANN is unusable at that scale). The
# dimension is deployment-dependent (nomic 768 local / titan ~1024 cloud)
# and is queried LIVE from the embedder at service start — never hardcoded
# and never read from a config value (operator ruling 2026-07-06).
EVENT_VECTOR_NAMESPACE = "session_ledger_event"
TABLE_EVENT_EMBEDDINGS = "embeddings"

TABLE_SOURCE = "source"
TABLE_SOURCE_CURSOR = "source_cursor"
TABLE_IMPORT_BATCH = "import_batch"
TABLE_SESSION = "session"
TABLE_EVENT = "event"
TABLE_TOOL_CALL = "tool_call"
TABLE_ATTACHMENT = "attachment"
TABLE_ACTIVE_LEASE = "active_lease"
TABLE_SUMMARY = "summary"
TABLE_DEPLOYMENT = "deployment"
TABLE_SESSION_SOURCE_KIND = "session_source_kind"

ID_PREFIX_SOURCE = "src"
ID_PREFIX_SOURCE_CURSOR = "scu"
ID_PREFIX_IMPORT_BATCH = "imb"
ID_PREFIX_SESSION = "les"
ID_PREFIX_EVENT = "evt"
ID_PREFIX_TOOL_CALL = "tcl"
ID_PREFIX_ATTACHMENT = "atc"
ID_PREFIX_ACTIVE_LEASE = "lse"
ID_PREFIX_SUMMARY = "sum"
ID_PREFIX_DEPLOYMENT = "dep"
ID_PREFIX_SESSION_SOURCE_KIND = "ssk"

CONTENT_INLINE_TEXT_MAX_BYTES = 4096

_TOOL_CALL_STATUS_VALUES = ("pending", "succeeded", "errored")


def _quoted_csv(values: tuple[str, ...]) -> str:
    return ", ".join(f"'{v}'" for v in values)


def _enum_csv(enum_cls: type[StrEnum]) -> str:
    return _quoted_csv(tuple(member.value for member in enum_cls))


def _source_table() -> TableSchema:
    return TableSchema(
        table_name=TABLE_SOURCE,
        id_prefix=ID_PREFIX_SOURCE,
        description="Configured ingest sources. One row per (source_kind, root_uri).",
        columns={
            "source_kind": ColumnDefinition(
                type=ColumnType.TEXT,
                not_null=True,
                check=f"source_kind IN ({_enum_csv(IngestSourceKind)})",
                description="StrEnum IngestSourceKind value.",
            ),
            "root_uri": ColumnDefinition(
                type=ColumnType.TEXT,
                not_null=True,
                description=(
                    "Filesystem path, blob_id, or sentinel — interpretation is "
                    "private to the source plugin."
                ),
            ),
            "account_label": ColumnDefinition(
                type=ColumnType.TEXT,
                description="Operator-supplied free text.",
            ),
            "enabled": ColumnDefinition(
                type=ColumnType.BOOLEAN,
                not_null=True,
                default=True,
                description="Operator can disable a source without deletion.",
            ),
            "config_json": ColumnDefinition(
                type=ColumnType.JSON,
                default="{}",
                description="Source-private configuration payload.",
            ),
            "polling_lease_until": ColumnDefinition(
                type=ColumnType.DATETIME,
                description=(
                    "Wall-clock UTC moment until which the current "
                    "polling-lease holder owns this source. NULL when no "
                    "lease is held. Refreshed by the importer's heartbeat "
                    "during long walks; acquired via "
                    "``try_acquire_polling_lease`` (atomic conditional "
                    "UPDATE on ``polling_lease_until IS NULL OR "
                    "polling_lease_until < NOW()``). Paired with "
                    "``polling_lease_token`` so successor pollers can fence "
                    "the stale prior owner out of refresh / release."
                ),
            ),
            "polling_lease_token": ColumnDefinition(
                type=ColumnType.TEXT,
                description=(
                    "Opaque per-acquisition fence token tying the current "
                    "lease holder to this source row. NULL when no lease is "
                    "held. Set atomically with ``polling_lease_until`` by "
                    "``try_acquire_polling_lease``; required in the WHERE "
                    "clause of ``refresh_polling_lease`` + "
                    "``release_polling_lease`` so a stale prior owner's "
                    "calls become silent no-ops."
                ),
            ),
        },
        indexes=[
            IndexDefinition(
                name="idx_source_kind_enabled",
                columns=["source_kind", "enabled"],
            ),
        ],
    )


def _source_cursor_table() -> TableSchema:
    return TableSchema(
        table_name=TABLE_SOURCE_CURSOR,
        id_prefix=ID_PREFIX_SOURCE_CURSOR,
        description=(
            "Persistent cursors per source. ``cursor_scope`` is either "
            "'discovery' (one per source, scope_key NULL) or 'event_read' "
            "(one per (source, external session))."
        ),
        columns={
            "source_id": ColumnDefinition(
                type=ColumnType.TEXT,
                not_null=True,
                description="repository_fk: session_ledger__source.id.",
            ),
            "cursor_scope": ColumnDefinition(
                type=ColumnType.TEXT,
                not_null=True,
                check=f"cursor_scope IN ({_enum_csv(CursorScope)})",
                description="StrEnum CursorScope value.",
            ),
            "cursor_payload": ColumnDefinition(
                type=ColumnType.JSON,
                not_null=True,
                description="Source-private opaque blob.",
            ),
            "scope_key": ColumnDefinition(
                type=ColumnType.TEXT,
                description=(
                    "For event_read: external_session_id. For discovery: NULL. "
                    "Partial unique indexes enforce one row per scope kind."
                ),
            ),
        },
        indexes=[
            IndexDefinition(
                name="idx_cursor_event_read_unique",
                columns=["source_id", "cursor_scope", "scope_key"],
                unique=True,
                where="cursor_scope = 'event_read'",
            ),
            IndexDefinition(
                name="idx_cursor_discovery_unique",
                columns=["source_id", "cursor_scope"],
                unique=True,
                where="cursor_scope = 'discovery'",
            ),
        ],
    )


def _import_batch_table() -> TableSchema:
    return TableSchema(
        table_name=TABLE_IMPORT_BATCH,
        id_prefix=ID_PREFIX_IMPORT_BATCH,
        description="Per-poll-pass diagnostics row.",
        columns={
            "source_id": ColumnDefinition(
                type=ColumnType.TEXT,
                not_null=True,
                description="repository_fk: session_ledger__source.id.",
            ),
            "started_at": ColumnDefinition(
                type=ColumnType.DATETIME,
                not_null=True,
                description="Distinct from the platform-managed created_at.",
            ),
            "finished_at": ColumnDefinition(
                type=ColumnType.DATETIME,
                description="Set when status transitions to completed/failed.",
            ),
            "status": ColumnDefinition(
                type=ColumnType.TEXT,
                not_null=True,
                check=f"status IN ({_enum_csv(ImportBatchStatus)})",
                description="StrEnum ImportBatchStatus value.",
            ),
            "event_count": ColumnDefinition(
                type=ColumnType.INTEGER,
                not_null=True,
                default=0,
                description="Events persisted successfully in this batch.",
            ),
            "error_message": ColumnDefinition(
                type=ColumnType.TEXT,
                description="Set on status=failed.",
            ),
            "error_kind": ColumnDefinition(
                type=ColumnType.TEXT,
                description=(
                    "Coarse error category, e.g. 'value_error', "
                    "'state_service_timeout'."
                ),
            ),
            "polling_lease_token": ColumnDefinition(
                type=ColumnType.TEXT,
                description=(
                    "Opaque per-acquisition fence token tying this batch row "
                    "to a specific polling-lease owner. NULL on route-created "
                    "batches (``register_chatgpt_export_source`` / "
                    "``register_claude_ai_export_source`` paths run from the "
                    "HTTP handler with no lease in hand). Set by "
                    "``start_batch`` when the importer passes its current "
                    "lease token. Adopted route batches have their token "
                    "UPDATEd NULL → importer's token via "
                    "``adopt_route_batch_for_source``. ``finish_batch`` is "
                    "conditional on this token: a stale owner's late finish "
                    "returns rowcount=0 and is silently dropped so "
                    "handed-off batches cannot be clobbered."
                ),
            ),
        },
        indexes=[
            IndexDefinition(
                name="idx_batch_status",
                columns=["status", "started_at"],
            ),
        ],
    )


def _session_table() -> TableSchema:
    return TableSchema(
        table_name=TABLE_SESSION,
        id_prefix=ID_PREFIX_SESSION,
        description="One row per ingested external session.",
        columns={
            "source_id": ColumnDefinition(
                type=ColumnType.TEXT,
                not_null=True,
                description="repository_fk: session_ledger__source.id.",
            ),
            "external_session_id": ColumnDefinition(
                type=ColumnType.TEXT,
                not_null=True,
                description="Vendor-supplied id (Claude Code UUID, Codex filename stem, etc.).",
            ),
            "vendor": ColumnDefinition(
                type=ColumnType.TEXT,
                not_null=True,
                check=f"vendor IN ({_enum_csv(SourceVendor)})",
                description="StrEnum SourceVendor value. Coarser than source_kind.",
            ),
            "vendor_session_label": ColumnDefinition(
                type=ColumnType.TEXT,
                description="Vendor-supplied name (project-derived, manual, etc.).",
            ),
            "project_path": ColumnDefinition(
                type=ColumnType.TEXT,
                description="When applicable (Claude Code projects, Codex cwd).",
            ),
            "first_event_at": ColumnDefinition(
                type=ColumnType.DATETIME,
                not_null=True,
                description="Vendor-supplied; distinct from standard created_at.",
            ),
            "last_event_at": ColumnDefinition(
                type=ColumnType.DATETIME,
                not_null=True,
                description="Vendor-supplied; sorted on by list_sessions.",
            ),
            "event_count": ColumnDefinition(
                type=ColumnType.INTEGER,
                not_null=True,
                default=0,
                description="Materialized count of events in this session.",
            ),
            "summary_text": ColumnDefinition(
                type=ColumnType.TEXT,
                description="M6 client-pushed text. NUL-byte sanitized before insert.",
            ),
            "originator_session_label": ColumnDefinition(
                type=ColumnType.TEXT,
                description=(
                    "Snapshot from core__agent_thread.originator_session_label "
                    "(peer threads) OR from filesystem-vendor session metadata "
                    "(claude_code_local: 'agent-name' line). NULL when the "
                    "source has no actor metadata. Per 2026-05-31 Architect "
                    "ruling §2: session-row fields are snapshotted once at "
                    "first-event time and never change; live /rename does "
                    "NOT propagate to historical rows."
                ),
            ),
            "originator_agent_instance_id": ColumnDefinition(
                type=ColumnType.TEXT,
                description=(
                    "Snapshot of the originating bridge's agent_instance_id "
                    "(peer threads) OR from filesystem-vendor session "
                    "metadata (claude_code_local: 'bridge-session' line). "
                    "NULL for non-MCP sources."
                ),
            ),
            "recipient_session_label": ColumnDefinition(
                type=ColumnType.TEXT,
                description=(
                    "Snapshot from core__agent_thread.recipient_session_label "
                    "(peer threads only). NULL for single-actor sessions "
                    "(filesystem ingest)."
                ),
            ),
            "recipient_agent_instance_id": ColumnDefinition(
                type=ColumnType.TEXT,
                description=(
                    "Snapshot of the recipient bridge's agent_instance_id "
                    "(peer threads only). NULL for single-actor sessions."
                ),
            ),
            "canonical_external_session_id": ColumnDefinition(
                type=ColumnType.TEXT,
                description=(
                    "M18 cross-source dedupe pointer. When NULL, this row IS "
                    "canonical for its (vendor, external_session_id) pair. "
                    "When non-NULL, this row is a non-canonical sibling and "
                    "the value is the external_session_id of the canonical "
                    "row that should be used for cross-source rollup. The "
                    "partial-unique index "
                    "idx_session_canonical_one_per_vendor_pair enforces at "
                    "most ONE canonical row per (vendor, external_session_id) "
                    "pair; concurrent INSERTs race for it and the database "
                    "atomically picks the winner — first-write-wins, a "
                    "race-free pattern."
                ),
            ),
        },
        indexes=[
            IndexDefinition(
                name="idx_session_source_external_unique",
                columns=["source_id", "external_session_id"],
                unique=True,
            ),
            # M18 partial-unique enforces first-write-wins for canonical
            # rows at the database level. At most ONE row per
            # (vendor, external_session_id) pair may claim canonical
            # status (canonical_external_session_id IS NULL). Concurrent
            # INSERTs race; the loser gets ON CONFLICT and the repository's
            # two-phase upsert pattern demotes to pointer.
            IndexDefinition(
                name="idx_session_canonical_one_per_vendor_pair",
                columns=["vendor", "external_session_id"],
                unique=True,
                where="canonical_external_session_id IS NULL AND is_deleted = 0",
            ),
            IndexDefinition(
                name="idx_session_canonical_pointer",
                columns=["canonical_external_session_id"],
                where="canonical_external_session_id IS NOT NULL",
            ),
            IndexDefinition(
                name="idx_session_last_event_at",
                columns=["last_event_at"],
            ),
            IndexDefinition(
                name="idx_session_project_path",
                columns=["project_path"],
            ),
            IndexDefinition(
                name="idx_session_originator_session_label",
                columns=["originator_session_label"],
                where="originator_session_label IS NOT NULL",
            ),
            IndexDefinition(
                name="idx_session_recipient_session_label",
                columns=["recipient_session_label"],
                where="recipient_session_label IS NOT NULL",
            ),
        ],
    )


def _event_table() -> TableSchema:
    return TableSchema(
        table_name=TABLE_EVENT,
        id_prefix=ID_PREFIX_EVENT,
        description=(
            "Append-only canonical events. Quarantined rows carry no content; "
            "the secret span is never persisted (spec §10.9)."
        ),
        columns={
            "session_id": ColumnDefinition(
                type=ColumnType.TEXT,
                not_null=True,
                description="repository_fk: session_ledger__session.id.",
            ),
            # Opt out of the platform-wide standalone UNIQUE(external_id):
            # this table's true identity is the COMPOSITE (session_id,
            # external_id) pair enforced by idx_event_session_external_unique
            # below — the same column, standalone, is not this table's key.
            # Schema-debt-external-id lane, 2a (2026-08-06); full rationale +
            # DB-verified evidence in
            # workbench/2026-08-06_schema_debt_external_id_findings_schema-debt-impl.md.
            "external_id": ColumnDefinition(
                type=ColumnType.TEXT,
                unique=False,
                data_sensitivity=0.5,
                description=(
                    "Vendor-supplied event id verbatim when present, else a "
                    "derv:-prefixed hash (derive_event_external_id). NOT "
                    "globally unique on its own — see idx_event_session_"
                    "external_unique below for the real identity."
                ),
            ),
            "sequence": ColumnDefinition(
                type=ColumnType.INTEGER,
                not_null=True,
                description=(
                    "Monotonic per session; allocated under transaction by "
                    "SessionLedgerRepository."
                ),
            ),
            "event_type": ColumnDefinition(
                type=ColumnType.TEXT,
                not_null=True,
                check=f"event_type IN ({_enum_csv(EventType)})",
                description="StrEnum EventType value.",
            ),
            "role": ColumnDefinition(
                type=ColumnType.TEXT,
                check="role IS NULL OR role IN ('user', 'assistant', 'system', 'tool')",
                description="StrEnum MessageRole value. Required for MESSAGE / SYSTEM.",
            ),
            "vendor_event_id": ColumnDefinition(
                type=ColumnType.TEXT,
                description="Vendor-supplied uuid for parent linkage.",
            ),
            "vendor_parent_event_id": ColumnDefinition(
                type=ColumnType.TEXT,
                description="Parent vendor uuid (Claude Code parent-child chains).",
            ),
            "content_text": ColumnDefinition(
                type=ColumnType.TEXT,
                description=(
                    "Inline text when len(text) <= CONTENT_INLINE_TEXT_MAX_BYTES. "
                    "Otherwise persisted via content_blob_id."
                ),
            ),
            "content_json": ColumnDefinition(
                type=ColumnType.JSON,
                description="Structured payload (tool args, model metadata).",
            ),
            "content_blob_id": ColumnDefinition(
                type=ColumnType.TEXT,
                description=(
                    "Set when content_text exceeds the inline cap. "
                    "Pointer into blob_storage_service."
                ),
            ),
            "event_at": ColumnDefinition(
                type=ColumnType.DATETIME,
                not_null=True,
                description="Vendor-supplied wall-clock event time.",
            ),
            "imported_at": ColumnDefinition(
                type=ColumnType.DATETIME,
                not_null=True,
                description="Platform receive time. Distinct from standard created_at.",
            ),
            "batch_id": ColumnDefinition(
                type=ColumnType.TEXT,
                not_null=True,
                description="repository_fk: session_ledger__import_batch.id.",
            ),
            "actor_session_label": ColumnDefinition(
                type=ColumnType.TEXT,
                description=(
                    "Snapshot of the actor's session_label at event-write "
                    "time. For peer events: derived from "
                    "core__agent_message.metadata['sender_session_label']. "
                    "For filesystem events: denormalized from the session "
                    "row's originator_session_label (single-actor sessions). "
                    "NULL when no actor identity is available. Per "
                    "2026-05-31 Architect ruling §1: event-row fields are "
                    "snapshotted at event-write time and reflect the actor "
                    "as it was when the specific event landed."
                ),
            ),
            "actor_agent_instance_id": ColumnDefinition(
                type=ColumnType.TEXT,
                description=(
                    "Snapshot of the actor's agent_instance_id at "
                    "event-write time. Same provenance as "
                    "actor_session_label."
                ),
            ),
            "session_vendor": ColumnDefinition(
                type=ColumnType.TEXT,
                description=(
                    "Denormalized snapshot of the parent session's "
                    "``vendor`` (SourceVendor value), copied onto the event "
                    "at append time. SQL-lockdown Slice 7: lets "
                    "``list_events_by_source_window`` filter by vendor on a "
                    "single-table ``query_ordered`` read instead of a 3-table "
                    "JOIN through __session/__source. Faithful-FOREVER: "
                    "``__session.vendor`` is INSERT-only (never in any UPDATE "
                    "SET) and events are never re-parented, so the snapshot "
                    "can never drift from the join it replaces. Per the "
                    "2026-05-31 Architect ruling §1 (event-row fields are "
                    "snapshotted at event-write time)."
                ),
            ),
            "source_kind": ColumnDefinition(
                type=ColumnType.TEXT,
                description=(
                    "Denormalized snapshot of the parent session's source's "
                    "``source_kind`` (IngestSourceKind value), copied onto the "
                    "event at append time via the session's immutable "
                    "``source_id``. SQL-lockdown Slice 7 companion to "
                    "``session_vendor`` — together they collapse the "
                    "event→session→source JOIN to a single-table read. "
                    "Faithful-FOREVER: ``__source.source_kind`` is INSERT-only "
                    "and a session's ``source_id`` never changes."
                ),
            ),
            "usage_json": ColumnDefinition(
                type=ColumnType.JSON,
                description=(
                    "T1 usage-capture lane (2026-08-05 ruling): per-message "
                    "token usage, persisted verbatim from the vendor's own "
                    "raw shape (no re-derivation/renaming). NULL for every "
                    "event the source vendor doesn't populate usage for -- "
                    "additive widening, re-installed via the metadata-driven "
                    "DDL lifecycle (ALTER TABLE ADD COLUMN, existing rows "
                    "preserved), never a required field."
                ),
            ),
        },
        indexes=[
            IndexDefinition(
                name="idx_event_session_sequence_unique",
                columns=["session_id", "sequence"],
                unique=True,
            ),
            IndexDefinition(
                name="idx_event_imported_at",
                columns=["imported_at"],
            ),
            IndexDefinition(
                name="idx_event_actor_session_label",
                columns=["actor_session_label", "event_at"],
                where="actor_session_label IS NOT NULL",
            ),
            # SQL-lockdown Slice 7: composite btrees serving the migrated
            # single-table ``list_events_by_source_window`` read
            # (``WHERE source_kind=? / session_vendor=? AND event_at<=?
            # ORDER BY event_at DESC``). The leading equality column + the
            # trailing ``event_at`` give an indexed reverse-scan for the two
            # documented filter modes — source-kind probes and vendor-only
            # audit pulls — without a JOIN. Non-partial: both columns are
            # denormalized at append time and (post-backfill) never NULL.
            IndexDefinition(
                name="idx_event_source_kind_event_at",
                columns=["source_kind", "event_at"],
            ),
            IndexDefinition(
                name="idx_event_session_vendor_event_at",
                columns=["session_vendor", "event_at"],
            ),
            # btree on event_at so an ``ORDER BY e.event_at DESC LIMIT %s``
            # planner-pick is the indexed reverse-scan rather than a top-N
            # sort over the unfiltered candidate set. The same index serves
            # ``since``/``until`` range scans in the
            # ``list_events_by_source_window`` time-window verb. The
            # actor-session-label compound at ``idx_event_actor_session_label``
            # has ``event_at`` as its trailing column under a partial WHERE,
            # so it CANNOT serve the unfiltered DESC scan — a standalone
            # btree is the right primitive here.
            IndexDefinition(
                name="idx_event_event_at",
                columns=["event_at"],
            ),
            # GAP-5 idempotent ingest: this COMPOSITE unique is the conflict
            # target for ``append_event``'s ``ON CONFLICT (session_id,
            # external_id) DO NOTHING`` upsert. ``external_id`` =
            # ``vendor_event_id`` ?? a deterministic ``derv:`` hash, so
            # re-ingest of the same source event dedups at the DB level
            # (replaces the unenforced ``vendor_event_id`` idempotency claim).
            # The index goes live in slice 1 — Postgres treats NULLs as DISTINCT,
            # so the legacy null-``external_id`` rows don't violate it; the
            # NOT-NULL constraint follows in slice 2 after the backfill. Non-
            # partial: post-backfill ``external_id`` is never NULL.
            #
            # This is now the SOLE DB-level enforcement of event identity
            # (schema-debt-external-id lane, 2a, 2026-08-06): the
            # platform-wide standalone ``UNIQUE(external_id)`` that used to
            # collide with this composite (a cross-session vendor-event-id
            # match tripped ``session_ledger__event_external_id_key``,
            # degraded to a dedup by the importer's narrow catch in
            # ``ingest.py`` on 2026-08-06) is opted out of above — see the
            # ``external_id`` column's own comment. Root cause fixed at the
            # schema layer; the importer's catch remains as defence for
            # pre-migration databases per
            # workbench/2026-08-06_schema_debt_external_id_findings_schema-debt-impl.md.
            IndexDefinition(
                name="idx_event_session_external_unique",
                columns=["session_id", "external_id"],
                unique=True,
            ),
        ],
    )


def _tool_call_table() -> TableSchema:
    return TableSchema(
        table_name=TABLE_TOOL_CALL,
        id_prefix=ID_PREFIX_TOOL_CALL,
        description="Projection of MESSAGE+TOOL_CALL pairs for fast tool-use queries.",
        columns={
            "session_id": ColumnDefinition(
                type=ColumnType.TEXT,
                not_null=True,
                description="repository_fk: session_ledger__session.id.",
            ),
            "call_event_id": ColumnDefinition(
                type=ColumnType.TEXT,
                not_null=True,
                description="repository_fk: session_ledger__event.id (the TOOL_CALL row).",
            ),
            "result_event_id": ColumnDefinition(
                type=ColumnType.TEXT,
                description=(
                    "repository_fk: session_ledger__event.id (the TOOL_RESULT row), "
                    "NULL while the call is still pending."
                ),
            ),
            "tool_name": ColumnDefinition(
                type=ColumnType.TEXT,
                not_null=True,
            ),
            "status": ColumnDefinition(
                type=ColumnType.TEXT,
                not_null=True,
                check=f"status IN ({_quoted_csv(_TOOL_CALL_STATUS_VALUES)})",
                description="One of pending / succeeded / errored.",
            ),
            "called_at": ColumnDefinition(
                type=ColumnType.DATETIME,
                not_null=True,
            ),
            "resolved_at": ColumnDefinition(
                type=ColumnType.DATETIME,
                description="Set when result_event_id arrives.",
            ),
        },
        indexes=[
            IndexDefinition(
                name="idx_tool_call_name_status",
                columns=["tool_name", "status"],
            ),
            IndexDefinition(
                name="idx_tool_call_session_called",
                columns=["session_id", "called_at"],
            ),
        ],
    )


def _attachment_table() -> TableSchema:
    return TableSchema(
        table_name=TABLE_ATTACHMENT,
        id_prefix=ID_PREFIX_ATTACHMENT,
        description=(
            "Per-event attachment metadata. ``blob_id`` is written ONLY after "
            "clean (text attachments) or after metadata-only scan "
            "(binary attachments). Quarantined text attachments have blob_id NULL."
        ),
        columns={
            "event_id": ColumnDefinition(
                type=ColumnType.TEXT,
                not_null=True,
                description="repository_fk: session_ledger__event.id.",
            ),
            "blob_id": ColumnDefinition(
                type=ColumnType.TEXT,
                description=(
                    "Reference to blob_storage_service. NULL when QUARANTINED."
                ),
            ),
            "mime_type": ColumnDefinition(
                type=ColumnType.TEXT,
                not_null=True,
            ),
            "filename": ColumnDefinition(
                type=ColumnType.TEXT,
            ),
            "size_bytes": ColumnDefinition(
                type=ColumnType.INTEGER,
                not_null=True,
            ),
        },
        indexes=[
            IndexDefinition(
                name="idx_attachment_event",
                columns=["event_id"],
            ),
            IndexDefinition(
                name="idx_attachment_blob",
                columns=["blob_id"],
                where="blob_id IS NOT NULL",
            ),
        ],
    )


def _active_lease_table() -> TableSchema:
    return TableSchema(
        table_name=TABLE_ACTIVE_LEASE,
        id_prefix=ID_PREFIX_ACTIVE_LEASE,
        description=(
            "Push-side keepalive. Sources send lease pings; the repository "
            "calculates expires_at = last_seen_at + lease_ttl_seconds. "
            "Sources never write expires_at directly."
        ),
        columns={
            "session_id": ColumnDefinition(
                type=ColumnType.TEXT,
                not_null=True,
                unique=True,
                description="repository_fk: session_ledger__session.id.",
            ),
            "source_id": ColumnDefinition(
                type=ColumnType.TEXT,
                not_null=True,
                description="repository_fk: session_ledger__source.id.",
            ),
            "last_seen_at": ColumnDefinition(
                type=ColumnType.DATETIME,
                not_null=True,
            ),
            "expires_at": ColumnDefinition(
                type=ColumnType.DATETIME,
                not_null=True,
                description="Platform-calculated from last_seen_at + lease_ttl_seconds.",
            ),
            "lease_ttl_seconds": ColumnDefinition(
                type=ColumnType.INTEGER,
                not_null=True,
                default=300,
                description="Per-source override; default 300.",
            ),
        },
        indexes=[
            IndexDefinition(
                name="idx_lease_expires",
                columns=["expires_at"],
            ),
        ],
    )


def _summary_table() -> TableSchema:
    return TableSchema(
        table_name=TABLE_SUMMARY,
        id_prefix=ID_PREFIX_SUMMARY,
        description=(
            "Created in M1; populated in M6. NUL-byte sanitized before insert "
            "AND before embedding (spec §10.10.3)."
        ),
        columns={
            "session_id": ColumnDefinition(
                type=ColumnType.TEXT,
                not_null=True,
                description="repository_fk: session_ledger__session.id.",
            ),
            "chunk_index": ColumnDefinition(
                type=ColumnType.INTEGER,
                not_null=True,
                description="Order within session.",
            ),
            "summary_text": ColumnDefinition(
                type=ColumnType.TEXT,
                not_null=True,
            ),
            "embedding_vector_id": ColumnDefinition(
                type=ColumnType.TEXT,
                description="Reference into the vector_service embeddings table.",
            ),
            "generated_at": ColumnDefinition(
                type=ColumnType.DATETIME,
                not_null=True,
            ),
            "generated_by_client_id": ColumnDefinition(
                type=ColumnType.TEXT,
                not_null=True,
                description="OAuth client_id from authenticated principal.",
            ),
        },
        indexes=[
            IndexDefinition(
                name="idx_summary_session_chunk_unique",
                columns=["session_id", "chunk_index"],
                unique=True,
            ),
        ],
    )


def _deployment_table() -> TableSchema:
    return TableSchema(
        table_name=TABLE_DEPLOYMENT,
        id_prefix=ID_PREFIX_DEPLOYMENT,
        description=(
            "M5 shipper pairing state. State machine: "
            "pending → approved → paired → revoked (spec §13.2)."
        ),
        columns={
            "machine_id": ColumnDefinition(
                type=ColumnType.TEXT,
                not_null=True,
                description="Operator-supplied or auto-generated installation UUID.",
            ),
            "pairing_status": ColumnDefinition(
                type=ColumnType.TEXT,
                not_null=True,
                check=f"pairing_status IN ({_enum_csv(PairingStatus)})",
                description="StrEnum PairingStatus value.",
            ),
            "pairing_token_hash": ColumnDefinition(
                type=ColumnType.TEXT,
                description="scrypt hash of the one-time poll token; cleared on consumption.",
            ),
            "pairing_token_salt": ColumnDefinition(
                type=ColumnType.TEXT,
                description="scrypt salt.",
            ),
            "user_code": ColumnDefinition(
                type=ColumnType.TEXT,
                description="Plaintext for operator display; cleared on approve_pairing.",
            ),
            "pairing_initiated_at": ColumnDefinition(
                type=ColumnType.DATETIME,
                description="For TTL enforcement (10 min).",
            ),
            "approved_at": ColumnDefinition(
                type=ColumnType.DATETIME,
                description="Set when approve_pairing succeeds.",
            ),
            "oauth_client_id": ColumnDefinition(
                type=ColumnType.TEXT,
                description=(
                    "Set ONLY during the authenticated one-time poll that mints "
                    "the client. NULL until that point."
                ),
            ),
            "initiating_client_id": ColumnDefinition(
                type=ColumnType.TEXT,
                not_null=True,
                description=(
                    "OAuth client_id that called generate_ingest_setup. Used "
                    "for the §13 ownership-binding check on approve_pairing."
                ),
            ),
            "authorized_source_kinds": ColumnDefinition(
                type=ColumnType.JSON,
                not_null=True,
                default="[]",
                description=(
                    "Per Codex item 10: bound at generate_ingest_setup from the "
                    "operator's sources list. Both ingest_blob and "
                    "ingest_raw_chunk validate the request's source_kind "
                    "against this list and reject 403 if not authorized."
                ),
            ),
            "paired_at": ColumnDefinition(
                type=ColumnType.DATETIME,
                description="Set on successful poll.",
            ),
            "revoked_at": ColumnDefinition(
                type=ColumnType.DATETIME,
                description="Set on shipper_self_revoke.",
            ),
        },
        indexes=[
            IndexDefinition(
                name="idx_deployment_status",
                columns=["pairing_status"],
            ),
            IndexDefinition(
                name="idx_deployment_oauth_client_unique",
                columns=["oauth_client_id"],
                unique=True,
                where="oauth_client_id IS NOT NULL",
            ),
        ],
    )


def _session_source_kind_table() -> TableSchema:
    """SQL-lockdown list_sessions junction (Architect ruling 2026-06-22).

    One row per ``(canonical session, distinct source_kind in its
    (vendor, external_session_id) group)``. Replaces the pre-migration
    EXISTS-over-canonical-group ``source_kind`` subquery with a read-then-route:
    ``query_state(this, {source_kind: K})`` → canonical session ids →
    ``query_state(session, {id: ANY(ids), …})`` + a Python fold (the two
    two-sided ``event_at`` windows + the configurable order + limit aren't
    expressible in the one-condition-per-column grammar, so the session read is
    uncapped query_state + Python — the #11/Slice-1 pattern).

    Maintained on the ingest attach-path (``upsert_state`` DO-NOTHING keyed on
    the UNIQUE ``(canonical_session_id, source_kind)``) and recomputed in
    ``canonical_pointer_repair`` AFTER canonical re-election (the survivor
    inherits the demoted group's kinds; ordering is load-bearing).
    """
    return TableSchema(
        table_name=TABLE_SESSION_SOURCE_KIND,
        id_prefix=ID_PREFIX_SESSION_SOURCE_KIND,
        description=(
            "Junction: one row per (canonical session, distinct source_kind in "
            "its (vendor, external_session_id) group). Backs list_sessions' "
            "source_kind filter as a read-then-route over query_state, "
            "replacing the EXISTS-over-canonical-group subquery."
        ),
        columns={
            "canonical_session_id": ColumnDefinition(
                type=ColumnType.TEXT,
                not_null=True,
                description="repository_fk: session_ledger__session.id of the canonical row.",
            ),
            "source_kind": ColumnDefinition(
                type=ColumnType.TEXT,
                not_null=True,
                check=f"source_kind IN ({_enum_csv(IngestSourceKind)})",
                description=(
                    "StrEnum IngestSourceKind value contributed by a group "
                    "member's source."
                ),
            ),
        },
        indexes=[
            IndexDefinition(
                name="idx_session_source_kind_unique",
                columns=["canonical_session_id", "source_kind"],
                unique=True,
            ),
            IndexDefinition(
                name="idx_session_source_kind_by_kind",
                columns=["source_kind", "canonical_session_id"],
            ),
        ],
    )


def get_session_ledger_schema() -> SchemaDefinition:
    """Return the schema definition for the LLM session ledger.

    Registered from ``CoreSchemaDefinitions.get_all_core_schemas()`` (spec §12.6).
    """
    return SchemaDefinition(
        namespace=NAMESPACE,
        version="1.0.0",
        description="LLM session ledger: sources, events, leases.",
        tables={
            TABLE_SOURCE: _source_table(),
            TABLE_SOURCE_CURSOR: _source_cursor_table(),
            TABLE_IMPORT_BATCH: _import_batch_table(),
            TABLE_SESSION: _session_table(),
            TABLE_EVENT: _event_table(),
            TABLE_TOOL_CALL: _tool_call_table(),
            TABLE_ATTACHMENT: _attachment_table(),
            TABLE_ACTIVE_LEASE: _active_lease_table(),
            TABLE_SUMMARY: _summary_table(),
            TABLE_DEPLOYMENT: _deployment_table(),
            TABLE_SESSION_SOURCE_KIND: _session_source_kind_table(),
        },
    )


def _summary_embeddings_table() -> TableSchema:
    """Per-namespace pgvector embeddings table for M6 summary search.

    Shape mirrors ``pgvector_service_plugin``'s own embeddings table
    (``plugin.py:253-287``) so the shared provider that does
    ``store_vectors``/``search_similar`` against the composed table name
    ``<schema>.<namespace>__embeddings`` finds the columns it expects.
    Standard fields (``id``, ``namespace``, ``created_at``, ``updated_at``,
    ``external_id``, ``name``) are auto-injected by ``SchemaStandardizer``.
    """
    return TableSchema(
        table_name=TABLE_SUMMARY_EMBEDDINGS,
        id_prefix="emb",
        description=(
            "M6 summary-chunk vector storage. Isolated from memory_service "
            "vectors via the session_ledger_summary namespace so "
            "search_sessions returns only session summaries."
        ),
        columns={
            "embedding": ColumnDefinition(
                type=ColumnType.VECTOR,
                type_params={},
                not_null=True,
                description="Vector embedding (native pgvector type).",
            ),
            "dimension": ColumnDefinition(
                type=ColumnType.INTEGER,
                not_null=True,
                description="Vector dimension for validation.",
            ),
            "metadata": ColumnDefinition(
                type=ColumnType.TEXT,
                description="JSON metadata: session_id, chunk_index, generated_by_client_id.",
            ),
            "distance_metric": ColumnDefinition(
                type=ColumnType.TEXT,
                description="Distance metric (cosine / l2 / inner). Provider default applies when null.",
            ),
        },
        indexes=[],
    )


def get_session_ledger_summary_embeddings_schema() -> SchemaDefinition:
    """Schema for the M6 summary-vector store (operator ruling 2026-05-31).

    Registered alongside :func:`get_session_ledger_schema` in
    ``CoreSchemaDefinitions.get_all_core_schemas()``. The split namespace
    keeps summary vectors out of the shared
    ``pgvector_service_plugin__embeddings`` table so ``search_sessions``
    returns only session summaries (rejected Option A from Task #8 report
    on search-result-pollution grounds).
    """
    return SchemaDefinition(
        namespace=SUMMARY_VECTOR_NAMESPACE,
        version="1.0.0",
        description=(
            "M6 session-ledger summary vector store. One table "
            "(session_ledger_summary__embeddings) backing "
            "service_interface::session_ledger_service::search_sessions."
        ),
        tables={TABLE_SUMMARY_EMBEDDINGS: _summary_embeddings_table()},
    )


def _event_embeddings_table(dimension: int) -> TableSchema:
    """LED-01 per-namespace pgvector table for event-content search.

    Unlike :func:`_summary_embeddings_table`, the ``embedding`` column
    carries a fixed dimension and an HNSW index. Both are mandatory for
    the event corpus (~1M+ vectors), where seq-scan ANN is unusably slow
    and pgvector can only build an HNSW index on a dimension-typed
    ``vector(N)`` column. ``dimension`` is supplied by the caller, which
    reads it live from the embedder at service start (nomic 768 local /
    titan ~1024 cloud) — it is never hardcoded. Standard fields (``id``,
    ``namespace``, ``created_at``, ``updated_at``, ``external_id``,
    ``name``) are auto-injected by ``SchemaStandardizer``; the producer
    sets ``external_id = f"{event_id}:{chunk_index}"``.
    """
    return TableSchema(
        table_name=TABLE_EVENT_EMBEDDINGS,
        id_prefix="emb",
        description=(
            "LED-01 event-content chunk vector storage. Isolated from the "
            "summary store via the session_ledger_event namespace so "
            "search_sessions stays summary-only."
        ),
        columns={
            "embedding": ColumnDefinition(
                type=ColumnType.VECTOR,
                type_params={"dimension": dimension},
                not_null=True,
                description=(
                    "Vector embedding (native pgvector type), fixed "
                    "dimension so the HNSW index below can build."
                ),
            ),
            "dimension": ColumnDefinition(
                type=ColumnType.INTEGER,
                not_null=True,
                description="Vector dimension for validation.",
            ),
            "metadata": ColumnDefinition(
                type=ColumnType.TEXT,
                description=(
                    "JSON metadata: event_id, session_id, event_at, "
                    "chunk_index."
                ),
            ),
            "distance_metric": ColumnDefinition(
                type=ColumnType.TEXT,
                description=(
                    "Distance metric (cosine / l2 / inner). Provider "
                    "default applies when null."
                ),
            ),
        },
        indexes=[
            IndexDefinition(
                name="idx_event_embeddings_hnsw",
                columns=["embedding"],
                using="hnsw",
                column_operator_classes={"embedding": "vector_cosine_ops"},
                index_with_options={"m": 16, "ef_construction": 64},
            ),
        ],
    )


def build_session_ledger_event_embeddings_schema(dimension: int) -> SchemaDefinition:
    """Schema for the LED-01 event-content vector store (operator ruling 2026-07-06).

    NOT registered in ``CoreSchemaDefinitions.get_all_core_schemas()``: the
    static core-schema list cannot carry a runtime-resolved dimension.
    Instead ``startup_sequence._initialize_schemas`` appends this schema at
    boot alongside the discovery-service schemas, reusing the SAME
    ``_resolve_embedding_dimensions`` value that dimension-types the
    discovery table (``EmbeddingService.get_default_dimensions()`` —
    synchronous, no plugin readiness, no network I/O), and is the sole
    declaration site, avoiding a dual-declaration ownership collision. The
    dimension is injected here (never hardcoded); a change across boots is
    an operator drop-and-recreate migration because the diff engine refuses
    an in-place vector-column reshape.
    """
    return SchemaDefinition(
        namespace=EVENT_VECTOR_NAMESPACE,
        version="1.0.0",
        description=(
            "LED-01 session-ledger event-content vector store. One table "
            "(session_ledger_event__embeddings) backing semantic search "
            "over session_ledger__event.content_text."
        ),
        tables={TABLE_EVENT_EMBEDDINGS: _event_embeddings_table(dimension)},
    )


__all__ = [
    "CONTENT_INLINE_TEXT_MAX_BYTES",
    "EVENT_VECTOR_NAMESPACE",
    "ID_PREFIX_ACTIVE_LEASE",
    "ID_PREFIX_ATTACHMENT",
    "ID_PREFIX_DEPLOYMENT",
    "ID_PREFIX_EVENT",
    "ID_PREFIX_IMPORT_BATCH",
    "ID_PREFIX_SESSION",
    "ID_PREFIX_SOURCE",
    "ID_PREFIX_SOURCE_CURSOR",
    "ID_PREFIX_SUMMARY",
    "ID_PREFIX_TOOL_CALL",
    "NAMESPACE",
    "SUMMARY_VECTOR_NAMESPACE",
    "TABLE_ACTIVE_LEASE",
    "TABLE_ATTACHMENT",
    "TABLE_DEPLOYMENT",
    "TABLE_EVENT",
    "TABLE_EVENT_EMBEDDINGS",
    "TABLE_IMPORT_BATCH",
    "TABLE_SESSION",
    "TABLE_SOURCE",
    "TABLE_SOURCE_CURSOR",
    "TABLE_SUMMARY",
    "TABLE_SUMMARY_EMBEDDINGS",
    "TABLE_TOOL_CALL",
    "build_session_ledger_event_embeddings_schema",
    "get_session_ledger_schema",
    "get_session_ledger_summary_embeddings_schema",
]
