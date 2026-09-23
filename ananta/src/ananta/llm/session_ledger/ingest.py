"""Ingest/write domain mixin for the session-ledger repository.

W5.O cycle 3 (`workbench/2026-06-13_w5o_session_ledger_repository_decomposition_design.md`
§3.2 + §3.11.1 + §3.11.2): relocates the 8 ingest-side public methods, the
5 ingest-private cross-mixin helpers (§3.11.1), the 6 module-level event-shape
validators, the ``_EVENT_SHAPE_VALIDATORS`` dispatch dict, and the
``EventInsertResult`` dataclass (§3.11.2 C8 fold) from the monolith.

Note on C5 fold: ``mark_session_summary_text`` MOVED to ``summarize.py`` per
the W5.O review C5 fold (KB-article 03 coherence for ``summary_text``-write
operations). Cycle 3's public surface is therefore 8 methods, not 9 as the
v1 design's §5.3 cycle 3 row pre-fold suggested.

Per Architect's §3.2:

- ``insert_source``, ``get_source``, ``find_source_id_by_kind_and_root_uri``,
  ``upsert_session``, ``append_event``, ``record_tool_call``,
  ``resolve_tool_call``, ``record_attachment`` (8 public verbs)

Per §3.11.1 cross-mixin private helpers:

- ``_resolve_canonical_session`` — Ingest-only Phase-2 helper for
  ``_insert_session_with_canonical_dispatch`` (resolves the canonical row's
  external_session_id when Phase 1's partial-unique ON CONFLICT fires). The
  pre-Slice-6 docstring claimed CanonicalPointerRepair also called it via MI
  access; that was stale (canonical_pointer_repair.py has no reference to it).
- ``_insert_session_with_canonical_dispatch`` — Ingest-only (upsert_session
  M18 two-phase INSERT helper).
- ``_next_sequence`` — Ingest-only (append_event MAX(sequence)+1 allocator).
- ``_touch_session_counters`` — Ingest-only (append_event last_event_at
  high-water update).
- ``_validate_event_shape`` — Ingest-only (append_event shape contract
  enforcement).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from ananta.core.domain.enums import ActionStatus
from ananta.interfaces.state_management_interface import StateTransaction
from ananta.llm.session_ledger.base import (
    LedgerRepositoryError,
    SessionLedgerRepositoryBase,
)
from ananta.llm.session_ledger.root_uri import normalize_root_uri
from ananta.llm.session_ledger.schema import (
    ID_PREFIX_ATTACHMENT,
    ID_PREFIX_EVENT,
    ID_PREFIX_SESSION,
    ID_PREFIX_SESSION_SOURCE_KIND,
    ID_PREFIX_SOURCE,
    ID_PREFIX_TOOL_CALL,
    NAMESPACE,
    TABLE_ATTACHMENT,
    TABLE_EVENT,
    TABLE_SESSION,
    TABLE_SESSION_SOURCE_KIND,
    TABLE_SOURCE,
    TABLE_TOOL_CALL,
)
from ananta.llm.session_ledger.shared import (
    SourceRow,
    _as_aware_utc,
    _new_id,
    _row_to_source,
    _strip_nuls,
    _strip_nuls_in_json,
)
from ananta.llm.session_ledger.types import (
    EventType,
    IngestSourceKind,
    MessageRole,
    NormalizedSessionEvent,
    SourceVendor,
)

# Module-level RELOAD_SAFE marker — pure mixin + module-level validators.
RELOAD_SAFE = True


# SQL-lockdown Slice 6 keystone (Architect ruling 2026-06-21 = Option B): the
# canonical-dispatch helpers (``_resolve_canonical_session`` +
# ``_insert_session_with_canonical_dispatch``) now ride the state-interface
# primitives — Phase 1 via ``upsert_state`` DO-NOTHING (the partial-unique
# ON CONFLICT), the canonical resolve via ``query_state`` ``is_null``, and the
# Phase 2 demotion INSERT via ``write_state``. No raw SQL remains in this module.


# 2026-08-06 finding (workbench/2026-08-05_canonical_memory_and_ledger_verification_findings.md
# #3): the SchemaStandardizer auto-injects its OWN full-table ``UNIQUE(external_id)``
# constraint on every table by platform convention (the standardizer's opaque
# write-idempotency conflict key) — a name collision with THIS table's own,
# unrelated reuse of ``external_id`` as a composite, per-session-scoped business
# column (the GAP-5 ``idx_event_session_external_unique (session_id, external_id)``
# index above). Measured live: this standardizer constraint is named
# ``session_ledger__event_external_id_key`` and is a SEPARATE, single-column
# index the ``ON CONFLICT (session_id, external_id) DO NOTHING`` upsert does not
# target — a rare cross-session external_id collision (structurally possible
# whenever two different sessions' events land on the derived-hash fallback path)
# trips this constraint, is NOT absorbed by the upsert, and raises. Un-caught here,
# that skipped the WHOLE session in the importer's outer per-session handler
# (``ledger poll: skipping session``), and — because a failed session's read
# cursor never advances — the SAME poisoned event re-fired on every subsequent
# poll (observed: some sessions skipped 30-40x). This narrows the blast radius to
# the ONE call site that can hit it, degrading a known collision to the same
# already-exists outcome the intended composite upsert already produces for a
# same-session duplicate — never the whole session.
#
# ROOT CAUSE FIXED AT THE SCHEMA LAYER (schema-debt-external-id lane, 2a,
# 2026-08-06): ``session_ledger/schema.py``'s ``event`` table now declares an
# explicit ``external_id: unique=False`` override, opting out of the
# standardizer's standalone constraint; the standalone
# ``session_ledger__event_external_id_key`` this module was catching drops
# automatically at the next solet boot after that fix lands (the
# platform's own schema-diff reconciliation, no separate migration step —
# see workbench/2026-08-06_schema_debt_external_id_findings_schema-debt-impl.md).
# This catch REMAINS regardless, as defence for pre-migration databases (a
# clone or seed instance booting against data created before that boot ran)
# — once the constraint is actually gone on a given database, this branch
# simply never matches and is a harmless no-op, never a silent hazard.
_STALE_EXTERNAL_ID_UNIQUE_CONSTRAINT = "session_ledger__event_external_id_key"


def _is_known_external_id_collision(exc: LedgerRepositoryError) -> bool:
    """True iff ``exc`` is the specific, understood
    ``_STALE_EXTERNAL_ID_UNIQUE_CONSTRAINT`` violation — never a broader
    "any unique-constraint failure" match. An unrelated database failure
    (connection loss, a genuinely unexpected constraint) must still raise
    loud; only this ONE known, named collision degrades to a dedup."""
    return _STALE_EXTERNAL_ID_UNIQUE_CONSTRAINT in str(exc)


def _upsert_event_or_dedup_known_collision(
    upsert_do_nothing: Callable[..., bool], record: dict[str, object],
) -> bool:
    """``append_event``'s upsert call, wrapped: degrades the ONE known
    ``_STALE_EXTERNAL_ID_UNIQUE_CONSTRAINT`` violation to the same
    ``inserted=False`` outcome the intended composite upsert already
    produces for a same-session duplicate. Split out (module-level, not a
    mixin method) to keep ``SessionLedgerIngestMixin`` under the god-class
    LOC threshold — a genuine gate red caught while landing this fix."""
    try:
        return upsert_do_nothing(
            TABLE_EVENT,
            record,
            conflict_columns=["session_id", "external_id"],
            conflict_predicate=[],
        )
    except LedgerRepositoryError as exc:
        if not _is_known_external_id_collision(exc):
            raise
        return False


# ─── EventInsertResult dataclass (W5.O C8 fold) ───────────────────────────


@dataclass(frozen=True, slots=True)
class EventInsertResult:
    event_id: str
    sequence: int
    # GAP-5 idempotent ingest: True when the (session_id, external_id) upsert hit
    # an existing row (DO NOTHING) — the event already existed, so NO counters
    # were bumped and the caller MUST skip the child projections (tool_call /
    # attachment) to avoid double-writing them.
    deduped: bool = False


# ─── Per-event-type shape validators (spec §9 table) ──────────────────────
# Each helper stays A; the orchestrator (``_validate_event_shape``) is a dict
# dispatch.


def _has_attachment_fields(normalized: NormalizedSessionEvent) -> bool:
    return (
        normalized.attachment_blob_upload is not None
        or normalized.attachment_mime_type is not None
        or normalized.attachment_filename is not None
    )


def _validate_message_event(normalized: NormalizedSessionEvent) -> None:
    if normalized.role is None or normalized.role is MessageRole.TOOL:
        raise ValueError(
            "MESSAGE events require role in {user, assistant, system}",
        )
    if normalized.content_text is None and normalized.content_json is None:
        raise ValueError("MESSAGE events require content_text or content_json")
    if _has_attachment_fields(normalized):
        raise ValueError("MESSAGE events must not carry attachment fields")


def _validate_tool_call_event(normalized: NormalizedSessionEvent) -> None:
    if normalized.content_json is None:
        raise ValueError("TOOL_CALL events require content_json")
    if normalized.content_text is not None or _has_attachment_fields(normalized):
        raise ValueError("TOOL_CALL events must carry only content_json")


def _validate_tool_result_event(normalized: NormalizedSessionEvent) -> None:
    if normalized.content_text is None and normalized.content_json is None:
        raise ValueError("TOOL_RESULT events require content_text or content_json")
    if _has_attachment_fields(normalized):
        raise ValueError("TOOL_RESULT events must not carry attachment fields")


def _validate_system_event(normalized: NormalizedSessionEvent) -> None:
    if normalized.role is not MessageRole.SYSTEM:
        raise ValueError("SYSTEM events require role='system'")
    if normalized.content_text is None and normalized.content_json is None:
        raise ValueError("SYSTEM events require content_text or content_json")
    if _has_attachment_fields(normalized):
        raise ValueError("SYSTEM events must not carry attachment fields")


def _validate_attachment_event(normalized: NormalizedSessionEvent) -> None:
    if normalized.content_text is not None or normalized.content_json is not None:
        raise ValueError(
            "ATTACHMENT events must carry only attachment fields, not text/json content",
        )
    if normalized.attachment_mime_type is None:
        raise ValueError("ATTACHMENT events require attachment_mime_type")


_EVENT_SHAPE_VALIDATORS: dict[
    EventType, Callable[[NormalizedSessionEvent], None]
] = {
    EventType.MESSAGE: _validate_message_event,
    EventType.TOOL_CALL: _validate_tool_call_event,
    EventType.TOOL_RESULT: _validate_tool_result_event,
    EventType.SYSTEM: _validate_system_event,
    EventType.ATTACHMENT: _validate_attachment_event,
}


# ─── Session-upsert module-level helpers ──────────────────────────────────
# Extracted as module-level (not methods on the mixin) per the W5.O cycle 3
# further-split authorization — keeps SessionLedgerIngestMixin under the
# god-class 500 non-process LOC threshold. Post-Slice-6 they take the
# repository base (``repo``) and drive its autocommit state-interface seams
# (``_query`` / ``_upsert_do_nothing`` / ``_write`` / ``_update``) rather than
# composing raw SQL on a ``StateTransaction``.


def _resolve_canonical_session(
    repo: SessionLedgerRepositoryBase,
    *,
    vendor: SourceVendor,
    external_session_id: str,
) -> dict[str, object] | None:
    """Return the currently-canonical row for a (vendor, external_session_id) pair.

    Returns the full canonical row (the caller reads its ``external_session_id``
    for the demoted sibling's pointer AND its ``id`` for the list_sessions
    junction maintenance), or ``None`` when no canonical row exists. The lookup
    is partial-unique-index-backed (idx_session_canonical_one_per_vendor_pair),
    so at most one row matches.

    M18 §3.3: Phase 2 of the two-phase upsert calls this when Phase 1's
    ``ON CONFLICT DO NOTHING`` fires — meaning the partial-unique constraint
    blocked the canonical-NULL INSERT, so another row is already canonical.
    The caller demotes the new row to non-canonical by populating its
    canonical_external_session_id with this return value.

    SQL-lockdown Slice 6 (Option B, autocommit): the canonical lookup is the
    ``query_state`` equality + ``canonical_external_session_id IS NULL`` filter
    (the flat grammar's ``is_null`` op). Phase 1's DO-NOTHING wrote nothing on
    the conflict path, so there is no same-flow uncommitted write for this
    resolve to need to see — an autocommit read is faithful (Architect ruling
    2026-06-21); single-writer-per-session via the serial-dispatch + lease
    fences means the canonical row this reads cannot race away mid-upsert.
    """
    rows = repo._query(
        TABLE_SESSION,
        {
            "vendor": vendor.value,
            "external_session_id": external_session_id,
            "canonical_external_session_id": {"op": "is_null"},
            "is_deleted": 0,
        },
    )
    if not rows:
        return None
    return rows[0]


def _attach_session_source_kind(
    repo: SessionLedgerRepositoryBase,
    *,
    canonical_id: str,
    source_kind: IngestSourceKind,
    now: datetime,
) -> None:
    """Record ``(canonical_id, source_kind)`` in the list_sessions junction.

    SQL-lockdown list_sessions junction maintenance (ingest attach-path). The
    write is ``upsert_state`` DO-NOTHING on the UNIQUE
    ``(canonical_session_id, source_kind)``, so it is idempotent: a re-poll, or a
    second sibling contributing an already-present kind, is a no-op. The
    ``source_kind`` is the attaching source's own kind, THREADED from the
    importer (it is polling that source — the same value it passes to
    ``append_event``; no per-insert ``__source`` re-read, no source-existence
    coupling on the hot path). For a new canonical it is the canonical's kind;
    for a demoted sibling it is the sibling's kind contributed to the canonical's
    group. The canonical row id (not its external_session_id) is the junction key
    so the list_sessions read can route ``canonical_ids → query_state(session,
    id=ANY)``.
    """
    repo._upsert_do_nothing(
        TABLE_SESSION_SOURCE_KIND,
        {
            "id": _new_id(ID_PREFIX_SESSION_SOURCE_KIND),
            "namespace": NAMESPACE,
            "canonical_session_id": canonical_id,
            "source_kind": source_kind.value,
            "created_at": now,
            "updated_at": now,
        },
        conflict_columns=["canonical_session_id", "source_kind"],
        conflict_predicate=[],
    )


def _insert_session_with_canonical_dispatch(
    repo: SessionLedgerRepositoryBase,
    *,
    session_id: str,
    source_id: str,
    vendor: SourceVendor,
    source_kind: IngestSourceKind,
    clean_external_session_id: str,
    clean_vendor_session_label: str | None,
    clean_project_path: str | None,
    first_event_at: datetime,
    last_event_at: datetime,
    clean_originator_session_label: str | None,
    clean_originator_agent_instance_id: str | None,
    clean_recipient_session_label: str | None,
    clean_recipient_agent_instance_id: str | None,
    clean_summary_text_seed: str | None,
    now: datetime,
) -> str:
    """M18 two-phase INSERT — Phase 1 canonical=NULL DO-NOTHING; Phase 2 demote-to-pointer.

    SQL-lockdown Slice 6 (Architect ruling 2026-06-21 = Option B, autocommit):

    * Phase 1 rides ``upsert_state`` DO-NOTHING with the structured
      ``conflict_predicate`` mirroring the M18 partial-unique index
      (``ON CONFLICT (vendor, external_session_id) WHERE
      canonical_external_session_id IS NULL AND is_deleted = 0``). Its
      ``inserted`` bool IS the landed signal — it replaces the pre-migration
      SELECT-back ``fetch_one`` (a ``DO NOTHING`` that wrote nothing returns
      ``inserted=False`` directly). Column+WHERE inference (NOT a named
      constraint) is required because the DDL renderer hash-suffixes the
      partial-index name.
    * On conflict (``inserted=False``) the loser is demoted: resolve the
      canonical row and INSERT the new row with the canonical pointer
      (the resolved row's external_session_id) populated via ``write_state``.

    SQL-lockdown list_sessions junction maintenance: BOTH branches call
    :func:`_attach_session_source_kind` to record ``(canonical row id, the
    attaching source's source_kind)`` (idempotent DO-NOTHING) so the migrated
    ``list_sessions`` source_kind filter routes through the junction. The
    canonical row's ``id`` (now surfaced by ``_resolve_canonical_session``
    returning the row) is the junction key.

    The pre-migration ``transactional()`` wrapped no multi-write atomicity
    (≤1 row-write per path); the dedupe race-closure is the PG-internal
    partial-unique ON CONFLICT, which is transaction-independent and survives
    autocommit. Single-writer-per-session (serial-dispatch + lease fences)
    closes the existing-check→insert TOCTOU.
    """
    base_record: dict[str, object] = {
        "id": session_id,
        "namespace": NAMESPACE,
        "source_id": source_id,
        "external_session_id": clean_external_session_id,
        "vendor": vendor.value,
        "vendor_session_label": clean_vendor_session_label,
        "project_path": clean_project_path,
        "first_event_at": first_event_at,
        "last_event_at": last_event_at,
        "event_count": 0,
        "originator_session_label": clean_originator_session_label,
        "originator_agent_instance_id": clean_originator_agent_instance_id,
        "recipient_session_label": clean_recipient_session_label,
        "recipient_agent_instance_id": clean_recipient_agent_instance_id,
        "summary_text": clean_summary_text_seed,
        "created_at": now,
        "updated_at": now,
    }
    inserted = repo._upsert_do_nothing(
        TABLE_SESSION,
        {**base_record, "canonical_external_session_id": None},
        conflict_columns=["vendor", "external_session_id"],
        conflict_predicate=[
            {"column": "canonical_external_session_id", "op": "is_null"},
            {"column": "is_deleted", "op": "eq", "value": 0},
        ],
    )
    if inserted:
        # New canonical: it is its own group head — record its kind.
        _attach_session_source_kind(
            repo, canonical_id=session_id, source_kind=source_kind, now=now,
        )
        return session_id
    canonical_row = _resolve_canonical_session(
        repo,
        vendor=vendor,
        external_session_id=clean_external_session_id,
    )
    if canonical_row is None:
        raise RuntimeError(
            "upsert_session canonical race: Phase 1 ON CONFLICT fired "
            "but _resolve_canonical_session returned None for "
            f"(vendor={vendor.value!r}, "
            f"external_session_id={clean_external_session_id!r}). "
            "Indicates schema/data anomaly — partial-unique constraint "
            "and lookup query disagree."
        )
    repo._write(
        TABLE_SESSION,
        {
            **base_record,
            "canonical_external_session_id": str(
                canonical_row["external_session_id"]
            ),
        },
    )
    # Demoted sibling: contribute its source_kind to the canonical's group.
    _attach_session_source_kind(
        repo,
        canonical_id=str(canonical_row["id"]),
        source_kind=source_kind,
        now=now,
    )
    return session_id


# ─── Session-upsert UPDATE-path read-compute-write (LEAST/GREATEST/COALESCE) ─
# The pre-migration UPDATE did ``first_event_at = LEAST(first_event_at, %s)``,
# ``last_event_at = GREATEST(last_event_at, %s)``, and seven ``COALESCE`` merges
# in one atomic-per-row statement. The state-interface ``update_state`` SET
# grammar is column = value only (no SQL ``LEAST``/``GREATEST``/``COALESCE``
# expressions), so the bound is recomputed in Python from a read of the current
# row and written back. That read-then-write is NON-commutative, so it is safe
# only when no two writers race the same session's row.
#
# THE REAL FENCE (Architect ruling 2026-06-21, code-grounded — supersedes this
# file's earlier "pushed dispatch is single-caller" guess, which named the wrong
# mechanism): the platform's action queue dispatches SERIALLY. ``ActionQueuePoller``
# runs ONE ``_poll_loop``; ``_poll_once`` drains the claimed actions in a serial
# ``for action: await self._process_action(action)`` loop; ``_process_action``
# AWAITS ``run_in_executor(execute_action)`` to completion before advancing. The
# write paths into a session's bounds are: ``upsert_session`` (importer-only) via
# the lease-fenced pulling poll OR via ``ingest_raw_chunk`` — a SYNCHRONOUS
# (``is_async=False``) ``@service_interface_process`` EDGE terminal whose whole
# ``dispatch_pushed`` → ``upsert_session`` persist runs to completion inside that
# one awaited call, so N concurrent shippers enqueue and drain ONE-AT-A-TIME;
# ``append_event``'s ``last_event_at`` bump is additionally row-lock-safe (its
# ``increment_and_return`` takes the session row lock first in the same txn);
# ``inverted_bounds_repair`` is operator-gated + quiescent. The DB is
# per-solet. So no two writers overlap on a session.
#
# ⚠ LOAD-BEARING: this safety holds ONLY while ``ingest_raw_chunk`` stays a
# SYNCHRONOUS service-interface process whose persist runs INLINE before it
# returns. Two ways it could break SILENTLY: (a) it is made ``is_async`` /
# self-completing (guarded by ``ingest_raw_chunk_sync_dispatch_tripwire_smoke.py``);
# (b) it is refactored to spawn the persist and return early (its synchronous
# ``events_persisted`` / ``batch_id`` return contract is what forbids that — a
# registry-flag tripwire would NOT catch a spawn-and-return-early refactor, so
# keep the persist inline). Either reopens the RCW race — hitting NOT JUST
# first/last_event_at but ALSO the COALESCE-keep snapshot cols below
# (originator/recipient/summary_text). (M6.5 Bug 2 chose the SQL-atomic shape
# specifically for concurrent-ingest safety; the serial-dispatch fence is its
# replacement.)


def _coalesce_new_wins(new_value: object, existing_value: object) -> object:
    """``COALESCE(new, existing)`` — the incoming value wins unless it is NULL.

    Matches the pre-migration ``col = COALESCE(%s, col)`` shape for
    ``vendor_session_label`` / ``project_path``: a re-poll carrying fresh
    metadata overwrites; a re-poll with no metadata leaves the stored value.
    """
    return new_value if new_value is not None else existing_value


def _coalesce_keep(existing_value: object, new_value: object) -> object:
    """``COALESCE(existing, new)`` — the stored value wins (snapshot semantics).

    Matches the pre-migration ``col = COALESCE(col, %s)`` shape for the four
    originator/recipient actor columns + ``summary_text``: per the 2026-05-31
    Architect ruling §2 an already-populated snapshot column is never
    overwritten; only a NULL is backfilled.
    """
    return existing_value if existing_value is not None else new_value


def _update_existing_session(
    repo: SessionLedgerRepositoryBase,
    *,
    existing: dict[str, object],
    first_event_at: datetime,
    last_event_at: datetime,
    clean_vendor_session_label: str | None,
    clean_project_path: str | None,
    clean_originator_session_label: str | None,
    clean_originator_agent_instance_id: str | None,
    clean_recipient_session_label: str | None,
    clean_recipient_agent_instance_id: str | None,
    clean_summary_text_seed: str | None,
    now: datetime,
) -> str:
    """Apply the LEAST/GREATEST/COALESCE merge to an already-existing session row.

    ``existing`` is the full current row (a ``query_state`` ``SELECT *`` record).
    The event-time bounds widen in Python via ``min``/``max`` over aware-UTC
    operands (``_as_aware_utc`` normalizes the naive-stored cells + the
    vendor-supplied candidates); the autocommit ``update_state`` (``repo._update``)
    then writes the merged values back. The serializer re-naives the aware
    datetimes at the write boundary (F1 seam). SQL-lockdown Slice 6: this single
    predicated UPDATE is Slice-5-safe without a transaction (the existing-row
    read + this write-back do not race under the serial-dispatch + lease fences).
    Returns the session id.
    """
    session_id = str(existing["id"])
    new_first = min(
        _as_aware_utc(existing["first_event_at"]), _as_aware_utc(first_event_at)
    )
    new_last = max(
        _as_aware_utc(existing["last_event_at"]), _as_aware_utc(last_event_at)
    )
    repo._update(
        TABLE_SESSION,
        {"id": session_id},
        {
            "first_event_at": new_first,
            "last_event_at": new_last,
            "vendor_session_label": _coalesce_new_wins(
                clean_vendor_session_label, existing.get("vendor_session_label")
            ),
            "project_path": _coalesce_new_wins(
                clean_project_path, existing.get("project_path")
            ),
            "originator_session_label": _coalesce_keep(
                existing.get("originator_session_label"),
                clean_originator_session_label,
            ),
            "originator_agent_instance_id": _coalesce_keep(
                existing.get("originator_agent_instance_id"),
                clean_originator_agent_instance_id,
            ),
            "recipient_session_label": _coalesce_keep(
                existing.get("recipient_session_label"),
                clean_recipient_session_label,
            ),
            "recipient_agent_instance_id": _coalesce_keep(
                existing.get("recipient_agent_instance_id"),
                clean_recipient_agent_instance_id,
            ),
            "summary_text": _coalesce_keep(
                existing.get("summary_text"), clean_summary_text_seed
            ),
            "updated_at": now,
        },
    )
    return session_id


# ─── The Ingest mixin ─────────────────────────────────────────────────────


class SessionLedgerIngestMixin(SessionLedgerRepositoryBase):
    """Ingest/write domain mixin.

    Per S1 of the W5.O review: IMPL-mixin organization is by implementation
    cohesion, NOT API-ABC alignment. The polling-lease / cursor / batch
    primitives that back the Ingest-API verbs ``register_source``,
    ``ingest_raw_chunk``, and ``get_import_status`` live in ``polling_driver.py``
    (cycle 5), not here.
    """

    __slots__ = ()

    # ------------------------------------------------------------------
    # Source insertion + lookups
    # ------------------------------------------------------------------

    def insert_source(
        self,
        *,
        source_kind: IngestSourceKind,
        root_uri: str,
        account_label: str | None,
        enabled: bool = True,
        config: dict[str, Any] | None = None,
    ) -> str:
        source_id = _new_id(ID_PREFIX_SOURCE)
        now = self._clock()
        # Defense-in-depth (P1.1.B): the registration apply seam already
        # canonicalizes, so this lexical pass is an idempotent fixed point for
        # ``file:///<abs>`` rows and a pass-through for blob-id / sentinel
        # ``root_uri`` values — it only matters for a direct ``insert_source``
        # caller that bypassed the seam.
        normalized_root_uri = normalize_root_uri(root_uri)
        # ``config_json`` is passed as a Python dict; the write layer serializes
        # it to JSONB (no caller ``json.dumps`` / ``::jsonb`` cast).
        self._write(
            TABLE_SOURCE,
            {
                "id": source_id,
                "namespace": NAMESPACE,
                "source_kind": source_kind.value,
                "root_uri": _strip_nuls(normalized_root_uri),
                "account_label": _strip_nuls(account_label),
                "enabled": enabled,
                "config_json": config or {},
                "created_at": now,
                "updated_at": now,
            },
        )
        return source_id

    def get_source(self, source_id: str) -> SourceRow | None:
        rows = self._query(TABLE_SOURCE, {"id": source_id, "is_deleted": 0})
        if not rows:
            return None
        return _row_to_source(rows[0])

    def repoint_source_root_uri(self, source_id: str, new_root_uri: str) -> bool:
        """Point a live source row at a different ``root_uri``; True if a row changed.

        Used by the export-blob-identity backfill (A3 Phase 1) to converge a
        source onto a content-deduplicated blob id. ``new_root_uri`` is stored
        verbatim (the caller has already resolved the canonical blob id); it is
        NUL-sanitized like every other text write. Touches only a single live
        (``is_deleted = 0``) row.
        """
        now = self._clock()
        affected = self._update(
            TABLE_SOURCE,
            {"id": source_id, "is_deleted": 0},
            {"root_uri": _strip_nuls(new_root_uri), "updated_at": now},
        )
        return affected > 0

    def find_source_id_by_kind_and_root_uri(
        self,
        *,
        source_kind: IngestSourceKind,
        root_uri: str,
    ) -> str | None:
        """Return the source id matching ``(source_kind, root_uri)`` or ``None``.

        The schema description (``_source_table()``) names ``(source_kind, root_uri)``
        as the row's logical key; v1 has no DB-level UNIQUE so the guard lives
        in code. Used by :meth:`SessionLedgerService.register_source` to make
        the verb idempotent across reboots — operator/boot starting_actions
        that name a known source receive the existing id instead of inserting
        a duplicate row.
        """
        rows = self._query_ordered(
            TABLE_SOURCE,
            filters={
                "source_kind": source_kind.value,
                "root_uri": normalize_root_uri(root_uri),
            },
            order_by=[["created_at", "asc"], ["id", "asc"]],
            limit=1,
        )
        if not rows:
            return None
        return str(rows[0]["id"])

    # ------------------------------------------------------------------
    # Session upsert
    # ------------------------------------------------------------------

    def upsert_session(
        self,
        *,
        source_id: str,
        external_session_id: str,
        vendor: SourceVendor,
        source_kind: IngestSourceKind,
        vendor_session_label: str | None,
        project_path: str | None,
        first_event_at: datetime,
        last_event_at: datetime,
        originator_session_label: str | None = None,
        originator_agent_instance_id: str | None = None,
        recipient_session_label: str | None = None,
        recipient_agent_instance_id: str | None = None,
        summary_text_seed: str | None = None,
    ) -> str:
        """Upsert one session row; snapshot the per-peer cols on first INSERT only.

        Per 2026-05-31 Architect ruling §2: the 4 actor-snapshot columns +
        ``summary_text_seed`` are written on the INSERT path only. On the
        UPDATE path they fall through to ``COALESCE(<new>, <existing>)``
        so a re-poll with the same metadata is a no-op, but a re-poll that
        suddenly carries metadata (the first time after a vendor adds
        ``agent-name`` lines, e.g.) backfills the row. Snapshot semantics
        guarantee: an already-populated column is never overwritten with
        ``NULL`` on UPDATE; existing values stick.

        M18 cross-source dedupe (§3.3): INSERT path uses the two-phase
        first-write-wins pattern. Phase 1 attempts the
        INSERT with ``canonical_external_session_id = NULL``; the partial-
        unique index ``idx_session_canonical_one_per_vendor_pair`` allows
        at most one canonical row per ``(vendor, external_session_id)``.
        When the constraint fires (rowcount=0), Phase 2 resolves the
        existing canonical row's external_session_id via
        ``_resolve_canonical_session`` and INSERTs with the canonical
        pointer populated. UPDATE path leaves the pointer untouched —
        within-rank tiebreaker is first-write-wins at the database level,
        not a re-evaluation per UPDATE.

        ⚠ LOAD-BEARING CONCURRENCY DEPENDENCY (Architect ruling 2026-06-21):
        the UPDATE path is a Python read-compute-write of first/last_event_at +
        the COALESCE merges (``_update_existing_session``), which is non-
        commutative and therefore race-unsafe under concurrent writers to one
        session. It is safe ONLY because the platform's action queue dispatches
        SERIALLY and the push entrypoint ``ingest_raw_chunk`` is a SYNCHRONOUS
        (``is_async=False``) ``@service_interface_process`` EDGE terminal that
        runs to completion before the next action (pulling polls are separately
        lease-fenced). If ``ingest_raw_chunk`` is ever made async/self-
        completing, the race returns silently. Guarded by
        ``ingest_raw_chunk_sync_dispatch_tripwire_smoke.py`` — see the module
        comment above ``_update_existing_session``.
        """
        now = self._clock()
        # Operator ruling 2026-06-01: strip NULs at the store boundary on
        # every vendor-/operator-supplied TEXT column. external_session_id
        # is stripped too because the session id seed comes from JSONL.
        clean_external_session_id = _strip_nuls(external_session_id) or ""
        clean_vendor_session_label = _strip_nuls(vendor_session_label)
        clean_project_path = _strip_nuls(project_path)
        clean_originator_session_label = _strip_nuls(originator_session_label)
        clean_originator_agent_instance_id = _strip_nuls(originator_agent_instance_id)
        clean_recipient_session_label = _strip_nuls(recipient_session_label)
        clean_recipient_agent_instance_id = _strip_nuls(recipient_agent_instance_id)
        clean_summary_text_seed = _strip_nuls(summary_text_seed)
        # SQL-lockdown Slice 6 (Architect ruling 2026-06-21 = Option B): the
        # canonical dispatch dissolves to autocommit. The existing-row read +
        # the single predicated write per branch carry no multi-write atomicity
        # that needs a transaction, and the dedupe race-closure is the
        # PG-internal partial-unique ON CONFLICT (txn-independent). Single-
        # writer-per-session (serial-dispatch + lease fences) closes the
        # existing-check → insert/update TOCTOU.
        existing_rows = self._query(
            TABLE_SESSION,
            {
                "source_id": source_id,
                "external_session_id": clean_external_session_id,
                "is_deleted": 0,
            },
        )
        if existing_rows:
            return _update_existing_session(
                self,
                existing=existing_rows[0],
                first_event_at=first_event_at,
                last_event_at=last_event_at,
                clean_vendor_session_label=clean_vendor_session_label,
                clean_project_path=clean_project_path,
                clean_originator_session_label=clean_originator_session_label,
                clean_originator_agent_instance_id=clean_originator_agent_instance_id,
                clean_recipient_session_label=clean_recipient_session_label,
                clean_recipient_agent_instance_id=clean_recipient_agent_instance_id,
                clean_summary_text_seed=clean_summary_text_seed,
                now=now,
            )
        session_id = _new_id(ID_PREFIX_SESSION)
        return _insert_session_with_canonical_dispatch(
            self,
            session_id=session_id,
            source_id=source_id,
            vendor=vendor,
            source_kind=source_kind,
            clean_external_session_id=clean_external_session_id,
            clean_vendor_session_label=clean_vendor_session_label,
            clean_project_path=clean_project_path,
            first_event_at=first_event_at,
            last_event_at=last_event_at,
            clean_originator_session_label=clean_originator_session_label,
            clean_originator_agent_instance_id=clean_originator_agent_instance_id,
            clean_recipient_session_label=clean_recipient_session_label,
            clean_recipient_agent_instance_id=clean_recipient_agent_instance_id,
            clean_summary_text_seed=clean_summary_text_seed,
            now=now,
        )

    # ------------------------------------------------------------------
    # Event insertion
    # ------------------------------------------------------------------

    def append_event(
        self,
        *,
        session_id: str,
        normalized: NormalizedSessionEvent,
        batch_id: str,
        content_blob_id: str | None,
        session_vendor: SourceVendor,
        source_kind: IngestSourceKind,
        external_id: str,
    ) -> EventInsertResult:
        """Insert an event row with full content — idempotent on ``external_id``.

        Schema contract: ``content_text`` is INLINE iff size is at or under
        ``CONTENT_INLINE_TEXT_MAX_BYTES``; otherwise the upstream importer has
        offloaded the payload to blob storage and supplied a non-None
        ``content_blob_id`` here. In that case ``content_text`` MUST be NULL
        on the row (the content lives in blob storage).

        SQL-lockdown Slice 7: ``session_vendor`` + ``source_kind`` are the
        parent session's ``vendor`` and its source's ``source_kind``,
        denormalized onto the event row at write time so
        ``list_events_by_source_window`` reads a single table instead of a
        3-table JOIN. The caller (the importer) already holds both — the
        ``vendor`` it just passed to ``upsert_session`` and the
        ``source.source_kind`` it is polling — so no per-event lookup is
        needed. Both are INSERT-only on their source rows + events are never
        re-parented, so this snapshot can never drift (see the column
        descriptions in ``schema.py``).
        """
        self._validate_event_shape(normalized)
        event_id = _new_id(ID_PREFIX_EVENT)
        now = self._clock()
        content_text = (
            None if content_blob_id is not None else _strip_nuls(normalized.content_text)
        )
        content_json_obj = _strip_nuls_in_json(normalized.content_json)
        usage_json_obj = _strip_nuls_in_json(normalized.usage_json)
        vendor_event_id = _strip_nuls(normalized.vendor_event_id)
        vendor_parent_event_id = _strip_nuls(normalized.vendor_parent_event_id)
        actor_session_label = _strip_nuls(normalized.actor_session_label)
        actor_agent_instance_id = _strip_nuls(normalized.actor_agent_instance_id)
        role_value = normalized.role.value if normalized.role is not None else None

        # GAP-5 idempotent-ingest decouple (design §BUILD SPEC / OPT-A): the event
        # INSERT is the AUTOCOMMIT ``ON CONFLICT (session_id, external_id) DO
        # NOTHING`` upsert (no txn upsert exists), committing BEFORE the gated
        # counter txn; ALL counters (event_count, batch, last_event_at, children)
        # are gated on ``inserted`` so a deduped event mutates nothing. Sequence is
        # MAX(sequence)+1 (``_next_sequence``) — crash-safe, but no longer row-lock
        # allocated: collision-freedom rests on the serial-dispatch fence above,
        # with ``idx_event_session_sequence_unique`` the LOUD backstop if violated.
        provisional_sequence = self._next_sequence(session_id)
        record: dict[str, object] = {
            "id": event_id,
            "namespace": NAMESPACE,
            "session_id": session_id,
            "sequence": provisional_sequence,
            "external_id": external_id,
            "event_type": normalized.event_type.value,
            "role": role_value,
            "vendor_event_id": vendor_event_id,
            "vendor_parent_event_id": vendor_parent_event_id,
            "content_text": content_text,
            # ``content_json`` is a Python dict (or None); the write layer
            # serializes it to JSONB / NULL (no caller ``json.dumps`` / cast).
            "content_json": content_json_obj,
            "content_blob_id": content_blob_id,
            "event_at": normalized.event_at,
            "imported_at": now,
            "batch_id": batch_id,
            "actor_session_label": actor_session_label,
            "actor_agent_instance_id": actor_agent_instance_id,
            "session_vendor": session_vendor.value,
            "source_kind": source_kind.value,
            "usage_json": usage_json_obj,  # same JSONB-or-NULL contract as content_json above
            "created_at": now,
            "updated_at": now,
        }
        inserted = _upsert_event_or_dedup_known_collision(
            self._upsert_do_nothing, record,
        )
        if inserted:
            with self._state.transactional() as txn:
                # Persistent event_count bump (the row-lock fence) — gated on the
                # insert so a re-ingested duplicate never drifts the counter.
                txn.increment_and_return(
                    NAMESPACE,
                    {
                        "table": TABLE_SESSION,
                        "filters": {"id": session_id},
                        "column": "event_count",
                        "by": 1,
                    },
                )
                self._increment_batch_counters(
                    txn,
                    batch_id=batch_id,
                    event_delta=1,
                )
                self._touch_session_counters(
                    txn,
                    session_id=session_id,
                    event_at=normalized.event_at,
                    now=now,
                )
        return EventInsertResult(
            event_id=event_id,
            sequence=provisional_sequence,
            deduped=not inserted,
        )

    # ------------------------------------------------------------------
    # Tool-call projection
    # ------------------------------------------------------------------

    def record_tool_call(
        self,
        *,
        session_id: str,
        call_event_id: str,
        tool_name: str,
        called_at: datetime,
    ) -> str:
        tool_call_id = _new_id(ID_PREFIX_TOOL_CALL)
        now = self._clock()
        self._write(
            TABLE_TOOL_CALL,
            {
                "id": tool_call_id,
                "namespace": NAMESPACE,
                "session_id": session_id,
                "call_event_id": call_event_id,
                "tool_name": tool_name,
                "status": "pending",
                "called_at": called_at,
                "created_at": now,
                "updated_at": now,
            },
        )
        return tool_call_id

    def resolve_tool_call(
        self,
        *,
        call_event_id: str,
        result_event_id: str,
        status: str,
        resolved_at: datetime,
    ) -> None:
        now = self._clock()
        self._update(
            TABLE_TOOL_CALL,
            {"call_event_id": call_event_id},
            {
                "result_event_id": result_event_id,
                "status": status,
                "resolved_at": resolved_at,
                "updated_at": now,
            },
        )

    # ------------------------------------------------------------------
    # Attachments
    # ------------------------------------------------------------------

    def record_attachment(
        self,
        *,
        event_id: str,
        blob_id: str | None,
        mime_type: str,
        filename: str | None,
        size_bytes: int,
    ) -> str:
        attachment_id = _new_id(ID_PREFIX_ATTACHMENT)
        now = self._clock()
        self._write(
            TABLE_ATTACHMENT,
            {
                "id": attachment_id,
                "namespace": NAMESPACE,
                "event_id": event_id,
                "blob_id": blob_id,
                "mime_type": _strip_nuls(mime_type),
                "filename": _strip_nuls(filename),
                "size_bytes": size_bytes,
                "created_at": now,
                "updated_at": now,
            },
        )
        return attachment_id

    # ------------------------------------------------------------------
    # Ingest-private cross-mixin helpers
    # ------------------------------------------------------------------

    def _next_sequence(self, session_id: str) -> int:
        """Provisional per-event ``sequence`` = ``MAX(sequence) + 1`` (1 if none).

        READ-ONLY autocommit ``max_value``, deliberately NOT ``event_count + 1``:
        the event INSERT is an autocommit upsert that commits separately from the
        gated counter txn, so an ``event_count``-based value could reissue a live
        sequence after a crash-in-window (→ ``(session_id, sequence)`` collision).
        ``MAX(sequence)`` reflects only COMMITTED events, so ``MAX + 1`` is always
        collision-free; ``event_count`` is left a gated stat that may drift by one
        on such a crash (display-only, regenerable). No-crash: ``MAX == event_count``.
        Single-threaded serial push (KB ``19_..._05``) fences concurrent readers;
        no auto ``is_deleted`` exclusion is fine — ``__event`` is append-only.
        """
        result = self._state.max_value(
            NAMESPACE,
            {
                "table": TABLE_EVENT,
                "column": "sequence",
                "filters": {"session_id": session_id},
            },
        )
        if result.get("action_status") != ActionStatus.COMPLETED.value:
            raise LedgerRepositoryError(
                f"state-service max_value(sequence) failed: {result.get('error')!r}",
            )
        data = result.get("data")
        inner = data.get("result") if isinstance(data, dict) else None
        value = inner.get("value") if isinstance(inner, dict) else None
        # MAX over an empty set is NULL → the session's first event is sequence 1.
        return int(value) + 1 if value is not None else 1

    def _touch_session_counters(
        self,
        txn: StateTransaction,
        *,
        session_id: str,
        event_at: datetime,
        now: datetime,
    ) -> None:
        # event_count was already incremented by the gated increment_and_return
        # in append_event (which holds the session row lock for the rest of this
        # txn); recompute the last_event_at high-water mark in Python — the
        # GREATEST equivalent — over aware-UTC operands, so out-of-order imports
        # don't regress it.
        # Safe under that row lock: no concurrent writer can change the row
        # between this read and the write-back.
        rows = txn.query_state(
            NAMESPACE,
            {"table": TABLE_SESSION, "filters": {"id": session_id}},
        )
        if not rows:
            raise LedgerRepositoryError(
                f"_touch_session_counters: session {session_id} not found",
            )
        new_last = max(
            _as_aware_utc(rows[0]["last_event_at"]), _as_aware_utc(event_at)
        )
        txn.update_state(
            NAMESPACE,
            {"table": TABLE_SESSION, "filters": {"id": session_id}},
            {"last_event_at": new_last, "updated_at": now},
        )

    def _validate_event_shape(self, normalized: NormalizedSessionEvent) -> None:
        """Enforce per-event-type field-presence contract (spec §9 table).

        Raises ValueError on violation; no defensive fixups. Each branch is
        delegated to a module-level validator keyed off ``event_type`` so the
        per-shape rules can evolve independently and stay individually testable.
        """
        validator = _EVENT_SHAPE_VALIDATORS.get(normalized.event_type)
        if validator is None:  # pragma: no cover - exhaustive enum
            raise ValueError(f"unknown event_type {normalized.event_type!r}")
        validator(normalized)


__all__ = [
    "EventInsertResult",
    "RELOAD_SAFE",
    "SessionLedgerIngestMixin",
    "_EVENT_SHAPE_VALIDATORS",
    "_has_attachment_fields",
    "_validate_attachment_event",
    "_validate_message_event",
    "_validate_system_event",
    "_validate_tool_call_event",
    "_validate_tool_result_event",
]
