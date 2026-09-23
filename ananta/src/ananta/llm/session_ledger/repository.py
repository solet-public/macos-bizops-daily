"""SQL-backed persistence for the LLM session ledger.

Spec §11.1. The repository is a pure SQL boundary: no policy, no validation,
no service-resolution logic — those live on ``SessionLedgerService``. Atomic
writes (sequence allocation, tool-call projection) run inside
``state_service.transactional()``. Read-only queries use the autocommit
``execute_sql`` API.

Per spec §8 preamble, foreign keys are repository-enforced (the platform's
``ColumnDefinition`` has no foreign-key field). Cross-table integrity is
maintained inside transaction blocks.
"""

from __future__ import annotations

import logging

from ananta.llm.session_ledger.base import LedgerRepositoryError
from ananta.llm.session_ledger.canonical_pointer_repair import (
    SessionLedgerCanonicalPointerRepairMixin,
)
from ananta.llm.session_ledger.deployment import SessionLedgerDeploymentMixin
from ananta.llm.session_ledger.duplicate_source_repair import (
    SessionLedgerDuplicateSourceRepairMixin,
)
from ananta.llm.session_ledger.event_external_id_backfill import (
    SessionLedgerEventExternalIdBackfillMixin,
)
from ananta.llm.session_ledger.event_source_denorm_backfill import (
    SessionLedgerEventSourceDenormBackfillMixin,
)
from ananta.llm.session_ledger.ingest import (
    EventInsertResult,
    SessionLedgerIngestMixin,
)
from ananta.llm.session_ledger.inverted_bounds_repair import (
    SessionLedgerInvertedBoundsRepairMixin,
)
from ananta.llm.session_ledger.polling_driver import (
    LeaseLostError,
    PollingLeaseHandle,
    SessionLedgerPollingDriverMixin,
)
from ananta.llm.session_ledger.read import SessionLedgerReadMixin
from ananta.llm.session_ledger.search import SessionLedgerSearchMixin
from ananta.llm.session_ledger.session_source_kind_backfill import (
    SessionLedgerSessionSourceKindBackfillMixin,
)
from ananta.llm.session_ledger.shared import (
    SessionRow,
    SourceRow,
)
from ananta.llm.session_ledger.summarize import SessionLedgerSummarizeMixin

logger = logging.getLogger(__name__)

# Module-level RELOAD_SAFE marker — no module-level mutable state, no
# background threads, no held service references. The repository is a pure
# class adapter; reloading patches the class in place and existing service
# instances pick up the new methods via attribute lookup.
RELOAD_SAFE = True


# W5.O cycles 2-10: every per-method use of the pre-computed table-name
# constants migrated with its method into the relevant domain mixin module.
# The constants stay defined in this orchestrator only when a remaining
# residual method needs them; today no residual method uses them.


# W5.O cycle 1: ``LedgerRepositoryError`` relocated to ``base.py``; it remains
# importable from this module via the import block above. (The cycle-1
# ``_ORDER_BY_SQL`` + ``_build_list_sessions_filters`` SQL composers were
# retired when ``list_sessions`` migrated off raw SQL onto the
# ``session_source_kind`` junction read-then-route + Python window/sort fold.)




# W5.O cycle 1: ``SourceRow`` and ``SessionRow`` relocated to ``shared.py``
# alongside the helper functions that consume them. ``EventInsertResult``
# stays here until cycle 3 (moves to ``ingest.py``).


# W5.O cycle 3: ``EventInsertResult`` relocated to ``ingest.py`` per C8 fold;
# re-exported from this module via the cycle-1 ``__all__`` block so
# ``importer.py``'s import path stays stable.


# ─── Per-event-type shape validators ────────────────────────────────────────
# Spec §9 table — split from a single D-26 method into one validator per
# EventType. Each helper stays A; the orchestrator (`_validate_event_shape`)
# is a dict dispatch.


# W5.O cycle 3: per-event-type shape validators (`_has_attachment_fields`,
# `_validate_message_event`, `_validate_tool_call_event`,
# `_validate_tool_result_event`, `_validate_system_event`,
# `_validate_attachment_event`) and the `_EVENT_SHAPE_VALIDATORS` dispatch
# dict relocated to `ingest.py` alongside their sole consumer
# `_validate_event_shape`.


class SessionLedgerRepository(
    SessionLedgerReadMixin,
    SessionLedgerIngestMixin,
    SessionLedgerPollingDriverMixin,
    SessionLedgerCanonicalPointerRepairMixin,
    SessionLedgerInvertedBoundsRepairMixin,
    SessionLedgerEventExternalIdBackfillMixin,
    SessionLedgerEventSourceDenormBackfillMixin,
    SessionLedgerSessionSourceKindBackfillMixin,
    SessionLedgerSummarizeMixin,
    SessionLedgerDeploymentMixin,
    SessionLedgerSearchMixin,
    SessionLedgerDuplicateSourceRepairMixin,
):
    """SQL adapter over the ``session_ledger`` schema.

    Inheritance grows one mixin per W5.O cycle. As of cycle 3:

    - :class:`SessionLedgerReadMixin` (cycle 2) — 12 read/query methods.
    - :class:`SessionLedgerIngestMixin` (cycle 3) — 8 ingest verbs + 5 private
      cross-mixin helpers (``_resolve_canonical_session``,
      ``_insert_session_with_canonical_dispatch``, ``_next_sequence``,
      ``_touch_session_counters``, ``_validate_event_shape``).
    - :class:`SessionLedgerRepositoryBase` (cycle 1, diamond-root via both
      mixins) — ``__init__`` + ``_state`` + ``_clock`` + the typed read seam
      (``_query`` / ``_query_ordered``, which replaced the retired raw-SQL
      ``_fetch_all``) + ``_increment_batch_counters`` + cross-cutting
      test/utility methods.

    Cycles 4-10 migrated domain-axis method clusters onto sibling mixins
    (polling_driver / canonical_pointer_repair / inverted_bounds_repair
    / summarize / deployment / search). The 2026-06-14 eradication PR1a
    (see ``workbench/2026-06-14_secretgate_full_eradication_design.md``)
    swapped the annotation mixin for a restoration mixin that retains
    only the content-restoration primitives for legacy stripped events.
    MRO stays deterministic thanks to the diamond-root pattern (all
    mixins inherit exactly from :class:`SessionLedgerRepositoryBase`).
    """

    __slots__ = ()

    # ------------------------------------------------------------------
    # Sources
    # ------------------------------------------------------------------

    # W5.O cycle 3: ``insert_source``, ``get_source``, and
    # ``find_source_id_by_kind_and_root_uri`` relocated to
    # ``SessionLedgerIngestMixin`` (``ingest.py``).

    # W5.O cycle 2: ``list_sources`` relocated to ``SessionLedgerReadMixin``
    # (``read.py``). Inherited via MI; callers see no change.

    # ------------------------------------------------------------------
    # Source cursors
    # ------------------------------------------------------------------


    # ------------------------------------------------------------------
    # Sessions
    # ------------------------------------------------------------------

    # W5.O cycle 2: ``list_sessions`` and ``list_quiescent_sessions``
    # relocated to ``SessionLedgerReadMixin`` (``read.py``).

    # W5.O cycle 2: ``list_quiescent_sessions`` and ``list_active_sessions``
    # relocated to ``SessionLedgerReadMixin`` (``read.py``).

    # ------------------------------------------------------------------
    # Events
    # ------------------------------------------------------------------


    # ------------------------------------------------------------------
    # Tool-call projection
    # ------------------------------------------------------------------


    # W5.O cycle 2: ``find_session_id_by_external_session_id`` relocated to
    # ``SessionLedgerReadMixin`` (``read.py``).


    # W5.O cycle 2: ``iter_all_events_for_session`` (renamed from
    # ``fetch_all_events_for_session`` by the 2026-08-16 read-cap sweep),
    # ``find_event_id_by_vendor_id``, ``find_call_event_id_for_resolution``,
    # and ``list_tool_calls`` relocated to ``SessionLedgerReadMixin``
    # (``read.py``).

    # ------------------------------------------------------------------
    # Attachments
    # ------------------------------------------------------------------


    # ------------------------------------------------------------------
    # Leases
    # ------------------------------------------------------------------


    # ------------------------------------------------------------------
    # Quarantine lifecycle

    # ------------------------------------------------------------------
    # M6 — summaries (spec §17.6)
    # ------------------------------------------------------------------





    # W5.O cycle 1: ``count_rows_per_table`` relocated to
    # ``SessionLedgerRepositoryBase`` (``base.py``) as cross-cutting
    # test/utility surface (still reached via MI on the concrete repository).
    # (The dead ``soft_delete_rows_atomic`` was removed in the SQL-lockdown
    # Slice-6 soft-delete cleanup; ``hard_delete_rows_atomic`` was removed in
    # GAP-5 slice 3 when ``reset_ingest_state`` became a non-destructive
    # per-source cursor reset — its last raw-SQL ``DELETE FROM`` site is gone.)

    # W5.O cycle 2: ``list_sessions_by_ids`` relocated to
    # ``SessionLedgerReadMixin`` (``read.py``).


    # ------------------------------------------------------------------
    # Cycle 4b — operator-facing diagnostics (D11 + D13 + D14b)
    # ------------------------------------------------------------------





    # ------------------------------------------------------------------
    # M5 — deployment / shipper pairing (spec §13)
    # ------------------------------------------------------------------


    # ------------------------------------------------------------------
    # Reads — session timeline
    # ------------------------------------------------------------------

    # W5.O cycle 2: ``get_session_timeline`` and
    # ``find_latest_away_summary_for_session`` relocated to
    # ``SessionLedgerReadMixin`` (``read.py``).


    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------


    # W5.O cycle 1: ``_fetch_all`` relocated to ``SessionLedgerRepositoryBase``
    # (``base.py``) per C1 PRIORITY fold, then RETIRED (SQL-lockdown) when the
    # ledger's last raw-SQL read migrated onto the typed ``_query`` seam — along
    # with the ``shared`` SELECT-column parser (``_columns_from_select_sql`` /
    # ``_split_select_pieces`` / ``_SELECT_FROM_RE`` / ``_TRAILING_AS_RE``) it
    # depended on.


# W5.O C8 fold: external consumers (notably ``importer.py``) import
# ``LeaseLostError``, ``PollingLeaseHandle``, ``EventInsertResult`` directly
# from this module; the re-export keeps that surface stable.
__all__ = [
    "EventInsertResult",
    "LeaseLostError",
    "LedgerRepositoryError",
    "PollingLeaseHandle",
    "SessionLedgerRepository",
    "SessionRow",
    "SourceRow",
]
