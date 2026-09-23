"""Duplicate-source repair domain mixin for the session-ledger repository.

Schema-debt-external-id lane, 2b-S1 (2026-08-06). One-shot repair primitives
for retiring a duplicate ``session_ledger__source`` row into its canonical
winner: count/re-point the four child tables that carry a ``source_id``
foreign key (``session``, ``import_batch``, ``source_cursor``,
``active_lease``), then soft-delete the now-orphaned loser row. Full
evidence, the winner-selection rules, and the per-pair quiesce protocol are
in
``workbench/2026-08-06_schema_debt_external_id_findings_schema-debt-impl.md``.

Off raw SQL, same discipline as every other mixin: reads/writes ride
``_query`` / ``_update`` / ``_delete`` (``StateManagementInterface``
primitives via ``SessionLedgerRepositoryBase``), never a bare
``cursor.execute``.
"""

from __future__ import annotations

from ananta.llm.session_ledger.base import SessionLedgerRepositoryBase
from ananta.llm.session_ledger.schema import (
    TABLE_ACTIVE_LEASE,
    TABLE_IMPORT_BATCH,
    TABLE_SESSION,
    TABLE_SOURCE,
    TABLE_SOURCE_CURSOR,
)

# The four tables carrying a ``source_id`` foreign key, in the order they are
# counted/re-pointed. Declared once so the repair verb and its dry-run report
# can never drift apart on "which tables did we check."
SOURCE_CHILD_TABLES: tuple[str, ...] = (
    TABLE_SESSION,
    TABLE_IMPORT_BATCH,
    TABLE_SOURCE_CURSOR,
    TABLE_ACTIVE_LEASE,
)


class SessionLedgerDuplicateSourceRepairMixin(SessionLedgerRepositoryBase):
    def count_source_children(self, source_id: str) -> dict[str, int]:
        """Live row count per child table referencing ``source_id``.

        Read-only; safe to call regardless of ``confirm``. Used both for the
        dry-run report and for the post-re-point orphan verification.
        """
        return {
            table: len(self._query(table, {"source_id": source_id, "is_deleted": 0}))
            for table in SOURCE_CHILD_TABLES
        }

    def get_source_lease_state(self, source_id: str) -> dict[str, object] | None:
        """Raw ``polling_lease_until`` / ``polling_lease_token`` for one source.

        Bypasses ``_row_to_source`` (which does not project the lease
        columns — confirmed by reading it directly) to read them via the
        same ``_query`` primitive every other read in this repository uses.
        Returns ``None`` when the source row does not exist (or is
        soft-deleted); otherwise ``{"polling_lease_until", "polling_lease_token"}``.
        """
        rows = self._query(TABLE_SOURCE, {"id": source_id, "is_deleted": 0})
        if not rows:
            return None
        row = rows[0]
        return {
            "polling_lease_until": row.get("polling_lease_until"),
            "polling_lease_token": row.get("polling_lease_token"),
        }

    def repoint_source_children(
        self, *, loser_source_id: str, winner_source_id: str,
    ) -> dict[str, int]:
        """Re-point every live child row from the loser to the winner source.

        Per-table rows-affected via ``_update`` (the native compare-and-set
        signal — see ``base.py:_update``), one table at a time, in
        ``SOURCE_CHILD_TABLES`` order. Not wrapped in a single cross-table
        transaction: each ``_update`` call is its own autocommit statement
        (matching every other multi-table repair in this repository, e.g.
        ``reset_source_cursor``'s own single-table delete) — the caller
        re-verifies via :meth:`count_source_children` afterward rather than
        relying on atomicity across tables that have no natural single
        transaction boundary here.
        """
        return {
            table: self._update(
                table,
                {"source_id": loser_source_id, "is_deleted": 0},
                {"source_id": winner_source_id},
            )
            for table in SOURCE_CHILD_TABLES
        }

    def retire_source_row(self, source_id: str) -> int:
        """Soft-delete one ``source`` row. Rows-affected (0 or 1).

        Soft, never hard — this repair's own rollback lever. Caller is
        responsible for confirming zero child rows still reference
        ``source_id`` (via :meth:`count_source_children`) before calling
        this; the method itself does not re-check, matching ``_delete``'s
        plain contract elsewhere in this repository.
        """
        return self._delete(TABLE_SOURCE, {"id": source_id, "is_deleted": 0}, soft=True)

    def set_source_enabled(
        self, source_id: str, enabled: bool,
    ) -> dict[str, object] | None:
        """Toggle one ``source`` row's ``enabled`` flag via ``_update``.

        Returns ``None`` when ``source_id`` does not resolve to a live
        (non-soft-deleted) row — the caller (service layer) turns that into
        a loud ``ValueError``. Otherwise
        ``{"prior_enabled": bool, "new_enabled": bool, "changed": bool}``.

        Idempotent: a call whose ``enabled`` already matches the row's
        current value fires ZERO writes and reports ``changed=False`` —
        matching this repository's pattern of never issuing a no-op write
        (see ``_register_source_internal``'s converge-absorb for the same
        discipline on the insert side).
        """
        rows = self._query(TABLE_SOURCE, {"id": source_id, "is_deleted": 0})
        if not rows:
            return None
        prior_enabled = bool(rows[0].get("enabled"))
        if prior_enabled == enabled:
            return {
                "prior_enabled": prior_enabled,
                "new_enabled": prior_enabled,
                "changed": False,
            }
        self._update(
            TABLE_SOURCE,
            {"id": source_id, "is_deleted": 0},
            {"enabled": enabled, "updated_at": self._clock()},
        )
        return {
            "prior_enabled": prior_enabled,
            "new_enabled": enabled,
            "changed": True,
        }


__all__ = ["SOURCE_CHILD_TABLES", "SessionLedgerDuplicateSourceRepairMixin"]
